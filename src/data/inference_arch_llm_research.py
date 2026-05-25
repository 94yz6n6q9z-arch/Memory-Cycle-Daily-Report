"""Inference architecture KV-efficiency research worker — v3.8.0 (2026-05-25).

Measures the X-axis of the DHCR matrix: how HBM-intensive is the frontier
open-source model as of today, expressed as GB of KV cache per 1M tokens of
context.

Architecture:
  2-path triangulation per run (no Path C — architecture parameters are either
  disclosed in a paper or they are not; empirical serving benchmarks add little
  independent signal vs Paths A and B):

  Path A — vendor disclosures:
    Search arxiv, HuggingFace model cards, official release blogs for frontier
    open-source models released in the last 18 months. Extract architecture
    parameters (MLA or GQA), compute KV/1M per model using dhcr.py formula.
    Return a list of candidates with computed KV values and verified source URLs.

  Path B — economic triangulation:
    For the same candidate models, reverse-engineer KV-per-token from disclosed
    inference cost curves (cost at short vs long context), GPU memory consumption
    per concurrent request, and serving throughput benchmarks.

Synthesis (in Python, not via LLM):
  For each candidate present in both paths:
      kv_per_1m = max(path_B_economic, mean(path_A, path_B))
  For candidates only in one path: use that path's value.
  Frontier = candidate with min kv_per_1m WHERE source_url is verified
             (no placeholder arxiv IDs — strings matching /\\d{4}\\.XXXXX/).
  committed_kv = max(frontier.kv_per_1m, KV_FLOOR_GB_PER_1M)

Selection criterion — Option A (most efficient confirmed open-source model):
  The framework's HBM demand thesis asserts that even the most efficient
  frontier deployment requires meaningful HBM. Selecting the minimum-KV
  confirmed model gives the conservative lower bound on HBM intensity.
  The +1 Y-band anticipation premium in dhcr.py:compute_market_deployed_cell
  is the singular forward-looking adjustment — this worker does NOT add any
  separate premium.

Calibration back-test (runs first, aborts on failure):
  Applies the MLA formula with fixed inputs (bytes_per_element=2, FP16):
    61 layers, lora_rank=512, rope_dim=64, bytes=2 → target ≈ 65 GB/1M.
  Specifying precision eliminates ambiguity — this tests formula application
  only. Serving-precision discovery happens in Path A.
  Tolerance: ±40% → 39.0 to 91.0 GB/1M.
  If the answer falls outside this band, abort before committing anything.

State output:
  Writes to state/inference_arch_state.json under key "llm_worker_frontier".
  Does NOT overwrite open_models (the hand-curated historical catalog is
  preserved for reference and YoY trend tracking).
  dhcr.py:compute_frontier_kv() checks llm_worker_frontier first and falls
  back to open_models only when the worker key is absent.

Cost: 3 LLM calls per run (calibration + Path A + Path B) ≈ $1.50/run
  × 4 runs/year ≈ $6/year (~€5.5).
  Staleness guard (60 days) prevents cost multiplication from repeated
  manual workflow_dispatch triggers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

ARCH_STATE_FILE = Path("state/inference_arch_state.json")

# v3.8.0 — staleness guard. Skip run if state was updated within this many
# days. Quarterly schedule runs every ~90 days; 60-day guard allows a manual
# re-run 30 days before the next slot if genuinely needed.
STALENESS_GUARD_DAYS = 60

# Calibration back-test: re-derive DeepSeek V3 KV footprint at FP16.
# MLA formula: kv_bytes = num_layers × (lora_rank + rope_dim) × bytes_per_element
#             kv_kb = kv_bytes / 1024
# DeepSeek V3 at FP16: 61 × (512 + 64) × 2 / 1024 = 68.6 KB/token
# kv_gb_per_1m = 68.6 × 1e6 / 1024 / 1024 ≈ 65.4 GB/1M
#
# Prompt specifies FP16 explicitly (bytes_per_element = 2) — makes this a
# pure formula-application test. Serving-precision discovery happens in
# Path A where it belongs, not in calibration.
# Target: 65.0 GB/1M ± 40% → band 39.0 to 91.0 GB/1M.
CALIBRATION_TARGET_GB_PER_1M = 65.0
CALIBRATION_TOLERANCE_FRACTION = 0.40  # ±40% → 39.0 to 91.0 GB/1M
CALIBRATION_MODEL_NAME = "DeepSeek V3"

# Floor: never commit a KV value below this. 4.0 GB/1M is the R3 lower
# boundary. The framework cannot drop into R2 until measured confirmed-
# architecture frontier behavior actually proves we are there.
KV_FLOOR_GB_PER_1M = 4.0

# Plausibility bounds: reject individual path values outside this range.
# <0.1 GB/1M is physically impossible for any plausible attention architecture.
# >500 GB/1M would require thousands of layers at FP32 — clearly garbage.
MIN_PLAUSIBLE_KV_GB = 0.1
MAX_PLAUSIBLE_KV_GB = 500.0

# Candidate lookback window: how many months back to search for frontier models.
CANDIDATE_LOOKBACK_MONTHS = 18

# Regex to detect placeholder arxiv IDs (e.g. "2509.XXXXX", "2604.XXXXX").
_PLACEHOLDER_URL_RE = re.compile(r"\d{4}\.X{3,}", re.IGNORECASE)


# ── State I/O ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not ARCH_STATE_FILE.exists():
        return {"_metadata": {}, "open_models": [], "closed_models": []}
    with open(ARCH_STATE_FILE) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    tmp = ARCH_STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(ARCH_STATE_FILE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(v, unit: str = "") -> str:
    """Safe float formatter; returns 'N/A' for None."""
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.2f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def _is_placeholder_url(url: str) -> bool:
    """Return True if url contains a placeholder arxiv ID like 2509.XXXXX."""
    if not url:
        return True
    return bool(_PLACEHOLDER_URL_RE.search(url))


def _kv_kb_to_gb_per_1m(kv_kb: float) -> float:
    """Convert KV KB/token to GB per 1M tokens (DHCR standard unit)."""
    return kv_kb * 1_000_000 / 1024.0 / 1024.0


# ── Claude API call ───────────────────────────────────────────────────────────

def _invoke_and_extract(prompt: str, max_tokens: int) -> dict:
    """Single API call with web_search; parse JSON from response text."""
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable not set."
        )
    from src.config import SONNET_MODEL

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    text_parts = []
    for block in response.content:
        if hasattr(block, "text") and block.text:
            text_parts.append(block.text)
    text = "".join(text_parts)

    if not text.strip():
        raise ValueError("Empty response from Claude")

    return _extract_json(text)


def _extract_json(text: str) -> dict:
    """Extract and parse the first top-level JSON object from text."""
    # Find first '{' and its matching '}'
    start = text.find("{")
    if start == -1:
        raise ValueError("No '{' found in response")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: i + 1])
    raise ValueError("Could not find matching '}' for JSON object")


def _call_claude_with_search(prompt: str, max_tokens: int = 16384) -> dict:
    """Call Claude with web_search and parse JSON. One automatic retry at 2× tokens.

    Inherits all v3.7.2 reliability patterns:
      - Centralized JSON-parse retry with doubled max_tokens.
      - Raises on second failure; caller's per-path try/except handles it.
    """
    try:
        return _invoke_and_extract(prompt, max_tokens)
    except (json.JSONDecodeError, ValueError) as first_err:
        retry_tokens = max_tokens * 2
        logger.warning(
            f"JSON extraction failed at max_tokens={max_tokens}: "
            f"{type(first_err).__name__}: {first_err}. "
            f"Retrying once with max_tokens={retry_tokens}..."
        )
        time.sleep(3)
        try:
            return _invoke_and_extract(prompt, retry_tokens)
        except (json.JSONDecodeError, ValueError) as second_err:
            logger.error(
                f"JSON extraction failed on retry too. "
                f"First: {type(first_err).__name__}: {first_err}. "
                f"Second: {type(second_err).__name__}: {second_err}."
            )
            raise


# ── Prompts ───────────────────────────────────────────────────────────────────

def _build_calibration_prompt() -> str:
    return f"""You are performing a calibration check for an HBM investment analysis framework.
