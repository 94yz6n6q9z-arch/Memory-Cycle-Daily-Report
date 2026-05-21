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

---

# v3.39.7 follow-ups — Cycle matrix Parts 5 and 6

**Status**: queued post-v3.39.7 (matrix indicator shipped in Parts 1-4). Build sequenced AHEAD of v3.4 captive-ratio indicators because both have data-accumulation needs that benefit from running passively for 2-3 weeks before threshold tuning.

**Why these are Parts 5 and 6, not v3.40**: the cycle matrix is one indicator (`t1_spot_decline`). Versioning it as v3.40 implies a feature-version step which it isn't — these are remaining pieces of the same architectural change. Numbering them as Parts (not minor versions) keeps the v3.4 captive-ratio label intact, consistent with the rest of the v3.3x sequence.

---

## Part 5 — Layer 2 integration + GHA history accumulation

**Two pieces sequenced together** because the Layer 2 integration needs accumulated history to be informative.

### 5a — Wire matrix verdict into Layer 2 signal_reasoner — ✅ DONE in v3.39.7.1

**Why**: in v3.39.7 the matrix verdict lives only in the email card + the deterministic indicator status override (`_override_t1_spot_decline_from_matrix`). It never reaches `signal_reasoner.py:reason_all_signals()` which produces the Layer 2 per-signal verdicts that Sonnet now receives (closed in v3.39.6). Result: Sonnet's exec summary references the OTHER 21 signals' Layer 2 verdicts but only references t1_spot_decline through the indicator badge — not through structured signal_reasoning. The exec summary cannot say "cycle matrix in Hold at R1/C2 with synthetic 54.6% QoQ" without the LLM re-deriving those numbers from prose.

**Closed spec**:

