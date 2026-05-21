"""Tests for v3.39.7.3 indicator evidence store.

Validates that:
  1. The bootstrap JSON at state/indicator_evidence.json loads cleanly
  2. Every indicator has a valid status from VALID_STATUSES
  3. Every evidence entry has a valid persistence and type
  4. The Layer 2 prompt-format method produces readable output
  5. active_evidence() correctly filters event_window entries by age
  6. update_evidence_after_layer2() records transitions correctly
  7. save → load roundtrips preserve all data

CHANGELOG (this file):
  v3.39.7.3 (2026-05-20) — NEW FILE. Test coverage for the evidence store
  introduced in v3.39.7.3. Tests use a tmp_path fixture so they don't
  collide with the real state/indicator_evidence.json.
"""

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.config import INDICATORS
from src.state import (
    EvidenceEntry,
    IndicatorEvidence,
    PERSISTENCE_CATEGORIES,
    EVIDENCE_TYPES,
    VALID_STATUSES,
    load_indicator_evidence_store,
    save_indicator_evidence_store,
    update_evidence_after_layer2,
)


class TestBootstrapFile(unittest.TestCase):
    """Validate the committed state/indicator_evidence.json bootstrap."""

    @classmethod
    def setUpClass(cls):
        cls.bootstrap_path = Path("state/indicator_evidence.json")
        cls.store = load_indicator_evidence_store(cls.bootstrap_path)

    def test_bootstrap_file_exists(self):
        self.assertTrue(
            self.bootstrap_path.exists(),
            f"Bootstrap file missing at {self.bootstrap_path}",
        )

    def test_all_23_indicators_present(self):
        # v3.4.0 added t1_decode_hbm_captive_ratio (DHCR) — direction monitor
        # collapsing original ROADMAP indicators 23/24/25 into one composite.
        self.assertEqual(
            len(self.store), 23,
            f"Expected 23 indicators in bootstrap, got {len(self.store)}",
        )

    def test_indicator_ids_match_config(self):
        config_ids = {ind.id for ind in INDICATORS}
        bootstrap_ids = set(self.store.keys())
        missing = config_ids - bootstrap_ids
        extra = bootstrap_ids - config_ids
        self.assertEqual(
            missing, set(),
            f"Indicators in config but missing from bootstrap: {missing}",
        )
        self.assertEqual(
            extra, set(),
            f"Indicators in bootstrap but not in config: {extra}",
        )

    def test_all_statuses_valid(self):
        for ind_id, ev in self.store.items():
            self.assertIn(
                ev.current_status, VALID_STATUSES,
                f"{ind_id}: invalid status '{ev.current_status}'",
            )

    def test_all_evidence_persistence_valid(self):
        for ind_id, ev in self.store.items():
            for i, e in enumerate(ev.evidence_log):
                self.assertIn(
                    e.persistence, PERSISTENCE_CATEGORIES,
                    f"{ind_id}.evidence_log[{i}]: invalid persistence '{e.persistence}'",
                )

    def test_all_evidence_types_valid(self):
        for ind_id, ev in self.store.items():
            for i, e in enumerate(ev.evidence_log):
                self.assertIn(
                    e.type, EVIDENCE_TYPES,
                    f"{ind_id}.evidence_log[{i}]: invalid type '{e.type}'",
                )

    def test_fired_indicators_have_fired_since(self):
        """Indicators with Fired/Partially fired status should record fired_since."""
        for ind_id, ev in self.store.items():
            if ev.current_status in ("Fired", "Partially fired"):
                self.assertIsNotNone(
                    ev.fired_since,
                    f"{ind_id}: status is {ev.current_status} but fired_since is None",
                )

    def test_fired_count_matches_may9_baseline(self):
        """May 9 doc had 5 T1 + 6 T2 + 1 T3 (reverted) fired = 12 total.

        After v3.39.7 framework upgrade (cycle matrix replaces spot-only
        trigger), t1_spot_decline goes from FIRED → Not fired since the
        matrix sits at R1/C2 today. t3_relative_strength was 'fired then
        reversed' on May 9 — current computed value supports Not fired.
        So expected: 4 T1 + 6 T2 + 0 T3 + 0 T4 = 10 fired.
        """
        fired = [
            ind_id for ind_id, ev in self.store.items()
            if ev.current_status == "Fired"
        ]
        self.assertEqual(
            len(fired), 10,
            f"Expected 10 fired indicators per May 9 baseline + framework "
            f"upgrades, got {len(fired)}: {fired}",
        )

    def test_every_fired_evidence_has_reversal_condition(self):
        """Every evidence entry in a fired indicator must declare what would
        constitute reversal. This is the contract that prevents the
        v3.39.6 'silence = unfire' regression.
        """
        for ind_id, ev in self.store.items():
            if ev.current_status not in ("Fired", "Partially fired"):
                continue
            for i, e in enumerate(ev.evidence_log):
                self.assertTrue(
                    len(e.reversal_condition) > 5,
                    f"{ind_id}.evidence_log[{i}]: reversal_condition is empty "
                    f"or trivial — required for fired indicators",
                )


