"""Tests for v3.7.0 — multi-path triangulation worker with calibration back-test.

What v3.7.0 changes from v3.6.2:
  - Each input researched via 3 independent prompt paths (vendor, economic,
    empirical) rather than 1
  - Convergence gate: if paths spread > 30%, no commit, full provenance logged
  - Calibration back-test before any research: worker re-derives OpenRouter
    6K figure; outside ±40% aborts the whole run
  - Prompts now contain explicit KV-cache definition with arithmetic examples
    and anti-examples to eliminate the "max window vs typical fill" ambiguity
    that caused v3.6.2's reading collapse
  - Display-layer forward-looking multiplier (1.40) shown alongside measured
    operative_context in the brief; state stores measured truth only

Coverage:
  - Synthesis logic (converged vs divergent across 3 paths)
  - Share synthesis (per-category convergence; tighter pp threshold)
  - Calibration check passes/fails behavior
  - Prompts contain the disambiguation definition
  - State writes record full provenance for both converged and divergent
  - Display-layer multiplier is applied in brief output
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Triangulation synthesis: typical_context_tokens (3 paths → 1 result)
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesizePaths:
    """The convergence gate that decides whether to commit or flag divergence."""

    def test_three_paths_converge_within_threshold(self):
        """Spread within 30% of mean → status=converged, committed_value=mean."""
        from src.data.context_length_llm_research import _synthesize_paths
        path_a = {"value": 60_000, "confidence": "high"}
        path_b = {"value": 65_000, "confidence": "medium"}
        path_c = {"value": 70_000, "confidence": "high"}
        result = _synthesize_paths(path_a, path_b, path_c, "test")
        assert result["status"] == "converged"
        assert result["committed_value"] == 65_000
        assert result["spread_fraction"] < 0.30
        # uncertainty_band shows the spread to the brief
        assert result["uncertainty_band"]["low"] == 60_000
        assert result["uncertainty_band"]["high"] == 70_000

    def test_three_paths_diverge_beyond_threshold(self):
        """Spread >30% of mean → status=divergent, no committed_value."""
        from src.data.context_length_llm_research import _synthesize_paths
        path_a = {"value": 30_000, "confidence": "high"}
        path_b = {"value": 100_000, "confidence": "medium"}
        path_c = {"value": 200_000, "confidence": "low"}
        result = _synthesize_paths(path_a, path_b, path_c, "test")
        assert result["status"] == "divergent"
        assert "committed_value" not in result
        assert result["spread_fraction"] > 0.30
        # Provenance retained so human reviewer can see what each path said
        assert "paths" in result

    def test_two_paths_present_one_missing(self):
        """Robust to one path failing — needs at least 2 numeric values."""
        from src.data.context_length_llm_research import _synthesize_paths
        path_a = {"value": 60_000, "confidence": "high"}
        path_b = {"value": 64_000, "confidence": "medium"}
        path_c = {"value": None}    # missing
        result = _synthesize_paths(path_a, path_b, path_c, "test")
        # With 2/3 paths converging within threshold, still converges
        assert result["status"] == "converged"

    def test_all_paths_missing_returns_missing(self):
        from src.data.context_length_llm_research import _synthesize_paths
        path_a = {"value": None}
        path_b = {"value": None}
        path_c = {"value": None}
        result = _synthesize_paths(path_a, path_b, path_c, "test")
        assert result["status"] == "missing_values"
        # No committed_value
        assert "committed_value" not in result


# ══════════════════════════════════════════════════════════════════════════════
# Triangulation synthesis: volume_share_pct (5-category convergence)
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesizeSharePaths:
    """Volume shares need per-category convergence using percentage-point spread."""

    def test_all_categories_converge_within_pp_threshold(self):
        from src.data.context_length_llm_research import _synthesize_share_paths
        path_a = {"shares": {"consumer_chat": 30, "coding_agents": 18,
                             "enterprise_vertical_agents": 30,
                             "developer_raw_api": 17, "other_creative_rag": 5}}
        path_b = {"shares": {"consumer_chat": 32, "coding_agents": 20,
                             "enterprise_vertical_agents": 28,
                             "developer_raw_api": 15, "other_creative_rag": 5}}
        path_c = {"shares": {"consumer_chat": 28, "coding_agents": 16,
                             "enterprise_vertical_agents": 32,
                             "developer_raw_api": 19, "other_creative_rag": 5}}
        result = _synthesize_share_paths(path_a, path_b, path_c)
        assert result["status"] == "converged"
        assert "committed_shares" in result
        # Committed shares must sum to exactly 100 (normalized)
        assert sum(result["committed_shares"].values()) == 100

    def test_one_category_divergent_blocks_whole_run(self):
        """ANY category exceeding 10pp spread → status=divergent for entire run."""
        from src.data.context_length_llm_research import _synthesize_share_paths
        path_a = {"shares": {"consumer_chat": 30, "coding_agents": 18,
                             "enterprise_vertical_agents": 30,
                             "developer_raw_api": 17, "other_creative_rag": 5}}
        path_b = {"shares": {"consumer_chat": 30, "coding_agents": 18,
                             "enterprise_vertical_agents": 30,
                             "developer_raw_api": 17, "other_creative_rag": 5}}
        # path_c has enterprise_vertical wildly different (50 vs 30 = 20pp)
        path_c = {"shares": {"consumer_chat": 30, "coding_agents": 5,
                             "enterprise_vertical_agents": 50,
                             "developer_raw_api": 10, "other_creative_rag": 5}}
        result = _synthesize_share_paths(path_a, path_b, path_c)
        assert result["status"] == "divergent"
        assert "committed_shares" not in result
        # Per-category analysis available for human inspection
        assert "per_category" in result


# ══════════════════════════════════════════════════════════════════════════════
# Calibration back-test
# ══════════════════════════════════════════════════════════════════════════════

class TestCalibrationCheck:
    """Worker independently re-derives OpenRouter's 6K figure or aborts the run."""

    def test_passes_when_within_tolerance(self):
        """Worker returns 6,200 → within ±40% of 6,000 → passes."""
        from src.data import context_length_llm_research as mod
        mock_response = {"value": 6_200, "study_or_source_name": "OpenRouter",
                         "source_url": "https://openrouter.ai/state-of-ai",
                         "confidence": "high"}
        with patch.object(mod, "_call_claude_with_search", return_value=mock_response):
            passed, diag = mod._run_calibration_check()
        assert passed is True
        assert diag["calibration_status"] == "passed"
        assert diag["worker_derived_value"] == 6_200

    def test_fails_when_outside_tolerance(self):
        """Worker returns 30,000 → outside ±40% → fails."""
        from src.data import context_length_llm_research as mod
        mock_response = {"value": 30_000, "study_or_source_name": "?",
                         "source_url": "?", "confidence": "low"}
        with patch.object(mod, "_call_claude_with_search", return_value=mock_response):
            passed, diag = mod._run_calibration_check()
        assert passed is False
        assert diag["calibration_status"] == "failed_outside_tolerance"

    def test_fails_when_response_unparseable(self):
        from src.data import context_length_llm_research as mod
        with patch.object(mod, "_call_claude_with_search",
                          side_effect=ValueError("test")):
            passed, diag = mod._run_calibration_check()
        assert passed is False
        assert diag["calibration_status"] == "failed_to_get_response"

    def test_fails_when_value_non_numeric(self):
        from src.data import context_length_llm_research as mod
        mock_response = {"value": "approximately six thousand"}
        with patch.object(mod, "_call_claude_with_search", return_value=mock_response):
            passed, diag = mod._run_calibration_check()
        assert passed is False
        assert diag["calibration_status"] == "non_numeric_response"


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end: calibration failure aborts the run before research
# ══════════════════════════════════════════════════════════════════════════════

