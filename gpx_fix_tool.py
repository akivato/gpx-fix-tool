#!/usr/bin/env python3
"""
GPX Fix Tool — Fix GPS-jammed runs with a reference path.
Works with local GPX files or directly from your Strava account.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os, math, io, json, time, webbrowser, threading, bisect
import http.server, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

# ─── Strava color palette ──────────────────────────────────────────────────────
C = {
    "bg":       "#1A1A1A",
    "panel":    "#242428",
    "card":     "#2D2D30",
    "border":   "#404040",
    "accent":   "#FC4C02",
    "accent_h": "#FF6A2F",
    "accent_d": "#D93D00",
    "teal":     "#36B37E",
    "text":     "#FFFFFF",
    "text_dim": "#9B9B9B",
    "success":  "#36B37E",
    "error":    "#E8503A",
    "warn":     "#F5A623",
}

FONT_TITLE = ("Segoe UI", 20, "bold")
FONT_HEAD  = ("Segoe UI", 11, "bold")
FONT_BODY  = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO  = ("Consolas", 9)


# ─── GPX data model ────────────────────────────────────────────────────────────

class TrackPoint:
    __slots__ = ("lat", "lon", "ele", "time", "hr", "cadence", "power", "speed")
    def __init__(self):
        self.lat = self.lon = self.ele = None
        self.time = self.hr = self.cadence = self.power = self.speed = None


def _parse_time(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _ns_tag(ns, local):
    return f"{{{ns}}}{local}" if ns else local


def parse_gpx(filepath):
    tree = ET.parse(filepath)
    root = tree.getroot()
    ns = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""
    def find(el, loc): return el.find(_ns_tag(ns, loc))
    points = []
    for trkpt in root.iter(_ns_tag(ns, "trkpt")):
        pt = TrackPoint()
        pt.lat = float(trkpt.get("lat", 0))
        pt.lon = float(trkpt.get("lon", 0))
        ele_el = find(trkpt, "ele")
        if ele_el is not None and ele_el.text:
            try: pt.ele = float(ele_el.text)
            except ValueError: pass
        time_el = find(trkpt, "time")
        if time_el is not None:
            pt.time = _parse_time(time_el.text)
        ext_el = find(trkpt, "extensions")
        if ext_el is not None:
            for child in ext_el.iter():
                local = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()
                text = (child.text or "").strip()
                if not text: continue
                try: val = float(text)
                except ValueError: continue
                if local in ("hr", "heartrate", "heartratebpm"): pt.hr = int(val)
                elif local in ("cad", "cadence", "runcadence"):  pt.cadence = int(val)
                elif local == "power":                            pt.power = int(val)
        points.append(pt)
    return points


# ─── Merge logic ───────────────────────────────────────────────────────────────

IDLE_GAP = 30  # seconds — gaps longer than this are auto-pause


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _build_cum_dist(points):
    cum = [0.0]
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        d = _haversine_km(a.lat, a.lon, b.lat, b.lon) if a.lat is not None and b.lat is not None else 0.0
        cum.append(cum[-1] + d)
    return cum


def _active_elapsed(points, idle_gap=IDLE_GAP):
    elapsed = [0.0]
    for i in range(1, len(points)):
        prev, cur = points[i - 1], points[i]
        if prev.time and cur.time:
            gap = (cur.time - prev.time).total_seconds()
            inc = gap if gap <= idle_gap else 0.0
        else:
            inc = 1.0
        elapsed.append(elapsed[-1] + inc)
    return elapsed


def _build_active_ref(ref_points):
    active, cum = [], []
    running = 0.0
    for i, pt in enumerate(ref_points):
        if i > 0 and pt.time and ref_points[i - 1].time:
            if (pt.time - ref_points[i - 1].time).total_seconds() > IDLE_GAP:
                continue
        active.append(pt)
        if len(active) == 1:
            cum.append(0.0)
        else:
            d = _haversine_km(active[-2].lat, active[-2].lon, pt.lat, pt.lon)
            running += d
            cum.append(running)
    return active, cum


def merge_points(ref_points, actual_points):
    """Use every active reference GPS point as-is; interpolate timestamp + metrics
    from the actual run's active timeline. Gives exact reference GPS for Strava."""
    if not ref_points or not actual_points:
        return []

    active_ref, ref_cum = _build_active_ref(ref_points)
    total_ref_dist = ref_cum[-1]

    act_elapsed = _active_elapsed(actual_points)
    total_active = act_elapsed[-1]

    act_t   = [p.time     for p in actual_points]
    act_hr  = [p.hr       for p in actual_points]
    act_cad = [p.cadence  for p in actual_points]
    act_pwr = [p.power    for p in actual_points]

    def _lerp(target_s):
        if target_s <= act_elapsed[0]:
            return act_t[0], act_hr[0], act_cad[0], act_pwr[0]
        if target_s >= act_elapsed[-1]:
            return act_t[-1], act_hr[-1], act_cad[-1], act_pwr[-1]
        hi = min(bisect.bisect_right(act_elapsed, target_s), len(act_elapsed) - 1)
        lo = hi - 1
        span = act_elapsed[hi] - act_elapsed[lo]
        frac = (target_s - act_elapsed[lo]) / span if span > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        ts = (act_t[lo] + timedelta(seconds=(act_t[hi] - act_t[lo]).total_seconds() * frac)
              if act_t[lo] and act_t[hi] else act_t[lo] or act_t[hi])
        hr  = int(round(act_hr[lo]  + frac * (act_hr[hi]  - act_hr[lo])))  if act_hr[lo]  is not None and act_hr[hi]  is not None else (act_hr[lo]  or act_hr[hi])
        cad = int(round(act_cad[lo] + frac * (act_cad[hi] - act_cad[lo]))) if act_cad[lo] is not None and act_cad[hi] is not None else (act_cad[lo] or act_cad[hi])
        pwr = act_pwr[lo] if act_pwr[lo] is not None else act_pwr[hi]
        return ts, hr, cad, pwr

    merged = []
    n = len(active_ref)
    for i, rp in enumerate(active_ref):
        frac = ref_cum[i] / total_ref_dist if total_ref_dist > 0 else i / max(n - 1, 1)
        ts, hr, cad, pwr = _lerp(frac * total_active)
        mp = TrackPoint()
        mp.lat, mp.lon, mp.ele = rp.lat, rp.lon, rp.ele
        mp.time, mp.hr, mp.cadence, mp.power = ts, hr, cad, pwr
        merged.append(mp)
    return merged


# ─── GPX output ────────────────────────────────────────────────────────────────

def _indent(elem, level=0):
    pad = "\n" + "  " * level
    if len(elem):
        if not (elem.text and elem.text.strip()): elem.text = pad + "  "
        for child in elem:
            _indent(child, level + 1)
        if not (child.tail and child.tail.strip()): child.tail = pad
    if level and not (elem.tail and elem.tail.strip()): elem.tail = pad


