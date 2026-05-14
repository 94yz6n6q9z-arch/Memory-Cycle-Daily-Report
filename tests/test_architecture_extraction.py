"""
v3.3 — Architecture extraction verification fixtures.

Three short synthetic earnings excerpts representing the three signal types
v3.3 is designed to catch. Each has a `expected` dict of what extraction
should produce (allowing for null/equivalent for fields not in the snippet).

To run:
    python -m tests.test_architecture_extraction

This will:
  1. Call digest_document on each fixture
  2. Check that the architecture_signals sub-object matches expectations
  3. Print per-fixture PASS/FAIL with a short reason

Do NOT run this against production state — it writes to state/extraction_log_*.jsonl
like a real run. Either run on a throwaway clone, or delete the resulting log
afterwards. The script prints the path of any logs it created at the end.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

# Allow this file to live in tests/ and still import src/
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.digester import digest_document  # noqa: E402


FIXTURES = [
    {
        "name": "memory_vendor_capex_mix",
        "source_type": "earnings_transcript",
        "title": "SK Hynix 1Q26 Earnings Call (excerpt)",
        "text": (
            "Operator: Our first question relates to capex allocation. CFO?\n"
            "CFO: For 2026, HBM-related capex is now approximately 55% of our total DRAM capex, "
            "up from roughly 40% in 2025. Conventional DRAM is approximately 35%, and LPDDR roughly 10%. "
            "We are continuing to weigh wafer allocation between HBM and conventional DRAM carefully — "
            "the recent DDR5 contract pricing strength has materially changed the per-wafer economics in some cases. "
            "We exited Q1 with supplier inventory at 2.1 weeks. ASP rose mid-60% quarter on quarter, "
            "driven primarily by conventional DRAM rather than HBM."
        ),
        "expected_hits": {
            "hbm_capex_pct": 55,
            "ddr_capex_pct": 35,
            "lpddr_capex_pct": 10,
            "wafer_allocation_language_keywords": ["hbm", "ddr"],
        },
    },
    {
        "name": "hyperscaler_custom_silicon",
        "source_type": "earnings_transcript",
        "title": "Hyperscaler 4Q25 Earnings Call (excerpt)",
        "text": (
            "Q: Can you talk about the role of custom silicon in your inference fleet?\n"
            "CFO: We continue to scale Trainium 2 deployment. Trainium 2 uses LPDDR-based memory architecture "
            "which gives us meaningful cost advantage for batch inference workloads. Memory is now approximately "
            "30% of our 2026 capex, up from roughly 20% last year. We achieved approximately 40% YoY "
            "inference cost reduction by combining quantization to INT4, speculative decoding deployed in production, "
            "and disaggregated serving where prefill and decode run on different hardware."
        ),
        "expected_hits": {
            "memory_pct_of_capex": 30,
            "custom_silicon_named_keywords": ["trainium"],
            "custom_silicon_memory_arch_keywords": ["lpddr"],
            "inference_cost_reduction_pct": 40,
            "deployed_quantization_keywords": ["int4"],
            "deployed_speculative_decoding": True,
            "deployed_disaggregated_serving": True,
        },
    },
    {
        "name": "cxl_lpddr_design_win",
        "source_type": "earnings_transcript",
        "title": "Astera Labs 1Q26 Earnings Call (excerpt)",
        "text": (
            "We are pleased to announce our first production CXL 3.0 memory pooling deployment with a "
            "Tier 1 hyperscaler customer. This is the first production CXL deployment in the industry at scale. "
            "Separately, Samsung announced a major LPDDR datacenter design win with NVIDIA for the Grace successor platform, "
            "which we expect will significantly accelerate LPDDR adoption in the datacenter."
        ),
        "expected_hits": {
            "cxl_production_deployment": True,
            "lpddr_datacenter_design_win_keywords": ["samsung"],
        },
    },
]


def _check(field_actual, field_expected) -> bool:
    """Soft match: if expected is a number, allow ±5% tolerance. If expected_keywords,
    check those substrings appear (case-insensitive). If True/False, equality."""
    if field_expected is None:
        return field_actual is None
    if isinstance(field_expected, bool):
        return field_actual is field_expected
    if isinstance(field_expected, (int, float)):
        if not isinstance(field_actual, (int, float)):
            return False
        return abs(field_actual - field_expected) / max(abs(field_expected), 1) < 0.05
    if isinstance(field_expected, list):  # keyword list
        if not isinstance(field_actual, str):
            return False
        actual_low = field_actual.lower()
        return any(k.lower() in actual_low for k in field_expected)
    return field_actual == field_expected


def run_one_fixture(fix: dict) -> tuple[bool, str]:
    digest = digest_document(
        source_id=f"FIXTURE_{fix['name']}",
        source_type=fix["source_type"],
        published_date_iso="2026-05-12",
        title=fix["title"],
        text=fix["text"],
    )
    if digest is None:
        return False, "digest returned None"

    arch = (digest.key_metrics or {}).get("architecture_signals")
    if not isinstance(arch, dict):
        return False, f"no architecture_signals dict in key_metrics (got: {type(arch).__name__})"

    failures = []
    for field, expected in fix["expected_hits"].items():
        # Strip _keywords suffix to map back to actual field name
        actual_field = field.replace("_keywords", "")
        actual = arch.get(actual_field)
        if not _check(actual, expected):
            failures.append(f"{actual_field}: expected ~{expected}, got {actual!r}")

    if failures:
        return False, "; ".join(failures)
    return True, "all expected fields matched"


def main():
    print("=" * 70)
    print("v3.3 — Architecture extraction fixture verification")
    print("=" * 70)
    results = []
    for fix in FIXTURES:
        ok, reason = run_one_fixture(fix)
        results.append((fix["name"], ok, reason))
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {fix['name']}: {reason}")

    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    print("-" * 70)
    print(f"Result: {n_pass}/{n_total} fixtures passed")

    # Identify any extraction logs created during the run
    state_dir = Path("state")
    logs = list(state_dir.glob("extraction_log_*.jsonl")) if state_dir.exists() else []
    if logs:
        print(f"Note: {len(logs)} extraction log(s) written during test: {[str(p) for p in logs]}")
        print("Delete these if you don't want them in your real state directory.")

    sys.exit(0 if n_pass == n_total else 1)


if __name__ == "__main__":
    main()
