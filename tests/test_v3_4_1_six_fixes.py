"""Tests for v3.4.1 — UI fixes + Reversed eradication + length caps.

Five fixes addressed:
  1. Trigger event banner color in exec summary varies by destination status
  2. Section 3 change-flag reverts to plain text (no colored background)
  3. "Reversed" eradicated three ways: VALID_STATUSES gate, legacy migration,
     render-time scrub, Sonnet prompt forbidden vocabulary
  4. Executive brief 250-word hard cap (was unbounded, hit 411 words May 21)
  5. where_we_stand 120-word hard cap + defensive render-time fallback to
     Layer 2 verdict when Sonnet emits empty field (max_tokens truncation)
"""

from __future__ import annotations

import unittest
from dataclasses import asdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# Fix 1 — Trigger event banner color by destination
# ============================================================

class TestTriggerBannerColorByDestination(unittest.TestCase):
    """Banner background color must match the DESTINATION status of the
    transition (where the indicator is going), not a hardcoded yellow."""

    def _build_es_with_trigger(self, description: str):
        from src.analysis.llm_summarizer import ExecutiveSummary, CaseScenario
        return ExecutiveSummary(
            current_phase=4, current_phase_label="Boom",
            tier_status_summary="T1: 4/6 fired",
            new_triggers_today=[{
                "tier": 2, "indicator_id": "t2_x", "indicator_name": "Test indicator",
                "description": description,
            }],
            base_case=CaseScenario(probability_pct=50, peak_months_away=4,
                peak_window_start="Q3 2026", peak_window_end="Q4 2026", return_to_peak_pct=18),
            bull_case=CaseScenario(probability_pct=25, peak_months_away=12,
                peak_window_start="Q1 2027", peak_window_end="Q2 2027", return_to_peak_pct=55),
            bear_case=CaseScenario(probability_pct=25, peak_months_away=0,
                peak_window_start="now", peak_window_end="now", return_to_peak_pct=-15),
            delta_from_prior_text="", urgent_attention_needed=False, urgent_attention_text="",
        )

    def test_transition_to_fired_renders_red(self):
        from src.email_module.template import _build_executive_summary
        es = self._build_es_with_trigger("Status changed from 'Not fired' to 'Fired'.")
        html = _build_executive_summary(es)
        self.assertIn("#B91C1C", html, "Transition to 'Fired' must render red background")
        self.assertIn("color:#FFFFFF", html, "White text on red background for readability")

    def test_transition_to_not_fired_renders_green(self):
        from src.email_module.template import _build_executive_summary
        es = self._build_es_with_trigger("Status changed from 'Fired' to 'Not fired'.")
        html = _build_executive_summary(es)
        self.assertIn("#C0DD97", html, "Transition to 'Not fired' must render green background")

    def test_transition_to_partially_fired_renders_yellow(self):
        from src.email_module.template import _build_executive_summary
        es = self._build_es_with_trigger("Status changed from 'Not fired' to 'Partially fired'.")
        html = _build_executive_summary(es)
        self.assertIn("#FAC775", html, "Transition to 'Partially fired' must render yellow background")

    def test_label_format_is_caps_with_emdash(self):
        from src.email_module.template import _build_executive_summary
        es = self._build_es_with_trigger("Status changed from 'Not fired' to 'Fired'.")
        html = _build_executive_summary(es)
        self.assertIn("NEW TRIGGER EVENT", html)
        self.assertIn("TIER 2", html)


# ============================================================
# Fix 2 — Section 3 change-flag plain text
# ============================================================

