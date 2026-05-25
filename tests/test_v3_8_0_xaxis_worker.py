"""Tests for v3.8.0 X-axis inference arch LLM research worker and dhcr.py shim.

Covers:
  - Staleness guard (skip/run logic)
  - Calibration abort on failure
  - Placeholder URL filtering
  - Floor application (committed = max(raw, 4.0))
  - Synthesis path-B max-privilege
  - Synthesis selects minimum-KV verified candidate
  - dhcr.py shim prefers llm_worker_frontier over open_models
  - dhcr.py shim falls back to open_models when worker absent or malformed
  - dhcr.py shim falls back gracefully on malformed worker entry
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def empty_anchor_catalog(monkeypatch):
    """Isolate unit tests from the v3.8.6 anchor safety net by patching
    ANCHOR_CATALOG to []. Tests that explicitly verify anchor behavior
    should NOT use this fixture."""
    monkeypatch.setattr(
        "src.data.inference_arch_llm_research.ANCHOR_CATALOG", []
    )
    yield



# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_arch_state(days_ago: int | None = None, worker: dict | None = None) -> dict:
    state: dict = {"_metadata": {}, "open_models": [], "closed_models": []}
    if days_ago is not None:
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        state.setdefault("llm_worker_frontier", {})["last_run_at"] = ts
    if worker is not None:
        state["llm_worker_frontier"] = worker
    return state


def _minimal_path_a(name: str, kv: float, verified: bool = True,
                    maturity: str = "proven") -> dict:
    """v3.8.6 schema: kv_gb_per_1m (not kv_gb_per_1m_tokens), maturity field,
    family (not architecture_family). Backward compat with old key names
    preserved by synthesis."""
    url = "https://arxiv.org/abs/2412.12345" if verified else "https://arxiv.org/abs/2509.XXXXX"
    return {
        "candidates": [{
            "name": name,
            "from_anchor_catalog": True,
            "family": "MLA",
            "precision": "FP8",
            "release_date": "2024-12-26",
            "kv_gb_per_1m": kv,
            "computation_path": "anchor",
            "computation_note": "test fixture",
            "maturity": maturity,
            "source_url": url,
            "confidence": "high",
        }]
    }


def _minimal_path_b(name: str, kv: float) -> dict:
    """Path B fixture retained only for legacy two-path tests (now unused
    by the worker but referenced in older test scaffolding)."""
    return {
        "candidates": [{
            "name": name,
            "release_date": "2024-12-26",
            "kv_gb_per_1m_tokens": kv,
            "estimation_method": "cost_curve_inflection",
            "data_sources": [],
            "derivation_steps": "mocked",
            "confidence": "medium",
        }]
    }


# ── Staleness guard ────────────────────────────────────────────────────────────

def test_xaxis_staleness_guard_skips_when_recent(tmp_path):
    state = _make_arch_state()
    ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    state["llm_worker_frontier"] = {"last_run_at": ts}
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch("src.data.inference_arch_llm_research._call_claude_with_search") as mock_api,
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    assert result.get("skipped") is True
    mock_api.assert_not_called()


def test_xaxis_staleness_guard_runs_when_stale(tmp_path):
    state = _make_arch_state()
    ts = (datetime.now(timezone.utc) - timedelta(days=70)).isoformat()
    state["llm_worker_frontier"] = {"last_run_at": ts}
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    assert result.get("skipped") is not True


def test_xaxis_staleness_guard_runs_when_no_prior(tmp_path):
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    assert result.get("skipped") is not True


# ── Calibration ────────────────────────────────────────────────────────────────

def test_xaxis_calibration_abort_prevents_state_write(tmp_path):
    """When calibration fails, state/inference_arch_state.json must not change."""
    state = {"_metadata": {}, "open_models": [], "sentinel": "unchanged"}
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "failed_outside_tolerance",
                                  "worker_derived_gb_per_1m": 99.0}),
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    assert reloaded.get("sentinel") == "unchanged"
    assert result.get("calibration_passed") is False
    assert result.get("committed") == []


# ── Placeholder URL filtering ──────────────────────────────────────────────────

def test_placeholder_url_detected():
    from src.data.inference_arch_llm_research import _is_placeholder_url
    assert _is_placeholder_url("https://arxiv.org/abs/2509.XXXXX") is True
    assert _is_placeholder_url("https://arxiv.org/abs/2604.XXXXX") is True
    assert _is_placeholder_url("https://arxiv.org/abs/2412.19437") is False
    assert _is_placeholder_url("https://ai.meta.com/blog/llama-4/") is False
    assert _is_placeholder_url("") is True


def test_all_candidates_participate_regardless_of_url_verification(tmp_path, empty_anchor_catalog):
    """All worker-found candidates must participate in frontier selection.

    URL verification is provenance only — not a selection gate. The worker's
    research process (Path A finding architecture params, Path B finding
    economic data) is itself the verification. Excluding unverified candidates
    causes fallback to older confirmed models rather than tracking the frontier.
    """
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    # Model A: 30 GB/1M, verified URL
    # Model B: 10 GB/1M, placeholder URL  ← should WIN (lower KV = frontier)
    path_a = {
        "candidates": [
            {
                "name": "Model A",
                "release_date": "2025-01-01",
                "architecture_family": "MLA",
                "num_layers": 61,
                "arch_params": {},
                "bytes_per_element": 1,
                "kv_kb_per_token": 31.5,
                "kv_gb_per_1m_tokens": 30.0,
                "source_url": "https://arxiv.org/abs/2412.19437",
                "source_verified": True,
                "confidence": "high",
            },
            {
                "name": "Model B",
                "release_date": "2025-06-01",
                "architecture_family": "MLA",
                "num_layers": 64,
                "arch_params": {},
                "bytes_per_element": 0.5,
                "kv_kb_per_token": 10.5,
                "kv_gb_per_1m_tokens": 10.0,
                "source_url": "https://arxiv.org/abs/2506.XXXXX",
                "source_verified": False,
                "confidence": "low",
            },
        ]
    }

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a],
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    worker = reloaded.get("llm_worker_frontier", {})
    assert worker.get("frontier_model_name") == "Model B", (
        f"Expected Model B (10 GB/1M, lower KV = efficiency frontier) to be selected "
        f"regardless of placeholder URL, got {worker.get('frontier_model_name')}"
    )


# ── Maturity-degradation (v3.8.6) ────────────────────────────────────────────

def test_paper_claim_kv_is_degraded_by_factor_2(tmp_path, empty_anchor_catalog):
    """v3.8.6 — a paper-claim entry at raw=3.3 must be committed at 6.6
    (raw × 2.0 paper-claim degradation factor). No floor applied."""
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    path_a = _minimal_path_a("DeepSeek V4-Pro", kv=3.3, verified=True,
                             maturity="paper-claim")

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a],
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    worker = reloaded.get("llm_worker_frontier", {})
    assert worker.get("maturity") == "paper-claim"
    assert worker.get("degradation_factor") == 2.0
    assert worker.get("raw_kv_gb_per_1m") == 3.3
    assert worker.get("committed_kv_gb_per_1m") == 6.6


def test_proven_kv_passes_through_unchanged(tmp_path, empty_anchor_catalog):
    """v3.8.6 — a proven entry at raw=35.0 must be committed at 35.0
    (raw × 1.0 proven degradation factor). No floor applied."""
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    path_a = _minimal_path_a("Proven Model", kv=35.0, verified=True,
                             maturity="proven")

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a],
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    worker = reloaded.get("llm_worker_frontier", {})
    assert worker.get("maturity") == "proven"
    assert worker.get("degradation_factor") == 1.0
    assert worker.get("raw_kv_gb_per_1m") == 35.0
    assert worker.get("committed_kv_gb_per_1m") == 35.0


# ── Single-path synthesis behavior (v3.8.3) ──────────────────────────────────

def test_synthesis_passes_through_path_a_value(empty_anchor_catalog):
    """v3.8.6 — synthesis applies maturity degradation. For a proven entry,
    raw × 1.0 = raw, so committed = raw."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = _minimal_path_a("TestModel", kv=30.0, verified=True,
                             maturity="proven")

    result = _synthesize_candidates(path_a)
    assert result["status"] == "committed"
    assert result["committed_kv_gb_per_1m"] == 30.0
    assert "maturity_degradation" in result["synthesis_rule"]