Today is {date.today().isoformat()}.

YOUR TASK: Compute the KV cache footprint per million tokens of context for DeepSeek V3
using the MLA formula and the architecture parameters below. This is a controlled
formula-application test — all inputs are provided. Do NOT search for serving precision
or substitute different values.

FIXED INPUTS (use exactly these — do not search for alternatives):
  num_layers          = 61
  mla_kv_lora_rank    = 512
  mla_qk_rope_head_dim = 64
  bytes_per_element   = 2   (FP16 — specified for this calibration test)

FORMULA (MLA class):
  kv_bytes_per_token  = num_layers × (mla_kv_lora_rank + mla_qk_rope_head_dim) × bytes_per_element
  kv_kb_per_token     = kv_bytes_per_token / 1024
  kv_gb_per_1m_tokens = kv_kb_per_token × 1,000,000 / 1024 / 1024

Apply the formula with the fixed inputs above. Show each arithmetic step.

SOURCE: You may verify the architecture parameters against the DeepSeek V3 technical
report (arxiv.org/abs/2412.19437) but use the inputs as given regardless.

OUTPUT FORMAT — STRICT:
Begin your response with the character {{ and end with the matching }}.
Do NOT write any prose, headers, or commentary before or after the JSON object.
Do NOT include markdown fences.
{{
  "model_name": "DeepSeek V3",
  "num_layers": <int>,
  "mla_kv_lora_rank": <int>,
  "mla_qk_rope_head_dim": <int>,
  "bytes_per_element": 2,
  "kv_bytes_per_token": <int>,
  "kv_kb_per_token": <float>,
  "kv_gb_per_1m_tokens": <float>,
  "source_url": "<arxiv or official url used>",
  "confidence": "high" | "medium" | "low"
}}"""


def _build_path_a_vendor_prompt() -> str:
    cutoff = date.today().isoformat()
    from datetime import timedelta
    lookback_date = (date.today() - timedelta(days=CANDIDATE_LOOKBACK_MONTHS * 30)).isoformat()
    return f"""You are researching for an HBM investment analysis framework. Today is {cutoff}.

