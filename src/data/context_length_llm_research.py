"""Context length research worker (v3.6.0, 2026-05-24).

Replaces:
  - src/data/context_length_scraper.py (v3.4.14) — bespoke regex scraping
  - src/analysis/context_length_aggregator.py (v3.4.14) — extraction_log signals

Why LLM-driven instead of scrapers:
  - The data we need (revenue mix, web traffic share, vendor context windows,
    ARR figures) lives in news articles, earnings transcripts, analyst reports.
    These are LLM-natural sources; regex breaks on them weekly.
  - One worker, one cadence, no per-source fragility.
  - Cost: ~$0.50 per run × 6 runs/year = ~$3/year. Negligible.

Operating model:
  - Bi-monthly schedule (1st of Jan/Mar/May/Jul/Sep/Nov, 06:00 UTC)
  - Manual workflow_dispatch trigger as backup
  - Each run re-researches:
      * typical_context_tokens for each of 5 workload categories (5 calls)
      * volume_share_pct distribution across all categories (1 call)
  - Each value is sanity-checked before commit; deltas >50% (or shares >15pp
    per category) are REJECTED, previous values retained, GitHub Issue opened
  - State written to state/context_length_state.json with full audit trail
    (sources, reasoning, confidence) per value
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

CONTEXT_STATE_FILE = Path("state/context_length_state.json")

# v3.6.0 — Opus 4.7 for bi-monthly research. Reuses the same Anthropic API key
# (ANTHROPIC_API_KEY) that fast_tier and deep_tier already use daily. Opus over
# Sonnet because this is open-ended triangulation where deeper reasoning matters
# more than latency. Cost: ~$0.10/call × 6 calls × 6 runs/year = ~$3.60/year.

# Sanity-check thresholds
MAX_DELTA_FRACTION_CONTEXT = 0.50    # typical_context_tokens: reject >50% change
MAX_DELTA_PP_SHARE = 15              # volume_share_pct: reject >15pp per category
MIN_PLAUSIBLE_CONTEXT = 1_000         # 1K tokens
MAX_PLAUSIBLE_CONTEXT = 10_000_000    # 10M tokens
ACCEPTED_CONFIDENCE = {"high", "medium"}   # "low" confidence rejected

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
        "Specialized coding agent products that load codebases — "
        "Claude Code, Cursor, GitHub Copilot Workspace, Devin (Cognition), "
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


# ── Prompts ───────────────────────────────────────────────────────────────────

def _build_typical_context_prompt(category: str) -> str:
    """Build the research prompt for one category's typical_context_tokens."""
    return f"""You are researching current public data for an AI investment framework. \
Today's date is {date.today().isoformat()}.

YOUR TASK: determine `typical_context_tokens` for the workload category "{category}".

CATEGORY DEFINITION: {CATEGORY_DESCRIPTIONS[category]}

WHAT "typical_context_tokens" MEANS: the weighted average context length (in tokens) \
that an actual session/request in this category uses on average. This is NOT the \
maximum context window the products support — it's the average actually filled by \
typical user behavior. Examples to calibrate intuition:
  - If ChatGPT supports 128K context but most users have short Q&A turns averaging \
12K tokens per session, the "typical" value is ~12K, not 128K
  - If Claude Code loads codebases averaging 600K tokens per session, the typical \
value is ~600K
  - If enterprise RAG apps routinely fill 1M context with retrieved documents, the \
typical value is ~1M

RESEARCH APPROACH:
1. Use web search to find current data (prefer sources from last 90 days, max 180 days)
2. Identify the major products in this category and their CURRENT market share weights
3. For each major product, find its published context window AND any data on actual \
usage patterns (vendor blog posts, case studies, analyst reports)
4. Compute a market-share-weighted average
5. Triangulate across at least 3 independent sources

PRIORITIZE SOURCES FROM:
- Vendor product pages and API docs (for max context specs)
- Reuters, Bloomberg, The Information, Time Magazine (for revenue / ARR figures)
- SimilarWeb, Sacra, Cloudflare Radar (for market share / web traffic)
- Earnings call transcripts and investor disclosures

OUTPUT FORMAT: Return ONLY a JSON object (no markdown fences, no other text):
{{
    "value": <integer number of tokens>,
    "confidence": "high" | "medium" | "low",
    "reasoning": "<one paragraph explaining your triangulation>",
    "providers_considered": [
        {{
            "name": "<product name>",
            "share_weight": <0.0 to 1.0>,
            "max_context": <int tokens>,
            "typical_usage_fraction": <0.0 to 1.0>,
            "implied_typical_tokens": <int>
        }}
    ],
    "weighted_calculation": "<algebra string showing how the value was computed>",
    "source_urls": ["<url1>", "<url2>", "<url3>", ...],
    "as_of_date": "<YYYY-MM-DD>"
}}

CRITICAL: provider share weights should sum to ~1.0. Show your math in \
weighted_calculation.
"""