class TestChangeFlagPlainText(unittest.TestCase):

    def test_change_flag_no_colored_background_in_template(self):
        """Source code inspection — the v3.39.7.9 dynamic background was reverted."""
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        self.assertNotIn('#FFFFFF" if a.current_status == "Fired"', src,
            "v3.39.7.9 white-text-on-red conditional must be removed")

    def test_change_flag_uses_muted_color_v3_4_1(self):
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        self.assertIn("v3.4.1 Fix 2", src,
            "v3.4.1 Fix 2 comment marker must be present")
        self.assertIn("color:#555", src,
            "change-flag must use muted #555 plain-text color")

    def test_changed_prefix_present_in_render(self):
        """The 'CHANGED:' prefix label is still rendered, just without
        the colored background."""
        from src.email_module.template import _build_indicators_section
        from src.analysis.llm_summarizer import IndicatorAssessment

        a = IndicatorAssessment(
            id="t3_x", tier=3, name="Test", description="desc",
            what_to_look_for="look", current_status="Fired",
            change_vs_prior="status moved from Not fired to Fired",
            where_we_stand="prose", latest_developments="recent",
        )
        html = _build_indicators_section([a])
        self.assertIn("CHANGED:", html)
        # Verify no colored background span wraps the CHANGED text
        self.assertNotIn("class='change-flag' style='background:#B91C1C", html)


# ============================================================
# Fix 3 — Reversed eradication
# ============================================================

class TestReversedEradication(unittest.TestCase):

    def test_valid_statuses_excludes_reversed(self):
        from src.state import VALID_STATUSES
        self.assertNotIn("Reversed", VALID_STATUSES,
            "VALID_STATUSES must not include 'Reversed' in v3.4.1+")
        self.assertEqual(VALID_STATUSES, {"Fired", "Partially fired", "Not fired"})

    def test_legacy_status_migration_map_exists(self):
        from src.state import LEGACY_STATUS_MIGRATIONS
        self.assertEqual(LEGACY_STATUS_MIGRATIONS.get("Reversed"), "Not fired")

    def test_render_time_scrub_in_trigger_banner(self):
        """If 'Reversed' slips through to render, the trigger banner must
        substitute 'Not fired'."""
        from src.analysis.llm_summarizer import ExecutiveSummary, CaseScenario
        from src.email_module.template import _build_executive_summary

        es = ExecutiveSummary(
            current_phase=4, current_phase_label="Boom",
            tier_status_summary="T1: 4/6 fired",
            new_triggers_today=[{
                "tier": 2, "indicator_id": "t2_x", "indicator_name": "Some indicator",
                "description": "Status changed from 'Fired' to 'Reversed'.",
            }],
            base_case=CaseScenario(probability_pct=50, peak_months_away=4,
                peak_window_start="Q3", peak_window_end="Q4", return_to_peak_pct=18),
            bull_case=CaseScenario(probability_pct=25, peak_months_away=12,
                peak_window_start="Q1", peak_window_end="Q2", return_to_peak_pct=55),
            bear_case=CaseScenario(probability_pct=25, peak_months_away=0,
                peak_window_start="now", peak_window_end="now", return_to_peak_pct=-15),
            delta_from_prior_text="", urgent_attention_needed=False, urgent_attention_text="",
        )
        html = _build_executive_summary(es)
        self.assertNotIn("Reversed", html, "Reversed must be scrubbed at render")
        self.assertIn("Not fired", html)

    def test_render_time_scrub_in_change_flag(self):
        """If change_vs_prior contains 'Reversed', the section 3 change-flag
        must substitute 'Not fired'."""
        from src.email_module.template import _build_indicators_section
        from src.analysis.llm_summarizer import IndicatorAssessment

        a = IndicatorAssessment(
            id="t2_x", tier=2, name="Test", description="desc",
            what_to_look_for="look", current_status="Not fired",
            change_vs_prior="Status moved from 'Fired' to 'Reversed' per Layer 2",
            where_we_stand="prose", latest_developments="recent",
        )
        html = _build_indicators_section([a])
        self.assertNotIn("Reversed", html, "Reversed in change_vs_prior must be scrubbed")
        self.assertIn("Not fired", html)

    def test_sonnet_prompt_includes_forbidden_vocabulary_rule(self):
        """Prompt must explicitly forbid 'Reversed' so Sonnet stops emitting it."""
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("FORBIDDEN VOCABULARY", src,
            "Sonnet prompt must contain a FORBIDDEN VOCABULARY rule")
        self.assertIn("Reversed", src,
            "Forbidden vocabulary rule must mention 'Reversed'")

    def test_legacy_migration_on_evidence_store_load(self):
        """If state/indicator_evidence.json has any entries with
        current_status='Reversed', load_indicator_evidence_store migrates
        them to 'Not fired' in memory."""
        import json
        import tempfile
        from src.state import load_indicator_evidence_store

        # Build a tmp evidence store with a 'Reversed' entry
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({
                "indicators": {
                    "t2_legacy_test": {
                        "current_status": "Reversed",
                        "fired_since": "2026-01-01",
                        "last_updated": "2026-05-21",
                        "evidence_log": [],
                    }
                }
            }, f)
            tmp_path = Path(f.name)

        try:
            store = load_indicator_evidence_store(path=tmp_path)
            self.assertIn("t2_legacy_test", store)
            self.assertEqual(store["t2_legacy_test"].current_status, "Not fired",
                "Legacy 'Reversed' status must be migrated to 'Not fired' on load")
        finally:
            tmp_path.unlink()


