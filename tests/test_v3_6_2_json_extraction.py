"""Tests for v3.6.2 — JSON extraction fix + Sonnet model revert.

Background:
  v3.6.0 shipped with two bugs:
    1. Model set to Opus 4.7 — too slow (40-90s/call) for our use case
    2. Response parsing called json.loads() on the entire response string,
       which fails when Claude emits reasoning prose before the JSON (which
       it does ~100% of the time with web_search enabled)
  First production run on 2026-05-25 confirmed both: 6/6 calls returned
  HTTP 200 but 6/6 JSON parses failed at "Expecting value: line 1 column 1
  (char 0)" — the universal symptom of json.loads() on non-JSON-starting text.

v3.6.2 fixes:
  - Switch model to Sonnet 4.6 (faster, better instruction-tuned for JSON)
  - Replace json.loads() with brace-matching extractor (_extract_json_from_text)
  - Add diagnostic logging of first 500 chars on parse failure
"""

from __future__ import annotations

import json

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# _extract_json_from_text — the actual fix
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractJsonFromText:
    """The function that fixes the production failure mode."""

    def test_clean_json_passes_through(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        assert _extract_json_from_text('{"value": 100}') == {"value": 100}

    def test_json_with_prose_before(self):
        """The actual production failure case: reasoning text precedes JSON."""
        from src.data.context_length_llm_research import _extract_json_from_text
        text = (
            'Based on my research across vendor docs, earnings transcripts, '
            'and SimilarWeb reports, here is the result:\n\n'
            '{"value": 18000, "confidence": "high"}'
        )
        result = _extract_json_from_text(text)
        assert result == {"value": 18000, "confidence": "high"}

    def test_json_with_markdown_fence(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        text = '```json\n{"value": 200000}\n```'
        assert _extract_json_from_text(text) == {"value": 200000}

    def test_json_with_prose_after(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        text = '{"value": 50} I hope this helps!'
        assert _extract_json_from_text(text) == {"value": 50}

    def test_json_with_prose_before_and_after(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        text = 'Here you go: {"value": 50} Let me know if you need more.'
        assert _extract_json_from_text(text) == {"value": 50}

    def test_nested_objects(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        text = (
            'Result: {"shares": {"consumer_chat": 30, "enterprise_vertical_agents": 30}, '
            '"total": 100}'
        )
        result = _extract_json_from_text(text)
        assert result["shares"]["consumer_chat"] == 30
        assert result["total"] == 100

    def test_braces_inside_string_literal(self):
        """A string containing { or } must not confuse the brace counter."""
        from src.data.context_length_llm_research import _extract_json_from_text
        text = 'Output: {"reasoning": "the value is {high} or {low}", "value": 5}'
        result = _extract_json_from_text(text)
        assert result["reasoning"] == "the value is {high} or {low}"
        assert result["value"] == 5

    def test_escaped_quotes_in_string(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        text = '{"name": "foo \\"bar\\" baz", "id": 1}'
        result = _extract_json_from_text(text)
        assert result["name"] == 'foo "bar" baz'
        assert result["id"] == 1

    def test_no_brace_raises(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        with pytest.raises(json.JSONDecodeError):
            _extract_json_from_text("No JSON here, just prose")

    def test_unbalanced_braces_raises(self):
        from src.data.context_length_llm_research import _extract_json_from_text
        with pytest.raises(json.JSONDecodeError):
            _extract_json_from_text('{"unclosed": "missing closing brace"')

    def test_returns_first_json_when_multiple(self):
        """Behavior: takes the first balanced object, ignores subsequent."""
        from src.data.context_length_llm_research import _extract_json_from_text
        text = '{"first": 1} and then {"second": 2}'
        result = _extract_json_from_text(text)
        assert result == {"first": 1}

    def test_handles_realistic_llm_response(self):
        """A realistic response shape matching what Sonnet+web_search produces."""
        from src.data.context_length_llm_research import _extract_json_from_text
        text = """Let me research current data for consumer chat context lengths.

I'll search for typical usage patterns across major consumer chat products.

Based on my research:
- ChatGPT (~58% share): 128K context available, ~12K typical usage
- Gemini (~22% share): 1M available, ~10K typical usage
- Microsoft Copilot (~14% share): 128K available, ~18K typical usage
- Claude consumer (~5% share): 200K available, ~30K typical usage
- Others (~1% share): variable

Weighted average: ~17,000 tokens

```json
{
  "value": 17000,
  "confidence": "high",
  "reasoning": "Weighted average across ChatGPT, Gemini, Copilot, and Claude consumer based on web traffic share (SimilarWeb March 2026) and observed typical session lengths.",
  "providers_considered": [
    {"name": "ChatGPT", "share_weight": 0.58, "max_context": 128000, "typical_usage_fraction": 0.09, "implied_typical_tokens": 12000}
  ],
  "weighted_calculation": "0.58*12K + 0.22*10K + 0.14*18K + 0.05*30K + 0.01*30K = 17K",
  "source_urls": ["https://www.similarweb.com/", "https://docs.anthropic.com/"],
  "as_of_date": "2026-05-25"
}
```
"""
        result = _extract_json_from_text(text)
        assert result["value"] == 17000
        assert result["confidence"] == "high"
        assert len(result["source_urls"]) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Model switched back to Sonnet
# ══════════════════════════════════════════════════════════════════════════════

class TestSonnetModel:
    def test_worker_imports_sonnet_not_opus(self):
        """The worker should reference SONNET_MODEL from config, not OPUS_MODEL."""
        import inspect
        from src.data import context_length_llm_research
        src = inspect.getsource(context_length_llm_research)
        # Should import SONNET_MODEL
        assert "from src.config import SONNET_MODEL" in src
        # Should NOT import OPUS_MODEL
        assert "OPUS_MODEL" not in src

    def test_sonnet_model_string_is_correct(self):
        """Sanity check: config.SONNET_MODEL is the expected string."""
        from src.config import SONNET_MODEL
        assert SONNET_MODEL == "claude-sonnet-4-6"


# ══════════════════════════════════════════════════════════════════════════════
# Diagnostic logging on parse failure
# ══════════════════════════════════════════════════════════════════════════════

class TestDiagnosticOnParseFailure:
    """When extraction fails, we should log a preview of the response so we
    can debug without re-running the workflow."""

    def test_logger_called_with_response_preview(self, caplog):
        """Mock a Claude response containing un-parseable text, verify the
        diagnostic preview shows up in logs."""
        import logging
        from unittest.mock import patch, MagicMock
        from src.data import context_length_llm_research as llm_mod

        # Mock the anthropic client to return a response with un-parseable content
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.text = "This is just prose with no JSON whatsoever in it"
        mock_response.content = [mock_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
             caplog.at_level(logging.ERROR):
            try:
                llm_mod._call_claude_with_search("test prompt")
            except json.JSONDecodeError:
                pass    # Expected — we want to confirm the LOG happened

        # The error log should include a preview of what Claude returned
        log_messages = " ".join(rec.message for rec in caplog.records)
        assert "JSON extraction failed" in log_messages
        assert "This is just prose" in log_messages
