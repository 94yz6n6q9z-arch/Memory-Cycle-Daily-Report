"""v3.41.12 — deterministic ADR event detection.

Proven gap (Jul 10): the extraction log's only producer was the transcript-
only deep-tier digester, so every event-native ADR field was structurally
starved — pricing_day stayed None while the 424B4 sat in the feed and the
$26.5B/$149 pricing was in every headline. This detector is the
deterministic leg: EDGAR form types + pricing/size/demand headline regexes,
emitting entries in the exact shape adr_aggregator reads, same run.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analysis.adr_filing_detector import (
    detect_and_log_adr_events,
    _form_type_from_filing_title,
    _adr_signals_for_form,
)
from src.analysis.adr_aggregator import _apply_extractions, _determine_lifecycle_phase
from src.analysis.adr_event_tracking import cycle_phase_4_late_from_indicators


def _n(title, url="https://x", source="Reuters", published="2026-07-10T02:00:00Z", summary=""):
    return SimpleNamespace(title=title, url=url, source=source,
                           published=published, summary=summary)


def _state_skeleton():
    return {
        "sub_8a_f1_filing": {"current_state": {}, "evidence_log": []},
        "sub_8b_roadshow_demand": {"current_state": {}, "evidence_log": []},
        "sub_8c_pricing_day": {"current_state": {}, "evidence_log": []},
        "sub_8d_insider_sales_in_window": {"current_state": {"insider_sale_events": []}, "evidence_log": []},
    }


# ---- form-type parsing --------------------------------------------------------

def test_form_types_map_to_signals():
    assert _form_type_from_filing_title("[FILING] 424B4 - Prospectus [Rule 424(b)(4)]") == "424B4"
    assert _adr_signals_for_form("424B4", "2026-07-09T21:30:00Z") == {"pricing_day": "2026-07-09"}
    assert _adr_signals_for_form("F-1", "x") == {"f1_filed": True}
    assert _adr_signals_for_form("F-1/A", "x") == {"f1_filed": True}
    assert _adr_signals_for_form("RW", "x") == {"f1_canceled": True}
    assert _adr_signals_for_form("6-K", "x") is None
    assert _form_type_from_filing_title("SK Hynix raises $26.5B") is None  # not a filing


# ---- the Jul-10 scenario, end to end ------------------------------------------

def test_jul10_scenario_detects_and_advances_phase_to_trading(tmp_path):
    news = [
        _n("[FILING] 424B4 - Prospectus [Rule 424(b)(4)]", url="https://sec/424b4",
           source="SEC EDGAR", published="2026-07-09T21:30:00Z"),
        _n("SK Hynix raises US$26.5 billion in US offering after pricing ADRs at $149",
           summary="7x oversubscribed order book"),
    ]
    s = detect_and_log_adr_events(news, log_dir=tmp_path)
    assert s["edgar_entries"] == 1 and s["headline_entries"] == 1
    assert s["signals"]["pricing_day"] == "2026-07-09"  # 424B date, authoritative
    assert s["signals"]["final_size_usd_b"] == 26.5
    assert s["signals"]["demand_classification"] == "strong"

    entries = [json.loads(l) for p in tmp_path.iterdir() for l in p.read_text().splitlines()]
    state, _ = _apply_extractions(_state_skeleton(), entries)
    assert state["sub_8c_pricing_day"]["current_state"]["pricing_day"] == "2026-07-09"
    assert state["sub_8c_pricing_day"]["current_state"]["final_size_usd_b"] == 26.5
    assert state["sub_8b_roadshow_demand"]["current_state"]["demand_classification"] == "strong"
    assert _determine_lifecycle_phase(state) == "trading"


def test_priced_headline_sets_pricing_day_when_no_424b(tmp_path):
    news = [_n("SK Hynix prices US IPO at $149", published="2026-07-09T23:00:00Z")]
    s = detect_and_log_adr_events(news, log_dir=tmp_path)
    assert s["signals"]["pricing_day"] == "2026-07-09"


def test_rw_withdrawal_fires_cancellation(tmp_path):
    news = [_n("[FILING] RW - Registration Withdrawal Request", source="SEC EDGAR",
               published="2026-08-01T10:00:00Z")]
    s = detect_and_log_adr_events(news, log_dir=tmp_path)
    assert s["signals"] == {"f1_canceled": True}


# ---- guards: no ADR context, wrong subjects ------------------------------------

def test_capex_and_guidance_raises_do_not_trigger(tmp_path):
    news = [
        _n("SK Hynix raises $10 billion for HBM capacity expansion"),   # capex, no ADR words
        _n("Micron raises guidance on strong HBM demand"),               # guidance
        _n("[FILING] 6-K - Report of foreign private issuer", source="SEC EDGAR"),
    ]
    s = detect_and_log_adr_events(news, log_dir=tmp_path)
    assert s["edgar_entries"] == 0 and s["headline_entries"] == 0
    assert s["signals"] == {}


def test_weak_demand_classified(tmp_path):
    news = [_n("SK Hynix ADR offering sees weak demand from institutions")]
    s = detect_and_log_adr_events(news, log_dir=tmp_path)
    assert s["signals"]["demand_classification"] == "weak"


# ---- idempotency + robustness ---------------------------------------------------

def test_second_run_dedups(tmp_path):
    news = [_n("[FILING] 424B4 - Prospectus [Rule 424(b)(4)]", source="SEC EDGAR",
               published="2026-07-09T21:30:00Z")]
    s1 = detect_and_log_adr_events(news, log_dir=tmp_path)
    s2 = detect_and_log_adr_events(news, log_dir=tmp_path)
    assert s1["edgar_entries"] == 1
    assert s2["edgar_entries"] == 0 and s2["skipped_dedup"] == 1
    entries = [l for p in tmp_path.iterdir() for l in p.read_text().splitlines() if l.strip()]
    assert len(entries) == 1


def test_empty_and_none_news_are_noops(tmp_path):
    assert detect_and_log_adr_events([], log_dir=tmp_path)["signals"] == {}
    assert detect_and_log_adr_events(None, log_dir=tmp_path)["signals"] == {}


def test_entries_carry_aggregator_shape(tmp_path):
    detect_and_log_adr_events(
        [_n("[FILING] F-1 - Registration statement", source="SEC EDGAR",
            published="2026-06-12T10:51:11Z")], log_dir=tmp_path)
    entry = json.loads([l for p in tmp_path.iterdir() for l in p.read_text().splitlines()][0])
    # the exact keys adr_aggregator._apply_extractions / _iter read
    assert entry["adr_signals"] == {"f1_filed": True}
    assert entry["source"] == "sec_edgar"
    assert "date" in entry and entry["date"].endswith("Z")
    assert entry["detector"].startswith("adr_deterministic")


# ---- peak-coincidence chain ------------------------------------------------------

def test_t3_fired_makes_cycle_phase_4_late_true():
    a = SimpleNamespace(tier=3, current_status="Fired")
    assert cycle_phase_4_late_from_indicators([a]) is True
    b = SimpleNamespace(tier=1, current_status="Fired")
    assert cycle_phase_4_late_from_indicators([b]) is False


def test_escalation_condition_wired_in_snapshot():
    import inspect
    from src.analysis import adr_event_tracking as aet
    src = inspect.getsource(aet.compute_adr_snapshot)
    assert "PHASE_TRADING" in src and "cycle_phase_4_late" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
