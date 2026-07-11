"""Indicator 8 (US ADR / SPO event) — Layer 1 extraction aggregator.

Reads accumulated extraction log entries from the past N days, extracts
ADR-relevant signals (F-1 filing detection, roadshow demand language,
424B pricing discount, insider sales >$10M in window), and updates
`state/adr_state.json` with refreshed lifecycle phase + sub statuses.

Runs as a daily step in the fast-tier pipeline BEFORE
`compute_adr_snapshot()`. State file is the only source of truth for
current event status; `data/adr_event_tracking.yaml` is thresholds +
calendar template + academic refs only.

Safe-failing — exception or empty extraction log preserves prior state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/adr_event_tracking.yaml")
STATE_FILE = Path("state/adr_state.json")
EXTRACTION_LOG_DIR = Path("state")
EXTRACTION_LOG_GLOB = "extraction_log_*.jsonl"

# Underwriter tier classification (kept in sync with the YAML)
TIER_A_UNDERWRITERS = {"goldman sachs", "morgan stanley", "jpmorgan", "j.p. morgan", "citi", "citigroup"}
TIER_B_UNDERWRITERS = {"bofa", "bank of america", "mizuho", "nomura", "daiwa", "ubs", "credit suisse"}


def _underwriter_lead_tier(syndicate: list[str]) -> Optional[str]:
    """Determine lead-underwriter tier from syndicate list."""
    if not syndicate:
        return None
    syn_lower = [s.lower() for s in syndicate]
    if any(any(t in s for t in TIER_A_UNDERWRITERS) for s in syn_lower):
        return "A"
    if any(any(t in s for t in TIER_B_UNDERWRITERS) for s in syn_lower):
        return "B"
    return "C"


def _iter_extraction_log_entries(lookback_days: int) -> list[dict]:
    entries = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    for path in sorted(EXTRACTION_LOG_DIR.glob(EXTRACTION_LOG_GLOB)):
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts_str = e.get("date") or e.get("extracted_at") or e.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if ts >= cutoff:
                    entries.append(e)
        except Exception as ex:
            logger.warning(f"Could not read extraction log {path}: {ex}")
    return entries


def _load_thresholds() -> dict:
    with open(THRESHOLDS_FILE) as f:
        return yaml.safe_load(f)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        logger.warning(f"{STATE_FILE} missing — using empty pre-event skeleton")
        return {
            "_metadata": {"schema_version": "2.0"},
            "lifecycle_phase": "pre_event",
            "composite_status": "Not fired",
            "composite_status_reason": "Pre-F-1; state file initialized empty.",
            "peak_coincidence_active": False,
            "sub_8a_f1_filing": {"current_status": "Pre-event", "current_state": {"f1_filed": False, "f1_canceled": False, "underwriter_syndicate": []}, "evidence_log": []},
            "sub_8b_roadshow_demand": {"current_status": "Pre-event", "current_state": {"roadshow_active": False, "demand_classification": None}, "evidence_log": []},
            "sub_8c_pricing_day": {"current_status": "Pre-event", "current_state": {"pricing_day": None, "pricing_discount_pct": None}, "evidence_log": []},
            "sub_8d_insider_sales_in_window": {"current_status": "Pre-event", "current_state": {"insider_sales_in_window": [], "insider_sales_total_usd_m_in_window": 0}, "evidence_log": []},
        }
    with open(STATE_FILE) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def _apply_extractions(state: dict, entries: list[dict]) -> tuple[dict, list[str]]:
    """Update state from extraction entries.

    Expected extraction shape:
        {
            "date": "...",
            "source": "sec_edgar | reuters | bloomberg | korean_press | sec_form_4 | dart",
            "adr_signals": {
                "f1_filed": bool | null,
                "f1_canceled": bool | null,
                "underwriter_syndicate": ["Goldman Sachs", ...] | null,
                "roadshow_active": bool | null,
                "demand_classification": "strong" | "normal" | "weak" | null,
                "pricing_day": "2026-06-15" | null,
                "pricing_discount_pct": numeric | null,
                "final_size_usd_b": numeric | null,
                "insider_sale_event": {"insider": "...", "role": "...", "usd_m": ..., "date": "..."} | null
            }
        }
    """
    changes = []

    most_recent_pricing_discount: Optional[float] = None
    most_recent_pricing_day: Optional[str] = None
    most_recent_final_size: Optional[float] = None
    most_recent_demand: Optional[str] = None
    insider_sale_events: list[dict] = state.get("sub_8d_insider_sales_in_window", {}).get(
        "current_state", {}
    ).get("insider_sales_in_window", []) or []

    f1_filed_observed = False
    f1_canceled_observed = False
    roadshow_active_observed = False
    syndicate: list[str] = state.get("sub_8a_f1_filing", {}).get("current_state", {}).get(
        "underwriter_syndicate", []
    ) or []

    for entry in entries:
        sigs = entry.get("adr_signals") or {}
        if not sigs:
            continue

        if sigs.get("f1_filed") is True:
            f1_filed_observed = True
            # v3.41.14 — the FILING date, not the extraction date. Entries
            # re-observing the same F-1 (the form sits in EDGAR's 30-day
            # feed, so the deterministic detector re-emits daily) previously
            # overwrote f1_filing_date with each run's timestamp — the June
            # 12 filing displayed as "filed <this morning>". Prefer the
            # entry's published date, and only ever move the recorded date
            # EARLIER: the first filing date wins.
            fdate = (
                entry.get("published_date_iso")
                or entry.get("date")
                or entry.get("extracted_at")
            )
            if fdate:
                cs = state.setdefault("sub_8a_f1_filing", {}).setdefault(
                    "current_state", {}
                )
                existing = cs.get("f1_filing_date")
                if not existing or str(fdate) < str(existing):
                    cs["f1_filing_date"] = fdate

        if sigs.get("f1_canceled") is True:
            f1_canceled_observed = True

        syndicate_obs = sigs.get("underwriter_syndicate") or []
        for u in syndicate_obs:
            if u not in syndicate:
                syndicate.append(u)
                changes.append(f"sub_8a_f1_filing.underwriter_syndicate: appended {u!r}")

        if sigs.get("roadshow_active") is True:
            roadshow_active_observed = True

        if sigs.get("demand_classification"):
            most_recent_demand = sigs["demand_classification"]

        if sigs.get("pricing_day"):
            most_recent_pricing_day = sigs["pricing_day"]

        if sigs.get("pricing_discount_pct") is not None:
            most_recent_pricing_discount = float(sigs["pricing_discount_pct"])

        if sigs.get("final_size_usd_b") is not None:
            most_recent_final_size = float(sigs["final_size_usd_b"])

        if sigs.get("insider_sale_event"):
            event = sigs["insider_sale_event"]
            # De-dup by (insider, date)
            key = (event.get("insider"), event.get("date"))
            if key not in {(e.get("insider"), e.get("date")) for e in insider_sale_events}:
                insider_sale_events.append(event)
                changes.append(f"sub_8d.insider_sales_in_window: appended {event}")

    def _set(sub_key: str, field_name: str, value):
        sub = state.setdefault(sub_key, {"current_state": {}, "evidence_log": []})
        cs = sub.setdefault("current_state", {})
        old = cs.get(field_name)
        if old != value:
            cs[field_name] = value
            changes.append(f"{sub_key}.{field_name}: {old} → {value}")

    if f1_filed_observed:
        _set("sub_8a_f1_filing", "f1_filed", True)
    if f1_canceled_observed:
        _set("sub_8a_f1_filing", "f1_canceled", True)
    if syndicate:
        _set("sub_8a_f1_filing", "underwriter_syndicate", syndicate)
        _set("sub_8a_f1_filing", "underwriter_lead_tier", _underwriter_lead_tier(syndicate))
    if roadshow_active_observed:
        _set("sub_8b_roadshow_demand", "roadshow_active", True)
    if most_recent_demand:
        _set("sub_8b_roadshow_demand", "demand_classification", most_recent_demand)
    if most_recent_pricing_day:
        _set("sub_8c_pricing_day", "pricing_day", most_recent_pricing_day)
    if most_recent_pricing_discount is not None:
        _set("sub_8c_pricing_day", "pricing_discount_pct", most_recent_pricing_discount)
    if most_recent_final_size is not None:
        _set("sub_8c_pricing_day", "final_size_usd_b", most_recent_final_size)
    if insider_sale_events:
        total = sum(float(e.get("usd_m", 0)) for e in insider_sale_events)
        _set("sub_8d_insider_sales_in_window", "insider_sales_in_window", insider_sale_events)
        _set("sub_8d_insider_sales_in_window", "insider_sales_total_usd_m_in_window", total)

    return state, changes


def _determine_lifecycle_phase(state: dict) -> str:
    """Derive lifecycle phase from sub states. Returns one of pre_event, f1_filed,
    roadshow, pricing, trading, completed, canceled."""
    s_8a = state.get("sub_8a_f1_filing", {}).get("current_state", {})
    s_8b = state.get("sub_8b_roadshow_demand", {}).get("current_state", {})
    s_8c = state.get("sub_8c_pricing_day", {}).get("current_state", {})

    if s_8a.get("f1_canceled"):
        return "canceled"
    if s_8c.get("pricing_day"):
        return "trading" if s_8c.get("final_size_usd_b") is not None else "pricing"
    if s_8b.get("roadshow_active") or s_8b.get("demand_classification"):
        return "roadshow"
    if s_8a.get("f1_filed"):
        return "f1_filed"
    return "pre_event"


def _evaluate_sub_status(sub_key: str, sub_state: dict, phase: str) -> tuple[str, str]:
    """Per-sub status mapping. Returns (status, status_pill_label)."""
    cs = sub_state.get("current_state", {})

    if sub_key == "sub_8a_f1_filing":
        if cs.get("f1_canceled"):
            return ("Fired", "F-1 canceled — sustained weakness")
        if cs.get("f1_filed"):
            # v3.39.10 — a CONFIRMED F-1 filing is itself the peak-coincidence
            # event (Part 11: SK Hynix 1995 listing → 1996 crash). It registers
            # as at least "Partially fired" regardless of syndicate. Underwriter
            # syndicate quality then modulates: a tier-C-only (weak) syndicate
            # escalates to Fired (desperate issuance); tier-A/B or
            # not-yet-disclosed stays Partially fired. Prior logic returned
            # "Not fired" for a filed-but-unsyndicated F-1, which contradicted
            # both the framework spec ("pending filing is Partial") and the
            # tier-table/exec status — see the 2026-06-06 brief inconsistency.
            lead = cs.get("underwriter_lead_tier")
            if lead == "C":
                return ("Fired", "F-1 filed with tier-C-only syndicate — weak/desperate issuance")
            if lead == "A":
                return ("Partially fired", "F-1 filed — peak-coincidence signal; tier-A syndicate lead")
            if lead == "B":
                return ("Partially fired", "F-1 filed — peak-coincidence signal; tier-B syndicate lead")
            return ("Partially fired", "F-1 filed — peak-coincidence signal; syndicate pending disclosure")
        return ("Pre-event", "Awaiting F-1 — expected June-July 2026")

    if sub_key == "sub_8b_roadshow_demand":
        demand = cs.get("demand_classification")
        if demand == "strong":
            return ("Not fired", "Roadshow demand strong (oversubscribed)")
        if demand == "normal":
            return ("Partially fired", "Roadshow demand normal")
        if demand == "weak":
            return ("Fired", "Roadshow demand weak / undersubscribed")
        return ("Pre-event", "Awaiting roadshow window")

    if sub_key == "sub_8c_pricing_day":
        discount = cs.get("pricing_discount_pct")
        if discount is None:
            return ("Pre-event", "Awaiting 424B pricing")
        if discount < 3:
            return ("Not fired", f"Pricing discount {discount:.1f}% — strong demand")
        if discount <= 5:
            return ("Partially fired", f"Pricing discount {discount:.1f}% — normal")
        return ("Fired", f"Pricing discount {discount:.1f}% — weak")

    if sub_key == "sub_8d_insider_sales_in_window":
        total = cs.get("insider_sales_total_usd_m_in_window", 0) or 0
        if total >= 25:
            return ("Fired", f"Insider sales ${total:.1f}M in window")
        if total >= 10:
            return ("Partially fired", f"Insider sales ${total:.1f}M in window")
        return ("Pre-event", "Monitoring SEC Form 4 + Korean DART")

    return ("Pre-event", "")


def aggregate_adr_extractions(lookback_days: Optional[int] = None) -> dict:
    """Main entry. Updates state/adr_state.json from extraction logs."""
    try:
        thresholds = _load_thresholds()
    except FileNotFoundError:
        logger.warning("data/adr_event_tracking.yaml missing — aggregator skipped")
        return {}

    if lookback_days is None:
        lookback_days = thresholds.get("aggregator_config", {}).get("extraction_log_lookback_days", 30)

    state = _load_state()

    try:
        entries = _iter_extraction_log_entries(lookback_days)
        if entries:
            state, changes = _apply_extractions(state, entries)
            logger.info(
                f"ADR aggregator: {len(entries)} extraction entries; "
                f"{len(changes)} state changes."
            )
            for ch in changes[:10]:
                logger.info(f"  • {ch}")
        else:
            logger.info(f"ADR aggregator: no extraction entries in trailing {lookback_days}d; state preserved.")

        # Derive lifecycle phase from sub states
        new_phase = _determine_lifecycle_phase(state)
        old_phase = state.get("lifecycle_phase")
        state["lifecycle_phase"] = new_phase
        if old_phase and old_phase != new_phase:
            logger.info(f"  ADR lifecycle phase: {old_phase} → {new_phase}")

        # Re-evaluate sub statuses
        for sub_key in ("sub_8a_f1_filing", "sub_8b_roadshow_demand",
                        "sub_8c_pricing_day", "sub_8d_insider_sales_in_window"):
            sub = state.setdefault(sub_key, {"current_state": {}})
            new_status, new_label = _evaluate_sub_status(sub_key, sub, new_phase)
            old_status = sub.get("current_status")
            sub["current_status"] = new_status
            sub["status_pill_label"] = new_label
            sub["status_evaluated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            sub["status_evaluator"] = "adr_aggregator"
            if old_status and old_status != new_status:
                logger.info(f"  {sub_key} status: {old_status} → {new_status}")

        state["_metadata"]["last_aggregator_run"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _save_state(state)
    except Exception as e:
        logger.error(f"ADR aggregator failed (state preserved): {e}", exc_info=True)

    return state