def test_synthesis_filters_implausibly_low_values(empty_anchor_catalog):
    """v3.8.3 — values < 1.0 GB/1M (new plausibility floor) are excluded."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [
            {**_minimal_path_a("RealisticModel", kv=30.0)["candidates"][0]},
            {**_minimal_path_a("ImplausibleModel", kv=0.05)["candidates"][0],
             "name": "ImplausibleModel"},
        ]
    }

    result = _synthesize_candidates(path_a)
    assert result["status"] == "committed"
    # ImplausibleModel filtered → RealisticModel selected
    assert result["frontier_model_name"] == "RealisticModel"


def test_synthesis_selects_minimum_kv_across_all_candidates(empty_anchor_catalog):
    """With multiple candidates, the one with the lowest KV is selected
    regardless of source URL verification status (v3.8.2 behavior preserved)."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [
            {**_minimal_path_a("ModelA", kv=30.0)["candidates"][0]},
            {**_minimal_path_a("ModelB", kv=10.0)["candidates"][0],
             "name": "ModelB", "source_url": "https://arxiv.org/abs/2501.11111"},
        ]
    }

    result = _synthesize_candidates(path_a)
    assert result["status"] == "committed"
    assert result["frontier_model_name"] == "ModelB"  # 10.0 < 30.0


