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

