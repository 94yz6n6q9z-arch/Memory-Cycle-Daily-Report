# v3.46.1 (2026-08-08) — event-anchored guidance ledger · TrendForce re-point · fetch hardening

Root cause (Aug-8 live defect): Layer 2 re-reported Amazon's Aug-7 call;
the duplicate guard compared raw period strings ("2026" != "CY2026"),
the echo superseded, prior_mid := 220 erased the $200B->$220B raise ->
stance accelerating->stable -> plateau 3/4 Fired instead of 2/4. The
v3.45.0 spec's own worked example already stated the correct answer.

- capex_ledger.py: period canonicalizer (2026==CY2026, FY27==FY2027);
  same-(company,period,event_date) updates are no-ops (same-event echo);
  older-than-stored prints rejected; EVENT ANCHORING — updates must land
  on a known earnings-call date (state/earnings_calendar_cache.json, ±1d)
  or carry an explicit revision verb + stated prior (flagged intra-quarter
  revision); coverage restating an old print is rejected by construction.
  LEDGER_UPDATE contract gains optional prior= / verb= / basis= tokens: a
  print carries its own baseline; verbs are tripwires (VERB-ARITHMETIC
  MISMATCH flag), ARITHMETIC DECIDES (Jul-31 rule); announced basis change
  + unchanged-verb + arithmetic-cut => stable with flag (MSFT ruling).
- Versioned ledger migrations applied on load (both legs, idempotent,
  ids persisted): 001 Amazon CY2026 prior 220->200 (raise restored);
  002 Microsoft stance_override=stable + basis_change (lease reclass —
  the $190B/$175B provenance trace is RESOLVED: CFO "investment
  expectations remain unchanged"). Expected verdict after deploy:
  2/4 Partially fired, score 0, Amazon accelerating.
- Spec worked example + v3.44/45 spec-consistency tests updated to the
  ruling; Layer 2 contract text gains the Amazon-echo and MSFT-basis
  named counterexamples and the new-event-only emission rule.
- TrendForce: keyword gate gains "memory price"/"price forecast" (the
  Jul-3 3Q26 forecast title matched ZERO old keywords — 130d stale
  anchor); one-time seeds for the two missed public press releases
  (20260703-13134, 20260709-13140) so the EXISTING extractor writes the
  3Q26 anchor row itself; new scrape_trendforce_presscenter() discovery
  source (collector.track pattern, parser_no_match observability).
- Fetch hardening: Google News 503 -> 2 retries with backoff+jitter
  (Aug-8 force-run threw 78x 503 under same-day burst); Benzinga/Cloudflare
  fetched via curl-cffi Chrome impersonation when available (dependency
  already in requirements since v3.4.6-era; graceful requests fallback).
- Tests: +13 (echo no-op, September-coverage rejection, next-call
  reiteration = legitimate plateau, stated-prior, verb tripwire, MSFT
  basis fixture, intra-quarter revision path, migration idempotency,
  Jul-3 title gate, presscenter parser, retry presence). Suite 1,375
  passed in sandbox; pre-existing failing set byte-identical.

