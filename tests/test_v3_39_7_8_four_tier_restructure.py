"""Regression tests for v3.39.7.8 — four-tier value-chain restructure.

The v3.39.7.7 three-tier shape (TRACKED_PURE_MEMORY / TRACKED_KEY_CUSTOMERS
/ TRACKED_REST) conflated chip designers (NVIDIA, Broadcom — companies
that BUY memory chips) with ecosystem peers (ASML, TSMC, OEMs) in the
same bucket. v3.39.7.8 restructures per user direction 2026-05-21 into
four explicit value-chain tiers:

  Tier A — Pure memory + rivals    (SK Hynix, Micron, Samsung, CXMT, ChangXin)
  Tier B — Chip designers          (NVIDIA, AMD, Intel, Broadcom, Cerebras)
  Tier C — Hyperscalers + AI labs  (MSFT, GOOGL, AMZN, META, OpenAI, Anthropic)
  Tier D — Ecosystem               (ASML, AMAT, TSMC, Oracle, Dell, HPE)

Also: AMD, Intel, Cerebras added to MOTLEY_FOOL_TICKERS,
YAHOO_TRANSCRIPT_TICKERS, and WIDER_BASKET. CXMT/ChangXin moved up
from Tier C to Tier A (they're competitive rivals in the same product
market, not just ecosystem peers).

Cerebras IPO'd 2026-05-14 on Nasdaq (CBRS); first earnings call ~3
months out — entries added now so the transcript cascade catches it
when it appears.

Tests cover:
  - Config structure (4 tier lists exist, no overlap, union matches
    TRACKED_COMPANIES)
  - Per-tier membership (each name is in the right tier)
  - Chip designer expansion (AMD/INTC/CBRS reach all four config dicts:
    TRACKED_COMPANIES, WIDER_BASKET, MOTLEY_FOOL_TICKERS,
    YAHOO_TRANSCRIPT_TICKERS)
  - Pass 1 acceptance for each tier
  - Pass 1 false-positive resistance (padding still works)
  - Pass 2 rubric structure (4 tiers explicit, NVIDIA scored in Tier B
    not Tier D, CXMT scored in Tier A not Tier D)
"""

from __future__ import annotations


# ============================================================
# Tier structure
# ============================================================

class TestFourTierStructure:
    """The four-tier shape exists and is well-formed."""

    def test_four_tier_lists_exist(self):
        from src.config import (
            TRACKED_PURE_MEMORY,
            TRACKED_CHIP_DESIGNERS,
            TRACKED_HYPERSCALERS_AND_LABS,
            TRACKED_ECOSYSTEM,
        )
        for tier_list in (
            TRACKED_PURE_MEMORY,
            TRACKED_CHIP_DESIGNERS,
            TRACKED_HYPERSCALERS_AND_LABS,
            TRACKED_ECOSYSTEM,
        ):
            assert isinstance(tier_list, list)
            assert len(tier_list) > 0
            for entry in tier_list:
                assert isinstance(entry, str)
                assert len(entry) > 0

    def test_tracked_companies_is_union_of_all_four_tiers(self):
        from src.config import (
            TRACKED_COMPANIES,
            TRACKED_PURE_MEMORY,
            TRACKED_CHIP_DESIGNERS,
            TRACKED_HYPERSCALERS_AND_LABS,
            TRACKED_ECOSYSTEM,
        )
        union = (
            TRACKED_PURE_MEMORY
            + TRACKED_CHIP_DESIGNERS
            + TRACKED_HYPERSCALERS_AND_LABS
            + TRACKED_ECOSYSTEM
        )
        assert TRACKED_COMPANIES == union, (
            "TRACKED_COMPANIES must equal the concatenation of the four "
            "tier lists in order. The fast_tier.py:468 call site relies "
            "on TRACKED_COMPANIES as the merged set."
        )

    def test_no_entry_appears_in_multiple_tiers(self):
        from src.config import (
            TRACKED_PURE_MEMORY,
            TRACKED_CHIP_DESIGNERS,
            TRACKED_HYPERSCALERS_AND_LABS,
            TRACKED_ECOSYSTEM,
        )
        tiers = {
            "Pure memory": set(TRACKED_PURE_MEMORY),
            "Chip designers": set(TRACKED_CHIP_DESIGNERS),
            "Hyperscalers + labs": set(TRACKED_HYPERSCALERS_AND_LABS),
            "Ecosystem": set(TRACKED_ECOSYSTEM),
        }
        for name_a, set_a in tiers.items():
            for name_b, set_b in tiers.items():
                if name_a >= name_b:
                    continue
                overlap = set_a & set_b
                assert not overlap, (
                    f"{name_a} and {name_b} share entries: {overlap}. "
                    "Each name should appear in exactly one tier."
                )


