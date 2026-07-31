"""v3.43.0 — TCB level-ladder fixes, S&P-only relative strength with sticky
release, and the t3_capex_plateau peak-confirmation indicator.

Covers the Jul-31 postmortem: the breaker read L1 at -52% (min/max shock
inversion + L3 gated behind L2's component count), the SOX benchmark flapped
(fired Jul-6, un-fired mid-crash, re-fired Jul-31), and a CapEx plateau
rendered as a green-ish "downgrade".
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analysis import tape_circuit_breaker as T
from src.data.prices import (
    evaluate_relative_strength_signal,
    get_relative_strength_vs_benchmark,
)
from src.email_module import template as TPL


def _mk_tcb(closes, vols=None, start="2025-08-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame({"close": closes}, index=idx)
    df["volume"] = vols if vols is not None else 1_000_000
    return df


class TestTCBLevelLadderFixes:
    def test_shock_threshold_uses_easier_of_abs_and_sigma(self):
        # Fat-vol regime: sigma ~3.6% -> -3σ ≈ -10.8%. A -10.3% day MUST
        # fire on the -8% absolute leg. The shipped min() required -10.8%.
        rng = np.random.default_rng(11)
        base = list(2_900_000 * np.exp(np.cumsum(rng.normal(0, 0.036, 200))))
        path = base + [base[-1] * 0.897]  # a single -10.3% day
        comp = T.compute_tcb_components(_mk_tcb(path))
        assert comp["shock"]["fired"] is True
        assert comp["shock"]["threshold_pct"] == -8.0

    def test_jul31_production_shape_now_reads_l3(self):
        # Drawdown -52% + distribution fired, shock window rolled past the
        # crash day, trend dark. Stop-out depth must carry to L3 alone.
        comp = {
            "shock": {"fired": False},
            "drawdown": {"fired": True, "stopout_depth": True,
                          "drawdown_pct": -52.0},
            "distribution": {"fired": True},
            "trend_break": {"fired": False, "status": "no_data"},
            "failed_good_news": {"fired": False},
        }
        assert T.compute_tcb_level(comp) == 3

    def test_stopout_alone_is_at_least_l2_and_l3(self):
        comp = {
            "shock": {"fired": False},
            "drawdown": {"fired": True, "stopout_depth": True},
            "distribution": {"fired": False, "status": "no_data"},
            "trend_break": {"fired": False, "status": "data_ok"},
            "failed_good_news": {"fired": False},
        }
        assert T.compute_tcb_level(comp) == 3

    def test_minus_20_without_stopout_still_needs_committee(self):
        comp = {
            "shock": {"fired": False},
            "drawdown": {"fired": True, "stopout_depth": False},
            "distribution": {"fired": False},
            "trend_break": {"fired": False},
            "failed_good_news": {"fired": False},
        }
        assert T.compute_tcb_level(comp) == 1  # -20% alone is alert, not de-risk

    def test_short_frame_marks_trend_no_data_with_reason(self):
        comp = T.compute_tcb_components(_mk_tcb([1_000_000] * 60))
        assert comp["trend_break"]["status"] == "no_data"
        assert "insufficient history" in comp["trend_break"]["reason"]

    def test_end_to_end_deep_drawdown_lands_l3(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up = list(np.linspace(1_500_000, 2_919_000, 300))
        down = list(np.linspace(2_850_000, 1_401_000, 22))
        v = T.run_tape_circuit_breaker(df=_mk_tcb(up + down), today_iso="x")
        assert v["level"] == 3 and v["post_peak"] is True


def _mk_rs(skhx_path, spx_path, mu_path=None, periods=None):
    n = periods or len(skhx_path)
    idx = pd.bdate_range("2026-05-01", periods=n)
    df = pd.DataFrame(index=idx)
    df["SK Hynix"] = skhx_path
    df["S&P 500"] = spx_path
    df["Micron"] = mu_path if mu_path is not None else spx_path
    return df


class TestRelativeStrengthRebuild:
    def test_benchmark_helper_calendar_alignment_preserved(self):
        # KRX-only tail day: S&P NaN on the last row; the aligned projection
        # must still produce a number (the v3.4.6.2 asset, generalized).
        idx = pd.bdate_range("2026-06-01", periods=30)
        df = pd.DataFrame(index=idx)
        df["SK Hynix"] = np.linspace(100, 90, 30)
        spx = list(np.linspace(100.0, 101.0, 30))
        spx[-1] = np.nan
        df["S&P 500"] = spx
        v = get_relative_strength_vs_benchmark("SK Hynix", df, days=5)
        assert v is not None and v < 0

    def test_one_bad_5d_window_does_not_fire_without_20d_confirm(self, tmp_path):
        # Run-up then a 5d crash: 5d spread ~-15pp but 20d ~-2pp — the
        # single-window burst must NOT fire without trend confirmation.
        skhx = [100.0] * 20 + list(np.linspace(100, 115, 15)) + [112, 108, 104, 100, 98]
        out = evaluate_relative_strength_signal(
            _mk_rs(skhx, [100.0] * 40), state_path=str(tmp_path / "rs.json"))
        assert out["overall_severity"] != "red"

    def test_both_horizons_breaching_fires_red(self, tmp_path):
        # 20 flat, grind to 92, then -13% in the last 5: 5d ~-13pp, 20d ~-20pp.
        skhx = [100.0] * 20 + list(np.linspace(100, 92, 15)) + [89, 86, 84, 82, 80]
        out = evaluate_relative_strength_signal(
            _mk_rs(skhx, [100.0] * 40), state_path=str(tmp_path / "rs.json"))
        assert out["overall_severity"] == "red" and out["overall_fired"]
        assert out["skhx_5d_vs_spx_pp"] < -10 and out["skhx_20d_vs_spx_pp"] < -10

    def test_sticky_release_holds_red_until_five_recovery_sessions(self, tmp_path):
        sp = str(tmp_path / "rs.json")
        skhx = [100.0] * 20 + list(np.linspace(100, 92, 15)) + [89, 86, 84, 82, 80]
        evaluate_relative_strength_signal(_mk_rs(skhx, [100.0] * 40), state_path=sp)
        recovered = _mk_rs([80.0] * 40, [80.0] * 40)  # spreads ~0 now
        for k in range(4):
            out = evaluate_relative_strength_signal(recovered, state_path=sp)
            assert out["overall_severity"] == "red", f"released early at {k+1}"
            assert out["latch"]["recovery_streak"] == k + 1
        out = evaluate_relative_strength_signal(recovered, state_path=sp)
        assert out["overall_severity"] is None
        assert out["latch"]["latched"] is None

    def test_relapse_resets_recovery_streak(self, tmp_path):
        sp = str(tmp_path / "rs.json")
        skhx = [100.0] * 20 + list(np.linspace(100, 92, 15)) + [89, 86, 84, 82, 80]
        evaluate_relative_strength_signal(_mk_rs(skhx, [100.0] * 40), state_path=sp)
        recovered = _mk_rs([80.0] * 40, [80.0] * 40)
        evaluate_relative_strength_signal(recovered, state_path=sp)
        relapse = _mk_rs([80] * 35 + [80, 78, 76, 75, 74], [80.0] * 40)  # 5d < -5 again
        out = evaluate_relative_strength_signal(relapse, state_path=sp)
        assert out["latch"]["recovery_streak"] == 0
        assert out["overall_severity"] == "red"

    def test_sox_fully_removed_from_verdict_and_module(self):
        out = evaluate_relative_strength_signal(
            _mk_rs([100.0] * 40, [100.0] * 40), state_path="/dev/null")
        assert not any("sox" in k.lower() for k in out)
        assert "dram_etf_vs_smh" not in out
        src = open("src/data/prices.py").read()
        assert "get_relative_strength_vs_sox" not in src.replace(
            "retired get_relative_strength_vs_sox", "")
        assert "get_dram_etf_vs_smh" not in src
        cfg = open("src/config.py").read()
        assert "Pure-plays vs S&P 500" in cfg

    def test_banner_and_card_use_spx_keys(self):
        src = open("src/email_module/template.py").read()
        assert "skhx_5d_vs_spx_pp" in src and "skhx_20d_vs_spx_pp" in src
        assert "skhx_5d_vs_sox_pp" not in src
        assert "DRAM ETF 5d vs SMH" not in src


class TestCapexPlateau:
    def test_indicator_registered_tier3(self):
        from src.config import INDICATORS
        ind = {i.id: i for i in INDICATORS}["t3_capex_plateau"]
        assert ind.tier == 3
        wl = ind.what_to_look_for
        assert "Microsoft" in wl and "$190B" in wl          # canonical example
        assert "NOT CapEx cutting" in wl                     # his exact framing
        assert "Tier 4" in wl                                # cut handoff
        assert "re-ACCELERATING" in wl                       # reversal = opposite only

    def test_t3_count_now_six(self):
        from src.config import INDICATORS
        assert sum(1 for i in INDICATORS if i.tier == 3) == 6

    def test_progression_banner_renders_neutral(self):
        h = TPL._build_progression_banner(
            {"indicator_id": "t2_capex_parabolic",
             "old_status": "Fired", "new_status": "Partially fired",
             "description": "Microsoft guides FY27 CapEx at $190B flat."},
            "Partially fired")
        assert "STAGE PROGRESSION" in h
        assert "not an improvement" in h
        assert "#5B4B8A" in h              # slate/purple, never green
        assert "✅" not in h and "SIGNAL DOWNGRADE" not in h

    def test_assembly_routes_capex_downgrade_to_progression(self):
        src = open("src/email_module/template.py").read()
        assert "_build_progression_banner(_trig, _plateau_status)" in src
        assert "t3_capex_plateau" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
