# Memory Cycle Brief — Changelog

This release consolidates v3.1 through v3.37 plus v3.38 (bug fixes + comprehensive diagnostics across both pipelines). v3.4 (captive-ratio indicators) is intentionally deferred — see `ROADMAP.md`.

> **A note on version numbering.** v3.31 through v3.38 are all maintenance fixes, deliberately numbered in the .3x range rather than as v3.4. This keeps the v3.4 label for the planned captive-ratio indicators.

---

## v3.38.5 — real Haiku root cause + Sonnet streaming guard + missing deep-tier commit

**Why this release**: The 16:25 UTC May 17 run completed in 358s (under the 25min timeout from v3.38.4 ✓), delivered an email (via fallback path ✓), and confirmed throttling holds (22/22 signals ✓). But three new issues showed up that I missed in prior releases:

1. **Haiku triage failed with a brand-new error mode**: "Expecting value (char 0)" → json_repair returned `str` not `dict`. Investigation revealed the actual root cause: a latent bug in `_strip_code_fence`. When Haiku emitted a properly closed code fence (```json\n{...}\n```), the function returned an empty string. The line `text.split("```", 2)[-1]` grabbed the empty string AFTER the closing fence instead of the JSON content BETWEEN the fences. This was not a prompt issue, not a max_tokens issue — it was a 12-line function with broken indexing logic. Has probably been silently degrading Haiku output for a long time depending on which fence style the model emitted.

2. **Sonnet was refused by the Anthropic SDK before making the call**: "Streaming is required for operations that may take longer than 10 minutes." The SDK has a client-side guard that demands streaming mode when max_tokens crosses an estimated 10-min wall-time threshold. My v3.38.4 bump to 24K crossed it. 16K stays comfortably under.

3. **Deep tier diagnostics weren't reaching the fast tier**: The user manually triggered the deep tier 3 hours before the fast tier run; deep tier produced data; fast tier still reported FAIL "deep tier hasn't run since v3.38 deploy." Root cause: `deep_tier.yml` commit step only stages `state/digests/` and `state/cache/`, missing the two root-level JSONs (`state/deep_tier_diagnostics.json`, `state/transcript_status.json`) added by the v3.38 deep_tier.py persistence work. Files written to runner, destroyed at shutdown, never reached origin/main. **Owning this one — I added the write code in v3.38 but never updated the commit step in the workflow.**

**Four fixes:**

1. **`src/analysis/llm_summarizer.py` — `_strip_code_fence` rewritten**. Explicit handling of three patterns (raw / closed-fence / unclosed-fence). Verified locally on all five common LLM-output shapes; all parse cleanly via stdlib now (no json_repair fallback even needed for the formerly-failing closed-fence case).

2. **`src/analysis/llm_summarizer.py` — Haiku prompt tightened**. Explicit "start with `{`, no markdown fences, no prose, no apologies" instructions. Belt-and-suspenders for the strip fix above.

3. **`src/config.py` — `SONNET_MAX_TOKENS: 24000 → 16000`**. Stays under SDK's 10-min streaming guard. json_repair fallback (v3.38.4) handles truncation risk that motivated the original bump.

4. **`.github/workflows/deep_tier.yml` — commit step now stages two missing JSONs**:
   ```yaml
   [ -f state/deep_tier_diagnostics.json ] && git add state/deep_tier_diagnostics.json
   [ -f state/transcript_status.json ] && git add state/transcript_status.json
   ```
   File-existence guards so the workflow doesn't break on runs where deep_tier.py errored before writing one of the files. Also rewrote two misleading "deep tier hasn't run" error strings in `src/fast_tier.py` to name both possibilities (didn't run / ran but didn't commit).

**To rescue this run's data**: trigger the deep tier manually after pushing v3.38.5. The fix will commit both JSONs on its first post-deploy run, and the next fast tier will pick them up.

**Validation done locally:**

- `_strip_code_fence` tested against all 5 common LLM output shapes — closed-fence case now passes stdlib parse cleanly (was returning empty string before)
- Syntax + imports clean on all 3 modified Python files
- Comprehensive AST signature audit: zero mismatches
- The fix to the actual bug means most runs won't even need the json_repair fallback going forward — stdlib will succeed on properly-fenced output

**Files changed:**

| File | Change |
|---|---|
| `.github/workflows/deep_tier.yml` | commit step now stages both root-level state JSONs |
| `src/analysis/llm_summarizer.py` | `_strip_code_fence` rewritten + Haiku prompt tightened |
| `src/config.py` | SONNET_MAX_TOKENS 24K → 16K + CHANGELOG |
| `src/fast_tier.py` | two error-message strings + CHANGELOG |
| `CHANGES.md` | this entry |

**What stays from prior releases:** json_repair fallback (still useful for truncation + unescaped chars in string values), fallback_analysis kwarg fix, expanded audit script, RSS pruning, plain Chrome UA, throttle, diagnostics wiring, 25-min workflow timeout.

**What's deliberately NOT fixed this release:**
- Sonnet streaming mode — deferred to v3.39. Would let us use 24K+ max_tokens. Bigger refactor.
- curl-cffi for Damnang + Investing.com x4 — deferred to v3.39.
- Migration to Anthropic tool_use for structured output — would eliminate the parse-fragility class of bug entirely.

---

## v3.38.4 — workflow timeout + JSON repair fallback + Cloudflare-blocked feeds dropped

**Why this release**: The 15:32 UTC May 17 run revealed two compounding bugs and exposed a misconfigured workflow timeout:

1. **Workflow killed at 12 min mark** (not a pipeline crash — GitHub Actions cancellation). The `.github/workflows/fast_tier.yml` had `timeout-minutes: 12` with a comment claiming "comfortable under GitHub's 15-min hard cap". **That comment is wrong** — GitHub Actions' actual hard cap is 6 HOURS (360 min). The 12-min limit was below the pipeline's own floor after v3.38.1 added the Layer 2 throttle (~4 min). This is THE primary cause of cancellation in this run.

2. **Haiku triage JSON parse failed again** at char 13926 — better than v3.38.3's char 6839, but still failing. After the v3.38.3 max_tokens bump (4K→8K), the news volume scaled to 50 items, requiring ~10K tokens of triage output. 8K was below floor. AND: char 13926 ≈ 3500 tokens is well under 8000 → likely NOT a truncation but malformed JSON (unescaped quote/newline in a string field).

3. **Cloudflare-fronted feeds still 403**: the v3.38.3 plain-Chrome UA fixed ~12 other previously-failing feeds, but Damnang Substack and Investing.com x4 remained 403. Cloudflare's bot detection uses TLS fingerprint (JA3/JA4) and HTTP/2 fingerprint, not just the UA string. Python requests has a recognizable TLS fingerprint regardless of UA.

**Five fixes:**

1. **`fast_tier.yml` — `timeout-minutes: 12 → 25`**. Plus corrected the misleading comment. Hard cap is 6h, not 15min. Current run profile: ~12-15 min total. 25 gives ~60% safety margin.

2. **`requirements.txt` — added `json-repair>=0.39.0,<1.0`**. Pure-Python, MIT, drop-in fallback for `json.loads()`.

3. **`src/analysis/llm_summarizer.py` — two-stage JSON parsing in both `haiku_triage_news` and `sonnet_analyze`**:
   ```python
   try:
       parsed = json.loads(raw)             # fast path
   except json.JSONDecodeError:
       parsed = json_repair.loads(raw)      # fallback: handles unescaped chars + truncation
   ```
   `json_repair` recovers from unescaped quotes/newlines in string fields (the v3.38.3 + v3.38.4 failure mode) AND from genuine truncation (returns best-effort partial dict). Functionally tested against the exact `"DDR5 prices "spike" 4% qoq"` malformation pattern that would have caused the v3.38.4 failure — all 3 items recovered.

4. **`src/config.py` — bumped max_tokens further (defensive)**:
   - `HAIKU_MAX_TOKENS: 8000 → 16000`
   - `SONNET_MAX_TOKENS: 16000 → 24000`
   - Both within each model's 64K hard cap. Per Anthropic docs, max_tokens doesn't factor into rate limits — no cost downside.

5. **`src/config.py` — RSS_FEEDS pruned 19→14**. Dropped Damnang Substack + Investing.com MU/NVDA/AMAT/ASML. The TLS-fingerprint issue isn't fixable with HEADERS alone. `TODO v3.39` updated in config.py with curl-cffi as the proper bypass approach for Damnang (the only one Raul flagged as critical).