1. Add a new SignalReasoning entry produced upstream of Layer 2 (not in Layer 2 itself — Layer 2 is per-signal Haiku, and the matrix verdict is deterministic, so it's parallel to the rel_strength_full case which v3.39.6 already does this way).
2. In `fast_tier.py` after `_run_cycle_matrix_step` returns the verdict, construct a `SignalReasoning(signal_id="t1_spot_decline", verdict=...)` and APPEND or REPLACE the entry in `signal_reasonings` list.
3. The structured fields written are the matrix verdict deconstructed: `cell` (HOLD/WARN/FIRE string), `cell_label`, `contract_row`, `spot_col`, `synthetic_central_pct`, `anchor_quarter`, `anchor_age_days`, `drawdown_pct`, `weeks_since_peak`, and the three adjustment values (`tone_adj_pp`, `supplier_adj_pp`, `spot_lead_adj_pp`).
4. Sonnet's prompt already iterates over `signal_reasonings`; no prompt change needed — the v3.39.6 LAYER 2 PER-SIGNAL VERDICTS block automatically picks up the new entry. Sonnet's exec summary can then reference the matrix structured fields directly without re-derivation.

**Verification**: re-render the May 17/18 May briefs locally with the matrix verdict piped in. Confirm Sonnet's exec summary references matrix numbers verbatim (not paraphrased), and that t1_spot_decline appears in the "LAYER 2 PER-SIGNAL VERDICTS" block in the Sonnet prompt.

**Files**: `signal_reasoner.py` (new dataclass init path, no Layer 2 Haiku change), `fast_tier.py` (post-matrix append step), tests in `test_v3_39_6_calculation_driven.py` extended with a t1_spot_decline parity case.

### 5b — GHA fast_tier.yml commit step for data/*.csv — ✅ SHIPPED in v3.39.7

**Why**: without this, the 63 seeded rows in `data/dram_spot_history.csv` are the ONLY history the matrix ever sees in production. GHA runners are ephemeral — every scrape result is computed, written to the runner filesystem, then destroyed when the runner shuts down. Days pile up with the matrix still evaluating against the May 19 2026 snapshot.

**Closed spec**:

1. Edit `.github/workflows/fast_tier.yml` to set `permissions: contents: write` (was `read`).
2. Append a step at the END of the fast_tier job: `git add data/dram_spot_history.csv data/dram_contract_actuals.csv data/dram_contract_quarterly_forecasts.csv data/dram_contract_signals.csv` (with `2>/dev/null || true`), then commit-if-staged, then `git pull --rebase origin main` + push with one retry (v3.34 pattern).
3. Verify the runner has the required token (`secrets.GITHUB_TOKEN` is the default and has write access when `contents: write` is set).
4. The `.github_workflow_patch.yml` file in the v3.39.7 zip is the canonical text for this step.

**Risk**: if two runs interleave (primary 11:00 + fallback 11:30) the second push may race the first. The v3.34 pattern (`git pull --rebase` before push, with one retry) handles this. Same pattern is already in `deep_tier.yml`.

**Files**: `.github/workflows/fast_tier.yml` only.

### 5 — Combined acceptance criteria

- After 5 consecutive weekday GHA runs, `data/dram_spot_history.csv` has grown by 5-7 rows per day (one per SKU per day, modulo TrendForce updating slower than daily).
- Sonnet exec summary's t1_spot_decline mention contains the matrix structured fields verbatim (cell, drawdown_pct, synthetic_central_pct).
- The cycle matrix card in the email and the Sonnet-generated text agree on numbers; no paraphrase drift.
- No regression in other 21 indicators or in the v3.39.6 Layer 2 + computed_metrics flow.


## Part 6 — Backtest harness against 2017-18 and 2022 cycle peaks

**Why**: the matrix weights `{"tone": 0.4, "supplier": 0.4, "spot_lead": 0.2}` and the cell semantics (R1-R5/C1-C5 thresholds) were specified ex ante in Part 1. They have not been validated against historical cycle turns. The framework's underlying logic — that spot drawdown leads contract direction by 6-10 weeks — is empirically grounded (2018 cycle spot peaked ~2 months before contract turned; 2022 cycle DDR4 spot rolled ~6 months ahead per the audit xlsx) but the specific weight choices and band cutoffs are not.

**Closed spec**:

1. Assemble a backtest dataset covering 2017Q1-2018Q4 and 2021Q2-2023Q1 (two cycle tops + the troughs around them):
   - Historical DDR4 16Gb 2Gx8 1600/2133 spot session_avg, weekly resolution (DDRexchange/DRAMeXchange archive, or paid TrendForce snapshot if free fails).
   - Historical TrendForce quarterly contract forecasts (need to scrape Press Center archive; 2017-18 may require paywalled access — surface this as a question to the user at build time).
   - Historical SK Hynix close price (yfinance — straightforward).
2. Implement `scripts/backtest_cycle_matrix.py` that walks the backtest dates week-by-week, calling `evaluate_matrix()` with `as_of=<historical-date>`. Emits a CSV with `date, cell, contract_row, spot_col, synthetic_central_pct, drawdown_pct, sk_hynix_close, sk_hynix_drawdown_from_peak_pct`.
3. Plot the backtest: SK Hynix close (left Y) + matrix cell color (background) over time. Goal is to visualize whether the matrix flipped to WARN/FIRE in the 4-8 weeks before SK Hynix made its all-time high in each cycle.
4. Compute summary statistics:
   - Did matrix reach WARN before SK Hynix's cycle peak? How many weeks ahead?
   - Did matrix reach FIRE before SK Hynix's cycle peak? How many weeks ahead?
   - Any false positives (FIRE that wasn't followed by an SK Hynix decline within N weeks)?
   - Any false negatives (SK Hynix decline of >X% with matrix still in HOLD)?
5. If false positive/negative counts are unacceptable, tune the weights. Specifically: if matrix lags (false negative), increase `spot_lead` weight (currently 0.2); if it whipsaws (false positive), tighten the spot drawdown bands (e.g., C2 boundary from -5% to -3%).

**Acceptance criteria**:

- Matrix reaches WARN at least 4 weeks before the cycle peak in both 2018 and 2022 backtests.
- Matrix reaches FIRE at most 2 weeks after the cycle peak in both backtests (otherwise the signal is too late).
- False positive count: at most 1 FIRE in a non-peak window across both backtests.
- If acceptance criteria fail, document the tuning iteration in CHANGES.md and re-run.

**Data sourcing risk**: historical TrendForce quarterly forecasts may be paywalled in archive. Fallback: use Korean broker PDFs (Mirae archive goes back to 2018) or manually transcribe from the brief's existing source citations in audit xlsx Tier 1. Surface to user at build time.

**Files**: `scripts/backtest_cycle_matrix.py` (new), `tests/test_cycle_matrix.py` extended with backtest-pin cases for the 2018 and 2022 peak dates, `CHANGES.md` (record any tuning), `CLAUDE.md` (update locked decisions if weights changed).

---

## What to ask the user before starting Parts 5 and 6

- Part 5a: confirm "OK to override the LLM's t1_spot_decline assessment in the signal_reasonings list, not just in the email indicator card" (this is the design choice that closes the Sonnet drift risk)
- Part 5b: confirm GHA token has write access to the repo's main branch from the fast_tier workflow (default is yes; user may have repo settings that disable it)
- Part 6: confirm historical data sourcing strategy — paid TrendForce archive vs Korean broker PDF reconstruction vs manual transcription. Decision affects time-to-build by 2-4 hours.
- Sequencing: build Part 5 first (1-2 days, modest scope) and run for 2-3 weeks to accumulate real history before Part 6 backtest. Build Part 6 in parallel if a backtest dataset can be assembled without waiting on production accumulation.

---

## What's explicitly NOT in Parts 5 and 6 (candidates for Part 7+)

- **HBM contract modeling**: open gap. SK Hynix is your primary position and HBM is its highest-margin product line; the cycle matrix currently models commodity DRAM only. Adding HBM would mean an HBM-specific anchor source (HBM forecast disclosures are gated; SK Hynix earnings commentary is the most reliable free source), an HBM-specific spot column proxy (spot prices for HBM don't exist meaningfully — HBM is contract-only), and likely a separate matrix grid (HBM cycle dynamics differ from commodity DRAM). Big enough to be its own version (v3.40 HBM cycle matrix), not a Part 7.
- **LPDDR pricing integration**: gap #1 in the existing v3.4 roadmap above. Would inform Indicator 25 (LPDDR Datacenter Ramp) more than t1_spot_decline directly. Keep sequenced with v3.4.
- **Real-time priority_alerter integration**: matrix verdict could feed `priority_alerter.py` for an off-cycle email when the cell flips Hold→Warn or Warn→Fire mid-day. Defer until Part 5 verifies the verdict is stable across runs.
---

# v3.39.7.3 follow-ups — Persistent evidence store

**Status**: v3.39.7.3 shipped May 20, 2026 with the persistent indicator evidence store (`state/indicator_evidence.json`) bootstrapped from the May 9 framework doc. Five persistence categories (permanent / quarterly / durable_contract / live / event_window) handle different memory characteristics per indicator. The follow-ups below are scoped post-deploy after 2-3 weeks of accumulated state.

**Why these are follow-ups, not v3.40**: same logic as Parts 5/6 above — same architectural change, completion in stages. Keeps the v3.4 captive-ratio label intact.

---

## Part 7a — Tune persistence categories from real data

**Why**: the bootstrap categorization assigned each indicator's persistence based on a priori judgment about how its underlying data behaves (capex = quarterly, TurboQuant = permanent, P/E = live, LTAs = durable_contract). After 4-8 weeks of GHA runs, we'll have a track record of how often each indicator's evidence is genuinely refreshed vs preserved-by-default. Some categorizations may need correction.

**Data source**: `state/indicator_evidence.json` evolves across GHA commits. Look at the diff between consecutive runs:
- How often does Layer 2 PRESERVE status because today's evidence didn't match reversal_condition? (expected: most days)
- How often does Layer 2 REVERSE a fired status? (expected: rare — once every few months per indicator)
- For each indicator: was the reversal triggered by a genuine data print, or did the LLM misjudge?

**Possible adjustments**:
- Indicators currently `quarterly` that prove to refresh more frequently (e.g., TrendForce weekly prints) — re-categorize to `live` or add an explicit `next_check_due` date.
- Indicators currently `permanent` with aging logic (e.g., insider sales 12mo window) — verify the 12mo window is right empirically; some sales should age out faster, some slower.
- Indicators currently `durable_contract` that need expiry-date awareness (LTAs do expire — HBM3E 2026 sold-out expires end-2026).

**Acceptance**: after 4-8 weeks, audit `state/indicator_evidence.json` diff history. If any indicator's category is producing wrong-direction errors (preserving when it should reverse, or vice versa), update its bootstrap entry. Schema is stable; only the data changes.

**Files**: `state/indicator_evidence.json` (data updates only); audit script could be added to `scripts/` to summarize evidence store evolution.

---

## Part 7b — Schema migrations + versioning

**Why**: the bootstrap was written with `_metadata.schema_version: "1.0"`. Future schema changes (new fields, new persistence categories, structural reorganization) need a migration path.

**Likely v1.1 additions**:
- `aging_window_days: int` field per evidence entry — make insider sales 12mo and event_window 35d explicit per-entry instead of hardcoded in `active_evidence()`.
- `confidence: "high" | "medium" | "low"` per evidence entry — record how strong the evidence was when first logged.
- `expiry_date: ISO date` for durable_contract entries — LTAs do have expiries.
- `transition_log: list[dict]` — separate from evidence_log, just the status transitions for clean audit.

**Acceptance**: `load_indicator_evidence_store()` recognizes `schema_version` and applies migrations idempotently. Bootstrap stays v1.0; new fields are additive.

**Files**: `src/state.py` (migration logic in load function), `state/indicator_evidence.json` (schema upgrade after migration is written).

---

## Part 7c — Layer 2 prompt observability

**Why**: the v3.39.7.3 prompt is long (DECISION RULES BY PERSISTENCE CATEGORY + INVALID DOWNGRADES + evidence_log block per indicator). Haiku may follow some rules better than others. Need to track which rules are being applied correctly.

**Plan**: log Layer 2's reasoning per indicator, comparing `evidence_for_change_vs_yesterday` against the evidence_log's persistence category. Specifically:
- If status preserved (Fired → Fired) AND today's evidence was empty → confirm the persistence category was the right reason (not just lazy preservation).
- If status changed (Fired → Not fired) → verify the reversal_condition was actually matched in today's evidence, not invented by Haiku.

**Output**: a weekly digest summarizing Layer 2 decisions per indicator: # of preserves vs reverses vs new fires, with sample reasoning quotes.

**Files**: probably a new script in `scripts/` consuming the GHA-committed `state/indicator_evidence.json` history via `git log`.

---

## v3.39.7.8 — ✅ SHIPPED 2026-05-21

Four-tier value-chain restructure of `TRACKED_COMPANIES` per user direction. The v3.39.7.7 three-tier shape conflated chip designers (NVIDIA, Broadcom) with ecosystem peers (ASML, TSMC, OEMs) in the same TRACKED_REST bucket. v3.39.7.8 splits into:

- **Tier A — Pure memory + rivals**: SK Hynix, Micron, Samsung, CXMT, ChangXin *(CXMT/ChangXin moved up from old Tier C — they're competitive rivals in the same product market)*
- **Tier B — Chip designers / first-layer bill-payers**: NVIDIA, AMD (NEW), Intel (NEW), Broadcom, Cerebras (NEW)
- **Tier C — Hyperscalers + AI labs / second-layer bill-payers**: MSFT/Azure, Google/Alphabet, Amazon/AWS, Meta, OpenAI, Anthropic
- **Tier D — Ecosystem**: ASML, AMAT, TSMC, Oracle, Dell, HPE

AMD, Intel, and Cerebras were absent from ALL config surfaces pre-v3.39.7.8 (no TRACKED_COMPANIES, no MOTLEY_FOOL_TICKERS, no YAHOO_TRANSCRIPT_TICKERS, no WIDER_BASKET). Now added to all four. Cerebras IPO'd 2026-05-14 (CBRS on Nasdaq); their WSE-3 uses on-chip SRAM not HBM, making them an HBM-substitution-risk signal directly relevant to the v3.4 captive-ratio indicator work.

Pass 2 Haiku rubric rewritten with the 4-tier structure and explicit CONCURRENT (Tier B) vs LEADING (Tier C) signal-type distinction. Both peak at 8-9 but for different reasons — Tier B tells us what chip designers ARE buying today, Tier C tells us what hyperscalers WILL spend in 6-12 months.

273 tests pass (200 from v3.39.7.6 baseline + 73 new in `tests/test_v3_39_7_8_four_tier_restructure.py`; the v3.39.7.7 test file was deleted as the 4-tier structure supersedes the 3-tier it tested).

Files: src/config.py, src/data/news.py, tests/test_v3_39_7_8_four_tier_restructure.py (NEW); deleted tests/test_v3_39_7_7_tracked_companies.py.

---

## v3.39.7.8 — Follow-up watch items (post-shipping)

**Cerebras transcript capture**: first earnings call expected Aug/Sep 2026 (~3 months after May 14 IPO). Watch for "Transcript: Cerebras (Motley Fool)" or "(Yahoo Finance)" row in section 7 when it lands. If the cascade returns `nothing` while Motley Fool / Yahoo clearly have the transcript, investigate — Cerebras is new enough that the platforms may use a non-standard URL pattern.

**Section 1 price table for Cerebras**: CBRS has ~1 week of trading history. The 1Y / 3M / 6M / 1M columns will display "—" until enough days accumulate. By August 2026 the 3M column should populate; 1Y not until May 2027. Don't read missing-percent as a fetch failure.

**AMD and Intel news volume**: AMD ships gaming GPUs (RDNA) + consumer CPUs (Ryzen) + datacenter (EPYC + MI accelerators); Intel ships consumer CPUs + Xeon + Gaudi + foundry. Most of their news is NOT memory-cycle-relevant. The Pass 2 Haiku rubric scores generic AMD/Intel gaming/consumer news at 0-3 (drops). If `state/news_dropped_*.json` shows lots of low-scored AMD/Intel headlines, that's the rubric working as intended. If LOTS of high-scored AMD/Intel headlines flood the brief with non-memory commentary, the Tier B rubric is too permissive and needs tightening (most likely lower the 8-9 band to require explicit HBM/data-center backlog commentary).

**Pass 2 token cost monitoring**: each tier expansion adds ~10-20% more headlines reaching Pass 2. Cumulative effect since v3.39.7.6 baseline is roughly +30-50% Haiku batch calls per run. Still well within rate limits (Haiku 8K input × ~10 batches/run = 80K tokens, comfortably below limits), but worth monitoring for token cost trend in the Anthropic console.

**CXMT/ChangXin scoring uplift**: now in Tier A, CXMT production milestones / capacity-expansion news now score 9-10 (was 7-8 in old Tier C). The May 21 brief surfaced "CXMT 1688% profit surge" as a meaningful supply-side risk signal — that kind of headline should now trigger higher relevance and more Sonnet attention.

**Deferred: TRACKED_CHIP_DESIGNERS dict for downstream indicators**. If a future v3.4 indicator wants to compute "aggregate chip designer HBM purchase commentary" or similar, we'd need a `{name: ticker}` dict analogous to `HYPERSCALERS`. Defer until needed — current scope doesn't have an indicator requiring this aggregation.

---

## v3.39.7.7 — ✅ SHIPPED 2026-05-21

Tracked-company expansion closes the news-pipeline blind spot where direct headlines about NVIDIA / hyperscalers / AI labs were silently filtered at Pass 1.

- **Diagnosis**: pre-v3.39.7.7 `TRACKED_COMPANIES` was a flat 7-entity list (memory pure-plays + equipment + competitors); zero customers. Direct CNBC / Reuters / Bloomberg headlines for NVIDIA, MSFT, GOOGL, META, AMZN, OpenAI, Anthropic dropped at Pass 1.
- **Fix**: split into three explicit relevance tiers per user direction. Tier A (pure memory): SK Hynix, Micron, Samsung. Tier B (key customers / bill-payers): OpenAI, Anthropic, Microsoft/Azure, Google/Alphabet, Amazon/AWS, Meta. Tier C (peers, equipment, OEMs): NVIDIA, Broadcom, TSMC, ASML, AMAT, Oracle, Dell, HPE, CXMT. `TRACKED_COMPANIES` derived as union for back-compat.
- **Pass 2 rubric**: rewrote with explicit per-tier scoring bands (Tier A 9-10 direct / 7-8 thematic; Tier B 8-9 capex / 6-7 general earnings; Tier C 7-8 NVIDIA-class earnings / 5-6 general; architectural shifts 8-10 first-of-kind).
- **Incidental fix**: latent Pass 1 boundary bug since v3.1 — padded keywords (" MU ", " AWS ", " HPE ") failed to match at start/end of headlines because the text being searched wasn't padded. Fixed by padding text with leading + trailing space.

41 new tests in `tests/test_v3_39_7_7_tracked_companies.py` (config structure + Pass 1 acceptance + Pass 1 false-positive resistance including boundary fix + Pass 2 rubric structure). 241 total tests pass.

Files: src/config.py, src/data/news.py, tests/test_v3_39_7_7_tracked_companies.py (NEW).

---

## v3.39.7.7 — Follow-up watch items (post-shipping)

**Pass 1 expansion verification — section 5 includes NVIDIA + hyperscalers + AI labs**: the next fast tier run should now surface direct headlines for these companies. Specifically:
- NVIDIA earnings articles from CNBC, Reuters, Bloomberg (CNBC Technology RSS has 30 items/run)
- Microsoft Azure capex / Google TPU / Meta MTIA / Amazon AWS Trainium articles
- OpenAI infrastructure deals / Anthropic funding rounds when they appear

If these still don't appear, the gap is no longer in TRACKED_COMPANIES — it's in source availability. Next iteration would add dedicated Google News queries: "NVIDIA earnings data center revenue", "Microsoft Azure AI capex commitment", "OpenAI compute infrastructure", "Anthropic capacity expansion", etc.

**Pass 2 token cost monitoring**: more headlines now reach Pass 2 (Tier B and expanded Tier C). Estimated +30-50% Pass 2 Haiku calls per run. Watch the `state/news_dropped_YYYY-MM-DD.json` file size — if it grows substantially, that's healthy (more headlines getting filtered at Pass 2 vs silent Pass 1 drops). If it explodes (10x), the rubric may be too permissive on Tier B/C and needs tightening.

**Boundary fix retroactive effect**: the padding fix at `score_relevance` will also unlock matches that were silently failing on ` MU ` at start/end of headlines pre-v3.39.7.7. If Micron-themed headlines suddenly appear with more frequency vs the historical baseline, that's the boundary fix taking effect on real data — not a relevance regression.

**Deferred follow-up — dedicated Google News queries for hyperscaler/AI lab capex**: the existing RSS feeds (CNBC, Benzinga, KED, etc.) cover most NVIDIA/MSFT/GOOGL/META coverage organically. But targeted Google News queries would catch articles in publications NOT in the RSS feed list. Candidates: "Microsoft Azure AI capex 2027", "Google capex AI infrastructure", "Meta capex AI buildout", "Amazon AWS Trainium production", "OpenAI Stargate buildout", "Anthropic compute commitment", "NVIDIA Q1 FY27 data center revenue". Each query would add one Pass 1 fetch (well within the existing 70+ Google News query budget). Estimated effort: 30 min. Gate: run v3.39.7.7 for 1-2 weeks first to see how much organic coverage the RSS feeds already provide vs how much these queries would add.

---

## v3.39.7.6 — ✅ SHIPPED 2026-05-21

Four-issue release addressing user feedback on the May 21 v3.39.7.5 brief. Details in CHANGES.md.

- **Issue 1** — Exec summary duplicate fix: the old `delta_from_prior_text` italic block survived the v3.39.7.5 consolidation cleanup and rendered alongside the new `executive_brief` paragraph. Removed `delta_line` construction + return concatenation in `_build_executive_summary`. Field stays on dataclass for prior_state.json back-compat.
- **Issue 2** — BENCHMARKS regression repair: `fast_tier.py` was calling `fetch_price_history(WIDER_BASKET, ...)` which silently broke `get_relative_strength_vs_sox` and `get_dram_etf_vs_smh` because `^SOX` / `SMH` / `DRAM` columns were never fetched. `t3_relative_strength` had been rendering "(not computed)" for SOX spreads across an unknown number of releases. Swapped to `ALL_TICKERS`; dead `BENCHMARKS` import (smoking gun of the refactor that broke it) removed.
- **Issue 3** — Sonnet RECENCY rule on contradicting events: May 21 brief wrote about a Samsung strike "commencing" when Korea Herald had already reported it was called off one hour before launch. Two compounding architectural gaps: no prompt rule for recency-on-contradiction, AND triage block had no timestamps. Fixed both: new RECENCY ON CONTRADICTING EVENTS block in Sonnet prompt (5 operational rules, May 21 case as worked example), `news_items` parameter threaded through `build_sonnet_analysis_prompt`/`sonnet_analyze`/`analyze_with_llm` to stamp each triage item with its published timestamp. Per user direction: prompt-level fix, not keyword hardcoding.
- **Issue 4** — Diagnostics 5th health state `quiet`: forward-looking architecture Google News watchers (CXL 3.0 production, GDDR7 inference accelerator, HBF, MTIA, etc.) and pre-listing SK Hynix SEC EDGAR feeds rendered as WARN indistinguishable from real failures, training the reader to ignore the WARN column. New `expected_empty` field on `SourceFetchStatus` → new `quiet` health when reached+0+expected_empty. `WATCH_GOOGLE_QUERIES` + `EXPECTED_EMPTY_EDGAR_FEEDS` sets in config.py enumerate the speculative watchers. Template renders QUIET as muted gray pill vs WARN amber. Back-compat: defaults preserve prior behavior for all existing callers; persisted v3.39.7.5 statuses load via dataclass default.

200 tests pass (180 prior + 20 new in `tests/test_v3_39_7_6_fixes.py`, organized as one test class per issue: 3 + 3 + 4 + 10 covering core path, back-compat, and edge cases for each fix).

Files: src/email_module/template.py, src/fast_tier.py, src/analysis/llm_summarizer.py, src/diagnostics.py, src/config.py, src/data/news.py, tests/test_v3_39_7_6_fixes.py (NEW).

---

## v3.39.7.6 — Follow-up watch items (post-shipping)

**Issue 2 verification — t3_relative_strength fires when it should**: with the BENCHMARKS fetch restored, the next brief's `t3_relative_strength` indicator card should show NUMERIC values for SK Hynix 5d vs SOX and Micron 5d vs SOX (no more "(not computed)"). Given recent moves (SK Hynix 1W -11.69% per the May 21 brief vs SOX roughly flat), the indicator should fire RED. If it doesn't fire despite numeric values clearly below -10pp, investigate the column-name match in `get_relative_strength_vs_sox` directly (the column name "MSCI ACWI Semiconductor" is hardcoded in `prices.py:394`; any future config rename in `BENCHMARKS` would silently break it again).

**Issue 3 verification — Sonnet honors recency on contradicting events**: hard to verify until another contradicting-events story appears organically. Two diagnostic surfaces to watch:
- The Sonnet prompt itself (via the prompt-capture mechanism, if added) — confirm the RECENCY block is present and triage items are stamped with timestamps in production runs.
- The `executive_brief` text — when a story DOES reverse intra-day, the paragraph should explicitly note the reversal rather than describe the older state. If Sonnet still ignores the rule, consider tightening: making the rule a top-level prompt section (not nested under EXECUTIVE BRIEF SPECIFICATION), or moving it earlier in the prompt before the news triage block.

**Issue 4 verification — QUIET classification doesn't mask real WARNs**: the table should now show ~30-40 entries in the QUIET state (the architectural watchers + SK Hynix EDGAR) and a substantially reduced WARN count. After 1-2 weeks of production:
- Audit any feed that's been WARN for 3+ consecutive runs — that's a candidate for either (a) it's genuinely broken (fix it) or (b) it actually belongs in WATCH_GOOGLE_QUERIES (re-categorize)
- Audit any feed that's been QUIET continuously but starts returning items — that's the intended signal trigger; verify the items are actually relevant to the captive-ratio indicators
- Korea Herald, KED Global, Benzinga, etc. should NOT be quiet — if they ever show QUIET, something is misclassified

**Issue 4 follow-up — extend `expected_empty` to other source types**: the v3.39.7.6 release only annotated Google News + EDGAR. RSS feeds with similar semantics (e.g., DRAMeXchange Weekly Research, which only publishes once a week) might benefit. Defer pending observability data — if any RSS feed routinely WARNs on weekdays where no publication is expected, add it to a future `EXPECTED_EMPTY_RSS_FEEDS` set with the same plumbing.

---

## v3.39.7.5 — ✅ SHIPPED 2026-05-20

Five-issue release addressing user feedback on the May 20 v3.39.7.4 brief. Details in CHANGES.md.

- **Issue 1** — KRX staleness repair: yfinance returned SK Hynix at yesterday's close while Yahoo's own page showed today's flat. Three-layer defensive fix in `prices.py` (asof check + FMP fallback + Naver fallback). New `fetch_quote_for_ticker` helper in `naver_finance.py`.
- **Issue 2** — Cycle matrix card moved from section 3 (Tier indicators summary) to section 4 (Top 5 deep-dive), where it belongs with the DRAM Price Evolution deep-dive narrative.
- **Issue 3** — Exec summary restructure: URGENT banner killed; the three v3.39.7.4 blocks (delta prose / big_movers list / Today's market) consolidated into ONE 5-8 sentence `executive_brief` paragraph written by Sonnet, covering 4 themes (signal changes, big news with directional-signal prioritization, big movers, macro). Anchor link convention `[→§N]` substituted to clickable HTML pointing at `id="section-N"` anchors on every numbered h2.
- **Issue 4** — 24h ephemeral trigger event window: new `state/trigger_events_24h.json` log + 3 helpers in `state.py`. Multiple manual reruns same day keep showing the same trigger callouts (key user requirement); tomorrow they auto-prune naturally.
- **Issue 4b** — Korea Herald RSS URL repaired (the old `020000000000.xml` endpoint had been 404'ing); 5 new Google News queries for Korean labor news so future Samsung-strike-style stories don't slip through.

180 tests pass (160 prior + 11 in NEW `tests/test_trigger_log_24h.py` + 9 in NEW `tests/test_executive_brief.py`). xlsx renamed to `_v3_39_7_5.xlsx` (no content changes — no indicator config changed in this release; row 7 of Tier 2 sheet still reflects the v3.39.7.4 capex/OCF rewrite).

Files: src/data/prices.py, src/data/naver_finance.py, src/fast_tier.py, src/email_module/template.py, src/analysis/llm_summarizer.py, src/state.py, src/config.py, .github/workflows/fast_tier.yml, tests/test_trigger_log_24h.py (NEW), tests/test_executive_brief.py (NEW).

---

## v3.39.7.4 — ✅ SHIPPED 2026-05-20 (Batches 1 + 2)

Two-batch correction following the May 20 production diagnostic. Details in CHANGES.md.

**Batch 1** (4 surgical fixes):
- 1a: matrix NameError (file ordering bug, fixed)
- 1b: tier count mismatch (deterministic post-processing, fixed)
- 1c: idempotency checkout race (`_refresh_from_origin_main()` added)
- 1d: cron schedule overlap + silent push failures (4 crons at 30-min spacing + hardened push)

**Batch 2** (bear case + capex/OCF):
- 2a: bear case magnitude calibration via Sonnet prompt section D (historical anchor -43%, bands keyed to fired-indicator regime)
- 2b: capex/OCF metric rebuild — ORCL dropped, last-published-quarter ratio per company + cross-company mean, thresholds 40% partial / 50% fired, severe-single-co-strain flag at >100%

160 tests pass (150 prior + 10 new). Files touched: src/fast_tier.py, src/analysis/llm_summarizer.py, src/analysis/signal_reasoner.py, src/data/estimates.py, src/config.py, .github/workflows/fast_tier.yml, tests/test_tier_status_summary.py (NEW), tests/test_v3_39_6_calculation_driven.py (updated).

---

## Follow-up watch items (post-shipping)

Items to monitor across the first ~2 weeks of v3.39.7.4 production runs. Not bugs — observability targets.

**Tier count overrides**: the v3.39.7.4 `_compute_tier_status_summary` logs `WARNING` whenever Sonnet's prose count differs from the deterministic count. Watch the cadence of these in the brief logs:
- High rate (>50% of runs override) → Sonnet's arithmetic is consistently off, prompt could be tightened to remove `tier_status_summary` from Sonnet's output entirely (drop the field from the JSON schema)
- Low rate (<10%) → keep current setup, prompt is mostly working
- Zero rate over a week → consider deleting the override (Sonnet is reliable)

**Capex/OCF threshold calibration**: the 40/50% thresholds were calibrated against annual data. Quarterly data has higher variance — a single capex spike from a one-off data center buildout could push >50% in a normally-calm cycle period. If we see the indicator firing in periods where the cycle isn't actually rolling, raise the threshold (probably to 50/60% for partial/fired).

**OCF row label discovery**: yfinance can return any of `Operating Cash Flow` / `Total Cash From Operating Activities` / `Cash Flow From Continuing Operating Activities` / `OperatingCashFlow` / `Cash Flow From Operating Activities`. Watch the warning logs for `No operating cash flow row found in yfinance cashflow for {ticker_symbol}` — if any of the 4 hyperscalers stops returning OCF, the mean computation degrades (drops to mean of 3). The candidate list may need extending.

**Cron race elimination verification**: after v3.39.7.4 deploys, monitor for ~3 days that the 11:43 / 12:13 / 12:43 UTC crons all no-op cleanly (~30s runs) once the 11:13 cron succeeds. If any of them runs the full pipeline despite a successful 11:13, the `_refresh_from_origin_main()` mechanism isn't enough and we'd need a stronger barrier (probably a `git log --grep` check).

---

## v3.39.7.5 — Follow-up watch items (post-shipping)

**KRX repair frequency**: each KRX repair via FMP or Naver logs `WARNING` `KRX ticker {symbol} ({name}) has stale price history: latest={d1}, expected={d2} ({n}d stale)`. Watch in production:
- 0% rate → yfinance KRX coverage is fine; repair path is dormant insurance
- Occasional (<10% of runs) → expected; repair path doing its job
- High rate (>50%) → yfinance has a systemic KRX issue; consider switching the primary fetch to FMP for KRX names and using yfinance as fallback instead

**FMP coverage gaps**: `_fetch_krx_quote_from_fmp` returns None on any FMP failure (404, 401, missing price field). When this happens for SK Hynix or Samsung, the Naver fallback should pick up — verify by checking that the success path log line `Repaired {symbol} via Naver: today's close = ...` actually fires on at least some runs. If Naver also fails consistently, the brief renders the stale yfinance value flagged as such in the data source assessment. No additional fallback is planned — three sources is enough.

**Executive brief quality**: the consolidated 5-8 sentence paragraph from Sonnet is the most visible part of the brief. Watch over the first week for:
- Whether Sonnet emits the `[→§N]` anchor link tokens correctly (regex substitution catches them; if it ever emits malformed tokens like `[→§]` or `[→§X]`, those will appear as literal text)
- Whether the paragraph actually covers all 4 themes or systematically skips one (likely (iv) macro, since it's the most lateral theme)
- Length compliance — if Sonnet routinely writes <5 sentences (too thin) or >8 (too long), tighten the prompt
- Directional-signal news prioritization — is the AsiaEconomy "Sell SK Hynix"-class news actually getting elevated in the paragraph? If not, the prompt's example needs to be made more prescriptive

**24h trigger log behavior**: the log is the user's primary feedback channel for state changes during the day. Watch:
- That manual reruns after the morning cron still show the same trigger callouts (the core requirement). If they vanish, the override in fast_tier.py isn't taking effect — check the log lines `Trigger event log: N entries in 24h window`
- That `fired_at_iso` doesn't migrate on rerun (each entry keeps its first-observed timestamp)
- That entries actually clear next calendar day. If the brief on day N+1 still shows day N's triggers, the prune logic in `load_trigger_log_24h` isn't working — likely a timezone confusion (24h is rolling UTC, but the user reads on Munich time which is +1 or +2)

**Korea Herald URL longevity**: koreaherald.com has restructured RSS URLs at least twice in the past 5 years per public blog posts. The current `/rss/kh_Business` + `/rss/kh_National` URLs were probed live on 2026-05-20 but could die at any time. Watch the data source assessment section for `Korea Herald: 0 results` repeating across multiple consecutive runs — that's the signal to re-probe. The Google News fallback queries added in v3.39.7.5 provide partial backup but won't catch everything Korea Herald carries directly.
