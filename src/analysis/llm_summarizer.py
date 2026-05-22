"""
Two-stage LLM analysis pipeline.

Stage 1 (Haiku): scrub all news, categorize relevance to indicators,
extract analyst broker actions (upgrade/downgrade/target change),
identify TrendForce price commentary, summarize key items.

Stage 2 (Sonnet): take Haiku's structured output + price/estimate data
+ prior assessments, produce tier-by-tier classification, executive
summary with probability calculations, and cycle position synthesis.

Falls back to deterministic synthesis if API unavailable.

CHANGELOG (this file):
  v3.39.7.3 (2026-05-20) — SYMMETRIC PROBABILITY RULES added to the Sonnet prompt. v3.39.7's PROBABILITY UPDATE RULES only covered "indicator fires today" (rule A); there was no rule for "indicator unfires today" (rule B). Result on the May 20 brief: 4 indicators went FIRED→Not fired, 1 went FIRED→Partially fired, 3 went Partially fired→Not fired — but bull/base/bear probabilities stayed byte-identical to May 18 (22%/50%/28%). The LLM literally wrote "no probability distribution changes warranted as no new tier signals fired", ignoring 8 unfires. Now adds: (B) explicit unfire rules — Tier 1 unfire = bear -2-4pp + bull +1-2pp + base peak extends 1mo; Tier 2 = bear -3-6pp + bull +2-4pp + 1-2mo; Tier 3 = bear -5-8pp + bull +3-5pp + 2-3mo; Tier 4 = bear -5-8pp + bull +5-8pp + 2-3mo. (C) MANDATORY RECALCULATION: probabilities stay byte-identical to prior only when EVERY indicator's status is unchanged — any status change in either direction requires recalc. (D) COMPUTED-METRIC OVERRIDE: when forward_pe / rel_strength_full / capex_revenue_ratios / eps_revisions show fired but Layer 2 returns Not fired with evidence_count=0, Sonnet must use the computed verdict, not Layer 2 — fixes the Forward P/E contradiction from the May 20 brief where all 3 names were below their thresholds per computed data but Layer 2 said Not fired.
  v3.39.7 (2026-05-20) — Cycle matrix Haiku extraction layer appended at bottom. Two new prompt+function pairs: extract_dram_signal_from_article (tone/segment/supplier/qoq structured fields → data/dram_contract_signals.csv) and extract_dram_forecasts_from_press_release (quarterly forecast list → data/dram_contract_quarterly_forecasts.csv). Both use HAIKU_MODEL with temperature=0. Pre-filtered by title in fast_tier.py to ~10 DRAM-relevant articles/run; ~$0.50/run cost ceiling. Allowed-value sets prevent free-text drift. Output validated and coerced into the CSV schemas. Reuses the existing Anthropic client pattern from this module.
  v3.38.5 (2026-05-17) — Real root cause of the 16:25 UTC May 17 Haiku triage failure found and fixed. The error pattern was "Failed to parse Haiku response as JSON: Expecting value (char 0)" → json_repair fallback returned str (not dict) → unrecoverable. This was NOT a prompt issue or a max_tokens issue. The bug was in `_strip_code_fence`: when the LLM returned a properly closed code fence (```json\n{...}\n```), the line `text.split("```", 2)[-1]` grabbed the empty string AFTER the closing fence instead of the JSON content BETWEEN the fences. The function returned `""`. json.loads("") fails with "Expecting value (char 0)"; json_repair.loads("") returns `""` (a str). Rewrote the function with explicit handling of three patterns (raw / closed-fence / unclosed-fence). Verified locally on all five common LLM-output shapes — all parse cleanly via stdlib now. Also tightened `build_haiku_triage_prompt` output format requirements (start with `{`, no markdown fences, no prose, no apologies) as belt-and-suspenders. The previous bump of HAIKU_MAX_TOKENS to 16000 and the json_repair fallback both remain — they fix related but distinct failure modes (truncation, unescaped chars).
  v3.38.4 (2026-05-17) — Both `haiku_triage_news` and `sonnet_analyze` now parse responses with a two-stage strategy: stdlib `json.loads(raw)` first (fast path for valid output), then `json_repair.loads(raw)` fallback on JSONDecodeError. Reason: the 15:32 UTC May 17 run had Haiku triage of 50 news items fail JSON parse at char 13926 (unterminated string — almost certainly unescaped quote/newline in a reasoning field), losing the entire triage. v3.38.3 had the same failure mode in Sonnet (char 27099) and Haiku (char 6839). Bumping max_tokens helps when the cause is truncation, but doesn't help when the cause is malformed string content — json_repair handles both. New explicit log at WARNING level when the fallback fires, with INFO-level confirmation of how much was recovered.
"""

from __future__ import annotations

import json
import logging
import math  # v3.39.7.9 — defensive NaN guard in COMPUTED METRICS rendering
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import json_repair  # v3.38.4 — fallback when stdlib json.loads fails on LLM output

from anthropic import Anthropic

from src.config import (
    HAIKU_MODEL,
    HAIKU_MAX_TOKENS,
    SONNET_MODEL,
    SONNET_MAX_TOKENS,
    LLM_TEMPERATURE,
    INDICATORS,
    TOP_5_INDICATORS,
    CYCLE_BASELINE,
)
from src.data.news import NewsItem
from src.data.estimates import EstimateSnapshot
from src.data.prices import PerformanceMetrics
from src.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


# ============================================================
# Output dataclasses
# ============================================================

@dataclass
class IndicatorAssessment:
    """LLM-generated assessment of a single indicator's current status."""
    id: str
    tier: int
    name: str
    description: str
    what_to_look_for: str
    current_status: str       # "Fired" | "Partially fired" | "Not fired"  (v3.39.7.9: "Reversed" removed — audit trail in change_vs_prior)
    where_we_stand: str
    latest_developments: str
    relevant_links: list[dict[str, str]] = field(default_factory=list)
    change_vs_prior: Optional[str] = None


@dataclass
class CaseScenario:
    """Probability of a length-to-peak + price-to-peak scenario for SK Hynix.

    v3.39: renamed `upside_to_peak_pct` → `return_to_peak_pct`. The previous
    name implied a non-negative number, but the bear case legitimately needs
    to express a NEGATIVE value (peak already in → downside from current
    price). Now signed: positive = upside, zero = at peak, negative = downside.

    Framing:
      base — most probable scenario by invariant; signals continuing at
             current pace; peak shortens and return trims as signals fire
      bull — requires signals to un-fire or delay materially; longer peak,
             higher return (always positive)
      bear — requires faster degradation than expected; shorter peak (can
             be 0), can carry a NEGATIVE return when peak is already in
    """
    probability_pct: float
    peak_months_away: float
    peak_window_start: str  # e.g., "August 2026"
    peak_window_end: str
    return_to_peak_pct: float  # signed


@dataclass
class ExecutiveSummary:
    """The structured exec summary at the top of the email.

    v3.39.7.5 — `executive_brief` field added: a single consolidated
    5-8 sentence paragraph synthesizing today's signal changes, big
    news, big movers, and macro context. Replaces the three-block
    pattern (delta_from_prior_text / big_movers list / market_summary)
    that the May 20 brief surfaced as repetitive. Anchor links of the
    form `[→§N]` are emitted by Sonnet and substituted to HTML
    `<a href="#section-N">§N</a>` at template render time.

    The legacy fields (`delta_from_prior_text`, `urgent_attention_*`,
    and `market_summary` on BriefAnalysis) are KEPT but no longer
    rendered. Sonnet still populates them as JSON output for any
    future debugging/observability use; they're dead-letter fields
    from the email's perspective.
    """
    current_phase: int
    current_phase_label: str
    tier_status_summary: str
    new_triggers_today: list[dict[str, str]]
    base_case: CaseScenario
    bull_case: CaseScenario
    bear_case: CaseScenario
    delta_from_prior_text: str
    urgent_attention_needed: bool
    urgent_attention_text: str
    executive_brief: str = ""  # v3.39.7.5 — populated by Sonnet, rendered as the consolidated exec block


# ============================================================
# v3.39 — Scenario invariant enforcement
# ============================================================
# The Sonnet prompt asks for three scenarios (base / bull / bear) with
# probability_pct summing to 100 and base being the most probable. The LLM
# usually obeys but can drift on either dimension — e.g., the 17:43 May 17
# brief produced base 28% / bear 30% / bull 18% (sum 76%, bear > base). The
# enforcer below normalizes deterministically: probabilities sum to exactly
# 100, base is strictly the highest, return_to_peak_pct signs make sense.


def _tier_from_id(indicator_id: str) -> int:
    """Infer tier (1-4) from an indicator id of the form `tN_…`.

    v3.39.2: when Sonnet truncates mid-indicator and json_repair recovers
    a partial dict missing the `tier` field, defaulting tier to 0 caused
    the indicator to be silently dropped from the email — the template
    iterates `for tier in (1, 2, 3, 4)` and filters strictly. Since every
    indicator id is canonically prefixed `t1_…` through `t4_…`, we can
    almost always recover the tier from the id even when the explicit
    field is missing.

    Returns 0 only if the id doesn't conform to the prefix scheme; the
    caller defaults to that value (which still renders as a no-tier row
    visible nowhere, matching the prior behavior on truly malformed ids).
    """
    if (
        isinstance(indicator_id, str)
        and len(indicator_id) >= 2
        and indicator_id.startswith("t")
        and indicator_id[1].isdigit()
    ):
        n = int(indicator_id[1])
        if 1 <= n <= 4:
            return n
    return 0


def _compute_tier_status_summary(assessments: list) -> str:
    """Deterministically compute the `tier_status_summary` string from the
    parsed `indicator_assessments` list.

    v3.39.7.4 — Sonnet's self-counted `tier_status_summary` in the exec
    summary has historically diverged from the per-card statuses in the
    body of the same brief (e.g. May 20 2026 16:00 UTC brief: body showed
    4 Tier 1 indicators with `Fired` badges, but exec summary said
    `T1: 3/6 fired`; T2 had the symmetric problem where a `Partially fired`
    body card was counted as fired in the exec total `T2: 6/7`). Off-by-
    one arithmetic errors in the LLM's prose are not fixable by prompt
    tightening alone, so this helper recomputes the count post-parse from
    the structured assessments and overrides Sonnet's string.

    Counting convention (locked in 2026-05-20 with user):
    - `Fired` and `Partially fired` both count toward the tier total.
      Rationale: a Partially fired indicator has met the trigger criteria
      on at least one of multiple stitched dimensions, which is
      directionally the same as Fired. Treating partial as fired makes
      the tier count monotonic in "progress toward firing".
    - When ANY tier has Partially fired indicators (>0), append a
      "(N partial)" annotation so the reader knows the full-fire breakdown.
    - The denominator per tier is fixed: T1=6, T2=7, T3=5, T4=4.

    Output format examples:
      "T1: 3/6 fired | T2: 5/7 fired (1 partial) | T3: 0/5 fired | T4: 0/4 fired"
      "T1: 0/6 fired | T2: 0/7 fired | T3: 0/5 fired | T4: 0/4 fired"
    """
    TIER_DENOMINATORS = {1: 6, 2: 7, 3: 5, 4: 4}
    counts = {1: [0, 0], 2: [0, 0], 3: [0, 0], 4: [0, 0]}  # [fired_total, partial_subset]
    for a in assessments:
        tier = getattr(a, "tier", 0) or _tier_from_id(getattr(a, "id", ""))
        if tier not in TIER_DENOMINATORS:
            continue
        status = (getattr(a, "current_status", "") or "").strip().lower()
        if status == "fired":
            counts[tier][0] += 1
        elif status.startswith("partial"):
            counts[tier][0] += 1
            counts[tier][1] += 1
    parts = []
    for tier in (1, 2, 3, 4):
        fired, partial = counts[tier]
        denom = TIER_DENOMINATORS[tier]
        seg = f"T{tier}: {fired}/{denom} fired"
        if partial > 0:
            seg += f" ({partial} partial)"
        parts.append(seg)
    return " | ".join(parts)


def _safe_indicator_assessment(a: dict) -> IndicatorAssessment:
    """Construct an IndicatorAssessment from a possibly-partial dict.

    Used after Sonnet output is parsed (or json_repair-recovered). When
    truncation hits mid-indicator, json_repair returns whatever it could
    salvage, often missing required fields like `latest_developments`.
    Prior code's `IndicatorAssessment(**a)` raised TypeError on missing
    fields and crashed the whole pipeline — see v3.39.1 fix notes.

    Every required field gets a safe default; `change_vs_prior` stays None
    if absent. The caller filters out dicts without an `id` upstream.

    v3.39.2: tier now falls back to `_tier_from_id(id)` before defaulting
    to 0. Previously a missing `tier` field meant the indicator was
    constructed cleanly but then filtered out by the template's
    `for tier in (1,2,3,4)` loop — defensive parsing produced a usable
    object that the renderer silently dropped. Inferring from the id
    prefix recovers the right tier in every realistic truncation case.
    """
    indicator_id = str(a.get("id", "unknown"))
    raw_tier = str(a.get("tier", "")).strip()
    if raw_tier.isdigit():
        tier = int(raw_tier)
    else:
        tier = _tier_from_id(indicator_id)
    return IndicatorAssessment(
        id=indicator_id,
        tier=tier,
        name=str(a.get("name", "")),
        description=str(a.get("description", "")),
        what_to_look_for=str(a.get("what_to_look_for", "")),
        current_status=str(a.get("current_status", "Unknown (Sonnet output truncated)")),
        where_we_stand=str(a.get("where_we_stand", "")),
        latest_developments=str(a.get("latest_developments", "")),
        relevant_links=a.get("relevant_links", []) if isinstance(a.get("relevant_links"), list) else [],
        change_vs_prior=a.get("change_vs_prior") if a.get("change_vs_prior") else None,
    )


