"""
youtube_monitor.py — YouTube brand monitoring for SkincareIntel
Searches YouTube Data API v3 for brand mentions, scores videos by
sentiment/engagement, and returns ScoredItems compatible with the
main agent pipeline (HTML report + Telegram + email).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

# -------------------------
# Constants
# -------------------------

YOUTUBE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_config.json")
YOUTUBE_SEARCH_URL  = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEO_URL   = "https://www.googleapis.com/youtube/v3/videos"

# Keyword buckets for sentiment scoring
NEGATIVE_SIGNALS: List[str] = [
    "recall", "banned", "dangerous", "unsafe", "lawsuit", "scam", "fake",
    "fraud", "toxic", "harmful", "reaction", "allergy", "burn", "rash",
    "side effect", "contamination", "cancer", "warning", "avoid", "horrible",
    "worst", "terrible", "boycott", "complaint", "disgusting", "broke out",
    "skin damage", "investigation", "fine", "penalty",
]

POSITIVE_SIGNALS: List[str] = [
    "launch", "new product", "collab", "collaboration", "partnership",
    "review", "unboxing", "haul", "first impression", "favourite", "favorite",
    "best", "love", "amazing", "holy grail", "game changer", "must have",
    "award", "innovation", "expansion", "campaign",
]

INFLUENCER_SIGNALS: List[str] = [
    "review", "unboxing", "haul", "try", "test", "routine", "honest",
    "first impression", "sponsored", "gifted", "ad", "collab",
]

BEAUTY_SCOPE: List[str] = [
    "skincare", "skin care", "beauty", "makeup", "cosmetic", "moisturizer",
    "serum", "cleanser", "sunscreen", "lotion", "cream", "foundation",
    "review", "routine", "haul", "unboxing", "skin", "face", "hair care",
]


# Some brand names are ordinary English phrases. These need stronger product
# context so search-engine noise is not mistaken for a real brand mention.
AMBIGUOUS_BRAND_CUES: Dict[str, List[str]] = {
    "the ordinary": [
        "skincare", "skin care", "serum", "niacinamide", "retinol",
        "glycolic", "salicylic", "hyaluronic", "deciem", "peeling solution",
        "moisturizer", "cleanser", "sunscreen", "cosmetic",
    ],
}


def _alias_regex(alias: str) -> re.Pattern[str]:
    """Compile a conservative, punctuation-tolerant alias matcher."""
    tokens = re.findall(r"[a-z0-9]+", alias.lower())
    if not tokens:
        return re.compile(r"a^")
    return re.compile(r"(?<![a-z0-9])" + r"[\s'’._-]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])", re.I)


def _match_brand_alias(text: str, brand_name: str, aliases: List[str]) -> Optional[str]:
    candidates: List[str] = []
    for value in [brand_name, *aliases]:
        value = (value or "").strip()
        if value and value.lower() not in {v.lower() for v in candidates}:
            candidates.append(value)

    lower_text = text.lower()
    for alias in sorted(candidates, key=len, reverse=True):
        if not _alias_regex(alias).search(text):
            continue
        cues = AMBIGUOUS_BRAND_CUES.get(alias.lower()) or AMBIGUOUS_BRAND_CUES.get(brand_name.lower())
        if cues and not any(cue in lower_text for cue in cues):
            continue
        return alias
    return None


# -------------------------
# Config
# -------------------------

def load_youtube_config(path: str = YOUTUBE_CONFIG_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_key = os.environ.get("YOUTUBE_API_KEY", cfg.get("api_key", "")).strip()
        if not cfg.get("enabled", True) and not api_key:
            logging.info("YouTube: disabled in youtube_config.json — skipping.")
            return None
        if not api_key.startswith("AIza"):
            logging.warning("YouTube: API key missing/invalid (YOUTUBE_API_KEY or youtube_config.json) — skipping.")
            return None
        cfg["api_key"] = api_key
        return cfg
    except Exception as e:
        logging.warning(f"YouTube: could not load config — {e}")
        return None


# -------------------------
# API helpers
# -------------------------

def _get(url: str, params: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    full_url = f"{url}?{urlencode(params)}"
    req = Request(full_url, headers={"User-Agent": "SkincareIntel/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_videos(
    query: str,
    api_key: str,
    max_results: int = 3,
    published_after: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search YouTube and return a list of video stubs."""
    params: Dict[str, Any] = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "relevance",
        "maxResults": max_results,
        "key": api_key,
        "relevanceLanguage": "en",
        "safeSearch": "none",
    }
    if published_after:
        params["publishedAfter"] = published_after

    try:
        data = _get(YOUTUBE_SEARCH_URL, params)
        return data.get("items", [])
    except HTTPError as e:
        if e.code == 403:
            logging.warning("YouTube: API quota exceeded — stopping YouTube searches for this run.")
            raise
        else:
            logging.warning(f"YouTube: search HTTP {e.code} for query '{query}'")
        return []
    except Exception as e:
        logging.warning(f"YouTube: search error for '{query}' — {e}")
        return []