def _build_volume_share_prompt() -> str:
    """Build the volume share prompt — one call for all 5 categories."""
    cats_text = "\n".join(f"  - {c}: {CATEGORY_DESCRIPTIONS[c]}" for c in CATEGORIES)
    return f"""You are researching current public data for an AI investment framework. \
Today's date is {date.today().isoformat()}.

YOUR TASK: determine the `volume_share_pct` distribution across 5 workload \
categories. The five shares MUST sum to exactly 100.

THE FIVE CATEGORIES:
{cats_text}

WHAT "volume_share_pct" MEANS: of all inference TOKENS served globally by AI \
systems, what percentage flows through each category? This is TOKEN volume, NOT \
user count and NOT request count. The distinction matters enormously:
  - Consumer chat has hundreds of millions of users but flat-fee subscriptions \
cap per-user token consumption
  - Enterprise has fewer users but pay-per-token economics and 100K-1M token \
prompts mean each enterprise user generates 100-1000x more tokens than a consumer

RESEARCH APPROACH:
1. Find current revenue mix disclosures for major AI providers (Anthropic, OpenAI, \
Google, Microsoft). What fraction of each company's revenue is consumer vs enterprise vs API?
2. Find product-specific ARR for coding agents (Claude Code ARR, Cursor ARR, \
Copilot ARR, Devin ARR)
3. Find web traffic share data for consumer products (SimilarWeb monthly reports, \
Cloudflare Radar, First Page Sage)
4. Convert pay-per-token revenue to estimated token volume using current API \
pricing (~$5-10 per 1M tokens average blended)
5. Estimate consumer token volume from disclosed WAU × prompts/week × tokens/prompt
6. Sanity check: total enterprise API revenue × inverse-of-avg-price should give \
roughly the total enterprise token volume

OUTPUT FORMAT: Return ONLY a JSON object (no markdown fences, no other text):
{{
    "shares": {{
        "consumer_chat": <int 0-100>,
        "coding_agents": <int 0-100>,
        "enterprise_vertical_agents": <int 0-100>,
        "developer_raw_api": <int 0-100>,
        "other_creative_rag": <int 0-100>
    }},
    "confidence": "high" | "medium" | "low",
    "reasoning": "<paragraph explaining triangulation methodology>",
    "category_rationale": {{
        "consumer_chat": "<reasoning for this category's share>",
        "coding_agents": "<reasoning>",
        "enterprise_vertical_agents": "<reasoning>",
        "developer_raw_api": "<reasoning>",
        "other_creative_rag": "<reasoning>"
    }},
    "calculation_steps": "<show your math: revenue figures, token-volume conversions, etc.>",
    "source_urls": ["<url1>", "<url2>", ...],
    "as_of_date": "<YYYY-MM-DD>"
}}

CRITICAL: the five share values MUST sum to exactly 100. Show your math in \
calculation_steps.
"""


# ── LLM call + JSON parsing ───────────────────────────────────────────────────

