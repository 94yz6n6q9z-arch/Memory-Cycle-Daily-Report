"""
Unit tests for the v3.39.4 Mirae Asset HTML scraper.

Runs entirely offline against embedded HTML fixtures modeled on real
production responses (one listing row block from the 2026-05-18 Phase D
diagnostic, one full detail page for messageId=2334442 / SK Inc. fetched
via Anthropic web_fetch during v3.39.4 design).

Run with:  python -m tests.test_mirae_asset

Coverage:
  - Listing date cell parsing (MM.DD.YYYY → ISO)
  - Detail page KST date parsing
  - Korean ticker code extraction from titles
  - Listing parser: full row extraction, malformed row tolerance
  - Relevance filter: ticker-match path, keyword-match path, miss case
  - Detail parser: title / category / author / date / stock code / rating
  - Body extraction with Prev/Next trimming
  - Target price regex against representative phrasings
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

# --- Path setup ------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Stub src.state so we don't import the real retry/cache plumbing -------
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

from src.data.korean_brokers import (
    MiraeListingRow,
    TRACKED_KOREAN_TICKER_CODES,
    _extract_korean_ticker_from_title,
    _extract_largest_text_block,
    _parse_mirae_date_cell,
    _parse_mirae_detail,
    _parse_mirae_kst_date,
    _parse_mirae_listing,
    _row_is_relevant,
)
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Three rows from the real 2026-05-18 Phase D listing dump. The first
# (Misto Holdings) is a non-tracked Korean fashion/textile name — should be
# filtered OUT. The second is a fabricated SK Hynix row that uses the EXACT
# same template (so the parser sees the same shape; only the values differ),
# which should pass the relevance filter via ticker code match. The third is
# fabricated to test keyword-match (no tracked ticker but contains "DRAM").
LISTING_FIXTURE = """
<table>
  <thead>
    <tr><th>Date</th><th>Category</th><th>Title</th><th>Author</th><th>File</th></tr>
  </thead>
  <tbody>
    <tr>
      <td class="font_spoqa">05.18.2026</td>
      <td><strong>Company Analysis</strong></td>
      <td>
        <div class="box_txt">
          <a href="javascript:void(0);" data-message-id="2339922" id="message_2339922_sub">
            <strong>Misto Holdings (081660 KS/Buy)</strong>
          </a>
          <a href="javascript:void(0);" class="txt_area" data-message-id="2339922" id="message_2339922_main">
            <p class="ellipsis">Steady performance</p>
          </a>
        </div>
      </td>
      <td>Songyi Bae</td>
      <td>
        <a class="btn_round_s black" data-attachment-id="2144703" href="javascript:void(0);" id="attachment_2144703">FILE</a>
      </td>
    </tr>
    <tr>
      <td class="font_spoqa">05.17.2026</td>
      <td><strong>Company Analysis</strong></td>
      <td>
        <div class="box_txt">
          <a href="javascript:void(0);" data-message-id="2339911" id="message_2339911_sub">
            <strong>SK Hynix (000660 KS/Buy)</strong>
          </a>
          <a href="javascript:void(0);" class="txt_area" data-message-id="2339911" id="message_2339911_main">
            <p class="ellipsis">HBM4 mass production on track</p>
          </a>
        </div>
      </td>
      <td>Young-gun Kim</td>
      <td>
        <a class="btn_round_s black" data-attachment-id="2144695" href="javascript:void(0);" id="attachment_2144695">FILE</a>
      </td>
    </tr>
    <tr>
      <td class="font_spoqa">05.16.2026</td>
      <td><strong>Industry Analysis</strong></td>
      <td>
        <div class="box_txt">
          <a href="javascript:void(0);" data-message-id="2339888" id="message_2339888_sub">
            <strong>Semiconductors Sector Note</strong>
          </a>
          <a href="javascript:void(0);" class="txt_area" data-message-id="2339888" id="message_2339888_main">
            <p class="ellipsis">DRAM contract prices firming into Q3</p>
          </a>
        </div>
      </td>
      <td>Sales Team</td>
      <td>
        <a class="btn_round_s black" data-attachment-id="2144680" href="javascript:void(0);" id="attachment_2144680">FILE</a>
      </td>
    </tr>
  </tbody>
