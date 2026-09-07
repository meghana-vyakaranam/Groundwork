"""Strava activity import: either a downloaded bulk-export CSV, or a live
connection to the Strava API (OAuth). Both paths converge on the same
`activities` table and the same source_id scheme (`strava_<activity id>`),
which is Strava's own permanent, globally-unique activity ID — so uploading
a historical CSV once and later connecting the live API never creates
duplicates; they just fill in each other's gaps.

Nothing is ever written to the database without going through preview()
first, mirroring the "sync is never silent" behavior the app has always had.
"""
from __future__ import annotations

import csv
import os
import time
from datetime import datetime
from pathlib import Path

import requests

# Overridable via DATA_DIR (shared with nutrition.py's food_db.json) so
# uploaded exports land on a persistent disk when hosted somewhere with an
# ephemeral filesystem. Defaults to the in-repo data/ folder for local runs.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path(__file__).parent / "data"))
UPLOAD_PATH = DATA_DIR / "strava_export.csv"

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

# Strava activity "type" strings (bulk-export CSV's Activity Type column, and
# the API's sport_type field both use variants of these) -> canonical type
# used throughout the app.
TYPE_ALIASES = {
    "run": "Run",
    "trail run": "Run",
    "virtual run": "Run",
    "walk": "Walk",
    "hike": "Hike",
    "weight training": "WeightTraining",
    "weighttraining": "WeightTraining",
    "ride": "Cycle",
    "virtual ride": "Cycle",
    "gravel ride": "Cycle",
    "mountain bike ride": "Cycle",
    "e-bike ride": "Cycle",
    "ebikeride": "Cycle",
    "handcycle": "Cycle",
    "velomobile": "Cycle",
    "swim": "Swim",
    "yoga": "Yoga",
    "pilates": "Yoga",
    "hiit": "HIIT",
    "high intensity interval training": "HIIT",
    "highintensityintervaltraining": "HIIT",
    "crossfit": "HIIT",
    "elliptical": "Elliptical",
    "tennis": "Tennis",
    "badminton": "Badminton",
    "pickleball": "Pickleball",
}

# Strava lumps Tennis/Badminton/Pickleball under a generic "Workout" type in
# both the CSV export and the API — the activity name is the only signal.
WORKOUT_NAME_KEYWORDS = {
    "tennis": "Tennis",
    "badminton": "Badminton",
    "pickleball": "Pickleball",
}


def normalize_type(raw_type: str, name: str = "") -> str:
    key = (raw_type or "").strip().lower()
    if key in ("workout", ""):
        name_lower = (name or "").lower()
        for kw, canonical in WORKOUT_NAME_KEYWORDS.items():
            if kw in name_lower:
                return canonical
        return "Other"
    return TYPE_ALIASES.get(key, raw_type.strip() or "Other")


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Bulk-export CSV upload
# ---------------------------------------------------------------------------

def save_upload(file_storage) -> None:
    UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_storage.save(UPLOAD_PATH)


def _parse_csv_date(raw: str) -> str:
    # Strava's export format: "Aug 9, 2026, 4:49:37 PM"
    dt = datetime.strptime(raw.strip(), "%b %d, %Y, %I:%M:%S %p")
    return dt.isoformat()


def parse_csv(path: Path) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            activity_id = (row.get("Activity ID") or "").strip()
            if not activity_id:
                continue
            name = (row.get("Activity Name") or "").strip()
            distance_m = _to_float(row.get("Distance"))  # last "Distance" column = meters
            moving_time_s = _to_float(row.get("Moving Time"))
            try:
                start_date = _parse_csv_date(row.get("Activity Date", ""))
            except ValueError:
                continue
            rows.append(
                {
                    "source_id": f"strava_{activity_id}",
                    "name": name or "Workout",
                    "type": normalize_type(row.get("Activity Type", ""), name),
                    "start_date": start_date,
                    "distance_m": distance_m,
                    "moving_time_s": moving_time_s,
                    "calories": _to_float(row.get("Calories"), default=None),
                    "avg_hr": _to_float(row.get("Average Heart Rate"), default=None),
                    "elevation_gain_m": _to_float(row.get("Elevation Gain"), default=None),
                }
            )
    return rows


