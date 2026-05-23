"""
Chart generation for the daily brief.

Produces a 1-year price chart (baseline 100) for the core memory tickers
and the SOX benchmark. Returned as base64-encoded PNG suitable for
embedding inline in HTML email.

v3.39.2 adds the supercycle phase chart — an overlay of the generic
5-phase memory-supercycle anatomy onto SK Hynix's actual price history,
with three dynamic annotation panels listing fired and pending indicators.

CHANGELOG (this file):
  v3.39.7.2 (2026-05-20) — Reverted the synthetic contract line from step() back to plot() in generate_spot_contract_chart. v3.39.7.1's step function looked too rectangular; the user wants direct lines connecting the contract data points. The original v3.39.7 "triangle" complaint was about the fill_between band (the angular polygon shapes) not the plot line itself — with the band gone the straight-line connection is clean. Small white-edged markers added at each point so anchor publish dates + today are identifiable on the line. Subtitle updated.
  v3.39.7.1 (2026-05-20) — Matrix chart cell labels updated from "HOLD"/"WARN"/"FIRE" to "Not fired"/"Partially fired"/"Fired" (with line breaks for fit at 7.5pt font). Internal lookup keys renamed to NOT_FIRED/PARTIAL/FIRED for dict-lookup ergonomics; the cell_display map controls the rendered text. In the dual-axis spot/contract chart, the contract line cleanup: removed the anchor dashed step + fill_between band + scatter dot + arrow annotation (they created angular polygons and a triangle peak at the Feb 2026 anchor jump). Now a single step function for the synthetic estimate with a plain text label at the right edge. Subtitle updated to reflect the new visualization.
  v3.39.7 (2026-05-20) — Two cycle-matrix charts appended at bottom:
    generate_cycle_matrix_chart() renders the 5x5 grid with HOLD/WARN/FIRE
    cell colors and a callout dot at today's (contract_row, spot_col).
    generate_spot_contract_chart() renders a dual-axis time series with
    DDR5 16Gb spot price (left Y, purple) and synthetic contract QoQ
    estimate (right Y, green), including peak and today annotations and
    the anchor step function as a dashed line. render_cycle_charts_from_verdict()
    is the one-call helper that reads the data CSVs and the MatrixVerdict
    from cycle_matrix.py, returning both base64 PNGs. Existing
    generate_performance_chart() and generate_supercycle_chart() unchanged.
"""

from __future__ import annotations

import base64
import io
import logging
from datetime import date, timedelta
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # headless backend; required in CI
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


PALETTE = {
    "SK Hynix": "#534AB7",
    "Micron": "#185FA5",
    "Samsung Electronics": "#0F6E56",
    "MSCI ACWI Semiconductor": "#888780",
    "S&P 500": "#A32D2D",
    "KOSPI": "#854F0B",
    "ASML": "#1F77B4",
    "Applied Materials": "#FF7F0E",
    "NVIDIA": "#2CA02C",
    "Broadcom": "#D62728",
    "TSMC": "#9467BD",
}


def generate_performance_chart(
    history_baseline100: pd.DataFrame,
    title: str = "1Y performance (baseline = 100)",
) -> str:
    """Generate the main performance chart and return as base64 PNG string.

    The returned string can be embedded directly in HTML via:
        <img src="data:image/png;base64,{result}" alt="..." />
    """
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120)

    for col in history_baseline100.columns:
        color = PALETTE.get(col, "#444441")
        # Make benchmark slightly thinner / dashed
        is_benchmark = col in ("MSCI ACWI Semiconductor", "S&P 500", "KOSPI")
        ax.plot(
            history_baseline100.index,
            history_baseline100[col],
            label=col,
            color=color,
            linewidth=1.4 if is_benchmark else 2.2,
            linestyle="--" if is_benchmark else "-",
            alpha=0.85,
        )

    ax.axhline(y=100, color="#CCCCCC", linewidth=0.8, linestyle=":")
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel("Indexed return (100 = 1Y ago)", fontsize=10)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, linestyle=":", alpha=0.4)
    ax.tick_params(axis="both", labelsize=9)

    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=2)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor="white", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
    return encoded


# ============================================================
# v3.39.2 — Supercycle phase chart
# ============================================================
# Overlays the generic 5-phase memory-supercycle anatomy onto SK Hynix's
# actual price history. Three dynamic annotation panels below the chart
# list Tier 1 fired indicators, Tier 2 fired indicators, and the
# T2/T3 watch list (signals still pending that would mark the roll-over).
#
# Phase boundaries are computed by anchoring Phase 4 END to the LLM's
# base_case.peak_window_start, then back-filling earlier phases using
# the generic durations (Phase 1: 9mo, Phase 2: 12mo, Phase 3: 12mo,
# Phase 4: 9mo). Phase 5 (18mo) extends forward from peak. The whole
# timeline is scaled to fit the actual trough → peak span so cycles
# that run 20-30% faster or slower than the generic template still
# show meaningful phase shading.
#
# Projection curves (base/bull/bear) are smooth quadratic interpolations
# from today's price to each scenario's endpoint, NOT literal price
# extrapolations — the intent is visualizing the three plausible paths,
# not predicting them.


_PHASE_ORDER = [
    "Phase 1 — Trough",
    "Phase 2 — Recovery",
    "Phase 3 — Expansion",
    "Phase 4 — Boom",
    "Phase 5 — Decline",
]