def _build_case_scenario(d: dict | None) -> CaseScenario:
    """Construct a CaseScenario from a dict, handling both the new field
    name (return_to_peak_pct) and the pre-v3.39 name (upside_to_peak_pct).
    Used when loading prior_state.json which may have older format, and
    when the LLM occasionally regresses to the older key name.
    """
    d = dict(d or {})
    if "return_to_peak_pct" not in d and "upside_to_peak_pct" in d:
        d["return_to_peak_pct"] = d.pop("upside_to_peak_pct")
    return CaseScenario(
        probability_pct=float(d.get("probability_pct", 0)),
        peak_months_away=float(d.get("peak_months_away", 0)),
        peak_window_start=str(d.get("peak_window_start", "")),
        peak_window_end=str(d.get("peak_window_end", "")),
        return_to_peak_pct=float(d.get("return_to_peak_pct", 0)),
    )


def _enforce_scenario_invariants(
    base: CaseScenario, bull: CaseScenario, bear: CaseScenario
) -> tuple[CaseScenario, CaseScenario, CaseScenario]:
    """Apply hard invariants to the three scenarios:
      1. Probabilities are integers in [1, 98] summing to exactly 100.
      2. base.probability_pct is the strict maximum (> bear, > bull).
      3. base.return_to_peak_pct >= 0 (base never assumes peak is past).
      4. bull.return_to_peak_pct > base.return_to_peak_pct.
      5. bear.return_to_peak_pct <= base.return_to_peak_pct.

    Algorithm (probabilities):
      a. Floor each at 1, then proportionally normalize sum to 100.
      b. For any scenario whose probability >= base, cap it at base-1 and
         redistribute the excess back into base (preserves sum=100, makes
         base strictly the largest).
      c. Round to integers; if rounding misses 100, adjust base for the
         residual. Final loop guards against rounding flipping the inequality.
    """
    # --- Probability invariants ---
    bp = max(1.0, float(base.probability_pct))
    blp = max(1.0, float(bull.probability_pct))
    brp = max(1.0, float(bear.probability_pct))

    # Step a: proportional normalize to sum=100
    total = bp + blp + brp
    bp = bp * 100.0 / total
    blp = blp * 100.0 / total
    brp = brp * 100.0 / total

    # Step b: cap bull/bear at base-1; redistribute excess into base
    if blp >= bp:
        excess = blp - (bp - 1.0)
        blp = bp - 1.0
        bp += excess
    if brp >= bp:
        excess = brp - (bp - 1.0)
        brp = bp - 1.0
        bp += excess
    # After redistribution, re-floor in case capping pushed below 1
    blp = max(1.0, blp)
    brp = max(1.0, brp)
    # Renormalize if floors changed the sum (very rare edge case)
    total = bp + blp + brp
    if total != 100.0:
        # Absorb residual into base (which is now strictly largest, so
        # this won't break the strict-max invariant)
        bp = 100.0 - blp - brp

    # Step c: round to integers, fix residual
    b_i = int(round(bp))
    bl_i = int(round(blp))
    br_i = int(round(brp))
    diff = 100 - (b_i + bl_i + br_i)
    b_i += diff
    # Final pp-by-pp loop in case rounding flipped strict inequalities
    while b_i <= bl_i:
        b_i += 1
        bl_i -= 1
    while b_i <= br_i:
        b_i += 1
        br_i -= 1
    while bl_i < 1:
        bl_i += 1
        b_i -= 1
    while br_i < 1:
        br_i += 1
        b_i -= 1

    # --- Return-to-peak invariants ---
    base_r = float(base.return_to_peak_pct)
    bull_r = float(bull.return_to_peak_pct)
    bear_r = float(bear.return_to_peak_pct)
    # Base never assumes peak is past
    base_r = max(0.0, base_r)
    # Bull strictly above base (always positive upside)
    if bull_r <= base_r:
        bull_r = base_r + 5.0
    # Bear no higher than base (can be negative when peak is in)
    if bear_r > base_r:
        bear_r = base_r

    return (
        CaseScenario(
            probability_pct=float(b_i),
            peak_months_away=base.peak_months_away,
            peak_window_start=base.peak_window_start,
            peak_window_end=base.peak_window_end,
            return_to_peak_pct=base_r,
        ),
        CaseScenario(
            probability_pct=float(bl_i),
            peak_months_away=bull.peak_months_away,
            peak_window_start=bull.peak_window_start,
            peak_window_end=bull.peak_window_end,
            return_to_peak_pct=bull_r,
        ),
        CaseScenario(
            probability_pct=float(br_i),
            peak_months_away=bear.peak_months_away,
            peak_window_start=bear.peak_window_start,
            peak_window_end=bear.peak_window_end,
            return_to_peak_pct=bear_r,
        ),
    )


@dataclass
class BigMover:
    """A ticker with >4% move in latest session."""
    name: str
    ticker: str
    change_1d_pct: float
    direction: str  # "up" | "down"


@dataclass
class BrokerAction:
    """A broker action extracted from news."""
    ticker: str
    broker: str
    action: str
    detail: str
    source_url: str


@dataclass
class NewsTriage:
    """Output of Haiku stage 1: structured news pre-processing."""
    relevant_news: list[dict]
    broker_actions: list[BrokerAction]
    trendforce_commentary: list[dict]
    indicator_relevance: dict[str, list[int]]


@dataclass
class BriefAnalysis:
    """Complete analytical output."""
    executive_summary: ExecutiveSummary
    big_movers: list[BigMover]
    indicator_assessments: list[IndicatorAssessment]
    top_5_deep_dives: list[IndicatorAssessment]
    market_summary: str
    key_news_summary: list[dict[str, str]]
    cycle_position_call: str


# ============================================================
# Stage 1: Haiku news pre-processing
# ============================================================

def build_haiku_triage_prompt(news_items: list[NewsItem]) -> str:
    """Prompt Haiku to triage and categorize the news feed."""

    indicator_summary = "\n".join(
        f"- [{ind.id}] T{ind.tier}: {ind.name} (looks for: {ind.what_to_look_for[:100]})"
        for ind in INDICATORS
    )

    news_text = ""
    for i, n in enumerate(news_items[:120]):
        pub = n.published.strftime("%Y-%m-%d %H:%M") if n.published else "?"
        news_text += f"[{i}] ({n.source} | {pub}) {n.title}\n"
        if n.summary:
            news_text += f"     Summary: {n.summary[:300]}\n"
        news_text += f"     URL: {n.url}\n\n"

    prompt = f"""You are pre-processing a news feed for a memory semiconductor cycle tracker. Extract structured information from these news items so a senior analyst can quickly synthesize the cycle status.

INDICATORS THE ANALYST CARES ABOUT (be specific about which apply):
{indicator_summary}

YOUR TASKS:
1. Filter: Identify the news items relevant to memory/semiconductor cycle (SK Hynix, Micron, Samsung, ASML, AMAT, NVIDIA, Broadcom, TSMC, hyperscaler capex, DRAM/NAND/HBM prices, AI inference efficiency). Discard everything else.
2. Broker actions: Extract any analyst broker actions (upgrades, downgrades, target price changes, estimate revisions). Action types: "upgrade", "downgrade", "target_raised", "target_cut", "estimate_raised", "estimate_cut".
3. TrendForce / price commentary: Extract any DRAM, NAND, HBM, or memory pricing commentary. Pull out specific price levels, percentage moves, and demand language ("buyers cautious", "price cuts not stimulating demand", etc).
4. Indicator mapping: For each remaining relevant news item, list which indicator IDs it informs.

OUTPUT FORMAT — STRICT REQUIREMENTS:
- Your entire response must be a single valid JSON object.
- Start your response with the opening `{{` character. End with the closing `}}`.
- Do NOT wrap the JSON in markdown code fences (```).
- Do NOT include any prose before or after the JSON.
- Do NOT include comments, explanations, or apologies.
- If you cannot triage some items, still return valid JSON — leave problematic items out of `relevant_news` rather than describing the problem in prose.

JSON SCHEMA:
{{
  "relevant_news": [
    {{"index": 5, "title": "...", "url": "...", "source": "...", "summary": "1-line takeaway"}}
  ],
  "broker_actions": [
    {{"ticker": "MU", "broker": "Mizuho", "action": "target_raised", "detail": "$545 -> $740", "source_url": "..."}}
  ],
  "trendforce_commentary": [
    {{"product": "DDR5 16Gb spot", "price_level": "$39", "move": "+0.34%", "language": "...", "source_url": "..."}}
  ],
  "indicator_relevance": {{
    "t1_spot_decline": [5, 12, 23],
    "t2_eps_revisions": [3, 17]
  }}
}}

Be aggressive about filtering. If an item is not directly about memory/semi cycle, discard it.

NEWS ITEMS TO TRIAGE:
{news_text}
"""
    return prompt


def haiku_triage_news(
    api_key: str,
    news_items: list[NewsItem],
    collector: Optional[DiagnosticsCollector] = None,
) -> Optional[NewsTriage]:
    """Run Haiku to pre-process the news feed."""
    if not api_key:
        if collector is not None:
            collector.record_skipped(
                f"Anthropic API / {HAIKU_MODEL} (stage 1 triage)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
        return None

    client = Anthropic(api_key=api_key)
    prompt = build_haiku_triage_prompt(news_items)

    response = None
    try:
        if collector is not None:
            with collector.track(
                f"Anthropic API / {HAIKU_MODEL} (stage 1 triage)",
                "llm",
            ) as t:
                response = client.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=HAIKU_MAX_TOKENS,
                    temperature=LLM_TEMPERATURE,
                    messages=[{"role": "user", "content": prompt}],
                )
                # items_returned = #news items the model successfully classified
                # (estimated from input volume; actual count set after parse)
                t.items_returned = len(news_items)
                t.detail = f"input: {len(news_items)} news items"
        else:
            response = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=HAIKU_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
    except Exception as e:
        logger.error(f"Haiku triage call failed: {e}")
        return None

    raw = _strip_code_fence(response.content[0].text)
    # v3.38.4: two-stage parse. Stdlib json first (fast path for valid output);
    # json_repair fallback for the cases that broke runs at char 6839, 13926,
    # 27099. json_repair recovers from unescaped quotes/newlines in string
    # fields AND from truncation (returns best-effort dict for as much as
    # parsed). If even json_repair returns empty/non-dict, log and bail.
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            f"Haiku JSON parse failed at {e.msg} (char {e.pos}); "
            f"attempting json_repair recovery..."
        )
        try:
            parsed = json_repair.loads(raw)
            if not isinstance(parsed, dict):
                logger.error(
                    f"json_repair returned {type(parsed).__name__}, not dict — "
                    f"Haiku triage unrecoverable"
                )
                return None
            n_recovered = len(parsed.get("relevant_news", []))
            logger.info(
                f"json_repair recovered Haiku triage: {n_recovered} relevant_news items"
            )
        except Exception as repair_err:
            logger.error(
                f"json_repair also failed: {repair_err}; Haiku triage unrecoverable"
            )
            return None

    return NewsTriage(
        relevant_news=parsed.get("relevant_news", []),
        broker_actions=[BrokerAction(**a) for a in parsed.get("broker_actions", [])],
        trendforce_commentary=parsed.get("trendforce_commentary", []),
        indicator_relevance=parsed.get("indicator_relevance", {}),
    )


# ============================================================
# Stage 2: Sonnet analysis + exec summary
# ============================================================

