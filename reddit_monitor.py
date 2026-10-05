"""
reddit_monitor.py — Reddit thread sentiment analysis for SkincareIntel.

Unlike the RSS feed approach (which only gets post titles), this module
fetches actual comment threads from brand-relevant Reddit posts and runs
sentiment analysis over them. A product with 200 complaint comments is a
very different signal than one article mentioning the brand once.

Requires no API key — uses Reddit's public JSON endpoints.
Add to reddit_config.json (see below for schema).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

REDDIT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reddit_config.json"
)

# ── Default config (written if missing) ──────────────────────────────────────
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "subreddits": [
        "SkincareAddiction",
        "AsianBeauty",
        "MakeupAddiction",
        "BeautyGuruChatter",
        "KenyanBeauty",
        "nairobi",
        "Kenya",
    ],
    "posts_per_subreddit": 25,
    "max_comments_per_post": 50,
    "published_within_hours": 72,
    "min_comments_to_analyse": 5,
    "min_severity_to_include": 3,
    "comment_sentiment_batch": True,   # analyse top comments as a batch
    "user_agent": "windows:SkincareIntel:v1.1 (contact: your-email@example.com)",
    "request_delay_seconds": 1.5,
    "max_retries": 2,
    "oauth": {
        "enabled": False,
        "access_token": "",
        "client_id": "",
        "client_secret": "",
    },
}

# ── Sentiment keyword lists ───────────────────────────────────────────────────

NEGATIVE_WORDS: List[str] = [
    "broke out", "breakout", "rash", "burn", "burning", "irritation", "irritated",
    "allergic", "allergy", "reaction", "awful", "terrible", "horrible", "worst",
    "scam", "fake", "counterfeit", "return", "refund", "lawsuit", "recall",
    "dangerous", "unsafe", "harmful", "toxic", "contaminated", "contamination",
    "side effect", "side effects", "damaged", "destroy", "ruined", "peeling",
    "itchy", "itching", "swollen", "swelling", "hives", "avoid", "do not buy",
    "don't buy", "stay away", "warning", "boycott", "disappointed", "waste",
    "overpriced", "stopped working", "made it worse", "regret", "never again",
]

POSITIVE_WORDS: List[str] = [
    "love", "amazing", "holy grail", "game changer", "works great", "highly recommend",
    "best", "glowing", "cleared", "transformed", "miracle", "favourite", "favorite",
    "repurchase", "repurchasing", "bought again", "worth it", "perfect", "obsessed",
    "incredible", "wonderful", "excellent", "smooth", "soft", "radiant",
]

INTENSIFIERS: List[str] = ["very", "extremely", "so", "absolutely", "completely", "totally"]


# ── Config loading ────────────────────────────────────────────────────────────

def load_reddit_config(path: str = REDDIT_CONFIG_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        # Write defaults so the user can see and edit them
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logging.info(f"Reddit: wrote default config to {path}")
        except Exception:
            pass
        return DEFAULT_CONFIG

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("enabled", True):
            logging.info("Reddit: disabled in reddit_config.json — skipping.")
            return None
        return cfg
    except Exception as e:
        logging.warning(f"Reddit: could not load config — {e}")
        return None


# ── Reddit API helpers ────────────────────────────────────────────────────────

_OAUTH_TOKEN: Optional[str] = None
_OAUTH_EXPIRES_AT: float = 0.0


def _user_agent(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]).strip()


def _oauth_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(cfg.get("oauth") or {})
    return {
        "access_token": os.environ.get("REDDIT_ACCESS_TOKEN", raw.get("access_token", "")).strip(),
        "client_id": os.environ.get("REDDIT_CLIENT_ID", raw.get("client_id", "")).strip(),
        "client_secret": os.environ.get("REDDIT_CLIENT_SECRET", raw.get("client_secret", "")).strip(),
        "enabled": bool(
            raw.get("enabled", False)
            or os.environ.get("REDDIT_ACCESS_TOKEN")
            or os.environ.get("REDDIT_CLIENT_ID")
        ),
    }


def _get_oauth_token(cfg: Dict[str, Any], timeout: int = 15) -> Optional[str]:
    """Get an application-only OAuth token when approved Reddit credentials exist."""
    global _OAUTH_TOKEN, _OAUTH_EXPIRES_AT
    now = time.time()
    auth = _oauth_settings(cfg)

    if auth["access_token"]:
        return auth["access_token"]
    if _OAUTH_TOKEN and now < _OAUTH_EXPIRES_AT - 60:
        return _OAUTH_TOKEN
    if not auth["enabled"] or not all(auth[k] for k in ("client_id", "client_secret")):
        return None

    basic = base64.b64encode(f"{auth['client_id']}:{auth['client_secret']}".encode()).decode()
    req = Request(
        "https://www.reddit.com/api/v1/access_token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": _user_agent(cfg),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        token = payload.get("access_token")
        if not token:
            logging.warning("Reddit OAuth: token response did not include access_token")
            return None
        _OAUTH_TOKEN = token
        _OAUTH_EXPIRES_AT = now + int(payload.get("expires_in", 3600))
        return token
    except Exception as e:
        logging.warning(f"Reddit OAuth: authentication failed — {e}")
        return None


def _read_json(url: str, headers: Dict[str, str], timeout: int) -> Any:
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, cfg: Dict[str, Any], timeout: int = 15) -> Optional[Any]:
    """Use OAuth when configured, otherwise try conservative public endpoints."""
    retries = max(1, int(cfg.get("max_retries", 2)))
    ua = _user_agent(cfg)
    token = _get_oauth_token(cfg, timeout=timeout)

    candidates: List[Tuple[str, Dict[str, str]]] = []
    if token:
        oauth_url = re.sub(r"https://(?:www|old)\.reddit\.com", "https://oauth.reddit.com", url)
        candidates.append((oauth_url, {"User-Agent": ua, "Authorization": f"bearer {token}", "Accept": "application/json"}))
    else:
        separator = "&" if "?" in url else "?"
        public_url = url if "raw_json=" in url else f"{url}{separator}raw_json=1"
        candidates.append((public_url, {"User-Agent": ua, "Accept": "application/json"}))
        candidates.append((public_url.replace("https://www.reddit.com", "https://old.reddit.com"), {"User-Agent": ua, "Accept": "application/json"}))

    last_code: Optional[int] = None
    for candidate, headers in candidates:
        for attempt in range(retries):
            try:
                return _read_json(candidate, headers, timeout)
            except HTTPError as e:
                last_code = e.code
                if e.code in (401, 403) and token:
                    # Token may have expired or been revoked; clear it for next run.
                    global _OAUTH_TOKEN, _OAUTH_EXPIRES_AT
                    _OAUTH_TOKEN, _OAUTH_EXPIRES_AT = None, 0.0
                if e.code == 429 and attempt < retries - 1:
                    delay = min(30, 3 * (attempt + 1))
                    logging.warning(f"Reddit: rate limited (429) — retrying in {delay}s")
                    time.sleep(delay)
                    continue
                logging.warning(f"Reddit: HTTP {e.code} for {candidate}")
                break
            except (URLError, OSError, ValueError) as e:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                logging.warning(f"Reddit: fetch error for {candidate} — {e}")
    if last_code in (403, 429):
        logging.info("Reddit: public JSON unavailable; trying RSS fallback for post discovery.")
    return None


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _fetch_subreddit_rss(
    subreddit: str,
    limit: int,
    hours_back: int,
    cfg: Dict[str, Any],
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    url = f"https://www.reddit.com/r/{subreddit}/new/.rss?limit={min(limit, 100)}"
    req = Request(url, headers={"User-Agent": _user_agent(cfg), "Accept": "application/atom+xml,application/rss+xml"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        logging.warning(f"Reddit RSS: unavailable for r/{subreddit} — {e}")
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    posts: List[Dict[str, Any]] = []
    for entry in root.findall("a:entry", ns)[:limit]:
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "").strip()
        content = entry.findtext("a:content", default="", namespaces=ns) or ""
        link_el = entry.find("a:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        try:
            created = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            created = datetime.now(timezone.utc)
        if created < cutoff:
            continue
        match = re.search(r"/comments/([a-z0-9]+)/", link, re.I)
        post_id = match.group(1) if match else link
        posts.append({
            "id": post_id,
            "title": title,
            "selftext": _strip_html(content),
            "url": link,
            "score": 0,
            "num_comments": 0,
            "created_utc": created.timestamp(),
            "published": created.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "subreddit": subreddit,
            "author": "",
            "flair": "",
        })
    return posts


def fetch_subreddit_posts(
    subreddit: str,
    limit: int = 25,
    hours_back: int = 72,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return recent posts from JSON; fall back to Reddit RSS when blocked."""
    cfg = cfg or DEFAULT_CONFIG
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={min(limit, 100)}"
    data = _get_json(url, cfg)
    if not data:
        return _fetch_subreddit_rss(subreddit, limit, hours_back, cfg)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    posts = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        created = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            continue
        posts.append({
            "id":           p.get("id", ""),
            "title":        p.get("title", ""),
            "selftext":     p.get("selftext", ""),
            "url":          f"https://www.reddit.com{p.get('permalink', '')}",
            "score":        int(p.get("score", 0)),
            "num_comments": int(p.get("num_comments", 0)),
            "created_utc":  p.get("created_utc", 0),
            "published":    created.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "subreddit":    subreddit,
            "author":       p.get("author", ""),
            "flair":        p.get("link_flair_text", ""),
        })
    return posts


