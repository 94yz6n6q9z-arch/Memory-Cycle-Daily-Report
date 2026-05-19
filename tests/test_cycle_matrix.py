"""
End-to-end smoke test for the cycle matrix (v3.39.7).

Verifies:
  1. HTML parsers correctly extract from TrendForce table structure
  2. CSV upsert helpers are idempotent
  3. Matrix evaluator produces expected verdict from realistic inputs
  4. Idempotency: re-running scrape persistence doesn't grow CSVs

The HTML fixtures below are verbatim from live TrendForce pages captured
2026-05-19. The synthetic signals/forecasts are constructed by hand from
real article content, demonstrating what the Haiku extraction prompts in
src/analysis/llm_summarizer.py would produce.

Run: pytest tests/test_cycle_matrix.py -v
"""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

from src.data.news import (
    SpotScrapeRow, SpotScrapeResult,
    ContractScrapeRow, ContractScrapeResult,
    parse_trendforce_spot_html, parse_trendforce_contract_html,
    persist_spot_to_history, persist_contract_to_history,
    upsert_contract_forecasts, upsert_contract_signals,
)
from src.data.cycle_matrix import (
    evaluate_matrix,
    classify_spot_column, classify_contract_row,
    lookup_cell, MatrixCell,
)


# ============================================================================
# Fixtures
# ============================================================================

SPOT_HTML_FIXTURE = """
<html><body>
<div class="spot-section">
  <h2>DRAM Spot Price</h2>
  <p>Last Update 2026-05-19 18:10 (GMT+8)</p>
  <table>
    <tr><th>Item</th><th>Daily High</th><th>Daily Low</th><th>Session High</th>
        <th>Session Low</th><th>Session Average</th><th>Session Change</th></tr>
    <tr><td>DDR5 16Gb (2Gx8) 4800/5600</td><td>53.00</td><td>29.80</td>
        <td>53.00</td><td>29.80</td><td>41.10</td><td>&#9660; -0.41 %</td></tr>
    <tr><td>DDR5 16Gb (2Gx8) eTT</td><td>24.60</td><td>22.00</td>
        <td>24.60</td><td>22.00</td><td>22.50</td><td>&mdash; 0.00 %</td></tr>
    <tr><td>DDR4 16Gb (2Gx8) 3200</td><td>70.00</td><td>33.50</td>
        <td>70.00</td><td>33.50</td><td>58.905</td><td>&#9650; 0.94 %</td></tr>
    <tr><td>DDR4 8Gb (1Gx8) 3200</td><td>53.00</td><td>16.40</td>
        <td>53.00</td><td>16.40</td><td>32.00</td><td>&mdash; 0.00 %</td></tr>
    <tr><td>DDR3 4Gb 512Mx8 1600/1866</td><td>11.50</td><td>5.50</td>
        <td>11.50</td><td>5.50</td><td>8.988</td><td>&mdash; 0.00 %</td></tr>
  </table>
</div>
</body></html>
"""

CONTRACT_HTML_FIXTURE = """
<html><body>
<div class="contract-section">
  <h2>DRAM Contract Price (2H Mar)</h2>
  <p>Last Update 2026-03-31 14:00 (GMT+8)</p>
  <table>
    <tr><th>Item</th><th>Session High</th><th>Session Low</th>
        <th>Session Average</th><th>Average Change</th><th>Low Change</th></tr>
    <tr><td>DDR5 8GB SO-DIMM</td><td>78.50</td><td>72.50</td>
        <td>75.00</td><td>&mdash; 0.00 %</td><td>&mdash; 0.00 %</td></tr>
    <tr><td>DDR4 16GB SO-DIMM</td><td>175.00</td><td>140.00</td>
        <td>170.00</td><td>&#9650; 3.03 %</td><td>&#9650; 7.69 %</td></tr>
    <tr><td>DDR4 8GB SO-DIMM</td><td>92.00</td><td>70.50</td>
        <td>85.00</td><td>&mdash; 0.00 %</td><td>&mdash; 0.00 %</td></tr>
    <tr><td>DDR4 16Gb 2Gx8</td><td>36.00</td><td>27.00</td>
        <td>29.50</td><td>&mdash; 0.00 %</td><td>&mdash; 0.00 %</td></tr>
  </table>
</div>
</body></html>
"""

DDR5_HISTORICAL_PRICES = [
    ("2026-03-12", 39.40),
    ("2026-04-02", 36.83),
    ("2026-04-08", 37.00),
    ("2026-04-28", 38.67),
    ("2026-04-30", 38.87),
    ("2026-05-05", 38.83),
    ("2026-05-08", 39.00),
    ("2026-05-11", 39.83),
    ("2026-05-12", 40.27),
    ("2026-05-15", 41.17),
]

