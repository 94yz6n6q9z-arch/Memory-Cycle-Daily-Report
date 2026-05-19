"""
Cycle matrix evaluation for the Memory Cycle Brief.

Implements the 5x5 DRAM cycle trigger matrix that maps:
  - Y-axis (rows): DRAM contract QoQ growth bands (1=>=+30%  ...  5=<=+5%)
  - X-axis (cols): DDR5 16Gb 4800/5600 spot drawdown from rolling 12-week peak
                   (1=at new highs ... 5=>=20% drop from peak)
  - Output: HOLD / WARN / FIRE verdict per cell.

Position rules:
  - Spot column derived from drawdown MAGNITUDE only.
    Duration since new high is captured as a soft annotation but does not
    move the column.
  - Contract row derived from a synthetic adjusted estimate that combines:
      * Anchor: most recent unsuperseded TrendForce quarterly forecast
                (e.g., +58-63% for Q2 2026 -> central 60.5%)
      * Tone adjustment: TrendForce news commentary keywords scored to pp
      * Supplier adjustment: median deviation of supplier-specific Korean
        media figures (Samsung / SK Hynix / Micron) from the anchor
      * Spot lead adjustment: DDR5 16Gb 4800/5600 spot 4-week trajectory
        treated as a forward indicator for contract direction
    Default weights: 0.4 tone, 0.4 supplier, 0.2 spot-lead. Configurable.

Inputs (CSV files; schemas at the bottom of this module):
  - dram_spot_history.csv                 daily session prices per SKU
  - dram_contract_quarterly_forecasts.csv quarterly forecasts w/ revisions
  - dram_contract_signals.csv             tone/supplier/news signals

Output: MatrixVerdict dataclass with cell, contributing inputs, and a
human-readable narrative ready for inclusion in the email body.

This module does NO I/O of its own beyond reading the CSVs it is handed.
Scraping and persistence live elsewhere; this module is the analytical engine.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from enum import Enum
from pathlib import Path
from typing import Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================================
# Constants — thresholds, weights, mappings
# ============================================================================

# The framework's focal spot SKU. Used to compute the spot column.
PRIMARY_SPOT_SKU = "DDR5_16Gb_4800_5600"

# Rolling window for peak/drawdown computation, in weeks.
ROLLING_PEAK_WINDOW_WEEKS = 12

# Lookback window for contract tone/supplier signal aggregation, in weeks.
SIGNALS_LOOKBACK_WEEKS = 4

# Lookback window for spot trajectory as contract-lead signal, in weeks.
SPOT_LEAD_LOOKBACK_WEEKS = 4

# Default weights for synthetic contract estimate components.
# Must sum to <= 1.0. Anchor takes (1.0 - sum) implicitly.
DEFAULT_WEIGHT_TONE = 0.4
DEFAULT_WEIGHT_SUPPLIER = 0.4
DEFAULT_WEIGHT_SPOT_LEAD = 0.2

# Maximum magnitude of any single adjustment (pp), to prevent outlier signals
# from dominating the synthetic estimate.
MAX_ADJUSTMENT_PP = 30.0


# ----------------------------------------------------------------------------
# Spot column thresholds
# Spot drawdown is expressed as a negative number when below peak.
# Examples: drawdown = 0.0 means at peak. drawdown = -7.5 means 7.5% below.
# ----------------------------------------------------------------------------

SPOT_COL_THRESHOLDS = [
    # (lower_bound_inclusive, upper_bound_exclusive, column_id, label)
    #  Drawdown is in percent (negative for declines).
    (-math.inf, -20.0, 5, ">=20% drop from peak"),
    (-20.0, -10.0, 4, "10-20% drop from peak"),
    (-10.0, -5.0, 3, "5-10% drop from peak"),
    (-5.0, 0.0, 2, "Pullback <5% from peak"),
    (0.0, math.inf, 1, "At new highs / stable"),
]


# ----------------------------------------------------------------------------
# Contract row thresholds
# Contract growth is expressed as QoQ percent (positive for growth).
# ----------------------------------------------------------------------------

CONTRACT_ROW_THRESHOLDS = [
    # (lower_bound_inclusive, upper_bound_exclusive, row_id, label)
    (30.0, math.inf, 1, ">=+30% QoQ"),
    (20.0, 30.0, 2, "+20 to +30% QoQ"),
    (10.0, 20.0, 3, "+10 to +20% QoQ"),
    (5.0, 10.0, 4, "+5 to +10% QoQ"),
    (-math.inf, 5.0, 5, "<=+5% QoQ"),
]


# ----------------------------------------------------------------------------
# The 5x5 matrix lookup
# Key: (contract_row, spot_col). Value: HOLD / WARN / FIRE.
# This is the canonical matrix locked in v3.39.7.
# ----------------------------------------------------------------------------

class MatrixCell(Enum):
    HOLD = "Hold"
    WARN = "Warn"
    FIRE = "Fire"


MATRIX: dict[tuple[int, int], MatrixCell] = {
    # Row 1 (>=+30% QoQ): mostly Hold, Fire only at >=20% spot drop
    (1, 1): MatrixCell.HOLD, (1, 2): MatrixCell.HOLD, (1, 3): MatrixCell.HOLD,
    (1, 4): MatrixCell.WARN, (1, 5): MatrixCell.FIRE,

    # Row 2 (+20 to +30%): Hold at low drawdown, Warn at moderate, Fire at >=20%
    (2, 1): MatrixCell.HOLD, (2, 2): MatrixCell.HOLD, (2, 3): MatrixCell.WARN,
    (2, 4): MatrixCell.WARN, (2, 5): MatrixCell.FIRE,

    # Row 3 (+10 to +20%): Warn dominant, Fire at deeper drawdown
    (3, 1): MatrixCell.WARN, (3, 2): MatrixCell.WARN, (3, 3): MatrixCell.WARN,
    (3, 4): MatrixCell.FIRE, (3, 5): MatrixCell.FIRE,

    # Row 4 (+5 to +10%): all Fire (historical peak band entry)
    (4, 1): MatrixCell.FIRE, (4, 2): MatrixCell.FIRE, (4, 3): MatrixCell.FIRE,
    (4, 4): MatrixCell.FIRE, (4, 5): MatrixCell.FIRE,

    # Row 5 (<=+5%): all Fire (already past peak indicator)
    (5, 1): MatrixCell.FIRE, (5, 2): MatrixCell.FIRE, (5, 3): MatrixCell.FIRE,
    (5, 4): MatrixCell.FIRE, (5, 5): MatrixCell.FIRE,
}


# ----------------------------------------------------------------------------
# Tone phrase to pp adjustment mapping
# These are heuristic conversions from qualitative TrendForce news commentary
# into numerical pp adjustments to the contract growth anchor.
# Calibration: rough first pass. Tune as we accumulate matched outcomes.
# Phrases are checked case-insensitively as substrings.
# ----------------------------------------------------------------------------

TONE_PHRASE_TO_PP: dict[str, float] = {
    # Strongly bearish phrases (deceleration signals)
    "ending the extreme seller's market": -15.0,
    "ending the seller's market": -15.0,
    "demand destruction": -20.0,
    "price cuts failing to revive demand": -15.0,
    "price cuts not stimulating": -15.0,
    "transactions slowing": -10.0,
    "buyers cautious": -8.0,
    "inventory rebuilding": -10.0,
    "cooling negotiations": -10.0,
    "momentum slowed": -5.0,
    "momentum slowing": -5.0,
    "softening": -5.0,
    "stabilized": -3.0,    # plateau language, mild bearish vs accelerating cycle
    "consolidating": -3.0,

    # Neutral / stable
    "stable": 0.0,
    "balanced": 0.0,
    "sustained": 0.0,

    # Bullish phrases (acceleration signals)
    "shortage intensifying": 10.0,
    "supply gap widening": 10.0,
    "extreme tightness": 10.0,
    "supplier raising further": 8.0,
    "structural shortage": 5.0,
    "demand outpacing supply": 5.0,
    "long-term agreements signed": 3.0,
}


# Mapping for tone_direction enum values used in the signals CSV.
# Used as a fallback when no specific phrase matches but a coarse direction
# was tagged by the Haiku extraction step.
TONE_DIRECTION_TO_PP: dict[str, float] = {
    "rising": 5.0,
    "stable": 0.0,
    "softening": -5.0,
    "cooling": -10.0,
    "declining": -15.0,
}


# ----------------------------------------------------------------------------
# Source reliability weights for signal aggregation
# Higher = more trusted when computing weighted average of signals
# ----------------------------------------------------------------------------

SOURCE_RELIABILITY: dict[str, float] = {
    "trendforce_press_release": 1.0,
    "trendforce_news": 0.8,
    "electronic_times": 0.7,        # Korean Electronic Times (etnews)
    "chosunbiz": 0.7,
    "businesskorea": 0.6,
    "theelec": 0.65,
    "digitimes": 0.7,
    "earnings_call": 0.9,           # Hynix/Samsung/Micron management
    "tomshardware": 0.4,            # aggregator
    "wccftech": 0.3,                # aggregator
    "semianalysis": 0.85,           # if cited
    "blog_industry": 0.3,           # BuySellRam etc.
    "default": 0.5,
}


# ============================================================================
# Dataclasses — structured types for inputs and outputs
# ============================================================================

@dataclass
class MatrixConfig:
    """Configurable knobs for the matrix evaluation."""
    rolling_peak_window_weeks: int = ROLLING_PEAK_WINDOW_WEEKS
    signals_lookback_weeks: int = SIGNALS_LOOKBACK_WEEKS
    spot_lead_lookback_weeks: int = SPOT_LEAD_LOOKBACK_WEEKS
    weight_tone: float = DEFAULT_WEIGHT_TONE
    weight_supplier: float = DEFAULT_WEIGHT_SUPPLIER
    weight_spot_lead: float = DEFAULT_WEIGHT_SPOT_LEAD
    max_adjustment_pp: float = MAX_ADJUSTMENT_PP
    primary_spot_sku: str = PRIMARY_SPOT_SKU


@dataclass
class SpotState:
    """Current spot price state for the primary SKU."""
    sku: str
    as_of_date: date
    current_session_avg: float
    weekly_mean_current: float
    rolling_peak_price: float
    rolling_peak_date: Optional[date]
    drawdown_pct: float                     # Negative when below peak
    weeks_since_peak: int                   # Soft annotation
    wow_change_pct: Optional[float]         # Week-over-week of weekly means
    weeks_observed: int                     # How many weeks of history we have
    spot_column: int
    spot_column_label: str


@dataclass
class ContractAdjustment:
    """A single adjustment to the contract growth anchor."""
    component: str            # "tone" | "supplier" | "spot_lead"
    pp: float                 # Adjustment in percentage points (signed)
    weight: float             # Effective weight applied
    sources_count: int        # Number of source signals contributing
    notes: str = ""


@dataclass
class ContractState:
    """Synthesized contract QoQ growth rate state."""
    anchor_quarter: Optional[str]
    anchor_low_pct: Optional[float]
    anchor_high_pct: Optional[float]
    anchor_central_pct: Optional[float]
    anchor_source: Optional[str]
    anchor_published_date: Optional[date]
    anchor_age_days: Optional[int]

    adjustments: list[ContractAdjustment]

    synthetic_central_pct: Optional[float]
    synthetic_low_pct: Optional[float]
    synthetic_high_pct: Optional[float]

    contract_row: int
    contract_row_label: str

    # Confidence indicators
    sources_count_total: int
    has_anchor: bool


@dataclass
class MatrixVerdict:
    """Full output of a matrix evaluation."""
    as_of: datetime
    cell: MatrixCell
    contract_row: int
    spot_col: int
    spot: SpotState
    contract: ContractState
    cell_label: str
    narrative: str
    warnings: list[str] = field(default_factory=list)


# ============================================================================
# Spot calculations
# ============================================================================

def compute_weekly_means(
    spot_history: pd.DataFrame,
    sku: str,
) -> pd.DataFrame:
    """Aggregate daily session averages into ISO-week weekly means.

    Returns a DataFrame indexed by week-ending date with columns:
      - weekly_mean
      - daily_observations (count)
    """
    df = spot_history[spot_history["sku"] == sku].copy()
    if df.empty:
        return pd.DataFrame(columns=["weekly_mean", "daily_observations"])

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["week_ending"] = pd.to_datetime(df["date"]).apply(
        lambda d: d + timedelta(days=(6 - d.weekday()))  # ISO week ending Sun
    ).dt.date

    weekly = df.groupby("week_ending").agg(
        weekly_mean=("session_avg", "mean"),
        daily_observations=("session_avg", "count"),
    )
    weekly = weekly.sort_index()
    return weekly


def compute_rolling_peak(
    weekly_means: pd.DataFrame,
    window_weeks: int = ROLLING_PEAK_WINDOW_WEEKS,
) -> tuple[float, Optional[date], int]:
    """Compute the rolling peak weekly mean within the lookback window.

    Returns (peak_price, peak_week_ending_date, weeks_since_peak).
    weeks_since_peak counts weeks from peak to most recent observation.
    """
    if weekly_means.empty:
        return (float("nan"), None, 0)

    window = weekly_means.tail(window_weeks)
    peak_price = window["weekly_mean"].max()
    peak_date = window["weekly_mean"].idxmax()

    most_recent_date = weekly_means.index[-1]
    weeks_since_peak = (
        pd.Timestamp(most_recent_date) - pd.Timestamp(peak_date)
    ).days // 7

    return (float(peak_price), peak_date, int(weeks_since_peak))


def compute_drawdown_pct(current: float, peak: float) -> float:
    """Compute drawdown as a signed percentage.

    Negative when current is below peak. Returns 0.0 if current == peak.
    """
    if peak <= 0 or math.isnan(peak):
        return 0.0
    return ((current - peak) / peak) * 100.0


def compute_wow_change_pct(weekly_means: pd.DataFrame) -> Optional[float]:
    """Week-over-week percent change of the weekly means."""
    if len(weekly_means) < 2:
        return None
    series = weekly_means["weekly_mean"]
    prior = series.iloc[-2]
    current = series.iloc[-1]
    if prior <= 0:
        return None
    return ((current - prior) / prior) * 100.0


def classify_spot_column(drawdown_pct: float) -> tuple[int, str]:
    """Map drawdown percentage to a spot column (1-5) and its label.

    Columns:
      1 = At new highs / stable (drawdown >= 0)
      2 = Pullback <5% from peak (drawdown in [-5, 0))
      3 = 5-10% drop from peak (drawdown in [-10, -5))
      4 = 10-20% drop from peak (drawdown in [-20, -10))
      5 = >=20% drop from peak (drawdown < -20)
    """
    for lower, upper, col_id, label in SPOT_COL_THRESHOLDS:
        if lower <= drawdown_pct < upper:
            return (col_id, label)
    # Fallback for drawdown exactly at +inf (shouldn't happen)
    return (1, "At new highs / stable")


def evaluate_spot_state(
    spot_history: pd.DataFrame,
    config: MatrixConfig,
) -> SpotState:
    """Compute the full spot state for the primary SKU."""
    sku = config.primary_spot_sku
    df_sku = spot_history[spot_history["sku"] == sku].copy()

    if df_sku.empty:
        raise ValueError(
            f"No spot history found for SKU '{sku}'. "
            f"Check dram_spot_history.csv contains rows with this SKU."
        )

    df_sku["date"] = pd.to_datetime(df_sku["date"]).dt.date
    df_sku = df_sku.sort_values("date")

    most_recent_row = df_sku.iloc[-1]
    current_session_avg = float(most_recent_row["session_avg"])
    as_of_date = most_recent_row["date"]

    weekly = compute_weekly_means(spot_history, sku)
    weekly_mean_current = (
        float(weekly["weekly_mean"].iloc[-1]) if not weekly.empty else current_session_avg
    )

    peak_price, peak_date, weeks_since_peak = compute_rolling_peak(
        weekly, config.rolling_peak_window_weeks
    )

    drawdown = compute_drawdown_pct(weekly_mean_current, peak_price)
    wow = compute_wow_change_pct(weekly)
    spot_col, spot_col_label = classify_spot_column(drawdown)

    return SpotState(
        sku=sku,
        as_of_date=as_of_date,
        current_session_avg=current_session_avg,
        weekly_mean_current=weekly_mean_current,
        rolling_peak_price=peak_price,
        rolling_peak_date=peak_date,
        drawdown_pct=drawdown,
        weeks_since_peak=weeks_since_peak,
        wow_change_pct=wow,
        weeks_observed=len(weekly),
        spot_column=spot_col,
        spot_column_label=spot_col_label,
    )


# ============================================================================
# Contract calculations
# ============================================================================

def select_anchor_forecast(
    contract_forecasts: pd.DataFrame,
    as_of: datetime,
) -> Optional[pd.Series]:
    """Pick the active forecast: most recent unsuperseded forecast for the
    current or upcoming quarter, preferring server-specific segments.

    Returns a row (pd.Series) or None if nothing is usable.
    """
    if contract_forecasts.empty:
        return None

    df = contract_forecasts.copy()
    df["published_date"] = pd.to_datetime(df["published_date"]).dt.date

    # Remove rows that are explicitly superseded (referenced by another row)
    superseded_ids = set(
        df["supersedes_id"].dropna().astype(int).tolist()
    ) if "supersedes_id" in df.columns else set()

    if "row_id" in df.columns:
        df = df[~df["row_id"].astype(int).isin(superseded_ids)]

    # Prefer server-specific segments; fall back to aggregate
    segment_priority = [
        "server_ddr5",
        "conventional_dram_aggregate",
        "samsung_specific",
        "sk_hynix_specific",
        "micron_specific",
    ]

    for segment in segment_priority:
        candidates = df[df["segment"] == segment].copy()
        if candidates.empty:
            continue
        candidates = candidates.sort_values("published_date", ascending=False)
        # Anchor must be a quarterly forecast (skip supplier_specific for anchor
        # purposes; those feed the supplier_adj instead).
        if segment in ("server_ddr5", "conventional_dram_aggregate"):
            return candidates.iloc[0]

    # No quarterly anchor found — accept supplier-specific only as last resort
    candidates = df[
        df["segment"].isin(["server_ddr5", "conventional_dram_aggregate"])
    ].sort_values("published_date", ascending=False)
    if not candidates.empty:
        return candidates.iloc[0]

    return None


def _phrase_matches(text: str, phrase: str) -> bool:
    """Case-insensitive substring match."""
    if not isinstance(text, str) or not text:
        return False
    return phrase.lower() in text.lower()


def score_tone_signal(
    headline: str,
    tone_keywords: str,
    tone_direction: str,
    raw_snippet: str,
) -> Optional[float]:
    """Score a single signal row to a pp adjustment.

    Priority:
      1. Match TONE_PHRASE_TO_PP keys against (tone_keywords, headline, raw_snippet)
         Sum all matches, capped at the per-signal cap.
      2. If no phrase match, fall back to TONE_DIRECTION_TO_PP[tone_direction].
      3. Return None if no signal is extractable.
    """
    text_blob = " ".join(
        s for s in (tone_keywords, headline, raw_snippet) if isinstance(s, str)
    )
    if not text_blob.strip():
        return TONE_DIRECTION_TO_PP.get(tone_direction.lower() if isinstance(tone_direction, str) else "", None)

    matched_pp = []
    for phrase, pp in TONE_PHRASE_TO_PP.items():
        if _phrase_matches(text_blob, phrase):
            matched_pp.append(pp)

    if matched_pp:
        # Sum matches but cap any single signal's contribution at +/-25pp
        total = sum(matched_pp)
        return max(-25.0, min(25.0, total))

    if isinstance(tone_direction, str):
        return TONE_DIRECTION_TO_PP.get(tone_direction.lower(), None)

    return None


def compute_tone_adjustment(
    contract_signals: pd.DataFrame,
    as_of: datetime,
    lookback_weeks: int = SIGNALS_LOOKBACK_WEEKS,
) -> ContractAdjustment:
    """Aggregate tone signals over the lookback window into a single pp adjustment.

    Each signal is scored independently and weighted by source reliability.
    Result is a weighted-average pp adjustment.
    """
    if contract_signals.empty:
        return ContractAdjustment(
            component="tone", pp=0.0, weight=0.0, sources_count=0,
            notes="No signals available",
        )

    df = contract_signals.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    cutoff = (as_of - timedelta(weeks=lookback_weeks)).date()
    df = df[df["date"] >= cutoff]

    if df.empty:
        return ContractAdjustment(
            component="tone", pp=0.0, weight=0.0, sources_count=0,
            notes=f"No signals within {lookback_weeks}w of {as_of.date()}",
        )

    weighted_sum = 0.0
    weight_total = 0.0
    used = 0

    for _, row in df.iterrows():
        pp = score_tone_signal(
            headline=row.get("headline", "") or "",
            tone_keywords=row.get("tone_keywords", "") or "",
            tone_direction=row.get("tone_direction", "") or "",
            raw_snippet=row.get("raw_snippet", "") or "",
        )
        if pp is None:
            continue
        source = row.get("source_name", "default") or "default"
        weight = SOURCE_RELIABILITY.get(source, SOURCE_RELIABILITY["default"])
        weighted_sum += pp * weight
        weight_total += weight
        used += 1

    if weight_total == 0.0 or used == 0:
        return ContractAdjustment(
            component="tone", pp=0.0, weight=0.0, sources_count=0,
            notes="No scoreable signals in window",
        )

    avg_pp = weighted_sum / weight_total
    # Clamp at module-level max
    avg_pp = max(-MAX_ADJUSTMENT_PP, min(MAX_ADJUSTMENT_PP, avg_pp))

    return ContractAdjustment(
        component="tone",
        pp=avg_pp,
        weight=DEFAULT_WEIGHT_TONE,
        sources_count=used,
        notes=f"Weighted avg of {used} signals over {lookback_weeks}w",
    )


def compute_supplier_adjustment(
    contract_signals: pd.DataFrame,
    contract_forecasts: pd.DataFrame,
    anchor_central_pct: Optional[float],
    as_of: datetime,
    lookback_weeks: int = SIGNALS_LOOKBACK_WEEKS,
) -> ContractAdjustment:
    """Compute supplier-specific adjustment.

    Sources of supplier-specific quoted percentages:
      (a) contract_signals rows with non-null qoq_pct_mentioned and
          supplier_mentioned in {"samsung", "sk_hynix", "micron"}
      (b) contract_forecasts rows with segment in {"samsung_specific",
          "sk_hynix_specific", "micron_specific"}

    Adjustment is the median deviation of supplier figures from the anchor,
    capped at MAX_ADJUSTMENT_PP.
    """
    if anchor_central_pct is None:
        return ContractAdjustment(
            component="supplier", pp=0.0, weight=0.0, sources_count=0,
            notes="No anchor to compare against",
        )

    supplier_pcts: list[float] = []

    # (a) Signals with quoted percentages
    if not contract_signals.empty:
        signals = contract_signals.copy()
        signals["date"] = pd.to_datetime(signals["date"]).dt.date
        cutoff = (as_of - timedelta(weeks=lookback_weeks)).date()
        signals = signals[signals["date"] >= cutoff]

        signal_suppliers = {"samsung", "sk_hynix", "micron"}
        for _, row in signals.iterrows():
            supplier = row.get("supplier_mentioned", "") or ""
            pct = row.get("qoq_pct_mentioned", None)
            if (
                isinstance(supplier, str)
                and supplier.lower() in signal_suppliers
                and pct is not None
                and not (isinstance(pct, float) and math.isnan(pct))
            ):
                supplier_pcts.append(float(pct))

    # (b) Forecasts with supplier-specific segments (within lookback window)
    if not contract_forecasts.empty:
        fcs = contract_forecasts.copy()
        fcs["published_date"] = pd.to_datetime(fcs["published_date"]).dt.date
        cutoff = (as_of - timedelta(weeks=lookback_weeks * 2)).date()  # wider for forecasts
        fcs = fcs[fcs["published_date"] >= cutoff]

        supplier_segs = {"samsung_specific", "sk_hynix_specific", "micron_specific"}
        for _, row in fcs.iterrows():
            if row.get("segment") in supplier_segs:
                central = row.get("central_pct")
                if pd.isna(central):
                    low, high = row.get("low_pct"), row.get("high_pct")
                    if pd.notna(low) and pd.notna(high):
                        central = (low + high) / 2.0
                if pd.notna(central):
                    supplier_pcts.append(float(central))

    if not supplier_pcts:
        return ContractAdjustment(
            component="supplier", pp=0.0, weight=0.0, sources_count=0,
            notes="No supplier-specific data in lookback window",
        )

    median_supplier_pct = sorted(supplier_pcts)[len(supplier_pcts) // 2]
    deviation_pp = median_supplier_pct - anchor_central_pct
    deviation_pp = max(-MAX_ADJUSTMENT_PP, min(MAX_ADJUSTMENT_PP, deviation_pp))

    return ContractAdjustment(
        component="supplier",
        pp=deviation_pp,
        weight=DEFAULT_WEIGHT_SUPPLIER,
        sources_count=len(supplier_pcts),
        notes=(
            f"Median supplier-specific QoQ {median_supplier_pct:.1f}% "
            f"vs anchor {anchor_central_pct:.1f}% "
            f"({len(supplier_pcts)} data points)"
        ),
    )


def compute_spot_lead_adjustment(
    spot_history: pd.DataFrame,
    config: MatrixConfig,
    as_of: datetime,
) -> ContractAdjustment:
    """Translate recent DDR5 spot trajectory into a contract-direction signal.

    Heuristic mapping of cumulative N-week WoW change to a pp adjustment:
      <= -8%  : -15pp  (strong forward signal of contract roll)
      -8 to -5%: -10pp
      -5 to -3%: -5pp
      -3 to -1%: -2pp
      -1 to +1%: 0pp   (noise)
      +1 to +3%: +2pp
      > +3%:    +5pp
    """
    sku = config.primary_spot_sku
    weekly = compute_weekly_means(spot_history, sku)
    if len(weekly) < config.spot_lead_lookback_weeks + 1:
        return ContractAdjustment(
            component="spot_lead", pp=0.0, weight=0.0, sources_count=0,
            notes="Insufficient spot history for lead signal",
        )

    window = weekly.tail(config.spot_lead_lookback_weeks + 1)
    start_price = float(window["weekly_mean"].iloc[0])
    end_price = float(window["weekly_mean"].iloc[-1])

    if start_price <= 0:
        return ContractAdjustment(
            component="spot_lead", pp=0.0, weight=0.0, sources_count=0,
            notes="Invalid start price",
        )

    cum_change_pct = ((end_price - start_price) / start_price) * 100.0

    if cum_change_pct <= -8.0:
        pp = -15.0
    elif cum_change_pct <= -5.0:
        pp = -10.0
    elif cum_change_pct <= -3.0:
        pp = -5.0
    elif cum_change_pct <= -1.0:
        pp = -2.0
    elif cum_change_pct < 1.0:
        pp = 0.0
    elif cum_change_pct < 3.0:
        pp = 2.0
    else:
        pp = 5.0

    return ContractAdjustment(
        component="spot_lead",
        pp=pp,
        weight=DEFAULT_WEIGHT_SPOT_LEAD,
        sources_count=1,
        notes=(
            f"DDR5 {sku} cumulative {config.spot_lead_lookback_weeks}w change "
            f"{cum_change_pct:+.2f}% -> {pp:+.1f}pp lead signal"
        ),
    )


def classify_contract_row(qoq_central_pct: float) -> tuple[int, str]:
    """Map QoQ growth percentage to a contract row (1-5) and its label."""
    for lower, upper, row_id, label in CONTRACT_ROW_THRESHOLDS:
        if lower <= qoq_central_pct < upper:
            return (row_id, label)
    # qoq exactly at boundary handled by inclusive comparison; fallback:
    return (5, "<=+5% QoQ")


def evaluate_contract_state(
    contract_forecasts: pd.DataFrame,
    contract_signals: pd.DataFrame,
    spot_history: pd.DataFrame,
    config: MatrixConfig,
    as_of: datetime,
) -> ContractState:
    """Compute the full contract state via anchor + adjustments."""

    anchor_row = select_anchor_forecast(contract_forecasts, as_of)

    if anchor_row is None:
        # No anchor at all — return a state that flags this clearly.
        return ContractState(
            anchor_quarter=None, anchor_low_pct=None, anchor_high_pct=None,
            anchor_central_pct=None, anchor_source=None,
            anchor_published_date=None, anchor_age_days=None,
            adjustments=[],
            synthetic_central_pct=None,
            synthetic_low_pct=None, synthetic_high_pct=None,
            contract_row=1,  # Default to most conservative band
            contract_row_label="No anchor available",
            sources_count_total=0,
            has_anchor=False,
        )

    anchor_low = float(anchor_row["low_pct"])
    anchor_high = float(anchor_row["high_pct"])
    anchor_central = (
        float(anchor_row["central_pct"])
        if pd.notna(anchor_row.get("central_pct"))
        else (anchor_low + anchor_high) / 2.0
    )
    anchor_quarter = str(anchor_row["quarter"])
    anchor_source = str(anchor_row.get("source", "unknown"))
    anchor_published = anchor_row["published_date"]
    if isinstance(anchor_published, pd.Timestamp):
        anchor_published = anchor_published.date()
    anchor_age = (as_of.date() - anchor_published).days

    # Compute each adjustment
    tone_adj = compute_tone_adjustment(
        contract_signals, as_of, config.signals_lookback_weeks
    )
    supplier_adj = compute_supplier_adjustment(
        contract_signals, contract_forecasts, anchor_central, as_of,
        config.signals_lookback_weeks,
    )
    spot_lead_adj = compute_spot_lead_adjustment(spot_history, config, as_of)

    adjustments = [tone_adj, supplier_adj, spot_lead_adj]

    # Synthetic central = anchor + weighted sum of adjustments
    synthetic_central = (
        anchor_central
        + tone_adj.pp * config.weight_tone
        + supplier_adj.pp * config.weight_supplier
        + spot_lead_adj.pp * config.weight_spot_lead
    )

    # Synthetic range: apply the same adjustment to both ends of the anchor band
    total_adj = synthetic_central - anchor_central
    synthetic_low = anchor_low + total_adj
    synthetic_high = anchor_high + total_adj

    # Floor at -50% (no realistic forecast below this for this cycle); ceiling 200%
    synthetic_central = max(-50.0, min(200.0, synthetic_central))
    synthetic_low = max(-50.0, min(200.0, synthetic_low))
    synthetic_high = max(-50.0, min(200.0, synthetic_high))

    contract_row, contract_row_label = classify_contract_row(synthetic_central)

    sources_total = sum(adj.sources_count for adj in adjustments)

    return ContractState(
        anchor_quarter=anchor_quarter,
        anchor_low_pct=anchor_low,
        anchor_high_pct=anchor_high,
        anchor_central_pct=anchor_central,
        anchor_source=anchor_source,
        anchor_published_date=anchor_published,
        anchor_age_days=anchor_age,
        adjustments=adjustments,
        synthetic_central_pct=synthetic_central,
        synthetic_low_pct=synthetic_low,
        synthetic_high_pct=synthetic_high,
        contract_row=contract_row,
        contract_row_label=contract_row_label,
        sources_count_total=sources_total,
        has_anchor=True,
    )


# ============================================================================
# Matrix evaluation orchestrator
# ============================================================================

def lookup_cell(contract_row: int, spot_col: int) -> MatrixCell:
    """Lookup the matrix cell for a given (row, col) coordinate."""
    return MATRIX.get((contract_row, spot_col), MatrixCell.HOLD)


def _generate_narrative(
    cell: MatrixCell,
    spot: SpotState,
    contract: ContractState,
) -> str:
    """Build a human-readable narrative paragraph for the email."""

    cell_phrasing = {
        MatrixCell.HOLD: "Hold full position.",
        MatrixCell.WARN: "Warn: review thesis, prepare partial trim (25-33%).",
        MatrixCell.FIRE: "Fire: exit substantial portion or full position.",
    }[cell]

    spot_phrase = (
        f"DDR5 16Gb 4800/5600 spot at ${spot.weekly_mean_current:.2f} "
        f"({spot.drawdown_pct:+.2f}% vs rolling {ROLLING_PEAK_WINDOW_WEEKS}w peak "
        f"${spot.rolling_peak_price:.2f}"
    )
    if spot.rolling_peak_date is not None:
        spot_phrase += f" set {spot.rolling_peak_date.isoformat()}"
    spot_phrase += f"). {spot.weeks_since_peak}w since last new peak."

    if contract.has_anchor:
        contract_phrase = (
            f"Contract anchor: {contract.anchor_quarter} "
            f"{contract.anchor_low_pct:.0f}-{contract.anchor_high_pct:.0f}% QoQ "
            f"({contract.anchor_source}, "
            f"published {contract.anchor_published_date}, "
            f"{contract.anchor_age_days}d old). "
            f"Synthetic adjusted: {contract.synthetic_central_pct:.1f}% central "
            f"(range {contract.synthetic_low_pct:.0f}-{contract.synthetic_high_pct:.0f}%)."
        )
        if contract.adjustments:
            adj_summary_parts = []
            for adj in contract.adjustments:
                if adj.sources_count > 0:
                    adj_summary_parts.append(
                        f"{adj.component} {adj.pp:+.1f}pp "
                        f"({adj.sources_count} src, weight {adj.weight:.1f})"
                    )
            if adj_summary_parts:
                contract_phrase += " Adjustments: " + "; ".join(adj_summary_parts) + "."
    else:
        contract_phrase = "Contract anchor: UNAVAILABLE. Defaulting to most conservative band."

    return (
        f"Cell: {cell.value} (Row {contract.contract_row} x Col {spot.spot_column}). "
        f"{cell_phrasing} {spot_phrase} {contract_phrase}"
    )


def evaluate_matrix(
    spot_history: Union[pd.DataFrame, Path, str],
    contract_forecasts: Union[pd.DataFrame, Path, str],
    contract_signals: Union[pd.DataFrame, Path, str],
    as_of: Optional[datetime] = None,
    config: Optional[MatrixConfig] = None,
) -> MatrixVerdict:
    """Run the full matrix evaluation pipeline.

    Each input may be a DataFrame or a path to a CSV file.
    Returns a MatrixVerdict including the cell, contributing inputs,
    and a human-readable narrative.
    """
    if as_of is None:
        as_of = datetime.now()
    if config is None:
        config = MatrixConfig()

    # Load inputs if paths
    spot_df = _load_csv(spot_history)
    forecasts_df = _load_csv(contract_forecasts)
    signals_df = _load_csv(contract_signals)

    warnings: list[str] = []

    if spot_df.empty:
        raise ValueError("Spot history is empty; cannot evaluate matrix.")

    if forecasts_df.empty:
        warnings.append(
            "Contract forecasts dataframe is empty. "
            "Matrix row will default to most conservative band."
        )

    if signals_df.empty:
        warnings.append(
            "Contract signals dataframe is empty. "
            "Tone and supplier adjustments will be zero."
        )

    spot_state = evaluate_spot_state(spot_df, config)
    contract_state = evaluate_contract_state(
        forecasts_df, signals_df, spot_df, config, as_of
    )

    # Stale anchor warning
    if contract_state.has_anchor and contract_state.anchor_age_days is not None:
        if contract_state.anchor_age_days > 90:
            warnings.append(
                f"Contract anchor is {contract_state.anchor_age_days} days old "
                f"(>90d). Next quarterly forecast may already be due."
            )

    # No supplier or tone data warning
    sources_count = contract_state.sources_count_total
    if contract_state.has_anchor and sources_count == 0:
        warnings.append(
            "No tone or supplier signals in lookback window. "
            "Synthetic estimate equals anchor central with no validation."
        )

    cell = lookup_cell(contract_state.contract_row, spot_state.spot_column)
    cell_label = (
        f"{cell.value} "
        f"(R{contract_state.contract_row} / C{spot_state.spot_column})"
    )
    narrative = _generate_narrative(cell, spot_state, contract_state)

    return MatrixVerdict(
        as_of=as_of,
        cell=cell,
        contract_row=contract_state.contract_row,
        spot_col=spot_state.spot_column,
        spot=spot_state,
        contract=contract_state,
        cell_label=cell_label,
        narrative=narrative,
        warnings=warnings,
    )


def _load_csv(source: Union[pd.DataFrame, Path, str]) -> pd.DataFrame:
    """Load a DataFrame from path or pass through if already a DataFrame."""
    if isinstance(source, pd.DataFrame):
        return source
    path = Path(source) if not isinstance(source, Path) else source
    if not path.exists():
        logger.warning(f"CSV not found: {path}; returning empty DataFrame.")
        return pd.DataFrame()
    return pd.read_csv(path)


# ============================================================================
# CSV schemas (canonical reference)
# ============================================================================

SPOT_HISTORY_SCHEMA = """
data/dram_spot_history.csv
  date              ISO 8601 yyyy-mm-dd (daily session date)
  sku               e.g. 'DDR5_16Gb_4800_5600', 'DDR5_16Gb_eTT',
                    'DDR4_16Gb_3200', 'DDR4_8Gb_3200', 'DDR5_RDIMM_32GB'
  session_avg       float, USD per chip (or per module for RDIMM)
  daily_high        float, USD (intraday high)
  daily_low         float, USD (intraday low)
  session_change_pct float, session-over-session percent change
  source            e.g. 'trendforce'
  scraped_at        ISO 8601 datetime
