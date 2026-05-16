"""
Configuration for the Memory Cycle Brief pipeline.

Defines the tickers tracked, the indicator framework (tiers 1-4),
and the thresholds that classify each signal as fired vs not fired.

CHANGELOG (this file):
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

# Hyperscaler watch list for capex monitoring
HYPERSCALERS = {
    "Microsoft": "MSFT",
    "Alphabet": "GOOGL",
    "Meta": "META",
    "Amazon": "AMZN",
    "Oracle": "ORCL",
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


INDICATORS: list[Indicator] = [
    # =====================================================================
    # Tier 1 — Early warning (lead stock peak by 3-6 months)
    # =====================================================================

    Indicator(
        id="t1_spot_decline",
        tier=1,
        name="DDR5 spot first MoM decline",
        description=(
            "DRAM spot prices show first month-over-month decline. DDR5 16Gb chip is "
            "the validation; DDR4 alone is partly EOL noise."
        ),
        what_to_look_for=(
            "DDR5 16Gb 4800/5600 spot prints 2 consecutive weeks of -2%+ MoW decline "
            "(current reference: $39). OR DDR5 RDIMM 32GB module first MoM decline "
            "(current $970, trigger if <$950). DDR4-only moves are NOT a trigger."
        ),
        sources=["TrendForce news", "Tom's Hardware RAM index (proxy)",
                 "Korean broker PDFs (Mirae/NH cite spot charts)"],
        trigger_patterns=[
            "DDR5 spot declined", "DDR5 spot fell", "DDR5 spot weakened",
            "DDR5 spot softened", "DDR5 spot weakness", "DDR5 module price decline",
            "DDR5 16Gb decline", "DDR5 RDIMM decline",
            # Korean broker note phrasings (will surface via Mirae/NH/Samsung PDFs):
            "spot price correction", "spot pricing under pressure",
        ],
        trigger_numeric={
            "ddr5_16gb_spot": {"trigger_below": 38.2, "consecutive_weeks": 2, "wow_pct": -2.0},
            "ddr5_rdimm_32gb": {"trigger_below": 950, "comparison": "MoM"},
            # Tom's Hardware proxy (we CAN scrape this):
            "tomshardware_ddr5_wow_pct": {"trigger_below": -2.0, "consecutive_weeks": 2},
        },
        cadence="2 consecutive weeks for spot; first MoM decline for module",
        stitching=(
            "Stitched: trigger fires when ANY TWO of {TrendForce news commentary, "
            "Tom's Hardware retail proxy, Korean broker PDF chart citation} corroborate "
            "within 14 days. DDR4-only moves don't trigger — must be DDR5 specific."
        ),
    ),

    Indicator(
        id="t1_lead_times",
        tier=1,
        name="Lead times shrinking",
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
        name="Capex announcements parabolic",
        description=(
            "Memory makers' and hyperscalers' capex growing parabolic — building 2027-28 "
            "supply glut. ALREADY FIRED on aggregate; watch for 2027 guidance direction at Jul prints."
        ),
        what_to_look_for=(
            "Big Three combined capex >2x 2024 (fired). Hyperscaler 2026 aggregate >$725B "
            "(fired). NEW threshold: any of MSFT/GOOG/META/AMZN/ORCL guides 2027 capex "
            "ABOVE 2026 at Q2 2026 earnings prints. Bull confirm if above; bear catalyst if flat/down."
        ),
        sources=[
            "Hyperscaler IR pages (microsoft.com/investor, abc.xyz/investor, ir.meta.com, ir.aboutamazon.com, investor.oracle.com)",
            "Yahoo Finance earnings transcripts (full Q&A on capex)",
            "Memory maker earnings (Korean press + press releases)",
        ],
        trigger_patterns=[
            "2027 capex", "2027 capital expenditure", "FY27 capex",
            "capex guidance raised", "capex guidance lowered",
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
            "Hyperscaler IR press release primary. Yahoo Finance Q&A transcript adds depth on capex intent. "
            "Trigger on EACH hyperscaler independently — one blink is enough for bear catalyst."
        ),
    ),

    Indicator(
        id="t2_ltas_signed",
        tier=2,
        name="Multi-year LTAs aggressively signed",
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
        name="Hyperscaler capex/revenue >40%",
        description="Capex/revenue ratios at historic norms 12-18%. Above 40% is unsustainable.",
        what_to_look_for=(
            "Any of MSFT/GOOG/META/AMZN/ORCL prints capex/revenue >40% in quarterly "
            "filings. Already fired: MSFT 45%, Oracle 57%. Watch for FCF projections cratering."
        ),
        sources=["yfinance quarterly capex + revenue", "Finnhub company financials",
                 "SEC EDGAR 10-Q filings"],
        trigger_patterns=[],  # pure numeric
        trigger_numeric={
            "msft_capex_revenue_ratio": {"trigger_above": 0.40},
            "googl_capex_revenue_ratio": {"trigger_above": 0.40},
            "meta_capex_revenue_ratio": {"trigger_above": 0.40},
            "amzn_capex_revenue_ratio": {"trigger_above": 0.40},
            "orcl_capex_revenue_ratio": {"trigger_above": 0.40},
        },
        cadence="Quarterly (each 10-Q filing)",
        stitching="Pure numeric — computed in estimates.py from yfinance + Finnhub fallback.",
    ),

    Indicator(
        id="t2_spot_contract_spread",
        tier=2,
        name="Spot/contract spread narrowing or inverted",
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
        name="Capex slashed",
        description="Memory makers announce major capex cuts.",
        what_to_look_for="Any Big Three announces capex reduction of >20% or fab idling.",
        sources=["Press releases", "Yahoo Finance earnings", "Korean press / DART filings"],
        trigger_patterns=[
            "capex reduction", "capex cut", "capital spending reduced",
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
]


# ============================================================
# Company-specific indicators (Indicators 6 and 7) — SK Hynix
# ============================================================
# These are outside the universal tier framework because they're
# specific to SK Hynix risk dynamics, not generalizable cycle signals.

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
    # Indicator 7 — US ADR / SPO launch tracking
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
    "t2_capex_parabolic",   # Hyperscaler capex specifically
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
# Indicator 2 — Hyperscaler capex baselines (self-updating)
# ============================================================
# These seed values are the consensus 2026 capex estimates. Layer 1
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
RSS_FEEDS = {
    # Korean press (existing)
    "Korea Times Business": "https://www.koreatimes.co.kr/www/rss/biz.xml",
    "Yonhap English Business": "https://en.yna.co.kr/RSS/business.xml",
    # Korean press (NEW — closes the gap flagged in v1 audit)
    "KED Global (Korea Economic Daily English)": "https://www.kedglobal.com/rss/all.xml",
    "Korea Herald Business": "https://www.koreaherald.com/rss/020000000000.xml",
    # Memory / semi industry (existing)
    "TrendForce": "https://www.trendforce.com/news/feed",
    "Tom's Hardware RAM": "https://www.tomshardware.com/feeds/all",
    "AnandTech": "https://www.anandtech.com/rss/",
    # AI labs (NEW — covers t1_efficiency_shock TurboQuant-class signal at source)
    "Google Research Blog": "https://research.google/blog/rss/",
    "OpenAI Blog": "https://openai.com/blog/rss.xml",
    "Anthropic News": "https://www.anthropic.com/news/rss.xml",
    "Meta AI Blog": "https://ai.meta.com/blog/rss/",
    # Equipment (NEW — covers t3_wfe_orders_crack)
    "SEMI press releases": "https://www.semi.org/en/news-resources/news-releases/feed",
    # US analyst rationale (NEW — Benzinga free RSS gives the 'why' layer)
    "Benzinga Analyst Ratings": "https://www.benzinga.com/feed",
    # Per-ticker fallback for US (NEW — Investing.com)
    "Investing.com MU": "https://www.investing.com/equities/micron-tech-inc-news/feed/",
    "Investing.com NVDA": "https://www.investing.com/equities/nvidia-corp-news/feed/",
    "Investing.com AMAT": "https://www.investing.com/equities/applied-matls.-news/feed/",
    "Investing.com ASML": "https://www.investing.com/equities/asml-holding-news/feed/",
    # US financial / tech media (NEW — catches MS/GS thematic notes syndicated to Yahoo/Investing.com)
    "Reuters Technology": "https://feeds.reuters.com/reuters/technologyNews",
    "CNBC Technology": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "Seeking Alpha Semiconductors": "https://seekingalpha.com/feed/sectors/technology/semiconductors",
    "Investing.com Asia Equities": "https://www.investing.com/rss/news_301.rss",
    # Asia trade press (NEW — The Elec covers Korean memory deep-dive; Nikkei covers Samsung/Hynix from Japan angle)
    "The Elec English": "https://www.thelec.net/feed/",
    "Nikkei Asia Companies": "https://asia.nikkei.com/rss/feed/section/companies",
    # Pulse by Maeil — moved from scrape target to proper RSS
    "Pulse by Maeil Business": "https://pulse.mk.co.kr/rss/030000000.xml",
    # v3.2 — Architectural sensing feeds (leading indicators for HBM captive ratio shifts)
    # Quality analyst commentary — Damnang covers the memory cycle from a quant/architectural angle
    "Damnang Memory Substack": "https://damnang2.substack.com/feed",
    # Inference framework releases — where research efficiency techniques become production-deployable
    "vLLM Blog": "https://blog.vllm.ai/feed.xml",
    "SGLang / LMSYS Blog": "https://lmsys.org/rss.xml",
    "HuggingFace Blog": "https://huggingface.co/blog/feed.xml",
    # AI lab research (DeepSeek pioneered MLA + KV cache compression — pattern-setter for inference efficiency)
    "DeepSeek API News": "https://api-docs.deepseek.com/news/rss.xml",
    # CXL / memory disaggregation canary — Astera Labs is the pure-play
    "Astera Labs Newsroom": "https://www.asteralabs.com/category/news/feed/",
    # Inference cost benchmarks
    "Artificial Analysis": "https://artificialanalysis.ai/rss",
    # SemiAnalysis free posts (the rest is paywalled but headlines + intros are useful)
    "SemiAnalysis Free": "https://semianalysis.com/feed/",
}

# URLs to scrape directly
SCRAPE_TARGETS = {
    "ChosunBiz English": "https://biz.chosun.com/en/",
    "Pulse News (Maeil Business)": "https://pulse.mk.co.kr/list.php?ctcd=600002&sc=30000020",
    "TrendForce News": "https://www.trendforce.com/news/category/dram",
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
        "ir_url": "https://www.skhynix.com/ir/UI-FR-IR15",
        "transcript_reliability": "low",   # Korean call; English transcript often days late or webcast-only
        "transcript_capable": False,       # v3.32 — IR site is materials/webcast only; Yahoo Finance is the transcript source
        "lang": "ko/en",
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

# Yahoo Finance earnings-call transcripts (v3.32) — the PRIMARY transcript
# source. Verified to carry full call text (incl. Korean+English Q&A) for
# every name in the basket, crucially including the KRX-listed memory makers
# that the IR scrapers and Motley Fool cannot cover. Latest call is free;
# historical is gated behind a Yahoo subscription — fine for a daily brief
# that wants the most recent call.
#
# Coverage verified May 2026: SK Hynix, Samsung, Micron, ASML, TSMC confirmed
# full; US mega-caps (NVDA/MSFT/GOOGL/META/AMZN/ORCL/AVGO/DELL/HPE) expected
# full (same syndication feed, all US-listed English calls).
#
# IMPORTANT: Yahoo syndicates each transcript across many ticker variants
# (SK Hynix also shows as HY9H.F/.DU/.SG; Samsung as SSU.F/SMSN.L/SSNLF;
# etc.). These are the SAME transcript — use ONE canonical ticker per company
# below, do not treat the variants as independent corroboration.
YAHOO_TRANSCRIPT_TICKERS = {
    # Core memory makers — Yahoo is the ONLY working full-transcript source
    # for the two Korean names.
    "SK Hynix": "000660.KS",
    "Samsung Electronics": "005930.KS",
    "Micron": "MU",
    # Wider basket — transcript-fed indicators (HBM4 / capex / WFE / OEM inventory)
    "Nvidia": "NVDA",
    "Broadcom": "AVGO",
    "TSMC": "TSM",
    "Applied Materials": "AMAT",
    "ASML": "ASML",
    "Microsoft": "MSFT",
    "Alphabet": "GOOGL",
    "Meta": "META",
    "Amazon": "AMZN",
    "Oracle": "ORCL",
    "Dell": "DELL",
    "HPE": "HPE",
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
    "Microsoft":         {"exchange": "nasdaq", "ticker": "MSFT"},    # hyperscaler capex
    "Alphabet":          {"exchange": "nasdaq", "ticker": "GOOGL"},   # hyperscaler capex + TPU
    "Meta":              {"exchange": "nasdaq", "ticker": "META"},    # hyperscaler capex + MTIA
    "Amazon":            {"exchange": "nasdaq", "ticker": "AMZN"},    # hyperscaler capex + Trainium
    "Oracle":            {"exchange": "nyse",   "ticker": "ORCL"},    # hyperscaler capex
    "Dell":              {"exchange": "nyse",   "ticker": "DELL"},    # OEM inventory (buyer-side)
    "HPE":               {"exchange": "nyse",   "ticker": "HPE"},     # OEM inventory (buyer-side)
    "ASML":              {"exchange": "nasdaq", "ticker": "ASML"},    # equipment — coverage less consistent; flag will tell us
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
    "Micron memory capex",
    "Samsung memory HBM",
    "DDR5 spot price",
    "TrendForce DRAM contract",
    "hyperscaler 2027 capex",
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
    # Broker + analyst combos (NEW — catches MS/GS/Bernstein thematic notes)
    "Morgan Stanley memory semiconductor",
    "Goldman Sachs SK Hynix Micron",
    "Bernstein semiconductor Rasgon",
    "JPMorgan memory DRAM analyst",
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
    "memory capex mix HBM",
    # AI lab public-listing watch (v3.31 — activate IR scrapers when these fire)
    "OpenAI IPO filing",
    "OpenAI public listing",
    "Anthropic IPO",
    "Anthropic public listing",
]


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
    "SK Hynix", "Hynix",
    "Micron", " MU ",  # spaces to avoid matching "museum" etc
    "Samsung", "Samsung Electronics",
    "ASML",
    "Applied Materials", "AMAT",
    "CXMT", "ChangXin",  # Chinese DRAM (Indicator 6A)
]

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
    # ----- Memory maker earnings (Tier 3 catalysts) -----
    ("2026-06-24", "Earnings", "Micron Q3 FY26 earnings",
     "Single largest catalyst for t3_eps_miss_guidance. Watch beat magnitude (prior 41.86%); first sub-10% beat is signal.",
     "t3_eps_miss_guidance"),
    ("2026-07-29", "Earnings", "SK Hynix Q2 2026 earnings",
     "THE BIG ONE. HBM4 yields, Samsung dynamics, capex guidance, FY27 outlook. First sub-10% beat after Q1 48.8% = peak signal.",
     "t3_eps_miss_guidance"),
    ("2026-07-31", "Earnings", "Samsung Q2 2026 earnings (full results)",
     "Memory segment commentary; HBM4 progress vs SK Hynix.",
     "i6b_samsung_hbm4"),

    # ----- Hyperscaler earnings (Tier 2 catalysts) -----
    ("2026-07-22", "Earnings", "Microsoft Q4 FY26 earnings",
     "2027 capex guidance — bull confirm if above 2026 ($190B); bear catalyst if flat/down.",
     "t2_capex_parabolic"),
    ("2026-07-22", "Earnings", "Alphabet Q2 2026 earnings",
     "2027 capex guidance vs 2026 baseline ($185B).",
     "t2_capex_parabolic"),
    ("2026-07-23", "Earnings", "Meta Q2 2026 earnings",
     "2027 capex guidance. Meta has worst FCF math — most likely first to blink.",
     "t2_capex_parabolic"),
    ("2026-07-24", "Earnings", "Amazon Q2 2026 earnings",
     "2027 capex guidance; AWS infrastructure spend.",
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
HAIKU_MAX_TOKENS = 4000

# Stage 2 — mid-tier reasoning model for tier classification, probability
# synthesis, exec summary composition. Currently resolves to Sonnet 4.6;
# will roll forward. The largest tier (`opus`) would be ~3-5x cost for
# marginal quality gain on this structured task.
SONNET_MODEL = os.environ.get("SONNET_MODEL_OVERRIDE") or "claude-sonnet-4-6"
SONNET_MAX_TOKENS = 8000

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
    "base_case_peak_months_away": 4,
    "base_case_probability_pct": 30,
    "base_case_upside_pct": 18,
    "bull_case_peak_months_away": 12,
    "bull_case_probability_pct": 20,
    "bull_case_upside_pct": 55,
    "bear_case_peak_months_away": 0,
    "bear_case_probability_pct": 25,
    "bear_case_upside_pct": 0,
    "neutral_case_peak_months_away": 7,  # peak Q4 2026 - Q1 2027
    "neutral_case_probability_pct": 25,
    "neutral_case_upside_pct": 25,
}


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
