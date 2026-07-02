"""v3.41.9 — earnings coverage-echo guard.

The exec brief led with Micron's Q3 print for a full week (June 25 - July 1
briefs). Root cause: every day's feed carried fresh-DATED articles about the
same week-old event (analyst notes, recaps, CEO-interview write-ups), each
satisfying the v3.41.5 "fresh same-day news" exception by publish date. Sonnet
had no way to know the print was ONE event, already featured, N echoes deep —
the v3.5.0 event log (state/event_log.json) knows exactly that but fed only
t2_eps_revisions.

Fix: (1) surface the event log's recent earnings events to Sonnet as a
structured block (date, age, write-up count, FRESH/STALE tag, divergence
flag); (2) an EARNINGS ECHO prompt rule distinguishing NEW ATTRIBUTABLE FACTS
(company disclosure; a named analyst's PT/rating action with a value; a
verdict reversal) — which may lead on their day — from repackaging (recaps,
"had a great quarter" N days later) — which must not.
"""
from datetime import date, timedelta

import pytest

from src.config import EARNINGS_LEAD_FRESH_DAYS
from src.analysis.llm_summarizer import (
    build_sonnet_analysis_prompt,
    NewsTriage,
    _build_known_earnings_events_block,
)

TODAY = date(2026, 7, 1)


def _log(**overrides):
    base = {
        "MU:earnings_call:2026-Q3": {
            "event_date_iso": "2026-06-24", "n_observations": 42,
            "divergence_flagged": False,
        },
        "000660.KS:earnings_call:2026-Q2": {
            "event_date_iso": "2026-06-30", "n_observations": 5,
            "divergence_flagged": True,
        },
    }
    base.update(overrides)
    return base


# ---- the helper ---------------------------------------------------------------

def test_stale_vs_fresh_tagging():
    out = _build_known_earnings_events_block(_log(), today=TODAY, fresh_days=2)
    # MU: 7 days old -> STALE, do-not-lead instruction attached
    assert "MU earnings 2026-06-24 (2026-Q3) — 7 days ago, 42 write-ups logged. STALE" in out
    assert "do NOT lead" in out
    # SK Hynix: 1 day old -> FRESH, may lead
    assert "000660.KS earnings 2026-06-30 (2026-Q2) — 1 day ago, 5 write-ups logged. FRESH" in out
    assert "may lead" in out


def test_divergence_flag_surfaces_as_lead_eligible_reversal():
    out = _build_known_earnings_events_block(_log(), today=TODAY)
    assert "DIVERGENCE FLAGGED" in out
    assert "lead-eligible" in out
    # and it's attached to the SK line (the flagged one), which sorts first (newest)
    assert out.index("000660.KS") < out.index("DIVERGENCE FLAGGED") < out.index("MU earnings")


def test_boundary_age_equal_to_fresh_days_is_fresh():
    log = {"MU:earnings_call:2026-Q3": {
        "event_date_iso": (TODAY - timedelta(days=2)).isoformat(), "n_observations": 3}}
    out = _build_known_earnings_events_block(log, today=TODAY, fresh_days=2)
    assert "FRESH" in out and "STALE" not in out


def test_non_earnings_keys_ignored():
    log = {"MU:guidance_update:2026": {"event_date_iso": "2026-06-30", "n_observations": 2}}
    assert _build_known_earnings_events_block(log, today=TODAY) == ""


def test_older_than_lookback_excluded():
    log = {"005930.KS:earnings_call:2026-Q1": {
        "event_date_iso": (TODAY - timedelta(days=15)).isoformat(), "n_observations": 12}}
    assert _build_known_earnings_events_block(log, today=TODAY, lookback_days=14) == ""


def test_future_dated_and_unparsable_skipped():
    log = {
        "A:earnings_call:x": {"event_date_iso": (TODAY + timedelta(days=3)).isoformat()},
        "B:earnings_call:y": {"event_date_iso": "not-a-date"},
        "C:earnings_call:z": {"event_date_iso": None},
    }
    assert _build_known_earnings_events_block(log, today=TODAY) == ""


def test_empty_and_none_log_return_empty_string():
    assert _build_known_earnings_events_block({}, today=TODAY) == ""
    assert _build_known_earnings_events_block(None, today=TODAY) == ""


def test_newest_first_ordering():
    out = _build_known_earnings_events_block(_log(), today=TODAY)
    assert out.index("000660.KS earnings 2026-06-30") < out.index("MU earnings 2026-06-24")


# ---- config -------------------------------------------------------------------

def test_fresh_days_constant_sane():
    # day-of + at least one aftermath day; small enough that a week-old print is stale
    assert 1 <= EARNINGS_LEAD_FRESH_DAYS <= 4


# ---- prompt wiring ------------------------------------------------------------

def _prompt(event_log):
    tri = NewsTriage(relevant_news=[], broker_actions=[],
                     trendforce_commentary=[], indicator_relevance={})
    return build_sonnet_analysis_prompt(tri, [], {}, [], {}, event_log=event_log)


def test_prompt_carries_events_block():
    # dates relative to the real today so ages are deterministic in the prompt path
    real_today = date.today()
    log = {
        "MU:earnings_call:2026-Q3": {
            "event_date_iso": (real_today - timedelta(days=7)).isoformat(),
            "n_observations": 42, "divergence_flagged": False},
        "000660.KS:earnings_call:2026-Q2": {
            "event_date_iso": (real_today - timedelta(days=1)).isoformat(),
            "n_observations": 5, "divergence_flagged": False},
    }
    p = _prompt(log)
    assert "EARNINGS EVENTS ALREADY IN THE RECORD (coverage-echo guard" in p
    assert "7 days ago, 42 write-ups logged. STALE" in p
    assert "1 day ago, 5 write-ups logged. FRESH" in p


def test_prompt_block_absent_on_empty_log():
    p = _prompt({})
    # the RULE text references the block by name, so assert on the block HEADER
    assert "EARNINGS EVENTS ALREADY IN THE RECORD (coverage-echo guard" not in p


def test_prompt_carries_earnings_echo_rule():
    p = _prompt({})
    assert "EARNINGS ECHO — new information vs repackaging" in p
    # the three-tier substance: analyst action with a value LEADS ...
    assert "raises/cuts a price target" in p
    assert "Morgan Stanley raises Micron's price target to $200 from $160" in p
    # ... repackaging does not
    assert "Micron had a great quarter" in p
    assert "does not re-lead" in p


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