class TestEvidenceFormatting(unittest.TestCase):
    """Layer 2 prompt formatting."""

    def test_format_for_layer2_prompt_includes_all_fields(self):
        ev = IndicatorEvidence(
            indicator_id="test",
            current_status="Fired",
            fired_since="2026-01-15",
            last_updated="2026-05-20",
            next_check_due="2026-07-22",
            evidence_log=[
                EvidenceEntry(
                    date="2026-01-15",
                    type="forecast",
                    value="Hyperscaler 2026 capex $725B (+77% YoY)",
                    source="Consensus + 10-Ks",
                    persistence="quarterly",
                    reversal_condition="Hyperscaler quarterly print shows capex CUT vs guidance",
                ),
            ],
        )
        prompt = ev.format_for_layer2_prompt()
        self.assertIn("Current status: Fired", prompt)
        self.assertIn("Fired since: 2026-01-15", prompt)
        self.assertIn("Next scheduled re-check: 2026-07-22", prompt)
        self.assertIn("$725B", prompt)
        self.assertIn("quarterly", prompt)
        self.assertIn("Reversal condition:", prompt)
        self.assertIn("capex CUT", prompt)

    def test_format_empty_evidence_log(self):
        ev = IndicatorEvidence(
            indicator_id="test",
            current_status="Not fired",
            fired_since=None,
            last_updated="2026-05-20",
            evidence_log=[],
        )
        self.assertIn("no prior evidence", ev.format_for_layer2_prompt())


class TestActiveEvidenceFiltering(unittest.TestCase):
    """Filtering of evidence by persistence + age."""

    def test_permanent_evidence_always_active(self):
        ev = IndicatorEvidence(
            indicator_id="test",
            current_status="Fired",
            fired_since="2020-01-01",
            last_updated="2026-05-20",
            evidence_log=[
                EvidenceEntry(
                    date="2020-01-01",  # 6 years old
                    type="announcement",
                    value="x",
                    source="x",
                    persistence="permanent",
                    reversal_condition="not applicable",
                ),
            ],
        )
        active = ev.active_evidence(today=date(2026, 5, 20))
        self.assertEqual(len(active), 1)

    def test_live_evidence_always_active(self):
        ev = IndicatorEvidence(
            indicator_id="test",
            current_status="Fired",
            fired_since="2026-05-20",
            last_updated="2026-05-20",
            evidence_log=[
                EvidenceEntry(
                    date="2026-05-20",
                    type="computed",
                    value="SK Hynix forward P/E 4.56x < 5x threshold",
                    source="computed_metrics",
                    persistence="live",
                    reversal_condition="P/E above threshold",
                ),
            ],
        )
        active = ev.active_evidence(today=date(2026, 5, 20))
        self.assertEqual(len(active), 1)

    def test_event_window_evidence_expires_after_35_days(self):
        ev = IndicatorEvidence(
            indicator_id="test",
            current_status="Not fired",
            fired_since=None,
            last_updated="2026-05-20",
            evidence_log=[
                EvidenceEntry(
                    date="2026-03-15",  # 66 days before May 20 — expired
                    type="data_point",
                    value="MU -20% vs PHLX in 5 days",
                    source="historical",
                    persistence="event_window",
                    reversal_condition="window expired",
                ),
                EvidenceEntry(
                    date="2026-05-10",  # 10 days before — still active
                    type="data_point",
                    value="recent move",
                    source="x",
                    persistence="event_window",
                    reversal_condition="x",
                ),
            ],
        )
        active = ev.active_evidence(today=date(2026, 5, 20))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].date, "2026-05-10")


