# v3.4 ROADMAP — Captive-Ratio Indicator (DHCR)

## v3.4 shipped releases (2026-05-21 / 22)

The v3.4 series consists of three shipped releases plus a v3.4.3 quick-wins candidate. The actual shipped path diverged substantially from the original ROADMAP plan — what was planned as "ship the indicator in v3.4.0, then activate trigger gates in v3.4.1" became "ship a delta-number direction monitor in v3.4.0, redesign the entire framework around a 2-axis matrix in v3.4.2 after the May 22 design session with Raul." The original v3.4.1+ plan (lines documented in the historical section below) is superseded by what actually shipped.

### ✅ v3.4.4.1 (2026-05-22) — Full automation: Layer 1 extraction → aggregator → state file

v3.4.4 shipped with `data/competitor_thresholds.yaml` documented as a "hand-curated monthly refresh" — a misframing that Raul caught immediately ("im not doing anything manual. What are u talking about?"). The intent has always been Layer 1 extraction as the data source; v3.4.4.1 cleans up the framing AND ships the wiring.

Five v3.4.4 references implied manual operator action — all corrected. Architecture split: `data/` files now hold thresholds + schema + extraction-field definitions + source authority tiers (stable, never auto-modified); `state/` files hold the live measured values (sole writers: paired aggregator modules running in fast-tier pipeline before snapshot computation). Layer 1 digester `DIGEST_SYSTEM_PROMPT` extended with `competitor_signals` (23 fields) + `adr_signals` (9 fields) sub-blocks mirroring `architecture_signals`. New `_log_indicator_signals()` shadow logger persists to existing `state/extraction_log_*.jsonl` files. New aggregators (`src/analysis/competitor_aggregator.py`, `src/analysis/adr_aggregator.py`) apply tier-weighted conflict resolution (Tier A: SK Hynix/Samsung/Nvidia/Mirae/KB earnings & broker; Tier B: Counterpoint/TrendForce/SemiAnalysis; Tier C: DigiTimes/ChosunBiz/Korea Times). Bootstrap state seeded once on deploy; thereafter aggregators are the only writers. Atomic state-file writes (tmp + rename). Safe-failing throughout.

Tests: 473 passing (462 v3.4.4 baseline + 11 new aggregator tests; v3.4.4 test helpers updated for thresholds/state split). Today's read confirmed against bootstrap: Competitor Partially fired ~11.5% cumulative EPS (7B-driven); ADR Pre-event lifecycle.

### ✅ v3.4.5 (2026-05-22) — DHCR catalog auto-update

Closes the DHCR architectural-debt item flagged at v3.4.4.1 shipping. The three DHCR reference YAMLs (`inference_arch_reference`, `hbm_per_accelerator`, `enterprise_ai_revenue`) now follow the same data/state split as the v3.4.4.1 Indicator 7 + 8 files. Schema YAMLs hold formulas + extraction definitions + source tiers; state JSONs hold the live model/accelerator/quarterly entries. `load_dhcr_data()` merges both transparently — downstream `compute_*()` functions unchanged.

Three new aggregator modules wired into fast_tier.py before DHCR computation. Three new field-groups added to `DIGEST_SYSTEM_PROMPT` under `architecture_signals`: `new_frontier_model_release`, `new_accelerator_announcement`, `enterprise_ai_quarterly_revenue`. Bootstrap state seeded from v3.4.0 YAML contents (preserved verbatim).

10 new tests; 483 passing. End-to-end verified: DHCR position_score 52, frontier_kv 14 KB/tok via the merged data/state path.

### ✅ v3.4.6 (2026-05-22) — Frontier-model source ingestion

Closes the v3.4.5 Tier-A gap. 9 new RSS_FEEDS entries (Meta AI / DeepSeek / Mistral / Qwen / Anthropic / DeepMind / HF Papers / arxiv cs.LG / arxiv cs.CL). arxiv pre-filter (`arxiv_frontier_paper_filter()`) in news.py applies at fetch time — title-keyword filter for lab names + architecture-family-plus-params + release-language keywords — drops ~95% of routine arxiv volume before items reach the relevance scorer. Source classification extended in `_classify_source_for_tier()`. 11 new tests; 494 passing.

When the next DeepSeek / Llama / Qwen / Mistral frontier release drops, the arch aggregator picks it up from the lab blog directly within hours and DHCR's frontier_kv computation uses the new model on the next fast-tier run.

Known unresolved: Cloudflare TLS-fingerprint blocking on some lab blogs (Mistral, DeepSeek possibly) — same root-cause as the v3.38.4 Investing.com + Damnang issue. If those persist after deploy, curl-cffi integration is a v3.4.7 candidate.

### ✅ v3.4.6.3 (2026-05-22) — CRITICAL PyYAML fix + RSS reconciliation

The headline issue Raul caught: section 4 only showed 5 cards instead of 8. Production logs revealed `PyYAML` was never in requirements.txt — every yaml-importing module (`dhcr.py`, `competitor_aggregator`, `competitor_scoring`, `adr_aggregator`, `adr_event_tracking`, 3 DHCR sub-aggregators) failed silently, snapshots returned None, template skipped the cards. **DHCR has been broken in production since v3.4.0**; Indicator 7 + 8 since v3.4.4. One-line fix: `PyYAML>=6.0` to requirements.txt.

RSS reconciliation: 6 of the 9 v3.4.6 lab-blog feeds dropped after the May 22 deploy confirmed they don't expose working RSS (Meta / DeepMind / DeepSeek / Anthropic / Qwen — no native RSS; HF Papers requires auth). Compensated with 14 new WATCH_GOOGLE_QUERIES entries for frontier-model release coverage via tech press. Mistral kept at retry URL. arxiv cs.LG + cs.CL still working. Section 4 will finally show all 8 cards on the next run.

527 tests passing (3 new PyYAML-availability tests guard against this class of regression).

### ✅ v3.4.7 (2026-05-22) — Section 4 reorder + curl-cffi + lab blog scrapers

Section 4 card order locked per Raul: top-5 → DHCR (6) → Competitor (7) → ADR (8, last). The two v3.4.6.3 queued items shipped:

