"""Tests for v3.39.7.5 — 24h ephemeral trigger event log.

Locks the contract that:
1. Transitions get logged on first observation within a 24h rolling window
2. Reruns within 24h re-display the same events (dedup by indicator_id + new_status)
3. Entries auto-prune past 24h on load
4. Empty/malformed state file → empty list (safe defaults)
5. Non-transitions (unchanged status, missing prior) → not logged
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# Import directly — the helpers we're testing
from src import state as state_module
from src.state import (
    load_trigger_log_24h,
    save_trigger_log_24h,
    update_trigger_log_24h,
)


class _FakeAssessment:
    """Stand-in for IndicatorAssessment with the four attrs the log reads."""
    def __init__(self, id: str, name: str, tier: int, current_status: str):
        self.id = id
        self.name = name
        self.tier = tier
        self.current_status = current_status


class TestTriggerLog24h(unittest.TestCase):
    """Test the 24h ephemeral trigger log."""

    def setUp(self):
        # Isolate state to a tmp dir for each test — avoid polluting real state/
        self._tmpdir = Path(tempfile.mkdtemp(prefix="trigger_log_test_"))
        self._original_state_dir = state_module.STATE_DIR
        self._original_file = state_module.TRIGGER_EVENTS_24H_FILE
        state_module.STATE_DIR = self._tmpdir
        state_module.TRIGGER_EVENTS_24H_FILE = self._tmpdir / "trigger_events_24h.json"

    def tearDown(self):
        state_module.STATE_DIR = self._original_state_dir
        state_module.TRIGGER_EVENTS_24H_FILE = self._original_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_empty_when_file_missing(self):
        """No file → empty list (not an exception)."""
        self.assertEqual(load_trigger_log_24h(), [])

    def test_load_empty_when_file_malformed_json(self):
        """Bad JSON → warned + empty list."""
        state_module.TRIGGER_EVENTS_24H_FILE.write_text("not valid json {{")
        self.assertEqual(load_trigger_log_24h(), [])

    def test_load_empty_when_root_not_a_list(self):
        """JSON object instead of array → empty list."""
        state_module.TRIGGER_EVENTS_24H_FILE.write_text('{"a": 1}')
        self.assertEqual(load_trigger_log_24h(), [])

    def test_load_prunes_entries_older_than_24h(self):
        """Entries with fired_at_iso > 24h ago are dropped on load."""
        now = datetime.utcnow()
        recent_iso = (now - timedelta(hours=12)).isoformat()
        old_iso = (now - timedelta(hours=25)).isoformat()
        save_trigger_log_24h([
            {"indicator_id": "t1_a", "indicator_name": "A", "tier": 1,
             "old_status": "Not fired", "new_status": "Fired",
             "description": "...", "fired_at_iso": recent_iso},
            {"indicator_id": "t1_b", "indicator_name": "B", "tier": 1,
             "old_status": "Not fired", "new_status": "Fired",
             "description": "...", "fired_at_iso": old_iso},
        ])
        loaded = load_trigger_log_24h(now_utc=now)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["indicator_id"], "t1_a")

    def test_update_adds_transitions(self):
        """A status change is captured into the log."""
        prior = {"t1_a": "Not fired"}
        current = [_FakeAssessment("t1_a", "Indicator A", 1, "Fired")]
        result = update_trigger_log_24h(prior, current)
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["indicator_id"], "t1_a")
        self.assertEqual(entry["old_status"], "Not fired")
        self.assertEqual(entry["new_status"], "Fired")
        self.assertEqual(entry["tier"], 1)
        # Persisted to disk too
        with open(state_module.TRIGGER_EVENTS_24H_FILE) as f:
            on_disk = json.load(f)
        self.assertEqual(len(on_disk), 1)

    def test_update_skips_unchanged_status(self):
        """Status that didn't change → not added."""
        prior = {"t1_a": "Fired"}
        current = [_FakeAssessment("t1_a", "Indicator A", 1, "Fired")]
        result = update_trigger_log_24h(prior, current)
        self.assertEqual(result, [])

    def test_update_skips_missing_prior(self):
        """Indicator without a prior baseline (first observation ever) is NOT logged.

        Rationale: log captures transitions, not initial-state-observations.
        First-ever observation is an entry into the assessment universe, not a
        change.
        """
        prior = {}  # empty — no prior data
        current = [_FakeAssessment("t1_a", "Indicator A", 1, "Fired")]
        result = update_trigger_log_24h(prior, current)
        self.assertEqual(result, [])

    def test_update_dedups_same_transition_within_24h(self):
        """The KEY case: rerun within 24h must NOT re-add the same transition.

        This is the whole point of the 24h log — multiple manual reruns
        during the day should keep showing the same events without duplicating.
        """
        prior = {"t1_a": "Not fired"}
        current = [_FakeAssessment("t1_a", "Indicator A", 1, "Fired")]
        # Morning run
        morning = datetime.utcnow() - timedelta(hours=4)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.utcnow.return_value = morning
            r1 = update_trigger_log_24h(prior, current)
        self.assertEqual(len(r1), 1)
        morning_fired_at = r1[0]["fired_at_iso"]
        # Afternoon rerun — same transition, same data
        afternoon = datetime.utcnow() - timedelta(hours=1)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.utcnow.return_value = afternoon
            r2 = update_trigger_log_24h(prior, current)
        # Still only one entry, with the ORIGINAL timestamp (not re-stamped)
        self.assertEqual(len(r2), 1)
        self.assertEqual(r2[0]["fired_at_iso"], morning_fired_at)

    def test_update_logs_second_transition_with_different_status(self):
        """Same indicator, DIFFERENT new_status → separate log entry.

        Morning: Not fired → Fired
        Afternoon: Fired → Partially fired
        Both should appear in the 24h log.
        """
        # Morning: enter Fired
        prior_morning = {"t1_a": "Not fired"}
        current_morning = [_FakeAssessment("t1_a", "A", 1, "Fired")]
        morning = datetime.utcnow() - timedelta(hours=4)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.utcnow.return_value = morning
            update_trigger_log_24h(prior_morning, current_morning)

        # Afternoon: enter Partially fired (with prior now Fired)
        prior_afternoon = {"t1_a": "Fired"}
        current_afternoon = [_FakeAssessment("t1_a", "A", 1, "Partially fired")]
        afternoon = datetime.utcnow() - timedelta(hours=1)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.utcnow.return_value = afternoon
            r = update_trigger_log_24h(prior_afternoon, current_afternoon)
        # Both transitions present
        self.assertEqual(len(r), 2)
        statuses = {e["new_status"] for e in r}
        self.assertEqual(statuses, {"Fired", "Partially fired"})

    def test_update_prunes_when_old_entries_present(self):
        """Old entries get pruned on update, even if no new transitions."""
        now = datetime.utcnow()
        old_iso = (now - timedelta(hours=25)).isoformat()
        save_trigger_log_24h([
            {"indicator_id": "t1_old", "indicator_name": "Old", "tier": 1,
             "old_status": "Not fired", "new_status": "Fired",
             "description": "...", "fired_at_iso": old_iso},
        ])
        # Update with no new transitions
        r = update_trigger_log_24h({"t1_a": "Fired"},
                                    [_FakeAssessment("t1_a", "A", 1, "Fired")])
        self.assertEqual(r, [])

    def test_update_handles_malformed_assessment_gracefully(self):
        """Assessment missing required attrs is skipped, not crashed."""

        class _BrokenAssessment:
            pass  # No id/name/tier/current_status

        prior = {"t1_a": "Not fired"}
        current = [_BrokenAssessment(), _FakeAssessment("t1_a", "A", 1, "Fired")]
        r = update_trigger_log_24h(prior, current)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["indicator_id"], "t1_a")


if __name__ == "__main__":
    unittest.main()