**What stayed (working from prior releases):**
- v3.38: diagnostics wiring + per-source-row visibility (deep_tier.py)
- v3.38.1: Layer 2 sequential throttle (signal_reasoner.py) — 22/22 signal reasonings produced in this run, no 429s
- v3.38.2: RSS UA shift to requests+feedparser — most feeds now succeed
- v3.38.3: fallback_analysis kwarg fix, expanded audit script, dead-URL pruning

**Files changed:**

| File | Change | CHANGELOG updated? |
|---|---|---|
| `.github/workflows/fast_tier.yml` | `timeout-minutes: 12 → 25`, fixed comment | inline |
| `requirements.txt` | added `json-repair>=0.39.0,<1.0` | inline |
| `src/analysis/llm_summarizer.py` | json_repair fallback in two parse sites; new CHANGELOG block | yes (NEW block) |
| `src/config.py` | max_tokens bumps; RSS_FEEDS pruning; updated TODO | yes |
| `CHANGES.md` | this entry | — |

**Validation done locally:**

- Syntax + imports clean across all edited files
- `json_repair` resolves via import chain
- Functional test of exact failure mode: malformed JSON with unescaped quote inside string field → all items recovered
- Comprehensive AST signature audit: zero mismatches
- CHANGELOG freshness check: zero stale entries (every modified file with a CHANGELOG block now mentions v3.38.4)

**Expected next-run behavior:**

- Pipeline completes end-to-end within 25-min workflow budget
- Layer 2 still ~4 min (throttle floor unchanged)
- Layer 3 Haiku triage: max_tokens has 60% headroom; if output is malformed despite that, json_repair recovers
- Layer 3 Sonnet analysis: max_tokens has 40% headroom; same fallback if needed
- Validation table: 14 RSS sources tracked (was 19); Damnang + Investing.com x4 no longer generate 403s
- json_repair fallback log messages will tell us if/when this path fires (WARNING level on activation, INFO level confirming what was recovered)

**What's deliberately NOT fixed in this release:**

- AnandTech RSS "invalid token" — feed sporadically malformed at source; may self-heal
- Damnang Substack bypass via curl-cffi — deferred to v3.39 as a proper Cloudflare-bypass project (curl-cffi is a non-trivial dependency; needs benchmarking against requests for latency cost)
- Migration to Anthropic tool_use for structured output — the rigorous fix that would eliminate JSON parse fragility entirely; deferred to v3.39 as it's a larger refactor of both `haiku_triage_news` and `sonnet_analyze` prompts

---

## v3.38.3 — fallback_analysis crash fix + JSON truncation fix + comprehensive audit + RSS UA / dead-URL cleanup

**Why**: The 14:31 UTC May 17 run (the post-v3.38.2 run) revealed two new bugs and exposed that the v3.38.2 RSS UA fix only partially landed. Walking through what the log actually shows:

✓ The v3.38 + v3.38.1 + v3.38.2 fixes all held:
- Layer 2 throttling worked perfectly — "Layer 2 produced 22 signal reasonings" (no 429s, all 22 succeeded)
- News kwarg / DataFrame / `analyze_with_llm` / `generate_performance_chart` / `send_email` all bound correctly
- RSS UA shift gave us proper HTTP status codes per feed instead of the cryptic XML errors

✗ But two new bugs surfaced — both pre-existing in v3.37, masked by upstream crashes:

1. **`fallback_analysis(news_items, performance, big_movers)` at fast_tier.py:399** — function signature is `(performance_data, estimates_data, news_items, big_movers)`. Three positional args passed where four are needed, AND the order was wrong (news_items would have gone into performance_data slot, etc.). My previous audit script (v3.38.1) only checked **kwargs**, not **positional args** — so it missed this entire class of bug. Pre-existing since v3.37. Never manifested before because `analyze_with_llm` always returned successfully OR crashed before this line (v3.37 broken kwargs would have raised TypeError on the call itself; v3.38.1 fixed those, so the function ran and could now return None on JSON parse failure → fallback path activated → this bug surfaced).

2. **JSON parse failures from Haiku triage and Sonnet analysis** — both `Unterminated string` errors:
   - Haiku triage truncated around char 6839 → JSON parse failure → empty triage used
   - Sonnet analysis truncated around char 27099 → JSON parse failure → analysis = None → fallback path → **crash from bug #1**
   
   Root cause: max_tokens too low. `HAIKU_MAX_TOKENS=4000` triaging 46 news items at ~100-150 tokens of structured output each = needs ~5,500-7,000 tokens minimum. `SONNET_MAX_TOKENS=8000` synthesizing 22 indicator assessments at ~300-500 tokens each = needs ~6,600-11,000 tokens. Both ran out of room.

**Four fixes:**

1. **`fast_tier.py:399` — `fallback_analysis` call site.** Rewritten as kwarg-style with all four required args:
   ```python
   fallback_analysis(
       performance_data=performance,
       estimates_data=estimates_map,
       news_items=news_items,
       big_movers=big_movers,
   )
   ```