# ============================================================
# Per-tier membership
# ============================================================

class TestTierAMembership:
    """Tier A: pure memory + CXMT/ChangXin (Chinese rivals)."""

    def test_sk_hynix_in_pure_memory(self):
        from src.config import TRACKED_PURE_MEMORY
        assert "SK Hynix" in TRACKED_PURE_MEMORY

    def test_micron_in_pure_memory(self):
        from src.config import TRACKED_PURE_MEMORY
        assert "Micron" in TRACKED_PURE_MEMORY

    def test_samsung_in_pure_memory(self):
        from src.config import TRACKED_PURE_MEMORY
        assert "Samsung" in TRACKED_PURE_MEMORY
        assert "Samsung Electronics" in TRACKED_PURE_MEMORY

    def test_cxmt_moved_to_pure_memory(self):
        """v3.39.7.8 moves CXMT/ChangXin from v3.39.7.7's TRACKED_REST
        into TRACKED_PURE_MEMORY — they're competitive rivals in the
        same product market as SK Hynix, not just ecosystem peers."""
        from src.config import TRACKED_PURE_MEMORY
        assert "CXMT" in TRACKED_PURE_MEMORY
        assert "ChangXin" in TRACKED_PURE_MEMORY

    def test_cxmt_not_in_ecosystem(self):
        """CXMT moved OUT of ecosystem in v3.39.7.8."""
        from src.config import TRACKED_ECOSYSTEM
        assert "CXMT" not in TRACKED_ECOSYSTEM
        assert "ChangXin" not in TRACKED_ECOSYSTEM


class TestTierBMembership:
    """Tier B: chip designers (NVIDIA, AMD, Intel, Broadcom, Cerebras)."""

    def test_nvidia_in_chip_designers(self):
        from src.config import TRACKED_CHIP_DESIGNERS
        assert "NVIDIA" in TRACKED_CHIP_DESIGNERS
        assert "NVDA" in TRACKED_CHIP_DESIGNERS

    def test_amd_in_chip_designers(self):
        """v3.39.7.8 NEW — AMD is a chip designer (MI300X/MI400 HBM
        consumer). Was completely absent pre-v3.39.7.8."""
        from src.config import TRACKED_CHIP_DESIGNERS
        assert "AMD" in TRACKED_CHIP_DESIGNERS

    def test_intel_in_chip_designers(self):
        """v3.39.7.8 NEW — Intel is a chip designer (Gaudi HBM
        consumer). Was completely absent pre-v3.39.7.8."""
        from src.config import TRACKED_CHIP_DESIGNERS
        assert "Intel" in TRACKED_CHIP_DESIGNERS
        assert "INTC" in TRACKED_CHIP_DESIGNERS

    def test_broadcom_moved_to_chip_designers(self):
        """v3.39.7.8 moves Broadcom from v3.39.7.7's TRACKED_REST into
        TRACKED_CHIP_DESIGNERS — AVGO manufactures custom AI silicon
        for Google TPU and Meta MTIA (HBM consumer)."""
        from src.config import TRACKED_CHIP_DESIGNERS
        assert "Broadcom" in TRACKED_CHIP_DESIGNERS
        assert "AVGO" in TRACKED_CHIP_DESIGNERS

    def test_cerebras_in_chip_designers(self):
        """v3.39.7.8 NEW — Cerebras IPO'd 2026-05-14 (CBRS on Nasdaq).
        WSE-3 uses on-chip SRAM (NOT HBM) — meaning Cerebras is an
        HBM-SUBSTITUTION-RISK signal, directly relevant to v3.4
        captive-ratio indicator work."""
        from src.config import TRACKED_CHIP_DESIGNERS
        assert "Cerebras" in TRACKED_CHIP_DESIGNERS
        assert "CBRS" in TRACKED_CHIP_DESIGNERS

    def test_nvidia_not_in_ecosystem(self):
        """NVIDIA moved OUT of ecosystem in v3.39.7.8."""
        from src.config import TRACKED_ECOSYSTEM
        assert "NVIDIA" not in TRACKED_ECOSYSTEM
        assert "NVDA" not in TRACKED_ECOSYSTEM


