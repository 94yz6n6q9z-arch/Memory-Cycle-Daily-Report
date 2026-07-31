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
        out = out.dropna(subset=["close"])
        return out if len(out) >= 60 else None
    except Exception as e:
        logger.warning(f"[tcb] OHLCV fetch failed (non-fatal): {e}")
        return None


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


def compute_tcb_components(df: pd.DataFrame) -> dict:
    """Pure component evaluation from an OHLCV frame (close, volume)."""
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
    comp["drawdown"] = {
        "fired": dd <= DD_FIRE,
        "watch": dd <= DD_WATCH,
        "stopout_depth": dd <= DD_STOPOUT,
        "drawdown_pct": round(dd * 100, 2),
        "peak_price": peak_val,
        "peak_date": peak_day,
        "last_close": last,
        "last_date": str(pd.Timestamp(close.index[-1]).date()),
    }

    # --- distribution (volume) ---------------------------------------------
    dist = {"fired": False, "status": "no_data", "days": None, "needed": DIST_MIN_DAYS}
    vol = df["volume"] if "volume" in df.columns else None
    if vol is not None and pd.to_numeric(vol, errors="coerce").notna().sum() >= 60:
        volf = pd.to_numeric(vol, errors="coerce")
        med50 = volf.rolling(50).median()
        win_ret = rets.iloc[-DIST_WINDOW:]
        win_vol = volf.iloc[-DIST_WINDOW:]
        win_med = med50.iloc[-DIST_WINDOW:]
        mask = (win_ret <= DIST_RET) & (win_vol >= DIST_VOL_MULT * win_med)
        n = int(mask.fillna(False).sum())
        dist.update({"status": "data_ok", "days": n, "fired": n >= DIST_MIN_DAYS})
    comp["distribution"] = dist

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


def run_tape_circuit_breaker(df: Optional[pd.DataFrame] = None,
                             today_iso: Optional[str] = None) -> Optional[dict]:
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

        comp = compute_tcb_components(df)
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
            f"days={di.get('days')}/{di.get('needed')}) | "
            f"stopout_depth={dd.get('stopout_depth')}"
        )
        return verdict
    except Exception as e:
        logger.warning(f"[tcb] failed (non-fatal): {e}")
        return None
