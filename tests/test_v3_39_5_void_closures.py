"""Tests for v3.39.5 — void-closure architecture changes.

Two test surfaces:
  1. Google News publisher-URL extraction (`_extract_publisher_url_from_google_news_entry`)
     — fixes the broken-hyperlink problem where entry.link was returning the encoded
     redirect URL (news.google.com/rss/articles/CBMi*) that frontends reject with HTTP 400.

  2. Sonnet prompt now receives Layer 1 broker note digests
     — `build_sonnet_analysis_prompt(..., digests=...)` injects a DEEP-TIER BROKER NOTES
     section into the prompt with the broker as primary source, plus a precedence
     instruction telling Sonnet to cite the broker (not the news aggregator) when
     the same broker action shows up in both news triage and a deep-tier digest.

Replicates the exact production failure case from the 18:01 UTC May 18 brief, where:
  - The Mirae W3.2M target raise digest existed in state/digests/2026-05-18/ (broker_Mirae_...json)
  - But analyze_with_llm was called without `digests=`, so Sonnet never saw it
  - Sonnet's section 4 cited Seoul Economic and AD HOC NEWS (Korean news aggregators)
    that had echoed the Mirae report, instead of citing Mirae directly
  - Sonnet's section 5 "Latest news from the memory & semi market" omitted the Mirae
    raise entirely — the single biggest analyst action in 4 weeks on the largest
    portfolio holding."""
from __future__ import annotations

import types
import unittest

from src.data.news import _extract_publisher_url_from_google_news_entry
from src.analysis.llm_summarizer import (
    NewsTriage,
    build_sonnet_analysis_prompt,
)


def _entry(summary: str, link: str = "https://news.google.com/rss/articles/CBMiogFAU"):
    """Minimal feedparser-style entry surrogate. feedparser entries support
    both attribute and `.get(...)` access — using a dict wrapper that mimics
    feedparser.FeedParserDict's `.get()` method is the cleanest test fixture."""
    return {"summary": summary, "link": link, "title": "Test article"}


class TestExtractPublisherUrlFromGoogleNewsEntry(unittest.TestCase):
    """v3.39.5: Google News RSS entries embed the publisher URL in entry.summary
    as the first <a href> tag. Direct use of entry.link returns an encoded
    redirect that the Google News frontend rejects with HTTP 400."""

    def test_extracts_publisher_url_from_summary_html(self):
        # Realistic Google News entry HTML — the publisher href is the first <a> tag,
        # followed by a publisher-name <font> element.
        summary = (
            '<ol><li><a href="https://www.seouleconomicdaily.com/news/articleView'
            '?idxno=12345" target="_blank">Top Traders Dump Samsung, Pile Into SK '
            'Hynix on 3.2M Won Target</a>&nbsp;&nbsp;<font color="#6f6f6f">'
            'Seoul Economic</font></li></ol>'
        )
        result = _extract_publisher_url_from_google_news_entry(_entry(summary))
        self.assertEqual(
            result,
            "https://www.seouleconomicdaily.com/news/articleView?idxno=12345",
        )

    def test_extracts_from_adhocnews_realistic_html(self):
        # AD HOC NEWS — second broken-link case from the May 18 email.
        summary = (
            '<ol><li><a href="https://www.adhocnews.com/article/sk-hynix-targets-surge"'
            '>SK Hynix: Analyst Targets Surge on Structural Shift</a>&nbsp;&nbsp;'
            '<font color="#6f6f6f">AD HOC NEWS</font></li></ol>'
        )
        result = _extract_publisher_url_from_google_news_entry(_entry(summary))
        self.assertEqual(
            result, "https://www.adhocnews.com/article/sk-hynix-targets-surge"
        )

    def test_returns_none_when_summary_is_empty(self):
        result = _extract_publisher_url_from_google_news_entry(_entry(""))
        self.assertIsNone(result)

    def test_returns_none_when_summary_has_no_anchor_tag(self):
        # Some Google News entries return summary text without HTML anchors
        # (the article was indexed but has no surfaced source link). Caller
        # should fall back to entry.link.
        result = _extract_publisher_url_from_google_news_entry(
            _entry("Plain text summary with no anchor tags here.")
        )
        self.assertIsNone(result)

    def test_rejects_self_referential_google_news_href(self):
        # Edge: if Google News ever emits an inner <a> pointing back to itself,
        # we don't want to "extract" that URL — caller would fall back to entry.link
        # which is at least the same level of broken (rather than masquerading
        # as a fix).
        summary = (
            '<ol><li><a href="https://news.google.com/rss/articles/CBMiZZZ">'
            'Article title</a></li></ol>'
        )
        result = _extract_publisher_url_from_google_news_entry(_entry(summary))
        self.assertIsNone(result)

    def test_rejects_non_http_schemes(self):
        # Defense against weird hrefs like "javascript:void(0)" or "mailto:" —
        # these would fail loudly if blindly substituted into NewsItem.url.
        for href in ("javascript:void(0)", "mailto:editor@example.com", "/relative-path"):
            with self.subTest(href=href):
                summary = f'<ol><li><a href="{href}">Title</a></li></ol>'
                self.assertIsNone(
                    _extract_publisher_url_from_google_news_entry(_entry(summary))
                )

    def test_takes_first_anchor_when_multiple(self):
        # Google News occasionally emits multiple anchors (the main article +
        # a "view all coverage" link). The first one is the canonical article.
        summary = (
            '<ol><li><a href="https://primary.com/article">Title</a>&nbsp;&nbsp;'
            '<a href="https://news.google.com/search?q=related">View all coverage</a>'
            '</li></ol>'
        )
        result = _extract_publisher_url_from_google_news_entry(_entry(summary))
        self.assertEqual(result, "https://primary.com/article")


