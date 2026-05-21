"""Tests for v3.4.0 — Decode HBM Captive Ratio (DHCR) indicator.

Covers:
  1. YAML structural integrity (all three reference files parse + required keys)
  2. KV/token math (GQA, MLA, FP16/FP8/INT4 quantization)
  3. Frontier KV computation (correct model selected for given date)
  4. HBM-per-accelerator computation
  5. Implied context capacity math
  6. Enterprise demand aggregation
  7. Composite direction signal (5-level enum + score)
  8. Trajectory computation (11-quarter bootstrap)
  9. State persistence (load + save round-trip)
  10. Snippet rendering (section 3 card + section 4 deep dive)
  11. Chart generation (smoke test only — matplotlib output)
  12. Integration: build_email_html end-to-end with DHCR
  13. Config registration (indicator present + tier + ID stable)
  14. Sonnet prompt integration (computed_metrics rendering includes DHCR block)
  15. Workflow file persistence (extraction_log + dhcr_state in git add)
"""

from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from src.analysis.dhcr import (
    DHCRSnapshot,
    compute_dhcr_snapshot,
    compute_dhcr_trajectory,
    compute_enterprise_demand,
    compute_frontier_hbm_per_accelerator,
    compute_frontier_kv,
    compute_kv_per_token,
    compute_capacity_creation_yoy,
    compute_direction,
    compute_hbm_yoy_change,
    compute_implied_context_capacity,
    compute_kv_yoy_change,
    load_dhcr_data,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


# ============================================================
# 1. YAML structural integrity
# ============================================================

class TestYAMLStructuralIntegrity(unittest.TestCase):
    """All three reference YAMLs must parse and carry required keys.

    These tests act as schema enforcement — any future edit that breaks
    the structure will trip here before reaching production.
    """

    def test_inference_arch_reference_parses(self):
        path = DATA_DIR / "inference_arch_reference.yaml"
        self.assertTrue(path.exists(), f"Missing: {path}")
        data = yaml.safe_load(path.read_text())
        self.assertIn("open_models", data)
        self.assertIn("closed_models", data)
        self.assertGreaterEqual(len(data["open_models"]), 10,
            "Need at least 10 open-architecture models for trajectory")

    def test_hbm_per_accelerator_parses(self):
        path = DATA_DIR / "hbm_per_accelerator.yaml"
        self.assertTrue(path.exists(), f"Missing: {path}")
        data = yaml.safe_load(path.read_text())
        self.assertIn("nvidia", data)
        self.assertIn("amd", data)
        self.assertIn("non_hbm_accelerators", data)
        self.assertGreaterEqual(len(data["nvidia"]), 5)

    def test_enterprise_ai_revenue_parses(self):
        path = DATA_DIR / "enterprise_ai_revenue.yaml"
        self.assertTrue(path.exists(), f"Missing: {path}")
        data = yaml.safe_load(path.read_text())
        for key in ("microsoft_azure_ai", "nvidia_datacenter",
                    "hyperscaler_ai_capex_aggregate"):
            self.assertIn(key, data, f"Missing key: {key}")

    def test_arch_entries_have_required_fields(self):
        data = yaml.safe_load((DATA_DIR / "inference_arch_reference.yaml").read_text())
        for m in data["open_models"]:
            self.assertIn("name", m)
            self.assertIn("release_date", m)
            self.assertIn("architecture_family", m)
            self.assertIn("kv_kb_per_token", m, f"{m['name']} missing kv_kb_per_token")

    def test_hbm_entries_have_required_fields(self):
        data = yaml.safe_load((DATA_DIR / "hbm_per_accelerator.yaml").read_text())
        for vendor_key in ("nvidia", "amd"):
            for g in data[vendor_key]:
                self.assertIn("generation", g)
                self.assertIn("hbm_gb_per_chip", g)
                self.assertIn("release_date", g)

    def test_revenue_entries_have_quarter_field(self):
        data = yaml.safe_load((DATA_DIR / "enterprise_ai_revenue.yaml").read_text())
        for entry in data["nvidia_datacenter"]:
            self.assertIn("quarter", entry)
            self.assertRegex(entry["quarter"], r"^\d{4}-Q[1-4]$",
                f"Invalid quarter format: {entry['quarter']}")


# ============================================================
# 2. KV/token math — exact formulas
# ============================================================

class TestKVPerTokenMath(unittest.TestCase):
    """Verify KV cache math against known-good reference points."""

    def test_gqa_fp16_llama2_70b(self):
        """Llama 2 70B: GQA, 80 layers, 8 KV heads, 128 head_dim, FP16."""
        entry = {
            "architecture_family": "GQA",
            "num_layers": 80,
            "num_kv_heads": 8,
            "head_dim": 128,
            "bytes_per_element": 2,
            "compression_factor": 1.0,
        }
        # 2 × 80 × 8 × 128 × 2 = 327,680 bytes = 320 KB
        self.assertAlmostEqual(compute_kv_per_token(entry), 320.0, places=1)

    def test_gqa_fp8_llama3_70b(self):
        """FP8 halves KV from FP16 baseline."""
        entry = {
            "architecture_family": "GQA",
            "num_layers": 80,
            "num_kv_heads": 8,
            "head_dim": 128,
            "bytes_per_element": 1,
            "compression_factor": 1.0,
        }
        self.assertAlmostEqual(compute_kv_per_token(entry), 160.0, places=1)

    def test_yaml_precomputed_value_trusted(self):
        """If kv_kb_per_token is in YAML, use it directly (verified externally)."""
        entry = {"kv_kb_per_token": 15.0, "architecture_family": "MLA"}
        self.assertEqual(compute_kv_per_token(entry), 15.0)

    def test_mla_smaller_than_gqa_equivalent(self):
        """MLA must produce smaller KV than GQA at same model size."""
        gqa_entry = {
            "architecture_family": "GQA",
            "num_layers": 60, "num_kv_heads": 8, "head_dim": 128,
            "bytes_per_element": 1, "compression_factor": 1.0,
        }
        mla_entry = {
            "architecture_family": "MLA",
            "num_layers": 60,
            "mla_kv_lora_rank": 512,
            "mla_qk_rope_head_dim": 64,
            "mla_num_heads": 128,
            "bytes_per_element": 1,
            "compression_factor": 1.0,
        }
        self.assertLess(compute_kv_per_token(mla_entry),
                        compute_kv_per_token(gqa_entry))


# ============================================================
# 3. Frontier KV computation (date-aware)
# ============================================================

class TestFrontierKV(unittest.TestCase):

    def test_frontier_kv_for_today(self):
        data = load_dhcr_data()
        frontier = compute_frontier_kv(data["arch"], as_of_date=date(2026, 5, 21))
        # Today's frontier should be the most efficient MLA model
        self.assertEqual(frontier["arch_family"], "MLA")
        self.assertLessEqual(frontier["kv_kb_per_token"], 50,
            "Today's frontier should be sub-50 KB/tok")

    def test_frontier_kv_changes_by_date(self):
        """Frontier at 2024-Q1 should be different from today."""
        data = load_dhcr_data()
        early = compute_frontier_kv(data["arch"], as_of_date=date(2024, 1, 1))
        now = compute_frontier_kv(data["arch"], as_of_date=date(2026, 5, 21))
        # Early frontier should be much higher KV (less efficient) than now
        self.assertGreater(early["kv_kb_per_token"], now["kv_kb_per_token"])

    def test_frontier_kv_excludes_future_models(self):
        """A model released after as_of_date must not be the frontier."""
        data = load_dhcr_data()
        frontier = compute_frontier_kv(data["arch"], as_of_date=date(2024, 7, 1))
        # As of mid-2024, DeepSeek V3 (Dec 2024) shouldn't be the frontier
        self.assertNotIn("V3", frontier["model_name"])


# ============================================================
# 4. HBM per accelerator
# ============================================================

class TestFrontierHBM(unittest.TestCase):

    def test_frontier_hbm_for_today(self):
        data = load_dhcr_data()
        frontier = compute_frontier_hbm_per_accelerator(
            data["hbm"], as_of_date=date(2026, 5, 21)
        )
        self.assertGreaterEqual(frontier["hbm_gb_per_chip"], 192,
            "Today's frontier should be >=192GB (B200-class minimum)")

    def test_frontier_hbm_excludes_projected_by_default(self):
        """Projected entries shouldn't show as today's frontier."""
        data = load_dhcr_data()
        frontier = compute_frontier_hbm_per_accelerator(
            data["hbm"], as_of_date=date(2026, 5, 21), include_projected=False
        )
        # Rubin etc. are projected for 2026-Q4 or later; should be excluded
        self.assertNotIn("Rubin", frontier["accelerator_name"])

    def test_hbm_grows_over_time(self):
        """HBM-per-accelerator should be larger today than 18 months ago."""
        data = load_dhcr_data()
        now = compute_frontier_hbm_per_accelerator(data["hbm"], as_of_date=date(2026, 5, 21))
        old = compute_frontier_hbm_per_accelerator(data["hbm"], as_of_date=date(2024, 8, 1))
        self.assertGreater(now["hbm_gb_per_chip"], old["hbm_gb_per_chip"])


# ============================================================
# 5. Implied context capacity math
# ============================================================

class TestImpliedContextCapacity(unittest.TestCase):

    def test_capacity_calculation(self):
        """80 GB HBM × 320 KB/tok = ~256K tokens."""
        cap = compute_implied_context_capacity(
            frontier_kv_kb=320.0, frontier_hbm_gb=80.0
        )
        # 80 × 1024 / 320 = 256 → 256000 tokens
        self.assertEqual(cap, 256_000)

    def test_capacity_today(self):
        """At today's frontier (14 KB × 576 GB GB300) should be ~42M tokens."""
        cap = compute_implied_context_capacity(
            frontier_kv_kb=14.0, frontier_hbm_gb=576.0
        )
        # 576 × 1024 / 14 = 42,130 → 42,130,000 tokens
        self.assertGreater(cap, 40_000_000)
        self.assertLess(cap, 45_000_000)

    def test_capacity_zero_kv_safe(self):
        """Zero KV would divide by zero — handle gracefully."""
        cap = compute_implied_context_capacity(frontier_kv_kb=0.0, frontier_hbm_gb=100.0)
        self.assertEqual(cap, 0)


# ============================================================
# 6. Capacity creation YoY composite
# ============================================================

class TestCapacityCreationYoY(unittest.TestCase):

    def test_capacity_creation_combines_correctly(self):
        """HBM +50% YoY + KV -40% YoY → capacity creation +90% YoY."""
        result = compute_capacity_creation_yoy(kv_yoy_pct=-40.0, hbm_yoy_pct=50.0)
        self.assertEqual(result, 90.0)

    def test_none_inputs_yield_none(self):
        self.assertIsNone(compute_capacity_creation_yoy(None, 50.0))
        self.assertIsNone(compute_capacity_creation_yoy(-40.0, None))


# ============================================================
# 7. Enterprise demand aggregation
# ============================================================

class TestEnterpriseDemand(unittest.TestCase):

    def test_aggregate_run_rate_sums_components(self):
        data = load_dhcr_data()
        demand = compute_enterprise_demand(data["rev"], as_of_date=date(2026, 5, 21))
        self.assertGreater(demand["aggregate_run_rate_usd_b"], 50,
            "Aggregate run-rate should be substantial in 2026")

    def test_yoy_growth_is_positive(self):
        """In current macro regime enterprise AI YoY should be > 50%."""
        data = load_dhcr_data()
        demand = compute_enterprise_demand(data["rev"], as_of_date=date(2026, 5, 21))
        self.assertIsNotNone(demand["weighted_yoy_growth_pct"])
        self.assertGreater(demand["weighted_yoy_growth_pct"], 50)


# ============================================================
# 8. Composite direction signal
# ============================================================

class TestDirectionSignal(unittest.TestCase):

    def test_strongly_bullish_when_demand_far_outpaces_supply(self):
        result = compute_direction(
            capacity_creation_yoy=50.0,
            enterprise_yoy=200.0,
            nvidia_dc_yoy=70.0,
        )
        # demand_pull = 0.6*200 + 0.4*70 = 148. delta = 148 - 50 = +98 → strongly_bullish
        self.assertEqual(result["direction"], "strongly_bullish_hbm")
        self.assertGreater(result["direction_score"], 0.5)

    def test_strongly_bearish_when_supply_far_outpaces_demand(self):
        result = compute_direction(
            capacity_creation_yoy=200.0,  # massive capacity creation
            enterprise_yoy=30.0,           # weak demand
            nvidia_dc_yoy=20.0,
        )
        # demand = 0.6*30 + 0.4*20 = 26. delta = 26 - 200 = -174 → strongly_bearish
        self.assertEqual(result["direction"], "strongly_bearish_hbm")
        self.assertLess(result["direction_score"], -0.5)

    def test_balanced_when_rates_match(self):
        result = compute_direction(
            capacity_creation_yoy=100.0,
            enterprise_yoy=100.0,
            nvidia_dc_yoy=100.0,
        )
        # demand = 100. delta = 0 → balanced
        self.assertEqual(result["direction"], "balanced")

    def test_insufficient_data_returns_balanced_low_confidence(self):
        result = compute_direction(
            capacity_creation_yoy=None, enterprise_yoy=None, nvidia_dc_yoy=None,
        )
        self.assertEqual(result["direction"], "balanced")
        self.assertEqual(result["confidence"], "low")


# ============================================================
# 9. Trajectory computation
# ============================================================

class TestTrajectory(unittest.TestCase):

    def test_trajectory_has_multiple_quarters(self):
        traj = compute_dhcr_trajectory(quarters_back=11)
        self.assertGreaterEqual(len(traj), 8,
            "Should have at least 8 quarters of bootstrapped data")

    def test_trajectory_chronological(self):
        traj = compute_dhcr_trajectory(quarters_back=11)
        dates = [t["as_of_date"] for t in traj]
        self.assertEqual(dates, sorted(dates), "Trajectory should be chronological")

    def test_trajectory_shows_capacity_expansion(self):
        """Capacity should grow over time as KV shrinks and HBM grows."""
        traj = compute_dhcr_trajectory(quarters_back=11)
        if len(traj) >= 2:
            self.assertGreater(
                traj[-1]["implied_context_capacity_tokens"],
                traj[0]["implied_context_capacity_tokens"],
                "Latest capacity should exceed oldest",
            )


# ============================================================
# 10. Full snapshot computation
# ============================================================

class TestFullSnapshot(unittest.TestCase):

    def test_snapshot_today_strongly_bullish(self):
        snap = compute_dhcr_snapshot(as_of_date=date(2026, 5, 21))
        self.assertIsInstance(snap, DHCRSnapshot)
        # Today's read: enterprise growing 200%+, capacity creating 100%+,
        # delta should be strongly bullish for HBM
        self.assertEqual(snap.direction, "strongly_bullish_hbm")

    def test_snapshot_carries_all_fields(self):
        snap = compute_dhcr_snapshot(as_of_date=date(2026, 5, 21))
        d = asdict(snap)
        required = [
            "frontier_kv_kb_per_token", "frontier_model_name", "frontier_hbm_gb_per_accelerator",
            "frontier_accelerator_name", "implied_context_capacity_per_accelerator_tokens",
            "kv_yoy_change_pct", "hbm_per_accelerator_yoy_change_pct",
            "capacity_creation_yoy_pct", "enterprise_ai_aggregate_run_rate_usd_b",
            "enterprise_ai_yoy_growth_pct", "nvidia_datacenter_yoy_growth_pct",
            "direction", "direction_score", "dominant_force", "confidence",
            "narrative_summary",
        ]
        for f in required:
            self.assertIn(f, d, f"Missing field: {f}")

    def test_narrative_summary_non_empty(self):
        snap = compute_dhcr_snapshot(as_of_date=date(2026, 5, 21))
        self.assertGreater(len(snap.narrative_summary), 100,
            "Narrative summary should be substantive")

    def test_substitution_risk_notes_present(self):
        snap = compute_dhcr_snapshot(as_of_date=date(2026, 5, 21))
        self.assertGreater(len(snap.substitution_risk_notes), 0,
            "Should flag at least Cerebras / Groq")


# ============================================================
# 11. State persistence round-trip
# ============================================================

class TestStatePersistence(unittest.TestCase):

    def setUp(self):
        from src.state import DHCR_STATE_FILE
        # Snapshot existing state to restore later
        self._original = None
        if DHCR_STATE_FILE.exists():
            self._original = DHCR_STATE_FILE.read_text()
        self.state_file = DHCR_STATE_FILE

    def tearDown(self):
        # Restore original or delete if none existed
        if self._original is not None:
            self.state_file.write_text(self._original)
        elif self.state_file.exists():
            self.state_file.unlink()

    def test_save_and_load_round_trip(self):
        from src.state import save_dhcr_state, load_dhcr_prior_state
        snap = compute_dhcr_snapshot(as_of_date=date(2026, 5, 21))
        save_dhcr_state(asdict(snap))
        loaded = load_dhcr_prior_state()
        self.assertIsNotNone(loaded)
        self.assertIn("current", loaded)
        self.assertEqual(
            loaded["current"]["direction"],
            snap.direction,
        )

    def test_history_caps_at_12(self):
        from src.state import save_dhcr_state
        # Write 15 snapshots; should retain only last 12
        for i in range(15):
            save_dhcr_state({"direction": f"test_{i}", "computed_at_iso": f"2026-01-{i+1:02d}"})
        with open(self.state_file) as f:
            data = json.load(f)
        self.assertLessEqual(len(data.get("recent_snapshots", [])), 12)


# ============================================================
# 12. Rendering integration
# ============================================================

class TestRendering(unittest.TestCase):

    def test_section3_snippet_renders(self):
        from src.email_module.template import _build_dhcr_section3_snippet
        snap = compute_dhcr_snapshot()
        dhcr_dict = asdict(snap)
        html = _build_dhcr_section3_snippet(dhcr_dict)
        self.assertIn("STRONGLY BULLISH HBM", html)
        self.assertIn("Frontier KV:", html)
        self.assertIn("Enterprise AI YoY:", html)

    def test_section3_snippet_empty_on_none(self):
        from src.email_module.template import _build_dhcr_section3_snippet
        self.assertEqual(_build_dhcr_section3_snippet(None), "")
        self.assertEqual(_build_dhcr_section3_snippet({}), "")

    def test_section4_card_renders(self):
        from src.email_module.template import _build_dhcr_section4_card
        snap = compute_dhcr_snapshot()
        dhcr_dict = asdict(snap)
        html = _build_dhcr_section4_card(dhcr_dict)
        self.assertIn("Decode HBM Captive Ratio — direction monitor", html)
        self.assertIn("SUPPLY-SIDE", html)
        self.assertIn("DEMAND-PULL", html)
        self.assertIn("Mental model", html)

    def test_section4_card_handles_no_chart(self):
        """Card must render even when chart_b64 is None."""
        from src.email_module.template import _build_dhcr_section4_card
        snap = compute_dhcr_snapshot()
        html = _build_dhcr_section4_card(asdict(snap), chart_b64=None)
        self.assertGreater(len(html), 1000)
        self.assertNotIn("data:image/png;base64,None", html)

    def test_direction_colors_distinct(self):
        from src.email_module.template import _build_dhcr_section3_snippet
        # Strongly bullish → green
        bullish = _build_dhcr_section3_snippet({
            "direction": "strongly_bullish_hbm",
            "frontier_kv_kb_per_token": 10, "frontier_hbm_gb_per_accelerator": 500,
            "implied_context_capacity_per_accelerator_tokens": 50_000_000,
            "kv_yoy_change_pct": -50, "hbm_per_accelerator_yoy_change_pct": 100,
            "capacity_creation_yoy_pct": 150, "enterprise_ai_yoy_growth_pct": 250,
            "nvidia_datacenter_yoy_growth_pct": 80,
        })
        # Strongly bearish → red
        bearish = _build_dhcr_section3_snippet({
            "direction": "strongly_bearish_hbm",
            "frontier_kv_kb_per_token": 10, "frontier_hbm_gb_per_accelerator": 500,
            "implied_context_capacity_per_accelerator_tokens": 50_000_000,
            "kv_yoy_change_pct": -50, "hbm_per_accelerator_yoy_change_pct": 100,
            "capacity_creation_yoy_pct": 200, "enterprise_ai_yoy_growth_pct": 20,
            "nvidia_datacenter_yoy_growth_pct": 10,
        })
        self.assertIn("#2C7A2C", bullish)  # deep green
        self.assertIn("#B91C1C", bearish)  # red


# ============================================================
# 13. Chart generation (smoke)
# ============================================================

class TestChartGeneration(unittest.TestCase):

    def test_chart_generates_for_full_trajectory(self):
        from src.analysis.chart_generator import generate_dhcr_trajectory_chart
        traj = compute_dhcr_trajectory(quarters_back=11)
        b64 = generate_dhcr_trajectory_chart(traj)
        self.assertIsNotNone(b64)
        self.assertGreater(len(b64), 1000)

    def test_chart_returns_none_for_empty_trajectory(self):
        from src.analysis.chart_generator import generate_dhcr_trajectory_chart
        self.assertIsNone(generate_dhcr_trajectory_chart([]))

    def test_chart_returns_none_for_single_point(self):
        """Need at least 2 points for a line chart."""
        from src.analysis.chart_generator import generate_dhcr_trajectory_chart
        single = [{"as_of_date": "2026-05-21", "frontier_kv_kb_per_token": 14,
                   "frontier_hbm_gb_per_accelerator": 576,
                   "implied_context_capacity_tokens": 42_000_000,
                   "enterprise_ai_yoy_growth_pct": 200,
                   "nvidia_datacenter_yoy_growth_pct": 70,
                   "capacity_creation_yoy_pct": 100,
                   "direction": "strongly_bullish_hbm", "direction_score": 0.5}]
        self.assertIsNone(generate_dhcr_trajectory_chart(single))


# ============================================================
# 14. Indicator config registration
# ============================================================

class TestConfigRegistration(unittest.TestCase):

    def test_dhcr_indicator_in_config(self):
        from src.config import INDICATORS
        ids = [i.id for i in INDICATORS]
        self.assertIn("t1_decode_hbm_captive_ratio", ids)

    def test_dhcr_is_tier_1(self):
        from src.config import INDICATORS
        dhcr = next(i for i in INDICATORS if i.id == "t1_decode_hbm_captive_ratio")
        self.assertEqual(dhcr.tier, 1)

    def test_total_count_now_23(self):
        from src.config import INDICATORS
        self.assertEqual(len(INDICATORS), 23,
            "v3.4.0 adds DHCR for total of 23 indicators")

    def test_dhcr_in_evidence_store(self):
        with open(REPO_ROOT / "state" / "indicator_evidence.json") as f:
            data = json.load(f)
        ind = data.get("indicators", data)
        self.assertIn("t1_decode_hbm_captive_ratio", ind)


# ============================================================
# 15. Sonnet prompt integration
# ============================================================

class TestSonnetPromptIntegration(unittest.TestCase):

    def _empty_triage(self):
        from src.analysis.llm_summarizer import NewsTriage
        return NewsTriage(
            relevant_news=[], broker_actions=[], trendforce_commentary=[],
            indicator_relevance={},
        )

    def test_dhcr_block_in_computed_metrics_prompt(self):
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt
        snap = compute_dhcr_snapshot()
        computed = {"dhcr_snapshot": asdict(snap)}
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=computed,
        )
        self.assertIn("t1_decode_hbm_captive_ratio", prompt)
        self.assertIn("DIRECTION READ", prompt)
        self.assertIn("DeepSeek V4", prompt)

    def test_dhcr_block_explains_mental_model(self):
        """Prompt must instruct Sonnet that DHCR is a direction monitor, not fire-able."""
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt
        snap = compute_dhcr_snapshot()
        computed = {"dhcr_snapshot": asdict(snap)}
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=computed,
        )
        self.assertIn("DIRECTION MONITOR", prompt)
        self.assertIn("Not fired", prompt)


