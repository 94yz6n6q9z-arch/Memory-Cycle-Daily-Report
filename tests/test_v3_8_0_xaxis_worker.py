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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_arch_state(days_ago: int | None = None, worker: dict | None = None) -> dict:
    state: dict = {"_metadata": {}, "open_models": [], "closed_models": []}
    if days_ago is not None:
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        state.setdefault("llm_worker_frontier", {})["last_run_at"] = ts
    if worker is not None:
        state["llm_worker_frontier"] = worker
    return state


def _minimal_path_a(name: str, kv: float, verified: bool = True) -> dict:
    url = "https://arxiv.org/abs/2412.12345" if verified else "https://arxiv.org/abs/2509.XXXXX"
    return {
        "candidates": [{
            "name": name,
            "release_date": "2024-12-26",
            "architecture_family": "MLA",
            "num_layers": 61,
            "arch_params": {},
            "bytes_per_element": 1,
            "kv_kb_per_token": kv * 1024 * 1024 / 1_000_000,
            "kv_gb_per_1m_tokens": kv,
            "source_url": url,
            "source_verified": verified,
            "confidence": "high",
        }]
    }


def _minimal_path_b(name: str, kv: float) -> dict:
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


def test_all_candidates_participate_regardless_of_url_verification(tmp_path):
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
    path_b = {"candidates": []}

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a, path_b],
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


# ── Floor application ──────────────────────────────────────────────────────────

def test_floor_applied_when_kv_below_threshold(tmp_path):
    """When best candidate KV < 4.0 GB/1M, committed value must be 4.0."""
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    path_a = _minimal_path_a("LowKV Model", kv=1.5, verified=True)
    path_b = _minimal_path_b("LowKV Model", kv=2.0)

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a, path_b],
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    worker = reloaded.get("llm_worker_frontier", {})
    assert worker.get("floor_applied") is True
    assert worker.get("committed_kv_gb_per_1m") == 4.0


def test_floor_not_applied_when_kv_above_threshold(tmp_path):
    """When best candidate KV >= 4.0 GB/1M, committed value equals raw KV."""
    state_file = tmp_path / "inference_arch_state.json"
    state_file.write_text(json.dumps({"_metadata": {}, "open_models": []}))

    path_a = _minimal_path_a("HighKV Model", kv=32.0, verified=True)
    path_b = _minimal_path_b("HighKV Model", kv=35.0)

    with (
        patch("src.data.inference_arch_llm_research.ARCH_STATE_FILE", state_file),
        patch(
            "src.data.inference_arch_llm_research._run_calibration_check",
            return_value=(True, {"calibration_status": "passed"}),
        ),
        patch(
            "src.data.inference_arch_llm_research._call_claude_with_search",
            side_effect=[path_a, path_b],
        ),
    ):
        from src.data.inference_arch_llm_research import research_inference_arch
        result = research_inference_arch()

    reloaded = json.loads(state_file.read_text())
    worker = reloaded.get("llm_worker_frontier", {})
    assert worker.get("floor_applied") is False
    # max(path_B=35, mean(32,35)=33.5) = 35.0
    assert worker.get("committed_kv_gb_per_1m") == 35.0


# ── Synthesis path-B max-privilege ────────────────────────────────────────────

def test_synthesis_prefers_path_b_over_mean():
    """max(path_B, mean(A,B)) must give path_B when path_B > mean."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = _minimal_path_a("TestModel", kv=10.0, verified=True)
    path_b = _minimal_path_b("TestModel", kv=30.0)  # B > mean(10,30)=20 → B wins

    result = _synthesize_candidates(path_a, path_b)
    assert result["status"] == "committed"
    # max(30, mean(10,30)) = max(30, 20) = 30
    assert result["committed_kv_gb_per_1m"] == 30.0


def test_synthesis_uses_mean_when_path_b_below():
    """max(path_B, mean(A,B)) must give mean when path_B < mean."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = _minimal_path_a("TestModel", kv=30.0, verified=True)
    path_b = _minimal_path_b("TestModel", kv=10.0)  # B < mean(30,10)=20 → mean wins

    result = _synthesize_candidates(path_a, path_b)
    assert result["status"] == "committed"
    # max(10, mean(30,10)) = max(10, 20) = 20
    assert result["committed_kv_gb_per_1m"] == 20.0


def test_synthesis_selects_minimum_kv_among_verified():
    """With multiple verified candidates, the one with the lowest KV must be selected."""
    from src.data.inference_arch_llm_research import _synthesize_candidates

    path_a = {
        "candidates": [
            {**_minimal_path_a("ModelA", kv=30.0)["candidates"][0]},
            {**_minimal_path_a("ModelB", kv=10.0)["candidates"][0],
             "name": "ModelB", "source_url": "https://arxiv.org/abs/2501.11111"},
        ]
    }
    path_b = {"candidates": []}

    result = _synthesize_candidates(path_a, path_b)
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