Primary key: (date, sku, source)
"""

CONTRACT_FORECASTS_SCHEMA = """
data/dram_contract_quarterly_forecasts.csv
  row_id            integer, auto-increment, used for supersedes_id
  quarter           e.g. 'Q1_2026', 'Q2_2026'
  segment           e.g. 'conventional_dram_aggregate', 'server_ddr5',
                    'samsung_specific', 'sk_hynix_specific',
                    'micron_specific', 'hbm_aggregate'
  low_pct           float, lower bound of forecast range
  high_pct          float, upper bound of forecast range
  central_pct       float, central estimate (defaults to (low+high)/2 if NaN)
  source            e.g. 'trendforce_press_release', 'electronic_times'
  source_url        URL
  published_date    ISO 8601 yyyy-mm-dd
  supersedes_id     integer row_id of forecast this one replaces (NaN if initial)
  notes             free text
"""

CONTRACT_SIGNALS_SCHEMA = """
data/dram_contract_signals.csv
  date              ISO 8601 yyyy-mm-dd
  source_name       e.g. 'trendforce_news', 'electronic_times', 'digitimes'
  source_url        URL
  headline          string
  tone_direction    enum: 'rising', 'stable', 'softening', 'cooling', 'declining'
  tone_keywords     comma-separated phrases extracted (for transparency)
  segment_mentioned e.g. 'server_ddr5', 'pc_dram', 'general'
  qoq_pct_mentioned float, explicit QoQ % cited in article (NaN if none)
  supplier_mentioned e.g. 'samsung', 'sk_hynix', 'micron', '' (none)
  raw_snippet       relevant excerpt (for human audit)