def preview_csv(conn) -> dict:
    if not UPLOAD_PATH.exists():
        return {"available": False, "rows": []}
    parsed = parse_csv(UPLOAD_PATH)
    existing_ids = {
        r["source_id"] for r in conn.execute("SELECT source_id FROM activities").fetchall()
    }
    for row in parsed:
        row["is_new"] = row["source_id"] not in existing_ids
    return {"available": True, "rows": parsed}


def confirm_csv(conn) -> int:
    if not UPLOAD_PATH.exists():
        return 0
    return _upsert(conn, parse_csv(UPLOAD_PATH))


def _upsert(conn, parsed: list) -> int:
    for row in parsed:
        conn.execute(
            """
            INSERT INTO activities
                (source_id, name, type, start_date, distance_m, moving_time_s,
                 calories, avg_hr, elevation_gain_m)
            VALUES (:source_id, :name, :type, :start_date, :distance_m, :moving_time_s,
                    :calories, :avg_hr, :elevation_gain_m)
            ON CONFLICT(source_id) DO UPDATE SET
                name=excluded.name,
                type=excluded.type,
                start_date=excluded.start_date,
                distance_m=excluded.distance_m,
                moving_time_s=excluded.moving_time_s,
                calories=excluded.calories,
                avg_hr=excluded.avg_hr,
                elevation_gain_m=excluded.elevation_gain_m
            """,
            row,
        )
    conn.commit()
    return len(parsed)


# ---------------------------------------------------------------------------
# Live Strava API (OAuth)
# ---------------------------------------------------------------------------

def api_configured() -> bool:
    return bool(os.environ.get("STRAVA_CLIENT_ID")) and bool(os.environ.get("STRAVA_CLIENT_SECRET"))


def authorize_url(redirect_uri: str) -> str:
    client_id = os.environ.get("STRAVA_CLIENT_ID", "")
    params = (
        f"client_id={client_id}&redirect_uri={redirect_uri}"
        "&response_type=code&approval_prompt=auto&scope=activity:read_all"
    )
    return f"{AUTHORIZE_URL}?{params}"


def is_connected(conn) -> bool:
    return conn.execute("SELECT id FROM strava_tokens WHERE id = 1").fetchone() is not None