</table>
"""

# A condensed but structurally faithful detail page based on messageId=2334442
# (SK Inc., Young-gun Kim, Sep 8 2025). The metadata block, body, and
# Prev/Next nav match the real page; surrounding chrome (header/footer) is
# trimmed for fixture brevity.
DETAIL_FIXTURE = """<!DOCTYPE html>
<html><head><title>Research Report | Mirae Asset Securities</title></head>
<body>
<div class="header_wrap"><div class="gnb">...nav chrome trimmed...</div></div>
<div id="container" class="content_area">
  <h2>Research Report</h2>
  <p><strong>Company Analysis</strong></p>
  <h2>SK Inc. (034730 KS/Buy) Ample upside potential</h2>
  <ul class="report_meta">
    <li><strong>Author</strong> Young-gun Kim</li>
    <li><strong>Date</strong> Mon Sep 08 00:00:00 KST 2025</li>
    <li><strong>Stock Code</strong> 034730</li>
    <li><strong>File</strong>
      <a href="javascript:void(0);">20250908_SK Inc. (034730 KS_Buy).pdf</a>
    </li>
  </ul>
  <div class="report_body">
    <p><strong>Ample upside potential</strong></p>
    <p><strong>Recommendation and valuation</strong></p>
    <p>We maintain our Buy rating on SK Inc. and raise our target price to W279,000
    (from W275,000). We believe our target price is conservative, given:
    1) SK AX's growth trajectory (driven by full-scale AI adoption and productivity gains);
    2) the conservative discount we applied to subsidiary valuations; and
    3) the company's commitment to fixed dividends and capacity for share buybacks/cancellations.</p>
    <p>We estimate SK Inc.'s standalone enterprise value at W6.5tr, which is based on
    our 2026 EBITDA estimate of W1.3tr and an EV/EBITDA multiple of 5.2x — the average
    of system integration (SI) peers Samsung SDS (3.4x) and LG CNS (7.0x). While
    SK AX's 2026F EBITDA (W0.4tr) is smaller than the levels of Samsung SDS (W1.6tr)
    and LG CNS (W0.7tr), we believe our multiple is justified given steady cash inflows
    from affiliate royalties and dividends.</p>
    <p>Under its shareholder return policy, SK Inc. plans to pay a fixed dividend of
    W5,000 per share (including the W1,500 interim dividend already paid in 1H25) and
    allocate 1–2% of its market cap to share buybacks/cancellations as a means of
    distributing divestment proceeds to shareholders. Considering the rise in market
    cap compared to the beginning of the year, we expect a W150bn buyback to be
    executed within the next three months. Based on the current share price, dividend
    yield is estimated at 3.5-4.5%.</p>
  </div>
  <div class="nav_prev_next">
    <p><strong>Prev</strong> <a href="javascript:void(0);">Netmarble (251270 KS/Buy) A strong buy</a></p>
    <p><strong>Next</strong> <a href="javascript:void(0);">CATL (300750 CH/Buy) Industry consolidation</a></p>
    <p><a href="javascript:void(0);">List</a></p>
  </div>
