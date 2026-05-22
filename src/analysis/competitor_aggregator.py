"""Indicator 7 (Competitor dynamics) — Layer 1 extraction aggregator.

Reads accumulated `state/extraction_log_*.jsonl` entries from the past
N days, extracts competitor-relevant signals, and updates
`state/competitor_state.json` with refreshed measured values + evidence
log entries. Re-evaluates each sub-indicator's status from updated state.

Runs as a daily step in the fast-tier pipeline BEFORE
`compute_competitor_snapshot()`. State file is the only source of truth
for current measured values; `data/competitor_thresholds.yaml` is
thresholds + metadata + extraction schema only.

v3.4.4.1 architecture (2026-05-22) — replaces the v3.4.4 "hand-curated
YAML" pattern. Bootstrap values were seeded once on v3.4.4.1 deploy;
thereafter this aggregator is the only writer to the state file.

The aggregator is safe-failing — exception or empty extraction log
preserves prior state. Layer 1 digester extraction is best-effort:
empty extractions stay empty; conflicting values resolve by source
tier (Tier A > B > C) then most-recent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/competitor_thresholds.yaml")
STATE_FILE = Path("state/competitor_state.json")
EXTRACTION_LOG_DIR = Path("state")
EXTRACTION_LOG_GLOB = "extraction_log_*.jsonl"

# Source authority tiers for conflict resolution.
TIER_A_SOURCES = {
    "sk_hynix_earnings", "samsung_earnings", "mirae_asset", "kb_securities",
    "micron_earnings", "nvidia_earnings",
}
TIER_B_SOURCES = {
    "counterpoint", "trendforce", "semianalysis",
}
TIER_C_SOURCES = {
    "digitimes", "chosunbiz", "korea_times", "ee_times", "tom's_hardware",
}


def _source_tier(source_str: str) -> int:
    """Map free-text source string to authority tier (1=Tier A, 2=B, 3=C, 4=unknown).
    Lower number = higher authority."""
    s = (source_str or "").lower()
    if any(t in s for t in TIER_A_SOURCES):
        return 1
    if any(t in s for t in TIER_B_SOURCES):
        return 2
    if any(t in s for t in TIER_C_SOURCES):
        return 3
    return 4


def _iter_extraction_log_entries(lookback_days: int) -> list[dict]:
    """Read all extraction log files in EXTRACTION_LOG_DIR matching the glob,
    return entries within the lookback window. Best-effort — missing files
    or malformed lines logged and skipped."""
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
                # Date check — entries should have a 'date' or 'extracted_at' field
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
    """Load data/competitor_thresholds.yaml (thresholds + extraction schema only)."""
    with open(THRESHOLDS_FILE) as f:
        return yaml.safe_load(f)


def _load_state() -> dict:
    """Load state/competitor_state.json (current measured values)."""
    if not STATE_FILE.exists():
        # First-run safety — return empty skeleton; bootstrap state should be
        # committed to repo, but if missing we still don't crash the pipeline.
        logger.warning(f"{STATE_FILE} missing — using empty skeleton")
        return {
            "_metadata": {"schema_version": "2.0"},
            "sub_7a_cxmt": {"current_status": "Not fired", "current_state": {}, "evidence_log": []},
            "sub_7b_samsung_hbm4": {"current_status": "Not fired", "current_state": {}, "evidence_log": []},
            "sub_7c_micron": {"current_status": "Not fired", "current_state": {}, "evidence_log": []},
            "sub_7d_hbf": {"current_status": "Not fired", "current_state": {}, "evidence_log": []},
        }
    with open(STATE_FILE) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    """Atomically write state file (write to temp, rename)."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def _apply_extraction_to_state(state: dict, entries: list[dict]) -> tuple[dict, list[str]]:
    """Walk extraction entries; update state's current_state fields per sub.
    Returns (updated_state, list_of_change_descriptions).

    Extraction entry shape (what the Layer 1 digester is expected to emit):
        {
            "date": "2026-05-22T10:00:00Z",
            "source": "mirae_asset_pdf_2026Q2",
            "article_id": "...",
            "competitor_signals": {
                "cxmt_oem_mainstream_win": "Dell" | null,
                "samsung_hbm4_nvidia_share_pct": 32.0 | null,
                "hbm_gross_margin_pct_sk_hynix": 78.0 | null,
                ...
            }
        }

    Aggregation rules:
      - For boolean fields: any True observation in window → True (sticky)
      - For string fields (OEM names, design wins): accumulate to list
      - For numeric fields: most recent value, weighted by source tier
        (Tier A overrides B overrides C; same-tier → most recent)
      - For enum fields (qualification lead, share-growth source): most
        recent value from highest-tier source
    """
    changes = []

    # Aggregate by field — group entries by (sub_field, value) for conflict resolution
    # For numeric/enum fields we need (value, tier, timestamp) tuples.
    numeric_field_observations: dict[str, list[tuple[float, int, str]]] = {}
    enum_field_observations: dict[str, list[tuple[str, int, str]]] = {}
    boolean_field_set: set[str] = set()  # fields that should be set True
    string_list_field: dict[str, list[str]] = {}  # accumulate distinct strings

    for entry in entries:
        sigs = entry.get("competitor_signals") or {}
        if not sigs:
            continue
        source = entry.get("source", "")
        tier = _source_tier(source)
        ts = entry.get("date") or entry.get("extracted_at") or entry.get("timestamp", "")

        for field_name, value in sigs.items():
            if value is None:
                continue
            # Boolean fields
            if isinstance(value, bool):
                if value:
                    boolean_field_set.add(field_name)
            # Numeric fields
            elif isinstance(value, (int, float)):
                numeric_field_observations.setdefault(field_name, []).append((float(value), tier, ts))
            # String fields — could be OEM name (add to list) or enum (single value)
            elif isinstance(value, str):
                # Enum fields: hbm4e_qualification_lead, micron_vera_rubin_design_win,
                # micron_hbm_share_growth_source, demand_classification
                if field_name in {
                    "hbm4e_qualification_lead", "micron_vera_rubin_design_win",
                    "micron_hbm_share_growth_source", "hbf_commercial_stage",
                    "sandisk_kioxia_hbf_lead",
                }:
                    enum_field_observations.setdefault(field_name, []).append((value, tier, ts))
                else:
                    # String list field — OEM names, design wins, hyperscaler wins
                    string_list_field.setdefault(field_name, []).append(value)

    # Resolve numeric values: highest tier wins; same tier → most recent
    resolved_numeric = {}
    for field_name, observations in numeric_field_observations.items():
        observations.sort(key=lambda x: (x[1], -hash(x[2])))  # tier asc; ts desc within
        resolved_numeric[field_name] = observations[0][0]

    resolved_enum = {}
    for field_name, observations in enum_field_observations.items():
        observations.sort(key=lambda x: (x[1], -hash(x[2])))
        resolved_enum[field_name] = observations[0][0]

    # Map each field to its sub-indicator key in state
    field_to_sub = {
        # 7A
        "cxmt_oem_mainstream_win": "sub_7a_cxmt",
        "cxmt_oem_lowend_win": "sub_7a_cxmt",
        "cxmt_ddr5_share_pct": "sub_7a_cxmt",
        "cxmt_ddr5_pricing_pressure_flagged": "sub_7a_cxmt",
        "us_export_controls_easing_event": "sub_7a_cxmt",
        # 7B
        "samsung_hbm4_nvidia_share_pct": "sub_7b_samsung_hbm4",
        "sk_hynix_hbm4_nvidia_share_pct": "sub_7b_samsung_hbm4",
        "samsung_hbm4_yield_pct": "sub_7b_samsung_hbm4",
        "hbm_gross_margin_pct_sk_hynix": "sub_7b_samsung_hbm4",
        "hbm_gross_margin_pct_samsung": "sub_7b_samsung_hbm4",
        "hbm4e_qualification_lead": "sub_7b_samsung_hbm4",
        "samsung_hbm4e_announcement": "sub_7b_samsung_hbm4",
        "amd_samsung_hbm4_supply_mou": "sub_7b_samsung_hbm4",
        # 7C
        "micron_vera_rubin_design_win": "sub_7c_micron",
        "micron_total_hbm_share_pct": "sub_7c_micron",
        "micron_hbm_share_growth_source": "sub_7c_micron",
        # 7D
        "hbf_hyperscaler_design_win": "sub_7d_hbf",
        "hbf_hot_tier_displacement": "sub_7d_hbf",
        "jedec_hbf_spec_ratified": "sub_7d_hbf",
        "sk_hynix_on_jedec_hbf_working_group": "sub_7d_hbf",
        "sk_hynix_hbf_roadmap_mention": "sub_7d_hbf",
        "sk_hynix_solidigm_hbf_integration": "sub_7d_hbf",
        "sandisk_kioxia_hbf_milestone": "sub_7d_hbf",
        "hbf_commercial_stage": "sub_7d_hbf",
    }

    # Apply to state
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _set(sub_key: str, field_name: str, value):
        sub = state.setdefault(sub_key, {"current_state": {}, "evidence_log": []})
        cs = sub.setdefault("current_state", {})
        old = cs.get(field_name)
        if old != value:
            cs[field_name] = value
            changes.append(f"{sub_key}.{field_name}: {old} → {value}")

    # Boolean updates
    for fn in boolean_field_set:
        sub_key = field_to_sub.get(fn)
        if sub_key:
            _set(sub_key, fn, True)
    # Numeric updates
    for fn, v in resolved_numeric.items():
        sub_key = field_to_sub.get(fn)
        if sub_key:
            _set(sub_key, fn, v)
    # Enum updates
    for fn, v in resolved_enum.items():
        sub_key = field_to_sub.get(fn)
        if sub_key:
            _set(sub_key, fn, v)
    # String list updates — OEM wins, design wins (append unique)
    for fn, vals in string_list_field.items():
        sub_key = field_to_sub.get(fn)
        if not sub_key:
            continue
        sub = state.setdefault(sub_key, {"current_state": {}, "evidence_log": []})
        cs = sub.setdefault("current_state", {})
        # Determine target list field name — convention: pluralize singular extraction field
        list_field_map = {
            "cxmt_oem_mainstream_win": "cxmt_oem_mainstream_wins",
            "cxmt_oem_lowend_win": "cxmt_oem_lowend_wins",
            "hbf_hyperscaler_design_win": "hyperscaler_hbf_design_wins",
            "sandisk_kioxia_hbf_milestone": "sandisk_kioxia_hbf_milestones",
        }
        target = list_field_map.get(fn, fn)
        existing = cs.setdefault(target, [])
        for v in vals:
            if v not in existing:
                existing.append(v)
                changes.append(f"{sub_key}.{target}: appended {v!r}")

    return state, changes


