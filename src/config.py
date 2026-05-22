"""
Configuration for the Memory Cycle Brief pipeline.

Defines the tickers tracked, the indicator framework (tiers 1-4),
and the thresholds that classify each signal as fired vs not fired.

CHANGELOG (this file):
  v3.39.7.3 (2026-05-20) — t1_spot_decline display name updated to "DRAM Price Evolution (spot + contract matrix)" / short_label "DRAM Price Evolution" to reflect that the matrix encompasses both spot drawdown AND contract growth, not just spot. ID kept as t1_spot_decline for state-file back-compat. No logic change; the underlying matrix evaluator and trigger config are unchanged from v3.39.7.
  v3.39.7.1 (2026-05-20) — Two updates on t1_spot_decline: (1) description and what_to_look_for prose updated to use the new vocabulary ("Returns Not fired / Partially fired / Fired per cell" instead of "Returns Hold / Warn / Fire per cell"; "Not fired = R1/R2 with low drawdown..." instead of "HOLD = R1/R2..."). (2) CYCLE_MATRIX_STATUS_MAP at the bottom simplified to identity ({"Not fired": "Not fired", ...}) since MatrixCell.HOLD/WARN/FIRE .value strings now match the framework status vocabulary directly; the map is kept as a documented constant for callers in fast_tier.py.
  v3.39.7 (2026-05-20) — t1_spot_decline entry reworked: trigger_patterns/trigger_numeric/cadence/stitching all kept as references (Phase A/B/C cycle reasoning still applies to news commentary) but the SCORING moves to a 5x5 matrix evaluated separately. Indicator name updated from "DDR5 spot first MoM decline" → "Cycle matrix — contract growth × spot drawdown". what_to_look_for rewritten to describe matrix logic (R1-R5 contract growth bands × C1-C5 spot drawdown bands). New CYCLE_MATRIX_* config block appended at end: CYCLE_MATRIX_DATA_DIR, four CSV paths, CYCLE_MATRIX_PRIMARY_SKU, CYCLE_MATRIX_WEIGHTS (tone 0.4 + supplier 0.4 + spot_lead 0.2), CYCLE_MATRIX_MAX_EXTRACTIONS_PER_RUN. Matrix evaluator lives in src/data/cycle_matrix.py. Anchor priority: server_ddr5 > conventional_dram_aggregate > supplier_specific (supplier_specific feeds supplier_adj, never anchor).
  v3.39.3.1 (2026-05-18) — RSS_FEEDS: dropped "AnandTech" (https://www.anandtech.com/rss/). Feed has been emitting `feed parse error: not well-formed (invalid token)` in every run since at least v3.39.1 — the published XML is malformed and feedparser bails. Site now publishes ~5 articles/month (down from the 2010s heyday); the maintenance cost of fixing the parser doesn't pay back. Tom's Hardware + DRAMeXchange + Google News cover the same beat. Companion deletions of NH Investment + Samsung Securities live in src/data/korean_brokers.py and src/deep_tier.py; no config-level change for those.
  v3.39.3 (2026-05-18) — TrendForce ingestion repair. Three config-level changes; companion patches in news.py same release. (a) RSS_FEEDS: dropped "TrendForce": "https://www.trendforce.com/news/feed" — that URL no longer publishes XML, replaced by "DRAMeXchange Weekly Research": "https://www.dramexchange.com/rss.xml". DRAMeXchange is TrendForce's sister property and the RSS is well-formed, current (most-recent items within a few days), and carries TrendForce-authored weekly research commentary which is exactly the behavioral-signal language Tier 1 t1_price_cuts_no_demand and t1_oem_inventory look for. (b) SCRAPE_TARGETS: "TrendForce News" URL fixed from /news/category/dram to /news/category/semiconductors/dram/ (TrendForce restructured the path; old URL returned 200 with 0 matching <article> selectors → silent zero). news.py scrape_trendforce() updated to match. (c) SCRAPE_TARGETS: added "TrendForce DRAM Spot (price table)" → /price/dram/dram_spot — handled by new scrape_trendforce_prices() in news.py with table parsing + freshness guard (stale snapshots tagged [STALE] in NewsItem title so Layer 2 won't fire Tier 1 on them).
  v3.38.5 (2026-05-17) — SONNET_MAX_TOKENS 24000 → 16000. The 16:25 UTC May 17 run had the Anthropic SDK refuse the Sonnet call with "Streaming is required for operations that may take longer than 10 minutes" — client-side guard that demands streaming when max_tokens crosses an estimated 10-min wall-time threshold. 24K crossed it; 16K stays under. Truncation risk that originally motivated the bump is covered by the json_repair fallback (v3.38.4) — if Sonnet truncates at 16K, json_repair returns best-effort partial dict. Proper fix (streaming) deferred to v3.39. Two non-config v3.38.5 fixes bundled into this release (not config-affecting, see their files): src/analysis/llm_summarizer.py (real root cause of Haiku failure: broken `_strip_code_fence` returned empty string on closed fences); .github/workflows/deep_tier.yml (commit step now stages state/deep_tier_diagnostics.json + state/transcript_status.json, fixing the silent loss of these v3.38 files at runner shutdown).
  v3.38.4 (2026-05-17) — Two changes. (a) HAIKU_MAX_TOKENS 8000→16000 and SONNET_MAX_TOKENS 16000→24000. The 15:32 UTC run had Haiku triage of 50 news items fail JSON parse at char 13926; 8K cap was below floor for that volume. Sonnet didn't fail JSON this run but it was canceled mid-generation by the workflow timeout — bumping preemptively. Both well under each model's 64K hard cap. (b) RSS_FEEDS pruned 19→14 entries: dropped Damnang Substack + Investing.com MU/NVDA/AMAT/ASML. The v3.38.3 UA change to plain Chrome UA fixed ~12 other previously-403-ing feeds but Cloudflare-fronted sources stayed 403. Cloudflare uses TLS fingerprint (JA3/JA4) and HTTP/2 fingerprint in addition to UA — Python requests has a recognizable fingerprint regardless of UA string. Bypass would need curl-cffi (mimics Chrome TLS handshake). Deferred to v3.39 for Damnang (architectural-signal source Raul flagged as critical); Investing.com x4 dropped permanently since Yahoo Finance + Benzinga + broker-name Google News cover the same signals.
  v3.38.3 (2026-05-17) — Two changes. (a) HAIKU_MAX_TOKENS 4000→8000 and SONNET_MAX_TOKENS 8000→16000. The 14:31 UTC run's Haiku triage and Sonnet analysis both produced truncated JSON ("Unterminated string"). Math: 46 news items × ~100-150 output tokens/item ≈ 5,500-7,000 tokens (over 4k Haiku cap); 22 indicator assessments × ~300-500 tokens ≈ 6,600-11,000 (over 8k Sonnet cap). Both safely under each model's 64K hard cap. Per Anthropic docs, max_tokens doesn't factor into rate limits — no cost downside to headroom. (b) RSS_FEEDS pruned from 32 to 19 entries: dropped 13 URLs confirmed 404/DNS in the same run (Yonhap, Anthropic News, Meta AI Blog, SEMI press, Reuters Technology, Seeking Alpha, The Elec, Nikkei, Pulse, vLLM, SGLang, DeepSeek, Artificial Analysis). Signal coverage preserved by existing Google News queries (KV cache compression, DDR5 spot price, broker combos, etc.). TODO v3.39 inline at bottom of RSS_FEEDS for URL hunt.
  v3.33  (2026-05-16) — Model strings fixed: HAIKU_MODEL='claude-haiku-4-5', SONNET_MODEL='claude-sonnet-4-6' (was the literal "haiku"/"sonnet" which Anthropic API doesn't accept). MOTLEY_FOOL_TICKERS gains exchange field per ticker (needed for per-ticker URL pattern in v3.33 scraper rewrite).
  v3.32  (2026-05-14) — Added YAHOO_TRANSCRIPT_TICKERS (primary transcript source); IR_SOURCES gains transcript_capable flag (SK Hynix/Samsung = False — IR sites are materials-only); scrubbed 24 stale "Quartr" refs in INDICATORS sources/stitching → "Yahoo Finance"
  v3.31  (2026-05-14) — Added IR_SOURCES, MOTLEY_FOOL_TICKERS, AI_LAB_IR_WATCH; OpenAI/Anthropic listing-watch queries
  v3.3   (2026-05-12) — (no direct change — TRACKED_* lists added v3.1/3.2)
  v3.2   (2026-05-12) — RSS feeds 24->32, queries 26->56, TRACKED_CONCEPTS added
  v3.1   (2026-05-12) — RSS feeds 17->24, broker+analyst queries, TRACKED_ANALYSTS/BROKERS/COMPANIES
"""

import os
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Tickers tracked
# ============================================================

CORE_TICKERS = {
    "SK Hynix": "000660.KS",     # KOSPI
    "Micron": "MU",               # NASDAQ
    "Samsung Electronics": "005930.KS",  # KOSPI
}

WIDER_BASKET = {
    **CORE_TICKERS,
    "ASML": "ASML",
    "Applied Materials": "AMAT",
    "NVIDIA": "NVDA",
    "Broadcom": "AVGO",
    "TSMC": "TSM",
    # v3.39.7.8 — Chip designer expansion per user direction 2026-05-21.
    # These join NVDA/AVGO as Tier B "first-layer bill-payers" — companies
    # that BUY memory chips and integrate them into GPUs/CPUs/AI
    # accelerators. Daily price moves now visible in the section 1 price
    # table alongside the rest of the WIDER_BASKET. Cerebras IPO'd
    # 2026-05-14 on Nasdaq at $185 (closed first day at $331, +68%); the
    # ticker has limited price history so the section 1 1Y / 3M / 6M /
    # 1M columns will show "—" until enough days accumulate.
    "AMD": "AMD",
    "Intel": "INTC",
    "Cerebras": "CBRS",
}

BENCHMARKS = {
    "MSCI ACWI Semiconductor": "^SOX",  # PHLX Semi as proxy; MSCI Semi is harder to get free
    "S&P 500": "^GSPC",
    "KOSPI": "^KS11",
    # DRAM ETF — pure memory exposure proxy. Roundhill DRAM ETF.
    # Used in t3_relative_strength as a cleaner memory-vs-broad-semi comparison
    # than SK Hynix or MU alone (which carry single-stock idiosyncrasies).
    # If DRAM ETF underperforms SOXX or SMH, that's the pure-memory cycle signal.
    "Roundhill DRAM ETF": "DRAM",
    "VanEck Semi ETF (SMH)": "SMH",  # broader semi for cross-check vs SOX
}

# Hyperscaler watch list for CapEx monitoring
# v3.39.7.4 — Oracle dropped. Empirical analysis (May 20 session) showed
# ORCL's CapEx/revenue (57%) was the only one ever to cross the 40% trigger
# threshold across the 4-hyperscaler cohort, but ORCL is structurally
# different — its revenue base ($15B/Q vs MSFT's $70B/Q) and recent OCI
# catch-up push distort the ratio. Including it produced false signals
# (indicator always "fired" because of ORCL) while masking the real
# cycle behaviour of the four genuine hyperscalers.
HYPERSCALERS = {
    "Microsoft": "MSFT",
    "Alphabet": "GOOGL",
    "Meta": "META",
    "Amazon": "AMZN",
}

ALL_TICKERS = {**WIDER_BASKET, **BENCHMARKS, **HYPERSCALERS}


# ============================================================
# Tier indicator framework
# ============================================================

@dataclass
class Indicator:
    """A single cycle indicator with its definition, trigger patterns, and stitching logic.

    Three trigger types:
      - NUMERIC: direct comparison against a value from a structured source (yfinance,
                 Finnhub, computed). Encoded in `trigger_numeric`.
      - PATTERN: phrase patterns within a cadence window (e.g., 2+ articles in 14 days
                 containing specific phrases). Encoded in `trigger_patterns`.
      - STITCHED: requires corroboration across 2+ independent sources because no single
                 source covers both sides of the trigger. Encoded in `stitching`.

    Most indicators have multiple trigger types — e.g., t1_oem_inventory needs supplier-
    side AND buyer-side evidence (stitched), each detected via numeric or pattern logic.
    """
    id: str
    tier: int  # 1, 2, 3, or 4
    name: str
    description: str
    what_to_look_for: str  # Plain-English summary of trigger (for email + LLM)
    sources: list[str] = field(default_factory=list)
    # Crisp trigger encoding — used by the Haiku triage prompt
    trigger_patterns: list[str] = field(default_factory=list)  # exact phrases to detect
    trigger_numeric: Optional[dict] = None  # {field: value, comparison: "lt"|"gt"|"eq", window: "5d"|"30d"}
    cadence: Optional[str] = None  # e.g. "2 articles within 14 days", "MoM print", "single event"
    stitching: Optional[str] = None  # how multiple sources combine; None = single-source
    initial_status: str = "Not fired"
    # v3.39.2: short_label for the supercycle chart's annotation boxes.
    # The chart's "T1 fired (X/Y)" panels list indicators by short_label,
    # not `name` — names like "DDR5 spot first MoM decline" are too long
    # for the panel layout. Hand-curated to mirror the user-facing labels
    # in the original supercycle chart screenshot (e.g. "Spot first decline",
    # "CapEx parabolic"). Falls back to `name` if unset.
    short_label: Optional[str] = None


