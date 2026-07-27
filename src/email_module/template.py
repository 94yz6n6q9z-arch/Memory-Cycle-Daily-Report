"""
HTML email template assembly for the daily brief.

Builds the email with:
0. URGENT BANNER (only if executive_summary.urgent_attention_needed == True)
1. EXECUTIVE SUMMARY block (the prominent top section in the format Raul requested)
2. Big movers callout (if any)
3. Performance chart + table
4. Tier indicators table (4 tiers, each with their indicators)
5. Top 5 indicators deep-dive
6. Latest news
7. DATA SOURCE VALIDATION (per-source fetch status — confirms each source
   was actually reached and parsed during this run)

CHANGELOG (this file):
  v3.39.7.1 (2026-05-20) — _build_cycle_matrix_card hardcoded color dict ({"Hold": "#C0DD97", "Warn": "#FAC775", "Fire": "#D85A30"}) replaced with lookup into STATUS_COLORS — same color values (green/amber/red) but keyed on the new MatrixCell.value strings "Not fired" / "Partially fired" / "Fired" which now match STATUS_COLORS keys directly.
  v3.39.7 (2026-05-20) — Cycle matrix card injection. build_email_html() signature gains three optional kwargs (cycle_verdict, cycle_matrix_chart_b64, spot_contract_chart_b64). _build_indicators_section() now accepts and forwards them; when rendering t1_spot_decline AND both chart base64 strings are non-None, it appends a full-width "Cycle matrix snapshot" card row directly under the indicator row (colspan=5, 40/60 split: matrix dot left, dual-axis chart right). New helper _build_cycle_matrix_card() composes the card HTML. All other rendering unchanged; if kwargs are absent the section renders exactly as in v3.38. No new imports; the verdict is read via getattr() to avoid importing cycle_matrix types here.
  v3.38  (2026-05-17) — Added three new `_SOURCE_TYPE_LABELS` entries used by the deep tier diagnostics rows: "api" → "External API" (Finnhub, DART), "transcript" → "Earnings transcript source" (Yahoo / Motley Fool / IR scrapers), "broker_pdf" → "Korean broker note feed" (Mirae, NH, Samsung Securities). Without these, the new deep-tier rows would group under raw lowercase type-strings in the validation table.
"""

from __future__ import annotations

import html
import math  # v3.39.7.9 — NaN guard in _build_relative_strength_snippet
import re
from datetime import datetime
from typing import Optional

from src.analysis.llm_summarizer import (
    BriefAnalysis,
    BigMover,
    ExecutiveSummary,
    IndicatorAssessment,
)
from src.data.prices import PerformanceMetrics
from src.diagnostics import SourceFetchStatus


TIER_COLORS = {
    1: "#FAEEDA",
    2: "#FCEBEB",
    3: "#E1F5EE",
    4: "#F1EFE8",
}

TIER_LABELS = {
    1: "Tier 1 — Early warning (3–6 months pre-stock-peak)",
    2: "Tier 2 — Mid-cycle (2–4 months pre-stock-peak)",
    3: "Tier 3 — Concurrent confirms (at stock peak)",
    4: "Tier 4 — Lagging confirms (post-peak)",
}

STATUS_COLORS = {
    # v3.39.7.9 — three-state collapse per user direction 2026-05-21.
    # "Reversed" removed (audit trail moved to evidence_for_change_vs_yesterday
    # and the trigger event banner). Fired color darkened from #D85A30
    # (red-orange that read as "amber") to #B91C1C (clearer red).
    "Fired": "#B91C1C",
    "Partially fired": "#FAC775",
    "Not fired": "#C0DD97",
    "Unknown (LLM unavailable)": "#CCCCCC",
}


def build_email_html(
    analysis: BriefAnalysis,
    performance: list[PerformanceMetrics],
    chart_base64: str,
    data_source_statuses: Optional[list[SourceFetchStatus]] = None,
    upcoming_events: Optional[list[dict]] = None,
    rel_strength_verdict: Optional[dict] = None,
    eps_scoring_verdict: Optional[dict] = None,
    brief_datetime: Optional[datetime] = None,
    # v3.39.2: new supercycle chart. When provided, renders as its own
    # section between "Performance overview" and "Tier indicators". When
    # None (chart generation failed or SK Hynix data missing), the section
    # is silently skipped so the email still ships.
    supercycle_chart_base64: Optional[str] = None,
    # v3.39.7: cycle matrix snapshot. When all three are non-None, a
    # "Cycle matrix snapshot" card is injected directly below the
    # t1_spot_decline row in the Tier 1 indicators table, showing the
    # matrix dot (40%) + dual-axis spot/contract chart (60%) side-by-side.
    # When any is None, the section renders exactly as in v3.38.
    cycle_verdict: Optional[object] = None,
    cycle_matrix_chart_b64: Optional[str] = None,
    spot_contract_chart_b64: Optional[str] = None,
    # v3.4.0: DHCR direction-monitor data. snapshot is a dict (from
    # asdict(DHCRSnapshot)) carrying all computed fields. chart_b64 is the
    # trajectory chart rendered by chart_generator.
    # v3.7.3 — when snapshot is None (DHCR YAML/state missing or computation
    # failed), section 4 now ALWAYS emits card 6 — either the real DHCR
    # card or a "Computation failed" placeholder with the actual error
    # text from dhcr_error_message. Section 4 never silently skips
    # numbering; cards always render in slots 6/7/8.
    dhcr_snapshot: Optional[dict] = None,
    dhcr_chart_b64: Optional[str] = None,
    # v3.4.2 — DHCR matrix chart (5x5 with memory-zone color coding).
    # Rendered inside the section 4 DHCR card alongside the trajectory chart.
    dhcr_matrix_chart_b64: Optional[str] = None,
    # v3.4.4 — Indicator 7 (Competitor dynamics) composite snapshot. Dict
    # form of CompetitorSnapshot from src/analysis/competitor_scoring.py.
    # Renders as section 4 deep-dive card with 4 sub-cards + dual-threat
    # callout. v3.7.3 — None now renders the placeholder, not silent skip.
    competitor_snapshot: Optional[dict] = None,
    # v3.4.4 — Indicator 8 (US ADR / SPO event) composite snapshot. Dict
    # form of ADREventSnapshot from src/analysis/adr_event_tracking.py.
    # Renders as section 4 deep-dive card with 4 sub-cards + calendar +
    # academic-calibration prose. v3.7.3 — None renders placeholder.
    adr_snapshot: Optional[dict] = None,
    # v3.7.3 — Error messages from fast_tier's compute try/except blocks.
    # When the corresponding snapshot is None, these strings are rendered
    # in the placeholder card's body so the operator sees the diagnosis on
    # the brief itself ("NameError: name 'build_narrative_summary' is not
    # defined", "FileNotFoundError: state/inference_arch_state.json", etc.)
    # rather than only in the buried workflow log. When the snapshot
    # rendered successfully these are None and the field is unused.
    dhcr_error_message: Optional[str] = None,
    competitor_error_message: Optional[str] = None,
    adr_error_message: Optional[str] = None,
    # v3.4.1 Fix 5b: Layer 2 per-signal reasonings indexed by signal_id.
    # Used as defensive fallback in _build_indicators_section when Sonnet's
    # where_we_stand field comes back empty (hit max_tokens mid-output).
    # Each entry: {"consensus_direction": "...", "confidence_level": "...",
    # "evidence_sources": [...]}. When None or missing for an indicator,
    # the original Sonnet text is used as-is (existing behavior).
    signal_reasonings_by_id: Optional[dict] = None,
) -> str:
    """Assemble the full HTML email."""

    when = brief_datetime or datetime.utcnow()
    when_str = when.strftime("%A, %B %d, %Y · %H:%M UTC")

    css = """
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #2c2c2a; line-height: 1.5; max-width: 920px; margin: 0 auto; padding: 20px; background: #fafaf7; }
      h1 { color: #534AB7; font-size: 26px; margin: 0 0 4px 0; }
      h2 { color: #2c2c2a; font-size: 18px; margin: 28px 0 10px 0; border-bottom: 1px solid #e0ddd4; padding-bottom: 4px; }
      h3 { color: #534AB7; font-size: 15px; margin: 22px 0 8px 0; }
      .subtitle { color: #888780; font-size: 13px; margin-bottom: 24px; }
      .urgent-banner { background: #FCEBEB; border: 2px solid #A32D2D; padding: 16px 20px; margin: 16px 0; border-radius: 6px; }
      .urgent-banner .label { display: inline-block; background: #A32D2D; color: #fff; padding: 3px 10px; border-radius: 3px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; margin-bottom: 8px; }
      .urgent-banner .text { color: #791F1F; font-weight: 500; font-size: 14px; }
      .exec-summary { background: #fff; border: 1px solid #e0ddd4; padding: 20px 24px; margin: 16px 0; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
      .exec-summary h2 { margin-top: 0; border: none; padding: 0; color: #534AB7; }
      .exec-line { font-size: 14px; line-height: 1.7; margin: 8px 0; }
      .exec-line strong { color: #2c2c2a; }
      .phase-tag { display: inline-block; background: #FAEEDA; color: #854F0B; padding: 3px 10px; border-radius: 3px; font-weight: 600; font-size: 13px; margin-right: 4px; }
      .case-block { display: inline-block; padding: 6px 10px; margin: 4px 6px 4px 0; border-radius: 3px; font-size: 13px; }
      .case-base { background: #FAEEDA; color: #854F0B; }
      .case-bull { background: #EAF3DE; color: #4A6B17; }
      .case-bear { background: #FCEBEB; color: #791F1F; }
      .delta-text { font-style: italic; color: #5F5E5A; font-size: 13px; margin-top: 8px; }
      /* v3.39.7.5 — consolidated executive brief paragraph. The chunky
         5-8 sentence narrative that anchors the brief. Anchor links
         (rendered from the literal token [→§N] in Sonnet output) use
         a subtle styling so they're visible but don't disrupt the
         reading flow. */
      .exec-brief { background: #f5f3ed; border-left: 3px solid #534AB7; padding: 14px 18px; margin: 14px 0 0 0; font-size: 14px; line-height: 1.55; color: #2d2d2d; }
      .exec-brief .anchor-link, .anchor-link { color: #534AB7; text-decoration: none; font-weight: 600; padding: 0 2px; border-bottom: 1px dotted #534AB7; }
      .exec-brief .anchor-link:hover, .anchor-link:hover { text-decoration: underline; }
      .new-trigger { background: #FFEBA1; border-left: 3px solid #C68A2A; padding: 8px 12px; margin: 8px 0; font-size: 13px; }
      .big-movers { background: #fff; border: 1px solid #e0ddd4; padding: 12px 16px; margin: 12px 0; border-radius: 4px; font-size: 13px; }
      .market-summary { background: #f5f3ed; padding: 10px 14px; margin: 12px 0; font-size: 13px; border-radius: 4px; border-left: 3px solid #888780; }
      table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 12px; background: #fff; table-layout: fixed; }
      th { background: #534AB7; color: #fff; padding: 8px 10px; text-align: left; font-weight: 600; }
      td { padding: 8px 10px; border-bottom: 1px solid #e0ddd4; vertical-align: top; word-break: break-word; overflow-wrap: anywhere; }
      /* v3.39: long Google News RSS URLs were stretching the "Latest
         developments" column and squeezing "Where we stand". break-all on
         <a> ensures the URL itself wraps within its allotted column width
         rather than forcing the column to widen. table-layout:fixed on the
         parent table makes the column widths (set via th style) authoritative
         instead of suggestion-only. */
      td a { word-break: break-all; }
      tr:last-child td { border-bottom: none; }
      .perf-pos { color: #0F6E56; font-weight: 600; }
      .perf-neg { color: #A32D2D; font-weight: 600; }
      .perf-flat { color: #888780; }
      .status-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; color: #2c2c2a; }
      .change-flag { background: #FFEBA1; color: #633806; padding: 2px 6px; border-radius: 3px; font-size: 11px; margin-left: 6px; font-weight: 600; }
      .news-item { padding: 8px 12px; border-left: 2px solid #e0ddd4; margin: 8px 0; background: #fff; }
      .news-item a { color: #185FA5; text-decoration: none; font-weight: 500; }
      .news-item a:hover { text-decoration: underline; }
      .why-matters { color: #5F5E5A; font-size: 12px; margin-top: 4px; font-style: italic; }
      .footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid #e0ddd4; color: #888780; font-size: 11px; }
      img.chart { max-width: 100%; height: auto; display: block; margin: 12px 0; border: 1px solid #e0ddd4; border-radius: 4px; }
      .indicator-cell { font-size: 12px; line-height: 1.45; }
      .indicator-cell strong { color: #2c2c2a; }
      .links-list a { color: #185FA5; text-decoration: none; font-size: 11px; }
      .links-list a:hover { text-decoration: underline; }
      /* Data source assessment section */
      .validation-summary { font-size: 13px; margin: 8px 0 12px 0; padding: 8px 12px; background: #fff; border: 1px solid #e0ddd4; border-radius: 4px; }
      .validation-pill { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 700; margin-right: 6px; letter-spacing: 0.3px; }
      .validation-ok { background: #EAF3DE; color: #4A6B17; }
      .validation-warn { background: #FAEEDA; color: #854F0B; }
      .validation-fail { background: #FCEBEB; color: #791F1F; }
      .validation-skipped { background: #ECEAE3; color: #5F5E5A; }
      /* v3.39.7.6 — quiet = reached, 0 items, but 0 items was the
         expected state for this watcher (e.g. "GDDR7 inference
         accelerator" Google query, or pre-listing SK Hynix EDGAR
         filings). Muted neutral gray distinguishes "nothing happened,
         as expected" from "feed might be broken" (WARN). */
      .validation-quiet { background: #ECEAE3; color: #6B6960; opacity: 0.85; }
      .source-row-ok td { background: #fff; }
      .source-row-warn td { background: #FFFBF1; }
      .source-row-fail td { background: #FCF3F3; }
      .source-row-skipped td { background: #F6F4EE; }
      .source-row-quiet td { background: #F6F4EE; color: #6B6960; }
      .source-status-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 700; letter-spacing: 0.3px; }
      .source-error { color: #791F1F; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
      .source-detail { color: #5F5E5A; font-size: 11px; font-style: italic; }
    </style>
    """

    parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        css,
        "</head><body>",
        f"<h1>Memory Cycle Daily Brief</h1>",
        f"<p class='subtitle'>{html.escape(when_str)}</p>",
    ]

    es = analysis.executive_summary

    # v3.39.7.5 — URGENT banner killed entirely. Was repetitive with body card
    # statuses and tier_status_summary line. Field still on ExecutiveSummary
    # dataclass for back-compat (won't break prior_state load); just not rendered.

    # 0b. 🚨 Relative strength banner — v3.41.10: transition-day only. Renders
    # when the sub-triggers breach AND t3_relative_strength has a transition in
    # the 24h display log (same clock as the NEW TRIGGER EVENT pill). Ongoing
    # red status stays visible in §3 and the t3 card; the banner no longer
    # persists day after day while the signal remains fired.
    _bespoke_rendered: set = set()
    if (
        rel_strength_verdict
        and rel_strength_verdict.get("overall_fired")
        and _indicator_in_24h_trigger_log(es, "t3_relative_strength")
    ):
        parts.append(_build_relative_strength_banner(rel_strength_verdict))
        _bespoke_rendered.add("t3_relative_strength")

    # 0c. 🚨 EPS Revisions Watch banner — v3.41.10: same transition-day gate
    # (this block had the identical persistent-status bug, latent until the
    # signal first fires).
    if eps_scoring_verdict and _indicator_in_24h_trigger_log(es, "t2_eps_revisions"):
        overall = eps_scoring_verdict.get("overall_status", "Not fired")
        if overall in ("FIRED", "EARLY_WARNING"):
            parts.append(_build_eps_revisions_banner(eps_scoring_verdict))
            _bespoke_rendered.add("t2_eps_revisions")

    # 0d. v3.41.15 — generic transition banners: every 24h-log transition not
    # already carried by a bespoke banner THIS run gets a top alarm — red for
    # fired-direction moves, amber for downgrades (a bespoke indicator whose
    # bespoke banner did NOT render, e.g. a t3 downgrade, falls through here
    # rather than vanishing). Capped to avoid takeover on busy days.
    _generic = [
        t for t in (getattr(es, "new_triggers_today", None) or [])
        if isinstance(t, dict) and t.get("indicator_id") not in _bespoke_rendered
    ]
    for _trig in _generic[:4]:
        parts.append(_build_generic_trigger_banner(_trig))
    if len(_generic) > 4:
        parts.append(
            f"<div style='font-size:12px;color:#5F5E5A;margin:4px 0 8px'>"
            f"+{len(_generic) - 4} further trigger event(s) — see the "
            f"executive summary.</div>"
        )

    # 1. Executive summary — phase line, new-trigger callouts, consolidated brief
    # v3.39.7.5: _build_executive_summary now renders es.executive_brief (the
    # consolidated 5-8 sentence paragraph) with anchor link substitution. The
    # old _build_big_movers and market_summary renders are GONE — both themes
    # are now folded into the consolidated paragraph.
    parts.append(_build_executive_summary(es))

    # 3. Performance section
    #
    # v3.39.2: dropped inline 1Y performance chart. The chart was visually
    # redundant with the performance table immediately below (which already
    # shows 1D / 1W / 1M / 3M / 6M / 1Y returns numerically). The lead visual
    # is now the supercycle phase chart (section 2 below). chart_base64 is
    # still computed upstream but no longer embedded here.
    parts.append("<h2 id='section-1'>1. Performance overview</h2>")
    parts.append(_build_performance_table(performance))

    # 4. Where we stand in the cycle (supercycle phase chart) — v3.39.2
    #
    # Skipped silently if supercycle_chart_base64 is None (chart generation
    # failed upstream, or SK Hynix data unavailable). Email still ships with
    # the rest of the content; renumbering downstream is not necessary
    # because section numbers below this one are stable regardless.
    if supercycle_chart_base64:
        parts.append("<h2 id='section-2'>2. Where we stand in the cycle</h2>")
        parts.append(
            "<p style='font-size:12px;color:#5F5E5A;margin-top:0'>"
            "SK Hynix mapped against the universal 5-phase memory-supercycle "
            "framework. Phase boundaries are anchored to the base-case peak "
            "window from the executive summary above and scaled to fit the "
            "actual cycle pace; today's position therefore moves day-to-day "
            "as the LLM updates the projection. The three projection curves "
            "trace each scenario's path from today's price to its respective "
            "peak; the side panels list fired Tier 1 and Tier 2 indicators "
            "and the remaining T2/T3 watch list."
            "</p>"
        )
        parts.append(
            f"<img class='chart' src='data:image/png;base64,{supercycle_chart_base64}' "
            f"alt='Supercycle phase chart — SK Hynix vs 5-phase framework' />"
        )

    # 5. Tier indicators (section 3 — summary cards table)
    parts.append("<h2 id='section-3'>3. Tier indicators — daily status</h2>")
    # v3.39.7.9 — rel_strength_verdict threaded through so t3_relative_strength
    # card surfaces the actual 5d-vs-SOX numbers (color-coded by fire band)
    # at the section 3 summary level, not just in the section 4 deep dive.
    parts.append(_build_indicators_section(
        analysis.indicator_assessments,
        rel_strength_verdict=rel_strength_verdict,
        dhcr_snapshot=dhcr_snapshot,
        signal_reasonings_by_id=signal_reasonings_by_id,
    ))

    # 6. Top 8 deep dives (section 4 — v3.4.4: renamed from Top 6.
    # v3.4.7 (2026-05-22) — Ordering LOCKED per Raul:
    #   Cards 1-5: the 5 Sonnet-picked per-indicator deep dives (current order)
    #   Card 6:    DHCR (framework-level macro direction monitor)
    #   Card 7:    Indicator 7 — Competitor dynamics
    #   Card 8:    Indicator 8 — US ADR / SPO event (last, ties to peak-coincidence)
    # All three of these (DHCR/Competitor/ADR) are filtered out of section 3.)
    parts.append("<h2 id='section-4'>4. Top 8 indicators — deep dive</h2>")

    # Cards 1-5: the 5 Sonnet-picked per-indicator deep dives.
    parts.append(_build_top5_section(
        analysis.top_5_deep_dives,
        cycle_verdict=cycle_verdict,
        cycle_matrix_chart_b64=cycle_matrix_chart_b64,
        spot_contract_chart_b64=spot_contract_chart_b64,
    ))

    # Card 6: DHCR framework-level direction-monitor card (v3.4.2 matrix
    # snapshot — status pill, score 0-100, workload-vs-market cells with
    # anticipation premium, 5x5 matrix chart, per-category context
    # contribution table, volume layer + composite HBM bit demand, sanity
    # check 3 signals, bull/bear case context boxes, framework primer footer).
    # v3.4.4 enhancement: spectrum bar visualization (vertical 0-100 bar
    # with memory-architecture segments + SK Hynix moat-strength labels).
    # v3.7.3 — Section 4 cards 6/7/8 are now ALWAYS emitted, even when their
    # snapshot is None due to a computation failure upstream. The placeholder
    # cards (rendered by _build_section4_placeholder_card) preserve the card
    # slot in section 4 — operator sees on the brief itself that the indicator
    # is broken (with the actual error message from fast_tier's except handler)
    # rather than a silent jump in numbering. Production bug 2026-05-25
    # showed DHCR's silent absence after a NameError crashed compute; the
    # brief jumped 5 → 7 with no indication on the email.
    #
    # v3.4.7 placement: card 6 (first after top-5). DHCR is structurally
    # Tier 1 but renders in section 4 ONLY — never in the section 3 tier
    # indicator row table.
    parts.append(_build_section4_card_or_placeholder(
        snapshot=dhcr_snapshot,
        indicator_number=6,
        indicator_title="Decode HBM Captive Ratio (DHCR)",
        error_message=dhcr_error_message,
        normal_card_builder=lambda snap: _build_dhcr_section4_card(
            snap,
            matrix_chart_b64=dhcr_matrix_chart_b64,
            trajectory_chart_b64=dhcr_chart_b64,
        ),
    ))

    # Card 7: Indicator 7 — Competitor dynamics deep-dive card.
    # 4 sub-cards (7A CXMT, 7B Samsung HBM4/4E, 7C Micron, 7D HBF) +
    # composite status + dual-threat scenario callout. Status calibrated
    # to estimated SK Hynix EPS impact (price × volume), not share %.
    parts.append(_build_section4_card_or_placeholder(
        snapshot=competitor_snapshot,
        indicator_number=7,
        indicator_title="Competitor dynamics",
        error_message=competitor_error_message,
        normal_card_builder=_build_competitor_section4_card,
    ))

    # Card 8 (last): Indicator 8 — US ADR / SPO event deep-dive card.
    # 4 sub-cards (8A F-1 watch, 8B roadshow, 8C pricing day, 8D insider
    # sales >$10M) + lifecycle phase pill + calendar milestones + academic
    # calibration prose. Pre-event state renders as "Awaiting F-1" framing.
    # Peak-coincidence signal per Part 11 — last position emphasises that
    # the ADR event is the closer of the section, the macro-peak warning.
    parts.append(_build_section4_card_or_placeholder(
        snapshot=adr_snapshot,
        indicator_number=8,
        indicator_title="US ADR / SPO event",
        error_message=adr_error_message,
        normal_card_builder=_build_adr_section4_card,
    ))

    # 7. Latest news
    parts.append("<h2 id='section-5'>5. Latest news from the memory & semi market</h2>")
    parts.append(_build_news_section(analysis.key_news_summary))

    # 7b. EPS Revisions Watch — detailed section when signal fired/approaching
    if eps_scoring_verdict and eps_scoring_verdict.get("overall_status") in ("FIRED", "EARLY_WARNING", "BULLISH_CONFIRM"):
        parts.append("<h2>5b. EPS Revisions Watch — t2_eps_revisions weighted scoring</h2>")
        parts.append(_build_eps_revisions_section(eps_scoring_verdict))

    # 8. Calendar of events (next 90 days)
    if upcoming_events:
        parts.append("<h2 id='section-6'>6. Calendar of events — next 90 days</h2>")
        parts.append(
            "<p style='font-size:12px;color:#5F5E5A;margin-top:0'>"
            "Forward-looking calendar across the core 7 indicators and wider industry. "
            "Earnings dates, ADR milestones, HBM4 qualification windows, TrendForce monthly "
            "report drops, conferences, and macro events. Each entry shows what to watch for "
            "and which signal it informs."
            "</p>"
        )
        parts.append(_build_calendar_section(upcoming_events))

    # 8. Data source assessment (per-source fetch status)
    if data_source_statuses:
        parts.append("<h2 id='section-7'>7. Data source assessment</h2>")
        parts.append(
            "<p style='font-size:12px;color:#5F5E5A;margin-top:0'>"
            "Per-source diagnostics for this run. Written confirmation that each configured "
            "data source was reached, read, and parsed — guards against silent failures where "
            "the brief looks healthy but the underlying feed has been broken for weeks. "
            "<strong>OK</strong> = reached and returned items. "
            "<strong>WARN</strong> = reached but returned 0 items where items WERE expected "
            "(could be a parsing issue or a quiet news day — investigate if it persists). "
            "<strong>QUIET</strong> = reached, 0 items, but 0 items is the routine expected "
            "state for this watcher (e.g. forward-looking architecture event detectors, "
            "pre-listing SK Hynix SEC filings). Informational only. "
            "<strong>FAIL</strong> = network/auth/parse error — investigate. "
            "<strong>SKIPPED</strong> = source intentionally not queried (e.g. missing API key)."
            "</p>"
        )
        parts.append(_build_data_source_validation(data_source_statuses))

    parts.append(
        "<div class='footer'>"
        "Generated by the Memory Cycle Brief pipeline (GitHub Actions + Anthropic Haiku/Sonnet). "
        "Not investment advice. Data sources: Yahoo Finance, TrendForce, Korean financial press, "
        "SEC EDGAR, Google News. "
        "Indicator framework per the Memory Supercycle Analysis (May 2026)."
        "</div>"
    )

    parts.append("</body></html>")
    return "\n".join(parts)


