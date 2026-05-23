"""v3.4.6.2 — Calendar-alignment fix for t3_relative_strength.

The bug: when yfinance batches KRX (SK Hynix, Samsung) and US-listed
tickers (^SOX, MU, DRAM, SMH), the resulting DataFrame index spans both
calendars. KRX-only trading days have NaN in US columns and vice versa.
The pre-v3.4.6.2 code did iloc[-1]/iloc[-days-1] against this misaligned
frame — the cells were NaN often enough that the SOX-relative metric
rendered as "(not computed)" on most run days (confirmed in May 20
and May 22 production briefs).

The fix: project each function's relevant column pair to rows where BOTH
are non-NaN before the iloc lookup. Tests below cover:
- Calendar misalignment at the tail (US bar not yet landed when KRX
  bar already has) — most common production case
- Calendar misalignment in the middle of the lookback window
- Aligned frame (control: should produce same result as pre-fix)
- Insufficient aligned rows (<days+1) — returns None
"""

import unittest

import numpy as np
import pandas as pd

from src.data.prices import (
    evaluate_relative_strength_signal,
    get_dram_etf_vs_smh,
    get_relative_strength_vs_sox,
)


def _make_history(n_days: int = 30, seed: int = 0) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-05-22", periods=n_days)
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "SK Hynix": 250000 + np.cumsum(rng.standard_normal(n_days) * 1000),
        "Micron": 100 + np.cumsum(rng.standard_normal(n_days) * 0.5),
        "MSCI ACWI Semiconductor": 5800 + np.cumsum(rng.standard_normal(n_days) * 20),
        "Roundhill DRAM ETF": 35 + np.cumsum(rng.standard_normal(n_days) * 0.2),
        "VanEck Semi ETF (SMH)": 220 + np.cumsum(rng.standard_normal(n_days) * 1.0),
    }, index=dates)


class TestCalendarAlignmentTailNaN(unittest.TestCase):
    """The dominant production case: latest bar is KRX-only — US columns NaN."""

    def test_skhx_5d_vs_sox_computes_when_last_us_bar_missing(self):
        df = _make_history()
        df.loc[df.index[-1], ["Micron", "MSCI ACWI Semiconductor",
                              "Roundhill DRAM ETF", "VanEck Semi ETF (SMH)"]] = np.nan
        result = get_relative_strength_vs_sox("SK Hynix", df, days=5)
        self.assertIsNotNone(result, "Pre-fix returned None here (bug); post-fix should compute")
        self.assertIsInstance(result, float)
        self.assertFalse(np.isnan(result))

    def test_micron_5d_vs_sox_computes_when_last_us_bar_missing(self):
        df = _make_history()
        df.loc[df.index[-1], ["Micron", "MSCI ACWI Semiconductor"]] = np.nan
        # SK Hynix has a value at -1 but the SOX column doesn't — pair-aligned
        # subset should still find 5 prior valid bars for Micron + SOX.
        result = get_relative_strength_vs_sox("Micron", df, days=5)
        self.assertIsNotNone(result)
        self.assertFalse(np.isnan(result))

    def test_dram_etf_vs_smh_computes_when_last_us_bar_missing(self):
        df = _make_history()
        df.loc[df.index[-1], ["Roundhill DRAM ETF", "VanEck Semi ETF (SMH)"]] = np.nan
        result = get_dram_etf_vs_smh(df, days=5)
        self.assertIsNotNone(result, "Pre-fix returned a NaN-containing dict; post-fix returns None or real numbers")
        self.assertFalse(np.isnan(result["spread_pp"]))


class TestCalendarAlignmentMidWindow(unittest.TestCase):
    """A US holiday inside the 5d window — tail aligned but middle has gaps."""

    def test_us_holiday_in_window_still_computes(self):
        df = _make_history()
        # Simulate US holiday (Memorial Day, etc.) inside the last 5 trading days
        holiday_idx = df.index[-3]
        df.loc[holiday_idx, ["Micron", "MSCI ACWI Semiconductor",
                             "Roundhill DRAM ETF", "VanEck Semi ETF (SMH)"]] = np.nan
        # The aligned subset has one fewer row but should still have ≥6
        result = get_relative_strength_vs_sox("Micron", df, days=5)
        self.assertIsNotNone(result)
        dram = get_dram_etf_vs_smh(df, days=5)
        self.assertIsNotNone(dram)
        self.assertFalse(np.isnan(dram["spread_pp"]))


class TestAlignedFrameControl(unittest.TestCase):
    """Sanity: when no NaN gaps exist, the fix doesn't break the simple case."""

    def test_clean_history_produces_finite_result(self):
        df = _make_history()
        result = get_relative_strength_vs_sox("SK Hynix", df, days=5)
        self.assertIsNotNone(result)
        self.assertFalse(np.isnan(result))


class TestInsufficientAlignedRows(unittest.TestCase):
    """When too many bars are NaN, return None gracefully (don't crash)."""

    def test_returns_none_when_aligned_subset_too_small(self):
        df = _make_history(n_days=20)
        # Wipe most US bars — only 3 aligned rows remain
        bad_idx = df.index[:-3]
        df.loc[bad_idx, "MSCI ACWI Semiconductor"] = np.nan
        result = get_relative_strength_vs_sox("SK Hynix", df, days=5)
        self.assertIsNone(result)

    def test_dram_returns_none_when_aligned_too_small(self):
        df = _make_history(n_days=20)
        bad_idx = df.index[:-3]
        df.loc[bad_idx, "Roundhill DRAM ETF"] = np.nan
        result = get_dram_etf_vs_smh(df, days=5)
        self.assertIsNone(result)


class TestEvaluateVerdictAfterFix(unittest.TestCase):
    """End-to-end: evaluate_relative_strength_signal returns real numbers
    in the calendar-misaligned case the production brief was hitting."""

    def test_verdict_has_real_numbers_with_tail_us_nan(self):
        df = _make_history()
        df.loc[df.index[-1], ["Micron", "MSCI ACWI Semiconductor",
                              "Roundhill DRAM ETF", "VanEck Semi ETF (SMH)"]] = np.nan
        verdict = evaluate_relative_strength_signal(df)
        # The two key fields that were rendering as None / NaN in production
        self.assertIsNotNone(verdict["skhx_5d_vs_sox_pp"])
        self.assertIsNotNone(verdict["mu_5d_vs_sox_pp"])
        self.assertIsInstance(verdict["skhx_5d_vs_sox_pp"], float)
        self.assertFalse(np.isnan(verdict["skhx_5d_vs_sox_pp"]))
        # DRAM ETF block should also have real numbers, not NaN
        if verdict["dram_etf_vs_smh"]:
            self.assertFalse(np.isnan(verdict["dram_etf_vs_smh"]["spread_pp"]))


if __name__ == "__main__":
    unittest.main()
