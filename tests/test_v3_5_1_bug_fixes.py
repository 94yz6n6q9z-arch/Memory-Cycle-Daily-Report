"""Tests for v3.5.1 — bug fixes for v3.4.14 + v3.5.0 audit findings.

Covers:
  Fix 1: Extraction log key mismatch — aggregators now read "signals" key
         (this is the most important test — it would have caught the
         pre-existing v3.4.5 bug had it existed)
  Fix 2: Scraper does not mislabel 100%-fallback runs as "data_extracted"
  Fix 3: Operative context history pruned by date (18mo) not by count (24)
  Fix 5: Trend prose conditional on current band, not hardcoded C2→C3
  Fix 6: Earnings detection uses regex on fyXX_eps_ field names
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 — Extraction log key mismatch
# This is the integration test that should have existed all along.
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractionLogKeyAlignment:
    """The digester writes architecture signals under e['signals'].
    All four aggregators MUST read them from there.

    Prior to v3.5.1, aggregators read e['architecture_signals'] (the wrong
    key) and silently returned 0 results. This test would have caught the
    bug had it been written when v3.4.5 shipped.
    """

    def _make_digester_log_entry(self, signals_dict: dict) -> dict:
        """Match the exact JSONL record shape that
        digester._log_architecture_signals() writes."""
        return {
            "source_id": "Test Source / 2026-05-24",
            "source_type": "earnings_transcript",
            "published_date_iso": "2026-05-24",
            "extracted_at_iso": "2026-05-24T08:00:00",
            "signals": signals_dict,    # the digester writes under this key
        }

    def test_context_length_aggregator_reads_signals_key(self, tmp_path):
        """The new v3.4.14 aggregator must find context_volume_signals."""
        from src.analysis.context_length_aggregator import _extract_volume_signals
        entry = self._make_digester_log_entry({
            "context_volume_signals": {
                "category": "enterprise_vertical_agents",
                "signal_type": "volume_share_commentary",
                "direction": "growing",
                "magnitude_hint": "tripled",
                "source_company": "microsoft",
            }
        })
        signals = _extract_volume_signals([entry])
        assert len(signals) == 1, (
            "Aggregator failed to read context_volume_signals from the 'signals' key. "
            "This is the v3.5.1 fix for the pre-existing v3.4.5 bug."
        )
        assert signals[0]["category"] == "enterprise_vertical_agents"

    def test_dhcr_arch_aggregator_reads_signals_key(self):
        """The existing v3.4.5 aggregator (pre-existing bug, now fixed)."""
        from src.analysis.dhcr_arch_aggregator import _iter_extraction_log_entries
        # Just check the code now reads e.get("signals") — line-level test
        import inspect
        from src.analysis import dhcr_arch_aggregator
        src = inspect.getsource(dhcr_arch_aggregator)
        assert 'e.get("signals")' in src, "dhcr_arch_aggregator must read 'signals' key (v3.5.1)"
        assert 'extracted_at_iso' in src, "Must accept extracted_at_iso timestamp (v3.5.1)"

    def test_dhcr_revenue_aggregator_reads_signals_key(self):
        import inspect
        from src.analysis import dhcr_revenue_aggregator
        src = inspect.getsource(dhcr_revenue_aggregator)
        assert 'e.get("signals")' in src
        assert 'extracted_at_iso' in src

    def test_dhcr_accelerator_aggregator_reads_signals_key(self):
        import inspect
        from src.analysis import dhcr_accelerator_aggregator
        src = inspect.getsource(dhcr_accelerator_aggregator)
        assert 'e.get("signals")' in src
        assert 'extracted_at_iso' in src

    def test_legacy_architecture_signals_key_still_works(self):
        """Backward compat: if any tooling wrote entries under the OLD
        'architecture_signals' key, they should still parse.
        The fix uses 'signals' OR 'architecture_signals' as fallback."""
        from src.analysis.context_length_aggregator import _extract_volume_signals
        legacy_entry = {
            "source_id": "Legacy / 2026-04-01",
            "published_date_iso": "2026-04-01",
            "extracted_at_iso": "2026-04-01T08:00:00",
            "architecture_signals": {      # OLD key
                "context_volume_signals": {
                    "category": "consumer_chat",
                    "signal_type": "volume_share_commentary",
                    "direction": "shrinking",
                    "magnitude_hint": "declining",
                    "source_company": "openai",
                }
            }
        }
        signals = _extract_volume_signals([legacy_entry])
        assert len(signals) == 1, "Legacy 'architecture_signals' key fallback broken"


class TestExtractionLogTimestampKeys:
    """Aggregators must accept extracted_at_iso / published_date_iso —
    the keys the digester actually writes."""

    def test_aggregator_finds_entries_with_extracted_at_iso(self, tmp_path):
        from src.analysis.context_length_aggregator import _iter_extraction_log_entries
        # Build a real extraction_log file in tmp_path/state/
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        today = date.today().isoformat()
        log_path = state_dir / f"extraction_log_{today}.jsonl"
        entry = {
            "source_id": "Test",
            "source_type": "earnings_transcript",
            "published_date_iso": today,
            "extracted_at_iso": f"{today}T08:00:00",
            "signals": {
                "context_volume_signals": {
                    "category": "coding_agents",
                    "signal_type": "workload_shift_commentary",
                    "direction": "growing",
                    "magnitude_hint": "rapidly",
                    "source_company": "cursor",
                }
            }
        }
        log_path.write_text(json.dumps(entry) + "\n")

        with patch("src.analysis.context_length_aggregator.EXTRACTION_LOG_DIR", state_dir):
            entries = _iter_extraction_log_entries(lookback_days=30)
        assert len(entries) == 1, (
            "Aggregator did not find an entry with extracted_at_iso timestamp. "
            "This is the v3.5.1 fix — pre-fix the aggregator looked for 'date' "
            "or 'extracted_at' (without _iso), which the digester never writes."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 — Scraper status taxonomy with 100%-fallback handling
# ══════════════════════════════════════════════════════════════════════════════

class TestScraperFallbackStatus:
    """When all providers in a category fail but fallback known_value is
    used, the scraper must NOT label the result as 'data_extracted'."""

    def test_all_providers_fail_status_is_not_data_extracted(self):
        from src.data.context_length_scraper import _compute_category_result
        # Two providers, both with URLs (so they'd actually fetch) and bad regex
        providers = [
            {"name": "A", "url": "https://example.com/nonexistent",
             "weight": 0.5, "patterns": [r"NEVER_MATCH"], "known_value": 100_000},
            {"name": "B", "url": "https://example.com/also-nonexistent",
             "weight": 0.5, "patterns": [r"NEVER_MATCH"], "known_value": 200_000},
        ]
        # Mock the fetch helper to simulate fetch_failed for both
        with patch("src.data.context_length_scraper._fetch_page", return_value=None):
            result = _compute_category_result("test_cat", providers)
        # Both providers fell back to known_value at full weight, so context_tokens
        # IS set, but status must reflect the failure
        assert result.status != "data_extracted", (
            "Scraper mislabeled all-fallback run as data_extracted (v3.5.1 fix)"
        )
        assert result.status in ("fetch_failed", "parser_no_match", "no_context_spec_found")

    def test_at_least_one_real_value_returns_data_extracted(self):
        from src.data.context_length_scraper import _compute_category_result
        providers = [
            # Provider A returns real data via static known_value (no URL)
            {"name": "A", "url": None, "weight": 0.6, "patterns": [], "known_value": 128_000},
            {"name": "B", "url": None, "weight": 0.4, "patterns": [], "known_value": 200_000},
        ]
        # Static-only providers (URL=None) count as data_extracted per the
        # _scrape_provider implementation (intentional — static fallbacks for
        # categories without scrape targets like "Others")
        result = _compute_category_result("test_cat", providers)
        assert result.status == "data_extracted"

    def test_no_history_written_on_all_fallback_run(self, tmp_path):
        """If status is no_context_spec_found / parser_no_match, run_context_length_scraper
        must skip the category — not write a fallback-sourced history entry."""
        from src.data.context_length_scraper import run_context_length_scraper
        state_file = tmp_path / "state" / "context_length_state.json"

        # Mock _compute_category_result to return parser_no_match for every category
        from src.data import context_length_scraper as scraper_mod

        def fake_compute(cat, providers):
            from src.data.context_length_scraper import CategoryScrapeResult
            return CategoryScrapeResult(
                category=cat, status="parser_no_match",
                context_tokens=100_000,    # value is set but status says failure
                providers_failed=["mock:parser_no_match"],
            )

        # Bootstrap state so the categories exist
        state_file.parent.mkdir(parents=True, exist_ok=True)
        initial_state = {
            "_metadata": {},
            "typical_context_tokens": {cat: {"current": 50_000, "history": []}
                                       for cat in scraper_mod.CATEGORIES},
            "volume_shares": {"committed": {}, "proposed": None, "proposal_evidence": [],
                              "proposal_status": "none", "proposal_created": None, "history": []},
            "operative_context_history": [],
        }
        state_file.write_text(json.dumps(initial_state))

        with (
            patch.object(scraper_mod, "CONTEXT_STATE_FILE", state_file),
            patch.object(scraper_mod, "CONTEXT_SCRAPER_SENTINEL_FILE",
                         tmp_path / "state" / "sentinel.json"),
            patch.object(scraper_mod, "_compute_category_result", side_effect=fake_compute),
        ):
            scraper_mod.run_context_length_scraper()

        # Verify no category got a new history entry from a failed run
        final_state = json.loads(state_file.read_text())
        for cat, cat_data in final_state["typical_context_tokens"].items():
            history_with_scraper_source = [
                h for h in cat_data.get("history", []) if h.get("source") == "scraper"
            ]
            assert len(history_with_scraper_source) == 0, (
                f"Category {cat} got a history entry from a parser_no_match run "
                f"(v3.5.1 fix violated)"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3 — Operative context history date-based pruning (not count-based)
# ══════════════════════════════════════════════════════════════════════════════

class TestOperativeContextHistoryPruning:
    """fast_tier prunes by date (18 months), not by count (was 24 entries).
    The trend phrase needs ≥ 90 days of history to find a quarter-old
    comparison point."""

    def test_fast_tier_uses_date_based_pruning(self):
        """Inspect fast_tier.py source — assert prune is by date, not count."""
        import inspect
        from src import fast_tier
        src = inspect.getsource(fast_tier)
        # The pre-v3.5.1 code had `[-24:]`. The new code prunes by date.
        # Count occurrences in the operative_context_history block specifically.
        assert "_hist = sorted(_hist, key=lambda h: h.get(\"date\", \"\"))[-24:]" not in src, (
            "fast_tier still uses [-24:] for operative_context_history — v3.5.1 fix not applied"
        )
        # New prune uses days=540 (~18 months)
        assert "days=540" in src, "fast_tier must use date-based prune (540 days)"


# ══════════════════════════════════════════════════════════════════════════════
# Fix 5 — Trend prose conditional on current band
# ══════════════════════════════════════════════════════════════════════════════

class TestTrendProseConditionalBand:
    def _make_dhcr(self, current_band: str, current_tokens: int):
        ref_date = (date.today() - timedelta(days=91)).isoformat()
        today_iso = date.today().isoformat()
        return {
            "operative_context_tokens": current_tokens,
            "context_band_name": current_band,
            "operative_context_history": [
                {"date": ref_date,
                 "operative_context_tokens": int(current_tokens * 0.8),
                 "band": current_band},
                {"date": today_iso,
                 "operative_context_tokens": current_tokens,
                 "band": current_band},
            ],
        }

    def test_c2_prose_mentions_c3_threshold(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C2", 119_000))
        assert "C3" in result and "C2 to C3" in result
        assert "200K" in result

    def test_c3_prose_mentions_c4_threshold_not_c2(self):
        """When matrix is already in C3, prose must NOT say 'shifts from C2 to C3'."""
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C3", 350_000))
        assert "C2 to C3" not in result, (
            "Trend prose still hardcoded to C2→C3 — v3.5.1 fix not applied"
        )
        assert "C4" in result and "C3 to C4" in result

    def test_c5_prose_notes_top_band(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C5", 15_000_000))
        # Top band — no further shift possible
        assert "top band" in result.lower() or "C5" in result

    def test_c1_prose_mentions_c2_threshold(self):
        from src.email_module.template import _build_operative_context_trend
        result = _build_operative_context_trend(self._make_dhcr("C1", 25_000))
        assert "C2" in result and "C1 to C2" in result


# ══════════════════════════════════════════════════════════════════════════════
# Fix 6 — Earnings detection uses regex on fyXX_eps_ pattern
# ══════════════════════════════════════════════════════════════════════════════

class TestEarningsDetectionRegex:
    """The earnings_coverage tag must fire for ANY fyXX_eps_ field, not
    just fy27. As the canonical 'next FY' rolls forward, detection must
    not silently break."""

    def _make_digest(self, key_metrics: dict) -> dict:
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

    def test_fy27_eps_still_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([
            self._make_digest({"fy27_eps_revision_pct": -8.5})
        ])
        assert actions[0].event_type == "earnings_coverage"

    def test_fy28_eps_also_detected(self):
        """Future-year EPS field — the bug we're fixing was hardcoded fy27."""
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([
            self._make_digest({"fy28_eps_revision_pct": -8.5})
        ])
        assert actions[0].event_type == "earnings_coverage", (
            "Detection did not fire for fy28_* — v3.5.1 regex fix not applied"
        )

    def test_fy29_eps_also_detected(self):
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([
            self._make_digest({"fy29_eps_new": 6.50})
        ])
        assert actions[0].event_type == "earnings_coverage"

    def test_fy26_eps_also_detected(self):
        """Past-year EPS revision is still earnings coverage."""
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([
            self._make_digest({"fy26_eps_pct_change": 5.2})
        ])
        assert actions[0].event_type == "earnings_coverage"

    def test_eps_beat_miss_still_detected(self):
        """The non-fyXX earnings fields also still fire detection."""
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([
            self._make_digest({"eps_beat_miss_direction": "beat"})
        ])
        assert actions[0].event_type == "earnings_coverage"

    def test_unrelated_fy_field_not_detected(self):
        """fy27_revenue_pct_change is NOT an EPS field — must not fire."""
        from src.analysis.signal_scoring import actions_from_broker_pdf_digests
        actions = actions_from_broker_pdf_digests([
            self._make_digest({"fy27_revenue_pct_change": 12.0})
        ])
        # Should NOT be tagged as earnings_coverage (no EPS field)
        assert actions[0].event_type == "broker_action"


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end integration test (would have caught Bug #1)
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndExtractionLogToAggregator:
    """Full chain: digester writes extraction_log → aggregator reads it.

    This is the test we should have had from day one. It would have caught
    the pre-existing v3.4.5 key mismatch bug immediately.
    """

    def test_digester_log_format_matches_aggregator_reader(self, tmp_path, monkeypatch):
        """Simulate the digester writing an architecture_signals entry, then
        verify the context_length_aggregator finds it."""
        from src.analysis.digester import _log_architecture_signals
        from src.analysis import context_length_aggregator as cla

        # The digester writes to Path("state") relative to CWD.
        # chdir into tmp_path so the JSONL goes somewhere we control.
        monkeypatch.chdir(tmp_path)

        _log_architecture_signals(
            source_id="MSFT Q2 2026 transcript",
            source_type="earnings_transcript",
            published_date_iso=date.today().isoformat(),
            signals={
                "context_volume_signals": {
                    "category": "enterprise_vertical_agents",
                    "signal_type": "volume_share_commentary",
                    "direction": "growing",
                    "magnitude_hint": "tripled",
                    "source_company": "microsoft",
                },
                "new_frontier_model_release": None,
            },
        )

        state_dir = tmp_path / "state"
        log_files = list(state_dir.glob("extraction_log_*.jsonl"))
        assert len(log_files) == 1, "Digester didn't write the extraction_log entry"

        with patch.object(cla, "EXTRACTION_LOG_DIR", state_dir):
            entries = cla._iter_extraction_log_entries(lookback_days=30)
            assert len(entries) >= 1, "Aggregator didn't find any entries"

            signals = cla._extract_volume_signals(entries)
            assert len(signals) == 1, (
                "End-to-end chain broken: digester wrote a context_volume_signal "
                "but the aggregator can't read it. This is the bug v3.5.1 fixes."
            )
            assert signals[0]["category"] == "enterprise_vertical_agents"
            assert signals[0]["direction"] == "growing"
