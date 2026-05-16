# Memory Cycle Brief — Deployment Guide (v3.32)

<!--
CHANGELOG (this file):
  v3.32  (2026-05-14) — Section 2.3: Yahoo Finance added as primary transcript source; troubleshooting + tuning notes updated
  v3.31  (2026-05-14) — Section 2.3 rewritten — no Quartr signup/key; transcript troubleshooting rows; transcript-sources tuning notes
  v3.3   (2026-05-12) — extraction_log review steps; GitHub-connect + @claude workflow section
  v3.1   (2026-05-12) — News relevance filter calibration + dropped-items log
-->

Step-by-step setup from zip file → first brief in your inbox. Takes ~90 minutes if everything goes smoothly. You don't need to write any code or install anything on your computer — everything runs on GitHub's free tier.

**What you need:**
- A GitHub account (free)
- A Gmail account (free)
- An Anthropic API account (will require a small payment method)
- About 90 minutes split across signup + verification + first run

---

## Part 1 — Set up the GitHub repo (15 min)

### 1.1 Create a GitHub account (skip if you have one)

Go to <https://github.com/signup>. Free tier is fine — the pipeline uses ~270 of the 2,000 free GitHub Actions minutes per month.

### 1.2 Create a new repository

1. Click `+` top-right → **New repository**
2. Repository name: `memory-cycle-brief` (or whatever you want)
3. **Private** — important, contains your strategy logic
4. Don't initialize with README/license/.gitignore — we'll upload existing files
5. Click **Create repository**

### 1.3 Upload the code

Easiest path (no command line):

1. On the empty repo page, click **uploading an existing file** (link in the middle)
2. Unzip `memory_cycle_brief_v3.zip` on your computer. Open the resulting folder so you can see `src`, `.github`, `README.md`, `SETUP.md`, etc. inside it.
3. **Select all files and folders inside that folder** (Ctrl+A on Windows / Cmd+A on Mac) and drag them onto the GitHub upload area.
4. Scroll down. Commit message: `Initial commit — v3`.
5. Click **Commit changes**.

After upload, your repo file list should show: `.github/`, `src/`, `docs/`, `README.md`, `SETUP.md`, `DEPLOYMENT_GUIDE.md`, `requirements.txt`, `.env.example` — about 30 files total.

**Common mistake**: uploading the outer folder instead of its contents. If you see a single nested folder in your repo, that's wrong — re-upload from inside that folder.

---

## Part 2 — Sign up for the APIs (30 min)

### 2.1 Anthropic (required)

1. Go to <https://console.anthropic.com/> → sign up
2. Add a payment method (Visa/Mastercard, no PayPal)
3. Add $20 prepayment to start — monthly burn will be €5-12 so this lasts months
4. **Settings → API keys → Create Key** — copy the key (looks like `sk-ant-api03-...`)

### 2.2 Finnhub (US analyst ratings — recommended)

1. <https://finnhub.io/register> — sign up, no credit card
2. Confirm email, log in
3. API key is on dashboard, top of page — looks like `c8a1k2hr01qtn8c5j7v0c8a1k2hr01qtn8c5j7vg`

### 2.3 Earnings transcripts — no signup needed (v3.32)

