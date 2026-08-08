"""v3.46.0 — BREAKING (idio shock-cluster gate promotion) + Distribution v2.

Executable spec. Every named counterexample from the 2026-08-08 Colab
calibration (five iterations, seven windows, 000660.KS + MU) is a fixture:
Iran must NOT promote, the July cascade must, early-June/Aug-24/COVID stay
inert, the −30% base stop is untouchable, the phase gate is a hard second
key, and Distribution v2 fires only on mature unabsorbed selling and
releases on watermark reclaim. Fixtures are deterministic-by-construction:
noisy (σ alive — constant-return segments make max(−8%, −3σ) degenerate)
but sign-constrained (no accidental shocks or excusals).
"""
import sys
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
import pytest

from src.analysis.tape_circuit_breaker import (
    compute_tcb_components, compute_tcb_level, compute_breaking,
    compute_distribution_v2, daily_shock_flags,
    DD_STOPOUT, DD_STOPOUT_BREAKING, BRK_CLUSTER_N, BRK_LATCH,
)

IDX = lambda n, start="2024-01-01": pd.date_range(start, periods=n, freq="B")


def series_from_returns(rets, start=100.0, index=None):
    px = [start]
    for r in rets:
        px.append(px[-1] * (1 + r))
    return pd.Series(px[1:], index=index if index is not None else IDX(len(rets)))


def crash_fixture(idio_shocks, macro_on_days=(), n_after_peak=30, seed=11):
    """Stock: all-positive noisy run-up, −9% shock days, −|N(0.4%,0.15%)|
    drift decline. Benchmark: strictly positive noise EXCEPT explicit
    macro_on_days (−2%) — accidental excusals impossible by construction.
    n_after_peak trims the series before the σ-window decay zone."""
    rng = np.random.RandomState(seed)
    peak_i = 260
    n = peak_i + n_after_peak
    idx = IDX(n)
    shot = set(peak_i + d for d in idio_shocks)
    rets = []
    for i in range(n):
        if i < peak_i:
            rets.append(abs(rng.normal(0.004, 0.008)))
        elif i in shot:
            rets.append(-0.09)
        else:
            rets.append(-abs(rng.normal(0.004, 0.0015)))
    sp_rets = np.abs(rng.normal(0.0010, 0.0030, n))
    for d in macro_on_days:
        sp_rets[peak_i + d] = -0.02
    s = series_from_returns(rets, 100.0, idx)
    sp = series_from_returns(list(sp_rets), 5000.0, idx)
    return s, sp, idx, peak_i


def as_of(series, i):
    return series.iloc[: i + 1]


# ------------------------------ shocks -----------------------------------

def test_shock_fires_on_sigma_cap_in_low_vol():
    rng = np.random.RandomState(3)
    rets = list(np.abs(rng.normal(0.001, 0.004, 60))) + [-0.07]
    s = series_from_returns(rets)
    assert bool(daily_shock_flags(s).iloc[-1])


def test_shock_absolute_cap_governs_in_high_vol():
    rng = np.random.RandomState(4)
    rets = list(rng.normal(0, 0.04, 140)) + [-0.07]
    s = series_from_returns(rets)
    assert not bool(daily_shock_flags(s).iloc[-1])


# --------------------------- macro-excusal --------------------------------

def test_grind_drawdown_excuses_iran_mechanism():
    """Small index down-days accumulating to a multi-sigma drawdown must
    excuse even though no single day breaches the day-sigma cut."""
    n = 300
    idx = IDX(n)
    rng = np.random.RandomState(6)
    sp_rets = np.abs(rng.normal(0.0004, 0.006, n))
    sp_rets[-8:] = -0.009
    sp = series_from_returns(list(sp_rets), 5000.0, idx)
    stock_rets = [abs(rng.normal(0.002, 0.006)) for _ in range(n - 1)] + [-0.09]
    s = series_from_returns(stock_rets, 100.0, idx)
    brk = compute_breaking(s, sp, phase_late=True)
    assert brk["status"] == "data_ok"
    assert brk["recent_shocks"][-1]["class"] == "MACRO-excused"
    assert brk["idio_count"] == 0


def test_quiet_uptrend_index_never_excuses():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1], n_after_peak=4)
    brk = compute_breaking(as_of(s, pk + 1), as_of(sp, pk + 1), True)
    assert brk["recent_shocks"][-1]["class"] == "IDIO"


# ------------------------- BREAKING cluster -------------------------------

def test_july_shape_promotes_gate_and_l3_before_base_stop():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1, 4, 7], n_after_peak=26)
    i = pk + 7
    df = pd.DataFrame({"close": as_of(s, i)})
    comp = compute_tcb_components(df, sp_close=as_of(sp, i), phase_late=True)
    brk, dd = comp["breaking"], comp["drawdown"]
    assert brk["active"] and brk["idio_count"] >= BRK_CLUSTER_N
    assert dd["stopout_gate_pct"] == DD_STOPOUT_BREAKING * 100
    assert dd["stopout_depth"] and not dd["stopout_depth_base"]   # −20 hit, −30 not
    assert DD_STOPOUT < dd["drawdown_pct"] / 100 <= DD_STOPOUT_BREAKING
    assert compute_tcb_level(comp) == 3                            # stop-out path


def test_iran_shape_two_idio_one_excused_no_promotion():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1, 3], macro_on_days=[6],
                                   n_after_peak=24)
    s2 = s.copy()
    s2.iloc[pk + 6:] = s2.iloc[pk + 6:] * (1 - 0.09) / (1 - 0.004)
    i = pk + 8
    brk = compute_breaking(as_of(s2, i), as_of(sp, i), phase_late=True)
    classes = {r["date"]: r["class"] for r in brk["recent_shocks"]}
    assert list(classes.values()).count("IDIO") == 2
    assert list(classes.values()).count("MACRO-excused") == 1
    assert brk["idio_count"] == 2 and not brk["latched"]
    assert brk["gate_pct"] == DD_STOPOUT * 100                     # base −30


