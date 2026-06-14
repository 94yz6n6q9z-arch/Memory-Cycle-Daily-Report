"""v3.41.2 — tests for the LIVE prose-cache path (scope-reduction + merge).

Covers the new, testable units added when the prose cache went from SHADOW
(log-only) to LIVE (Sonnet writes only changed cards; dormant cards reused):

  - word-cap truncation backstop (_truncate_words / _truncate_assessment_prose)
  - cache → IndicatorAssessment reconstruction (_assessment_from_cache)
  - the run-scope prompt instruction (_build_scope_instruction)
  - the merge of fresh + cached cards (_merge_prose_cards)
  - the fingerprint-algorithm version stamp + grandfathering in prose_cache

The full sonnet_analyze integration (which calls the Anthropic API) is exercised
in production via the live `[prose-cache] N reuse / M regen` + `merged N cards`
log lines; these tests pin the pure logic those depend on.
"""
from types import SimpleNamespace as NS

import pytest

from src.analysis import prose_cache as pc
from src.analysis.llm_summarizer import (
    _truncate_words,
    _truncate_assessment_prose,
    _assessment_from_cache,
    _build_scope_instruction,
    _merge_prose_cards,
    IndicatorAssessment,
)
from src.config import TOP_5_INDICATORS, INDICATORS


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _reasoning(sid, fired="Fired", direction="bearish", convergence="high"):
    return NS(
        signal_id=sid,
        fired_status=fired,
        consensus_direction=direction,
        convergence=convergence,
        confidence="high",
        evidence_count=3,
        evidence_sources=["src"],
    )


def _fresh(ind_id, where="w", latest="l", change=None):
    return IndicatorAssessment(
        id=ind_id, tier=1, name="n", description="d", what_to_look_for="x",
        current_status="Fired", where_we_stand=where, latest_developments=latest,
        relevant_links=[], change_vs_prior=change,
    )


def _cache_entry(where="cached prose", status="Fired"):
    return {
        "current_status": status,
        "where_we_stand": where,
        "latest_developments": "cached latest",
        "relevant_links": [],
        "change_vs_prior": None,
    }


# ----------------------------------------------------------------------------
# word-cap truncation
# ----------------------------------------------------------------------------
class TestTruncateWords:
    def test_under_cap_passthrough(self):
        assert _truncate_words("one two three", 5) == "one two three"

    def test_at_cap_passthrough(self):
        assert _truncate_words("a b c", 3) == "a b c"

    def test_over_cap_cuts_with_ellipsis(self):
        out = _truncate_words("a b c d e f", 3)
        assert out == "a b c…"
        assert len(out.replace("…", "").split()) == 3

    def test_empty_and_none_passthrough(self):
        assert _truncate_words("", 5) == ""
        assert _truncate_words(None, 5) is None

    def test_trailing_punct_stripped_before_ellipsis(self):
        assert _truncate_words("alpha beta, gamma delta", 2) == "alpha beta…"


class TestTruncateAssessmentProse:
    def test_default_cap_50_words(self):
        long_where = " ".join(["w"] * 80)
        a = _truncate_assessment_prose(_fresh("t1_oem_inventory", where=long_where))
        assert a.where_we_stand.endswith("…")
        assert len(a.where_we_stand.replace("…", "").split()) == 50

    def test_top5_gets_90_word_cap(self):
        top5_id = next(iter(TOP_5_INDICATORS))
        long_where = " ".join(["w"] * 120)
        a = _truncate_assessment_prose(_fresh(top5_id, where=long_where))
        assert len(a.where_we_stand.replace("…", "").split()) == 90

    def test_latest_capped_at_35(self):
        a = _truncate_assessment_prose(_fresh("t1_oem_inventory", latest=" ".join(["x"] * 60)))
        assert len(a.latest_developments.replace("…", "").split()) == 35

    def test_short_prose_untouched(self):
        a = _truncate_assessment_prose(_fresh("t1_oem_inventory", where="short", latest="also short"))
        assert a.where_we_stand == "short"
        assert a.latest_developments == "also short"


# ----------------------------------------------------------------------------
# cache -> assessment reconstruction
# ----------------------------------------------------------------------------
class TestAssessmentFromCache:
    def test_static_fields_sourced_from_config(self):
        # pick a real configured indicator id
        ind = INDICATORS[0]
        a = _assessment_from_cache(ind.id, _cache_entry(where="cached"))
        assert a.id == ind.id
        assert a.tier == ind.tier
        assert a.name == ind.name
        assert a.description == ind.description
        assert a.what_to_look_for == ind.what_to_look_for

    def test_prose_from_cache(self):
        ind = INDICATORS[0]
        a = _assessment_from_cache(ind.id, _cache_entry(where="my cached prose", status="Partially fired"))
        assert a.where_we_stand == "my cached prose"
        assert a.current_status == "Partially fired"

    def test_links_capped_at_three(self):
        ind = INDICATORS[0]
        entry = _cache_entry()
        entry["relevant_links"] = [{"title": str(i), "url": "u"} for i in range(6)]
        a = _assessment_from_cache(ind.id, entry)
        assert len(a.relevant_links) == 3

    def test_unknown_id_does_not_crash(self):
        a = _assessment_from_cache("totally_unknown_id", _cache_entry())
        assert a.id == "totally_unknown_id"
        assert isinstance(a.tier, int)


