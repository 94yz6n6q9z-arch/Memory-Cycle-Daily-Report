"""v3.46.1 — event-anchored guidance ledger + TrendForce/feed fixes.

Named counterexamples as executable spec:
  AMAZON ECHO (2026-08-08): Layer 2 re-reported the same Aug-7 call; a raw
    period-string mismatch ('2026' != 'CY2026') defeated the duplicate
    guard, supersede ran, prior_mid := 220 erased the $200B->$220B raise,
    plateau mis-fired 3/4. Same-event re-ingestion must be a no-op.
  SEPTEMBER COVERAGE ECHO: an article a month later saying 'Amazon has
    committed $220B' is not a new guidance event — no call that day ->
    rejected. Guidance changes only at guidance events.
  NEXT-CALL REITERATION: Amazon's NEXT call reiterating $220B IS a real
    event -> supersede correct, stance flips to stable. That is the
    plateau signal firing legitimately (MSFT $190B-on-$190B canon).
  MSFT LEASE RECLASS (Jul-29-26): 190->175 with 'expectations remain
    unchanged' + finance->operating lease shift = accounting basis change,
    ruled stable + flag — not an economic cut.
  JUL-3 TITLE (TrendForce): 'AI Server Demand Continues to Support Memory
    Prices in 3Q26...' matched ZERO keywords -> the 3Q26 forecast press
    release was never ingested and the contract anchor went 130d stale.
"""
import json
import sys
sys.path.insert(0, ".")

import pytest

from src.analysis.capex_ledger import (
    apply_ledger_updates, company_stance, evaluate_plateau, load_ledger,
    _apply_migrations, _norm_period, _SEED,
)
from src.fast_tier import _is_dram_relevant_v3397


CAL = {"Amazon": {"2026-08-07", "2026-11-05"},
       "Microsoft": {"2026-07-29"},
       "Alphabet": {"2026-07-22"},
       "Meta": {"2026-07-29"}}


def seeded():
    return {k: dict(v) for k, v in _SEED.items()}


# ------------------------- period normalization ---------------------------

def test_period_canonicalization():
    assert _norm_period("2026") == "CY2026"
    assert _norm_period("cy2026") == "CY2026"
    assert _norm_period("FY27") == "FY2027"
    assert _norm_period("FY2027") == "FY2027"


# --------------------------- the Amazon echo ------------------------------

def test_amazon_same_event_echo_is_noop():
    led = seeded()
    led["Amazon"].update({"mid": 220.0, "prior_mid": 200.0,
                          "event_date": "2026-08-07"})
    n = apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | 2026 | 220-220 | 2026-08-07 | echo coverage"],
        led, today="2026-08-08", calendar_dates=CAL)
    assert n == 0
    assert led["Amazon"]["prior_mid"] == 200.0          # raise NOT erased
    assert company_stance(led["Amazon"])[0] == "accelerating"


def test_september_coverage_echo_rejected():
    led = seeded()
    led["Amazon"].update({"mid": 220.0, "prior_mid": 200.0,
                          "event_date": "2026-08-07"})
    n = apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | CY2026 | 220-220 | 2026-09-14 | "
         "'Amazon has committed CapEx at 220B' article"],
        led, today="2026-09-15", calendar_dates=CAL)
    assert n == 0                                       # no call that day
    assert led["Amazon"]["event_date"] == "2026-08-07"
    assert led["Amazon"]["prior_mid"] == 200.0


def test_next_call_reiteration_is_legit_plateau():
    led = seeded()
    led["Amazon"].update({"mid": 220.0, "prior_mid": 200.0,
                          "event_date": "2026-08-07"})
    n = apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | CY2026 | 220-220 | 2026-11-05 | "
         "verb=reiterated | Q3 call"],
        led, today="2026-11-06", calendar_dates=CAL)
    assert n == 1
    assert led["Amazon"]["prior_mid"] == 220.0          # real supersede
    assert company_stance(led["Amazon"])[0] == "stable"  # plateau, correctly


def test_older_than_stored_rejected():
    led = seeded()
    led["Amazon"].update({"mid": 220.0, "prior_mid": 200.0,
                          "event_date": "2026-08-07"})
    n = apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | CY2026 | 200-200 | 2026-05-01 | stale"],
        led, today="2026-08-08", calendar_dates=CAL)
    assert n == 0 and led["Amazon"]["mid"] == 220.0


# ------------------- stated prior / verbs / basis change ------------------

