"""v3.45.0 — guidance ledger scoring (count-only bands), LEDGER_UPDATE
parsing, taxonomy epoch, transcript sentinel/window, evidence dedup."""
import os, sys
import pytest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.analysis.capex_ledger import (
    COMPANIES, apply_ledger_updates, company_stance, evaluate_plateau, load_ledger)


class TestStanceMath:
    def test_raise_stable_cut_bands(self):
        assert company_stance({"mid": 220, "prior_mid": 200}) == ("accelerating", -1)
        assert company_stance({"mid": 137.5, "prior_mid": 135}) == ("stable", 1)
        assert company_stance({"mid": 175, "prior_mid": 190}) == ("cutting", 2)
        assert company_stance({}) == ("awaiting print", 0)
        assert company_stance({"stance_override": "accelerating"}) == ("accelerating", -1)

    def test_exact_band_edges(self):
        assert company_stance({"mid": 105.0, "prior_mid": 100})[0] == "stable"
        assert company_stance({"mid": 94.9, "prior_mid": 100})[0] == "cutting"


class TestCountOnlyBands:
    def _led(self, stances):
        led = {}
        vals = {"accelerating": (220, 200), "stable": (100, 100), "cutting": (80, 100)}
        for c, st in zip(COMPANIES, stances):
            m, p = vals[st]
            led[c] = {"mid": m, "prior_mid": p, "period": "X", "event_date": "2026-08-07"}
        return led

    def test_seed_state_matches_approved_worked_example(self):
        # v3.46.1 — updated to Raul's MSFT basis-change ruling (2026-08-08):
        # $190B->$175B is a finance->operating lease reclassification, ruled
        # stable + flag (migration 002 applies on load). N stays 2; S: +1 -> 0
        # (MSFT +2 cutting -> +1 stable). Old expectation superseded.
        v = evaluate_plateau(load_ledger(path="/nonexistent/forces/seed.json"))
        assert v["count_at_or_beyond"] == 2 and v["score"] == 0
        assert v["status"] == "Partially fired"
        assert any("basis change" in l for l in v["company_lines"])
        assert any("withheld" in f for f in v["watch_flags"])
        assert len(v["company_lines"]) == 4

    def test_two_cutters_two_raisers_stays_partial(self):
        v = evaluate_plateau(self._led(["cutting", "cutting", "accelerating", "accelerating"]))
        assert v["count_at_or_beyond"] == 2 and v["score"] == 2
        assert v["status"] == "Partially fired"

    def test_three_at_or_beyond_fires(self):
        v = evaluate_plateau(self._led(["stable", "stable", "cutting", "accelerating"]))
        assert v["count_at_or_beyond"] == 3 and v["status"] == "Fired"

    def test_zero_not_fired(self):
        v = evaluate_plateau(self._led(["accelerating"] * 4))
        assert v["status"] == "Not fired" and v["score"] == -4


class TestLedgerUpdates:
    def test_valid_update_supersedes_and_sets_prior(self):
        led = {"Microsoft": {"mid": 190.0, "period": "FY27"}}
        n = apply_ledger_updates(
            ["blah LEDGER_UPDATE: Microsoft | FY27 | 175 | 2026-08-07 | Q4 call"],
            led, today="2026-08-08")
        assert n == 1 and led["Microsoft"]["mid"] == 175.0
        assert led["Microsoft"]["prior_mid"] == 190.0

    def test_range_midpoint_and_reject_future(self):
        led = {}
        apply_ledger_updates(
            ["LEDGER_UPDATE: Meta | CY2026 | 130-145 | 2026-08-07 | call"],
            led, today="2026-08-08")
        assert led["Meta"]["mid"] == 137.5
        n = apply_ledger_updates(
            ["LEDGER_UPDATE: Amazon | CY2026 | 240 | 2027-01-01 | future"],
            led, today="2026-08-08")
        assert n == 0 and "Amazon" not in led

    def test_malformed_ignored(self):
        led = {}
        assert apply_ledger_updates(["LEDGER_UPDATE: Tesla | X | 10 | 2026-08-07 | n"], led) == 0


class TestWiringAndSpecs:
    def test_verdict_layer_exposes_plateau(self):
        from src.analysis.computed_verdicts import evaluate_capex_plateau_verdict
        v = evaluate_capex_plateau_verdict()
        assert v.get("t3_capex_plateau", {}).get("mode") == "two_sided"
        assert "ledger" in v["t3_capex_plateau"]["basis"]

    def test_fast_tier_merges_plateau_at_both_legs(self):
        src = open("src/fast_tier.py").read()
        assert src.count("evaluate_capex_plateau_verdict()") >= 2
        assert "apply_ledger_updates" in src

    def test_spec_contains_contract_and_ruling(self):
        from src.config import INDICATORS
        wl = {i.id: i for i in INDICATORS}["t3_capex_plateau"].what_to_look_for
        assert "LEDGER_UPDATE:" in wl and "STATUS IS DECIDED BY COUNT ALONE" in wl
        assert "CALL date not the coverage date" in wl
        assert "never omitted" in wl

    def test_yahoo_window_widened_and_taxonomy_epoch_present(self):
        src = open("src/data/transcript_sources.py").read()
        assert "YAHOO_EARNINGS_WINDOW_DAYS = 7" in src
        assert "digest_taxonomy.json" in src and "transcript-sentinel" in src

    def test_transition_count_hard_rule_in_prompt(self):
        src = open("src/analysis/llm_summarizer.py").read()
        assert "HARD RULE — transition count" in src
        assert "t3_capex_plateau (guidance ledger — deterministic)" in src

    def test_evidence_dedup_guard_present(self):
        src = open("src/state.py").read()
        assert "[evidence-dedup]" in src and "0.6" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
