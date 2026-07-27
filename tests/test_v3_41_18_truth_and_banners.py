"""v3.41.18 — four fixes: banner destination colors + readability + evidence
context; the P/E truth chain (rendered-status override, prose-cache guard on
the field that renders, forward-P/E data fallback, prompt scoping); the
news_event_extractor schema 400 (dead lane since v3.41.13) + failure
sentinel; t4 repair negation predicates (frozen since v1 on the May-9
bootstrap absence entry + the Jul-24 'not memory' annotation).

Fixture discipline (v3.41.16 lesson): the t4 tests run the repair against the
VERBATIM production evidence entries, including the May-9 bootstrap line the
v3.41.16 fixture omitted — the very entry that froze the repair.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analysis import news_event_extractor as nee
from src.analysis import prose_cache as pc
from src.email_module import template as T
import src.fast_tier as FT
import src.state as S


# ---- shared fakes --------------------------------------------------------------

@dataclass
class FakeAssessment:
    id: str
    name: str = "X"
    tier: int = 2
    current_status: str = "Not fired"
    change_vs_prior: object = None


@dataclass
class FakeAnalysis:
    indicator_assessments: list = field(default_factory=list)


@dataclass
class FakeReasoning:
    signal_id: str
    fired_status: str = "Not fired"
    evidence_for_change_vs_yesterday: str = ""
    consensus_direction: str = ""


# ---- (3) extractor schema + sentinel -------------------------------------------

class TestExtractorSchemaAndSentinel:
    def test_schema_carries_no_enum_anywhere(self):
        # The API validator rejects enum-on-union ("Enum value 'strong' does
        # not match declared type") — the one line that 400'd every call
        # Jul-10 → Jul-25. No enum may reappear anywhere in the schema.
        def walk(o):
            if isinstance(o, dict):
                assert "enum" not in o, f"enum present: {o}"
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(nee.NEWS_EVENT_SCHEMA)

    def test_demand_classification_normalized_in_converter(self):
        class N:
            def __init__(self, t, u):
                self.title, self.url = t, u
                self.source, self.published = "reuters", "2026-07-27"
        items = [N("t0", "http://a"), N("t1", "http://b"), N("t2", "http://c")]
        parsed = {"extractions": [
            {"news_index": 0, "adr_signals": {"demand_classification": "Strong"}},
            {"news_index": 1, "adr_signals": {"demand_classification": "blockbuster",
                                              "roadshow_active": True}},
            {"news_index": 2, "adr_signals": {"demand_classification": None,
                                              "f1_canceled": True}},
        ]}
        es = nee._entries_from_extractions(parsed, items, "2026-07-27T00:00:00Z")
        assert es[0]["adr_signals"]["demand_classification"] == "strong"
        assert "demand_classification" not in es[1]["adr_signals"]  # garbage → dropped
        assert es[1]["adr_signals"]["roadshow_active"] is True
        assert "demand_classification" not in es[2]["adr_signals"]

    def test_sentinel_counts_escalates_and_resets(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        for _ in range(nee.SENTINEL_THRESHOLD - 1):
            nee._sentinel_failure()
        assert nee._sentinel_read() == nee.SENTINEL_THRESHOLD - 1
        import logging
        with caplog.at_level(logging.ERROR):
            nee._sentinel_failure()
        assert any("SENTINEL" in r.message for r in caplog.records)
        nee._sentinel_success()
        assert nee._sentinel_read() == 0

    def test_api_failure_path_increments_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        class Boom:
            class messages:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("400 schema")

        class N:
            title, url, source, published = "t", "u", "reuters", "2026-07-27"

        out = nee.extract_and_log_news_events(None, [N()], client=Boom())
        assert out["reason"] == "api_error"
        assert nee._sentinel_read() == 1


# ---- (4) t4 predicates ---------------------------------------------------------

# VERBATIM production entries (state/indicator_evidence.json, Jul-25 state).
_T4_PROD_BOOTSTRAP = ("No layoffs or fab idling announced at any of the three "
                      "majors")
_T4_PROD_POISON = ("Layer 2 transition Not fired -> Fired. Reasoning: Samsung "
                   "has announced layoffs of over 800 US employees, primarily "
                   "affecting smartphone division and related to Texas "
                   "headquarters consolidation.")
_T4_PROD_REVERT_NOTE = ("Layer 2 transition Fired -> Not fired. Reasoning: No "
                        "new evidence today. Prior Layer 2 transition "
                        "(2026-07-20) recorded Samsung US layoffs affecting "
                        "smartphone division, not memory operations. Per "
                        "v3.41.16 rule, non-memory business-unit layoffs do no")


def _prod_store(status="Not fired"):
    ev = S.IndicatorEvidence(
        indicator_id="t4_layoffs", current_status=status,
        fired_since="2026-07-20", last_updated="2026-07-24",
        evidence_log=[
            S.EvidenceEntry(date="2026-05-09", type="data_point",
                            value=_T4_PROD_BOOTSTRAP,
                            source="Korean press + SEC filings (per May 9 framework doc)",
                            persistence="quarterly", reversal_condition="x"),
            S.EvidenceEntry(date="2026-07-20", type="data_point",
                            value=_T4_PROD_POISON,
                            source="Layer 2 reasoner (3 sources)",
                            persistence="live", reversal_condition="x"),
            S.EvidenceEntry(date="2026-07-24", type="data_point",
                            value=_T4_PROD_REVERT_NOTE,
                            source="Layer 2 reasoner (0 sources)",
                            persistence="live", reversal_condition="x"),
        ])
    return {"t4_layoffs": ev}


class TestT4Predicates:
    def test_bootstrap_absence_entry_is_absence_not_memory_context(self):
        assert S._t4_is_absence(_T4_PROD_BOOTSTRAP)
        # 'fab idl' substring must no longer freeze the repair via this entry

    def test_not_memory_negation_scrubbed(self):
        scrubbed = S._t4_scrub(_T4_PROD_REVERT_NOTE)
        assert "not memory" not in scrubbed
        # after scrub the note carries no affirmative memory context term
        assert not any(t in scrubbed for t in S._T4_MEMORY_CTX)

    def test_revert_note_is_absence_excluded(self):
        # "No new evidence today" → absence → excluded from BOTH predicates,
        # so the corrective note is never itself invalidated as poison.
        assert S._t4_is_absence(_T4_PROD_REVERT_NOTE)

    def test_repair_on_exact_prod_store_invalidates_only_jul20(self):
        store = _prod_store()
        r = S.repair_t4_layoffs_bu_misfire(store)
        assert r is not None and r["invalidated"] == 1
        assert r["status_reverted"] is False  # already Not fired
        ev = store["t4_layoffs"]
        inv = [e for e in ev.evidence_log
               if e.value.startswith(S.T4_INVALIDATED_PREFIX)]
        assert len(inv) == 1 and "smartphone division" in inv[0].value
        assert not any(e.value.startswith(S.T4_INVALIDATED_PREFIX)
                       for e in ev.evidence_log
                       if "No new evidence today" in e.value)

    def test_repair_second_run_is_noop(self):
        store = _prod_store()
        S.repair_t4_layoffs_bu_misfire(store)
        n = len(store["t4_layoffs"].evidence_log)
        assert S.repair_t4_layoffs_bu_misfire(store) is None
        assert len(store["t4_layoffs"].evidence_log) == n

    def test_standing_fired_on_poison_still_reverts(self):
        store = _prod_store(status="Fired")
        r = S.repair_t4_layoffs_bu_misfire(store)
        assert r is not None and r["status_reverted"] is True
        assert store["t4_layoffs"].current_status == "Not fired"

    def test_genuine_memory_fire_still_freezes_repair(self):
        store = _prod_store()
        store["t4_layoffs"].evidence_log.append(S.EvidenceEntry(
            date="2026-07-27", type="data_point",
            value=("SK Hynix idles M15 DRAM fab lines and cuts memory "
                   "division workforce by 5%"),
            source="Reuters", persistence="quarterly", reversal_condition="x"))
        assert S.repair_t4_layoffs_bu_misfire(store) is None


# ---- (2) P/E truth chain -------------------------------------------------------

class TestPeAssessmentOverride:
    def test_breach_forces_fired_with_note(self):
        an = FakeAnalysis([FakeAssessment("t2_pe_compressed")])
        FT._override_t2_pe_assessment(
            an, {"forward_pe": {"000660.KS": 4.2, "005930.KS": 7.0, "MU": 9.0}}, [])
        a = an.indicator_assessments[0]
        assert a.current_status == "Fired"
        assert "deterministically" in a.change_vs_prior

    def test_no_breach_forces_not_fired(self):
        an = FakeAnalysis([FakeAssessment("t2_pe_compressed", current_status="Fired")])
        FT._override_t2_pe_assessment(
            an, {"forward_pe": {"000660.KS": 5.5, "005930.KS": 7.0, "MU": 9.0}}, [])
        assert an.indicator_assessments[0].current_status == "Not fired"

    def test_no_data_slaves_to_layer2_verdict(self):
        # The exact Jul-23 production shape: computed empty, Sonnet 'Fired',
        # Layer 2 'Not fired' — Sonnet's arithmetic is overruled.
        an = FakeAnalysis([FakeAssessment("t2_pe_compressed", current_status="Fired")])
        FT._override_t2_pe_assessment(
            an, {"forward_pe": {}},
            [FakeReasoning("t2_pe_compressed", fired_status="Not fired")])
        a = an.indicator_assessments[0]
        assert a.current_status == "Not fired"
        assert "no computed forward P/E" in a.change_vs_prior

    def test_no_data_no_layer2_leaves_untouched(self):
        an = FakeAnalysis([FakeAssessment("t2_pe_compressed", current_status="Fired")])
        FT._override_t2_pe_assessment(an, {"forward_pe": {}}, [])
        assert an.indicator_assessments[0].current_status == "Fired"

    def test_agreement_leaves_card_alone(self):
        an = FakeAnalysis([FakeAssessment("t2_pe_compressed", current_status="Fired")])
        FT._override_t2_pe_assessment(an, {"forward_pe": {"000660.KS": 4.2}}, [])
        a = an.indicator_assessments[0]
        assert a.current_status == "Fired" and a.change_vs_prior is None

    def test_override_runs_before_trigger_log_in_pipeline(self):
        import inspect
        src = inspect.getsource(FT.run_fast_tier)
        assert src.index("_override_t2_pe_assessment") < src.index(
            "update_trigger_log_24h(")


class TestProseCacheRenderedGuard:
    def test_rendered_status_mismatch_forces_regen(self):
        # The frozen Jul-23 card: cached status (Layer 2) = live status, but
        # the RENDERED current_status disagrees — old guard reused it forever.
        cache = {"t2_pe_compressed": {"status": "Not fired",
                                      "current_status": "Fired",
                                      "where_we_stand": "x"}}
        d = {"t2_pe_compressed": {"action": "reuse"}}
        pc.apply_status_guard(d, cache, {"t2_pe_compressed": "Not fired"})
        assert d["t2_pe_compressed"]["action"] == "regen"
        assert "rendered_status_mismatch" in d["t2_pe_compressed"]["reason"]

    def test_layer2_mismatch_still_caught_first(self):
        cache = {"a": {"status": "Fired", "current_status": "Fired"}}
        d = {"a": {"action": "reuse"}}
        pc.apply_status_guard(d, cache, {"a": "Not fired"})
        assert d["a"]["reason"].startswith("status_mismatch")

    def test_consistent_card_stays_reused_and_legacy_entries_unaffected(self):
        cache = {"a": {"status": "Fired", "current_status": "Fired"},
                 "b": {"status": "Fired"}}  # legacy: no current_status
        d = {"a": {"action": "reuse"}, "b": {"action": "reuse"}}
        pc.apply_status_guard(d, cache, {"a": "Fired", "b": "Fired"})
        assert d["a"]["action"] == "reuse" and d["b"]["action"] == "reuse"


class TestPeDataFallback:
    def _snap(self, monkeypatch, info):
        import src.data.estimates as E

        class FakeT:
            def __init__(self, sym):
                self.info = info

        monkeypatch.setattr(E.yf, "Ticker", FakeT)
        return E.fetch_estimates_yfinance("000660.KS", "SK Hynix")

    def test_forward_pe_used_when_present(self, monkeypatch):
        s = self._snap(monkeypatch, {"forwardPE": 4.5})
        assert s.forward_pe == 4.5

    def test_computed_from_price_over_forward_eps(self, monkeypatch):
        s = self._snap(monkeypatch,
                       {"forwardEps": 400000.0, "currentPrice": 1760000.0})
        assert s.forward_pe == pytest.approx(4.4)

    def test_unavailable_when_no_inputs(self, monkeypatch):
        s = self._snap(monkeypatch, {})
        assert s.forward_pe is None

    def test_zero_or_negative_eps_never_divides(self, monkeypatch):
        s = self._snap(monkeypatch,
                       {"forwardEps": 0, "currentPrice": 1760000.0})
        assert s.forward_pe is None


class TestPromptScoping:
    def test_absent_metric_rule_present_and_leak_instruction_gone(self):
        src = open("src/analysis/llm_summarizer.py").read()
        assert "WHEN THE COMPUTED METRIC IS ABSENT" in src
        assert "MUST NOT\nsubstitute your own arithmetic" in src.replace("\r", "") \
            or "MUST NOT substitute your own arithmetic" in " ".join(src.split())
        # The old example literally instructed Sonnet to write a version-tag
        # phrase into where_we_stand — the leak-teaching text must be gone.
        assert "overridden per v3.39.7.3 computed-metric authority rule" not in src


# ---- (1) trigger-log enrichment + banners --------------------------------------

class TestTriggerLogEnrichment:
    def test_update_uses_evidence_and_falls_back_generic(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "state").mkdir()
        log = S.update_trigger_log_24h(
            {"a": "Fired", "b": "Fired"},
            [FakeAssessment("a", name="A", current_status="Not fired"),
             FakeAssessment("b", name="B", current_status="Not fired")],
            evidence_by_id={"a": "net bullish revision bias, zero downward"},
        )
        by = {e["indicator_id"]: e for e in log}
        assert by["a"]["description"] == "net bullish revision bias, zero downward"
        assert by["b"]["description"] == "Status changed from 'Fired' to 'Not fired'."

    def test_collapse_carries_freshest_non_generic_description(self):
        legs = [
            {"indicator_id": "x", "indicator_name": "X", "tier": 1,
             "old_status": "Not fired", "new_status": "Partially fired",
             "description": "first leg evidence", "fired_at_iso": "2026-07-27T08:00:00"},
            {"indicator_id": "x", "indicator_name": "X", "tier": 1,
             "old_status": "Partially fired", "new_status": "Fired",
             "description": "Status changed from 'Partially fired' to 'Fired'.",
             "fired_at_iso": "2026-07-27T10:00:00"},
        ]
        c = S.collapse_trigger_log_for_display(legs)[0]
        assert (c["old_status"], c["new_status"]) == ("Not fired", "Fired")
        assert c["description"] == "first leg evidence"

    def test_collapse_all_generic_synthesizes_net_phrase_and_roundtrip_drops(self):
        legs = [{"indicator_id": "y", "indicator_name": "Y", "tier": 1,
                 "old_status": "Not fired", "new_status": "Fired",
                 "description": "Status changed from 'Not fired' to 'Fired'.",
                 "fired_at_iso": "a"}]
        c = S.collapse_trigger_log_for_display(legs)[0]
        assert c["description"] == "Status changed from 'Not fired' to 'Fired'."
        rt = S.collapse_trigger_log_for_display(legs + [
            {"indicator_id": "y", "indicator_name": "Y", "tier": 1,
             "old_status": "Fired", "new_status": "Not fired",
             "description": "d", "fired_at_iso": "b"}])
        assert rt == []

    def test_fast_tier_sanitizes_evidence_before_logging(self):
        src = open("src/fast_tier.py").read()
        i = src.index("evidence_by_id")
        assert "_strip_internal_jargon" in src[i:i + 1500]


class TestBannerDestinationContract:
    def _trig(self, old, new, desc=None):
        return {"indicator_id": "i", "indicator_name": "Name", "tier": 2,
                "old_status": old, "new_status": new,
                "description": desc or f"Status changed from '{old}' to '{new}'."}

    def test_to_fired_is_red(self):
        h = T._build_generic_trigger_banner(self._trig("Not fired", "Fired"))
        assert "#FCEBEB" in h and "#B91C1C" in h and "🚨" in h

    def test_to_not_fired_is_green_both_origins(self):
        for old in ("Fired", "Partially fired"):
            h = T._build_generic_trigger_banner(self._trig(old, "Not fired"))
            assert "#EAF4E2" in h and "#2F6B2F" in h and "✅" in h, old
            assert "#FFF6E0" not in h, old

    def test_to_partial_is_amber_both_directions(self):
        up = T._build_generic_trigger_banner(self._trig("Not fired", "Partially fired"))
        down = T._build_generic_trigger_banner(self._trig("Fired", "Partially fired"))
        for h in (up, down):
            assert "#FFF6E0" in h and "#8A5A00" in h and "⚠️" in h
        assert "(PARTIAL)" in up and "SIGNAL DOWNGRADE" in down

    def test_unknown_direction_stays_red_alarm_no_question_marks(self):
        h = T._build_generic_trigger_banner({
            "indicator_id": "i", "indicator_name": "Name", "tier": 1,
            "description": "something without statuses"})
        assert "#FCEBEB" in h and "something without statuses" in h
        assert "Status: ?" not in h

    def test_enriched_description_renders_generic_suppressed(self):
        rich = T._build_generic_trigger_banner(self._trig(
            "Fired", "Not fired",
            desc="SK Hynix net bullish revision bias +41.25 over 30d."))
        assert "net bullish revision bias" in rich
        plain = T._build_generic_trigger_banner(self._trig("Fired", "Not fired"))
        assert "Status changed from" not in plain

    def test_every_variant_pill_is_white_on_explicit_dark_background(self):
        cases = [("Not fired", "Fired"), ("Not fired", "Partially fired"),
                 ("Fired", "Partially fired"), ("Fired", "Not fired"),
                 ("Partially fired", "Not fired")]
        for old, new in cases:
            h = T._build_generic_trigger_banner(self._trig(old, new))
            label_style = h.split("class='label'")[1].split(">")[0]
            assert "background:" in label_style and "color:#fff" in label_style, (old, new)
            assert "#7A5500" not in h, (old, new)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