SYNTHETIC_HAIKU_SIGNALS = [
    {
        "date": "2026-04-30",
        "source_name": "trendforce_press_release",
        "source_url": "https://www.trendforce.com/research/download/RP260430QO",
        "headline": "DRAM Contract Price Apr. 2026",
        "tone_direction": "softening",
        "tone_keywords": "momentum slowed,cooling negotiations,ending the extreme seller's market",
        "segment_mentioned": "pc_dram",
        "qoq_pct_mentioned": None,
        "supplier_mentioned": "",
        "raw_snippet": "PC DRAM contract prices rose this quarter, but momentum slowed.",
    },
    {
        "date": "2026-04-29",
        "source_name": "trendforce_news",
        "source_url": "https://www.trendforce.com/news/2026/04/29/example",
        "headline": "DDR4 Price Cuts Fail to Revive Demand",
        "tone_direction": "cooling",
        "tone_keywords": "price cuts failing to revive demand,buyers cautious",
        "segment_mentioned": "general",
        "qoq_pct_mentioned": None,
        "supplier_mentioned": "",
        "raw_snippet": "Only DDR5 chips saw sporadic buying interest.",
    },
    {
        "date": "2026-04-05",
        "source_name": "electronic_times",
        "source_url": "https://www.etnews.com/samsung-q2-2026-30pct",
        "headline": "Samsung Q2 2026 DRAM Contracts at +30%",
        "tone_direction": "rising",
        "tone_keywords": "30% over Q1 levels",
        "segment_mentioned": "server_ddr5",
        "qoq_pct_mentioned": 30.0,
        "supplier_mentioned": "samsung",
        "raw_snippet": "Samsung finalized Q2 contracts ~+30% over Q1.",
    },
]

SYNTHETIC_HAIKU_FORECASTS = [
    {
        "quarter": "Q1_2026",
        "segment": "conventional_dram_aggregate",
        "low_pct": 55.0, "high_pct": 60.0, "central_pct": 57.5,
        "source": "trendforce_press_release",
        "source_url": "https://www.trendforce.com/presscenter/news/20260105-12860.html",
        "published_date": "2026-01-05",
        "supersedes_id": None,
        "notes": "Initial Q1 2026 forecast",
    },
    {
        "quarter": "Q1_2026",
        "segment": "conventional_dram_aggregate",
        "low_pct": 90.0, "high_pct": 95.0, "central_pct": 92.5,
        "source": "trendforce_press_release",
        "source_url": "https://www.trendforce.com/presscenter/news/20260202-12911.html",
        "published_date": "2026-02-02",
        "supersedes_id": 1,
        "notes": "REVISION up: 55-60% to 90-95%",
    },
    {
        "quarter": "Q2_2026",
        "segment": "conventional_dram_aggregate",
        "low_pct": 58.0, "high_pct": 63.0, "central_pct": 60.5,
        "source": "trendforce_press_release",
        "source_url": "https://www.trendforce.com/presscenter/news/20260331-12995.html",
        "published_date": "2026-03-31",
        "supersedes_id": None,
        "notes": "Initial Q2 2026 forecast",
    },
    {
        "quarter": "Q2_2026",
        "segment": "samsung_specific",
        "low_pct": 28.0, "high_pct": 32.0, "central_pct": 30.0,
        "source": "electronic_times",
        "source_url": "https://www.etnews.com/samsung-q2-2026-30pct",
        "published_date": "2026-04-05",
        "supersedes_id": None,
        "notes": "Samsung Q2 contracts finalized ~+30% over Q1",
    },
]


