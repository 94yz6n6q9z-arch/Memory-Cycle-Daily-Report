"""
Fast tier entry point — runs 11:00 / 11:30 / 12:05 UTC weekdays.

Idempotent: first successful run of the day marks brief_sent_dates.json
and subsequent triggers exit early. Brief lands 13:00 CEST / 12:00 CET,
which is 1+ hour before US open even in summer DST.

Pipeline:
  0. Idempotency check — exit if already sent today
  1. Concurrent fast-source fetch:
     - RSS feeds (17), EDGAR atom feeds (6), Google News queries (17)
     - Naver Finance scraper (★ Korean broker structured aggregation)
     - Finnhub API (US analyst rating actions)
     - yfinance prices + estimates + computed metrics
     - DART API (Korean material disclosures)
  2. Load Layer 1 digests from state/digests/ (deep tier output)
  3. Layer 2: per-signal reasoning over all evidence (22 indicators × 1 Haiku call each)
  4. Layer 3: Sonnet final synthesis — exec summary, brief composition
  5. Chart generation + email build + send
  6. Mark idempotency + commit prior state for tomorrow

Failure modes — see src/state.py for retry/cache policy. Critical sources
(yfinance, Anthropic, Naver) trigger 🚨 banner on full failure; important
sources use cached payload tagged STALE; nice-to-have just skip.

CHANGELOG (this file):
  v3.31  (2026-05-14) — Added _surface_transcript_status() — commentary availability shown in email validation table
  v3.2   (2026-05-12) — Passes TRACKED_CONCEPTS to relevance filter
  v3.1   (2026-05-12) — Wired relevance filter as Step 1b (post-fetch, pre-digest-load)
"""

from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.config import (
    ANTHROPIC_API_KEY,
    BENCHMARKS,
    BIG_MOVER_THRESHOLD_PCT,
    CORE_TICKERS,
    EDGAR_FEEDS,
    EMAIL_TO,
    FMP_API_KEY,
    GOOGLE_NEWS_QUERIES,
    HAIKU_MODEL,
    HYPERSCALERS,
    INDICATORS,
    RSS_FEEDS,
    TRACKED_ANALYSTS,
    TRACKED_BROKERS,
    TRACKED_COMPANIES,
    TRACKED_CONCEPTS,
    WIDER_BASKET,
    get_upcoming_events,
)
from src.state import (
    has_brief_been_sent_today,
    mark_brief_sent_today,
    read_digests_in_window,
    fetch_with_retry,
    Criticality,
    PRIOR_STATE_FILE,
    STATE_DIR,
)
from src.diagnostics import DiagnosticsCollector, SourceFetchStatus
from src.data.prices import (
    baseline_to_100,
    calculate_performance,
    fetch_price_history,
    evaluate_relative_strength_signal,
)
from src.data.estimates import fetch_all_estimates, fetch_all_hyperscaler_capex
from src.data.news import fetch_all_news, filter_relevant_news
from src.data.naver_finance import fetch_naver_with_retry
from src.data.finnhub import fetch_finnhub_with_retry
from src.data.dart import fetch_dart_with_retry
from src.config import HYPERSCALERS
from src.analysis.signal_reasoner import reason_all_signals
from src.analysis.llm_summarizer import (
    analyze_with_llm,
    fallback_analysis,
    identify_big_movers,
)
from src.analysis.chart_generator import generate_performance_chart
from src.email_module.template import build_email_html
from src.email_module.sender import send_email

import anthropic


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("memory_cycle_brief.fast_tier")