2. **`config.py` — `max_tokens` bumps.**
   - `HAIKU_MAX_TOKENS: 4000 → 8000` (well within Haiku 4.5's 64K hard cap)
   - `SONNET_MAX_TOKENS: 8000 → 16000` (well within Sonnet 4.6's 64K hard cap)
   - Per Anthropic docs: "The max_tokens parameter does not factor into OTPM rate limit calculations, so there is no rate limit downside to setting a higher max_tokens value." Cost is paid only on tokens actually generated — no penalty for headroom.

3. **`scripts/audit_signatures.py` — expanded to check positional args + arg count.** The v3.38.1 audit only checked unknown kwargs; missed positional bugs entirely. Now also detects:
   - Missing required positional args (the bug that just happened)
   - Too many positional args
   - Total arg-count exceeding function's positional capacity
   
   Re-running the expanded audit against the v3.38.3 working tree: **zero mismatches**. Should be wired into CI as a pre-deploy gate (`python scripts/audit_signatures.py`).

4. **`news.py` HEADERS + `config.py` RSS_FEEDS — UA hardening + dead-URL pruning.** The 14:31 log gave us actual HTTP status codes for each feed:
   - **403s (Substack, Investing.com) fixed via UA change**: replaced "Mozilla/5.0 (compatible; MemoryCycleBrief/1.0; ...)" with a plain Chrome UA. The "compatible;" pattern is the classic bot signal that Cloudflare and similar middleware match against. Plain Chrome UA passes their filters. Damnang and Investing.com x4 should now return content.
   - **404s pruned (13 feeds)**: Yonhap, Anthropic News, Meta AI Blog, SEMI press releases, Reuters Technology, Seeking Alpha Semis, The Elec English, Nikkei Asia, Pulse, vLLM Blog, SGLang/LMSYS, DeepSeek API News, Artificial Analysis. All confirmed 404 (or DNS failure) via real HTTP responses, no obvious public RSS replacement, and **all signal coverage preserved by existing Google News queries** (`KV cache compression`, `inference efficiency breakthrough`, `DDR5 spot price`, `TrendForce DRAM contract`, broker-name combos). Dropping these doesn't lose data — it removes the dead path and stops generating FAIL noise in the validation table.
   - **TODO v3.39** documented inline in `config.py` for the URL hunt — find working replacements where Google News coverage proves insufficient.

**Files changed:**

| File | Change |
|---|---|
| `src/fast_tier.py` | fallback_analysis call site rewritten as kwarg form |
| `src/config.py` | HAIKU_MAX_TOKENS 4000→8000; SONNET_MAX_TOKENS 8000→16000; RSS_FEEDS pruned from 32 to 19 entries |
| `src/data/news.py` | HEADERS now uses plain Chrome UA (no "compatible;" bot signal); Accept + Accept-Language added |
| `scripts/audit_signatures.py` | expanded to check positional args + arg count + missing required, not just unknown kwargs |
| `CHANGES.md` | this entry |

**Validation:**

- Comprehensive AST audit: ZERO mismatches across all of `src/` (positional + kwargs + count)
- Syntax + import test on all four edited files: clean
- The original v3.38.3 bug (`fallback_analysis` missing positional arg) is now FLAGGED by the expanded audit if it were re-introduced — verified by temporarily reverting the fix and re-running

**Expected behavior on next run:**

- Pipeline completes end-to-end (fallback path no longer crashes; main path no longer truncates)
- Layer 2: 22/22 signal reasonings (throttle held; just slower at 7s spacing)
- Layer 3: Haiku triage + Sonnet analysis both complete with valid JSON (max_tokens has 2× headroom)
- Validation table: ~19 RSS sources tracked instead of 32, with most rows green; Damnang and Investing.com x4 should flip from FAIL to OK now that UA is browser-shaped
- Total wall-time: ~5-6 minutes (Layer 2 throttle is the floor)

**What's deliberately NOT fixed in this release:**

- AnandTech RSS "invalid token" errors — feed is sporadically malformed at source; may self-heal
- KED Global occasional 502 — transient server errors; built-in retry will handle
- Manual RSS URL hunt for the 13 pruned feeds — deferred to v3.39 as a quality improvement, since Google News queries already cover the signals

---

## v3.38.2 — RSS user-agent fix: ~15 feed failures from the 13:42 UTC run traced to a single root cause

**Why**: 15 of 20 RSS feeds parse-failed in the 13:42 UTC May 17 run with three distinct error patterns (`"...is not an XML media type"`, `"<unknown>:2:0: syntax error"`, `"<unknown>:N:M: not well-formed (invalid token)"`). Investigation traced all three patterns to a single root cause: `feedparser.parse(url)` uses feedparser's library-default User-Agent (`UniversalFeedParser/6.x +https://github.com/kurtmckee/feedparser/`), which Substack, Cloudflare-fronted sites, and various anti-bot middleware treat as a bot — they respond with an HTML "are you human" page or just silently 200 with HTML body. feedparser then can't parse the HTML as XML and emits one of the three error families.

Per Raul: "stocks ALWAYS have fresh data". The feeds aren't dry — we're being filtered out at the edge.

The fix is one-line at root: route through `requests.get(url, headers=HEADERS, timeout=20)` (using the existing browser-UA HEADERS dict from `news.py`), then pass `resp.content` to `feedparser.parse(bytes)`. EDGAR was already doing this correctly (lines 124/140). RSS and Google News were not. Fixed both.

**Why this should fix most-but-not-all of the failures:**

| Failure pattern from 13:42 UTC log | Affected feeds | Fix |
|---|---|---|
| Server returns HTML page → `"...is not an XML media type"` | vLLM, SGLang, Artificial Analysis | UA fix; if URL also moved, will need separate fix |
| Server returns 4xx with HTML body → `"<unknown>:2:0: syntax error"` | Yonhap, The Elec, Pulse, DeepSeek | UA fix |
| Cloudflare challenge page → `"<unknown>:N:M: not well-formed (invalid token)"` | AnandTech, Anthropic, Meta AI, SEMI, Investing.com ×4, Damnang, Seeking Alpha, Nikkei | UA fix |
| Pedantic content-type rejection → `"xml;charset=UTF-8 is not an XML media type"` | Korea Herald | UA fix may or may not help; if site sends weird content-type for everyone, needs explicit override |
| `Name or service not known` | Reuters | DNS blip; transient, not a fix needed |

So one fix likely covers ~18 of 20 failed feeds. Any remaining failures after the next run will be a small enough set to debug individually (likely some combination of: feed URL actually moved, requires JS rendering, or genuinely dead). At that point we go feed-by-feed per Raul's original plan.

**Files changed:**

| File | Change |
|---|---|
| `src/data/news.py` | `_safe_parse_rss_feed`: replace `feedparser.parse(url)` with `requests.get(url, headers=HEADERS)` + `feedparser.parse(content)`. Same fix for `fetch_google_news`. Refactored Google News with/without-collector branches into a single `_fetch_and_parse` inner function for clarity. |
| `CHANGES.md` | this entry |

**Confidence statement:**

This is a high-confidence root-cause fix for the *category* of failures, **not empirically tested feed-by-feed** — I couldn't fetch the failing URLs directly from the sandbox (network restricted). Tomorrow's first cron run is the empirical test. The hypothesis is strong because (1) the HEADERS dict was specifically added for this purpose and is already used by other scrapers in the same file; (2) the three error-message families match exactly what UA-based filtering produces; (3) Damnang's site was confirmed alive and posting via web search.

**What this fix doesn't address:**

- Feed URLs that have *genuinely moved* (e.g., a site switched RSS endpoints). The next run's diagnostics will identify which feeds are still failing, and those become the v3.39 manual-fix list.
- The `Reuters Technology` DNS failure (transient — separate concern).
- Feed `content-type` headers that some sites set in ways feedparser remains strict about even with browser UA (Korea Herald is the candidate). If still failing after this, we can pass `response_headers` override to feedparser.

---

## v3.38.1 — Three pre-existing signature mismatches in `fast_tier.py` (surfaced when v3.38 unblocked upstream crashes) + Layer 2 rate-limit throttling

**Why**: First post-v3.38 run (13:42 UTC May 17) reached `analyze_with_llm(...)` at fast_tier.py:385 — further than any prior run since at least v3.37 — and crashed with `TypeError: analyze_with_llm() got an unexpected keyword argument 'estimates'`. Three pre-existing call-site bugs were sitting downstream of where the v3.37 pipeline used to crash. Classic bug cascade: an upstream crash was masking three downstream ones. None caused by the v3.38 edits — verified by diffing `/tmp/src/fast_tier.py` (v3.37 as deployed) against v3.38 working tree. Same broken call site at the same kwarg names in both.

Plus the same run hit Haiku 4.5's 50,000 input-tokens/min rate limit during Layer 2: the concurrent `ThreadPoolExecutor(max_workers=6)` path burst at ~120k tokens/min, lost 3/22 signals to 429s after the SDK ran out of retries. Per Raul: "do the fix on throttling now, so it makes it a bit slower which is not an issue but we get the data." Bundled into this release rather than piecemealing.

After the failures I ran a system-wide AST-level kwarg audit (would have caught all three call-site bugs pre-deploy). Kept it as `scripts/audit_signatures.py` — should run as a CI gate.

**Four fixes:**

1. **`fast_tier.py:385 → analyze_with_llm(...)`** — three kwarg names were wrong:
   - `estimates=` → `estimates_data=` (matches function signature)
   - `performance=` → `performance_data=`
   - `layer2_input=layer3_input` → **dropped entirely**; the kwarg has never existed on `analyze_with_llm`. Also dropped the now-unused `layer3_input` dict construction (signal_reasonings, naver_summary, finnhub_summary, dart_summary, etc.) since nothing consumes it.

   **Pre-existing architectural note worth surfacing**: Sonnet has *never* received signal_reasonings or the naver/finnhub/dart summaries. The `layer2_input=` kwarg was a TODO masquerading as working code ("backward-compatible — old llm_summarizer ignores if None") — but the function signature simply doesn't accept it, so the call has always raised TypeError. Pipeline always crashed earlier, so this never manifested. Sonnet has been making its executive summary based only on news triage + performance + estimates + big_movers + prior_state. If Sonnet should see Layer 2 reasonings, that's a follow-up feature decision, not a bug fix.

2. **`fast_tier.py:400 → generate_performance_chart(...)`** — two problems:
   - `collector=collector` kwarg doesn't exist on the function (signature is `(history_baseline100, title="...")`). Removed.
   - `WIDER_BASKET` was a spurious positional arg that landed in `title:str` — would have rendered a chart titled `['NVDA', 'MU', 'SK Hynix', ...]`. Removed; default title used.

3. **`fast_tier.py:430 → send_email(...)`** — `collector=collector` kwarg doesn't exist (signature is `(subject, html_body, recipient=None)`). Removed.

4. **`signal_reasoner.py:reason_all_signals` — Layer 2 throttling.** Replaced `ThreadPoolExecutor(max_workers=6)` concurrent fan-out with sequential loop + `time.sleep(7.0)` between calls. Math:
   - Per-call input tokens ≈ 5,000 (system + indicator + 10 digests + news + signal_specific cap)
   - 50,000 tokens/min ÷ 5,000 tokens/call = 10 calls/min ceiling → 6s minimum spacing
   - Using 7s for safety margin against per-call token variance
   - 22 signals × ~2s Haiku latency + 21 gaps × 7s = ~191s ≈ 3.2 min for Layer 2 (was ~37s concurrent)
   - Effective rate: ~34,555 tokens/min — comfortable 31% margin under the 50k limit
   - Total fast-tier runtime moves from ~2 min to ~5 min. Worth it for not losing signals.
   - `max_workers` kwarg retained for backward-compat; `inter_call_delay_seconds=7.0` is the new lever.

**Files changed:**

| File | Change |
|---|---|
| `src/fast_tier.py` | analyze_with_llm / generate_performance_chart / send_email call sites corrected |
| `src/analysis/signal_reasoner.py` | concurrent → sequential with 7s throttle; `time` import added |
| `CHANGES.md` | this entry |
| `scripts/audit_signatures.py` | **new** — pre-deploy AST audit |

**Validation:**

- System-wide AST audit of kwarg call sites: ZERO mismatches remaining
- Live smoke test of all three fixed call sites — all bind successfully; `generate_performance_chart` produced valid 77KB PNG; `analyze_with_llm` returned None gracefully on no-API-key; `send_email` returned False gracefully on no-SMTP-creds
- Throttle empirically tested with mocked Haiku at 0.5s spacing: 22 signals × 0.5s = exactly 10.5s elapsed, gap precision 500ms ±0ms
- Throttle disabled (0s) confirmed as zero-overhead no-op
- Syntax + import test on both edited files: clean

**On the ~15 RSS feed failures from the 13:42 UTC run** (separate concern from this release, but worth tracking for v3.39):

Yes, parse failure = zero items from that source today. Diagnostics now make this loud (v3.38), but the failures themselves are upstream — sites changing URLs, returning HTML instead of XML, encoding bugs, or DNS blips. Categorized by impact:

| Source | Why failed | Impact |
|---|---|---|
| Damnang Memory Substack | invalid token | **UNIQUE source** — independent memory analyst; loss = real signal loss |
| The Elec English | syntax error | **UNIQUE** — Korean memory industry press |
| Anthropic News, Meta AI, DeepSeek, vLLM, SGLang, Artificial Analysis | wrong content-type / invalid token | **UNIQUE** — AI-lab efficiency signal (t1_efficiency_shock) |
| SEMI press releases | invalid token | mostly redundant with Google News for SEMI announcements |
| AnandTech | invalid token | partially redundant with Tom's Hardware RSS |
| Yonhap, Korea Herald, Pulse | wrong content-type / syntax | redundant — Korean press covered by KED Global, Naver, ChosunBiz |
| Investing.com MU/NVDA/AMAT/ASML | invalid token | redundant — each ticker covered by Google News + EDGAR + Finnhub |
| Reuters Technology | DNS not found | transient (one-off blip) — retry tomorrow |
| Seeking Alpha Semiconductors | invalid token | partial loss — some unique analyst commentary |
| Nikkei Asia Companies | invalid token | redundant for memory news; Japan-specific business news partial loss |

So out of ~15 failures, roughly half represent genuinely lost unique data today, half are redundantly covered. **Action item for v3.39 (not this release)**: walk every failed URL, decide replace / refresh / drop. Particularly Damnang and The Elec — both unique memory-industry sources.

---

## v3.38 — Pipeline crash fixes + comprehensive diagnostics across fast tier + deep tier

**Why**: The 11:30 UTC fast-tier run on May 17 (this morning) crashed with exit 1. Two real bugs and one latent. Investigation also surfaced that the silent-failure surface was much larger than the email's validation table reflected — three fast-tier sources only recorded successes (Naver, Finnhub, DART), and the entire deep tier (8 sources: Yahoo / Motley Fool / IR scrapers / Mirae / NH / Samsung Securities / Haiku digester) produced **zero diagnostic rows**. Per Raul's commitment to check the brief only periodically once stable, silent failure = data loss without notification — unacceptable. Package B from the v3.38 design discussion: fix the crash, close every silent-failure path.

**Changes:**

1. **`src/fast_tier.py` line 160 — news kwarg fix.** `fetch_all_news(RSS_FEEDS, GOOGLE_NEWS_QUERIES, EDGAR_FEEDS, collector=collector)` passed `EDGAR_FEEDS` (a dict) into the third positional parameter `hours_back: int`, producing `TypeError: unsupported type for timedelta hours component: dict` inside the RSS feed fetcher's `cutoff = datetime.now() - timedelta(hours=hours_back)`. Fixed with explicit kwarg `edgar_feeds=EDGAR_FEEDS`. Bug pre-dates v3.37; was caught by `_run()` so news ran empty rather than crashing the pipeline — but news being silently empty for an indeterminate number of past runs is itself the silent-failure problem this release exists to solve.

2. **`src/fast_tier.py` line 169 — pandas DataFrame None-fallback.** `fetch_results.get("prices") or {}` triggered `ValueError: The truth value of a DataFrame is ambiguous` because `fetch_price_history()` returns a `pd.DataFrame`, not a dict. The `or` operator forces a truthiness check pandas refuses to evaluate. Rewritten to `_prices = fetch_results.get("prices"); price_history = _prices if _prices is not None else pd.DataFrame()`. Added `import pandas as pd` to fast_tier imports. **This was the line that actually killed today's run.**

3. **`.github/workflows/fast_tier.yml` — `issues: write` permission.** The "Create issue on critical failure" step at the end of the workflow uses `github.rest.issues.create` but the workflow only declared `contents: write`. So when the run crashed today, the fallback issue-creation step *also* failed with `HttpError: Resource not accessible by integration`. Net effect: pipeline died with zero notification — neither a brief nor an issue. Added `issues: write` so the fallback hook actually fires.

4. **`src/fast_tier.py` — 8 `collector.add(...)` → `collector.record(...)` fixes (latent bug).** `DiagnosticsCollector` only exposes `record()`, not `add()`. Eight calls would have raised `AttributeError` if reached; the surrounding `try/except` blocks would have swallowed them, meaning validation-table rows that *should* have been recorded for Naver / Finnhub / DART / transcript availability / hyperscaler capex have likely been silently missing for some unknown number of past runs. Caught while editing the same surface; safer to fix now than leave latent.

5. **`src/fast_tier.py` `_run()` — record FAIL row on top-level fetcher crash.** Previously, when a fetcher lambda threw, `_run()` logged the error and returned `None`. Nothing was written to the collector, so the email's validation table showed *no row at all* for that subsystem — a silent loss. Now records a `SourceFetchStatus(reached=False, error=...)` with a descriptive `source_name` per label (e.g. `"News aggregator (RSS + Google News + EDGAR)"`, `"Yahoo Finance / yfinance batch (prices)"`) so a top-level crash is unambiguous in the table. Sub-source rows recorded *before* the crash remain visible too — you get a partial picture plus a FAIL row for the aggregator, which is the right outcome.

6. **`src/fast_tier.py` — Naver / Finnhub / DART record FAIL when wrapper returns None.** The previous pattern `if naver_data: collector.add(...)` only recorded rows on success. Failures (when `fetch_X_with_retry` returns `(None, {status: "failed", ...})`) produced zero rows — silent again. Now every wrapper result emits a row via the new `status_from_retry_meta()` helper, which interprets the `(payload, meta)` tuple from `fetch_with_retry`: `status="ok"` → green; `status="cached_stale"` → green-with-loud-detail (`"USING STALE CACHE — N day(s) old"`); `status="failed"` → red FAIL row; `status="skipped"` → grey skipped row.

7. **`src/diagnostics.py` — three new helpers for cross-tier diagnostics.**
   - `status_from_retry_meta(source_name, source_type, payload, meta, items_count, items_detail)` — the converter described above. Used by both fast tier and deep tier for any `fetch_*_with_retry` call.
   - `statuses_to_dict_list(statuses)` — serialize a list of `SourceFetchStatus` for JSON persistence across pipeline tiers.
   - `statuses_from_dict_list(payload)` — re-hydrate. Tolerates missing/malformed entries (returns empty list rather than raising).

8. **`src/deep_tier.py` — full `DiagnosticsCollector` wiring (no prior instrumentation existed).** The deep tier ran with zero diagnostic rows surfaced — `transcript_status.json` covered per-company transcript availability but missed everything else. Now:
   - One `DiagnosticsCollector` per deep-tier run.
   - `_record_transcript_source_rows()` decomposes the transcript cascade by source (`Motley Fool`, `Yahoo Finance`, `IR scrapers (SK Hynix / Samsung / Micron)`) with detail showing per-source breakdown (`13 co · 13 full / 0 materials-only / 0 cached / 0 nothing`). If the wrapper itself fails, one FAIL row with the wrapper's error.
   - Per-broker rows from `fetch_mirae_with_retry` / `fetch_nh_with_retry` / `fetch_samsung_securities_with_retry` (each one independently failable; not interchangeable — Samsung Securities is the only Gmail-IMAP source).
   - Haiku digester roll-up row with three health states: `ok` (digests written), `fail` (sources had content but zero digests — digester died), `ok` (quiet day, zero digests but zero source content too — legitimate).
   - All rows persisted to `state/deep_tier_diagnostics.json` via `_write_deep_tier_diagnostics()`. Schema: `{written_at_iso, run_date, summary_counts, statuses: [...]}`. Written unconditionally so the fast tier can detect "deep tier didn't run today" vs "deep tier ran and everything failed."

9. **`src/fast_tier.py` — `_surface_deep_tier_diagnostics()` ingests deep-tier rows.** Reads `state/deep_tier_diagnostics.json`, re-hydrates via `statuses_from_dict_list()`, appends to fast-tier collector. Staleness check: if `run_date != today`, emits a single WARN row pointing that out (deep tier didn't run today — itself a signal). Called right after `_surface_transcript_status()` so all transcript-related rows appear together in the validation table.

10. **`src/email_module/template.py` — three new `_SOURCE_TYPE_LABELS` entries.** `api` → "External API" (Finnhub, DART), `transcript` → "Earnings transcript source" (Yahoo / Motley Fool / IR), `broker_pdf` → "Korean broker note feed" (Mirae, NH, Samsung Securities). Without these the new rows would group under raw lowercase type-string headings.

**Files changed:**

| File | Changes |
|---|---|
| `src/fast_tier.py` | news kwarg fix; pd.DataFrame None-fallback; `_LABEL_META` + `_run()` FAIL recording; naver/finnhub/dart record-on-failure via `status_from_retry_meta`; `_surface_deep_tier_diagnostics()` function; 8 `.add` → `.record`; new imports (pd, helpers) |
| `src/deep_tier.py` | `DiagnosticsCollector` wiring; `_record_transcript_source_rows()` per-source decomposition; per-broker rows; Haiku roll-up row; `_write_deep_tier_diagnostics()` persistence |
| `src/diagnostics.py` | new helpers `status_from_retry_meta`, `statuses_to_dict_list`, `statuses_from_dict_list` |
| `src/email_module/template.py` | added `api` / `transcript` / `broker_pdf` to `_SOURCE_TYPE_LABELS` |
| `.github/workflows/fast_tier.yml` | added `issues: write` permission |

**Validation:**

Empirically tested locally before zip:
- Syntax check (ast.parse) on all four edited files — pass.
- Import check on `fast_tier`, `deep_tier`, `diagnostics` — pass.
- `status_from_retry_meta` classification across all 5 status paths (ok / cached_stale / failed / skipped / unknown) — correct health flag in each.
- Round-trip serialization via `statuses_to_dict_list` → `statuses_from_dict_list` preserves failure error messages.
- Today's exact bug reproduced (`fetch_all_news` with `EDGAR_FEEDS` as positional 3rd arg) confirmed signature in `news.py` — kwarg fix is the right shape.
- DataFrame None-fallback verified for both populated DataFrame and `None`.
- `_run()` failure path verified with a lambda that raises the exact `TypeError` from today's logs — FAIL row written with descriptive source_name and full error message.
- Cross-tier persistence: deep_tier_diagnostics.json round-trip preserves FAIL rows including error fields.
- Staleness check: `run_date != today` correctly detected.

**Not yet validated empirically:**
- Actual deep-tier run end-to-end (requires Anthropic API + proxy + real network — needs production workflow_dispatch).
- Email rendering of new source rows in the validation table (requires real send).

**Recommended deploy sequence:**
1. Push v3.38, manual `workflow_dispatch` on `deep_tier.yml` first → produces `state/deep_tier_diagnostics.json`.
2. Then manual `workflow_dispatch` on `fast_tier.yml` → email should arrive with the new rows in the validation table.
3. First few cron runs: check the table carefully (per Raul: "the first runs i will certainly look all the way") — confirm Mirae/NH/Samsung Securities/Haiku rows all appear with sensible items_returned counts.

---

## v3.37 — Haiku input cap bumped to 80k + IR URL cache (no more daily re-digestion of unchanged materials)

**Why**: v3.36 deploy (May 17, 10:25 UTC) succeeded — Motley Fool flipped to 13/13 full_transcript, SK Hynix IR returned materials_only via the new WordPress RSS path, Yahoo proxy bandwidth dropped ~95% as the cascade demoted Yahoo to fallback for Korean names only. Two follow-ups flagged in the v3.36 review:

1. **Three transcripts just clipped the 60k char cap**: Meta (60,304 → 60,000), Dell (61,428 → 60,000), HPE (60,883 → 60,000). Loss of ~0.5-2.4% of content each — small but unnecessary, since Haiku's 200k window has ample room.

2. **IR materials get re-digested every daily run.** Samsung's earnings release and SK Hynix's `q1-2026-business-results/` post are unchanged content but the IR scrapers re-fetch and re-digest them every day until a new quarter lands. That's ~90 days × 2 companies = ~180 wasted Haiku calls per cycle. The Yahoo + Motley Fool path already has a URL cache (`state/cache/transcripts_seen.json` from v3.33) that returns `already_processed` for known URLs — IR was never wired in.

**Changes:**

1. **`src/analysis/digester.py`** — `MAX_INPUT_CHARS` bumped from `60_000` to `80_000`. Comment updated to reflect the cycle's growing transcript sizes (some now 60-65k after the v3.35 Yahoo paragraph parser captures the full call rather than a stub). 80k ≈ 20-25k tokens, still well within Haiku's 200k context with room for the prompt and structured output.

2. **`src/data/transcript_sources.py`** — extend `seen_urls` cache to IR scrapers:
   - `_scrape_ir_rss_feed(company, cfg, seen_urls)` — new `seen_urls` parameter. After matching the newest earnings feed entry, check if `entry_link` is in the cache. If yes, return an `already_processed` stub immediately (preserves `event_date_iso` from the feed entry's `published_parsed` for cross-run identity).
   - `_scrape_ir_site(company, cfg, days_back, seen_urls)` — new `seen_urls` parameter. Inside the per-candidate fetch loop, cache check before `_http_get(href)`. Mirrors the v3.33 Motley Fool / Yahoo pattern.
   - `fetch_ir_transcripts(ir_sources, days_back, seen_urls=None)` — optional `seen_urls` parameter, defaults to empty set for backward compatibility. Forwards to `_scrape_ir_site`.
   - `fetch_all_transcripts` aggregator — already loads `seen_urls`; now passes it to `fetch_ir_transcripts` (was the missing wire).
   - The existing persist block (`for r in results: if r.availability in ("full_transcript", "materials_only"): seen_urls.add(r.source_url)`) already covered IR URLs — no change needed there.

| File | Changes |
|---|---|
| `src/analysis/digester.py` | `MAX_INPUT_CHARS` 60_000 → 80_000, comment updated |
| `src/data/transcript_sources.py` | `_scrape_ir_rss_feed`, `_scrape_ir_site`, `fetch_ir_transcripts` take `seen_urls`. Cache-hit branches added in both IR paths. Aggregator wires `seen_urls` through. Changelog block updated |
| `CHANGES.md` | This entry |

**Expected v3.37 deploy outcome:**
- Day 1 (next deploy): SK Hynix IR cache-hits the `q1-2026-business-results/` URL persisted by v3.36's run. Returns `already_processed`, no Haiku call. Samsung IR cache-hits its two materials URLs. Aggregate flips to roughly `13 full, 0 materials-only, 4-5 already-processed, 1 nothing`.
- Day until Q2 reports (~late July 2026): IR materials stay cached, ~zero Haiku tokens spent on unchanged content.
- Day N (Q2 release lands): WordPress feed surfaces new `q2-2026-business-results/` post, URL not in cache, fresh digest. Cache updates. Resumes ~90 day quiet period.
- Three previously-truncated transcripts (Meta, Dell, HPE) now fit under 80k → no truncation.

---

## v3.36 — Motley Fool body selector + SK Hynix IR via WordPress RSS

**Why**: v3.35 deep-tier run (May 17, 09:22 UTC) flipped Yahoo from 0/15 to 14/15 full transcripts (huge win), but Motley Fool stayed at 13/13 `nothing` and SK Hynix IR stayed at `nothing`. Both diagnosed empirically from live pages provided in-session:

1. **Motley Fool — wrong body selector.** Raul pasted the DevTools view of the Applied Materials AMAT Q2 2026 transcript page. The transcript body wraps in `<div id="article-body-transcript" class="article-body transcript-content">` — a stable explicit ID. But the v3.34 selector was `psoup.find("article") or psoup.find("div", class_=re.compile("article|content|body"))`. The first call, `find("article")`, matches the FIRST `<article>` tag in the document — which on fool.com transcript pages is a "Read Next" recommendation card at the bottom (e.g. "Cheap Tech Stocks for 2026 and How to Invest", ~57 chars). That short text falls below the 400-char `materials_only` threshold and the result is `nothing`. The actual transcript body (~50K chars) was never reached because `find("article")` matched the wrong thing first. v3.35's diagnostic logging didn't fire because the URL regex IS finding transcript URLs at the quote-page stage — the failure was downstream at body parsing, an instrumentation gap. Locally validated v3.36 selector on a synthetic fixture mirroring the AMAT page: v3.34 selector → 57 chars; v3.36 selector → 3115 chars of clean transcript text (real pages will be ~50K chars).

2. **SK Hynix IR — wrong URL + JS shell.** Two compounding bugs. (a) Config pointed at `skhynix.com/ir/UI-FR-IR15`, which is a different IR subsection; the correct earnings landing is `UI-FR-IR06`. (b) But fixing the URL alone doesn't help: `UI-FR-IR06` is a JS shell — the "Earnings Release" and "Conference Call" PDF buttons resolve to `/webundefined` placeholders in static HTML. Real URLs are injected by JavaScript only after page load. The win: `news.skhynix.com` is a fully server-rendered WordPress newsroom. Posts under category IR (e.g. `q1-2026-business-results/`) contain the entire earnings release in plain HTML: revenue, operating profit, operating margin, forward commentary on agentic AI / HBM4 / LPDDR6 / NAND, capex plans (M15X, Yongin cluster, EUV). And WordPress always exposes `/feed/rss/` as a standard RSS endpoint — confirmed live. v3.36 switches `IR_SOURCES["SK Hynix"]["ir_url"]` to that feed.

**Local validation before commit** (continuing the empirical-validation commitment): synthetic AMAT fixture mirroring the live page → v3.36 MF selector extracts 3115 chars (real pages = ~50K). News.skhynix.com WordPress structure confirmed live via web fetch (server-rendered, no JS required).

3. **`src/data/transcript_sources.py`**:
   - `_scrape_motley_fool_ticker` body selector: replaces v3.34's `find("article") or find("div", class_=re.compile("article|content|body"))` with `find("div", id="article-body-transcript")` plus a fallback that anchors on the `article-body` + `transcript-content` class combination. If a future fool.com redesign breaks both anchors, the scraper degrades to `nothing` with logged warning rather than silently returning page-chrome text as it did in v3.34/v3.35.
   - New `_scrape_ir_rss_feed(company, cfg)` helper above `_scrape_ir_site`. Detects RSS feeds by URL pattern (`/feed/` in path), uses feedparser, picks the newest feed entry matching earnings keywords (business-results, financial results, quarterly results, q1-q4, 1q-4q), pulls full content from `<content:encoded>` when present else falls back to fetching the post URL. Sets `event_date_iso` from `entry.published_parsed` for stable cross-run identity. Caps to `materials_only` via existing `transcript_capable=False` flag.
   - `_scrape_ir_site` gets a small early-branch check: if `ir_url` is an RSS feed, dispatch to `_scrape_ir_rss_feed`. Existing logic for non-RSS IR pages unchanged — Samsung's path is untouched.

4. **`src/config.py`**: `IR_SOURCES["SK Hynix"]["ir_url"]` changed from `https://www.skhynix.com/ir/UI-FR-IR15` to `https://news.skhynix.com/feed/rss/`. Inline comment block documents the two bugs and the WordPress-RSS rationale.

| File | Changes |
|---|---|
| `src/data/transcript_sources.py` | `_scrape_motley_fool_ticker` body selector rewritten. New `_scrape_ir_rss_feed` helper added above `_scrape_ir_site`. `_scrape_ir_site` gets RSS-feed dispatch branch at the top. Changelog block updated |
| `src/config.py` | `IR_SOURCES["SK Hynix"]["ir_url"]` switched to news.skhynix.com WordPress RSS feed. Inline comment documents the JS-shell rationale |
| `CHANGES.md` | This entry |

**Expected v3.36 deploy outcome:**
- Motley Fool: flips from 13/13 `nothing` to most/all tickers returning `full_transcript`. MF becomes the primary US transcript source again (per v3.33 architectural plan), Yahoo demotes to fallback. Yahoo proxy bandwidth drops accordingly.
- AMAT: now covered by MF (Yahoo's 404 on the stale article ID 543062 becomes irrelevant — MF has the May 14 Q2 2026 transcript).
- SK Hynix IR: flips from `nothing` to `materials_only` with the actual earnings release text (revenue / op profit / forward commentary). The Yahoo HY9H.MU transcript is still the call commentary source; SK Hynix IR is the additive slide-deck / release signal.

**Open items deliberately deferred:**
- Daily re-digestion of unchanged IR materials. The IR scraper (both old path and new RSS path) re-fetches and re-digests the SK Hynix / Samsung IR pages every run, burning Haiku tokens on identical content until a new quarter lands. Same behavior as pre-v3.36 — not new. Fix would extend the `transcripts_seen.json` cache pattern to IR sources, keyed on post URL. Pre-existing issue.
- AMAT quoteSummary URL discovery: removed from roadmap now that MF covers AMAT.

---

## v3.35 — Yahoo parser rewrite (DOM test-id extraction + Scout AI capture) + Motley Fool diagnostic logging

**Why**: v3.34 deep-tier run (May 17, 07:15 UTC) showed Yahoo returning 200 OK on every fetch (proxy working, redirects working, canonical tickers correct) but 0/15 `full_transcript` results. v3.34's diagnostic logging revealed why: the bs4 selector `psoup.find("article") or div.body|content|caas` was returning <400 chars on 3 MB pages. Investigation against `/tmp/yahoo_nvda.html` (NVDA Q4 FY2026, 3.6 MB, fetched via Bright Data proxy) confirmed the transcript text lives in `<p data-testid="typography" class="...type-paragraph-md-reg...">` tags, not inside an `<article>` wrapper. Yahoo also embeds a Scout AI structured analysis (TLDR, 12 key_insights sections, 5 labeled quotes, 11 Q&A exchanges) in a SvelteKit hydration script block at `mad/v2/analyst/ect` — high-quality signal that was being silently discarded.

**Local validation before commit** (per the explicit "no more shipping without parsing actual content locally first" commitment from the v3.35 handoff brief): on `/tmp/yahoo_nvda.html`, the new parser extracts 90 timestamp-paired paragraphs (51,990 chars text), all 12 Scout AI insight sections, 5 quotes, 11 Q&A exchanges. First paragraph is operator greeting at 0:00:00; last is operator closing at 1:06:32. Cross-checks pass.

1. **Inlined helpers in `src/data/transcript_sources.py`** (above `_scrape_yahoo_ticker`, matching the inline pattern used by Motley Fool and IR scrapers):
   - `_yahoo_extract_paragraphs(html)` → `[{"timestamp": "0:33:21", "text": "..."}, ...]`. Anchored on the stable `data-testid="typography"` test-automation hook and the stable `type-paragraph-md-reg` class prefix. The volatile runtime hash suffix (`yf-gjjls9` and similar) is deliberately ignored. Dedupes by content (each paragraph appears twice on Yahoo's page — SSR'd DOM + SvelteKit hydration data — empirically confirmed via `grep -c` on a unique phrase: count = 2).
   - `_yahoo_paragraphs_to_text(paragraphs)` → renders as `[H:MM:SS]\\n<text>\\n\\n[H:MM:SS]\\n<text>` for `TranscriptResult.text`.
   - `_yahoo_extract_analysis(html)` → Yahoo Scout AI's structured analysis dict from the embedded SvelteKit block. Double-decode (wrapper JSON has `body` as JSON-encoded string).

2. **`_scrape_yahoo_ticker` patched**: replaces lines 740-743 (the failing bs4 selector) with calls to `extract_paragraphs` + `extract_analysis`. New diagnostic log line per ticker: `paragraph count, text length, Scout AI presence`. The length-based `_classify_availability` heuristic unchanged — 52K chars on a real call → `full_transcript`.

3. **`TranscriptResult.analysis_json: Optional[dict] = None`** new field. Captures Scout AI analysis when Yahoo provides it (and only Yahoo provides it). Current deep-tier Haiku digester ignores unknown fields, so this is non-breaking. v3.5 will wire it into the email template proper.

4. **Motley Fool diagnostic logging** mirroring Yahoo's v3.34 anchor-count pattern. When `_scrape_motley_fool_ticker` finds zero transcript links, the new log line records `page_html_len`, `total_hrefs`, and up to 5 `near_miss_transcript_hrefs` (anchors that contained "transcript" but didn't match the strict pattern). v3.34 produced 13/13 "nothing" from MF with zero diagnostic output — this gives the next deploy actionable signal on whether MF served a real page, served a JS shell, or changed its URL pattern.

| File | Changes |
|---|---|
| `src/data/transcript_sources.py` | `import html as html_module` added. New helpers inlined above the Yahoo section: `_yahoo_extract_paragraphs`, `_yahoo_paragraphs_to_text`, `_yahoo_extract_analysis`, plus the regex constants and stability notes. `TranscriptResult` gains `analysis_json: Optional[dict] = None`. `_scrape_yahoo_ticker` replaces the bs4 article/body selector (3 lines) with calls into the inlined helpers; logs paragraph count + text length + scout_ai_analysis presence; threads `analysis_json` into the returned result. `_scrape_motley_fool_ticker` adds a diagnostic log before the "no transcript links" return showing page length, total anchor count, and near-miss transcript hrefs. Changelog block updated |
| `CHANGES.md` | This entry |

**Stability assumptions worth flagging.** The DOM selectors anchor on `data-testid="typography"` (test-automation hooks, designed to be stable across redesigns) and `type-paragraph-md-reg` (semantic CSS class). If Yahoo ships a major redesign that changes either of these, this scraper degrades to `nothing` and logs a 0-paragraph diagnostic — same defensive failure mode as v3.34. The SvelteKit `mad/v2/analyst/ect` API path is Yahoo internal but published in the page's hydration data; treated as same-stability as the DOM selectors.

**Coverage caveat to confirm next deploy.** Scout AI analysis was validated present on NVDA (US mega-cap, recent call). Whether it's also populated for HY9H.MU (SK Hynix, Munich cross-listing) and SSU.F (Samsung, Frankfurt) remains to be empirically confirmed in the May 18 deep-tier run. Raw paragraph extraction is the primary value here; Scout AI is an additive signal that v3.5 will surface.

---

## v3.34 — Post-v3.33 fixes (Motley Fool URL + Yahoo cross-listing tickers + Yahoo diagnostics + git push race)

**Why**: First v3.33 deep-tier run (May 16, 16:08 UTC) showed three problems. While diagnosing, Raul also surfaced a critical empirical finding about Yahoo's ticker behavior that resolves the SK Hynix transcript coverage problem cleanly.

1. **Motley Fool: 13/13 tickers returned `nothing`** because every URL the scraper tried had a stray `\` appended (e.g. `/transcript/\` → URL-encodes to `%5C` → 404). Root cause: my regex character class `[^\"\s'?#]+` excluded the closing quote but not the backslash. Motley Fool's React-rendered page embeds some URLs in JSON-stringified blobs where the closing quote is escaped — the regex greedily included the `\` before stopping at the `"`. All 13 URLs were *actually correct* once the backslash was stripped.

2. **Yahoo: SK Hynix (000660.KS) returned `nothing` because the wrong ticker was canonical.** v3.32 picked the KRX primary listing (000660.KS) as the Yahoo canonical ticker for SK Hynix. Empirically that page does NOT have transcript anchors. The European cross-listing tickers (HY9H.MU / HY9H.F / HY9H.DU / HY9H.SG) DO — same transcript content, but Yahoo's UI only renders the link from those variants. Confirmed 2026-05-16 from Raul's Yahoo Finance app: the SK Hynix Q1 FY2026 transcript is at `/quote/HY9H.MU/earnings/HY9H.MU-Q1-2026-earnings_call-547968.html` ("Powered by Quartr, Apr 23, 2026 at 2:00 AM GMT+2"). Canonical Yahoo ticker for SK Hynix updated to HY9H.MU; Samsung updated to SSU.F (Frankfurt cross-listing, same pattern assumption — v3.34 diagnostic logging will confirm).

3. **Yahoo: 14/15 US tickers also returned `nothing`** — 200 responses but bs4 found zero transcript-link anchors. Whether this is the same "wrong ticker variant" issue (e.g. Yahoo doesn't link transcripts from `/quote/AMAT/` index, only from a different variant) or a deeper JS-shell issue isn't yet known. v3.34 adds detailed logging (HTTP status, body length, anchor count, sample hrefs) so the *next* run gives us hard evidence on what Yahoo actually returns through the proxy for each ticker. AMAT specifically found 1 transcript link but it 404'd — could be a stale article ID in the static HTML.

4. **Git push at end of run rejected**: `! [rejected] main -> main (fetch first)`. The runner cloned at commit `44801cb` at 16:08:15; Raul was pushing v3.33 commits via the web UI during the 3.5-minute run, so the runner's main was stale when it tried to push digests. All 30 digest files written but lost because the workflow doesn't pull-rebase before push.

| File | Changes |
|---|---|
| `src/config.py` | `YAHOO_TRANSCRIPT_TICKERS["SK Hynix"]` changed from `000660.KS` to `HY9H.MU` (Munich cross-listing, empirically confirmed to have transcript link). `YAHOO_TRANSCRIPT_TICKERS["Samsung Electronics"]` changed from `005930.KS` to `SSU.F` (Frankfurt cross-listing, same pattern assumption pending v3.34 diagnostic logs). Comment block updated with the empirical correction and rationale for cross-listing canonical tickers |
| `src/data/transcript_sources.py` | `MOTLEY_FOOL_TRANSCRIPT_PATTERN` character class extended to `[^\"\s'?#\\]+` (exclude backslash). Belt-and-suspenders: matched URLs additionally have trailing `\` stripped and `/` re-added defensively. **Yahoo diagnostic logging**: `_http_get` logs HTTP status + body length when `use_proxy=True`; `_scrape_yahoo_ticker` logs total anchor count + transcript-link match status + first 5 anchor hrefs |
| `.github/workflows/deep_tier.yml` | Commit step now runs `git pull --rebase origin main` before `git push`; retries once after a 5-second sleep on push failure. Handles the race where main moves forward during the 3-5 minute deep-tier run |
| `CHANGES.md`, `CLAUDE.md` | v3.34 entry added |

**Honest read on the Yahoo episode**: I told Raul earlier that the AMAT curl test "proved the architecture works" based on a status 200. That was overclaiming — I only verified the status code, never inspected response body content. Production showed 200 responses without parseable transcript content. The Munich-listing correction (above) explains part of it for SK Hynix. The remaining "0 transcript anchors on US ticker pages" question remains open until v3.34 diagnostic logs come back from the next run.

**What works after v3.34**:
- Motley Fool: should return `full_transcript` for all 13 US names (URLs were always correct, just had a stray `\`).
- IR scrapers: unchanged behavior — Micron `nothing`, Samsung `materials_only` (worked in the v3.33 run too).
- Yahoo for SK Hynix: should now return `full_transcript` via HY9H.MU. If it doesn't, diagnostic logs will tell us why.
- Yahoo for Samsung: SSU.F is a guess; diagnostic logs will confirm or point us at the right variant.
- Yahoo for US names: still unreliable, but logs will now tell us *why*.
- Git push: state file commits will land even if the repo moved during the run.

**Not in scope (parked, see `ROADMAP.md`)**: Yahoo SPA workaround / per-ticker variant fallback chains → v3.35; transcript-memory + QoQ diff → v3.5; event dedup → v3.5.

---

## v3.33 — Transcript cascade + Yahoo via residential proxy

**Why**: First post-v3.32 deep-tier run (May 16, 13:10 UTC) revealed three blocking bugs.

1. **`HAIKU_MODEL` / `SONNET_MODEL` defaults were literal `"haiku"` / `"sonnet"`** — placeholder strings the Anthropic API doesn't accept. Every Layer 1 Haiku call 404'd; the deep tier ran without actually producing any digests.
2. **Yahoo Finance blocks datacenter IPs with HTTP 404** — both GitHub Actions runners and Anthropic's own fetcher get the same 404 when hitting `/quote/{TICKER}/earnings-calls/`. Yahoo's bot detection cloaks the page from non-residential ASNs.
3. **Motley Fool scraper was hitting the global recent-transcripts index** (`/earnings-call-transcripts/`) which only shows the ~20 most-recently-published transcripts site-wide. By the time the deep tier ran each morning, every name in the basket had already rolled off (replaced by hundreds of small-cap transcripts published in the intervening hours). The result: Motley Fool returned `nothing` for every ticker, every day.

The fix isn't "always proxy everything" — it's "proxy the source that needs it, fix the scraper that's pointed at the wrong URL, and order the sources so the free one runs first."

| File | Changes |
|---|---|
| `src/config.py` | `HAIKU_MODEL` default → `"claude-haiku-4-5"`; `SONNET_MODEL` default → `"claude-sonnet-4-6"` (was literal `"haiku"`/`"sonnet"` — these aren't valid Anthropic API model strings and every LLM call was 404'ing). `MOTLEY_FOOL_TICKERS` restructured from `{name: ticker}` to `{name: {"exchange": "nasdaq"\|"nyse", "ticker": ...}}` — exchange is needed by the new per-ticker URL pattern (`/quote/{exchange}/{ticker}/`) |
| `src/data/transcript_sources.py` | Substantial rewrite. **New cascade architecture** (`fetch_all_transcripts`): Motley Fool first → IR-site scrapers → Yahoo Finance ONLY for companies still missing a full transcript (or with `already_processed` cache hits — the "old cache hides new content" edge case). The only state that skips Yahoo is a fresh `full_transcript` captured this run. **Motley Fool scraper rewritten** to hit per-ticker quote page (`https://www.fool.com/quote/{exchange}/{ticker}/`) and parse for `/earnings/call-transcripts/YYYY/MM/DD/...` links — these pages persist (don't roll off in 24-48h like the global index), and contain the full transcript history for the company. **Yahoo scraper rewritten** to route through residential proxy: realistic Chrome `User-Agent` + `Accept` / `Accept-Language` / `Accept-Encoding` / `Upgrade-Insecure-Requests` headers, per-request `-session-{N}` token injection for residential-IP rotation, redirect-following (Yahoo serves 301s to canonicalize URLs), one retry-on-429 with 10s backoff and a fresh session token, and a 3-second inter-ticker delay. `RESIDENTIAL_PROXY_URL` env var enables proxy mode; absent the var, Yahoo degrades to `nothing` gracefully. **NEW URL cache** at `state/cache/transcripts_seen.json` records URLs already digested; scrapers short-circuit to `availability="already_processed"` on re-encounter (saves Haiku tokens + proxy bandwidth on transcripts captured in earlier runs). `datetime.utcnow()` → `datetime.now()` (Python 3.12+ deprecation) |
| `src/analysis/digester.py` | `digest_transcript_results()` gains an `already_processed` branch — cache hits skip both the Haiku call AND the placeholder gap record (the prior digest is already in `state/digests/`, no point recording a "gap" for a transcript we already have) |
| `src/deep_tier.py` | Docstring updated for cascade architecture. `datetime.utcnow().isoformat()` → `datetime.now(timezone.utc).isoformat()` (deprecation fix, same line that records `written_at_iso` in `transcript_status.json`). Summary log line surfaces `already_processed` count and `yahoo_attempted_for` list — makes it visible at a glance which names made it past the MF+IR gate to require proxy fallback |
| `.github/workflows/deep_tier.yml` | Adds `RESIDENTIAL_PROXY_URL: ${{ secrets.RESIDENTIAL_PROXY_URL }}` to the deep-tier step env |
| `CLAUDE.md`, `README.md`, `SETUP.md`, `DEPLOYMENT_GUIDE.md` | Architecture descriptions updated to MF-primary + Yahoo-via-proxy-fallback. `SETUP.md` secrets table gains `RESIDENTIAL_PROXY_URL`. `DEPLOYMENT_GUIDE.md` Section 2.3 (Transcript sources) rewritten |
| `memory_cycle_data_source_audit_v3_33.xlsx` | Source Registry updated: Yahoo Finance row gains "residential proxy required" note; Motley Fool row notes per-ticker URL pattern. Summary sheet gains v3.33 notes |

**Empirical validation done before the commit** (May 16, 2026):

- Yahoo at datacenter IP: `https://finance.yahoo.com/quote/AMAT/earnings-calls/` → 404 (confirmed bot block, both GitHub Actions and Anthropic fetcher).
- Yahoo via Bright Data residential, no headers: 429 (Yahoo rate-limit, NOT a Bright Data block — confirmed via Bright Data support, who clarified that under Immediate Access mode 429 = target-site response, not their throttle).
- Yahoo via Bright Data residential + realistic Chrome headers + `-session-rand` token + `-L` (redirect follow): **200, real AMAT Q2 FY26 transcript content**.
- Motley Fool global index (`/earnings-call-transcripts/`): contains only ~20 most-recent transcripts; none of the basket present after a 24-48h window.
- Motley Fool per-ticker quote page (`/quote/nasdaq/amat/`): 200 from datacenter IPs, persists for months, lists transcript history with the URL pattern `/earnings/call-transcripts/YYYY/MM/DD/{slug}`.

**Architectural decision (Raul, May 16)**: aggressive Yahoo fallback. The cascade fires Yahoo for *any* name not in fresh `full_transcript` state — including the `already_processed` cache state — out of an abundance of caution. Trades ~$3-5/year in residual proxy spend for full coverage of the "MF cached an old quarter, Yahoo has the new one" edge case. Goal is the transcript; cost is secondary.

**Cost ceiling under the new cascade**: ~3-5 MB/day proxy bandwidth worst case (~$0.10/month), well within Bright Data's $27 trial credit budget (3.3 GB residential) which alone covers about 2 years of normal operation. Trial credits expire 30 days from May 16; ongoing usage at this rate is ~$0.30-1.00/month on paid Residential at $4/GB.

**Bright Data setup constraint surfaced**: individual accounts (no business KYC) are stuck on Bright Data's Immediate Access mode — domain blocklist enforcement + robots.txt enforcement + GET/HEAD only + "high volume throttled" per their docs. For Yahoo Finance `/quote/.../earnings-calls`, this is not a block (confirmed with Bright Data support) — it's just the throttling, mitigable with rotation + headers + slow request rate + retry-on-429.

**Not in scope** (parked, see `ROADMAP.md`): transcript-memory + QoQ diff (v3.5), event dedup across rerun cycles (v3.5).

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

**Bonus finding from empirical investigation (May 15, 2026)**: Yahoo's transcript pages are tagged `Powered by Quartr` — meaning Quartr is still alive as a content distributor to Yahoo Finance; they only killed the *free public developer API*. The scrape against Yahoo is, mechanically, re-acquiring Quartr coverage through Quartr's own consumer partner.

**Schedule shift (deep tier cron 03:00 → 08:00 UTC)**: empirical timestamps from 5 recent US after-hours calls (NVDA Q4 FY26, AVGO Q1 FY26, MSFT Q3 FY26, AMAT Q2 FY26, NU Q1 2026) put the Yahoo/Quartr transcript availability distribution at mean ~6.5h post-call-end, σ ~2.5h. The original 03:00 UTC deep tier caught only ~34% of US after-hours transcripts same-day; 08:00 UTC catches ~94% and still leaves ~3h buffer before the first fast-tier run at 11:00 UTC. Also helps Asian calls (SK Hynix / Samsung / TSMC) whose transcripts land ~05:00-09:00 UTC. Changed in `.github/workflows/deep_tier.yml`, and synced across `src/deep_tier.py`, `src/analysis/digester.py`, `README.md`, `CLAUDE.md`, `SETUP.md`, `DEPLOYMENT_GUIDE.md`. Workflow file also cleaned of leftover `QUARTR_API_KEY` env and "Quartr" step label (v3.31 misses).

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
