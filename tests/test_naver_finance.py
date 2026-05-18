"""
Unit tests for src.data.naver_finance (v3.39.3 WiseReport rewrite).

Run with:  python -m tests.test_naver_finance

These tests run entirely offline against an embedded HTML fixture
modeled on the real WiseReport response structure. No network calls;
designed so they keep passing even when WiseReport's site is down.

Coverage:
  - Helper parsers: KRW int, percent, WiseReport YY/MM/DD date
  - Direction inference: raised / lowered / maintained / initiated
  - Rating normalization (Korean and English forms)
  - Broker name mapping (short and long Korean forms)
  - Action-row parsing: happy path + malformed rows
  - Table-level parsing: table#cTB24 actions + table#cTB15 summary
  - Missing-table graceful degradation (returns empty list / dict)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# --- Path setup ------------------------------------------------------------
# Make src/ importable when running as `python -m tests.test_naver_finance`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Stub src.state so we don't import the real retry/cache plumbing -------
import types
if "src.state" not in sys.modules:
    state_stub = types.ModuleType("src.state")

    class _Criticality:
        CRITICAL = "CRITICAL"
        IMPORTANT = "IMPORTANT"

    def _stub_fetch_with_retry(**kwargs):
        return (None, {})

    state_stub.Criticality = _Criticality
    state_stub.fetch_with_retry = _stub_fetch_with_retry
    sys.modules["src.state"] = state_stub

from src.data.naver_finance import (
    BrokerAction,
    KOREAN_TO_ENGLISH_BROKER,
    _compute_direction,
    _parse_action_row,
    _parse_krw_int,
    _parse_pct,
    _parse_wisereport_date,
    normalize_rating,
    parse_wisereport_actions,
    parse_wisereport_summary,
)

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Embedded fixture — mirrors the WiseReport response structure with a
# handful of representative rows (header + 5 data rows + 1 malformed row).
# Real WiseReport pages have ~25 broker rows; this trimmed version is
# enough to exercise every parser path without requiring a 92KB file.
# ---------------------------------------------------------------------------

FIXTURE_HTML = """\
<html lang="ko">
<body>

  <!-- Consensus summary -->
  <table id="cTB15" class="gHead all-width">
    <tr>
      <th>4.00</th>
      <th>투자의견</th>
      <th>목표주가(원)</th>
      <th>EPS(원)</th>
      <th>PER(배)</th>
      <th>추정기관수</th>
    </tr>
    <tr>
      <td>4.00</td>
      <td>2,051,200</td>
      <td>292,970</td>
      <td>6.21</td>
      <td>25</td>
    </tr>
  </table>

  <!-- Broker actions -->
  <table id="cTB24" class="gHead01 all-width">
    <tr>
      <th>제공처</th>
      <th>최종일자</th>
      <th>목표가</th>
      <th>직전목표가</th>
      <th>변동률(%)</th>
      <th>투자의견</th>
      <th>직전투자의견</th>
    </tr>
    <tr>
      <td>KB</td><td>26/05/15</td><td>3,000,000</td><td>2,800,000</td>
      <td>7.14</td><td>BUY</td><td>BUY</td>
    </tr>
    <tr>
      <td>미래에셋</td><td>26/05/11</td><td>2,700,000</td><td>2,700,000</td>
      <td>0.00</td><td>매수</td><td>매수</td>
    </tr>
    <tr>
      <td>BNK투자</td><td>26/05/12</td><td>1,850,000</td><td>1,300,000</td>
      <td>42.31</td><td>보유</td><td>보유</td>
    </tr>
    <tr>
      <td>유진투자</td><td>26/05/04</td><td>2,300,000</td><td>1,170,000</td>
      <td>96.58</td><td>STRONG BUY</td><td>BUY</td>
    </tr>
    <tr>
      <td>키움</td><td>26/05/11</td><td>1,900,000</td><td>1,300,000</td>
      <td>46.15</td><td>Outperform</td><td>BUY</td>
    </tr>
    <!-- Malformed row: only 4 cells; should be skipped without crashing -->
    <tr>
      <td>broken</td><td>row</td><td>only</td><td>four</td>
    </tr>
  </table>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helper parser tests
# ---------------------------------------------------------------------------

