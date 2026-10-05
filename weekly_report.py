"""
weekly_report.py — Weekly PDF summary + competitor comparison dashboard.
Generates every Friday. Called from agent.py main().
"""

from __future__ import annotations

import logging
import os
import json
import smtplib
import ssl
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR  = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ── Colour palette ───────────────────────────────────────────────────────────
C_NAVY      = colors.HexColor("#0f172a")
C_ACCENT    = colors.HexColor("#1d4ed8")
C_RED       = colors.HexColor("#dc2626")
C_ORANGE    = colors.HexColor("#d97706")
C_GREEN     = colors.HexColor("#059669")
C_PURPLE    = colors.HexColor("#7c3aed")
C_LIGHT_BG  = colors.HexColor("#f8fafc")
C_BORDER    = colors.HexColor("#e2e8f0")
C_MUTED     = colors.HexColor("#64748b")
C_WHITE     = colors.white
C_ROW_ALT   = colors.HexColor("#f1f5f9")


# ── Styles ───────────────────────────────────────────────────────────────────

def build_styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle("cover_title",
            fontSize=32, fontName="Helvetica-Bold",
            textColor=C_WHITE, alignment=TA_CENTER, leading=38),
        "cover_sub": ParagraphStyle("cover_sub",
            fontSize=13, fontName="Helvetica",
            textColor=colors.HexColor("#cbd5e1"), alignment=TA_CENTER, leading=18),
        "cover_meta": ParagraphStyle("cover_meta",
            fontSize=10, fontName="Helvetica",
            textColor=colors.HexColor("#94a3b8"), alignment=TA_CENTER),
        "section_title": ParagraphStyle("section_title",
            fontSize=15, fontName="Helvetica-Bold",
            textColor=C_NAVY, spaceBefore=18, spaceAfter=8, leading=20),
        "subsection": ParagraphStyle("subsection",
            fontSize=11, fontName="Helvetica-Bold",
            textColor=C_ACCENT, spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("body",
            fontSize=9, fontName="Helvetica",
            textColor=colors.HexColor("#374151"), leading=14, spaceAfter=4),
        "body_bold": ParagraphStyle("body_bold",
            fontSize=9, fontName="Helvetica-Bold",
            textColor=C_NAVY, leading=14),
        "small": ParagraphStyle("small",
            fontSize=8, fontName="Helvetica",
            textColor=C_MUTED, leading=12),
        "kpi_val": ParagraphStyle("kpi_val",
            fontSize=28, fontName="Helvetica-Bold",
            textColor=C_ACCENT, alignment=TA_CENTER, leading=32),
        "kpi_label": ParagraphStyle("kpi_label",
            fontSize=8, fontName="Helvetica",
            textColor=C_MUTED, alignment=TA_CENTER, leading=12),
        "tag_neg": ParagraphStyle("tag_neg",
            fontSize=8, fontName="Helvetica-Bold",
            textColor=C_RED),
        "tag_pos": ParagraphStyle("tag_pos",
            fontSize=8, fontName="Helvetica-Bold",
            textColor=C_GREEN),
        "footer": ParagraphStyle("footer",
            fontSize=7, fontName="Helvetica",
            textColor=C_MUTED, alignment=TA_CENTER),
    }


# ── Data helpers ─────────────────────────────────────────────────────────────

def _sev_label(s: int) -> str:
    if s >= 7: return "HIGH"
    if s >= 5: return "MEDIUM"
    if s >= 3: return "LOW"
    return "INFO"


def aggregate_by_brand(items: List[Any]) -> Dict[str, Dict[str, Any]]:
    brands: Dict[str, Dict[str, Any]] = {}
    for si in items:
        name = si.brand_hit or "Industry"
        if name not in brands:
            brands[name] = {
                "total": 0, "negative": 0, "positive": 0, "neutral": 0,
                "max_severity": 0, "total_severity": 0,
                "risk_types": defaultdict(int),
                "top_stories": [],
                "youtube": [],
            }
        b = brands[name]
        b["total"] += 1
        b[si.event_type] = b.get(si.event_type, 0) + 1
        b["max_severity"] = max(b["max_severity"], si.severity)
        b["total_severity"] += si.severity
        b["risk_types"][si.risk_type] += 1
        if si.item.source and "youtube" in si.item.source.lower():
            b["youtube"].append(si)
        else:
            b["top_stories"].append(si)
    # Sort stories by severity
    for b in brands.values():
        b["top_stories"].sort(key=lambda x: x.severity, reverse=True)
        b["youtube"].sort(key=lambda x: x.severity, reverse=True)
    return brands


def overall_sentiment(items: List[Any]) -> Tuple[int, int, int]:
    neg = sum(1 for i in items if i.event_type == "negative")
    pos = sum(1 for i in items if i.event_type == "positive")
    neu = sum(1 for i in items if i.event_type == "neutral")
    return neg, pos, neu


def top_stories_week(items: List[Any], n: int = 5) -> List[Any]:
    neg = [i for i in items if i.event_type == "negative"]
    neg.sort(key=lambda x: x.severity, reverse=True)
    return neg[:n]


