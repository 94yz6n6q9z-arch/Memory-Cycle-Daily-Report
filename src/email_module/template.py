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
  v3.38  (2026-05-17) — Added three new `_SOURCE_TYPE_LABELS` entries used by the deep tier diagnostics rows: "api" → "External API" (Finnhub, DART), "transcript" → "Earnings transcript source" (Yahoo / Motley Fool / IR scrapers), "broker_pdf" → "Korean broker note feed" (Mirae, NH, Samsung Securities). Without these, the new deep-tier rows would group under raw lowercase type-strings in the validation table.
"""

from __future__ import annotations

import html
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
    "Fired": "#D85A30",
    "Partially fired": "#FAC775",
    "Not fired": "#C0DD97",
    "Reversed": "#888780",
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
      .source-row-ok td { background: #fff; }
      .source-row-warn td { background: #FFFBF1; }
      .source-row-fail td { background: #FCF3F3; }
      .source-row-skipped td { background: #F6F4EE; }
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

    # 0. Urgent banner if flagged
    if es.urgent_attention_needed and es.urgent_attention_text:
        parts.append(
            f"<div class='urgent-banner'>"
            f"<span class='label'>⚠ URGENT — REQUIRES ATTENTION</span><br/>"
            f"<span class='text'>{html.escape(es.urgent_attention_text)}</span>"
            f"</div>"
        )

    # 0b. 🚨 Relative strength banner — fires when any of 3 sub-triggers breach
    if rel_strength_verdict and rel_strength_verdict.get("overall_fired"):
        parts.append(_build_relative_strength_banner(rel_strength_verdict))

    # 0c. 🚨 EPS Revisions Watch banner — fires when canonical Tier 2 signal moves
    if eps_scoring_verdict:
        overall = eps_scoring_verdict.get("overall_status", "Not fired")
        if overall in ("FIRED", "EARLY_WARNING"):
            parts.append(_build_eps_revisions_banner(eps_scoring_verdict))

    # 1. Executive summary in the requested format
    parts.append(_build_executive_summary(es))

    # 2. Big movers callout
    if analysis.big_movers:
        parts.append(_build_big_movers(analysis.big_movers))

    # Market summary
    if analysis.market_summary:
        parts.append(
            f"<div class='market-summary'><strong>Today's market:</strong> {html.escape(analysis.market_summary)}</div>"
        )

    # 3. Performance section
    parts.append("<h2>1. Performance overview</h2>")
    parts.append(
        f"<img class='chart' src='data:image/png;base64,{chart_base64}' alt='1Y performance chart' />"
    )
    parts.append(_build_performance_table(performance))

    # 4. Tier indicators
    parts.append("<h2>2. Tier indicators — daily status</h2>")
    parts.append(_build_indicators_section(analysis.indicator_assessments))

    # 5. Top 5 deep dives
    parts.append("<h2>3. Top 5 indicators — deep dive</h2>")
    parts.append(_build_top5_section(analysis.top_5_deep_dives))

    # 6. Latest news
    parts.append("<h2>4. Latest news from the memory & semi market</h2>")
    parts.append(_build_news_section(analysis.key_news_summary))

    # 6b. EPS Revisions Watch — detailed section when signal fired/approaching
    if eps_scoring_verdict and eps_scoring_verdict.get("overall_status") in ("FIRED", "EARLY_WARNING", "BULLISH_CONFIRM"):
        parts.append("<h2>4b. EPS Revisions Watch — t2_eps_revisions weighted scoring</h2>")
        parts.append(_build_eps_revisions_section(eps_scoring_verdict))

    # 7. Calendar of events (next 90 days)
    if upcoming_events:
        parts.append("<h2>5. Calendar of events — next 90 days</h2>")
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
        parts.append("<h2>6. Data source assessment</h2>")
        parts.append(
            "<p style='font-size:12px;color:#5F5E5A;margin-top:0'>"
            "Per-source diagnostics for this run. Written confirmation that each configured "
            "data source was reached, read, and parsed — guards against silent failures where "
            "the brief looks healthy but the underlying feed has been broken for weeks. "
            "<strong>OK</strong> = reached and returned items. "
            "<strong>WARN</strong> = reached but returned 0 items (could be legitimately "
            "empty, or a parsing issue). "
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

    # Trigger event line
    if es.new_triggers_today:
        trigger_lines = []
        for trig in es.new_triggers_today:
            trigger_lines.append(
                f"<div class='new-trigger'>"
                f"<strong>New trigger event on Tier {trig.get('tier', '?')}:</strong> "
                f"{html.escape(trig.get('indicator_name', ''))} — {html.escape(trig.get('description', ''))}"
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

    delta_line = (
        f"<div class='delta-text'>"
        f"{html.escape(es.delta_from_prior_text)}"
        f"</div>"
    )

    return (
        f"<div class='exec-summary'>"
        f"<h2>Executive summary</h2>"
        f"{phase_text}"
        f"{trigger_text}"
        f"{peak_line}"
        f"{cases_line}"
        f"{delta_line}"
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


def _build_indicators_section(assessments: list[IndicatorAssessment]) -> str:
    """Build the four-tier indicator status tables."""
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
            change_flag = ""
            if a.change_vs_prior:
                change_flag = f"<span class='change-flag'>CHANGED: {html.escape(a.change_vs_prior[:80])}</span>"

            links_html = ""
            if a.relevant_links:
                link_strs = [
                    f"<a href='{html.escape(l.get('url', '#'))}' target='_blank'>{html.escape(l.get('title', 'link')[:60])}</a>"
                    for l in a.relevant_links[:3]
                ]
                links_html = f"<div class='links-list' style='margin-top:6px'>{' · '.join(link_strs)}</div>"

            parts.append(
                "<tr>"
                f"<td class='indicator-cell'><strong>{html.escape(a.name)}</strong><br/><span style='color:#888;font-size:11px'>{html.escape(a.description[:120])}</span></td>"
                f"<td class='indicator-cell'>{html.escape(a.what_to_look_for)}</td>"
                f"<td class='indicator-cell'><span class='status-badge' style='background:{status_color}'>{html.escape(a.current_status)}</span>{change_flag}</td>"
                f"<td class='indicator-cell'>{html.escape(a.where_we_stand)}</td>"
                f"<td class='indicator-cell'>{a.latest_developments}{links_html}</td>"
                "</tr>"
            )

        parts.append("</tbody></table>")

    return "\n".join(parts)


def _build_top5_section(top5: list[IndicatorAssessment]) -> str:
    """Deeper write-up for the top 5 indicators."""
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
    """
    # Summary pills
    ok_count = sum(1 for s in statuses if s.health == "ok")
    warn_count = sum(1 for s in statuses if s.health == "warn")
    fail_count = sum(1 for s in statuses if s.health == "fail")
    skipped_count = sum(1 for s in statuses if s.health == "skipped")
    total = len(statuses)

    pills = (
        f"<span class='validation-pill validation-ok'>OK {ok_count}</span>"
        f"<span class='validation-pill validation-warn'>WARN {warn_count}</span>"
        f"<span class='validation-pill validation-fail'>FAIL {fail_count}</span>"
    )
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

    # Sort within each group: fail first, warn, ok, then skipped at the bottom
    health_order = {"fail": 0, "warn": 1, "ok": 2, "skipped": 3}
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
    }
    badge_label_map = {
        "ok": "OK",
        "warn": "WARN",
        "fail": "FAIL",
        "skipped": "SKIPPED",
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
