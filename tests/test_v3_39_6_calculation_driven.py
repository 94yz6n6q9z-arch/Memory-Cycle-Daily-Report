"""Tests for v3.39.6 — computed_metrics + Layer 2 verdicts threaded into Sonnet.

Replicates the May 18 brief failure where Sonnet's t3_relative_strength prose
read:

    "Today's price action is the most significant development for this
    indicator in the current cycle. Samsung -8.61%, SK Hynix -7.66%,
    Micron -6.54%, AMAT -5.56% all exceeded the >4% single-session
    threshold. ... the 5-day cumulative underperformance window has
    almost certainly crossed the >10% threshold."

That was wrong. The actual computed verdict in fast_tier's
computed_metrics["relative_strength_full"] showed overall_severity=None
(no banner_text was logged in the 18:01 UTC run, meaning the 5-day
spread didn't cross either threshold). Sonnet was reasoning backward
from single-day news headlines because it never received the actual
5-day numbers.

v3.39.6 threads both `signal_reasonings` (22 Layer 2 Haiku verdicts)
and `computed_metrics` (deterministic numerical evaluations) into
Sonnet's prompt, with strict NUMBER-FIRST formatting instructions:
- LEAD with exact computed numbers
- THEN ≤1/3 commentary
- NO speculative "almost certainly past threshold" language
- DEFER to Layer 2 on per-signal status
"""
from __future__ import annotations

import unittest

from src.analysis.llm_summarizer import (
    NewsTriage,
    build_sonnet_analysis_prompt,
)


class _FakeSignalReasoning:
    """Minimal SignalReasoning surrogate — Layer 2 output shape per
    src/analysis/signal_reasoner.py. Only the to_dict() method is needed
    by the prompt builder."""
    def __init__(self, **kw):
        self._d = kw

    def to_dict(self):
        return self._d


