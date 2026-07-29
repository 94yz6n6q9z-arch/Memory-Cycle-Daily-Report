"""v3.42.0 — Tape Circuit Breaker + computed-verdict table + render truth.

Covers the Jul-29 postmortem set: the breaker state machine (synthetic
replay of the 2026 top shape, shakeout immunity, hysteresis, degradation,
stale fallback), the one-truth verdict table on both legs, the exec-pill
new_status key fix, the render-side jargon guard, the S&P rel-strength
OR-path, and TCB-forced post-peak framing in the executive summary.
"""

import json
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analysis import computed_verdicts as CV
from src.analysis import tape_circuit_breaker as T
from src.email_module import template as TPL


def _mk(closes, vols=None, start="2025-08-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame({"close": closes}, index=idx)
    df["volume"] = vols if vols is not None else 1_000_000
    return df


def _top_2026():
    rng = np.random.default_rng(7)
    up = list(np.linspace(1_200_000, 2_919_000, 200) * (1 + rng.normal(0, 0.004, 200)))
    up[-1] = 2_919_000
    crash = [2_850_000, 2_760_000, 2_700_000, 2_640_000, 2_790_000, 2_360_000,
             2_290_000, 2_180_000, 2_120_000, 2_010_000, 1_950_000, 1_880_000,
             1_820_000, 1_759_000, 1_720_000, 1_690_000, 1_640_000, 1_560_000,
             1_401_000]
    vols = [1_000_000] * 200 + [
        1_400_000 if i and crash[i] < crash[i - 1] else 900_000
        for i in range(len(crash))
    ]
    return up, crash, vols


class TestTapeCircuitBreaker:
    def test_2026_shape_escalates_l1_l2_l3_in_time(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, vols = _top_2026()
        levels = []
        for i in range(200, len(up) + len(crash) + 1):
            v = T.run_tape_circuit_breaker(df=_mk((up + crash)[:i], vols[:i]),
                                           today_iso=f"d{i}")
            levels.append((v["level"], v["drawdown_pct"]))
        assert levels[-1][0] == 3
        l2 = next(x for x in levels if x[0] >= 2)
        l3 = next(x for x in levels if x[0] >= 3)
        # The whole point: out around a quarter down, not -52%.
        assert l2[1] > -32, l2
        assert l3[1] > -45, l3

    def test_midcycle_shakeout_never_reaches_derisk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        base = list(np.linspace(1_000_000, 1_600_000, 250))
        shake = [1_560_000, 1_500_000, 1_460_000, 1_420_000, 1_400_000,
                 1_450_000, 1_500_000, 1_560_000, 1_600_000, 1_640_000]
        v = T.run_tape_circuit_breaker(
            df=_mk(base + shake, [1_000_000] * (len(base) + len(shake))),
            today_iso="x")
        assert v["level"] <= 1

    def test_hysteresis_holds_level_through_bounce(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, vols = _top_2026()
        T.run_tape_circuit_breaker(df=_mk(up + crash, vols), today_iso="a")
        bounce = up + crash + [1_480_000, 1_520_000, 1_560_000]
        v = T.run_tape_circuit_breaker(df=_mk(bounce, vols + [800_000] * 3),
                                       today_iso="b")
        assert v["level"] == 3  # 3 green days do NOT stand down a SELL state

    def test_deescalation_gate_eventually_releases(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, vols = _top_2026()
        T.run_tape_circuit_breaker(df=_mk(up + crash, vols), today_iso="a")
        # Long recovery: grind back inside -15% of peak with 40 closes above MA50.
        rec = list(np.linspace(1_401_000, 2_600_000, 60))
        v = T.run_tape_circuit_breaker(
            df=_mk(up + crash + rec, vols + [900_000] * 60), today_iso="b")
        assert v["level"] < 3

    def test_volume_missing_degrades_distribution_only(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, _ = _top_2026()
        df = _mk(up + crash, None); df["volume"] = pd.NA
        v = T.run_tape_circuit_breaker(df=df, today_iso="x")
        assert v["components"]["distribution"]["status"] == "no_data"
        assert v["level"] == 3

    def test_no_data_serves_persisted_verdict_as_stale(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, vols = _top_2026()
        T.run_tape_circuit_breaker(df=_mk(up + crash, vols), today_iso="a")
        import unittest.mock as um
        with um.patch.object(T, "fetch_tcb_ohlcv", return_value=None):
            v = T.run_tape_circuit_breaker(df=None, today_iso="b")
        assert v["data_status"] == "stale" and v["level"] == 3

    def test_state_file_persists_level(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, vols = _top_2026()
        T.run_tape_circuit_breaker(df=_mk(up + crash, vols), today_iso="a")
        st = json.loads((tmp_path / "state" / "tcb_state.json").read_text())
        assert st["level"] == 3 and st["level_since"]

    def test_first_run_on_history_lands_directly_at_current_level(self, tmp_path, monkeypatch):
        # Deploy-day property: the breaker needs no lived history — it
        # recomputes the level from the fetched window, so the FIRST run on
        # the July tape declares L3 immediately.
        monkeypatch.chdir(tmp_path); (tmp_path / "state").mkdir()
        up, crash, vols = _top_2026()
        v = T.run_tape_circuit_breaker(df=_mk(up + crash, vols), today_iso="first")
        assert v["level"] == 3 and v["post_peak"] is True


class TestComputedVerdictTable:
    class R:
        def __init__(self, sid, dump=None, status="Partially fired"):
            self.signal_id = sid
            self.raw_evidence_dump = dump or {}
            self.fired_status = status
            self.evidence_for_change_vs_yesterday = ""

    class A:
        def __init__(self, i, st):
            self.id, self.current_status, self.change_vs_prior = i, st, None

    def _jul29_reasonings(self, score=-18.0):
        return [self.R("t2_eps_revisions", {"signal_specific_data": {
            "weighted_scoring_verdict": {"per_ticker": {
                "000660.KS": {"net_30d": score},
                "005930.KS": {"net_30d": -14.5}}}}})]

    def test_jul29_shape_all_four_fired(self):
        v = CV.evaluate_all(
            {"forward_pe": {"000660.KS": 4.6, "005930.KS": 4.5},
             "relative_strength_full": {"overall_severity": "red",
                                        "fired_sub_triggers": [("red", "x")]},
             "mean_capex_ocf_ratio_2q": 0.53},
            self._jul29_reasonings())
        assert {v[k]["status"] for k in v} == {"Fired"} and len(v) == 4

    def test_assessment_leg_unifies_banner_and_card(self):
        v = CV.evaluate_all({}, self._jul29_reasonings())
        objs = [self.A("t2_eps_revisions", "Partially fired")]
        CV.apply_to_objects(objs, v, "current_status", "id", "change_vs_prior",
                            "assessment")
        assert objs[0].current_status == "Fired"
        assert "deterministically" in objs[0].change_vs_prior
        assert "30d weighted revision score -18.0" in objs[0].change_vs_prior

    def test_up_only_never_forces_down(self):
        v = CV.evaluate_all({}, self._jul29_reasonings(score=-8.0))
        assert "t2_eps_revisions" not in v  # cadence partials stay with Layer 2

    def test_two_sided_relaxes_rel_strength(self):
        v = CV.evaluate_all(
            {"relative_strength_full": {"overall_severity": None,
                                        "fired_sub_triggers": []}}, [])
        assert v["t3_relative_strength"]["status"] == "Not fired"

    def test_capex_partial_band(self):
        v = CV.evaluate_all({"mean_capex_ocf_ratio_2q": 0.44}, [])
        assert v["t2_hyperscaler_capex_revenue"]["status"] == "Partially fired"

    def test_pe_no_data_returns_none_leaving_l2_rule(self):
        assert CV.evaluate_pe({"forward_pe": {}}) is None

    def test_pre_store_leg_applies_to_reasonings(self):
        r = self._jul29_reasonings()[0]
        CV.apply_to_objects([r], CV.evaluate_all({}, [r]), "fired_status",
                            "signal_id", "evidence_for_change_vs_yesterday",
                            "pre-store")
        assert r.fired_status == "Fired"

    def test_pipeline_wires_both_legs_in_order(self):
        src = open("src/fast_tier.py").read()
        i_pre = src.index('stage="pre-store"')
        i_store = src.index("update_evidence_after_layer2(")
        i_asmt = src.index('stage="assessment"')
        i_log = src.index("update_trigger_log_24h(")
        assert i_pre < i_store and i_asmt < i_log


class TestRenderTruth:
    def test_exec_pill_uses_new_status_key_over_description(self):
        # The Jul-29 bug: log entries carry new_status; the pill only knew
        # to_status, fell to description regex, and colored a re-FIRE green
        # off the historical phrase inside the enriched evidence.
        src = open("src/email_module/template.py").read()
        assert "trig.get('to_status') or trig.get('new_status')" in src

    def test_render_jargon_guard_suppresses_leaks(self):
        assert TPL._clean_render_desc(
            "fired_status_evidence='supports_not_fired' deep-tier") == ""
        assert TPL._clean_render_desc("Layer 2 transition to Fired") == ""
        assert "Mirae" in TPL._clean_render_desc(
            "Mirae cut SK Hynix target 33%; BNK to W1.48M")

    def test_tcb_banner_l3_contract(self):
        h = TPL._build_tcb_banner({
            "level": 3, "action": "SELL — thesis stopped out",
            "level_since": "2026-07-20", "post_peak": True,
            "drawdown_pct": -52.0, "peak_price": 2919000,
            "peak_date": "2026-06-26", "last_close": 1401000,
            "last_date": "2026-07-29", "data_status": "fresh",
            "components_fired": ["shock", "drawdown", "trend_break"],
            "components": {
                "shock": {"fired": True, "worst_ret_pct": -15.37,
                          "worst_day": "2026-07-13"},
                "drawdown": {"fired": True, "drawdown_pct": -52.0},
                "distribution": {"fired": False, "status": "no_data"},
                "trend_break": {"fired": True},
                "failed_good_news": {"fired": False}}})
        assert "LEVEL 3" in h and "SELL" in h
        assert "Post-peak confirmed" in h and "2,919,000" in h
        assert "color:#fff" in h and "🛑" in h

    def test_tcb_banner_stale_flagged(self):
        h = TPL._build_tcb_banner({"level": 2, "action": "DE-RISK — peak likely behind us",
                                   "level_since": "x", "data_status": "stale",
                                   "components": {}, "components_fired": []})
        assert "STALE DATA" in h

    def test_sp500_or_path_present_in_evaluator(self):
        src = open("src/data/prices.py").read()
        assert "vs S&P 500" in src and "skhx_5d_vs_spx_pp" in src


class TestPostPeakFraming:
    def _es(self):
        from src.analysis.llm_summarizer import ExecutiveSummary
        import inspect
        # Build with whatever the real dataclass requires, minimally.
        kwargs = {}
        for name, p in inspect.signature(ExecutiveSummary).parameters.items():
            if p.default is not inspect.Parameter.empty:
                continue
            ann = str(p.annotation)
            if "ScenarioCase" in ann or "Case" in ann:
                from src.analysis.llm_summarizer import CaseScenario as ScenarioCase
                kwargs[name] = ScenarioCase(
                    probability_pct=46, peak_months_away=3,
                    peak_window_start="September 2026",
                    peak_window_end="October 2026", return_to_peak_pct=10.0)
            elif "int" in ann:
                kwargs[name] = 4
            elif "list" in ann:
                kwargs[name] = []
            else:
                kwargs[name] = "Boom / Euphoria (late stage)" if "label" in name else "x"
        return ExecutiveSummary(**kwargs)

    def test_tcb_l3_forces_trough_anchor_and_tape_line(self):
        es = self._es()
        es.current_phase = 4
        out = TPL._build_executive_summary(es, tcb_verdict={
            "level": 3, "post_peak": True, "peak_price": 2919000,
            "peak_date": "2026-06-26", "drawdown_pct": -52.0})
        assert "TAPE STATE: POST-PEAK" in out
        assert "trough is" in out or "trough" in out
        assert "the peak is" not in out  # no peak-ahead sentence survives

    def test_without_tcb_phase4_stays_peak_framed(self):
        es = self._es()
        es.current_phase = 4
        out = TPL._build_executive_summary(es, tcb_verdict=None)
        assert "TAPE STATE" not in out and "peak" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
