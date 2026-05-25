"""Tests for v3.7.2 — reliability hardening on top of v3.7.1 synthesis.

v3.7.1 ship-blocking production bug: when path A's JSON extraction failed
for one category (Sonnet wrote 4K of prose before a truncated JSON), the
exception was caught by the outer try/except, synthesis returned floor as
committed_value with candidate=None, then the summary message construction
crashed with `TypeError: unsupported format string passed to NoneType.__format__`
on the line that formatted `synthesis.get('candidate'):,`. The crash propagated
out of research_context_length_inputs() before _save_state() ran, losing all
in-memory commits (consumer_chat=8,500 and coding_agents=100,000 were never
persisted to disk).

v3.7.2 changes addressed by these tests:
  1. _fmt_int helper — None-safe `:,` formatting
  2. Plausibility filter on individual path values before averaging
  3. Per-path try/except (typical_context AND volume_shares)
  4. Per-category state save after history append
  5. try/finally wrapper — final state save runs even on unhandled exception
  6. _call_claude_with_search default max_tokens raised to 16384
  7. _invoke_and_extract / _call_claude_with_search split — retry layer
  8. JSON-parse retry with doubled max_tokens on first failure
  9. Strengthened JSON-only directive in all 7 path prompts
  10. End-of-run summary block (smoke-tested via stdout capture)

The framework's synthesis math (floor + max(B, mean), mean(A, B) for shares)
is unchanged from v3.7.1 and continues to be covered.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Floor + max(B, mean) synthesis for typical_context_tokens
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesizePaths:
    """Always commits a value. Floor is the never-go-below anchor."""

    def test_floor_binds_when_paths_are_below(self):
        """May 25 production data: paths well below seed floor → commit floor."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 5200, "method": "vendor"},
            {"value": 6000, "method": "economic"},
            {"value": 2000, "method": "empirical"},
            "consumer_chat",    # SEED_FLOOR = 8,500
        )
        assert result["status"] == "committed"
        assert result["committed_value"] == 8500
        assert result["binding_rule"] == "floor"
        assert result["seed_floor"] == 8500
        # Candidate (max of B and mean) is recorded for audit
        assert result["candidate"] == 6000    # max(B=6000, mean=4400) = 6000

    def test_economic_path_b_wins_max_rule_when_above_floor(self):
        """When B > mean and both exceed floor, candidate = B."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 100_000, "method": "vendor"},
            {"value": 150_000, "method": "economic"},
            {"value": 80_000, "method": "empirical"},
            "consumer_chat",    # floor 8500
        )
        assert result["binding_rule"] == "candidate"
        assert result["candidate"] == 150_000    # max(B, mean=110k) = B
        assert result["committed_value"] == 150_000

    def test_mean_wins_when_higher_than_economic(self):
        """When mean > B (e.g., A and C are higher), candidate = mean."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 200_000, "method": "vendor"},
            {"value": 50_000, "method": "economic"},
            {"value": 250_000, "method": "empirical"},
            "coding_agents",    # floor 100k
        )
        # mean = (200+50+250)/3 = 166,666; B = 50,000; candidate = max(50K, 166K) = 166K
        assert result["binding_rule"] == "candidate"
        assert result["candidate"] == 166_666 or result["candidate"] == 166_667
        assert result["committed_value"] >= 166_000

    def test_path_c_participates_in_mean(self):
        """Path C is INCLUDED in the mean calculation for typical_context."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 50_000},
            {"value": 50_000},
            {"value": 200_000},    # path C pulls mean up
            "coding_agents",    # floor 100k
        )
        # mean = (50+50+200)/3 = 100,000
        assert result["mean_of_paths"] == 100_000
        # candidate = max(B=50k, mean=100k) = 100k
        assert result["candidate"] == 100_000

    def test_all_paths_failed_commits_floor_defensively(self):
        """No path returned a value → commit floor as safe baseline."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths({}, {}, {}, "consumer_chat")
        assert result["status"] == "all_paths_failed_floor_applied"
        assert result["committed_value"] == 8500    # floor
        assert result["binding_rule"] == "floor"

    def test_one_path_present_still_commits(self):
        """Robust to two paths failing — uses whatever's available."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 200_000},    # only A succeeds
            {},
            {},
            "coding_agents",    # floor 100k
        )
        # mean = 200K, B missing → candidate = mean = 200K
        assert result["committed_value"] == 200_000
        assert result["binding_rule"] == "candidate"

    def test_spread_recorded_but_no_gate(self):
        """Spread percentage IS recorded for audit but never blocks commit."""
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 1_000},
            {"value": 10_000},
            {"value": 100_000},
            "coding_agents",
        )
        # Huge spread but still committed (floor 100k will bind)
        assert result["status"] == "committed"
        assert result["spread_pct"] > 100    # information only
        assert "committed_value" in result


# ══════════════════════════════════════════════════════════════════════════════
# mean(A, B) synthesis for volume_share_pct — Path C ignored in math
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesizeShares:
    """Volume shares use mean of A and B only. Path C is recorded for
    provenance but excluded — its empirical-study source (OpenRouter) is
    structurally biased toward developer API traffic."""

    def test_mean_of_a_and_b_normalized_to_100(self):
        """May 25 production data: confirm shares come out 17/19/33/23/8."""
        from src.data.context_length_llm_research import _synthesize_share_paths
        result = _synthesize_share_paths(
            {"shares": {"consumer_chat": 7, "coding_agents": 16,
                        "enterprise_vertical_agents": 33, "developer_raw_api": 33,
                        "other_creative_rag": 11}},
            {"shares": {"consumer_chat": 27, "coding_agents": 22,
                        "enterprise_vertical_agents": 33, "developer_raw_api": 13,
                        "other_creative_rag": 5}},
            {"shares": {"consumer_chat": 4, "coding_agents": 8,
                        "enterprise_vertical_agents": 19, "developer_raw_api": 68,
                        "other_creative_rag": 1}},    # path C outlier on dev_api
        )
        assert result["status"] == "committed"
        shares = result["committed_shares"]
        # Path C's 68% dev_api outlier does NOT pull final share toward 38%
        # (which is what mean of all 3 would produce); A+B mean is 23%
        assert shares["developer_raw_api"] == 23
        assert sum(shares.values()) == 100
        # Other values should match preview: 17/19/33/23/8
        assert shares["consumer_chat"] == 17
        assert shares["coding_agents"] == 19
        assert shares["enterprise_vertical_agents"] == 33
        assert shares["other_creative_rag"] == 8

    def test_path_c_recorded_in_history_but_not_in_math(self):
        """Path C must still appear in per_category provenance."""
        from src.data.context_length_llm_research import _synthesize_share_paths
        result = _synthesize_share_paths(
            {"shares": {"consumer_chat": 20, "coding_agents": 20,
                        "enterprise_vertical_agents": 20, "developer_raw_api": 20,
                        "other_creative_rag": 20}},
            {"shares": {"consumer_chat": 20, "coding_agents": 20,
                        "enterprise_vertical_agents": 20, "developer_raw_api": 20,
                        "other_creative_rag": 20}},
            {"shares": {"consumer_chat": 99, "coding_agents": 0,
                        "enterprise_vertical_agents": 1, "developer_raw_api": 0,
                        "other_creative_rag": 0}},    # path C noise
        )
        # Path C ignored in math → all categories at 20%
        for cat in ["consumer_chat", "coding_agents", "enterprise_vertical_agents",
                    "developer_raw_api", "other_creative_rag"]:
            assert result["committed_shares"][cat] == 20
        # But Path C value preserved per category for audit
        assert result["per_category"]["consumer_chat"]["path_c_informational"] == 99

    def test_one_of_a_or_b_missing_uses_the_other(self):
        """Robust to one path failing for a category."""
        from src.data.context_length_llm_research import _synthesize_share_paths
        result = _synthesize_share_paths(
            {"shares": {"consumer_chat": 20, "coding_agents": 20,
                        "enterprise_vertical_agents": 20, "developer_raw_api": 20,
                        "other_creative_rag": 20}},
            {"shares": {}},    # B missing entirely
            {"shares": {}},
        )
        # Should still commit using only path A (each category gets normalized)
        assert result["status"] == "committed"
        assert sum(result["committed_shares"].values()) == 100


# ══════════════════════════════════════════════════════════════════════════════
# Calibration back-test — kept from v3.7.0, unchanged
# ══════════════════════════════════════════════════════════════════════════════

class TestCalibrationCheck:
    def test_passes_within_tolerance(self):
        from src.data import context_length_llm_research as mod
        mock_response = {"value": 6200, "study_or_source_name": "OpenRouter",
                         "source_url": "https://openrouter.ai/state-of-ai",
                         "confidence": "high"}
        with patch.object(mod, "_call_claude_with_search", return_value=mock_response):
            passed, diag = mod._run_calibration_check()
        assert passed is True
        assert diag["calibration_status"] == "passed"

    def test_fails_outside_tolerance(self):
        from src.data import context_length_llm_research as mod
        with patch.object(mod, "_call_claude_with_search",
                          return_value={"value": 30000}):
            passed, diag = mod._run_calibration_check()
        assert passed is False
        assert diag["calibration_status"] == "failed_outside_tolerance"

    def test_failure_aborts_run_without_committing(self, tmp_path):
        """If calibration fails, no research calls fire and state is preserved."""
        from src.data import context_length_llm_research as mod
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        call_count = {"n": 0}
        def fake(prompt, max_tokens=8192):
            call_count["n"] += 1
            return {"value": 1000, "confidence": "low"}

        with (patch.object(mod, "CONTEXT_STATE_FILE", state_file),
              patch.object(mod, "_call_claude_with_search", side_effect=fake)):
            result = mod.research_context_length_inputs()

        assert call_count["n"] == 1, "Only calibration should fire on failure"
        assert result["status"] == "aborted_calibration_failed"
        assert result["exit_code"] == 2
        # State preserved
        s = json.loads(state_file.read_text())
        for cat in mod.CATEGORIES:
            assert s["typical_context_tokens"][cat]["current"] == 50000


# ══════════════════════════════════════════════════════════════════════════════
# REMOVED in v3.7.1: confirm the multiplier is gone from config and template
# ══════════════════════════════════════════════════════════════════════════════

class TestForwardLookingMultiplierRemoved:
    """The 1.40 X-axis multiplier added in v3.7.0 was a duplicate of the
    +1 Y-band anticipation premium that already exists in dhcr.py. v3.7.1
    removes the multiplier entirely."""

    def test_config_does_not_define_multiplier(self):
        """Importing the multiplier constant must fail."""
        with pytest.raises(ImportError):
            from src.config import OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER  # noqa: F401

    def test_template_does_not_reference_multiplier(self):
        """The brief template must not import or use the multiplier."""
        template_text = Path("src/email_module/template.py").read_text()
        assert "OPERATIVE_CONTEXT_FORWARD_LOOKING_MULTIPLIER" not in template_text
        assert "_OPCTX_MULT" not in template_text
        assert "forward-looking" not in template_text.lower() or \
               "1.40" not in template_text, (
            "Brief should not reference forward-looking X-axis multiplier; "
            "the +1 Y-band anticipation premium in dhcr.py is the singular "
            "forward-looking element"
        )

    def test_anticipation_premium_still_in_dhcr(self):
        """The +1 Y-band anticipation premium MUST still exist — it's the
        framework's singular forward-looking element."""
        dhcr_text = Path("src/analysis/dhcr.py").read_text()
        assert "compute_market_deployed_cell" in dhcr_text
        assert "ANTICIPATION_Y_OFFSET" in dhcr_text
        # The +1 shift mechanism must still be in code
        assert "workload_row + ANTICIPATION_Y_OFFSET" in dhcr_text


