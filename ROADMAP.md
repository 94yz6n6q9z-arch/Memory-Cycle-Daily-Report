# v3.4 ROADMAP — Captive-Ratio Indicators (deferred)

**Status**: deferred from the v3.2 + v3.3 build session. Version label is **v3.4** (the Quartr-replacement maintenance build is v3.31, kept deliberately in the .3x range so this keeps the v3.4 label). Target build date: ~4 weeks after v3.3 deploys (Sunday June 14, 2026 calendar reminder).

**Why deferred**: v3.4 introduces three new indicators that *fire alerts*. Calibrating their thresholds requires real extraction data to look at. v3.3's `state/extraction_log_*.jsonl` accumulates this data starting day one of deployment. After 3 weeks (~15 days of data on a weekday cadence), we'll have enough to set thresholds intelligently instead of guessing.

**Note on versioning**: A maintenance build **v3.31** shipped before this — replacing the gated Quartr API with free transcript sources (IR-site scrapers + Motley Fool). It was numbered v3.31 (a .3x maintenance fix, not a feature increment) specifically so these deferred captive-ratio indicators keep their original **v3.4** label. If you're reading old notes that called the Quartr work "v3.4" — that was a transient mislabel, corrected to v3.31.

**Anti-forgetting mechanisms**:
1. This file exists in the repo root.
2. A calendar reminder is set for the build date.
3. v3.3's extraction log is the data source — it accumulates passively, and the first time someone opens it after 3 weeks they'll see what v3.4 should consume.

---

## The three v3.4 indicators

### Indicator 23 — Inference Efficiency Shock (T1, leading)

**Hypothesis**: when ≥3 major inference efficiency advances ship in a 30-day window, the captive-ratio compression timeline accelerates.

**Trigger sources**:
- News items scored ≥7 by relevance filter that match concepts: MoE deployment, INT2/INT4 quantization, speculative decoding, KV cache compression, disaggregated serving
- vLLM / SGLang / TensorRT-LLM release notes for major version bumps
- DeepSeek / Meta / Mistral model releases claiming ≥2x inference cost reduction

**Trigger logic** (proposed, to calibrate):
- COUNT distinct major efficiency events in trailing 30 days
- threshold_warning: 3 events → EARLY WARNING
- threshold_fired: 5 events → FIRED (T1 status)

**Calibration data needed**: 3+ weeks of extraction logs to see actual event density. If we see >5 events in any given week routinely, raise thresholds. If <1 per month, lower them.

**Bearish for**: HBM-concentrated names (SK Hynix relatively most exposed). Bullish for: LPDDR-strong names (Samsung), CXL ecosystem (Astera), HBF wildcards.

---

### Indicator 24 — CXL 3.0 Production Deployment (T1, binary)

**Hypothesis**: the first production-scale CXL 3.0 memory pooling deployment at a hyperscaler is an inflection event for memory disaggregation. After it lands, capex shifts from in-server DDR/HBM toward pooled architectures.

**Trigger sources**:
- Hyperscaler earnings transcripts (extraction_log: `cxl_production_deployment: true`)
- Astera Labs IR / earnings (binary event)
- Samsung / SK Hynix CMM-D revenue disclosure
- News items mentioning specific CXL deployment names + production scale

**Trigger logic** (binary):
- Single confirmed first production deployment → FIRED
- No early warning — this is binary

**Calibration data needed**: probably none — binary events don't need threshold tuning. But validate by examining 3 weeks of false-positive risk (e.g. "CXL ecosystem advancing" vs "first production deployment").

**Bearish for**: in-server-DDR-heavy revenue. Bullish for: pure-play CXL (Astera), memory vendor LPDDR/CXL product lines.

---

### Indicator 25 — LPDDR Datacenter Ramp (T2, specific to SK Hynix relative-position risk)

**Hypothesis**: this is the indicator most specifically relevant to the user's SK Hynix concentration. If hyperscaler inference workloads shift LPDDR-heavy via custom silicon, SK Hynix loses relative share to Samsung (and to a lesser degree Micron).

