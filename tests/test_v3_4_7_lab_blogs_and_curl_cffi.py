"""v3.4.7 tests — lab blog HTML scrapers + curl-cffi integration.

Validates:
- Anthropic / Meta AI / DeepMind parsers on representative HTML
- Date extraction across the different date formats each blog uses
- Filtering of UI strings ("Learn More", "FEATURED", "Newsroom", etc.)
- Deduplication on anchor href (same post appearing multiple times)
- Item attribution: source field set to the canonical lab-blog hostname
  so _classify_source_for_tier routes to Tier A
- smart_get() switches between curl_cffi and requests based on availability
- Exception types from BOTH stdlib requests and curl_cffi are caught
  cleanly in _safe_parse_rss_feed
- Section 4 ordering test: DHCR (6) → Competitor (7) → ADR (8)
"""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch


class TestAnthropicNewsParser(unittest.TestCase):

    def test_basic_extraction(self):
        from src.data.lab_blog_scrapers import parse_anthropic_news
        html = """
        <a href="https://www.anthropic.com/news/widening-conversation-ai">May 19, 2026Announcements Widening the conversation on frontier AI</a>
        <a href="https://www.anthropic.com/news/anthropic-kpmg">May 19, 2026Announcements KPMG integrates Claude across its core business</a>
        """
        items = parse_anthropic_news(html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].source, "anthropic.com")
        self.assertEqual(items[0].published.date(), datetime(2026, 5, 19).date())
        self.assertIn("frontier AI", items[0].title)

    def test_relative_url_normalized(self):
        from src.data.lab_blog_scrapers import parse_anthropic_news
        html = '<a href="/news/test-slug">May 1, 2026Announcements Test Title</a>'
        items = parse_anthropic_news(html)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].url.startswith("https://www.anthropic.com/"))

    def test_newsroom_link_skipped(self):
        from src.data.lab_blog_scrapers import parse_anthropic_news
        html = '<a href="/news">Newsroom</a><a href="https://www.anthropic.com/news/">Newsroom</a>'
        items = parse_anthropic_news(html)
        self.assertEqual(items, [])

    def test_dedup_by_url(self):
        from src.data.lab_blog_scrapers import parse_anthropic_news
        html = """
        <a href="https://www.anthropic.com/news/x">May 1, 2026Announcements Title</a>
        <a href="https://www.anthropic.com/news/x">May 1, 2026Announcements Title</a>
        """
        items = parse_anthropic_news(html)
        self.assertEqual(len(items), 1)


class TestMetaAIBlogParser(unittest.TestCase):

    def test_basic_extraction(self):
        from src.data.lab_blog_scrapers import parse_meta_ai_blog
        html = """
        <div>
          <a href="https://ai.meta.com/blog/scaling-how-we-build-test-advanced-ai/">Scaling How We Build and Test Our Most Advanced AI</a>
          <span>April 08, 2026</span>
        </div>
        """
        items = parse_meta_ai_blog(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "ai.meta.com")
        self.assertEqual(items[0].published.date(), datetime(2026, 4, 8).date())

    def test_ui_string_filtered(self):
        from src.data.lab_blog_scrapers import parse_meta_ai_blog
        html = """
        <div>
          <a href="https://ai.meta.com/blog/abc/">FEATURED</a>
          <span>April 08, 2026</span>
        </div>
        """
        items = parse_meta_ai_blog(html)
        self.assertEqual(items, [])

    def test_blog_index_skipped(self):
        from src.data.lab_blog_scrapers import parse_meta_ai_blog
        html = """
        <a href="https://ai.meta.com/blog">Blog Index</a>
        """
        items = parse_meta_ai_blog(html)
        self.assertEqual(items, [])


class TestDeepMindBlogParser(unittest.TestCase):

    def test_basic_extraction(self):
        from src.data.lab_blog_scrapers import parse_deepmind_blog
        html = """
        <a href="https://deepmind.google/blog/co-scientist-a-multi-agent-ai-partner/">
          <h3>Co-Scientist: A multi-agent AI partner to accelerate research</h3>
          <span>May 2026 Science</span>
        </a>
        """
        items = parse_deepmind_blog(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "deepmind.google")
        self.assertEqual(items[0].published.year, 2026)
        self.assertEqual(items[0].published.month, 5)

    def test_relative_url_normalized(self):
        from src.data.lab_blog_scrapers import parse_deepmind_blog
        html = """
        <a href="/blog/test-slug/">
          <h3>Test Title</h3>
          <span>March 2026</span>
        </a>
        """
        items = parse_deepmind_blog(html)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].url.startswith("https://deepmind.google/"))


