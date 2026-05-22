"""Tests for v3.4.2 — DHCR matrix redesign + reliability + CapEx capitalization.

Five domains covered:
  1. Dual-input X-axis math (per-category context × volume share)
  2. 5x5 matrix cell lookup with memory-zone labels
  3. Anticipation premium (+1 Y-band) → market-deployed cell
  4. Position score formula (50/50 weighted)
  5. Status mapping (Fired/Partially fired/Not fired)
  6. Sanity check signals + calibration flag
  7. Bull case / bear case narrative builders
  8. Matrix chart generation
  9. Section 4 title is "Top 6" + DHCR card rendered FIRST
 10. Sonnet retry-with-backoff for 529
 11. Sonnet fallback to Haiku-4.5 when all retries fail
 12. Layer 2 retry-with-backoff for 529
 13. Layer 2 skips DHCR (no Haiku call)
 14. CapEx capitalization in user-facing strings + identifiers preserved
"""

from __future__ import annotations

import re
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 1. Dual-input X-axis math
# ============================================================

class TestDualInputXAxis(unittest.TestCase):
    """compute_operative_context = Σ (context × share/100) across categories."""

    def test_operative_context_is_volume_weighted_sum(self):
        from src.analysis.dhcr import compute_operative_context
        yaml_dict = {
            "categories": {
                "a": {"label": "A", "typical_context_tokens": 100000, "volume_share_pct": 50},
                "b": {"label": "B", "typical_context_tokens": 200000, "volume_share_pct": 50},
            }
        }
        result = compute_operative_context(yaml_dict)
        # 0.5 × 100K + 0.5 × 200K = 150K
        self.assertEqual(result["operative_context_tokens"], 150000)

    def test_share_skewed_to_short_context_pulls_left(self):
        """High share on short-context category pulls operative context left."""
        from src.analysis.dhcr import compute_operative_context
        yaml_dict = {
            "categories": {
                "consumer": {"label": "C", "typical_context_tokens": 30000, "volume_share_pct": 90},
                "enterprise": {"label": "E", "typical_context_tokens": 1000000, "volume_share_pct": 10},
            }
        }
        result = compute_operative_context(yaml_dict)
        # 0.9 × 30K + 0.1 × 1M = 27K + 100K = 127K
        self.assertEqual(result["operative_context_tokens"], 127000)

    def test_enterprise_share_explosion_pulls_right(self):
        """The Raul bull case: enterprise share goes 5% → 30%, context 800K → 5M."""
        from src.analysis.dhcr import compute_operative_context
        # Today: 5% × 800K = 40K contribution from enterprise
        today = compute_operative_context({"categories": {
            "consumer": {"label": "C", "typical_context_tokens": 30000, "volume_share_pct": 50},
            "enterprise": {"label": "E", "typical_context_tokens": 800000, "volume_share_pct": 5},
            "other": {"label": "O", "typical_context_tokens": 50000, "volume_share_pct": 45},
        }})
        # Future: 30% × 5M = 1.5M contribution from enterprise
        future = compute_operative_context({"categories": {
            "consumer": {"label": "C", "typical_context_tokens": 50000, "volume_share_pct": 30},
            "enterprise": {"label": "E", "typical_context_tokens": 5000000, "volume_share_pct": 30},
            "other": {"label": "O", "typical_context_tokens": 100000, "volume_share_pct": 40},
        }})
        # Today: 0.5×30K + 0.05×800K + 0.45×50K = 15K + 40K + 22.5K = 77.5K
        self.assertEqual(today["operative_context_tokens"], 77500)
        # Future: 0.3×50K + 0.3×5M + 0.4×100K = 15K + 1.5M + 40K = 1,555K
        self.assertEqual(future["operative_context_tokens"], 1555000)
        # Operative context grew 20x
        self.assertGreater(future["operative_context_tokens"] / today["operative_context_tokens"], 15)

    def test_volume_share_mismatch_warns_not_crashes(self):
        """If shares don't sum to 100%, function logs warning but still computes."""
        from src.analysis.dhcr import compute_operative_context
        yaml_dict = {"categories": {
            "a": {"label": "A", "typical_context_tokens": 100000, "volume_share_pct": 60},
            "b": {"label": "B", "typical_context_tokens": 200000, "volume_share_pct": 50},  # 110% total
        }}
        result = compute_operative_context(yaml_dict)
        self.assertEqual(result["sum_check_pct"], 110)


