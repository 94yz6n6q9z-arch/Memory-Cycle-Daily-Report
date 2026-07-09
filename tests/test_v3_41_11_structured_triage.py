"""v3.41.11 — structured-outputs triage, item-level routing, keyword floor.

Root cause (proven from the Jul 9 run log): the Haiku triage's free-form JSON
reply broke mid-output ("Unterminated string at char 28384" — the model
emitted a JSON-breaking character while copying news text). json_repair kept
what parsed BEFORE the break; `indicator_relevance`, the LAST field, was
dropped wholesale → the Section-4 card attach silently received an empty map
→ card 8 said "No new developments" while §5 led with the $28B listing.
Third occurrence of the class (chars 6839, 13926 in May; Sonnet char 4166 in
June).

Fixes:
  (1) STRUCTURED OUTPUTS — the triage call passes TRIAGE_JSON_SCHEMA via
      output_config.format: grammar-constrained decoding makes the malformed-
      character class impossible at the sampler level. Kill switch
      USE_STRUCTURED_TRIAGE + automatic in-run fallback to the legacy call.
  (2) ITEM-LEVEL ROUTING — each relevant_news item carries its own
      `indicators` array; the aggregate map is DERIVED (merged with the
      legacy map when present), so routing survives any partial output
      item-by-item and no critical field trails the response.
  (3) KEYWORD FLOOR — when the map yields nothing for a card, candidates are
      selected deterministically from relevant_news via narrow, card-specific
      vocabulary (bare "samsung"/"micron" deliberately excluded — router-only).
  (4) OBSERVABILITY — [card-news] logs every run including zeros; attach
      returns per-card stats; [triage] logs path, stop_reason, and map size.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.analysis.llm_summarizer as llm
from src.analysis.llm_summarizer import (
    TRIAGE_JSON_SCHEMA,
    NewsTriage,
    _merge_indicator_relevance,
    attach_triage_news_to_snapshots,
    build_haiku_triage_prompt,
)
from src.config import USE_STRUCTURED_TRIAGE


# ---- (1) the schema --------------------------------------------------------

def test_schema_is_constrained_and_carries_item_routing():
    assert TRIAGE_JSON_SCHEMA["additionalProperties"] is False
    item = TRIAGE_JSON_SCHEMA["properties"]["relevant_news"]["items"]
    assert item["additionalProperties"] is False
    assert "indicators" in item["properties"]
    assert "indicators" in item["required"]
    # no dynamic-key map in the enforced schema — routing travels per item
    assert "indicator_relevance" not in TRIAGE_JSON_SCHEMA["properties"]


def test_flag_defaults_on():
    assert USE_STRUCTURED_TRIAGE is True


# ---- (2) merge: map ∪ item tags --------------------------------------------

def test_merge_from_item_tags_only():
    parsed = {"relevant_news": [
        {"index": 7, "indicators": ["t1_us_adr_event"]},
        {"index": 3, "indicators": ["t1_competitor_dynamics", "t1_us_adr_event"]},
    ]}
    merged, n = _merge_indicator_relevance(parsed)
    assert merged == {"t1_us_adr_event": [7, 3], "t1_competitor_dynamics": [3]}
    assert n == 3


def test_merge_union_and_dedupe_with_legacy_map():
    parsed = {
        "indicator_relevance": {"t1_us_adr_event": [7]},
        "relevant_news": [{"index": 7, "indicators": ["t1_us_adr_event"]},
                          {"index": 9, "indicators": ["t1_us_adr_event"]}],
    }
    merged, n = _merge_indicator_relevance(parsed)
    assert merged == {"t1_us_adr_event": [7, 9]}
    assert n == 1  # only index 9 was new


def test_merge_filters_garbage():
    parsed = {
        "indicator_relevance": {"t1_x": [1, "two", None], 3: [4]},
        "relevant_news": [{"index": "7", "indicators": ["t1_x"]},
                          {"index": 5, "indicators": [None, ""]}],
    }
    merged, _ = _merge_indicator_relevance(parsed)
    assert merged == {"t1_x": [1]}


# ---- (1)+(2) the call: structured first, legacy fallback --------------------

def _fake_response(payload: dict, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))],
                           stop_reason=stop_reason)


STRUCTURED_PAYLOAD = {
    "relevant_news": [
        {"index": 7, "title": "SK Hynix $28B Nasdaq listing 7x oversubscribed",
         "url": "https://x/f1a", "source": "S", "summary": "ADR live",
         "indicators": ["t1_us_adr_event"]},
    ],
    "broker_actions": [], "trendforce_commentary": [],
}


def test_structured_call_sends_schema_and_derives_map(monkeypatch):
    fake = MagicMock()
    fake.messages.create.return_value = _fake_response(STRUCTURED_PAYLOAD)
    monkeypatch.setattr(llm, "Anthropic", lambda api_key: fake)
    monkeypatch.setattr(llm, "USE_STRUCTURED_TRIAGE", True)
    tri = llm.haiku_triage_news("key", [])
    kwargs = fake.messages.create.call_args.kwargs
    assert kwargs["extra_body"]["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["extra_body"]["output_config"]["format"]["schema"] is TRIAGE_JSON_SCHEMA
    assert tri.indicator_relevance == {"t1_us_adr_event": [7]}


def test_structured_failure_falls_back_to_legacy(monkeypatch):
    legacy_payload = {
        "indicator_relevance": {"t1_us_adr_event": [7]},
        "relevant_news": [{"index": 7, "title": "t", "url": "u",
                           "source": "s", "summary": "x"}],
        "broker_actions": [], "trendforce_commentary": [],
    }
    calls = []

    def side_effect(**kw):
        calls.append(kw)
        if "extra_body" in kw:
            raise RuntimeError("output_config not supported")
        return _fake_response(legacy_payload)

    fake = MagicMock()
    fake.messages.create.side_effect = side_effect
    monkeypatch.setattr(llm, "Anthropic", lambda api_key: fake)
    monkeypatch.setattr(llm, "USE_STRUCTURED_TRIAGE", True)
    tri = llm.haiku_triage_news("key", [])
    assert len(calls) == 2
    assert "extra_body" in calls[0] and "extra_body" not in calls[1]
    assert tri is not None
    assert tri.indicator_relevance == {"t1_us_adr_event": [7]}


def test_flag_off_goes_straight_to_legacy(monkeypatch):
    fake = MagicMock()
    fake.messages.create.return_value = _fake_response(
        {"relevant_news": [], "broker_actions": [], "trendforce_commentary": [],
         "indicator_relevance": {}})
    monkeypatch.setattr(llm, "Anthropic", lambda api_key: fake)
    monkeypatch.setattr(llm, "USE_STRUCTURED_TRIAGE", False)
    llm.haiku_triage_news("key", [])
    assert "extra_body" not in fake.messages.create.call_args.kwargs


# ---- legacy prompt hardening -------------------------------------------------

def test_legacy_prompt_map_first_and_items_carry_indicators():
    p = build_haiku_triage_prompt([])
    assert p.index('"indicator_relevance"') < p.index('"relevant_news"')
    assert '"indicators": ["t1_spot_decline"]' in p
    assert "tag EVERY relevant news item inline" in p


# ---- (3) keyword floor -------------------------------------------------------

def _floor_triage():
    return NewsTriage(relevant_news=[
        {"index": 1, "title": "SK Hynix $28B Nasdaq listing 7x oversubscribed, F-1/A filed July 6",
         "url": "https://x/f1a", "source": "S", "summary": "cross-listing at top"},
        {"index": 2, "title": "Micron beats earnings estimates",
         "url": "https://x/mu", "source": "S", "summary": "blowout quarter"},
        {"index": 3, "title": "CXMT validates DDR5-8000+ on AMD boards",
         "url": "https://x/cxmt", "source": "S", "summary": "progress"},
    ], broker_actions=[], trendforce_commentary=[], indicator_relevance={})


def test_keyword_floor_pins_the_jul9_listing_and_cxmt():
    comp = {"subs": [{"sub_id": "7A", "display_name": "CXMT", "status": "Not fired",
                      "estimated_eps_impact_pct": 1.0, "current_state_summary": "s"}]}
    adr = {"subs": [{"sub_id": "8A", "display_name": "F-1 watch",
                     "status": "Partially fired", "current_state_summary": "s"}]}
    stats = attach_triage_news_to_snapshots(_floor_triage(), [], comp, adr)
    assert stats["t1_us_adr_event"] == {"relevance": 0, "keyword": 1, "attached": 1}
    assert adr["subs"][0]["recent_news"][0]["title"].startswith("SK Hynix $28B")
    assert comp["subs"][0]["recent_news"][0]["title"].startswith("CXMT")


def test_bare_micron_never_selected_by_floor():
    comp = {"subs": [{"sub_id": "7C", "display_name": "Micron", "status": "Not fired",
                      "estimated_eps_impact_pct": 0.0, "current_state_summary": "s"}]}
    tri = NewsTriage(relevant_news=[
        {"index": 2, "title": "Micron beats earnings estimates", "url": "https://x/mu",
         "source": "S", "summary": "blowout quarter"}],
        broker_actions=[], trendforce_commentary=[], indicator_relevance={})
    stats = attach_triage_news_to_snapshots(tri, [], comp, None)
    assert stats["t1_competitor_dynamics"]["keyword"] == 0
    assert stats["t1_competitor_dynamics"]["attached"] == 0
    assert "recent_news" not in comp["subs"][0] and "recent_news" not in comp


def test_relevance_present_skips_floor():
    tri = _floor_triage()
    tri.indicator_relevance = {"t1_us_adr_event": [1]}
    adr = {"subs": [{"sub_id": "8A", "display_name": "F-1 watch",
                     "status": "Partially fired", "current_state_summary": "s"}]}
    stats = attach_triage_news_to_snapshots(tri, [], None, adr)
    assert stats["t1_us_adr_event"]["relevance"] == 1
    assert stats["t1_us_adr_event"]["keyword"] == 0
    assert stats["t1_us_adr_event"]["attached"] == 1


# ---- (4) observability: stats exist even for the zero/None case --------------

def test_stats_returned_for_none_snapshots():
    stats = attach_triage_news_to_snapshots(_floor_triage(), [], None, None)
    assert stats["t1_competitor_dynamics"]["attached"] == 0
    assert stats["t1_us_adr_event"]["attached"] == 0
    # candidates were still counted — the log line carries the breakdown
    assert stats["t1_us_adr_event"]["keyword"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