def get_video_stats(video_ids: List[str], api_key: str) -> Dict[str, Dict[str, Any]]:
    """Fetch view/like/comment counts for a list of video IDs."""
    if not video_ids:
        return {}
    params = {
        "part": "statistics,snippet",
        "id": ",".join(video_ids),
        "key": api_key,
    }
    try:
        data = _get(YOUTUBE_VIDEO_URL, params)
        result = {}
        for item in data.get("items", []):
            vid_id = item["id"]
            stats  = item.get("statistics", {})
            result[vid_id] = {
                "viewCount":    int(stats.get("viewCount",    0)),
                "likeCount":    int(stats.get("likeCount",    0)),
                "commentCount": int(stats.get("commentCount", 0)),
            }
        return result
    except Exception as e:
        logging.warning(f"YouTube: stats fetch error — {e}")
        return {}


# -------------------------
# Scoring
# -------------------------

def _count_signals(text: str, signals: List[str]) -> int:
    t = text.lower()
    return sum(1 for s in signals if s in t)


def _engagement_bump(view_count: int) -> int:
    """Extra severity based on how viral a video is."""
    if view_count >= 1_000_000: return 3
    if view_count >= 100_000:   return 2
    if view_count >= 10_000:    return 1
    return 0


def _recency_bump(published_at: str) -> int:
    """Bonus for very recent videos (within 24h)."""
    try:
        pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
        if age_hours <= 24:
            return 1
    except Exception:
        pass
    return 0


def score_video(
    video: Dict[str, Any],
    stats: Dict[str, Any],
    brand_name: str,
    brand_aliases: List[str],
    min_views: int,
) -> Optional[Dict[str, Any]]:
    """
    Score a single YouTube video. Returns a dict with all fields needed
    to build a ScoredItem, or None if the video should be skipped.
    """
    snippet     = video.get("snippet", {})
    video_id    = video.get("id", {}).get("videoId", "")
    title       = snippet.get("title", "")
    description = snippet.get("description", "")
    channel     = snippet.get("channelTitle", "")
    published   = snippet.get("publishedAt", "")
    searchable_text = f"{title} {description} {channel}"
    text        = searchable_text.lower()

    matched_alias = _match_brand_alias(searchable_text, brand_name, brand_aliases)
    if not matched_alias:
        return None

    view_count = stats.get("viewCount", 0)
    if view_count < min_views:
        return None

    # Must contain at least one beauty/skincare signal to be relevant
    if not any(w in text for w in BEAUTY_SCOPE):
        return None

    neg_hits = _count_signals(text, NEGATIVE_SIGNALS)
    pos_hits = _count_signals(text, POSITIVE_SIGNALS)
    inf_hits = _count_signals(text, INFLUENCER_SIGNALS)

    # Determine event type
    if neg_hits > pos_hits:
        event_type = "negative"
        risk_type  = "Consumer Safety" if neg_hits >= 2 else "Reputation Risk"
        base       = 3 + neg_hits
    elif inf_hits >= 2:
        event_type = "positive"
        risk_type  = "Influencer Content"
        base       = 2 + pos_hits
    elif pos_hits >= 1:
        event_type = "positive"
        risk_type  = "Brand / Product Update"
        base       = 2 + pos_hits
    else:
        # Generic mention — only keep if decent views
        if view_count < 50_000:
            return None
        event_type = "neutral"
        risk_type  = "Brand Mention"
        base       = 2

    severity = min(10, base + _engagement_bump(view_count) + _recency_bump(published))

    # Impact area
    if event_type == "negative":
        impact_area    = "reputation"
        why_it_matters = "Negative YouTube content can spread rapidly and damage brand perception."
    elif risk_type == "Influencer Content":
        impact_area    = "marketing"
        why_it_matters = "Influencer coverage drives purchase decisions in the beauty space."
    else:
        impact_area    = "marketing"
        why_it_matters = "YouTube product coverage reaches high-intent beauty consumers."

    views_fmt = f"{view_count:,}"

    return {
        "video_id":       video_id,
        "title":          title,
        "channel":        channel,
        "published":      published,
        "link":           f"https://www.youtube.com/watch?v={video_id}",
        "summary":        f"📺 YouTube · {channel} · {views_fmt} views · {description[:200]}",
        "brand_hit":      brand_name,
        "alias_matched":  matched_alias,
        "severity":       severity,
        "event_type":     event_type,
        "risk_type":      risk_type,
        "impact_area":    impact_area,
        "why_it_matters": why_it_matters,
        "view_count":     view_count,
        "keyword_hits":   [s for s in NEGATIVE_SIGNALS + POSITIVE_SIGNALS if s in text],
        "reasons": [
            f"YouTube: {brand_name} mention by {channel} (matched: {matched_alias})",
            f"Views: {views_fmt}  |  Neg signals: {neg_hits}  |  Pos signals: {pos_hits}",
            f"Severity: {severity}/10",
        ],
    }