INDICATORS: list[Indicator] = [
    # =====================================================================
    # Tier 1 — Early warning (lead stock peak by 3-6 months)
    # =====================================================================

    Indicator(
        id="t1_spot_decline",
        tier=1,
        # v3.39.7.3 — display name updated from "Cycle matrix" / "DDR5 spot first
        # MoM decline" to "DRAM Price Evolution (spot + contract matrix)" to
        # reflect that the matrix encompasses BOTH spot drawdown and contract
        # growth dynamics, not just spot. The ID t1_spot_decline is preserved
        # for state-file backward compatibility (prior_state.json keys on it).
        name="DRAM Price Evolution (spot + contract matrix)",
        short_label="DRAM Price Evolution",
        description=(
            "5x5 trigger matrix mapping DRAM contract QoQ growth (rows) against "
            "DDR5 16Gb 4800/5600 spot drawdown from rolling 12-week peak (cols). "
            "Returns Not fired / Partially fired / Fired per cell. Replaces the "
            "v3.39 absolute-threshold trigger ($38.2 spot / $950 module). The "
            "matrix is the framework's canonical early-warning signal — spot "
            "drawdown leads contract direction by 6-10 weeks. Matrix engine: "
            "src/data/cycle_matrix.py; charts: src/analysis/chart_generator.py."
        ),
        what_to_look_for=(
            "Today's cell position on the 5x5 matrix. ROWS (contract QoQ): "
            "R1 >=+30%, R2 +20-30%, R3 +10-20%, R4 +5-10%, R5 <=+5%. "
            "COLS (spot drawdown from rolling 12w peak): C1 at peak, C2 <5% off, "
            "C3 5-10% off, C4 10-20% off, C5 >=20% off. Not fired = R1/R2 with low "
            "drawdown (full position). Partially fired = mid-band (review thesis, "
            "consider 25-33% trim). Fired = any R4-R5 row, any C5 column, or R3+C4. "
            "Historical cycle peaks all landed in R5 by the time contract growth "
            "hit ~0% — the matrix exists to act BEFORE that. Contract row uses a "
            "SYNTHETIC estimate: anchor (most recent TrendForce quarterly forecast, "
            "preferring server_ddr5 > conventional_dram_aggregate segments) + 0.4 * "
            "tone_adj + 0.4 * supplier_adj + 0.2 * spot_lead_adj, each capped "
            "+/-30pp."
        ),
        sources=[
            "TrendForce DRAM Spot Price page (free, daily) — primary spot signal",
            "TrendForce DRAM Contract Price page (free, monthly, lagged) — secondary",
            "TrendForce Press Center quarterly forecasts — anchor source",
            "TrendForce news commentary — tone signal extraction (Haiku)",
            "Korean Electronic Times (etnews), ChosunBiz — supplier-specific QoQ",
            "DigiTimes Asia — contract echo, 2-4 day lag",
            "Tom's Hardware RAM index (proxy)",
            "Korean broker PDFs (Mirae/NH cite spot charts)",
        ],
        trigger_patterns=[
            # Phase A — cooling tone (earliest, target for matrix tone_adj)
            "momentum slowed", "cooling negotiations",
            "ending the extreme seller's market", "more balanced market",
            "pricing power moderating", "rally fading",
            # Phase B — softening
            "DDR5 spot softened", "DDR5 spot weakened", "DDR5 spot weakness",
            "buyers more selective", "demand softening",
            # Phase C — destruction (lagging confirmation)
            "DDR5 spot declined", "DDR5 spot fell", "DDR5 16Gb decline",
            "DDR5 module price decline", "price cuts failed to revive",
            # Korean broker note phrasings:
            "spot price correction", "spot pricing under pressure",
        ],
        trigger_numeric={
            # Matrix cell lookup is the authoritative trigger; keys below
            # document the matrix inputs for engineering reference.
            "matrix_cell": {
                "logic": "MATRIX[(contract_row, spot_col)]",
                "rows_from_synthetic_qoq": {"R1": ">=30", "R2": "20-30",
                                            "R3": "10-20", "R4": "5-10", "R5": "<=5"},
                "cols_from_spot_drawdown_pct": {"C1": ">=0", "C2": "-5 to 0",
                                                "C3": "-10 to -5", "C4": "-20 to -10",
                                                "C5": "<=-20"},
                "cells_warn": ["R1C4", "R2C3", "R2C4", "R3C1", "R3C2", "R3C3"],
                "cells_fire": ["R*C5", "R3C4", "R4C*", "R5C*"],
            },
            "ddr5_16gb_spot_session_avg": {
                "primary_sku": "DDR5_16Gb_4800_5600",
                "rolling_peak_window_weeks": 12,
                "weekly_aggregation": "mean of session_avg by week",
            },
            "synthetic_contract_qoq": {
                "formula": "anchor + 0.4*tone + 0.4*supplier + 0.2*spot_lead",
                "each_adjustment_capped_pp": 30,
            },
        },
        cadence=(
            "Spot: daily (TrendForce free page) → weekly aggregation for matrix. "
            "Contract anchor: quarterly forecasts (real-time when published) + "
            "monthly contract actuals (1-2mo lag on free tier). "
            "Tone signals: continuous, lookback 4 weeks for tone_adj."
        ),
        stitching=(
            "Single-source: matrix verdict from src/data/cycle_matrix.py evaluating "
            "data/dram_spot_history.csv + data/dram_contract_quarterly_forecasts.csv "
            "+ data/dram_contract_signals.csv. Trigger phrases above still flow into "
            "the news pipeline for Layer 2 per-signal reasoning, but the BADGE "
            "status (Hold/Warn/Fire → Not fired/Partially fired/Fired) is set by "
            "the matrix verdict, not by phrase frequency. Phrase matches feed the "
            "tone_adj component, weight 0.4."
        ),
    ),

    Indicator(
        id="t1_lead_times",
        tier=1,
        name="Lead times shrinking",
        short_label="Lead times shrinking",
        description=(
            "Memory product lead times contracting from peak levels. "
            "Available via earnings call commentary + Korean broker PDF citations."
        ),
        what_to_look_for=(
            "LPDDR5X lead time falling below 26 weeks (current 26-39). HBM3E delivery "
            "slots opening up (currently sold out through 2026). Detected via memory "
            "maker earnings calls + OEM earnings + Korean broker PDFs citing TrendForce."
        ),
        sources=["Yahoo Finance earnings transcripts", "Korean broker PDFs", "TrendForce press releases"],
        trigger_patterns=[
            # Direction language (must pair with lead time / delivery context):
            "lead time shrinking", "lead times shrinking", "lead times shortening",
            "lead time easing", "lead times normalizing", "delivery times improving",
            "LPDDR5X availability improving", "LPDDR5X lead time",
            "HBM3E availability", "HBM delivery slots opening",
            "memory allocation easing", "supply tightness easing",
            # Earnings call mgmt phrasings:
            "lead times have come in", "lead times are easing",
        ],
        trigger_numeric={
            "lpddr5x_lead_time_weeks": {"trigger_below": 26, "comparison": "vs_prior_quarter"},
        },
        cadence="Single Yahoo Finance earnings call statement OR Korean broker PDF numeric citation",
        stitching=(
            "Earnings transcript (Yahoo Finance) provides direct mgmt statement = highest signal. "
            "Korean broker PDF numeric citation = secondary confirm. News articles using "
            "shrinking/shortening language = tertiary. Trigger on primary alone; corroboration not required."
        ),
    ),

    Indicator(
        id="t1_price_cuts_no_demand",
        tier=1,
        name="Demand destruction language",
        short_label="Demand destruction",
        description=(
            "TrendForce/DigiTimes/Korean press commentary using specific phrases that "
            "historically appear at cycle turns. Applied to DDR5 specifically (not DDR4 EOL noise)."
        ),
        what_to_look_for=(
            "Phrases like 'price cuts failed to revive', 'buyers cautious', 'even at lower "
            "prices', 'transactions slowing', 'demand failing to absorb supply' — applied "
            "to DDR5 or generic 'memory' (not DDR4-specific)."
        ),
        sources=["TrendForce news", "DigiTimes Asia headlines", "Korean press (KED, Korea Herald)"],
        trigger_patterns=[
            # TrendForce canonical phrasings (already fired for DDR4 on Apr 28, 2026):
            "price cuts failed to revive",
            "price cuts have not stimulated",
            "price reductions failed to",
            # Behavioral/buyer language:
            "buyers cautious", "buyers are cautious", "buyers stepping away",
            "purchasing cautious", "transactions slowing", "trading slowing",
            "demand weakening", "demand failing to absorb",
            "even at lower prices", "lower prices not stimulating",
            "inventory rebuilding at OEMs", "OEMs stockpiling",
        ],
        trigger_numeric=None,
        cadence="2+ articles within 14 days OR 1 article with 2+ phrases. Must apply to DDR5 or 'memory' (not DDR4-only).",
        stitching=(
            "Pure pattern detection across TrendForce + DigiTimes + Korean press. "
            "Haiku triage flags articles matching phrase patterns; signal fires on "
            "cadence threshold. CRITICAL: exclude DDR4-only articles — DDR4 EOL distorts."
        ),
    ),

    Indicator(
        id="t1_oem_inventory",
        tier=1,
        name="OEM inventory rising while supplier still tight",
        short_label="OEM inv mismatch",
        description=(
            "Supplier-buyer inventory gap exposes prior double-ordering. Requires "
            "BOTH sides confirmed (stitched signal)."
        ),
        what_to_look_for=(
            "Supplier inventory <3 weeks AND OEM/buyer inventory >7 weeks. Gap >5 weeks "
            "is unprecedented (currently ~7 weeks per TrendForce Q3 2025). Trigger requires "
            "evidence from BOTH supplier-side (memory makers) AND buyer-side (Dell/HPE/Lenovo)."
        ),
        sources=[
            "Yahoo Finance earnings (memory makers state supplier inventory weeks)",
            "Yahoo Finance earnings (Dell/HPE/Lenovo state component inventory)",
            "TrendForce monthly press releases",
        ],
        trigger_patterns=[
            # Supplier-side phrasings (low = expected, rising = trigger):
            "supplier inventory", "we exited the quarter at", "weeks of inventory",
            "inventory weeks", "inventory remained lean", "inventory remained tight",
            # Buyer-side phrasings (rising = trigger):
            "component inventory rebuilding", "memory inventory elevated",
            "OEM inventory rising", "buyer inventory rebuilding",
            "stockpiling at OEMs", "channel inventory elevated",
        ],
        trigger_numeric={
            "supplier_inventory_weeks": {"trigger_above": 3, "context": "supplier-side"},
            "oem_inventory_weeks": {"trigger_above": 7, "context": "buyer-side"},
            "inventory_gap_weeks": {"trigger_above": 5, "computed": True},
        },
        cadence="Quarterly earnings cycle (~3 months) — each earnings call updates numbers",
        stitching=(
            "STITCHED — REQUIRES BOTH SIDES. Supplier inventory from memory maker earnings "
            "(Yahoo Finance) + buyer inventory from OEM earnings (Yahoo Finance Dell/HPE/Lenovo) OR "
            "TrendForce monthly summary citing both. Trigger fires only when both sides "
            "show the gap widening simultaneously."
        ),
    ),

    Indicator(
        id="t1_efficiency_shock",
        tier=1,
        name="AI inference efficiency breakthrough",
        short_label="AI efficiency shock",
        description=(
            "Algorithmic or architectural improvement reducing memory-per-token by >2x. "
            "TurboQuant-class shocks. The cycle-breaking risk."
        ),
        what_to_look_for=(
            "Major announcement from Google / OpenAI / Anthropic / Meta / DeepSeek / "
            "Mistral. Specific phrases: 'KV cache compression', 'inference efficiency 2x+', "
            "'memory footprint reduction', 'quantization breakthrough'. Source must be "
            "official lab blog, arXiv, or 3+ mainstream tech publications within 48 hours."
        ),
        sources=[
            "AI lab official blogs (Google Research, OpenAI, Anthropic, Meta AI)",
            "arXiv cs.LG", "HackerNews Algolia", "Mainstream tech press",
        ],
        trigger_patterns=[
            "KV cache compression", "KV-cache compression",
            "inference efficiency", "inference speedup",
            "memory footprint reduction", "memory-per-token",
            "quantization breakthrough", "TurboQuant",
            "throughput improvement", "tokens per second",
            # Must co-occur with a multiplier:
            "2x speedup", "3x speedup", "4x speedup", "6x speedup", "8x speedup",
            "2x compression", "3x compression", "4x compression", "6x compression",
        ],
        trigger_numeric={
            "efficiency_multiplier": {"trigger_above": 2.0},
        },
        cadence=(
            "Single announcement from AI lab official blog or arXiv preprint. "
            "Multiplier of 2x+ required. Confirmed if 3+ tech publications echo within 48hr."
        ),
        stitching=(
            "Primary: AI lab official blog (Google Research, OpenAI, Anthropic, Meta AI) "
            "or arXiv. Secondary: mainstream tech press echo. Trigger on primary alone if "
            "multiplier >= 2x; on secondary alone requires 3+ publications within 48hr."
        ),
    ),

    Indicator(
        id="t1_demand_destruction",
        tier=1,
        name="PC / smartphone / server demand cuts",
        short_label="PC/smartphone cuts",
        description="IDC, Counterpoint, Gartner revise forecasts down. Authoritative single-source signals.",
        what_to_look_for=(
            "IDC PC shipment forecast revised to -5% or worse for 2026 (current -2.4 to "
            "-11.3% — already fired). Counterpoint smartphone <0% (current -2%). Gartner "
            "data center server forecast cut."
        ),
        sources=["IDC press releases", "Counterpoint Research", "Gartner press releases",
                 "Reuters / Bloomberg via Google News"],
        trigger_patterns=[
            "IDC revised", "IDC lowered", "IDC cut forecast",
            "Counterpoint revised", "Counterpoint cut",
            "Gartner revised", "Gartner cut",
            "PC shipments revised down", "smartphone shipments revised down",
            "server forecast cut", "data center forecast lowered",
        ],
        trigger_numeric={
            "pc_shipment_forecast_yoy_pct": {"trigger_below": -5.0},
            "smartphone_shipment_forecast_yoy_pct": {"trigger_below": 0.0},
        },
        cadence="Single press release from primary source (IDC/Counterpoint/Gartner) sufficient",
        stitching=(
            "These three firms are authoritative — single press release fires the signal. "
            "News coverage from Reuters/Bloomberg/CNBC corroborates timing but isn't required."
        ),
    ),

    # =====================================================================
    # Tier 2 — Mid-cycle (lead stock peak by 2-4 months)
    # =====================================================================

    Indicator(
        id="t2_eps_revisions",
        tier=2,
        name="Forward FY27/FY28 EPS revision direction — THE canonical Tier 2 signal",
        short_label="EPS revision flip",
        description=(
            "First month with net negative FY27 or FY28 EPS revisions for SK Hynix or "
            "Micron. The single most important missing signal — currently NOT FIRED."
        ),
        what_to_look_for=(
            ">5 analysts revise SK Hynix or MU FY27 EPS DOWN in a single month while <3 "
            "revise up. Single broker target price cut of >-10% vs prior is a strong "
            "early signal even before the count threshold."
        ),
        sources=[
            "Naver Finance (20+ Korean brokers, 30-60 min lag)",
            "Finnhub API (US analyst rating actions, 5-30 min lag)",
            "yfinance (consensus aggregates, 3-7 day lag)",
            "Mirae Asset / Samsung Securities / NH PDFs (rationale, 24-48hr)",
        ],
        trigger_patterns=[
            # Target price action keywords in broker notes:
            "target price cut", "target price lowered", "PT cut", "PT lowered",
            "downgrade to hold", "downgrade to sell", "estimates lowered",
            "FY27 EPS revised down", "FY28 EPS revised down",
            "earnings estimate cut",
        ],
        trigger_numeric={
            "fy27_eps_down_revisions_count_30d": {"trigger_above": 5, "ticker": "any of SKHX/MU/Samsung"},
            "fy27_eps_up_revisions_count_30d": {"trigger_below": 3, "concurrent_with_down": True},
            "single_broker_target_cut_pct": {"trigger_below": -10.0, "early_signal": True},
        },
        cadence=(
            "Rolling 30-day count of net direction. Single >-10% revision is an early signal. "
            "Reaches operational signal in ~5-15 trading days from cycle turn."
        ),
        stitching=(
            "Naver Finance is the structured signal source (count + direction). Finnhub does "
            "the same for US. Mirae/Samsung Securities/NH PDFs explain WHY (rationale). yfinance "
            "is the 3-7 day fallback. Trigger on Naver/Finnhub count alone; PDFs add depth."
        ),
    ),

    Indicator(
        id="t2_capex_parabolic",
        tier=2,
        name="CapEx announcements parabolic",
        short_label="CapEx parabolic",
        description=(
            "Memory makers' and hyperscalers' CapEx growing parabolic — building 2027-28 "
            "supply glut. ALREADY FIRED on aggregate; watch for 2027 guidance direction at Jul prints."
        ),
        what_to_look_for=(
            "Big Three combined CapEx >2x 2024 (fired). Hyperscaler 2026 aggregate >$725B "
            "(fired). NEW threshold: any of MSFT/GOOG/META/AMZN/ORCL guides 2027 CapEx "
            "ABOVE 2026 at Q2 2026 earnings prints. Bull confirm if above; bear catalyst if flat/down."
        ),
        sources=[
            "Hyperscaler IR pages (microsoft.com/investor, abc.xyz/investor, ir.meta.com, ir.aboutamazon.com, investor.oracle.com)",
            "Yahoo Finance earnings transcripts (full Q&A on CapEx)",
            "Memory maker earnings (Korean press + press releases)",
        ],
        trigger_patterns=[
            "2027 CapEx", "2027 capital expenditure", "FY27 CapEx",
            "CapEx guidance raised", "CapEx guidance lowered",
            "infrastructure investment plan", "AI infrastructure spending",
            "data center buildout", "fab capacity expansion",
        ],
        trigger_numeric={
            "msft_2027_capex_usd_b": {"baseline_2026": 190, "trigger_above": 190, "bull_signal": True},
            "googl_2027_capex_usd_b": {"baseline_2026": 185, "trigger_above": 185, "bull_signal": True},
            "meta_2027_capex_usd_b": {"baseline_2026": 105, "trigger_above": 105, "bull_signal": True},
            "amzn_2027_capex_usd_b": {"baseline_2026": 140, "trigger_above": 140, "bull_signal": True},
            "any_hyperscaler_2027_flat_or_down": {"bear_catalyst": True},
        },
        cadence="Quarterly earnings (Q2 2026 prints MSFT Jul 22, GOOG Jul 22, META Jul 23, AMZN Jul 24)",
        stitching=(
            "Hyperscaler IR press release primary. Yahoo Finance Q&A transcript adds depth on CapEx intent. "
            "Trigger on EACH hyperscaler independently — one blink is enough for bear catalyst."
        ),
    ),

    Indicator(
        id="t2_ltas_signed",
        tier=2,
        name="Multi-year LTAs aggressively signed",
        short_label="LTAs aggressive",
        description="Buyers panic-locking long-term supply. Paradoxical bearish signal — already fired.",
        what_to_look_for=(
            "3+ year HBM LTA announcements. 'Sold out through 2027/2028' phrasing. "
            "Currently fired (HBM3E sold out through 2026; Hynix HBM4 LTAs with Nvidia)."
        ),
        sources=["Memory maker press releases", "Korean press", "Yahoo Finance earnings transcripts (Q&A)"],
        trigger_patterns=[
            "sold out through 2027", "sold out through 2028", "sold out through 2029",
            "multi-year LTA", "long-term supply agreement",
            "3-year HBM agreement", "5-year HBM agreement",
            "HBM4 commitment", "forward HBM commitment",
            "supply secured through",
        ],
        trigger_numeric=None,
        cadence="Single press release sufficient",
        stitching="Single-source. Press releases or earnings call statements both work.",
    ),

    Indicator(
        id="t2_insider_sales",
        tier=2,
        name="Insider sales at memory makers (US Micron + Korean SK Hynix/Samsung)",
        short_label="Insider sales",
        description=(
            "C-suite or board sales — material insider activity. "
            "Coverage is COMPREHENSIVE: Micron via SEC EDGAR Form 4 (US standard); "
            "SK Hynix + Samsung Electronics + SK Square via DART (Korean regulatory equivalent). "
            "Korean entities don't file SEC Form 4 — DART is the canonical source for them."
        ),
        what_to_look_for=(
            "FOR MICRON: Form 4 filings via SEC EDGAR atom feed showing C-suite (CEO Mehrotra, "
            "CFO Murphy) or board sales with transaction code S, value >$1M. "
            "FOR SK HYNIX (corp_code 00164779): DART API disclosure types B (material event), "
            "D (ownership change), I (audit-related) — captures executive/board sales, "
            "stake reductions by major holders, board changes. "
            "FOR SAMSUNG ELECTRONICS (00126380): Same DART disclosure types as SK Hynix. "
            "Already fired Mar 2026 (Mehrotra Micron sales)."
        ),
        sources=[
            "SEC EDGAR Form 4 atom feed (Micron CIK 0000723125) — US insider sales",
            "DART API for SK Hynix (corp_code 00164779) — Korean material disclosures",
            "DART API for Samsung Electronics (corp_code 00126380) — Korean material disclosures",
            "DART API for SK Square (corp_code 01515323) — SK Hynix parent holding",
            "Finnhub /stock/insider-transactions — redundant US fallback for MU",
        ],
        trigger_patterns=[
            # Form 4 transaction codes parsed structurally — phrases are secondary:
            "insider sale", "Form 4 filing", "executive stock sale",
            "C-suite divestiture", "officer disposition",
            # Korean DART disclosure title patterns (defensive — Haiku triages):
            "주식양도",        # share transfer
            "임원변경",        # executive change
            "지분공시",        # ownership disclosure
            "단일판매",        # large single sale
        ],
        trigger_numeric={
            "form4_transaction_code": {"trigger_eq": "S", "context": "sale (US)"},
            "form4_value_usd": {"trigger_above": 1_000_000, "context": "material sale threshold (US)"},
            "form4_filer_is_csuite": {"trigger_eq": True, "context": "US"},
            "dart_material_disclosure_count_30d": {"trigger_above": 0, "context": "Any DART B/D/I-type filing for SK Hynix/Samsung in 30d window"},
        },
        cadence="Event-driven — single filing fires signal if C-suite or >$1M (US) / any material DART filing (Korea)",
        stitching=(
            "TWO REGULATORY FEEDS, both authoritative: EDGAR Form 4 atom feed for US (Micron); "
            "DART API for Korean entities (SK Hynix + Samsung + SK Square). "
            "These are governmental disclosure feeds — no ambiguity, no need to corroborate "
            "across multiple sources. Single feed entry = trigger. Finnhub redundant US fallback."
        ),
    ),

    Indicator(
        id="t2_pe_compressed",
        tier=2,
        name="Forward P/E compressed to 3-5x",
        short_label="Fwd P/E to 3-5×",
        description=(
            "Forward P/E compresses on PEAK earnings denominator — bearish, not bullish. "
            "Already fired for SK Hynix and Samsung."
        ),
        what_to_look_for=(
            "SK Hynix forward FY27 P/E below 5x (currently 3.7x — fired). Micron below 8x "
            "(currently 7-12x). Samsung below 6x (currently 5.3x — fired)."
        ),
        sources=["yfinance API (price + FY27 EPS estimate)", "Finnhub /price-target endpoint"],
        trigger_patterns=[],  # pure numeric
        trigger_numeric={
            "skhx_fwd_pe_fy27": {"trigger_below": 5.0},
            "mu_fwd_pe_fy27": {"trigger_below": 8.0},
            "samsung_fwd_pe_fy27": {"trigger_below": 6.0},
        },
        cadence="Continuous (daily computation from price + estimates)",
        stitching="Pure numeric, no stitching. yfinance primary, Finnhub cross-check.",
    ),

    Indicator(
        id="t2_hyperscaler_capex_revenue",
        tier=2,
        # v3.39.7.4 — display name changed from "Hyperscaler CapEx/revenue >40%"
        # to "Hyperscaler CapEx/OCF strain >50%". ID kept stable for prior_state
        # back-compat. Empirical analysis (May 20 session) showed the prior
        # CapEx/revenue >40% trigger NEVER fired across the 3 historical memory
        # crashes (2014/2018/2022) — the 4 hyperscalers' max CapEx/revenue
        # historically peaked at ~27% (META 2022). CapEx/OCF is dramatically
        # better: mean across 4 hyperscalers crossed 40% in all 3 crash years
        # and is at 62% TTM today. ORCL dropped (structural noise).
        name="Hyperscaler CapEx/OCF strain >50%",
        short_label="CapEx/OCF strain",
        description=(
            "CapEx relative to operating cash flow signals financing strain. "
            "Historic non-crash years: mean <40% across MSFT/GOOGL/AMZN/META. "
            "Memory cycle crash years (2014/2018/2022): mean >40%. "
            "CapEx/OCF >100% for any single co. = company must tap debt/equity "
            "to fund CapEx (the 'they can't rely on operations' regime)."
        ),
        what_to_look_for=(
            "Last-published quarter CapEx/OCF ratio per company. "
            "Partial fire: mean across MSFT/GOOGL/AMZN/META >40%. "
            "Full fire: mean across MSFT/GOOGL/AMZN/META >50%. "
            "Single-company strain overlay: any company >100% CapEx/OCF "
            "(Amazon 2021-22 reached 120-125% during cycle peak)."
        ),
        sources=["yfinance quarterly cashflow (CapEx + OCF)", "SEC EDGAR 10-Q filings"],
        trigger_patterns=[],  # pure numeric
        # v3.39.7.4 — trigger keyed on CapEx/OCF ratio per company. The aggregate
        # mean rule is applied in Layer 2 / Layer 3 (which see all 4 ratios),
        # not via single-company threshold (which would be too sensitive at AMZN's
        # structurally-elevated baseline of 50-70%). Individual >100% still flags
        # severe single-company strain as a secondary signal.
        trigger_numeric={
            "msft_capex_ocf_ratio": {"trigger_above": 1.00},   # >100% single-co strain
            "googl_capex_ocf_ratio": {"trigger_above": 1.00},
            "meta_capex_ocf_ratio": {"trigger_above": 1.00},
            "amzn_capex_ocf_ratio": {"trigger_above": 1.00},
            "mean_capex_ocf_ratio": {"trigger_above": 0.50},  # primary aggregate fire
        },
        cadence="Quarterly (each 10-Q filing)",
        stitching=(
            "Pure numeric — computed in estimates.py from yfinance quarterly_cashflow. "
            "Uses LAST published quarter per company (NOT TTM), per user decision "
            "2026-05-20: 'we are saying that this will crash in 3 months, so we go "
            "with last published quarter' — responsiveness over smoothness."
        ),
    ),

    Indicator(
        id="t2_spot_contract_spread",
        tier=2,
        name="Spot/contract spread narrowing or inverted",
        short_label="Spot/contract inverted",
        description=(
            "Spot prices weakening while contract still rises means spot has rolled. "
            "DDR4 inverted (fired). DDR5 not yet."
        ),
        what_to_look_for=(
            "DDR5 spot below DDR5 server contract for any product variant. Spread narrowing toward zero."
        ),
        sources=[
            "TrendForce monthly contract report press releases",
            "Mirae Asset PDFs (reproduce TrendForce spot+contract charts)",
            "Yahoo Finance earnings call commentary",
        ],
        trigger_patterns=[
            "spot weakness", "spot below contract", "spot-contract spread narrowing",
            "spot-contract inversion", "contract still rising while spot",
            "DDR5 spot weakness", "DDR5 contract resilient",
        ],
        trigger_numeric={
            "ddr5_spot_minus_contract_usd": {"trigger_below": 0, "indicates": "inversion"},
        },
        cadence="Monthly (last week of month TrendForce press release)",
        stitching=(
            "TrendForce monthly press release headline contract direction + spot weakness "
            "language. Mirae Asset PDFs reproduce the charts visually. Trigger on press "
            "release language OR Mirae chart showing inversion."
        ),
    ),

    # =====================================================================
    # Tier 3 — Concurrent confirms (at stock peak)
    # =====================================================================

    Indicator(
        id="t3_contract_decline",
        tier=3,
        name="First sequential decline in CONTRACT prices — most diagnostic Tier 3",
        short_label="Contract first decline",
        description="The single most diagnostic Tier 3 signal. Contract prices roll over MoM.",
        what_to_look_for=(
            "DDR5 server contract MoM decline of any magnitude in TrendForce monthly "
            "report (last week of month). OR HBM contract renegotiation downward (any "
            "reported renegotiation = dam break). Currently Q2 2026 +58-63% QoQ projected."
        ),
        sources=[
            "TrendForce monthly contract report press release",
            "Mirae Asset PDFs (Korean brokers cite the contract table)",
            "Yahoo Finance memory maker earnings commentary",
        ],
        trigger_patterns=[
            "contract prices declined", "contract prices fell", "contract pricing weakened",
            "DDR5 server contract decline", "DDR5 contract weakened",
            "HBM contract renegotiated", "HBM LTA renegotiated downward",
            "contract price reduction",
        ],
        trigger_numeric={
            "ddr5_server_contract_mom_pct": {"trigger_below": 0.0, "first_negative": True},
            "hbm_contract_renegotiation": {"trigger_eq": True, "any_occurrence": True},
        },
        cadence="Monthly (TrendForce last-week-of-month press release) + earnings-driven",
        stitching=(
            "TrendForce monthly press release is primary (always free, always headlines "
            "the contract direction). Mirae Asset PDF reproduction confirms numeric. "
            "Yahoo Finance earnings call commentary adds context."
        ),
    ),

    Indicator(
        id="t3_supplier_inventory_rebuild",
        tier=3,
        name="Supplier inventory rebuilds 2-3 → 4-5 weeks",
        short_label="Inventory rebuild",
        description="Memory makers' inventory weeks rising from record lows.",
        what_to_look_for=(
            "SK Hynix or Micron inventory weeks crossing 4 in TrendForce monthly print "
            "or earnings disclosure. Currently 2-3 weeks."
        ),
        sources=[
            "Yahoo Finance memory maker earnings (★ management states inventory weeks directly)",
            "Mirae Asset PDFs", "TrendForce monthly press releases",
        ],
        trigger_patterns=[
            "exited the quarter at", "weeks of inventory",
            "inventory rebuilding", "inventory weeks elevated",
            "supplier inventory increased", "inventory of 4 weeks", "inventory of 5 weeks",
        ],
        trigger_numeric={
            "skhx_inventory_weeks": {"trigger_above": 4},
            "mu_inventory_weeks": {"trigger_above": 4},
            "samsung_inventory_weeks": {"trigger_above": 4},
        },
        cadence="Quarterly earnings (every ~3 months) + monthly TrendForce",
        stitching=(
            "★ Yahoo Finance earnings transcripts BEST — management states verbatim each quarter "
            "('we exited Q1 at 2.3 weeks'). TrendForce monthly press release confirms. "
            "SK Hynix Q2 print Jul 29, Micron Q3 FY26 Jun 24."
        ),
    ),

    Indicator(
        id="t3_relative_strength",
        tier=3,
        name="Memory pure-plays underperform SOX",
        short_label="Pure-plays vs SOX",
        description="Memory stocks losing relative strength vs SOX semiconductor index.",
        what_to_look_for=(
            "SK Hynix (000660.KS) or Micron (MU) underperforms SOX (^SOX) by >10% over "
            "5 trading days. Currently fired then reversed (Mar 2026 fired, May rally erased)."
        ),
        sources=["yfinance API price data", "Finnhub quote endpoint (cross-check)"],
        trigger_patterns=[],  # pure numeric
        trigger_numeric={
            "skhx_minus_sox_5d_return_pp": {"trigger_below": -10.0},
            "mu_minus_sox_5d_return_pp": {"trigger_below": -10.0},
        },
        cadence="Rolling 5-trading-day window, computed daily",
        stitching="Pure yfinance computation. Finnhub redundant cross-check.",
    ),

    Indicator(
        id="t3_wfe_orders_crack",
        tier=3,
        name="ASML/SEMI WFE order book cracking",
        short_label="WFE order crack",
        description="Equipment makers' order books showing first weakness.",
        what_to_look_for=(
            "ASML or AMAT bookings -10% or worse QoQ. Book-to-bill below 1.0. SEMI "
            "worldwide WFE billings printing MoM decline."
        ),
        sources=["ASML earnings press releases", "AMAT earnings press releases",
                 "SEMI press releases RSS (semi.org)", "Yahoo Finance earnings transcripts",
                 "Finnhub for ASML/AMAT analyst reactions"],
        trigger_patterns=[
            "bookings declined", "bookings fell", "bookings weakened",
            "book-to-bill below", "book-to-bill ratio of 0",
            "WFE billings declined", "SEMI billings fell",
            "order book weakening",
        ],
        trigger_numeric={
            "asml_bookings_qoq_pct": {"trigger_below": -10.0},
            "amat_bookings_qoq_pct": {"trigger_below": -10.0},
            "book_to_bill_ratio": {"trigger_below": 1.0},
        },
        cadence="Quarterly (ASML/AMAT earnings) + monthly (SEMI billings)",
        stitching=(
            "ASML or AMAT earnings press release primary (Yahoo Finance adds depth). SEMI monthly "
            "billings RSS confirms broader picture. Single source sufficient for trigger."
        ),
    ),

    Indicator(
        id="t3_eps_miss_guidance",
        tier=3,
        name="EPS miss or beat narrowing on still-rising guidance",
        short_label="EPS miss / beat narrows",
        description="Beat narrows or first miss while forward guidance still positive — peak signal.",
        what_to_look_for=(
            "MU or SK Hynix beat <10% (vs prior 30-40% beats — current SKHX Q1 48.8%, "
            "MU Q2 41.86%). OR first miss while forward guidance maintained or raised. "
            "Scenario A and Scenario B from Part 8 Indicator 5."
        ),
        sources=["Memory maker earnings press releases", "Yahoo Finance full transcripts",
                 "Finnhub earnings + analyst reactions", "Investing.com per-ticker RSS"],
        trigger_patterns=[
            "earnings miss", "EPS missed", "results below consensus",
            "below estimates", "fell short of expectations",
            "guidance maintained despite", "raised guidance",
        ],
        trigger_numeric={
            "beat_magnitude_pct": {"trigger_below": 10.0, "context": "vs prior 30-40% pattern"},
            "miss_with_guidance_raised": {"trigger_eq": True, "scenario_b": True},
        },
        cadence="Quarterly (SK Hynix Jul 29 Q2, MU Jun 24 Q3 FY26)",
        stitching=(
            "Press release gives EPS number. Yahoo Finance Q&A reveals guidance subtext. Both "
            "needed for Scenario B detection (small miss + guidance still raised)."
        ),
    ),

    # =====================================================================
    # Tier 4 — Lagging (post-peak)
    # =====================================================================

    Indicator(
        id="t4_yoy_asp_negative",
        tier=4,
        name="Negative YoY ASP prints",
        short_label="YoY ASP negative",
        description="Memory ASPs print year-over-year declines.",
        what_to_look_for="DRAM or NAND ASP YoY change negative in TrendForce or supplier earnings.",
        sources=["Yahoo Finance earnings transcripts", "TrendForce press releases", "Mirae PDFs"],
        trigger_patterns=[
            "ASP declined", "ASP fell", "average selling price decline",
            "ASP YoY negative", "year-over-year price decline",
        ],
        trigger_numeric={
            "dram_asp_yoy_pct": {"trigger_below": 0.0},
            "nand_asp_yoy_pct": {"trigger_below": 0.0},
        },
        cadence="Quarterly earnings + monthly TrendForce",
        stitching="Memory maker earnings (Yahoo Finance) primary. TrendForce confirms third-party.",
    ),

    Indicator(
        id="t4_guidance_cuts",
        tier=4,
        name="Earnings guidance cuts",
        short_label="Guidance cuts",
        description="Memory makers cut forward guidance or pre-announce miss.",
        what_to_look_for="MU or SK Hynix or Samsung issues guidance cut or pre-announces miss.",
        sources=["Press releases", "Yahoo Finance earnings", "Finnhub company news"],
        trigger_patterns=[
            "cut guidance", "lowered guidance", "reduced guidance",
            "pre-announce", "preliminary results",
            "below previous outlook", "guidance revision lower",
        ],
        trigger_numeric=None,
        cadence="Event-driven (pre-announcement window 1-2 weeks before scheduled earnings)",
        stitching="Single press release sufficient. Pre-announcements catch this 1-2wk early.",
    ),

    Indicator(
        id="t4_capex_slashed",
        tier=4,
        name="CapEx slashed",
        short_label="CapEx slashed",
        description="Memory makers announce major CapEx cuts.",
        what_to_look_for="Any Big Three announces CapEx reduction of >20% or fab idling.",
        sources=["Press releases", "Yahoo Finance earnings", "Korean press / DART filings"],
        trigger_patterns=[
            "CapEx reduction", "CapEx cut", "capital spending reduced",
            "fab idling", "production cut", "capacity reduction",
        ],
        trigger_numeric={"capex_reduction_pct": {"trigger_above": 20.0}},
        cadence="Event-driven (quarterly earnings or interim press release)",
        stitching="Press release primary. Korean press / DART for SK Hynix and Samsung.",
    ),

    Indicator(
        id="t4_layoffs",
        tier=4,
        name="Layoffs / fab idling",
        short_label="Layoffs / fab idling",
        description="Memory makers announce workforce reductions or fab utilization cuts.",
        what_to_look_for="Any Big Three announces layoffs or fab idling.",
        sources=["Press releases", "Korean press (KED, Korea Herald, ChosunBiz)"],
        trigger_patterns=[
            "layoffs", "workforce reduction", "headcount reduction",
            "fab utilization cut", "fab idling",
        ],
        trigger_numeric=None,
        cadence="Event-driven",
        stitching="News + press release. Single source sufficient.",
    ),

    # ============================================================
    # v3.4.0 — Decode HBM Captive Ratio (DHCR)
    # ============================================================
    # Direction monitor (no trigger gate in v3.4.0).
    #
    # Replaces the original ROADMAP indicators 23 (Inference Efficiency Shock),
    # 24 (CXL Production Deployment) and 25 (LPDDR Datacenter Ramp) which were
    # collapsed into this single composite per Raul's direction 2026-05-21.
    #
    # The CORE QUESTION (Raul's mental model):
    #   Is enterprise demand-pull growing faster than the supply-side capacity-
    #   creation rate (efficiency × HBM-per-accelerator)?
    #
    # The mental model rejects the naive "efficiency vs context" trade-off.
    # Efficiency UNLOCKS economic context capacity. Enterprise workloads
    # expand to consume what's economically possible. The binding question
    # is whether enterprise demand grows fast enough to fill the unlocked
    # headroom. If yes → HBM stays tight (bullish SK Hynix); if no → slack
    # (bearish).
    #
    # Computed in src/analysis/dhcr.py from three YAML reference files:
    #   data/inference_arch_reference.yaml — frontier KV efficiency
    #   data/hbm_per_accelerator.yaml — HBM capacity per accelerator
    #   data/enterprise_ai_revenue.yaml — demand-pull metrics
    #
    # v3.4.0 ships as direction-only (5 levels: strongly_bullish_hbm,
    # mildly_bullish_hbm, balanced, mildly_bearish_hbm, strongly_bearish_hbm).
    # Trigger logic deferred to v3.4.1+ after 4-6 quarters of shadow data
    # validates zone-transition thresholds.
    Indicator(
        id="t1_decode_hbm_captive_ratio",
        tier=1,
        name="Decode HBM captive ratio (efficiency × demand)",
        short_label="HBM captive ratio",
        description=(
            "Direction monitor on whether enterprise AI demand growth is "
            "keeping pace with the supply-side capacity-creation rate "
            "(KV efficiency improvements × HBM-per-accelerator growth). "
            "Bullish HBM when demand-pull outruns capacity creation; "
            "bearish when efficiency / HBM packing outruns demand."
        ),
        what_to_look_for=(
            "Composite direction signal from three drivers: (1) frontier KV "
            "efficiency YoY change (open-architecture models); (2) HBM-per-"
            "accelerator YoY change (NVIDIA/AMD merchant chips); "
            "(3) enterprise AI revenue YoY weighted blend across Microsoft "
            "Azure AI, AWS, Google Cloud, Oracle, NVIDIA datacenter. "
            "Watch for direction to flip from bullish to balanced or worse — "
            "would signal demand growth decelerating below efficiency gains."
        ),
        sources=[
            "Open-model architecture disclosures (Meta, DeepSeek, Mistral, Alibaba)",
            "NVIDIA / AMD product specs (HBM per chip)",
            "Hyperscaler earnings (segment AI revenue disclosures)",
            "NVIDIA datacenter segment revenue (proxy for total AI demand)",
            "Hyperscaler CapEx disclosures",
        ],
        # v3.4.0: no trigger patterns — direction monitor only. The news
        # pipeline doesn't drive this indicator; computed YAML data does.
        trigger_patterns=[],
        trigger_numeric=None,
        cadence="Monthly (architecture YAML), quarterly (revenue YAML)",
        stitching=(
            "Computed from data/inference_arch_reference.yaml + "
            "data/hbm_per_accelerator.yaml + data/enterprise_ai_revenue.yaml "
            "via src/analysis/dhcr.py:compute_dhcr_snapshot(). Does not "
            "consume news items directly."
        ),
    ),

    # v3.4.4 — Indicator 7 (Competitor dynamics). Composite over 4 sub-
    # indicators (7A CXMT, 7B Samsung HBM4/4E, 7C Micron, 7D HBF) calibrated
    # to estimated SK Hynix EPS impact. Status mapping per sub: Not fired
    # <5% EPS impact, Partially fired 5-20%, Fired >20%. Composite logic
    # applies the Part 14 dual-threat amplifier (2+ subs Fired → Fired
    # regardless of cumulative; 1 Fired + 2+ Partially fired → Fired).
    # Same v3.4.3.1 placement pattern as DHCR — filtered out of section 3
    # tier table; renders as section 4 deep-dive card via dedicated builder.
    Indicator(
        id="t1_competitor_dynamics",
        tier=1,
        name="Competitor dynamics (CXMT, Samsung HBM4, Micron, HBF)",
        short_label="Competitor dynamics",
        description=(
            "Structural overlay on SK Hynix premium thesis: pressure from "
            "Chinese commodity DRAM (CXMT), Samsung HBM4/HBM4E share gains "
            "at Nvidia, Micron HBM catch-up, and HBF (High Bandwidth Flash) "
            "substitution risk. Status calibrated to estimated EPS impact "
            "via price × volume mechanics, not raw market share. The Part "
            "14 dual-threat scenario (CXMT + Samsung simultaneously) is "
            "the worst-case overlay on cycle peak (-30 to -45% combined; "
            "-60 to -75% with peak-window coincidence)."
        ),
        what_to_look_for=(
            "Per-sub-indicator EPS-impact thresholds in data/competitor_thresholds.yaml. "
            "Headline triggers: 7A CXMT DDR5 mainstream Tier-1 OEM win; "
            "7B SK Hynix Nvidia HBM4 share <50% OR HBM gross margin <60%; "
            "7C Micron wins primary Vera Rubin SKU at SK Hynix's expense; "
            "7D 2+ hyperscaler HBF design wins displacing hot-tier HBM AND "
            "SK Hynix behind on HBF roadmap."
        ),
        sources=[
            "Counterpoint quarterly DRAM/HBM share reports",
            "TrendForce HBM forecast reports",
            "Korean broker reports (Mirae, KB Securities)",
            "Nvidia / AMD earnings transcripts",
            "SK Hynix / Samsung / Micron earnings",
            "DigiTimes Asia (OEM win leaks)",
            "JEDEC press releases (HBF standardization)",
            "SemiAnalysis Free RSS",
        ],
        trigger_patterns=[],  # composite indicator; sub-indicators carry their own patterns
        trigger_numeric=None,
        cadence=(
            "Daily Layer 1 extraction → src/analysis/competitor_aggregator.py "
            "→ state/competitor_state.json (auto-updated before each fast-tier "
            "snapshot computation)"
        ),
        stitching=(
            "data/competitor_thresholds.yaml holds the thresholds + extraction "
            "schema (stable, never auto-modified). state/competitor_state.json "
            "holds the current measured values (sole source of truth, written "
            "only by the aggregator). compute_competitor_snapshot() in "
            "src/analysis/competitor_scoring.py reads both. Sub-indicator "
            "triggers (i6a_cxmt_commodity, i6b_samsung_hbm4, i6c_micron_hbm4, "
            "i7d_hbf) in COMPANY_SPECIFIC_INDICATORS below drive the Layer 1 "
            "news extraction the aggregator consumes."
        ),
    ),

    # v3.4.4 — Indicator 8 (US ADR / SPO event tracking). Composite over 4
    # sub-indicators (8A F-1 filing watch, 8B roadshow demand, 8C pricing-day
    # execution, 8D insider sales in offering window). Per Raul 2026-05-22:
    # canceled F-1 = Fired; insider threshold $10M; Fired persists through
    # Phase 4 (no 90-day reversion). Peak-coincidence escalation rule per
    # Part 11 (Hynix 1995 listing / 1996 crash pattern). Same v3.4.3.1
    # placement pattern as DHCR — filtered out of section 3 tier table.
    Indicator(
        id="t1_us_adr_event",
        tier=1,
        name="US ADR / SPO event tracking",
        short_label="US ADR / SPO",
        description=(
            "Event-driven indicator: SK Hynix US ADR launch via new share "
            "issuance (treasury share path closed Feb 9 2026). Expected "
            "June-July 2026; ~17.4M new shares (~2.4% dilution); estimated "
            "$9.6-14B raise. Two-part read: (1) mechanical drag -4 to -9% "
            "cumulative for established foreign issuers per academic studies "
            "(Doidge-Karolyi-Stulz 2004, Sarkissian-Schill 2009); (2) peak-"
            "coincidence signal per Part 11 — memory makers cross-listing at "
            "peak is a recurring top-tick pattern."
        ),
        what_to_look_for=(
            "F-1 filing on SEC EDGAR (beats Korean press by 1-3 days); "
            "underwriter syndicate quality at filing; roadshow demand "
            "language (oversubscribed Nx vs soft); 424B pricing discount "
            "(<3% strong / 3-5% normal / >5% weak); insider sales >$10M "
            "in the offering window; ADR completion concurrent with Tier 3 "
            "signals firing = peak-coincidence escalation to Fired."
        ),
        sources=[
            "SEC EDGAR F-1 atom feed (already in WATCH_GOOGLE_QUERIES)",
            "SEC 424B prospectus filings",
            "SEC Form 4 (insider transactions)",
            "Korean DART (SK Hynix C-suite KRX-side disclosures)",
            "Reuters / Bloomberg ADR coverage",
            "ChosunBiz / Korea Times (Korean syndicate-side leaks)",
        ],
        trigger_patterns=[],  # event-driven; sub-trigger patterns in COMPANY_SPECIFIC_INDICATORS
        trigger_numeric=None,
        cadence=(
            "Daily SEC EDGAR atom feed (F-1 detection) + Layer 1 extraction "
            "→ src/analysis/adr_aggregator.py → state/adr_state.json"
        ),
        stitching=(
            "data/adr_event_tracking.yaml holds the thresholds + lifecycle "
            "phase enum + calendar template + academic refs (stable). "
            "state/adr_state.json holds the current lifecycle phase + sub "
            "statuses (sole source of truth, written only by the aggregator). "
            "compute_adr_snapshot() in src/analysis/adr_event_tracking.py "
            "reads both. Pre-F-1 state renders all 4 subs as 'Pre-event' "
            "with the calendar window prominent."
        ),
    ),
]