class TestTierCMembership:
    """Tier C: hyperscalers + AI labs (second-layer bill-payers)."""

    def test_microsoft_in_hyperscalers(self):
        from src.config import TRACKED_HYPERSCALERS_AND_LABS
        assert "Microsoft" in TRACKED_HYPERSCALERS_AND_LABS
        assert "MSFT" in TRACKED_HYPERSCALERS_AND_LABS
        assert "Azure" in TRACKED_HYPERSCALERS_AND_LABS

    def test_alphabet_google_in_hyperscalers(self):
        from src.config import TRACKED_HYPERSCALERS_AND_LABS
        assert "Alphabet" in TRACKED_HYPERSCALERS_AND_LABS
        assert "Google" in TRACKED_HYPERSCALERS_AND_LABS
        assert "GOOGL" in TRACKED_HYPERSCALERS_AND_LABS

    def test_amazon_in_hyperscalers(self):
        from src.config import TRACKED_HYPERSCALERS_AND_LABS
        assert "Amazon" in TRACKED_HYPERSCALERS_AND_LABS
        assert "AMZN" in TRACKED_HYPERSCALERS_AND_LABS

    def test_meta_in_hyperscalers(self):
        from src.config import TRACKED_HYPERSCALERS_AND_LABS
        assert "Meta" in TRACKED_HYPERSCALERS_AND_LABS

    def test_openai_in_hyperscalers(self):
        from src.config import TRACKED_HYPERSCALERS_AND_LABS
        assert "OpenAI" in TRACKED_HYPERSCALERS_AND_LABS
        assert "Open AI" in TRACKED_HYPERSCALERS_AND_LABS

    def test_anthropic_in_hyperscalers(self):
        from src.config import TRACKED_HYPERSCALERS_AND_LABS
        assert "Anthropic" in TRACKED_HYPERSCALERS_AND_LABS


class TestTierDMembership:
    """Tier D: ecosystem (suppliers, foundry, OEMs)."""

    def test_asml_in_ecosystem(self):
        from src.config import TRACKED_ECOSYSTEM
        assert "ASML" in TRACKED_ECOSYSTEM

    def test_amat_in_ecosystem(self):
        from src.config import TRACKED_ECOSYSTEM
        assert "Applied Materials" in TRACKED_ECOSYSTEM
        assert "AMAT" in TRACKED_ECOSYSTEM

    def test_tsmc_in_ecosystem(self):
        from src.config import TRACKED_ECOSYSTEM
        assert "TSMC" in TRACKED_ECOSYSTEM
        assert "TSM" in TRACKED_ECOSYSTEM

    def test_oracle_in_ecosystem(self):
        from src.config import TRACKED_ECOSYSTEM
        assert "Oracle" in TRACKED_ECOSYSTEM
        assert "ORCL" in TRACKED_ECOSYSTEM

    def test_dell_hpe_in_ecosystem(self):
        from src.config import TRACKED_ECOSYSTEM
        assert "Dell" in TRACKED_ECOSYSTEM


# ============================================================
# Chip designer expansion — AMD/INTC/CBRS reach all surfaces
# ============================================================

