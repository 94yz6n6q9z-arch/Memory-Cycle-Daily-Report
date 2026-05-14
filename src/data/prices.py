"""
Price and market data fetching via yfinance.

Provides:
- Current prices and recent performance for tracked tickers
- 1Y price history baselined to 100 for charting
- Performance over multiple windows (1D, 1W, 1M, 3M, 6M, 1Y)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
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
) -> pd.DataFrame:
    """Fetch adjusted close price history for a basket of tickers.

    Returns a DataFrame indexed by date with one column per ticker name.
    Records a single SourceFetchStatus for the batch yfinance call,
    with items_returned set to the number of tickers that resolved.
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
    return out


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
            if len(series) < days + 1:
                return None
            try:
                past = series.iloc[-days - 1]
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
# The original `get_relative_strength_vs_sox` returns the 5-day spread.
# The framework's full t3 trigger has three sub-conditions:
#   (a) 5-day spread vs SOX < -10pp       (existing — pure numeric)
#   (b) Any single-day -5%+ vs SOX        (NEW — early-warning sub-trigger)
#   (c) DRAM ETF underperforming SMH      (NEW — pure-memory proxy)
# Layer 2 reasons over all three. Sonnet's exec summary surfaces an alert
# banner when ANY sub-trigger fires.


def get_single_day_max_underperformance(
    ticker_name: str,
    history: pd.DataFrame,
    lookback_days: int = 5,
) -> Optional[dict]:
    """Find the single worst single-day relative move vs SOX in the lookback window.

    Returns {"date": ..., "delta_pp": ...} or None. Trigger threshold: < -5pp.
    """
    sox_col = "MSCI ACWI Semiconductor"
    if sox_col not in history.columns or ticker_name not in history.columns:
        return None
    if len(history) < lookback_days + 1:
        return None

    # Daily returns over lookback window
    recent = history.tail(lookback_days + 1)
    ticker_daily = recent[ticker_name].pct_change().dropna() * 100
    sox_daily = recent[sox_col].pct_change().dropna() * 100
    delta_daily = ticker_daily - sox_daily

    if delta_daily.empty:
        return None

    worst_day = delta_daily.idxmin()
    worst_delta = delta_daily.min()
    return {"date": str(worst_day), "delta_pp": round(worst_delta, 2)}


def get_dram_etf_vs_smh(
    history: pd.DataFrame,
    days: int = 5,
) -> Optional[dict]:
    """Compute DRAM ETF vs SMH (broader semi ETF) over `days` trading days.

    Pure-memory proxy: if DRAM ETF underperforms SMH, that's a clean memory-cycle
    signal independent of any single stock's idiosyncrasies. Trigger: < -5pp.
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

    return {
        "dram_return_pct": round(dram_return, 2),
        "smh_return_pct": round(smh_return, 2),
        "spread_pp": round(delta, 2),
        "fired_threshold": delta < -5.0,
    }


def evaluate_relative_strength_signal(history: pd.DataFrame) -> dict:
    """Evaluate the full t3_relative_strength signal across all 3 sub-triggers.

    Returns a structured verdict for Layer 2 / Layer 3.
    """
    out = {
        "skhx_5d_vs_sox_pp": None,
        "mu_5d_vs_sox_pp": None,
        "skhx_worst_single_day_pp": None,
        "mu_worst_single_day_pp": None,
        "dram_etf_vs_smh": None,
        "fired_sub_triggers": [],
        "overall_fired": False,
        "banner_text": None,
    }

    # Sub-trigger (a): 5-day spread
    skhx_5d = get_relative_strength_vs_sox("SK Hynix", history, days=5)
    mu_5d = get_relative_strength_vs_sox("Micron", history, days=5)
    out["skhx_5d_vs_sox_pp"] = round(skhx_5d, 2) if skhx_5d is not None else None
    out["mu_5d_vs_sox_pp"] = round(mu_5d, 2) if mu_5d is not None else None
    if skhx_5d is not None and skhx_5d < -10:
        out["fired_sub_triggers"].append(f"SK Hynix 5d vs SOX = {skhx_5d:.1f}pp (< -10pp)")
    if mu_5d is not None and mu_5d < -10:
        out["fired_sub_triggers"].append(f"Micron 5d vs SOX = {mu_5d:.1f}pp (< -10pp)")

    # Sub-trigger (b): single-day underperformance
    skhx_worst = get_single_day_max_underperformance("SK Hynix", history, lookback_days=5)
    mu_worst = get_single_day_max_underperformance("Micron", history, lookback_days=5)
    out["skhx_worst_single_day_pp"] = skhx_worst
    out["mu_worst_single_day_pp"] = mu_worst
    if skhx_worst and skhx_worst["delta_pp"] < -5:
        out["fired_sub_triggers"].append(f"SK Hynix single-day vs SOX = {skhx_worst['delta_pp']}pp on {skhx_worst['date'][:10]}")
    if mu_worst and mu_worst["delta_pp"] < -5:
        out["fired_sub_triggers"].append(f"Micron single-day vs SOX = {mu_worst['delta_pp']}pp on {mu_worst['date'][:10]}")

    # Sub-trigger (c): DRAM ETF vs SMH
    dram_vs_smh = get_dram_etf_vs_smh(history, days=5)
    out["dram_etf_vs_smh"] = dram_vs_smh
    if dram_vs_smh and dram_vs_smh["fired_threshold"]:
        out["fired_sub_triggers"].append(f"DRAM ETF vs SMH 5d = {dram_vs_smh['spread_pp']}pp (< -5pp)")

    out["overall_fired"] = bool(out["fired_sub_triggers"])
    if out["overall_fired"]:
        out["banner_text"] = "🚨 RELATIVE STRENGTH ALERT — " + " · ".join(out["fired_sub_triggers"])

    return out