def _strip_markdown_fences(text: str) -> str:
    """Remove ```json...``` or ```...``` wrappers if present."""
    text = text.strip()
    # Handle ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        # Strip opening fence (with optional language hint)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _call_claude_with_search(prompt: str, max_tokens: int = 4096) -> dict:
    """Call Claude with web_search tool enabled. Return parsed JSON response.

    Reuses the same Anthropic API setup that fast_tier / deep_tier use daily —
    just the secret ANTHROPIC_API_KEY which is already configured in repo secrets.

    Raises:
        ImportError: if `anthropic` package is not installed
        RuntimeError: if ANTHROPIC_API_KEY not set
        json.JSONDecodeError: if response can't be parsed as JSON
        anthropic.APIError: if the API call fails
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "The 'anthropic' package is required. Already in requirements.txt."
        )

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "This is the same key fast_tier and deep_tier use."
        )

    # Import lazily so tests don't require config.py side-effects
    from src.config import OPUS_MODEL

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search"
        }]
    )

    # Concatenate text blocks (the model may have interleaved tool_use blocks
    # which Anthropic's API handles server-side; we only see text + tool_use
    # results in the final response content)
    final_text_parts = []
    for block in response.content:
        if hasattr(block, "text") and block.text:
            final_text_parts.append(block.text)
    final_text = "".join(final_text_parts)

    if not final_text.strip():
        raise ValueError("Empty response from Claude")

    cleaned = _strip_markdown_fences(final_text)
    return json.loads(cleaned)


# ── Sanity checks ─────────────────────────────────────────────────────────────

def _sanity_check_typical_context(
    value: int, prev_value: int, confidence: str, category: str,
) -> tuple[bool, str]:
    """Return (ok, reason_if_rejected) for a typical_context_tokens value."""
    if not isinstance(value, (int, float)):
        return False, f"Value is not numeric: {type(value).__name__}"
    value = int(value)
    if value < MIN_PLAUSIBLE_CONTEXT:
        return False, f"Value {value:,} below plausible floor ({MIN_PLAUSIBLE_CONTEXT:,})"
    if value > MAX_PLAUSIBLE_CONTEXT:
        return False, f"Value {value:,} above plausible ceiling ({MAX_PLAUSIBLE_CONTEXT:,})"
    if confidence not in ACCEPTED_CONFIDENCE:
        return False, f"Confidence '{confidence}' is below accepted threshold (need 'high' or 'medium')"
    if prev_value and prev_value > 0:
        delta = abs(value - prev_value) / prev_value
        if delta > MAX_DELTA_FRACTION_CONTEXT:
            return False, (
                f"Delta {delta * 100:.0f}% from previous ({prev_value:,} → {value:,}) "
                f"exceeds {MAX_DELTA_FRACTION_CONTEXT * 100:.0f}% threshold"
            )
    return True, ""


def _sanity_check_shares(
    shares: dict, prev_shares: dict, confidence: str,
) -> tuple[bool, str]:
    """Return (ok, reason_if_rejected) for a volume_share_pct distribution."""
    # All categories present
    for cat in CATEGORIES:
        if cat not in shares:
            return False, f"Missing category in shares: {cat}"

    # Coerce to float
    try:
        shares_f = {cat: float(shares[cat]) for cat in CATEGORIES}
    except (TypeError, ValueError) as e:
        return False, f"Non-numeric share value: {e}"

    # Sum to 100 ±1
    total = sum(shares_f.values())
    if abs(total - 100) > 1:
        return False, f"Shares sum to {total:.1f} (must be 100 ±1)"

    # Per-category bounds
    for cat, share in shares_f.items():
        if share < 0:
            return False, f"Share for {cat} is negative ({share})"
        if share > 90:
            return False, f"Share for {cat} is implausibly large ({share}%)"

    # Confidence threshold
    if confidence not in ACCEPTED_CONFIDENCE:
        return False, f"Confidence '{confidence}' below accepted threshold"

    # Per-category delta from previous
    if prev_shares:
        for cat in CATEGORIES:
            prev = float(prev_shares.get(cat, 0))
            cur = shares_f[cat]
            if abs(cur - prev) > MAX_DELTA_PP_SHARE:
                return False, (
                    f"Delta for {cat} ({prev:.0f}pp → {cur:.0f}pp = "
                    f"{abs(cur - prev):.0f}pp) exceeds {MAX_DELTA_PP_SHARE}pp threshold"
                )

    return True, ""


# ── Main entry point ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load current state. Bootstrap with the v3.5.4 manual calibration baseline
    if no state file exists (matches the values in TRIANGULATION_METHODOLOGY.md)."""
    if CONTEXT_STATE_FILE.exists():
        return json.loads(CONTEXT_STATE_FILE.read_text())
    return {
        "_metadata": {
            "schema_version": "1.0",
            "bootstrapped_on": date.today().isoformat(),
            "note": "Bootstrapped from v3.5.4 manual calibration baseline"
        },
        "typical_context_tokens": {
            "consumer_chat":              {"current": 20_000,    "history": []},
            "coding_agents":              {"current": 500_000,   "history": []},
            "enterprise_vertical_agents": {"current": 1_000_000, "history": []},
            "developer_raw_api":          {"current": 50_000,    "history": []},
            "other_creative_rag":         {"current": 50_000,    "history": []},
        },
        "volume_shares": {
            "committed": {
                "consumer_chat": 30, "coding_agents": 18,
                "enterprise_vertical_agents": 30,
                "developer_raw_api": 17, "other_creative_rag": 5,
            },
            "proposed": None,
            "proposal_evidence": [],
            "proposal_status": "none",
            "proposal_created": None,
            "history": [],
        },
        "operative_context_history": [],
    }