**Trigger sources**:
- Extraction log: `custom_silicon_memory_arch: "LPDDR" | "mixed"` mentions across hyperscaler calls
- Extraction log: `lpddr_datacenter_design_win` mentions
- LPDDR pricing strength (currently the pipeline doesn't track LPDDR pricing separately — see "Open gap" below)
- News mentioning specific LPDDR-DC SKU launches

**Trigger logic** (proposed):
- COUNT distinct LPDDR datacenter mentions in trailing 60 days
- COUNT distinct hyperscaler earnings calls mentioning LPDDR custom silicon in trailing 90 days
- threshold_warning: 3 mentions across 2+ distinct hyperscalers → EARLY WARNING
- threshold_fired: 5 mentions across 3+ distinct hyperscalers OR explicit Samsung/Micron LPDDR-DC revenue disclosure → FIRED

**Calibration data needed**: 4+ weeks of extraction logs to see mention density. Adjust thresholds based on what the actual frequency looks like.

**Bearish for**: SK Hynix relative position. Bullish for: Samsung. Neutral for: Micron (mid-position).

---

## Open gaps to address in v3.4 or beyond

1. **LPDDR pricing data** — TrendForce publishes LPDDR contract reports but the pipeline currently only tracks DRAM + HBM. v3.4 should add an LPDDR-specific scrape or query. Without LPDDR pricing the LPDDR Datacenter Ramp indicator is missing its confirmation tier.

2. **HotChips / MICRO / ISCA proceedings** — currently caught via Google News queries but proceedings themselves aren't fetched. Architecture-leak signals are time-sensitive (window: ~24 hours after the talk). Worth scraping if architecture conferences become a critical source.

3. **Artificial Analysis / EpochAI tokens-$/cost benchmarks** — added as RSS in v3.2, but no structured extraction. v3.4 could add a parser that pulls the headline tokens/$ trends and tracks them as a derived metric.

---

## Build-day checklist (June 14, 2026)

When picking this up in a future session:

1. Open `state/extraction_log_*.jsonl` files from the past 3-4 weeks. Read 30-50 records. Get a feel for what's actually being extracted and at what frequency.
2. Tally:
   - How often does any `architecture_signals` field have a non-null value? (sparsity check)
   - How often does `cxl_production_deployment` fire? (should be very rare; if frequent → prompt is over-eager)
   - How often does `custom_silicon_memory_arch` get populated? Across how many distinct hyperscaler tickers? (LPDDR ramp prevalence check)
   - How often do `deployed_quantization` / `deployed_moe` / `deployed_speculative_decoding` get populated? (efficiency shock frequency)
3. Set thresholds for Indicators 23 and 25 based on the empirical distribution. Aim for: warning fires ~1-2x per month, fired fires 1-2x per year.
4. Implement the indicators as additions to `INDICATORS` list in `src/config.py`. Each gets a `trigger_logic_fn` reference (a new pattern — these indicators don't fit the trigger_patterns/trigger_numeric model since they aggregate over time).
5. Add a new function `evaluate_v34_indicators()` in `src/analysis/signal_reasoner.py` that reads the extraction log and computes the indicator states.
6. Wire into fast_tier between Layer 2 and Layer 3. The output of these indicators feeds Sonnet exec summary as additional context.
7. Update `DEPLOYMENT_GUIDE.md` "Where to make adjustments" section to mention the v3.4 indicator thresholds.
8. Update the email template to include the three new indicators in the appropriate sections.

---

## What to ask the user for at v3.4 build time

- Confirmation of threshold targets ("fire warning ~1-2x/month, fired 1-2x/year — agree?")
- Whether to include or exclude LPDDR pricing (gap #1 above) in this build
- Whether to wire v3.4 indicators into priority_alerter for real-time push, or keep them daily-only

This is intentionally light — at build time most decisions will already be informed by the extraction log data.

---

# v3.5 — Parking lot (opportunistic fixes)

**Status**: not a planned feature version — a catch-all for small/medium items
surfaced during v3.3–v3.4 work. Build opportunistically; some entries may get
picked off mid-stream rather than in a dedicated session. Sequenced **after**
v3.4 so the digester edits don't land mid-stream against v3.4's calibration
window.

## Entry 1 — Transcript memory + quarter-over-quarter diff

**Why**: the brief reacts to each earnings call in isolation. The signal that
matters is *change* — what a call says vs. what the last call said. The
pipeline has no transcript memory, so it can't see deltas.

**Closed spec** (decided in design discussion, May 2026):
1. Persist each transcript's digest per `(company, quarter)` — **plus the raw
   transcript text** (raw text means any later schema change is backfillable;
   keep the raw archive *outside* the git-committed `state/` for size/copyright
   reasons).
2. A delta pass: on each new call, compare against the stored prior, expressed
   in the **vocabulary of the indicators** (`signal_relevance` /
   `fired_status_evidence` / the named numeric `key_metrics` moving) — not a
   generic field-diff. There's a working precedent: `INVENTORY_WEEKS_TRAJECTORY_SEED`
   in `config.py` already does this for one metric (inventory weeks); v3.5
   generalises that pattern.
3. `key_quotes` carries **one quote per indicator the call was tagged against**,
   not one total — so a fired trigger can surface the exact quote it fired on.
4. Per-indicator **transcript-admissibility flag** (primary / contributing /
   context-only) on the `Indicator` dataclass — for the ~13 transcript-driven
   indicators a transcript is admissible evidence; for the price-feed /
   regulatory-filing indicators a transcript tag is context-only and capped at
   `neutral` `fired_status_evidence`. (Note: `t2_eps_revisions` should be
   added as transcript-admissible — the CFO's guidance language *is* the
   EPS-relevant event; the analyst note hours/days later is its derivative.
   But it feeds as a **parallel evidence stream**, not through the broker-
   authority scorer, which scores broker *actions*.)
5. **No shadow mode** — goes straight to live scoring, consistent with how
   every other data source was added. The safeguard instead: every
   transcript-fed trigger must surface its `key_quote` + `source_url` in the
   brief, so a fired trigger is auditable in 30 seconds. Bad extraction →
   user feedback → fix. The quote + link is the safety mechanism.

**Files**: `config.py` (admissibility flag), `digester.py` (the high-risk one —
`DIGEST_SYSTEM_PROMPT` + `Digest` schema for per-indicator quotes; re-validate
`test_architecture_extraction.py`), a new transcript-diff / quarter-store module,
`state.py` (per-quarter retrieval + raw archive path), `signal_reasoner.py`
(parallel EPS evidence stream), `template.py` (surface deltas + quote + link).

**The 18 `architecture_signals` fields are NOT this.** They are v3.4 feedstock —
they map to Indicators 23/24/25, not the 22-board. The transcript-diff runs on
the general digest (`key_metrics` / `signal_relevance` / `fired_status_evidence`),
a separate extraction layer.

## Entry 2 — Event deduplication

**Why**: one earnings event is observed repeatedly — the call, analyst notes
(D-0 to D+1), the Yahoo transcript (D+1 to D+2), Korean broker PDF echoes
(D+1 to D+3), news rehashes. That's potentially 8-10 ingested items for **one
event**. If `t2_eps_revisions` scores each arrival independently, a single
guidance cut compounds across coverage volume and the rolling sum fires on
echo, not evidence.

**Needs**: an **event-identity key** — probably `(ticker, event_type,
event_window)` — so observations match to one event; and a **corroboration-vs-
compounding rule**: first observation scores, later observations adjust
confidence (and can flag divergence — "CFO sounded fine, analysts cut anyway")
but **cannot re-fire**. Indicator 3's existing scoring dedups *broker actions*
but has no event identity that spans source types — that's the gap. This sits
*underneath* Indicator 3's scoring and the news filter, so it's more
architecturally central than a bolt-on.