class TestFetchOrchestrator(unittest.TestCase):
    """fetch_lab_blog_scrapes wires through smart_get + records SourceFetchStatus."""

    def test_records_diagnostics_per_blog(self):
        from src.data.lab_blog_scrapers import (
            fetch_lab_blog_scrapes, LabBlogConfig, parse_anthropic_news,
        )
        from src.diagnostics import DiagnosticsCollector

        mock_resp = MagicMock()
        mock_resp.text = '<a href="/news/x">May 22, 2026Announcements Recent Item</a>'
        mock_resp.raise_for_status = MagicMock()

        cfgs = [LabBlogConfig(
            name="Anthropic Test", url="https://www.anthropic.com/news",
            parser=parse_anthropic_news,
        )]
        collector = DiagnosticsCollector()
        with patch("src.data.lab_blog_scrapers.smart_get", return_value=mock_resp):
            items = fetch_lab_blog_scrapes(
                hours_back=168 * 4,  # 4 weeks so May 22 item not stale-filtered
                collector=collector,
                configs=cfgs,
            )
        self.assertEqual(len(items), 1)
        self.assertEqual(len(collector.statuses), 1)
        self.assertEqual(collector.statuses[0].health, "ok")
        self.assertEqual(collector.statuses[0].items_returned, 1)

    def test_recency_filter_applied(self):
        from src.data.lab_blog_scrapers import (
            fetch_lab_blog_scrapes, LabBlogConfig, parse_anthropic_news,
        )

        old_date_str = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%b %-d, %Y")
        mock_resp = MagicMock()
        # Item is 60 days old; default hours_back=36 should filter it out
        mock_resp.text = f'<a href="/news/x">{old_date_str}Announcements Old Item</a>'
        mock_resp.raise_for_status = MagicMock()

        cfgs = [LabBlogConfig(
            name="Test", url="https://example.com",
            parser=parse_anthropic_news,
        )]
        with patch("src.data.lab_blog_scrapers.smart_get", return_value=mock_resp):
            items = fetch_lab_blog_scrapes(hours_back=36, configs=cfgs)
        self.assertEqual(items, [])


class TestCurlCffiAvailable(unittest.TestCase):
    """curl_cffi was pulled in via yfinance pre-v3.4.7. v3.4.7 makes
    the dependency explicit and uses it in smart_get()."""

    def test_curl_cffi_importable(self):
        try:
            import curl_cffi  # noqa: F401
            from curl_cffi import requests as cffi_requests  # noqa: F401
        except ImportError:
            self.fail("curl_cffi missing — added in v3.4.7 for TLS-fingerprint bypass")

    def test_smart_get_uses_curl_cffi_when_available(self):
        from src.data import news as news_module
        # _HAS_CURL_CFFI should be True in this env
        self.assertTrue(news_module._HAS_CURL_CFFI)
        # smart_get function exists
        self.assertTrue(callable(news_module.smart_get))

    def test_exception_type_unions_include_both(self):
        from src.data import news as news_module
        import requests
        # Both stdlib HTTPError and curl_cffi HTTPError should be in the
        # union tuple so the _parse() catch handles either client.
        self.assertIn(requests.HTTPError, news_module._HTTPError_Types)
        if news_module._HAS_CURL_CFFI:
            from curl_cffi.requests import exceptions as cffi_exc
            self.assertIn(cffi_exc.HTTPError, news_module._HTTPError_Types)


class TestSection4Ordering(unittest.TestCase):
    """v3.4.7 — Section 4 ordering: top-5 → DHCR (6) → Competitor (7)
    → ADR (8). Test scans the template source for the ordering of the
    three card-building helper invocations."""

    def test_dhcr_before_competitor_before_adr(self):
        path = "src/email_module/template.py"
        with open(path) as f:
            src = f.read()
        # Find positions of each card builder call
        dhcr_pos = src.find("_build_dhcr_section4_card(")
        comp_pos = src.find("_build_competitor_section4_card(")
        adr_pos = src.find("_build_adr_section4_card(")
        # All must be present
        self.assertGreater(dhcr_pos, 0, "DHCR card builder not found")
        self.assertGreater(comp_pos, 0, "Competitor card builder not found")
        self.assertGreater(adr_pos, 0, "ADR card builder not found")
        # v3.4.7 ordering: DHCR before Competitor before ADR
        self.assertLess(dhcr_pos, comp_pos,
            "DHCR (card 6) must be invoked before Competitor (card 7) in template")
        self.assertLess(comp_pos, adr_pos,
            "Competitor (card 7) must be invoked before ADR (card 8 = last) in template")


