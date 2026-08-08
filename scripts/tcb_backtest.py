"""v3.42.0 — TCB calibration replay; v3.46.0 — BASELINE vs MERGED head-to-head.
Run via GitHub Actions (network needed): replays the Tape Circuit Breaker over
prior memory-cycle tops and prints level-transition dates. v3.46.0 adds a
second replay per window with BREAKING + Distribution v2 live (benchmark
sliced as-of each day — no look-ahead; phase_late forced True to expose the
gate's full effect) so the two exit paths print side by side. The merge
acceptance rule: MERGED may only differ from BASELINE by exiting EARLIER on
the 2026 idiosyncratic cascade; every other window must be unchanged.

Usage: python scripts/tcb_backtest.py [SYMBOL] (default 000660.KS)
"""
import sys
sys.path.insert(0, ".")
import pandas as pd
import yfinance as yf
from src.analysis.tape_circuit_breaker import (
    compute_tcb_components, compute_tcb_level)

WINDOWS = [
    ("2018 top", "2017-06-01", "2019-03-01"),
    ("2021-22 top", "2020-09-01", "2022-12-31"),
    ("2024 top", "2023-12-01", "2025-03-01"),
    ("2026 top", "2025-10-01", None),
]

def _fetch(symbol: str, cols) -> pd.DataFrame:
    df = yf.download(symbol, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: c.lower() for c in df.columns})
    keep = [c for c in cols if c in df.columns]
    return df[keep].dropna(subset=["close"])


def _replay_leg(w: pd.DataFrame, sp: "pd.Series | None", phase_late: bool,
                tag: str) -> None:
    prev = 0
    brk_seen = False
    for i in range(130, len(w)):
        sub = w.iloc[: i + 1]
        asof = sub.index[-1]
        sp_sub = sp.loc[:asof] if sp is not None else None
        comp = compute_tcb_components(sub, sp_close=sp_sub,
                                      phase_late=phase_late)
        brk = comp.get("breaking", {})
        if brk.get("active") and not brk_seen:
            print(f"  [{tag}] {asof.date()}  BREAKING armed "
                  f"(idio={brk.get('idio_count')}) — gate → "
                  f"{brk.get('gate_pct'):.0f}%  dd={comp['drawdown']['drawdown_pct']}%")
            brk_seen = True
        lvl = compute_tcb_level(comp)
        lvl = max(lvl, prev) if lvl < prev else lvl  # ratchet (no de-esc in replay)
        if lvl > prev:
            d = comp["drawdown"]
            fired = [k for k in ("shock", "drawdown", "distribution",
                                 "trend_break") if comp[k].get("fired")]
            extra = (f"  dist_mode={comp['distribution'].get('mode')}"
                     if lvl >= 2 else "")
            print(f"  [{tag}] {asof.date()}  L{prev}→L{lvl}  "
                  f"dd={d['drawdown_pct']}%  gate={d.get('stopout_gate_pct')}%"
                  f"  fired={fired}{extra}")
            prev = lvl
    if prev == 0:
        print(f"  [{tag}] no levels fired in window")


def replay(symbol: str):
    print(f"=== TCB backtest: {symbol} ===")
    df = _fetch(symbol, ("close", "volume", "high", "low"))
    sp = _fetch("^GSPC", ("close",))["close"]
    for label, start, end in WINDOWS:
        w = df.loc[start:end] if end else df.loc[start:]
        if len(w) < 150:
            print(f"{label}: insufficient data ({len(w)} rows)")
            continue
        print(f"\n--- {label} ({w.index[0].date()} → {w.index[-1].date()}) ---")
        _replay_leg(w, None, False, "BASELINE")
        _replay_leg(w, sp, True, "MERGED  ")


if __name__ == "__main__":
    replay(sys.argv[1] if len(sys.argv) > 1 else "000660.KS")
