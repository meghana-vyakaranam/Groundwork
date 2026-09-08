# Groundwork

An app that answers **"Given my daily workouts and fitness goals, how do I maintain an optimised personal health?"**

Set a goal once (Maintain / Bulk / Performance), sync activity data from
Strava (connect your account or upload your data export), log meals in
plain language (including Indian dishes), track water, and see at a glance
whether today's intake is on target — plus a data-backed read on whether
today is a day to train hard or ease off.

## Install

```bash
cd groundwork
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The `.env` values are only used as a BMR fallback *before* you complete
onboarding in the app. Once you finish the onboarding form, the profile
stored in SQLite takes over and `.env` is ignored.

## Run

```bash
python app.py
```

- On your Mac: open `http://localhost:5050`
- From your phone or another device on the same WiFi: find your Mac's local
  IP with:

  ```bash
  ipconfig getifaddr en0
  ```

  then open `http://<that-ip>:5050` on the other device. The first time you
  connect from another device, macOS may prompt you to allow incoming
  connections for Python — allow it.


## Strava sync setup

Activity data comes from Strava, via either of two independent options —
use one or both, since they're complementary (they dedupe against each
other by Strava's own activity ID, so backfilling history with a CSV and
later connecting the live API never creates duplicates):

### Option A — Upload your Strava data export

No API app or credentials needed:

1. In Strava: **Settings → My Account → Download or Delete Your Account →
   Request your Archive**. Strava emails you a zip file (can take a while).
2. Unzip it and find `activities.csv` inside.
3. On the dashboard, click **Sync now → Upload & preview** and select that
   file.

This is the best way to backfill your full history in one go.

### Option B — Connect your Strava account (live API)

1. Create an API application at
   [strava.com/settings/api](https://www.strava.com/settings/api). Set
   **Authorization Callback Domain** to `localhost`.
2. Copy the Client ID and Client Secret into `.env` as `STRAVA_CLIENT_ID`
   and `STRAVA_CLIENT_SECRET`.
3. On the dashboard, click **Sync now → Connect Strava account** and
   authorize access.
4. After that, Groundwork automatically syncs your last 30 days of
   activities in the background — at most once every 24h, whenever you
   have the dashboard open. Since this is a locally-run app rather than an
   always-on server, "daily" means "the first dashboard load after 24h has
   passed" rather than a fixed clock time. You can still click **Sync
   now** any time for an on-demand refresh; it's just no longer required
   for day-to-day use.

Note: Strava lumps Tennis, Badminton, and Pickleball together under a
generic "Workout" type in both the CSV export and the API — Groundwork
disambiguates them by matching keywords in the activity's name (e.g. an
activity named "Evening Tennis" is recognized as Tennis).

### Preview before import

## Nutrition matching

Meal logging first matches free text against a local, curated dataset
(`data/food_db.json`) — no API call needed for anything already in there.
Matching uses token overlap (not just substring matching), so messy input
like "pb sandwich" still resolves to "peanut butter sandwich". The dataset
covers common Western staples and a broad set of Indian dishes.

### AI fallback for dishes not in the dataset (optional)

If a food doesn't match anything locally — any Indian dish not yet in the
curated list, typos and all (e.g. "aloo tiki=ki sandwich") — and
`ANTHROPIC_API_KEY` is set in `.env`, Groundwork asks Claude
(`ai_nutrition.py`, model `claude-opus-4-7`) for a one-off macro estimate
for that dish, then **saves the result into `data/food_db.json`**. The
next time you (or anyone) logs that same dish, or something worded
similarly, it matches locally — no API call, no repeat cost. This is how
the dataset "learns" new Indian dishes over time instead of needing them
hand-added.

Get a key at [console.anthropic.com](https://console.anthropic.com), add
it to `.env` as `ANTHROPIC_API_KEY`, restart the app, and it's active. No
key set — the app runs exactly as before, fully offline.

Anything that still doesn't match (no key set, or the estimate call fails)
gets flagged distinctly in the UI rather than guessed at silently — use the
**"Enter calories myself"** manual-entry toggle on any meal panel to log it
by hand (no food name required, just numbers).

You can also extend the dataset by hand by adding entries directly to
`data/food_db.json` — no code changes needed. Each entry needs `name`,
`unit`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, and an
`aliases` list. Values are reasonable per-serving approximations, not
lab-measured.

## Project structure

```
groundwork/
  app.py                 # Flask routes
  db.py                  # SQLite schema + connection helper
  nutrition.py           # food-name -> macros lookup + smart-entry parsing
  ai_nutrition.py        # optional Claude fallback for unmatched dishes
  strava_import.py       # Strava CSV parsing + OAuth API sync, preview, upsert
  calculations.py        # BMR, goal targets, weekly rollups, training load
  requirements.txt
  Procfile                # gunicorn start command (used by Render)
  render.yaml             # Render Blueprint: web service + persistent disk
  .env.example
  data/
    food_db.json
  templates/
    base.html
    onboarding.html
    dashboard.html
    strava_status.html
    strava_preview.html
  static/
    style.css
```
