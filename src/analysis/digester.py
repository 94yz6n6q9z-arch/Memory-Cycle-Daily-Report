"""
Layer 1 — Document digester.

Runs in the DEEP TIER (08:00 UTC daily). For each large input (Yahoo Finance / Motley Fool / IR
earnings transcript, Korean broker English PDF, hyperscaler 10-Q
exhibit), produces a structured JSON digest:

    {
      "source_id":      "Mirae Asset 2026-05-09 SK Hynix HBM4 note",
      "source_type":    "korean_broker_pdf" | "earnings_transcript" | "regulatory_filing",
      "published_date": "2026-05-09",
      "key_metrics":    { ddr5_16gb_spot_referenced: "$39",
                          lpddr5x_lead_time_referenced: "26-39 weeks",
                          supplier_inventory_weeks: "2-3", ... },
      "signal_relevance": ["t1_lead_times", "t1_oem_inventory", "t2_eps_revisions", "i6b_samsung_hbm4"],
      "key_quotes":     ["...", "..."],
      "target_price_action": { broker, ticker, new_pt, prior_pt, direction },
      "fired_status_evidence": "neutral" | "supports_fire" | "supports_not_fired",
    }

Each digest is one Haiku call. ~5-30K input tokens, ~3-5K output. Cost:
~$0.01-0.04 per digest. On a typical day there are 0-5 new documents
to digest; on heavy days (earnings season) up to 15-20.

The digester PROMPT is the operational implementation of the trigger
framework — it sees the indicator's trigger_patterns + trigger_numeric
verbatim from config.py and is told to flag which apply.

CHANGELOG (this file):
  v3.31  (2026-05-14) — Added digest_transcript_results() + _placeholder_gap_digest(); digest_quartr_transcripts() now a no-op shim
  v3.3   (2026-05-12) — DIGEST_SYSTEM_PROMPT extracts architecture_signals; _log_architecture_signals() shadow logger
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Optional

from anthropic import Anthropic

from src.config import INDICATORS, COMPANY_SPECIFIC_INDICATORS, HAIKU_MODEL, LLM_TEMPERATURE
from src.state import (
    write_digest, fetch_with_retry, Criticality,
    # v3.4.12 — per-quarter store + diff cache
    quarter_key_from_event_date, get_prior_quarter_key,
    load_quarter_digest, save_quarter_digest,
)
# v3.4.12 — Haiku QoQ diff between consecutive transcripts
from src.analysis.transcript_diff import compute_diff_with_prior

logger = logging.getLogger("memory_cycle_brief.digester")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ============================================================
# Build the "trigger framework as prompt" — once, at import time
# ============================================================

def _build_trigger_reference_block() -> str:
    """Produce the trigger framework as compact reference text for the Haiku prompt.

    Includes every indicator's id, what_to_look_for, trigger_patterns,
    trigger_numeric, cadence. This is what makes the LLM aware of the
    operational definition of each signal.
    """
    lines = ["INDICATOR TRIGGER FRAMEWORK (apply when classifying each document):", ""]
    for ind in INDICATORS:
        lines.append(f"## {ind.id} — {ind.name} (Tier {ind.tier})")
        lines.append(f"   Trigger summary: {ind.what_to_look_for}")
        if ind.trigger_patterns:
            patterns_short = ", ".join(f'"{p}"' for p in ind.trigger_patterns[:6])
            lines.append(f"   Trigger phrases: {patterns_short}")
        if ind.trigger_numeric:
            lines.append(f"   Trigger numeric: {json.dumps(ind.trigger_numeric, default=str)}")
        if ind.cadence:
            lines.append(f"   Cadence: {ind.cadence}")
        if ind.stitching:
            lines.append(f"   Stitching: {ind.stitching}")
        lines.append("")

    lines.append("## Company-specific indicators")
    for ind_id, ind_data in COMPANY_SPECIFIC_INDICATORS.items():
        lines.append(f"## {ind_id} — {ind_data['name']}")
        if "trigger_patterns" in ind_data and ind_data["trigger_patterns"]:
            patterns_short = ", ".join(f'"{p}"' for p in ind_data["trigger_patterns"][:6])
            lines.append(f"   Trigger phrases: {patterns_short}")
        if ind_data.get("trigger_numeric"):
            lines.append(f"   Trigger numeric: {json.dumps(ind_data['trigger_numeric'], default=str)}")
        if ind_data.get("stitching"):
            lines.append(f"   Stitching: {ind_data['stitching']}")
        lines.append("")
    return "\n".join(lines)


TRIGGER_REFERENCE_BLOCK = _build_trigger_reference_block()


DIGEST_SYSTEM_PROMPT = f"""You are a memory-semiconductor cycle analyst processing one source document at a time. Your job is to produce a structured JSON digest that the downstream Layer 2 reasoner will combine with other sources.

You will be given one document (earnings transcript, broker PDF text, regulatory filing) and asked to extract:
  1. key_metrics  — any NUMERIC values mgmt or analysts cited (lead times, inventory weeks, ASPs, contract prices, EPS, capex, P/E)
  2. signal_relevance — which of the indicator IDs below this document informs
  3. key_quotes — 1-3 verbatim short quotes (each <15 words) capturing what changed
  4. target_price_action — if a Korean or US broker action, the new vs prior PT
  5. fired_status_evidence — does this document support firing/not-firing/no-change for the signals it informs

CRITICAL RULES:
- Quote each excerpt under 15 words. Maximum ONE quote per document.
- Be conservative: if a trigger phrase is mentioned but not in the right context (e.g. DDR4-only when the signal requires DDR5), do NOT include that signal in signal_relevance.
- For numeric fields, extract EXACT values mgmt stated. If mgmt said "we exited the quarter at 2.3 weeks", record 2.3, not "low".
- Output VALID JSON only. No prose outside the JSON object.

SPECIAL HANDLING FOR KOREAN BROKER PDFs (source_type="korean_broker_pdf"):
These notes always have an estimate revision summary somewhere in the body. You MUST extract these fields into key_metrics when present:
  - "fy27_eps_revision_pct": signed % change to FY27 EPS estimate (e.g. -8.5 if cut from $5.50 to $5.03)
  - "fy28_eps_revision_pct": same for FY28 if shown
  - "fy27_eps_new": the new FY27 EPS estimate value
  - "fy27_eps_prior": the prior FY27 EPS estimate if shown
  - "fy28_eps_new", "fy28_eps_prior": same for FY28