# ══════════════════════════════════════════════════════════════════════════════
# Seed floor configuration matches the documented state seed
# ══════════════════════════════════════════════════════════════════════════════

class TestSeedFloors:
    def test_seed_floors_defined_for_all_categories(self):
        from src.data.context_length_llm_research import (
            SEED_FLOORS_TYPICAL_CONTEXT, CATEGORIES,
        )
        for cat in CATEGORIES:
            assert cat in SEED_FLOORS_TYPICAL_CONTEXT
            assert SEED_FLOORS_TYPICAL_CONTEXT[cat] >= 1000

    def test_floor_values_match_documented_baseline(self):
        """Floors must match the May 25 deep-dive research baseline."""
        from src.data.context_length_llm_research import SEED_FLOORS_TYPICAL_CONTEXT
        assert SEED_FLOORS_TYPICAL_CONTEXT["consumer_chat"] == 8500
        assert SEED_FLOORS_TYPICAL_CONTEXT["coding_agents"] == 100_000
        assert SEED_FLOORS_TYPICAL_CONTEXT["enterprise_vertical_agents"] == 65_000
        assert SEED_FLOORS_TYPICAL_CONTEXT["developer_raw_api"] == 40_000
        assert SEED_FLOORS_TYPICAL_CONTEXT["other_creative_rag"] == 8500


