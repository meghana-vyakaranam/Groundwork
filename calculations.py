"""BMR, goal targets, weekly rollups, and training-load heuristics.

All functions take plain dicts/rows and primitive values so they can be
unit-tested without a live database.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

import nutrition

# Canonical activity "type" -> relative training-intensity factor.
# Used only to derive a comparable daily training load
# (minutes x intensity) — not a physiological measurement.
INTENSITY_FACTORS = {
    "Run": 1.4,
    "Walk": 0.5,
    "Hike": 1.1,
    "Tennis": 1.0,
    "Badminton": 0.9,
    "Pickleball": 0.8,
    "WeightTraining": 0.9,
    "Cycle": 1.0,
    "Swim": 1.3,
    "Yoga": 0.4,
    "HIIT": 1.5,
    "Elliptical": 0.9,
    "Other": 0.8,
}

# Activity types whose most meaningful "this week" detail is pace + distance.
PACE_TYPES = {"Run", "Walk", "Hike"}
# Activity types compared week-over-week by absolute minutes, not percent.
MINUTES_DELTA_TYPES = {"Tennis", "Badminton", "Pickleball"}


def bmr(weight_kg: float, height_cm: float, age: float, sex: str) -> float:
    """Mifflin-St Jeor equation."""
    value = (10 * weight_kg) + (6.25 * height_cm) - (5 * age)
    value += 5 if sex == "male" else -161
    return value


# How far the carb/fat split shifts between a full rest day and a hard
# training day, and the activity-calorie level at which the full shift
# applies. Rest days genuinely warrant lower carbs; hard days warrant more,
# rather than applying the same fixed ratio every day regardless of load.
CARB_SHIFT_MAX = 0.10
CARB_SHIFT_ACTIVITY_CAL_CAP = 500

# Extra water target per calorie burned via today's logged activity, so a
# long/hot session raises hydration needs by more than a short easy one,
# instead of a flat weight-only number.
WATER_ML_PER_ACTIVITY_CAL = 1.0


def _carb_fat_ratio_for_today(base_carbs_ratio: float, base_fat_ratio: float, todays_activity_calories: float):
    """Shifts the carb/fat split for today's target based on today's own
    training load (using activity calories already being passed into
    daily_target as the load proxy) -- rest days lean lower-carb, hard days
    lean higher-carb, on a straight line between the two."""
    frac = max(0.0, min(1.0, (todays_activity_calories or 0) / CARB_SHIFT_ACTIVITY_CAL_CAP))
    shift = (frac - 0.5) * 2 * CARB_SHIFT_MAX  # ranges -CARB_SHIFT_MAX .. +CARB_SHIFT_MAX
    carbs_ratio = max(0.3, min(0.8, base_carbs_ratio + shift))
    fat_ratio = max(0.2, min(0.7, base_fat_ratio - shift))
    return carbs_ratio, fat_ratio, shift


def daily_target(profile, todays_activity_calories: float) -> dict:
    """Returns today's calorie/macro/fiber/water targets for the given profile.

    `profile` is a sqlite3.Row (or dict) with the columns from the
    `profile` table.
    """
    weight_kg = profile["weight_kg"]
    goal = profile["goal"]
    the_bmr = bmr(weight_kg, profile["height_cm"], profile["age"], profile["sex"])

    base = the_bmr + todays_activity_calories
    if goal == "bulk":
        target_calories = base + profile["bulk_surplus_cal"]
    else:
        # maintain and performance use the same formula; performance differs
        # only in messaging (never under-fuel on hard training days).
        target_calories = base

    target_protein_g = weight_kg * profile["protein_factor_g_per_kg"]
    remaining_cal = max(target_calories - (target_protein_g * 4), 0)
    carbs_ratio, fat_ratio, carb_shift = _carb_fat_ratio_for_today(
        profile["carbs_ratio"], profile["fat_ratio"], todays_activity_calories
    )
    target_carbs_g = (remaining_cal * carbs_ratio) / 4
    target_fat_g = (remaining_cal * fat_ratio) / 9
    target_fiber_g = profile["fiber_target_g"]

    water_activity_bonus_ml = round((todays_activity_calories or 0) * WATER_ML_PER_ACTIVITY_CAL)
    target_water_ml = (weight_kg * profile["water_ml_per_kg"]) + water_activity_bonus_ml

    parts = [f"{round(the_bmr):,} BMR"]
    if todays_activity_calories:
        parts.append(f"{round(todays_activity_calories):,} today's activity")
        formula = " + ".join(parts) + f" = {round(target_calories):,} cal."
    else:
        formula = f"{round(the_bmr):,} BMR = {round(target_calories):,} cal (no activity logged today)."

    goal_label = {"maintain": "Maintain", "bulk": "Bulk", "performance": "Performance"}[goal]
    if goal == "bulk":
        goal_line = f"Goal: Bulk, so a {round(profile['bulk_surplus_cal']):,} cal surplus is added."
    else:
        goal_line = f"Goal: {goal_label}, so no added surplus."

    protein_line = (
        f"Protein target: {round(target_protein_g)}g "
        f"({profile['protein_factor_g_per_kg']}g x {round(weight_kg)}kg)."
    )

    if abs(carb_shift) >= 0.01:
        direction = "up" if carb_shift > 0 else "down"
        carb_line = f"Carb ratio shifted {direction} for today's training load ({round(carbs_ratio * 100)}% of remaining cal)."
    else:
        carb_line = ""

    if water_activity_bonus_ml:
        water_line = f"Water target includes +{water_activity_bonus_ml:,}ml for today's activity."
    else:
        water_line = ""

    breakdown_text = " ".join(
        part for part in (formula, goal_line, protein_line, carb_line, water_line) if part
    )

    return {
        "target_calories": round(target_calories),
        "target_protein_g": round(target_protein_g, 1),
        "target_carbs_g": round(target_carbs_g, 1),
        "target_fat_g": round(target_fat_g, 1),
        "target_fiber_g": round(target_fiber_g, 1),
        "target_water_ml": round(target_water_ml),
        "water_activity_bonus_ml": water_activity_bonus_ml,
        "breakdown_text": breakdown_text,
        "bmr": round(the_bmr),
    }


def macro_status(logged: float, target: float) -> dict:
    """Status of a single macro/fiber value against its target.

    Within +/-10% -> "on track". More than 10% under -> "increase by Ng".
    More than 10% over -> "decrease by Ng".
    """
    logged = logged or 0
    target = target or 0
    if target <= 0:
        return {"status": "good", "label": "on track", "delta_g": 0, "pct": 0}

    pct = logged / target
    if pct < 0.9:
        delta = round(target - logged)
        return {"status": "warn", "label": f"increase {delta}g", "delta_g": delta, "pct": pct}
    if pct > 1.1:
        delta = round(logged - target)
        return {"status": "over", "label": "decrease " + str(delta) + "g", "delta_g": -delta, "pct": pct}
    return {"status": "good", "label": "on track", "delta_g": 0, "pct": pct}


def daily_nutrition_recommendation(logged_totals: dict, target: dict, water: dict) -> dict:
    """Verdict for the day's food + water ledger, generated on demand when
    the user clicks "Submit today's log" (not shown passively like the
    always-on macro bars) — this is what gets snapshotted into
    day_summaries so it's preserved as history.
    """
    target_cal = target.get("target_calories") or 0
    logged_cal = logged_totals.get("calories") or 0

    if target_cal <= 0:
        return {
            "status": "good",
            "title": "Nothing to recommend yet",
            "message": "Finish onboarding to get a daily target, then log today's food to get a recommendation.",
            "calories_diff": 0,
        }

    diff = target_cal - logged_cal  # positive => under target, negative => over
    pct = logged_cal / target_cal

    protein_stat = macro_status(logged_totals.get("protein_g"), target.get("target_protein_g"))
    water_pct = water.get("pct", 0) if water else 0

    protein_suggestions = []
    extras = []
    if protein_stat["status"] == "warn":
        extras.append(f"protein is {protein_stat['delta_g']}g short of target")
        protein_suggestions = nutrition.suggest_protein_foods(protein_stat["delta_g"])
    elif protein_stat["status"] == "over":
        extras.append(f"protein is {abs(protein_stat['delta_g'])}g over target")
    if water_pct < 70:
        extras.append(f"water is only at {water_pct}% of target")
    extra_text = f" Also: {', and '.join(extras)}." if extras else ""

    if protein_suggestions:
        options = " or ".join(
            f"{s['name']} ({s['protein_g']}g protein, {s['unit']})" for s in protein_suggestions
        )
        extra_text += f" To close the protein gap, try adding {options}."

    if 0.9 <= pct <= 1.1:
        status = "good"
        title = "On track for today"
        message = (
            f"You've logged {round(logged_cal):,} of {round(target_cal):,} cal — right in "
            f"range for today's target.{extra_text}"
        )
    elif pct < 0.9:
        status = "warn"
        title = "You're under today"
        message = (
            f"You've logged {round(logged_cal):,} of {round(target_cal):,} cal — about "
            f"{round(diff):,} cal left. Add a snack or a bigger portion at your next meal "
            f"to close the gap.{extra_text}"
        )
    else:
        status = "over"
        over_by = round(logged_cal - target_cal)
        title = "You're over today"
        message = (
            f"You've logged {round(logged_cal):,} of {round(target_cal):,} cal — about "
            f"{over_by:,} cal over. Keep the rest of today lighter; no need to compensate "
            f"tomorrow, just pick back up at your normal target.{extra_text}"
        )

    return {
        "status": status,
        "title": title,
        "message": message,
        "calories_diff": round(diff),
        "protein_suggestions": protein_suggestions,
    }


def water_progress(logged_ml: float, target_ml: float) -> dict:
    logged_ml = logged_ml or 0
    target_ml = target_ml or 1
    pct = min(100, round((logged_ml / target_ml) * 100))
    return {
        "logged_ml": round(logged_ml),
        "target_ml": round(target_ml),
        "pct": pct,
        "glasses": round(logged_ml / 250),
        "target_glasses": round(target_ml / 250),
    }


# ---------------------------------------------------------------------------
# Weekly summary tiles (past 7 days, inclusive of today)
# ---------------------------------------------------------------------------

def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value[:10]).date()


def week_window(today: date, days: int = 7):
    start = today - timedelta(days=days - 1)
    return start, today


def weekly_summary(activities: list, today: date) -> dict:
    start, end = week_window(today, 7)
    week_acts = [a for a in activities if start <= _parse_date(a["start_date"]) <= end]
    session_count = len(week_acts)
    types = sorted({a["type"] for a in week_acts})
    total_calories = sum((a["calories"] or 0) for a in week_acts)
    avg_per_day = total_calories / 7 if week_acts else 0
    return {
        "session_count": session_count,
        "types": types,
        "total_calories": round(total_calories),
        "avg_per_day": round(avg_per_day),
    }


# ---------------------------------------------------------------------------
# Training load + 7-day trend
# ---------------------------------------------------------------------------

def activity_load(activity) -> float:
    minutes = (activity["moving_time_s"] or 0) / 60
    factor = INTENSITY_FACTORS.get(activity["type"], INTENSITY_FACTORS["Other"])
    return minutes * factor


def daily_loads(activities: list, start: date, end: date) -> dict:
    """Returns {date: total_load} for every day in [start, end]."""
    loads = defaultdict(float)
    d = start
    while d <= end:
        loads[d] = 0.0
        d += timedelta(days=1)
    for a in activities:
        d = _parse_date(a["start_date"])
        if start <= d <= end:
            loads[d] += activity_load(a)
    return dict(sorted(loads.items()))


def seven_day_load_trend(activities: list, today: date) -> list:
    start, end = week_window(today, 7)
    loads = daily_loads(activities, start, end)
    return [
        {"date": d.isoformat(), "day_label": d.strftime("%a"), "load": round(v, 1)}
        for d, v in loads.items()
    ]


# ---------------------------------------------------------------------------
# Training recommendation
# ---------------------------------------------------------------------------

def training_recommendation(profile, activities: list, meals: list, today: date) -> dict:
    """Heuristic training-vs-fueling verdict.

    `activities` should cover at least the last 14 days, `meals` the last
    7 days, so week-over-week comparisons are possible.
    """
    start3, _ = week_window(today, 3)
    loads = daily_loads(activities, start3, today)
    load_values = list(loads.values())

    rising_3_days = len(load_values) >= 3 and load_values[-3] < load_values[-2] < load_values[-1]

    # Deficit / protein-under-target check over the last 3 days.
    meals_by_date = defaultdict(list)
    for m in meals:
        meals_by_date[_parse_date(m["date"])].append(m)

    deficits = []
    protein_under_days = 0
    for d in sorted(loads.keys()):
        day_meals = meals_by_date.get(d, [])
        logged_cal = sum((m["calories"] or 0) for m in day_meals)
        logged_protein = sum((m["protein_g"] or 0) for m in day_meals)
        day_activity_cal = sum(
            (a["calories"] or 0) for a in activities if _parse_date(a["start_date"]) == d
        )
        target = daily_target(profile, day_activity_cal)
        if logged_cal and (target["target_calories"] - logged_cal) > 300:
            deficits.append(target["target_calories"] - logged_cal)
        if logged_protein and logged_protein < target["target_protein_g"] * 0.9:
            protein_under_days += 1

    meaningful_deficit = len(deficits) >= 2 and statistics.mean(deficits) > 300
    protein_lagging = protein_under_days >= 2

    recent_avg_load = statistics.mean(load_values) if load_values else 0
    flat_or_low_load = recent_avg_load < 40 and not rising_3_days

    if rising_3_days and (meaningful_deficit or protein_lagging):
        status = "ease"
        reduced_minutes_pct = 35
        reasons = []
        if meaningful_deficit:
            reasons.append("running a calorie deficit")
        if protein_lagging:
            reasons.append("protein lagging behind target")
        reason_text = " and ".join(reasons)
        title = "Ease off today"
        message = (
            f"Load has climbed 3 days straight and you're {reason_text}. "
            f"Swap today's session for a light walk or rest day, and aim closer to "
            f"{reduced_minutes_pct}% of your usual session length if you do train."
        )
        tip = (
            "Load has risen noticeably since earlier this week. Prioritize sleep and "
            "hydration today too — recovery tends to lag a day behind fueling on weeks like this."
        )
    elif flat_or_low_load:
        status = "push"
        title = "Room to push"
        message = (
            "Recovery and fueling both look solid and your recent training load is flat. "
            "Good day to add intensity or stretch a session longer if you're feeling it."
        )
        tip = "This is a heuristic prompt based on recent load and fueling, not a coaching plan."
    else:
        status = "good"
        title = "On track"
        message = (
            "Training load and fueling look sustainable right now — keep doing what "
            "you're doing today."
        )
        tip = "This is a heuristic prompt based on recent load and fueling, not a coaching plan."

    return {"status": status, "title": title, "message": message, "tip": tip}


# ---------------------------------------------------------------------------
# Per-activity weekly breakdown, this week vs last week
# ---------------------------------------------------------------------------

def _fmt_pace(moving_time_s: float, distance_m: float) -> float | None:
    if not distance_m:
        return None
    return (moving_time_s / 60) / (distance_m / 1000)  # min per km


def _fmt_pace_str(pace_min_per_km: float) -> str:
    minutes = int(pace_min_per_km)
    seconds = round((pace_min_per_km - minutes) * 60)
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}/km"


def activity_weekly_breakdown(activities: list, today: date, recommendation_status: str) -> list:
    this_start, this_end = week_window(today, 7)
    last_start, last_end = this_start - timedelta(days=7), this_start - timedelta(days=1)

    this_week = [a for a in activities if this_start <= _parse_date(a["start_date"]) <= this_end]
    last_week = [a for a in activities if last_start <= _parse_date(a["start_date"]) <= last_end]

    types = sorted({a["type"] for a in this_week})

    # Find which type contributed most to a load rise, to flag it as an
    # "attention" delta rather than a naively positive one when the overall
    # verdict is "ease off".
    contributor_type = None
    if recommendation_status == "ease":
        gains = {}
        for t in types:
            this_load = sum(activity_load(a) for a in this_week if a["type"] == t)
            last_load = sum(activity_load(a) for a in last_week if a["type"] == t)
            gains[t] = this_load - last_load
        if gains:
            contributor_type = max(gains, key=gains.get)
            if gains[contributor_type] <= 0:
                contributor_type = None

    rows = []
    for t in types:
        this_acts = [a for a in this_week if a["type"] == t]
        last_acts = [a for a in last_week if a["type"] == t]
        sessions = len(this_acts)
        total_duration_min = sum((a["moving_time_s"] or 0) for a in this_acts) / 60
        last_duration_min = sum((a["moving_time_s"] or 0) for a in last_acts) / 60

        delta_text = "first one logged this month"
        delta_class = "neutral"

        if t in PACE_TYPES:
            distance_km = sum((a["distance_m"] or 0) for a in this_acts) / 1000
            total_time_s = sum((a["moving_time_s"] or 0) for a in this_acts)
            pace = _fmt_pace(total_time_s, distance_km * 1000)
            if t == "Hike":
                elevation_m = sum((a["elevation_gain_m"] or 0) for a in this_acts)
                detail = f"{distance_km:.1f} km · {round(elevation_m)}m elevation"
            elif pace:
                detail = f"{distance_km:.1f} km · avg pace {_fmt_pace_str(pace)}"
            else:
                detail = f"{distance_km:.1f} km"

            last_distance_km = sum((a["distance_m"] or 0) for a in last_acts) / 1000
            last_time_s = sum((a["moving_time_s"] or 0) for a in last_acts)
            last_pace = _fmt_pace(last_time_s, last_distance_km * 1000)
            if pace and last_pace:
                delta_s_per_km = round((pace - last_pace) * 60)
                if delta_s_per_km < 0:
                    delta_text = f"↓{abs(delta_s_per_km)}s/km vs last week"
                    delta_class = "up"
                elif delta_s_per_km > 0:
                    delta_text = f"↑{delta_s_per_km}s/km vs last week"
                    delta_class = "down"
                else:
                    delta_text = "same pace vs last week"
                    delta_class = "neutral"
        elif t in MINUTES_DELTA_TYPES:
            detail = f"{total_duration_min / 60:.1f} hrs on court" if total_duration_min >= 60 else f"{round(total_duration_min)} min"
            if last_acts:
                delta_min = round(total_duration_min - last_duration_min)
                if delta_min != 0:
                    arrow = "↑" if delta_min > 0 else "↓"
                    delta_text = f"{arrow}{abs(delta_min)} min vs last week"
                    delta_class = "up" if delta_min > 0 else "down"
                    if t == contributor_type and delta_min > 0:
                        delta_class = "down"
                else:
                    delta_text = "same duration vs last week"
                    delta_class = "neutral"
        else:
            # Duration/volume-style types (Gym, Cycle, Swim, Yoga, HIIT, etc).
            # Note: Apple Health's workout export has no set/rep/weight data,
            # so total session duration is used as the volume proxy.
            detail = f"total volume {round(total_duration_min)} min"
            if last_duration_min:
                pct = round(((total_duration_min - last_duration_min) / last_duration_min) * 100)
                if pct != 0:
                    arrow = "↑" if pct > 0 else "↓"
                    delta_text = f"{arrow}{abs(pct)}% vs last week"
                    delta_class = "up" if pct > 0 else "down"
                    if t == contributor_type and pct > 0:
                        delta_class = "down"
                else:
                    delta_text = "same volume vs last week"
                    delta_class = "neutral"

        rows.append(
            {
                "type": t,
                "sessions": sessions,
                "detail": detail,
                "delta_text": delta_text,
                "delta_class": delta_class,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Training insights: injury-risk / overload flags, aerobic efficiency trend,
# and a positively-framed weekly balance score. All pure functions over
# `activities` (expects at least ~60 days for the aerobic-efficiency trend;
# the caller decides how much history to pass in).
# ---------------------------------------------------------------------------

# The "10% rule": week-over-week volume jumps bigger than this are the
# classic overuse-injury trigger, regardless of the absolute load level.
OVERLOAD_PCT_THRESHOLD = 0.10
# A single activity type ramping faster than this in one week (e.g. running
# mileage) is flagged separately from the overall-volume check.
TYPE_SPIKE_PCT_THRESHOLD = 0.25
# High-impact activity types that stack injury risk when several land in one
# week (running + tennis + badminton all pound joints, unlike swim/cycle/yoga).
HIGH_IMPACT_TYPES = {"Run", "Tennis", "Badminton", "Pickleball", "HIIT"}
IMPACT_STACK_DAY_THRESHOLD = 4
# Daily training load at/below this counts as a rest or true recovery day.
REST_DAY_LOAD_THRESHOLD = 15
NO_REST_DAY_STREAK = 7


def overload_flags(activities: list, today: date) -> list:
    """The '10% rule': flags weeks where total training volume, or a single
    activity type's volume/distance, jumped more than the threshold over the
    prior week. Sudden spikes -- not sustained high load -- are what the
    injury literature ties to overuse risk, so this is checked separately
    from (and in addition to) the day-to-day training_recommendation heuristic.
    """
    this_start, this_end = week_window(today, 7)
    last_start, last_end = this_start - timedelta(days=7), this_start - timedelta(days=1)

    this_week = [a for a in activities if this_start <= _parse_date(a["start_date"]) <= this_end]
    last_week = [a for a in activities if last_start <= _parse_date(a["start_date"]) <= last_end]

    flags = []

    this_total_min = sum((a["moving_time_s"] or 0) for a in this_week) / 60
    last_total_min = sum((a["moving_time_s"] or 0) for a in last_week) / 60
    if last_total_min >= 20:  # only meaningful once there's a real baseline week
        pct = (this_total_min - last_total_min) / last_total_min
        if pct > OVERLOAD_PCT_THRESHOLD:
            flags.append(
                {
                    "icon": "⚠️",
                    "title": "Weekly volume jumped",
                    "message": (
                        f"Total training time is up {round(pct * 100)}% vs last week "
                        f"({round(last_total_min)} → {round(this_total_min)} min). Jumps over "
                        f"~10% are the classic overuse-injury trigger -- consider holding this "
                        f"week's volume flat before adding more."
                    ),
                    "tone": "warn",
                }
            )

    # Per-type spike: distance for endurance types, duration for everything else.
    types = sorted({a["type"] for a in this_week})
    for t in types:
        this_acts = [a for a in this_week if a["type"] == t]
        last_acts = [a for a in last_week if a["type"] == t]
        if not last_acts:
            continue
        if t in PACE_TYPES or t in ("Cycle", "Swim"):
            this_val = sum((a["distance_m"] or 0) for a in this_acts) / 1000
            last_val = sum((a["distance_m"] or 0) for a in last_acts) / 1000
            unit = "km"
        else:
            this_val = sum((a["moving_time_s"] or 0) for a in this_acts) / 60
            last_val = sum((a["moving_time_s"] or 0) for a in last_acts) / 60
            unit = "min"
        if last_val <= 0:
            continue
        pct = (this_val - last_val) / last_val
        if pct > TYPE_SPIKE_PCT_THRESHOLD:
            flags.append(
                {
                    "icon": "📈",
                    "title": f"{t} volume spiked",
                    "message": (
                        f"{t} is up {round(pct * 100)}% vs last week "
                        f"({last_val:.1f} → {this_val:.1f} {unit}). Ramping a single activity type "
                        f"this fast is a common overuse pattern -- ease the increase over 2-3 weeks "
                        f"instead of all at once."
                    ),
                    "tone": "warn",
                }
            )

    return flags


def impact_stacking_flag(activities: list, today: date) -> dict | None:
    """Flags when several high-impact activity types (running, racquet
    sports, HIIT) land in the same week -- repetitive-impact stacking is its
    own overuse-injury pattern distinct from a pure volume spike."""
    this_start, this_end = week_window(today, 7)
    this_week = [a for a in activities if this_start <= _parse_date(a["start_date"]) <= this_end]
    impact_days = {
        _parse_date(a["start_date"]) for a in this_week if a["type"] in HIGH_IMPACT_TYPES
    }
    if len(impact_days) < IMPACT_STACK_DAY_THRESHOLD:
        return None
    types_hit = sorted({a["type"] for a in this_week if a["type"] in HIGH_IMPACT_TYPES})
    return {
        "icon": "🦵",
        "title": "High-impact days stacking up",
        "message": (
            f"{len(impact_days)} high-impact days this week ({', '.join(types_hit)}). Repetitive "
            f"impact across multiple sports is a known overuse pattern -- swap one for a "
            f"lower-impact day (swim, cycle, yoga) this week."
        ),
        "tone": "warn",
    }


def days_since_rest_day(activities: list, today: date) -> int:
    """How many consecutive days up to and including today have had
    meaningful training load -- i.e. days since the last true rest day."""
    d = today
    streak = 0
    while streak <= 30:  # safety bound against bad/incomplete data
        day_load = sum(activity_load(a) for a in activities if _parse_date(a["start_date"]) == d)
        if day_load <= REST_DAY_LOAD_THRESHOLD:
            break
        streak += 1
        d -= timedelta(days=1)
    return streak


def rest_day_flag(activities: list, today: date) -> dict | None:
    streak = days_since_rest_day(activities, today)
    if streak < NO_REST_DAY_STREAK:
        return None
    return {
        "icon": "🛌",
        "title": "No rest day in a while",
        "message": (
            f"{streak} days in a row with meaningful training load. A full rest day, or a very "
            f"light one (walk, gentle yoga), would let recovery catch up."
        ),
        "tone": "warn",
    }


def aerobic_efficiency_trend(activities: list, today: date) -> dict | None:
    """Slow-moving fatigue signal: pace-for-heart-rate on runs, compared
    over the last two ~30-day windows using Strava's own avg-HR and pace
    fields -- no extra hardware needed. Tracked monthly, not daily, since
    this is meant to validate or override the day-to-day load-based verdict
    with something more physiological.
    """
    recent_start = today - timedelta(days=29)
    prior_start = today - timedelta(days=59)
    prior_end = today - timedelta(days=30)

    def _window_efficiency(acts):
        total_dist_km = sum((a["distance_m"] or 0) for a in acts) / 1000
        total_time_min = sum((a["moving_time_s"] or 0) for a in acts) / 60
        hr_acts = [a for a in acts if a["avg_hr"]]
        if not hr_acts or total_dist_km <= 0 or total_time_min <= 0:
            return None
        avg_hr = statistics.mean(a["avg_hr"] for a in hr_acts)
        avg_pace = total_time_min / total_dist_km  # min per km
        return {"avg_hr": avg_hr, "avg_pace": avg_pace}

    recent_runs = [
        a for a in activities
        if a["type"] == "Run" and recent_start <= _parse_date(a["start_date"]) <= today
    ]
    prior_runs = [
        a for a in activities
        if a["type"] == "Run" and prior_start <= _parse_date(a["start_date"]) <= prior_end
    ]

    recent = _window_efficiency(recent_runs)
    prior = _window_efficiency(prior_runs)
    if not recent or not prior:
        return None

    # Seconds per km per bpm -- lower means covering more ground for the
    # same heart-rate effort (i.e. more efficient).
    recent_ratio = (recent["avg_pace"] * 60) / recent["avg_hr"]
    prior_ratio = (prior["avg_pace"] * 60) / prior["avg_hr"]
    if prior_ratio <= 0:
        return None
    pct_change = (recent_ratio - prior_ratio) / prior_ratio

    if pct_change > 0.05:
        return {
            "icon": "📉",
            "title": "Aerobic efficiency trending down",
            "message": (
                f"Pace-for-effort on runs has drifted {round(pct_change * 100)}% over the last "
                f"month (avg HR {round(prior['avg_hr'])}→{round(recent['avg_hr'])} bpm at a "
                f"similar pace). This moves slower than day-to-day load -- worth weighing "
                f"alongside today's training call as a fatigue signal."
            ),
            "tone": "warn",
        }
    if pct_change < -0.05:
        return {
            "icon": "📈",
            "title": "Aerobic efficiency improving",
            "message": (
                f"Pace-for-effort on runs has improved {round(abs(pct_change) * 100)}% over the "
                f"last month -- the same heart-rate effort is covering more ground now. Fitness "
                f"is trending the right way."
            ),
            "tone": "good",
        }
    return None


def weekly_balance_score(activities: list, today: date) -> dict | None:
    """Positive-framing composite score (0-100): rewards activity variety
    and actually taking rest days, and penalizes a week dominated by one
    huge spike day -- something to feel good about, not just warnings."""
    start, end = week_window(today, 7)
    week_acts = [a for a in activities if start <= _parse_date(a["start_date"]) <= end]
    if not week_acts:
        return None

    types = {a["type"] for a in week_acts}
    variety_score = min(40, len(types) * 13)

    loads = daily_loads(activities, start, end)
    rest_days = sum(1 for v in loads.values() if v <= REST_DAY_LOAD_THRESHOLD)
    rest_score = min(30, rest_days * 10)

    load_values = list(loads.values())
    spike_penalty = 0
    if load_values and sum(load_values) > 0:
        top_share = max(load_values) / sum(load_values)
        if top_share > 0.6:
            spike_penalty = 15
    consistency_score = 30 - spike_penalty

    score = max(0, min(100, round(variety_score + rest_score + consistency_score)))

    if score >= 75:
        label, tone = "Well balanced week", "good"
    elif score >= 50:
        label, tone = "Decent balance", "good"
    else:
        label, tone = "Room for more balance", "info"

    return {
        "icon": "⚖️",
        "title": label,
        "message": (
            f"Balance score {score}/100 this week -- {len(types)} activity type"
            f"{'s' if len(types) != 1 else ''}, {rest_days} lighter/rest day"
            f"{'s' if rest_days != 1 else ''}."
        ),
        "tone": tone,
        "score": score,
    }


def _load_fallback_insight(activities: list, today: date) -> dict:
    """Evergreen load status for when neither the 10%-rule nor the
    impact-stacking check fired -- keeps the "load" category from silently
    disappearing on an otherwise-unremarkable week."""
    start, end = week_window(today, 7)
    week_acts = [a for a in activities if start <= _parse_date(a["start_date"]) <= end]
    if not week_acts:
        return {
            "icon": "📋",
            "title": "No sessions logged this week",
            "message": "Once activity syncs in, load and recovery insights will show up here.",
            "tone": "info",
        }
    total_min = round(sum((a["moving_time_s"] or 0) for a in week_acts) / 60)
    return {
        "icon": "📈",
        "title": "Load looks manageable",
        "message": (
            f"{len(week_acts)} session{'s' if len(week_acts) != 1 else ''} "
            f"({total_min} min) this week -- no volume spikes or impact stacking flagged."
        ),
        "tone": "good",
    }


def _recovery_fallback_insight(activities: list, today: date) -> dict:
    """Evergreen recovery status for when the no-rest-day streak isn't long
    enough to warrant a warning -- the positive counterpart to
    rest_day_flag, so the "recovery" category always says something."""
    streak = days_since_rest_day(activities, today)
    if streak == 0:
        message = "Today (or yesterday) was a lighter/rest day -- recovery is on track."
    else:
        message = (
            f"{streak} day{'s' if streak != 1 else ''} since your last rest day -- "
            f"still within a healthy range."
        )
    return {"icon": "🛌", "title": "Recovery looks good", "message": message, "tone": "good"}


def training_insights(activities: list, today: date) -> list:
    """Aggregates all training-side flags/insights into one ordered list of
    cards for the dashboard: warnings first (overload, impact stacking, no
    rest day, efficiency trend), then the balance score last so the section
    doesn't open on an all-warnings note when things are otherwise fine.

    Grouped into three categories -- load, recovery, efficiency/balance --
    each of which always contributes at least one card. The underlying
    checks are gated on real signals (a real spike, a real long no-rest
    streak, two months of runs, etc.), so a quiet, unremarkable week would
    otherwise leave this card row empty; an evergreen fallback grounded in
    the same data fills in instead, matching the design's fixed 3-card
    "Training recommendations" row.
    """
    insights = list(overload_flags(activities, today))
    impact = impact_stacking_flag(activities, today)
    if impact:
        insights.append(impact)
    if not insights:
        insights.append(_load_fallback_insight(activities, today))

    rest = rest_day_flag(activities, today)
    insights.append(rest or _recovery_fallback_insight(activities, today))

    efficiency = aerobic_efficiency_trend(activities, today)
    if efficiency:
        insights.append(efficiency)
    balance = weekly_balance_score(activities, today)
    if balance:
        insights.append(balance)
    elif not efficiency:
        # weekly_balance_score only returns None when there's truly no
        # activity logged this week -- say so plainly instead of leaving
        # this category empty.
        insights.append(
            {
                "icon": "⚖️",
                "title": "No activity logged this week yet",
                "message": "Balance and efficiency insights need at least a session or two to say anything useful.",
                "tone": "info",
            }
        )

    return insights


# ---------------------------------------------------------------------------
# Nutrition insights: energy availability, training-day-aware nudges, and
# post-workout protein timing (cross-referencing meal-log timestamps against
# Strava session end-times).
# ---------------------------------------------------------------------------

# Rough sports-science "low energy availability" threshold, in kcal left
# over (after exercise) per kg of body weight per day. Uses total body
# weight as a simplification since this app doesn't track lean body mass.
LOW_ENERGY_AVAILABILITY_KCAL_PER_KG = 30
# An activity-calorie or duration level big enough to count as "today was a
# long/hard session" for the carb-up-tonight nudge.
HARD_SESSION_ACTIVITY_CAL_THRESHOLD = 500
HARD_SESSION_MINUTES_THRESHOLD = 75


def energy_availability(meals: list, activities: list, weight_kg: float, today: date) -> dict | None:
    """Weekly Relative-Energy-Availability-style score: (calories eaten -
    exercise calories burned) per kg of body weight, averaged over logged
    days in the last 7. This is the real merge of the food and training
    ledgers -- the body cares whether fueling matches training demand, not
    calorie balance in isolation. Being in a deficit AND training hard at
    the same time is the real risk combo (not either alone), which is why
    this only fires once there's enough logged history to say something.
    """
    if not weight_kg:
        return None
    start, end = week_window(today, 7)

    days_with_data = 0
    total_ea = 0.0
    d = start
    while d <= end:
        day_meals = [m for m in meals if _parse_date(m["date"]) == d]
        logged_cal = sum((m["calories"] or 0) for m in day_meals)
        if logged_cal:  # only count days that actually have food logged
            day_acts = [a for a in activities if _parse_date(a["start_date"]) == d]
            activity_cal = sum((a["calories"] or 0) for a in day_acts)
            total_ea += (logged_cal - activity_cal) / weight_kg
            days_with_data += 1
        d += timedelta(days=1)

    if days_with_data < 3:
        return None

    avg_ea = total_ea / days_with_data

    if avg_ea < LOW_ENERGY_AVAILABILITY_KCAL_PER_KG:
        return {
            "icon": "🔋",
            "title": "Fueling looks low for your training",
            "message": (
                f"Averaging ~{round(avg_ea)} kcal/kg/day left over after exercise across "
                f"{days_with_data} logged days this week -- below the "
                f"~{LOW_ENERGY_AVAILABILITY_KCAL_PER_KG} kcal/kg range tied to energy-availability "
                f"risk. Running a deficit while training hard is the real risk combo, not either "
                f"one alone."
            ),
            "tone": "warn",
        }
    return {
        "icon": "🔋",
        "title": "Fueling matches your training",
        "message": (
            f"Averaging ~{round(avg_ea)} kcal/kg/day left over after exercise across "
            f"{days_with_data} logged days this week -- comfortably fueling what you're asking "
            f"your body to do."
        ),
        "tone": "good",
    }


def carb_up_nudge(todays_activities: list) -> dict | None:
    """Retrospective version of 'carb up before a hard session' -- the app
    only sees activities after they happen, not a training plan, so this
    fires the evening of a long/hard day to help restock glycogen ahead of
    whatever's next, rather than trying to predict tomorrow."""
    total_cal = sum((a["calories"] or 0) for a in todays_activities)
    total_min = sum((a["moving_time_s"] or 0) for a in todays_activities) / 60
    if total_cal < HARD_SESSION_ACTIVITY_CAL_THRESHOLD and total_min < HARD_SESSION_MINUTES_THRESHOLD:
        return None
    return {
        "icon": "🍚",
        "title": "Carb up tonight",
        "message": (
            "Today was a long/hard session -- leaning toward complex carbs at your evening meal "
            "helps restock glycogen stores before your next session."
        ),
        "tone": "info",
    }


