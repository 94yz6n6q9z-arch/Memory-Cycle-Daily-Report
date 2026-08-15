"""v3.41.17 — four fixes from the Jul 23 brief.

(1) BANNER: the fast_tier shape-conversion stripped old_status/new_status
    ("the dict shape ExecutiveSummary expects"), so the P/E fire rendered as
    an amber DOWNGRADE with "Status: ? → ?". Conversion now keeps the keys,
    Sonnet's example emits them, the banner parses the description as
    fallback, never prints "?", and unknown direction alarms red.
(2) SCENARIOS: bear return was a Sonnet judgment number with no anchor.
    Now deterministic: trough = cycle peak × (1 − 57.5%) (docx Part 2:
    memory pure-plays 50–65% peak-to-trough), return computed FROM CURRENT
    price, anchor numbers attached for the email's context line.
(3) CAPEX FIRST-PARTY: analyst forecasts were credited with meeting a
    company-guidance threshold. Prompt rules added in both layers; the
    extractor gains a capex_signals group (first-party prints only) and a
    fast-tier ingest lands them as t2_capex_parabolic evidence.
(4) T4 GATE: prompt control failed twice; discretion withdrawn. A Fired
    verdict without explicit memory-operations attribution in its evidence
    is forced to Not fired; the repair's invalidation is now idempotent so
    the Jul-22/23 re-fire entries get cleaned too.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import src.email_module.template as T
from src.analysis.news_event_extractor import (
    NEWS_EVENT_SCHEMA,
    _entries_from_extractions,
)
from src.analysis.signal_reasoner import REASONER_SYSTEM_PROMPT, SignalReasoning
from src.config import BEAR_TROUGH_FROM_PEAK_PCT, BEAR_TROUGH_BAND_PCT
from src.fast_tier import (
    _gate_t4_layoffs,
    _ingest_capex_guidance_evidence,
    _reanchor_bear_case,
)
from src.state import (
    EvidenceEntry,
    IndicatorEvidence,
    T4_INVALIDATED_PREFIX,
    repair_t4_layoffs_bu_misfire,
)


def _sr(status, cons="Samsung confirmed 800+ layoffs smartphone division", **kw):
    d = dict(signal_id="t4_layoffs", signal_name="Layoffs", tier=4,
             evidence_count=3, evidence_sources=["a"], consensus_direction=cons,
             convergence="high", fired_status=status, fired_status_evidence=[],
             evidence_for_change_vs_yesterday="", key_quotes_consolidated=[],
             confidence="high", raw_evidence_dump={})
    d.update(kw)
    return SignalReasoning(**d)


# ---- (4) the gate --------------------------------------------------------------

def test_gate_forces_not_fired_without_memory_attribution():
    r = [_sr("Fired")]
    _gate_t4_layoffs(r)
    assert r[0].fired_status == "Not fired"
    assert "Deterministic BU gate" in r[0].evidence_for_change_vs_yesterday


def test_gate_rejects_bare_memory_chipmaker_phrasing():
    # press routinely writes "memory chip maker Samsung" — that is naming,
    # not operations attribution, and must not open the gate
    r = [_sr("Fired", cons="memory chip maker Samsung cuts smartphone jobs")]
    _gate_t4_layoffs(r)
    assert r[0].fired_status == "Not fired"


def test_gate_passes_genuine_memory_operations_fire():
    r = [_sr("Fired", cons="SK Hynix idles DRAM fab lines; memory workforce cut")]
    _gate_t4_layoffs(r)
    assert r[0].fired_status == "Fired"


def test_gate_passthrough_on_not_fired():
    r = [_sr("Not fired")]
    _gate_t4_layoffs(r)
    assert r[0].fired_status == "Not fired"
    assert r[0].evidence_for_change_vs_yesterday == ""


def _ev(v, src="Layer 2 reasoner"):
    return EvidenceEntry(date="2026-07-23", type="data_point", value=v,
                         source=src, persistence="live", reversal_condition="x")


def test_repair_now_idempotent_cleans_post_v2_refire():
    # exact current prod shape: prior INVALIDATED entry + v2 annotation +
    # the Jul-22/23 re-fire entry, status Fired — v2's run-once tag used to
    # freeze this forever
    prod = {"t4_layoffs": IndicatorEvidence(
        indicator_id="t4_layoffs", current_status="Fired",
        fired_since="2026-07-20", last_updated="2026-07-23",
        evidence_log=[
            _ev(T4_INVALIDATED_PREFIX + "Layer 2 transition ... smartphone division"),
            _ev("REPAIR v3.41.16 (t4_layoffs BU scope v2): 2 entries invalidated",
                src="v3.41.16 one-shot BU-scope repair (v2)"),
            _ev("Layer 2 transition Not fired -> Fired. Reasoning: Samsung "
                "confirmed layoffs of 800+ US employees smartphone division "
                "Texas HQ. Three sources corroborate."),
        ])}
    r = repair_t4_layoffs_bu_misfire(prod)
    assert r is not None and r["invalidated"] == 1 and r["status_reverted"]
    assert prod["t4_layoffs"].current_status == "Not fired"
    assert repair_t4_layoffs_bu_misfire(prod) is None  # same-day idempotent


def test_repair_reverts_fired_standing_only_on_invalidated_evidence():
    s = {"t4_layoffs": IndicatorEvidence(
        indicator_id="t4_layoffs", current_status="Fired",
        fired_since="2026-07-20", last_updated="2026-07-23",
        evidence_log=[_ev(T4_INVALIDATED_PREFIX + "old smartphone entry")])}
    r = repair_t4_layoffs_bu_misfire(s)
    assert r is not None and r["status_reverted"] and r["invalidated"] == 0
    assert s["t4_layoffs"].current_status == "Not fired"


# ---- (1) banner ----------------------------------------------------------------

def test_banner_parses_description_and_renders_red_for_fire():
    h = T._build_generic_trigger_banner({
        "indicator_id": "t2_pe_compressed",
        "indicator_name": "Forward P/E compressed to 3-5x", "tier": 2,
        "description": "Status changed from 'Not fired' to 'Fired'."})
    assert "🚨 NEW TIER 2 TRIGGER" in h
    assert "Not fired → Fired" in h
    assert "?" not in h
    assert "SIGNAL DOWNGRADE" not in h


def test_banner_keys_beat_description_and_downgrade_is_green():
    # v3.41.18 — destination coloring: → Not fired renders GREEN (was amber).
    h = T._build_generic_trigger_banner({
        "indicator_id": "x", "indicator_name": "X", "tier": 4,
        "old_status": "Fired", "new_status": "Not fired",
        "description": "Correction: BU gate"})
    assert "✅ SIGNAL DOWNGRADE" in h and "Fired → Not fired" in h
    assert "#EAF4E2" in h and "Correction: BU gate" in h


def test_banner_unknown_direction_alarms_red_without_question_marks():
    h = T._build_generic_trigger_banner({
        "indicator_id": "x", "indicator_name": "X", "tier": 1,
        "description": "something without statuses"})
    assert "🚨" in h and "something without statuses" in h
    assert "Status: ?" not in h


def test_conversion_keeps_statuses_and_logs_source():
    src = open("src/fast_tier.py").read()
    assert '"old_status": e.get("old_status")' in src
    assert '"new_status": e.get("new_status")' in src
    assert "[trigger-log] source=authoritative" in src


def test_sonnet_example_carries_statuses():
    s = open("src/analysis/llm_summarizer.py").read()
    assert '"old_status": "Not fired", "new_status": "Fired"' in s


# ---- (2) scenario re-anchor -----------------------------------------------------

def _price_history(peak=2_620_000.0, current=1_840_000.0):
    idx = pd.date_range("2023-08-01", periods=750, freq="D")
    s = pd.Series(1_000_000.0, index=idx)
    s.iloc[-120] = peak
    s.iloc[-1] = current
    return pd.DataFrame({"SK Hynix": s})


def test_bear_reanchored_from_current_price():
    es = SimpleNamespace(bear_case=SimpleNamespace(return_to_peak_pct=-40.0))
    analysis = SimpleNamespace(executive_summary=es)
    _reanchor_bear_case(analysis, _price_history())
    # v3.47.0 — all cases re-anchor; bear = full historical magnitude (-65).
    from src.config import PHASE5_TROUGH_ANCHORS_FROM_PEAK
    expected = (2_620_000 * (1 + PHASE5_TROUGH_ANCHORS_FROM_PEAK["bear"] / 100) / 1_840_000 - 1) * 100
    assert abs(es.bear_case.return_to_peak_pct - expected) < 0.1
    sa = es.scenario_anchor
    assert abs(sa["drawdown_now_pct"] - ((1_840_000 / 2_620_000 - 1) * 100)) < 0.1
    assert sa["band"] == tuple(BEAR_TROUGH_BAND_PCT)


def test_reanchor_noops_without_data():
    es = SimpleNamespace(bear_case=SimpleNamespace(return_to_peak_pct=-5.0))
    _reanchor_bear_case(SimpleNamespace(executive_summary=es), pd.DataFrame())
    assert es.bear_case.return_to_peak_pct == -5.0
    assert not hasattr(es, "scenario_anchor")


def test_docx_band_encoded():
    assert BEAR_TROUGH_FROM_PEAK_PCT == -57.5
    assert tuple(BEAR_TROUGH_BAND_PCT) == (-65.0, -50.0)


# ---- (3) capex first-party -------------------------------------------------------

def test_prompt_rules_present_in_both_layers():
    assert "FIRST-PARTY GUIDANCE PRECEDENCE" in REASONER_SYSTEM_PROMPT
    s = open("src/analysis/llm_summarizer.py").read()
    assert "FIRST-PARTY GUIDANCE PRECEDENCE" in s
    assert "Citi projects 2027 CapEx" in s  # the named counterexample


def test_extractor_capex_group_and_conversion():
    item = NEWS_EVENT_SCHEMA["properties"]["extractions"]["items"]
    assert "capex_signals" in item["properties"]
    assert "capex_signals" in item["required"]
    news = [SimpleNamespace(title="Alphabet Q2", url="https://x/g",
                            source="Reuters", published="2026-07-23T01:00:00Z",
                            summary="")]
    parsed = {"extractions": [{
        "news_index": 0, "adr_signals": None, "competitor_signals": None,
        "capex_signals": {"company": "Alphabet",
                          "guides_2027_capex_above_2026": True,
                          "capex_2026_guidance_usd_b": 200.0,
                          "note": "increase significantly"}}]}
    entries = _entries_from_extractions(parsed, news, "2026-07-23T09:00:00Z")
    assert len(entries) == 1
    assert entries[0]["capex_signals"]["guides_2027_capex_above_2026"] is True


def test_capex_ingest_appends_deduped_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    entry = {"source_id": "newsevent:capex_signals:https://x/g",
             "source": "reuters", "date": f"{today}T09:00:00Z",
             "capex_signals": {"company": "Alphabet",
                               "guides_2027_capex_above_2026": True,
                               "capex_2026_guidance_usd_b": 200.0,
                               "note": "increase significantly"}}
    (tmp_path / "state" / f"extraction_log_{today}.jsonl").write_text(
        json.dumps(entry) + "\n")
    store = {"t2_capex_parabolic": IndicatorEvidence(
        indicator_id="t2_capex_parabolic", current_status="Fired",
        fired_since="2026-05-01", last_updated="2026-07-22", evidence_log=[])}
    _ingest_capex_guidance_evidence(store)
    vals = [e.value for e in store["t2_capex_parabolic"].evidence_log]
    assert len(vals) == 1 and vals[0].startswith("FIRST-PARTY: Alphabet guides 2027")
    _ingest_capex_guidance_evidence(store)
    assert len(store["t2_capex_parabolic"].evidence_log) == 1  # dedup


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