# ── dhcr.py shim ──────────────────────────────────────────────────────────────

def test_dhcr_shim_prefers_worker_result():
    """compute_frontier_kv must return llm_worker_frontier when present and valid."""
    from src.analysis.dhcr import compute_frontier_kv

    arch_yaml = {
        "llm_worker_frontier": {
            "committed_kv_kb_per_token": 34.3,
            "frontier_model_name": "DeepSeek V3",
            "arch_family": "MLA",
            "frontier_model_release_date": "2024-12-26",
        },
        "open_models": [
            {
                "name": "Llama 2 70B",
                "release_date": "2023-07-18",
                "architecture_family": "GQA",
                "num_layers": 80,
                "num_kv_heads": 8,
                "head_dim": 128,
                "bytes_per_element": 2,
                "compression_factor": 1.0,
                "kv_kb_per_token": 320,
            }
        ],
    }
    result = compute_frontier_kv(arch_yaml)
    assert result["kv_kb_per_token"] == 34.3
    assert result["model_name"] == "DeepSeek V3"
    assert result["source"] == "llm_worker_frontier"


def test_dhcr_shim_falls_back_to_open_models():
    """compute_frontier_kv must use open_models when llm_worker_frontier is absent."""
    from src.analysis.dhcr import compute_frontier_kv
    from datetime import date

    arch_yaml = {
        "open_models": [
            {
                "name": "Llama 2 70B",
                "release_date": "2023-07-18",
                "architecture_family": "GQA",
                "num_layers": 80,
                "num_kv_heads": 8,
                "head_dim": 128,
                "bytes_per_element": 2,
                "compression_factor": 1.0,
                "kv_kb_per_token": 320,
            }
        ],
    }
    result = compute_frontier_kv(arch_yaml)
    assert result["model_name"] == "Llama 2 70B"
    assert result["source"] == "open_models_catalog"


def test_dhcr_shim_falls_back_on_malformed_worker():
    """compute_frontier_kv must fall back gracefully when worker entry is malformed."""
    from src.analysis.dhcr import compute_frontier_kv

    arch_yaml = {
        "llm_worker_frontier": {
            "committed_kv_kb_per_token": "not-a-number",  # malformed
        },
        "open_models": [
            {
                "name": "Fallback Model",
                "release_date": "2023-07-18",
                "architecture_family": "GQA",
                "num_layers": 80,
                "num_kv_heads": 8,
                "head_dim": 128,
                "bytes_per_element": 2,
                "compression_factor": 1.0,
                "kv_kb_per_token": 320,
            }
        ],
    }
    result = compute_frontier_kv(arch_yaml)
    assert result["model_name"] == "Fallback Model"
    assert result["source"] == "open_models_catalog"


