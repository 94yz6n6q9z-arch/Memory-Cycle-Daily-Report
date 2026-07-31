"""v3.4.6.2 calendar-alignment tests — v3.43.0 update: the SOX benchmark and
DRAM-ETF-vs-SMH sub-trigger are retired; the alignment logic (the asset this
file protects) lives in get_relative_strength_vs_benchmark with the S&P 500
as the signal benchmark. Test intent preserved: KRX/US calendar misalignment
at the tail and mid-window must still produce numbers, not None/NaN.
"""

import unittest

import numpy as np
import pandas as pd

from src.data.prices import (
    evaluate_relative_strength_signal,
    get_relative_strength_vs_benchmark,
)


def _make_history(n_days: int = 45, seed: int = 0) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-05-22", periods=n_days)
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "SK Hynix": 250000 + np.cumsum(rng.standard_normal(n_days) * 1000),
        "Micron": 100 + np.cumsum(rng.standard_normal(n_days) * 0.5),
        "S&P 500": 5800 + np.cumsum(rng.standard_normal(n_days) * 20),
    }, index=dates)


class TestCalendarAlignmentTailNaN(unittest.TestCase):
    """The dominant production case: latest bar is KRX-only — US columns NaN."""

    def test_skhx_5d_computes_when_last_us_bar_missing(self):
        df = _make_history()
        df.loc[df.index[-1], ["Micron", "S&P 500"]] = np.nan
        result = get_relative_strength_vs_benchmark("SK Hynix", df, days=5)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, float)
        self.assertFalse(np.isnan(result))

    def test_micron_5d_computes_when_last_us_bar_missing(self):
        df = _make_history()
        df.loc[df.index[-1], ["Micron", "S&P 500"]] = np.nan
        result = get_relative_strength_vs_benchmark("Micron", df, days=5)
        self.assertIsNotNone(result)
        self.assertFalse(np.isnan(result))


class TestCalendarAlignmentMidWindow(unittest.TestCase):
    """A US holiday inside the 5d window — tail aligned but middle has gaps."""

    def test_us_holiday_in_window_still_computes(self):
        df = _make_history()
        df.loc[df.index[-3], ["Micron", "S&P 500"]] = np.nan
        result = get_relative_strength_vs_benchmark("Micron", df, days=5)
        self.assertIsNotNone(result)


class TestAlignedFrameControl(unittest.TestCase):
    def test_clean_history_produces_finite_result(self):
        result = get_relative_strength_vs_benchmark("SK Hynix", _make_history(), days=5)
        self.assertIsNotNone(result)
        self.assertFalse(np.isnan(result))


class TestInsufficientAlignedRows(unittest.TestCase):
    def test_returns_none_when_aligned_subset_too_small(self):
        df = _make_history(n_days=20)
        df.loc[df.index[:-3], "S&P 500"] = np.nan
        self.assertIsNone(get_relative_strength_vs_benchmark("SK Hynix", df, days=5))


class TestEvaluateVerdictAfterFix(unittest.TestCase):
    def test_verdict_has_real_numbers_with_tail_us_nan(self, tmp_path=None):
        import tempfile, os
        df = _make_history()
        df.loc[df.index[-1], ["Micron", "S&P 500"]] = np.nan
        with tempfile.TemporaryDirectory() as d:
            verdict = evaluate_relative_strength_signal(
                df, state_path=os.path.join(d, "rs.json"))
        self.assertIsNotNone(verdict["skhx_5d_vs_spx_pp"])
        self.assertIsNotNone(verdict["mu_5d_vs_spx_pp"])
        self.assertIsInstance(verdict["skhx_5d_vs_spx_pp"], float)
        self.assertFalse(np.isnan(verdict["skhx_5d_vs_spx_pp"]))


if __name__ == "__main__":
    unittest.main()
