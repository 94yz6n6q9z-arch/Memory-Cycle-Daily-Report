"""v3.4.8 — Tests for selectivity + defensive parsing changes.

Background: v3.4.7 production crashed on 2026-05-23 with
  TypeError: BrokerAction.__init__() missing 1 required
             positional argument: 'source_url'
during Haiku triage. Root cause: v3.4.7 added 4 new lab blog scrapers
which pushed news volume to 139 items; Haiku's response truncated past
its output token cap; json_repair recovered a partial dict missing
required fields; BrokerAction construction crashed.

Fixes covered here:
  1. Drop HuggingFace Daily Papers from LAB_BLOG_SCRAPERS (redundant
     with arxiv we already ingest; biggest noise contributor)
  2. Strict architecture-relevance filter for lab blog titles
  3. Per-source max_items cap (default 5)
  4. Drop 5 Cloudflare-blocked feeds (Damnang + Investing.com x4) from
     RSS_FEEDS — curl-cffi didn't bypass them
  5. Defensive BrokerAction construction via _safe_construct_list
"""

import unittest


class TestStrictArchitectureRelevanceFilter(unittest.TestCase):
    """The v3.4.8 strict filter keeps model-release / architecture posts
    and drops partnership / policy / hiring / applied-research."""

    def test_model_release_with_verb_passes(self):
        from src.data.lab_blog_scrapers import _is_architecture_relevant
        for title in [
            "Introducing Claude 5",
            "Announcing Llama 4",
            "Gemini 2.5 Pro now available",
            "Sonnet 4.7 released today",
            "Mistral unveils new frontier model",
            "Llama 4 technical report",
        ]:
            self.assertTrue(_is_architecture_relevant(title),
                f"Should pass: {title!r}")

    def test_model_name_with_version_passes(self):
        from src.data.lab_blog_scrapers import _is_architecture_relevant
        for title in [
            "Claude 5 changes how we think about reasoning",
            "Gemini 2.5: deeper context",
            "DeepSeek V3 architecture overview",
        ]:
            self.assertTrue(_is_architecture_relevant(title),
                f"Should pass: {title!r}")

    def test_architecture_terms_pass(self):
        from src.data.lab_blog_scrapers import _is_architecture_relevant
        for title in [
            "Reducing KV cache memory with sparse attention",
            "Mixture-of-experts at scale",
            "Long-context inference optimization",
            "70B parameters with MoE routing",
            "FlashAttention v3 in production inference",
        ]:
            self.assertTrue(_is_architecture_relevant(title),
                f"Should pass: {title!r}")

    def test_noise_titles_filtered_out(self):
        from src.data.lab_blog_scrapers import _is_architecture_relevant
        for title in [
            "Widening the conversation on frontier AI",
            "Anthropic acquires Stainless",
            "KPMG integrates Claude across its core business",
            "Our partnership with Salesforce",
            "Building responsible AI",
            "Engineering culture at Meta AI",
            "Hiring research engineers in London",
            "AI policy panel at Davos",
            "How we measure model safety",
        ]:
            self.assertFalse(_is_architecture_relevant(title),
                f"Should filter out: {title!r}")

    def test_empty_or_short_titles_filtered(self):
        from src.data.lab_blog_scrapers import _is_architecture_relevant
        for title in ["", "   ", "TBD"]:
            self.assertFalse(_is_architecture_relevant(title),
                f"Should filter out: {title!r}")


class TestParserAppliesFilter(unittest.TestCase):
    """The three HTML parsers apply _is_architecture_relevant inline."""

    def test_anthropic_parser_filters_noise(self):
        from src.data.lab_blog_scrapers import parse_anthropic_news
        html = """
        <a href="https://www.anthropic.com/news/claude-5">May 22, 2026Announcements Introducing Claude 5</a>
        <a href="https://www.anthropic.com/news/partnership">May 22, 2026Announcements Anthropic and KPMG announce partnership</a>
        <a href="https://www.anthropic.com/news/policy">May 22, 2026Policy AI policy at Davos panel discussion</a>
        """
        items = parse_anthropic_news(html)
        titles = [it.title for it in items]
        self.assertIn("Introducing Claude 5", titles)
        # Noise items filtered out
        self.assertNotIn("Anthropic and KPMG announce partnership", titles)
        self.assertNotIn("AI policy at Davos panel discussion", titles)

    def test_meta_parser_filters_noise(self):
        from src.data.lab_blog_scrapers import parse_meta_ai_blog
        html = """
        <div>
          <a href="https://ai.meta.com/blog/llama-4/">Introducing Llama 4</a>
          <span>April 08, 2026</span>
        </div>
        <div>
          <a href="https://ai.meta.com/blog/applied/">How Reels uses AI for ranking</a>
          <span>April 09, 2026</span>
        </div>
        """
        items = parse_meta_ai_blog(html)
        titles = [it.title for it in items]
        self.assertIn("Introducing Llama 4", titles)
        self.assertNotIn("How Reels uses AI for ranking", titles)