And ALSO populate target_price_action with EXPLICIT:
  - broker, ticker, new_pt, prior_pt, direction (raised/lowered/maintained/initiated)
These feed the t2_eps_revisions weighted scoring engine — without them the signal can't score this note's contribution beyond the target price change.

SPECIAL HANDLING FOR EARNINGS TRANSCRIPTS (source_type="earnings_transcript"):
Memory makers state inventory weeks verbatim during the call. Extract into key_metrics:
  - "supplier_inventory_weeks": numeric value mgmt stated (e.g. "we exited Q1 at 2.3 weeks" → 2.3)
  - "lead_time_weeks": if mgmt commented on lead times
  - "asp_yoy_pct": if mgmt commented on ASP YoY
  - "contract_price_qoq_pct": if mgmt commented on contract direction
For hyperscaler / OEM transcripts:
  - "capex_2027_guidance_usd_b": if 2027 capex guidance was provided
  - "component_inventory_weeks": OEM-side inventory commentary (Dell/HPE/Lenovo)

v3.3 — CAPTIVE-RATIO ARCHITECTURE SIGNALS (extract for any source type when mentioned):
These signals track the leading indicators of HBM captive ratio shifts. Extract into key_metrics
under the "architecture_signals" sub-object. Only populate fields that are EXPLICITLY mentioned;
do not infer or speculate. Use null for unknown.

  "architecture_signals": {{
    // Memory vendor capex / mix disclosures
    "hbm_capex_pct": null or number,           // e.g. mgmt says "HBM is now 50% of our DRAM capex" → 50
    "lpddr_capex_pct": null or number,         // same idea for LPDDR
    "ddr_capex_pct": null or number,           // same idea for conventional DDR
    "hbm_bit_share_pct": null or number,       // % of total memory bits going to HBM
    "wafer_allocation_language": null or "favoring_hbm" | "favoring_ddr" | "balanced" | "shifting_to_lpddr",

    // Hyperscaler memory mix disclosures
    "memory_pct_of_capex": null or number,     // e.g. "memory is 30% of our 2026 capex" → 30
    "custom_silicon_memory_arch": null or "HBM" | "LPDDR" | "mixed" | "GDDR" | "CXL_pooled",
    "custom_silicon_named": null or "TPU v7" | "Trainium 2" | "MTIA Gen 3" | "Maia 100" | etc,
    "inference_cost_reduction_pct": null or number,  // claimed YoY inference cost reduction
    "tokens_per_dollar_claim": null or number,        // if mgmt cited specific tokens-$ figure

    // Efficiency-technique adoption signals
    "deployed_quantization": null or "INT4" | "INT2" | "FP4" | "FP8",
    "deployed_moe": null or true | false,             // if mgmt confirmed MoE in production
    "deployed_speculative_decoding": null or true | false,
    "deployed_disaggregated_serving": null or true | false,

    // Memory architecture infrastructure signals
    "cxl_production_deployment": null or true | false,  // first production CXL pool
    "lpddr_datacenter_design_win": null or "<vendor name>",
    "hbf_mention": null or true | false,                // High Bandwidth Flash

    // v3.4.0 — Enterprise agent workload context (feeds DHCR demand-pull narrative)
    // These fields are extracted when mgmt explicitly discusses enterprise AI
    // adoption, agent workload deployment, or context length growth at a customer.
    // The DHCR direction monitor uses these as evidence that demand-pull is
    // expanding into the efficiency-unlocked headroom.
    "enterprise_ai_workload_context_tokens": null or number,  // e.g. "customer X processes 500K-token workflows" → 500000
    "enterprise_ai_customer_name": null or "<customer name if disclosed>",
    "enterprise_ai_use_case": null or "<one-liner: legal review / underwriting / coding agent / etc>",
    "enterprise_ai_revenue_growth_qoq_pct": null or number,  // QoQ enterprise AI segment growth if disclosed
    "agent_product_usage_disclosure": null or "<one-liner: 'Copilot has X paying users at Y context'>",

    // v3.4.5 — DHCR catalog auto-update fields. Three lightweight sub-objects
    // that the dhcr_arch_aggregator, dhcr_accelerator_aggregator, and
    // dhcr_revenue_aggregator consume to keep the three reference catalogs
    // fresh without manual edits. All fields null unless explicitly disclosed.
    "new_frontier_model_release": null or {{
      "name": "<model name>",
      "release_date": "YYYY-MM-DD",
      "architecture_family": "GQA" | "GQA-MoE" | "MLA" | "MLA-MoE" | "MHA",
      "num_layers": number,
      "num_kv_heads": number,         // or mla_kv_lora_rank for MLA-class
      "head_dim": number,
      "bytes_per_element": 2 | 1 | 0.5 | 0.25,
      "compression_factor": number,
      "kv_kb_per_token": number,      // optional if direct cite available
      "source_url": "<url>",
      "is_open": true | false
    }},

    "new_accelerator_announcement": null or {{
      "vendor": "nvidia" | "amd" | "google" | "aws" | "meta" | "microsoft" | "tesla" | "<other>",
      "name": "<accelerator name>",   // e.g. "Rubin Ultra", "MI400", "TPU v8"
      "hbm_gb": number,
      "release_date": "YYYY-MM-DD",   // announced ship date or stated shipping target
      "generation": "<generation label>",  // e.g. "Vera Rubin Ultra", "Hopper Next"
      "category": "nvidia" | "amd" | "custom_silicon" | "non_hbm_accelerators",
      "notes": "<any spec disclosures>"
    }},

    "enterprise_ai_quarterly_revenue": null or {{
      "category": "microsoft_azure_ai" | "aws_ai_services" | "google_cloud_ai" | "oracle_ai_infra" | "nvidia_datacenter",
      "quarter": "<YYYY-Q[1-4]>",
      "revenue_usd_b": number,
      "qoq_growth_pct": number,
      "yoy_growth_pct": number,
      "source_quote": "<earnings quote>"
    }},

    // Free-text catch for anything material that doesn't fit above
    "architecture_freetext": null or "<one-sentence summary of the material architectural point>"
  }},

  // v3.4.4.1 — Indicator 7 (Competitor dynamics) extraction fields
  // The Layer 1 digester surfaces these signals so the competitor_aggregator
  // can update state/competitor_state.json. Each field is null unless the
  // document EXPLICITLY discloses the value. Do not infer from general
  // commentary.
  "competitor_signals": {{
    // 7A — CXMT
    "cxmt_oem_mainstream_win": null or "<Tier-1 OEM name>",   // Dell/HPE/Lenovo/ASUS/Acer in MAINSTREAM CXMT DDR5 product line
    "cxmt_oem_lowend_win": null or "<Tier-1 OEM name>",       // Same OEMs in low-end CXMT product line only
    "cxmt_ddr5_share_pct": null or number,                    // CXMT DDR5 share % if disclosed
    "cxmt_ddr5_pricing_pressure_flagged": null or true,        // TrendForce/DigiTimes flag CXMT-driven DDR5 contract pricing pressure
    "us_export_controls_easing_event": null or true,           // BIS easing announcement re CXMT-relevant equipment

    // 7B — Samsung HBM4/HBM4E
    "samsung_hbm4_nvidia_share_pct": null or number,           // Samsung Nvidia HBM4 share % (Vera Rubin allocation)
    "sk_hynix_hbm4_nvidia_share_pct": null or number,          // SK Hynix Nvidia HBM4 share %
    "samsung_hbm4_yield_pct": null or number,                  // Samsung HBM4 production yield %
    "hbm_gross_margin_pct_sk_hynix": null or number,           // SK Hynix HBM gross margin %
    "hbm_gross_margin_pct_samsung": null or number,            // Samsung HBM gross margin %
    "hbm4e_qualification_lead": null or "sk_hynix_ahead" | "tied" | "samsung_ahead",
    "samsung_hbm4e_announcement": null or true,                // Samsung announced HBM4E (GTC/Hot Chips/etc.)
    "amd_samsung_hbm4_supply_mou": null or true,               // AMD-Samsung HBM4 supply MOU signed/expanded

    // 7C — Micron HBM
    "micron_vera_rubin_design_win": null or "primary_sku" | "secondary_sku" | "excluded",
    "micron_total_hbm_share_pct": null or number,
    "micron_hbm_share_growth_source": null or "samsung" | "sk_hynix" | "both",

    // 7D — HBF
    "hbf_hyperscaler_design_win": null or "<hyperscaler+accelerator+tier (hot|warm|cold)>",
    "hbf_hot_tier_displacement": null or true,                 // hot-tier HBM displaced specifically (not cold-tier addition)
    "jedec_hbf_spec_ratified": null or true,
    "sk_hynix_on_jedec_hbf_working_group": null or true | false,
    "sk_hynix_hbf_roadmap_mention": null or true,              // SK Hynix mentioned HBF in earnings/IR/tech day
    "sk_hynix_solidigm_hbf_integration": null or true,         // Solidigm + HBM packaging integration announcement
    "sandisk_kioxia_hbf_milestone": null or "<milestone description>",
    "hbf_commercial_stage": null or "pre_commercial" | "sampling" | "first_production" | "volume"
  }},

  // v3.4.4.1 — Indicator 8 (US ADR / SPO event) extraction fields
  // Only populated when the document explicitly references SK Hynix ADR
  // / SPO event progression. SEC EDGAR atom feed already detects F-1
  // filings directly (no LLM extraction needed for that); Layer 1
  // covers news coverage during roadshow/pricing/insider-sales windows.
  "adr_signals": {{
    "f1_filed": null or true,                                  // SEC EDGAR F-1 detected (or news confirms filing)
    "f1_canceled": null or true,                               // F-1 filed then offering pulled (= Fired)
    "underwriter_syndicate": null or ["Goldman Sachs", "Morgan Stanley", ...],
    "roadshow_active": null or true,                           // US roadshow has started
    "demand_classification": null or "strong" | "normal" | "weak",  // from coverage language
    "pricing_day": null or "YYYY-MM-DD",                       // 424B pricing date
    "pricing_discount_pct": null or number,                    // discount to prior close at pricing
    "final_size_usd_b": null or number,                        // final issuance size
    "insider_sale_event": null or {{
      "insider": "<name>",
      "role": "<CEO|CFO|director|etc>",
      "usd_m": number,
      "date": "YYYY-MM-DD"
    }}
  }}
}}

