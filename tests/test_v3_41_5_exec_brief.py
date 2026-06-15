"""v3.41.5 — exec-brief stale-carryover fix.

A: _prune_prior_state_prose strips free-text narrative from the prior_state
   dump so Sonnet stops echoing a resolved story (e.g. the weeks-old Samsung
   strike) forward, while leaving the structured fields change-tracking reads
   fully intact.
B: the EXECUTIVE BRIEF SPECIFICATION encodes the spine-first / summarize /
   macro-via-indicator / no-carryover contract.
"""
import json
from pathlib import Path

from src.analysis.llm_summarizer import _prune_prior_state_prose

_LLM_SRC = (
    Path(__file__).resolve().parent.parent
    / "src" / "analysis" / "llm_summarizer.py"
).read_text()


def _sample_prior_state():
    return {
        "saved_at_iso": "2026-06-14T13:00:00+00:00",
        "executive_summary": {
            "current_phase": 4,
            "current_phase_label": "Phase 4 — Cycle peak forming",
            "tier_status_summary": "T1: 2/6 fired | T2: 1/7 fired | T3: 0/5 | T4: 0/4",
            "new_triggers_today": [
                {"tier": 2, "indicator_id": "t2_capex_parabolic",
                 "indicator_name": "Capex parabolic", "description": "x"}
            ],
            "base_case": {"probability_pct": 55, "peak_months_away": 6,
                          "peak_window_start": "December 2026",
                          "return_to_peak_pct": 12.0},
            "bull_case": {"probability_pct": 25, "peak_months_away": 9,
                          "peak_window_start": "March 2027",
                          "return_to_peak_pct": 28.0},
            "bear_case": {"probability_pct": 20, "peak_months_away": 2,
                          "peak_window_start": "August 2026",
                          "return_to_peak_pct": -5.0},
            "urgent_attention_needed": False,
            # --- prose narrative fields (should be stripped) ---
            "delta_from_prior_text": "Samsung strike resolved; probabilities unchanged.",
            "urgent_attention_text": "Watch the Samsung labor situation closely.",
            "executive_brief": (
                "The Samsung labor story resolved: the planned strike was "
                "averted, and Samsung +4.5% reflects that relief."
            ),
        },
        "indicator_assessments": [
            {
                "id": "t1_competitor_dynamics", "tier": 1,
                "name": "Competitor dynamics", "description": "desc",
                "what_to_look_for": "look", "current_status": "Not fired",
                # --- prose narrative fields (should be stripped) ---
                "where_we_stand": "Samsung strike averted, no HBM tightening.",
                "latest_developments": "Samsung union reached a deal.",
                "change_vs_prior": "Reverted framing on Samsung.",
                "relevant_links": [{"title": "Samsung deal", "url": "http://x"}],
            },
            {
                "id": "t2_pe_compressed", "tier": 2,
                "name": "Forward P/E compressed", "description": "desc2",
                "what_to_look_for": "look2", "current_status": "Fired",
                "where_we_stand": "Forward multiple at 6x.",
                "latest_developments": "n/a",
                "change_vs_prior": "n/a",
                "relevant_links": [],
            },
        ],
    }


# ---------------------------------------------------------------- A: prune

def test_prune_drops_exec_summary_prose():
    pruned = _prune_prior_state_prose(_sample_prior_state())
    es = pruned["executive_summary"]
    for k in ("executive_brief", "delta_from_prior_text", "urgent_attention_text"):
        assert k not in es, f"{k} should be stripped"


def test_prune_keeps_exec_summary_structured():
    pruned = _prune_prior_state_prose(_sample_prior_state())
    es = pruned["executive_summary"]
    # structured fields change-tracking / peak-window / probability checks read
    assert es["current_phase"] == 4
    assert es["current_phase_label"].startswith("Phase 4")
    assert es["tier_status_summary"].startswith("T1:")
    assert es["new_triggers_today"][0]["indicator_id"] == "t2_capex_parabolic"
    assert es["base_case"]["peak_months_away"] == 6
    assert es["bull_case"]["probability_pct"] == 25
    assert es["urgent_attention_needed"] is False


def test_prune_drops_per_card_prose_keeps_status():
    pruned = _prune_prior_state_prose(_sample_prior_state())
    for c in pruned["indicator_assessments"]:
        for k in ("where_we_stand", "latest_developments",
                  "change_vs_prior", "relevant_links"):
            assert k not in c, f"{k} should be stripped from {c['id']}"
        # structured status the deterministic prior_status_by_id diff needs
        assert c["id"]
        assert c["tier"] in (1, 2)
        assert c["current_status"] in ("Fired", "Not fired")


def test_prune_removes_the_echo_fuel():
    """The Samsung narrative lived only in prose fields; after pruning it must
    be gone from the serialized prior_state Sonnet sees."""
    raw = json.dumps(_sample_prior_state())
    assert "Samsung" in raw  # precondition: the fixture carries it
    pruned = json.dumps(_prune_prior_state_prose(_sample_prior_state()))
    assert "Samsung" not in pruned


def test_prune_does_not_mutate_original():
    original = _sample_prior_state()
    _prune_prior_state_prose(original)
    assert "executive_brief" in original["executive_summary"]
    assert "where_we_stand" in original["indicator_assessments"][0]


def test_prune_handles_non_dict():
    assert _prune_prior_state_prose(None) is None
    assert _prune_prior_state_prose("nope") == "nope"


def test_prune_handles_missing_sections():
    # empty / partial prior_state must not raise
    assert _prune_prior_state_prose({}) == {}
    assert "executive_summary" not in _prune_prior_state_prose({"x": 1})


# ---------------------------------------------------------------- B: spec contract

def test_spec_has_drivers_and_attribution_rule():
    assert "DRIVERS & ATTRIBUTION" in _LLM_SRC
    assert "do not fabricate a driver" in _LLM_SRC


def test_spec_has_no_stale_carryover_rule():
    assert "NO STALE CARRYOVER" in _LLM_SRC
    assert "covers TODAY" in _LLM_SRC


def test_spec_is_spine_first():
    assert "SPINE is developments along the signals" in _LLM_SRC


def test_spec_macro_routed_through_indicators():
    # macro mention gated on a transmission channel into an indicator
    assert "transmission channel INTO one of our indicators" in _LLM_SRC


def test_old_cover_all_four_themes_leadin_gone():
    assert "Content — cover all four themes in this order" not in _LLM_SRC