"""


# ============================================================================
# CLI entry point — usage example with toy fixtures
# ============================================================================

def _build_toy_fixtures() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build minimal toy fixtures matching today's situation for sanity check."""

    # Spot: 10 weeks of DDR5 16Gb 4800/5600 from March to May 2026
    spot_rows = [
        ("2026-03-12", "DDR5_16Gb_4800_5600", 39.40),
        ("2026-04-02", "DDR5_16Gb_4800_5600", 36.83),
        ("2026-04-08", "DDR5_16Gb_4800_5600", 37.00),
        ("2026-04-09", "DDR5_16Gb_4800_5600", 36.99),
        ("2026-04-28", "DDR5_16Gb_4800_5600", 38.67),
        ("2026-04-30", "DDR5_16Gb_4800_5600", 38.87),
        ("2026-05-05", "DDR5_16Gb_4800_5600", 38.83),
        ("2026-05-08", "DDR5_16Gb_4800_5600", 39.00),
        ("2026-05-11", "DDR5_16Gb_4800_5600", 39.83),
        ("2026-05-12", "DDR5_16Gb_4800_5600", 40.27),
        ("2026-05-15", "DDR5_16Gb_4800_5600", 41.17),
        ("2026-05-19", "DDR5_16Gb_4800_5600", 41.10),
    ]
    spot_df = pd.DataFrame(
        spot_rows, columns=["date", "sku", "session_avg"]
    )
    spot_df["daily_high"] = spot_df["session_avg"] * 1.10
    spot_df["daily_low"] = spot_df["session_avg"] * 0.75
    spot_df["session_change_pct"] = 0.0
    spot_df["source"] = "trendforce"
    spot_df["scraped_at"] = "2026-05-19T10:10:00Z"

    # Forecasts: Q1 initial + revision + Q2 initial
    forecasts_df = pd.DataFrame([
        {
            "row_id": 1, "quarter": "Q1_2026",
            "segment": "conventional_dram_aggregate",
            "low_pct": 55.0, "high_pct": 60.0, "central_pct": 57.5,
            "source": "trendforce_press_release",
            "source_url": "https://www.trendforce.com/presscenter/news/20260105-12860.html",
            "published_date": "2026-01-05",
            "supersedes_id": None,
            "notes": "Initial Q1 forecast",
        },
        {
            "row_id": 2, "quarter": "Q1_2026",
            "segment": "conventional_dram_aggregate",
            "low_pct": 90.0, "high_pct": 95.0, "central_pct": 92.5,
            "source": "trendforce_press_release",
            "source_url": "https://www.trendforce.com/presscenter/news/20260202-12911.html",
            "published_date": "2026-02-02",
            "supersedes_id": 1,
            "notes": "Revised UP from initial",
        },
        {
            "row_id": 3, "quarter": "Q2_2026",
            "segment": "conventional_dram_aggregate",
            "low_pct": 58.0, "high_pct": 63.0, "central_pct": 60.5,
            "source": "trendforce_press_release",
            "source_url": "https://www.trendforce.com/presscenter/news/20260331-12995.html",
            "published_date": "2026-03-31",
            "supersedes_id": None,
            "notes": "Initial Q2 forecast",
        },
        {
            "row_id": 4, "quarter": "Q2_2026",
            "segment": "samsung_specific",
            "low_pct": 28.0, "high_pct": 32.0, "central_pct": 30.0,
            "source": "electronic_times",
            "source_url": "https://www.etnews.com/...",
            "published_date": "2026-04-05",
            "supersedes_id": None,
            "notes": "Samsung Q2 contracts finalized ~+30% over Q1",
        },
    ])

    # Signals: tone shifts
    signals_df = pd.DataFrame([
        {
            "date": "2026-04-30",
            "source_name": "trendforce_news",
            "source_url": "https://www.trendforce.com/...",
            "headline": "PC DRAM contract prices rose, momentum slowed",
            "tone_direction": "softening",
            "tone_keywords": "momentum slowed,cooling negotiations,ending the extreme seller's market",
            "segment_mentioned": "pc_dram",
            "qoq_pct_mentioned": None,
            "supplier_mentioned": "",
            "raw_snippet": (
                "PC DRAM contract prices rose this quarter, but momentum slowed. "
                "High costs weakened PC sales, cooling negotiations and ending the "
                "extreme seller's market. Spot markets stabilized."
            ),
        },
        {
            "date": "2026-05-06",
            "source_name": "trendforce_news",
            "source_url": "https://www.trendforce.com/...",
            "headline": "Memory spot price update: DDR5 mainstream interest",
            "tone_direction": "stable",
            "tone_keywords": "",
            "segment_mentioned": "general",
            "qoq_pct_mentioned": None,
            "supplier_mentioned": "",
            "raw_snippet": "DDR4 chips edged down 0.25% this week. DDR5 holding.",
        },
    ])

    return spot_df, forecasts_df, signals_df