def _parse_peak_window_start(text: str, fallback_months_away: float) -> date:
    """Parse strings like 'August 2026' or 'Q3 2026' into a date.

    The exec summary uses a few formats — written month ('August 2026'),
    quarter ('Q3 2026'), or month abbreviation ('Aug 2026'). We try a
    few common patterns and fall back to today + peak_months_away if
    nothing parses. Always returns a sensible date (never None) so the
    chart can render even when the LLM produces a weird format.
    """
    today = date.today()
    if not text or not isinstance(text, str):
        return today + timedelta(days=int(fallback_months_away * 30.44))
    s = text.strip()
    # Try "MonthName YYYY" or "MonthAbbr YYYY"
    for fmt in ("%B %Y", "%b %Y"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    # Try "Q1 2026" / "Q2 2026" / etc. — anchor to mid-quarter.
    s_upper = s.upper().replace(" ", "")
    if len(s_upper) >= 6 and s_upper.startswith("Q") and s_upper[1].isdigit():
        try:
            q = int(s_upper[1])
            yr = int(s_upper[2:6])
            if 1 <= q <= 4 and 2020 <= yr <= 2030:
                # Mid-quarter month: Q1→Feb, Q2→May, Q3→Aug, Q4→Nov
                month = (q - 1) * 3 + 2
                return date(yr, month, 15)
        except (ValueError, IndexError):
            pass
    # Fallback: today + fallback_months_away
    return today + timedelta(days=int(fallback_months_away * 30.44))


def _compute_phase_boundaries(
    trough_date: date,
    peak_date: date,
    phase_durations_months: dict,
) -> dict:
    """Return {phase_label: (start_date, end_date)} for all 5 phases.

    Scales the generic phase durations proportionally so that the sum
    of Phases 1-4 lands exactly at peak_date. Phase 5 extends forward
    from peak using its scaled duration.

    If trough → peak spans LESS than the generic 42 months, all phases
    compress. If MORE, they stretch. Ratio preserved either way.
    """
    trough_to_peak_months = max(
        1.0, (peak_date - trough_date).days / 30.44
    )
    generic_trough_to_peak = sum(
        phase_durations_months[p] for p in _PHASE_ORDER[:4]
    )
    scale = trough_to_peak_months / generic_trough_to_peak

    boundaries = {}
    cursor = trough_date
    for phase in _PHASE_ORDER:
        duration_days = int(phase_durations_months[phase] * scale * 30.44)
        end = cursor + timedelta(days=duration_days)
        boundaries[phase] = (cursor, end)
        cursor = end
    return boundaries


def _quadratic_curve(
    start_date: date,
    start_price: float,
    end_date: date,
    end_price: float,
    n_points: int = 30,
) -> tuple[list[date], list[float]]:
    """Smooth (decelerating) quadratic curve from start to end.

    Used for projection lines. The curve is concave-down (price changes
    fast initially, then plateaus near the endpoint) which mirrors how
    cyclical assets actually behave near peaks — sharp run-up, slowing
    approach to the high. For bear projections (end_price < start_price),
    the same shape applies in reverse: sharp initial decline that
    decelerates toward the floor.

    Returns parallel lists of dates and prices, suitable for ax.plot().
    """
    total_days = (end_date - start_date).days
    if total_days <= 0:
        return [start_date, end_date], [start_price, end_price]
    dates_out = []
    prices_out = []
    for i in range(n_points + 1):
        t = i / n_points
        # Quadratic ease-out: 1 - (1 - t)^2 → decelerating
        eased = 1.0 - (1.0 - t) ** 2
        d_days = int(t * total_days)
        dates_out.append(start_date + timedelta(days=d_days))
        prices_out.append(start_price + (end_price - start_price) * eased)
    return dates_out, prices_out


def _categorize_indicators(
    indicator_assessments: list,
    indicators_config: list,
) -> tuple[list[str], list[str], list[str], int, int, int, int, int]:
    """Bucket assessments into T1/T2 fired and T2+T3 not-yet-fired.

    Joins assessments to INDICATORS by id to get short_label. Falls
    back to indicator name (truncated) if short_label isn't set.

    "Fired" means current_status starts with "Fired" and doesn't
    contain "reversed" (handles the "Fired then reversed" case from
    t3_relative_strength which is currently NOT actually fired).
    "Partially fired" is counted as fired.

    v3.4.7 (2026-05-23) — SECTION_4_ONLY_INDICATORS (DHCR / Competitor /
    ADR) are EXCLUDED from totals AND counts. Per Raul 2026-05-23 these
    are watch-only overlays whose tier placement is unsettled; until
    that's decided, they must not affect the side-panel "Tier 1 fired
    (X/Y)" tally any more than they affect the section 3 row table.

    Returns: (t1_fired_labels, t2_fired_labels, t23_pending_labels,
              t1_total, t2_total, t3_total, t1_fired_count, t2_fired_count)
    """
    from src.config import SECTION_4_ONLY_INDICATORS

    # Build id → short_label map from INDICATORS config
    label_map = {}
    tier_map = {}
    totals = {1: 0, 2: 0, 3: 0, 4: 0}
    for ind in indicators_config:
        if ind.id in SECTION_4_ONLY_INDICATORS:
            # v3.4.7 — DHCR/Competitor/ADR don't contribute to the
            # per-tier denominators in the side-panel chart.
            continue
        label_map[ind.id] = ind.short_label or (ind.name[:25] + "…") if len(ind.name) > 25 else (ind.short_label or ind.name)
        tier_map[ind.id] = ind.tier
        totals[ind.tier] = totals.get(ind.tier, 0) + 1

    def is_fired(status: str) -> bool:
        s = (status or "").lower()
        if "reversed" in s:
            return False
        return s.startswith("fired") or s.startswith("partially fired")

    t1_fired, t2_fired, t23_pending = [], [], []
    t1_fired_count, t2_fired_count = 0, 0
    for a in indicator_assessments:
        id_ = getattr(a, "id", "")
        if id_ in SECTION_4_ONLY_INDICATORS:
            # v3.4.7 — Skip in the firing tallies too. The deep-dive
            # cards in section 4 surface their status independently.
            continue
        tier = getattr(a, "tier", 0) or tier_map.get(id_, 0)
        label = label_map.get(
            id_,
            (getattr(a, "name", "") or "?")[:25],
        )
        if tier == 1 and is_fired(getattr(a, "current_status", "")):
            t1_fired.append(label)
            t1_fired_count += 1
        elif tier == 2 and is_fired(getattr(a, "current_status", "")):
            t2_fired.append(label)
            t2_fired_count += 1
        elif tier in (2, 3) and not is_fired(getattr(a, "current_status", "")):
            t23_pending.append(label)

    return (
        t1_fired, t2_fired, t23_pending,
        totals.get(1, 0), totals.get(2, 0), totals.get(3, 0),
        t1_fired_count, t2_fired_count,
    )


def _draw_annotation_panel(
    ax,
    title: str,
    items: list[str],
    title_color: str,
    bg_color: str,
) -> None:
    """Render one of the three annotation boxes below the main chart."""
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    # Rounded-rect background spanning the panel
    rect = mpatches.FancyBboxPatch(
        (0.02, 0.05), 0.96, 0.90,
        boxstyle="round,pad=0.02,rounding_size=0.04",
        linewidth=0.8, edgecolor="#CCCCCC", facecolor=bg_color,
    )
    ax.add_patch(rect)
    # Title
    ax.text(
        0.06, 0.82, title,
        fontsize=10, fontweight="bold", color=title_color,
        transform=ax.transAxes,
    )
    # Items as bullet lines
    if items:
        y = 0.66
        for item in items[:8]:  # cap at 8 to keep box readable
            ax.text(
                0.08, y, f"• {item}",
                fontsize=8.5, color="#333333",
                transform=ax.transAxes,
            )
            y -= 0.12
        if len(items) > 8:
            ax.text(
                0.08, y, f"… +{len(items) - 8} more",
                fontsize=8, color="#888888", style="italic",
                transform=ax.transAxes,
            )
    else:
        ax.text(
            0.06, 0.45, "(none)",
            fontsize=9, color="#888888", style="italic",
            transform=ax.transAxes,
        )


def generate_supercycle_chart(
    sk_hynix_price_history: pd.Series,
    executive_summary,
    indicator_assessments: list,
    indicators_config: list,
    trough_date_iso: str,
    phase_durations_months: dict,
    phase_colors: dict,
    currency: str = "KRW",
) -> Optional[str]:
    """Render the supercycle phase chart as base64 PNG.

    Args:
        sk_hynix_price_history: pd.Series indexed by date, values = SK Hynix
            close price. Should extend back to (or near) the cycle trough.
        executive_summary: ExecutiveSummary dataclass instance with
            base_case / bull_case / bear_case (each with peak_months_away,
            peak_window_start, return_to_peak_pct).
        indicator_assessments: list[IndicatorAssessment] from the LLM.
        indicators_config: INDICATORS list from config — used to look up
            short_label by id, since IndicatorAssessment doesn't carry it.
        trough_date_iso: cycle trough date as 'YYYY-MM-DD'.
        phase_durations_months: dict mapping phase label → months. From
            CYCLE_PHASE_DURATIONS_MONTHS in config.
        phase_colors: dict mapping phase label → hex color. From
            CYCLE_PHASE_COLORS in config.
        currency: y-axis label currency (default KRW).

    Returns: base64-encoded PNG, or None if input data is unusable.
    """
    if sk_hynix_price_history is None or len(sk_hynix_price_history) < 5:
        logger.warning("Supercycle chart: SK Hynix price history insufficient")
        return None

    series = sk_hynix_price_history.dropna()
    if len(series) < 5:
        logger.warning("Supercycle chart: SK Hynix series too sparse after dropna")
        return None

    # ----- Resolve dates -----
    from datetime import datetime as _dt
    trough_date = _dt.fromisoformat(trough_date_iso).date()
    today = series.index[-1].date() if hasattr(series.index[-1], "date") else date.today()
    current_price = float(series.iloc[-1])

    base_case = getattr(executive_summary, "base_case", None)
    bull_case = getattr(executive_summary, "bull_case", None)
    bear_case = getattr(executive_summary, "bear_case", None)
    if base_case is None:
        logger.warning("Supercycle chart: no base_case in executive_summary")
        return None

    base_peak_date = _parse_peak_window_start(
        getattr(base_case, "peak_window_start", ""),
        getattr(base_case, "peak_months_away", 4),
    )
    # Sanity check: peak must be in the future. If not, shift to today + 1m.
    if base_peak_date <= today:
        base_peak_date = today + timedelta(days=30)

    bull_peak_date = _parse_peak_window_start(
        getattr(bull_case, "peak_window_start", "") if bull_case else "",
        getattr(bull_case, "peak_months_away", 12) if bull_case else 12,
    )
    bear_peak_date = _parse_peak_window_start(
        getattr(bear_case, "peak_window_start", "") if bear_case else "",
        getattr(bear_case, "peak_months_away", 0) if bear_case else 0,
    )

    # Phase boundaries scaled to fit actual trough → base-case-peak span
    phases = _compute_phase_boundaries(
        trough_date, base_peak_date, phase_durations_months
    )

    # Chart x-extent: trough → end of Phase 5
    chart_end_date = phases["Phase 5 — Decline"][1]

    # ----- Figure layout: main chart on top, 3 annotation panels below -----
    # v3.39.2: figsize/dpi tuned for email — at 12×7.5 dpi=120 the PNG
    # was ~197KB base64, which together with the rest of the email pushed
    # past Gmail's ~102KB single-message clip threshold. 11×6.6 dpi=100
    # drops the PNG to ~110KB while preserving readability and the
    # three-panel layout. Tradeoff: minor reduction in line crispness.
    fig = plt.figure(figsize=(11, 6.6), dpi=100)
    gs = fig.add_gridspec(
        2, 3, height_ratios=[3.6, 1.0], hspace=0.22, wspace=0.10,
    )
    ax_main = fig.add_subplot(gs[0, :])
    ax_t1 = fig.add_subplot(gs[1, 0])
    ax_t2 = fig.add_subplot(gs[1, 1])
    ax_pending = fig.add_subplot(gs[1, 2])

    # ----- Phase shading -----
    # Compute y-axis range first (need it for shading extent)
    historical_prices = list(series.values)
    base_end_price = current_price * (1 + getattr(base_case, "return_to_peak_pct", 0) / 100.0)
    bull_end_price = current_price * (1 + (getattr(bull_case, "return_to_peak_pct", 0) if bull_case else 0) / 100.0)
    bear_end_price = current_price * (1 + (getattr(bear_case, "return_to_peak_pct", 0) if bear_case else 0) / 100.0)
    y_min = min(min(historical_prices), bear_end_price * 0.85, 0)
    y_max = max(max(historical_prices), bull_end_price, base_end_price) * 1.08
    if y_min < 0:
        y_min = 0

    for phase_name, (start_d, end_d) in phases.items():
        ax_main.axvspan(
            start_d, end_d,
            ymin=0, ymax=1,
            color=phase_colors.get(phase_name, "#F0F0F0"),
            alpha=0.7, linewidth=0, zorder=0,
        )

    # Phase labels at top of each region
    for phase_name, (start_d, end_d) in phases.items():
        mid_d = start_d + (end_d - start_d) / 2
        # Two-line label: "Phase N" / "<descriptor>"
        parts = phase_name.split(" — ")
        if len(parts) == 2:
            line1, line2 = parts
        else:
            line1, line2 = phase_name, ""
        is_current_phase = start_d <= today <= end_d
        weight = "bold" if is_current_phase else "normal"
        color = "#534AB7" if is_current_phase else "#666666"
        ax_main.text(
            mid_d, y_max * 0.965, line1,
            ha="center", va="top", fontsize=9, fontweight=weight, color=color,
        )
        ax_main.text(
            mid_d, y_max * 0.925, line2,
            ha="center", va="top", fontsize=8.5, fontweight=weight, color=color,
        )
        if is_current_phase:
            ax_main.text(
                mid_d, y_max * 0.885, "(current)",
                ha="center", va="top", fontsize=8, fontweight="bold",
                color="#A04030", style="italic",
            )

    # ----- Historical price line -----
    ax_main.plot(
        [d.date() if hasattr(d, "date") else d for d in series.index],
        series.values,
        color=PALETTE["SK Hynix"],
        linewidth=2.4, zorder=5, label="SK Hynix (historical)",
    )

    # ----- Projection curves (dashed) from today to each scenario peak -----
    # Base
    base_dates, base_prices = _quadratic_curve(today, current_price, base_peak_date, base_end_price)
    ax_main.plot(
        base_dates, base_prices,
        color=PALETTE["SK Hynix"], linewidth=2.0, linestyle="--",
        alpha=0.85, zorder=4,
        label=f"Base case (peak {getattr(base_case,'peak_window_start','?')})",
    )
    ax_main.plot(
        [base_peak_date], [base_end_price],
        marker="o", markersize=9, color=PALETTE["SK Hynix"],
        markeredgecolor="white", markeredgewidth=1.5, zorder=6,
    )
    ax_main.text(
        base_peak_date, base_end_price * 1.03,
        "peak",
        ha="center", va="bottom", fontsize=9, color=PALETTE["SK Hynix"],
        fontweight="bold",
    )

    # Bull
    if bull_case is not None:
        bull_dates, bull_prices = _quadratic_curve(today, current_price, bull_peak_date, bull_end_price)
        ax_main.plot(
            bull_dates, bull_prices,
            color="#1F9D55", linewidth=1.6, linestyle=":", alpha=0.75, zorder=3,
            label=f"Bull case (peak {getattr(bull_case,'peak_window_start','?')})",
        )

    # Bear
    if bear_case is not None:
        # For bear case, if peak_months_away is ~0 we still want a visible curve
        # from current to bear_end_price over a short horizon.
        bear_horizon_date = bear_peak_date if bear_peak_date > today else today + timedelta(days=60)
        bear_dates, bear_prices = _quadratic_curve(today, current_price, bear_horizon_date, bear_end_price)
        ax_main.plot(
            bear_dates, bear_prices,
            color="#A32D2D", linewidth=1.6, linestyle=":", alpha=0.75, zorder=3,
            label=f"Bear case (peak {getattr(bear_case,'peak_window_start','?')})",
        )

    # Current point marker
    ax_main.plot(
        [today], [current_price],
        marker="o", markersize=10, color="#A04030",
        markeredgecolor="white", markeredgewidth=1.8, zorder=7,
    )
    # Current price annotation — offset enough to clear the 10pt marker
    fmt_price = f"{current_price:,.0f} {currency}"
    ax_main.annotate(
        f"{fmt_price}\n({today.strftime('%b %d, %Y')})\ncurrent",
        xy=(today, current_price),
        xytext=(28, 40), textcoords="offset points",
        fontsize=9, color="#A04030", fontweight="bold",
        ha="left", va="bottom",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white",
                  edgecolor="#A04030", linewidth=1.0, alpha=0.95),
        arrowprops=dict(arrowstyle="-", color="#A04030", linewidth=0.9,
                        connectionstyle="arc3,rad=0.0"),
    )

    # ----- Axes formatting -----
    ax_main.set_xlim(trough_date, chart_end_date)
    ax_main.set_ylim(y_min, y_max)
    # Y axis in thousands KRW for readability
    if currency == "KRW":
        ax_main.yaxis.set_major_formatter(plt.FuncFormatter(
            lambda x, _: f"{x / 1000:,.0f}K"
        ))
        ax_main.set_ylabel(f"SK Hynix share price ({currency})", fontsize=10)
    else:
        ax_main.set_ylabel(f"SK Hynix share price ({currency})", fontsize=10)
    ax_main.set_title(
        "Where we stand in the cycle — SK Hynix mapped against the 5-phase supercycle framework",
        fontsize=12, fontweight="bold", loc="left", pad=10,
    )
    ax_main.spines["top"].set_visible(False)
    ax_main.spines["right"].set_visible(False)
    ax_main.grid(True, axis="y", linestyle=":", alpha=0.35)
    ax_main.tick_params(axis="both", labelsize=9)
    ax_main.legend(
        loc="upper left", fontsize=8, frameon=False,
        bbox_to_anchor=(0.005, 0.78),
    )

    # ----- Annotation panels (dynamic from indicator_assessments) -----
    (
        t1_fired, t2_fired, t23_pending,
        t1_total, t2_total, t3_total,
        t1_fired_count, t2_fired_count,
    ) = _categorize_indicators(indicator_assessments, indicators_config)

    _draw_annotation_panel(
        ax_t1,
        title=f"Tier 1 fired ({t1_fired_count}/{t1_total})",
        items=t1_fired,
        title_color="#A04030",
        bg_color="#FDF0EC",
    )
    _draw_annotation_panel(
        ax_t2,
        title=f"Tier 2 fired ({t2_fired_count}/{t2_total})",
        items=t2_fired,
        title_color="#B5760A",
        bg_color="#FBF3E1",
    )
    _draw_annotation_panel(
        ax_pending,
        title=f"Not yet fired (T2/T3 watch — {len(t23_pending)} signals)",
        items=t23_pending,
        title_color="#534AB7",
        bg_color="#F0EEF7",
    )

    # ----- Render -----
    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor="white", bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    buf.seek(0)
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
    return encoded

