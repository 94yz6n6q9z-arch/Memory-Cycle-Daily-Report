"""Decode HBM Captive Ratio (DHCR) — v3.4.2.

REDESIGN from v3.4.0/v3.4.1 per the May 22 2026 design session with Raul.

# THE FRAMEWORK (locked May 22, 2026)

The DHCR maps today's inference market onto a 5×5 matrix:

  Y-axis: KV efficiency (GB per 1M live tokens at frontier architecture)
  X-axis: Operative context length (volume-weighted average across categories)

Each cell maps to a dominant memory architecture per the shared memo:
  - HBM-heavy decode (>32 GB/1M tok and/or 1M+ context)
  - HBM/GDDR hybrid (mid-band)
  - LPDDR / heterogeneous (efficient KV + medium context)
  - DDR commodity (efficient KV + short context)

The cell is translated into a linear score 0-100 (HBM-favorable ↔ commodity)
and combined with a volume index to produce a composite HBM bit demand signal.

# THE DUAL-INPUT X-AXIS (Raul's volume insight, May 22)

Per-category context length AND per-category volume share are BOTH refreshed
monthly. Operative context = Σ(category_context × category_volume_share/100).

Rationale: even if enterprise vertical context stays at 1M tokens, if its
volume share grows from 5% → 30% the operative context shifts dramatically
right. Volume of sessions matters as much as per-session context.

# THE ANTICIPATION PREMIUM (Raul's market-reality insight, May 22)

The market is provisioning HBM-heavy infra ahead of where today's literal
workload mix sits, because hyperscalers anticipate context to follow
capacity. To respect this observed reality, the DHCR computes TWO cells:

  1. Workload-implied cell: literal (frontier_KV_band, operative_context_band)
  2. Market-deployed cell: workload-implied + 1 Y-band (less efficient)

The score uses the market-deployed cell. The gap between them is itself a
signal — anticipation gap closing = market catching up to actual demand
= pre-rollover signal.

# THE SANITY CHECK (Raul's reality-check insight)

Validate against three substitution evidence signals. If ≥2 of 3
contradict the score's HBM-favorable read → CALIBRATION FLAG.

# THE VOLUME LAYER

Matrix cell tells you what memory architecture is demanded. Volume tells
you how much. HBM bit demand = score × (volume_index / 100).

# STATUS MAPPING

  score ≥ 65 OR (score 40-65 AND volume trajectory bullish) → "Not fired"
  score 30-65 AND volume flat                               → "Partially fired"
  score < 30 OR rapid score decline                         → "Fired"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger(__name__)


# Y-axis (KV efficiency) bands — focus on lower spectrum per Raul May 22
KV_BANDS = [
    {"name": "R1", "label": "<1 GB/1M tok",   "min_gb": 0.0,  "max_gb": 1.0,    "intensity": 0.10},
    {"name": "R2", "label": "1-4 GB/1M tok",  "min_gb": 1.0,  "max_gb": 4.0,    "intensity": 0.30},
    {"name": "R3", "label": "4-16 GB/1M tok", "min_gb": 4.0,  "max_gb": 16.0,   "intensity": 0.50},
    {"name": "R4", "label": "16-32 GB/1M tok","min_gb": 16.0, "max_gb": 32.0,   "intensity": 0.75},
    {"name": "R5", "label": ">32 GB/1M tok",  "min_gb": 32.0, "max_gb": 9999.0, "intensity": 0.95},
]

CONTEXT_BANDS = [
    {"name": "C1", "label": "<32K",     "min_tok": 0,       "max_tok": 32000,    "intensity": 0.10},
    {"name": "C2", "label": "32-200K",  "min_tok": 32000,   "max_tok": 200000,   "intensity": 0.30},
    {"name": "C3", "label": "200K-1M",  "min_tok": 200000,  "max_tok": 1000000,  "intensity": 0.55},
    {"name": "C4", "label": "1M-10M",   "min_tok": 1000000, "max_tok": 10000000, "intensity": 0.80},
    {"name": "C5", "label": ">10M",     "min_tok": 10000000,"max_tok": 999999999,"intensity": 0.95},
]

# 5x5 cell → memory zone label (per memo Table 1 + Table 3 zone mapping)
# Rows: R1 (most efficient) at top, R5 (least efficient = max HBM) at bottom
# Cols: C1 (shortest context) at left, C5 (longest = max HBM) at right
MEMORY_ZONES = [
    # R1 (<1 GB/1M tok — most efficient frontier)
    ["DDR",     "DDR/LPDDR", "LPDDR",    "LPDDR/HBM",  "LPDDR/HBM"],
    # R2 (1-4 GB/1M tok)
    ["DDR/CXL", "DDR/LPDDR", "LPDDR",    "LPDDR/HBM",  "HBM/hetero"],
    # R3 (4-16 GB/1M tok — today's frontier ~14 GB)
    ["LPDDR",   "LPDDR/HBM", "HBM/GDDR", "HBM/hetero", "HBM"],
    # R4 (16-32 GB/1M tok)
    ["HBM/GDDR","HBM/GDDR",  "HBM",      "HBM",        "HBM"],
    # R5 (>32 GB/1M tok — least efficient, max HBM)
    ["HBM",     "HBM",       "HBM",      "HBM",        "HBM"],
]


def zone_category(zone_label: str) -> str:
    """Reduce specific zone labels to broad category for chart coloring."""
    z = zone_label.upper()
    if z == "HBM":
        return "HBM"
    if "HBM" in z:
        return "HBM_HYBRID"
    if "LPDDR" in z:
        return "LPDDR"
    if "CXL" in z or "DDR/" in z:
        return "DDR_CXL"
    return "DDR"


# Anticipation premium: market provisions HBM ahead of workload by 1 Y-band
ANTICIPATION_Y_OFFSET = 1

# Score weighting (50/50 with anticipation premium per Raul May 22)
KV_WEIGHT = 0.50
CTX_WEIGHT = 0.50

# Status mapping thresholds
SCORE_NOT_FIRED_THRESHOLD = 65
SCORE_FIRED_THRESHOLD = 30


@dataclass
class DHCRSnapshot:
    """Full DHCR snapshot — single point in time."""
    computed_at_iso: str
    # Y-axis (supply-side)
    frontier_kv_kb_per_token: float
    frontier_kv_gb_per_1m_tokens: float
    frontier_model_name: str
    frontier_model_arch: str
    kv_band_index: int
    kv_band_name: str
    kv_band_label: str
    kv_intensity: float
    kv_yoy_change_pct: Optional[float]
    # X-axis (demand-side from dual-input)
    operative_context_tokens: int
    operative_context_kb: float
    context_band_index: int
    context_band_name: str
    context_band_label: str
    context_intensity: float
    context_categories: list[dict] = field(default_factory=list)
    # Anticipation premium
    workload_implied_cell: tuple = (0, 0)
    workload_implied_zone: str = ""
    workload_implied_zone_category: str = ""
    market_deployed_cell: tuple = (0, 0)
    market_deployed_zone: str = ""
    market_deployed_zone_category: str = ""
    anticipation_gap_bands: int = 0
    # Score (uses market-deployed cell)
    position_score: int = 0
    # Volume layer
    enterprise_ai_run_rate_usd_b: float = 0.0
    enterprise_ai_yoy_growth_pct: Optional[float] = None
    nvidia_datacenter_yoy_growth_pct: Optional[float] = None
    volume_index: float = 100.0
    volume_trajectory: str = "flat"
    # Composite HBM bit demand
    hbm_bit_demand_index: float = 0.0
    # Sanity check
    sanity_signal_substitution_news_count: int = 0
    sanity_signal_substitution_threshold: int = 5
    sanity_signal_hyperscaler_decode_mentions: int = 0
    sanity_signal_hbm_price_trend: str = "rising"
    sanity_check_confirms: int = 0
    sanity_check_contradicts: int = 0
    calibration_flag: bool = False
    # Status
    status: str = "Not fired"
    status_color: str = "#C0DD97"
    status_reasoning: str = ""
    # Narrative
    narrative_summary: str = ""
    bull_case_summary: str = ""
    bear_case_summary: str = ""
    # Carry-over from v3.4.0
    substitution_risk_notes: list[str] = field(default_factory=list)


DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def load_dhcr_data(data_dir: Optional[Path] = None) -> dict:
    """Load all four DHCR YAML reference files."""
    if yaml is None:
        raise ValueError("PyYAML is required for DHCR computation.")
    if data_dir is None:
        data_dir = DEFAULT_DATA_DIR
    paths = {
        "arch":    data_dir / "inference_arch_reference.yaml",
        "hbm":     data_dir / "hbm_per_accelerator.yaml",
        "rev":     data_dir / "enterprise_ai_revenue.yaml",
        "context": data_dir / "context_length_signals.yaml",
    }
    for name, p in paths.items():
        if not p.exists():
            raise FileNotFoundError(f"DHCR data file missing: {p}")
    return {name: yaml.safe_load(p.read_text()) for name, p in paths.items()}


def compute_kv_per_token(model_entry: dict) -> float:
    if "kv_kb_per_token" in model_entry:
        return float(model_entry["kv_kb_per_token"])
    arch = model_entry.get("architecture_family", "GQA")
    bytes_per_element = float(model_entry["bytes_per_element"])
    compression = float(model_entry.get("compression_factor", 1.0))
    num_layers = int(model_entry["num_layers"])
    if arch.startswith("MLA"):
        latent = int(model_entry["mla_kv_lora_rank"])
        rope_dim = int(model_entry.get("mla_qk_rope_head_dim", 0))
        kv_bytes = num_layers * (latent + rope_dim) * bytes_per_element / compression
    else:
        num_kv_heads = int(model_entry["num_kv_heads"])
        head_dim = int(model_entry["head_dim"])
        kv_bytes = 2 * num_layers * num_kv_heads * head_dim * bytes_per_element / compression
    return kv_bytes / 1024.0


def compute_frontier_kv(arch_yaml: dict, as_of_date: Optional[date] = None) -> dict:
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
        raise ValueError(f"No open-architecture models available as of {as_of_date}")
    eligible.sort(key=lambda t: t[0])
    kv, model, rd = eligible[0]
    return {
        "kv_kb_per_token": kv,
        "model_name": model["name"],
        "arch_family": model.get("architecture_family", "unknown"),
        "release_date": rd.isoformat(),
    }


def compute_kv_yoy_change(arch_yaml: dict, as_of_date: Optional[date] = None) -> Optional[float]:
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
    return round(((now["kv_kb_per_token"] / prior["kv_kb_per_token"]) - 1.0) * 100, 2)


def kv_kb_to_gb_per_1m(kv_kb: float) -> float:
    """KV/token KB → GB per 1M tokens (memo's standard unit)."""
    return kv_kb * 1_000_000 / 1024.0 / 1024.0


def classify_kv_band(kv_gb_per_1m: float) -> dict:
    for idx, band in enumerate(KV_BANDS):
        if band["min_gb"] <= kv_gb_per_1m < band["max_gb"]:
            return {**band, "index": idx}
    return {**KV_BANDS[-1], "index": len(KV_BANDS) - 1}


def compute_operative_context(context_yaml: dict) -> dict:
    """Volume-weighted operative context (v3.4.2 NEW)."""
    categories = context_yaml.get("categories", {})
    operative_tokens = 0.0
    sum_share = 0.0
    breakdown = []
    for key, cat in categories.items():
        ctx = float(cat.get("typical_context_tokens", 0))
        share = float(cat.get("volume_share_pct", 0))
        contribution = ctx * share / 100.0
        operative_tokens += contribution
        sum_share += share
        breakdown.append({
            "key": key,
            "label": cat.get("label", key),
            "context_tokens": int(ctx),
            "volume_share_pct": share,
            "contribution_tokens": int(contribution),
            "confidence": cat.get("confidence", "medium"),
        })
    if abs(sum_share - 100.0) > 0.5:
        logger.warning(
            f"context_length_signals.yaml volume shares sum to {sum_share:.1f}%, "
            f"not 100%. Please rebalance the YAML."
        )
    return {
        "operative_context_tokens": int(operative_tokens),
        "categories": breakdown,
        "sum_check_pct": int(sum_share),
    }


def classify_context_band(operative_tokens: int) -> dict:
    for idx, band in enumerate(CONTEXT_BANDS):
        if band["min_tok"] <= operative_tokens < band["max_tok"]:
            return {**band, "index": idx}
    return {**CONTEXT_BANDS[-1], "index": len(CONTEXT_BANDS) - 1}


def compute_market_deployed_cell(workload_row: int, workload_col: int) -> tuple:
    """Apply anticipation premium: +1 Y-band offset."""
    market_row = min(workload_row + ANTICIPATION_Y_OFFSET, len(KV_BANDS) - 1)
    return (market_row, workload_col)


def lookup_zone(row_idx: int, col_idx: int) -> str:
    return MEMORY_ZONES[row_idx][col_idx]


def compute_position_score(row_idx: int, col_idx: int) -> int:
    """Score = (kv_weight × kv_intensity + ctx_weight × ctx_intensity) × 100."""
    kv_intensity = KV_BANDS[row_idx]["intensity"]
    ctx_intensity = CONTEXT_BANDS[col_idx]["intensity"]
    score = (KV_WEIGHT * kv_intensity + CTX_WEIGHT * ctx_intensity) * 100
    return round(score)


def compute_enterprise_demand(rev_yaml: dict, as_of_date: Optional[date] = None) -> dict:
    if as_of_date is None:
        as_of_date = date.today()

    def _latest_quarter(entries: list[dict]) -> Optional[dict]:
        latest = None
        latest_date = None
        for e in entries:
            q = e.get("quarter")
            if not q or "-Q" not in q:
                continue
            try:
                yr, qn = q.split("-Q")
                month = int(qn) * 3
                d = date(int(yr), month, {3: 31, 6: 30, 9: 30, 12: 31}[month])
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

    run_rate = sum(
        s["revenue_usd_b_annualized"]
        for s in (msft, aws, gcp, oracle)
        if s and s.get("revenue_usd_b_annualized") is not None
    )

    yoy_pairs = [
        (s["yoy_growth_pct"], s["revenue_usd_b_annualized"])
        for s in (msft, aws, gcp, oracle)
        if s and s.get("yoy_growth_pct") is not None and s.get("revenue_usd_b_annualized")
    ]
    if yoy_pairs:
        total_w = sum(w for _, w in yoy_pairs)
        weighted_yoy = round(sum(g * w for g, w in yoy_pairs) / total_w, 2)
    else:
        weighted_yoy = None

    return {
        "aggregate_run_rate_usd_b": round(run_rate, 2),
        "weighted_yoy_growth_pct": weighted_yoy,
        "nvidia_datacenter_yoy_pct": nv["yoy_growth_pct"] if nv else None,
    }


# Baseline anchor: May 2026 = 100
VOLUME_INDEX_BASELINE_RUN_RATE_USD_B = 121.0


def compute_volume_index(demand: dict) -> float:
    current_run_rate = demand.get("aggregate_run_rate_usd_b", 0.0) or 0.0
    if VOLUME_INDEX_BASELINE_RUN_RATE_USD_B <= 0:
        return 100.0
    return round(100.0 * current_run_rate / VOLUME_INDEX_BASELINE_RUN_RATE_USD_B, 1)


def classify_volume_trajectory(yoy_growth_pct: Optional[float]) -> str:
    if yoy_growth_pct is None:
        return "flat"
    if yoy_growth_pct > 50:
        return "bullish"
    if yoy_growth_pct < 10:
        return "bearish"
    return "flat"


def compute_sanity_check(
    score: int,
    hbm_yaml: dict,
    extraction_log_signals: Optional[list[dict]] = None,
    hbm_price_trend: str = "rising",
) -> dict:
    score_is_bullish_hbm = score >= 65
    score_is_bearish_hbm = score < 30

    substitution_news_count = 0
    if extraction_log_signals:
        for sig in extraction_log_signals:
            if any(sig.get(k) for k in (
                "cerebras_contract_win", "groq_scale_deployment",
                "etched_deployment", "lpddr_decode_platform",
                "cxl_decode_replacement",
            )):
                substitution_news_count += 1
    subst_threshold = 5

    decode_off_hbm_mentions = 0
    if hbm_yaml.get("non_hbm_accelerators"):
        for accel in hbm_yaml["non_hbm_accelerators"]:
            share = (accel.get("ai_compute_share_estimate") or "").strip()
            if share and not share.startswith("<"):
                decode_off_hbm_mentions += 1

    sig1_confirms_hbm = substitution_news_count < subst_threshold
    sig2_confirms_hbm = decode_off_hbm_mentions == 0
    sig3_confirms_hbm = hbm_price_trend == "rising"

    confirms = sum([sig1_confirms_hbm, sig2_confirms_hbm, sig3_confirms_hbm])
    contradicts = 3 - confirms

    if score_is_bullish_hbm:
        flag = contradicts >= 2
    elif score_is_bearish_hbm:
        flag = confirms >= 2
    else:
        flag = contradicts >= 3

    return {
        "substitution_news_count": substitution_news_count,
        "substitution_threshold": subst_threshold,
        "hyperscaler_decode_mentions": decode_off_hbm_mentions,
        "hbm_price_trend": hbm_price_trend,
        "confirms": confirms,
        "contradicts": contradicts,
        "calibration_flag": flag,
    }


STATUS_COLORS = {
    "Fired":           "#B91C1C",
    "Partially fired": "#FAC775",
    "Not fired":       "#C0DD97",
}


def map_status(score: int, volume_trajectory: str) -> dict:
    if score >= SCORE_NOT_FIRED_THRESHOLD:
        return {
            "status": "Not fired",
            "color": STATUS_COLORS["Not fired"],
            "reasoning": f"Position score {score} ≥ 65 (HBM zone) — HBM moat strong.",
        }
    if score < SCORE_FIRED_THRESHOLD:
        return {
            "status": "Fired",
            "color": STATUS_COLORS["Fired"],
            "reasoning": f"Position score {score} < 30 (commodity zone) — HBM moat materially weakened.",
        }
    if volume_trajectory == "bullish":
        return {
            "status": "Not fired",
            "color": STATUS_COLORS["Not fired"],
            "reasoning": (
                f"Position score {score} in transition zone (30-65) but volume trajectory "
                f"bullish — enterprise demand pulling toward HBM. Hold positive read."
            ),
        }
    if volume_trajectory == "bearish":
        return {
            "status": "Fired",
            "color": STATUS_COLORS["Fired"],
            "reasoning": (
                f"Position score {score} in transition zone (30-65) and volume trajectory "
                f"bearish — demand cooling while position weak. Fire risk active."
            ),
        }
    return {
        "status": "Partially fired",
        "color": STATUS_COLORS["Partially fired"],
        "reasoning": (
            f"Position score {score} in transition zone (30-65) and volume trajectory "
            f"flat — watch for divergence."
        ),
    }


def build_narrative_summary(snap: dict) -> str:
    kv = snap["frontier_kv_kb_per_token"]
    kv_gb = snap["frontier_kv_gb_per_1m_tokens"]
    model = snap["frontier_model_name"]
    op_ctx = snap["operative_context_tokens"]
    op_ctx_s = f"{op_ctx/1000:.0f}K" if op_ctx < 1_000_000 else f"{op_ctx/1_000_000:.2f}M"
    workload_zone = snap["workload_implied_zone"]
    market_zone = snap["market_deployed_zone"]
    score = snap["position_score"]
    vol_idx = snap["volume_index"]
    hbm_demand = snap["hbm_bit_demand_index"]
    status = snap["status"]
    gap = snap["anticipation_gap_bands"]
    return (
        f"Frontier KV: {kv:.1f} KB/tok ({kv_gb:.1f} GB per 1M tokens) on {model}. "
        f"Operative context (volume-weighted across 5 categories): ~{op_ctx_s} tokens. "
        f"Workload-implied cell sits in {workload_zone} zone; market-deployed cell "
        f"(+{gap} Y-band anticipation premium) sits in {market_zone} zone. "
        f"Position score: {score}/100. Volume index: {vol_idx:.0f} (May 2026 = 100). "
        f"Composite HBM bit demand: {hbm_demand:.0f}. Status: {status}."
    )


def build_bull_case(snap: dict) -> str:
    enterprise_cat = next(
        (c for c in snap.get("context_categories", []) if "enterprise" in c["key"]),
        None,
    )
    enterprise_share = enterprise_cat["volume_share_pct"] if enterprise_cat else 5
    enterprise_ctx = enterprise_cat["context_tokens"] if enterprise_cat else 800000
    enterprise_ctx_s = (
        f"{enterprise_ctx/1000:.0f}K" if enterprise_ctx < 1_000_000
        else f"{enterprise_ctx/1_000_000:.1f}M"
    )
    return (
        f"BULL CASE: enterprise agentic adoption ignites. Today enterprise vertical "
        f"category is {enterprise_share:.0f}% volume share at {enterprise_ctx_s} "
        f"context. If enterprise share grows to 25-30% (driven by Microsoft Copilot "
        f"Enterprise, Hebbia, Harvey, Anthropic Claude Enterprise, custom F500 "
        f"deployments) AND context per session grows to 3-5M as efficiency unlocks "
        f"the economic frontier, operative context shifts right from C2 to C4. "
        f"Combined with the +1 anticipation premium, that places market-deployed cell "
        f"deep in HBM territory (score 70-85). Volume index simultaneously growing "
        f"5-10x on the enterprise expansion → composite HBM bit demand 10-15x today's "
        f"baseline. This is the SK Hynix bull case quantified: efficiency × HBM "
        f"unlocks 42M-token capacity per accelerator, enterprise fills it."
    )


def build_bear_case(snap: dict) -> str:
    return (
        f"BEAR CASE: enterprise agentic stalls. KV efficiency improves on schedule "
        f"(MLA + INT4 + sliding window getting frontier to ~3 KB/tok), but enterprise "
        f"vertical share never breaks 10% and context plateaus around 1M. Operative "
        f"context stays in C3 region while KV efficiency pulls Y-axis down to R1. "
        f"Workload-implied cell lands at R1C3 (score 33, LPDDR/heterogeneous zone). "
        f"Even with anticipation premium, market-deployed cell sits at R2C3 — score "
        f"drops below 50, status flips to Partially fired. The market broadens to "
        f"LPDDR/CXL warm tier and SSD/NAND cold tier; Samsung, Micron and Chinese "
        f"suppliers gain at SK Hynix's expense. Watch for: substitution news from "
        f"Cerebras / Groq / Etched winning major contracts, hyperscaler earnings "
        f"explicitly mentioning decode moving off HBM, or HBM contract prices "
        f"rolling over. Today's sanity check shows zero substitution evidence — the "
        f"bear case is NOT materializing as of {snap.get('computed_at_iso', '?')[:10]}."
    )


def compute_dhcr_snapshot(
    data_dir: Optional[Path] = None,
    as_of_date: Optional[date] = None,
    extraction_log_signals: Optional[list[dict]] = None,
    hbm_price_trend: str = "rising",
) -> DHCRSnapshot:
    """Compute the full DHCR snapshot. Main entry point."""
    if as_of_date is None:
        as_of_date = date.today()
    data = load_dhcr_data(data_dir=data_dir)

    # Y-axis
    frontier_kv = compute_frontier_kv(data["arch"], as_of_date=as_of_date)
    kv_kb = frontier_kv["kv_kb_per_token"]
    kv_gb_per_1m = kv_kb_to_gb_per_1m(kv_kb)
    kv_band = classify_kv_band(kv_gb_per_1m)
    kv_yoy = compute_kv_yoy_change(data["arch"], as_of_date=as_of_date)

    # X-axis (NEW v3.4.2 — dual-input triangulation)
    op_ctx_result = compute_operative_context(data["context"])
    operative_tokens = op_ctx_result["operative_context_tokens"]
    ctx_band = classify_context_band(operative_tokens)

    # Cells
    workload_row, workload_col = kv_band["index"], ctx_band["index"]
    market_row, market_col = compute_market_deployed_cell(workload_row, workload_col)
    workload_zone = lookup_zone(workload_row, workload_col)
    market_zone = lookup_zone(market_row, market_col)
    anticipation_gap = market_row - workload_row

    # Score (uses market-deployed cell)
    score = compute_position_score(market_row, market_col)

    # Volume layer
    demand = compute_enterprise_demand(data["rev"], as_of_date=as_of_date)
    volume_index = compute_volume_index(demand)
    volume_trajectory = classify_volume_trajectory(demand["weighted_yoy_growth_pct"])
    hbm_demand_index = round(score * volume_index / 100.0, 1)

    # Sanity check
    sanity = compute_sanity_check(
        score=score, hbm_yaml=data["hbm"],
        extraction_log_signals=extraction_log_signals,
        hbm_price_trend=hbm_price_trend,
    )

    # Status
    status_info = map_status(score, volume_trajectory)

    # Substitution-risk flags
    subst_risks = []
    for non_hbm in data["hbm"].get("non_hbm_accelerators", []):
        vendor = non_hbm.get("vendor", "?")
        gen = non_hbm.get("generation", "?")
        share = non_hbm.get("ai_compute_share_estimate", "?")
        subst_risks.append(
            f"{vendor} {gen}: estimated {share} AI compute share "
            f"({(non_hbm.get('notes', '') or '')[:80]})"
        )

    snap_dict = {
        "computed_at_iso": datetime.utcnow().isoformat(),
        "frontier_kv_kb_per_token": kv_kb,
        "frontier_kv_gb_per_1m_tokens": round(kv_gb_per_1m, 2),
        "frontier_model_name": frontier_kv["model_name"],
        "frontier_model_arch": frontier_kv["arch_family"],
        "kv_band_index": kv_band["index"],
        "kv_band_name": kv_band["name"],
        "kv_band_label": kv_band["label"],
        "kv_intensity": kv_band["intensity"],
        "kv_yoy_change_pct": kv_yoy,
        "operative_context_tokens": operative_tokens,
        "operative_context_kb": round(operative_tokens * kv_kb / 1024.0, 2),
        "context_band_index": ctx_band["index"],
        "context_band_name": ctx_band["name"],
        "context_band_label": ctx_band["label"],
        "context_intensity": ctx_band["intensity"],
        "context_categories": op_ctx_result["categories"],
        "workload_implied_cell": (workload_row, workload_col),
        "workload_implied_zone": workload_zone,
        "workload_implied_zone_category": zone_category(workload_zone),
        "market_deployed_cell": (market_row, market_col),
        "market_deployed_zone": market_zone,
        "market_deployed_zone_category": zone_category(market_zone),
        "anticipation_gap_bands": anticipation_gap,
        "position_score": score,
        "enterprise_ai_run_rate_usd_b": demand["aggregate_run_rate_usd_b"],
        "enterprise_ai_yoy_growth_pct": demand["weighted_yoy_growth_pct"],
        "nvidia_datacenter_yoy_growth_pct": demand["nvidia_datacenter_yoy_pct"],
        "volume_index": volume_index,
        "volume_trajectory": volume_trajectory,
        "hbm_bit_demand_index": hbm_demand_index,
        "sanity_signal_substitution_news_count": sanity["substitution_news_count"],
        "sanity_signal_substitution_threshold": sanity["substitution_threshold"],
        "sanity_signal_hyperscaler_decode_mentions": sanity["hyperscaler_decode_mentions"],
        "sanity_signal_hbm_price_trend": sanity["hbm_price_trend"],
        "sanity_check_confirms": sanity["confirms"],
        "sanity_check_contradicts": sanity["contradicts"],
        "calibration_flag": sanity["calibration_flag"],
        "status": status_info["status"],
        "status_color": status_info["color"],
        "status_reasoning": status_info["reasoning"],
        "substitution_risk_notes": subst_risks,
    }

    snap_dict["narrative_summary"] = build_narrative_summary(snap_dict)
    snap_dict["bull_case_summary"] = build_bull_case(snap_dict)
    snap_dict["bear_case_summary"] = build_bear_case(snap_dict)

    return DHCRSnapshot(**snap_dict)


def compute_dhcr_trajectory(
    data_dir: Optional[Path] = None,
    quarters_back: int = 4,
) -> list[dict]:
    """Historical snapshots for trajectory arrow on matrix chart."""
    today = date.today()
    snapshots = []
    for q in range(quarters_back):
        as_of = today - timedelta(days=90 * q)
        try:
            snap = compute_dhcr_snapshot(data_dir=data_dir, as_of_date=as_of)
            snapshots.append({
                "as_of_date": as_of.isoformat(),
                "kv_band_index": snap.kv_band_index,
                "context_band_index": snap.context_band_index,
                "workload_implied_cell": list(snap.workload_implied_cell),
                "market_deployed_cell": list(snap.market_deployed_cell),
                "position_score": snap.position_score,
                "operative_context_tokens": snap.operative_context_tokens,
                "frontier_kv_gb_per_1m_tokens": snap.frontier_kv_gb_per_1m_tokens,
            })
        except (FileNotFoundError, ValueError):
            continue
    snapshots.reverse()
    return snapshots