def test_dhcr_shim_falls_back_on_empty_worker():
    """compute_frontier_kv must fall back when llm_worker_frontier has no committed value."""
    from src.analysis.dhcr import compute_frontier_kv

    arch_yaml = {
        "llm_worker_frontier": {},  # no committed_kv_kb_per_token
        "open_models": [
            {
                "name": "Fallback Model",
                "release_date": "2023-07-18",
                "architecture_family": "GQA",
                "num_layers": 80,
                "num_kv_heads": 8,
                "head_dim": 128,
                "bytes_per_element": 2,
                "compression_factor": 1.0,
                "kv_kb_per_token": 320,
            }
        ],
    }
    result = compute_frontier_kv(arch_yaml)
    assert result["source"] == "open_models_catalog"


# ── Workflow presence check ────────────────────────────────────────────────────

def test_inference_arch_workflow_exists():
    """inference_arch_research.yml must exist in .github/workflows/."""
    wf = Path(".github/workflows/inference_arch_research.yml")
    assert wf.exists(), "inference_arch_research.yml not found"


def test_inference_arch_workflow_quarterly_cron():
    """The X-axis workflow must use the quarterly cron."""
    wf = Path(".github/workflows/inference_arch_research.yml").read_text()
    assert "1,4,7,10" in wf


def test_xaxis_force_run_bypasses_staleness_guard(tmp_path):
    """FORCE_RUN=true must bypass the X-axis staleness guard."""
    state = _make_arch_state()
    ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    state["llm_worker_frontier"] = {"last_run_at": ts}
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch.dict("os.environ", {"FORCE_RUN": "true"}),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(False, {"calibration_status": "mocked_abort"}),
        ) as mock_cal,
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    assert result.get("skipped") is not True, "FORCE_RUN=true should bypass the guard"
    mock_cal.assert_called_once()


def test_xaxis_force_run_false_does_not_bypass(tmp_path):
    """FORCE_RUN=false must NOT bypass the X-axis staleness guard."""
    state = _make_arch_state()
    ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    state["llm_worker_frontier"] = {"last_run_at": ts}
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps(state))

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch.dict("os.environ", {"FORCE_RUN": "false"}),
        patch("src.data.inference_arch_llm_research._call_claude_with_search") as mock_api,
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    assert result.get("skipped") is True
    mock_api.assert_not_called()


def test_xaxis_force_run_workflow_input_present():
    """inference_arch_research.yml must expose force_run as a workflow_dispatch input."""
    wf = Path(".github/workflows/inference_arch_research.yml").read_text()
    assert "force_run" in wf
    assert "FORCE_RUN" in wf


def test_v3_8_4_uses_streaming_api(monkeypatch):
    """v3.8.4 — _invoke_and_extract must use messages.stream() not messages.create().

    The Anthropic SDK enforces a 10-minute timeout on non-streaming requests.
    Path A with the rigorous prompt + web search agentic loop reliably exceeded
    this on retry. Switching to streaming removes the timeout.
    """
    from unittest.mock import MagicMock, patch

    # Mock the env var and config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Build a fake stream context manager that returns a fake message
    fake_message = MagicMock()
    fake_block = MagicMock()
    fake_block.text = '{"as_of_date": "2026-05-25", "method": "vendor_disclosures", "candidates": []}'
    fake_message.content = [fake_block]

    fake_stream_cm = MagicMock()
    fake_stream_cm.__enter__ = MagicMock(return_value=MagicMock(
        get_final_message=MagicMock(return_value=fake_message)
    ))
    fake_stream_cm.__exit__ = MagicMock(return_value=False)

    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=fake_stream_cm)
    # If anyone calls messages.create, this attribute exists but should not be invoked
    fake_client.messages.create = MagicMock(side_effect=AssertionError(
        "messages.create() was called — v3.8.4 must use messages.stream()"
    ))

    import anthropic
    with patch.object(anthropic, "Anthropic", return_value=fake_client):
        from src.data.inference_arch_llm_research import _invoke_and_extract
        result = _invoke_and_extract("test prompt", max_tokens=4096)

    fake_client.messages.stream.assert_called_once()
    fake_client.messages.create.assert_not_called()
    assert result["method"] == "vendor_disclosures"


