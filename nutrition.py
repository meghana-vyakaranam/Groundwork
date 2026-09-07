"""Food-name -> macro lookup and free-text meal parsing.

Matching uses token overlap (not substring) against each food's name and
its aliases, so messy input like "pb sandwich" still resolves to
"peanut butter sandwich".
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

# The curated dish list bundled with the repo (132+ Indian dishes + staples,
# checked into git) — used as a seed, never written to directly once DATA_DIR
# points elsewhere.
_BUNDLED_DB_PATH = Path(__file__).parent / "data" / "food_db.json"

# Overridable via DATA_DIR (shared with strava_import.py's UPLOAD_PATH) so
# AI-learned entries persist on a persistent disk when hosted somewhere with
# an ephemeral filesystem — otherwise every deploy/restart on a host like
# Render would silently discard everything learned via the Anthropic API
# fallback. Defaults to the in-repo data/ folder for local runs.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path(__file__).parent / "data"))
DB_PATH = DATA_DIR / "food_db.json"


def _ensure_db_seeded() -> None:
    """First boot against an external DATA_DIR (e.g. a fresh Render disk)
    won't have food_db.json yet — seed it from the bundled copy so the
    curated dish list is available immediately, before anything gets
    AI-learned into it."""
    if DB_PATH == _BUNDLED_DB_PATH:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and _BUNDLED_DB_PATH.exists():
        shutil.copy(_BUNDLED_DB_PATH, DB_PATH)


_ensure_db_seeded()

_QTY_X_SUFFIX = re.compile(r"\bx\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_QTY_X_PREFIX = re.compile(r"\b(\d+(?:\.\d+)?)\s*x\b", re.IGNORECASE)
_QTY_LEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)\s+")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

_MATCH_THRESHOLD = 0.5
# A match also has to cover most of BOTH the query's tokens and the
# variant's tokens -- not just clear the combined _MATCH_THRESHOLD score.
# Without this, a query like "chole bhature" (2 tokens) could hijack-match
# on a single shared word against a one-word alias like "chole" (an alias
# of "chana masala"): overlap=1, max(2,1)=2, score=0.5 clears the old
# threshold even though "bhature" -- half the dish name, and the half that
# makes it a completely different, much higher-calorie dish -- was ignored.
# That silently returned the wrong dish's macros instead of falling through
# to a real lookup for the exact thing typed. Requiring high coverage on
# both sides rejects partial/incomplete matches like that while still
# allowing genuine near-exact matches (typos, registered short aliases like
# "pb sandwich") through.
_MIN_TOKEN_COVERAGE = 0.6


def _load_foods() -> list:
    with open(DB_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["foods"]


_FOODS_CACHE = None


def _foods():
    global _FOODS_CACHE
    if _FOODS_CACHE is None:
        _FOODS_CACHE = _load_foods()
    return _FOODS_CACHE


def suggest_protein_foods(deficit_g: float, limit: int = 3) -> list:
    """Pick foods from food_db.json best-suited to close a protein gap of
    roughly `deficit_g` grams — ranked by protein-per-calorie (so a
    suggestion doesn't cost the whole rest of the day's calorie budget),
    with a preference for items sized close to the actual gap rather than
    ones that barely help or wildly overshoot it.

    Used by calculations.daily_nutrition_recommendation() to turn "protein
    is 25g short" into concrete "add X" suggestions instead of just a
    number.
    """
    if not deficit_g or deficit_g <= 0:
        return []

    scored = []
    for f in _foods():
        protein = f.get("protein_g") or 0
        cal = f.get("calories") or 0
        if protein <= 0 or cal <= 0:
            continue
        ratio = protein / cal
        size_fit = 1.0
        if protein < deficit_g * 0.25:
            size_fit = 0.5  # too small to meaningfully close the gap
        elif protein > deficit_g * 2.5:
            size_fit = 0.7  # would badly overshoot the gap
        scored.append((ratio * size_fit, f["name"], f.get("unit"), protein, cal))

    scored.sort(key=lambda row: row[0], reverse=True)
    seen = set()
    out = []
    for _, name, unit, protein, cal in scored:
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "unit": unit, "protein_g": protein, "calories": cal})
        if len(out) >= limit:
            break
    return out


def _learn_food(entry: dict) -> None:
    """Append a newly AI-estimated food to food_db.json and the in-memory
    cache, so future lookups (even after a restart) match it locally
    without another API call."""
    foods = _foods()
    foods.append(entry)
    with open(DB_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data["foods"] = foods
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def tokenize(text: str) -> set:
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if t}


def extract_quantity(item_text: str) -> tuple:
    """Returns (quantity, remaining_text) for a single comma-separated item."""
    text = item_text.strip()

    m = _QTY_X_SUFFIX.search(text)
    if m:
        qty = float(m.group(1))
        text = (text[: m.start()] + text[m.end() :]).strip()
        return qty, re.sub(r"\s+", " ", text).strip()

    m = _QTY_X_PREFIX.search(text)
    if m:
        qty = float(m.group(1))
        text = (text[: m.start()] + text[m.end() :]).strip()
        return qty, re.sub(r"\s+", " ", text).strip()

    m = _QTY_LEADING_NUMBER.match(text)
    if m:
        qty = float(m.group(1))
        text = text[m.end() :].strip()
        return qty, text

    return 1.0, text


def match_food(food_text: str):
    """Best-matching food_db entry for a food name/description, or None."""
    query_tokens = tokenize(food_text)
    if not query_tokens:
        return None

    best_entry = None
    best_score = 0.0
    for entry in _foods():
        variants = [entry["name"], *entry.get("aliases", [])]
        for variant in variants:
            variant_tokens = tokenize(variant)
            if not variant_tokens:
                continue
            overlap = query_tokens & variant_tokens
            if not overlap:
                continue
            query_coverage = len(overlap) / len(query_tokens)
            variant_coverage = len(overlap) / len(variant_tokens)
            if query_coverage < _MIN_TOKEN_COVERAGE or variant_coverage < _MIN_TOKEN_COVERAGE:
                continue
            score = len(overlap) / max(len(query_tokens), len(variant_tokens))
            if score > best_score:
                best_score = score
                best_entry = entry

    if best_entry and best_score >= _MATCH_THRESHOLD:
        return best_entry

    # Nothing in the local dataset matched closely enough. If an Anthropic
    # API key is configured, ask Claude for a one-off estimate and persist
    # it into food_db.json so this (and similarly-worded) dishes resolve
    # locally, with no API call, from now on.
    import ai_nutrition

    if ai_nutrition.is_configured():
        estimate = ai_nutrition.estimate_food(food_text)
        if estimate:
            _learn_food(estimate)
            return estimate

    return None


def parse_smart_entry(raw_text: str) -> list:
    """Parses a comma-separated smart-entry string into per-item results.

    Each result: {raw, matched, food_name, quantity, calories, protein_g,
    carbs_g, fat_g, fiber_g}. Unmatched items have matched=False and null
    macro fields.
    """
    results = []
    for raw_item in raw_text.split(","):
        raw_item = raw_item.strip()
        if not raw_item:
            continue
        quantity, food_text = extract_quantity(raw_item)
        entry = match_food(food_text)
        if entry:
            results.append(
                {
                    "raw": raw_item,
                    "matched": True,
                    "food_name": entry["name"],
                    "quantity": quantity,
                    "calories": round(entry["calories"] * quantity, 1),
                    "protein_g": round(entry["protein_g"] * quantity, 1),
                    "carbs_g": round(entry["carbs_g"] * quantity, 1),
                    "fat_g": round(entry["fat_g"] * quantity, 1),
                    "fiber_g": round(entry["fiber_g"] * quantity, 1),
                }
            )
        else:
            results.append(
                {
                    "raw": raw_item,
                    "matched": False,
                    "food_name": None,
                    "quantity": quantity,
                    "calories": None,
                    "protein_g": None,
                    "carbs_g": None,
                    "fat_g": None,
                    "fiber_g": None,
                }
            )
    return results