</div>
<div class="footer_wrap">...footer chrome trimmed...</div>
</body></html>
"""


# A condensed but structurally faithful detail page based on messageId=2335435
# (SK Hynix Oct 30 2025, Young-gun Kim — "Capacity fully booked"). Fetched live
# during v3.39.4 design via Anthropic web_fetch as same-shape validation for the
# May 11 2026 PDF Raul flagged. Trimmed to the article block + nav markers; the
# raise-by-percentage phrasing in the body is the exact phrasing that
# uncovered the [^\d] bug in the first target-price regex.
SK_HYNIX_DETAIL_FIXTURE = """<!DOCTYPE html>
<html><head><title>Research Report | Mirae Asset Securities</title></head>
<body>
<div class="header_wrap"><div class="gnb">...nav chrome trimmed...</div></div>
<div id="container" class="content_area">
  <h2>Research Report</h2>
  <p><strong>Company Analysis</strong></p>
  <h2>SK Hynix (000660 KS/Buy) Capacity fully booked</h2>
  <ul class="report_meta">
    <li><strong>Author</strong> Young-gun Kim</li>
    <li><strong>Date</strong> Thu Oct 30 00:00:00 KST 2025</li>
    <li><strong>Stock Code</strong> 000660</li>
    <li><strong>File</strong>
      <a href="javascript:void(0);">20251030_SK Hynix (000660 KS_Buy).pdf</a>
    </li>
  </ul>
  <div class="report_body">
    <p><strong>Capacity fully booked</strong></p>
    <p><strong>Raising 2026 earnings estimates and target multiple</strong></p>
    <p>We raise our target price for SK Hynix by 30.7% to W680,000 (from W520,000).
    We revised up our 2026 operating profit estimate by 4.5% to W62.6tr and our
    target P/B by 16% to 2.9x. Notably, reflecting the strong upward trend in memory
    contract prices, we have lifted our 4Q25 DRAM and NAND ASP growth assumptions
    by 4.1%p and 6.9%p, respectively. We expect prices to remain elevated through 2026.</p>
    <p>Although our target P/B of 2.9x (based on the memory sector's average 12-month
    forward multiple) exceeds SK Hynix's historical valuation range, we believe this
    is justified given the company's projected 2026 ROE of 38%, which is the highest
    in the industry. We also note that the company owns an approximately 40% stake
    in Kioxia, which currently garners a P/B of 4.2x — the highest among chipmakers.</p>
    <p><strong>Production capacity fully booked through 2026; further upside for HBM prices</strong></p>
    <p>For 4Q25, we expect SK Hynix to deliver record quarterly results, with revenue
    of W27.9tr (+14.1% QoQ) and operating profit of W13.6tr (+19.8% QoQ). The firm
    has indicated that it will keep DRAM and NAND bit growth (QoQ) at low-single-digit
    levels citing normalized inventory levels and signaling a continued focus on
    profitability. Meanwhile, we forecast DRAM and NAND ASPs to rise 8.4% and 11.2%
    QoQ, respectively, driving both top- and bottom-line growth.</p>
  </div>
  <div class="nav_prev_next">
    <p><strong>Prev</strong> <a href="javascript:void(0);">Samsung Biologics (207940 KS/Buy) Spin-off likely to unlock additional value</a></p>
    <p><strong>Next</strong> <a href="javascript:void(0);">Samsung Electro-Mechanics (009150 KS/Buy) Seasonality fading amid structural growth</a></p>
    <p><a href="javascript:void(0);">List</a></p>
  </div>