def test_v3_8_4_path_a_prompt_has_final_reminder():
    """v3.8.4 — Path A prompt must end with the FINAL REMINDER block
    that overrides the model's urge to narrate before producing JSON.
    Preserved through v3.8.6 prompt restructure.
    """
    from src.data.inference_arch_llm_research import _build_path_a_vendor_prompt
    prompt = _build_path_a_vendor_prompt()
    assert "FINAL REMINDER" in prompt
    # The directive may wrap across lines; collapse whitespace for the check
    collapsed = " ".join(prompt.split())
    assert "VERY FIRST output character must be the opening brace" in collapsed


# ── v3.8.5 tests ─────────────────────────────────────────────────────────────

def test_v3_8_5_retry_max_tokens_capped_at_32k():
    """Retry doubling must cap at 32K to avoid exceeding Sonnet streaming limit."""
    import inspect
    from src.data.inference_arch_llm_research import _call_claude_with_search
    src = inspect.getsource(_call_claude_with_search)
    assert "min(max_tokens * 2, 32768)" in src, (
        "Retry max_tokens must be capped at 32768"
    )


# ── v3.8.6 tests ─────────────────────────────────────────────────────────────

def test_v3_8_6_anchor_catalog_present_in_prompt():
    """v3.8.6 — Path A prompt embeds the anchor catalog so Sonnet has known-good
    baseline values rather than recomputing every entry from scratch."""
    from src.data.inference_arch_llm_research import _build_path_a_vendor_prompt
    prompt = _build_path_a_vendor_prompt()
    assert "ANCHOR CATALOG" in prompt
    assert "return the kv_gb_per_1m value\nVERBATIM" in prompt
    # Specific known-good anchors must appear
    assert "DeepSeek V3" in prompt
    assert "Llama 4 Maverick" in prompt
    assert "DeepSeek V4-Pro" in prompt
    # Maturity classifications must appear
    assert "proven" in prompt
    assert "paper-claim" in prompt


def test_v3_8_6_maturity_degradation_factors_defined():
    """v3.8.6 — maturity factors must be defined with the expected values."""
    from src.data.inference_arch_llm_research import MATURITY_DEGRADATION_FACTORS
    assert MATURITY_DEGRADATION_FACTORS["proven"] == 1.0
    assert MATURITY_DEGRADATION_FACTORS["emerging"] == 1.5
    assert MATURITY_DEGRADATION_FACTORS["paper-claim"] == 2.0
    assert MATURITY_DEGRADATION_FACTORS["preview"] == 2.5


def test_v3_8_6_no_floor_in_module():
    """v3.8.6 — KV_FLOOR_GB_PER_1M must not be importable. Conservatism
    comes from maturity-degradation, not a post-hoc floor."""
    import src.data.inference_arch_llm_research as mod
    assert not hasattr(mod, "KV_FLOOR_GB_PER_1M"), (
        "KV_FLOOR_GB_PER_1M was removed in v3.8.6 — degradation replaces floor"
    )


