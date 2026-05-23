"""v3.4.6 — Tests for frontier-model source ingestion:
- arxiv frontier-paper pre-filter (title-keyword filtering before relevance scorer)
- Lab blog + arxiv + HF Papers source-tier classification (digester._classify_source_for_tier)
- RSS_FEEDS additions are present in config
- DHCR arch aggregator picks up Tier-A sources from new feeds
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


class TestArxivPrefilter(unittest.TestCase):

    def test_lab_name_in_title_kept(self):
        from src.data.news import arxiv_frontier_paper_filter
        items = [
            SimpleNamespace(title="Llama 4 Maverick: A Mixture-of-Experts Frontier Model"),
            SimpleNamespace(title="DeepSeek V5 Technical Report: MLA at Scale"),
            SimpleNamespace(title="Qwen 4 235B: Open-Weights Reasoning"),
        ]
        kept = arxiv_frontier_paper_filter(items)
        self.assertEqual(len(kept), 3)

    def test_arch_keyword_plus_params_kept(self):
        from src.data.news import arxiv_frontier_paper_filter
        items = [
            SimpleNamespace(title="A 70B model with FP8 quantization for inference"),
            SimpleNamespace(title="Scaling MoE to 405B parameters"),
        ]
        kept = arxiv_frontier_paper_filter(items)
        self.assertEqual(len(kept), 2)

    def test_release_keyword_kept(self):
        from src.data.news import arxiv_frontier_paper_filter
        items = [
            SimpleNamespace(title="Phi-5 Technical Report"),
            SimpleNamespace(title="Open-weights state-of-the-art reasoning model"),
        ]
        kept = arxiv_frontier_paper_filter(items)
        self.assertEqual(len(kept), 2)

    def test_routine_papers_dropped(self):
        from src.data.news import arxiv_frontier_paper_filter
        items = [
            SimpleNamespace(title="A study of gradient methods on convex objectives"),
            SimpleNamespace(title="Improving generalization bounds via PAC-Bayes"),
            SimpleNamespace(title="Robustness of vision transformers to adversarial perturbations"),
            SimpleNamespace(title="Diffusion models for protein structure prediction"),
        ]
        kept = arxiv_frontier_paper_filter(items)
        self.assertEqual(len(kept), 0)

    def test_empty_title_dropped(self):
        from src.data.news import arxiv_frontier_paper_filter
        items = [SimpleNamespace(title=""), SimpleNamespace(title=None)]
        kept = arxiv_frontier_paper_filter(items)
        self.assertEqual(len(kept), 0)


class TestSourceClassification(unittest.TestCase):

    def test_arxiv_routes_to_arxiv(self):
        from src.analysis.digester import _classify_source_for_tier
        self.assertEqual(_classify_source_for_tier("research_paper", "arxiv_2025.12345"), "arxiv")

    def test_huggingface_papers_distinct_from_blog(self):
        from src.analysis.digester import _classify_source_for_tier
        self.assertEqual(_classify_source_for_tier("rss", "huggingface_papers_daily"), "huggingface_papers")
        self.assertEqual(_classify_source_for_tier("rss", "huggingface_blog"), "huggingface")

    def test_meta_ai_blog(self):
        from src.analysis.digester import _classify_source_for_tier
        self.assertEqual(_classify_source_for_tier("rss", "ai.meta.com_blog"), "ai.meta.com")

    def test_deepseek_mistral_qwen(self):
        from src.analysis.digester import _classify_source_for_tier
        self.assertEqual(_classify_source_for_tier("rss", "deepseek_news"), "deepseek.com")
        self.assertEqual(_classify_source_for_tier("rss", "mistral_news"), "mistral.ai")
        self.assertEqual(_classify_source_for_tier("rss", "qwenlm_blog"), "qwenlm.github.io")


class TestRSSFeedsConfig(unittest.TestCase):
    """Verify the new feed entries exist in RSS_FEEDS.

    v3.4.6.3 reconciliation: 6 of the 9 v3.4.6-added feeds were dropped
    after production confirmed they don't expose native RSS or require
    auth (Meta AI Blog, DeepMind Blog, DeepSeek API News, Anthropic News,
    Qwen Blog, HuggingFace Papers). 3 surviving feeds tested below.
    Coverage gap compensated via WATCH_GOOGLE_QUERIES entries that map
    onto the FRONTIER_LAB_NAMES set in news.py's arxiv pre-filter.
    """

    def test_surviving_frontier_feeds_present(self):
        from src.config import RSS_FEEDS
        # v3.4.10: "Mistral News" removed from the surviving list — see
        # test_dropped_feeds_absent below for the assertion.
        for name in (
            "arxiv cs.LG (filtered)",
            "arxiv cs.CL (filtered)",
        ):
            self.assertIn(name, RSS_FEEDS, f"{name} missing from RSS_FEEDS")
            self.assertTrue(RSS_FEEDS[name].startswith("http"), f"{name} URL malformed")

    def test_dropped_feeds_absent(self):
        """v3.4.6.3 + v3.4.10 — these were in v3.4.6 / v3.4.8 / v3.4.9 but
        production confirmed they don't work as RSS endpoints. Asserting
        absence so the config doesn't quietly regrow them in a future merge."""
        from src.config import RSS_FEEDS
        for name in (
            "Meta AI Blog",
            "DeepSeek API News",
            "Qwen Blog",
            "Anthropic News",
            "Google DeepMind Blog",
            "HuggingFace Papers",
            # v3.4.10: persistent 404 on /news/rss/ after v3.4.6.3 retry
            "Mistral News",
            # v3.4.10: persistent 403 (Cloudflare beyond JA3) — same class
            # as the 5 v3.4.8 drops
            "SemiAnalysis Free",
        ):
            self.assertNotIn(name, RSS_FEEDS,
                f"{name} was dropped (no working RSS available) — "
                f"don't re-add without confirming a working URL")

    def test_compensating_google_news_queries_present(self):
        """When the lab blogs were dropped, frontier-model release Google
        News queries were added so tech-press coverage of the same
        releases still reaches the digester."""
        from src.config import WATCH_GOOGLE_QUERIES
        # Sample of names that should be covered via Google News queries
        lab_keywords_to_cover = ["llama", "claude", "deepseek", "qwen", "mistral", "gemini", "gemma"]
        for kw in lab_keywords_to_cover:
            matching = [q for q in WATCH_GOOGLE_QUERIES if kw in q.lower()]
            self.assertTrue(matching,
                f"No WATCH_GOOGLE_QUERIES entry covers '{kw}' — the lab blog "
                f"was dropped in v3.4.6.3 and no Google News compensation added")


