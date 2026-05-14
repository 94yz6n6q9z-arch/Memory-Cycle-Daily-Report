"""
Memory Cycle Daily Brief - main orchestrator.

Pipeline:
1. Fetch price/performance data for memory players + benchmarks
2. Identify big movers (>+/-4% in 1D)
3. Fetch analyst estimates focused on FY27/FY28
4. Scrape news from RSS, EDGAR, Google News, ChosunBiz, TrendForce
5. Load prior state for change tracking
6. Stage 1 (Haiku): triage news, extract broker actions, summarize
7. Stage 2 (Sonnet): tier-by-tier analysis + executive summary with probability deltas
8. Generate price chart
9. Build HTML email (with data-source validation table at the bottom)
10. Send via Gmail SMTP
11. Persist today's state for tomorrow's change tracking

Run via: python -m src.main
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.config import (
    ANTHROPIC_API_KEY,
    BENCHMARKS,
    BIG_MOVER_THRESHOLD_PCT,
    CORE_TICKERS,
    EDGAR_FEEDS,
    EMAIL_TO,
    FMP_API_KEY,
    GOOGLE_NEWS_QUERIES,
    HYPERSCALERS,
    RSS_FEEDS,
    WIDER_BASKET,
    get_upcoming_events,
)
from src.diagnostics import DiagnosticsCollector
from src.data.prices import (
    baseline_to_100,
    calculate_performance,
    fetch_price_history,
)
from src.data.estimates import fetch_all_estimates
from src.data.news import fetch_all_news
from src.analysis.llm_summarizer import (
    analyze_with_llm,
    fallback_analysis,
    identify_big_movers,
)
from src.analysis.chart_generator import generate_performance_chart
from src.email_module.template import build_email_html
from src.email_module.sender import send_email


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("memory_cycle_brief")


STATE_DIR = Path("./state")
STATE_FILE = STATE_DIR / "prior_state.json"


def load_prior_state() -> dict:
    """Load yesterday's full state (assessments + executive summary) for change tracking."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load prior state: {e}")
        return {}


