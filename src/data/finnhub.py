"""
Finnhub API integration — US analyst rating actions for Micron and the
broader US semi basket.

Real-time structured feed with broker name attached to each action.
5-30 min latency from broker publish. Free tier 60 calls/min.

Endpoints used:
  /stock/recommendation        — current consensus snapshot per ticker
  /stock/price-target          — target price aggregates (mean/high/low)
  /news?category=company       — company-specific news + analyst commentary
  /stock/insider-transactions  — fallback for Form 4 if EDGAR fails

The recommendation history is a list of monthly snapshots; we diff the
most recent two to detect direction changes. The price-target endpoint
gives mean/high/low so we can spot single-broker outliers (a >-10%
target cut from any one broker is an early-warning even before counts
flip).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests

from src.state import fetch_with_retry, Criticality

logger = logging.getLogger("memory_cycle_brief.finnhub")

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
BASE = "https://finnhub.io/api/v1"
REQUEST_TIMEOUT_SEC = 15

# US tickers we monitor for analyst action — memory + semi peers + hyperscalers
FINNHUB_TICKERS = [
    "MU",     # Micron — primary
    "NVDA",
    "AMAT",
    "ASML",
    "AVGO",
    "TSM",
    "MSFT",
    "GOOGL",
    "META",
    "AMZN",
    "ORCL",
]


def _get(endpoint: str, params: dict[str, Any]) -> Any:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY not set in env")
    params = {**params, "token": FINNHUB_API_KEY}
    r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def fetch_recommendation_trends(ticker: str) -> list[dict]:
    """Monthly consensus snapshots. Most recent two diffed for direction.

    Each row: {period, strongBuy, buy, hold, sell, strongSell, symbol}.
    """
    return _get("stock/recommendation", {"symbol": ticker})


def fetch_price_target(ticker: str) -> dict:
    """Current price-target aggregates: targetMean / targetHigh / targetLow / numberOfAnalysts."""
    return _get("stock/price-target", {"symbol": ticker})


def fetch_company_news(ticker: str, from_date: str, to_date: str) -> list[dict]:
    """Recent company-specific news (analyst commentary, earnings reactions, etc.)."""
    return _get("company-news", {"symbol": ticker, "from": from_date, "to": to_date})


def fetch_insider_transactions(ticker: str) -> dict:
    """Insider transactions — used as redundant fallback for SEC EDGAR Form 4."""
    return _get("stock/insider-transactions", {"symbol": ticker})


def fetch_earnings_calendar_date(ticker: str) -> Optional["date"]:
    """v3.4.10 — Most recent past earnings_date for `ticker`, via Finnhub
    /calendar/earnings, used by the transcript cascade's earnings-window
    gate (decides whether to burn Bright Data on a Yahoo fetch).

    Query window: [today-90d, today+30d]. Returns:
      - The most recent earnings_date that is <= today, OR
      - None if Finnhub has no past earnings in window (e.g., ticker is a
        newly-IPO'd name pre-first-call), or call fails.

    Future-only earnings dates (entries with date > today) are ignored —
    the gate cares only about whether a call has already happened that
    might have a transcript online.

    Raises if FINNHUB_API_KEY isn't set OR the HTTP call fails. Caller is
    expected to catch and fall back to cache or default.
    """
    from datetime import date, timedelta
    today = date.today()
    from_date = (today - timedelta(days=90)).isoformat()
    to_date = (today + timedelta(days=30)).isoformat()
    data = _get("calendar/earnings", {"symbol": ticker, "from": from_date, "to": to_date})
    entries = data.get("earningsCalendar", []) if isinstance(data, dict) else []
    past_dates = []
    for e in entries:
        d_str = e.get("date") if isinstance(e, dict) else None
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str)
        except (ValueError, TypeError):
            continue
        if d <= today:
            past_dates.append(d)
    return max(past_dates) if past_dates else None


def _detect_revision_direction(history: list[dict]) -> Optional[str]:
    """Compare two most recent monthly snapshots to detect net direction.

    Returns "up" / "down" / "flat" / None if insufficient data.
    """
    if len(history) < 2:
        return None
    # Finnhub orders by period ascending — we want most recent two
    sorted_h = sorted(history, key=lambda r: r.get("period", ""))
    prev, curr = sorted_h[-2], sorted_h[-1]

    def buy_weight(r):
        return r.get("strongBuy", 0) * 2 + r.get("buy", 0)
    def sell_weight(r):
        return r.get("strongSell", 0) * 2 + r.get("sell", 0)

    prev_net = buy_weight(prev) - sell_weight(prev)
    curr_net = buy_weight(curr) - sell_weight(curr)
    delta = curr_net - prev_net
    if delta > 0:
        return "up"
    elif delta < 0:
        return "down"
    return "flat"


def fetch_all_finnhub() -> dict:
    """Aggregate analyst-related data for every ticker. JSON-serializable output."""
    from datetime import date, timedelta
    today = date.today()
    from_date = (today - timedelta(days=30)).isoformat()
    to_date = today.isoformat()

    out = {
        "fetched_at_iso": datetime.utcnow().isoformat(),
        "tickers": {},
    }
    for ticker in FINNHUB_TICKERS:
        ticker_data: dict[str, Any] = {"ticker": ticker}
        try:
            history = fetch_recommendation_trends(ticker)
            ticker_data["recommendation_history"] = history
            ticker_data["recent_direction"] = _detect_revision_direction(history)
        except Exception as e:
            ticker_data["recommendation_error"] = str(e)

        try:
            pt = fetch_price_target(ticker)
            ticker_data["price_target"] = pt
        except Exception as e:
            ticker_data["price_target_error"] = str(e)

        try:
            news = fetch_company_news(ticker, from_date, to_date)
            # Trim — we only need recent items for the Haiku triage
            ticker_data["company_news"] = news[:20] if isinstance(news, list) else []
        except Exception as e:
            ticker_data["news_error"] = str(e)

        out["tickers"][ticker] = ticker_data
        logger.info(f"Finnhub: {ticker} → direction={ticker_data.get('recent_direction')}")
    return out


def fetch_finnhub_with_retry() -> tuple[Optional[dict], dict]:
    """Top-level entry — wrapped in retry/cache policy."""
    if not FINNHUB_API_KEY:
        logger.info("FINNHUB_API_KEY not set; skipping")
        return None, {"status": "skipped", "reason": "no API key"}
    return fetch_with_retry(
        source_id="Finnhub",
        fetch_fn=fetch_all_finnhub,
        criticality=Criticality.IMPORTANT,
    )
