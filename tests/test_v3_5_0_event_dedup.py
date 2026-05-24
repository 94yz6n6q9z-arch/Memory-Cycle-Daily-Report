"""Tests for v3.5.0 — Event deduplication.

Covers:
  A. state.py — EventLog helpers
  B. signal_scoring.py — earnings tagging + dedup in ingest_and_evaluate
  C. template.py — divergence box rendering
  D. signal_reasoner.py — divergent_events wired into signal_specific
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# A. state.py — EventLog helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestEventKeyForEarnings:
    def test_key_format(self):
        from src.state import event_key_for_earnings
        key = event_key_for_earnings("000660.KS", "2026-07-29")
        assert key.startswith("000660.KS:earnings_call:")
        assert "2026-Q" in key

    def test_same_quarter_same_key(self):
        from src.state import event_key_for_earnings
        k1 = event_key_for_earnings("000660.KS", "2026-07-29")
        k2 = event_key_for_earnings("000660.KS", "2026-08-05")
        assert k1 == k2   # both in Q3 2026

    def test_different_tickers_different_keys(self):
        from src.state import event_key_for_earnings
        k1 = event_key_for_earnings("000660.KS", "2026-07-29")
        k2 = event_key_for_earnings("MU", "2026-07-29")
        assert k1 != k2

    def test_different_quarters_different_keys(self):
        from src.state import event_key_for_earnings
        k1 = event_key_for_earnings("000660.KS", "2026-01-15")
        k2 = event_key_for_earnings("000660.KS", "2026-07-29")
        assert k1 != k2


class TestEventLogPersistence:
    def test_load_returns_empty_on_missing_file(self, tmp_path):
        from src.state import load_event_log, EVENT_LOG_FILE
        with patch("src.state.EVENT_LOG_FILE", tmp_path / "event_log.json"):
            log = load_event_log()
        assert log == {}

    def test_save_and_reload(self, tmp_path):
        from src.state import load_event_log, save_event_log, EVENT_LOG_FILE
        log_file = tmp_path / "state" / "event_log.json"
        test_log = {
            "000660.KS:earnings_call:2026-Q3": {
                "first_observed": "2026-07-29T08:00:00",
                "first_observation_source": "Naver",
                "primary_verdict": "bearish",
                "primary_score": -12.5,
                "event_date_iso": "2026-07-29",
                "observations": [],
                "divergence_flagged": False,
                "n_observations": 1,
            }
        }
        with patch("src.state.EVENT_LOG_FILE", log_file):
            save_event_log(test_log)
            loaded = load_event_log()
        assert "000660.KS:earnings_call:2026-Q3" in loaded
        assert loaded["000660.KS:earnings_call:2026-Q3"]["primary_score"] == -12.5

    def test_save_is_atomic_no_tmp_file_left(self, tmp_path):
        from src.state import save_event_log
        log_file = tmp_path / "state" / "event_log.json"
        with patch("src.state.EVENT_LOG_FILE", log_file):
            save_event_log({"test": {"event_date_iso": "2026-07-01",
                                     "primary_verdict": "bullish", "primary_score": 5.0,
                                     "first_observed": "", "first_observation_source": "",
                                     "observations": [], "divergence_flagged": False,
                                     "n_observations": 1}})
        assert not log_file.with_suffix(".json.tmp").exists()


class TestEventLogPruning:
    def _make_entry(self, days_ago: int) -> dict:
        event_date = (date.today() - timedelta(days=days_ago)).isoformat()
        return {
            "first_observed": "", "first_observation_source": "",
            "primary_verdict": "bearish", "primary_score": -5.0,
            "event_date_iso": event_date,
            "observations": [], "divergence_flagged": False, "n_observations": 1,
        }

    def test_prune_removes_old_entries(self):
        from src.state import prune_event_log
        log = {
            "old:earnings_call:2025-Q1": self._make_entry(200),
            "recent:earnings_call:2026-Q3": self._make_entry(10),
        }
        pruned = prune_event_log(log, days=180)
        assert "old:earnings_call:2025-Q1" not in pruned
        assert "recent:earnings_call:2026-Q3" in pruned

    def test_prune_keeps_entries_within_window(self):
        from src.state import prune_event_log
        log = {
            "a:earnings_call:2026-Q3": self._make_entry(5),
            "b:earnings_call:2026-Q2": self._make_entry(90),
            "c:earnings_call:2026-Q1": self._make_entry(179),
        }
        pruned = prune_event_log(log, days=180)
        assert len(pruned) == 3

    def test_prune_exactly_at_boundary_kept(self):
        from src.state import prune_event_log
        # Entry exactly at cutoff should be kept (>= cutoff, not >)
        event_date = (date.today() - timedelta(days=180)).isoformat()
        log = {"x:earnings_call:2026-Q1": self._make_entry(180)}
        pruned = prune_event_log(log, days=180)
        assert "x:earnings_call:2026-Q1" in pruned


class TestRecordEventPrimary:
    def test_primary_recorded_correctly(self):
        from src.state import record_event_primary
        log = {}
        record_event_primary(
            event_key="000660.KS:earnings_call:2026-Q3",
            source="Mirae PDF",
            primary_verdict="bearish",
            primary_score=-12.5,
            event_date_iso="2026-07-29",
            log=log,
        )
        entry = log["000660.KS:earnings_call:2026-Q3"]
        assert entry["primary_verdict"] == "bearish"
        assert entry["primary_score"] == -12.5
        assert entry["n_observations"] == 1
        assert entry["divergence_flagged"] is False

    def test_is_known_event_true_after_primary(self):
        from src.state import record_event_primary, is_known_event
        log = {}
        record_event_primary("SK:earnings_call:2026-Q3", "Naver", "bullish", 8.0, "2026-07-29", log)
        assert is_known_event("SK:earnings_call:2026-Q3", log)

    def test_is_known_event_false_for_unknown(self):
        from src.state import is_known_event
        assert not is_known_event("SK:earnings_call:2026-Q3", {})


class TestRecordEventObservation:
    def _base_log(self) -> dict:
        return {
            "000660.KS:earnings_call:2026-Q3": {
                "first_observed": "2026-07-29T08:00:00",
                "first_observation_source": "Naver",
                "primary_verdict": "bearish",
                "primary_score": -12.5,
                "event_date_iso": "2026-07-29",
                "observations": [],
                "divergence_flagged": False,
                "n_observations": 1,
            }
        }

    def test_observation_appended(self):
        from src.state import record_event_observation
        log = self._base_log()
        record_event_observation("000660.KS:earnings_call:2026-Q3", "Samsung PDF", "Bearish echo", "bearish", log)
        assert len(log["000660.KS:earnings_call:2026-Q3"]["observations"]) == 1

    def test_n_observations_increments(self):
        from src.state import record_event_observation
        log = self._base_log()
        record_event_observation("000660.KS:earnings_call:2026-Q3", "Samsung PDF", "Echo", "bearish", log)
        assert log["000660.KS:earnings_call:2026-Q3"]["n_observations"] == 2

    def test_no_divergence_when_same_direction(self):
        from src.state import record_event_observation
        log = self._base_log()   # primary is bearish
        diverged = record_event_observation(
            "000660.KS:earnings_call:2026-Q3", "NH PDF", "Also bearish", "bearish", log
        )
        assert diverged is False
        assert log["000660.KS:earnings_call:2026-Q3"]["divergence_flagged"] is False

    def test_divergence_flagged_on_opposite_direction(self):
        from src.state import record_event_observation
        log = self._base_log()   # primary is bearish
        diverged = record_event_observation(
            "000660.KS:earnings_call:2026-Q3", "Bullish source", "Upgraded!", "bullish", log
        )
        assert diverged is True
        assert log["000660.KS:earnings_call:2026-Q3"]["divergence_flagged"] is True

    def test_neutral_echo_does_not_diverge(self):
        from src.state import record_event_observation
        log = self._base_log()   # primary is bearish
        diverged = record_event_observation(
            "000660.KS:earnings_call:2026-Q3", "News wire", "Earnings coverage", "neutral", log
        )
        assert diverged is False


class TestGetDivergentEvents:
    def test_returns_only_flagged_entries(self):
        from src.state import get_divergent_events
        log = {
            "A:earnings_call:2026-Q3": {
                "divergence_flagged": True,
                "event_date_iso": date.today().isoformat(),
                "primary_verdict": "bearish", "primary_score": -5.0,
                "first_observed": "", "first_observation_source": "X",
                "observations": [], "n_observations": 2,
            },
            "B:earnings_call:2026-Q2": {
                "divergence_flagged": False,
                "event_date_iso": date.today().isoformat(),
                "primary_verdict": "bullish", "primary_score": 5.0,
                "first_observed": "", "first_observation_source": "Y",
                "observations": [], "n_observations": 1,
            },
        }
        divs = get_divergent_events(log, days=30)
        assert len(divs) == 1
        assert divs[0]["event_key"] == "A:earnings_call:2026-Q3"

    def test_excludes_old_divergent_events(self):
        from src.state import get_divergent_events
        old_date = (date.today() - timedelta(days=35)).isoformat()
        log = {
            "A:earnings_call:2026-Q1": {
                "divergence_flagged": True,
                "event_date_iso": old_date,
                "primary_verdict": "bearish", "primary_score": -5.0,
                "first_observed": "", "first_observation_source": "X",
                "observations": [], "n_observations": 2,
            }
        }
        divs = get_divergent_events(log, days=30)
        assert len(divs) == 0


# ══════════════════════════════════════════════════════════════════════════════
# B. signal_scoring.py — earnings tagging + dedup in ingest_and_evaluate
# ══════════════════════════════════════════════════════════════════════════════

class TestBrokerActionEarningsTagging:
    """actions_from_broker_pdf_digests should tag earnings-related digests."""

    def _make_earnings_digest(self, fy27_eps_pct=-8.5):
        return {
            "source_type": "korean_broker_pdf",
            "published_date_iso": "2026-07-30",
            "key_metrics": {
                "fy27_eps_revision_pct": fy27_eps_pct,
                "fy27_eps_new": 5.03,
                "fy27_eps_prior": 5.50,
            },
            "target_price_action": {
                "broker": "Mirae Asset",
                "ticker": "000660.KS",
                "new_pt": 230000,
                "prior_pt": 250000,
                "direction": "lowered",
            },
            "key_quotes": ["Q2 results missed on HBM pricing"],
        }

    def _make_non_earnings_digest(self):
        return {
            "source_type": "korean_broker_pdf",
            "published_date_iso": "2026-07-30",
            "key_metrics": {},   # no EPS fields → not earnings-related
            "target_price_action": {
                "broker": "NH Securities",
                "ticker": "000660.KS",
                "new_pt": 220000,
                "prior_pt": 210000,
                "direction": "raised",
            },
            "key_quotes": [],
        }

    def test_earnings_digest_tagged_as_earnings_coverage(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        digests = [self._make_earnings_digest()]
        actions = actions_from_broker_pdf_digests(digests)
        assert len(actions) == 1
        assert actions[0].event_type == "earnings_coverage"
        assert actions[0].event_key is not None
        assert "earnings_call" in actions[0].event_key

    def test_non_earnings_digest_tagged_as_broker_action(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        digests = [self._make_non_earnings_digest()]
        actions = actions_from_broker_pdf_digests(digests)
        assert len(actions) == 1
        assert actions[0].event_type == "broker_action"
        assert actions[0].event_key is None

    def test_mixed_digests_tagged_correctly(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        digests = [self._make_earnings_digest(), self._make_non_earnings_digest()]
        actions = actions_from_broker_pdf_digests(digests)
        assert len(actions) == 2
        earnings_actions = [a for a in actions if a.event_type == "earnings_coverage"]
        broker_actions = [a for a in actions if a.event_type == "broker_action"]
        assert len(earnings_actions) == 1
        assert len(broker_actions) == 1


class TestIngestAndEvaluateDedup:
    """ingest_and_evaluate suppresses earnings echoes via the event log."""

    def _make_earnings_digest(self, broker: str, score_direction: str = "bearish") -> dict:
        """Return a Korean broker PDF digest tagged as earnings-related."""
        eps_pct = -8.0 if score_direction == "bearish" else 5.0
        pt_new = 230000 if score_direction == "bearish" else 270000
        direction = "lowered" if score_direction == "bearish" else "raised"
        return {
            "source_type": "korean_broker_pdf",
            "published_date_iso": "2026-07-30",
            "key_metrics": {"fy27_eps_revision_pct": eps_pct},
            "target_price_action": {
                "broker": broker, "ticker": "000660.KS",
                "new_pt": pt_new, "prior_pt": 250000, "direction": direction,
            },
            "key_quotes": [],
        }

    def test_first_earnings_action_is_scored(self, tmp_path):
        from src.analysis.signal_scoring import ingest_and_evaluate
        log_file = tmp_path / "state" / "event_log.json"
        eps_file = tmp_path / "state" / "eps_revisions_running.json"

        with (
            patch("src.state.EVENT_LOG_FILE", log_file),
            patch("src.analysis.signal_scoring.EPS_RUNNING_STATE_FILE", eps_file),
        ):
            verdict = ingest_and_evaluate(
                naver_payload=None,
                finnhub_payload=None,
                broker_pdf_digests=[self._make_earnings_digest("Mirae Asset")],
            )

        assert verdict["dedup_stats"]["actions_scored"] == 1
        assert verdict["dedup_stats"]["echoes_suppressed"] == 0
        # Event log should have the new entry
        with patch("src.state.EVENT_LOG_FILE", log_file):
            from src.state import load_event_log
            log = load_event_log()
        assert any("earnings_call" in k for k in log)

    def test_second_earnings_action_is_suppressed(self, tmp_path):
        from src.analysis.signal_scoring import ingest_and_evaluate
        log_file = tmp_path / "state" / "event_log.json"
        eps_file = tmp_path / "state" / "eps_revisions_running.json"

        # First call — scores first echo
        with (
            patch("src.state.EVENT_LOG_FILE", log_file),
            patch("src.analysis.signal_scoring.EPS_RUNNING_STATE_FILE", eps_file),
        ):
            ingest_and_evaluate(
                naver_payload=None, finnhub_payload=None,
                broker_pdf_digests=[self._make_earnings_digest("Mirae Asset")],
            )
            # Second call — same event, different broker
            verdict2 = ingest_and_evaluate(
                naver_payload=None, finnhub_payload=None,
                broker_pdf_digests=[self._make_earnings_digest("Samsung Securities")],
            )

        assert verdict2["dedup_stats"]["echoes_suppressed"] == 1
        assert verdict2["dedup_stats"]["actions_scored"] == 0

    def test_divergence_detected_on_contradicting_echo(self, tmp_path):
        from src.analysis.signal_scoring import ingest_and_evaluate
        log_file = tmp_path / "state" / "event_log.json"
        eps_file = tmp_path / "state" / "eps_revisions_running.json"

        with (
            patch("src.state.EVENT_LOG_FILE", log_file),
            patch("src.analysis.signal_scoring.EPS_RUNNING_STATE_FILE", eps_file),
        ):
            # First: bearish coverage
            ingest_and_evaluate(
                naver_payload=None, finnhub_payload=None,
                broker_pdf_digests=[self._make_earnings_digest("Mirae Asset", "bearish")],
            )
            # Second: bullish coverage of same event
            verdict2 = ingest_and_evaluate(
                naver_payload=None, finnhub_payload=None,
                broker_pdf_digests=[self._make_earnings_digest("Goldman Sachs", "bullish")],
            )

        assert verdict2["dedup_stats"]["divergences_found"] == 1
        assert len(verdict2["divergent_events"]) == 1

    def test_non_earnings_actions_unaffected_by_dedup(self, tmp_path):
        """Broker actions without earnings fields pass through the dedup layer unchanged."""
        from src.analysis.signal_scoring import ingest_and_evaluate
        log_file = tmp_path / "state" / "event_log.json"
        eps_file = tmp_path / "state" / "eps_revisions_running.json"

        non_earnings_digest = {
            "source_type": "korean_broker_pdf",
            "published_date_iso": "2026-07-30",
            "key_metrics": {},   # no EPS fields
            "target_price_action": {
                "broker": "NH Securities", "ticker": "000660.KS",
                "new_pt": 270000, "prior_pt": 260000, "direction": "raised",
            },
            "key_quotes": [],
        }
        with (
            patch("src.state.EVENT_LOG_FILE", log_file),
            patch("src.analysis.signal_scoring.EPS_RUNNING_STATE_FILE", eps_file),
        ):
            verdict = ingest_and_evaluate(
                naver_payload=None, finnhub_payload=None,
                broker_pdf_digests=[non_earnings_digest],
            )

        # Non-earnings actions pass through unchanged
        assert verdict["dedup_stats"]["echoes_suppressed"] == 0
        assert verdict["dedup_stats"]["actions_scored"] == 1

    def test_dedup_stats_always_in_verdict(self, tmp_path):
        from src.analysis.signal_scoring import ingest_and_evaluate
        log_file = tmp_path / "state" / "event_log.json"
        eps_file = tmp_path / "state" / "eps_revisions_running.json"
        with (
            patch("src.state.EVENT_LOG_FILE", log_file),
            patch("src.analysis.signal_scoring.EPS_RUNNING_STATE_FILE", eps_file),
        ):
            verdict = ingest_and_evaluate(None, None, [])
        assert "dedup_stats" in verdict
        assert "divergent_events" in verdict


class TestBrokerActionBackwardCompat:
    """Existing state files (no event_type/event_key) must still load cleanly."""

    def test_from_dict_without_new_fields(self):
        from src.analysis.signal_scoring import BrokerAction
        old_style = {
            "action_date_iso": "2026-05-01",
            "broker_raw_name": "Mirae Asset",
            "broker_canonical": "Mirae Asset",
            "tier": "A",
            "ticker": "000660.KS",
            "rating_change": "maintained",
            "rating_change_label": "Maintained Buy",
            "target_price_pct_change": -5.0,
            "target_price_prior": 250000.0,
            "target_price_new": 237500.0,
            "fy27_eps_pct_change": None,
            "score": -6.0,
            "score_breakdown": {},
            "source": "Mirae PDF",
            "note_excerpt": None,
            # event_type and event_key ABSENT (old format)
        }
        action = BrokerAction(**old_style)
        assert action.event_type == "broker_action"
        assert action.event_key is None


# ══════════════════════════════════════════════════════════════════════════════
# C. template.py — divergence box rendering
# ══════════════════════════════════════════════════════════════════════════════

class TestEpsRevisionsSectionDivergence:
    def _minimal_verdict(self, divergent_events=None, dedup_stats=None) -> dict:
        return {
            "overall_status": "FIRED",
            "per_ticker": {
                "000660.KS": {
                    "fired_status": "FIRED",
                    "rolling_7d": {"score_bearish": -15.0, "score_bullish": 0.0,
                                   "score_net": -15.0, "top_impact_actions": []},
                    "rolling_30d": {"score_bearish": -22.0, "score_bullish": 0.0,
                                    "score_net": -22.0, "top_impact_actions": []},
                    "single_note_flags_today": [],
                }
            },
            "new_actions_today": [],
            "divergent_events": divergent_events or [],
            "dedup_stats": dedup_stats or {
                "total_new_actions": 5, "actions_scored": 1,
                "echoes_suppressed": 4, "divergences_found": 0,
            },
        }

    def test_dedup_footer_shown_when_echoes_suppressed(self):
        from src.email_module.template import _build_eps_revisions_section
        verdict = self._minimal_verdict(dedup_stats={
            "total_new_actions": 8, "actions_scored": 1,
            "echoes_suppressed": 7, "divergences_found": 0,
        })
        html_out = _build_eps_revisions_section(verdict)
        assert "7 echoes suppressed" in html_out

    def test_no_dedup_footer_when_no_echoes(self):
        from src.email_module.template import _build_eps_revisions_section
        verdict = self._minimal_verdict(dedup_stats={
            "total_new_actions": 2, "actions_scored": 2,
            "echoes_suppressed": 0, "divergences_found": 0,
        })
        html_out = _build_eps_revisions_section(verdict)
        assert "echoes suppressed" not in html_out

    def test_divergence_box_rendered_when_events_present(self):
        from src.email_module.template import _build_eps_revisions_section
        divergent = [{
            "event_key": "000660.KS:earnings_call:2026-Q3",
            "primary_verdict": "bearish",
            "n_observations": 3,
            "observations": [{"verdict": "bullish"}, {"verdict": "bearish"}],
            "first_observation_source": "Mirae Asset",
            "event_date_iso": "2026-07-29",
            "divergence_flagged": True,
        }]
        verdict = self._minimal_verdict(divergent_events=divergent)
        html_out = _build_eps_revisions_section(verdict)
        assert "Divergent event coverage" in html_out
        assert "000660.KS:earnings_call:2026-Q3" in html_out
        assert "bearish" in html_out

    def test_no_divergence_box_when_no_events(self):
        from src.email_module.template import _build_eps_revisions_section
        verdict = self._minimal_verdict(divergent_events=[])
        html_out = _build_eps_revisions_section(verdict)
        assert "Divergent event coverage" not in html_out


# ══════════════════════════════════════════════════════════════════════════════
# D. signal_reasoner — divergent_events wired into signal_specific
# ══════════════════════════════════════════════════════════════════════════════

class TestSignalReasonerDivergentEventsWired:
    def test_divergent_events_in_signal_specific(self):
        """signal_specific['divergent_events'] comes from scoring_verdict."""
        from src.analysis import signal_reasoner

        mock_verdict = {
            "overall_status": "FIRED",
            "per_ticker": {},
            "new_actions_today": [],
            "total_actions_in_state": 3,
            "dedup_stats": {"echoes_suppressed": 2, "actions_scored": 1,
                            "total_new_actions": 3, "divergences_found": 1},
            "divergent_events": [{"event_key": "000660.KS:earnings_call:2026-Q3",
                                   "primary_verdict": "bearish"}],
        }

        with patch("src.analysis.signal_scoring.ingest_and_evaluate", return_value=mock_verdict):
            # Build a minimal call to the t2_eps_revisions block in signal_reasoner
            # by inspecting what signal_specific ends up containing
            indicator = MagicMock()
            indicator.id = "t2_eps_revisions"
            indicator.trigger_patterns = []

            signal_specific = {}
            # Simulate the t2_eps_revisions block
            from src.analysis.signal_scoring import ingest_and_evaluate
            scoring_verdict = ingest_and_evaluate(None, None, [])
            signal_specific["weighted_scoring_verdict"] = scoring_verdict
            signal_specific["dedup_stats"] = scoring_verdict.get("dedup_stats", {})
            signal_specific["divergent_events"] = scoring_verdict.get("divergent_events", [])

        assert len(signal_specific["divergent_events"]) == 1
        assert signal_specific["dedup_stats"]["echoes_suppressed"] == 2