1. **curl-cffi**: `smart_get()` helper in news.py uses `curl_cffi.requests.get(impersonate="chrome120")` when available, falls back to stdlib `requests`. `_safe_parse_rss_feed` catch clauses extended via `_HTTPError_Types`/`_RequestException_Types` tuples to handle both clients. Bypasses Cloudflare JA3/JA4 TLS fingerprinting.

2. **Lab blog HTML scrapers**: new `src/data/lab_blog_scrapers.py` with parsers for Anthropic / Meta AI / DeepMind blogs. Recovers the Tier-A source-of-origin path that was lost when v3.4.6.3 dropped these RSS feeds. Source attribution maps to existing `_classify_source_for_tier` labels — DeepSeek release on anthropic.com routes to Tier A automatically.

542 tests passing. v3.4.8 candidate: restore Damnang Substack + Investing.com x4 (dropped v3.38.4 for TLS fingerprint) once production confirms curl-cffi works on GHA runner.

### 📋 v3.4.8 (candidate) — Restore Cloudflare-blocked feeds + DeepSeek/Qwen scrapers

Pending production confirmation of v3.4.7's curl-cffi behavior on the GHA runner:
1. Restore Damnang Substack + Investing.com x4 (`Investing.com MU / NVDA / AMAT / ASML`) to RSS_FEEDS. These were dropped in v3.38.4 specifically because requests' TLS fingerprint failed Cloudflare; if curl-cffi chrome120 impersonation works in production, these should come back online without additional changes.
2. Add HTML scrapers for DeepSeek + Qwen if their blog structures stabilize.

## v3.4 shipped releases (2026-05-21 / 22)

### ✅ v3.4.0 (2026-05-21) — DHCR direction monitor (initial version)

One new Tier 1 indicator (`t1_decode_hbm_captive_ratio`), three new YAML reference files (architecture / HBM-per-accelerator / enterprise revenue), 11 quarters of bootstrapped trajectory data (Q4 2023 → Q2 2026). Framework: delta-number direction signal (5-level: strongly_bullish_hbm → strongly_bearish_hbm), composite of three drivers (KV efficiency YoY, HBM-per-accelerator YoY, enterprise AI revenue YoY weighted blend). Phase 2 fix: workflow YAMLs now commit `state/extraction_log_*.jsonl` and `state/dhcr_state.json`.

**Note**: the v3.4.0 delta-number framework was REPLACED by the v3.4.2 matrix framework. The underlying YAMLs and computation pipeline remain in use but the v3.4.0 5-level direction enum is gone.

### ✅ v3.4.1 (2026-05-22) — UI fixes + Reversed eradication + length caps

Five fixes diagnosed from May 22 production screenshots. (1) Trigger event banner color in exec summary now varies by destination status (red Fired / green Not fired / yellow Partially fired). (2) Section 3 change-flag reverted from v3.39.7.9 colored background to plain muted text. (3) "Reversed" status eradicated 4 ways: VALID_STATUSES gate, legacy migration, render-time scrub, Sonnet prompt forbidden vocabulary. (4) Executive brief HARD CAP 250 words. (5) where_we_stand HARD CAP 120 words + never-empty rule + render-time Layer 2 fallback.

**Note**: this release was NOT the "activate trigger gates" plan from the original v3.4.1 ROADMAP. The actual v3.4.1 was UI fixes; the trigger gate concept itself was conceptually superseded by v3.4.2's score-based status mapping.

### ✅ v3.4.2 (2026-05-22) — DHCR matrix redesign + reliability + CapEx

Three workstreams:

**(A) Reliability**: Sonnet streaming call wrapped with 3-retry app-level backoff (30s/90s/240s, ~6min budget) + Haiku 4.5 fallback if all retries fail. Layer 2 per-signal Haiku calls wrapped with 3-retry backoff (10s/30s/90s). DHCR explicitly skipped from Layer 2 (verdict is deterministic). Closes the May 22 03:50 UTC 529 incident where SDK retry was insufficient for sustained API overload.

**(B) DHCR framework redesign** per the May 22 design session: 5×5 matrix mapping KV efficiency (Y, 5 bands) × operative context length (X, 5 bands) onto memory-architecture zones per the May memo. Each cell maps to a memory zone label AND a linear score 0-100 (HBM-favorable ↔ commodity). Three new mechanisms: (i) dual-input X-axis via new `data/context_length_signals.yaml` — operative context = Σ (category_context × volume_share/100) across 5 categories; (ii) anticipation premium — market-deployed cell = workload-implied cell + 1 Y-band, accounts for hyperscalers provisioning HBM ahead of literal demand; (iii) sanity check — score validated against 3 substitution-evidence signals, ≥2 contradicting raises CALIBRATION FLAG. Volume layer separate from position (HBM bit demand = score × volume_index/100). Section 4 renamed "Top 6" with DHCR card rendered FIRST (with matrix chart + bull/bear case context boxes + framework primer). Section 3 unchanged.

**(C) CapEx capitalization**: 86 replacements in user-facing strings. Code identifiers unchanged.

Today's read: KV 14 KB/tok → R3, operative context 114K → C2, workload R3C2 → market R4C2 (anticipation +1), score 52, Not fired, 3/3 sanity confirm.

## ✅ v3.4.3 (2026-05-22) — Quick wins shipped

Three small items from the v3.39 follow-up backlog, all bundled into one micro-release:

**Fix 3a — Layer 2 prompt observability** (was Part 7c). `src/analysis/signal_reasoner.py` appends a per-call audit record to `state/layer2_audit_YYYY-MM-DD.jsonl` for every Layer 2 Haiku call: timestamp, signal_id, tier, prior_status, prompt fingerprint (head 400 chars + total length), evidence input counts, verdict (fired_status/convergence/confidence/consensus_direction/evidence_for_change), and status_transition (None / "no_change" / "Fired→Not fired"). Wrapped in `try/except` — observability never breaks the brief. `state/layer2_audit_*.jsonl` added to fast_tier.yml commit set so per-day audit files accumulate in git. Companion script `scripts/audit_layer2.py` reads the JSONL and produces a summary table (per-indicator call count + convergence% + confidence% + status transitions detail with reasons + reliability check on zero-evidence calls + prompt size distribution).

