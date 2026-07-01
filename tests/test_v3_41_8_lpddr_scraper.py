"""v3.41.8 — LPDDR contract-direction scraper: stop the false-alarm + widen regex.

Diagnosis (empirical, against live TrendForce text): the scraper was NOT broken.
TrendForce states LPDDR contract *directions* only in its ~quarterly memory-price
outlook releases; between them, recent DRAM articles mention LPDDR with no price
direction — a legitimate `parser_no_match`. The sentinel's threshold of 3 daily
runs assumed near-daily directions and false-alarmed (RuntimeError "regex likely
needs update") for ~6 weeks straight. Two real gaps were also found in the regex:
past-tense verbs ("rose") and range magnitudes ("58-63%", TrendForce's norm).

Fixes:
  - threshold 3 -> 75 (quarter-aware: a quarter is ~63 weekday runs)
  - regex: past-tense direction verbs + range-aware magnitude capture
"""
import pytest

import src.data.news as news


def _dir(text):
    sig, unparsed = news._extract_lpddr_signal_from_body(text, "http://u", "title")
    return sig, unparsed


# ---- regex now catches real TrendForce phrasings ----------------------------

def test_range_magnitude_past_tense():
    # "rose 58-63%" — past-tense verb + range magnitude; previously missed entirely
    sig, _ = _dir("1Q26 LPDDR5X pricing rose 58-63% QoQ as the shortage intensified.")
    assert sig is not None
    assert sig.direction == "rising"
    assert sig.pct_qoq == 58.0  # captures the low end of the range


def test_single_magnitude_canonical():
    sig, _ = _dir("Contract prices for LPDDR4X and LPDDR5X are both expected to surge by around 90% QoQ.")
    assert sig is not None and sig.direction == "rising" and sig.pct_qoq == 90.0


def test_range_surge():
    sig, _ = _dir("LPDDR5X contract prices are expected to surge 90-95% QoQ in 1Q26.")
    assert sig is not None and sig.direction == "rising" and sig.pct_qoq == 90.0


def test_directional_double_no_magnitude():
    sig, _ = _dir("LPDDR5X prices are expected to at least double QoQ in 1Q26.")
    assert sig is not None and sig.direction == "rising" and sig.pct_qoq is None


def test_falling_range():
    sig, _ = _dir("LPDDR4X contract prices are expected to decline 5-10% QoQ in 3Q25.")
    assert sig is not None and sig.direction == "falling" and sig.pct_qoq == -5.0


# ---- correctly does NOT fire on non-direction LPDDR mentions -----------------

def test_passing_mention_is_parser_no_match_not_a_signal():
    # the exact failure sample from the production log — LPDDR mentioned, no direction
    sig, unparsed = _dir("reduced HBM4 volumes are being redirected to HBM3E and server DRAM such as LPDDR, suggesting demand holds.")
    assert sig is None
    assert unparsed  # classified parser_no_match (the normal between-outlook state), not a fabricated direction


def test_revenue_stat_is_not_a_price_direction():
    sig, unparsed = _dir("Winbond expanded shipments of DDR4 and LPDDR4 products, lifting quarterly revenue 91.4% QoQ.")
    assert sig is None and unparsed  # revenue %, not an LPDDR price direction


def test_no_lpddr_keyword_returns_nothing():
    sig, unparsed = _dir("Conventional DDR5 server contract prices rose 90% QoQ on tight supply.")
    assert sig is None and not unparsed  # no LPDDR at all → not even parser_no_match


# ---- the sentinel is now quarter-aware --------------------------------------

def test_sentinel_threshold_is_quarter_aware():
    # 3 daily runs was the false-alarm bug; a quarter is ~63 weekday runs
    assert news.LPDDR_PARSER_NO_MATCH_SENTINEL_THRESHOLD >= 60


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