def _evaluate_sub_status(sub_key: str, sub_state: dict, thresholds: dict) -> tuple[str, float]:
    """Evaluate a sub's status from its current_state vs thresholds.
    Returns (status, estimated_eps_impact_pct).

    Implements the trigger threshold logic per sub from the YAML spec.
    This is the single point of truth for status mapping — if you change
    thresholds in data/competitor_thresholds.yaml, this is what re-reads them.
    """
    cs = sub_state.get("current_state", {})

    if sub_key == "sub_7a_cxmt":
        mainstream_wins = cs.get("cxmt_oem_mainstream_wins", [])
        lowend_wins = cs.get("cxmt_oem_lowend_wins", [])
        share = cs.get("cxmt_ddr5_share_pct", 0)
        pricing_pressure = cs.get("cxmt_ddr5_pricing_pressure_flagged", False)
        if mainstream_wins or (share is not None and share > 15):
            return ("Fired", 22.0)
        if lowend_wins or pricing_pressure or (share is not None and share >= 8):
            return ("Partially fired", 10.0)
        return ("Not fired", 1.0)

    if sub_key == "sub_7b_samsung_hbm4":
        sk_share = cs.get("sk_hynix_hbm4_nvidia_share_pct")
        s_share = cs.get("samsung_hbm4_nvidia_share_pct")
        sk_margin = cs.get("hbm_gross_margin_pct_sk_hynix")
        s_yield = cs.get("samsung_hbm4_yield_pct")
        qual_lead = cs.get("hbm4e_qualification_lead", "tied")
        # Fired conditions
        if s_share is not None and s_share > 50:
            return ("Fired", 27.0)
        if sk_share is not None and sk_share < 30:
            return ("Fired", 27.0)
        if sk_margin is not None and sk_margin < 60:
            return ("Fired", 25.0)
        # Partially fired conditions — Samsung Nvidia HBM4 capture share is the
        # canonical signal per the May 22 framework session. Samsung at ≥25%
        # of Vera Rubin allocation = Partially fired (10% EPS today's read).
        if s_share is not None and s_share >= 25:
            return ("Partially fired", 10.0)
        if sk_share is not None and 30 <= sk_share < 50:
            return ("Partially fired", 15.0)
        if s_yield is not None and s_yield >= 65:
            return ("Partially fired", 12.0)
        if qual_lead == "samsung_ahead":
            return ("Partially fired", 12.0)
        if sk_margin is not None and 60 <= sk_margin < 70:
            return ("Partially fired", 12.0)
        # Default — preserve bootstrap eps estimate when present
        eps = float(sub_state.get("estimated_eps_impact_pct", 4.0))
        if eps >= 5:
            return ("Partially fired", eps)
        return ("Not fired", eps)

    if sub_key == "sub_7c_micron":
        vr_status = cs.get("micron_vera_rubin_design_win", "excluded")
        share = cs.get("micron_total_hbm_share_pct", 0) or 0
        growth_source = cs.get("micron_hbm_share_growth_source", "samsung")
        if vr_status == "primary_sku":
            return ("Fired", 12.0)
        if share >= 30:
            return ("Fired", 12.0)
        if vr_status == "secondary_sku" or growth_source == "sk_hynix" or share >= 22:
            return ("Partially fired", 7.0)
        return ("Not fired", 0.5)

    if sub_key == "sub_7d_hbf":
        hot_wins = cs.get("hbf_hot_tier_substitution_events", []) or []
        hyperscaler_wins = cs.get("hyperscaler_hbf_design_wins", []) or []
        jedec_ratified = cs.get("jedec_hbf_spec_ratified", False)
        sk_on_wg = cs.get("sk_hynix_on_jedec_hbf_working_group")
        sk_roadmap = cs.get("sk_hynix_hbf_public_roadmap", False)
        position = cs.get("sandisk_kioxia_hbf_lead")
        share = cs.get("hbf_share_of_accelerator_memory_pct", 0) or 0
        # Fired
        if len(hot_wins) >= 2 and position == "ahead" and share >= 10:
            return ("Fired", 18.0)
        # Partially fired
        if hot_wins or (jedec_ratified and sk_on_wg is False):
            return ("Partially fired", 8.0)
        # Bull case: SK Hynix has public roadmap → still Not fired (potentially positive)
        return ("Not fired", 0.0)

    return ("Not fired", 0.0)


