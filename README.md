@ -0,0 +1,83 @@
# GPX Fix Tool

Fix GPS-jammed runs using a reference path — works with local GPX files or directly from your Strava account.

Built for runners in areas affected by GPS jamming (e.g. Israel), where the watch records valid time, heart rate and cadence but GPS coordinates are unusable.

---

## How it works

You provide two activities that follow the **same route**:

| | Reference run | Jammed run |
|---|---|---|
| GPS | ✅ Good | ❌ Jammed |
| Time / HR / Cadence | — | ✅ Valid |

The tool merges them: GPS coordinates from the reference, timestamps + biometrics from the jammed run, producing a corrected GPX that Strava can process normally (pace per km, segment matching, etc.).

---

## Features

- **Local Files tab** — pick two GPX files, save a merged `_fixed.gpx`
- **Strava tab** — connect your Strava account, pick two activities, fix & save GPX locally, then upload it back
- Handles auto-pause gaps correctly (idle time doesn't advance GPS)
- Strava badge in header links to the developer's profile
- Standalone `.exe` — no Python installation required

---

## Usage

### Local files
1. Open the **Local Files** tab
2. Load your reference GPX (good GPS, same path)
3. Load your jammed run GPX (has time & BPM)
4. Click **⚡ Merge GPS + Run Data**
5. Fixed file saved as `<original name>_fixed.gpx`

### Strava
1. Open the **Strava** tab
2. Enter your Strava API credentials and click **Connect to Strava**
3. Select the **Reference Run** (good GPS) and the **Activity to Fix**
4. Click **Fix & Save GPX** — fixed file saved to `~/Downloads/`
5. The original activity opens in your browser — delete it there (⋯ → Delete)
6. Click **Upload to Strava** to upload the fixed version

---

## Getting Strava API credentials

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api)
2. Create an app (any name)
3. Set **Authorization Callback Domain** to: `localhost`
4. Copy the **Client ID** and **Client Secret** into the app

> **Sharing with friends?** Fill in `EMBEDDED_CLIENT_ID` and `EMBEDDED_CLIENT_SECRET` at the top of `gpx_fix_tool.py`, rebuild — friends only see "Connect to Strava", no credentials form.

---

## Building the executable

Requires Python 3.10+ and PyInstaller:

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name "GPX Fix Tool" gpx_fix_tool.py
```

The standalone `.exe` is created in `dist/`.

---

## Requirements

- Python 3.10+
- tkinter (included with standard Python on Windows)
- No third-party packages needed — stdlib only

---

*Built with ❤️ for the GPS-jammed running community*