class TestChipDesignerExpansion:
    """AMD/INTC/CBRS were absent everywhere pre-v3.39.7.8. They must now
    reach all four config surfaces so the news pipeline + price table +
    transcript cascade all cover them."""

    def test_amd_in_wider_basket(self):
        from src.config import WIDER_BASKET
        assert "AMD" in WIDER_BASKET
        assert WIDER_BASKET["AMD"] == "AMD"

    def test_intel_in_wider_basket(self):
        from src.config import WIDER_BASKET
        assert "Intel" in WIDER_BASKET
        assert WIDER_BASKET["Intel"] == "INTC"

    def test_cerebras_in_wider_basket(self):
        from src.config import WIDER_BASKET
        assert "Cerebras" in WIDER_BASKET
        assert WIDER_BASKET["Cerebras"] == "CBRS"

    def test_amd_in_motley_fool_tickers(self):
        from src.config import MOTLEY_FOOL_TICKERS
        assert "AMD" in MOTLEY_FOOL_TICKERS
        assert MOTLEY_FOOL_TICKERS["AMD"]["ticker"] == "AMD"
        assert MOTLEY_FOOL_TICKERS["AMD"]["exchange"] == "nasdaq"

    def test_intel_in_motley_fool_tickers(self):
        from src.config import MOTLEY_FOOL_TICKERS
        assert "Intel" in MOTLEY_FOOL_TICKERS
        assert MOTLEY_FOOL_TICKERS["Intel"]["ticker"] == "INTC"

    def test_cerebras_in_motley_fool_tickers(self):
        from src.config import MOTLEY_FOOL_TICKERS
        assert "Cerebras" in MOTLEY_FOOL_TICKERS
        assert MOTLEY_FOOL_TICKERS["Cerebras"]["ticker"] == "CBRS"

    def test_amd_in_yahoo_transcript_tickers(self):
        from src.config import YAHOO_TRANSCRIPT_TICKERS
        assert "AMD" in YAHOO_TRANSCRIPT_TICKERS
        assert YAHOO_TRANSCRIPT_TICKERS["AMD"] == "AMD"

    def test_intel_in_yahoo_transcript_tickers(self):
        from src.config import YAHOO_TRANSCRIPT_TICKERS
        assert "Intel" in YAHOO_TRANSCRIPT_TICKERS
        assert YAHOO_TRANSCRIPT_TICKERS["Intel"] == "INTC"

    def test_cerebras_in_yahoo_transcript_tickers(self):
        from src.config import YAHOO_TRANSCRIPT_TICKERS
        assert "Cerebras" in YAHOO_TRANSCRIPT_TICKERS
        assert YAHOO_TRANSCRIPT_TICKERS["Cerebras"] == "CBRS"

    def test_all_tickers_includes_new_chip_designers(self):
        """ALL_TICKERS is the merged set used for the price fetch
        (v3.39.7.6 fix). AMD/INTC/CBRS must propagate through."""
        from src.config import ALL_TICKERS
        assert "AMD" in ALL_TICKERS
        assert "Intel" in ALL_TICKERS
        assert "Cerebras" in ALL_TICKERS


# ============================================================
# Pass 1 — keyword filter acceptance for each tier
# ============================================================

def _pass1_matches(headline: str, summary: str = "") -> bool:
    """Replicate the Pass 1 keyword match exactly (post v3.39.7.7
    boundary fix that pads the text with leading + trailing space)."""
    from src.config import TRACKED_COMPANIES
    text = f" {headline} {summary} ".lower()
    company_names = {c.lower() for c in TRACKED_COMPANIES}
    return any(c in text for c in company_names)


