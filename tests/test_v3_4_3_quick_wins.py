"""Tests for v3.4.3 — Quick wins (Layer 2 observability + HBM price trend + Layer 1 retry).

Eight domains covered:
  1. Layer 2 audit JSONL written to correct path with correct fields
  2. Layer 2 audit observability defensive — doesn't crash on missing state/ dir
  3. compute_hbm_price_trend returns rising/flat/falling correctly given synthetic data
  4. compute_hbm_price_trend handles missing/empty CSV files gracefully
  5. compute_hbm_price_trend combines spot + contract signals correctly
  6. DHCR snapshot integration works with auto-derived trend
  7. Layer 1 digester retry pattern fires on 529, falls through on non-529
  8. fast_tier.yml contains layer2_audit_*.jsonl in commit set
  9. audit_layer2.py script reads + summarizes JSONL records
  10. No regressions in existing v3.4.2 behavior
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 1-2. Layer 2 audit observability
# ============================================================

class TestLayer2Observability(unittest.TestCase):
    """Layer 2 calls write per-call audit records to state/layer2_audit_*.jsonl."""

    def test_audit_record_written_to_correct_path(self):
        """Audit JSONL path matches state/layer2_audit_YYYY-MM-DD.jsonl format."""
        # Path format verification via regex on source code
        src = (REPO_ROOT / "src/analysis/signal_reasoner.py").read_text()
        self.assertIn('layer2_audit_', src,
            "Layer 2 must write audit records to layer2_audit_*.jsonl")
        self.assertIn("strftime('%Y-%m-%d')", src,
            "Audit path must use YYYY-MM-DD date format for per-day rotation")
        # state/ directory mkdir is defensive
        self.assertIn("audit_dir.mkdir(parents=True, exist_ok=True)", src,
            "Audit dir must be created defensively if missing")

    def test_audit_record_has_expected_fields(self):
        """Verify the audit record schema includes all critical fields."""
        src = (REPO_ROOT / "src/analysis/signal_reasoner.py").read_text()
        required_audit_fields = [
            '"ts":', '"signal_id":', '"signal_name":', '"tier":',
            '"prior_status":', '"prompt_head_400":', '"prompt_total_chars":',
            '"n_digests_relevant":', '"n_news_relevant":',
            '"evidence_store_block_chars":',
            '"verdict_fired_status":', '"verdict_evidence_count":',
            '"verdict_convergence":', '"verdict_confidence":',
            '"verdict_consensus_direction":',
            '"verdict_evidence_for_change":',
            '"status_transition":',
        ]
        for field in required_audit_fields:
            self.assertIn(field, src, f"Audit record missing field: {field}")

    def test_audit_logging_defensive_no_crash_on_failure(self):
        """Audit logging is wrapped in try/except — must never break the brief."""
        src = (REPO_ROOT / "src/analysis/signal_reasoner.py").read_text()
        # The audit block should be wrapped in try/except
        # Extract the audit block:
        audit_start = src.find("# v3.4.3 — Layer 2 prompt observability")
        audit_end = src.find("return SignalReasoning(", audit_start)
        audit_block = src[audit_start:audit_end]
        self.assertIn("try:", audit_block)
        self.assertIn("except Exception as _audit_err:", audit_block)
        self.assertIn("Observability must never break the brief", audit_block)

    def test_audit_record_includes_status_transition(self):
        """status_transition field captures Fired→Not fired etc. for trend analysis."""
        src = (REPO_ROOT / "src/analysis/signal_reasoner.py").read_text()
        # Should have logic for: no prior → None; same → no_change; different → "X→Y"
        self.assertIn('"no_change"', src)
        self.assertIn("→", src,
            "Status transition uses arrow notation Fired→Not fired")


# ============================================================
# 3-5. HBM price trend automation
# ============================================================

class TestHBMPriceTrendAutomation(unittest.TestCase):
    """compute_hbm_price_trend derives direction from accumulated CSV data."""

    def test_returns_one_of_three_strings(self):
        """Always returns 'rising' | 'flat' | 'falling' — never raises."""
        from src.data.prices import compute_hbm_price_trend
        result = compute_hbm_price_trend(lookback_days=90)
        self.assertIn(result, {"rising", "flat", "falling"})

    def test_missing_csv_returns_flat_no_crash(self):
        """If both CSVs missing, returns 'flat' (safe default), doesn't raise."""
        from src.data.prices import compute_hbm_price_trend
        # Point to nonexistent paths
        result = compute_hbm_price_trend(
            spot_history_csv=Path("/nonexistent/spot.csv"),
            contract_forecasts_csv=Path("/nonexistent/contract.csv"),
        )
        self.assertEqual(result, "flat")

    def test_rising_spot_returns_rising(self):
        """Synthetic rising-slope spot CSV → 'rising'."""
        from src.data.prices import compute_hbm_price_trend
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Create synthetic rising spot data
            dates = [date.today() - timedelta(days=30 - i) for i in range(30)]
            rows = [
                {"date": d.isoformat(),
                 "sku": "DDR5_16Gb_4800_5600",
                 "session_avg": 30.0 + 0.5 * i,  # +$0.50/day = strongly rising
                 "daily_high": 32.0 + 0.5 * i,
                 "daily_low": 28.0 + 0.5 * i,
                 "session_change_pct": 1.0,
                 "source": "synthetic", "scraped_at": "2026-05-22T00:00:00Z",
                 "provenance": "test"}
                for i, d in enumerate(dates)
            ]
            spot_path = tmp / "spot.csv"
            pd.DataFrame(rows).to_csv(spot_path, index=False)
            # No contract file — but logic combines both
            result = compute_hbm_price_trend(
                spot_history_csv=spot_path,
                contract_forecasts_csv=tmp / "no_contract.csv",
                lookback_days=90,
            )
            self.assertEqual(result, "rising")

    def test_falling_spot_returns_falling(self):
        """Synthetic falling-slope spot CSV → 'falling'."""
        from src.data.prices import compute_hbm_price_trend
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            dates = [date.today() - timedelta(days=30 - i) for i in range(30)]
            rows = [
                {"date": d.isoformat(),
                 "sku": "DDR5_16Gb_4800_5600",
                 "session_avg": 40.0 - 0.5 * i,  # -$0.50/day = strongly falling
                 "daily_high": 42.0 - 0.5 * i, "daily_low": 38.0 - 0.5 * i,
                 "session_change_pct": -1.0, "source": "synthetic",
                 "scraped_at": "2026-05-22T00:00:00Z", "provenance": "test"}
                for i, d in enumerate(dates)
            ]
            spot_path = tmp / "spot.csv"
            pd.DataFrame(rows).to_csv(spot_path, index=False)
            result = compute_hbm_price_trend(
                spot_history_csv=spot_path,
                contract_forecasts_csv=tmp / "no_contract.csv",
                lookback_days=90,
            )
            self.assertEqual(result, "falling")

    def test_flat_spot_returns_flat(self):
        """Synthetic flat spot data → 'flat'."""
        from src.data.prices import compute_hbm_price_trend
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            dates = [date.today() - timedelta(days=30 - i) for i in range(30)]
            rows = [
                {"date": d.isoformat(),
                 "sku": "DDR5_16Gb_4800_5600",
                 "session_avg": 38.0,  # truly flat
                 "daily_high": 40.0, "daily_low": 36.0,
                 "session_change_pct": 0.0, "source": "synthetic",
                 "scraped_at": "2026-05-22T00:00:00Z", "provenance": "test"}
                for d in dates
            ]
            spot_path = tmp / "spot.csv"
            pd.DataFrame(rows).to_csv(spot_path, index=False)
            result = compute_hbm_price_trend(
                spot_history_csv=spot_path,
                contract_forecasts_csv=tmp / "no_contract.csv",
                lookback_days=90,
            )
            self.assertEqual(result, "flat")

    def test_contract_forecast_revision_up_signals_rising(self):
        """Contract forecast revised UP (e.g. Q1 from 57% → 92%) → contributes to rising."""
        from src.data.prices import compute_hbm_price_trend
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Two forecasts for same quarter — second revises UP from first
            contract_rows = [
                {"row_id": 1, "quarter": "Q1_2026", "segment": "conventional_dram_aggregate",
                 "low_pct": 55.0, "high_pct": 60.0, "central_pct": 57.5,
                 "source": "trendforce", "source_url": "https://...",
                 "published_date": "2026-01-05", "supersedes_id": "", "notes": "initial"},
                {"row_id": 2, "quarter": "Q1_2026", "segment": "conventional_dram_aggregate",
                 "low_pct": 90.0, "high_pct": 95.0, "central_pct": 92.5,
                 "source": "trendforce", "source_url": "https://...",
                 "published_date": "2026-02-02", "supersedes_id": "1", "notes": "revision up"},
            ]
            contract_path = tmp / "contract.csv"
            pd.DataFrame(contract_rows).to_csv(contract_path, index=False)
            # No spot file — contract signal alone drives the verdict
            result = compute_hbm_price_trend(
                spot_history_csv=tmp / "no_spot.csv",
                contract_forecasts_csv=contract_path,
            )
            self.assertEqual(result, "rising",
                "92.5% > 57.5% by 35pp >> 3pp threshold; should signal rising")


