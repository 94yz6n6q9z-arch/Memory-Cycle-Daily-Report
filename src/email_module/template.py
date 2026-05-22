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
    # trajectory chart rendered by chart_generator. When both are None
    # (DHCR YAML missing or computation failed), the indicator simply
    # renders Sonnet's fallback prose like any other indicator — the
    # framework degrades gracefully.
    dhcr_snapshot: Optional[dict] = None,
    dhcr_chart_b64: Optional[str] = None,
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

    # 0b. 🚨 Relative strength banner — fires when any of 3 sub-triggers breach
    if rel_strength_verdict and rel_strength_verdict.get("overall_fired"):
        parts.append(_build_relative_strength_banner(rel_strength_verdict))

    # 0c. 🚨 EPS Revisions Watch banner — fires when canonical Tier 2 signal moves
    if eps_scoring_verdict:
        overall = eps_scoring_verdict.get("overall_status", "Not fired")
        if overall in ("FIRED", "EARLY_WARNING"):
            parts.append(_build_eps_revisions_banner(eps_scoring_verdict))

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

    # 6. Top 5 deep dives (section 4 — includes cycle matrix card under
    # t1_spot_decline per v3.39.7.5 placement decision)
    parts.append("<h2 id='section-4'>4. Top 5 indicators — deep dive</h2>")
    parts.append(_build_top5_section(
        analysis.top_5_deep_dives,
        cycle_verdict=cycle_verdict,
        cycle_matrix_chart_b64=cycle_matrix_chart_b64,
        spot_contract_chart_b64=spot_contract_chart_b64,
    ))

    # v3.4.0 — DHCR section 4 deep-dive card. Rendered as its own card
    # block (not in top_5_deep_dives because DHCR isn't a per-indicator
    # status — it's a direction monitor with computed numbers + chart).
    # Always renders when dhcr_snapshot is provided, regardless of whether
    # it's in top_5_deep_dives.
    if dhcr_snapshot:
        parts.append(_build_dhcr_section4_card(dhcr_snapshot, chart_b64=dhcr_chart_b64))

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

    peak_line = (
        f"<div class='exec-line' style='margin-top:14px'>"
        f"Based on latest information, our <strong>base case "
        f"(prob {base.probability_pct:.0f}%)</strong> estimates the peak is "
        f"<strong>{base.peak_months_away:.0f} months away</strong>, "
        f"placing it between <strong>{html.escape(base.peak_window_start)}</strong> "
        f"and <strong>{html.escape(base.peak_window_end)}</strong>. "
        f"On our base case the {_ret_label(base.return_to_peak_pct)} to peak is estimated at "
        f"<strong>{base.return_to_peak_pct:+.0f}%</strong>."
        f"</div>"
    )

    cases_line = (
        f"<div class='exec-line'>"
        f"<span class='case-block case-bear'>Bear case ({bear.probability_pct:.0f}%): "
        f"peak in {bear.peak_months_away:.0f}mo, "
        f"{_ret_label(bear.return_to_peak_pct)} {bear.return_to_peak_pct:+.0f}%</span>"
        f"<span class='case-block case-base'>Base case ({base.probability_pct:.0f}%): "
        f"peak in {base.peak_months_away:.0f}mo, "
        f"{_ret_label(base.return_to_peak_pct)} {base.return_to_peak_pct:+.0f}%</span>"
        f"<span class='case-block case-bull'>Bull case ({bull.probability_pct:.0f}%): "
        f"peak in {bull.peak_months_away:.0f}mo, "
        f"{_ret_label(bull.return_to_peak_pct)} {bull.return_to_peak_pct:+.0f}%</span>"
        f"</div>"
    )

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