</div>
<div class="footer_wrap">...footer chrome trimmed...</div>
</body></html>
"""




class TestParseMiraeDateCell(unittest.TestCase):
    def test_standard_us_format(self):
        self.assertEqual(_parse_mirae_date_cell("05.18.2026"), "2026-05-18")

    def test_zero_padded(self):
        self.assertEqual(_parse_mirae_date_cell("01.07.2026"), "2026-01-07")

    def test_slash_separator(self):
        self.assertEqual(_parse_mirae_date_cell("05/18/2026"), "2026-05-18")

    def test_two_digit_year(self):
        self.assertEqual(_parse_mirae_date_cell("05.18.26"), "2026-05-18")

    def test_malformed_falls_back_to_today(self):
        from datetime import date as _date
        result = _parse_mirae_date_cell("not a date")
        self.assertEqual(result, _date.today().isoformat())


class TestParseMiraeKstDate(unittest.TestCase):
    def test_full_kst_format(self):
        self.assertEqual(
            _parse_mirae_kst_date("Mon Sep 08 00:00:00 KST 2025"),
            "2025-09-08",
        )

    def test_january_date(self):
        self.assertEqual(
            _parse_mirae_kst_date("Thu Jan 30 00:00:00 KST 2026"),
            "2026-01-30",
        )

    def test_iso_fallback(self):
        self.assertEqual(_parse_mirae_kst_date("2026-05-18"), "2026-05-18")

    def test_malformed_falls_back_to_today(self):
        from datetime import date as _date
        self.assertEqual(_parse_mirae_kst_date("garbage"), _date.today().isoformat())


class TestExtractKoreanTicker(unittest.TestCase):
    def test_sk_hynix(self):
        self.assertEqual(_extract_korean_ticker_from_title("SK Hynix (000660 KS/Buy)"), "000660")

    def test_samsung(self):
        self.assertEqual(_extract_korean_ticker_from_title("Samsung Electronics (005930 KS/Buy)"), "005930")

    def test_no_ticker(self):
        self.assertIsNone(_extract_korean_ticker_from_title("Semiconductors Sector Note"))

    def test_us_listing_not_matched(self):
        # "(300750 CH/Buy)" — CH not KS — should NOT match Korean ticker pattern
        self.assertIsNone(_extract_korean_ticker_from_title("CATL (300750 CH/Buy)"))

    def test_with_spaces(self):
        self.assertEqual(_extract_korean_ticker_from_title("Hansae (105630 KS/Buy)"), "105630")


class TestParseMiraeListing(unittest.TestCase):
    def setUp(self):
        self.rows = _parse_mirae_listing(LISTING_FIXTURE)

    def test_three_rows_parsed(self):
        self.assertEqual(len(self.rows), 3)

    def test_first_row_misto(self):
        r = self.rows[0]
        self.assertEqual(r.message_id, "2339922")
        self.assertEqual(r.attachment_id, "2144703")
        self.assertEqual(r.category, "Company Analysis")
        self.assertEqual(r.title, "Misto Holdings (081660 KS/Buy)")
        self.assertEqual(r.subtitle, "Steady performance")
        self.assertEqual(r.author, "Songyi Bae")
        self.assertEqual(r.published_date_iso, "2026-05-18")
        self.assertEqual(r.ticker_code_korean, "081660")

    def test_second_row_sk_hynix(self):
        r = self.rows[1]
        self.assertEqual(r.message_id, "2339911")
        self.assertEqual(r.title, "SK Hynix (000660 KS/Buy)")
        self.assertEqual(r.ticker_code_korean, "000660")
        self.assertEqual(r.published_date_iso, "2026-05-17")

    def test_third_row_industry(self):
        r = self.rows[2]
        self.assertEqual(r.message_id, "2339888")
        self.assertEqual(r.category, "Industry Analysis")
        self.assertEqual(r.title, "Semiconductors Sector Note")
        self.assertIsNone(r.ticker_code_korean)
        self.assertEqual(r.subtitle, "DRAM contract prices firming into Q3")

    def test_missing_table_returns_empty(self):
        self.assertEqual(_parse_mirae_listing("<html><body><p>no rows</p></body></html>"), [])

    def test_malformed_row_skipped(self):
        # Anchor with data-message-id but missing the parent <tr>'s tds
        bad = '<tr><a data-message-id="999" id="message_999_sub"><strong>orphan</strong></a></tr>'
        result = _parse_mirae_listing(bad)
        self.assertEqual(result, [])


class TestRelevanceFilter(unittest.TestCase):
    def _row(self, **kwargs):
        defaults = dict(
            message_id="1", attachment_id=None, category="Company Analysis",
            title="X", subtitle="", author="", published_date_iso="2026-01-01",
            ticker_code_korean=None,
        )
        defaults.update(kwargs)
        return MiraeListingRow(**defaults)

    def test_tracked_ticker_passes(self):
        self.assertTrue(_row_is_relevant(self._row(
            title="SK Hynix (000660 KS/Buy)", ticker_code_korean="000660",
        )))

    def test_untracked_korean_ticker_passes_keyword(self):
        # Has a 6-digit ticker but it's not tracked AND title has keyword
        self.assertTrue(_row_is_relevant(self._row(
            title="Random Memory Co (123456 KS/Hold)", ticker_code_korean="123456",
            subtitle="DRAM stalwart",
        )))

    def test_untracked_no_keyword_rejected(self):
        self.assertFalse(_row_is_relevant(self._row(
            title="Misto Holdings (081660 KS/Buy)", ticker_code_korean="081660",
            subtitle="Steady performance",
        )))

    def test_industry_note_with_dram_keyword_passes(self):
        self.assertTrue(_row_is_relevant(self._row(
            title="Semiconductors Sector Note",
            subtitle="DRAM contract prices firming into Q3",
        )))

    def test_industry_note_with_hbm_keyword_passes(self):
        self.assertTrue(_row_is_relevant(self._row(
            title="AI memory deep dive — HBM4 ramp signals",
        )))

    def test_completely_unrelated_rejected(self):
        self.assertFalse(_row_is_relevant(self._row(
            title="Cosmetics Sector Note", subtitle="K-Beauty exports recovering",
        )))

    def test_samsung_fire_and_marine_rejected_after_v3_39_4_1_keyword_tightening(self):
        # v3.39.4.1 regression test. Samsung Fire & Marine is an insurance
        # company (ticker 000810 — not in TRACKED_KOREAN_TICKER_CODES). Phase E
        # dry-run output 2026-05-18 caught the original "samsung" keyword
        # pulling this row into the digester. With "samsung" dropped from
        # TRACKED_TITLE_KEYWORDS in v3.39.4.1, this title must NO LONGER pass.
        self.assertFalse(_row_is_relevant(self._row(
            title="Samsung Fire & Marine (000810 KS/Hold)",
            subtitle="New business CSM down 11% YoY",
            ticker_code_korean="000810",
        )))

    def test_samsung_sdi_rejected_after_v3_39_4_1(self):
        # Same class as the Fire & Marine case — batteries, not memory.
        self.assertFalse(_row_is_relevant(self._row(
            title="Samsung SDI (006400 KS/Buy)",
            subtitle="EV battery shipments improving",
            ticker_code_korean="006400",
        )))

    def test_samsung_electronics_still_passes_via_ticker_match(self):
        # The ONLY samsung name we still want — caught by ticker-match, not
        # keyword. Verify the v3.39.4.1 keyword removal didn't break this path.
        self.assertTrue(_row_is_relevant(self._row(
            title="Samsung Electronics (005930 KS/Buy)",
            subtitle="HBM3E ramp on track",
            ticker_code_korean="005930",
        )))


class TestParseMiraeDetail(unittest.TestCase):
    def setUp(self):
        self.detail = _parse_mirae_detail(DETAIL_FIXTURE)

    def test_title_extracted(self):
        self.assertIn("SK Inc. (034730 KS/Buy)", self.detail["title"])
        self.assertIn("Ample upside potential", self.detail["title"])

    def test_category_extracted(self):
        self.assertEqual(self.detail["category"], "Company Analysis")

    def test_author_extracted(self):
        self.assertEqual(self.detail["author"], "Young-gun Kim")

    def test_date_extracted(self):
        self.assertEqual(self.detail["published_date_iso"], "2025-09-08")

    def test_stock_code_extracted(self):
        self.assertEqual(self.detail["stock_code"], "034730")

    def test_rating_extracted_from_title(self):
        self.assertEqual(self.detail["rating"], "Buy")

    def test_target_price_extracted(self):
        self.assertEqual(self.detail["target_price_krw"], 279000)

    def test_body_contains_key_phrases(self):
        body = self.detail["body_text"]
        self.assertIn("Recommendation and valuation", body)
        self.assertIn("target price to W279,000", body)
        # Should NOT contain the Prev/Next navigation
        self.assertNotIn("Netmarble", body)
        self.assertNotIn("CATL", body)

    def test_body_length_above_digester_minimum(self):
        # digester rejects notes with <200 chars body
        self.assertGreater(len(self.detail["body_text"]), 200)


class TestParseSkHynixDetailEndToEnd(unittest.TestCase):
    """End-to-end validation against the SK Hynix Oct 30 2025 detail page
    fixture (messageId=2335435). Modeled on the actual HTML structure fetched
    live during v3.39.4 design. This is the regression guard against the
    real-world phrasing class that broke v3.39.4's initial target-price
    regex — see TestTargetPriceRegex.test_raise_by_percentage_to_w."""

    def setUp(self):
        self.detail = _parse_mirae_detail(SK_HYNIX_DETAIL_FIXTURE)

    def test_title_extracted(self):
        self.assertIn("SK Hynix (000660 KS/Buy)", self.detail["title"])

    def test_stock_code_is_sk_hynix(self):
        self.assertEqual(self.detail["stock_code"], "000660")

    def test_rating_is_buy(self):
        self.assertEqual(self.detail["rating"], "Buy")

    def test_author_is_young_gun_kim(self):
        self.assertEqual(self.detail["author"], "Young-gun Kim")

    def test_date_is_oct_30_2025(self):
        self.assertEqual(self.detail["published_date_iso"], "2025-10-30")

    def test_target_price_extracted_despite_percentage_in_phrasing(self):
        # This is the regression test: phrasing is "raise our target price by 30.7% to W680,000"
        self.assertEqual(self.detail["target_price_krw"], 680000)

    def test_body_has_analytical_content(self):
        body = self.detail["body_text"]
        self.assertIn("Raising 2026 earnings estimates", body)
        self.assertIn("target price for SK Hynix", body)
        self.assertIn("W680,000", body)

    def test_body_excludes_prev_next_nav(self):
        body = self.detail["body_text"]
        self.assertNotIn("Samsung Biologics", body)
        self.assertNotIn("Samsung Electro-Mechanics", body)


class TestTargetPriceRegex(unittest.TestCase):
    """The TP regex sees a variety of phrasings in Mirae reports. Verify each.

    Real-world validation: cases below mirror SK Hynix Oct 30 2025
    (messageId=2335435, fetched live during v3.39.4 design) and SK Hynix
    May 11 2026 (the PDF Raul flagged for confirmation). v3.39.4 initially
    shipped with a [^\\d]{1,20} interior pattern that broke on the "raise by
    X% to W{NEW}" phrasing — fixed mid-release after live validation."""

    def _tp(self, body):
        return _parse_mirae_detail(
            f'<html><body><div class="report_body"><p>{"X "*60} {body}</p></div></body></html>'
        )["target_price_krw"]

    def test_raise_to(self):
        self.assertEqual(self._tp("raise our target price to W279,000 (from W275,000)"), 279000)

    def test_lower_to(self):
        self.assertEqual(self._tp("we lower our target price to W137,000"), 137000)

    def test_of_w(self):
        self.assertEqual(self._tp("target price of W2,700,000 is justified"), 2700000)

    def test_tp_shorthand(self):
        self.assertEqual(self._tp("12-month TP of W1,370,000 implied"), 1370000)

    def test_maintain_at_w(self):
        # SK Hynix May 11 2026 phrasing exactly
        self.assertEqual(
            self._tp("We maintain our target price for SK Hynix at W2,700,000"),
            2700000,
        )

    def test_raise_by_percentage_to_w(self):
        # SK Hynix Oct 30 2025 phrasing — has digits (the %) BETWEEN
        # "target price" and the new TP. This was the bug v3.39.4 caught
        # via live validation against the actual messageId=2335435 page.
        self.assertEqual(
            self._tp("We raise our target price for SK Hynix by 30.7% to W680,000 (from W520,000)"),
            680000,
        )

    def test_raise_by_percentage_to_w_short_form(self):
        # Same shape, different connector words
        self.assertEqual(
            self._tp("We lift our target price on SK Hynix by 15% to W782,000 (from W680,000)"),
            782000,
        )

    def test_asp_does_not_falsely_match_as_tp(self):
        # \bTP\b word boundary should prevent matching "TP" inside "ASP"
        self.assertIsNone(
            self._tp("the company achieved ASP growth, reaching W5,000 per unit")
        )

    def test_no_target_price_returns_none(self):
        self.assertIsNone(self._tp("we maintain our Buy rating; the stock is attractive."))


class TestExtractLargestTextBlock(unittest.TestCase):
    def test_prev_next_trimmed(self):
        html = """<html><body><div>
            <p>This is a long body paragraph that runs over three hundred characters so
            that the largest-block heuristic selects it as the content container. It
            keeps repeating sentences to reach the threshold. It keeps repeating. It
            keeps repeating. It keeps repeating. It keeps repeating. It keeps repeating.</p>
            <p>Prev</p>
            <p>This should be trimmed because it follows the Prev marker.</p>
        </div></body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_largest_text_block(soup)
        self.assertIn("long body paragraph", result)
        self.assertNotIn("should be trimmed", result)

    def test_short_pages_fall_back_to_body(self):
        soup = BeautifulSoup("<html><body><p>too short</p></body></html>", "html.parser")
        result = _extract_largest_text_block(soup)
        self.assertEqual(result, "too short")


class TestTrackedTickerSet(unittest.TestCase):
    def test_set_matches_naver_tickers(self):
        # Anti-drift guard: if NAVER_TICKERS changes, this should fail and
        # remind us to keep the Mirae filter in sync.
        self.assertEqual(
            TRACKED_KOREAN_TICKER_CODES,
            {"000660", "005930", "034730"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

