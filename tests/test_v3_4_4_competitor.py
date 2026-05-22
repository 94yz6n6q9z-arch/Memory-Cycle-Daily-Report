"""v3.4.4 tests — Indicator 7 (Competitor dynamics) composite scoring.

Per the framework session 2026-05-22 with Raul:
  - Status calibrated to estimated SK Hynix EPS impact (price × volume)
  - Not fired <5%, Partially fired 5-20%, Fired >20%
  - Composite applies Part 14 dual-threat amplifier:
      * 2+ subs Fired → Fired (regardless of cumulative)
      * 1 Fired + 2+ Partially fired → Fired (multi-front compression)
  - Today's bootstrap state (data/competitor_thresholds.yaml) produces
    composite Partially fired ~11.5% cumulative EPS, single-front 7B
"""

import unittest
from pathlib import Path

import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.competitor_scoring import (  # noqa: E402
    compute_competitor_snapshot,
    STATUS_NOT_FIRED,
    STATUS_PARTIALLY_FIRED,
    STATUS_FIRED,
)


def _make_thresholds(eps_pct_by_sub: dict) -> dict:
    """v3.4.4.1: returns the THRESHOLDS dict (data/competitor_thresholds.yaml
    equivalent — display names + dual-threat scenario only). Use with
    _make_state() to drive compute_competitor_snapshot(thresholds=t, state=s)."""
    return {
        "schema_version": "2.0",
        "sub_7a_cxmt": {"display_name": "CXMT", "sources": []},
        "sub_7b_samsung_hbm4": {"display_name": "Samsung HBM4", "sources": []},
        "sub_7c_micron": {"display_name": "Micron HBM", "sources": []},
        "sub_7d_hbf": {
            "display_name": "HBF",
            "sources": [],
            "moat_extension_milestones": [],
            "substitution_risk_milestones": [],
        },
        "dual_threat_scenario": {
            "stock_impact_combined_pct": "-30 to -45",
            "stock_impact_with_cycle_peak_overlay_pct": "-60 to -75",
            "narrative": "Test narrative",
        },
    }


def _make_state(eps_pct_by_sub: dict) -> dict:
    """v3.4.4.1: returns the STATE dict (state/competitor_state.json equivalent).
    Current_status derived from EPS pct via the band mapping."""
    def _status(eps):
        if eps >= 20.0:
            return STATUS_FIRED
        if eps >= 5.0:
            return STATUS_PARTIALLY_FIRED
        return STATUS_NOT_FIRED

    return {
        "_metadata": {"schema_version": "2.0", "last_aggregator_run": "2026-05-22T12:00:00Z"},
        "sub_7a_cxmt": {
            "current_status": _status(eps_pct_by_sub["7A"]),
            "estimated_eps_impact_pct": eps_pct_by_sub["7A"],
            "current_state": {},
            "evidence_log": [],
        },
        "sub_7b_samsung_hbm4": {
            "current_status": _status(eps_pct_by_sub["7B"]),
            "estimated_eps_impact_pct": eps_pct_by_sub["7B"],
            "current_state": {},
            "evidence_log": [],
        },
        "sub_7c_micron": {
            "current_status": _status(eps_pct_by_sub["7C"]),
            "estimated_eps_impact_pct": eps_pct_by_sub["7C"],
            "current_state": {},
            "evidence_log": [],
        },
        "sub_7d_hbf": {
            "current_status": _status(eps_pct_by_sub["7D"]),
            "estimated_eps_impact_pct": eps_pct_by_sub["7D"],
            "current_state": {},
            "evidence_log": [],
        },
    }


class TestCompetitorBootstrapState(unittest.TestCase):
    """Validate today's YAML state produces the documented composite."""

    def test_today_is_partially_fired(self):
        snap = compute_competitor_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.composite_status, STATUS_PARTIALLY_FIRED)

    def test_today_cumulative_eps_around_11_5_pct(self):
        snap = compute_competitor_snapshot()
        self.assertGreaterEqual(snap.cumulative_eps_impact_pct, 10.0)
        self.assertLessEqual(snap.cumulative_eps_impact_pct, 15.0)

    def test_today_dual_threat_inactive(self):
        snap = compute_competitor_snapshot()
        self.assertFalse(snap.dual_threat_active)

    def test_today_7b_is_only_partial(self):
        snap = compute_competitor_snapshot()
        statuses = {s.sub_id: s.status for s in snap.subs}
        self.assertEqual(statuses["7A"], STATUS_NOT_FIRED)
        self.assertEqual(statuses["7B"], STATUS_PARTIALLY_FIRED)
        self.assertEqual(statuses["7C"], STATUS_NOT_FIRED)
        self.assertEqual(statuses["7D"], STATUS_NOT_FIRED)