class TestMaxItemsCap(unittest.TestCase):
    """v3.4.8 — per-source cap defaults to 5; orchestrator enforces it."""

    def test_max_items_default_is_5(self):
        from src.data.lab_blog_scrapers import LabBlogConfig, parse_anthropic_news
        cfg = LabBlogConfig(name="x", url="https://x", parser=parse_anthropic_news)
        self.assertEqual(cfg.max_items, 5)

    def test_orchestrator_caps_at_max_items(self):
        from src.data.lab_blog_scrapers import (
            fetch_lab_blog_scrapes, LabBlogConfig,
        )
        from src.data.news import NewsItem
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, patch

        # Fake parser that returns 8 items all dated today (recent)
        now = datetime.now(timezone.utc)
        def fake_parser(_html: str):
            return [
                NewsItem(title=f"Claude {i+1} released",  # passes filter
                         url=f"https://x/{i}",
                         source="anthropic.com",
                         published=now,
                         summary=None)
                for i in range(8)
            ]

        cfg = LabBlogConfig(name="Test", url="https://x", parser=fake_parser, max_items=3)
        mock_resp = MagicMock()
        mock_resp.text = "<html></html>"
        mock_resp.raise_for_status = MagicMock()

        with patch("src.data.lab_blog_scrapers.smart_get", return_value=mock_resp):
            items = fetch_lab_blog_scrapes(hours_back=24, configs=[cfg])
        self.assertEqual(len(items), 3,
            "Orchestrator should cap at cfg.max_items=3 even if parser returns 8")


class TestHuggingFacePapersRemoved(unittest.TestCase):
    """v3.4.8 — HF Daily Papers no longer in active LAB_BLOG_SCRAPERS."""

    def test_hf_papers_not_in_active_scrapers(self):
        from src.data.lab_blog_scrapers import LAB_BLOG_SCRAPERS
        active_urls = [c.url for c in LAB_BLOG_SCRAPERS]
        for url in active_urls:
            self.assertNotIn("huggingface.co/api/daily_papers", url,
                "HF Daily Papers must be removed from LAB_BLOG_SCRAPERS per v3.4.8")
            self.assertNotIn("huggingface.co/papers", url)

    def test_parser_still_exists_for_future_reactivation(self):
        """The parse_huggingface_daily_papers function is kept (just
        unregistered) so it can be re-enabled later without rewriting."""
        from src.data import lab_blog_scrapers
        self.assertTrue(hasattr(lab_blog_scrapers, "parse_huggingface_daily_papers"))


class TestCloudflareFeedsDropped(unittest.TestCase):
    """v3.4.8 — 5 CF-blocked feeds removed from RSS_FEEDS based on
    v3.4.7 production confirming curl-cffi didn't bypass them."""

    def test_damnang_not_in_feeds(self):
        from src.config import RSS_FEEDS
        for name in list(RSS_FEEDS.keys()):
            self.assertFalse("damnang" in name.lower(),
                f"Damnang feed should be dropped in v3.4.8: found {name!r}")

    def test_investing_company_feeds_dropped(self):
        from src.config import RSS_FEEDS
        # The general "Investing.com Asia Equities" feed stays;
        # the per-company ones (MU/NVDA/AMAT/ASML) are CF-blocked
        for name in ["Investing.com MU news", "Investing.com NVDA news",
                     "Investing.com AMAT news", "Investing.com ASML news"]:
            self.assertNotIn(name, RSS_FEEDS,
                f"{name} must be removed (CF blocked even with curl-cffi)")

    def test_investing_asia_general_feed_kept(self):
        from src.config import RSS_FEEDS
        # The general Asia Equities feed wasn't CF-blocked; keep it
        self.assertIn("Investing.com Asia Equities", RSS_FEEDS)


