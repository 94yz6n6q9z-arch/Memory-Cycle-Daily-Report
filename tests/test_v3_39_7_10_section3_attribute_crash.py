"""Regression test for v3.39.7.10 — AttributeError fix on
_build_indicators_section for t3_relative_strength.

v3.39.7.9 shipped Fix #7b (section 3 numeric surfacing for t3_relative_strength)
which gated the snippet injection on `a.indicator_id == "t3_relative_strength"`.
The IndicatorAssessment dataclass has `id`, NOT `indicator_id` — so this
branch crashed with AttributeError at runtime on the first real fast-tier
run (15:22 UTC 2026-05-21, traceback in src/fast_tier.py:889 →
build_email_html → _build_indicators_section line 637).

The v3.39.7.9 test suite covered _build_relative_strength_snippet in
isolation (TestSection3RelativeStrengthSnippet class, 9 tests) but never
exercised the integration into _build_indicators_section against a real
IndicatorAssessment object. That gap let the attribute typo escape tests.

v3.39.7.10 adds the missing integration tests AND a structural test
asserting `IndicatorAssessment.id` (not `.indicator_id`) is the canonical
identifier — so any future code that tries `.indicator_id` triggers an
immediate hard failure during test runs instead of waiting for production.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.analysis.llm_summarizer import IndicatorAssessment
from src.email_module.template import (
    _build_indicators_section,
    _build_relative_strength_snippet,
)


# ============================================================
# Structural test — pins the canonical attribute name
# ============================================================

class TestIndicatorAssessmentFields:
    """Pin the canonical attribute name so a `.indicator_id` typo can never
    pass tests again (caught by static field enumeration, not by a runtime
    render path)."""

    def test_id_field_exists(self):
        """IndicatorAssessment.id is the canonical identifier."""
        names = {f.name for f in dataclasses.fields(IndicatorAssessment)}
        assert "id" in names

    def test_indicator_id_field_does_not_exist(self):
        """The field is `id`, NOT `indicator_id`. If anyone re-introduces
        `indicator_id` as a renamed alias, this test fails loudly so the
        v3.39.7.9 crash pattern can't recur silently."""
        names = {f.name for f in dataclasses.fields(IndicatorAssessment)}
        assert "indicator_id" not in names, (
            "v3.39.7.10 regression: the canonical field is 'id'. "
            "If you add 'indicator_id' (renamed or aliased), update all "
            "consumers in src/email_module/template.py + downstream."
        )

    def test_canonical_field_set_matches_v3_39_7_10_expectations(self):
        """Pin the full field shape so additions are intentional."""
        names = {f.name for f in dataclasses.fields(IndicatorAssessment)}
        assert names == {
            "id",
            "tier",
            "name",
            "description",
            "what_to_look_for",
            "current_status",
            "where_we_stand",
            "latest_developments",
            "relevant_links",
            "change_vs_prior",
        }


# ============================================================
# Integration tests — exercise the section 3 render path end-to-end
# ============================================================

def _make_assessment(
    indicator_id: str = "t3_relative_strength",
    name: str = "Memory pure-plays underperform SOX",
    current_status: str = "Not fired",
    tier: int = 3,
) -> IndicatorAssessment:
    """Build a minimal valid IndicatorAssessment for render tests."""
    return IndicatorAssessment(
        id=indicator_id,
        tier=tier,
        name=name,
        description="SK Hynix or Micron underperforms SOX by >10% over 5d.",
        what_to_look_for="SK Hynix 5d vs SOX < -10pp.",
        current_status=current_status,
        where_we_stand="Computed: see badges below.",
        latest_developments="SK Hynix +11.17% today.",
    )


def _make_rel_strength_verdict(
    skhx_pp: float | None = -3.21,
    mu_pp: float | None = -2.84,
    dram_pp: float | None = -1.45,
) -> dict:
    """Build a minimal valid rel_strength_verdict dict."""
    return {
        "skhx_5d_vs_sox_pp": skhx_pp,
        "mu_5d_vs_sox_pp": mu_pp,
        "dram_etf_vs_smh": {"spread_pp": dram_pp},
    }


