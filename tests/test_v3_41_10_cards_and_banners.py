"""v3.41.10 — Section-4 card news feed, banner transition-gating, and
producer/consumer key fixes.

Three fixes, one release:

(1) BANNER GATE — the 🚨 RELATIVE STRENGTH RED FIRE banner rendered on raw
    signal STATUS (`overall_fired`), so it persisted at the top of every brief
    while t3 stayed red (observed: fired Fri Jul 3, still showing Mon Jul 6
    while the exec summary correctly said "No new trigger events today").
    Now gated on the 24h trigger log — the same clock as the NEW TRIGGER
    EVENT pill: shown on the transition day (and intraday reruns), gone the
    next day even if the signal stays fired. The EPS-revisions banner had the
    identical latent bug and gets the same gate.

(2) CARD NEWS PRODUCER — cards 7 (Competitor) and 8 (ADR) render a
    deterministic "Latest developments" from sub["recent_news"], a field
    nothing ever populated (consumer with no producer since v3.4.4) — both
    cards printed the quiet-day filler on every brief ever sent.
    attach_triage_news_to_snapshots now feeds it from the Haiku triage's
    indicator_relevance map, keyword-routed to subs with a card-level
    fallback.

(3) KEY FIXES — the cards' prose sub-lines read `id`/`summary` while the
    producers emit `sub_id`/`current_state_summary` (→ "?" ids and dangling
    ": ;" everywhere, real summaries discarded); the ADR milestones read
    `date`/`description` while the producer emits `date_window`/`event`
    (→ literal "?: ; ?: ; ?: ." while the yaml content was thrown away).
"""
from types import SimpleNamespace

import pytest

import src.email_module.template as T
from src.analysis.llm_summarizer import (
    NewsTriage,
    attach_triage_news_to_snapshots,
)


def _comp_snapshot():
    return {"composite_status": "Not fired", "subs": [
        {"sub_id": "7A", "display_name": "CXMT (Chinese commodity DRAM)",
         "status": "Not fired", "estimated_eps_impact_pct": 1.0,
         "current_state_summary": "DDR5 capacity ramping; no OEM mainstream win yet."},
        {"sub_id": "7B", "display_name": "Samsung HBM4/4E", "status": "Not fired",
         "estimated_eps_impact_pct": 2.0, "current_state_summary": "HBM4 qual ongoing."},
    ]}


def _adr_snapshot():
    return {"composite_status": "Partially fired", "subs": [
        {"sub_id": "8A", "display_name": "F-1 filing watch",
         "status": "Partially fired",
         "current_state_summary": "F-1 filed May 2026; amendments tracked."},
    ], "calendar": [
        {"date_window": "2026-Q3", "event": "Expected pricing window",
         "sub_indicator": "8C", "mechanical_impact": "x"},
        {"date_window": "2026-Q4", "event": "Lock-up expiry",
         "sub_indicator": "8D", "mechanical_impact": "y"},
    ]}


def _triage():
    return NewsTriage(
        relevant_news=[
            {"index": 3, "title": "CXMT validates DDR5-8000+ on AMD platforms",
             "url": "https://x/cxmt", "source": "S", "summary": "Chinese DDR5 progress"},
            {"index": 7, "title": "SK Hynix files F-1/A amendment for $28B listing",
             "url": "https://x/f1a", "source": "S", "summary": "ADR filing amendment"},
            {"index": 9, "title": "Generic memory competition intensifies",
             "url": "https://x/gen", "source": "S", "summary": "no clear sub"},
        ],
        broker_actions=[], trendforce_commentary=[],
        indicator_relevance={"t1_competitor_dynamics": [3, 9],
                             "t1_us_adr_event": [7]},
    )


_NEWS = [SimpleNamespace(url="https://x/cxmt", published="2026-07-06T08:00:00Z"),
         SimpleNamespace(url="https://x/f1a", published="2026-07-06T07:00:00Z")]


# ---- (2) the producer ---------------------------------------------------------

def test_routing_cxmt_to_7a_and_f1_to_8a_with_dates():
    comp, adr = _comp_snapshot(), _adr_snapshot()
    attach_triage_news_to_snapshots(_triage(), _NEWS, comp, adr)
    assert comp["subs"][0]["recent_news"] == [
        {"title": "CXMT validates DDR5-8000+ on AMD platforms",
         "url": "https://x/cxmt", "date": "2026-07-06"}]
    assert adr["subs"][0]["recent_news"][0]["title"].startswith("SK Hynix files F-1/A")
    assert adr["subs"][0]["recent_news"][0]["date"] == "2026-07-06"


def test_unrouted_item_lands_card_level():
    comp, adr = _comp_snapshot(), _adr_snapshot()
    attach_triage_news_to_snapshots(_triage(), _NEWS, comp, adr)
    assert [i["title"] for i in comp["recent_news"]] == ["Generic memory competition intensifies"]
    assert comp["recent_news"][0]["date"] == ""  # no matching NewsItem -> empty date