**Fix 3b — HBM spot/contract price trend automation**. New function `compute_hbm_price_trend(spot_history_csv, contract_forecasts_csv, lookback_days=90)` in `src/data/prices.py` derives `rising`/`flat`/`falling` from accumulated CSV data: (a) DDR5 16Gb spot slope over trailing 90 days (thresholds ±$0.05/day), (b) contract forecast revision direction for latest quarter (thresholds ±3pp central_pct delta). Both signals must agree to call rising/falling; mixed → flat. `src/fast_tier.py` now passes auto-derived `hbm_price_trend` and `extraction_log_signals` to `compute_dhcr_snapshot()`, replacing the operator-supplied `hbm_price_trend="rising"` default.

**Fix 3c — Layer 1 digester retry**. `src/analysis/digester.py:digest_document()` now has the same 3-retry pattern as v3.4.2 added to Sonnet and Layer 2: `[10s, 30s, 90s]` backoffs for 529/overloaded errors. Per-document isolation. Closes the audit gap from v3.4.2 — previously Layer 1 relied on SDK's ~3s built-in retry which is insufficient for sustained API overload windows.

**Bug fix discovered during 3b wiring**: the existing `fast_tier.py` log line at the DHCR computation site referenced v3.4.0 fields (`.direction`, `.frontier_hbm_gb_per_accelerator`, `.implied_context_capacity_per_accelerator_tokens`, `.capacity_creation_yoy_pct`) that NO LONGER EXIST after the v3.4.2 framework redesign. Would have thrown `AttributeError` in production. Log line rewritten to v3.4.2 fields (status, score, workload_cell, market_cell, KV GB/1M, op_ctx, vol_idx, hbm_demand, sanity confirms ratio, calibration_flag). This is the kind of regression v3.4.2's CapEx sweep should have caught had the v3.4.2 release process exercised the full DHCR log path.

414 tests pass (385 v3.4.2 baseline + 29 new in `tests/test_v3_4_3_quick_wins.py` across 8 classes covering: audit record schema + defensive try/except + path format, HBM price trend synthetic rising/falling/flat scenarios + contract revision detection + missing-CSV handling, DHCR integration + extraction_log_signals threading + stale-field removal regression test, Layer 1 retry backoffs + 529 detection + per-document isolation, fast_tier.yml commit set, audit_layer2.py script existence + stdlib-only imports + empty-records handling, no v3.4.2 regressions).

---

## ✅ v3.4.3.1 (2026-05-22) — DHCR placement hotfix + Fired pill readability

Four user-visible issues from v3.4.2 that v3.4.3 shipped without fixing. Raul surfaced them on the May 22 2026 production email review session. Scope deliberately small — placement and rendering only; no framework changes.

**Fix 1 — DHCR removed from section 3 tier indicator status table**. DHCR was rendering as a row in the Tier 1 group since v3.4.0. Per `DHCR_Framework_Explained.docx` Appendix B and the May 22 framework session, DHCR is structurally Tier 1 but renders ONLY as a section 4 deep-dive card — the matrix + score + sanity outputs don't fit the section 3 row format. The v3.4.2 framework redesign was supposed to delete the section 3 row; a handover propagation error preserved a "leave section 3 unchanged" note that contradicted the intended decision. `_build_indicators_section` per-tier loop now filters out `t1_decode_hbm_captive_ratio` before iteration.

**Fix 2 — `_build_dhcr_section3_snippet` function deleted (86 lines)**. The function rendered a v3.4.0 5-level direction enum and accessed four dataclass fields the v3.4.2 redesign eliminated (`direction`, `frontier_hbm_gb_per_accelerator`, `capacity_creation_yoy_pct`, `implied_context_capacity_per_accelerator_tokens`). Same staleness pattern as the v3.4.3 fast_tier.py log line fix.

**Fix 3 — DHCR section 4 card moved from FIRST to LAST position**. DHCR is the macro framing closer of section 4, not the opener. Section 4 order is now: header → top-5 deep dives → DHCR card.

**Fix 4 — White text on Fired and Partially fired status pills**. Default near-black text on the dark red and dark amber backgrounds was unreadable. White text now conditional on those two statuses; light backgrounds keep near-black. Applied in both the section 3 tier table and the section 4 top-5 deep-dive card headers for consistency.

**Important correction to the historical record**: the v3.4.2 entry below (and at line 25 of this file) describes DHCR placement as "Section 4 renamed 'Top 6' with DHCR card rendered FIRST. Section 3 unchanged." Both clauses are now incorrect after v3.4.3.1. The v3.4.2 entry is preserved verbatim because it records what v3.4.2 actually shipped with — not what was intended. The authoritative current state is: **DHCR appears in section 4 ONLY, as the LAST card. NEVER in section 3.** See `CLAUDE.md` locked decisions #5 and `DHCR_Framework_Explained.docx` Appendix B.

423 tests pass (414 v3.4.3 baseline + 9 new in `tests/test_v3_4_3_1_dhcr_placement_and_pills.py` across 4 classes; one v3.4.2 placement test flipped from `assertLess` to `assertGreater` and renamed accordingly).

---

## ✅ v3.4.4 (2026-05-22) — Indicator 7 (Competitor) + Indicator 8 (ADR) + DHCR spectrum bar

The biggest content release since v3.4.0 (DHCR introduction). The framework session 2026-05-22 with Raul produced a substantially different calibration approach than the original ROADMAP v3.4.4 planning text (preserved below at lines 200+ for historical record). Key changes from the plan:

- **Indicator numbering corrected**: DHCR=6, Competitor=7, ADR=8 (originally planned as Competitor=6, ADR=7 before the DHCR-as-Indicator-6 framing was confirmed)
- **EPS-impact calibration replaces share-%-threshold approach**: per Raul's nuance — "the challenge on competitors should come from will this hit the ASP and will this hit my volume." Status mapped to estimated SK Hynix EPS damage (price × volume), not raw share movements
- **Composite logic uses cumulative EPS + Part 14 dual-threat amplifier**: 2+ subs Fired escalates regardless of cumulative; 1 Fired + 2+ Partially fired also escalates because multi-front compression exceeds single-vector damage
- **7D HBF confirmed two-sided**: not just substitution risk, but also Solidigm (former Intel NAND) moat-extension upside. Renders 🛡️ Moat-extension + ⚔️ Substitution-risk milestone boxes rather than collapsing nuance into a single status

