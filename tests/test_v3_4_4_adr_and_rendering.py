"""v3.4.4 tests — Indicator 8 (US ADR / SPO event tracking) + section 4 rendering.

Validates:
  - Pre-event state renders correctly (today's bootstrap)
  - Phase transitions (pre_event → f1_filed → roadshow → pricing → trading → completed | canceled)
  - Canceled F-1 = Fired (per Raul May 22 framework decision)
  - Peak-coincidence escalation when ADR completion + late Phase 4
  - Section 4 renders 8 cards in correct order
  - DHCR spectrum bar renders 5 segments + score marker at correct position
"""

import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.adr_event_tracking import (  # noqa: E402
    compute_adr_snapshot,
    cycle_phase_4_late_from_indicators,
    STATUS_NOT_FIRED,
    STATUS_PARTIALLY_FIRED,
    STATUS_FIRED,
    STATUS_PRE_EVENT,
    PHASE_PRE_EVENT,
    PHASE_F1_FILED,
    PHASE_ROADSHOW,
    PHASE_PRICING,
    PHASE_TRADING,
    PHASE_COMPLETED,
    PHASE_CANCELED,
)


def _make_adr_state(phase: str = PHASE_PRE_EVENT, sub_statuses: dict = None) -> dict:
    """v3.4.4.1: build the STATE dict (state/adr_state.json equivalent).
    lifecycle_phase at top level (not nested). Pair with _make_adr_thresholds()."""
    sub_statuses = sub_statuses or {}
    return {
        "_metadata": {"schema_version": "2.0", "last_aggregator_run": "2026-05-22T12:00:00Z"},
        "lifecycle_phase": phase,
        "composite_status": "Not fired",
        "composite_status_reason": "Test reason",
        "peak_coincidence_active": False,
        "sub_8a_f1_filing": {
            "current_status": sub_statuses.get("8A", STATUS_PRE_EVENT),
            "status_pill_label": "Awaiting F-1 — expected June-July 2026",
            "current_state": {"f1_filed": phase != PHASE_PRE_EVENT, "f1_canceled": phase == PHASE_CANCELED},
            "evidence_log": [],
        },
        "sub_8b_roadshow_demand": {
            "current_status": sub_statuses.get("8B", STATUS_PRE_EVENT),
            "status_pill_label": "Awaiting roadshow window",
            "current_state": {},
            "evidence_log": [],
        },
        "sub_8c_pricing_day": {
            "current_status": sub_statuses.get("8C", STATUS_PRE_EVENT),
            "status_pill_label": "Awaiting 424B pricing",
            "current_state": {},
            "evidence_log": [],
        },
        "sub_8d_insider_sales_in_window": {
            "current_status": sub_statuses.get("8D", STATUS_PRE_EVENT),
            "status_pill_label": "Monitoring Form 4",
            "current_state": {"insider_sales_in_window": [], "insider_sales_total_usd_m_in_window": 0},
            "evidence_log": [],
        },
    }


def _make_adr_thresholds() -> dict:
    """v3.4.4.1: build the THRESHOLDS dict (data/adr_event_tracking.yaml equivalent)."""
    return {
        "schema_version": "2.0",
        "sub_8a_f1_filing": {"display_name": "F-1 filing watch", "sources": []},
        "sub_8b_roadshow_demand": {"display_name": "Roadshow demand quality", "sources": []},
        "sub_8c_pricing_day": {"display_name": "Pricing-day execution", "sources": []},
        "sub_8d_insider_sales_in_window": {"display_name": "Insider participation", "sources": []},
        "calendar_milestones": [],
        "confirmed_offering_structure": {
            "new_shares_issued_count_millions_est": 17.4,
            "share_capital_pct_dilution_est": 2.4,
            "estimated_raise_usd_b_central": 10.0,
        },
        "academic_calibration": {
            "mechanical_drag_pct_range": "-4 to -9",
            "fade_window_months": "1 to 3",
            "citations": ["Doidge-Karolyi-Stulz 2004"],
            "reference_events": [],
        },
    }


