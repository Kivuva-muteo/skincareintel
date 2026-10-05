"""
weekly_state.py — Accumulates daily scored items into a rolling weekly log.
Saves to weekly_state.json next to agent.py. Resets automatically each Monday.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

WEEKLY_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "weekly_state.json"
)


# -------------------------
# Serialise / deserialise ScoredItem
# -------------------------

def scored_item_to_dict(si: Any) -> Dict[str, Any]:
    """Convert a ScoredItem dataclass to a plain dict for JSON storage."""
    return {
        "title":         si.item.title,
        "link":          si.item.link,
        "published":     si.item.published,
        "source":        si.item.source,
        "summary":       si.item.summary,
        "brand_hit":     si.brand_hit,
        "alias_matched": si.alias_matched,
        "keyword_hits":  si.keyword_hits,
        "severity":      si.severity,
        "impact_area":   si.impact_area,
        "risk_type":     si.risk_type,
        "reasons":       si.reasons,
        "lane":          si.lane,
        "event_type":    si.event_type,
        "why_it_matters": si.why_it_matters,
        "matched_areas": si.matched_areas,
        "recorded_at":   datetime.now(timezone.utc).isoformat(),
    }


def dict_to_scored_item(d: Dict[str, Any]) -> Any:
    """Reconstruct a ScoredItem from a stored dict."""
    from agent import ScoredItem, Item
    item = Item(
        title=d.get("title", ""),
        link=d.get("link", ""),
        published=d.get("published", ""),
        source=d.get("source", ""),
        summary=d.get("summary", ""),
    )
    return ScoredItem(
        item=item,
        brand_hit=d.get("brand_hit"),
        alias_matched=d.get("alias_matched"),
        keyword_hits=d.get("keyword_hits", []),
        severity=d.get("severity", 1),
        impact_area=d.get("impact_area", "general"),
        risk_type=d.get("risk_type", "General Mention"),
        reasons=d.get("reasons", []),
        lane=d.get("lane", "brand"),
        event_type=d.get("event_type", "neutral"),
        why_it_matters=d.get("why_it_matters", ""),
        matched_areas=d.get("matched_areas", {}),
    )


# -------------------------
# Load / save weekly state
# -------------------------

def _current_week_key() -> str:
    """ISO week string e.g. '2026-W10'"""
    now = datetime.now(timezone.utc)
    return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"


def load_weekly_state(path: str = WEEKLY_STATE_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"week": _current_week_key(), "runs": [], "items": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Auto-reset if we've rolled into a new week
        if state.get("week") != _current_week_key():
            logging.info(f"Weekly state: new week detected — resetting (was {state.get('week')}).")
            return {"week": _current_week_key(), "runs": [], "items": []}
        return state
    except Exception as e:
        logging.warning(f"Weekly state: could not load — {e}")
        return {"week": _current_week_key(), "runs": [], "items": []}


def save_weekly_state(state: Dict[str, Any], path: str = WEEKLY_STATE_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logging.warning(f"Weekly state: could not save — {e}")


# -------------------------
# Record a daily run
# -------------------------

def record_daily_run(
    scored_items: List[Any],
    total_fetched: int,
    feed_health: Dict[str, Dict[str, Any]],
    path: str = WEEKLY_STATE_PATH,
) -> None:
    """
    Called from agent.py main() after each run.
    Appends today's scored items and run metadata to the weekly state.
    Deduplicates by title+link so re-runs don't inflate counts.
    """
    state = load_weekly_state(path)

    # Track existing links to avoid duplicates across runs
    existing_links = {item["link"] for item in state.get("items", [])}

    new_items = []
    for si in scored_items:
        if si.item.link and si.item.link in existing_links:
            continue
        existing_links.add(si.item.link)
        new_items.append(scored_item_to_dict(si))

    state["items"] = state.get("items", []) + new_items

    # Record run metadata
    feeds_ok    = sum(1 for v in feed_health.values() if v.get("ok"))
    feeds_total = len(feed_health)
    run_record  = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "total_fetched": total_fetched,
        "scored":       len(scored_items),
        "new_added":    len(new_items),
        "feeds_ok":     feeds_ok,
        "feeds_total":  feeds_total,
    }
    state["runs"] = state.get("runs", []) + [run_record]

    save_weekly_state(state, path)
    logging.info(
        f"Weekly state: added {len(new_items)} new item(s) "
        f"({len(state['items'])} total this week)."
    )


# -------------------------
# Read weekly data for report
# -------------------------

def get_weekly_items(path: str = WEEKLY_STATE_PATH) -> List[Any]:
    """Return all ScoredItems accumulated this week."""
    state = load_weekly_state(path)
    items = []
    for d in state.get("items", []):
        try:
            items.append(dict_to_scored_item(d))
        except Exception as e:
            logging.debug(f"Weekly state: skipping malformed item — {e}")
    return items


def get_weekly_runs(path: str = WEEKLY_STATE_PATH) -> List[Dict[str, Any]]:
    """Return run metadata list for the current week."""
    state = load_weekly_state(path)
    return state.get("runs", [])


def get_week_label(path: str = WEEKLY_STATE_PATH) -> str:
    state = load_weekly_state(path)
    return state.get("week", _current_week_key())