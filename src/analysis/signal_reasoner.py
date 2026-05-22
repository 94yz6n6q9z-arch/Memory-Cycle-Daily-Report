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
~500 output. Cost: ~$0.10-0.30 per fast-tier run. v3.38.1 changed concurrent
ThreadPoolExecutor to sequential with throttle; runtime is now ~3 min not
~30s. Worth it for not losing signals to 429s.

CHANGELOG (this file):
  v3.39.7.3 (2026-05-20) — EVIDENCE STORE INTEGRATION. The PRIOR-STATUS PRESERVATION RULE added earlier in this session is upgraded from a string-based prior_status check to a full evidence-store-aware reasoning loop. Layer 2 now receives the full evidence_log + reversal_conditions for each indicator (from src/state.py:load_indicator_evidence_store), formatted into the user prompt via IndicatorEvidence.format_for_layer2_prompt(). REASONER_SYSTEM_PROMPT rewritten with DECISION RULES BY PERSISTENCE CATEGORY (live/permanent/quarterly/durable_contract/event_window) — each category has explicit instructions for when to preserve, escalate, or reverse status. INVALID DOWNGRADES section explicitly lists the v3.39.7 bugs (capex_parabolic, oem_inventory, demand_destruction, forward_pe) so Haiku doesn't repeat them. reason_one_signal gains optional indicator_evidence: IndicatorEvidence parameter; reason_all_signals gains optional indicator_evidence_store: dict parameter. Both default to None for backward-compat — when None, falls back to the v3.39.7 prior_status-only behavior. Diagnosed from the May 20 brief where 8 indicators inappropriately downgraded due to Layer 2 having no carry-forward.
  v3.38.1 (2026-05-17) — Replaced ThreadPoolExecutor(max_workers=6) concurrent fan-out with sequential loop + `time.sleep(inter_call_delay_seconds)` between calls. Default delay = 7.0s. Reason: the May 17 13:42 UTC run burst input tokens at ~120k/min vs Haiku 4.5's 50k/min org rate limit, saturating the bucket in ~8s and losing 3/22 signals to 429 after SDK retries gave up. Math: 5k tokens/call × 10 calls/min ceiling = 6s minimum spacing; 7s for safety margin. Total wall-time moves from ~37s to ~191s. `max_workers` kwarg kept for backward-compat but ignored. Added `import time` at module top.
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
    fired_status: str         # "Fired" | "Not fired" | "Partially fired"  (v3.39.7.9: "Reversed" removed — collapsed to "Not fired"; audit trail in evidence_for_change_vs_yesterday)
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
        # v3.39.7.9: capex_revenue_ratios REMOVED from signal_specific. The
        # indicator changed from capex/revenue to capex/OCF in v3.39.7.4 but
        # we kept passing capex_revenue_ratios "for context" — and Layer 2
        # latched onto them to reverse the verdict against the actual rule.
        # Diagnosed May 21 2026 when Layer 2 returned Reversed citing
        # capex/revenue 24-37% < 40% even though COMPUTED METRICS showed
        # capex/OCF mean 93.2% (well above 50% threshold). capex/revenue is
        # no longer authoritative anywhere in the system — don't expose it.
        #
        # v3.39.7.9 also: mean_capex_ocf_ratio_latest_q renamed to
        # mean_capex_ocf_ratio_2q to reflect new 2-quarter semantics per
        # user direction (single-quarter OCF dip noise — e.g. Amazon Q1
        # 2026 169.8% vs Q4 2025 ~50% — was triggering false single-quarter
        # spikes).
        signal_specific["mean_capex_ocf_ratio_2q"] = computed_metrics.get("mean_capex_ocf_ratio_2q")
        signal_specific["n_companies_with_2q_capex_ocf"] = computed_metrics.get(
            "n_companies_with_2q_capex_ocf", 0
        )
        signal_specific["per_company_capex_ocf_2q"] = computed_metrics.get(
            "per_company_capex_ocf_2q", {}
        )

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

EVIDENCE-STORE-AWARE REASONING (v3.39.7.3) — THE FUNDAMENTAL RULE:

You are given an INDICATOR EVIDENCE STORE block above. It contains the cumulative record of WHY this indicator is in its current state — every prior evidence item that fired it, with its persistence category and reversal_condition. This is the framework's institutional memory. You MUST use it.

