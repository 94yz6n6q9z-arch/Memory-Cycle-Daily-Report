"""
Chart generation for the daily brief.

Produces a 1-year price chart (baseline 100) for the core memory tickers
and the SOX benchmark. Returned as base64-encoded PNG suitable for
embedding inline in HTML email.

v3.39.2 adds the supercycle phase chart — an overlay of the generic
5-phase memory-supercycle anatomy onto SK Hynix's actual price history,
with three dynamic annotation panels listing fired and pending indicators.
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