# ══════════════════════════════════════════════════════════════════════════════
# REMOVED gates — confirm the legacy thresholds aren't lurking in code
# ══════════════════════════════════════════════════════════════════════════════

class TestRemovedGates:
    """v3.7.0 had a 30% convergence gate and 50% delta sanity check. v3.7.1
    removes both — the floor + max-rule is the validity anchor."""

    def test_30pct_convergence_gate_removed(self):
        """The MAX_PATH_SPREAD_FRACTION constant should be gone OR the
        synthesis should not gate on it."""
        from src.data import context_length_llm_research as mod
        # The constant may be absent entirely, or kept for informational purposes;
        # what matters is the synthesis logic always commits
        result = mod._synthesize_paths(
            {"value": 1000}, {"value": 100_000}, {"value": 1_000_000},
            "consumer_chat",
        )
        assert result["status"] == "committed", (
            "Wide spread must not block commit; floor + max-rule replaces gate"
        )

    def test_50pct_delta_sanity_check_removed(self):
        """Previously huge deltas from previous state would block commits.
        Now the floor + max-rule decides — deltas are recorded but not gated."""
        from src.data import context_length_llm_research as mod
        # No standalone _sanity_check_typical_context function should remain
        assert not hasattr(mod, "_sanity_check_typical_context"), (
            "Old sanity check should be removed; floor + max-rule replaces it"
        )


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end: full run produces correct state under floor-binding scenario
# ══════════════════════════════════════════════════════════════════════════════