def _build_gpx_root(merged_points, run_name="Fixed GPS Run"):
    GPX_NS = "http://www.topografix.com/GPX/1/1"
    TPX_NS = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
    XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
    ET.register_namespace("",       GPX_NS)
    ET.register_namespace("gpxtpx", TPX_NS)
    ET.register_namespace("xsi",    XSI_NS)
    def tag(l): return f"{{{GPX_NS}}}{l}"
    root = ET.Element(tag("gpx"))
    root.set("version", "1.1")
    root.set("creator", "GPX Fix Tool")
    root.set(f"{{{XSI_NS}}}schemaLocation",
             "http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd")
    ET.SubElement(ET.SubElement(root, tag("metadata")), tag("name")).text = run_name
    trk = ET.SubElement(root, tag("trk"))
    ET.SubElement(trk, tag("name")).text = run_name
    ET.SubElement(trk, tag("type")).text = "running"
    seg = ET.SubElement(trk, tag("trkseg"))
    for pt in merged_points:
        if pt.lat is None or pt.lon is None: continue
        trkpt = ET.SubElement(seg, tag("trkpt"))
        trkpt.set("lat", f"{pt.lat:.7f}")
        trkpt.set("lon", f"{pt.lon:.7f}")
        if pt.ele is not None:
            ET.SubElement(trkpt, tag("ele")).text = f"{pt.ele:.2f}"
        if pt.time:
            ET.SubElement(trkpt, tag("time")).text = pt.time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if pt.hr or pt.cadence or pt.power:
            ext = ET.SubElement(trkpt, tag("extensions"))
            tpe = ET.SubElement(ext, f"{{{TPX_NS}}}TrackPointExtension")
            if pt.hr:      ET.SubElement(tpe, f"{{{TPX_NS}}}hr").text  = str(pt.hr)
            if pt.cadence: ET.SubElement(tpe, f"{{{TPX_NS}}}cad").text = str(pt.cadence)
            if pt.power:   ET.SubElement(tpe, f"{{{TPX_NS}}}power").text = str(pt.power)
    _indent(root)
    return root, ET.ElementTree(root)


def write_gpx(merged_points, output_path, run_name="Fixed GPS Run"):
    _, tree = _build_gpx_root(merged_points, run_name)
    with open(output_path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)


def gpx_to_bytes(merged_points, run_name="Fixed GPS Run"):
    _, tree = _build_gpx_root(merged_points, run_name)
    buf = io.BytesIO()
    buf.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    return buf.getvalue()


# ─── Stats ─────────────────────────────────────────────────────────────────────

def _fmt_duration(secs):
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _stats(points):
    if not points: return {}
    times = [p.time for p in points if p.time]
    hrs   = [p.hr   for p in points if p.hr]
    cads  = [p.cadence for p in points if p.cadence]
    lats  = [p.lat  for p in points if p.lat is not None]
    lons  = [p.lon  for p in points if p.lon is not None]
    result = {"points": len(points)}
    if len(times) >= 2:
        total = (max(times) - min(times)).total_seconds()
        result["duration"] = _fmt_duration(total)
        active = 0.0; pauses = 0
        for i in range(1, len(times)):
            g = (times[i] - times[i-1]).total_seconds()
            if g <= IDLE_GAP: active += g
            else: pauses += 1
        result["moving_time"] = _fmt_duration(active)
        result["pause_count"] = pauses
        result["has_pauses"]  = pauses > 0
    else:
        result["duration"] = result["moving_time"] = "—"
        result["pause_count"] = 0; result["has_pauses"] = False
    result["has_time"]    = len(times) > 0
    result["has_hr"]      = len(hrs) > 0
    result["has_cadence"] = len(cads) > 0
    if hrs:  result["hr_avg"] = int(sum(hrs)/len(hrs)); result["hr_max"] = max(hrs)
    if cads: result["cad_avg"] = int(sum(cads)/len(cads))
    if lats:
        result["has_gps"] = True
        km = sum(_haversine_km(lats[i-1],lons[i-1],lats[i],lons[i]) for i in range(1,len(lats)))
        result["distance_km"] = round(km, 2)
    else:
        result["has_gps"] = False
    return result


# ─── Strava API ────────────────────────────────────────────────────────────────

STRAVA_API_BASE  = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_AUTH_URL  = "https://www.strava.com/oauth/authorize"
CONFIG_DIR  = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GPXFixTool")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CALLBACK_PORT = 8765

# ── Embedded Strava app credentials ──────────────────────────────────────────
# Fill these in ONCE before building the exe you share with friends.
# Leave both empty to show the manual credential-entry form (developer mode).
# To get credentials: strava.com/settings/api → create app → set
#   "Authorization Callback Domain" to:  localhost
EMBEDDED_CLIENT_ID     = ""   # e.g. "12345"
EMBEDDED_CLIENT_SECRET = ""   # e.g. "abc123def456..."


class StravaConfig:
    def __init__(self):
        self.client_id = self.client_secret = ""
        self.access_token = self.refresh_token = ""
        self.expires_at = 0
        self.athlete_name = ""
        self.load()
        # Embedded credentials override saved ones (shared-exe mode)
        if EMBEDDED_CLIENT_ID and EMBEDDED_CLIENT_SECRET:
            self.client_id     = EMBEDDED_CLIENT_ID
            self.client_secret = EMBEDDED_CLIENT_SECRET

    def load(self):
        try:
            if os.path.exists(CONFIG_FILE):
                d = json.loads(open(CONFIG_FILE, encoding="utf-8").read())
                # Only load saved client_id/secret when NOT using embedded creds
                if not (EMBEDDED_CLIENT_ID and EMBEDDED_CLIENT_SECRET):
                    self.client_id     = d.get("client_id", "")
                    self.client_secret = d.get("client_secret", "")
                self.access_token  = d.get("access_token", "")
                self.refresh_token = d.get("refresh_token", "")
                self.expires_at    = d.get("expires_at", 0)
                self.athlete_name  = d.get("athlete_name", "")
        except Exception:
            pass

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"client_id": self.client_id, "client_secret": self.client_secret,
                       "access_token": self.access_token, "refresh_token": self.refresh_token,
                       "expires_at": self.expires_at, "athlete_name": self.athlete_name}, f)

    def clear_tokens(self):
        self.access_token = self.refresh_token = self.athlete_name = ""
        self.expires_at = 0
        self.save()

    @property
    def is_configured(self):  return bool(self.client_id and self.client_secret)
    @property
    def is_authenticated(self): return bool(self.access_token)
    @property
    def token_expired(self): return time.time() > self.expires_at - 300