CRITICAL: These fields are for direct quotes/disclosures only. If mgmt did not state it, use null.
Do NOT extract from general AI/tech commentary — only from explicit memory/architecture statements.

{TRIGGER_REFERENCE_BLOCK}
"""


# ============================================================
# Schemas
# ============================================================

@dataclass
class Digest:
    source_id: str
    source_type: str         # earnings_transcript | korean_broker_pdf | regulatory_filing | news_article
    published_date_iso: str
    title: str
    char_count_input: int
    key_metrics: dict[str, Any]
    signal_relevance: list[str]      # indicator IDs
    key_quotes: list[str]
    target_price_action: Optional[dict[str, Any]]
    fired_status_evidence: str       # "supports_fire" | "supports_not_fired" | "neutral"
    raw_url: Optional[str] = None
    digested_at_iso: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["digested_at_iso"]:
            d["digested_at_iso"] = datetime.utcnow().isoformat()
        return d


# ============================================================
# Digester
# ============================================================

DIGEST_USER_TEMPLATE = """Document to digest:

Source ID: {source_id}
Source type: {source_type}
Published date: {published_date_iso}
Title: {title}
Length: {char_count} characters

--- DOCUMENT TEXT ---
{text}
--- END DOCUMENT ---

Respond with a JSON object matching this schema:
{{
  "key_metrics": {{ "metric_name": "value or number", ... }},
  "signal_relevance": ["indicator_id_1", "indicator_id_2"],
  "key_quotes": ["verbatim quote under 15 words", ...],
  "target_price_action": {{ "broker": "...", "ticker": "...", "new_pt": ..., "prior_pt": ..., "direction": "raised|lowered|maintained|initiated" }} or null,
  "fired_status_evidence": "supports_fire" | "supports_not_fired" | "neutral"
}}