# ============================================================
# 2-4. Matrix cell lookup, score, anticipation
# ============================================================

class TestMatrixFundamentals(unittest.TestCase):

    def test_kv_band_classification(self):
        from src.analysis.dhcr import classify_kv_band
        self.assertEqual(classify_kv_band(0.5)["name"], "R1")
        self.assertEqual(classify_kv_band(2.0)["name"], "R2")
        self.assertEqual(classify_kv_band(13.4)["name"], "R3")  # today
        self.assertEqual(classify_kv_band(25.0)["name"], "R4")
        self.assertEqual(classify_kv_band(100.0)["name"], "R5")

    def test_context_band_classification(self):
        from src.analysis.dhcr import classify_context_band
        self.assertEqual(classify_context_band(10000)["name"], "C1")
        self.assertEqual(classify_context_band(114000)["name"], "C2")  # today
        self.assertEqual(classify_context_band(500000)["name"], "C3")
        self.assertEqual(classify_context_band(2000000)["name"], "C4")
        self.assertEqual(classify_context_band(20000000)["name"], "C5")

    def test_memory_zone_grid_dimensions(self):
        from src.analysis.dhcr import MEMORY_ZONES
        self.assertEqual(len(MEMORY_ZONES), 5, "5 rows")
        for row in MEMORY_ZONES:
            self.assertEqual(len(row), 5, "5 cols per row")

    def test_memory_zone_corners_per_memo(self):
        """Top-right = HBM dominance; bottom-left = commodity. Per memo Table 1."""
        from src.analysis.dhcr import MEMORY_ZONES
        # R5C5 (least efficient × longest context) = pure HBM
        self.assertEqual(MEMORY_ZONES[4][4], "HBM")
        # R1C1 (most efficient × shortest context) = DDR commodity
        self.assertEqual(MEMORY_ZONES[0][0], "DDR")
        # R3C2 (today's workload) = LPDDR/HBM transition
        self.assertEqual(MEMORY_ZONES[2][1], "LPDDR/HBM")

    def test_anticipation_premium_one_band_offset(self):
        from src.analysis.dhcr import compute_market_deployed_cell, KV_BANDS
        # R3C2 → R4C2
        market = compute_market_deployed_cell(2, 1)
        self.assertEqual(market, (3, 1))
        # R5C? should cap at R5 (can't go beyond max band)
        market_capped = compute_market_deployed_cell(4, 2)
        self.assertEqual(market_capped, (4, 2))

    def test_position_score_50_50_formula(self):
        from src.analysis.dhcr import compute_position_score, KV_BANDS, CONTEXT_BANDS
        # R3 intensity 0.5; C2 intensity 0.3
        # Score = (0.5 × 0.5 + 0.5 × 0.3) × 100 = 40
        self.assertEqual(compute_position_score(2, 1), 40)
        # R4C2: 0.5 × 0.75 + 0.5 × 0.3 = 0.525 × 100 = 53 (rounded from 52.5)
        # banker's rounding: round(52.5) = 52 in Python (round-half-to-even)
        self.assertIn(compute_position_score(3, 1), [52, 53])
        # R5C5: 0.5 × 0.95 + 0.5 × 0.95 = 95
        self.assertEqual(compute_position_score(4, 4), 95)
        # R1C1: 0.5 × 0.10 + 0.5 × 0.10 = 10
        self.assertEqual(compute_position_score(0, 0), 10)


# ============================================================
# 5. Status mapping
# ============================================================