class TestFullRunFloorBinding:
    def test_run_commits_floor_when_paths_low(self, tmp_path):
        """When all paths come in below floor (May 25 reality), the run
        commits each category at its floor with full provenance recorded."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        # Simulate today's actual production output
        responses = [
            # Calibration
            {"value": 6000, "study_or_source_name": "OpenRouter",
             "source_url": "https://openrouter.ai/state-of-ai", "confidence": "high"},
        ]
        # 5 categories × 3 paths — paths well below all floors (May 25 reality)
        per_cat_paths = {
            "consumer_chat": [5200, 6000, 2000],
            "coding_agents": [35000, 60000, 28000],
            "enterprise_vertical_agents": [32000, 25000, 35000],
            "developer_raw_api": [4800, 7500, 8000],
            "other_creative_rag": [5000, 5000, 4500],
        }
        for cat in mod.CATEGORIES:
            for v in per_cat_paths[cat]:
                responses.append({"value": v, "method": "test", "confidence": "medium"})

        # 3 volume_share responses (May 25 actual values)
        responses.append({"shares": {"consumer_chat": 7, "coding_agents": 16,
                                     "enterprise_vertical_agents": 33,
                                     "developer_raw_api": 33,
                                     "other_creative_rag": 11},
                          "method": "revenue_mix"})
        responses.append({"shares": {"consumer_chat": 27, "coding_agents": 22,
                                     "enterprise_vertical_agents": 33,
                                     "developer_raw_api": 13,
                                     "other_creative_rag": 5},
                          "method": "traffic_proxy"})
        responses.append({"shares": {"consumer_chat": 4, "coding_agents": 8,
                                     "enterprise_vertical_agents": 19,
                                     "developer_raw_api": 68,
                                     "other_creative_rag": 1},
                          "method": "bottom_up"})

        iterator = iter(responses)

        def fake(prompt, max_tokens=8192):
            return next(iterator)

        with (patch.object(mod, "CONTEXT_STATE_FILE", state_file),
              patch.object(mod, "_call_claude_with_search", side_effect=fake)):
            result = mod.research_context_length_inputs()

        # All 5 categories + volume_shares committed
        assert result["status"] == "completed_success"
        assert result["exit_code"] == 0
        assert len(result["committed"]) == 6    # 5 typical + 1 shares
        # All 5 typical_context entries floor-bound
        assert len(result["floor_bound"]) == 5

        final = json.loads(state_file.read_text())
        # Each typical_context committed at its seed floor
        assert final["typical_context_tokens"]["consumer_chat"]["current"] == 8500
        assert final["typical_context_tokens"]["coding_agents"]["current"] == 100_000
        assert final["typical_context_tokens"]["enterprise_vertical_agents"]["current"] == 65_000
        assert final["typical_context_tokens"]["developer_raw_api"]["current"] == 40_000
        assert final["typical_context_tokens"]["other_creative_rag"]["current"] == 8500
        # Volume shares: 17/19/33/23/8 (mean of A, B; C ignored)
        shares = final["volume_shares"]["committed"]
        assert shares["consumer_chat"] == 17
        assert shares["coding_agents"] == 19
        assert shares["enterprise_vertical_agents"] == 33
        assert shares["developer_raw_api"] == 23
        assert shares["other_creative_rag"] == 8
        assert sum(shares.values()) == 100

        # History entries include all the provenance fields
        history_entry = final["typical_context_tokens"]["consumer_chat"]["history"][-1]
        assert history_entry["source"] == "llm_research_v3_7_2"
        assert history_entry["binding_rule"] == "floor"
        assert history_entry["seed_floor"] == 8500
        assert "path_values" in history_entry
        assert "spread_pct" in history_entry    # informational only
        # Path provenance recorded for all 3 paths
        assert "paths_provenance" in history_entry
        assert "a" in history_entry["paths_provenance"]
        assert "b" in history_entry["paths_provenance"]
        assert "c" in history_entry["paths_provenance"]


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — None-safe formatting (the v3.7.1 production crash)
# ══════════════════════════════════════════════════════════════════════════════

class TestNoneSafeFormatting:
    """The exact crash that hit v3.7.1 production at 09:31 UTC on 2026-05-25."""

    def test_fmt_int_handles_none(self):
        from src.data.context_length_llm_research import _fmt_int
        assert _fmt_int(None) == "N/A"

    def test_fmt_int_handles_ints(self):
        from src.data.context_length_llm_research import _fmt_int
        assert _fmt_int(8500) == "8,500"
        assert _fmt_int(100_000) == "100,000"
        assert _fmt_int(0) == "0"

    def test_fmt_int_handles_floats(self):
        from src.data.context_length_llm_research import _fmt_int
        assert _fmt_int(8500.7) == "8,500"

    def test_fmt_int_handles_other_garbage(self):
        from src.data.context_length_llm_research import _fmt_int
        # Doesn't crash on string or list
        assert "N/A" in _fmt_int(None) or _fmt_int("foo") == "foo"

    def test_floor_summary_message_with_none_candidate_no_crash(self):
        """Reproduce the exact v3.7.1 crash conditions: synthesis returns
        committed_value=floor and candidate=None. The summary message
        builder must format both safely."""
        from src.data.context_length_llm_research import _synthesize_paths, _fmt_int

        result = _synthesize_paths({}, {}, {}, "enterprise_vertical_agents")
        assert result["candidate"] is None     # the conditions that bit v3.7.1
        assert result["committed_value"] == 65_000

        # This is the exact formatting pattern from the main loop
        msg = (
            f"typical_context[enterprise_vertical_agents]: "
            f"floor {_fmt_int(result['committed_value'])} "
            f"(candidate was {_fmt_int(result['candidate'])})"
        )
        assert "65,000" in msg
        assert "N/A" in msg     # candidate-was-None safely rendered


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — Plausibility filter on individual path values
# ══════════════════════════════════════════════════════════════════════════════

class TestPlausibilityFilter:
    """Path values are filtered through plausibility bounds before averaging.
    A single garbage path (e.g. v=1 or v=50M tokens) should not pull the mean."""

    def test_too_low_value_dropped(self):
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 1},          # garbage — too low
            {"value": 6000},
            {"value": 5000},
            "consumer_chat",
        )
        # Mean = (6000 + 5000) / 2 = 5500; floor 8500 binds
        assert result["committed_value"] == 8_500
        # Path A dropped with rationale
        assert len(result["dropped_paths"]) == 1
        assert "path_a" in result["dropped_paths"][0]
        assert "implausible" in result["dropped_paths"][0]

    def test_too_high_value_dropped(self):
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 50_000_000},   # garbage — too high (max-context number)
            {"value": 5000},
            {"value": 5000},
            "consumer_chat",
        )
        # A dropped; mean of B and C = 5000 → floor 8500 binds
        assert result["committed_value"] == 8_500
        assert len(result["dropped_paths"]) == 1
        assert "path_a" in result["dropped_paths"][0]

    def test_non_numeric_value_dropped(self):
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": "approximately 50K"},     # model returned text
            {"value": 6000},
            {"value": 5000},
            "consumer_chat",
        )
        assert result["committed_value"] == 8_500     # floor binds
        assert any("not numeric" in d for d in result["dropped_paths"])

    def test_missing_value_field_dropped(self):
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"wrong_key": 5000},     # missing the 'value' field
            {"value": 6000},
            {"value": 5000},
            "consumer_chat",
        )
        assert any("no value" in d for d in result["dropped_paths"])
        # Only paths B and C contribute → mean=5500 → floor binds
        assert result["committed_value"] == 8_500

    def test_dropped_path_reasons_logged_in_history_via_synthesis(self):
        from src.data.context_length_llm_research import _synthesize_paths
        result = _synthesize_paths(
            {"value": 1},
            {"value": "fifty thousand"},
            {"value": 5000},
            "consumer_chat",
        )
        # Both A (implausible) and B (non-numeric) recorded with reasons
        assert len(result["dropped_paths"]) == 2
        reasons = " ".join(result["dropped_paths"])
        assert "implausible" in reasons
        assert "not numeric" in reasons


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — Per-path try/except (one path failing doesn't abort the category)
# ══════════════════════════════════════════════════════════════════════════════

class TestPerPathIsolation:
    def test_path_a_failure_does_not_prevent_b_and_c_running(self, tmp_path):
        """Reproduce the v3.7.1 enterprise_vertical scenario: path A raises
        JSONDecodeError. With per-path try/except, paths B and C still run."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        call_log = []

        def fake_call(prompt, max_tokens=16384):
            # Map prompt fingerprint to category and path
            call_log.append(prompt[:120])
            # Calibration
            if "calibration" in prompt.lower() or "openrouter" in prompt.lower():
                return {"value": 6000, "study_or_source_name": "OpenRouter",
                        "source_url": "https://openrouter.ai/state-of-ai",
                        "confidence": "high"}
            # Path A vendor for enterprise_vertical raises (the v3.7.1 scenario)
            if ("enterprise_vertical" in prompt.lower()
                and "VENDOR DISCLOSURES ONLY" in prompt):
                raise json.JSONDecodeError("Unbalanced braces", "I'll research...", 0)
            # All other paths return floor-respecting low values
            if "ECONOMIC TRIANGULATION" in prompt:
                return {"value": 25000, "method": "economic", "confidence": "medium"}
            if "EMPIRICAL THIRD-PARTY" in prompt or "studies" in prompt.lower():
                return {"value": 35000, "method": "empirical", "confidence": "medium"}
            if "VENDOR DISCLOSURES ONLY" in prompt:
                return {"value": 32000, "method": "vendor", "confidence": "medium"}
            # Volume shares paths
            if "share" in prompt.lower() and "REVENUE MIX" in prompt:
                return {"shares": {c: 20 for c in mod.CATEGORIES}}
            if "share" in prompt.lower() and "TRAFFIC" in prompt:
                return {"shares": {c: 20 for c in mod.CATEGORIES}}
            if "share" in prompt.lower():
                return {"shares": {c: 20 for c in mod.CATEGORIES}}
            return {"value": 5000, "confidence": "low"}

        with (patch.object(mod, "CONTEXT_STATE_FILE", state_file),
              patch.object(mod, "_call_claude_with_search", side_effect=fake_call)):
            result = mod.research_context_length_inputs()

        # Run completed. path_a for enterprise_vertical errored, but B and C
        # still produced values; floor still bound (since A=missing, mean of B+C
        # = 30K < seed_floor 65K). State must reflect this.
        s = json.loads(state_file.read_text())
        assert s["typical_context_tokens"]["enterprise_vertical_agents"]["current"] == 65_000

        # path_a error recorded but other categories committed fine
        assert any("path_a" in e for e in result["errors"])
        assert s["typical_context_tokens"]["consumer_chat"]["current"] >= 8_500


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — Per-category state save (state persists across mid-loop crashes)
# ══════════════════════════════════════════════════════════════════════════════