def test_v3_8_6_unknown_maturity_defaults_to_paper_claim(empty_anchor_catalog):
    """v3.8.6 — if a candidate has unknown maturity, synthesis defaults to
    paper-claim (degradation factor 2.0) rather than crashing or trusting it."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [{
            "name": "MysteryModel",
            "family": "MLA",
            "precision": "FP8",
            "release_date": "2026-05-01",
            "kv_gb_per_1m": 10.0,
            "maturity": "some-bogus-value",
            "source_url": "https://example.com/paper",
            "confidence": "low",
        }]
    }
    result = _synthesize_candidates(path_a)
    assert result["status"] == "committed"
    assert result["maturity"] == "paper-claim"  # defaulted
    assert result["degradation_factor"] == 2.0
    assert result["committed_kv_gb_per_1m"] == 20.0  # 10 × 2.0


def test_v3_8_6_frontier_selects_min_effective_not_min_raw(empty_anchor_catalog):
    """v3.8.6 — frontier is min(effective_kv) = min(raw × degradation),
    NOT min(raw). A paper-claim entry at raw=3.3 (effective=6.6) loses to
    a proven entry at raw=5.0 (effective=5.0)."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [
            {"name": "PaperBragger", "family": "novel:DSA", "precision": "FP8",
             "release_date": "2026-05-01", "kv_gb_per_1m": 3.3,
             "maturity": "paper-claim",
             "source_url": "https://example.com/paper1", "confidence": "medium"},
            {"name": "ProvenSolid", "family": "MLA", "precision": "FP8",
             "release_date": "2025-01-01", "kv_gb_per_1m": 5.0,
             "maturity": "proven",
             "source_url": "https://example.com/paper2", "confidence": "high"},
        ]
    }
    result = _synthesize_candidates(path_a)
    # PaperBragger: 3.3 × 2.0 = 6.6 effective
    # ProvenSolid:   5.0 × 1.0 = 5.0 effective
    # ProvenSolid wins.
    assert result["frontier_model_name"] == "ProvenSolid"
    assert result["committed_kv_gb_per_1m"] == 5.0


# ── v3.8.6 anchor safety net tests ───────────────────────────────────────────

def test_v3_8_6_anchor_injection_when_sonnet_omits_anchors():
    """If Sonnet returns NO candidates, synthesis must inject all anchors
    from ANCHOR_CATALOG as a safety net. Frontier should be V4-Pro at
    raw=3.3 × 2.0 (paper-claim) = 6.6 effective GB/1M."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    empty_path_a = {"candidates": []}
    result = _synthesize_candidates(empty_path_a)

    assert result["status"] == "committed"
    # Frontier from anchor catalog must be V4-Pro
    assert "v4" in result["frontier_model_name"].lower()
    # Effective KV = raw 3.3 × paper-claim 2.0 = 6.6
    assert abs(result["committed_kv_gb_per_1m"] - 6.6) < 0.01
    # All 10 anchors should be in all_candidates
    assert len(result["all_candidates"]) == 10


def test_v3_8_6_anchor_override_when_sonnet_returns_wrong_kv():
    """If Sonnet returns DeepSeek V3 with kv=10 (wrong; catalog has 33),
    synthesis must override with catalog value 33.0."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [{
            "name": "DeepSeek V3",
            "from_anchor_catalog": True,
            "family": "MLA-MoE",
            "precision": "FP8",
            "release_date": "2024-12-26",
            "kv_gb_per_1m": 10.0,  # WRONG (catalog has 33.0)
            "maturity": "proven",
            "source_url": "https://arxiv.org/abs/2412.19437",
            "confidence": "high",
        }]
    }
    result = _synthesize_candidates(path_a)

    # Find the V3 entry in all_candidates
    v3 = None
    for k, v in result["all_candidates"].items():
        if "v3" in k and "v3 5" not in k and "v4" not in k:
            # match "deepseek v3" but not "deepseek v3 5"
            if k.strip() == "deepseek v3":
                v3 = v
                break
    assert v3 is not None, f"DeepSeek V3 not in candidates: {list(result['all_candidates'].keys())}"
    # The raw kv should have been overridden to 33.0
    assert v3["kv_gb_per_1m_raw"] == 33.0, (
        f"Expected catalog value 33.0 to override Sonnet's 10.0, "
        f"got raw={v3['kv_gb_per_1m_raw']}"
    )


def test_v3_8_6_anchor_override_handles_name_variations():
    """Anchor matching must be tolerant: 'DeepSeek-V3' or 'deepseek_v3' or
    'DeepSeek V3' should all match the catalog entry."""
    from src.data.inference_arch_llm_research import _find_anchor_by_name

    assert _find_anchor_by_name("DeepSeek V3") is not None
    assert _find_anchor_by_name("DeepSeek-V3") is not None
    assert _find_anchor_by_name("deepseek_v3") is not None
    assert _find_anchor_by_name("deepseek v3") is not None
    assert _find_anchor_by_name("DEEPSEEK V3") is not None
    # Negative case
    assert _find_anchor_by_name("DeepSeek V99") is None