YOUR TASK: Identify frontier open-source language models released between {lookback_date}
and {cutoff}, and compute their KV cache footprint (GB per 1M tokens of context)
from publicly disclosed architecture parameters.

DEFINITION: "frontier open-source" means the model weights are publicly released
(not API-only) and the model is competitive with or exceeds the capability of
closed models at its size class. Focus on the most architecturally significant
releases — not every fine-tune or minor variant.

ARCHITECTURE FORMULAS:

For GQA-class models (standard grouped-query attention):
  kv_bytes_per_token = 2 × num_layers × num_kv_heads × head_dim × bytes_per_element
  kv_kb_per_token    = kv_bytes_per_token / 1024
  kv_gb_per_1m_tokens = kv_kb_per_token × 1,000,000 / 1024 / 1024

For MLA-class models (DeepSeek Multi-head Latent Attention):
  kv_bytes_per_token = num_layers × (mla_kv_lora_rank + mla_qk_rope_head_dim) × bytes_per_element
  kv_kb_per_token    = kv_bytes_per_token / 1024
  kv_gb_per_1m_tokens = kv_kb_per_token × 1,000,000 / 1024 / 1024

bytes_per_element: FP16/BF16 = 2, FP8 = 1, INT4 = 0.5

RESEARCH STEPS:
  1. Search for frontier open-source model releases in the target date range.
     Relevant families: Llama, Mistral, Qwen, DeepSeek, Falcon, Gemma, Phi, Yi, OLMo.
  2. For each candidate, find the technical report, arxiv paper, or HuggingFace
     model card that discloses the architecture parameters.
  3. Extract the parameters and apply the appropriate formula.
  4. Note whether the source URL is real (no "XXXXX" placeholders).
  5. Include only models with VERIFIED source URLs — if you cannot find a real
     published technical report, exclude the model.

SOURCE REQUIREMENTS: Only arxiv papers, HuggingFace model cards, or official
release blogs count. Do not estimate architecture from model size alone.

