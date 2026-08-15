"""v3.47.0 — deterministic trough scenarios + reversal-gated downgrades.

Counterexamples (Aug-12-2026 brief):
  SCRAMBLE: bear -18% / base -40% / bull -25% from-here at -48% realized
    drawdown — only bear was re-anchored to the peak band; base/bull were
    static from-current numbers double-counting the fall.
  0MO: "trough is 0 months away ... between Q1 2027 and Q3 2027".
  DOWNGRADE: t1_demand_destruction Fired -> Partially fired while the
    verdict's own prose said evidence was INTENSIFYING.
"""
import sys
sys.path.insert(0, ".")
from datetime import date
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from src.config import PHASE5_TROUGH_ANCHORS_FROM_PEAK
from src.fast_tier import _reanchor_bear_case
from src.analysis.computed_verdicts import apply_reversal_gate


def _case(r, ws="Q1 2027", we="Q3 2027", months=0.0):
    return NS(return_to_peak_pct=r, peak_months_away=months,
              peak_window_start=ws, peak_window_end=we, probability_pct=50)


def _analysis():
    es = NS(base_case=_case(-40.0), bull_case=_case(-25.0),
            bear_case=_case(-18.0))
    return NS(executive_summary=es)


def _ph(current=1_504_000, peak=2_919_000, n=260):
    idx = pd.date_range("2025-08-01", periods=n, freq="B")
    vals = [peak * 0.6] * (n - 2) + [peak, current]
    return pd.DataFrame({"SK Hynix": vals}, index=idx)


def test_aug12_scramble_fixed_ordering_and_values():
    a = _analysis()
    _reanchor_bear_case(a, _ph())
    es = a.executive_summary
    b, ba, bu = (es.bear_case.return_to_peak_pct,
                 es.base_case.return_to_peak_pct,
                 es.bull_case.return_to_peak_pct)
    assert b <= ba <= bu                       # ordering restored
    assert b == pytest.approx(-32.1, abs=0.5)  # -65 from peak
    assert ba == pytest.approx(-17.5, abs=0.5) # -57.5
    assert bu == pytest.approx(-3.0, abs=0.5)  # -50
    assert getattr(es, "scenario_anchor")["all_cases_reanchored"]


def test_months_derived_from_window_not_zero():
    a = _analysis()
    _reanchor_bear_case(a, _ph())
    m = a.executive_summary.base_case.peak_months_away
    # Q1-Q3 2027 midpoint ~ May-2027; from Aug-2026 that's ~7-10 months
    assert 5.0 <= m <= 12.0


def test_trough_reached_caps_at_zero():
    a = _analysis()
    _reanchor_bear_case(a, _ph(current=900_000))   # below all anchors
    es = a.executive_summary
    assert es.bull_case.return_to_peak_pct == 0.0
    assert es.base_case.return_to_peak_pct == 0.0


def test_short_or_missing_history_noop():
    a = _analysis()
    _reanchor_bear_case(a, None)
    assert a.executive_summary.base_case.return_to_peak_pct == -40.0


# ------------------------------- gate -------------------------------------

def _sig(status, note=""):
    return NS(signal_id="t1_demand_destruction", fired_status=status,
              evidence_for_change_vs_yesterday=note,
              where_we_stand="", latest_developments="",
              fired_status_evidence=[])


def test_gate_rejects_uncited_downgrade():
    s = _sig("Partially fired", "PC demand destruction INTENSIFYING ...")
    apply_reversal_gate([s], {"t1_demand_destruction": "Fired"},
                        status_attr="fired_status", id_attr="signal_id",
                        note_attr="evidence_for_change_vs_yesterday",
                        stage="pre-store")
    assert s.fired_status == "Fired"
    assert "status-guard" in s.evidence_for_change_vs_yesterday


def test_gate_allows_cited_reversal():
    s = _sig("Partially fired",
             "REVERSAL: IDC upgraded the 2026 PC forecast to +1.2%")
    apply_reversal_gate([s], {"t1_demand_destruction": "Fired"},
                        status_attr="fired_status", id_attr="signal_id",
                        note_attr="evidence_for_change_vs_yesterday",
                        stage="pre-store")
    assert s.fired_status == "Partially fired"


def test_gate_ignores_escalations_and_non_gated():
    up = _sig("Fired")
    apply_reversal_gate([up], {"t1_demand_destruction": "Partially fired"},
                        status_attr="fired_status", id_attr="signal_id",
                        note_attr="evidence_for_change_vs_yesterday",
                        stage="pre-store")
    assert up.fired_status == "Fired"
    other = NS(signal_id="t2_eps_revisions", fired_status="Not fired",
               evidence_for_change_vs_yesterday="")
    apply_reversal_gate([other], {"t2_eps_revisions": "Fired"},
                        status_attr="fired_status", id_attr="signal_id",
                        note_attr="evidence_for_change_vs_yesterday",
                        stage="pre-store")
    assert other.fired_status == "Not fired"   # not in gated set


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
