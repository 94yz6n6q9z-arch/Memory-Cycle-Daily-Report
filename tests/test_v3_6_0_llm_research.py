"""Tests for v3.6.0 — LLM-driven context length research worker.

Covers:
  - Sanity-check functions reject implausible values
  - JSON parsing handles markdown fences and whitespace
  - State file updates preserve schema + append history
  - Rejected values retain previous state (no silent corruption)
  - Workflow file has correct schedule, secrets, permissions
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Sanity check: typical_context_tokens
# ══════════════════════════════════════════════════════════════════════════════

class TestSanityCheckTypicalContext:
    def test_accepts_plausible_value(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        ok, reason = _sanity_check_typical_context(500_000, 400_000, "high", "coding_agents")
        assert ok is True
        assert reason == ""

    def test_rejects_below_plausible_floor(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        ok, reason = _sanity_check_typical_context(500, 50_000, "high", "consumer_chat")
        assert ok is False
        assert "below plausible floor" in reason

    def test_rejects_above_plausible_ceiling(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        ok, reason = _sanity_check_typical_context(50_000_000, 1_000_000, "high", "enterprise_vertical_agents")
        assert ok is False
        assert "above plausible ceiling" in reason

    def test_rejects_delta_over_50_percent(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        # Previous 100K, proposed 200K = 100% delta
        ok, reason = _sanity_check_typical_context(200_000, 100_000, "high", "consumer_chat")
        assert ok is False
        assert "exceeds 50% threshold" in reason

    def test_accepts_delta_within_50_percent(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        # Previous 100K, proposed 140K = 40% delta
        ok, reason = _sanity_check_typical_context(140_000, 100_000, "high", "consumer_chat")
        assert ok is True

    def test_rejects_low_confidence(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        ok, reason = _sanity_check_typical_context(500_000, 400_000, "low", "coding_agents")
        assert ok is False
        assert "below accepted threshold" in reason or "Confidence" in reason

    def test_accepts_medium_confidence(self):
        from src.data.context_length_llm_research import _sanity_check_typical_context
        ok, reason = _sanity_check_typical_context(500_000, 400_000, "medium", "coding_agents")
        assert ok is True

    def test_no_previous_value_accepts_any_plausible(self):
        """First-run case: no previous value → only floor/ceiling checks apply."""
        from src.data.context_length_llm_research import _sanity_check_typical_context
        ok, reason = _sanity_check_typical_context(500_000, 0, "high", "coding_agents")
        assert ok is True


# ══════════════════════════════════════════════════════════════════════════════
# Sanity check: volume_share_pct
# ══════════════════════════════════════════════════════════════════════════════

class TestSanityCheckShares:
    def _prev(self):
        return {
            "consumer_chat": 30, "coding_agents": 18,
            "enterprise_vertical_agents": 30,
            "developer_raw_api": 17, "other_creative_rag": 5,
        }

    def test_accepts_plausible_shares(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        proposed = {
            "consumer_chat": 28, "coding_agents": 20,
            "enterprise_vertical_agents": 32,
            "developer_raw_api": 15, "other_creative_rag": 5,
        }
        ok, reason = _sanity_check_shares(proposed, self._prev(), "high")
        assert ok is True

    def test_rejects_when_not_sum_to_100(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        proposed = {
            "consumer_chat": 28, "coding_agents": 20,
            "enterprise_vertical_agents": 32,
            "developer_raw_api": 15, "other_creative_rag": 10,  # sums to 105
        }
        ok, reason = _sanity_check_shares(proposed, self._prev(), "high")
        assert ok is False
        assert "sum to" in reason.lower()

    def test_rejects_when_category_missing(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        proposed = {
            "consumer_chat": 50, "coding_agents": 20,
            "enterprise_vertical_agents": 25,
            "developer_raw_api": 5,
            # other_creative_rag missing
        }
        ok, reason = _sanity_check_shares(proposed, self._prev(), "high")
        assert ok is False
        assert "Missing category" in reason

    def test_rejects_delta_over_15pp(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        # Previous enterprise=30, proposed=50 → 20pp delta
        proposed = {
            "consumer_chat": 15, "coding_agents": 15,
            "enterprise_vertical_agents": 50,
            "developer_raw_api": 15, "other_creative_rag": 5,
        }
        ok, reason = _sanity_check_shares(proposed, self._prev(), "high")
        assert ok is False
        assert "exceeds 15pp threshold" in reason

    def test_accepts_delta_within_15pp(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        proposed = {
            "consumer_chat": 25, "coding_agents": 22,
            "enterprise_vertical_agents": 35,    # +5pp from 30
            "developer_raw_api": 13, "other_creative_rag": 5,
        }
        ok, reason = _sanity_check_shares(proposed, self._prev(), "high")
        assert ok is True

    def test_rejects_low_confidence(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        proposed = self._prev()
        ok, reason = _sanity_check_shares(proposed, self._prev(), "low")
        assert ok is False

    def test_rejects_negative_share(self):
        from src.data.context_length_llm_research import _sanity_check_shares
        proposed = {
            "consumer_chat": -5, "coding_agents": 35,
            "enterprise_vertical_agents": 40,
            "developer_raw_api": 20, "other_creative_rag": 10,
        }
        ok, reason = _sanity_check_shares(proposed, self._prev(), "high")
        assert ok is False
        assert "negative" in reason.lower()


# ══════════════════════════════════════════════════════════════════════════════
# Markdown fence stripping
# ══════════════════════════════════════════════════════════════════════════════

class TestStripMarkdownFences:
    def test_strips_json_fence(self):
        from src.data.context_length_llm_research import _strip_markdown_fences
        wrapped = '```json\n{"value": 100}\n```'
        result = _strip_markdown_fences(wrapped)
        assert result == '{"value": 100}'

    def test_strips_plain_fence(self):
        from src.data.context_length_llm_research import _strip_markdown_fences
        wrapped = '```\n{"value": 100}\n```'
        result = _strip_markdown_fences(wrapped)
        assert result == '{"value": 100}'

    def test_preserves_unwrapped_json(self):
        from src.data.context_length_llm_research import _strip_markdown_fences
        result = _strip_markdown_fences('{"value": 100}')
        assert result == '{"value": 100}'

    def test_strips_leading_whitespace(self):
        from src.data.context_length_llm_research import _strip_markdown_fences
        result = _strip_markdown_fences('  \n  {"value": 100}\n  ')
        assert result == '{"value": 100}'


# ══════════════════════════════════════════════════════════════════════════════
# State file update + rejection behavior
# ══════════════════════════════════════════════════════════════════════════════

class TestStateFileUpdate:
    def _bootstrap_state(self):
        return {
            "_metadata": {},
            "typical_context_tokens": {
                "consumer_chat":              {"current": 20_000,    "history": []},
                "coding_agents":              {"current": 500_000,   "history": []},
                "enterprise_vertical_agents": {"current": 1_000_000, "history": []},
                "developer_raw_api":          {"current": 50_000,    "history": []},
                "other_creative_rag":         {"current": 50_000,    "history": []},
            },
            "volume_shares": {
                "committed": {
                    "consumer_chat": 30, "coding_agents": 18,
                    "enterprise_vertical_agents": 30,
                    "developer_raw_api": 17, "other_creative_rag": 5,
                },
                "proposed": None, "proposal_evidence": [],
                "proposal_status": "none", "proposal_created": None,
                "history": [],
            },
            "operative_context_history": [],
        }

    def _mock_llm_response_typical(self, category, new_value=22_000):
        """Mock LLM response for typical_context_tokens for a category."""
        return {
            "value": new_value,
            "confidence": "high",
            "reasoning": "Triangulated from web traffic + vendor docs",
            "providers_considered": [{"name": "Test", "share_weight": 1.0}],
            "weighted_calculation": "test calc",
            "source_urls": ["https://example.com/1", "https://example.com/2"],
            "as_of_date": date.today().isoformat(),
        }

    def _mock_llm_response_shares(self):
        return {
            "shares": {
                "consumer_chat": 28, "coding_agents": 20,
                "enterprise_vertical_agents": 32,
                "developer_raw_api": 15, "other_creative_rag": 5,
            },
            "confidence": "high",
            "reasoning": "Triangulated from revenue mix + web traffic",
            "category_rationale": {c: "test" for c in [
                "consumer_chat", "coding_agents", "enterprise_vertical_agents",
                "developer_raw_api", "other_creative_rag",
            ]},
            "calculation_steps": "test math",
            "source_urls": ["https://example.com/3"],
            "as_of_date": date.today().isoformat(),
        }

    def test_accepted_value_updates_current_and_appends_history(self, tmp_path):
        from src.data import context_length_llm_research as llm_mod
        state_file = tmp_path / "context_length_state.json"
        state_file.write_text(json.dumps(self._bootstrap_state()))

        def mock_call(prompt, max_tokens=4096):
            # Return small-delta responses for all calls
            if "shares" in prompt or "volume_share" in prompt:
                return self._mock_llm_response_shares()
            return self._mock_llm_response_typical("anycat", new_value=22_000)

        with (
            patch.object(llm_mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(llm_mod, "_call_claude_with_search", side_effect=mock_call),
        ):
            result = llm_mod.research_context_length_inputs()

        final_state = json.loads(state_file.read_text())
        # consumer_chat current updated from 20_000 to 22_000
        assert final_state["typical_context_tokens"]["consumer_chat"]["current"] == 22_000
        # History entry appended with source attribution
        history = final_state["typical_context_tokens"]["consumer_chat"]["history"]
        assert len(history) == 1
        assert history[0]["source"] == "llm_research_v3_6_0"
        assert "source_urls" in history[0]
        assert history[0]["previous_value"] == 20_000

    def test_rejected_value_keeps_previous(self, tmp_path):
        """Implausible delta → previous value retained, rejection logged."""
        from src.data import context_length_llm_research as llm_mod
        state_file = tmp_path / "context_length_state.json"
        state_file.write_text(json.dumps(self._bootstrap_state()))

        def mock_call(prompt, max_tokens=4096):
            if "volume_share" in prompt:
                return self._mock_llm_response_shares()
            # Return an implausible 10x jump
            return self._mock_llm_response_typical("anycat", new_value=200_000)

        with (
            patch.object(llm_mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(llm_mod, "_call_claude_with_search", side_effect=mock_call),
        ):
            result = llm_mod.research_context_length_inputs()

        final_state = json.loads(state_file.read_text())
        # consumer_chat current SHOULD be unchanged (rejection retains previous)
        assert final_state["typical_context_tokens"]["consumer_chat"]["current"] == 20_000
        # And the rejection should be in the summary
        assert result["rejected_count"] >= 1

    def test_low_confidence_rejected(self, tmp_path):
        from src.data import context_length_llm_research as llm_mod
        state_file = tmp_path / "context_length_state.json"
        state_file.write_text(json.dumps(self._bootstrap_state()))

        def mock_call(prompt, max_tokens=4096):
            if "volume_share" in prompt:
                return self._mock_llm_response_shares()
            r = self._mock_llm_response_typical("anycat", new_value=22_000)
            r["confidence"] = "low"
            return r

        with (
            patch.object(llm_mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(llm_mod, "_call_claude_with_search", side_effect=mock_call),
        ):
            result = llm_mod.research_context_length_inputs()

        # Low confidence on all 5 categories → 5 rejections
        assert result["rejected_count"] >= 5
        final_state = json.loads(state_file.read_text())
        # Previous values retained
        assert final_state["typical_context_tokens"]["consumer_chat"]["current"] == 20_000

    def test_metadata_records_last_run(self, tmp_path):
        from src.data import context_length_llm_research as llm_mod
        state_file = tmp_path / "context_length_state.json"
        state_file.write_text(json.dumps(self._bootstrap_state()))

        def mock_call(prompt, max_tokens=4096):
            if "volume_share" in prompt:
                return self._mock_llm_response_shares()
            return self._mock_llm_response_typical("anycat", new_value=22_000)

        with (
            patch.object(llm_mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(llm_mod, "_call_claude_with_search", side_effect=mock_call),
        ):
            llm_mod.research_context_length_inputs()

        final_state = json.loads(state_file.read_text())
        assert "last_llm_research" in final_state["_metadata"]
        assert "last_run_summary" in final_state["_metadata"]


# ══════════════════════════════════════════════════════════════════════════════
# Workflow file structure
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkflowFile:
    PATH = Path(".github/workflows/context_length_research.yml")

    def test_workflow_file_exists(self):
        assert self.PATH.exists(), (
            "context_length_research.yml workflow must exist in v3.6.0 bundle"
        )

    def test_workflow_has_bimonthly_cron(self):
        content = self.PATH.read_text()
        # Cron should fire 1st of Jan/Mar/May/Jul/Sep/Nov
        assert "cron:" in content
        assert "1,3,5,7,9,11" in content

    def test_workflow_uses_anthropic_api_key_secret(self):
        content = self.PATH.read_text()
        assert "ANTHROPIC_API_KEY" in content
        assert "secrets.ANTHROPIC_API_KEY" in content

    def test_workflow_has_contents_write_permission(self):
        content = self.PATH.read_text()
        assert "contents: write" in content

    def test_workflow_supports_manual_dispatch(self):
        content = self.PATH.read_text()
        assert "workflow_dispatch:" in content

    def test_workflow_invokes_llm_research_module(self):
        content = self.PATH.read_text()
        assert "python -m src.data.context_length_llm_research" in content

    def test_workflow_opens_issue_on_failure(self):
        content = self.PATH.read_text()
        assert "if: failure()" in content
        assert "github.rest.issues.create" in content

    def test_workflow_uses_pull_rebase_pattern(self):
        """Same v3.4.7 pattern as other workflows: pull --rebase before push."""
        content = self.PATH.read_text()
        assert "git pull --rebase" in content


# ══════════════════════════════════════════════════════════════════════════════
# Deprecated stub for proposal diagnostic
# ══════════════════════════════════════════════════════════════════════════════

class TestProposalDiagnosticStub:
    """v3.6.0 removed the volume-share proposal mechanism. The function stub
    must still exist and return empty string so existing call sites don't break."""

    def test_function_exists(self):
        from src.email_module.template import _build_context_volume_proposal_diagnostic
        assert callable(_build_context_volume_proposal_diagnostic)

    def test_returns_empty_for_any_input(self):
        from src.email_module.template import _build_context_volume_proposal_diagnostic
        # Should return "" regardless of input
        assert _build_context_volume_proposal_diagnostic({}) == ""
        assert _build_context_volume_proposal_diagnostic({"context_volume_proposal": None}) == ""
        assert _build_context_volume_proposal_diagnostic({
            "context_volume_proposal": {
                "proposed": {"consumer_chat": 40},
                "committed": {"consumer_chat": 30},
            }
        }) == ""


