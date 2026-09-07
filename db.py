import os
import sqlite3
from pathlib import Path

# Overridable via DATABASE_PATH so the DB can live on a persistent disk when
# hosted somewhere with an ephemeral filesystem (e.g. Render without a
# mounted volume would lose this file on every deploy/restart otherwise).
# Defaults to the same in-repo path used for local runs.
DB_PATH = Path(os.environ.get("DATABASE_PATH") or (Path(__file__).parent / "groundwork.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    weight_kg REAL,
    height_cm REAL,
    age INTEGER,
    sex TEXT,
    goal TEXT DEFAULT 'maintain',
    bulk_surplus_cal REAL DEFAULT 400,
    protein_factor_g_per_kg REAL DEFAULT 1.6,
    carbs_ratio REAL DEFAULT 0.6,
    fat_ratio REAL DEFAULT 0.4,
    fiber_target_g REAL DEFAULT 30,
    water_ml_per_kg REAL DEFAULT 35,
    onboarded INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT UNIQUE,
    name TEXT,
    type TEXT,
    start_date TEXT,
    distance_m REAL,
    moving_time_s REAL,
    calories REAL,
    avg_hr REAL,
    elevation_gain_m REAL
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    meal_type TEXT,
    raw_text TEXT,
    matched_food_name TEXT,
    quantity REAL,
    calories REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    fiber_g REAL,
    source TEXT DEFAULT 'auto',
    logged_at TEXT
);

CREATE TABLE IF NOT EXISTS water_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    amount_ml REAL,
    logged_at TEXT
);

CREATE TABLE IF NOT EXISTS strava_tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    athlete_id INTEGER,
    access_token TEXT,
    refresh_token TEXT,
    expires_at INTEGER,
    last_auto_sync_at INTEGER
);

-- One row per day, written when the user clicks "Submit today's log" on the
-- dashboard. Snapshots that day's logged totals + targets + the generated
-- recommendation, so the ledger has permanent history even as targets
-- change later (e.g. after editing the profile).
CREATE TABLE IF NOT EXISTS day_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT UNIQUE,
    calories_logged REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    fiber_g REAL,
    water_ml REAL,
    target_calories REAL,
    target_protein_g REAL,
    target_water_ml REAL,
    activity_calories REAL,
    status TEXT,
    title TEXT,
    message TEXT,
    submitted_at TEXT
);
"""


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_names(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)

    # Lightweight migration for columns added after a table already existed
    # on disk — CREATE TABLE IF NOT EXISTS above is a no-op for existing
    # installs, so new columns need to be added explicitly.
    if "last_auto_sync_at" not in _column_names(conn, "strava_tokens"):
        conn.execute("ALTER TABLE strava_tokens ADD COLUMN last_auto_sync_at INTEGER")

    # Needed to cross-reference meal-log timestamps against Strava session
    # end-times (e.g. "was protein logged within ~2h of today's workout?").
    if "logged_at" not in _column_names(conn, "meals"):
        conn.execute("ALTER TABLE meals ADD COLUMN logged_at TEXT")

    row = conn.execute("SELECT id FROM profile WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO profile (id, onboarded) VALUES (1, 0)"
        )
    conn.commit()
    conn.close()


def get_profile(conn):
    return conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
