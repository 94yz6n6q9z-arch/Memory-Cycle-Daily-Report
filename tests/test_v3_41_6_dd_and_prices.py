"""v3.41.6 — demand-destruction coverage/awareness + US price-staleness repair.

#1 downstream-demand coverage lane (config queries)
#2 un-freeze fired indicators on fresh evidence (prose_cache guard)
#3 broadened t1_demand_destruction trigger (config)
#4 forward transmission watch (Sonnet value-chain inference)
#5 US peer-relative price-staleness repair (prices), holiday-robust
"""
import types
from pathlib import Path

import pandas as pd

import src.config as config
from src.analysis.prose_cache import apply_news_freshness_guard, update_cache_after_synthesis
import src.data.prices as prices

_LLM_SRC = (
    Path(__file__).resolve().parent.parent
    / "src" / "analysis" / "llm_summarizer.py"
).read_text()


def _r(signal_id, evidence_count, fired_status="Fired"):
    return types.SimpleNamespace(
        signal_id=signal_id, evidence_count=evidence_count,
        fired_status=fired_status, current_status=fired_status,
    )


# ----------------------------------------------------------------- #1 coverage
def test_downstream_demand_queries_present():
    q = config.GOOGLE_NEWS_QUERIES
    joined = " || ".join(q).lower()
    assert "apple raises prices memory chip cost" in joined
    assert "counterpoint" in joined and "idc" in joined
    # estimate-revision transmission
    assert any("estimate" in x.lower() for x in q)


# ----------------------------------------------------------------- #3 DD trigger
def test_dd_trigger_has_price_passthrough():
    ind = next(i for i in config.INDICATORS if i.id == "t1_demand_destruction")
    wtl = ind.what_to_look_for.lower()
    assert "price" in wtl and ("offset" in wtl or "pass" in wtl or "raising" in wtl)
    # the inverted-content-reduction trap is called out
    assert "12gb" in wtl or "reversal" in wtl
    # leading patterns wired
    assert any("ramageddon" in p.lower() or "memory" in p.lower()
               for p in ind.trigger_patterns)


# ----------------------------------------------------------------- #2 un-freeze
def test_freshness_guard_flips_reuse_on_new_evidence():
    cache = {"t1_demand_destruction": {"evidence_count": 3}}
    decisions = {"t1_demand_destruction": {"action": "reuse", "reason": "dormant"}}
    reasonings = [_r("t1_demand_destruction", 5)]  # 5 > 3 → fresh
    apply_news_freshness_guard(decisions, cache, reasonings)
    assert decisions["t1_demand_destruction"]["action"] == "regen"
    assert "new_evidence" in decisions["t1_demand_destruction"]["reason"]


def test_freshness_guard_keeps_reuse_when_count_unchanged():
    cache = {"x": {"evidence_count": 4}}
    decisions = {"x": {"action": "reuse", "reason": "dormant"}}
    apply_news_freshness_guard(decisions, cache, [_r("x", 4)])
    assert decisions["x"]["action"] == "reuse"


def test_freshness_guard_keeps_reuse_on_decrease_or_no_baseline():
    # decrease (evidence expired) does not force a refresh
    cache = {"a": {"evidence_count": 6}, "b": {}}
    decisions = {
        "a": {"action": "reuse", "reason": "dormant"},
        "b": {"action": "reuse", "reason": "dormant"},  # no baseline
    }
    apply_news_freshness_guard(decisions, cache, [_r("a", 4), _r("b", 9)])
    assert decisions["a"]["action"] == "reuse"
    assert decisions["b"]["action"] == "reuse"  # no stored baseline → left alone


def test_freshness_guard_ignores_non_reuse():
    cache = {"x": {"evidence_count": 1}}
    decisions = {"x": {"action": "regen", "reason": "fingerprint_changed"}}
    apply_news_freshness_guard(decisions, cache, [_r("x", 99)])
    assert decisions["x"]["action"] == "regen"  # untouched