class TestPass1Acceptance:
    """Representative headlines for each tier must pass Pass 1."""

    # Tier A
    def test_sk_hynix_earnings_passes(self):
        assert _pass1_matches("SK Hynix Q1 2026 operating profit beats")

    def test_micron_passes(self):
        assert _pass1_matches("Micron raises HBM3E guidance for 2026")

    def test_samsung_passes(self):
        assert _pass1_matches("Samsung Electronics union calls off strike")

    def test_cxmt_passes(self):
        """CXMT moved into Tier A in v3.39.7.8."""
        assert _pass1_matches("CXMT 1688% profit surge stuns DRAM market")

    def test_changxin_passes(self):
        assert _pass1_matches("ChangXin Memory production expansion")

    # Tier B — chip designers
    def test_nvidia_earnings_passes(self):
        assert _pass1_matches(
            "NVIDIA Q1 FY27 data center revenue $35B, Jensen sees AI accelerating"
        )

    def test_amd_mi400_passes(self):
        """AMD was absent pre-v3.39.7.8."""
        assert _pass1_matches("AMD MI400 production ramp doubles HBM3E demand")

    def test_intel_gaudi_passes(self):
        """Intel was absent pre-v3.39.7.8."""
        assert _pass1_matches("Intel Gaudi 3 ships to 4 cloud providers")

    def test_intel_ticker_passes(self):
        assert _pass1_matches("INTC raises 2027 guidance on AI accelerator demand")

    def test_broadcom_passes(self):
        assert _pass1_matches("Broadcom AI segment revenue $5B, custom silicon ramp")

    def test_cerebras_earnings_passes(self):
        """Cerebras was absent pre-v3.39.7.8."""
        assert _pass1_matches(
            "Cerebras IPO debut: WSE-3 wafer-scale chip challenges NVIDIA GPUs"
        )

    def test_cerebras_ticker_passes(self):
        assert _pass1_matches("CBRS stock soars 68% on first trading day")

    # Tier C — hyperscalers + AI labs
    def test_microsoft_azure_passes(self):
        assert _pass1_matches(
            "Microsoft commits $80B Azure AI capex through 2027"
        )

    def test_google_alphabet_passes(self):
        assert _pass1_matches("Alphabet TPU v7 production scales 4x in Q3")

    def test_meta_mtia_passes(self):
        assert _pass1_matches("Meta MTIA v3 to ship 2027 with HBM4")

    def test_amazon_aws_trainium_passes(self):
        assert _pass1_matches("Amazon AWS Trainium 2 displaces 30% of NVIDIA share")

    def test_openai_capex_passes(self):
        assert _pass1_matches("OpenAI signs $5B compute deal with Microsoft Azure")

    def test_anthropic_funding_passes(self):
        assert _pass1_matches("Anthropic raises $5B, builds 1M-H100 cluster")

    # Tier D — ecosystem
    def test_asml_passes(self):
        assert _pass1_matches("ASML EUV backlog grows on memory demand")

    def test_tsmc_cowos_passes(self):
        assert _pass1_matches("TSMC CoWoS capacity expansion bottleneck eases")

    def test_oracle_passes(self):
        assert _pass1_matches("Oracle cloud capex grows 40% on NVIDIA contract")

    def test_dell_passes(self):
        assert _pass1_matches("Dell server inventory rebuild begins")


# ============================================================
# Pass 1 — false-positive resistance (padded keywords)
# ============================================================

class TestPass1FalsePositives:
    """Padded keywords must NOT match common substring false positives."""

    def test_aws_does_not_match_lawsuit(self):
        assert not _pass1_matches("Tech giant files lawsuit over patent dispute")

    def test_aws_does_not_match_jaws(self):
        assert not _pass1_matches("Jaws reboot greenlit by Universal")

    def test_mu_does_not_match_museum(self):
        assert not _pass1_matches("Museum donations rise in Korea")

    def test_mu_does_not_match_amused(self):
        assert not _pass1_matches("Investors amused by retail rally")

    def test_hpe_does_not_match_shape(self):
        assert not _pass1_matches("Shape of recovery debated by economists")

    # Boundary fix (v3.39.7.7) — padded keywords must work at start/end
    def test_mu_at_start_of_headline_matches(self):
        assert _pass1_matches("MU files Q3 10-Q with revised guidance")

    def test_mu_at_end_of_headline_matches(self):
        assert _pass1_matches("Wedbush turns bullish on MU")

    def test_aws_at_start_of_headline_matches(self):
        assert _pass1_matches("AWS announces new Trainium instance type")

    def test_hpe_at_start_of_headline_matches(self):
        assert _pass1_matches("HPE acquires Juniper Networks integration complete")


# ============================================================
# Pass 2 — Haiku rubric structure
# ============================================================

