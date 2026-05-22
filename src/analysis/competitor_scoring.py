"""Indicator 7 (Competitor dynamics) — composite scoring.

v3.4.4.1 (2026-05-22) architecture:
  - Reads `state/competitor_state.json` for current measured values
    (auto-updated by src/analysis/competitor_aggregator.py before each run)
  - Reads `data/competitor_thresholds.yaml` for schema + threshold metadata
    (stable, never auto-modified — used for display formatting)

Composite logic (unchanged from v3.4.4):
  Each sub-indicator (7A/7B/7C/7D) carries an estimated SK Hynix EPS impact
  pct, mapped to:
      Not fired       — <5% EPS impact
      Partially fired — 5-20% EPS impact
      Fired           — >20% EPS impact

  Indicator-level composite:
    1. Cumulative-EPS path
    2. Part 14 dual-threat amplifier (2+ Fired → Fired; 1 Fired + 2+ Partial → Fired)

Note on the v3.4.4 → v3.4.4.1 split: the v3.4.4 module read the full
sub-state from `data/competitor_thresholds.yaml` directly (mixing
thresholds + measured values in one file, with a "hand-curated monthly
refresh" comment that didn't match the actual auto-update goal). v3.4.4.1
separates concerns: this module reads measured values from the state file
which the aggregator auto-updates from Layer 1 extraction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/competitor_thresholds.yaml")
STATE_FILE = Path("state/competitor_state.json")

STATUS_NOT_FIRED = "Not fired"
STATUS_PARTIALLY_FIRED = "Partially fired"
STATUS_FIRED = "Fired"

STATUS_COLORS = {
    STATUS_FIRED: "#B91C1C",
    STATUS_PARTIALLY_FIRED: "#FAC775",
    STATUS_NOT_FIRED: "#C0DD97",
}


@dataclass
class SubIndicatorState:
    sub_id: str
    display_name: str
    status: str
    estimated_eps_impact_pct: float
    current_state_summary: str
    sources: list[str] = field(default_factory=list)
    moat_extension_milestones: list[str] = field(default_factory=list)
    substitution_risk_milestones: list[str] = field(default_factory=list)


@dataclass
class CompetitorSnapshot:
    composite_status: str
    composite_status_color: str
    composite_reasoning: str
    cumulative_eps_impact_pct: float
    subs: list[SubIndicatorState]
    dual_threat_active: bool
    dual_threat_scenario_text: str
    dual_threat_impact_combined_pct: str
    dual_threat_impact_with_peak_pct: str
    last_aggregator_run: str


def load_competitor_thresholds(path: Path = THRESHOLDS_FILE) -> dict:
    """Load thresholds + metadata YAML (schema only — no measured values)."""
    with open(path) as f:
        return yaml.safe_load(f)


def load_competitor_state(path: Path = STATE_FILE) -> dict:
    """Load current measured values from state file (auto-updated by aggregator)."""
    with open(path) as f:
        return json.load(f)


def _build_sub_7a_summary(cs: dict) -> str:
    """CXMT — describe current DDR5 mainstream-win + share state."""
    mainstream = cs.get("cxmt_oem_mainstream_wins", []) or []
    lowend = cs.get("cxmt_oem_lowend_wins", []) or []
    ddr5_share = cs.get("cxmt_ddr5_share_pct", 0) or 0
    total = cs.get("cxmt_total_dram_share_pct", 0) or 0
    if mainstream:
        return (
            f"CXMT DDR5 mainstream OEM win: {', '.join(mainstream)}. "
            f"Estimated DDR5 share {ddr5_share}%. Binary trigger fired — "
            f"mainstream Tier-1 OEM adoption breaks commodity DRAM pricing structure."
        )
    if lowend:
        return (
            f"CXMT low-end OEM presence: {', '.join(lowend)}. Mainstream adoption "
            f"not yet announced. Estimated DDR5 share {ddr5_share}%. Watch — "
            f"low-end wins are not-fire signals."
        )
    return (
        f"CXMT total DRAM share ~{total}% (estimated DDR5 share ~{ddr5_share}%). "
        f"Yield parity on DDR5 expected late 2026 per industry estimates. No "
        f"Tier-1 OEM mainstream DDR5 win. DDR4 share gains are irrelevant "
        f"(SK Hynix EOL'ing DDR4 to ~20% of 2025 levels)."
    )


def _build_sub_7b_summary(cs: dict) -> str:
    """Samsung HBM4 — describe Nvidia share + yields + margin convergence."""
    sk_share = cs.get("sk_hynix_hbm4_nvidia_share_pct", "?")
    s_share = cs.get("samsung_hbm4_nvidia_share_pct", "?")
    s_yield = cs.get("samsung_hbm4_yield_pct", "?")
    sk_margin = cs.get("hbm_gross_margin_pct_sk_hynix", "?")
    s_margin = cs.get("hbm_gross_margin_pct_samsung", "?")
    qual = cs.get("hbm4e_qualification_lead", "tied")
    return (
        f"Nvidia HBM4 allocation: SK Hynix {sk_share}%, Samsung {s_share}% "
        f"(captured ~28-30% of Vera Rubin). Samsung HBM4 yield ~{s_yield}% "
        f"vs SK Hynix mature; HBM4E qualification status: {qual}. HBM gross "
        f"margins: SK Hynix ~{sk_margin}% vs Samsung ~{s_margin}% — convergence "
        f"watch is the most reliable corroborator. Samsung unveiled HBM4E at "
        f"GTC 2026 at 16Gbps/pin."
    )


def _build_sub_7c_summary(cs: dict) -> str:
    """Micron — share growth source + Vera Rubin status."""
    share = cs.get("micron_total_hbm_share_pct", "?")
    vr_status = cs.get("micron_vera_rubin_design_win", "excluded")
    source = cs.get("micron_hbm_share_growth_source", "samsung")
    vr_text = {
        "excluded": "excluded from Vera Rubin",
        "secondary_sku": "WON Vera Rubin secondary SKU",
        "primary_sku": "WON Vera Rubin primary SKU",
    }.get(vr_status, vr_status)
    return (
        f"Micron HBM4 share ~{share}% (Counterpoint), {vr_text}. Critical nuance: "
        f"Micron's HBM share growth is mostly coming from {source.upper()}'s "
        f"portion, not SK Hynix's. Indicator 7C only fires if Micron eats "
        f"specifically SK Hynix share — different from raw share growth tracking."
    )


def _build_sub_7d_summary(cs: dict) -> str:
    """HBF — stage + SK Hynix Solidigm angle."""
    stage = cs.get("hbf_commercial_stage", "pre_commercial")
    jedec = cs.get("jedec_hbf_spec_ratified", False)
    hot_wins = cs.get("hbf_hot_tier_substitution_events", []) or []
    sk_roadmap = cs.get("sk_hynix_hbf_public_roadmap", False)
    parts = [f"HBF commercial stage: {stage.replace('_', ' ')}."]
    if jedec:
        parts.append("JEDEC HBF spec ratified.")
    else:
        parts.append("JEDEC HBF spec NOT yet ratified.")
    if hot_wins:
        parts.append(f"{len(hot_wins)} hot-tier HBM displacement event(s) — fire-relevant.")
    else:
        parts.append(
            "No hot-tier HBM displacement events. Any cold-tier HBF additions "
            "are demand-unlocking, not displacing."
        )
    if sk_roadmap:
        parts.append("SK Hynix HBF roadmap publicly disclosed (moat-extension active).")
    else:
        parts.append(
            "SK Hynix HBF roadmap NOT public — Solidigm NAND base + HBM packaging "
            "expertise positions them to lead, but execution evidence absent."
        )
    return " ".join(parts)


def compute_competitor_snapshot(
    thresholds: Optional[dict] = None,
    state: Optional[dict] = None,
) -> Optional[CompetitorSnapshot]:
    """Build the composite Indicator 7 snapshot.

    Args:
        thresholds: optional pre-loaded thresholds dict (for testing); if None,
            loaded from data/competitor_thresholds.yaml.
        state: optional pre-loaded state dict (for testing); if None, loaded
            from state/competitor_state.json (which the aggregator updates).

    Returns CompetitorSnapshot or None if either file is missing.
    """
    if thresholds is None:
        try:
            thresholds = load_competitor_thresholds()
        except FileNotFoundError:
            logger.warning("competitor_thresholds.yaml missing — skipping Indicator 7")
            return None
    if state is None:
        try:
            state = load_competitor_state()
        except FileNotFoundError:
            logger.warning("competitor_state.json missing — skipping Indicator 7")
            return None

    # Build sub states from STATE file (current measured values) and THRESHOLDS
    # (display metadata only — sources, moat/risk milestones).
    sub_configs = [
        ("7A", "sub_7a_cxmt", _build_sub_7a_summary),
        ("7B", "sub_7b_samsung_hbm4", _build_sub_7b_summary),
        ("7C", "sub_7c_micron", _build_sub_7c_summary),
        ("7D", "sub_7d_hbf", _build_sub_7d_summary),
    ]
    subs = []
    for sub_id, sub_key, summary_fn in sub_configs:
        sub_state = state.get(sub_key, {}) or {}
        sub_threshold = thresholds.get(sub_key, {}) or {}
        cs = sub_state.get("current_state", {}) or {}
        subs.append(SubIndicatorState(
            sub_id=sub_id,
            display_name=sub_threshold.get("display_name", sub_id),
            status=sub_state.get("current_status", STATUS_NOT_FIRED),
            estimated_eps_impact_pct=float(sub_state.get("estimated_eps_impact_pct", 0)),
            current_state_summary=summary_fn(cs),
            sources=sub_threshold.get("sources", []) or [],
            moat_extension_milestones=sub_threshold.get("moat_extension_milestones", []) or [],
            substitution_risk_milestones=sub_threshold.get("substitution_risk_milestones", []) or [],
        ))

    # Composite logic
    cumulative_eps = sum(s.estimated_eps_impact_pct for s in subs)
    n_fired = sum(1 for s in subs if s.status == STATUS_FIRED)
    n_partial = sum(1 for s in subs if s.status == STATUS_PARTIALLY_FIRED)

    dual_threat_active = (n_fired >= 2) or (n_fired == 1 and n_partial >= 2)

    if dual_threat_active:
        composite_status = STATUS_FIRED
        composite_reasoning = (
            f"Dual-threat amplifier active ({n_fired} subs Fired, {n_partial} subs "
            f"Partially fired). Multi-front compression per Part 14: combined "
            f"stock impact -30 to -45%; with cycle-peak overlay -60 to -75%."
        )
    elif cumulative_eps >= 20.0 or n_fired >= 1:
        composite_status = STATUS_FIRED
        composite_reasoning = (
            f"Single-front Fired sub OR cumulative EPS impact ~{cumulative_eps:.0f}% "
            f"exceeds 20% threshold. {n_fired} sub(s) Fired, {n_partial} Partially fired."
        )
    elif cumulative_eps >= 10.0 or n_partial >= 1:
        composite_status = STATUS_PARTIALLY_FIRED
        composite_reasoning = (
            f"Partial-front pressure: cumulative EPS impact ~{cumulative_eps:.0f}%; "
            f"{n_partial} sub(s) Partially fired. Material but not thesis-breaking."
        )
    else:
        composite_status = STATUS_NOT_FIRED
        composite_reasoning = (
            f"All sub-indicators benign. Cumulative EPS impact ~{cumulative_eps:.1f}%. "
            f"No competitor scenarios crossing fire thresholds."
        )

    dual_threat_yaml = thresholds.get("dual_threat_scenario", {}) or {}

    return CompetitorSnapshot(
        composite_status=composite_status,
        composite_status_color=STATUS_COLORS[composite_status],
        composite_reasoning=composite_reasoning,
        cumulative_eps_impact_pct=cumulative_eps,
        subs=subs,
        dual_threat_active=dual_threat_active,
        dual_threat_scenario_text=dual_threat_yaml.get("narrative", "") or "",
        dual_threat_impact_combined_pct=str(
            dual_threat_yaml.get("stock_impact_combined_pct", "-30 to -45")
        ),
        dual_threat_impact_with_peak_pct=str(
            dual_threat_yaml.get("stock_impact_with_cycle_peak_overlay_pct", "-60 to -75")
        ),
        last_aggregator_run=state.get("_metadata", {}).get("last_aggregator_run", "unknown"),
    )