### Section 4 final layout (8 cards)

1. Sonnet pick #1
2. Sonnet pick #2
3. Sonnet pick #3
4. Sonnet pick #4
5. Sonnet pick #5
6. **Indicator 7 — Competitor dynamics** (NEW) — 4 sub-cards (7A CXMT, 7B Samsung HBM4/4E, 7C Micron, 7D HBF) + composite status pill + Part 14 dual-threat scenario callout
7. **Indicator 8 — US ADR / SPO event tracking** (NEW) — phase banner + composite status pill + confirmed offering structure box + 4 sub-cards (8A F-1, 8B roadshow, 8C pricing, 8D insider) + calendar milestones table + academic calibration prose
8. DHCR (still last per v3.4.3.1 lock) — now with v3.4.4 spectrum bar inline after matrix chart

Section title: "Top 6 indicators — deep dive" → **"Top 8 indicators — deep dive"**.

### Indicator 7 EPS-impact framework (locked)

Status bands per sub-indicator: Not fired <5% EPS impact, Partially fired 5-20%, Fired >20%. Per-sub mechanics (the nuances that distinguish each sub) documented in `CLAUDE.md` locked decision #6. Composite = cumulative EPS impact across the 4 subs + Part 14 dual-threat amplifier.

**Today's read (2026-05-22 bootstrap)**: Partially fired, ~11.5% cumulative EPS impact, single-front (7B-driven by Samsung capturing ~28-30% of Vera Rubin HBM4 + HBM4E qualification tied). 7A/7C/7D all Not fired. Dual-threat inactive.

### Indicator 8 ADR framework (locked)

Four sub-indicators: 8A F-1 filing watch + underwriter syndicate quality; 8B roadshow demand quality; 8C pricing-day execution (424B discount); 8D insider sales >$10M in offering window. Locked design: canceled F-1 = Fired; insider threshold $10M; Fired persists through Phase 4 (no 90-day reversion); composite = worst-of subs UNLESS peak-coincidence rule escalates (ADR completion + late Phase 4 → Fired per Part 11 / SK Hynix 1995-1996 pattern).

**Today's state (2026-05-22 bootstrap)**: Pre-F-1, all 4 subs in Pre-event waiting state. Phase banner "📅 Awaiting F-1 filing — expected June-July 2026". Confirmed offering structure rendered prominently (17.4M new shares, 2.4% dilution, $10B central case raise, treasury path closed). Calendar milestones inline. Academic calibration prose at footer (Doidge-Karolyi-Stulz 2004, Sarkissian-Schill 2009; -4 to -9% cumulative mechanical drag).

### DHCR spectrum bar (per Raul's hand-drawn sketch)

Inline HTML/CSS vertical bar inside the DHCR section 4 card, rendered right after the matrix chart. Score axis 0-100 (top→bottom), 5 stacked color-coded segments matching matrix palette. Three visual columns: bar | Memory type | SK Hynix moat. Today's score (52) marked with a left-pointing red arrow at proportional position (48% from top, inside the LPDDR/HBM hybrid Partial-moat band). Renders responsively in email clients with no PNG weight.

Segment boundaries: 85-100 HBM pure (Strongest moat), 65-85 HBM-GDDR/hybrid (Strong), 40-65 LPDDR/HBM hybrid (Partial — duopoly with Samsung), 20-40 LPDDR-led (Weak — Samsung-advantaged), 0-20 DDR commodity (No moat).

### Test summary

- Baseline tests pass: 423/423
- 1 placement test updated: `test_section_4_title_is_top_6` → `test_section_4_title_is_top_8`
- New placement test: `test_v3_4_4_indicator_7_renders_between_top5_and_dhcr`
- 38 new v3.4.4 tests across competitor + ADR + rendering
- **Total: 462 passing**

See `CLAUDE.md` locked decisions #6 and #7 for the full framework specifications. See `CHANGES.md` for the complete code surface diff.

---

## v3.4.4 — ORIGINAL PLANNING (superseded by v3.4.4 shipped above)

The text below is the pre-build planning section. **Preserved for historical record only** — the actual v3.4.4 release diverged in three substantive ways:
1. Indicator numbering corrected to DHCR=6, Competitor=7, ADR=8
2. Calibration approach switched from share-%-based to EPS-impact-based per Raul's nuance
3. 7D HBF confirmed two-sided (substitution risk + Solidigm moat extension) rather than threat-only

The original planning text follows for reference:

**Scope**: promote two existing tracked indicators from their current standalone-section treatment to full deep-dive cards in section 4, taking the brief from "Top 6 indicators — deep dive" → "**Top 8 indicators — deep dive**".

This is the next concrete release after v3.4.3, locked from the May 22 conversation with Raul referencing Part 14 of `memory_supercycle_analysis.docx`. The two indicators already exist in the framework (`Indicator 6 - Competitors` and `Indicator 7 - US ADR` sheets are already in the audit xlsx) — what's missing is their deep-dive treatment in the daily brief alongside DHCR and the 5 Sonnet-picked deep dives.

### Indicator 6 — Competitor dynamics (with NEW sub-indicator 6D — HBF)

Per Part 14 of the framework doc, this is split across three sub-indicators plus a new fourth that Raul explicitly flagged as missing:

**6A — Chinese commodity DRAM threat (CXMT, YMTC)**. Position as of Q1 2026: ~240,000 wafers/month capacity (about half of SK Hynix), 5% global DRAM market share, 4th globally behind Big Three. DDR5-8000 + LPDDR5X-10667 capability demonstrated. AMD added EXPO 1.2 support for CXMT memory (soft endorsement). Yield parity on DDR5 expected late 2026. Key trigger: any major Tier-1 PC OEM (Dell, HPE, Lenovo, ASUS, Acer) announcing CXMT DDR5 in mainstream / non-low-end product lines. Watch: CXMT IPO success on Shanghai STAR Market, yield parity reaching ~80%, pricing leak via Korea Times / ChosunBiz / DigiTimes for major customers, easing of US export controls on memory equipment. Why it matters: SK Hynix Q1 2026 71.5% operating margin is blended HBM (~80%) + commodity DRAM (~50-60%); if commodity compresses 30pp, blended drops to ~58%, EPS -25-35% even with HBM unchanged.

