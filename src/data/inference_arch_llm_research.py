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

    The prompt (Parts 1-6 inside _build_path_a_vendor_prompt) is intentionally
    rigorous: explicit lab-of-origin filter, explicit benchmark presence filter,
    variant collapsing rules, MoE handling rule (KV does NOT multiply by experts),
    paper-stated-vs-computed cross-check, worked numeric examples for both GQA
    and MLA, and explicit "include with nulls if you can't find params" so the
    worker tells us what's missing rather than fabricating values.

Synthesis (in Python, not via LLM):
  Frontier = candidate with min kv_gb_per_1m within plausibility bounds
             [MIN_PLAUSIBLE_KV_GB, MAX_PLAUSIBLE_KV_GB].
  URL verification is recorded for provenance but does NOT gate selection.
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

v3.8.3 history:
  - Path B (economic triangulation) was dropped. In production it returned
    implausible <0.1 GB/1M values for multiple candidates — providers do not
    consistently disclose the cost-curve-vs-context-length data needed for
    triangulation. Path A with the rigorous prompt is the authoritative path.
  - Plausibility floor raised from 0.1 to 1.0 GB/1M.

Cost: 2 LLM calls per run (calibration + Path A) ≈ $1/run × 4 runs/year ≈ $4/year.
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
# <1.0 GB/1M is implausible for any current frontier model (DeepSeek V3 at
# FP8 with MLA — the most efficient real architecture — is ~33 GB/1M).
# Sub-1 GB/1M values in production runs indicated the model giving up rather
# than producing a real estimate.
# >500 GB/1M would require thousands of layers at FP32 — clearly garbage.
MIN_PLAUSIBLE_KV_GB = 1.0
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
    """Single API call with web_search; parse JSON from response text.

    v3.8.4 — Uses streaming. The Anthropic SDK enforces a 10-minute timeout
    on non-streaming requests, which the rigorous Path A prompt (with web
    search agentic loop accumulating across multiple internal turns) reliably
    exceeds on retry. Streaming removes that timeout. Calibration calls are
    short but go through the same code path for consistency — streaming adds
    no overhead for fast calls.
    """
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable not set."
        )
    from src.config import SONNET_MODEL

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=SONNET_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    ) as stream:
        response = stream.get_final_message()

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
    return f"""You are researching for a quantitative HBM investment analysis framework.
Today is {cutoff}. Your output drives a financial model — incorrect values
flow directly into investment decisions. Precision matters more than coverage.

═══════════════════════════════════════════════════════════════════════════
PART 1 — WHAT YOU ARE LOOKING FOR
═══════════════════════════════════════════════════════════════════════════

Identify "frontier open-source language models" released between {lookback_date}
and {cutoff}. A model qualifies as a frontier open-source candidate if AND
ONLY IF it meets BOTH of these criteria:

  (A) LAB OF ORIGIN. The model comes from one of these labs that have shipped
      architecturally significant work in the last 24 months:
        - DeepSeek (deepseek-ai)
        - Meta (Llama family)
        - Alibaba (Qwen family)
        - Mistral AI
        - Z.ai (GLM family, formerly Zhipu)
        - xAI (Grok open-weights releases only)
        - Google (Gemma family — open-weights only, NOT Gemini)
        - Anthropic research releases (open-weights only, currently none)

  (B) BENCHMARK PRESENCE. The model appears in at least one of:
        - MMLU-Pro top-30 in its size class (e.g. <100B params, or MoE)
        - LiveBench top-30 overall
        - Artificial Analysis intelligence index top-30
        - lmarena.ai top-30 in Hard prompts category

  Both criteria must be met. A lab-of-origin model that no benchmark has
  picked up yet is too new/experimental. A benchmark-listed model from
  a non-listed lab is likely a fine-tune, not architecturally novel.

EXPLICITLY EXCLUDED:
  - Closed-source models (GPT, Claude, Gemini API-only, Grok-API-only)
  - Fine-tunes of other labs' base models (e.g. Nous-Hermes, WizardLM)
  - Quantized redistributions (e.g. Unsloth GGUFs of Llama)
  - Models older than {lookback_date}
  - Research-only models with no production weights released
  - Vision/multimodal-only models (text inference is the HBM driver)

═══════════════════════════════════════════════════════════════════════════
PART 2 — VARIANT HANDLING
═══════════════════════════════════════════════════════════════════════════

A "model family" can have multiple variants. Apply these rules:

  - SAME ARCHITECTURE (same num_layers, same KV head config, same precision):
    Pick ONE representative. Use the most-cited or most-deployed variant.
    Example: DeepSeek V3, V3-0324, V3.5 → ONE entry (use "DeepSeek V3.5"
    as the latest representative of the V3 architecture line).

  - DIFFERENT SIZES (different num_layers): SEPARATE entries.
    Example: Llama 4 Scout (small) and Llama 4 Maverick (large) → TWO entries.

  - DIFFERENT PRECISIONS published by the same lab for the same arch:
    Use the LOWEST-PRECISION variant the lab itself validates as production
    (typically FP8 if published, otherwise BF16). Do NOT use community
    INT4/INT8 quantizations unless the lab itself published them.

═══════════════════════════════════════════════════════════════════════════
PART 3 — KV CACHE FORMULAS (this is what you compute, per candidate)
═══════════════════════════════════════════════════════════════════════════

The KV cache stores per-token key and value tensors across all layers.
At inference, KV grows linearly with context length. We compute KV per
1 million tokens of context, in gigabytes.

CRITICAL: For MoE models, KV cache is computed ONCE per token before
expert routing. KV does NOT multiply by number of experts. The MoE
routing happens AFTER KV is written. Use the SHARED attention block's
parameters, not "total params" or "active params" arithmetic.

──────────────────────────────────────────────────────────────────────
GQA / GQA-MoE (most Llama, Mistral, Qwen2.5, Gemma):

  kv_bytes_per_token = 2 × num_layers × num_kv_heads × head_dim × bytes_per_element

  Where:
    num_layers       = transformer decoder layer count
    num_kv_heads     = NUMBER OF KEY-VALUE HEADS (the GQA group count,
                       NOT the total attention heads). For pure MHA,
                       num_kv_heads = num_attention_heads.
    head_dim         = hidden_size / num_attention_heads (often 128)
    bytes_per_element: see Part 4

  WORKED EXAMPLE — Llama 3 70B at BF16:
    num_layers = 80, num_kv_heads = 8, head_dim = 128, bytes = 2
    kv_bytes = 2 × 80 × 8 × 128 × 2 = 327,680 bytes per token
    kv_kb   = 327,680 / 1024 = 320 KB per token
    kv_gb_per_1m = 320 × 1,000,000 / 1024 / 1024 = 305.18 GB per 1M tokens

  WORKED EXAMPLE — Llama 3 70B at FP8 (if the lab published FP8):
    Same architecture, bytes = 1
    kv_bytes = 2 × 80 × 8 × 128 × 1 = 163,840 bytes per token
    kv_gb_per_1m = 152.59 GB per 1M tokens

──────────────────────────────────────────────────────────────────────
MLA / MLA-MoE (DeepSeek V2/V3/V3.5, Qwen3 with MLA, anything explicitly
labelled "Multi-head Latent Attention"):

  kv_bytes_per_token = num_layers × (mla_kv_lora_rank + mla_qk_rope_head_dim) × bytes_per_element

  Where:
    num_layers           = decoder layer count
    mla_kv_lora_rank     = the latent KV dimension (typically 512 in DeepSeek)
    mla_qk_rope_head_dim = the RoPE dimension added per token (typically 64)
    bytes_per_element    = see Part 4

  WORKED EXAMPLE — DeepSeek V3 at FP8:
    num_layers = 61, lora_rank = 512, rope_dim = 64, bytes = 1
    kv_bytes = 61 × (512 + 64) × 1 = 35,136 bytes per token
    kv_kb   = 35,136 / 1024 = 34.3 KB per token
    kv_gb_per_1m = 34.3 × 1,000,000 / 1024 / 1024 = 32.69 GB per 1M tokens

  Note: this is ~10x more efficient than Llama 3 70B at FP8 — MLA's whole
  point is dramatic KV reduction. If your MLA computation is not in this
  efficiency neighborhood, recheck your inputs.

═══════════════════════════════════════════════════════════════════════════
PART 4 — PRECISION (bytes_per_element)
═══════════════════════════════════════════════════════════════════════════

Use the SERVING PRECISION DECLARED BY THE LAB in their official release —
not community quantizations, not theoretical lower-precision variants.

  FP32 → bytes = 4   (almost never used at inference; legacy)
  BF16 → bytes = 2   (default for most pre-2024 models)
  FP16 → bytes = 2   (older default)
  FP8  → bytes = 1   (DeepSeek V3+, some 2025+ releases — must be lab-published)
  INT4 → bytes = 0.5 (rare; only if the lab explicitly publishes INT4 weights)

If the paper publishes weights in BF16 but mentions FP8 as a deployment
option without architectural support (no FP8 training, no FP8 validation):
USE BF16. Only adopt FP8 if the lab demonstrably trained or fine-tuned
at FP8 (DeepSeek V3 is the canonical example).

═══════════════════════════════════════════════════════════════════════════
PART 5 — RESEARCH PROCESS
═══════════════════════════════════════════════════════════════════════════

For each candidate that passes Part 1:

  1. Find the PRIMARY source. Acceptable in order of preference:
       (a) arxiv paper or official technical report PDF
       (b) HuggingFace model card with full config.json visible
       (c) Official lab blog post with architecture disclosure
       (d) Official GitHub config files
     NOT acceptable: third-party blog posts, news articles, leaderboard
     entries without underlying disclosure, community benchmarks.

  2. Extract the required parameters with citations to the source.

  3. Compute kv_gb_per_1m_tokens using the appropriate formula.

  4. CROSS-CHECK: If the paper itself states a "KV cache per token" number
     (some papers do — DeepSeek V3 paper does, Llama papers don't),
     compare your computed value to the paper's stated value.
       - If within 10%: confidence "high"
       - If 10-30% off: confidence "medium", note the discrepancy
       - If >30% off: confidence "low", recheck your formula inputs

  5. If you cannot find primary-source architecture parameters for a model
     that otherwise qualifies, INCLUDE IT with kv values set to null and
     a note explaining what's missing. Do NOT guess from model size.

═══════════════════════════════════════════════════════════════════════════
PART 6 — OUTPUT FORMAT (STRICT)
═══════════════════════════════════════════════════════════════════════════

Begin your response with {{ and end with the matching }}. No prose before
or after. No markdown fences. JSON only.

{{
  "as_of_date": "{cutoff}",
  "method": "vendor_disclosures",
  "candidates": [
    {{
      "name": "<canonical model name as the lab uses it>",
      "lab": "<lab name from Part 1 list>",
      "release_date": "<YYYY-MM-DD>",
      "architecture_family": "GQA" | "GQA-MoE" | "MLA" | "MLA-MoE" | "MHA",
      "num_layers": <int>,
      "arch_params": {{
        "num_kv_heads": <int or null>,
        "head_dim": <int or null>,
        "mla_kv_lora_rank": <int or null>,
        "mla_qk_rope_head_dim": <int or null>
      }},
      "bytes_per_element": 2 | 1 | 0.5,
      "precision_declared": "BF16" | "FP16" | "FP8" | "INT4",
      "is_moe": true | false,
      "kv_kb_per_token_computed": <float>,
      "kv_kb_per_token_paper_stated": <float or null>,
      "kv_gb_per_1m_tokens": <float>,
      "computed_vs_paper_pct_diff": <float or null>,
      "source_url": "<primary source URL>",
      "source_type": "arxiv" | "huggingface" | "lab_blog" | "github",
      "benchmark_presence": ["<benchmark names from Part 1B where this model appears>"],
      "confidence": "high" | "medium" | "low",
      "notes": "<one-sentence note on any caveat, discrepancy, or excluded variant>"
    }}
  ],
  "candidates_considered_but_excluded": [
    {{
      "name": "<model name>",
      "reason": "<which Part 1 criterion failed>"
    }}
  ],
  "confidence": "high" | "medium" | "low"
}}

═══════════════════════════════════════════════════════════════════════════
FINAL REMINDER (this overrides any urge you have to explain first)
═══════════════════════════════════════════════════════════════════════════

Output the JSON object directly. Do your web research silently — do NOT
narrate your search strategy, do NOT write "I'll start by searching for...",
do NOT summarize what you found before giving the JSON. Your output budget
is limited; spending tokens on prose risks truncating the JSON.

Your VERY FIRST output character must be the opening brace. Begin now:"""


