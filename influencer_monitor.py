"""
influencer_monitor.py — Kenya beauty influencer tracking for SkincareIntel.

Monitors a curated list of Kenyan beauty creators across YouTube and Reddit.
For YouTube, it uses the existing youtube_monitor.py API infrastructure but
searches specifically for these creator channels rather than general brand queries.
For content without a YouTube API, it also monitors their RSS feeds where available.

Why this matters: A Kenyan influencer with 50K followers reviewing a brand product
is a far stronger local signal than a global beauty publication mentioning the brand.
Their audiences are the actual consumers brands are trying to reach in Kenya.

influencer_config.json is auto-created with defaults on first run.
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
from urllib.error import HTTPError, URLError

INFLUENCER_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "influencer_config.json"
)

# ── Kenya beauty influencer registry ─────────────────────────────────────────
# Format: name, platform, identifier, approx_reach (for severity weighting)
# YouTube channel IDs can be found at: youtube.com/@handle → view source → "channelId"
# RSS: YouTube channels have public RSS at feeds.google.com/youtube/feeds/...

DEFAULT_INFLUENCERS: List[Dict[str, Any]] = [
    # ── YouTube Kenya Beauty Creators ────────────────────────────
    {
        "name":      "SkincarewithDee",
        "platform":  "youtube",
        "handle":    "SkincarewithDee",
        "channel_id": "",  # populate via YouTube search
        "reach":     "medium",   # low / medium / high / mega
        "tags":      ["skincare", "kenya", "dark skin"],
        "rss":       "",
    },
    {
        "name":      "Vee Beauty Kenya",
        "platform":  "youtube",
        "handle":    "VeeBeautyKenya",
        "channel_id": "",
        "reach":     "medium",
        "tags":      ["makeup", "kenya", "beauty"],
        "rss":       "",
    },
    {
        "name":      "Wanjiru Waweru",
        "platform":  "youtube",
        "handle":    "WanjiruWaweru",
        "channel_id": "",
        "reach":     "medium",
        "tags":      ["lifestyle", "beauty", "nairobi"],
        "rss":       "",
    },
    {
        "name":      "Lyra Beauty",
        "platform":  "youtube",
        "handle":    "LyraBeautyKE",
        "channel_id": "",
        "reach":     "low",
        "tags":      ["skincare", "natural beauty", "kenya"],
        "rss":       "",
    },
    {
        "name":      "Naomy Kwamboka",
        "platform":  "youtube",
        "handle":    "NaomyKwamboka",
        "channel_id": "",
        "reach":     "medium",
        "tags":      ["beauty", "lifestyle", "kenya"],
        "rss":       "",
    },
    # ── Add more influencers here ──────────────────────────────
    # To add: name, platform="youtube", handle="@handle", reach, tags
    # Channel ID optional — leave "" and the monitor will search by name
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "influencers": DEFAULT_INFLUENCERS,
    "published_within_hours": 72,
    "min_views_youtube": 500,     # lower than global threshold — local reach matters
    "reach_severity_map": {
        "low":   1,   # added to base severity
        "medium": 2,
        "high":  3,
        "mega":  4,
    },
    "min_severity_to_include": 3,
}

NEGATIVE_REVIEW_WORDS: List[str] = [
    "broke me out", "breakout", "reaction", "rash", "burn", "irritation",
    "allergic", "returned", "refund", "fake", "counterfeit", "terrible",
    "avoid", "do not buy", "don't buy", "worst", "damaged my skin",
    "recall", "dangerous", "unsafe", "disappointed", "waste of money",
]

POSITIVE_REVIEW_WORDS: List[str] = [
    "holy grail", "repurchase", "amazing", "love", "highly recommend",
    "game changer", "cleared my skin", "transformed", "glowing", "obsessed",
    "best", "favourite", "worth it", "must have", "will buy again",
]


# ── Config loading ────────────────────────────────────────────────────────────

def load_influencer_config(path: str = INFLUENCER_CONFIG_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logging.info(f"Influencer: wrote default config to {path}")
        except Exception:
            pass
        return DEFAULT_CONFIG

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("enabled", True):
            logging.info("Influencer: disabled in influencer_config.json — skipping.")
            return None
        return cfg
    except Exception as e:
        logging.warning(f"Influencer: could not load config — {e}")
        return None


# ── YouTube search for influencer content ─────────────────────────────────────

def _search_youtube_for_influencer(
    influencer: Dict[str, Any],
    brand_name: str,
    api_key: str,
    published_after: str,
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    """Search YouTube for a specific influencer mentioning a specific brand."""
    from urllib.parse import urlencode

    handle = influencer.get("handle", influencer["name"])
    query  = f"{handle} {brand_name}"

    params = {
        "part":              "snippet",
        "q":                 query,
        "type":              "video",
        "order":             "date",
        "maxResults":        max_results,
        "key":               api_key,
        "publishedAfter":    published_after,
        "relevanceLanguage": "en",
        "regionCode":        "KE",
    }
    url = f"https://www.googleapis.com/youtube/v3/search?{urlencode(params)}"

    try:
        req = Request(url, headers={"User-Agent": "SkincareIntel/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("items", [])
    except HTTPError as e:
        if e.code == 403:
            logging.warning("Influencer/YouTube: quota exceeded — stopping.")
            raise
        logging.warning(f"Influencer/YouTube: HTTP {e.code} for query '{query}'")
        return []
    except Exception as e:
        logging.warning(f"Influencer/YouTube: error for '{query}' — {e}")
        return []


def _get_video_views(video_ids: List[str], api_key: str) -> Dict[str, int]:
    """Fetch view counts for a list of video IDs."""
    if not video_ids:
        return {}
    from urllib.parse import urlencode
    params = {"part": "statistics", "id": ",".join(video_ids), "key": api_key}
    url = f"https://www.googleapis.com/youtube/v3/videos?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": "SkincareIntel/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            item["id"]: int(item.get("statistics", {}).get("viewCount", 0))
            for item in data.get("items", [])
        }
    except Exception as e:
        logging.warning(f"Influencer/YouTube: stats fetch error — {e}")
        return {}


# ── Content sentiment scoring ─────────────────────────────────────────────────

def _score_content_sentiment(title: str, description: str) -> Tuple[str, int]:
    """
    Returns (event_type, sentiment_severity_bump).
    Looks at title + description for review sentiment signals.
    """
    text = f"{title} {description}".lower()
    neg_hits = sum(1 for w in NEGATIVE_REVIEW_WORDS if w in text)
    pos_hits = sum(1 for w in POSITIVE_REVIEW_WORDS if w in text)

    if neg_hits > pos_hits:
        return "negative", min(neg_hits, 3)
    elif pos_hits > 0:
        return "positive", 0
    else:
        return "neutral", 0


def _reach_bump(reach: str, reach_map: Dict[str, int]) -> int:
    return reach_map.get(reach, 1)


# ── RSS feed monitoring (fallback for influencers with public feeds) ───────────

def _fetch_influencer_rss(rss_url: str, brand_names: List[str], hours_back: int) -> List[Dict[str, Any]]:
    """Parse an influencer's RSS/Atom feed for brand mentions."""
    import xml.etree.ElementTree as ET

    try:
        req = Request(rss_url, headers={"User-Agent": "SkincareIntel/1.0"})
        with urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
    except Exception as e:
        logging.warning(f"Influencer RSS: could not fetch {rss_url} — {e}")
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    results = []

    channel = root.find("channel")
    entries = (channel.findall("item") if channel is not None
               else root.findall("{http://www.w3.org/2005/Atom}entry"))

    for entry in entries:
        if channel is not None:
            title = (entry.findtext("title") or "").strip()
            link  = (entry.findtext("link") or "").strip()
            pub   = (entry.findtext("pubDate") or "").strip()
            desc  = (entry.findtext("description") or "").strip()
        else:
            ns    = "http://www.w3.org/2005/Atom"
            title = (entry.findtext(f"{{{ns}}}title") or "").strip()
            link_el = entry.find(f"{{{ns}}}link")
            link  = (link_el.attrib.get("href", "") if link_el is not None else "")
            pub   = (entry.findtext(f"{{{ns}}}updated") or "").strip()
            desc  = (entry.findtext(f"{{{ns}}}summary") or "").strip()

        text = f"{title} {desc}".lower()
        matched_brand = next((b for b in brand_names if b.lower() in text), None)
        if not matched_brand:
            continue

        results.append({
            "title":        title,
            "link":         link,
            "published":    pub,
            "description":  desc,
            "brand":        matched_brand,
        })

    return results


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_influencer_items(
    brands: List[Any],
    youtube_api_key: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    Called from agent.py main(). Returns ScoredItems for influencer content
    mentioning monitored brands.

    Pass youtube_api_key from youtube_config.json for YouTube search.
    Influencers with RSS feeds work without an API key.
    """
    from agent import ScoredItem, Item

    inf_cfg = load_influencer_config()
    if not inf_cfg:
        return []

    influencers      = inf_cfg.get("influencers", DEFAULT_INFLUENCERS)
    hours_back       = int(inf_cfg.get("published_within_hours", 72))
    min_views        = int(inf_cfg.get("min_views_youtube", 500))
    reach_map        = inf_cfg.get("reach_severity_map", DEFAULT_CONFIG["reach_severity_map"])
    min_sev          = int(inf_cfg.get("min_severity_to_include", 3))
    brand_names      = [b.name for b in brands]

    published_after = (
        datetime.now(timezone.utc) - timedelta(hours=hours_back)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    results: List[ScoredItem] = []
    seen_ids: set = set()
    quota_exceeded = False

    for influencer in influencers:
        platform = influencer.get("platform", "youtube")
        reach    = influencer.get("reach", "low")
        inf_name = influencer["name"]

        # ── YouTube monitoring ────────────────────────────────────
        if platform == "youtube" and youtube_api_key and not quota_exceeded:
            for brand in brands:
                if quota_exceeded:
                    break
                try:
                    videos = _search_youtube_for_influencer(
                        influencer, brand.name, youtube_api_key,
                        published_after, max_results=3
                    )
                except HTTPError:
                    quota_exceeded = True
                    break

                if not videos:
                    continue

                video_ids = [v.get("id", {}).get("videoId", "") for v in videos if v.get("id", {}).get("videoId")]
                views_map = _get_video_views(video_ids, youtube_api_key)

                for video in videos:
                    vid_id  = video.get("id", {}).get("videoId", "")
                    if not vid_id or vid_id in seen_ids:
                        continue
                    seen_ids.add(vid_id)

                    snippet     = video.get("snippet", {})
                    title       = snippet.get("title", "")
                    description = snippet.get("description", "")
                    published   = snippet.get("publishedAt", "")
                    channel     = snippet.get("channelTitle", "")

                    # Only include if the influencer's name/handle is in the channel
                    handle = influencer.get("handle", "").lower()
                    if handle and handle not in channel.lower() and handle not in title.lower():
                        continue  # This video isn't from our influencer — skip

                    view_count = views_map.get(vid_id, 0)
                    if view_count < min_views:
                        continue

                    event_type, sentiment_bump = _score_content_sentiment(title, description)
                    base_severity = 2 + _reach_bump(reach, reach_map) + sentiment_bump

                    # Engagement boost
                    if view_count >= 100_000: base_severity += 2
                    elif view_count >= 10_000: base_severity += 1

                    severity = max(1, min(10, base_severity))
                    if severity < min_sev:
                        continue

                    link = f"https://www.youtube.com/watch?v={vid_id}"

                    if event_type == "negative":
                        risk_type      = "Influencer Negative Review"
                        impact_area    = "reputation"
                        why_it_matters = (
                            f"{inf_name} has a Kenyan beauty audience. A negative review "
                            "directly reaches your target market and can deter purchase intent."
                        )
                    else:
                        risk_type      = "Influencer Coverage"
                        impact_area    = "marketing"
                        why_it_matters = (
                            f"{inf_name} is a Kenyan beauty creator. This coverage reaches "
                            "local consumers and drives market awareness."
                        )

                    item = Item(
                        title=f"[{inf_name}] {title}",
                        link=link,
                        published=published,
                        source="https://www.youtube.com",
                        summary=(
                            f"📱 Kenya Influencer: {inf_name} ({reach} reach) · "
                            f"{view_count:,} views · {description[:200]}"
                        ),
                    )
                    si = ScoredItem(
                        item=item,
                        brand_hit=brand.name,
                        alias_matched=brand.name,
                        keyword_hits=[],
                        severity=severity,
                        impact_area=impact_area,
                        risk_type=risk_type,
                        reasons=[
                            f"Kenya influencer: {inf_name} ({reach} reach) mentioned {brand.name}",
                            f"YouTube views: {view_count:,} · Sentiment: {event_type}",
                            f"Severity: {severity}/10",
                        ],
                        lane="brand",
                        event_type=event_type,
                        why_it_matters=why_it_matters,
                        matched_areas={impact_area: 1},
                    )
                    results.append(si)
                    logging.info(
                        f"Influencer: [{severity}/10] {event_type.upper()} — "
                        f"{inf_name} → {brand.name}: {title[:60]} ({view_count:,} views)"
                    )
                time.sleep(0.3)

        # ── RSS fallback ──────────────────────────────────────────
        rss_url = influencer.get("rss", "")
        if rss_url:
            rss_items = _fetch_influencer_rss(rss_url, brand_names, hours_back)
            for rss_item in rss_items:
                uid = f"rss_{rss_item['link']}"
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)

                event_type, sentiment_bump = _score_content_sentiment(
                    rss_item["title"], rss_item["description"]
                )
                severity = max(1, min(10, 2 + _reach_bump(reach, reach_map) + sentiment_bump))
                if severity < min_sev:
                    continue

                matched_brand_name = rss_item["brand"]
                item = Item(
                    title=f"[{inf_name}] {rss_item['title']}",
                    link=rss_item["link"],
                    published=rss_item["published"],
                    source=rss_url,
                    summary=f"📱 Kenya Influencer: {inf_name} · {rss_item['description'][:200]}",
                )
                si = ScoredItem(
                    item=item,
                    brand_hit=matched_brand_name,
                    alias_matched=matched_brand_name,
                    keyword_hits=[],
                    severity=severity,
                    impact_area="marketing",
                    risk_type="Influencer Coverage",
                    reasons=[
                        f"Kenya influencer: {inf_name} mentioned {matched_brand_name} (RSS)",
                        f"Sentiment: {event_type}",
                    ],
                    lane="brand",
                    event_type=event_type,
                    why_it_matters=f"{inf_name} covered {matched_brand_name} on their channel.",
                    matched_areas={"marketing": 1},
                )
                results.append(si)

    logging.info(f"Influencer: {len(results)} item(s) from {len(influencers)} influencer(s).")
    return results