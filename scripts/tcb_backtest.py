"""v3.42.0 — TCB calibration replay. Run via GitHub Actions (network needed):
replays the Tape Circuit Breaker over prior memory-cycle tops and prints the
level-transition dates, so thresholds are tuned against history instead of
guessed. Windows: 2018 (MU + 000660.KS), 2021-22, 2024, and Jun-Jul 2026.

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

def replay(symbol: str):
    print(f"=== TCB backtest: {symbol} ===")
    df = yf.download(symbol, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: c.lower() for c in df.columns})[["close", "volume"]].dropna(subset=["close"])
    for label, start, end in WINDOWS:
        w = df.loc[start:end] if end else df.loc[start:]
        if len(w) < 150:
            print(f"{label}: insufficient data ({len(w)} rows)")
            continue
        prev = 0
        print(f"\n--- {label} ({w.index[0].date()} → {w.index[-1].date()}) ---")
        for i in range(130, len(w)):
            sub = w.iloc[: i + 1]
            comp = compute_tcb_components(sub)
            lvl = compute_tcb_level(comp)
            lvl = max(lvl, prev) if lvl < prev else lvl  # ratchet (no de-esc in replay)
            if lvl > prev:
                d = comp["drawdown"]
                print(f"{sub.index[-1].date()}  L{prev}→L{lvl}  "
                      f"dd={d['drawdown_pct']}%  fired={[k for k in ('shock','drawdown','distribution','trend_break') if comp[k].get('fired')]}")
                prev = lvl
        if prev == 0:
            print("no levels fired in window")

if __name__ == "__main__":
    replay(sys.argv[1] if len(sys.argv) > 1 else "000660.KS")