class TestHuggingFaceDailyPapersJSON(unittest.TestCase):
    """v3.4.7.1 — HF Papers via public JSON endpoint (no auth required)."""

    def test_basic_extraction(self):
        from src.data.lab_blog_scrapers import parse_huggingface_daily_papers
        sample = """[
          {
            "paper": {
              "id": "2508.18124",
              "title": "CMPhysBench: A Benchmark for Evaluating LLMs in Condensed Matter Physics",
              "authors": [{"name": "Weida Wang"}],
              "summary": "CMPhysBench evaluates LLMs in condensed matter physics.",
              "publishedAt": "2026-05-22T10:30:00.000Z"
            },
            "upvotes": 15
          },
          {
            "paper": {
              "id": "2508.18200",
              "title": "DeepSeek V4 Technical Report",
              "authors": [{"name": "DeepSeek-AI"}],
              "summary": "We present DeepSeek V4, a frontier MoE model.",
              "publishedAt": "2026-05-22T12:00:00Z"
            },
            "upvotes": 220
          }
        ]"""
        items = parse_huggingface_daily_papers(sample)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].source, "huggingface.co/papers")
        self.assertTrue(items[0].url.startswith("https://huggingface.co/papers/"))
        self.assertEqual(items[0].published.date(), datetime(2026, 5, 22).date())

    def test_malformed_json_returns_empty(self):
        from src.data.lab_blog_scrapers import parse_huggingface_daily_papers
        self.assertEqual(parse_huggingface_daily_papers("not json"), [])
        self.assertEqual(parse_huggingface_daily_papers("{}"), [])
        self.assertEqual(parse_huggingface_daily_papers(""), [])

    def test_missing_fields_skipped(self):
        from src.data.lab_blog_scrapers import parse_huggingface_daily_papers
        sample = """[
          {"paper": {"id": "no-title"}},
          {"paper": {"title": "no-id"}},
          {},
          {"paper": null}
        ]"""
        items = parse_huggingface_daily_papers(sample)
        self.assertEqual(items, [])

    def test_huggingface_papers_routes_to_tier_a(self):
        """The source attribution 'huggingface.co/papers' must route to
        the 'huggingface_papers' tier label in the digester."""
        from src.analysis.digester import _classify_source_for_tier
        self.assertEqual(
            _classify_source_for_tier("scraper", "huggingface.co/papers"),
            "huggingface_papers"
        )


class TestDiagnosticVisibility(unittest.TestCase):
    """v3.4.7.1 — Per-feed client tag and startup log line so v3.4.8
    restore decisions for Damnang + Investing.com x4 are evidence-based."""

    def test_client_name_module_attribute(self):
        from src.data import news as news_module
        self.assertIn(news_module.CLIENT_NAME, ("curl_cffi", "stdlib"))

    def test_client_name_matches_has_curl_cffi(self):
        from src.data import news as news_module
        if news_module._HAS_CURL_CFFI:
            self.assertEqual(news_module.CLIENT_NAME, "curl_cffi")
        else:
            self.assertEqual(news_module.CLIENT_NAME, "stdlib")

    def test_rss_fetch_tags_client_in_detail(self):
        """End-to-end: a successful RSS fetch records the client tag in
        SourceFetchStatus.detail, which renders in the email's
        'Detail / error' column."""
        from src.data.news import _safe_parse_rss_feed, CLIENT_NAME
        from src.diagnostics import DiagnosticsCollector
        from unittest.mock import MagicMock, patch

        # Mock a successful RSS response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.text = ""
        mock_resp.content = (
            b'<?xml version="1.0"?><rss><channel>'
            b'<item><title>Test</title><link>https://x.com/1</link>'
            b'<pubDate>Thu, 22 May 2026 12:00:00 +0000</pubDate></item>'
            b'</channel></rss>'
        )
        mock_resp.raise_for_status = MagicMock()

        collector = DiagnosticsCollector()
        with patch("src.data.news.smart_get", return_value=mock_resp):
            _safe_parse_rss_feed(
                source_name="Test Feed",
                feed_url="https://example.com/feed.xml",
                cutoff=datetime(2020, 1, 1, tzinfo=timezone.utc),
                source_type="rss",
                collector=collector,
            )
        self.assertEqual(len(collector.statuses), 1)
        self.assertEqual(collector.statuses[0].detail, f"client={CLIENT_NAME}")


class TestRestoredCloudflareFeeds(unittest.TestCase):
    """v3.4.7.1 — Damnang Substack + Investing.com x4 restored to
    RSS_FEEDS after being dropped in v3.38.4. They were dropped
    because Cloudflare blocked Python requests' TLS fingerprint;
    curl-cffi Chrome120 impersonation should bypass."""

    def test_damnang_substack_in_feeds(self):
        from src.config import RSS_FEEDS
        self.assertIn("Damnang Substack (memory architecture)", RSS_FEEDS)

    def test_investing_x4_in_feeds(self):
        from src.config import RSS_FEEDS
        for name in (
            "Investing.com MU news",
            "Investing.com NVDA news",
            "Investing.com AMAT news",
            "Investing.com ASML news",
        ):
            self.assertIn(name, RSS_FEEDS, f"{name} should be restored in v3.4.7.1")



if __name__ == "__main__":
    unittest.main()
