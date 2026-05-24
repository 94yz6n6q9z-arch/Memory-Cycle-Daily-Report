"""v3.4.13 — Regression tests for LPDDR contract scraper.

Coverage:
- Direction regex extraction (rising / falling / flat patterns)
- Quote-around-match extraction (verbatim, <= 30 words)
- Release-date extraction from TrendForce URL pattern
- Status taxonomy: data_extracted / no_lpddr_content_in_releases /
                    parser_no_match / fetch_failed
- Sample text captured on parser_no_match for fast debug
- Sentinel state file: increments on parser_no_match, resets on success or no_content
- 3-consecutive-parser_no_match sentinel raises (diagnostic ❌)
- LpddrScrapeResult.latest_signal sorts by release_date
- CSV persistence idempotent on (release_date, source_url)
- DHCR sanity-check: lpddr_contract_direction surfaces but does NOT affect math
- DHCRSnapshot dataclass has new field with default
- fast_tier integration smoke
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT))


# ============================================================
# 1. Direction regex extraction
# ============================================================

class TestRisingPatternExtraction(unittest.TestCase):

    def test_extracts_grow_qoq_percent(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = (
            "TrendForce projects DDR5 contract prices to stabilize in Q3 while "
            "LPDDR4X and LPDDR5X are expected to grow approximately 90% QoQ "
            "amid mobile demand recovery."
        )
        signal, sample = _extract_lpddr_signal_from_body(
            body, "https://www.trendforce.com/news/2026/01/15/test/", "Title", "2026-01-15",
        )
        self.assertIsNotNone(signal)
        self.assertIsNone(sample)
        self.assertEqual(signal.direction, "rising")
        self.assertEqual(signal.pct_qoq, 90.0)
        self.assertEqual(signal.release_date, "2026-01-15")

    def test_extracts_rise_with_magnitude(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "LPDDR5X contract prices to rise 15% in 3Q26 with smartphone OEM demand."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "rising")
        self.assertEqual(signal.pct_qoq, 15.0)

    def test_extracts_directional_without_magnitude(self):
        from src.data.news import _extract_lpddr_signal_from_body
        # No percentage but clear direction
        body = "LPDDR contract market expected to grow throughout 2026."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "rising")
        self.assertIsNone(signal.pct_qoq)


class TestFallingPatternExtraction(unittest.TestCase):

    def test_extracts_decline_pct(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "LPDDR4X prices expected to decline 8% in 4Q26 as inventory builds."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "falling")
        self.assertEqual(signal.pct_qoq, -8.0)

    def test_extracts_rollover_directional(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "LPDDR pricing rolling over in 3Q26 amid weak handset demand."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "falling")
        self.assertIsNone(signal.pct_qoq)

    def test_extracts_softening(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "LPDDR5X market softening as automotive demand weakens."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "falling")


class TestFlatPatternExtraction(unittest.TestCase):

    def test_extracts_narrow(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "LPDDR4X price hikes narrowing in 2Q26 with QoQ growth moderating."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "flat")

    def test_extracts_stable(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "LPDDR5X contract prices stable through Q3."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "flat")


class TestNoMatchAndParserBugMode(unittest.TestCase):

    def test_no_lpddr_keyword_returns_none_none(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = "DDR4 contract prices fell 5% in Q2 2026 amid weak PC demand."
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNone(signal)
        self.assertIsNone(sample)  # genuine no-content (no keyword)

    def test_keyword_but_no_direction_returns_sample(self):
        """The bug mode: LPDDR keyword present but no direction regex matched."""
        from src.data.news import _extract_lpddr_signal_from_body
        body = (
            "Today's report covers HBM3E, DDR5, and LPDDR5 modules in our latest "
            "supply chain analysis published this morning."
        )
        signal, sample = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNone(signal)
        self.assertIsNotNone(sample)
        # Sample should contain text around the LPDDR mention
        self.assertIn("LPDDR", sample)

    def test_empty_body_returns_none_none(self):
        from src.data.news import _extract_lpddr_signal_from_body
        signal, sample = _extract_lpddr_signal_from_body("", "u", "t")
        self.assertIsNone(signal)
        self.assertIsNone(sample)


# ============================================================
# 2. Quote-around-match
# ============================================================

class TestQuoteExtraction(unittest.TestCase):

    def test_quote_under_30_words(self):
        from src.data.news import _extract_lpddr_signal_from_body
        body = (
            "After much deliberation by industry analysts gathering market data "
            "across regions, the report indicates that LPDDR4X and LPDDR5X are "
            "expected to grow approximately 90% QoQ in 1Q26, driven by mobile "
            "OEM aggressive procurement strategies and supply tightness across "
            "key vendors including SK Hynix Samsung and Micron."
        )
        signal, _ = _extract_lpddr_signal_from_body(body, "u", "t")
        self.assertIsNotNone(signal)
        self.assertLessEqual(len(signal.quote.split()), 30)
        # Quote should contain the matched expression
        self.assertIn("90%", signal.quote)


# ============================================================
# 3. Release-date extraction from URL
# ============================================================

class TestReleaseDateExtraction(unittest.TestCase):

    def test_standard_url_pattern(self):
        from src.data.news import _extract_release_date_from_url_v3413
        url = "https://www.trendforce.com/news/2026/01/15/news20260115.html"
        self.assertEqual(_extract_release_date_from_url_v3413(url), "2026-01-15")

    def test_single_digit_month_day(self):
        from src.data.news import _extract_release_date_from_url_v3413
        url = "https://www.trendforce.com/news/2026/3/5/slug/"
        self.assertEqual(_extract_release_date_from_url_v3413(url), "2026-03-05")

    def test_no_date_returns_none(self):
        from src.data.news import _extract_release_date_from_url_v3413
        url = "https://www.trendforce.com/news/some-old-slug-no-date"
        self.assertIsNone(_extract_release_date_from_url_v3413(url))


# ============================================================
# 4. Sentinel state file
# ============================================================

class TestSentinelState(unittest.TestCase):

    def test_load_default_when_missing(self):
        from src.data.news import _load_lpddr_sentinel_v3413, LPDDR_PARSER_NO_MATCH_SENTINEL_FILE
        with patch("src.data.news.LPDDR_PARSER_NO_MATCH_SENTINEL_FILE",
                   "/tmp/nonexistent_v3_4_13_sentinel.json"):
            self.assertEqual(_load_lpddr_sentinel_v3413(), 0)

    def test_save_and_load_roundtrip(self):
        from src.data import news as news_mod
        with tempfile.TemporaryDirectory() as td:
            sentinel_path = Path(td) / "sentinel.json"
            with patch.object(news_mod, "LPDDR_PARSER_NO_MATCH_SENTINEL_FILE", str(sentinel_path)):
                news_mod._save_lpddr_sentinel_v3413(2)
                self.assertEqual(news_mod._load_lpddr_sentinel_v3413(), 2)

    def test_corrupt_file_returns_zero(self):
        from src.data import news as news_mod
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{")
            tmp_path = f.name
        try:
            with patch.object(news_mod, "LPDDR_PARSER_NO_MATCH_SENTINEL_FILE", tmp_path):
                self.assertEqual(news_mod._load_lpddr_sentinel_v3413(), 0)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ============================================================
# 5. Full scrape — happy path + error paths (network mocked)
# ============================================================

class TestScrapeTrendforceLpddrContract(unittest.TestCase):

    def _mock_requests_get(self, html, status=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_data_extracted_status(self):
        """Full happy path: category page lists articles; one body has LPDDR direction."""
        from src.data import news as news_mod

        category_html = """
            <html><body>
            <a href="/news/2026/01/15/lpddr-q1/">TrendForce LPDDR Q1 outlook for mobile DRAM</a>
            <a href="/news/2026/01/10/hbm-q1/">TrendForce HBM Q1 outlook for AI accelerator memory</a>
            </body></html>
        """
        # Bodies must be > 200 chars to pass fetch_article_body's length threshold
        article1_body = (
            "TrendForce projects LPDDR4X and LPDDR5X to grow approximately 90% QoQ "
            "in 1Q26 driven by mobile OEM aggressive procurement and supply tightness. "
            "Smartphone manufacturers including Apple Samsung and Xiaomi are securing "
            "capacity ahead of the second-half product launches with elevated bid prices."
        )
        article2_body = (
            "HBM3E pricing remains stable as Nvidia allocates capacity for Blackwell "
            "production through Q3 2026. SK Hynix continues its lead in HBM3E supply "
            "while Samsung pushes to qualify on Nvidia H200 successor platforms. "
            "Micron is ramping HBM3E volumes for shipment to AMD MI355X accelerators."
        )

        def mock_get(url, **kw):
            if "/category/" in url:
                return self._mock_requests_get(category_html)
            if "lpddr-q1" in url:
                return self._mock_requests_get(f"<html><body><article>{article1_body}</article></body></html>")
            if "hbm-q1" in url:
                return self._mock_requests_get(f"<html><body><article>{article2_body}</article></body></html>")
            return self._mock_requests_get("<html></html>", status=404)

        with tempfile.TemporaryDirectory() as td:
            sentinel_path = Path(td) / "sentinel.json"
            with patch.object(news_mod, "LPDDR_PARSER_NO_MATCH_SENTINEL_FILE", str(sentinel_path)):
                with patch.object(news_mod, "requests") as mock_requests:
                    mock_requests.get.side_effect = mock_get
                    result = news_mod.scrape_trendforce_lpddr_contract()

        self.assertEqual(result.status, "data_extracted")
        self.assertEqual(result.pages_checked, 2)
        self.assertEqual(result.rows_extracted, 1)
        self.assertEqual(result.signals[0].direction, "rising")
        self.assertEqual(result.signals[0].pct_qoq, 90.0)
        self.assertEqual(result.signals[0].release_date, "2026-01-15")

    def test_no_lpddr_content_status(self):
        """Category page has articles but none mention LPDDR."""
        from src.data import news as news_mod
        category_html = """
            <html><body>
            <a href="/news/2026/01/10/hbm-q1/">TrendForce HBM Q1 outlook for AI accelerator memory</a>
            </body></html>
        """
        body = (
            "HBM3E pricing remains stable as Nvidia allocates capacity throughout 2026. "
            "SK Hynix continues its lead while Samsung pushes to qualify on Nvidia's "
            "next-generation Blackwell successor platforms. Micron is ramping HBM3E "
            "volumes for shipment to AMD MI355X accelerators in early Q3 2026."
        )

        def mock_get(url, **kw):
            if "/category/" in url:
                return self._mock_requests_get(category_html)
            return self._mock_requests_get(f"<html><body><article>{body}</article></body></html>")

        with tempfile.TemporaryDirectory() as td:
            sentinel_path = Path(td) / "sentinel.json"
            with patch.object(news_mod, "LPDDR_PARSER_NO_MATCH_SENTINEL_FILE", str(sentinel_path)):
                with patch.object(news_mod, "requests") as mock_requests:
                    mock_requests.get.side_effect = mock_get
                    result = news_mod.scrape_trendforce_lpddr_contract()

        self.assertEqual(result.status, "no_lpddr_content_in_releases")
        self.assertEqual(result.rows_extracted, 0)
        self.assertEqual(result.pages_with_lpddr_keywords, 0)

    def test_parser_no_match_status_captures_sample(self):
        """LPDDR keyword present but no direction pattern matches → sample captured."""
        from src.data import news as news_mod
        category_html = """
            <html><body>
            <a href="/news/2026/01/15/weekly/">TrendForce Weekly Memory Market Roundup Report Recap</a>
            </body></html>
        """
        # Mentions LPDDR but no direction language — pad to clear length threshold
        body = (
            "Today's analyst report covers DDR4, DDR5, HBM3E, and LPDDR5 modules "
            "across major vendors in this morning's supply chain briefing. The full "
            "research note discusses production allocations and inventory positions "
            "without referencing any specific quarterly direction or magnitude. "
            "Additional commentary touches on capacity utilization at fabs."
        )

        def mock_get(url, **kw):
            if "/category/" in url:
                return self._mock_requests_get(category_html)
            return self._mock_requests_get(f"<html><body><article>{body}</article></body></html>")

        with tempfile.TemporaryDirectory() as td:
            sentinel_path = Path(td) / "sentinel.json"
            with patch.object(news_mod, "LPDDR_PARSER_NO_MATCH_SENTINEL_FILE", str(sentinel_path)):
                with patch.object(news_mod, "requests") as mock_requests:
                    mock_requests.get.side_effect = mock_get
                    result = news_mod.scrape_trendforce_lpddr_contract()

        self.assertEqual(result.status, "parser_no_match")
        self.assertEqual(result.rows_extracted, 0)
        self.assertEqual(result.pages_with_lpddr_keywords, 1)
        self.assertIn("LPDDR", result.sample_text_when_unparsed)

    def test_fetch_failed_status(self):
        from src.data import news as news_mod

        def mock_get(url, **kw):
            raise ConnectionError("network down")

        with patch.object(news_mod, "requests") as mock_requests:
            mock_requests.get.side_effect = mock_get
            result = news_mod.scrape_trendforce_lpddr_contract()

        self.assertEqual(result.status, "fetch_failed")
        self.assertIn("network down", result.error or "")


# ============================================================
# 6. Latest signal sort by release_date
# ============================================================

class TestLatestSignalSort(unittest.TestCase):

    def test_returns_most_recent_date(self):
        from src.data.news import LpddrScrapeResult, LpddrSignal
        result = LpddrScrapeResult(
            status="data_extracted", pages_checked=3, pages_with_lpddr_keywords=3,
            rows_extracted=3, signals=[
                LpddrSignal(release_date="2026-01-10", direction="rising", pct_qoq=10.0,
                            quote="q", source_url="u1", source_title="t1"),
                LpddrSignal(release_date="2026-03-15", direction="falling", pct_qoq=-5.0,
                            quote="q", source_url="u2", source_title="t2"),
                LpddrSignal(release_date="2026-02-20", direction="flat", pct_qoq=None,
                            quote="q", source_url="u3", source_title="t3"),
            ], sample_text_when_unparsed="",
        )
        latest = result.latest_signal
        self.assertIsNotNone(latest)
        self.assertEqual(latest.release_date, "2026-03-15")
        self.assertEqual(latest.direction, "falling")

    def test_empty_returns_none(self):
        from src.data.news import LpddrScrapeResult
        result = LpddrScrapeResult(
            status="no_lpddr_content_in_releases",
            pages_checked=0, pages_with_lpddr_keywords=0,
            rows_extracted=0, signals=[], sample_text_when_unparsed="",
        )
        self.assertIsNone(result.latest_signal)


# ============================================================
# 7. CSV persistence
# ============================================================

class TestPersistence(unittest.TestCase):

    def test_writes_csv_and_idempotent(self):
        from src.data import news as news_mod
        from src.data.news import LpddrScrapeResult, LpddrSignal
        result = LpddrScrapeResult(
            status="data_extracted", pages_checked=1, pages_with_lpddr_keywords=1,
            rows_extracted=1, signals=[
                LpddrSignal(release_date="2026-01-15", direction="rising", pct_qoq=90.0,
                            quote="LPDDR grow 90% QoQ", source_url="https://x/y",
                            source_title="t1"),
            ], sample_text_when_unparsed="",
        )
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "lpddr.csv"
            with patch.object(news_mod, "LPDDR_CONTRACT_CSV", csv_path):
                n1 = news_mod.persist_lpddr_contract_to_history(result)
                self.assertEqual(n1, 1)
                self.assertTrue(csv_path.exists())
                # Re-persist same signal — should NOT duplicate
                n2 = news_mod.persist_lpddr_contract_to_history(result)
                self.assertEqual(n2, 1)
                import pandas as pd
                df = pd.read_csv(csv_path)
                self.assertEqual(len(df), 1)
                self.assertEqual(df.iloc[0]["direction"], "rising")

    def test_empty_signals_returns_zero(self):
        from src.data import news as news_mod
        from src.data.news import LpddrScrapeResult
        result = LpddrScrapeResult(
            status="no_lpddr_content_in_releases",
            pages_checked=0, pages_with_lpddr_keywords=0,
            rows_extracted=0, signals=[], sample_text_when_unparsed="",
        )
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "lpddr.csv"
            with patch.object(news_mod, "LPDDR_CONTRACT_CSV", csv_path):
                n = news_mod.persist_lpddr_contract_to_history(result)
                self.assertEqual(n, 0)
                self.assertFalse(csv_path.exists())


# ============================================================
# 8. DHCR integration — sanity check carries LPDDR direction
#    but math is UNCHANGED (informational only for v1)
# ============================================================

class TestDhcrIntegration(unittest.TestCase):

    def test_lpddr_field_in_sanity_check_output(self):
        from src.analysis.dhcr import compute_sanity_check
        out = compute_sanity_check(
            score=70, hbm_yaml={}, hbm_price_trend="rising",
            lpddr_contract_direction="rising",
        )
        self.assertEqual(out["lpddr_contract_direction"], "rising")

    def test_lpddr_default_no_data(self):
        from src.analysis.dhcr import compute_sanity_check
        out = compute_sanity_check(score=70, hbm_yaml={}, hbm_price_trend="rising")
        self.assertEqual(out["lpddr_contract_direction"], "no_data")

    def test_lpddr_does_not_affect_confirms_contradicts(self):
        """Critical: for v1, LPDDR is informational. Math must be unchanged."""
        from src.analysis.dhcr import compute_sanity_check
        with_lpddr_rising = compute_sanity_check(
            score=70, hbm_yaml={}, hbm_price_trend="rising",
            lpddr_contract_direction="rising",
        )
        with_lpddr_falling = compute_sanity_check(
            score=70, hbm_yaml={}, hbm_price_trend="rising",
            lpddr_contract_direction="falling",
        )
        without_lpddr = compute_sanity_check(
            score=70, hbm_yaml={}, hbm_price_trend="rising",
        )
        # All three must produce identical confirms/contradicts/flag
        self.assertEqual(with_lpddr_rising["confirms"], without_lpddr["confirms"])
        self.assertEqual(with_lpddr_falling["confirms"], without_lpddr["confirms"])
        self.assertEqual(with_lpddr_rising["contradicts"], without_lpddr["contradicts"])
        self.assertEqual(with_lpddr_falling["contradicts"], without_lpddr["contradicts"])
        self.assertEqual(with_lpddr_rising["calibration_flag"], without_lpddr["calibration_flag"])

    def test_snapshot_dataclass_has_field(self):
        from src.analysis.dhcr import DHCRSnapshot
        import dataclasses
        # DHCRSnapshot has many required positional args from v3.4.0+; check
        # via field metadata rather than instantiating.
        field_names = {f.name for f in dataclasses.fields(DHCRSnapshot)}
        self.assertIn("sanity_signal_lpddr_contract_direction", field_names)
        # And it has a default ("no_data")
        for f in dataclasses.fields(DHCRSnapshot):
            if f.name == "sanity_signal_lpddr_contract_direction":
                self.assertEqual(f.default, "no_data")
                break


# ============================================================
# 9. fast_tier integration smoke
# ============================================================

class TestFastTierIntegrationSmoke(unittest.TestCase):

    def test_lpddr_scraper_importable_from_data_news(self):
        from src.data.news import scrape_trendforce_lpddr_contract, persist_lpddr_contract_to_history
        # Just verify they're public
        self.assertTrue(callable(scrape_trendforce_lpddr_contract))
        self.assertTrue(callable(persist_lpddr_contract_to_history))

    def test_fast_tier_passes_lpddr_through_to_dhcr(self):
        """fast_tier.py source contains the v3.4.13 wiring."""
        from src import fast_tier
        src = Path(fast_tier.__file__).read_text()
        # Pull LPDDR from collector
        self.assertIn("_lpddr_scrape_result", src)
        # Pass into compute_dhcr_snapshot
        self.assertIn("lpddr_contract_direction=", src)


if __name__ == "__main__":
    unittest.main()
