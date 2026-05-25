"""Tests for v3.7.4 — visual polish in section 4.

Three small changes:
  1. DHCR card's two-column chart row uses `vertical-align:middle` on both
     outer cells so the matrix chart and the spectrum bar center-align to
     each other instead of both being top-anchored.
  2. Competitor card title drops the "Indicator 7 — " prefix.
  3. ADR card title drops the "Indicator 8 — " prefix.

The DHCR card title was already correct (no prefix) and is asserted as a
regression guard. The placeholder card builder from v3.7.3 already uses the
clean titles via its `indicator_title` kwarg, so no changes needed there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Change 1: DHCR chart row — vertical center alignment
# ══════════════════════════════════════════════════════════════════════════════

class TestDhcrChartRowVerticalCenter:
    """The DHCR card's two-column chart row (matrix on left, spectrum bar on
    right) should center the two charts vertically relative to each other,
    not anchor them both at the top of their cells."""

    def test_outer_chart_row_cells_use_vertical_align_middle(self):
        """The two outer `<td>` cells holding the matrix image and the
        spectrum bar must both use `vertical-align:middle`."""
        from src.email_module.template import _build_dhcr_chart_row
        result = _build_dhcr_chart_row(
            matrix_chart_b64="iVBORw0KGgo=",    # stub base64
            score=52,
            market_zone="HBM/GDDR",
        )
        # Find the two outer cells (the ones with width:50%)
        outer_cells = re.findall(
            r"<td style='width:50%;vertical-align:([^;]+);[^']*'>",
            result,
        )
        assert outer_cells == ["middle", "middle"], (
            f"Both outer chart-row cells should be vertical-align:middle "
            f"(matrix left, spectrum bar right); got {outer_cells}"
        )

    def test_chart_row_still_works_when_matrix_absent(self):
        """When matrix_chart_b64 is None, the spectrum-bar cell still
        renders with middle alignment — the row degrades gracefully."""
        from src.email_module.template import _build_dhcr_chart_row
        result = _build_dhcr_chart_row(
            matrix_chart_b64=None,
            score=52,
            market_zone="HBM/GDDR",
        )
        # Only one outer cell expected
        outer_cells = re.findall(
            r"<td style='width:50%;vertical-align:([^;]+);[^']*'>",
            result,
        )
        assert outer_cells == ["middle"]

    def test_no_top_aligned_outer_chart_cells_remain(self):
        """Regression guard: the legacy `vertical-align:top` on outer
        cells must not creep back in."""
        text = Path("src/email_module/template.py").read_text()
        # The exact legacy strings that v3.7.4 removed
        assert "width:50%;vertical-align:top;padding-right:10px" not in text, (
            "Legacy top-aligned matrix cell pattern must not reappear"
        )
        assert "width:50%;vertical-align:top;padding-left:10px" not in text, (
            "Legacy top-aligned spectrum cell pattern must not reappear"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Change 2: Competitor card title — drop "Indicator 7 — " prefix
# ══════════════════════════════════════════════════════════════════════════════

class TestCompetitorTitleNoIndicatorPrefix:
    def _build_minimal_comp_snapshot(self) -> dict:
        return {
            "composite_status": "Partially fired",
            "composite_status_color": "#FCE5A5",
            "cumulative_eps_impact_pct": 11.5,
            "dual_threat_active": False,
            "subs": [
                {"sub_id": "7A", "status": "Not fired",
                 "estimated_eps_impact_pct": 1.0,
                 "metric_summary": "", "evidence": ""},
                {"sub_id": "7B", "status": "Partially fired",
                 "estimated_eps_impact_pct": 10.0,
                 "metric_summary": "", "evidence": ""},
                {"sub_id": "7C", "status": "Not fired",
                 "estimated_eps_impact_pct": 0.5,
                 "metric_summary": "", "evidence": ""},
                {"sub_id": "7D", "status": "Not fired",
                 "estimated_eps_impact_pct": 0.0,
                 "metric_summary": "", "evidence": ""},
            ],
            "computed_at_iso": "2026-05-25T11:00:00Z",
        }

    def test_competitor_card_h3_does_not_contain_indicator_7(self):
        """The active builder's <h3> must not contain 'Indicator 7'."""
        from src.email_module.template import _build_competitor_section4_card
        result = _build_competitor_section4_card(self._build_minimal_comp_snapshot())
        # Pull out just the h3
        match = re.search(r"<h3>.*?</h3>", result, re.DOTALL)
        assert match, "Competitor card must emit an h3 header"
        header = match.group(0)
        assert "Indicator 7" not in header, (
            f"Header should NOT contain 'Indicator 7' prefix; got: {header[:200]}"
        )

    def test_competitor_card_h3_still_says_competitor_dynamics(self):
        """The substantive part of the title — 'Competitor dynamics' —
        must still be there."""
        from src.email_module.template import _build_competitor_section4_card
        result = _build_competitor_section4_card(self._build_minimal_comp_snapshot())
        match = re.search(r"<h3>.*?</h3>", result, re.DOTALL)
        header = match.group(0)
        assert "Competitor dynamics" in header

    def test_competitor_card_h3_does_not_start_with_em_dash(self):
        """Make sure we didn't leave a stray 'em-dash space' from the
        removed prefix."""
        from src.email_module.template import _build_competitor_section4_card
        result = _build_competitor_section4_card(self._build_minimal_comp_snapshot())
        match = re.search(r"<h3>\s*(.*?)\s*<", result, re.DOTALL)
        assert match
        title_text = match.group(1).strip()
        assert not title_text.startswith("—"), (
            f"Title should not start with em-dash: '{title_text[:50]}'"
        )
        assert not title_text.startswith("-"), (
            f"Title should not start with hyphen: '{title_text[:50]}'"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Change 3: ADR card title — drop "Indicator 8 — " prefix
# ══════════════════════════════════════════════════════════════════════════════

class TestAdrTitleNoIndicatorPrefix:
    def _build_minimal_adr_snapshot(self) -> dict:
        return {
            "composite_status": "Not fired",
            "composite_status_color": "#C0DD97",
            "lifecycle_phase": "pre_event",
            "peak_coincidence_active": False,
            "subs": [
                {"sub_id": "8A", "status": "Pre-event",
                 "metric_summary": "", "evidence": ""},
                {"sub_id": "8B", "status": "Pre-event",
                 "metric_summary": "", "evidence": ""},
                {"sub_id": "8C", "status": "Pre-event",
                 "metric_summary": "", "evidence": ""},
                {"sub_id": "8D", "status": "Pre-event",
                 "metric_summary": "", "evidence": ""},
            ],
            "computed_at_iso": "2026-05-25T11:00:00Z",
            "calendar_milestones": [],
        }

    def test_adr_card_h3_does_not_contain_indicator_8(self):
        from src.email_module.template import _build_adr_section4_card
        result = _build_adr_section4_card(self._build_minimal_adr_snapshot())
        match = re.search(r"<h3>.*?</h3>", result, re.DOTALL)
        assert match, "ADR card must emit an h3 header"
        header = match.group(0)
        assert "Indicator 8" not in header, (
            f"Header should NOT contain 'Indicator 8' prefix; got: {header[:200]}"
        )

    def test_adr_card_h3_still_says_us_adr_spo_event_tracking(self):
        from src.email_module.template import _build_adr_section4_card
        result = _build_adr_section4_card(self._build_minimal_adr_snapshot())
        match = re.search(r"<h3>.*?</h3>", result, re.DOTALL)
        header = match.group(0)
        assert "US ADR / SPO event tracking" in header

    def test_adr_card_h3_does_not_start_with_em_dash(self):
        from src.email_module.template import _build_adr_section4_card
        result = _build_adr_section4_card(self._build_minimal_adr_snapshot())
        match = re.search(r"<h3>\s*(.*?)\s*<", result, re.DOTALL)
        assert match
        title_text = match.group(1).strip()
        assert not title_text.startswith("—")
        assert not title_text.startswith("-")


# ══════════════════════════════════════════════════════════════════════════════
# Regression guards: cards that were already correct stay correct
# ══════════════════════════════════════════════════════════════════════════════

class TestExistingTitlesUnchanged:
    """The DHCR card title was already correct (no 'Indicator 6' prefix).
    Make sure v3.7.4 didn't accidentally affect it."""

    def test_dhcr_card_title_does_not_contain_indicator_6_prefix(self):
        """DHCR's active card builder should render a title like
        'Decode HBM Captive Ratio (DHCR)' with no 'Indicator 6' prefix."""
        # Read the active builder's body and confirm the h3 source
        text = Path("src/email_module/template.py").read_text()
        # Find the active builder's body (between its def and the legacy one)
        # We look for the specific header line pattern
        assert "f\"<h3>Decode HBM Captive Ratio (DHCR) \"" in text, (
            "DHCR active builder should still emit the clean title h3"
        )
        # And no 'Indicator 6 —' anywhere in the active code path
        active_code = text.split("def _build_dhcr_section4_card_legacy")[0]
        assert "Indicator 6 —" not in active_code, (
            "DHCR active code path must not contain 'Indicator 6 —' prefix"
        )

    def test_placeholder_card_uses_clean_indicator_titles(self):
        """The v3.7.3 placeholder helper is invoked with clean titles
        (no 'Indicator N — ' prefix). Verify the call sites pass them."""
        text = Path("src/email_module/template.py").read_text()
        # The build_email_html body should pass these clean titles
        assert 'indicator_title="Decode HBM Captive Ratio (DHCR)"' in text
        assert 'indicator_title="Competitor dynamics"' in text
        assert 'indicator_title="US ADR / SPO event"' in text
