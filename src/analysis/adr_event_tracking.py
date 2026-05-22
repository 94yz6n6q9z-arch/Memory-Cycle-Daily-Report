"""Indicator 8 (US ADR / SPO event) — composite scoring.

v3.4.4.1 (2026-05-22) architecture:
  - Reads `state/adr_state.json` for current lifecycle phase + sub statuses
    (auto-updated by src/analysis/adr_aggregator.py before each run)
  - Reads `data/adr_event_tracking.yaml` for schema + thresholds + calendar
    template + academic refs (stable, never auto-modified)

Composite logic (unchanged from v3.4.4):
  - Worst-of subs EXCEPT canceled F-1 = Fired, EXCEPT peak-coincidence
    escalation when ADR completion + late Phase 4 (per Part 11)
  - Fired persists through Phase 4 — no 90-day reversion

The v3.4.4 module read sub statuses directly from the YAML. v3.4.4.1
separates concerns: this module reads sub statuses from the state file
which the aggregator auto-updates from Layer 1 extraction (SEC EDGAR
F-1 detection, roadshow demand language, 424B pricing discount, insider
sale events from Form 4 + DART).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/adr_event_tracking.yaml")
STATE_FILE = Path("state/adr_state.json")

STATUS_NOT_FIRED = "Not fired"
STATUS_PARTIALLY_FIRED = "Partially fired"
STATUS_FIRED = "Fired"
STATUS_PRE_EVENT = "Pre-event"

STATUS_COLORS = {
    STATUS_FIRED: "#B91C1C",
    STATUS_PARTIALLY_FIRED: "#FAC775",
    STATUS_NOT_FIRED: "#C0DD97",
    STATUS_PRE_EVENT: "#B0BEC5",
}

PHASE_PRE_EVENT = "pre_event"
PHASE_F1_FILED = "f1_filed"
PHASE_ROADSHOW = "roadshow"
PHASE_PRICING = "pricing"
PHASE_TRADING = "trading"
PHASE_COMPLETED = "completed"
PHASE_CANCELED = "canceled"


@dataclass
class ADRSubState:
    sub_id: str
    display_name: str
    status: str
    status_pill_label: str
    what_we_watch: list[str]
    current_state_summary: str
    sources: list[str] = field(default_factory=list)


@dataclass
class ADRCalendarMilestone:
    date_window: str
    event: str
    sub_indicator: str
    mechanical_impact: str


@dataclass
class ADREventSnapshot:
    composite_status: str
    composite_status_color: str
    composite_reasoning: str
    lifecycle_phase: str
    subs: list[ADRSubState]
    calendar: list[ADRCalendarMilestone]
    confirmed_offering_structure: dict
    academic_calibration: dict
    peak_coincidence_active: bool
    last_aggregator_run: str


def load_adr_thresholds(path: Path = THRESHOLDS_FILE) -> dict:
    """Load thresholds + calendar + academic refs YAML (schema only)."""
    with open(path) as f:
        return yaml.safe_load(f)


def load_adr_state(path: Path = STATE_FILE) -> dict:
    """Load current lifecycle phase + sub statuses (aggregator-managed)."""
    with open(path) as f:
        return json.load(f)


def compute_adr_snapshot(
    thresholds: Optional[dict] = None,
    state: Optional[dict] = None,
    cycle_phase_4_late: bool = False,
) -> Optional[ADREventSnapshot]:
    """Build the Indicator 8 ADR snapshot.

    Args:
        thresholds: optional pre-loaded YAML (for tests)
        state: optional pre-loaded state (for tests)
        cycle_phase_4_late: triggers peak-coincidence escalation when True
            and lifecycle_phase is trading or completed

    Returns ADREventSnapshot or None.
    """
    if thresholds is None:
        try:
            thresholds = load_adr_thresholds()
        except FileNotFoundError:
            logger.warning("adr_event_tracking.yaml missing — skipping Indicator 8")
            return None
    if state is None:
        try:
            state = load_adr_state()
        except FileNotFoundError:
            logger.warning("adr_state.json missing — skipping Indicator 8")
            return None

    phase = state.get("lifecycle_phase", PHASE_PRE_EVENT)

    # Sub-states from state file (current_status + status_pill_label set by aggregator)
    sub_configs = [
        ("8A", "sub_8a_f1_filing"),
        ("8B", "sub_8b_roadshow_demand"),
        ("8C", "sub_8c_pricing_day"),
        ("8D", "sub_8d_insider_sales_in_window"),
    ]
    subs = []
    for sub_id, sub_key in sub_configs:
        sub_state = state.get(sub_key, {}) or {}
        sub_threshold = thresholds.get(sub_key, {}) or {}
        cs = sub_state.get("current_state", {}) or {}
        status = sub_state.get("current_status", STATUS_PRE_EVENT)
        subs.append(ADRSubState(
            sub_id=sub_id,
            display_name=sub_threshold.get("display_name", sub_id),
            status=status,
            status_pill_label=sub_state.get("status_pill_label", status),
            what_we_watch=_what_we_watch_for_sub(sub_id),
            current_state_summary=_build_sub_summary(sub_id, cs, phase),
            sources=sub_threshold.get("sources", []) or [],
        ))

    # Composite
    rank_map = {STATUS_NOT_FIRED: 0, STATUS_PRE_EVENT: 0,
                STATUS_PARTIALLY_FIRED: 1, STATUS_FIRED: 2}
    worst_rank = max(rank_map.get(s.status, 0) for s in subs)

    if phase == PHASE_CANCELED:
        composite_status = STATUS_FIRED
        composite_reasoning = (
            "F-1 filed then offering canceled. Per the framework session: a "
            "canceled ADR is a stronger weakness signal than a completed-but-weak "
            "ADR. Fired persists through Phase 4."
        )
        peak_coincidence_active = False
    elif phase == PHASE_PRE_EVENT:
        composite_status = STATUS_NOT_FIRED
        composite_reasoning = state.get(
            "composite_status_reason",
            "Pre-F-1. ADR expected June-July 2026 per Korean press."
        )
        peak_coincidence_active = False
    else:
        rank_to_status = {0: STATUS_NOT_FIRED, 1: STATUS_PARTIALLY_FIRED, 2: STATUS_FIRED}
        base_status = rank_to_status[worst_rank]
        peak_coincidence_active = (
            phase in (PHASE_TRADING, PHASE_COMPLETED) and cycle_phase_4_late
        )
        if peak_coincidence_active:
            composite_status = STATUS_FIRED
            composite_reasoning = (
                f"ADR completion coincident with late Phase 4 cycle state — "
                f"peak-coincidence escalation per Part 11 (SK Hynix 1995 / 1996 "
                f"crash pattern). Worst-of subs was {base_status}; escalated to "
                f"Fired by peak coincidence."
            )
        else:
            composite_status = base_status
            composite_reasoning = (
                f"Worst-of-subs: {base_status}. Phase: {phase}. No peak-coincidence "
                f"escalation active."
            )

    calendar = [
        ADRCalendarMilestone(
            date_window=m["date_window"],
            event=m["event"],
            sub_indicator=m["sub_indicator"],
            mechanical_impact=m["mechanical_impact"],
        )
        for m in (thresholds.get("calendar_milestones", []) or [])
    ]

    return ADREventSnapshot(
        composite_status=composite_status,
        composite_status_color=STATUS_COLORS[composite_status],
        composite_reasoning=composite_reasoning,
        lifecycle_phase=phase,
        subs=subs,
        calendar=calendar,
        confirmed_offering_structure=thresholds.get("confirmed_offering_structure", {}) or {},
        academic_calibration=thresholds.get("academic_calibration", {}) or {},
        peak_coincidence_active=peak_coincidence_active,
        last_aggregator_run=state.get("_metadata", {}).get("last_aggregator_run", "unknown"),
    )


def _what_we_watch_for_sub(sub_id: str) -> list[str]:
    return {
        "8A": [
            "SEC EDGAR daily check for F-1 (and F-1/A amendments)",
            "Underwriter syndicate composition at filing",
            "Filing date itself (early = thorough; late = scramble)",
        ],
        "8B": [
            "Reuters / Bloomberg ADR demand coverage during roadshow",
            "Korean syndicate-side leaks (ChosunBiz, Korea Times)",
            "Layer 1 digester extraction of demand-language from coverage",
        ],
        "8C": [
            "SEC 424B prospectus filing (final pricing disclosure)",
            "Discount vs prior-close ADR-equivalent KRX price",
            "Final issuance size vs F-1 indicated range",
        ],
        "8D": [
            "SEC Form 4 filings during 30-day pre-pricing through completion window",
            "Korean DART disclosures (SK Hynix C-suite KRX-side sales)",
            "Dollar value of insider sales in window",
        ],
    }.get(sub_id, [])


def _build_sub_summary(sub_id: str, cs: dict, phase: str) -> str:
    """Per-sub current-state summary for the section 4 card."""
    if phase == PHASE_PRE_EVENT:
        return {
            "8A": (
                "Daily SEC EDGAR check for SK Hynix F-1 filing. Korean press "
                "(ChosunBiz, Korea Times) tracked in parallel for syndicate-side "
                "leaks (SEC filing beats Korean press by 1-3 days). No F-1 yet."
            ),
            "8B": (
                "Roadshow window has not opened. When it does, watch Reuters / "
                "Bloomberg ADR coverage for demand language: 'oversubscribed Nx' "
                "(strong), 'in line' (normal), 'demand soft' (weak)."
            ),
            "8C": (
                "424B pricing not yet filed. Watch for discount-to-prior-close: "
                "<3% strong demand; 3-5% normal; >5% weak (Fired threshold). "
                "Final-issuance-size vs F-1-indicated range also diagnostic."
            ),
            "8D": (
                "Monitoring SEC Form 4 + Korean DART for SK Hynix C-suite sales "
                "from 30 days pre-pricing through completion. $10M total window "
                "threshold per the May 22 framework decision."
            ),
        }.get(sub_id, "")

    # Phase-active summaries
    if sub_id == "8A":
        filed_date = cs.get("f1_filing_date") or "filed"
        if cs.get("f1_canceled"):
            return f"F-1 filed {filed_date} then canceled. Sustained weakness signal."
        tier = cs.get("underwriter_lead_tier", "?")
        synd = cs.get("underwriter_syndicate", []) or []
        return (
            f"F-1 filed {filed_date} with tier-{tier} underwriter lead. Syndicate: "
            f"{', '.join(synd) if synd else '(awaiting disclosure)'}."
        )
    if sub_id == "8B":
        demand = cs.get("demand_classification", "?")
        return f"Roadshow demand classification: {demand}."
    if sub_id == "8C":
        pday = cs.get("pricing_day", "?")
        disc = cs.get("pricing_discount_pct")
        size = cs.get("final_size_usd_b")
        return f"Pricing day {pday}; discount {disc}%; final size ${size}B."
    if sub_id == "8D":
        total = cs.get("insider_sales_total_usd_m_in_window", 0) or 0
        events = cs.get("insider_sales_in_window", []) or []
        return f"Insider sales in window: ${total:.1f}M total across {len(events)} event(s)."

    return ""


def cycle_phase_4_late_from_indicators(
    tier_assessments: Optional[list] = None,
    dhcr_snapshot: Optional[dict] = None,
) -> bool:
    """Determine if the broader cycle is in late Phase 4 for peak-coincidence.

    True if any of:
      - DHCR position score in warning band (≤40)
      - ≥3 Tier 2 indicators fired in trailing 30 days
      - Any Tier 3 indicator fired in trailing 30 days
    """
    if dhcr_snapshot and dhcr_snapshot.get("position_score", 100) <= 40:
        return True
    if tier_assessments is None:
        return False
    n_tier2_fired = sum(
        1 for a in tier_assessments
        if getattr(a, "tier", 0) == 2 and getattr(a, "current_status", "") == STATUS_FIRED
    )
    n_tier3_fired = sum(
        1 for a in tier_assessments
        if getattr(a, "tier", 0) == 3 and getattr(a, "current_status", "") == STATUS_FIRED
    )
    return n_tier2_fired >= 3 or n_tier3_fired >= 1