def test_v3_8_6_anchor_override_does_not_apply_within_tolerance():
    """Tiny rounding differences (< 0.05 GB/1M) should NOT trigger override.
    Sonnet returning 33.001 for V3 (catalog: 33.0) is fine."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [{
            "name": "DeepSeek V3",
            "from_anchor_catalog": True,
            "family": "MLA-MoE",
            "precision": "FP8",
            "release_date": "2024-12-26",
            "kv_gb_per_1m": 33.001,  # essentially catalog
            "maturity": "proven",
            "source_url": "https://arxiv.org/abs/2412.19437",
            "confidence": "high",
        }]
    }
    result = _synthesize_candidates(path_a)
    v3 = result["all_candidates"]["deepseek v3"]
    # Should keep Sonnet's value (within tolerance)
    assert v3["kv_gb_per_1m_raw"] == 33.001


def test_v3_8_6_maverick_protection_against_recomputation():
    """The specific failure mode from v3.8.5: Sonnet returns Llama 4 Maverick
    at 22.89 GB/1M (formula misapplied by 7x; correct is 153). Synthesis
    must override to the catalog value 153.0."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [{
            "name": "Llama 4 Maverick",
            "from_anchor_catalog": False,  # Sonnet may forget the flag
            "family": "GQA-MoE",
            "precision": "FP8",
            "release_date": "2026-04-05",
            "kv_gb_per_1m": 22.89,  # THE WRONG VALUE FROM v3.8.5
            "maturity": "proven",
            "source_url": "https://ai.meta.com/blog/llama-4/",
            "confidence": "medium",
        }]
    }
    result = _synthesize_candidates(path_a)
    maverick = result["all_candidates"]["llama 4 maverick"]
    # Must be overridden to catalog value 153
    assert maverick["kv_gb_per_1m_raw"] == 153.0, (
        f"Maverick must be overridden from Sonnet's wrong 22.89 to catalog 153.0, "
        f"got {maverick['kv_gb_per_1m_raw']}"
    )


def test_v3_8_6_end_to_end_expected_committed_value(tmp_path):
    """End-to-end: simulate a realistic v3.8.6 run where Sonnet returns
    the anchor catalog correctly. Verify final committed value lands at
    V4-Pro 6.6 GB/1M (→ R3 workload → R4 market with +1)."""
    import json
    from unittest.mock import patch
    from src.data.inference_arch_llm_research import ANCHOR_CATALOG

    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    # Build a realistic path_a output: all 10 anchors returned verbatim
    path_a = {
        "as_of_date": "2026-05-25",
        "anchor_catalog_used": "2026-05-25",
        "candidates": [
            {
                "name": a["name"],
                "from_anchor_catalog": True,
                "family": a["family"],
                "precision": a["precision"],
                "release_date": "2024-01-01",  # whatever
                "kv_gb_per_1m": a["kv_gb_per_1m"],
                "computation_path": "anchor",
                "computation_note": a["formula_check"],
                "maturity": a["maturity"],
                "source_url": a["source"],
                "confidence": "high",
            }
            for a in ANCHOR_CATALOG
        ],
        "anchor_promotion_suggestions": [],
        "confidence": "high",
    }

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a],
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    worker = reloaded.get("llm_worker_frontier", {})

    assert "v4" in worker.get("frontier_model_name", "").lower()
    assert worker.get("maturity") == "paper-claim"
    assert worker.get("degradation_factor") == 2.0
    assert worker.get("raw_kv_gb_per_1m") == 3.3
    assert abs(worker.get("committed_kv_gb_per_1m") - 6.6) < 0.01

    # Confirm DHCR band classification
    from src.analysis.dhcr import classify_kv_band
    band = classify_kv_band(worker["committed_kv_gb_per_1m"])
    assert band["name"] == "R3", f"Expected R3 (4-16 GB/1M), got {band['name']}"