# ============================================================
# 16. Workflow file persistence
# ============================================================

class TestWorkflowPersistence(unittest.TestCase):

    def test_fast_tier_workflow_commits_dhcr_state(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "fast_tier.yml").read_text()
        self.assertIn("dhcr_state.json", workflow,
            "fast_tier.yml must commit state/dhcr_state.json")

    def test_fast_tier_workflow_commits_extraction_log(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "fast_tier.yml").read_text()
        self.assertIn("extraction_log_*.jsonl", workflow,
            "fast_tier.yml must commit state/extraction_log_*.jsonl")

    def test_deep_tier_workflow_commits_extraction_log(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "deep_tier.yml").read_text()
        self.assertIn("extraction_log_*.jsonl", workflow,
            "deep_tier.yml must commit state/extraction_log_*.jsonl since the digester writes them")


# ============================================================
# 17. End-to-end build_email_html integration
# ============================================================

class TestBuildEmailHtmlIntegration(unittest.TestCase):

    def _build_minimal_analysis(self):
        from src.analysis.llm_summarizer import (
            BriefAnalysis, IndicatorAssessment, ExecutiveSummary, CaseScenario,
        )
        dhcr_assessment = IndicatorAssessment(
            id="t1_decode_hbm_captive_ratio", tier=1,
            name="Decode HBM captive ratio",
            description="Direction monitor on demand vs capacity.",
            what_to_look_for="Composite signal.",
            current_status="Not fired",
            where_we_stand="(DHCR snippet replaces this)",
            latest_developments="See deep-dive.",
        )
        es = ExecutiveSummary(
            current_phase=4, current_phase_label="Boom",
            tier_status_summary="T1: 5/7",
            new_triggers_today=[],
            base_case=CaseScenario(probability_pct=30, peak_months_away=4,
                peak_window_start="Q3 2026", peak_window_end="Q4 2026", return_to_peak_pct=18),
            bull_case=CaseScenario(probability_pct=20, peak_months_away=12,
                peak_window_start="Q1 2027", peak_window_end="Q2 2027", return_to_peak_pct=55),
            bear_case=CaseScenario(probability_pct=25, peak_months_away=0,
                peak_window_start="now", peak_window_end="now", return_to_peak_pct=0),
            delta_from_prior_text="No prior",
            urgent_attention_needed=False, urgent_attention_text="",
        )
        return BriefAnalysis(
            executive_summary=es, big_movers=[],
            indicator_assessments=[dhcr_assessment], top_5_deep_dives=[],
            market_summary="t", key_news_summary=[], cycle_position_call="t",
        )

    def test_full_render_includes_dhcr_section4(self):
        from src.email_module.template import build_email_html
        snap = compute_dhcr_snapshot()
        html = build_email_html(
            analysis=self._build_minimal_analysis(),
            performance=[], chart_base64="",
            dhcr_snapshot=asdict(snap),
        )
        self.assertIn("Decode HBM Captive Ratio — direction monitor", html)

    def test_render_works_without_dhcr_snapshot(self):
        """Backwards compat: render must work when dhcr_snapshot is None."""
        from src.email_module.template import build_email_html
        html = build_email_html(
            analysis=self._build_minimal_analysis(),
            performance=[], chart_base64="",
            dhcr_snapshot=None,
        )
        # Section 4 DHCR card should NOT render when snapshot is None
        self.assertNotIn("Decode HBM Captive Ratio — direction monitor", html)


if __name__ == "__main__":
    unittest.main()
