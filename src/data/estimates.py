"""
Analyst estimate data fetching.

Uses yfinance as the free baseline. Optionally augmented with FMP
(Financial Modeling Prep) if FMP_API_KEY is set.

The most important metric for the cycle framework is the FY27 / FY28
forward EPS estimate trajectory - whether it's still being revised up
(bull) or has flipped negative (Tier 2 signal fired).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
import yfinance as yf

from src.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


@dataclass
class EstimateSnapshot:
    """Snapshot of consensus EPS estimates for a single ticker."""
    ticker: str
    name: str
    fy_current_eps_estimate: Optional[float]
    fy_next_eps_estimate: Optional[float]      # FY+1 (likely FY27 for our names)
    fy_next2_eps_estimate: Optional[float]     # FY+2 (likely FY28)
    num_analysts: Optional[int]
    avg_target_price: Optional[float]
    high_target_price: Optional[float]
    low_target_price: Optional[float]
    forward_pe: Optional[float]
    upgrades_30d: Optional[int] = None
    downgrades_30d: Optional[int] = None


def fetch_estimates_yfinance(ticker_symbol: str, ticker_name: str) -> EstimateSnapshot:
    """Fetch consensus estimates and analyst data from yfinance."""
    try:
        t = yf.Ticker(ticker_symbol)
        info = t.info
    except Exception as e:
        logger.warning(f"Failed to fetch yfinance info for {ticker_symbol}: {e}")
        info = {}

    # yfinance estimate fields
    fy_current = info.get("forwardEps") or info.get("trailingEps")
    avg_target = info.get("targetMeanPrice")
    high_target = info.get("targetHighPrice")
    low_target = info.get("targetLowPrice")
    num_analysts = info.get("numberOfAnalystOpinions")
    forward_pe = info.get("forwardPE")

    # v3.41.18 — deterministic fallback. On Jul-23/25 yfinance returned no
    # forwardPE for any of the three memory names (and FMP 403'd), so
    # computed_metrics["forward_pe"] was EMPTY, the v3.41.16 [pe-override]
    # never ran, and t2_pe_compressed's status fell back to LLM discretion —
    # Sonnet self-computed 4.25x and fired a Tier-2 trigger the deterministic
    # path could not verify. When forwardPE is absent, compute it here from
    # the same source's own price and forward EPS (both fields carry the
    # ticker's native currency, so the ratio is unit-safe). The [pe-data]
    # line makes the source of every P/E greppable per run.
    pe_source = "yf.forwardPE" if forward_pe is not None else None
    if forward_pe is None:
        fwd_eps = info.get("forwardEps")
        px = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("regularMarketPreviousClose")
        )
        try:
            if fwd_eps and px and float(fwd_eps) > 0:
                forward_pe = float(px) / float(fwd_eps)
                pe_source = "computed(price/forwardEps)"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    logger.info(
        f"[pe-data] {ticker_symbol}: "
        + (f"{forward_pe:.2f}x via {pe_source}" if forward_pe is not None
           else "unavailable (no forwardPE, no price/forwardEps pair)")
    )

    # Try to get full forward estimates table
    fy_next = None
    fy_next2 = None
    try:
        # yfinance's analyst_price_targets and earnings_estimate
        earnings_est = t.earnings_estimate if hasattr(t, "earnings_estimate") else None
        if earnings_est is not None and not earnings_est.empty:
            # earnings_estimate has rows for current/next quarter and current/next year
            # Index labels: '0q', '+1q', '0y', '+1y'
            if "+1y" in earnings_est.index:
                fy_next = float(earnings_est.loc["+1y", "avg"])
            if "+2y" in earnings_est.index:
                fy_next2 = float(earnings_est.loc["+2y", "avg"])
    except Exception as e:
        logger.debug(f"Could not fetch detailed earnings estimates for {ticker_symbol}: {e}")

    # Try to get estimate revisions
    upgrades_30d = None
    downgrades_30d = None
    try:
        # yfinance also exposes 'eps_revisions' on some versions
        rev = t.eps_revisions if hasattr(t, "eps_revisions") else None
        if rev is not None and not rev.empty:
            if "+1y" in rev.index:
                row = rev.loc["+1y"]
                upgrades_30d = int(row.get("upLast30days", 0)) if row.get("upLast30days") else None
                downgrades_30d = int(row.get("downLast30days", 0)) if row.get("downLast30days") else None
    except Exception as e:
        logger.debug(f"Could not fetch EPS revisions for {ticker_symbol}: {e}")

    return EstimateSnapshot(
        ticker=ticker_symbol,
        name=ticker_name,
        fy_current_eps_estimate=fy_current,
        fy_next_eps_estimate=fy_next,
        fy_next2_eps_estimate=fy_next2,
        num_analysts=num_analysts,
        avg_target_price=avg_target,
        high_target_price=high_target,
        low_target_price=low_target,
        forward_pe=forward_pe,
        upgrades_30d=upgrades_30d,
        downgrades_30d=downgrades_30d,
    )


def fetch_estimates_fmp(ticker_symbol: str, ticker_name: str, api_key: str) -> Optional[EstimateSnapshot]:
    """Optional augmentation via Financial Modeling Prep API.

    FMP has more detailed analyst estimate data than yfinance,
    including per-analyst breakdowns. Free tier is 250 req/day.
    """
    if not api_key:
        return None

    try:
        # FMP analyst estimates endpoint
        url = f"https://financialmodelingprep.com/api/v3/analyst-estimates/{ticker_symbol}?period=annual&apikey={api_key}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if not data or not isinstance(data, list):
            return None

        # Sort by date to find the next two fiscal years
        sorted_estimates = sorted(data, key=lambda x: x.get("date", ""))
        future = [e for e in sorted_estimates if e.get("date", "") > datetime.utcnow().strftime("%Y-%m-%d")]

        fy_next_eps = None
        fy_next2_eps = None
        if len(future) >= 1:
            fy_next_eps = future[0].get("estimatedEpsAvg")
        if len(future) >= 2:
            fy_next2_eps = future[1].get("estimatedEpsAvg")

        # Number of analysts (use most recent)
        num_analysts = future[0].get("numberAnalystEstimatedEps") if future else None

        return EstimateSnapshot(
            ticker=ticker_symbol,
            name=ticker_name,
            fy_current_eps_estimate=None,
            fy_next_eps_estimate=fy_next_eps,
            fy_next2_eps_estimate=fy_next2_eps,
            num_analysts=num_analysts,
            avg_target_price=None,  # separate FMP endpoint
            high_target_price=None,
            low_target_price=None,
            forward_pe=None,
        )
    except Exception as e:
        logger.warning(f"FMP estimate fetch failed for {ticker_symbol}: {e}")
        return None


def fetch_all_estimates(
    tickers: dict[str, str],
    fmp_api_key: Optional[str] = None,
    collector: Optional[DiagnosticsCollector] = None,
) -> dict[str, EstimateSnapshot]:
    """Fetch estimates for all tickers, with optional FMP augmentation.

    Records two batched SourceFetchStatus entries:
    - one for the yfinance estimates batch (items_returned = tickers resolved)
    - one for the FMP batch if fmp_api_key set, else a 'skipped' status
    """
    out: dict[str, EstimateSnapshot] = {}

    # ---- yfinance batch (always run) ----
    yf_resolved = 0
    yf_failed: list[str] = []

    if collector is not None:
        # We track this as a single batched call rather than one entry per
        # ticker — yfinance .info is hit per-ticker but pipeline-wise it's
        # one logical operation.
        with collector.track("Yahoo Finance / estimates batch", "yfinance") as t:
            for name, symbol in tickers.items():
                try:
                    snap = fetch_estimates_yfinance(symbol, name)
                    out[name] = snap
                    yf_resolved += 1
                except Exception as e:
                    logger.warning(f"Estimate fetch failed for {name}: {e}")
                    yf_failed.append(name)
                    continue
            t.items_returned = yf_resolved
            if yf_failed:
                t.detail = f"{yf_resolved}/{len(tickers)} resolved, failed: {', '.join(yf_failed)}"
            else:
                t.detail = f"{yf_resolved}/{len(tickers)} tickers resolved"
    else:
        for name, symbol in tickers.items():
            try:
                out[name] = fetch_estimates_yfinance(symbol, name)
            except Exception as e:
                logger.warning(f"Estimate fetch failed for {name}: {e}")
                continue

    # ---- FMP augmentation (optional) ----
    if fmp_api_key:
        fmp_resolved = 0
        if collector is not None:
            with collector.track("FMP / estimate augmentation", "fmp") as t:
                for name, symbol in tickers.items():
                    if name not in out:
                        continue
                    fmp_snap = fetch_estimates_fmp(symbol, name, fmp_api_key)
                    if fmp_snap and fmp_snap.fy_next_eps_estimate is not None:
                        out[name].fy_next_eps_estimate = fmp_snap.fy_next_eps_estimate
                        out[name].fy_next2_eps_estimate = fmp_snap.fy_next2_eps_estimate
                        fmp_resolved += 1
                t.items_returned = fmp_resolved
                t.detail = f"{fmp_resolved}/{len(tickers)} tickers augmented"
        else:
            for name, symbol in tickers.items():
                if name not in out:
                    continue
                fmp_snap = fetch_estimates_fmp(symbol, name, fmp_api_key)
                if fmp_snap and fmp_snap.fy_next_eps_estimate is not None:
                    out[name].fy_next_eps_estimate = fmp_snap.fy_next_eps_estimate
                    out[name].fy_next2_eps_estimate = fmp_snap.fy_next2_eps_estimate
    else:
        if collector is not None:
            collector.record_skipped(
                "FMP / estimate augmentation",
                "fmp",
                "no FMP_API_KEY set (yfinance baseline only)",
            )

    return out


# ============================================================
# Hyperscaler capex/OCF ratio computation (Indicator t2_hyperscaler_capex_revenue)
# ============================================================
# v3.39.7.4 — Primary metric is now capex/OCF (operating cash flow), not
# capex/revenue. Empirical analysis (May 20 session) showed capex/revenue
# >40% NEVER fires across the 4 actual hyperscalers historically — the
# trigger only ever fired because of ORCL. Capex/OCF cleanly separates
# crash years (mean >40% in 2014/2018/2022) from non-crash years.
#
# Triggers (applied downstream in Layer 2 + Layer 3, not in this module):
#   - Partially fired: mean(capex_ocf_ratio across 4 hyperscalers) > 0.40
#                      using the LAST PUBLISHED QUARTER per company (NOT TTM)
#   - Fired:           mean(capex_ocf_ratio) > 0.50
#   - Severe single-company strain (secondary): any company capex/OCF > 1.00
#
# We fetch quarterly capex + OCF (both from cashflow statement) and quarterly
# revenue (from income statement) via yfinance. capex/revenue is computed
# alongside capex/OCF for context but is NOT the trigger metric.
#
# The state file state/hyperscaler_capex_baselines.json is consumed by
# Layer 2 for trigger evaluation and by Layer 1 digesters when processing
# new earnings calls.


@dataclass
class HyperscalerCapexSnapshot:
    """Quarterly snapshot of one hyperscaler's capex strain.

    v3.39.7.4: ocf_usd_b + capex_ocf_ratio added. `capex_revenue_ratio` kept
    for context but is no longer the trigger metric. `fired_threshold`
    semantic was "capex/revenue > 0.40" pre-v3.39.7.4 (deprecated, never
    fired in production); now means "capex/OCF > 1.00" (the severe single-
    company strain flag — Amazon 2021-22 hit 120-125% during cycle peak).
    """
    ticker: str
    name: str
    quarter_iso: str              # e.g. "2026-Q1"
    capex_usd_b: Optional[float]
    revenue_usd_b: Optional[float]
    ocf_usd_b: Optional[float]                # v3.39.7.4 NEW
    capex_revenue_ratio: Optional[float]      # kept for context
    capex_ocf_ratio: Optional[float]          # v3.39.7.4 NEW — primary trigger metric
    fired_threshold: bool                     # v3.39.7.4: now means capex/OCF > 1.00
    fy_capex_run_rate_usd_b: Optional[float]  # 4-quarter rolling sum


def _fetch_hyperscaler_capex_one(ticker_symbol: str, ticker_name: str) -> list[HyperscalerCapexSnapshot]:
    """Pull last 4 quarters of capex + revenue + OCF for one hyperscaler from yfinance.

    v3.39.7.4: OCF extraction added. Operating cash flow line label varies
    across yfinance versions / company filing styles — we try several
    candidate labels in order of frequency.
    """
    snapshots: list[HyperscalerCapexSnapshot] = []
    try:
        t = yf.Ticker(ticker_symbol)
        # yfinance returns quarterly_cashflow as DataFrame with columns = quarter end dates
        qcf = t.quarterly_cashflow
        qis = t.quarterly_financials  # income statement
    except Exception as e:
        logger.warning(f"Could not fetch yfinance quarterly data for {ticker_symbol}: {e}")
        return snapshots

    if qcf is None or qcf.empty or qis is None or qis.empty:
        logger.warning(f"yfinance returned empty quarterly data for {ticker_symbol}")
        return snapshots

    # Capex line — yfinance labels it 'Capital Expenditure' (negative numbers — outflows)
    capex_row_candidates = ["Capital Expenditure", "Capital Expenditures", "CapitalExpenditure"]
    capex_row = None
    for candidate in capex_row_candidates:
        if candidate in qcf.index:
            capex_row = qcf.loc[candidate]
            break
    if capex_row is None:
        logger.warning(f"No capex row found in yfinance cashflow for {ticker_symbol}")
        return snapshots

    # v3.39.7.4: Operating cash flow row — primary trigger metric is now capex/OCF.
    # yfinance label varies: 'Operating Cash Flow' (most common), older filings
    # use 'Total Cash From Operating Activities', some use 'Cash Flow From
    # Continuing Operating Activities', and the camelCase form appears in
    # some yfinance versions.
    ocf_row_candidates = [
        "Operating Cash Flow",
        "Total Cash From Operating Activities",
        "Cash Flow From Continuing Operating Activities",
        "OperatingCashFlow",
        "Cash Flow From Operating Activities",
    ]
    ocf_row = None
    for candidate in ocf_row_candidates:
        if candidate in qcf.index:
            ocf_row = qcf.loc[candidate]
            break
    if ocf_row is None:
        # Soft-fail: we keep producing snapshots with capex/revenue but no
        # capex/OCF. Layer 2 sees capex_ocf_ratio=None and treats that as
        # missing data, not as a "Not fired" signal.
        logger.warning(
            f"No operating cash flow row found in yfinance cashflow for {ticker_symbol}; "
            f"capex/OCF ratio will be None. Available rows: {list(qcf.index)[:10]}"
        )

    revenue_row_candidates = ["Total Revenue", "Revenue", "Operating Revenue"]
    revenue_row = None
    for candidate in revenue_row_candidates:
        if candidate in qis.index:
            revenue_row = qis.loc[candidate]
            break
    if revenue_row is None:
        logger.warning(f"No revenue row found in yfinance financials for {ticker_symbol}")
        return snapshots

    # Iterate the columns (each is a quarter end date)
    rolling_capex_4q = 0.0
    for col_date in sorted(capex_row.index, reverse=True)[:4]:
        try:
            capex_val = abs(float(capex_row[col_date]))  # absolute value (yfinance gives negative)
            if col_date in revenue_row.index:
                rev_val = float(revenue_row[col_date])
            else:
                rev_val = None
        except (ValueError, TypeError, KeyError):
            continue

        # OCF for the same quarter (may be None if row wasn't found)
        ocf_val: Optional[float] = None
        if ocf_row is not None and col_date in ocf_row.index:
            try:
                # yfinance gives OCF as positive (inflow), but be defensive
                raw_ocf = ocf_row[col_date]
                if raw_ocf is not None:
                    ocf_val = float(raw_ocf)
            except (ValueError, TypeError):
                ocf_val = None

        # capex/revenue ratio (kept for context)
        capex_rev_ratio = None
        if rev_val and rev_val > 0:
            capex_rev_ratio = round(capex_val / rev_val, 4)

        # v3.39.7.4: capex/OCF ratio — PRIMARY trigger metric
        capex_ocf_ratio = None
        if ocf_val and ocf_val > 0:
            capex_ocf_ratio = round(capex_val / ocf_val, 4)

        # Quarter label
        quarter = f"{col_date.year}-Q{((col_date.month - 1) // 3) + 1}" if hasattr(col_date, "year") else str(col_date)

        rolling_capex_4q += capex_val / 1e9 if capex_val else 0.0

        snapshots.append(HyperscalerCapexSnapshot(
            ticker=ticker_symbol,
            name=ticker_name,
            quarter_iso=quarter,
            capex_usd_b=round(capex_val / 1e9, 2) if capex_val else None,
            revenue_usd_b=round(rev_val / 1e9, 2) if rev_val else None,
            ocf_usd_b=round(ocf_val / 1e9, 2) if ocf_val else None,
            capex_revenue_ratio=capex_rev_ratio,
            capex_ocf_ratio=capex_ocf_ratio,
            # v3.39.7.4: fired_threshold semantic changed from "capex/rev > 40%"
            # (which never fired in practice for the 4 actual hyperscalers) to
            # "capex/OCF > 100%" — severe single-company strain (Amazon
            # historical: hit 120-125% during 2021-22 cycle peak). The
            # aggregate mean-based trigger lives downstream in computed_metrics.
            fired_threshold=(capex_ocf_ratio is not None and capex_ocf_ratio > 1.00),
            fy_capex_run_rate_usd_b=None,  # filled in after loop
        ))

    # Fill in rolling 4Q sum (TTM capex run rate) for all snapshots
    for s in snapshots:
        s.fy_capex_run_rate_usd_b = round(rolling_capex_4q, 2)

    return snapshots


def fetch_all_hyperscaler_capex(
    hyperscalers: dict[str, str],
    collector: Optional[DiagnosticsCollector] = None,
) -> dict[str, list[HyperscalerCapexSnapshot]]:
    """Fetch capex + OCF + revenue ratios for every hyperscaler.

    v3.39.7.4: aggregate mean(capex_ocf_ratio) across hyperscalers using
    EACH COMPANY'S LAST PUBLISHED QUARTER is the primary trigger value.
    Per user decision 2026-05-20: "we go with last published quarter"
    (NOT TTM — TTM is one year stale; cycle calls need responsiveness).

    Returns dict: ticker_symbol → list of 4 quarterly snapshots (most recent first).
    Writes results to state/hyperscaler_capex_baselines.json including the
    computed aggregate, for Layer 2 consumption.
    """
    out: dict[str, list[HyperscalerCapexSnapshot]] = {}
    n_severe_strain = 0  # >100% capex/OCF, any company

    if collector is not None:
        with collector.track("yfinance / hyperscaler capex+OCF+revenue", "yfinance") as t:
            for name, ticker_symbol in hyperscalers.items():
                snaps = _fetch_hyperscaler_capex_one(ticker_symbol, name)
                out[ticker_symbol] = snaps
                if any(s.fired_threshold for s in snaps):
                    n_severe_strain += 1
            t.items_returned = sum(len(v) for v in out.values())
            t.detail = (
                f"{n_severe_strain}/{len(hyperscalers)} hyperscalers showing severe "
                f"single-company strain (capex/OCF >100%) in any of the last 4Q"
            )
    else:
        for name, ticker_symbol in hyperscalers.items():
            snaps = _fetch_hyperscaler_capex_one(ticker_symbol, name)
            out[ticker_symbol] = snaps

    # v3.39.7.9 — 2-quarter rolling mean per user direction 2026-05-21.
    # Pre-v3.39.7.9 (v3.39.7.4) used snaps[0] only — single-quarter capex/OCF.
    # The May 21 brief surfaced the noise cost: Amazon's Q1 2026 OCF was
    # $26.03B vs Q4 2025 $54.46B = isolated single-quarter dip, making
    # AMZN capex/OCF look like 169.8% when the 2-quarter mean is ~85%. This
    # single-quarter spike contributed to mean of 93.2% across hyperscalers,
    # which was correct as a snapshot but mislead Layer 2 about persistence.
    #
    # Strategy: compute each company's 2-quarter capex/OCF mean (snaps[0:2])
    # FIRST, then average across companies. Per-company smoothing avoids
    # the noise; cross-company aggregation preserves the cycle signal. For
    # companies with only 1 published quarter (rare — recently-listed names),
    # fall back gracefully to that single quarter.
    per_company_2q_ratios: dict[str, float] = {}
    for ticker, snaps in out.items():
        if not snaps:
            continue
        # Take up to first 2 quarters with valid capex_ocf_ratio
        valid_quarters = [
            s.capex_ocf_ratio for s in snaps[:2]
            if s.capex_ocf_ratio is not None
        ]
        if valid_quarters:
            per_company_2q_ratios[ticker] = round(
                sum(valid_quarters) / len(valid_quarters), 4
            )

    mean_capex_ocf_ratio_2q: Optional[float] = None
    if per_company_2q_ratios:
        mean_capex_ocf_ratio_2q = round(
            sum(per_company_2q_ratios.values()) / len(per_company_2q_ratios), 4
        )

    # Persist to state for Layer 2 consumption
    try:
        import json
        from pathlib import Path
        state_dir = Path("./state")
        state_dir.mkdir(exist_ok=True)
        baselines_file = state_dir / "hyperscaler_capex_baselines.json"
        baseline_payload = {
            "computed_at_iso": datetime.utcnow().isoformat(),
            # v3.39.7.9: 2-quarter rolling mean per company, then cross-company
            # mean. Layer 2 / Layer 3 fire on:
            #   > 0.40 → Partially fired
            #   > 0.50 → Fired
            "mean_capex_ocf_ratio_2q": mean_capex_ocf_ratio_2q,
            "per_company_capex_ocf_2q": per_company_2q_ratios,
            "n_companies_with_2q_capex_ocf": len(per_company_2q_ratios),
            "data": {
                ticker: [
                    {
                        "ticker": s.ticker,
                        "name": s.name,
                        "quarter_iso": s.quarter_iso,
                        "capex_usd_b": s.capex_usd_b,
                        "revenue_usd_b": s.revenue_usd_b,
                        "ocf_usd_b": s.ocf_usd_b,
                        "capex_revenue_ratio": s.capex_revenue_ratio,  # legacy — no longer authoritative
                        "capex_ocf_ratio": s.capex_ocf_ratio,           # single-quarter snapshot (per-quarter); aggregation now uses 2q mean
                        "fired_threshold": s.fired_threshold,
                        "fy_capex_run_rate_usd_b": s.fy_capex_run_rate_usd_b,
                    }
                    for s in snaps
                ]
                for ticker, snaps in out.items()
            },
        }
        with open(baselines_file, "w") as f:
            json.dump(baseline_payload, f, indent=2, default=str)
        logger.info(f"Wrote hyperscaler capex baselines: {baselines_file}")
    except Exception as e:
        logger.warning(f"Could not write hyperscaler capex baselines: {e}")

    return out
