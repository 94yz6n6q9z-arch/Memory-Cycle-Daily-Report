"""Tests for v3.5.1 bug fixes — surviving subset after v3.6.0 migration.

v3.6.0 deleted context_length_scraper.py and context_length_aggregator.py.
These tests targeted bugs in those modules:
  - TestExtractionLogKeyAlignment (context_length_aggregator portion) — DELETED
  - TestExtractionLogTimestampKeys — DELETED
  - TestScraperFallbackStatus — DELETED
  - TestEndToEndExtractionLogToAggregator — DELETED

Tests that remain valid (targeting code paths still in the codebase):
  - DHCR aggregator key alignment (arch/revenue/accelerator) — pre-existing
    v3.4.5 bug, still relevant since those aggregators still exist
  - Trend prose conditional band (template.py) — unchanged
  - Earnings detection regex (signal_scoring.py) — unchanged
  - Operative context history pruning (fast_tier.py) — unchanged
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# DHCR aggregators (arch/revenue/accelerator) — pre-existing v3.4.5 bug, fixed
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractionLogKeyAlignmentDhcrAggregators:
    """v3.4.5 aggregators must read e.get("signals") not e.get("architecture_signals")."""

    def test_dhcr_arch_aggregator_reads_signals_key(self):
        import inspect
        from src.analysis import dhcr_arch_aggregator
        src = inspect.getsource(dhcr_arch_aggregator)
        assert 'e.get("signals")' in src
        assert "extracted_at_iso" in src

    def test_dhcr_revenue_aggregator_reads_signals_key(self):
        import inspect
        from src.analysis import dhcr_revenue_aggregator
        src = inspect.getsource(dhcr_revenue_aggregator)
        assert 'e.get("signals")' in src
        assert "extracted_at_iso" in src

    def test_dhcr_accelerator_aggregator_reads_signals_key(self):
        import inspect
        from src.analysis import dhcr_accelerator_aggregator
        src = inspect.getsource(dhcr_accelerator_aggregator)
        assert 'e.get("signals")' in src
        assert "extracted_at_iso" in src


# ══════════════════════════════════════════════════════════════════════════════
# Operative context history pruning (fast_tier.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestOperativeContextHistoryPruning:
    """v3.5.1 fixed [-24:] count-based prune to date-based 540-day prune."""

    def test_fast_tier_uses_date_based_pruning(self):
        import inspect
        from src import fast_tier
        src = inspect.getsource(fast_tier)
        assert '_hist = sorted(_hist, key=lambda h: h.get("date", ""))[-24:]' not in src
        assert "days=540" in src


# ══════════════════════════════════════════════════════════════════════════════
# Trend prose conditional on current band
# ══════════════════════════════════════════════════════════════════════════════

class TestTrendProseConditionalBand:
    def _make_dhcr(self, current_band, current_tokens):
        ref_date = (date.today() - timedelta(days=91)).isoformat()
        today_iso = date.today().isoformat()
        return {
            "operative_context_tokens": current_tokens,
            "context_band_name": current_band,
            "operative_context_history": [
                {"date": ref_date, "operative_context_tokens": int(current_tokens * 0.8), "band": current_band},
                {"date": today_iso, "operative_context_tokens": current_tokens, "band": current_band},
            ],
        }

    def test_c2_prose_mentions_c3_threshold(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C2", 119_000))
        assert "C2 to C3" in result and "200K" in result

    def test_c3_prose_mentions_c4_threshold_not_c2(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C3", 350_000))
        assert "C2 to C3" not in result
        assert "C3 to C4" in result

    def test_c5_prose_notes_top_band(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C5", 15_000_000))
        assert "top band" in result.lower() or "no further" in result.lower()

    def test_c1_prose_mentions_c2_threshold(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C1", 25_000))
        assert "C1 to C2" in result


# ══════════════════════════════════════════════════════════════════════════════
# Earnings detection regex (signal_scoring.py) — unchanged from v3.5.1
# ══════════════════════════════════════════════════════════════════════════════

class TestEarningsDetectionRegex:
    def _make_digest(self, key_metrics):
        return {
            "source_type": "korean_broker_pdf",
            "published_date_iso": "2026-07-30",
            "key_metrics": key_metrics,
            "target_price_action": {
                "broker": "Mirae Asset", "ticker": "000660.KS",
                "new_pt": 230000, "prior_pt": 250000, "direction": "lowered",
            },
            "key_quotes": [],
        }

    def test_fy27_eps_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([self._make_digest({"fy27_eps_revision_pct": -8.5})])
        assert actions[0].event_type == "earnings_coverage"

    def test_fy28_eps_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([self._make_digest({"fy28_eps_revision_pct": -8.5})])
        assert actions[0].event_type == "earnings_coverage"

    def test_fy29_eps_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([self._make_digest({"fy29_eps_new": 6.50})])
        assert actions[0].event_type == "earnings_coverage"

    def test_fy26_eps_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([self._make_digest({"fy26_eps_pct_change": 5.2})])
        assert actions[0].event_type == "earnings_coverage"

    def test_eps_beat_miss_still_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([self._make_digest({"eps_beat_miss_direction": "beat"})])
        assert actions[0].event_type == "earnings_coverage"

    def test_unrelated_fy_field_not_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([self._make_digest({"fy27_revenue_pct_change": 12.0})])
        assert actions[0].event_type == "broker_action"