**6B — Samsung HBM4/HBM4E share gain at Nvidia**. Samsung was effectively shut out of HBM3E supply to Nvidia (SK Hynix ~85-90%, Samsung ~5-10%). HBM4 is different: Samsung passed Nvidia 11Gbps HBM4 qualification first (early 2026), captured ~28-30% of Vera Rubin HBM4 allocation. SK Hynix retains ~70% but lost ~15-20pp in one generation. Forward concern: HBM4E (2027-28) qualification competing now; Samsung unveiled HBM4E at GTC 2026 at 16Gbps/pin. If Samsung wins majority HBM4E, SK Hynix HBM dominance thesis breaks. Watch: Samsung HBM4 yield >65-70% (currently ~50-60%), HBM4E qualification timing at Nvidia/AMD/Google, Vera Rubin Ultra (2027) BOM allocation, Samsung HBM margin commentary (rumored ~50-55% vs SK Hynix HBM 80%+; convergence signals competitive parity), AMD HBM4 supply MOU with Samsung expansion (March 18, 2026).

**6C — Micron HBM threat (smaller, lower priority)**. 18% of 2026 global HBM4 share. Excluded from Vera Rubin allocation specifically. Less structural threat than Samsung. Watch: Micron HBM3E volume ramp, HBM4 design wins at Nvidia or AMD, HBM4E qualification commentary.

**6D — HBF (High Bandwidth Flash) leadership tracking — NEW per Raul's flag (May 22 2026)**. The shift from HBM toward HBF as a complementary or partially substituting memory technology for AI inference is an emerging risk/opportunity that the framework currently doesn't track. HBF combines NAND-like storage density with high bandwidth, potentially addressing the cost-per-token constraint at long context that pure HBM hits. **The question for SK Hynix**: can they be the leader on HBF the way they were on HBM, or will Samsung / Micron / a non-Korean entrant define the standard? Research needed at v3.4.4 build time: (a) current HBF technology landscape — who's published roadmaps, what JEDEC standardization status, who has working samples; (b) SK Hynix's specific HBF positioning vs Samsung vs Micron vs SanDisk/Kioxia; (c) which AI accelerator vendors (Nvidia/AMD/custom) have indicated interest; (d) what triggers should fire (HBF design wins, JEDEC ratification, first production HBF announcement, hyperscaler HBF deployment). This is the sub-indicator most likely to be incomplete on first build — flag at build time that this needs human input on what data sources track HBF specifically.

**Combined scenario** (per Part 14): if China commodity threat materializes (CXMT $30 DDR5 in volume) AND Samsung wins 50% of Nvidia HBM4E allocation, combined stock impact is approximately -30 to -45%; combined with cycle peak at the same time → 60-75% drawdown. The dual-threat scenario is the worst-case overlay on the cycle peak — not base case but meaningful tail risk and the reason this indicator deserves separate tracking outside the universal cycle framework.

### Indicator 7 — US ADR / SPO event tracking

Per Part 14: SK Hynix is launching US ADR listing in **June or July 2026 via NEW share issuance** (treasury share path closed Feb 9 2026 when SK Hynix canceled 12.24 trillion KRW treasury). Path is confirmed:

- **Size**: ~2.4% of total share capital, ~17.4M new shares
- **Estimated raise**: $9.6-14B depending on share price at pricing (Korean press central case $10B)
- **Dilution**: 2.4% direct
- **Use of proceeds**: M15X fab Cheongju, Yongin semiconductor cluster, Indiana fab, $8B ASML EUV order, broader HBM CapEx
- **Confirmed timeline**: June-July 2026

Primary tracking source: **SEC EDGAR F-1 filings — beats Korean press by 1-3 days**. URL: `sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix&type=F-1&dateb=&owner=include&count=40`. Set daily check from late April 2026 onward. When the public F-1 lands, it appears within minutes of submission.

**Mechanical impact (separate from cycle dynamics)**: per academic studies (Doidge-Karolyi-Stulz 2004, Sarkissian-Schill 2009), pure SPO/ADR event drag for established foreign companies is **-4 to -9% cumulative**, fading within 1-3 months as stock returns to fundamentals. Calibration reference points: Saudi Aramco SPO June 2024 ($12.4B at 2.6% discount, dropped ~3% on announcement, traded flat 30 days post); TSMC ADR 1997 (small positive bump); HDFC Bank, NTT ADRs (minimal impact).

**Calendar items to track**:
- F-1 filing date (the SPO event start — primary trigger)
- Underwriter syndicate quality (top-tier Goldman/MS/JPM lead = institutional credibility; second-tier = weaker demand signal)
- Pricing-day discount (<3% strong demand; 3-5% normal; >5% weak)
- Insider participation in offering (any disclosed insider sales = strong negative signal beyond mechanical)
- US institutional uptake (oversubscribed 2-5x = positive; below subscription = negative)

**Interaction with cycle thesis**: ADR launch lands BEFORE expected cycle peak window (Q3 2026 - Q1 2027). If June ADR: stock takes -4 to -9% mechanical drag, retraces normally over 1-3 months, cycle continues running. If July ADR + Q2 2026 earnings July 29 fires Tier 2 cycle signals: ADR drag overlaps with cycle deterioration → compounding move (unfavorable scenario). The interaction is what to watch — not the ADR alone.

### v3.4.4 implementation plan

**Section 4 ordering after v3.4.4** (assuming v3.4.3.1 is already in production):
1. Sonnet pick #1
2. Sonnet pick #2
3. Sonnet pick #3
4. Sonnet pick #4
5. Sonnet pick #5
6. Indicator 6 — Competitor dynamics (4 sub-indicators including new 6D HBF)
7. Indicator 7 — US ADR / SPO event tracking
8. DHCR — framework-level direction monitor (last, per v3.4.3.1 lock)

