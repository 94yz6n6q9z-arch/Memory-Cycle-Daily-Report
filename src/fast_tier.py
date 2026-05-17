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
  v3.38.6 (2026-05-17) — FORCE_RUN env var bypass for the Step 0 idempotency check. Set automatically by fast_tier.yml when event_name == 'workflow_dispatch' (manual run from the Actions UI); scheduled cron triggers leave it empty so the existing primary/fallback/safety cron coordination via state/brief_sent_dates.json continues to work. Motivation: after the v3.38.5 push, manually re-running the fast tier to validate the fix bundle was a no-op because the morning 16:25 UTC run had already marked today. Manual dispatch always means "I want this to run right now" (test/validate/recover) — auto-bypass is the right default. `os` added to imports. No behavior change for scheduled runs.
  v3.38.5 (2026-05-17) — Two error-message strings rewrote to reflect real causes. The previous strings ("deep tier hasn't run since v3.38 deploy" / "deep tier may not have run yet") implied the deep tier didn't run, when in practice the deep tier DID run but the v3.38 root-level state JSONs (state/deep_tier_diagnostics.json, state/transcript_status.json) weren't in the deep_tier.yml workflow's `git add` line. Files generated on the runner, destroyed at shutdown, never reached origin/main. Fixed in the deep_tier.yml workflow in same release. Error messages now name both possibilities: tier didn't run, OR tier ran but didn't commit.
  v3.38.3 (2026-05-17) — Fixed `fallback_analysis` call site (line 399): was passing 3 positional args (news_items, performance, big_movers) to a function with 4 required parameters in different order (performance_data, estimates_data, news_items, big_movers). Pre-existing v3.37 bug masked by upstream crashes; surfaced when Sonnet JSON parse failed → fallback path activated → TypeError. Now uses kwarg form with all four. Caught by the expanded scripts/audit_signatures.py audit (positional + kwargs + arg count).
  v3.38.1 (2026-05-17) — Three pre-existing signature mismatches fixed (all crashed when v3.38 unblocked upstream errors). (a) `analyze_with_llm` call: renamed kwargs `estimates`→`estimates_data`, `performance`→`performance_data`; dropped non-existent `layer2_input=` kwarg and unused `layer3_input` dict. (b) `generate_performance_chart` call: dropped spurious WIDER_BASKET positional that landed in `title:str`, dropped non-existent `collector=` kwarg. (c) `send_email` call: dropped non-existent `collector=` kwarg.
  v3.38   (2026-05-17) — Bug fixes from the 11:30 UTC crash log: (a) news kwarg fix at fetch_all_news call — EDGAR_FEEDS now passed as `edgar_feeds=` kwarg, was landing in `hours_back:int` and causing TypeError. (b) DataFrame None-fallback for prices: `_prices if _prices is not None else pd.DataFrame()` (was `or {}` which triggered pandas truthiness ValueError). (c) Latent bug fixed: 8 `collector.add()` calls changed to `collector.record()` (the .add method doesn't exist on DiagnosticsCollector; calls would have raised AttributeError, silently swallowed by surrounding try/except — meaning some validation rows have been missing from past briefs). (d) `_run()` now records a FAIL row in the collector when a fetcher lambda throws (was silent before). (e) Naver/Finnhub/DART always record via status_from_retry_meta (was only recording on success). (f) New `_surface_deep_tier_diagnostics()` reads state/deep_tier_diagnostics.json and merges deep-tier rows into the fast-tier collector; includes staleness check (warn row if run_date != today). Imports added: pandas as pd, status_from_retry_meta, statuses_from_dict_list. v3.38.1 throttle (in signal_reasoner.py) bundled into same release.
  v3.31  (2026-05-14) — Added _surface_transcript_status() — commentary availability shown in email validation table
  v3.2   (2026-05-12) — Passes TRACKED_CONCEPTS to relevance filter
  v3.1   (2026-05-12) — Wired relevance filter as Step 1b (post-fetch, pre-digest-load)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import pandas as pd
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
from src.diagnostics import (
    DiagnosticsCollector, SourceFetchStatus,
    status_from_retry_meta, statuses_from_dict_list,
)
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
    # v3.38.6: FORCE_RUN env var bypasses this check. Set automatically by
    # the fast_tier.yml workflow when event_name == 'workflow_dispatch' (i.e.
    # manual run from Actions UI). Scheduled cron runs leave it empty and
    # the check fires as before — important, since the three crons
    # (11:00 / 11:30 / 12:05 UTC) explicitly rely on idempotency so that
    # the fallback/safety crons no-op when the primary succeeded.
    already_sent = has_brief_been_sent_today(today)
    force_run = os.environ.get("FORCE_RUN", "").strip().lower() in ("1", "true", "yes")
    if already_sent and not force_run:
        logger.info(f"Brief already sent today ({today.isoformat()}). Exiting early — no-op.")
        return 0
    if already_sent and force_run:
        logger.info(
            f"Brief already sent today ({today.isoformat()}), "
            "but FORCE_RUN is set — bypassing idempotency check and running full pipeline."
        )

    logger.info(f"=== FAST TIER START — {today.isoformat()} {started_at.strftime('%H:%M')} UTC ===")

    collector = DiagnosticsCollector()

    # ----- Step 1: Concurrent fast-source fetch -----
    logger.info("Fetching from concurrent source pool...")
    fetch_results: dict[str, Any] = {}

    # v3.38: descriptive labels per fetcher so a FAIL row is unambiguous
    # about which subsystem died. Maps to source_name + source_type used
    # when this branch crashes top-level (before any inner instrumentation).
    _LABEL_META = {
        "prices":    ("Yahoo Finance / yfinance batch (prices)",  "yfinance"),
        "estimates": ("Estimates (yfinance + FMP)",               "yfinance"),
        "news":      ("News aggregator (RSS + Google News + EDGAR)", "rss"),
        "naver":     ("Naver Finance (Korean broker scraper)",    "scraper"),
        "finnhub":   ("Finnhub (US analyst actions)",             "api"),
        "dart":      ("DART (Korean regulatory filings)",         "api"),
    }

    def _run(label, fn):
        """Run a fetcher; on exception, record a FAIL row in the collector
        so a top-level crash is visible in the email validation table rather
        than vanishing silently. (Sub-source rows that completed before the
        crash remain — you'll see a partial picture plus a FAIL row for the
        aggregator, which is the right outcome.)"""
        try:
            return label, fn()
        except Exception as e:
            logger.error(f"{label} fetch raised: {e}")
            src_name, src_type = _LABEL_META.get(label, (label, "scraper"))
            collector.record(SourceFetchStatus(
                source_name=src_name, source_type=src_type,
                reached=False, items_returned=0, latency_ms=0,
                error=f"{type(e).__name__}: {e}",
                detail="aggregator-level crash (sub-source rows above may be partial)",
            ))
            return label, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_run, "prices",    lambda: fetch_price_history(WIDER_BASKET, collector=collector)),
            pool.submit(_run, "estimates", lambda: fetch_all_estimates(CORE_TICKERS, fmp_api_key=FMP_API_KEY, collector=collector)),
            # v3.38: edgar_feeds passed as kwarg — fetch_all_news signature is
            # (rss, google, hours_back, edgar_feeds, collector); positional
            # placement of EDGAR_FEEDS lands in hours_back. See CHANGES.md.
            pool.submit(_run, "news",      lambda: fetch_all_news(RSS_FEEDS, GOOGLE_NEWS_QUERIES, edgar_feeds=EDGAR_FEEDS, collector=collector)),
            pool.submit(_run, "naver",     fetch_naver_with_retry),
            pool.submit(_run, "finnhub",   fetch_finnhub_with_retry),
            pool.submit(_run, "dart",      fetch_dart_with_retry),
        ]
        for fut in as_completed(futures):
            label, result = fut.result()
            fetch_results[label] = result

    # v3.38: prices returns a pd.DataFrame, not a dict — `... or {}` triggers
    # DataFrame truthiness which pandas refuses to evaluate. Explicit None check.
    _prices_raw = fetch_results.get("prices")
    price_history = _prices_raw if _prices_raw is not None else pd.DataFrame()
    estimates_map = fetch_results.get("estimates") or {}
    news_items = fetch_results.get("news") or []
    # naver/finnhub/dart return (payload, meta) tuples; legacy code also handled bare payload
    naver_result   = fetch_results.get("naver")
    finnhub_result = fetch_results.get("finnhub")
    dart_result    = fetch_results.get("dart")

    def _split(res):
        """(payload, meta) tuple → (payload, meta); bare payload → (payload, {}); None → (None, {})."""
        if isinstance(res, tuple) and len(res) == 2:
            return res
        return (res, {})

    naver_data,   naver_meta   = _split(naver_result)
    finnhub_data, finnhub_meta = _split(finnhub_result)
    dart_data,    dart_meta    = _split(dart_result)

    # v3.38: ALWAYS record a row — failure must be visible, not silent.
    # `status_from_retry_meta` interprets the meta from fetch_with_retry:
    # status=ok → green; cached_stale → green-with-loud-detail; failed → FAIL row.
    # The aggregator-crash path in _run() above has already emitted a FAIL row
    # if the wrapper itself threw, so we skip recording here when the meta is
    # empty AND payload is None (that means _run() already covered it).
    if not (naver_data is None and not naver_meta):
        collector.record(status_from_retry_meta(
            "Naver Finance (Korean broker scraper)", "scraper",
            naver_data, naver_meta,
            items_count=sum(t.get("n_actions", 0) for t in (naver_data or {}).get("tickers", {}).values()),
            items_detail=f"{len((naver_data or {}).get('tickers', {}))} tickers" if naver_data else None,
        ))
    if not (finnhub_data is None and not finnhub_meta):
        collector.record(status_from_retry_meta(
            "Finnhub (US analyst actions)", "api",
            finnhub_data, finnhub_meta,
            items_count=len((finnhub_data or {}).get("tickers", {})),
        ))
    if not (dart_data is None and not dart_meta):
        collector.record(status_from_retry_meta(
            "DART (Korean regulatory filings)", "api",
            dart_data, dart_meta,
            items_count=sum(t.get("n_disclosures", 0) for t in (dart_data or {}).get("tickers", {}).values()),
        ))

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
            collector.record(SourceFetchStatus(
                "News relevance filter", "llm", True,
                len(news_items), 0,
                detail=f"{pre_filter_count} → {len(news_items)} (min_score=3)",
            ))
            logger.info(f"News relevance filter: {pre_filter_count} → {len(news_items)} items")
        except Exception as e:
            logger.warning(f"Relevance filter failed, proceeding with unfiltered news: {e}")
            collector.record(SourceFetchStatus(
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

    # v3.38: ingest deep-tier diagnostics (state/deep_tier_diagnostics.json).
    # Adds rows for each transcript source (Motley Fool, Yahoo, IR), Korean
    # broker feeds (Mirae, NH, Samsung Securities), and the Haiku digester.
    # Without this, a deep-tier source failure overnight would be silent.
    try:
        _surface_deep_tier_diagnostics(collector)
    except Exception as e:
        logger.warning(f"Could not surface deep-tier diagnostics: {e}")

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
    # v3.39 NOTE: Earlier code constructed a `layer3_input` dict and passed it
    # as `layer2_input=` to analyze_with_llm. That kwarg has never existed on
    # the function — call was pre-existing broken since at least v3.37, masked
    # by the v3.37 upstream crashes that prevented Layer 3 from running at all.
    # Removed: dead `layer3_input` dict, bogus `layer2_input` kwarg.
    # Renamed: estimates → estimates_data, performance → performance_data
    # (matching analyze_with_llm's actual signature).
    #
    # Architectural note: this means Sonnet does NOT receive signal_reasonings,
    # naver_summary, finnhub_summary, or dart_summary today. That's the
    # pre-existing condition — the data has never reached Sonnet. Threading
    # it through is a follow-up architectural decision, not a bug fix.
    logger.info("Running Layer 3 (Sonnet) synthesis...")
    analysis = analyze_with_llm(
        api_key=ANTHROPIC_API_KEY,
        performance_data=performance,
        estimates_data=estimates_map,
        news_items=news_items,
        big_movers=big_movers,
        prior_state=prior_state,
        collector=collector,
    )
    if analysis is None:
        logger.error("Layer 3 (Sonnet) returned None; using fallback analysis")
        # v3.38.3: fallback_analysis signature is (performance_data, estimates_data,
        # news_items, big_movers). Old call passed (news_items, performance, big_movers)
        # — 3 positional args in wrong order, missing estimates_data → TypeError.
        # Pre-existing v3.37 bug, masked by previous crashes which prevented Sonnet
        # from returning None (it errored at the call site itself).
        analysis = fallback_analysis(
            performance_data=performance,
            estimates_data=estimates_map,
            news_items=news_items,
            big_movers=big_movers,
        )

    # ----- Step 6: Chart + email -----
    # v3.39: generate_performance_chart takes (history_baseline100, title=...).
    # Old call passed WIDER_BASKET as positional 2 (landed in title:str — would
    # render chart titled "['NVDA', 'MU', ...]") and collector=collector (kwarg
    # doesn't exist — TypeError next after analyze_with_llm). Both removed;
    # default title used.
    chart_b64 = generate_performance_chart(baseline_to_100(price_history))

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
        # v3.39: send_email takes (subject, html_body, recipient=None).
        # Old call passed collector=collector — kwarg doesn't exist, TypeError.
        send_email(
            subject=f"Memory Cycle Brief — {today.strftime('%a %b %d')}",
            html_body=html_body,
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
        # v3.38.5: rephrased. Previously said "deep tier may not have run yet"
        # which masked the v3.38 bug where the deep tier DID run but its
        # output JSONs weren't in the workflow's `git add` line, so they
        # were destroyed at runner shutdown. Real cause now reflected in
        # the message: file missing on origin/main, either because (a) deep
        # tier hasn't run today, or (b) it ran but didn't push the file.
        collector.record(SourceFetchStatus(
            source_name="Earnings transcripts (IR + Motley Fool)",
            source_type="scraper",
            reached=False,
            items_returned=0,
            latency_ms=0,
            error=(
                "state/transcript_status.json missing on origin/main. "
                "Either the deep tier hasn't run today, OR it ran but the file "
                "wasn't committed (pre-v3.38.5 bug: deep_tier.yml only staged "
                "state/digests/ + state/cache/, missing the v3.38 root-level "
                "JSON files). Check the deep tier workflow's most recent run "
                "and its commit step."
            ),
        ))
        return

    try:
        data = json.loads(status_path.read_text())
    except Exception as e:
        collector.record(SourceFetchStatus(
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
        collector.record(SourceFetchStatus(
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
            collector.record(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=1,
                latency_ms=0,
                detail="Full call commentary captured",
            ))
        elif availability == "already_processed":
            # v3.39: was falling through to the catch-all "nothing" branch
            # below, producing false WARN rows reading "No transcript or
            # materials found." But we DO have the transcript — it was
            # fetched and Haiku-digested in a prior run, and the digest is
            # already on disk in state/digests/. The deep tier emits this
            # status from the URL cache (state/transcripts_seen.json) when
            # the IR/Yahoo/Motley Fool URL has been seen before, skipping
            # the body re-fetch and the Haiku call. Steady-state output
            # for most days outside earnings season — ~28 of 30 transcripts
            # are cached. Mapping to OK (items_returned=1) reflects reality:
            # the commentary IS available for this brief, just not freshly
            # fetched today.
            collector.record(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=1,
                latency_ms=0,
                detail="Cached from prior run — digest available, no re-fetch needed",
            ))
        elif availability == "materials_only":
            collector.record(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=0,
                latency_ms=0,
                error="COMMENTARY NOT AVAILABLE — materials only",
                detail=note[:200],
            ))
        else:  # nothing
            collector.record(SourceFetchStatus(
                source_name=f"Transcript: {company} ({source_name})",
                source_type="scraper",
                reached=True,
                items_returned=0,
                latency_ms=0,
                detail=f"No transcript or materials found. {note[:160]}",
            ))


def _surface_deep_tier_diagnostics(collector) -> None:
    """v3.38 — read state/deep_tier_diagnostics.json (written by the deep tier)
    and merge its rows into the fast-tier collector.

    Without this, deep-tier source failures (Motley Fool down, Yahoo proxy
    auth flap, Mirae broker site outage, Haiku digester rate-limited into
    silence) would never appear in the email — the fast tier doesn't run the
    deep-tier fetchers itself, it only consumes their digest outputs.

    Staleness check: if the file is from a previous day, emit a single WARN
    row pointing that out. Means the deep tier didn't run today, which is
    itself a signal worth surfacing.
    """
    diag_path = Path("state") / "deep_tier_diagnostics.json"
    if not diag_path.exists():
        # v3.38.5: rephrased — old message "deep tier hasn't run since v3.38
        # deploy" was wrong in practice. The actual common cause through
        # v3.38.4 was the deep_tier.yml workflow not committing this file
        # (only `state/digests/` and `state/cache/` were in its `git add`).
        # File present on the runner, never pushed to origin. Fixed in v3.38.5
        # by adding `[ -f state/deep_tier_diagnostics.json ] && git add ...`.
        collector.record(SourceFetchStatus(
            source_name="Deep tier diagnostics",
            source_type="transcript",
            reached=False,
            items_returned=0,
            latency_ms=0,
            error=(
                "state/deep_tier_diagnostics.json missing on origin/main. "
                "Either the deep tier hasn't run since the v3.38.5 workflow "
                "fix landed, OR a pre-v3.38.5 deep tier run wrote this file "
                "on its runner but never committed it (bug fixed in v3.38.5)."
            ),
            detail="Trigger deep tier from Actions tab; confirm its commit step now includes both root-level state JSONs",
        ))
        return

    try:
        data = json.loads(diag_path.read_text())
    except Exception as e:
        collector.record(SourceFetchStatus(
            source_name="Deep tier diagnostics",
            source_type="transcript",
            reached=False,
            items_returned=0,
            latency_ms=0,
            error=f"deep_tier_diagnostics.json unreadable: {e}",
        ))
        return

    # Staleness check: deep tier runs at 06:00 UTC, fast tier at 11:00 UTC.
    # Same calendar date = fresh; older = warn (deep tier didn't run today).
    run_date_str = data.get("run_date", "")
    today_str = date.today().isoformat()
    if run_date_str and run_date_str != today_str:
        collector.record(SourceFetchStatus(
            source_name="Deep tier freshness",
            source_type="transcript",
            reached=True,
            items_returned=0,
            latency_ms=0,
            error=f"STALE — deep tier last ran {run_date_str}, expected {today_str}",
            detail="Check deep-tier workflow run; transcripts/brokers/Haiku rows below "
                   "reflect that older run, not today",
        ))

    deep_statuses = statuses_from_dict_list(data.get("statuses", []))
    for s in deep_statuses:
        collector.record(s)
    logger.info(f"Surfaced {len(deep_statuses)} deep-tier diagnostic rows from {run_date_str or 'unknown date'}")


if __name__ == "__main__":
    sys.exit(run_fast_tier())
