# Memory Cycle Brief — Changelog

This release consolidates v3.1 (news relevance filter), v3.2 (architectural sensing feeds), v3.3 (earnings architecture extraction), v3.31 (transcript sources — Quartr replacement), and v3.32 (transcript source correction — Yahoo Finance primary). v3.4 (captive-ratio indicators) is intentionally deferred — see `ROADMAP.md`.

> **A note on version numbering.** v3.31 and v3.32 are maintenance fixes, deliberately numbered in the .3x range rather than as v3.4. This keeps the v3.4 label for the planned captive-ratio indicators. A transient mislabel during the v3.31 build briefly called the transcript work "v3.4" — that has been corrected to v3.31 everywhere.

---

## v3.32 — Transcript Source Correction (Yahoo Finance primary)

**Why**: v3.31 replaced the gated Quartr API with IR-site scrapers + Motley Fool — but that left a real gap. The SK Hynix and Samsung IR sites publish slides + webcast only, **no text transcript**, and Motley Fool doesn't cover Korean listings. So the two most important names in the portfolio had *no working full-transcript source*. Verification (May 2026) found Yahoo Finance carries the full call text — including the Korean+English Q&A — for SK Hynix, Samsung, Micron, ASML, TSMC, and (by the same syndication feed) the US mega-caps.

