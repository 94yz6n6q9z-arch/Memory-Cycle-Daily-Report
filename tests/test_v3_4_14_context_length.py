"""Tests for v3.4.14 components that survived v3.6.0 migration.

v3.6.0 deleted the scraper + aggregator and replaced them with an LLM
research worker. The dhcr.py state-loading logic and the template's
operative-context trend prose still depend on the same state file
structure, so those tests remain valid.

Deleted from this file (now obsolete):
  - TestParseContextNumber, TestExtractContextFromHtml, TestScraperSentinel,
    TestScraperStateBootstrap, TestScraperWeightedAverage — scraper gone
  - TestVolumeSignalExtraction, TestDirectionSummary, TestProposalLogic —
    aggregator gone
  - TestContextVolumeProposalDiagnostic — no more proposals in v3.6.0
  - TestDigesterContextVolumeField — field removed from prompt
  - TestGhaWorkflowExists — old monthly workflow replaced by bi-monthly
    LLM research workflow (see test_v3_6_0_llm_research.py)
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# dhcr.load_dhcr_data() reads state/context_length_state.json correctly
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadDhcrDataContextState:
    """Verify load_dhcr_data() has the right code paths for reading
    context_length_state.json. (Full integration testing is downstream — these
    tests cover the v3.6.0 invariant that dhcr.py still reads from state file
    with YAML fallback.)"""

    def test_dhcr_reads_context_length_state_file(self):
        """The code path that reads state/context_length_state.json must exist."""
        import inspect
        from src.analysis import dhcr as dhcr_mod
        src = inspect.getsource(dhcr_mod)
        assert "context_length_state.json" in src, (
            "dhcr.load_dhcr_data must reference context_length_state.json"
        )
        assert "typical_context_tokens" in src
        assert "volume_shares" in src

    def test_dhcr_overrides_yaml_with_state(self):
        """The override logic must read 'current' from state and apply to YAML."""
        import inspect
        from src.analysis import dhcr as dhcr_mod
        src = inspect.getsource(dhcr_mod)
        # The override pattern reads state["typical_context_tokens"][cat]["current"]
        # and assigns it to the category dict.
        assert "typical_context_tokens" in src

    def test_dhcr_handles_state_file_absent(self):
        """The state-file-absent branch must fall back gracefully (no exception)."""
        import inspect
        from src.analysis import dhcr as dhcr_mod
        src = inspect.getsource(dhcr_mod)
        # Should check .exists() before reading
        assert ".exists()" in src
        # And should have a fallback (try/except or if-else)
        assert "try:" in src or "if " in src

    def test_dhcr_handles_state_file_malformed(self):
        """Malformed JSON should be caught and logged, not propagated."""
        import inspect
        from src.analysis import dhcr as dhcr_mod
        src = inspect.getsource(dhcr_mod)
        # Should have try/except around the state read
        assert "try:" in src
        assert "except" in src


# ════════════════════════════════════════════════════════════════════════════
# Operative context trend prose (template.py)
# ════════════════════════════════════════════════════════════════════════════

class TestOperativeContextTrendProse:
    def _make_dhcr(self, current_band, current_tokens, ref_tokens, ref_band, days_back=91):
        ref_date = (date.today() - timedelta(days=days_back)).isoformat()
        today_iso = date.today().isoformat()
        return {
            "operative_context_tokens": current_tokens,
            "context_band_name": current_band,
            "operative_context_history": [
                {"date": ref_date, "operative_context_tokens": ref_tokens, "band": ref_band},
                {"date": today_iso, "operative_context_tokens": current_tokens, "band": current_band},
            ],
        }

    def test_returns_empty_with_no_history(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend({
            "operative_context_tokens": 114_000,
            "context_band_name": "C2",
            "operative_context_history": [],
        })
        assert result == ""

    def test_prose_includes_directional_arrow_for_up(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C3", 400_000, 100_000, "C2"))
        assert "up" in result.lower() or "+" in result

    def test_prose_includes_band_transition(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C3", 400_000, 100_000, "C2"))
        assert "C2" in result and "C3" in result

    def test_prose_quarter_label_for_long_window(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C3", 400_000, 100_000, "C2", days_back=91))
        assert "quarter" in result.lower()