def test_caps_and_url_dedup():
    comp = _comp_snapshot()
    items = [{"index": i, "title": f"CXMT story {i}", "url": f"https://x/{i}",
              "summary": "cxmt"} for i in range(1, 6)]
    items.append({"index": 6, "title": "dup", "url": "https://x/1", "summary": "cxmt"})
    tri = NewsTriage(relevant_news=items, broker_actions=[], trendforce_commentary=[],
                     indicator_relevance={"t1_competitor_dynamics": [1, 2, 3, 4, 5, 6]})
    attach_triage_news_to_snapshots(tri, [], comp, None)
    assert len(comp["subs"][0]["recent_news"]) == 2  # per_sub_cap
    urls = [i["url"] for i in comp["subs"][0]["recent_news"]]
    assert "https://x/1" in urls and len(set(urls)) == len(urls)


def test_none_snapshots_and_empty_relevance_are_noops():
    attach_triage_news_to_snapshots(_triage(), _NEWS, None, None)  # no raise
    comp = _comp_snapshot()
    tri = NewsTriage(relevant_news=[], broker_actions=[], trendforce_commentary=[],
                     indicator_relevance={})
    attach_triage_news_to_snapshots(tri, [], comp, None)
    assert "recent_news" not in comp
    assert all("recent_news" not in s for s in comp["subs"])


# ---- (2)+(3) rendering --------------------------------------------------------

def test_card7_renders_news_summaries_and_no_placeholder_garbage():
    comp = _comp_snapshot()
    attach_triage_news_to_snapshots(_triage(), _NEWS, comp, _adr_snapshot())
    h = T._build_competitor_section4_card(comp)
    assert "CXMT validates DDR5-8000+" in h                      # 7A dev line
    assert "Generic memory competition intensifies" in h          # card-level line
    assert "No new developments today" not in h
    assert "impact): DDR5 capacity ramping" in h                  # real summary, colon kept
    assert "?</strong>" not in h and "impact): ;" not in h        # no "?" ids / dangling colon


def test_card8_renders_real_milestones_and_summary():
    adr = _adr_snapshot()
    attach_triage_news_to_snapshots(_triage(), _NEWS, _comp_snapshot(), adr)
    h = T._build_adr_section4_card(adr)
    assert "F-1/A amendment" in h
    assert "2026-Q3: Expected pricing window" in h and "2026-Q4: Lock-up expiry" in h
    assert "?:" not in h                                          # the old "?: ; ?: ; ?: ."
    assert "F-1 filed May 2026" in h                              # current_state_summary read


def test_quiet_day_keeps_filler_and_drops_dangling_colon():
    comp = {"composite_status": "Not fired", "subs": [
        {"sub_id": "7A", "display_name": "CXMT", "status": "Not fired",
         "estimated_eps_impact_pct": 1.0, "current_state_summary": ""}]}
    h = T._build_competitor_section4_card(comp)
    assert "No new developments today" in h
    assert "impact):" not in h  # empty summary -> no colon at all


def test_milestones_skip_entries_with_no_content():
    adr = {"composite_status": "Not fired", "subs": [],
           "calendar": [{"sub_indicator": "8C", "mechanical_impact": "x"}]}
    h = T._build_adr_section4_card(adr)
    assert "Upcoming milestones" not in h  # nothing renderable -> line omitted


# ---- (1) the banner gate ------------------------------------------------------

def test_gate_true_only_on_transition_day():
    es_day0 = SimpleNamespace(new_triggers_today=[
        {"indicator_id": "t3_relative_strength", "tier": 3}])
    es_day1 = SimpleNamespace(new_triggers_today=[])
    assert T._indicator_in_24h_trigger_log(es_day0, "t3_relative_strength") is True
    assert T._indicator_in_24h_trigger_log(es_day1, "t3_relative_strength") is False
    # other indicator transitioning does not open the t3 banner
    es_other = SimpleNamespace(new_triggers_today=[
        {"indicator_id": "t2_eps_revisions", "tier": 2}])
    assert T._indicator_in_24h_trigger_log(es_other, "t3_relative_strength") is False


def test_gate_is_none_safe():
    assert T._indicator_in_24h_trigger_log(
        SimpleNamespace(new_triggers_today=None), "t3_relative_strength") is False
    assert T._indicator_in_24h_trigger_log(object(), "t3_relative_strength") is False


def test_banner_call_sites_are_transition_gated():
    # Source-level assertion: both 0b and 0c consult the 24h-log gate. This is
    # the actual fix — the pure banner builders themselves are unchanged.
    import inspect
    src = inspect.getsource(T.build_email_html)
    assert '_indicator_in_24h_trigger_log(es, "t3_relative_strength")' in src
    assert '_indicator_in_24h_trigger_log(es, "t2_eps_revisions")' in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