class TestStatusMapping(unittest.TestCase):

    def test_score_above_65_always_not_fired(self):
        from src.analysis.dhcr import map_status
        for traj in ["bullish", "flat", "bearish"]:
            self.assertEqual(map_status(70, traj)["status"], "Not fired")

    def test_score_below_30_always_fired(self):
        from src.analysis.dhcr import map_status
        for traj in ["bullish", "flat", "bearish"]:
            self.assertEqual(map_status(25, traj)["status"], "Fired")

    def test_transition_zone_with_bullish_volume_not_fired(self):
        from src.analysis.dhcr import map_status
        # Today's case: score 52 + bullish volume trajectory
        result = map_status(52, "bullish")
        self.assertEqual(result["status"], "Not fired")
        self.assertIn("transition", result["reasoning"].lower())

    def test_transition_zone_with_flat_volume_partial(self):
        from src.analysis.dhcr import map_status
        result = map_status(45, "flat")
        self.assertEqual(result["status"], "Partially fired")

    def test_transition_zone_with_bearish_volume_fired(self):
        from src.analysis.dhcr import map_status
        result = map_status(40, "bearish")
        self.assertEqual(result["status"], "Fired")


# ============================================================
# 6. Sanity check
# ============================================================

class TestSanityCheck(unittest.TestCase):

    def test_all_three_signals_confirm_when_no_substitution(self):
        from src.analysis.dhcr import compute_sanity_check
        # Bullish score 70, no substitution news, no decode-off-HBM, rising prices
        result = compute_sanity_check(
            score=70,
            hbm_yaml={"non_hbm_accelerators": [{"vendor": "X", "generation": "y", "ai_compute_share_estimate": "<1%"}]},
            extraction_log_signals=[],
            hbm_price_trend="rising",
        )
        self.assertEqual(result["confirms"], 3)
        self.assertFalse(result["calibration_flag"])

    def test_bullish_score_with_substitution_evidence_flags(self):
        """Score says bullish HBM but substitution news + decode-off-HBM + falling prices → FLAG."""
        from src.analysis.dhcr import compute_sanity_check
        many_subst = [{"cerebras_contract_win": True} for _ in range(10)]
        result = compute_sanity_check(
            score=70,
            hbm_yaml={"non_hbm_accelerators": [
                {"vendor": "X", "generation": "y", "ai_compute_share_estimate": "5%"},
            ]},
            extraction_log_signals=many_subst,
            hbm_price_trend="falling",
        )
        self.assertGreaterEqual(result["contradicts"], 2)
        self.assertTrue(result["calibration_flag"])

    def test_bearish_score_with_no_substitution_flags(self):
        """Score says bearish but signals all confirm HBM → flag (false negative concern)."""
        from src.analysis.dhcr import compute_sanity_check
        result = compute_sanity_check(
            score=20,
            hbm_yaml={"non_hbm_accelerators": []},
            extraction_log_signals=[],
            hbm_price_trend="rising",
        )
        # 3 confirms HBM, but score says bearish → flag
        self.assertEqual(result["confirms"], 3)
        self.assertTrue(result["calibration_flag"])


# ============================================================
# 7. Bull / Bear case narratives
# ============================================================

class TestBullBearNarratives(unittest.TestCase):

    def test_bull_case_references_enterprise_share_growth(self):
        from src.analysis.dhcr import build_bull_case
        snap = {
            "context_categories": [
                {"key": "enterprise_vertical_agents", "volume_share_pct": 5, "context_tokens": 800000},
            ],
        }
        text = build_bull_case(snap)
        self.assertIn("BULL CASE", text)
        self.assertIn("enterprise", text.lower())
        # References the share/context growth path
        self.assertTrue(any(s in text for s in ["25-30%", "share grows"]))
        # References the framework's HBM bit demand multiplication
        self.assertIn("HBM bit demand", text)

    def test_bear_case_references_lpddr_landing(self):
        from src.analysis.dhcr import build_bear_case
        snap = {"computed_at_iso": "2026-05-22T10:00:00"}
        text = build_bear_case(snap)
        self.assertIn("BEAR CASE", text)
        # References LPDDR/hetero outcome
        self.assertIn("LPDDR", text)
        # References sanity-check evidence as today's read
        self.assertIn("substitution", text.lower())


