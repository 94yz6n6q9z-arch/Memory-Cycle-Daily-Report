"""
Layer 2 — Per-signal evidence gatherer.

Runs in the FAST TIER (11:00 UTC weekdays). For each of the 22 universal
indicators + Indicator 6 sub-threats + Indicator 7, gathers ALL evidence
from:
  - Layer 1 digests (Quartr transcripts + Korean broker PDFs, from the deep tier)
  - Fast-tier news items (RSS + Google News + EDGAR + Naver + Finnhub)
  - Computed numeric metrics (prices, P/E, hyperscaler capex/revenue)

Produces per-signal JSON:

    {
      "signal_id": "t1_lead_times",
      "evidence_count": 4,
      "evidence_sources": ["Mirae 2026-05-09", "Quartr SKHX Q1", "ChosunBiz 2026-05-10", "DigiTimes headline"],
      "consensus_direction": "no change — lead times still elevated 26-39 wks",
      "convergence": "high",       // high | medium | low | conflicting
      "fired_status": "Not fired",
      "fired_status_evidence": ["..."],
      "evidence_for_change_vs_yesterday": "none" | "<text>",
      "key_quotes_consolidated": ["..."],  // top 1-3 across all sources
    }

This is the "let me make sense of all this for THIS signal" step.
Critically, each signal's evidence is gathered BEFORE judgment — so
convergence (multiple sources agreeing) becomes an explicit field.
Sonnet at Layer 3 reasons over these compact outputs rather than raw
transcripts.

22 indicators × ~1 Haiku call each = ~22 calls. Each ~2-5K input tokens,
~500 output. Cost: ~$0.10-0.30 per fast-tier run. Parallelizable via
ThreadPoolExecutor — completes in 30-60 sec.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import time  # v3.38.1 — for Layer 2 inter-call throttling
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from anthropic import Anthropic

from src.config import INDICATORS, COMPANY_SPECIFIC_INDICATORS, HAIKU_MODEL, LLM_TEMPERATURE, Indicator

logger = logging.getLogger("memory_cycle_brief.signal_reasoner")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ============================================================
# Schemas
# ============================================================

@dataclass
class SignalReasoning:
    """Output of Layer 2 for a single indicator."""
    signal_id: str
    signal_name: str
    tier: int
    evidence_count: int
    evidence_sources: list[str]
    consensus_direction: str
    convergence: str          # "high" | "medium" | "low" | "conflicting"
    fired_status: str         # "Fired" | "Not fired" | "Partially fired" | "Reversed"
    fired_status_evidence: list[str]
    evidence_for_change_vs_yesterday: str
    key_quotes_consolidated: list[str]
    confidence: str           # "high" | "medium" | "low"
    raw_evidence_dump: dict   # for debugging — what the reasoner saw

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "signal_name": self.signal_name,
            "tier": self.tier,
            "evidence_count": self.evidence_count,
            "evidence_sources": self.evidence_sources,
            "consensus_direction": self.consensus_direction,
            "convergence": self.convergence,
            "fired_status": self.fired_status,
            "fired_status_evidence": self.fired_status_evidence,
            "evidence_for_change_vs_yesterday": self.evidence_for_change_vs_yesterday,
            "key_quotes_consolidated": self.key_quotes_consolidated,
            "confidence": self.confidence,
        }


# ============================================================
# Evidence gathering
# ============================================================

def _gather_evidence_for_signal(
    indicator: Indicator,
    digests: list[dict],
    fast_tier_news: list[dict],
    naver_data: Optional[dict],
    finnhub_data: Optional[dict],
    edgar_items: list[dict],
    computed_metrics: dict,
    prior_status: Optional[str] = None,
) -> dict:
    """Pre-filter evidence relevant to one indicator.

    Returns a dict the Haiku reasoner will see — already filtered so we
    don't waste tokens making it read irrelevant material.
    """
    relevant_digests = [
        d for d in digests
        if indicator.id in d.get("signal_relevance", [])
    ]
    relevant_news = [
        n for n in fast_tier_news
        if any(phrase.lower() in (n.get("title", "") + " " + n.get("summary", "")).lower()
               for phrase in indicator.trigger_patterns[:10])
    ][:20]  # cap per signal to control tokens

    # Per-signal-type special handling
    signal_specific: dict[str, Any] = {}

    if indicator.id == "t2_eps_revisions":
        # Use the weighted scoring engine — Naver + Finnhub + Korean broker PDF digests
        # are ingested, scored per-broker with Tier A/B/C weighting + magnitude + combo bonus,
        # and the verdict already includes per-ticker 7d/30d rolling sums.
        # The reasoner sees the scored output, not the raw broker rows — Sonnet at Layer 3
        # consumes the structured verdict.
        try:
            from src.analysis.signal_scoring import ingest_and_evaluate
            korean_pdf_digests = [
                d for d in digests
                if d.get("source_type") == "korean_broker_pdf"
            ]
            scoring_verdict = ingest_and_evaluate(
                naver_payload=naver_data,
                finnhub_payload=finnhub_data,
                broker_pdf_digests=korean_pdf_digests,
            )
            signal_specific["weighted_scoring_verdict"] = scoring_verdict
            # Also pass condensed top-impact actions for Layer 2 to reference
            signal_specific["top_impact_actions_30d"] = {
                ticker: td["rolling_30d"]["top_impact_actions"]
                for ticker, td in scoring_verdict.get("per_ticker", {}).items()
            }
        except Exception as e:
            logger.warning(f"Weighted scoring engine failed for t2_eps_revisions: {e}")
            # Fall back to raw passthrough
            if naver_data:
                signal_specific["naver_korean_broker_actions"] = naver_data.get("tickers", {})
            if finnhub_data:
                signal_specific["finnhub_us_analyst_actions"] = {
                    t: {
                        "direction": d.get("recent_direction"),
                        "price_target": d.get("price_target"),
                    }
                    for t, d in finnhub_data.get("tickers", {}).items()
                }

    if indicator.id == "t2_insider_sales":
        signal_specific["edgar_form4_items"] = [
            e for e in edgar_items
            if "Form 4" in e.get("title", "") or "form 4" in e.get("source", "").lower()
        ]
        # DART filings for SK Hynix + Samsung — pulled from computed_metrics if present
        signal_specific["dart_material_disclosures"] = computed_metrics.get("dart_material_disclosures", {})

    if indicator.id == "t2_pe_compressed":
        signal_specific["computed_forward_pe"] = computed_metrics.get("forward_pe", {})

    if indicator.id == "t2_hyperscaler_capex_revenue":
        signal_specific["computed_capex_revenue"] = computed_metrics.get("capex_revenue_ratios", {})

    if indicator.id == "t3_relative_strength":
        # Full verdict from prices.evaluate_relative_strength_signal — all 3 sub-triggers
        signal_specific["computed_relative_strength"] = computed_metrics.get("relative_strength_full", {})

    if indicator.id.startswith("i7"):  # ADR/SPO indicators
        signal_specific["edgar_skhx_filings"] = [
            e for e in edgar_items
            if "SK Hynix" in e.get("source", "") or "sk_hynix" in e.get("source", "").lower()
        ]

    return {
        "indicator_id": indicator.id,
        "indicator_name": indicator.name,
        "what_to_look_for": indicator.what_to_look_for,
        "trigger_patterns": indicator.trigger_patterns,
        "trigger_numeric": indicator.trigger_numeric,
        "cadence": indicator.cadence,
        "stitching": indicator.stitching,
        "relevant_digests": relevant_digests,
        "relevant_news": relevant_news,
        "signal_specific_data": signal_specific,
        "prior_status": prior_status,
    }


# ============================================================
# Layer 2 prompts
# ============================================================

REASONER_SYSTEM_PROMPT = """You are reasoning over ALL evidence gathered for ONE memory-cycle indicator.

