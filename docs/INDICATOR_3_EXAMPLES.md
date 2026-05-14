# Indicator 3 — Worked examples & historical validation

The canonical Tier 2 EPS revisions signal (`t2_eps_revisions`) uses weighted broker scoring implemented in `src/analysis/signal_scoring.py`. This doc covers:

1. The scoring formula
2. Six worked examples — four synthetic + two real historical cycle-turn events
3. Why the scoring fires on bearish-only rather than net
4. Honest limits

## The scoring formula

```
final_score = direction × (rating_points + pt_points + eps_points)
              × authority_tier_multiplier × (1 + combo_bonus)
```

### Components

**Tier multipliers**: A=3×, B=2×, C=1×.

| Tier A (×3) | Tier B (×2) | Tier C (×1) |
|---|---|---|
| Mirae Asset | Samsung Securities | All other Korean brokers via Naver |
| Goldman Sachs | NH Investment | Smaller US shops |
| Morgan Stanley | Korea Investment & Securities | |
| JPMorgan | Bernstein, Susquehanna | |
| Mizuho | BofA, Citi, UBS, HSBC, Macquarie | |

**Magnitude brackets** (PT % or FY27 EPS revision %):
- <5% → ±0.5
- 5-10% → ±1.0
- 10-20% → ±1.5
- ≥20% → ±2.0

**Rating change points**:
- Initiate Buy/Sell: ±1.5
- One-notch upgrade/downgrade: ±1.0
- **Two-notch upgrade/downgrade**: ±2.0 (e.g. Overweight → Underweight, skipping Equal-weight)
- Maintained: 0

**Combo bonus**: +50% per additional aligned component (rating + PT + EPS all moving same direction → ×2.0 total).

### Trigger thresholds (bearish-only firing)

Each ticker has rolling 7d and 30d windows. We track THREE numbers per window:
- **bearish** = sum of negative-score actions
- **bullish** = sum of positive-score actions
- **net** = bearish + bullish

Firing logic is based on the bearish-only number (not the net) — a recent bearish cluster shouldn't be masked by older bullish actions in the same window.

| Condition | Status |
|---|---|
| Bearish 30d sum < -15 | **FIRED** (canonical Tier 2 trigger) |
| Bearish 7d sum < -10 | **EARLY WARNING** |
| Bullish 30d sum > +15 | **BULLISH CONFIRM** |
| Single note < -5 OR Tier A note < -3 | Flagged in brief |

Why this matters: in Example C below the cascade shows 30d bearish = -25.5 and 30d bullish = +7.5 simultaneously. Net is -18.0 (fires anyway in this case). But imagine the cascade hit AFTER a strong bullish month — net might be -2 while bearish is still -25.5. With net-only firing, the recent bearish move would be hidden. With bearish-only firing, the signal still fires sharply.

---

## Example A — Typical positive day

> Mirae Asset raised SK Hynix target ₩1,280K → ₩1,370K (+7.0%). FY27 EPS unchanged. Rating maintained at Buy.

| Component | Value | Points |
|---|---|---|
| Direction | +1 | |
| Rating | maintained | 0 |
| PT change | +7.0% (5-10% bracket) | +1.0 |
| FY27 EPS | unchanged | 0 |
| Sum | | +1.0 |
| Tier multiplier | A | ×3.0 |
| Combo (only 1 component) | | ×1.0 |
| **Final score** | | **+3.0** |

What you see in the brief: nothing special — score within normal range. Action appears only in the EPS Revisions Watch detail section if the signal is already approaching/firing. Rolling 7d bullish gets +3.0.

---

## Example B — Single-note early warning

> Mizuho cut Micron PT $740 → $595 (-19.6%), revised FY27 EPS down -12.0%, downgraded Buy → Hold.

| Component | Value | Points |
|---|---|---|
| Direction | -1 | |
| Rating | downgrade | -1.0 |
| PT change | -19.6% (10-20% bracket) | -1.5 |
| FY27 EPS | -12.0% (10-20% bracket) | -1.5 |
| Sum | | -4.0 |
| Tier multiplier | A | ×3.0 |
| Combo (3 aligned components) | | ×2.0 |
| **Final score** | | **-24.0** |

