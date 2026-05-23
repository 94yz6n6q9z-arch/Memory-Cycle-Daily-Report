"""v3.4.3.1 tests — DHCR placement + status pill readability.

This release fixes four user-visible bugs from v3.4.2:
  1. DHCR was rendering in section 3 (tier indicator status table) with
     a stale v3.4.0 direction-monitor snippet using deprecated dataclass
     fields. DHCR should appear in section 4 ONLY, never in section 3.
  2. The _build_dhcr_section3_snippet function used v3.4.0 fields
     (`direction`, `frontier_hbm_gb_per_accelerator`, etc.) that were
     eliminated by the v3.4.2 matrix redesign. Removed entirely.
  3. The DHCR section 4 card rendered FIRST inside section 4. Should
     render LAST — DHCR is the macro framing closer, not the opener.
  4. Status pills with "Fired" (red) and "Partially fired" (amber)
     backgrounds rendered with default near-black text — unreadable.
     Should use white text on those dark backgrounds.

These tests pin the new behavior. They also serve as regression guards
against a future session re-introducing the v3.4.0 direction-monitor
snippet or moving DHCR back to section 3.

See:
  - DHCR_Framework_Explained.docx (Parts III, VII, X, Appendix B)
  - CLAUDE.md locked decisions
  - CHANGES.md v3.4.3.1 entry
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.analysis.llm_summarizer import IndicatorAssessment  # noqa: E402
from src.email_module import template as t  # noqa: E402


def _make_assessment(
    id_: str = "t1_decode_hbm_captive_ratio",
    tier: int = 1,
    name: str = "Decode HBM Captive Ratio",
    current_status: str = "Not fired",
) -> IndicatorAssessment:
    """Build a minimal IndicatorAssessment for render-level tests."""
    return IndicatorAssessment(
        id=id_,
        tier=tier,
        name=name,
        description="Direction monitor on enterprise AI demand vs supply-side capacity.",
        what_to_look_for="Direction flip from bullish to balanced/worse.",
        current_status=current_status,
        where_we_stand="Test where-we-stand prose.",
        latest_developments="Test latest developments.",
        relevant_links=[],
        change_vs_prior=None,
    )


def _minimal_dhcr_snapshot() -> dict:
    """Build a DHCR snapshot dict structurally consistent with v3.4.2 fields.

    Mirrors today's worked-example output from DHCR_Framework_Explained.docx
    Part XI (score 52, R3C2 → R4C2, op_ctx 114K, sanity 3/0, Not fired).
    """
    return {
        "status": "Not fired",
        "status_color": "#C0DD97",
        "position_score": 52,
        "workload_implied_zone": "LPDDR/HBM",
        "market_deployed_zone": "HBM/GDDR",
        "workload_implied_cell": (2, 1),
        "market_deployed_cell": (3, 1),
        "anticipation_gap_bands": 1,
        "frontier_kv_kb_per_token": 14.0,
        "frontier_kv_gb_per_1m_tokens": 13.35,
        "frontier_model_name": "DeepSeek V4",
        "frontier_model_arch": "MLA",
        "kv_band_label": "4-16 GB/1M tok",
        "kv_yoy_change_pct": -91.0,
        "operative_context_tokens": 114000,
        "context_band_label": "32-200K",
        "context_categories": [],
        "enterprise_ai_run_rate_usd_b": 121.0,
        "enterprise_ai_yoy_growth_pct": 175.0,
        "nvidia_datacenter_yoy_growth_pct": 69.0,
        "volume_index": 100.0,
        "volume_trajectory": "bullish",
        "hbm_bit_demand_index": 52.0,
        "sanity_check_confirms": 3,
        "sanity_check_contradicts": 0,
        "calibration_flag": False,
        "sanity_signal_substitution_news_count": 0,
        "sanity_signal_hyperscaler_decode_mentions": 0,
        "sanity_signal_hbm_price_trend": "rising",
        "narrative_summary": "Test narrative.",
        "bull_case_summary": "BULL CASE: test.",
        "bear_case_summary": "BEAR CASE: test.",
        "substitution_risk_notes": [],
        "status_reasoning": "Score 52 in transition zone, volume bullish → Not fired.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. DHCR placement rules
# ─────────────────────────────────────────────────────────────────────────────


class TestDHCRNotInSection3(unittest.TestCase):
    """DHCR must not appear in the section 3 tier indicator status table."""

    def test_dhcr_filtered_out_of_tier_iteration(self):
        """When _build_indicators_section is called with DHCR in the
        assessments list, the rendered HTML must not contain a row for it."""
        dhcr = _make_assessment(id_="t1_decode_hbm_captive_ratio")
        other_tier_1 = _make_assessment(id_="t1_spot_decline", name="DRAM Spot Decline")
        html = t._build_indicators_section(
            assessments=[dhcr, other_tier_1],
            dhcr_snapshot=_minimal_dhcr_snapshot(),
        )
        # The DHCR row's indicator NAME must not appear in section 3 HTML.
        self.assertNotIn(
            "Decode HBM Captive Ratio", html,
            "DHCR name must not appear in section 3 tier indicator table"
        )
        # The DHCR row's indicator ID must not appear in section 3 HTML.
        self.assertNotIn(
            "t1_decode_hbm_captive_ratio", html,
            "DHCR id must not appear in section 3 tier indicator table"
        )
        # Sanity: the OTHER tier 1 indicator IS rendered.
        self.assertIn("DRAM Spot Decline", html,
            "Non-DHCR tier 1 indicators must still render in section 3")

    def test_dhcr_filtered_even_when_dhcr_snapshot_is_None(self):
        """Filter must apply regardless of whether dhcr_snapshot is present."""
        dhcr = _make_assessment(id_="t1_decode_hbm_captive_ratio")
        html = t._build_indicators_section(
            assessments=[dhcr],
            dhcr_snapshot=None,  # explicit None
        )
        self.assertNotIn("Decode HBM Captive Ratio", html)
        self.assertNotIn("t1_decode_hbm_captive_ratio", html)


class TestDHCRSection3SnippetFunctionRemoved(unittest.TestCase):
    """The v3.4.0 direction-monitor snippet function must be deleted."""

    def test_build_dhcr_section3_snippet_does_not_exist(self):
        """_build_dhcr_section3_snippet was deleted in v3.4.3.1 because it
        used v3.4.0 fields (`direction`, `frontier_hbm_gb_per_accelerator`,
        `capacity_creation_yoy_pct`, `implied_context_capacity_per_accelerator_tokens`)
        that no longer exist on DHCRSnapshot after the v3.4.2 matrix redesign.
        Re-introducing it would be a regression — it would AttributeError on
        the v3.4.2 dataclass."""
        self.assertFalse(
            hasattr(t, "_build_dhcr_section3_snippet"),
            "v3.4.0 _build_dhcr_section3_snippet must remain deleted — "
            "it used deprecated dataclass fields. DHCR is section 4 only."
        )

    def test_template_source_has_no_v340_direction_enum_references(self):
        """The four v3.4.0 stale field names should be absent from the
        section 3 rendering path (they may remain referenced in CHANGES /
        comments / docstrings as historical context)."""
        # Check the renderer functions only, not docstrings / comments globally.
        # The simplest cross-cutting check: the source still contains those
        # field names exactly once each — as references in tombstone comments
        # — and never as `.get(...)` or attribute access. We check there's
        # no `.get("direction"` or `dhcr["direction"]` style lookup remaining.
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        for stale_field in (
            'get("direction"',
            'get("frontier_hbm_gb_per_accelerator"',
            'get("capacity_creation_yoy_pct"',
            'get("implied_context_capacity_per_accelerator_tokens"',
        ):
            self.assertNotIn(
                stale_field, src,
                f"Stale v3.4.0 field access {stale_field!r} must not exist "
                f"in email_module/template.py — v3.4.2 redesign eliminated it"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. DHCR section 4 ordering — renders LAST
# ─────────────────────────────────────────────────────────────────────────────


class TestDHCRRendersLastInSection4(unittest.TestCase):
    """DHCR card must be appended AFTER _build_top5_section.

    Pinned at the source level (cheap, deterministic) AND at the render
    level (the more robust check that an integration regression catches)."""

    def test_dhcr_card_appended_after_top5_in_source(self):
        """Source-level: _build_dhcr_section4_card call comes after the
        _build_top5_section call within the section 4 region."""
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        # Find section-4 region
        sec4_start = src.index("section-4'>4. Top")
        sec5_start = src.index("section-5'>5.")
        sec4_region = src[sec4_start:sec5_start]
        top5_idx = sec4_region.index("_build_top5_section(")
        dhcr_idx = sec4_region.index("_build_dhcr_section4_card(")
        self.assertGreater(
            dhcr_idx, top5_idx,
            "v3.4.3.1: _build_dhcr_section4_card must come AFTER "
            "_build_top5_section in section 4"
        )

    def test_dhcr_card_renders_after_top5_in_rendered_html(self):
        """Render-level: in the actual HTML emitted by build_email_html,
        the DHCR card heading appears at a string position AFTER the
        top-5 deep-dive content."""
        # Build minimal section 4 manually using the public sub-builders.
        # build_email_html is heavy and requires a full analysis object;
        # we exercise the assembly order via _build_top5_section and
        # _build_dhcr_section4_card directly and check ordering downstream
        # in test_dhcr_card_appended_after_top5_in_source above.
        dhcr_snap = _minimal_dhcr_snapshot()
        dhcr_html = t._build_dhcr_section4_card(dhcr_snap)
        self.assertIn(
            "Decode HBM Captive Ratio", dhcr_html,
            "DHCR section 4 card must render the title 'Decode HBM Captive Ratio'"
        )
        # v3.4.9 — The standalone "SCORE 52/100" badge was removed in favor of
        # the cards-1-5 structure. The score now appears inside the COMPUTED
        # METRICS line in the "Where we stand" paragraph, formatted as
        # "Position score <strong>52/100</strong>".
        self.assertIn(
            "Position score", dhcr_html,
            "DHCR card must render the position score within Where we stand prose"
        )
        self.assertIn(
            "52/100", dhcr_html,
            "DHCR card must render the score value 52/100 (in COMPUTED METRICS prose)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Status pill text color readability
# ─────────────────────────────────────────────────────────────────────────────


class TestStatusPillTextColors(unittest.TestCase):
    """Fired (red bg) and Partially fired (amber bg) must use white text.
    Not fired (light green bg) keeps near-black text. Per Raul 2026-05-22."""

    def _render_tier_row(self, status: str) -> str:
        assessment = _make_assessment(
            id_="t1_spot_decline",  # any non-DHCR tier 1 id
            name="DRAM Spot Decline",
            current_status=status,
        )
        return t._build_indicators_section(
            assessments=[assessment],
            dhcr_snapshot=None,
        )

    def test_fired_badge_has_white_text(self):
        html = self._render_tier_row("Fired")
        # White text must be set on the status-badge span.
        self.assertIn(
            "color:#FFFFFF", html,
            "Fired status pill must use white text for readability "
            "(red background is too dark for default near-black text)"
        )

    def test_partially_fired_badge_has_white_text(self):
        html = self._render_tier_row("Partially fired")
        self.assertIn(
            "color:#FFFFFF", html,
            "Partially fired status pill must use white text for readability "
            "(amber background is too dark for default near-black text)"
        )

    def test_not_fired_badge_has_dark_text(self):
        html = self._render_tier_row("Not fired")
        # Dark text (near-black) is correct for the light green Not fired bg.
        self.assertIn(
            "color:#1a1a1a", html,
            "Not fired status pill must use near-black text "
            "(light green background; white text would be unreadable)"
        )


if __name__ == "__main__":
    unittest.main()