# ============================================================
# 8. Full snapshot integration
# ============================================================

class TestFullDHCRSnapshot(unittest.TestCase):
    """End-to-end: snapshot loads YAMLs, computes everything, sane outputs."""

    def test_snapshot_today_lands_in_HBM_territory_via_anticipation(self):
        from src.analysis.dhcr import compute_dhcr_snapshot
        snap = compute_dhcr_snapshot(as_of_date=date(2026, 5, 22))
        # Today: KV ~14 KB/tok → R3, operative ~114K → C2
        self.assertEqual(snap.workload_implied_cell, (2, 1))
        # Market-deployed: R4C2 (anticipation +1)
        self.assertEqual(snap.market_deployed_cell, (3, 1))
        # Score uses market cell (R4C2)
        self.assertGreaterEqual(snap.position_score, 50)
        self.assertLess(snap.position_score, 60)
        # Status with bullish enterprise volume → Not fired
        self.assertEqual(snap.status, "Not fired")
        # Sanity check confirms today
        self.assertGreaterEqual(snap.sanity_check_confirms, 2)
        self.assertFalse(snap.calibration_flag)

    def test_snapshot_includes_bull_and_bear_narratives(self):
        from src.analysis.dhcr import compute_dhcr_snapshot
        snap = compute_dhcr_snapshot()
        self.assertIn("BULL CASE", snap.bull_case_summary)
        self.assertIn("BEAR CASE", snap.bear_case_summary)
        self.assertTrue(len(snap.narrative_summary) > 50)


# ============================================================
# 9. Section 4 placement + title rename
# ============================================================

class TestSection4Placement(unittest.TestCase):

    def test_section_4_title_is_top_6(self):
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        self.assertIn("Top 6 indicators", src,
            "Section 4 title must read 'Top 6 indicators'")
        self.assertNotIn("Top 5 indicators — deep dive", src,
            "Old 'Top 5 indicators — deep dive' title should be removed")

    def test_dhcr_card_renders_before_top5_section(self):
        """In the build_email_html flow, DHCR card must be appended BEFORE
        _build_top5_section, so it renders first inside section 4."""
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        # Find the section-4 header
        header_idx = src.index("section-4'>4. Top 6")
        # Find DHCR card append and top5 section append AFTER the header
        after_header = src[header_idx:]
        dhcr_call_idx = after_header.index("_build_dhcr_section4_card(")
        top5_call_idx = after_header.index("_build_top5_section(")
        self.assertLess(dhcr_call_idx, top5_call_idx,
            "DHCR card must be appended before top5 section in section 4")

    def test_dhcr_card_renders_with_matrix_chart_kwarg(self):
        """DHCR card now accepts matrix_chart_b64 kwarg (v3.4.2)."""
        src = (REPO_ROOT / "src/email_module/template.py").read_text()
        self.assertIn("matrix_chart_b64", src)

    def test_dhcr_card_includes_bull_case_box(self):
        """v3.4.2: bull/bear cases rendered as colored boxes inside the DHCR card."""
        from src.email_module.template import _build_dhcr_section4_card
        dhcr = {
            "status": "Not fired", "status_color": "#C0DD97", "position_score": 52,
            "workload_implied_zone": "LPDDR/HBM", "market_deployed_zone": "HBM/GDDR",
            "workload_implied_cell": (2, 1), "market_deployed_cell": (3, 1),
            "anticipation_gap_bands": 1,
            "frontier_kv_kb_per_token": 14.0, "frontier_kv_gb_per_1m_tokens": 13.35,
            "frontier_model_name": "DeepSeek V4", "frontier_model_arch": "MLA",
            "kv_band_label": "4-16 GB/1M tok", "kv_yoy_change_pct": -61.0,
            "operative_context_tokens": 114000, "context_band_label": "32-200K",
            "context_categories": [
                {"key": "enterprise_vertical_agents", "label": "Enterprise vertical",
                 "context_tokens": 800000, "volume_share_pct": 5,
                 "contribution_tokens": 40000, "confidence": "low"},
            ],
            "enterprise_ai_run_rate_usd_b": 121.0,
            "enterprise_ai_yoy_growth_pct": 175.0,
            "nvidia_datacenter_yoy_growth_pct": 69.0,
            "volume_index": 100.0, "volume_trajectory": "bullish",
            "hbm_bit_demand_index": 52.0,
            "sanity_check_confirms": 3, "sanity_check_contradicts": 0,
            "calibration_flag": False,
            "sanity_signal_substitution_news_count": 0,
            "sanity_signal_hyperscaler_decode_mentions": 0,
            "sanity_signal_hbm_price_trend": "rising",
            "narrative_summary": "Test narrative.",
            "bull_case_summary": "BULL CASE: enterprise agentic adoption ignites.",
            "bear_case_summary": "BEAR CASE: enterprise agentic stalls.",
            "status_reasoning": "Test reasoning.",
            "substitution_risk_notes": [],
        }
        html = _build_dhcr_section4_card(dhcr, matrix_chart_b64="fakeb64data")
        self.assertIn("BULL CASE", html)
        self.assertIn("BEAR CASE", html)
        self.assertIn("🐂", html)
        self.assertIn("🐻", html)
        # Framework primer explanation
        self.assertIn("FRAMEWORK PRIMER", html)
        # Matrix chart embedded
        self.assertIn("data:image/png;base64,fakeb64data", html)