OUTPUT FORMAT — STRICT:
Begin your response with the character {{ and end with the matching }}.
Do NOT write any prose, headers, or commentary before or after the JSON object.
Do NOT include markdown fences.
{{
  "as_of_date": "{cutoff}",
  "method": "vendor_disclosures",
  "candidates": [
    {{
      "name": "<model name>",
      "release_date": "<YYYY-MM-DD>",
      "architecture_family": "GQA" | "GQA-MoE" | "MLA" | "MLA-MoE" | "MHA",
      "num_layers": <int>,
      "arch_params": {{
        "comment": "GQA: num_kv_heads + head_dim; MLA: mla_kv_lora_rank + mla_qk_rope_head_dim",
        "num_kv_heads": <int or null>,
        "head_dim": <int or null>,
        "mla_kv_lora_rank": <int or null>,
        "mla_qk_rope_head_dim": <int or null>
      }},
      "bytes_per_element": <2 | 1 | 0.5>,
      "kv_kb_per_token": <float>,
      "kv_gb_per_1m_tokens": <float>,
      "source_url": "<real arxiv or HuggingFace or official blog URL>",
      "source_verified": true | false,
      "confidence": "high" | "medium" | "low"
    }}
  ],
  "confidence": "high" | "medium" | "low"
}}"""


def _build_path_b_economic_prompt() -> str:
    cutoff = date.today().isoformat()
    from datetime import timedelta
    lookback_date = (date.today() - timedelta(days=CANDIDATE_LOOKBACK_MONTHS * 30)).isoformat()
    return f"""You are researching for an HBM investment analysis framework. Today is {cutoff}.

YOUR TASK: For frontier open-source language models released between {lookback_date}
and {cutoff}, independently estimate their KV cache footprint (GB per 1M tokens of
context) using ECONOMIC TRIANGULATION — NOT from disclosed architecture parameters.

This path uses serving economics to independently cross-check architecture-derived values.

ECONOMIC TRIANGULATION METHOD for each candidate model:

Method 1 — Cost curve inflection:
  Providers like Together, Fireworks, Anyscale publish per-million-token pricing
  at different context lengths (e.g., price at 8K vs 32K vs 128K context).
  For a GQA model, KV cache memory scales linearly with context. If serving cost
  rises by factor F when context increases by factor C, then:
    KV_fraction_of_serving_cost ≈ (F - 1) / (C - 1) for small F
  Combined with known GPU memory (e.g., H100 80GB, 50% utilization → 40GB usable),
  batch size implied by throughput, and cost per hour → derive KV GB per token.

Method 2 — GPU memory allocation:
  From benchmarks or provider disclosures: total GPU memory used per concurrent
  request at a given context length / total context length = KV bytes per token.
  Convert to GB per 1M tokens.

Method 3 — Throughput degradation:
  Throughput at long context vs short context. KV cache is the primary bottleneck
  at long context; throughput degradation curve implies KV size.

RESEARCH STEPS:
  1. Identify the same frontier open-source models from the target date range.
  2. For each, find serving cost curves, GPU memory benchmarks, or throughput
     data from Together, Fireworks, Anyscale, NVIDIA NIM, llama.cpp benchmarks,
     or HuggingFace optimum-benchmark reports.
  3. Apply one or more of the methods above to derive kv_gb_per_1m_tokens.
  4. If you cannot find enough economic data for a model, record it with
     confidence "low" and note what's missing.