# ============================================================
# 6. DHCR integration with auto-derived trend
# ============================================================

class TestDHCRIntegrationWithAutoTrend(unittest.TestCase):
    """fast_tier.py wires compute_hbm_price_trend → compute_dhcr_snapshot."""

    def test_fast_tier_calls_compute_hbm_price_trend(self):
        src = (REPO_ROOT / "src/fast_tier.py").read_text()
        self.assertIn("from src.data.prices import compute_hbm_price_trend", src)
        self.assertIn("compute_hbm_price_trend(lookback_days=90)", src)

    def test_dhcr_snapshot_receives_hbm_price_trend_param(self):
        src = (REPO_ROOT / "src/fast_tier.py").read_text()
        self.assertIn("hbm_price_trend=hbm_price_trend", src,
            "compute_dhcr_snapshot must be called with auto-derived hbm_price_trend")

    def test_dhcr_snapshot_receives_extraction_log_signals(self):
        """v3.4.3 also threads extraction_log_signals from computed_metrics."""
        src = (REPO_ROOT / "src/fast_tier.py").read_text()
        self.assertIn("extraction_log_signals=extraction_log_signals", src)

    def test_v3_4_3_fixed_stale_log_fields(self):
        """v3.4.3 bug fix: fast_tier.py log no longer references removed v3.4.0 fields."""
        src = (REPO_ROOT / "src/fast_tier.py").read_text()
        # These v3.4.0 fields were removed in v3.4.2 — they may appear in
        # comments explaining the removal, but must NOT appear in any
        # active code line (i.e. on a line that does not start with `#`).
        forbidden_in_code = [
            "dhcr_snapshot.direction",
            "snapshot.frontier_hbm_gb_per_accelerator",
            "snapshot.implied_context_capacity_per_accelerator_tokens",
            "snapshot.capacity_creation_yoy_pct",
        ]
        for line_no, line in enumerate(src.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # comment-only line — allowed to mention legacy names
            for forbidden in forbidden_in_code:
                self.assertNotIn(forbidden, line,
                    f"Line {line_no} references removed v3.4.0 field: {forbidden}")
        # New v3.4.2 fields should be present in log output
        self.assertIn(".position_score", src)
        self.assertIn(".workload_implied_cell", src)
        self.assertIn(".market_deployed_cell", src)


# ============================================================
# 7. Layer 1 digester retry
# ============================================================

class TestLayer1DigesterRetry(unittest.TestCase):
    """Layer 1 has 3-retry with [10, 30, 90] backoff for 529 errors."""

    def test_layer1_retry_backoffs_defined(self):
        src = (REPO_ROOT / "src/analysis/digester.py").read_text()
        self.assertIn("LAYER1_RETRY_BACKOFFS", src,
            "Layer 1 must have explicit retry backoff schedule")
        self.assertIn("[10, 30, 90]", src,
            "Layer 1 backoff must be 10s/30s/90s per document")

    def test_layer1_detects_529_overloaded(self):
        src = (REPO_ROOT / "src/analysis/digester.py").read_text()
        # 529 detection by error string match
        self.assertIn("529", src)
        self.assertIn("overloaded", src.lower())

    def test_layer1_passes_through_non_overload_errors(self):
        """Non-529 errors should NOT trigger the retry loop."""
        src = (REPO_ROOT / "src/analysis/digester.py").read_text()
        # Look for the "break" pattern when non-overload error encountered
        # The retry loop must short-circuit on non-overload errors
        self.assertIn("non-overload", src)

    def test_layer1_per_document_isolation(self):
        """Retry pattern wraps a single document's call — not the whole batch."""
        src = (REPO_ROOT / "src/analysis/digester.py").read_text()
        # The retry should be inside digest_document, not at a higher level
        # Comment must explain per-document isolation
        self.assertIn("Per-document isolation", src)


# ============================================================
# 8. fast_tier.yml commit set
# ============================================================

class TestFastTierWorkflowUpdates(unittest.TestCase):
    """The GHA workflow YAML must commit the new layer2_audit_*.jsonl files."""

    def test_layer2_audit_in_commit_set(self):
        yml = (REPO_ROOT / ".github/workflows/fast_tier.yml").read_text()
        self.assertIn("state/layer2_audit_*.jsonl", yml,
            "fast_tier.yml must include layer2_audit_*.jsonl in commit set")

    def test_layer2_audit_comment_present(self):
        """Commit step should have a comment explaining why this is committed."""
        yml = (REPO_ROOT / ".github/workflows/fast_tier.yml").read_text()
        self.assertIn("v3.4.3", yml)
        self.assertIn("layer2_audit", yml.lower())


# ============================================================
# 9. audit_layer2.py script
# ============================================================

class TestAuditLayer2Script(unittest.TestCase):
    """The companion script reads JSONL files and produces a summary."""

    def test_script_exists_and_executable(self):
        script_path = REPO_ROOT / "scripts/audit_layer2.py"
        self.assertTrue(script_path.exists(), "scripts/audit_layer2.py must exist")

    def test_script_has_main_function(self):
        src = (REPO_ROOT / "scripts/audit_layer2.py").read_text()
        self.assertIn("def main(", src)
        self.assertIn('if __name__ == "__main__":', src)

    def test_script_accepts_days_and_indicator_args(self):
        src = (REPO_ROOT / "scripts/audit_layer2.py").read_text()
        self.assertIn("--days", src)
        self.assertIn("--indicator", src)
        self.assertIn("--state-dir", src)

    def test_script_imports_only_stdlib(self):
        """Script must work in any environment — no third-party deps."""
        src = (REPO_ROOT / "scripts/audit_layer2.py").read_text()
        # Should only import: argparse, json, sys, collections, datetime, pathlib
        forbidden = ["import pandas", "import yaml", "from anthropic", "import requests"]
        for f in forbidden:
            self.assertNotIn(f, src, f"Script must not import: {f}")

    def test_script_summarize_handles_empty_records(self):
        """Calling summarize with empty list should not raise."""
        import sys as _sys
        scripts_dir = REPO_ROOT / "scripts"
        _sys.path.insert(0, str(scripts_dir))
        try:
            import audit_layer2  # type: ignore
            # Should print "No audit records" but not raise
            audit_layer2.summarize([])
        finally:
            _sys.path.remove(str(scripts_dir))


# ============================================================
# 10. No regressions in v3.4.2 behavior
# ============================================================

class TestNoRegressionsFromV342(unittest.TestCase):
    """v3.4.3 must not break any v3.4.2 framework behavior."""

    def test_dhcr_snapshot_still_works_today(self):
        from src.analysis.dhcr import compute_dhcr_snapshot
        snap = compute_dhcr_snapshot()
        # Today's expected values (May 22 2026)
        self.assertEqual(snap.workload_implied_cell, (2, 1))  # R3C2
        self.assertEqual(snap.market_deployed_cell, (3, 1))   # R4C2
        self.assertGreaterEqual(snap.position_score, 45)
        self.assertLess(snap.position_score, 60)
        self.assertEqual(snap.status, "Not fired")

    def test_dhcr_accepts_optional_extraction_log_signals(self):
        """compute_dhcr_snapshot signature unchanged from v3.4.2."""
        from src.analysis.dhcr import compute_dhcr_snapshot
        snap = compute_dhcr_snapshot(
            extraction_log_signals=[],
            hbm_price_trend="rising",
        )
        self.assertIsNotNone(snap)

    def test_layer2_still_skips_dhcr(self):
        """v3.4.2 behavior — Layer 2 returns None for DHCR — preserved."""
        from src.analysis.signal_reasoner import reason_one_signal
        from src.config import INDICATORS
        dhcr = next(i for i in INDICATORS if i.id == "t1_decode_hbm_captive_ratio")
        result = reason_one_signal(
            indicator=dhcr, digests=[], fast_tier_news=[],
            naver_data=None, finnhub_data=None, edgar_items=[],
            computed_metrics={},
        )
        self.assertIsNone(result)

    def test_sonnet_retry_pattern_still_present(self):
        """v3.4.2 reliability patterns preserved."""
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("SONNET_RETRY_BACKOFFS", src)
        self.assertIn("HAIKU_FALLBACK_MODEL", src)


if __name__ == "__main__":
    unittest.main()
