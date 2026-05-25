"""Tests for v3.8.0 Y-axis cost fixes.

Covers:
  - Staleness guard skips run when state is recent
  - Staleness guard runs when state is stale or absent
  - Volume shares Path C call is no longer present in the codebase
  - Staleness guard handles malformed last_run_at gracefully
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_state(days_ago: int | None) -> dict:
    """Build a minimal context_length_state dict with last_run_at set days_ago."""
    state: dict = {"_metadata": {}, "typical_context_tokens": {}, "volume_shares": {}}
    if days_ago is not None:
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        state["_metadata"]["last_run_at"] = ts
    return state


def test_staleness_guard_skips_when_recent(tmp_path):
    """Worker must return immediately without API calls when last_run_at < 60 days."""
    state = _make_state(days_ago=5)
    state_file = tmp_path / "context_length_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch(
            "src.data.context_length_llm_research.CONTEXT_STATE_FILE",
            state_file,
        ),
        patch("src.data.context_length_llm_research._call_claude_with_search") as mock_api,
    ):
        from src.data.context_length_llm_research import research_context_length_inputs
        result = research_context_length_inputs()

    assert result.get("skipped") is True, f"Expected skipped=True, got {result}"
    mock_api.assert_not_called()


def test_staleness_guard_runs_when_stale(tmp_path):
    """Worker must NOT skip when last_run_at is older than 60 days."""
    state = _make_state(days_ago=70)
    state_file = tmp_path / "context_length_state.json"
    state_file.write_text(json.dumps(state))

    # Patch API call so it raises immediately — we just need to confirm
    # the guard did not short-circuit
    with (
        patch(
            "src.data.context_length_llm_research.CONTEXT_STATE_FILE",
            state_file,
        ),
        patch(
            "src.data.context_length_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ),
    ):
        from src.data.context_length_llm_research import research_context_length_inputs
        result = research_context_length_inputs()

    assert result.get("skipped") is not True, "Guard should not have fired for 70-day-old state"


def test_staleness_guard_runs_when_no_prior_run(tmp_path):
    """Worker must NOT skip when there is no last_run_at in state."""
    state = _make_state(days_ago=None)
    state_file = tmp_path / "context_length_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch(
            "src.data.context_length_llm_research.CONTEXT_STATE_FILE",
            state_file,
        ),
        patch(
            "src.data.context_length_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ),
    ):
        from src.data.context_length_llm_research import research_context_length_inputs
        result = research_context_length_inputs()

    assert result.get("skipped") is not True


def test_staleness_guard_handles_malformed_timestamp(tmp_path):
    """Malformed last_run_at must not crash the worker; it must proceed."""
    state = _make_state(days_ago=None)
    state["_metadata"]["last_run_at"] = "not-a-valid-timestamp"
    state_file = tmp_path / "context_length_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch(
            "src.data.context_length_llm_research.CONTEXT_STATE_FILE",
            state_file,
        ),
        patch(
            "src.data.context_length_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ),
    ):
        from src.data.context_length_llm_research import research_context_length_inputs
        # Should not raise
        result = research_context_length_inputs()

    assert result.get("skipped") is not True


def test_share_path_c_not_in_source():
    """Regression: _build_share_path_c_bottom_up must not be called in the research loop.

    We verify this structurally by checking the source text of the module does
    not call _build_share_path_c_bottom_up() anywhere other than its own
    definition line.
    """
    import ast
    src_path = Path("src/data/context_length_llm_research.py")
    source = src_path.read_text()
    tree = ast.parse(source)

    call_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "_build_share_path_c_bottom_up":
                call_sites.append(node.lineno)

    assert call_sites == [], (
        f"_build_share_path_c_bottom_up() is called at line(s) {call_sites} "
        f"but should have been removed in v3.8.0"
    )


def test_share_path_c_call_removed_from_research_loop():
    """Regression: the string '_call_claude_with_search(_build_share_path_c'
    must not appear in the research function body."""
    src_path = Path("src/data/context_length_llm_research.py")
    source = src_path.read_text()
    assert "_call_claude_with_search(_build_share_path_c" not in source, (
        "Path C volume shares API call was not removed from the research loop"
    )


def test_quarterly_cron_in_workflow():
    """Verify the context_length_research workflow uses the quarterly cron."""
    wf = Path(".github/workflows/context_length_research.yml").read_text()
    assert "1,4,7,10" in wf, "Quarterly cron (months 1,4,7,10) not found in workflow"
    assert "1,3,5,7,9,11" not in wf, "Old bi-monthly cron still present in workflow"


def test_staleness_guard_constant_is_sixty():
    """STALENESS_GUARD_DAYS must be 60."""
    from src.data.context_length_llm_research import STALENESS_GUARD_DAYS
    assert STALENESS_GUARD_DAYS == 60


def test_force_run_bypasses_staleness_guard(tmp_path):
    """FORCE_RUN=true must bypass the staleness guard and proceed to calibration."""
    state = _make_state(days_ago=5)  # recent — guard would normally fire
    state_file = tmp_path / "context_length_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.context_length_llm_research.CONTEXT_STATE_FILE", state_file),
        patch.dict("os.environ", {"FORCE_RUN": "true"}),
        patch(
            "src.data.context_length_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ) as mock_cal,
    ):
        from src.data.context_length_llm_research import research_context_length_inputs
        result = research_context_length_inputs()

    assert result.get("skipped") is not True, "FORCE_RUN=true should bypass the guard"
    mock_cal.assert_called_once()


def test_force_run_false_does_not_bypass(tmp_path):
    """FORCE_RUN=false must NOT bypass the staleness guard."""
    state = _make_state(days_ago=5)
    state_file = tmp_path / "context_length_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.context_length_llm_research.CONTEXT_STATE_FILE", state_file),
        patch.dict("os.environ", {"FORCE_RUN": "false"}),
        patch("src.data.context_length_llm_research._call_claude_with_search") as mock_api,
    ):
        from src.data.context_length_llm_research import research_context_length_inputs
        result = research_context_length_inputs()

    assert result.get("skipped") is True
    mock_api.assert_not_called()


def test_force_run_workflow_input_present():
    """context_length_research.yml must expose force_run as a workflow_dispatch input."""
    wf = Path(".github/workflows/context_length_research.yml").read_text()
    assert "force_run" in wf
    assert "FORCE_RUN" in wf