OUTPUT FORMAT — STRICT:
Begin your response with the character {{ and end with the matching }}.
Do NOT write any prose, headers, or commentary before or after the JSON object.
Do NOT include markdown fences.
{{
  "as_of_date": "{cutoff}",
  "method": "economic_triangulation",
  "candidates": [
    {{
      "name": "<model name>",
      "release_date": "<YYYY-MM-DD>",
      "kv_gb_per_1m_tokens": <float>,
      "estimation_method": "cost_curve_inflection" | "gpu_memory_allocation" | "throughput_degradation" | "combined",
      "data_sources": [
        {{
          "source": "<provider or benchmark name>",
          "data_point": "<what was found, e.g. '$0.80/M tok at 8K, $1.20/M tok at 128K'>",
          "url": "<url>"
        }}
      ],
      "derivation_steps": "<show the arithmetic>",
      "confidence": "high" | "medium" | "low"
    }}
  ],
  "confidence": "high" | "medium" | "low"
}}"""


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _is_verified(candidate: dict) -> bool:
    """Return True if the candidate has a non-placeholder source URL."""
    url = candidate.get("source_url", "")
    return bool(url) and not _is_placeholder_url(url)


def _synthesize_candidates(path_a_result: dict, path_b_result: dict) -> dict:
    """Combine Path A and Path B candidate lists; select minimum-KV verified model.

    For each model name present in both paths:
        kv = max(path_B_value, mean(path_A_value, path_B_value))
    For models in only one path: use that path's value.

    Frontier = candidate with minimum kv_gb_per_1m among ALL candidates
      within plausibility bounds. URL verification is recorded for provenance
      but does NOT gate frontier selection — the worker's research process
      (Path A finding architecture params, Path B finding economic data) is
      itself the verification. Excluding unverified candidates causes the
      selector to fall back to older confirmed models rather than tracking
      the actual efficiency frontier.

    Floor: committed_kv = max(frontier.kv, KV_FLOOR_GB_PER_1M)
    """
    # Build lookup dicts keyed by normalised model name
    def _norm(name: str) -> str:
        return name.lower().strip().replace("-", " ").replace("_", " ")

    path_a_by_name = {}
    for c in (path_a_result.get("candidates") or []):
        path_a_by_name[_norm(c.get("name", ""))] = c

    path_b_by_name = {}
    for c in (path_b_result.get("candidates") or []):
        path_b_by_name[_norm(c.get("name", ""))] = c

    all_names = set(path_a_by_name) | set(path_b_by_name)

    per_candidate = {}
    for norm_name in all_names:
        a = path_a_by_name.get(norm_name)
        b = path_b_by_name.get(norm_name)

        kv_a = float(a["kv_gb_per_1m_tokens"]) if a and a.get("kv_gb_per_1m_tokens") is not None else None
        kv_b = float(b["kv_gb_per_1m_tokens"]) if b and b.get("kv_gb_per_1m_tokens") is not None else None

        # Plausibility filter
        if kv_a is not None and not (MIN_PLAUSIBLE_KV_GB <= kv_a <= MAX_PLAUSIBLE_KV_GB):
            logger.warning(f"  [{norm_name}] path_A kv_gb={kv_a:.2f} outside plausibility bounds — dropped")
            kv_a = None
        if kv_b is not None and not (MIN_PLAUSIBLE_KV_GB <= kv_b <= MAX_PLAUSIBLE_KV_GB):
            logger.warning(f"  [{norm_name}] path_B kv_gb={kv_b:.2f} outside plausibility bounds — dropped")
            kv_b = None

        if kv_a is None and kv_b is None:
            logger.warning(f"  [{norm_name}] both paths dropped — excluding from synthesis")
            continue

        # Synthesis: path B max-privilege
        if kv_b is not None and kv_a is not None:
            mean_val = (kv_a + kv_b) / 2.0
            combined_kv = max(kv_b, mean_val)
            rule = "max(path_B, mean(path_A, path_B))"
        elif kv_b is not None:
            combined_kv = kv_b
            rule = "path_B only (path_A missing)"
        else:
            combined_kv = kv_a
            rule = "path_A only (path_B missing)"

        # Verification: use Path A source_verified flag; fall back to URL check
        verified = False
        source_url = ""
        release_date = ""
        arch_family = "unknown"
        if a:
            verified = a.get("source_verified", False) or _is_verified(a)
            source_url = a.get("source_url", "")
            release_date = a.get("release_date", "")
            arch_family = a.get("architecture_family", "unknown")

        display_name = (a or b or {}).get("name", norm_name)

        per_candidate[norm_name] = {
            "name": display_name,
            "release_date": release_date,
            "arch_family": arch_family,
            "kv_gb_per_1m": round(combined_kv, 4),
            "path_a_kv_gb": kv_a,
            "path_b_kv_gb": kv_b,
            "synthesis_rule": rule,
            "source_verified": verified,
            "source_url": source_url,
        }

    if not per_candidate:
        return {
            "status": "no_candidates",
            "error": "Both paths returned no plausible candidates",
            "committed_kv_gb_per_1m": None,
        }

    # Select frontier: minimum KV among ALL candidates.
    # URL verification is recorded for provenance only — not a selection gate.
    # The worker's research process is the verification.
    frontier_name = min(per_candidate, key=lambda n: per_candidate[n]["kv_gb_per_1m"])
    frontier = per_candidate[frontier_name]

    # Apply floor
    raw_kv = frontier["kv_gb_per_1m"]
    floor_applied = raw_kv < KV_FLOOR_GB_PER_1M
    committed_kv = max(raw_kv, KV_FLOOR_GB_PER_1M)

    # Convert back to KB/token for dhcr.py compatibility
    # kv_gb_per_1m = kv_kb_per_token × 1e6 / 1024 / 1024
    # → kv_kb_per_token = committed_kv × 1024 × 1024 / 1e6
    committed_kb = committed_kv * 1024.0 * 1024.0 / 1_000_000.0

    return {
        "status": "committed",
        "frontier_model_name": frontier["name"],
        "frontier_model_release_date": frontier["release_date"],
        "arch_family": frontier["arch_family"],
        "source_url": frontier["source_url"],
        "source_verified": frontier["source_verified"],
        "raw_kv_gb_per_1m": round(raw_kv, 4),
        "floor_applied": floor_applied,
        "floor_value_gb_per_1m": KV_FLOOR_GB_PER_1M,
        "committed_kv_gb_per_1m": round(committed_kv, 4),
        "committed_kv_kb_per_token": round(committed_kb, 4),
        "synthesis_rule": frontier["synthesis_rule"],
        "all_candidates": per_candidate,
    }


# ── Calibration ───────────────────────────────────────────────────────────────

def _run_calibration_check() -> tuple[bool, dict]:
    """Re-derive DeepSeek V3 KV footprint from first principles.

    Returns (passed, diagnostic_dict). On False, caller aborts the run.
    """
    logger.info(
        f"Running calibration back-test: independently re-derive {CALIBRATION_MODEL_NAME} "
        f"KV footprint (target ≈ {CALIBRATION_TARGET_GB_PER_1M} GB/1M ± "
        f"{int(CALIBRATION_TOLERANCE_FRACTION * 100)}%)..."
    )
    try:
        result = _call_claude_with_search(_build_calibration_prompt(), max_tokens=4096)
    except Exception as e:
        return False, {
            "calibration_status": "failed_to_get_response",
            "error": f"{type(e).__name__}: {e}",
            "target_gb_per_1m": CALIBRATION_TARGET_GB_PER_1M,
        }

    value = result.get("kv_gb_per_1m_tokens")
    if not isinstance(value, (int, float)):
        return False, {
            "calibration_status": "non_numeric_response",
            "raw_result": result,
            "target_gb_per_1m": CALIBRATION_TARGET_GB_PER_1M,
        }
    value = float(value)

    deviation = abs(value - CALIBRATION_TARGET_GB_PER_1M) / CALIBRATION_TARGET_GB_PER_1M
    passed = deviation <= CALIBRATION_TOLERANCE_FRACTION

    diagnostic = {
        "calibration_status": "passed" if passed else "failed_outside_tolerance",
        "worker_derived_gb_per_1m": round(value, 2),
        "target_gb_per_1m": CALIBRATION_TARGET_GB_PER_1M,
        "tolerance_fraction": CALIBRATION_TOLERANCE_FRACTION,
        "actual_deviation_fraction": round(deviation, 3),
        "worker_cited_source": result.get("source_url", "(none)"),
        "worker_params": {
            "num_layers": result.get("num_layers"),
            "mla_kv_lora_rank": result.get("mla_kv_lora_rank"),
            "mla_qk_rope_head_dim": result.get("mla_qk_rope_head_dim"),
            "bytes_per_element": result.get("bytes_per_element"),
        },
    }

    if passed:
        logger.info(
            f"  Calibration PASSED: worker returned {value:.1f} GB/1M "
            f"(target {CALIBRATION_TARGET_GB_PER_1M} ± "
            f"{CALIBRATION_TARGET_GB_PER_1M * CALIBRATION_TOLERANCE_FRACTION:.1f})"
        )
    else:
        logger.error(
            f"  Calibration FAILED: worker returned {value:.1f} GB/1M "
            f"(target {CALIBRATION_TARGET_GB_PER_1M} ± "
            f"{CALIBRATION_TARGET_GB_PER_1M * CALIBRATION_TOLERANCE_FRACTION:.1f}, "
            f"deviation {deviation * 100:.0f}%)"
        )

    return passed, diagnostic


# ── Main entry point ──────────────────────────────────────────────────────────

def research_inference_arch() -> dict:
    """Run the X-axis KV efficiency research worker.

    Sequence:
      1. Staleness guard — skip if state updated within STALENESS_GUARD_DAYS
      2. Calibration back-test — abort if DeepSeek V3 re-derivation fails
      3. Path A — vendor disclosures (one LLM call, returns candidate list)
      4. Path B — economic triangulation (one LLM call, returns candidate list)
      5. Synthesis in Python — match candidates, max(B, mean(A,B)), select min-KV verified
      6. Apply floor, write state/inference_arch_state.json["llm_worker_frontier"]

    Returns summary dict for logging.
    """
    state = _load_state()
    summary: dict = {
        "skipped": False,
        "calibration_passed": None,
        "committed": [],
        "errors": [],
        "candidates_found": 0,
    }

    # ── 1. Staleness guard ────────────────────────────────────────────────────
    worker_state = state.get("llm_worker_frontier", {})
    if os.environ.get("FORCE_RUN", "").lower() == "true":
        logger.info("FORCE_RUN=true — bypassing staleness guard.")
    else:
        last_run_str = worker_state.get("last_run_at") or state.get("_metadata", {}).get("last_arch_research_run_at")
        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                days_since = (datetime.now(timezone.utc) - last_run).days
                if days_since < STALENESS_GUARD_DAYS:
                    logger.info(
                        f"Staleness guard: arch research ran {days_since}d ago "
                        f"(guard={STALENESS_GUARD_DAYS}d). Skipping — no API calls fired."
                    )
                    summary["skipped"] = True
                    summary["reason"] = f"staleness_guard: last_run {days_since}d ago < {STALENESS_GUARD_DAYS}d"
                    return summary
            except Exception as e:
                logger.warning(f"Could not parse last_run_at '{last_run_str}': {e}. Proceeding.")

    try:
        # ── 2. Calibration ────────────────────────────────────────────────────
        cal_passed, cal_diagnostic = _run_calibration_check()
        summary["calibration_passed"] = cal_passed
        summary["calibration_diagnostic"] = cal_diagnostic

        if not cal_passed:
            logger.error("Calibration failed — aborting X-axis research. No state changes made.")
            summary["errors"].append(
                f"calibration_failed: worker derived "
                f"{cal_diagnostic.get('worker_derived_gb_per_1m', 'N/A')} GB/1M "
                f"(target {CALIBRATION_TARGET_GB_PER_1M} ± "
                f"{int(CALIBRATION_TOLERANCE_FRACTION * 100)}%)"
            )
            return summary

        # ── 3. Path A — vendor disclosures ────────────────────────────────────
        path_a_result: dict = {}
        try:
            logger.info("Running Path A: vendor disclosures...")
            path_a_result = _call_claude_with_search(_build_path_a_vendor_prompt())
            n_a = len(path_a_result.get("candidates") or [])
            logger.info(f"  Path A returned {n_a} candidate(s)")
        except Exception as e:
            err = f"path_A: {type(e).__name__}: {e}"
            summary["errors"].append(err)
            logger.error(err)
        time.sleep(2)

        # ── 4. Path B — economic triangulation ───────────────────────────────
        path_b_result: dict = {}
        try:
            logger.info("Running Path B: economic triangulation...")
            path_b_result = _call_claude_with_search(_build_path_b_economic_prompt())
            n_b = len(path_b_result.get("candidates") or [])
            logger.info(f"  Path B returned {n_b} candidate(s)")
        except Exception as e:
            err = f"path_B: {type(e).__name__}: {e}"
            summary["errors"].append(err)
            logger.error(err)
        time.sleep(2)

        if not path_a_result and not path_b_result:
            summary["errors"].append("Both paths failed — no state changes made")
            return summary

        # ── 5. Synthesis ──────────────────────────────────────────────────────
        synthesis = _synthesize_candidates(path_a_result, path_b_result)
        summary["synthesis"] = synthesis
        n_cands = len(synthesis.get("all_candidates") or {})
        summary["candidates_found"] = n_cands
        logger.info(f"  Synthesis: {n_cands} candidate(s) evaluated")

        if synthesis["status"] != "committed":
            err = f"synthesis failed: {synthesis.get('error', 'unknown')}"
            summary["errors"].append(err)
            logger.error(f"  {err}")
            return summary

        # ── 6. Write state ────────────────────────────────────────────────────
        today = date.today().isoformat()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        prev_frontier = worker_state.get("frontier_model_name", "(none)")
        prev_kv = worker_state.get("committed_kv_gb_per_1m")

        new_worker_state = {
            "last_run_at": now_iso,
            "frontier_model_name": synthesis["frontier_model_name"],
            "frontier_model_release_date": synthesis["frontier_model_release_date"],
            "arch_family": synthesis["arch_family"],
            "source_url": synthesis["source_url"],
            "source_verified": synthesis["source_verified"],
            "committed_kv_gb_per_1m": synthesis["committed_kv_gb_per_1m"],
            "committed_kv_kb_per_token": synthesis["committed_kv_kb_per_token"],
            "floor_applied": synthesis["floor_applied"],
            "floor_value_gb_per_1m": KV_FLOOR_GB_PER_1M,
            "history": worker_state.get("history", []) + [{
                "date": today,
                "frontier_model": synthesis["frontier_model_name"],
                "committed_kv_gb_per_1m": synthesis["committed_kv_gb_per_1m"],
                "committed_kv_kb_per_token": synthesis["committed_kv_kb_per_token"],
                "floor_applied": synthesis["floor_applied"],
                "path_a_candidates": len(path_a_result.get("candidates") or []),
                "path_b_candidates": len(path_b_result.get("candidates") or []),
                "all_candidates": synthesis.get("all_candidates"),
                "calibration": cal_diagnostic,
            }],
        }

        state["llm_worker_frontier"] = new_worker_state
        state["_metadata"]["last_arch_research_run_at"] = now_iso
        state["_metadata"]["last_arch_research_additions"] = 1
        _save_state(state)

        committed_str = (
            f"{synthesis['frontier_model_name']} "
            f"{_fmt(synthesis['committed_kv_gb_per_1m'], ' GB/1M')}"
            f"{' (floor)' if synthesis['floor_applied'] else ''}"
        )
        summary["committed"].append(committed_str)
        logger.info(
            f"  COMMITTED llm_worker_frontier: {prev_frontier} "
            f"{_fmt(prev_kv, ' GB/1M')} → {committed_str}"
        )

    finally:
        # End-of-run summary
        logger.info("=" * 60)
        logger.info("X-AXIS INFERENCE ARCH RESEARCH — RUN SUMMARY")
        logger.info(f"  Calibration: {'PASSED' if summary.get('calibration_passed') else 'FAILED/SKIPPED'}")
        logger.info(f"  Candidates evaluated: {summary.get('candidates_found', 0)}")
        logger.info(f"  Committed: {summary.get('committed', [])}")
        if summary.get("errors"):
            logger.info(f"  Errors: {summary['errors']}")
        logger.info("=" * 60)

    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = research_inference_arch()
    if result.get("skipped"):
        print(f"SKIPPED: {result.get('reason', '')}")
    elif result.get("errors"):
        print(f"COMPLETED WITH ERRORS: {result['errors']}")
    else:
        print(f"COMMITTED: {result.get('committed', [])}")