def _get_pass2_prompt_template() -> str:
    """Extract the Pass 2 prompt template by inspecting score_relevance.
    The prompt is constructed inline; we read the source to validate
    structure without needing to actually call the LLM."""
    from pathlib import Path
    return Path("src/data/news.py").read_text()


class TestPass2RubricStructure:
    """Pass 2 Haiku prompt must reflect the 4-tier structure."""

    def test_prompt_names_all_four_tiers(self):
        prompt_src = _get_pass2_prompt_template()
        assert "TIER A — Pure memory" in prompt_src
        assert "TIER B — Chip designers" in prompt_src
        assert "TIER C — Hyperscalers + AI labs" in prompt_src
        assert "TIER D — Ecosystem" in prompt_src

    def test_prompt_lists_cxmt_in_tier_a(self):
        """CXMT/ChangXin must be referenced in Tier A in the prompt."""
        prompt_src = _get_pass2_prompt_template()
        # Find the Tier A block and verify CXMT is named in it
        tier_a_start = prompt_src.find("TIER A — Pure memory")
        tier_b_start = prompt_src.find("TIER B — Chip designers")
        tier_a_block = prompt_src[tier_a_start:tier_b_start]
        assert "CXMT" in tier_a_block, (
            "CXMT must be listed in TIER A block; v3.39.7.8 moved them "
            "out of Tier D (ecosystem) and into Tier A (rivals)."
        )

    def test_prompt_lists_chip_designers_in_tier_b(self):
        prompt_src = _get_pass2_prompt_template()
        tier_b_start = prompt_src.find("TIER B — Chip designers")
        tier_c_start = prompt_src.find("TIER C — Hyperscalers")
        tier_b_block = prompt_src[tier_b_start:tier_c_start]
        for name in ("NVIDIA", "AMD", "Intel", "Broadcom", "Cerebras"):
            assert name in tier_b_block, (
                f"{name} must be listed in TIER B (chip designers) block."
            )

    def test_prompt_lists_hyperscalers_in_tier_c(self):
        prompt_src = _get_pass2_prompt_template()
        tier_c_start = prompt_src.find("TIER C — Hyperscalers")
        tier_d_start = prompt_src.find("TIER D — Ecosystem")
        tier_c_block = prompt_src[tier_c_start:tier_d_start]
        for name in ("Microsoft", "Google", "Amazon", "Meta", "OpenAI", "Anthropic"):
            assert name in tier_c_block, (
                f"{name} must be listed in TIER C (hyperscalers + labs) block."
            )

    def test_prompt_lists_ecosystem_in_tier_d(self):
        prompt_src = _get_pass2_prompt_template()
        tier_d_start = prompt_src.find("TIER D — Ecosystem")
        # Find end of Tier D — next section is "ARCHITECTURAL SHIFTS"
        next_section = prompt_src.find("ARCHITECTURAL SHIFTS", tier_d_start)
        tier_d_block = prompt_src[tier_d_start:next_section]
        for name in ("ASML", "TSMC", "Oracle", "Dell", "HPE"):
            assert name in tier_d_block, (
                f"{name} must be listed in TIER D (ecosystem) block."
            )

    def test_prompt_distinguishes_concurrent_vs_leading_signals(self):
        """Tier B = CONCURRENT HBM demand signal; Tier C = LEADING capex
        commitment signal. Both peak at 8-9 but for different reasons.
        The prompt must make this distinction explicit so Haiku
        understands WHY each tier matters."""
        prompt_src = _get_pass2_prompt_template()
        assert "CONCURRENT HBM demand" in prompt_src
        assert "LEADING" in prompt_src

    def test_prompt_calls_out_cerebras_as_hbm_substitution_signal(self):
        """Cerebras WSE-3 uses on-chip SRAM not HBM — explicitly relevant
        to the v3.4 captive-ratio (HBM substitution) work. The prompt
        should reference this so Haiku doesn't score Cerebras news as
        generic peer commentary."""
        prompt_src = _get_pass2_prompt_template()
        assert "Cerebras" in prompt_src
        # Either "substitution" or "captive-ratio" or similar — let's
        # check for the substitution framing specifically
        assert "substitution" in prompt_src.lower() or "captive-ratio" in prompt_src.lower()