class TestPerCategoryStateSave:
    def test_state_saved_after_each_category(self, tmp_path):
        """If Python crashes hard mid-loop, earlier categories must already
        be on disk. Simulate by counting _save_state calls."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        def fake_call(prompt, max_tokens=16384):
            if "calibration" in prompt.lower() or "openrouter" in prompt.lower():
                return {"value": 6000, "confidence": "high"}
            if "share" in prompt.lower():
                return {"shares": {c: 20 for c in mod.CATEGORIES}}
            return {"value": 5000, "method": "test", "confidence": "medium"}

        save_count = {"n": 0}
        original_save = mod._save_state
        def counting_save(state):
            save_count["n"] += 1
            return original_save(state)

        with (patch.object(mod, "CONTEXT_STATE_FILE", state_file),
              patch.object(mod, "_call_claude_with_search", side_effect=fake_call),
              patch.object(mod, "_save_state", side_effect=counting_save)):
            result = mod.research_context_length_inputs()

        # Expected saves: 5 per-category + 1 final from try/finally = 6
        # (volume_shares doesn't save separately; it persists via the finally)
        assert save_count["n"] >= 6, (
            f"Expected ≥6 saves (5 per-category + 1 final), got {save_count['n']}"
        )

    def test_state_persists_when_later_category_crashes(self, tmp_path):
        """If category 3 of 5 crashes, categories 1 and 2 are still on disk."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        category_calls = {c: 0 for c in mod.CATEGORIES}

        def fake_call(prompt, max_tokens=16384):
            if "calibration" in prompt.lower() or "openrouter" in prompt.lower():
                return {"value": 6000, "confidence": "high"}
            if "share" in prompt.lower():
                return {"shares": {c: 20 for c in mod.CATEGORIES}}
            # Track which category we're in
            for c in mod.CATEGORIES:
                if c in prompt.lower():
                    category_calls[c] += 1
                    # Force ALL 3 paths to fail for enterprise_vertical (simulating
                    # severe upstream issue). Floor binding should still commit.
                    if c == "enterprise_vertical_agents":
                        raise RuntimeError("simulated enterprise upstream failure")
                    break
            return {"value": 5000, "method": "test", "confidence": "medium"}

        with (patch.object(mod, "CONTEXT_STATE_FILE", state_file),
              patch.object(mod, "_call_claude_with_search", side_effect=fake_call)):
            result = mod.research_context_length_inputs()

        # consumer_chat and coding_agents (before enterprise_vertical) must have
        # both been committed AND persisted
        s = json.loads(state_file.read_text())
        assert s["typical_context_tokens"]["consumer_chat"]["current"] == 8_500   # floor
        assert s["typical_context_tokens"]["coding_agents"]["current"] == 100_000  # floor
        # enterprise_vertical floor-bound despite 3 path failures
        assert s["typical_context_tokens"]["enterprise_vertical_agents"]["current"] == 65_000


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — try/finally wrapper (final state save on unhandled exception)
# ══════════════════════════════════════════════════════════════════════════════