def test_stated_prior_sets_baseline():
    led = {}
    apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | CY2026 | 220-220 | 2026-08-07 | "
         "prior=200 | verb=raised | call"],
        led, today="2026-08-08", calendar_dates=CAL)
    assert led["Amazon"]["prior_mid"] == 200.0
    assert company_stance(led["Amazon"])[0] == "accelerating"


def test_verb_arithmetic_mismatch_tripwire():
    led = seeded()
    led["Amazon"].update({"mid": 200.0, "prior_mid": 180.0,
                          "event_date": "2026-05-01"})
    apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | CY2026 | 200-200 | 2026-08-07 | "
         "verb=raised | call"],                       # arithmetic says stable
        led, today="2026-08-08", calendar_dates=CAL)
    assert any("VERB-ARITHMETIC MISMATCH" in f
               for f in led["Amazon"].get("flags", []))
    assert company_stance(led["Amazon"])[0] == "stable"   # numbers decide


def test_msft_basis_change_ruled_stable():
    led = {"Microsoft": {"period": "FY2027", "mid": 190.0,
                         "prior_mid": 185.0, "event_date": "2026-04-29"}}
    apply_ledger_updates(
        ["LEDGER_UPDATE: Microsoft | FY27 | 175-175 | 2026-07-29 | "
         "prior=190 | verb=unchanged | basis=changed | lease reclass"],
        led, today="2026-08-01",
        calendar_dates={"Microsoft": {"2026-07-29"}})
    e = led["Microsoft"]
    assert e["stance_override"] == "stable" and e["basis_change"]
    assert company_stance(e) == ("stable", 1)


def test_intra_quarter_revision_needs_verb_and_prior():
    led = seeded()
    led["Amazon"].update({"mid": 220.0, "prior_mid": 200.0,
                          "event_date": "2026-08-07"})
    # official revision off-calendar: verb+prior present -> applied, flagged
    n = apply_ledger_updates(
        ["LEDGER_UPDATE: Amazon | CY2026 | 240-240 | 2026-09-20 | "
         "prior=220 | verb=raised | official 8-K revision"],
        led, today="2026-09-21", calendar_dates=CAL)
    assert n == 1
    assert any("intra-quarter revision" in f for f in led["Amazon"]["flags"])


# ------------------------------ migrations --------------------------------

def test_migrations_fix_state_and_are_idempotent(tmp_path):
    p = tmp_path / "ledger.json"
    broken = seeded()
    broken["Amazon"].update({"prior_mid": 220.0})       # the live defect
    p.write_text(json.dumps(broken))
    led = load_ledger(p)
    assert led["Amazon"]["prior_mid"] == 200.0
    assert led["Microsoft"]["stance_override"] == "stable"
    assert set(led["_migrations"]) >= {"001_amazon_prior_200",
                                       "002_msft_basis_stable"}
    led2 = load_ledger(p)                               # second load: no re-apply
    assert led2["Amazon"]["prior_mid"] == 200.0
    v = evaluate_plateau(led2)
    assert v["status"] == "Partially fired" and v["count_at_or_beyond"] == 2


# --------------------------- TrendForce fixes -----------------------------

def test_jul3_forecast_title_now_passes_gate():
    t = ("AI Server Demand Continues to Support Memory Prices in 3Q26, but "
         "Gains Moderate as Consumer Demand Weakens")
    assert _is_dram_relevant_v3397(t)
    assert _is_dram_relevant_v3397("3Q26 Memory Price Forecast")
    assert not _is_dram_relevant_v3397("Companion robots evolve beyond elder care")


def test_presscenter_parser_extracts_release_links(monkeypatch):
    import src.data.news as news
    html = """<html><body>
      <a href='/presscenter/news/20260703-13134.html'>AI Server Demand Continues to Support Memory Prices in 3Q26, Says TrendForce</a>
      <a href='/presscenter/news/20260709-13140.html'>Server DRAM Contract Prices Expected to Rise 13-18% QoQ in 3Q26, Says TrendForce</a>
      <a href='/presscenter/news'>Press Center</a>
      <a href='/presscenter/news/20260703-13134.html'>AI Server Demand Continues to Support Memory Prices in 3Q26, Says TrendForce</a>
    </body></html>"""

    class R:
        text = html
        def raise_for_status(self): pass

    monkeypatch.setattr(news.requests, "get", lambda *a, **k: R())
    items = news.scrape_trendforce_presscenter()
    urls = [i.url for i in items]
    assert len(items) == 2                              # deduped, listing skipped
    assert "https://www.trendforce.com/presscenter/news/20260703-13134.html" in urls


def test_google_retry_helper_exists():
    from src.data.news import fetch_google_news
    import inspect
    body = inspect.getsource(fetch_google_news)
    assert "503" in body and "retry" in body.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