Your job: produce a structured JSON judgment about whether the signal is fired/not fired today, with explicit citation of evidence sources, an assessment of how many independent channels corroborate, and a comparison to yesterday's status.

CRITICAL:
- Convergence: "high" = 3+ independent sources agree; "medium" = 2 sources agree; "low" = only 1 source; "conflicting" = sources disagree.
- For STITCHED signals (e.g. t1_oem_inventory needs both supplier AND buyer side), fired requires BOTH sides confirmed simultaneously. Single side alone = "Not fired" even if dramatic.
- For PATTERN+CADENCE signals, verify the cadence requirement (e.g. 2 articles within 14 days). Single article = "Partially fired" unless the cadence allows it.
- Be conservative: when in doubt between "Fired" and "Partially fired", choose "Partially fired".
- Quote no more than 3 verbatim excerpts, each under 15 words. These will be shown to the user.
- Output VALID JSON only. No prose outside the JSON object."""


REASONER_USER_TEMPLATE = """SIGNAL TO ASSESS:

Signal ID:       {indicator_id}
Signal name:     {indicator_name}
Tier:            {tier}
What to look for: {what_to_look_for}

Trigger patterns: {trigger_patterns_json}
Trigger numeric:  {trigger_numeric_json}
Cadence:          {cadence}
Stitching:        {stitching}

