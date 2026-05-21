"""Decode HBM Captive Ratio (DHCR) — v3.4.0.

Computes the Decode HBM Captive Ratio indicator from three reference
YAML files:
  - data/inference_arch_reference.yaml   (KV efficiency frontier)
  - data/hbm_per_accelerator.yaml        (HBM capacity per accelerator)
  - data/enterprise_ai_revenue.yaml      (demand-pull metrics)

The CORE QUESTION (Raul's mental model, locked in May 21, 2026
conversation):

    Is enterprise demand-pull growing faster than the supply-side
    capacity-creation rate (efficiency × HBM-per-accelerator)?

The mental model rejects the naive "efficiency vs context length"
trade-off. Instead:
  - Efficiency gains UNLOCK economic context capacity. The market
    consumes context up to whatever is economically possible at the
    current efficiency × cost.
  - Enterprise workloads expand into the unlocked capacity (the
    100M-token full-enterprise-agent workload that doesn't exist
    today because it's not economic, but will exist tomorrow when
    efficiency makes it economic).
  - Therefore the binding question is whether enterprise demand
    growth keeps pace with capacity creation. If enterprise grows
    faster, HBM stays tight (bullish SK Hynix). If capacity creation
    outruns demand, HBM gets slack (bearish).

This module is a DIRECTION MONITOR. It does NOT produce fire/not-fired
trigger states (v3.4.0 ships with no trigger logic). Future v3.4.1+
may attach triggers to zone transitions after 4-6 quarters of shadow
data validates the band boundaries.

v3.4.0 ships with bootstrapped historical data (Q4 2023 → Q2 2026),
giving the trajectory chart 11 quarters of data from day one.

Update cadence:
  - inference_arch_reference.yaml: monthly (per major model release)
  - hbm_per_accelerator.yaml: per accelerator launch (~3-6 mo)
  - enterprise_ai_revenue.yaml: quarterly (per earnings cycle)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger(__name__)


# ============================================================
# Dataclasses for DHCR outputs
# ============================================================

@dataclass
class DHCRSnapshot:
    """A single point-in-time DHCR computation.

    Three components:
      1. supply_side — KV efficiency × HBM per accelerator (capacity-creation rate)
      2. demand_side — enterprise AI revenue growth (demand-pull rate)
      3. direction   — composite read derived from the relative rates

    Trajectory analysis compares snapshots over time. The current snapshot
    is committed to state/dhcr_state.json each run for QoQ tracking.
    """
    # When this snapshot was computed
    computed_at_iso: str

    # Supply-side: capacity creation
    frontier_kv_kb_per_token: float
    frontier_model_name: str
    frontier_model_arch: str  # "MLA" | "GQA" | etc.
    frontier_hbm_gb_per_accelerator: float
    frontier_accelerator_name: str

    # Derived: how much context capacity does a single accelerator HOLD?
    implied_context_capacity_per_accelerator_tokens: int

    # YoY change rates (supply-side drivers)
    kv_yoy_change_pct: Optional[float]                    # negative = efficiency improving
    hbm_per_accelerator_yoy_change_pct: Optional[float]   # positive = supply capacity growing
    capacity_creation_yoy_pct: Optional[float]            # composite of KV efficiency + HBM packing

    # Demand-pull: enterprise AI revenue trajectory
    enterprise_ai_aggregate_run_rate_usd_b: float
    enterprise_ai_yoy_growth_pct: Optional[float]
    nvidia_datacenter_yoy_growth_pct: Optional[float]
    hyperscaler_capex_yoy_growth_pct: Optional[float]

    # Composite direction signal
    # Levels: strongly_bullish_hbm, mildly_bullish_hbm, balanced,
    #         mildly_bearish_hbm, strongly_bearish_hbm
    direction: str
    direction_score: float  # numerical score [-1.0, +1.0]; see _compute_direction

    # Which side is dominating ("demand-pull leads", "supply-creation leads", "in balance")
    dominant_force: str

    # Confidence: how strong is this read?
    confidence: str  # "high" | "medium" | "low"

    # Sonnet-friendly summary for narrative integration
    narrative_summary: str

    # Optional: non-HBM substitution risk flag (e.g. Cerebras taking share)
    substitution_risk_notes: list[str] = field(default_factory=list)


# ============================================================
# YAML loading
# ============================================================

# Default search path for data files. dhcr.py lives in src/analysis/,
# so data/ is two levels up.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def load_dhcr_data(data_dir: Optional[Path] = None) -> dict:
    """Load the three DHCR YAML reference files.

    Returns:
        dict with keys 'arch', 'hbm', 'rev' mapping to parsed YAML.

    Raises:
        FileNotFoundError if any required file is missing.
        ValueError if pyyaml is not installed.
    """
    if yaml is None:
        raise ValueError(
            "PyYAML is required for DHCR computation. "
            "Install via: pip install pyyaml"
        )

    if data_dir is None:
        data_dir = DEFAULT_DATA_DIR

    arch_path = data_dir / "inference_arch_reference.yaml"
    hbm_path = data_dir / "hbm_per_accelerator.yaml"
    rev_path = data_dir / "enterprise_ai_revenue.yaml"

    for p in (arch_path, hbm_path, rev_path):
        if not p.exists():
            raise FileNotFoundError(f"DHCR data file missing: {p}")

    return {
        "arch": yaml.safe_load(arch_path.read_text()),
        "hbm": yaml.safe_load(hbm_path.read_text()),
        "rev": yaml.safe_load(rev_path.read_text()),
    }


# ============================================================
# Frontier KV efficiency
# ============================================================

def compute_kv_per_token(model_entry: dict) -> float:
    """Compute KV cache bytes per token for a single open-architecture model.

    Returns KV size in KB/token.

    Supports two architecture families:
      - GQA (and MHA as special case where num_kv_heads == num_attention_heads):
          kv_bytes = 2 × layers × num_kv_heads × head_dim × bytes_per_element
                     / compression_factor
      - MLA: uses kv_lora_rank (latent dim) + qk_rope_head_dim × num_heads_for_rope
          kv_bytes ≈ layers × (kv_lora_rank + qk_rope_head_dim) × bytes_per_element
                     / compression_factor
          (No 2x for K+V — MLA stores a single shared latent.)

    For entries that have a pre-computed kv_kb_per_token field, return it
    directly (treats the YAML as authoritative). The from-parameters
    computation is the verification path and useful for tests.
    """
    # If the YAML pre-computed the answer, trust it (it's been verified)
    if "kv_kb_per_token" in model_entry:
        return float(model_entry["kv_kb_per_token"])

    arch = model_entry.get("architecture_family", "GQA")
    bytes_per_element = float(model_entry["bytes_per_element"])
    compression = float(model_entry.get("compression_factor", 1.0))
    num_layers = int(model_entry["num_layers"])

    if arch.startswith("MLA"):
        # MLA: single latent cache, not K+V double-stored
        latent = int(model_entry["mla_kv_lora_rank"])
        rope_dim = int(model_entry.get("mla_qk_rope_head_dim", 0))
        # The RoPE component is per-token, not per-head per-token in standard MLA
        # The 2× would only apply if we were storing K and V separately, which MLA doesn't.
        kv_bytes = num_layers * (latent + rope_dim) * bytes_per_element / compression
    else:
        # GQA / MHA: standard K+V double-store
        num_kv_heads = int(model_entry["num_kv_heads"])
        head_dim = int(model_entry["head_dim"])
        kv_bytes = (
            2 * num_layers * num_kv_heads * head_dim * bytes_per_element / compression
        )

    kv_kb = kv_bytes / 1024.0
    return kv_kb


def compute_frontier_kv(arch_yaml: dict, as_of_date: Optional[date] = None) -> dict:
    """Find the frontier (most efficient) open-architecture model as of a given date.

    The frontier KV is the MINIMUM KV/token across open-architecture models
    released on or before as_of_date. We use min rather than e.g. recent-90-day
    minimum because frontier efficiency monotonically ratchets down (once a more
    efficient architecture exists, it remains the frontier for the comparison).

    Args:
        arch_yaml: parsed inference_arch_reference.yaml
        as_of_date: snapshot date; defaults to today

    Returns:
        dict with keys: kv_kb_per_token, model_name, arch_family, release_date
    """
    if as_of_date is None:
        as_of_date = date.today()

    eligible = []
    for m in arch_yaml.get("open_models", []):
        try:
            rd = datetime.fromisoformat(m["release_date"]).date()
        except (ValueError, KeyError):
            continue
        if rd > as_of_date:
            continue
        kv = compute_kv_per_token(m)
        eligible.append((kv, m, rd))

    if not eligible:
        raise ValueError(
            f"No open-architecture models available for frontier as of {as_of_date}"
        )

    eligible.sort(key=lambda t: t[0])  # ascending KV → smallest is frontier
    kv, model, rd = eligible[0]

    return {
        "kv_kb_per_token": kv,
        "model_name": model["name"],
        "arch_family": model.get("architecture_family", "unknown"),
        "release_date": rd.isoformat(),
    }


def compute_kv_yoy_change(arch_yaml: dict, as_of_date: Optional[date] = None) -> Optional[float]:
    """YoY change in frontier KV. Negative = efficiency improving."""
    if as_of_date is None:
        as_of_date = date.today()
    year_ago = as_of_date - timedelta(days=365)

    try:
        now = compute_frontier_kv(arch_yaml, as_of_date)
        prior = compute_frontier_kv(arch_yaml, year_ago)
    except ValueError:
        return None

    if prior["kv_kb_per_token"] == 0:
        return None
    delta = (now["kv_kb_per_token"] / prior["kv_kb_per_token"]) - 1.0
    return round(delta * 100, 2)


# ============================================================
# HBM per accelerator (supply-side capacity)
# ============================================================

def compute_frontier_hbm_per_accelerator(
    hbm_yaml: dict,
    as_of_date: Optional[date] = None,
    include_projected: bool = False,
) -> dict:
    """Find the maximum HBM-per-accelerator across NVIDIA + AMD merchant chips
    shipping on or before as_of_date.

    Custom hyperscaler silicon (Google TPU, AWS Trainium, etc.) is EXCLUDED
    from the frontier because it's captive — not part of the merchant market
    that SK Hynix's HBM revenue depends on.

    Non-HBM accelerators (Cerebras WSE, Groq) are EXCLUDED from frontier
    HBM but are tracked separately for substitution-risk flags.

    Args:
        hbm_yaml: parsed hbm_per_accelerator.yaml
        as_of_date: snapshot date
        include_projected: if True, include entries marked confidence="projected"

    Returns:
        dict: hbm_gb_per_chip, accelerator_name, vendor, release_date
    """
    if as_of_date is None:
        as_of_date = date.today()

    candidates = []
    for vendor, key in (("NVIDIA", "nvidia"), ("AMD", "amd")):
        for gen in hbm_yaml.get(key, []):
            if not include_projected and gen.get("confidence") == "projected":
                continue
            try:
                # Some YAML entries use "2026-Q4" instead of ISO date
                rd_str = gen["release_date"]
                if "Q" in rd_str:
                    # e.g. "2026-Q4" → use end-of-quarter approximation
                    yr, q = rd_str.split("-Q")
                    rd = date(int(yr), int(q) * 3, 28)
                elif "-H" in rd_str:
                    # e.g. "2027-H2" → mid-half-year approximation
                    yr, h = rd_str.split("-H")
                    rd = date(int(yr), 6 if h == "1" else 11, 1)
                else:
                    rd = datetime.fromisoformat(rd_str).date()
            except (ValueError, KeyError):
                continue
            if rd > as_of_date:
                continue
            candidates.append((gen["hbm_gb_per_chip"], gen, vendor, rd))

    if not candidates:
        raise ValueError(
            f"No HBM accelerators available for frontier as of {as_of_date}"
        )

    candidates.sort(key=lambda t: t[0], reverse=True)  # descending → largest is frontier
    hbm, gen, vendor, rd = candidates[0]

    return {
        "hbm_gb_per_chip": float(hbm),
        "accelerator_name": f"{vendor} {gen['generation']}",
        "vendor": vendor,
        "release_date": rd.isoformat(),
    }


def compute_hbm_yoy_change(
    hbm_yaml: dict,
    as_of_date: Optional[date] = None,
) -> Optional[float]:
    """YoY change in frontier HBM-per-accelerator. Positive = packing more HBM."""
    if as_of_date is None:
        as_of_date = date.today()
    year_ago = as_of_date - timedelta(days=365)

    try:
        now = compute_frontier_hbm_per_accelerator(hbm_yaml, as_of_date)
        prior = compute_frontier_hbm_per_accelerator(hbm_yaml, year_ago)
    except ValueError:
        return None

    if prior["hbm_gb_per_chip"] == 0:
        return None
    delta = (now["hbm_gb_per_chip"] / prior["hbm_gb_per_chip"]) - 1.0
    return round(delta * 100, 2)


# ============================================================
# Derived: implied context capacity per accelerator
# ============================================================

def compute_implied_context_capacity(
    frontier_kv_kb: float,
    frontier_hbm_gb: float,
) -> int:
    """How many tokens of context can fit in one accelerator's HBM at today's
    frontier efficiency?

    implied_context = HBM (GB) / KV-per-token (KB) × 1024² / 1024
                    = HBM (GB) × 1024² / (KV_KB × 1024)
                    = HBM_GB × 1024 / KV_KB

    Example: H200 (141GB HBM) × Llama 3 70B (320 KB/tok) →
             141 × 1024 / 320 ≈ 451K tokens

    Example: GB300 (576GB) × DeepSeek V4 (14 KB/tok) →
             576 × 1024 / 14 ≈ 42M tokens

    This is the ECONOMIC FRONTIER OF CONTEXT — the upper bound of what
    can run on a single accelerator. Per Raul's mental model, enterprise
    workloads will expand to consume this capacity.
    """
    if frontier_kv_kb <= 0:
        return 0
    tokens = (frontier_hbm_gb * 1024.0) / frontier_kv_kb
    return int(tokens * 1000)  # convert back from KB scale: GB×1024/KB×1024 → tokens


# ============================================================
# Capacity creation rate (composite supply-side)
# ============================================================

def compute_capacity_creation_yoy(
    kv_yoy_pct: Optional[float],
    hbm_yoy_pct: Optional[float],
) -> Optional[float]:
    """Composite supply-side capacity creation rate.

    capacity_creation_yoy = HBM_yoy - KV_yoy (since KV_yoy is negative when
    improving, subtracting it ADDS the efficiency tailwind to capacity)

    Example:
        HBM_yoy = +50% (more HBM per chip)
        KV_yoy  = -40% (40% more efficient per token)
        capacity_creation = 50 - (-40) = +90% — capacity for context expanded
        90% YoY through combined supply + efficiency gains.

    Returns None if either input is missing.
    """
    if kv_yoy_pct is None or hbm_yoy_pct is None:
        return None
    return round(hbm_yoy_pct - kv_yoy_pct, 2)


# ============================================================
# Demand-pull (from enterprise_ai_revenue.yaml)
# ============================================================

def compute_enterprise_demand(
    rev_yaml: dict,
    as_of_date: Optional[date] = None,
) -> dict:
    """Aggregate enterprise AI demand metrics for the most recent quarter
    on or before as_of_date.

    Returns aggregate run-rate, weighted YoY growth, NVIDIA datacenter growth,
    hyperscaler capex growth.
    """
    if as_of_date is None:
        as_of_date = date.today()

    def _latest_quarter(entries: list[dict]) -> Optional[dict]:
        """Find the most recent quarterly entry on or before as_of_date.
        Quarters are encoded as 'YYYY-QN'; treat them as end-of-quarter.
        """
        latest = None
        latest_date = None
        for e in entries:
            q = e.get("quarter")
            if not q or "-Q" not in q:
                continue
            try:
                yr, qn = q.split("-Q")
                # End of quarter ≈ months 3, 6, 9, 12
                month = int(qn) * 3
                # Use last day of quarter as a rough proxy
                if month == 3:
                    d = date(int(yr), 3, 31)
                elif month == 6:
                    d = date(int(yr), 6, 30)
                elif month == 9:
                    d = date(int(yr), 9, 30)
                else:
                    d = date(int(yr), 12, 31)
            except (ValueError, KeyError):
                continue
            if d > as_of_date:
                continue
            if latest_date is None or d > latest_date:
                latest = e
                latest_date = d
        return latest

    msft = _latest_quarter(rev_yaml.get("microsoft_azure_ai", []))
    aws = _latest_quarter(rev_yaml.get("aws_ai_services", []))
    gcp = _latest_quarter(rev_yaml.get("google_cloud_ai", []))
    oracle = _latest_quarter(rev_yaml.get("oracle_ai_infra", []))
    nv = _latest_quarter(rev_yaml.get("nvidia_datacenter", []))
    capex = _latest_quarter(rev_yaml.get("hyperscaler_ai_capex_aggregate", []))

    # Aggregate enterprise AI run-rate (annualized)
    run_rate_components = []
    for src in (msft, aws, gcp, oracle):
        if src and src.get("revenue_usd_b_annualized") is not None:
            run_rate_components.append(src["revenue_usd_b_annualized"])
    aggregate_run_rate = sum(run_rate_components) if run_rate_components else 0.0

    # YoY growth: weighted average of the four (weighted by absolute size)
    yoy_pairs = []
    for src in (msft, aws, gcp, oracle):
        if src and src.get("yoy_growth_pct") is not None and src.get("revenue_usd_b_annualized"):
            yoy_pairs.append((src["yoy_growth_pct"], src["revenue_usd_b_annualized"]))

    if yoy_pairs:
        total_weight = sum(w for _, w in yoy_pairs)
        weighted_yoy = sum(g * w for g, w in yoy_pairs) / total_weight if total_weight else None
        weighted_yoy = round(weighted_yoy, 2) if weighted_yoy is not None else None
    else:
        weighted_yoy = None

    return {
        "aggregate_run_rate_usd_b": round(aggregate_run_rate, 2),
        "weighted_yoy_growth_pct": weighted_yoy,
        "nvidia_datacenter_yoy_pct": nv["yoy_growth_pct"] if nv else None,
        "nvidia_datacenter_rev_usd_b": nv["revenue_usd_b"] if nv else None,
        "hyperscaler_capex_yoy_pct": capex["yoy_growth_pct"] if capex else None,
        "as_of_quarter": (msft or nv or {}).get("quarter", "unknown"),
    }


# ============================================================
# Composite direction signal
# ============================================================

def compute_direction(
    capacity_creation_yoy: Optional[float],
    enterprise_yoy: Optional[float],
    nvidia_dc_yoy: Optional[float],
) -> dict:
    """Compute the composite DHCR direction signal.

    The core comparison (Raul's mental model):
      capacity_creation_yoy  vs  demand_pull_yoy

    Where demand_pull is approximated as the weighted blend of:
      - enterprise AI revenue YoY (60% weight — direct enterprise pull)
      - NVIDIA datacenter YoY (40% weight — total accelerator demand)

    Result:
      delta = demand_pull_yoy - capacity_creation_yoy

      delta > +40%  → strongly_bullish_hbm (demand outrunning capacity by wide margin)
      delta in +10 to +40% → mildly_bullish_hbm (demand slightly ahead)
      delta in -10 to +10% → balanced
      delta in -40 to -10% → mildly_bearish_hbm (capacity outrunning demand)
      delta < -40% → strongly_bearish_hbm (efficiency outpacing demand significantly)

    Returns dict with direction string, numerical score [-1.0, +1.0],
    dominant_force description, and confidence assessment.
    """
    if capacity_creation_yoy is None or (enterprise_yoy is None and nvidia_dc_yoy is None):
        return {
            "direction": "balanced",
            "direction_score": 0.0,
            "dominant_force": "insufficient data",
            "confidence": "low",
            "delta_pp": None,
        }

    # Weighted demand-pull signal
    components = []
    if enterprise_yoy is not None:
        components.append((enterprise_yoy, 0.60))
    if nvidia_dc_yoy is not None:
        components.append((nvidia_dc_yoy, 0.40))

    if not components:
        demand_pull = None
    else:
        total_w = sum(w for _, w in components)
        demand_pull = sum(g * w for g, w in components) / total_w

    if demand_pull is None:
        return {
            "direction": "balanced",
            "direction_score": 0.0,
            "dominant_force": "insufficient data",
            "confidence": "low",
            "delta_pp": None,
        }

    delta_pp = demand_pull - capacity_creation_yoy

    # Classification
    if delta_pp > 40:
        direction = "strongly_bullish_hbm"
        score = min(1.0, delta_pp / 100.0)
        dominant = "demand-pull substantially outrunning capacity creation"
    elif delta_pp > 10:
        direction = "mildly_bullish_hbm"
        score = delta_pp / 100.0
        dominant = "demand-pull modestly ahead of capacity creation"
    elif delta_pp > -10:
        direction = "balanced"
        score = delta_pp / 100.0
        dominant = "demand and capacity creation roughly matched"
    elif delta_pp > -40:
        direction = "mildly_bearish_hbm"
        score = delta_pp / 100.0
        dominant = "capacity creation outpacing demand-pull"
    else:
        direction = "strongly_bearish_hbm"
        score = max(-1.0, delta_pp / 100.0)
        dominant = "efficiency / supply substantially outpacing demand"

    # Confidence assessment
    if enterprise_yoy is not None and nvidia_dc_yoy is not None and capacity_creation_yoy is not None:
        confidence = "high"
    elif (enterprise_yoy is not None or nvidia_dc_yoy is not None) and capacity_creation_yoy is not None:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "direction": direction,
        "direction_score": round(score, 3),
        "dominant_force": dominant,
        "confidence": confidence,
        "delta_pp": round(delta_pp, 2),
    }


# ============================================================
# Substitution-risk flags (non-HBM accelerator share)
# ============================================================

def assess_substitution_risk(hbm_yaml: dict) -> list[str]:
    """Flag substitution risks from non-HBM AI accelerators.

    For v3.4.0 this is qualitative — we don't have hard non-HBM share data.
    The flags are pulled from yaml notes and ai_compute_share_estimate fields.
    Future versions may compute quantitative non-HBM share from earnings data.
    """
    flags = []
    for non_hbm in hbm_yaml.get("non_hbm_accelerators", []):
        vendor = non_hbm.get("vendor", "?")
        gen = non_hbm.get("generation", "?")
        share = non_hbm.get("ai_compute_share_estimate", "?")
        flags.append(
            f"{vendor} {gen}: estimated {share} AI compute share "
            f"({non_hbm.get('notes', '')[:80]})"
        )
    return flags


# ============================================================
# Narrative summary (for Sonnet integration)
# ============================================================

def build_narrative_summary(snapshot: dict) -> str:
    """Build a one-paragraph human-readable summary of the DHCR state.

    Used by Sonnet as input to the section 4 deep-dive paragraph and
    the exec brief macro-context line.
    """
    direction = snapshot["direction"]
    delta_pp = snapshot["direction_score"]

    parts = []

    # Capacity creation side
    parts.append(
        f"Supply-side capacity creation YoY: KV efficiency "
        f"{'compressed' if (snapshot['kv_yoy_change_pct'] or 0) < 0 else 'flat'} "
        f"{abs(snapshot['kv_yoy_change_pct'] or 0):.0f}% "
        f"(frontier: {snapshot['frontier_model_name']} at "
        f"{snapshot['frontier_kv_kb_per_token']:.0f} KB/tok); "
        f"HBM-per-accelerator "
        f"{'+' if (snapshot['hbm_per_accelerator_yoy_change_pct'] or 0) > 0 else ''}"
        f"{snapshot['hbm_per_accelerator_yoy_change_pct'] or 0:.0f}% "
        f"(frontier: {snapshot['frontier_accelerator_name']} at "
        f"{snapshot['frontier_hbm_gb_per_accelerator']:.0f} GB)."
    )

    # Implied context capacity
    cap_tokens = snapshot["implied_context_capacity_per_accelerator_tokens"]
    if cap_tokens >= 1_000_000:
        cap_str = f"{cap_tokens/1_000_000:.1f}M"
    elif cap_tokens >= 1_000:
        cap_str = f"{cap_tokens/1_000:.0f}K"
    else:
        cap_str = str(cap_tokens)
    parts.append(
        f"Implied economic context frontier per accelerator: ~{cap_str} tokens. "
    )

    # Demand-pull side
    parts.append(
        f"Demand-pull: aggregate enterprise AI run-rate "
        f"~${snapshot['enterprise_ai_aggregate_run_rate_usd_b']:.0f}B "
        f"growing {snapshot['enterprise_ai_yoy_growth_pct'] or 0:.0f}% YoY; "
        f"NVIDIA datacenter "
        f"{snapshot['nvidia_datacenter_yoy_growth_pct'] or 0:.0f}% YoY."
    )

    # Direction read
    dir_label = direction.replace("_", " ")
    delta = snapshot.get("direction_delta_pp")
    if delta is not None:
        parts.append(
            f"Composite read: {dir_label.upper()} "
            f"(delta {delta:+.0f}pp — "
            f"{snapshot['dominant_force']})."
        )
    else:
        parts.append(
            f"Composite read: {dir_label.upper()} "
            f"(insufficient data for delta — {snapshot['dominant_force']})."
        )

    return " ".join(parts)


# ============================================================
# Top-level: compute full DHCR snapshot
# ============================================================

def compute_dhcr_snapshot(
    data_dir: Optional[Path] = None,
    as_of_date: Optional[date] = None,
) -> DHCRSnapshot:
    """Compute the full DHCR snapshot from the three YAML files.

    This is the main entry point called by fast_tier.py.

    Args:
        data_dir: optional override for data file location
        as_of_date: optional override for snapshot date (default: today)

    Returns:
        DHCRSnapshot dataclass with all computed fields.

    Raises:
        FileNotFoundError if YAML files are missing.
        ValueError if data is too sparse to compute.
    """
    if as_of_date is None:
        as_of_date = date.today()

    data = load_dhcr_data(data_dir=data_dir)

    # 1. Frontier KV efficiency
    frontier_kv = compute_frontier_kv(data["arch"], as_of_date=as_of_date)
    kv_yoy = compute_kv_yoy_change(data["arch"], as_of_date=as_of_date)

    # 2. Frontier HBM per accelerator
    frontier_hbm = compute_frontier_hbm_per_accelerator(data["hbm"], as_of_date=as_of_date)
    hbm_yoy = compute_hbm_yoy_change(data["hbm"], as_of_date=as_of_date)

    # 3. Implied context capacity
    implied_capacity = compute_implied_context_capacity(
        frontier_kv["kv_kb_per_token"],
        frontier_hbm["hbm_gb_per_chip"],
    )

    # 4. Capacity creation rate
    capacity_creation_yoy = compute_capacity_creation_yoy(kv_yoy, hbm_yoy)

    # 5. Enterprise demand
    demand = compute_enterprise_demand(data["rev"], as_of_date=as_of_date)

    # 6. Composite direction
    direction = compute_direction(
        capacity_creation_yoy=capacity_creation_yoy,
        enterprise_yoy=demand["weighted_yoy_growth_pct"],
        nvidia_dc_yoy=demand["nvidia_datacenter_yoy_pct"],
    )

    # 7. Substitution risk
    subst_risks = assess_substitution_risk(data["hbm"])

    # Build snapshot
    snap_dict = {
        "computed_at_iso": datetime.utcnow().isoformat(),
        "frontier_kv_kb_per_token": frontier_kv["kv_kb_per_token"],
        "frontier_model_name": frontier_kv["model_name"],
        "frontier_model_arch": frontier_kv["arch_family"],
        "frontier_hbm_gb_per_accelerator": frontier_hbm["hbm_gb_per_chip"],
        "frontier_accelerator_name": frontier_hbm["accelerator_name"],
        "implied_context_capacity_per_accelerator_tokens": implied_capacity,
        "kv_yoy_change_pct": kv_yoy,
        "hbm_per_accelerator_yoy_change_pct": hbm_yoy,
        "capacity_creation_yoy_pct": capacity_creation_yoy,
        "enterprise_ai_aggregate_run_rate_usd_b": demand["aggregate_run_rate_usd_b"],
        "enterprise_ai_yoy_growth_pct": demand["weighted_yoy_growth_pct"],
        "nvidia_datacenter_yoy_growth_pct": demand["nvidia_datacenter_yoy_pct"],
        "hyperscaler_capex_yoy_growth_pct": demand["hyperscaler_capex_yoy_pct"],
        "direction": direction["direction"],
        "direction_score": direction["direction_score"],
        "dominant_force": direction["dominant_force"],
        "confidence": direction["confidence"],
        "substitution_risk_notes": subst_risks,
    }

    # Add direction_delta_pp for narrative (separate from dataclass field)
    snap_dict_with_delta = {**snap_dict, "direction_delta_pp": direction.get("delta_pp")}
    snap_dict["narrative_summary"] = build_narrative_summary(snap_dict_with_delta)

    return DHCRSnapshot(**snap_dict)


# ============================================================
# Trajectory: compute historical snapshots for the trend chart
# ============================================================

def compute_dhcr_trajectory(
    data_dir: Optional[Path] = None,
    quarters_back: int = 11,
) -> list[dict]:
    """Compute historical DHCR snapshots for trajectory plotting.

    Returns a list of snapshots, one per quarter, going back `quarters_back`
    quarters. Used by the chart generator to plot the trend over time.
    """
    today = date.today()
    snapshots = []

    for q in range(quarters_back):
        # Walk backwards by approximately quarters
        as_of = today - timedelta(days=90 * q)
        try:
            snap = compute_dhcr_snapshot(data_dir=data_dir, as_of_date=as_of)
            snapshots.append({
                "as_of_date": as_of.isoformat(),
                "frontier_kv_kb_per_token": snap.frontier_kv_kb_per_token,
                "frontier_hbm_gb_per_accelerator": snap.frontier_hbm_gb_per_accelerator,
                "implied_context_capacity_tokens": snap.implied_context_capacity_per_accelerator_tokens,
                "enterprise_ai_yoy_growth_pct": snap.enterprise_ai_yoy_growth_pct,
                "nvidia_datacenter_yoy_growth_pct": snap.nvidia_datacenter_yoy_growth_pct,
                "capacity_creation_yoy_pct": snap.capacity_creation_yoy_pct,
                "direction": snap.direction,
                "direction_score": snap.direction_score,
            })
        except (FileNotFoundError, ValueError):
            # Sparse historical data — skip the quarter
            continue

    # Reverse so oldest is first (chronological)
    snapshots.reverse()
    return snapshots