# ══════════════════════════════════════════════════════════════════════════════
# Verify obsolete modules are gone
# ══════════════════════════════════════════════════════════════════════════════

class TestObsoleteModulesRemoved:
    """v3.6.0 deleted the scraper and aggregator. Confirm they're gone."""

    def test_scraper_module_deleted(self):
        scraper = Path("src/data/context_length_scraper.py")
        assert not scraper.exists(), (
            "src/data/context_length_scraper.py should be deleted in v3.6.0"
        )

    def test_aggregator_module_deleted(self):
        aggregator = Path("src/analysis/context_length_aggregator.py")
        assert not aggregator.exists(), (
            "src/analysis/context_length_aggregator.py should be deleted in v3.6.0"
        )

    def test_old_monthly_refresh_workflow_deleted(self):
        wf = Path(".github/workflows/context_length_refresh.yml")
        assert not wf.exists(), (
            "context_length_refresh.yml should be replaced by context_length_research.yml"
        )

    def test_dry_run_workflow_deleted(self):
        wf = Path(".github/workflows/context_length_dry_run.yml")
        assert not wf.exists(), (
            "context_length_dry_run.yml was a v3.5.3 helper, not needed in v3.6.0"
        )

    def test_digester_prompt_no_longer_references_context_volume_signals(self):
        from src.analysis.digester import DIGEST_SYSTEM_PROMPT
        assert "context_volume_signals" not in DIGEST_SYSTEM_PROMPT, (
            "DIGEST_SYSTEM_PROMPT should no longer reference context_volume_signals"
        )

    def test_fast_tier_no_longer_calls_aggregator(self):
        import inspect
        from src import fast_tier
        src = inspect.getsource(fast_tier)
        assert "run_context_volume_aggregator" not in src, (
            "fast_tier should no longer call run_context_volume_aggregator"
        )
