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
  For each candidate:
    raw_kv       = candidate['kv_gb_per_1m'] (anchor-verbatim or paper-derived)
    degradation  = MATURITY_DEGRADATION_FACTORS[candidate['maturity']]
    effective_kv = raw_kv × degradation
  Frontier = candidate with min effective_kv across all candidates within
             plausibility bounds [MIN_PLAUSIBLE_KV_GB, MAX_PLAUSIBLE_KV_GB].
  URL verification is recorded for provenance but does NOT gate selection.
  committed_kv = effective_kv of frontier. No floor (v3.8.6 removed it —
  see top-of-module comment).

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

# v3.8.6 — KV_FLOOR_GB_PER_1M removed entirely. Conservatism now comes from
# per-candidate maturity-degradation (see MATURITY_DEGRADATION_FACTORS below)
# applied during synthesis. The degradation factor structurally attaches
# uncertainty to each candidate based on evidence quality, replacing the
# blunt post-hoc floor. Effective_kv = raw_kv × degradation_factor.

# Maturity classification per candidate. Each level corresponds to evidence
# accumulation: paper exists → weights released → independent benchmarks →
# multi-provider production deployment.
#
# The framework picks min(effective_kv) across candidates. A paper-claim
# entry with raw=3.3 becomes effective=6.6 (× 2.0); a proven entry with
# raw=33 stays at 33. The proven 33 still beats the paper-claim 6.6 only
# if degradation pushes the paper above the proven — i.e., if the bragger
# isn't actually that much better than the established frontier.
#
# As paper-claim entries mature (deployment evidence accumulates over
# quarterly runs), they get promoted in the anchor catalog and their
# degradation factor decreases. The framework follows the proven frontier,
# not the bragged frontier.
MATURITY_DEGRADATION_FACTORS = {
    "proven":      1.0,   # >12mo old + multi-provider production deployment
    "emerging":    1.5,   # 6-12mo + independent benchmark data
    "paper-claim": 2.0,   # paper + weights, <6mo or limited validation
    "preview":     2.5,   # preview/early-access, paper may be incomplete
}
DEFAULT_NEW_CANDIDATE_MATURITY = "paper-claim"

# Plausibility bounds: reject individual RAW values outside this range.
# Applied BEFORE maturity degradation. <1.0 GB/1M is implausible for any
# current frontier model. >500 GB/1M would require thousands of layers at
# FP32 — clearly garbage.
# With degradation removed-floor system, a paper-claim at raw=0.5 would
# fail this plausibility filter anyway, eliminating the need for a post-hoc
# floor as a defensive backstop.
MIN_PLAUSIBLE_KV_GB = 1.0
MAX_PLAUSIBLE_KV_GB = 500.0