PRIOR STATUS (yesterday): {prior_status}

EVIDENCE (pre-filtered to items mentioning this signal's trigger patterns):

Relevant digests from deep-tier (Quartr transcripts, Korean broker PDFs):
{digests_json}

Relevant fast-tier news items:
{news_json}

Signal-specific structured data (numeric metrics, broker actions, filings):
{signal_specific_json}

Respond with a JSON object matching this schema:
{{
  "evidence_count": <int>,
  "evidence_sources": ["<source 1>", "<source 2>", ...],
  "consensus_direction": "<brief plain-English summary of what the evidence says>",
  "convergence": "high" | "medium" | "low" | "conflicting",
  "fired_status": "Fired" | "Not fired" | "Partially fired" | "Reversed",
  "fired_status_evidence": ["<concrete reason 1>", "<concrete reason 2>"],
  "evidence_for_change_vs_yesterday": "<text or 'none'>",
  "key_quotes_consolidated": ["<quote under 15 words>", ...],
  "confidence": "high" | "medium" | "low"
}}

Only output the JSON object."""


def reason_one_signal(
    indicator: Indicator,
    digests: list[dict],
    fast_tier_news: list[dict],
    naver_data: Optional[dict],
    finnhub_data: Optional[dict],
    edgar_items: list[dict],
    computed_metrics: dict,
    prior_status: Optional[str] = None,
) -> Optional[SignalReasoning]:
    """Layer 2 for one indicator."""
    if client is None:
        return None

    ev = _gather_evidence_for_signal(
        indicator, digests, fast_tier_news, naver_data, finnhub_data,
        edgar_items, computed_metrics, prior_status,
    )

    user_msg = REASONER_USER_TEMPLATE.format(
        indicator_id=indicator.id,
        indicator_name=indicator.name,
        tier=indicator.tier,
        what_to_look_for=indicator.what_to_look_for,
        trigger_patterns_json=json.dumps(indicator.trigger_patterns[:15], default=str),
        trigger_numeric_json=json.dumps(indicator.trigger_numeric, default=str),
        cadence=indicator.cadence or "—",
        stitching=indicator.stitching or "—",
        prior_status=prior_status or "unknown",
        digests_json=json.dumps([
            {
                "source_id": d.get("source_id"),
                "published_date": d.get("published_date_iso"),
                "key_metrics": d.get("key_metrics"),
                "key_quotes": d.get("key_quotes"),
                "fired_status_evidence": d.get("fired_status_evidence"),
            }
            for d in ev["relevant_digests"][:10]
        ], default=str, indent=2),
        news_json=json.dumps([
            {"title": n.get("title"), "url": n.get("url"), "source": n.get("source")}
            for n in ev["relevant_news"]
        ], default=str, indent=2),
        signal_specific_json=json.dumps(ev["signal_specific_data"], default=str, indent=2)[:8000],
    )

    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1500,
            temperature=LLM_TEMPERATURE,
            system=REASONER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Layer 2 JSON parse failed for {indicator.id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Layer 2 LLM call failed for {indicator.id}: {e}")
        return None

    return SignalReasoning(
        signal_id=indicator.id,
        signal_name=indicator.name,
        tier=indicator.tier,
        evidence_count=parsed.get("evidence_count", 0),
        evidence_sources=parsed.get("evidence_sources", []),
        consensus_direction=parsed.get("consensus_direction", ""),
        convergence=parsed.get("convergence", "low"),
        fired_status=parsed.get("fired_status", "Not fired"),
        fired_status_evidence=parsed.get("fired_status_evidence", []),
        evidence_for_change_vs_yesterday=parsed.get("evidence_for_change_vs_yesterday", "none"),
        key_quotes_consolidated=parsed.get("key_quotes_consolidated", []),
        confidence=parsed.get("confidence", "low"),
        raw_evidence_dump=ev,
    )


def reason_all_signals(
    digests: list[dict],
    fast_tier_news: list[dict],
    naver_data: Optional[dict],
    finnhub_data: Optional[dict],
    edgar_items: list[dict],
    computed_metrics: dict,
    prior_status_map: Optional[dict] = None,
    max_workers: int = 6,  # v3.38.1: kept for backward-compat, no longer used (was ThreadPoolExecutor count)
    inter_call_delay_seconds: float = 7.0,
) -> list[SignalReasoning]:
    """Run Layer 2 across every indicator — sequentially, with throttling.

    v3.38.1: changed from concurrent ThreadPoolExecutor(max_workers=6) to
    sequential with a per-call delay. The concurrent path burst input tokens
    at ~120k/min (vs Haiku 4.5's 50k tokens/min org limit), saturated the
    bucket in ~8s, then got 429s for the back half — losing 3/22 signals
    permanently on the May 17 13:42 UTC run (t3_wfe_orders_crack,
    t4_guidance_cuts, t3_eps_miss_guidance).

    Math:
      • per-call input tokens ≈ 5,000 (system + indicator + digests + news + signal_specific)
      • 50,000 tokens/min ÷ 5,000 tokens/call = 10 calls/min ceiling
      • 60s/min ÷ 10 calls/min = 6.0s minimum spacing
      • Using 7.0s spacing for safety margin against per-call token variance

    Wall-time impact: 22 signals × ~2s latency + 21 gaps × 7s = ~3.2 min for
    Layer 2 (was ~37s concurrent). Total fast-tier runtime moves from ~2min
    to ~5min. Per Raul: 'makes it a bit slower which is not an issue but we
    get the data'. Reliability > speed.

    `max_workers` kwarg retained for backward-compat with any callers passing
    it — it's now ignored. `inter_call_delay_seconds` is the new lever.
    """
    prior_status_map = prior_status_map or {}
    results: list[SignalReasoning] = []
    n = len(INDICATORS)
    logger.info(
        f"Layer 2: running {n} signals sequentially with {inter_call_delay_seconds}s "
        f"inter-call delay (est. {((n-1) * inter_call_delay_seconds + n * 2):.0f}s total)"
    )

    for i, ind in enumerate(INDICATORS):
        # Throttle before every call except the first one
        if i > 0 and inter_call_delay_seconds > 0:
            time.sleep(inter_call_delay_seconds)
        try:
            result = reason_one_signal(
                ind, digests, fast_tier_news, naver_data, finnhub_data,
                edgar_items, computed_metrics, prior_status_map.get(ind.id),
            )
            if result is not None:
                results.append(result)
            else:
                logger.warning(f"Layer 2 returned None for {ind.id}")
        except Exception as e:
            logger.error(f"Layer 2 raised for {ind.id}: {e}")
    # Sort by tier then signal id for stable output
    results.sort(key=lambda r: (r.tier, r.signal_id))
    return results
