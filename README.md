# Memory Cycle Brief — v3

Daily email brief on the memory semiconductor cycle, focused on SK Hynix as the primary position. Tracks 22 cycle indicators across 4 tiers + competitor dynamics (Indicator 6) + SK Hynix US ADR launch (Indicator 7) using a hierarchical LLM synthesis pipeline that runs on free-tier GitHub Actions.

The brief is delivered ~13:00 CEST weekdays — at least 1 hour before US market open even in summer DST. Run cost is ~€5-12/month for Anthropic API; everything else (data sources, hosting, scheduling) is free.

## What's new in v3

- **Crisp trigger framework** — every indicator has explicit `trigger_patterns` (specific phrases to detect), `trigger_numeric` (numeric thresholds), `cadence` (how many occurrences over what window), and `stitching` logic (when one source isn't sufficient). 19/22 indicators have phrase patterns, 18/22 have numeric thresholds, 22/22 have stitching logic.
- **Weighted broker scoring for the canonical Tier 2 signal** (`t2_eps_revisions`). Each broker action scored by direction × magnitude × authority tier (A=3×, B=2×, C=1×) × combo bonus (rating+PT+EPS aligned). Rolling 7d and 30d sums drive trigger logic — see `src/analysis/signal_scoring.py`.
- **Two-tier execution architecture** — deep tier 03:00 UTC daily pulls slow sources (earnings transcripts via IR-site scrapers + Motley Fool, Korean broker English PDFs) and produces structured digests; fast tier 11:00 UTC weekdays consumes those digests + fresh fast sources and delivers the brief. Idempotency via `state/brief_sent_dates.json`.
- **Hierarchical LLM synthesis** — Layer 1 (Haiku, deep tier) digests each large document; Layer 2 (Haiku, fast tier) reasons per-signal across all evidence with explicit convergence assessment; Layer 3 (Sonnet, fast tier) writes the exec summary.
- **9 new data sources** — Naver Finance, Finnhub, Mirae Asset, NH Investment, Samsung Securities (Gmail IMAP), DART, Alpha Vantage, KED Global / Korea Herald RSS, 4 AI lab blogs, SEMI press releases, per-ticker Investing.com, IR-site transcript scrapers (SK Hynix / Samsung / Micron), Motley Fool transcripts.
- **Email enhancements** — 🚨 banner for relative strength alerts (3 sub-triggers), 🚨 banner + dedicated section for EPS revisions when canonical Tier 2 fires, **Calendar of events** showing the next 90 days of catalysts, **Data source assessment** confirming each source was read successfully.

## Architecture

```
                          ┌─────────────────────────────┐
                          │  GitHub Actions schedulers  │
                          └──────────────┬──────────────┘
                                         │
        ┌────────────────────────────────┼─────────────────────────────────┐
        │                                │                                 │
┌───────▼─────────────┐  ┌──────────────▼──────────────┐  ┌───────────────▼──────────────┐
│ PRIORITY ALERTS     │  │  DEEP TIER (03:00 UTC)      │  │  FAST TIER (11:00/11:30/12:05) │
│ Every 30 min        │  │  10-25 min runtime          │  │  4-10 min runtime              │
│ during mkt hrs      │  │                             │  │                                │
│ ~1 min/run          │  │ • Transcripts: IR+MotleyFool│  │ • RSS (32 feeds)               │
│                     │  │ • Mirae/NH PDFs             │  │ • EDGAR atom (6 feeds)         │
│ • Naver + Finnhub   │  │ • Samsung Sec (Gmail)       │  │ • Google News (17 queries)     │
│ • Score → filter:   │  │                             │  │ • Naver Finance                │
│   Tier A only +     │  │ Layer 1 (Haiku) →           │  │ • Finnhub + DART API           │
│   SKHX/MU/SAMS +    │  │ Per-doc digests             │  │ • yfinance + capex/rev ratios  │
│   score <=-6 OR     │  │                             │  │                                │
│   2-notch OR PT>15% │  │ state/digests/ ─────────────┼─►│ Reads digests from disk        │
│                     │  └─────────────────────────────┘  │                                │
│ ↓ ntfy.sh PUSH      │                                   │ Signal scoring (Indicator 3) → │
│   to mobile         │                                   │  Bearish/bullish/net split     │
│   ~60min latency    │                                   │                                │
│   from broker pub   │                                   │ Layer 2 (Haiku, 22 calls) →    │
└─────────────────────┘                                   │  Per-signal reasoning          │
                                                          │                                │
                                                          │ Layer 3 (Sonnet, 1 call) →     │
                                                          │  Exec summary + composition    │
                                                          │                                │
                                                          │ Email render + send 13:00 CEST │
                                                          └────────────────────────────────┘
```

## Three delivery channels — choose your latency

| Channel | Latency | Coverage | Best for |
|---|---|---|---|
| **Priority alerts** (ntfy.sh push) | 30-90 min from broker publish | Tier A brokers only on SKHX/MU/Samsung, score ≤ -6 OR 2-notch OR PT >15% | Material events that need same-day awareness |
| **Daily brief** (email 13:00 CEST) | ~next morning | All 22 indicators × all tiers × all 13 tickers | Full picture, position management |
| **Deep digests** (state/digests/) | Daily 03:00 UTC | Earnings transcripts (IR + Motley Fool) + Korean broker PDFs digested | Quarterly catalysts, structural changes |

## Indicator coverage scorecard

| Tier | Total | Automated | Partial | Manual |
|---|---|---|---|---|
| Tier 1 — Early warning | 6 | 4 | 2 | 0 |
| Tier 2 — Mid-cycle | 7 | 7 | 0 | 0 |
| Tier 3 — Concurrent confirms | 5 | 5 | 0 | 0 |
| Tier 4 — Lagging confirms | 4 | 4 | 0 | 0 |
| Indicator 6 — Competitor dynamics | 3 | 2 | 1 | 0 |
| Indicator 7 — US ADR launch | 6 | 6 | 0 | 0 |
| General news & analyst-by-name | 4 | 3 | 0 | 1 |
| **Total** | **35** | **31 (89%)** | **3 (8%)** | **1 (3%)** |

The 3 remaining partials are architectural — TrendForce DRAMeXchange price tables are JS-rendered (not scrapable), so `t1_spot_decline` and `t2_spot_contract_spread` rely on three corroborating channels (Tom's Hardware daily proxy + TrendForce news commentary + Korean broker chart citations). Indicator 6A (CXMT commodity threat) is partial because the deepest CXMT analysis comes from Mirae PDFs (covered) but trader-level intel from @KevinXuChina is manual.

## Trigger framework — three trigger types

Each indicator falls into one or more of:

1. **NUMERIC** — read the number directly (yfinance, Finnhub, computed). Pure arithmetic. Example: `t3_relative_strength` → `(SKHX_5d − SOX_5d) < -10pp`.

2. **PATTERN + CADENCE** — paywalled or JS-rendered number, language describing it is free. Trigger = historically-loaded phrase patterns within cadence window. Example: `t1_price_cuts_no_demand` fires on 2+ articles within 14 days containing TrendForce's verbatim phrasings like "price cuts failed to revive" / "buyers cautious" / "even at lower prices" — applied to DDR5 specifically.

3. **STITCHED** — requires BOTH SIDES from independent channels. Example: `t1_oem_inventory` fires only when supplier-side AND buyer-side both confirm. Single side alone = noise.

## Indicator 3 weighted scoring — the canonical Tier 2 signal

Operationalized in `src/analysis/signal_scoring.py`. Each broker action gets scored with:

**Broker authority tiers**:
- Tier A (×3): Mirae Asset, Goldman Sachs, Morgan Stanley, JPMorgan, Mizuho
- Tier B (×2): Samsung Securities, NH Investment, Korea Investment & Securities, Bernstein, Susquehanna, BofA, Citi, UBS, Macquarie, HSBC
- Tier C (×1): All other Korean Tier 2 brokers (~15+ via Naver) + smaller US shops

**Per-note formula**: `final_score = direction × (rating + PT + EPS points) × tier × (1 + combo)`

**Magnitude brackets** (PT % or FY27 EPS revision %): <5%→0.5, 5-10%→1.0, 10-20%→1.5, ≥20%→2.0
**Rating changes**: initiate ±1.5, one-notch upgrade/downgrade ±1.0, **two-notch upgrade/downgrade ±2.0** (e.g. Overweight → Underweight, skipping Equal-weight), maintained 0
**Combo bonus**: +50% per additional aligned component (rating + PT + EPS all moving same direction → ×2.0)

**Trigger thresholds (bearish-only firing)** — we track bearish and bullish sums SEPARATELY per window, so a fresh bearish cluster doesn't get masked by older bullish actions:

- Single note < -5 → flagged
- Single Tier A note < -3 → flagged
- **Bearish 7d sum < -10** → EARLY WARNING
- **Bearish 30d sum < -15** → **FIRED** (canonical Tier 2)
- **Bullish 30d sum > +15** → BULLISH CONFIRM

Validated against historic 2021 + 2024 Morgan Stanley downgrades — see `docs/INDICATOR_3_EXAMPLES.md` for worked examples and acid tests.

## Setup

See `SETUP.md` for technical setup details, or `DEPLOYMENT_GUIDE.md` for the non-developer walkthrough (~90 min).

## State management

All persisted state in `state/`:
- `brief_sent_dates.json` — idempotency for fast-tier triggers
- `prior_state.json` — yesterday's analysis for diff/change-tracking
- `eps_revisions_running.json` — rolling 60-day broker actions for Indicator 3
- `hyperscaler_capex_baselines.json` — auto-updating capex/revenue for Indicator 2
- `inventory_weeks_running.json` — quarterly trajectory for Indicator 4
- `cache/<source_id>.json` — per-source cached payloads for stale-fallback
- `digests/YYYY-MM-DD/*.json` — Layer 1 outputs from deep tier

State directory commits back to repo automatically so each run diffs against previous.

## Cost

- **Anthropic API**: €5-12/month
- **Everything else**: free tier (GitHub Actions ~270 of 2000 min/mo, Finnhub free 60/min, transcript scrapers free, DART free 10k/day, Alpha Vantage free 25/day, SEC EDGAR free, yfinance free, Naver free with 7-sec politeness delay)
- **Koyfin**: not recommended — we're already faster than Koyfin's Capital IQ feed (Naver 30-60min vs Koyfin 24-72hr Korean, Finnhub 5-30min vs Koyfin hours-to-days US)
