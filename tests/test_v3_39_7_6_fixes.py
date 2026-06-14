"""Regression tests for v3.39.7.6 fixes.

Four issues addressed in this release; one test class per issue:

  1. Exec summary rendered TWO blocks (old italic delta + new gray brief).
     Test: rendered HTML must contain the new brief but NOT the old
     italic delta block.

  2. t3_relative_strength's SOX comparison returned "(not computed)"
     because BENCHMARKS (^SOX, SMH, DRAM ETF) were never fetched.
     Test: ALL_TICKERS must include BENCHMARKS; given a price-history
     DataFrame with the right columns, evaluate_relative_strength_signal
     must produce a numeric spread (not None).

  3. Sonnet had no published timestamps and could not apply recency to
     contradicting news (May 21 brief wrote about a Samsung strike
     "commencing" when a later Korea Herald article reported it was
     called off). Test: the Sonnet prompt builder must include
     timestamps when news_items is passed AND the prompt must contain
     the "RECENCY ON CONTRADICTING EVENTS" rule.

  4. SEC EDGAR feeds for pre-listing SK Hynix filings, and forward-
     looking architecture Google News queries, rendered as WARN
     indistinguishable from real fetch failures. Test: the new
     `expected_empty` field on SourceFetchStatus produces health
     'quiet' when reached + 0 items + expected_empty=True; remains
     'warn' otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ============================================================
# Issue 1 — exec summary delta-line duplicate
# ============================================================

class TestIssue1ExecSummaryNoDuplicate:
    """Verify _build_executive_summary renders ONE block (the brief)."""

    def _make_summary(self, executive_brief: str, delta_text: str):
        from src.analysis.llm_summarizer import ExecutiveSummary, CaseScenario
        return ExecutiveSummary(
            current_phase=4,
            current_phase_label="Boom / Euphoria",
            tier_status_summary="T1: 4/6 | T2: 6/7 | T3: 0/5 | T4: 0/4",
            new_triggers_today=[],
            base_case=CaseScenario(
                probability_pct=46.0, peak_months_away=3.0,
                peak_window_start="August 2026", peak_window_end="October 2026",
                return_to_peak_pct=10.0,
            ),
            bull_case=CaseScenario(
                probability_pct=16.0, peak_months_away=11.0,
                peak_window_start="Q1 2027", peak_window_end="Q2 2027",
                return_to_peak_pct=45.0,
            ),
            bear_case=CaseScenario(
                probability_pct=38.0, peak_months_away=0.0,
                peak_window_start="May 2026", peak_window_end="May 2026",
                return_to_peak_pct=-38.0,
            ),
            delta_from_prior_text=delta_text,
            # v3.39.7.5 — urgent fields kept on dataclass for prior_state.json
            # back-compat, even though they no longer render. Parser sets
            # them to False/"" on the production path; the test mirrors that.
            urgent_attention_needed=False,
            urgent_attention_text="",
            executive_brief=executive_brief,
        )

    def test_executive_brief_renders_but_delta_does_not(self):
        from src.email_module.template import _build_executive_summary
        es = self._make_summary(
            executive_brief="THIS_IS_THE_BRIEF paragraph with consolidated content.",
            delta_text="THIS_IS_THE_DELTA — should NOT appear in rendered HTML.",
        )
        html_out = _build_executive_summary(es)
        assert "THIS_IS_THE_BRIEF" in html_out, "executive_brief must render"
        assert "THIS_IS_THE_DELTA" not in html_out, (
            "delta_from_prior_text MUST NOT render in v3.39.7.6 — it was the "
            "v3.39.7.5 regression that duplicated content with executive_brief."
        )

    def test_no_delta_text_div_class_in_output(self):
        from src.email_module.template import _build_executive_summary
        es = self._make_summary(
            executive_brief="brief text",
            delta_text="this would have been delta",
        )
        html_out = _build_executive_summary(es)
        assert "delta-text" not in html_out, (
            "The CSS class .delta-text is harmless to leave in the stylesheet "
            "but the div using it should NOT be emitted into rendered HTML."
        )

    def test_empty_executive_brief_renders_no_brief_block(self):
        """Defensive: if Sonnet ever fails to emit executive_brief, the
        exec summary should still render cleanly (phase + cases + nothing)."""
        from src.email_module.template import _build_executive_summary
        es = self._make_summary(executive_brief="", delta_text="delta would have been here")
        html_out = _build_executive_summary(es)
        assert "exec-brief" not in html_out
        assert "delta would have been here" not in html_out
        # Sanity: the phase line is still there
        assert "Phase" in html_out


# ============================================================
# Issue 2 — BENCHMARKS now fetched (SOX computable)
# ============================================================

class TestIssue2BenchmarksFetched:
    """The prices fetch now uses ALL_TICKERS, so SOX is in the columns
    and t3_relative_strength can compute the 5d spread."""

    def test_all_tickers_includes_benchmarks(self):
        from src.config import ALL_TICKERS, BENCHMARKS
        for benchmark_name, symbol in BENCHMARKS.items():
            assert benchmark_name in ALL_TICKERS, (
                f"ALL_TICKERS must include benchmark {benchmark_name!r}; "
                f"the fast_tier price fetch passes ALL_TICKERS as of v3.39.7.6, "
                f"and any missing benchmark would silently break the "
                f"relative-strength computation that depends on it."
            )
            assert ALL_TICKERS[benchmark_name] == symbol

    def test_sox_relative_strength_returns_numeric_when_sox_present(self):
        """When ^SOX is in the history columns (because ALL_TICKERS was
        fetched), get_relative_strength_vs_sox returns a number, not None."""
        from src.data.prices import get_relative_strength_vs_sox

        # 10 trading days of fake history; SK Hynix down -12%, SOX down -2%
        dates = pd.date_range("2026-05-01", periods=10, freq="B")
        skhx_prices = [100, 99, 98, 96, 95, 94, 92, 90, 89, 88]      # ≈ -12%
        sox_prices  = [100, 99.8, 99.5, 99.2, 99.0, 98.7, 98.5, 98.3, 98.1, 98.0]  # ≈ -2%
        history = pd.DataFrame({
            "SK Hynix": skhx_prices,
            "MSCI ACWI Semiconductor": sox_prices,  # benchmark name from config
        }, index=dates)

        spread = get_relative_strength_vs_sox("SK Hynix", history, days=5)
        assert spread is not None, (
            "v3.39.7.6 issue 2 regression: with SOX in the columns, the "
            "function must compute a numeric spread."
        )
        assert spread < 0, "SK Hynix underperforming SOX should yield negative spread"

    def test_fast_tier_uses_all_tickers_not_wider_basket(self):
        """The smoking-gun check: fast_tier.py:364 must pass ALL_TICKERS to
        fetch_price_history. The regression that shipped through v3.39.7.5
        passed only WIDER_BASKET, leaving BENCHMARKS unfetched."""
        from pathlib import Path
        src = Path("src/fast_tier.py").read_text()
        # The exact production call site we care about
        assert "fetch_price_history(ALL_TICKERS" in src, (
            "fast_tier.py must call fetch_price_history(ALL_TICKERS, ...) so "
            "BENCHMARKS get fetched. The pre-v3.39.7.6 call site used "
            "WIDER_BASKET, which silently broke SOX/SMH/DRAM ETF columns."
        )
        # And explicitly check the old broken call is GONE
        assert "fetch_price_history(WIDER_BASKET" not in src, (
            "fast_tier.py still has the old WIDER_BASKET call site — issue 2 "
            "fix incomplete."
        )


# ============================================================
# Issue 3 — Recency rule on contradicting news items
# ============================================================

class TestIssue3RecencyContradictingEvents:
    """build_sonnet_analysis_prompt must include the RECENCY rule and stamp
    triage items with published timestamps from news_items."""

    def _make_news_item(self, title: str, url: str, published_utc: datetime):
        from src.data.news import NewsItem
        return NewsItem(
            title=title,
            url=url,
            source="Korea Herald",
            published=published_utc,
            summary="test summary",
        )

    def _make_triage(self, items_dicts):
        from src.analysis.llm_summarizer import NewsTriage
        return NewsTriage(
            relevant_news=items_dicts,
            broker_actions=[],
            trendforce_commentary=[],
            indicator_relevance={},
        )

    def test_prompt_contains_recency_rule(self):
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt
        triage = self._make_triage([])
        prompt = build_sonnet_analysis_prompt(
            triage=triage,
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
        )
        # v3.41.1: the rule header was de-tagged/renamed from "RECENCY ON
        # CONTRADICTING EVENTS (v3.39.7.6)" to a neutral, investor-safe name to
        # stop Sonnet parroting the internal rule name into prose. The rule
        # itself is unchanged — assert on the new header AND its substance
        # (resolve same-event contradictions by published timestamp).
        assert "HANDLING CONFLICTING NEWS ON THE SAME EVENT" in prompt, (
            "issue 3 must add the timestamp-resolution rule block to the Sonnet "
            "prompt. Without it, Sonnet has no instruction to resolve the "
            "Samsung strike contradiction by timestamp."
        )
        assert "LATEST published timestamp" in prompt, (
            "the recency rule must still instruct resolution by the latest "
            "published timestamp."
        )

    def test_prompt_stamps_timestamps_when_news_items_provided(self):
        """When news_items is provided, each triage item must show its
        published timestamp in the prompt — joined by URL."""
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt

        url = "https://www.koreaherald.com/article/10742322"
        published = datetime(2026, 5, 20, 14, 17, 10, tzinfo=timezone.utc)
        news_items = [self._make_news_item(
            "Samsung Electronics union calls off strike",
            url, published,
        )]
        triage = self._make_triage([{
            "source": "Korea Herald",
            "title": "Samsung Electronics union calls off strike",
            "url": url,
            "summary": "Strike called off one hour before launch.",
        }])

        prompt = build_sonnet_analysis_prompt(
            triage=triage,
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            news_items=news_items,
        )
        # Timestamp format from the implementation: "%Y-%m-%d %H:%M UTC"
        assert "2026-05-20 14:17 UTC" in prompt, (
            "Triage item must be stamped with its published timestamp when "
            "news_items is provided. Without the stamp, the RECENCY rule "
            "cannot be applied by Sonnet — same as pre-v3.39.7.6."
        )

    def test_prompt_falls_back_to_question_mark_when_no_news_items(self):
        """Backward compatibility: when news_items is not provided, items
        render with '?' for timestamp (no crash, no error)."""
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt
        triage = self._make_triage([{
            "source": "Korea Herald",
            "title": "Some headline",
            "url": "https://example.com/article",
            "summary": "test",
        }])
        prompt = build_sonnet_analysis_prompt(
            triage=triage,
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            # no news_items
        )
        # The item still renders, but with '?' for timestamp
        assert "Some headline" in prompt
        assert "published ?" in prompt

    def test_prompt_joins_by_title_when_url_misses(self):
        """If Haiku triage emitted a different URL than the original
        NewsItem (rare), fall back to title-match."""
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt

        published = datetime(2026, 5, 21, 8, 0, 0, tzinfo=timezone.utc)
        news_items = [self._make_news_item(
            "Headline A", "https://orig.example.com/a", published,
        )]
        triage = self._make_triage([{
            "source": "Test",
            "title": "Headline A",
            "url": "https://different.example.com/a",  # different from original
            "summary": "x",
        }])
        prompt = build_sonnet_analysis_prompt(
            triage=triage,
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            news_items=news_items,
        )
        assert "2026-05-21 08:00 UTC" in prompt, (
            "Title-match fallback must kick in when URL doesn't match."
        )


# ============================================================
# Issue 4 — expected_empty distinction in diagnostics
# ============================================================

class TestIssue4ExpectedEmptyDiagnostics:
    """The new `expected_empty` field on SourceFetchStatus produces the
    'quiet' health state when reached + 0 items + expected_empty=True."""

    def test_status_health_quiet_when_expected_empty_and_zero_items(self):
        from src.diagnostics import SourceFetchStatus
        s = SourceFetchStatus(
            source_name="Google News: GDDR7 inference accelerator",
            source_type="google_news",
            reached=True,
            items_returned=0,
            latency_ms=300,
            expected_empty=True,
        )
        assert s.health == "quiet"

    def test_status_health_warn_when_not_expected_empty_and_zero_items(self):
        """Back-compat: a regular live feed returning 0 still WARNs."""
        from src.diagnostics import SourceFetchStatus
        s = SourceFetchStatus(
            source_name="Korea Herald Business",
            source_type="rss",
            reached=True,
            items_returned=0,
            latency_ms=500,
            expected_empty=False,  # default; explicit for clarity
        )
        assert s.health == "warn"

    def test_status_health_ok_overrides_expected_empty(self):
        """If a watcher feed DOES return items, that's ok regardless of
        the expected_empty flag — having items is always good."""
        from src.diagnostics import SourceFetchStatus
        s = SourceFetchStatus(
            source_name="Google News: CXL 3.0 memory pooling production",
            source_type="google_news",
            reached=True,
            items_returned=5,
            latency_ms=300,
            expected_empty=True,
        )
        assert s.health == "ok"

    def test_status_health_fail_overrides_expected_empty(self):
        """A watcher feed that fails (network error) is still fail, not
        quiet. expected_empty only affects the reached+0 case."""
        from src.diagnostics import SourceFetchStatus
        s = SourceFetchStatus(
            source_name="SEC EDGAR / SK Hynix F-1",
            source_type="edgar",
            reached=False,
            items_returned=0,
            latency_ms=0,
            error="HTTPError: 500",
            expected_empty=True,
        )
        assert s.health == "fail"

    def test_status_health_skipped_overrides_expected_empty(self):
        """Skipped is the highest-priority state."""
        from src.diagnostics import SourceFetchStatus
        s = SourceFetchStatus(
            source_name="Test source",
            source_type="rss",
            reached=False,
            items_returned=0,
            latency_ms=0,
            expected_empty=True,
            skipped=True,
        )
        assert s.health == "skipped"

    def test_summary_counts_includes_quiet(self):
        from src.diagnostics import DiagnosticsCollector, SourceFetchStatus
        c = DiagnosticsCollector()
        c.statuses = [
            SourceFetchStatus(source_name="a", source_type="rss",
                              reached=True, items_returned=3, latency_ms=100),
            SourceFetchStatus(source_name="b", source_type="google_news",
                              reached=True, items_returned=0, latency_ms=100,
                              expected_empty=True),
            SourceFetchStatus(source_name="c", source_type="google_news",
                              reached=True, items_returned=0, latency_ms=100,
                              expected_empty=True),
            SourceFetchStatus(source_name="d", source_type="rss",
                              reached=True, items_returned=0, latency_ms=100),
        ]
        counts = c.summary_counts()
        assert counts["ok"] == 1
        assert counts["quiet"] == 2
        assert counts["warn"] == 1
        assert counts["total"] == 4

    def test_track_handle_carries_expected_empty_into_status(self):
        """The collector's track() context manager carries expected_empty
        from the _TrackHandle into the recorded SourceFetchStatus."""
        from src.diagnostics import DiagnosticsCollector
        c = DiagnosticsCollector()
        with c.track("Test source", "google_news") as t:
            t.expected_empty = True
            t.items_returned = 0
        assert len(c.statuses) == 1
        assert c.statuses[0].expected_empty is True
        assert c.statuses[0].health == "quiet"

    def test_track_handle_default_not_expected_empty(self):
        """Default behavior unchanged: regular tracker → warn on 0 items."""
        from src.diagnostics import DiagnosticsCollector
        c = DiagnosticsCollector()
        with c.track("Test source", "rss") as t:
            t.items_returned = 0
        assert c.statuses[0].expected_empty is False
        assert c.statuses[0].health == "warn"

    def test_persisted_status_without_expected_empty_loads(self):
        """Back-compat: a status persisted by v3.39.7.5 (no expected_empty
        field) must load cleanly — the dataclass default kicks in."""
        from src.diagnostics import statuses_from_dict_list
        old_payload = [{
            "source_name": "old-format source",
            "source_type": "rss",
            "reached": True,
            "items_returned": 0,
            "latency_ms": 200,
            "error": None,
            "timestamp": "2026-05-20T12:00:00+00:00",
            "detail": None,
            "skipped": False,
            # No `expected_empty` key — pre-v3.39.7.6 format
        }]
        loaded = statuses_from_dict_list(old_payload)
        assert len(loaded) == 1
        assert loaded[0].expected_empty is False
        assert loaded[0].health == "warn"

    def test_watch_google_queries_includes_known_speculative_watchers(self):
        """Sanity check the config classification: speculative architecture
        watchers and SK Hynix pre-listing trackers are in the set."""
        from src.config import WATCH_GOOGLE_QUERIES, EXPECTED_EMPTY_EDGAR_FEEDS
        # Architecture watchers
        assert "CXL 3.0 memory pooling production" in WATCH_GOOGLE_QUERIES
        assert "GDDR7 inference accelerator" in WATCH_GOOGLE_QUERIES
        assert "HBF High Bandwidth Flash SanDisk" in WATCH_GOOGLE_QUERIES
        # AI lab listing watchers
        assert "OpenAI IPO filing" in WATCH_GOOGLE_QUERIES
        # SK Hynix pre-listing EDGAR feeds
        assert "SEC EDGAR / SK Hynix F-1 (registration)" in EXPECTED_EMPTY_EDGAR_FEEDS
        assert "SEC EDGAR / SK Hynix 20-F (annual)" in EXPECTED_EMPTY_EDGAR_FEEDS
        # Sanity: live news feeds and Micron Form 4 are NOT in the watch sets
        assert "SK Hynix earnings" not in WATCH_GOOGLE_QUERIES
        assert "Samsung union strike" not in WATCH_GOOGLE_QUERIES, (
            "Korean labor queries are live signal trackers, not speculative "
            "watchers — they should produce items most weeks."
        )
        assert "SEC EDGAR / Micron Form 4 (insider transactions)" not in EXPECTED_EMPTY_EDGAR_FEEDS, (
            "Micron files Form 4s regularly; 0 items would be legitimately "
            "suspicious."
        )