Only output the JSON object."""


# Cap input length to keep Haiku context safe + costs sane.
# v3.37: bumped 60k → 80k after three v3.36 transcripts (Meta 60,304 / Dell
# 61,428 / HPE 60,883) just clipped the cap, losing ~1-2% of content each.
# 80k chars ≈ 20-25k tokens, still well within Haiku's 200k context window
# with ample room for the prompt and structured output.
MAX_INPUT_CHARS = 80_000


def _build_digest_user_msg(
    source_id: str,
    source_type: str,
    published_date_iso: str,
    title: str,
    text: str,
) -> Optional[tuple[str, str]]:
    """Build the Layer 1 digester user message for one document.

    v3.40.1 — extracted from digest_document so the synchronous path and the
    Message-Batches path build byte-identical prompts. Returns
    ``(user_msg, truncated_text)`` or ``None`` when the text is too short to
    digest (same skip rule as the sync path).
    """
    if not text or len(text.strip()) < 50:
        logger.info(f"Skipping digest for {source_id}: text too short ({len(text)} chars)")
        return None
    truncated = text[:MAX_INPUT_CHARS]
    if len(text) > MAX_INPUT_CHARS:
        logger.info(f"Truncated {source_id} from {len(text)} → {MAX_INPUT_CHARS} chars")
    user_msg = DIGEST_USER_TEMPLATE.format(
        source_id=source_id,
        source_type=source_type,
        published_date_iso=published_date_iso,
        title=title,
        char_count=len(truncated),
        text=truncated,
    )
    return user_msg, truncated


def _parse_digest_response(
    resp: Any,
    source_id: str,
    source_type: str,
    published_date_iso: str,
    title: str,
    truncated: str,
    raw_url: Optional[str],
) -> Optional["Digest"]:
    """Parse a Haiku response (sync OR batch) into a Digest + run the three
    shadow-loggers (architecture / competitor / ADR signals). v3.40.1 —
    extracted from digest_document so both call paths share identical parsing
    and the per-document extraction side effects.
    """
    try:
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Digest JSON parse failed for {source_id}: {e}; raw={raw[:300]!r}")
        return None
    except Exception as e:
        logger.error(f"Digest response parse failed for {source_id}: {e}")
        return None

    digest = Digest(
        source_id=source_id,
        source_type=source_type,
        published_date_iso=published_date_iso,
        title=title,
        char_count_input=len(truncated),
        key_metrics=parsed.get("key_metrics", {}),
        signal_relevance=parsed.get("signal_relevance", []),
        key_quotes=parsed.get("key_quotes", []),
        target_price_action=parsed.get("target_price_action"),
        fired_status_evidence=parsed.get("fired_status_evidence", "neutral"),
        raw_url=raw_url,
    )

    # v3.3 — Shadow-mode extraction log for architecture signals.
    try:
        arch_signals = (parsed.get("key_metrics") or {}).get("architecture_signals")
        if arch_signals and isinstance(arch_signals, dict):
            _log_architecture_signals(
                source_id=source_id,
                source_type=source_type,
                published_date_iso=published_date_iso,
                signals=arch_signals,
            )
    except Exception as e:
        logger.warning(f"Architecture-signal shadow log failed for {source_id}: {e}")

    # v3.4.4.1 — Indicator 7 + 8 shadow loggers.
    try:
        comp_signals = (parsed.get("key_metrics") or {}).get("competitor_signals")
        if comp_signals and isinstance(comp_signals, dict):
            _log_indicator_signals(
                source_id=source_id,
                source_type=source_type,
                published_date_iso=published_date_iso,
                signals=comp_signals,
                signal_key="competitor_signals",
            )
    except Exception as e:
        logger.warning(f"Competitor-signal shadow log failed for {source_id}: {e}")

    try:
        adr_signals = (parsed.get("key_metrics") or {}).get("adr_signals")
        if adr_signals and isinstance(adr_signals, dict):
            _log_indicator_signals(
                source_id=source_id,
                source_type=source_type,
                published_date_iso=published_date_iso,
                signals=adr_signals,
                signal_key="adr_signals",
            )
    except Exception as e:
        logger.warning(f"ADR-signal shadow log failed for {source_id}: {e}")

    return digest


# ============================================================
# v3.40.1 — Layer 1 digester batching (Message Batches API, 50% token cost).
# ============================================================
# Deep tier digests are independent per-document, so they batch cleanly. The
# digest_*_results loops keep their delicate post-processing (QoQ diff, gap
# placeholders, commentary-availability marking) untouched — only the digest
# PRODUCTION moves to a batch. prime_digest_batch() runs one batch up front and
# fills this cache keyed by source_id; digest_document() then returns the
# cached Digest if present, else falls back to a live synchronous call. A
# cache miss (batch failure, timeout, or any source_id mismatch) degrades
# safely to the proven synchronous path — correctness is never at risk, only
# the discount on that document.
_DIGEST_BATCH_CACHE: dict[str, Optional["Digest"]] = {}
_DIGEST_MISS = object()  # sentinel: "not produced by a batch" → sync fallback


def prime_digest_batch(jobs: list[dict], *, label: str = "Layer 1") -> None:
    """Submit `jobs` as ONE Message Batch and cache the resulting Digests by
    source_id for digest_document() to consume.

    jobs: list of digest_document-style kwargs dicts, each with at least
        source_id / source_type / published_date_iso / title / text, and
        optionally raw_url. Jobs whose text is too short are skipped (same rule
        as the sync path). No-op when USE_BATCH_API is off or jobs is empty.
    """
    try:
        from src.config import USE_BATCH_API, BATCH_POLL_CAP_SECONDS
    except Exception:
        return
    if not USE_BATCH_API or not jobs or client is None:
        return
    from src.analysis.batch_client import run_batch

    requests: list[dict] = []
    sidecar: dict[str, dict] = {}  # custom_id → {built inputs + job}
    for i, job in enumerate(jobs):
        built = _build_digest_user_msg(
            job["source_id"], job["source_type"], job["published_date_iso"],
            job["title"], job.get("text", ""),
        )
        if built is None:
            continue  # too short — sync path will also skip it
        user_msg, truncated = built
        cid = f"doc_{i}"  # Anthropic custom_id: no spaces/slashes; source_id has both
        requests.append({
            "custom_id": cid,
            "params": {
                "model": HAIKU_MODEL,
                "max_tokens": 2000,
                "temperature": LLM_TEMPERATURE,
                "system": DIGEST_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
        })
        sidecar[cid] = {"job": job, "truncated": truncated}

    if not requests:
        return

    succeeded, _needs_fallback = run_batch(
        client, requests, label=label, poll_cap_seconds=BATCH_POLL_CAP_SECONDS,
    )
    for cid, msg in succeeded.items():
        sc = sidecar[cid]
        job = sc["job"]
        digest = _parse_digest_response(
            msg, job["source_id"], job["source_type"], job["published_date_iso"],
            job["title"], sc["truncated"], job.get("raw_url"),
        )
        # Cache even None (parse failure) is NOT desired — leave misses to sync.
        if digest is not None:
            _DIGEST_BATCH_CACHE[job["source_id"]] = digest
    logger.info(
        f"{label}: primed {len(_DIGEST_BATCH_CACHE)} digest(s) via batch; "
        f"remainder (if any) will digest synchronously"
    )


def digest_document(
    source_id: str,
    source_type: str,
    published_date_iso: str,
    title: str,
    text: str,
    raw_url: Optional[str] = None,
) -> Optional[Digest]:
    """Run Haiku over one document and return a structured Digest.

    Returns None on LLM failure (caller decides whether to retry).
    """
    # v3.40.1 — if prime_digest_batch already produced this document via the
    # batch endpoint, return that Digest (and consume the cache entry so a
    # repeat call can't double-count). A cached result is valid independent of
    # client state, so this check comes first.
    _cached = _DIGEST_BATCH_CACHE.pop(source_id, _DIGEST_MISS)
    if _cached is not _DIGEST_MISS:
        return _cached

    if client is None:
        logger.error("Anthropic client unavailable (no API key)")
        return None

    _built = _build_digest_user_msg(source_id, source_type, published_date_iso, title, text)
    if _built is None:
        return None
    user_msg, truncated = _built

    # v3.4.3 — Layer 1 digester retry-with-backoff for 529 Overloaded.
    # Same audit conclusion as v3.4.2 for Sonnet and Layer 2: SDK's built-in
    # retry (~3s total budget) is insufficient for sustained API overload
    # windows that can run minutes. If digester fails silently during the
    # 08:00 UTC deep-tier window, transcript digests don't reach Layer 2 /
    # Sonnet and the morning briefs run with stale context.
    #
    # 3 attempts with [10s, 30s, 90s] backoffs = up to ~2min per document.
    # Per-document isolation — one transcript's 529 doesn't affect others.
    # Typical case unchanged (clean runs hit no retry path).
    LAYER1_RETRY_BACKOFFS = [10, 30, 90]
    import time as _time
    resp = None
    last_error = None
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2000,
            temperature=LLM_TEMPERATURE,
            system=DIGEST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        last_error = e
        err_str = str(e).lower()
        if "529" in err_str or "overloaded" in err_str:
            logger.warning(
                f"Layer 1 attempt 1 overloaded for {source_id}: {e}. "
                f"Entering retry-with-backoff."
            )
            for attempt_idx, backoff_s in enumerate(LAYER1_RETRY_BACKOFFS, start=2):
                _time.sleep(backoff_s)
                try:
                    resp = client.messages.create(
                        model=HAIKU_MODEL,
                        max_tokens=2000,
                        temperature=LLM_TEMPERATURE,
                        system=DIGEST_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_msg}],
                    )
                    logger.info(f"Layer 1 retry attempt {attempt_idx} succeeded for {source_id}")
                    break
                except Exception as retry_e:
                    last_error = retry_e
                    err_str2 = str(retry_e).lower()
                    if "529" not in err_str2 and "overloaded" not in err_str2:
                        logger.error(f"Layer 1 retry attempt {attempt_idx} failed (non-overload) for {source_id}: {retry_e}")
                        break
                    logger.warning(f"Layer 1 retry attempt {attempt_idx} still overloaded for {source_id}")
        else:
            logger.error(f"Digest LLM call failed for {source_id} (non-overload): {e}")

    if resp is None:
        logger.error(f"Layer 1 exhausted retries for {source_id}; final error: {last_error}")
        return None

    return _parse_digest_response(
        resp, source_id, source_type, published_date_iso, title, truncated, raw_url,
    )


def _log_indicator_signals(
    source_id: str,
    source_type: str,
    published_date_iso: str,
    signals: dict,
    signal_key: str,
) -> None:
    """v3.4.4.1 shadow logger: append extracted signals to a daily JSONL file
    under the named key ('competitor_signals' or 'adr_signals'). Consumed by
    src/analysis/competitor_aggregator.py and src/analysis/adr_aggregator.py
    on subsequent fast-tier runs.

    Append-only, isolated: any failure is caught by the caller. File path
    matches the architecture_signals shadow logger so all Layer 1 extraction
    flows through a single daily log.
    """
    from datetime import datetime, timezone
    from pathlib import Path
    import json

    # Strip null-valued fields so the log stays compact.
    filtered = {k: v for k, v in signals.items() if v is not None and v is not False}
    if not filtered:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = Path("state") / f"extraction_log_{today}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "source_id": source_id,
        "source_type": source_type,
        "published_date_iso": published_date_iso,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": _classify_source_for_tier(source_type, source_id),
        signal_key: filtered,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _classify_source_for_tier(source_type: str, source_id: str) -> str:
    """Map source identifiers to the strings the aggregators key off for
    tier-weighted conflict resolution. Returns the tier-aware source label
    (e.g. 'mirae_asset', 'sk_hynix_earnings'); falls back to source_type.

    v3.4.6 adds frontier-lab + arxiv + HF Papers source labels so the
    dhcr_arch_aggregator's Tier-A source set reaches the right places.
    """
    sid = (source_id or "").lower()
    stype = (source_type or "").lower()
    # v3.4.6 — frontier model sources (Tier A for DHCR arch aggregator)
    if "arxiv" in sid:
        return "arxiv"
    if "huggingface" in sid and "papers" in sid:
        return "huggingface_papers"
    if "huggingface" in sid:
        return "huggingface"
    if "ai.meta.com" in sid or "meta_ai_blog" in sid or "meta ai" in sid:
        return "ai.meta.com"
    if "deepseek" in sid:
        return "deepseek.com"
    if "mistral" in sid:
        return "mistral.ai"
    if "qwen" in sid or "qwenlm" in sid:
        return "qwenlm.github.io"
    if "anthropic" in sid:
        return "anthropic.com"
    if "deepmind" in sid:
        return "deepmind.google"
    # Hyperscaler / GPU vendor tech days (Tier A for DHCR accelerator aggregator)
    if "gtc" in sid or "nvidia" in sid and ("keynote" in stype or "keynote" in sid):
        return "nvidia_gtc"
    if "amd_ai_day" in sid or ("amd" in sid and ("ai_day" in sid or "ai day" in sid)):
        return "amd_ai_day"
    # Existing competitor/ADR mappings below — unchanged
    if "mirae" in sid:
        return "mirae_asset"
    if "kb_securities" in sid or "kb-securities" in sid:
        return "kb_securities"
    if "sk hynix" in sid or "skhynix" in sid or "sk_hynix" in sid:
        if "earnings" in stype or "transcript" in stype:
            return "sk_hynix_earnings"
        return "sk_hynix"
    if "samsung" in sid and ("earnings" in stype or "transcript" in stype):
        return "samsung_earnings"
    if "nvidia" in sid and ("earnings" in stype or "transcript" in stype):
        return "nvidia_earnings"
    if "micron" in sid and ("earnings" in stype or "transcript" in stype):
        return "micron_earnings"
    if "msft" in sid or "microsoft" in sid:
        if "earnings" in stype or "transcript" in stype:
            return "msft_earnings"
    if "amzn" in sid or "amazon" in sid:
        if "earnings" in stype or "transcript" in stype:
            return "amzn_earnings"
    if "googl" in sid or "google" in sid:
        if "earnings" in stype or "transcript" in stype:
            return "googl_earnings"
    if "orcl" in sid or "oracle" in sid:
        if "earnings" in stype or "transcript" in stype:
            return "orcl_earnings"
    if "counterpoint" in sid:
        return "counterpoint"
    if "trendforce" in sid:
        return "trendforce"
    if "semianalysis" in sid:
        return "semianalysis"
    if "digitimes" in sid:
        return "digitimes"
    if "chosun" in sid:
        return "chosunbiz"
    if "korea_times" in sid or "koreatimes" in sid:
        return "korea_times"
    if "sec" in sid or "edgar" in sid:
        return "sec_edgar"
    return stype or "unknown"


def _log_architecture_signals(
    source_id: str,
    source_type: str,
    published_date_iso: str,
    signals: dict,
) -> None:
    """v3.3 shadow logger: append extracted architecture signals to a daily JSONL file.

    File format: one JSON object per line at state/extraction_log_{today}.jsonl
    Each line includes the source identifiers + signals + extraction timestamp.

    Designed to be append-only and isolated: any failure here is caught by the
    caller and never affects digest output. Consumed by v3.4 indicators after
    a calibration period (target: 3+ weeks of accumulation for threshold tuning).
    """
    from pathlib import Path
    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    log_path = state_dir / f"extraction_log_{date.today().isoformat()}.jsonl"

    # Only log if at least one field is non-null (avoid noise)
    if not any(v is not None and v is not False for v in signals.values()):
        return

    record = {
        "source_id": source_id,
        "source_type": source_type,
        "published_date_iso": published_date_iso,
        "extracted_at_iso": datetime.utcnow().isoformat(),
        "signals": signals,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    logger.info(f"Architecture signals logged for {source_id}")


def digest_quartr_transcripts(quartr_payload: dict) -> list[Digest]:
    """DEPRECATED (v3.31) — Quartr is gone. Kept as a no-op for backward compat.
    Use digest_transcript_results() with the transcript_sources payload instead."""
    if quartr_payload and quartr_payload.get("deprecated"):
        return []
    # Defensive: if somehow handed a real old-shape payload, still handle it.
    digests: list[Digest] = []
    for ticker, t_data in quartr_payload.get("tickers", {}).items():
        for transcript in t_data.get("transcripts", []):
            text = transcript.get("text", "")
            event_date = transcript.get("event_date", "")
            event_type = transcript.get("event_type", "earnings_call")
            source_id = f"Quartr / {ticker} / {event_date} / {event_type}"
            d = digest_document(
                source_id=source_id,
                source_type="earnings_transcript",
                published_date_iso=event_date,
                title=f"{ticker} {event_type} {event_date}",
                text=text,
            )
            if d:
                digests.append(d)
                write_digest(name=f"quartr_{ticker}_{event_date}", payload=d.to_dict())
    return digests


def _persist_and_maybe_diff_quarter(
    digest: Digest,
    ticker: str,
    company: str,
    event_date: str,
) -> None:
    """v3.4.12 — Write per-quarter digest record and, if prior quarter exists,
    trigger ONE Haiku diff call between today's digest and the prior.

    Safe-failing: any exception is logged and the calling transcript flow
    continues normally. The per-quarter store is supplemental to the existing
    state/digests/ persistence — its purpose is to enable QoQ comparison,
    not to replace day-of digest storage.

    The diff cache lives in the same per-quarter file (diff_with_prior slot).
    Re-runs that find diff_with_prior already populated skip the Haiku call
    entirely (this is the cost-efficiency design — process once, cache).

    Sole caller: digest_transcript_results() after each successful
    write_digest(). Both full_transcript and materials_only branches hook in.
    """
    if not ticker or not event_date:
        logger.debug(
            f"Per-quarter store skipped: missing ticker ({ticker!r}) or "
            f"event_date ({event_date!r})"
        )
        return

    current_quarter = quarter_key_from_event_date(event_date)
    if not current_quarter:
        logger.debug(f"Per-quarter store skipped: could not derive quarter from {event_date!r}")
        return

    try:
        # Check if THIS quarter's record already exists with a cached diff —
        # if so, this is a re-run on the same transcript and we don't need
        # to do anything (digest is already current; diff is already cached).
        existing = load_quarter_digest(ticker, current_quarter)
        already_cached_diff = (
            existing is not None
            and existing.get("diff_with_prior") is not None
            and existing.get("digest", {}).get("digested_at_iso") == digest.digested_at_iso
        )
        if already_cached_diff:
            logger.debug(
                f"Per-quarter diff cache hit for {ticker} {current_quarter} — skipping Haiku"
            )
            return

        # Look up prior quarter
        prior_quarter = get_prior_quarter_key(current_quarter)
        prior_record = load_quarter_digest(ticker, prior_quarter) if prior_quarter else None

        diff_payload = None
        if prior_record is not None and prior_record.get("digest"):
            # Compute the QoQ diff via Haiku
            try:
                diff_obj = compute_diff_with_prior(
                    current_digest=digest.to_dict(),
                    prior_digest=prior_record["digest"],
                    ticker=ticker,
                    company=company,
                    current_quarter=current_quarter,
                    prior_quarter=prior_quarter,
                    anthropic_api_key=ANTHROPIC_API_KEY,
                    haiku_model=HAIKU_MODEL,
                )
                if diff_obj is not None:
                    diff_payload = diff_obj.to_dict()
                    logger.info(
                        f"QoQ diff computed: {company} ({ticker}) "
                        f"{prior_quarter} → {current_quarter} | "
                        f"tone={diff_payload.get('overall_tone_shift')} | "
                        f"indicator_deltas={len(diff_payload.get('indicator_deltas', []))} | "
                        f"dropped={len(diff_payload.get('language_dropped', []))} | "
                        f"added={len(diff_payload.get('language_added', []))}"
                    )
            except Exception as e:
                logger.warning(
                    f"Diff Haiku call failed for {ticker} {current_quarter} (safe-failing): {e}"
                )
        else:
            logger.info(
                f"Per-quarter digest saved without diff: {ticker} {current_quarter} "
                f"(no prior quarter found at {prior_quarter})"
            )

        save_quarter_digest(
            ticker=ticker,
            company=company,
            quarter_key=current_quarter,
            digest=digest.to_dict(),
            diff_with_prior=diff_payload,
        )
    except Exception as e:
        logger.warning(
            f"Per-quarter persist failed for {ticker} {current_quarter} (safe-failing): {e}"
        )


def digest_transcript_results(transcript_payload: dict) -> list[Digest]:
    """v3.31 — digest the transcript_sources payload (IR scrapers + Motley Fool).

    Honours the availability flag (v3.33 adds `already_processed`):
      - "full_transcript":    run the normal digester over the call text
      - "materials_only":     digest what we have BUT tag the digest so the email
                              can say "commentary NOT available". The digest still
                              captures whatever numeric data is in the release/slides.
      - "already_processed":  cache hit (URL was digested in a previous run).
                              Skip silently — the prior digest is already in
                              state/digests/. Avoids re-burning Haiku tokens
                              and proxy bandwidth on repeat captures.
      - "nothing":            write a minimal placeholder digest recording the gap,
                              so the absence is visible in the brief rather than silent.

    The whole point of this pipeline is getting commentary right — so a missing
    transcript is itself a tracked fact, not a silent hole.

    CHANGELOG (this function):
      v3.33  (2026-05-16) — Added `already_processed` branch (URL-cache hits skip
                            re-digesting; prior digest already in state/digests/).
    """
    digests: list[Digest] = []
    # v3.40.1 — prime ONE Layer 1 batch for every digestable transcript result,
    # then let the loop below consume the cached Digests (digest_document hits
    # the cache). The source_id / title construction here MUST match the loop's
    # exactly so cache keys line up; if it ever drifts, the loop simply falls
    # back to a live synchronous call (safe — only the discount is lost).
    _transcript_jobs: list[dict] = []
    for _r in transcript_payload.get("results", []):
        _av = _r.get("availability", "nothing")
        _co = _r.get("company", "Unknown")
        _ev = _r.get("event_date_iso", date.today().isoformat())
        _sn = _r.get("source_name", "transcript source")
        _su = _r.get("source_url")
        _tx = _r.get("text", "") or ""
        _sid = f"{_sn} / {_co} / {_ev}"
        if _av == "full_transcript":
            _transcript_jobs.append(dict(
                source_id=_sid, source_type="earnings_transcript",
                published_date_iso=_ev, title=f"{_co} earnings call {_ev}",
                text=_tx, raw_url=_su,
            ))
        elif _av == "materials_only":
            _transcript_jobs.append(dict(
                source_id=_sid + " [MATERIALS ONLY — no call commentary]",
                source_type="earnings_transcript", published_date_iso=_ev,
                title=f"{_co} earnings materials {_ev} (commentary NOT available)",
                text=_tx, raw_url=_su,
            ))
    prime_digest_batch(_transcript_jobs, label="Layer 1 transcripts")

    for r in transcript_payload.get("results", []):
        company = r.get("company", "Unknown")
        ticker = r.get("ticker", "")
        event_date = r.get("event_date_iso", date.today().isoformat())
        availability = r.get("availability", "nothing")
        text = r.get("text", "") or ""
        source_name = r.get("source_name", "transcript source")
        source_url = r.get("source_url")
        note = r.get("note")

        source_id = f"{source_name} / {company} / {event_date}"

        if availability == "full_transcript":
            d = digest_document(
                source_id=source_id,
                source_type="earnings_transcript",
                published_date_iso=event_date,
                title=f"{company} earnings call {event_date}",
                text=text,
                raw_url=source_url,
            )
            if d:
                # mark commentary availability for downstream consumers
                d.key_metrics["_transcript_availability"] = "full_transcript"
                digests.append(d)
                write_digest(
                    name=f"transcript_{company.replace(' ', '_')}_{event_date}",
                    payload=d.to_dict(),
                )
                # v3.4.12 — per-quarter store + QoQ diff cache (safe-failing)
                _persist_and_maybe_diff_quarter(d, ticker, company, event_date)

        elif availability == "materials_only":
            # Digest the materials we DO have (release / slides often carry
            # numeric data), but flag clearly that call commentary is missing.
            d = digest_document(
                source_id=source_id + " [MATERIALS ONLY — no call commentary]",
                source_type="earnings_transcript",
                published_date_iso=event_date,
                title=f"{company} earnings materials {event_date} (commentary NOT available)",
                text=text,
                raw_url=source_url,
            )
            if d:
                d.key_metrics["_transcript_availability"] = "materials_only"
                d.key_metrics["_commentary_gap_note"] = (
                    note or "Full earnings-call commentary not available from this source."
                )
                digests.append(d)
                write_digest(
                    name=f"transcript_{company.replace(' ', '_')}_{event_date}_materials",
                    payload=d.to_dict(),
                )
                # v3.4.12 — per-quarter store + QoQ diff cache (safe-failing).
                # Materials-only digests are still useful for diffing because
                # they capture numeric data from releases/slides; the diff will
                # naturally flag the call commentary as "language dropped" vs
                # a prior full transcript.
                _persist_and_maybe_diff_quarter(d, ticker, company, event_date)
            else:
                # digest call itself failed — still record the gap
                digests.append(_placeholder_gap_digest(
                    company, ticker, event_date, source_name, source_url,
                    availability="materials_only",
                    note=note or "Materials found but digest failed.",
                ))

        elif availability == "already_processed":
            # v3.33: cache hit. Prior digest is in state/digests/; no Haiku call,
            # no placeholder record. Log it so we know the cache fired correctly.
            logger.info(
                f"Transcript cache hit for {company} ({source_name}): {source_url} "
                f"— skipping (already digested in a previous run)"
            )

        else:  # "nothing"
            digests.append(_placeholder_gap_digest(
                company, ticker, event_date, source_name, source_url,
                availability="nothing",
                note=note or "No transcript or materials found for this event.",
            ))

    return digests


def _placeholder_gap_digest(
    company: str,
    ticker: str,
    event_date: str,
    source_name: str,
    source_url: Optional[str],
    availability: str,
    note: str,
) -> Digest:
    """Build a minimal Digest that records a transcript gap, so the brief can
    surface 'commentary not available' instead of silently omitting it."""
    d = Digest(
        source_id=f"{source_name} / {company} / {event_date} [GAP: {availability}]",
        source_type="earnings_transcript",
        published_date_iso=event_date,
        title=f"{company} — earnings commentary NOT available ({availability})",
        char_count_input=0,
        key_metrics={
            "_transcript_availability": availability,
            "_commentary_gap_note": note,
            "_ticker": ticker,
        },
        signal_relevance=[],
        key_quotes=[],
        target_price_action=None,
        fired_status_evidence="neutral",
        raw_url=source_url,
    )
    write_digest(
        name=f"transcript_{company.replace(' ', '_')}_{event_date}_GAP",
        payload=d.to_dict(),
    )
    logger.info(f"Recorded transcript gap for {company} ({availability})")
    return d


def digest_korean_broker_notes(broker_payloads: list[dict]) -> list[Digest]:
    """Iterate Korean broker note payloads (Mirae since v3.39.3.1) and digest
    each note. NH Investment and Samsung Securities were removed in v3.39.3.1.

    v3.39.4.2 fixed a latent filename-sanitization bug here. The original
    `title[:40].replace(' ', '_')` left forward slashes intact, which the
    v3.39.4 Mirae HTML title format ("SK Hynix (000660 KS/Buy)Reflecting...")
    exposed for the first time. The slash made open() try to write into a
    non-existent subdirectory. Now sanitized with the same regex pattern
    korean_brokers uses for html_path filenames."""
    digests: list[Digest] = []
    # v3.40.1 — prime one Layer 1 batch for all digestable broker notes (mirrors
    # the loop's <200-char skip + source_id construction; cache miss → sync).
    _broker_jobs: list[dict] = []
    for _payload in broker_payloads:
        _bn = _payload.get("broker", "Unknown Korean broker")
        for _note in _payload.get("notes", []):
            _tx = _note.get("extracted_text", "")
            if not _tx or len(_tx) < 200:
                continue
            _pi = _note.get("published_date_iso", date.today().isoformat())
            _ti = _note.get("title", "Untitled note")
            _broker_jobs.append(dict(
                source_id=f"{_bn} / {_pi} / {_ti[:60]}",
                source_type="korean_broker_pdf", published_date_iso=_pi,
                title=_ti, text=_tx, raw_url=_note.get("url"),
            ))
    prime_digest_batch(_broker_jobs, label="Layer 1 broker notes")

    for payload in broker_payloads:
        broker_name = payload.get("broker", "Unknown Korean broker")
        for note in payload.get("notes", []):
            text = note.get("extracted_text", "")
            if not text or len(text) < 200:
                continue
            published_iso = note.get("published_date_iso", date.today().isoformat())
            title = note.get("title", "Untitled note")
            source_id = f"{broker_name} / {published_iso} / {title[:60]}"
            d = digest_document(
                source_id=source_id,
                source_type="korean_broker_pdf",
                published_date_iso=published_iso,
                title=title,
                text=text,
                raw_url=note.get("url"),
            )
            if d:
                digests.append(d)
                # v3.39.4.2: sanitize title for filename. The naive
                # `.replace(' ', '_')` left forward slashes intact, which
                # blew up on v3.39.4's new Mirae HTML titles like
                # "SK Hynix (000660 KS/Buy)Reflecting additional NAND
                # momentum" — the slash made open() try to nest the file
                # inside a non-existent "(000660 KS/" directory. Use the
                # same regex pattern korean_brokers._fetch_mirae_detail
                # applies to its html_path filenames for consistency.
                safe_broker = re.sub(r"[^A-Za-z0-9._-]", "_", broker_name)
                safe_title = re.sub(r"[^A-Za-z0-9._-]", "_", title[:40])
                write_digest(
                    name=f"broker_{safe_broker}_{published_iso}_{safe_title}",
                    payload=d.to_dict(),
                )
    return digests