Memory-cycle indicators are CUMULATIVE STATE, not daily news triggers. They fire on data that has happened (capex announcements, signed contracts, computed P/E values, supplier inventory prints), and they stay fired until the underlying condition is reversed. The evidence store records this. Your job is to decide if today's evidence:
  (a) PRESERVES the current status (the recorded conditions still hold), OR
  (b) REVERSES it (today's data matches the recorded reversal_condition), OR
  (c) ESCALATES it (today's data fires a previously Not-fired signal)

DECISION RULES BY PERSISTENCE CATEGORY:

[A] LIVE persistence (forward P/E, relative strength, cycle matrix verdict, spot/contract spread):
The signal is recomputed every run from current data. ALWAYS use today's computed value as the verdict. Don't preserve a prior fired status if today's computation says Not fired, and don't preserve Not fired if today's computation fires. The evidence store entry for live signals is informational — the latest computation is authoritative.
Example: if t2_pe_compressed evidence store shows it fired on SK Hynix 4.56x < 5x, and today's computation shows SK Hynix at 4.5x (still below 5x), STATUS = Fired (computed value still crosses threshold). If today's computation shows SK Hynix at 5.2x (above threshold), STATUS = Not fired.

[B] PERMANENT persistence (historical announcements, transactions):
Once a permanent event is recorded, it cannot be unfired by silence in today's news. Status preserves. If aging applies (e.g., insider sales 12-month window per the evidence's reversal_condition), only age out per the explicit rule.
Example: t1_efficiency_shock has TurboQuant announced March 24, 2026. This is permanent. Status stays Fired regardless of today's news.

[C] QUARTERLY persistence (forecasts, capex announcements, inventory prints):
Status preserves until today's evidence EXPLICITLY MATCHES the reversal_condition recorded in the store. Silence does NOT count as reversal — only explicit contradicting evidence does.
Examples:
  - t2_capex_parabolic reversal_condition: "Hyperscaler quarterly print shows capex CUT vs guidance". Today's news mentions no capex changes → STATUS PRESERVES (Fired).
  - t1_demand_destruction reversal_condition: "IDC publishes upgraded PC/smartphone forecast". Today's news has no IDC update → STATUS PRESERVES (Fired). If today's news has IDC announcing UPGRADED forecast → STATUS = Not fired.

[D] DURABLE_CONTRACT persistence (multi-year LTAs, sold-out commitments):
Status preserves until explicit news of contract cancellation or buyer pull-out. These are durable agreements that don't expire on news cycles.
Example: t2_ltas_signed fired on HBM3E sold-out + SK Hynix HBM4 LTAs with Nvidia. Status stays Fired unless news reports an LTA cancellation.

[E] EVENT_WINDOW persistence (5d relative strength, single-day moves):
Status decays after the window. If the window has expired without re-confirmation, the entry is dormant. Use today's computed metric (typically a live entry will accompany).

INVALID DOWNGRADES (these are bugs the v3.39.7.3 fix eliminates):
- "No new capex announcements in today's news feed" does NOT unfire t2_capex_parabolic. The $725B 2026 hyperscaler capex consensus and SK Hynix M15X are recorded. Silence is not reversal.
- "Today's Layer 2 found zero supplier-side or buyer-side inventory data" does NOT unfire t1_oem_inventory. The September 2025 TrendForce print (supplier 2-3w, buyer 10w) stands until a new print reverses it.
- "No IDC/Gartner/Counterpoint reports in today's feed" does NOT unfire t1_demand_destruction. The April 2026 IDC PC -2.4% to -11.3% forecast stands until IDC publishes an upgrade.
- For Forward P/E (live category): if computed values show all three names BELOW their thresholds (SK Hynix <5x, Samsung <6x, Micron <8x), STATUS = Fired regardless of evidence_count=0 in news. The computed verdict IS the evidence.
- (v3.39.7.9) For t2_hyperscaler_capex_revenue: the rule is capex/OCF >50% (Fired) / >40% (Partially fired). NOT capex/revenue. capex/revenue ratios are no longer passed in signal_specific and any historical reference to capex/revenue in the evidence store reversal_condition is LEGACY — ignore it. Use mean_capex_ocf_ratio_2q (2-quarter mean) as the authoritative metric. Today's Amazon Q1 2026 capex/OCF of 169.8% in isolation is single-quarter OCF noise; the 2-quarter mean smooths it. If mean_capex_ocf_ratio_2q > 0.50, STATUS = Fired. If > 0.40, STATUS = Partially fired. Only if < 0.40 for two consecutive published quarters does the indicator un-fire.

NEW EVIDENCE HANDLING:
If today's news ADDS corroborating evidence (e.g., another capex announcement, another LTA), the status remains Fired and the new evidence is logged downstream. Mention the new evidence in `evidence_for_change_vs_yesterday`.

If today's news matches a recorded reversal_condition, the status transitions:
  - From Fired → set to "Not fired" (v3.39.7.9: collapsed from "Reversed". The audit trail lives in evidence_for_change_vs_yesterday and the trigger event banner — the status pill only carries the current state)
  - From Partially fired → set to "Not fired" if reversal is clean, "Partially fired" if mixed
Explain the reversal in `evidence_for_change_vs_yesterday` with citation of the reversal evidence.

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

INDICATOR EVIDENCE STORE (v3.39.7.3) — cumulative record of WHY this indicator
is in its current state, with explicit reversal_conditions per evidence entry:

{evidence_store_block}

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
  "fired_status": "Fired" | "Not fired" | "Partially fired",
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
    indicator_evidence: Optional[Any] = None,  # IndicatorEvidence from src.state
) -> Optional[SignalReasoning]:
    """Layer 2 for one indicator.

    v3.39.7.3: optional `indicator_evidence` parameter carries the persistent
    evidence store entry for this indicator (an IndicatorEvidence dataclass
    from src.state). When provided, the prompt includes the full evidence_log
    + reversal_conditions so Haiku can preserve prior fired status against
    silence in today's news. When None, falls back to the prior behavior
    (just the prior_status string).
    """
    # v3.4.2 — skip DHCR (direction monitor computed deterministically from
    # YAML, not Haiku-reasoned). Saves ~10s + reduces 529 surface area + avoids
    # Haiku contradicting computed direction.
    if indicator.id == "t1_decode_hbm_captive_ratio":
        logger.debug(f"Layer 2 skipped for {indicator.id} — direction monitor")
        return None

    if client is None:
        return None

    ev = _gather_evidence_for_signal(
        indicator, digests, fast_tier_news, naver_data, finnhub_data,
        edgar_items, computed_metrics, prior_status,
    )

    # v3.39.7.3 — format the evidence store block for the prompt
    if indicator_evidence is not None:
        try:
            evidence_store_block = indicator_evidence.format_for_layer2_prompt()
        except Exception as e:
            logger.warning(f"Could not format evidence store for {indicator.id}: {e}")
            evidence_store_block = "(evidence store unavailable for this indicator)"
    else:
        evidence_store_block = (
            "(no evidence store available — relying on prior_status alone; "
            "this is the v3.39.7 behavior pre-v3.39.7.3)"
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
        evidence_store_block=evidence_store_block,
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

    # v3.4.2 Fix A — per-signal retry-with-backoff for 529 Overloaded.
    # The May 22 03:50 run had 13/23 indicators fail with 529 because the
    # SDK's quick retry (~3s total) was insufficient for sustained overload.
    # 3 attempts at 10s / 30s / 90s = up to ~2min per signal. Affects worst-
    # case Layer 2 runtime but typical case is unchanged.
    LAYER2_RETRY_BACKOFFS = [10, 30, 90]
    import time as _time
    resp = None
    last_error = None
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1500,
            temperature=LLM_TEMPERATURE,
            system=REASONER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        last_error = e
        err_str = str(e).lower()
        if "529" in err_str or "overloaded" in err_str:
            logger.warning(
                f"Layer 2 attempt 1 overloaded for {indicator.id}: {e}. "
                f"Entering retry-with-backoff."
            )
            for attempt_idx, backoff_s in enumerate(LAYER2_RETRY_BACKOFFS, start=2):
                _time.sleep(backoff_s)
                try:
                    resp = client.messages.create(
                        model=HAIKU_MODEL,
                        max_tokens=1500,
                        temperature=LLM_TEMPERATURE,
                        system=REASONER_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_msg}],
                    )
                    logger.info(f"Layer 2 retry attempt {attempt_idx} succeeded for {indicator.id}")
                    break
                except Exception as retry_e:
                    last_error = retry_e
                    err_str2 = str(retry_e).lower()
                    if "529" not in err_str2 and "overloaded" not in err_str2:
                        logger.error(f"Layer 2 retry attempt {attempt_idx} failed (non-overload) for {indicator.id}: {retry_e}")
                        break
                    logger.warning(f"Layer 2 retry attempt {attempt_idx} still overloaded for {indicator.id}")
        else:
            logger.error(f"Layer 2 LLM call failed for {indicator.id} (non-overload): {e}")

    if resp is None:
        logger.error(f"Layer 2 exhausted retries for {indicator.id}; final error: {last_error}")
        return None

    try:
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
        logger.error(f"Layer 2 response parse failed for {indicator.id}: {e}")
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
    indicator_evidence_store: Optional[dict] = None,  # v3.39.7.3
    max_workers: int = 6,  # v3.38.1: kept for backward-compat, no longer used (was ThreadPoolExecutor count)
    inter_call_delay_seconds: float = 7.0,
) -> list[SignalReasoning]:
    """Run Layer 2 across every indicator — sequentially, with throttling.

    v3.39.7.3: optional `indicator_evidence_store` parameter is a dict mapping
    indicator_id → IndicatorEvidence (from src.state.load_indicator_evidence_store).
    When provided, each indicator's evidence_log + reversal_conditions are
    passed into the Layer 2 prompt, enabling carry-forward of prior fired
    status against silence in today's news.

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
    indicator_evidence_store = indicator_evidence_store or {}
    results: list[SignalReasoning] = []
    n = len(INDICATORS)
    logger.info(
        f"Layer 2: running {n} signals sequentially with {inter_call_delay_seconds}s "
        f"inter-call delay (est. {((n-1) * inter_call_delay_seconds + n * 2):.0f}s total)"
    )
    if indicator_evidence_store:
        logger.info(
            f"Layer 2: evidence store loaded for "
            f"{sum(1 for ind in INDICATORS if ind.id in indicator_evidence_store)}/{n} indicators"
        )

    for i, ind in enumerate(INDICATORS):
        # Throttle before every call except the first one
        if i > 0 and inter_call_delay_seconds > 0:
            time.sleep(inter_call_delay_seconds)
        try:
            result = reason_one_signal(
                ind, digests, fast_tier_news, naver_data, finnhub_data,
                edgar_items, computed_metrics, prior_status_map.get(ind.id),
                indicator_evidence=indicator_evidence_store.get(ind.id),
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