# v3.39.7 — Cycle matrix charts
# ============================================================================
# Two charts surfaced inside the t1_spot_decline indicator card:
#   1. Cycle matrix dot — 5x5 grid + current-position marker
#   2. Spot price & contract growth dual-axis time series
#
# Both return base64-encoded PNGs ready for inline HTML embedding.
# ============================================================================

from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Union

import numpy as np
from matplotlib.patches import Rectangle, FancyBboxPatch
from matplotlib import patheffects


# Matrix cell color map (matches HOLD/WARN/FIRE semantics)
MATRIX_COLOR_HOLD = "#C0DD97"   # green from existing palette
MATRIX_COLOR_WARN = "#FAC775"   # amber from existing palette
MATRIX_COLOR_FIRE = "#D85A30"   # red from existing palette
MATRIX_DOT_COLOR = "#2c2c2a"    # body text color
MATRIX_DOT_RING = "#FFEBA1"     # change-flag yellow ring around the dot


def generate_cycle_matrix_chart(
    contract_row: int,
    spot_col: int,
    cell_label: str,
    synthetic_central_pct: Optional[float],
    drawdown_pct: float,
    width_in: float = 5.2,
    height_in: float = 4.6,
) -> str:
    """Render the 5x5 cycle matrix with a dot at (contract_row, spot_col).

    Returns a base64-encoded PNG suitable for inline HTML embedding.
    """
    # Matrix lookup (must match cycle_matrix.MATRIX exactly)
    # v3.39.7.1 — display labels updated to match the framework's status
    # vocabulary. Internal keys (NOT_FIRED/PARTIAL/FIRED) kept short for
    # dict-lookup ergonomics; display strings use line breaks for "Partially
    # fired" since "Partially" doesn't fit on a 1.0-unit cell at 8pt.
    matrix_cells = {
        (1, 1): "NOT_FIRED", (1, 2): "NOT_FIRED", (1, 3): "NOT_FIRED", (1, 4): "PARTIAL", (1, 5): "FIRED",
        (2, 1): "NOT_FIRED", (2, 2): "NOT_FIRED", (2, 3): "PARTIAL",   (2, 4): "PARTIAL", (2, 5): "FIRED",
        (3, 1): "PARTIAL",   (3, 2): "PARTIAL",   (3, 3): "PARTIAL",   (3, 4): "FIRED",   (3, 5): "FIRED",
        (4, 1): "FIRED",     (4, 2): "FIRED",     (4, 3): "FIRED",     (4, 4): "FIRED",   (4, 5): "FIRED",
        (5, 1): "FIRED",     (5, 2): "FIRED",     (5, 3): "FIRED",     (5, 4): "FIRED",   (5, 5): "FIRED",
    }
    cell_colors = {
        "NOT_FIRED": MATRIX_COLOR_HOLD,
        "PARTIAL":   MATRIX_COLOR_WARN,
        "FIRED":     MATRIX_COLOR_FIRE,
    }
    cell_display = {
        "NOT_FIRED": "Not\nfired",
        "PARTIAL":   "Partially\nfired",
        "FIRED":     "Fired",
    }

    row_labels = [">=+30%", "+20-30%", "+10-20%", "+5-10%", "<=+5%"]
    col_labels = ["At peak", "<5% off", "5-10% off", "10-20% off", ">=20% off"]

    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=120)

    # Draw cells. Row 1 at top, Row 5 at bottom (y inverted)
    for (r, c), kind in matrix_cells.items():
        x = c - 1
        y = 5 - r  # invert so R1 sits at top
        rect = Rectangle(
            (x, y), 1, 1,
            facecolor=cell_colors[kind],
            edgecolor="white",
            linewidth=2.0,
            alpha=0.78,
        )
        ax.add_patch(rect)
        ax.text(
            x + 0.5, y + 0.5, cell_display[kind],
            ha="center", va="center",
            fontsize=7.5, fontweight="bold",
            color="#2c2c2a",
            linespacing=0.95,
        )

    # Plot the today-dot
    today_x = spot_col - 1 + 0.5
    today_y = 5 - contract_row + 0.5
    ax.scatter(
        [today_x], [today_y],
        s=420, marker="o",
        facecolor=MATRIX_DOT_COLOR,
        edgecolor=MATRIX_DOT_RING,
        linewidth=4.0,
        zorder=10,
    )
    # Today label callout
    ax.annotate(
        f"TODAY\n{cell_label}",
        xy=(today_x, today_y),
        xytext=(today_x + 1.1, today_y + 0.6),
        fontsize=8, fontweight="bold",
        color="#2c2c2a",
        ha="left", va="center",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#FFEBA1",
                  edgecolor="#C68A2A", linewidth=1.2),
        arrowprops=dict(arrowstyle="->", color="#2c2c2a", lw=1.2),
        zorder=11,
    )

    # Axes ticks and labels
    ax.set_xticks([i + 0.5 for i in range(5)])
    ax.set_xticklabels(col_labels, fontsize=8)
    ax.set_yticks([i + 0.5 for i in range(5)])
    ax.set_yticklabels(row_labels[::-1], fontsize=8)  # reverse to align with inverted y

    ax.set_xlabel("Spot drawdown from rolling 12w peak", fontsize=9, fontweight="bold")
    ax.set_ylabel("Contract growth QoQ (synthetic estimate)", fontsize=9, fontweight="bold")

    title_main = "Cycle Matrix — contract growth × spot drawdown"
    subtitle = (
        f"Synthetic contract: {synthetic_central_pct:.1f}% QoQ · "
        f"Spot drawdown: {drawdown_pct:+.2f}%"
        if synthetic_central_pct is not None
        else f"Spot drawdown: {drawdown_pct:+.2f}%"
    )
    ax.set_title(title_main, fontsize=10.5, fontweight="bold", loc="left", pad=32)
    ax.text(0, 5.45, subtitle, fontsize=8.5, color="#5F5E5A",
            transform=ax.transData)

    ax.set_xlim(0, 5)
    ax.set_ylim(0, 5.3)
    ax.set_aspect("equal")

    # Hide spines
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(axis="both", which="both", length=0)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor="white", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def generate_spot_contract_chart(
    spot_history_df: pd.DataFrame,
    contract_synthetic_series: list[tuple[date, float]],
    contract_anchor_series: list[tuple[date, float, float, float]],
    today_spot: Optional[float] = None,
    today_synthetic: Optional[float] = None,
    width_in: float = 7.6,
    height_in: float = 4.6,
) -> str:
    """Render the dual-axis chart: DDR5 spot (left) + contract growth (right).

    Parameters:
      spot_history_df: DataFrame with at least columns 'date' (ISO) and 'session_avg'
                       for SKU DDR5_16Gb_4800_5600.
      contract_synthetic_series: list of (date, central_pct) — weekly synthetic.
      contract_anchor_series: list of (date, low_pct, central_pct, high_pct) —
                       step function showing where anchor jumped on forecast pubs.
      today_spot / today_synthetic: optional override for the rightmost dot label.

    Returns base64-encoded PNG.
    """
    df = spot_history_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    fig, ax_spot = plt.subplots(figsize=(width_in, height_in), dpi=120)

    # ----- Left axis: spot price -----
    ax_spot.plot(
        df["date"], df["session_avg"],
        color="#534AB7",      # SK Hynix purple = our primary signal color
        linewidth=2.4,
        label="DDR5 16Gb 4800/5600 spot (session avg)",
        zorder=3,
    )
    # Fill under for emphasis
    ax_spot.fill_between(
        df["date"], df["session_avg"], alpha=0.10, color="#534AB7", zorder=2,
    )

    # Annotate peak (early Feb point) — find global max in window
    peak_idx = df["session_avg"].idxmax()
    peak_date = df.loc[peak_idx, "date"]
    peak_price = df.loc[peak_idx, "session_avg"]
    ax_spot.scatter([peak_date], [peak_price], s=70, color="#A32D2D",
                    zorder=5, edgecolor="white", linewidth=1.5)
    ax_spot.annotate(
        f"Peak ${peak_price:.2f}",
        xy=(peak_date, peak_price),
        xytext=(15, 12), textcoords="offset points",
        fontsize=8, color="#A32D2D", fontweight="bold",
        arrowprops=dict(arrowstyle="-", color="#A32D2D", lw=0.8),
    )

    # Today marker
    if today_spot is not None:
        today_date = df["date"].iloc[-1]
        ax_spot.scatter([today_date], [today_spot], s=80, color="#2c2c2a",
                        zorder=6, edgecolor="#FFEBA1", linewidth=2.5)
        ax_spot.annotate(
            f"Today ${today_spot:.2f}",
            xy=(today_date, today_spot),
            xytext=(-65, -25), textcoords="offset points",
            fontsize=8, color="#2c2c2a", fontweight="bold",
            arrowprops=dict(arrowstyle="-", color="#2c2c2a", lw=0.8),
        )

    ax_spot.set_ylabel("DDR5 16Gb spot (USD per chip)", fontsize=9,
                       fontweight="bold", color="#534AB7")
    ax_spot.tick_params(axis="y", labelcolor="#534AB7", labelsize=8)
    ax_spot.tick_params(axis="x", labelsize=8)
    ax_spot.spines["top"].set_visible(False)
    ax_spot.spines["right"].set_color("#0F6E56")
    ax_spot.spines["left"].set_color("#534AB7")
    ax_spot.grid(True, linestyle=":", alpha=0.35)

    # ----- Right axis: contract growth -----
    # v3.39.7.1 — contract line cleanup. Previously this axis had three visual
    # elements competing for attention: (1) anchor dashed step, (2) anchor
    # fill_between band, (3) synthetic continuous line drawn with plot(). The
    # fill band created angular polygon shapes, and v3.39.7.1 then converted
    # the synthetic to a step function. User feedback (v3.39.7.2): the step
    # function looked too rectangular; switch back to direct lines connecting
    # the contract points. The "triangle" artifact in the original v3.39.7
    # render was the fill_between band, not the plot line itself — with the
    # band gone the straight-line connection is clean.
    #
    # Now: ONE plot() line for the synthetic estimate connecting the data
    # points directly. Visible markers at each point so the underlying anchor
    # publish dates + today's value are identifiable on the line. No fill,
    # no arrow, no end-cap callout — just the line and small markers.
    ax_contract = ax_spot.twinx()

    if contract_synthetic_series:
        s_dates = [d for d, _ in contract_synthetic_series]
        s_central = [v for _, v in contract_synthetic_series]
        ax_contract.plot(
            s_dates, s_central,
            color="#0F6E56", linewidth=2.2,
            marker="o", markersize=5,
            markerfacecolor="#0F6E56", markeredgecolor="white",
            markeredgewidth=1.2,
            label="Synthetic contract QoQ estimate",
            zorder=3,
        )

    # Today's contract value as a simple text label at the right edge
    # (no arrow, no extra geometry).
    if today_synthetic is not None and contract_synthetic_series:
        last_date = contract_synthetic_series[-1][0]
        ax_contract.text(
            last_date, today_synthetic,
            f"  {today_synthetic:.1f}% QoQ",
            fontsize=8, color="#0F6E56", fontweight="bold",
            ha="left", va="center",
        )

    ax_contract.set_ylabel("Contract growth QoQ (%)", fontsize=9,
                           fontweight="bold", color="#0F6E56")
    ax_contract.tick_params(axis="y", labelcolor="#0F6E56", labelsize=8)
    ax_contract.spines["top"].set_visible(False)
    ax_contract.spines["left"].set_color("#534AB7")
    ax_contract.spines["right"].set_color("#0F6E56")

    # Reasonable y-range for contract axis
    if contract_synthetic_series:
        all_c = [v for _, v in contract_synthetic_series]
        c_min = min(0, min(all_c) - 10)
        c_max = max(all_c) + 15
        ax_contract.set_ylim(c_min, c_max)

    # Title
    title = "DDR5 16Gb spot price & synthetic contract QoQ estimate"
    subtitle = "Two inputs to the cycle matrix; spot peaked early Feb 2026, contract synthetic updated at each forecast revision"
    ax_spot.set_title(title, fontsize=10.5, fontweight="bold", loc="left", pad=22)
    ax_spot.text(0, 1.02, subtitle, fontsize=8.5, color="#5F5E5A",
                 transform=ax_spot.transAxes)

    # Build combined legend at the bottom
    lines_spot, labels_spot = ax_spot.get_legend_handles_labels()
    lines_contract, labels_contract = ax_contract.get_legend_handles_labels()
    ax_spot.legend(
        lines_spot + lines_contract,
        labels_spot + labels_contract,
        loc="upper left", fontsize=7.5, frameon=False, ncol=1,
        bbox_to_anchor=(0, 0.95),
    )

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor="white", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def render_cycle_charts_from_verdict(
    verdict,
    spot_history_csv: Union[Path, str],
    contract_forecasts_csv: Union[Path, str],
) -> tuple[str, str]:
    """High-level helper: takes a MatrixVerdict + the two CSVs, returns
    (matrix_chart_b64, spot_contract_chart_b64). Used by main.py.
    """
    # Matrix dot chart
    matrix_b64 = generate_cycle_matrix_chart(
        contract_row=verdict.contract_row,
        spot_col=verdict.spot_col,
        cell_label=verdict.cell_label,
        synthetic_central_pct=verdict.contract.synthetic_central_pct,
        drawdown_pct=verdict.spot.drawdown_pct,
    )

    # Spot+contract chart
    spot_df = pd.read_csv(spot_history_csv)
    spot_df = spot_df[spot_df["sku"] == verdict.spot.sku].copy()

    # Contract forecast anchor series — load published forecasts (non-superseded)
    fc_df = pd.read_csv(contract_forecasts_csv)
    fc_df["published_date"] = pd.to_datetime(fc_df["published_date"]).dt.date
    fc_df = fc_df.sort_values("published_date")
    # Filter to aggregate-segment for the anchor (cleanest signal)
    fc_anc = fc_df[fc_df["segment"].isin(["conventional_dram_aggregate", "server_ddr5"])]
    # Build series as (date, low, central, high)
    anchor_series = [
        (row["published_date"], float(row["low_pct"]),
         float(row["central_pct"]) if pd.notna(row["central_pct"])
                                   else (row["low_pct"] + row["high_pct"]) / 2.0,
         float(row["high_pct"]))
        for _, row in fc_anc.iterrows()
    ]

    # Synthetic series: for now we have today's verdict point and historical anchors
    # The synthetic blends anchor + tone + supplier + spot_lead. Without weekly
    # historical signals, approximate the synthetic as the active anchor central
    # for past dates plus today's actual synthetic value.
    synthetic_points = []
    if anchor_series:
        for i, (d, _, central, _) in enumerate(anchor_series):
            synthetic_points.append((d, central))
    if verdict.contract.synthetic_central_pct is not None:
        synthetic_points.append(
            (verdict.as_of.date(), verdict.contract.synthetic_central_pct)
        )

    spot_contract_b64 = generate_spot_contract_chart(
        spot_history_df=spot_df,
        contract_synthetic_series=synthetic_points,
        contract_anchor_series=anchor_series,
        today_spot=verdict.spot.current_session_avg,
        today_synthetic=verdict.contract.synthetic_central_pct,
    )

    return matrix_b64, spot_contract_b64


