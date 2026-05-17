"""
v3.39.2 — Tests for _safe_indicator_assessment and _tier_from_id helpers.

These cover the seven scenarios validated during v3.39.1 development plus
two new ones for the tier-from-id inference added in v3.39.2.

The defensive parser exists because the May 17 20:30 fast-tier run
crashed when Sonnet truncated mid-indicator and json_repair recovered
a partial dict missing the `latest_developments` field. The prior code
unpacked the dict directly into IndicatorAssessment(**a), which raised
TypeError on missing required positional args. This test suite locks in
that the defensive helper now handles those cases without exception.

To run:
    python -m tests.test_safe_indicator_assessment
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow this file to live in tests/ and still import src/
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.llm_summarizer import (  # noqa: E402
    IndicatorAssessment,
    _safe_indicator_assessment,
    _tier_from_id,
)


# ============================================================
# Test cases
# ============================================================

CASES = [
    # ----- _tier_from_id ----- #
    {
        "name": "tier_from_id_t1_prefix",
        "fn": _tier_from_id,
        "input": "t1_spot_decline",
        "expect_eq": 1,
    },
    {
        "name": "tier_from_id_t2_prefix",
        "fn": _tier_from_id,
        "input": "t2_capex_parabolic",
        "expect_eq": 2,
    },
    {
        "name": "tier_from_id_t4_prefix",
        "fn": _tier_from_id,
        "input": "t4_layoffs",
        "expect_eq": 4,
    },
    {
        "name": "tier_from_id_invalid_returns_0",
        "fn": _tier_from_id,
        "input": "nonsense",
        "expect_eq": 0,
    },
    {
        "name": "tier_from_id_empty_returns_0",
        "fn": _tier_from_id,
        "input": "",
        "expect_eq": 0,
    },
    {
        "name": "tier_from_id_non_string_returns_0",
        "fn": _tier_from_id,
        "input": None,
        "expect_eq": 0,
    },
    {
        "name": "tier_from_id_out_of_range_returns_0",
        "fn": _tier_from_id,
        "input": "t9_unknown",  # out of 1-4 range
        "expect_eq": 0,
    },

    # ----- _safe_indicator_assessment: the canonical happy case ----- #
    {
        "name": "complete_dict",
        "input": {
            "id": "t1_spot_decline",
            "tier": 1,
            "name": "DDR5 spot first MoM decline",
            "description": "Spot price MoM",
            "what_to_look_for": "first decline",
            "current_status": "Fired",
            "where_we_stand": "Already fired in April",
            "latest_developments": "DDR4 -5% per TrendForce",
            "relevant_links": [{"title": "TF", "url": "https://trendforce.com/x"}],
            "change_vs_prior": None,
        },
        "expect_attrs": {
            "id": "t1_spot_decline",
            "tier": 1,
            "current_status": "Fired",
        },
    },

    # ----- The exact May 17 20:30 crash scenario: truncated mid-indicator ----- #
    {
        "name": "truncated_missing_latest_developments",
        "input": {
            "id": "t3_supplier_inventory_rebuild",
            "tier": 3,
            "name": "Supplier inventory rebuilds 2-3 → 4-5 weeks",
            "description": "Inventory at suppliers",
            "what_to_look_for": "rebuild from 2-3 to 4-5 weeks",
            "current_status": "Not fired",
            "where_we_stand": "Still 2-3 weeks at SK Hynix",
            # latest_developments missing — Sonnet was mid-write
            "relevant_links": [],
        },
        "expect_attrs": {
            "id": "t3_supplier_inventory_rebuild",
            "tier": 3,
            "latest_developments": "",  # safe default, not a crash
        },
    },

    # ----- v3.39.2: tier missing but recoverable from id prefix ----- #
    {
        "name": "tier_missing_recovered_from_id",
        "input": {
            "id": "t2_eps_revisions",
            # tier field missing entirely
            "name": "Forward EPS revisions",
            "current_status": "Not fired",
        },
        "expect_attrs": {
            "id": "t2_eps_revisions",
            "tier": 2,  # recovered via _tier_from_id, not defaulted to 0
        },
    },

    # ----- v3.39.2: tier non-numeric AND id non-prefixed → falls to 0 ----- #
    {
        "name": "tier_garbage_no_id_recovery",
        "input": {
            "id": "weird_no_prefix",
            "tier": "high",  # json_repair garbage
            "name": "Some indicator",
        },
        "expect_attrs": {
            "id": "weird_no_prefix",
            "tier": 0,  # nothing to recover from
        },
    },

    # ----- Non-numeric tier with id recovery available ----- #
    {
        "name": "tier_non_numeric_id_recovers",
        "input": {
            "id": "t4_layoffs",
            "tier": "four",  # json_repair garbage
            "name": "Layoffs",
        },
        "expect_attrs": {
            "id": "t4_layoffs",
            "tier": 4,  # recovered from id prefix
        },
    },

    # ----- relevant_links wrong type ----- #
    {
        "name": "relevant_links_wrong_type",
        "input": {
            "id": "t1_lead_times",
            "tier": 1,
            "name": "Lead times",
            "relevant_links": {"title": "x", "url": "y"},  # dict instead of list
        },
        "expect_attrs": {
            "id": "t1_lead_times",
            "tier": 1,
            "relevant_links": [],  # defensively defaulted to []
        },
    },

    # ----- Completely empty dict (still gets defaults, doesn't crash) ----- #
    {
        "name": "empty_dict",
        "input": {},
        "expect_attrs": {
            "id": "unknown",
            "tier": 0,
            "current_status": "Unknown (Sonnet output truncated)",
        },
    },

    # ----- change_vs_prior with real value ----- #
    {
        "name": "change_vs_prior_string",
        "input": {
            "id": "t2_pe_compressed",
            "tier": 2,
            "change_vs_prior": "Compressed from 6.1x to 5.5x this week",
        },
        "expect_attrs": {
            "id": "t2_pe_compressed",
            "change_vs_prior": "Compressed from 6.1x to 5.5x this week",
        },
    },

    # ----- change_vs_prior as empty string — coerced to None ----- #
    {
        "name": "change_vs_prior_empty_string",
        "input": {
            "id": "t1_spot_decline",
            "tier": 1,
            "change_vs_prior": "",
        },
        "expect_attrs": {
            "id": "t1_spot_decline",
            "change_vs_prior": None,  # falsy strings coerced to None
        },
    },
]


# ============================================================
# Runner
# ============================================================

def run_case(case: dict) -> tuple[bool, str]:
    """Execute one case. Returns (passed, reason)."""
    name = case["name"]
    try:
        if "fn" in case:
            # Direct function-under-test (used for _tier_from_id cases)
            result = case["fn"](case["input"])
            if result != case["expect_eq"]:
                return False, f"got {result!r}, expected {case['expect_eq']!r}"
            return True, "OK"
        # Default: _safe_indicator_assessment
        result = _safe_indicator_assessment(case["input"])
        if not isinstance(result, IndicatorAssessment):
            return False, f"got {type(result).__name__}, expected IndicatorAssessment"
        for attr, expected in case["expect_attrs"].items():
            actual = getattr(result, attr)
            if actual != expected:
                return False, f"{attr}: got {actual!r}, expected {expected!r}"
        return True, "OK"
    except Exception as e:
        return False, f"raised {type(e).__name__}: {e}"


def main() -> int:
    passed = 0
    failed = 0
    print(f"Running {len(CASES)} cases for _safe_indicator_assessment / _tier_from_id\n")
    for case in CASES:
        ok, reason = run_case(case)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {case['name']}: {reason}")
        if ok:
            passed += 1
        else:
            failed += 1
    print(f"\n{passed} passed, {failed} failed of {len(CASES)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