# ============================================================================
# Tests
# ============================================================================

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Per-test isolated data directory."""
    return tmp_path / "data"


def test_spot_parser_extracts_correct_values():
    result = parse_trendforce_spot_html(SPOT_HTML_FIXTURE)
    assert result.update_date == date(2026, 5, 19)
    assert len(result.rows) == 5
    ddr5 = next(r for r in result.rows if r.sku == "DDR5_16Gb_4800_5600")
    assert ddr5.session_avg == 41.10
    assert ddr5.session_change_pct == -0.41
    assert ddr5.daily_high == 53.00
    assert ddr5.daily_low == 29.80


def test_contract_parser_extracts_correct_values():
    result = parse_trendforce_contract_html(CONTRACT_HTML_FIXTURE)
    assert result.update_date == date(2026, 3, 31)
    assert result.period_label == "2H Mar"
    assert len(result.rows) == 4
    so_dimm = next(r for r in result.rows if r.sku == "DDR5_8GB_SODIMM_contract")
    assert so_dimm.price_usd == 75.0
    assert so_dimm.average_change_pct == 0.0


def test_classify_spot_column_boundaries():
    assert classify_spot_column(0.0) == (1, "At new highs / stable")
    assert classify_spot_column(-2.0) == (2, "Pullback <5% from peak")
    assert classify_spot_column(-7.5) == (3, "5-10% drop from peak")
    assert classify_spot_column(-15.0) == (4, "10-20% drop from peak")
    assert classify_spot_column(-25.0) == (5, ">=20% drop from peak")


def test_classify_contract_row_boundaries():
    assert classify_contract_row(45.0)[0] == 1   # R1
    assert classify_contract_row(25.0)[0] == 2   # R2
    assert classify_contract_row(15.0)[0] == 3   # R3
    assert classify_contract_row(7.5)[0] == 4    # R4
    assert classify_contract_row(2.0)[0] == 5    # R5


def test_matrix_lookup_cells():
    assert lookup_cell(1, 1) == MatrixCell.HOLD
    assert lookup_cell(1, 5) == MatrixCell.FIRE   # any >=20% drop = Fire
    assert lookup_cell(3, 4) == MatrixCell.FIRE
    assert lookup_cell(4, 1) == MatrixCell.FIRE   # R4 all Fire
    assert lookup_cell(5, 1) == MatrixCell.FIRE   # R5 all Fire


def test_persist_spot_idempotent(tmp_data_dir):
    csv_path = tmp_data_dir / "spot.csv"
    result = parse_trendforce_spot_html(SPOT_HTML_FIXTURE)
    n1 = persist_spot_to_history(result, csv_path)
    n2 = persist_spot_to_history(result, csv_path)
    assert n1 == 5
    assert n2 == 5
    # CSV should not have grown
    import pandas as pd
    df = pd.read_csv(csv_path)
    assert len(df) == 5  # Same 5 SKUs, not 10


def test_persist_contract_idempotent(tmp_data_dir):
    csv_path = tmp_data_dir / "contract.csv"
    result = parse_trendforce_contract_html(CONTRACT_HTML_FIXTURE)
    n1 = persist_contract_to_history(result, csv_path)
    n2 = persist_contract_to_history(result, csv_path)
    assert n1 == 4
    assert n2 == 4
    import pandas as pd
    df = pd.read_csv(csv_path)
    assert len(df) == 4


def test_upsert_forecasts_assigns_row_ids(tmp_data_dir):
    csv_path = tmp_data_dir / "forecasts.csv"
    upsert_contract_forecasts(SYNTHETIC_HAIKU_FORECASTS, csv_path)
    import pandas as pd
    df = pd.read_csv(csv_path)
    assert len(df) == 4
    assert "row_id" in df.columns
    assert df["row_id"].is_unique


def test_upsert_signals_dedup_by_url(tmp_data_dir):
    csv_path = tmp_data_dir / "signals.csv"
    upsert_contract_signals(SYNTHETIC_HAIKU_SIGNALS, csv_path)
    upsert_contract_signals(SYNTHETIC_HAIKU_SIGNALS, csv_path)
    import pandas as pd
    df = pd.read_csv(csv_path)
    assert len(df) == 3  # 3 unique URLs, not 6


def test_evaluate_matrix_end_to_end(tmp_data_dir):
    """Hold (R1/C1) is the expected verdict for the May 19, 2026 fixture data."""
    spot_csv = tmp_data_dir / "spot.csv"
    forecasts_csv = tmp_data_dir / "forecasts.csv"
    signals_csv = tmp_data_dir / "signals.csv"

    # Historical backfill
    for d_str, price in DDR5_HISTORICAL_PRICES:
        backfill = SpotScrapeResult(
            update_date=date.fromisoformat(d_str),
            rows=[SpotScrapeRow(
                sku="DDR5_16Gb_4800_5600",
                session_avg=price,
                daily_high=price * 1.05,
                daily_low=price * 0.80,
                session_change_pct=0.0,
            )],
            fresh=False,
        )
        persist_spot_to_history(backfill, spot_csv)

    # Today's scrape
    today_result = parse_trendforce_spot_html(SPOT_HTML_FIXTURE)
    persist_spot_to_history(today_result, spot_csv)

    upsert_contract_forecasts(SYNTHETIC_HAIKU_FORECASTS, forecasts_csv)
    upsert_contract_signals(SYNTHETIC_HAIKU_SIGNALS, signals_csv)

    verdict = evaluate_matrix(
        spot_history=spot_csv,
        contract_forecasts=forecasts_csv,
        contract_signals=signals_csv,
        as_of=datetime(2026, 5, 19, 18, 30),
    )

    # Today's expected verdict per Part 1 design discussion
    assert verdict.cell == MatrixCell.HOLD
    assert verdict.contract_row == 1
    assert verdict.spot_col == 1
    assert verdict.contract.has_anchor
    assert verdict.spot.drawdown_pct >= -1.0  # at or near peak

    # Synthetic should drop from anchor but stay >=30%
    assert verdict.contract.synthetic_central_pct >= 30.0
    assert verdict.contract.synthetic_central_pct <= verdict.contract.anchor_central_pct
