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
  v3.38.5 (2026-05-17) — Real root cause of the 16:25 UTC May 17 Haiku triage failure found and fixed. The error pattern was "Failed to parse Haiku response as JSON: Expecting value (char 0)" → json_repair fallback returned str (not dict) → unrecoverable. This was NOT a prompt issue or a max_tokens issue. The bug was in `_strip_code_fence`: when the LLM returned a properly closed code fence (```json\n{...}\n```), the line `text.split("```", 2)[-1]` grabbed the empty string AFTER the closing fence instead of the JSON content BETWEEN the fences. The function returned `""`. json.loads("") fails with "Expecting value (char 0)"; json_repair.loads("") returns `""` (a str). Rewrote the function with explicit handling of three patterns (raw / closed-fence / unclosed-fence). Verified locally on all five common LLM-output shapes — all parse cleanly via stdlib now. Also tightened `build_haiku_triage_prompt` output format requirements (start with `{`, no markdown fences, no prose, no apologies) as belt-and-suspenders. The previous bump of HAIKU_MAX_TOKENS to 16000 and the json_repair fallback both remain — they fix related but distinct failure modes (truncation, unescaped chars).
  v3.38.4 (2026-05-17) — Both `haiku_triage_news` and `sonnet_analyze` now parse responses with a two-stage strategy: stdlib `json.loads(raw)` first (fast path for valid output), then `json_repair.loads(raw)` fallback on JSONDecodeError. Reason: the 15:32 UTC May 17 run had Haiku triage of 50 news items fail JSON parse at char 13926 (unterminated string — almost certainly unescaped quote/newline in a reasoning field), losing the entire triage. v3.38.3 had the same failure mode in Sonnet (char 27099) and Haiku (char 6839). Bumping max_tokens helps when the cause is truncation, but doesn't help when the cause is malformed string content — json_repair handles both. New explicit log at WARNING level when the fallback fires, with INFO-level confirmation of how much was recovered.
"""

from __future__ import annotations

import json
import logging
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
    current_status: str       # "Fired" | "Partially fired" | "Not fired" | "Reversed"
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
    """The structured exec summary at the top of the email."""
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


# ============================================================
# v3.39 — Scenario invariant enforcement
# ============================================================
# The Sonnet prompt asks for three scenarios (base / bull / bear) with
# probability_pct summing to 100 and base being the most probable. The LLM
# usually obeys but can drift on either dimension — e.g., the 17:43 May 17
# brief produced base 28% / bear 30% / bull 18% (sum 76%, bear > base). The
# enforcer below normalizes deterministically: probabilities sum to exactly
# 100, base is strictly the highest, return_to_peak_pct signs make sense.


def _safe_indicator_assessment(a: dict) -> IndicatorAssessment:
    """Construct an IndicatorAssessment from a possibly-partial dict.

    Used after Sonnet output is parsed (or json_repair-recovered). When
    truncation hits mid-indicator, json_repair returns whatever it could
    salvage, often missing required fields like `latest_developments`.
    Prior code's `IndicatorAssessment(**a)` raised TypeError on missing
    fields and crashed the whole pipeline — see v3.39.1 fix notes.

    Every required field gets a safe default; `change_vs_prior` stays None
    if absent. The caller filters out dicts without an `id` upstream.
    """
    return IndicatorAssessment(
        id=str(a.get("id", "unknown")),
        tier=int(a.get("tier", 0)) if str(a.get("tier", "")).strip().isdigit() else 0,
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
) -> str:
    """Construct the Sonnet analysis prompt using Haiku-triaged data."""

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
    for item in triage.relevant_news[:50]:
        triage_text += f"- [{item.get('source', '?')}] {item.get('title', '')}\n"
        triage_text += f"  Takeaway: {item.get('summary', '')}\n"
        triage_text += f"  URL: {item.get('url', '')}\n"

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
1. Build the EXECUTIVE SUMMARY in the structured format below.
2. Assess EVERY indicator (status, where we stand, latest developments with links).
3. Provide DEEP DIVES for the top 5 indicators.
4. Compose the cycle position synthesis paragraph.
5. Identify NEW TRIGGERS that fired today (compare against prior state).
6. Calculate UPDATED PROBABILITY DISTRIBUTION based on new evidence.
7. Compare current peak estimate vs prior to flag if peak has shortened or extended.
8. If a new tier signal fired today, set urgent_attention_needed=true and explain.

OUTPUT FORMAT - respond with ONLY a JSON object:

{{
  "executive_summary": {{
    "current_phase": 4,
    "current_phase_label": "Boom / Euphoria (late stage)",
    "tier_status_summary": "T1: X/6 fired | T2: Y/7 fired | T3: Z/5 fired | T4: W/4 fired",
    "new_triggers_today": [
      {{"tier": 2, "indicator_id": "...", "indicator_name": "...", "description": "..."}}
    ],
    "base_case": {{"probability_pct": 50, "peak_months_away": 4, "peak_window_start": "August 2026", "peak_window_end": "October 2026", "return_to_peak_pct": 18}},
    "bull_case": {{"probability_pct": 22, "peak_months_away": 12, "peak_window_start": "Q1 2027", "peak_window_end": "Q2 2027", "return_to_peak_pct": 55}},
    "bear_case": {{"probability_pct": 28, "peak_months_away": 0, "peak_window_start": "May 2026", "peak_window_end": "May 2026", "return_to_peak_pct": -8}},
    "delta_from_prior_text": "Base case peak unchanged | OR shortened by X months due to ... | OR no prior data",
    "urgent_attention_needed": false,
    "urgent_attention_text": "Empty if not urgent. Otherwise: clear description of what to address."
  }},
  "indicator_assessments": [
    {{
      "id": "t1_spot_decline",
      "tier": 1,
      "name": "...",
      "description": "...",
      "what_to_look_for": "...",
      "current_status": "Fired | Partially fired | Not fired | Reversed",
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
- New Tier 2 trigger fired today: bear probability +3-6pp, base shortens
  peak_months_away by ~1mo and trims return_to_peak_pct by ~3-5pp. Bull -1-3pp.
- New Tier 3 trigger fired today: bear probability +5-8pp (still under base),
  base peak_months_away drops to ≤2mo, base return_to_peak_pct trims ~5-10pp.
  Bull case shrinks meaningfully (-3-5pp).
- New Tier 4 trigger: same magnitude as Tier 3 but with bear return_to_peak_pct
  going negative (e.g., -5 to -15pp) — confirms peak is in.
- Bull confirms (e.g., hyperscaler 2027 capex >2026): bull probability +3-5pp,
  bear -2-4pp, base nudges peak_months_away by ~+1mo.

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

Cycle baseline (use as your prior, adjust per today's evidence):
{json.dumps(CYCLE_BASELINE, indent=2)}

Top 5 indicators for deep dives: {", ".join(TOP_5_INDICATORS)}

NOW THE DATA:

{perf_text}
{movers_text}
{est_text}
{indicator_text}
{prior_text}
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
) -> Optional[BriefAnalysis]:
    """Run Sonnet to produce the full analysis with exec summary."""
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
        triage, performance_data, estimates_data, big_movers, prior_state
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
        urgent_attention_needed=exec_data.get("urgent_attention_needed", False),
        urgent_attention_text=exec_data.get("urgent_attention_text", ""),
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
) -> Optional[BriefAnalysis]:
    """Two-stage LLM pipeline: Haiku triage -> Sonnet analysis."""
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

    logger.info("Stage 2: Sonnet analysis...")
    analysis = sonnet_analyze(
        api_key, triage, performance_data, estimates_data, big_movers, prior_state,
        collector=collector,
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