def hydration_note(target: dict, todays_activities: list) -> dict | None:
    """Surfaces the activity-based water bump already folded into
    daily_target's target_water_ml, so a long/hot session's extra hydration
    need is visible, not just baked silently into one number."""
    bonus = target.get("water_activity_bonus_ml") or 0
    if bonus < 200 or not todays_activities:
        return None
    return {
        "icon": "💧",
        "title": "Hydration target bumped up",
        "message": (
            f"Today's water target is up {bonus:,} ml for what you burned in today's session(s) "
            f"-- a long or hot session needs more than a flat weight-based number."
        ),
        "tone": "info",
    }


def post_workout_protein_check(todays_activities: list, todays_meals: list) -> dict | None:
    """Cross-references today's strength-training end-time (Strava) against
    meal-log timestamps (both already collected, just never compared
    before) to flag whether a protein-forward meal landed in the ~2h
    post-session window that matters most for muscle repair."""
    strength_acts = [a for a in todays_activities if a["type"] == "WeightTraining"]
    if not strength_acts:
        return None

    def _act_end(a):
        start = datetime.fromisoformat(a["start_date"])
        return start + timedelta(seconds=a["moving_time_s"] or 0)

    latest_end = max(_act_end(a) for a in strength_acts)

    logged_after = []
    for m in todays_meals:
        if not m["logged_at"]:
            continue
        try:
            logged_at = datetime.fromisoformat(m["logged_at"])
        except ValueError:
            continue
        if logged_at >= latest_end:
            logged_after.append(logged_at)

    if logged_after and min(logged_after) - latest_end <= timedelta(hours=2):
        return {
            "icon": "💪",
            "title": "Protein timing looks good",
            "message": "You logged a meal within 2h of today's strength session -- good window for muscle repair.",
            "tone": "good",
        }

    hours_since = (datetime.now() - latest_end).total_seconds() / 3600
    if hours_since >= 2:
        return {
            "icon": "💪",
            "title": "Protein window may have passed",
            "message": (
                f"Today's strength session ended about {round(hours_since)}h ago and no meal's "
                f"been logged since. A protein-forward meal or shake helps most within ~2h "
                f"post-session."
            ),
            "tone": "warn",
        }
    return {
        "icon": "💪",
        "title": "Strength session logged",
        "message": "Aim for a protein-forward meal within the next couple hours to support muscle repair.",
        "tone": "info",
    }