def _build_dhcr_section3_snippet(dhcr: Optional[dict]) -> str:
    """v3.4.0 — section 3 summary card for t1_decode_hbm_captive_ratio.

    Compact direction-monitor representation: shows the composite direction
    (5-level scale), the key driver numbers (KV efficiency YoY, HBM-per-
    accelerator YoY, enterprise revenue YoY), and the implied context
    capacity per accelerator. Color-coded by direction.

    Returns "" if dhcr is None (DHCR computation failed or YAML missing) —
    section 3 row falls back to pure Sonnet prose.
    """
    if not dhcr:
        return ""

    direction = dhcr.get("direction", "balanced")
    direction_colors = {
        "strongly_bullish_hbm": "#2C7A2C",  # deep green
        "mildly_bullish_hbm": "#C0DD97",
        "balanced": "#CCCCCC",
        "mildly_bearish_hbm": "#FAC775",
        "strongly_bearish_hbm": "#B91C1C",
    }
    direction_labels = {
        "strongly_bullish_hbm": "STRONGLY BULLISH HBM",
        "mildly_bullish_hbm": "Mildly bullish HBM",
        "balanced": "Balanced",
        "mildly_bearish_hbm": "Mildly bearish HBM",
        "strongly_bearish_hbm": "STRONGLY BEARISH HBM",
    }
    direction_arrows = {
        "strongly_bullish_hbm": "↑↑",
        "mildly_bullish_hbm": "↑",
        "balanced": "→",
        "mildly_bearish_hbm": "↓",
        "strongly_bearish_hbm": "↓↓",
    }

    color = direction_colors.get(direction, "#CCCCCC")
    label = direction_labels.get(direction, direction)
    arrow = direction_arrows.get(direction, "→")

    # Direction badge (top of cell)
    text_color = "#FFFFFF" if direction == "strongly_bearish_hbm" else "#1a1a1a"
    direction_badge = (
        f"<div style='margin-bottom:6px'>"
        f"<span style='display:inline-block;background:{color};color:{text_color};"
        f"padding:3px 10px;border-radius:3px;font-weight:700;font-size:12px'>"
        f"{arrow} {label}</span>"
        f"</div>"
    )

    # Key driver lines
    kv = dhcr.get("frontier_kv_kb_per_token", 0)
    kv_yoy = dhcr.get("kv_yoy_change_pct")
    hbm = dhcr.get("frontier_hbm_gb_per_accelerator", 0)
    hbm_yoy = dhcr.get("hbm_per_accelerator_yoy_change_pct")
    cap_yoy = dhcr.get("capacity_creation_yoy_pct")
    ent_yoy = dhcr.get("enterprise_ai_yoy_growth_pct")
    nv_yoy = dhcr.get("nvidia_datacenter_yoy_growth_pct")
    cap_tokens = dhcr.get("implied_context_capacity_per_accelerator_tokens", 0) or 0

    if cap_tokens >= 1_000_000:
        cap_s = f"{cap_tokens / 1_000_000:.1f}M"
    elif cap_tokens >= 1_000:
        cap_s = f"{cap_tokens / 1_000:.0f}K"
    else:
        cap_s = str(cap_tokens)

    def _fmt_pct(v):
        if v is None:
            return "n/a"
        return f"{v:+.0f}%"

    drivers_html = (
        "<div style='font-size:11px;color:#444;line-height:1.4'>"
        f"<div><strong>Frontier KV:</strong> {kv:.0f} KB/tok "
        f"(YoY {_fmt_pct(kv_yoy)}) · "
        f"<strong>HBM/accel:</strong> {hbm:.0f} GB (YoY {_fmt_pct(hbm_yoy)})</div>"
        f"<div><strong>Implied capacity:</strong> ~{cap_s} tokens per accelerator</div>"
        f"<div><strong>Capacity-creation YoY:</strong> {_fmt_pct(cap_yoy)} · "
        f"<strong>Enterprise AI YoY:</strong> {_fmt_pct(ent_yoy)} · "
        f"<strong>NVIDIA DC YoY:</strong> {_fmt_pct(nv_yoy)}</div>"
        "</div>"
    )

    return f"<div style='margin-bottom:8px'>{direction_badge}{drivers_html}</div>"