# ============================================================
# v3.4.0 — DHCR (Decode HBM Captive Ratio) trajectory chart
# ============================================================

def generate_dhcr_trajectory_chart(trajectory: list[dict]) -> Optional[str]:
    """v3.4.0: Render the DHCR trajectory as a dual-axis chart.

    Top panel: capacity-creation rate (left axis, %) vs enterprise AI
    YoY growth (right axis, %), with the delta shaded.

    Bottom panel: implied context capacity per accelerator over time
    (log scale, since it expands from ~250K to ~42M tokens).

    Args:
        trajectory: list of quarterly snapshot dicts (from
                    compute_dhcr_trajectory). Each entry has fields:
                    as_of_date, frontier_kv_kb_per_token,
                    frontier_hbm_gb_per_accelerator,
                    implied_context_capacity_tokens,
                    enterprise_ai_yoy_growth_pct,
                    nvidia_datacenter_yoy_growth_pct,
                    capacity_creation_yoy_pct,
                    direction.

    Returns base64-encoded PNG, or None on failure / no data.
    """
    if not trajectory or len(trajectory) < 2:
        logger.info("DHCR trajectory has insufficient data — skipping chart")
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import numpy as np
        from datetime import datetime as _dt
    except ImportError as e:
        logger.warning(f"matplotlib not available — skipping DHCR chart: {e}")
        return None

    try:
        dates = []
        cap_creation = []
        ent_yoy = []
        nv_yoy = []
        capacity_tokens = []
        kv_kb = []
        for t in trajectory:
            try:
                d = _dt.fromisoformat(t["as_of_date"]).date()
            except (ValueError, KeyError):
                continue
            dates.append(d)
            cap_creation.append(t.get("capacity_creation_yoy_pct"))
            ent_yoy.append(t.get("enterprise_ai_yoy_growth_pct"))
            nv_yoy.append(t.get("nvidia_datacenter_yoy_growth_pct"))
            capacity_tokens.append(t.get("implied_context_capacity_tokens", 0))
            kv_kb.append(t.get("frontier_kv_kb_per_token", 0))

        if not dates:
            return None

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                                        gridspec_kw={"height_ratios": [3, 2]})
        fig.suptitle(
            "Decode HBM Captive Ratio — supply-side capacity creation vs enterprise demand-pull",
            fontsize=13, fontweight="bold", y=0.97,
        )

        # ----- Top panel: capacity creation vs demand growth -----
        # Demand-pull aggregate (enterprise + NVIDIA datacenter blend, 60/40)
        demand_blend = []
        for e, n in zip(ent_yoy, nv_yoy):
            if e is not None and n is not None:
                demand_blend.append(0.6 * e + 0.4 * n)
            elif e is not None:
                demand_blend.append(e)
            elif n is not None:
                demand_blend.append(n)
            else:
                demand_blend.append(None)

        # Filter pairs where both are non-None for shading
        valid_pairs = [(d, c, dem) for d, c, dem in zip(dates, cap_creation, demand_blend)
                       if c is not None and dem is not None]
        if valid_pairs:
            v_dates, v_cap, v_dem = zip(*valid_pairs)
            ax1.fill_between(
                v_dates, v_cap, v_dem,
                where=[d > c for c, d in zip(v_cap, v_dem)],
                color="#2C7A2C", alpha=0.15,
                label="Demand outpacing capacity (bullish HBM)",
            )
            ax1.fill_between(
                v_dates, v_cap, v_dem,
                where=[d <= c for c, d in zip(v_cap, v_dem)],
                color="#B91C1C", alpha=0.15,
                label="Capacity outpacing demand (bearish HBM)",
            )

        # Capacity creation line
        cap_dates = [d for d, c in zip(dates, cap_creation) if c is not None]
        cap_vals = [c for c in cap_creation if c is not None]
        if cap_dates:
            ax1.plot(cap_dates, cap_vals, marker="o", color="#888888",
                     linewidth=2, label="Capacity creation YoY (HBM/accel + KV efficiency)")

        # Enterprise YoY line
        ent_dates = [d for d, e in zip(dates, ent_yoy) if e is not None]
        ent_vals = [e for e in ent_yoy if e is not None]
        if ent_dates:
            ax1.plot(ent_dates, ent_vals, marker="s", color="#0F6E56",
                     linewidth=2, label="Enterprise AI revenue YoY")

        # NVIDIA datacenter line
        nv_dates = [d for d, n in zip(dates, nv_yoy) if n is not None]
        nv_vals = [n for n in nv_yoy if n is not None]
        if nv_dates:
            ax1.plot(nv_dates, nv_vals, marker="^", color="#76B900",
                     linewidth=2, linestyle="--",
                     label="NVIDIA datacenter YoY")

        ax1.axhline(y=0, color="#333", linewidth=0.5)
        ax1.set_ylabel("YoY growth rate (%)", fontsize=11)
        ax1.grid(True, alpha=0.3)
        ax1.legend(loc="upper right", fontsize=9, framealpha=0.9)
        ax1.set_title(
            "Top: supply-side capacity-creation rate vs demand-pull (enterprise revenue growth)",
            fontsize=10, pad=6,
        )

        # ----- Bottom panel: implied context capacity (log scale) -----
        cap_dates_b = [d for d, c in zip(dates, capacity_tokens) if c and c > 0]
        cap_vals_b = [c for c in capacity_tokens if c and c > 0]
        if cap_dates_b:
            ax2.semilogy(cap_dates_b, cap_vals_b, marker="o", color="#2C5AA0",
                         linewidth=2.5, label="Implied context capacity per accelerator")
            ax2.fill_between(cap_dates_b, 1, cap_vals_b, alpha=0.12, color="#2C5AA0")

        # Annotate the start and end points
        if cap_dates_b and cap_vals_b:
            first_d, first_v = cap_dates_b[0], cap_vals_b[0]
            last_d, last_v = cap_dates_b[-1], cap_vals_b[-1]
            ax2.annotate(
                f"{first_v/1000:.0f}K tokens" if first_v < 1_000_000
                else f"{first_v/1_000_000:.1f}M tokens",
                xy=(first_d, first_v), xytext=(8, 8),
                textcoords="offset points", fontsize=9,
                bbox=dict(boxstyle="round,pad=0.3", fc="#FFFFE0", ec="#888"),
            )
            ax2.annotate(
                f"{last_v/1_000_000:.1f}M tokens",
                xy=(last_d, last_v), xytext=(8, -18),
                textcoords="offset points", fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc="#E0F0E0", ec="#2C7A2C"),
            )

        ax2.set_ylabel("Tokens (log scale)", fontsize=11)
        ax2.set_xlabel("Date", fontsize=11)
        ax2.grid(True, alpha=0.3, which="both")
        ax2.set_title(
            "Bottom: implied context capacity per accelerator (KV efficiency × HBM-per-chip)",
            fontsize=10, pad=6,
        )

        # X-axis formatting
        ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha="right")

        plt.tight_layout()
        plt.subplots_adjust(top=0.93)

        # Encode to base64
        import io
        import base64
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")

    except Exception as e:
        logger.exception(f"DHCR trajectory chart rendering failed: {e}")
        return None