# ============================================================
# Company-specific indicators — SK Hynix (Indicators 7 and 8)
# ============================================================
# v3.4.4 user-facing renumbering (per Raul 2026-05-22):
#   Indicator 6 = DHCR (already in INDICATORS list as t1_decode_hbm_captive_ratio)
#   Indicator 7 = Competitor dynamics (this dict, sub-keys i6a / i6b / i6c + new i7d_hbf)
#   Indicator 8 = US ADR / SPO event (this dict, sub-key i7_adr_launch)
#
# The INTERNAL dict keys (i6a_cxmt_commodity, i6b_samsung_hbm4, i6c_micron_hbm4,
# i7_adr_launch) are LEGACY naming — preserved here so signal_reasoner.py and
# digester.py don't need cross-cutting changes. User-facing numbering is what's
# in the section 4 card titles. Section 4 deep-dive cards for Indicator 7
# and Indicator 8 read from data/competitor_thresholds.yaml and
# data/adr_event_tracking.yaml respectively, computed by
# src/analysis/competitor_scoring.py and src/analysis/adr_event_tracking.py.
#
# These are outside the universal tier framework because they're SK Hynix-
# specific structural overlays, not generalizable cycle-phase signals — same
# rationale as DHCR (Indicator 6).

COMPANY_SPECIFIC_INDICATORS = {
    # Indicator 6 — Competitor Dynamics (3 sub-threats)
    "i6a_cxmt_commodity": {
        "name": "CXMT commodity DRAM threat",
        "trigger_patterns": [
            "CXMT DDR5", "ChangXin Memory DDR5",
            "Dell CXMT", "HP CXMT", "Acer CXMT", "Asus CXMT", "Lenovo CXMT",
            "CXMT qualified", "Chinese DRAM adoption", "Chinese DDR5 supply",
        ],
        "trigger_numeric": {
            # DDR5 16Gb price scenarios → stock impact:
            "cxmt_ddr5_16gb_price_usd": {"$35": -7, "$30": -25, "$25": -42, "$20": -62},
        },
        "stitching": "Korean press + Mirae PDFs (deep CXMT analysis) + DigiTimes. @KevinXuChina manual.",
    },
    "i6b_samsung_hbm4": {
        "name": "Samsung HBM4 share gain at Nvidia",
        "trigger_patterns": [
            "Samsung HBM4 Vera Rubin", "Samsung HBM4 Nvidia",
            "Samsung HBM4 qualified", "Samsung HBM4 design win",
            "SK Hynix HBM4 11Gbps optimizing", "SK Hynix HBM4 delayed",
            "HBM4 yield", "HBM4 share",
        ],
        "trigger_numeric": {
            # Samsung share at Nvidia → stock impact:
            "samsung_hbm4_nvidia_share_pct": {"40": -6, "50": -15, "70": -30},
            "skhx_hbm4_yield_pct": {"trigger_below": 65},
        },
        "stitching": "Mirae Asset PDFs (Korean brokers do HBM yield analysis) + Yahoo Finance Nvidia/Samsung calls.",
    },
    "i6c_micron_hbm4": {
        "name": "Micron HBM4 catch-up",
        "trigger_patterns": [
            "Micron HBM4 design win", "Micron HBM4 qualified",
            "Micron HBM4 Nvidia", "Micron HBM4 AMD MI400",
            "Micron HBM4 hyperscaler",
        ],
        "trigger_numeric": None,
        "stitching": "Micron earnings (Yahoo Finance) + Nvidia/AMD earnings + news scraping.",
    },
    # 7D — HBF (High Bandwidth Flash) leadership — NEW v3.4.4
    # Two-sided indicator (substitution risk + Solidigm moat-extension).
    # Fire condition specifically requires HOT-tier HBM displacement, not
    # cold-tier HBF additions (which are likely demand-unlocking per DHCR
    # mental model). See data/competitor_thresholds.yaml sub_7d_hbf for
    # the full sub-state definition and trigger thresholds.
    "i7d_hbf": {
        "name": "HBF (High Bandwidth Flash) leadership",
        "trigger_patterns": [
            "HBF High Bandwidth Flash", "HBF SanDisk", "HBF Kioxia",
            "HBF JEDEC", "HBF design win", "HBF hyperscaler",
            "HBF Nvidia", "HBF accelerator", "SK Hynix HBF",
            "Solidigm HBF", "HBF roadmap",
        ],
        "trigger_numeric": None,
        "stitching": "JEDEC press + SemiAnalysis + Tom's Hardware + SanDisk/Kioxia IR + SK Hynix earnings.",
    },
    # Indicator 8 — US ADR / SPO launch tracking
    # (Internal dict key remains "i7_adr_launch" for back-compat with
    # signal_reasoner.py + digester.py. User-facing label is "Indicator 8".)
    "i7_adr_launch": {
        "name": "SK Hynix US ADR / SPO launch",
        "trigger_patterns": [
            "SK Hynix F-1", "SK Hynix ADR", "SK Hynix US listing",
            "SK Hynix roadshow", "SK Hynix 424B", "SK Hynix pricing day",
        ],
        "events_to_track": [
            "F-1 public release (imminent)",
            "F-1/A amendments (May-Jul 2026, 3-5 typically)",
            "US roadshow (mid-May to mid-Jun 2026)",
            "424B pricing day (Jun/Jul 2026) — HIGHEST mechanical impact event",
            "First trading day (1-2 days after 424B)",
            "Mandatory ADR disclosure (~Sep 24, 2026 — 6-month F-1 window)",
        ],
        "stitching": "SEC EDGAR atom feeds (F-1, F-1/A, 424, 6-K, 20-F) + Korean press + Reuters.",
    },
}