# ============================================================
# Fix 4 — Executive brief 250-word hard cap
# ============================================================

class TestExecBrief250WordCap(unittest.TestCase):

    def test_prompt_contains_250_word_hard_cap(self):
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("HARD CAP 250 WORDS", src,
            "Sonnet prompt must contain the explicit 250-word cap for exec brief")

    def test_prompt_explains_compress_not_cut(self):
        """The cap must be framed as 'compress, don't cut' so Sonnet
        doesn't sacrifice content."""
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("compress, don't cut", src,
            "Sonnet prompt must instruct compression, not content cutting")

    def test_prompt_provides_self_check_guidance(self):
        """Sonnet should know HOW to trim — specific guidance, not vague."""
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("Self-check before emitting", src,
            "Sonnet prompt must include a self-check step with concrete trim guidance")


# ============================================================
# Fix 5 — where_we_stand 120-word cap + fallback
# ============================================================

class TestWhereWeStand120WordCap(unittest.TestCase):

    def test_prompt_contains_120_word_hard_cap(self):
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("HARD CAP: 120 WORDS per where_we_stand", src,
            "Sonnet prompt must contain the explicit 120-word cap per where_we_stand")

    def test_prompt_includes_never_empty_rule(self):
        """The May 21 empty t2_capex_parabolic was the trigger — Sonnet
        must know empty is forbidden, with explicit fallback guidance."""
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("NEVER LEAVE where_we_stand EMPTY", src,
            "Sonnet prompt must contain a never-empty rule for where_we_stand")
        self.assertIn("t2_capex_parabolic", src,
            "Prompt must include the canonical never-empty example for t2_capex_parabolic")