# ============================================================
# v3.4.2 — DHCR matrix chart (5x5 memory-zone visualization)
# ============================================================

def generate_dhcr_matrix_chart(snapshot: dict, trajectory: Optional[list[dict]] = None) -> Optional[str]:
    """v3.4.2: Render the DHCR 5x5 matrix with memory-zone coloring.

    Visualization:
      - 5x5 grid colored by memory zone (DDR green, LPDDR yellow,
        HBM_HYBRID amber, HBM red-orange)
      - Today's workload-implied cell: black outlined circle
      - Today's market-deployed cell: filled colored circle with white border
      - Connecting line between the two cells = the anticipation gap
      - Optional trajectory arrow from 90-day-prior market-deployed cell
        to today's market-deployed cell
      - Axis labels: rows = KV efficiency bands; cols = context length bands
      - Cell labels (compact) inside each cell
      - Title + score badge + status badge

    Args:
        snapshot: DHCRSnapshot dict (from asdict(snapshot))
        trajectory: optional list of prior DHCR snapshots for trajectory arrow

    Returns base64-encoded PNG, or None on failure.
    """
    if not snapshot:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyArrowPatch, Circle
    except ImportError as e:
        logger.warning(f"matplotlib not available for DHCR matrix: {e}")
        return None

    # Zone label per cell — must match dhcr.py MEMORY_ZONES exactly
    MEMORY_ZONES = [
        ["DDR",     "DDR/LPDDR", "LPDDR",    "LPDDR/HBM",  "LPDDR/HBM"],
        ["DDR/CXL", "DDR/LPDDR", "LPDDR",    "LPDDR/HBM",  "HBM/hetero"],
        ["LPDDR",   "LPDDR/HBM", "HBM/GDDR", "HBM/hetero", "HBM"],
        ["HBM/GDDR","HBM/GDDR",  "HBM",      "HBM",        "HBM"],
        ["HBM",     "HBM",       "HBM",      "HBM",        "HBM"],
    ]
    # Color per zone category (intuition: green=commodity-safe, red=HBM-dominant)
    ZONE_COLORS = {
        "DDR":         "#A6D89B",  # green — commodity safe zone
        "DDR_CXL":     "#C9E5BC",  # lighter green
        "LPDDR":       "#FCE38A",  # yellow — heterogeneous transition
        "HBM_HYBRID":  "#F8A55C",  # amber — HBM-leaning hybrid
        "HBM":         "#E0775F",  # red-orange — HBM-dominant
    }
    def _zone_cat(label: str) -> str:
        z = label.upper()
        if z == "HBM": return "HBM"
        if "HBM" in z: return "HBM_HYBRID"
        if "LPDDR" in z: return "LPDDR"
        if "CXL" in z or "DDR/" in z: return "DDR_CXL"
        return "DDR"

    fig, ax = plt.subplots(figsize=(11, 9))

    # Y-axis labels (rows): R1 (most efficient) at TOP visually
    # Note: matplotlib origin is bottom-left, so we flip row order for display
    y_labels = ["R1 <1 GB/1M tok", "R2 1-4 GB/1M", "R3 4-16 GB/1M",
                "R4 16-32 GB/1M", "R5 >32 GB/1M tok"]
    x_labels = ["C1 <32K", "C2 32-200K", "C3 200K-1M", "C4 1M-10M", "C5 >10M"]

    # Draw grid cells. Display: row 0 (R1) at top
    for r in range(5):
        for c in range(5):
            # Display row position: r=0 → top row (y=4); r=4 → bottom (y=0)
            display_y = 4 - r
            zone_label = MEMORY_ZONES[r][c]
            zone_cat = _zone_cat(zone_label)
            color = ZONE_COLORS[zone_cat]
            rect = plt.Rectangle((c, display_y), 1, 1, facecolor=color,
                                 edgecolor="#333", linewidth=0.8)
            ax.add_patch(rect)
            # Cell label centered
            ax.text(c + 0.5, display_y + 0.6, zone_label,
                    ha="center", va="center", fontsize=9,
                    color="#222", fontweight="bold")

    # Today's cells
    workload_row, workload_col = snapshot.get("workload_implied_cell", (0, 0))
    market_row, market_col = snapshot.get("market_deployed_cell", (0, 0))
    workload_display_y = 4 - workload_row
    market_display_y = 4 - market_row

    # Workload-implied: black outlined circle (no fill)
    wl_circle = Circle((workload_col + 0.25, workload_display_y + 0.25),
                       radius=0.16, fill=False, edgecolor="#1a1a1a", linewidth=2.5)
    ax.add_patch(wl_circle)
    ax.text(workload_col + 0.25, workload_display_y + 0.08, "workload",
            ha="center", va="top", fontsize=7, color="#1a1a1a", fontweight="bold")

    # Market-deployed: filled red circle with white outline
    md_circle = Circle((market_col + 0.75, market_display_y + 0.25),
                       radius=0.16, facecolor="#B91C1C", edgecolor="#FFFFFF", linewidth=2.5)
    ax.add_patch(md_circle)
    ax.text(market_col + 0.75, market_display_y + 0.08, "market-deployed",
            ha="center", va="top", fontsize=7, color="#1a1a1a", fontweight="bold")

    # Anticipation gap line between workload and market-deployed
    if (workload_row, workload_col) != (market_row, market_col):
        ax.annotate(
            "", xy=(market_col + 0.75, market_display_y + 0.25),
            xytext=(workload_col + 0.25, workload_display_y + 0.25),
            arrowprops=dict(arrowstyle="->", color="#666", lw=1.5, linestyle="dashed"),
        )

    # Trajectory arrow from 90d-ago market-deployed to today's
    if trajectory and len(trajectory) >= 2:
        prior = trajectory[0]  # oldest = ~90d ago
        prior_md = prior.get("market_deployed_cell")
        if prior_md and tuple(prior_md) != (market_row, market_col):
            prior_y = 4 - prior_md[0]
            prior_x = prior_md[1]
            arrow = FancyArrowPatch(
                (prior_x + 0.75, prior_y + 0.75),
                (market_col + 0.75, market_display_y + 0.75),
                arrowstyle="-|>", mutation_scale=20,
                color="#2C5AA0", linewidth=2.0, linestyle="solid",
                alpha=0.85,
            )
            ax.add_patch(arrow)
            ax.text(prior_x + 0.75, prior_y + 0.92, "90d ago",
                    ha="center", va="bottom", fontsize=7,
                    color="#2C5AA0", fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", fc="#FFFFFF", ec="#2C5AA0"))

    # Axis setup
    ax.set_xlim(0, 5)
    ax.set_ylim(0, 5)
    ax.set_xticks([i + 0.5 for i in range(5)])
    ax.set_xticklabels(x_labels, fontsize=9)
    ax.set_yticks([i + 0.5 for i in range(5)])
    ax.set_yticklabels(reversed(y_labels), fontsize=9)
    ax.set_xlabel("Operative context length (volume-weighted) →  longer context",
                  fontsize=11, fontweight="bold")
    ax.set_ylabel("KV efficiency  ←  less efficient (more HBM-intensive)",
                  fontsize=11, fontweight="bold")
    ax.set_aspect("equal")

    # Title with score
    score = snapshot.get("position_score", 0)
    status = snapshot.get("status", "?")
    op_ctx = snapshot.get("operative_context_tokens", 0)
    op_ctx_s = f"{op_ctx/1000:.0f}K" if op_ctx < 1_000_000 else f"{op_ctx/1_000_000:.2f}M"
    kv_gb = snapshot.get("frontier_kv_gb_per_1m_tokens", 0)
    title = (
        f"DHCR Matrix — score {score}/100, status: {status}\n"
        f"Today: KV {kv_gb:.1f} GB/1M tok × {op_ctx_s} operative context"
    )
    ax.set_title(title, fontsize=12, fontweight="bold", pad=14)

    # Color legend at top
    legend_zones = [("HBM dominance", "#E0775F"), ("HBM hybrid", "#F8A55C"),
                    ("LPDDR / hetero", "#FCE38A"), ("DDR/CXL warm", "#C9E5BC"),
                    ("DDR commodity", "#A6D89B")]
    legend_handles = [plt.Rectangle((0, 0), 1, 1, facecolor=c, edgecolor="#333")
                      for _, c in legend_zones]
    legend_labels = [n for n, _ in legend_zones]
    ax.legend(legend_handles, legend_labels, loc="upper center",
              bbox_to_anchor=(0.5, -0.10), ncol=5, fontsize=9,
              frameon=False)

    plt.tight_layout()

    import io, base64
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")