def youtube_highlights(items: List[Any], n: int = 5) -> List[Any]:
    yt = [i for i in items if i.item.source and "youtube" in i.item.source.lower()]
    yt.sort(key=lambda x: x.severity, reverse=True)
    return yt[:n]


# ── PDF building blocks ──────────────────────────────────────────────────────

def _kpi_table(kpis: List[Tuple[str, str, Any]], styles: Dict) -> Table:
    """A row of KPI boxes: [(value, label, color), ...]"""
    val_cells  = [Paragraph(str(v), ParagraphStyle("kv",
        fontSize=26, fontName="Helvetica-Bold",
        textColor=c, alignment=TA_CENTER, leading=30)) for v, l, c in kpis]
    lbl_cells  = [Paragraph(l, styles["kpi_label"]) for v, l, c in kpis]
    data       = [val_cells, lbl_cells]
    col_width  = 16.5 * cm / len(kpis)
    t = Table(data, colWidths=[col_width] * len(kpis))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_LIGHT_BG),
        ("BOX",        (0, 0), (-1, -1), 0.5, C_BORDER),
        ("INNERGRID",  (0, 0), (-1, -1), 0.5, C_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _bar(ratio: float, width_cm: float = 6.0, color: Any = C_ACCENT) -> Table:
    """Simple horizontal bar as a single-cell table."""
    filled = max(0.01, min(1.0, ratio)) * width_cm * cm
    empty  = max(0.0, width_cm * cm - filled)
    data   = [[""]]
    t = Table(data, colWidths=[filled])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), color),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _sentiment_bar_table(neg: int, pos: int, neu: int, styles: Dict) -> Table:
    total = max(1, neg + pos + neu)
    rows = []
    for label, count, color in [
        ("Negative", neg, C_RED),
        ("Positive", pos, C_GREEN),
        ("Neutral",  neu, C_MUTED),
    ]:
        pct   = count / total
        bar_w = pct * 10
        bar_t = Table([[""]], colWidths=[max(0.05, bar_w) * cm])
        bar_t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), color),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        rows.append([
            Paragraph(label, styles["body"]),
            bar_t,
            Paragraph(f"{count}  ({pct:.0%})", styles["body"]),
        ])
    t = Table(rows, colWidths=[3*cm, 10.5*cm, 3*cm])
    t.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBEFORE",    (1, 0), (1, -1), 0.3, C_BORDER),
    ]))
    return t


def _story_table(items: List[Any], styles: Dict, max_rows: int = 5) -> Table:
    header = [
        Paragraph("Title", styles["body_bold"]),
        Paragraph("Brand", styles["body_bold"]),
        Paragraph("Severity", styles["body_bold"]),
        Paragraph("Type", styles["body_bold"]),
    ]
    rows = [header]
    for si in items[:max_rows]:
        sev_color = C_RED if si.severity >= 7 else C_ORANGE if si.severity >= 5 else C_MUTED
        title_text = (si.item.title or "")[:80] + ("…" if len(si.item.title or "") > 80 else "")
        rows.append([
            Paragraph(title_text, styles["small"]),
            Paragraph(si.brand_hit or "Industry", styles["small"]),
            Paragraph(f"{si.severity}/10  {_sev_label(si.severity)}",
                ParagraphStyle("sv", fontSize=8, fontName="Helvetica-Bold", textColor=sev_color)),
            Paragraph(si.risk_type or "", styles["small"]),
        ])
    t = Table(rows, colWidths=[8*cm, 2.5*cm, 2.5*cm, 3.5*cm])
    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]
    for i in range(2, len(rows), 2):
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), C_ROW_ALT))
    t.setStyle(TableStyle(style_cmds))
    return t


def _competitor_table(brand_data: Dict[str, Dict], styles: Dict) -> Table:
    """Rankings table sorted by risk score."""
    header = [
        Paragraph("#",          styles["body_bold"]),
        Paragraph("Brand",      styles["body_bold"]),
        Paragraph("Mentions",   styles["body_bold"]),
        Paragraph("Alerts",     styles["body_bold"]),
        Paragraph("Positive",   styles["body_bold"]),
        Paragraph("Max Sev",    styles["body_bold"]),
        Paragraph("Risk Score", styles["body_bold"]),
    ]
    # Risk score = weighted sum
    def risk_score(b):
        return b["negative"] * 3 + b["max_severity"] * 2 + b["total"]

    sorted_brands = sorted(
        [(name, data) for name, data in brand_data.items() if name != "Industry"],
        key=lambda x: risk_score(x[1]),
        reverse=True,
    )

    rows = [header]
    for rank, (name, b) in enumerate(sorted_brands, 1):
        rs = risk_score(b)
        rs_color = C_RED if rs >= 20 else C_ORANGE if rs >= 10 else C_GREEN
        rows.append([
            Paragraph(str(rank), styles["small"]),
            Paragraph(name, styles["body_bold"]),
            Paragraph(str(b["total"]),    styles["small"]),
            Paragraph(str(b["negative"]), ParagraphStyle("rn", fontSize=8,
                fontName="Helvetica-Bold", textColor=C_RED if b["negative"] > 0 else C_MUTED)),
            Paragraph(str(b["positive"]), ParagraphStyle("rp", fontSize=8,
                fontName="Helvetica-Bold", textColor=C_GREEN if b["positive"] > 0 else C_MUTED)),
            Paragraph(f"{b['max_severity']}/10", styles["small"]),
            Paragraph(str(rs), ParagraphStyle("rs", fontSize=9,
                fontName="Helvetica-Bold", textColor=rs_color)),
        ])

    t = Table(rows, colWidths=[0.8*cm, 4*cm, 2*cm, 2*cm, 2*cm, 2*cm, 2.7*cm])
    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (0, 0), (0, -1), "CENTER"),
        ("ALIGN",         (2, 0), (-1, -1), "CENTER"),
    ]
    for i in range(2, len(rows), 2):
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), C_ROW_ALT))
    # Highlight top row (most risky brand)
    if len(rows) > 1:
        style_cmds.append(("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#fef2f2")))
    t.setStyle(TableStyle(style_cmds))
    return t


