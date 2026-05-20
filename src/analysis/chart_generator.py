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

    Returns: (t1_fired_labels, t2_fired_labels, t23_pending_labels,
              t1_total, t2_total, t3_total, t1_fired_count, t2_fired_count)
    """
    # Build id → short_label map from INDICATORS config
    label_map = {}
    tier_map = {}
    totals = {1: 0, 2: 0, 3: 0, 4: 0}
    for ind in indicators_config:
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
        tier = getattr(a, "tier", 0) or tier_map.get(getattr(a, "id", ""), 0)
        label = label_map.get(
            getattr(a, "id", ""),
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