TOP_5_INDICATORS = [
    "t1_spot_decline",
    "t2_capex_parabolic",   # Hyperscaler CapEx specifically
    "t2_eps_revisions",
    "t3_supplier_inventory_rebuild",
    "t3_relative_strength",
]


# ============================================================
# Indicator 1 — Memory price baselines (for explicit deviation tracking)
# ============================================================
# When TrendForce news or Korean broker PDFs reference a memory price,
# Layer 2 computes deviation from these baselines to anchor fuzzy
# "softening" / "declining" language to concrete numbers.
#
# Update these monthly as the cycle progresses. Source: TrendForce
# May 2026 press releases + Mirae Asset May 2026 SK Hynix note.
# Last updated: May 10, 2026.

MEMORY_PRICE_BASELINES = {
    # Spot prices (per chip USD)
    "ddr5_16gb_4800_5600_spot_usd":      {"value": 39.0,  "updated": "2026-05-10", "source": "TrendForce"},
    "ddr5_8gb_4800_5600_spot_usd":       {"value": 19.0,  "updated": "2026-05-10", "source": "TrendForce"},
    "ddr4_16gb_3200_spot_usd":           {"value": 61.0,  "updated": "2026-05-10", "source": "TrendForce — anomalously high due to EOL scarcity"},
    # Module prices (per module USD)
    "ddr5_rdimm_32gb_module_usd":        {"value": 970.0, "updated": "2026-05-10", "source": "TrendForce — server-grade"},
    "ddr5_udimm_16gb_module_usd":        {"value": 75.0,  "updated": "2026-05-10", "source": "Tom's Hardware retail (range 70-80)"},
    # Lead times (weeks)
    "lpddr5x_lead_time_weeks_low":       {"value": 26,    "updated": "2026-05-10", "source": "TrendForce — Q1 2026 (range 26-39)"},
    "lpddr5x_lead_time_weeks_high":      {"value": 39,    "updated": "2026-05-10", "source": "TrendForce — Q1 2026 (range 26-39)"},
    # Contract prices (relative — Q-on-Q)
    "ddr5_server_contract_q2_2026_qoq_pct": {"value": 60.5, "updated": "2026-05-10", "source": "TrendForce — range 58-63%"},
    "ddr5_server_contract_q1_2026_qoq_pct": {"value": 92.5, "updated": "2026-05-10", "source": "TrendForce — range 90-95%"},
    # Inventory weeks (memory makers)
    "supplier_inventory_weeks":          {"value": 2.5,   "updated": "2026-05-10", "source": "TrendForce — range 2-3 weeks at Big Three"},
    "oem_inventory_weeks":               {"value": 10.0,  "updated": "2026-05-10", "source": "TrendForce Q3 2025 — ~10 weeks at buyers"},
}


