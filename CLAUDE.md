## v3.45.0 lessons (2026-08-08)

- **Verify against code and history before stating.** Two claims this week
  (Yahoo routing scope; transcript-latency cause) were made from memory or
  a stale comment and falsified by Raul's own audit table. Production
  artifacts and the repo tree are the only admissible sources.
- **Taxonomy growth must trigger re-digestion.** Any once-and-frozen
  derived artifact (digests) is stale the moment the schema that shaped it
  (indicator set) changes. Stamp derived artifacts with the schema hash;
  invalidate on change.
- **When the LLM proposes rows and arithmetic decides, contract the
  handoff.** LEDGER_UPDATE lines: rigid grammar, validation, rejection
  logging — same pattern as every deterministic gate that has held.
- **Count bands and intensity scores are different organs.** Status from
  counts (auditable, flap-resistant); scores for severity display only.

## v3.44.0 lessons (2026-07-31)

- **Latched signals need a mechanical latch, not a spec sentence.** Prompt
  rules failed twice on capex signals in two days; 'once triggered it
  triggered' is now arithmetic (apply_status_ratchet at both legs), and the
  spec sentence exists only to explain the arithmetic.
- **A signal about a SURPRISE must be measured against prior guidance, not
  a level.** 'AT baseline' vs 'flat vs prior' was the escape hatch; the fix
  defines the comparator (own prior forward guidance) and states that
  confirming the number IS the event.
- **Verbal qualifiers must never override reiterated numbers.** Named as a
  hard rule with the exact escape reasoning quoted as forbidden.
- **De-escalation of one signal is often the FIRE of its successor.** The
  parabolic->plateau->cuts ladder encodes this; downgrades of the earlier
  stage are structurally impossible, progression is told by the successor.

## v3.43.0 lessons (2026-07-31)

- **min/max threshold inversion masked by calm test σ.** A volatility-scaled
  threshold combined with min() got HARDER exactly when vol exploded. The
  test suite used a calm synthetic run-up whose tiny σ made min() pick the
  absolute leg — masked in test, revealed in production. Any σ-scaled
  threshold test MUST include a crash-volatility regime where the σ leg
  dominates.
- **Stop-out depth must never require a committee.** Gating the highest
  breaker level behind a component count let a -52% drawdown sit at L1 when
  the shock rolled out of its lookback window and trend reported nothing.
  Any single condition that IS the definition of the terminal state must
  escalate alone.
- **Self-referential benchmarks flap by construction.** The SOX is ~40%
  memory; a memory-vs-SOX spread mean-reverts against memory during a
  memory unwind. Benchmark choice is an economic decision, not a
  convenience.
- **Anti-flap = asymmetric latch.** Fire fast (dual-horizon confirmation),
  release slow (N consecutive recovery sessions, streak resets on relapse),
  persist the latch. Same philosophy as TCB hysteresis; now applied to
  rel-strength.
- **Plateau ≠ improvement.** A boom-signal de-escalation caused by the next
  cycle stage must never render in improvement colors. Progression gets its
  own neutral banner keyed on the successor signal's status.
- **Silent no-data disables components.** Every not-fired component must
  log its inputs and reason; a short data frame must be loud (row count,
  date span), never a quiet fired=False.

## v3.42.1 lessons (2026-07-29)
- **Assert every automated replace.** The v3.42.0 import insertion no-op'd
  on a fictional anchor and every printed check passed anyway. Postcondition
  asserts or it didn't happen.
- **Binding, not importability.** `import module` proves nothing about names
  used inside functions; smoke `hasattr(module, name)` for every name a new
  call site depends on.
- **Belt every new call site** in the send path: degraded feature > dead run.

