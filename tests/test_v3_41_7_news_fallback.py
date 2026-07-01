"""v3.41.7 — Section-5 news robustness.

Root cause this fixes: Sonnet's JSON output broke mid-generation (an inline
markdown link in a prose field → unterminated string). json_repair salvaged
the fields BEFORE the break and dropped everything after — and key_news_summary
sat late in the object, so the news section rendered empty even though 54
relevant news items existed. The prose-cache masked the damage to the indicator
cards (they come from cache), leaving the empty news section as the only visible
symptom.

Three defenses:
  (1) _news_fallback_from_triage — when Sonnet's key_news_summary is empty, fall
      back to the Haiku-triaged relevant_news so Section 5 is never blank when
      relevant news exists.
  (2) prompt forbids inline URLs/markdown links in prose (the break cause).
  (3) key_news_summary emitted BEFORE indicator_assessments so a mid-card break
      can't drop it.
"""
from types import SimpleNamespace

import pytest

from src.analysis.llm_summarizer import (
    build_sonnet_analysis_prompt,
    NewsTriage,
    _news_fallback_from_triage,
)


def _triage(relevant_news):
    return NewsTriage(
        relevant_news=relevant_news,
        broker_actions=[],
        trendforce_commentary=[],
        indicator_relevance={},
    )


# ---- (1) the fallback helper -------------------------------------------------

def test_fallback_uses_triaged_items_with_summary_as_why():
    tri = _triage([
        {"index": 1, "title": "DRAM contracts +60%", "url": "https://a", "source": "DigiTimes", "summary": "Q3 DDR5 up sharply"},
        {"index": 2, "title": "Apple raises Mac prices", "url": "https://b", "source": "Bloomberg", "summary": "Memory squeeze passed on"},
    ])
    out = _news_fallback_from_triage(tri)
    assert [d["headline"] for d in out] == ["DRAM contracts +60%", "Apple raises Mac prices"]
    # Haiku's one-line takeaway becomes why_it_matters — faithful, not degraded
    assert out[0]["why_it_matters"] == "Q3 DDR5 up sharply"
    assert out[0]["url"] == "https://a"


def test_fallback_to_raw_news_items_when_triage_empty():
    # triage produced nothing relevant, but the raw feed has items
    raw = [SimpleNamespace(title="Raw headline", url="https://raw")]
    out = _news_fallback_from_triage(_triage([]), news_items=raw)
    assert out == [{"headline": "Raw headline", "url": "https://raw", "why_it_matters": ""}]


def test_fallback_empty_when_no_news_anywhere():
    assert _news_fallback_from_triage(_triage([]), news_items=[]) == []
    assert _news_fallback_from_triage(_triage([]), news_items=None) == []


def test_fallback_skips_items_missing_title():
    tri = _triage([
        {"index": 1, "title": "", "url": "https://a", "summary": "no title"},
        {"index": 2, "title": "Has title", "url": "https://b", "summary": "ok"},
    ])
    out = _news_fallback_from_triage(tri)
    assert [d["headline"] for d in out] == ["Has title"]


def test_fallback_missing_url_defaults_to_hash():
    out = _news_fallback_from_triage(_triage([{"title": "No URL", "summary": "x"}]))
    assert out[0]["url"] == "#"


def test_fallback_respects_top_n_cap():
    items = [{"title": f"H{i}", "url": f"https://{i}", "summary": "s"} for i in range(20)]
    out = _news_fallback_from_triage(_triage(items), top_n=8)
    assert len(out) == 8


def test_triage_preferred_over_raw_when_both_present():
    tri = _triage([{"title": "Triaged", "url": "https://t", "summary": "curated"}])
    raw = [SimpleNamespace(title="Raw", url="https://r")]
    out = _news_fallback_from_triage(tri, news_items=raw)
    assert len(out) == 1 and out[0]["headline"] == "Triaged"


# ---- (2) + (3) the prompt changes -------------------------------------------

def _prompt():
    tri = _triage([
        {"index": 1, "title": "t", "url": "https://a", "source": "S", "summary": "x"},
    ])
    return build_sonnet_analysis_prompt(tri, [], {}, [], {})


def test_key_news_summary_emitted_before_indicator_assessments():
    # (3) so a mid-card JSON break can't drop the news block
    p = _prompt()
    i_news = p.find('"key_news_summary"')
    i_cards = p.find('"indicator_assessments"')
    assert i_news >= 0 and i_cards >= 0
    assert i_news < i_cards


def test_prompt_forbids_inline_links_in_prose():
    # (2) the break cause: inline links were previously sanctioned
    p = _prompt()
    assert "OUTPUT HYGIENE — URLs and links" in p
    assert "Markdown links allowed" not in p


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