class TestADRBootstrapState(unittest.TestCase):
    """Today's YAML bootstrap should produce pre-event composite."""

    def test_today_pre_event(self):
        snap = compute_adr_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.lifecycle_phase, PHASE_PRE_EVENT)

    def test_today_composite_is_not_fired_pre_event(self):
        snap = compute_adr_snapshot()
        # Pre-event composite status is Not fired (rendered with Pre-event pill in template)
        self.assertEqual(snap.composite_status, STATUS_NOT_FIRED)

    def test_today_all_subs_pre_event(self):
        snap = compute_adr_snapshot()
        for sub in snap.subs:
            self.assertEqual(sub.status, STATUS_PRE_EVENT)

    def test_today_peak_coincidence_inactive(self):
        snap = compute_adr_snapshot()
        self.assertFalse(snap.peak_coincidence_active)

    def test_today_calendar_has_4_milestones(self):
        snap = compute_adr_snapshot()
        self.assertEqual(len(snap.calendar), 4)


class TestPhaseTransitions(unittest.TestCase):

    def test_canceled_f1_is_fired(self):
        """Per Raul May 22 framework decision: canceled F-1 = Fired."""
        state = _make_adr_state(phase=PHASE_CANCELED)
        snap = compute_adr_snapshot(thresholds=_make_adr_thresholds(), state=state)
        self.assertEqual(snap.composite_status, STATUS_FIRED)
        self.assertIn("canceled", snap.composite_reasoning.lower())

    def test_f1_filed_worst_of_subs(self):
        """Post-F-1, composite = worst-of subs."""
        state = _make_adr_state(
            phase=PHASE_F1_FILED,
            sub_statuses={"8A": STATUS_PARTIALLY_FIRED}
        )
        snap = compute_adr_snapshot(thresholds=_make_adr_thresholds(), state=state)
        self.assertEqual(snap.composite_status, STATUS_PARTIALLY_FIRED)

    def test_pricing_fired_sub_escalates(self):
        state = _make_adr_state(
            phase=PHASE_PRICING,
            sub_statuses={"8C": STATUS_FIRED}
        )
        snap = compute_adr_snapshot(thresholds=_make_adr_thresholds(), state=state)
        self.assertEqual(snap.composite_status, STATUS_FIRED)


class TestPeakCoincidence(unittest.TestCase):
    """Completion + late Phase 4 → Fired regardless of sub statuses."""

    def test_completed_with_phase4_late_escalates_to_fired(self):
        state = _make_adr_state(phase=PHASE_COMPLETED)
        snap = compute_adr_snapshot(thresholds=_make_adr_thresholds(), state=state, cycle_phase_4_late=True)
        self.assertEqual(snap.composite_status, STATUS_FIRED)
        self.assertTrue(snap.peak_coincidence_active)
        self.assertIn("peak-coincidence", snap.composite_reasoning.lower())

    def test_completed_without_phase4_late_no_escalation(self):
        state = _make_adr_state(phase=PHASE_COMPLETED)
        snap = compute_adr_snapshot(thresholds=_make_adr_thresholds(), state=state, cycle_phase_4_late=False)
        # All subs pre-event/not-fired + no peak coincidence
        self.assertEqual(snap.composite_status, STATUS_NOT_FIRED)
        self.assertFalse(snap.peak_coincidence_active)

    def test_pre_event_no_peak_coincidence_even_in_late_phase4(self):
        """Pre-event ADR doesn't activate peak coincidence — the trigger
        requires an actual completion event."""
        state = _make_adr_state(phase=PHASE_PRE_EVENT)
        snap = compute_adr_snapshot(thresholds=_make_adr_thresholds(), state=state, cycle_phase_4_late=True)
        self.assertFalse(snap.peak_coincidence_active)


class TestPhase4LateHelper(unittest.TestCase):

    def test_dhcr_warning_band_triggers_phase4_late(self):
        dhcr = {"position_score": 40}
        self.assertTrue(cycle_phase_4_late_from_indicators(dhcr_snapshot=dhcr))

    def test_dhcr_above_warning_no_phase4_late(self):
        dhcr = {"position_score": 60}
        self.assertFalse(cycle_phase_4_late_from_indicators(dhcr_snapshot=dhcr))


