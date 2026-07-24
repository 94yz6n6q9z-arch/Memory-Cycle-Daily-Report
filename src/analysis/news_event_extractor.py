"""v3.41.13 — News-event extraction lane (the missing `news_article` producer).

The extraction schema declared source_type="news_article" from day one, and
the aggregators were written to consume it — but no producer ever fed it:
the deep-tier digester digests only transcripts + broker PDFs. This module
is that producer. It runs in the FAST tier, before the aggregators, over the
day's already-fetched news items, and extracts ONLY the news-native event
fields — the ones announced via press coverage rather than stated by
management in a transcript:

  ADR (Indicator 8): f1_canceled, underwriter_syndicate, roadshow_active,
    demand_classification, pricing_day, pricing_discount_pct,
    final_size_usd_b, insider_sale_event
  Competitor (Indicator 7): us_export_controls_easing_event,
    cxmt_oem_mainstream_win, samsung_hbm4e_announcement,
    amd_samsung_hbm4_supply_mou, jedec_hbf_spec_ratified,
    sk_hynix_solidigm_hbf_integration, sandisk_kioxia_hbf_milestone,
    hbf_hyperscaler_design_win, hbf_hot_tier_displacement

Design: ONE Haiku call per run under structured outputs (the v3.41.11
grammar-constrained pattern from day one — a malformed reply is impossible),
producing per-item extractions that are converted to extraction-log entries
in the exact shape the existing aggregators read (top-level `adr_signals` /
`competitor_signals` + `date` + `source`). Deterministic facts (EDGAR form
types, pricing/size regexes) are adr_filing_detector's job — where both see
the same fact, the deterministic entry is equally present and the aggregator
resolves normally. Fail-quiet: any API error skips the lane for the run and
logs; there is no free-form fallback for this lane by design. Same-day dedup
by (source_id, signal group). Cost: ~$0.01–0.03/run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from anthropic import Anthropic

from src.config import (
    HAIKU_MODEL,
    LLM_TEMPERATURE,
    USE_NEWS_EVENT_EXTRACTOR,
)

logger = logging.getLogger(__name__)

EXTRACTOR_TAG = "news_event_extractor_v3.41.13"
MAX_ITEMS = 150
MAX_TOKENS = 8000

_NULLABLE_BOOL = {"type": ["boolean", "null"]}
_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_NUM = {"type": ["number", "null"]}

_ADR_PROPS = {
    "f1_canceled": _NULLABLE_BOOL,
    "underwriter_syndicate": {"type": ["array", "null"], "items": {"type": "string"}},
    "roadshow_active": _NULLABLE_BOOL,
    "demand_classification": {"type": ["string", "null"], "enum": ["strong", "normal", "weak", None]},
    "pricing_day": _NULLABLE_STR,
    "pricing_discount_pct": _NULLABLE_NUM,
    "final_size_usd_b": _NULLABLE_NUM,
    "insider_sale_event": {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": {
            "insider": {"type": "string"},
            "role": {"type": "string"},
            "usd_m": {"type": "number"},
            "date": {"type": "string"},
        },
        "required": ["insider", "role", "usd_m", "date"],
    },
}

_COMPETITOR_PROPS = {
    "us_export_controls_easing_event": _NULLABLE_BOOL,
    "cxmt_oem_mainstream_win": _NULLABLE_STR,
    "samsung_hbm4e_announcement": _NULLABLE_BOOL,
    "amd_samsung_hbm4_supply_mou": _NULLABLE_BOOL,
    "jedec_hbf_spec_ratified": _NULLABLE_BOOL,
    "sk_hynix_solidigm_hbf_integration": _NULLABLE_BOOL,
    "sandisk_kioxia_hbf_milestone": _NULLABLE_STR,
    "hbf_hyperscaler_design_win": _NULLABLE_STR,
    "hbf_hot_tier_displacement": _NULLABLE_BOOL,
}

_CAPEX_PROPS = {
    "company": {"type": ["string", "null"]},
    "guides_2027_capex_above_2026": _NULLABLE_BOOL,
    "capex_2026_guidance_usd_b": _NULLABLE_NUM,
    "note": _NULLABLE_STR,
}

NEWS_EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "extractions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "news_index": {"type": "integer"},
                    "adr_signals": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": _ADR_PROPS,
                        "required": list(_ADR_PROPS.keys()),
                    },
                    "competitor_signals": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": _COMPETITOR_PROPS,
                        "required": list(_COMPETITOR_PROPS.keys()),
                    },
                    "capex_signals": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": _CAPEX_PROPS,
                        "required": list(_CAPEX_PROPS.keys()),
                    },
                },
                "required": ["news_index", "adr_signals", "competitor_signals",
                             "capex_signals"],
            },
        }
    },
    "required": ["extractions"],
}

_PROMPT_HEADER = """You are extracting STRUCTURED EVENT SIGNALS from today's memory-semiconductor news feed for a cycle-tracking framework. One pass over the numbered items below.

Extract ONLY facts the text explicitly states. If an item states no listed signal, do not emit an extraction object for it. Never infer, never extrapolate from general commentary. Dates as YYYY-MM-DD. Sizes in USD billions (26.5 for "$26.5 billion"). Only emit an adr_signals or competitor_signals object when at least one of its fields is non-null; otherwise use null for that whole object.