def _brand_comparison_table(brand_data: Dict[str, Dict], styles: Dict) -> Table:
    """Side-by-side comparison with sentiment bars per brand."""
    filtered = {k: v for k, v in brand_data.items() if k != "Industry"}
    if not filtered:
        return Table([[Paragraph("No brand data this week.", styles["body"])]])

    # Sort by total mentions
    sorted_brands = sorted(filtered.items(), key=lambda x: x[1]["total"], reverse=True)

    header = [
        Paragraph("Brand",    styles["body_bold"]),
        Paragraph("Negative", styles["body_bold"]),
        Paragraph("Positive", styles["body_bold"]),
        Paragraph("Sentiment", styles["body_bold"]),
        Paragraph("Top Risk",  styles["body_bold"]),
    ]
    rows = [header]
    for name, b in sorted_brands:
        total = max(1, b["total"])
        neg_pct = b["negative"] / total
        pos_pct = b["positive"] / total
        # Sentiment bar: red portion | green portion
        bar_total_w = 6.0
        neg_w = max(0.05, neg_pct * bar_total_w)
        pos_w = max(0.05, pos_pct * bar_total_w)
        rem_w = max(0, bar_total_w - neg_w - pos_w)
        bar_data = [[
            Table([[""]], colWidths=[neg_w*cm],
                  style=TableStyle([("BACKGROUND",(0,0),(-1,-1),C_RED),
                                    ("TOPPADDING",(0,0),(-1,-1),4),
                                    ("BOTTOMPADDING",(0,0),(-1,-1),4)])),
            Table([[""]], colWidths=[pos_w*cm],
                  style=TableStyle([("BACKGROUND",(0,0),(-1,-1),C_GREEN),
                                    ("TOPPADDING",(0,0),(-1,-1),4),
                                    ("BOTTOMPADDING",(0,0),(-1,-1),4)])),
            Table([[""]], colWidths=[max(0.01,rem_w)*cm],
                  style=TableStyle([("BACKGROUND",(0,0),(-1,-1),C_BORDER),
                                    ("TOPPADDING",(0,0),(-1,-1),4),
                                    ("BOTTOMPADDING",(0,0),(-1,-1),4)])),
        ]]
        bar_t = Table(bar_data, colWidths=[bar_total_w*cm])
        bar_t.setStyle(TableStyle([("TOPPADDING",(0,0),(-1,-1),0),
                                   ("BOTTOMPADDING",(0,0),(-1,-1),0),
                                   ("LEFTPADDING",(0,0),(-1,-1),0),
                                   ("RIGHTPADDING",(0,0),(-1,-1),0)]))

        top_risk = max(b["risk_types"], key=b["risk_types"].get) if b["risk_types"] else "—"
        rows.append([
            Paragraph(name, styles["body_bold"]),
            Paragraph(str(b["negative"]), ParagraphStyle("cn", fontSize=9,
                fontName="Helvetica-Bold",
                textColor=C_RED if b["negative"] > 0 else C_MUTED, alignment=TA_CENTER)),
            Paragraph(str(b["positive"]), ParagraphStyle("cp", fontSize=9,
                fontName="Helvetica-Bold",
                textColor=C_GREEN if b["positive"] > 0 else C_MUTED, alignment=TA_CENTER)),
            bar_t,
            Paragraph(top_risk[:30], styles["small"]),
        ])

    t = Table(rows, colWidths=[3.5*cm, 2*cm, 2*cm, 6*cm, 4*cm])
    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (1, 0), (2, -1), "CENTER"),
    ]
    for i in range(2, len(rows), 2):
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), C_ROW_ALT))
    t.setStyle(TableStyle(style_cmds))
    return t