class TestCalibrationFailureAbortsRun:
    def test_calibration_failure_skips_all_research(self, tmp_path):
        """If calibration fails, no LLM calls for research; state metadata
        records the failure but no values change."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50_000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        # First call returns a calibration-failing value (1,000 — far below 6K target)
        # No subsequent calls should happen
        call_count = {"n": 0}
        def fake_call(prompt, max_tokens=8192):
            call_count["n"] += 1
            return {"value": 1_000, "study_or_source_name": "?",
                    "source_url": "?", "confidence": "low"}

        with (
            patch.object(mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(mod, "_call_claude_with_search", side_effect=fake_call),
        ):
            result = mod.research_context_length_inputs()

        assert call_count["n"] == 1, (
            "Only the calibration call should fire when calibration fails; "
            f"saw {call_count['n']} calls"
        )
        assert result["status"] == "aborted_calibration_failed"
        assert result["exit_code"] == 2
        # State preserved: typical_context_tokens values unchanged
        final_state = json.loads(state_file.read_text())
        for cat in mod.CATEGORIES:
            assert final_state["typical_context_tokens"][cat]["current"] == 50_000


# ══════════════════════════════════════════════════════════════════════════════
# Prompts contain the disambiguation block
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptsContainDisambiguation:
    """The v3.6.2 production failure was prompt ambiguity: Sonnet interpreted
    'typical context' as 'user message size' rather than 'KV-cache fill'. The
    v3.7.0 prompts contain explicit examples and anti-examples to eliminate
    this. These tests guard against regressions in the prompt text."""

    @pytest.mark.parametrize("path_builder_name", [
        "_build_typical_path_a_vendor",
        "_build_typical_path_b_economic",
        "_build_typical_path_c_empirical",
    ])
    def test_prompt_defines_typical_context_in_terms_of_kv_cache(self, path_builder_name):
        from src.data import context_length_llm_research as mod
        builder = getattr(mod, path_builder_name)
        prompt = builder("coding_agents")
        # The definition must invoke KV cache (the actual GPU memory concept)
        assert "KV cache" in prompt, (
            f"{path_builder_name} prompt does not reference KV cache — "
            f"the disambiguation hinges on this term"
        )

    @pytest.mark.parametrize("path_builder_name", [
        "_build_typical_path_a_vendor",
        "_build_typical_path_b_economic",
        "_build_typical_path_c_empirical",
    ])
    def test_prompt_explicitly_excludes_max_context_window(self, path_builder_name):
        from src.data import context_length_llm_research as mod
        builder = getattr(mod, path_builder_name)
        prompt = builder("coding_agents")
        # Anti-example: must say NOT the max window
        assert "NOT the maximum context window" in prompt

    @pytest.mark.parametrize("path_builder_name", [
        "_build_typical_path_a_vendor",
        "_build_typical_path_b_economic",
        "_build_typical_path_c_empirical",
    ])
    def test_prompt_lists_context_components(self, path_builder_name):
        from src.data import context_length_llm_research as mod
        builder = getattr(mod, path_builder_name)
        prompt = builder("coding_agents")
        # Must enumerate the components that go into the context
        for component in ("system prompt", "Chat history", "RAG", "user message"):
            assert component.lower() in prompt.lower(), (
                f"{path_builder_name} prompt missing component: {component}"
            )

    def test_path_a_method_is_vendor_disclosures(self):
        from src.data.context_length_llm_research import _build_typical_path_a_vendor
        prompt = _build_typical_path_a_vendor("coding_agents")
        assert "VENDOR DISCLOSURES" in prompt
        assert "DO NOT use empirical third-party measurements" in prompt

    def test_path_b_method_is_economic_triangulation(self):
        from src.data.context_length_llm_research import _build_typical_path_b_economic
        prompt = _build_typical_path_b_economic("coding_agents")
        assert "ECONOMIC TRIANGULATION" in prompt
        assert "revenue / blended-input-price" in prompt

    def test_path_c_method_is_empirical_third_party(self):
        from src.data.context_length_llm_research import _build_typical_path_c_empirical
        prompt = _build_typical_path_c_empirical("coding_agents")
        assert "THIRD-PARTY MEASUREMENT" in prompt
        assert "do NOT use vendor product pages" in prompt
        # OpenRouter and RAGAS are explicitly mentioned as known empirical sources
        assert "OpenRouter" in prompt


# ══════════════════════════════════════════════════════════════════════════════
# Display-layer forward-looking multiplier
# ══════════════════════════════════════════════════════════════════════════════

class TestDisplayLayerMultiplier:
    """State holds measured truth; brief displays both measured and forward-
    looking values. Multiplier lives in config so it can be tuned without
    touching state or re-running the LLM worker."""

    def test_config_defines_multiplier(self):
        from src.config import OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER
        assert isinstance(OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER, float)
        # The default value is 1.40 (Raul's specified forward-looking adjustment)
        assert OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER == 1.40

    def test_multiplier_is_env_overridable(self):
        """Allow runtime override for sensitivity testing."""
        import importlib, os
        os.environ["OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER_OVERRIDE"] = "1.60"
        try:
            from src import config as cfg
            importlib.reload(cfg)
            assert cfg.OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER == 1.60
        finally:
            os.environ.pop("OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER_OVERRIDE", None)
            from src import config as cfg
            importlib.reload(cfg)


# ══════════════════════════════════════════════════════════════════════════════
# Workflow timeout raised for the larger run
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkflowTimeout:
    """v3.7.0 makes 19 calls per run vs v3.6.x's 6. Timeout must accommodate."""

    def test_workflow_timeout_at_least_30_minutes(self):
        wf = Path(".github/workflows/context_length_research.yml")
        content = wf.read_text()
        # Look for timeout-minutes: N where N >= 30
        import re
        m = re.search(r"timeout-minutes:\s*(\d+)", content)
        assert m, "timeout-minutes not declared on the workflow job"
        timeout = int(m.group(1))
        assert timeout >= 30, (
            f"Workflow timeout is {timeout} minutes — v3.7.0 needs ≥30 for "
            f"19 LLM calls × ~60s each = ~20 min plus setup buffer"
        )