class TestSonnetPromptDigestThreading(unittest.TestCase):
    """v3.39.5: build_sonnet_analysis_prompt now accepts `digests=` and injects
    a DEEP-TIER BROKER NOTES section + primary-source precedence instruction.
    These tests assert the void-closure: when Mirae's digest is passed in,
    Sonnet's prompt contains the broker's analytical fields by name."""

    def _empty_triage(self) -> NewsTriage:
        return NewsTriage(
            relevant_news=[], broker_actions=[],
            trendforce_commentary=[], indicator_relevance={},
        )

    def _mirae_digest(self) -> dict:
        # Mirrors what src/analysis/digester.py would write to state/digests/
        # for the May 18 SK Hynix W3.2M raise note. Schema per Digest dataclass.
        return {
            "source_id": "mirae_2339906",
            "source_type": "korean_broker_pdf",
            "published_date_iso": "2026-05-18",
            "title": "SK Hynix (000660 KS/Buy)Reflecting additional NAND momentum",
            "raw_url": "https://en.miraeassetdaewoo.com/.../researchReportsView.jsp?messageId=2339906",
            "key_metrics": {
                "fy27_eps_revision_pct": 22.5,
                "fy27_eps_new": 76800,
                "fy27_eps_prior": 62700,
            },
            "signal_relevance": ["t2_eps_revisions", "t1_spot_decline"],
            "key_quotes": ["NAND ASP up mid-70% QoQ supports higher P/B multiple"],
            "target_price_action": {
                "broker": "Mirae Asset Securities",
                "ticker": "000660 KS",
                "new_pt": "W3,200,000",
                "prior_pt": "W2,700,000",
                "direction": "raised",
            },
            "fired_status_evidence": "supports_fire",
        }

    def test_digest_block_present_when_digests_passed(self):
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            digests=[self._mirae_digest()],
        )
        # Section header must be present
        self.assertIn("DEEP-TIER BROKER NOTES (PRIMARY SOURCE", prompt)
        # The broker's identity must appear by name in the prompt
        self.assertIn("Mirae Asset Securities", prompt)
        # The new target price must be in the prompt
        self.assertIn("W3,200,000", prompt)
        # Prior target — so Sonnet can compute the magnitude
        self.assertIn("W2,700,000", prompt)
        # The analytical excerpt
        self.assertIn("NAND ASP up mid-70% QoQ", prompt)

    def test_precedence_instruction_present(self):
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            digests=[self._mirae_digest()],
        )
        self.assertIn("PRIMARY-SOURCE PRECEDENCE", prompt)
        # The aggregators that broke the May 18 email are named explicitly in
        # the instruction so Sonnet doesn't drift back to citing them.
        self.assertIn("Seoul Economic", prompt)
        self.assertIn("AD HOC NEWS", prompt)
        # The fallback path is also instructed: ≥10% target moves must be in
        # key_news_summary even if news triage didn't see them.
        self.assertIn("MAJOR BROKER ACTIONS IN KEY NEWS", prompt)
        self.assertIn("≥10%", prompt)

    def test_digest_block_omitted_gracefully_when_no_digests(self):
        # Backward compatibility: call sites that don't pass digests yet
        # (e.g. fallback paths, test fixtures, anything mid-migration)
        # should not crash and should not inject empty section headers.
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            digests=None,
        )
        # Sentinel for legacy mode is present, but the broker-notes header
        # (which would imply real digests) is not.
        self.assertIn("legacy mode", prompt)
        self.assertNotIn("DEEP-TIER BROKER NOTES (PRIMARY SOURCE", prompt)

    def test_transcript_digest_renders_metrics(self):
        # Earnings transcript path: key_metrics like supplier_inventory_weeks
        # and architecture_signals must surface in the prompt — they're the
        # cycle-defining numbers (Tier 1 + Tier 2 signal feedstock).
        transcript_digest = {
            "source_id": "skhynix_q1_2026_call",
            "source_type": "earnings_transcript",
            "published_date_iso": "2026-04-22",
            "title": "SK Hynix Q1 2026 Earnings Call",
            "raw_url": "https://news.skhynix.com/q1-2026-business-results/",
            "key_metrics": {
                "supplier_inventory_weeks": 2.3,
                "asp_yoy_pct": 198.0,
                "architecture_signals": {
                    "hbm_capex_pct": 50,
                    "wafer_allocation_language": "favoring_hbm",
                },
            },
            "signal_relevance": ["t3_supplier_inventory_rebuild"],
            "key_quotes": ["DRAM ASP up mid-60% QoQ, NAND ASP up mid-70% QoQ"],
            "target_price_action": None,
            "fired_status_evidence": "supports_fire",
        }
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            digests=[transcript_digest],
        )
        self.assertIn("DEEP-TIER EARNINGS TRANSCRIPTS (PRIMARY SOURCE)", prompt)
        self.assertIn("supplier inventory: 2.3 weeks", prompt)
        self.assertIn("HBM/CapEx: 50%", prompt)
        self.assertIn("wafer: favoring_hbm", prompt)

    def test_mixed_digests_sorted_by_source_type(self):
        # When both broker notes and transcripts are present, both sections appear.
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            digests=[
                self._mirae_digest(),
                {
                    "source_id": "mu_q3_call",
                    "source_type": "earnings_transcript",
                    "published_date_iso": "2026-05-17",
                    "title": "Micron Q3 Earnings",
                    "raw_url": "https://...",
                    "key_metrics": {"supplier_inventory_weeks": 1.8},
                    "signal_relevance": [],
                    "key_quotes": [],
                    "target_price_action": None,
                    "fired_status_evidence": "supports_fire",
                },
            ],
        )
        # Order: broker notes block precedes transcripts block (matches
        # the prompt structure — broker primary sources are higher-priority
        # context for "what changed today")
        broker_idx = prompt.find("DEEP-TIER BROKER NOTES (PRIMARY SOURCE")
        transcript_idx = prompt.find("DEEP-TIER EARNINGS TRANSCRIPTS (PRIMARY SOURCE)")
        self.assertGreaterEqual(broker_idx, 0)
        self.assertGreaterEqual(transcript_idx, 0)
        self.assertLess(broker_idx, transcript_idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