def _save_state(state: dict) -> None:
    """Persist state atomically (tmp + rename)."""
    CONTEXT_STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = CONTEXT_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(CONTEXT_STATE_FILE)


def research_context_length_inputs() -> dict:
    """Re-research all context length inputs from current public sources.

    Returns a summary dict for logging / diagnostics.
    """
    state = _load_state()
    today = date.today().isoformat()
    state.setdefault("_metadata", {})

    updated: list[str] = []
    rejected: list[dict] = []
    errors: list[str] = []

    # ── 1. Research typical_context_tokens per category ───────────────────────
    for category in CATEGORIES:
        logger.info(f"Researching typical_context_tokens for {category}...")
        try:
            prev_value = int(state["typical_context_tokens"][category].get("current") or 0)
            result = _call_claude_with_search(_build_typical_context_prompt(category))

            new_value = int(result["value"])
            confidence = str(result.get("confidence", "low")).lower()

            ok, reason = _sanity_check_typical_context(
                new_value, prev_value, confidence, category
            )

            if not ok:
                rejected.append({
                    "field": "typical_context_tokens",
                    "category": category,
                    "proposed_value": new_value,
                    "previous_value": prev_value,
                    "rejection_reason": reason,
                    "llm_confidence": confidence,
                    "llm_reasoning": result.get("reasoning", "")[:500],
                })
                logger.warning(
                    f"REJECTED typical_context_tokens[{category}]: {reason}"
                )
                continue

            # Accept
            state["typical_context_tokens"][category]["current"] = new_value
            state["typical_context_tokens"][category].setdefault("history", []).append({
                "date": today,
                "value": new_value,
                "source": "llm_research_v3_6_0",
                "confidence": confidence,
                "reasoning": result.get("reasoning", ""),
                "providers_considered": result.get("providers_considered", []),
                "weighted_calculation": result.get("weighted_calculation", ""),
                "source_urls": result.get("source_urls", []),
                "previous_value": prev_value,
                "delta_pct": (
                    round((new_value - prev_value) / prev_value * 100, 1)
                    if prev_value else 0
                ),
            })
            updated.append(f"typical_context[{category}]: {prev_value:,} → {new_value:,}")
            logger.info(
                f"UPDATED typical_context_tokens[{category}]: "
                f"{prev_value:,} → {new_value:,} (confidence={confidence})"
            )

        except json.JSONDecodeError as e:
            errors.append(f"typical_context[{category}]: JSON parse error: {e}")
            logger.error(f"JSON parse failed for {category}: {e}")
        except Exception as e:
            errors.append(f"typical_context[{category}]: {type(e).__name__}: {e}")
            logger.error(f"Error researching {category}: {e}")

    # ── 2. Research volume_share_pct (single call for all 5) ──────────────────
    logger.info("Researching volume_share_pct across all categories...")
    try:
        prev_shares = dict(state["volume_shares"]["committed"])
        result = _call_claude_with_search(_build_volume_share_prompt())

        new_shares = result["shares"]
        confidence = str(result.get("confidence", "low")).lower()

        ok, reason = _sanity_check_shares(new_shares, prev_shares, confidence)

        if not ok:
            rejected.append({
                "field": "volume_share_pct",
                "proposed_value": new_shares,
                "previous_value": prev_shares,
                "rejection_reason": reason,
                "llm_confidence": confidence,
                "llm_reasoning": result.get("reasoning", "")[:500],
            })
            logger.warning(f"REJECTED volume_share_pct: {reason}")
        else:
            # Coerce to int after sanity check
            new_shares_int = {cat: int(round(float(new_shares[cat]))) for cat in CATEGORIES}
            # Normalize to exact 100 (drift from rounding)
            drift = 100 - sum(new_shares_int.values())
            if drift != 0:
                # Adjust the largest share to absorb drift
                largest_cat = max(new_shares_int, key=new_shares_int.get)
                new_shares_int[largest_cat] += drift

            state["volume_shares"]["committed"] = new_shares_int
            state["volume_shares"]["history"].append({
                "date": today,
                "shares": new_shares_int,
                "trigger": "llm_research_v3_6_0",
                "confidence": confidence,
                "reasoning": result.get("reasoning", ""),
                "category_rationale": result.get("category_rationale", {}),
                "calculation_steps": result.get("calculation_steps", ""),
                "source_urls": result.get("source_urls", []),
                "previous_shares": prev_shares,
            })
            state["volume_shares"]["proposed"] = None
            state["volume_shares"]["proposal_status"] = "none"
            updated.append(f"volume_shares: {prev_shares} → {new_shares_int}")
            logger.info(f"UPDATED volume_share_pct: {new_shares_int} (confidence={confidence})")

    except json.JSONDecodeError as e:
        errors.append(f"volume_shares: JSON parse error: {e}")
        logger.error(f"JSON parse failed for volume_shares: {e}")
    except Exception as e:
        errors.append(f"volume_shares: {type(e).__name__}: {e}")
        logger.error(f"Error researching volume_shares: {e}")

    # ── 3. Persist + summary ──────────────────────────────────────────────────
    state["_metadata"]["last_llm_research"] = datetime.now(timezone.utc).isoformat()
    state["_metadata"]["last_run_summary"] = {
        "updated_count": len(updated),
        "rejected_count": len(rejected),
        "error_count": len(errors),
    }

    _save_state(state)

    summary = {
        "status": "completed" if not errors else "completed_with_errors",
        "updated_count": len(updated),
        "rejected_count": len(rejected),
        "error_count": len(errors),
        "updated": updated,
        "rejected": rejected,
        "errors": errors,
    }
    logger.info(
        f"LLM research complete: {summary['updated_count']} updated, "
        f"{summary['rejected_count']} rejected, {summary['error_count']} errors"
    )

    # Exit non-zero if any errors or rejections, so the workflow can react
    if errors or rejected:
        summary["exit_code"] = 1
    else:
        summary["exit_code"] = 0
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