def _build_executive_summary(es: ExecutiveSummary) -> str:
    """Render the executive summary in the format Raul specified."""

    # Phase line
    phase_text = (
        f"<div class='exec-line'>"
        f"<strong>We are currently in Phase <span class='phase-tag'>{es.current_phase}</span> "
        f"({html.escape(es.current_phase_label)}).</strong> "
        f"{html.escape(es.tier_status_summary)}."
        f"</div>"
    )

    # v3.4.1 Fix 1 — trigger banner background varies by DESTINATION status:
    #   reverted to "Not fired"      → green (#C0DD97), dark text
    #   transitioned to "Fired"      → red (#B91C1C), white text
    #   transitioned to "Partial..." → yellow (#FAC775), dark text
    # The destination is extracted from the `to_status` field if present,
    # otherwise parsed from the description string (e.g. "from 'Fired' to
    # 'Not fired'"). Falls back to the legacy yellow if destination can't
    # be determined.
    # v3.4.1 Fix 3b — "Reversed" is forbidden vocabulary; convert at render
    # time if any code path or stale state slips it through.
    if es.new_triggers_today:
        trigger_lines = []
        for trig in es.new_triggers_today:
            description = trig.get('description', '')
            to_status = trig.get('to_status') or ''
            # Parse destination from description if to_status not provided.
            # Look for "to 'X'" pattern at end of typical phrasing.
            if not to_status:
                import re as _re
                m = _re.search(r"to ['\"]([^'\"]+)['\"]", description)
                if m:
                    to_status = m.group(1)
            # Normalize: "Reversed" → "Not fired" (v3.4.1 Fix 3b)
            if to_status == "Reversed":
                to_status = "Not fired"
                description = description.replace("'Reversed'", "'Not fired'")
            # Also sanitize description in case it contains "Reversed" elsewhere
            description = description.replace("'Reversed'", "'Not fired'")
            description = description.replace("Reversed", "Not fired")
            # Pick colors by destination
            if to_status == "Fired":
                bg_color = "#B91C1C"
                border_color = "#7A1212"
                text_color = "#FFFFFF"
                label_bg = "#7A1212"
            elif to_status == "Not fired":
                bg_color = "#C0DD97"
                border_color = "#6B8B4A"
                text_color = "#1a3300"
                label_bg = "#6B8B4A"
            elif to_status == "Partially fired":
                bg_color = "#FAC775"
                border_color = "#B58A1A"
                text_color = "#5A3F00"
                label_bg = "#B58A1A"
            else:
                # Unknown destination — legacy yellow
                bg_color = "#FFEBA1"
                border_color = "#C68A2A"
                text_color = "#5A3F00"
                label_bg = "#C68A2A"
            trigger_lines.append(
                f"<div style='background:{bg_color};border-left:4px solid {border_color};"
                f"padding:10px 14px;margin:8px 0;font-size:13px;color:{text_color};border-radius:3px'>"
                f"<span style='display:inline-block;background:{label_bg};color:#fff;"
                f"padding:2px 8px;border-radius:3px;font-size:11px;font-weight:700;"
                f"letter-spacing:0.3px;margin-right:8px'>NEW TRIGGER EVENT — TIER {trig.get('tier', '?')}</span>"
                f"<strong>{html.escape(trig.get('indicator_name', ''))}</strong> — {html.escape(description)}"
                f"</div>"
            )
        trigger_text = "\n".join(trigger_lines)
    else:
        trigger_text = (
            f"<div class='exec-line' style='color:#5F5E5A'>"
            f"<em>No new trigger events today.</em>"
            f"</div>"
        )

    # Probability / peak window
    base = es.base_case
    bull = es.bull_case
    bear = es.bear_case

    # v3.39: bear case can carry a NEGATIVE return when peak is already in.
    # Label each scenario as "upside" or "downside" based on sign so the
    # email reads naturally. The +/- formatter still includes the sign,
    # so a -8% bear renders as "downside -8%".
    def _ret_label(r: float) -> str:
        return "downside" if r < 0 else "upside"

    # v3.4.11 — phase-conditional anchor noun.
    # Phase 4: every case is framed against the cycle PEAK ahead of us.
    # Phase 5: the peak is behind us; every case is framed against the
    # TROUGH ahead of us. The return field stays signed; only the labels
    # and anchor noun change.
    is_phase_5 = int(getattr(es, "current_phase", 4) or 4) >= 5
    anchor_noun = "trough" if is_phase_5 else "peak"
    anchor_noun_cap = "Trough" if is_phase_5 else "Peak"

    peak_line = (
        f"<div class='exec-line' style='margin-top:14px'>"
        f"Based on latest information, our <strong>base case "
        f"(prob {base.probability_pct:.0f}%)</strong> estimates the {anchor_noun} is "
        f"<strong>{base.peak_months_away:.0f} months away</strong>, "
        f"placing it between <strong>{html.escape(base.peak_window_start)}</strong> "
        f"and <strong>{html.escape(base.peak_window_end)}</strong>. "
        f"On our base case the {_ret_label(base.return_to_peak_pct)} to {anchor_noun} is estimated at "
        f"<strong>{base.return_to_peak_pct:+.0f}%</strong>."
        f"</div>"
    )

    cases_line = (
        f"<div class='exec-line'>"
        f"<span class='case-block case-bear'>Bear case ({bear.probability_pct:.0f}%): "
        f"{anchor_noun} in {bear.peak_months_away:.0f}mo, "
        f"{_ret_label(bear.return_to_peak_pct)} {bear.return_to_peak_pct:+.0f}%</span>"
        f"<span class='case-block case-base'>Base case ({base.probability_pct:.0f}%): "
        f"{anchor_noun} in {base.peak_months_away:.0f}mo, "
        f"{_ret_label(base.return_to_peak_pct)} {base.return_to_peak_pct:+.0f}%</span>"
        f"<span class='case-block case-bull'>Bull case ({bull.probability_pct:.0f}%): "
        f"{anchor_noun} in {bull.peak_months_away:.0f}mo, "
        f"{_ret_label(bull.return_to_peak_pct)} {bull.return_to_peak_pct:+.0f}%</span>"
        f"</div>"
    )
    # v3.41.17 — scenario anchor context: the bear return is now computed
    # from current price against the cycle-peak trough anchor; show the
    # arithmetic so the number is never ambiguous again.
    _sa = getattr(es, "scenario_anchor", None)
    if isinstance(_sa, dict):
        try:
            _band = _sa.get("band") or (None, None)
            cases_line += (
                f"<div class='exec-line' style='font-size:12px;color:#5F5E5A'>"
                f"Scenario anchor: SK Hynix ₩{_sa['current_price']:,.0f} vs cycle peak "
                f"₩{_sa['peak_price']:,.0f} (<strong>{_sa['drawdown_now_pct']:+.0f}%</strong> from peak already). "
                f"Bear trough anchor {_sa['bear_trough_from_peak_pct']:.0f}% from peak "
                f"(historical band {_band[0]:.0f} to {_band[1]:.0f}%); bear return above is computed "
                f"from the current price.</div>"
            )
        except Exception:
            pass

    # v3.39.7.6 — `delta_line` construction removed entirely. v3.39.7.5
    # was supposed to consolidate the old three-block pattern
    # (delta_from_prior_text + big_movers + market_summary) into the
    # single `executive_brief` paragraph, but the delta_line render
    # survived the cleanup by mistake and shipped duplicating content
    # with the new brief. The `delta_from_prior_text` field stays on the
    # ExecutiveSummary dataclass for prior_state.json back-compat
    # (same pattern as the URGENT fields kept after v3.39.7.5).

    # v3.39.7.5 — consolidated executive brief paragraph.
    # Replaces the old 3-block pattern (delta_from_prior_text prose alone +
    # big_movers list + market_summary). Sonnet writes ONE 5-8 sentence
    # paragraph covering 4 themes (signal changes, big news with directional-
    # signal prioritization, big movers, macro). Anchor links of the form
    # `[→§N]` are substituted into clickable HTML pointing to section ids.
    if es.executive_brief:
        # Escape HTML first to prevent injection, then substitute anchor links.
        # The Sonnet prompt explicitly emits the literal token `[→§N]` (with
        # the arrow + section sign + number + brackets), so we look for that
        # exact pattern in the escaped string.
        escaped = html.escape(es.executive_brief)
        # The arrow → and section sign § survive html.escape (only <,>,&," get
        # escaped). Sub them: [→§N] → clickable HTML link.
        brief_with_links = re.sub(
            r"\[→§(\d+)\]",
            lambda m: f"<a href='#section-{m.group(1)}' class='anchor-link'>§{m.group(1)}</a>",
            escaped,
        )
        brief_block = (
            f"<div class='exec-brief'>"
            f"{brief_with_links}"
            f"</div>"
        )
    else:
        brief_block = ""

    return (
        f"<div class='exec-summary'>"
        f"<h2>Executive summary</h2>"
        f"{phase_text}"
        f"{trigger_text}"
        f"{peak_line}"
        f"{cases_line}"
        f"{brief_block}"
        f"</div>"
    )


def _build_big_movers(movers: list[BigMover]) -> str:
    """Render big movers (>+/-4% in 1 session) callout."""
    parts = ["<div class='big-movers'><strong>Big movers today (>±4% in 1 session):</strong> "]
    chunks = []
    for m in movers:
        color = "#0F6E56" if m.direction == "up" else "#A32D2D"
        arrow = "▲" if m.direction == "up" else "▼"
        chunks.append(
            f"<span style='color:{color};font-weight:600'>"
            f"{arrow} {html.escape(m.name)} ({html.escape(m.ticker)}) {m.change_1d_pct:+.2f}%"
            f"</span>"
        )
    parts.append(" · ".join(chunks))
    parts.append("</div>")
    return "".join(parts)


def _build_performance_table(performance: list[PerformanceMetrics]) -> str:
    """Performance table next to / below the chart."""
    rows = ["<table>", "<thead><tr>",
            "<th>Ticker</th><th>Last</th><th>1D</th><th>1W</th><th>1M</th><th>3M</th><th>6M</th><th>1Y</th>",
            "</tr></thead><tbody>"]

    for p in performance:
        currency_str = f"{p.current_price:,.2f} {p.currency}"
        rows.append("<tr>")
        rows.append(f"<td><strong>{html.escape(p.name)}</strong><br/><span style='color:#888;font-size:11px'>{p.ticker}</span></td>")
        rows.append(f"<td>{currency_str}</td>")
        for change in (p.change_1d, p.change_1w, p.change_1m, p.change_3m, p.change_6m, p.change_1y):
            rows.append(f"<td>{_format_change(change)}</td>")
        rows.append("</tr>")

    rows.append("</tbody></table>")
    return "\n".join(rows)


def _format_change(value: Optional[float]) -> str:
    if value is None:
        return "<span class='perf-flat'>n/a</span>"
    if value > 0.05:
        return f"<span class='perf-pos'>+{value:.2f}%</span>"
    if value < -0.05:
        return f"<span class='perf-neg'>{value:.2f}%</span>"
    return f"<span class='perf-flat'>{value:.2f}%</span>"


# v3.4.3.1 — _build_dhcr_section3_snippet (the v3.4.0 direction-monitor
# snippet) was DELETED in this release. It rendered a 5-level direction
# enum + v3.4.0 fields (frontier_hbm_gb_per_accelerator,
# capacity_creation_yoy_pct, implied_context_capacity_per_accelerator_tokens)
# all of which were eliminated by the v3.4.2 matrix redesign. DHCR no
# longer appears in section 3 at all. The function had to go with the row.



# ─────────────────────────────────────────────────────────────────────────────
# v3.4.4 — Indicator 7 (Competitor dynamics) section 4 deep-dive card
# ─────────────────────────────────────────────────────────────────────────────


def _build_competitor_sub_card(sub: dict) -> str:
    """Render one sub-indicator card (7A/7B/7C/7D) inside the Indicator 7 card.

    sub is a dict form of SubIndicatorState. 7D additionally renders the
    🛡️ moat-extension + ⚔️ substitution-risk milestone boxes.
    """
    sub_id = sub.get("sub_id", "?")
    name = sub.get("display_name", "?")
    status = sub.get("status", "Not fired")
    eps_impact = float(sub.get("estimated_eps_impact_pct", 0))
    summary = sub.get("current_state_summary", "")
    status_color = STATUS_COLORS.get(status, "#CCCCCC")
    text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    parts = []
    parts.append(
        "<div style='margin:10px 0;padding:12px;background:#FFFFFF;"
        "border:1px solid #DDD;border-radius:4px'>"
    )
    parts.append(
        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:6px'>"
        f"<strong style='color:#1a1a1a;font-size:13px'>{html.escape(sub_id)} — {html.escape(name)}</strong>"
        f"<span style='background:{status_color};color:{text_color};"
        f"padding:3px 10px;border-radius:3px;font-weight:700;font-size:11px'>"
        f"{html.escape(status)}</span>"
        f"<span style='color:#666;font-size:11px'>est. EPS impact ~{eps_impact:.1f}%</span>"
        f"</div>"
    )
    parts.append(
        f"<div style='font-size:12px;color:#333;line-height:1.55'>{html.escape(summary)}</div>"
    )

    # 7D-specific: render moat-extension + substitution-risk boxes
    moat_milestones = sub.get("moat_extension_milestones", []) or []
    sub_risk_milestones = sub.get("substitution_risk_milestones", []) or []
    if moat_milestones or sub_risk_milestones:
        parts.append("<div style='margin-top:10px;display:table;width:100%;table-layout:fixed'>")
        # Moat-extension box (bull-case watch)
        if moat_milestones:
            parts.append(
                "<div style='display:table-cell;width:50%;padding-right:5px;vertical-align:top'>"
                "<div style='background:#E8F5E9;border:1px solid #4CAF50;border-radius:4px;"
                "padding:8px 10px;font-size:11px;color:#1a3300;line-height:1.5'>"
                "<strong style='color:#1B5E20'>🛡️ Moat-extension watch (bull triggers)</strong>"
                "<ul style='margin:4px 0 0 0;padding-left:18px'>"
            )
            for m in moat_milestones[:5]:
                parts.append(f"<li>{html.escape(m)}</li>")
            parts.append("</ul></div></div>")
        # Substitution-risk box (bear-case watch)
        if sub_risk_milestones:
            parts.append(
                "<div style='display:table-cell;width:50%;padding-left:5px;vertical-align:top'>"
                "<div style='background:#FFEBEE;border:1px solid #E57373;border-radius:4px;"
                "padding:8px 10px;font-size:11px;color:#5A1A1A;line-height:1.5'>"
                "<strong style='color:#B71C1C'>⚔️ Substitution-risk watch (bear triggers)</strong>"
                "<ul style='margin:4px 0 0 0;padding-left:18px'>"
            )
            for m in sub_risk_milestones[:5]:
                parts.append(f"<li>{html.escape(m)}</li>")
            parts.append("</ul></div></div>")
        parts.append("</div>")

    parts.append("</div>")
    return "\n".join(parts)


