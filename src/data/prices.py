"""
Price and market data fetching via yfinance.

Provides:
- Current prices and recent performance for tracked tickers
- 1Y price history baselined to 100 for charting
- Performance over multiple windows (1D, 1W, 1M, 3M, 6M, 1Y)

v3.39.7.5 — KRX staleness repair. yfinance occasionally returns
yesterday's bar as the "latest" for `.KS` / `.KQ` tickers even when
the daily session has been closed for many hours. The May 20 2026
brief surfaced SK Hynix at -5.16% (yesterday's print) while Yahoo's
own page showed 1,745,000 KRW flat (today's actual close). The
repair path here checks each KRX ticker's freshness after the batch
yfinance call and falls back through (1) single-ticker yfinance
refetch, (2) FMP `/api/v3/quote/{symbol}`, (3) Naver Finance scrape.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

from src.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Performance metrics for a single ticker over standard windows."""
    ticker: str
    name: str
    current_price: float
    currency: str
    change_1d: Optional[float]   # %
    change_1w: Optional[float]
    change_1m: Optional[float]
    change_3m: Optional[float]
    change_6m: Optional[float]
    change_1y: Optional[float]


def fetch_price_history(
    tickers: dict[str, str],
    period: str = "1y",
    interval: str = "1d",
    collector: Optional[DiagnosticsCollector] = None,
    fmp_api_key: Optional[str] = None,
) -> pd.DataFrame:
    """Fetch adjusted close price history for a basket of tickers.

    Returns a DataFrame indexed by date with one column per ticker name.
    Records a single SourceFetchStatus for the batch yfinance call,
    with items_returned set to the number of tickers that resolved.

    v3.39.7.5 — After the batch fetch, KRX tickers (.KS / .KQ) are
    checked for staleness. yfinance has been observed returning
    yesterday's bar as the latest for KRX names even when the daily
    session closed >12h ago. Stale KRX tickers get their latest row
    repaired from FMP first (if `fmp_api_key` provided), then from
    Naver Finance as a last resort.
    """
    symbols = list(tickers.values())
    name_map = {symbol: name for name, symbol in tickers.items()}

    def _do_download() -> pd.DataFrame:
        return yf.download(
            tickers=symbols,
            period=period,
            interval=interval,
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )

    try:
        if collector is not None:
            with collector.track("Yahoo Finance / yfinance batch", "yfinance") as t:
                data = _do_download()
                # items_returned is set after we count resolved tickers below
                _t_handle = t
        else:
            data = _do_download()
            _t_handle = None
    except Exception as e:
        logger.error(f"Failed to fetch price history: {e}")
        raise

    # Single-ticker case: yfinance returns a different structure
    if len(symbols) == 1:
        out = pd.DataFrame({name_map[symbols[0]]: data["Close"]})
        resolved = 1
    else:
        # Multi-ticker case: extract Close from each
        frames = {}
        resolved = 0
        for symbol in symbols:
            try:
                frames[name_map[symbol]] = data[symbol]["Close"]
                resolved += 1
            except KeyError:
                logger.warning(f"No data returned for {symbol}")
                continue
        out = pd.DataFrame(frames)

    if _t_handle is not None:
        _t_handle.items_returned = resolved
        _t_handle.detail = f"{resolved}/{len(symbols)} tickers resolved"

    out = out.dropna(how="all")

    # v3.39.7.5: repair stale KRX tickers via FMP → Naver fallback chain
    out = _repair_stale_krx_tickers(
        out, tickers, fmp_api_key=fmp_api_key, collector=collector,
    )

    return out


# ============================================================
# v3.39.7.5 — KRX staleness repair
# ============================================================
# KRX (Korea Exchange) trading hours: 09:00-15:30 KST = 00:00-06:30 UTC.
# After 06:30 UTC on a weekday, the daily session is closed and the
# day's close should be available in price feeds. yfinance is usually
# reliable but has been seen returning T-1 data for KRX tickers even
# many hours after close. We check freshness and fall back through:
#   1. FMP `/api/v3/quote/{symbol}` (API, fast, well-formatted)
#   2. Naver Finance scrape (server-rendered, free, last resort)
# If all fail, we keep the stale data and emit a WARN diagnostic.