class TestParseKrwInt(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(_parse_krw_int("3,000,000"), 3_000_000)

    def test_man_suffix(self):
        # Korean 만 = 10,000
        self.assertEqual(_parse_krw_int("137만"), 1_370_000)
        self.assertEqual(_parse_krw_int("2.5만"), 25_000)

    def test_strips_won_suffix(self):
        self.assertEqual(_parse_krw_int("1,000원"), 1000)

    def test_empty_and_dashes(self):
        self.assertIsNone(_parse_krw_int(""))
        self.assertIsNone(_parse_krw_int("-"))
        self.assertIsNone(_parse_krw_int("N/A"))
        self.assertIsNone(_parse_krw_int("—"))

    def test_malformed_returns_none(self):
        self.assertIsNone(_parse_krw_int("garbage"))


class TestParsePct(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(_parse_pct("57.14"), 57.14)

    def test_with_percent_sign(self):
        self.assertEqual(_parse_pct("25%"), 25.0)

    def test_empty_dashes(self):
        self.assertIsNone(_parse_pct(""))
        self.assertIsNone(_parse_pct("-"))

    def test_malformed(self):
        self.assertIsNone(_parse_pct("garbage"))


class TestParseWisereportDate(unittest.TestCase):
    def test_two_digit_year_slash(self):
        self.assertEqual(_parse_wisereport_date("26/05/15"), "2026-05-15")

    def test_two_digit_year_dot(self):
        self.assertEqual(_parse_wisereport_date("26.05.15"), "2026-05-15")

    def test_four_digit_year(self):
        self.assertEqual(_parse_wisereport_date("2026/05/15"), "2026-05-15")

    def test_zero_padding(self):
        self.assertEqual(_parse_wisereport_date("26/5/1"), "2026-05-01")

    def test_malformed_falls_back_to_today(self):
        # Should not raise; falls back to today's ISO date
        result = _parse_wisereport_date("garbage")
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2}$")


class TestComputeDirection(unittest.TestCase):
    def test_raised(self):
        self.assertEqual(_compute_direction(3_000_000, 2_800_000), "raised")

    def test_lowered(self):
        self.assertEqual(_compute_direction(1_500_000, 1_700_000), "lowered")

    def test_maintained(self):
        self.assertEqual(_compute_direction(2_000_000, 2_000_000), "maintained")

    def test_initiated_when_prior_none(self):
        self.assertEqual(_compute_direction(1_000_000, None), "initiated")

    def test_initiated_when_prior_zero(self):
        self.assertEqual(_compute_direction(1_000_000, 0), "initiated")

    def test_target_none_returns_none(self):
        self.assertIsNone(_compute_direction(None, 1_000_000))


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------

class TestNormalizeRating(unittest.TestCase):
    def test_korean_buy(self):
        self.assertEqual(normalize_rating("매수"), "Buy")

    def test_korean_hold(self):
        self.assertEqual(normalize_rating("보유"), "Hold")
        self.assertEqual(normalize_rating("중립"), "Hold")

    def test_english_uppercase(self):
        self.assertEqual(normalize_rating("BUY"), "Buy")
        self.assertEqual(normalize_rating("HOLD"), "Hold")

    def test_english_titlecase(self):
        self.assertEqual(normalize_rating("Buy"), "Buy")

    def test_strong_buy(self):
        self.assertEqual(normalize_rating("STRONG BUY"), "Strong Buy")
        self.assertEqual(normalize_rating("강력매수"), "Strong Buy")

    def test_outperform_maps_to_buy(self):
        # Korean broker convention: Outperform = Buy
        self.assertEqual(normalize_rating("Outperform"), "Buy")

    def test_market_perform_maps_to_hold(self):
        self.assertEqual(normalize_rating("Market Perform"), "Hold")
        self.assertEqual(normalize_rating("Marketperform"), "Hold")

    def test_unknown_returns_none(self):
        self.assertIsNone(normalize_rating("garbage_rating"))

    def test_empty_returns_none(self):
        self.assertIsNone(normalize_rating(""))
        self.assertIsNone(normalize_rating(None))


# ---------------------------------------------------------------------------
# Broker name mapping
# ---------------------------------------------------------------------------

class TestBrokerNameMap(unittest.TestCase):
    def test_short_forms(self):
        # WiseReport uses short forms — verify the critical Tier-1 brokers
        self.assertEqual(KOREAN_TO_ENGLISH_BROKER["미래에셋"], "Mirae Asset Securities")
        self.assertEqual(KOREAN_TO_ENGLISH_BROKER["NH투자"], "NH Investment & Securities")
        self.assertEqual(KOREAN_TO_ENGLISH_BROKER["삼성"], "Samsung Securities")

    def test_long_forms_backward_compat(self):
        # Legacy long forms still map (preserves any cached state on disk)
        self.assertEqual(KOREAN_TO_ENGLISH_BROKER["미래에셋증권"], "Mirae Asset Securities")
        self.assertEqual(KOREAN_TO_ENGLISH_BROKER["삼성증권"], "Samsung Securities")


# ---------------------------------------------------------------------------
# Row-level parser
# ---------------------------------------------------------------------------

class TestParseActionRow(unittest.TestCase):
    def _make_cells(self, *texts):
        # Build a list of soup elements like BeautifulSoup would yield
        soup = BeautifulSoup(
            "<tr>" + "".join(f"<td>{t}</td>" for t in texts) + "</tr>",
            "html.parser",
        )
        return soup.find_all("td")

    def test_happy_path(self):
        cells = self._make_cells("KB", "26/05/15", "3,000,000", "2,800,000",
                                 "7.14", "BUY", "BUY")
        action = _parse_action_row(cells, "SK Hynix", "000660")
        self.assertIsNotNone(action)
        self.assertEqual(action.broker_korean, "KB")
        self.assertEqual(action.broker_english, "KB Securities")
        self.assertEqual(action.target_price_krw, 3_000_000)
        self.assertEqual(action.prior_target_krw, 2_800_000)
        self.assertAlmostEqual(action.target_change_pct, 7.14)
        self.assertEqual(action.rating, "BUY")
        self.assertEqual(action.rating_english, "Buy")
        self.assertEqual(action.prior_rating, "Buy")
        self.assertEqual(action.direction, "raised")
        self.assertEqual(action.published_date_iso, "2026-05-15")

    def test_wrong_cell_count_returns_none(self):
        cells = self._make_cells("only", "four", "cells", "here")
        self.assertIsNone(_parse_action_row(cells, "SK Hynix", "000660"))

    def test_header_row_returns_none(self):
        cells = self._make_cells("제공처", "최종일자", "목표가", "직전목표가",
                                 "변동률(%)", "투자의견", "직전투자의견")
        self.assertIsNone(_parse_action_row(cells, "SK Hynix", "000660"))

    def test_korean_rating(self):
        cells = self._make_cells("미래에셋", "26/05/11", "2,700,000", "2,700,000",
                                 "0.00", "매수", "매수")
        action = _parse_action_row(cells, "SK Hynix", "000660")
        self.assertEqual(action.rating_english, "Buy")
        self.assertEqual(action.direction, "maintained")

    def test_unmapped_broker_keeps_korean(self):
        cells = self._make_cells("UnknownBroker", "26/05/15", "1,000,000", "900,000",
                                 "11.11", "BUY", "BUY")
        action = _parse_action_row(cells, "SK Hynix", "000660")
        self.assertEqual(action.broker_korean, "UnknownBroker")
        self.assertIsNone(action.broker_english)  # gracefully None


# ---------------------------------------------------------------------------
# Table-level parsers
# ---------------------------------------------------------------------------

class TestParseWisereportActions(unittest.TestCase):
    def test_fixture_yields_five_actions(self):
        # Fixture has 5 valid rows + 1 malformed (should be skipped) + 1 header
        actions = parse_wisereport_actions(FIXTURE_HTML, "SK Hynix", "000660")
        self.assertEqual(len(actions), 5)

    def test_all_rows_mapped_to_english(self):
        actions = parse_wisereport_actions(FIXTURE_HTML, "SK Hynix", "000660")
        for a in actions:
            self.assertIsNotNone(a.broker_english,
                                 f"Broker '{a.broker_korean}' has no English mapping")
            self.assertIsNotNone(a.rating_english,
                                 f"Rating '{a.rating}' could not be normalized")

    def test_target_change_pct_consistent_with_direction(self):
        actions = parse_wisereport_actions(FIXTURE_HTML, "SK Hynix", "000660")
        for a in actions:
            if a.direction == "raised":
                self.assertGreater(a.target_price_krw, a.prior_target_krw)
            elif a.direction == "lowered":
                self.assertLess(a.target_price_krw, a.prior_target_krw)
            elif a.direction == "maintained":
                self.assertEqual(a.target_price_krw, a.prior_target_krw)

    def test_missing_table_returns_empty(self):
        actions = parse_wisereport_actions(
            "<html><body><p>No table here</p></body></html>",
            "SK Hynix", "000660"
        )
        self.assertEqual(actions, [])


class TestParseWisereportSummary(unittest.TestCase):
    def test_fixture_summary(self):
        summary = parse_wisereport_summary(FIXTURE_HTML)
        self.assertEqual(summary.get("avg_opinion_score"), 4.00)
        self.assertEqual(summary.get("avg_target_price_krw"), 2_051_200)
        self.assertEqual(summary.get("avg_eps_krw"), 292_970)
        self.assertAlmostEqual(summary.get("avg_per"), 6.21)
        self.assertEqual(summary.get("num_estimating_institutions"), 25)

    def test_missing_table_returns_empty_dict(self):
        summary = parse_wisereport_summary("<html></html>")
        self.assertEqual(summary, {})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