def _feed_health_table(runs: List[Dict], styles: Dict) -> Table:
    if not runs:
        return Table([[Paragraph("No run data available.", styles["body"])]])

    total_runs    = len(runs)
    total_fetched = sum(r.get("total_fetched", 0) for r in runs)
    avg_feeds_ok  = sum(r.get("feeds_ok", 0) for r in runs) / total_runs if total_runs else 0
    avg_feeds_tot = sum(r.get("feeds_total", 0) for r in runs) / total_runs if total_runs else 0

    summary_data = [
        [Paragraph("Runs this week", styles["body_bold"]),
         Paragraph(str(total_runs), styles["body"])],
        [Paragraph("Total articles scanned", styles["body_bold"]),
         Paragraph(f"{total_fetched:,}", styles["body"])],
        [Paragraph("Avg feeds healthy", styles["body_bold"]),
         Paragraph(f"{avg_feeds_ok:.0f} / {avg_feeds_tot:.0f}", styles["body"])],
    ]
    t = Table(summary_data, colWidths=[6*cm, 10.5*cm])
    t.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("LINEBELOW",     (0, 0), (-1, -2), 0.3, C_BORDER),
        ("BACKGROUND",    (0, 0), (0, -1), C_LIGHT_BG),
    ]))
    return t


# ── Cover page ────────────────────────────────────────────────────────────────

def _cover_page(story: list, week_label: str, styles: Dict, neg: int, pos: int, total: int) -> None:
    now = datetime.now()
    date_range = _week_date_range()

    # Navy background block using a table
    cover_data = [[
        Paragraph("SkincareIntel", ParagraphStyle("logo",
            fontSize=11, fontName="Helvetica-Bold",
            textColor=colors.HexColor("#60a5fa"), alignment=TA_CENTER)),
        Paragraph(" ", styles["cover_sub"]),
        Paragraph("Weekly Intelligence Report", styles["cover_title"]),
        Paragraph(week_label, styles["cover_sub"]),
        Paragraph(date_range, styles["cover_meta"]),
        Paragraph(" ", styles["cover_sub"]),
        Paragraph(f"{total} signals tracked  ·  {neg} risk alerts  ·  {pos} positive developments",
                  styles["cover_meta"]),
        Paragraph(" ", styles["cover_meta"]),
        Paragraph(f"Generated {now.strftime('%d %B %Y, %H:%M')}",
                  ParagraphStyle("gen", fontSize=9, fontName="Helvetica",
                                 textColor=colors.HexColor("#64748b"), alignment=TA_CENTER)),
    ]]

    cover_items = [
        Spacer(1, 3*cm),
        Paragraph("SkincareIntel", ParagraphStyle("logo2",
            fontSize=13, fontName="Helvetica-Bold",
            textColor=C_ACCENT, alignment=TA_CENTER)),
        Spacer(1, 0.4*cm),
        HRFlowable(width="60%", thickness=1, color=C_ACCENT, hAlign="CENTER"),
        Spacer(1, 0.6*cm),
        Paragraph("Weekly Intelligence Report", ParagraphStyle("ct",
            fontSize=28, fontName="Helvetica-Bold",
            textColor=C_NAVY, alignment=TA_CENTER, leading=34)),
        Spacer(1, 0.3*cm),
        Paragraph(week_label, ParagraphStyle("wl",
            fontSize=14, fontName="Helvetica",
            textColor=C_ACCENT, alignment=TA_CENTER)),
        Spacer(1, 0.2*cm),
        Paragraph(date_range, ParagraphStyle("dr",
            fontSize=10, fontName="Helvetica",
            textColor=C_MUTED, alignment=TA_CENTER)),
        Spacer(1, 1.5*cm),
        HRFlowable(width="100%", thickness=0.5, color=C_BORDER),
        Spacer(1, 1*cm),
    ]

    # KPI strip
    kpi_t = _kpi_table([
        (str(total), "Total Signals",  C_ACCENT),
        (str(neg),   "Risk Alerts",    C_RED),
        (str(pos),   "Positive",       C_GREEN),
    ], styles)
    cover_items.append(kpi_t)
    cover_items.append(Spacer(1, 1*cm))
    cover_items.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    cover_items.append(Spacer(1, 0.5*cm))
    cover_items.append(Paragraph(
        f"Generated {now.strftime('%d %B %Y at %H:%M')}  ·  Confidential",
        ParagraphStyle("gen2", fontSize=8, fontName="Helvetica",
                       textColor=C_MUTED, alignment=TA_CENTER)))

    story.extend(cover_items)
    story.append(PageBreak())


def _week_date_range() -> str:
    from datetime import timedelta
    now  = datetime.now()
    mon  = now - timedelta(days=now.weekday())
    fri  = mon + timedelta(days=4)
    return f"{mon.strftime('%d %b')} – {fri.strftime('%d %b %Y')}"


# ── Main PDF builder ──────────────────────────────────────────────────────────

