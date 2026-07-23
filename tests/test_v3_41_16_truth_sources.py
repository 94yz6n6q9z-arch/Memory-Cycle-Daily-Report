"""v3.41.16 — sources of truth: evidence invalidation (t4 v2 repair),
deterministic Forward P/E status, spec hygiene.

Jul 22 exposed three truth dislocations in one brief:
(1) The v1 t4 repair reverted the STATUS but left the Jul-20 pro-fire
    evidence text in the store — Layer 2 read it as prior context and
    RE-FIRED in the same run. Exec prose narrated the repair annotation
    ("reverting to Not fired") while the tier line said T4 1/4 and the card
    said Fired: three sources of truth, three answers.
(2) t2_pe_compressed showed all three names below threshold on its own card
    (SKH 4.07x<5, Samsung 3.91x<6, Micron 6.01x<8) yet Haiku returned
    'Not fired' "pending broader estimate rollover" — violating the
    prompt's explicit [A]-category rule. Prompt rules are requests;
    a pure-numeric signal's status must be arithmetic.
(3) The spec text itself carried stale point-in-time numbers ("currently
    3.7x") shown beside live computed values.

Fixes: v2 repair invalidates the poisoned entries (INVALIDATED prefix) and
is immune to its own v1 annotation; INVALID FIRES prompt rule ('Samsung' +
'layoffs' is never sufficient — explicit memory-operations attribution
required; INVALIDATED entries are not evidence); deterministic P/E override
runs before the evidence-store update; spec texts carry thresholds only.
"""
import pytest

from src.config import INDICATORS
from src.analysis.signal_reasoner import REASONER_SYSTEM_PROMPT, SignalReasoning
from src.fast_tier import _override_t2_pe_from_computed
from src.state import (
    T4_BU_REPAIR_V2_TAG,
    T4_INVALIDATED_PREFIX,
    EvidenceEntry,
    IndicatorEvidence,
    repair_t4_layoffs_bu_misfire,
)


def _ev(v, src="Layer 2 reasoner (3 sources)", date="2026-07-20"):
    return EvidenceEntry(date=date, type="data_point", value=v, source=src,
                         persistence="live", reversal_condition="x")


def _t4(entries, status="Fired"):
    return {"t4_layoffs": IndicatorEvidence(
        indicator_id="t4_layoffs", current_status=status,
        fired_since="2026-07-20", last_updated="2026-07-22",
        evidence_log=entries)}


PROD_ENTRIES = lambda: [
    _ev("Layer 2 transition Not fired -> Fired. Reasoning: Samsung confirms "
        "800+ US job cuts across its smartphone division and Texas "
        "headquarters, corroborated by three sources"),
    _ev("REPAIR v3.41.15 (t4_layoffs BU scope): prior Fired was based on "
        "Samsung smartphone/consumer-division job cuts — a non-memory "
        "business unit. Status reverted to Not fired.",
        src="v3.41.15 one-shot BU-scope repair"),
    _ev("Layer 2 transition Not fired -> Fired. Reasoning: Samsung confirmed "
        "layoffs of 800+ US employees affecting smartphone division and "
        "Texas HQ operations. Three independent sources corroborate."),
]


# ---- (1) repair v2 ------------------------------------------------------------

def test_v2_neutralizes_the_exact_prod_store():
    s = _t4(PROD_ENTRIES())
    r = repair_t4_layoffs_bu_misfire(s)
    ev = s["t4_layoffs"]
    assert r is not None and r["status_reverted"] is True and r["invalidated"] == 2
    assert ev.current_status == "Not fired"
    assert sum(1 for e in ev.evidence_log
               if e.value.startswith(T4_INVALIDATED_PREFIX)) == 2
    # the v1 annotation is a marker, never rewritten
    assert any(e.value.startswith("REPAIR v3.41.15") for e in ev.evidence_log)
    assert any(T4_BU_REPAIR_V2_TAG in e.value for e in ev.evidence_log)


def test_v2_runs_exactly_once():
    s = _t4(PROD_ENTRIES())
    assert repair_t4_layoffs_bu_misfire(s) is not None
    assert repair_t4_layoffs_bu_misfire(s) is None
    assert s["t4_layoffs"].current_status == "Not fired"


def test_v1_annotation_cannot_fake_memory_context():
    # 'non-memory business unit' contains the substring 'memory' — the scrub
    # must prevent the v1 annotation from tripping the mixed-evidence guard
    # (this is exactly how the prod store looks today).
    s = _t4(PROD_ENTRIES())
    assert repair_t4_layoffs_bu_misfire(s) is not None  # acted despite v1 text