class StravaAPI:
    def __init__(self, cfg): self.cfg = cfg

    def _h(self): return {"Authorization": f"Bearer {self.cfg.access_token}"}

    def _raise(self, e, context=""):
        """Turn an HTTPError into a readable exception."""
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace"))
            msg  = body.get("message", "") or str(body)
        except Exception:
            msg = e.reason
        if e.code == 401:
            raise Exception(
                f"401 Unauthorized{' (' + context + ')' if context else ''}.\n\n"
                f"Your token may be expired or missing write permission.\n"
                f"Please click Disconnect then Connect again to re-authorise."
            )
        if e.code == 403:
            raise Exception(
                f"403 Forbidden{' (' + context + ')' if context else ''}.\n\n"
                f"The token lacks 'activity:write' scope.\n"
                f"Click Disconnect → Connect and approve ALL permissions on the Strava page."
            )
        raise Exception(f"HTTP {e.code} {' (' + context + ')' if context else ''}: {msg}")

    def _get(self, path, params=None):
        url = STRAVA_API_BASE + path
        if params: url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self._h()), timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self._raise(e, f"GET {path}")


    def ensure_token(self):
        if not self.cfg.token_expired: return
        if not self.cfg.refresh_token: raise Exception("Not authenticated — please reconnect.")
        data = urllib.parse.urlencode({
            "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret,
            "refresh_token": self.cfg.refresh_token, "grant_type": "refresh_token",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(STRAVA_TOKEN_URL, data=data, method="POST"), timeout=15) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self._raise(e, "token refresh")
        self.cfg.access_token = d["access_token"]
        self.cfg.refresh_token = d["refresh_token"]
        self.cfg.expires_at = d["expires_at"]
        self.cfg.save()

    def exchange_code(self, code):
        data = urllib.parse.urlencode({
            "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret,
            "code": code, "grant_type": "authorization_code",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(STRAVA_TOKEN_URL, data=data, method="POST"), timeout=15) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self._raise(e, "code exchange")
        self.cfg.access_token  = d["access_token"]
        self.cfg.refresh_token = d["refresh_token"]
        self.cfg.expires_at    = d["expires_at"]
        granted_scope = d.get("scope", "")
        ath = d.get("athlete", {})
        self.cfg.athlete_name  = f"{ath.get('firstname','')} {ath.get('lastname','')}".strip()
        self.cfg.save()
        # Warn immediately if write scope wasn't granted
        if "activity:write" not in granted_scope:
            raise Exception(
                f"Connected as {self.cfg.athlete_name}, but 'activity:write' scope was not granted "
                f"(got: '{granted_scope}').\n\n"
                f"On the Strava authorization page make sure you approve ALL permissions, "
                f"then Disconnect and Connect again."
            )

    def get_activities(self, per_page=50):
        self.ensure_token()
        return self._get("/athlete/activities", {"per_page": per_page, "page": 1})

    def get_streams(self, activity_id):
        self.ensure_token()
        return self._get(f"/activities/{activity_id}/streams",
                         {"keys": "latlng,time,altitude,heartrate,cadence", "key_by_type": "true"})

    def upload_activity(self, gpx_bytes, name, sport_type="running"):
        self.ensure_token()
        boundary = f"GPXFix{int(time.time())}"
        fields   = {"data_type": "gpx", "name": name, "sport_type": sport_type}
        body = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            for k, v in fields.items()
        )
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"activity.gpx\"\r\nContent-Type: application/gpx+xml\r\n\r\n").encode()
        body += gpx_bytes + f"\r\n--{boundary}--\r\n".encode()
        headers = {**self._h(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        req = urllib.request.Request(STRAVA_API_BASE + "/uploads",
                                     data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self._raise(e, "POST /uploads")

    def poll_upload(self, upload_id, timeout=90):
        self.ensure_token()
        for _ in range(timeout):
            s = self._get(f"/uploads/{upload_id}")
            if s.get("activity_id"): return s["activity_id"]
            if s.get("error"):       raise Exception(f"Upload error: {s['error']}")
            time.sleep(1)
        raise Exception("Upload timed out — check Strava manually.")


def streams_to_points(streams, start_time_str):
    """Convert Strava activity streams dict to TrackPoint list."""
    start = _parse_time(start_time_str)
    if not start:
        try: start = datetime.strptime(start_time_str[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception: start = None
    latlng    = (streams.get("latlng")    or {}).get("data", [])
    times_s   = (streams.get("time")      or {}).get("data", [])
    altitudes = (streams.get("altitude")  or {}).get("data", [])
    hrs       = (streams.get("heartrate") or {}).get("data", [])
    cadences  = (streams.get("cadence")   or {}).get("data", [])
    points = []
    for i in range(len(latlng)):
        pt = TrackPoint()
        if i < len(latlng) and latlng[i]:
            pt.lat, pt.lon = latlng[i][0], latlng[i][1]
        if i < len(times_s) and start:
            pt.time = start + timedelta(seconds=times_s[i])
        if i < len(altitudes) and altitudes[i] is not None:
            pt.ele = altitudes[i]
        if i < len(hrs) and hrs[i] is not None:
            pt.hr = int(hrs[i])
        if i < len(cadences) and cadences[i] is not None:
            pt.cadence = int(cadences[i])
        points.append(pt)
    return points


def _do_oauth(cfg):
    """Open browser for Strava auth, wait for callback, return auth code."""
    scope = "activity:read_all,activity:write"
    auth_url = (f"{STRAVA_AUTH_URL}?client_id={cfg.client_id}"
                f"&redirect_uri=http://localhost:{CALLBACK_PORT}/callback"
                f"&response_type=code&approval_prompt=force&scope={scope}")

    result = [None, None]  # [code, error]

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in p:  result[0] = p["code"][0]
            else:             result[1] = p.get("error", ["unknown"])[0]
            html = (b"<html><body style='font-family:sans-serif;background:#1A1A1A;color:#fff;"
                    b"display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                    b"<div style='text-align:center'><div style='font-size:64px'>&#x2713;</div>"
                    b"<h2>Connected! You can close this tab.</h2></div></body></html>"
                    if result[0] else
                    b"<html><body style='font-family:sans-serif;background:#1A1A1A;color:#E8503A;"
                    b"display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                    b"<h2>Authentication failed. Please close this tab and try again.</h2></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)
        def log_message(self, *a): pass

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _Handler)
    server.timeout = 120
    webbrowser.open(auth_url)
    deadline = time.time() + 120
    while result[0] is None and result[1] is None:
        if time.time() > deadline: break
        server.handle_request()
    server.server_close()
    if result[1]: raise Exception(f"Strava auth denied: {result[1]}")
    if not result[0]: raise Exception("Auth timed out — no response from Strava after 2 minutes.")
    return result[0]


# ─── GUI helpers ───────────────────────────────────────────────────────────────

def _btn(parent, text, cmd, color=None, **kw):
    color = color or C["accent"]
    kw.setdefault("font",   FONT_BODY)
    kw.setdefault("padx",   14)
    kw.setdefault("pady",   6)
    return tk.Button(parent, text=text, command=cmd,
                     bg=color, fg="white", relief="flat",
                     cursor="hand2", activebackground=C["accent_h"],
                     activeforeground="white", **kw)


def _log_widget(parent):
    f = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
    tk.Label(f, text="Log", fg=C["text_dim"], bg=C["card"], font=FONT_SMALL, anchor="w"
             ).pack(fill="x", padx=10, pady=(6, 0))
    t = tk.Text(f, bg=C["card"], fg=C["text_dim"], font=FONT_MONO, relief="flat",
                wrap="word", state="disabled", height=6)
    t.pack(fill="both", expand=True, padx=10, pady=(0, 8))
    t.tag_config("ok",   foreground=C["success"])
    t.tag_config("err",  foreground=C["error"])
    t.tag_config("info", foreground=C["teal"])
    t.tag_config("warn", foreground=C["warn"])
    return f, t


def _append_log(widget, msg, tag=""):
    widget.config(state="normal")
    widget.insert("end", f"[{datetime.now():%H:%M:%S}] {msg}\n", tag)
    widget.see("end")
    widget.config(state="disabled")


# ─── Local Files Tab ───────────────────────────────────────────────────────────

class FileCard(tk.Frame):
    def __init__(self, parent, label, role, callback, **kw):
        super().__init__(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["border"], **kw)
        self.role = role; self.callback = callback
        self.filepath = tk.StringVar(); self.points = []
        self._build(label)

    def _build(self, label):
        hdr = tk.Frame(self, bg=C["card"])
        hdr.pack(fill="x", padx=14, pady=(14, 0))
        dot = C["teal"] if self.role == "reference" else C["accent"]
        tk.Label(hdr, text="●", fg=dot, bg=C["card"], font=("Segoe UI", 12)).pack(side="left", padx=(0, 6))
        tk.Label(hdr, text=label, fg=C["text"], bg=C["card"], font=FONT_HEAD).pack(side="left")

        row = tk.Frame(self, bg=C["card"])
        row.pack(fill="x", padx=14, pady=6)
        tk.Entry(row, textvariable=self.filepath, bg=C["panel"], fg=C["text_dim"],
                 insertbackground=C["text"], relief="flat", font=FONT_MONO,
                 highlightthickness=1, highlightbackground=C["border"],
                 highlightcolor=C["accent"], state="readonly"
                 ).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        _btn(row, "Browse…", self._browse).pack(side="right")

        self._stats_lbl = tk.Label(self, text="No file loaded", fg=C["text_dim"],
                                    bg=C["card"], font=FONT_SMALL, anchor="w")
        self._stats_lbl.pack(fill="x", padx=14, pady=(0, 14))

    def _browse(self):
        p = filedialog.askopenfilename(title="Select GPX file",
                                        filetypes=[("GPX files", "*.gpx"), ("All files", "*.*")])
        if not p: return
        self.filepath.set(p)
        try:
            self.points = parse_gpx(p)
            s = _stats(self.points)
            dur = s.get("duration", "—")
            if s.get("has_pauses"):
                dur += f"  (moving: {s['moving_time']}, {s['pause_count']} pause{'s' if s['pause_count']>1 else ''})"
            gps = "✓ GPS" if s.get("has_gps") else "✗ No GPS"
            hr  = f"✓ HR avg {s['hr_avg']} / max {s['hr_max']} bpm" if s.get("has_hr") else "✗ No HR"
            cad = f"✓ Cadence {s['cad_avg']} spm" if s.get("has_cadence") else ""
            dist = f"  dist ≈ {s['distance_km']} km" if s.get("distance_km") else ""
            parts = [gps, hr] + ([cad] if cad else [])
            lines = [f"  {s['points']} pts   ·   {dur}", "  " + "   ·   ".join(parts)]
            if dist: lines.append(dist)
            color = C["success"] if s.get("has_gps") and s.get("has_time") else C["warn"]
            self._stats_lbl.config(text="\n".join(lines), fg=color)
        except Exception as e:
            self.points = []
            self._stats_lbl.config(text=f"Error: {e}", fg=C["error"])
        self.callback()


class LocalFilesTab(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C["bg"], **kw)
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=C["bg"])
        wrap.pack(fill="both", expand=True, padx=24, pady=16)

        self.ref_card = FileCard(wrap, "Reference Run  (good GPS, same path)", "reference", self._on_change)
        self.ref_card.pack(fill="x", pady=(0, 10))
        self.act_card = FileCard(wrap, "Actual Run  (GPS jammed — has time & BPM)", "actual", self._on_change)
        self.act_card.pack(fill="x", pady=(0, 18))

        btn_row = tk.Frame(wrap, bg=C["bg"])
        btn_row.pack(fill="x")
        self.merge_btn = _btn(btn_row, "⚡  Merge GPS + Run Data", self._start_merge,
                               font=("Segoe UI", 12, "bold"), pady=10, state="disabled")
        self.merge_btn.pack(side="left")
        self._status = tk.Label(btn_row, text="", fg=C["text_dim"], bg=C["bg"], font=FONT_BODY)
        self._status.pack(side="left", padx=16)

        sty = ttk.Style()
        sty.configure("F.Horizontal.TProgressbar", troughcolor=C["panel"],
                       background=C["accent"], bordercolor=C["border"])
        self.prog = ttk.Progressbar(wrap, style="F.Horizontal.TProgressbar",
                                    mode="indeterminate")
        self.prog.pack(fill="x", pady=(12, 0))

        self._log_frame, self._log = _log_widget(wrap)
        self._log_frame.pack(fill="both", expand=True, pady=(14, 0))
        _append_log(self._log, "Ready. Load a reference GPX and an actual run GPX to begin.", "info")

    def _on_change(self):
        ready = bool(self.ref_card.points and self.act_card.points)
        self.merge_btn.config(state="normal" if ready else "disabled")
        if ready:
            _append_log(self._log, f"Both files loaded — ref: {len(self.ref_card.points)} pts, "
                        f"actual: {len(self.act_card.points)} pts.", "info")

    def _start_merge(self):
        self.merge_btn.config(state="disabled"); self.prog.start(12)
        self._status.config(text="Working…", fg=C["warn"])
        threading.Thread(target=self._do_merge, daemon=True).start()

    def _do_merge(self):
        try:
            merged  = merge_points(self.ref_card.points, self.act_card.points)
            apath   = self.act_card.filepath.get()
            base, _ = os.path.splitext(apath)
            out     = base + "_fixed.gpx"
            name    = os.path.splitext(os.path.basename(apath))[0] + " (GPS Fixed)"
            write_gpx(merged, out, run_name=name)
            s = _stats(merged)
            self.after(0, self._done, out, s)
        except Exception as e:
            self.after(0, self._error, str(e))

    def _done(self, path, s):
        self.prog.stop(); self.merge_btn.config(state="normal")
        self._status.config(text="Done!", fg=C["success"])
        _append_log(self._log, f"✓ Saved: {path}", "ok")
        _append_log(self._log,
                    f"  {s.get('points',0)} pts · {s.get('duration','—')}"
                    + (f" · HR avg {s['hr_avg']}/{s['hr_max']} bpm" if s.get("has_hr") else "")
                    + (f" · {s['distance_km']} km" if s.get("distance_km") else ""), "ok")
        messagebox.showinfo("Done", f"Saved to:\n{path}")

    def _error(self, msg):
        self.prog.stop(); self.merge_btn.config(state="normal")
        self._status.config(text="Error", fg=C["error"])
        _append_log(self._log, f"✗ {msg}", "err")
        messagebox.showerror("Error", msg)


# ─── Strava Tab ────────────────────────────────────────────────────────────────

def _fmt_dist(meters):
    return f"{meters/1000:.1f} km" if meters else "—"


def _fmt_pace(meters, seconds):
    if not meters or not seconds: return "—"
    spm = seconds / (meters / 1000)
    m, s = divmod(int(spm), 60)
    return f"{m}:{s:02d} /km"


def _activity_date(iso_str):
    try:
        dt = datetime.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%b %d, %Y  %H:%M")
    except Exception:
        return iso_str[:16]


class ActivityPicker(tk.Frame):
    """Scrollable list of Strava activities; one can be selected."""

    def __init__(self, parent, label, role, on_select, **kw):
        super().__init__(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["border"], **kw)
        self.role = role
        self.on_select = on_select
        self.selected_id = None
        self.selected_name = None
        self._rows = []
        self._activities = []
        self._build(label)

    def _build(self, label):
        dot = C["teal"] if self.role == "reference" else C["accent"]
        hdr = tk.Frame(self, bg=C["card"])
        hdr.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(hdr, text="●", fg=dot, bg=C["card"],
                 font=("Segoe UI", 12)).pack(side="left", padx=(0, 6))
        tk.Label(hdr, text=label, fg=C["text"], bg=C["card"],
                 font=FONT_HEAD).pack(side="left")

        # Column headers
        col_hdr = tk.Frame(self, bg=C["panel"])
        col_hdr.pack(fill="x")
        for txt, w in [("Date & Name", 220), ("Dist", 70), ("Pace", 70)]:
            tk.Label(col_hdr, text=txt, fg=C["text_dim"], bg=C["panel"],
                     font=FONT_SMALL, width=0, anchor="w",
                     padx=10).pack(side="left")

        # Scrollable list
        container = tk.Frame(self, bg=C["card"])
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg=C["card"], bd=0, highlightthickness=0)
        sb = tk.Scrollbar(container, orient="vertical", command=canvas.yview,
                          bg=C["panel"], troughcolor=C["panel"])
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._inner = tk.Frame(canvas, bg=C["card"])
        self._win_id = canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
                         lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(self._win_id, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        self._canvas = canvas
        self._placeholder = tk.Label(self._inner, text="No activities loaded",
                                     fg=C["text_dim"], bg=C["card"], font=FONT_BODY,
                                     pady=20)
        self._placeholder.pack()

        # Selected info bar
        self._sel_lbl = tk.Label(self, text="Nothing selected", fg=C["text_dim"],
                                  bg=C["panel"], font=FONT_SMALL, anchor="w", padx=12, pady=6)
        self._sel_lbl.pack(fill="x")

    def load(self, activities):
        self._activities = activities
        for w in self._inner.winfo_children():
            w.destroy()
        self._rows.clear()
        self.selected_id = None

        if not activities:
            tk.Label(self._inner, text="No running activities found",
                     fg=C["text_dim"], bg=C["card"], font=FONT_BODY, pady=20).pack()
            return

        for act in activities:
            self._add_row(act)

    def _add_row(self, act):
        aid   = act.get("id")
        name  = act.get("name", "Untitled")
        date  = _activity_date(act.get("start_date_local", ""))
        dist  = _fmt_dist(act.get("distance", 0))
        pace  = _fmt_pace(act.get("distance"), act.get("moving_time"))

        row = tk.Frame(self._inner, bg=C["card"], cursor="hand2")
        row.pack(fill="x", pady=1)

        line1 = tk.Label(row, text=date, fg=C["text_dim"], bg=C["card"],
                         font=FONT_SMALL, anchor="w", padx=10)
        line1.pack(fill="x", pady=(4, 0))
        line2 = tk.Frame(row, bg=C["card"])
        line2.pack(fill="x", pady=(0, 4))
        tk.Label(line2, text=name, fg=C["text"], bg=C["card"],
                 font=FONT_BODY, anchor="w", padx=10).pack(side="left")
        tk.Label(line2, text=f"{dist}  {pace}", fg=C["text_dim"], bg=C["card"],
                 font=FONT_SMALL, anchor="e", padx=10).pack(side="right")

        sep = tk.Frame(self._inner, bg=C["border"], height=1)
        sep.pack(fill="x")

        for w in (row, line1, line2) + tuple(line2.winfo_children()) + tuple(row.winfo_children()):
            w.bind("<Button-1>", lambda e, a=act: self._select(a))

        self._rows.append((aid, row))

    def _select(self, act):
        aid  = act["id"]
        name = act.get("name", "Untitled")
        dot  = C["teal"] if self.role == "reference" else C["accent"]

        # Highlight selected row, de-highlight others
        for rid, rw in self._rows:
            bg = C["panel"] if rid == aid else C["card"]
            for w in [rw] + list(rw.winfo_children()):
                try: w.config(bg=bg)
                except Exception: pass
            for child in rw.winfo_children():
                for gc in child.winfo_children():
                    try: gc.config(bg=bg)
                    except Exception: pass

        self.selected_id = aid
        self.selected_name = name
        dist  = _fmt_dist(act.get("distance", 0))
        pace  = _fmt_pace(act.get("distance"), act.get("moving_time"))
        self._sel_lbl.config(
            text=f"✓  {name}  ·  {dist}  ·  {pace}",
            fg=dot)
        self.on_select()

    def deselect_if(self, aid):
        if self.selected_id == aid:
            self.selected_id = None
            self.selected_name = None
            self._sel_lbl.config(text="Nothing selected", fg=C["text_dim"])
            for _, rw in self._rows:
                for w in [rw] + list(rw.winfo_children()):
                    try: w.config(bg=C["card"])
                    except Exception: pass
                for ch in rw.winfo_children():
                    for gc in ch.winfo_children():
                        try: gc.config(bg=C["card"])
                        except Exception: pass


class StravaTab(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C["bg"], **kw)
        self.cfg = StravaConfig()
        self.api = StravaAPI(self.cfg)
        self._activities = []
        self._build()
        if self.cfg.is_authenticated:
            self._update_connected_ui()
        else:
            self._update_disconnected_ui()

    def _build(self):
        wrap = tk.Frame(self, bg=C["bg"])
        wrap.pack(fill="both", expand=True, padx=24, pady=16)

        # ── Connection header ─────────────────────────────────────────────
        self._conn_frame = tk.Frame(wrap, bg=C["panel"], pady=10)
        self._conn_frame.pack(fill="x", pady=(0, 14))
        conn = self._conn_frame  # alias for readability below

        self._athlete_lbl = tk.Label(conn, text="Not connected to Strava",
                                      fg=C["text_dim"], bg=C["panel"], font=FONT_HEAD)
        self._athlete_lbl.pack(side="left", padx=14)

        self._disconnect_btn = _btn(conn, "Disconnect", self._disconnect,
                                     color=C["panel"])
        self._disconnect_btn.config(fg=C["text_dim"])
        self._disconnect_btn.pack(side="right", padx=8)

        self._connect_btn = _btn(conn, "⚡  Connect to Strava", self._start_connect)
        self._connect_btn.pack(side="right", padx=8)

        # ── API setup (only shown when no embedded credentials) ───────────
        self._setup_frame = tk.Frame(wrap, bg=C["card"],
                                      highlightthickness=1, highlightbackground=C["border"])
        self._id_var     = tk.StringVar(value=self.cfg.client_id)
        self._secret_var = tk.StringVar(value=self.cfg.client_secret)

        if not (EMBEDDED_CLIENT_ID and EMBEDDED_CLIENT_SECRET):
            # Developer / manual-credentials mode — populate the setup card
            hint = ("To connect, you need a free Strava API app (2 minutes):\n"
                    "1. Go to  strava.com/settings/api\n"
                    "2. Set Authorization Callback Domain to:  localhost\n"
                    "3. Paste your Client ID and Client Secret below")
            tk.Label(self._setup_frame, text=hint, fg=C["text_dim"], bg=C["card"],
                     font=FONT_SMALL, justify="left", anchor="w",
                     padx=14, pady=10).pack(fill="x")

            creds = tk.Frame(self._setup_frame, bg=C["card"])
            creds.pack(fill="x", padx=14, pady=(0, 12))
            for lbl, attr in [("Client ID", "_id_var"), ("Client Secret", "_secret_var")]:
                tk.Label(creds, text=lbl, fg=C["text_dim"], bg=C["card"],
                         font=FONT_SMALL).pack(side="left", padx=(0, 4))
                var = getattr(self, attr)
                tk.Entry(creds, textvariable=var, bg=C["panel"], fg=C["text"],
                         insertbackground=C["text"], relief="flat", font=FONT_MONO,
                         highlightthickness=1, highlightbackground=C["border"],
                         width=22).pack(side="left", ipady=4, padx=(0, 16))

        # Pack setup_frame now (correct position: right after conn header).
        # _update_connected_ui will hide it; _update_disconnected_ui will re-show it.
        self._setup_frame.pack(fill="x", pady=(0, 14), after=self._conn_frame)

        # ── Two activity pickers ──────────────────────────────────────────
        pickers = tk.Frame(wrap, bg=C["bg"])
        pickers.pack(fill="both", expand=True, pady=(0, 14))
        pickers.columnconfigure(0, weight=1)
        pickers.columnconfigure(1, weight=1)

        self.ref_picker = ActivityPicker(pickers, "Reference Run  (good GPS)",
                                          "reference", self._on_select)
        self.ref_picker.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.fix_picker = ActivityPicker(pickers, "Activity to Fix  (GPS jammed)",
                                          "actual", self._on_select)
        self.fix_picker.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # ── Action row ────────────────────────────────────────────────────
        act_row = tk.Frame(wrap, bg=C["bg"])
        act_row.pack(fill="x", pady=(4, 0))

        # Upload button anchored to the right — pack before left items
        self.upload_btn = _btn(act_row, "Upload to Strava",
                               self._start_upload, color=C["teal"],
                               font=("Segoe UI", 12, "bold"), pady=10, state="disabled")
        self.upload_btn.pack(side="right")

        self.fix_btn = _btn(act_row, "Fix & Save GPX",
                             self._start_fix,
                             font=("Segoe UI", 12, "bold"), pady=10, state="disabled")
        self.fix_btn.pack(side="left")

        self._status = tk.Label(act_row, text="", fg=C["text_dim"],
                                 bg=C["bg"], font=FONT_BODY)
        self._status.pack(side="left", padx=16)

        # Saved state passed from step 1 → step 3
        self._saved_gpx_path = None
        self._saved_fix_id   = None
        self._saved_fix_name = None

        sty = ttk.Style()
        sty.configure("S.Horizontal.TProgressbar", troughcolor=C["panel"],
                       background=C["accent"], bordercolor=C["border"])
        self.prog = ttk.Progressbar(wrap, style="S.Horizontal.TProgressbar",
                                    mode="indeterminate")
        self.prog.pack(fill="x", pady=(12, 0))

        self._log_frame, self._log = _log_widget(wrap)
        self._log_frame.pack(fill="both", pady=(12, 0))

    def _update_connected_ui(self):
        self._connect_btn.pack_forget()
        self._setup_frame.pack_forget()
        self._athlete_lbl.config(
            text=f"Connected as  {self.cfg.athlete_name}",
            fg=C["success"])
        self._disconnect_btn.config(fg=C["text"], bg=C["card"])
        self._load_activities()

    def _update_disconnected_ui(self):
        self._disconnect_btn.config(fg=C["text_dim"], bg=C["panel"])
        self._athlete_lbl.config(text="Not connected to Strava", fg=C["text_dim"])
        self._connect_btn.pack(side="right", padx=8)
        # Re-show credential form in the correct position (manual mode only)
        if not (EMBEDDED_CLIENT_ID and EMBEDDED_CLIENT_SECRET) and not self.cfg.is_configured:
            self._setup_frame.pack(fill="x", pady=(0, 14), after=self._conn_frame)

    def _start_connect(self):
        # In embedded mode credentials are already set; in manual mode read from fields
        if not (EMBEDDED_CLIENT_ID and EMBEDDED_CLIENT_SECRET):
            self.cfg.client_id     = self._id_var.get().strip()
            self.cfg.client_secret = self._secret_var.get().strip()
            if not self.cfg.is_configured:
                messagebox.showwarning("Missing credentials",
                                       "Please enter your Strava Client ID and Client Secret.")
                return
        self.cfg.save()
        self._connect_btn.config(state="disabled", text="Waiting for browser…")
        self._status.config(text="Opening Strava in your browser…", fg=C["warn"])

        def _thread():
            try:
                code = _do_oauth(self.cfg)
                self.api.exchange_code(code)
                self.after(0, self._connected_ok)
            except Exception as e:
                self.after(0, self._connect_err, str(e))

        threading.Thread(target=_thread, daemon=True).start()

    def _connected_ok(self):
        self._connect_btn.config(state="normal", text="⚡  Connect to Strava")
        self._status.config(text="")
        _append_log(self._log, f"Connected as {self.cfg.athlete_name}", "ok")
        self._update_connected_ui()

    def _connect_err(self, msg):
        self._connect_btn.config(state="normal", text="⚡  Connect to Strava")
        self._status.config(text="Connection failed", fg=C["error"])
        _append_log(self._log, f"✗ {msg}", "err")

    def _disconnect(self):
        self.cfg.clear_tokens()
        self.ref_picker.load([])
        self.fix_picker.load([])
        self._activities = []
        self.fix_btn.config(state="disabled")
        self._update_disconnected_ui()
        _append_log(self._log, "Disconnected.", "info")

    def _load_activities(self):
        self._status.config(text="Loading activities…", fg=C["warn"])
        self.prog.start(12)

        def _thread():
            try:
                all_acts = self.api.get_activities(per_page=50)
                runs = [a for a in all_acts
                        if a.get("sport_type", "").lower() in
                           ("run", "trailrun", "treadmill", "virtualrun", "running")]
                self.after(0, self._activities_loaded, runs)
            except Exception as e:
                self.after(0, self._load_err, str(e))

        threading.Thread(target=_thread, daemon=True).start()

    def _activities_loaded(self, runs):
        self.prog.stop()
        self._status.config(text="")
        self._activities = runs
        self.ref_picker.load(runs)
        self.fix_picker.load(runs)
        _append_log(self._log, f"Loaded {len(runs)} running activities.", "info")

    def _load_err(self, msg):
        self.prog.stop()
        self._status.config(text="Load failed", fg=C["error"])
        _append_log(self._log, f"✗ {msg}", "err")

    def _on_select(self):
        # Prevent selecting the same activity for both roles
        ref_id = self.ref_picker.selected_id
        fix_id = self.fix_picker.selected_id
        if ref_id and ref_id == fix_id:
            # Deselect whichever was just set (the one that matches the other)
            self.fix_picker.deselect_if(fix_id)
            messagebox.showwarning("Same activity", "Please select two different activities.")
            return
        ready = bool(ref_id and fix_id)
        self.fix_btn.config(state="normal" if ready else "disabled")
        # New selection invalidates any previously saved GPX
        self.upload_btn.config(state="disabled")
        self._saved_gpx_path = self._saved_fix_id = self._saved_fix_name = None

    def _start_fix(self):
        ref_id  = self.ref_picker.selected_id
        fix_id  = self.fix_picker.selected_id
        fix_name = self.fix_picker.selected_name or "activity"
        if not ref_id or not fix_id:
            return

        msg = (f"The fixed GPX for:\n\n"
               f"  \"{fix_name}\"\n\n"
               f"will be saved to your Downloads folder.\n\n"
               f"Afterwards the original activity will open in your browser\n"
               f"so you can delete it and upload the fixed file manually.\n\n"
               f"Continue?")
        if not messagebox.askyesno("Fix & Save GPX", msg):
            return

        self.fix_btn.config(state="disabled")
        self.prog.start(12)
        self._status.config(text="Working…", fg=C["warn"])
        _append_log(self._log, "Fetching streams…", "info")

        threading.Thread(target=self._do_fix,
                         args=(ref_id, fix_id, fix_name), daemon=True).start()

    def _do_fix(self, ref_id, fix_id, fix_name):
        try:
            # 1 — download streams
            self.after(0, _append_log, self._log, "Fetching reference streams…", "")
            ref_streams = self.api.get_streams(ref_id)
            ref_acts    = [a for a in self._activities if a["id"] == ref_id]
            ref_start   = ref_acts[0]["start_date"] if ref_acts else ""
            ref_points  = streams_to_points(ref_streams, ref_start)

            self.after(0, _append_log, self._log, "Fetching activity-to-fix streams…", "")
            fix_streams = self.api.get_streams(fix_id)
            fix_acts    = [a for a in self._activities if a["id"] == fix_id]
            fix_start   = fix_acts[0]["start_date"] if fix_acts else ""
            fix_points  = streams_to_points(fix_streams, fix_start)

            self.after(0, _append_log, self._log,
                       f"Merging {len(ref_points)} ref pts × {len(fix_points)} actual pts…", "")

            # 2 — merge
            merged = merge_points(ref_points, fix_points)
            gpx_bytes = gpx_to_bytes(merged, f"{fix_name} (GPS Fixed)")

            s = _stats(merged)
            self.after(0, _append_log, self._log,
                       f"Merged: {s.get('points',0)} pts · {s.get('distance_km','?')} km · "
                       f"HR avg {s.get('hr_avg','?')} bpm", "")

            # 3 — save to Downloads
            safe_name = "".join(c if c.isalnum() or c in " ._-()" else "_" for c in fix_name)
            out_path = os.path.join(os.path.expanduser("~"), "Downloads",
                                    f"{safe_name}_fixed.gpx")
            with open(out_path, "wb") as f:
                f.write(gpx_bytes)

            self.after(0, self._fix_done, fix_id, fix_name, out_path)

        except Exception as e:
            self.after(0, self._fix_error, str(e))

    def _fix_done(self, fix_id, fix_name, out_path):
        self.prog.stop()
        self.fix_btn.config(state="normal")
        self._status.config(text="GPX saved — step 2: delete original", fg=C["warn"])

        # Store for step 3
        self._saved_gpx_path = out_path
        self._saved_fix_id   = fix_id
        self._saved_fix_name = fix_name

        old_url = f"https://www.strava.com/activities/{fix_id}"

        _append_log(self._log, f"✓ Step 1 done — saved: {out_path}", "ok")
        _append_log(self._log,  "  Step 2: delete the original on Strava (browser opening…)", "warn")
        _append_log(self._log, f"  {old_url}", "warn")
        _append_log(self._log,  "  Step 3: click  Upload to Strava  when done.", "info")

        messagebox.showinfo(
            "Step 1 done — GPX saved",
            f'Fixed GPX saved to:\n{out_path}\n\n'
            f'Step 2: The original activity is opening in your browser.\n'
            f'        Delete it there:  ⋯ menu → Delete\n\n'
            f'Step 3: Come back here and click  "Upload to Strava".')
        webbrowser.open(old_url)

        # Enable step 3
        self.upload_btn.config(state="normal")

    def _fix_error(self, msg):
        self.prog.stop()
        self.fix_btn.config(state="normal")
        self._status.config(text="Error", fg=C["error"])
        _append_log(self._log, f"✗ {msg}", "err")
        messagebox.showerror("Error", msg)

    # ── Step 3: Upload ────────────────────────────────────────────────────

    def _start_upload(self):
        if not self._saved_gpx_path or not os.path.exists(self._saved_gpx_path):
            messagebox.showerror("No file", "No saved GPX found. Run Step 1 first.")
            return

        self.upload_btn.config(state="disabled")
        self.fix_btn.config(state="disabled")
        self.prog.start(12)
        self._status.config(text="Uploading…", fg=C["warn"])
        _append_log(self._log, f"Uploading {os.path.basename(self._saved_gpx_path)}…", "info")

        fix_acts = [a for a in self._activities if a["id"] == self._saved_fix_id]
        sport_type = fix_acts[0].get("sport_type", "Run") if fix_acts else "Run"

        threading.Thread(target=self._do_upload,
                         args=(self._saved_gpx_path,
                               f"{self._saved_fix_name} (GPS Fixed)",
                               sport_type),
                         daemon=True).start()

    def _do_upload(self, path, name, sport_type):
        try:
            gpx_bytes = open(path, "rb").read()
            result    = self.api.upload_activity(gpx_bytes, name, sport_type)
            upload_id = result.get("id") or result.get("upload_id")
            if not upload_id:
                raise Exception(f"Unexpected upload response: {result}")
            self.after(0, _append_log, self._log, "Processing on Strava…", "")
            new_id = self.api.poll_upload(upload_id)
            self.after(0, self._upload_done, new_id, name)
        except Exception as e:
            self.after(0, self._upload_error, str(e))

    def _upload_done(self, new_id, name):
        self.prog.stop()
        self.fix_btn.config(state="normal")
        self.upload_btn.config(state="disabled")   # job done — disable until next fix
        self._status.config(text="Uploaded!", fg=C["success"])

        new_url = f"https://www.strava.com/activities/{new_id}"
        _append_log(self._log, f"✓ Uploaded → {new_url}", "ok")

        if messagebox.askyesno("Done!", f'"{name}" is live on Strava.\n\nOpen it in browser?'):
            webbrowser.open(new_url)
        self._load_activities()

    def _upload_error(self, msg):
        self.prog.stop()
        self.fix_btn.config(state="normal")
        self.upload_btn.config(state="normal")   # let them retry
        self._status.config(text="Upload failed", fg=C["error"])
        _append_log(self._log, f"✗ {msg}", "err")
        messagebox.showerror("Upload error", msg)


# ─── Main window ───────────────────────────────────────────────────────────────

class GPXFixApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GPX Fix Tool")
        self.configure(bg=C["bg"])
        self.resizable(True, True)
        self.minsize(700, 620)
        self._center()
        self._build()

    def _center(self):
        self.update_idletasks()
        w, h = 860, 700
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    def _build(self):
        # ── Title bar ─────────────────────────────────────────────────────
        title_row = tk.Frame(self, bg=C["bg"])
        title_row.pack(fill="x", padx=24, pady=(18, 0))

        # Badge icon — canvas clips sprite to 48×48, falls back to hexagon
        self._badge_canvas = tk.Canvas(title_row, width=48, height=48,
                                        bg=C["bg"], highlightthickness=0,
                                        cursor="hand2")
        self._badge_canvas.pack(side="left", padx=(0, 10))
        self._badge_canvas.bind("<Button-1>",
            lambda e: webbrowser.open("https://strava.com/athletes/26652292"))
        # Show hexagon immediately; replace with real badge once downloaded
        self._badge_canvas.create_text(24, 24, text="⬡", fill=C["accent"],
                                        font=("Segoe UI", 22), tags="placeholder")
        threading.Thread(target=self._load_badge, daemon=True).start()

        tk.Label(title_row, text="GPX Fix Tool", fg=C["text"], bg=C["bg"],
                 font=FONT_TITLE).pack(side="left")
        tk.Label(title_row, text="GPS jamming recovery for runners",
                 fg=C["text_dim"], bg=C["bg"], font=FONT_SMALL).pack(
            side="left", padx=(14, 0), pady=(6, 0))

    def _load_badge(self):
        """Download Strava badge sprite in background; clip to 48×48 first frame."""
        try:
            import base64
            url = "https://badges.strava.com/echelon-sprite-48.png"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                raw = r.read()
            img = tk.PhotoImage(data=base64.b64encode(raw).decode())
            # Use a canvas to display only the top 48 px of the sprite sheet
            def _apply():
                self._badge_img = img          # keep reference alive
                self._badge_canvas.delete("placeholder")
                self._badge_canvas.create_image(0, 0, anchor="nw", image=img)
            self.after(0, _apply)
        except Exception:
            pass  # hexagon placeholder stays

        # ── Tab bar ───────────────────────────────────────────────────────
        tab_bar = tk.Frame(self, bg=C["panel"])
        tab_bar.pack(fill="x", padx=0, pady=(14, 0))

        self._tab_btns = {}
        for key, label in [("files", "📁   Local Files"), ("strava", "⚡   Strava")]:
            btn = tk.Button(tab_bar, text=label,
                            command=lambda k=key: self._show_tab(k),
                            bg=C["panel"], fg=C["text_dim"],
                            relief="flat", font=FONT_BODY,
                            cursor="hand2", padx=20, pady=10,
                            activebackground=C["card"], activeforeground=C["text"],
                            borderwidth=0)
            btn.pack(side="left")
            self._tab_btns[key] = btn

        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # ── Tab content ───────────────────────────────────────────────────
        self._content = tk.Frame(self, bg=C["bg"])
        self._content.pack(fill="both", expand=True)

        self._tabs = {
            "files":  LocalFilesTab(self._content),
            "strava": StravaTab(self._content),
        }
        self._active_tab = None
        self._show_tab("files")

    def _show_tab(self, key):
        if self._active_tab == key:
            return
        for k, frame in self._tabs.items():
            if k == key:
                frame.pack(fill="both", expand=True)
                self._tab_btns[k].config(bg=C["card"], fg=C["text"])
            else:
                frame.pack_forget()
                self._tab_btns[k].config(bg=C["panel"], fg=C["text_dim"])
        self._active_tab = key


if __name__ == "__main__":
    app = GPXFixApp()
    app.mainloop()
