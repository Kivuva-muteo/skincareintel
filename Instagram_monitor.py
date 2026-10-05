"""
instagram_monitor.py — Instagram + Social Media monitoring for SkincareIntel.

HOW IT WORKS (two-layer approach):
─────────────────────────────────
LAYER 1 — Instagram direct monitoring (requires login credentials):
  Uses Instaloader to fetch public posts from brand accounts and beauty
  hashtags. Requires a dedicated Instagram account (free burner account works).
  Monitors: brand official pages + Kenya beauty hashtags + influencer accounts.

LAYER 2 — Social signal RSS feeds (no login, always runs):
  Google News RSS queries that catch when Instagram/TikTok/Facebook content
  goes viral enough to be covered by news sites. Catches the signals that
  actually matter for brand intelligence (a viral complaint, a ban story,
  a fake product warning shared on WhatsApp that got picked up by media).

SETUP:
  1. Create a free Instagram account (use a dedicated monitoring account)
  2. Add credentials to instagram_config.json
  3. Set "enabled": true
  Layer 2 runs automatically with no setup required.

RATE LIMITS:
  Instagram rate-limits aggressively. This module:
  - Sleeps 3-5s between requests
  - Checks max 10 profiles + 6 hashtags per run
  - Stops gracefully on rate limit errors
  - Safe to run once daily
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
import xml.etree.ElementTree as ET

INSTAGRAM_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "instagram_config.json"
)

# ── Default config ────────────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,

    "_setup_note": (
        "For Instagram direct monitoring: create a free burner Instagram account, "
        "add its username and password below, set instagram_login.enabled=true. "
        "Social signal RSS feeds (layer 2) run automatically without any login."
    ),

    "instagram_login": {
        "enabled": False,
        "username": "",
        "password": "",
        "session_file": "instagram_session"
    },

    # Brand official Instagram accounts to monitor
    "brand_accounts": [
        "niveakenya",
        "garnierkenya",
        "fentybeauty",
        "lorealpariskenya",
        "dovekenya",
        "vaseline",
        "cerave",
        "theordinary",
        "neutrogena",
        "esteelauder"
    ],

    # Kenya beauty/skincare hashtags to monitor
    "kenya_hashtags": [
        "skincareKenya",
        "beautyKenya",
        "NairobiBeauty",
        "KenyanBeauty",
        "skincareroutineKenya",
        "MadeInKenya",
        "KenyanInfluencer",
        "beautyproductsKenya",
        "darkskincare",
        "blackskincare"
    ],

    # Kenya beauty influencer Instagram handles
    "kenya_influencers": [
        "skincarewithdee_",
        "veebeautykenya",
        "wanjiruwaweru",
        "lyrabeautyke",
        "naomykwamboka",
        "beautybloggernairobi",
        "glowwithwanjiku",
        "kenyabeautyblog"
    ],

    "published_within_hours": 72,
    "min_likes_brand_post": 100,
    "min_likes_hashtag_post": 50,
    "min_severity_to_include": 3,
    "max_posts_per_account": 5,
    "max_posts_per_hashtag": 8,
    "request_delay_seconds": 4,

    # Layer 2: Social signal RSS feeds — always runs, no login needed
    "social_rss_feeds": [
        {
            "name": "TikTok skincare Kenya",
            "url": "https://news.google.com/rss/search?q=tiktok+skincare+beauty+kenya&hl=en&gl=KE&ceid=KE:en",
            "severity": 5
        },
        {
            "name": "Instagram beauty Kenya viral",
            "url": "https://news.google.com/rss/search?q=instagram+beauty+skincare+kenya+viral&hl=en&gl=KE&ceid=KE:en",
            "severity": 5
        },
        {
            "name": "Kenya influencer skincare review",
            "url": "https://news.google.com/rss/search?q=kenyan+influencer+skincare+beauty+review+2026&hl=en&gl=KE&ceid=KE:en",
            "severity": 4
        },
        {
            "name": "Social media cosmetics warning Kenya",
            "url": "https://news.google.com/rss/search?q=social+media+cosmetics+warning+fake+kenya&hl=en&gl=KE&ceid=KE:en",
            "severity": 6
        },
        {
            "name": "TikTok skincare recall viral",
            "url": "https://news.google.com/rss/search?q=tiktok+skincare+recall+dangerous+viral&hl=en",
            "severity": 7
        },
        {
            "name": "Facebook counterfeit beauty Kenya",
            "url": "https://news.google.com/rss/search?q=facebook+counterfeit+beauty+skincare+kenya&hl=en&gl=KE&ceid=KE:en",
            "severity": 7
        },
        {
            "name": "Instagram skin reaction viral",
            "url": "https://news.google.com/rss/search?q=instagram+skin+reaction+cream+lotion+viral&hl=en",
            "severity": 6
        },
        {
            "name": "WhatsApp beauty warning Kenya",
            "url": "https://news.google.com/rss/search?q=whatsapp+beauty+fake+warning+kenya&hl=en&gl=KE&ceid=KE:en",
            "severity": 6
        }
    ]
}

# ── Sentiment keywords ────────────────────────────────────────────────────────

NEGATIVE_CAPTION_WORDS: List[str] = [
    "broke out", "breakout", "rash", "burn", "burning", "irritated", "irritation",
    "allergic", "allergy", "reaction", "terrible", "horrible", "awful", "worst",
    "fake", "counterfeit", "scam", "dangerous", "unsafe", "harmful", "recall",
    "do not buy", "dont buy", "avoid", "warning", "returned", "refund",
    "damaged skin", "ruined", "itchy", "swollen", "hives", "boycott",
    "disappointed", "waste of money", "never again", "side effects",
]

POSITIVE_CAPTION_WORDS: List[str] = [
    "love", "amazing", "holy grail", "game changer", "obsessed", "must have",
    "highly recommend", "repurchase", "cleared my skin", "glowing", "transformed",
    "best ever", "favourite", "worth it", "incredible", "finally found",
]

VIRAL_INDICATORS: List[str] = [
    "viral", "trending", "everyone is talking", "blown up", "gone viral",
    "thousands of views", "millions of views",
]


AMBIGUOUS_ALIAS_CUES: Dict[str, List[str]] = {
    "the ordinary": ["skincare", "skin care", "serum", "niacinamide", "retinol", "glycolic", "salicylic", "hyaluronic", "deciem", "peeling", "cleanser", "moisturizer"],
    "rihanna": ["fenty", "beauty", "skincare", "makeup", "cosmetic"],
    "dove": ["beauty", "soap", "body wash", "deodorant", "skincare", "lotion", "personal care"],
    "lux": ["soap", "beauty", "body wash", "personal care"],
    "origins": ["skincare", "beauty", "serum", "moisturizer", "estee lauder"],
}


def _alias_matches(text: str, alias: str, brand_name: str = "") -> bool:
    tokens = re.findall(r"[a-z0-9]+", alias.lower())
    if not tokens:
        return False
    pattern = re.compile(r"(?<![a-z0-9])" + r"[\s'’._&-]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])", re.I)
    if not pattern.search(text):
        return False
    cues = AMBIGUOUS_ALIAS_CUES.get(alias.lower()) or AMBIGUOUS_ALIAS_CUES.get(brand_name.lower())
    return not cues or any(cue in text.lower() for cue in cues)


# ── Config loading ────────────────────────────────────────────────────────────

def load_instagram_config(path: str = INSTAGRAM_CONFIG_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logging.info(
                f"Instagram: wrote default config to {path}\n"
                "  → To enable Instagram direct monitoring, add login credentials\n"
                "    and set instagram_login.enabled=true in instagram_config.json\n"
                "  → Social signal RSS feeds run automatically without any setup."
            )
        except Exception:
            pass
        return DEFAULT_CONFIG

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("enabled", True):
            logging.info("Instagram: disabled in instagram_config.json — skipping.")
            return {}
        return cfg
    except Exception as e:
        logging.warning(f"Instagram: could not load config — {e}")
        return DEFAULT_CONFIG


# ── Caption sentiment analysis ────────────────────────────────────────────────

def _analyse_caption(caption: str, brand_name: str) -> Tuple[str, int]:
    """
    Returns (event_type, severity_bump) based on caption content.
    """
    if not caption:
        return "neutral", 0

    text = caption.lower()
    neg  = sum(1 for w in NEGATIVE_CAPTION_WORDS if w in text)
    pos  = sum(1 for w in POSITIVE_CAPTION_WORDS if w in text)
    viral = sum(1 for w in VIRAL_INDICATORS if w in text)

    if neg > pos:
        return "negative", min(neg + viral, 3)
    elif pos > 0:
        return "positive", 0
    else:
        return "neutral", viral  # Viral neutral = mild bump


def _engagement_bump(likes: int, comments: int = 0) -> int:
    total = likes + (comments * 3)  # Comments weight more than likes
    if total >= 50_000: return 3
    if total >= 10_000: return 2
    if total >= 1_000:  return 1
    return 0


def _find_brand_in_caption(caption: str, brands: List[Any]) -> Optional[str]:
    if not caption:
        return None
    for brand in brands:
        aliases = [brand.name] + (brand.aliases or [])
        if any(a and _alias_matches(caption, a, brand.name) for a in aliases):
            return brand.name
    return None


# ── Layer 1: Instagram direct (requires login) ────────────────────────────────

def _fetch_instagram_direct(
    cfg: Dict[str, Any],
    brands: List[Any],
) -> List[Dict[str, Any]]:
    """
    Fetch posts directly from Instagram using Instaloader.
    Requires credentials in instagram_config.json.
    Returns list of raw post dicts ready for ScoredItem conversion.
    """
    login_cfg = cfg.get("instagram_login", {})
    env_username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    if not login_cfg.get("enabled", False) and not env_username:
        logging.info("Instagram direct: login disabled — skipping Layer 1.")
        return []

    username = env_username or login_cfg.get("username", "").strip()
    password = os.environ.get("INSTAGRAM_PASSWORD", login_cfg.get("password", "")).strip()

    if not username:
        logging.warning("Instagram direct: username missing (INSTAGRAM_USERNAME or instagram_config.json) — skipping.")
        return []

    try:
        import instaloader
    except ImportError:
        logging.warning("Instagram direct: instaloader not installed. Run: pip install instaloader")
        return []

    hours_back     = int(cfg.get("published_within_hours", 72))
    max_per_acct   = int(cfg.get("max_posts_per_account", 5))
    max_per_tag    = int(cfg.get("max_posts_per_hashtag", 8))
    min_likes_brand = int(cfg.get("min_likes_brand_post", 100))
    min_likes_tag   = int(cfg.get("min_likes_hashtag_post", 50))
    delay          = float(cfg.get("request_delay_seconds", 4))
    cutoff         = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    session_file   = login_cfg.get("session_file", "instagram_session")
    session_path   = os.path.join(os.path.dirname(INSTAGRAM_CONFIG_PATH), session_file)
    if not os.path.exists(session_path) and not password:
        logging.warning(
            "Instagram direct: no saved session and no password supplied "
            "(INSTAGRAM_PASSWORD or instagram_config.json) — skipping."
        )
        return []

    L = instaloader.Instaloader(
        sleep=True,
        quiet=True,
        request_timeout=20,
        max_connection_attempts=2,
    )

    # Login (or load saved session to avoid repeated login)
    try:
        if os.path.exists(f"{session_path}"):
            L.load_session_from_file(username, session_path)
            logging.info(f"Instagram: loaded saved session for @{username}")
        else:
            L.login(username, password)
            L.save_session_to_file(session_path)
            logging.info(f"Instagram: logged in as @{username}, session saved")
    except Exception as e:
        logging.warning(f"Instagram: login failed — {e}")
        logging.warning("Instagram: check credentials in instagram_config.json")
        return []

    results: List[Dict[str, Any]] = []
    seen_shortcodes: set = set()
    brand_names    = [b.name for b in brands]

    # ── Monitor brand official accounts ──────────────────────────
    for handle in cfg.get("brand_accounts", []):
        try:
            logging.info(f"Instagram: checking brand account @{handle}...")
            profile = instaloader.Profile.from_username(L.context, handle)
            count = 0
            for post in profile.get_posts():
                if post.date_utc.replace(tzinfo=timezone.utc) < cutoff:
                    break
                if post.shortcode in seen_shortcodes:
                    continue
                if post.likes < min_likes_brand:
                    continue
                seen_shortcodes.add(post.shortcode)

                caption = post.caption or ""
                event_type, sentiment_bump = _analyse_caption(caption, handle)
                eng_bump   = _engagement_bump(post.likes, post.comments)
                severity   = max(1, min(10, 3 + sentiment_bump + eng_bump))

                # Try to match to a monitored brand
                brand_hit = _find_brand_in_caption(caption, brands)
                if not brand_hit:
                    # Map handle to brand name
                    for bname in brand_names:
                        if bname.lower().replace(" ", "") in handle.lower().replace(" ", ""):
                            brand_hit = bname
                            break

                results.append({
                    "source":      "instagram_brand",
                    "handle":      handle,
                    "shortcode":   post.shortcode,
                    "title":       f"@{handle}: {caption[:100] or '(no caption)'}",
                    "link":        f"https://www.instagram.com/p/{post.shortcode}/",
                    "published":   post.date_utc.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    "caption":     caption[:400],
                    "likes":       post.likes,
                    "comments":    post.comments,
                    "brand_hit":   brand_hit,
                    "event_type":  event_type,
                    "severity":    severity,
                    "risk_type":   "Negative Instagram Post" if event_type == "negative" else "Brand Instagram Activity",
                    "impact_area": "reputation" if event_type == "negative" else "marketing",
                    "why_it_matters": (
                        f"Official brand account @{handle} posted content with negative signals — "
                        "visible to all followers and can spread rapidly."
                        if event_type == "negative" else
                        f"Brand account @{handle} is active on Instagram Kenya."
                    ),
                })
                count += 1
                if count >= max_per_acct:
                    break
            time.sleep(delay)
        except instaloader.exceptions.ProfileNotExistsException:
            logging.warning(f"Instagram: account @{handle} not found — skipping.")
        except instaloader.exceptions.ConnectionException as e:
            if "429" in str(e) or "rate" in str(e).lower():
                logging.warning("Instagram: rate limited — stopping direct fetch for this run.")
                break
            logging.warning(f"Instagram: connection error for @{handle} — {e}")
        except Exception as e:
            logging.warning(f"Instagram: error fetching @{handle} — {e}")

    # ── Monitor Kenya beauty hashtags ─────────────────────────────
    for hashtag in cfg.get("kenya_hashtags", []):
        try:
            logging.info(f"Instagram: checking hashtag #{hashtag}...")
            tag   = instaloader.Hashtag.from_name(L.context, hashtag)
            count = 0
            for post in tag.get_posts_resumable():
                if post.date_utc.replace(tzinfo=timezone.utc) < cutoff:
                    break
                if post.shortcode in seen_shortcodes:
                    continue
                if post.likes < min_likes_tag:
                    continue
                seen_shortcodes.add(post.shortcode)

                caption   = post.caption or ""
                brand_hit = _find_brand_in_caption(caption, brands)
                if not brand_hit:
                    count += 1
                    if count >= max_per_tag:
                        break
                    continue  # Only keep posts that mention a monitored brand

                event_type, sentiment_bump = _analyse_caption(caption, brand_hit)
                eng_bump  = _engagement_bump(post.likes, post.comments)
                severity  = max(1, min(10, 3 + sentiment_bump + eng_bump))

                results.append({
                    "source":      "instagram_hashtag",
                    "handle":      post.owner_username,
                    "shortcode":   post.shortcode,
                    "title":       f"#{hashtag} by @{post.owner_username}: {caption[:100]}",
                    "link":        f"https://www.instagram.com/p/{post.shortcode}/",
                    "published":   post.date_utc.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    "caption":     caption[:400],
                    "likes":       post.likes,
                    "comments":    post.comments,
                    "brand_hit":   brand_hit,
                    "event_type":  event_type,
                    "severity":    severity,
                    "risk_type":   "Consumer Sentiment" if event_type == "negative" else "Influencer / Community Content",
                    "impact_area": "reputation" if event_type == "negative" else "marketing",
                    "why_it_matters": (
                        f"#{hashtag} post mentioning {brand_hit} with {post.likes:,} likes — "
                        "Kenyan beauty community is reacting to this brand."
                    ),
                })
                count += 1
                if count >= max_per_tag:
                    break
            time.sleep(delay)
        except instaloader.exceptions.ConnectionException as e:
            if "429" in str(e) or "rate" in str(e).lower():
                logging.warning("Instagram: rate limited on hashtags — stopping.")
                break
            logging.warning(f"Instagram: error fetching #{hashtag} — {e}")
        except Exception as e:
            logging.warning(f"Instagram: error fetching #{hashtag} — {e}")

    # ── Monitor Kenya influencer accounts ─────────────────────────
    for handle in cfg.get("kenya_influencers", []):
        try:
            logging.info(f"Instagram: checking influencer @{handle}...")
            profile = instaloader.Profile.from_username(L.context, handle)
            count   = 0
            for post in profile.get_posts():
                if post.date_utc.replace(tzinfo=timezone.utc) < cutoff:
                    break
                if post.shortcode in seen_shortcodes:
                    continue
                seen_shortcodes.add(post.shortcode)

                caption   = post.caption or ""
                brand_hit = _find_brand_in_caption(caption, brands)
                if not brand_hit:
                    count += 1
                    if count >= max_per_acct:
                        break
                    continue  # Only keep influencer posts that mention a brand

                event_type, sentiment_bump = _analyse_caption(caption, brand_hit)
                eng_bump  = _engagement_bump(post.likes, post.comments)
                # Influencer posts get +1 base since they have local Kenya reach
                severity  = max(1, min(10, 4 + sentiment_bump + eng_bump))

                results.append({
                    "source":      "instagram_influencer",
                    "handle":      handle,
                    "shortcode":   post.shortcode,
                    "title":       f"[KE Influencer] @{handle} mentions {brand_hit}: {caption[:80]}",
                    "link":        f"https://www.instagram.com/p/{post.shortcode}/",
                    "published":   post.date_utc.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    "caption":     caption[:400],
                    "likes":       post.likes,
                    "comments":    post.comments,
                    "brand_hit":   brand_hit,
                    "event_type":  event_type,
                    "severity":    severity,
                    "risk_type":   "Kenya Influencer Review",
                    "impact_area": "reputation" if event_type == "negative" else "marketing",
                    "why_it_matters": (
                        f"Kenyan beauty influencer @{handle} mentioned {brand_hit}. "
                        "Their audience is your target market — this directly affects purchase intent."
                    ),
                })
                count += 1
                if count >= max_per_acct:
                    break
            time.sleep(delay)
        except instaloader.exceptions.ProfileNotExistsException:
            logging.warning(f"Instagram: influencer @{handle} not found — skipping.")
        except instaloader.exceptions.ConnectionException as e:
            if "429" in str(e) or "rate" in str(e).lower():
                logging.warning("Instagram: rate limited — stopping influencer fetch.")
                break
            logging.warning(f"Instagram: error fetching @{handle} — {e}")
        except Exception as e:
            logging.warning(f"Instagram: error on influencer @{handle} — {e}")

    logging.info(f"Instagram direct: {len(results)} post(s) fetched.")
    return results


# ── Layer 2: Social signal RSS feeds (no login) ───────────────────────────────

def _parse_rss_feed(url: str, timeout: int = 15) -> List[Dict[str, Any]]:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (SkincareIntel/1.0)"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        channel = root.find("channel")
        items   = channel.findall("item") if channel else []
        results = []
        for it in items:
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link") or "").strip()
            pub   = (it.findtext("pubDate") or "").strip()
            desc  = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
            desc  = re.sub(r"\s+", " ", desc).strip()
            if title or link:
                results.append({"title": title, "link": link, "published": pub, "summary": desc[:400]})
        return results
    except Exception as e:
        logging.warning(f"Instagram/Social RSS: fetch error for {url[:60]} — {e}")
        return []


SOCIAL_NEGATIVE_WORDS: List[str] = [
    "recall", "fake", "counterfeit", "dangerous", "unsafe", "scam", "fraud",
    "ban", "warning", "reaction", "burn", "rash", "allergy", "harmful",
    "toxic", "side effect", "broke out", "lawsuit", "complaint", "boycott",
    "contaminated", "investigation", "substandard", "falsified",
]

SOCIAL_POSITIVE_WORDS: List[str] = [
    "launch", "viral", "trending", "new product", "review", "haul",
    "collaboration", "partnership", "award", "best", "love",
]


def _score_social_rss_item(
    title: str,
    summary: str,
    brands: List[Any],
    base_severity: int,
) -> Optional[Dict[str, Any]]:
    """Score a social RSS item. Returns None if not relevant."""
    full_text = f"{title} {summary}".lower()

    # Must mention a social platform
    platforms = ["instagram", "tiktok", "facebook", "twitter", "x.com",
                 "snapchat", "youtube", "whatsapp", "influencer", "social media",
                 "viral", "trending"]
    if not any(p in full_text for p in platforms):
        return None

    # Must be beauty/skincare relevant
    beauty_words = ["skincare", "beauty", "cosmetic", "lotion", "cream", "serum",
                    "makeup", "skin", "hair", "moisturizer", "sunscreen", "bleach",
                    "whitening", "foundation", "lipstick", "nail", "fragrance"]
    if not any(b in full_text for b in beauty_words):
        return None

    neg_hits = sum(1 for w in SOCIAL_NEGATIVE_WORDS if w in full_text)
    pos_hits = sum(1 for w in SOCIAL_POSITIVE_WORDS if w in full_text)

    if neg_hits > pos_hits:
        event_type = "negative"
        severity   = min(10, base_severity + neg_hits)
    elif pos_hits > 0:
        event_type = "positive"
        severity   = base_severity
    else:
        return None  # No clear signal

    # Which platform?
    platform = "Social Media"
    for p in ["TikTok", "Instagram", "Facebook", "Twitter", "WhatsApp", "YouTube"]:
        if p.lower() in full_text:
            platform = p
            break

    # Which brand?
    brand_hit = None
    for brand in brands:
        aliases = [brand.name] + (brand.aliases or [])
        if any(a and _alias_matches(full_text, a, brand.name) for a in aliases):
            brand_hit = brand.name
            break

    if event_type == "negative":
        risk_type      = f"{platform} Negative Signal"
        impact_area    = "reputation"
        why_it_matters = (
            f"Negative {platform} content has gone viral or been covered by news media. "
            "Social media signals at this scale indicate widespread consumer reaction "
            "that can rapidly damage brand trust in the Kenya market."
        )
    else:
        risk_type      = f"{platform} Brand Content"
        impact_area    = "marketing"
        why_it_matters = (
            f"{platform} brand content is getting media coverage — "
            "indicates significant reach and consumer engagement."
        )

    return {
        "event_type":    event_type,
        "severity":      severity,
        "risk_type":     risk_type,
        "impact_area":   impact_area,
        "brand_hit":     brand_hit,
        "platform":      platform,
        "why_it_matters": why_it_matters,
    }


def _fetch_social_rss_signals(
    cfg: Dict[str, Any],
    brands: List[Any],
) -> List[Dict[str, Any]]:
    """
    Layer 2: Fetch social media signals via Google News RSS.
    No login required. Catches viral moments covered by news media.
    """
    feeds    = cfg.get("social_rss_feeds", DEFAULT_CONFIG["social_rss_feeds"])
    results: List[Dict[str, Any]] = []
    seen_links: set = set()

    for feed in feeds:
        url      = feed.get("url", "")
        base_sev = int(feed.get("severity", 5))
        name     = feed.get("name", url[:40])

        if not url:
            continue

        logging.info(f"Instagram/Social RSS: fetching '{name}'...")
        raw_items = _parse_rss_feed(url)
        time.sleep(0.5)

        for raw in raw_items:
            link  = raw.get("link", "")
            title = raw.get("title", "")
            if link in seen_links or not title:
                continue
            seen_links.add(link)

            scored = _score_social_rss_item(
                title, raw.get("summary", ""), brands, base_sev
            )
            if not scored:
                continue

            results.append({
                "source":        "social_rss",
                "feed_name":     name,
                "title":         title,
                "link":          link,
                "published":     raw.get("published", ""),
                "caption":       raw.get("summary", "")[:300],
                "brand_hit":     scored["brand_hit"],
                "event_type":    scored["event_type"],
                "severity":      scored["severity"],
                "risk_type":     scored["risk_type"],
                "impact_area":   scored["impact_area"],
                "why_it_matters": scored["why_it_matters"],
                "platform":      scored["platform"],
            })

    logging.info(f"Instagram/Social RSS: {len(results)} signal(s) from {len(feeds)} feed(s).")
    return results


# ── Convert raw results to ScoredItems ───────────────────────────────────────

def _to_scored_items(raw_results: List[Dict[str, Any]], brands: List[Any]) -> List[Any]:
    from agent import ScoredItem, Item

    items: List[ScoredItem] = []
    for r in raw_results:
        source   = r.get("source", "instagram")
        platform = r.get("platform", "Instagram")
        handle   = r.get("handle", "")
        likes    = r.get("likes", 0)
        comments = r.get("comments", 0)

        # Source icon for the report
        if "tiktok" in source.lower() or platform == "TikTok":
            icon = "🎵"
        elif "facebook" in source.lower() or platform == "Facebook":
            icon = "👥"
        elif "whatsapp" in source.lower() or platform == "WhatsApp":
            icon = "💬"
        else:
            icon = "📸"

        engagement_str = f" · {likes:,} likes" if likes else ""
        summary = (
            f"{icon} {platform}"
            + (f" @{handle}" if handle else "")
            + engagement_str
            + f" · {r.get('caption', r.get('title', ''))[:200]}"
        )

        item = Item(
            title=r["title"],
            link=r["link"],
            published=r.get("published", ""),
            source=f"https://www.instagram.com" if "instagram" in source else r.get("link", ""),
            summary=summary,
        )

        reasons = [
            f"{'Instagram' if 'instagram' in source else platform}: {r['risk_type']}",
        ]
        if handle:
            reasons.append(f"Account: @{handle}")
        if likes:
            reasons.append(f"Engagement: {likes:,} likes, {comments:,} comments")
        reasons.append(f"Severity: {r['severity']}/10")

        si = ScoredItem(
            item=item,
            brand_hit=r.get("brand_hit"),
            alias_matched=r.get("brand_hit"),
            keyword_hits=[],
            severity=r["severity"],
            impact_area=r["impact_area"],
            risk_type=r["risk_type"],
            reasons=reasons,
            lane="brand" if r.get("brand_hit") else "industry",
            event_type=r["event_type"],
            why_it_matters=r["why_it_matters"],
            matched_areas={r["impact_area"]: 1},
        )
        items.append(si)
        logging.info(
            f"{'Instagram' if 'instagram' in source else platform}: "
            f"[{si.severity}/10] {si.event_type.upper()} — {r['title'][:70]}"
        )

    return items


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_instagram_items(brands: List[Any]) -> List[Any]:
    """
    Called from agent.py main(). Returns ScoredItems from:
      - Layer 1: Instagram direct (brand accounts, hashtags, influencers) — needs login
      - Layer 2: Social signal RSS feeds (TikTok/Instagram/Facebook/WhatsApp news) — always runs

    Install dependency: pip install instaloader
    """
    cfg = load_instagram_config()
    if not cfg:
        return []

    min_sev     = int(cfg.get("min_severity_to_include", 3))
    raw_results: List[Dict[str, Any]] = []

    # Layer 1: Instagram direct
    try:
        direct = _fetch_instagram_direct(cfg, brands)
        raw_results.extend(direct)
    except Exception as e:
        logging.warning(f"Instagram Layer 1: unexpected error — {e}")

    # Layer 2: Social signal RSS (always runs)
    try:
        social = _fetch_social_rss_signals(cfg, brands)
        raw_results.extend(social)
    except Exception as e:
        logging.warning(f"Instagram Layer 2 (social RSS): unexpected error — {e}")

    # Filter by minimum severity
    raw_results = [r for r in raw_results if r.get("severity", 0) >= min_sev]

    scored = _to_scored_items(raw_results, brands)
    logging.info(
        f"Instagram/Social: {len(scored)} total signal(s) "
        f"(direct={sum(1 for r in raw_results if r.get('source','').startswith('instagram_'))}, "
        f"rss={sum(1 for r in raw_results if r.get('source') == 'social_rss')})"
    )
    return scored