# ============================================================
# Indicator 4 — Inventory weeks running trajectory baselines
# ============================================================
# Used to compute trajectory across quarters. Each entry is the
# memory maker's stated inventory weeks at quarter end. Layer 2
# appends to this on each new earnings disclosure and computes
# direction-of-travel ("rising from 2.3 → 2.5 → 2.8 = approaching trigger").

INVENTORY_WEEKS_TRAJECTORY_SEED = {
    # ticker → list of (quarter_iso, inventory_weeks, source)
    "000660.KS": [  # SK Hynix
        ("2025-Q1", 2.0, "Q1 2025 earnings"),
        ("2025-Q2", 2.2, "Q2 2025 earnings"),
        ("2025-Q3", 2.1, "Q3 2025 earnings"),
        ("2025-Q4", 2.3, "Q4 2025 earnings"),
        ("2026-Q1", 2.3, "Q1 2026 earnings"),
    ],
    "MU": [
        ("FY25-Q1", 2.4, "Micron Q1 FY25 earnings"),
        ("FY25-Q2", 2.5, "Micron Q2 FY25 earnings"),
        ("FY25-Q3", 2.6, "Micron Q3 FY25 earnings"),
        ("FY25-Q4", 2.7, "Micron Q4 FY25 earnings"),
        ("FY26-Q1", 2.8, "Micron Q1 FY26 earnings"),
        ("FY26-Q2", 3.0, "Micron Q2 FY26 earnings"),
    ],
}

INVENTORY_TRIGGER_THRESHOLD_WEEKS = 4.0  # crossing this = t3_supplier_inventory_rebuild fires


# ============================================================
# Indicator 2 — Hyperscaler CapEx baselines (self-updating)
# ============================================================
# These seed values are the consensus 2026 CapEx estimates. Layer 1
# digester of each hyperscaler earnings call writes updated values to
# state/hyperscaler_capex_baselines.json. Layer 2 reads from state
# file first, falls back to these seeds if state file absent.
#
# Trigger: any 2027 guidance number < the 2026 baseline = BEAR
# CATALYST. Even one hyperscaler blinking is enough.

HYPERSCALER_CAPEX_2026_BASELINES_USD_B = {
    "MSFT":  190.0,  # current 2026 guidance (May 2026)
    "GOOGL": 185.0,
    "META":  105.0,
    "AMZN":  140.0,
    "ORCL":   65.0,
    # Combined: ~$725B aggregate, +77% YoY from 2025
}


# ============================================================
# Indicator 3 — Broker authority weighting + EPS revisions scoring
# ============================================================
# The canonical Tier 2 signal (`t2_eps_revisions`) requires more than
# a count of revisions. A single -20% Mizuho cut with FY27 EPS revision
# is a stronger signal than 5 Tier-C brokers cutting -2% each.
#
# We weight every broker action by:
#   1. Broker authority tier (A=3x, B=2x, C=1x)
#   2. Action magnitude (PT %, EPS %, rating change)
#   3. Combination bonus when rating + PT + EPS all move together
#
# Final score per note = direction × (sum of component points) ×
#                        authority × (1 + combo_bonus)
#
# Rolling 30-day sum < -15 = canonical Tier 2 signal FIRED.

# Broker authority tiers — opinionated; adjustable after 4-6 weeks of observation
BROKER_AUTHORITY_TIERS: dict[str, str] = {
    # ----- Tier A (3x) — global benchmark-setters -----
    "Mirae Asset Securities": "A",
    "미래에셋증권": "A",
    "Goldman Sachs": "A",
    "골드만삭스": "A",
    "Morgan Stanley": "A",
    "모건스탠리": "A",
    "JPMorgan": "A",
    "JP Morgan": "A",
    "JP모건": "A",
    "Mizuho": "A",
    "미즈호": "A",

    # ----- Tier B (2x) — significant influence -----
    "Samsung Securities": "B",
    "삼성증권": "B",
    "NH Investment & Securities": "B",
    "NH투자증권": "B",
    "Korea Investment & Securities": "B",
    "한국투자증권": "B",
    "Bernstein": "B",
    "AB Bernstein": "B",
    "Susquehanna": "B",
    "BofA Securities": "B",
    "Bank of America": "B",
    "BofA": "B",
    "Citi": "B",
    "Citigroup": "B",
    "UBS": "B",
    "HSBC": "B",
    "Macquarie": "B",
    "Credit Suisse": "B",
    "Deutsche Bank": "B",

    # ----- Tier C (1x) — baseline (everyone else, including all other Korean Tier 2 brokers via Naver) -----
    # Don't need to list explicitly — falls through to default Tier C in code
}

TIER_MULTIPLIERS = {"A": 3.0, "B": 2.0, "C": 1.0}


# Magnitude brackets for target price / EPS revisions (signed percentages map to magnitude points)
def _magnitude_points(pct_change: float) -> float:
    """Map a percentage change to magnitude points (0.5 / 1.0 / 1.5 / 2.0)."""
    abs_pct = abs(pct_change)
    if abs_pct < 5:
        return 0.5
    elif abs_pct < 10:
        return 1.0
    elif abs_pct < 20:
        return 1.5
    else:
        return 2.0


# Rating change point values
RATING_CHANGE_POINTS = {
    "initiate_buy":      1.5,
    "initiate_sell":    -1.5,
    "initiate_hold":     0.0,
    "upgrade":           1.0,   # one-notch upgrade (e.g. Hold → Buy)
    "downgrade":        -1.0,
    # Two-notch moves are emphatically more bearish/bullish than single-notch.
    # E.g. Morgan Stanley Overweight → Underweight on SK Hynix on Sept 15, 2024 —
    # skipping Equal-weight entirely. Stock dropped 11% intraday on first trading day.
    # Without this distinction, a two-notch action would score the same as a regular
    # downgrade, understating its severity.
    "double_upgrade":    2.0,
    "double_downgrade": -2.0,
    "maintained":        0.0,
}


# Trigger thresholds for the weighted scoring engine.
#
# After validation against historic 2021 + 2024 Morgan Stanley downgrades (see
# docs/INDICATOR_3_EXAMPLES.md), we fire the canonical Tier 2 signal off the
# BEARISH-ONLY rolling sum, not the net. Rationale: a fresh -20 bearish cluster
# in May 11-15 shouldn't be masked by an older +18 bullish cluster from April.
# Separating bearish from bullish prevents that hiding problem while still
# letting Layer 2 surface "conflicting signals" via the bullish-vs-bearish gap.
EPS_SCORING_THRESHOLDS = {
    "single_action_flag_score":          5.0,    # |score| > this = flag in brief
    "tier_a_single_flag_score":          3.0,    # |Tier A score| > this = flag
    "bearish_7d_early_warning":        -10.0,    # bearish 7d-sum < this = EARLY WARNING
    "bearish_30d_fire_threshold":      -15.0,    # bearish 30d-sum < this = t2_eps_revisions FIRED
    "bullish_30d_confirm_threshold":    15.0,    # bullish 30d-sum > this = BULLISH CONFIRM
    # Legacy keys retained for backward compat with prior state files:
    "rolling_7d_early_warning":        -10.0,
    "rolling_30d_fire_threshold":      -15.0,
    "rolling_30d_bull_threshold":       15.0,
}

# Tickers we compute t2_eps_revisions for
EPS_REVISIONS_TICKERS = ["000660.KS", "MU", "005930.KS"]  # SK Hynix, Micron, Samsung


# ============================================================
# News sources to scrape
# ============================================================

# RSS feeds (preferred when available)
#
# v3.38.3 RSS audit (2026-05-17 14:31 UTC run):
#   13 of 28 feeds returned 404/DNS failures with no clear public RSS alternative.
#   Many AI-lab/Korean-press sites have retired RSS in favor of email newsletters
#   or never had stable feeds. Pruned to stop generating FAIL noise in the
#   diagnostics table. The same signals are still picked up by:
#     - Google News queries (KV cache compression, inference efficiency, DDR5
#       spot price, TrendForce DRAM contract, broker-name combos, etc.)
#     - The TrendForce news scraper
#     - The ChosunBiz scraper
#   Pruned this release (will revisit in v3.39 — see TODO at bottom of file):
#     ✗ Yonhap English Business     (404)
#     ✗ Anthropic News              (404 — Anthropic has no official RSS)
#     ✗ Meta AI Blog                (404 — no public RSS)
#     ✗ SEMI press releases         (404)
#     ✗ Reuters Technology          (DNS — feed retired; Reuters paywalled news in 2024)
#     ✗ Seeking Alpha Semiconductors (404)
#     ✗ The Elec English            (404 — wrong path; correct one TBD)
#     ✗ Nikkei Asia Companies       (404)
#     ✗ Pulse by Maeil Business     (404 — but ChosunBiz scraper covers Korean press)
#     ✗ vLLM Blog                   (404 — redirect chain broken)
#     ✗ SGLang / LMSYS Blog         (404)
#     ✗ DeepSeek API News           (404)
#     ✗ Artificial Analysis         (404)
#   Kept after 403 → 200 (fixed via UA change in news.py HEADERS):
#     ✓ Damnang Memory Substack     (Substack/Cloudflare blocked old "compatible;" UA)
#     ✓ Investing.com MU/NVDA/AMAT/ASML (same UA issue)
RSS_FEEDS = {
    # Korean press
    "Korea Times Business": "https://www.koreatimes.co.kr/www/rss/biz.xml",
    # NOTE: Yonhap, Pulse still 404 on their RSS endpoints. Korea Herald
    # was repaired in v3.39.7.5 (see below). The Korean press signal also
    # flows via Google News RSS targeted queries (see GOOGLE_NEWS_QUERIES).
    # via ChosunBiz scraper, KED Global (when not 502), and Korean-name Google News.
    "KED Global (Korea Economic Daily English)": "https://www.kedglobal.com/rss/all.xml",
    # v3.39.7.5 — Korea Herald RSS URL repaired. Old URL
    # `koreaherald.com/rss/020000000000.xml` was the legacy numeric-key
    # format and 404'd. The current site uses path-keyed RSS:
    # `/rss/kh_Business` (Business section). Probed live 2026-05-20.
    # Also added /rss/kh_National which carries labor news (Samsung
    # strike resolution May 20 2026 was published here, not Business
    # — Korean labor disputes are National-section content).
    "Korea Herald Business": "https://www.koreaherald.com/rss/kh_Business",
    "Korea Herald National": "https://www.koreaherald.com/rss/kh_National",
    # Memory / semi industry
    # v3.39.3 — Dropped "TrendForce": "https://www.trendforce.com/news/feed".
    # That URL returns no XML — TrendForce restructured their news section
    # and the /feed path is no longer published. Replacement: DRAMeXchange
    # RSS, which is TrendForce's sister property on the data side and DOES
    # publish a well-formed RSS feed with TrendForce-authored weekly research
    # commentary (the behavioral-signal language Tier 1 needs: "buyers
    # cautious", "price cuts not stimulating demand", etc.). Items use
    # relative <link> paths (/WeeklyResearch/Post/...) — _safe_parse_rss_feed
    # passes them through; the relative URL still resolves under the
    # dramexchange.com origin in the email's links.
    "DRAMeXchange Weekly Research": "https://www.dramexchange.com/rss.xml",
    "Tom's Hardware RAM": "https://www.tomshardware.com/feeds/all",
    # v3.39.3.1: AnandTech RSS removed. Feed had been throwing
    # `feed parse error: not well-formed (invalid token)` in every run since
    # before v3.39.1; site publishes ~5 articles/month (a fraction of its
    # 2010s heyday), so the maintenance cost of fixing the malformed XML
    # doesn't pay back. Tom's Hardware + DRAMeXchange + general tech press
    # via Google News cover the same beat.
    # AI labs (only ones with confirmed-stable RSS — others pruned, covered by Google News)
    "Google Research Blog": "https://research.google/blog/rss/",
    "OpenAI Blog": "https://openai.com/blog/rss.xml",
    # US analyst rationale
    "Benzinga Analyst Ratings": "https://www.benzinga.com/feed",
    # v3.38.4: Investing.com MU/NVDA/AMAT/ASML DROPPED. The v3.38.3 UA change
    # (plain Chrome UA, dropped "compatible;" pattern) fixed ~12 other feeds but
    # Investing.com x4 + Damnang Substack stayed 403. Cloudflare's bot detection
    # uses TLS fingerprint (JA3/JA4) and HTTP/2 fingerprint, not just UA string.
    # Python requests has a recognizable TLS fingerprint regardless of UA.
    # Proper fix needs curl-cffi (mimics Chrome TLS handshake) — deferred to v3.39.
    # For now, dropping these. Signal coverage: Investing.com ticker news flows
    # via Yahoo Finance (deep tier) and Benzinga; broker actions via Naver and
    # broker-name Google News queries.
    "CNBC Technology": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "Investing.com Asia Equities": "https://www.investing.com/rss/news_301.rss",
    # v3.38.4: Damnang Substack DROPPED for same reason as Investing.com x4 —
    # Cloudflare TLS fingerprint check, UA-only fix insufficient. Architectural
    # signal coverage retained via Google News queries on KV cache compression,
    # CXMT DDR5, HBM AI bottleneck, inference efficiency breakthrough.
    # HuggingFace publishes a working RSS for model + research drops
    "HuggingFace Blog": "https://huggingface.co/blog/feed.xml",
    # CXL / memory disaggregation canary — Astera Labs is the pure-play
    "Astera Labs Newsroom": "https://www.asteralabs.com/category/news/feed/",
    # SemiAnalysis free posts (the rest is paywalled but headlines + intros are useful)
    "SemiAnalysis Free": "https://semianalysis.com/feed/",

    # v3.4.6 — Frontier-model lab blogs. Feed the DHCR arch aggregator's
    # Tier-A source set so newly-released frontier inference models get
    # picked up at the lab announcement (highest authority on architecture
    # specs). Several of these failed in v3.38.3's prune as 404/DNS; URLs
    # below are documented current endpoints — production may show some
    # still 403/404 (Cloudflare TLS fingerprinting on Mistral/DeepSeek a
    # known risk; if so they fall through and Google News covers the
    # release indirectly via Tier-B/C).
    "Meta AI Blog": "https://ai.meta.com/blog/rss/",
    "DeepSeek API News": "https://api-docs.deepseek.com/news/rss.xml",
    "Mistral News": "https://mistral.ai/news/feed.xml",
    "Qwen Blog": "https://qwenlm.github.io/feed.xml",
    "Anthropic News": "https://www.anthropic.com/news/rss.xml",
    "Google DeepMind Blog": "https://deepmind.google/discover/blog/rss.xml",
    # HuggingFace Papers (community-curated daily) — distinct from
    # "HuggingFace Blog" above which is the official HF announcements feed.
    # Papers feed surfaces academic releases including major foundation
    # model papers within 24h of arxiv posting.
    "HuggingFace Papers": "https://huggingface.co/papers/feed.xml",
    # arxiv — direct ML/NLP paper RSS. Volume is ~100-200/day in cs.LG
    # alone, so news.py applies arxiv_frontier_paper_filter() before items
    # reach the relevance scorer. Filter retains items whose title matches
    # known frontier-model release patterns (vendor names, parameter-count
    # patterns, architecture-family keywords); 95%+ of routine papers
    # dropped at fetch time. See src/data/news.py:arxiv_frontier_paper_filter
    "arxiv cs.LG (filtered)": "https://rss.arxiv.org/rss/cs.LG",
    "arxiv cs.CL (filtered)": "https://rss.arxiv.org/rss/cs.CL",
}
# TODO v3.39 — feed URL hunt + Cloudflare bypass: find working replacements for
# sources pruned in v3.38.3 / v3.38.4. Candidates:
#   - vLLM blog: real RSS may live at a different path under blog.vllm.ai/
#   - The Elec: switched RSS index location, needs investigation
#   - Damnang Substack + Investing.com x4: try curl-cffi (Chrome TLS fingerprint)
#     to bypass Cloudflare. Damnang in particular is the architectural-signal
#     source Raul flagged as critical; worth the engineering investment.