def exchange_code(conn, code: str) -> None:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": os.environ.get("STRAVA_CLIENT_ID"),
            "client_secret": os.environ.get("STRAVA_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _save_tokens(conn, data)


def _save_tokens(conn, data: dict) -> None:
    athlete = data.get("athlete") or {}
    conn.execute(
        """
        INSERT INTO strava_tokens (id, athlete_id, access_token, refresh_token, expires_at)
        VALUES (1, :athlete_id, :access_token, :refresh_token, :expires_at)
        ON CONFLICT(id) DO UPDATE SET
            athlete_id=excluded.athlete_id,
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at
        """,
        {
            "athlete_id": athlete.get("id"),
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": data["expires_at"],
        },
    )
    conn.commit()


def _get_valid_access_token(conn) -> str | None:
    row = conn.execute("SELECT * FROM strava_tokens WHERE id = 1").fetchone()
    if row is None:
        return None
    if row["expires_at"] > time.time() + 60:
        return row["access_token"]
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": os.environ.get("STRAVA_CLIENT_ID"),
            "client_secret": os.environ.get("STRAVA_CLIENT_SECRET"),
            "refresh_token": row["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _save_tokens(conn, data)
    return data["access_token"]


def _api_row(raw: dict) -> dict:
    name = raw.get("name") or ""
    start_local = (raw.get("start_date_local") or "").rstrip("Z")
    return {
        "source_id": f"strava_{raw['id']}",
        "name": name or "Workout",
        "type": normalize_type(raw.get("sport_type") or raw.get("type") or "", name),
        "start_date": start_local,
        "distance_m": raw.get("distance") or 0,
        "moving_time_s": raw.get("moving_time") or 0,
        "calories": raw.get("calories"),
        "avg_hr": raw.get("average_heartrate"),
        "elevation_gain_m": raw.get("total_elevation_gain"),
    }


def _fetch_activity_calories(token: str, activity_id) -> float | None:
    """Strava's list/summary endpoint (used below) never includes a
    "calories" field at all — it's only present on the detailed
    single-activity resource. Without this, every synced activity's
    calories comes back None and the app's "calories burned today" is
    always 0 even though Strava's own app shows a real number per
    activity. One extra request per activity that needs it."""
    resp = requests.get(
        f"{API_BASE}/activities/{activity_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("calories")


def _fetch_recent(conn, days: int) -> list:
    token = _get_valid_access_token(conn)
    if not token:
        return []
    after = int(time.time()) - days * 86400
    resp = requests.get(
        f"{API_BASE}/athlete/activities",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": after, "per_page": 100},
        timeout=15,
    )
    resp.raise_for_status()
    raw_activities = resp.json()

    # Only backfill calories for activities we don't already have a value
    # for, so a daily re-sync of the same 30-day window doesn't re-hit the
    # detail endpoint for activities already filled in.
    existing_calories = {
        row["source_id"]: row["calories"]
        for row in conn.execute("SELECT source_id, calories FROM activities").fetchall()
    }
    for raw in raw_activities:
        source_id = f"strava_{raw['id']}"
        if existing_calories.get(source_id) is None:
            try:
                raw["calories"] = _fetch_activity_calories(token, raw["id"])
            except Exception:
                pass  # this activity just stays without a calorie figure; sync keeps going

    return [_api_row(a) for a in raw_activities]


def preview_api(conn, days: int = 30) -> dict:
    if not is_connected(conn):
        return {"available": False, "rows": []}
    parsed = _fetch_recent(conn, days)
    existing_ids = {
        r["source_id"] for r in conn.execute("SELECT source_id FROM activities").fetchall()
    }
    for row in parsed:
        row["is_new"] = row["source_id"] not in existing_ids
    return {"available": True, "rows": parsed}


def confirm_api(conn, days: int = 30) -> int:
    if not is_connected(conn):
        return 0
    return _upsert(conn, _fetch_recent(conn, days))


AUTO_SYNC_INTERVAL_S = 24 * 60 * 60  # once a day


def auto_sync_if_stale(conn) -> int | None:
    """Silently pulls the last 30 days of activities if it's been >=24h
    since the last auto-sync (or auto-sync has never run yet).

    This is what makes sync happen "on a daily basis" without a manual
    Sync now click — called once per dashboard load. Since Groundwork is a
    locally-run app rather than an always-on server, "daily" here means
    "at most once every 24h, whenever the app happens to be open" — the
    first dashboard load after the 24h window has passed triggers it.

    Returns the number of activities synced, or None if skipped (not
    connected yet, or not stale yet) or failed (network/auth hiccup —
    fails silently so a Strava outage never breaks the dashboard).
    """
    if not is_connected(conn):
        return None
    row = conn.execute(
        "SELECT last_auto_sync_at FROM strava_tokens WHERE id = 1"
    ).fetchone()
    last = (row["last_auto_sync_at"] if row else None) or 0
    if time.time() - last < AUTO_SYNC_INTERVAL_S:
        return None
    try:
        count = confirm_api(conn, days=30)
    except Exception:
        return None
    conn.execute(
        "UPDATE strava_tokens SET last_auto_sync_at = ? WHERE id = 1", (int(time.time()),)
    )
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Combined status for the dashboard banner
# ---------------------------------------------------------------------------

def status(conn) -> dict:
    last = conn.execute(
        "SELECT MAX(start_date) AS last_date FROM activities WHERE source_id LIKE 'strava_%'"
    ).fetchone()
    return {
        "connected": is_connected(conn),
        "csv_available": UPLOAD_PATH.exists(),
        "api_configured": api_configured(),
        "last_sync": last["last_date"] if last else None,
    }
