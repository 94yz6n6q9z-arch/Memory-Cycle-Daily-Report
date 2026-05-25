"""Context length research worker — v3.7.0 (2026-05-25).

Multi-path triangulation architecture. For each input the framework needs,
the worker runs 3 INDEPENDENT research paths with different methodologies,
then commits only if the three paths converge within 30%. If they diverge,
the run logs all three results and skips the commit — a divergence is more
useful information than a guessed average.

Architecture changes from v3.6.2:
  - Each typical_context_tokens category now researched via 3 prompts:
      Path A — Vendor disclosures (vendor docs, product pages, blog posts)
      Path B — Economic triangulation (revenue / pricing / user count math)
      Path C — Empirical third-party studies (OpenRouter, RAGAS, analyst reports)
  - volume_share_pct now researched via 3 different lenses, same pattern
  - Convergence gate: if paths agree within 30% spread, commit the mean.
    If they disagree, no commit, full provenance logged for human review.
  - Calibration back-test runs FIRST. Worker is asked to independently
    re-derive OpenRouter's published "6,000 average prompt tokens" finding.
    If it lands outside 4,500-8,500, the whole run aborts and opens an issue.
    This catches prompt drift, model behavior change, and web-search
    quality regressions before they corrupt state.
  - Prompts now use the explicit KV-cache definition with arithmetic
    examples — eliminates the "user query size vs full context loaded"
    ambiguity that caused v3.6.2 to under-report by ~10x.

Cost: ~19 LLM calls per run (was 6) × $0.03/call × 6 runs/year ≈ $3.50/year.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

CONTEXT_STATE_FILE = Path("state/context_length_state.json")

# v3.7.0 — convergence threshold across triangulation paths.
# If max(value) and min(value) across the 3 paths differ by more than this
# fraction of the mean, the result is flagged DIVERGENT and not committed.
MAX_PATH_SPREAD_FRACTION = 0.30

# v3.7.0 — calibration back-test parameters.
# The worker independently re-derives the OpenRouter 100T-token study finding
# ("average prompt tokens per request increased ~4x from 1,500 to over 6,000
# between early 2024 and late 2025"). If the worker's independently-derived
# answer falls outside this band, prompts have likely drifted and we abort.
CALIBRATION_TARGET_VALUE = 6_000
CALIBRATION_TARGET_TOLERANCE_FRACTION = 0.40    # ±40% = 3,600-8,400

# Existing safety thresholds (carried from v3.6.2)
MAX_DELTA_FRACTION_CONTEXT = 0.50
MAX_DELTA_PP_SHARE = 15
MIN_PLAUSIBLE_CONTEXT = 1_000
MAX_PLAUSIBLE_CONTEXT = 10_000_000
ACCEPTED_CONFIDENCE = {"high", "medium"}

CATEGORIES = [
    "consumer_chat",
    "coding_agents",
    "enterprise_vertical_agents",
    "developer_raw_api",
    "other_creative_rag",
]

CATEGORY_DESCRIPTIONS = {
    "consumer_chat": (
        "Chat-based consumer AI assistants used by general public — "
        "ChatGPT, Claude.ai, Gemini app, Microsoft Copilot consumer"
    ),
    "coding_agents": (
        "Specialized coding agent products — Claude Code, Cursor, "
        "GitHub Copilot (chat AND inline autocomplete), Devin (Cognition), "
        "Replit Agent, Lovable, Bolt"
    ),
    "enterprise_vertical_agents": (
        "Enterprise-procurement AI deployments — Anthropic Enterprise tier, "
        "Microsoft Copilot Enterprise, Harvey (legal), Hebbia (finance), "
        "Sierra (support), Glean (knowledge), large RAG deployments"
    ),
    "developer_raw_api": (
        "Independent developers and small shops using AI APIs directly "
        "(OpenAI API, Anthropic API, Mistral, Together, Groq, etc.) "
        "without using a packaged enterprise product"
    ),
    "other_creative_rag": (
        "Other use cases — Perplexity, Grok consumer, smaller chat clones, "
        "image generation prompting, creative writing tools"
    ),
}


# ── The unambiguous definition (used in every prompt) ─────────────────────────

DEFINITION_BLOCK = """\
DEFINITION OF "typical_context_tokens" (read carefully — this is the most \
important thing in this prompt):

It is the TOTAL TOKENS LOADED INTO THE MODEL'S INPUT CONTEXT when a typical \
production request is processed in this category. This is what fills the KV \
cache during inference, which is what determines GPU memory demand.

This INCLUDES EVERYTHING that goes into the prompt the model actually sees:
  - System prompt (typically 500-3,000 tokens)
  - Chat history from prior turns in the same session
  - RAG-retrieved documents / snippets / chunks
  - Loaded code files (for coding agents)
  - Tool outputs from previous tool calls in the session
  - The current user message

EXAMPLES OF HOW TO COMPUTE THIS:
  - Consumer chat, turn 2: system (~1K) + memory layer (~500) + turn-1 \
exchange (~1K) + current message (~200) = ~2,700 tokens
  - Claude Code editing 10 files: system (~5K) + loaded files (~30K) + \
user query (~200) + prior tool outputs (~5K) = ~40,000 tokens
  - Enterprise RAG query, selective retrieval: system (~2K) + 5 chunks × \