# URLs to scrape directly
SCRAPE_TARGETS = {
    "ChosunBiz English": "https://biz.chosun.com/en/",
    "Pulse News (Maeil Business)": "https://pulse.mk.co.kr/list.php?ctcd=600002&sc=30000020",
    # v3.39.3 — URL fixed. Old /news/category/dram returns 200 with no matching
    # <article> selectors → 0 items silently. Correct path is
    # /news/category/semiconductors/dram/ (with /semiconductors/ segment and
    # trailing slash). news.py scrape_trendforce() also hardcoded the same URL
    # and has been updated to match.
    "TrendForce News": "https://www.trendforce.com/news/category/semiconductors/dram/",
    # v3.39.3 — TrendForce public DRAM spot price table. Scraped by
    # scrape_trendforce_prices() in news.py (separate code path from the
    # news category scraper above — uses table parsing + freshness guard
    # rather than article-link extraction).
    "TrendForce DRAM Spot (price table)": "https://www.trendforce.com/price/dram/dram_spot",
}

# ============================================================
# Earnings transcript sources (v3.31 — replaces gated Quartr API)
# ============================================================
# Quartr discontinued its free developer tier (now enterprise-only, "contact us"
# pricing). Transcript coverage is rebuilt from free sources:
#   1. Direct IR-site scrapers for the 3 core memory makers
#   2. Motley Fool transcripts for US-listed names (English calls)
#   3. EDGAR 8-K earnings exhibits (already in EDGAR_FEEDS — digester treats
#      earnings-related 8-K exhibits as transcript-equivalent input)
#
# Every transcript source returns a three-state availability flag per event:
#   "full_transcript" — got the actual call text, commentary intact
#   "materials_only"  — got earnings release / slides but NO call commentary
#   "nothing"         — source had nothing for this event
# The digester and email surface this explicitly so absence of commentary is
# never confused with "nothing notable was said".

# Direct IR-site scrapers — the 3 core memory makers. These are PERMANENT
# (not a pre-ADR stopgap): even after the SK Hynix US ADR lists, the earnings
# CALL is still Korean-hosted on the company IR site. The ADR gives a US ticker
# for price/analyst data via Finnhub — it does NOT create transcript coverage.
#
# v3.32 — `transcript_capable` makes the SK Hynix/Samsung reality explicit:
# their IR sites publish slides + webcast only, NOT a text transcript. When
# False, the IR scraper is capped at `materials_only` regardless of how much
# text it finds — it must never masquerade as a full-transcript source. The
# actual full transcript for these names comes from Yahoo Finance (see
# YAHOO_TRANSCRIPT_TICKERS below), which carries the Korean+English call text.
IR_SOURCES = {
    "SK Hynix": {
        "ticker": "000660.KS",
        "ir_url": "https://news.skhynix.com/feed/rss/",
        "transcript_reliability": "low",   # Korean call; English transcript often days late or webcast-only
        "transcript_capable": False,       # v3.32 — IR site is materials/webcast only; Yahoo Finance is the transcript source
        "lang": "ko/en",
        # v3.36 — switched from skhynix.com/ir/UI-FR-IR15 (wrong page) and
        # skhynix.com/ir/UI-FR-IR06 (correct earnings landing but JS shell —
        # PDF links resolve to /webundefined placeholders only client-side)
        # to the news.skhynix.com WordPress newsroom RSS feed. WordPress
        # feeds are fully server-rendered and standard across the platform;
        # earnings posts use the URL pattern q{N}-{YYYY}-business-results/
        # (e.g. q1-2026-business-results/ for the Apr 23, 2026 release).
        # The RSS path in _scrape_ir_site picks the newest earnings post and
        # extracts the full release text (revenue, op profit, op margin,
        # forward commentary, CapEx). Capped at materials_only by
        # transcript_capable=False; Yahoo HY9H.MU is the transcript source.
    },
    "Samsung Electronics": {
        "ticker": "005930.KS",
        "ir_url": "https://www.samsung.com/global/ir/",
        "transcript_reliability": "low",   # same as SK Hynix — flag aggressively
        "transcript_capable": False,       # v3.32 — IR site is materials/webcast only; Yahoo Finance is the transcript source
        "lang": "ko/en",
    },
    "Micron": {
        "ticker": "MU",
        "ir_url": "https://investors.micron.com/events-and-presentations",
        "transcript_reliability": "high",  # US, English, prompt posting
        "transcript_capable": True,        # v3.32 — Micron IR posts the full call promptly
        "lang": "en",
    },
}

# Yahoo Finance earnings-call transcripts — the FALLBACK transcript source
# (v3.33+). Carries full call text (incl. Korean+English Q&A) for the names
# Motley Fool can't cover (SK Hynix, Samsung) and as a belt-and-suspenders
# fallback for everything else. Latest call is free; historical is gated.
#
# v3.34 EMPIRICAL CORRECTION: Yahoo's transcript pages exist for SK Hynix /
# Samsung, but the linking is asymmetric across ticker variants. The KRX
# primary listings (000660.KS, 005930.KS) hit Yahoo earnings-calls index
# pages that do NOT statically link the transcripts. The European cross-
# listings DO. Raul confirmed 2026-05-16: SK Hynix Q1 FY2026 transcript is
# reachable at /quote/HY9H.MU/earnings/HY9H.MU-Q1-2026-earnings_call-547968.html
# while /quote/000660.KS/earnings-calls/ shows zero transcript anchors.
# So we use the European cross-listing tickers as the canonical Yahoo ticker
# for the two Korean memory makers, even though the underlying call (and
# transcript content) is the same Korean call. The Yahoo "syndication" note
# from v3.32 still applies — Yahoo serves the same transcript content under
# every variant — but the UI/HTML rendering only links it from certain ones.
#
# IMPORTANT: Even though Yahoo syndicates each transcript across many ticker
# variants (SK Hynix as HY9H.F/.MU/.DU/.SG; Samsung as SSU.F/SMSN.L/SSNLF;
# etc.), pick ONE canonical ticker per company — duplicate scrapes of the
# same transcript content are wasted bandwidth and risk double-counting.
YAHOO_TRANSCRIPT_TICKERS = {
    # Core memory makers — Yahoo is the ONLY working full-transcript source
    # for the two Korean names. Canonical ticker MUST be a cross-listing
    # variant (not KRX) because Yahoo's UI only links transcripts from those.
    "SK Hynix":             "HY9H.MU",   # Munich listing — empirically confirmed has transcript link 2026-05-16
    "Samsung Electronics":  "SSU.F",     # Frankfurt listing — likely same pattern; will confirm with v3.34 diag logs
    "Micron":               "MU",
    # Wider basket — transcript-fed indicators (HBM4 / CapEx / WFE / OEM inventory)
    "Nvidia":               "NVDA",
    "Broadcom":             "AVGO",
    "TSMC":                 "TSM",
    "Applied Materials":    "AMAT",
    "ASML":                 "ASML",
    "Microsoft":            "MSFT",
    "Alphabet":             "GOOGL",
    "Meta":                 "META",
    "Amazon":               "AMZN",
    "Oracle":               "ORCL",
    "Dell":                 "DELL",
    "HPE":                  "HPE",
    # v3.39.7.8 — Chip designer expansion. AMD MI400 + Intel Gaudi
    # consume HBM at scale; Cerebras WSE-3 uses on-chip SRAM (NOT HBM)
    # making Cerebras an HBM-substitution-risk signal — earnings calls
    # explicitly relevant to the v3.4 captive-ratio indicator work.
    # Cerebras IPO'd 2026-05-14 so first earnings call ~3 months out;
    # entry added now so the cascade catches it when it appears.
    "AMD":                  "AMD",
    "Intel":                "INTC",
    "Cerebras":             "CBRS",
}

# Motley Fool transcript coverage — US-listed names with English earnings calls.
# v3.33: maps display name -> {exchange, ticker}. Exchange is needed because
# the scraper now hits the per-ticker quote page at
# https://www.fool.com/quote/{exchange}/{ticker}/  (persistent, lists all
# transcripts for that company) instead of the global recent-transcripts
# index (which only shows the last ~20 and rolls off in 24-48h).
MOTLEY_FOOL_TICKERS = {
    "Micron":            {"exchange": "nasdaq", "ticker": "MU"},      # memory maker — core (also in IR_SOURCES as cross-check)
    "Nvidia":            {"exchange": "nasdaq", "ticker": "NVDA"},    # HBM demand driver
    "Broadcom":          {"exchange": "nasdaq", "ticker": "AVGO"},    # custom silicon / networking
    "TSMC":              {"exchange": "nyse",   "ticker": "TSM"},     # CoWoS packaging bottleneck (NYSE-listed ADR)
    "Applied Materials": {"exchange": "nasdaq", "ticker": "AMAT"},    # equipment — Tier 3 confirm
    "Microsoft":         {"exchange": "nasdaq", "ticker": "MSFT"},    # hyperscaler CapEx
    "Alphabet":          {"exchange": "nasdaq", "ticker": "GOOGL"},   # hyperscaler CapEx + TPU
    "Meta":              {"exchange": "nasdaq", "ticker": "META"},    # hyperscaler CapEx + MTIA
    "Amazon":            {"exchange": "nasdaq", "ticker": "AMZN"},    # hyperscaler CapEx + Trainium
    "Oracle":            {"exchange": "nyse",   "ticker": "ORCL"},    # hyperscaler CapEx
    "Dell":              {"exchange": "nyse",   "ticker": "DELL"},    # OEM inventory (buyer-side)
    "HPE":               {"exchange": "nyse",   "ticker": "HPE"},     # OEM inventory (buyer-side)
    "ASML":              {"exchange": "nasdaq", "ticker": "ASML"},    # equipment — coverage less consistent; flag will tell us
    # v3.39.7.8 — Chip designer expansion. AMD + Intel are Tier B
    # "first-layer bill-payers" (BUY memory chips for GPU/CPU/AI
    # accelerator integration). Cerebras IPO'd 2026-05-14; first
    # earnings call ~3 months out — entry added now so Motley Fool
    # captures it when it appears. Cerebras represents HBM-substitution
    # risk (WSE-3 uses on-chip SRAM not HBM).
    "AMD":               {"exchange": "nasdaq", "ticker": "AMD"},     # MI300X / MI400 — HBM demand driver
    "Intel":             {"exchange": "nasdaq", "ticker": "INTC"},    # Gaudi accelerators — HBM2e/HBM3 demand
    "Cerebras":          {"exchange": "nasdaq", "ticker": "CBRS"},    # WSE-3 wafer-scale — HBM substitution signal
}

# AI labs — NOT yet publicly traded. Dormant entries: when either lists, fill in
# the ticker + ir_url and move the name into IR_SOURCES (or MOTLEY_FOOL_TICKERS
# if US-listed with English calls). The news pipeline already watches for the
# listing event via GOOGLE_NEWS_QUERIES ("OpenAI IPO", "Anthropic public listing").
AI_LAB_IR_WATCH = {
    "OpenAI": {
        "ticker": None,        # fill when public
        "ir_url": None,
        "status": "not_public",
        "note": "Activate when IPO/direct-listing confirmed. Likely US-listed, English calls -> MOTLEY_FOOL_TICKERS.",
    },
    "Anthropic": {
        "ticker": None,
        "ir_url": None,
        "status": "not_public",
        "note": "Activate when IPO/direct-listing confirmed. Likely US-listed, English calls -> MOTLEY_FOOL_TICKERS.",
    },
}

# SEC EDGAR atom feeds — covers the SK Hynix US ADR / SPO event tracking
# (Indicator 7). Form types:
#   F-1   = registration statement for foreign private issuer (initial ADR/SPO filing)
#   F-1/A = amendments (SEC review iterations during roadshow)
#   424   = final prospectus / pricing supplement (filed pricing day)
#   6-K   = current report for foreign private issuer (material events)
#   20-F  = annual report
# We track all five so we catch every step of the F-1 -> roadshow -> 424 -> first-trade arc.
# Beats Korean press by 1-3 days on event detection.
EDGAR_FEEDS = {
    "SEC EDGAR / SK Hynix F-1 (registration)":
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix&type=F-1&dateb=&owner=include&count=10&output=atom",
    "SEC EDGAR / SK Hynix F-1/A (amendments)":
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix&type=F-1%2FA&dateb=&owner=include&count=10&output=atom",
    "SEC EDGAR / SK Hynix 424 (final prospectus)":
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix&type=424&dateb=&owner=include&count=10&output=atom",
    "SEC EDGAR / SK Hynix 6-K (material events)":
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix&type=6-K&dateb=&owner=include&count=10&output=atom",
    "SEC EDGAR / SK Hynix 20-F (annual)":
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=sk+hynix&type=20-F&dateb=&owner=include&count=5&output=atom",
    # Micron insider sales (t2_insider_sales — Micron CIK 0000723125)
    "SEC EDGAR / Micron Form 4 (insider transactions)":
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000723125&type=4&dateb=&owner=include&count=40&output=atom",
}

# Google News queries (RSS-ish)
GOOGLE_NEWS_QUERIES = [
    "SK Hynix earnings",
    "SK Hynix target price",
    "Micron memory CapEx",
    "Samsung memory HBM",
    "DDR5 spot price",
    "TrendForce DRAM contract",
    "hyperscaler 2027 CapEx",
    "HBM4 yield",
    "memory cycle peak",
    # ADR / SPO event tracking (Indicator 7)
    "SK Hynix F-1 SEC filing",
    "SK Hynix ADR roadshow",
    "SK Hynix US listing pricing",
    # Competitor dynamics (Indicator 6A)
    "CXMT DDR5",
    "ChangXin Memory production",
    "Chinese DRAM OEM",
    # AI efficiency (Tier 1)
    "KV cache compression",
    "inference efficiency breakthrough",
    # Broker + analyst combos (catches MS/GS/Bernstein thematic notes)
    "Morgan Stanley memory semiconductor",
    "Goldman Sachs SK Hynix Micron",
    "Bernstein semiconductor Rasgon",
    "JPMorgan memory DRAM analyst",
    # v3.39.7.5 — Korean labor + macro queries. The May 20 2026 brief
    # missed the Samsung-strike-resolution story (Korea Herald) which
    # is a major SK Hynix-positive catalyst (their primary competitor
    # avoiding production disruption). These targeted queries catch
    # labor / CapEx commitment / Korean macro stories that the
    # framework-keyword queries above miss because they don't mention
    # memory products or pricing directly.
    "Samsung union strike",
    "Samsung Electronics labor dispute",
    "SK Hynix labor union",
    "Samsung Electronics chip workers",
    "Korea memory chip industry",
    "Mizuho semiconductor memory",
    "Mirae Asset memory analyst",
    # Thematic cycle signals (NEW — would have caught the Jan 16 Shawn Kim note)
    "memory supercycle 2026",
    "HBM AI bottleneck",
    "memory analyst upgrade downgrade",
    # v3.2 — Captive-ratio / architectural-shift signals
    # Inference framework releases (production deployment of efficiency techniques)
    "vLLM release inference",
    "SGLang serving framework",
    "TensorRT-LLM release",
    # Quantization / efficiency techniques (Tier 1 leading indicators)
    "INT4 quantization LLM inference",
    "INT2 quantization research",
    "MoE Mixture of Experts efficiency",
    "speculative decoding deployment",
    "KV cache compression inference",
    "MLA multi-latent attention",
    "disaggregated serving inference",
    # Memory disaggregation / CXL
    "CXL 3.0 memory pooling production",
    "Astera Labs CXL revenue",
    "Samsung CMM-D CXL DRAM",
    # LPDDR datacenter (specific to SK Hynix relative-weakness risk)
    "LPDDR datacenter design win",
    "LPDDR5X server",
    "NVIDIA Grace LPDDR datacenter",
    "Samsung LPDDR datacenter Hynix Micron",
    # Custom silicon memory architecture
    "TPU v7 memory architecture",
    "Trainium 2 memory HBM LPDDR",
    "MTIA Meta memory architecture",
    "Maia Microsoft memory",
    # Inference cost benchmarks
    "Artificial Analysis tokens per dollar",
    "inference cost reduction hyperscaler",
    # GDDR inference-accelerator path
    "Etched Sohu Tenstorrent inference accelerator",
    "GDDR7 inference accelerator",
    # HBF wildcard
    "HBF High Bandwidth Flash SanDisk",
    # Conference / architecture leaks
    "HotChips memory architecture",
    "MICRO ISCA semiconductor memory",
    # Wafer allocation language (memory vendor strategy signal)
    "wafer allocation HBM DDR5",
    "memory CapEx mix HBM",
    # AI lab public-listing watch (v3.31 — activate IR scrapers when these fire)
    "OpenAI IPO filing",
    "OpenAI public listing",
    "Anthropic IPO",
    "Anthropic public listing",
]