**There is nothing to set up here.** Earlier versions used the Quartr API for
earnings-call transcripts, but Quartr discontinued its free developer tier
(it's now enterprise-only, "contact us" pricing). Transcripts now come from
free sources that need no API key, used in priority order:

- **Yahoo Finance** *(primary)* — full earnings-call transcripts for every
  name in the basket, including the KRX-listed memory makers SK Hynix and
  Samsung (with the Korean+English Q&A). Latest call is free.
- **Motley Fool transcripts** — US-listed names with English earnings calls.
- **Direct IR-site scrapers** — Micron posts the full call; SK Hynix and
  Samsung IR sites publish slides + webcast only (no text transcript), so
  they're treated as **materials-only** by config and kept just for the
  earnings release / slide deck.
- **SEC EDGAR 8-K earnings exhibits** — already covered by your EDGAR feeds.

Skip straight to 2.4. There is no `QUARTR_API_KEY` anymore — if you have one
in your environment from an earlier setup, you can delete it; it's unused.

One thing to know about how this behaves: every transcript source reports a
three-state availability flag — `full_transcript`, `materials_only`, or
`nothing` — and the aggregator keeps the best state across sources. For SK
Hynix and Samsung, Yahoo Finance is normally the source that delivers
`full_transcript`; the IR scraper for those two is capped at `materials_only`
on purpose. If Yahoo *also* comes back short for a Korean name, the brief's
data-source validation section shows that company as a WARN with "COMMENTARY
NOT AVAILABLE — materials only". That's a tracked fact surfaced explicitly,
not a silent gap — and the Korean broker PDF echo (Mirae / NH / Samsung
Securities) usually still carries the commentary.

### 2.4 Alpha Vantage (EPS cross-check — optional)

1. <https://www.alphavantage.co/support/#api-key>
2. Fill form — they email key immediately, no account needed

### 2.5 DART (Korean disclosures — recommended)

This site is Korean — there's an EN toggle but it's spotty.

1. <https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do>
2. Toggle to EN (top right)
3. Fill registration form. Affiliation: Personal. Purpose: Personal investment research.
4. Confirm email, log in
5. Top-right menu → **Open API → API Key** → request key (granted immediately)
6. Copy the 40-character key

---

## Part 3 — Generate Gmail credentials (15 min)

### 3.1 Sending Gmail (required)

You can use your existing Gmail or create a new one.

1. Enable 2-factor authentication if not already: <https://myaccount.google.com/security> → **2-Step Verification**
2. Generate an app password:
   - <https://myaccount.google.com/apppasswords>
   - App name: `Memory Cycle Brief`
   - Click **Create**
   - Copy the 16-character password (no spaces). **You can only see it once** — save it.

The app password is what the pipeline uses to send. Your regular Gmail password is never touched.

### 3.2 Samsung Securities Gmail (optional but recommended)

Samsung Securities distributes research PDFs by email. To capture them, the pipeline polls a dedicated Gmail inbox.

1. Create a new Gmail address — e.g. `your.name.semis@gmail.com`. Use a **fresh inbox** so the pipeline doesn't read your personal email.
2. <https://www.samsungpop.com/global> → register with that new Gmail
3. Tick boxes for Equity Research / Memory / Semiconductors in distribution preferences
4. Wait 1-2 business days for first emails to arrive (proves it's working)
5. Then enable 2FA + generate app password (same process as 3.1)

If you skip: Samsung Securities marked SKIPPED in assessment table. Mirae Asset + NH Investment still cover Korean Tier 1 broker depth.

---

## Part 4 — Add secrets to GitHub (15 min)

GitHub Secrets are encrypted; never appear in logs or repo.

1. In your repo: **Settings** (top menu) → **Secrets and variables** → **Actions**
2. Click **New repository secret** for each below. **Name** must match exactly (case-sensitive).

| Name | Value | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key from Part 2.1 | **Required** |
| `GMAIL_ADDRESS` | Gmail sending the brief | **Required** |
| `GMAIL_APP_PASSWORD` | 16-char app password from Part 3.1 | **Required** |
| `EMAIL_RECIPIENT` | Gmail receiving the brief (usually same as above) | **Required** |
| `FINNHUB_API_KEY` | Key from Part 2.2 | Recommended |
| `ALPHA_VANTAGE_KEY` | Key from Part 2.4 | Optional |
| `DART_API_KEY` | Key from Part 2.5 | Recommended |
| `SAMSUNG_GMAIL_USER` | Dedicated Gmail from Part 3.2 | Optional |
| `SAMSUNG_GMAIL_APP_PW` | App password from Part 3.2 | Optional |
| `NTFY_TOPIC_URL` | See Part 4.5 below | Recommended for real-time alerts |
| `FMP_API_KEY` | Leave unset — not used in v3 | Skip |

After adding, the Secrets page should show 7-10 entries.

---

## Part 4.5 — Set up real-time mobile push alerts (15 min, free)

The priority alerts workflow pushes mobile notifications when Tier A brokers (Mirae Asset, Goldman, Morgan Stanley, JPMorgan, Mizuho) take material actions on SK Hynix, Micron, or Samsung. Runs every 30 minutes during Korean + US market hours, much faster than waiting for the next daily brief.

### 4.5.1 Install the ntfy app on your phone

1. iPhone: App Store → search "ntfy" → install **ntfy** (the one by Philipp Heckel, free)
2. Android: Play Store → same

ntfy is a free open-source push notification service. No account needed.

### 4.5.2 Choose your topic name

Pick a unique, opaque topic name. **Anyone who knows the topic name can read your notifications** — so don't use anything guessable. Example: `memory-cycle-raul-7f3a9b2c1d` (your name + random string).

You don't register this anywhere. The first time you POST to the URL, the topic is created automatically.

### 4.5.3 Subscribe in the app

1. Open the ntfy app on your phone
2. Tap **+** (top right)
3. Enter your topic name (just the name, no URL prefix — e.g. `memory-cycle-raul-7f3a9b2c1d`)
4. Keep server as default (`ntfy.sh`)
5. Save

The app now listens for any message sent to that topic.

### 4.5.4 Test the topic

Open Terminal (Mac) or Command Prompt (Windows), and run:

```
curl -d "Test notification" ntfy.sh/memory-cycle-raul-7f3a9b2c1d
```

(replace the topic name with yours). Within ~10 seconds your phone should buzz with "Test notification". If yes, ntfy is working.

If you don't have a terminal, you can also test via any browser — just visit `https://ntfy.sh/memory-cycle-raul-7f3a9b2c1d` and use the "Publish a message" form on the page.

### 4.5.5 Add the topic URL as a GitHub secret

1. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**
2. **Name**: `NTFY_TOPIC_URL`
3. **Secret**: `https://ntfy.sh/memory-cycle-raul-7f3a9b2c1d` (full URL, your topic name)

### 4.5.6 Test the priority alerter end-to-end

1. **Actions** tab → **Memory Cycle Brief — Priority Alerts (real-time mobile push)**
2. **Run workflow** → green button
3. The workflow takes ~1-2 minutes. If there are no qualifying actions today, no push will fire (working as intended — we don't want false alerts).
4. To verify the workflow CAN reach your phone: temporarily set the bearish threshold to `-1.0` in `src/priority_alerter.py` line `ALERT_THRESHOLD_BEARISH_SCORE = -6.0`, push, and run manually. You should get pushes for nearly every Tier A action. Then revert to `-6.0` for normal operation.

### 4.5.7 What you'll receive

When Mizuho cuts Micron PT 20% or Morgan Stanley double-downgrades SK Hynix, your phone will receive a notification like:

```
🚨 Morgan Stanley — SK Hynix (Score -18.0)

Action: PT ₩260,000 → ₩120,000 (-54.0%) + double_downgrade
PT change: -54.0%
Source: Bloomberg
Tier: A · 2024-09-15

Why alerting:
  • Score -18.0 ≤ -6.0 (significant bearish)
  • Two-notch rating change (double_downgrade)
  • PT change -54.0% ≥ ±15.0%
```

Tapping the notification opens the Naver Finance page for that ticker.

### 4.5.8 Privacy / advanced options

ntfy.sh is free and anonymous, but anyone who knows your topic name can read your notifications. Trade-offs:

- **Acceptable**: notifications contain only public broker actions (no portfolio data, no PII). Worst case: someone reads "Morgan Stanley downgraded SK Hynix" — same info as Bloomberg's homepage.
- **Higher privacy**: self-host ntfy on a VPS (~$5/mo), or use Pushover ($5 one-time per platform) — code change is one URL swap.
- **Telegram alternative**: create a Telegram bot, set `NTFY_TOPIC_URL` to its webhook URL. Same architecture, fully private.

I recommend default ntfy.sh — privacy concerns are minimal given the content.

---

## Part 5 — First manual run (15 min)

### 5.1 Run the deep tier first

Deep tier seeds the digests that fast tier reads.

1. Your repo → **Actions** (top menu)
2. Two workflows in left sidebar: **Memory Cycle Brief — Deep Tier (overnight)** and **Memory Cycle Brief — Fast Tier (daily delivery)**

   If you don't see them, workflow files weren't uploaded correctly — go back to Part 1.3.

3. Click **Memory Cycle Brief — Deep Tier (overnight)**
4. Right side: **Run workflow** dropdown → keep `Branch: main` → green **Run workflow**
5. Wait ~20 seconds, refresh. Yellow circle = running. Click the row to watch logs.
6. After 5-15 min: green checkmark (success) or red X (failure).
7. If succeeded: click latest commit in repo → new files under `state/digests/{today's date}/`
8. If failed: click into the failed step's logs. Common failure is wrong API key — error message says which one. Fix the secret and re-run.

### 5.2 Run the fast tier

1. Actions tab → **Memory Cycle Brief — Fast Tier (daily delivery)**
2. **Run workflow** → green button
3. Watch logs. Takes 4-10 min.
4. On success, brief should arrive in your inbox within 1 minute.

### 5.3 What to check in the first brief

Open and verify all sections are present:

1. Executive summary — should show tier statuses (e.g. "T1: 5/6 | T2: 6/7...")
2. Performance overview with chart
3. Tier indicators
4. Top 5 deep dives
5. Latest news
6. **🆕 Calendar of events (next 90 days)** — should show ~21 upcoming events
7. **🆕 Data source assessment** — every row should be OK or SKIPPED

If you see FAIL rows in section 6, cause is usually:
- Wrong API key → Settings → Secrets → update value
- Source returned empty → legitimate (no news that source today)
- Source unreachable → rare; retry tomorrow

If 🚨 banners fire at the top, that's by design — they indicate active EPS revisions or relative-strength alerts.

---

## Part 6 — Confirm schedule (next morning)

Schedule activates automatically after first manual run.

- **Deep tier**: 08:00 UTC = 10:00 Munich (summer) / 09:00 (winter)
- **Fast tier**: weekdays 11:00 UTC + 11:30 fallback + 12:05 safety = 13:00 / 13:30 / 14:05 Munich (summer)

Tomorrow 13:00-14:05 Munich → brief should arrive. Check **Actions** tab to confirm both workflows ran.

GitHub disables scheduled workflows in inactive repos after 60 days. The pipeline auto-commits state daily, which keeps it active. If you see 60+ days of silence, trigger a manual run to re-arm.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflows don't appear in Actions tab | `.github/workflows/` folder not uploaded | Re-upload preserving folder structure |
| "Bad credentials" in Anthropic step | ANTHROPIC_API_KEY wrong/missing | Settings → Secrets → update |
| Brief missing 🚨 banners | Signal not actually firing (correct behavior) | Check the EPS section if visible |
| Brief sent but no Korean broker data | Finnhub / Naver scraper / Korean broker PDF fetch fail | Check assessment table for which |
| Transcript shows "COMMENTARY NOT AVAILABLE — materials only" | Yahoo Finance came back short for that name (and IR site is materials-only) | For SK Hynix / Samsung, Yahoo Finance is the transcript source — if it's short, the latest call may not be posted yet, or Yahoo changed its page structure (check `state/transcript_status.json` notes). The Korean broker PDF echo usually still carries the commentary. If it persists for **Micron** or a US name, the Yahoo or IR scraper selectors need review — see "Where to make adjustments" |
| All transcript rows show "nothing" for weeks | Yahoo Finance / Motley Fool / IR site structure change | Check `state/transcript_status.json` for the per-source notes. Each scraper degrades gracefully, so this won't break the brief — but selectors in `src/data/transcript_sources.py` need updating (same maintenance pattern as the Naver scraper) |
| "Naver Finance: FAIL" | Korea blocking GitHub IPs (rare) | Self-resolves in 24hr; cached payload used |
| No email arriving | GMAIL_APP_PASSWORD wrong (most common) | Must be 16 chars from apppasswords page, NOT regular Gmail password |
| Email missing a section | Old workflow file cached | Push any commit to force refresh |
| Got GitHub issue notification "pipeline failed" | Email send failed but brief was generated | Look in repo for `state/failed_send_*.html` |
| Brief looks light on news / missing a story you expected | News relevance filter may be too aggressive | Check `state/news_dropped_YYYY-MM-DD.json` — if real signal was dropped, add the missing keyword to `TRACKED_COMPANIES` / `TRACKED_BROKERS` in `src/config.py`, or lower `min_score` from 3 to 2 in `src/fast_tier.py` |
| News section shows obvious noise | Filter too permissive | Raise `min_score` from 3 to 4 in `src/fast_tier.py` |

---

## What's now self-sustaining

After Part 6 verification:

**Daily**: brief arrives 13:00 Munich. No action needed.

**Weekly**: glance at the assessment table — if any source shows STALE for more than 7 days, that scraper may need a tweak (Naver / Mirae redesigns happen 1-2x/year).

**First 2 weeks after deployment** (calibration window): open `state/news_dropped_YYYY-MM-DD.json` and spot-check 20-30 dropped headlines. Any that should have been kept → add the missing analyst/broker/company name to the `TRACKED_*` lists in `src/config.py`. After 2 weeks, the filter is calibrated and you can ignore the dropped log unless something feels off.

**First 3-4 weeks after deployment** (v3.3 calibration): open `state/extraction_log_YYYY-MM-DD.jsonl` periodically. These are the captive-ratio architecture signals being extracted from earnings calls and broker notes. They feed the v3.4 indicators (deferred — see `ROADMAP.md`). At ~4 weeks, you'll have enough data to set v3.4 thresholds intelligently. Until then, the extraction runs silently — no email impact, just accumulating data.

**Monthly**: check Anthropic spend at <https://console.anthropic.com/settings/billing>. Should be €5-12. If above €20, something's looping — pause workflows from Actions tab and investigate.

**Quarterly** (after each major earnings prints): the `state/inventory_weeks_running.json` and `state/hyperscaler_capex_baselines.json` auto-update from earnings call digests. No action needed.

**After ADR launches (June/July 2026)**: add the SK Hynix ADR ticker symbol to `CORE_TICKERS` in `config.py` once it's known. This enables direct Finnhub coverage of US-broker actions on the ADR.

---

## Where to make adjustments

- **Trigger phrases too sensitive / not sensitive enough** → edit `INDICATORS = [...]` in `src/config.py`, adjust `trigger_patterns` list
- **Broker tier assignment** → edit `BROKER_AUTHORITY_TIERS` in `src/config.py` (start opinionated, refine after 4-6 weeks of observation)
- **EPS scoring thresholds** → edit `EPS_SCORING_THRESHOLDS` in `src/config.py`. Current thresholds (bearish 7d < -10 = EARLY WARNING, bearish 30d < -15 = FIRED) were validated against the 2021 and 2024 Morgan Stanley "Winter is Coming" / "Winter Looms" events — see `docs/INDICATOR_3_EXAMPLES.md` for the math
- **News relevance filter tuning** → edit `TRACKED_ANALYSTS` / `TRACKED_BROKERS` / `TRACKED_COMPANIES` / `TRACKED_CONCEPTS` in `src/config.py` to expand or narrow the keyword pass. The Haiku scoring threshold is the `min_score=3` argument in `src/fast_tier.py` (Step 1b). Lower it to catch more (and accept more noise); raise it to be stricter. Dropped items are logged to `state/news_dropped_*.json` for spot-checking
- **Architecture extraction prompt (v3.3)** → the captive-ratio extraction rules live in `DIGEST_SYSTEM_PROMPT` in `src/analysis/digester.py`. If you see extraction missing a signal type you care about, add the field to the `architecture_signals` schema in the prompt. Output flows to `state/extraction_log_*.jsonl`. Test changes by running `python -m tests.test_architecture_extraction` against your local copy before deploying — this validates the prompt still extracts the three known fixture types correctly
- **Transcript sources (v3.32)** → all three scrapers — Yahoo Finance, Motley Fool, and the IR sites — live in `src/data/transcript_sources.py`. Yahoo Finance is the primary source (full transcripts incl. the Korean names); the SK Hynix / Samsung IR scrapers are capped at `materials_only` via `transcript_capable: False` in `IR_SOURCES`. When a site is redesigned, the link-matching keywords or content selectors need updating — same maintenance pattern as the Naver scraper, expect 1-2 fixes/year. Each scraper is defensive: a broken selector degrades to `materials_only` or `nothing` and logs a warning, it never crashes the deep tier. Diagnose from `state/transcript_status.json`. To add a new company: add it to `YAHOO_TRANSCRIPT_TICKERS` (use ONE canonical Yahoo ticker — Yahoo syndicates the same transcript across many listing variants), and optionally to `MOTLEY_FOOL_TICKERS` or `IR_SOURCES` in `src/config.py`. When OpenAI or Anthropic goes public, fill in the `AI_LAB_IR_WATCH` entry and add the ticker to `YAHOO_TRANSCRIPT_TICKERS` — the news pipeline already watches for the listing event
- **Add new ticker** → edit `WIDER_BASKET` in `src/config.py`
- **Email format** → edit `src/email_module/template.py` (HTML/CSS)

After any edit, push to GitHub. The next scheduled run picks up the change automatically — no rebuild needed.

---

## Working on this repo with Claude

Two complementary ways to involve Claude in maintaining the pipeline. They split
cleanly: **think in chat, execute in GitHub.**

### A. Connect the repo to a chat session (read access — for design discussion)

This lets a Claude chat session *see* the live repo, so you no longer upload zips
for Claude to inspect the current code. The design back-and-forth stays exactly
as it is — same chat interface — Claude just always has the real, current state.

To connect:
1. In the Claude chat interface, click the **"+" button** (lower-left of the chat box).
2. Select **"Add from GitHub"** from the drop-down.
3. If not already authenticated, you'll be redirected to GitHub to authorise.
4. Search for this repository or paste its URL.
5. Use the file browser to select the folders that matter — `src/`, `docs/`,
   `tests/`, and the root `.md` files. You don't need to select `state/` (runtime
   output) or anything large.
6. Send your message — Claude reads the selected content as context.

Notes:
- Use the **"Sync"** icon to refresh Claude's view before starting a new analysis
  or after you've pushed changes — otherwise it may be looking at an older copy.
- If the repo is **private**, you'll be prompted to grant access via the Claude
  GitHub App. If you're the repo admin you can grant it yourself; otherwise the
  request goes to whoever administers the GitHub account.
- This is **read-only**. Claude sees the code to discuss and design changes; it
  does not push anything back. Delivery still happens via option B or by hand.

### B. `@claude` in GitHub (write access — for executing settled specs)

Once a change has been designed and agreed in a chat session, you don't need to
hand-place files. You open a GitHub Issue, describe the change (paste the spec
that came out of the chat session), and tag **`@claude`** in a comment. It produces
a Pull Request — a proposed change shown as a reviewable diff. You review it,
request tweaks in PR comments if needed, and merge when satisfied. Nothing goes
live until you merge.

One-time setup (requires repo admin rights):
1. Install Claude Code locally and run `/install-github-app`, OR install the
   Claude GitHub App manually from `https://github.com/apps/claude`.
2. Add your `ANTHROPIC_API_KEY` to the repository secrets (same place you added
   the other secrets in Part 4).
3. Add a workflow file at `.github/workflows/claude.yml` (the install command
   does this for you; manual setup means copying the example workflow).

After that, you never touch a terminal again — it's all GitHub web UI: open
issue, tag `@claude`, review PR, merge.

**Important — `@claude` does not share chat memory.** A GitHub `@claude` instance
has no record of prior chat sessions. It relies entirely on:
- `CLAUDE.md` in the repo root — standing context: who you are, locked decisions,
  architecture, communication preferences, what's deferred. Read first by every
  `@claude` invocation.
- The Issue text you write — this must be self-contained. The good news: a spec
  that came out of a proper design discussion already is self-contained.

So the durable workflow is: **design in chat (with the repo connected via A),
then paste the resulting spec into a GitHub Issue and let `@claude` execute it
(via B).** If a PR surfaces a real problem, bring it back to a chat session to
re-think — don't try to redesign in PR comments.

### Keep CLAUDE.md current

`CLAUDE.md` is the permanent, in-repo version of the session handover doc. Any
time a locked decision changes, a new version ships, or a deferred item gets
built, update `CLAUDE.md` in the same change. It's what keeps every future Claude
— chat or `@claude` — from re-litigating settled decisions.

## What to do when an alert fires

The brief is deliberately structured to make the most important moves unmissable:

1. **🚨 banners at the top** appear ONLY when something has actually triggered (relative strength sub-trigger breach, or EPS revisions canonical signal fired/early-warning). Most days you won't see a banner.
2. **Section 4b "EPS Revisions Watch"** appears only when the canonical Tier 2 signal is approaching/firing — gives you the full broker-by-broker scoring breakdown so you can verify the verdict.
3. For Tier A US broker actions where FY27 EPS wasn't extracted (US broker, no Korean PDF echo yet), each row carries an explicit "⚠️ FY27 EPS not extracted — consider manual Bloomberg/Mirae check" hint. This surfaces the gap; you decide per-event whether to dig deeper.

Real-time mobile alerts (separate from this daily brief) — handled via a subscription to The Fly on the Wall (~$30/mo), which provides push notifications on analyst actions for SK Hynix / Micron / Samsung with the rationale paragraph included. Once the SK Hynix US ADR launches (June/July 2026), the standard Yahoo Finance / Webull free apps will cover the ADR ticker natively and The Fly may become redundant — reassess then.