# -------------------------
# Main entry point
# -------------------------

def fetch_youtube_items(
    brands: List[Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    Called from agent.py main(). Returns a list of ScoredItems from YouTube.
    Stops immediately if quota is exceeded to avoid spamming warnings.
    """
    from agent import ScoredItem, Item

    yt_cfg = load_youtube_config()
    if not yt_cfg:
        return []

    api_key    = yt_cfg["api_key"]
    max_res    = int(yt_cfg.get("max_results_per_brand", 3))
    hours_back = int(yt_cfg.get("published_within_hours", 72))
    min_views  = int(yt_cfg.get("min_view_count", 1000))
    queries    = yt_cfg.get("search_queries", [
        "{brand} skincare review",
        "{brand} recall controversy",
    ])

    # Only use first 2 queries to stay well within quota
    queries = queries[:2]

    published_after = (
        datetime.now(timezone.utc) - timedelta(hours=hours_back)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    results: List[ScoredItem] = []
    seen_video_ids: set = set()
    quota_exceeded = False

    for brand in brands:
        if quota_exceeded:
            break

        brand_name = brand.name
        logging.info(f"YouTube: searching for '{brand_name}'...")

        for query_template in queries:
            if quota_exceeded:
                break

            query = query_template.replace("{brand}", brand_name)

            try:
                videos = search_videos(
                    query=query,
                    api_key=api_key,
                    max_results=max_res,
                    published_after=published_after,
                )
            except HTTPError:
                quota_exceeded = True
                logging.warning("YouTube: quota exceeded — stopping early, will resume tomorrow.")
                break

            if not videos:
                continue

            video_ids = [v.get("id", {}).get("videoId", "") for v in videos if v.get("id", {}).get("videoId")]
            stats_map = get_video_stats(video_ids, api_key)

            for video in videos:
                vid_id = video.get("id", {}).get("videoId", "")
                if not vid_id or vid_id in seen_video_ids:
                    continue
                scored = score_video(
                    video=video,
                    stats=stats_map.get(vid_id, {}),
                    brand_name=brand_name,
                    brand_aliases=list(brand.aliases or []),
                    min_views=min_views,
                )
                if not scored:
                    continue
                seen_video_ids.add(vid_id)

                item = Item(
                    title=scored["title"],
                    link=scored["link"],
                    published=scored["published"],
                    source="https://www.youtube.com",
                    summary=scored["summary"],
                )

                si = ScoredItem(
                    item=item,
                    brand_hit=scored["brand_hit"],
                    alias_matched=scored["alias_matched"],
                    keyword_hits=scored["keyword_hits"],
                    severity=scored["severity"],
                    impact_area=scored["impact_area"],
                    risk_type=scored["risk_type"],
                    reasons=scored["reasons"],
                    lane="brand",
                    event_type=scored["event_type"],
                    why_it_matters=scored["why_it_matters"],
                    matched_areas={scored["impact_area"]: 1},
                )
                results.append(si)
                logging.info(
                    f"YouTube: [{si.severity}/10] {si.event_type.upper()} — "
                    f"{scored['title'][:60]} ({scored['view_count']:,} views)"
                )

            time.sleep(0.2)

    logging.info(f"YouTube: {len(results)} video(s) scored across {len(brands)} brand(s).")
    return results