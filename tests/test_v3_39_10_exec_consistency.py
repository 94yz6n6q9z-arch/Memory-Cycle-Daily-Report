"""Tests for v3.39.10 — executive-summary display consistency fixes.

Three independent inconsistencies were observed on the 2026-06-06 brief:

  Issue 1 — executive_brief opened with "No signals fired or unfired today"
            while the banners showed status changes (and the same sentence
            then described an unfire). Guarded by
            state.reconcile_executive_brief_opener.

  Issue 2 — the same indicator (t3_relative_strength) showed BOTH a
            "Not fired → Partially fired" and a "Partially fired → Not fired"
            banner, because the 24h trigger log kept both legs of a
            round-trip. Fixed by state.collapse_trigger_log_for_display.

  Issue 3 — the ADR (Indicator 8) showed "Partially fired" in the exec
            summary but "Not fired" in its section-4 card. Two causes: the
            8A sub-status mapping treated a filed-but-unsyndicated F-1 as
            "Not fired" (fixed in adr_aggregator._evaluate_sub_status), and
            the tier-table status was not slaved to the aggregator composite
            (fixed by the composite-authority override in fast_tier, covered
            indirectly here via the 8A mapping that drives the composite).
"""

import unittest

from src.state import (
    collapse_trigger_log_for_display,
    reconcile_executive_brief_opener,
)
from src.analysis.adr_aggregator import _evaluate_sub_status


class TestRoundTripCollapse(unittest.TestCase):
    """Issue 2 — collapse the 24h log to one NET transition per indicator."""

    def test_round_trip_is_dropped(self):
        # t3 went Not fired → Partially fired yesterday, then back today.
        log = [
            {"indicator_id": "t3_relative_strength", "indicator_name": "RS",
             "tier": 3, "old_status": "Not fired", "new_status": "Partially fired",
             "fired_at_iso": "2026-06-05T12:00:00"},
            {"indicator_id": "t3_relative_strength", "indicator_name": "RS",
             "tier": 3, "old_status": "Partially fired", "new_status": "Not fired",
             "fired_at_iso": "2026-06-06T11:00:00"},
        ]
        out = collapse_trigger_log_for_display(log)
        self.assertEqual(out, [], "a net round-trip should produce no banner")

    def test_single_transition_preserved(self):
        log = [
            {"indicator_id": "t2_eps_revisions", "indicator_name": "EPS",
             "tier": 2, "old_status": "Not fired", "new_status": "Fired",
             "fired_at_iso": "2026-06-06T10:00:00"},
        ]
        out = collapse_trigger_log_for_display(log)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["old_status"], "Not fired")
        self.assertEqual(out[0]["new_status"], "Fired")

    def test_net_transition_uses_earliest_old_and_latest_new(self):
        # Not fired → Partially fired → Fired collapses to Not fired → Fired.
        log = [
            {"indicator_id": "t1_x", "indicator_name": "X", "tier": 1,
             "old_status": "Not fired", "new_status": "Partially fired",
             "fired_at_iso": "2026-06-06T08:00:00"},
            {"indicator_id": "t1_x", "indicator_name": "X", "tier": 1,
             "old_status": "Partially fired", "new_status": "Fired",
             "fired_at_iso": "2026-06-06T11:00:00"},
        ]
        out = collapse_trigger_log_for_display(log)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["old_status"], "Not fired")
        self.assertEqual(out[0]["new_status"], "Fired")

    def test_distinct_indicators_each_kept(self):
        log = [
            {"indicator_id": "t1_a", "indicator_name": "A", "tier": 1,
             "old_status": "Not fired", "new_status": "Fired",
             "fired_at_iso": "2026-06-06T09:00:00"},
            {"indicator_id": "t1_us_adr_event", "indicator_name": "ADR", "tier": 1,
             "old_status": "Not fired", "new_status": "Partially fired",
             "fired_at_iso": "2026-06-06T11:05:00"},
        ]
        out = collapse_trigger_log_for_display(log)
        self.assertEqual(len(out), 2)
        # sorted by latest fired_at_iso
        self.assertEqual(out[0]["indicator_id"], "t1_a")
        self.assertEqual(out[1]["indicator_id"], "t1_us_adr_event")

    def test_june6_scenario_exactly(self):
        # The full observed log: t3 round-trip + ADR single move.
        log = [
            {"indicator_id": "t3_relative_strength", "indicator_name": "Memory pure-plays underperform SOX",
             "tier": 3, "old_status": "Not fired", "new_status": "Partially fired",
             "fired_at_iso": "2026-06-05T12:00:00"},
            {"indicator_id": "t3_relative_strength", "indicator_name": "Memory pure-plays underperform SOX",
             "tier": 3, "old_status": "Partially fired", "new_status": "Not fired",
             "fired_at_iso": "2026-06-06T11:00:00"},
            {"indicator_id": "t1_us_adr_event", "indicator_name": "US ADR / SPO event tracking",
             "tier": 1, "old_status": "Not fired", "new_status": "Partially fired",
             "fired_at_iso": "2026-06-06T11:05:00"},
        ]
        out = collapse_trigger_log_for_display(log)
        ids = [e["indicator_id"] for e in out]
        self.assertNotIn("t3_relative_strength", ids)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["indicator_id"], "t1_us_adr_event")


