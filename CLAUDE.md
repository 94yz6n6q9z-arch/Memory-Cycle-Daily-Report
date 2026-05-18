# CLAUDE.md — Context for Claude working on this repo

<!--
CHANGELOG (this file):
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