What you see in the brief — a 🚨 banner at the top:

> 🚨 **EPS REVISIONS — EARLY WARNING**
> 7d bearish < -10 — approaching trigger
> - **MU**: 30d bearish=-24.0 / 30d bullish=0.0 / 30d net=-24.0
> - **000660.KS**: 30d bearish=0.0 / 30d bullish=0.0 / 30d net=0.0
> - **005930.KS**: 30d bearish=0.0 / 30d bullish=0.0 / 30d net=0.0

And in the detail section "4b. EPS Revisions Watch":

| Broker | Tier | Ticker | Action | PT change | FY27 EPS | Score |
|---|---|---|---|---|---|---|
| Mizuho | **A** | MU | Downgrade Buy→Hold + PT $740→$595 | -19.6% | -12.0% | **-24.0** |

This is the canonical "we're rolling" pattern: one influential Tier A broker takes a comprehensive view change in a single note, immediately moving the rolling sum well past the trigger threshold.

---

## Example C — Cascade firing

> Past 7 days, Tier A actions only:
>
> - **Mon**: Goldman (Shawn Kim) — SK Hynix ₩1,790K→₩1,650K (-7.8%)
> - **Tue**: Mirae Asset — SK Hynix ₩1,370K→₩1,280K (-6.6%), FY27 EPS -5%, downgrade Buy→Hold
> - **Thu**: JPMorgan — Micron PT $740→$680 (-8.1%)
> - **Fri**: Morgan Stanley — SK Hynix ₩1,800K→₩1,620K (-10.0%)
> - **Mon (today)**: Mizuho — Micron PT $700→$640 (-8.6%), FY27 EPS -6%, downgrade

| Broker | Tier | PT% | EPS% | Rating | Score |
|---|---|---|---|---|---|
| Goldman Sachs | A | -7.8% | — | maintained | **-3.0** |
| Mirae Asset | A | -6.6% | -5% | downgrade | **-15.0** |
| JPMorgan | A | -8.1% | — | maintained | **-3.0** |
| Morgan Stanley | A | -10.0% | — | maintained | **-4.5** |
| Mizuho | A | -8.6% | -6% | downgrade | **-18.0** |

**Per-ticker rolling sums** (today):

- **000660.KS** (SK Hynix): 7d bearish = -22.5 · 7d bullish = 0 · 7d net = -22.5
- **MU** (Micron): 7d bearish = -21.0 · 7d bullish = 0 · 7d net = -21.0

What you see at the top of the brief:

> 🚨 **EPS REVISIONS FIRED**
> Canonical Tier 2 signal: 30d bearish < -15
> - **000660.KS**: 30d bearish=-22.5
> - **MU**: 30d bearish=-21.0

The cascade fires correctly; both tickers cross the -15 bearish threshold.

---

## Example D — Tier C noise (correctly NOT firing)

> Daishin Securities cut SK Hynix target ₩1,200K → ₩1,150K (-4.2%). Rating maintained.

| Component | Value | Points |
|---|---|---|
| Direction | -1 | |
| Rating | maintained | 0 |
| PT change | -4.2% (<5% bracket) | -0.5 |
| FY27 EPS | unchanged | 0 |
| Sum | | -0.5 |
| Tier multiplier | C | ×1.0 |
| **Final score** | | **-0.5** |

Action gets logged but doesn't surface in the brief — below the |score|>5 single-note flag threshold and below the |Tier A score|>3 threshold. This is correct behavior; small Tier C adjustments shouldn't trigger anything.

---

## Historical acid test 1 — Aug 12, 2021 Morgan Stanley "Memory: Winter Is Coming"

This was the start of the prior memory cycle turn. Joseph Moore (Morgan Stanley) published pre-market ~8:05 AM ET on Thursday Aug 12, 2021:

- Micron: Overweight → Equal-weight (one-notch downgrade)
- PT $105 → $75 (-28.6%)
- SK Hynix also downgraded same day

**Same-day stock reaction:**

