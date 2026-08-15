# v3.47.0 (2026-08-12) — deterministic trough scenarios · reversal-gated downgrades

Two defects from the Aug-12 brief (Raul):
(1) SCENARIO SCRAMBLE — bear -18% vs base -40% vs bull -25% from-here at
-48% realized drawdown. Root cause: v3.41.17 re-anchored ONLY bear to the
peak band; base/bull passed Sonnet's static from-current anchors through
(authored for Phase-5 entry near the peak) — double-counting the realized
fall. Fix: PHASE5_TROUGH_ANCHORS_FROM_PEAK (bear -65 full historical
magnitude / base -57.5 band mid / bull -50 shallow edge, docx band);
ALL THREE cases re-anchor from the cycle peak every run, from-here return
capped at 0, ordering guaranteed; months-to-trough derived from each
case's own window midpoint (kills "trough in 0mo ... Q1-Q3 2027").
Sonnet keeps probabilities + windows; never authors the returns again.
Today's print: bear -32% / base -18% / bull -3%.
(2) DOWNGRADE-ON-INTENSIFICATION — t1_demand_destruction Fired ->
Partially fired while its own prose said evidence was "INTENSIFYING"
(Windows OEM -7% YoY, Intel low-DD client guide, AMD shipment cuts).
Recurrence of the exact v3.39.7.3 INVALID-DOWNGRADES case on the named
indicator — prompt guards are advisory. Fix: apply_reversal_gate()
(computed_verdicts) at BOTH legs beside the ratchet: for
t1_demand_destruction + t1_oem_inventory, Fired -> lower is rejected
unless the rationale carries a literal "REVERSAL: <condition met>"
token; rejection logged [status-guard] and Fired preserved. Reasoner
prompt gains the Aug-12 counterexample + the REVERSAL contract.
- Superseded expectations updated: v3.41.17 bear test (bear now -65).
- Tests: +7 (scramble fixed w/ today's numbers, months-from-window,
  trough-reached cap, no-op guards, gate reject/allow/scope). Suite
  1,382 passed; failing set byte-identical.