def test_genuine_memory_evidence_freezes_everything():
    s = _t4([_ev("Samsung layoffs smartphone division"),
             _ev("SK Hynix idles DRAM fab lines, memory workforce reduction")])
    assert repair_t4_layoffs_bu_misfire(s) is None
    assert s["t4_layoffs"].current_status == "Fired"
    assert not any(e.value.startswith(T4_INVALIDATED_PREFIX)
                   for e in s["t4_layoffs"].evidence_log)


def test_invalidates_even_when_already_not_fired():
    # If Layer 2 self-reverted before deploy, the poisoned text must still be
    # neutralized so nothing can re-fire on it later.
    s = _t4([_ev("Samsung confirms smartphone division layoffs, Texas HQ")],
            status="Not fired")
    r = repair_t4_layoffs_bu_misfire(s)
    assert r is not None and r["status_reverted"] is False
    assert s["t4_layoffs"].evidence_log[0].value.startswith(T4_INVALIDATED_PREFIX)


def test_noop_when_clean_or_absent():
    assert repair_t4_layoffs_bu_misfire(
        _t4([_ev("Micron memory fab utilization cut")])) is None
    assert repair_t4_layoffs_bu_misfire({}) is None
    assert repair_t4_layoffs_bu_misfire(None) is None


# ---- (2) prompt + spec: 'Samsung + layoffs' is never sufficient ----------------

def test_prompt_invalid_fires_rule():
    assert "INVALID FIRES" in REASONER_SYSTEM_PROMPT
    assert "'Samsung' + 'layoffs' is NEVER sufficient" in REASONER_SYSTEM_PROMPT
    assert "RE-FIRING" in REASONER_SYSTEM_PROMPT
    assert 'begins with "INVALIDATED"' in REASONER_SYSTEM_PROMPT


def test_t4_spec_requires_explicit_memory_attribution():
    t4 = next(i for i in INDICATORS if i.id == "t4_layoffs")
    assert "'Samsung announces layoffs' alone is NEVER" in t4.what_to_look_for
    assert "memory operations/employees" in t4.what_to_look_for


# ---- (3) deterministic P/E ------------------------------------------------------

def _sr(status):
    return SignalReasoning(
        signal_id="t2_pe_compressed", signal_name="Forward P/E", tier=2,
        evidence_count=0, evidence_sources=[], consensus_direction="x",
        convergence="low", fired_status=status, fired_status_evidence=[],
        evidence_for_change_vs_yesterday="", key_quotes_consolidated=[],
        confidence="low", raw_evidence_dump={})


def test_pe_override_fires_on_the_jul22_numbers():
    srs = [_sr("Not fired")]
    _override_t2_pe_from_computed(
        srs, {"forward_pe": {"000660.KS": 4.07, "005930.KS": 3.91, "MU": 6.01}})
    assert srs[0].fired_status == "Fired"
    assert "Deterministic override" in srs[0].evidence_for_change_vs_yesterday
    assert srs[0].fired_status_evidence[0].startswith("Deterministic override")


def test_pe_override_unfires_when_none_breach():
    srs = [_sr("Fired")]
    _override_t2_pe_from_computed(
        srs, {"forward_pe": {"000660.KS": 6.2, "005930.KS": 7.1, "MU": 9.5}})
    assert srs[0].fired_status == "Not fired"


def test_pe_single_name_breach_is_sufficient():
    srs = [_sr("Not fired")]
    _override_t2_pe_from_computed(srs, {"forward_pe": {"000660.KS": 4.5}})
    assert srs[0].fired_status == "Fired"


def test_pe_no_data_leaves_layer2_verdict():
    srs = [_sr("Partially fired")]
    _override_t2_pe_from_computed(srs, {"forward_pe": {}})
    assert srs[0].fired_status == "Partially fired"
    _override_t2_pe_from_computed(srs, {})
    assert srs[0].fired_status == "Partially fired"


def test_pe_override_runs_before_evidence_store_update():
    src = open("src/fast_tier.py").read()
    assert src.index("_override_t2_pe_from_computed(signal_reasonings") \
        < src.index("update_evidence_after_layer2(indicator_evidence_store")


def test_spec_texts_carry_no_point_in_time_numbers():
    import re
    cfg = open("src/config.py").read()
    assert not re.search(r"currently \d", cfg)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