# ── Anchor catalog (v3.8.6) ───────────────────────────────────────────────────
# Hand-verified reference set as of the date below. The Path A prompt embeds
# this catalog so the worker has a known-good baseline rather than re-deriving
# every value from scratch (which v3.8.5 production runs got wrong by 7x for
# well-known models like Llama 4 Maverick).
#
# Each entry has a maturity classification that determines its degradation
# factor at synthesis time. The worker uses anchor values verbatim for
# anchored models and only computes from scratch for genuinely new candidates.
#
# UPDATE PROCESS: when a quarterly run proposes a maturity promotion (e.g.,
# "V4 has now been deployed by 3 providers, suggest promoting paper-claim →
# emerging"), review and update this catalog. Anchor entries are version-
# controlled; the worker cannot mutate them.
ANCHOR_CATALOG_AS_OF = "2026-05-25"
ANCHOR_CATALOG = [
    # GQA family
    {"name": "Llama 3 70B",         "family": "GQA", "precision": "BF16",
     "kv_gb_per_1m": 305.0, "maturity": "proven",
     "source": "https://ai.meta.com/blog/meta-llama-3/",
     "formula_check": "2×80×8×128×2/1024 = 320 KB/tok → 305 GB/1M"},
    {"name": "Llama 3.1 70B",       "family": "GQA", "precision": "FP8",
     "kv_gb_per_1m": 153.0, "maturity": "proven",
     "source": "https://ai.meta.com/blog/meta-llama-3-1/",
     "formula_check": "2×80×8×128×1/1024 = 160 KB/tok → 153 GB/1M"},
    {"name": "Llama 4 Maverick",    "family": "GQA-MoE", "precision": "FP8",
     "kv_gb_per_1m": 153.0, "maturity": "proven",
     "source": "https://ai.meta.com/blog/llama-4/",
     "formula_check": "Same KV arch as Llama 3 70B at FP8"},
    {"name": "Mistral Large 2",     "family": "GQA", "precision": "FP8",
     "kv_gb_per_1m": 168.0, "maturity": "proven",
     "source": "https://mistral.ai/news/mistral-large-2407/",
     "formula_check": "2×88×8×128×1/1024 = 176 KB/tok → 168 GB/1M"},
    {"name": "Qwen 2.5 72B",        "family": "GQA", "precision": "FP8",
     "kv_gb_per_1m": 153.0, "maturity": "proven",
     "source": "https://qwen.readthedocs.io/",
     "formula_check": "2×80×8×128×1/1024 = 160 KB/tok → 153 GB/1M"},
    # MLA family
    {"name": "DeepSeek V2",         "family": "MLA", "precision": "BF16",
     "kv_gb_per_1m": 68.0, "maturity": "proven",
     "source": "https://arxiv.org/abs/2405.04434",
     "formula_check": "60×(512+64)×2/1024 = 67.5 KB/tok → 64 GB/1M"},
    {"name": "DeepSeek V3",         "family": "MLA-MoE", "precision": "FP8",
     "kv_gb_per_1m": 33.0, "maturity": "proven",
     "source": "https://arxiv.org/abs/2412.19437",
     "formula_check": "61×(512+64)×1/1024 = 34.3 KB/tok → 33 GB/1M"},
    {"name": "DeepSeek V3.5",       "family": "MLA-MoE", "precision": "FP8",
     "kv_gb_per_1m": 33.0, "maturity": "emerging",
     "source": "https://api-docs.deepseek.com/news/news250922",
     "formula_check": "Same MLA arch as V3"},
    {"name": "Qwen3 235B MLA",      "family": "MLA-MoE", "precision": "FP8",
     "kv_gb_per_1m": 53.0, "maturity": "emerging",
     "source": "https://qwenlm.github.io/blog/qwen3/",
     "formula_check": "94×(512+64)×1/1024 = 53 KB/tok → 50 GB/1M"},
    # Novel architectures (paper-stated; degradation handles uncertainty)
    {"name": "DeepSeek V4-Pro",     "family": "novel:CSA-HCA", "precision": "FP8",
     "kv_gb_per_1m": 3.3, "maturity": "paper-claim",
     "source": "https://fe-static.deepseek.com/chat/transparency/deepseek-V4-model-card-EN.pdf",
     "formula_check": "Paper claim: V4-Pro KV cache is ~10% of V3.2 at 1M context → 33 × 0.10 = 3.3 GB/1M. Architecture: CSA/HCA hybrid sparse attention (April 2026 release)."},
]


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
      - Centralized JSON-parse retry with doubled max_tokens, capped at 32K.
      - Raises on second failure; caller's per-path try/except handles it.

    v3.8.5 — retry tokens capped at 32768. Doubling above this point exceeds
    Sonnet's practical streaming output limit and the retry would itself fail
    with a payload size error rather than the JSON parsing it was meant to fix.
    """
    try:
        return _invoke_and_extract(prompt, max_tokens)
    except (json.JSONDecodeError, ValueError) as first_err:
        retry_tokens = min(max_tokens * 2, 32768)
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
    """Build the Path A research prompt with embedded anchor catalog (v3.8.6).

    Design philosophy: rather than asking Sonnet to re-derive every KV value
    from first principles (which v3.8.5 production runs got wrong by 7x for
    well-known models like Llama 4 Maverick), the prompt embeds a hand-verified
    anchor catalog as the baseline. Sonnet's job is narrow:

      1. Verify the anchor catalog is still accurate (no architecture corrections)
      2. Find any new frontier model released since the catalog's as-of date
      3. Compute KV for new models, with the anchor catalog as sanity reference
      4. Classify each new candidate's maturity (paper-claim or preview)
      5. Return all candidates — anchors verbatim + any new ones

    Maturity classification drives synthesis degradation downstream.
    Anchors are used verbatim (worker cannot mutate anchor values).
    """
    cutoff = date.today().isoformat()
    from datetime import timedelta
    lookback_date = (date.today() - timedelta(days=CANDIDATE_LOOKBACK_MONTHS * 30)).isoformat()

    # Format the anchor catalog as a readable block
    anchor_block_lines = [f"  As-of date: {ANCHOR_CATALOG_AS_OF}", ""]
    for a in ANCHOR_CATALOG:
        anchor_block_lines.append(
            f"  • {a['name']:<24s} | {a['family']:<10s} | {a['precision']:<5s} | "
            f"{a['kv_gb_per_1m']:>6.1f} GB/1M | {a['maturity']:<11s}"
        )
        anchor_block_lines.append(f"      {a['formula_check']}")
        anchor_block_lines.append(f"      source: {a['source']}")
    anchor_block = "\n".join(anchor_block_lines)

    return f"""You are researching for a quantitative HBM investment analysis framework.