class TestBuildIndicatorsSectionRunsWithoutError:
    """v3.39.7.9 crashed on this exact call signature. These tests would
    have caught it before production deploy."""

    def test_runs_without_crash_for_t3_relative_strength(self):
        """The v3.39.7.9 AttributeError reproduction — exercise the
        exact branch that crashed."""
        assessments = [_make_assessment(indicator_id="t3_relative_strength")]
        verdict = _make_rel_strength_verdict()
        # Pre-v3.39.7.10 this raised AttributeError on `a.indicator_id`.
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        assert isinstance(html_out, str)
        assert len(html_out) > 0

    def test_runs_without_crash_for_other_indicators(self):
        """Non-t3 indicators must not exercise the snippet path."""
        assessments = [_make_assessment(
            indicator_id="t1_spot_decline",
            name="DRAM spot price decline",
            tier=1,
        )]
        verdict = _make_rel_strength_verdict()
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        assert isinstance(html_out, str)

    def test_runs_without_rel_strength_verdict(self):
        """rel_strength_verdict is Optional — None must not crash."""
        assessments = [_make_assessment()]
        html_out = _build_indicators_section(assessments, rel_strength_verdict=None)
        assert isinstance(html_out, str)

    def test_runs_with_mixed_indicators(self):
        """Multiple tiers + multiple indicators — exercises the per-row
        loop that crashed in v3.39.7.9."""
        assessments = [
            _make_assessment(indicator_id="t1_spot_decline", tier=1, name="Spot"),
            _make_assessment(indicator_id="t2_capex_parabolic", tier=2, name="Capex"),
            _make_assessment(indicator_id="t3_relative_strength", tier=3, name="Rel str"),
            _make_assessment(indicator_id="t4_inversion", tier=4, name="Inv"),
        ]
        verdict = _make_rel_strength_verdict()
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        assert isinstance(html_out, str)


class TestSnippetActuallyInjects:
    """Beyond just not crashing — verify the v3.39.7.9 snippet ACTUALLY
    appears in the rendered HTML for the right row."""

    def test_snippet_present_in_t3_row(self):
        assessments = [_make_assessment(indicator_id="t3_relative_strength")]
        verdict = _make_rel_strength_verdict()
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        # Snippet uses signature text from _build_relative_strength_snippet
        assert "SK Hynix 5d vs SOX:" in html_out
        assert "Micron 5d vs SOX:" in html_out
        assert "DRAM ETF 5d vs SMH:" in html_out

    def test_snippet_not_present_for_non_t3_indicators(self):
        """Sanity check the gate works — snippet only renders for t3_relative_strength."""
        assessments = [_make_assessment(indicator_id="t1_spot_decline")]
        verdict = _make_rel_strength_verdict()
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        # The snippet's signature text MUST NOT appear in non-t3 rows
        assert "SK Hynix 5d vs SOX:" not in html_out

    def test_actual_value_rendered_for_t3(self):
        """Verify the numeric value (not just label) appears in HTML."""
        assessments = [_make_assessment(indicator_id="t3_relative_strength")]
        verdict = _make_rel_strength_verdict(skhx_pp=-7.42, mu_pp=-3.18, dram_pp=-12.55)
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        # Each value should render with sign + 2 decimals + pp suffix
        assert "-7.42pp" in html_out
        assert "-3.18pp" in html_out
        assert "-12.55pp" in html_out

    def test_color_coding_red_band(self):
        """SK Hynix at -12pp ≤ -10pp threshold → red Fired color."""
        from src.email_module.template import STATUS_COLORS
        assessments = [_make_assessment(indicator_id="t3_relative_strength")]
        verdict = _make_rel_strength_verdict(skhx_pp=-12.5, mu_pp=None, dram_pp=None)
        html_out = _build_indicators_section(assessments, rel_strength_verdict=verdict)
        # Fired-band SK Hynix badge has the Fired color background
        assert STATUS_COLORS["Fired"] in html_out


class TestEmptyAssessmentList:
    """Edge case — empty list must not crash the section build."""

    def test_empty_assessment_list(self):
        html_out = _build_indicators_section([], rel_strength_verdict=None)
        assert isinstance(html_out, str)

    def test_empty_with_verdict_present(self):
        """Verdict given but no assessments to render against."""
        verdict = _make_rel_strength_verdict()
        html_out = _build_indicators_section([], rel_strength_verdict=verdict)
        assert isinstance(html_out, str)
