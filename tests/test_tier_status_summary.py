"""
v3.39.7.4 — Tests for _compute_tier_status_summary deterministic counter.

The May 20 2026 brief surfaced a Sonnet arithmetic bug: body cards
showed 4 Tier 1 indicators with `Fired` badges (t1_demand_destruction,
t1_efficiency_shock, t1_oem_inventory, t1_price_cuts_no_demand) but the
exec_summary's tier_status_summary said `T1: 3/6 fired`. Symmetrically,
t2_hyperscaler_capex_revenue was rendered with a `Partially fired` badge
in the body but counted as fired in `T2: 6/7`. Both errors traced to
Sonnet writing the tier_status_summary string in prose from its own
internal count rather than deriving it from the structured
indicator_assessments list.

The deterministic helper recomputes the string post-parse from the
structured assessments and overrides Sonnet's version. These tests lock
in the counting convention agreed with user on 2026-05-20:
  - `Fired` counts toward tier total
  - `Partially fired` ALSO counts toward tier total (monotonic in
    "progress toward firing")
  - `(N partial)` annotation appears only when N>0
  - Denominators are fixed: T1=6, T2=7, T3=5, T4=4

To run:
    python -m pytest tests/test_tier_status_summary.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.llm_summarizer import (  # noqa: E402
    IndicatorAssessment,
    _compute_tier_status_summary,
)


def _mk(id_: str, tier: int, status: str) -> IndicatorAssessment:
    """Test-only minimal IndicatorAssessment builder."""
    return IndicatorAssessment(
        id=id_, tier=tier, name="", description="",
        what_to_look_for="", current_status=status,
        where_we_stand="", latest_developments="",
        relevant_links=[], change_vs_prior=None,
    )


def test_all_not_fired():
    assessments = [_mk(f"t{t}_x{i}", t, "Not fired") for t in (1, 2, 3, 4) for i in range(2)]
    assert _compute_tier_status_summary(assessments) == (
        "T1: 0/6 fired | T2: 0/7 fired | T3: 0/5 fired | T4: 0/4 fired"
    )


def test_may20_actual_pattern_t1_4fired_t2_5fired_plus_1partial():
    """The exact May 20 2026 16:00 UTC brief pattern that triggered this fix."""
    assessments = [
        # T1: 4 Fired (the four upgraded today), 2 Not fired
        _mk("t1_demand_destruction", 1, "Fired"),
        _mk("t1_efficiency_shock", 1, "Fired"),
        _mk("t1_oem_inventory", 1, "Fired"),
        _mk("t1_price_cuts_no_demand", 1, "Fired"),
        _mk("t1_spot_decline", 1, "Not fired"),
        _mk("t1_lead_times", 1, "Not fired"),
        # T2: 5 Fired (capex, ltas, insider, pe, spot_contract), 1 Partial (hyperscaler_capex), 1 Not fired (eps_revisions)
        _mk("t2_capex_parabolic", 2, "Fired"),
        _mk("t2_ltas_signed", 2, "Fired"),
        _mk("t2_insider_sales", 2, "Fired"),
        _mk("t2_pe_compressed", 2, "Fired"),
        _mk("t2_spot_contract_spread", 2, "Fired"),
        _mk("t2_hyperscaler_capex_revenue", 2, "Partially fired"),
        _mk("t2_eps_revisions", 2, "Not fired"),
        # T3, T4: all Not fired
        *[_mk(f"t3_x{i}", 3, "Not fired") for i in range(5)],
        *[_mk(f"t4_x{i}", 4, "Not fired") for i in range(4)],
    ]
    # Per convention: T1 should be 4 (not the 3 Sonnet wrote), T2 should be 6
    # WITH (1 partial) annotation.
    assert _compute_tier_status_summary(assessments) == (
        "T1: 4/6 fired | T2: 6/7 fired (1 partial) | T3: 0/5 fired | T4: 0/4 fired"
    )


def test_partially_fired_counts_toward_total():
    """Partially fired SHOULD count in the tier total. Locked convention."""
    assessments = [_mk("t1_a", 1, "Partially fired")]
    out = _compute_tier_status_summary(assessments)
    assert "T1: 1/6 fired" in out
    assert "(1 partial)" in out


def test_partial_annotation_only_when_nonzero():
    """No `(0 partial)` clutter on tiers with no partials."""
    assessments = [_mk("t1_a", 1, "Fired"), _mk("t2_a", 2, "Fired")]
    out = _compute_tier_status_summary(assessments)
    assert "partial" not in out  # neither tier has partials → no annotation anywhere


def test_status_strings_case_insensitive():
    """Sonnet sometimes returns 'fired' lowercase or 'FIRED' uppercase. Both count."""
    assessments = [
        _mk("t1_a", 1, "fired"),
        _mk("t1_b", 1, "FIRED"),
        _mk("t1_c", 1, "Fired"),
    ]
    out = _compute_tier_status_summary(assessments)
    assert "T1: 3/6 fired" in out


def test_partially_fired_variant_spellings():
    """'Partially fired' should match common case variants the LLM might emit."""
    assessments = [
        _mk("t2_a", 2, "Partially fired"),
        _mk("t2_b", 2, "partially fired"),
        _mk("t2_c", 2, "Partially Fired"),
    ]
    out = _compute_tier_status_summary(assessments)
    assert "T2: 3/7 fired (3 partial)" in out


def test_indicators_with_missing_tier_field_use_id_fallback():
    """When tier is missing/0, the helper recovers it from the id prefix."""
    # tier=0 simulates Sonnet truncating mid-indicator and dropping the field
    a = IndicatorAssessment(
        id="t3_relative_strength", tier=0,
        name="", description="", what_to_look_for="",
        current_status="Fired", where_we_stand="",
        latest_developments="", relevant_links=[], change_vs_prior=None,
    )
    out = _compute_tier_status_summary([a])
    assert "T3: 1/5 fired" in out


def test_empty_assessments_list_renders_zeros():
    """Edge case: Sonnet completely failed. Render zero counts, don't crash."""
    out = _compute_tier_status_summary([])
    assert out == "T1: 0/6 fired | T2: 0/7 fired | T3: 0/5 fired | T4: 0/4 fired"


def test_invalid_tier_skipped():
    """Tier 5+ or tier 0 with non-tN id should be silently skipped."""
    assessments = [
        _mk("t1_a", 1, "Fired"),
        _mk("orphan_x", 0, "Fired"),  # no tN_ prefix → skipped
        _mk("t5_unknown", 5, "Fired"),  # tier 5 → skipped
    ]
    out = _compute_tier_status_summary(assessments)
    assert "T1: 1/6 fired" in out
    assert "T2: 0/7 fired" in out  # nothing leaks into T2


def test_all_tiers_fired_to_max():
    """Saturation test: every indicator fired."""
    assessments = (
        [_mk(f"t1_x{i}", 1, "Fired") for i in range(6)]
        + [_mk(f"t2_x{i}", 2, "Fired") for i in range(7)]
        + [_mk(f"t3_x{i}", 3, "Fired") for i in range(5)]
        + [_mk(f"t4_x{i}", 4, "Fired") for i in range(4)]
    )
    out = _compute_tier_status_summary(assessments)
    assert out == "T1: 6/6 fired | T2: 7/7 fired | T3: 5/5 fired | T4: 4/4 fired"