def build_weekly_pdf(
    items: List[Any],
    runs: List[Dict],
    week_label: str,
    out_path: str,
) -> str:
    styles = build_styles()
    story  = []

    neg, pos, neu = overall_sentiment(items)
    brand_data    = aggregate_by_brand(items)

    # ── Cover ──
    _cover_page(story, week_label, styles, neg, pos, len(items))

    # ── 1. Executive summary ──
    story.append(Paragraph("1. Executive Summary", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))

    total_runs    = len(runs)
    total_fetched = sum(r.get("total_fetched", 0) for r in runs)
    story.append(Paragraph(
        f"This report covers <b>{total_runs} daily run(s)</b> during {week_label}, "
        f"scanning <b>{total_fetched:,} articles</b> across all monitored sources. "
        f"A total of <b>{len(items)} signals</b> were captured: "
        f"<b>{neg} risk alerts</b>, <b>{pos} positive developments</b>, and {neu} neutral mentions.",
        styles["body"]))
    story.append(Spacer(1, 0.4*cm))

    top5 = top_stories_week(items)
    if top5:
        story.append(Paragraph("Highest-risk story of the week:", styles["body_bold"]))
        top = top5[0]
        story.append(Paragraph(
            f"<b>{top.item.title[:100]}</b> — {top.brand_hit or 'Industry'} "
            f"[{top.severity}/10 {_sev_label(top.severity)}]",
            styles["body"]))
        story.append(Paragraph(top.why_it_matters or "", styles["small"]))
    story.append(Spacer(1, 0.5*cm))

    # ── 2. Sentiment trend ──
    story.append(Paragraph("2. Sentiment Trend", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))
    story.append(_sentiment_bar_table(neg, pos, neu, styles))
    story.append(Spacer(1, 0.4*cm))

    # Sentiment by brand mini-table
    story.append(Paragraph("Sentiment breakdown by brand:", styles["subsection"]))
    sent_rows = [[
        Paragraph("Brand",    styles["body_bold"]),
        Paragraph("Negative", styles["body_bold"]),
        Paragraph("Positive", styles["body_bold"]),
        Paragraph("Neutral",  styles["body_bold"]),
        Paragraph("Total",    styles["body_bold"]),
    ]]
    for name, b in sorted(brand_data.items(), key=lambda x: x[1]["negative"], reverse=True):
        if name == "Industry":
            continue
        sent_rows.append([
            Paragraph(name, styles["body"]),
            Paragraph(str(b["negative"]), ParagraphStyle("sn", fontSize=9,
                fontName="Helvetica-Bold",
                textColor=C_RED if b["negative"] > 0 else C_MUTED, alignment=TA_CENTER)),
            Paragraph(str(b["positive"]), ParagraphStyle("sp", fontSize=9,
                fontName="Helvetica-Bold",
                textColor=C_GREEN if b["positive"] > 0 else C_MUTED, alignment=TA_CENTER)),
            Paragraph(str(b["neutral"]), styles["small"]),
            Paragraph(str(b["total"]), styles["body_bold"]),
        ])
    if len(sent_rows) > 1:
        st = Table(sent_rows, colWidths=[5*cm, 2.5*cm, 2.5*cm, 2.5*cm, 4*cm])
        st.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
        ]))
        story.append(st)
    story.append(Spacer(1, 0.5*cm))

    # ── 3. Top stories ──
    story.append(PageBreak())
    story.append(Paragraph("3. Top Risk Stories of the Week", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))

    top_neg = top_stories_week(items, n=8)
    if top_neg:
        story.append(_story_table(top_neg, styles, max_rows=8))
    else:
        story.append(Paragraph("No risk stories detected this week.", styles["body"]))
    story.append(Spacer(1, 0.5*cm))

    # ── 4. Risk alerts count by brand ──
    story.append(Paragraph("4. Risk Alerts by Brand", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))

    alert_rows = [[
        Paragraph("Brand",       styles["body_bold"]),
        Paragraph("Risk Alerts", styles["body_bold"]),
        Paragraph("Max Severity",styles["body_bold"]),
        Paragraph("Top Risk Type",styles["body_bold"]),
    ]]
    for name, b in sorted(brand_data.items(), key=lambda x: x[1]["negative"], reverse=True):
        if name == "Industry" or b["negative"] == 0:
            continue
        top_risk = max(b["risk_types"], key=b["risk_types"].get) if b["risk_types"] else "—"
        sev_col  = C_RED if b["max_severity"] >= 7 else C_ORANGE if b["max_severity"] >= 5 else C_MUTED
        alert_rows.append([
            Paragraph(name, styles["body_bold"]),
            Paragraph(str(b["negative"]), ParagraphStyle("ar", fontSize=11,
                fontName="Helvetica-Bold", textColor=C_RED, alignment=TA_CENTER)),
            Paragraph(f"{b['max_severity']}/10", ParagraphStyle("ms", fontSize=9,
                fontName="Helvetica-Bold", textColor=sev_col, alignment=TA_CENTER)),
            Paragraph(top_risk, styles["small"]),
        ])

    if len(alert_rows) > 1:
        at = Table(alert_rows, colWidths=[4.5*cm, 3*cm, 3*cm, 6*cm])
        at.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("ALIGN",         (1, 0), (2, -1), "CENTER"),
        ]))
        story.append(at)
    else:
        story.append(Paragraph("No brand risk alerts this week.", styles["body"]))
    story.append(Spacer(1, 0.5*cm))

    # ── 5. YouTube highlights ──
    story.append(PageBreak())
    story.append(Paragraph("5. YouTube Highlights", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))

    yt_items = youtube_highlights(items, n=8)
    if yt_items:
        yt_rows = [[
            Paragraph("Video Title",  styles["body_bold"]),
            Paragraph("Brand",        styles["body_bold"]),
            Paragraph("Severity",     styles["body_bold"]),
            Paragraph("Signal",       styles["body_bold"]),
        ]]
        for si in yt_items:
            title_t = (si.item.title or "")[:70] + ("…" if len(si.item.title or "") > 70 else "")
            sev_col = C_RED if si.severity >= 7 else C_ORANGE if si.severity >= 5 else C_GREEN
            yt_rows.append([
                Paragraph(title_t, styles["small"]),
                Paragraph(si.brand_hit or "Industry", styles["small"]),
                Paragraph(f"{si.severity}/10", ParagraphStyle("ys", fontSize=8,
                    fontName="Helvetica-Bold", textColor=sev_col, alignment=TA_CENTER)),
                Paragraph(si.event_type.upper(), ParagraphStyle("ye", fontSize=8,
                    fontName="Helvetica-Bold",
                    textColor=C_RED if si.event_type == "negative" else C_GREEN)),
            ])
        yt = Table(yt_rows, colWidths=[8*cm, 2.5*cm, 2.5*cm, 3.5*cm])
        yt.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("ALIGN",         (2, 0), (3, -1), "CENTER"),
        ]))
        story.append(yt)
    else:
        story.append(Paragraph("No YouTube data captured this week.", styles["body"]))
    story.append(Spacer(1, 0.5*cm))

    # ── 6. Competitor dashboard ──
    story.append(PageBreak())
    story.append(Paragraph("6. Competitor Dashboard", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))

    story.append(Paragraph("Rankings — Risk Score", styles["subsection"]))
    story.append(Paragraph(
        "Risk score = (negative alerts × 3) + (max severity × 2) + total mentions. "
        "Higher = more exposure this week.",
        styles["small"]))
    story.append(Spacer(1, 0.2*cm))
    story.append(_competitor_table(brand_data, styles))
    story.append(Spacer(1, 0.6*cm))

    story.append(Paragraph("Side-by-Side Sentiment Comparison", styles["subsection"]))
    story.append(Spacer(1, 0.2*cm))
    story.append(_brand_comparison_table(brand_data, styles))
    story.append(Spacer(1, 0.5*cm))

    # ── 7. Feed health ──
    story.append(PageBreak())
    story.append(Paragraph("7. Feed Health Summary", styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 0.3*cm))
    story.append(_feed_health_table(runs, styles))
    story.append(Spacer(1, 0.5*cm))

    # Per-run log
    if runs:
        story.append(Paragraph("Daily run log:", styles["subsection"]))
        run_rows = [[
            Paragraph("Timestamp",       styles["body_bold"]),
            Paragraph("Scanned",         styles["body_bold"]),
            Paragraph("Scored",          styles["body_bold"]),
            Paragraph("Feeds OK",        styles["body_bold"]),
        ]]
        for r in runs:
            ts = r.get("timestamp", "")[:16].replace("T", " ")
            run_rows.append([
                Paragraph(ts,                         styles["small"]),
                Paragraph(str(r.get("total_fetched", 0)), styles["small"]),
                Paragraph(str(r.get("scored", 0)),    styles["small"]),
                Paragraph(f"{r.get('feeds_ok',0)}/{r.get('feeds_total',0)}", styles["small"]),
            ])
        rt = Table(run_rows, colWidths=[5*cm, 3*cm, 3*cm, 5.5*cm])
        rt.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#334155")),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ]))
        story.append(rt)

    # ── Build ──
    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(C_MUTED)
        canvas.drawString(2*cm, 1.2*cm, "SkincareIntel — Confidential Weekly Report")
        canvas.drawRightString(A4[0] - 2*cm, 1.2*cm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm,  bottomMargin=2*cm,
        title=f"SkincareIntel Weekly Report — {week_label}",
        author="SkincareIntel",
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path


# ── Competitor HTML dashboard ────────────────────────────────────────────────

def build_competitor_dashboard_html(
    items: List[Any],
    week_label: str,
    out_path: str,
) -> str:
    import html as _html
    brand_data = aggregate_by_brand(items)
    neg_total, pos_total, _ = overall_sentiment(items)
    now = datetime.now().strftime("%d %b %Y, %H:%M")

    def esc(s): return _html.escape(str(s) if s else "")

    def risk_score(b):
        return b["negative"] * 3 + b["max_severity"] * 2 + b["total"]

    sorted_brands = sorted(
        [(n, d) for n, d in brand_data.items() if n != "Industry"],
        key=lambda x: risk_score(x[1]), reverse=True
    )

    rows_html = ""
    for rank, (name, b) in enumerate(sorted_brands, 1):
        rs = risk_score(b)
        total = max(1, b["total"])
        neg_pct = b["negative"] / total * 100
        pos_pct = b["positive"] / total * 100
        neu_pct = max(0, 100 - neg_pct - pos_pct)
        rs_cls = "risk-high" if rs >= 20 else "risk-med" if rs >= 10 else "risk-low"
        rows_html += f"""
        <tr>
          <td class="rank">{rank}</td>
          <td class="brand-name">{esc(name)}</td>
          <td class="num">{b['total']}</td>
          <td class="num neg">{b['negative']}</td>
          <td class="num pos">{b['positive']}</td>
          <td class="num">{b['max_severity']}/10</td>
          <td>
            <div class="sent-bar">
              <div class="bar-neg" style="width:{neg_pct:.0f}%" title="Negative {neg_pct:.0f}%"></div>
              <div class="bar-pos" style="width:{pos_pct:.0f}%" title="Positive {pos_pct:.0f}%"></div>
              <div class="bar-neu" style="width:{neu_pct:.0f}%" title="Neutral {neu_pct:.0f}%"></div>
            </div>
          </td>
          <td><span class="risk-badge {rs_cls}">{rs}</span></td>
        </tr>"""

    html_out = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SkincareIntel — Competitor Dashboard {esc(week_label)}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  :root{{
    --navy:#0f172a;--accent:#1d4ed8;--red:#dc2626;--green:#059669;
    --orange:#d97706;--muted:#64748b;--border:#e2e8f0;--bg:#f8fafc;--white:#fff;
  }}
  body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--navy);font-size:14px;padding-bottom:60px}}
  .header{{background:var(--white);border-bottom:1px solid var(--border);padding:0 40px}}
  .header-inner{{max-width:1100px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;height:60px}}
  .logo{{font-weight:700;font-size:16px}}.logo span{{color:var(--accent)}}
  .meta{{font-size:12px;color:var(--muted);text-align:right}}
  .wrap{{max-width:1100px;margin:0 auto;padding:32px 40px 0}}
  .summary-bar{{background:var(--white);border:1px solid var(--border);border-radius:12px;padding:24px 28px;margin-bottom:28px;display:flex;gap:32px;flex-wrap:wrap;align-items:center}}
  .kpi{{text-align:center;min-width:70px}}
  .kpi-val{{font-size:28px;font-weight:700;line-height:1;margin-bottom:4px}}
  .kpi-label{{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
  .divider{{width:1px;height:40px;background:var(--border)}}
  h2{{font-size:15px;font-weight:600;color:var(--navy);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
  .card{{background:var(--white);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:24px}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:var(--navy);color:#fff;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:10px 12px;text-align:left}}
  td{{padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}}
  tr:nth-child(even) td{{background:#f8fafc}}
  .rank{{font-weight:700;color:var(--muted);text-align:center;width:36px}}
  .brand-name{{font-weight:600}}
  .num{{text-align:center}}
  .neg{{color:var(--red);font-weight:700}}
  .pos{{color:var(--green);font-weight:700}}
  .sent-bar{{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--border);min-width:100px}}
  .bar-neg{{background:var(--red);height:100%}}
  .bar-pos{{background:var(--green);height:100%}}
  .bar-neu{{background:#cbd5e1;height:100%}}
  .risk-badge{{display:inline-block;font-size:11px;font-weight:700;border-radius:4px;padding:3px 10px}}
  .risk-high{{background:#fef2f2;color:var(--red);border:1px solid #fecaca}}
  .risk-med{{background:#fffbeb;color:var(--orange);border:1px solid #fde68a}}
  .risk-low{{background:#f0fdf4;color:var(--green);border:1px solid #a7f3d0}}
  .footer{{max-width:1100px;margin:32px auto 0;padding:16px 40px;border-top:1px solid var(--border);font-size:11px;color:var(--muted);display:flex;justify-content:space-between}}
  @media(max-width:700px){{.wrap,.footer{{padding:16px}}.header{{padding:0 16px}}.summary-bar{{gap:16px}}}}
</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <div class="logo">Skincare<span>Intel</span></div>
    <div class="meta">Competitor Dashboard<br>{esc(week_label)} · {esc(now)}</div>
  </div>
</div>
<div class="wrap">
  <div class="summary-bar">
    <div class="kpi"><div class="kpi-val" style="color:var(--accent)">{len(items)}</div><div class="kpi-label">Total Signals</div></div>
    <div class="divider"></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--red)">{neg_total}</div><div class="kpi-label">Risk Alerts</div></div>
    <div class="divider"></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--green)">{pos_total}</div><div class="kpi-label">Positive</div></div>
    <div class="divider"></div>
    <div class="kpi"><div class="kpi-val" style="color:var(--navy)">{len(sorted_brands)}</div><div class="kpi-label">Brands Tracked</div></div>
  </div>
  <div class="card">
    <h2>Brand Risk Rankings</h2>
    <table>
      <thead><tr>
        <th>#</th><th>Brand</th><th>Mentions</th><th>Alerts</th>
        <th>Positive</th><th>Max Sev</th><th>Sentiment</th><th>Risk Score</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    <p style="font-size:11px;color:var(--muted);margin-top:12px">
      Risk Score = (alerts × 3) + (max severity × 2) + total mentions
    </p>
  </div>
</div>
<div class="footer">
  <span>SkincareIntel · Weekly Competitor Dashboard · {esc(week_label)}</span>
  <span>{esc(now)}</span>
</div>
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return out_path


# ── Email the PDF ─────────────────────────────────────────────────────────────

def email_weekly_report(
    pdf_path: str,
    dashboard_path: str,
    week_label: str,
    email_cfg_path: str,
) -> None:
    if not os.path.exists(email_cfg_path):
        logging.warning("Weekly report: email_config.json not found — skipping email.")
        return
    try:
        with open(email_cfg_path, "r", encoding="utf-8") as f:
            ec = json.load(f)
    except Exception as e:
        logging.warning(f"Weekly report: could not load email config — {e}")
        return

    ec["from_email"] = os.environ.get("EMAIL_FROM", ec.get("from_email", "")).strip()
    to_env = os.environ.get("EMAIL_TO", "").strip()
    if to_env:
        ec["to_emails"] = [x.strip() for x in to_env.split(",") if x.strip()]
    ec["smtp_user"] = os.environ.get("SMTP_USER", ec.get("smtp_user", "")).strip()
    ec["smtp_pass"] = os.environ.get("SMTP_PASS", ec.get("smtp_pass", "")).strip()

    required = ["from_email", "to_emails", "smtp_host", "smtp_port", "smtp_user", "smtp_pass"]
    missing = [key for key in required if not ec.get(key)]
    if missing:
        logging.warning(f"Weekly report: missing email fields: {', '.join(missing)} — skipping email")
        return

    subject = f"[SkincareIntel] Weekly Report — {week_label}"
    body    = (
        f"Please find attached the SkincareIntel Weekly Intelligence Report for {week_label}.\n\n"
        f"The PDF contains:\n"
        f"  • Sentiment trend (positive vs negative)\n"
        f"  • Top risk stories of the week\n"
        f"  • Risk alerts count by brand\n"
        f"  • YouTube highlights\n"
        f"  • Competitor dashboard (rankings + side-by-side comparison)\n"
        f"  • Feed health summary\n\n"
        f"The competitor dashboard HTML is also attached for interactive viewing.\n\n"
        f"— SkincareIntel"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = ec["from_email"]
    msg["To"]      = ", ".join(ec["to_emails"])
    msg.set_content(body)

    for path, name in [(pdf_path, "weekly_report.pdf"), (dashboard_path, "competitor_dashboard.html")]:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            maintype, subtype = ("application", "pdf") if path.endswith(".pdf") else ("text", "html")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(ec["smtp_host"], int(ec["smtp_port"]), timeout=30) as server:
            server.ehlo(); server.starttls(context=ctx); server.ehlo()
            server.login(ec["smtp_user"], ec["smtp_pass"])
            server.send_message(msg)
        logging.info(f"Weekly report: emailed successfully to {ec['to_emails']}")
    except smtplib.SMTPAuthenticationError:
        logging.error(
            "Weekly report: email authentication rejected. For Gmail, create a fresh "
            "App Password after enabling 2-Step Verification."
        )
    except Exception as e:
        logging.error(f"Weekly report: email failed — {e}")


# ── Entry point called from agent.py ─────────────────────────────────────────

def maybe_generate_weekly_report(
    cfg: Dict[str, Any],
    items: List[Any],
    runs: List[Dict],
    week_label: str,
) -> None:
    """
    Called from agent.py every run. Only generates the report on Fridays.
    Pass force=True in cfg to generate any day (for testing).
    """
    today = datetime.now().weekday()  # 0=Mon … 4=Fri
    force = cfg.get("force_weekly_report", False)

    if today != 4 and not force:
        logging.info("Weekly report: not Friday — skipping.")
        return

    if not items:
        logging.info("Weekly report: no data accumulated this week — skipping.")
        return

    ts           = datetime.now().strftime("%Y%m%d")
    pdf_path     = os.path.join(REPORTS_DIR, f"weekly_report_{ts}.pdf")
    dash_path    = os.path.join(REPORTS_DIR, f"competitor_dashboard_{ts}.html")
    email_cfg    = os.path.join(BASE_DIR, cfg.get("email", {}).get("config_path", "email_config.json"))

    logging.info(f"Weekly report: generating PDF for {week_label}...")
    build_weekly_pdf(items, runs, week_label, pdf_path)
    logging.info(f"Weekly report: PDF saved → {pdf_path}")

    logging.info("Weekly report: generating competitor dashboard...")
    build_competitor_dashboard_html(items, week_label, dash_path)
    logging.info(f"Weekly report: dashboard saved → {dash_path}")

    if cfg.get("email", {}).get("enabled", False):
        email_weekly_report(pdf_path, dash_path, week_label, email_cfg)