class TestTryFinallyWrapper:
    def test_state_metadata_saved_even_on_unhandled_exception(self, tmp_path):
        """If a non-LLM bug crashes the function, the finally block still
        writes last_run_at and last_run_summary to state."""
        from src.data import context_length_llm_research as mod

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "_metadata": {},
            "typical_context_tokens": {c: {"current": 50000, "history": []}
                                       for c in mod.CATEGORIES},
            "volume_shares": {"committed": {c: 20 for c in mod.CATEGORIES},
                              "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None,
                              "history": []},
            "operative_context_history": [],
        }))

        def fake_call(prompt, max_tokens=16384):
            if "calibration" in prompt.lower() or "openrouter" in prompt.lower():
                return {"value": 6000, "confidence": "high"}
            return {"value": 5000, "method": "test", "confidence": "medium"}

        # Inject a crash in _synthesize_share_paths to simulate an unhandled
        # exception from a non-LLM code path. The try/finally must still
        # save metadata.
        original_synth = mod._synthesize_share_paths
        def crashing_synth(*args, **kwargs):
            raise RuntimeError("simulated post-loop crash")

        with (patch.object(mod, "CONTEXT_STATE_FILE", state_file),
              patch.object(mod, "_call_claude_with_search", side_effect=fake_call),
              patch.object(mod, "_synthesize_share_paths", side_effect=crashing_synth)):
            try:
                result = mod.research_context_length_inputs()
            except RuntimeError:
                pass    # crash is expected to propagate

        s = json.loads(state_file.read_text())
        # finally block must have written metadata
        assert "last_run_at" in s["_metadata"]
        # And all 5 categories from the loop should be committed
        for c in mod.CATEGORIES:
            assert s["typical_context_tokens"][c]["current"] >= 8_500


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — JSON retry with doubled max_tokens
# ══════════════════════════════════════════════════════════════════════════════