def _latest_expected_krx_session_date(now_utc: Optional[datetime] = None) -> date:
    """The most recent trading-day date for which KRX has a closed session.

    Walks back through weekends/holidays-as-weekends. We don't have a
    full KRX holiday calendar; weekend handling alone catches ~95% of
    cases, and the safety_buffer parameter (1h) provides slack on
    days where ingestion is slow.

    KRX closes at 06:30 UTC. We add a 1.5h ingestion buffer, so we only
    expect today's data to be available from 08:00 UTC onward.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    candidate = now_utc.date()
    cutoff_today = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_utc < cutoff_today:
        candidate -= timedelta(days=1)
    # Walk back through Sat/Sun (and unrecognized holidays — they'll
    # appear as a stale-but-recent-weekday case)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _fetch_krx_quote_from_fmp(symbol: str, api_key: str) -> Optional[dict]:
    """Fetch latest quote from Financial Modeling Prep.

    Args:
        symbol: full Yahoo-style ticker (e.g. "000660.KS"). FMP accepts
            the same format.
        api_key: FMP API key (env var `FMP_API_KEY`)

    Returns dict with keys {price, previous_close, change_pct, asof,
    source} or None if the fetch failed or didn't return usable data.
    """
    if not api_key:
        return None
    try:
        url = f"https://financialmodelingprep.com/api/v3/quote/{symbol}?apikey={api_key}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list) or not data[0].get("price"):
            return None
        item = data[0]
        price = float(item["price"])
        prev = item.get("previousClose")
        prev_f = float(prev) if prev is not None else None
        change_pct = None
        if prev_f and prev_f > 0:
            change_pct = round((price / prev_f - 1) * 100, 4)
        return {
            "price": price,
            "previous_close": prev_f,
            "change_pct": change_pct,
            "asof": datetime.now(timezone.utc).isoformat(),
            "source": "FMP",
        }
    except Exception as e:
        logger.warning(f"FMP quote fetch failed for {symbol}: {e}")
        return None


def _repair_stale_krx_tickers(
    history: pd.DataFrame,
    tickers: dict[str, str],
    fmp_api_key: Optional[str] = None,
    collector: Optional[DiagnosticsCollector] = None,
) -> pd.DataFrame:
    """For each KRX-listed ticker (.KS / .KQ) in `tickers`, check whether
    `history`'s latest row reflects the most recent expected KRX session.
    If not, try to repair by appending today's close from FMP, then Naver.

    Mutates and returns the DataFrame.
    """
    krx_tickers = {
        name: symbol for name, symbol in tickers.items()
        if symbol.endswith(".KS") or symbol.endswith(".KQ")
    }
    if not krx_tickers:
        return history

    expected_date = _latest_expected_krx_session_date()
    expected_ts = pd.Timestamp(expected_date)

    repairs_attempted = 0
    repairs_succeeded = 0
    repair_details = []

    for name, symbol in krx_tickers.items():
        if name not in history.columns:
            continue
        series = history[name].dropna()
        if len(series) == 0:
            continue

        latest_ts = pd.Timestamp(series.index[-1]).normalize()
        if latest_ts >= expected_ts:
            continue  # Fresh, no repair needed

        repairs_attempted += 1
        days_stale = (expected_ts - latest_ts).days
        logger.warning(
            f"KRX ticker {symbol} ({name}) has stale price history: "
            f"latest={latest_ts.date()}, expected={expected_date} "
            f"({days_stale}d stale). Attempting FMP → Naver fallback."
        )

        # Try FMP first
        quote = _fetch_krx_quote_from_fmp(symbol, fmp_api_key) if fmp_api_key else None

        # Fall back to Naver scrape (uses 6-digit code, no exchange suffix)
        if quote is None:
            try:
                from src.data.naver_finance import fetch_quote_for_ticker
                naver_code = symbol.replace(".KS", "").replace(".KQ", "")
                quote = fetch_quote_for_ticker(naver_code)
            except Exception as e:
                logger.warning(f"Naver quote fallback failed for {symbol}: {e}")
                quote = None

        if quote is None:
            logger.error(
                f"Could not repair stale {symbol} via FMP or Naver — "
                f"keeping yfinance value of {series.iloc[-1]:.0f} from {latest_ts.date()}. "
                f"Brief will display this stale value flagged in data source assessment."
            )
            repair_details.append(f"{symbol}: failed (all fallbacks exhausted)")
            continue

        # Inject the fresh value at expected_ts
        history.loc[expected_ts, name] = quote["price"]
        history = history.sort_index()
        repairs_succeeded += 1
        logger.info(
            f"Repaired {symbol} via {quote['source']}: today's close = "
            f"{quote['price']:.0f} KRW (was {series.iloc[-1]:.0f} from {latest_ts.date()})"
        )
        repair_details.append(
            f"{symbol}: repaired via {quote['source']} → {quote['price']:.0f} KRW"
        )

    if repairs_attempted > 0 and collector is not None:
        with collector.track("KRX staleness repair", "internal") as t:
            t.items_returned = repairs_succeeded
            t.detail = (
                f"{repairs_succeeded}/{repairs_attempted} KRX tickers repaired. "
                + " | ".join(repair_details)
            )

    return history


def calculate_performance(
    history: pd.DataFrame,
    tickers: dict[str, str],
) -> list[PerformanceMetrics]:
    """Calculate performance metrics for each ticker over standard windows."""
    metrics = []

    for name, symbol in tickers.items():
        if name not in history.columns:
            logger.warning(f"No history for {name}; skipping")
            continue

        series = history[name].dropna()
        if len(series) < 2:
            continue

        current = float(series.iloc[-1])
        currency = _get_currency(symbol)

        def pct_change_lookback(days: int) -> Optional[float]:
            # v3.39.2: hardened against off-by-one. Prior guard
            # `if len(series) < days + 1` fired at exact equality
            # (the May 17 run had len(series)=252 and days=252, so
            # 252 < 253 → returned None for 1Y even though all 252
            # rows were valid). Now we accept any series long enough
            # to contain the requested lookback point at all, falling
            # back to the earliest available row when the window is
            # tight rather than refusing.
            if len(series) < 2:
                return None
            lookback_idx = -days - 1
            if abs(lookback_idx) > len(series):
                # Series shorter than requested window — fall back to
                # the oldest available row. The resulting return is
                # labeled by the caller as `change_<window>` regardless;
                # this is acceptable since fast_tier now fetches 3y of
                # data, so this branch is realistically unreachable.
                # Kept as defense-in-depth in case the fetch window is
                # ever shortened again.
                lookback_idx = 0
            try:
                past = series.iloc[lookback_idx]
                if past == 0:
                    return None
                return (current / past - 1) * 100
            except IndexError:
                return None

        metrics.append(PerformanceMetrics(
            ticker=symbol,
            name=name,
            current_price=current,
            currency=currency,
            change_1d=pct_change_lookback(1),
            change_1w=pct_change_lookback(5),
            change_1m=pct_change_lookback(21),
            change_3m=pct_change_lookback(63),
            change_6m=pct_change_lookback(126),
            change_1y=pct_change_lookback(252),
        ))

    return metrics


def baseline_to_100(history: pd.DataFrame) -> pd.DataFrame:
    """Rebase a price history DataFrame so each series starts at 100."""
    rebased = history.copy()
    for col in rebased.columns:
        first_valid = rebased[col].first_valid_index()
        if first_valid is None:
            continue
        base_value = rebased[col].loc[first_valid]
        if base_value == 0:
            continue
        rebased[col] = (rebased[col] / base_value) * 100
    return rebased


def _get_currency(symbol: str) -> str:
    """Infer the trading currency from the ticker suffix."""
    if symbol.endswith(".KS") or symbol.endswith(".KQ"):
        return "KRW"
    if symbol.endswith(".T"):
        return "JPY"
    if symbol.endswith(".HK"):
        return "HKD"
    if symbol.endswith(".L"):
        return "GBP"
    return "USD"


def get_relative_strength_vs_sox(
    ticker_name: str,
    history: pd.DataFrame,
    days: int = 5,
) -> Optional[float]:
    """Calculate ticker's relative performance vs SOX over `days` trading days.

    Returns a percentage. Negative means underperformance.
    """
    sox_col = "MSCI ACWI Semiconductor"  # name we use for SOX in BENCHMARKS
    if sox_col not in history.columns or ticker_name not in history.columns:
        return None

    if len(history) < days + 1:
        return None

    ticker_return = (history[ticker_name].iloc[-1] / history[ticker_name].iloc[-days - 1] - 1) * 100
    sox_return = (history[sox_col].iloc[-1] / history[sox_col].iloc[-days - 1] - 1) * 100

    return ticker_return - sox_return


# ============================================================
# Sharpened relative-strength signals for t3_relative_strength
# ============================================================
# v3.39: redesigned around two severity tiers on the SAME 5-day metric.
# Prior versions (v3.36-v3.38.6) used:
#   (a) 5-day spread vs SOX < -10pp           (red fire)
#   (b) ANY SINGLE DAY worst spread < -5pp    (red fire)  ← whipsawed daily
#   (c) DRAM ETF underperforming SMH 5d       (red fire)
# The single-day sub-trigger (b) fired on May 17 from one rough session
# (Samsung -8.61%, SK Hynix -7.66%, MU -6.62%) when SOX was down ~3-4%,
# yielding 3-5pp single-day spreads. Raul's feedback: a single session
# fires-and-unfires daily, masks the persistent-weakness signal we want.
#
# v3.39 design:
#   (a) 5-day spread vs SOX < -10pp           → RED full fire (unchanged)
#   (b) 5-day spread vs SOX < -5pp            → YELLOW watch (informational)
#   (c) DRAM ETF underperforming SMH 5d       → YELLOW watch (unchanged threshold)
# Same 5-day metric for (a) and (b), two thresholds. Yellow does NOT trip
# urgent_attention; only red does. Cleaner, no single-day noise, less code.


def get_dram_etf_vs_smh(
    history: pd.DataFrame,
    days: int = 5,
) -> Optional[dict]:
    """Compute DRAM ETF vs SMH (broader semi ETF) over `days` trading days.

    Pure-memory proxy: if DRAM ETF underperforms SMH, that's a clean memory-cycle
    signal independent of any single stock's idiosyncrasies. v3.39: yellow watch
    threshold (< -5pp) — same as prior versions; this sub-trigger was never the
    whipsawing one. Red fire threshold (< -10pp) added for parity.
    """
    dram_col = "Roundhill DRAM ETF"
    smh_col = "VanEck Semi ETF (SMH)"
    if dram_col not in history.columns or smh_col not in history.columns:
        return None
    if len(history) < days + 1:
        return None

    dram_return = (history[dram_col].iloc[-1] / history[dram_col].iloc[-days - 1] - 1) * 100
    smh_return = (history[smh_col].iloc[-1] / history[smh_col].iloc[-days - 1] - 1) * 100
    delta = dram_return - smh_return

    if delta < -10.0:
        severity = "red"
    elif delta < -5.0:
        severity = "yellow"
    else:
        severity = None

    return {
        "dram_return_pct": round(dram_return, 2),
        "smh_return_pct": round(smh_return, 2),
        "spread_pp": round(delta, 2),
        "severity": severity,
        "fired_threshold": severity is not None,  # back-compat for prior callers
    }


def evaluate_relative_strength_signal(history: pd.DataFrame) -> dict:
    """Evaluate the full t3_relative_strength signal across the two thresholds
    and the DRAM-ETF sub-trigger. Returns a structured verdict for Layer 2/3.

    Severity ladder:
      red   — 5-day spread < -10pp on SK Hynix or Micron (full Tier 3 fire)
      yellow — 5-day spread < -5pp (informational watch)
      none  — no underperformance

    overall_severity is the max severity across all sub-triggers (red beats
    yellow beats none). urgent_attention should be wired to overall_severity
    == 'red' only.
    """
    out = {
        "skhx_5d_vs_sox_pp": None,
        "mu_5d_vs_sox_pp": None,
        "dram_etf_vs_smh": None,
        "fired_sub_triggers": [],     # list of (severity, description)
        "overall_severity": None,     # 'red' | 'yellow' | None
        "overall_fired": False,        # back-compat: True iff overall_severity == 'red'
        "banner_text": None,
    }

    skhx_5d = get_relative_strength_vs_sox("SK Hynix", history, days=5)
    mu_5d = get_relative_strength_vs_sox("Micron", history, days=5)
    out["skhx_5d_vs_sox_pp"] = round(skhx_5d, 2) if skhx_5d is not None else None
    out["mu_5d_vs_sox_pp"] = round(mu_5d, 2) if mu_5d is not None else None

    def _classify_5d(name: str, val: Optional[float]) -> None:
        if val is None:
            return
        if val < -10.0:
            out["fired_sub_triggers"].append(
                ("red", f"{name} 5d vs SOX = {val:.1f}pp (< -10pp — RED fire)")
            )
        elif val < -5.0:
            out["fired_sub_triggers"].append(
                ("yellow", f"{name} 5d vs SOX = {val:.1f}pp (< -5pp — yellow watch)")
            )

    _classify_5d("SK Hynix", skhx_5d)
    _classify_5d("Micron", mu_5d)

    # Sub-trigger (c): DRAM ETF vs SMH — yellow/red on same scale
    dram_vs_smh = get_dram_etf_vs_smh(history, days=5)
    out["dram_etf_vs_smh"] = dram_vs_smh
    if dram_vs_smh and dram_vs_smh["severity"]:
        sev = dram_vs_smh["severity"]
        pp = dram_vs_smh["spread_pp"]
        thresh = "-10pp — RED fire" if sev == "red" else "-5pp — yellow watch"
        out["fired_sub_triggers"].append(
            (sev, f"DRAM ETF vs SMH 5d = {pp}pp (< {thresh})")
        )

    # Compute overall severity
    severities = [s for s, _ in out["fired_sub_triggers"]]
    if "red" in severities:
        out["overall_severity"] = "red"
        out["overall_fired"] = True
    elif "yellow" in severities:
        out["overall_severity"] = "yellow"
        out["overall_fired"] = False

    if out["overall_severity"]:
        prefix = "🚨 RELATIVE STRENGTH RED FIRE" if out["overall_severity"] == "red" else "🟡 RELATIVE STRENGTH YELLOW WATCH"
        descriptions = [desc for _, desc in out["fired_sub_triggers"]]
        out["banner_text"] = f"{prefix} — " + " · ".join(descriptions)

    return out