class TestSaveLoadRoundtrip(unittest.TestCase):
    """Persistence: save → load → equality."""

    def setUp(self):
        self.tmp_dir = Path("/tmp/test_evidence_roundtrip")
        self.tmp_dir.mkdir(exist_ok=True)
        self.tmp_path = self.tmp_dir / "indicator_evidence.json"
        if self.tmp_path.exists():
            self.tmp_path.unlink()

    def tearDown(self):
        if self.tmp_path.exists():
            self.tmp_path.unlink()

    def test_roundtrip_preserves_data(self):
        original = {
            "t1_test": IndicatorEvidence(
                indicator_id="t1_test",
                current_status="Fired",
                fired_since="2026-01-15",
                last_updated="2026-05-20",
                next_check_due="2026-07-22",
                notes="test note",
                evidence_log=[
                    EvidenceEntry(
                        date="2026-01-15",
                        type="forecast",
                        value="value 1",
                        source="src 1",
                        persistence="quarterly",
                        reversal_condition="condition 1",
                    ),
                ],
            ),
        }
        save_indicator_evidence_store(original, self.tmp_path)
        loaded = load_indicator_evidence_store(self.tmp_path)
        self.assertEqual(len(loaded), 1)
        self.assertIn("t1_test", loaded)
        ev = loaded["t1_test"]
        self.assertEqual(ev.current_status, "Fired")
        self.assertEqual(ev.fired_since, "2026-01-15")
        self.assertEqual(ev.notes, "test note")
        self.assertEqual(len(ev.evidence_log), 1)
        self.assertEqual(ev.evidence_log[0].value, "value 1")
        self.assertEqual(ev.evidence_log[0].persistence, "quarterly")


class TestStatusTransitions(unittest.TestCase):
    """update_evidence_after_layer2() correctly records transitions."""

    def test_no_change_status_only_updates_last_updated(self):
        store = {
            "t1_test": IndicatorEvidence(
                indicator_id="t1_test",
                current_status="Fired",
                fired_since="2026-01-15",
                last_updated="2026-05-19",
                evidence_log=[],
            ),
        }

        class FakeResult:
            signal_id = "t1_test"
            fired_status = "Fired"  # no change
            consensus_direction = "preserved"
            evidence_sources = []

        update_evidence_after_layer2(store, [FakeResult()], today=date(2026, 5, 20))
        self.assertEqual(store["t1_test"].current_status, "Fired")
        self.assertEqual(store["t1_test"].last_updated, "2026-05-20")
        self.assertEqual(len(store["t1_test"].evidence_log), 0)  # no transition entry

    def test_status_transition_appends_log_entry(self):
        store = {
            "t1_test": IndicatorEvidence(
                indicator_id="t1_test",
                current_status="Fired",
                fired_since="2026-01-15",
                last_updated="2026-05-19",
                evidence_log=[
                    EvidenceEntry("2026-01-15", "forecast", "v1", "s1", "quarterly", "rc1"),
                ],
            ),
        }

        class FakeResult:
            signal_id = "t1_test"
            fired_status = "Not fired"  # CHANGED
            consensus_direction = "Reversal evidence found: capex cut announced"
            evidence_sources = ["src1", "src2", "src3"]

        update_evidence_after_layer2(store, [FakeResult()], today=date(2026, 5, 20))
        self.assertEqual(store["t1_test"].current_status, "Not fired")
        self.assertEqual(len(store["t1_test"].evidence_log), 2)  # original + transition
        latest = store["t1_test"].evidence_log[-1]
        self.assertEqual(latest.date, "2026-05-20")
        self.assertIn("Fired -> Not fired", latest.value)

    def test_first_time_indicator_creates_entry(self):
        store = {}

        class FakeResult:
            signal_id = "t1_new"
            fired_status = "Fired"
            consensus_direction = "Strong evidence"
            evidence_sources = []

        update_evidence_after_layer2(store, [FakeResult()], today=date(2026, 5, 20))
        self.assertIn("t1_new", store)
        self.assertEqual(store["t1_new"].current_status, "Fired")
        self.assertEqual(store["t1_new"].fired_since, "2026-05-20")


if __name__ == "__main__":
    unittest.main()
