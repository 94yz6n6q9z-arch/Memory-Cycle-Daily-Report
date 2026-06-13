"""Tests for v3.39.11 — exec-summary source-of-truth + subject line.

Three follow-up problems observed on the 2026-06-12 brief, all stemming
from gaps the v3.39.10 fix didn't fully close:

  Issue (i)  — Subject line "[Not fired]" suffix was misleading: it's the
               t1_spot_decline matrix cell value, not a brief-level summary.
               On 2026-06-12 the subject read "[Not fired]" while a tier-3
               signal had actually unfired that day. Fix: strip suffix,
               date only.

  Issue (ii) — Executive_brief opened with "No tier-1-5 signals fired or
               unfired today — all 23 indicators hold their prior status"
               while a banner showed t3_relative_strength Fired → Not fired.
               Two failures:
               (a) the v3.39.10 regex required `tier\\s+` (whitespace) but
                   Sonnet wrote `tier-1-5` (hyphen); regex also required a
                   `[.;]` sentence terminator but Sonnet used an em-dash.
                   Hardened in this release.
               (b) Sonnet's prior_state diff covered "today" while the
                   banner reflected the rolling 24h window — so the prose
                   could be technically self-consistent yet contradict the
                   banner. Fix: inject the deterministic 24h log into
                   Sonnet's PROMPT before prose is written, making the same
                   list everyone reads.
"""

import unittest

from src.state import (
    _FALSE_NO_SIGNAL_OPENER,
    reconcile_executive_brief_opener,
)


class TestHardenedOpenerRegex(unittest.TestCase):
    """The v3.39.10 regex missed the 2026-06-12 phrasing — hardened here."""

    def test_june6_phrasing_still_matches(self):
        # Regression: the original phrasing the v3.39.10 fix was built for
        # must still match.
        s = ("No signals fired or unfired today; t3_relative_strength reverted "
             "to 'Not fired'.")
        self.assertIsNotNone(_FALSE_NO_SIGNAL_OPENER.search(s))

    def test_june12_phrasing_with_hyphenated_qualifier_and_em_dash(self):
        # The exact observed opener that evaded v3.39.10.
        s = ("No tier-1-5 signals fired or unfired today — all 23 indicators "
             "hold their prior status; the new Micron Form 4 reinforces "
             "t2_insider_sales (already Fired).")
        self.assertIsNotNone(_FALSE_NO_SIGNAL_OPENER.search(s))

    def test_tier_with_space_variant(self):
        s = ("No tier 1-5 signals fired or unfired today — all 23 indicators "
             "hold prior status.")
        self.assertIsNotNone(_FALSE_NO_SIGNAL_OPENER.search(s))

    def test_qualifier_after_signals(self):
        # "No new signals fired or unfired IN TIER 1-5 today; ..." —
        # qualifier comes AFTER "signals".
        s = "No new signals fired or unfired in tier 1-5 today; the tape held."
        self.assertIsNotNone(_FALSE_NO_SIGNAL_OPENER.search(s))

    def test_en_dash_terminator(self):
        # En-dash (–) is rarer than em-dash but Sonnet uses it occasionally.
        s = ("No tier-1-5 signals fired or unfired today – all indicators hold "
             "their prior status.")
        self.assertIsNotNone(_FALSE_NO_SIGNAL_OPENER.search(s))

    def test_clean_brief_with_quote_does_not_match(self):
        # MUST NOT match a brief that leads with the actual change. Single
        # quotes around 'Not fired' contain the substring "fired" but the
        # opener doesn't start with "no" so the anchor fails.
        s = "Relative strength (t3) reverted to 'Not fired' as SK Hynix outperformed."
        self.assertIsNone(_FALSE_NO_SIGNAL_OPENER.search(s))

    def test_clean_brief_with_capex_focus_does_not_match(self):
        s = ("CapEx parabolic (t2) reaffirmed by Google's $920M/month xAI "
             "commitment and NVIDIA's $1T 2027 hyperscaler tally.")
        self.assertIsNone(_FALSE_NO_SIGNAL_OPENER.search(s))