def _build_competitor_section4_card(comp: Optional[dict]) -> str:
    """v3.4.9 — Indicator 7 (Competitor dynamics) section 4 card.

    REBUILT to match cards 1-5 structure: header + status pill, then
    "What we're looking for / Where we stand / Latest developments /
    source links" labeled paragraphs. The 4 sub-indicators (7A CXMT,
    7B Samsung HBM4/4E, 7C Micron, 7D HBF) are folded into prose with
    their statuses inline, instead of separate sub-cards.

    Previous v3.4.4 layout (4 stacked sub-cards each with own status
    pill, dual-threat banner, framework footer) is dropped per user
    direction 2026-05-23: look-and-feel must match cards 1-5.
    """
    if not comp:
        return ""

    status = comp.get("composite_status", "Not fired")
    status_color = comp.get("composite_status_color", "#C0DD97")
    reasoning = comp.get("composite_reasoning", "")
    cumulative_eps = float(comp.get("cumulative_eps_impact_pct", 0))
    subs = comp.get("subs", []) or []
    dual_threat_active = bool(comp.get("dual_threat_active", False))
    dual_threat_text = comp.get("dual_threat_scenario_text", "")
    dual_combined = comp.get("dual_threat_impact_combined_pct", "-30 to -45")
    dual_with_peak = comp.get("dual_threat_impact_with_peak_pct", "-60 to -75")

    text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    parts = []
    # Header (matches cards 1-5: h3 + status badge inline)
    # v3.7.4 — dropped "Indicator 7 — " prefix per consistency with the other
    # section-4 cards (DHCR / cards 1-5), none of which carry the "Indicator X — "
    # prefix in their title. The numbering is implicit from the section's order.
    parts.append(
        f"<h3>Competitor dynamics "
        f"<span class='status-badge' "
        f"style='background:{status_color};color:{text_color};margin-left:8px'>"
        f"{html.escape(status)}</span></h3>"
    )

    # What we're looking for
    parts.append(
        "<p style='font-size:13px'><strong>What we're looking for:</strong> "
        "Four sub-indicators tracking competitive threats to SK Hynix's HBM moat, "
        "calibrated to <em>estimated SK Hynix EPS impact</em> (price × volume), "
        "not raw market share. Sub-thresholds: Not fired &lt;5% EPS impact, "
        "Partially fired 5–20%, Fired &gt;20%. <strong>7A</strong> CXMT (Chinese "
        "DRAM market entry — DDR5 is the trigger, DDR4 is ignored). "
        "<strong>7B</strong> Samsung HBM4/HBM4E (50% of NVIDIA HBM4 demand is "
        "Partially fired threshold; 70% triggers Fired). <strong>7C</strong> Micron "
        "(only counts if Micron eats SK Hynix's share specifically, not Samsung's). "
        "<strong>7D</strong> HBF / hot-tier displacement (HBM-Flash architecture "
        "substitution + Solidigm watch). Composite applies the Part 14 dual-threat "
        "amplifier when 2+ subs fire (or 1 Fired + 2+ Partially fired)."
        "</p>"
    )

    # Where we stand — composite read + per-sub status in prose
    # v3.41.10 — key fix: subs are asdict(SubIndicatorState) with fields
    # `sub_id` / `current_state_summary`; this loop read `id` / `summary`,
    # so every line rendered "?" for the id and a dangling ": ;" where the
    # (real, populated) summary should be. Read the producer's keys with the
    # old names as fallback, and only emit the colon when there is a summary.
    sub_lines = []
    for sub in subs:
        sub_id = sub.get("sub_id") or sub.get("id") or "?"
        sub_name = sub.get("display_name") or sub.get("name") or "?"
        sub_status = sub.get("status", "Not fired")
        sub_summary = sub.get("current_state_summary") or sub.get("summary") or ""
        eps_impact = sub.get("estimated_eps_impact_pct")
        eps_text = f" (~{eps_impact:.1f}% EPS impact)" if eps_impact is not None else ""
        sub_lines.append(
            f"<strong>{html.escape(sub_id)} {html.escape(sub_name)}</strong> — "
            f"{html.escape(sub_status)}{eps_text}"
            + (f": {html.escape(sub_summary)}" if sub_summary else "")
        )

    where_we_stand_parts = []
    where_we_stand_parts.append(
        f"<strong>COMPUTED METRICS:</strong> Composite status "
        f"<strong>{html.escape(status)}</strong>; cumulative EPS impact across all "
        f"subs ~<strong>{cumulative_eps:.1f}%</strong>; "
        f"{sum(1 for s in subs if s.get('status') == 'Fired')} of {len(subs)} subs "
        f"Fired, {sum(1 for s in subs if s.get('status') == 'Partially fired')} "
        f"Partially fired."
    )
    if reasoning:
        where_we_stand_parts.append(html.escape(reasoning))
    if sub_lines:
        # Inline sub statuses as a sentence list
        where_we_stand_parts.append("Sub-indicators: " + "; ".join(sub_lines) + ".")
    if dual_threat_active:
        where_we_stand_parts.append(
            f"<strong>⚠️ DUAL-THREAT SCENARIO ACTIVE</strong> — combined CXMT + "
            f"Samsung stock impact <strong>{html.escape(dual_combined)}%</strong>; "
            f"with cycle-peak overlay <strong>{html.escape(dual_with_peak)}%</strong>. "
            f"{html.escape(dual_threat_text)}"
        )
    parts.append(
        f"<p style='font-size:13px'><strong>Where we stand:</strong> "
        f"{' '.join(where_we_stand_parts)}</p>"
    )

    # Latest developments — pull from each sub's recent_news if available
    dev_lines = []
    for sub in subs:
        recent = sub.get("recent_news", []) or []
        if recent:
            sub_id = sub.get("sub_id") or sub.get("id") or "?"
            # Take the top 1-2 most relevant news items per sub
            for item in recent[:2]:
                title = item.get("title", "")
                date = item.get("date", "")
                if title:
                    dev_lines.append(
                        f"<strong>{html.escape(sub_id)}:</strong> "
                        f"{html.escape(title)}"
                        + (f" ({html.escape(date)})" if date else "")
                    )
    # v3.41.10 — card-level items (triage-mapped news that didn't route to a
    # specific sub) render without a sub prefix, before the quiet-day filler.
    for item in (comp.get("recent_news") or [])[:3]:
        title = item.get("title", "")
        date = item.get("date", "")
        if title:
            dev_lines.append(
                f"{html.escape(title)}"
                + (f" ({html.escape(date)})" if date else "")
            )
    if not dev_lines:
        dev_lines.append("No new developments today across the four sub-indicators.")
    parts.append(
        f"<p style='font-size:13px'><strong>Latest developments:</strong> "
        f"{' '.join(dev_lines)}</p>"
    )

    # Source links — pull URLs from sub recent_news, or use canonical sources
    link_urls = []
    seen = set()
    for sub in subs:
        for item in (sub.get("recent_news", []) or [])[:3]:
            url = item.get("url")
            title = item.get("title", "source")
            if url and url not in seen:
                link_urls.append((title, url))
                seen.add(url)
    # v3.41.10 — card-level recent_news URLs too
    for item in (comp.get("recent_news") or [])[:3]:
        url = item.get("url")
        title = item.get("title", "source")
        if url and url not in seen:
            link_urls.append((title, url))
            seen.add(url)
    # Always include some canonical sources for context
    canonical = [
        ("DRAMeXchange", "https://www.dramexchange.com/"),
        ("TrendForce Press", "https://www.trendforce.com/news/"),
        ("CXMT news — Google", "https://news.google.com/search?q=CXMT+DDR5"),
    ]
    for title, url in canonical:
        if url not in seen and len(link_urls) < 5:
            link_urls.append((title, url))
            seen.add(url)
    link_html = " · ".join(
        f"<a href='{html.escape(url)}' target='_blank'>{html.escape(title[:80])}</a>"
        for title, url in link_urls[:5]
    )
    parts.append(f"<p class='links-list' style='font-size:12px'>{link_html}</p>")

    return "".join(parts)


def _build_competitor_section4_card_legacy(comp: Optional[dict]) -> str:
    """v3.4.4 — Indicator 7 (Competitor dynamics) deep-dive card.

    Structure:
      - Header: title + composite status pill + "N of 4 subs firing" sub-badge
      - 4 sub-cards (7A/7B/7C/7D), each with sub-status pill + summary +
        (for 7D) moat-extension + substitution-risk boxes
      - Composite reasoning (cumulative EPS impact + dual-threat status)
      - Dual-threat scenario callout (per Part 14) — amber background
      - Footer with refresh cadence
    """
    if not comp:
        return ""

    status = comp.get("composite_status", "Not fired")
    status_color = comp.get("composite_status_color", "#C0DD97")
    reasoning = comp.get("composite_reasoning", "")
    cumulative_eps = float(comp.get("cumulative_eps_impact_pct", 0))
    subs = comp.get("subs", []) or []
    dual_threat_active = bool(comp.get("dual_threat_active", False))
    dual_threat_text = comp.get("dual_threat_scenario_text", "")
    dual_combined = comp.get("dual_threat_impact_combined_pct", "-30 to -45")
    dual_with_peak = comp.get("dual_threat_impact_with_peak_pct", "-60 to -75")
    last_refresh = comp.get("last_refresh", "unknown")

    n_fired = sum(1 for s in subs if s.get("status") == "Fired")
    n_partial = sum(1 for s in subs if s.get("status") == "Partially fired")
    sub_badge_text = f"{n_fired + n_partial} of {len(subs)} subs firing"

    text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    parts = []
    parts.append(
        "<div style='margin:24px 0;border:2px solid #555;background:#FAFAFA;"
        "padding:18px;border-radius:5px'>"
    )

    # Header
    parts.append(
        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap'>"
        f"<h3 style='margin:0;color:#1a1a1a;display:inline'>Indicator 7 — Competitor dynamics</h3>"
        f"<span style='background:{status_color};color:{text_color};padding:6px 14px;"
        f"border-radius:4px;font-weight:700;font-size:13px'>{html.escape(status)}</span>"
        f"<span style='background:#444;color:#fff;padding:6px 12px;border-radius:4px;"
        f"font-weight:600;font-size:11px'>{html.escape(sub_badge_text)}</span>"
        f"<span style='font-size:12px;color:#666'>cumulative EPS impact ~{cumulative_eps:.1f}%</span>"
        f"</div>"
    )

    # Composite reasoning
    parts.append(
        f"<div style='font-size:12px;color:#444;margin-bottom:14px;font-style:italic;"
        f"padding:10px 12px;background:#FFFFFF;border-left:3px solid {status_color}'>"
        f"<strong style='color:#1a1a1a;font-style:normal'>READ:</strong> "
        f"{html.escape(reasoning)}"
        f"</div>"
    )

    # Sub-indicator cards (4 stacked vertically per v3.4.4 layout decision)
    for sub in subs:
        parts.append(_build_competitor_sub_card(sub))

    # Dual-threat scenario callout — always rendered (educational even when inactive)
    dual_threat_bg = "#FFEBEE" if dual_threat_active else "#FFF8E1"
    dual_threat_border = "#B71C1C" if dual_threat_active else "#B58A1A"
    dual_threat_header = "⚠️ DUAL-THREAT SCENARIO — ACTIVE" if dual_threat_active else "ℹ️ DUAL-THREAT SCENARIO (reference)"
    parts.append(
        f"<div style='margin:14px 0;padding:12px;background:{dual_threat_bg};"
        f"border:2px solid {dual_threat_border};border-radius:4px;"
        f"font-size:12px;color:#333;line-height:1.55'>"
        f"<strong style='color:#1a1a1a'>{html.escape(dual_threat_header)}</strong><br/>"
        f"Combined stock impact (CXMT + Samsung simultaneously): "
        f"<strong>{html.escape(dual_combined)}%</strong>. "
        f"With cycle-peak overlay (Phase 4 window Q3 2026 - Q1 2027): "
        f"<strong>{html.escape(dual_with_peak)}%</strong>.<br/>"
        f"<div style='margin-top:6px;color:#555;font-size:11px'>"
        f"{html.escape(dual_threat_text)}</div>"
        f"</div>"
    )

    # Footer
    parts.append(
        f"<div style='margin-top:14px;padding-top:10px;border-top:1px solid #DDD;"
        f"font-size:11px;color:#666'>"
        f"<strong>FRAMEWORK NOTE:</strong> Sub-indicator statuses calibrated to "
        f"estimated SK Hynix EPS impact (price × volume), not raw market share. "
        f"Not fired &lt;5% EPS, Partially fired 5-20%, Fired &gt;20%. Composite "
        f"applies the Part 14 dual-threat amplifier when 2+ subs Fire (or 1 Fired + "
        f"2+ Partially fired). Last YAML refresh: {html.escape(last_refresh)}."
        f"</div>"
    )

    parts.append("</div>")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# v3.4.4 — Indicator 8 (US ADR / SPO event) section 4 deep-dive card
# ─────────────────────────────────────────────────────────────────────────────


def _build_adr_sub_card(sub: dict) -> str:
    """Render one sub-indicator card (8A/8B/8C/8D)."""
    sub_id = sub.get("sub_id", "?")
    name = sub.get("display_name", "?")
    status = sub.get("status", "Not fired")
    status_pill_label = sub.get("status_pill_label", status)
    what_we_watch = sub.get("what_we_watch", []) or []
    summary = sub.get("current_state_summary", "")

    # Pre-event uses a distinct grey pill
    if status == "Pre-event":
        status_color = "#B0BEC5"
        text_color = "#1a1a1a"
    else:
        status_color = STATUS_COLORS.get(status, "#CCCCCC")
        text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    parts = []
    parts.append(
        "<div style='margin:10px 0;padding:12px;background:#FFFFFF;"
        "border:1px solid #DDD;border-radius:4px'>"
    )
    parts.append(
        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap'>"
        f"<strong style='color:#1a1a1a;font-size:13px'>{html.escape(sub_id)} — {html.escape(name)}</strong>"
        f"<span style='background:{status_color};color:{text_color};"
        f"padding:3px 10px;border-radius:3px;font-weight:700;font-size:11px'>"
        f"{html.escape(status_pill_label)}</span>"
        f"</div>"
    )
    parts.append(
        f"<div style='font-size:12px;color:#333;line-height:1.55;margin-bottom:8px'>"
        f"{html.escape(summary)}</div>"
    )
    if what_we_watch:
        parts.append(
            "<div style='font-size:11px;color:#666'>"
            "<strong style='color:#1a1a1a'>What we watch:</strong>"
            "<ul style='margin:4px 0 0 0;padding-left:18px'>"
        )
        for w in what_we_watch[:5]:
            parts.append(f"<li>{html.escape(w)}</li>")
        parts.append("</ul></div>")
    parts.append("</div>")
    return "\n".join(parts)