| Stock | Same-day move |
|---|---|
| Micron (MU) | -6.1% in early trading → -4.3% to -7% close |
| SK Hynix (000660.KS) | -4.7% |
| Samsung | -1.9% |
| Western Digital | -7% |

**Scoring this event in our system:**

| Component | Value | Points |
|---|---|---|
| Direction | -1 | |
| Rating | downgrade | -1.0 |
| PT change | -28.6% (≥20% bracket) | -2.0 |
| FY27 EPS | (not in public note — Mirae echo would extract later) | 0 |
| Sum | | -3.0 |
| Tier multiplier | A (Morgan Stanley) | ×3.0 |
| Combo (2 aligned components) | | ×1.5 |
| **Final score** | | **-13.5** |

What our pipeline would have surfaced:
- Single Tier A note with |score| > 3 → flagged as single-note alert
- 7d bearish hits -13.5 → past EARLY WARNING threshold (-10)
- 30d bearish at -13.5 → approaching but not yet at FIRED threshold (-15)

**Verdict**: pipeline catches the event correctly. Stock had already moved -6.1% by 9:46 AM ET (1.5 hours after note publish). Our next-day brief at 13:00 CEST would have surfaced the signal with full context the morning AFTER the news, when European investors are still inside the multi-day move (MU continued lower over following days; cycle peaked late 2021).

---

## Historical acid test 2 — Sept 15, 2024 Morgan Stanley "Winter Looms"

The bigger and more recent test. Shawn Kim + Duan Liu (Morgan Stanley) published Sunday Sept 15 US time, during Korean Chuseok holiday:

- SK Hynix: Overweight → **Underweight (TWO-NOTCH downgrade!)**
- PT 260,000 → 120,000 KRW (-54%)
- Samsung PT 105,000 → 76,000 KRW (-27.6%)

**First-trading-day reaction** (Thursday Sept 19 — Korean markets reopened post-Chuseok):

| Stock | Intraday low | Close |
|---|---|---|
| SK Hynix | -11% (lowest since Feb 8) | -7.8% |
| Hanmi Semiconductor | -8.2% | — |
| Samsung | -3.4% | — |

Per Korea Exchange investigation, Morgan Stanley Seoul branch had sold 1,011,719 SK Hynix shares on Sept 13 — 2 days BEFORE the public report.

**Scoring this event in our system:**

| Component | Value | Points |
|---|---|---|
| Direction | -1 | |
| Rating | **double_downgrade** (Overweight → Underweight) | **-2.0** |
| PT change | -54% (≥20% bracket) | -2.0 |
| FY27 EPS | (not in public note — would extract from Mirae echo if available) | 0 |
| Sum | | -4.0 |
| Tier multiplier | A (Morgan Stanley) | ×3.0 |
| Combo (2 aligned components) | | ×1.5 |
| **Final score** | | **-18.0** |

What our pipeline would have surfaced:
- Single Tier A note with |score| > 3 → flagged
- 30d bearish hits -18.0 in a single note → **immediately past FIRED threshold (-15)**
- Pipeline detection: Naver Finance ~30-60 min after note published, Korean press echo within 1-3 hours

**Verdict**: bull's-eye. Single comprehensive Tier A two-notch downgrade immediately fires the canonical Tier 2 signal — no need to wait for cascade.

Without the `double_downgrade` rating point (-2.0), this would have scored -13.5 instead of -18.0 and only triggered EARLY WARNING. The two-notch distinction matters.

---

## Why bearish-only firing matters — counterfactual

Suppose the cycle is still expanding in April (lots of bullish actions) and then a fresh Tier A bearish cluster hits in early May. With **net** firing:

| Month | Actions | Net 30d |
|---|---|---|
| April | 8 bullish (Tier A & B raises) | +22.0 |
| May 1 | quiet | +18.0 (bullish ages out) |
| May 8 | Mizuho -24 single note | -6.0 (looks calm, but isn't!) |
| May 10 | Goldman -3 | -9.0 |
| May 12 | Mirae -15 cascade | -22.0 (finally fires) |

The net-based system delays the signal by 4 days because the older bullish actions mask the recent bearish ones. With **bearish-only** firing:

| Day | Bearish 30d |
|---|---|
| May 8 | -24.0 (Mizuho FIRES single-note) |
| May 10 | -27.0 |
| May 12 | -42.0 (cascade compounding) |

Signal fires on May 8 instead of May 12 — 4 days of headstart on what the framework calls the "canonical Tier 2 turn."

The bullish context isn't lost — it's still tracked separately and visible in the brief as "bearish=-24 / bullish=+18 / net=-6". Layer 2 can call it convergence="conflicting" when the bear/bull gap is wide. But the trigger fires when warranted, regardless of how much bullish history sits in the window.

---

## What you'll see in the email — banner + section

When the signal approaches or fires, the email shows:

**At the top** (above exec summary):

> 🚨 EPS REVISIONS FIRED
> Canonical Tier 2 signal: 30d bearish < -15
> - 000660.KS: 30d bearish=-22.5
> - MU: 30d bearish=-21.0

**As a new section 4b** (between News and Calendar), the EPS Revisions Watch table:

| Ticker | Fired | 7d bearish | 30d bearish | 30d bullish | 30d net | Top impact actions |
|---|---|---|---|---|---|---|
| 000660.KS | FIRED | -22.5 | -22.5 | 0.0 | -22.5 | Mirae -15.0 / MS -4.5 / Goldman -3.0 |
| MU | FIRED | -21.0 | -21.0 | 0.0 | -21.0 | Mizuho -18.0 / JPMorgan -3.0 |

Plus a **"New broker actions today"** table listing every Tier A/B action from the current day with score breakdown.

For Tier A actions from US brokers without a PDF echo (Mirae/NH/Samsung Securities haven't published an echo note yet), each row carries a "⚠️ FY27 EPS not extracted — consider manual Bloomberg/Mirae check for fuller context" hint. This surfaces the gap explicitly — you decide whether to dig deeper that day.

---

## Honest limits

1. **Tier assignment is opinionated** — the Tier A/B/C list reflects an opinion on which brokers move the stock historically. Can be adjusted after 4-6 weeks of brief output if (e.g.) JPMorgan calls don't seem to drive Korean reaction as much as assumed. Just edit `BROKER_AUTHORITY_TIERS` in `src/config.py`.

2. **FY27 EPS extraction depends on source** — Mirae/NH/Samsung Securities PDFs give FY27 EPS via the Layer 1 digester (Haiku extracts from the estimate-summary page). Korean Tier 2 brokers and US brokers captured via Naver give only target price + rating, no EPS in structured form. The brief flags this explicitly per-action so the user knows when to do manual Bloomberg check vs trust the score.

3. **US broker coverage on SK Hynix** — until the ADR launches (June/July 2026), US brokers covering 000660.KS reach us via:
   - **Naver Finance** (catches US brokers as they're picked up by Korean financial press — Goldman = "골드만삭스", Morgan Stanley = "모건스탠리", etc. — our scraper maps to English)
   - **Korean press echoes** within 1-3 hours
   - **Bloomberg/Reuters wire** via Google News

   After ADR launches, Finnhub structured feed gives US broker actions on the SK Hynix ADR ticker directly. At that point US-broker rationale depth equals Korean coverage.

4. **Magnitude bracketing** — a 9.9% cut scores the same as a 5.1% cut (both bracket 1.0). Could be replaced with a smooth function if we observe systematic mis-firing, but bracketed approach is more interpretable.

5. **Paid alternatives that DON'T help**:
   - **Koyfin Plus ($22/mo)** — Capital IQ feed is 24-72hr behind Naver and Finnhub. Slower than what we have. Doesn't add value.
   - **TIKR Plus ($19/mo)** — same Capital IQ feed as Koyfin. Same speed issue.
   - **Benzinga Pro ($99-177/mo)** — fast for US analyst actions but weak on KRX. Helpful for MU specifically; doesn't change the SK Hynix picture.
   - **Bloomberg Terminal ($2,400/mo)** — real-time, but unnecessary for daily-brief cadence.

6. **For real-time mobile alerts** (separate from this daily brief) — see the alerter discussion in a separate future module. Pushover + custom Tier A filter delivers 20-40 min mobile push alerts for $5 one-time.