def _build_dhcr_section4_card(dhcr: Optional[dict], chart_b64: Optional[str] = None) -> str:
    """v3.4.0 — section 4 deep-dive card for DHCR.

    Renders the full DHCR snapshot including: composite direction reading,
    the matrix-based visualization (efficiency × HBM-capacity), the YoY
    trend numbers for all three drivers, and the substitution-risk flags.
    Pulls Sonnet's narrative paragraph from dhcr.narrative_summary.
    """
    if not dhcr:
        return ""

    direction = dhcr.get("direction", "balanced")
    direction_colors = {
        "strongly_bullish_hbm": "#2C7A2C",
        "mildly_bullish_hbm": "#C0DD97",
        "balanced": "#CCCCCC",
        "mildly_bearish_hbm": "#FAC775",
        "strongly_bearish_hbm": "#B91C1C",
    }
    color = direction_colors.get(direction, "#CCCCCC")
    text_color = "#FFFFFF" if direction == "strongly_bearish_hbm" else "#1a1a1a"

    kv = dhcr.get("frontier_kv_kb_per_token", 0)
    model = dhcr.get("frontier_model_name", "?")
    arch = dhcr.get("frontier_model_arch", "?")
    hbm = dhcr.get("frontier_hbm_gb_per_accelerator", 0)
    accel = dhcr.get("frontier_accelerator_name", "?")
    cap_tokens = dhcr.get("implied_context_capacity_per_accelerator_tokens", 0) or 0
    kv_yoy = dhcr.get("kv_yoy_change_pct") or 0
    hbm_yoy = dhcr.get("hbm_per_accelerator_yoy_change_pct") or 0
    cap_yoy = dhcr.get("capacity_creation_yoy_pct") or 0
    ent_yoy = dhcr.get("enterprise_ai_yoy_growth_pct") or 0
    nv_yoy = dhcr.get("nvidia_datacenter_yoy_growth_pct") or 0
    capex_yoy = dhcr.get("hyperscaler_capex_yoy_growth_pct") or 0
    ent_rr = dhcr.get("enterprise_ai_aggregate_run_rate_usd_b", 0)
    narrative = dhcr.get("narrative_summary", "")
    dominant = dhcr.get("dominant_force", "")
    confidence = dhcr.get("confidence", "?")
    subst = dhcr.get("substitution_risk_notes") or []

    if cap_tokens >= 1_000_000:
        cap_s = f"{cap_tokens / 1_000_000:.1f}M"
    elif cap_tokens >= 1_000:
        cap_s = f"{cap_tokens / 1_000:.0f}K"
    else:
        cap_s = str(cap_tokens)

    direction_label = direction.replace("_", " ").upper()

    parts = []
    parts.append(
        "<div style='margin:24px 0;border:2px solid #555;background:#FAFAFA;"
        "padding:18px;border-radius:5px'>"
    )
    parts.append(
        f"<h3 style='margin:0 0 12px 0;color:#1a1a1a'>"
        f"Decode HBM Captive Ratio — direction monitor "
        f"<span style='font-size:12px;color:#888;font-weight:normal'>(new v3.4.0)</span>"
        f"</h3>"
    )
    parts.append(
        f"<div style='margin-bottom:12px'>"
        f"<span style='background:{color};color:{text_color};padding:6px 14px;"
        f"border-radius:4px;font-weight:700;font-size:14px'>"
        f"{direction_label}</span>"
        f"<span style='margin-left:12px;color:#666;font-size:12px'>"
        f"confidence: {confidence} · {dominant}</span>"
        f"</div>"
    )

    # Two-column layout: supply (left), demand (right)
    parts.append("<table style='width:100%;border-collapse:collapse;margin-bottom:14px'><tr>")

    # Supply side
    parts.append(
        "<td style='width:50%;vertical-align:top;padding-right:14px;"
        "border-right:1px solid #DDD'>"
        "<div style='font-weight:600;color:#1a1a1a;margin-bottom:6px;font-size:13px'>"
        "SUPPLY-SIDE: capacity creation rate</div>"
        f"<div style='font-size:12px;color:#333;line-height:1.55'>"
        f"<strong>Frontier KV efficiency:</strong> {kv:.0f} KB/tok<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"{html.escape(model)} ({arch})</span><br/>"
        f"&nbsp;&nbsp;YoY: {kv_yoy:+.0f}% (negative = efficiency improving)<br/><br/>"
        f"<strong>Frontier HBM-per-accelerator:</strong> {hbm:.0f} GB<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"{html.escape(accel)}</span><br/>"
        f"&nbsp;&nbsp;YoY: {hbm_yoy:+.0f}%<br/><br/>"
        f"<strong>Composite capacity-creation rate:</strong> "
        f"<span style='font-size:14px;font-weight:600'>{cap_yoy:+.0f}% YoY</span><br/><br/>"
        f"<strong>Implied context-capacity frontier:</strong><br/>"
        f"&nbsp;&nbsp;~<span style='font-size:18px;font-weight:700;color:#0F6E56'>"
        f"{cap_s}</span> tokens per accelerator<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"(at frontier KV × frontier HBM-per-accel)</span>"
        f"</div>"
        "</td>"
    )

    # Demand side
    parts.append(
        "<td style='width:50%;vertical-align:top;padding-left:14px'>"
        "<div style='font-weight:600;color:#1a1a1a;margin-bottom:6px;font-size:13px'>"
        "DEMAND-PULL: enterprise AI revenue</div>"
        f"<div style='font-size:12px;color:#333;line-height:1.55'>"
        f"<strong>Aggregate run-rate:</strong> "
        f"${ent_rr:.0f}B annualized<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"MSFT Azure AI + AWS + GCP + Oracle AI infra</span><br/><br/>"
        f"<strong>Weighted enterprise YoY:</strong> "
        f"<span style='font-size:14px;font-weight:600'>{ent_yoy:+.0f}%</span><br/><br/>"
        f"<strong>NVIDIA datacenter YoY:</strong> {nv_yoy:+.0f}%<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"(proxy for total AI accelerator demand)</span><br/><br/>"
        f"<strong>Hyperscaler capex YoY:</strong> {capex_yoy:+.0f}%<br/>"
        f"&nbsp;&nbsp;<span style='color:#888;font-size:11px'>"
        f"(MSFT + GOOGL + AMZN + META)</span>"
        f"</div>"
        "</td>"
    )

    parts.append("</tr></table>")

    # Chart if available
    if chart_b64:
        parts.append(
            f"<img src='data:image/png;base64,{chart_b64}' "
            f"style='width:100%;max-width:720px;margin:8px 0' "
            f"alt='DHCR trajectory chart — capacity creation vs enterprise demand growth' />"
        )

    # Narrative
    if narrative:
        parts.append(
            f"<div style='background:#FFFFFF;padding:10px 12px;border-left:3px solid {color};"
            f"margin:10px 0;font-size:12px;color:#333;line-height:1.5'>"
            f"<strong style='color:#1a1a1a'>Read:</strong> "
            f"{html.escape(narrative)}"
            f"</div>"
        )

    # Substitution risk
    if subst:
        parts.append(
            "<div style='margin-top:10px;font-size:11px;color:#666'>"
            "<strong style='color:#1a1a1a'>HBM-substitution risk flags "
            "(non-HBM accelerators):</strong><ul style='margin:4px 0 0 0;padding-left:20px'>"
        )
        for note in subst[:5]:
            parts.append(f"<li>{html.escape(note)}</li>")
        parts.append("</ul></div>")

    # Mental model footer
    parts.append(
        "<div style='margin-top:14px;padding-top:10px;border-top:1px solid #DDD;"
        "font-size:11px;color:#888;line-height:1.4'>"
        "<strong>Mental model:</strong> Efficiency gains don't displace HBM — they "
        "UNLOCK economic context capacity. Enterprise workloads then expand to "
        "consume what's economically possible. The binding question is whether "
        "enterprise demand grows fast enough to fill the unlocked headroom. "
        "Today's read: demand-pull running ahead of capacity creation by "
        f"~{ent_yoy - cap_yoy:.0f}pp YoY — HBM stays tight (bullish SK Hynix). "
        "Watch for enterprise YoY decelerating below capacity-creation rate.</div>"
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

    v3.4.0 — dhcr_snapshot param added. For t1_decode_hbm_captive_ratio,
    the "Where we stand" cell renders the direction-monitor snippet
    (composite direction + YoY drivers + implied capacity) instead of
    Sonnet's prose. This is the only direction-monitor indicator in the
    framework — its read is the computed numbers, not narrative.
    """
    # v3.39.7.9 — pre-build the t3_relative_strength computed snippet once
    # so the per-row loop can drop it in cheaply.
    rs_snippet = _build_relative_strength_snippet(rel_strength_verdict)
    # v3.4.0 — same pattern for DHCR
    dhcr_snippet = _build_dhcr_section3_snippet(dhcr_snapshot)

    parts = []
    for tier in (1, 2, 3, 4):
        tier_assessments = [a for a in assessments if a.tier == tier]
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
            elif a.id == "t1_decode_hbm_captive_ratio" and dhcr_snippet:
                # v3.4.0 — DHCR direction monitor. Snippet shows the
                # composite direction + key driver YoY rates + implied
                # context capacity. Replaces (not prepends to) Sonnet's
                # prose because the direction monitor IS the read — Sonnet's
                # text would be redundant with the computed numbers shown.
                where_we_stand_html = dhcr_snippet

            parts.append(
                "<tr>"
                f"<td class='indicator-cell'><strong>{html.escape(a.name)}</strong><br/><span style='color:#888;font-size:11px'>{html.escape(a.description[:120])}</span></td>"
                f"<td class='indicator-cell'>{html.escape(a.what_to_look_for)}</td>"
                f"<td class='indicator-cell'><span class='status-badge' style='background:{status_color}'>{html.escape(a.current_status)}</span>{change_flag}</td>"
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
        parts.append(
            f"<h3>{html.escape(a.name)} "
            f"<span class='status-badge' style='background:{status_color};margin-left:8px'>{html.escape(a.current_status)}</span></h3>"
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

    return "\n".join(parts)
