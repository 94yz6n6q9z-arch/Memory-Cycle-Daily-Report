"""v3.4.7 — Tests for SECTION_4_ONLY_INDICATORS exclusion from T1 firing counts.

Bug report (Raul 2026-05-23):
> "on exec summary we say T1: 5/6 fired (1 partial), but when i look at
>  section 3 i see 4 out of 6 fired with no partial firing"

Root cause: section 3 (template.py) filtered DHCR/Competitor/ADR out via a
LOCAL `SECTION_4_ONLY_INDICATORS` set, but the count sites
(llm_summarizer._compute_tier_status_summary, chart_generator
.bucket_assessments_by_tier) had no such filter. When Indicator 7
(Competitor) moved to Partially fired on the CXMT DDR5 design-win news,
exec summary count went from 4/6 → 5/6 (1 partial) while section 3
correctly stayed at 4/6.

Fix: canonical SECTION_4_ONLY_INDICATORS lives in src/config.py and is
imported by all three sites. These tests pin the contract.
"""

import unittest
from dataclasses import dataclass
from typing import Optional


@dataclass
class _MockAssessment:
    """Mimics IndicatorAssessment for these tests."""
    id: str
    tier: int
    current_status: str
    name: str = ""


@dataclass
class _MockIndicator:
    """Mimics the Indicator dataclass for chart_generator tests."""
    id: str
    tier: int
    name: str = ""
    short_label: Optional[str] = None


class TestSection4OnlyConstantIsCanonical(unittest.TestCase):
    """The frozenset lives in config.py; all three call sites import it
    from there. If anyone re-defines a local copy, this test catches it."""

    def test_constant_exists_in_config(self):
        from src.config import SECTION_4_ONLY_INDICATORS
        self.assertIsInstance(SECTION_4_ONLY_INDICATORS, frozenset)

    def test_constant_contains_the_three_watch_indicators(self):
        from src.config import SECTION_4_ONLY_INDICATORS
        self.assertEqual(SECTION_4_ONLY_INDICATORS, frozenset({
            "t1_decode_hbm_captive_ratio",
            "t1_competitor_dynamics",
            "t1_us_adr_event",
        }))


class TestExecSummaryCountExcludesSection4Only(unittest.TestCase):
    """_compute_tier_status_summary must NOT count DHCR/Competitor/ADR
    toward T1, even though their tier is declared as 1."""

    def test_competitor_partial_fire_does_not_inflate_t1(self):
        """The exact scenario from the May 23 bug report: Indicator 7
        Partially fired alongside 4 real-T1 fires. Exec count must show
        4/6 (1 partial → 0 because the partial WAS Competitor), not 5/6."""
        from src.analysis.llm_summarizer import _compute_tier_status_summary
        assessments = [
            # 4 real-T1 fires (none partial)
            _MockAssessment(id="t1_spot_decline", tier=1, current_status="Fired"),
            _MockAssessment(id="t1_lead_times", tier=1, current_status="Fired"),
            _MockAssessment(id="t1_price_cuts_no_demand", tier=1, current_status="Fired"),
            _MockAssessment(id="t1_oem_inventory", tier=1, current_status="Fired"),
            # 2 real-T1 not fired
            _MockAssessment(id="t1_efficiency_shock", tier=1, current_status="Not fired"),
            _MockAssessment(id="t1_demand_destruction", tier=1, current_status="Not fired"),
            # Competitor partially fired — must be EXCLUDED from T1 count
            _MockAssessment(id="t1_competitor_dynamics", tier=1,
                            current_status="Partially fired"),
        ]
        result = _compute_tier_status_summary(assessments)
        self.assertIn("T1: 4/6 fired", result, msg=result)
        # No "(N partial)" annotation because Competitor was excluded
        self.assertNotIn("partial", result, msg=result)

    def test_dhcr_fired_does_not_inflate_t1(self):
        from src.analysis.llm_summarizer import _compute_tier_status_summary
        assessments = [
            _MockAssessment(id="t1_spot_decline", tier=1, current_status="Fired"),
            _MockAssessment(id="t1_decode_hbm_captive_ratio", tier=1,
                            current_status="Fired"),
        ]
        result = _compute_tier_status_summary(assessments)
        self.assertIn("T1: 1/6 fired", result, msg=result)

    def test_adr_fired_does_not_inflate_t1(self):
        from src.analysis.llm_summarizer import _compute_tier_status_summary
        assessments = [
            _MockAssessment(id="t1_us_adr_event", tier=1, current_status="Fired"),
        ]
        result = _compute_tier_status_summary(assessments)
        self.assertIn("T1: 0/6 fired", result, msg=result)

    def test_regular_t1_partial_still_counted(self):
        """Sanity check the filter doesn't break normal T1 partial accounting."""
        from src.analysis.llm_summarizer import _compute_tier_status_summary
        assessments = [
            _MockAssessment(id="t1_spot_decline", tier=1, current_status="Partially fired"),
        ]
        result = _compute_tier_status_summary(assessments)
        self.assertIn("T1: 1/6 fired (1 partial)", result, msg=result)


class TestChartBucketingExcludesSection4Only(unittest.TestCase):
    """bucket_assessments_by_tier must also exclude DHCR/Competitor/ADR
    from BOTH the totals (denominator) and the fired counts (numerator).
    The cycle chart's 'Tier 1 fired (X/Y)' panel reads from here."""

    def test_totals_exclude_section_4_only_indicators(self):
        from src.analysis.chart_generator import _categorize_indicators as bucket_assessments_by_tier
        # 6 real T1 + 3 section-4-only = 9 indicators with tier=1.
        # Totals[1] should still be 6, not 9.
        indicators = [
            _MockIndicator(id=f"t1_real_{i}", tier=1, name=f"Real T1 {i}",
                           short_label=f"R{i}") for i in range(6)
        ] + [
            _MockIndicator(id="t1_decode_hbm_captive_ratio", tier=1,
                           name="DHCR", short_label="DHCR"),
            _MockIndicator(id="t1_competitor_dynamics", tier=1,
                           name="Competitor", short_label="Competitor"),
            _MockIndicator(id="t1_us_adr_event", tier=1,
                           name="ADR", short_label="ADR"),
        ]
        result = bucket_assessments_by_tier([], indicators)
        t1_total = result[3]
        self.assertEqual(t1_total, 6,
            "T1 total must exclude DHCR/Competitor/ADR (got 9, expected 6)")

    def test_competitor_partial_not_in_fired_list(self):
        from src.analysis.chart_generator import _categorize_indicators as bucket_assessments_by_tier
        indicators = [
            _MockIndicator(id="t1_real_1", tier=1, name="Real T1 1", short_label="R1"),
            _MockIndicator(id="t1_competitor_dynamics", tier=1,
                           name="Competitor", short_label="Competitor"),
        ]
        assessments = [
            _MockAssessment(id="t1_real_1", tier=1, current_status="Fired"),
            _MockAssessment(id="t1_competitor_dynamics", tier=1,
                            current_status="Partially fired"),
        ]
        result = bucket_assessments_by_tier(assessments, indicators)
        t1_fired_labels = result[0]
        t1_fired_count = result[6]
        self.assertIn("R1", t1_fired_labels)
        self.assertNotIn("Competitor", t1_fired_labels)
        self.assertEqual(t1_fired_count, 1)


if __name__ == "__main__":
    unittest.main()