class TestReconcileEndToEndOnJune12Brief(unittest.TestCase):
    """End-to-end: the deployed helper now rewrites the June-12 opener."""

    def test_june12_opener_is_rewritten(self):
        brief = ("No tier-1-5 signals fired or unfired today — all 23 indicators "
                 "hold their prior status; the new Micron Form 4 (2026-06-11, "
                 "SEC EDGAR §3) reinforces t2_insider_sales (already Fired).")
        out = reconcile_executive_brief_opener(
            brief, ["Memory pure-plays underperform SOX (t3)"]
        )
        self.assertNotEqual(out, brief, "the false opener must be replaced")
        self.assertFalse(out.lower().startswith("no tier"))
        self.assertIn("1 signal status change today", out)
        # Sonnet's actual content after the opener must survive.
        self.assertIn("Micron Form 4", out)

    def test_empty_signal_list_leaves_brief_untouched(self):
        # If no tier-1-5 signals changed, even a regex match must not rewrite —
        # caller-side filtering (overlay-only triggers) is the safety net.
        brief = ("No tier-1-5 signals fired or unfired today — Indicator 7 "
                 "(Competitor dynamics) updated to 'Partially fired'.")
        self.assertEqual(reconcile_executive_brief_opener(brief, []), brief)


class TestSubjectLineDateOnly(unittest.TestCase):
    """Subject suffix removed. We inspect the source to confirm."""

    def test_fast_tier_subject_no_suffix(self):
        # The build site should contain a plain f-string with the date and no
        # cycle_verdict.cell.value append. This is a structural assertion
        # rather than a runtime one because the live subject is assembled
        # inside run_fast_tier (heavy I/O dependencies).
        with open("src/fast_tier.py") as f:
            content = f.read()
        # The plain subject must exist.
        self.assertIn(
            'subject = f"Memory Cycle Brief — {today.strftime',
            content,
            "fast_tier must still build a date-prefixed subject",
        )
        # The verdict-suffix append must be gone.
        self.assertNotIn(
            'subject += f" [{cycle_verdict.cell.value}]"',
            content,
            "the [Not fired|Partially fired|Fired] subject suffix must be removed",
        )


class TestAuthoritative24hLogPromptInjection(unittest.TestCase):
    """The deterministic 24h log is fed to Sonnet as authoritative input."""

    def test_build_sonnet_analysis_prompt_accepts_authoritative_log(self):
        # Signature must accept the new parameter; the prompt must reference
        # the authoritative-transitions block for both populated and empty
        # log cases.
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt
        import inspect
        sig = inspect.signature(build_sonnet_analysis_prompt)
        self.assertIn("authoritative_24h_log", sig.parameters)
        self.assertIsNone(sig.parameters["authoritative_24h_log"].default)

    def test_analyze_with_llm_accepts_authoritative_log(self):
        from src.analysis.llm_summarizer import analyze_with_llm
        import inspect
        sig = inspect.signature(analyze_with_llm)
        self.assertIn("authoritative_24h_log", sig.parameters)

    def test_sonnet_analyze_accepts_authoritative_log(self):
        from src.analysis.llm_summarizer import sonnet_analyze
        import inspect
        sig = inspect.signature(sonnet_analyze)
        self.assertIn("authoritative_24h_log", sig.parameters)

    def test_prompt_text_contains_authoritative_block_anchor(self):
        # The injected block carries a distinctive header; verify it appears
        # in the prompt source unconditionally (both populated and empty
        # branches emit a block).
        with open("src/analysis/llm_summarizer.py") as f:
            src = f.read()
        self.assertIn("AUTHORITATIVE INDICATOR TRANSITIONS", src)
        # Hard rule explicitly forbids the false phrasing.
        self.assertIn(
            "may NOT open the executive_brief with any claim equivalent to",
            src,
        )


if __name__ == "__main__":
    unittest.main()