class TestSafeConstructList(unittest.TestCase):
    """v3.4.8 — Defensive dataclass construction tolerates incomplete
    dicts from json_repair recovery of truncated Haiku output."""

    def test_complete_entries_constructed_normally(self):
        from src.analysis.llm_summarizer import _safe_construct_list, BrokerAction
        items = [
            {"ticker": "005930", "broker": "Mirae", "action": "buy",
             "detail": "Upgrade", "source_url": "https://x.com/1"},
            {"ticker": "000660", "broker": "Goldman", "action": "hold",
             "detail": "Neutral", "source_url": "https://x.com/2"},
        ]
        result = _safe_construct_list(BrokerAction, items, "broker_actions")
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], BrokerAction)
        self.assertEqual(result[0].ticker, "005930")

    def test_missing_required_field_skipped_with_warning(self):
        """The exact 2026-05-23 production crash: source_url missing."""
        from src.analysis.llm_summarizer import _safe_construct_list, BrokerAction
        items = [
            {"ticker": "005930", "broker": "Mirae", "action": "buy",
             "detail": "Upgrade", "source_url": "https://x.com/1"},
            # This is the truncated entry that crashed v3.4.7 production
            {"ticker": "000660", "broker": "Goldman", "action": "hold",
             "detail": "Neutral"},  # missing source_url
        ]
        result = _safe_construct_list(BrokerAction, items, "broker_actions")
        # The well-formed one is kept, the malformed one is skipped
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ticker, "005930")

    def test_non_dict_entries_skipped(self):
        from src.analysis.llm_summarizer import _safe_construct_list, BrokerAction
        items = [
            {"ticker": "X", "broker": "Y", "action": "buy",
             "detail": "z", "source_url": "https://x"},
            "not a dict — could happen if json_repair returns a string",
            None,
            42,
        ]
        result = _safe_construct_list(BrokerAction, items, "broker_actions")
        self.assertEqual(len(result), 1)

    def test_empty_list_returns_empty(self):
        from src.analysis.llm_summarizer import _safe_construct_list, BrokerAction
        self.assertEqual(_safe_construct_list(BrokerAction, [], "x"), [])

    def test_all_malformed_returns_empty(self):
        from src.analysis.llm_summarizer import _safe_construct_list, BrokerAction
        items = [{"ticker": "X"}, {"broker": "Y"}, {}]
        result = _safe_construct_list(BrokerAction, items, "broker_actions")
        self.assertEqual(result, [])


class TestHaikuTriagePipelineRobustness(unittest.TestCase):
    """End-to-end: the haiku_triage_news return-block (line 577-area)
    no longer crashes when broker_actions has malformed entries."""

    def test_partial_recovery_does_not_crash(self):
        """Simulate a json_repair recovery that returned an incomplete
        broker_action. Pre-v3.4.8 this crashed haiku_triage_news.
        Post-v3.4.8 it logs and continues with what's salvageable."""
        from src.analysis.llm_summarizer import _safe_construct_list, BrokerAction, NewsTriage
        parsed = {
            "relevant_news": [{"i": 0, "summary": "Foo"}, {"i": 1, "summary": "Bar"}],
            "broker_actions": [
                {"ticker": "005930", "broker": "Mirae", "action": "buy",
                 "detail": "Upgrade", "source_url": "https://x/1"},
                {"ticker": "000660", "broker": "Goldman", "action": "hold"},  # incomplete
            ],
            "trendforce_commentary": [],
            "indicator_relevance": {},
        }
        # This is the exact construction pattern from haiku_triage_news
        result = NewsTriage(
            relevant_news=parsed.get("relevant_news", []),
            broker_actions=_safe_construct_list(
                BrokerAction, parsed.get("broker_actions", []), "broker_actions"),
            trendforce_commentary=parsed.get("trendforce_commentary", []),
            indicator_relevance=parsed.get("indicator_relevance", {}),
        )
        self.assertEqual(len(result.relevant_news), 2)
        self.assertEqual(len(result.broker_actions), 1)  # 1 kept, 1 skipped


if __name__ == "__main__":
    unittest.main()