def save_state(analysis) -> None:
    """Persist today's state for tomorrow's change tracking."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "executive_summary": {
            "current_phase": analysis.executive_summary.current_phase,
            "tier_status_summary": analysis.executive_summary.tier_status_summary,
            "base_case": asdict(analysis.executive_summary.base_case),
            "bull_case": asdict(analysis.executive_summary.bull_case),
            "bear_case": asdict(analysis.executive_summary.bear_case),
        },
        "indicator_assessments": {
            a.id: {"status": a.current_status, "where_we_stand": a.where_we_stand}
            for a in analysis.indicator_assessments
        },
    }

    try:
        with open(STATE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"Saved state to {STATE_FILE}")
    except Exception as e:
        logger.warning(f"Could not save state: {e}")


def main() -> int:
    logger.info("=" * 60)
    logger.info("Memory Cycle Daily Brief - starting pipeline")
    logger.info("=" * 60)

    # ------------------------------------------------------------
    # 0. Initialize diagnostics collector — passed to every fetch
    #    so the email's validation table can confirm each source
    #    was actually reached and parsed.
    # ------------------------------------------------------------
    collector = DiagnosticsCollector()

    # ------------------------------------------------------------
    # 1. Fetch price/performance data
    # ------------------------------------------------------------
    logger.info("Fetching price history for wider basket + benchmarks...")
    chart_tickers = {**CORE_TICKERS, **BENCHMARKS}
    perf_tickers = {**WIDER_BASKET, **BENCHMARKS}

    try:
        history = fetch_price_history(
            perf_tickers, period="1y", interval="1d", collector=collector,
        )
        logger.info(f"Fetched price history: {history.shape[0]} days x {history.shape[1]} tickers")
    except Exception as e:
        logger.error(f"Critical: failed to fetch price data: {e}")
        return 1

    performance = calculate_performance(history, perf_tickers)
    logger.info(f"Calculated performance for {len(performance)} tickers")

    # ------------------------------------------------------------
    # 2. Identify big movers (>+/-4% 1D)
    # ------------------------------------------------------------
    big_movers = identify_big_movers(performance, threshold_pct=BIG_MOVER_THRESHOLD_PCT)
    if big_movers:
        logger.info(f"Identified {len(big_movers)} big movers:")
        for m in big_movers:
            logger.info(f"  {m.name} ({m.ticker}): {m.change_1d_pct:+.2f}%")
    else:
        logger.info("No big movers (>+/-4%) today")

    # Chart uses only core tickers + SOX for cleanliness
    chart_history = history[[c for c in history.columns if c in chart_tickers]]
    chart_baseline = baseline_to_100(chart_history)
    chart_b64 = generate_performance_chart(chart_baseline)
    logger.info("Generated performance chart")

    # ------------------------------------------------------------
    # 3. Fetch analyst estimates
    # ------------------------------------------------------------
    logger.info("Fetching analyst estimates (FY27/FY28 focus)...")
    estimates = fetch_all_estimates(
        WIDER_BASKET,
        fmp_api_key=FMP_API_KEY or None,
        collector=collector,
    )
    logger.info(f"Fetched estimates for {len(estimates)} tickers")

    # ------------------------------------------------------------
    # 4. Fetch news from all sources (RSS + EDGAR + Google News + scrapers)
    # ------------------------------------------------------------
    logger.info("Fetching news from RSS + EDGAR + Google News + scrapers...")
    news_items = fetch_all_news(
        RSS_FEEDS,
        GOOGLE_NEWS_QUERIES,
        hours_back=18,
        edgar_feeds=EDGAR_FEEDS,
        collector=collector,
    )
    logger.info(f"Fetched {len(news_items)} unique news items")

    # ------------------------------------------------------------
    # 5. Load prior state
    # ------------------------------------------------------------
    prior_state = load_prior_state()
    if prior_state:
        logger.info(
            f"Loaded prior state from {prior_state.get('timestamp', '?')} "
            f"({len(prior_state.get('indicator_assessments', {}))} indicators)"
        )
    else:
        logger.info("No prior state found - this is the first run")

    # ------------------------------------------------------------
    # 6-7. Two-stage LLM analysis (Haiku triage -> Sonnet analysis)
    # ------------------------------------------------------------
    logger.info("Running two-stage LLM analysis (Haiku -> Sonnet)...")
    analysis = analyze_with_llm(
        api_key=ANTHROPIC_API_KEY,
        performance_data=performance,
        estimates_data=estimates,
        news_items=news_items,
        big_movers=big_movers,
        prior_state=prior_state,
        collector=collector,
    )
    if analysis is None:
        logger.warning("Falling back to deterministic analysis")
        analysis = fallback_analysis(performance, estimates, news_items, big_movers)

    # ------------------------------------------------------------
    # Diagnostics summary log (also rendered in the email)
    # ------------------------------------------------------------
    counts = collector.summary_counts()
    logger.info(
        f"Data source diagnostics: {counts['ok']} ok | {counts['warn']} warn (0 items) "
        f"| {counts['fail']} failed | {counts.get('skipped', 0)} skipped "
        f"| {counts['total']} total sources"
    )
    for s in collector.all():
        if s.health == "fail":
            logger.warning(f"  FAIL: {s.source_name} — {s.error}")
        elif s.health == "warn":
            logger.info(f"  WARN: {s.source_name} — reached but 0 items")
        elif s.health == "skipped":
            logger.info(f"  SKIPPED: {s.source_name} — {s.detail}")

    # ------------------------------------------------------------
    # 8-9. Build and send email
    # ------------------------------------------------------------
    logger.info("Building HTML email...")
    upcoming = get_upcoming_events(within_days=90)
    logger.info(f"Calendar: {len(upcoming)} upcoming events in next 90 days")
    html_body = build_email_html(
        analysis=analysis,
        performance=performance,
        chart_base64=chart_b64,
        data_source_statuses=collector.all(),
        upcoming_events=upcoming,
    )

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    urgent_prefix = "🚨 " if analysis.executive_summary.urgent_attention_needed else ""
    subject = f"{urgent_prefix}Memory Cycle Brief — {today_str}"

    logger.info(f"Sending email to {EMAIL_TO}...")
    success = send_email(subject, html_body)
    if not success:
        logger.error("Email send failed; check SMTP credentials and recipient")
        return 2

    # ------------------------------------------------------------
    # 10. Persist for change tracking
    # ------------------------------------------------------------
    save_state(analysis)

    logger.info("Pipeline complete ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
