"""v3.41.1 — exec-summary / prose leak sanitizer.

Guards that framework version tags and internal-mechanism vocabulary are
stripped from investor-facing prose, while normal analyst prose is left intact.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analysis.llm_summarizer import _strip_internal_jargon as strip


class TestExecLeakSanitizer(unittest.TestCase):
    def test_version_paren_tag_removed(self):
        out = strip("SK Hynix flat (per v3.4.1 length cap) amid first dissent.")
        self.assertNotIn("v3.4.1", out)
        self.assertNotIn("(", out)
        self.assertIn("SK Hynix flat amid first dissent.", out)

    def test_inline_per_version_removed(self):
        out = strip("Resolved by timestamp per v3.39.7.6.")
        self.assertNotIn("v3.39.7.6", out)
        self.assertNotIn("per v", out.lower())

    def test_bare_version_token_removed(self):
        out = strip("This follows v3.39.7.9 logic.")
        self.assertNotIn("v3.39.7.9", out)

    def test_operative_state_rewritten(self):
        self.assertIn("current state", strip("the operative state of the world today"))
        self.assertNotIn("operative state", strip("the operative state today").lower())

    def test_requires_resolution_rewritten(self):
        out = strip("The Samsung strike requires resolution.")
        self.assertIn("remains unresolved", out)
        self.assertNotIn("requires resolution", out.lower())

    def test_recency_rule_name_removed(self):
        out = strip("The later item wins per the RECENCY ON CONTRADICTING EVENTS rule.")
        self.assertNotIn("RECENCY ON CONTRADICTING EVENTS", out)

    def test_normal_prose_untouched(self):
        clean = "Spot DRAM fell 3% w/w as DDR5 loosened; HBM4 qualification on track."
        self.assertEqual(strip(clean), clean)

    def test_non_string_passthrough(self):
        self.assertIsNone(strip(None))
        self.assertEqual(strip(""), "")

    def test_no_double_spaces_or_space_before_punct(self):
        out = strip("Hynix flat (v3.4.1) , then up per v3.4.2 .")
        self.assertNotIn("  ", out)
        self.assertNotIn(" ,", out)
        self.assertNotIn(" .", out)


if __name__ == "__main__":
    unittest.main()
