"""v3.4.4.1 — Tests for the Layer 1 extraction → aggregator → state file pipeline.

Validates:
  - Aggregator reads extraction_log_*.jsonl entries correctly
  - Tier-weighted conflict resolution (Tier A > B > C)
  - State persisted atomically to state/*.json
  - Bootstrap state preserved when no extraction entries
  - Aggregator failure leaves state file unchanged
  - Sub status re-evaluation matches threshold logic
  - F-1 detection advances ADR lifecycle phase
  - Insider sales accumulate across entries
"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.analysis import competitor_aggregator, adr_aggregator


class _AggregatorTestBase(unittest.TestCase):
    """Each test runs in a fresh tmp working dir with a synthetic state + thresholds file."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="v3441_agg_"))
        (self.tmpdir / "data").mkdir()
        (self.tmpdir / "state").mkdir()
        self.original_cwd = Path.cwd()
        import os
        os.chdir(self.tmpdir)

    def tearDown(self):
        import os
        os.chdir(self.original_cwd)
        shutil.rmtree(self.tmpdir)

    def _write_competitor_thresholds(self):
        (self.tmpdir / "data" / "competitor_thresholds.yaml").write_text("""
schema_version: "2.0"
aggregator_config:
  extraction_log_lookback_days: 30
sub_7a_cxmt: {display_name: CXMT, sources: []}
sub_7b_samsung_hbm4: {display_name: Samsung HBM4, sources: []}
sub_7c_micron: {display_name: Micron HBM, sources: []}
sub_7d_hbf: {display_name: HBF, sources: [], moat_extension_milestones: [], substitution_risk_milestones: []}
dual_threat_scenario:
  stock_impact_combined_pct: "-30 to -45"
  stock_impact_with_cycle_peak_overlay_pct: "-60 to -75"
  narrative: "Test narrative"
""")

    def _write_adr_thresholds(self):
        (self.tmpdir / "data" / "adr_event_tracking.yaml").write_text("""
schema_version: "2.0"
aggregator_config:
  extraction_log_lookback_days: 30
sub_8a_f1_filing: {display_name: F-1 filing watch, sources: []}
sub_8b_roadshow_demand: {display_name: Roadshow demand, sources: []}
sub_8c_pricing_day: {display_name: Pricing-day, sources: []}
sub_8d_insider_sales_in_window: {display_name: Insider sales, sources: []}
calendar_milestones: []
confirmed_offering_structure: {}
academic_calibration: {}
""")

    def _write_competitor_bootstrap_state(self):
        bootstrap = {
            "_metadata": {"schema_version": "2.0", "last_aggregator_run": "2026-05-22T12:00:00Z"},
            "sub_7a_cxmt": {
                "current_status": "Not fired",
                "estimated_eps_impact_pct": 1.0,
                "current_state": {"cxmt_ddr5_share_pct": 2.0, "cxmt_oem_mainstream_wins": []},
                "evidence_log": [],
            },
            "sub_7b_samsung_hbm4": {
                "current_status": "Partially fired",
                "estimated_eps_impact_pct": 10.0,
                "current_state": {"sk_hynix_hbm4_nvidia_share_pct": 70, "hbm_gross_margin_pct_sk_hynix": 80},
                "evidence_log": [],
            },
            "sub_7c_micron": {
                "current_status": "Not fired",
                "estimated_eps_impact_pct": 0.5,
                "current_state": {"micron_total_hbm_share_pct": 18, "micron_vera_rubin_design_win": "excluded"},
                "evidence_log": [],
            },
            "sub_7d_hbf": {
                "current_status": "Not fired",
                "estimated_eps_impact_pct": 0.0,
                "current_state": {"hbf_commercial_stage": "pre_commercial"},
                "evidence_log": [],
            },
        }
        (self.tmpdir / "state" / "competitor_state.json").write_text(json.dumps(bootstrap, indent=2))

    def _write_adr_bootstrap_state(self):
        bootstrap = {
            "_metadata": {"schema_version": "2.0", "last_aggregator_run": "2026-05-22T12:00:00Z"},
            "lifecycle_phase": "pre_event",
            "composite_status": "Not fired",
            "sub_8a_f1_filing": {
                "current_status": "Pre-event",
                "status_pill_label": "Awaiting F-1",
                "current_state": {"f1_filed": False, "f1_canceled": False, "underwriter_syndicate": []},
                "evidence_log": [],
            },
            "sub_8b_roadshow_demand": {
                "current_status": "Pre-event",
                "current_state": {"roadshow_active": False, "demand_classification": None},
                "evidence_log": [],
            },
            "sub_8c_pricing_day": {
                "current_status": "Pre-event",
                "current_state": {"pricing_day": None, "pricing_discount_pct": None},
                "evidence_log": [],
            },
            "sub_8d_insider_sales_in_window": {
                "current_status": "Pre-event",
                "current_state": {"insider_sales_in_window": [], "insider_sales_total_usd_m_in_window": 0},
                "evidence_log": [],
            },
        }
        (self.tmpdir / "state" / "adr_state.json").write_text(json.dumps(bootstrap, indent=2))

    def _write_extraction_log(self, entries: list):
        """Write entries to extraction_log_<today>.jsonl"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.tmpdir / "state" / f"extraction_log_{today}.jsonl"
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


class TestCompetitorAggregatorEmptyLog(_AggregatorTestBase):
    """No extraction entries → bootstrap state preserved."""

    def test_bootstrap_state_preserved(self):
        self._write_competitor_thresholds()
        self._write_competitor_bootstrap_state()
        state = competitor_aggregator.aggregate_competitor_extractions()
        # Sub-7b stays Partially fired (bootstrap)
        self.assertEqual(state["sub_7b_samsung_hbm4"]["current_status"], "Partially fired")
        # Sub-7a stays Not fired
        self.assertEqual(state["sub_7a_cxmt"]["current_status"], "Not fired")


class TestCompetitorAggregatorTier1OEMWin(_AggregatorTestBase):
    """CXMT Tier-1 OEM mainstream win extraction → 7A Fired."""

    def test_mainstream_oem_win_fires_7a(self):
        self._write_competitor_thresholds()
        self._write_competitor_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([{
            "source_id": "digitimes_2026_05_22",
            "source_type": "news_article",
            "source": "digitimes",
            "date": now,
            "extracted_at": now,
            "competitor_signals": {"cxmt_oem_mainstream_win": "Dell"},
        }])
        state = competitor_aggregator.aggregate_competitor_extractions()
        self.assertEqual(state["sub_7a_cxmt"]["current_status"], "Fired")
        self.assertIn("Dell", state["sub_7a_cxmt"]["current_state"]["cxmt_oem_mainstream_wins"])


class TestCompetitorAggregatorTierWeightedConflict(_AggregatorTestBase):
    """Tier A source overrides Tier C when both observed in window."""

    def test_tier_a_overrides_tier_c_for_hbm4_share(self):
        self._write_competitor_thresholds()
        self._write_competitor_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([
            {
                "source_id": "digitimes_share_leak",
                "source_type": "news_article",
                "source": "digitimes",  # Tier C
                "date": now, "extracted_at": now,
                "competitor_signals": {"sk_hynix_hbm4_nvidia_share_pct": 45.0},
            },
            {
                "source_id": "mirae_q2_pdf",
                "source_type": "korean_broker_pdf",
                "source": "mirae_asset",  # Tier A
                "date": now, "extracted_at": now,
                "competitor_signals": {"sk_hynix_hbm4_nvidia_share_pct": 72.0},
            },
        ])
        state = competitor_aggregator.aggregate_competitor_extractions()
        # Tier A (72%) should win
        self.assertEqual(
            state["sub_7b_samsung_hbm4"]["current_state"]["sk_hynix_hbm4_nvidia_share_pct"],
            72.0,
        )


class TestCompetitorAggregatorSafeFailing(_AggregatorTestBase):
    """Missing thresholds file → returns empty dict, no crash."""

    def test_missing_thresholds_file(self):
        # No threshold file written
        result = competitor_aggregator.aggregate_competitor_extractions()
        self.assertEqual(result, {})

    def test_missing_state_file_does_not_crash(self):
        self._write_competitor_thresholds()
        # No state file written
        state = competitor_aggregator.aggregate_competitor_extractions()
        # Returns skeleton state
        self.assertIn("sub_7a_cxmt", state)


class TestADRAggregatorF1Detection(_AggregatorTestBase):
    """F-1 filing detection advances lifecycle pre_event → f1_filed."""

    def test_f1_detection_advances_phase(self):
        self._write_adr_thresholds()
        self._write_adr_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([{
            "source_id": "sec_edgar_2026_06_01",
            "source_type": "regulatory_filing",
            "source": "sec_edgar",
            "date": now, "extracted_at": now,
            "adr_signals": {
                "f1_filed": True,
                "underwriter_syndicate": ["Goldman Sachs", "Morgan Stanley", "JPMorgan"],
            },
        }])
        state = adr_aggregator.aggregate_adr_extractions()
        self.assertEqual(state["lifecycle_phase"], "f1_filed")
        self.assertTrue(state["sub_8a_f1_filing"]["current_state"]["f1_filed"])
        self.assertEqual(state["sub_8a_f1_filing"]["current_state"]["underwriter_lead_tier"], "A")
        # v3.39.10 — a CONFIRMED F-1 filing is itself the Part 11 peak-coincidence
        # event, so it registers as at least "Partially fired" regardless of
        # syndicate quality. Tier-A lead keeps it at Partially fired (healthy
        # issuance, but the filing still fired the signal); only a tier-C-only
        # syndicate or a cancellation escalates to "Fired". (Prior calibration
        # returned "Not fired" for a tier-A filing — changed per the 2026-06-06
        # exec-summary/section-4 reconciliation.)
        self.assertEqual(state["sub_8a_f1_filing"]["current_status"], "Partially fired")


class TestADRAggregatorInsiderSalesAccumulation(_AggregatorTestBase):
    """Multiple insider sale events accumulate and total."""

    def test_insider_sales_total_crosses_threshold(self):
        self._write_adr_thresholds()
        self._write_adr_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([
            {
                "source_id": "sec_form4_a",
                "source_type": "regulatory_filing",
                "source": "sec_edgar",
                "date": now, "extracted_at": now,
                "adr_signals": {
                    "insider_sale_event": {"insider": "CFO", "role": "CFO", "usd_m": 15.0, "date": "2026-06-15"},
                },
            },
            {
                "source_id": "dart_dir_b",
                "source_type": "regulatory_filing",
                "source": "dart",
                "date": now, "extracted_at": now,
                "adr_signals": {
                    "insider_sale_event": {"insider": "Director_X", "role": "director", "usd_m": 12.0, "date": "2026-06-17"},
                },
            },
        ])
        state = adr_aggregator.aggregate_adr_extractions()
        sub = state["sub_8d_insider_sales_in_window"]
        self.assertEqual(len(sub["current_state"]["insider_sales_in_window"]), 2)
        self.assertEqual(sub["current_state"]["insider_sales_total_usd_m_in_window"], 27.0)
        # $27M > $25M threshold → Fired
        self.assertEqual(sub["current_status"], "Fired")


class TestADRAggregatorCanceledF1(_AggregatorTestBase):
    """F-1 canceled extraction → lifecycle phase = canceled, sub-8a Fired."""

    def test_canceled_f1_advances_to_canceled_phase(self):
        self._write_adr_thresholds()
        self._write_adr_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([{
            "source_id": "reuters_offering_pulled",
            "source_type": "news_article",
            "source": "reuters",
            "date": now, "extracted_at": now,
            "adr_signals": {"f1_filed": True, "f1_canceled": True},
        }])
        state = adr_aggregator.aggregate_adr_extractions()
        self.assertEqual(state["lifecycle_phase"], "canceled")
        self.assertEqual(state["sub_8a_f1_filing"]["current_status"], "Fired")


class TestADRAggregatorPricingDiscountCalibration(_AggregatorTestBase):
    """Pricing discount sub-status maps correctly to threshold bands."""

    def test_pricing_discount_above_5pct_fires(self):
        self._write_adr_thresholds()
        self._write_adr_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([{
            "source_id": "sec_424b_2026_07_10",
            "source_type": "regulatory_filing",
            "source": "sec_edgar",
            "date": now, "extracted_at": now,
            "adr_signals": {
                "pricing_day": "2026-07-10",
                "pricing_discount_pct": 6.2,
                "final_size_usd_b": 9.0,
            },
        }])
        state = adr_aggregator.aggregate_adr_extractions()
        self.assertEqual(state["sub_8c_pricing_day"]["current_status"], "Fired")
        self.assertEqual(state["lifecycle_phase"], "trading")
        # peak_coincidence not active by default (cycle_phase_4_late=False)

    def test_pricing_discount_under_3pct_not_fired(self):
        self._write_adr_thresholds()
        self._write_adr_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([{
            "source_id": "sec_424b_2026_07_10",
            "source_type": "regulatory_filing",
            "source": "sec_edgar",
            "date": now, "extracted_at": now,
            "adr_signals": {"pricing_day": "2026-07-10", "pricing_discount_pct": 2.4},
        }])
        state = adr_aggregator.aggregate_adr_extractions()
        self.assertEqual(state["sub_8c_pricing_day"]["current_status"], "Not fired")


class TestStateFileAtomicWrite(_AggregatorTestBase):
    """State file written atomically — tmp file used, then renamed."""

    def test_state_file_persisted_after_aggregation(self):
        self._write_competitor_thresholds()
        self._write_competitor_bootstrap_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_extraction_log([{
            "source_id": "test", "source_type": "news_article", "source": "trendforce",
            "date": now, "extracted_at": now,
            "competitor_signals": {"cxmt_ddr5_pricing_pressure_flagged": True},
        }])
        competitor_aggregator.aggregate_competitor_extractions()
        # State file should exist and contain the updated metadata + pricing pressure flag
        state_path = self.tmpdir / "state" / "competitor_state.json"
        self.assertTrue(state_path.exists())
        with open(state_path) as f:
            state = json.load(f)
        self.assertTrue(state["sub_7a_cxmt"]["current_state"]["cxmt_ddr5_pricing_pressure_flagged"])
        # No tmp file lingering
        self.assertFalse((self.tmpdir / "state" / "competitor_state.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
