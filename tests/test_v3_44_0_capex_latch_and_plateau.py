"""v3.44.0 — t2_capex_parabolic latched up-only + t3_capex_plateau
redefined around the guidance SURPRISE with the 1-2/3-4 ladder.

Covers the Jul-31 defects: (a) parabolic downgraded Fired -> Partially
fired on flat/withheld guidance ("once triggered it triggered"), (b)
Layer 2 argued past the canonical MSFT $190B-on-$190B example via the
'AT baseline, not flat' / growth-qualifier escapes.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analysis.computed_verdicts import (
    LATCHED_UP_ONLY_IDS,
    apply_status_ratchet,
)
from src.config import INDICATORS

IND = {i.id: i for i in INDICATORS}


def _reasoning(status):
    return SimpleNamespace(signal_id="t2_capex_parabolic", fired_status=status,
                           evidence_for_change_vs_yesterday=None)


class TestStatusRatchet:
    def test_downgrade_is_overwritten_back_to_prior(self):
        # The exact Jul-31 move: Layer 2 says Partially fired, prior was Fired.
        obj = _reasoning("Partially fired")
        apply_status_ratchet([obj], {"t2_capex_parabolic": "Fired"},
                             status_attr="fired_status", id_attr="signal_id",
                             note_attr="evidence_for_change_vs_yesterday",
                             stage="test")
        assert obj.fired_status == "Fired"
        assert "latch" in obj.evidence_for_change_vs_yesterday
        assert "plateau" in obj.evidence_for_change_vs_yesterday

    def test_rising_transition_passes_untouched(self):
        obj = _reasoning("Fired")
        apply_status_ratchet([obj], {"t2_capex_parabolic": "Partially fired"},
                             status_attr="fired_status", id_attr="signal_id",
                             note_attr="evidence_for_change_vs_yesterday",
                             stage="test")
        assert obj.fired_status == "Fired"
        assert obj.evidence_for_change_vs_yesterday is None  # no note on no-op

    def test_first_run_no_prior_is_noop(self):
        obj = _reasoning("Partially fired")
        apply_status_ratchet([obj], {}, status_attr="fired_status",
                             id_attr="signal_id", note_attr=None, stage="test")
        assert obj.fired_status == "Partially fired"

    def test_non_latched_signal_unaffected(self):
        obj = SimpleNamespace(signal_id="t3_relative_strength",
                              fired_status="Not fired")
        apply_status_ratchet([obj], {"t3_relative_strength": "Fired"},
                             status_attr="fired_status", id_attr="signal_id",
                             note_attr=None, stage="test")
        assert obj.fired_status == "Not fired"  # only latched ids ratchet

    def test_assessment_shape_also_ratchets(self):
        obj = SimpleNamespace(id="t2_capex_parabolic",
                              current_status="Not fired", change_vs_prior=None)
        apply_status_ratchet([obj], {"t2_capex_parabolic": "Fired"},
                             status_attr="current_status", id_attr="id",
                             note_attr="change_vs_prior", stage="test")
        assert obj.current_status == "Fired"

    def test_parabolic_is_registered_as_latched(self):
        assert "t2_capex_parabolic" in LATCHED_UP_ONLY_IDS


class TestFastTierWiring:
    def test_ratchet_applied_at_both_legs(self):
        src = open("src/fast_tier.py").read()
        assert src.count("apply_status_ratchet(") >= 2
        assert 'stage="pre-store"' in src and 'stage="assessment"' in src

    def test_module_level_binding_exists(self):
        import src.fast_tier as ft
        assert hasattr(ft, "apply_status_ratchet")


class TestParabolicLatchSpec:
    def test_spec_states_latch_and_forbids_the_jul31_move(self):
        wl = IND["t2_capex_parabolic"].what_to_look_for
        assert "STAYS Fired" in wl
        assert "FORBIDDEN COUNTEREXAMPLE (2026-07-31)" in wl
        assert "t3_capex_plateau" in wl
        assert "bear catalyst if flat/down" not in wl
        assert "LATCHED" in IND["t2_capex_parabolic"].description

    def test_flat_guidance_routes_to_plateau_in_numeric_spec(self):
        tn = IND["t2_capex_parabolic"].trigger_numeric
        v = tn["any_hyperscaler_2027_flat_or_down"]
        assert v.get("routes_to") == "t3_capex_plateau"
        assert "bear_catalyst" not in v


class TestPlateauRedefinition:
    def test_ladder_is_1_2_partial_3_4_fired(self):
        ind = IND["t3_capex_plateau"]
        wl = ind.what_to_look_for
        assert "1-2 = Partially fired" in wl and "3-4 = Fired" in wl
        tn = ind.trigger_numeric["hyperscalers_at_plateau_count"]
        assert tn == {"partial_at": 1, "trigger_at": 3}

    def test_surprise_definition_and_qualifier_hard_rule(self):
        wl = IND["t3_capex_plateau"].what_to_look_for
        assert "PRIOR FORWARD GUIDANCE" in wl.upper()
        assert "REITERATE/CONFIRM within ±5%" in wl
        assert "qualifiers cannot override numbers" in wl
        assert "2026-07-31" in wl  # forbidden escape named

    def test_withheld_guidance_is_watch_flag_not_count(self):
        wl = IND["t3_capex_plateau"].what_to_look_for
        assert "Withheld forward guidance" in wl
        assert "does NOT count toward the plateau tally" in wl
        assert "watch flag" in wl

    def test_worked_example_matches_current_spec(self):
        from src.config import INDICATORS
        wl = {x.id: x for x in INDICATORS}["t3_capex_plateau"].what_to_look_for
        # v3.46.1 — MSFT lease-reclass ruling (2026-08-08): worked example
        # now shows Microsoft stable +1 (basis change), S=0.
        assert "Microsoft stable +1" in wl
        assert "lease reclassification" in wl
        assert "$220B" in wl
        assert "N=2, S=0 -> Partially fired" in wl


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