def fetch_post_comments(
    post_id: str,
    subreddit: str,
    max_comments: int = 50,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return top-level comments when Reddit JSON/OAuth access is available."""
    cfg = cfg or DEFAULT_CONFIG
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit={max_comments}&sort=top"
    data = _get_json(url, cfg)
    if not data or not isinstance(data, list) or len(data) < 2:
        return []

    comments = []
    for child in data[1].get("data", {}).get("children", []):
        body = child.get("data", {}).get("body", "")
        if body and body not in ("[deleted]", "[removed]"):
            comments.append(body)
        if len(comments) >= max_comments:
            break
    return comments


# ── Sentiment scoring ─────────────────────────────────────────────────────────

def _count_weighted(text: str, words: List[str]) -> float:
    """Count keyword hits, giving 1.5x weight if preceded by an intensifier."""
    t = text.lower()
    score = 0.0
    for w in words:
        idx = 0
        while True:
            pos = t.find(w, idx)
            if pos == -1:
                break
            # Check for intensifier in the 30 chars before the word
            prefix = t[max(0, pos - 30): pos]
            weight = 1.5 if any(intens in prefix for intens in INTENSIFIERS) else 1.0
            score += weight
            idx = pos + 1
    return score


def analyse_comments(
    comments: List[str],
    brand_name: str,
    post_title: str,
) -> Dict[str, Any]:
    """
    Run lightweight sentiment analysis over a list of comment strings.
    Returns a dict with counts, scores, sample quotes, and a severity estimate.
    """
    brand_lower = brand_name.lower()
    relevant_comments = [c for c in comments if brand_lower in c.lower()]

    # If brand isn't mentioned directly in comments, fall back to all comments
    # when the post title is clearly about the brand
    if len(relevant_comments) < 3 and brand_lower in post_title.lower():
        relevant_comments = comments

    if not relevant_comments:
        return {"relevant": 0, "neg_score": 0.0, "pos_score": 0.0,
                "sentiment": "neutral", "severity_bump": 0,
                "sample_neg": [], "sample_pos": []}

    neg_total = 0.0
    pos_total = 0.0
    sample_neg: List[str] = []
    sample_pos: List[str] = []

    for comment in relevant_comments:
        neg = _count_weighted(comment, NEGATIVE_WORDS)
        pos = _count_weighted(comment, POSITIVE_WORDS)
        neg_total += neg
        pos_total += pos

        snippet = comment[:120].replace("\n", " ").strip()
        if neg > pos and neg >= 1.0 and len(sample_neg) < 3:
            sample_neg.append(snippet)
        elif pos > neg and pos >= 1.0 and len(sample_pos) < 3:
            sample_pos.append(snippet)

    n = len(relevant_comments)
    net = neg_total - pos_total

    if net > n * 0.4:
        sentiment = "negative"
        # Severity bump: 1 for mild, 2 for strong, 3 for overwhelming negativity
        severity_bump = 1 if net < n else (2 if net < n * 2 else 3)
    elif pos_total > neg_total * 1.5:
        sentiment = "positive"
        severity_bump = 0
    else:
        sentiment = "neutral"
        severity_bump = 0

    return {
        "relevant":      n,
        "neg_score":     round(neg_total, 1),
        "pos_score":     round(pos_total, 1),
        "sentiment":     sentiment,
        "severity_bump": severity_bump,
        "sample_neg":    sample_neg,
        "sample_pos":    sample_pos,
    }


# ── Brand matching ────────────────────────────────────────────────────────────

AMBIGUOUS_ALIAS_CUES: Dict[str, List[str]] = {
    "the ordinary": ["skincare", "skin care", "serum", "niacinamide", "retinol", "glycolic", "salicylic", "hyaluronic", "deciem", "peeling", "cleanser", "moisturizer"],
    "rihanna": ["fenty", "beauty", "skincare", "makeup", "cosmetic"],
    "dove": ["beauty", "soap", "body wash", "deodorant", "skincare", "lotion", "personal care"],
    "lux": ["soap", "beauty", "body wash", "personal care"],
    "origins": ["skincare", "beauty", "serum", "moisturizer", "estee lauder"],
}


def _alias_matches(text: str, alias: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", alias.lower())
    if not tokens:
        return False
    pattern = re.compile(r"(?<![a-z0-9])" + r"[\s'’._&-]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])", re.I)
    return bool(pattern.search(text))


def _post_matches_brand(post: Dict[str, Any], brand: Any) -> Optional[str]:
    """Return the matched alias if this post is genuinely about `brand`."""
    text = f"{post['title']} {post['selftext']}"
    lower_text = text.lower()
    aliases = [brand.name] + (brand.aliases or [])
    for alias in sorted((a for a in aliases if a), key=len, reverse=True):
        if not _alias_matches(text, alias):
            continue
        cues = AMBIGUOUS_ALIAS_CUES.get(alias.lower()) or AMBIGUOUS_ALIAS_CUES.get(brand.name.lower())
        if cues and not any(cue in lower_text for cue in cues):
            continue
        return alias
    return None


def analyse_post_text(post: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback sentiment when Reddit allows post discovery but blocks comments."""
    text = f"{post.get('title', '')} {post.get('selftext', '')}".strip()
    neg = _count_weighted(text, NEGATIVE_WORDS)
    pos = _count_weighted(text, POSITIVE_WORDS)
    if neg > pos and neg >= 1:
        sentiment = "negative"
        bump = min(3, max(1, int(round(neg))))
        sample_neg = [text[:120]]
        sample_pos: List[str] = []
    elif pos > neg and pos >= 1:
        sentiment = "positive"
        bump = 0
        sample_neg = []
        sample_pos = [text[:120]]
    else:
        sentiment = "neutral"
        bump = 0
        sample_neg = []
        sample_pos = []
    return {
        "relevant": 1 if text else 0,
        "neg_score": round(neg, 1),
        "pos_score": round(pos, 1),
        "sentiment": sentiment,
        "severity_bump": bump,
        "sample_neg": sample_neg,
        "sample_pos": sample_pos,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_reddit_sentiment_items(
    brands: List[Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    Called from agent.py main(). Returns a list of ScoredItems derived from
    Reddit post + comment sentiment analysis.

    No API key required — uses Reddit's public JSON API.
    Rate limit: ~1 req/sec is safe. We sleep between calls.
    """
    from agent import ScoredItem, Item

    reddit_cfg = load_reddit_config()
    if not reddit_cfg:
        return []

    subreddits      = reddit_cfg.get("subreddits", DEFAULT_CONFIG["subreddits"])
    posts_per_sub   = int(reddit_cfg.get("posts_per_subreddit", 25))
    max_comments    = int(reddit_cfg.get("max_comments_per_post", 50))
    hours_back      = int(reddit_cfg.get("published_within_hours", 72))
    min_comments    = int(reddit_cfg.get("min_comments_to_analyse", 5))
    min_severity    = int(reddit_cfg.get("min_severity_to_include", 3))

    results: List[ScoredItem] = []
    seen_post_ids: set = set()

    for subreddit in subreddits:
        logging.info(f"Reddit: fetching r/{subreddit}...")
        posts = fetch_subreddit_posts(
            subreddit, limit=posts_per_sub, hours_back=hours_back, cfg=reddit_cfg
        )
        time.sleep(float(reddit_cfg.get("request_delay_seconds", 1.5)))

        for post in posts:
            if post["id"] in seen_post_ids:
                continue

            matched_brand = None
            matched_alias = None

            for brand in brands:
                alias = _post_matches_brand(post, brand)
                if alias:
                    matched_brand = brand
                    matched_alias = alias
                    break

            if not matched_brand:
                continue

            seen_post_ids.add(post["id"])

            # Only fetch comments if the post has enough engagement
            comments: List[str] = []
            if post["num_comments"] >= min_comments:
                comments = fetch_post_comments(
                    post["id"], subreddit, max_comments, cfg=reddit_cfg
                )
                time.sleep(float(reddit_cfg.get("request_delay_seconds", 1.5)))

            sentiment_data = (
                analyse_comments(comments, matched_brand.name, post["title"])
                if comments else analyse_post_text(post)
            )

            # Base severity: engagement + comment count signals
            score_bump = int(post["score"])
            base_severity = 2
            if score_bump >= 500:
                base_severity = 4
            elif score_bump >= 100:
                base_severity = 3

            base_severity += sentiment_data["severity_bump"]

            # Negative Reddit thread with many comments = higher signal
            if post["num_comments"] >= 50:
                base_severity += 1
            if post["num_comments"] >= 200:
                base_severity += 1

            severity = max(1, min(10, base_severity))

            if severity < min_severity:
                continue

            event_type = sentiment_data["sentiment"]
            if event_type == "neutral" and severity < 4:
                continue  # Skip low-signal neutral posts

            # Build summary with comment excerpts
            sample = sentiment_data["sample_neg"] or sentiment_data["sample_pos"]
            sample_text = " | ".join(f'"{s[:80]}"' for s in sample[:2])
            summary_parts = [
                f"💬 r/{subreddit} · {post['num_comments']} comments · Score: {post['score']}",
                f"Sentiment: {sentiment_data['sentiment'].upper()} "
                f"(neg={sentiment_data['neg_score']}, pos={sentiment_data['pos_score']})",
            ]
            if sample_text:
                summary_parts.append(f"Comments: {sample_text}")
            if post["selftext"]:
                summary_parts.append(post["selftext"][:200])

            summary = " · ".join(summary_parts)

            if event_type == "negative":
                risk_type      = "Consumer Sentiment"
                impact_area    = "reputation"
                why_it_matters = (
                    f"Negative Reddit thread with {post['num_comments']} comments signals "
                    "growing consumer dissatisfaction that can go viral."
                )
            elif event_type == "positive":
                risk_type      = "Positive Community Buzz"
                impact_area    = "marketing"
                why_it_matters = (
                    "Positive community chatter on Reddit drives organic discovery "
                    "and purchase intent among beauty consumers."
                )
            else:
                risk_type      = "Community Mention"
                impact_area    = "marketing"
                why_it_matters = "Brand mentioned in active beauty community discussion."

            reasons = [
                f"Reddit: {matched_brand.name} mentioned in r/{subreddit} (alias: {matched_alias})",
                f"Post score: {post['score']} · Comments: {post['num_comments']}",
                f"Reddit sentiment: {sentiment_data['sentiment'].upper()} "
                f"(neg={sentiment_data['neg_score']}, pos={sentiment_data['pos_score']}, "
                f"relevant comments={sentiment_data['relevant']})",
            ]
            if sentiment_data["sample_neg"]:
                reasons.append(f"Sample negative: {sentiment_data['sample_neg'][0][:100]}")

            item = Item(
                title=post["title"],
                link=post["url"],
                published=post["published"],
                source=f"https://www.reddit.com/r/{subreddit}",
                summary=summary,
            )

            si = ScoredItem(
                item=item,
                brand_hit=matched_brand.name,
                alias_matched=matched_alias,
                keyword_hits=[],
                severity=severity,
                impact_area=impact_area,
                risk_type=risk_type,
                reasons=reasons,
                lane="brand",
                event_type=event_type,
                why_it_matters=why_it_matters,
                matched_areas={impact_area: 1},
            )
            results.append(si)
            logging.info(
                f"Reddit: [{severity}/10] {event_type.upper()} — "
                f"{post['title'][:60]} (r/{subreddit}, {post['num_comments']} comments)"
            )

    logging.info(f"Reddit: {len(results)} post(s) scored across {len(subreddits)} subreddit(s).")
    return results