NUTRITION_INSIGHT_CARD_COUNT = 3  # matches the fixed 3-card "Nutrition recommendations" row in the UI


def _protein_fallback_insight(todays_meals: list, target: dict) -> dict:
    """Evergreen protein card, always answerable from today's log + target
    -- no gating, so the card row never goes empty just because there isn't
    enough history yet for the fancier protein-timing check."""
    logged = sum((m["protein_g"] or 0) for m in todays_meals)
    tgt = target.get("target_protein_g") or 0
    if tgt and logged < tgt * 0.9:
        gap = round(tgt - logged)
        return {
            "icon": "🥩",
            "title": "Boost your protein",
            "message": (
                f"You've logged {round(logged)}g of your {round(tgt)}g target today "
                f"-- aim to close the {gap}g gap, especially around training."
            ),
            "tone": "warn",
        }
    return {
        "icon": "🥩",
        "title": "Protein on track",
        "message": f"You've logged {round(logged)}g of your {round(tgt)}g target today -- nice work.",
        "tone": "good",
    }


def _carb_timing_fallback_insight() -> dict:
    """Evergreen carb-timing tip for days the personalized carb_up_nudge
    doesn't fire (i.e. today wasn't itself a long/hard session)."""
    return {
        "icon": "🍚",
        "title": "Carb timing",
        "message": (
            "If you're training tomorrow, eating complex carbs tonight helps top up "
            "glycogen stores ahead of the session."
        ),
        "tone": "info",
    }


