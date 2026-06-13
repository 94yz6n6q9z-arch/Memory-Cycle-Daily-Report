"""Tests for v3.40.0 — batch pipeline trims + shared L2 helpers + config.

Covers:
  * trim (a): static card fields (name/description/what_to_look_for) filled
    from config, NOT from Sonnet output.
  * trim (b): relevant_links capped at 3.
  * trim (c) + output discipline: prompt schema no longer asks Sonnet to emit
    the static fields and carries the word caps.
  * input trim: dormant ("Not fired", unchanged) signals are one-lined in the
    Sonnet prompt.
  * the shared Layer 2 parse helper (_parse_l2_message) used by the batch path
    produces a correct SignalReasoning.
  * config flags + lowered Sonnet cap.
"""

import unittest
from types import SimpleNamespace

from src.config import INDICATORS, USE_BATCH_API, BATCH_POLL_CAP_SECONDS, SONNET_MAX_TOKENS
from src.analysis.llm_summarizer import _safe_indicator_assessment, _INDICATOR_BY_ID


class TestStaticFieldFillTrimA(unittest.TestCase):
    def test_description_and_name_come_from_config_not_sonnet(self):
        ind = INDICATORS[0]
        # Sonnet emits garbage static fields; we must ignore them.
        a = {
            "id": ind.id,
            "tier": ind.tier,
            "name": "WRONG NAME FROM SONNET",
            "description": "WRONG DESCRIPTION FROM SONNET",
            "what_to_look_for": "WRONG WTLF FROM SONNET",
            "current_status": "Fired",
            "where_we_stand": "x",
            "latest_developments": "y",
        }
        out = _safe_indicator_assessment(a)
        self.assertEqual(out.name, ind.name)
        self.assertEqual(out.description, ind.description)
        self.assertEqual(out.what_to_look_for, ind.what_to_look_for)
        # dynamic fields still come from Sonnet
        self.assertEqual(out.current_status, "Fired")
        self.assertEqual(out.where_we_stand, "x")

    def test_unknown_id_falls_back_to_emitted_fields(self):
        a = {"id": "totally_unknown_id", "tier": 2, "name": "N",
             "description": "D", "what_to_look_for": "W", "current_status": "Not fired"}
        out = _safe_indicator_assessment(a)
        # not in config → keep whatever Sonnet gave (truncation safety)
        self.assertEqual(out.description, "D")
        self.assertEqual(out.name, "N")


class TestLinkCapTrimB(unittest.TestCase):
    def test_links_capped_at_three(self):
        ind = INDICATORS[0]
        a = {
            "id": ind.id, "tier": ind.tier, "current_status": "Not fired",
            "relevant_links": [{"title": f"t{i}", "url": f"http://x/{i}"} for i in range(8)],
        }
        out = _safe_indicator_assessment(a)
        self.assertEqual(len(out.relevant_links), 3)

    def test_non_list_links_become_empty(self):
        ind = INDICATORS[0]
        a = {"id": ind.id, "tier": ind.tier, "current_status": "Not fired",
             "relevant_links": "not-a-list"}
        out = _safe_indicator_assessment(a)
        self.assertEqual(out.relevant_links, [])


class TestPromptOutputDiscipline(unittest.TestCase):
    """Source-level checks on the Sonnet prompt schema (trim a/b/c)."""

    def setUp(self):
        with open("src/analysis/llm_summarizer.py") as f:
            self.src = f.read()

    def test_schema_does_not_request_static_fields(self):
        # The indicator_assessments JSON example must not ask for these.
        i = self.src.find('"indicator_assessments": [')
        j = self.src.find('"market_summary"', i)
        schema = self.src[i:j]
        self.assertNotIn('"description": "..."', schema)
        self.assertNotIn('"what_to_look_for": "..."', schema)
        self.assertNotIn('"name": "..."', schema)

    def test_output_discipline_instructions_present(self):
        self.assertIn("v3.40.0 OUTPUT DISCIPLINE", self.src)
        self.assertIn("do NOT emit `name`, `description`, or", self.src)
        self.assertIn("Cap", self.src)

    def test_word_caps_present(self):
        self.assertIn("≤50 words", self.src)
        self.assertIn("≤35 words", self.src)

    def test_input_trim_one_liner_present(self):
        self.assertIn("Not fired (unchanged)", self.src)


class TestL2ParseHelper(unittest.TestCase):
    """The shared parser used by the batch path yields a correct reasoning."""

    def test_parse_batch_message_to_reasoning(self):
        from src.analysis.signal_reasoner import _parse_l2_message
        import json as _json
        ind = INDICATORS[0]
        verdict = {
            "evidence_count": 3, "evidence_sources": ["a", "b"],
            "consensus_direction": "bearish", "convergence": "high",
            "fired_status": "Partially fired",
            "fired_status_evidence": ["e1", "e2"],
            "evidence_for_change_vs_yesterday": "new broker note",
            "key_quotes_consolidated": ["q1"], "confidence": "high",
        }
        resp = SimpleNamespace(content=[SimpleNamespace(text=_json.dumps(verdict))])
        ev = {"relevant_digests": [], "relevant_news": [], "signal_specific_data": {}}
        sr = _parse_l2_message(ind, resp, "Not fired", ev, "(none)", "user-msg-text")
        self.assertIsNotNone(sr)
        self.assertEqual(sr.signal_id, ind.id)
        self.assertEqual(sr.fired_status, "Partially fired")
        self.assertEqual(sr.convergence, "high")
        self.assertEqual(sr.evidence_count, 3)

    def test_parse_handles_code_fence(self):
        from src.analysis.signal_reasoner import _parse_l2_message
        ind = INDICATORS[0]
        fenced = '```json\n{"fired_status": "Fired", "confidence": "high"}\n```'
        resp = SimpleNamespace(content=[SimpleNamespace(text=fenced)])
        ev = {"relevant_digests": [], "relevant_news": [], "signal_specific_data": {}}
        sr = _parse_l2_message(ind, resp, None, ev, "(none)", "m")
        self.assertEqual(sr.fired_status, "Fired")

    def test_parse_bad_json_returns_none(self):
        from src.analysis.signal_reasoner import _parse_l2_message
        ind = INDICATORS[0]
        resp = SimpleNamespace(content=[SimpleNamespace(text="not json at all")])
        ev = {"relevant_digests": [], "relevant_news": [], "signal_specific_data": {}}
        sr = _parse_l2_message(ind, resp, None, ev, "(none)", "m")
        self.assertIsNone(sr)


class TestConfigFlags(unittest.TestCase):
    def test_batch_flag_defaults_true(self):
        self.assertIsInstance(USE_BATCH_API, bool)

    def test_poll_cap_is_eight_minutes(self):
        self.assertEqual(BATCH_POLL_CAP_SECONDS, 480)

    def test_sonnet_cap_lowered(self):
        self.assertEqual(SONNET_MAX_TOKENS, 28000)

    def test_indicator_lookup_covers_all(self):
        self.assertEqual(len(_INDICATOR_BY_ID), len(INDICATORS))


if __name__ == "__main__":
    unittest.main()
