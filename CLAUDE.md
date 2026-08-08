## v3.46.1 (2026-08-08) — ledger event-anchoring

- **Numbers must be tied to guidance EVENTS, not to when we read about
  them.** Coverage restating a known print is not information; guidance
  changes only at earnings calls or official revisions. Validate update
  dates against the earnings calendar; between events the stance is
  frozen. (Raul: "this is just a confirmation from what happened on last
  transcript, not any real new piece of info.")
- **Duplicate keys must be canonicalized before comparison.** "2026" vs
  "CY2026" defeated an idempotency guard and destroyed state via
  supersede-with-prior. Any guard comparing free-text labels needs a
  normalizer on BOTH sides.
- **A print should carry its own baseline.** "up from about $200 billion"
  states the prior; capture stated_prior instead of inheriting whatever
  the store held. Verbs corroborate as tripwires; arithmetic decides.
- **An accounting-basis change is not an economic signal.** MSFT
  $190B->$175B = finance->operating lease reclассification, investment
  unchanged — ruled stable + flag. Basis moves need their own field or
  every reclass scores as a cut.
- **State repairs ship as versioned migrations applied on load** — never
  manual state surgery; idempotent, id-tracked, loud in the log.
- **The spec already knew.** The v3.45.0 worked example stated the
  correct 2/4 answer while the applier bug produced 3/4 — spec-vs-output
  divergence is itself a defect signal worth a consistency test.