**Required changes**:
1. `src/config.py` — Indicator 6 add sub-indicator 6D for HBF; Indicator 7 update path to new-share-issuance only (treasury path removed)
2. `src/email_module/template.py` — section 4 template extended to render Indicator 6 + Indicator 7 deep-dive cards between `_build_top5_section` and the DHCR card (DHCR remains LAST per v3.4.3.1); section 4 title rename "Top 6" → "Top 8 indicators — deep dive"
3. `src/analysis/llm_summarizer.py` — Sonnet prompt anchor link reference updated from §4 ref of "Top 6" → "Top 8"
4. `data/inference_arch_reference.yaml` or NEW `data/hbf_tracking.yaml` — depending on Raul's preference at build time, the HBF tracking data may need its own YAML
5. NEW DHCR card visualization element — **0-100 spectrum bar** rendered alongside the existing matrix chart. Per Raul's hand-drawn sketch May 22 2026: vertical bar to the right of the matrix, 100 at top → 0 at bottom, 5 stacked color-coded segments (matching matrix palette: green → yellow → amber → red-orange), each segment annotated with `Memory type` (HBM pure / HBM-GDDR hybrid / LPDDR-HBM hybrid / LPDDR-led / DDR commodity) AND `SK Hynix moat strength` (Strongest → Strong → Partial moat → Weak → No moat) — honest about duopoly with Samsung at the high end. Today's score marked with a left-pointing arrow at the proportional position (52 → 48% from bottom). Implemented as inline HTML/CSS (stacked divs with proportional heights and inline labels), not a matplotlib PNG — responsive in email clients, no image weight.
6. New tests in `tests/test_v3_4_4_*.py` — validate section 4 renders 8 cards in the right order (top-5 → Ind6 → Ind7 → DHCR); Indicator 6/7 cards have all expected sub-content; HBF sub-indicator structure; spectrum bar renders 5 color-coded segments with correct memory-type + moat labels and the score marker positioned proportionally
7. xlsx update — Indicator 6 sheet (currently 3 sub-indicators) add sub-6D HBF; Indicator 7 sheet update path to new issuance only

**User decisions needed at v3.4.4 build time**:
- HBF data source strategy: is there a HBF-specific RSS / news feed / press release source we should add, or wait until HBF news appears in general semiconductor coverage?
- ADR calendar: do we want a dedicated calendar widget showing F-1 filing watch + pricing day + lockup expiry, or fold into the existing Calendar of Events sheet?
- Format of the dual-threat combined scenario: explicit "if BOTH 6A AND 6B fire" combined readout in the brief, or keep it as separate cards?
- Spectrum bar segment boundaries: 20 / 40 / 65 / 85 proposed (matching framework status partitions Fired/Partial/Not fired at 30 and 65, with intermediate splits for LPDDR-led vs LPDDR-HBM hybrid). Adjust if empirical data suggests otherwise.

**Estimated build time**: 1-2 days; HBF research adds ~half day depending on data source availability; spectrum bar adds ~2-3 hours including tests.

---

## v3.4.3+ candidates — Pending items carried over from original v3.4 design

These are the items from the original ROADMAP that never got built because v3.4.1 and v3.4.2 went a different direction. Still relevant; no urgency unless something changes.

### LPDDR-specific TrendForce contract scrape