def _build_adr_section4_card(adr: Optional[dict]) -> str:
    """v3.4.9 — Indicator 8 (US ADR / SPO event tracking) section 4 card.

    REBUILT to match cards 1-5 structure: header + status pill, then
    "What we're looking for / Where we stand / Latest developments /
    source links" labeled paragraphs. Phase lifecycle, offering structure,
    sub-statuses, calendar milestones, and academic-calibration prose all
    folded into the four standard paragraphs.

    Previous v3.4.4 layout (phase banner, reasoning box, peak-coincidence
    banner, offering structure block, 4 sub-cards, calendar block,
    academic-calibration box) is dropped per user direction 2026-05-23.
    """
    if not adr:
        return ""

    status = adr.get("composite_status", "Not fired")
    status_color = adr.get("composite_status_color", "#C0DD97")
    reasoning = adr.get("composite_reasoning", "")
    phase = adr.get("lifecycle_phase", "pre_event")
    subs = adr.get("subs", []) or []
    calendar = adr.get("calendar", []) or []
    offering = adr.get("confirmed_offering_structure", {}) or {}
    academic = adr.get("academic_calibration", {}) or {}
    peak_coincidence_active = bool(adr.get("peak_coincidence_active", False))

    text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    phase_banner_text = {
        "pre_event": "Awaiting F-1 filing — expected June-July 2026",
        "f1_filed": "F-1 filed — roadshow window opens",
        "roadshow": "Roadshow active — institutional demand signals incoming",
        "pricing": "Pricing day window — 424B filing imminent",
        "trading": "First trading day — ADR live on NYSE/NASDAQ",
        "completed": "ADR completed — monitoring mechanical drag fade",
        "canceled": "ADR canceled — sustained weakness signal",
    }
    phase_text = phase_banner_text.get(phase, phase)

    parts = []
    # Header
    # v3.7.4 — dropped "Indicator 8 — " prefix per consistency with the other
    # section-4 cards (DHCR / cards 1-5), none of which carry the "Indicator X — "
    # prefix in their title.
    parts.append(
        f"<h3>US ADR / SPO event tracking "
        f"<span class='status-badge' "
        f"style='background:{status_color};color:{text_color};margin-left:8px'>"
        f"{html.escape(status)}</span></h3>"
    )

    # What we're looking for
    parts.append(
        "<p style='font-size:13px'><strong>What we're looking for:</strong> "
        "A US ADR (or SPO) by SK Hynix as a peak-coincidence signal — listed "
        "1995 pre-1996 crash pattern per memory_supercycle_analysis.docx Part 11. "
        "Four sub-indicators track lifecycle stages: <strong>8A</strong> F-1 "
        "filing watch (SEC EDGAR — pending filing is Partial, canceled F-1 is "
        "Fired and persists through Phase 4 with no 90-day reversion). "
        "<strong>8B</strong> roadshow institutional demand signals. "
        "<strong>8C</strong> pricing day proximity (424B filing). "
        "<strong>8D</strong> insider sales &gt;$10M threshold within 90-day window. "
        "Composite escalates to Fired automatically when ADR completion is "
        "coincident with late Phase 4 cycle state (peak-coincidence override)."
        "</p>"
    )

    # Where we stand
    where_we_stand_parts = []
    where_we_stand_parts.append(
        f"<strong>COMPUTED METRICS:</strong> Phase <strong>{html.escape(phase)}</strong> "
        f"— {html.escape(phase_text)}. Composite status: <strong>{html.escape(status)}</strong>."
    )
    if peak_coincidence_active:
        where_we_stand_parts.append(
            "<strong>⚠️ PEAK-COINCIDENCE ESCALATION ACTIVE</strong> — composite "
            "escalated to Fired regardless of individual sub statuses; ADR completion "
            "is coincident with late Phase 4 cycle state."
        )
    if offering:
        size_m = offering.get("new_shares_issued_count_millions_est", "?")
        dilution = offering.get("share_capital_pct_dilution_est", "?")
        raise_central = offering.get("estimated_raise_usd_b_central", "?")
        where_we_stand_parts.append(
            f"Confirmed offering structure: ~<strong>{size_m}M new shares</strong> "
            f"({dilution}% dilution), estimated raise "
            f"<strong>${raise_central}B</strong> (central estimate)."
        )
    if reasoning:
        where_we_stand_parts.append(html.escape(reasoning))
    # v3.41.10 — same producer/consumer key fix as the competitor card: subs
    # are asdict(ADRSubState) with `sub_id` / `current_state_summary`.
    sub_lines = []
    for sub in subs:
        sub_id = sub.get("sub_id") or sub.get("id") or "?"
        sub_name = sub.get("display_name") or sub.get("name") or "?"
        sub_status = sub.get("status", "Not fired")
        sub_summary = sub.get("current_state_summary") or sub.get("summary") or ""
        sub_lines.append(
            f"<strong>{html.escape(sub_id)} {html.escape(sub_name)}</strong> — "
            f"{html.escape(sub_status)}"
            + (f": {html.escape(sub_summary)}" if sub_summary else "")
        )
    if sub_lines:
        where_we_stand_parts.append("Sub-indicators: " + "; ".join(sub_lines) + ".")
    if calendar:
        # v3.41.10 — milestones are asdict(ADRCalendarMilestone) with fields
        # `date_window` / `event`; this read `date` / `description`, so every
        # milestone rendered as the literal "?: " while the real yaml content
        # was discarded. Read the producer's keys (old names as fallback) and
        # skip entries with neither populated.
        cal_items = []
        for e in calendar[:3]:
            when = e.get("date_window") or e.get("date") or ""
            what = e.get("event") or e.get("description") or ""
            if not (when or what):
                continue
            cal_items.append(
                f"{html.escape(when or '?')}: {html.escape(what)}" if what
                else html.escape(when)
            )
        if cal_items:
            where_we_stand_parts.append(f"Upcoming milestones: {'; '.join(cal_items)}.")
    if academic:
        ac_summary = academic.get("summary") or academic.get("notes", "")
        if ac_summary:
            where_we_stand_parts.append(html.escape(ac_summary))
    parts.append(
        f"<p style='font-size:13px'><strong>Where we stand:</strong> "
        f"{' '.join(where_we_stand_parts)}</p>"
    )

    # Latest developments
    dev_lines = []
    for sub in subs:
        recent = sub.get("recent_news", []) or []
        for item in recent[:2]:
            title = item.get("title", "")
            date = item.get("date", "")
            sub_id = sub.get("sub_id") or sub.get("id") or "?"
            if title:
                dev_lines.append(
                    f"<strong>{html.escape(sub_id)}:</strong> "
                    f"{html.escape(title)}"
                    + (f" ({html.escape(date)})" if date else "")
                )
    # v3.41.10 — card-level items (triage-mapped news that didn't route to a
    # specific sub) render without a sub prefix, before the quiet-day filler.
    for item in (adr.get("recent_news") or [])[:3]:
        title = item.get("title", "")
        date = item.get("date", "")
        if title:
            dev_lines.append(
                f"{html.escape(title)}"
                + (f" ({html.escape(date)})" if date else "")
            )
    if not dev_lines:
        dev_lines.append("No new developments today across the four sub-indicators.")
    parts.append(
        f"<p style='font-size:13px'><strong>Latest developments:</strong> "
        f"{' '.join(dev_lines)}</p>"
    )

    # Source links
    link_urls = []
    seen = set()
    for sub in subs:
        for item in (sub.get("recent_news", []) or [])[:3]:
            url = item.get("url")
            title = item.get("title", "source")
            if url and url not in seen:
                link_urls.append((title, url))
                seen.add(url)
    # v3.41.10 — card-level recent_news URLs too
    for item in (adr.get("recent_news") or [])[:3]:
        url = item.get("url")
        title = item.get("title", "source")
        if url and url not in seen:
            link_urls.append((title, url))
            seen.add(url)
    canonical = [
        ("SEC EDGAR — SK Hynix F-1 watch", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix"),
        ("Korea Exchange — SK Hynix disclosures", "https://kind.krx.co.kr/"),
        ("WSJ — IPO / SPO calendar", "https://www.wsj.com/market-data/stocks/ipo"),
    ]
    for title, url in canonical:
        if url not in seen and len(link_urls) < 5:
            link_urls.append((title, url))
            seen.add(url)
    link_html = " · ".join(
        f"<a href='{html.escape(url)}' target='_blank'>{html.escape(title[:80])}</a>"
        for title, url in link_urls[:5]
    )
    parts.append(f"<p class='links-list' style='font-size:12px'>{link_html}</p>")

    return "".join(parts)


def _build_adr_section4_card_legacy(adr: Optional[dict]) -> str:
    """v3.4.4 — Indicator 8 (US ADR / SPO event) deep-dive card.

    Structure:
      - Header: title + composite status pill + lifecycle-phase badge
      - Composite reasoning
      - Confirmed offering structure (size, dilution, raise, use of proceeds)
      - 4 sub-cards (8A/8B/8C/8D)
      - Calendar milestones inline
      - Academic calibration prose (mechanical drag references)
      - Peak-coincidence callout if active
      - Footer
    """
    if not adr:
        return ""

    status = adr.get("composite_status", "Not fired")
    status_color = adr.get("composite_status_color", "#C0DD97")
    reasoning = adr.get("composite_reasoning", "")
    phase = adr.get("lifecycle_phase", "pre_event")
    subs = adr.get("subs", []) or []
    calendar = adr.get("calendar", []) or []
    offering = adr.get("confirmed_offering_structure", {}) or {}
    academic = adr.get("academic_calibration", {}) or {}
    peak_coincidence_active = bool(adr.get("peak_coincidence_active", False))
    last_refresh = adr.get("last_refresh", "unknown")

    text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    # Phase-specific banner text (rendered above the header)
    phase_banner_text = {
        "pre_event": "Awaiting F-1 filing — expected June-July 2026",
        "f1_filed": "F-1 filed — roadshow window opens",
        "roadshow": "Roadshow active — institutional demand signals incoming",
        "pricing": "Pricing day window — 424B filing imminent",
        "trading": "First trading day — ADR live on NYSE/NASDAQ",
        "completed": "ADR completed — monitoring mechanical drag fade",
        "canceled": "ADR canceled — sustained weakness signal",
    }
    phase_banner = phase_banner_text.get(phase, phase)

    parts = []
    parts.append(
        "<div style='margin:24px 0;border:2px solid #555;background:#FAFAFA;"
        "padding:18px;border-radius:5px'>"
    )

    # Phase banner
    parts.append(
        f"<div style='background:#E3F2FD;color:#0D47A1;padding:6px 12px;"
        f"border-radius:3px;font-size:12px;font-weight:600;margin-bottom:10px'>"
        f"📅 {html.escape(phase_banner)}"
        f"</div>"
    )

    # Header
    parts.append(
        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap'>"
        f"<h3 style='margin:0;color:#1a1a1a;display:inline'>Indicator 8 — US ADR / SPO event tracking</h3>"
        f"<span style='background:{status_color};color:{text_color};padding:6px 14px;"
        f"border-radius:4px;font-weight:700;font-size:13px'>{html.escape(status)}</span>"
        f"</div>"
    )

    # Composite reasoning
    parts.append(
        f"<div style='font-size:12px;color:#444;margin-bottom:14px;font-style:italic;"
        f"padding:10px 12px;background:#FFFFFF;border-left:3px solid {status_color}'>"
        f"<strong style='color:#1a1a1a;font-style:normal'>READ:</strong> "
        f"{html.escape(reasoning)}"
        f"</div>"
    )

    # Peak-coincidence callout (only if active)
    if peak_coincidence_active:
        parts.append(
            "<div style='margin:10px 0;padding:12px;background:#FFEBEE;"
            "border:2px solid #B71C1C;border-radius:4px;"
            "font-size:12px;color:#5A1A1A;line-height:1.55'>"
            "<strong style='color:#B71C1C'>⚠️ PEAK-COINCIDENCE ESCALATION ACTIVE</strong><br/>"
            "ADR completion is coincident with late Phase 4 cycle state per "
            "memory_supercycle_analysis.docx Part 11 (Hynix 1995 listing → 1996 "
            "crash pattern). Indicator 8 escalated to Fired regardless of "
            "individual sub statuses."
            "</div>"
        )

    # Confirmed offering structure
    if offering:
        size_m = offering.get("new_shares_issued_count_millions_est", "?")
        dilution = offering.get("share_capital_pct_dilution_est", "?")
        raise_low = offering.get("estimated_raise_usd_b_low", "?")
        raise_central = offering.get("estimated_raise_usd_b_central", "?")
        raise_high = offering.get("estimated_raise_usd_b_high", "?")
        use_of_proceeds = offering.get("use_of_proceeds", []) or []
        parts.append(
            "<div style='margin:12px 0;padding:10px 12px;background:#FFFFFF;"
            "border-left:3px solid #2C5AA0;font-size:12px;color:#333;line-height:1.55'>"
            "<strong style='color:#1a1a1a'>CONFIRMED OFFERING STRUCTURE</strong><br/>"
            f"<strong>New shares issued:</strong> ~{size_m}M (~{dilution}% dilution)<br/>"
            f"<strong>Estimated raise:</strong> ${raise_low}B - ${raise_high}B (central case ${raise_central}B)<br/>"
            f"<strong>Path:</strong> New share issuance only — treasury share path closed Feb 9 2026<br/>"
        )
        if use_of_proceeds:
            parts.append("<strong>Use of proceeds:</strong> " + html.escape(", ".join(use_of_proceeds)))
        parts.append("</div>")

    # Sub-indicator cards
    parts.append("<div style='margin:14px 0'>")
    parts.append(
        "<div style='font-weight:600;color:#1a1a1a;font-size:13px;margin-bottom:6px'>"
        "Sub-indicator status"
        "</div>"
    )
    for sub in subs:
        parts.append(_build_adr_sub_card(sub))
    parts.append("</div>")

    # Calendar milestones
    if calendar:
        parts.append(
            "<div style='margin:14px 0;padding:10px 12px;background:#FAFAFA;"
            "border:1px solid #DDD;border-radius:4px;font-size:12px;color:#333;line-height:1.5'>"
            "<strong style='color:#1a1a1a'>📆 CALENDAR MILESTONES</strong>"
            "<table style='width:100%;margin-top:6px;border-collapse:collapse;font-size:11px'>"
            "<thead><tr style='background:#EEE'>"
            "<th style='text-align:left;padding:4px 8px'>Date window</th>"
            "<th style='text-align:left;padding:4px 8px'>Event</th>"
            "<th style='text-align:left;padding:4px 8px'>Sub</th>"
            "<th style='text-align:left;padding:4px 8px'>Mechanical impact</th>"
            "</tr></thead><tbody>"
        )
        for m in calendar:
            parts.append(
                f"<tr style='border-bottom:1px solid #EEE'>"
                f"<td style='padding:4px 8px;color:#555'>{html.escape(m.get('date_window', ''))}</td>"
                f"<td style='padding:4px 8px'>{html.escape(m.get('event', ''))}</td>"
                f"<td style='padding:4px 8px;color:#666;font-weight:600'>{html.escape(m.get('sub_indicator', ''))}</td>"
                f"<td style='padding:4px 8px;color:#666'>{html.escape(m.get('mechanical_impact', ''))}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table></div>")

    # Academic calibration
    if academic:
        mech_drag = academic.get("mechanical_drag_pct_range", "")
        fade = academic.get("fade_window_months", "")
        citations = academic.get("citations", []) or []
        ref_events = academic.get("reference_events", []) or []
        parts.append(
            "<div style='margin:14px 0;padding:10px 12px;background:#F0F4F8;"
            "border-left:3px solid #455A64;font-size:11px;color:#333;line-height:1.5'>"
            "<strong style='color:#1a1a1a'>ACADEMIC CALIBRATION (mechanical drag for established foreign issuers)</strong><br/>"
            f"Mechanical drag: <strong>{html.escape(mech_drag)}%</strong> cumulative; "
            f"fade window <strong>{html.escape(fade)} months</strong>.<br/>"
            f"Citations: {html.escape('; '.join(citations))}<br/>"
            f"Reference events:<ul style='margin:4px 0 0 0;padding-left:18px'>"
        )
        for r in ref_events[:5]:
            parts.append(f"<li>{html.escape(r)}</li>")
        parts.append("</ul></div>")

    # Footer
    parts.append(
        f"<div style='margin-top:14px;padding-top:10px;border-top:1px solid #DDD;"
        f"font-size:11px;color:#666'>"
        f"<strong>FRAMEWORK NOTE:</strong> Mechanical drag is small and "
        f"predictable; the framework-relevant value is the peak-coincidence "
        f"pattern per Part 11 — memory makers cross-listing at peak is a "
        f"recurring top-tick pattern (SK Hynix 1995 KRX listing preceded the "
        f"1996 crash). Fired persists through Phase 4 — no 90-day reversion "
        f"per the v3.4.4 framework decision. Last YAML refresh: {html.escape(last_refresh)}."
        f"</div>"
    )

    parts.append("</div>")
    return "\n".join(parts)



# ─────────────────────────────────────────────────────────────────────────────
# v3.4.4 — DHCR spectrum bar (per Raul's sketch May 22 2026)
# ─────────────────────────────────────────────────────────────────────────────


def _build_dhcr_spectrum_bar(score: int, market_zone: str = "") -> str:
    """v3.4.9.2 — DHCR spectrum bar (per Raul's iteration May 23 2026).

    Three things changed vs the original v3.4.4 sketch implementation:

      1. Colors flipped to financial-signal coding: GREEN at top (HBM =
         strong moat = positive for SK Hynix), RED at bottom (DDR commodity
         = no moat = negative). This intentionally diverges from the matrix
         palette, which colors by memory-type intensity; here we color by
         moat strength. The bar IS a moat-strength indicator.

      2. Zone boundaries recalibrated to natural cell-score breakpoints:
         0-30 / 30-50 / 50-65 / 65-80 / 80-100. This makes the cluster of
         four cells at score 52.5 (R1/C5 LPDDR/HBM, R3/C3 HBM/GDDR,
         R4/C2 HBM/GDDR, R5/C1 HBM) all land in a single "HBM-GDDR /
         hybrid" zone, matching the matrix cell labels at that score.

      3. Marker style per sketch — simple black triangle + plain
         "TODAY X/100" text label to the left of the bar. No callout box,
         no halo, no connector. (The matrix marker has those because it
         sits mid-2D-grid; the bar marker sits on a 1D axis where text
         fits naturally to the left.)

      4. Marker at EXACT score position. No fudging to a zone midpoint.
         If today's score is 52, the marker sits at the 52 line on the
         axis. Honest math.

      5. Column headers ("Memory type" / "SK Hynix moat") sit in their own
         row ABOVE the top of the bar so they label the entire column,
         not the top segment.
    """
    # Segments defined top-to-bottom (matches visual order — 100 at top).
    # COLORS FLIPPED vs v3.4.4: green at top (HBM = strong moat), red at
    # bottom (DDR commodity = no moat).
    segments = [
        {
            "score_range": (80, 100),
            "color": "#A6D89B",  # GREEN — HBM dominant
            "memory_type": "HBM dominant",
            "memory_detail": "(R4/C5, R5/C4-C5)",
            "moat": "Strongest moat",
            "moat_detail": "Sold-out HBM peak — ~60% share, Samsung gaining",
        },
        {
            "score_range": (65, 80),
            "color": "#C9E5BC",  # light green — HBM-leaning
            "memory_type": "HBM-leaning",
            "memory_detail": "(R3/C5, R4/C4, R5/C3)",
            "moat": "Strong moat",
            "moat_detail": "HBM majority — Samsung + Micron contest at margin",
        },
        {
            "score_range": (50, 65),
            "color": "#FCE38A",  # yellow — transition (HBM/GDDR cluster lives here)
            "memory_type": "HBM-GDDR / hybrid",
            "memory_detail": "(R1/C5, R3/C3-C4, R4/C2-C3, R5/C1-C2)",
            "moat": "Partial moat",
            "moat_detail": "Duopoly with Samsung — architecture transition zone",
        },
        {
            "score_range": (30, 50),
            "color": "#F8A55C",  # amber — LPDDR-led
            "memory_type": "LPDDR-led",
            "memory_detail": "(R1/C3-C4, R2/C2-C4, R3/C1-C2, R4/C1)",
            "moat": "Weak moat",
            "moat_detail": "Samsung-advantaged on LPDDR",
        },
        {
            "score_range": (0, 30),
            "color": "#E0775F",  # RED — DDR commodity
            "memory_type": "DDR commodity",
            "memory_detail": "(R1/C1-C2, R2/C1)",
            "moat": "No moat",
            "moat_detail": "Compete on margin — Samsung / Micron / CXMT",
        },
    ]

    BAR_TOTAL_HEIGHT_PX = 360
    BAR_COL_WIDTH_PX = 150  # widened from 90 to fit "TODAY X/100" text on left
    # HONEST MATH: marker at exact score position, not segment midpoint
    marker_from_top_pct = 100 - score

    parts = []
    parts.append(
        "<div style='margin:18px 0;padding:14px;background:#FFFFFF;"
        "border:1px solid #DDD;border-radius:4px'>"
    )
    # Title — include market_zone hint if provided
    market_hint = (
        f", market cell: {html.escape(market_zone)}" if market_zone else ""
    )
    parts.append(
        "<div style='font-weight:600;color:#1a1a1a;font-size:13px;margin-bottom:14px'>"
        "SK HYNIX HBM CAPTIVE-RATIO SPECTRUM"
        "<span style='color:#666;font-weight:400;font-size:11px;margin-left:8px'>"
        f"— Today's score {score}/100{market_hint}"
        "</span></div>"
    )

    # HEADER ROW above the bar — column labels above the 100 mark, not next
    # to the top segment.
    parts.append(
        f"<table style='width:100%;border-collapse:collapse;margin-bottom:6px'><tr>"
        f"<td style='width:{BAR_COL_WIDTH_PX}px'></td>"
        "<td style='padding:2px 0 2px 14px;font-weight:600;font-size:12px;color:#1a1a1a;"
        "border-left:3px solid #555'>Memory type</td>"
        "<td style='padding:2px 0 2px 14px;font-weight:600;font-size:12px;color:#1a1a1a;"
        "border-left:3px solid #555'>SK Hynix moat</td>"
        "</tr></table>"
    )

    # Outer table: 3 cells — bar | memory type | moat
    parts.append("<table style='width:100%;border-collapse:collapse'><tr>")

    # Cell 1: the bar itself + score axis labels on left + marker
    parts.append(
        f"<td style='vertical-align:top;width:{BAR_COL_WIDTH_PX}px;position:relative'>"
        f"<div style='position:relative;height:{BAR_TOTAL_HEIGHT_PX}px;width:60px;"
        f"margin-left:{BAR_COL_WIDTH_PX - 60 - 8}px;border:1px solid #555'>"
    )
    # Stack segments inside the bar (top-to-bottom)
    for seg in segments:
        lo, hi = seg["score_range"]
        height_pct = hi - lo
        parts.append(
            f"<div style='height:{height_pct}%;background:{seg['color']};"
            f"border-bottom:1px solid #555;position:relative'>"
            f"<span style='position:absolute;left:-32px;top:0;font-size:10px;"
            f"color:#555;font-variant-numeric:tabular-nums'>{hi}</span>"
            f"</div>"
        )
    # Bottom-axis "0"
    parts.append(
        "<span style='position:absolute;left:-32px;bottom:-7px;font-size:10px;"
        "color:#555;font-variant-numeric:tabular-nums'>0</span>"
    )

    # SKETCH-STYLE MARKER per Raul's drawing — black triangle pointing
    # INTO the bar from the left, with plain "TODAY X/100" text label to
    # the left of the triangle. No callout box, no halo, no connector.
    # Marker sits at the exact numeric score position on the axis.
    parts.append(
        f"<div style='position:absolute;top:{marker_from_top_pct}%;left:0;"
        f"transform:translateY(-50%);width:0;height:0;overflow:visible'>"
        # Text label (LEFT of the marker)
        f"<div style='position:absolute;top:50%;right:8px;transform:translateY(-50%);"
        f"font-size:12px;color:#1a1a1a;font-weight:700;line-height:1.15;"
        f"text-align:right;white-space:nowrap'>"
        f"TODAY<br/><span style='font-size:11px'>{score}/100</span>"
        f"</div>"
        # Black triangle pointing INTO the bar (CSS triangle via borders)
        f"<div style='position:absolute;top:50%;left:-9px;transform:translateY(-50%);"
        f"width:0;height:0;"
        f"border-top:7px solid transparent;border-bottom:7px solid transparent;"
        f"border-left:10px solid #1a1a1a'></div>"
        f"</div>"
    )
    parts.append("</div></td>")

    # Cell 2: Memory type column (labels aligned to segment midpoints; no
    # inner header — header is in the row above the bar now)
    parts.append(
        "<td style='vertical-align:top;padding-left:14px'>"
        f"<div style='position:relative;height:{BAR_TOTAL_HEIGHT_PX}px'>"
    )
    for seg in segments:
        lo, hi = seg["score_range"]
        midpoint_from_top_pct = 100 - (lo + hi) / 2
        parts.append(
            f"<div style='position:absolute;top:{midpoint_from_top_pct}%;left:0;"
            f"transform:translateY(-50%);font-size:11px;color:#333;line-height:1.35'>"
            f"<strong>{html.escape(seg['memory_type'])}</strong> "
            f"<span style='color:#777;font-size:10px'>{html.escape(seg['memory_detail'])}</span>"
            f"</div>"
        )
    parts.append("</div></td>")

    # Cell 3: SK Hynix moat column (no inner header)
    parts.append(
        "<td style='vertical-align:top;padding-left:14px'>"
        f"<div style='position:relative;height:{BAR_TOTAL_HEIGHT_PX}px'>"
    )
    for seg in segments:
        lo, hi = seg["score_range"]
        midpoint_from_top_pct = 100 - (lo + hi) / 2
        parts.append(
            f"<div style='position:absolute;top:{midpoint_from_top_pct}%;left:0;"
            f"transform:translateY(-50%);font-size:11px;color:#333;line-height:1.35'>"
            f"<strong>{html.escape(seg['moat'])}</strong>"
            f"<div style='color:#777;font-size:10px;line-height:1.3'>"
            f"{html.escape(seg['moat_detail'])}</div>"
            f"</div>"
        )
    parts.append("</div></td>")

    parts.append("</tr></table>")
    parts.append("</div>")
    return "\n".join(parts)


def _build_section4_card_or_placeholder(
    snapshot: Optional[dict],
    indicator_number: int,
    indicator_title: str,
    error_message: Optional[str],
    normal_card_builder,
) -> str:
    """v3.7.3 — Always emit a section-4 deep-dive card.

    Before v3.7.3, section 4 contained three `if snapshot: append(builder())`
    guards. When any of the three snapshots was None due to a computation
    failure (e.g., the 2026-05-25 NameError on a stale `build_narrative_summary`
    call that crashed DHCR compute), the corresponding card silently vanished
    from the brief. The brief jumped from indicator 5 to indicator 7 with
    no error visible to the operator — the failure only surfaced in the
    GitHub Actions workflow log.

    This wrapper makes the section-4 cards self-diagnosing. The card slot
    is ALWAYS present. If the snapshot is present, the normal card builder
    renders. If not, a placeholder card renders with:
      - The same header structure as the normal cards (h3 + status badge)
      - A red "Computation failed" status badge
      - Body text naming the actual error message from fast_tier's except
        handler (NameError, FileNotFoundError, etc.)
      - "Indicator N of 8" framing so the operator sees that the card is
        intended to be here and section 4 numbering is preserved

    Args:
        snapshot: The computed snapshot dict (DHCR / Competitor / ADR), or
            None if compute failed upstream.
        indicator_number: 6 / 7 / 8 — used for the placeholder's "N of 8" prose.
        indicator_title: Display name, e.g., "Decode HBM Captive Ratio (DHCR)".
        error_message: The actual exception string captured by fast_tier's
            try/except for this indicator. None means we don't know why the
            snapshot is missing (defensive fallback: show generic message).
        normal_card_builder: Callable that takes the snapshot and returns the
            normal card HTML. Only invoked if snapshot is truthy.

    Returns:
        HTML string for either the normal card or the placeholder.
    """
    if snapshot:
        # Defensive: even if snapshot is present, the normal builder might
        # raise on a corrupt field. Wrap so a builder bug doesn't crash the
        # whole email render — instead the placeholder fires with the
        # builder's own traceback as error context.
        try:
            rendered = normal_card_builder(snapshot)
            if rendered:    # builders return "" for falsy snapshots historically
                return rendered
            # Builder returned empty — treat as failure so placeholder fires
            internal_error = (
                f"_build_section4_card_or_placeholder: normal card builder "
                f"for Indicator {indicator_number} returned empty string "
                f"despite snapshot being present"
            )
        except Exception as e:
            internal_error = (
                f"Normal card builder for Indicator {indicator_number} "
                f"raised {type(e).__name__}: {e}"
            )
        # Fall through to placeholder with internal error message
        effective_error = error_message or internal_error
    else:
        effective_error = error_message or (
            f"Snapshot is None and no specific error message was captured "
            f"(check fast_tier workflow log for the underlying exception)"
        )

    # Render the placeholder
    parts = []
    parts.append(
        f"<h3>{indicator_title} "
        f"<span class='status-badge' style='background-color: #dc3545; "
        f"color: #ffffff; padding: 2px 8px; border-radius: 4px; "
        f"font-size: 0.85em; font-weight: 600;'>Computation failed</span>"
        f"</h3>"
    )
    parts.append(
        f"<p><strong>What we're looking for:</strong> Indicator "
        f"{indicator_number} of 8 — {indicator_title}. The full deep-dive "
        f"card normally renders here. Section 4 cards 6, 7, and 8 are the "
        f"three structural indicators (DHCR / Competitor / ADR); they "
        f"render in order regardless of compute success, so an operator "
        f"can immediately see when one is broken.</p>"
    )
    parts.append(
        f"<p><strong>Where we stand:</strong> The compute step for this "
        f"indicator raised an exception during today's fast-tier run. "
        f"The snapshot could not be produced, so the normal card content "
        f"is unavailable. Other indicators on the brief continue to "
        f"render normally — this is a card-level failure, not a brief-"
        f"wide one.</p>"
    )
    # Escape the error message for HTML safety
    safe_error = (
        (effective_error or "(no error message)")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    parts.append(
        f"<p><strong>Underlying error:</strong> "
        f"<code style='background:#f4f4f4; padding:2px 6px; "
        f"border-radius:3px; font-family:monospace; font-size:0.9em;'>"
        f"{safe_error}</code></p>"
    )
    parts.append(
        f"<p><strong>Next step:</strong> Inspect the GitHub Actions workflow "
        f"log for the most recent fast-tier run and locate the "
        f"<code>{_indicator_log_keyword(indicator_number)}</code> log line. "
        f"The traceback there pinpoints the source file and line. Cards "
        f"6/7/8 are version-locked; this placeholder is a deliberate signal, "
        f"not the brief failing gracefully into nothing.</p>"
    )
    return "".join(parts)


def _indicator_log_keyword(indicator_number: int) -> str:
    """Map section-4 indicator number to the substring an operator would
    grep for in the workflow log."""
    return {
        6: "DHCR computation failed",
        7: "Indicator 7 computation failed",
        8: "Indicator 8 computation failed",
    }.get(indicator_number, "computation failed")


def _build_dhcr_section4_card(
    dhcr: Optional[dict],
    matrix_chart_b64: Optional[str] = None,
    trajectory_chart_b64: Optional[str] = None,
) -> str:
    """v3.4.9 — DHCR section 4 deep-dive card.

    REBUILT to match the visual structure of cards 1-5:
      - Header: title + status pill (no "v3.4.2 framework" prefix label)
      - "What we're looking for:" — DHCR framework explanation in prose
      - "Where we stand:" — today's numbers + where-we-came-from prose
        (replaces the trajectory arrow on the matrix chart per user direction
        2026-05-23: history is verbalized in prose, not overlaid visually)
      - "Latest developments:" — substitution risk notes + sanity signal notes
      - Source links — links to DHCR data sources
      - Two-column chart row at bottom: matrix (left) + vertical spectrum
        bar (right) at matching heights. The spectrum bar (the existing
        _build_dhcr_spectrum_bar HTML/CSS) shows the 0-100 score range
        with memory type AND SK Hynix moat strength annotated per band,
        with a marker arrow at today's score. This matches Raul's
        original sketch from 2026-05-22 — single bar with score on the
        axis and BOTH memory-type and moat-strength annotations linked
        to position on the bar.

    Previous v3.4.4/v3.4.2 layout (calibration banner, sub-tables, sanity
    box, narrative box, bull/bear context boxes) is dropped in favor of
    the cards-1-5 structure. All quantitative substance preserved — moved
    into the "Where we stand" prose.
    """
    if not dhcr:
        return ""

    status = dhcr.get("status", "Not fired")
    status_color = dhcr.get("status_color", "#C0DD97")
    text_color = "#FFFFFF" if status in ("Fired", "Partially fired") else "#1a1a1a"

    # Header (mirror cards 1-5: h3 + status badge inline)
    parts = []
    parts.append(
        f"<h3>Decode HBM Captive Ratio (DHCR) "
        f"<span class='status-badge' "
        f"style='background:{status_color};color:{text_color};margin-left:8px'>"
        f"{html.escape(status)}</span></h3>"
    )

    # "What we're looking for" — the framework definition
    parts.append(
        "<p style='font-size:13px'><strong>What we're looking for:</strong> "
        "Position on a 5×5 matrix of KV efficiency (y-axis, R1 most efficient "
        "to R5 least efficient) crossed with operative context length (x-axis, "
        "C1 &lt;32K to C5 &gt;10M tokens). The matrix is colored by memory-zone "
        "intensity (DDR/commodity → LPDDR hybrid → HBM-dominant). Today's "
        "market-deployed cell yields a 0–100 score; higher = more HBM-intensive "
        "compute regime, which lengthens SK Hynix's moat in the HBM oligopoly. "
        "Direction matters more than level: an improving trajectory toward "
        "lower-row / higher-column cells signals enterprise inference workloads "
        "pulling architecture toward higher HBM intensity."
        "</p>"
    )

    # "Where we stand" — today's numbers + history prose (no chart overlays)
    parts.append(_build_dhcr_where_we_stand_prose(dhcr))

    # v3.4.14 — pending volume-share proposal (amber box if awaiting confirmation)
    proposal_html = _build_context_volume_proposal_diagnostic(dhcr)
    if proposal_html:
        parts.append(proposal_html)

    # "Latest developments" — substitution risk + sanity signal observations
    parts.append(_build_dhcr_latest_developments_prose(dhcr))

    # Source links (cards-1-5 style: inline "· " separated)
    parts.append(_build_dhcr_source_links(dhcr))

    # Two-column chart row: matrix (left) + vertical spectrum bar (right).
    # The spectrum bar is the v3.4.9.2 _build_dhcr_spectrum_bar HTML/CSS
    # rendering — vertical 0-100 bar with score-based color gradient,
    # marker at exact score position, market cell label in subtitle.
    if matrix_chart_b64:
        score = dhcr.get("position_score", 0)
        # Resolve the market-deployed cell's memory-zone label for the
        # spectrum-bar subtitle (e.g. "HBM/GDDR"). The matrix cell label
        # comes from the canonical MEMORY_ZONES grid in chart_generator.
        market_zone = _dhcr_market_zone_label(dhcr)
        parts.append(_build_dhcr_chart_row(matrix_chart_b64, score, market_zone))

    return "".join(parts)


def _dhcr_market_zone_label(dhcr: dict) -> str:
    """v3.4.9.2 — resolve the matrix cell zone label (e.g. 'HBM/GDDR')
    for the market-deployed cell, used as a subtitle on the spectrum bar.

    Mirrors the MEMORY_ZONES grid in src/analysis/chart_generator.py so
    the spectrum bar's market-cell label always matches what the matrix
    displays. The grid is duplicated here (small, stable) rather than
    imported to avoid a circular template→chart_generator dependency.
    """
    MEMORY_ZONES = [
        ["DDR",      "DDR/LPDDR", "LPDDR",    "LPDDR/HBM",  "LPDDR/HBM"],   # R1
        ["DDR/CXL",  "DDR/LPDDR", "LPDDR",    "LPDDR/HBM",  "HBM/hetero"],  # R2
        ["LPDDR",    "LPDDR/HBM", "HBM/GDDR", "HBM/hetero", "HBM"],         # R3
        ["HBM/GDDR", "HBM/GDDR",  "HBM",      "HBM",        "HBM"],         # R4
        ["HBM",      "HBM",       "HBM",      "HBM",        "HBM"],         # R5
    ]
    cell = dhcr.get("market_deployed_cell")
    if not cell or not isinstance(cell, (list, tuple)) or len(cell) != 2:
        return ""
    r, c = int(cell[0]), int(cell[1])
    if 0 <= r < 5 and 0 <= c < 5:
        return MEMORY_ZONES[r][c]
    return ""


def _build_dhcr_where_we_stand_prose(dhcr: dict) -> str:
    """v3.4.9 — Verbalize today's DHCR numbers and the 'where we came from'
    trajectory. Replaces the v3.4.2 chart-overlay arrow."""
    score = dhcr.get("position_score", 0)
    market_cell = dhcr.get("market_deployed_cell", (0, 0))
    market_zone = dhcr.get("market_deployed_zone", "?")
    workload_cell = dhcr.get("workload_implied_cell", (0, 0))
    workload_zone = dhcr.get("workload_implied_zone", "?")
    anticipation_gap = dhcr.get("anticipation_gap_bands", 0)
    kv_gb = dhcr.get("frontier_kv_gb_per_1m_tokens", 0)
    model = dhcr.get("frontier_model_name", "?")
    op_ctx = dhcr.get("operative_context_tokens", 0)
    op_ctx_s = f"{op_ctx/1000:.0f}K" if op_ctx < 1_000_000 else f"{op_ctx/1_000_000:.2f}M"
    ent_rr = dhcr.get("enterprise_ai_run_rate_usd_b", 0)
    ent_yoy = dhcr.get("enterprise_ai_yoy_growth_pct") or 0
    hbm_demand = dhcr.get("hbm_bit_demand_index", 0)
    reasoning = dhcr.get("status_reasoning", "")
    narrative = dhcr.get("narrative_summary", "")

    # "Where we came from" — synthesize trajectory from prior_score field if present
    # The trajectory list is on the snapshot; we read prior scores to verbalize history
    trajectory = dhcr.get("trajectory", []) or []
    history_phrase = _verbalize_dhcr_history(score, trajectory)

    # COMPUTED METRICS block (matches the cards-1-5 pattern of putting hard
    # numbers up front in "Where we stand")
    sentences = []
    sentences.append(
        f"<strong>COMPUTED METRICS:</strong> Position score "
        f"<strong>{score}/100</strong>, status: {html.escape(status_label(dhcr))}. "
        f"Today's market-deployed cell: R{market_cell[0]+1}/C{market_cell[1]+1} "
        f"({html.escape(market_zone)}). Frontier KV efficiency: "
        f"<strong>{kv_gb:.1f} GB per 1M tokens</strong> ({html.escape(model)}). "
        f"Operative context length: <strong>~{op_ctx_s} tokens</strong> "
        f"(volume-weighted across enterprise inference workloads)."
    )
    sentences.append(
        f"Anticipation gap: <strong>+{anticipation_gap} Y-band(s)</strong> — "
        f"market provisions HBM-heavy infrastructure ahead of literal workload "
        f"demand (workload-implied cell: R{workload_cell[0]+1}/C{workload_cell[1]+1}, "
        f"{html.escape(workload_zone)})."
    )
    sentences.append(
        f"Volume layer: enterprise AI run-rate <strong>${ent_rr:.0f}B</strong> "
        f"annualized ({ent_yoy:+.0f}% YoY); composite HBM bit demand index "
        f"<strong>{hbm_demand:.0f}</strong> (score × volume / 100)."
    )
    if history_phrase:
        sentences.append(history_phrase)

    # v3.4.14 — operative context trend from context_length_state.json history
    ctx_trend = _build_operative_context_trend(dhcr)
    if ctx_trend:
        sentences.append(ctx_trend)

    if reasoning:
        sentences.append(html.escape(reasoning))
    if narrative:
        sentences.append(html.escape(narrative))

    body = " ".join(sentences)
    return f"<p style='font-size:13px'><strong>Where we stand:</strong> {body}</p>"


def _verbalize_dhcr_history(today_score: float, trajectory: list) -> str:
    """v3.4.9 — Turn the trajectory list into prose so we don't need the
    blue arrow on the matrix. Returns an empty string if no usable history."""
    if not trajectory or len(trajectory) < 1:
        return ""
    # trajectory entries are dicts ordered oldest → newest, each with a
    # `position_score` field (and a `date` field if available)
    prior_scores = [t.get("position_score") for t in trajectory if t.get("position_score") is not None]
    if not prior_scores:
        return ""
    oldest = prior_scores[0]
    delta = today_score - oldest
    if abs(delta) < 2:
        direction_word = "broadly flat"
    elif delta > 0:
        direction_word = f"up {delta:+.0f} points"
    else:
        direction_word = f"down {delta:+.0f} points"
    n_points = len(prior_scores)
    return (
        f"<strong>Where we came from:</strong> trajectory is {direction_word} "
        f"over the last {n_points} reading(s) — prior score was {oldest:.0f}/100, "
        f"today {today_score:.0f}/100."
    )


def _build_operative_context_trend(dhcr: dict) -> str:
    """v3.4.14 — Produce a 1-2 sentence trend phrase for operative context.

    Uses operative_context_history from context_length_state.json (attached
    to dhcr_dict in fast_tier.py).  Returns empty string if fewer than
    2 historical data points exist (nothing to compare against).

    Raul's direction: render in the 'Where we stand' text section —
    'over the last quarter it has moved...' — no trend line on the matrix.
    """
    history = dhcr.get("operative_context_history") or []
    current_tokens = dhcr.get("operative_context_tokens", 0)
    current_band = dhcr.get("context_band_name", "?")

    if len(history) < 2 or not current_tokens:
        return ""

    # Find the entry closest to ~90 days ago for the "last quarter" comparison
    from datetime import date as _date, timedelta as _td
    import datetime as _dt
    try:
        target_date = _date.today() - _td(days=90)
        # history entries are dicts with 'date' (YYYY-MM-DD) and 'operative_context_tokens'
        parsed = []
        for h in history:
            try:
                d = _dt.date.fromisoformat(h["date"])
                v = int(h.get("operative_context_tokens", 0))
                if v > 0:
                    parsed.append((d, v, h.get("band", "?")))
            except Exception:
                continue
        if not parsed:
            return ""
        parsed.sort(key=lambda t: t[0])

        # Pick the point closest to 90 days ago, at least 14 days back
        min_days_back = 14
        today = _date.today()
        candidates = [(d, v, b) for d, v, b in parsed
                      if (today - d).days >= min_days_back]
        if not candidates:
            return ""

        # Closest to 90 days ago
        ref_date, ref_tokens, ref_band = min(
            candidates, key=lambda t: abs((t[0] - target_date).days)
        )
        delta_tokens = current_tokens - ref_tokens
        delta_pct = delta_tokens / ref_tokens * 100 if ref_tokens else 0
        days_back = (today - ref_date).days

        if abs(delta_pct) < 2:
            trend_phrase = "broadly flat"
        elif delta_pct > 0:
            trend_phrase = f"up <strong>{delta_pct:+.0f}%</strong>"
        else:
            trend_phrase = f"down <strong>{delta_pct:+.0f}%</strong>"

        ref_str = f"{ref_tokens/1000:.0f}K" if ref_tokens < 1_000_000 else f"{ref_tokens/1_000_000:.2f}M"
        cur_str = f"{current_tokens/1000:.0f}K" if current_tokens < 1_000_000 else f"{current_tokens/1_000_000:.2f}M"
        period_label = "quarter" if days_back >= 60 else f"{days_back}-day period"

        # v3.5.1 — derive the next-band watch threshold from current_band
        # rather than hardcoding "C2 to C3". Renders correct prose whether
        # the matrix is currently in C1, C2, C3, etc.
        _band_to_next = {
            "C1": ("C2", "32K"),
            "C2": ("C3", "200K"),
            "C3": ("C4", "1M"),
            "C4": ("C5", "10M"),
            "C5": (None, None),
        }
        _next_band, _next_threshold = _band_to_next.get(current_band, (None, None))
        if _next_band:
            watch_clause = (
                f" Enterprise volume share and context window expansion are the "
                f"primary drivers to watch — when operative context crosses "
                f"~{_next_threshold} the X-axis shifts from {current_band} to {_next_band}."
            )
        else:
            watch_clause = (
                " Operative context is already in the top band (C5); further "
                "expansion no longer shifts the X-axis."
            )

        return (
            f"<strong>Operative context trend:</strong> over the last {period_label}, "
            f"operative context has moved {trend_phrase} "
            f"({html.escape(ref_str)} {html.escape(ref_band)} → "
            f"{html.escape(cur_str)} {html.escape(current_band)})."
            + watch_clause
        )
    except Exception:
        return ""


def _build_context_volume_proposal_diagnostic(dhcr: dict) -> str:
    """v3.6.0 — Deprecated stub.

    The volume-share proposal mechanism was removed in v3.6.0 when the LLM
    research worker replaced the daily aggregator. The worker commits
    values directly after sanity checks (no proposal step). This stub is
    retained so the existing call site in _build_dhcr_section_4_card
    keeps working without an explicit code change.
    """
    return ""


def _build_dhcr_latest_developments_prose(dhcr: dict) -> str:
    """v3.4.9 — Convert substitution risk notes + sanity signals into a
    bulleted-prose Latest developments block matching cards 1-5."""
    subst = dhcr.get("substitution_risk_notes", []) or []
    sanity_confirms = dhcr.get("sanity_check_confirms", 0)
    sanity_contradicts = dhcr.get("sanity_check_contradicts", 0)
    subst_count = dhcr.get("sanity_signal_substitution_news_count", 0)
    decode_off_hbm = dhcr.get("sanity_signal_hyperscaler_decode_mentions", 0)
    hbm_price = dhcr.get("sanity_signal_hbm_price_trend", "?")
    calibration_flag = dhcr.get("calibration_flag", False)

    bullets = []
    bullets.append(
        f"Sanity check: {sanity_confirms}/3 signals confirm matrix output, "
        f"{sanity_contradicts}/3 contradict"
        + (" — <strong>CALIBRATION FLAG ACTIVE</strong>" if calibration_flag else "")
        + "."
    )
    bullets.append(
        f"HBM-substitution news (last 90d): {subst_count} items "
        f"(threshold for concern: 5+); hyperscaler decode-off-HBM mentions: "
        f"{decode_off_hbm}; HBM spot/contract price trend: "
        f"<strong>{html.escape(hbm_price)}</strong>."
    )
    if subst:
        # Show up to 3 substitution risk observations as continuation
        bullets.append(
            "Substitution-risk observations: "
            + "; ".join(html.escape(s) for s in subst[:3])
            + "."
        )

    body = " ".join(bullets)
    return f"<p style='font-size:13px'><strong>Latest developments:</strong> {body}</p>"


def _build_dhcr_source_links(dhcr: dict) -> str:
    """v3.4.9 — DHCR source links in the same '· ' inline format as cards 1-5."""
    # DHCR sources are stable per the framework — list the canonical primaries
    links = [
        ("Anthropic engineering — context window roadmap", "https://www.anthropic.com/engineering"),
        ("Meta AI — Llama architecture papers", "https://ai.meta.com/research/"),
        ("DeepMind — Gemini model cards", "https://deepmind.google/technologies/gemini/"),
        ("MSFT Azure AI revenue — earnings transcripts", "https://www.microsoft.com/en-us/investor/"),
        ("NVIDIA datacenter revenue", "https://investor.nvidia.com/"),
    ]
    link_html = " · ".join(
        f"<a href='{html.escape(url)}' target='_blank'>{html.escape(title[:80])}</a>"
        for title, url in links
    )
    return f"<p class='links-list' style='font-size:12px'>{link_html}</p>"


def _build_dhcr_chart_row(
    matrix_chart_b64: Optional[str],
    score: int,
    market_zone: str = "",
) -> str:
    """v3.4.9.2 — Two-column chart layout per Raul's original sketch.
    Matrix (left) + vertical spectrum bar (right) at matching heights.

    The right column uses `_build_dhcr_spectrum_bar` (v3.4.9.2):
    a vertical 0-100 bar with 5 score-based color-coded segments
    (green at top for HBM dominance, red at bottom for DDR commodity),
    annotated with memory type per band AND SK Hynix moat strength per
    band, both linked to the same 0-100 score axis. Today's score is
    marked with a black triangle + plain "TODAY X/100" text label at
    the exact score position (no zone-midpoint fudging).

    Parameters
    ----------
    matrix_chart_b64
        Base64-encoded matrix PNG (renders left column).
    score
        Today's DHCR position_score, 0-100.
    market_zone
        Matrix cell zone label for the market-deployed cell (e.g.
        "HBM/GDDR"). Shown in the spectrum bar's subtitle so the bar
        and matrix agree on the market position.
    """
    cells = []
    if matrix_chart_b64:
        cells.append(
            "<td style='width:50%;vertical-align:middle;padding-right:10px'>"
            f"<img src='data:image/png;base64,{matrix_chart_b64}' "
            f"style='width:100%;display:block' "
            f"alt='DHCR 5x5 matrix — KV efficiency vs operative context' />"
            "</td>"
        )
    # Right column: the v3.4.9.2 vertical spectrum bar
    cells.append(
        "<td style='width:50%;vertical-align:middle;padding-left:10px'>"
        f"{_build_dhcr_spectrum_bar(score, market_zone)}"
        "</td>"
    )
    if not cells:
        return ""
    return (
        "<table style='width:100%;border-collapse:collapse;margin:14px 0'>"
        f"<tr>{''.join(cells)}</tr></table>"
    )


def status_label(snap: dict) -> str:
    """Helper to get a clean status string for the DHCR snapshot."""
    return snap.get("status", "Not fired")


def _build_dhcr_section4_card_legacy(
    dhcr: Optional[dict],
    matrix_chart_b64: Optional[str] = None,
    trajectory_chart_b64: Optional[str] = None,
) -> str:
    """v3.4.2 — DHCR section 4 deep-dive card with matrix + bull/bear context.

    Renders the full DHCR snapshot per the May 22 framework:
      - Status pill (Fired / Partially fired / Not fired)
      - Score badge (0-100 with HBM↔commodity spectrum)
      - Two-column workload-vs-market cell readout (anticipation gap)
      - Matrix chart embedded (5x5 with memory-zone color coding)
      - Per-category context contribution table (the dual-input X-axis)
      - Volume index + composite HBM bit demand
      - Sanity check status with calibration flag
      - Sonnet's narrative paragraph
      - Bull case + Bear case context boxes (NEW v3.4.2 per Raul's ask)
      - Mental model footer + framework explanation
    """
    if not dhcr:
        return ""

    status = dhcr.get("status", "Not fired")
    status_color = dhcr.get("status_color", "#C0DD97")
    score = dhcr.get("position_score", 0)
    workload_zone = dhcr.get("workload_implied_zone", "?")
    market_zone = dhcr.get("market_deployed_zone", "?")
    workload_cell = dhcr.get("workload_implied_cell", (0, 0))
    market_cell = dhcr.get("market_deployed_cell", (0, 0))
    anticipation_gap = dhcr.get("anticipation_gap_bands", 0)

    kv_kb = dhcr.get("frontier_kv_kb_per_token", 0)
    kv_gb = dhcr.get("frontier_kv_gb_per_1m_tokens", 0)
    model = dhcr.get("frontier_model_name", "?")
    arch = dhcr.get("frontier_model_arch", "?")
    kv_band = dhcr.get("kv_band_label", "?")
    kv_yoy = dhcr.get("kv_yoy_change_pct") or 0

    op_ctx = dhcr.get("operative_context_tokens", 0)
    op_ctx_s = f"{op_ctx/1000:.0f}K" if op_ctx < 1_000_000 else f"{op_ctx/1_000_000:.2f}M"
    ctx_band = dhcr.get("context_band_label", "?")
    categories = dhcr.get("context_categories", []) or []

    ent_rr = dhcr.get("enterprise_ai_run_rate_usd_b", 0)
    ent_yoy = dhcr.get("enterprise_ai_yoy_growth_pct") or 0
    nv_yoy = dhcr.get("nvidia_datacenter_yoy_growth_pct") or 0
    vol_idx = dhcr.get("volume_index", 100)
    vol_traj = dhcr.get("volume_trajectory", "flat")
    hbm_demand = dhcr.get("hbm_bit_demand_index", 0)

    sanity_confirms = dhcr.get("sanity_check_confirms", 0)
    sanity_contradicts = dhcr.get("sanity_check_contradicts", 0)
    calibration_flag = dhcr.get("calibration_flag", False)
    subst_count = dhcr.get("sanity_signal_substitution_news_count", 0)
    decode_off_hbm = dhcr.get("sanity_signal_hyperscaler_decode_mentions", 0)
    hbm_price = dhcr.get("sanity_signal_hbm_price_trend", "?")

    narrative = dhcr.get("narrative_summary", "")
    bull_case = dhcr.get("bull_case_summary", "")
    bear_case = dhcr.get("bear_case_summary", "")
    subst = dhcr.get("substitution_risk_notes", []) or []
    reasoning = dhcr.get("status_reasoning", "")

    text_color = "#FFFFFF" if status == "Fired" else "#1a1a1a"

    parts = []
    parts.append(
        "<div style='margin:24px 0;border:2px solid #555;background:#FAFAFA;"
        "padding:18px;border-radius:5px'>"
    )

    # Header: title + status pill + score badge
    parts.append(
        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap'>"
        f"<h3 style='margin:0;color:#1a1a1a;display:inline'>Decode HBM Captive Ratio (DHCR)</h3>"
        f"<span style='background:{status_color};color:{text_color};padding:6px 14px;"
        f"border-radius:4px;font-weight:700;font-size:13px'>{html.escape(status)}</span>"
        f"<span style='background:#2C5AA0;color:#fff;padding:6px 14px;border-radius:4px;"
        f"font-weight:700;font-size:13px'>SCORE {score}/100</span>"
        f"<span style='font-size:12px;color:#666'>"
        f"v3.4.2 framework — direction monitor</span>"
        f"</div>"
    )

    if calibration_flag:
        parts.append(
            "<div style='background:#FAC775;border:2px solid #B58A1A;color:#5A3F00;"
            "padding:8px 12px;margin-bottom:12px;border-radius:3px;font-weight:600;font-size:12px'>"
            "⚠️ CALIBRATION FLAG — ≥2 of 3 sanity signals contradict the matrix output. "
            "Review substitution evidence before relying on this score."
            "</div>"
        )

    # Reasoning under status
    parts.append(
        f"<div style='font-size:12px;color:#444;margin-bottom:14px;font-style:italic'>"
        f"{html.escape(reasoning)}</div>"
    )

    # MATRIX CHART (the centerpiece)
    if matrix_chart_b64:
        parts.append(
            f"<img src='data:image/png;base64,{matrix_chart_b64}' "
            f"style='width:100%;max-width:760px;margin:8px 0' "
            f"alt='DHCR 5x5 matrix — KV efficiency vs operative context, color-coded by memory zone' />"
        )

    # v3.4.4 — SPECTRUM BAR (per Raul's sketch May 22 2026).
    # Vertical bar from 100 (top) to 0 (bottom) with 5 stacked color-coded
    # segments mapping score ranges to memory architecture AND SK Hynix
    # moat-strength. Today's score marked with a left-pointing arrow at the
    # proportional position. Renders as inline HTML/CSS (no image) for
    # responsive scaling in email clients.
    parts.append(_build_dhcr_spectrum_bar(score))

    # Two-column: workload-implied vs market-deployed
    parts.append("<table style='width:100%;border-collapse:collapse;margin:14px 0'><tr>")
    parts.append(
        "<td style='width:50%;vertical-align:top;padding-right:14px;border-right:1px solid #DDD'>"
        "<div style='font-weight:600;color:#1a1a1a;margin-bottom:6px;font-size:13px'>"
        "WORKLOAD-IMPLIED CELL</div>"
        f"<div style='font-size:12px;color:#333;line-height:1.55'>"
        f"<strong>Cell:</strong> R{workload_cell[0]+1}C{workload_cell[1]+1}"
        f" ({html.escape(workload_zone)})<br/>"
        f"<strong>Frontier KV:</strong> {kv_kb:.1f} KB/tok = {kv_gb:.1f} GB per 1M tokens<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"{html.escape(model)} ({html.escape(arch)}), band {html.escape(kv_band)}</span><br/>"
        f"&nbsp;&nbsp;YoY: {kv_yoy:+.0f}% (negative = improving)<br/>"
        f"<strong>Operative context:</strong> ~{op_ctx_s} tokens (band {html.escape(ctx_band)})<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"volume-weighted across {len(categories)} categories</span>"
        "</div></td>"
    )
    parts.append(
        "<td style='width:50%;vertical-align:top;padding-left:14px'>"
        "<div style='font-weight:600;color:#1a1a1a;margin-bottom:6px;font-size:13px'>"
        "MARKET-DEPLOYED CELL <span style='color:#666;font-size:11px;font-weight:400'>"
        "(workload + anticipation premium)</span></div>"
        f"<div style='font-size:12px;color:#333;line-height:1.55'>"
        f"<strong>Cell:</strong> R{market_cell[0]+1}C{market_cell[1]+1}"
        f" ({html.escape(market_zone)})<br/>"
        f"<strong>Anticipation gap:</strong> +{anticipation_gap} Y-band(s)<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"market provisions HBM-heavy infra ahead of literal workload demand</span><br/>"
        f"<strong>Position score:</strong> "
        f"<span style='font-size:18px;font-weight:700;color:#0F6E56'>{score}/100</span><br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"50/50 weighted (KV intensity × context intensity)</span>"
        "</div></td>"
    )
    parts.append("</tr></table>")

    # Per-category context breakdown (the dual-input X-axis)
    if categories:
        parts.append(
            "<div style='margin:14px 0;font-size:12px;color:#333'>"
            "<strong style='color:#1a1a1a'>X-axis triangulation: per-category contribution "
            "to operative context</strong><br/>"
            "<table style='width:100%;border-collapse:collapse;margin-top:6px;font-size:11px'>"
            "<thead><tr style='background:#EEE'>"
            "<th style='text-align:left;padding:4px 8px'>Category</th>"
            "<th style='text-align:right;padding:4px 8px'>Context/session</th>"
            "<th style='text-align:right;padding:4px 8px'>Volume share</th>"
            "<th style='text-align:right;padding:4px 8px'>Contribution</th>"
            "<th style='text-align:center;padding:4px 8px'>Confidence</th>"
            "</tr></thead><tbody>"
        )
        for c in categories:
            ctx = c.get("context_tokens", 0)
            ctx_s = f"{ctx/1000:.0f}K" if ctx < 1_000_000 else f"{ctx/1_000_000:.1f}M"
            contrib = c.get("contribution_tokens", 0)
            contrib_s = f"{contrib/1000:.1f}K"
            conf = c.get("confidence", "?")
            conf_color = {"high": "#1D6F3D", "medium": "#888", "low": "#A32D2D"}.get(conf, "#888")
            parts.append(
                f"<tr style='border-bottom:1px solid #EEE'>"
                f"<td style='padding:4px 8px'>{html.escape(c.get('label', '?'))}</td>"
                f"<td style='text-align:right;padding:4px 8px;font-variant-numeric:tabular-nums'>{ctx_s}</td>"
                f"<td style='text-align:right;padding:4px 8px;font-variant-numeric:tabular-nums'>{c.get('volume_share_pct', 0):.0f}%</td>"
                f"<td style='text-align:right;padding:4px 8px;font-variant-numeric:tabular-nums'><strong>{contrib_s}</strong></td>"
                f"<td style='text-align:center;padding:4px 8px;color:{conf_color};font-weight:600'>{html.escape(conf)}</td>"
                f"</tr>"
            )
        parts.append(
            f"<tr style='background:#F0F0F0;font-weight:700'>"
            f"<td colspan='3' style='padding:6px 8px;text-align:right'>Operative context (sum):</td>"
            f"<td style='text-align:right;padding:6px 8px;font-variant-numeric:tabular-nums'>{op_ctx_s}</td>"
            f"<td></td>"
            f"</tr>"
        )
        parts.append("</tbody></table></div>")

    # Volume layer
    parts.append(
        "<div style='margin:14px 0;padding:12px;background:#FFFFFF;border-left:3px solid #2C5AA0;"
        "font-size:12px;color:#333;line-height:1.55'>"
        "<strong style='color:#1a1a1a'>VOLUME LAYER (multiplier on the position score)</strong><br/>"
        f"Aggregate enterprise AI run-rate: <strong>${ent_rr:.0f}B</strong> annualized "
        f"(MSFT Azure AI + AWS + GCP + Oracle AI infra)<br/>"
        f"Weighted enterprise YoY: <strong>{ent_yoy:+.0f}%</strong> &nbsp;·&nbsp; "
        f"NVIDIA datacenter YoY: <strong>{nv_yoy:+.0f}%</strong><br/>"
        f"<strong>Volume index:</strong> {vol_idx:.0f} "
        f"<span style='color:#888;font-size:11px'>(May 2026 baseline = 100)</span> "
        f"&nbsp;·&nbsp; <strong>Trajectory:</strong> {html.escape(vol_traj)}<br/>"
        f"<strong style='font-size:14px'>Composite HBM bit demand index: "
        f"<span style='color:#0F6E56'>{hbm_demand:.0f}</span></strong> "
        f"<span style='color:#888;font-size:11px'>(= position score × volume index / 100)</span>"
        "</div>"
    )

    # Sanity check
    sanity_bg = "#FAC775" if calibration_flag else "#E8F5E9"
    sanity_border = "#B58A1A" if calibration_flag else "#4CAF50"
    parts.append(
        f"<div style='margin:14px 0;padding:10px 12px;background:{sanity_bg};"
        f"border-left:3px solid {sanity_border};font-size:12px;color:#333;line-height:1.5'>"
        f"<strong style='color:#1a1a1a'>SANITY CHECK</strong> — "
        f"{sanity_confirms}/3 signals confirm matrix output, {sanity_contradicts}/3 contradict<br/>"
        f"&nbsp;&nbsp;• HBM-substitution news (last 90d): {subst_count} items "
        f"<span style='color:#888;font-size:11px'>(threshold for concern: 5+)</span><br/>"
        f"&nbsp;&nbsp;• Hyperscaler decode-off-HBM mentions: {decode_off_hbm}<br/>"
        f"&nbsp;&nbsp;• HBM spot/contract price trend: <strong>{html.escape(hbm_price)}</strong>"
        f"</div>"
    )

    # Sonnet narrative
    if narrative:
        parts.append(
            f"<div style='background:#FFFFFF;padding:10px 12px;border-left:3px solid {status_color};"
            f"margin:14px 0;font-size:12px;color:#333;line-height:1.55'>"
            f"<strong style='color:#1a1a1a'>READ:</strong> {html.escape(narrative)}"
            f"</div>"
        )

    # Bull case + Bear case (NEW v3.4.2 per Raul's ask)
    if bull_case:
        parts.append(
            "<div style='margin:14px 0;padding:12px;background:#E8F5E9;border:1px solid #4CAF50;"
            "border-radius:4px;font-size:12px;color:#1a3300;line-height:1.55'>"
            f"<strong style='color:#1B5E20'>🐂 {html.escape(bull_case[:24])}</strong>"
            f"{html.escape(bull_case[24:])}"
            "</div>"
        )
    if bear_case:
        parts.append(
            "<div style='margin:14px 0;padding:12px;background:#FFEBEE;border:1px solid #E57373;"
            "border-radius:4px;font-size:12px;color:#5A1A1A;line-height:1.55'>"
            f"<strong style='color:#B71C1C'>🐻 {html.escape(bear_case[:24])}</strong>"
            f"{html.escape(bear_case[24:])}"
            "</div>"
        )

    # Substitution risk flags
    if subst:
        parts.append(
            "<div style='margin-top:10px;font-size:11px;color:#666'>"
            "<strong style='color:#1a1a1a'>HBM-substitution risk flags (non-HBM accelerators):</strong>"
            "<ul style='margin:4px 0 0 0;padding-left:20px'>"
        )
        for note in subst[:5]:
            parts.append(f"<li>{html.escape(note)}</li>")
        parts.append("</ul></div>")

    # Framework explanation footer
    parts.append(
        "<div style='margin-top:18px;padding-top:12px;border-top:1px solid #DDD;"
        "font-size:11px;color:#666;line-height:1.5'>"
        "<strong style='color:#1a1a1a'>FRAMEWORK PRIMER:</strong> "
        "DHCR maps today's inference market onto a 5×5 matrix — Y-axis is KV "
        "efficiency (GB per 1M live tokens at frontier architecture), X-axis is "
        "operative context length (volume-weighted across consumer chat, coding "
        "agents, enterprise vertical agents, developer API, other). Each cell maps "
        "to a dominant memory architecture from the May 2026 design memo "
        "(commodity DDR ↔ LPDDR/heterogeneous ↔ HBM dominance). The score "
        "translates cell position to a linear 0-100 spectrum (HBM-favorable "
        "↔ commodity). An anticipation premium (+1 Y-band) accounts for "
        "the empirical reality that hyperscalers provision HBM ahead of "
        "where today's workload demand sits, because they expect context to "
        "follow capacity. Volume layer multiplies position by enterprise AI "
        "revenue growth (May 2026 = 100). Sanity check validates against "
        "three substitution evidence signals.<br/><br/>"
        "<strong style='color:#1a1a1a'>WHY THIS MATTERS:</strong> "
        "Efficiency gains do NOT displace HBM — they UNLOCK economic context "
        "capacity. Enterprise workloads expand to consume what's economically "
        "possible. The make-or-break is enterprise agentic adoption: if it "
        "ignites (volume share growing 5% → 30% × context growing 800K → 5M), "
        "HBM bit demand grows 10-15x even WITH dramatic efficiency improvements. "
        "If enterprise stalls, the market lands in LPDDR/heterogeneous regardless "
        "of efficiency. Watch the enterprise vertical category row carefully — "
        "that's where this thesis lives or dies.</div>"
    )

    parts.append("</div>")
    return "\n".join(parts)


def _build_relative_strength_snippet(rel_strength_verdict: Optional[dict]) -> str:
    """v3.39.7.9 — pre-built HTML snippet showing the computed 5d-vs-SOX
    relative strength numbers, color-coded per the fire thresholds:

      ≤ -10pp  →  red (Fired band)
      -10 to -5pp →  amber (Partially fired band)
      > -5pp   →  green (Not fired band)

    Renders for SK Hynix, Micron, and DRAM ETF vs SMH. Used in the
    section 3 summary card for t3_relative_strength so the user sees
    the actual numeric value at a glance (not just the status pill).

    Returns "" if rel_strength_verdict is None / empty — section
    3 row falls back to pure Sonnet prose. Defensive against NaN
    (math.isnan check) — pre-v3.39.7.9 a NaN propagated as "+nanpp".
    """
    if not rel_strength_verdict:
        return ""

    def _band_color(pp_value: Optional[float]) -> str:
        """Map a 5d-vs-SOX value (in percentage points) to a status color."""
        if pp_value is None or (isinstance(pp_value, float) and math.isnan(pp_value)):
            return "#888888"  # grey — not computed
        if pp_value <= -10.0:
            return STATUS_COLORS["Fired"]
        if pp_value <= -5.0:
            return STATUS_COLORS["Partially fired"]
        return STATUS_COLORS["Not fired"]

    def _format_pp(pp_value: Optional[float]) -> str:
        """Format a percentage-point value with NaN guard."""
        if pp_value is None or (isinstance(pp_value, float) and math.isnan(pp_value)):
            return "(not computed)"
        return f"{pp_value:+.2f}pp"

    skhx_pp = rel_strength_verdict.get("skhx_5d_vs_sox_pp")
    mu_pp = rel_strength_verdict.get("mu_5d_vs_sox_pp")
    dram_block = rel_strength_verdict.get("dram_etf_vs_smh") or {}
    dram_pp = dram_block.get("spread_pp")

    rows = []
    # SK Hynix
    skhx_color = _band_color(skhx_pp)
    rows.append(
        f"<span style='display:inline-block;background:{skhx_color};"
        f"color:#1a1a1a;padding:2px 8px;border-radius:3px;font-weight:600;"
        f"font-size:12px;margin-right:6px;margin-bottom:3px'>"
        f"SK Hynix 5d vs SOX: {_format_pp(skhx_pp)}</span>"
    )
    # Micron
    mu_color = _band_color(mu_pp)
    rows.append(
        f"<span style='display:inline-block;background:{mu_color};"
        f"color:#1a1a1a;padding:2px 8px;border-radius:3px;font-weight:600;"
        f"font-size:12px;margin-right:6px;margin-bottom:3px'>"
        f"Micron 5d vs SOX: {_format_pp(mu_pp)}</span>"
    )
    # DRAM ETF vs SMH
    dram_color = _band_color(dram_pp)
    rows.append(
        f"<span style='display:inline-block;background:{dram_color};"
        f"color:#1a1a1a;padding:2px 8px;border-radius:3px;font-weight:600;"
        f"font-size:12px;margin-right:6px;margin-bottom:3px'>"
        f"DRAM ETF 5d vs SMH: {_format_pp(dram_pp)}</span>"
    )

    # Thresholds line for context
    thresholds_line = (
        "<div style='font-size:11px;color:#666;margin-top:4px'>"
        "Thresholds: red ≤ −10pp (Fired) | yellow ≤ −5pp (Partially fired) | green &gt; −5pp (Not fired)"
        "</div>"
    )

    return (
        "<div style='margin-bottom:8px'>"
        + "".join(rows)
        + thresholds_line
        + "</div>"
    )


def _build_indicators_section(
    assessments: list[IndicatorAssessment],
    rel_strength_verdict: Optional[dict] = None,
    dhcr_snapshot: Optional[dict] = None,
    signal_reasonings_by_id: Optional[dict] = None,
) -> str:
    """Build the four-tier indicator status tables.

    v3.39.7.5 — the cycle matrix snapshot card was moved out of this
    section (was injected under t1_spot_decline at the bottom of section
    3 since v3.39.7). It now renders inside section 4 (Top 5 indicators
    — deep dive), at the bottom of the DRAM Price Evolution deep-dive
    item per user direction 2026-05-20.

    v3.39.7.9 — rel_strength_verdict param added.

    v3.4.0 — dhcr_snapshot param was added, originally to inject a section 3
    direction-monitor snippet for t1_decode_hbm_captive_ratio.

    v3.4.3.1 — DHCR snippet removed; t1_decode_hbm_captive_ratio is filtered
    out of the per-tier loop entirely. DHCR renders ONLY in section 4 as
    the last deep-dive card. The dhcr_snapshot parameter is retained for
    backward-compatible call signatures but is no longer consumed here.
    See CLAUDE.md locked decisions and DHCR_Framework_Explained.docx.
    """
    # v3.39.7.9 — pre-build the t3_relative_strength computed snippet once
    # so the per-row loop can drop it in cheaply.
    rs_snippet = _build_relative_strength_snippet(rel_strength_verdict)
    # v3.4.3.1 — _build_dhcr_section3_snippet prep removed. DHCR no longer
    # appears in section 3; the section 4 card is the only DHCR surface.

    parts = []
    # v3.4.3.1 — DHCR (t1_decode_hbm_captive_ratio) is structurally Tier 1
    # but renders ONLY as a section 4 deep-dive card; NEVER in this section 3
    # tier indicator status table. The v3.4.2 framework redesign (5x5 matrix
    # with score 0-100, anticipation premium, sanity check) produces outputs
    # that don't fit the section 3 row format. Filter DHCR out before the
    # per-tier loop so it can't render here. See CLAUDE.md locked decisions
    # and DHCR_Framework_Explained.docx Appendix B for the placement rule.
    #
    # v3.4.4 — same pattern extended to t1_competitor_dynamics (Indicator 7)
    # and t1_us_adr_event (Indicator 8). Both are composite indicators with
    # rich sub-structure (4 subs each + EPS-impact composite for 7, lifecycle
    # phase + calendar for 8) that render only as section 4 deep-dive cards.
    # v3.4.7 — The set lives in src/config.py as the canonical source of
    # truth so the count sites (llm_summarizer._compute_tier_status_summary
    # and chart_generator.bucket_assessments_by_tier) use exactly the
    # same list. Drift between section-3 filter and exec-summary count
    # was the cause of the May 23 2026 "T1: 5/6 fired vs section-3 4/6"
    # discrepancy.
    from src.config import SECTION_4_ONLY_INDICATORS
    for tier in (1, 2, 3, 4):
        tier_assessments = [a for a in assessments
                            if a.tier == tier
                            and a.id not in SECTION_4_ONLY_INDICATORS]
        if not tier_assessments:
            continue

        parts.append(f"<h3 style='background:{TIER_COLORS[tier]};padding:6px 10px;border-radius:3px'>{TIER_LABELS[tier]}</h3>")
        parts.append("<table>")
        parts.append(
            "<thead><tr>"
            "<th style='width:16%'>Indicator</th>"
            "<th style='width:22%'>What we're looking for</th>"
            "<th style='width:9%'>Status</th>"
            "<th style='width:28%'>Where we stand</th>"
            "<th style='width:25%'>Latest developments</th>"
            "</tr></thead><tbody>"
        )

        for a in tier_assessments:
            status_color = STATUS_COLORS.get(a.current_status, "#CCCCCC")
            # v3.4.3.1 — Fired (red) and Partially fired (amber) backgrounds
            # are too dark for default near-black badge text per Raul May 22.
            # White text on dark backgrounds; near-black on light backgrounds
            # (Not fired green, etc.).
            status_text_color = (
                "#FFFFFF" if a.current_status in ("Fired", "Partially fired")
                else "#1a1a1a"
            )
            # v3.4.1 Fix 2 — REVERTED v3.39.7.9's colored-background experiment.
            # Per user direction 2026-05-22: the "CHANGED:" annotation should
            # render as plain text (no background tint) — only the status pill
            # itself carries the destination color. The colored background was
            # visually redundant with the pill and made the cell read as two
            # competing badges. This version: small bold prefix + plain
            # description text in muted color.
            # v3.4.1 Fix 3b — also scrub "Reversed" → "Not fired" at render time
            # in case any stale state slipped the word through.
            change_flag = ""
            if a.change_vs_prior:
                change_text = a.change_vs_prior[:80]
                # Eradicate "Reversed" from any code path that still emits it
                change_text = change_text.replace("'Reversed'", "'Not fired'")
                change_text = change_text.replace("Reversed", "Not fired")
                change_flag = (
                    f"<div style='margin-top:6px;font-size:11px;color:#555;line-height:1.4'>"
                    f"<strong style='color:#333'>CHANGED:</strong> "
                    f"{html.escape(change_text)}</div>"
                )

            links_html = ""
            if a.relevant_links:
                link_strs = [
                    f"<a href='{html.escape(l.get('url', '#'))}' target='_blank'>{html.escape(l.get('title', 'link')[:60])}</a>"
                    for l in a.relevant_links[:3]
                ]
                links_html = f"<div class='links-list' style='margin-top:6px'>{' · '.join(link_strs)}</div>"

            # v3.39.7.9 — for t3_relative_strength, lead the "Where we stand"
            # cell with the computed 5d-vs-SOX numbers (color-coded by fire
            # threshold) before the Sonnet-written prose. Per user direction
            # 2026-05-21: the number is what trips the indicator, so the
            # number must be visible at the section 3 summary level — not
            # just in section 4 deep dive.
            #
            # v3.39.7.10 — fixed AttributeError crash in production. The
            # attribute is `id` on IndicatorAssessment, not `indicator_id`
            # (the latter doesn't exist). v3.39.7.9 unit tests covered the
            # _build_relative_strength_snippet helper in isolation but never
            # exercised this branch in _build_indicators_section against a
            # real IndicatorAssessment object, so the typo escaped tests
            # and only surfaced when the 15:13 UTC fast-tier run hit this
            # line. v3.39.7.10 adds an integration test that constructs
            # an IndicatorAssessment(id="t3_relative_strength", ...) and
            # verifies the rendered HTML contains the snippet badges.
            # v3.4.1 Fix 5b — defensive fallback for empty where_we_stand.
            # The May 21 2026 brief had t2_capex_parabolic's where_we_stand
            # rendered as empty cell despite rich Layer 2 verdict + evidence
            # store carry-forward. Root cause: Sonnet hits its max_tokens
            # budget mid-output and silently truncates trailing fields. The
            # word cap in the prompt (v3.4.1 Fix 5) is the primary mitigation;
            # this is belt-and-braces — if the field arrives empty or
            # absurdly short (<30 chars), render a fallback derived from the
            # Layer 2 verdict for that indicator (threaded via the new
            # signal_reasonings_by_id kwarg). User-visible result: a populated
            # cell with the L2-derived "where we currently stand" prose
            # instead of an empty cell.
            where_we_stand_raw = (a.where_we_stand or "").strip()
            if len(where_we_stand_raw) < 30 and signal_reasonings_by_id:
                fallback = signal_reasonings_by_id.get(a.id)
                if fallback:
                    consensus = (fallback.get("consensus_direction") or "").strip()
                    confidence = (fallback.get("confidence_level") or "").strip()
                    if consensus:
                        fb_text = (
                            f"[Sonnet output truncated — fallback from Layer 2 verdict] "
                            f"{consensus[:400]}"
                        )
                        if confidence:
                            fb_text += f" (confidence: {confidence})"
                        where_we_stand_raw = fb_text
            # Also scrub "Reversed" → "Not fired" here (v3.4.1 Fix 3b defensive)
            where_we_stand_raw = where_we_stand_raw.replace("'Reversed'", "'Not fired'")
            where_we_stand_raw = where_we_stand_raw.replace("Reversed", "Not fired")
            where_we_stand_html = html.escape(where_we_stand_raw)
            if a.id == "t3_relative_strength" and rs_snippet:
                where_we_stand_html = rs_snippet + where_we_stand_html
            # v3.4.3.1 — DHCR (t1_decode_hbm_captive_ratio) snippet branch
            # removed. DHCR is filtered out of this loop's tier_assessments
            # at the top of _build_indicators_section; its rendering lives
            # exclusively in the section 4 deep-dive card.

            parts.append(
                "<tr>"
                f"<td class='indicator-cell'><strong>{html.escape(a.name)}</strong><br/><span style='color:#888;font-size:11px'>{html.escape(a.description[:120])}</span></td>"
                f"<td class='indicator-cell'>{html.escape(a.what_to_look_for)}</td>"
                f"<td class='indicator-cell'><span class='status-badge' style='background:{status_color};color:{status_text_color}'>{html.escape(a.current_status)}</span>{change_flag}</td>"
                f"<td class='indicator-cell'>{where_we_stand_html}</td>"
                f"<td class='indicator-cell'>{a.latest_developments}{links_html}</td>"
                "</tr>"
            )

            # v3.39.7.5: cycle matrix card moved to section 4 deep-dive,
            # was previously injected here under t1_spot_decline.

        parts.append("</tbody></table>")

    return "\n".join(parts)


def _build_cycle_matrix_card(
    cycle_verdict: Optional[object],
    matrix_b64: str,
    spot_contract_b64: str,
) -> str:
    """v3.39.7 — render the two cycle-matrix charts as a card row spanning all
    columns of the indicators table, placed directly under t1_spot_decline.

    cycle_verdict is read via getattr() to keep this module decoupled from
    cycle_matrix.py types (avoids a new import on this hot path).
    """
    narrative = ""
    cell_value = ""
    cell_color = "#CCCCCC"
    if cycle_verdict is not None:
        narrative = getattr(cycle_verdict, "narrative", "") or ""
        cell_obj = getattr(cycle_verdict, "cell", None)
        if cell_obj is not None:
            cell_value = getattr(cell_obj, "value", "")
            # v3.39.7.1 — cell_value strings now match the framework's
            # status vocabulary directly ("Not fired" / "Partially fired" /
            # "Fired"), so the map keys are STATUS_COLORS keys.
            cell_color = STATUS_COLORS.get(cell_value, "#CCCCCC")

    cell_badge = (
        f"<span style='display:inline-block;background:{cell_color};"
        f"color:#2c2c2a;padding:4px 12px;border-radius:3px;"
        f"font-weight:700;font-size:13px;margin-left:8px'>{html.escape(cell_value)}</span>"
        if cell_value else ""
    )

    return f"""<tr><td colspan="5" style="padding:0;background:#fafaf7">
  <div style="border:1px solid #e0ddd4;border-radius:6px;margin:8px 0;padding:14px 18px;background:#fff">
    <div style="font-size:13px;font-weight:700;color:#534AB7;margin-bottom:6px">
      Cycle matrix snapshot{cell_badge}
    </div>
    <div style="font-size:12px;color:#5F5E5A;margin-bottom:12px">
      {html.escape(narrative[:600])}
    </div>
    <table style="width:100%;border-collapse:separate;border-spacing:8px 0;margin:0">
      <tr>
        <td style="width:40%;vertical-align:top;border:none;padding:0">
          <img src="data:image/png;base64,{matrix_b64}"
               alt="Cycle matrix grid with current position"
               style="width:100%;height:auto;display:block;border:1px solid #e0ddd4;border-radius:4px"/>
        </td>
        <td style="width:60%;vertical-align:top;border:none;padding:0">
          <img src="data:image/png;base64,{spot_contract_b64}"
               alt="DDR5 spot price and synthetic contract estimate over time"
               style="width:100%;height:auto;display:block;border:1px solid #e0ddd4;border-radius:4px"/>
        </td>
      </tr>
    </table>
  </div>
</td></tr>"""


def _build_top5_section(
    top5: list[IndicatorAssessment],
    cycle_verdict: Optional[object] = None,
    cycle_matrix_chart_b64: Optional[str] = None,
    spot_contract_chart_b64: Optional[str] = None,
) -> str:
    """Deeper write-up for the top 5 indicators.

    v3.39.7.5 — when rendering t1_spot_decline AND both chart base64
    strings are provided, append a full-width "Cycle matrix snapshot"
    card directly under that indicator's deep-dive write-up. Moved here
    from section 3 (where it lived in v3.39.7-v3.39.7.4) per user
    direction: the matrix + chart belong inside the deep-dive context,
    not stuck under a summary card.
    """
    parts = []
    for a in top5:
        status_color = STATUS_COLORS.get(a.current_status, "#CCCCCC")
        # v3.4.3.1 — same Fired/Partially fired white-text fix as section 3.
        status_text_color = (
            "#FFFFFF" if a.current_status in ("Fired", "Partially fired")
            else "#1a1a1a"
        )
        parts.append(
            f"<h3>{html.escape(a.name)} "
            f"<span class='status-badge' style='background:{status_color};color:{status_text_color};margin-left:8px'>{html.escape(a.current_status)}</span></h3>"
        )
        parts.append(f"<p style='font-size:13px'><strong>What we're looking for:</strong> {html.escape(a.what_to_look_for)}</p>")
        parts.append(f"<p style='font-size:13px'><strong>Where we stand:</strong> {html.escape(a.where_we_stand)}</p>")
        if a.latest_developments:
            parts.append(f"<p style='font-size:13px'><strong>Latest developments:</strong> {a.latest_developments}</p>")
        if a.relevant_links:
            link_html = " · ".join(
                f"<a href='{html.escape(l.get('url', '#'))}' target='_blank'>{html.escape(l.get('title', 'link')[:80])}</a>"
                for l in a.relevant_links[:5]
            )
            parts.append(f"<p class='links-list' style='font-size:12px'>{link_html}</p>")

        # v3.39.7.5: inject cycle matrix card under t1_spot_decline's deep dive.
        # If either chart b64 is missing, falls back to silent skip — keeps the
        # deep-dive narrative readable even when cycle_matrix step failed.
        if (a.id == "t1_spot_decline"
            and cycle_matrix_chart_b64
            and spot_contract_chart_b64):
            parts.append(_build_cycle_matrix_card(
                cycle_verdict, cycle_matrix_chart_b64, spot_contract_chart_b64,
            ))

    return "\n".join(parts)


def _build_news_section(news_items: list[dict]) -> str:
    """Render the curated news headlines."""
    if not news_items:
        return "<p style='color:#888'>No major news items selected for this brief.</p>"

    parts = []
    for n in news_items:
        headline = html.escape(n.get("headline", ""))
        url = html.escape(n.get("url", "#"))
        why = n.get("why_it_matters", "")
        parts.append(
            f"<div class='news-item'>"
            f"<a href='{url}' target='_blank'>{headline}</a>"
            + (f"<div class='why-matters'>{html.escape(why)}</div>" if why else "")
            + "</div>"
        )

    return "\n".join(parts)

# ============================================================
# Data source validation section
# ============================================================

# Human labels for source_type categories
_SOURCE_TYPE_LABELS = {
    "rss": "RSS feed",
    "edgar": "SEC EDGAR (atom)",
    "google_news": "Google News (RSS)",
    "scraper": "Web scraper",
    "yfinance": "yfinance",
    "fmp": "FMP (estimates)",
    "llm": "Anthropic API",
    # v3.38 — added to surface deep-tier and previously-batched sources
    "api": "External API",
    "transcript": "Earnings transcript source",
    "broker_pdf": "Korean broker note feed",
}


def _build_data_source_validation(statuses: list[SourceFetchStatus]) -> str:
    """Build the per-source validation table at the bottom of the email.

    Groups: shows a one-line summary pill, then a grouped table by source_type.
    Failures and warnings sort to the top within each group; skipped rows
    sort to the bottom.

    v3.39.7.6 — fifth health state 'quiet' rendered. Sources flagged
    `expected_empty=True` (forward-looking architecture watchers, pre-
    listing SK Hynix EDGAR feeds) render as 'QUIET (0)' in muted gray
    rather than as 'WARN'. The distinction is informational: the WARN
    count now only contains sources that are unexpectedly empty (worth
    investigating); QUIET counts the routine-quiet watchers (expected).
    """
    # Summary pills
    ok_count = sum(1 for s in statuses if s.health == "ok")
    warn_count = sum(1 for s in statuses if s.health == "warn")
    fail_count = sum(1 for s in statuses if s.health == "fail")
    skipped_count = sum(1 for s in statuses if s.health == "skipped")
    quiet_count = sum(1 for s in statuses if s.health == "quiet")  # v3.39.7.6
    total = len(statuses)

    pills = (
        f"<span class='validation-pill validation-ok'>OK {ok_count}</span>"
        f"<span class='validation-pill validation-warn'>WARN {warn_count}</span>"
        f"<span class='validation-pill validation-fail'>FAIL {fail_count}</span>"
    )
    if quiet_count:
        pills += f"<span class='validation-pill validation-quiet'>QUIET {quiet_count}</span>"
    if skipped_count:
        pills += f"<span class='validation-pill validation-skipped'>SKIPPED {skipped_count}</span>"

    summary = (
        f"<div class='validation-summary'>"
        f"<strong>{total} sources queried this run · </strong>"
        f"{pills}"
        f"</div>"
    )

    # Group by source_type, preserve insertion order
    groups: dict[str, list[SourceFetchStatus]] = {}
    for s in statuses:
        groups.setdefault(s.source_type, []).append(s)

    # Sort within each group: fail first, warn, ok, quiet, then skipped at the bottom
    # v3.39.7.6 — quiet sits below ok (informational, expected) and above
    # skipped (intentionally not queried).
    health_order = {"fail": 0, "warn": 1, "ok": 2, "quiet": 3, "skipped": 4}
    for group_list in groups.values():
        group_list.sort(key=lambda x: (health_order.get(x.health, 9), x.source_name))

    table_parts = ["<table style='font-size:11px'>"]
    table_parts.append(
        "<thead><tr>"
        "<th style='width:32%'>Source</th>"
        "<th style='width:14%'>Type</th>"
        "<th style='width:10%'>Status</th>"
        "<th style='width:10%'>Items</th>"
        "<th style='width:10%'>Latency</th>"
        "<th style='width:24%'>Detail / error</th>"
        "</tr></thead><tbody>"
    )

    # Render groups in a stable order (most signal-rich first)
    type_order = ["edgar", "rss", "google_news", "scraper", "yfinance", "fmp", "llm"]
    seen_types = set()

    badge_class_map = {
        "ok": "validation-ok",
        "warn": "validation-warn",
        "fail": "validation-fail",
        "skipped": "validation-skipped",
        "quiet": "validation-quiet",  # v3.39.7.6
    }
    badge_label_map = {
        "ok": "OK",
        "warn": "WARN",
        "fail": "FAIL",
        "skipped": "SKIPPED",
        "quiet": "QUIET",  # v3.39.7.6
    }

    for tkey in type_order + [t for t in groups if t not in type_order]:
        if tkey not in groups or tkey in seen_types:
            continue
        seen_types.add(tkey)

        # Group header row
        table_parts.append(
            f"<tr><td colspan='6' style='background:#f0eee5;font-weight:700;color:#534AB7;font-size:11px;padding:4px 10px'>"
            f"{html.escape(_SOURCE_TYPE_LABELS.get(tkey, tkey))}"
            f"</td></tr>"
        )

        for s in groups[tkey]:
            row_class = f"source-row-{s.health}"
            badge_class = badge_class_map.get(s.health, "validation-warn")
            badge_label = badge_label_map.get(s.health, s.health.upper())

            # Detail / error column
            detail_html = ""
            if s.error:
                detail_html = f"<span class='source-error'>{html.escape(s.error[:200])}</span>"
            elif s.detail:
                detail_html = f"<span class='source-detail'>{html.escape(s.detail[:200])}</span>"

            latency_str = f"{s.latency_ms} ms" if s.latency_ms else "—"

            table_parts.append(
                f"<tr class='{row_class}'>"
                f"<td><strong>{html.escape(s.source_name)}</strong></td>"
                f"<td style='color:#888;font-size:11px'>{html.escape(_SOURCE_TYPE_LABELS.get(s.source_type, s.source_type))}</td>"
                f"<td><span class='source-status-badge {badge_class}'>{badge_label}</span></td>"
                f"<td style='font-variant-numeric:tabular-nums'>{s.items_returned}</td>"
                f"<td style='font-variant-numeric:tabular-nums;color:#888'>{latency_str}</td>"
                f"<td>{detail_html}</td>"
                f"</tr>"
            )

    table_parts.append("</tbody></table>")

    return summary + "\n" + "\n".join(table_parts)


# ============================================================
# Calendar of events section
# ============================================================

# Color tints per category (light backgrounds for visual grouping)
_CATEGORY_TINTS = {
    "Earnings":              "#FCEBEB",   # red tint — earnings = catalyst
    "Industry data":         "#FAEEDA",   # amber — TrendForce, IDC, Counterpoint
    "Conference":            "#DCE9F6",   # blue — JP Morgan, COMPUTEX, GTC
    "Indicator 6 / HBM4":    "#EAF3DE",   # green — competitor dynamics
    "Indicator 7 / ADR":     "#F0E6F7",   # purple tint — ADR launch
    "Macro":                 "#ECEAE3",   # neutral
    "Multiple":              "#F6F4EE",   # neutral
}


def _build_calendar_section(events: list[dict]) -> str:
    """Render the upcoming events calendar as a grouped table.

    Events arrive sorted by date ascending. Groups by week buckets:
    'This week', 'Next week', 'In 2-4 weeks', 'In 1-2 months', 'In 2-3 months'.
    """
    if not events:
        return "<p style='color:#888;font-size:12px'>No major events in the next 90 days.</p>"

    # Bucket events by horizon
    buckets = {
        "Imminent (≤7 days)":  [],
        "Next 2 weeks":        [],
        "In 2-4 weeks":        [],
        "In 1-2 months":       [],
        "In 2-3 months":       [],
    }
    for e in events:
        d = e["days_away"]
        if d <= 7:
            buckets["Imminent (≤7 days)"].append(e)
        elif d <= 14:
            buckets["Next 2 weeks"].append(e)
        elif d <= 28:
            buckets["In 2-4 weeks"].append(e)
        elif d <= 60:
            buckets["In 1-2 months"].append(e)
        else:
            buckets["In 2-3 months"].append(e)

    parts = ["<table style='font-size:12px'>"]
    parts.append(
        "<thead><tr>"
        "<th style='width:14%'>Date</th>"
        "<th style='width:8%'>In</th>"
        "<th style='width:16%'>Category</th>"
        "<th style='width:28%'>Event</th>"
        "<th style='width:24%'>Why it matters</th>"
        "<th style='width:10%'>Signal</th>"
        "</tr></thead><tbody>"
    )

    for bucket_label, bucket_events in buckets.items():
        if not bucket_events:
            continue
        # Bucket header row
        parts.append(
            f"<tr><td colspan='6' style='background:#f0eee5;font-weight:700;color:#534AB7;"
            f"padding:4px 10px;font-size:12px'>{html.escape(bucket_label)}</td></tr>"
        )
        for e in bucket_events:
            category = e["category"]
            tint = _CATEGORY_TINTS.get(category, "#ffffff")
            date_str = e["date"].strftime("%b %d (%a)")
            days_str = f"+{e['days_away']}d"
            parts.append(
                f"<tr>"
                f"<td style='font-variant-numeric:tabular-nums'>{html.escape(date_str)}</td>"
                f"<td style='font-variant-numeric:tabular-nums;color:#888'>{html.escape(days_str)}</td>"
                f"<td style='background:{tint};font-size:11px'>{html.escape(category)}</td>"
                f"<td><strong>{html.escape(e['title'])}</strong></td>"
                f"<td style='font-size:11px;color:#5F5E5A'>{html.escape(e['why_it_matters'])}</td>"
                f"<td style='font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#534AB7'>"
                f"{html.escape(e['related'])}</td>"
                f"</tr>"
            )

    parts.append("</tbody></table>")
    return "\n".join(parts)


# ============================================================
# Sharpened banners: relative strength + EPS revisions watch
# ============================================================

def _indicator_in_24h_trigger_log(es, indicator_id: str) -> bool:
    """v3.41.10 — True iff `indicator_id` has a transition in today's display log.

    Gate for the top-of-brief status banners (relative-strength RED FIRE, EPS
    revisions). Those banners were rendered on raw signal STATUS — as long as
    t3_relative_strength stayed at red severity the 🚨 banner squatted at the
    top of every brief (observed: fired Fri Jul 3, still showing Mon Jul 6
    while the exec summary correctly said "No new trigger events today").
    The NEW TRIGGER EVENT pill uses the 24h trigger log (v3.39.7.5) and
    therefore vanished on schedule; the banners were never wired to the same
    clock. This helper checks the same collapsed log the pill renders
    (es.new_triggers_today), so banner and pill now share transition-day
    semantics: shown the day of the transition (and on intraday reruns within
    24h), gone the next day even if the signal stays fired — ongoing status
    remains visible in the §3 table and the indicator card. A genuine
    re-transition (drops out, re-enters) logs a new event and re-shows the
    banner.
    """
    try:
        entries = getattr(es, "new_triggers_today", None) or []
        return any(
            isinstance(t, dict) and t.get("indicator_id") == indicator_id
            for t in entries
        )
    except Exception:
        return False


_STATUS_FROM_DESC_RE = re.compile(r"from '([^']+)' to '([^']+)'")


def _build_generic_trigger_banner(trig: dict) -> str:
    """v3.41.15 — top-of-brief banner for ANY 24h-log transition.

    v3.41.17 — shape-proof. The Jul-23 P/E fire rendered as an amber
    "SIGNAL DOWNGRADE" with "Status: ? → ?": the fast_tier shape-conversion
    stripped old_status/new_status (fixed at source) and this function
    printed the literal "?" defaults and mis-classified direction. Now:
    statuses resolve from keys when present, else parse from the description
    ("from 'X' to 'Y'"); the status line renders only when both are known —
    never "?"; the description renders only when it adds information beyond
    the status line; unresolved direction defaults to the RED alarm (a
    trigger event with unknown direction should alarm, not soothe).
    """
    # v3.41.18 — colors keyed to the DESTINATION status, matching the
    # exec-summary trigger pills (v3.4.1 convention) that render in the SAME
    # email: → Fired red, → Partially fired amber, → Not fired green. The
    # v3.41.15 banner knew only red-for-fired-direction / amber-for-the-rest,
    # so a Fired→Not fired reversion sat in an amber warning box above an
    # exec summary showing the same event as a green pill. Readability: the
    # amber variant set label color #7A5500 but inherited the CSS class's
    # DARK RED pill background (.urgent-banner .label) — dark olive on dark
    # red, the unreadable titles in the Jul-24/25 briefs. Every variant now
    # sets pill background AND text explicitly; dark pills always get white
    # text (the v3.4.3.1 pill lesson). Unknown destination stays the red
    # alarm (v3.41.17: alarm, never soothe).
    name = trig.get("indicator_name") or trig.get("indicator_id") or "indicator"
    tier = trig.get("tier", "?")
    old = trig.get("old_status")
    new = trig.get("new_status")
    desc = trig.get("description") or ""
    if not old or not new:
        m = _STATUS_FROM_DESC_RE.search(desc)
        if m:
            old, new = m.group(1), m.group(2)
    known = bool(old) and bool(new)
    status_line = (
        f"Status: {html.escape(str(old))} → {html.escape(str(new))}."
        if known else ""
    )
    generic_phrase = (
        f"Status changed from '{old}' to '{new}'." if known else None
    )
    show_desc = bool(desc) and desc != generic_phrase

    if not known or new == "Fired":
        # Fresh full fire — or unresolvable direction, which must alarm.
        box_bg, box_border = "#FCEBEB", "#A32D2D"
        pill_bg, body_color = "#B91C1C", "#791F1F"
        label = f"🚨 NEW TIER {html.escape(str(tier))} TRIGGER — {html.escape(str(name))}"
    elif new == "Partially fired":
        box_bg, box_border = "#FFF6E0", "#B58A1A"
        pill_bg, body_color = "#8A5A00", "#5A3F00"
        if old == "Fired":
            label = (f"⚠️ SIGNAL DOWNGRADE — {html.escape(str(name))} "
                     f"(Tier {html.escape(str(tier))})")
        else:
            label = (f"⚠️ NEW TIER {html.escape(str(tier))} TRIGGER (PARTIAL) — "
                     f"{html.escape(str(name))}")
    else:  # → Not fired: the signal receded — green, per destination rule
        box_bg, box_border = "#EAF4E2", "#4C8B3B"
        pill_bg, body_color = "#2F6B2F", "#244F24"
        label = (f"✅ SIGNAL DOWNGRADE — {html.escape(str(name))} "
                 f"(Tier {html.escape(str(tier))})")

    desc_html = (
        f"<div style='margin-top:6px;font-size:13px;color:{body_color}'>"
        f"{html.escape(desc)}</div>"
        if show_desc else ""
    )
    body = status_line or html.escape(desc)
    return (
        f"<div class='urgent-banner' style='background:{box_bg};"
        f"border:2px solid {box_border}'>"
        f"<span class='label' style='background:{pill_bg};color:#fff'>"
        f"{label}</span><br/>"
        f"<span class='text' style='color:{body_color}'>{body}</span>"
        f"{desc_html if status_line else ''}"
        "</div>"
    )


def _build_relative_strength_banner(verdict: dict) -> str:
    """Render the relative-strength banner. v3.39: two severity levels.
    - red:    full Tier 3 fire — bold red banner, 🚨
    - yellow: informational watch (5d spread between -5 and -10pp) — soft
              amber banner, 🟡. Does NOT trip urgent_attention.
    - none:   no banner.

    Back-compat: handles older verdict dicts that only had `fired_sub_triggers`
    (list of strings) and `overall_fired` (bool). For those, treat as red.
    """
    triggers = verdict.get("fired_sub_triggers", [])
    if not triggers:
        return ""

    severity = verdict.get("overall_severity")
    # Normalize triggers: v3.39 emits (sev, desc) tuples; older versions emit
    # plain strings. Extract description regardless.
    descs = []
    for t in triggers:
        if isinstance(t, (list, tuple)) and len(t) == 2:
            descs.append(t[1])
        else:
            descs.append(t)
    bullets = "".join(f"<li>{html.escape(d)}</li>" for d in descs)

    # Older verdicts without overall_severity default to red (preserves v3.38
    # behavior when an old prior_state.json is in scope).
    if severity is None:
        severity = "red" if verdict.get("overall_fired") else "yellow"

    if severity == "red":
        return (
            "<div class='urgent-banner' style='background:#FCEBEB;border:2px solid #A32D2D'>"
            "<span class='label'>🚨 RELATIVE STRENGTH RED FIRE</span><br/>"
            "<span class='text'>Memory pure-plays underperforming semis by &gt;10pp over 5d — Tier 3 signal active</span>"
            f"<ul style='margin:8px 0 0 0;color:#791F1F;font-size:13px'>{bullets}</ul>"
            "</div>"
        )
    else:  # yellow
        return (
            "<div class='urgent-banner' style='background:#FFF6E0;border:2px solid #B58A1A'>"
            "<span class='label' style='color:#7A5500'>🟡 RELATIVE STRENGTH YELLOW WATCH</span><br/>"
            "<span class='text' style='color:#5A3F00'>Memory pure-plays underperforming semis by 5-10pp over 5d — informational, not a full fire</span>"
            f"<ul style='margin:8px 0 0 0;color:#5A3F00;font-size:13px'>{bullets}</ul>"
            "</div>"
        )


def _build_eps_revisions_banner(verdict: dict) -> str:
    """🚨 banner for canonical Tier 2 EPS revisions signal."""
    status = verdict.get("overall_status", "Not fired")
    label_map = {
        "FIRED": ("🚨 EPS REVISIONS FIRED", "#A32D2D", "Canonical Tier 2 signal: 30d weighted score < -15"),
        "EARLY_WARNING": ("⚠️ EPS REVISIONS EARLY WARNING", "#B36B00", "7d weighted score < -10 — approaching trigger"),
    }
    if status not in label_map:
        return ""
    label, color, sub = label_map[status]
    # Build per-ticker summary — bearish-only for FIRED/EARLY_WARNING since those fire on bearish
    rows = []
    for ticker, td in verdict.get("per_ticker", {}).items():
        if td.get("fired_status") in ("FIRED", "EARLY_WARNING"):
            r30 = td["rolling_30d"].get("score_bearish", 0)
            r7 = td["rolling_7d"].get("score_bearish", 0)
            rows.append(f"<li><strong>{html.escape(ticker)}</strong>: 30d bearish={r30:+.1f} / 7d bearish={r7:+.1f}</li>")
    rows_html = "".join(rows) if rows else ""
    return (
        f"<div class='urgent-banner' style='background:#FCEBEB;border:2px solid {color}'>"
        f"<span class='label' style='background:{color}'>{html.escape(label)}</span><br/>"
        f"<span class='text'>{html.escape(sub)}</span>"
        f"<ul style='margin:8px 0 0 0;color:#791F1F;font-size:13px'>{rows_html}</ul>"
        "</div>"
    )


def _build_eps_revisions_section(verdict: dict) -> str:
    """Detailed EPS Revisions Watch section — shown when signal approaches/fires."""
    parts = []
    parts.append(
        "<p style='font-size:12px;color:#5F5E5A;margin-top:0'>"
        "Weighted broker scoring: each action scored by direction × magnitude × "
        "authority tier (A=3×, B=2×, C=1×) × combo bonus (rating+PT+EPS aligned). "
        "Rolling sums tracked as <strong>bearish</strong> / <strong>bullish</strong> / <strong>net</strong> "
        "— trigger fires on <strong>bearish-only</strong> so recent bearish clusters aren't masked by older bullish history. "
        "<strong>Tier A</strong>: Mirae Asset, Goldman, Morgan Stanley, JPMorgan, Mizuho. "
        "<strong>Tier B</strong>: Samsung Securities, NH, KIS, Bernstein, Susquehanna, BofA, Citi, UBS. "
        "<strong>Tier C</strong>: all others (Korean Tier 2 brokers via Naver, smaller US shops)."
        "</p>"
    )
    # Per-ticker breakdown
    parts.append("<table style='font-size:12px'>")
    parts.append(
        "<thead><tr>"
        "<th style='width:12%'>Ticker</th>"
        "<th style='width:12%'>Fired</th>"
        "<th style='width:11%'>7d bearish</th>"
        "<th style='width:11%'>30d bearish</th>"
        "<th style='width:11%'>30d bullish</th>"
        "<th style='width:9%'>30d net</th>"
        "<th style='width:34%'>Top impact actions (30d)</th>"
        "</tr></thead><tbody>"
    )
    for ticker, td in verdict.get("per_ticker", {}).items():
        status = td.get("fired_status", "Not fired")
        r7 = td["rolling_7d"]
        r30 = td["rolling_30d"]
        r7_bearish = r7.get("score_bearish", 0)
        r30_bearish = r30.get("score_bearish", 0)
        r30_bullish = r30.get("score_bullish", 0)
        r30_net = r30.get("score_net", 0)
        top_actions = r30.get("top_impact_actions", [])
        top_html_parts = []
        for a in top_actions[:3]:
            broker = a.get("broker_canonical") or a.get("broker_raw_name") or "Unknown"
            tier_letter = a.get("tier", "C")
            score = a.get("score", 0)
            label = a.get("rating_change_label") or ""
            # Hint flag: Tier A US broker action without FY27 EPS extracted
            eps_change = a.get("fy27_eps_pct_change")
            is_us_action = a.get("source", "").lower().startswith(("finnhub", "bloomberg", "reuters"))
            hint = ""
            if tier_letter == "A" and is_us_action and eps_change is None and abs(score) > 5:
                hint = " <span style='color:#B36B00;font-size:10px'>⚠️ FY27 EPS not extracted (US broker, no PDF echo yet) — consider manual Bloomberg/Mirae check for fuller context</span>"
            top_html_parts.append(
                f"<div style='font-size:11px;margin-bottom:3px'>"
                f"<strong>{html.escape(broker)}</strong> "
                f"<span style='color:#888;font-size:10px'>(Tier {tier_letter})</span> "
                f"score={score:+.1f} {html.escape(label[:60])}{hint}"
                f"</div>"
            )
        top_html = "".join(top_html_parts) if top_html_parts else "<span style='color:#888'>No actions</span>"

        status_bg = {
            "FIRED": "#FCEBEB",
            "EARLY_WARNING": "#FAEEDA",
            "BULLISH_CONFIRM": "#EAF3DE",
            "Not fired": "#ffffff",
        }.get(status, "#ffffff")

        parts.append(
            f"<tr>"
            f"<td><strong>{html.escape(ticker)}</strong></td>"
            f"<td style='background:{status_bg};font-weight:600;text-align:center;font-size:11px'>{html.escape(status)}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:#A32D2D'>{r7_bearish:+.1f}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;font-weight:600;color:#A32D2D'>{r30_bearish:+.1f}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:#1D6F3D'>{r30_bullish:+.1f}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{r30_net:+.1f}</td>"
            f"<td>{top_html}</td>"
            f"</tr>"
        )
    parts.append("</tbody></table>")

    # Today's new actions
    new_actions = verdict.get("new_actions_today", [])
    if new_actions:
        parts.append("<h3 style='margin-top:18px'>New broker actions today</h3>")
        parts.append("<table style='font-size:11px'>")
        parts.append(
            "<thead><tr>"
            "<th>Broker</th><th>Tier</th><th>Ticker</th><th>Action</th>"
            "<th>PT change</th><th>FY27 EPS</th><th>Score</th><th>Source</th>"
            "</tr></thead><tbody>"
        )
        for a in sorted(new_actions, key=lambda x: -abs(x.get("score", 0)))[:10]:
            broker = a.get("broker_canonical") or a.get("broker_raw_name") or "?"
            pt_pct = a.get("target_price_pct_change")
            pt_str = f"{pt_pct:+.1f}%" if pt_pct is not None else "—"
            eps_pct = a.get("fy27_eps_pct_change")
            eps_str = f"{eps_pct:+.1f}%" if eps_pct is not None else "—"
            score = a.get("score", 0)
            score_color = "#A32D2D" if score < -3 else "#1D6F3D" if score > 3 else "#5F5E5A"
            parts.append(
                f"<tr>"
                f"<td>{html.escape(broker)}</td>"
                f"<td style='text-align:center'><strong>{html.escape(a.get('tier', 'C'))}</strong></td>"
                f"<td>{html.escape(a.get('ticker', '?'))}</td>"
                f"<td style='font-size:10px'>{html.escape(a.get('rating_change_label') or '—')}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{pt_str}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{eps_str}</td>"
                f"<td style='text-align:right;font-weight:600;color:{score_color}'>{score:+.1f}</td>"
                f"<td style='font-size:10px;color:#888'>{html.escape(a.get('source', '?'))}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")

    # v3.5.0 — dedup stats (informational footer)
    dedup = verdict.get("dedup_stats") or {}
    if dedup.get("echoes_suppressed", 0) > 0:
        parts.append(
            f"<p style='font-size:11px;color:#888;margin-top:6px'>"
            f"Event dedup (v3.5.0): {dedup.get('total_new_actions',0)} raw actions → "
            f"{dedup.get('actions_scored',0)} scored, "
            f"<strong>{dedup.get('echoes_suppressed',0)} echoes suppressed</strong> "
            f"(same earnings event, multiple sources counted once)."
            f"</p>"
        )

    # v3.5.0 — divergent events (signal-quality warning when sources disagree)
    divergent = verdict.get("divergent_events") or []
    if divergent:
        div_rows = ""
        for ev in divergent[:5]:
            ekey = ev.get("event_key", "?")
            primary = ev.get("primary_verdict", "?")
            n_obs = ev.get("n_observations", 1)
            n_div = sum(1 for o in ev.get("observations", [])
                        if o.get("verdict") and o.get("verdict") != primary)
            first_src = ev.get("first_observation_source", "?")
            div_rows += (
                f"<tr>"
                f"<td style='padding:3px 8px;font-size:11px'>{html.escape(ekey)}</td>"
                f"<td style='padding:3px 8px;font-size:11px'>{html.escape(primary)}</td>"
                f"<td style='padding:3px 8px;font-size:11px'>{n_obs} sources, {n_div} diverge</td>"
                f"<td style='padding:3px 8px;font-size:11px;color:#888'>{html.escape(first_src)}</td>"
                f"</tr>"
            )
        parts.append(
            f"<div style='margin-top:12px;padding:8px;background:#FFF7ED;"
            f"border-left:3px solid #D97706;border-radius:3px'>"
            f"<strong style='font-size:12px;color:#92400E'>"
            f"⚠ Divergent event coverage — sources disagree on outcome</strong>"
            f"<p style='font-size:11px;margin:4px 0'>One or more earnings events have been "
            f"covered with contradictory verdicts across sources (e.g. one broker PDF says "
            f"beat, another says miss). The primary verdict (first observed) was used for "
            f"scoring. Divergent sources were suppressed from the rolling sum but may "
            f"indicate data quality issues or a genuinely contested result — investigate.</p>"
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<tr style='background:#F5F5F5'>"
            f"<th style='padding:3px 8px;font-size:11px;text-align:left'>Event</th>"
            f"<th style='padding:3px 8px;font-size:11px;text-align:left'>Primary verdict</th>"
            f"<th style='padding:3px 8px;font-size:11px;text-align:left'>Coverage</th>"
            f"<th style='padding:3px 8px;font-size:11px;text-align:left'>First source</th>"
            f"</tr>"
            f"{div_rows}"
            f"</table>"
            f"</div>"
        )

    return "\n".join(parts)