SIGNALS — ADR (SK Hynix US listing lifecycle): f1_canceled (offering pulled/withdrawn); underwriter_syndicate (named banks); roadshow_active; demand_classification strong|normal|weak (oversubscription/books language); pricing_day; pricing_discount_pct (discount to prior close); final_size_usd_b; insider_sale_event (a NAMED insider selling, with USD millions and date).
SIGNALS — CapEx guidance (FIRST-PARTY prints only — never analyst forecasts): company (MSFT|GOOG|Alphabet|META|AMZN|Amazon|ORCL|Oracle); guides_2027_capex_above_2026 (true ONLY if the company's own print/guidance states 2027 capex above/increasing vs 2026); capex_2026_guidance_usd_b (company-guided 2026 number if stated, in USD billions); note (short quote).
SIGNALS — Competitor: us_export_controls_easing_event (BIS easing re China memory equipment); cxmt_oem_mainstream_win (named OEM adopting CXMT for mainstream products); samsung_hbm4e_announcement; amd_samsung_hbm4_supply_mou; jedec_hbf_spec_ratified; sk_hynix_solidigm_hbf_integration; sandisk_kioxia_hbf_milestone (milestone description); hbf_hyperscaler_design_win ("<hyperscaler>+<accelerator>+<tier>"); hbf_hot_tier_displacement (HBM displaced in the HOT tier specifically).

TODAY'S ITEMS:
"""


def _build_prompt(news_items: list) -> str:
    lines = []
    for i, n in enumerate(news_items[:MAX_ITEMS]):
        title = (getattr(n, "title", "") or "").strip()
        summary = (getattr(n, "summary", "") or "").strip()[:300]
        source = (getattr(n, "source", "") or "").strip()
        pub = str(getattr(n, "published", "") or "")[:10]
        lines.append(f"[{i}] ({source} | {pub}) {title}" + (f" — {summary}" if summary else ""))
    return _PROMPT_HEADER + "\n".join(lines)


_ENUM_SOURCES = ("reuters", "bloomberg", "korean_press", "sec_edgar", "dart")


def _source_label(raw: str) -> str:
    low = (raw or "").lower()
    for s in _ENUM_SOURCES:
        if s.split("_")[0] in low:
            return s
    if "korea" in low or "yonhap" in low or "chosun" in low or "hankyung" in low:
        return "korean_press"
    return low or "news"


def _entries_from_extractions(parsed: dict, news_items: list, now_iso: str) -> list[dict]:
    entries: list[dict] = []
    for ex in parsed.get("extractions") or []:
        if not isinstance(ex, dict):
            continue
        idx = ex.get("news_index")
        if not isinstance(idx, int) or not (0 <= idx < min(len(news_items), MAX_ITEMS)):
            continue
        item = news_items[idx]
        url = (getattr(item, "url", "") or "").strip()
        title = (getattr(item, "title", "") or "").strip()
        pub_iso = str(getattr(item, "published", "") or now_iso)
        for key in ("adr_signals", "competitor_signals", "capex_signals"):
            group = ex.get(key)
            if not isinstance(group, dict):
                continue
            filtered = {k: v for k, v in group.items() if v is not None and v is not False}
            if not filtered:
                continue
            entries.append({
                "source_id": f"newsevent:{key}:{(url or title)[:120]}",
                "source_type": "news_article",
                "published_date_iso": pub_iso,
                "extracted_at": now_iso,
                "date": now_iso,
                "source": _source_label(getattr(item, "source", "")),
                "detector": EXTRACTOR_TAG,
                "evidence": title[:200],
                key: filtered,
            })
    return entries


def extract_and_log_news_events(
    api_key: Optional[str],
    news_items: Optional[list],
    log_dir: Path = Path("state"),
    client: Optional[Any] = None,
) -> dict:
    """One structured-output Haiku pass over the run's news; write entries.

    Returns a summary dict (always logged, including zeros). Fail-quiet:
    disabled flag, missing key, empty news, API error, or schema surprise all
    degrade to a logged no-op — the deterministic detector and the rest of
    the run are unaffected.
    """
    summary = {"ran": False, "entries": 0, "skipped_dedup": 0, "reason": ""}
    try:
        if not USE_NEWS_EVENT_EXTRACTOR:
            summary["reason"] = "disabled"
        elif not api_key and client is None:
            summary["reason"] = "no_api_key"
        elif not news_items:
            summary["reason"] = "no_news"
        if summary["reason"]:
            logger.info(f"[event-extract] skipped ({summary['reason']})")
            return summary

        prompt = _build_prompt(news_items)
        cl = client or Anthropic(api_key=api_key)
        try:
            response = cl.messages.create(
                model=HAIKU_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
                extra_body={
                    "output_config": {
                        "format": {"type": "json_schema", "schema": NEWS_EVENT_SCHEMA}
                    }
                },
            )
        except Exception as e:
            logger.warning(
                f"[event-extract] structured call failed ({e}); skipping this "
                f"run (deterministic detector unaffected)"
            )
            summary["reason"] = "api_error"
            return summary

        summary["ran"] = True
        stop_reason = getattr(response, "stop_reason", None)
        parsed = json.loads(response.content[0].text)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = _entries_from_extractions(parsed, list(news_items), now_iso)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = log_dir / f"extraction_log_{today}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        seen: set = set()
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                try:
                    sid = json.loads(line).get("source_id")
                    if sid:
                        seen.add(sid)
                except Exception:
                    continue
        new_entries = []
        for e in entries:
            if e["source_id"] in seen:
                summary["skipped_dedup"] += 1
                continue
            new_entries.append(e)
            seen.add(e["source_id"])
        if new_entries:
            with open(log_path, "a") as f:
                for e in new_entries:
                    f.write(json.dumps(e) + "\n")
        summary["entries"] = len(new_entries)
        logger.info(
            f"[event-extract] entries={summary['entries']} "
            f"dedup_skipped={summary['skipped_dedup']} stop_reason={stop_reason}"
        )
    except Exception as e:
        logger.warning(f"[event-extract] failed (non-fatal): {e}")
        summary["reason"] = summary["reason"] or "error"
    return summary