def _hydration_fallback_insight(water_logged_ml: float, target: dict) -> dict:
    """Evergreen hydration card based on today's actual progress, for days
    without an activity-driven water bump to talk about."""
    tgt = target.get("target_water_ml") or 0
    logged = water_logged_ml or 0
    pct = 0 if tgt <= 0 else round((logged / tgt) * 100)
    tone = "warn" if pct < 30 else "info"
    return {
        "icon": "💧",
        "title": "Hydration",
        "message": (
            f"You're at {round(logged):,} ml. Aim for {round(tgt):,} ml today -- "
            f"spread evenly across meals helps absorption."
        ),
        "tone": tone,
    }


def nutrition_insights(
    meals_7: list, activities_14: list, todays_activities: list, todays_meals: list,
    profile, target: dict, today: date, water_logged_ml: float = 0
) -> list:
    """Aggregates all food-side insights into one ordered list of cards for
    the dashboard.

    The sports-science checks (energy availability, carb-up, hydration bump,
    post-workout protein timing) are personalized but gated -- they only
    fire when there's a real signal (e.g. today actually had a hard
    session, or a week of logged food). Left alone, a quiet week means this
    entire card row silently disappears, which reads as broken rather than
    "not enough data yet". So each category (protein / carbs / hydration)
    always contributes exactly one card: the personalized version if it
    fired, otherwise an evergreen fallback grounded in today's actual
    logged totals -- matching the always-3-cards "Nutrition recommendations"
    row in the design.
    """
    insights = []

    protein_timing = post_workout_protein_check(todays_activities, todays_meals)
    insights.append(protein_timing or _protein_fallback_insight(todays_meals, target))

    carb_nudge = carb_up_nudge(todays_activities)
    insights.append(carb_nudge or _carb_timing_fallback_insight())

    hydration = hydration_note(target, todays_activities)
    insights.append(hydration or _hydration_fallback_insight(water_logged_ml, target))

    ea = energy_availability(meals_7, activities_14, profile["weight_kg"], today)
    if ea:
        insights.append(ea)

    return insights


