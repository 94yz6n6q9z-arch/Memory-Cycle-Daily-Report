"""v3.41.12 — Deterministic ADR event detection (fast tier).

The v3.4.4.1 design comments promised two feeders that were never built:
"SEC EDGAR atom feed already detects F-1 filings directly" and "Layer 1
covers news coverage during roadshow/pricing/insider-sales windows". In
reality the extraction log had a single producer — the deep-tier digester,
whose entire document diet is earnings transcripts + broker PDFs — so every
event-native ADR field (pricing_day, final_size, demand, cancellation) was
structurally starved. Proven July 10: the 424B4 sat in the news feed, the
$26.5B/$149 pricing was in every headline, and the state machine showed
"Pricing day None" through the framework's own peak-coincidence window.

This module is the deterministic leg of the fix. It runs in the FAST tier,
on the news items already fetched every run, and emits extraction-log
entries in the exact shape `adr_aggregator._apply_extractions` reads
(top-level `adr_signals` + `date` + `source`). Facts covered are the ones
that are machine-readable facts:

  (a) EDGAR form types, parsed from the "[FILING] <form> - ..." titles:
        F-1 / F-1/A  -> f1_filed
        RW / withdrawal -> f1_canceled  (canceled = Fired)
        424B*        -> pricing_day = the filing's published date
  (b) Headline regexes (ADR-context-guarded):
        "raises US$26.5 billion"      -> final_size_usd_b
        "priced ... at $149"          -> pricing_day fallback (424B wins) +
                                          price captured as evidence
        "7x oversubscribed" / "books multiple times covered" -> demand strong
        "undersubscribed / weak demand" -> demand weak

Everything language-fuzzy beyond these belongs to the news_article LLM lane
(v3.41.13), not to more regexes. Same-day dedup by source_id; never raises;
logs its summary every run including zeros.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DETECTOR_TAG = "adr_deterministic_v3.41.12"

# ADR context guard for headline regexes — a size/price/demand phrase only
# counts when the same text is talking about the listing event, not capex
# ("raises $10B for HBM capacity") or guidance ("Micron raises guidance").
_ADR_CONTEXT = (
    "adr", "offering", "ipo", "listing", "share sale", "share offering",
    "us share", "nasdaq", "oversubscri", "prospectus", "f-1", "424b",
)

_SIZE_RE = re.compile(
    r"raise[sd]?\s+(?:up to\s+)?(?:us)?\$\s?(\d+(?:\.\d+)?)\s*b(?:illion)?\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"(?:priced?|prices)\s+(?:its\s+)?(?:adrs?|shares|us\s+ipo|ipo|offering)?\s*"
    r"(?:at\s+)?(?:us)?\$\s?(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_OVERSUB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*oversubscri", re.IGNORECASE)
_BOOKS_COVERED_RE = re.compile(
    r"books?\s+(?:were\s+)?(?:multiple\s+times|heavily|fully)\s+covered",
    re.IGNORECASE,
)
_WEAK_RE = re.compile(r"undersubscrib|weak\s+demand", re.IGNORECASE)


def _form_type_from_filing_title(title: str) -> Optional[str]:
    """'[FILING] 424B4 - Prospectus [Rule 424(b)(4)]' -> '424B4'."""
    t = (title or "").strip()
    if not t.upper().startswith("[FILING]"):
        return None
    rest = t[len("[FILING]"):].strip()
    if not rest:
        return None
    return rest.split()[0].rstrip("-").strip()


def _adr_signals_for_form(form: str, filed_date_iso: str) -> Optional[dict]:
    f = (form or "").upper()
    if f.startswith("424B") or f.startswith("424("):
        return {"pricing_day": filed_date_iso[:10]}
    if f == "F-1/A" or f == "F-1A":
        return {"f1_filed": True}
    if f == "F-1":
        return {"f1_filed": True}
    if f == "RW" or "WITHDRAW" in f:
        return {"f1_canceled": True}
    return None  # 6-K / 20-F / others carry no deterministic ADR signal


def _published_iso(item: Any) -> str:
    pub = getattr(item, "published", None)
    if pub is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(pub)
    return s if len(s) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _existing_source_ids(log_path: Path) -> set:
    seen: set = set()
    if not log_path.exists():
        return seen
    try:
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            sid = e.get("source_id")
            if sid:
                seen.add(sid)
    except Exception:
        pass
    return seen


def detect_and_log_adr_events(
    news_items: Optional[list],
    log_dir: Path = Path("state"),
) -> dict:
    """Scan the run's news items; write deterministic adr_signals entries.

    Returns a summary dict (also logged): counts per leg plus the merged
    signal view. Never raises — any failure degrades to a logged no-op.
    """
    summary = {"edgar_entries": 0, "headline_entries": 0, "skipped_dedup": 0,
               "signals": {}}
    try:
        items = news_items or []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = log_dir / f"extraction_log_{today}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        seen_ids = _existing_source_ids(log_path)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records: list[dict] = []
        merged: dict = {}
        saw_424b_pricing = False

        # ---- leg (a): EDGAR form types --------------------------------------
        for item in items:
            title = getattr(item, "title", "") or ""
            form = _form_type_from_filing_title(title)
            if not form:
                continue
            pub_iso = _published_iso(item)
            sigs = _adr_signals_for_form(form, pub_iso)
            if not sigs:
                continue
            source_id = f"edgar:{form}:{pub_iso[:10]}"
            if source_id in seen_ids:
                summary["skipped_dedup"] += 1
                continue
            if "pricing_day" in sigs:
                saw_424b_pricing = True
            records.append({
                "source_id": source_id,
                "source_type": "regulatory_filing",
                "published_date_iso": pub_iso,
                "extracted_at": now_iso,
                "date": now_iso,
                "source": "sec_edgar",
                "detector": DETECTOR_TAG,
                "evidence": title[:200],
                "adr_signals": sigs,
            })
            merged.update(sigs)
            summary["edgar_entries"] += 1
            seen_ids.add(source_id)

        # ---- leg (b): headline regexes ---------------------------------------
        for item in items:
            title = getattr(item, "title", "") or ""
            if title.upper().startswith("[FILING]"):
                continue  # EDGAR items handled by leg (a)
            text = f"{title} {getattr(item, 'summary', '') or ''}"
            low = text.lower()
            if not any(k in low for k in _ADR_CONTEXT):
                continue
            sigs: dict = {}
            evidence_bits: list[str] = []
            m = _SIZE_RE.search(text)
            if m:
                sigs["final_size_usd_b"] = float(m.group(1))
                evidence_bits.append(f"size ${m.group(1)}B")
            pm = _PRICE_RE.search(text)
            if pm:
                evidence_bits.append(f"priced at ${pm.group(1)}")
                if not saw_424b_pricing:
                    # 424B is the authoritative pricing_day; headline is the
                    # fallback when the form hasn't appeared in the feed yet.
                    sigs.setdefault("pricing_day", _published_iso(item)[:10])
            if _OVERSUB_RE.search(text) or _BOOKS_COVERED_RE.search(text):
                sigs["demand_classification"] = "strong"
                om = _OVERSUB_RE.search(text)
                if om:
                    evidence_bits.append(f"{om.group(1)}x oversubscribed")
            elif _WEAK_RE.search(text):
                sigs["demand_classification"] = "weak"
            if not sigs:
                continue
            url = (getattr(item, "url", "") or "").strip()
            source_id = f"newsdet:{(url or title)[:120]}"
            if source_id in seen_ids:
                summary["skipped_dedup"] += 1
                continue
            records.append({
                "source_id": source_id,
                "source_type": "news_article",
                "published_date_iso": _published_iso(item),
                "extracted_at": now_iso,
                "date": now_iso,
                "source": (getattr(item, "source", "") or "news").lower(),
                "detector": DETECTOR_TAG,
                "evidence": "; ".join(evidence_bits) or title[:200],
                "adr_signals": sigs,
            })
            for k, v in sigs.items():
                merged.setdefault(k, v)
            summary["headline_entries"] += 1
            seen_ids.add(source_id)

        if records:
            with open(log_path, "a") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
        summary["signals"] = merged
        logger.info(
            f"[adr-detect] edgar={summary['edgar_entries']} "
            f"headline={summary['headline_entries']} "
            f"dedup_skipped={summary['skipped_dedup']} signals={merged or '{}'}"
        )
    except Exception as e:  # never break the run for card decoration
        logger.warning(f"[adr-detect] detector failed (non-fatal): {e}")
    return summary
