"""
Two-stage LLM analysis pipeline.

Stage 1 (Haiku): scrub all news, categorize relevance to indicators,
extract analyst broker actions (upgrade/downgrade/target change),
identify TrendForce price commentary, summarize key items.

Stage 2 (Sonnet): take Haiku's structured output + price/estimate data
+ prior assessments, produce tier-by-tier classification, executive
summary with probability calculations, and cycle position synthesis.

Falls back to deterministic synthesis if API unavailable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

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
    """Bull / base / bear case scenario."""
    probability_pct: float
    peak_months_away: float
    peak_window_start: str  # e.g., "August 2026"
    peak_window_end: str
    upside_to_peak_pct: float


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

OUTPUT FORMAT - respond with ONLY a JSON object, no prose:
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
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Haiku response as JSON: {e}")
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
    "base_case": {{"probability_pct": 30, "peak_months_away": 4, "peak_window_start": "August 2026", "peak_window_end": "October 2026", "upside_to_peak_pct": 18}},
    "bull_case": {{"probability_pct": 20, "peak_months_away": 12, "peak_window_start": "Q1 2027", "peak_window_end": "Q2 2027", "upside_to_peak_pct": 55}},
    "bear_case": {{"probability_pct": 25, "peak_months_away": 0, "peak_window_start": "May 2026", "peak_window_end": "May 2026", "upside_to_peak_pct": 0}},
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
  "top_5_deep_dives": [
    // Same structure but deeper - one for each: t1_spot_decline, t2_capex_parabolic, t2_eps_revisions, t3_supplier_inventory_rebuild, t3_relative_strength
  ],
  "market_summary": "2-3 sentence overview of today's memory/semi market action",
  "key_news_summary": [
    {{"headline": "...", "url": "...", "why_it_matters": "..."}}
  ],
  "cycle_position_call": "1 paragraph: where we are in cycle today, what changed since prior brief, what to watch next"
}}

PROBABILITY CALCULATION RULES:
- Base case: most likely peak window. Probability 25-35%.
- Bull case: cycle extends, requires Tier B1 signals. Probability 15-25%.
- Bear case: peak now or already in. Probability 20-30%.
- Total of base+bull+bear+neutral should sum to ~100%.
- If new Tier 2 trigger fired today: shift bear case probability +5-10%, base case forward in time.
- If new Tier 3 trigger: bear case probability dominant, peak likely already in.
- If bull confirms (hyperscaler capex up): shift bull case +5-10%.

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

    response = None
    try:
        if collector is not None:
            with collector.track(
                f"Anthropic API / {SONNET_MODEL} (stage 2 analysis)",
                "llm",
            ) as t:
                response = client.messages.create(
                    model=SONNET_MODEL,
                    max_tokens=SONNET_MAX_TOKENS,
                    temperature=LLM_TEMPERATURE,
                    messages=[{"role": "user", "content": prompt}],
                )
                t.items_returned = 1  # one analysis produced
                t.detail = f"prompt: {len(prompt)} chars"
        else:
            response = client.messages.create(
                model=SONNET_MODEL,
                max_tokens=SONNET_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
    except Exception as e:
        logger.error(f"Sonnet analysis call failed: {e}")
        return None

    raw = _strip_code_fence(response.content[0].text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Sonnet response as JSON: {e}")
        logger.debug(f"Raw response (first 1500 chars): {raw[:1500]}")
        return None

    exec_data = parsed.get("executive_summary", {})
    exec_summary = ExecutiveSummary(
        current_phase=exec_data.get("current_phase", 4),
        current_phase_label=exec_data.get("current_phase_label", ""),
        tier_status_summary=exec_data.get("tier_status_summary", ""),
        new_triggers_today=exec_data.get("new_triggers_today", []),
        base_case=CaseScenario(**exec_data.get("base_case", {})),
        bull_case=CaseScenario(**exec_data.get("bull_case", {})),
        bear_case=CaseScenario(**exec_data.get("bear_case", {})),
        delta_from_prior_text=exec_data.get("delta_from_prior_text", ""),
        urgent_attention_needed=exec_data.get("urgent_attention_needed", False),
        urgent_attention_text=exec_data.get("urgent_attention_text", ""),
    )

    return BriefAnalysis(
        executive_summary=exec_summary,
        big_movers=big_movers,
        indicator_assessments=[IndicatorAssessment(**a) for a in parsed.get("indicator_assessments", [])],
        top_5_deep_dives=[IndicatorAssessment(**a) for a in parsed.get("top_5_deep_dives", [])],
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
            upside_to_peak_pct=CYCLE_BASELINE["base_case_upside_pct"],
        ),
        bull_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["bull_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["bull_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            upside_to_peak_pct=CYCLE_BASELINE["bull_case_upside_pct"],
        ),
        bear_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["bear_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["bear_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            upside_to_peak_pct=CYCLE_BASELINE["bear_case_upside_pct"],
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
    """Strip markdown code fences from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[-1] if text.count("```") >= 2 else text[3:]
        if text.startswith("json"):
            text = text[4:]
        elif text.startswith("\n"):
            text = text[1:]
        text = text.rsplit("```", 1)[0].strip()
    return text


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"
