"""Tests for v3.39.7.5 — executive_brief paragraph + anchor link substitution.

Locks the contract that:
1. The Sonnet-emitted token [→§N] becomes a clickable HTML anchor to #section-N
2. Multiple anchors in one paragraph all render
3. HTML escaping in the brief body doesn't break the substitution
4. Empty/missing executive_brief renders nothing (graceful)
5. URGENT banner is no longer rendered even when fields are set on dataclass
"""

from __future__ import annotations

import unittest

from src.analysis.llm_summarizer import (
    BriefAnalysis,
    CaseScenario,
    ExecutiveSummary,
    IndicatorAssessment,
)
from src.email_module.template import _build_executive_summary


def _scenario(prob: float, ret: float = 18.0, months: float = 4.0) -> CaseScenario:
    return CaseScenario(
        probability_pct=prob,
        peak_months_away=months,
        peak_window_start="August 2026",
        peak_window_end="October 2026",
        return_to_peak_pct=ret,
    )


def _exec_summary(
    brief: str = "",
    triggers: list = None,
    urgent: bool = False,
    urgent_text: str = "",
) -> ExecutiveSummary:
    return ExecutiveSummary(
        current_phase=4,
        current_phase_label="Boom / Euphoria (late stage)",
        tier_status_summary="T1: 3/6 fired | T2: 2/7 fired | T3: 0/5 fired | T4: 0/4 fired",
        new_triggers_today=triggers or [],
        base_case=_scenario(50, 18.0),
        bull_case=_scenario(25, 55.0, 12.0),
        bear_case=_scenario(25, -15.0, 0.0),
        delta_from_prior_text="Base case peak unchanged.",
        urgent_attention_needed=urgent,
        urgent_attention_text=urgent_text,
        executive_brief=brief,
    )


class TestExecutiveBriefRendering(unittest.TestCase):
    """v3.39.7.5: consolidated executive_brief paragraph + anchor links."""

    def test_single_anchor_link_substituted(self):
        """The literal [→§1] becomes a clickable HTML link to #section-1."""
        es = _exec_summary(brief="ASML +5.5% [→§1] led equipment names higher.")
        html = _build_executive_summary(es)
        self.assertIn("<a href='#section-1' class='anchor-link'>§1</a>", html)
        # The literal source token must not survive in output
        self.assertNotIn("[→§1]", html)

    def test_multiple_anchors_all_substituted(self):
        """Many anchors in one paragraph all become clickable."""
        es = _exec_summary(brief=(
            "Signal fires today on [→§3] include t1_demand_destruction; "
            "see the [→§4] deep dive. ASML +5.5% [→§1] and key news [→§5]."
        ))
        html = _build_executive_summary(es)
        for n in (1, 3, 4, 5):
            self.assertIn(f"<a href='#section-{n}'", html)
            self.assertIn(f">§{n}</a>", html)
        self.assertNotIn("[→§", html)  # no unsubstituted tokens

    def test_brief_text_html_escaped(self):
        """Body text with HTML special chars escapes; anchors still work."""
        es = _exec_summary(brief="Margin <50% on capex [→§4] & risk now.")
        html = _build_executive_summary(es)
        # < > & escaped
        self.assertIn("&lt;50%", html)
        self.assertIn("&amp; risk", html)
        # Anchor still substituted
        self.assertIn("<a href='#section-4'", html)

    def test_empty_brief_renders_nothing(self):
        """No executive_brief → no .exec-brief div appears at all."""
        es = _exec_summary(brief="")
        html = _build_executive_summary(es)
        self.assertNotIn("exec-brief", html)

    def test_urgent_banner_NOT_rendered_in_exec_summary(self):
        """v3.39.7.5: URGENT banner is killed from build_executive_summary.

        The banner is rendered (or not) in the outer build_email_html — this
        test confirms _build_executive_summary itself never emits it.
        """
        es = _exec_summary(
            brief="Normal brief.",
            urgent=True,
            urgent_text="This is the urgent text",
        )
        html = _build_executive_summary(es)
        self.assertNotIn("URGENT", html)
        self.assertNotIn("urgent-banner", html)
        self.assertNotIn("urgent text", html.lower())

    def test_phase_line_unchanged(self):
        """Phase + tier counts line still present at top (v3.39.7.4 format)."""
        es = _exec_summary(brief="Body.")
        html = _build_executive_summary(es)
        self.assertIn("Phase", html)
        self.assertIn("4", html)
        self.assertIn("Boom / Euphoria", html)
        self.assertIn("T1: 3/6", html)

    def test_trigger_callout_when_triggers_present(self):
        """new_triggers_today list still renders as the yellow callout."""
        es = _exec_summary(
            brief="Today's brief body.",
            triggers=[
                {"tier": 1, "indicator_id": "t1_demand_destruction",
                 "indicator_name": "PC/Smartphone demand cuts",
                 "description": "Status changed from 'Not fired' to 'Fired'."},
            ],
        )
        html = _build_executive_summary(es)
        self.assertIn("New trigger event on Tier 1", html)
        self.assertIn("PC/Smartphone demand cuts", html)

    def test_no_trigger_callout_shows_quiet_text(self):
        """Empty new_triggers_today → 'No new trigger events today.' line."""
        es = _exec_summary(brief="Body.", triggers=[])
        html = _build_executive_summary(es)
        self.assertIn("No new trigger events today", html)

    def test_section_anchor_link_class_present_for_styling(self):
        """Rendered anchor has the .anchor-link class so CSS picks it up."""
        es = _exec_summary(brief="See [→§3].")
        html = _build_executive_summary(es)
        self.assertIn("class='anchor-link'", html)


if __name__ == "__main__":
    unittest.main()