# ----------------------------------------------------------------------------
# scope instruction
# ----------------------------------------------------------------------------
class TestScopeInstruction:
    def test_empty_means_no_cards(self):
        out = _build_scope_instruction(set())
        assert "EMPTY indicator_assessments" in out
        assert "executive_brief" in out

    def test_subset_lists_ids_and_says_only(self):
        out = _build_scope_instruction({"t1_a", "t2_b"})
        assert "t1_a" in out and "t2_b" in out
        assert "ONLY" in out
        assert "OMIT" in out

    def test_subset_ids_sorted(self):
        out = _build_scope_instruction({"t2_z", "t1_a"})
        assert out.index("t1_a") < out.index("t2_z")


# ----------------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------------
class TestMergeProseCards:
    def test_reuse_served_from_cache_regen_from_fresh(self):
        reasonings = [_reasoning("t1_a"), _reasoning("t1_b")]
        fresh = [_fresh("t1_b", where="FRESH B")]  # only b regenerated
        cache = {"t1_a": _cache_entry(where="CACHED A")}
        reuse_ids = {"t1_a"}
        merged, n_stopgap = _merge_prose_cards(reasonings, fresh, reuse_ids, cache)
        by_id = {a.id: a for a in merged}
        assert by_id["t1_a"].where_we_stand == "CACHED A"
        assert by_id["t1_b"].where_we_stand == "FRESH B"
        assert n_stopgap == 0

    def test_all_dormant_yields_all_from_cache(self):
        reasonings = [_reasoning("t1_a"), _reasoning("t1_b")]
        cache = {"t1_a": _cache_entry(where="A"), "t1_b": _cache_entry(where="B")}
        merged, n_stopgap = _merge_prose_cards(reasonings, [], {"t1_a", "t1_b"}, cache)
        assert {a.id for a in merged} == {"t1_a", "t1_b"}
        assert n_stopgap == 0

    def test_dropped_regen_card_falls_back_to_cache_stopgap(self):
        # t1_b is regen (not in reuse) but Sonnet didn't return it; cache has it
        reasonings = [_reasoning("t1_a"), _reasoning("t1_b")]
        fresh = [_fresh("t1_a", where="FRESH A")]
        cache = {"t1_b": _cache_entry(where="STALE B")}
        merged, n_stopgap = _merge_prose_cards(reasonings, fresh, set(), cache)
        by_id = {a.id: a for a in merged}
        assert by_id["t1_b"].where_we_stand == "STALE B"
        assert n_stopgap == 1

    def test_dropped_regen_card_with_no_cache_is_omitted(self):
        reasonings = [_reasoning("t1_a"), _reasoning("t1_b")]
        fresh = [_fresh("t1_a", where="FRESH A")]
        merged, n_stopgap = _merge_prose_cards(reasonings, fresh, set(), cache={})
        assert {a.id for a in merged} == {"t1_a"}  # b vanishes (legacy behavior)
        assert n_stopgap == 0

    def test_unexpected_fresh_card_preserved(self):
        # Sonnet emits a card with an id not in reasonings → keep it
        reasonings = [_reasoning("t1_a")]
        fresh = [_fresh("t1_a", where="A"), _fresh("t1_surprise", where="S")]
        merged, _ = _merge_prose_cards(reasonings, fresh, set(), cache={})
        assert "t1_surprise" in {a.id for a in merged}

    def test_fresh_cards_are_word_capped_in_merge(self):
        reasonings = [_reasoning("t1_oem_inventory")]
        fresh = [_fresh("t1_oem_inventory", where=" ".join(["w"] * 80))]
        merged, _ = _merge_prose_cards(reasonings, fresh, set(), cache={})
        assert merged[0].where_we_stand.endswith("…")


# ----------------------------------------------------------------------------
# fingerprint-algorithm version stamp + grandfathering
# ----------------------------------------------------------------------------
class TestFingerprintAlgoStamp:
    def test_missing_stamp_is_grandfathered(self):
        # entry written by v3.41.1 (Option-A fingerprint, NO algo stamp) with an
        # unchanged verdict must REUSE, not re-churn on the first v3.41.2 run.
        r = _reasoning("t1_a")
        fp = pc.compute_fingerprint(r)
        cache = {"t1_a": {"where_we_stand": "p", "fingerprint": fp, "status": "Fired",
                          "last_full_regen_date": pc._today().isoformat()}}
        d = pc.evaluate_cache(cache, [r])
        assert d["t1_a"]["action"] == "reuse"

    def test_different_stamp_forces_clean_rebuild(self):
        r = _reasoning("t1_a")
        fp = pc.compute_fingerprint(r)
        cache = {"t1_a": {"where_we_stand": "p", "fingerprint": fp, "status": "Fired",
                          "fingerprint_algo": "v1-old",
                          "last_full_regen_date": pc._today().isoformat()}}
        d = pc.evaluate_cache(cache, [r])
        assert d["t1_a"]["action"] == "regen"
        assert d["t1_a"]["reason"] == "fingerprint_algo_changed"

    def test_matching_stamp_reuses(self):
        r = _reasoning("t1_a")
        fp = pc.compute_fingerprint(r)
        cache = {"t1_a": {"where_we_stand": "p", "fingerprint": fp, "status": "Fired",
                          "fingerprint_algo": pc.FINGERPRINT_ALGO,
                          "last_full_regen_date": pc._today().isoformat()}}
        d = pc.evaluate_cache(cache, [r])
        assert d["t1_a"]["action"] == "reuse"

    def test_write_stamps_current_algo(self):
        r = _reasoning("t1_a")
        cache = {}
        a = _fresh("t1_a", where="new")
        pc.update_cache_after_synthesis(cache, [a], [r], regenerated_ids={"t1_a"})
        assert cache["t1_a"]["fingerprint_algo"] == pc.FINGERPRINT_ALGO
