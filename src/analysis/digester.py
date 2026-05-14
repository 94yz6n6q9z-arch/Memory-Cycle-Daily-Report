"""
Layer 1 — Document digester.

Runs in the DEEP TIER (03:00 UTC daily). For each large input (Quartr
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
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Optional

from anthropic import Anthropic

from src.config import INDICATORS, COMPANY_SPECIFIC_INDICATORS, HAIKU_MODEL, LLM_TEMPERATURE
from src.state import write_digest, fetch_with_retry, Criticality

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

    // Free-text catch for anything material that doesn't fit above
    "architecture_freetext": null or "<one-sentence summary of the material architectural point>"
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
# 60k chars ≈ 15-20k tokens, well within Haiku's window with room for the prompt.
MAX_INPUT_CHARS = 60_000


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
    if client is None:
        logger.error("Anthropic client unavailable (no API key)")
        return None
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

    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2000,
            temperature=LLM_TEMPERATURE,
            system=DIGEST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        # Strip ```json fences if model added them
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
        logger.error(f"Digest LLM call failed for {source_id}: {e}")
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
    # Purely additive: if architecture_signals is present in key_metrics,
    # append to a daily log file. Failure is non-fatal. The log is consumed
    # by v3.4 indicators (Inference Efficiency Shock / CXL Deployment / LPDDR Datacenter Ramp).
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

    return digest


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


def digest_transcript_results(transcript_payload: dict) -> list[Digest]:
    """v3.31 — digest the transcript_sources payload (IR scrapers + Motley Fool).

    Honours the three-state availability flag:
      - "full_transcript": run the normal digester over the call text
      - "materials_only":  digest what we have BUT tag the digest so the email
                           can say "commentary NOT available". The digest still
                           captures whatever numeric data is in the release/slides.
      - "nothing":         write a minimal placeholder digest recording the gap,
                           so the absence is visible in the brief rather than silent.

    The whole point of this pipeline is getting commentary right — so a missing
    transcript is itself a tracked fact, not a silent hole.
    """
    digests: list[Digest] = []
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
            else:
                # digest call itself failed — still record the gap
                digests.append(_placeholder_gap_digest(
                    company, ticker, event_date, source_name, source_url,
                    availability="materials_only",
                    note=note or "Materials found but digest failed.",
                ))

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
    """Iterate Korean broker PDF payloads (Mirae / NH / Samsung Securities) and digest each note."""
    digests: list[Digest] = []
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
                write_digest(
                    name=f"broker_{broker_name.replace(' ', '_')}_{published_iso}_{title[:40].replace(' ', '_')}",
                    payload=d.to_dict(),
                )
    return digests
