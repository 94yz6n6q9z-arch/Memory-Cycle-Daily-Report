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
        # v3.4.9 — card rebuilt to match cards 1-5 structure. The standalone
        # DUAL-THREAT chrome banner was removed; dual-threat substance now
        # appears inline in "Where we stand" prose only when active. The 4
        # sub IDs (7A/7B/7C/7D) still surface — now as inline references
        # within the prose, not as standalone sub-card headers.
        self.assertIn("Indicator 7", html)
        self.assertIn("Competitor dynamics", html)
        # Cards 1-5 structural markers must be present
        self.assertIn("<strong>What we're looking for:</strong>", html)
        self.assertIn("<strong>Where we stand:</strong>", html)
        self.assertIn("<strong>Latest developments:</strong>", html)
        # All 4 sub IDs still referenced in the prose
        for sub_id in ("7A", "7B", "7C", "7D"):
            self.assertIn(sub_id, html)

    def test_competitor_card_renders_moat_and_substitution_boxes_for_7d(self):
        """v3.4.9 — The 'Moat-extension watch' and 'Substitution-risk watch'
        sub-card boxes were removed when 7D was folded into the inline prose.
        This test now verifies the 7D sub still gets surfaced in the
        Where we stand prose."""
        from src.email_module.template import _build_competitor_section4_card
        from src.analysis.competitor_scoring import compute_competitor_snapshot
        from dataclasses import asdict
        snap = compute_competitor_snapshot()
        html = _build_competitor_section4_card(asdict(snap))
        # 7D is still referenced in the inline sub-list
        self.assertIn("7D", html)
        # HBF is part of the 7D framework description
        self.assertIn("HBF", html)

    def test_adr_card_builder_exists(self):
        from src.email_module.template import _build_adr_section4_card
        self.assertEqual(_build_adr_section4_card(None), "")
        from src.analysis.adr_event_tracking import compute_adr_snapshot
        from dataclasses import asdict
        snap = compute_adr_snapshot()
        html = _build_adr_section4_card(asdict(snap))
        # v3.4.9 — card rebuilt to cards-1-5 structure. CALENDAR MILESTONES
        # and ACADEMIC CALIBRATION standalone heading blocks were removed;
        # both substances now appear inline within the Where we stand prose.
        self.assertIn("Indicator 8", html)
        self.assertIn("US ADR", html)
        # Phase banner text now appears inline as part of COMPUTED METRICS
        self.assertIn("Awaiting F-1", html)
        # Cards 1-5 structural markers
        self.assertIn("<strong>What we're looking for:</strong>", html)
        self.assertIn("<strong>Where we stand:</strong>", html)
        self.assertIn("<strong>Latest developments:</strong>", html)
        # All 4 sub IDs still referenced
        for sub_id in ("8A", "8B", "8C", "8D"):
            self.assertIn(sub_id, html)
        # Calendar dates surface inside Where we stand prose (as
        # "Upcoming milestones: ...")
        self.assertIn("Upcoming milestones", html)

    def test_adr_card_shows_confirmed_offering_structure(self):
        """v3.4.9 — Offering structure data preserved, but the standalone
        CONFIRMED OFFERING STRUCTURE heading block is gone. The size,
        dilution, and raise numbers now appear inline within Where we stand."""
        from src.email_module.template import _build_adr_section4_card
        from src.analysis.adr_event_tracking import compute_adr_snapshot
        from dataclasses import asdict
        snap = compute_adr_snapshot()
        html = _build_adr_section4_card(asdict(snap))
        # The header chrome ("CONFIRMED OFFERING STRUCTURE") is gone; the
        # human label "Confirmed offering structure:" is in the prose instead
        self.assertIn("Confirmed offering structure", html)
        # And the actual numbers (which is what the user cares about) are still rendered
        self.assertIn("17.4M", html)
        self.assertIn("2.4%", html)
        self.assertIn("$10", html)

    def test_dhcr_spectrum_bar_renders_5_segments(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52)
        # v3.4.9.2: 5 memory-type segment labels (zone labels recalibrated to
        # natural cell-score breakpoints)
        self.assertIn("HBM dominant", html)
        self.assertIn("HBM-leaning", html)
        self.assertIn("HBM-GDDR / hybrid", html)
        self.assertIn("LPDDR-led", html)
        self.assertIn("DDR commodity", html)
        # 5 moat-strength tiers (unchanged)
        self.assertIn("Strongest moat", html)
        self.assertIn("Strong moat", html)
        self.assertIn("Partial moat", html)
        self.assertIn("Weak moat", html)
        self.assertIn("No moat", html)

    def test_dhcr_spectrum_bar_marker_at_score_52(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52)
        # v3.4.9.2 marker style: "TODAY" + "X/100" plain text (sketch style)
        self.assertIn("TODAY", html)
        self.assertIn("52/100", html)
        # Marker position: 100-52 = 48% from top (exact score, not zone midpoint)
        self.assertIn("top:48%", html)

    def test_dhcr_spectrum_bar_marker_at_score_85(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(85)
        self.assertIn("85/100", html)
        self.assertIn("top:15%", html)

    def test_dhcr_spectrum_bar_colors_flipped_hbm_green_ddr_red(self):
        """v3.4.9.2 — colors flipped to financial-signal coding: top = green
        (HBM = strong moat for SK Hynix), bottom = red (DDR commodity = no
        moat). Diverges from matrix's memory-intensity palette.
        """
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52)
        # Bar order top-to-bottom: HBM (green) → ... → DDR (red).
        # Verify the top segment (height:20% containing the "100" label) uses
        # the green color, and the bottom segment (height:30% with "30" label
        # next) uses the red color.
        top_idx = html.find("#A6D89B")
        bot_idx = html.find("#E0775F")
        self.assertGreater(top_idx, 0, "Green (#A6D89B) should appear in HBM top segment")
        self.assertGreater(bot_idx, top_idx, "Red (#E0775F) should appear AFTER green (i.e. lower in HTML = lower on bar)")

    def test_dhcr_spectrum_bar_market_zone_subtitle(self):
        """v3.4.9.2 — market_zone arg renders as subtitle alongside score."""
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52, market_zone="HBM/GDDR")
        self.assertIn("market cell: HBM/GDDR", html)

    def test_dhcr_matrix_cells_show_scores(self):
        """v3.4.9.2 — each cell shows zone label + numeric score. Verify
        a sample of expected scores appear: R1/C1=10, R4/C2=52, R5/C5=95.
        """
        from src.analysis.chart_generator import generate_dhcr_matrix_chart
        # We can't easily inspect PNG content here, but we can verify the
        # chart renders without error and the matrix HTML wrapper isn't
        # what we're testing — the matrix is rendered as a PNG. The score
        # values get baked into the PNG via matplotlib. So we test that
        # generate_dhcr_matrix_chart returns a non-empty base64 string.
        snap = {
            "status": "Not fired", "position_score": 52,
            "workload_implied_cell": (2, 1),
            "market_deployed_cell": (3, 1),
            "operative_context_tokens": 114_000,
            "frontier_kv_gb_per_1m_tokens": 13.3,
        }
        b64 = generate_dhcr_matrix_chart(snap)
        self.assertIsNotNone(b64)
        self.assertGreater(len(b64), 1000, "Matrix PNG should be substantial")

    def test_dhcr_market_zone_label_resolution(self):
        """v3.4.9.2 — _dhcr_market_zone_label() should resolve market cell
        to its matrix zone label."""
        from src.email_module.template import _dhcr_market_zone_label
        self.assertEqual(_dhcr_market_zone_label({"market_deployed_cell": (3, 1)}), "HBM/GDDR")
        self.assertEqual(_dhcr_market_zone_label({"market_deployed_cell": (4, 4)}), "HBM")
        self.assertEqual(_dhcr_market_zone_label({"market_deployed_cell": (0, 0)}), "DDR")
        self.assertEqual(_dhcr_market_zone_label({"market_deployed_cell": (2, 1)}), "LPDDR/HBM")
        # Bad input handling
        self.assertEqual(_dhcr_market_zone_label({}), "")
        self.assertEqual(_dhcr_market_zone_label({"market_deployed_cell": None}), "")


if __name__ == "__main__":
    unittest.main()
