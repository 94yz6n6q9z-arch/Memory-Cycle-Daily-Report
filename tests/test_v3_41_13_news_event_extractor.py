"""v3.41.13 — the news_article extraction lane.

The extraction schema declared source_type="news_article" and the
aggregators were written to consume it, but no producer ever existed — the
deep-tier digester digests only transcripts/broker PDFs, so every
language-borne event field (ADR cancellation, syndicate, insider sales;
competitor announcements: BIS easing, HBM4E, AMD-Samsung MOU, JEDEC HBF,
Solidigm integration, CXMT OEM wins, HBF design wins) was structurally
starved. This lane is that producer: one structured-output Haiku pass per
fast run over the day's news, writing extraction-log entries in the exact
shape both aggregators read, before they run. Deterministic facts stay with
adr_filing_detector (v3.41.12); this lane covers arbitrary language.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.analysis.news_event_extractor as nee
from src.analysis.news_event_extractor import (
    NEWS_EVENT_SCHEMA,
    extract_and_log_news_events,
    _entries_from_extractions,
    _source_label,
)
from src.analysis.adr_aggregator import _apply_extractions
from src.config import USE_NEWS_EVENT_EXTRACTOR


def _news():
    return [
        SimpleNamespace(title="SK Hynix pulls US offering amid market turmoil",
                        url="https://r/pull", source="Reuters",
                        published="2026-08-01T08:00:00Z", summary="withdrawn"),
        SimpleNamespace(title="JEDEC ratifies High Bandwidth Flash specification",
                        url="https://r/jedec", source="Bloomberg",
                        published="2026-07-10T05:00:00Z", summary="HBF spec final"),
    ]


def _adr_group(**overrides):
    g = {"f1_canceled": None, "underwriter_syndicate": None, "roadshow_active": None,
         "demand_classification": None, "pricing_day": None, "pricing_discount_pct": None,
         "final_size_usd_b": None, "insider_sale_event": None}
    g.update(overrides)
    return g


def _comp_group(**overrides):
    g = {"us_export_controls_easing_event": None, "cxmt_oem_mainstream_win": None,
         "samsung_hbm4e_announcement": None, "amd_samsung_hbm4_supply_mou": None,
         "jedec_hbf_spec_ratified": None, "sk_hynix_solidigm_hbf_integration": None,
         "sandisk_kioxia_hbf_milestone": None, "hbf_hyperscaler_design_win": None,
         "hbf_hot_tier_displacement": None}
    g.update(overrides)
    return g


def _payload():
    return {"extractions": [
        {"news_index": 0, "adr_signals": _adr_group(f1_canceled=True),
         "competitor_signals": None},
        {"news_index": 1, "adr_signals": None,
         "competitor_signals": _comp_group(jedec_hbf_spec_ratified=True)},
    ]}


def _client(payload):
    fake = MagicMock()
    fake.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload))], stop_reason="end_turn")
    return fake


# ---- schema ---------------------------------------------------------------------

def test_schema_constrained_and_flag_on():
    assert USE_NEWS_EVENT_EXTRACTOR is True
    assert NEWS_EVENT_SCHEMA["additionalProperties"] is False
    item = NEWS_EVENT_SCHEMA["properties"]["extractions"]["items"]
    assert item["additionalProperties"] is False
    # v3.41.17 — capex_signals group added (first-party 2027-guidance lane)
    assert set(item["required"]) == {"news_index", "adr_signals",
                                     "competitor_signals", "capex_signals"}
    # nullable groups, fixed keys inside
    assert item["properties"]["adr_signals"]["additionalProperties"] is False
    assert "insider_sale_event" in item["properties"]["adr_signals"]["properties"]
    assert "jedec_hbf_spec_ratified" in item["properties"]["competitor_signals"]["properties"]


# ---- the call + conversion --------------------------------------------------------

def test_structured_call_and_entries(tmp_path):
    fake = _client(_payload())
    s = extract_and_log_news_events("key", _news(), log_dir=tmp_path, client=fake)
    assert s["ran"] is True and s["entries"] == 2
    kw = fake.messages.create.call_args.kwargs
    assert kw["extra_body"]["output_config"]["format"]["schema"] is NEWS_EVENT_SCHEMA
    entries = [json.loads(l) for p in tmp_path.iterdir() for l in p.read_text().splitlines()]
    adr = next(e for e in entries if "adr_signals" in e)
    comp = next(e for e in entries if "competitor_signals" in e)
    # nulls stripped, top-level group key, aggregator-required fields present
    assert adr["adr_signals"] == {"f1_canceled": True}
    assert adr["source"] == "reuters" and adr["source_type"] == "news_article"
    assert "date" in adr and adr["detector"].startswith("news_event_extractor")
    assert comp["competitor_signals"] == {"jedec_hbf_spec_ratified": True}
    assert comp["source"] == "bloomberg"


def test_adr_cancellation_round_trips_through_real_aggregator(tmp_path):
    extract_and_log_news_events("key", _news(), log_dir=tmp_path, client=_client(_payload()))
    entries = [json.loads(l) for p in tmp_path.iterdir() for l in p.read_text().splitlines()]
    state = {"sub_8a_f1_filing": {"current_state": {}, "evidence_log": []},
             "sub_8b_roadshow_demand": {"current_state": {}, "evidence_log": []},
             "sub_8c_pricing_day": {"current_state": {}, "evidence_log": []},
             "sub_8d_insider_sales_in_window": {"current_state": {"insider_sale_events": []}, "evidence_log": []}}
    state, _ = _apply_extractions(state, [e for e in entries if "adr_signals" in e])
    assert state["sub_8a_f1_filing"]["current_state"].get("f1_canceled") is True


def test_out_of_range_index_and_allnull_groups_skipped():
    payload = {"extractions": [
        {"news_index": 99, "adr_signals": _adr_group(f1_canceled=True), "competitor_signals": None},
        {"news_index": 1, "adr_signals": _adr_group(), "competitor_signals": _comp_group()},
    ]}
    entries = _entries_from_extractions(payload, _news(), "2026-07-10T10:00:00Z")
    assert entries == []


# ---- fail-quiet + dedup + flags ----------------------------------------------------

def test_api_failure_quiet_skips(tmp_path):
    bad = MagicMock(); bad.messages.create.side_effect = RuntimeError("rejected")
    s = extract_and_log_news_events("key", _news(), log_dir=tmp_path, client=bad)
    assert s["ran"] is False and s["reason"] == "api_error" and s["entries"] == 0
    assert not any(tmp_path.iterdir())


def test_dedup_on_rerun(tmp_path):
    fake = _client(_payload())
    extract_and_log_news_events("key", _news(), log_dir=tmp_path, client=fake)
    s2 = extract_and_log_news_events("key", _news(), log_dir=tmp_path, client=fake)
    assert s2["entries"] == 0 and s2["skipped_dedup"] == 2


def test_flag_off_and_missing_inputs_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(nee, "USE_NEWS_EVENT_EXTRACTOR", False)
    assert extract_and_log_news_events("key", _news(), log_dir=tmp_path)["reason"] == "disabled"
    monkeypatch.setattr(nee, "USE_NEWS_EVENT_EXTRACTOR", True)
    assert extract_and_log_news_events(None, _news(), log_dir=tmp_path)["reason"] == "no_api_key"
    assert extract_and_log_news_events("key", [], log_dir=tmp_path)["reason"] == "no_news"


def test_source_labels():
    assert _source_label("Reuters") == "reuters"
    assert _source_label("Korea Herald") == "korean_press"
    assert _source_label("The Elec") == "the elec"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
