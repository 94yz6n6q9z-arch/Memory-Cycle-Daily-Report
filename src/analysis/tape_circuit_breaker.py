"""v3.42.0 — Tape Circuit Breaker (TCB).

The July 2026 unwind proved the fundamental stack cannot protect the
position at a top: every Tier 1–4 indicator is a fundamentals/narrative
detector, they lag price by construction, and the brief printed "+13%
upside to peak" while the tape was three weeks and −30% into the crash.
The TCB is the missing organ: a deterministic price/volume state machine
with NO LLM anywhere in the loop, computed before any model runs, and
carrying explicit action semantics chosen by Raul:

    L1  TAPE ALERT                 — the tape disagrees with the thesis
    L2  DE-RISK — peak likely behind us
    L3  SELL — thesis stopped out

Components (all trailing daily closes; volume where available):
    shock          — any day in the trailing 15 sessions with a return
                     ≤ −3σ of the prior 120d daily returns, or ≤ −8%.
    drawdown       — last close ≤ −20% from the trailing 250d high
                     (−12% = watch, reported but not fired).
    distribution   — ≥4 days in the trailing 20 sessions closing ≤ −1.5%
                     on volume ≥ 1.3× the trailing 50d median volume.
                     Degrades to no_data when volume is unavailable.
    trend_break    — close < 50d MA, 50d MA falling (vs 10 sessions ago),
                     and 20d MA < 50d MA.
    failed_good_news — reserved (Release B): price falling through
                     positive catalysts. Reported as not_implemented.

Levels (ratchet up instantly, decay only through hysteresis):
    L1: any component fired.
    L2: ≥3 components fired, OR shock + drawdown together.
    L3: L2 AND (trend_break OR drawdown ≤ −30%).
De-escalation requires BOTH: 10 consecutive closes above the 50d MA AND
drawdown recovered inside −15%. Until then the persisted level holds even
if components individually relax — a breaker that flaps is worthless.

The module is deliberately self-contained: it fetches its own OHLCV (the
pipeline's shared price frame keeps adjusted closes only), never raises
into the run, and persists state to state/tcb_state.json.

v3.46.0 — BREAKING + Distribution v2 (calibrated 2026-08-08, five Colab
iterations over 2018 / 2021-22 / Iran-Mar-26 / early-June-26 / Jun-Aug-26 /
Aug-24 / COVID, on 000660.KS and MU):

    breaking       — idiosyncratic shock-cluster escalation. Each daily
                     shock is classified MACRO-excused vs IDIO by the
                     BENCHMARK'S OWN sigma state (Raul's rule: "losing 10%
                     for the S&P is like losing 50% for SK Hynix" — fixed-%
                     cuts cannot separate a war outbreak from an
                     idiosyncratic cascade; sigma-scaled event state can):
                     a session is MACRO when S&P running drawdown ≤ −4σ of
                     its own 60d vol OR a single S&P day ≤ −2σ, worst-of
                     (t, t−1) for the KRX close-time offset. ≥3 IDIO shocks
                     in 10 sessions, latched 5 sessions, AND the persisted
                     framework phase is 4/5 (two-key rule) → the stop-out
                     gate promotes from −30% to −20%. The −30% base stop
                     stays UNCONDITIONAL — macro crashes ride to −30% by
                     design; only our-stock-alone cascades exit at −20%.
                     Named counterexamples (executable in tests):
                       · Iran Mar-2026: Mar-3/4 IDIO (day-zero), Mar-9
                         MACRO at S&P dd −4.7σ → cluster stalls at 2 → no
                         promotion; the stock then ran +73%. Exiting there
                         is the canonical forbidden outcome.
                       · Real crash: Jun-23/Jun-26/Jul-2 all IDIO (S&P
                         within ~1–4σ of highs) → promoted gate exits
                         Jul-2 at −25% vs the base stop's Jul-13 at −37%.
                       · early-June-26 / Aug-24 / COVID: excused → no
                         change vs base. 2018/2022 tops: no idio clusters
                         → identical to base.
                     BREAKING is frame-derived and stateless (the 2y frame
                     recomputes the ledger each run — silence on quiet days
                     cannot lose the cluster). It never counts toward the
                     L1/L2 component tally; it ONLY moves the stop-out
                     gate, plus a cascade-forming warning at 2-in-10.
    distribution   — REPLACED by v2 (Raul's override ruling 2026-08-08;
                     v1 was never backtested). v2 = five AND-gates:
                     up/down-volume ratio < 0.75 (dominance, not
                     presence) · OBV 20d slope < 0 while price > 200dMA
                     (flow rolls over pre-price) · ≥5 distribution days /
                     25 (the old v1 idea, demoted to one gate of five) ·
                     maturity (above 200dMA ≥90% of 150 sessions) · lower
                     highs (5d max < 97% of 60d max). Live release: on
                     fire, watermark = trailing 60d high; a close ≥
                     watermark = the dip was absorbed → release, re-arm
                     (this is what kept Iran and early-June quiet while
                     the July fires survived). Degrades loudly to the v1
                     counter when the frame is short (<360 rows) or
                     high/low are unavailable, and to no_data without
                     volume — a breaker component that silently disables
                     is worse than a crude one that works.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

TCB_STATE_FILE = Path("state/tcb_state.json")
TCB_SYMBOL = "000660.KS"

ACTION_LABELS = {
    0: "CLEAR",
    1: "TAPE ALERT",
    2: "DE-RISK — peak likely behind us",
    3: "SELL — thesis stopped out",
}

# Thresholds (provisional pending the tcb_backtest calibration replay of
# 2018 / 2021-22 / 2024 / 2026 — see scripts/tcb_backtest.py).
SHOCK_SIGMA = 3.0
SHOCK_ABS = -0.08
SHOCK_LOOKBACK = 15
DD_FIRE = -0.20
DD_WATCH = -0.12
DD_STOPOUT = -0.30
DIST_RET = -0.015
DIST_VOL_MULT = 1.3
DIST_WINDOW = 20
DIST_MIN_DAYS = 4
PEAK_WINDOW = 250
DEESC_SESSIONS_ABOVE_MA50 = 10
DEESC_DD_RECOVERY = -0.15

# v3.46.0 — BREAKING (idiosyncratic shock-cluster gate promotion).
BENCH_SYMBOL = "^GSPC"
SP_VOL_WIN = 60
SP_DD_SIGMA = -4.0        # S&P running dd ≤ −4σ of its own vol → MACRO
SP_DAY_SIGMA = -2.0       # or a single S&P day ≤ −2σ → MACRO
BRK_CLUSTER_N = 3         # idio shocks to arm
BRK_CLUSTER_WIN = 10      # …within this many sessions
BRK_WARN_N = 2            # cascade-forming warning level
BRK_LATCH = 5             # sessions the armed state persists (rel-strength pattern)
DD_STOPOUT_BREAKING = -0.20

# v3.46.0 — Distribution v2 (five-gate regime classifier).
D2_MIN_ROWS = 360         # 200dMA + 150-session maturity lookback
D2_UDVR_WIN = 20
D2_UDVR_FIRE = 0.75
D2_DIST_WIN = 25
D2_DIST_MIN = 5
D2_DIST_DOWN = -0.002
D2_MATURE_MA = 200
D2_MATURE_LOOKBACK = 150
D2_MATURE_FRAC = 0.90
D2_LH_WIN = 60
D2_LH_TOL = 0.97
D2_RELEASE_WIN = 60


def fetch_tcb_ohlcv(symbol: str = TCB_SYMBOL, period: str = "2y") -> Optional[pd.DataFrame]:
    """Own OHLCV fetch (Close + Volume). Safe-failing: None on any error."""
    try:
        import yfinance as yf
        df = yf.download(tickers=symbol, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            logger.warning("[tcb] OHLCV fetch returned empty frame")
            return None
        # yfinance may return MultiIndex columns for single tickers too.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        cols = {c.lower(): c for c in df.columns}
        if "close" not in cols:
            logger.warning(f"[tcb] no Close column in OHLCV frame ({list(df.columns)})")
            return None
        out = pd.DataFrame(index=df.index)
        out["close"] = pd.to_numeric(df[cols["close"]], errors="coerce")
        out["volume"] = (
            pd.to_numeric(df[cols["volume"]], errors="coerce")
            if "volume" in cols else pd.NA
        )
        # v3.46.0 — Distribution v2's distribution-day gate needs the close's
        # position in the daily range (clv < 0.5).
        for extra in ("high", "low"):
            out[extra] = (
                pd.to_numeric(df[cols[extra]], errors="coerce")
                if extra in cols else pd.NA
            )
        out = out.dropna(subset=["close"])
        return out if len(out) >= 60 else None
    except Exception as e:
        logger.warning(f"[tcb] OHLCV fetch failed (non-fatal): {e}")
        return None


def fetch_benchmark_closes(symbol: str = BENCH_SYMBOL,
                           period: str = "2y") -> Optional[pd.Series]:
    """v3.46.0 — benchmark closes for BREAKING's macro-excusal. Safe-failing:
    None on any error; BREAKING then degrades to no_benchmark and the gate
    stays at the −30% base (fail-safe = base behaviour)."""
    try:
        import yfinance as yf
        df = yf.download(tickers=symbol, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            logger.warning("[tcb] benchmark fetch returned empty frame")
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        cols = {c.lower(): c for c in df.columns}
        if "close" not in cols:
            return None
        s = pd.to_numeric(df[cols["close"]], errors="coerce").dropna()
        return s if len(s) >= SP_VOL_WIN + 20 else None
    except Exception as e:
        logger.warning(f"[tcb] benchmark fetch failed (non-fatal): {e}")
        return None


# ---------------------- v3.46.0 pure helpers ----------------------------

def daily_shock_flags(close: pd.Series) -> pd.Series:
    """Per-day shock series, production semantics: return ≤
    max(SHOCK_ABS, −SHOCK_SIGMA · σ) with σ = trailing 120d std (shifted —
    a day is judged against the vol regime that PRECEDED it). All seven
    calibration windows are σ-window-invariant here: every relevant day
    breached the −8% absolute cap outright."""
    rets = close.pct_change()
    sigma = rets.rolling(120, min_periods=30).std().shift(1)
    thr = (-SHOCK_SIGMA * sigma).clip(lower=SHOCK_ABS)
    thr = thr.fillna(SHOCK_ABS)
    return (rets <= thr).fillna(False)


def benchmark_stress(sp_close: pd.Series, target_index) -> pd.DataFrame:
    """Benchmark stress in the benchmark's OWN sigma units, worst-of
    (t, t−1) to bridge the KRX/NYSE close-time offset, reindexed to the
    target calendar. Columns: day_sig, dd_sig."""
    spr = sp_close.pct_change()
    vol = spr.rolling(SP_VOL_WIN).std()
    dd = sp_close / sp_close.rolling(PEAK_WINDOW, min_periods=20).max() - 1
    day_sig = spr / vol
    dd_sig = dd / vol
    worst = lambda s: pd.concat([s, s.shift(1)], axis=1).min(axis=1)
    return pd.DataFrame({
        "day_sig": worst(day_sig).reindex(target_index, method="ffill"),
        "dd_sig": worst(dd_sig).reindex(target_index, method="ffill"),
    })


def compute_breaking(close: pd.Series,
                     sp_close: Optional[pd.Series],
                     phase_late: bool) -> dict:
    """Frame-derived, stateless BREAKING ledger (see module docstring)."""
    out = {"status": "no_benchmark", "active": False, "latched": False,
           "warning": False, "idio_count": 0, "gate_pct": DD_STOPOUT * 100,
           "phase_late": bool(phase_late), "recent_shocks": []}
    if sp_close is None or len(sp_close) < SP_VOL_WIN + 20:
        return out
    shocks = daily_shock_flags(close)
    stress = benchmark_stress(sp_close, close.index)
    macro = ((stress["dd_sig"] <= SP_DD_SIGMA) |
             (stress["day_sig"] <= SP_DAY_SIGMA)).fillna(False)
    idio = shocks & ~macro
    idio_count = int(idio.iloc[-BRK_CLUSTER_WIN:].sum())
    armed_series = idio.rolling(BRK_CLUSTER_WIN).sum() >= BRK_CLUSTER_N
    armed_now = bool(armed_series.iloc[-1])
    latched = bool(armed_series.iloc[-BRK_LATCH:].any())
    active = latched and bool(phase_late)
    rets = close.pct_change()
    recent = []
    for ts in close.index[-45:]:
        if shocks.loc[ts]:
            recent.append({
                "date": str(pd.Timestamp(ts).date()),
                "ret_pct": round(float(rets.loc[ts]) * 100, 2),
                "sp_day_sig": (round(float(stress.loc[ts, "day_sig"]), 2)
                               if pd.notna(stress.loc[ts, "day_sig"]) else None),
                "sp_dd_sig": (round(float(stress.loc[ts, "dd_sig"]), 2)
                              if pd.notna(stress.loc[ts, "dd_sig"]) else None),
                "class": "MACRO-excused" if macro.loc[ts] else "IDIO",
            })
    out.update({
        "status": "data_ok",
        "idio_count": idio_count,
        "warning": (idio_count == BRK_WARN_N) and not armed_now,
        "armed_now": armed_now,
        "latched": latched,
        "active": active,
        "gate_pct": (DD_STOPOUT_BREAKING if active else DD_STOPOUT) * 100,
        "recent_shocks": recent,
    })
    return out


def compute_distribution_v2(df: pd.DataFrame) -> dict:
    """Distribution v2 with loud degradation ladder (see module docstring).
    Returns the v1-compatible shape (fired/status/days/needed) plus v2
    detail. status ∈ data_ok(v2) / v1_fallback_short_frame /
    v1_fallback_no_hilo / no_data."""
    base = {"fired": False, "status": "no_data", "days": None,
            "needed": D2_DIST_MIN, "mode": "v2"}
    vol_raw = df["volume"] if "volume" in df.columns else None
    if vol_raw is None or pd.to_numeric(vol_raw, errors="coerce").notna().sum() < 60:
        return base
    close = df["close"]
    volume = pd.to_numeric(vol_raw, errors="coerce")
    have_hilo = ("high" in df.columns and "low" in df.columns
                 and pd.to_numeric(df["high"], errors="coerce").notna().sum() >= 60)

    def _v1(status: str) -> dict:
        rets = close.pct_change()
        med50 = volume.rolling(50).median()
        win_ret = rets.iloc[-DIST_WINDOW:]
        win_vol = volume.iloc[-DIST_WINDOW:]
        win_med = med50.iloc[-DIST_WINDOW:]
        mask = (win_ret <= DIST_RET) & (win_vol >= DIST_VOL_MULT * win_med)
        n = int(mask.fillna(False).sum())
        return {"fired": n >= DIST_MIN_DAYS, "status": status, "days": n,
                "needed": DIST_MIN_DAYS, "mode": "v1_fallback"}

    if len(close) < D2_MIN_ROWS:
        return _v1("v1_fallback_short_frame")
    if not have_hilo:
        return _v1("v1_fallback_no_hilo")

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    rets = close.pct_change()
    up = volume.where(rets > 0, 0).rolling(D2_UDVR_WIN).sum()
    dn = volume.where(rets < 0, 0).rolling(D2_UDVR_WIN).sum()
    udvr = up / dn.replace(0, pd.NA)
    obv = ((rets > 0).astype(int) - (rets < 0).astype(int)).mul(volume).cumsum()
    ma200 = close.rolling(D2_MATURE_MA).mean()
    obv_div = (obv.diff(D2_UDVR_WIN) < 0) & (close > ma200)
    clv = (close - low) / (high - low).replace(0, pd.NA)
    dist_days = ((rets < D2_DIST_DOWN) & (volume > volume.shift(1))
                 & (clv < 0.5)).rolling(D2_DIST_WIN).sum()
    mature = (close > ma200).rolling(D2_MATURE_LOOKBACK).mean() > D2_MATURE_FRAC
    lower_highs = close.rolling(5).max() < close.rolling(D2_LH_WIN).max() * D2_LH_TOL
    raw = ((udvr < D2_UDVR_FIRE) & obv_div & (dist_days >= D2_DIST_MIN)
           & mature & lower_highs).fillna(False).astype(bool)

    hi = close.rolling(D2_RELEASE_WIN).max()
    watermark = None
    fired_now = False
    for i in range(len(close)):
        px = float(close.iloc[i])
        if watermark is not None and px >= watermark:
            watermark = None
        if watermark is None and bool(raw.iloc[i]):
            watermark = float(hi.iloc[i])
        fired_now = watermark is not None
    last = close.index[-1]
    return {
        "fired": bool(fired_now),
        "status": "data_ok",
        "days": int(dist_days.iloc[-1]) if pd.notna(dist_days.iloc[-1]) else None,
        "needed": D2_DIST_MIN,
        "mode": "v2",
        "udvr": round(float(udvr.iloc[-1]), 2) if pd.notna(udvr.iloc[-1]) else None,
        "mature": bool(mature.iloc[-1]) if pd.notna(mature.iloc[-1]) else False,
        "lower_highs": bool(lower_highs.iloc[-1]),
        "watermark": round(watermark, 2) if watermark is not None else None,
        "raw_today": bool(raw.iloc[-1]),
        "asof": str(pd.Timestamp(last).date()),
    }


def _read_state() -> dict:
    try:
        return json.loads(TCB_STATE_FILE.read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        TCB_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TCB_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"[tcb] state write failed (non-fatal): {e}")


def compute_tcb_components(df: pd.DataFrame,
                           sp_close: Optional[pd.Series] = None,
                           phase_late: bool = False) -> dict:
    """Pure component evaluation from an OHLCV frame (close, volume, and —
    v3.46.0 — high/low when available). sp_close/phase_late feed the
    BREAKING gate; both default to inert (backward compatible: existing
    callers get exactly the base −30% stop-out behaviour)."""
    close = df["close"]
    rets = close.pct_change()
    comp: dict = {}

    # --- shock ------------------------------------------------------------
    shock = {"fired": False, "worst_day": None, "worst_ret_pct": None,
             "threshold_pct": None}
    if len(rets.dropna()) > 30:
        recent = rets.iloc[-SHOCK_LOOKBACK:]
        base = rets.iloc[:-1].dropna().iloc[-120:]
        sigma = float(base.std()) if len(base) >= 30 else None
        # v3.43.0 — max(), not min(): fire on the EASIER of -8% / -3σ. The
        # shipped min() made the breaker harder exactly when volatility
        # exploded (crash-era σ pushed -3σ past -10%, so Jul-29's -10.3%
        # day didn't count as a shock). Masked in tests by a calm synthetic
        # run-up where min() happened to pick -8%.
        thr = max(SHOCK_ABS, -SHOCK_SIGMA * sigma) if sigma else SHOCK_ABS
        shock["threshold_pct"] = round(thr * 100, 2)
        hits = recent[recent <= thr]
        if len(hits) > 0:
            worst_idx = hits.idxmin()
            shock.update({
                "fired": True,
                "worst_day": str(pd.Timestamp(worst_idx).date()),
                "worst_ret_pct": round(float(hits.min()) * 100, 2),
            })
    comp["shock"] = shock

    # --- drawdown ---------------------------------------------------------
    window = close.iloc[-PEAK_WINDOW:]
    peak_val = float(window.max())
    peak_day = str(pd.Timestamp(window.idxmax()).date())
    last = float(close.iloc[-1])
    dd = last / peak_val - 1.0
    # v3.46.0 — BREAKING computes first so the stop-out gate can promote.
    breaking = compute_breaking(close, sp_close, phase_late)
    comp["breaking"] = breaking
    effective_stopout = (DD_STOPOUT_BREAKING if breaking.get("active")
                         else DD_STOPOUT)
    comp["drawdown"] = {
        "fired": dd <= DD_FIRE,
        "watch": dd <= DD_WATCH,
        # stopout_depth drives the ladder and reflects the EFFECTIVE gate;
        # the −30% base is reported alongside and is never modified.
        "stopout_depth": dd <= effective_stopout,
        "stopout_depth_base": dd <= DD_STOPOUT,
        "stopout_gate_pct": round(effective_stopout * 100, 1),
        "drawdown_pct": round(dd * 100, 2),
        "peak_price": peak_val,
        "peak_date": peak_day,
        "last_close": last,
        "last_date": str(pd.Timestamp(close.index[-1]).date()),
    }

    # --- distribution (volume) ---------------------------------------------
    # v3.46.0 — v2 five-gate classifier with loud degradation ladder
    # (v1 counter retained inside compute_distribution_v2 as the short-frame
    # / no-high-low fallback). Raul's override ruling 2026-08-08.
    comp["distribution"] = compute_distribution_v2(df)

    # --- trend break --------------------------------------------------------
    trend = {"fired": False, "status": "no_data",
             "reason": f"insufficient history ({len(close)} rows < 65)"}
    if len(close) >= 65:
        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma50_now = float(ma50.iloc[-1])
        ma50_prev = float(ma50.iloc[-11])
        ma20_now = float(ma20.iloc[-1])
        import math as _math
        if any(_math.isnan(v) for v in (ma50_now, ma50_prev, ma20_now)):
            # v3.43.0 — NaN inputs previously produced a silent fired=False
            # with status data_ok. NaN is no_data, loudly.
            trend = {"status": "no_data", "fired": False,
                     "reason": "NaN in MA inputs",
                     "ma50": ma50_now, "ma20": ma20_now}
        else:
            trend = {
                "status": "data_ok",
                "fired": bool(last < ma50_now and ma50_now < ma50_prev
                              and ma20_now < ma50_now),
                "close": last,
                "ma50": round(ma50_now, 2),
                "ma20": round(ma20_now, 2),
                "ma50_falling": ma50_now < ma50_prev,
                "ma20_below_ma50": ma20_now < ma50_now,
                "reason": None,
            }
    comp["trend_break"] = trend

    comp["failed_good_news"] = {"fired": False, "status": "not_implemented"}

    # --- de-escalation preconditions ---------------------------------------
    above = None
    if len(close) >= 65:
        ma50s = close.rolling(50).mean()
        tail = (close.iloc[-DEESC_SESSIONS_ABOVE_MA50:]
                > ma50s.iloc[-DEESC_SESSIONS_ABOVE_MA50:])
        above = bool(tail.all())
    comp["_deescalation"] = {
        "closes_above_ma50_streak_ok": above,
        "dd_recovered": dd > DEESC_DD_RECOVERY,
    }
    return comp


def compute_tcb_level(comp: dict) -> int:
    """v3.43.0 ladder. The shipped version gated L3 behind L2's component
    count, so a -52% drawdown sat at L1 when the shock day rolled out of
    its 15-session window and trend reported nothing. A stop-out-depth
    drawdown (<= -30% from the 250d high) now escalates ON ITS OWN — no
    committee of components required to declare the thesis stopped out."""
    fired = [k for k in ("shock", "drawdown", "distribution", "trend_break",
                         "failed_good_news") if comp.get(k, {}).get("fired")]
    n = len(fired)
    stopout = bool(comp["drawdown"].get("stopout_depth"))
    level = 0
    if n >= 1:
        level = 1
    if n >= 3 or (comp["shock"]["fired"] and comp["drawdown"]["fired"]) or stopout:
        level = 2
    if level >= 2 and (comp["trend_break"].get("fired") or stopout):
        level = 3
    return level


def _load_phase_late() -> bool:
    """v3.46.0 — two-key rule: BREAKING may promote the gate only when the
    PERSISTED framework phase is 4/5. The TCB runs before this run's phase
    detection (fast_tier sequencing), so the prior persisted phase is both
    the only phase available and the causally correct one — the regime gate
    is a standing state, not this run's output. Fail-safe: unreadable or
    missing state → False → gate stays at the −30% base."""
    try:
        from src.state import load_phase_state
        phase = load_phase_state().get("current_phase")
        return phase in (4, 5)
    except Exception as e:
        logger.warning(f"[tcb] phase state unavailable for BREAKING gate "
                       f"(fail-safe → base gate): {e}")
        return False


def run_tape_circuit_breaker(df: Optional[pd.DataFrame] = None,
                             today_iso: Optional[str] = None,
                             sp_close: Optional[pd.Series] = None,
                             phase_late: Optional[bool] = None) -> Optional[dict]:
    """Orchestrator: fetch → compute → apply hysteresis → persist → verdict.

    Never raises. Returns None only when no usable price data exists at all
    (in which case the previous persisted verdict, if any, is returned with
    data_status='stale' so the email never silently loses the breaker).
    """
    try:
        today = today_iso or date.today().isoformat()
        state = _read_state()
        if df is None:
            # v3.43.0 — period fallback ladder + loud row-count logging. A
            # silently short frame disables the shock/trend blocks; the
            # Jul-31 L1-at--52% incident was undiagnosable from the log
            # because neither frame length nor not-fired reasons were
            # recorded.
            for _period in ("2y", "1y", "6mo"):
                df = fetch_tcb_ohlcv(period=_period)
                if df is not None and len(df) >= 200:
                    break
            if df is not None:
                logger.info(f"[tcb] OHLCV frame: {len(df)} rows "
                            f"({df.index[0].date()} → {df.index[-1].date()})")
        if df is None:
            prev = state.get("last_verdict")
            if prev:
                prev = dict(prev)
                prev["data_status"] = "stale"
                logger.warning("[tcb] no fresh data — serving persisted verdict as STALE")
                return prev
            logger.warning("[tcb] no data and no persisted verdict — breaker offline this run")
            return None

        # v3.46.0 — benchmark + phase inputs for BREAKING (both safe-fail
        # to inert: no benchmark → no_benchmark status, gate stays −30%).
        if sp_close is None:
            sp_close = fetch_benchmark_closes()
        if phase_late is None:
            phase_late = _load_phase_late()

        comp = compute_tcb_components(df, sp_close=sp_close,
                                      phase_late=bool(phase_late))
        computed_level = compute_tcb_level(comp)
        prev_level = int(state.get("level", 0))

        # Ratchet up immediately; decay only through the hysteresis gate.
        if computed_level >= prev_level:
            level = computed_level
        else:
            de = comp["_deescalation"]
            if de.get("closes_above_ma50_streak_ok") and de.get("dd_recovered"):
                level = computed_level
                logger.info(f"[tcb] de-escalation gate passed: L{prev_level} → L{level}")
            else:
                level = prev_level

        level_since = state.get("level_since")
        if level != prev_level or not level_since:
            level_since = today

        dd = comp["drawdown"]
        brk = comp["breaking"]
        # v3.46.0 — cascade-state logging: the warning and the promotion are
        # the two lines Raul reads first.
        if brk.get("active"):
            logger.warning(
                f"[tcb] BREAKING ACTIVE — {brk['idio_count']} idio shocks/"
                f"{BRK_CLUSTER_WIN} sessions, phase-late confirmed → stop-out "
                f"gate promoted to {brk['gate_pct']:.0f}%"
            )
        elif brk.get("latched") and not brk.get("phase_late"):
            logger.warning(
                "[tcb] BREAKING latched but phase-gate closed (framework not "
                "Phase 4/5) — gate stays at base −30%"
            )
        elif brk.get("warning"):
            logger.warning(
                f"[tcb] CASCADE FORMING — {BRK_WARN_N} idio shocks in "
                f"{BRK_CLUSTER_WIN} sessions; one more arms the −20% gate"
            )
        fired_names = [k for k in ("shock", "drawdown", "distribution",
                                   "trend_break") if comp[k].get("fired")]
        verdict = {
            "level": level,
            "action": ACTION_LABELS[level],
            "level_since": level_since,
            "post_peak": level >= 2,
            "components_fired": fired_names,
            "components": {k: comp[k] for k in
                           ("shock", "drawdown", "distribution",
                            "trend_break", "failed_good_news")},
            "peak_price": dd["peak_price"],
            "peak_date": dd["peak_date"],
            "drawdown_pct": dd["drawdown_pct"],
            "last_close": dd["last_close"],
            "last_date": dd["last_date"],
            "data_status": "fresh",
            "deescalation": comp["_deescalation"],
            "breaking": brk,
            "stopout_gate_pct": dd.get("stopout_gate_pct"),
        }
        _write_state({"level": level, "level_since": level_since,
                      "updated": today, "last_verdict": verdict})
        sh, tr, di = comp["shock"], comp["trend_break"], comp["distribution"]
        logger.warning(
            f"[tcb] LEVEL {level} ({ACTION_LABELS[level]}) | "
            f"fired={fired_names} | peak={dd['peak_price']:.0f} on {dd['peak_date']} | "
            f"drawdown={dd['drawdown_pct']:.1f}% | close={dd['last_close']:.0f} ({dd['last_date']})"
        )
        # v3.43.0 — full component-input logging: every NOT-fired component
        # states why, so the next anomaly is diagnosable from the log alone.
        logger.info(
            f"[tcb] components: shock(fired={sh['fired']}, thr={sh.get('threshold_pct')}%, "
            f"worst={sh.get('worst_ret_pct')}% on {sh.get('worst_day')}) | "
            f"trend(fired={tr.get('fired')}, status={tr.get('status')}, "
            f"close={tr.get('close')}, ma50={tr.get('ma50')}, ma20={tr.get('ma20')}, "
            f"falling={tr.get('ma50_falling')}, reason={tr.get('reason')}) | "
            f"distribution(fired={di.get('fired')}, status={di.get('status')}, "
            f"mode={di.get('mode')}, days={di.get('days')}/{di.get('needed')}, "
            f"udvr={di.get('udvr')}) | "
            f"stopout_depth={dd.get('stopout_depth')} "
            f"(gate {dd.get('stopout_gate_pct')}%, base_hit="
            f"{dd.get('stopout_depth_base')}) | "
            f"breaking(status={brk.get('status')}, idio={brk.get('idio_count')}, "
            f"latched={brk.get('latched')}, phase_late={brk.get('phase_late')}, "
            f"active={brk.get('active')})"
        )
        return verdict
    except Exception as e:
        logger.warning(f"[tcb] failed (non-fatal): {e}")
        return None