class TestArchAggregatorTierAFromLabBlog(unittest.TestCase):
    """End-to-end: a lab-blog extraction reaches the arch aggregator with Tier A."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="v346_arch_"))
        (self.tmpdir / "data").mkdir()
        (self.tmpdir / "state").mkdir()
        self.original_cwd = Path.cwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.tmpdir)

    def test_meta_ai_blog_extraction_added_to_open_models(self):
        from src.analysis import dhcr_arch_aggregator
        (self.tmpdir / "data" / "inference_arch_reference.yaml").write_text(
            "schema_version: '2.0'\n"
            "aggregator_config:\n"
            "  extraction_log_lookback_days: 180\n"
            "  state_file_path: state/inference_arch_state.json\n"
        )
        (self.tmpdir / "state" / "inference_arch_state.json").write_text(json.dumps({
            "_metadata": {"schema_version": "2.0"},
            "open_models": [],
            "closed_models": [],
        }))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_path = self.tmpdir / "state" / f"extraction_log_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        with open(log_path, "w") as f:
            f.write(json.dumps({
                "source_id": "ai_meta_com_llama5_post",
                "source": "ai.meta.com",  # Tier A per arch aggregator
                "source_type": "blog_post",
                "date": now, "extracted_at": now,
                "architecture_signals": {
                    "new_frontier_model_release": {
                        "name": "Llama 5 405B",
                        "release_date": "2026-07-15",
                        "architecture_family": "GQA-MoE",
                        "num_layers": 96,
                        "num_kv_heads": 8,
                        "head_dim": 128,
                        "bytes_per_element": 1,
                        "kv_kb_per_token": 192,
                        "is_open": True,
                    }
                }
            }) + "\n")
        state = dhcr_arch_aggregator.aggregate_arch_extractions()
        names = [m["name"] for m in state["open_models"]]
        self.assertIn("Llama 5 405B", names)


if __name__ == "__main__":
    unittest.main()