| File | Changes |
|---|---|
| `src/config.py` | NEW `YAHOO_TRANSCRIPT_TICKERS` — canonical Yahoo ticker per company (15 names), the primary transcript source. `IR_SOURCES` gains a `transcript_capable` flag: `False` for SK Hynix / Samsung (IR sites are materials-only), `True` for Micron. Also scrubbed 24 stale `"Quartr"` references in the `INDICATORS` `sources` / `stitching` fields (left over from v3.31) → `"Yahoo Finance"` — the `stitching` text feeds the live Haiku digester prompt, so this matters |
| `src/data/transcript_sources.py` | NEW `fetch_yahoo_finance_transcripts()` + `_scrape_yahoo_ticker()` — defensive Yahoo scraper following the same three-state-flag contract. `_scrape_ir_site()` now honours `transcript_capable` — caps SK Hynix / Samsung at `materials_only` regardless of text length. `fetch_all_transcripts()` and `fetch_transcripts_with_retry()` take a `yahoo_tickers` arg; source priority is Yahoo → Motley Fool → IR, with the aggregator keeping the best availability state (so Yahoo's full SK Hynix transcript wins over the IR-site materials automatically) |
| `src/deep_tier.py` | Step 1 imports and passes `YAHOO_TRANSCRIPT_TICKERS` to `fetch_transcripts_with_retry()` |
| `DEPLOYMENT_GUIDE.md` | Section 2.3 rewritten — Yahoo Finance listed as primary; SK Hynix / Samsung IR explicitly materials-only. Troubleshooting + "Where to make adjustments" rows updated |
| `README.md`, `SETUP.md`, `CLAUDE.md` | Transcript-source descriptions updated to Yahoo Finance + Motley Fool + IR; CLAUDE.md gains a v3.32 section and a correction note on v3.31 |
| `memory_cycle_data_source_audit_v3_32.xlsx` | Source Registry corrected: stale "Quartr — TO ADD" row replaced; Yahoo Finance / Motley Fool / IR scrapers added as ✅ in-repo. Indicator-sheet "Primary sources" columns updated from "Quartr earnings transcripts" to the actual sources. Summary sheet gains v3.31 + v3.32 notes |

**The Yahoo syndication quirk**: Yahoo carries the same transcript under many ticker variants (SK Hynix also shows as HY9H.F / .DU / .SG; Samsung as SSU.F / SMSN.L / SSNLF). `YAHOO_TRANSCRIPT_TICKERS` uses ONE canonical ticker per company so the same call isn't counted as independent corroboration.

**Caveat carried forward**: Yahoo gives the *latest* call free; historical calls are behind a Yahoo subscription. Fine for a daily brief that wants the most recent call — but there's no free backfill of old quarters.

**Not in scope** (parked): transcript-memory / quarter-over-quarter diff and event-deduplication — these are a v3.5 item, see `ROADMAP.md`.

---

## v3.31 — Transcript Sources (Quartr replacement)

**Why**: Quartr discontinued its free/self-serve developer tier — the API is now enterprise-only ("contact us" pricing). This was a known risk flagged in the original handover doc. v3.31 replaces it with free sources, no API key required.

| File | Changes |
|---|---|
| `src/data/transcript_sources.py` | NEW. `TranscriptResult` shape + two scrapers: `fetch_ir_transcripts()` (direct IR-site scrapers for SK Hynix, Samsung, Micron) and `fetch_motley_fool_transcripts()` (US-listed names, English calls). `fetch_all_transcripts()` aggregates + de-dupes (Micron appears in both sources — keeps the better availability state). Every result carries a **three-state availability flag**: `full_transcript` / `materials_only` / `nothing`. All scrapers are defensive — a broken selector degrades to `materials_only` or `nothing` and logs a warning, never raises into the orchestrator |
| `src/data/quartr.py` | Replaced with a deprecation shim. Both functions return the "skipped" shape. `QUARTR_API_KEY` is no longer used anywhere |
| `src/analysis/digester.py` | NEW `digest_transcript_results()` honours the three-state flag: `full_transcript` → normal digest; `materials_only` → digest the materials but tag `_transcript_availability` + `_commentary_gap_note` so the brief says "commentary NOT available"; `nothing` → writes a `_placeholder_gap_digest()` so the absence is a tracked fact, not a silent hole. `digest_quartr_transcripts()` kept as a no-op shim |
| `src/deep_tier.py` | Rewired: Step 1 now calls `fetch_transcripts_with_retry()` instead of Quartr. Writes `state/transcript_status.json` so the fast tier can surface commentary availability |
| `src/fast_tier.py` | New `_surface_transcript_status()` reads `transcript_status.json` and adds one `SourceFetchStatus` row per transcript result — `materials_only` shows as a WARN with "COMMENTARY NOT AVAILABLE", so commentary gaps are visible in the email's validation section |
| `src/config.py` | NEW `IR_SOURCES` (SK Hynix / Samsung / Micron IR URLs + reliability hints), `MOTLEY_FOOL_TICKERS` (13 US names), `AI_LAB_IR_WATCH` (dormant OpenAI / Anthropic entries — activate when they list). New Google News queries watch for the OpenAI/Anthropic listing events |
| `DEPLOYMENT_GUIDE.md` | Bumped to v3.31. Section 2.3 rewritten — no Quartr signup, no `QUARTR_API_KEY` secret. New troubleshooting rows for "COMMENTARY NOT AVAILABLE" and transcript-scraper breakage. New "Where to make adjustments" entry for transcript sources |
| `CLAUDE.md`, `SETUP.md`, `README.md` | Quartr references replaced with transcript-sources description |
| `ROADMAP.md` | Versioning note added: the deferred captive-ratio indicators keep their original **v3.4** label; this transcript build is v3.31 (.3x maintenance range) |

**The post-ADR note**: even after the SK Hynix US ADR lists, the IR scrapers stay — the ADR gives a US *ticker* for price/analyst data (via Finnhub), but the earnings *call* is still Korean-hosted. The IR scrapers are the permanent transcript solution for the Korean names, not a pre-ADR stopgap.

**Known fragility**: IR-site scrapers are the most maintenance-prone part of the pipeline. When SK Hynix / Samsung / Micron redesign their IR pages, the link-matching or content selectors in `transcript_sources.py` need updating — expect 1-2 fixes/year, same pattern as the Naver scraper. Graceful degradation means a broken scraper produces `nothing` results, not a crashed pipeline.

---

## v3.1 — News Relevance Filter

| File | Changes |
|---|---|
| `src/config.py` | RSS feeds 17 → 24; Google News queries 17 → 26; added `TRACKED_ANALYSTS` (12), `TRACKED_BROKERS` (20), `TRACKED_COMPANIES` (10) |
| `src/data/news.py` | Added `score_relevance()` and `filter_relevant_news()` (two-pass: keyword match → Haiku scoring; EDGAR auto-keep; dropped-items log) |
| `src/fast_tier.py` | Wired filter as Step 1b, after concurrent fetch and before Layer 1 digest load |
| `DEPLOYMENT_GUIDE.md` | Updated to v3.1 |

## v3.2 — Architectural Sensing

| File | Changes |
|---|---|
| `src/config.py` | RSS feeds 24 → **32** (Damnang Substack, vLLM blog, SGLang/LMSYS, HuggingFace, DeepSeek, Astera Labs newsroom, Artificial Analysis, SemiAnalysis); Google News queries 26 → **56** (LPDDR datacenter, CXL, custom silicon, quantization/MoE/efficiency techniques, wafer allocation); new `TRACKED_CONCEPTS` (46 architectural terms) |
| `src/data/news.py` | Extended `score_relevance()` and `filter_relevant_news()` to accept `tracked_concepts` parameter for Pass 1 keyword boost. Updated Haiku scoring rubric to recognize architectural signals (CXL deployments, custom silicon, inference efficiency releases) at high scores |
| `src/fast_tier.py` | Wires `TRACKED_CONCEPTS` through to the filter call |

## v3.3 — Earnings Architecture Extraction

| File | Changes |
|---|---|
| `src/analysis/digester.py` | Extended `DIGEST_SYSTEM_PROMPT` with rules for extracting captive-ratio architectural signals from earnings transcripts and broker notes. New `architecture_signals` sub-object inside `key_metrics` captures 18 specific fields (memory vendor capex mix, hyperscaler memory % of capex, custom silicon memory architecture, deployed quantization/MoE/speculative-decoding, CXL production deployments, LPDDR datacenter design wins, HBF mentions, etc.) |
| `src/analysis/digester.py` | New `_log_architecture_signals()` shadow logger writes extracted signals to `state/extraction_log_YYYY-MM-DD.jsonl`. Append-only, isolated, never affects digest output if it fails |
| `tests/test_architecture_extraction.py` | NEW. Three synthetic fixtures (memory vendor capex mix, hyperscaler custom silicon, CXL/LPDDR design win) with expected extraction results. Run before deploying digester prompt changes |
| `DEPLOYMENT_GUIDE.md` | Updated to v3.3; new Weekly/Monthly review items for `extraction_log_*.jsonl`; "Where to make adjustments" now covers `TRACKED_CONCEPTS` and the architecture extraction prompt |
| `ROADMAP.md` | NEW. Explicit handover for v3.4 (three new indicators: Inference Efficiency Shock, CXL Production Deployment, LPDDR Datacenter Ramp). Includes build-day checklist for picking this up in a future session after ~4 weeks of extraction-log accumulation |
| `CLAUDE.md` | NEW. Permanent in-repo context file — the durable cousin of `SESSION_HANDOFF.md`. Read first by any Claude (chat with repo connected, Claude Code, or `@claude` GitHub Action). Captures who the user is, communication preferences, architecture, all locked decisions, v3.1-3.3 build state, the v3.4 deferral, the captive-ratio analytical framework, and working norms. Keep current whenever a locked decision changes or a version ships |

---

## How the three layers cooperate

```
┌──────────────────────────────────────────────────────────────┐
│  Fetch (17→32 RSS, 26→56 queries)                            │
│  Adds vLLM/SGLang releases, Damnang, Astera, AA, DeepSeek    │
└────────────────────────────┬─────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  v3.1/3.2 Relevance Filter (Pass 1 keyword + Pass 2 Haiku)   │
│  Now matches against analysts + brokers + companies          │
│         + TRACKED_CONCEPTS (architectural terms)             │
│  Haiku rubric recognizes architectural signals at high score │
└────────────────────────────┬─────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  v3.3 Digester (Layer 1 Haiku per document)                  │
│  Extracts architecture_signals from earnings + broker notes  │
│  Writes to state/extraction_log_YYYY-MM-DD.jsonl (shadow)    │
└────────────────────────────┬─────────────────────────────────┘
                             ▼
                    [v3.4 indicators consume
                     this log after ~3-4 weeks
                     of accumulation]
```

## Cost impact

- **v3.1 relevance filter**: ~5-10 Haiku batches/day, ~50-100K tokens/day. Well under €1/month additional.
- **v3.2 added queries/RSS**: more fetches (~10-50 extra news items/day pre-filter). The filter throws most away. Net Haiku cost: marginal.
- **v3.3 architecture extraction**: extends an existing digester call, ~10-15% larger output. Adds ~5-10K extra output tokens/day on heavy days. Marginal cost.

Overall: still €5-12/month total Anthropic spend target.

## Safeguards committed to (status)

| Safeguard | Status |
|---|---|
| Test fixtures for v3.3 extraction prompt | ✅ `tests/test_architecture_extraction.py` |
| Shadow mode for v3.3 (no email impact in calibration window) | ✅ Writes to `extraction_log_*.jsonl`, never affects digest output |
| Graceful failure | ✅ All new code wrapped in try/except; pipeline continues on failure |
| Dropped-items logging for v3.1 filter calibration | ✅ Already in place from v3.1 |
| v3.4 explicit deferral with reminder mechanisms | ✅ `ROADMAP.md` + calendar reminder (recommend setting for Sunday June 14, 2026) |

## Calendar reminder to set

```
Title: Memory Brief v3.4 — build captive-ratio indicators
Date: Sunday June 14, 2026 (allow ~3-4 hours)
Notes: Open ROADMAP.md in the v3.3 repo. Read 3-4 weeks of
       state/extraction_log_*.jsonl files. Set v3.4 thresholds
       from observed data. Build the three new indicators.
```