class TestSectionRenderingV3_4_4(unittest.TestCase):
    """Section 4 renders 8 cards in the right order; cards filtered from section 3."""

    def test_section_3_filter_excludes_all_three_section4_only_indicators(self):
        # v3.4.7 — SECTION_4_ONLY_INDICATORS canonical definition lives in
        # src/config.py (lifted out of template.py so the count sites
        # in llm_summarizer.py and chart_generator.py can share it).
        # template.py still imports and uses it.
        from src.config import SECTION_4_ONLY_INDICATORS
        self.assertEqual(SECTION_4_ONLY_INDICATORS, frozenset({
            "t1_decode_hbm_captive_ratio",
            "t1_competitor_dynamics",
            "t1_us_adr_event",
        }))
        # template.py imports and references the constant
        src = (Path(__file__).resolve().parents[1] / "src/email_module/template.py").read_text()
        self.assertIn("SECTION_4_ONLY_INDICATORS", src)
        self.assertIn("from src.config import SECTION_4_ONLY_INDICATORS", src)

    def test_competitor_card_builder_exists(self):
        from src.email_module.template import _build_competitor_section4_card
        # None input safe-fails
        self.assertEqual(_build_competitor_section4_card(None), "")
        # Real snapshot renders
        from src.analysis.competitor_scoring import compute_competitor_snapshot
        from dataclasses import asdict
        snap = compute_competitor_snapshot()
        html = _build_competitor_section4_card(asdict(snap))
        self.assertIn("Indicator 7", html)
        self.assertIn("Competitor dynamics", html)
        self.assertIn("DUAL-THREAT", html)
        # All 4 sub-cards rendered
        for sub_id in ("7A", "7B", "7C", "7D"):
            self.assertIn(sub_id, html)

    def test_competitor_card_renders_moat_and_substitution_boxes_for_7d(self):
        from src.email_module.template import _build_competitor_section4_card
        from src.analysis.competitor_scoring import compute_competitor_snapshot
        from dataclasses import asdict
        snap = compute_competitor_snapshot()
        html = _build_competitor_section4_card(asdict(snap))
        self.assertIn("Moat-extension watch", html)
        self.assertIn("Substitution-risk watch", html)

    def test_adr_card_builder_exists(self):
        from src.email_module.template import _build_adr_section4_card
        self.assertEqual(_build_adr_section4_card(None), "")
        from src.analysis.adr_event_tracking import compute_adr_snapshot
        from dataclasses import asdict
        snap = compute_adr_snapshot()
        html = _build_adr_section4_card(asdict(snap))
        self.assertIn("Indicator 8", html)
        self.assertIn("US ADR", html)
        self.assertIn("Awaiting F-1", html)
        # All 4 sub-cards rendered
        for sub_id in ("8A", "8B", "8C", "8D"):
            self.assertIn(sub_id, html)
        # Calendar rendered
        self.assertIn("CALENDAR MILESTONES", html)
        # Academic calibration prose rendered
        self.assertIn("ACADEMIC CALIBRATION", html)

    def test_adr_card_shows_confirmed_offering_structure(self):
        from src.email_module.template import _build_adr_section4_card
        from src.analysis.adr_event_tracking import compute_adr_snapshot
        from dataclasses import asdict
        snap = compute_adr_snapshot()
        html = _build_adr_section4_card(asdict(snap))
        self.assertIn("CONFIRMED OFFERING STRUCTURE", html)
        self.assertIn("17.4M", html)
        self.assertIn("2.4%", html)
        self.assertIn("$10", html)

    def test_dhcr_spectrum_bar_renders_5_segments(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52)
        # 5 memory types
        self.assertIn("HBM pure", html)
        self.assertIn("HBM-GDDR", html)
        self.assertIn("LPDDR / HBM hybrid", html)
        self.assertIn("LPDDR-led", html)
        self.assertIn("DDR commodity", html)
        # 5 moat-strength tiers
        self.assertIn("Strongest moat", html)
        self.assertIn("Strong moat", html)
        self.assertIn("Partial moat", html)
        self.assertIn("Weak moat", html)
        self.assertIn("No moat", html)

    def test_dhcr_spectrum_bar_marker_at_score_52(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52)
        # Marker text "today 52"
        self.assertIn("today 52", html)
        # Marker position: 100-52 = 48% from top
        self.assertIn("top:48%", html)

    def test_dhcr_spectrum_bar_marker_at_score_85(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(85)
        self.assertIn("today 85", html)
        self.assertIn("top:15%", html)


if __name__ == "__main__":
    unittest.main()