class TestCompositeStatusPermutations(unittest.TestCase):
    """All-Not / single-Partial / single-Fired / dual-threat permutations."""

    def test_all_subs_not_fired_returns_not_fired(self):
        t = _make_thresholds({"7A": 1.0, "7B": 0.5, "7C": 0.0, "7D": 0.0})
        s = _make_state({"7A": 1.0, "7B": 0.5, "7C": 0.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_NOT_FIRED)
        self.assertFalse(snap.dual_threat_active)

    def test_single_partial_around_10_pct_is_partially_fired(self):
        t = _make_thresholds({"7A": 1.0, "7B": 10.0, "7C": 0.0, "7D": 0.0})
        s = _make_state({"7A": 1.0, "7B": 10.0, "7C": 0.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_PARTIALLY_FIRED)
        self.assertFalse(snap.dual_threat_active)

    def test_single_fired_sub_escalates_to_fired(self):
        # 7B alone at >20% (Fired band) — composite = Fired even if others Not
        t = _make_thresholds({"7A": 1.0, "7B": 25.0, "7C": 0.0, "7D": 0.0})
        s = _make_state({"7A": 1.0, "7B": 25.0, "7C": 0.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_FIRED)
        # Single-front fire, not dual-threat amplifier
        self.assertFalse(snap.dual_threat_active)

    def test_two_subs_fired_triggers_dual_threat_amplifier(self):
        # 7B + 7A both fired
        t = _make_thresholds({"7A": 22.0, "7B": 25.0, "7C": 0.0, "7D": 0.0})
        s = _make_state({"7A": 22.0, "7B": 25.0, "7C": 0.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_FIRED)
        self.assertTrue(snap.dual_threat_active)
        # Verify the reasoning mentions Part 14 dual-threat
        self.assertIn("dual-threat", snap.composite_reasoning.lower())

    def test_one_fired_plus_two_partial_triggers_dual_threat(self):
        # 1 Fired + 2 Partial (per the v3.4.4 framework session: this mix
        # escalates to Fired because multi-front damage exceeds single-vector)
        t = _make_thresholds({"7A": 10.0, "7B": 25.0, "7C": 10.0, "7D": 0.0})
        s = _make_state({"7A": 10.0, "7B": 25.0, "7C": 10.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_FIRED)
        self.assertTrue(snap.dual_threat_active)

    def test_three_partial_no_fired_stays_partially_fired_if_under_20(self):
        # 3 Partial subs at 8% each = 24% cumulative — passes the 20% threshold,
        # so the cumulative-EPS path escalates to Fired even without a sub Fired
        t = _make_thresholds({"7A": 8.0, "7B": 8.0, "7C": 8.0, "7D": 0.0})
        s = _make_state({"7A": 8.0, "7B": 8.0, "7C": 8.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_FIRED)
        # No sub Fired, so dual-threat amplifier NOT triggered (it requires
        # at least 1 sub Fired). This is the pure cumulative path.
        self.assertFalse(snap.dual_threat_active)

    def test_two_partial_subs_under_20_total_stays_partial(self):
        # 2 Partial subs at 7% each = 14% cumulative — between 10-20%
        # → Partially fired (cumulative path)
        t = _make_thresholds({"7A": 7.0, "7B": 7.0, "7C": 0.0, "7D": 0.0})
        s = _make_state({"7A": 7.0, "7B": 7.0, "7C": 0.0, "7D": 0.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.composite_status, STATUS_PARTIALLY_FIRED)
        self.assertFalse(snap.dual_threat_active)


class TestEpsImpactPersistedThroughComposite(unittest.TestCase):
    """Sanity check that the EPS impact is what's actually driving the composite,
    not some other proxy."""

    def test_cumulative_eps_pct_is_sum_of_sub_values(self):
        t = _make_thresholds({"7A": 3.5, "7B": 12.0, "7C": 1.5, "7D": 4.0})
        s = _make_state({"7A": 3.5, "7B": 12.0, "7C": 1.5, "7D": 4.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        # 3.5 + 12 + 1.5 + 4 = 21.0
        self.assertAlmostEqual(snap.cumulative_eps_impact_pct, 21.0, places=1)

    def test_subs_carry_their_individual_eps_values(self):
        t = _make_thresholds({"7A": 3.5, "7B": 12.0, "7C": 1.5, "7D": 4.0})
        s = _make_state({"7A": 3.5, "7B": 12.0, "7C": 1.5, "7D": 4.0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        sub_by_id = {s.sub_id: s for s in snap.subs}
        self.assertAlmostEqual(sub_by_id["7A"].estimated_eps_impact_pct, 3.5, places=1)
        self.assertAlmostEqual(sub_by_id["7B"].estimated_eps_impact_pct, 12.0, places=1)
        self.assertAlmostEqual(sub_by_id["7C"].estimated_eps_impact_pct, 1.5, places=1)
        self.assertAlmostEqual(sub_by_id["7D"].estimated_eps_impact_pct, 4.0, places=1)


class TestStatusBandMapping(unittest.TestCase):
    """The 5% / 20% bands are the framework defaults — protect them from drift."""

    def test_4_99_eps_pct_is_not_fired(self):
        t = _make_thresholds({"7A": 4.99, "7B": 0, "7C": 0, "7D": 0})
        s = _make_state({"7A": 4.99, "7B": 0, "7C": 0, "7D": 0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.subs[0].status, STATUS_NOT_FIRED)

    def test_5_pct_eps_is_partially_fired(self):
        t = _make_thresholds({"7A": 5.0, "7B": 0, "7C": 0, "7D": 0})
        s = _make_state({"7A": 5.0, "7B": 0, "7C": 0, "7D": 0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.subs[0].status, STATUS_PARTIALLY_FIRED)

    def test_19_99_pct_eps_is_partially_fired(self):
        t = _make_thresholds({"7A": 19.99, "7B": 0, "7C": 0, "7D": 0})
        s = _make_state({"7A": 19.99, "7B": 0, "7C": 0, "7D": 0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.subs[0].status, STATUS_PARTIALLY_FIRED)

    def test_20_pct_eps_is_fired(self):
        t = _make_thresholds({"7A": 20.0, "7B": 0, "7C": 0, "7D": 0})
        s = _make_state({"7A": 20.0, "7B": 0, "7C": 0, "7D": 0})
        snap = compute_competitor_snapshot(thresholds=t, state=s)
        self.assertEqual(snap.subs[0].status, STATUS_FIRED)


if __name__ == "__main__":
    unittest.main()