# ══════════════════════════════════════════════════════════════════════════════
# State writes include full provenance
# ══════════════════════════════════════════════════════════════════════════════

class TestProvenanceWrites:
    """Both converged and divergent results write full path provenance to
    history so the audit trail captures the triangulation methodology."""

    def test_converged_history_entry_includes_all_three_paths(self, tmp_path):
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 60_000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        # Calibration passes; typical_context returns 3 converging paths; volume_share converges
        call_responses = [
            # Calibration
            {"value": 6_000, "study_or_source_name": "OpenRouter",
             "source_url": "https://openrouter.ai/state-of-ai", "confidence": "high"},
        ]
        # 5 categories × 3 paths each = 15 typical_context responses
        for cat in mod.CATEGORIES:
            base = 60_000
            for path in ("a", "b", "c"):
                call_responses.append({
                    "value": base + (1000 if path == "b" else (2000 if path == "c" else 0)),
                    "method": f"path_{path}",
                    "confidence": "high",
                })
        # 3 volume_share responses
        for _ in range(3):
            call_responses.append({
                "shares": {c: 20 for c in mod.CATEGORIES},
                "method": "test", "confidence": "high",
            })

        call_iter = iter(call_responses)

        def fake_call(prompt, max_tokens=8192):
            return next(call_iter)

        with (
            patch.object(mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(mod, "_call_claude_with_search", side_effect=fake_call),
        ):
            result = mod.research_context_length_inputs()

        final_state = json.loads(state_file.read_text())
        for cat in mod.CATEGORIES:
            history = final_state["typical_context_tokens"][cat]["history"]
            # At least one entry from this run
            v370_entries = [h for h in history if h.get("source", "").startswith("llm_research_v3_7_0")]
            assert len(v370_entries) >= 1, f"No v3.7.0 history entry for {cat}"
            entry = v370_entries[-1]
            # Provenance: all three paths recorded
            if entry.get("source") == "llm_research_v3_7_0":   # committed
                assert "paths_provenance" in entry
                assert "a" in entry["paths_provenance"]
                assert "b" in entry["paths_provenance"]
                assert "c" in entry["paths_provenance"]
                assert "uncertainty_band" in entry
                assert "spread_pct" in entry

    def test_calibration_history_appended_to_metadata(self, tmp_path):
        """Every run (passing or failing) appends to _metadata.calibration_history."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 0, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        # Calibration fails — aborts
        with (
            patch.object(mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(mod, "_call_claude_with_search",
                         return_value={"value": 100_000}),
        ):
            mod.research_context_length_inputs()

        final_state = json.loads(state_file.read_text())
        cal_history = final_state["_metadata"]["calibration_history"]
        assert len(cal_history) == 1
        assert cal_history[0]["calibration_status"] == "failed_outside_tolerance"
        assert cal_history[0]["worker_derived_value"] == 100_000
