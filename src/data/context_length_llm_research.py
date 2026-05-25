"""Context length research worker — v3.7.1 (2026-05-25).

Architecture (v3.7.1 update over v3.7.0):
  - 3-path triangulation per input is unchanged (Path A vendor, B economic, C empirical)
  - Calibration back-test is unchanged (must independently re-derive ~6,000 from OpenRouter)
  - SYNTHESIS LOGIC CHANGED — convergence gate removed; floor + max-rule replaces it

For typical_context_tokens (per category):
  candidate = max(path_B_economic, mean(path_A, path_B, path_C))
  final     = max(candidate, SEED_FLOOR[category])

  Path C is INCLUDED in the math (the mean accounts for all three paths).
  The seed floor is the never-go-below anchor; outcomes can never go lower.
  Order-of-magnitude work: we pick the higher of the economic path or the mean.

For volume_share_pct (per category):
  candidate = mean(path_A, path_B)        # Path C IGNORED in math (recorded in history only)
  final     = candidate, renormalized to sum to 100 if needed

  Path C is recorded for provenance but excluded from math because empirical
  share studies (e.g., OpenRouter) are themselves developer-API-only and miss
  consumer chat traffic entirely. The May 25 first run had Path C reporting
  developer_raw_api at 68% (vs 13% from B and 33% from A); the methodology
  is structurally biased for shares.

REMOVED in v3.7.1:
  - 30% convergence gate (was: spread > 30% blocked commits)
  - 50% delta sanity check (was: |new - prev| / prev > 50% blocked commits)
  - Floor + max-rule replaces both as the validity anchor; first run commits
    everything at seed floor (proven safe baseline), future runs ratchet up
    only when measured behavior genuinely exceeds the conservative floor.

KEPT in v3.7.1:
  - Calibration back-test (catches catastrophic prompt drift before any commits)
  - Full provenance recording (every history entry includes all 3 path values,
    candidate, floor, final, which-rule-was-binding)
  - Plausibility bounds (committed values must be 1K-10M tokens, paths must
    be numeric — catches outright garbage but doesn't gate on disagreement)

Cost: ~19 LLM calls per run × $0.03/call × 6 runs/year ≈ $3.50/year (unchanged).
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

# v3.7.1 — calibration back-test parameters.
# The worker independently re-derives the OpenRouter 100T-token study finding
# ("average prompt tokens per request increased ~4x from 1,500 to over 6,000
# between early 2024 and late 2025"). If the worker's independently-derived
# answer falls outside this band, prompts have likely drifted and we abort.
CALIBRATION_TARGET_VALUE = 6_000
CALIBRATION_TARGET_TOLERANCE_FRACTION = 0.40    # ±40% = 3,600-8,400

# v3.7.1 — seed floors for typical_context_tokens.
# Committed values can never go below these. The floors reflect the May 25
# deep-dive research baseline, anchored to public sources documented in
# state/context_length_state.json history entries. Forward-looking signal lives
# in the framework's +1 Y-band anticipation premium (src/analysis/dhcr.py),
# not in these floors and not in any X-axis multiplier.
SEED_FLOORS_TYPICAL_CONTEXT = {
    "consumer_chat": 8_500,
    "coding_agents": 100_000,
    "enterprise_vertical_agents": 65_000,
    "developer_raw_api": 40_000,
    "other_creative_rag": 8_500,
}

# Plausibility bounds (carried from v3.6.2) — catch outright garbage but don't
# gate on path disagreement. The floor + max-rule does the validity work.
MIN_PLAUSIBLE_CONTEXT = 1_000
MAX_PLAUSIBLE_CONTEXT = 10_000_000

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
    path_a: dict, path_b: dict, path_c: dict, category: str,
) -> dict:
    """v3.7.1 — Floor + max(economic, mean) synthesis.

    Rule:
        candidate = max(path_B_economic, mean(all_available_paths))
        final     = max(candidate, SEED_FLOORS_TYPICAL_CONTEXT[category])

    The seed floor is the never-go-below anchor. Order-of-magnitude work:
    we pick the higher of the economic path or the mean across all paths.
    All three paths participate in the mean (Path C included), but the
    economic path gets a "max" privilege because it derives from disclosed
    throughput / revenue / pricing — the most rigorously deterministic of
    the three methodologies.

    Returns:
        Dict with full provenance — path values, candidate, floor, final,
        which-rule-was-binding, raw paths. ALWAYS commits (no divergence
        gate); divergence info is recorded in spread_pct field for audit.
    """
    # Collect numeric values from each path
    values_by_label = {}
    for label, result in (("a", path_a), ("b", path_b), ("c", path_c)):
        v = result.get("value") if result else None
        if isinstance(v, (int, float)) and v > 0:
            values_by_label[label] = int(v)

    seed_floor = SEED_FLOORS_TYPICAL_CONTEXT.get(category, MIN_PLAUSIBLE_CONTEXT)

    if not values_by_label:
        # All paths failed — commit floor as a defensive baseline
        return {
            "status": "all_paths_failed_floor_applied",
            "label": f"typical_context[{category}]",
            "path_values": {},
            "candidate": None,
            "seed_floor": seed_floor,
            "committed_value": seed_floor,
            "binding_rule": "floor",
            "paths": {"a": path_a, "b": path_b, "c": path_c},
        }

    nums = list(values_by_label.values())
    mean = int(round(sum(nums) / len(nums)))
    spread_fraction = ((max(nums) - min(nums)) / mean) if mean > 0 else 0.0

    # Path B (economic) gets the "max" privilege — if present, candidate is
    # max(B, mean). If B is missing, fall back to mean of the other paths.
    path_b_value = values_by_label.get("b")
    if path_b_value is not None:
        candidate = max(path_b_value, mean)
        candidate_source = "max(path_B_economic, mean_of_paths)"
    else:
        candidate = mean
        candidate_source = "mean_of_paths_(path_B_missing)"

    # Apply seed floor — never go below it
    final = max(candidate, seed_floor)
    binding_rule = "floor" if final == seed_floor and candidate < seed_floor else "candidate"

    # Final plausibility sanity (catches outright garbage like negative or 100M-token values)
    final = min(max(final, MIN_PLAUSIBLE_CONTEXT), MAX_PLAUSIBLE_CONTEXT)

    result = {
        "status": "committed",
        "label": f"typical_context[{category}]",
        "path_values": values_by_label,
        "mean_of_paths": mean,
        "candidate": candidate,
        "candidate_source": candidate_source,
        "seed_floor": seed_floor,
        "committed_value": final,
        "binding_rule": binding_rule,
        "spread_fraction": round(spread_fraction, 3),
        "spread_pct": round(spread_fraction * 100, 1),    # informational only
        "paths": {"a": path_a, "b": path_b, "c": path_c},
    }

    if binding_rule == "floor":
        logger.info(
            f"  [{category}] FLOOR BINDING — paths {nums}, mean={mean:,}, "
            f"candidate={candidate:,} < floor={seed_floor:,} → committed {final:,}"
        )
    else:
        logger.info(
            f"  [{category}] CANDIDATE — paths {nums}, mean={mean:,}, "
            f"candidate={candidate:,} ≥ floor={seed_floor:,} → committed {final:,}"
        )

    return result


def _synthesize_share_paths(
    path_a: dict, path_b: dict, path_c: dict,
) -> dict:
    """v3.7.1 — Volume shares synthesized as mean(A, B), Path C ignored.

    Rule:
        per_category[i] = mean(path_A[i], path_B[i])
        committed = normalize_to_100(per_category)

    Path C is RECORDED in history for provenance but EXCLUDED from the math.
    The empirical study landscape for share-of-volume is structurally biased
    (OpenRouter is the only large-scale dataset and is 100% developer API
    traffic, so Path C systematically over-counts dev_raw_api). The May 25
    first run had Path C reporting dev_raw_api at 68% vs A=33% and B=13% —
    a clear methodology problem, not a measurement issue.

    No floors on shares (zero-sum constraint), no divergence gate.
    """
    shares_a = path_a.get("shares") if path_a else None
    shares_b = path_b.get("shares") if path_b else None
    shares_c = path_c.get("shares") if path_c else {}    # for provenance only

    per_cat_synthesis = {}
    raw_means = {}

    for cat in CATEGORIES:
        a_val = shares_a.get(cat) if shares_a else None
        b_val = shares_b.get(cat) if shares_b else None
        c_val = shares_c.get(cat) if shares_c else None    # informational

        usable = [v for v in (a_val, b_val) if isinstance(v, (int, float))]
        if len(usable) == 2:
            mean_ab = sum(usable) / 2.0
        elif len(usable) == 1:
            mean_ab = float(usable[0])    # one path missing; use the other
        else:
            mean_ab = None    # both paths missing for this category

        per_cat_synthesis[cat] = {
            "path_a": a_val,
            "path_b": b_val,
            "path_c_informational": c_val,
            "mean_ab": mean_ab,
        }
        if mean_ab is not None:
            raw_means[cat] = mean_ab

    if len(raw_means) < len(CATEGORIES):
        # Some categories failed entirely — can't normalize confidently
        return {
            "status": "incomplete_paths",
            "per_category": per_cat_synthesis,
            "missing_categories": [c for c in CATEGORIES if c not in raw_means],
            "paths": {"a": path_a, "b": path_b, "c": path_c},
        }

    # Normalize to exactly 100 (integer shares)
    total = sum(raw_means.values())
    if total > 0:
        normalized_floats = {c: v * 100 / total for c, v in raw_means.items()}
    else:
        normalized_floats = {c: 20.0 for c in CATEGORIES}

    # Round to ints and absorb drift into largest share
    normalized = {c: int(round(v)) for c, v in normalized_floats.items()}
    drift = 100 - sum(normalized.values())
    if drift != 0:
        largest = max(normalized, key=normalized.get)
        normalized[largest] += drift

    logger.info(
        f"  volume_shares mean(A,B) raw={raw_means}, sum={total:.1f}, "
        f"normalized to 100 → {normalized}"
    )

    return {
        "status": "committed",
        "per_category": per_cat_synthesis,
        "raw_means_pre_normalization": raw_means,
        "committed_shares": normalized,
        "paths": {"a": path_a, "b": path_b, "c": path_c},
    }


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
    """v3.7.1 — Floor + max(B, mean) synthesis. Always commits.

    Sequence:
      1. Calibration back-test — if fails, abort and write nothing
      2. For each of 5 categories: 3-path typical_context research, then
         apply floor + max(B, mean) rule. Always commits a value.
      3. 3-path volume_share research, then mean(A, B) ignoring C.
         Normalize to 100; commit unless math fails entirely.
      4. Persist state with full provenance per history entry.
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
        "floor_bound": [],
        "errors": [],
        "exit_code": 0,
    }

    # 1. Calibration — same as v3.7.0
    cal_passed, cal_diag = _run_calibration_check()
    summary["calibration"] = cal_diag
    state["_metadata"].setdefault("calibration_history", []).append({
        "date": today, **cal_diag,
    })
    state["_metadata"]["calibration_history"] = (
        state["_metadata"]["calibration_history"][-12:]
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

    # 2. Typical context — 3 paths × 5 categories, floor + max-rule synthesis
    for category in CATEGORIES:
        logger.info(f"Researching typical_context_tokens[{category}] (3 paths)...")
        path_a = path_b = path_c = None
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
            # Even if some paths fail, synthesis below handles missing values

        synthesis = _synthesize_paths(
            path_a or {}, path_b or {}, path_c or {}, category
        )
        summary["typical_context_results"][category] = synthesis

        new_value = synthesis["committed_value"]
        prev_value = int(state["typical_context_tokens"][category].get("current") or 0)

        # Always commit — the floor + max-rule has already done the validity work
        state["typical_context_tokens"][category]["current"] = new_value
        state["typical_context_tokens"][category].setdefault("history", []).append({
            "date": today,
            "value": new_value,
            "source": "llm_research_v3_7_1",
            "synthesis_rule": "max(path_B_economic, mean(A,B,C)) floored at seed",
            "path_values": synthesis.get("path_values", {}),
            "mean_of_paths": synthesis.get("mean_of_paths"),
            "candidate": synthesis.get("candidate"),
            "candidate_source": synthesis.get("candidate_source"),
            "seed_floor": synthesis.get("seed_floor"),
            "binding_rule": synthesis.get("binding_rule"),
            "spread_pct": synthesis.get("spread_pct"),    # informational; no gate
            "previous_value": prev_value,
            "delta_pct": (round((new_value - prev_value) / prev_value * 100, 1)
                          if prev_value else 0),
            "paths_provenance": {
                "a": {k: (path_a or {}).get(k)
                      for k in ("value", "method", "confidence",
                                "weighted_calculation", "products_analyzed")},
                "b": {k: (path_b or {}).get(k)
                      for k in ("value", "method", "confidence",
                                "math_steps", "sanity_check")},
                "c": {k: (path_c or {}).get(k)
                      for k in ("value", "method", "confidence",
                                "studies_cited", "category_adjustment_reasoning")},
            },
        })

        if synthesis.get("binding_rule") == "floor":
            summary["floor_bound"].append(
                f"typical_context[{category}]: floor {new_value:,} "
                f"(candidate was {synthesis.get('candidate'):,})"
            )
        summary["committed"].append(
            f"typical_context[{category}]: {prev_value:,} → {new_value:,} "
            f"({synthesis.get('binding_rule')})"
        )

    # 3. Volume shares — mean(A, B), Path C ignored
    logger.info("Researching volume_share_pct (3 paths × 5 categories; C ignored in math)...")
    share_a = share_b = share_c = None
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

    share_synthesis = _synthesize_share_paths(share_a or {}, share_b or {}, share_c or {})
    summary["volume_shares_result"] = share_synthesis

    if share_synthesis["status"] == "committed":
        prev_shares = dict(state["volume_shares"]["committed"])
        new_shares = share_synthesis["committed_shares"]
        state["volume_shares"]["committed"] = new_shares
        state["volume_shares"]["history"].append({
            "date": today,
            "shares": new_shares,
            "source": "llm_research_v3_7_1",
            "synthesis_rule": "mean(path_A_revenue_mix, path_B_traffic_proxy), path_C_ignored",
            "raw_means_pre_normalization": share_synthesis["raw_means_pre_normalization"],
            "per_category": share_synthesis["per_category"],
            "previous_shares": prev_shares,
            "paths_provenance": {
                "a": {"shares": (share_a or {}).get("shares"),
                      "method": (share_a or {}).get("method")},
                "b": {"shares": (share_b or {}).get("shares"),
                      "method": (share_b or {}).get("method")},
                "c_informational_only": {"shares": (share_c or {}).get("shares"),
                                         "method": (share_c or {}).get("method"),
                                         "note": "v3.7.1 excludes path C from share math; "
                                                 "logged for provenance only"},
            },
        })
        state["volume_shares"]["proposal_status"] = "none"
        summary["committed"].append(f"volume_shares: {prev_shares} → {new_shares}")
        logger.info(f"  COMMITTED volume_shares: {new_shares}")
    else:
        # Paths failed for at least some categories — can't normalize safely
        summary["errors"].append(
            f"volume_shares synthesis incomplete: {share_synthesis.get('missing_categories')}"
        )
        state["volume_shares"]["history"].append({
            "date": today,
            "source": "llm_research_v3_7_1_incomplete",
            "status": "incomplete",
            "per_category": share_synthesis.get("per_category"),
        })

    # 4. Persist
    state["_metadata"]["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["_metadata"]["last_run_summary"] = {
        "committed_count": len(summary["committed"]),
        "floor_bound_count": len(summary["floor_bound"]),
        "error_count": len(summary["errors"]),
    }
    _save_state(state)

    if summary["errors"]:
        summary["status"] = "completed_with_errors"
        summary["exit_code"] = 1
    else:
        summary["status"] = "completed_success"
        summary["exit_code"] = 0

    logger.info(
        f"v3.7.1 research complete: "
        f"committed={len(summary['committed'])}, "
        f"floor_bound={len(summary['floor_bound'])}, "
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
