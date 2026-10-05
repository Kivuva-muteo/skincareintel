"""
agent.py — SkincareIntel main orchestrator.

Pipeline (each run):
  1. Load config + seen-IDs state
  2. Fetch RSS/Atom feeds  →  score items (brand + industry lanes)
  3. YouTube monitoring    →  scored items
  4. Reddit sentiment      →  scored items
  5. Kenya influencer      →  scored items
  6. Kenya regulatory      →  scored items
  7. AI verification       →  prune false-positives (optional)
  8. Cross-feed dedup      →  remove near-duplicates
  9. Generate HTML report
 10. Telegram alerts
 11. Slack alerts
 12. Email delivery
 13. Record to weekly state
 14. Friday: generate weekly PDF + competitor dashboard

Run daily via cron/Task Scheduler. See run.bat for Windows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH     = os.path.join(BASE_DIR, "config.json")
STATE_PATH      = os.path.join(BASE_DIR, "state.json")
AI_CONFIG_PATH  = os.path.join(BASE_DIR, "ai_config.json")
TG_CONFIG_PATH  = os.path.join(BASE_DIR, "telegram_config.json")
EMAIL_CFG_PATH  = os.path.join(BASE_DIR, "email_config.json")
SLACK_CFG_PATH  = os.path.join(BASE_DIR, "slack_monitor.py")   # actually JSON content
REPORTS_DIR     = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, "agent.log"), encoding="utf-8"),
    ],
)

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Item:
    title:     str
    link:      str
    published: str
    source:    str
    summary:   str = ""


@dataclass
class Brand:
    name:     str
    aliases:  List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)


@dataclass
class ScoredItem:
    item:          Item
    brand_hit:     Optional[str]
    alias_matched: Optional[str]
    keyword_hits:  List[str]
    severity:      int                      # 1–10
    impact_area:   str
    risk_type:     str
    reasons:       List[str]
    lane:          str                      # "brand" | "industry"
    event_type:    str                      # "negative" | "positive" | "neutral"
    why_it_matters: str = ""
    matched_areas: Dict[str, int] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Optional module imports
# ─────────────────────────────────────────────────────────────────────────────

try:
    from youtube_monitor import fetch_youtube_items as _fetch_youtube
    _YOUTUBE_AVAILABLE = True
except ImportError:
    _YOUTUBE_AVAILABLE = False

try:
    from reddit_monitor import fetch_reddit_sentiment_items as _fetch_reddit
    _REDDIT_AVAILABLE = True
except Exception as e:
    logging.warning(f"Reddit module unavailable — {e}")
    _REDDIT_AVAILABLE = False

try:
    from influencer_monitor import fetch_influencer_items as _fetch_influencers
    _INFLUENCER_AVAILABLE = True
except ImportError:
    _INFLUENCER_AVAILABLE = False

try:
    from kenya_regulatory_monitor import fetch_regulatory_items as _fetch_regulatory
    _REGULATORY_AVAILABLE = True
except ImportError:
    _REGULATORY_AVAILABLE = False

try:
    from instagram_monitor import fetch_instagram_items as _fetch_instagram
    _INSTAGRAM_AVAILABLE = True
except Exception as e:
    logging.warning(f"Instagram/Social module unavailable — {e}")
    _INSTAGRAM_AVAILABLE = False

try:
    from weekly_state import (
        record_daily_run, get_weekly_items,
        get_weekly_runs, get_week_label,
    )
    _WEEKLY_AVAILABLE = True
except ImportError:
    _WEEKLY_AVAILABLE = False

try:
    from weekly_report import maybe_generate_weekly_report
    _WEEKLY_REPORT_AVAILABLE = True
except ImportError:
    _WEEKLY_REPORT_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Config & state helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_brands(cfg: Dict[str, Any]) -> List[Brand]:
    return [
        Brand(
            name=b["name"],
            aliases=b.get("aliases", []),
            keywords=b.get("keywords", []),
        )
        for b in cfg.get("brands", [])
    ]


def load_state(path: str = STATE_PATH) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except Exception:
        return set()


def save_state(seen_ids: Set[str], path: str = STATE_PATH) -> None:
    # Keep only the most recent 5 000 IDs to avoid unbounded growth
    ids_list = list(seen_ids)[-5000:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": ids_list}, f)


def _item_id(item: Item) -> str:
    raw = f"{item.link}|{item.title}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_ai_config(path: str = AI_CONFIG_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_key = os.environ.get("ANTHROPIC_API_KEY", cfg.get("api_key", "")).strip()
        if not cfg.get("enabled", False) and not api_key:
            return None
        if not api_key.startswith("sk-ant"):
            return None
        cfg["api_key"] = api_key
        return cfg
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RSS fetching
# ─────────────────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 25, retries: int = 3,
               backoff: int = 2) -> Optional[bytes]:
    headers = {"User-Agent": "Mozilla/5.0 (SkincareMonitor/1.0)"}
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as e:
            logging.warning(f"HTTP {e.code} fetching {url}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
            else:
                logging.warning(f"Failed to fetch {url} — {e}")
    return None


def _parse_feed(raw: bytes, source_url: str) -> List[Item]:
    """Parse RSS or Atom bytes into a list of Items."""
    items: List[Item] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logging.debug(f"XML parse error for {source_url}: {e}")
        return items

    ns_atom = "http://www.w3.org/2005/Atom"

    # Atom feed
    if root.tag == f"{{{ns_atom}}}feed" or "feed" in root.tag.lower():
        for entry in root.findall(f"{{{ns_atom}}}entry"):
            title = (entry.findtext(f"{{{ns_atom}}}title") or "").strip()
            link_el = entry.find(f"{{{ns_atom}}}link")
            link = ""
            if link_el is not None:
                link = link_el.attrib.get("href", "")
                if not link:
                    link = link_el.text or ""
            pub = (entry.findtext(f"{{{ns_atom}}}updated")
                   or entry.findtext(f"{{{ns_atom}}}published") or "").strip()
            summary = (entry.findtext(f"{{{ns_atom}}}summary")
                       or entry.findtext(f"{{{ns_atom}}}content") or "").strip()
            # Strip HTML tags from summary
            summary = re.sub(r"<[^>]+>", " ", summary)[:500]
            if title and link:
                items.append(Item(title=title, link=link, published=pub,
                                  source=source_url, summary=summary))
        return items

    # RSS feed
    channel = root.find("channel")
    if channel is None:
        channel = root  # some feeds omit <channel>
    for entry in channel.findall("item"):
        title   = (entry.findtext("title") or "").strip()
        link    = (entry.findtext("link") or "").strip()
        pub     = (entry.findtext("pubDate") or "").strip()
        summary = (entry.findtext("description") or "").strip()
        summary = re.sub(r"<[^>]+>", " ", summary)[:500]
        if title and link:
            items.append(Item(title=title, link=link, published=pub,
                              source=source_url, summary=summary))
    return items


def _parse_date(pub: str) -> Optional[datetime]:
    """Try common date formats and return a UTC-aware datetime or None."""
    if not pub:
        return None
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    # Normalise GMT → +0000
    clean = pub.replace("GMT", "+0000").strip()
    for fmt in fmts:
        try:
            dt = datetime.strptime(clean, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def fetch_all_feeds(
    cfg: Dict[str, Any],
    seen_ids: Set[str],
) -> Tuple[List[Item], Dict[str, Dict[str, Any]]]:
    """
    Fetch every RSS/Atom feed in config. Returns:
      - new_items: unseen Items within max_article_age_days
      - feed_health: {url: {ok, count, error}}
    """
    feeds         = cfg.get("feeds", [])
    max_age_days  = int(cfg.get("max_article_age_days", 14))
    max_per_feed  = int(cfg.get("max_items_per_feed", 50))
    timeout       = int(cfg.get("timeout_seconds", 25))
    retries       = int(cfg.get("fetch_retries", 3))
    backoff       = int(cfg.get("retry_backoff_seconds", 2))
    cutoff        = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    new_items:   List[Item]              = []
    feed_health: Dict[str, Dict[str, Any]] = {}

    for url in feeds:
        raw = _http_get(url, timeout=timeout, retries=retries, backoff=backoff)
        if raw is None:
            feed_health[url] = {"ok": False, "count": 0, "error": "fetch_failed"}
            continue

        parsed = _parse_feed(raw, url)[:max_per_feed]
        added  = 0
        for item in parsed:
            iid = _item_id(item)
            if iid in seen_ids:
                continue
            # Age filter
            dt = _parse_date(item.published)
            if dt and dt < cutoff:
                continue
            seen_ids.add(iid)
            new_items.append(item)
            added += 1

        feed_health[url] = {"ok": True, "count": added}
        logging.debug(f"Feed OK ({added} new): {url}")

    total_ok = sum(1 for v in feed_health.values() if v["ok"])
    logging.info(
        f"Feeds: {total_ok}/{len(feeds)} healthy  |  {len(new_items)} new items"
    )
    return new_items, feed_health


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

NEGATIVE_SIGNALS = [
    "recall", "banned", "dangerous", "unsafe", "lawsuit", "scam", "fake",
    "fraud", "toxic", "harmful", "reaction", "allergy", "burn", "rash",
    "side effect", "contamination", "cancer", "warning", "avoid",
    "horrible", "worst", "terrible", "boycott", "complaint", "fine",
    "penalty", "class action", "investigation", "violation", "counterfeit",
]

POSITIVE_SIGNALS = [
    "launch", "new product", "partnership", "collaboration", "campaign",
    "award", "innovation", "expansion", "revenue", "growth", "profit",
    "acquisition", "review", "haul", "favourite", "favorite", "best",
    "love", "amazing", "holy grail", "game changer", "must have",
]


AMBIGUOUS_ALIAS_CUES: Dict[str, List[str]] = {
    "the ordinary": ["skincare", "skin care", "serum", "niacinamide", "retinol", "glycolic", "salicylic", "hyaluronic", "deciem", "peeling", "cleanser", "moisturizer", "cosmetic"],
    "rihanna": ["fenty", "beauty", "skincare", "skin care", "makeup", "cosmetic", "fragrance"],
    "dove": ["beauty", "soap", "body wash", "deodorant", "skincare", "lotion", "personal care", "unilever"],
    "lux": ["soap", "beauty", "body wash", "personal care", "unilever"],
    "origins": ["skincare", "beauty", "cosmetic", "serum", "moisturizer", "estee lauder"],
}


def _alias_pattern(alias: str) -> re.Pattern[str]:
    tokens = re.findall(r"[a-z0-9]+", alias.lower())
    if not tokens:
        return re.compile(r"a^")
    return re.compile(r"(?<![a-z0-9])" + r"[\s'’._&-]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])", re.I)


def _match_alias(text: str, brand_name: str, aliases: List[str]) -> Optional[str]:
    candidates: List[str] = []
    for value in [brand_name, *aliases]:
        value = (value or "").strip()
        if value and value.lower() not in {x.lower() for x in candidates}:
            candidates.append(value)
    lower_text = text.lower()
    for alias in sorted(candidates, key=len, reverse=True):
        if not _alias_pattern(alias).search(text):
            continue
        cues = AMBIGUOUS_ALIAS_CUES.get(alias.lower()) or AMBIGUOUS_ALIAS_CUES.get(brand_name.lower())
        if cues and not any(cue in lower_text for cue in cues):
            continue
        return alias
    return None


def _sev_from_hits(neg: int, pos: int) -> Tuple[int, str, str]:
    """Return (severity, event_type, risk_type) from keyword hit counts."""
    if neg >= 3:
        return 8, "negative", "Consumer Safety"
    if neg == 2:
        return 6, "negative", "Reputation Risk"
    if neg == 1:
        return 4, "negative", "Brand Mention – Risk"
    if pos >= 2:
        return 4, "positive", "Brand / Product Update"
    if pos == 1:
        return 3, "positive", "Brand Mention – Positive"
    return 2, "neutral", "General Mention"


def score_item_for_brand(item: Item, brand: Brand) -> Optional[ScoredItem]:
    """Return a ScoredItem if item is relevant to brand, else None."""
    raw_text = f"{item.title} {item.summary}"
    text = raw_text.lower()

    # Must match a complete brand name/alias, with extra context for ambiguous names.
    matched_alias = _match_alias(raw_text, brand.name, brand.aliases)
    if not matched_alias:
        return None

    # Count keyword signals
    keyword_hits = [kw for kw in brand.keywords if kw.lower() in text]
    neg_hits     = sum(1 for s in NEGATIVE_SIGNALS if s in text)
    pos_hits     = sum(1 for s in POSITIVE_SIGNALS if s in text)

    # Keyword hits amplify
    neg_hits += sum(1 for kw in keyword_hits
                    if kw.lower() in NEGATIVE_SIGNALS)

    severity, event_type, risk_type = _sev_from_hits(neg_hits, pos_hits)

    # Boost for safety-critical terms in title
    title_lower = item.title.lower()
    if any(w in title_lower for w in ["recall", "banned", "unsafe", "lawsuit", "cancer"]):
        severity = min(10, severity + 2)
        event_type = "negative"
        risk_type  = "Consumer Safety"

    if event_type == "negative":
        impact_area    = "reputation"
        why_it_matters = (
            "Negative brand coverage can spread rapidly and damage consumer trust. "
            "Early detection allows rapid response."
        )
    else:
        impact_area    = "marketing"
        why_it_matters = (
            "Positive brand signals indicate market activity, "
            "competitive movement, or growing consumer interest."
        )

    reasons = [
        f"Brand match: '{matched_alias}' found in article",
        f"Neg signals: {neg_hits}  |  Pos signals: {pos_hits}  |  Keywords hit: {len(keyword_hits)}",
        f"Severity: {severity}/10 ({event_type.upper()})",
    ]

    return ScoredItem(
        item=item,
        brand_hit=brand.name,
        alias_matched=matched_alias,
        keyword_hits=keyword_hits,
        severity=severity,
        impact_area=impact_area,
        risk_type=risk_type,
        reasons=reasons,
        lane="brand",
        event_type=event_type,
        why_it_matters=why_it_matters,
        matched_areas={impact_area: 1},
    )


def score_item_industry(item: Item, cfg: Dict[str, Any]) -> Optional[ScoredItem]:
    """Return a ScoredItem for industry-level signals (no specific brand)."""
    industry_keywords = [kw.lower() for kw in cfg.get("industry_keywords", [])]
    text = f"{item.title} {item.summary}".lower()

    hits = [kw for kw in industry_keywords if kw in text]
    if len(hits) < 2:
        return None

    neg_hits = sum(1 for s in NEGATIVE_SIGNALS if s in text)
    pos_hits = sum(1 for s in POSITIVE_SIGNALS if s in text)
    severity, event_type, risk_type = _sev_from_hits(neg_hits, pos_hits)

    # Industry items need a slightly higher bar
    threshold = int(cfg.get("industry_alert_threshold", 3))
    if severity < threshold:
        return None

    return ScoredItem(
        item=item,
        brand_hit=None,
        alias_matched=None,
        keyword_hits=hits[:10],
        severity=severity,
        impact_area="industry",
        risk_type=f"Industry — {risk_type}",
        reasons=[
            f"Industry keywords matched: {', '.join(hits[:5])}",
            f"Severity: {severity}/10 ({event_type.upper()})",
        ],
        lane="industry",
        event_type=event_type,
        why_it_matters="Broad industry movement can affect all monitored brands.",
        matched_areas={"industry": 1},
    )


def score_all_items(
    items: List[Item],
    brands: List[Brand],
    cfg: Dict[str, Any],
) -> List[ScoredItem]:
    """Score every item against all brands + industry lane."""
    alert_threshold    = int(cfg.get("alert_threshold", 3))
    industry_mode      = cfg.get("industry_mode", True)
    industry_threshold = int(cfg.get("industry_alert_threshold", 3))

    scored: List[ScoredItem] = []
    for item in items:
        hit_brands: Set[str] = set()

        # Brand lane
        for brand in brands:
            si = score_item_for_brand(item, brand)
            if si and si.severity >= alert_threshold:
                scored.append(si)
                hit_brands.add(brand.name)

        # Industry lane (only if item didn't match a brand)
        if industry_mode and not hit_brands:
            si = score_item_industry(item, cfg)
            if si and si.severity >= industry_threshold:
                scored.append(si)

    logging.info(f"Scoring: {len(scored)} items scored from {len(items)} candidates")
    return scored


# ─────────────────────────────────────────────────────────────────────────────
# Cross-feed deduplication (Jaccard similarity)
# ─────────────────────────────────────────────────────────────────────────────

def _tokenise(text: str) -> Set[str]:
    return set(re.findall(r"\b\w{4,}\b", text.lower()))


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_cross_feed(
    scored: List[ScoredItem],
    threshold: float = 0.55,
) -> Tuple[List[ScoredItem], int]:
    """Remove near-duplicate items across sources. Returns (deduped, dropped_count)."""
    kept:   List[ScoredItem] = []
    tokens: List[Set[str]]   = []

    for si in scored:
        t = _tokenise(f"{si.item.title} {si.item.summary}")
        is_dup = any(_jaccard(t, other) >= threshold for other in tokens)
        if not is_dup:
            kept.append(si)
            tokens.append(t)

    dropped = len(scored) - len(kept)
    if dropped:
        logging.info(f"Dedup: removed {dropped} near-duplicate item(s)")
    return kept, dropped


# ─────────────────────────────────────────────────────────────────────────────
# AI verification  (optional — requires ai_config.json with enabled: true)
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(prompt: str, ai_cfg: Dict[str, Any]) -> Optional[str]:
    url     = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type":         "application/json",
        "x-api-key":            ai_cfg["api_key"],
        "anthropic-version":    "2023-06-01",
    }
    body = json.dumps({
        "model":      ai_cfg.get("model", "claude-haiku-4-5-20251001"),
        "max_tokens": int(ai_cfg.get("max_tokens", 100)),
        "messages":   [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    try:
        req = Request(url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["content"][0]["text"].strip()
    except Exception as e:
        logging.warning(f"AI verify: API call failed — {e}")
        return None


def run_ai_verification(
    scored: List[ScoredItem],
    ai_cfg: Dict[str, Any],
    min_severity: int = 3,
) -> Tuple[List[ScoredItem], int]:
    """
    For negative items at or above min_severity, ask Claude whether the
    article is genuinely negative about the brand. Drop if AI says no.
    Returns (verified_list, dropped_count).
    """
    kept:    List[ScoredItem] = []
    dropped: int = 0

    for si in scored:
        # Only verify high-confidence negative brand items
        if si.event_type != "negative" or si.severity < min_severity or si.lane != "brand":
            kept.append(si)
            continue

        prompt = (
            f"You are a brand risk analyst. Read this headline and summary, "
            f"then answer with exactly one word: YES if the article represents "
            f"a genuine negative risk or bad news for the brand '{si.brand_hit}', "
            f"or NO if it is neutral, positive, or unrelated.\n\n"
            f"Headline: {si.item.title}\n"
            f"Summary: {si.item.summary[:300]}\n\n"
            f"Answer (YES or NO):"
        )
        answer = _call_claude(prompt, ai_cfg)
        if answer and answer.upper().startswith("NO"):
            logging.info(f"AI verify: dropped '{si.item.title[:60]}' — AI said not negative")
            dropped += 1
        else:
            kept.append(si)
        time.sleep(0.5)  # gentle rate-limit

    if dropped:
        logging.info(f"AI verify: dropped {dropped} false-positive(s)")
    return kept, dropped


# ─────────────────────────────────────────────────────────────────────────────
# HTML report generation
# ─────────────────────────────────────────────────────────────────────────────

def _sev_badge(sev: int) -> str:
    if sev >= 7:
        cls = "high";   label = "HIGH"
    elif sev >= 5:
        cls = "medium"; label = "MED"
    else:
        cls = "low";    label = "LOW"
    return f'<span class="badge badge-{cls}">{sev}/10 {label}</span>'


def _evt_badge(evt: str) -> str:
    cls = {"negative": "neg", "positive": "pos"}.get(evt, "neu")
    return f'<span class="badge badge-{cls}">{evt.upper()}</span>'


def generate_html_report(
    scored: List[ScoredItem],
    feed_health: Dict[str, Dict[str, Any]],
    total_fetched: int,
    cfg: Dict[str, Any],
    out_path: str,
) -> str:
    import html as _html

    now = datetime.now().strftime("%d %b %Y  %H:%M")
    brand_alerts    = [si for si in scored if si.lane == "brand"]
    industry_alerts = [si for si in scored if si.lane == "industry"]
    neg_count       = sum(1 for si in scored if si.event_type == "negative")
    pos_count       = sum(1 for si in scored if si.event_type == "positive")

    # Group brand alerts by brand name
    by_brand: Dict[str, List[ScoredItem]] = {}
    for si in brand_alerts:
        by_brand.setdefault(si.brand_hit or "Unknown", []).append(si)
    for lst in by_brand.values():
        lst.sort(key=lambda x: x.severity, reverse=True)

    # Feed health summary
    feeds_ok    = sum(1 for v in feed_health.values() if v.get("ok"))
    feeds_total = len(feed_health)

    def esc(s): return _html.escape(str(s) if s else "")

    def brand_section(name: str, items: List[ScoredItem]) -> str:
        rows = ""
        for si in items:
            rows += f"""
            <tr>
              <td><a href="{esc(si.item.link)}" target="_blank">{esc(si.item.title[:100])}</a>
                  <div class="meta">{esc(si.item.source)}  ·  {esc(si.item.published[:16])}</div>
              </td>
              <td>{_sev_badge(si.severity)}</td>
              <td>{_evt_badge(si.event_type)}</td>
              <td class="risk-type">{esc(si.risk_type)}</td>
              <td class="why">{esc(si.why_it_matters[:120])}</td>
            </tr>"""
        max_sev = max((si.severity for si in items), default=0)
        header_cls = "brand-header-high" if max_sev >= 7 else "brand-header-med" if max_sev >= 5 else "brand-header-low"
        return f"""
        <div class="brand-block">
          <div class="brand-header {header_cls}">
            <span class="brand-name">{esc(name)}</span>
            <span class="brand-meta">{len(items)} signal(s) · max severity {max_sev}/10</span>
          </div>
          <table class="signals-table">
            <thead><tr>
              <th>Article</th><th>Severity</th><th>Signal</th>
              <th>Risk Type</th><th>Why It Matters</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    brands_html = "".join(
        brand_section(name, items)
        for name, items in sorted(
            by_brand.items(),
            key=lambda x: max(si.severity for si in x[1]),
            reverse=True,
        )
    ) or '<p class="empty">No brand alerts this run.</p>'

    industry_rows = "".join(f"""
        <tr>
          <td><a href="{esc(si.item.link)}" target="_blank">{esc(si.item.title[:100])}</a>
              <div class="meta">{esc(si.item.source)}</div>
          </td>
          <td>{_sev_badge(si.severity)}</td>
          <td>{_evt_badge(si.event_type)}</td>
          <td class="risk-type">{esc(si.risk_type)}</td>
        </tr>""" for si in industry_alerts[:20]
    ) or '<tr><td colspan="4" class="empty">No industry signals this run.</td></tr>'

    feed_rows = "".join(
        f'<tr><td class="feed-url">{esc(url)}</td>'
        f'<td><span class="dot dot-{"green" if v.get("ok") else "red"}"></span>'
        f'{"OK" if v.get("ok") else "FAIL"}</td>'
        f'<td>{v.get("count", 0)}</td></tr>'
        for url, v in list(feed_health.items())[:40]
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SkincareIntel Report — {now}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --navy:#0f172a;--accent:#1d4ed8;--red:#dc2626;--green:#059669;
  --orange:#d97706;--muted:#64748b;--border:#e2e8f0;--bg:#f8fafc;--white:#fff;
}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--navy);font-size:14px;padding-bottom:60px}}
a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}
.header{{background:var(--white);border-bottom:1px solid var(--border);padding:0 40px}}
.header-inner{{max-width:1100px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;height:60px}}
.logo{{font-weight:700;font-size:17px}}.logo span{{color:var(--accent)}}
.meta-bar{{font-size:12px;color:var(--muted);text-align:right}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px 40px 0}}
.kpi-strip{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
.kpi{{background:var(--white);border:1px solid var(--border);border-radius:10px;padding:16px 22px;flex:1;min-width:120px}}
.kpi-val{{font-size:26px;font-weight:700;line-height:1;margin-bottom:4px}}
.kpi-label{{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
h2{{font-size:15px;font-weight:600;color:var(--navy);margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
.brand-block{{background:var(--white);border:1px solid var(--border);border-radius:10px;margin-bottom:18px;overflow:hidden}}
.brand-header{{display:flex;align-items:center;justify-content:space-between;padding:12px 18px}}
.brand-header-high{{background:#fef2f2;border-bottom:2px solid var(--red)}}
.brand-header-med{{background:#fffbeb;border-bottom:2px solid var(--orange)}}
.brand-header-low{{background:#f0f9ff;border-bottom:1px solid var(--border)}}
.brand-name{{font-weight:700;font-size:14px}}
.brand-meta{{font-size:12px;color:var(--muted)}}
table.signals-table{{width:100%;border-collapse:collapse}}
th{{background:var(--navy);color:#fff;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:8px 12px;text-align:left}}
td{{padding:9px 12px;border-bottom:1px solid var(--border);font-size:12px;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:nth-child(even) td{{background:#fafafa}}
.meta{{font-size:10px;color:var(--muted);margin-top:2px}}
.risk-type{{font-size:11px;color:var(--muted);white-space:nowrap}}
.why{{font-size:11px;color:var(--muted);max-width:260px}}
.badge{{display:inline-block;font-size:10px;font-weight:700;border-radius:4px;padding:2px 7px;white-space:nowrap}}
.badge-high{{background:#fef2f2;color:var(--red);border:1px solid #fecaca}}
.badge-medium{{background:#fffbeb;color:var(--orange);border:1px solid #fde68a}}
.badge-low{{background:#f0fdf4;color:#16a34a;border:1px solid #a7f3d0}}
.badge-neg{{background:#fef2f2;color:var(--red)}}
.badge-pos{{background:#f0fdf4;color:var(--green)}}
.badge-neu{{background:#f1f5f9;color:var(--muted)}}
.empty{{padding:16px;color:var(--muted);font-style:italic}}
.feed-url{{font-size:11px;color:var(--muted);max-width:500px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}}
.dot-green{{background:var(--green)}}.dot-red{{background:var(--red)}}
.footer{{max-width:1100px;margin:32px auto 0;padding:16px 40px;border-top:1px solid var(--border);font-size:11px;color:var(--muted);display:flex;justify-content:space-between}}
@media(max-width:700px){{.wrap,.footer{{padding:12px}}.header{{padding:0 12px}}.kpi-strip{{gap:10px}}}}
</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <div class="logo">Skincare<span>Intel</span></div>
    <div class="meta-bar">Daily Intelligence Report<br>{esc(now)}</div>
  </div>
</div>
<div class="wrap">
  <div class="kpi-strip">
    <div class="kpi"><div class="kpi-val" style="color:var(--accent)">{total_fetched}</div><div class="kpi-label">Articles Scanned</div></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--navy)">{len(scored)}</div><div class="kpi-label">Signals Found</div></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--red)">{neg_count}</div><div class="kpi-label">Risk Alerts</div></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--green)">{pos_count}</div><div class="kpi-label">Positive Signals</div></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--muted)">{feeds_ok}/{feeds_total}</div><div class="kpi-label">Feeds Healthy</div></div>
  </div>

  <h2>Brand Alerts</h2>
  {brands_html}

  <h2>Industry Signals</h2>
  <div class="brand-block">
    <table class="signals-table">
      <thead><tr><th>Article</th><th>Severity</th><th>Signal</th><th>Risk Type</th></tr></thead>
      <tbody>{industry_rows}</tbody>
    </table>
  </div>

  <h2>Feed Health ({feeds_ok}/{feeds_total} OK)</h2>
  <div class="brand-block">
    <table class="signals-table">
      <thead><tr><th>Feed URL</th><th>Status</th><th>New Items</th></tr></thead>
      <tbody>{feed_rows}</tbody>
    </table>
  </div>
</div>
<div class="footer">
  <span>SkincareIntel — Confidential Daily Report</span>
  <span>{esc(now)}</span>
</div>
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    logging.info(f"HTML report saved → {out_path}")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# Telegram alerts
# ─────────────────────────────────────────────────────────────────────────────

def maybe_send_telegram(cfg: Dict[str, Any], scored: List[ScoredItem]) -> None:
    tg_path = TG_CONFIG_PATH
    if not os.path.exists(tg_path):
        return
    try:
        with open(tg_path, "r", encoding="utf-8") as f:
            tg = json.load(f)
    except Exception:
        return

    token      = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", "")).strip()
    chat_id    = os.environ.get("TELEGRAM_CHAT_ID", str(tg.get("chat_id", ""))).strip()
    if not tg.get("enabled", False) and not (token and chat_id):
        return
    min_sev    = int(tg.get("min_severity_to_alert", 7))
    max_alerts = int(tg.get("max_alerts_per_run", 5))

    if not token or not chat_id:
        return

    alerts = sorted(
        [si for si in scored if si.severity >= min_sev],
        key=lambda x: x.severity, reverse=True,
    )[:max_alerts]

    if not alerts:
        logging.info("Telegram: no alerts above threshold — nothing to send")
        return

    base_url = f"https://api.telegram.org/bot{token}/sendMessage"
    for si in alerts:
        emoji  = "🚨" if si.event_type == "negative" else "✅" if si.event_type == "positive" else "ℹ️"
        text   = (
            f"{emoji} SkincareIntel Alert\n"
            f"Brand: {si.brand_hit or 'Industry'}\n"
            f"Severity: {si.severity}/10\n"
            f"Type: {si.risk_type}\n"
            f"Title: {si.item.title[:120]}\n"
            f"Link: {si.item.link}"
        )
        payload = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        try:
            req = Request(
                base_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                _ = resp.read()
            logging.info(f"Telegram: sent alert for '{si.item.title[:50]}'")
        except HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", errors="replace")).get("description", "")
            except Exception:
                detail = ""
            if e.code == 403:
                logging.warning(
                    "Telegram: bot is forbidden from messaging this chat. Start the bot first, "
                    "confirm the chat ID, or re-add the bot to the group."
                )
            else:
                logging.warning(f"Telegram: HTTP {e.code} — {detail or e.reason}")
        except Exception as e:
            logging.warning(f"Telegram: failed to send — {e}")
        time.sleep(0.3)


# ─────────────────────────────────────────────────────────────────────────────
# Slack alerts
# ─────────────────────────────────────────────────────────────────────────────

def _load_slack_config() -> Optional[Dict[str, Any]]:
    """slack_monitor.py is actually a JSON config file."""
    if not os.path.exists(SLACK_CFG_PATH):
        return None
    try:
        with open(SLACK_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("enabled", False):
            return None
        if not cfg.get("webhook_url", "").startswith("https://"):
            return None
        return cfg
    except Exception:
        return None


def _slack_post(webhook_url: str, payload: Dict[str, Any]) -> bool:
    body = json.dumps(payload).encode("utf-8")
    try:
        req = Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=15) as resp:
            _ = resp.read()
        return True
    except Exception as e:
        logging.warning(f"Slack: webhook POST failed — {e}")
        return False


def maybe_send_slack(
    cfg: Dict[str, Any],
    scored: List[ScoredItem],
    brand_alerts: List[ScoredItem],
    industry_alerts: List[ScoredItem],
    total_fetched: int,
) -> None:
    slack = _load_slack_config()
    if not slack:
        logging.info("Slack: not configured or disabled — skipping")
        return

    webhook     = slack["webhook_url"]
    min_sev     = int(slack.get("min_severity_to_alert", 5))
    max_alerts  = int(slack.get("max_alerts_per_run", 8))
    bot_name    = slack.get("bot_name", "SkincareIntel")
    bot_icon    = slack.get("bot_icon_emoji", ":bar_chart:")
    send_digest = slack.get("send_daily_digest", True)
    digest_min  = int(slack.get("digest_min_items", 1))

    # Individual high-severity alerts
    high_webhook = slack.get("high_alert_webhook_url", webhook)
    alerts = sorted(
        [si for si in scored if si.severity >= min_sev],
        key=lambda x: x.severity, reverse=True,
    )[:max_alerts]

    for si in alerts:
        color   = "#dc2626" if si.event_type == "negative" else "#059669"
        payload = {
            "username":   bot_name,
            "icon_emoji": bot_icon,
            "attachments": [{
                "color": color,
                "title": si.item.title[:120],
                "title_link": si.item.link,
                "text": si.why_it_matters or "",
                "fields": [
                    {"title": "Brand",    "value": si.brand_hit or "Industry", "short": True},
                    {"title": "Severity", "value": f"{si.severity}/10",        "short": True},
                    {"title": "Type",     "value": si.risk_type,               "short": True},
                    {"title": "Signal",   "value": si.event_type.upper(),       "short": True},
                ],
                "footer": "SkincareIntel",
                "ts": int(datetime.now().timestamp()),
            }],
        }
        _slack_post(high_webhook, payload)
        time.sleep(0.2)

    # Daily digest summary
    if send_digest and len(scored) >= digest_min:
        neg_count = sum(1 for si in scored if si.event_type == "negative")
        pos_count = sum(1 for si in scored if si.event_type == "positive")
        digest = {
            "username":   bot_name,
            "icon_emoji": bot_icon,
            "text": (
                f"*SkincareIntel Daily Digest*  |  "
                f"{total_fetched} articles scanned  ·  "
                f"{len(scored)} signals  ·  "
                f":red_circle: {neg_count} risks  ·  "
                f":large_green_circle: {pos_count} positive"
            ),
        }
        _slack_post(webhook, digest)
        logging.info("Slack: daily digest sent")


# ─────────────────────────────────────────────────────────────────────────────
# Email delivery
# ─────────────────────────────────────────────────────────────────────────────

def maybe_send_email(
    cfg: Dict[str, Any],
    brand_alerts: List[ScoredItem],
    industry_alerts: List[ScoredItem],
    total_fetched: int,
    report_path: str,
) -> None:
    email_cfg_block = cfg.get("email", {})
    env_email_ready = bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS") and os.environ.get("EMAIL_TO"))
    if not email_cfg_block.get("enabled", False) and not env_email_ready:
        logging.info("Email: disabled in config.json — skipping")
        return

    only_on_alerts = email_cfg_block.get("only_on_alerts", True)
    if only_on_alerts and not brand_alerts and not industry_alerts:
        logging.info("Email: no alerts this run and only_on_alerts=true — skipping")
        return

    ec_path = os.path.join(BASE_DIR, email_cfg_block.get("config_path", "email_config.json"))
    if not os.path.exists(ec_path):
        logging.warning(f"Email: config file not found at {ec_path}")
        return

    try:
        with open(ec_path, "r", encoding="utf-8") as f:
            ec = json.load(f)
    except Exception as e:
        logging.warning(f"Email: could not load email config — {e}")
        return

    # Prefer environment variables so credentials do not need to live in files.
    ec["from_email"] = os.environ.get("EMAIL_FROM", ec.get("from_email", "")).strip()
    to_env = os.environ.get("EMAIL_TO", "").strip()
    if to_env:
        ec["to_emails"] = [x.strip() for x in to_env.split(",") if x.strip()]
    ec["smtp_user"] = os.environ.get("SMTP_USER", ec.get("smtp_user", "")).strip()
    ec["smtp_pass"] = os.environ.get("SMTP_PASS", ec.get("smtp_pass", "")).strip()

    required = ["from_email", "to_emails", "smtp_host", "smtp_port", "smtp_user", "smtp_pass"]
    missing = [key for key in required if not ec.get(key)]
    if missing:
        logging.warning(f"Email: missing configuration fields: {', '.join(missing)} — skipping")
        return

    prefix  = email_cfg_block.get("subject_prefix", "SkincareIntel")
    neg_count = sum(1 for si in brand_alerts if si.event_type == "negative")
    subject = (
        f"[{prefix}] {neg_count} Risk Alert(s) — "
        f"{datetime.now().strftime('%d %b %Y')}"
        if neg_count else
        f"[{prefix}] Daily Report — {datetime.now().strftime('%d %b %Y')}"
    )

    lines = [
        f"SkincareIntel — Daily Report",
        f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')}",
        f"",
        f"SUMMARY",
        f"  Articles scanned : {total_fetched}",
        f"  Brand alerts     : {len(brand_alerts)}",
        f"  Risk alerts      : {neg_count}",
        f"  Industry signals : {len(industry_alerts)}",
        f"",
    ]

    if brand_alerts:
        lines.append("TOP BRAND ALERTS (by severity)")
        lines.append("-" * 50)
        for si in sorted(brand_alerts, key=lambda x: x.severity, reverse=True)[:10]:
            lines += [
                f"  [{si.severity}/10 {si.event_type.upper()}]  {si.brand_hit}",
                f"  {si.item.title[:100]}",
                f"  {si.item.link}",
                f"  {si.why_it_matters[:120]}",
                "",
            ]

    lines.append("The full HTML report is attached.")

    msg            = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = ec["from_email"]
    msg["To"]      = ", ".join(ec["to_emails"])
    msg.set_content("\n".join(lines))

    if os.path.exists(report_path):
        with open(report_path, "rb") as f:
            msg.add_attachment(
                f.read(), maintype="text", subtype="html",
                filename=os.path.basename(report_path),
            )

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(ec["smtp_host"], int(ec["smtp_port"]), timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(ec["smtp_user"], ec["smtp_pass"])
            server.send_message(msg)
        logging.info(f"Email: sent to {ec['to_emails']}")
    except smtplib.SMTPAuthenticationError:
        logging.error(
            "Email: authentication rejected. For Gmail, create a fresh App Password "
            "after enabling 2-Step Verification; do not use the normal account password."
        )
    except Exception as e:
        logging.error(f"Email: delivery failed — {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.info("=" * 60)
    logging.info("SkincareIntel agent starting")
    logging.info("=" * 60)

    # ── Load config ──────────────────────────────────────────────
    cfg    = load_config()
    brands = load_brands(cfg)
    logging.info(f"Config: {len(brands)} brands  |  {len(cfg.get('feeds', []))} feeds")

    # ── Load seen-IDs state ──────────────────────────────────────
    seen_ids = load_state()
    logging.info(f"State: {len(seen_ids)} previously seen IDs")

    # ── RSS/Atom feeds ───────────────────────────────────────────
    new_items, feed_health = fetch_all_feeds(cfg, seen_ids)
    total_fetched = len(new_items)

    # ── Score RSS items ──────────────────────────────────────────
    scored: List[ScoredItem] = score_all_items(new_items, brands, cfg)

    # ── YouTube monitoring ───────────────────────────────────────
    if _YOUTUBE_AVAILABLE:
        try:
            yt_items = _fetch_youtube(brands, cfg)
            if yt_items:
                scored.extend(yt_items)
                logging.info(f"YouTube: added {len(yt_items)} item(s)")
        except Exception as e:
            logging.warning(f"YouTube: unexpected error — {e}")
    else:
        logging.info("YouTube: youtube_monitor.py not found — skipping")

    # ── Reddit sentiment ─────────────────────────────────────────
    if _REDDIT_AVAILABLE:
        try:
            reddit_items = _fetch_reddit(brands)
            if reddit_items:
                scored.extend(reddit_items)
                logging.info(f"Reddit: added {len(reddit_items)} item(s)")
        except Exception as e:
            logging.warning(f"Reddit: unexpected error — {e}")
    else:
        logging.info("Reddit: reddit_monitor.py not found — skipping")

    # ── Kenya influencer tracking ────────────────────────────────
    if _INFLUENCER_AVAILABLE:
        try:
            yt_api_key: Optional[str] = None
            yt_cfg_path = os.path.join(BASE_DIR, "youtube_config.json")
            if os.path.exists(yt_cfg_path):
                with open(yt_cfg_path) as _f:
                    _yt = json.load(_f)
                candidate_key = os.environ.get("YOUTUBE_API_KEY", _yt.get("api_key", "")).strip()
                yt_api_key = candidate_key if (_yt.get("enabled") or candidate_key) else None
            influencer_items = _fetch_influencers(brands, youtube_api_key=yt_api_key)
            if influencer_items:
                scored.extend(influencer_items)
                logging.info(f"Influencer: added {len(influencer_items)} item(s)")
        except Exception as e:
            logging.warning(f"Influencer: unexpected error — {e}")
    else:
        logging.info("Influencer: influencer_monitor.py not found — skipping")

    # ── Kenya regulatory monitoring ──────────────────────────────
    if _REGULATORY_AVAILABLE:
        try:
            regulatory_items = _fetch_regulatory(brands)
            if regulatory_items:
                scored.extend(regulatory_items)
                logging.info(f"Regulatory: added {len(regulatory_items)} item(s)")
        except Exception as e:
            logging.warning(f"Regulatory: unexpected error — {e}")
    else:
        logging.info("Regulatory: kenya_regulatory_monitor.py not found — skipping")

    # ── Instagram + Social Media monitoring ─────────────────────
    if _INSTAGRAM_AVAILABLE:
        try:
            instagram_items = _fetch_instagram(brands)
            if instagram_items:
                scored.extend(instagram_items)
                logging.info(f"Instagram/Social: added {len(instagram_items)} item(s)")
        except Exception as e:
            logging.warning(f"Instagram/Social: unexpected error — {e}")
    else:
        logging.info("Instagram/Social: instagram_monitor.py not found — skipping")

    # ── AI verification (optional) ───────────────────────────────
    ai_cfg = load_ai_config(AI_CONFIG_PATH)
    if ai_cfg:
        min_sev = int(ai_cfg.get("only_verify_above_severity", 3))
        logging.info(f"AI Verify: running on negative items with severity >= {min_sev}")
        scored, _ = run_ai_verification(scored, ai_cfg, min_sev)
    else:
        logging.info("AI Verify: disabled or not configured — skipping")

    # ── Cross-feed deduplication ─────────────────────────────────
    scored, _ = dedup_cross_feed(scored)

    # ── Separate by lane ─────────────────────────────────────────
    brand_alerts    = [si for si in scored if si.lane == "brand"]
    industry_alerts = [si for si in scored if si.lane == "industry"]
    logging.info(
        f"Final: {len(scored)} total  |  "
        f"{len(brand_alerts)} brand  |  "
        f"{len(industry_alerts)} industry"
    )

    # ── HTML report ──────────────────────────────────────────────
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = os.path.join(REPORTS_DIR, f"report_{ts}.html")
    generate_html_report(scored, feed_health, total_fetched, cfg, out_path)

    # ── Telegram alerts ──────────────────────────────────────────
    maybe_send_telegram(cfg, scored)

    # ── Slack alerts ─────────────────────────────────────────────
    maybe_send_slack(cfg, scored, brand_alerts, industry_alerts, total_fetched)

    # ── Email delivery ───────────────────────────────────────────
    maybe_send_email(cfg, brand_alerts, industry_alerts, total_fetched, out_path)

    # ── Save state ───────────────────────────────────────────────
    save_state(seen_ids)

    # ── Weekly state tracking ────────────────────────────────────
    if _WEEKLY_AVAILABLE:
        try:
            record_daily_run(scored, total_fetched, feed_health)
        except Exception as e:
            logging.warning(f"Weekly state: record failed — {e}")

    # ── Friday weekly report ─────────────────────────────────────
    if _WEEKLY_AVAILABLE and _WEEKLY_REPORT_AVAILABLE:
        try:
            weekly_items = get_weekly_items()
            weekly_runs  = get_weekly_runs()
            week_label   = get_week_label()
            maybe_generate_weekly_report(cfg, weekly_items, weekly_runs, week_label)
        except Exception as e:
            logging.warning(f"Weekly report: generation failed — {e}")

    logging.info("SkincareIntel run complete")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