# Path B (economic triangulation) was removed in v3.8.3 — the X-axis question
# (KV bytes per token from architecture) is precisely answered by primary sources
# in Path A. Path B's serving-economics approach produced implausible values
# (<0.1 GB/1M) in production because providers do not consistently disclose the
# cost-curve-vs-context-length data needed for triangulation. Path A with the
# rigorous prompt is the authoritative path; no synthesis cross-check needed.


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _is_verified(candidate: dict) -> bool:
    """Return True if the candidate has a non-placeholder source URL."""
    url = candidate.get("source_url", "")
    return bool(url) and not _is_placeholder_url(url)


def _synthesize_candidates(path_a_result: dict) -> dict:
    """Single-path synthesis from Path A vendor disclosures.

    v3.8.3 — Path B (economic triangulation) was dropped. The X-axis question
    (KV bytes per token from architecture) is precisely answered by primary
    sources; Path B's serving-economics approach returned implausible values
    in production and added no useful signal.

    Frontier = candidate with minimum kv_gb_per_1m_tokens among ALL candidates
    within plausibility bounds. The new rigorous Path A prompt (Parts 1-6)
    handles candidate filtering, variant collapsing, MoE handling, precision
    declaration, and primary-source citation — so the synthesis layer is
    intentionally minimal.

    Plausibility bounds [MIN_PLAUSIBLE_KV_GB, MAX_PLAUSIBLE_KV_GB] catch
    formula misapplications. Floor: committed_kv = max(frontier.kv, KV_FLOOR_GB_PER_1M).
    """
    def _norm(name: str) -> str:
        return name.lower().strip().replace("-", " ").replace("_", " ")

    per_candidate = {}
    for c in (path_a_result.get("candidates") or []):
        name = c.get("name", "")
        norm_name = _norm(name)
        if not norm_name:
            continue

        kv = c.get("kv_gb_per_1m_tokens")
        if kv is None:
            logger.warning(f"  [{norm_name}] no kv value — included for inventory but excluded from selection")
            continue
        try:
            kv = float(kv)
        except (TypeError, ValueError):
            logger.warning(f"  [{norm_name}] non-numeric kv={kv!r} — excluded")
            continue

        # Plausibility filter
        if not (MIN_PLAUSIBLE_KV_GB <= kv <= MAX_PLAUSIBLE_KV_GB):
            logger.warning(f"  [{norm_name}] kv_gb={kv:.2f} outside plausibility bounds — excluded")
            continue

        url = c.get("source_url", "") or ""
        per_candidate[norm_name] = {
            "name": name,
            "release_date": c.get("release_date", ""),
            "arch_family": c.get("architecture_family", "unknown"),
            "lab": c.get("lab", ""),
            "precision_declared": c.get("precision_declared", ""),
            "is_moe": c.get("is_moe", False),
            "kv_gb_per_1m": round(kv, 4),
            "kv_kb_per_token_computed": c.get("kv_kb_per_token_computed"),
            "kv_kb_per_token_paper_stated": c.get("kv_kb_per_token_paper_stated"),
            "computed_vs_paper_pct_diff": c.get("computed_vs_paper_pct_diff"),
            "synthesis_rule": "path_A_only",
            "source_verified": bool(url) and not _is_placeholder_url(url),
            "source_url": url,
            "source_type": c.get("source_type", ""),
            "benchmark_presence": c.get("benchmark_presence", []),
            "candidate_confidence": c.get("confidence", "unknown"),
            "notes": c.get("notes", ""),
        }

    if not per_candidate:
        return {
            "status": "no_candidates",
            "error": "Path A returned no plausible candidates",
            "committed_kv_gb_per_1m": None,
        }

    # Select frontier: minimum KV across ALL candidates. URL verification is
    # provenance only, not a selection gate (see v3.8.2 fix).
    frontier_name = min(per_candidate, key=lambda n: per_candidate[n]["kv_gb_per_1m"])
    frontier = per_candidate[frontier_name]

    # Apply floor
    raw_kv = frontier["kv_gb_per_1m"]
    floor_applied = raw_kv < KV_FLOOR_GB_PER_1M
    committed_kv = max(raw_kv, KV_FLOOR_GB_PER_1M)

    # kv_gb_per_1m = kv_kb_per_token × 1e6 / 1024 / 1024
    # → kv_kb_per_token = committed_kv × 1024 × 1024 / 1e6
    committed_kb = committed_kv * 1024.0 * 1024.0 / 1_000_000.0

    return {
        "status": "committed",
        "frontier_model_name": frontier["name"],
        "frontier_model_release_date": frontier["release_date"],
        "arch_family": frontier["arch_family"],
        "lab": frontier["lab"],
        "precision_declared": frontier["precision_declared"],
        "is_moe": frontier["is_moe"],
        "source_url": frontier["source_url"],
        "source_verified": frontier["source_verified"],
        "raw_kv_gb_per_1m": round(raw_kv, 4),
        "floor_applied": floor_applied,
        "floor_value_gb_per_1m": KV_FLOOR_GB_PER_1M,
        "committed_kv_gb_per_1m": round(committed_kv, 4),
        "committed_kv_kb_per_token": round(committed_kb, 4),
        "synthesis_rule": "path_A_only",
        "all_candidates": per_candidate,
        "excluded_by_prompt": path_a_result.get("candidates_considered_but_excluded", []),
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
      4. Synthesis in Python — plausibility filter, min-KV selection, floor
      5. Apply floor, write state/inference_arch_state.json["llm_worker_frontier"]

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

        # v3.8.3 — Path B (economic triangulation) removed; Path A with the
        # rigorous prompt is the authoritative path.

        if not path_a_result:
            summary["errors"].append("Path A failed — no state changes made")
            return summary

        # ── 4. Synthesis ──────────────────────────────────────────────────────
        synthesis = _synthesize_candidates(path_a_result)
        summary["synthesis"] = synthesis
        n_cands = len(synthesis.get("all_candidates") or {})
        summary["candidates_found"] = n_cands
        logger.info(f"  Synthesis: {n_cands} candidate(s) evaluated")

        if synthesis["status"] != "committed":
            err = f"synthesis failed: {synthesis.get('error', 'unknown')}"
            summary["errors"].append(err)
            logger.error(f"  {err}")
            return summary

        # ── 5. Write state ────────────────────────────────────────────────────
        today = date.today().isoformat()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        prev_frontier = worker_state.get("frontier_model_name", "(none)")
        prev_kv = worker_state.get("committed_kv_gb_per_1m")

        new_worker_state = {
            "last_run_at": now_iso,
            "frontier_model_name": synthesis["frontier_model_name"],
            "frontier_model_release_date": synthesis["frontier_model_release_date"],
            "arch_family": synthesis["arch_family"],
            "lab": synthesis.get("lab", ""),
            "precision_declared": synthesis.get("precision_declared", ""),
            "is_moe": synthesis.get("is_moe", False),
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
                "all_candidates": synthesis.get("all_candidates"),
                "excluded_by_prompt": synthesis.get("excluded_by_prompt", []),
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
