"""v3.4.10 — Regression tests for the Yahoo earnings-window cascade fix,
the Finnhub earnings-calendar cache, dead RSS drops, and the Bright Data
probe script.

The cascade fix replaces v3.33's "fire Yahoo for ANYTHING that isn't a
fresh full_transcript" rule with: skip Yahoo when outside the 2-day
post-earnings window. Korean ADR tickers (SK Hynix HY9H.MU,
Samsung SSU.F) bypass the gate entirely (Finnhub doesn't cover them by
ADR symbol; they fall back to Yahoo's own seen_urls cache).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


# Make project root importable
PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT))


# ============================================================
# Earnings-calendar cache refresh rules
# ============================================================

class TestEarningsCalendarCacheRefreshRules(unittest.TestCase):
    """v3.4.10 — `_cache_entry_needs_refresh` implements four rules. Each
    is tested in isolation so failures point at the specific rule that
    broke."""

    def setUp(self):
        from src.data.transcript_sources import _cache_entry_needs_refresh
        self.fn = _cache_entry_needs_refresh
        self.today = date(2026, 5, 23)

    def test_cache_miss_triggers_refresh(self):
        """Rule 1: no entry → refresh."""
        self.assertTrue(self.fn(None, self.today))

    def test_missing_fetched_at_triggers_refresh(self):
        """Defensive: corrupted entry with no fetched_at → refresh."""
        entry = {"earnings_date": "2026-04-15"}  # no fetched_at
        self.assertTrue(self.fn(entry, self.today))

    def test_age_over_7_days_triggers_refresh(self):
        """Rule 2: weekly heartbeat."""
        old_dt = datetime(2026, 5, 15, tzinfo=timezone.utc)  # 8 days ago
        entry = {
            "earnings_date": "2025-12-01",  # far past, would otherwise NOT trigger window rule
            "fetched_at": old_dt.isoformat(),
        }
        self.assertTrue(self.fn(entry, self.today))

    def test_within_30_day_window_triggers_refresh(self):
        """Rule 3: cached earnings_date within ±30 days of today → daily refresh."""
        fresh_dt = datetime(2026, 5, 22, 10, tzinfo=timezone.utc)  # 1 day ago
        # earnings_date is 20 days in the future (within ±30 window)
        entry = {
            "earnings_date": (self.today + timedelta(days=20)).isoformat(),
            "fetched_at": fresh_dt.isoformat(),
        }
        self.assertTrue(self.fn(entry, self.today))

    def test_within_30_days_past_triggers_refresh(self):
        """Rule 3 (past side): earnings happened 20 days ago, still in window."""
        fresh_dt = datetime(2026, 5, 22, 10, tzinfo=timezone.utc)
        entry = {
            "earnings_date": (self.today - timedelta(days=20)).isoformat(),
            "fetched_at": fresh_dt.isoformat(),
        }
        self.assertTrue(self.fn(entry, self.today))

    def test_post_call_window_triggers_refresh(self):
        """Rule 4: earnings 3 days ago AND cache fetched today → still refresh
        (catches Finnhub date stamp revisions post-call)."""
        fresh_dt = datetime(2026, 5, 23, 8, tzinfo=timezone.utc)
        entry = {
            "earnings_date": (self.today - timedelta(days=3)).isoformat(),
            "fetched_at": fresh_dt.isoformat(),
        }
        self.assertTrue(self.fn(entry, self.today))

    def test_stable_entry_outside_window_uses_cache(self):
        """No refresh: earnings 90 days ago, cache fetched 2 days ago, no
        upcoming earnings in next 30 days."""
        fresh_dt = datetime(2026, 5, 21, 10, tzinfo=timezone.utc)  # 2 days ago
        entry = {
            "earnings_date": (self.today - timedelta(days=90)).isoformat(),
            "fetched_at": fresh_dt.isoformat(),
        }
        self.assertFalse(self.fn(entry, self.today))

    def test_null_earnings_date_uses_cache_within_week(self):
        """Cached `earnings_date: null` (no upcoming earnings) → weekly only."""
        fresh_dt = datetime(2026, 5, 22, tzinfo=timezone.utc)
        entry = {
            "earnings_date": None,
            "fetched_at": fresh_dt.isoformat(),
        }
        self.assertFalse(self.fn(entry, self.today))

    def test_corrupted_earnings_date_string_triggers_refresh(self):
        """Defensive: bad ISO string → refresh (don't trust corrupt cache)."""
        fresh_dt = datetime(2026, 5, 22, tzinfo=timezone.utc)
        entry = {
            "earnings_date": "not-an-iso-date",
            "fetched_at": fresh_dt.isoformat(),
        }
        self.assertTrue(self.fn(entry, self.today))


# ============================================================
# Earnings-window gate
# ============================================================

class TestYahooEarningsWindowGate(unittest.TestCase):
    """v3.4.10 — `_is_in_yahoo_earnings_window(ticker)` returns True iff the
    most-recent past earnings was 0-2 days ago. Default-attempt semantics
    when data is missing."""

    def test_within_window_returns_true(self):
        """Earnings 1 day ago → in window."""
        with patch("src.data.transcript_sources._get_recent_earnings_date") as mock_get:
            mock_get.return_value = date.today() - timedelta(days=1)
            from src.data.transcript_sources import _is_in_yahoo_earnings_window
            self.assertTrue(_is_in_yahoo_earnings_window("ORCL"))

    def test_earnings_today_returns_true(self):
        """Day 0 (earnings today) → in window."""
        with patch("src.data.transcript_sources._get_recent_earnings_date") as mock_get:
            mock_get.return_value = date.today()
            from src.data.transcript_sources import _is_in_yahoo_earnings_window
            self.assertTrue(_is_in_yahoo_earnings_window("ORCL"))

    def test_earnings_2_days_ago_returns_true(self):
        """Day +2 (last day of window) → in window."""
        with patch("src.data.transcript_sources._get_recent_earnings_date") as mock_get:
            mock_get.return_value = date.today() - timedelta(days=2)
            from src.data.transcript_sources import _is_in_yahoo_earnings_window
            self.assertTrue(_is_in_yahoo_earnings_window("ORCL"))

    def test_earnings_3_days_ago_returns_true_v3_45_window(self):
        """Day +3 (first day OUT of window) → skip."""
        with patch("src.data.transcript_sources._get_recent_earnings_date") as mock_get:
            mock_get.return_value = date.today() - timedelta(days=3)
            from src.data.transcript_sources import _is_in_yahoo_earnings_window
            self.assertTrue(  # v3.45.0: window 2->7d
            _is_in_yahoo_earnings_window("ORCL"))

    def test_earnings_30_days_ago_returns_false(self):
        """Far past → skip."""
        with patch("src.data.transcript_sources._get_recent_earnings_date") as mock_get:
            mock_get.return_value = date.today() - timedelta(days=30)
            from src.data.transcript_sources import _is_in_yahoo_earnings_window
            self.assertFalse(_is_in_yahoo_earnings_window("ORCL"))

    def test_none_returns_true_default_attempt(self):
        """No Finnhub data → DEFAULT TO ATTEMPT (preserve coverage)."""
        with patch("src.data.transcript_sources._get_recent_earnings_date") as mock_get:
            mock_get.return_value = None
            from src.data.transcript_sources import _is_in_yahoo_earnings_window
            self.assertTrue(_is_in_yahoo_earnings_window("UNKNOWN_TICKER"))


# ============================================================
# Cascade integration — yahoo_to_run construction
# ============================================================

class TestCascadeYahooSelection(unittest.TestCase):
    """v3.4.10 — Verify the cascade's `yahoo_to_run` dict is built correctly
    given different MF/IR outcomes AND earnings-window states."""

    def _build_minimal_setup(self):
        """Return (yahoo_tickers, mf_results, ir_results) for tests."""
        from src.data.transcript_sources import TranscriptResult
        yahoo_tickers = {
            "Oracle": "ORCL",
            "Nvidia": "NVDA",
            "Micron": "MU",
            "SK Hynix": "HY9H.MU",
            "Samsung Electronics": "SSU.F",
        }
        # MF returned `already_processed` for Oracle (cached) and NVDA (cached),
        # nothing for Micron, doesn't cover Korean names
        mf_results = [
            TranscriptResult(
                company="Oracle", ticker="ORCL",
                event_date_iso="2026-03-10", event_type="earnings_call",
                availability="already_processed",
                source_name="Motley Fool",
            ),
            TranscriptResult(
                company="Nvidia", ticker="NVDA",
                event_date_iso="2026-02-15", event_type="earnings_call",
                availability="already_processed",
                source_name="Motley Fool",
            ),
            TranscriptResult(
                company="Micron", ticker="MU",
                event_date_iso=date.today().isoformat(), event_type="earnings_call",
                availability="nothing",
                source_name="Motley Fool",
            ),
        ]
        ir_results = []
        return yahoo_tickers, mf_results, ir_results

    def test_already_processed_outside_window_skips_yahoo(self):
        """The Oracle scenario from 2026-05-23: MF returned already_processed
        for ORCL, last earnings was ~74 days ago (March 10) → SKIP Yahoo.
        This is the core fix."""
        yahoo_tickers, mf_results, ir_results = self._build_minimal_setup()

        def fake_window(ticker, window_days=2):
            # Simulate Finnhub knowledge: ORCL last earnings 74 days ago (Mar 10)
            # NVDA last earnings 95 days ago (Feb 15)
            # MU last earnings today (in window)
            if ticker == "ORCL":
                return False
            if ticker == "NVDA":
                return False
            if ticker == "MU":
                return True
            return True  # default attempt

        with patch("src.data.transcript_sources._is_in_yahoo_earnings_window",
                   side_effect=fake_window), \
             patch("src.data.transcript_sources.fetch_motley_fool_transcripts",
                   return_value=mf_results), \
             patch("src.data.transcript_sources.fetch_ir_transcripts",
                   return_value=ir_results), \
             patch("src.data.transcript_sources.fetch_yahoo_finance_transcripts") as mock_yahoo, \
             patch("src.data.transcript_sources._load_seen_urls", return_value=set()), \
             patch("src.data.transcript_sources._save_seen_urls"):
            mock_yahoo.return_value = []
            from src.data.transcript_sources import fetch_all_transcripts
            fetch_all_transcripts({}, {}, yahoo_tickers, days_back=10)

            # Inspect what got passed to Yahoo
            self.assertEqual(mock_yahoo.call_count, 1,
                "fetch_yahoo_finance_transcripts must be called exactly once")
            yahoo_run_dict = mock_yahoo.call_args[0][0]
            yahoo_companies = set(yahoo_run_dict.keys())

            # Oracle SKIPPED (the v3.4.10 fix), NVDA SKIPPED, MU INCLUDED, both Korean INCLUDED
            self.assertNotIn("Oracle", yahoo_companies,
                "Oracle MUST be skipped: already_processed + outside earnings window")
            self.assertNotIn("Nvidia", yahoo_companies,
                "Nvidia MUST be skipped: already_processed + outside earnings window")
            self.assertIn("Micron", yahoo_companies,
                "Micron MUST run: in earnings window")
            self.assertIn("SK Hynix", yahoo_companies,
                "SK Hynix MUST run: international tickers bypass window check")
            self.assertIn("Samsung Electronics", yahoo_companies,
                "Samsung MUST run: international tickers bypass window check")

    def test_already_processed_within_window_runs_yahoo(self):
        """If a US ticker IS in its 2-day post-earnings window, even an
        already_processed cache state should trigger a Yahoo recheck —
        the cached URL might be the prior quarter; today's fresh call's
        transcript might be on Yahoo but not yet on MF."""
        from src.data.transcript_sources import TranscriptResult
        yahoo_tickers = {"Oracle": "ORCL"}
        mf_results = [TranscriptResult(
            company="Oracle", ticker="ORCL",
            event_date_iso="2026-03-10", event_type="earnings_call",
            availability="already_processed",
            source_name="Motley Fool",
        )]

        with patch("src.data.transcript_sources._is_in_yahoo_earnings_window",
                   return_value=True), \
             patch("src.data.transcript_sources.fetch_motley_fool_transcripts",
                   return_value=mf_results), \
             patch("src.data.transcript_sources.fetch_ir_transcripts",
                   return_value=[]), \
             patch("src.data.transcript_sources.fetch_yahoo_finance_transcripts") as mock_yahoo, \
             patch("src.data.transcript_sources._load_seen_urls", return_value=set()), \
             patch("src.data.transcript_sources._save_seen_urls"):
            mock_yahoo.return_value = []
            from src.data.transcript_sources import fetch_all_transcripts
            fetch_all_transcripts({}, {}, yahoo_tickers, days_back=10)

            yahoo_run_dict = mock_yahoo.call_args[0][0]
            self.assertIn("Oracle", yahoo_run_dict,
                "Oracle MUST run when within earnings window even with already_processed")

    def test_full_transcript_skips_yahoo_unconditionally(self):
        """If MF or IR captured a full_transcript THIS RUN, Yahoo is skipped
        even within the earnings window. Don't waste proxy bandwidth."""
        from src.data.transcript_sources import TranscriptResult
        yahoo_tickers = {"Oracle": "ORCL"}
        mf_results = [TranscriptResult(
            company="Oracle", ticker="ORCL",
            event_date_iso="2026-05-22", event_type="earnings_call",
            availability="full_transcript",
            text="x" * 15000,  # well above FULL_TRANSCRIPT_MIN_CHARS
            source_name="Motley Fool",
        )]

        with patch("src.data.transcript_sources._is_in_yahoo_earnings_window",
                   return_value=True), \
             patch("src.data.transcript_sources.fetch_motley_fool_transcripts",
                   return_value=mf_results), \
             patch("src.data.transcript_sources.fetch_ir_transcripts",
                   return_value=[]), \
             patch("src.data.transcript_sources.fetch_yahoo_finance_transcripts") as mock_yahoo, \
             patch("src.data.transcript_sources._load_seen_urls", return_value=set()), \
             patch("src.data.transcript_sources._save_seen_urls"):
            mock_yahoo.return_value = []
            from src.data.transcript_sources import fetch_all_transcripts
            fetch_all_transcripts({}, {}, yahoo_tickers, days_back=10)

            # When yahoo_to_run is empty, fetch_yahoo_finance_transcripts is NOT
            # called (cascade short-circuits at the `if yahoo_to_run:` check).
            # Either condition proves the skip:
            #   (a) mock_yahoo not called at all, OR
            #   (b) mock_yahoo called with empty dict
            if mock_yahoo.call_args is None:
                # Cascade correctly short-circuited
                self.assertEqual(mock_yahoo.call_count, 0)
            else:
                yahoo_run_dict = mock_yahoo.call_args[0][0]
                self.assertNotIn("Oracle", yahoo_run_dict,
                    "Oracle MUST be skipped when MF returned full_transcript this run")


# ============================================================
# Earnings-calendar cache I/O
# ============================================================

class TestEarningsCalendarCacheIO(unittest.TestCase):
    """File I/O for state/earnings_calendar_cache.json."""

    def test_load_missing_file_returns_empty(self):
        with patch("src.data.transcript_sources.EARNINGS_CACHE_PATH",
                   Path("/nonexistent/path/that/does/not/exist.json")):
            from src.data.transcript_sources import _load_earnings_cache
            self.assertEqual(_load_earnings_cache(), {})

    def test_load_corrupt_file_returns_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            tmp_path = f.name
        try:
            with patch("src.data.transcript_sources.EARNINGS_CACHE_PATH",
                       Path(tmp_path)):
                from src.data.transcript_sources import _load_earnings_cache
                self.assertEqual(_load_earnings_cache(), {})
        finally:
            os.unlink(tmp_path)

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "earnings_calendar_cache.json"
            data = {
                "ORCL": {
                    "earnings_date": "2026-06-09",
                    "fetched_at": "2026-05-23T15:00:00+00:00",
                    "source": "finnhub",
                }
            }
            with patch("src.data.transcript_sources.EARNINGS_CACHE_PATH", cache_path):
                from src.data.transcript_sources import _save_earnings_cache, _load_earnings_cache
                _save_earnings_cache(data)
                loaded = _load_earnings_cache()
                self.assertEqual(loaded, data)


# ============================================================
# Finnhub failure mode — don't poison cache
# ============================================================

class TestFinnhubFailureDoesNotPoisonCache(unittest.TestCase):
    """v3.4.10 — When Finnhub fetch fails, fall back to cached value
    rather than overwriting it with None."""

    def test_finnhub_exception_returns_cached_value(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "earnings_calendar_cache.json"
            cached_date_str = (date.today() - timedelta(days=10)).isoformat()
            initial_cache = {
                "ORCL": {
                    "earnings_date": cached_date_str,
                    "fetched_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
                    "source": "finnhub",
                }
            }
            with open(cache_path, "w") as f:
                json.dump(initial_cache, f)

            with patch("src.data.transcript_sources.EARNINGS_CACHE_PATH", cache_path), \
                 patch("src.data.finnhub.fetch_earnings_calendar_date",
                       side_effect=RuntimeError("Finnhub 500 error")):
                from src.data.transcript_sources import _get_recent_earnings_date
                result = _get_recent_earnings_date("ORCL")
                # Should return cached value despite Finnhub failure
                self.assertEqual(result, date.fromisoformat(cached_date_str))

            # Verify cache wasn't overwritten
            with open(cache_path) as f:
                post_cache = json.load(f)
            self.assertEqual(post_cache["ORCL"]["earnings_date"], cached_date_str,
                "Failed Finnhub call MUST NOT overwrite the cached earnings_date")


# ============================================================
# RSS feed drops (v3.4.10)
# ============================================================

class TestDeadRSSFeedsRemoved(unittest.TestCase):
    """v3.4.10 — Mistral News (404) and SemiAnalysis Free (Cloudflare 403)
    were dropped from RSS_FEEDS. Asserting absence guards against future
    re-add without a working URL."""

    def test_mistral_news_not_in_rss_feeds(self):
        from src.config import RSS_FEEDS
        self.assertNotIn("Mistral News", RSS_FEEDS,
            "Mistral News was dropped in v3.4.10 (404 on both /news/feed.xml "
            "and /news/rss/). Don't re-add without a working URL.")

    def test_semianalysis_free_not_in_rss_feeds(self):
        from src.config import RSS_FEEDS
        self.assertNotIn("SemiAnalysis Free", RSS_FEEDS,
            "SemiAnalysis Free was dropped in v3.4.10 (Cloudflare 403 beyond "
            "JA3). Don't re-add unless you find a working bypass.")


# ============================================================
# Bright Data probe script smoke test
# ============================================================

class TestBrightDataProbeScript(unittest.TestCase):
    """v3.4.10 — `scripts/probe_bright_data.py` smoke test. Verifies the
    script imports cleanly, has expected functions, and the probe ticker
    list includes the canary (ORCL)."""

    def test_script_exists(self):
        script = PROJ_ROOT / "scripts" / "probe_bright_data.py"
        self.assertTrue(script.exists(), "scripts/probe_bright_data.py must exist")

    def test_script_imports_cleanly(self):
        spec_path = PROJ_ROOT / "scripts" / "probe_bright_data.py"
        # Import via importlib so we don't pollute sys.modules permanently
        import importlib.util
        spec = importlib.util.spec_from_file_location("probe_bright_data", spec_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Sanity: exposes the functions we expect
        self.assertTrue(hasattr(module, "probe_one"))
        self.assertTrue(hasattr(module, "main"))
        self.assertTrue(hasattr(module, "PROBE_TICKERS"))

    def test_probe_tickers_include_canary(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "probe_bright_data",
            PROJ_ROOT / "scripts" / "probe_bright_data.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ticker_symbols = [t for _, t in module.PROBE_TICKERS]
        self.assertIn("ORCL", ticker_symbols,
            "ORCL must be in PROBE_TICKERS — it's the canary that triggered v3.4.10")
        self.assertGreaterEqual(len(module.PROBE_TICKERS), 5,
            "Probe should hit at least 5 tickers to distinguish systemic vs per-ticker issues")

    def test_no_proxy_url_returns_exit_code_2(self):
        """When RESIDENTIAL_PROXY_URL is unset, main() must return 2 (config error)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "probe_bright_data",
            PROJ_ROOT / "scripts" / "probe_bright_data.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {"RESIDENTIAL_PROXY_URL": ""}, clear=False):
            # Need to ensure it's empty since `clear=False` doesn't remove if set
            os.environ.pop("RESIDENTIAL_PROXY_URL", None)
            rc = module.main()
            self.assertEqual(rc, 2)

    def test_probe_workflow_yml_was_removed_v3_5_2(self):
        """v3.5.2 — probe_bright_data.yml was intentionally removed from the
        bundle as part of obsolete-workflow cleanup. This test documents
        that removal and prevents the file from creeping back in.
        The probe script itself (scripts/probe_bright_data.py) is retained
        for manual local execution if needed."""
        yml = PROJ_ROOT / ".github" / "workflows" / "probe_bright_data.yml"
        self.assertFalse(yml.exists(),
            "probe_bright_data.yml should NOT exist — removed in v3.5.2 as obsolete")


# ============================================================
# Finnhub earnings_calendar helper
# ============================================================

class TestFinnhubEarningsCalendarFetcher(unittest.TestCase):
    """v3.4.10 — `fetch_earnings_calendar_date(ticker)` returns the most
    recent past earnings date or None."""

    def test_returns_max_past_date(self):
        """Finnhub response with multiple past + future earnings → returns
        the most recent past one."""
        fake_response = {
            "earningsCalendar": [
                {"date": "2025-09-09", "symbol": "ORCL"},   # past
                {"date": "2025-12-08", "symbol": "ORCL"},   # past
                {"date": "2026-03-10", "symbol": "ORCL"},   # past (most recent)
                {"date": "2026-06-09", "symbol": "ORCL"},   # future
            ]
        }
        with patch("src.data.finnhub._get", return_value=fake_response):
            from src.data.finnhub import fetch_earnings_calendar_date
            today = date.today()
            result = fetch_earnings_calendar_date("ORCL")
            # Most recent past depends on today. The fake data includes
            # dates that should all be in the past relative to 2026-05-23.
            past_dates_in_fake = [
                date.fromisoformat(e["date"])
                for e in fake_response["earningsCalendar"]
                if date.fromisoformat(e["date"]) <= today
            ]
            expected = max(past_dates_in_fake) if past_dates_in_fake else None
            self.assertEqual(result, expected)

    def test_returns_none_when_no_past_earnings(self):
        """All earnings are in the future → return None."""
        future = (date.today() + timedelta(days=30)).isoformat()
        fake_response = {
            "earningsCalendar": [{"date": future, "symbol": "NEWCO"}],
        }
        with patch("src.data.finnhub._get", return_value=fake_response):
            from src.data.finnhub import fetch_earnings_calendar_date
            self.assertIsNone(fetch_earnings_calendar_date("NEWCO"))

    def test_returns_none_for_empty_calendar(self):
        """Finnhub returns an empty earningsCalendar list."""
        fake_response = {"earningsCalendar": []}
        with patch("src.data.finnhub._get", return_value=fake_response):
            from src.data.finnhub import fetch_earnings_calendar_date
            self.assertIsNone(fetch_earnings_calendar_date("NOTREAL"))

    def test_handles_malformed_date(self):
        """Defensive: corrupt date strings in response don't crash."""
        fake_response = {
            "earningsCalendar": [
                {"date": "not-a-date", "symbol": "ORCL"},
                {"date": None, "symbol": "ORCL"},
                {"date": "2026-03-10", "symbol": "ORCL"},
            ]
        }
        with patch("src.data.finnhub._get", return_value=fake_response):
            from src.data.finnhub import fetch_earnings_calendar_date
            today = date.today()
            result = fetch_earnings_calendar_date("ORCL")
            valid_date = date(2026, 3, 10)
            if valid_date <= today:
                self.assertEqual(result, valid_date)


if __name__ == "__main__":
    unittest.main()
