import os
import secrets
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import calculations as calc
import db
import nutrition
import strava_import

load_dotenv()

app = Flask(__name__)

# Render (and most PaaS hosts) terminate TLS at a reverse proxy and forward
# requests over plain HTTP internally, adding X-Forwarded-* headers. Without
# this, url_for(..., _external=True) — used to build the Strava OAuth
# redirect_uri — would generate an http:// URL even in production, and
# request.remote_addr would show the proxy's IP instead of the client's.
# Harmless no-op when running locally with `python app.py` (no proxy in
# front, headers just aren't present).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Initialized unconditionally (not just under `if __name__ == "__main__"`)
# so it also runs under a WSGI server like gunicorn on Render, which imports
# this module without ever executing the __main__ block.
db.init_db()


@app.before_request
def _require_basic_auth():
    """Optional HTTP Basic Auth gate, off by default.

    Groundwork was designed as a home-WiFi-only, single-user app with no
    login — fine when it's unreachable from outside your house. Hosted
    publicly (e.g. on Render), that same app would be wide open to anyone
    with the URL, exposing personal health data and live Strava tokens. Set
    APP_PASSWORD (and optionally APP_USERNAME) in the environment to require
    a login; leave it unset for local use and nothing changes.
    """
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    username = os.environ.get("APP_USERNAME", "groundwork")
    auth = request.authorization
    valid = bool(auth) and secrets.compare_digest(
        auth.username or "", username
    ) and secrets.compare_digest(auth.password or "", password)
    if not valid:
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Groundwork"'},
        )
    return None


DEFAULT_MEAL_TYPES = ["breakfast", "lunch", "snack", "dinner"]
MEAL_TYPE_LABELS = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "snack": "Snack",
    "dinner": "Dinner",
}

MACRO_META = [
    ("protein", "Protein", "protein_g"),
    ("carbs", "Carbs", "carbs_g"),
    ("fat", "Fat", "fat_g"),
    ("fiber", "Fiber", "fiber_g"),
]

MACRO_COLOR = {"good": "var(--forest)", "warn": "var(--amber)", "over": "var(--rose)"}
MACRO_STATUS_CLASS = {"good": "status-good", "warn": "status-warn", "over": "status-over"}

# Minimal line-icon set (subset of the design reference's icon style) keyed
# by canonical activity type, with a generic fallback for anything else.
ACTIVITY_ICONS = {
    "Run": '<path d="M13 4a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM6 20l3-6 2 2 3-7"/><path d="M4 9l3-2 3 2 4-4 4 2"/>',
    "Walk": '<circle cx="12" cy="12" r="9"/><path d="M9 8l3 4-2 4M13 8l2 3"/>',
    "Hike": '<path d="M4 20l6-8 4 4 6-10"/>',
    "Tennis": '<circle cx="12" cy="12" r="8"/><path d="M12 4v16M4 12h16"/>',
    "Badminton": '<circle cx="12" cy="12" r="8"/><path d="M12 4v16M4 12h16"/>',
    "Pickleball": '<circle cx="12" cy="12" r="8"/><path d="M12 4v16M4 12h16"/>',
    "WeightTraining": '<circle cx="12" cy="12" r="9"/><path d="M8 12h8M12 8v8"/>',
    "Cycle": '<circle cx="6" cy="17" r="3"/><circle cx="18" cy="17" r="3"/><path d="M6 17l5-9h4l-3 5h4l-6 4"/>',
    "Swim": '<path d="M3 17c1.5 1.5 3 1.5 4.5 0s3-1.5 4.5 0 3 1.5 4.5 0 3-1.5 4.5 0"/><circle cx="7" cy="7" r="1.5"/>',
    "Yoga": '<circle cx="12" cy="5" r="2"/><path d="M12 7v6l-5 6M12 13l5 6M7 11h10"/>',
    "HIIT": '<path d="M13 3L4 14h6l-1 7 9-11h-6z"/>',
    "Elliptical": '<circle cx="12" cy="12" r="9"/><path d="M8 16l3-8 3 4 2-3"/>',
    "Other": '<circle cx="12" cy="12" r="9"/>',
}


def today() -> date:
    return date.today()


_STRAVA_ENDPOINTS = {
    "strava_status_page",
    "strava_connect",
    "strava_callback",
    "strava_upload",
    "strava_preview",
    "strava_confirm",
}