def build_sonnet_analysis_prompt(
    triage: NewsTriage,
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    big_movers: list[BigMover],
    prior_state: dict,
    digests: Optional[list[dict]] = None,
    signal_reasonings: Optional[list] = None,
    computed_metrics: Optional[dict] = None,
    news_items: Optional[list[NewsItem]] = None,
) -> str:
    """Construct the Sonnet analysis prompt using Haiku-triaged data.

    v3.39.5: New `digests` parameter. Threads Layer 1 deep-tier broker note
    and transcript digests into Sonnet's prompt as a dedicated section.
    Closes the architectural void where Mirae note bodies were written to
    state/digests/ but never reached Sonnet — Sonnet was citing Korean news
    aggregator coverage of Mirae's W3.2M raise instead of Mirae directly.

    v3.39.6: New `signal_reasonings` and `computed_metrics` parameters.
    Threads Layer 2 per-signal Haiku verdicts AND the numerical computed
    metrics (relative strength 5-day spreads, hyperscaler capex ratios,
    forward P/E, DART materials) into Sonnet's prompt. Closes the void
    where Sonnet was speculating about signals it had no numbers for —
    e.g., May 18 brief's t3_relative_strength prose hallucinated "almost
    certainly past -10% threshold" from single-day news headlines when
    the actual 5-day computed spread was below threshold. Sonnet now sees
    the verdict directly and is instructed to LEAD with the numbers and
    keep commentary to ≤1/3 of indicator text.

    v3.39.7.6: New `news_items` parameter. The Haiku triage output
    (`relevant_news`) carries source/title/url/summary but NOT the
    published timestamp — and without timestamps Sonnet cannot apply
    the RECENCY ON CONTRADICTING EVENTS rule. When `news_items` is
    passed in, we build a URL→published lookup and stamp each triage
    item with its timestamp in the prompt. The May 21 2026 brief
    surfaced this when Sonnet wrote about a Samsung strike "commencing
    May 21" as a live event despite a later Korea Herald article in
    the same feed reporting the strike was called off one hour before
    launch — Sonnet had both items but no way to tell which was later.
    """

    # v3.39.7.6 — URL → published-timestamp lookup. Joins the original
    # NewsItem objects back to Haiku's triaged subset. URL is the join
    # key (Haiku doesn't mangle URLs in its output schema). Falls back
    # to title-match for items where URL is missing or differs.
    published_by_url: dict[str, str] = {}
    published_by_title: dict[str, str] = {}
    if news_items:
        for n in news_items:
            if n.published:
                ts = n.published.strftime("%Y-%m-%d %H:%M UTC")
                if n.url:
                    published_by_url[n.url] = ts
                if n.title:
                    published_by_title[n.title] = ts

    perf_text = "PERFORMANCE DATA (Yahoo Finance):\n"
    for p in performance_data:
        perf_text += (
            f"- {p.name} ({p.ticker}): {p.current_price:.2f} {p.currency} | "
            f"1D {_fmt_pct(p.change_1d)} | 1W {_fmt_pct(p.change_1w)} | "
            f"1M {_fmt_pct(p.change_1m)} | 3M {_fmt_pct(p.change_3m)} | "
            f"6M {_fmt_pct(p.change_6m)} | 1Y {_fmt_pct(p.change_1y)}\n"
        )

    est_text = "\nANALYST ESTIMATES (FY+1 = ~FY27, FY+2 = ~FY28):\n"
    for name, e in estimates_data.items():
        est_text += (
            f"- {name} ({e.ticker}): "
            f"FY+1 EPS est: {e.fy_next_eps_estimate} | "
            f"FY+2 EPS est: {e.fy_next2_eps_estimate} | "
            f"# analysts: {e.num_analysts} | "
            f"Fwd P/E: {e.forward_pe} | "
            f"Avg target: {e.avg_target_price}"
        )
        if e.upgrades_30d is not None or e.downgrades_30d is not None:
            est_text += f" | Up30d: {e.upgrades_30d} | Down30d: {e.downgrades_30d}"
        est_text += "\n"

    triage_text = f"\nNEWS TRIAGE OUTPUT (pre-processed by Haiku):\n"
    triage_text += f"\nRelevant news items ({len(triage.relevant_news)}):\n"
    triage_text += (
        "Each item shows its PUBLISHED TIMESTAMP. When two items reference the\n"
        "same event with contradictory direction, the LATER timestamp is the\n"
        "operative state of the world per the RECENCY ON CONTRADICTING EVENTS\n"
        "rule above.\n"
    )
    for item in triage.relevant_news[:50]:
        url = item.get("url", "")
        title = item.get("title", "")
        # v3.39.7.6 — published timestamp lookup. URL match first (stable
        # join key); title match second; "?" if both miss.
        pub_str = (
            published_by_url.get(url)
            or published_by_title.get(title)
            or "?"
        )
        triage_text += f"- [{item.get('source', '?')} | published {pub_str}] {title}\n"
        triage_text += f"  Takeaway: {item.get('summary', '')}\n"
        triage_text += f"  URL: {url}\n"

    triage_text += f"\nBroker actions detected ({len(triage.broker_actions)}):\n"
    for ba in triage.broker_actions[:30]:
        triage_text += f"- {ba.broker} | {ba.ticker} | {ba.action} | {ba.detail}\n"

    triage_text += f"\nTrendForce / price commentary ({len(triage.trendforce_commentary)}):\n"
    for tc in triage.trendforce_commentary[:20]:
        triage_text += f"- {tc.get('product', '')}: {tc.get('price_level', '')} ({tc.get('move', '')}) - {tc.get('language', '')}\n"

    triage_text += "\nIndicator relevance map:\n"
    for ind_id, indices in triage.indicator_relevance.items():
        triage_text += f"- {ind_id}: news indices {indices}\n"

    movers_text = "\nBIG MOVERS today (>+/-4% in 1 session):\n"
    if big_movers:
        for m in big_movers:
            movers_text += f"- {m.name} ({m.ticker}): {m.change_1d_pct:+.2f}%\n"
    else:
        movers_text += "(none)\n"

    indicator_text = "\nINDICATOR FRAMEWORK (assess each):\n"
    for ind in INDICATORS:
        indicator_text += (
            f"\n[{ind.id}] Tier {ind.tier}: {ind.name}\n"
            f"  Description: {ind.description}\n"
            f"  Trigger: {ind.what_to_look_for}\n"
        )

    prior_text = "\nPRIOR STATE (yesterday's brief - identify changes):\n"
    if prior_state:
        prior_text += json.dumps(prior_state, indent=2)
    else:
        prior_text += "(no prior state - this is the first run)\n"

    # v3.39.5: Build the deep-tier digests block (Layer 1 output from
    # overnight Haiku digestion of earnings transcripts and Korean broker
    # notes). These are PRIMARY SOURCES — the actual analytical body of
    # broker reports, written by the broker's analysts, not Korean news
    # aggregators' coverage of those reports. When today's news triage
    # mentions a broker action that overlaps with one of these digests,
    # the broker is the source of record, not the aggregator.
    #
    # Schema reference: src/analysis/digester.py Digest dataclass.
    # Fields: source_id, source_type, published_date_iso, title,
    # key_metrics (dict), signal_relevance (list of indicator IDs),
    # key_quotes (list of <15-word quotes, max 1 per doc), target_price_action
    # (dict with broker/ticker/new_pt/prior_pt/direction — broker notes only),
    # fired_status_evidence, raw_url, digested_at_iso.
    digest_text = ""
    if digests:
        broker_digests = [d for d in digests if d.get("source_type") == "korean_broker_pdf"]
        transcript_digests = [d for d in digests if d.get("source_type") == "earnings_transcript"]

        digest_text += "\nDEEP-TIER BROKER NOTES (PRIMARY SOURCE — horse's mouth):\n"
        if broker_digests:
            digest_text += (
                f"({len(broker_digests)} broker notes digested overnight by Haiku.\n"
                "These are the actual analytical bodies of broker reports — PRIMARY\n"
                "sources that take precedence over news aggregator coverage.)\n\n"
            )
            for d in broker_digests:
                tpa = d.get("target_price_action") or {}
                km = d.get("key_metrics") or {}
                digest_text += f"- TITLE: {d.get('title', '')}\n"
                digest_text += f"  PUBLISHED: {d.get('published_date_iso', '?')}\n"
                digest_text += f"  SOURCE URL: {d.get('raw_url', '')}\n"
                if tpa:
                    digest_text += (
                        f"  TARGET PRICE ACTION: {tpa.get('broker', '?')} "
                        f"{tpa.get('direction', '?')} {tpa.get('ticker', '?')} target → "
                        f"{tpa.get('new_pt', '?')} (prior: {tpa.get('prior_pt', '?')})\n"
                    )
                if km.get("fy27_eps_revision_pct") is not None:
                    digest_text += f"  FY27 EPS revision: {km['fy27_eps_revision_pct']:+.1f}%"
                    if km.get("fy27_eps_new") is not None and km.get("fy27_eps_prior") is not None:
                        digest_text += f" ({km['fy27_eps_prior']} → {km['fy27_eps_new']})"
                    digest_text += "\n"
                if km.get("fy28_eps_revision_pct") is not None:
                    digest_text += f"  FY28 EPS revision: {km['fy28_eps_revision_pct']:+.1f}%\n"
                key_quotes = d.get("key_quotes") or []
                if key_quotes:
                    digest_text += "  ANALYTICAL EXCERPT:\n"
                    for kq in key_quotes[:3]:
                        digest_text += f"    \"{kq}\"\n"
                sr = d.get("signal_relevance") or []
                if sr:
                    digest_text += f"  INFORMS INDICATORS: {', '.join(sr[:8])}\n"
                fse = d.get("fired_status_evidence", "")
                if fse:
                    digest_text += f"  FIRED-STATUS EVIDENCE: {fse}\n"
                digest_text += "\n"
        else:
            digest_text += "(no broker notes digested in window)\n"

        digest_text += "\nDEEP-TIER EARNINGS TRANSCRIPTS (PRIMARY SOURCE):\n"
        if transcript_digests:
            digest_text += f"({len(transcript_digests)} transcripts digested overnight.)\n\n"
            for d in transcript_digests:
                km = d.get("key_metrics") or {}
                digest_text += f"- TITLE: {d.get('title', '')}\n"
                digest_text += f"  PUBLISHED: {d.get('published_date_iso', '?')}\n"
                digest_text += f"  SOURCE URL: {d.get('raw_url', '')}\n"
                # Surface the most cycle-relevant metrics if present
                metric_lines = []
                if km.get("supplier_inventory_weeks") is not None:
                    metric_lines.append(f"supplier inventory: {km['supplier_inventory_weeks']} weeks")
                if km.get("lead_time_weeks") is not None:
                    metric_lines.append(f"lead time: {km['lead_time_weeks']} weeks")
                if km.get("asp_yoy_pct") is not None:
                    metric_lines.append(f"ASP YoY: {km['asp_yoy_pct']:+.1f}%")
                if km.get("contract_price_qoq_pct") is not None:
                    metric_lines.append(f"contract QoQ: {km['contract_price_qoq_pct']:+.1f}%")
                if km.get("capex_2027_guidance_usd_b") is not None:
                    metric_lines.append(f"2027 capex guidance: ${km['capex_2027_guidance_usd_b']}B")
                if metric_lines:
                    digest_text += "  METRICS: " + " | ".join(metric_lines) + "\n"
                # Architecture signals (v3.3 captive-ratio feedstock — surface salient ones)
                arch = km.get("architecture_signals") or {}
                arch_lines = []
                if arch.get("hbm_capex_pct") is not None:
                    arch_lines.append(f"HBM/capex: {arch['hbm_capex_pct']}%")
                if arch.get("memory_pct_of_capex") is not None:
                    arch_lines.append(f"memory/capex: {arch['memory_pct_of_capex']}%")
                if arch.get("wafer_allocation_language"):
                    arch_lines.append(f"wafer: {arch['wafer_allocation_language']}")
                if arch.get("architecture_freetext"):
                    arch_lines.append(arch["architecture_freetext"])
                if arch_lines:
                    digest_text += "  ARCHITECTURE: " + " | ".join(arch_lines) + "\n"
                key_quotes = d.get("key_quotes") or []
                if key_quotes:
                    digest_text += "  EXCERPT:\n"
                    for kq in key_quotes[:3]:
                        digest_text += f"    \"{kq}\"\n"
                sr = d.get("signal_relevance") or []
                if sr:
                    digest_text += f"  INFORMS INDICATORS: {', '.join(sr[:8])}\n"
                digest_text += "\n"
        else:
            digest_text += "(no new earnings transcripts in window)\n"
    else:
        digest_text = "\nDEEP-TIER DIGESTS: (deep_tier digests not threaded — running in legacy mode)\n"

    # v3.39.6: COMPUTED METRICS BLOCK. The numerical verdicts the fast_tier
    # ALREADY computed deterministically (no LLM involved) — relative strength
    # spreads, hyperscaler capex/revenue ratios, forward P/Es, DART materials.
    # Threading these in lets Sonnet QUOTE exact numbers instead of estimating
    # from news headlines. This is the cure for the May 18 brief's
    # t3_relative_strength hallucination: Sonnet wrote "5-day cumulative
    # underperformance has almost certainly crossed the >10% threshold" when
    # the actual computed spread was below threshold (banner_text was None
    # in the fast_tier log, meaning overall_severity was None).
    computed_text = "\nCOMPUTED METRICS (deterministic numerical verdicts — quote these EXACTLY):\n"
    if computed_metrics:
        rs = computed_metrics.get("relative_strength_full") or {}
        if rs:
            computed_text += "\n  t3_relative_strength (5-day rolling window vs SOX / SMH):\n"
            skhx = rs.get("skhx_5d_vs_sox_pp")
            mu = rs.get("mu_5d_vs_sox_pp")
            dram = rs.get("dram_etf_vs_smh") or {}
            # v3.39.7.9 — defensive NaN guard (belt + braces). Pre-v3.39.7.9
            # `if x is not None` allowed NaN floats through and produced
            # "+nanpp" in the brief. get_relative_strength_vs_sox() now
            # returns None on NaN, but defensive check stays here in case
            # an upstream path bypasses the guard.
            def _valid(v):
                return v is not None and not (isinstance(v, float) and math.isnan(v))
            if _valid(skhx):
                computed_text += f"    SK Hynix 5d vs SOX:      {skhx:+.2f}pp\n"
            else:
                computed_text += "    SK Hynix 5d vs SOX:      (not computed — SOX or SK Hynix price cell missing in lookback window)\n"
            if _valid(mu):
                computed_text += f"    Micron 5d vs SOX:        {mu:+.2f}pp\n"
            else:
                computed_text += "    Micron 5d vs SOX:        (not computed — SOX or Micron price cell missing in lookback window)\n"
            if dram.get("spread_pp") is not None:
                computed_text += (
                    f"    DRAM ETF 5d vs SMH:      {dram['spread_pp']:+.2f}pp "
                    f"(DRAM {dram.get('dram_return_pct', '?')}%, SMH {dram.get('smh_return_pct', '?')}%)\n"
                )
            computed_text += (
                f"    Overall severity:        {rs.get('overall_severity') or 'None'}\n"
                f"    Overall fired (red):     {rs.get('overall_fired', False)}\n"
                "    Thresholds:              red ≤ -10pp on ANY sub-trigger | yellow ≤ -5pp\n"
            )
            sub = rs.get("fired_sub_triggers") or []
            if sub:
                computed_text += "    Fired sub-triggers:      " + "; ".join(
                    f"{s[0]}: {s[1]}" if isinstance(s, (list, tuple)) and len(s) >= 2 else str(s)
                    for s in sub
                ) + "\n"

        fpe = computed_metrics.get("forward_pe") or {}
        if fpe:
            computed_text += "\n  Forward P/E by ticker:\n"
            for ticker, val in fpe.items():
                if val is None:
                    continue
                try:
                    computed_text += f"    {ticker:<12} {float(val):.2f}\n"
                except (TypeError, ValueError):
                    computed_text += f"    {ticker:<12} {val}\n"

        quarterly_snapshots = computed_metrics.get("hyperscaler_quarterly_snapshots") or {}
        if quarterly_snapshots:
            # v3.39.7.9 — capex/OCF is the ONLY trigger metric. capex/revenue
            # was removed entirely from the prompt (pre-v3.39.7.9 it was kept
            # "for context" and Layer 2 reversed verdicts against it). Mean
            # is now 2-quarter rolling per company per user direction
            # 2026-05-21 to filter out single-quarter OCF dip noise.
            mean_co_ratio = computed_metrics.get("mean_capex_ocf_ratio_2q")
            per_company = computed_metrics.get("per_company_capex_ocf_2q") or {}
            n_co = computed_metrics.get("n_companies_with_2q_capex_ocf", 0)
            computed_text += (
                "\n  Hyperscaler capex/OCF strain (PRIMARY and ONLY trigger for t2_hyperscaler_capex_revenue;\n"
                "  the legacy ratio of capex over revenue is NOT used as a trigger — do not cite or reason from it):\n"
            )
            if mean_co_ratio is not None:
                # Determine band relative to thresholds
                if mean_co_ratio > 0.50:
                    band = "FIRED (>50%)"
                elif mean_co_ratio > 0.40:
                    band = "PARTIALLY FIRED (>40%)"
                else:
                    band = "Not fired (<40%)"
                computed_text += (
                    f"    Aggregate (cross-company mean of 2-quarter rolling per-company means, {n_co} hyperscalers): "
                    f"{mean_co_ratio*100:.1f}%  →  {band}\n"
                    f"    Historical reference: mean >40% in all 3 memory crashes "
                    f"(2014/2018/2022); stays <40% in non-crash years.\n"
                )
                if per_company:
                    computed_text += "    Per-company 2-quarter rolling mean capex/OCF:\n"
                    for ticker, r in per_company.items():
                        computed_text += f"      {ticker:<6} {r*100:.1f}%\n"
            else:
                computed_text += "    Aggregate: insufficient data (no last-quarter ratios available).\n"
            computed_text += "\n  Per-company quarterly snapshots (raw — for narrative context only; trigger uses 2q mean above):\n"
            for ticker, quarters in quarterly_snapshots.items():
                if not quarters:
                    continue
                # Snapshots sorted newest-first; show up to 2 quarters
                for i, q in enumerate(quarters[:2]):
                    co_r = q.get('capex_ocf_ratio')
                    co_s = f"{co_r*100:.1f}%" if co_r is not None else "n/a"
                    strain_flag = "  ⚠ severe single-q strain >100%" if q.get('fired_threshold') else ""
                    label = "latest" if i == 0 else "prior"
                    computed_text += (
                        f"    {ticker:<6} {q.get('quarter_iso', '?')} ({label}): "
                        f"capex ${q.get('capex_usd_b', '?')}B / "
                        f"OCF ${q.get('ocf_usd_b', 'n/a')}B = capex/OCF {co_s}{strain_flag}\n"
                    )

        dart = computed_metrics.get("dart_material_disclosures") or {}
        if dart:
            n_companies = len(dart)
            total_mat = sum(len(v) if isinstance(v, list) else 0 for v in dart.values())
            computed_text += f"\n  DART material disclosures: {total_mat} across {n_companies} companies\n"

        # v3.4.0: DECODE HBM CAPTIVE RATIO (DHCR) — direction monitor
        # New Tier 1 indicator. Composite of (a) frontier KV efficiency,
        # (b) HBM-per-accelerator capacity, (c) enterprise AI demand growth.
        # The CORE QUESTION: is enterprise demand-pull growing faster than
        # the supply-side capacity-creation rate? If yes → HBM tight (bullish);
        # if no → HBM slack (bearish).
        #
        # Mental model rejects the naive "efficiency vs context" trade-off.
        # Efficiency UNLOCKS economic context capacity. Enterprise workloads
        # expand to consume what's economically possible. The binding question
        # is whether enterprise demand grows fast enough to fill the unlocked
        # headroom.
        dhcr = computed_metrics.get("dhcr_snapshot") or {}
        if dhcr:
            computed_text += (
                "\n  t1_decode_hbm_captive_ratio (direction monitor — NEW v3.4.0):\n"
            )
            cap_tokens = dhcr.get("implied_context_capacity_per_accelerator_tokens", 0) or 0
            if cap_tokens >= 1_000_000:
                cap_s = f"{cap_tokens/1_000_000:.1f}M"
            elif cap_tokens >= 1_000:
                cap_s = f"{cap_tokens/1_000:.0f}K"
            else:
                cap_s = str(cap_tokens)
            computed_text += (
                f"    Frontier KV efficiency: {dhcr.get('frontier_kv_kb_per_token', 0):.0f} KB/tok "
                f"({dhcr.get('frontier_model_name', '?')}, {dhcr.get('frontier_model_arch', '?')})\n"
                f"    Frontier HBM/accelerator: {dhcr.get('frontier_hbm_gb_per_accelerator', 0):.0f} GB "
                f"({dhcr.get('frontier_accelerator_name', '?')})\n"
                f"    Implied context capacity: ~{cap_s} tokens per accelerator at frontier efficiency\n"
                f"    YoY KV change: {dhcr.get('kv_yoy_change_pct', 0) or 0:+.0f}% "
                f"(negative = efficiency improving)\n"
                f"    YoY HBM-per-accelerator change: {dhcr.get('hbm_per_accelerator_yoy_change_pct', 0) or 0:+.0f}%\n"
                f"    Composite capacity-creation rate: {dhcr.get('capacity_creation_yoy_pct', 0) or 0:+.0f}% YoY\n"
                f"    Enterprise AI run-rate (MSFT+AWS+GCP+Oracle): "
                f"${dhcr.get('enterprise_ai_aggregate_run_rate_usd_b', 0):.0f}B annualized\n"
                f"    Enterprise AI YoY: {dhcr.get('enterprise_ai_yoy_growth_pct', 0) or 0:+.0f}%\n"
                f"    NVIDIA datacenter YoY: {dhcr.get('nvidia_datacenter_yoy_growth_pct', 0) or 0:+.0f}%\n"
                f"    Hyperscaler capex YoY: {dhcr.get('hyperscaler_capex_yoy_growth_pct', 0) or 0:+.0f}%\n"
                f"    DIRECTION READ: {dhcr.get('direction', 'unknown').upper()} "
                f"(confidence: {dhcr.get('confidence', '?')})\n"
                f"    Dominant force: {dhcr.get('dominant_force', '?')}\n"
            )
            subst = dhcr.get("substitution_risk_notes") or []
            if subst:
                computed_text += "    Substitution-risk flags (non-HBM AI accelerators):\n"
                for note in subst[:5]:
                    computed_text += f"      • {note}\n"
            computed_text += (
                "    Interpretation guidance: This is a DIRECTION MONITOR, not a "
                "fire/not-fire signal. Always render current_status='Not fired' for "
                "this indicator in v3.4.0. Use 'where_we_stand' field to describe "
                "the direction read and what's driving it. The mental model is that "
                "efficiency gains UNLOCK context capacity which enterprise workloads "
                "then consume — so DECELERATION in enterprise revenue growth is the "
                "actual bearish signal, not efficiency improvements per se.\n"
            )
    else:
        computed_text += "  (computed_metrics not threaded — running in legacy mode)\n"

    # v3.39.6: LAYER 2 SIGNAL REASONINGS BLOCK. Haiku already evaluated each
    # of the 22 indicators with full access to digests, naver_data, finnhub,
    # computed_metrics, and fast_tier news. Its verdicts are authoritative
    # PER-SIGNAL. Sonnet should CITE these (not re-derive them).
    layer2_text = "\nLAYER 2 PER-SIGNAL VERDICTS (Haiku, with full evidence access — DEFER to these on individual signal status):\n"
    if signal_reasonings:
        layer2_text += (
            f"({len(signal_reasonings)} signals evaluated. Each verdict is the result\n"
            "of Haiku reading ALL evidence for that one signal — broker digests,\n"
            "transcripts, Naver rows, Finnhub direction, computed metrics, news. When\n"
            "writing indicator_assessments[].current_status and change_vs_prior,\n"
            "use these verdicts as the source of truth.)\n\n"
        )
        for r in signal_reasonings:
            rd = r.to_dict() if hasattr(r, "to_dict") else r
            layer2_text += (
                f"  [{rd.get('signal_id', '?')}] T{rd.get('tier', '?')} "
                f"{rd.get('signal_name', '?')}\n"
                f"    Verdict: {rd.get('fired_status', '?')} "
                f"(confidence: {rd.get('confidence', '?')}, "
                f"convergence: {rd.get('convergence', '?')}, "
                f"evidence_count: {rd.get('evidence_count', 0)})\n"
            )
            fse = rd.get("fired_status_evidence") or []
            if fse:
                layer2_text += "    Evidence for current status:\n"
                for ev in fse[:4]:
                    layer2_text += f"      - {ev}\n"
            change = rd.get("evidence_for_change_vs_yesterday") or ""
            # v3.39.6: suppress noisy "no change" lines — we only render
            # change-vs-yesterday when something actually changed. Use
            # startswith so phrasings like "no change — still not fired"
            # or "no change — verdict held" are also suppressed.
            change_lower = change.lower().strip()
            is_noop = (
                not change_lower
                or change_lower in ("no change", "none", "no", "n/a")
                or change_lower.startswith("no change")
            )
            if change and not is_noop:
                layer2_text += f"    Change vs yesterday: {change}\n"
            layer2_text += "\n"
    else:
        layer2_text += "  (signal_reasonings not threaded — running in legacy mode)\n"

    prompt = f"""You are a senior memory/semiconductor cycle analyst preparing a daily institutional briefing. Use the Haiku-pre-processed news + market data + indicator framework below to produce a comprehensive structured analysis.

ANALYTICAL FRAMEWORK CONTEXT:
- Memory cycle has 5 phases. Currently late Phase 4 ("Boom / Euphoria").
- Stock peak typically leads fundamental peak (peak EPS) by 3-6 months.
- Tier 1 signals lead stock peak by 3-6 months.
- Tier 2 signals lead stock peak by 2-4 months.
- Tier 3 signals are concurrent with stock peak.
- Tier 4 signals lag stock peak.
- Most likely peak window per current evidence: Q3 2026 - Q1 2027.

YOUR JOB:
1. Build the EXECUTIVE SUMMARY in the structured format below — including the consolidated `executive_brief` paragraph that anchors the brief (see detailed spec in section EXECUTIVE BRIEF SPECIFICATION below).
2. Assess EVERY indicator (status, where we stand, latest developments with links).
3. Provide DEEP DIVES for the top 5 indicators.
4. Compose the cycle position synthesis paragraph.
5. Identify NEW TRIGGERS that fired today (compare against prior state).
6. Calculate UPDATED PROBABILITY DISTRIBUTION based on new evidence.
7. Compare current peak estimate vs prior to flag if peak has shortened or extended.

EXECUTIVE BRIEF SPECIFICATION (v3.39.7.5, length cap v3.4.1):

The `executive_brief` field is the consolidated paragraph at the top of the brief — the FIRST thing the reader sees after the phase line and trigger callouts. It replaces three separate v3.39.7.4 blocks (delta_from_prior_text prose, big_movers list, market_summary) into a single coherent narrative.

Length: **HARD CAP 250 WORDS** (v3.4.1 — previous unbounded brief reached 411 words on May 21 2026). The cap is a discipline, not a sacrifice — compress, don't cut. Target 5-8 sentences, ONE paragraph. Remove filler words ("notably", "interestingly", "with respect to"), nest clauses tightly, prefer active voice, lose redundant qualifiers. If a sentence is 30+ words, split it or trim it. The reader uses this to decide whether to read on; details are in later sections — point them to §N rather than restating.

Self-check before emitting: count your words. If >250, compress further. Look first for: (a) intensifiers ("dramatically", "substantially"); (b) restated context already in the indicator status pills; (c) hedges ("appears to", "seems to indicate"); (d) numerical detail that belongs in §3 / §4 cards, not the headline paragraph; (e) anchor link explanations like "see §3 for more" — the anchor itself IS the explanation.

Content — cover all four themes in this order, but blend them into prose (NOT four labeled bullets):
  (i)  Signal fires/unfires today + what drove them (cite the specific indicator and the trigger value / news that caused the change)
  (ii) Big news from today's feed. PRIORITIZE items that align with a framework signal even when they don't formally fire it. Example: a single-firm "Sell SK Hynix" rating doesn't fire t2_insider_sales (which requires actual insider transactions), but in a market of 26-up / 0-down EPS revisions it IS the first sell-side dissent — a psychological inflection the framework cares about deeply. Surface these as "directional confirmation of [signal name]" or "first dissent from the [signal name] consensus".
  (iii) Big movers today — name 2-4 specific tickers with their 1D % moves. Focus on those that diverge from the broader semi tape (especially the memory pure-plays).
  (iv) Macro / AI / semiconductor backdrop news that impacts the trade (Fed decisions, China export controls, NVIDIA orders, AI capex announcements, etc).

FORBIDDEN VOCABULARY (v3.4.1):
The word "Reversed" is BANNED in all output fields — `executive_brief`, `new_triggers_today`, `change_vs_prior`, `where_we_stand`, `latest_developments`. Use "Not fired" for reversals. If an indicator transitioned from "Fired" to "Not fired", phrase it as: "t2_X reverted to 'Not fired' after [reason]" — NEVER "Status changed from 'Fired' to 'Reversed'". This is a hard rule — any output containing "Reversed" will be post-processed to "Not fired" at render time, but writing it correctly the first time avoids ambiguity.

Anchor links — when referencing detail elsewhere in the brief, emit hyperlinks of the form `[→§N]` where N is the section number (1-7):
  §1 = Performance overview (price table)
  §2 = Where we stand in the cycle (phase chart)
  §3 = Tier indicators — daily status (summary cards)
  §4 = Top 5 indicators — deep dive (deep-dive narratives, includes matrix)
  §5 = Latest news from the memory & semi market
  §6 = Calendar of events — next 90 days
  §7 = Data source assessment

The template substitutes `[→§N]` into clickable HTML anchors. Use them liberally — every claim that has a detail elsewhere should link. Example:
  "...ASML +5.5% [→§1] led equipment names higher on strong demand commentary [→§5], while SK Hynix flat amid first sell-side dissent (directional confirmation of t2_insider_sales [→§3])..."

Tone: institutional analyst, declarative, no hedging language ("might", "could", "perhaps"). State the facts as observed.

RECENCY ON CONTRADICTING EVENTS (v3.39.7.6):

When two or more news items in today's feed reference the SAME real-world event but report contradictory direction (e.g. one says "Samsung strike to launch", another says "Samsung union calls off strike"), you MUST resolve the contradiction by published timestamp, not by item count.

Operational rules:
  (a) Identify event clusters — items are in the same cluster when they reference the same company/entity AND the same event window (a strike, a wage negotiation, an earnings beat, a regulatory ruling, a deal closure, etc.).
  (b) Within a cluster, the item with the LATEST published timestamp is the operative state of the world. Earlier items in the cluster are superseded — treat them as background context, not as current facts.
  (c) If the cluster contains a REVERSAL (event was on, then called off; or event was averted, then re-triggered), the executive_brief MUST explicitly note the reversal. Do not write as if the older state still holds. Example: do NOT write "the Samsung 45,000-worker strike commencing May 21 tightens HBM supply" if a later item in the same cluster reports the strike was called off — the operative narrative is "the planned Samsung strike was called off one hour before launch, removing the near-term HBM supply tightening risk that was previously priced in".
  (d) The reversal itself is news — surface it explicitly. The framework cares about state changes, not historical states.
  (e) This rule applies to the executive_brief paragraph AND to the key_news_summary block AND to per-indicator commentary. Apply it everywhere news drives narrative.

This rule does NOT change the relevance filter or which items reach you — it changes how you SYNTHESIZE items that all reach you. Multiple contradicting items in your context window is the normal case for fast-moving stories; you are responsible for resolving them.

VALUE-CHAIN INFERENCE (v3.39.7.9):

When two or more signals in today's feed are causally linked through the memory value chain, draw the chain EXPLICITLY in the executive_brief and per-indicator commentary. Reporting upstream and downstream signals as if they're independent loses the framework's core analytical value. Examples of inference chains you should draw when the evidence is present in today's brief:

  CAPACITY-MIX SHIFTS:
  - SK Hynix or Samsung announces HBM capacity expansion / wafer reallocation toward HBM → less DDR5 / LPDDR / NAND wafer allocation → spot price tightening short-term + contract price acceleration 1-2 quarters out. Conversely a HBM capacity cut → DDR5 spot loosens. State the chain explicitly: "X capacity shift to Y means Z effect on the W market in T months".
  - Korean fab outage / labor disruption affecting Samsung or SK Hynix → near-term HBM AND DDR5/LPDDR spot tightening → spread between spot and contract narrows → t2_spot_contract_spread input direction shifts.

  DEMAND CASCADES:
  - Hyperscaler ups AI capex guidance (Tier C) → NVIDIA / AMD / Broadcom backlog increases (Tier B) → SK Hynix HBM revenue 1-2 quarters out → memory pure-play earnings beat / guidance raise.
  - AI lab signs multi-billion infrastructure deal (OpenAI / Anthropic) → chip designer capacity commitment (Cerebras / NVIDIA) → memory order book → DRAM/HBM cycle support.
  - Conversely a hyperscaler capex cut warning → downstream HBM order moderation 6-12 months out → bearish for the cycle peak timing.

  COMPETITIVE / SUBSTITUTION:
  - CXMT / ChangXin capacity milestone or yield improvement → commodity DDR4/DDR5 oversupply risk → Samsung/SK Hynix spot pricing pressure on those nodes. State the timing: short-term impact (1-3mo) on spot, medium-term (3-9mo) on contract.
  - Cerebras WSE-3 deployment news or in-house ASIC production milestone (TPU v7, Trainium 2, MTIA v3) → potential HBM substitution at the margin → captive-ratio risk to SK Hynix HBM revenue trajectory.

  PRICE-COMMENTARY LINKS:
  - TrendForce spot print + earnings commentary on contract / inventory → connect to t1_spot_decline, t2_spot_contract_spread, and t1_oem_inventory together rather than as isolated data points.

Inference style: "X implies Y because Z" rather than "X happened. Y happened." Make the causal mechanism visible — even one sentence is enough. The framework's job is connecting upstream and downstream signals, not just listing them.

LIMITS:
  - Draw inferences ONLY when both ends of the chain have evidence in today's brief or a recent (≤7d) carried-forward evidence entry from the indicator evidence store. Do not speculate from one end with no anchor at the other.
  - Inferences that are "common knowledge" without specific evidence in the brief don't belong in the brief — they belong in the framework documentation.
  - If you draw an inference, cite the evidence anchors for both ends inline.

OUTPUT FORMAT - respond with ONLY a JSON object:

{{
  "executive_summary": {{
    "current_phase": 4,
    "current_phase_label": "Boom / Euphoria (late stage)",
    "tier_status_summary": "T1: X/6 fired | T2: Y/7 fired | T3: Z/5 fired | T4: W/4 fired",
    "new_triggers_today": [
      {{"tier": 2, "indicator_id": "...", "indicator_name": "...", "description": "..."}}
    ],
    "executive_brief": "5-8 sentence consolidated paragraph per EXECUTIVE BRIEF SPECIFICATION above. Cover all 4 themes (signal changes, big news with directional-signal prioritization, big movers, macro). Use anchor links of the form [→§N] for cross-references.",
    "base_case": {{"probability_pct": 50, "peak_months_away": 4, "peak_window_start": "August 2026", "peak_window_end": "October 2026", "return_to_peak_pct": 18}},
    "bull_case": {{"probability_pct": 22, "peak_months_away": 12, "peak_window_start": "Q1 2027", "peak_window_end": "Q2 2027", "return_to_peak_pct": 55}},
    "bear_case": {{"probability_pct": 28, "peak_months_away": 0, "peak_window_start": "May 2026", "peak_window_end": "May 2026", "return_to_peak_pct": -8}},
    "delta_from_prior_text": "Legacy field (v3.39.7.5: no longer rendered, but keep emitting for back-compat / observability). Short description of probability/peak-window shifts vs prior brief."
  }},
  "indicator_assessments": [
    {{
      "id": "t1_spot_decline",
      "tier": 1,
      "name": "...",
      "description": "...",
      "what_to_look_for": "...",
      "current_status": "Fired | Partially fired | Not fired",
      "where_we_stand": "1-3 sentences",
      "latest_developments": "Markdown-formatted with embedded links",
      "relevant_links": [{{"title": "...", "url": "..."}}],
      "change_vs_prior": "If status changed vs prior, describe; else null"
    }}
  ],
  "market_summary": "2-3 sentence overview of today's memory/semi market action",
  "key_news_summary": [
    {{"headline": "...", "url": "...", "why_it_matters": "..."}}
  ],
  "cycle_position_call": "1 paragraph: where we are in cycle today, what changed since prior brief, what to watch next"
}}

DEEP-DIVE DEPTH (v3.39.1): The following indicator IDs should be written with
EXTRA depth in their `where_we_stand` and `latest_developments` fields — 2-3
fuller paragraphs vs 1-2 sentences for the rest. The client synthesizes the
"Top 5 deep dives" section by plucking these IDs from indicator_assessments,
so do NOT emit a separate top_5_deep_dives array (was costing ~10K tokens
and triggering output truncation):
  t1_spot_decline, t2_capex_parabolic, t2_eps_revisions,
  t3_supplier_inventory_rebuild, t3_relative_strength

SCENARIO FRAMING — READ CAREFULLY:
The three scenarios are signal-CONDITIONAL probabilities for the length-to-peak
and price-to-peak in SK Hynix stock. They are NOT three symmetric outcomes.

• base_case = the EXPECTED scenario given signals continuing at their current
  pace. By definition the most probable scenario. As Tier 2/3/4 signals fire,
  update base case's PARAMETERS (peak_months_away SHORTENS, return_to_peak_pct
  TRIMS) — do NOT let bear probability exceed base.

• bull_case = scenario where signals UN-FIRE (those that fired reverse) or
  fire much later than expected. Requires abnormal positive surprise. Always
  has positive return_to_peak_pct. Probability SHRINKS as more signals fire
  (becomes less plausible).

• bear_case = scenario where degradation accelerates BEYOND current signal
  trajectory. Requires abnormal negative surprise. Peak shortens to 0; the
  return_to_peak_pct can be NEGATIVE (peak already in → downside from here).
  Probability GROWS as more signals fire — but capped strictly below base.

HARD INVARIANTS (enforced post-output; produce values that already satisfy):
  1. probability_pct sums to EXACTLY 100 across the three scenarios.
  2. base_case.probability_pct > bear_case.probability_pct.
  3. base_case.probability_pct > bull_case.probability_pct.
  4. base_case.return_to_peak_pct >= 0 (positive — base never assumes peak passed).
  5. bull_case.return_to_peak_pct > base_case.return_to_peak_pct (always higher upside).
  6. bear_case.return_to_peak_pct <= base_case.return_to_peak_pct (lower; can be negative).

PROBABILITY UPDATE RULES (signal-conditional):

A. WHEN AN INDICATOR FIRES TODAY (Not fired → Partially fired, or → Fired):
- New Tier 2 trigger fired today: bear probability +3-6pp, base shortens
  peak_months_away by ~1mo and trims return_to_peak_pct by ~3-5pp. Bull -1-3pp.
- New Tier 3 trigger fired today: bear probability +5-8pp (still under base),
  base peak_months_away drops to ≤2mo, base return_to_peak_pct trims ~5-10pp.
  Bull case shrinks meaningfully (-3-5pp).
- New Tier 4 trigger: same magnitude as Tier 3 but with bear return_to_peak_pct
  going negative (e.g., -5 to -15pp) — confirms peak is in.
- Bull confirms (e.g., hyperscaler 2027 capex >2026): bull probability +3-5pp,
  bear -2-4pp, base nudges peak_months_away by ~+1mo.

B. WHEN AN INDICATOR UNFIRES TODAY (v3.39.7.3 — SYMMETRIC MIRROR OF RULE A):
Indicators going FIRED → Partially fired, or FIRED → Not fired, or Partially
fired → Not fired are EQUALLY material as new fires. Yesterday's signal
distribution is no longer valid. You MUST recalculate probabilities in the
OPPOSITE direction:
- Tier 1 indicator unfires today: bear probability -2-4pp, bull +1-2pp,
  base extends peak_months_away by ~1mo, base return_to_peak_pct restores
  by 2-3pp.
- Tier 2 indicator unfires today: bear -3-6pp, bull +2-4pp, base peak
  extends 1-2mo, return_to_peak_pct restores ~3-5pp.
- Tier 3 indicator unfires today: bear -5-8pp, bull +3-5pp, base peak
  extends 2-3mo, return_to_peak_pct restores ~5-10pp.
- Tier 4 indicator unfires today: bear -5-8pp (clamp at min 5%), bull +5-8pp,
  base peak extends 2-3mo, return_to_peak_pct restores ~5-15pp.

MANDATORY RECALCULATION INSTRUCTION (v3.39.7.3 — DO NOT SKIP):
Inertia is FORBIDDEN. If the count of {{Fired, Partially fired, Not fired}}
indicators differs from prior_state in ANY direction (more fires OR more
unfires OR mixed), you MUST recalculate base/bull/bear probabilities by
walking through each status change and applying rules A or B above. The
ONLY case where probabilities stay byte-identical to prior_state is when
EVERY single indicator's status is unchanged. Even one status change
requires a recalculation.

Reasoning template for delta_from_prior_text when status counts changed:
  "Indicator distribution shifted vs prior: {{fired_then}}→{{fired_now}} fired,
   {{partial_then}}→{{partial_now}} partial. Applied [rule A or B] for [list
   indicators with old→new status]; bear {{pp_change}}pp, bull {{pp_change}}pp,
   base peak {{month_change}}mo."

If the LLM is tempted to write "no probability distribution changes
warranted" because no NEW fires happened, this is a BUG — check for UNFIRES.
A reduction in fired count is equally probability-moving as an increase.

C. WHEN COMPUTED METRICS CONTRADICT LAYER 2 VERDICTS (v3.39.7.3, updated v3.39.7.9):
For indicators whose status is primarily driven by computed numerical
data (rel_strength_full, forward_pe, mean_capex_ocf_ratio_2q, eps_revisions
weighted scoring), the COMPUTED VERDICT is authoritative. If Layer 2
returns "Not fired" with evidence_count=0 BUT the computed data shows
the threshold is crossed, REJECT Layer 2 and use the computed verdict.
Explicitly note this in `where_we_stand`: "Computed verdict: Fired
(P/E 4.56x < 5x threshold). Layer 2 returned Not fired with evidence_count=0
but is overridden per v3.39.7.3 computed-metric authority rule."

D. BEAR CASE MAGNITUDE CALIBRATION (v3.39.7.4 — DO NOT SKIP):

The May 20 2026 brief produced bear `return_to_peak_pct` = -12% with 4 T1
+ 2 T2 indicators fired. This was empirically 3-4x too mild. Rules A/B/C
above govern PROBABILITY shifts; this block governs MAGNITUDE.

The bear case is calibrated to historical SK Hynix peak-to-trough drawdowns
in completed memory cycles:
- 2014 NAND mini-cycle: -36%
- 2018 DRAM cycle: -43%
- 2022 DRAM/NAND cycle: -49%
- AVERAGE: -43%

The bear case is NOT a small downward drift from base case. It is the
AVERAGE historical drawdown scaled by current signal density. As more
indicators fire, bear `return_to_peak_pct` must approach this historical
norm.

Bear `return_to_peak_pct` magnitude bands (apply ON TOP of A/B/C):

| Fired-indicator regime                  | Bear return_to_peak_pct |
|-----------------------------------------|-------------------------|
| 0-1 T1 + 0-2 T2 fired (early cycle)     | -10 to -20%             |
| 2-3 T1 OR 3-4 T2 (mid escalation)       | -25 to -35%             |
| 4+ T1 OR 5+ T2 (late, peak imminent)    | -35 to -45%             |
| Any T3 fired (post-peak confirmation)   | -40 to -50%             |
| Any T4 fired (cycle bottom approaching) | -45 to -55%             |

When you write bear `return_to_peak_pct`, refer to these bands directly.
If you find yourself writing a number above -20% with majority-T1 fired,
that is a BUG — go back and apply the band rules.

Anchoring instruction: cite the historical reference verbatim in
`delta_from_prior_text` when bear case changes magnitude band:
  "Bear case calibrated to historical average drawdown (-43%, mean of
   2014/-36%, 2018/-43%, 2022/-49% SK Hynix peak-to-trough)."

PEAK MONTH ESTIMATE RULES:
- Compare against prior_state.executive_summary.base_case.peak_months_away
- If shorter: "shortened by X months due to [specific trigger]"
- If longer: "extended by X months due to [specific trigger]"
- If unchanged: "Base case peak window unchanged vs yesterday"
- If first run: "No prior comparison available (first brief)"

URGENT ATTENTION TRIGGER RULES:
Set urgent_attention_needed=true if ANY of:
- A previously "Not fired" indicator becomes "Fired" or "Partially fired" today
- Multiple Tier 2/3 indicators changed status
- Big movers include SK Hynix, Micron, or Samsung with >4% move
- Probability distribution shifted significantly (e.g., bear case +10pp vs prior)

PRIMARY-SOURCE PRECEDENCE (v3.39.5):
When today's NEWS TRIAGE mentions a broker action (target price change,
rating change, estimate revision) that ALSO appears in DEEP-TIER BROKER
NOTES, the broker IS the primary source. Cite the broker (from the
TARGET PRICE ACTION line, e.g. "Mirae Asset Securities") in your
latest_developments and key_news_summary — NOT the Korean news
aggregator (Seoul Economic, AD HOC NEWS, abhs.in) that echoed the
broker's report. Use the broker's SOURCE URL (from the digest's raw_url
field), not the aggregator's URL.

MAJOR BROKER ACTIONS IN KEY NEWS (v3.39.5):
A target-price change of ≥10% or a rating change is material market-moving
news. If a DEEP-TIER BROKER NOTE shows a TARGET PRICE ACTION with such a
move, it MUST appear in your `key_news_summary` array — even if news
triage didn't surface it. Format the headline as: "{{broker}}
raises/lowers {{ticker}} target to {{new_pt}} from {{prior_pt}}". Use
the broker's ANALYTICAL EXCERPT lines to write the why_it_matters field.

NUMBER-FIRST INDICATOR FORMAT (v3.39.6 — STRICT, length cap v3.4.1):
For every indicator_assessments[] entry, the `where_we_stand` field
MUST follow this template when the indicator has computed numerical
evidence (rel-strength spreads, hyperscaler capex/OCF ratios, forward P/E,
EPS revisions, single-day moves, etc.):

  1. LEAD with the exact numbers. Quote them from COMPUTED METRICS.
     Example: "5-day vs SOX: SK Hynix -3.21pp, Micron -2.84pp, DRAM
     ETF vs SMH -1.10pp. Severity: None (yellow threshold -5pp,
     red threshold -10pp — neither crossed)."
  2. THEN a SHORT trend/context note (≤1/3 of the words). Example:
     "Spread has widened ~2pp this week as memory drawdowns
     accelerated; would cross yellow if today's relative weakness
     persists 2 more sessions as the May 11-12 positive days roll off."
  3. NO speculative "almost certainly past threshold" or "highly
     probable when measured" — the metric IS measured. If you don't
     see it in COMPUTED METRICS, say "(not threaded)" — do NOT
     reason backward from news headlines.

**HARD CAP: 120 WORDS per where_we_stand field** (v3.4.1).
Per the May 21 2026 audit, where_we_stand was sometimes empty (notably
t2_capex_parabolic on May 21) because Sonnet was hitting its
max_tokens budget late in the output and silently truncating fields.
The cap is two-sided protection:
  - Each individual where_we_stand must be ≤120 words so the schema
    fits comfortably in the token budget across all 23 indicators.
  - For indicators with rich evidence (t2_capex_parabolic regularly
    has 5+ named announcements in the evidence store), use the cap
    to PRIORITIZE — pick the 2-3 most material items, name them with
    dollar amounts, link the source. Don't try to list everything;
    that's what the evidence store is for.

CRITICAL — NEVER LEAVE where_we_stand EMPTY (v3.4.1 Fix 5):
If today's news has no fresh update on this indicator, where_we_stand
must STILL be populated with the carried-forward evidence. Pull from
LAYER 2 PER-SIGNAL VERDICTS or the evidence_store entries for that
indicator and write a 1-2 sentence summary of "where we currently
stand based on accumulated evidence" — what's fired, since when, and
the dollar amounts/dates that drove it. Example for t2_capex_parabolic
when today has no new capex print: "Fired since Mar 2025: $725B aggregate
2026 hyperscaler capex (MSFT+GOOGL+AMZN+META) confirmed via Q4 2025
earnings cycle, latest NVIDIA Q1 FY27 $81.6B revenue with $1T cumulative
hyperscaler capex by 2027 (Tom's Hardware [→§5]) reinforces the trend."
NEVER emit empty or sub-30-character where_we_stand — that's a bug.

`latest_developments` should be similarly tight: 2-4 sentences max
with embedded links, NOT multi-paragraph essays. The reader scans
this column on mobile; brevity is the design intent.

For indicator status (`current_status`, `change_vs_prior`), DEFER
to LAYER 2 PER-SIGNAL VERDICTS when present. Haiku already weighed
the full evidence for that one signal — re-deriving the verdict in
Sonnet's prose risks contradicting Layer 2 and looks bad in the
dashboard where Layer 2 status renders alongside your text.

Cycle baseline (use as your prior, adjust per today's evidence):
{json.dumps(CYCLE_BASELINE, indent=2)}

Top 5 indicators for deep dives: {", ".join(TOP_5_INDICATORS)}

NOW THE DATA:

{perf_text}
{movers_text}
{est_text}
{indicator_text}
{prior_text}
{digest_text}
{computed_text}
{layer2_text}
{triage_text}
"""
    return prompt