def load_prior_state() -> dict:
    if not PRIOR_STATE_FILE.exists():
        return {}
    try:
        with open(PRIOR_STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load prior state: {e}")
        return {}


def save_state(analysis) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    try:
        with open(PRIOR_STATE_FILE, "w") as f:
            payload = {
                "saved_at_iso": datetime.utcnow().isoformat(),
                "executive_summary": asdict(analysis.executive_summary) if hasattr(analysis, "executive_summary") else {},
                "indicator_assessments": [asdict(a) for a in (analysis.indicator_assessments or [])] if hasattr(analysis, "indicator_assessments") else [],
            }
            json.dump(payload, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Could not persist prior state: {e}")


def run_fast_tier() -> int:
    """Execute the fast-tier pipeline. Returns exit code (0 = success)."""
    today = date.today()
    started_at = datetime.utcnow()

    # ----- Step 0: Idempotency check -----
    if has_brief_been_sent_today(today):
        logger.info(f"Brief already sent today ({today.isoformat()}). Exiting early — no-op.")
        return 0

    logger.info(f"=== FAST TIER START — {today.isoformat()} {started_at.strftime('%H:%M')} UTC ===")

    collector = DiagnosticsCollector()

    # ----- Step 1: Concurrent fast-source fetch -----
    logger.info("Fetching from concurrent source pool...")
    fetch_results: dict[str, Any] = {}

    def _run(label, fn):
        try:
            return label, fn()
        except Exception as e:
            logger.error(f"{label} fetch raised: {e}")
            return label, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_run, "prices", lambda: fetch_price_history(WIDER_BASKET, collector=collector)),
            pool.submit(_run, "estimates", lambda: fetch_all_estimates(CORE_TICKERS, fmp_api_key=FMP_API_KEY, collector=collector)),
            pool.submit(_run, "news", lambda: fetch_all_news(RSS_FEEDS, GOOGLE_NEWS_QUERIES, EDGAR_FEEDS, collector=collector)),
            pool.submit(_run, "naver", fetch_naver_with_retry),
            pool.submit(_run, "finnhub", fetch_finnhub_with_retry),
            pool.submit(_run, "dart", fetch_dart_with_retry),
        ]
        for fut in as_completed(futures):
            label, result = fut.result()
            fetch_results[label] = result

    price_history = fetch_results.get("prices") or {}
    estimates_map = fetch_results.get("estimates") or {}
    news_items = fetch_results.get("news") or []
    naver_data = fetch_results.get("naver", (None, {}))[0] if isinstance(fetch_results.get("naver"), tuple) else fetch_results.get("naver")
    finnhub_data = fetch_results.get("finnhub", (None, {}))[0] if isinstance(fetch_results.get("finnhub"), tuple) else fetch_results.get("finnhub")
    dart_data = fetch_results.get("dart", (None, {}))[0] if isinstance(fetch_results.get("dart"), tuple) else fetch_results.get("dart")

    # Record source fetches in collector
    if naver_data:
        collector.add(SourceFetchStatus("Naver Finance", "scraper", True,
                                        sum(t.get("n_actions", 0) for t in naver_data.get("tickers", {}).values()),
                                        0, detail=f"{len(naver_data.get('tickers', {}))} tickers"))
    if finnhub_data:
        collector.add(SourceFetchStatus("Finnhub", "api", True,
                                        len(finnhub_data.get("tickers", {})), 0))
    if dart_data:
        collector.add(SourceFetchStatus("DART API", "api", True,
                                        sum(t.get("n_disclosures", 0) for t in dart_data.get("tickers", {}).values()),
                                        0))

    # ----- Step 1b: Relevance filter on news (keyword + Haiku) -----
    # Two-pass filter: keyword match against tracked analysts/brokers/companies,
    # then Haiku scoring of the matches in batches of 10. EDGAR items are
    # auto-preserved (mechanical signals). Dropped items logged for spot-checking.
    if ANTHROPIC_API_KEY and news_items:
        try:
            _ant_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            STATE_DIR.mkdir(exist_ok=True)
            dropped_log = str(STATE_DIR / f"news_dropped_{today.isoformat()}.json")
            pre_filter_count = len(news_items)
            news_items = filter_relevant_news(
                items=news_items,
                tracked_analysts=TRACKED_ANALYSTS,
                tracked_companies=TRACKED_COMPANIES,
                tracked_brokers=TRACKED_BROKERS,
                anthropic_client=_ant_client,
                haiku_model=HAIKU_MODEL,
                min_score=3,
                log_dropped_path=dropped_log,
                tracked_concepts=TRACKED_CONCEPTS,
            )
            collector.add(SourceFetchStatus(
                "News relevance filter", "llm", True,
                len(news_items), 0,
                detail=f"{pre_filter_count} → {len(news_items)} (min_score=3)",
            ))
            logger.info(f"News relevance filter: {pre_filter_count} → {len(news_items)} items")
        except Exception as e:
            logger.warning(f"Relevance filter failed, proceeding with unfiltered news: {e}")
            collector.add(SourceFetchStatus(
                "News relevance filter", "llm", False, 0, 1,
                detail=f"filter error: {e}",
            ))

    # ----- Step 2: Load Layer 1 digests -----
    digests = read_digests_in_window(days_back=2)
    logger.info(f"Loaded {len(digests)} Layer 1 digests from deep tier")

    # Surface transcript-commentary availability in the validation section.
    # The deep tier writes state/transcript_status.json; we turn each result
    # into a SourceFetchStatus row so 'commentary NOT available' is visible.
    try:
        _surface_transcript_status(collector)
    except Exception as e:
        logger.warning(f"Could not surface transcript status: {e}")

    # ----- Step 3: Performance + big movers + sharpened signal evaluators -----
    performance = calculate_performance(price_history, WIDER_BASKET)
    big_movers = identify_big_movers(performance, BIG_MOVER_THRESHOLD_PCT)

    # Compute hyperscaler capex/revenue ratios — feeds t2_hyperscaler_capex_revenue
    logger.info("Computing hyperscaler capex/revenue ratios...")
    try:
        hyperscaler_capex = fetch_all_hyperscaler_capex(HYPERSCALERS, collector=collector)
    except Exception as e:
        logger.warning(f"Hyperscaler capex computation failed: {e}")
        hyperscaler_capex = {}

    # Evaluate full t3_relative_strength signal (all 3 sub-triggers)
    logger.info("Evaluating relative strength signal (5d + single-day + DRAM ETF)...")
    try:
        rel_strength_verdict = evaluate_relative_strength_signal(price_history)
        if rel_strength_verdict.get("banner_text"):
            logger.warning(f"🚨 {rel_strength_verdict['banner_text']}")
    except Exception as e:
        logger.warning(f"Relative strength evaluation failed: {e}")
        rel_strength_verdict = {}

    # Build computed_metrics block — Layer 2 consumes this
    computed_metrics = {
        "forward_pe": {
            t: estimates_map.get(t, {}).get("forward_pe") if hasattr(estimates_map.get(t, {}), "get")
            else getattr(estimates_map.get(t, None), "forward_pe", None)
            for t in CORE_TICKERS.values()
            if estimates_map.get(t)
        },
        "capex_revenue_ratios": {
            ticker: [
                {
                    "quarter_iso": s.quarter_iso,
                    "capex_usd_b": s.capex_usd_b,
                    "revenue_usd_b": s.revenue_usd_b,
                    "ratio": s.capex_revenue_ratio,
                    "fired_threshold": s.fired_threshold,
                }
                for s in snaps
            ]
            for ticker, snaps in hyperscaler_capex.items()
        },
        "relative_strength_full": rel_strength_verdict,
        "dart_material_disclosures": dart_data or {},
    }

    # ----- Step 4: Layer 2 per-signal reasoning -----
    prior_state = load_prior_state()
    prior_status_map = {
        a.get("signal_id", a.get("id")): a.get("fired_status", a.get("status"))
        for a in (prior_state.get("indicator_assessments") or [])
    }

    logger.info("Running Layer 2 per-signal reasoning across all 22 indicators...")
    signal_reasonings = reason_all_signals(
        digests=digests,
        fast_tier_news=[
            {"title": n.title, "url": n.url, "source": n.source, "summary": getattr(n, "summary", "")}
            for n in news_items
        ],
        naver_data=naver_data,
        finnhub_data=finnhub_data,
        edgar_items=[
            {"title": n.title, "source": n.source, "url": n.url}
            for n in news_items if "edgar" in n.source.lower()
        ],
        computed_metrics=computed_metrics,
        prior_status_map=prior_status_map,
    )
    logger.info(f"Layer 2 produced {len(signal_reasonings)} signal reasonings")

    # ----- Step 5: Layer 3 (Sonnet) synthesis -----
    logger.info("Running Layer 3 (Sonnet) synthesis...")
    layer3_input = {
        "signal_reasonings": [r.to_dict() for r in signal_reasonings],
        "performance": [asdict(p) for p in performance],
        "big_movers": [asdict(m) for m in big_movers],
        "naver_summary": naver_data,
        "finnhub_summary": finnhub_data,
        "dart_summary": dart_data,
        "prior_state": prior_state,
    }
    analysis = analyze_with_llm(
        news_items=news_items,
        estimates=estimates_map,
        performance=performance,
        big_movers=big_movers,
        prior_state=prior_state,
        api_key=ANTHROPIC_API_KEY,
        collector=collector,
        layer2_input=layer3_input,  # backward-compatible — old llm_summarizer ignores if None
    )
    if analysis is None:
        logger.error("Layer 3 (Sonnet) returned None; using fallback analysis")
        analysis = fallback_analysis(news_items, performance, big_movers)

    # ----- Step 6: Chart + email -----
    chart_b64 = generate_performance_chart(
        baseline_to_100(price_history),
        WIDER_BASKET,
        collector=collector,
    )

    upcoming = get_upcoming_events(within_days=90)
    logger.info(f"Calendar: {len(upcoming)} upcoming events")

    # Compute the EPS scoring verdict for the email banner + section.
    # The Layer 2 reasoner already computed it for the t2_eps_revisions signal —
    # we extract the structured verdict from its raw_evidence_dump.
    eps_scoring_verdict = None
    for r in signal_reasonings:
        if r.signal_id == "t2_eps_revisions":
            eps_scoring_verdict = r.raw_evidence_dump.get("signal_specific_data", {}).get("weighted_scoring_verdict")
            break

    html_body = build_email_html(
        analysis=analysis,
        performance=performance,
        chart_base64=chart_b64,
        data_source_statuses=collector.all(),
        upcoming_events=upcoming,
        rel_strength_verdict=rel_strength_verdict,
        eps_scoring_verdict=eps_scoring_verdict,
    )

    # Send
    try:
        send_email(
            html_body=html_body,
            subject=f"Memory Cycle Brief — {today.strftime('%a %b %d')}",
            collector=collector,
        )
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        # Save HTML to repo for manual retrieval
        failed_path = Path(f"state/failed_send_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.html")
        failed_path.parent.mkdir(exist_ok=True)
        failed_path.write_text(html_body)
        logger.error(f"Saved failed-send body to {failed_path}")
        return 1

    # ----- Step 7: Mark idempotent + save state for tomorrow's diff -----
    mark_brief_sent_today(today)
    save_state(analysis)

    duration = (datetime.utcnow() - started_at).total_seconds()
    logger.info(f"=== FAST TIER COMPLETE in {duration:.0f}s ===")
    return 0