@app.before_request
def ensure_onboarded():
    # Strava connect/upload/preview/confirm are reachable before onboarding
    # is complete — they're now embedded directly on the onboarding page
    # (see the "Connect your activity data" section), so a brand-new user
    # can link Strava as their very first action instead of being forced
    # through the profile form first.
    if request.endpoint in ("onboarding", "static") or request.endpoint in _STRAVA_ENDPOINTS:
        return
    conn = db.get_db()
    profile = db.get_profile(conn)
    conn.close()
    if not profile or not profile["onboarded"]:
        return redirect(url_for("onboarding"))


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    conn = db.get_db()
    profile = db.get_profile(conn)

    if request.method == "POST":
        weight_kg = float(request.form["weight_kg"])
        height_cm = float(request.form["height_cm"])
        age = int(request.form["age"])
        sex = request.form["sex"]
        goal = request.form["goal"]
        protein_factor = 2.0 if goal == "bulk" else 1.6
        conn.execute(
            """UPDATE profile SET weight_kg=?, height_cm=?, age=?, sex=?, goal=?,
                   protein_factor_g_per_kg=?, onboarded=1 WHERE id=1""",
            (weight_kg, height_cm, age, sex, goal, protein_factor),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    defaults = {
        "weight_kg": profile["weight_kg"] or float(os.environ.get("USER_WEIGHT_KG", 65)),
        "height_cm": profile["height_cm"] or float(os.environ.get("USER_HEIGHT_CM", 165)),
        "age": profile["age"] or int(os.environ.get("USER_AGE", 30)),
        "sex": profile["sex"] or os.environ.get("USER_SEX", "female"),
        "goal": profile["goal"] or "maintain",
    }
    strava_status = strava_import.status(conn)
    conn.close()
    return render_template(
        "onboarding.html",
        defaults=defaults,
        is_edit=bool(profile and profile["onboarded"]),
        strava_status=strava_status,
    )


def _food_tile_preview(macro_rows):
    for row in macro_rows:
        if row["status"] != "good":
            return f"{row['label']} needs attention"
    return "On track"


@app.route("/")
def dashboard():
    conn = db.get_db()
    profile = db.get_profile(conn)
    strava_import.auto_sync_if_stale(conn)
    d = today()
    week_start = d - timedelta(days=6)
    # 60 days back covers everything the dashboard needs in one query: the
    # 14-day window used by load trend/breakdown/training_recommendation,
    # and the ~59-day lookback the aerobic-efficiency trend needs to compare
    # this month's pace-for-HR against last month's.
    sixty_days_ago = (d - timedelta(days=59)).isoformat()

    activities_60 = conn.execute(
        "SELECT * FROM activities WHERE start_date >= ? ORDER BY start_date",
        (sixty_days_ago,),
    ).fetchall()
    activities_7 = [a for a in activities_60 if a["start_date"][:10] >= week_start.isoformat()]
    todays_activities = [a for a in activities_60 if a["start_date"][:10] == d.isoformat()]
    todays_activity_cal = sum((a["calories"] or 0) for a in todays_activities)

    todays_activity_rows = [
        {
            "name": a["name"] or a["type"],
            "type": a["type"],
            "icon": ACTIVITY_ICONS.get(a["type"], ACTIVITY_ICONS["Other"]),
            "calories": round(a["calories"] or 0),
            "duration_min": round((a["moving_time_s"] or 0) / 60),
            "distance_km": round((a["distance_m"] or 0) / 1000, 2) if a["distance_m"] else None,
        }
        for a in todays_activities
    ]

    target = calc.daily_target(profile, todays_activity_cal)

    meals_7 = conn.execute(
        "SELECT * FROM meals WHERE date >= ? ORDER BY id", (week_start.isoformat(),)
    ).fetchall()
    todays_meals = [m for m in meals_7 if m["date"] == d.isoformat()]

    meal_type_order = list(DEFAULT_MEAL_TYPES)
    for m in todays_meals:
        if m["meal_type"] not in meal_type_order:
            meal_type_order.append(m["meal_type"])

    meal_sections = []
    for meal_type in meal_type_order:
        raw_items = [m for m in todays_meals if m["meal_type"] == meal_type]
        items = []
        for i in raw_items:
            qty = i["quantity"]
            qty_display = None
            if qty and qty != 1:
                qty_display = int(qty) if qty == int(qty) else qty
            display_name = i["matched_food_name"] or i["raw_text"] or (
                "Manual entry" if i["source"] == "manual" else "Unnamed item"
            )
            items.append(
                {
                    "id": i["id"],
                    "display_name": display_name,
                    "calories": i["calories"],
                    "protein_g": i["protein_g"],
                    "carbs_g": i["carbs_g"],
                    "fat_g": i["fat_g"],
                    "fiber_g": i["fiber_g"],
                    "quantity": qty,
                    "qty_display": qty_display,
                }
            )
        meal_sections.append(
            {
                "key": meal_type,
                "label": MEAL_TYPE_LABELS.get(meal_type, meal_type.title() if meal_type else "Meal"),
                "entries": items,
                "total_calories": round(sum((i["calories"] or 0) for i in raw_items)),
            }
        )

    logged_totals = {
        "calories": sum((m["calories"] or 0) for m in todays_meals),
        "protein_g": sum((m["protein_g"] or 0) for m in todays_meals),
        "carbs_g": sum((m["carbs_g"] or 0) for m in todays_meals),
        "fat_g": sum((m["fat_g"] or 0) for m in todays_meals),
        "fiber_g": sum((m["fiber_g"] or 0) for m in todays_meals),
    }

    macro_rows = []
    for key, label, field in MACRO_META:
        logged = logged_totals[field]
        target_val = target[f"target_{field}"]
        status_info = calc.macro_status(logged, target_val)
        pct = 0 if target_val <= 0 else min(100, round((logged / target_val) * 100))
        macro_rows.append(
            {
                "key": key,
                "label": label,
                "logged": round(logged, 1),
                "target": round(target_val, 1),
                "status": status_info["status"],
                "status_label": status_info["label"],
                "status_class": MACRO_STATUS_CLASS[status_info["status"]],
                "fill_color": MACRO_COLOR[status_info["status"]],
                "pct": pct,
            }
        )

    water_today = conn.execute(
        "SELECT COALESCE(SUM(amount_ml), 0) AS total FROM water_logs WHERE date = ?", (d.isoformat(),)
    ).fetchone()["total"]
    water = calc.water_progress(water_today, target["target_water_ml"])

    today_summary = conn.execute(
        "SELECT * FROM day_summaries WHERE date = ?", (d.isoformat(),)
    ).fetchone()

    summary = calc.weekly_summary(activities_7, d)
    load_trend = calc.seven_day_load_trend(activities_60, d)
    max_load = max((row["load"] for row in load_trend), default=0) or 1
    for row in load_trend:
        row["bar_height"] = round(4 + (row["load"] / max_load) * 52)

    recommendation = calc.training_recommendation(profile, activities_60, meals_7, d)
    train_box_class = "ease" if recommendation["status"] == "ease" else "good"

    breakdown = calc.activity_weekly_breakdown(activities_60, d, recommendation["status"])
    for row in breakdown:
        row["icon"] = ACTIVITY_ICONS.get(row["type"], ACTIVITY_ICONS["Other"])

    training_insights = calc.training_insights(activities_60, d)

    # Personal pattern call-outs need the user's full check-in history (not
    # just the last 60 days), but only worth querying once there's actually
    # enough of it to say something (see MIN_CHECKIN_DAYS_FOR_PATTERNS).
    day_summary_count = conn.execute("SELECT COUNT(*) AS n FROM day_summaries").fetchone()["n"]
    if day_summary_count >= calc.MIN_CHECKIN_DAYS_FOR_PATTERNS:
        all_summaries = conn.execute("SELECT * FROM day_summaries ORDER BY date").fetchall()
        earliest = conn.execute("SELECT MIN(date) AS d FROM day_summaries").fetchone()["d"]
        pattern_activities = conn.execute(
            "SELECT * FROM activities WHERE start_date >= ? ORDER BY start_date", (earliest,)
        ).fetchall()
        training_insights = training_insights + calc.personal_pattern_insights(
            all_summaries, pattern_activities
        )

    nutrition_insights = calc.nutrition_insights(
        meals_7, activities_60, todays_activities, todays_meals, profile, target, d,
        water_logged_ml=water_today,
    )

    strava_status = strava_import.status(conn)
    conn.close()

    remaining = round(target["target_calories"] - logged_totals["calories"])
    calorie_goal_pct = (
        0 if target["target_calories"] <= 0
        else min(100, round((logged_totals["calories"] / target["target_calories"]) * 100))
    )

    return render_template(
        "dashboard.html",
        profile=profile,
        today=d,
        target=target,
        logged_totals={k: round(v) for k, v in logged_totals.items()},
        remaining=remaining,
        calorie_goal_pct=calorie_goal_pct,
        macro_rows=macro_rows,
        food_preview=_food_tile_preview(macro_rows),
        water=water,
        summary=summary,
        load_trend=load_trend,
        recommendation=recommendation,
        train_box_class=train_box_class,
        breakdown=breakdown,
        training_insights=training_insights[:4],
        nutrition_insights=nutrition_insights[:4],
        meal_sections=meal_sections,
        strava_status=strava_status,
        today_summary=today_summary,
        todays_activity_rows=todays_activity_rows,
        todays_activity_cal=round(todays_activity_cal),
    )


def _todays_totals(conn, d):
    """Shared by the dashboard and /log/submit: today's activity calories,
    calorie/macro/water targets, logged totals, and water progress."""
    profile = db.get_profile(conn)

    activities = conn.execute(
        "SELECT * FROM activities WHERE start_date >= ?", (d.isoformat(),)
    ).fetchall()
    todays_activities = [a for a in activities if a["start_date"][:10] == d.isoformat()]
    activity_cal = sum((a["calories"] or 0) for a in todays_activities)

    target = calc.daily_target(profile, activity_cal)

    todays_meals = conn.execute("SELECT * FROM meals WHERE date = ?", (d.isoformat(),)).fetchall()
    logged_totals = {
        "calories": sum((m["calories"] or 0) for m in todays_meals),
        "protein_g": sum((m["protein_g"] or 0) for m in todays_meals),
        "carbs_g": sum((m["carbs_g"] or 0) for m in todays_meals),
        "fat_g": sum((m["fat_g"] or 0) for m in todays_meals),
        "fiber_g": sum((m["fiber_g"] or 0) for m in todays_meals),
    }

    water_ml = conn.execute(
        "SELECT COALESCE(SUM(amount_ml), 0) AS total FROM water_logs WHERE date = ?", (d.isoformat(),)
    ).fetchone()["total"]
    water = calc.water_progress(water_ml, target["target_water_ml"])

    return {
        "activity_cal": activity_cal,
        "target": target,
        "logged_totals": logged_totals,
        "water_ml": water_ml,
        "water": water,
    }


@app.route("/log/submit", methods=["POST"])
def submit_day():
    conn = db.get_db()
    d = today()
    snap = _todays_totals(conn, d)
    rec = calc.daily_nutrition_recommendation(snap["logged_totals"], snap["target"], snap["water"])

    conn.execute(
        """INSERT INTO day_summaries (date, calories_logged, protein_g, carbs_g, fat_g, fiber_g,
               water_ml, target_calories, target_protein_g, target_water_ml, activity_calories,
               status, title, message, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               calories_logged = excluded.calories_logged,
               protein_g = excluded.protein_g,
               carbs_g = excluded.carbs_g,
               fat_g = excluded.fat_g,
               fiber_g = excluded.fiber_g,
               water_ml = excluded.water_ml,
               target_calories = excluded.target_calories,
               target_protein_g = excluded.target_protein_g,
               target_water_ml = excluded.target_water_ml,
               activity_calories = excluded.activity_calories,
               status = excluded.status,
               title = excluded.title,
               message = excluded.message,
               submitted_at = excluded.submitted_at""",
        (
            d.isoformat(),
            snap["logged_totals"]["calories"],
            snap["logged_totals"]["protein_g"],
            snap["logged_totals"]["carbs_g"],
            snap["logged_totals"]["fat_g"],
            snap["logged_totals"]["fiber_g"],
            snap["water_ml"],
            snap["target"]["target_calories"],
            snap["target"]["target_protein_g"],
            snap["target"]["target_water_ml"],
            snap["activity_cal"],
            rec["status"],
            rec["title"],
            rec["message"],
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/history")
def history():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM day_summaries ORDER BY date DESC").fetchall()
    conn.close()
    return render_template("history.html", rows=rows)


@app.route("/log/meal/smart", methods=["POST"])
def log_meal_smart():
    meal_type = (request.form.get("meal_type") or "meal").strip()
    text = (request.form.get("text") or "").strip()
    if text:
        conn = db.get_db()
        items = nutrition.parse_smart_entry(text)
        d = today().isoformat()
        logged_at = datetime.now().isoformat()
        for item in items:
            conn.execute(
                """INSERT INTO meals (date, meal_type, raw_text, matched_food_name,
                       quantity, calories, protein_g, carbs_g, fat_g, fiber_g, source, logged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?)""",
                (
                    d,
                    meal_type,
                    item["raw"],
                    item["food_name"],
                    item["quantity"],
                    item["calories"],
                    item["protein_g"],
                    item["carbs_g"],
                    item["fat_g"],
                    item["fiber_g"],
                    logged_at,
                ),
            )
        conn.commit()
        conn.close()
    return redirect(url_for("dashboard"))


@app.route("/log/meal/manual", methods=["POST"])
def log_meal_manual():
    meal_type = (request.form.get("meal_type") or "meal").strip()
    try:
        row_count = int(request.form.get("row_count", 1))
    except ValueError:
        row_count = 1

    d = today().isoformat()
    logged_at = datetime.now().isoformat()
    conn = db.get_db()
    any_saved = False

    def num(field, index):
        raw = (request.form.get(f"{field}_{index}") or "").strip()
        return float(raw) if raw else None

    for index in range(row_count):
        label = (request.form.get(f"label_{index}") or "").strip() or None
        qty = num("qty", index)
        cal = num("cal", index)
        protein = num("protein", index)
        carbs = num("carbs", index)
        fat = num("fat", index)
        if label is None and all(v is None for v in (qty, cal, protein, carbs, fat)):
            continue
        conn.execute(
            """INSERT INTO meals (date, meal_type, raw_text, matched_food_name,
                   quantity, calories, protein_g, carbs_g, fat_g, fiber_g, source, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'manual', ?)""",
            (d, meal_type, label, label, qty, cal, protein, carbs, fat, logged_at),
        )
        any_saved = True

    if any_saved:
        conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/log/meal/delete/<int:meal_id>", methods=["POST"])
def log_meal_delete(meal_id):
    conn = db.get_db()
    conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/log/meal/update/<int:meal_id>", methods=["POST"])
def log_meal_update(meal_id):
    def num(field):
        raw = (request.form.get(field) or "").strip()
        return float(raw) if raw else None

    quantity = num("quantity")
    calories = num("calories")
    protein_g = num("protein_g")
    carbs_g = num("carbs_g")
    fat_g = num("fat_g")
    fiber_g = num("fiber_g")

    conn = db.get_db()
    conn.execute(
        """UPDATE meals SET quantity = ?, calories = ?, protein_g = ?, carbs_g = ?,
               fat_g = ?, fiber_g = ? WHERE id = ?""",
        (quantity, calories, protein_g, carbs_g, fat_g, fiber_g, meal_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/log/water", methods=["POST"])
def log_water():
    amount = float(request.form.get("amount_ml", 0))
    d = today().isoformat()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO water_logs (date, amount_ml, logged_at) VALUES (?, ?, ?)",
        (d, amount, datetime.now().isoformat()),
    )
    conn.commit()
    total = conn.execute(
        "SELECT COALESCE(SUM(amount_ml), 0) AS total FROM water_logs WHERE date = ?", (d,)
    ).fetchone()["total"]
    conn.close()
    return jsonify({"total_ml": total})


@app.route("/strava/status")
def strava_status_page():
    conn = db.get_db()
    status_data = strava_import.status(conn)
    conn.close()
    return render_template("strava_status.html", status=status_data)


@app.route("/strava/connect")
def strava_connect():
    redirect_uri = url_for("strava_callback", _external=True)
    return redirect(strava_import.authorize_url(redirect_uri))


@app.route("/strava/callback")
def strava_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return redirect(url_for("strava_status_page"))
    conn = db.get_db()
    strava_import.exchange_code(conn, code)
    conn.close()
    return redirect(url_for("strava_preview", source="api"))


@app.route("/strava/upload", methods=["POST"])
def strava_upload():
    file = request.files.get("export_file")
    if file and file.filename:
        strava_import.save_upload(file)
        return redirect(url_for("strava_preview", source="csv"))
    return redirect(url_for("strava_status_page"))


@app.route("/strava/preview")
def strava_preview():
    source = request.args.get("source", "csv")
    conn = db.get_db()
    if source == "api":
        preview_data = strava_import.preview_api(conn)
    else:
        preview_data = strava_import.preview_csv(conn)
    conn.close()
    for row in preview_data.get("rows", []):
        row["duration_min"] = round(row["moving_time_s"] / 60)
        row["distance_km"] = round((row["distance_m"] or 0) / 1000, 2)
        row["calories_display"] = round(row["calories"] or 0)
    return render_template("strava_preview.html", preview=preview_data, source=source)


@app.route("/strava/confirm", methods=["POST"])
def strava_confirm():
    source = request.form.get("source", "csv")
    conn = db.get_db()
    if source == "api":
        strava_import.confirm_api(conn)
    else:
        strava_import.confirm_csv(conn)
    conn.close()
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    # PORT is honored so this also works unmodified if something ever runs
    # `python app.py` directly on a host that assigns a port via env var;
    # Render itself uses the Procfile's gunicorn command instead, which
    # binds to $PORT on its own.
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