class TestComputedMetricsBlock(unittest.TestCase):
    """v3.39.6: computed_metrics is rendered into a COMPUTED METRICS section
    that Sonnet is instructed to quote EXACTLY. Numbers come first; no
    speculation."""

    def _empty_triage(self):
        return NewsTriage(
            relevant_news=[], broker_actions=[],
            trendforce_commentary=[], indicator_relevance={},
        )

    def test_relative_strength_block_with_values_below_threshold(self):
        # The actual May 18 situation: 5-day spreads BELOW threshold (no fire,
        # no yellow). Sonnet's prompt must show these so its prose can quote them.
        computed_metrics = {
            "relative_strength_full": {
                "skhx_5d_vs_sox_pp": -3.21,
                "mu_5d_vs_sox_pp": -2.84,
                "dram_etf_vs_smh": {
                    "spread_pp": -1.10,
                    "dram_return_pct": -2.5,
                    "smh_return_pct": -1.4,
                    "severity": None,
                },
                "overall_severity": None,
                "overall_fired": False,
                "fired_sub_triggers": [],
                "banner_text": None,
            },
        }
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=computed_metrics,
        )
        # The numeric values must appear in the prompt — these are the
        # numbers Sonnet should quote instead of speculating from headlines.
        self.assertIn("SK Hynix 5d vs SOX:", prompt)
        self.assertIn("-3.21pp", prompt)
        self.assertIn("Micron 5d vs SOX:", prompt)
        self.assertIn("-2.84pp", prompt)
        self.assertIn("DRAM ETF 5d vs SMH:", prompt)
        self.assertIn("-1.10pp", prompt)
        # The status must be unambiguous — "None" not "Partially fired"
        self.assertIn("Overall severity:", prompt)
        self.assertIn("None", prompt)
        # The thresholds must be present so Sonnet can correctly characterize
        # the verdict relative to them.
        self.assertIn("red ≤ -10pp", prompt)
        self.assertIn("yellow ≤ -5pp", prompt)

    def test_relative_strength_block_with_red_fire(self):
        # The OTHER scenario — actual fire. The block formatting is identical;
        # only the severity field changes. Asserting both ends of the range.
        computed_metrics = {
            "relative_strength_full": {
                "skhx_5d_vs_sox_pp": -12.40,
                "mu_5d_vs_sox_pp": -11.15,
                "dram_etf_vs_smh": {
                    "spread_pp": -8.20,
                    "dram_return_pct": -9.3,
                    "smh_return_pct": -1.1,
                    "severity": "yellow",
                },
                "overall_severity": "red",
                "overall_fired": True,
                "fired_sub_triggers": [
                    ("red", "SK Hynix 5d -12.40pp vs SOX"),
                    ("red", "Micron 5d -11.15pp vs SOX"),
                    ("yellow", "DRAM ETF 5d -8.20pp vs SMH"),
                ],
                "banner_text": "🚨 t3_relative_strength FIRED (red)",
            },
        }
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=computed_metrics,
        )
        self.assertIn("-12.40pp", prompt)
        self.assertIn("-11.15pp", prompt)
        # Severity must say "red" — Sonnet should not have to guess.
        self.assertIn("red", prompt)
        self.assertIn("Overall fired (red):     True", prompt)
        # Sub-trigger detail surfaces so Sonnet can quote which sub-trigger fired.
        self.assertIn("SK Hynix 5d -12.40pp vs SOX", prompt)

    def test_forward_pe_renders_per_ticker(self):
        computed_metrics = {
            "forward_pe": {
                "000660.KS": 6.8,
                "005930.KS": 11.2,
                "MU": 18.4,
                "NVDA": None,  # missing data — should be skipped
            },
        }
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=computed_metrics,
        )
        self.assertIn("Forward P/E by ticker:", prompt)
        self.assertIn("000660.KS", prompt)
        self.assertIn("6.80", prompt)
        self.assertIn("11.20", prompt)
        self.assertIn("18.40", prompt)
        # NVDA had None — should not appear at all (no orphan rows).
        # We don't make stronger claims here; a regex would be too strict.

    def test_capex_revenue_ratios_render_latest_quarter(self):
        """v3.39.7.9: COMPUTED METRICS block now uses 2-quarter rolling
        per-company mean keys (mean_capex_ocf_ratio_2q + per_company_capex_ocf_2q
        + hyperscaler_quarterly_snapshots). capex/revenue is fully removed
        from the prompt. Test renamed in spirit but kept under the old name
        to preserve grep-ability of v3.39.7.4 → v3.39.7.9 migration history.
        """
        computed_metrics = {
            "hyperscaler_quarterly_snapshots": {
                "MSFT": [
                    # Newest first
                    {"quarter_iso": "2026-Q1", "capex_usd_b": 28.5, "revenue_usd_b": 70.1,
                     "ocf_usd_b": 52.0, "capex_ocf_ratio": 0.55, "fired_threshold": False},
                    {"quarter_iso": "2025-Q4", "capex_usd_b": 22.6, "revenue_usd_b": 65.6,
                     "ocf_usd_b": 48.0, "capex_ocf_ratio": 0.47, "fired_threshold": False},
                ],
                "META": [
                    {"quarter_iso": "2026-Q1", "capex_usd_b": 13.7, "revenue_usd_b": 42.3,
                     "ocf_usd_b": 25.0, "capex_ocf_ratio": 0.55, "fired_threshold": False},
                ],
            },
            # v3.39.7.9 keys: per-company 2-quarter mean, then cross-company mean
            # MSFT 2q mean = (0.55 + 0.47) / 2 = 0.51; META 2q mean = 0.55 (only 1 q)
            # Cross-company mean = (0.51 + 0.55) / 2 = 0.53 (fires >0.50)
            "mean_capex_ocf_ratio_2q": 0.53,
            "per_company_capex_ocf_2q": {"MSFT": 0.51, "META": 0.55},
            "n_companies_with_2q_capex_ocf": 2,
        }
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=computed_metrics,
        )
        # Header explicitly states capex/OCF is the only trigger
        self.assertIn("Hyperscaler CapEx/OCF strain", prompt)
        # Aggregate is surfaced with 2-quarter rolling framing
        self.assertIn("2-quarter rolling", prompt)
        self.assertIn("53.0%", prompt)  # 0.53 formatted as percentage
        self.assertIn("FIRED", prompt)  # band marker at >50%
        # Historical reference cited
        self.assertIn("2014", prompt)
        self.assertIn("2018", prompt)
        self.assertIn("2022", prompt)
        # Per-company 2q mean visible
        self.assertIn("MSFT", prompt)
        self.assertIn("51.0%", prompt)  # MSFT 2q mean
        # Per-company quarterly snapshots present (raw data for narrative)
        self.assertIn("2026-Q1", prompt)
        self.assertIn("28.5", prompt)   # capex for MSFT 2026-Q1
        self.assertIn("META", prompt)
        # capex/OCF is the only metric — capex/revenue should NOT appear
        # in the prompt anymore (v3.39.7.9 removed it)
        self.assertNotIn("capex/rev", prompt)
        self.assertNotIn("capex_revenue_ratio", prompt)
        # Explicit "do not use capex/revenue" instruction present so Layer 2
        # doesn't latch onto stale evidence
        self.assertIn("NOT used", prompt)

    def test_block_says_legacy_mode_when_no_computed_metrics(self):
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            computed_metrics=None,
        )
        # The header is still present, but with a legacy-mode sentinel inside.
        self.assertIn("COMPUTED METRICS", prompt)
        self.assertIn("computed_metrics not threaded", prompt)