class TestExecutiveBriefOpenerGuard(unittest.TestCase):
    """Issue 1 — strip a false "no signals fired/unfired" opener."""

    def test_observed_contradiction_is_rewritten(self):
        brief = ("No signals fired or unfired today; t3_relative_strength reverted "
                 "to 'Not fired' as computed metrics show...")
        out = reconcile_executive_brief_opener(brief, ["Relative strength (t3)"])
        self.assertFalse(out.lower().startswith("no signals"))
        self.assertIn("t3_relative_strength reverted", out)
        self.assertIn("1 signal status change today", out)

    def test_clean_brief_untouched(self):
        brief = "Relative strength (t3) reverted to 'Not fired' as SK Hynix outperformed."
        self.assertEqual(reconcile_executive_brief_opener(brief, ["RS"]), brief)

    def test_no_signal_changes_leaves_brief_alone(self):
        # Even a literal "no signals fired" opener stays if nothing changed.
        brief = "No signals fired or unfired today; the tape was quiet."
        self.assertEqual(reconcile_executive_brief_opener(brief, []), brief)

    def test_overlay_only_change_is_callers_responsibility(self):
        # The helper trusts the caller to pass only tier-1-5 signal names;
        # with an empty signal list it must not rewrite an overlay-only brief.
        brief = ("No new signals fired today; one watch overlay (Competitor "
                 "dynamics) updated to 'Partially fired'.")
        self.assertEqual(reconcile_executive_brief_opener(brief, []), brief)

    def test_plural_count_phrasing(self):
        brief = "No signals fired or unfired today; two indicators moved."
        out = reconcile_executive_brief_opener(brief, ["A", "B"])
        self.assertIn("2 signal status changes today (A, B)", out)


class TestADR8AMapping(unittest.TestCase):
    """Issue 3 — a confirmed F-1 filing is at least 'Partially fired'."""

    def _status(self, current_state):
        status, _label = _evaluate_sub_status(
            "sub_8a_f1_filing", {"current_state": current_state}, "f1_filed"
        )
        return status

    def test_filed_syndicate_pending_is_partial(self):
        # THE 2026-06-06 fix: was "Not fired", now "Partially fired".
        self.assertEqual(self._status({"f1_filed": True}), "Partially fired")

    def test_filed_tier_a_is_partial(self):
        self.assertEqual(
            self._status({"f1_filed": True, "underwriter_lead_tier": "A"}),
            "Partially fired",
        )

    def test_filed_tier_b_is_partial(self):
        self.assertEqual(
            self._status({"f1_filed": True, "underwriter_lead_tier": "B"}),
            "Partially fired",
        )

    def test_filed_tier_c_only_is_fired(self):
        self.assertEqual(
            self._status({"f1_filed": True, "underwriter_lead_tier": "C"}),
            "Fired",
        )

    def test_canceled_is_fired(self):
        self.assertEqual(self._status({"f1_canceled": True}), "Fired")

    def test_not_filed_is_pre_event(self):
        self.assertEqual(self._status({}), "Pre-event")


if __name__ == "__main__":
    unittest.main()