def _surface_transcript_status(collector) -> None:
    """Read state/transcript_status.json (written by the deep tier) and add one
    SourceFetchStatus row per transcript result, so commentary availability —
    especially 'materials_only' (commentary NOT available) — is visible in the
    email's data-source validation section.

    Mapping to the four-state health model:
      full_transcript -> ok   (reached, items_returned=1)
      materials_only  -> warn (reached, items_returned=0, detail explains the gap)
      nothing         -> warn (reached, items_returned=0, detail explains)
    A 'warn' is correct for materials_only: the source worked, but the thing we
    actually wanted — call commentary — isn't there.
    """
    import json
    from pathlib import Path

    status_path = Path("state") / "transcript_status.json"
    if not status_path.exists():
        # Deep tier hasn't run yet, or ran before v3.31 — note it once, don't fail.
        collector.add(SourceFetchStatus(
            source_name="Earnings transcripts (IR + Motley Fool)",
            source_type="scraper",
            reached=False,
            items_returned=0,
            latency_ms=0,
            error="No transcript_status.json — deep tier may not have run yet",
        ))
        return

    try:
        data = json.loads(status_path.read_text())
    except Exception as e:
        collector.add(SourceFetchStatus(
            source_name="Earnings transcripts (IR + Motley Fool)",
            source_type="scraper",
            reached=False,
            items_returned=0,
            latency_ms=0,
            error=f"transcript_status.json unreadable: {e}",
        ))
        return

    results = data.get("results", [])
    if not results:
        collector.add(SourceFetchStatus(
            source_name="Earnings transcripts (IR + Motley Fool)",
            source_type="scraper",
            reached=True,
            items_returned=0,
            latency_ms=0,
            detail="No earnings events in window (normal outside earnings season)",
        ))
        return

    for r in results:
        company = r.get("company", "?")
        availability = r.get("availability", "nothing")
        source_name = r.get("source_name", "transcript source")
        note = r.get("note") or ""

        if availability == "full_transcript":
            collector.add(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=1,
                latency_ms=0,
                detail="Full call commentary captured",
            ))
        elif availability == "materials_only":
            collector.add(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=0,
                latency_ms=0,
                error="COMMENTARY NOT AVAILABLE — materials only",
                detail=note[:200],
            ))
        else:  # nothing
            collector.add(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=0,
                latency_ms=0,
                detail=f"No transcript or materials found. {note[:160]}",
            ))


if __name__ == "__main__":
    sys.exit(run_fast_tier())
