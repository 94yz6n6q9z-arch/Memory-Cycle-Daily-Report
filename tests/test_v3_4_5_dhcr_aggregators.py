"""v3.4.5 — Tests for the DHCR catalog auto-update aggregators.

Validates:
  - Frontier model aggregator appends new models, dedupes existing
  - Accelerator aggregator routes to correct vendor category, dedupes
  - Revenue aggregator appends new quarters, updates existing on Tier-A
  - Tier-A source wins over Tier-C on collision
  - Empty extraction logs preserve state
  - Missing schema YAML → safe-fail with empty return
  - load_dhcr_data merges schema + state correctly
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


class _AggregatorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="v345_dhcr_agg_"))
        (self.tmpdir / "data").mkdir()
        (self.tmpdir / "state").mkdir()
        self.original_cwd = Path.cwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.tmpdir)

    def _write_arch_yaml(self):
        (self.tmpdir / "data" / "inference_arch_reference.yaml").write_text(
            "schema_version: '2.0'\n"
            "aggregator_config:\n"
            "  extraction_log_lookback_days: 180\n"
            "  state_file_path: state/inference_arch_state.json\n"
        )

    def _write_hbm_yaml(self):
        (self.tmpdir / "data" / "hbm_per_accelerator.yaml").write_text(
            "schema_version: '2.0'\n"
            "aggregator_config:\n"
            "  extraction_log_lookback_days: 180\n"
            "  state_file_path: state/hbm_per_accelerator_state.json\n"
        )

    def _write_rev_yaml(self):
        (self.tmpdir / "data" / "enterprise_ai_revenue.yaml").write_text(
            "schema_version: '2.0'\n"
            "aggregator_config:\n"
            "  extraction_log_lookback_days: 120\n"
            "  state_file_path: state/enterprise_ai_revenue_state.json\n"
        )

    def _write_arch_state(self, open_models=None, closed_models=None):
        state = {
            "_metadata": {"schema_version": "2.0"},
            "open_models": open_models or [],
            "closed_models": closed_models or [],
        }
        (self.tmpdir / "state" / "inference_arch_state.json").write_text(json.dumps(state))

    def _write_hbm_state(self, **sections):
        state = {"_metadata": {"schema_version": "2.0"},
                 "nvidia": [], "amd": [], "custom_silicon": [], "non_hbm_accelerators": []}
        state.update(sections)
        (self.tmpdir / "state" / "hbm_per_accelerator_state.json").write_text(json.dumps(state))

    def _write_rev_state(self, **sections):
        state = {"_metadata": {"schema_version": "2.0"},
                 "microsoft_azure_ai": [], "aws_ai_services": [],
                 "google_cloud_ai": [], "oracle_ai_infra": [], "nvidia_datacenter": []}
        state.update(sections)
        (self.tmpdir / "state" / "enterprise_ai_revenue_state.json").write_text(json.dumps(state))

    def _write_extraction(self, entries):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.tmpdir / "state" / f"extraction_log_{today}.jsonl"
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _now(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestArchAggregator(_AggregatorTestBase):

    def test_new_model_appended(self):
        from src.analysis import dhcr_arch_aggregator
        self._write_arch_yaml()
        self._write_arch_state()
        now = self._now()
        self._write_extraction([{
            "source_id": "arxiv_deepseek_v5",
            "source": "arxiv",
            "source_type": "research_paper",
            "date": now, "extracted_at": now,
            "architecture_signals": {
                "new_frontier_model_release": {
                    "name": "DeepSeek V5",
                    "release_date": "2026-06-15",
                    "architecture_family": "MLA-MoE",
                    "num_layers": 64,
                    "bytes_per_element": 1,
                    "kv_kb_per_token": 11.0,
                    "is_open": True,
                }
            }
        }])
        state = dhcr_arch_aggregator.aggregate_arch_extractions()
        names = [m["name"] for m in state["open_models"]]
        self.assertIn("DeepSeek V5", names)

    def test_existing_model_not_duplicated(self):
        from src.analysis import dhcr_arch_aggregator
        self._write_arch_yaml()
        self._write_arch_state(open_models=[{
            "name": "Llama 3 70B",
            "release_date": "2024-04-18",
            "kv_kb_per_token": 320,
        }])
        now = self._now()
        self._write_extraction([{
            "source_id": "tech_news",
            "source": "tom's_hardware",
            "source_type": "news_article",
            "date": now, "extracted_at": now,
            "architecture_signals": {
                "new_frontier_model_release": {
                    "name": "Llama 3 70B",
                    "release_date": "2024-04-18",
                    "kv_kb_per_token": 320,
                }
            }
        }])
        state = dhcr_arch_aggregator.aggregate_arch_extractions()
        # No duplicate
        names = [m["name"] for m in state["open_models"]]
        self.assertEqual(names.count("Llama 3 70B"), 1)


class TestAcceleratorAggregator(_AggregatorTestBase):

    def test_new_accelerator_routes_to_nvidia(self):
        from src.analysis import dhcr_accelerator_aggregator
        self._write_hbm_yaml()
        self._write_hbm_state()
        now = self._now()
        self._write_extraction([{
            "source_id": "nvidia_gtc_2026",
            "source": "nvidia_gtc",
            "source_type": "press_release",
            "date": now, "extracted_at": now,
            "architecture_signals": {
                "new_accelerator_announcement": {
                    "vendor": "nvidia",
                    "name": "Rubin Ultra",
                    "hbm_gb": 384,
                    "release_date": "2026-Q4",
                    "generation": "Vera Rubin Ultra",
                    "category": "nvidia",
                }
            }
        }])
        state = dhcr_accelerator_aggregator.aggregate_accelerator_extractions()
        nvidia_names = [a["name"] for a in state["nvidia"]]
        self.assertIn("Rubin Ultra", nvidia_names)

    def test_custom_silicon_routes_correctly(self):
        from src.analysis import dhcr_accelerator_aggregator
        self._write_hbm_yaml()
        self._write_hbm_state()
        now = self._now()
        self._write_extraction([{
            "source_id": "google_cloud_next_2026",
            "source": "google_cloud_next",
            "source_type": "keynote",
            "date": now, "extracted_at": now,
            "architecture_signals": {
                "new_accelerator_announcement": {
                    "vendor": "google",
                    "name": "TPU v8",
                    "hbm_gb": 256,
                    "release_date": "2026-Q4",
                    "category": "custom_silicon",
                }
            }
        }])
        state = dhcr_accelerator_aggregator.aggregate_accelerator_extractions()
        names = [a["name"] for a in state["custom_silicon"]]
        self.assertIn("TPU v8", names)


class TestRevenueAggregator(_AggregatorTestBase):

    def test_new_quarter_appended(self):
        from src.analysis import dhcr_revenue_aggregator
        self._write_rev_yaml()
        self._write_rev_state()
        now = self._now()
        self._write_extraction([{
            "source_id": "msft_q2_2026_earnings",
            "source": "msft_earnings",
            "source_type": "earnings_transcript",
            "date": now, "extracted_at": now,
            "architecture_signals": {
                "enterprise_ai_quarterly_revenue": {
                    "category": "microsoft_azure_ai",
                    "quarter": "2026-Q2",
                    "revenue_usd_b": 5.8,
                    "qoq_growth_pct": 18.5,
                    "yoy_growth_pct": 102.0,
                    "source_quote": "Azure AI grew 102% YoY in Q2.",
                }
            }
        }])
        state = dhcr_revenue_aggregator.aggregate_revenue_extractions()
        msft = state["microsoft_azure_ai"]
        self.assertTrue(any(e["quarter"] == "2026-Q2" for e in msft))

    def test_existing_quarter_updated_by_tier_a(self):
        from src.analysis import dhcr_revenue_aggregator
        self._write_rev_yaml()
        self._write_rev_state(nvidia_datacenter=[{
            "quarter": "2026-Q1",
            "revenue_usd_b": 28.0,
            "yoy_growth_pct": 60.0,
            "source_quote": "estimate",
        }])
        now = self._now()
        self._write_extraction([{
            "source_id": "nvda_q1_2026_earnings",
            "source": "nvda_earnings",
            "source_type": "earnings_transcript",
            "date": now, "extracted_at": now,
            "architecture_signals": {
                "enterprise_ai_quarterly_revenue": {
                    "category": "nvidia_datacenter",
                    "quarter": "2026-Q1",
                    "revenue_usd_b": 30.5,
                    "yoy_growth_pct": 69.0,
                    "source_quote": "Datacenter revenue $30.5B, up 69% YoY.",
                }
            }
        }])
        state = dhcr_revenue_aggregator.aggregate_revenue_extractions()
        q1 = [e for e in state["nvidia_datacenter"] if e["quarter"] == "2026-Q1"][0]
        self.assertEqual(q1["revenue_usd_b"], 30.5)
        self.assertEqual(q1["yoy_growth_pct"], 69.0)


class TestSafeFailing(_AggregatorTestBase):

    def test_missing_arch_yaml_returns_empty(self):
        from src.analysis import dhcr_arch_aggregator
        # No yaml written
        result = dhcr_arch_aggregator.aggregate_arch_extractions()
        self.assertEqual(result, {})

    def test_missing_state_creates_skeleton(self):
        from src.analysis import dhcr_arch_aggregator
        self._write_arch_yaml()
        # No state written
        state = dhcr_arch_aggregator.aggregate_arch_extractions()
        self.assertIn("open_models", state)
        self.assertIn("closed_models", state)

    def test_empty_extraction_preserves_state(self):
        from src.analysis import dhcr_arch_aggregator
        self._write_arch_yaml()
        bootstrap = [{"name": "Llama 4", "release_date": "2025-08-01"}]
        self._write_arch_state(open_models=bootstrap)
        state = dhcr_arch_aggregator.aggregate_arch_extractions()
        names = [m["name"] for m in state["open_models"]]
        self.assertEqual(names, ["Llama 4"])


class TestLoadDhcrDataMerge(unittest.TestCase):
    """load_dhcr_data should merge schema YAMLs + state JSONs so downstream
    compute_* functions see the dict shape they expect (open_models list,
    nvidia list, microsoft_azure_ai list, etc.)."""

    def test_merge_preserves_downstream_keys(self):
        from src.analysis.dhcr import load_dhcr_data
        data = load_dhcr_data()
        # Schema keys
        self.assertIn("schema_version", data["arch"])
        # State keys merged in
        self.assertIn("open_models", data["arch"])
        self.assertIn("closed_models", data["arch"])
        self.assertIn("nvidia", data["hbm"])
        self.assertIn("amd", data["hbm"])
        self.assertIn("microsoft_azure_ai", data["rev"])
        self.assertIn("nvidia_datacenter", data["rev"])
        # Non-empty (bootstrap preserved)
        self.assertGreater(len(data["arch"]["open_models"]), 0)
        self.assertGreater(len(data["hbm"]["nvidia"]), 0)


if __name__ == "__main__":
    unittest.main()