# v3.39.7.6 — Watch-type queries (forward-looking event detectors).
# These queries are SUPPOSED to stay quiet most days — they exist to catch
# specific architectural / corporate-event signals when they happen.
# Examples:
#   - "CXL 3.0 memory pooling production" fires when the FIRST hyperscaler
#     announces production CXL pooling (the v3.4 indicator 24 trigger)
#   - "GDDR7 inference accelerator" fires when a vendor ships a GDDR7-based
#     inference part displacing HBM
#   - "OpenAI IPO filing" fires when (if) OpenAI files an S-1
#   - "Etched Sohu Tenstorrent inference accelerator" fires on production-
#     scale architecture-shift announcements
# 0 items returned for these is the EXPECTED routine state, not a problem.
# The diagnostics table renders them as 'quiet' (informational, muted)
# instead of 'warn' (might be broken). When they DO return items, the
# alert value is high.
#
# Queries NOT in this set are "live" feeds expected to have a steady news
# flow (analyst broker queries, earnings queries, Korean labor queries).
# 0 items from those IS suspicious and stays 'warn'.
WATCH_GOOGLE_QUERIES = {
    # v3.2 architectural-shift watchers
    "vLLM release inference",
    "SGLang serving framework",
    "TensorRT-LLM release",
    "INT4 quantization LLM inference",
    "INT2 quantization research",
    "MoE Mixture of Experts efficiency",
    "speculative decoding deployment",
    "KV cache compression inference",
    "MLA multi-latent attention",
    "disaggregated serving inference",
    "CXL 3.0 memory pooling production",
    "Astera Labs CXL revenue",
    "Samsung CMM-D CXL DRAM",
    "LPDDR datacenter design win",
    "LPDDR5X server",
    "NVIDIA Grace LPDDR datacenter",
    "Samsung LPDDR datacenter Hynix Micron",
    "TPU v7 memory architecture",
    "Trainium 2 memory HBM LPDDR",
    "MTIA Meta memory architecture",
    "Maia Microsoft memory",
    "Artificial Analysis tokens per dollar",
    "inference cost reduction hyperscaler",
    "Etched Sohu Tenstorrent inference accelerator",
    "GDDR7 inference accelerator",
    "HBF High Bandwidth Flash SanDisk",
    "HotChips memory architecture",
    "MICRO ISCA semiconductor memory",
    "wafer allocation HBM DDR5",
    "memory CapEx mix HBM",
    # AI lab listing watchers
    "OpenAI IPO filing",
    "OpenAI public listing",
    "Anthropic IPO",
    "Anthropic public listing",
    # SK Hynix ADR-event tracking (pre-listing speculative)
    "SK Hynix F-1 SEC filing",
    "SK Hynix ADR roadshow",
    "SK Hynix US listing pricing",
    # Specific-analyst queries (only fire when that analyst publishes)
    "Bernstein semiconductor Rasgon",
    "Mirae Asset memory analyst",
    "Mizuho semiconductor memory",
    # Competitor watchers (binary-ish events)
    "CXMT DDR5",
    "ChangXin Memory production",
    "Chinese DRAM OEM",
}


# v3.39.7.6 — SEC EDGAR feeds where 0-item is the EXPECTED state.
# SK Hynix isn't yet US-listed; the ADR / SPO is in progress (Indicator 7).
# Until the ADR lists, the F-1 / F-1/A / 424 / 6-K / 20-F atom feeds should
# stay empty — that emptiness is the indicator's NORMAL state. Once one of
# these fires (a filing actually appears), it's a major event. Diagnostics
# table should reflect "still pre-listing" rather than "feed broken".
# Micron Form 4 is NOT in this set — Micron files Form 4s regularly when
# insiders trade, so a 0-item Micron Form 4 IS legitimately suspicious.
EXPECTED_EMPTY_EDGAR_FEEDS = {
    "SEC EDGAR / SK Hynix F-1 (registration)",
    "SEC EDGAR / SK Hynix F-1/A (amendments)",
    "SEC EDGAR / SK Hynix 424 (final prospectus)",
    "SEC EDGAR / SK Hynix 6-K (material events)",
    "SEC EDGAR / SK Hynix 20-F (annual)",
}


# ============================================================
# Analyst / broker / company tracking — used by news relevance filter
# Keyword matching here decides which headlines make it past Pass 1.
# ============================================================

TRACKED_ANALYSTS = {
    # Morgan Stanley
    "Shawn Kim":        "Morgan Stanley",   # SK Hynix / Samsung Korea
    "Joseph Moore":     "Morgan Stanley",   # Micron US
    # Goldman Sachs
    "Giuni Lee":        "Goldman Sachs",    # SK Hynix Korea
    "Toshiya Hari":     "Goldman Sachs",    # US semis
    # Bernstein
    "Stacy Rasgon":     "Bernstein",        # most-quoted US semi analyst
    # JPMorgan
    "Gokul Hariharan":  "JPMorgan",         # Asia semis
    "Harlan Sur":       "JPMorgan",         # US semis
    # Mizuho
    "Vijay Rakesh":     "Mizuho",
    # BofA
    "Vivek Arya":       "BofA",
    # Mirae Asset
    "Doh Hyun-woo":     "Mirae Asset",
    "Lee Soon-hak":     "Mirae Asset",
    # NH Investment
    "Do Hyun-woo":      "NH Investment",
}

TRACKED_COMPANIES = [
    # v3.39.7.7 — TRACKED_COMPANIES is now derived (see end of this block).
    # The three explicit tier lists below are the source of truth; they
    # capture both Pass 1 keyword matching (here) and Pass 2 Haiku scoring
    # rubric (in src/data/news.py score_relevance). The rubric explicitly
    # references the three tiers by name so Haiku gives news on each tier
    # the right relevance weight.
    #
    # The historical (pre-v3.39.7.7) shape of TRACKED_COMPANIES was a flat
    # list containing only SK Hynix/Hynix, Micron/MU, Samsung/Samsung
    # Electronics, ASML, Applied Materials/AMAT, CXMT/ChangXin. That made
    # the news pipeline blind to the customer ecosystem (hyperscalers, AI
    # labs, NVIDIA), causing direct headlines like "NVIDIA Q1 FY27 Data
    # Center revenue $35B" to drop at Pass 1 because no tracked keyword
    # appeared. The May 21 2026 brief surfaced this gap when the Samsung
    # strike + NVIDIA earnings dominated semis news and the framework
    # could only see NVIDIA second-hand via Samsung/Micron-mentioning
    # articles. The deep tier transcript captured the call itself but
    # the fast tier news section had no direct NVIDIA coverage.
]

# v3.39.7.8 — Restructured from v3.39.7.7's three tiers into four value-
# chain tiers per user direction 2026-05-21. The v3.39.7.7 layout
# conflated "chip designers that BUY memory chips" (NVIDIA, Broadcom,
# AMD, Intel, Cerebras) with "ecosystem peers" (ASML, TSMC, Oracle,
# Dell, HPE) and put both into the same TRACKED_REST bucket. User's
# framing: chip designers are the FIRST-LAYER bill-payers from SK
# Hynix's perspective — they're the direct customers paying memory
# makers for HBM/DRAM. Hyperscalers + AI labs are the SECOND-LAYER —
# they pay the chip designers for finished GPU/accelerator systems.
# Ecosystem (equipment, foundry, OEMs) sits to the side.
#
# Additionally CXMT and ChangXin (Chinese DRAM) were in v3.39.7.7's
# TRACKED_REST as "competitors" but they're competitive RIVALS in the
# same product market as SK Hynix — they belong in Tier A.

# Tier A — Pure memory makers and competitive rivals. The assets the
# framework tracks (SK Hynix, Micron, Samsung) plus the up-and-coming
# Chinese rivals producing the same product class. Highest baseline
# relevance — anything mentioning these names is first-order signal
# for the memory cycle. Haiku rubric: 9-10 for direct analyst action /
# TrendForce-DigiTimes pricing / Korean labor news affecting Samsung
# or SK Hynix production / insider sales filings / earnings; 7-8 for
# thematic notes naming these companies.
TRACKED_PURE_MEMORY = [
    "SK Hynix", "Hynix",
    "Micron", " MU ",  # spaces around MU avoid matching "museum", "amused", etc.
    "Samsung", "Samsung Electronics",
    "CXMT", "ChangXin",  # Chinese DRAM up-and-coming rivals (Indicator 6A)
]

# Tier B — Chip designers / first-layer bill-payers. Companies that BUY
# memory chips and integrate them into GPUs / CPUs / AI accelerators.
# NVIDIA is the dominant HBM consumer (their GPUs contain SK Hynix HBM3E
# and HBM4); AMD MI300X/MI400 + Intel Gaudi accelerators consume HBM at
# scale; Broadcom manufactures custom AI silicon (Google TPU, Meta MTIA)
# which contains HBM; Cerebras WSE-3 uses on-chip SRAM (NOT HBM) which
# makes Cerebras especially relevant as an HBM-SUBSTITUTION signal —
# i.e. a direct read on the v3.4 captive-ratio risk.
#
# From SK Hynix's perspective these are the direct customers — the
# invoice goes to them. Haiku rubric: 8-9 for earnings + HBM/data-center
# backlog commentary / GPU+HBM supply agreements with named volume;
# 6-7 for general product launches without quantified memory demand
# impact. Tier B is the CONCURRENT HBM demand signal (these companies
# tell us what they ARE buying today).
TRACKED_CHIP_DESIGNERS = [
    "NVIDIA", "Nvidia", "NVDA",
    "AMD", "Advanced Micro Devices",  # AMD short enough to skip padding — no common false-positive substrings
    "Intel", "INTC",
    "Broadcom", "AVGO",
    "Cerebras", "CBRS",  # IPO'd 2026-05-14 on Nasdaq; WSE-3 uses on-chip SRAM not HBM — HBM substitution-risk signal
]

# Tier C — Hyperscalers + AI labs / second-layer bill-payers. They buy
# finished GPUs / AI accelerator systems FROM the chip designers (Tier
# B). Their CapEx commitments flow downhill through the value chain:
# Microsoft commits $80B to Azure → flows to NVIDIA → flows to SK
# Hynix HBM. OpenAI / Anthropic are private — no earnings call to
# capture, but their CapEx commitments, model deployments, and
# infrastructure partnership announcements directly inform memory demand.
#
# Tier C is the LEADING CapEx commitment signal (6-12 months ahead of
# memory cycle peak). Haiku rubric: 8-9 for CapEx commitments / multi-
# billion infrastructure deals / hyperscaler earnings with explicit
# memory or chip-supply commentary / AI lab funding rounds at $5B+;
# 6-7 for model releases / general earnings without infra detail.
TRACKED_HYPERSCALERS_AND_LABS = [
    # AI labs (private — no earnings call; news mentions only)
    "OpenAI", "Open AI",
    "Anthropic",
    # Hyperscalers (public — also covered by transcript cascade)
    "Microsoft", "MSFT", "Azure",
    "Alphabet", "Google", "GOOGL",
    "Amazon", "AMZN", " AWS ",  # spaces around AWS avoid "laws", "jaws", "outlaws"
    "Meta", "Facebook",  # "Meta" intentionally unpadded — too many real headlines use it bare ("Meta Platforms", "Meta Q3"); cost of false-positive on "metadata"/"metaphor" is one Haiku call that drops at 0-3
]

# Tier D — Ecosystem. Suppliers, foundry partners, OEMs, distributors.
# ASML (lithography supplier to memory + chip makers); TSMC (foundry —
# manufactures for AMD/NVIDIA/Broadcom/Cerebras; CoWoS packaging is the
# HBM integration bottleneck); Applied Materials (deposition equipment
# supplier); Oracle (cloud — secondary hyperscaler); Dell/HPE (server
# OEMs whose inventory commentary is the t3_supplier_inventory_rebuild
# input).
#
# Haiku rubric: 6-7 for equipment WFE earnings + memory mix commentary /
# TSMC CoWoS commentary / OEM channel inventory; 4-5 for general
# equipment news naming these companies without memory-specific
# implication.
TRACKED_ECOSYSTEM = [
    # Equipment
    "ASML",
    "Applied Materials", "AMAT",
    # Foundry / packaging
    "TSMC", "TSM",
    # Hyperscaler-adjacent (Oracle as secondary cloud)
    "Oracle", "ORCL",
    # OEMs (server box vendors — Tier 3 channel inventory signal)
    "Dell", " HPE ",  # spaces around HPE avoid false matches in "shape", "stripes"
]

# Back-compat merged union — keep TRACKED_COMPANIES symbol stable so the
# single call site (src/fast_tier.py:468) doesn't need to change. Any
# external scripts importing TRACKED_COMPANIES continue to work; the
# union now spans all four tiers.
TRACKED_COMPANIES = (
    TRACKED_PURE_MEMORY
    + TRACKED_CHIP_DESIGNERS
    + TRACKED_HYPERSCALERS_AND_LABS
    + TRACKED_ECOSYSTEM
)

TRACKED_BROKERS = [
    "Morgan Stanley", "Goldman Sachs", "JPMorgan", "JP Morgan", "Mizuho",
    "Bernstein", "BofA", "Bank of America", "Citi", "Citigroup",
    "UBS", "HSBC", "Macquarie", "Susquehanna",
    "Mirae Asset", "NH Investment", "Samsung Securities",
    "Korea Investment", "Daishin", "Hana Securities",
]


# v3.2 — Architectural concept tracking for relevance filter
# These are technique / architecture / mechanism names that signal a
# captive-ratio-relevant story. The relevance filter Pass 1 treats any
# keyword match here as a hit, same as a tracked analyst or company.
TRACKED_CONCEPTS = [
    # Quantization / model compression
    "INT4", "INT2", "FP4", "FP8 quantization", "quantization-aware",
    # Architecture techniques (decode bandwidth reduction)
    "Mixture of Experts", "MoE", "speculative decoding", "Medusa", "EAGLE",
    "multi-token prediction", "sliding window attention",
    # KV cache work
    "KV cache", "MLA", "multi-latent attention", "paged attention", "GQA",
    # Serving / deployment
    "disaggregated serving", "prefill decode disaggregation",
    "vLLM", "SGLang", "TensorRT-LLM",
    # Memory architecture / pooling
    "CXL 3.0", "CXL pooling", "memory disaggregation", "CMM-D",
    "LPDDR datacenter", "LPDDR-DC", "Grace LPDDR",
    # Vendor-specific custom silicon
    "TPU v7", "Trainium", "MTIA", "Maia", "Inferentia",
    # Wildcards
    "HBF", "High Bandwidth Flash",
    # Cost / economics framing
    "captive ratio", "wafer allocation", "tokens per dollar", "tokens/$",
    "inference cost reduction", "BOM share", "BOM cost",
    # Substitution candidates
    "GDDR7", "HBM substitute", "HBM alternative",
]


# ============================================================
# Calendar of upcoming events (next 90 days from today)
# Renders as the second-to-last section of the email.
# Pipeline auto-filters to dates <= today + 90.
# ============================================================
# Format: each entry is (date_iso, category, title, why_it_matters, related_indicator_or_signal)
# Pipeline reads CALENDAR_OF_EVENTS, filters by date, and renders sorted.
# Update this list as events publish or pass.

from datetime import date as _date