Today is {cutoff}. Your output drives a financial model — incorrect values
flow directly into investment decisions.

═══════════════════════════════════════════════════════════════════════════
ANCHOR CATALOG — hand-verified reference values (use these directly)
═══════════════════════════════════════════════════════════════════════════

The following frontier open-source models have been verified by the framework
maintainers. For ANY model on this list, return the kv_gb_per_1m value
VERBATIM. Do NOT recompute. Do NOT search for "more accurate" values.
These are the authoritative numbers for these architectures.

{anchor_block}

═══════════════════════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════════════════════

You have TWO jobs, in order:

JOB 1 — Return all anchor catalog entries above as candidates, using the
  exact kv_gb_per_1m value shown and the maturity classification shown.
  Source URLs from the catalog must be passed through unchanged.

JOB 2 — Find any frontier open-source model released between
  {ANCHOR_CATALOG_AS_OF} and {cutoff} that is NOT already in the anchor
  catalog.

  If this date window is zero days (which happens when the anchor catalog
  was updated today), there is nothing to research. JOB 2 returns an
  empty addition — that is correct, not an error.

  For each new candidate found:

  (a) Verify it's from an approved lab: DeepSeek, Meta (Llama),
      Alibaba (Qwen), Mistral AI, Z.ai (GLM), xAI (Grok open-weights only),
      Google (Gemma open-weights only).

  (b) Find the primary source (arxiv paper, HuggingFace model card with
      config.json visible, or official lab blog).

  (c) Compute kv_gb_per_1m. CHOOSE ONE OF THESE PATHS:

      PATH I — paper-stated value (PREFERRED when available):
        If the paper states KV cache per token directly, use that value.
        If the paper states KV as a ratio vs a known model (e.g., "10% of
        V3.2's KV at 1M context"), compute using anchor catalog values
        above. Example: "10% of V3.2" → 33 × 0.10 = 3.3 GB/1M.

      PATH II — compute from architecture parameters (only if Path I N/A):
        For GQA: kv_kb_per_token = 2 × num_layers × num_kv_heads × head_dim
                                   × bytes_per_element / 1024
        For MLA: kv_kb_per_token = num_layers × (mla_kv_lora_rank
                                   + mla_qk_rope_head_dim) × bytes_per_element
                                   / 1024
        Then: kv_gb_per_1m = kv_kb_per_token × 1e6 / 1024 / 1024
        bytes_per_element: BF16/FP16 = 2, FP8 = 1, INT4 = 0.5

        SANITY CHECK against the anchor catalog: if your computed value
        differs by more than 2× from the nearest comparable anchor (same
        family, similar layer count, same precision), you have likely
        made an error. Either recheck your inputs or use Path I if the
        paper provides any KV claim at all.

  (d) Classify maturity:
        "preview"     — early-access, paper may be incomplete, <30 days old
        "paper-claim" — paper + weights released, <6 months old, limited
                        independent validation (DEFAULT for new candidates)

      You do NOT promote to "emerging" or "proven" — those transitions
      require multi-quarter evidence and are handled by maintainers.

═══════════════════════════════════════════════════════════════════════════
WHAT TO EXCLUDE (applies to JOB 2 new candidates only — anchor entries
in JOB 1 are returned regardless of age)
═══════════════════════════════════════════════════════════════════════════

  - Closed-source models (GPT, Claude, Gemini API-only)
  - Fine-tunes of other labs' base models (Nous-Hermes, WizardLM, etc.)
  - Quantized redistributions (Unsloth GGUFs, AWQ rehosts)
  - Models older than {lookback_date}
  - Vision/multimodal-only models
  - Research-only models with no production weights released

═══════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT (STRICT JSON)
═══════════════════════════════════════════════════════════════════════════

{{
  "as_of_date": "{cutoff}",
  "anchor_catalog_used": "{ANCHOR_CATALOG_AS_OF}",
  "candidates": [
    {{
      "name": "<model name>",
      "from_anchor_catalog": true | false,
      "family": "GQA" | "GQA-MoE" | "MLA" | "MLA-MoE" | "novel:<short_name>",
      "precision": "BF16" | "FP16" | "FP8" | "INT4",
      "release_date": "<YYYY-MM-DD>",
      "kv_gb_per_1m": <float>,
      "computation_path": "anchor" | "paper_stated" | "paper_ratio" | "formula",
      "computation_note": "<one-line explanation if computed; for anchors, copy formula_check>",
      "maturity": "proven" | "emerging" | "paper-claim" | "preview",
      "source_url": "<primary source URL>",
      "confidence": "high" | "medium" | "low"
    }}
  ],
  "anchor_promotion_suggestions": [
    {{
      "name": "<anchor model name>",
      "current_maturity": "<current value>",
      "suggested_maturity": "<proposed value>",
      "rationale": "<why — e.g., now deployed by N major providers>"
    }}
  ],
  "confidence": "high" | "medium" | "low"
}}

The "anchor_promotion_suggestions" array is OPTIONAL — only populate it
if during research you found clear evidence that an anchor entry should
graduate to a higher maturity level. Empty array is fine.

═══════════════════════════════════════════════════════════════════════════
FINAL REMINDER
═══════════════════════════════════════════════════════════════════════════

Output the JSON object directly. Do not narrate your research or
explain your approach. Your VERY FIRST output character must be the
opening brace. Begin now:"""


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


def _norm_model_name(name: str) -> str:
    """Normalize model name for anchor lookup: case/whitespace/dash-insensitive."""
    return name.lower().strip().replace("-", " ").replace("_", " ").replace(".", " ")


def _find_anchor_by_name(name: str) -> dict | None:
    """Return the ANCHOR_CATALOG entry matching the given name (loose match),
    or None if not found. Tolerates dash/underscore/case/dot variations."""
    target = _norm_model_name(name)
    for a in ANCHOR_CATALOG:
        if _norm_model_name(a["name"]) == target:
            return a
    return None


def _synthesize_candidates(path_a_result: dict) -> dict:
    """Single-path synthesis with maturity-degradation (v3.8.6).

    For each candidate:
      raw_kv         = candidate['kv_gb_per_1m'] (anchor-verbatim or paper-derived)
      degradation    = MATURITY_DEGRADATION_FACTORS[candidate['maturity']]
      effective_kv   = raw_kv × degradation

    Frontier = candidate with minimum effective_kv across all candidates within
    plausibility bounds. committed_kv = effective_kv of frontier. No floor.

    The degradation factor structurally attaches uncertainty to each candidate
    based on evidence quality. A paper-claim entry at raw=3.3 becomes
    effective=6.6 (×2.0); a proven entry at raw=33 stays at 33. As paper-claim
    entries mature into emerging/proven (anchor catalog updates), their
    degradation factor drops and the framework follows the evidence frontier.
    """
    def _norm(name: str) -> str:
        return name.lower().strip().replace("-", " ").replace("_", " ")

    per_candidate = {}
    for c in (path_a_result.get("candidates") or []):
        name = c.get("name", "")
        norm_name = _norm(name)
        if not norm_name:
            continue

        # v3.8.6 — read kv_gb_per_1m directly (new schema); fall back to
        # kv_gb_per_1m_tokens for backward compat with older path_a outputs
        kv_raw = c.get("kv_gb_per_1m")
        if kv_raw is None:
            kv_raw = c.get("kv_gb_per_1m_tokens")
        if kv_raw is None:
            logger.warning(f"  [{norm_name}] no kv value — excluded from selection")
            continue
        try:
            kv_raw = float(kv_raw)
        except (TypeError, ValueError):
            logger.warning(f"  [{norm_name}] non-numeric kv={kv_raw!r} — excluded")
            continue

        # v3.8.6 — Anchor validation: ALWAYS check if the candidate name matches
        # an anchor entry (regardless of from_anchor_catalog flag — Sonnet may
        # forget to set the flag but the name match is the real signal).
        # If matched and kv differs from catalog, override with catalog value.
        # Defense against Sonnet "correcting" anchor values despite VERBATIM
        # instruction, AND against Sonnet computing from scratch for a known
        # anchor model and getting it wrong (like Maverick at 22.89 in v3.8.5).
        anchor_override_applied = False
        anchor = _find_anchor_by_name(name)
        if anchor is not None:
            anchor_kv = float(anchor["kv_gb_per_1m"])
            if abs(kv_raw - anchor_kv) > 0.05:  # 0.05 GB/1M tolerance
                logger.warning(
                    f"  [{norm_name}] matches anchor catalog entry but Sonnet returned "
                    f"kv={kv_raw} while catalog has {anchor_kv}. Overriding with "
                    f"catalog value (defense against Sonnet recomputation errors)."
                )
                kv_raw = anchor_kv
                anchor_override_applied = True
            # Also override maturity from catalog
            if c.get("maturity") != anchor["maturity"]:
                logger.warning(
                    f"  [{norm_name}] matches anchor catalog entry but Sonnet returned "
                    f"maturity='{c.get('maturity')}' while catalog has "
                    f"'{anchor['maturity']}'. Overriding with catalog value."
                )
                c["maturity"] = anchor["maturity"]
                anchor_override_applied = True
            # Force from_anchor_catalog flag to True for downstream provenance
            c["from_anchor_catalog"] = True

        # Plausibility filter on RAW value (before degradation)
        if not (MIN_PLAUSIBLE_KV_GB <= kv_raw <= MAX_PLAUSIBLE_KV_GB):
            logger.warning(f"  [{norm_name}] raw kv_gb={kv_raw:.2f} outside plausibility bounds — excluded")
            continue

        # v3.8.6 — apply maturity degradation
        maturity = c.get("maturity", DEFAULT_NEW_CANDIDATE_MATURITY)
        degradation = MATURITY_DEGRADATION_FACTORS.get(maturity)
        if degradation is None:
            logger.warning(
                f"  [{norm_name}] unknown maturity '{maturity}' — defaulting to "
                f"'{DEFAULT_NEW_CANDIDATE_MATURITY}' (degradation "
                f"{MATURITY_DEGRADATION_FACTORS[DEFAULT_NEW_CANDIDATE_MATURITY]})"
            )
            maturity = DEFAULT_NEW_CANDIDATE_MATURITY
            degradation = MATURITY_DEGRADATION_FACTORS[maturity]

        kv_effective = kv_raw * degradation

        url = c.get("source_url", "") or ""
        per_candidate[norm_name] = {
            "name": name,
            "release_date": c.get("release_date", ""),
            "arch_family": c.get("family") or c.get("architecture_family", "unknown"),
            "precision_declared": c.get("precision") or c.get("precision_declared", ""),
            "kv_gb_per_1m_raw": round(kv_raw, 4),
            "kv_gb_per_1m_effective": round(kv_effective, 4),
            "maturity": maturity,
            "degradation_factor": degradation,
            "computation_path": c.get("computation_path", "unknown"),
            "computation_note": c.get("computation_note", ""),
            "from_anchor_catalog": bool(c.get("from_anchor_catalog", False)),
            "source_verified": bool(url) and not _is_placeholder_url(url),
            "source_url": url,
            "candidate_confidence": c.get("confidence", "unknown"),
        }

    # v3.8.6 — Safety net: inject any anchor catalog entries that Sonnet
    # failed to return. The anchor catalog is the source of truth; we should
    # never lose entries because Sonnet skipped them. This guarantees the
    # framework always considers the full known frontier set regardless of
    # Sonnet's behavior.
    returned_names_norm = set(per_candidate.keys())
    injected_anchors = []
    for a in ANCHOR_CATALOG:
        anchor_name_norm = _norm(a["name"])
        if anchor_name_norm in returned_names_norm:
            continue
        # Anchor was missing from Sonnet's response — inject it
        injected_anchors.append(a["name"])
        kv_raw = float(a["kv_gb_per_1m"])
        maturity = a["maturity"]
        degradation = MATURITY_DEGRADATION_FACTORS[maturity]
        per_candidate[anchor_name_norm] = {
            "name": a["name"],
            "release_date": "",
            "arch_family": a["family"],
            "precision_declared": a["precision"],
            "kv_gb_per_1m_raw": round(kv_raw, 4),
            "kv_gb_per_1m_effective": round(kv_raw * degradation, 4),
            "maturity": maturity,
            "degradation_factor": degradation,
            "computation_path": "anchor_injected",
            "computation_note": f"Injected by synthesis safety net (Sonnet did not return this anchor). {a['formula_check']}",
            "from_anchor_catalog": True,
            "source_verified": True,
            "source_url": a["source"],
            "candidate_confidence": "high",
        }
    if injected_anchors:
        logger.warning(
            f"  Synthesis injected {len(injected_anchors)} anchor(s) missing from "
            f"Sonnet's response: {', '.join(injected_anchors)}"
        )

    if not per_candidate:
        return {
            "status": "no_candidates",
            "error": "Path A returned no plausible candidates",
            "committed_kv_gb_per_1m": None,
        }

    # Select frontier: minimum EFFECTIVE KV across all candidates
    frontier_name = min(per_candidate, key=lambda n: per_candidate[n]["kv_gb_per_1m_effective"])
    frontier = per_candidate[frontier_name]

    committed_kv = frontier["kv_gb_per_1m_effective"]
    committed_kb = committed_kv * 1024.0 * 1024.0 / 1_000_000.0

    return {
        "status": "committed",
        "frontier_model_name": frontier["name"],
        "frontier_model_release_date": frontier["release_date"],
        "arch_family": frontier["arch_family"],
        "precision_declared": frontier["precision_declared"],
        "from_anchor_catalog": frontier["from_anchor_catalog"],
        "source_url": frontier["source_url"],
        "source_verified": frontier["source_verified"],
        "raw_kv_gb_per_1m": frontier["kv_gb_per_1m_raw"],
        "maturity": frontier["maturity"],
        "degradation_factor": frontier["degradation_factor"],
        "committed_kv_gb_per_1m": round(committed_kv, 4),
        "committed_kv_kb_per_token": round(committed_kb, 4),
        "synthesis_rule": "anchor_or_paper × maturity_degradation",
        "all_candidates": per_candidate,
        "anchor_promotion_suggestions": path_a_result.get("anchor_promotion_suggestions", []),
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
            path_a_result = _call_claude_with_search(_build_path_a_vendor_prompt(), max_tokens=32768)
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
            "precision_declared": synthesis.get("precision_declared", ""),
            "from_anchor_catalog": synthesis.get("from_anchor_catalog", False),
            "source_url": synthesis["source_url"],
            "source_verified": synthesis["source_verified"],
            "raw_kv_gb_per_1m": synthesis.get("raw_kv_gb_per_1m"),
            "maturity": synthesis.get("maturity", ""),
            "degradation_factor": synthesis.get("degradation_factor"),
            "committed_kv_gb_per_1m": synthesis["committed_kv_gb_per_1m"],
            "committed_kv_kb_per_token": synthesis["committed_kv_kb_per_token"],
            "synthesis_rule": synthesis.get("synthesis_rule", ""),
            "anchor_catalog_as_of": ANCHOR_CATALOG_AS_OF,
            "anchor_promotion_suggestions": synthesis.get("anchor_promotion_suggestions", []),
            "history": worker_state.get("history", []) + [{
                "date": today,
                "frontier_model": synthesis["frontier_model_name"],
                "raw_kv_gb_per_1m": synthesis.get("raw_kv_gb_per_1m"),
                "maturity": synthesis.get("maturity", ""),
                "degradation_factor": synthesis.get("degradation_factor"),
                "committed_kv_gb_per_1m": synthesis["committed_kv_gb_per_1m"],
                "committed_kv_kb_per_token": synthesis["committed_kv_kb_per_token"],
                "path_a_candidates": len(path_a_result.get("candidates") or []),
                "all_candidates": synthesis.get("all_candidates"),
                "anchor_promotion_suggestions": synthesis.get("anchor_promotion_suggestions", []),
                "calibration": cal_diagnostic,
            }],
        }

        state["llm_worker_frontier"] = new_worker_state
        state["_metadata"]["last_arch_research_run_at"] = now_iso
        state["_metadata"]["last_arch_research_additions"] = 1
        _save_state(state)

        raw = synthesis.get("raw_kv_gb_per_1m")
        maturity = synthesis.get("maturity", "")
        deg = synthesis.get("degradation_factor")
        committed_str = (
            f"{synthesis['frontier_model_name']} "
            f"raw={_fmt(raw, ' GB/1M')} × {deg} ({maturity}) = "
            f"{_fmt(synthesis['committed_kv_gb_per_1m'], ' GB/1M')}"
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
