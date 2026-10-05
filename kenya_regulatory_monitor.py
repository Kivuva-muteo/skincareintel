"""
kenya_regulatory_monitor.py — KEBS & PPB enforcement monitoring for SkincareIntel.

Monitors Kenya Bureau of Standards (KEBS) and Pharmacy and Poisons Board (PPB)
for enforcement actions, product recalls, safety alerts, and gazette notices
affecting cosmetics and personal care products sold in Kenya.

These are the two bodies Kenyan brands actually answer to. A KEBS market
surveillance action or a PPB import restriction is a far more severe signal
than a US FDA notice for a brand operating in Kenya.

Sources:
  - KEBS: https://www.kebs.org (market surveillance, standards alerts)
  - PPB: https://www.pharmacyboardkenya.org (product recalls, import restrictions)
  - Kenya Gazette (eKLR) for legal notices
  - Kenya Law RSS for regulatory instruments
  - Google News filtered for KEBS/PPB cosmetics

No API key required — uses public pages and RSS feeds.
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

REGULATORY_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "regulatory_config.json"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "sources": [
        # KEBS public notices — check for cosmetics/beauty enforcement
        {
            "name":     "KEBS Market Surveillance",
            "type":     "scrape",
            "url":      "https://www.kebs.org/index.php/market-surveillance/",
            "label":    "KEBS",
            "severity": 8,
        },
        {
            "name":     "KEBS Standards Alerts",
            "type":     "scrape",
            "url":      "https://www.kebs.org/index.php/alerts/",
            "label":    "KEBS",
            "severity": 7,
        },
        # PPB notices
        {
            "name":     "PPB Product Recalls",
            "type":     "scrape",
            "url":      "https://www.pharmacyboardkenya.org/recalls/",
            "label":    "PPB",
            "severity": 9,
        },
        {
            "name":     "PPB Safety Alerts",
            "type":     "scrape",
            "url":      "https://www.pharmacyboardkenya.org/safety-alerts/",
            "label":    "PPB",
            "severity": 8,
        },
        # Google News RSS — KEBS/PPB cosmetics mentions
        {
            "name":     "Google News: KEBS cosmetics",
            "type":     "rss",
            "url":      "https://news.google.com/rss/search?q=KEBS+cosmetics+Kenya&hl=en&gl=KE&ceid=KE:en",
            "label":    "KEBS/News",
            "severity": 6,
        },
        {
            "name":     "Google News: PPB recall Kenya",
            "type":     "rss",
            "url":      "https://news.google.com/rss/search?q=PPB+recall+Kenya+cosmetics&hl=en&gl=KE&ceid=KE:en",
            "label":    "PPB/News",
            "severity": 7,
        },
        {
            "name":     "Google News: Kenya cosmetics ban",
            "type":     "rss",
            "url":      "https://news.google.com/rss/search?q=Kenya+cosmetics+ban+recall+2026&hl=en&gl=KE&ceid=KE:en",
            "label":    "Regulatory/News",
            "severity": 6,
        },
        {
            "name":     "Standard Media: KEBS",
            "type":     "rss",
            "url":      "https://www.standardmedia.co.ke/rss/business.php",
            "label":    "Standard/KEBS",
            "severity": 5,
        },
        {
            "name":     "Kenya Gazette eKLR",
            "type":     "rss",
            "url":      "https://kenyalaw.org/kl/fileadmin/pdfdownloads/gazettes/rss/",
            "label":    "Kenya Gazette",
            "severity": 7,
        },
    ],
    "max_age_days": 14,
    "min_severity_to_include": 5,
}

# ── Keyword sets ──────────────────────────────────────────────────────────────

KEBS_TRIGGER_WORDS: List[str] = [
    "kebs", "kenya bureau of standards", "market surveillance",
    "substandard", "counterfeit", "non-compliant", "standards mark",
    "cs mark", "banned product", "product seizure", "market withdrawal",
    "unsafe product", "enforcement action", "market raid",
]

PPB_TRIGGER_WORDS: List[str] = [
    "ppb", "pharmacy board", "pharmacy and poisons",
    "product recall", "import restriction", "product ban",
    "manufacturing defect", "safety alert", "post-market surveillance",
    "unregistered product", "counterfeit drug", "adverse event",
]

COSMETICS_SCOPE_WORDS: List[str] = [
    "cosmetic", "cosmetics", "skincare", "skin care", "beauty product",
    "lotion", "cream", "serum", "sunscreen", "spf", "makeup",
    "hair care", "shampoo", "conditioner", "soap", "deodorant",
    "fragrance", "perfume", "bleaching cream", "skin lightening",
    "whitening cream", "personal care",
]

HIGH_SEVERITY_TRIGGERS: List[str] = [
    "recall", "ban", "banned", "seizure", "seized", "unsafe",
    "health risk", "toxic", "dangerous", "contaminated", "counterfeit",
    "withdrawn from market", "do not use", "stop selling",
]


# ── Config loading ────────────────────────────────────────────────────────────

def load_regulatory_config(path: str = REGULATORY_CONFIG_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logging.info(f"Regulatory: wrote default config to {path}")
        except Exception:
            pass
        return DEFAULT_CONFIG

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("enabled", True):
            logging.info("Regulatory: disabled in regulatory_config.json — skipping.")
            return None
        return cfg
    except Exception as e:
        logging.warning(f"Regulatory: could not load config — {e}")
        return None


# ── Fetch helpers ─────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (SkincareIntel/1.0 regulatory monitor)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-KE,en;q=0.9",
}


def _fetch(url: str, timeout: int = 20) -> Optional[bytes]:
    try:
        req = Request(url, headers=_HEADERS)
        # PPB website has an expired SSL cert — bypass verification for known gov sites
        if "pharmacyboardkenya.org" in url:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as e:
        logging.warning(f"Regulatory: HTTP {e.code} for {url}")
        return None
    except (URLError, OSError) as e:
        logging.warning(f"Regulatory: fetch error for {url} — {e}")
        return None


def _strip_tags(text: str) -> str:
    text = re.sub(r"<script.*?>.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?>.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── RSS parsing ───────────────────────────────────────────────────────────────

def _parse_rss(xml_bytes: bytes, source_label: str, max_items: int = 30) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items

    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else []

    # Atom fallback
    if not entries:
        ns = "http://www.w3.org/2005/Atom"
        for entry in root.findall(f"{{{ns}}}entry")[:max_items]:
            title = (entry.findtext(f"{{{ns}}}title") or "").strip()
            link_el = entry.find(f"{{{ns}}}link")
            link  = (link_el.attrib.get("href", "") if link_el is not None else "")
            pub   = (entry.findtext(f"{{{ns}}}updated") or "").strip()
            desc  = (entry.findtext(f"{{{ns}}}summary") or "").strip()
            items.append({"title": title, "link": link, "published": pub, "summary": _strip_tags(desc)[:500]})
        return items

    for it in entries[:max_items]:
        title = (it.findtext("title") or "").strip()
        link  = (it.findtext("link") or "").strip()
        pub   = (it.findtext("pubDate") or "").strip()
        desc  = (it.findtext("description") or "").strip()
        items.append({"title": title, "link": link, "published": pub, "summary": _strip_tags(desc)[:500]})

    return items


# ── HTML scraping ─────────────────────────────────────────────────────────────

def _scrape_notices(html_bytes: bytes, source_label: str) -> List[Dict[str, Any]]:
    """
    Extracts enforcement/recall/safety notices from KEBS and PPB pages.
    Uses a strict allowlist of signal words so navigation links, FAQs,
    committee pages, and other noise are discarded before they score.
    """
    html_text = html_bytes.decode("utf-8", errors="replace")
    items: List[Dict[str, Any]] = []
    now_str = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    # ── Words that a real enforcement/recall notice title must contain ────────
    NOTICE_SIGNAL_WORDS: List[str] = [
        "recall", "withdrawal", "seized", "seizure", "ban", "banned",
        "unsafe", "substandard", "counterfeit", "falsified", "fake",
        "alert", "warning", "safety", "hazard", "toxic", "contamina",
        "prohibited", "enforcement", "surveillance", "violation",
        "penalty", "fine", "action", "investigation", "notice",
        "suspend", "revoke", "import restriction", "do not use",
        # cosmetics-specific
        "cosmetic", "skincare", "beauty product", "lotion", "cream",
        "bleaching", "whitening", "skin lightening", "sunscreen",
        "hair care", "makeup", "personal care",
    ]

    # ── Hard skip list — nav/structural page elements ─────────────────────────
    NAV_SKIP_WORDS: List[str] = [
        "home", "about us", "contact", "login", "sign in", "register",
        "search", "sitemap", "privacy policy", "terms", "cookie",
        "faq", "frequently asked", "have any questions", "toll free",
        "our services", "who we are", "leadership", "board of directors",
        "annual report", "newsletter", "subscribe", "follow us",
        "national standards council", "technical committee",
        "stakeholder engagement", "apply for", "application for",
        "feedback form", "call for comments", "wto/tbt",
        "public review", "systematic review", "codex",
        "verify products", "smark", "dmark", "fmark",
        "corporate social responsibility", "quality assurance faq",
        "pre-export verification", "iec", "iso",
        "vaccines and biologicals", "medical devices",
        "herbal and complementary", "exports and imports permit",
        "inspection and compliance", "clinical trials",
        "product advertisements", "stakeholder feedback",
        "incb", "antimicro", "pharmacovigilance",
        "regional training", "national regulators conference",
        "partner", "showcase", "collaboration", "progress in",
    ]

    patterns = [
        r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{10,300})\s*</a>',
        r'<td[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]{10,300})</a>',
        r'<li[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]{10,300})</a>',
    ]

    seen_links = set()
    for pattern in patterns:
        for link_m, title_m in re.findall(pattern, html_text, re.S | re.I):
            link  = link_m.strip()
            title = re.sub(r"\s+", " ", title_m).strip()

            if not title or len(title) < 8:
                continue
            if link in seen_links:
                continue

            title_lower = title.lower()

            # Hard skip navigation and structural noise
            if any(skip in title_lower for skip in NAV_SKIP_WORDS):
                continue

            # Must contain at least one enforcement/recall signal word
            if not any(signal in title_lower for signal in NOTICE_SIGNAL_WORDS):
                continue

            # Skip mailto, javascript, anchors
            if link.startswith(("mailto:", "javascript:", "#", "tel:")):
                continue

            # Resolve relative URLs
            if link.startswith("/"):
                if "kebs" in source_label.lower():
                    link = "https://www.kebs.org" + link
                elif "ppb" in source_label.lower() or "pharmacy" in source_label.lower():
                    link = "https://www.pharmacyboardkenya.org" + link

            seen_links.add(link)
            items.append({
                "title":     title,
                "link":      link,
                "published": now_str,
                "summary":   f"[{source_label}] {title}",
            })

    return items[:15]  # Cap at 15 genuine notices per page


# ── Relevance scoring ─────────────────────────────────────────────────────────

def _is_cosmetics_relevant(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in COSMETICS_SCOPE_WORDS)


def _is_regulatory_trigger(text: str) -> bool:
    t = text.lower()
    kebs_hit = any(w in t for w in KEBS_TRIGGER_WORDS)
    ppb_hit  = any(w in t for w in PPB_TRIGGER_WORDS)
    return kebs_hit or ppb_hit


# Parent companies can appear in unrelated food, medicine, or household-product
# notices. A bare parent-company match is therefore not enough to imply cosmetics.
GENERIC_PARENT_BRANDS = {"unilever", "procter & gamble"}


def _brand_match_implies_cosmetics(brand_match: Optional[Tuple[str, str]]) -> bool:
    if not brand_match:
        return False
    brand_name, alias = brand_match
    if brand_name.lower() in GENERIC_PARENT_BRANDS and alias.lower() == brand_name.lower():
        return False
    return True


def _is_age_ok(pub_str: str, max_days: int) -> bool:
    """Return True if article is recent enough, or if date is unparseable."""
    if not pub_str:
        return True
    # Try common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S +0000",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            pub_str_clean = pub_str.replace("GMT", "+0000").replace("UTC", "+0000")
            dt = datetime.strptime(pub_str_clean, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
            return dt >= cutoff
        except ValueError:
            continue
    return True  # Can't parse → include


def _calculate_severity(
    text: str,
    base_severity: int,
    brand_matched: bool,
) -> Tuple[int, str]:
    """
    Returns (severity, risk_type) for a regulatory item.
    Higher base severity for KEBS/PPB sources.
    """
    t = text.lower()

    high_hits = sum(1 for w in HIGH_SEVERITY_TRIGGERS if w in t)
    kebs_hit  = any(w in t for w in KEBS_TRIGGER_WORDS)
    ppb_hit   = any(w in t for w in PPB_TRIGGER_WORDS)

    severity = base_severity + min(high_hits, 2)
    if brand_matched:
        severity += 2

    if ppb_hit and high_hits >= 1:
        risk_type = "PPB Recall / Safety Alert"
    elif kebs_hit and high_hits >= 1:
        risk_type = "KEBS Enforcement Action"
    elif ppb_hit:
        risk_type = "PPB Regulatory Notice"
    elif kebs_hit:
        risk_type = "KEBS Standards Notice"
    else:
        risk_type = "Kenya Regulatory Alert"

    return min(10, max(1, severity)), risk_type


AMBIGUOUS_BRAND_CUES: Dict[str, List[str]] = {
    "the ordinary": ["skincare", "skin care", "serum", "niacinamide", "retinol", "glycolic", "salicylic", "hyaluronic", "deciem", "peeling", "cleanser", "moisturizer", "cosmetic"],
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


def _find_brand_in_text(text: str, brands: List[Any]) -> Optional[Tuple[str, str]]:
    """Returns (brand_name, alias) only for a complete, context-valid match."""
    lower_text = text.lower()
    for brand in brands:
        aliases = [brand.name] + (brand.aliases or [])
        for alias in sorted((a for a in aliases if a), key=len, reverse=True):
            if not _alias_matches(text, alias):
                continue
            cues = AMBIGUOUS_BRAND_CUES.get(alias.lower()) or AMBIGUOUS_BRAND_CUES.get(brand.name.lower())
            if cues and not any(cue in lower_text for cue in cues):
                continue
            return brand.name, alias
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_regulatory_items(
    brands: List[Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    Called from agent.py main(). Returns ScoredItems for KEBS/PPB regulatory
    actions relevant to monitored brands or the Kenya cosmetics industry.

    Items are always added to the "industry" lane unless a specific brand is
    named, in which case they go to "brand" lane with elevated severity.
    """
    from agent import ScoredItem, Item

    reg_cfg = load_regulatory_config()
    if not reg_cfg:
        return []

    sources    = reg_cfg.get("sources", DEFAULT_CONFIG["sources"])
    max_age    = int(reg_cfg.get("max_age_days", 14))
    min_sev    = int(reg_cfg.get("min_severity_to_include", 5))

    results: List[ScoredItem] = []
    seen_links: set = set()

    for source in sources:
        stype    = source.get("type", "rss")
        url      = source.get("url", "")
        label    = source.get("label", "Kenya Regulatory")
        base_sev = int(source.get("severity", 6))

        if not url:
            continue

        logging.info(f"Regulatory: fetching {label} — {url[:70]}...")
        raw = _fetch(url)
        time.sleep(1.0)

        if not raw:
            continue

        if stype == "rss":
            raw_items = _parse_rss(raw, label)
        else:
            raw_items = _scrape_notices(raw, label)

        for raw_item in raw_items:
            link  = raw_item.get("link", "")
            title = raw_item.get("title", "")
            pub   = raw_item.get("published", "")
            desc  = raw_item.get("summary", "")

            if not title:
                continue
            if link and link in seen_links:
                continue
            if link:
                seen_links.add(link)

            if not _is_age_ok(pub, max_age):
                continue

            full_text = f"{title} {desc}"
            brand_match = _find_brand_in_text(full_text, brands)

            # Every source must be both regulatory and cosmetics-relevant. A
            # direct beauty-brand/product alias can supply the cosmetics context,
            # but a bare parent-company name cannot.
            is_relevant = (
                _is_regulatory_trigger(full_text)
                and (
                    _is_cosmetics_relevant(full_text)
                    or _brand_match_implies_cosmetics(brand_match)
                )
            )

            if not is_relevant:
                continue
            severity, risk_type = _calculate_severity(full_text, base_sev, brand_match is not None)

            if severity < min_sev:
                continue

            if brand_match:
                brand_name, alias_matched = brand_match
                lane = "brand"
                brand_hit = brand_name
                why_it_matters = (
                    f"KEBS or PPB regulatory action naming {brand_name} directly. "
                    "This is the highest-priority signal for a Kenya-market brand — "
                    "requires immediate response from compliance and management."
                )
            else:
                brand_hit     = None
                alias_matched = None
                lane          = "industry"
                why_it_matters = (
                    "Kenya regulatory body (KEBS or PPB) has issued a notice affecting "
                    "the cosmetics/personal care sector. This may set precedents or "
                    "indicate increased market surveillance across the industry."
                )

            now_str = pub or datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

            item = Item(
                title=f"[{label}] {title}",
                link=link or url,
                published=now_str,
                source=url,
                summary=f"🏛️ {label} · {desc[:400] or title}",
            )

            reasons = [
                f"Kenya regulatory source: {label}",
                f"Risk type: {risk_type}",
            ]
            if brand_match:
                reasons.append(f"Brand named: {brand_match[0]} (matched: {brand_match[1]})")
            reasons.append(f"Severity: {severity}/10 (base from {label}: {base_sev})")

            si = ScoredItem(
                item=item,
                brand_hit=brand_hit,
                alias_matched=alias_matched,
                keyword_hits=[],
                severity=severity,
                impact_area="regulatory",
                risk_type=risk_type,
                reasons=reasons,
                lane=lane,
                event_type="negative",
                why_it_matters=why_it_matters,
                matched_areas={"regulatory": 1},
            )
            results.append(si)
            logging.info(
                f"Regulatory: [{severity}/10] {risk_type} — {title[:70]} "
                f"({'brand: ' + brand_hit if brand_hit else 'industry'})"
            )

    logging.info(f"Regulatory: {len(results)} item(s) from {len(sources)} source(s).")
    return results