Pipeline tracks DRAM + HBM but not LPDDR pricing separately. Most relevant now for the DHCR sanity check (if LPDDR pricing rolls over alongside HBM, that's substitution evidence — feeds into the calibration flag logic). Also relevant for any future LPDDR-specific Tier 2 indicator. Source: TrendForce LPDDR contract reports.

### Auto-extract closed-model KV via cost-per-token gradient analysis

GPT-5 / Claude Opus 4.x / Gemini 2.5 currently carry hand-curated `kv_kb_per_token_range` confidence bands in `inference_arch_reference.yaml` because their architecture is private. Long-context cost-per-token data from provider APIs could derive these mechanically — KV scaling shows up as a non-linear cost gradient at long context. Would reduce hand-curation burden and improve frontier KV accuracy at the closed-model upper bound.

### Artificial Analysis tokens-$ trend structured extraction

Already in the RSS pipeline as a feed (v3.2), but no structured extraction. Would feed the Y-axis as a derived efficiency metric. Specifically: parse the headline tokens/$ trends and track them as a time series — could either confirm or contradict the open-model frontier KV reads.

### HotChips / MICRO / ISCA proceedings scrape

Caught loosely via Google News today; proceedings themselves aren't fetched. Architecture-leak signals are time-sensitive (24-hour window after talks). Would improve Y-axis recency for the KV efficiency signal — proceedings often disclose architectures before papers hit arXiv.

---

## v3.4.x recalibration after data accumulation (~Q4 2026 / Q1 2027)

The v3.4.2 matrix framework set everything ex ante:
- KV band intensities (0.10 / 0.30 / 0.50 / 0.75 / 0.95)
- Context band intensities (0.10 / 0.30 / 0.55 / 0.80 / 0.95)
- Score thresholds (≥65 = Not fired, <30 = Fired, 30-65 = volume-trajectory-dependent)
- Sanity check threshold (5+ substitution news items in 90d = "evidence high")
- Anticipation premium magnitude (currently fixed +1 Y-band — could become adaptive)
- KV/context weighting (50/50 with anticipation premium baked in)

After 4-6 quarters of real DHCR data plus monthly `context_length_signals.yaml` refreshes, validate or tune. The recalibration should be empirical: if the matrix consistently flags transitions that don't translate into actual price/inventory moves, the band edges are wrong. If the matrix misses real transitions, the weighting is wrong.

---

## v3.4.x operational dependencies (from v3.4.2 framework)

These aren't features — they're ongoing maintenance commitments created by the v3.4.2 framework that the operator/agent needs to honor:

1. **Monthly `data/context_length_signals.yaml` refresh** — currently manual with procedure documented in YAML header. Review product docs (consumer chat / coding agents / enterprise vertical / dev API / other) + hyperscaler earnings transcripts. Update `typical_context_tokens` × `volume_share_pct` × confidence per category. Append `refresh_evidence` log entry.

2. **Quarterly `data/enterprise_ai_revenue.yaml` refresh** — when each hyperscaler earnings cycle completes, append new quarter to MSFT Azure AI / AWS / GCP / Oracle AI Infra / NVIDIA datacenter arrays. Drives the volume index.

3. **Irregular `data/inference_arch_reference.yaml` refresh** — when a new frontier open-architecture model ships (Llama 3.x, DeepSeek V4/V5, Qwen, Mistral, etc.), append entry. Drives the Y-axis frontier KV.

4. **Irregular `data/hbm_per_accelerator.yaml` refresh** — when a new NVIDIA/AMD accelerator generation announces (or a non-HBM accelerator wins material share), append entry.

---

## Open gaps from original ROADMAP (still open)

1. **LPDDR pricing data** — see v3.4.4+ above
2. **HotChips / MICRO / ISCA proceedings scrape** — see v3.4.4+ above
3. **Artificial Analysis / EpochAI tokens-$/cost benchmarks structured extraction** — see v3.4.4+ above

---

## Historical reference — original v3.4 design (SUPERSEDED, kept for context only)

What follows below this line is the original v3.4 plan from before the May 21 design session. **None of it is current.** The "three-indicator design" (Indicators 23/24/25) was collapsed into one composite DHCR per the mental-model reframe; the "v3.4.1+ trigger gates after 4-6 quarters" plan was preempted by the v3.4.2 matrix framework which has score-based status mapping baked in. Kept for design-evolution context.

**The mental-model reframe** (Raul's locked direction May 21, 2026):

> "I don't think there is a version where we take efficiencies and we keep constrained to small context. Think we push context as much as we can as long as economically possible."

The original ROADMAP framed the three indicators as independent measurements of HBM-erosion forces (efficiency shocks, CXL adoption, LPDDR ramp). Raul rejected that framing. The correct mechanism: efficiency gains don't displace HBM — they UNLOCK economic context capacity. Enterprise workloads then expand to consume what's economically possible. The binding question is whether enterprise demand-pull grows fast enough to fill the unlocked headroom.

So the three indicators are really three INPUTS to one underlying question, and the answer is a direction signal, not a fire/not-fire trigger. DHCR is that single composite direction monitor.

---

## Original ROADMAP design (preserved for historical reference)

Below is the original three-indicator design from before the May 21 reframe. Kept here for context on how the design evolved. **None of this is current** — the actual v3.4.0 build is the single composite DHCR described above and in CHANGES.md.

### Indicator 23 — Inference Efficiency Shock (T1, leading) — SUPERSEDED

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

> **Build-day checklist and "what to ask the user" sections from the original ROADMAP have been removed** — they described the three-indicator build (Indicators 23/24/25) that was collapsed into DHCR in v3.4.0 and then redesigned around the matrix framework in v3.4.2. The work they planned has been done (just differently than originally specified).

---

# v3.5 — Parking lot (opportunistic fixes)

**Status as of 2026-05-22**: both entries NOT STARTED. Sequenced after v3.4 calibration window. Entry 2 (event deduplication) is architecturally more central — sits underneath multiple scoring paths — and probably should go first when this batch is picked up.

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

**Status as of 2026-05-22**: NOT STARTED. Recommended priority RAISED — the framework's expected cycle peak window is Q3 2026 - Q1 2027, which is now 4-8 months out. The matrix weights `{tone: 0.4, supplier: 0.4, spot_lead: 0.2}` and the R1-R5/C1-C5 band cutoffs were specified ex ante and have never been validated against historical turns. If the matrix lags or whipsaws in backtest, better to discover it now than miss the peak. Currently 6 indicators are fired or partially fired with untested band thresholds — the more signals fire near the peak, the more matrix accuracy matters.

**Blocked on**: user decision about historical data sourcing — see "What to ask the user" section below. Decision affects build time by 2-4 hours.

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

**Status as of 2026-05-22**: NOT STARTED. Data-accumulation-gated. v3.39.7.3 shipped May 20 — only ~3 days of GHA-accumulated `state/indicator_evidence.json` diff history exist. Wait minimum 4-8 weeks before attempting tuning.

**Why**: the bootstrap categorization assigned each indicator's persistence based on a priori judgment about how its underlying data behaves (CapEx = quarterly, TurboQuant = permanent, P/E = live, LTAs = durable_contract). After 4-8 weeks of GHA runs, we'll have a track record of how often each indicator's evidence is genuinely refreshed vs preserved-by-default. Some categorizations may need correction.

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

**Status as of 2026-05-22**: NOT STARTED. Sequenced after Part 7a — schema changes are best driven by empirical findings from 7a (which categorizations need adjusting, which fields need expiry-date awareness, etc.) rather than speculatively. Build after 7a tuning is done.

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

**Status as of 2026-05-22**: RECOMMENDED FOR v3.4.3 BUNDLE. NOT data-accumulation-gated. Especially useful given the May 22 529 incidents — better diagnostics for understanding which signals get under-attested when LLM calls fail. Low risk, small change. See v3.4.3 — Quick-wins candidate section at the top of this file.

**Why**: the v3.39.7.3 prompt is long (DECISION RULES BY PERSISTENCE CATEGORY + INVALID DOWNGRADES + evidence_log block per indicator). Haiku may follow some rules better than others. Need to track which rules are being applied correctly.

**Plan**: log Layer 2's reasoning per indicator, comparing `evidence_for_change_vs_yesterday` against the evidence_log's persistence category. Specifically:
- If status preserved (Fired → Fired) AND today's evidence was empty → confirm the persistence category was the right reason (not just lazy preservation).
- If status changed (Fired → Not fired) → verify the reversal_condition was actually matched in today's evidence, not invented by Haiku.

**Output**: a weekly digest summarizing Layer 2 decisions per indicator: # of preserves vs reverses vs new fires, with sample reasoning quotes.

**Files**: probably a new script in `scripts/` consuming the GHA-committed `state/indicator_evidence.json` history via `git log`.

---

## v3.39.7.10 — ✅ SHIPPED 2026-05-21

Hotfix for v3.39.7.9 production crash. The 15:13 UTC fast-tier deploy completed the full pipeline (deep tier ingestion through Layer 3 Sonnet synthesis through supercycle chart rendering) then crashed in the email HTML rendering step with `AttributeError: 'IndicatorAssessment' object has no attribute 'indicator_id'`.

**Cause**: v3.39.7.9 Fix #7b gated snippet injection on `a.indicator_id == "t3_relative_strength"` but the canonical field name is `a.id` (per `IndicatorAssessment` dataclass at `src/analysis/llm_summarizer.py:58`). The typo escaped the v3.39.7.9 test suite because the 9 tests in `TestSection3RelativeStrengthSnippet` covered the helper `_build_relative_strength_snippet` in isolation but never exercised the integration in `_build_indicators_section` against a real `IndicatorAssessment` object.

**Fix**: one-line change `a.indicator_id` → `a.id` plus 13 regression tests in `tests/test_v3_39_7_10_section3_attribute_crash.py` across 4 classes:
- `TestIndicatorAssessmentFields` (3 tests) — structural pinning via `dataclasses.fields()` enumeration: explicitly asserts `id` IS a field AND `indicator_id` is NOT a field AND the full field set matches expectations
- `TestBuildIndicatorsSectionRunsWithoutError` (4 tests) — exercises `_build_indicators_section` against real `IndicatorAssessment` objects for t3, non-t3, None verdict, mixed-tier
- `TestSnippetActuallyInjects` (4 tests) — verifies snippet text appears in HTML for t3 rows, doesn't appear for non-t3, renders correct numeric values, uses Fired color for ≤ -10pp
- `TestEmptyAssessmentList` (2 tests) — edge case coverage

Bug-catching validation: temporarily re-introduced the v3.39.7.9 typo and confirmed 8 of 13 v3.39.7.10 tests failed with the production AttributeError. Reverting the fix made them all pass — proving the regression coverage actually catches the bug.

325 tests pass (312 from v3.39.7.9 + 13 new).

Files: `src/email_module/template.py` (1-line fix + explanatory comment), `tests/test_v3_39_7_10_section3_attribute_crash.py` (NEW), `CHANGES.md`, `CLAUDE.md`, `ROADMAP.md`, `UPLOAD_GUIDE.md` (release notes). xlsx renamed `_v3_39_7_10.xlsx` (no content change).

---

## v3.39.7.9 — ✅ SHIPPED 2026-05-21

Six-fix release diagnosed from the May 21 brief:

1. **Capex/OCF authority** — signal_reasoner.py no longer passes capex_revenue_ratios; evidence store reversal_condition updated; INVALID DOWNGRADES section explicitly tells Layer 2 the rule is capex/OCF >50%. Pre-v3.39.7.9 Layer 2 was using capex/revenue (24-37%) to override capex/OCF (93.2%).
2. **2-quarter rolling capex/OCF mean per company** — replaces single-quarter snaps[0]. Smooths the Amazon Q1 169.8% OCF-dip noise without losing the cycle signal. New keys: mean_capex_ocf_ratio_2q + per_company_capex_ocf_2q.
3. **"Reversed" status removed** — three states only (Fired/Partially fired/Not fired). Audit trail lives in change_vs_prior and the trigger banner.
4. **Color scheme** — Fired darkened from #D85A30 to #B91C1C; change-flag color matches destination status (red on fires, green on un-fires, yellow on partial transitions).
5. **QUIET state explanation** — no fix needed; semantics documented in CHANGES.md (source reached + 0 items + expected_empty flag = healthy informational state for ~35 architectural watchers + 5 SK Hynix pre-listing EDGAR feeds).
6. **Value-chain inference** — Sonnet exec brief prompt gains a VALUE-CHAIN INFERENCE section instructing Sonnet to draw causal chains across adjacent signals (HBM capacity → DDR5 spot; hyperscaler capex → NVIDIA backlog → SK Hynix revenue; CXMT capacity → spot pressure; Cerebras → HBM substitution risk).
7. **NaN-guarded relative strength** — get_relative_strength_vs_sox returns None on NaN; defensive math.isnan check in llm_summarizer COMPUTED METRICS render. No more "+nanpp" leakage.
8. **Section 3 numeric surfacing for t3_relative_strength** — new _build_relative_strength_snippet() helper renders SK Hynix / Micron / DRAM-ETF 5d-vs-SOX as color-coded badges with threshold legend. The number is visible at the section 3 summary, not just section 4 deep dive.

312 tests pass (273 from v3.39.7.8 baseline + 39 new in `tests/test_v3_39_7_9_six_fixes.py`).

Files: src/data/estimates.py, src/data/prices.py, src/fast_tier.py, src/analysis/signal_reasoner.py, src/analysis/llm_summarizer.py, src/email_module/template.py, state/indicator_evidence.json, tests/test_v3_39_6_calculation_driven.py (existing test updated for new keys), tests/test_v3_39_7_9_six_fixes.py (NEW).

---

## v3.39.7.9 — Follow-up watch items (post-shipping)

**Sonnet drawing value-chain inferences**: monitor the first 1-2 briefs post-deploy to see if Sonnet actually draws the causal chains when adjacent signals appear. If it doesn't, the prompt examples may be too abstract — strengthen with concrete sentence templates ("X implies Y in T months because Z mechanism").

**Section 3 t3_relative_strength snippet rendering on mobile**: the three-badge layout is designed for desktop email width. On narrow mobile, badges may wrap. If wrapping breaks visual flow, switch to stacked vertical layout for narrow viewports via CSS media query.

**capex/OCF 2-quarter rolling — re-evaluate responsiveness**: the v3.39.7.4 design choice was "responsiveness over smoothness". v3.39.7.9 reverses that. Watch for the OPPOSITE failure mode — an indicator that should have fired but didn't because the previous quarter was below threshold and dragged the 2q mean down. If observed, may need a hybrid: 2q mean for primary trigger, single-quarter spike override at >100% for any company.

**Color contrast on the Fired badge**: #B91C1C with white text should be readable, but if user feedback says otherwise, consider switching to #DC2626 (slightly lighter red) or testing #991B1B (darker still). The Fired/Not fired/Partially fired triad of colors works as a system — any change should be evaluated together.

**Cerebras WSE-3 substitution-risk capture**: the value-chain inference prompt explicitly mentions Cerebras as an HBM-substitution-risk signal. Monitor whether Cerebras news (when it appears in section 5) actually triggers the captive-ratio inference language Sonnet produces. If not, the v3.4 captive-ratio indicator work may need to formalize this as a first-class indicator rather than relying on Sonnet's inference.

**Status pill semantic for indicators with NO change today**: currently the brief shows the status pill (Fired/Partially fired/Not fired) for every indicator regardless of whether it changed today. The change_vs_prior CHANGED flag highlights transitions. If you want a clearer "today's brief had no changes for this indicator" indicator, we'd need to add a fourth visual treatment (e.g., a small dot or "unchanged" tag). Defer until you have a brief where the change-vs-static distinction matters.

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
