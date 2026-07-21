"""v3.41.15 — t4_layoffs BU scoping + one-shot misfire repair + generic
transition banners.

Jul 20: t4_layoffs fired on "Samsung confirms 800+ US job cuts across its
smartphone division" — a consumer-devices event. Root cause was the spec
itself: what_to_look_for read "Any Big Three announces layoffs or fab
idling" — entity-scoped, never BU-scoped — and that text is embedded
verbatim in the Layer-2 prompt. The model obeyed a wrong spec. The wrong
fire then persisted (cumulative-state evidence store, by design) and moved
the T4 scorecard + peak probabilities. Separately, the transition produced
no top-of-brief alarm because only two bespoke banners exist.

Fixes: (1) spec re-written with mandatory business-unit scoping + the exact
counterexample; (2) a general conglomerate/BU-attribution rule in
REASONER_SYSTEM_PROMPT protecting every memory-sector signal; (3) a
conservative one-shot repair reverting the misfire in the evidence store
(mixed evidence → hands off + loud warning); (4) a generic transition
banner (red for fired-direction, amber for downgrades) for every 24h-log
event not carried by a bespoke banner that run.
"""
import pytest

import src.email_module.template as T
from src.config import INDICATORS
from src.analysis.signal_reasoner import REASONER_SYSTEM_PROMPT
from src.state import (
    T4_BU_REPAIR_TAG,
    EvidenceEntry,
    IndicatorEvidence,
    repair_t4_layoffs_bu_misfire,
)


def _store(values, status="Fired"):
    return {"t4_layoffs": IndicatorEvidence(
        indicator_id="t4_layoffs", current_status=status,
        fired_since="2026-07-20", last_updated="2026-07-20",
        evidence_log=[EvidenceEntry(
            date="2026-07-20", type="data_point", value=v,
            source="Layer 2 reasoner (3 sources)", persistence="live",
            reversal_condition="x") for v in values])}


JUL20_EVIDENCE = ("Layer 2 transition Not fired -> Fired. Reasoning: Samsung "
                  "confirms 800+ US job cuts across its smartphone division "
                  "and Texas headquarters, corroborated by three sources")


# ---- (1) the spec -------------------------------------------------------------

def test_t4_spec_is_bu_scoped_with_counterexample():
    t4 = next(i for i in INDICATORS if i.id == "t4_layoffs")
    w = t4.what_to_look_for
    assert "MEMORY OPERATIONS" in w
    assert "conglomerate" in w
    assert "smartphone" in w and "do NOT fire" in w      # the Jul-20 case, named
    assert "pure-play" in w.lower()                       # SK/Micron company-level OK
    assert "Any Big Three announces layoffs" not in w     # the old blanket spec is gone


# ---- (2) the general Layer-2 rule ---------------------------------------------

def test_reasoner_prompt_carries_conglomerate_rule():
    assert "CONGLOMERATE / BUSINESS-UNIT ATTRIBUTION" in REASONER_SYSTEM_PROMPT
    assert "must NOT fire t4_layoffs" in REASONER_SYSTEM_PROMPT
    # applies beyond layoffs
    assert "capex, margin, inventory" in REASONER_SYSTEM_PROMPT


# ---- (3) the repair -----------------------------------------------------------

def test_repair_reverts_the_jul20_misfire_and_annotates():
    s = _store([JUL20_EVIDENCE])
    r = repair_t4_layoffs_bu_misfire(s)
    assert r is not None
    assert r["old_status"] == "Fired" and r["new_status"] == "Not fired"
    assert s["t4_layoffs"].current_status == "Not fired"
    assert any(T4_BU_REPAIR_TAG in e.value for e in s["t4_layoffs"].evidence_log)


def test_repair_runs_exactly_once():
    s = _store([JUL20_EVIDENCE])
    assert repair_t4_layoffs_bu_misfire(s) is not None
    assert repair_t4_layoffs_bu_misfire(s) is None
    assert s["t4_layoffs"].current_status == "Not fired"


def test_repair_hands_off_on_mixed_evidence():
    s = _store(["Samsung layoffs smartphone division",
                "Samsung idles DRAM fab lines, memory workforce cut"])
    assert repair_t4_layoffs_bu_misfire(s) is None
    assert s["t4_layoffs"].current_status == "Fired"


def test_repair_never_touches_a_legitimate_memory_fire():
    s = _store(["Micron announces memory fab utilization cut and layoffs"])
    assert repair_t4_layoffs_bu_misfire(s) is None
    assert s["t4_layoffs"].current_status == "Fired"


def test_repair_noops_when_not_fired_or_absent():
    assert repair_t4_layoffs_bu_misfire(_store(["x"], status="Not fired")) is None
    assert repair_t4_layoffs_bu_misfire({}) is None
    assert repair_t4_layoffs_bu_misfire(None) is None


# ---- (4) generic banners ------------------------------------------------------

def _trig(old, new, name="Layoffs / fab idling", tier=4, desc="d"):
    return {"indicator_id": "t4_layoffs", "indicator_name": name, "tier": tier,
            "old_status": old, "new_status": new, "description": desc}


def test_fired_direction_renders_red_alarm():
    h = T._build_generic_trigger_banner(_trig("Not fired", "Fired",
                                              desc="Samsung confirms cuts"))
    assert "🚨 NEW TIER 4 TRIGGER" in h and "Layoffs / fab idling" in h
    assert "#A32D2D" in h and "Not fired → Fired" in h and "Samsung confirms" in h
    assert "🚨" in T._build_generic_trigger_banner(_trig("Not fired", "Partially fired"))


def test_downgrade_renders_amber_with_description():
    h = T._build_generic_trigger_banner(
        _trig("Fired", "Not fired", desc="Correction: non-memory BU"))
    assert "⚠️ SIGNAL DOWNGRADE" in h and "#B58A1A" in h
    assert "Fired → Not fired" in h and "Correction" in h


def test_banner_block_wired_with_bespoke_dedup_and_cap():
    import inspect
    src = inspect.getsource(T.build_email_html)
    assert "_bespoke_rendered" in src
    assert "_build_generic_trigger_banner(_trig)" in src
    assert "_generic[:4]" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
