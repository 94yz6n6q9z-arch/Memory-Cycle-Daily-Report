# v3.43.0 (2026-07-31) — TCB ladder fixes, S&P-only relative strength, CapEx plateau indicator

Response to the Jul-31 brief: the Tape Circuit Breaker read L1 at -52%, the
SOX-based relative-strength signal flapped mid-crash, and a CapEx plateau
rendered as a green-ish "downgrade".

## Tape Circuit Breaker (src/analysis/tape_circuit_breaker.py)
- FIX shock threshold inversion: `min(-8%, -3σ)` -> `max(-8%, -3σ)`. The
  shipped min() made the breaker HARDER exactly when volatility exploded —
  crash-era σ pushed -3σ past -10%, so Jul-29's -10.3% day didn't register.
  Masked in tests by a calm synthetic run-up (tiny σ made min() pick -8%).
- FIX level ladder: stop-out-depth drawdown (<= -30% vs 250d high) now
  escalates to L2 AND satisfies the L3 condition ON ITS OWN — no component
  committee required to declare the thesis stopped out. The Jul-31 shape
  (drawdown -52% + distribution, shock rolled out of window, trend dark)
  now deterministically reads L3 SELL. A -20% drawdown without stop-out
  depth still requires the committee (stays L1 alone).
- Trend component: NaN MA inputs and short frames now report status
  no_data with an explicit reason instead of a silent fired=False.
- OHLCV fetch: period fallback ladder 2y -> 1y -> 6mo (accept >= 200 rows)
  with a loud `[tcb] OHLCV frame: N rows (start -> end)` log line.
- Full component-input logging: every NOT-fired component states why
  (shock threshold vs worst return; trend close/MA50/MA20/falling/reason;
  distribution day count; stopout flag) so the next anomaly is diagnosable
  from the log alone.

## Relative strength rebuilt — SOX removed across the board (Raul, Jul-31)
- SOX (~40% memory) is self-referential and mean-reverts against memory
  names exactly during a memory unwind (fired Jul-6, un-fired mid-crash,
  re-fired Jul-31). `get_relative_strength_vs_sox` and
  `get_dram_etf_vs_smh` DELETED; generalized
  `get_relative_strength_vs_benchmark` preserves the v3.4.6.2 KRX/US
  calendar-alignment fix.
- New evaluator: S&P 500 ONLY. RED = 5d AND 20d spreads < -10pp for any
  name (fast entry, trend-confirmed); YELLOW = both < -5pp; STICKY
  RELEASE = once red, stays red until the worst 5d spread holds > -5pp
  for 5 CONSECUTIVE sessions (state/rel_strength_state.json; streak
  resets on relapse). Thresholds provisional pending backtest.
- New verdict keys: skhx/mu_{5d,20d}_vs_spx_pp + latch. Config indicator
  def, Sonnet COMPUTED METRICS block (with Overall fired/severity legacy
  lines, thresholds line, sub-trigger echo, LATCH line), §3 card (four
  chips: 5d+20d per name), §4 banner, and all prompt references rewritten
  to S&P 500. Where-we-stand and targets are spec-bound to the S&P spread;
  the spec says "never reference the SOX".

## NEW indicator: t3_capex_plateau (T3 now 6 signals; 26 total)
- Hyperscalers keep spending at enormous scale but STOP ACCELERATING —
  guidance flat vs prior baseline (±5%) or stabilization language. 1
  hyperscaler = Partially fired; >= 2 = Fired. Canonical: MSFT 2026-07-30
  FY27 $190B on a $190B baseline.
- HARD RULES: NOT CapEx cutting (guide-down >5% hands to Tier 4 and the
  plateau STAYS fired — progression, not reversal); >5% growth is still
  parabolic territory; contrarian-direction ("we are done building —
  thanks Nvidia, Micron, SK Hynix"), never good news; reversal only on
  re-acceleration >10% above baseline; fired-on-print persists.
- Routed into deep-tier digester example ids; Layer 2 picks it up
  generically from config.

## STAGE PROGRESSION render rule (template.py)
- A t2_capex_parabolic DOWNGRADE while t3_capex_plateau is Partial/Fired
  renders as a neutral slate/purple "STAGE PROGRESSION — CapEx boom signal
  superseded by plateau" banner — never green/amber "SIGNAL DOWNGRADE".
  De-escalation of a boom signal during late cycle is not good news.

## Tests
- NEW tests/test_v3_43_0_tcb_relstrength_plateau.py (17): TCB max-threshold
  under fat σ, Jul-31 production shape -> L3, stopout-alone -> L3, -20%
  needs committee, short-frame no_data, e2e L3+post_peak; rel-strength
  dual-horizon, sticky release, relapse reset, no-SOX contract; plateau
  config + T3 count; progression banner neutrality.
- Legacy suites updated to the new contract (v3_4_6_2 rewritten around the
  generalized benchmark helper; 39_6/39_7_6/39_7_9/39_7_10 keys, labels,
  and DRAM-ETF asserts retired; indicator-evidence bootstrap test now
  allows config ⊇ bootstrap for known-new indicators).
- Full suite: 1,384 passed; 39 failing set byte-identical to the v3.41.18
  baseline.

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