CALENDAR_OF_EVENTS = [
    # ----- AI demand reads (NVIDIA earnings — Tier 2 catalyst, hyperscaler-adjacent) -----
    # v3.39: NVIDIA Q1 FY27 confirmed for May 20, 2026 after US close (press
    # release ~4:20pm ET, call 5pm ET). The earnings drive hyperscaler buying-
    # intent signal: NVIDIA's data center commentary informs t2_capex_parabolic
    # ("AI infrastructure orders accelerating / decelerating") and provides the
    # earliest read on HBM pull-through demand, feeding all memory pure-plays.
    # Was missing from the hand-curated calendar through v3.38.x; added now.
    ("2026-05-20", "Earnings", "NVIDIA Q1 FY27 earnings (after US close)",
     "AI demand pull-through read for HBM and memory cycle. Data center revenue + commentary on hyperscaler buying intent are the leading signal for t2_capex_parabolic and Tier 1 HBM demand. Watch CFO Kress's prepared commentary (1:20pm PT release) for forward color.",
     "t2_capex_parabolic"),
    ("2026-08-19", "Earnings", "NVIDIA Q2 FY27 earnings (approx)",
     "Next NVIDIA print — typical reporting cadence is ~3 months. Confirms or contradicts Q1 narrative on hyperscaler buying intent. Approximate date; refine when confirmed.",
     "t2_capex_parabolic"),

    # ----- Memory maker earnings (Tier 3 catalysts) -----
    ("2026-06-24", "Earnings", "Micron Q3 FY26 earnings",
     "Single largest catalyst for t3_eps_miss_guidance. Watch beat magnitude (prior 41.86%); first sub-10% beat is signal.",
     "t3_eps_miss_guidance"),
    ("2026-07-29", "Earnings", "SK Hynix Q2 2026 earnings",
     "THE BIG ONE. HBM4 yields, Samsung dynamics, CapEx guidance, FY27 outlook. First sub-10% beat after Q1 48.8% = peak signal.",
     "t3_eps_miss_guidance"),
    ("2026-07-31", "Earnings", "Samsung Q2 2026 earnings (full results)",
     "Memory segment commentary; HBM4 progress vs SK Hynix.",
     "i6b_samsung_hbm4"),

    # ----- Hyperscaler earnings (Tier 2 catalysts) -----
    ("2026-07-22", "Earnings", "Microsoft Q4 FY26 earnings",
     "2027 CapEx guidance — bull confirm if above 2026 ($190B); bear catalyst if flat/down.",
     "t2_capex_parabolic"),
    ("2026-07-22", "Earnings", "Alphabet Q2 2026 earnings",
     "2027 CapEx guidance vs 2026 baseline ($185B).",
     "t2_capex_parabolic"),
    ("2026-07-23", "Earnings", "Meta Q2 2026 earnings",
     "2027 CapEx guidance. Meta has worst FCF math — most likely first to blink.",
     "t2_capex_parabolic"),
    ("2026-07-24", "Earnings", "Amazon Q2 2026 earnings",
     "2027 CapEx guidance; AWS infrastructure spend.",
     "t2_capex_parabolic"),

    # ----- Equipment makers (Tier 3 confirm) -----
    ("2026-07-15", "Earnings", "ASML Q2 2026 earnings (approx)",
     "Bookings + book-to-bill; EUV order direction. Equipment forward indicator.",
     "t3_wfe_orders_crack"),
    ("2026-08-13", "Earnings", "Applied Materials Q3 FY26 earnings (approx)",
     "WFE bookings; memory-mix percentage. AMAT is most memory-exposed of WFE names.",
     "t3_wfe_orders_crack"),

    # ----- TrendForce monthly contract reports (Tier 3 most diagnostic) -----
    ("2026-05-28", "Industry data", "TrendForce May DRAM monthly contract report",
     "DDR5 server contract direction. First sub-58% QoQ = warning; first negative = Tier 3 fired.",
     "t3_contract_decline"),
    ("2026-06-25", "Industry data", "TrendForce June DRAM monthly contract report",
     "Same as above. Q3 2026 contract guidance prints first appear here.",
     "t3_contract_decline"),
    ("2026-07-30", "Industry data", "TrendForce July DRAM monthly contract report",
     "Critical — first full month after SK Hynix Q2 earnings. Confirms or contradicts mgmt commentary.",
     "t3_contract_decline"),

    # ----- Indicator 7 ADR/SPO milestones -----
    ("2026-05-20", "Indicator 7 / ADR", "Expected F-1 public release window opens",
     "First public SEC filing for the ADR. Confirms size, structure, underwriters. -1 to -4% mechanical.",
     "i7_adr_launch"),
    ("2026-06-15", "Indicator 7 / ADR", "US roadshow window (mid-May to mid-Jun)",
     "Reuters/Bloomberg demand signals. 'Oversubscribed' = positive; 'demand soft' = negative.",
     "i7_adr_launch"),
    ("2026-07-01", "Indicator 7 / ADR", "Expected 424B pricing day window",
     "Final pricing disclosure. -3 to -6% mechanical (highest impact event in cycle).",
     "i7_adr_launch"),
    ("2026-07-03", "Indicator 7 / ADR", "Expected first US trading day",
     "ADR begins on NYSE/NASDAQ. +1 to +4% from US flows.",
     "i7_adr_launch"),

    # ----- HBM4 milestone calendar (Indicator 6B) -----
    ("2026-06-30", "Indicator 6 / HBM4", "SK Hynix HBM4 11Gbps qualification deadline (informal)",
     "Currently 'still optimizing'. Silence past Jun = problem. Delays = Samsung gains share.",
     "i6b_samsung_hbm4"),
    ("2026-07-29", "Indicator 6 / HBM4", "HBM4 yield disclosure window (via SK Hynix Q2 earnings)",
     "Target 65-70% by Q3 2026 (vs 50-60% HBM3E at same maturity). Below 65% = margin compression risk.",
     "i6b_samsung_hbm4"),

    # ----- Industry conferences -----
    ("2026-06-02", "Conference", "COMPUTEX 2026 (Taipei)",
     "Management commentary triggers ~20% of off-quarter analyst revisions. Watch Nvidia, AMD, supply chain.",
     "Multiple"),
    ("2026-09-09", "Conference", "Goldman Comm-Tech Conference",
     "Mid-quarter analyst commentary trigger. Memory mgmt often presents.",
     "Multiple"),

    # ----- Macro / industry data -----
    ("2026-07-15", "Macro", "Fed FOMC meeting (approx)",
     "Rate decisions affect memory makers indirectly via USD/KRW and hyperscaler cost of capital.",
     "Macro"),
    ("2026-06-10", "Industry data", "IDC quarterly PC shipment data (approx)",
     "Revisions to 2026 PC forecast. Current -2.4 to -11.3%. Worse than -5% = t1_demand_destruction confirms.",
     "t1_demand_destruction"),
    ("2026-08-05", "Industry data", "Counterpoint Q2 smartphone shipment data (approx)",
     "Current -2%. Below 0% confirmed = Tier 1 demand destruction.",
     "t1_demand_destruction"),

    # ----- Mandatory disclosure deadlines -----
    ("2026-09-24", "Indicator 7 / ADR", "Mandatory ADR path disclosure deadline",
     "Per F-1 6-month window. Final disclosure of use of proceeds and dilution math.",
     "i7_adr_launch"),
]


def get_upcoming_events(within_days: int = 90, today: Optional[_date] = None) -> list[dict]:
    """Return calendar events within `within_days` from `today` (default: now).

    Used by the email template to render the 'Calendar of events' section.
    Returns events sorted by date ascending.
    """
    from datetime import date, timedelta
    today = today or date.today()
    horizon = today + timedelta(days=within_days)
    upcoming = []
    for event_date_str, category, title, why, related in CALENDAR_OF_EVENTS:
        event_date = date.fromisoformat(event_date_str)
        if today <= event_date <= horizon:
            upcoming.append({
                "date": event_date,
                "date_iso": event_date_str,
                "days_away": (event_date - today).days,
                "category": category,
                "title": title,
                "why_it_matters": why,
                "related": related,
            })
    upcoming.sort(key=lambda e: e["date"])
    return upcoming


# ============================================================
# LLM configuration - two-model strategy for cost efficiency
# ============================================================
#
# DEFAULT: tier aliases (`haiku`, `sonnet`) that auto-resolve to the
# current recommended version of each tier on the Anthropic API. This
# makes the pipeline future-proof against model deprecation: when a
# specific version retires, the alias rolls forward to the replacement
# without any code or config change.
#
# The trade-off vs. pinning a specific version:
#   - Lose: tight cost predictability, audit trail of exact version used
#   - Gain: zero maintenance across the inevitable model retirements
# For a personal daily brief this is a clear win. The validation table
# at the bottom of every brief still catches outright failures.
#
# OVERRIDE PATH — for deliberate version control. Set in GitHub Secrets:
#   HAIKU_MODEL_OVERRIDE  = pin to a specific version, OR substitute
#                           a different tier entirely (e.g. if you decide
#                           Haiku 7 is good enough for stage 2 and want
#                           to drop Sonnet for both stages)
#   SONNET_MODEL_OVERRIDE = same idea for stage 2
# Empty / unset = use the alias default.
#
# The override env var is also useful in two narrower cases:
#   1. Pinning to a specific dated version for reproducibility during
#      a backtest window
#   2. Forcing a downgrade if a new generation introduces a regression
#      and you want to stay on the prior version until it stabilizes
#      (note: deprecated models eventually retire — this is a temporary
#      bridge, not a long-term solution)

# Stage 1 — small/cheap model for high-volume news classification.
# Currently resolves to Haiku 4.5; will roll forward as Anthropic ships
# new Haiku-tier models.
HAIKU_MODEL = os.environ.get("HAIKU_MODEL_OVERRIDE") or "claude-haiku-4-5"
# v3.38.4: bumped 8000 → 16000. The 15:32 UTC May 17 run had Haiku triage
# fail JSON parse at char 13926 (unterminated string). News volume scales:
# 50 items × ~200 tokens output each = ~10K tokens. 8000 was below floor;
# 16000 gives 60% headroom. Still well within Haiku 4.5's 64K cap. json_repair
# in llm_summarizer.py covers the case where output IS valid but malformed.
HAIKU_MAX_TOKENS = 16000

# Stage 2 — mid-tier reasoning model for tier classification, probability
# synthesis, exec summary composition. Currently resolves to Sonnet 4.6;
# will roll forward. The largest tier (`opus`) would be ~3-5x cost for
# marginal quality gain on this structured task.
SONNET_MODEL = os.environ.get("SONNET_MODEL_OVERRIDE") or "claude-sonnet-4-6"
# v3.39.1: bumped 24000 → 48000. The 20:30 May 17 run (first with v3.39
# streaming) used the full 24K cap and still truncated at char 47369, with
# json_repair recovering 14/22 indicators. The 14th was partial, missing
# `latest_developments`, which crashed the parse step. Output need is
# ~46-50K tokens for the full prompt response: ~22 indicators × ~2K each
# (with five getting deep-dive depth) + exec_summary ~3K + market_summary
# ~1K + key_news_summary ~3K + cycle_position_call ~2K. 48K cap leaves
# margin for variance.
#
# Sonnet 4.6 supports up to 64K max output, so 48K is well under the model
# ceiling. Streaming bypasses the SDK wall-time guard. At observed ~45
# tokens/sec streaming throughput, 48K tokens = ~18 min Sonnet phase,
# which still fits the workflow timeout (now 35 min — see fast_tier.yml).
#
# Defensive parsing in _safe_indicator_assessment ensures that even if we
# do truncate at 48K, recovery doesn't crash — partial indicator dicts get
# safe defaults instead of TypeError.
#
# Prior history:
#   v3.39:    16K → 24K (with streaming bypassing SDK guard) — still truncated
#   v3.38.5:  24K → 16K (dodged SDK guard at cost of truncation)
#   v3.38.4:  8K → 24K (handle full 22-indicator output)
SONNET_MAX_TOKENS = 48000

LLM_TEMPERATURE = 0.3  # low for analytical work


# ============================================================
# Cycle baseline (current as of May 2026)
# Updated periodically as new evidence comes in.
# Used as the LLM's prior for probability calculations.
# ============================================================

CYCLE_BASELINE = {
    "current_phase": 4,
    "current_phase_label": "Boom / Euphoria (late stage)",
    "months_into_cycle": 31,  # from Q4 2023 trough
    # v3.39: dropped neutral_case_*. Previously there were FOUR scenarios
    # in the baseline summing to 100% (base 30 + bull 20 + bear 25 + neutral
    # 25), but the email template only ever displayed three (base/bull/bear).
    # The hidden neutral case absorbed 24-25% of probability mass invisibly,
    # which is why the May 17 brief showed bear 30% + base 28% + bull 18%
    # = 76% summed in the email — the 24% neutral was real but never shown.
    # Now three scenarios only, summing to 100% by post-LLM invariant
    # enforcement (see _enforce_scenario_invariants in llm_summarizer.py).
    #
    # Framing: probability of length-to-peak + return-to-peak scenario for
    # SK Hynix. Base is the most-probable scenario (always > bear and bull
    # by invariant); bull requires signals to un-fire or delay materially;
    # bear requires faster degradation than expected, and can carry a
    # NEGATIVE return-to-peak (peak already in → downside from here).
    "base_case_peak_months_away": 4,
    "base_case_probability_pct": 50,        # was 30; absorbed neutral's share
    "base_case_return_to_peak_pct": 18,     # signed: positive = upside
    "bull_case_peak_months_away": 12,
    "bull_case_probability_pct": 22,        # was 20; small bump
    "bull_case_return_to_peak_pct": 55,
    "bear_case_peak_months_away": 0,
    "bear_case_probability_pct": 28,        # was 25; small bump
    "bear_case_return_to_peak_pct": 0,      # can go NEGATIVE as signals fire
}


# ============================================================
# Supercycle chart constants (v3.39.2)
# ============================================================
# The "Where we stand in the cycle" chart in the email overlays the
# generic 5-phase memory supercycle anatomy onto SK Hynix's actual
# price history. These constants pin the timeline.
#
# Phase boundaries are computed at render time by anchoring the END
# of Phase 4 to executive_summary.base_case.peak_window_start (so the
# chart self-positions against the LLM's current peak projection) and
# back-filling the earlier phases using CYCLE_PHASE_DURATIONS_MONTHS.
# This means as the base case shifts month-to-month, the phase shading
# on the chart shifts with it — preserving the visual relationship
# "today is X months from end of Phase 4" no matter when the brief runs.
#
# Anchoring at the BASE case (not bull or bear) is intentional. The
# base case is the most-probable scenario by invariant. Using it as
# the phase anchor means the chart shows "the cycle we expect" as the
# default visual context, with bull/bear as projection curves that
# diverge from that anchor.

# Cycle trough = Q4 2023. Pinned date; update if the cycle ever resets.
# SK Hynix closing low in Q4 2023 was around October 31, 2023 (~120,000 KRW)
# per the price record. Phase 1 (trough) extends from this date forward.
CYCLE_TROUGH_DATE = "2023-10-31"

# Generic memory-supercycle phase durations in months. From the
# memory_supercycle_analysis.docx Part 1 — "The universal anatomy".
# Total = 60 months across a full cycle. Real cycles vary by 20-30%,
# but the sequence is consistent. v3.39.2 chart uses these as the
# base shape and stretches/compresses to fit the actual base-case peak.
CYCLE_PHASE_DURATIONS_MONTHS = {
    "Phase 1 — Trough":      9,
    "Phase 2 — Recovery":    12,
    "Phase 3 — Expansion":   12,
    "Phase 4 — Boom":        9,
    "Phase 5 — Decline":     18,
}

# Phase colors for the chart shading. Soft pastels to match the
# generic chart in the source document; harmonized with the email's
# existing phase-tag palette (template.py).
CYCLE_PHASE_COLORS = {
    "Phase 1 — Trough":      "#F4F2EC",  # neutral cream
    "Phase 2 — Recovery":    "#E8EEF6",  # pale blue
    "Phase 3 — Expansion":   "#E5F0E9",  # pale green
    "Phase 4 — Boom":        "#FAEEDA",  # warm amber (matches email phase-tag bg)
    "Phase 5 — Decline":     "#F8DEDE",  # pale red
}

# The ticker whose price drives the supercycle chart. SK Hynix is the
# single best vehicle to read the current cycle per the analysis doc
# (60% global HBM share, ~60-70% revenue from DRAM). Centralized here
# so chart_generator doesn't hard-code the lookup name.
SUPERCYCLE_CHART_TICKER_NAME = "SK Hynix"
SUPERCYCLE_CHART_TICKER_CURRENCY = "KRW"


# ============================================================
# Big mover threshold (for executive summary)
# ============================================================

BIG_MOVER_THRESHOLD_PCT = 4.0  # +/-4% in single session


# ============================================================
# Email configuration (loaded from env)
# ============================================================

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("GMAIL_ADDRESS", "")
SMTP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_RECIPIENT", "")
EMAIL_FROM_NAME = "Memory Cycle Brief"


# ============================================================
# API keys
# ============================================================

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")  # Optional, free tier 250 req/day


# ============================================================
# v3.39.7 — Cycle matrix configuration
# ============================================================
# The cycle matrix is a contract-growth x spot-drawdown trigger grid
# evaluated by src/data/cycle_matrix.py against the CSVs below.
# Verdict (Hold/Warn/Fire) maps to t1_spot_decline indicator status
# (Not fired/Partially fired/Fired) in fast_tier.py.

from pathlib import Path as _Path_v3397

CYCLE_MATRIX_DATA_DIR = _Path_v3397("data")
CYCLE_MATRIX_SPOT_CSV = CYCLE_MATRIX_DATA_DIR / "dram_spot_history.csv"
CYCLE_MATRIX_CONTRACT_ACTUALS_CSV = CYCLE_MATRIX_DATA_DIR / "dram_contract_actuals.csv"
CYCLE_MATRIX_FORECASTS_CSV = CYCLE_MATRIX_DATA_DIR / "dram_contract_quarterly_forecasts.csv"
CYCLE_MATRIX_SIGNALS_CSV = CYCLE_MATRIX_DATA_DIR / "dram_contract_signals.csv"

# The focal SKU used to compute the spot column (drawdown from rolling peak)
CYCLE_MATRIX_PRIMARY_SKU = "DDR5_16Gb_4800_5600"

# Synthetic estimate weights (must sum to <= 1.0)
CYCLE_MATRIX_WEIGHTS = {
    "tone": 0.4,
    "supplier": 0.4,
    "spot_lead": 0.2,
}

# Lookback windows
CYCLE_MATRIX_SIGNALS_LOOKBACK_WEEKS = 4
CYCLE_MATRIX_ROLLING_PEAK_WINDOW_WEEKS = 12

# Cost control: max number of Haiku extractions per pipeline run
CYCLE_MATRIX_MAX_EXTRACTIONS_PER_RUN = 10

# Status mapping: matrix verdict cell -> indicator status badge.
# v3.39.7.1 — MatrixCell.HOLD/WARN/FIRE .value strings were changed to
# "Not fired" / "Partially fired" / "Fired" directly, so this map is now
# the identity. Kept as a constant for documentation and so fast_tier.py's
# override code reads naturally ("here we map the matrix verdict to the
# indicator status") without callers needing to know they're identical.
CYCLE_MATRIX_STATUS_MAP = {
    "Not fired": "Not fired",
    "Partially fired": "Partially fired",
    "Fired": "Fired",
}