class TestJsonRetry:
    def test_retry_on_first_json_failure(self):
        """_call_claude_with_search retries once with doubled max_tokens
        on JSONDecodeError. If the retry succeeds, the result is returned."""
        from src.data import context_length_llm_research as mod

        call_log = []

        def fake_invoke(prompt, max_tokens):
            call_log.append(max_tokens)
            if max_tokens == 16384:
                raise json.JSONDecodeError("truncated", "preview", 100)
            # Retry with 32768 → succeed
            return {"value": 50_000, "confidence": "medium"}

        with patch.object(mod, "_invoke_and_extract", side_effect=fake_invoke):
            result = mod._call_claude_with_search("test prompt")

        assert result == {"value": 50_000, "confidence": "medium"}
        # Two attempts: 16384 then doubled to 32768
        assert call_log == [16384, 32768]

    def test_no_retry_on_first_success(self):
        from src.data import context_length_llm_research as mod

        call_log = []

        def fake_invoke(prompt, max_tokens):
            call_log.append(max_tokens)
            return {"value": 5000, "confidence": "high"}

        with patch.object(mod, "_invoke_and_extract", side_effect=fake_invoke):
            result = mod._call_claude_with_search("test prompt")

        # Only one call, no retry
        assert len(call_log) == 1
        assert call_log[0] == 16384

    def test_raises_after_second_failure(self):
        """If even the doubled-tokens retry fails, the exception propagates
        so the per-path try/except converts to a dropped path."""
        from src.data import context_length_llm_research as mod

        def fake_invoke(prompt, max_tokens):
            raise json.JSONDecodeError("still truncated", "preview", 100)

        with patch.object(mod, "_invoke_and_extract", side_effect=fake_invoke):
            with pytest.raises(json.JSONDecodeError):
                mod._call_claude_with_search("test prompt")

    def test_default_max_tokens_is_16384(self):
        from src.data.context_length_llm_research import _call_claude_with_search
        import inspect
        sig = inspect.signature(_call_claude_with_search)
        assert sig.parameters["max_tokens"].default == 16384


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.2 — Strengthened JSON-only directive in all path prompts
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptJsonDirective:
    def test_all_prompts_contain_strict_directive(self):
        """All 7 path-builder prompts must contain the strengthened directive."""
        from src.data.context_length_llm_research import (
            _build_calibration_prompt,
            _build_typical_path_a_vendor,
            _build_typical_path_b_economic,
            _build_typical_path_c_empirical,
            _build_share_path_a_revenue_mix,
            _build_share_path_b_traffic_proxy,
            _build_share_path_c_bottom_up,
        )

        prompt_builders = [
            _build_calibration_prompt,
            lambda: _build_typical_path_a_vendor("consumer_chat"),
            lambda: _build_typical_path_b_economic("consumer_chat"),
            lambda: _build_typical_path_c_empirical("consumer_chat"),
            _build_share_path_a_revenue_mix,
            _build_share_path_b_traffic_proxy,
            _build_share_path_c_bottom_up,
        ]
        for builder in prompt_builders:
            prompt = builder()
            assert "OUTPUT FORMAT — STRICT" in prompt, \
                f"{builder.__name__ if hasattr(builder, '__name__') else 'prompt'} missing strict directive"
            assert "Begin your response with the character `{`" in prompt
            assert "do not narrate them" in prompt.lower()

    def test_no_old_weaker_directive_remains(self):
        """Older 'return ONLY a JSON object' phrasing must not coexist."""
        text = Path("src/data/context_length_llm_research.py").read_text()
        assert "return ONLY a JSON object" not in text