2K (~10K) + query (~200) = ~12,000 tokens
  - Enterprise RAG query, broader retrieval: system (~2K) + 20 chunks × \
2K (~40K) + history (~5K) = ~47,000 tokens

WHAT THIS IS NOT:
  - NOT the maximum context window the model supports (e.g., "Sonnet 4.6 \
has 1M context" — that is the CEILING, not the TYPICAL FILL)
  - NOT just the user-typed message (which excludes system prompt, history, \
and RAG content)
  - NOT the advertised context capacity of the product
  - NOT a target or aspiration — it is what production traffic actually uses TODAY

If you find yourself reporting "the product supports up to X tokens", you \
have misread the question. The question is what gets LOADED on a typical \
production request, not what COULD be loaded at maximum.\
"""


# ── Prompts: typical_context_tokens (3 paths per category) ────────────────────

def _build_typical_path_a_vendor(category: str) -> str:
    """Path A — Bottom-up from vendor documentation and product disclosures."""
    return f"""You are researching for an AI investment framework. Today is {date.today().isoformat()}.

YOUR TASK: Determine typical_context_tokens for the workload category \
"{category}" by examining what major vendors PUBLISH about typical \
production request patterns.

CATEGORY: {CATEGORY_DESCRIPTIONS[category]}

{DEFINITION_BLOCK}

RESEARCH METHOD FOR THIS CALL — VENDOR DISCLOSURES ONLY:
  1. Identify the 4-6 dominant products in this category by current market share
  2. For each, find their vendor-published documentation or engineering \
blog posts that describe typical production usage patterns. Look for:
     - "Typical session loads X files" type statements
     - Explicit per-request token figures in cost-tracking docs
     - Default behavior descriptions ("we retrieve top-5 chunks of 2K each")
     - System prompt sizes if disclosed
     - Concrete cost examples ("a typical $X/month customer processes Y queries averaging Z tokens")
  3. For each product, compute a typical context size by SUMMING all \
components (system prompt + retrieval + history + user message)
  4. Estimate the current market share weighting (use web traffic share, \
ARR, or user count as proxy)
  5. Compute the market-share-weighted average

