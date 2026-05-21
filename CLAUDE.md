# CLAUDE.md — Context for Claude working on this repo

<!--
CHANGELOG (this file):
  v3.39.7.10 (2026-05-21) — Hotfix for v3.39.7.9 AttributeError. The 15:13 UTC fast-tier deploy ran the full pipeline cleanly (deep tier ingestion → news filter → Layer 2 across 22 indicators → cycle matrix → Layer 3 Sonnet → trigger event log → supercycle chart) and crashed in the FINAL step (email HTML rendering) with `AttributeError: 'IndicatorAssessment' object has no attribute 'indicator_id'` at src/email_module/template.py:637. v3.39.7.9 Fix #7b (section 3 numeric surfacing) gated snippet injection on `a.indicator_id == "t3_relative_strength"` but the canonical attribute is `a.id` (per the @dataclass IndicatorAssessment definition at llm_summarizer.py:58). One-line fix: `a.indicator_id` → `a.id`. The v3.39.7.9 test suite covered _build_relative_strength_snippet in isolation (9 tests in TestSection3RelativeStrengthSnippet) but never exercised the integration into _build_indicators_section against a real IndicatorAssessment object — the branch with the typo was unreachable from any test path. v3.39.7.10 adds 13 regression tests in tests/test_v3_39_7_10_section3_attribute_crash.py covering: (1) STRUCTURAL pinning via dataclasses.fields() — explicitly asserts `id` IS a field AND `indicator_id` is NOT a field AND the full field set matches v3.39.7.10 expectations (any future addition is intentional); (2) CRASH-REPRODUCTION integration — calls _build_indicators_section with real IndicatorAssessment objects for t3 + non-t3 + None verdict + mixed-tier mix; would have failed with the v3.39.7.9 typo; (3) SNIPPET CORRECTNESS — verifies snippet text appears in rendered HTML for t3 rows, doesn't appear for non-t3 rows, renders correct numeric values (-7.42pp etc), uses Fired color for ≤ -10pp band; (4) EDGE CASES — empty list with and without verdict. Bug-catching validation: temporarily re-introduced the v3.39.7.9 typo and confirmed 8 of 13 v3.39.7.10 tests failed with the production AttributeError; reverting the fix made them all pass. 325 tests pass (312 from v3.39.7.9 + 13 new). xlsx renamed memory_cycle_data_source_audit_v3_39_7_10.xlsx (no content change). LESSON for future Claude: when adding render-path gates on dataclass fields (e.g. `if a.<field> == "..."`), VERIFY the attribute name against `dataclasses.fields()` before writing assertions in tests. Unit tests on helpers in isolation are necessary but not sufficient — there must be at least one integration test exercising the helper INSIDE its caller against the real input shape it'll see in production. This is a recurring failure mode and the structural TestIndicatorAssessmentFields test is the cheap mitigation.
  v3.39.7.9 (2026-05-21) — Six-fix release diagnosed from the May 21 brief showing t2_hyperscaler_capex_revenue flipping to Reversed against the COMPUTED METRICS, t3_relative_strength rendering as "+nanpp", "Reversed" appearing as a 4th status pill, change-flag highlights always yellow regardless of destination, and exec summary not drawing the SK Hynix HBM capacity → DDR5 spot causal chain. Fix #1 (capex/OCF authority): signal_reasoner.py no longer passes capex_revenue_ratios in signal_specific (was being used by Layer 2 to override the actual capex/OCF rule via the stale v3.39.7.4 reversal_condition in evidence store), reversal_condition in state/indicator_evidence.json updated to cite capex/OCF 2q-rolling, INVALID DOWNGRADES section in REASONER_SYSTEM_PROMPT gets a new entry explicitly forbidding capex/revenue reversal of capex/OCF verdicts. Fix #2 (2-quarter rolling mean): estimates.py + fast_tier.py compute per-company 2q rolling capex/OCF mean (snaps[:2]) BEFORE cross-company averaging, smoothing single-quarter OCF dips like Amazon Q1 2026 OCF $26.03B vs Q4 2025 $54.46B that made AMZN look like 169.8% in isolation. Key renames: mean_capex_ocf_ratio_latest_q → mean_capex_ocf_ratio_2q; n_companies_with_capex_ocf_latest_q → n_companies_with_2q_capex_ocf; new per_company_capex_ocf_2q dict so Layer 2 can identify driver companies. Fix #3 ("Reversed" status removed): collapsed from 4 states (Fired/Not fired/Partially fired/Reversed) to 3 (Fired/Not fired/Partially fired). Pre-v3.39.7.9 "Reversed" was a 4th status preserving audit trail ("was Fired, now reversed"); user wanted 3-state UX. Audit trail moves to evidence_for_change_vs_yesterday + trigger event banner. Touched fired_status enum comment in signal_reasoner.py, transition rule ("From Fired → set to 'Not fired'"), JSON schema in REASONER_USER_TEMPLATE, IndicatorAssessment.current_status enum in llm_summarizer.py, output JSON schema in build_sonnet_analysis_prompt, STATUS_COLORS dict in email_module/template.py. Fix #4 (color scheme): Fired darkened from #D85A30 (red-orange that read as amber in the May 21 screenshot) to #B91C1C (clearer red); change-flag inline style now uses dynamic {status_color} interpolation matching destination status rather than hardcoded #FFEBA1 yellow — red highlight on new fires, green on un-fires, yellow only for partial-fire transitions; text color flips to white on red background for readability. Fix #5 (QUIET explanation): no code change — semantics documented in CHANGES.md (source reached + 0 items + expected_empty=True flag = healthy informational state for ~35 architectural Google News watchers like CXL 3.0/GDDR7/MTIA/HBF/OpenAI IPO/Anthropic IPO + 5 SK Hynix pre-listing SEC EDGAR feeds that stay empty most days and fire only when their specific event happens). Fix #6 (value-chain inference): new VALUE-CHAIN INFERENCE section added to Sonnet executive brief specification prompt instructing Sonnet to draw explicit causal chains when adjacent signals appear in the same brief — three pattern categories (CAPACITY-MIX SHIFTS like HBM capacity → DDR5 spot tightening; DEMAND CASCADES like hyperscaler capex → chip designer backlog → memory revenue; COMPETITIVE/SUBSTITUTION like CXMT capacity → spot pressure or Cerebras WSE-3 → HBM substitution risk) with explicit "X implies Y because Z" template + LIMITS clause requiring both ends of the chain to have brief or ≤7d carried-forward evidence. Fix #7a (NaN guard): get_relative_strength_vs_sox in prices.py now explicitly checks pd.isna() on the four price cells (ticker_end, ticker_start, sox_end, sox_start) before division and on the final returns; returns None on NaN instead of letting NaN propagate. Pre-v3.39.7.9 NaN values bypassed `is not None` checks because NaN IS a valid float, then format string {value:+.2f}pp produced "+nanpp" in the brief. Also defensive math.isnan check added to llm_summarizer COMPUTED METRICS render block (belt + braces). Fix #7b (section 3 numeric surfacing): new _build_relative_strength_snippet() helper in email_module/template.py renders SK Hynix / Micron / DRAM-ETF 5d-vs-SOX as three color-coded inline badges (red ≤ -10pp, yellow -10 to -5pp, green > -5pp, grey for not-computed) with a small thresholds legend; the snippet is prepended to the t3_relative_strength row's "Where we stand" cell in section 3 so the user sees the actual numeric value at a glance instead of having to scroll to section 4 deep dive. Per user direction 2026-05-21: "i expect to see a number as a number is what will trigger a warn (partially fired) or fired". build_email_html signature already accepts rel_strength_verdict; _build_indicators_section gained a new optional rel_strength_verdict kwarg threading it through. Other indicator rows unchanged. Test coverage: 39 new tests in tests/test_v3_39_7_9_six_fixes.py across 8 classes (TestCapexOcfAuthority x4, TestTwoQuarterRollingMean x4, TestReversedStatusRemoved x5, TestColorScheme x5, TestValueChainInferencePrompt x5, TestRelativeStrengthNaNGuard x5, TestSection3RelativeStrengthSnippet x9, TestLlmSummarizerNaNGuard x2); existing test test_capex_revenue_ratios_render_latest_quarter in test_v3_39_6_calculation_driven.py updated to use new 2q keys + assert capex/revenue is not in the prompt. 312 tests pass (273 prior + 39 new). xlsx renamed memory_cycle_data_source_audit_v3_39_7_9.xlsx (no content change — no indicator config changed).
  v3.39.7.8 (2026-05-21) — Four-tier value-chain restructure of TRACKED_COMPANIES per user direction 2026-05-21. Pre-v3.39.7.8 (v3.39.7.7) had three tiers conflating chip designers (NVIDIA, Broadcom — companies that BUY memory chips) with ecosystem peers (ASML, TSMC, OEMs) in the same TRACKED_REST bucket. v3.39.7.8 splits into: Tier A — Pure memory + rivals (SK Hynix, Hynix, Micron, " MU ", Samsung, Samsung Electronics, CXMT, ChangXin) — CXMT/ChangXin moved up from old Tier C because they're competitive RIVALS in the same product market not just ecosystem peers; Tier B — Chip designers / first-layer bill-payers (NVIDIA, Nvidia, NVDA, AMD, Advanced Micro Devices, Intel, INTC, Broadcom, AVGO, Cerebras, CBRS) — AMD/Intel/Cerebras NEW additions, completely absent from all config surfaces pre-v3.39.7.8; Tier C — Hyperscalers + AI labs / second-layer bill-payers (Microsoft, MSFT, Azure, Alphabet, Google, GOOGL, Amazon, AMZN, " AWS ", Meta, Facebook, OpenAI, Open AI, Anthropic); Tier D — Ecosystem (ASML, Applied Materials, AMAT, TSMC, TSM, Oracle, ORCL, Dell, " HPE "). TRACKED_COMPANIES symbol preserved as union of the four tier lists for back-compat with fast_tier.py:468 call site (unchanged). Cerebras IPO'd 2026-05-14 on Nasdaq (CBRS), $185 IPO price, +68% first day close at $331, market cap ~$95B; $10B contract with OpenAI for 750MW AI compute announced January 2026. Cerebras WSE-3 uses 44 GB on-chip SRAM (NOT HBM) — making Cerebras especially relevant as an HBM-SUBSTITUTION-RISK signal directly informing the planned v3.4 captive-ratio indicator work; the Pass 2 rubric calls this out explicitly so Haiku scores Cerebras news appropriately rather than as generic peer commentary. AMD/Intel/Cerebras added to WIDER_BASKET (price table coverage in section 1), MOTLEY_FOOL_TICKERS (transcript primary source), YAHOO_TRANSCRIPT_TICKERS (transcript fallback). Cerebras has only ~1 week of trading history so the section 1 1Y/3M/6M/1M columns will display "—" until enough days accumulate. Pass 2 Haiku rubric in src/data/news.py score_relevance rewritten with the 4-tier structure: Tier A 9-10 direct + 7-8 thematic (now includes CXMT capacity-expansion as 9-10 input); Tier B 8-9 chip designer earnings + HBM/data-center backlog commentary + 6-7 general; Tier C 8-9 capex commitments + 6-7 general earnings; Tier D 6-7 ecosystem with memory implications + 4-5 general. New prose making explicit that Tier B is the CONCURRENT HBM demand signal (chip designers tell us what they ARE buying today) while Tier C is the LEADING capex commitment signal (6-12 months ahead of memory cycle peak) — both peak at 8-9 but for different reasons. Files: src/config.py + src/data/news.py + NEW tests/test_v3_39_7_8_four_tier_restructure.py (73 tests across 9 classes: TestFourTierStructure x3, TestTierAMembership x5, TestTierBMembership x6, TestTierCMembership x6, TestTierDMembership x5, TestChipDesignerExpansion x10 covering AMD/INTC/CBRS in all 4 surfaces, TestPass1Acceptance x22 incl. per-company headline tests, TestPass1FalsePositives x9 incl. boundary fix preservation, TestPass2RubricStructure x7 covering 4-tier rubric + CXMT-in-A + chip-designers-in-B + concurrent-vs-leading framing + Cerebras-as-HBM-substitution); DELETED tests/test_v3_39_7_7_tracked_companies.py (41 tests for the obsolete 3-tier structure superseded by the new 73-test file at 4-tier granularity). 273 tests pass (200 from v3.39.7.6 baseline + 73 new for v3.39.7.8; net +32 vs v3.39.7.7's 241). HYPERSCALERS dict (used by t2_hyperscaler_capex_revenue indicator capex/OCF computation) UNCHANGED — AMD/Intel/Cerebras are chip designers not hyperscalers, capex math doesn't apply. xlsx renamed memory_cycle_data_source_audit_v3_39_7_8.xlsx (no content changes — no indicator config changed). Recovery moment: Claude initially flagged Cerebras as "unauthorized addition" in a pre-populated bundle and was about to strip it, then web-searched and confirmed (a) Cerebras DID IPO 2026-05-14 (CBRS at $185, +68% close) and (b) user explicitly wanted Cerebras added after Claude's strip warning. Lesson: when surprised by unfamiliar additions in unexpected places, search before stripping; the additions may be factually correct and intent-aligned. Especially relevant for post-cutoff events like the May 14 IPO that Claude's training data wouldn't include.
  v3.39.7.7 (2026-05-21) — Tracked-company expansion closes the news-pipeline blind spot where direct headlines about NVIDIA / hyperscalers (MSFT, GOOGL, AMZN, META) / AI labs (OpenAI, Anthropic) were silently filtered at Pass 1 because none of these names were in TRACKED_COMPANIES. Pre-v3.39.7.7 TRACKED_COMPANIES was a flat 7-entity list of memory pure-plays + equipment + competitors only; zero customers. May 21 2026 brief surfaced this when NVIDIA Q1 FY27 earnings + Samsung-strike-called-off stories dominated semis news and direct CNBC/Reuters/Bloomberg NVIDIA coverage dropped from the fast tier (Sonnet only learned about NVIDIA earnings via the deep-tier transcript digest at section 4, never via the news pipeline at section 5). Per user direction (2026-05-21): "Sk Hynix/Pure Memory (Micron, Samsung Electronics), Key Customers (Labs like Open Ai and Anthropic, Amazon, Meta, Google, Microsoft) and then the rest". Implemented as three explicit tier lists in src/config.py: TRACKED_PURE_MEMORY (SK Hynix, Hynix, Micron, " MU ", Samsung, Samsung Electronics) + TRACKED_KEY_CUSTOMERS (OpenAI, Open AI, Anthropic, Microsoft, MSFT, Azure, Alphabet, Google, GOOGL, Amazon, AMZN, " AWS ", Meta, Facebook) + TRACKED_REST (NVIDIA, Nvidia, NVDA, Broadcom, AVGO, TSMC, TSM, ASML, Applied Materials, AMAT, Oracle, ORCL, Dell, " HPE ", CXMT, ChangXin). TRACKED_COMPANIES derived as union for back-compat with single call site at fast_tier.py:468 (unchanged). NVIDIA sits in Tier C despite being SK Hynix's largest HBM customer in dollar terms — framework treats them as a peer/rival; ultimate bill-payers are the hyperscalers/AI labs who buy NVIDIA GPUs. Padding rationale: " MU "/" AWS "/" HPE " padded to avoid "museum"/"lawsuit"/"shape" false positives; "Meta" intentionally unpadded (recall-vs-precision favored recall — false positives on "metadata"/"metaphor" cost one Haiku Pass 2 call that scores 0-3 and drops). Pass 2 Haiku rubric in src/data/news.py score_relevance rewritten with explicit three-tier scoring bands: Tier A 9-10 direct + 7-8 thematic; Tier B 8-9 capex/infrastructure + 6-7 general earnings; Tier C 7-8 NVIDIA earnings + Broadcom AI + TSMC CoWoS + OEM inventory + 5-6 general equipment; architectural shifts 8-10 first-of-kind / 6-7 papers + frameworks; adjacent 4-5; not relevant 0-3. INCIDENTAL FIX (latent bug since v3.1): Pass 1 boundary matching — pre-v3.39.7.7 text was built unpadded as f"{title} {summary}".lower() so the padded " MU " keyword failed to match "MU files Q3" at start of headline or "bullish on MU" at end. Fixed by padding the text itself: f" {title} {summary} ".lower(). Can only add matches at boundaries, can't drop existing mid-sentence matches. Files: src/config.py + src/data/news.py + NEW tests/test_v3_39_7_7_tracked_companies.py (41 tests across 4 classes: TestConfigStructure x6, TestPass1Acceptance x18, TestPass1FalsePositives x11 incl. boundary fix, TestPass2RubricStructure x6). 241 tests pass (200 prior + 41 new). xlsx renamed memory_cycle_data_source_audit_v3_39_7_7.xlsx (no content changes — no indicator config changed). Sloppy moment to record: Claude initially wrote "deep tier transcript diagnostics are not bubbled to fast tier section 7" in the pre-coding analysis — WRONG; _surface_transcript_status() in fast_tier.py:928 reads state/transcript_status.json and emits per-(company, source) rows, shipping since v3.38. User's May 21 brief screenshot of section 7 (rows like "Transcript: Nvidia (Yahoo Finance) OK Cached from prior run — digest available, no re-fetch needed") was the evidence. Claude corrected analysis before coding.
  v3.39.7.6 (2026-05-21) — Four-issue release addressing May 21 brief feedback. (1) Exec summary duplicate block: v3.39.7.5 was supposed to consolidate the legacy delta_from_prior_text italic block + big_movers + market_summary into a single executive_brief paragraph, but the delta_line render survived the cleanup in src/email_module/template.py:_build_executive_summary's return statement — both blocks rendered side-by-side ("Indicator distribution unchanged..." italic AND "No indicator status changes..." gray box). Pure copy-paste oversight. Fixed by removing the delta_line construction (5 lines) and removing {delta_line} from the return concatenation (1 line). delta_from_prior_text field STAYS on ExecutiveSummary dataclass for prior_state.json back-compat, same pattern as URGENT fields retention. .delta-text CSS class left in stylesheet harmlessly. (2) BENCHMARKS regression repair: fast_tier.py:364 was calling fetch_price_history(WIDER_BASKET, ...) at some unrecorded refactor point, leaving BENCHMARKS (^SOX, ^GSPC, ^KS11, DRAM ETF, SMH) unfetched. get_relative_strength_vs_sox() and get_dram_etf_vs_smh() both returned None because "MSCI ACWI Semiconductor" / "Roundhill DRAM ETF" / "VanEck Semi ETF (SMH)" columns were absent. t3_relative_strength silently rendered "(not computed)" for SOX spreads across an unknown number of releases. Smoking gun: BENCHMARKS imported at fast_tier.py:56 but never used = dead import = classic refactor residue. Fixed by swapping the dead import for ALL_TICKERS and changing the prices fetch call to fetch_price_history(ALL_TICKERS, ...). calculate_performance(price_history, WIDER_BASKET) at the section-1 table call site is INTENTIONALLY UNCHANGED — keeps showing portfolio names only, not benchmark indices. KRX staleness repair from v3.39.7.5 unaffected (.KS/.KQ filter keeps it off ^KS11). (3) RECENCY ON CONTRADICTING EVENTS prompt rule + timestamp threading: May 21 brief wrote "Samsung 45,000-worker 18-day strike commencing May 21 tightens HBM supply" when Korea Herald 10742322 (published 2026-05-20 14:17 UTC, well within fetch window) reported the strike was called off one hour before launch. Sonnet had both items in context but two compounding gaps prevented recency resolution: (gap A) prompt had no rule telling Sonnet to prefer the later timestamp on contradicting items in the same event cluster; (gap B) news triage block format dropped published timestamps — Sonnet literally could not tell which item was later. Fixed both: new RECENCY ON CONTRADICTING EVENTS block in Sonnet prompt under EXECUTIVE BRIEF SPECIFICATION with five operational rules (cluster by company+event window; latest timestamp = operative state; reversals must be explicitly noted; reversal is itself news; rule applies to executive_brief AND key_news_summary AND per-indicator commentary) and the May 21 Samsung-strike-called-off case used as the prompt's worked example. build_sonnet_analysis_prompt gains optional news_items: list[NewsItem] parameter — builds URL→timestamp lookup (with title-match fallback) and stamps each triage item as "[{source} | published {YYYY-MM-DD HH:MM UTC}] {title}". sonnet_analyze + analyze_with_llm gain matching news_items parameter for the call chain. Per user direction (2026-05-21): "this is reasoning issue — I don't want to hard code something for hard coding". Auto-keep-at-score-10 for labor news (alternative considered) was explicitly NOT taken; the fix is prompt+data, not keyword hardcoding. (4) Diagnostics 5th health state 'quiet' for expected-empty watchers: the data source assessment table treated all reached+0-item feeds as 'warn', conflating "feed might be broken" with "this watcher is supposed to be quiet most days". ~35 architectural Google News watchers (CXL 3.0 production, GDDR7 inference accelerator, MTIA Meta memory architecture, HBF, OpenAI IPO filing, etc.) and 5 SK Hynix pre-listing SEC EDGAR feeds (F-1, F-1/A, 424, 6-K, 20-F) are ALL expected to be empty until specific events happen. Calling them WARN trained the reader to ignore the WARN column, masking real WARN signal. Fixed additively: new expected_empty: bool = False field on SourceFetchStatus (default False = prior behavior preserved); new 'quiet' health state when reached=True AND items_returned=0 AND expected_empty=True; new WATCH_GOOGLE_QUERIES set + EXPECTED_EMPTY_EDGAR_FEEDS set in config.py enumerate the speculative event detectors; fetch_google_news / fetch_edgar_feeds / fetch_all_news thread the sets through; template renders QUIET as muted gray pill (vs WARN amber); sort order quiet sits below ok above skipped; legend prose under section 7 explains QUIET vs WARN distinction. Persisted v3.39.7.5-format statuses load via dataclass default (back-compat). Live news / analyst / labor queries NOT in WATCH_GOOGLE_QUERIES; Micron Form 4 NOT in EXPECTED_EMPTY_EDGAR_FEEDS. Files: src/email_module/template.py + src/fast_tier.py + src/analysis/llm_summarizer.py + src/diagnostics.py + src/config.py + src/data/news.py + NEW tests/test_v3_39_7_6_fixes.py (20 tests across all 4 issues). 200 tests pass (180 prior + 20 new). xlsx renamed memory_cycle_data_source_audit_v3_39_7_6.xlsx (no content changes — no indicator config changed).
  v3.39.7.5 (2026-05-20) — Five-issue release addressing May 20 brief feedback. (1) KRX staleness repair: yfinance returned SK Hynix at yesterday's -5.16% close while Yahoo's own page showed today's flat (1,745,000 KRW). Symptom-specific to SK Hynix (Samsung KS came back fresh same run). Three-layer defensive fix in src/data/prices.py: _latest_expected_krx_session_date() walks back from now − 8h UTC (KRX 06:30 UTC close + 1.5h ingestion buffer) through weekends; _fetch_krx_quote_from_fmp() hits FMP `/api/v3/quote/{symbol}` using existing FMP_API_KEY (confirmed FMP has 000660.KS); _repair_stale_krx_tickers() checks each .KS/.KQ ticker's latest bar after batch yfinance call, falls back FMP → Naver if stale; new fetch_quote_for_ticker() in src/data/naver_finance.py scrapes finance.naver.com server-rendered HTML. (2) Cycle matrix card moved section 3 → 4: was inline in `_build_indicators_section` under t1_spot_decline summary row; now in `_build_top5_section` under t1_spot_decline deep-dive write-up (matrix+chart are deep-dive material). (3) Exec summary restructure: killed URGENT banner (urgent_attention_needed always False post-parse), removed _build_big_movers + market_summary "Today's market" prose renders. New single executive_brief field on ExecutiveSummary dataclass: Sonnet writes ONE consolidated 5-8 sentence paragraph covering (i) signal fires/unfires + drivers, (ii) big news with directional-signal prioritization per AsiaEconomy "Sell SK Hynix" example, (iii) big movers, (iv) macro/AI/semi. Anchor link convention: Sonnet emits literal `[→§N]` token; template substitutes via regex to `<a href='#section-N' class='anchor-link'>§N</a>`. All 7 numbered h2 headers carry `id="section-N"` for the targets. New CSS .exec-brief + .anchor-link. (4) 24h ephemeral trigger event window: new state/trigger_events_24h.json. Three helpers in state.py (load auto-prunes >24h, save persists, update merges new transitions with dedup key (indicator_id, new_status) preserving original fired_at_iso). fast_tier.py builds prior_status_by_id from prior_state.json then calls update_trigger_log_24h after analyze_with_llm; overrides exec_summary.new_triggers_today with log-derived list. Multiple manual reruns same day keep showing same events (key user requirement); tomorrow they auto-prune naturally. GHA workflow stages trigger_events_24h.json so log persists across cron runs. (4b) Korea Herald RSS repaired: dead /rss/020000000000.xml replaced with kh_Business + kh_National (probed live 2026-05-20). Five new GOOGLE_NEWS_QUERIES for Korean labor news. Files: src/data/prices.py + naver_finance.py + fast_tier.py + email_module/template.py + analysis/llm_summarizer.py + state.py + config.py + .github/workflows/fast_tier.yml + NEW tests/test_trigger_log_24h.py (11 tests) + NEW tests/test_executive_brief.py (9 tests). 180 tests pass (160 prior + 20 new). xlsx renamed memory_cycle_data_source_audit_v3_39_7_5.xlsx (no content changes — no indicator config changed in this release).
  v3.39.7.4 (2026-05-20) — BATCH 1 + BATCH 2 SHIPPED. Two-batch correction following the May 20 production diagnostic. **BATCH 1** (4 surgical fixes): (1a) Cycle matrix NameError root cause: `if __name__ == "__main__":` block was at line 935 of src/fast_tier.py with the cycle-matrix helpers AFTER it at lines 943-1234; Python sys.exit'd before reaching the defs, NameError on `_run_cycle_matrix_step` call from run_fast_tier. Moved __main__ block to end of file (line 1236), all helpers now defined first. (1b) Sonnet's `tier_status_summary` prose count diverged from body card statuses (May 20 16:00 brief: body 4 T1 Fired badges, exec said T1:3/6). New _compute_tier_status_summary helper in llm_summarizer.py recounts from indicator_assessments post-parse and overrides Sonnet's string; Partially fired counts toward tier total with `(N partial)` annotation. (1c) Idempotency check defeated by checkout race: with 14-min cron spacing and ~25-min runs, cron N's actions/checkout sometimes resolved origin/main BEFORE cron N-1's push had propagated. New _refresh_from_origin_main() helper in fast_tier.py does explicit `git fetch origin main` + `git reset --hard` if behind, before reading brief_sent_dates.json. (1d) Cron schedule thinned 7→4 at 30-min spacing (11:13/11:43/12:13/12:43 UTC) so each run completes before next fires; commit/push step hardened with `set -euo pipefail` + explicit retry + loud `::error::` annotations so silent push failures stop being silent. **BATCH 2** (bear case + capex/OCF): (2a) Bear case calibrated to historical SK Hynix peak-to-trough drawdowns (avg -43% across 2014/-36%, 2018/-43%, 2022/-49%). New BEAR CASE MAGNITUDE CALIBRATION section D added to Sonnet prompt with magnitude bands keyed to fired-indicator regime (0-1 T1 + 0-2 T2 → -10 to -20%; 4+ T1 → -35 to -45%; any T3 → -40 to -50%; etc). Anchoring instruction: cite historical reference in delta_from_prior_text when bear crosses a band. (2b) Capex/OCF metric rebuild — capex/revenue >40% trigger never fired across 3 historical memory crashes (the 4 actual hyperscalers stay 5-27% range; only ORCL crossed 40%). Drop ORCL (structurally different). Capex/OCF cleanly separates: mean >40% in all 3 crash years, <40% in non-crash years, currently 48% (2024) → 62% (2025 TTM). Last published quarter (NOT TTM per user 2026-05-20). Mean across 4 hyperscalers: >40% Partially fired, >50% Fired. Severe single-co strain flag: any co. capex/OCF >100%. Files: src/data/estimates.py (HyperscalerCapexSnapshot gains ocf_usd_b + capex_ocf_ratio; _fetch_hyperscaler_capex_one extracts OCF from quarterly_cashflow; fetch_all_hyperscaler_capex computes mean_capex_ocf_ratio_latest_q from snapshots[0]), src/config.py (ORCL dropped from HYPERSCALERS, t2_hyperscaler_capex_revenue indicator config rewritten, ID kept stable for prior_state compat), src/fast_tier.py (computed_metrics carries both ratios + aggregate), src/analysis/llm_summarizer.py (COMPUTED METRICS prompt block leads with capex/OCF, cites historical reference; quiet bug fix: latest-quarter selection switched from quarters[-1] to quarters[0] to match production sort order), src/analysis/signal_reasoner.py (Layer 2 sees aggregate mean), tests/test_v3_39_6_calculation_driven.py (capex/OCF test updated). 160 tests pass (150 prior + 10 new from 1b).
  v3.39.7.3 (2026-05-20) — PERSISTENT EVIDENCE STORE for memory/data preservation. Diagnosis (vs May 9 framework doc + May 18/May 20 briefs side-by-side): 9 indicators inappropriately downgraded since May 9, all due to Layer 2 (Haiku) having no institutional memory of WHY each indicator fired. User's framing: "CapEx discussions are likely not going to happen until next quarterly earnings. The fact that we don't hear about this over the next three months doesn't make it go away." Fix: new NEW file `state/indicator_evidence.json` (bootstrap from May 9 doc; 10 indicators pre-fired) records per-indicator current_status + fired_since + evidence_log (each entry: date + type + value + source + persistence + reversal_condition) + next_check_due. Five persistence categories handle different memory characteristics: `permanent` (historical events like TurboQuant — never unfire), `quarterly` (refreshes at next earnings/forecast — capex, IDC, TrendForce inventory), `durable_contract` (LTAs valid until expiry), `live` (recomputed each run — forward P/E, rel_strength, cycle matrix), `event_window` (decays after 35d). REASONER_SYSTEM_PROMPT rewritten with DECISION RULES BY PERSISTENCE CATEGORY + explicit INVALID DOWNGRADES section calling out the v3.39.7 bugs by name ("No new capex announcements in today's news feed does NOT unfire t2_capex_parabolic"). After Layer 2, status transitions and new evidence are appended via update_evidence_after_layer2 + save_indicator_evidence_store; GHA workflow commits state/indicator_evidence.json so tomorrow's run sees today's decisions. Also: t1_spot_decline display name → "DRAM Price Evolution (spot + contract matrix)" per user direction (encompasses spot AND contract — matrix R1/C2 today means Not fired despite May 9 DDR4-spot fire). ID kept stable. Six files touched: src/state.py (+345 lines: dataclasses + load/save/update API), src/analysis/signal_reasoner.py (prompt rewrite + evidence_log param), src/fast_tier.py (load before L2 + save after), src/config.py (rename), .github/workflows/fast_tier.yml (commit step), NEW state/indicator_evidence.json (bootstrap), NEW tests/test_indicator_evidence.py (18 tests). 108 tests pass (90 prior + 18 new). Supersedes the earlier-in-session v3.39.7.3 prompt-only fix; that fix's symmetric probability rules in llm_summarizer.py remain in place but now operate on correct Layer 2 inputs.
  v3.39.7.2 (2026-05-20) — Single chart fix: contract line in dual-axis spot/contract chart switched back from step(where="post") to plot() per user direction. v3.39.7.1's step function was too rectangular; the original "triangle" complaint was about the fill_between band (now gone) not the plot line itself. Small markers added at each data point so the four contract anchor publish dates are identifiable on the line. Subtitle updated. Single file touched: src/analysis/chart_generator.py.
  v3.39.7.1 (2026-05-20) — Three follow-ups on v3.39.7. (1) Verdict wording aligned with the framework's status vocabulary: MatrixCell.HOLD/WARN/FIRE .value strings changed from "Hold"/"Warn"/"Fire" to "Not fired"/"Partially fired"/"Fired". Python enum identifiers (HOLD/WARN/FIRE) unchanged so MATRIX dict + tests stay valid. Auto-propagates to email badge, chart cell labels, narrative text, subject suffix (now [Not fired] etc), and `_override_t1_spot_decline_from_matrix` which now reads `verdict.cell.value` directly. CYCLE_MATRIX_STATUS_MAP in config.py simplified to identity (kept as constant for documentation only). (2) Contract line in dual-axis chart cleaned up: anchor dashed step + fill_between band + scatter dot + arrow annotation all removed (they created angular polygon shapes and a triangle peak at the Feb 2026 anchor jump). Now: single step function for the synthetic estimate. (3) Part 5a delivered: matrix verdict now wired into signal_reasonings list via new helper _inject_matrix_verdict_into_signal_reasonings. Cycle matrix step MOVED from "between Layer 3 and email build" to "between Layer 2 and Layer 3" so Sonnet's prompt sees the verdict via the v3.39.6 LAYER 2 PER-SIGNAL VERDICTS block. Sonnet exec summary can now reference matrix structured fields (cell, drawdown_pct, synthetic_central_pct, anchor_quarter, weeks_since_peak, adjustment breakdown) verbatim from raw_evidence_dump. All 90 tests still pass.
  v3.39.7 (2026-05-20) — Cycle matrix indicator (t1_spot_decline reworked). 5x5 trigger matrix replacing the prior trajectory-relative variance trigger: rows = contract QoQ growth bands (R1 >=+30% ... R5 <=+5%), cols = DDR5 16Gb 4800/5600 spot drawdown from rolling 12-week peak (C1 at peak ... C5 >=-20% off). Verdict cells HOLD/WARN/FIRE with explicit semantics. Contract row uses a synthetic estimate (anchor + 0.4*tone + 0.4*supplier + 0.2*spot_lead) computed weekly between TrendForce quarterly forecasts. Matrix verdict OVERRIDES the LLM's t1_spot_decline assessment via CYCLE_MATRIX_STATUS_MAP (Hold→Not fired, Warn→Partially fired, Fire→Fired) so the badge is deterministic, not LLM-paraphrased. Two charts injected into the indicator card: matrix dot (40%) + dual-axis spot/contract time series (60%). New module src/data/cycle_matrix.py (1243L). News.py extended with structured spot/contract scrapers + persistence helpers (preserving v3.39.3 scrape_trendforce_prices for the news pipeline). llm_summarizer.py extended with extract_dram_signal_from_article + extract_dram_forecasts_from_press_release (Haiku-based). fast_tier.py wires _run_cycle_matrix_step between Layer 3 and email build. Subject line gets [Hold|Warn|Fire] suffix. Pre-seeded data/ CSVs: 63-row spot history (Jul 2025 - May 2026, triangulated from 3DCenter retail index + EU 32GB kit pricing + Liberty Street Bloomberg DDR5 chart shape + user-provided TrendForce live prints for Mar-May 2026), 3 quarterly forecasts (Q1'26 initial+revised, Q2'26 initial), 4 tone signals. Current verdict: Hold (R1/C2) — cycle peak was early Feb at $44.57, today $41.10, drawdown -2.07% from rolling 12w peak $41.98, synthetic 54.6% QoQ. Next decision-relevant event: Q3'26 TrendForce forecast (~end-June 2026). Parts 1-4 covered in this release; Parts 5+6 articulated in ROADMAP.md.
  v3.39.6 (2026-05-19) — Sonnet now receives Layer 2 per-signal verdicts (the 22 Haiku indicator reasonings) AND computed_metrics (rel_strength_full, capex_revenue_ratios, forward_pe, dart materials). New `signal_reasonings` and `computed_metrics` parameters on `build_sonnet_analysis_prompt`, `sonnet_analyze`, `analyze_with_llm`; `fast_tier.py:462` passes them through. Two new prompt blocks (COMPUTED METRICS, LAYER 2 PER-SIGNAL VERDICTS) plus a STRICT NUMBER-FIRST INDICATOR FORMAT instruction telling Sonnet to lead with exact numbers, keep commentary to ≤1/3 of indicator text, ban speculative "almost certainly past threshold" / "highly probable when measured" phrasings, and DEFER to Layer 2 on per-signal status. Closes the void where Sonnet hallucinated the t3_relative_strength verdict on May 18 (wrote "almost certainly past -10pp threshold" when computed verdict was severity=None). 9 new tests in tests/test_v3_39_6_calculation_driven.py; total 122 tests pass.
  v3.39.5 (2026-05-18) — Sonnet now receives Layer 1 deep-tier digests. `build_sonnet_analysis_prompt`, `sonnet_analyze`, `analyze_with_llm` all gain `digests` parameter; `fast_tier.py:462` call site passes it through. New `DEEP-TIER BROKER NOTES (PRIMARY SOURCE)` + `DEEP-TIER EARNINGS TRANSCRIPTS (PRIMARY SOURCE)` blocks injected into the Sonnet prompt before the news triage block, with two new instructions: (1) PRIMARY-SOURCE PRECEDENCE — when a broker_action overlaps with a deep-tier digest, cite the broker not the news aggregator; (2) MAJOR BROKER ACTIONS IN KEY NEWS — target-price moves ≥10% must surface in key_news_summary even if news triage didn't see them. Closes the void where Mirae's W3.2M raise was written to state/digests/ but never reached Sonnet, causing section 4 to cite Seoul Economic and section 5 to omit the raise entirely. Plus independent fix in src/data/news.py: Google News entry URLs now extracted from entry.summary HTML (publisher href in first <a> tag) since entry.link returns encoded redirect URLs that frontend rejects with HTTP 400. 12 new regression tests in tests/test_v3_39_5_void_closures.py; total 113 tests across the project. Out of scope (deferred to v3.39.6): Haiku Layer A3 structured-source summarization, Layer 2 signal_reasonings → Sonnet, DART filter widening, KIND/KRX scraper, Samsung IR audit.
  v3.39.4.2 (2026-05-18) — Digester filename sanitization fix in src/analysis/digester.py. The naive `.replace(' ', '_')` left forward slashes intact; the v3.39.4 Mirae HTML title format ("SK Hynix (000660 KS/Buy)...") exposed it as a path-separator confusion bug at write time. Now uses regex `[^A-Za-z0-9._-]` → `_` matching what korean_brokers.py applies to html_path filenames. One new regression test. No architecture changes.
  v3.39.4.1 (2026-05-18) — Mirae relevance filter tightened: `"samsung"` dropped from `TRACKED_TITLE_KEYWORDS` after Phase E dry-run output (17:22 UTC May 18) caught it pulling Samsung Fire & Marine (insurance, 000810) into the digester. Samsung Electronics still flows via ticker-match on 005930. Three regression tests added covering the Fire & Marine, SDI, and Electronics paths. No architectural changes.
  v3.39.4   (2026-05-18) — Mirae HTML scraper restored end-to-end. Phase D diagnostic identified `data-message-id` as the selector format (not `messageId=` URL params or `goView()` handlers as initially hypothesized); fetch_mirae_notes rewritten to construct detail URLs from message IDs and parse the HTML body directly. pdfplumber dependency dropped. Mirae is now the sole Korean broker note source (NH and Samsung Securities deleted in v3.39.3.1) and is back to producing notes daily. Architecture diagram in this file remains pre-v3.39.3 wording; full refresh deferred.
  v3.39.3.1 (2026-05-18) — Module map note: src/data/korean_brokers.py is now Mirae-only after the v3.39.3.1 cleanup pass deleted NH Investment (closed source) and Samsung Securities (IMAP never configured). NH and Samsung's target/rating signals still flow into Tier 2 scoring via WiseReport's cTB24 (Naver path) — the locked Tier-B broker decision at line ~108 remains accurate. AnandTech also removed from RSS_FEEDS. Schedule + cron diagram below remain pre-v3.39.3 wording; full refresh deferred.
  v3.39.3 (2026-05-18) — Section header bumped (v3.34 → v3.39.3) to reflect that CHANGES.md is now authoritative for v3.35+. v3.39.3 itself ships the Naver Finance scraper restoration via WiseReport iframe — three Korean broker scrapers (Mirae/NH/Samsung) remain inert pending v3.39.4; signal flow restored through the Naver path alone.
  v3.34  (2026-05-16) — Added v3.34 section (Motley Fool URL regex fix + Yahoo diagnostic logging + git push race fix); honest record of what the first v3.33 run showed
  v3.33  (2026-05-16) — Added v3.33 section (cascade architecture: Motley Fool primary + Yahoo via residential proxy + URL-seen cache); model name bug + first-run diagnosis recorded
  v3.32  (2026-05-14) — Added v3.32 section (Yahoo Finance transcript source); v3.31 section corrected re: IR-site coverage
  v3.31  (2026-05-14) — Version sections relabelled — transcript work = v3.31, deferred indicators reclaim v3.4
  v3.3   (2026-05-12) — NEW FILE — permanent in-repo context (who/architecture/locked decisions/build state/norms)
-->

This file is read first by any Claude (Claude Code, `@claude` GitHub Action, or
a chat session with the repo connected) before working on this codebase. It is
the permanent, in-repo cousin of the `SESSION_HANDOFF.md` brief. Keep it current.

---

## Who the user is

Raul — Munich-based senior supply chain executive (CET/CEST timezone). Not a
professional developer, but technically literate and detail-oriented. Holds a
memory-semiconductor equity portfolio: **SK Hynix is the primary position**,
also MU, Samsung, ASML, AMAT, NVDA, AVGO, TSM.

### Communication preferences — follow these

- Direct, honest tradeoffs. Say when something is a bad idea. Do not agree to be
  agreeable.
- Empirical validation beats theoretical reasoning. If a claim can be tested
  against history, test it.
- Do not suggest paid solutions without explaining why the free option is
  insufficient.
- For complex changes: show the math/logic first, then make the edit, then
  verify with a test.
- Concise. Bullets and tables when they aid clarity, prose when they do not.
- Push back on requests that seem wrong — Raul explicitly prefers this over
  compliance. But push back with empirical reasoning, not "we already decided".
- The back-and-forth is the point. Design discussion happens in chat sessions;
  this repo's `@claude` integration is for *executing* already-settled specs,
  not for designing from scratch.

---

## What this project is

A daily email brief on the memory semiconductor cycle, focused on SK Hynix.
Runs on free-tier GitHub Actions. Hierarchical LLM synthesis pipeline:
Haiku Layer 1 digesters → Haiku Layer 2 per-signal reasoning → Sonnet Layer 3
exec summary. Tracks 22 cycle indicators + competitor dynamics (Indicator 6) +
SK Hynix US ADR launch progress (Indicator 7). Delivered ~13:00 CEST weekdays.

**Cost target**: €5-12/month (Anthropic API only). Everything else is free tier.
Do not introduce changes that break this without explicit discussion.

---

## Architecture (locked — do not re-debate)

### Two-tier execution

```
DEEP TIER (08:00 UTC daily)         FAST TIER (11:00/11:30/12:05 UTC weekdays)
  10-25 min runtime                   4-10 min runtime
  • Transcripts: IR + Motley Fool      • RSS (32 feeds)
  • Mirae/NH PDFs                     • EDGAR (6 feeds)
  • Samsung Sec (Gmail)               • Naver Finance, Finnhub, DART
  • Layer 1 (Haiku) digests           • News relevance filter (Step 1b)
                                      • Loads Layer 1 digests from disk
                                      • Signal scoring (Indicator 3)
                                      • Layer 2 (Haiku × 22 signals)
                                      • Layer 3 (Sonnet × 1)
                                      • Email render + send
```

Idempotency via `state/brief_sent_dates.json`. 3 cron triggers on fast tier for
redundancy. `src/main.py` is the legacy single-shot, kept for local dev only —
production is `deep_tier.py` + `fast_tier.py`.

### Module map

```
src/
├── config.py            # 22 INDICATORS + tiers + baselines + calendar +
│                        #   RSS_FEEDS + GOOGLE_NEWS_QUERIES + TRACKED_* lists
├── state.py             # idempotency, retry, cache
├── deep_tier.py         # overnight orchestrator
├── fast_tier.py         # daily delivery orchestrator
├── main.py              # legacy single-shot (local dev only)
├── diagnostics.py       # DiagnosticsCollector / SourceFetchStatus
├── priority_alerter.py
├── data/                # source modules: prices, estimates, news, transcript_sources,
│                        #   naver_finance, finnhub, dart, korean_brokers
│                        #   (korean_brokers is Mirae-only since v3.39.3.1;
│                        #    NH and Samsung Securities deleted as inert.
│                        #    Mirae rebuilt as HTML scraper in v3.39.4 after
│                        #    Phase D identified the data-message-id selector.)
├── analysis/            # digester, signal_reasoner, signal_scoring,
│                        #   llm_summarizer, chart_generator
└── email_module/        # template + sender
tests/
└── test_architecture_extraction.py   # v3.3 extraction prompt fixtures
```

---

## Locked decisions — DO NOT re-litigate without empirical reason

### 1. Indicator 3 weighted broker scoring
Each broker action: `direction × (rating + PT + EPS points) × tier × (1 + combo)`
- Tier A (×3): Mirae Asset, Goldman Sachs, Morgan Stanley, JPMorgan, Mizuho
- Tier B (×2): Samsung Securities, NH Investment, Korea Investment & Securities,
  Bernstein, Susquehanna, BofA, Citi, UBS, HSBC, Macquarie
- Tier C (×1): other Korean Tier 2 brokers + smaller US shops
- Magnitude brackets (PT% or FY27 EPS%): <5%→0.5, 5-10%→1.0, 10-20%→1.5, ≥20%→2.0
- Rating: initiate ±1.5, one-notch ±1.0, two-notch ±2.0, maintained 0
- Combo bonus: +50% per additional aligned component

### 2. Bearish-only firing (not net)
Track bearish + bullish + net separately per ticker per window. Firing uses
**bearish-only** so a fresh bearish cluster isn't masked by older bullish actions.
- Bearish 7d < -10 → EARLY WARNING
- Bearish 30d < -15 → FIRED
- Bullish 30d > +15 → BULLISH CONFIRM
- Single note < -5 OR Tier A < -3 → flagged
Validated against the Aug 2021 and Sept 2024 Morgan Stanley downturn calls — see
`docs/INDICATOR_3_EXAMPLES.md`.

### 3. Koyfin NOT purchased
Naver Finance (30-60min Korean lag) + Finnhub (5-30min US lag) are both faster
than Koyfin's Capital IQ feed (24-72hr). Deferred indefinitely.

### 4. Two-tier architecture is fixed
Do not merge the tiers, do not change the cron strategy, do not move Layer 1 out
of deep tier. The split exists for runtime and cost reasons.

---

## v3.1 → v3.39.3 — what's already built

### v3.1 — News relevance filter
Two-pass filter (`src/data/news.py`: `score_relevance` + `filter_relevant_news`)
inserted in `fast_tier.py` as Step 1b. Pass 1 = keyword match against
`TRACKED_ANALYSTS` / `TRACKED_BROKERS` / `TRACKED_COMPANIES`. Pass 2 = Haiku
0-10 scoring of the matches. EDGAR items auto-keep at 10. Score < 3 dropped and
logged to `state/news_dropped_*.json`. `min_score=3` is the tuning knob.

### v3.2 — Architectural sensing
Added RSS feeds and Google News queries for captive-ratio leading indicators
(inference efficiency, memory disaggregation, LPDDR datacenter, custom silicon).
New `TRACKED_CONCEPTS` list (architectural terms) wired into the filter Pass 1.
Haiku rubric updated to score first-of-kind architectural deployments highly.

### v3.3 — Earnings architecture extraction
`src/analysis/digester.py` extended: `DIGEST_SYSTEM_PROMPT` now extracts an
`architecture_signals` sub-object (18 fields) from earnings transcripts and
broker notes. `_log_architecture_signals()` shadow-logs to
`state/extraction_log_*.jsonl` — append-only, isolated, failure cannot affect
digest output. Test fixtures in `tests/test_architecture_extraction.py`.

### v3.31 — Transcript sources (Quartr replacement) — BUILT
Quartr discontinued its free developer tier. `src/data/transcript_sources.py`
replaces it with free sources. **Note: the original v3.31 build relied on
IR-site scrapers + Motley Fool only — this missed that the SK Hynix and Samsung
IR sites publish no text transcript, and that Yahoo Finance carries the full
call text for them. Corrected in v3.32 below.** `src/data/quartr.py` is a
deprecation shim. Every transcript source returns a three-state availability
flag (full_transcript / materials_only / nothing); the digester records it and
the email surfaces "commentary NOT available" explicitly. OpenAI/Anthropic are
dormant `AI_LAB_IR_WATCH` entries — activate when they list (news pipeline
already watches for the listing event). The scrapers are the most fragile part
of the pipeline — expect selector maintenance 1-2x/year.
NOTE ON NUMBERING: this was deliberately numbered v3.31 — a .3x maintenance fix,
not a feature increment — so the deferred captive-ratio indicators keep their
original **v3.4** label. (A transient mislabel briefly called this work "v3.4";
corrected to v3.31.)

### v3.32 — Transcript source correction (Yahoo Finance primary) — BUILT
v3.31 left a real gap: it scraped the SK Hynix / Samsung IR sites for
transcripts, but those sites publish slides + webcast only — no text
transcript — and Motley Fool doesn't cover Korean listings. So the two most
important names had **no working full-transcript source**. v3.32 fixes it:
- **Yahoo Finance is now the primary transcript source** (`YAHOO_TRANSCRIPT_TICKERS`
  in config.py, `fetch_yahoo_finance_transcripts()` in transcript_sources.py).
  Verified to carry full call text incl. Korean+English Q&A for SK Hynix,
  Samsung, Micron, ASML, TSMC; expected full for the US mega-caps. Latest call
  free; historical gated — fine for a daily brief.
- **SK Hynix / Samsung IR scrapers demoted to materials-only** via
  `transcript_capable: False` in `IR_SOURCES` — the IR scraper is now capped at
  `materials_only` for them and can never masquerade as a transcript source.
  Kept for the earnings release / slide deck. Micron IR stays full-capable.
- Source priority is now Yahoo → Motley Fool → IR; the aggregator keeps the
  best availability state, so Yahoo's full SK Hynix transcript wins over the
  IR-site materials automatically.
- One Yahoo quirk handled: Yahoo syndicates the same transcript across many
  ticker variants — `YAHOO_TRANSCRIPT_TICKERS` uses ONE canonical ticker per
  company to avoid double-counting.
Files touched: `config.py`, `transcript_sources.py`, `deep_tier.py`,
`DEPLOYMENT_GUIDE.md`, `README.md`, `SETUP.md`, `CHANGES.md`, the data-source
audit xlsx. Still a .3x maintenance fix — v3.4 keeps its label.

### v3.33 — Transcript cascade + Yahoo via residential proxy — BUILT
v3.32's "Yahoo is primary" design broke when the first deep-tier run revealed
Yahoo blocks datacenter IPs (404). v3.33 reorders the architecture: free
sources first, paid proxy only where needed. Also fixed two unrelated bugs
caught in the same run.

Three bugs the first run surfaced:
- **Model name bug**: `HAIKU_MODEL = "haiku"` and `SONNET_MODEL = "sonnet"`
  literal defaults — those are placeholders the Anthropic API doesn't accept.
  Fixed to `"claude-haiku-4-5"` / `"claude-sonnet-4-6"` (dateless aliases work,
  pin to current snapshots).
- **Motley Fool pointed at the wrong URL**: scraper hit
  `/earnings-call-transcripts/` (global recent index, ~20 transcripts, rolls
  off in 24-48h — every basket name was already gone). Rewritten to hit
  `/quote/{exchange}/{ticker}/` per company — persistent pages that list the
  full transcript history. Hence the `MOTLEY_FOOL_TICKERS` shape change from
  `{name: ticker}` → `{name: {"exchange": "nasdaq"|"nyse", "ticker": ...}}`.
- **Yahoo blocked from datacenter IPs**: HTTP 404 to GitHub Actions runners.
  Routed through Bright Data residential proxy via `RESIDENTIAL_PROXY_URL` env;
  uses realistic Chrome headers, `-session-{N}` username token injection for
  per-request IP rotation, redirect-following, retry-on-429 with fresh session,
  and 3-second inter-ticker delay. Empirically validated 2026-05-16 (one curl
  AMAT test returned 200 with real Q2 FY26 transcript content).

The cascade (in `fetch_all_transcripts`):
1. **Motley Fool** — primary for the 13 US names. Direct fetch, no proxy.
2. **IR scrapers** — Micron (full transcript), SK Hynix + Samsung
   (`materials_only` per v3.32). Direct fetch.
3. **Yahoo Finance** — fallback. Fires for any name NOT in fresh
   `full_transcript` state at this point — so `nothing` + `materials_only` +
   `already_processed` (per Raul's "goal is the transcript" rule, the cached
   state still triggers Yahoo because the cache may be hiding new content).
   Skips only when Motley Fool or IR just digested a fresh full transcript.
   Korean names always reach this step (no MF coverage). Routes through
   residential proxy.

New URL-seen cache at `state/cache/transcripts_seen.json` — scrapers
short-circuit to `availability="already_processed"` on URL re-encounter; the
digester skips those entirely. Saves Haiku tokens, proxy bandwidth, and avoids
filling `state/digests/` with daily re-digests of the same call.

Bright Data setup constraint surfaced in v3.33: individual accounts (no
business KYC) are stuck on Bright Data's "Immediate Access" mode — robots.txt
enforcement, GET/HEAD only, "high volume throttled". For our use (15 daily GET
requests on Yahoo Finance) this is fine. 429s seen during testing were Yahoo
target-site rate limits, NOT Bright Data blocks (confirmed via support chat);
the rotation + headers + 3s-delay pattern mitigates them.

Cost ceiling: ~3-5 MB/day proxy bandwidth worst case (~$0.10/month); trial
credits ($27, 3.3 GB) cover ~2 years of normal operation. Ongoing at
~$0.30-1.00/month on Bright Data paid residential ($4/GB) if Raul keeps the
account active after trial expiry.

Files touched: `config.py`, `transcript_sources.py`, `analysis/digester.py`,
`deep_tier.py`, `.github/workflows/deep_tier.yml`, `CLAUDE.md`, `README.md`,
`SETUP.md`, `DEPLOYMENT_GUIDE.md`, `CHANGES.md`, audit xlsx. Still .3x; v3.4
keeps the captive-ratio label.

### v3.34 — Post-v3.33 fixes (Motley Fool URL + Yahoo cross-listing tickers + diagnostics + git race) — BUILT
First v3.33 run revealed three bugs. Diagnosing them surfaced a fourth fix
(Yahoo ticker variant) that resolves the SK Hynix transcript coverage problem.

1. **Motley Fool URL regex was capturing a trailing backslash** from
   JSON-stringified blobs in the React-rendered quote page. Every fetch URL
   ended in `/\` which URL-encodes to `%5C` → 404. The URLs the regex was
   finding were *actually correct* — just had a stray escape char appended.
   Fixed by excluding the backslash from the regex character class, plus an
   `rstrip("\\")` + slash re-add as belt-and-suspenders. After v3.34, Motley
   Fool should return `full_transcript` for all 13 US names.
2. **Yahoo canonical ticker for SK Hynix / Samsung was wrong**. v3.32 picked
   the KRX primary listings (000660.KS, 005930.KS) as canonical. Empirically
   (Raul's screenshot of Yahoo Finance, 2026-05-16): Yahoo's transcript pages
   are linked from the European cross-listing tickers (HY9H.MU, SSU.F etc.)
   but NOT from the KRX primaries. Same transcript content under both —
   Yahoo's UI just doesn't render the link on the KRX-ticker pages. Updated
   canonical Yahoo ticker: SK Hynix → HY9H.MU (confirmed), Samsung → SSU.F
   (likely; v3.34 diag logs will confirm). v3.32's note about "use ONE
   canonical ticker per company to avoid double-counting" still applies —
   the correction is which one to pick, not whether to pick one.
3. **Yahoo for the US ticker variants returned 200 but 0 transcript anchors**
   on parse (only AMAT found 1 URL, which 404'd). Could be the same wrong-
   variant issue or a deeper JS-shell problem. Honest admission: my earlier
   curl test "proved architecture works" was wrong; I only verified status
   200, never inspected body content. v3.34 adds diagnostic logging (HTTP
   status, body length, anchor count, sample hrefs) so the *next* run gives
   us actual data on what Yahoo returns through the proxy, before I propose
   a real fix.
4. **Git push at end of deep-tier run was rejected** because main moved
   forward during the 3-5 minute run (Raul committing v3.33 manually via web
   UI). Workflow now does `git pull --rebase origin main` before push with
   one retry, so state-file commits land even when the repo moved.

What's pending for v3.35: depending on what the v3.34 Yahoo diagnostic logs
show, either pivot per-ticker variant fallback chain (try multiple Yahoo
listings per company until one returns content), swap to a different free
transcript source, or accept Yahoo as best-effort and rely on Motley Fool +
Korean broker PDFs. Decision deferred until empirical data is in hand.

Files touched: `src/config.py`, `transcript_sources.py`,
`.github/workflows/deep_tier.yml`, `CLAUDE.md`, `CHANGES.md`. Still .3x;
v3.4 keeps the captive-ratio label.

### v3.39.7 — Cycle matrix indicator + charts — BUILT
Reworks `t1_spot_decline` from a trajectory-relative variance trigger into a
5×5 trigger matrix mapping DRAM contract QoQ growth (rows) against DDR5 16Gb
4800/5600 spot drawdown from rolling 12-week peak (cols). Returns Hold / Warn
/ Fire per cell. The matrix verdict OVERRIDES the LLM's t1_spot_decline
assessment via `CYCLE_MATRIX_STATUS_MAP` (Hold→Not fired, Warn→Partially
fired, Fire→Fired) so the status badge is deterministic, not LLM-paraphrased.

**Matrix grid (locked):**

```
                 C1            C2            C3            C4            C5
              at peak      <5% off       5-10% off    10-20% off    >=20% off
R1 >=+30%      HOLD          HOLD          HOLD          WARN          FIRE
R2 +20-30%     HOLD          HOLD          WARN          WARN          FIRE
R3 +10-20%     WARN          WARN          WARN          FIRE          FIRE
R4 +5-10%      FIRE          FIRE          FIRE          FIRE          FIRE
R5 <=+5%       FIRE          FIRE          FIRE          FIRE          FIRE
```

R4-R5 rows = "growth halving" — historical peak band, all Fire. C5 column =
">=20% off peak" — always Fire regardless of contract band.

**Synthetic contract estimate**: `anchor + 0.4*tone_adj + 0.4*supplier_adj +
0.2*spot_lead_adj`, each adjustment capped ±30pp. Anchor priority: server_ddr5
> conventional_dram_aggregate > supplier_specific (supplier_specific feeds
supplier_adj, never the anchor).

**Today's verdict (May 19, 2026): Hold (R1 / C2)**. Spot $41.10, drawdown
-2.07% from rolling 12w peak $41.98 (week ending 2026-03-08). Cycle peak was
early Feb 2026 at $44.57, now 11 weeks outside the 12w rolling window —
intentional design choice (catches rollovers without false signals from old
peaks; chart shows cycle peak as annotation for context). Synthetic 54.6%
QoQ central: anchor Q2'26 +60.5%, tone -17.3pp from 3 cooling signals,
supplier 0pp, spot_lead +5pp from DDR5 +6.5% over last 4 weeks.

**Spot history backfill provenance** (data/dram_spot_history.csv, 63 rows
Jul 2025 - May 2026):
- 3DCenter Speicherkrise-Preisindex (German retail DDR5, Jul 2025=100,
  Feb 2026=440 peak, Mar=408, Apr=~416, May=~432)
- Tweakers/Tom's Hardware EU 32GB kit pricing (€95-100 Aug 2025 → €430-470
  early Feb peak → €425 late Feb)
- Liberty Street Economics / a16z DDR5 daily price chart (Bloomberg + BLS
  via Haver Analytics — same shape, Feb peak)
- TrendForce live prints (user-provided CSV Mar 12 - May 15 2026)
Each row tagged with `provenance` column: `trendforce` (actual print) or
`backfill` (reconstructed). Production scrapes will only update mid-March 2026
onwards; pre-March is the documented reconstruction.

**Two charts in the t1_spot_decline indicator card** (40/60 split):
- Cycle matrix dot: 5x5 grid colored by HOLD/WARN/FIRE, callout dot at today's
  (contract_row, spot_col) position
- Spot price + synthetic contract dual-axis: DDR5 16Gb 4800/5600 spot (left Y,
  purple) + synthetic contract QoQ estimate (right Y, green) over time, with
  peak ($44.57 early Feb) and today ($41.10) annotations, anchor step function
  as dashed line

**New module**: `src/data/cycle_matrix.py` (1243L) — matrix evaluator. Spot
calculations (weekly aggregation, 12w rolling peak, drawdown classification),
contract calculations (anchor selection, tone scoring, supplier adjustment,
spot lead signal, synthetic central), matrix lookup, narrative generation.
CSV schemas documented at the bottom.

**Modules extended** (additions only, all existing behaviour preserved):
- `src/data/news.py` — structured spot/contract scrapers
  (`scrape_trendforce_spot_structured`, `scrape_trendforce_contract`) +
  persistence helpers (`persist_*_to_history`, `upsert_contract_*`) +
  `fetch_article_body`. Existing v3.39.3 `scrape_trendforce_prices()` keeps
  feeding the news pipeline unchanged.
- `src/analysis/llm_summarizer.py` — Haiku extractors
  (`extract_dram_signal_from_article` + `extract_dram_forecasts_from_press_release`)
  with prompts as module constants.
- `src/analysis/chart_generator.py` — `generate_cycle_matrix_chart` +
  `generate_spot_contract_chart` + `render_cycle_charts_from_verdict` helper.
- `src/email_module/template.py` — `build_email_html()` signature gains three
  optional kwargs; `_build_indicators_section` injects the matrix card under
  `t1_spot_decline`; new `_build_cycle_matrix_card` helper.
- `src/fast_tier.py` — `_run_cycle_matrix_step()` between Layer 3 and email
  build; `_override_t1_spot_decline_from_matrix()` writes the deterministic
  status badge; subject-line `[Hold|Warn|Fire]` suffix.
- `src/config.py` — `t1_spot_decline` Indicator entry rewritten (matrix
  logic; all dataclass fields preserved: trigger_patterns/numeric/cadence/
  stitching refined for matrix semantics, not stripped); `CYCLE_MATRIX_*`
  config block appended at end, including `CYCLE_MATRIX_STATUS_MAP`.

**Cost impact**: ~$0.50 per pipeline run (max 10 Haiku extractions × ~$0.05),
~$22/month at fast-tier cadence. Spot/contract scrapes free. Chart rendering
negligible (matplotlib).

**Known limitations**:
- HBM contract pricing not modeled (open gap; would be a Part 7 candidate)
- Synthetic estimate historical trajectory in the chart approximates the step
  function; proper weekly back-computation needs historical signal extractions
- 12-week rolling peak excludes the early Feb 2026 cycle high (design choice
  for rollover sensitivity; the cycle peak shows as chart annotation)

**Parts breakdown** for v3.39.7:
- Part 1: Matrix design + `cycle_matrix.py`
- Part 2: Scrapers + persistence helpers
- Part 3: Haiku extraction prompts + functions
- Part 4: Charts + email integration + status mapping fix

**Parts 5 and 6 planned** — see `ROADMAP.md`. Part 5 wires the matrix
verdict into `signal_reasoner.py` so Sonnet exec summary can reference it,
plus the GHA workflow commit step for data/*.csv accumulation. Part 6 is the
backtest/calibration harness against 2017-18 and 2022 cycle peaks.

Files touched: `src/config.py`, `src/data/news.py`, `src/data/cycle_matrix.py`
(new), `src/analysis/llm_summarizer.py`, `src/analysis/chart_generator.py`,
`src/email_module/template.py`, `src/fast_tier.py`,
`tests/test_cycle_matrix.py` (new), `data/*.csv` (new seeded),
`memory_cycle_data_source_audit_v3_39_7.xlsx` (updated),
`.github/workflows/fast_tier.yml`, `CLAUDE.md`, `CHANGES.md`, `ROADMAP.md`.

### v3.4 — DEFERRED (see ROADMAP.md)
Three new indicators (Inference Efficiency Shock, CXL Production Deployment,
LPDDR Datacenter Ramp). Intentionally deferred ~4 weeks from v3.3 deploy so
thresholds can be calibrated from real `extraction_log_*.jsonl` data instead of
guessed. Do not build these without reading `ROADMAP.md` and the accumulated
extraction logs first.

---

## Analytical framework context (for judgment calls)

The user tracks a thesis-level concern about **HBM captive ratio** — the share
of AI memory demand that *must* stay in HBM vs. what can substitute to cheaper
tiers (DDR5+CXL, LPDDR, GDDR, HBF). Key points that inform what's "relevant":

- Decode-phase inference is what makes HBM essential today, because it reads the
  entire model per token. Architectural efficiency (quantization, MoE,
  speculative decoding, KV cache compression, disaggregated serving) can each
  reduce decode bandwidth need — stacked, they could shift workloads off HBM.
- SK Hynix is the most HBM-concentrated of the three majors. If inference shifts
  LPDDR-heavy, SK Hynix loses relative share to Samsung (stronger in LPDDR). This
  makes LPDDR signals the most consequential for the user's specific position.
- Leading indicators matter more than the captive ratio itself, which is ex-post.
  The pipeline should privilege Tier 1 (research/architecture) and Tier 2
  (commercial deployment) signals over Tier 4 (pricing) signals.

When judging whether a news item or a code change is "relevant", use this frame.

---

## Working norms for changes to this repo

- **Every editable file carries a version log.** Near the top of each source
  file (inside the module docstring for `.py`, as an HTML comment after the H1
  for `.md`) there is a `CHANGELOG (this file):` block listing what changed at
  each version, newest-first. When you modify a file, ADD a line to its block.
  This is a hard learning from prior repo work — the file's own history must be
  legible without cross-referencing the global `CHANGES.md`. Files that ARE
  changelogs (`CHANGES.md`, `ROADMAP.md`) don't need their own block.
- **Validate before delivering**: `python -m py_compile` on changed files at
  minimum; run `tests/test_architecture_extraction.py` if the digester prompt
  changed.
- **Graceful failure**: any new external call or parsing step must be wrapped so
  that failure degrades gracefully and never breaks the daily brief. The pipeline
  running with reduced data beats the pipeline not running.
- **Shadow mode for anything that fires alerts**: new indicators should log
  silently for a calibration period before affecting the email.
- **Cost awareness**: flag any change that materially moves the €5-12/month
  Anthropic spend.
- **Diagnostics**: new data sources should register with `DiagnosticsCollector`
  so they appear in the email's validation table.
- **Don't restructure for taste**: match existing patterns. This is a working
  pipeline, not a greenfield project.