# ============================================================
# 10-11. Sonnet reliability — retry with backoff + Haiku fallback
# ============================================================

class TestSonnetReliability(unittest.TestCase):

    def test_sonnet_retry_backoffs_30_90_240(self):
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("SONNET_RETRY_BACKOFFS", src,
            "Sonnet must have explicit retry backoff schedule")
        self.assertIn("[30, 90, 240]", src,
            "Backoff schedule must be 30s/90s/240s = ~6min budget")

    def test_sonnet_haiku_fallback_model(self):
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn('HAIKU_FALLBACK_MODEL = "claude-haiku-4-5-20251001"', src,
            "Sonnet must fall back to claude-haiku-4-5 model on persistent 529")

    def test_sonnet_overload_detection_by_string(self):
        """Code must detect 529 by checking for '529' or 'overloaded' in error."""
        src = (REPO_ROOT / "src/analysis/llm_summarizer.py").read_text()
        self.assertIn("529", src)
        self.assertIn("overloaded", src.lower())


# ============================================================
# 12-13. Layer 2 reliability + DHCR skip
# ============================================================

class TestLayer2Reliability(unittest.TestCase):

    def test_layer2_retry_backoffs(self):
        src = (REPO_ROOT / "src/analysis/signal_reasoner.py").read_text()
        self.assertIn("LAYER2_RETRY_BACKOFFS", src)
        self.assertIn("[10, 30, 90]", src,
            "Layer 2 backoff must be 10s/30s/90s per per-signal call")

    def test_layer2_skips_dhcr(self):
        """Layer 2 must early-return None for t1_decode_hbm_captive_ratio
        because DHCR's status is computed deterministically, not Haiku-reasoned."""
        from src.analysis.signal_reasoner import reason_one_signal
        from src.config import INDICATORS
        dhcr_indicator = next(
            (i for i in INDICATORS if i.id == "t1_decode_hbm_captive_ratio"),
            None,
        )
        if dhcr_indicator is None:
            self.skipTest("t1_decode_hbm_captive_ratio not in INDICATORS")
        # Call with minimum required args — should return None immediately
        result = reason_one_signal(
            indicator=dhcr_indicator,
            digests=[], fast_tier_news=[], naver_data=None,
            finnhub_data=None, edgar_items=[], computed_metrics={},
        )
        self.assertIsNone(result, "Layer 2 must skip DHCR (return None)")