def aggregate_competitor_extractions(lookback_days: Optional[int] = None) -> dict:
    """Main entry point. Reads extraction logs, updates state, re-evaluates
    sub statuses, persists state file, returns updated state dict.

    Safe-failing — exceptions logged but state preserved.
    """
    try:
        thresholds = _load_thresholds()
    except FileNotFoundError:
        logger.warning("data/competitor_thresholds.yaml missing — aggregator skipped")
        return {}

    if lookback_days is None:
        lookback_days = thresholds.get("aggregator_config", {}).get("extraction_log_lookback_days", 30)

    state = _load_state()

    try:
        entries = _iter_extraction_log_entries(lookback_days)
        if entries:
            state, changes = _apply_extraction_to_state(state, entries)
            logger.info(
                f"Competitor aggregator: {len(entries)} extraction entries in "
                f"trailing {lookback_days}d; {len(changes)} state changes."
            )
            for ch in changes[:10]:
                logger.info(f"  • {ch}")
        else:
            logger.info(f"Competitor aggregator: no extraction entries in trailing {lookback_days}d; state preserved.")

        # Re-evaluate sub statuses from updated state
        for sub_key in ("sub_7a_cxmt", "sub_7b_samsung_hbm4", "sub_7c_micron", "sub_7d_hbf"):
            sub = state.setdefault(sub_key, {"current_state": {}})
            new_status, new_eps = _evaluate_sub_status(sub_key, sub, thresholds)
            old_status = sub.get("current_status")
            sub["current_status"] = new_status
            sub["estimated_eps_impact_pct"] = new_eps
            sub["status_evaluated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            sub["status_evaluator"] = "competitor_aggregator"
            if old_status and old_status != new_status:
                logger.info(f"  {sub_key} status: {old_status} → {new_status} (eps {new_eps:.1f}%)")

        state["_metadata"]["last_aggregator_run"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _save_state(state)
    except Exception as e:
        logger.error(f"Competitor aggregator failed (state preserved): {e}", exc_info=True)

    return state