# ---------------------------------------------------------------------------
# Personal pattern call-outs: simple correlations across a user's own
# history, once there's enough of it to say anything meaningful. Per the
# product idea this is genuinely uninteresting in week one and genuinely
# interesting by month two or three, so it stays silent below the threshold
# rather than forcing a generic insight out of too little data.
# ---------------------------------------------------------------------------

MIN_CHECKIN_DAYS_FOR_PATTERNS = 21  # ~3 weeks of submitted daily check-ins


def personal_pattern_insights(day_summaries: list, activities: list) -> list:
    """`day_summaries` is the full check-in history (one row per date the
    user hit "Submit today's log"), `activities` should cover at least that
    same date range so next-day training load can be looked up.
    """
    if len(day_summaries) < MIN_CHECKIN_DAYS_FOR_PATTERNS:
        return []

    hit_next_day_loads = []
    miss_next_day_loads = []
    for s in day_summaries:
        d = _parse_date(s["date"])
        next_d = d + timedelta(days=1)
        next_day_load = sum(
            activity_load(a) for a in activities if _parse_date(a["start_date"]) == next_d
        )
        target_protein = s["target_protein_g"] or 1
        hit = (s["protein_g"] or 0) >= target_protein * 0.9
        (hit_next_day_loads if hit else miss_next_day_loads).append(next_day_load)

    insights = []
    if len(hit_next_day_loads) >= 5 and len(miss_next_day_loads) >= 5:
        hit_avg = statistics.mean(hit_next_day_loads)
        miss_avg = statistics.mean(miss_next_day_loads)
        if miss_avg > 0 and hit_avg > miss_avg * 1.15:
            insights.append(
                {
                    "icon": "🔍",
                    "title": "Personal pattern spotted",
                    "message": (
                        f"On days you hit your protein target, the next day's training load runs "
                        f"~{round(((hit_avg / miss_avg) - 1) * 100)}% higher than after days you "
                        f"miss it -- worth keeping protein consistent, not just on hard-training days."
                    ),
                    "tone": "good",
                }
            )

    return insights