class TestWhereWeStandFallback(unittest.TestCase):
    """Render-time defensive fallback when Sonnet emits empty where_we_stand."""

    def _build_assessment(self, where_we_stand: str):
        from src.analysis.llm_summarizer import IndicatorAssessment
        return IndicatorAssessment(
            id="t2_capex_parabolic", tier=2,
            name="Capex announcements parabolic",
            description="Memory makers' and hyperscalers' capex growing parabolic.",
            what_to_look_for="Big Three combined capex >2x 2024.",
            current_status="Fired",
            change_vs_prior="No change",
            where_we_stand=where_we_stand,
            latest_developments="NVIDIA Q1 FY27 print at $81.6B.",
        )

    def test_empty_where_we_stand_falls_back_to_layer2_verdict(self):
        """When Sonnet's where_we_stand is empty, render-time fallback
        pulls from Layer 2 verdict so the cell isn't blank."""
        from src.email_module.template import _build_indicators_section

        assessment = self._build_assessment(where_we_stand="")
        layer2_by_id = {
            "t2_capex_parabolic": {
                "signal_id": "t2_capex_parabolic",
                "consensus_direction": (
                    "Fired since 2025 — $725B aggregate 2026 hyperscaler capex "
                    "confirmed via Q4 2025 earnings. NVIDIA $81.6B Q1 FY27 + "
                    "$1T cumulative hyperscaler capex by 2027 forecast (Tom's Hardware) "
                    "reinforces parabolic trend."
                ),
                "confidence_level": "high",
            }
        }
        html = _build_indicators_section([assessment], signal_reasonings_by_id=layer2_by_id)
        # The fallback marker must appear
        self.assertIn("Sonnet output truncated", html,
            "Empty where_we_stand must trigger Layer 2 fallback marker")
        # Layer 2 prose must appear in the rendered cell
        self.assertIn("$725B", html)
        self.assertIn("confidence: high", html)

    def test_short_where_we_stand_triggers_fallback(self):
        """A sub-30-character where_we_stand also triggers fallback
        (defensive against partial truncation, not just total emptiness)."""
        from src.email_module.template import _build_indicators_section

        assessment = self._build_assessment(where_we_stand="See section 4.")  # 14 chars
        layer2_by_id = {
            "t2_capex_parabolic": {
                "signal_id": "t2_capex_parabolic",
                "consensus_direction": "Detailed Layer 2 verdict text with rich evidence.",
                "confidence_level": "high",
            }
        }
        html = _build_indicators_section([assessment], signal_reasonings_by_id=layer2_by_id)
        self.assertIn("Sonnet output truncated", html)
        self.assertIn("Layer 2 verdict text", html)

    def test_long_where_we_stand_no_fallback(self):
        """Healthy where_we_stand (>=30 chars) must NOT trigger fallback —
        Sonnet's text is rendered as-is."""
        from src.email_module.template import _build_indicators_section

        rich_text = ("Fired since 2025: $725B aggregate hyperscaler capex confirmed "
                     "via earnings, NVIDIA $81.6B Q1 FY27 reinforces parabolic trend.")
        assessment = self._build_assessment(where_we_stand=rich_text)
        layer2_by_id = {
            "t2_capex_parabolic": {
                "signal_id": "t2_capex_parabolic",
                "consensus_direction": "Should NOT appear in output.",
                "confidence_level": "high",
            }
        }
        html = _build_indicators_section([assessment], signal_reasonings_by_id=layer2_by_id)
        self.assertNotIn("Sonnet output truncated", html,
            "Healthy where_we_stand must not trigger fallback")
        self.assertNotIn("Should NOT appear in output", html)
        self.assertIn("$725B aggregate hyperscaler", html)

    def test_no_layer2_data_keeps_original_behavior(self):
        """If signal_reasonings_by_id not provided or doesn't contain this
        indicator, the cell renders Sonnet's text as-is (backward compatible
        with v3.4.0 callers that don't thread the kwarg)."""
        from src.email_module.template import _build_indicators_section

        assessment = self._build_assessment(where_we_stand="")
        html = _build_indicators_section([assessment])  # no kwarg
        self.assertNotIn("Sonnet output truncated", html,
            "Without Layer 2 data, must not synthesize a fallback marker")


# ============================================================
# Backward compatibility — build_email_html signature
# ============================================================

class TestBuildEmailHtmlBackcompat(unittest.TestCase):

    def test_signal_reasonings_by_id_is_optional_kwarg(self):
        """v3.4.1 added the kwarg but it must remain optional so v3.4.0
        callers continue to work."""
        from src.email_module.template import build_email_html
        from src.analysis.llm_summarizer import (
            BriefAnalysis, ExecutiveSummary, CaseScenario,
        )
        es = ExecutiveSummary(
            current_phase=4, current_phase_label="Boom",
            tier_status_summary="T1: 4/6", new_triggers_today=[],
            base_case=CaseScenario(probability_pct=50, peak_months_away=4,
                peak_window_start="Q3", peak_window_end="Q4", return_to_peak_pct=18),
            bull_case=CaseScenario(probability_pct=25, peak_months_away=12,
                peak_window_start="Q1", peak_window_end="Q2", return_to_peak_pct=55),
            bear_case=CaseScenario(probability_pct=25, peak_months_away=0,
                peak_window_start="now", peak_window_end="now", return_to_peak_pct=-15),
            delta_from_prior_text="", urgent_attention_needed=False, urgent_attention_text="",
        )
        analysis = BriefAnalysis(
            executive_summary=es, big_movers=[], indicator_assessments=[],
            top_5_deep_dives=[], market_summary="t", key_news_summary=[],
            cycle_position_call="t",
        )
        # No kwarg — must not raise
        html = build_email_html(analysis=analysis, performance=[], chart_base64="")
        self.assertGreater(len(html), 100)


if __name__ == "__main__":
    unittest.main()