def test_update_cache_stores_evidence_count_baseline():
    cache = {}
    a = types.SimpleNamespace(
        id="t1_demand_destruction", current_status="Fired",
        current_status_pill=None,
    )
    # _PROSE_FIELDS may reference attrs not on the stub; getattr defaults None
    update_cache_after_synthesis(
        cache, [a], [_r("t1_demand_destruction", 7)],
        regenerated_ids={"t1_demand_destruction"},
    )
    assert cache["t1_demand_destruction"]["evidence_count"] == 7


# ----------------------------------------------------------------- #4 transmission
def test_value_chain_has_dd_to_peakcaller_transmission():
    assert "PEAK-CALLER TRANSMISSION" in _LLM_SRC
    assert "first sell-side dissent" in _LLM_SRC
    # capex framed as the LAST domino, not the next
    assert "capex is the last to turn" in _LLM_SRC or "lagging confirmation" in _LLM_SRC


# ----------------------------------------------------------------- #5 price repair
def _hist(cols):
    idx = pd.to_datetime(["2026-06-24", "2026-06-25", "2026-06-26"])  # Wed Thu Fri
    return pd.DataFrame(cols, index=idx)


def test_us_repair_refetches_only_the_stale_peer(monkeypatch):
    calls = []

    def fake_refetch(symbol, expected_ts, fmp_api_key=None):
        calls.append(symbol)
        return {"price": 55.0, "asof": "x", "source": "test"}

    monkeypatch.setattr(prices, "_refetch_us_ticker_latest", fake_refetch)

    # NVDA fresh through Fri; MU stale (Fri missing → last valid = Thu)
    hist = _hist({
        "NVIDIA": [100.0, 101.0, 102.0],
        "Micron": [50.0, 51.0, float("nan")],
    })
    tickers = {"NVIDIA": "NVDA", "Micron": "MU"}
    out = prices._repair_stale_us_tickers(hist, tickers)
    assert calls == ["MU"]  # only the laggard refetched
    fri = pd.Timestamp("2026-06-26")
    assert out.loc[fri, "Micron"] == 55.0
    assert out.loc[fri, "NVIDIA"] == 102.0  # untouched


def test_us_repair_no_false_flag_on_uniform_staleness(monkeypatch):
    """Holiday case: every US name shares the same latest bar (Fri closed) →
    nothing looks stale relative to peers → no refetch, no false alarm."""
    calls = []
    monkeypatch.setattr(
        prices, "_refetch_us_ticker_latest",
        lambda *a, **k: calls.append(a) or {"price": 1.0, "source": "t"},
    )
    hist = _hist({
        "NVIDIA": [100.0, 101.0, float("nan")],   # all last-valid = Thu
        "Micron": [50.0, 51.0, float("nan")],
    })
    prices._repair_stale_us_tickers(hist, {"NVIDIA": "NVDA", "Micron": "MU"})
    assert calls == []  # holiday-robust: no repair attempted


def test_us_repair_excludes_krx_and_indices(monkeypatch):
    calls = []
    monkeypatch.setattr(
        prices, "_refetch_us_ticker_latest",
        lambda symbol, *a, **k: calls.append(symbol) or {"price": 1.0, "source": "t"},
    )
    # KRX ticker stale relative to US baseline must NOT be touched by US repair
    hist = _hist({
        "NVIDIA": [100.0, 101.0, 102.0],            # Fri (US baseline)
        "SK Hynix": [200.0, 201.0, float("nan")],   # .KS — excluded
        "SOX": [10.0, 11.0, float("nan")],          # ^ index — excluded
    })
    tickers = {"NVIDIA": "NVDA", "SK Hynix": "000660.KS", "SOX": "^SOX"}
    prices._repair_stale_us_tickers(hist, tickers)
    assert calls == []  # neither KRX nor index pulled by the US guard