def _main_demo():
    """Run a demo evaluation on toy fixtures and print the verdict."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    spot_df, forecasts_df, signals_df = _build_toy_fixtures()
    as_of = datetime(2026, 5, 19, 12, 0, 0)

    verdict = evaluate_matrix(
        spot_history=spot_df,
        contract_forecasts=forecasts_df,
        contract_signals=signals_df,
        as_of=as_of,
    )

    print("=" * 70)
    print(f"Memory Cycle Matrix Verdict — as of {verdict.as_of}")
    print("=" * 70)
    print()
    print(f"CELL: {verdict.cell_label}")
    print()
    print("Spot state")
    print("-" * 70)
    print(f"  SKU                  : {verdict.spot.sku}")
    print(f"  Date                 : {verdict.spot.as_of_date}")
    print(f"  Session avg          : ${verdict.spot.current_session_avg:.2f}")
    print(f"  Weekly mean (current): ${verdict.spot.weekly_mean_current:.2f}")
    print(f"  Rolling 12w peak     : ${verdict.spot.rolling_peak_price:.2f} "
          f"({verdict.spot.rolling_peak_date})")
    print(f"  Drawdown             : {verdict.spot.drawdown_pct:+.2f}%")
    print(f"  Weeks since peak     : {verdict.spot.weeks_since_peak}w")
    if verdict.spot.wow_change_pct is not None:
        print(f"  WoW change           : {verdict.spot.wow_change_pct:+.2f}%")
    print(f"  -> Spot column {verdict.spot.spot_column}: {verdict.spot.spot_column_label}")
    print()
    print("Contract state")
    print("-" * 70)
    c = verdict.contract
    if c.has_anchor:
        print(f"  Anchor               : {c.anchor_quarter} "
              f"{c.anchor_low_pct:.0f}-{c.anchor_high_pct:.0f}% "
              f"(central {c.anchor_central_pct:.1f}%)")
        print(f"  Anchor source        : {c.anchor_source}")
        print(f"  Anchor age           : {c.anchor_age_days}d "
              f"(published {c.anchor_published_date})")
        print(f"  Adjustments:")
        for adj in c.adjustments:
            print(f"    - {adj.component:11s} {adj.pp:+6.2f}pp "
                  f"(weight {adj.weight:.1f}, {adj.sources_count} src) "
                  f"-- {adj.notes}")
        print(f"  Synthetic central    : {c.synthetic_central_pct:.2f}%")
        print(f"  Synthetic range      : {c.synthetic_low_pct:.1f}% - "
              f"{c.synthetic_high_pct:.1f}%")
    else:
        print("  Anchor               : NONE — defaulting to most conservative band")
    print(f"  -> Contract row {c.contract_row}: {c.contract_row_label}")
    print()
    print("Narrative")
    print("-" * 70)
    print(f"  {verdict.narrative}")
    print()
    if verdict.warnings:
        print("Warnings")
        print("-" * 70)
        for w in verdict.warnings:
            print(f"  ! {w}")
    print()


if __name__ == "__main__":
    _main_demo()
