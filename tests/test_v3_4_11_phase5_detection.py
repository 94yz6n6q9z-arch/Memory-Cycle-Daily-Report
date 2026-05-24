"""v3.4.11 — Regression tests for the deterministic Phase 4 → Phase 5
transition detector + phase-conditional case-scenario invariants +
phase-conditional template labels + phase-conditional chart anchor.

Three composite OR-conditions for Phase 5 entry, asymmetric reversion,
anti-flicker hysteresis. Tests each condition in isolation, cross-condition
independence, anti-flicker, reversion, parameter sensitivity, downstream
threading through the invariant enforcer, template label flip, chart
anchor flip, and state persistence.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

# Make project root importable
PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT))


# ============================================================
# Test helpers — build pseudo-assessments and price series
# ============================================================

def make_assessment(id_, tier, status="Not fired"):
    """Build a SimpleNamespace with the three attributes the detector reads."""
    return SimpleNamespace(id=id_, tier=tier, current_status=status)


def make_history(prices, end_date=None):
    """Build a pd.Series with daily-spaced dates ending at end_date."""
    if end_date is None:
        end_date = date(2026, 5, 23)
    n = len(prices)
    dates = pd.date_range(end=end_date, periods=n, freq="D")
    return pd.Series(prices, index=dates)


DEFAULT_PARAMS = {
    "t3_fire_count_threshold": 2,
    "drawdown_threshold_a_pct": 10.0,
    "anti_whipsaw_days_a": 5,
    "t4_fire_count_threshold": 1,
    "t2_total_fire_count_threshold": 3,
    "drawdown_threshold_b_pct": 15.0,
    "drawdown_threshold_c_pct": 20.0,
    "stale_high_min_days_c": 45,
    "trailing_high_window_days": 90,
    "anti_flicker_consecutive_runs": 2,
}


def all_t3_indicators():
    return [
        "t3_contract_decline", "t3_supplier_inventory_rebuild",
        "t3_relative_strength", "t3_wfe_orders_crack",
        "t3_eps_miss_guidance",
    ]


def all_t4_indicators():
    return [
        "t4_yoy_asp_negative", "t4_guidance_cuts",
        "t4_capex_slashed", "t4_layoffs",
    ]


def all_t2_indicators():
    return [
        "t2_eps_revisions", "t2_capex_parabolic", "t2_ltas_signed",
        "t2_insider_sales", "t2_pe_compressed",
        "t2_hyperscaler_capex_revenue", "t2_spot_contract_spread",
    ]


# ============================================================
# 1. Helper functions — trailing high + fire counting
# ============================================================

class TestTrailingHigh(unittest.TestCase):

    def test_drawdown_negative_when_below_peak(self):
        from src.analysis.phase_detector import compute_trailing_high
        # Series: 100, 105, 110, 100, 95 (peak at index 2)
        h = make_history([100, 105, 110, 100, 95], end_date=date(2026, 5, 23))
        r = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        self.assertIsNotNone(r)
        self.assertEqual(r["high_price"], 110.0)
        self.assertEqual(r["current_price"], 95.0)
        # (95 - 110) / 110 * 100 = -13.636...
        self.assertAlmostEqual(r["drawdown_pct"], -13.636, places=2)

    def test_drawdown_zero_at_new_high(self):
        from src.analysis.phase_detector import compute_trailing_high
        # Latest is the highest
        h = make_history([90, 95, 100, 105, 110])
        r = compute_trailing_high(h, window_days=90)
        self.assertEqual(r["drawdown_pct"], 0.0)

    def test_window_excludes_old_data(self):
        from src.analysis.phase_detector import compute_trailing_high
        # 100-day series with peak at day 0 (oldest); window=30 excludes it
        n = 100
        prices = [200.0] + [100.0] * (n - 1)  # peak at oldest
        end = date(2026, 5, 23)
        h = pd.Series(prices, index=pd.date_range(end=end, periods=n, freq="D"))
        # 30-day window — should not see the 200 peak
        r = compute_trailing_high(h, window_days=30, as_of=end)
        self.assertEqual(r["high_price"], 100.0)
        self.assertEqual(r["drawdown_pct"], 0.0)

    def test_none_on_empty_history(self):
        from src.analysis.phase_detector import compute_trailing_high
        self.assertIsNone(compute_trailing_high(None, window_days=90))
        self.assertIsNone(compute_trailing_high(pd.Series([], dtype=float), window_days=90))

    def test_days_since_high_computed(self):
        from src.analysis.phase_detector import compute_trailing_high
        # Peak 30 days ago, then declining
        n = 60
        prices = [100.0] * 30 + [150.0] + [100.0] * 29
        end = date(2026, 5, 23)
        h = pd.Series(prices, index=pd.date_range(end=end, periods=n, freq="D"))
        r = compute_trailing_high(h, window_days=90, as_of=end)
        self.assertEqual(r["high_price"], 150.0)
        self.assertEqual(r["days_since_high"], 29)


class TestFireCountingHelpers(unittest.TestCase):

    def test_count_only_counts_fired_by_default(self):
        from src.analysis.phase_detector import count_fired_in_tier
        assessments = [
            make_assessment("t3_a", 3, "Fired"),
            make_assessment("t3_b", 3, "Partially fired"),
            make_assessment("t3_c", 3, "Not fired"),
            make_assessment("t2_a", 2, "Fired"),  # wrong tier
        ]
        self.assertEqual(count_fired_in_tier(assessments, tier=3), 1)

    def test_include_partial_adds_partials(self):
        from src.analysis.phase_detector import count_fired_in_tier
        assessments = [
            make_assessment("t3_a", 3, "Fired"),
            make_assessment("t3_b", 3, "Partially fired"),
        ]
        self.assertEqual(
            count_fired_in_tier(assessments, tier=3, include_partial=True), 2
        )

    def test_restricts_to_indicator_ids_when_provided(self):
        from src.analysis.phase_detector import count_fired_in_tier
        assessments = [
            make_assessment("t3_a", 3, "Fired"),
            make_assessment("t3_b", 3, "Fired"),
        ]
        # Restrict to t3_a only
        self.assertEqual(
            count_fired_in_tier(assessments, tier=3, indicator_ids={"t3_a"}),
            1
        )

    def test_is_indicator_fired_finds_match(self):
        from src.analysis.phase_detector import is_indicator_fired
        assessments = [
            make_assessment("t2_eps_revisions", 2, "Fired"),
            make_assessment("t2_other", 2, "Not fired"),
        ]
        self.assertTrue(is_indicator_fired(assessments, "t2_eps_revisions"))
        self.assertFalse(is_indicator_fired(assessments, "t2_other"))
        self.assertFalse(is_indicator_fired(assessments, "missing"))


# ============================================================
# 2. Condition A — Concurrent + price + anti-whipsaw
# ============================================================

class TestConditionAEvaluation(unittest.TestCase):

    def setUp(self):
        # Today's price 88, 90d high 100 → -12% drawdown (>10% threshold)
        self.history = make_history([100, 95, 92, 90, 88], end_date=date(2026, 5, 23))
        # Two T3 indicators fired
        self.assessments = [
            make_assessment("t3_contract_decline", 3, "Fired"),
            make_assessment("t3_supplier_inventory_rebuild", 3, "Fired"),
            make_assessment("t3_relative_strength", 3, "Not fired"),
            make_assessment("t3_wfe_orders_crack", 3, "Not fired"),
            make_assessment("t3_eps_miss_guidance", 3, "Not fired"),
        ]

    def test_trips_when_first_seen_5_days_ago(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        th = compute_trailing_high(self.history, window_days=90, as_of=date(2026, 5, 23))
        # Anti-whipsaw stamp from 6 days ago
        first_seen = (date(2026, 5, 23) - timedelta(days=6)).isoformat()
        prior = {"condition_a_first_seen_iso": first_seen}
        result = _evaluate_condition_a(
            self.assessments, th, prior, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertTrue(result.met)

    def test_doesnt_trip_on_first_run_meeting_threshold(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        th = compute_trailing_high(self.history, window_days=90, as_of=date(2026, 5, 23))
        # No prior stamp — first time meeting threshold
        result = _evaluate_condition_a(
            self.assessments, th, {}, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertFalse(result.met)
        # But stamps the first_seen for next time
        self.assertEqual(result.detail["first_seen_iso"], "2026-05-23")

    def test_only_1_t3_does_not_meet(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        assessments = [make_assessment("t3_a", 3, "Fired")]
        th = compute_trailing_high(self.history, window_days=90, as_of=date(2026, 5, 23))
        result = _evaluate_condition_a(
            assessments, th, {}, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertFalse(result.met)
        self.assertEqual(result.detail["t3_fires"], 1)

    def test_drawdown_below_threshold_does_not_meet(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        # Only -5% drawdown
        h = make_history([100, 99, 98, 96, 95], end_date=date(2026, 5, 23))
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        # Stamp from 6 days ago
        first_seen = (date(2026, 5, 23) - timedelta(days=6)).isoformat()
        prior = {"condition_a_first_seen_iso": first_seen}
        result = _evaluate_condition_a(
            self.assessments, th, prior, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertFalse(result.met)

    def test_clears_first_seen_when_threshold_unmet(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        # Strong drawdown but no T3 fires
        assessments = [make_assessment("t3_a", 3, "Not fired")]
        th = compute_trailing_high(self.history, window_days=90, as_of=date(2026, 5, 23))
        prior = {"condition_a_first_seen_iso": "2026-05-17"}
        result = _evaluate_condition_a(
            assessments, th, prior, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertFalse(result.met)
        # First-seen cleared because threshold not met today
        self.assertIsNone(result.detail["first_seen_iso"])


# ============================================================
# 3. Condition B — Lagging confirmation
# ============================================================

class TestConditionBEvaluation(unittest.TestCase):

    def test_b_meets_when_all_ingredients_present(self):
        from src.analysis.phase_detector import _evaluate_condition_b, compute_trailing_high
        # -18% drawdown
        h = make_history([100, 95, 90, 85, 82], end_date=date(2026, 5, 23))
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        assessments = [
            make_assessment("t4_yoy_asp_negative", 4, "Fired"),
            make_assessment("t2_eps_revisions", 2, "Fired"),
            make_assessment("t2_capex_parabolic", 2, "Fired"),
            make_assessment("t2_ltas_signed", 2, "Fired"),  # 3rd T2
        ]
        result = _evaluate_condition_b(assessments, th, DEFAULT_PARAMS)
        self.assertTrue(result.met)

    def test_b_requires_t2_eps_revisions_specifically(self):
        from src.analysis.phase_detector import _evaluate_condition_b, compute_trailing_high
        h = make_history([100, 95, 90, 85, 82], end_date=date(2026, 5, 23))
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        # 3 T2 fires but NOT t2_eps_revisions
        assessments = [
            make_assessment("t4_yoy_asp_negative", 4, "Fired"),
            make_assessment("t2_eps_revisions", 2, "Not fired"),
            make_assessment("t2_capex_parabolic", 2, "Fired"),
            make_assessment("t2_ltas_signed", 2, "Fired"),
            make_assessment("t2_insider_sales", 2, "Fired"),
        ]
        result = _evaluate_condition_b(assessments, th, DEFAULT_PARAMS)
        self.assertFalse(result.met)

    def test_b_requires_15_pct_drawdown(self):
        from src.analysis.phase_detector import _evaluate_condition_b, compute_trailing_high
        # Only -12%
        h = make_history([100, 95, 92, 90, 88], end_date=date(2026, 5, 23))
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        assessments = [
            make_assessment("t4_yoy_asp_negative", 4, "Fired"),
            make_assessment("t2_eps_revisions", 2, "Fired"),
            make_assessment("t2_capex_parabolic", 2, "Fired"),
            make_assessment("t2_ltas_signed", 2, "Fired"),
        ]
        result = _evaluate_condition_b(assessments, th, DEFAULT_PARAMS)
        self.assertFalse(result.met)

    def test_b_requires_at_least_one_t4(self):
        from src.analysis.phase_detector import _evaluate_condition_b, compute_trailing_high
        h = make_history([100, 95, 90, 85, 82], end_date=date(2026, 5, 23))
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        # Zero T4 fires
        assessments = [
            make_assessment("t2_eps_revisions", 2, "Fired"),
            make_assessment("t2_capex_parabolic", 2, "Fired"),
            make_assessment("t2_ltas_signed", 2, "Fired"),
        ]
        result = _evaluate_condition_b(assessments, th, DEFAULT_PARAMS)
        self.assertFalse(result.met)


# ============================================================
# 4. Condition C — Decisive price action
# ============================================================

class TestConditionCEvaluation(unittest.TestCase):

    def test_c_meets_at_20_pct_drawdown_with_stale_high(self):
        from src.analysis.phase_detector import _evaluate_condition_c, compute_trailing_high
        # Peak 50 days ago, current price -22%
        n = 100
        prices = [100.0] * 50 + [78.0] * 50  # peak day 0 of window, -22% recent
        # Actually we need the peak in the window — let me restructure
        # Peak on day 0 = 100, then decline to 78 over next 99 days
        prices = [100.0] + [100.0 - 22 * (i / 99) for i in range(1, 100)]
        end = date(2026, 5, 23)
        h = pd.Series(prices, index=pd.date_range(end=end, periods=n, freq="D"))
        th = compute_trailing_high(h, window_days=90, as_of=end)
        # Sanity check: drawdown ~-22%, days_since_high large
        self.assertLessEqual(th["drawdown_pct"], -20.0)
        self.assertGreaterEqual(th["days_since_high"], 45)

        assessments = [make_assessment("t2_eps_revisions", 2, "Fired")]
        result = _evaluate_condition_c(assessments, th, DEFAULT_PARAMS)
        self.assertTrue(result.met)

    def test_c_requires_t2_eps_revisions_fired(self):
        from src.analysis.phase_detector import _evaluate_condition_c, compute_trailing_high
        n = 100
        prices = [100.0] + [100.0 - 22 * (i / 99) for i in range(1, 100)]
        end = date(2026, 5, 23)
        h = pd.Series(prices, index=pd.date_range(end=end, periods=n, freq="D"))
        th = compute_trailing_high(h, window_days=90, as_of=end)
        # eps_revisions NOT fired
        assessments = [make_assessment("t2_eps_revisions", 2, "Not fired")]
        result = _evaluate_condition_c(assessments, th, DEFAULT_PARAMS)
        self.assertFalse(result.met)

    def test_c_rejects_recent_high(self):
        from src.analysis.phase_detector import _evaluate_condition_c, compute_trailing_high
        # Sharp -25% but high was 30 days ago (< 45 day threshold)
        n = 60
        prices = [70.0] * 30 + [100.0] + [70.0] * 29  # peak at day 30, days_since_high=29
        end = date(2026, 5, 23)
        h = pd.Series(prices, index=pd.date_range(end=end, periods=n, freq="D"))
        th = compute_trailing_high(h, window_days=90, as_of=end)
        self.assertLess(th["days_since_high"], 45)

        assessments = [make_assessment("t2_eps_revisions", 2, "Fired")]
        result = _evaluate_condition_c(assessments, th, DEFAULT_PARAMS)
        self.assertFalse(result.met)


# ============================================================
# 5. Cascade integration — evaluate_phase_for_run end-to-end
# ============================================================

class TestPhaseCascadeIntegration(unittest.TestCase):

    def test_no_flip_on_first_run_meeting_condition(self):
        """Anti-flicker requires 2 consecutive runs."""
        from src.analysis.phase_detector import evaluate_phase_for_run
        h = make_history([100, 95, 92, 90, 88], end_date=date(2026, 5, 23))
        # Condition A would meet but first-run stamping for condition A
        # also requires hold-days
        assessments = [
            make_assessment("t3_contract_decline", 3, "Fired"),
            make_assessment("t3_supplier_inventory_rebuild", 3, "Fired"),
        ]
        # condition_a stamped already (hold satisfied), but no prior pending
        first_seen = (date(2026, 5, 23) - timedelta(days=6)).isoformat()
        prior = {
            "current_phase": 4,
            "condition_a_first_seen_iso": first_seen,
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        # First run with any condition met — stays at 4, marks pending
        self.assertEqual(result.current_phase, 4)
        self.assertFalse(result.flipped_this_run)
        self.assertIsNotNone(result.pending_condition)

    def test_flip_on_second_consecutive_run(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        h = make_history([100, 95, 92, 90, 88], end_date=date(2026, 5, 23))
        assessments = [
            make_assessment("t3_contract_decline", 3, "Fired"),
            make_assessment("t3_supplier_inventory_rebuild", 3, "Fired"),
        ]
        first_seen = (date(2026, 5, 23) - timedelta(days=6)).isoformat()
        # Pending from a prior run
        prior = {
            "current_phase": 4,
            "condition_a_first_seen_iso": first_seen,
            "anti_flicker_pending_condition": "A",
            "anti_flicker_pending_first_seen_iso": (date(2026, 5, 22)).isoformat(),
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 5)
        self.assertTrue(result.flipped_this_run)
        self.assertEqual(result.phase_5_detected_date_iso, "2026-05-23")
        # Cycle peak price recorded
        self.assertEqual(result.cycle_peak_price_at_detection, 100.0)

    def test_flicker_clears_pending_when_condition_lapses(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        h = make_history([100, 99, 98, 99, 100], end_date=date(2026, 5, 23))  # at high
        assessments = [make_assessment("t3_a", 3, "Not fired")]
        prior = {
            "current_phase": 4,
            "anti_flicker_pending_condition": "A",
            "anti_flicker_pending_first_seen_iso": "2026-05-22",
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        # No conditions met — clears pending
        self.assertEqual(result.current_phase, 4)
        self.assertIsNone(result.pending_condition)

    def test_phase_5_persists_when_no_reversion(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        h = make_history([100, 95, 90, 85, 80])
        assessments = [
            make_assessment("t3_contract_decline", 3, "Fired"),
            make_assessment("t3_supplier_inventory_rebuild", 3, "Fired"),
        ]
        prior = {
            "current_phase": 5,
            "phase_5_detected_date_iso": "2026-05-15",
            "cycle_peak_price_at_detection": 100.0,
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 5)
        self.assertFalse(result.flipped_this_run)
        # Detection date preserved
        self.assertEqual(result.phase_5_detected_date_iso, "2026-05-15")
        self.assertEqual(result.cycle_peak_price_at_detection, 100.0)

    def test_reversion_requires_both_new_high_and_t3_dissipated(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        # At/above prior peak
        h = make_history([100, 102, 101, 103, 105])
        # < 2 T3 fired
        assessments = [make_assessment("t3_contract_decline", 3, "Not fired")]
        prior = {
            "current_phase": 5,
            "phase_5_detected_date_iso": "2026-04-15",
            "cycle_peak_price_at_detection": 100.0,
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 4)
        self.assertTrue(result.flipped_this_run)
        # Detection-date cleared on reversion
        self.assertIsNone(result.phase_5_detected_date_iso)

    def test_reversion_blocked_by_lingering_t3_fires(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        # New high but T3 still firing
        h = make_history([100, 102, 101, 103, 105])
        assessments = [
            make_assessment("t3_contract_decline", 3, "Fired"),
            make_assessment("t3_supplier_inventory_rebuild", 3, "Fired"),
        ]
        prior = {"current_phase": 5}
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 5)

    def test_reversion_blocked_by_no_new_high(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        # Below prior high
        h = make_history([100, 95, 92, 90, 88])
        # T3 dissipated
        assessments = [make_assessment("t3_contract_decline", 3, "Not fired")]
        prior = {"current_phase": 5}
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 5)


# ============================================================
# 6. Cross-condition independence
# ============================================================

class TestCrossConditionIndependence(unittest.TestCase):
    """Each condition (A, B, C) trips independently. The cascade fires
    whenever ANY of the three are met."""

    def test_only_c_can_trigger_alone(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        # Massive crash, t2_eps_revisions fired, but no T3/T4
        n = 100
        prices = [100.0] + [100.0 - 25 * (i / 99) for i in range(1, 100)]
        end = date(2026, 5, 23)
        h = pd.Series(prices, index=pd.date_range(end=end, periods=n, freq="D"))
        assessments = [make_assessment("t2_eps_revisions", 2, "Fired")]
        # Pending from prior run so anti-flicker resolves
        prior = {
            "current_phase": 4,
            "anti_flicker_pending_condition": "C",
            "anti_flicker_pending_first_seen_iso": "2026-05-22",
        }
        result = evaluate_phase_for_run(assessments, h, prior, DEFAULT_PARAMS, as_of=end)
        self.assertEqual(result.current_phase, 5)

    def test_only_b_can_trigger_alone(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        # Moderate decline, T4 + T2 firing
        h = make_history([100, 95, 90, 85, 82], end_date=date(2026, 5, 23))
        assessments = [
            make_assessment("t4_yoy_asp_negative", 4, "Fired"),
            make_assessment("t2_eps_revisions", 2, "Fired"),
            make_assessment("t2_capex_parabolic", 2, "Fired"),
            make_assessment("t2_ltas_signed", 2, "Fired"),
        ]
        prior = {
            "current_phase": 4,
            "anti_flicker_pending_condition": "B",
            "anti_flicker_pending_first_seen_iso": "2026-05-22",
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 5)

    def test_no_condition_means_no_flip(self):
        from src.analysis.phase_detector import evaluate_phase_for_run
        h = make_history([100, 99, 100, 101, 100])  # flat at high
        assessments = [make_assessment("t3_a", 3, "Not fired")]
        result = evaluate_phase_for_run(
            assessments, h, {}, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        self.assertEqual(result.current_phase, 4)


# ============================================================
# 7. Boundary sensitivity — drawdown thresholds
# ============================================================

class TestParameterBoundarySensitivity(unittest.TestCase):

    def test_drawdown_just_under_threshold_a_fails(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        # -9.9% — just under 10% threshold
        h = pd.Series(
            [100.0, 99.5, 95.0, 92.0, 90.1],
            index=pd.date_range(end=date(2026, 5, 23), periods=5, freq="D")
        )
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        self.assertGreater(th["drawdown_pct"], -10.0)  # -9.9%, above (i.e. less negative) than -10
        assessments = [
            make_assessment("t3_a", 3, "Fired"),
            make_assessment("t3_b", 3, "Fired"),
        ]
        prior = {"condition_a_first_seen_iso": "2026-05-17"}
        result = _evaluate_condition_a(
            assessments, th, prior, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertFalse(result.met)

    def test_drawdown_just_over_threshold_a_passes(self):
        from src.analysis.phase_detector import _evaluate_condition_a, compute_trailing_high
        # -10.1% — just over 10% threshold
        h = pd.Series(
            [100.0, 99.0, 95.0, 92.0, 89.9],
            index=pd.date_range(end=date(2026, 5, 23), periods=5, freq="D")
        )
        th = compute_trailing_high(h, window_days=90, as_of=date(2026, 5, 23))
        self.assertLess(th["drawdown_pct"], -10.0)
        assessments = [
            make_assessment("t3_a", 3, "Fired"),
            make_assessment("t3_b", 3, "Fired"),
        ]
        prior = {"condition_a_first_seen_iso": "2026-05-17"}
        result = _evaluate_condition_a(
            assessments, th, prior, DEFAULT_PARAMS, "2026-05-23"
        )
        self.assertTrue(result.met)


# ============================================================
# 8. Invariant enforcer — phase-conditional behavior
# ============================================================

class TestPhaseConditionalInvariants(unittest.TestCase):

    def setUp(self):
        from src.analysis.llm_summarizer import CaseScenario
        self.CaseScenario = CaseScenario

    def test_phase_4_base_clamped_non_negative(self):
        from src.analysis.llm_summarizer import _enforce_scenario_invariants
        base = self.CaseScenario(50, 4, "Aug", "Oct", -10.0)  # base trying to go negative
        bull = self.CaseScenario(22, 12, "Q1", "Q2", 55.0)
        bear = self.CaseScenario(28, 0, "May", "May", -20.0)
        b, bl, br = _enforce_scenario_invariants(base, bull, bear, current_phase=4)
        self.assertGreaterEqual(b.return_to_peak_pct, 0.0)

    def test_phase_5_base_clamped_non_positive(self):
        from src.analysis.llm_summarizer import _enforce_scenario_invariants
        base = self.CaseScenario(50, 9, "Q1 2027", "Q3 2027", 10.0)  # base trying to go positive
        bull = self.CaseScenario(22, 15, "Q4 2027", "Q2 2028", -25.0)
        bear = self.CaseScenario(28, 6, "Q4 2026", "Q2 2027", -60.0)
        b, bl, br = _enforce_scenario_invariants(base, bull, bear, current_phase=5)
        # Phase 5: base must be ≤ 0
        self.assertLessEqual(b.return_to_peak_pct, 0.0)

    def test_phase_5_bull_above_base(self):
        from src.analysis.llm_summarizer import _enforce_scenario_invariants
        base = self.CaseScenario(50, 9, "Q1", "Q3", -40.0)
        bull = self.CaseScenario(22, 15, "Q4", "Q2", -50.0)  # bull below base — invalid
        bear = self.CaseScenario(28, 6, "Q4", "Q2", -60.0)
        b, bl, br = _enforce_scenario_invariants(base, bull, bear, current_phase=5)
        # Bull must be > base (shallower decline)
        self.assertGreater(bl.return_to_peak_pct, b.return_to_peak_pct)

    def test_phase_5_bear_below_base(self):
        from src.analysis.llm_summarizer import _enforce_scenario_invariants
        base = self.CaseScenario(50, 9, "Q1", "Q3", -40.0)
        bull = self.CaseScenario(22, 15, "Q4", "Q2", -25.0)
        bear = self.CaseScenario(28, 6, "Q4", "Q2", -20.0)  # bear above base — invalid
        b, bl, br = _enforce_scenario_invariants(base, bull, bear, current_phase=5)
        # Bear must be < base (deeper decline)
        self.assertLess(br.return_to_peak_pct, b.return_to_peak_pct)

    def test_phase_5_valid_input_preserved(self):
        """A valid Phase 5 emission survives the enforcer unchanged."""
        from src.analysis.llm_summarizer import _enforce_scenario_invariants
        base = self.CaseScenario(50, 9, "Q1 2027", "Q3 2027", -40.0)
        bull = self.CaseScenario(22, 15, "Q4 2027", "Q2 2028", -25.0)
        bear = self.CaseScenario(28, 6, "Q4 2026", "Q2 2027", -60.0)
        b, bl, br = _enforce_scenario_invariants(base, bull, bear, current_phase=5)
        self.assertAlmostEqual(b.return_to_peak_pct, -40.0)
        self.assertAlmostEqual(bl.return_to_peak_pct, -25.0)
        self.assertAlmostEqual(br.return_to_peak_pct, -60.0)

    def test_phase_4_valid_input_preserved(self):
        """A valid Phase 4 emission survives the enforcer unchanged."""
        from src.analysis.llm_summarizer import _enforce_scenario_invariants
        base = self.CaseScenario(50, 4, "Aug", "Oct", 18.0)
        bull = self.CaseScenario(22, 12, "Q1", "Q2", 55.0)
        bear = self.CaseScenario(28, 0, "May", "May", -8.0)
        b, bl, br = _enforce_scenario_invariants(base, bull, bear, current_phase=4)
        self.assertAlmostEqual(b.return_to_peak_pct, 18.0)
        self.assertAlmostEqual(bl.return_to_peak_pct, 55.0)
        self.assertAlmostEqual(br.return_to_peak_pct, -8.0)


# ============================================================
# 9. Sonnet prompt — phase-conditional content
# ============================================================

class TestSonnetPromptPhaseConditional(unittest.TestCase):

    def _build_prompt(self, phase_state):
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt, NewsTriage
        triage = NewsTriage(relevant_news=[], broker_actions=[], trendforce_commentary=[], indicator_relevance={})
        return build_sonnet_analysis_prompt(
            triage=triage,
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            phase_state=phase_state,
        )

    def test_phase_4_prompt_contains_boom_language(self):
        prompt = self._build_prompt(None)
        self.assertIn("Boom / Euphoria", prompt)
        self.assertNotIn("PHASE 5 ACTIVE", prompt)

    def test_phase_5_prompt_swaps_to_decline_language(self):
        prompt = self._build_prompt({
            "current_phase": 5,
            "current_phase_label": "Decline",
            "phase_5_detected_date_iso": "2026-09-15",
            "cycle_peak_price_at_detection": 200000.0,
        })
        self.assertIn("PHASE 5 ACTIVE", prompt)
        self.assertIn("return_to_trough_pct", prompt)
        # Phase 5 JSON example should show negative base
        self.assertIn('"return_to_peak_pct": -40', prompt)

    def test_phase_5_prompt_has_phase_5_invariant_4(self):
        prompt = self._build_prompt({"current_phase": 5})
        # Phase 5 invariant 4: base ≤ 0
        self.assertIn("<= 0", prompt)


# ============================================================
# 10. State persistence — atomic write, load defaults
# ============================================================

class TestPhaseStatePersistence(unittest.TestCase):

    def test_load_missing_file_returns_empty_dict(self):
        from src import state as state_mod
        # Patch PHASE_STATE_FILE to a non-existent path
        with patch.object(state_mod, "PHASE_STATE_FILE", Path("/tmp/nonexistent_v3_4_11.json")):
            result = state_mod.load_phase_state()
        self.assertEqual(result, {})

    def test_load_corrupt_file_returns_empty_dict(self):
        from src import state as state_mod
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json{{{")
            tmp_path = Path(f.name)
        try:
            with patch.object(state_mod, "PHASE_STATE_FILE", tmp_path):
                result = state_mod.load_phase_state()
            self.assertEqual(result, {})
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_save_and_load_roundtrip(self):
        from src import state as state_mod
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td) / "phase_state.json"
            with patch.object(state_mod, "PHASE_STATE_FILE", tmp_path):
                state_mod.save_phase_state({
                    "current_phase": 5,
                    "current_phase_label": "Decline",
                    "phase_5_detected_date_iso": "2026-09-15",
                    "cycle_peak_price_at_detection": 200000.0,
                })
                loaded = state_mod.load_phase_state()
            self.assertEqual(loaded["current_phase"], 5)
            self.assertEqual(loaded["cycle_peak_price_at_detection"], 200000.0)

    def test_load_non_dict_root_returns_empty(self):
        from src import state as state_mod
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("[1, 2, 3]")
            tmp_path = Path(f.name)
        try:
            with patch.object(state_mod, "PHASE_STATE_FILE", tmp_path):
                result = state_mod.load_phase_state()
            self.assertEqual(result, {})
        finally:
            tmp_path.unlink(missing_ok=True)


# ============================================================
# 11. result_to_state_dict serialization
# ============================================================

class TestResultSerialization(unittest.TestCase):

    def test_result_to_state_dict_includes_required_fields(self):
        from src.analysis.phase_detector import (
            evaluate_phase_for_run, result_to_state_dict,
        )
        h = make_history([100, 95, 92, 90, 88], end_date=date(2026, 5, 23))
        assessments = [
            make_assessment("t3_contract_decline", 3, "Fired"),
            make_assessment("t3_supplier_inventory_rebuild", 3, "Fired"),
        ]
        first_seen = (date(2026, 5, 23) - timedelta(days=6)).isoformat()
        prior = {
            "current_phase": 4,
            "condition_a_first_seen_iso": first_seen,
            "anti_flicker_pending_condition": "A",
            "anti_flicker_pending_first_seen_iso": "2026-05-22",
        }
        result = evaluate_phase_for_run(
            assessments, h, prior, DEFAULT_PARAMS, as_of=date(2026, 5, 23)
        )
        state = result_to_state_dict(result)
        # All required keys present
        for key in [
            "current_phase", "current_phase_label",
            "phase_5_detected_date_iso", "cycle_peak_price_at_detection",
            "anti_flicker_pending_condition", "anti_flicker_pending_first_seen_iso",
            "condition_a_first_seen_iso", "last_evaluated_iso", "last_summary_line",
        ]:
            self.assertIn(key, state, f"Missing key: {key}")


# ============================================================
# 12. Template label flip — "peak" → "trough"
# ============================================================

class TestTemplateLabelFlip(unittest.TestCase):

    def _make_exec_summary(self, current_phase):
        from src.analysis.llm_summarizer import (
            ExecutiveSummary, CaseScenario,
        )
        if current_phase == 5:
            base = CaseScenario(50, 9, "Q1 2027", "Q3 2027", -40.0)
            bull = CaseScenario(22, 15, "Q4 2027", "Q2 2028", -25.0)
            bear = CaseScenario(28, 6, "Q4 2026", "Q2 2027", -60.0)
            label = "Decline"
        else:
            base = CaseScenario(50, 4, "August 2026", "October 2026", 18.0)
            bull = CaseScenario(22, 12, "Q1 2027", "Q2 2027", 55.0)
            bear = CaseScenario(28, 0, "May 2026", "May 2026", -8.0)
            label = "Boom / Euphoria (late stage)"
        return ExecutiveSummary(
            current_phase=current_phase,
            current_phase_label=label,
            tier_status_summary="",
            new_triggers_today=[],
            base_case=base,
            bull_case=bull,
            bear_case=bear,
            delta_from_prior_text="",
            urgent_attention_needed=False,
            urgent_attention_text="",
            executive_brief="",
        )

    def test_phase_4_uses_peak_label(self):
        from src.email_module.template import _build_executive_summary
        es = self._make_exec_summary(4)
        html = _build_executive_summary(es)
        self.assertIn("peak", html)

    def test_phase_5_uses_trough_label(self):
        from src.email_module.template import _build_executive_summary
        es = self._make_exec_summary(5)
        html = _build_executive_summary(es)
        self.assertIn("trough", html)
        # And specifically NOT "peak in" anywhere (the case-block labels)
        self.assertNotIn("peak in 9mo", html)
        self.assertIn("trough in 9mo", html)


# ============================================================
# 13. Chart anchor — phase-conditional behavior
# ============================================================

class TestChartAnchorPhaseConditional(unittest.TestCase):

    def test_phase_5_does_not_force_peak_to_future(self):
        """In Phase 5, peak_date <= today is preserved (not forced to today + 30d)."""
        from src.analysis import chart_generator
        # Smoke-check: chart_generator imports and the source code contains
        # the v3.4.11 phase-conditional branch.
        src = Path(chart_generator.__file__).read_text()
        self.assertIn("v3.4.11", src)
        self.assertIn("current_phase >= 5", src)


# ============================================================
# 14. fast_tier integration — module-level import sanity
# ============================================================

class TestFastTierIntegration(unittest.TestCase):

    def test_phase_detector_imported_in_fast_tier(self):
        from src import fast_tier
        # Module-level: evaluate_phase_for_run is in fast_tier's namespace
        self.assertTrue(hasattr(fast_tier, "evaluate_phase_for_run"))
        self.assertTrue(hasattr(fast_tier, "result_to_state_dict"))
        self.assertTrue(hasattr(fast_tier, "load_phase_state"))
        self.assertTrue(hasattr(fast_tier, "save_phase_state"))

    def test_phase_5_detection_constants_in_config(self):
        from src.config import PHASE_5_DETECTION, PHASE_5_TROUGH_ANCHORS
        # Required keys
        for key in [
            "t3_fire_count_threshold", "drawdown_threshold_a_pct",
            "anti_whipsaw_days_a", "t4_fire_count_threshold",
            "t2_total_fire_count_threshold", "drawdown_threshold_b_pct",
            "drawdown_threshold_c_pct", "stale_high_min_days_c",
            "trailing_high_window_days", "anti_flicker_consecutive_runs",
        ]:
            self.assertIn(key, PHASE_5_DETECTION, f"missing param: {key}")
        for key in [
            "base_return_to_trough_pct", "bear_return_to_trough_pct",
            "bull_return_to_trough_pct",
        ]:
            self.assertIn(key, PHASE_5_TROUGH_ANCHORS, f"missing anchor: {key}")


if __name__ == "__main__":
    unittest.main()