class TestLayer2VerdictBlock(unittest.TestCase):
    """v3.39.6: 22 per-signal Haiku verdicts threaded as authoritative source
    of truth for indicator status. Sonnet is instructed to DEFER to these."""

    def _empty_triage(self):
        return NewsTriage(
            relevant_news=[], broker_actions=[],
            trendforce_commentary=[], indicator_relevance={},
        )

    def test_layer2_verdicts_render_per_signal(self):
        reasonings = [
            _FakeSignalReasoning(
                signal_id="t3_relative_strength",
                signal_name="Memory pure-plays underperform SOX",
                tier=3,
                evidence_count=4,
                evidence_sources=["computed_metrics", "performance_data"],
                consensus_direction="bearish",
                convergence="high",
                fired_status="Not fired",  # ← the truthful verdict the May 18 brief got wrong
                fired_status_evidence=[
                    "SK Hynix 5d vs SOX: -3.21pp (below -5pp yellow threshold)",
                    "Micron 5d vs SOX: -2.84pp",
                    "DRAM ETF 5d vs SMH: -1.10pp",
                    "Single-day moves large but window includes positive May 11-12 sessions",
                ],
                evidence_for_change_vs_yesterday="no change — still not fired",
                key_quotes_consolidated=[],
                confidence="high",
                raw_evidence_dump={},
            ),
            _FakeSignalReasoning(
                signal_id="t2_eps_revisions",
                signal_name="Weighted analyst EPS revisions",
                tier=2,
                evidence_count=3,
                evidence_sources=["broker_digests", "naver"],
                consensus_direction="bullish",
                convergence="high",
                fired_status="Fired",
                fired_status_evidence=[
                    "Mirae raised SK Hynix FY27 EPS +22.5%",
                    "Naver consensus FY27 +8% on SK Hynix over 30d",
                ],
                evidence_for_change_vs_yesterday="status flipped from Partially fired",
                key_quotes_consolidated=[],
                confidence="high",
                raw_evidence_dump={},
            ),
        ]
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            signal_reasonings=reasonings,
        )
        # Header present
        self.assertIn("LAYER 2 PER-SIGNAL VERDICTS", prompt)
        # Both signals appear with the right verdicts
        self.assertIn("[t3_relative_strength]", prompt)
        self.assertIn("Not fired", prompt)
        self.assertIn("[t2_eps_revisions]", prompt)
        self.assertIn("Fired", prompt)
        # v3.40.0 input trim: t3 is a DORMANT signal (Not fired + no change), so
        # it renders as a single line and its evidence bullets are dropped (those
        # numbers also live in the COMPUTED METRICS block; Sonnet isn't writing
        # about a dormant signal). The fired/changed t2 keeps full detail.
        self.assertIn("Not fired (unchanged)", prompt)
        self.assertNotIn("SK Hynix 5d vs SOX: -3.21pp", prompt)  # dormant → dropped
        # The FIRED signal keeps confidence + convergence + evidence so Sonnet
        # can weight and quote it.
        self.assertIn("confidence: high", prompt)
        self.assertIn("convergence: high", prompt)
        self.assertIn("Mirae raised SK Hynix FY27 EPS +22.5%", prompt)
        # Change-vs-yesterday surfaces only when there IS a change
        # ("no change" gets suppressed so the prompt doesn't get noisy)
        self.assertNotIn("Change vs yesterday: no change", prompt)
        self.assertIn("status flipped from Partially fired", prompt)

    def test_block_says_legacy_mode_when_no_signal_reasonings(self):
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
            signal_reasonings=None,
        )
        self.assertIn("LAYER 2 PER-SIGNAL VERDICTS", prompt)
        self.assertIn("signal_reasonings not threaded", prompt)


class TestNumberFirstFormattingInstruction(unittest.TestCase):
    """v3.39.6: explicit prompt instruction telling Sonnet to lead with
    numbers and keep commentary ≤1/3 of indicator text."""

    def _empty_triage(self):
        return NewsTriage(
            relevant_news=[], broker_actions=[],
            trendforce_commentary=[], indicator_relevance={},
        )

    def test_number_first_instruction_present(self):
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
        )
        # The instruction header
        self.assertIn("NUMBER-FIRST INDICATOR FORMAT", prompt)
        # The 1/3-words rule
        self.assertIn("1/3", prompt)
        # Anti-speculation rules with specific phrases banned. The prompt
        # text wraps these across lines so we normalize whitespace before
        # asserting — what matters is that the phrases appear as content,
        # not that they're on a single source line.
        import re as _re
        normalized = _re.sub(r"\s+", " ", prompt)
        self.assertIn("almost certainly past threshold", normalized)
        self.assertIn("highly probable when measured", normalized)
        # Defer-to-Layer-2 rule
        self.assertIn("DEFER", prompt)
        self.assertIn("LAYER 2 PER-SIGNAL VERDICTS", prompt)

    def test_example_template_in_instruction_uses_realistic_format(self):
        # The instruction includes an inline example. The example should match
        # the format Sonnet should produce — leading numeric quote then short
        # trend note.
        prompt = build_sonnet_analysis_prompt(
            triage=self._empty_triage(),
            performance_data=[], estimates_data={}, big_movers=[], prior_state={},
        )
        # The example numeric line must appear in the instruction text
        self.assertIn("5-day vs SOX:", prompt)
        # Threshold language
        self.assertIn("yellow threshold -5pp", prompt)
        self.assertIn("red threshold -10pp", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
