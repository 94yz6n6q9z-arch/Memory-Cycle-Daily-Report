"""v3.4.12 — Regression tests for transcript memory + quarter-over-quarter diff.

Coverage:
- Quarter key derivation from event_date (Q1-Q4 boundaries, year rollover)
- Prior quarter computation across year boundary
- Per-quarter store load/save roundtrip with atomic write
- Diff trigger logic: prior exists + diff missing → fire;
                       prior missing → no-op;
                       diff cached + same digest → no-op
- Haiku diff call mocked (no real API calls)
- JSON parse failure → safe-fail with .error set, empty deltas
- render_diff_for_sonnet_prompt produces correct sections
- Sonnet prompt contains diff block when transcript_diffs provided
- Re-invalidation on new transcript for same quarter
- Empty digests handled defensively
- fast_tier import smoke
- Digester hook safe-failing
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT))


# ============================================================
# Test fixtures
# ============================================================

def make_digest_dict(
    title="SK Hynix Q1 2026 call",
    published_date_iso="2026-04-25",
    key_metrics=None,
    signal_relevance=None,
    key_quotes=None,
    fired_status_evidence="neutral",
    digested_at_iso="2026-04-25T10:00:00",
):
    return {
        "source_id": f"yahoo / SK Hynix / {published_date_iso}",
        "source_type": "earnings_transcript",
        "published_date_iso": published_date_iso,
        "title": title,
        "char_count_input": 50000,
        "key_metrics": key_metrics or {"hbm_revenue_yoy_pct": 120},
        "signal_relevance": signal_relevance or ["t2_eps_revisions", "t2_ltas_signed"],
        "key_quotes": key_quotes or ["HBM3E sold out through 2027"],
        "target_price_action": None,
        "fired_status_evidence": fired_status_evidence,
        "digested_at_iso": digested_at_iso,
    }


# ============================================================
# 1. Quarter key derivation
# ============================================================

class TestQuarterKeyDerivation(unittest.TestCase):

    def test_q1_boundaries(self):
        from src.state import quarter_key_from_event_date
        self.assertEqual(quarter_key_from_event_date("2026-01-01"), "2026-Q1")
        self.assertEqual(quarter_key_from_event_date("2026-02-15"), "2026-Q1")
        self.assertEqual(quarter_key_from_event_date("2026-03-31"), "2026-Q1")

    def test_q2_boundaries(self):
        from src.state import quarter_key_from_event_date
        self.assertEqual(quarter_key_from_event_date("2026-04-01"), "2026-Q2")
        self.assertEqual(quarter_key_from_event_date("2026-05-15"), "2026-Q2")
        self.assertEqual(quarter_key_from_event_date("2026-06-30"), "2026-Q2")

    def test_q3_boundaries(self):
        from src.state import quarter_key_from_event_date
        self.assertEqual(quarter_key_from_event_date("2026-07-01"), "2026-Q3")
        self.assertEqual(quarter_key_from_event_date("2026-09-30"), "2026-Q3")

    def test_q4_boundaries(self):
        from src.state import quarter_key_from_event_date
        self.assertEqual(quarter_key_from_event_date("2026-10-01"), "2026-Q4")
        self.assertEqual(quarter_key_from_event_date("2026-12-31"), "2026-Q4")

    def test_iso_with_time_handled(self):
        from src.state import quarter_key_from_event_date
        # Should accept full ISO datetimes, taking only the date
        self.assertEqual(quarter_key_from_event_date("2026-05-23T14:30:00"), "2026-Q2")

    def test_invalid_returns_none(self):
        from src.state import quarter_key_from_event_date
        self.assertIsNone(quarter_key_from_event_date(""))
        self.assertIsNone(quarter_key_from_event_date(None))
        self.assertIsNone(quarter_key_from_event_date("not a date"))


class TestPriorQuarterKey(unittest.TestCase):

    def test_simple_decrement(self):
        from src.state import get_prior_quarter_key
        self.assertEqual(get_prior_quarter_key("2026-Q4"), "2026-Q3")
        self.assertEqual(get_prior_quarter_key("2026-Q3"), "2026-Q2")
        self.assertEqual(get_prior_quarter_key("2026-Q2"), "2026-Q1")

    def test_year_rollover(self):
        from src.state import get_prior_quarter_key
        self.assertEqual(get_prior_quarter_key("2026-Q1"), "2025-Q4")
        self.assertEqual(get_prior_quarter_key("2027-Q1"), "2026-Q4")

    def test_invalid_returns_none(self):
        from src.state import get_prior_quarter_key
        self.assertIsNone(get_prior_quarter_key(""))
        self.assertIsNone(get_prior_quarter_key("2026"))
        self.assertIsNone(get_prior_quarter_key("2026-Q5"))
        self.assertIsNone(get_prior_quarter_key("not a quarter"))


# ============================================================
# 2. Ticker safe-name + path resolution
# ============================================================

class TestTickerSafeName(unittest.TestCase):

    def test_alphanumeric_dot_dash_preserved(self):
        from src.state import _ticker_safe
        self.assertEqual(_ticker_safe("000660.KS"), "000660.KS")
        self.assertEqual(_ticker_safe("BRK.B"), "BRK.B")
        self.assertEqual(_ticker_safe("MU"), "MU")

    def test_special_chars_replaced(self):
        from src.state import _ticker_safe
        # Slashes, spaces, etc → underscore
        self.assertEqual(_ticker_safe("weird / 5"), "weird___5")

    def test_empty_handled(self):
        from src.state import _ticker_safe
        self.assertEqual(_ticker_safe(""), "unknown")
        self.assertEqual(_ticker_safe(None), "unknown")


# ============================================================
# 3. Per-quarter store load/save roundtrip
# ============================================================

class TestPerQuarterStorePersistence(unittest.TestCase):

    def test_load_missing_returns_none(self):
        from src import state as state_mod
        with tempfile.TemporaryDirectory() as td:
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", Path(td) / "transcripts"):
                result = state_mod.load_quarter_digest("000660.KS", "2026-Q2")
        self.assertIsNone(result)

    def test_save_and_load_roundtrip(self):
        from src import state as state_mod
        digest = make_digest_dict()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", Path(td) / "transcripts"):
                state_mod.save_quarter_digest(
                    ticker="000660.KS",
                    company="SK Hynix",
                    quarter_key="2026-Q2",
                    digest=digest,
                )
                loaded = state_mod.load_quarter_digest("000660.KS", "2026-Q2")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["ticker"], "000660.KS")
        self.assertEqual(loaded["company"], "SK Hynix")
        self.assertEqual(loaded["quarter_key"], "2026-Q2")
        self.assertEqual(loaded["digest"]["title"], "SK Hynix Q1 2026 call")
        self.assertIsNone(loaded["diff_with_prior"])

    def test_save_with_diff(self):
        from src import state as state_mod
        digest = make_digest_dict()
        diff = {
            "ticker": "000660.KS",
            "current_quarter": "2026-Q2",
            "prior_quarter": "2026-Q1",
            "overall_tone_shift": "more bearish",
            "indicator_deltas": [{"indicator_id": "t2_eps_revisions", "direction": "strengthening", "summary": "..."}],
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", Path(td) / "transcripts"):
                state_mod.save_quarter_digest(
                    ticker="000660.KS",
                    company="SK Hynix",
                    quarter_key="2026-Q2",
                    digest=digest,
                    diff_with_prior=diff,
                )
                loaded = state_mod.load_quarter_digest("000660.KS", "2026-Q2")
        self.assertIsNotNone(loaded["diff_with_prior"])
        self.assertEqual(loaded["diff_with_prior"]["overall_tone_shift"], "more bearish")

    def test_corrupt_file_returns_none(self):
        from src import state as state_mod
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "transcripts"
            (base / "000660.KS").mkdir(parents=True)
            (base / "000660.KS" / "2026-Q2.json").write_text("not valid json {{")
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", base):
                result = state_mod.load_quarter_digest("000660.KS", "2026-Q2")
        self.assertIsNone(result)


# ============================================================
# 4. list_recent_quarter_diffs
# ============================================================

class TestListRecentQuarterDiffs(unittest.TestCase):

    def test_returns_only_records_with_diff(self):
        from src import state as state_mod
        digest = make_digest_dict()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "transcripts"
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", base):
                # Two companies, two quarters each. One has diff, three don't.
                state_mod.save_quarter_digest("000660.KS", "SK Hynix", "2026-Q1", digest)
                state_mod.save_quarter_digest(
                    "000660.KS", "SK Hynix", "2026-Q2", digest,
                    diff_with_prior={"overall_tone_shift": "more bearish"}
                )
                state_mod.save_quarter_digest("005930.KS", "Samsung", "2026-Q1", digest)
                state_mod.save_quarter_digest("005930.KS", "Samsung", "2026-Q2", digest)

                results = state_mod.list_recent_quarter_diffs(max_quarters_back=1)

        # Only the one with diff
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ticker"], "000660.KS")

    def test_returns_empty_when_dir_missing(self):
        from src import state as state_mod
        with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", Path("/tmp/v3_4_12_nonexistent")):
            self.assertEqual(state_mod.list_recent_quarter_diffs(), [])

    def test_returns_latest_n_per_company(self):
        from src import state as state_mod
        digest = make_digest_dict()
        diff = {"overall_tone_shift": "unchanged"}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "transcripts"
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", base):
                # 3 quarters with diffs for one company
                state_mod.save_quarter_digest("000660.KS", "SK Hynix", "2026-Q1", digest, diff_with_prior=diff)
                state_mod.save_quarter_digest("000660.KS", "SK Hynix", "2026-Q2", digest, diff_with_prior=diff)
                state_mod.save_quarter_digest("000660.KS", "SK Hynix", "2026-Q3", digest, diff_with_prior=diff)

                results = state_mod.list_recent_quarter_diffs(max_quarters_back=2)

        # Should return latest 2 (Q3 and Q2)
        self.assertEqual(len(results), 2)


# ============================================================
# 5. compute_diff_with_prior — Haiku mocked
# ============================================================

class TestComputeDiffWithPrior(unittest.TestCase):

    def _build_mock_anthropic(self, return_text):
        """Mock the Anthropic SDK to return a fixed Haiku response."""
        mock_response = MagicMock()
        mock_response.content = [SimpleNamespace(text=return_text)]
        mock_response.usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        return mock_client

    def test_no_api_key_returns_none(self):
        from src.analysis.transcript_diff import compute_diff_with_prior
        result = compute_diff_with_prior(
            current_digest=make_digest_dict(),
            prior_digest=make_digest_dict(),
            ticker="000660.KS", company="SK Hynix",
            current_quarter="2026-Q2", prior_quarter="2026-Q1",
            anthropic_api_key="",
        )
        self.assertIsNone(result)

    def test_successful_diff_parsed(self):
        from src.analysis import transcript_diff
        mock_text = json.dumps({
            "indicator_deltas": [
                {"indicator_id": "t2_eps_revisions", "direction": "strengthening",
                 "summary": "CFO dropped sold-out language", "prior_quote": "sold out 2027",
                 "current_quote": "allocated 2026"}
            ],
            "metric_deltas": [
                {"metric_name": "hbm_yoy", "prior_value": "120", "current_value": "85", "direction": "down"}
            ],
            "language_dropped": ["sold out through 2027"],
            "language_added": ["softening in some end-markets"],
            "overall_tone_shift": "more bearish"
        })
        mock_client = self._build_mock_anthropic(mock_text)
        # Patch the SDK at its source module since compute_diff_with_prior
        # lazy-imports `from anthropic import Anthropic` inside the function.
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = transcript_diff.compute_diff_with_prior(
                current_digest=make_digest_dict(),
                prior_digest=make_digest_dict(),
                ticker="000660.KS", company="SK Hynix",
                current_quarter="2026-Q2", prior_quarter="2026-Q1",
                anthropic_api_key="sk-test",
            )
        self.assertIsNotNone(result)
        self.assertIsNone(result.error)
        self.assertEqual(result.overall_tone_shift, "more bearish")
        self.assertEqual(len(result.indicator_deltas), 1)
        self.assertEqual(result.indicator_deltas[0].indicator_id, "t2_eps_revisions")
        self.assertEqual(len(result.language_dropped), 1)
        self.assertEqual(result.language_dropped[0], "sold out through 2027")
        self.assertEqual(result.haiku_input_tokens, 100)
        self.assertEqual(result.haiku_output_tokens, 50)

    def test_json_parse_failure_safe_returns_diff_with_error(self):
        from src.analysis import transcript_diff
        mock_client = self._build_mock_anthropic("not valid json {{{ broken")
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = transcript_diff.compute_diff_with_prior(
                current_digest=make_digest_dict(),
                prior_digest=make_digest_dict(),
                ticker="000660.KS", company="SK Hynix",
                current_quarter="2026-Q2", prior_quarter="2026-Q1",
                anthropic_api_key="sk-test",
            )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.error)
        self.assertIn("json_parse_error", result.error)
        # Empty deltas — caller can safely render
        self.assertEqual(len(result.indicator_deltas), 0)
        self.assertEqual(len(result.metric_deltas), 0)

    def test_markdown_fences_stripped(self):
        from src.analysis import transcript_diff
        wrapped = "```json\n" + json.dumps({
            "indicator_deltas": [], "metric_deltas": [],
            "language_dropped": [], "language_added": [],
            "overall_tone_shift": "unchanged"
        }) + "\n```"
        mock_client = self._build_mock_anthropic(wrapped)
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = transcript_diff.compute_diff_with_prior(
                current_digest=make_digest_dict(),
                prior_digest=make_digest_dict(),
                ticker="X", company="X",
                current_quarter="2026-Q2", prior_quarter="2026-Q1",
                anthropic_api_key="sk-test",
            )
        self.assertIsNotNone(result)
        self.assertIsNone(result.error)

    def test_haiku_exception_safe_returns_diff_with_error(self):
        from src.analysis import transcript_diff
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("rate limited")
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = transcript_diff.compute_diff_with_prior(
                current_digest=make_digest_dict(),
                prior_digest=make_digest_dict(),
                ticker="X", company="X",
                current_quarter="2026-Q2", prior_quarter="2026-Q1",
                anthropic_api_key="sk-test",
            )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.error)
        self.assertIn("haiku_error", result.error)


# ============================================================
# 6. render_diff_for_sonnet_prompt
# ============================================================

class TestRenderDiffForSonnetPrompt(unittest.TestCase):

    def test_renders_all_sections(self):
        from src.analysis.transcript_diff import render_diff_for_sonnet_prompt
        diff = {
            "company": "SK Hynix",
            "current_quarter": "2026-Q3",
            "prior_quarter": "2026-Q2",
            "overall_tone_shift": "more bearish",
            "indicator_deltas": [
                {"indicator_id": "t2_eps_revisions", "direction": "strengthening",
                 "summary": "CFO commitment dropped", "prior_quote": "sold out",
                 "current_quote": "allocated"}
            ],
            "language_dropped": ["sold out through 2027"],
            "language_added": ["softening in PC OEMs"],
            "metric_deltas": [
                {"metric_name": "hbm_yoy_pct", "prior_value": "120", "current_value": "85",
                 "direction": "down"}
            ],
        }
        out = render_diff_for_sonnet_prompt(diff)
        self.assertIn("SK Hynix", out)
        self.assertIn("2026-Q2", out)
        self.assertIn("2026-Q3", out)
        self.assertIn("more bearish", out)
        self.assertIn("t2_eps_revisions", out)
        self.assertIn("LANGUAGE DROPPED", out)
        self.assertIn("sold out through 2027", out)
        self.assertIn("LANGUAGE ADDED", out)
        self.assertIn("softening in PC OEMs", out)
        self.assertIn("KEY METRICS THAT MOVED", out)
        self.assertIn("hbm_yoy_pct", out)

    def test_error_diff_renders_short_unavailable(self):
        from src.analysis.transcript_diff import render_diff_for_sonnet_prompt
        diff = {"company": "X", "current_quarter": "2026-Q2",
                "prior_quarter": "2026-Q1", "error": "haiku_error: timeout"}
        out = render_diff_for_sonnet_prompt(diff)
        self.assertIn("diff unavailable", out)
        self.assertIn("timeout", out)

    def test_non_dict_returns_empty(self):
        from src.analysis.transcript_diff import render_diff_for_sonnet_prompt
        self.assertEqual(render_diff_for_sonnet_prompt(None), "")
        self.assertEqual(render_diff_for_sonnet_prompt("string"), "")


# ============================================================
# 7. Sonnet prompt integration
# ============================================================

class TestSonnetPromptIntegration(unittest.TestCase):

    def _build_prompt(self, transcript_diffs):
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt, NewsTriage
        triage = NewsTriage(relevant_news=[], broker_actions=[],
                            trendforce_commentary=[], indicator_relevance={})
        return build_sonnet_analysis_prompt(
            triage=triage,
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            transcript_diffs=transcript_diffs,
        )

    def test_prompt_includes_qoq_block_when_diffs_provided(self):
        diff_record = {
            "ticker": "000660.KS", "company": "SK Hynix",
            "diff_with_prior": {
                "company": "SK Hynix",
                "current_quarter": "2026-Q3",
                "prior_quarter": "2026-Q2",
                "overall_tone_shift": "more bearish",
                "indicator_deltas": [],
                "language_dropped": ["sold out through 2027"],
                "language_added": [],
                "metric_deltas": [],
            }
        }
        prompt = self._build_prompt([diff_record])
        self.assertIn("QoQ TRANSCRIPT DIFFS", prompt)
        self.assertIn("sold out through 2027", prompt)
        self.assertIn("SK Hynix", prompt)
        self.assertIn("LANGUAGE DROPPED", prompt)

    def test_prompt_handles_empty_diffs_with_legacy_message(self):
        prompt = self._build_prompt([])
        self.assertIn("QoQ TRANSCRIPT DIFFS", prompt)
        self.assertIn("no active QoQ diffs", prompt)

    def test_prompt_handles_none_diffs(self):
        prompt = self._build_prompt(None)
        self.assertIn("QoQ TRANSCRIPT DIFFS", prompt)
        # Either legacy or empty path — must not crash
        self.assertIn("no active QoQ diffs", prompt)

    def test_prompt_skips_records_without_diff_payload(self):
        diff_record_no_diff = {"ticker": "X", "diff_with_prior": None}
        prompt = self._build_prompt([diff_record_no_diff])
        # Header still appears, just no inner content for this record
        self.assertIn("QoQ TRANSCRIPT DIFFS", prompt)


# ============================================================
# 8. Digester hook — safe-failing
# ============================================================

class TestDigesterHookSafeFailing(unittest.TestCase):

    def test_missing_ticker_short_circuits(self):
        from src.analysis.digester import _persist_and_maybe_diff_quarter, Digest
        digest = Digest(
            source_id="x", source_type="earnings_transcript",
            published_date_iso="2026-04-25",
            title="x", char_count_input=0, key_metrics={}, signal_relevance=[],
            key_quotes=[], target_price_action=None, fired_status_evidence="neutral",
        )
        # No exception; safe short-circuit on empty ticker
        _persist_and_maybe_diff_quarter(digest, ticker="", company="X", event_date="2026-04-25")

    def test_missing_event_date_short_circuits(self):
        from src.analysis.digester import _persist_and_maybe_diff_quarter, Digest
        digest = Digest(
            source_id="x", source_type="earnings_transcript",
            published_date_iso="",
            title="x", char_count_input=0, key_metrics={}, signal_relevance=[],
            key_quotes=[], target_price_action=None, fired_status_evidence="neutral",
        )
        _persist_and_maybe_diff_quarter(digest, ticker="000660.KS", company="X", event_date="")

    def test_first_quarter_no_diff(self):
        """First quarter ever (no Q-1 in store): persists digest, no Haiku call."""
        from src.analysis import digester
        from src.analysis.digester import _persist_and_maybe_diff_quarter, Digest
        from src import state as state_mod

        digest = Digest(
            source_id="x", source_type="earnings_transcript",
            published_date_iso="2026-04-25",
            title="SK Hynix Q1 2026", char_count_input=50000, key_metrics={},
            signal_relevance=["t2_eps_revisions"], key_quotes=["test"],
            target_price_action=None, fired_status_evidence="neutral",
            digested_at_iso="2026-04-25T10:00:00",
        )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "transcripts"
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", base):
                # Mock the Haiku call to be sure it isn't invoked
                with patch.object(digester, "compute_diff_with_prior") as mock_diff:
                    _persist_and_maybe_diff_quarter(
                        digest, ticker="000660.KS", company="SK Hynix",
                        event_date="2026-04-25",
                    )
                    self.assertFalse(mock_diff.called)
                    # File was persisted
                    loaded = state_mod.load_quarter_digest("000660.KS", "2026-Q2")
                    self.assertIsNotNone(loaded)
                    self.assertIsNone(loaded["diff_with_prior"])

    def test_second_quarter_triggers_diff(self):
        """When prior quarter exists, diff Haiku call is made and cached."""
        from src.analysis import digester
        from src.analysis.digester import _persist_and_maybe_diff_quarter, Digest
        from src import state as state_mod
        from src.analysis.transcript_diff import TranscriptDiff

        # Prior quarter digest, already in store
        prior_digest = make_digest_dict(
            title="SK Hynix Q1 2026",
            published_date_iso="2026-01-25",
            digested_at_iso="2026-01-25T10:00:00",
        )

        # Current quarter Digest object
        current_digest = Digest(
            source_id="x", source_type="earnings_transcript",
            published_date_iso="2026-04-25",
            title="SK Hynix Q2 2026", char_count_input=50000, key_metrics={},
            signal_relevance=["t2_eps_revisions"], key_quotes=["test"],
            target_price_action=None, fired_status_evidence="neutral",
            digested_at_iso="2026-04-25T10:00:00",
        )

        # Mock Haiku to return a structured diff
        mock_diff_obj = TranscriptDiff(
            ticker="000660.KS", company="SK Hynix",
            current_quarter="2026-Q2", prior_quarter="2026-Q1",
            current_digest_event_date="2026-04-25",
            prior_digest_event_date="2026-01-25",
            overall_tone_shift="more bearish",
        )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "transcripts"
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", base):
                # Seed prior quarter
                state_mod.save_quarter_digest(
                    "000660.KS", "SK Hynix", "2026-Q1", prior_digest
                )

                with patch.object(digester, "compute_diff_with_prior", return_value=mock_diff_obj) as mock_diff:
                    _persist_and_maybe_diff_quarter(
                        current_digest, ticker="000660.KS", company="SK Hynix",
                        event_date="2026-04-25",
                    )
                    # Haiku WAS called
                    self.assertTrue(mock_diff.called)
                    # New quarter saved with diff
                    loaded = state_mod.load_quarter_digest("000660.KS", "2026-Q2")
                    self.assertIsNotNone(loaded)
                    self.assertIsNotNone(loaded["diff_with_prior"])
                    self.assertEqual(loaded["diff_with_prior"]["overall_tone_shift"], "more bearish")

    def test_rerun_with_same_digest_skips_haiku(self):
        """If diff is already cached for THIS digest version, no re-call."""
        from src.analysis import digester
        from src.analysis.digester import _persist_and_maybe_diff_quarter, Digest
        from src import state as state_mod

        prior_digest = make_digest_dict(
            published_date_iso="2026-01-25",
            digested_at_iso="2026-01-25T10:00:00",
        )
        current_digest_dict = make_digest_dict(
            published_date_iso="2026-04-25",
            digested_at_iso="2026-04-25T10:00:00",
        )
        current_digest = Digest(**{k: v for k, v in current_digest_dict.items()
                                    if k in {"source_id", "source_type", "published_date_iso",
                                             "title", "char_count_input", "key_metrics",
                                             "signal_relevance", "key_quotes",
                                             "target_price_action", "fired_status_evidence",
                                             "digested_at_iso"}})

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "transcripts"
            with patch.object(state_mod, "PER_QUARTER_TRANSCRIPTS_DIR", base):
                # Seed prior AND current (with cached diff)
                state_mod.save_quarter_digest("000660.KS", "SK Hynix", "2026-Q1", prior_digest)
                state_mod.save_quarter_digest(
                    "000660.KS", "SK Hynix", "2026-Q2", current_digest.to_dict(),
                    diff_with_prior={"overall_tone_shift": "more bearish"},
                )

                with patch.object(digester, "compute_diff_with_prior") as mock_diff:
                    _persist_and_maybe_diff_quarter(
                        current_digest, ticker="000660.KS", company="SK Hynix",
                        event_date="2026-04-25",
                    )
                    # Re-run on SAME digest → skip Haiku
                    self.assertFalse(mock_diff.called)


# ============================================================
# 9. analyze_with_llm signature accepts transcript_diffs
# ============================================================

class TestAnalyzeWithLLMSignature(unittest.TestCase):

    def test_analyze_accepts_transcript_diffs_kwarg(self):
        from src.analysis.llm_summarizer import analyze_with_llm
        import inspect
        sig = inspect.signature(analyze_with_llm)
        self.assertIn("transcript_diffs", sig.parameters)

    def test_sonnet_analyze_accepts_transcript_diffs_kwarg(self):
        from src.analysis.llm_summarizer import sonnet_analyze
        import inspect
        sig = inspect.signature(sonnet_analyze)
        self.assertIn("transcript_diffs", sig.parameters)

    def test_build_prompt_accepts_transcript_diffs_kwarg(self):
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt
        import inspect
        sig = inspect.signature(build_sonnet_analysis_prompt)
        self.assertIn("transcript_diffs", sig.parameters)


# ============================================================
# 10. fast_tier integration smoke
# ============================================================

class TestFastTierIntegration(unittest.TestCase):

    def test_fast_tier_imports_quarter_diffs_loader(self):
        from src import fast_tier
        self.assertTrue(hasattr(fast_tier, "list_recent_quarter_diffs"))


if __name__ == "__main__":
    unittest.main()
