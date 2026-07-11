"""v3.41.14 — spot-display coherence + peak-coincidence inputs + f1 date.

Three defects from the Jul 11 brief:
(1) One card, three DDR5 numbers: the matrix narrative printed the WEEKLY
    mean as "spot at $47.34" and the week-ENDING date as "peak set
    2026-07-12" (a Sunday — tomorrow), while its own chart annotated the raw
    print $47.80. Fix: the raw latest print leads with its real date; the
    weekly mean stays (it is the matrix's band input — math unchanged) but
    is labeled as the matrix basis, and the peak date is labeled as the
    week-ending it is.
(2) Phase reached `trading` on listing day but "No peak-coincidence
    escalation active": the gate received ONLY the DHCR snapshot (its own
    comment: "we don't have tier_assessments yet"), and the function's
    getattr-based reader silently ignored dict-shaped assessments anyway.
    Fix: prior-day indicator statuses (valid for a trailing-30d test, and
    available) are passed in; the reader accepts dicts and objects; the
    decision inputs are logged every run.
(3) 8A displayed "F-1 filed 2026-07-11T08:25:33Z" — the run's extraction
    timestamp — because every re-observation of the F-1 (the form sits in
    EDGAR's 30-day feed) overwrote f1_filing_date with entry["date"]. Fix:
    prefer published_date_iso and only ever move the date EARLIER.
"""
from datetime import date

import pytest

import src.data.cycle_matrix as cm
from src.analysis.adr_aggregator import _apply_extractions
from src.analysis.adr_event_tracking import cycle_phase_4_late_from_indicators


def _spot(**over):
    base = dict(sku="DDR5 16Gb 4800/5600", as_of_date=date(2026, 7, 11),
                current_session_avg=47.80, weekly_mean_current=47.34,
                rolling_peak_price=47.34, rolling_peak_date=date(2026, 7, 12),
                drawdown_pct=0.0, weeks_since_peak=0, wow_change_pct=1.2,
                weeks_observed=40, spot_column=1, spot_column_label="At peak")
    base.update(over)
    return cm.SpotState(**base)


def _contract():
    return cm.ContractState(
        has_anchor=False, anchor_quarter=None, anchor_low_pct=None,
        anchor_high_pct=None, anchor_central_pct=None, anchor_source=None,
        anchor_published_date=None, anchor_age_days=None,
        synthetic_central_pct=51.1, synthetic_low_pct=49.0,
        synthetic_high_pct=54.0, adjustments=[], sources_count_total=0,
        contract_row=1, contract_row_label=">=+30%")


# ---- (1) spot narrative ---------------------------------------------------------

def test_narrative_leads_with_raw_print_and_labels_weekly_basis():
    txt = cm._generate_narrative(cm.MatrixCell.HOLD, _spot(), _contract())
    assert "spot $47.80 (2026-07-11 print)" in txt
    assert "Matrix basis (weekly means): current wk $47.34" in txt
    assert "wk-peak $47.34 (wk ending 2026-07-12)" in txt
    # the misleading forms are gone
    assert "spot at $47.34" not in txt
    assert " set 2026-07-12" not in txt


def test_narrative_handles_missing_peak_date():
    txt = cm._generate_narrative(cm.MatrixCell.HOLD,
                                 _spot(rolling_peak_date=None), _contract())
    assert "wk ending" not in txt
    assert "spot $47.80 (2026-07-11 print)" in txt


# ---- (2) peak-coincidence inputs -------------------------------------------------

def test_dict_assessments_now_count():
    # prior_state assessments arrive as dicts; getattr-based reading ignored them
    six_t2 = [{"tier": 2, "current_status": "Fired"}] * 6
    assert cycle_phase_4_late_from_indicators(six_t2) is True
    assert cycle_phase_4_late_from_indicators(
        [{"tier": 3, "current_status": "Fired"}]) is True
    assert cycle_phase_4_late_from_indicators(
        [{"tier": 2, "current_status": "Fired"}] * 2) is False
    assert cycle_phase_4_late_from_indicators([]) is False


def test_object_assessments_still_work():
    from types import SimpleNamespace
    a = SimpleNamespace(tier=3, current_status="Fired")
    assert cycle_phase_4_late_from_indicators([a]) is True


def test_fast_tier_passes_prior_assessments():
    # source-level: the call site feeds tier_assessments from prior state and
    # logs the decision; the "we don't have tier_assessments" era is over.
    src = open("src/fast_tier.py").read()
    assert "tier_assessments=_prior_assessments" in src
    assert "[peak-coincidence]" in src


# ---- (3) f1 filing date -----------------------------------------------------------

def _skeleton():
    return {"sub_8a_f1_filing": {"current_state": {}, "evidence_log": []},
            "sub_8b_roadshow_demand": {"current_state": {}, "evidence_log": []},
            "sub_8c_pricing_day": {"current_state": {}, "evidence_log": []},
            "sub_8d_insider_sales_in_window": {"current_state": {"insider_sale_events": []}, "evidence_log": []}}


def test_f1_filing_date_prefers_published_and_earliest_wins():
    st, _ = _apply_extractions(_skeleton(), [
        {"date": "2026-06-12T10:51:11Z", "published_date_iso": "2026-06-12T10:51:11Z",
         "source": "sec_edgar", "adr_signals": {"f1_filed": True}},
        # a later re-observation (F-1/A in the 30d feed) with today's extraction stamp
        {"date": "2026-07-11T08:25:33Z", "published_date_iso": "2026-07-06T10:00:00Z",
         "source": "sec_edgar", "adr_signals": {"f1_filed": True}},
    ])
    assert st["sub_8a_f1_filing"]["current_state"]["f1_filing_date"] == "2026-06-12T10:51:11Z"


def test_f1_filing_date_can_move_earlier_when_older_filing_surfaces():
    st, _ = _apply_extractions(_skeleton(), [
        {"date": "2026-07-11T08:00:00Z", "published_date_iso": "2026-07-06T10:00:00Z",
         "source": "sec_edgar", "adr_signals": {"f1_filed": True}},
        {"date": "2026-07-11T08:00:01Z", "published_date_iso": "2026-06-12T10:51:11Z",
         "source": "sec_edgar", "adr_signals": {"f1_filed": True}},
    ])
    assert st["sub_8a_f1_filing"]["current_state"]["f1_filing_date"] == "2026-06-12T10:51:11Z"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
