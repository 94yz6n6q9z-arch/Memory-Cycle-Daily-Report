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

    # v3.41.6: repair stale US tickers (peer-relative, holiday-robust)
    out = _repair_stale_us_tickers(
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
        # v3.42.1 — stable-first (see estimates.py rationale)
        _urls = [
            f"https://financialmodelingprep.com/stable/quote?symbol={symbol}&apikey={api_key}",
            f"https://financialmodelingprep.com/api/v3/quote/{symbol}?apikey={api_key}",
        ]
        url = _urls[0]
        resp = None
        for url in _urls:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                break
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


# ============================================================
# v3.41.6 — US/EU staleness repair (peer-relative, holiday-robust)
# ============================================================
# The KRX repair above was the first fix for yfinance partial-batch
# staleness, but it was scoped to .KS/.KQ only. US-listed names (MU,
# AMAT, ASML, NVDA, hyperscalers) had NO freshness guard, so when the
# batch yf.download returned a stale last bar for a SUBSET of US tickers
# (observed 2026-06-27: NVDA fresh, MU/AMAT/ASML a session behind), the
# stale bar passed straight through calculate_performance's .iloc[-1]
# and rendered as current — a whole row one session stale, invisible.
#
# This guard is deliberately CALENDAR-FREE. Rather than computing an
# "expected US session" (which would need the full NYSE/Nasdaq holiday
# calendar to avoid false-flagging every Thanksgiving / July 4, and
# US/KRX holidays differ so a global expected-session is wrong), it
# compares each US ticker only against ITS OWN MARKET'S PEERS. US
# equities share the NYSE/Nasdaq calendar, so if any peer has a fresher
# bar, the laggards should too — and the laggards get refetched. On a
# genuine US market holiday, every US name shares the same latest bar,
# nothing looks stale relative to peers, and nothing fires. Holidays
# move the peer baseline rather than tripping a false alarm.
#
# Limitation (accepted): if the ENTIRE US group is uniformly stale (all
# names a session behind), peer-relative finds nothing — that rarer
# failure mode is not covered here. The observed bug is a partial subset,
# which this catches exactly.


def _refetch_us_ticker_latest(
    symbol: str,
    expected_ts: pd.Timestamp,
    fmp_api_key: Optional[str] = None,
) -> Optional[dict]:
    """Re-pull a single US ticker's latest close, targeting expected_ts.

    The observed failure is a partial BATCH — yfinance had the fresh bar
    (it served it to peers), the batch just didn't populate it for this
    symbol. A single-ticker re-download usually resolves it. Falls back
    to FMP's ticker-agnostic /quote endpoint if yfinance is still short.
    Returns {price, asof, source} or None.
    """
    # 1. single-ticker yfinance re-download (short window)
    try:
        df = yf.download(
            tickers=symbol, period="5d", interval="1d",
            auto_adjust=True, progress=False,
        )
        if df is not None and not df.empty and "Close" in df:
            close = df["Close"].dropna()
            if not close.empty:
                last_ts = pd.Timestamp(close.index[-1]).normalize()
                if last_ts >= expected_ts:
                    val = close.iloc[-1]
                    # single-ticker frame can return a 1-col DataFrame
                    if hasattr(val, "iloc"):
                        val = val.iloc[0]
                    return {
                        "price": float(val),
                        "asof": last_ts.isoformat(),
                        "source": "yfinance-single",
                    }
    except Exception as e:
        logger.warning(f"yfinance single re-download failed for {symbol}: {e}")

    # 2. FMP /quote — the endpoint is ticker-agnostic (works for US too),
    #    reusing the same helper the KRX path uses.
    quote = _fetch_krx_quote_from_fmp(symbol, fmp_api_key) if fmp_api_key else None
    if quote is not None:
        return quote

    return None


def _repair_stale_us_tickers(
    history: pd.DataFrame,
    tickers: dict[str, str],
    fmp_api_key: Optional[str] = None,
    collector: Optional[DiagnosticsCollector] = None,
) -> pd.DataFrame:
    """Peer-relative staleness repair for US-listed tickers.

    US group = symbols that are NOT KRX (.KS/.KQ) and NOT indices (^...).
    Within the group, the freshest last-valid-bar date is the peer
    baseline; any ticker older than the baseline is stale relative to its
    market and gets refetched (single-ticker yfinance → FMP). Calendar-free
    by construction (see module note above). Mutates and returns history.
    """
    us_tickers = {
        name: symbol for name, symbol in tickers.items()
        if not (symbol.endswith(".KS") or symbol.endswith(".KQ"))
        and not symbol.startswith("^")
    }
    if len(us_tickers) < 2:
        return history  # need peers to compare against

    # last-valid-bar date per US ticker present in history
    last_dates: dict[str, pd.Timestamp] = {}
    for name in us_tickers:
        if name not in history.columns:
            continue
        series = history[name].dropna()
        if len(series) == 0:
            continue
        last_dates[name] = pd.Timestamp(series.index[-1]).normalize()

    if len(last_dates) < 2:
        return history

    baseline_ts = max(last_dates.values())

    repairs_attempted = 0
    repairs_succeeded = 0
    repair_details = []

    for name, symbol in us_tickers.items():
        latest_ts = last_dates.get(name)
        if latest_ts is None or latest_ts >= baseline_ts:
            continue  # fresh relative to peers

        repairs_attempted += 1
        days_stale = (baseline_ts - latest_ts).days
        logger.warning(
            f"US ticker {symbol} ({name}) stale vs peers: "
            f"latest={latest_ts.date()}, peer baseline={baseline_ts.date()} "
            f"({days_stale}d behind). Refetching."
        )

        fresh = _refetch_us_ticker_latest(symbol, baseline_ts, fmp_api_key)
        if fresh is None:
            prev = history[name].dropna().iloc[-1]
            logger.error(
                f"Could not repair stale {symbol} via yfinance or FMP — "
                f"keeping value {prev:.2f} from {latest_ts.date()}. "
                f"Brief will display this stale value."
            )
            repair_details.append(f"{symbol}: failed (refetch exhausted)")
            continue

        history.loc[baseline_ts, name] = fresh["price"]
        history = history.sort_index()
        repairs_succeeded += 1
        logger.info(
            f"Repaired {symbol} via {fresh['source']}: latest close = "
            f"{fresh['price']:.2f} (was {history[name].dropna().iloc[-2]:.2f} "
            f"from {latest_ts.date()})"
        )
        repair_details.append(f"{symbol}: repaired via {fresh['source']}")

    if repairs_attempted > 0 and collector is not None:
        with collector.track("US staleness repair", "internal") as t:
            t.items_returned = repairs_succeeded
            t.detail = (
                f"{repairs_succeeded}/{repairs_attempted} US tickers repaired. "
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


def get_relative_strength_vs_benchmark(
    ticker_name: str,
    history: pd.DataFrame,
    days: int = 5,
    benchmark_col: str = "S&P 500",
) -> Optional[float]:
    """Ticker's relative performance vs a benchmark column over `days`
    trading days. Negative = underperformance (percentage points).

    v3.43.0 — generalized from the retired get_relative_strength_vs_sox.
    The SOX benchmark is REMOVED from the t3_relative_strength signal
    entirely (Raul, Jul-31: it is ~40% memory — self-referential and
    volatile, the spread mean-reverts mid-crash). The v3.4.6.2 calendar-
    alignment fix is preserved verbatim: KRX and US calendars misalign at
    the tail, so both series are projected onto rows where BOTH have
    values before taking the window endpoints.
    """
    if benchmark_col not in history.columns or ticker_name not in history.columns:
        return None
    aligned = history[[ticker_name, benchmark_col]].dropna()
    if len(aligned) < days + 1:
        return None
    t_end = aligned[ticker_name].iloc[-1]
    t_start = aligned[ticker_name].iloc[-days - 1]
    b_end = aligned[benchmark_col].iloc[-1]
    b_start = aligned[benchmark_col].iloc[-days - 1]
    if any(pd.isna(v) for v in (t_end, t_start, b_end, b_start)):
        return None
    if t_start == 0 or b_start == 0:
        return None
    t_ret = (t_end / t_start - 1) * 100
    b_ret = (b_end / b_start - 1) * 100
    if pd.isna(t_ret) or pd.isna(b_ret):
        return None
    return t_ret - b_ret

def evaluate_relative_strength_signal(
    history: pd.DataFrame,
    state_path: Optional[str] = None,
) -> dict:
    """t3_relative_strength, v3.43.0 rebuild.

    Benchmark: S&P 500 ONLY. SOX and DRAM-ETF-vs-SMH are removed from the
    fire path and from the verdict entirely — a memory-heavy benchmark
    mean-reverts against memory names precisely during a memory unwind
    (fired Jul-6-2026, un-fired mid-crash, re-fired Jul-31).

    Anti-flap design (Raul, Jul-31 — "in a way that volatility doesn't
    turn it on and off all the time"):
      FIRE (red):  any name with 5d spread < -10pp AND 20d spread < -10pp
                   — fast entry, trend-confirmed; one bad day can't trip it.
      WATCH (yellow): any name with 5d < -5pp AND 20d < -5pp (not red).
      STICKY RELEASE: once red, stays red until the worst 5d spread holds
                   above -5pp for 5 CONSECUTIVE sessions. Fires fast,
                   releases slow. Latch state: state/rel_strength_state.json.

    Thresholds provisional pending the backtest replay.
    """
    import json as _json
    from pathlib import Path as _Path

    out = {
        "skhx_5d_vs_spx_pp": None,
        "mu_5d_vs_spx_pp": None,
        "skhx_20d_vs_spx_pp": None,
        "mu_20d_vs_spx_pp": None,
        "fired_sub_triggers": [],      # list of (severity, description)
        "overall_severity": None,      # 'red' | 'yellow' | None (post-latch)
        "raw_severity": None,          # pre-latch, for the log
        "overall_fired": False,
        "banner_text": None,
        "latch": {"latched": None, "recovery_streak": 0},
        "benchmark": "S&P 500",
    }

    spreads = {}
    for name, k5, k20 in (("SK Hynix", "skhx_5d_vs_spx_pp", "skhx_20d_vs_spx_pp"),
                          ("Micron", "mu_5d_vs_spx_pp", "mu_20d_vs_spx_pp")):
        s5 = get_relative_strength_vs_benchmark(name, history, days=5)
        s20 = get_relative_strength_vs_benchmark(name, history, days=20)
        out[k5] = round(s5, 2) if s5 is not None else None
        out[k20] = round(s20, 2) if s20 is not None else None
        spreads[name] = (out[k5], out[k20])

    raw = None
    for name, (s5, s20) in spreads.items():
        if s5 is None or s20 is None:
            continue
        if s5 < -10.0 and s20 < -10.0:
            raw = "red"
            out["fired_sub_triggers"].append(
                ("red", f"{name} vs S&P 500: 5d {s5:+.1f}pp / 20d {s20:+.1f}pp "
                        f"(both < -10pp — RED fire)"))
        elif s5 < -5.0 and s20 < -5.0:
            if raw is None:
                raw = "yellow"
            out["fired_sub_triggers"].append(
                ("yellow", f"{name} vs S&P 500: 5d {s5:+.1f}pp / 20d {s20:+.1f}pp "
                           f"(both < -5pp — yellow watch)"))
    out["raw_severity"] = raw

    # ---- sticky release latch -------------------------------------------
    sp = _Path(state_path or "state/rel_strength_state.json")
    try:
        latch = _json.loads(sp.read_text())
    except Exception:
        latch = {"latched": None, "recovery_streak": 0}

    severity = raw
    if raw == "red":
        latch = {"latched": "red", "recovery_streak": 0}
    elif latch.get("latched") == "red":
        vals5 = [v for v, _ in spreads.values() if v is not None]
        worst5 = min(vals5) if vals5 else None
        if worst5 is not None and worst5 > -5.0:
            latch["recovery_streak"] = int(latch.get("recovery_streak", 0)) + 1
        else:
            latch["recovery_streak"] = 0
        if latch["recovery_streak"] >= 5:
            latch = {"latched": None, "recovery_streak": 0}
        else:
            severity = "red"
            out["fired_sub_triggers"].append(
                ("red", f"RED latched — recovery {latch['recovery_streak']}/5 "
                        f"sessions (release requires 5d spread > -5pp for 5 "
                        f"consecutive sessions)"))
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(_json.dumps(latch))
    except Exception:
        pass
    out["latch"] = latch

    out["overall_severity"] = severity
    out["overall_fired"] = severity == "red"
    if severity:
        prefix = ("🚨 RELATIVE STRENGTH RED FIRE" if severity == "red"
                  else "🟡 RELATIVE STRENGTH YELLOW WATCH")
        out["banner_text"] = f"{prefix} — " + " · ".join(
            d for _, d in out["fired_sub_triggers"])
    return out

def compute_hbm_price_trend(
    spot_history_csv: Optional[Path] = None,
    contract_forecasts_csv: Optional[Path] = None,
    lookback_days: int = 90,
) -> str:
    """Derive HBM/DRAM price trend direction from accumulated CSV data.

    v3.4.3 — replaces the operator-supplied 'hbm_price_trend' parameter
    that compute_sanity_check() in src/analysis/dhcr.py previously required.
    HBM itself is contract-only (no spot market), so we use:
      (a) DDR5 16Gb spot trajectory over trailing N days (correlated proxy
          for memory tightness in general; HBM rolls with DRAM cycle)
      (b) Quarterly contract forecast direction (latest published vs
          prior published for same quarter — TrendForce revisions)

    Combined logic:
      - Both DDR5 spot trending up AND contract revisions up   → "rising"
      - Both DDR5 spot trending down AND contract revisions down → "falling"
      - Mixed or near-flat                                       → "flat"

    Returns one of: "rising" | "flat" | "falling".

    For the DHCR sanity check this answers: "Is the memory market validating
    the matrix's bullish-HBM read by also showing tight prices?"
      rising  = matrix bullish + prices firm → confirmed
      falling = matrix bullish + prices weak → contradicting (calibration concern)

    Args:
        spot_history_csv: Path to dram_spot_history.csv (default: data/...)
        contract_forecasts_csv: Path to dram_contract_quarterly_forecasts.csv
        lookback_days: window for spot trend slope (default 90 days)

    Returns:
        "rising" | "flat" | "falling"
    """
    from pathlib import Path as _Path
    if spot_history_csv is None:
        spot_history_csv = _Path("data/dram_spot_history.csv")
    if contract_forecasts_csv is None:
        contract_forecasts_csv = _Path("data/dram_contract_quarterly_forecasts.csv")

    spot_direction = "flat"
    contract_direction = "flat"

    # (a) Spot trend — slope of session_avg over trailing N days
    try:
        if spot_history_csv.exists():
            df = pd.read_csv(spot_history_csv)
            df["date"] = pd.to_datetime(df["date"])
            cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=lookback_days)
            # Focus on the canonical SKU (DDR5_16Gb_4800_5600) per cycle matrix
            df = df[(df["sku"] == "DDR5_16Gb_4800_5600") & (df["date"] >= cutoff)]
            df = df.sort_values("date")
            if len(df) >= 5:
                # Simple linear slope on session_avg vs day-index
                df["day_idx"] = (df["date"] - df["date"].min()).dt.days
                slope = float(
                    ((df["day_idx"] * df["session_avg"]).mean()
                     - df["day_idx"].mean() * df["session_avg"].mean())
                    / max(((df["day_idx"] ** 2).mean() - df["day_idx"].mean() ** 2), 1e-9)
                )
                # Slope thresholds: $/day. For DDR5 16Gb at ~$40 base,
                # >$0.05/day = rising trend; <-$0.05/day = falling
                if slope > 0.05:
                    spot_direction = "rising"
                elif slope < -0.05:
                    spot_direction = "falling"
                else:
                    spot_direction = "flat"
    except Exception as e:
        logger.warning(f"compute_hbm_price_trend: spot trend computation failed: {e}")

    # (b) Contract forecast direction — for the latest quarter, compare the
    # most recent forecast central_pct against the prior forecast for the
    # same quarter. Revisions UP = forecast rising; revisions DOWN = falling.
    try:
        if contract_forecasts_csv.exists():
            df = pd.read_csv(contract_forecasts_csv)
            df["published_date"] = pd.to_datetime(df["published_date"])
            df = df.sort_values("published_date")
            # Get the most recent quarter that has multiple forecasts
            latest_q = df["quarter"].iloc[-1] if len(df) else None
            if latest_q:
                same_q = df[df["quarter"] == latest_q].sort_values("published_date")
                if len(same_q) >= 2:
                    earliest = float(same_q.iloc[0]["central_pct"])
                    latest = float(same_q.iloc[-1]["central_pct"])
                    delta = latest - earliest
                    # >3pp revision = directional move
                    if delta > 3.0:
                        contract_direction = "rising"
                    elif delta < -3.0:
                        contract_direction = "falling"
    except Exception as e:
        logger.warning(f"compute_hbm_price_trend: contract trend computation failed: {e}")

    # Combine — both signals must agree to call rising/falling
    if spot_direction == "rising" and contract_direction != "falling":
        return "rising"
    if spot_direction == "falling" and contract_direction != "rising":
        return "falling"
    if contract_direction == "rising" and spot_direction != "falling":
        return "rising"
    if contract_direction == "falling" and spot_direction != "rising":
        return "falling"
    return "flat"
