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

### 5a — Wire matrix verdict into Layer 2 signal_reasoner

**Why**: in v3.39.7 the matrix verdict lives only in the email card + the deterministic indicator status override (`_override_t1_spot_decline_from_matrix`). It never reaches `signal_reasoner.py:reason_all_signals()` which produces the Layer 2 per-signal verdicts that Sonnet now receives (closed in v3.39.6). Result: Sonnet's exec summary references the OTHER 21 signals' Layer 2 verdicts but only references t1_spot_decline through the indicator badge — not through structured signal_reasoning. The exec summary cannot say "cycle matrix in Hold at R1/C2 with synthetic 54.6% QoQ" without the LLM re-deriving those numbers from prose.

**Closed spec**:

1. Add a new SignalReasoning entry produced upstream of Layer 2 (not in Layer 2 itself — Layer 2 is per-signal Haiku, and the matrix verdict is deterministic, so it's parallel to the rel_strength_full case which v3.39.6 already does this way).
2. In `fast_tier.py` after `_run_cycle_matrix_step` returns the verdict, construct a `SignalReasoning(signal_id="t1_spot_decline", verdict=...)` and APPEND or REPLACE the entry in `signal_reasonings` list.
3. The structured fields written are the matrix verdict deconstructed: `cell` (HOLD/WARN/FIRE string), `cell_label`, `contract_row`, `spot_col`, `synthetic_central_pct`, `anchor_quarter`, `anchor_age_days`, `drawdown_pct`, `weeks_since_peak`, and the three adjustment values (`tone_adj_pp`, `supplier_adj_pp`, `spot_lead_adj_pp`).
4. Sonnet's prompt already iterates over `signal_reasonings`; no prompt change needed — the v3.39.6 LAYER 2 PER-SIGNAL VERDICTS block automatically picks up the new entry. Sonnet's exec summary can then reference the matrix structured fields directly without re-derivation.

**Verification**: re-render the May 17/18 May briefs locally with the matrix verdict piped in. Confirm Sonnet's exec summary references matrix numbers verbatim (not paraphrased), and that t1_spot_decline appears in the "LAYER 2 PER-SIGNAL VERDICTS" block in the Sonnet prompt.

**Files**: `signal_reasoner.py` (new dataclass init path, no Layer 2 Haiku change), `fast_tier.py` (post-matrix append step), tests in `test_v3_39_6_calculation_driven.py` extended with a t1_spot_decline parity case.

### 5b — GHA fast_tier.yml commit step for data/*.csv

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

---

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
