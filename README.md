# Groundwork

A local, mobile-and-desktop-friendly web app that answers one question every
day: **"Given what I did today and my goal, am I eating the right amount?"**

Set a goal once (Maintain / Bulk / Performance), sync activity data from
Strava (connect your account or upload your data export), log meals in
plain language (including Indian dishes), track water, and see at a glance
whether today's intake is on target — plus a data-backed read on whether
today is a day to train hard or ease off.

No subscriptions required. Plain Python + Flask + SQLite. Runs locally by
default, reachable from your phone or laptop over home WiFi only (not from
outside the house — a deliberate simplicity/cost tradeoff) — or optionally
[deployed to Render](#deploying-to-render) if you want a stable URL reachable
from anywhere.

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

By default this app is home-WiFi-only. It is not reachable from outside
your house unless you deploy it (see below) — the local-only setup avoids
the cost and complexity of exposing a single-user app to the public
internet, but isn't required.

## Deploying to Render

The app is ready to deploy as-is — it's already set up to run under a real
WSGI server and generate correct HTTPS URLs behind Render's proxy. What
changes between local and Render is where config comes from: locally it's
`.env`; on Render it's environment variables you set in the dashboard
(`.env` itself is gitignored and never deployed).

`render.yaml` is configured for Render's **free** plan, with no persistent
disk — nothing to pay for. The trade-off: `groundwork.db`,
`data/strava_export.csv`, and `data/food_db.json` live on the service's
ephemeral filesystem, so logged meals/weights/AI-learned foods can reset
whenever the free service restarts or redeploys. If you want that data to
actually persist, upgrade the `plan` in `render.yaml` to `starter` (or
higher) and add a `disk:` block mounted at, say, `/var/data`, with
`DATABASE_PATH`/`DATA_DIR` env vars pointing into it — this is a paid
feature (disks aren't available on the free plan).

1. **Push this repo to GitHub** (or GitLab), since Render deploys from a git
   remote.
2. **In the Render dashboard**, choose "New → Blueprint" and point it at the
   repo — it will read `render.yaml` and provision the web service
   automatically. Alternatively, "New → Web Service" and set the
   build/start commands manually from `render.yaml` if you'd rather not use
   the Blueprint flow.
3. **Set the secret env vars** Render will prompt for (declared but left
   blank in `render.yaml`):
   - `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` — same values as `.env`
   - `ANTHROPIC_API_KEY` — optional, same as `.env`
   - `APP_USERNAME` / `APP_PASSWORD` — **set these.** Groundwork has no
     login of its own; on home WiFi that's fine, but on a public Render URL
     it means anyone with the link can see your food/weight logs and your
     live Strava tokens. Setting `APP_PASSWORD` puts the whole app behind
     an HTTP Basic Auth prompt.
4. **Update your Strava API app's Authorization Callback Domain** at
   [strava.com/settings/api](https://www.strava.com/settings/api) to your
   Render domain (e.g. `groundwork.onrender.com`) — Strava checks this
   against the OAuth redirect, so the old `localhost` value won't work once
   you're connecting from the deployed URL. You can list multiple domains
   there if you still want `localhost` to keep working too.
5. Deploy. `data/food_db.json` starts out as the curated list checked into
   this repo, then behaves exactly like the local app from there —
   including the 24h Strava auto-sync, which now actually runs "daily" in
   the literal sense, since a hosted app (unlike your laptop) is up all the
   time (as long as it isn't asleep — see the free-tier note below).

Notes:
- **Free-tier Render web services spin down after inactivity** and take
  ~30–60s to wake back up on the next request — fine for a personal daily
  check-in app, just don't expect an instant load on the first visit of the
  day.
- The Procfile runs a single gunicorn worker on purpose: SQLite handles one
  writer at a time, and this is a single-user app, so extra worker
  processes would add nothing but lock-contention risk.

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

Syncing is never silent, for either option:

- The **Sync now** flow always shows a preview table of everything found,
  with each row flagged as **new** or **already imported** — nothing is
  written to the database until you click **Confirm import**.
- Re-running an import over already-synced data is safe: activities are
  upserted by Strava's own permanent activity ID, so nothing is duplicated.

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

## Design reference

`design_a_sage_v2.html` (the original click-through mockup provided
alongside the PRD) is the visual reference for colors, spacing, and
component patterns — useful if you're iterating on `templates/` or
`static/style.css` later. Two things were added beyond that mockup, since
it didn't cover them: the Strava sync banner/preview table, and a
"Save" button on manual meal entry (the mockup's manual-entry form was a
non-interactive demo).

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