# ============================================================
# 14. CapEx capitalization
# ============================================================

class TestCapExCapitalization(unittest.TestCase):

    def test_user_facing_strings_use_capex(self):
        """User-facing fields (descriptions, names) must say 'CapEx' not 'Capex'/'capex'.

        Check by regex on the original (case-preserving) string: no naked
        'capex' or 'Capex' should appear, only 'CapEx' or identifier-like
        patterns ('capex_yoy_pct', 't2_capex_parabolic').
        """
        from src.config import INDICATORS
        capex_indicator = next(
            (i for i in INDICATORS if i.id == "t2_capex_parabolic"),
            None,
        )
        self.assertIsNotNone(capex_indicator)
        # User-facing name uses CapEx
        self.assertIn("CapEx", capex_indicator.name)
        # No naked lowercase 'capex' / 'Capex' in description prose
        # (matches only standalone words, not identifier substrings)
        naked_pattern = re.compile(r'(?<![A-Za-z_])(capex|Capex)(?![A-Za-z_])')
        matches = naked_pattern.findall(capex_indicator.description)
        self.assertEqual(matches, [],
            f"Found naked capex/Capex in description: {matches}. Expected only 'CapEx'.")

    def test_code_identifiers_preserved(self):
        """Code identifiers stay lowercase: t2_capex_parabolic, capex_yoy_pct, etc."""
        from src.config import INDICATORS
        ids = {i.id for i in INDICATORS}
        # The indicator ID itself must stay lowercase per Python convention
        self.assertIn("t2_capex_parabolic", ids)
        self.assertNotIn("t2_CapEx_parabolic", ids)

    def test_no_naked_capex_in_user_facing_yaml(self):
        """Data YAMLs' human-readable notes/descriptions must use CapEx, not capex.

        We allow lowercase 'capex' inside identifier-like fields (keys, IDs)
        but not in prose strings.
        """
        yaml_path = REPO_ROOT / "data/enterprise_ai_revenue.yaml"
        text = yaml_path.read_text()
        # Find prose-y lines (after a colon, ignoring keys-with-underscore patterns)
        prose_lines = [
            line for line in text.splitlines()
            if ":" in line and not line.strip().startswith("#")
        ]
        # Count naked 'capex' / 'Capex' in prose values (right side of colon)
        naked_count = 0
        for line in prose_lines:
            after_colon = line.split(":", 1)[1] if ":" in line else ""
            # Skip if it's purely an identifier-like value (no spaces, ends in _capex or similar)
            if re.search(r'(?<![A-Za-z_])(capex|Capex)(?![A-Za-z_])', after_colon):
                naked_count += 1
        self.assertEqual(naked_count, 0,
            f"Found {naked_count} naked 'capex'/'Capex' in YAML prose — must be 'CapEx'")


# ============================================================
# 15. Backward compatibility — no v3.4.0/v3.4.1 regressions
# ============================================================

class TestNoRegressions(unittest.TestCase):
    """Ensure prior version tests still pass against v3.4.2 changes."""

    def test_dhcr_snapshot_is_dataclass_with_required_fields(self):
        from src.analysis.dhcr import DHCRSnapshot
        from dataclasses import fields
        field_names = {f.name for f in fields(DHCRSnapshot)}
        required = {
            "computed_at_iso", "position_score", "status", "status_color",
            "workload_implied_cell", "market_deployed_cell",
            "operative_context_tokens", "frontier_kv_kb_per_token",
            "narrative_summary", "bull_case_summary", "bear_case_summary",
        }
        self.assertEqual(required - field_names, set(),
            f"DHCRSnapshot missing required fields: {required - field_names}")


if __name__ == "__main__":
    unittest.main()
