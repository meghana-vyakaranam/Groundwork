"""AI-assisted macro estimation for foods (especially Indian dishes) that
aren't in the local curated data/food_db.json dataset.

Used only as a fallback: nutrition.match_food() first tries the fast, free,
offline token-overlap match against food_db.json. If that fails and
ANTHROPIC_API_KEY is set, we ask Claude to estimate a typical serving's
macros for the dish, then persist the result into food_db.json so the same
(or a similarly-worded) dish resolves locally — no API call — every time
after. If no API key is configured, this module is a no-op and the app
falls back to the existing "couldn't estimate" flag.

Two-step estimation:
1. Research (`_research_food`) — Claude gets the server-side `web_search`
   tool and is told to use it for anything it isn't confident estimating
   from memory alone (uncommon regional dishes, specific restaurant/brand
   items, packaged foods with a printed label, etc.). Common home-style
   dishes can still be answered from memory — search is judgment-based, not
   forced on every call, so a plain "1 apple" doesn't pay web-search latency.
2. Extraction (`estimate_food`) — a second, tool-free call turns whatever
   came out of step 1 (search-grounded findings, or just memory if research
   failed/was skipped) into the strict `_FoodEstimate` schema via structured
   output. Splitting it this way keeps the structured-output call simple and
   means a research failure degrades to the old memory-only behavior instead
   of losing the estimate entirely.
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

_MODEL = "claude-opus-4-7"

_RESEARCH_SYSTEM_PROMPT = (
    "You are a nutrition-research assistant inside a food-logging app. Given a short, "
    "possibly messy or misspelled description of a dish (often an Indian dish — curries, "
    "breads, snacks, sweets, thalis, regional specialties, etc., but sometimes other "
    "cuisines too), figure out the macros for ONE typical single serving as eaten at home "
    "or a restaurant. Correct obvious typos in the dish name yourself (e.g. 'aloo "
    "tiki=ki sandwich' is 'aloo tikki sandwich').\n\n"
    "If this is a common, well-known dish you're confident estimating from your own "
    "nutrition knowledge, just answer directly — no need to search. If it's an uncommon "
    "or regional dish, a specific restaurant or packaged/branded item, or you're not "
    "confident in your memory, use the web_search tool to find real nutrition data "
    "(nutrition labels, verified recipe or nutrition sites, USDA FoodData Central, etc.) "
    "before answering, rather than guessing.\n\n"
    "Report: calories, protein (g), carbs (g), fat (g), and fiber (g) for one serving; "
    "what one serving means (e.g. '1 cup', '2 pieces'); a clean canonical dish name with "
    "typos corrected; and a couple of common alternate spellings/names."
)

_SYSTEM_PROMPT = (
    "You are a nutrition-estimation assistant inside a food-logging app. You'll be given "
    "either raw notes/findings about a dish (possibly from a web search) or just a short "
    "dish description. Extract your best reasonable estimate of the macros for ONE typical "
    "single serving into the given schema. If notes/findings are provided, prefer the "
    "numbers in them over your own memory. Use your general nutrition knowledge to fill any "
    "gaps — these are approximations, not lab measurements, which is fine for this use "
    "case. Always return a value for every field; never refuse."
)


class _FoodEstimate(BaseModel):
    name: str = Field(description="Clean, canonical dish name (typos corrected)")
    unit: str = Field(description="What one serving means, e.g. '1 cup', '2 pieces'")
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float
    aliases: list[str] = Field(
        default_factory=list,
        description="Other common spellings/names for this dish, including the original raw input text",
    )


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _research_food(food_text: str, client) -> Optional[str]:
    """Best-effort web-search-grounded research pass. Returns Claude's
    findings as plain text, or None if the call fails or turns up nothing
    — callers should fall back to a memory-only estimate in that case, not
    fail the whole lookup."""
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_RESEARCH_SYSTEM_PROMPT,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": f"Dish: {food_text}"}],
        )
    except Exception:
        return None

    text_blocks = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_blocks).strip() or None


def estimate_food(food_text: str) -> Optional[dict]:
    """Estimate macros for a single food description, searching the web
    first for dishes Claude isn't confident estimating from memory alone.

    Returns a food_db-shaped dict on success, or None if unconfigured or
    the call fails for any reason (network, auth, parsing, etc.) — callers
    should treat None exactly like "no match found".
    """
    if not is_configured():
        return None

    try:
        import anthropic
    except ImportError:
        return None

    try:
        client = anthropic.Anthropic()
        research = _research_food(food_text, client)
        extraction_input = research or f"Dish: {food_text}"

        response = client.messages.parse(
            model=_MODEL,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": extraction_input}],
            output_format=_FoodEstimate,
        )
        estimate = response.parsed_output
        if estimate is None:
            return None
    except Exception:
        # Any failure (offline, bad key, rate limit, refusal, etc.) just
        # falls back to the existing unmatched-food behavior.
        return None

    aliases = list(dict.fromkeys([*estimate.aliases, food_text.strip().lower()]))
    return {
        "name": estimate.name.strip(),
        "unit": estimate.unit.strip(),
        "calories": round(estimate.calories, 1),
        "protein_g": round(estimate.protein_g, 1),
        "carbs_g": round(estimate.carbs_g, 1),
        "fat_g": round(estimate.fat_g, 1),
        "fiber_g": round(estimate.fiber_g, 1),
        "aliases": aliases,
        "ai_estimated": True,
    }