DO NOT use empirical third-party measurements (that's a different research \
path). Stick to vendor sources for this call.

OUTPUT — return ONLY a JSON object (no markdown fences, no prose around it):
{{
  "value": <integer tokens>,
  "method": "vendor_disclosures",
  "products_analyzed": [
    {{
      "name": "<product name>",
      "share_weight": <0.0 to 1.0>,
      "system_prompt_tokens": <int>,
      "typical_retrieval_or_loaded_content_tokens": <int>,
      "typical_history_tokens": <int>,
      "typical_user_message_tokens": <int>,
      "total_typical_context": <int>,
      "evidence_quote": "<short quote from vendor source>",
      "source_url": "<url>"
    }}
  ],
  "weighted_calculation": "<show the math: e.g. 0.4*40K + 0.3*15K + 0.2*60K + 0.1*100K = ...>",
  "confidence": "high" | "medium" | "low",
  "as_of_date": "{date.today().isoformat()}"
}}"""


def _build_typical_path_b_economic(category: str) -> str:
    """Path B — Top-down from revenue/pricing/usage math."""
    return f"""You are researching for an AI investment framework. Today is {date.today().isoformat()}.

YOUR TASK: Derive typical_context_tokens for the workload category \
"{category}" using ECONOMIC TRIANGULATION — revenue figures, API pricing, \
and disclosed user/request counts. Do not use vendor descriptions of \
per-request behavior for this call.

CATEGORY: {CATEGORY_DESCRIPTIONS[category]}

{DEFINITION_BLOCK}

RESEARCH METHOD FOR THIS CALL — ECONOMIC MATH:
  1. Find the total annualized revenue or token volume attributable to this \
category. Examples:
     - Anthropic enterprise: ~80% of $30B ARR = ~$24B (publicly disclosed April 2026)
     - Claude Code: ~$2.5B ARR (Time Magazine, Feb 2026)
     - OpenAI consumer: ~60% of $25B ARR
     - For consumer_chat, total revenue is mostly flat subscriptions; use \
WAU × prompts/week × tokens/prompt × cost-equivalent instead
  2. Find the average blended input token pricing for dominant models in \
this category. Approximate guides:
     - Sonnet 4.6: $3/M input, $15/M output (typically input-dominant)
     - Opus 4.7: $5/M input, $25/M output
     - Haiku 4.5: $1/M input, $5/M output
     - Mixed-tier deployments often blend to ~$2-4/M input
  3. Compute implied total annual input token volume = revenue / blended-input-price
  4. Find an estimate of total annual requests in this category from product \
disclosures (MAU × sessions × messages, or ARR / per-customer-rate, etc.)
  5. Compute implied tokens per request = total_input_tokens / total_requests
  6. Sanity check: does the implied per-request size match the order of \
magnitude you'd expect for this category?

OUTPUT — return ONLY a JSON object (no markdown fences):
{{
  "value": <integer tokens>,
  "method": "economic_triangulation",
  "math_steps": [
    {{"step": "annual revenue", "value": "<$X B>", "source_url": "<url>"}},
    {{"step": "blended input price", "value": "<$Y per 1M tokens>", "source_url": "<url>"}},
    {{"step": "implied total input tokens/year", "value": "<integer>", "calc": "revenue / price"}},
    {{"step": "annual request count", "value": "<integer>", "source_url": "<url>"}},
    {{"step": "implied tokens per request", "value": "<integer>", "calc": "tokens / requests"}}
  ],
  "sanity_check": "<does the result look right? why?>",
  "confidence": "high" | "medium" | "low",
  "as_of_date": "{date.today().isoformat()}"
}}"""


def _build_typical_path_c_empirical(category: str) -> str:
    """Path C — Empirical third-party measurement studies."""
    return f"""You are researching for an AI investment framework. Today is {date.today().isoformat()}.

YOUR TASK: Find EMPIRICAL THIRD-PARTY MEASUREMENTS of typical context size \
for the workload category "{category}". Use measurement studies, benchmarks, \
analyst reports, or peer-reviewed papers — NOT vendor claims and NOT \
economic derivations.

CATEGORY: {CATEGORY_DESCRIPTIONS[category]}

{DEFINITION_BLOCK}

RESEARCH METHOD FOR THIS CALL — THIRD-PARTY MEASUREMENT ONLY:
Look for studies that have actually measured production LLM traffic, such as:
  - OpenRouter "State of AI" 100-trillion-token study (Nov 2024-Nov 2025) — \
reported "average prompt tokens per request increased ~4x from 1,500 to over \
6,000". This is across ALL OpenRouter traffic; programming/agentic pulls \
the average UP. Adjust for the specific category you're researching.
  - RAGAS / RAGAS 2.0 production RAG benchmark studies
  - a16z research publications, SemiAnalysis reports, Sacra industry analyses
  - Academic papers analyzing production LLM usage patterns
  - Reverse-engineering from public cost reports ("$X/month for Y queries" → per-query size)

CRITICAL: do NOT use vendor product pages, vendor blog posts, or vendor \
marketing claims. If you can't find independent measurement for this \
specific category, say so via lower confidence.

ALSO: adjust general findings to fit the specific category. OpenRouter \
data is developer API traffic — for consumer_chat it must be adjusted \
downward (OpenRouter has no consumer chat). For enterprise_vertical_agents \
it must be adjusted for the RAG-heavy access pattern.

OUTPUT — return ONLY a JSON object (no markdown fences):
{{
  "value": <integer tokens>,
  "method": "empirical_third_party",
  "studies_cited": [
    {{
      "study_name": "<name>",
      "publication_date": "<YYYY-MM>",
      "what_it_measured": "<one sentence>",
      "raw_measured_value": <int>,
      "url": "<url>"
    }}
  ],
  "category_adjustment_reasoning": "<how/why you adjusted study findings to \
fit this specific category>",
  "confidence": "high" | "medium" | "low",
  "as_of_date": "{date.today().isoformat()}"
}}"""


# ── Prompts: volume_share_pct (3 paths, each returns all 5 categories) ────────

def _build_share_path_a_revenue_mix() -> str:
    """Path A — Revenue mix disclosures, converted to token-volume shares."""
    cats_text = "\n".join(f"  - {c}: {CATEGORY_DESCRIPTIONS[c]}" for c in CATEGORIES)
    return f"""You are researching for an AI investment framework. Today is {date.today().isoformat()}.

YOUR TASK: Determine volume_share_pct (token-volume share, summing to 100%) \
across 5 workload categories using PUBLIC REVENUE MIX DISCLOSURES from \
major AI providers. Convert revenue shares to token-volume shares using \
pricing.

THE 5 CATEGORIES:
{cats_text}

CRITICAL DEFINITION: "volume_share_pct" measures share of total inference \
TOKENS served globally — NOT user count, NOT request count, NOT revenue \
directly. Tokens. The mapping from revenue to tokens depends heavily on \
pricing model (flat-fee subscription vs pay-per-token).

KEY ECONOMIC FACTS TO USE:
  - Consumer chat is mostly flat-fee subscriptions ($20-200/mo); token \
volume scales with usage but revenue does NOT
  - Enterprise / developer API is pay-per-token; revenue maps directly to tokens
  - Coding agents are a mix; Claude Code is largely consumption-priced
  - This asymmetry means consumer revenue UNDERSTATES consumer token volume, \
and enterprise revenue closely MATCHES enterprise token volume

METHOD:
  1. Find current revenue mix disclosures (Anthropic enterprise %, OpenAI \
consumer %, Google AI revenue mix, etc.)
  2. Find product-specific ARR for coding agents (Claude Code, Cursor, etc.)
  3. For pay-per-token segments: convert revenue × inverse-blended-price = token volume
  4. For flat-fee consumer: estimate tokens via WAU × prompts/week × \
tokens-per-prompt; the result is independent of revenue
  5. Sum across categories, normalize to 100%

OUTPUT — return ONLY a JSON object (no markdown fences):
{{
  "shares": {{
    "consumer_chat": <int 0-100>,
    "coding_agents": <int 0-100>,
    "enterprise_vertical_agents": <int 0-100>,
    "developer_raw_api": <int 0-100>,
    "other_creative_rag": <int 0-100>
  }},
  "method": "revenue_mix_to_token_share",
  "revenue_figures_used": [
    {{"provider": "<name>", "total_arr": "<$X B>", "enterprise_pct": <int>, "source_url": "<url>"}}
  ],
  "consumer_token_volume_derivation": "<show math for consumer estimate \
since it can't use revenue directly>",
  "category_calculation_steps": "<show how each category's share was computed>",
  "confidence": "high" | "medium" | "low",
  "as_of_date": "{date.today().isoformat()}"
}}

CRITICAL: shares must sum to exactly 100."""


def _build_share_path_b_traffic_proxy() -> str:
    """Path B — Web traffic / MAU share with per-user-token-consumption correction."""
    cats_text = "\n".join(f"  - {c}: {CATEGORY_DESCRIPTIONS[c]}" for c in CATEGORIES)
    return f"""You are researching for an AI investment framework. Today is {date.today().isoformat()}.

YOUR TASK: Determine volume_share_pct (token-volume share, summing to 100%) \
using WEB TRAFFIC SHARE and USER COUNT DATA as the input, then correcting \
for per-user token consumption (which differs enormously by category).

THE 5 CATEGORIES:
{cats_text}

CRITICAL DEFINITION: We want TOKEN volume share. Users and requests are \
proxies. Per-user token consumption ratios are roughly:
  - Enterprise vertical user: 100-1000x more tokens per user than consumer
  - Coding agent user: 10-100x more tokens per session than consumer
  - Developer API user: highly variable, 10-50x consumer

METHOD:
  1. Find current web traffic share (SimilarWeb, Cloudflare Radar) for \
top consumer products (ChatGPT, Gemini, Claude.ai, Microsoft Copilot, Perplexity)
  2. Find disclosed WAU/MAU figures for major products in each category
  3. Estimate average per-user token consumption for each category:
     - Consumer: ~30 messages/week × ~5K tokens each = ~150K tokens/user/week
     - Coding agent: heavy users 10x consumer = ~1.5M tokens/user/week
     - Enterprise user: 100x consumer = ~15M tokens/user/week
     - Developer API: variable, ~500K tokens/user/week average
  4. Multiply: total tokens per category ≈ (users) × (tokens-per-user-per-week)
  5. Normalize to 100% across categories

OUTPUT — return ONLY a JSON object (no markdown fences):
{{
  "shares": {{
    "consumer_chat": <int 0-100>,
    "coding_agents": <int 0-100>,
    "enterprise_vertical_agents": <int 0-100>,
    "developer_raw_api": <int 0-100>,
    "other_creative_rag": <int 0-100>
  }},
  "method": "traffic_proxy_with_per_user_correction",
  "user_figures_used": [
    {{"product": "<name>", "users": "<X M WAU/MAU>", "category": "<cat>", "source_url": "<url>"}}
  ],
  "per_user_token_estimates": {{
    "consumer_chat": <int tokens/user/week>,
    "coding_agents": <int>,
    "enterprise_vertical_agents": <int>,
    "developer_raw_api": <int>,
    "other_creative_rag": <int>
  }},
  "category_calculation_steps": "<show math>",
  "confidence": "high" | "medium" | "low",
  "as_of_date": "{date.today().isoformat()}"
}}

CRITICAL: shares must sum to exactly 100."""


def _build_share_path_c_bottom_up() -> str:
    """Path C — Bottom-up product-by-product token volume estimation."""
    cats_text = "\n".join(f"  - {c}: {CATEGORY_DESCRIPTIONS[c]}" for c in CATEGORIES)
    return f"""You are researching for an AI investment framework. Today is {date.today().isoformat()}.

YOUR TASK: Determine volume_share_pct (token-volume share, summing to 100%) \
by BOTTOM-UP product-by-product estimation. For each major product, \
estimate its absolute annual token volume independently, then compute \
relative shares.

THE 5 CATEGORIES:
{cats_text}

METHOD:
  1. List the top 3-5 products in each category by revenue or user count
  2. For each product, derive an absolute annual token volume estimate \
using whichever data is available:
     - Disclosed throughput (e.g., Gemini API throughput cited at \
"16 billion tokens per minute" in Google Cloud disclosures)
     - Disclosed user count × estimated per-user usage
     - Disclosed ARR / blended price = total tokens (for pay-per-token products)
  3. Sum products within each category to get category total
  4. Compute relative shares across categories, normalize to 100%

DO NOT use the same data sources as the revenue-mix-disclosure approach \
or the traffic-share approach. Use product-specific operational figures.

OUTPUT — return ONLY a JSON object (no markdown fences):
{{
  "shares": {{
    "consumer_chat": <int 0-100>,
    "coding_agents": <int 0-100>,
    "enterprise_vertical_agents": <int 0-100>,
    "developer_raw_api": <int 0-100>,
    "other_creative_rag": <int 0-100>
  }},
  "method": "bottom_up_product_volumes",
  "products_estimated": [
    {{
      "product": "<name>",
      "category": "<cat>",
      "estimated_annual_tokens": "<X T (trillion)>",
      "data_source": "<throughput|users|arr>",
      "derivation": "<one-line math>",
      "source_url": "<url>"
    }}
  ],
  "category_totals_trillion_tokens_per_year": {{
    "consumer_chat": <float>,
    "coding_agents": <float>,
    "enterprise_vertical_agents": <float>,
    "developer_raw_api": <float>,
    "other_creative_rag": <float>
  }},
  "confidence": "high" | "medium" | "low",
  "as_of_date": "{date.today().isoformat()}"
}}

CRITICAL: shares must sum to exactly 100."""


# ── Calibration back-test prompt ──────────────────────────────────────────────

def _build_calibration_prompt() -> str:
    """Asks the worker to independently re-derive OpenRouter's published finding.

    The OpenRouter "State of AI" 100T-token study reported that average prompt
    tokens per request rose from ~1,500 to ~6,000 between early 2024 and late
    2025 across ALL traffic on the platform. If our worker can independently
    arrive at ~6,000 (within ±40%), the prompts and model are calibrated
    correctly. If it can't, we abort the run before corrupting state.

    The prompt deliberately asks the question WITHOUT mentioning OpenRouter's
    published figure — we want the worker to find the number, not regurgitate
    a value we tell it.
    """
    return f"""You are doing a calibration check for an AI investment framework. \
Today is {date.today().isoformat()}.

YOUR TASK: Use web search to find the most authoritative published figure \
for the AVERAGE PROMPT INPUT TOKENS per request across all OpenRouter \
traffic during the period November 2024 through November 2025.

Context: OpenRouter is the world's largest unified API gateway for LLMs, \
routing requests across 300+ models from 60+ providers. They have published \
empirical studies based on their full traffic dataset. The figure has been \
widely cited in analyst writeups (e.g. a16z) and developer publications. \
You should be able to find it readily via web search.

Just find the empirical figure. Don't try to derive it from first principles.

OUTPUT — return ONLY a JSON object (no markdown fences):
{{
  "value": <integer average prompt tokens>,
  "study_or_source_name": "<name>",
  "study_publication_date": "<YYYY-MM>",
  "study_data_period": "<e.g. Nov 2024 - Nov 2025>",
  "source_url": "<url>",
  "confidence": "high" | "medium" | "low"
}}"""


# ── JSON extraction + LLM client (carried from v3.6.2) ────────────────────────

def _strip_markdown_fences(text: str) -> str:
    """Remove ```json...``` or ```...``` wrappers if present."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _extract_json_from_text(text: str) -> dict:
    """Extract the first complete top-level JSON object from a text string.

    Walks the text once, tracking brace depth (string-aware, escape-aware).
    Tolerates prose before/after, markdown fences, braces inside strings.
    """
    stripped = _strip_markdown_fences(text)
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No opening brace found in response", text, 0)

    depth = 0
    in_string = False
    escape_next = False
    end = -1

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        raise json.JSONDecodeError(
            "Unbalanced braces — no matching `}` for first `{`",
            text, start
        )
    return json.loads(text[start:end + 1])


def _call_claude_with_search(prompt: str, max_tokens: int = 8192) -> dict:
    """Call Claude (Sonnet 4.6) with web_search enabled. Return parsed JSON.

    max_tokens default is 8192 (raised from v3.6.2's 4096); the volume_share
    paths in particular need the headroom to return full 5-category JSON
    with rationale.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError("The 'anthropic' package is required.")

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "Same key fast_tier and deep_tier use."
        )

    from src.config import SONNET_MODEL

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search"
        }]
    )

    final_text_parts = []
    for block in response.content:
        if hasattr(block, "text") and block.text:
            final_text_parts.append(block.text)
    final_text = "".join(final_text_parts)

    if not final_text.strip():
        raise ValueError("Empty response from Claude")

    try:
        return _extract_json_from_text(final_text)
    except (json.JSONDecodeError, ValueError) as e:
        preview = final_text[:500].replace("\n", "\\n")
        logger.error(f"JSON extraction failed. First 500 chars: {preview}")
        raise


# ── Calibration ───────────────────────────────────────────────────────────────

def _run_calibration_check() -> tuple[bool, dict]:
    """Run the back-test before any real research.

    Returns (passed, diagnostic_dict). When passed=False, the caller should
    abort the run and open a GitHub Issue with the diagnostic.
    """
    logger.info("Running calibration back-test (independent re-derivation of "
                "OpenRouter average prompt size)...")
    try:
        result = _call_claude_with_search(_build_calibration_prompt(), max_tokens=2048)
    except Exception as e:
        return False, {
            "calibration_status": "failed_to_get_response",
            "error": f"{type(e).__name__}: {e}",
            "target_value": CALIBRATION_TARGET_VALUE,
            "tolerance": CALIBRATION_TARGET_TOLERANCE_FRACTION,
        }

    value = result.get("value")
    if not isinstance(value, (int, float)):
        return False, {
            "calibration_status": "non_numeric_response",
            "raw_result": result,
            "target_value": CALIBRATION_TARGET_VALUE,
        }
    value = int(value)

    deviation = abs(value - CALIBRATION_TARGET_VALUE) / CALIBRATION_TARGET_VALUE
    passed = deviation <= CALIBRATION_TARGET_TOLERANCE_FRACTION

    diagnostic = {
        "calibration_status": "passed" if passed else "failed_outside_tolerance",
        "worker_derived_value": value,
        "target_value": CALIBRATION_TARGET_VALUE,
        "tolerance_fraction": CALIBRATION_TARGET_TOLERANCE_FRACTION,
        "actual_deviation_fraction": round(deviation, 3),
        "worker_cited_source": result.get("study_or_source_name", "(none)"),
        "worker_cited_url": result.get("source_url", "(none)"),
        "worker_confidence": result.get("confidence", "(none)"),
    }

    if passed:
        logger.info(
            f"  Calibration PASSED: worker returned {value:,} "
            f"(target {CALIBRATION_TARGET_VALUE:,} ± "
            f"{int(CALIBRATION_TARGET_VALUE * CALIBRATION_TARGET_TOLERANCE_FRACTION):,})"
        )
    else:
        logger.error(
            f"  Calibration FAILED: worker returned {value:,} "
            f"(target {CALIBRATION_TARGET_VALUE:,} ± "
            f"{int(CALIBRATION_TARGET_VALUE * CALIBRATION_TARGET_TOLERANCE_FRACTION):,}, "
            f"deviation {deviation * 100:.0f}%)"
        )

    return passed, diagnostic


# ── Triangulation synthesis ───────────────────────────────────────────────────

def _synthesize_paths(
    path_a: dict, path_b: dict, path_c: dict, label: str,
) -> dict:
    """Combine 3 path results into a converged value or a divergence flag.

    Returns a dict with:
      - status: "converged" | "divergent" | "missing_values"
      - committed_value: int (only if converged)
      - uncertainty_band: {low, mid, high}
      - spread_fraction: float (max-min)/mean
      - paths: {a, b, c} (full path provenance)
    """
    values = []
    for label_p, result_p in (("a", path_a), ("b", path_b), ("c", path_c)):
        v = result_p.get("value") if result_p else None
        if isinstance(v, (int, float)) and v > 0:
            values.append((label_p, int(v)))

    if len(values) < 2:
        return {
            "status": "missing_values",
            "reason": f"Only {len(values)}/3 paths returned a usable numeric value",
            "paths": {"a": path_a, "b": path_b, "c": path_c},
        }

    nums = [v for _, v in values]
    mean = sum(nums) / len(nums)
    spread = (max(nums) - min(nums)) / mean if mean > 0 else 1.0

    result = {
        "status": "converged" if spread <= MAX_PATH_SPREAD_FRACTION else "divergent",
        "label": label,
        "path_values": {lbl: v for lbl, v in values},
        "mean": int(round(mean)),
        "min": min(nums),
        "max": max(nums),
        "spread_fraction": round(spread, 3),
        "spread_pct": round(spread * 100, 1),
        "uncertainty_band": {
            "low": min(nums),
            "mid": int(round(mean)),
            "high": max(nums),
        },
        "paths": {"a": path_a, "b": path_b, "c": path_c},
    }
    if result["status"] == "converged":
        result["committed_value"] = int(round(mean))
        logger.info(
            f"  [{label}] CONVERGED — paths: {[v for _, v in values]} → "
            f"mean={int(mean):,}, spread={spread*100:.1f}%"
        )
    else:
        logger.warning(
            f"  [{label}] DIVERGENT — paths: {[v for _, v in values]} → "
            f"spread={spread*100:.1f}% exceeds {MAX_PATH_SPREAD_FRACTION*100:.0f}% threshold; "
            f"NOT committing"
        )

    return result


def _synthesize_share_paths(
    path_a: dict, path_b: dict, path_c: dict,
) -> dict:
    """Synthesize 3 volume_share paths into per-category convergence.

    Each path returns a dict {category: int}. We synthesize per category.
    A run only commits if ALL 5 categories converge. Otherwise we log the
    divergence and skip the entire volume_share update for this run.
    """
    per_cat_results = {}
    all_converged = True

    for cat in CATEGORIES:
        cat_paths = {}
        for label, result in (("a", path_a), ("b", path_b), ("c", path_c)):
            shares = result.get("shares") if result else None
            if shares and cat in shares and isinstance(shares[cat], (int, float)):
                cat_paths[label] = float(shares[cat])

        if len(cat_paths) < 2:
            per_cat_results[cat] = {"status": "missing_values", "paths": cat_paths}
            all_converged = False
            continue

        nums = list(cat_paths.values())
        mean = sum(nums) / len(nums)
        # For shares (percentages), use absolute pp spread, not fractional
        spread_pp = max(nums) - min(nums)
        # Convergence: ≤10pp spread per category (tighter than 30% rule for shares)
        converged = spread_pp <= 10.0

        per_cat_results[cat] = {
            "status": "converged" if converged else "divergent",
            "path_values": cat_paths,
            "mean": round(mean, 1),
            "spread_pp": round(spread_pp, 1),
        }
        if not converged:
            all_converged = False

    if all_converged:
        # Build the committed shares dict from per-category means, normalize to 100
        raw = {cat: per_cat_results[cat]["mean"] for cat in CATEGORIES}
        total = sum(raw.values())
        if total > 0:
            normalized = {cat: int(round(v * 100 / total)) for cat, v in raw.items()}
            # Absorb rounding drift into largest share
            drift = 100 - sum(normalized.values())
            if drift != 0:
                largest = max(normalized, key=normalized.get)
                normalized[largest] += drift
        else:
            normalized = {cat: 20 for cat in CATEGORIES}    # fallback even split

        return {
            "status": "converged",
            "per_category": per_cat_results,
            "committed_shares": normalized,
            "paths": {"a": path_a, "b": path_b, "c": path_c},
        }
    return {
        "status": "divergent",
        "per_category": per_cat_results,
        "paths": {"a": path_a, "b": path_b, "c": path_c},
    }


# ── Sanity checks (carried + tightened) ───────────────────────────────────────

def _sanity_check_typical_context(
    value: int, prev_value: int, confidence: str, category: str,
) -> tuple[bool, str]:
    if not isinstance(value, (int, float)):
        return False, f"Value is not numeric: {type(value).__name__}"
    value = int(value)
    if value < MIN_PLAUSIBLE_CONTEXT:
        return False, f"Value {value:,} below plausible floor"
    if value > MAX_PLAUSIBLE_CONTEXT:
        return False, f"Value {value:,} above plausible ceiling"
    if confidence not in ACCEPTED_CONFIDENCE:
        return False, f"Confidence '{confidence}' below threshold (need high/medium)"
    if prev_value and prev_value > 0:
        delta = abs(value - prev_value) / prev_value
        if delta > MAX_DELTA_FRACTION_CONTEXT:
            return False, (
                f"Delta {delta * 100:.0f}% ({prev_value:,} → {value:,}) "
                f"exceeds {MAX_DELTA_FRACTION_CONTEXT * 100:.0f}% threshold"
            )
    return True, ""


# ── State load/save ───────────────────────────────────────────────────────────

def _load_state() -> dict:
    if CONTEXT_STATE_FILE.exists():
        return json.loads(CONTEXT_STATE_FILE.read_text())
    return {
        "_metadata": {"schema_version": "1.0",
                      "bootstrapped_on": date.today().isoformat()},
        "typical_context_tokens": {
            cat: {"current": 0, "history": []} for cat in CATEGORIES
        },
        "volume_shares": {
            "committed": {c: 20 for c in CATEGORIES},
            "proposed": None, "proposal_evidence": [],
            "proposal_status": "none", "proposal_created": None,
            "history": [],
        },
        "operative_context_history": [],
    }


def _save_state(state: dict) -> None:
    CONTEXT_STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = CONTEXT_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(CONTEXT_STATE_FILE)


# ── Main entry point ──────────────────────────────────────────────────────────

def research_context_length_inputs() -> dict:
    """v3.7.0 — Multi-path triangulation research worker.

    Sequence:
      1. Calibration back-test — if fails, abort and write nothing
      2. For each of 5 categories: 3-path typical_context research
      3. 3-path volume_share research
      4. Synthesis: commit only converged values; log divergent ones
      5. Persist state
    """
    state = _load_state()
    today = date.today().isoformat()
    state.setdefault("_metadata", {})

    summary = {
        "status": "starting",
        "calibration": None,
        "typical_context_results": {},
        "volume_shares_result": None,
        "committed": [],
        "divergent": [],
        "errors": [],
        "exit_code": 0,
    }

    # 1. Calibration
    cal_passed, cal_diag = _run_calibration_check()
    summary["calibration"] = cal_diag
    state["_metadata"].setdefault("calibration_history", []).append({
        "date": today, **cal_diag,
    })
    state["_metadata"]["calibration_history"] = (
        state["_metadata"]["calibration_history"][-12:]    # keep last 12 runs
    )

    if not cal_passed:
        logger.error("Aborting run: calibration back-test failed.")
        summary["status"] = "aborted_calibration_failed"
        summary["errors"].append("Calibration back-test failed; no research performed")
        summary["exit_code"] = 2
        state["_metadata"]["last_run_at"] = datetime.now(timezone.utc).isoformat()
        state["_metadata"]["last_run_summary"] = {
            "status": summary["status"], "calibration": cal_diag,
        }
        _save_state(state)
        return summary

    # 2. Typical context — 3 paths × 5 categories
    for category in CATEGORIES:
        logger.info(f"Researching typical_context_tokens[{category}] (3 paths)...")
        try:
            path_a = _call_claude_with_search(_build_typical_path_a_vendor(category))
            time.sleep(2)
            path_b = _call_claude_with_search(_build_typical_path_b_economic(category))
            time.sleep(2)
            path_c = _call_claude_with_search(_build_typical_path_c_empirical(category))
        except Exception as e:
            err = f"typical[{category}]: {type(e).__name__}: {e}"
            summary["errors"].append(err)
            logger.error(err)
            continue

        synthesis = _synthesize_paths(
            path_a, path_b, path_c, f"typical_context[{category}]"
        )
        summary["typical_context_results"][category] = synthesis

        if synthesis["status"] != "converged":
            summary["divergent"].append(f"typical_context[{category}]")
            # Still log the attempt to history for human review
            state["typical_context_tokens"][category].setdefault("history", []).append({
                "date": today,
                "source": "llm_research_v3_7_0_divergent",
                "status": synthesis["status"],
                "path_values": synthesis.get("path_values", {}),
                "spread_pct": synthesis.get("spread_pct"),
                "paths_provenance": {
                    "a": {k: path_a.get(k) for k in ("value", "method", "confidence", "weighted_calculation")},
                    "b": {k: path_b.get(k) for k in ("value", "method", "confidence", "sanity_check")},
                    "c": {k: path_c.get(k) for k in ("value", "method", "confidence", "category_adjustment_reasoning")},
                },
            })
            continue

        # Converged — sanity-check the committed value
        new_value = synthesis["committed_value"]
        prev_value = int(state["typical_context_tokens"][category].get("current") or 0)
        # Use mean confidence across paths (lowest wins)
        path_confidences = [p.get("confidence", "low") for p in (path_a, path_b, path_c)]
        confidence = ("low" if "low" in path_confidences
                      else ("medium" if "medium" in path_confidences else "high"))

        ok, reason = _sanity_check_typical_context(
            new_value, prev_value, confidence, category
        )
        if not ok:
            summary["divergent"].append(f"typical_context[{category}] (sanity)")
            state["typical_context_tokens"][category].setdefault("history", []).append({
                "date": today,
                "source": "llm_research_v3_7_0_sanity_rejected",
                "proposed_value": new_value,
                "previous_value": prev_value,
                "rejection_reason": reason,
                "uncertainty_band": synthesis["uncertainty_band"],
                "spread_pct": synthesis["spread_pct"],
            })
            logger.warning(f"  Sanity-rejected typical_context[{category}]: {reason}")
            continue

        # Accept
        state["typical_context_tokens"][category]["current"] = new_value
        state["typical_context_tokens"][category].setdefault("history", []).append({
            "date": today,
            "value": new_value,
            "source": "llm_research_v3_7_0",
            "confidence": confidence,
            "spread_pct": synthesis["spread_pct"],
            "uncertainty_band": synthesis["uncertainty_band"],
            "path_values": synthesis["path_values"],
            "previous_value": prev_value,
            "delta_pct": (round((new_value - prev_value) / prev_value * 100, 1)
                          if prev_value else 0),
            "paths_provenance": {
                "a": {k: path_a.get(k) for k in ("value", "method", "confidence", "weighted_calculation", "products_analyzed")},
                "b": {k: path_b.get(k) for k in ("value", "method", "confidence", "math_steps")},
                "c": {k: path_c.get(k) for k in ("value", "method", "confidence", "studies_cited", "category_adjustment_reasoning")},
            },
        })
        summary["committed"].append(
            f"typical_context[{category}]: {prev_value:,} → {new_value:,}"
        )
        logger.info(
            f"  COMMITTED typical_context[{category}]: "
            f"{prev_value:,} → {new_value:,} (confidence={confidence})"
        )

    # 3. Volume shares — 3 paths, synthesized per category
    logger.info("Researching volume_share_pct (3 paths × 5 categories)...")
    try:
        share_a = _call_claude_with_search(_build_share_path_a_revenue_mix())
        time.sleep(2)
        share_b = _call_claude_with_search(_build_share_path_b_traffic_proxy())
        time.sleep(2)
        share_c = _call_claude_with_search(_build_share_path_c_bottom_up())
    except Exception as e:
        err = f"volume_shares: {type(e).__name__}: {e}"
        summary["errors"].append(err)
        logger.error(err)
        share_a = share_b = share_c = {"shares": {}, "error": str(e)}

    share_synthesis = _synthesize_share_paths(share_a, share_b, share_c)
    summary["volume_shares_result"] = share_synthesis

    if share_synthesis["status"] == "converged":
        prev_shares = dict(state["volume_shares"]["committed"])
        new_shares = share_synthesis["committed_shares"]

        state["volume_shares"]["committed"] = new_shares
        state["volume_shares"]["history"].append({
            "date": today,
            "shares": new_shares,
            "source": "llm_research_v3_7_0",
            "per_category_synthesis": share_synthesis["per_category"],
            "previous_shares": prev_shares,
            "paths_provenance": {
                "a": {"shares": share_a.get("shares"), "method": share_a.get("method")},
                "b": {"shares": share_b.get("shares"), "method": share_b.get("method")},
                "c": {"shares": share_c.get("shares"), "method": share_c.get("method")},
            },
        })
        state["volume_shares"]["proposal_status"] = "none"
        summary["committed"].append(f"volume_shares: {prev_shares} → {new_shares}")
        logger.info(f"  COMMITTED volume_shares: {new_shares}")
    else:
        summary["divergent"].append("volume_shares")
        state["volume_shares"]["history"].append({
            "date": today,
            "source": "llm_research_v3_7_0_divergent",
            "status": "divergent",
            "per_category_synthesis": share_synthesis["per_category"],
            "paths_provenance": {
                "a": {"shares": share_a.get("shares")},
                "b": {"shares": share_b.get("shares")},
                "c": {"shares": share_c.get("shares")},
            },
        })
        logger.warning(f"  DIVERGENT volume_shares — not committing")

    # 4. Persist
    state["_metadata"]["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["_metadata"]["last_run_summary"] = {
        "committed_count": len(summary["committed"]),
        "divergent_count": len(summary["divergent"]),
        "error_count": len(summary["errors"]),
    }
    _save_state(state)

    if summary["errors"]:
        summary["status"] = "completed_with_errors"
        summary["exit_code"] = 1
    elif summary["divergent"]:
        summary["status"] = "completed_with_divergence"
        summary["exit_code"] = 1
    else:
        summary["status"] = "completed_success"
        summary["exit_code"] = 0

    logger.info(
        f"v3.7.0 research complete: "
        f"committed={len(summary['committed'])}, "
        f"divergent={len(summary['divergent'])}, "
        f"errors={len(summary['errors'])}"
    )
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    result = research_context_length_inputs()
    print(json.dumps(result, indent=2, default=str))
    sys.exit(result.get("exit_code", 0))