def sonnet_analyze(
    api_key: str,
    triage: NewsTriage,
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    big_movers: list[BigMover],
    prior_state: dict,
    collector: Optional[DiagnosticsCollector] = None,
    digests: Optional[list[dict]] = None,
    signal_reasonings: Optional[list] = None,
    computed_metrics: Optional[dict] = None,
    news_items: Optional[list[NewsItem]] = None,
) -> Optional[BriefAnalysis]:
    """Run Sonnet to produce the full analysis with exec summary.

    v3.39.5: Accepts `digests` (deep-tier Layer 1 output).
    v3.39.6: Accepts `signal_reasonings` (Layer 2 Haiku verdicts) and
    `computed_metrics` (numerical deterministic verdicts). Threaded into
    the prompt so Sonnet quotes exact numbers and defers to Layer 2 on
    per-signal status, rather than speculating from news headlines.
    v3.39.7.6: Accepts `news_items` (raw NewsItem list). Threaded into
    `build_sonnet_analysis_prompt` so triage items in the Sonnet prompt
    carry their published timestamps — required for the RECENCY ON
    CONTRADICTING EVENTS prompt rule to be actionable.
    """
    if not api_key:
        if collector is not None:
            collector.record_skipped(
                f"Anthropic API / {SONNET_MODEL} (stage 2 analysis)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
        return None

    client = Anthropic(api_key=api_key)
    prompt = build_sonnet_analysis_prompt(
        triage, performance_data, estimates_data, big_movers, prior_state,
        digests=digests,
        signal_reasonings=signal_reasonings,
        computed_metrics=computed_metrics,
        news_items=news_items,
    )

    # v3.39: switch from messages.create to messages.stream. Two motivations:
    #
    # (1) Bypass the SDK's client-side wall-time guard. messages.create raises
    #     "Streaming is required for operations that may take longer than 10
    #     minutes" when SDK-estimated throughput * max_tokens exceeds 600s.
    #     That threshold fired in the 16:25 May 17 run at max_tokens=24000,
    #     prompting v3.38.5 to lower the cap to 16000 — which caused the
    #     truncation seen in the 17:43 run (output cut at ~33K chars / ~16K
    #     tokens mid-string; json_repair recovered only 12/22 indicators,
    #     dropping Tier 3, Tier 4, top-5, and selected_news sections from
    #     the email). Streaming has no such guard.
    #
    # (2) Stability under transient 503s. The 17:43 run hit one Anthropic
    #     503 mid-call that the SDK auto-retried, but a streaming call lets
    #     us observe partial progress and surfaces network issues earlier.
    #
    # We accumulate chunks into a final message via stream.get_final_message().
    # The SDK exposes .content[0].text on that object identical to the
    # non-streaming path, so downstream parsing is unchanged.
    response = None
    try:
        if collector is not None:
            with collector.track(
                f"Anthropic API / {SONNET_MODEL} (stage 2 analysis, streaming)",
                "llm",
            ) as t:
                with client.messages.stream(
                    model=SONNET_MODEL,
                    max_tokens=SONNET_MAX_TOKENS,
                    temperature=LLM_TEMPERATURE,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    response = stream.get_final_message()
                t.items_returned = 1
                t.detail = f"prompt: {len(prompt)} chars, max_tokens={SONNET_MAX_TOKENS}"
        else:
            with client.messages.stream(
                model=SONNET_MODEL,
                max_tokens=SONNET_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                response = stream.get_final_message()
    except Exception as e:
        logger.error(f"Sonnet analysis streaming call failed: {e}")
        return None

    # v3.39.1: surface Sonnet token usage in the run log. The 20:30 May 17
    # run truncated silently — without this log we had to infer cap-hit
    # from json_repair recovery counts. With usage logged, we can compare
    # output_tokens directly to SONNET_MAX_TOKENS and know immediately if
    # we're capping out.
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_t = getattr(usage, "input_tokens", 0)
            out_t = getattr(usage, "output_tokens", 0)
            cap_hit = " (HIT MAX_TOKENS CAP)" if out_t >= SONNET_MAX_TOKENS - 100 else ""
            logger.info(
                f"Sonnet usage: {in_t:,} input + {out_t:,} output tokens"
                f" (max_tokens={SONNET_MAX_TOKENS:,}){cap_hit}"
            )
    except Exception:
        pass  # usage logging is non-essential; don't let it fail the run

    raw = _strip_code_fence(response.content[0].text)
    # v3.38.4: same json_repair fallback pattern as Haiku triage. Sonnet
    # produces a deeper nested structure (22 indicator assessments + exec
    # summary + scenarios + news synthesis) so it has more surface area for
    # malformed-string failures. If json_repair returns an empty dict or
    # something unrecognizable, bail to fallback_analysis (which IS now
    # callable post v3.38.3 — the kwarg fix at fast_tier.py:399).
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            f"Sonnet JSON parse failed at {e.msg} (char {e.pos}); "
            f"attempting json_repair recovery..."
        )
        logger.debug(f"Raw response around fail point: ...{raw[max(0, e.pos-200):e.pos+200]}...")
        try:
            parsed = json_repair.loads(raw)
            if not isinstance(parsed, dict) or not parsed:
                logger.error(
                    f"json_repair returned {type(parsed).__name__} (empty or wrong type) — "
                    f"Sonnet analysis unrecoverable"
                )
                return None
            n_indicators = len(parsed.get("indicator_assessments", []))
            has_exec = bool(parsed.get("executive_summary"))
            logger.info(
                f"json_repair recovered Sonnet analysis: "
                f"{n_indicators}/22 indicators, exec_summary present={has_exec}"
            )
        except Exception as repair_err:
            logger.error(
                f"json_repair also failed: {repair_err}; "
                f"Sonnet analysis unrecoverable, will use fallback"
            )
            return None

    exec_data = parsed.get("executive_summary", {})
    # v3.39: build CaseScenario via the migration-aware helper (handles
    # legacy upside_to_peak_pct from older prior_state.json files), then
    # apply invariants so probabilities sum to 100 and base is the strict
    # maximum. See _enforce_scenario_invariants for the full rule set.
    base_raw = _build_case_scenario(exec_data.get("base_case"))
    bull_raw = _build_case_scenario(exec_data.get("bull_case"))
    bear_raw = _build_case_scenario(exec_data.get("bear_case"))
    base_fixed, bull_fixed, bear_fixed = _enforce_scenario_invariants(
        base_raw, bull_raw, bear_raw
    )
    # Log any non-trivial correction so it's visible during debugging.
    if (
        abs(base_fixed.probability_pct - base_raw.probability_pct) > 0.5
        or abs(bull_fixed.probability_pct - bull_raw.probability_pct) > 0.5
        or abs(bear_fixed.probability_pct - bear_raw.probability_pct) > 0.5
    ):
        logger.info(
            f"Scenario invariants applied: "
            f"base {base_raw.probability_pct:.0f}→{base_fixed.probability_pct:.0f}, "
            f"bull {bull_raw.probability_pct:.0f}→{bull_fixed.probability_pct:.0f}, "
            f"bear {bear_raw.probability_pct:.0f}→{bear_fixed.probability_pct:.0f}"
        )
    exec_summary = ExecutiveSummary(
        current_phase=exec_data.get("current_phase", 4),
        current_phase_label=exec_data.get("current_phase_label", ""),
        tier_status_summary=exec_data.get("tier_status_summary", ""),
        new_triggers_today=exec_data.get("new_triggers_today", []),
        base_case=base_fixed,
        bull_case=bull_fixed,
        bear_case=bear_fixed,
        delta_from_prior_text=exec_data.get("delta_from_prior_text", ""),
        # v3.39.7.5 — URGENT banner killed; fields kept on dataclass for back-
        # compat with prior_state.json but Sonnet no longer populates them
        # and template no longer renders them.
        urgent_attention_needed=False,
        urgent_attention_text="",
        # v3.39.7.5 — consolidated exec paragraph replacing the old 3-block
        # pattern (delta prose / big movers / today's market). Anchor links
        # of the form `[→§N]` are substituted into clickable HTML at render.
        executive_brief=exec_data.get("executive_brief", ""),
    )

    # v3.39.1: defensive construction of IndicatorAssessment objects from the
    # (possibly json_repair-recovered) parsed dict. The 20:30 May 17 run had
    # Sonnet truncate mid-14th-indicator, json_repair recovered 14 dicts but
    # the 14th was missing `latest_developments` — the prior code did
    # `IndicatorAssessment(**a)` which raised TypeError on the missing
    # required field, crashing the whole pipeline (no email sent, GitHub
    # issue auto-created). Helper fills every required field with a safe
    # default so partial dicts produce a usable (if incomplete) row instead
    # of crashing. Indicators without an `id` are dropped entirely.
    #
    # Separately: top_5_deep_dives no longer requested from Sonnet — it was
    # duplicating 5 indicators with richer content, costing ~10K output
    # tokens. Now synthesized client-side by plucking the TOP_5_INDICATORS
    # IDs from indicator_assessments. The deep-dive ranking is fixed in
    # config (TOP_5_INDICATORS), so client-side synthesis preserves intent.
    raw_assessments = parsed.get("indicator_assessments", [])
    assessments = [
        _safe_indicator_assessment(a)
        for a in raw_assessments
        if isinstance(a, dict) and a.get("id")
    ]
    # Client-side top_5 synthesis from the same list (no separate Sonnet call)
    top_5 = [a for a in assessments if a.id in TOP_5_INDICATORS]
    if len(assessments) < len(raw_assessments):
        logger.warning(
            f"Dropped {len(raw_assessments) - len(assessments)} indicator(s) "
            f"with missing or invalid id from Sonnet output."
        )

    # v3.39.7.4 — deterministic tier_status_summary override.
    # Sonnet's self-counted exec summary string has historically diverged
    # from the per-card statuses in the body of the same brief (off-by-one
    # arithmetic in the LLM's prose). Recompute from the parsed
    # indicator_assessments and override. Log when Sonnet's value differed
    # so we have signal on the underlying prompt quality.
    sonnet_summary = exec_summary.tier_status_summary
    deterministic_summary = _compute_tier_status_summary(assessments)
    if sonnet_summary != deterministic_summary:
        logger.warning(
            f"tier_status_summary override (Sonnet→deterministic): "
            f"{sonnet_summary!r} → {deterministic_summary!r}"
        )
    exec_summary.tier_status_summary = deterministic_summary

    return BriefAnalysis(
        executive_summary=exec_summary,
        big_movers=big_movers,
        indicator_assessments=assessments,
        top_5_deep_dives=top_5,
        market_summary=parsed.get("market_summary", ""),
        key_news_summary=parsed.get("key_news_summary", []),
        cycle_position_call=parsed.get("cycle_position_call", ""),
    )


# ============================================================
# Top-level pipeline
# ============================================================

def analyze_with_llm(
    api_key: str,
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    news_items: list[NewsItem],
    big_movers: list[BigMover],
    prior_state: dict,
    collector: Optional[DiagnosticsCollector] = None,
    digests: Optional[list[dict]] = None,
    signal_reasonings: Optional[list] = None,
    computed_metrics: Optional[dict] = None,
) -> Optional[BriefAnalysis]:
    """Two-stage LLM pipeline: Haiku triage -> Sonnet analysis.

    v3.39.5: New `digests` parameter (deep-tier Layer 1 broker notes +
    earnings transcripts).
    v3.39.6: New `signal_reasonings` (Layer 2 Haiku verdicts on each of the
    22 indicators) and `computed_metrics` (numerical verdicts: 5-day
    relative strength, hyperscaler capex ratios, forward P/E, DART
    materials). Threaded through so Sonnet quotes the exact numbers
    and defers to Layer 2 on per-signal status — fixing the May 18
    brief's t3_relative_strength prose that speculated "almost certainly
    past -10% threshold" from news headlines when the actual computed
    spread was below threshold.
    """
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set; falling back to deterministic synthesis")
        if collector is not None:
            collector.record_skipped(
                f"Anthropic API / {HAIKU_MODEL} (stage 1 triage)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
            collector.record_skipped(
                f"Anthropic API / {SONNET_MODEL} (stage 2 analysis)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
        return None

    logger.info(f"Stage 1: Haiku triaging {len(news_items)} news items...")
    triage = haiku_triage_news(api_key, news_items, collector=collector)
    if triage is None:
        logger.warning("Haiku triage failed; using empty triage")
        triage = NewsTriage(relevant_news=[], broker_actions=[], trendforce_commentary=[], indicator_relevance={})
    else:
        logger.info(
            f"Haiku triage: {len(triage.relevant_news)} relevant news | "
            f"{len(triage.broker_actions)} broker actions | "
            f"{len(triage.trendforce_commentary)} price commentary items"
        )

    n_digests = len(digests) if digests else 0
    n_reasonings = len(signal_reasonings) if signal_reasonings else 0
    has_cm = "yes" if computed_metrics else "no"
    logger.info(
        f"Stage 2: Sonnet analysis (digests={n_digests} | "
        f"layer2_verdicts={n_reasonings} | computed_metrics={has_cm})..."
    )
    analysis = sonnet_analyze(
        api_key, triage, performance_data, estimates_data, big_movers, prior_state,
        collector=collector,
        digests=digests,
        signal_reasonings=signal_reasonings,
        computed_metrics=computed_metrics,
        news_items=news_items,
    )
    return analysis


def fallback_analysis(
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    news_items: list[NewsItem],
    big_movers: list[BigMover],
) -> BriefAnalysis:
    """Deterministic fallback if the LLM is unavailable."""
    assessments = []
    for ind in INDICATORS:
        assessments.append(IndicatorAssessment(
            id=ind.id,
            tier=ind.tier,
            name=ind.name,
            description=ind.description,
            what_to_look_for=ind.what_to_look_for,
            current_status="Unknown (LLM unavailable)",
            where_we_stand="Automated classification unavailable; please review manually.",
            latest_developments="See news feed below.",
            relevant_links=[],
            change_vs_prior=None,
        ))

    top_5 = [a for a in assessments if a.id in TOP_5_INDICATORS]

    news_summary = []
    for n in news_items[:10]:
        news_summary.append({
            "headline": n.title,
            "url": n.url,
            "why_it_matters": "Auto-classification unavailable.",
        })

    exec_summary = ExecutiveSummary(
        current_phase=CYCLE_BASELINE["current_phase"],
        current_phase_label=CYCLE_BASELINE["current_phase_label"],
        tier_status_summary="LLM unavailable; manual review needed",
        new_triggers_today=[],
        base_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["base_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["base_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            return_to_peak_pct=CYCLE_BASELINE["base_case_return_to_peak_pct"],
        ),
        bull_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["bull_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["bull_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            return_to_peak_pct=CYCLE_BASELINE["bull_case_return_to_peak_pct"],
        ),
        bear_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["bear_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["bear_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            return_to_peak_pct=CYCLE_BASELINE["bear_case_return_to_peak_pct"],
        ),
        delta_from_prior_text="LLM unavailable - cannot compute deltas",
        urgent_attention_needed=False,
        urgent_attention_text="",
        # v3.39.7.5 — fallback exec brief when Sonnet is unavailable
        executive_brief=(
            "LLM analysis is unavailable for this brief; please review raw data manually. "
            f"Performance data captured for {len(performance_data)} tickers — see §1 for the price table. "
            "Indicator statuses default to 'Unknown' in §3; tier counts and trigger event detection "
            "require the Sonnet path. Today's news feed is summarized in §5."
        ),
    )

    return BriefAnalysis(
        executive_summary=exec_summary,
        big_movers=big_movers,
        indicator_assessments=assessments,
        top_5_deep_dives=top_5,
        market_summary=f"Performance data captured for {len(performance_data)} tickers. LLM analysis unavailable.",
        key_news_summary=news_summary,
        cycle_position_call="Cycle position synthesis unavailable in fallback mode.",
    )


def identify_big_movers(
    performance_data: list[PerformanceMetrics],
    threshold_pct: float = 4.0,
) -> list[BigMover]:
    """Identify tickers with absolute 1D move >= threshold."""
    movers = []
    for p in performance_data:
        if p.change_1d is None:
            continue
        if abs(p.change_1d) >= threshold_pct:
            movers.append(BigMover(
                name=p.name,
                ticker=p.ticker,
                change_1d_pct=p.change_1d,
                direction="up" if p.change_1d > 0 else "down",
            ))
    movers.sort(key=lambda m: abs(m.change_1d_pct), reverse=True)
    return movers


# ============================================================
# Helpers
# ============================================================

def _strip_code_fence(text: str) -> str:
    """Strip markdown code fences from an LLM response.

    Handles three patterns:
      1. Raw JSON (no fence)               → returned unchanged
      2. ```json\\n{...}\\n``` (closed)    → inner content
      3. ```json\\n{...  (unclosed)        → body after the opening marker

    v3.38.5: the previous implementation had a bug — on closed fences,
    `text.split("```", 2)[-1]` returned the empty string AFTER the closing
    fence (not the middle content), causing `json.loads` to fail at char 0
    with "Expecting value", which then fed an empty string to json_repair
    (which returned `""` rather than a dict). This was the actual root cause
    of the 16:25 UTC May 17 Haiku failure, not a prompt issue.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    # Strip opening fence + optional language tag, up to the first newline.
    first_nl = text.find("\n")
    if first_nl >= 0:
        text = text[first_nl + 1:]
    else:
        text = text[3:]
        if text.startswith("json"):
            text = text[4:]
    # Strip trailing fence if present (closed-fence case).
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3].rstrip()
    return text


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


# ============================================================================
# v3.39.7 — Cycle matrix Haiku extraction
# ============================================================================
# Two extraction tasks consumed by data/dram_contract_*.csv:
#   1. extract_dram_signal_from_article — tone/segment/supplier/qoq fields
#      from any DRAM news article → dram_contract_signals.csv
#   2. extract_dram_forecasts_from_press_release — quarterly forecasts
#      from TrendForce press releases → dram_contract_quarterly_forecasts.csv
#
# Both use HAIKU_MODEL (already imported above) and the existing Anthropic
# client pattern. Pre-filtered by title in fast_tier.py to limit cost.
# ============================================================================

import re as _re_v3397
from datetime import date as _date_v3397


SIGNAL_EXTRACTION_SYSTEM_PROMPT_V3397 = """You are a structured-data extractor for DRAM industry news. Your job is to read a news article and return a single JSON object describing what it says about DRAM contract pricing momentum.

Output strictly valid JSON. No preamble, no markdown fences. The object must have exactly these fields:

{
  "tone_direction": one of ["rising", "stable", "softening", "cooling", "declining"] or null,
  "tone_keywords": array of exact phrases from the article that informed the tone judgment (max 5),
  "segment_mentioned": one of ["server_ddr5", "pc_dram", "hbm", "mobile_dram", "graphics_dram", "nand_flash", "general"] or null,
  "qoq_pct_mentioned": number (explicit QoQ % cited) or null,
  "qoq_quarter_mentioned": string like "Q1_2026", "Q2_2026" or null,
  "supplier_mentioned": one of ["samsung", "sk_hynix", "micron", "kingston", "transcend", "trendforce", "multiple"] or "" if none,
  "is_quarterly_forecast": true if the article cites a forward-looking quarterly forecast with explicit % range, else false,
  "forecast_low_pct": number or null (only if is_quarterly_forecast),
  "forecast_high_pct": number or null (only if is_quarterly_forecast)
}

Rules:
- tone_direction reflects DRAM PRICING MOMENTUM specifically:
  * "rising" = prices increasing, supply tightening, demand strong, suppliers raising
  * "stable" = flat, sustained, balanced, holding
  * "softening" = momentum slowed, modest deceleration, mild pullback
  * "cooling" = cooling negotiations, ending seller's market, transactions slowing
  * "declining" = prices falling, demand destruction, price cuts failing
- tone_keywords are EXACT verbatim phrases from the article, not paraphrases.
- segment_mentioned reflects the PRIMARY segment. "general" if mixed.
- qoq_pct_mentioned: only include if a specific % cited. If a range is given, use midpoint AND populate forecast_low_pct/high_pct.
- is_quarterly_forecast: TRUE only if a forward-looking forecast is TIED TO A SPECIFIC QUARTER.
- supplier_mentioned: most prominently discussed supplier. "multiple" if equally featured. "" if none singled out.
- Return strict JSON parseable by json.loads(). No trailing commas, no markdown."""


FORECAST_EXTRACTION_SYSTEM_PROMPT_V3397 = """You are a structured-data extractor for TrendForce press releases announcing DRAM contract price forecasts.

A typical press release contains sentences like:
- "Conventional DRAM contract prices are expected to rise 58-63% QoQ in 2Q26"
- "Server DRAM prices are projected to surge by more than 60% QoQ"
- "1Q26 DRAM contract prices revised upward from 55-60% to 90-95%"

Extract every distinct quarterly forecast announcement into a JSON array. Output strictly valid JSON, no preamble or markdown:

{
  "forecasts": [
    {
      "quarter": "Q1_2026" | "Q2_2026" | "Q3_2026" | "Q4_2026",
      "segment": "conventional_dram_aggregate" | "server_ddr5" | "pc_dram" | "hbm" | "mobile_dram" | "graphics_dram" | "samsung_specific" | "sk_hynix_specific" | "micron_specific",
      "low_pct": number,
      "high_pct": number,
      "is_revision": true if explicitly described as a revision of a prior forecast, else false,
      "exact_quote": the exact sentence containing the forecast
    }
  ]
}

Rules:
- One forecast per (quarter, segment). Both initial and revised values for the same (quarter, segment) -> include both as separate entries with is_revision flagged appropriately.
- "conventional_dram_aggregate" applies to phrases like "conventional DRAM", general "DRAM contract prices" (not segment-specific). Use "server_ddr5" only when explicitly server/RDIMM/hyperscaler/AI-server.
- For ranges like "58-63%", low_pct=58, high_pct=63.
- For "more than 60%", treat as point estimate: low_pct=60, high_pct=60.
- If no forecasts present, return {"forecasts": []}.
- exact_quote should be the source sentence verbatim, useful for audit."""


_ALLOWED_TONE_V3397 = {"rising", "stable", "softening", "cooling", "declining"}
_ALLOWED_SEGMENTS_V3397 = {
    "server_ddr5", "pc_dram", "hbm", "mobile_dram", "graphics_dram",
    "nand_flash", "general",
}
_ALLOWED_SUPPLIERS_V3397 = {
    "samsung", "sk_hynix", "micron", "kingston", "transcend", "trendforce",
    "multiple", "",
}
_ALLOWED_FORECAST_SEGMENTS_V3397 = {
    "conventional_dram_aggregate", "server_ddr5", "pc_dram",
    "hbm", "mobile_dram", "graphics_dram",
    "samsung_specific", "sk_hynix_specific", "micron_specific",
}


def _call_haiku_json_v3397(client, system_prompt: str, user_msg: str,
                            max_tokens: int = 1024) -> Optional[dict]:
    """Internal: Haiku call returning a parsed JSON dict (None on failure)."""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=max_tokens,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        logger.error(f"Haiku call failed (v3.39.7 extraction): {e}")
        return None
    if not response.content:
        return None
    text = response.content[0].text.strip()
    text = _re_v3397.sub(r"^```(?:json)?\s*", "", text)
    text = _re_v3397.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Haiku v3.39.7 returned invalid JSON: {e}\nResponse: {text[:500]}")
        return None


def _safe_num_v3397(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def extract_dram_signal_from_article(
    client, title: str, article_body: str, url: str,
    published_date: Optional[_date_v3397] = None,
) -> Optional[dict]:
    """Extract a structured DRAM signal from an article via Haiku.

    Returns a dict matching the dram_contract_signals.csv schema, ready to
    pass to upsert_contract_signals() in news.py. None if extraction failed
    or article is too short.
    """
    if not article_body or len(article_body) < 100:
        logger.debug(f"Skipping article with insufficient body: {url}")
        return None

    body = article_body[:6000]
    user_msg = (
        f"Title: {title}\n"
        f"Published: {published_date.isoformat() if published_date else 'unknown'}\n"
        f"URL: {url}\n\n"
        f"Article body:\n{body}\n\n"
        f"Return the JSON object."
    )
    parsed = _call_haiku_json_v3397(client, SIGNAL_EXTRACTION_SYSTEM_PROMPT_V3397, user_msg)
    if parsed is None:
        return None

    tone = parsed.get("tone_direction")
    if isinstance(tone, str):
        tone = tone.lower()
        if tone not in _ALLOWED_TONE_V3397:
            tone = ""
    else:
        tone = ""

    kw = parsed.get("tone_keywords", [])
    keywords_str = ",".join(str(k).strip() for k in kw if k)[:500] if isinstance(kw, list) else ""

    segment = parsed.get("segment_mentioned")
    if isinstance(segment, str):
        segment = segment.lower()
        if segment not in _ALLOWED_SEGMENTS_V3397:
            segment = "general"
    else:
        segment = ""

    supplier = parsed.get("supplier_mentioned", "") or ""
    if isinstance(supplier, str):
        supplier = supplier.lower()
        if supplier not in _ALLOWED_SUPPLIERS_V3397:
            supplier = ""
    else:
        supplier = ""

    qoq_pct = _safe_num_v3397(parsed.get("qoq_pct_mentioned"))
    is_forecast = bool(parsed.get("is_quarterly_forecast", False))
    article_date = published_date or _date_v3397.today()

    url_l = url.lower()
    if "trendforce.com/presscenter" in url_l:
        source_name = "trendforce_press_release"
    elif "trendforce.com" in url_l:
        source_name = "trendforce_news"
    elif "digitimes.com" in url_l:
        source_name = "digitimes"
    elif "etnews.com" in url_l:
        source_name = "electronic_times"
    elif "chosun.com" in url_l:
        source_name = "chosunbiz"
    elif "businesskorea" in url_l:
        source_name = "businesskorea"
    elif "theelec" in url_l:
        source_name = "theelec"
    else:
        source_name = "unknown"

    return {
        "date": article_date.isoformat(),
        "source_name": source_name,
        "source_url": url,
        "headline": title,
        "tone_direction": tone,
        "tone_keywords": keywords_str,
        "segment_mentioned": segment,
        "qoq_pct_mentioned": qoq_pct,
        "supplier_mentioned": supplier,
        "raw_snippet": article_body[:500],
        "is_quarterly_forecast": is_forecast,
    }


def extract_dram_forecasts_from_press_release(
    client, title: str, article_body: str, url: str,
    published_date: Optional[_date_v3397] = None,
) -> list[dict]:
    """Extract structured quarterly forecasts from a TrendForce press release.

    Returns a list of dicts matching dram_contract_quarterly_forecasts.csv
    schema, ready to pass to upsert_contract_forecasts() in news.py. Empty
    list if nothing found.
    """
    if not article_body or len(article_body) < 100:
        return []
    body = article_body[:8000]
    user_msg = (
        f"Title: {title}\n"
        f"Published: {published_date.isoformat() if published_date else 'unknown'}\n"
        f"URL: {url}\n\n"
        f"Article body:\n{body}\n\n"
        f"Return the JSON object."
    )
    parsed = _call_haiku_json_v3397(
        client, FORECAST_EXTRACTION_SYSTEM_PROMPT_V3397, user_msg, max_tokens=1500,
    )
    if parsed is None:
        return []
    raw = parsed.get("forecasts", [])
    if not isinstance(raw, list):
        return []
    pub_date = (published_date or _date_v3397.today()).isoformat()

    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        quarter = entry.get("quarter", "")
        if not isinstance(quarter, str) or not _re_v3397.match(r"^Q[1-4]_\d{4}$", quarter):
            continue
        segment = entry.get("segment", "")
        if segment not in _ALLOWED_FORECAST_SEGMENTS_V3397:
            continue
        low = _safe_num_v3397(entry.get("low_pct"))
        high = _safe_num_v3397(entry.get("high_pct"))
        if low is None or high is None:
            continue
        is_rev = bool(entry.get("is_revision", False))
        notes = ("REVISION: " if is_rev else "") + str(entry.get("exact_quote", ""))[:500]
        out.append({
            "quarter": quarter, "segment": segment,
            "low_pct": low, "high_pct": high,
            "central_pct": (low + high) / 2.0,
            "source": "trendforce_press_release",
            "source_url": url,
            "published_date": pub_date,
            "supersedes_id": None,
            "notes": notes,
        })
    return out