def test_cascade_forming_warning_at_two_in_ten():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1, 4], n_after_peak=10)
    brk = compute_breaking(as_of(s, pk + 5), as_of(sp, pk + 5), True)
    assert brk["warning"] and not brk["armed_now"]


def test_phase_gate_blocks_promotion():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1, 4, 7], n_after_peak=26)
    i = pk + 7
    df = pd.DataFrame({"close": as_of(s, i)})
    comp = compute_tcb_components(df, sp_close=as_of(sp, i), phase_late=False)
    brk, dd = comp["breaking"], comp["drawdown"]
    assert brk["latched"] and not brk["active"]
    assert dd["stopout_gate_pct"] == DD_STOPOUT * 100
    assert not dd["stopout_depth"]                                 # −25% vs −30 gate
    assert compute_tcb_level(comp) == 2                            # shock+dd only


def test_latch_decays_after_five_sessions_and_rearms():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1, 3, 5, 18, 20, 22],
                                   n_after_peak=24)
    latched_at = lambda i: compute_breaking(as_of(s, pk + i),
                                            as_of(sp, pk + i), True)["latched"]
    assert latched_at(14)          # armed last at pk+10 → latched through pk+14
    assert not latched_at(16)      # dark
    assert latched_at(22)          # second cluster re-arms


def test_base_stop_unconditional_regardless_of_breaking():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1, 4, 7], n_after_peak=26)
    i = pk + 25                    # deep in the decline, past −30%
    df = pd.DataFrame({"close": as_of(s, i)})
    comp = compute_tcb_components(df)                              # no benchmark at all
    assert comp["breaking"]["status"] == "no_benchmark"
    if comp["drawdown"]["drawdown_pct"] / 100 <= DD_STOPOUT:
        assert comp["drawdown"]["stopout_depth"]
        assert compute_tcb_level(comp) == 3


def test_backward_compatible_single_arg_call():
    s, sp, idx, pk = crash_fixture(idio_shocks=[1], n_after_peak=6)
    comp = compute_tcb_components(pd.DataFrame({"close": as_of(s, pk + 2)}))
    assert comp["breaking"]["status"] == "no_benchmark"
    assert not comp["breaking"]["active"]
    assert comp["drawdown"]["stopout_gate_pct"] == DD_STOPOUT * 100


# -------------------------- Distribution v2 -------------------------------

def dist_fixture(n=520, top_at=420, seed=5):
    rng = np.random.RandomState(seed)
    idx = IDX(n)
    px, vol = [50.0], []
    for i in range(n):
        r = rng.normal(0.0035, 0.012) if i < top_at else rng.normal(-0.011, 0.012)
        px.append(max(1.0, px[-1] * (1 + r)))
        base = 1e6 * rng.uniform(0.85, 1.25)
        vol.append(base * (1.7 if (r < 0 and i >= top_at)
                           else (1.5 if r > 0 and i < top_at else 0.6)))
    close = pd.Series(px[1:], index=idx)
    return pd.DataFrame({"close": close, "high": close * 1.02,
                         "low": close * 0.997,
                         "volume": pd.Series(vol, index=idx)}), top_at


def test_d2_quiet_on_runup_fires_after_mature_top():
    df, top = dist_fixture()
    pre = compute_distribution_v2(df.iloc[:top])
    assert pre["status"] == "data_ok" and not pre["fired"]
    post = compute_distribution_v2(df)
    assert post["status"] == "data_ok" and post["mode"] == "v2"
    assert post["fired"]


def test_d2_releases_when_watermark_reclaimed():
    df, top = dist_fixture(n=560, top_at=400)
    rec = df["close"].copy()
    tail = rec.index[480:]
    rec.loc[tail] = rec.iloc[479] * (1.02 ** np.arange(1, len(tail) + 1))
    df2 = pd.DataFrame({"close": rec, "high": rec * 1.02, "low": rec * 0.997,
                        "volume": df["volume"]})
    mid = compute_distribution_v2(df2.iloc[:470])
    assert mid["fired"]                                            # fired in decline
    end = compute_distribution_v2(df2)
    assert not end["fired"]                                        # absorbed → released


def test_d2_maturity_gate_blocks_fresh_breakout():
    rng = np.random.RandomState(9)
    idx = IDX(400)
    rets = list(rng.normal(-0.001, 0.01, 260)) + [0.02] * 50 + [-0.015] * 90
    close = series_from_returns(rets, 40, idx)
    volume = pd.Series(1e6 * (1.8 * (close.pct_change() < 0) + 0.6),
                       index=idx).fillna(1e6)
    df = pd.DataFrame({"close": close, "high": close * 1.02,
                       "low": close * 0.997, "volume": volume})
    out = compute_distribution_v2(df)
    assert not out["fired"]


def test_d2_fallback_ladder():
    df, top = dist_fixture()
    short = compute_distribution_v2(df.iloc[-300:])
    assert short["status"] == "v1_fallback_short_frame"
    assert short["mode"] == "v1_fallback"
    no_hilo = compute_distribution_v2(df[["close", "volume"]])
    assert no_hilo["status"] == "v1_fallback_no_hilo"
    no_vol = compute_distribution_v2(df[["close", "high", "low"]])
    assert no_vol["status"] == "no_data" and not no_vol["fired"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
