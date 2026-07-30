# v3.42.1 — 2026-07-29 — HOTFIX: v3.42.0 crashed every run (imports never landed) + FMP stable migration

v3.42.0 deployed and the 11:52 UTC fast-tier run died at the Tape Circuit
Breaker call site with NameError: run_tape_circuit_breaker is not defined —
as does every subsequent trigger. No briefs ship until this lands.

## Root cause (mine, twice over)
The v3.42.0 build inserted the new imports by replacing a
`from src.analysis.prose_cache import` anchor that DOES NOT EXIST in
fast_tier.py — the replace silently no-op'd, and none of the printed
verification checks covered the import itself. The import smoke then passed
because `import src.fast_tier` succeeds regardless: the missing names
resolve only at runtime inside run_fast_tier(). Calls shipped, imports
didn't.

## Fixes
1. Imports added at module top (anchored on the real signal_reasoner import,
   ASSERTED). Runtime binding verified: src.fast_tier now exposes
   run_tape_circuit_breaker / evaluate_computed_verdicts /
   apply_computed_verdicts at module level.
2. Belt: the TCB call site is wrapped — a banner source must never kill the
   brief again. tcb_verdict=None degrades to "no banner", not "no email".
3. FMP migrated stable-first at BOTH call sites (estimates
   analyst-estimates + prices KRX quote fallback). The Jul-29 11:52 log
   proves the new annual-plan key still 403s the legacy /api/v3 family;
   FMP serves current plans from /stable. Legacy kept as fallback;
   [fmp-health] now logs which family answered. Stable field names
   normalized (epsAvg→estimatedEpsAvg, numAnalystsEps→
   numberAnalystEstimatedEps) so downstream parsing is family-agnostic.

## Confirmed working from the crash log (silver linings)
- S&P 500 rel-strength OR-path fired on its very first evaluation:
  SK Hynix 5d vs S&P 500 = -22.9pp — RED (the SOX-spread failure this
  replaced would have stayed quiet).
- [fmp-health] negative-path lines surfaced the 403s explicitly.

## Process lessons (CLAUDE.md)
- ASSERT every automated string replace; a printed "OK" that doesn't test
  the exact postcondition is theater.
- Import smokes must check RUNTIME name binding (hasattr on the module),
  not just module importability.

Tests: 26 in tests/test_v3_42_0_tcb_and_truth.py (3 new: module-level
binding, stable-first ordering at both call sites, belted call site).
Suite: 1369 passed / 39 failed — failing set byte-identical to baseline.

