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
  3. Layer 2: per-signal reasoning over all evidence (23 indicators × 1 Haiku call each)
  4. Layer 3: Sonnet final synthesis — exec summary, brief composition
  5. Chart generation + email build + send
  6. Mark idempotency + commit prior state for tomorrow

Failure modes — see src/state.py for retry/cache policy. Critical sources
(yfinance, Anthropic, Naver) trigger 🚨 banner on full failure; important
sources use cached payload tagged STALE; nice-to-have just skip.

CHANGELOG (this file):
  v3.39.7.3 (2026-05-20) — INDICATOR EVIDENCE STORE integration. Before Layer 2, load state/indicator_evidence.json (per-indicator current_status + evidence_log + reversal_conditions) via src/state.py:load_indicator_evidence_store. Pass to reason_all_signals as indicator_evidence_store=. After Layer 2, mutate the store via update_evidence_after_layer2 (records status transitions + new evidence entries) and persist via save_indicator_evidence_store. The store is committed via fast_tier.yml so tomorrow's run sees today's decisions. Solves the v3.39.6/v3.39.7 regression where Layer 2's silence-on-quiet-day caused cumulative-state indicators (capex, LTAs, P/E, OEM inventory, etc.) to inappropriately downgrade. Bootstrap from May 9, 2026 framework doc lives at state/indicator_evidence.json with 10 indicators pre-fired. Also: signal_name in _inject_matrix_verdict_into_signal_reasonings updated to "DRAM Price Evolution (spot + contract matrix)" to match the renamed display label in config.py. Safe-failing: any exception in load/save/update is logged and the brief still ships.
  v3.39.7.1 (2026-05-20) — Three changes. (1) Cycle matrix step MOVED from "between Layer 3 and email build" to "between Layer 2 and Layer 3", so Sonnet's prompt sees the matrix verdict via the v3.39.6 LAYER 2 PER-SIGNAL VERDICTS block. (2) Part 5a: new helper _inject_matrix_verdict_into_signal_reasonings() appended at bottom — constructs a SignalReasoning with the matrix structured fields in raw_evidence_dump.signal_specific_data.matrix_verdict (cell, cell_label, contract_row, spot_col, synthetic_central_pct, synthetic_low/high, anchor_quarter, anchor_central_pct, anchor_age_days, drawdown_pct, current_session_avg, rolling_peak_price, rolling_peak_date, weeks_since_peak, adjustments list + warnings) + replaces any existing t1_spot_decline entry in-place (appends if missing). Convergence/confidence derived from count of adjustments with data: ≥2→high, 1→medium, 0→low. (3) Subject suffix wording aligned: [Hold|Warn|Fire] → [Not fired|Partially fired|Fired] (driven directly off verdict.cell.value which is now those strings). _override_t1_spot_decline_from_matrix no longer needs the CYCLE_MATRIX_STATUS_MAP lookup since the cell value IS the status; map is kept as identity in config for documentation.
  v3.39.7 (2026-05-20) — Cycle matrix step inserted between Layer 3 and chart generation. New helper run_cycle_matrix_step() (a) calls scrape_trendforce_spot_structured() + scrape_trendforce_contract() + persist_*() (b) Haiku-extracts tone/forecast signals from up to CYCLE_MATRIX_MAX_EXTRACTIONS_PER_RUN articles (c) evaluates the matrix via cycle_matrix.evaluate_matrix(). The matrix verdict OVERRIDES the LLM's t1_spot_decline indicator status via CYCLE_MATRIX_STATUS_MAP: Hold→Not fired, Warn→Partially fired, Fire→Fired. The where_we_stand text is rewritten from the matrix narrative so the cell/drawdown/synthetic numbers are deterministic, not LLM-paraphrased. Both cycle charts are rendered via render_cycle_charts_from_verdict() and passed to build_email_html. Cycle step is safe-failing — exception at any sub-step logs and continues; LLM's t1_spot_decline assessment used as fallback. Email subject also gets a [Hold|Warn|Fire] suffix when matrix verdict is available.
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
import subprocess
import sys
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.config import (
    ANTHROPIC_API_KEY,
    ALL_TICKERS,
    BIG_MOVER_THRESHOLD_PCT,
    CORE_TICKERS,
    CYCLE_PHASE_COLORS,
    CYCLE_PHASE_DURATIONS_MONTHS,
    CYCLE_TROUGH_DATE,
    EDGAR_FEEDS,
    EMAIL_TO,
    EXPECTED_EMPTY_EDGAR_FEEDS,
    FMP_API_KEY,
    GOOGLE_NEWS_QUERIES,
    HAIKU_MODEL,
    HYPERSCALERS,
    INDICATORS,
    RSS_FEEDS,
    SUPERCYCLE_CHART_TICKER_CURRENCY,
    SUPERCYCLE_CHART_TICKER_NAME,
    TRACKED_ANALYSTS,
    TRACKED_BROKERS,
    TRACKED_COMPANIES,
    TRACKED_CONCEPTS,
    WATCH_GOOGLE_QUERIES,
    WIDER_BASKET,
    get_upcoming_events,
    # v3.39.7 — cycle matrix config
    CYCLE_MATRIX_DATA_DIR,
    CYCLE_MATRIX_SPOT_CSV,
    CYCLE_MATRIX_CONTRACT_ACTUALS_CSV,
    CYCLE_MATRIX_FORECASTS_CSV,
    CYCLE_MATRIX_SIGNALS_CSV,
    CYCLE_MATRIX_MAX_EXTRACTIONS_PER_RUN,
    CYCLE_MATRIX_STATUS_MAP,
    # v3.4.11 — phase detector params
    PHASE_5_DETECTION,
)
from src.state import (
    has_brief_been_sent_today,
    mark_brief_sent_today,
    read_digests_in_window,
    fetch_with_retry,
    Criticality,
    PRIOR_STATE_FILE,
    STATE_DIR,
    # v3.39.7.3 — persistent evidence store API
    load_indicator_evidence_store,
    save_indicator_evidence_store,
    update_evidence_after_layer2,
    # v3.4.12 — QoQ transcript diffs
    list_recent_quarter_diffs,
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
from src.data.news import (
    fetch_all_news, filter_relevant_news,
    # v3.39.7 cycle matrix data pipeline:
    scrape_trendforce_spot_structured, scrape_trendforce_contract,
    fetch_article_body,
    persist_spot_to_history, persist_contract_to_history,
    upsert_contract_forecasts, upsert_contract_signals,
)
from src.data.naver_finance import fetch_naver_with_retry
from src.data.finnhub import fetch_finnhub_with_retry
from src.data.dart import fetch_dart_with_retry
from src.config import HYPERSCALERS
from src.analysis.signal_reasoner import reason_all_signals
from src.analysis.tape_circuit_breaker import run_tape_circuit_breaker
from src.analysis.computed_verdicts import (
    apply_reversal_gate,
    evaluate_all as evaluate_computed_verdicts,
    apply_to_objects as apply_computed_verdicts,
    apply_status_ratchet,
    evaluate_capex_plateau_verdict,
)
from src.analysis.llm_summarizer import (
    analyze_with_llm,
    fallback_analysis,
    identify_big_movers,
    # v3.39.7 — Haiku extraction for cycle matrix
    extract_dram_signal_from_article,
    extract_dram_forecasts_from_press_release,
)
# v3.4.11 — deterministic Phase 4 → Phase 5 detector
from src.analysis.phase_detector import (
    evaluate_phase_for_run,
    result_to_state_dict,
)
from src.state import load_phase_state, save_phase_state
from src.analysis.chart_generator import (
    generate_performance_chart, generate_supercycle_chart,
    # v3.39.7 — cycle matrix charts
    render_cycle_charts_from_verdict,
)
# v3.39.7 — cycle matrix engine
from src.data.cycle_matrix import evaluate_matrix, MatrixVerdict
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
                "saved_at_iso": datetime.now(UTC).isoformat(),
                "executive_summary": asdict(analysis.executive_summary) if hasattr(analysis, "executive_summary") else {},
                "indicator_assessments": [asdict(a) for a in (analysis.indicator_assessments or [])] if hasattr(analysis, "indicator_assessments") else [],
            }
            json.dump(payload, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Could not persist prior state: {e}")


def _refresh_from_origin_main() -> bool:
    """Fast-forward the local checkout to origin/main if behind.

    v3.39.7.4 — hardening for the cron-overlap race. With 30-min cron
    spacing (11:13 / 11:43 / 12:13 / 12:43 UTC) and ~25-min runs, the
    11:13 run typically commits/pushes ~11:38-11:39. The 11:43 cron then
    starts and `actions/checkout` resolves origin/main — but there's a
    short window (seconds to a minute) where GitHub's API has accepted
    the push but the runner's fetch might still see the prior tip.
    Without this refresh, the 11:43 run can see a `brief_sent_dates.json`
    that doesn't yet have today's date, fail idempotency, and run the
    full pipeline → duplicate email.

    Belt-and-suspenders: do an explicit `git fetch origin main` and, if
    origin is ahead, hard-reset to it. Runs at the very start of
    run_fast_tier() before any state is written by the runner, so the
    reset is safe (no local changes to lose).

    Returns True if a refresh happened (or no-op was needed); False if
    we couldn't reach origin (in which case we proceed with the local
    checkout — degraded but functional).
    """
    try:
        # Quick check: are we actually in a git checkout?
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=True, capture_output=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        logger.info("Not in a git checkout (likely local dev run). Skipping origin/main refresh.")
        return False
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            check=True, capture_output=True, timeout=30,
        )
        # How many commits is HEAD behind origin/main?
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        behind = int(result.stdout.strip())
        if behind == 0:
            logger.info("Local checkout matches origin/main. No refresh needed.")
            return True
        logger.info(
            f"Local checkout is {behind} commit(s) behind origin/main. "
            f"Hard-resetting before idempotency check to avoid stale-state duplicate-run race."
        )
        subprocess.run(
            ["git", "reset", "--hard", "origin/main"],
            check=True, capture_output=True, timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(
            f"Could not refresh from origin/main ({e}). "
            f"Proceeding with whatever the runner checked out — idempotency check may see stale state."
        )
        return False


def run_fast_tier() -> int:
    """Execute the fast-tier pipeline. Returns exit code (0 = success)."""
    today = date.today()
    started_at = datetime.now(UTC)

    # ----- Step 0a: Pre-flight state refresh (v3.39.7.4) -----
    # Ensure the local checkout reflects origin/main BEFORE we read
    # state/brief_sent_dates.json for the idempotency check. Without
    # this, a run that started while a prior run's push was still
    # propagating could see stale state and send a duplicate brief.
    # FORCE_RUN bypasses this so manual dispatches don't pay the
    # network round-trip cost when iterating during development.
    force_run = os.environ.get("FORCE_RUN", "").strip().lower() in ("1", "true", "yes")
    if not force_run:
        _refresh_from_origin_main()

    # ----- Step 0b: Idempotency check -----
    # v3.38.6: FORCE_RUN env var bypasses this check. Set automatically by
    # the fast_tier.yml workflow when event_name == 'workflow_dispatch' (i.e.
    # manual run from Actions UI). Scheduled cron runs leave it empty and
    # the check fires as before — important, since the multiple scheduled
    # crons rely on idempotency so fallback/safety crons no-op when the
    # primary succeeded.
    already_sent = has_brief_been_sent_today(today)
    if already_sent and not force_run:
        logger.info(f"Brief already sent today ({today.isoformat()}). Exiting early — no-op.")
        return 0
    if already_sent and force_run:
        logger.info(
            f"Brief already sent today ({today.isoformat()}), "
            "but FORCE_RUN is set — bypassing idempotency check and running full pipeline."
        )

    # ----- Step 0c: Cascade gate — wait for deep_tier -----
    # v3.39.3: hard gate on deep_tier completion. The fast_tier consumes
    # deep_tier's Layer 1 Haiku digests (state/digests/) and the diagnostics
    # file (state/deep_tier_diagnostics.json). Running fast_tier before
    # deep_tier produces a degraded brief — historically this was surfaced
    # only as a WARN row in the email's data source table while still
    # sending the email, which is the wrong default. Now we exit cleanly
    # and wait for the next scheduled trigger. FORCE_RUN bypasses this gate
    # for manual recovery scenarios.
    if not force_run:
        deep_diag_path = Path("state") / "deep_tier_diagnostics.json"
        today_iso = today.isoformat()
        if not deep_diag_path.exists():
            logger.info(
                f"Cascade gate: state/deep_tier_diagnostics.json doesn't exist on disk. "
                f"Deep tier hasn't committed today yet. Exiting cleanly — will retry "
                f"on next scheduled trigger."
            )
            return 0
        try:
            with open(deep_diag_path) as f:
                deep_data = json.load(f)
            deep_run_date = deep_data.get("run_date")
            if deep_run_date != today_iso:
                logger.info(
                    f"Cascade gate: state/deep_tier_diagnostics.json dated "
                    f"{deep_run_date} (not today, {today_iso}). Deep tier hasn't run "
                    f"yet today. Exiting cleanly — will retry on next scheduled trigger."
                )
                return 0
            logger.info(f"Cascade gate: deep_tier ran today ({today_iso}) ✓ — proceeding.")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                f"Cascade gate: could not parse state/deep_tier_diagnostics.json ({e}). "
                f"Treating as stale and exiting cleanly. Manual `workflow_dispatch` with "
                f"FORCE_RUN bypasses this gate."
            )
            return 0

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
            # v3.39.2: period bumped 1y → 3y. Two reasons coordinated:
            # (1) 1Y performance returns were rendering "n/a" in the email
            #     because period="1y" yielded ~252 trading days, and the
            #     pct_change_lookback(252) guard `len(series) < days + 1`
            #     fired (250-252 rows < 253). Bumping the fetch window
            #     gives the 252-day lookback plenty of headroom (~756 rows).
            # (2) The new supercycle chart (v3.39.2) needs SK Hynix history
            #     back to the Q4 2023 cycle trough — ~30 months. 3y covers
            #     it with margin. Defense-in-depth: pct_change_lookback also
            #     hardened in prices.py to fall back gracefully even when
            #     the series is shorter than the requested window.
            # v3.39.7.6 — was WIDER_BASKET, swapped to ALL_TICKERS so
            #     BENCHMARKS (^SOX, ^GSPC, ^KS11, DRAM ETF, SMH) get
            #     fetched alongside the portfolio names. The previous
            #     call silently broke `get_relative_strength_vs_sox()`
            #     and `get_dram_etf_vs_smh()` in src/data/prices.py
            #     because they look for columns named
            #     "MSCI ACWI Semiconductor", "Roundhill DRAM ETF",
            #     "VanEck Semi ETF (SMH)" — none of which were in the
            #     fetched columns when only WIDER_BASKET was requested.
            #     Result: t3_relative_strength always returned
            #     `(not computed)` for SOX spreads regardless of the
            #     actual move. The May 21 2026 brief surfaced this when
            #     SK Hynix 1W was -11.69% (red-fire territory) while
            #     SOX moved approximately flat — a clear underperformance
            #     that the indicator card silently failed to detect.
            #     `calculate_performance(price_history, WIDER_BASKET)`
            #     at the section-1 price table call site is unchanged —
            #     that table should keep showing portfolio names only,
            #     not the benchmark indices.
            pool.submit(_run, "prices",    lambda: fetch_price_history(ALL_TICKERS, period="3y", collector=collector, fmp_api_key=FMP_API_KEY or None)),
            pool.submit(_run, "estimates", lambda: fetch_all_estimates(CORE_TICKERS, fmp_api_key=FMP_API_KEY, collector=collector)),
            # v3.38: edgar_feeds passed as kwarg — fetch_all_news signature is
            # (rss, google, hours_back, edgar_feeds, collector); positional
            # placement of EDGAR_FEEDS lands in hours_back. See CHANGES.md.
            # v3.39.7.6: watch_google_queries + expected_empty_edgar_feeds
            # threaded through so forward-looking watcher feeds render as
            # 'quiet' (informational) on 0 items instead of 'warn'.
            pool.submit(_run, "news",      lambda: fetch_all_news(
                RSS_FEEDS, GOOGLE_NEWS_QUERIES,
                edgar_feeds=EDGAR_FEEDS,
                collector=collector,
                watch_google_queries=WATCH_GOOGLE_QUERIES,
                expected_empty_edgar_feeds=EXPECTED_EMPTY_EDGAR_FEEDS,
            )),
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

    # v3.39.7.4: Compute hyperscaler capex/OCF + capex/revenue — feeds
    # t2_hyperscaler_capex_revenue (now: capex/OCF strain). The mean-across-
    # companies aggregate is computed inside fetch_all_hyperscaler_capex and
    # surfaces below as `mean_capex_ocf_ratio_latest_q`.
    logger.info("Computing hyperscaler capex/OCF + capex/revenue ratios...")
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

    # v3.42.0 — Tape Circuit Breaker: deterministic price/volume exit-state
    # machine, computed before any LLM touches anything. See
    # src/analysis/tape_circuit_breaker.py for the level semantics
    # (L1 TAPE ALERT / L2 DE-RISK / L3 SELL). Never raises; None = offline.
    logger.info("Running Tape Circuit Breaker (price/volume state machine)...")
    try:
        tcb_verdict = run_tape_circuit_breaker()
    except Exception as e:
        logger.warning(f"[tcb] call-site failure (non-fatal): {e}")
        tcb_verdict = None

    # v3.39.7.9: Compute per-company 2-quarter capex/OCF mean, then cross-
    # company mean. Pre-v3.39.7.9 used snaps[0] (single quarter) — the May
    # 21 brief showed why: Amazon Q1 2026 OCF dipped to $26.03B (vs $54.46B
    # Q4 2025) making capex/OCF spike to 169.8% in isolation. 2-quarter mean
    # smooths that to ~85% while preserving the cycle signal. For companies
    # with only 1 published quarter (rare), falls back to that quarter.
    per_company_2q_capex_ocf: dict[str, float] = {}
    for ticker, snaps in hyperscaler_capex.items():
        if not snaps:
            continue
        valid = [s.capex_ocf_ratio for s in snaps[:2] if s.capex_ocf_ratio is not None]
        if valid:
            per_company_2q_capex_ocf[ticker] = round(sum(valid) / len(valid), 4)

    mean_capex_ocf_2q = (
        round(sum(per_company_2q_capex_ocf.values()) / len(per_company_2q_capex_ocf), 4)
        if per_company_2q_capex_ocf else None
    )

    # Build computed_metrics block — Layer 2 consumes this.
    # v3.39.7.9: `capex_revenue_ratios` REMOVED from this block. Pre-v3.39.7.9
    # we kept it "for back-compat with downstream readers" but signal_reasoner
    # was using capex/revenue (24-37%) to override the actual capex/OCF rule
    # (93.2% mean) — Layer 2 returned Reversed against the COMPUTED METRICS.
    # No downstream code path uses capex/revenue anywhere now; removing it
    # closes the bug at the source.
    computed_metrics = {
        "forward_pe": {
            t: estimates_map.get(t, {}).get("forward_pe") if hasattr(estimates_map.get(t, {}), "get")
            else getattr(estimates_map.get(t, None), "forward_pe", None)
            for t in CORE_TICKERS.values()
            if estimates_map.get(t)
        },
        # v3.39.7.9: aggregate trigger value — Layer 2 + Layer 3 fire when:
        #   mean_capex_ocf_ratio_2q > 0.40 → Partially fired
        #   mean_capex_ocf_ratio_2q > 0.50 → Fired
        # The 2q semantics requires the value to persist for TWO consecutive
        # published quarters before tripping. The dict also carries per-company
        # 2q ratios so Layer 2 can identify which companies drive the aggregate.
        "mean_capex_ocf_ratio_2q": mean_capex_ocf_2q,
        "per_company_capex_ocf_2q": per_company_2q_capex_ocf,
        "n_companies_with_2q_capex_ocf": len(per_company_2q_capex_ocf),
        # Raw per-quarter snapshots kept for narrative purposes (Layer 2 can
        # cite "Amazon Q1 2026 OCF dipped to $26.03B" in the verdict) but
        # NOT used for trigger decisions.
        "hyperscaler_quarterly_snapshots": {
            ticker: [
                {
                    "quarter_iso": s.quarter_iso,
                    "capex_usd_b": s.capex_usd_b,
                    "revenue_usd_b": s.revenue_usd_b,
                    "ocf_usd_b": s.ocf_usd_b,
                    "capex_ocf_ratio": s.capex_ocf_ratio,
                    "fired_threshold": s.fired_threshold,
                }
                for s in snaps
            ]
            for ticker, snaps in hyperscaler_capex.items()
        },
        "relative_strength_full": rel_strength_verdict,
        "tape_circuit_breaker": tcb_verdict or {},
        "dart_material_disclosures": dart_data or {},
    }

    # v3.4.0 — Compute Decode HBM Captive Ratio (DHCR) snapshot.
    # v3.4.5: aggregator step added — reads accumulated Layer 1 extraction logs
    # and updates state/inference_arch_state.json, state/hbm_per_accelerator_state.json,
    # and state/enterprise_ai_revenue_state.json with newly-disclosed models,
    # accelerators, and quarterly revenue points BEFORE the snapshot is computed.
    # All three aggregators safe-fail; state preserved on error.
    try:
        from src.analysis.dhcr_arch_aggregator import aggregate_arch_extractions
        aggregate_arch_extractions()
    except Exception as e:
        logger.error(f"DHCR arch aggregator failed (state preserved): {e}", exc_info=True)
    try:
        from src.analysis.dhcr_accelerator_aggregator import aggregate_accelerator_extractions
        aggregate_accelerator_extractions()
    except Exception as e:
        logger.error(f"DHCR accelerator aggregator failed (state preserved): {e}", exc_info=True)
    try:
        from src.analysis.dhcr_revenue_aggregator import aggregate_revenue_extractions
        aggregate_revenue_extractions()
    except Exception as e:
        logger.error(f"DHCR revenue aggregator failed (state preserved): {e}", exc_info=True)
    # v3.6.0 — the context_volume_aggregator was replaced by the
    # context_length_llm_research worker (bi-monthly GHA workflow).
    # No daily aggregator call here anymore; state/context_length_state.json
    # is now maintained exclusively by the LLM worker.

    try:
        from src.analysis.dhcr import compute_dhcr_snapshot, compute_dhcr_trajectory
        from src.state import save_dhcr_state
        from src.data.prices import compute_hbm_price_trend
        from dataclasses import asdict

        # v3.4.3 — auto-derive HBM/DRAM price trend from accumulated CSV data
        # instead of operator-supplied parameter. Spot trajectory + contract
        # forecast revisions combine into rising/flat/falling for DHCR's
        # sanity check signal #3.
        try:
            hbm_price_trend = compute_hbm_price_trend(lookback_days=90)
            logger.info(f"v3.4.3 auto-derived hbm_price_trend: {hbm_price_trend}")
        except Exception as _trend_err:
            logger.warning(f"hbm_price_trend auto-derivation failed: {_trend_err}; defaulting to 'rising'")
            hbm_price_trend = "rising"

        # v3.4.3 — pass extraction_log_signals for substitution-evidence
        # signal #1, and the now-auto-derived hbm_price_trend for signal #3
        extraction_log_signals = computed_metrics.get("extraction_log_signals_last_90d", [])

        # v3.4.13 — pull latest LPDDR contract direction from the cycle
        # matrix step (stashed on the collector by _run_cycle_matrix_step).
        # Informational signal only for v1 — surfaces in the DHCR snapshot
        # but does NOT affect the confirms/contradicts math until
        # 2-3 quarters of LPDDR direction data accumulate.
        lpddr_direction = None
        try:
            lpddr_result = getattr(collector, "_lpddr_scrape_result", None)
            if lpddr_result is not None and lpddr_result.signals:
                latest = lpddr_result.latest_signal
                if latest:
                    lpddr_direction = latest.direction
        except Exception as _lpddr_err:
            logger.warning(f"could not read lpddr direction from collector: {_lpddr_err}")

        dhcr_snapshot = compute_dhcr_snapshot(
            extraction_log_signals=extraction_log_signals,
            hbm_price_trend=hbm_price_trend,
            lpddr_contract_direction=lpddr_direction,
        )
        # v3.7.3 — removed orphaned `snap_dict["narrative_summary"] = build_narrative_summary(snap_dict)`
        # line that referenced two symbols not in scope (build_narrative_summary
        # is not imported here, snap_dict is a local variable inside
        # compute_dhcr_snapshot). The call was redundant: compute_dhcr_snapshot
        # already populates narrative_summary internally at dhcr.py:793 and the
        # DHCRSnapshot dataclass declares it as a field at dhcr.py:191. The
        # NameError was crashing DHCR computation on every fast-tier run, which
        # the bare `except Exception` swallowed into `dhcr_snapshot = None`,
        # which template.py:308's `if dhcr_snapshot:` silently skipped — so
        # the brief jumped from indicator 5 to indicator 7 with no error visible
        # to the operator. Production observed 2026-05-25.
        dhcr_dict = asdict(dhcr_snapshot)

        # v3.4.14 — attach operative context history from context_length_state.json
        # and append today's computed operative context to the history time-series.
        # This is what powers the "over the last quarter" trend prose in template.py.
        try:
            from pathlib import Path as _Path
            import json as _json2
            _cl_state_path = _Path("state/context_length_state.json")
            if _cl_state_path.exists():
                _cl_state = _json2.loads(_cl_state_path.read_text())
                _hist = _cl_state.get("operative_context_history") or []

                # Append today's reading (deduplicated by date)
                import datetime as _dt
                _today = _dt.date.today().isoformat()
                _existing_dates = {h.get("date") for h in _hist}
                _ctx_band = dhcr_dict.get("context_band_name", "?")
                if _today not in _existing_dates:
                    _hist.append({
                        "date": _today,
                        "operative_context_tokens": dhcr_snapshot.operative_context_tokens,
                        "band": _ctx_band,
                    })
                    # v3.5.1 — was [-24:] which kept only 24 daily entries (~24 days).
                    # The "over the last quarter" trend prose needs ≥90 days of
                    # history, so prune by date instead: keep entries from the
                    # last 18 months (matches DHCR quarterly trajectory window).
                    _cutoff = (_dt.date.today() - _dt.timedelta(days=540)).isoformat()
                    _hist = sorted(
                        [h for h in _hist if h.get("date", "") >= _cutoff],
                        key=lambda h: h.get("date", "")
                    )
                    _cl_state["operative_context_history"] = _hist
                    # Atomic write
                    _cl_tmp = _cl_state_path.with_suffix(".json.tmp")
                    _cl_tmp.write_text(_json2.dumps(_cl_state, indent=2))
                    _cl_tmp.replace(_cl_state_path)

                dhcr_dict["operative_context_history"] = _hist
                _vol = _cl_state.get("volume_shares") or {}
                if _vol.get("proposal_status") == "pending_confirmation" and _vol.get("proposed"):
                    dhcr_dict["context_volume_proposal"] = {
                        "proposed": _vol["proposed"],
                        "committed": _vol.get("committed") or {},
                        "created": _vol.get("proposal_created", ""),
                        "evidence": _vol.get("proposal_evidence") or [],
                    }
                else:
                    dhcr_dict["context_volume_proposal"] = None
            else:
                dhcr_dict["operative_context_history"] = []
                dhcr_dict["context_volume_proposal"] = None
        except Exception as _cl_err:
            logger.warning(f"Could not attach context_length history to dhcr_dict: {_cl_err}")
            dhcr_dict["operative_context_history"] = []
            dhcr_dict["context_volume_proposal"] = None

        computed_metrics["dhcr_snapshot"] = dhcr_dict
        # 11-quarter trajectory for charting
        dhcr_traj = compute_dhcr_trajectory(quarters_back=11)
        computed_metrics["dhcr_trajectory"] = dhcr_traj
        # Persist for next run's QoQ comparison
        save_dhcr_state(dhcr_dict)
        # v3.4.3 — log line updated to v3.4.2 framework fields (was referencing
        # v3.4.0 fields like .direction / .frontier_hbm_gb_per_accelerator /
        # .implied_context_capacity_per_accelerator_tokens / .capacity_creation_yoy_pct
        # which no longer exist in DHCRSnapshot after the v3.4.2 matrix redesign —
        # this log would have thrown AttributeError in production)
        logger.info(
            f"DHCR computed: status={dhcr_snapshot.status} score={dhcr_snapshot.position_score}/100 "
            f"workload_cell=R{dhcr_snapshot.workload_implied_cell[0]+1}C{dhcr_snapshot.workload_implied_cell[1]+1} "
            f"({dhcr_snapshot.workload_implied_zone}) "
            f"market_cell=R{dhcr_snapshot.market_deployed_cell[0]+1}C{dhcr_snapshot.market_deployed_cell[1]+1} "
            f"({dhcr_snapshot.market_deployed_zone}) "
            f"KV={dhcr_snapshot.frontier_kv_gb_per_1m_tokens:.1f}GB/1Mtok "
            f"op_ctx={dhcr_snapshot.operative_context_tokens:,}t "
            f"vol_idx={dhcr_snapshot.volume_index:.0f} "
            f"hbm_demand={dhcr_snapshot.hbm_bit_demand_index:.0f} "
            f"sanity={dhcr_snapshot.sanity_check_confirms}/{dhcr_snapshot.sanity_check_confirms + dhcr_snapshot.sanity_check_contradicts} confirms "
            f"lpddr_dir={dhcr_dict.get('sanity_signal_lpddr_contract_direction', 'no_data')} "
            f"calibration_flag={dhcr_snapshot.calibration_flag}"
        )
    except FileNotFoundError as e:
        msg = f"DHCR data/state file missing: {e}"
        logger.warning(msg)
        computed_metrics["dhcr_snapshot"] = None
        computed_metrics["dhcr_trajectory"] = []
        computed_metrics["dhcr_error_message"] = msg
    except Exception as e:
        msg = f"DHCR computation raised {type(e).__name__}: {e}"
        logger.error(f"DHCR computation failed: {e}", exc_info=True)
        computed_metrics["dhcr_snapshot"] = None
        computed_metrics["dhcr_trajectory"] = []
        computed_metrics["dhcr_error_message"] = msg

    # v3.4.4 — Indicator 7 (Competitor dynamics) composite snapshot.
    # v3.4.4.1: aggregator step added — reads accumulated Layer 1 extraction
    # logs and updates state/competitor_state.json with refreshed measured
    # values BEFORE the snapshot is computed. The snapshot then reads from
    # the freshly-updated state file. State preserved on aggregator failure.
    try:
        from src.analysis.competitor_aggregator import aggregate_competitor_extractions
        aggregate_competitor_extractions()  # updates state/competitor_state.json
    except Exception as e:
        logger.error(f"Indicator 7 aggregator failed (state preserved): {e}", exc_info=True)

    try:
        from src.analysis.competitor_scoring import compute_competitor_snapshot
        from dataclasses import asdict
        comp_snapshot = compute_competitor_snapshot()
        if comp_snapshot is not None:
            computed_metrics["competitor_snapshot"] = asdict(comp_snapshot)
            logger.info(
                f"Indicator 7 (Competitor) computed: status={comp_snapshot.composite_status} "
                f"cumulative_eps={comp_snapshot.cumulative_eps_impact_pct:.1f}% "
                f"dual_threat_active={comp_snapshot.dual_threat_active} "
                f"subs={[(s.sub_id, s.status, f'{s.estimated_eps_impact_pct:.1f}%') for s in comp_snapshot.subs]}"
            )
        else:
            computed_metrics["competitor_snapshot"] = None
    except FileNotFoundError as e:
        msg = f"competitor_thresholds.yaml missing: {e}"
        logger.warning(f"{msg} — skipping Indicator 7")
        computed_metrics["competitor_snapshot"] = None
        computed_metrics["competitor_error_message"] = msg
    except Exception as e:
        msg = f"Competitor computation raised {type(e).__name__}: {e}"
        logger.error(f"Indicator 7 computation failed: {e}", exc_info=True)
        computed_metrics["competitor_snapshot"] = None
        computed_metrics["competitor_error_message"] = msg

    # v3.4.4 — Indicator 8 (US ADR / SPO event) composite snapshot.
    # v3.41.12 — deterministic ADR event detection runs FIRST, on the news
    # items already fetched this run: EDGAR form types ([FILING] F-1 / F-1/A /
    # RW / 424B*) and pricing/size/demand headline regexes emit extraction-log
    # entries in the aggregator's shape. (The v3.4.4.1 comment previously here
    # claimed EDGAR detection, roadshow-language, 424B pricing, and Form 4 /
    # DART insider feeds already existed — none did; the log's only producer
    # was the transcript-only deep-tier digester. The deterministic legs now
    # exist; the language-fuzzy legs arrive via the news_article extractor
    # lane. Form 4 / DART parsers remain unbuilt.)
    try:
        from src.analysis.adr_filing_detector import detect_and_log_adr_events
        detect_and_log_adr_events(news_items)
    except Exception as e:
        logger.error(f"ADR deterministic detector failed (non-fatal): {e}", exc_info=True)
    # v3.41.13 — the news_article LLM lane: one structured-output Haiku pass
    # over the run's news, extracting the language-fuzzy event fields (ADR +
    # competitor) into extraction-log entries BEFORE the aggregators read the
    # log, so news-borne events reach the state machines in the same run.
    try:
        from src.analysis.news_event_extractor import extract_and_log_news_events
        extract_and_log_news_events(ANTHROPIC_API_KEY, news_items)
    except Exception as e:
        logger.error(f"News event extractor failed (non-fatal): {e}", exc_info=True)
    try:
        from src.analysis.adr_aggregator import aggregate_adr_extractions
        aggregate_adr_extractions()  # updates state/adr_state.json
    except Exception as e:
        logger.error(f"Indicator 8 aggregator failed (state preserved): {e}", exc_info=True)

    try:
        from src.analysis.adr_event_tracking import (
            compute_adr_snapshot,
            cycle_phase_4_late_from_indicators,
        )
        from dataclasses import asdict
        # v3.41.14 — the escalation previously received ONLY the DHCR
        # snapshot ("we don't have tier_assessments yet at this point"), so
        # with DHCR >40 it could never fire on tier signals: on ADR listing
        # day the phase reached `trading` while 6/7 Tier-2s and a fired
        # Tier-3 were invisible to the gate → "No peak-coincidence
        # escalation active". Yesterday's indicator statuses (prior_state)
        # are a valid input for a trailing-30-day test and ARE available
        # here — load them.
        _prior_assessments = []
        try:
            _prior_assessments = (load_prior_state() or {}).get(
                "indicator_assessments"
            ) or []
        except Exception as _e:
            logger.warning(f"[peak-coincidence] prior-state load failed: {_e}")
        cycle_late = cycle_phase_4_late_from_indicators(
            tier_assessments=_prior_assessments,
            dhcr_snapshot=computed_metrics.get("dhcr_snapshot"),
        )
        _t2 = sum(1 for a in _prior_assessments
                  if isinstance(a, dict) and a.get("tier") == 2
                  and a.get("current_status") == "Fired")
        _t3 = sum(1 for a in _prior_assessments
                  if isinstance(a, dict) and a.get("tier") == 3
                  and a.get("current_status") == "Fired")
        _dhcr = (computed_metrics.get("dhcr_snapshot") or {}).get("position_score")
        logger.info(
            f"[peak-coincidence] dhcr_score={_dhcr} t2_fired={_t2} "
            f"t3_fired={_t3} (prior-day statuses, n={len(_prior_assessments)}) "
            f"→ cycle_phase_4_late={cycle_late}"
        )
        adr_snapshot = compute_adr_snapshot(cycle_phase_4_late=cycle_late)
        if adr_snapshot is not None:
            computed_metrics["adr_snapshot"] = asdict(adr_snapshot)
            logger.info(
                f"Indicator 8 (ADR) computed: status={adr_snapshot.composite_status} "
                f"phase={adr_snapshot.lifecycle_phase} "
                f"peak_coincidence_active={adr_snapshot.peak_coincidence_active} "
                f"subs={[(s.sub_id, s.status) for s in adr_snapshot.subs]}"
            )
        else:
            computed_metrics["adr_snapshot"] = None
    except FileNotFoundError as e:
        msg = f"adr_event_tracking.yaml missing: {e}"
        logger.warning(f"{msg} — skipping Indicator 8")
        computed_metrics["adr_snapshot"] = None
        computed_metrics["adr_error_message"] = msg
    except Exception as e:
        msg = f"ADR computation raised {type(e).__name__}: {e}"
        logger.error(f"Indicator 8 computation failed: {e}", exc_info=True)
        computed_metrics["adr_snapshot"] = None
        computed_metrics["adr_error_message"] = msg

    # ----- Step 4: Layer 2 per-signal reasoning -----
    prior_state = load_prior_state()
    prior_status_map = {
        a.get("signal_id", a.get("id")): a.get("fired_status", a.get("status"))
        for a in (prior_state.get("indicator_assessments") or [])
    }

    # v3.39.7.3 — load the persistent indicator evidence store. Each indicator
    # carries a full evidence_log + reversal_conditions so Layer 2 (Haiku) can
    # preserve prior fired state against silence in today's news. Cumulative
    # state evidence (capex announcements, multi-year LTAs, IDC forecasts,
    # supplier inventory prints) is durable — silence ≠ reversal.
    #
    # Bootstrap from May 9, 2026 framework doc lives at state/indicator_evidence.json
    # (committed to repo). After Layer 2 runs, status transitions and new
    # evidence are appended back to the store and committed via the GHA
    # workflow's commit step.
    indicator_evidence_store = load_indicator_evidence_store()
    # v3.41.15 — one-shot BU-scope repair for the Jul-20 t4_layoffs misfire
    # (Samsung smartphone-division cuts fired a memory-operations signal).
    # Runs before Layer 2 so today's reasoning sees the corrected state; the
    # Fired→Not fired transition then surfaces via the normal 24h trigger
    # log (prior_state still says Fired) — visible, not silent.
    try:
        from src.state import (
            repair_t4_layoffs_bu_misfire,
            save_indicator_evidence_store as _save_ev_store,
        )
        _rep = repair_t4_layoffs_bu_misfire(indicator_evidence_store)
        if _rep:
            _save_ev_store(indicator_evidence_store)
            logger.warning(f"[t4-repair] applied: {_rep['description']}")
    except Exception as e:
        logger.warning(f"[t4-repair] wiring failed (non-fatal): {e}")

    # v3.41.17 — first-party capex-guidance ingest: news_event_extractor's
    # capex_signals entries (company guides 2027 above 2026 — first-party
    # prints only, by the extractor's own prompt) land as structured
    # evidence on t2_capex_parabolic BEFORE Layer 2, so the card's evidence
    # leads with the print rather than analyst echoes.
    try:
        _ingest_capex_guidance_evidence(indicator_evidence_store)
    except Exception as e:
        logger.warning(f"[capex-guidance] ingest failed (non-fatal): {e}")
    if not indicator_evidence_store:
        logger.warning(
            "v3.39.7.3 evidence store empty — Layer 2 will run without "
            "prior-evidence carry-forward this run. Check state/indicator_evidence.json."
        )

    logger.info("Running Layer 2 per-signal reasoning across all 23 indicators...")
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
        indicator_evidence_store=indicator_evidence_store,  # v3.39.7.3
    )
    logger.info(f"Layer 2 produced {len(signal_reasonings)} signal reasonings")

    # v3.41.16 — deterministic Forward P/E override. t2_pe_compressed is a
    # pure-numeric signal (config: trigger_patterns=[], "Pure numeric") whose
    # values are computed every run — yet its status was decided by Haiku,
    # which on Jul 22 returned 'Not fired' "pending broader estimate
    # rollover" while its own card showed all three names below threshold
    # (SKH 4.07x<5, Samsung 3.91x<6, Micron 6.01x<8) — a direct violation of
    # the prompt's [A]-category rule ("the computed verdict IS the
    # evidence"). Prompt rules are requests; arithmetic isn't. This override
    # runs BEFORE update_evidence_after_layer2 so the store records the
    # deterministic status, mirroring the cycle-matrix injector pattern.
    try:
        _override_t2_pe_from_computed(signal_reasonings, computed_metrics)
        # v3.42.0 — computed-verdict table, pre-store leg: every indicator
        # with a deterministic verdict this run gets its Layer 2 status
        # slaved to it BEFORE the evidence store records anything. Ends the
        # per-indicator whack-a-mole (P/E in .16/.18, EPS-revisions and the
        # exec/banner split on Jul-29).
        try:
            # v3.45.0 — parse LEDGER_UPDATE lines from the plateau signal's
            # Layer 2 output, apply to the guidance ledger, then merge the
            # deterministic plateau verdict into the one-truth table.
            try:
                from src.analysis.capex_ledger import (
                    load_ledger, save_ledger, apply_ledger_updates)
                _led = load_ledger()
                _plateau_texts = [
                    " ".join(str(getattr(r, f, "") or "") for f in (
                        "reasoning", "evidence_for_change_vs_yesterday",
                        "where_we_stand"))
                    for r in (signal_reasonings or [])
                    if getattr(r, "signal_id", None) == "t3_capex_plateau"]
                if apply_ledger_updates(_plateau_texts, _led):
                    save_ledger(_led)
            except Exception as _le:
                logger.warning(f"[capex-ledger] update pass failed (non-fatal): {_le}")
            _cv = evaluate_computed_verdicts(computed_metrics, signal_reasonings)
            _cv.update(evaluate_capex_plateau_verdict())
            apply_computed_verdicts(
                signal_reasonings, _cv, status_attr="fired_status",
                id_attr="signal_id", note_attr="evidence_for_change_vs_yesterday",
                stage="pre-store")
            # v3.44.0 — latched up-only ratchet, pre-store leg: runs after
            # the verdict table so the evidence store can never record a
            # downgrade of a latched signal (the Jul-31 Fired→Partial move
            # on t2_capex_parabolic is structurally impossible now).
            _prior_cx = {a.get("id"): a.get("current_status")
                         for a in (load_prior_state() or {}).get(
                             "indicator_assessments", [])}
            apply_status_ratchet(
                signal_reasonings, _prior_cx, status_attr="fired_status",
                id_attr="signal_id", note_attr="evidence_for_change_vs_yesterday",
                stage="pre-store")
            apply_reversal_gate(
                signal_reasonings, _prior_cx, status_attr="fired_status",
                id_attr="signal_id", note_attr="evidence_for_change_vs_yesterday",
                stage="pre-store")
        except Exception as _cve:
            logger.warning(f"[computed-verdict] pre-store apply failed (non-fatal): {_cve}")
    except Exception as e:
        logger.warning(f"[pe-override] failed (non-fatal): {e}")

    # v3.41.17 — deterministic t4 business-unit gate. Prompt-level control
    # failed twice on this signal (Jul 20 fire, Jul 23 re-fire in defiance of
    # the named INVALID FIRES rule): the news feed re-delivers the echoed
    # Samsung consumer-division coverage daily via the "layoffs" trigger
    # pattern, and Layer 2 keeps firing on corroboration of an out-of-scope
    # event. Discretion is withdrawn: a Fired/Partially-fired verdict must
    # carry explicit memory-operations attribution in its evidence or it is
    # forced to Not fired. A genuine memory-side fire passes untouched.
    try:
        _gate_t4_layoffs(signal_reasonings)
    except Exception as e:
        logger.warning(f"[t4-gate] failed (non-fatal): {e}")

    # v3.39.7.3 — update the evidence store with any status transitions from
    # this Layer 2 run, then persist back to disk. The GHA workflow's commit
    # step (extended in v3.39.7.3 to include indicator_evidence.json) pushes
    # the updated state back to the repo so tomorrow's run sees today's
    # decisions. Safe-failing: any exception is logged and the brief still ships.
    try:
        update_evidence_after_layer2(indicator_evidence_store, signal_reasonings)
        save_indicator_evidence_store(indicator_evidence_store)
        logger.info(
            f"Indicator evidence store updated: {len(indicator_evidence_store)} indicators persisted"
        )
    except Exception as e:
        logger.error(f"Could not update indicator evidence store: {e}", exc_info=True)

    # ----- v3.39.7 cycle matrix step (BETWEEN Layer 2 and Layer 3) -----
    # v3.39.7.1 — moved from "between Layer 3 and email build" to here so
    # that Part 5a can inject the matrix verdict into signal_reasonings
    # BEFORE Sonnet consumes the list. Otherwise the verdict reaches the
    # email but not the exec summary, and Sonnet re-derives t1 numbers from
    # prose. Order is now: Layer 2 → cycle matrix → inject verdict into
    # signal_reasonings → Layer 3 → override indicator_assessments → email.
    #
    # Safe-failing: any exception at any sub-step is logged and Sonnet still
    # runs (with the unmodified signal_reasonings list).
    cycle_verdict = None
    cycle_matrix_chart_b64 = None
    spot_contract_chart_b64 = None
    try:
        cycle_verdict = _run_cycle_matrix_step(
            news_items=news_items,
            anthropic_client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None,
            collector=collector,
        )
    except Exception as e:
        logger.error(f"Cycle matrix step failed: {e}", exc_info=True)

    if cycle_verdict is not None:
        # Part 5a — inject matrix verdict into signal_reasonings list so
        # Sonnet's exec summary references matrix numbers via the v3.39.6
        # LAYER 2 PER-SIGNAL VERDICTS prompt block.
        try:
            _inject_matrix_verdict_into_signal_reasonings(
                signal_reasonings, cycle_verdict,
            )
        except Exception as e:
            logger.warning(
                f"Could not inject matrix verdict into signal_reasonings: {e}"
            )

    # ----- v3.4.11 — Phase 4 → Phase 5 detector (BETWEEN Layer 2 and Layer 3) -----
    # Runs after Layer 2 so signal_reasonings carry per-indicator status
    # verdicts (the detector's primary input), and before Sonnet so the
    # detector's verdict (current_phase + cycle_peak_price) is in the prompt
    # — Sonnet flips to Phase 5 framework semantics when phase=5.
    #
    # Safe-failing: if anything goes wrong here, the detector returns None
    # downstream, phase_state stays empty, Sonnet defaults to Phase 4
    # prompt, and the brief ships normally with Phase 4 semantics.
    phase_state_for_sonnet: Optional[dict] = None
    try:
        # Build pseudo-IndicatorAssessment objects from signal_reasonings.
        # The detector reads .id, .tier, .current_status — that's it. We
        # construct a SimpleNamespace adapter rather than importing
        # IndicatorAssessment (avoids a heavier coupling). Layer 2's
        # fired_status_evidence vocabulary ("Fired"/"Partially fired"/"Not fired")
        # matches IndicatorAssessment.current_status verbatim.
        from types import SimpleNamespace
        layer2_assessments = []
        for r in signal_reasonings:
            rd = r.to_dict() if hasattr(r, "to_dict") else r
            layer2_assessments.append(SimpleNamespace(
                id=rd.get("signal_id", ""),
                tier=rd.get("tier", 0),
                current_status=rd.get("fired_status", "Not fired"),
            ))

        # SK Hynix history must be available; fall back to None if not
        sk_history = None
        if SUPERCYCLE_CHART_TICKER_NAME in price_history.columns:
            sk_history = price_history[SUPERCYCLE_CHART_TICKER_NAME]

        prior_phase_state = load_phase_state()
        detection_result = evaluate_phase_for_run(
            assessments=layer2_assessments,
            sk_hynix_history=sk_history,
            prior_phase_state=prior_phase_state,
            params=PHASE_5_DETECTION,
        )
        phase_state_for_sonnet = result_to_state_dict(
            detection_result, prior_state=prior_phase_state,
        )

        logger.info(f"Phase detector: {detection_result.summary_line}")
        if detection_result.flipped_this_run:
            logger.warning(
                f"PHASE TRANSITION: → Phase {detection_result.current_phase} "
                f"({detection_result.current_phase_label})"
            )

        # Persist now so anti-flicker pending state survives even if
        # Sonnet crashes downstream.
        save_phase_state(phase_state_for_sonnet)
    except Exception as e:
        logger.error(f"Phase detector failed (safe-failing): {e}", exc_info=True)
        phase_state_for_sonnet = None

    # ----- Step 5: Layer 3 (Sonnet) synthesis -----
    # v3.39 NOTE: Earlier code constructed a `layer3_input` dict and passed it
    # as `layer2_input=` to analyze_with_llm. That kwarg has never existed on
    # the function — call was pre-existing broken since at least v3.37, masked
    # by the v3.37 upstream crashes that prevented Layer 3 from running at all.
    # Removed: dead `layer3_input` dict, bogus `layer2_input` kwarg.
    # Renamed: estimates → estimates_data, performance → performance_data
    # (matching analyze_with_llm's actual signature).
    #
    # v3.39.5: digests now threaded through to Sonnet. Closes the void
    # the v3.39 comment below previously described as a "pre-existing
    # condition" — the Mirae note bodies, transcript summaries, and other
    # Layer 1 outputs no longer get dropped before Sonnet's prompt.
    # Sonnet now has the broker's analytical body as a primary source,
    # not just news aggregators' coverage.
    #
    # v3.39.6: signal_reasonings (22 Layer 2 Haiku verdicts) AND
    # computed_metrics (rel_strength_full, capex_revenue_ratios,
    # forward_pe, dart_material_disclosures) now also threaded.
    # Closes the void where Sonnet was reasoning about indicator status
    # from news headlines instead of the actual computed verdicts —
    # cured the May 18 t3_relative_strength hallucination where Sonnet
    # wrote "5-day cumulative almost certainly past -10pp" when the
    # actual computed spread was below threshold.
    #
    # Still on the void list (deferred): naver_data, finnhub_data (raw
    # structured outputs would need Haiku A3 pre-summarization to fit
    # Sonnet's input cleanly — distinct from the verdicts which are
    # already terse). And the new data sources (KIND/KRX, Samsung IR
    # audit, DART widening).
    # v3.4.12 — load active QoQ transcript diffs (Haiku diffs computed in
    # the deep-tier digester between consecutive quarter digests for the
    # same company). Each active diff is a quarter_record dict with
    # `diff_with_prior` populated. Safe-failing — empty list on error.
    transcript_diffs_active = []
    try:
        transcript_diffs_active = list_recent_quarter_diffs(max_quarters_back=1)
        logger.info(
            f"QoQ transcript diffs loaded: {len(transcript_diffs_active)} active"
        )
    except Exception as e:
        logger.warning(f"Could not load transcript diffs (safe-failing): {e}")

    logger.info("Running Layer 3 (Sonnet) synthesis...")
    # v3.39.11 — AUTHORITATIVE 24h trigger log. Load what's already on disk
    # from prior runs (yesterday's transitions still inside the rolling 24h
    # window) and pass it to Sonnet as the SOURCE OF TRUTH for the
    # executive_brief. Pre-v3.39.11, Sonnet wrote prose based on its own
    # diff of prior_state.json vs current evidence, then fast_tier
    # overwrote new_triggers_today AFTER the fact — leaving the prose
    # potentially contradicting the banners (2026-06-12: brief opened
    # "No tier-1-5 signals fired or unfired today" while the banner showed
    # a tier-3 unfire from the same 24h window). By loading the persisted
    # log here and injecting it into Sonnet's prompt BEFORE prose is
    # written, Sonnet sees the same ground truth the banners do.
    try:
        from src.state import load_trigger_log_24h, collapse_trigger_log_for_display
        _persisted_log = load_trigger_log_24h()  # already pruned to last 24h
        authoritative_24h_log_for_sonnet = collapse_trigger_log_for_display(_persisted_log)
        logger.info(
            f"Authoritative 24h log loaded for Sonnet prompt: "
            f"{len(_persisted_log)} raw entries → {len(authoritative_24h_log_for_sonnet)} "
            f"after net-collapse"
        )
    except Exception as e:
        logger.warning(
            f"Could not load 24h trigger log for Sonnet prompt (safe-failing): {e}"
        )
        authoritative_24h_log_for_sonnet = []

    analysis = analyze_with_llm(
        api_key=ANTHROPIC_API_KEY,
        performance_data=performance,
        estimates_data=estimates_map,
        news_items=news_items,
        big_movers=big_movers,
        prior_state=prior_state,
        collector=collector,
        digests=digests,
        signal_reasonings=signal_reasonings,
        computed_metrics=computed_metrics,
        phase_state=phase_state_for_sonnet,
        transcript_diffs=transcript_diffs_active,
        authoritative_24h_log=authoritative_24h_log_for_sonnet,
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

    # v3.4.11 — Deterministic Phase 4 → Phase 5 override.
    # The detector's verdict is authoritative for current_phase /
    # current_phase_label. Sonnet may emit its own number (and is
    # instructed to emit the same one because the prompt sees
    # phase_state), but if there's any divergence the detector wins.
    # Same pattern as the v3.39.6 computed-metric override and the
    # v3.39.7.1 cycle matrix verdict override.
    if phase_state_for_sonnet:
        try:
            target_phase = int(phase_state_for_sonnet.get("current_phase", 4))
            target_label = phase_state_for_sonnet.get(
                "current_phase_label", "Boom / Euphoria (late stage)"
            )
            sonnet_phase = int(analysis.executive_summary.current_phase or 4)
            if sonnet_phase != target_phase:
                logger.warning(
                    f"Phase detector override: Sonnet emitted {sonnet_phase}, "
                    f"detector verdict is {target_phase}; using detector."
                )
            analysis.executive_summary.current_phase = target_phase
            analysis.executive_summary.current_phase_label = target_label
        except Exception as e:
            logger.warning(f"Could not apply phase override: {e}")

    # v3.39.10 — INDICATOR 7/8 COMPOSITE AUTHORITY.
    #
    # t1_competitor_dynamics (Indicator 7) and t1_us_adr_event (Indicator 8)
    # have deterministic aggregator composites (computed_metrics[*_snapshot])
    # that drive their detailed cards in section 4. But their tier-table /
    # exec-summary status comes from Layer 2 / Sonnet, which can disagree —
    # on the 2026-06-06 brief the ADR showed "Partially fired" in the exec
    # summary while section 4's composite said "Not fired". The COMPUTED-
    # METRIC OVERRIDE rule (v3.39.7.3) covers forward_pe/rel_strength/capex/
    # eps but not these two composites. Slave the assessment status to the
    # composite so section 3, the exec summary, the banners, and section 4
    # always agree. (These indicators are informational — outside the
    # section-3 phase/DHCR math — so this only affects display consistency.)
    _composite_overrides = {
        "t1_competitor_dynamics": ("competitor_snapshot", "Indicator 7 (Competitor)"),
        "t1_us_adr_event": ("adr_snapshot", "Indicator 8 (ADR)"),
    }
    for assessment in (analysis.indicator_assessments or []):
        ind_id = getattr(assessment, "id", None)
        if ind_id not in _composite_overrides:
            continue
        snapshot_key, label = _composite_overrides[ind_id]
        snapshot = computed_metrics.get(snapshot_key)
        if not isinstance(snapshot, dict):
            continue  # snapshot unavailable (aggregator errored) — leave Sonnet's value
        composite = snapshot.get("composite_status")
        if not composite:
            continue
        old = getattr(assessment, "current_status", None)
        if old != composite:
            logger.warning(
                f"{label} status override (Layer2/Sonnet→composite): "
                f"{old!r} → {composite!r}"
            )
            assessment.current_status = composite

    # v3.41.18 — deterministic status for the RENDERED t2_pe_compressed card.
    # v3.41.16's override corrected Layer 2's verdict and the evidence store
    # but never touched Sonnet's indicator_assessments — the object that
    # renders, becomes prior_state, and feeds the 24h trigger log. On Jul-23
    # (computed forward_pe EMPTY: FMP 403 + yfinance blank) Sonnet
    # self-computed 4.25x, wrote 'Fired', and that reached the reader as a
    # red NEW TIER 2 TRIGGER banner and T2 7/7 while the store said Not
    # fired. Rule: with computed data, the assessment status is the computed
    # status; with NO computed data, it is slaved to Layer 2's verdict
    # (evidence-store-governed) — Sonnet may never move this signal's status
    # on its own arithmetic. MUST run before update_trigger_log_24h so the
    # banner, log, and prior_state all see the corrected status.
    try:
        _override_t2_pe_assessment(analysis, computed_metrics, signal_reasonings)
    except Exception as e:
        logger.warning(f"[pe-override] assessment wiring failed (non-fatal): {e}")

    # v3.42.0 — computed-verdict table, post-Sonnet leg: the rendered
    # assessments (the objects that become prior_state and feed the 24h
    # trigger log) are slaved to the same deterministic verdicts the store
    # just recorded. One truth per indicator, both legs, every run.
    try:
        _cv2 = evaluate_computed_verdicts(computed_metrics, signal_reasonings)
        _cv2.update(evaluate_capex_plateau_verdict())
        apply_computed_verdicts(
            analysis.indicator_assessments or [], _cv2,
            status_attr="current_status", id_attr="id",
            note_attr="change_vs_prior", stage="assessment")
        # v3.44.0 — latched up-only ratchet, post-Sonnet leg: the rendered
        # assessments (which become prior_state and feed the 24h trigger
        # log) can never show a latched signal below its prior status.
        _prior_cx2 = {a.get("id"): a.get("current_status")
                      for a in (load_prior_state() or {}).get(
                          "indicator_assessments", [])}
        apply_status_ratchet(
            analysis.indicator_assessments or [], _prior_cx2,
            status_attr="current_status", id_attr="id",
            note_attr="change_vs_prior", stage="assessment")
        apply_reversal_gate(
            analysis.indicator_assessments or [], _prior_cx2,
            status_attr="current_status", id_attr="id",
            note_attr="change_vs_prior", stage="assessment")
    except Exception as e:
        logger.warning(f"[computed-verdict] assessment apply failed (non-fatal): {e}")

    # v3.41.17 — deterministic bear-case return anchored to current price
    # (Sonnet keeps probabilities + base/bull path estimates).
    try:
        _reanchor_bear_case(analysis, price_history)
    except Exception as e:
        logger.warning(f"[scenario-anchor] wiring failed (non-fatal): {e}")

    # v3.39.7.5 — 24h ephemeral trigger event window.
    #
    # The brief used to compute "new triggers today" via Sonnet's diff of
    # current vs prior_state.json. With idempotent re-runs (which the user
    # frequently triggers manually during the day) the second+ runs would
    # see prior_state already reflecting today's status → empty diff →
    # "No new trigger events today", losing the morning's events.
    #
    # The 24h log keeps each transition visible until 24h after first
    # observation regardless of how many manual reruns happen. Tomorrow's
    # first run sees them naturally pruned (>24h old) and the brief shows
    # "No new trigger events today" only when there have genuinely been
    # none in the last 24h.
    try:
        from src.state import update_trigger_log_24h, collapse_trigger_log_for_display
        # Build prior_status_by_id from the PREVIOUS prior_state.json
        # (loaded at the top of run_fast_tier, before this run's analysis
        # gets persisted to prior_state.json by save_state below). This is
        # the "diff target" — what was true at the start of today's processing.
        prior_status_by_id = {
            a.get("id"): a.get("current_status", "")
            for a in (prior_state.get("indicator_assessments") or [])
            if isinstance(a, dict) and a.get("id")
        }
        # v3.41.18 — attach the WHY to each transition. The authoritative-log
        # switch (v3.39.11) traded Sonnet's evidence-bearing descriptions for
        # the log's truth, and the log only knew the generic "Status changed
        # from X to Y" phrase — so the top banners lost the trigger evidence
        # the reader used to get. Source: Layer 2's own change reasoning
        # (which already carries the deterministic override notes for P/E and
        # the matrix, since those prepend to it). MUST pass through the
        # machinery-leak sanitizer — Layer-2 notes contain version tags
        # ("Per v3.41.16 rule…") that would otherwise land verbatim in the
        # most prominent spot of the email. Sanitization happens HERE, not in
        # state.py, because llm_summarizer imports state (cycle).
        evidence_by_id: dict = {}
        try:
            from src.analysis.llm_summarizer import _strip_internal_jargon
            for _r in (signal_reasonings or []):
                _rid = getattr(_r, "signal_id", None)
                if not _rid:
                    continue
                _ev = (
                    (getattr(_r, "evidence_for_change_vs_yesterday", "") or "").strip()
                    or (getattr(_r, "consensus_direction", "") or "").strip()
                )
                if not _ev:
                    continue
                _ev = _strip_internal_jargon(_ev) or ""
                if len(_ev) > 320:
                    _ev = _ev[:320].rsplit(" ", 1)[0].rstrip(",;:") + " …"
                if _ev:
                    evidence_by_id[_rid] = _ev
        except Exception as _ev_err:
            logger.warning(f"[trigger-log] evidence map build failed (non-fatal): {_ev_err}")
        log_entries = update_trigger_log_24h(
            prior_status_by_id=prior_status_by_id,
            current_assessments=analysis.indicator_assessments or [],
            evidence_by_id=evidence_by_id,
        )
        # v3.39.10 — collapse to one NET transition per indicator for the
        # banners. The stored log keeps individual transitions; this only
        # affects display, dropping round-trips (e.g. an indicator that went
        # Not fired→Partially fired→Not fired within 24h) that otherwise
        # render as two contradictory "NEW TRIGGER EVENT" callouts.
        display_entries = collapse_trigger_log_for_display(log_entries)
        # Override Sonnet's computed new_triggers_today with the log-derived
        # list. The log already covers Sonnet's case (today's transitions)
        # PLUS earlier transitions today that Sonnet wouldn't see on reruns.
        # Convert log entry shape to the dict shape ExecutiveSummary expects
        # (keys: tier, indicator_id, indicator_name, description).
        _sonnet_had = len(analysis.executive_summary.new_triggers_today or [])
        # v3.41.17 — keep old_status/new_status: this conversion previously
        # STRIPPED them ("the dict shape ExecutiveSummary expects"), which is
        # why the Jul-23 P/E banner rendered "Status: ? -> ?" and classified
        # a fresh fire as a DOWNGRADE — the direction test saw no statuses.
        analysis.executive_summary.new_triggers_today = [
            {
                "tier": e["tier"],
                "indicator_id": e["indicator_id"],
                "indicator_name": e["indicator_name"],
                "old_status": e.get("old_status"),
                "new_status": e.get("new_status"),
                "description": e["description"],
            }
            for e in display_entries
        ]
        logger.info(
            f"[trigger-log] source=authoritative entries={len(display_entries)} "
            f"(raw_24h={len(log_entries)}, sonnet_had={_sonnet_had})"
        )
    except Exception as e:
        # Safe-fail: if the log update fails for any reason, keep Sonnet's
        # computed new_triggers_today list so the brief still renders the
        # callouts for transitions Sonnet itself detected.
        logger.warning(f"Could not update 24h trigger event log: {e}")

    # v3.39.10 — EXECUTIVE-BRIEF CONTRADICTION GUARD (Issue 1, 2026-06-06).
    #
    # Sonnet has repeatedly opened executive_brief with a blanket "No signals
    # fired or unfired today" even when status changes occurred — sometimes
    # in the SAME sentence that then describes an unfire ("...; t3_relative_
    # strength reverted to 'Not fired'"). The banners (new_triggers_today)
    # are deterministic and authoritative. If they are non-empty but the
    # prose leads with a "no signals fired/unfired" claim, strip that false
    # opener and replace it with a deterministic, accurate lead. This is a
    # render-time backstop; the Sonnet prompt also forbids the phrasing.
    try:
        triggers = analysis.executive_summary.new_triggers_today or []
        brief = analysis.executive_summary.executive_brief or ""
        # Indicators 6/7/8 are WATCH OVERLAYS — a change in one of them is
        # NOT a "signal firing" in the framework's vocabulary, so prose like
        # "No new signals fired today; one watch indicator updated" is
        # legitimate and must NOT be rewritten. Only a tier-1-5 SIGNAL change
        # makes a "no signals fired or unfired" claim false.
        _watch_overlay_ids = {
            "t1_competitor_dynamics",       # Indicator 7
            "t1_us_adr_event",              # Indicator 8
            "t1_decode_hbm_captive_ratio",  # Indicator 6 (DHCR)
        }
        signal_changed_names = [
            t.get("indicator_name", t.get("indicator_id", "?"))
            for t in triggers
            if t.get("indicator_id") not in _watch_overlay_ids
        ]
        from src.state import reconcile_executive_brief_opener
        new_brief = reconcile_executive_brief_opener(brief, signal_changed_names)
        if new_brief != brief:
            analysis.executive_summary.executive_brief = new_brief
            logger.warning(
                "executive_brief opened with a 'no signals fired/unfired' "
                f"claim while {len(signal_changed_names)} tier-1-5 signal(s) "
                "changed; replaced the false opener."
            )
    except Exception as e:
        logger.warning(f"Could not run executive_brief contradiction guard: {e}")

    # ----- Step 6: Chart + email -----
    # v3.39: generate_performance_chart takes (history_baseline100, title=...).
    # Old call passed WIDER_BASKET as positional 2 (landed in title:str — would
    # render chart titled "['NVDA', 'MU', ...]") and collector=collector (kwarg
    # doesn't exist — TypeError next after analyze_with_llm). Both removed;
    # default title used.
    chart_b64 = generate_performance_chart(baseline_to_100(price_history))

    # v3.39.2: new supercycle phase chart, replaces the 1Y performance chart
    # as the lead visual in the email. The 1Y performance chart is no longer
    # rendered into the email body (the performance table immediately below
    # already carries 1D/1W/1M/3M/6M/1Y numbers — the chart was redundant).
    # The 1Y chart is still generated above because other downstream code
    # may consume it; current template just doesn't reference chart_b64.
    # See email_module/template.py:build_email_html for the new layout.
    supercycle_chart_b64 = None
    if SUPERCYCLE_CHART_TICKER_NAME in price_history.columns:
        try:
            supercycle_chart_b64 = generate_supercycle_chart(
                sk_hynix_price_history=price_history[SUPERCYCLE_CHART_TICKER_NAME],
                executive_summary=analysis.executive_summary,
                indicator_assessments=analysis.indicator_assessments,
                indicators_config=INDICATORS,
                trough_date_iso=CYCLE_TROUGH_DATE,
                phase_durations_months=CYCLE_PHASE_DURATIONS_MONTHS,
                phase_colors=CYCLE_PHASE_COLORS,
                currency=SUPERCYCLE_CHART_TICKER_CURRENCY,
            )
            if supercycle_chart_b64:
                logger.info(
                    f"Supercycle chart rendered "
                    f"({len(supercycle_chart_b64):,} base64 chars)"
                )
            else:
                logger.warning("Supercycle chart generation returned None")
        except Exception as e:
            # Non-fatal: if the chart fails we still ship the email without it.
            # Template handles supercycle_chart_base64=None by skipping the section.
            logger.error(f"Supercycle chart generation failed: {e}")
            supercycle_chart_b64 = None
    else:
        logger.warning(
            f"Supercycle chart skipped: '{SUPERCYCLE_CHART_TICKER_NAME}' "
            f"not in price_history columns ({list(price_history.columns)})"
        )

    upcoming = get_upcoming_events(within_days=90)
    logger.info(f"Calendar: {len(upcoming)} upcoming events")

    # Compute the EPS scoring verdict for the email banner + section.
    # The Layer 2 reasoner already computed it for the t2_eps_revisions signal —
    # we extract the structured verdict from its raw_evidence_dump.
    eps_scoring_verdict = None
    for r in signal_reasonings:
        if r.signal_id == "t2_eps_revisions":
            sig_data = r.raw_evidence_dump.get("signal_specific_data", {})
            eps_scoring_verdict = sig_data.get("weighted_scoring_verdict")
            # v3.5.0 — attach divergent_events so template can render them
            if eps_scoring_verdict is not None:
                eps_scoring_verdict["divergent_events"] = sig_data.get("divergent_events", [])
                eps_scoring_verdict["dedup_stats"] = sig_data.get("dedup_stats", {})
            break

    # ----- v3.39.7 cycle matrix follow-up (override + charts) -----
    # The matrix step itself runs earlier (between Layer 2 and Layer 3) so
    # that signal_reasonings can be injected before Sonnet consumes them.
    # Here we (1) override the LLM's t1_spot_decline assessment with the
    # deterministic matrix verdict, and (2) render the two cycle charts
    # for the email card. cycle_verdict is already in scope from above;
    # if it's None (matrix step failed) both calls are skipped and the
    # rest of the brief ships as usual.
    if cycle_verdict is not None:
        # Override t1_spot_decline assessment status + where_we_stand with
        # the deterministic matrix verdict. Status badge becomes
        # "Not fired" / "Partially fired" / "Fired" (the MatrixCell.value
        # strings since v3.39.7.1). Where_we_stand becomes the matrix
        # narrative — deterministic numbers, no LLM paraphrase.
        try:
            _override_t1_spot_decline_from_matrix(
                analysis.indicator_assessments, cycle_verdict,
            )
        except Exception as e:
            logger.warning(f"Could not override t1_spot_decline from matrix: {e}")

        # Render the two cycle charts (matrix dot + dual-axis spot/contract).
        try:
            cycle_matrix_chart_b64, spot_contract_chart_b64 = render_cycle_charts_from_verdict(
                verdict=cycle_verdict,
                spot_history_csv=CYCLE_MATRIX_SPOT_CSV,
                contract_forecasts_csv=CYCLE_MATRIX_FORECASTS_CSV,
            )
            logger.info("Cycle matrix charts rendered")
        except Exception as e:
            logger.error(f"Cycle chart rendering failed: {e}", exc_info=True)

    # v3.4.0 — Generate DHCR trajectory chart from the snapshot data
    # v3.4.2 — Also generate DHCR matrix chart (5x5 memory-zone visualization).
    # v3.4.9 — Matrix redesigned: y-flip, single TODAY marker + callout, no
    # overlay circles or trajectory arrow. The right-hand spectrum bar in the
    # card is inline HTML (`_build_dhcr_spectrum_bar` in template.py), not a
    # generated PNG — no separate chart generation step needed for it.
    dhcr_chart_b64 = None
    dhcr_matrix_chart_b64 = None
    dhcr_snapshot_dict = computed_metrics.get("dhcr_snapshot")
    dhcr_traj_data = computed_metrics.get("dhcr_trajectory") or []
    if dhcr_snapshot_dict:
        try:
            from src.analysis.chart_generator import (
                generate_dhcr_trajectory_chart,
                generate_dhcr_matrix_chart,
            )
            if dhcr_traj_data:
                dhcr_chart_b64 = generate_dhcr_trajectory_chart(dhcr_traj_data)
                if dhcr_chart_b64:
                    logger.info(f"DHCR trajectory chart rendered: {len(dhcr_chart_b64)} b64 chars")
            dhcr_matrix_chart_b64 = generate_dhcr_matrix_chart(
                dhcr_snapshot_dict, trajectory=dhcr_traj_data
            )
            if dhcr_matrix_chart_b64:
                logger.info(f"DHCR matrix chart rendered: {len(dhcr_matrix_chart_b64)} b64 chars")
        except Exception as e:
            logger.warning(f"DHCR chart generation failed: {e}", exc_info=True)

    # v3.4.1 Fix 5b — build signal_id-keyed dict from Layer 2 reasonings
    # so the template can fall back to L2 verdict when Sonnet's
    # where_we_stand field comes back empty (max_tokens truncation).
    signal_reasonings_by_id = {}
    for r in signal_reasonings or []:
        rd = r.to_dict() if hasattr(r, "to_dict") else r
        sid = rd.get("signal_id") or rd.get("id")
        if sid:
            signal_reasonings_by_id[sid] = rd

    html_body = build_email_html(
        tcb_verdict=tcb_verdict,
        analysis=analysis,
        performance=performance,
        chart_base64=chart_b64,
        supercycle_chart_base64=supercycle_chart_b64,
        data_source_statuses=collector.all(),
        upcoming_events=upcoming,
        rel_strength_verdict=rel_strength_verdict,
        eps_scoring_verdict=eps_scoring_verdict,
        # v3.39.7 cycle matrix card
        cycle_verdict=cycle_verdict,
        cycle_matrix_chart_b64=cycle_matrix_chart_b64,
        spot_contract_chart_b64=spot_contract_chart_b64,
        # v3.4.0 DHCR
        dhcr_snapshot=dhcr_snapshot_dict,
        dhcr_chart_b64=dhcr_chart_b64,
        # v3.4.2 — matrix chart (5x5 memory-zone visualization)
        dhcr_matrix_chart_b64=dhcr_matrix_chart_b64,
        # v3.4.4 — Indicator 7 (Competitor) + Indicator 8 (ADR) section 4 cards
        competitor_snapshot=computed_metrics.get("competitor_snapshot"),
        adr_snapshot=computed_metrics.get("adr_snapshot"),
        # v3.7.3 — when any section-4 deep-dive snapshot is None, the
        # template renders a "Computation failed" placeholder card instead
        # of silently skipping. These error_message kwargs surface the
        # actual exception text so the placeholder body shows the diagnosis
        # ("NameError: ...", "FileNotFoundError: ...") to the operator on
        # the email itself rather than only in a buried workflow log.
        dhcr_error_message=computed_metrics.get("dhcr_error_message"),
        competitor_error_message=computed_metrics.get("competitor_error_message"),
        adr_error_message=computed_metrics.get("adr_error_message"),
        # v3.4.1 Fix 5b — Layer 2 fallback for empty where_we_stand
        signal_reasonings_by_id=signal_reasonings_by_id,
    )

    # Send
    try:
        # v3.39: send_email takes (subject, html_body, recipient=None).
        # v3.39.11: subject is the date only. Earlier (v3.39.7.1) the
        # t1_spot_decline matrix verdict was appended as "[Not fired|Partially
        # fired|Fired]" — but that one cell value is misleading at the email
        # subject level (it's a per-indicator status, not a brief-level
        # summary; on 2026-06-12 the brief was tagged "[Not fired]" while a
        # tier-3 signal had actually unfired that day, making the subject
        # read as if nothing had happened). Removed per user request.
        subject = f"Memory Cycle Brief — {today.strftime('%a %b %d')}"
        send_email(
            subject=subject,
            html_body=html_body,
        )
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        # Save HTML to repo for manual retrieval
        failed_path = Path(f"state/failed_send_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.html")
        failed_path.parent.mkdir(exist_ok=True)
        failed_path.write_text(html_body)
        logger.error(f"Saved failed-send body to {failed_path}")
        return 1

    # ----- Step 7: Mark idempotent + save state for tomorrow's diff -----
    mark_brief_sent_today(today)
    save_state(analysis)

    # v3.41.2 — the indicator-prose cache is now LIVE inside sonnet_analyze
    # (decision + scope-restriction before Sonnet, merge + cache persist after).
    # The former post-send SHADOW block here has been removed; toggle the whole
    # feature with USE_PROSE_CACHE in config (off → full regeneration, no cache).

    duration = (datetime.now(UTC) - started_at).total_seconds()
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

# ============================================================================
# v3.39.7 — Cycle matrix helpers
# ============================================================================

def _run_cycle_matrix_step(
    news_items: list,
    anthropic_client,
    collector: DiagnosticsCollector,
) -> MatrixVerdict | None:
    """Scrape spot+contract, extract structured signals, evaluate the matrix.

    Safe-failing: each sub-step is wrapped in try/except. Returns the
    MatrixVerdict (or None if matrix evaluation itself fails). Persistence
    runs even if Haiku extraction fails so spot/contract CSVs still grow.
    """
    logger.info("=== Cycle matrix step (v3.39.7) ===")
    CYCLE_MATRIX_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Spot scrape (structured)
    try:
        spot = scrape_trendforce_spot_structured(collector=collector)
        if spot.rows:
            n = persist_spot_to_history(spot, CYCLE_MATRIX_SPOT_CSV)
            collector.record(SourceFetchStatus(
                "TrendForce spot (structured)", "scrape", True,
                len(spot.rows), 0,
                detail=f"{len(spot.rows)} SKUs as of {spot.update_date}; "
                       f"{n} rows persisted; fresh={spot.fresh}",
            ))
            logger.info(f"  Spot: {len(spot.rows)} SKUs persisted (fresh={spot.fresh})")
        else:
            collector.record(SourceFetchStatus(
                "TrendForce spot (structured)", "scrape", False, 0, 1,
                detail="0 rows scraped",
            ))
    except Exception as e:
        logger.error(f"  Spot scrape failed: {e}")
        collector.record(SourceFetchStatus(
            "TrendForce spot (structured)", "scrape", False, 0, 1,
            detail=f"error: {e}",
        ))

    # 2. Contract scrape
    try:
        contract = scrape_trendforce_contract(collector=collector)
        if contract.rows:
            n = persist_contract_to_history(contract, CYCLE_MATRIX_CONTRACT_ACTUALS_CSV)
            collector.record(SourceFetchStatus(
                "TrendForce contract", "scrape", True,
                len(contract.rows), 0,
                detail=f"{len(contract.rows)} SKUs ({contract.period_label}) "
                       f"as of {contract.update_date}; {n} rows persisted",
            ))
            logger.info(f"  Contract: {len(contract.rows)} SKUs persisted ({contract.period_label})")
        else:
            collector.record(SourceFetchStatus(
                "TrendForce contract", "scrape", False, 0, 1, detail="0 rows scraped",
            ))
    except Exception as e:
        logger.error(f"  Contract scrape failed: {e}")
        collector.record(SourceFetchStatus(
            "TrendForce contract", "scrape", False, 0, 1, detail=f"error: {e}",
        ))

    # 2a. v3.4.13 — LPDDR contract direction scrape.
    # Reuses the same TrendForce news category page that step 1 of this
    # cycle matrix flow already touches (different selector / different
    # downstream consumer). Result is persisted to lpddr_contract_history.csv
    # and the latest direction is stashed on the collector for DHCR's
    # sanity-check rendering. Status is explicit (data_extracted /
    # no_lpddr_content_in_releases / parser_no_match / fetch_failed) so
    # "no data" is distinguishable from "scraper broken" in diagnostics.
    try:
        from src.data.news import (
            scrape_trendforce_lpddr_contract,
            persist_lpddr_contract_to_history,
        )
        lpddr_result = scrape_trendforce_lpddr_contract(collector=collector)
        if lpddr_result.signals:
            n_persisted = persist_lpddr_contract_to_history(lpddr_result)
            logger.info(
                f"  LPDDR: {lpddr_result.rows_extracted} signal(s) extracted, "
                f"{n_persisted} row(s) persisted, "
                f"latest direction={lpddr_result.latest_signal.direction if lpddr_result.latest_signal else 'n/a'}"
            )
        else:
            logger.info(
                f"  LPDDR: no signals extracted "
                f"(status={lpddr_result.status}, pages_checked={lpddr_result.pages_checked})"
            )
        # Stash result for DHCR rendering downstream.
        # Stored on the collector so it doesn't require changing more sigs.
        setattr(collector, "_lpddr_scrape_result", lpddr_result)
    except Exception as e:
        logger.error(f"  LPDDR scrape failed (safe-failing): {e}")

    # 3. Haiku extraction over DRAM-relevant news (cost-capped)
    if anthropic_client is not None and news_items:
        try:
            already_seen = _load_processed_signal_urls(CYCLE_MATRIX_SIGNALS_CSV)
            # v3.46.1 — one-time seeds: the two public 3Q26 forecast press
            # releases the old keyword gate missed. already_seen makes this
            # a no-op after first ingestion.
            _seed_press = [
                ("AI Server Demand Continues to Support Memory Prices in 3Q26, "
                 "but Gains Moderate as Consumer Demand Weakens and High Base "
                 "Effects Take Hold, Says TrendForce",
                 "https://www.trendforce.com/presscenter/news/20260703-13134.html"),
                ("Long-Term Agreements Cap Price Increases; Server DRAM Contract "
                 "Prices Expected to Rise 13-18% QoQ in 3Q26, Says TrendForce",
                 "https://www.trendforce.com/presscenter/news/20260709-13140.html"),
            ]
            from types import SimpleNamespace as _NS
            seeded = [_NS(title=t, url=u, source="TrendForce Press Center (seed)")
                      for t, u in _seed_press if u not in already_seen]
            if seeded:
                logger.info(f"  [cycle-matrix] seeding {len(seeded)} missed "
                            f"TrendForce press release(s) for one-time ingestion")
            relevant = (seeded + [
                n for n in news_items
                if _is_dram_relevant_v3397(getattr(n, "title", ""))
                and getattr(n, "url", "") not in already_seen
            ])[:CYCLE_MATRIX_MAX_EXTRACTIONS_PER_RUN]

            logger.info(f"  Haiku extraction over {len(relevant)} new DRAM articles")
            new_signals, new_forecasts = [], []
            for item in relevant:
                body = fetch_article_body(item.url)
                if not body:
                    continue
                signal = extract_dram_signal_from_article(
                    client=anthropic_client,
                    title=item.title,
                    article_body=body,
                    url=item.url,
                    published_date=item.published.date() if item.published else None,
                )
                if signal is None:
                    continue
                new_signals.append(signal)

                if signal.get("is_quarterly_forecast") or "/presscenter/" in item.url:
                    forecasts = extract_dram_forecasts_from_press_release(
                        client=anthropic_client,
                        title=item.title, article_body=body, url=item.url,
                        published_date=item.published.date() if item.published else None,
                    )
                    new_forecasts.extend(forecasts)

            if new_signals:
                n = upsert_contract_signals(new_signals, CYCLE_MATRIX_SIGNALS_CSV)
                logger.info(f"  Signals: {n} written")
            if new_forecasts:
                n = upsert_contract_forecasts(new_forecasts, CYCLE_MATRIX_FORECASTS_CSV)
                logger.info(f"  Forecasts: {n} written")
        except Exception as e:
            logger.error(f"  Haiku extraction failed: {e}", exc_info=True)

    # 4. Evaluate matrix
    try:
        verdict = evaluate_matrix(
            spot_history=CYCLE_MATRIX_SPOT_CSV,
            contract_forecasts=CYCLE_MATRIX_FORECASTS_CSV,
            contract_signals=CYCLE_MATRIX_SIGNALS_CSV,
            as_of=datetime.now(UTC),
        )
        logger.info(f"  Verdict: {verdict.cell_label}")
        for w in (verdict.warnings or []):
            logger.warning(f"    ! {w}")
        return verdict
    except Exception as e:
        logger.error(f"  Matrix evaluation failed: {e}", exc_info=True)
        return None


def _override_t1_spot_decline_from_matrix(
    indicator_assessments: list, verdict: MatrixVerdict,
) -> None:
    """Override t1_spot_decline status and where_we_stand from the matrix verdict.

    Maps Hold→Not fired, Warn→Partially fired, Fire→Fired so the status
    badge in the email matches the matrix cell. Where_we_stand is replaced
    with the matrix narrative so the cell/drawdown/synthetic numbers are
    deterministic, not LLM-paraphrased.
    """
    cell_value = getattr(verdict.cell, "value", "")
    new_status = CYCLE_MATRIX_STATUS_MAP.get(cell_value, "Unknown (LLM unavailable)")
    for a in indicator_assessments:
        if a.id == "t1_spot_decline":
            old_status = a.current_status
            a.current_status = new_status
            a.where_we_stand = (verdict.narrative or a.where_we_stand)[:800]
            logger.info(
                f"  t1_spot_decline status overridden: '{old_status}' -> '{new_status}' "
                f"(matrix cell: {cell_value})"
            )
            return
    logger.warning("  t1_spot_decline not found in indicator_assessments")


def _is_dram_relevant_v3397(title: str) -> bool:
    """Heuristic: should this article be sent to Haiku for signal extraction?"""
    if not title:
        return False
    keywords = ["dram", "ddr5", "ddr4", "hbm", "memory contract",
                "memory spot", "contract price", "spot price",
                "samsung", "sk hynix", "micron", "qoq", "forecast",
                # v3.46.1 — TrendForce retitled their quarterly forecast
                # releases ("...Support Memory Prices in 3Q26...") so the
                # Jul-3 3Q26 forecast matched ZERO keywords and the anchor
                # went 130d stale. Named counterexample in tests.
                "memory price", "price forecast"]
    low = title.lower()
    return any(kw in low for kw in keywords)


def _load_processed_signal_urls(signals_csv) -> set:
    """Read signals CSV; return URLs already processed (don't re-Haiku them)."""
    if not signals_csv.exists() or signals_csv.stat().st_size == 0:
        return set()
    try:
        df = pd.read_csv(signals_csv, usecols=["source_url"])
        return set(df["source_url"].dropna().astype(str).tolist())
    except Exception as e:
        logger.warning(f"Could not load processed URLs: {e}")
        return set()


_T4_GATE_MEMORY_TERMS = (
    "dram", "nand", "hbm", "memory fab", "memory division", "memory workforce",
    "memory operations", "memory business", "memory employees", "ds-memory",
    "ds division", "fab idl", "fab utilization", "wafer line",
    "semiconductor workforce",
)


def _ingest_capex_guidance_evidence(indicator_evidence_store: dict) -> None:
    """v3.41.17 — feed first-party capex guidance into t2_capex_parabolic.

    Reads today's extraction log for entries carrying `capex_signals` with
    `guides_2027_capex_above_2026` true (the extractor's prompt restricts
    these to the company's OWN prints), and appends a deduped evidence entry
    to the t2_capex_parabolic store so Layer 2 and the card cite the print,
    not an analyst echo. Dedup key: company + 'guides 2027' in existing
    evidence values. No-ops safely on missing store/log.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path
    from src.state import EvidenceEntry as _EE
    ev = (indicator_evidence_store or {}).get("t2_capex_parabolic")
    if ev is None:
        return
    today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    log_path = _Path("state") / f"extraction_log_{today}.jsonl"
    if not log_path.exists():
        return
    existing = " ".join(
        str(getattr(e, "value", "")) for e in (getattr(ev, "evidence_log", None) or [])
    ).lower()
    n = 0
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except Exception:
            continue
        sig = entry.get("capex_signals") or {}
        if sig.get("guides_2027_capex_above_2026") is not True:
            continue
        company = (sig.get("company") or "hyperscaler").strip()
        dedup_key = f"{company.lower()} guides 2027"
        if dedup_key in existing:
            continue
        note = (sig.get("note") or "").strip()
        g26 = sig.get("capex_2026_guidance_usd_b")
        ev.evidence_log.append(_EE(
            date=today,
            type="data_point",
            value=(
                f"FIRST-PARTY: {company} guides 2027 CapEx above 2026 "
                f"(own print"
                + (f"; 2026 guided ${g26:g}B" if isinstance(g26, (int, float)) else "")
                + (f"; \"{note[:120]}\"" if note else "")
                + "). Bull-confirm threshold input — analyst forecasts do "
                  "not satisfy this threshold."
            ),
            source=str(entry.get("source") or "news_article"),
            persistence="quarterly",
            reversal_condition=f"{company} guides 2027 capex below 2026",
        ))
        existing += " " + dedup_key
        n += 1
    if n:
        logger.info(f"[capex-guidance] ingested {n} first-party guidance event(s)")


def _gate_t4_layoffs(signal_reasonings: list) -> None:
    """v3.41.17 — hard BU gate for t4_layoffs (same pattern as the P/E override).

    A Fired/Partially-fired verdict must carry explicit memory-operations
    attribution in its own evidence (DRAM/NAND/HBM, memory fab/division/
    workforce, DS-Memory…). Bare "memory chip maker Samsung" press phrasing
    does not qualify — only operations-specific compounds count. Absent
    attribution → forced Not fired with a logged note. Never touches a
    Not-fired verdict or one with genuine memory-side evidence.
    """
    import json as _json
    for r in signal_reasonings:
        if getattr(r, "signal_id", None) != "t4_layoffs":
            continue
        st = getattr(r, "fired_status", "")
        if st not in ("Fired", "Partially fired"):
            logger.info(f"[t4-gate] status={st!r} — pass-through")
            return
        parts = [str(getattr(r, "consensus_direction", "") or "")]
        parts += [str(x) for x in (getattr(r, "fired_status_evidence", None) or [])]
        parts += [str(x) for x in (getattr(r, "key_quotes_consolidated", None) or [])]
        parts.append(str(getattr(r, "evidence_for_change_vs_yesterday", "") or ""))
        try:
            parts.append(_json.dumps(getattr(r, "raw_evidence_dump", {}) or {}))
        except Exception:
            pass
        blob = " ".join(parts).lower()
        if any(t in blob for t in _T4_GATE_MEMORY_TERMS):
            logger.info(
                f"[t4-gate] memory-operations attribution present — "
                f"'{st}' stands"
            )
            return
        note = (
            f"Deterministic BU gate (v3.41.17): Layer 2 returned '{st}' but "
            f"the evidence contains no explicit memory-operations attribution "
            f"(DRAM/NAND/HBM, memory fab/division/workforce, DS-Memory). "
            f"Non-memory-BU events cannot fire this signal; status forced to "
            f"Not fired."
        )
        r.fired_status = "Not fired"
        r.evidence_for_change_vs_yesterday = (
            note + " " + (getattr(r, "evidence_for_change_vs_yesterday", "") or "")
        ).strip()
        try:
            r.fired_status_evidence = [note] + list(
                getattr(r, "fired_status_evidence", None) or []
            )
        except Exception:
            pass
        logger.warning(
            f"[t4-gate] FORCED Not fired (was {st!r}) — no memory-operations "
            f"attribution in evidence"
        )
        return


def _reanchor_bear_case(analysis, price_history) -> None:
    """v3.41.17 bear-only re-anchor, superseded v3.47.0: ALL THREE cases
    re-anchor from the cycle peak. Sonnet's static from-current numbers,
    authored for Phase-5 entry near the peak, double-count realized
    drawdown (Aug-12 brief: bear -18% vs base -40% — bear better than
    base). Now: trough_i = peak x (1 + anchor_i/100); return_i =
    trough_i vs CURRENT close, capped at 0; ordering bear <= base <= bull
    holds by anchor construction. Months-to-trough derived from each
    case's own window midpoint (kills "trough in 0mo ... Q1-Q3 2027").
    Probabilities and window strings stay Sonnet's.
    """
    try:
        from src.config import (PHASE5_TROUGH_ANCHORS_FROM_PEAK,
                                BEAR_TROUGH_BAND_PCT)
        if price_history is None or getattr(price_history, "empty", True):
            return
        if "SK Hynix" not in getattr(price_history, "columns", []):
            return
        series = price_history["SK Hynix"].dropna()
        if len(series) < 30:
            return
        current = float(series.iloc[-1])
        peak = float(series.max())
        if current <= 0 or peak <= 0:
            return
        drawdown_now_pct = (current / peak - 1.0) * 100.0
        es = analysis.executive_summary

        def _from_here(anchor_pct: float) -> float:
            trough = peak * (1.0 + anchor_pct / 100.0)
            return min(0.0, (trough / current - 1.0) * 100.0)

        def _months_from_window(case) -> "float | None":
            import re as _re
            from datetime import date as _date
            mids = []
            for s in (getattr(case, "peak_window_start", "") or "",
                      getattr(case, "peak_window_end", "") or ""):
                m = _re.search(r"Q([1-4])\s*(20\d\d)", str(s))
                if m:
                    mids.append(_date(int(m.group(2)),
                                      {1: 2, 2: 5, 3: 8, 4: 11}[int(m.group(1))], 15))
            if not mids:
                return None
            mid = mids[0] if len(mids) == 1 else _date.fromordinal(
                (mids[0].toordinal() + mids[1].toordinal()) // 2)
            return max(0.0, round((mid - _date.today()).days / 30.44, 1))

        applied = {}
        for name, anchor in PHASE5_TROUGH_ANCHORS_FROM_PEAK.items():
            case = getattr(es, f"{name}_case", None)
            if case is None:
                continue
            old = getattr(case, "return_to_peak_pct", None)
            case.return_to_peak_pct = _from_here(anchor)
            months = _months_from_window(case)
            if months is not None:
                case.peak_months_away = months
            applied[name] = (old, case.return_to_peak_pct, months)
        # ordering sanity (anchors guarantee it; assert loudly if not)
        try:
            b, ba, bu = (es.bear_case.return_to_peak_pct,
                         es.base_case.return_to_peak_pct,
                         es.bull_case.return_to_peak_pct)
            if not (b <= ba <= bu):
                logger.warning(f"[scenario-anchor] ordering violated after "
                               f"re-anchor: bear={b:+.1f} base={ba:+.1f} "
                               f"bull={bu:+.1f}")
        except Exception:
            pass
        setattr(es, "scenario_anchor", {
            "current_price": current,
            "peak_price": peak,
            "drawdown_now_pct": drawdown_now_pct,
            "bear_trough_from_peak_pct": PHASE5_TROUGH_ANCHORS_FROM_PEAK["bear"],
            "anchors_from_peak": dict(PHASE5_TROUGH_ANCHORS_FROM_PEAK),
            "band": tuple(BEAR_TROUGH_BAND_PCT),
            "all_cases_reanchored": True,
        })
        logger.info(
            f"[scenario-anchor] SKH current={current:,.0f} peak={peak:,.0f} "
            f"({drawdown_now_pct:+.1f}% off peak); re-anchored all cases "
            + "; ".join(f"{k}: {v[0] if v[0] is None else format(v[0], '+.0f')}%"
                        f" -> {v[1]:+.1f}% ({v[2]}mo)"
                        for k, v in applied.items())
        )
    except Exception as e:
        logger.warning(f"[scenario-anchor] failed (non-fatal): {e}")


def _override_t2_pe_assessment(analysis, computed_metrics: dict,
                               signal_reasonings: list) -> None:
    """v3.41.18 — slave the RENDERED t2_pe_compressed status to a
    deterministic source. Companion to _override_t2_pe_from_computed (which
    corrects Layer 2's verdict + the store): this one corrects the
    IndicatorAssessment Sonnet emitted, closing the last leg where the
    on-screen status could diverge from every deterministic truth.

    - Computed forward_pe present (>=1 name): status = Fired if any name is
      below its threshold, else Not fired (same rule as the Layer-2
      override, so the two never disagree).
    - Computed forward_pe EMPTY: status = Layer 2's verdict for
      t2_pe_compressed (post-override, evidence-store-governed). Sonnet's
      own arithmetic is never allowed to set this signal's status.
    """
    assessments = getattr(analysis, "indicator_assessments", None) or []
    target = next((a for a in assessments
                   if getattr(a, "id", None) == "t2_pe_compressed"), None)
    if target is None:
        return

    pe = (computed_metrics or {}).get("forward_pe") or {}
    thresholds = [
        ("SK Hynix", "000660.KS", 5.0),
        ("Samsung", "005930.KS", 6.0),
        ("Micron", "MU", 8.0),
    ]
    breaches, readings, n_with_data = [], [], 0
    for label, ticker, thr in thresholds:
        v = pe.get(ticker)
        if v is None:
            continue
        n_with_data += 1
        readings.append(f"{label} {v:.2f}x({'<' if v < thr else '>='}{thr:g})")
        if v < thr:
            breaches.append(label)

    if n_with_data > 0:
        status = "Fired" if breaches else "Not fired"
        basis = f"computed forward P/E — {'; '.join(readings)}"
    else:
        l2 = next((r for r in (signal_reasonings or [])
                   if getattr(r, "signal_id", None) == "t2_pe_compressed"), None)
        status = getattr(l2, "fired_status", None) if l2 is not None else None
        if status not in ("Fired", "Partially fired", "Not fired"):
            logger.info(
                "[pe-override] assessment: no computed data and no Layer 2 "
                "verdict — leaving Sonnet's status untouched"
            )
            return
        basis = (
            "no computed forward P/E available this run — status governed by "
            "the evidence-backed verdict; a data-driven fire requires live "
            "values below threshold (SK Hynix <5x, Samsung <6x, Micron <8x)"
        )

    old = getattr(target, "current_status", None)
    if old != status:
        target.current_status = status
        note = f"Status set deterministically ({basis})."
        prior_cvp = getattr(target, "change_vs_prior", None)
        target.change_vs_prior = (
            note if not prior_cvp else f"{note} {prior_cvp}"
        )
        logger.warning(
            f"[pe-override] assessment: {old!r} → {status!r} ({basis})"
        )
    else:
        logger.info(f"[pe-override] assessment agrees: {status!r} ({basis})")


def _override_t2_pe_from_computed(signal_reasonings: list, computed_metrics: dict) -> None:
    """v3.41.16 — hard status override for the pure-numeric P/E signal.

    Rule (per config trigger_numeric + the reasoner's [A]-category text,
    which treats a single-name breach as sufficient): Fired if ANY name is
    below its threshold — SK Hynix <5x, Samsung <6x, Micron <8x — Not fired
    if none are (among names with data). No data at all → leave Layer 2's
    verdict untouched. Haiku keeps writing the prose; the status is
    arithmetic.
    """
    pe = (computed_metrics or {}).get("forward_pe") or {}
    thresholds = [
        ("SK Hynix", "000660.KS", 5.0),
        ("Samsung", "005930.KS", 6.0),
        ("Micron", "MU", 8.0),
    ]
    breaches, readings = [], []
    n_with_data = 0
    for label, ticker, thr in thresholds:
        v = pe.get(ticker)
        if v is None:
            continue
        n_with_data += 1
        readings.append(f"{label} {v:.2f}x({'<' if v < thr else '>='}{thr:g})")
        if v < thr:
            breaches.append(label)
    if n_with_data == 0:
        return
    status = "Fired" if breaches else "Not fired"
    for r in signal_reasonings:
        if getattr(r, "signal_id", None) != "t2_pe_compressed":
            continue
        old = getattr(r, "fired_status", None)
        if old != status:
            r.fired_status = status
            note = (
                f"Deterministic override (v3.41.16): computed forward P/E is "
                f"authoritative for this pure-numeric signal — "
                f"{'; '.join(readings)} → {status}. Layer 2 had returned "
                f"'{old}'."
            )
            r.evidence_for_change_vs_yesterday = (
                note + " " + (getattr(r, "evidence_for_change_vs_yesterday", "") or "")
            ).strip()
            try:
                r.fired_status_evidence = [note] + list(
                    getattr(r, "fired_status_evidence", None) or []
                )
            except Exception:
                pass
        logger.info(
            f"[pe-override] {'; '.join(readings)} → {status}"
            + (f" (was: {old})" if old != status else " (Layer 2 agreed)")
        )
        return


def _inject_matrix_verdict_into_signal_reasonings(
    signal_reasonings: list, verdict: MatrixVerdict,
) -> None:
    """Part 5a — construct a SignalReasoning for t1_spot_decline from the matrix
    verdict and REPLACE any existing entry in signal_reasonings.

    Sonnet's v3.39.6 prompt iterates over signal_reasonings to build the
    LAYER 2 PER-SIGNAL VERDICTS block. Without this injection the matrix
    verdict reaches the email card and the indicator badge but not the
    Sonnet prompt — forcing Sonnet to re-derive t1_spot_decline numbers
    from prose in indicator_assessments. With it, Sonnet can reference
    cell, cell_label, contract_row, spot_col, synthetic_central_pct,
    drawdown_pct, anchor_quarter, anchor_age_days, weeks_since_peak, and
    the three adjustment values verbatim from raw_evidence_dump.

    Mutates signal_reasonings in-place. Returns None.
    """
    from src.analysis.signal_reasoner import SignalReasoning

    # Status string IS the cell value since v3.39.7.1 ("Not fired" / etc).
    status = verdict.cell.value

    # Convergence + confidence reflect the matrix's evidence base.
    # We have 3+ signals over a 4w lookback feeding tone_adj, so call it
    # "high" when there are signals, "medium" if only spot_lead has data,
    # "low" otherwise.
    adj_with_data = sum(1 for a in verdict.contract.adjustments if a.sources_count > 0)
    if adj_with_data >= 2:
        convergence = "high"
        confidence = "high"
    elif adj_with_data == 1:
        convergence = "medium"
        confidence = "medium"
    else:
        convergence = "low"
        confidence = "low"

    # Source list for the LAYER 2 block — show what the matrix consumed.
    evidence_sources = []
    for adj in verdict.contract.adjustments:
        if adj.sources_count > 0:
            evidence_sources.append(
                f"{adj.component}: {adj.pp:+.1f}pp ({adj.sources_count} sources)"
            )
    if not evidence_sources:
        evidence_sources = ["matrix evaluated with no fresh signals in lookback"]

    # Build the raw evidence dump — Sonnet's prompt format shows this
    # under "signal_specific_data" for each Layer 2 reasoning.
    raw_dump = {
        "signal_specific_data": {
            "matrix_verdict": {
                "cell": verdict.cell.value,
                "cell_label": verdict.cell_label,
                "contract_row": verdict.contract.contract_row,
                "spot_col": verdict.spot.spot_column,
                "synthetic_central_pct": verdict.contract.synthetic_central_pct,
                "synthetic_low_pct": verdict.contract.synthetic_low_pct,
                "synthetic_high_pct": verdict.contract.synthetic_high_pct,
                "anchor_quarter": verdict.contract.anchor_quarter,
                "anchor_central_pct": verdict.contract.anchor_central_pct,
                "anchor_age_days": verdict.contract.anchor_age_days,
                "drawdown_pct": verdict.spot.drawdown_pct,
                "current_session_avg": verdict.spot.current_session_avg,
                "rolling_peak_price": verdict.spot.rolling_peak_price,
                "rolling_peak_date": (
                    verdict.spot.rolling_peak_date.isoformat()
                    if verdict.spot.rolling_peak_date else None
                ),
                "weeks_since_peak": verdict.spot.weeks_since_peak,
                "adjustments": [
                    {
                        "component": a.component,
                        "pp": a.pp,
                        "weight": a.weight,
                        "sources_count": a.sources_count,
                        "notes": a.notes,
                    }
                    for a in verdict.contract.adjustments
                ],
            },
        },
        "warnings": list(verdict.warnings or []),
    }

    sr = SignalReasoning(
        signal_id="t1_spot_decline",
        signal_name="DRAM Price Evolution (spot + contract matrix)",
        tier=1,
        evidence_count=adj_with_data,
        evidence_sources=evidence_sources,
        consensus_direction=(
            f"Matrix {verdict.cell_label}: synthetic contract "
            f"{verdict.contract.synthetic_central_pct:.1f}% QoQ, spot "
            f"{verdict.spot.drawdown_pct:+.2f}% from rolling 12w peak"
            if verdict.contract.synthetic_central_pct is not None
            else f"Matrix {verdict.cell_label}: anchor unavailable"
        ),
        convergence=convergence,
        fired_status=status,
        fired_status_evidence=[
            verdict.narrative[:300] if verdict.narrative else "",
        ],
        evidence_for_change_vs_yesterday="(matrix verdict computed fresh each run)",
        key_quotes_consolidated=[],
        confidence=confidence,
        raw_evidence_dump=raw_dump,
    )

    # Replace any existing t1_spot_decline entry in-place; otherwise append.
    for i, r in enumerate(signal_reasonings):
        if getattr(r, "signal_id", None) == "t1_spot_decline":
            signal_reasonings[i] = sr
            logger.info(
                f"  Matrix verdict injected into signal_reasonings "
                f"(replaced existing t1_spot_decline entry; status={status})"
            )
            return
    signal_reasonings.append(sr)
    logger.info(
        f"  Matrix verdict injected into signal_reasonings "
        f"(appended new t1_spot_decline entry; status={status})"
    )


# ============================================================================
# Module entry point
# ============================================================================

if __name__ == "__main__":
    sys.exit(run_fast_tier())
