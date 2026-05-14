"""
Chart generation for the daily brief.

Produces a 1-year price chart (baseline 100) for the core memory tickers
and the SOX benchmark. Returned as base64-encoded PNG suitable for
embedding inline in HTML email.
"""

from __future__ import annotations

import base64
import io
import logging

import matplotlib

matplotlib.use("Agg")  # headless backend; required in CI
import matplotlib.pyplot as plt
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
