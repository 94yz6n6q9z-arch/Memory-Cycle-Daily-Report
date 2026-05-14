"""
Naver Finance scraper — Korean broker target prices aggregated.

Single most important source in v2: closes the t2_eps_revisions speed gap
by pulling structured target-price + rating + date data from 20+ Korean
brokers within 30-60 min of publication. Beats yfinance (3-7 day lag) and
Koyfin (~3 day lag from Capital IQ).

Naver Finance is the Korean retail-investor reference platform; its
broker-consensus page aggregates every public Korean broker's research
note on a given KOSPI ticker. This is the data Capital IQ eventually
ingests and Bloomberg/Yahoo eventually surface — we go to the source.

Pages we scrape (per ticker):
  https://finance.naver.com/item/main.naver?code={code}     — overview
  https://finance.naver.com/item/coinfo.naver?code={code}   — consensus
  Companion iframe pages for broker target-price tables

Politeness: 1 request per 5-10 sec per origin (we use 7s). Defensive
selectors with try/except per broker row; missing fields → None, not
crash. CI test asserts at least one row parses for SK Hynix.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

from src.state import fetch_with_retry, Criticality

logger = logging.getLogger("memory_cycle_brief.naver_finance")


# Tickers we actively monitor. Naver uses 6-digit KRX codes.
NAVER_TICKERS = {
    "SK Hynix":            "000660",
    "Samsung Electronics": "005930",
    "SK Square":           "034730",
}

BASE = "https://finance.naver.com"
HEADERS = {
    # Naver requires a normal browser UA + Korean accept-language to render properly.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
}

POLITE_DELAY_SEC = 7  # per-request delay; Naver allows ~1 req per 5-10s
REQUEST_TIMEOUT_SEC = 30


@dataclass
class BrokerAction:
    """A single broker's published rating action on a ticker.

    This is the unit the Layer 2 signal reasoner consumes for t2_eps_revisions.
    """
    ticker_name: str
    ticker_code: str
    broker_korean: str
    broker_english: Optional[str]  # mapped from KOREAN_TO_ENGLISH_BROKER below
    rating: str          # e.g. "매수" (Buy) / "보유" (Hold) / "매도" (Sell)
    rating_english: Optional[str]
    target_price_krw: Optional[int]
    prior_target_krw: Optional[int]  # if Naver shows a delta column
    target_change_pct: Optional[float]
    published_date_iso: str  # YYYY-MM-DD
    direction: Optional[str]  # "raised" | "lowered" | "maintained" | "initiated"
    raw_row_text: str  # fallback if any structured field failed to parse


# Korean broker name → English mapping. Tier 1 first (full English PDFs),
# Tier 2 second (captured via Naver as signal-only).
KOREAN_TO_ENGLISH_BROKER = {
    # Tier 1
    "미래에셋증권":  "Mirae Asset Securities",
    "삼성증권":      "Samsung Securities",
    "NH투자증권":    "NH Investment & Securities",
    # Tier 2
    "한국투자증권":  "Korea Investment & Securities",
    "하나증권":      "Hana Securities",
    "KB증권":        "KB Securities",
    "신한투자증권":  "Shinhan Investment",
    "대신증권":      "Daishin Securities",
    "키움증권":      "Kiwoom Securities",
    "메리츠증권":    "Meritz Securities",
    "유진투자증권":  "Eugene Investment & Securities",
    "현대차증권":    "Hyundai Motor Securities",
    "유안타증권":    "Yuanta Securities",
    "이베스트투자증권": "eBest Investment & Securities",
    "DB금융투자":    "DB Financial Investment",
    "교보증권":      "Kyobo Securities",
    "BNK투자증권":   "BNK Investment & Securities",
    "IBK투자증권":   "IBK Investment & Securities",
}

KOREAN_RATING_TO_ENGLISH = {
    "매수":   "Buy",
    "강력매수": "Strong Buy",
    "적극매수": "Strong Buy",
    "보유":   "Hold",
    "중립":   "Hold",
    "유지":   "Hold",
    "매도":   "Sell",
    "비중확대": "Overweight",
    "비중축소": "Underweight",
}


def _polite_get(url: str) -> requests.Response:
    """GET with the per-request politeness delay and Naver-friendly headers."""
    time.sleep(POLITE_DELAY_SEC)
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    # Naver serves EUC-KR for some pages; rely on requests' apparent_encoding
    r.encoding = r.apparent_encoding or "euc-kr"
    return r


def _parse_broker_consensus_table(html: str, ticker_name: str, ticker_code: str) -> list[BrokerAction]:
    """Parse the broker-consensus table from a Naver coinfo page.

    Defensive: each row parsed in try/except; partial rows kept as
    `raw_row_text` rather than dropped.
    """
    soup = BeautifulSoup(html, "html.parser")
    actions: list[BrokerAction] = []

    # Naver embeds the consensus table inside multiple `table.tb_type1` elements.
    # The exact one we want has columns: broker | rating | target_price | published_date.
    # If Naver redesigns (~1-2x/year), update selectors here and bump the CI test.
    for table in soup.find_all("table", class_=["tb_type1", "tb_type1_ifrs"]):
        for tr in table.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 3:
                continue
            row_text = " | ".join(t.get_text(strip=True) for t in tds)
            # Skip header rows
            if "증권사" in row_text or "투자의견" in row_text or "목표주가" in row_text and "투자의견" in row_text:
                # Could be a header row; the if-elif catches accidental data overlaps
                if "증권사" in tds[0].get_text(strip=True):
                    continue
            try:
                broker_korean = tds[0].get_text(strip=True)
                if not broker_korean or broker_korean.startswith("-"):
                    continue
                rating = tds[1].get_text(strip=True) if len(tds) > 1 else ""
                pt_raw = tds[2].get_text(strip=True) if len(tds) > 2 else ""
                date_raw = tds[3].get_text(strip=True) if len(tds) > 3 else ""

                target_price_krw = None
                if pt_raw:
                    # Naver shows values like "1,370,000" or "137만" — handle both
                    cleaned = pt_raw.replace(",", "").replace("원", "").strip()
                    if "만" in cleaned:
                        cleaned = cleaned.replace("만", "")
                        try:
                            target_price_krw = int(float(cleaned) * 10000)
                        except ValueError:
                            pass
                    else:
                        try:
                            target_price_krw = int(cleaned)
                        except ValueError:
                            pass

                # Normalize date — Naver shows "2026.05.09" or "05.09"
                published_iso = ""
                if date_raw:
                    parts = date_raw.replace("/", ".").replace("-", ".").split(".")
                    if len(parts) == 3:
                        y, m, d = parts
                        if len(y) == 4:
                            published_iso = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                    elif len(parts) == 2:
                        # Naver omits the year for current-year rows
                        m, d = parts
                        published_iso = f"{datetime.utcnow().year}-{m.zfill(2)}-{d.zfill(2)}"

                actions.append(BrokerAction(
                    ticker_name=ticker_name,
                    ticker_code=ticker_code,
                    broker_korean=broker_korean,
                    broker_english=KOREAN_TO_ENGLISH_BROKER.get(broker_korean),
                    rating=rating,
                    rating_english=KOREAN_RATING_TO_ENGLISH.get(rating),
                    target_price_krw=target_price_krw,
                    prior_target_krw=None,  # Naver doesn't expose this on the basic page
                    target_change_pct=None,
                    published_date_iso=published_iso or datetime.utcnow().date().isoformat(),
                    direction=None,
                    raw_row_text=row_text,
                ))
            except Exception as e:
                logger.warning(f"Could not parse Naver row for {ticker_name}: {e}; raw={row_text!r}")
                continue
    return actions


def _fetch_one_ticker(ticker_name: str, ticker_code: str) -> list[BrokerAction]:
    """Fetch and parse the broker-consensus table for one KOSPI ticker."""
    url = f"{BASE}/item/coinfo.naver?code={ticker_code}"
    resp = _polite_get(url)
    actions = _parse_broker_consensus_table(resp.text, ticker_name, ticker_code)
    if not actions:
        # If the embedded iframe approach failed, try the analyst-opinion page directly
        url2 = f"{BASE}/research/company_list.naver?keyword={ticker_code}"
        try:
            resp2 = _polite_get(url2)
            actions = _parse_broker_consensus_table(resp2.text, ticker_name, ticker_code)
        except Exception as e:
            logger.warning(f"Fallback Naver page failed for {ticker_name}: {e}")
    return actions


def fetch_all_naver_actions() -> dict:
    """Fetch broker actions for every ticker in NAVER_TICKERS.

    Returns a JSON-serializable dict ready for the validation table + Haiku digest.
    Wrapped in `fetch_with_retry` at the orchestrator level to handle
    network blips. Per-ticker failure is contained.
    """
    out = {
        "fetched_at_iso": datetime.utcnow().isoformat(),
        "tickers": {},
    }
    for ticker_name, code in NAVER_TICKERS.items():
        try:
            actions = _fetch_one_ticker(ticker_name, code)
            out["tickers"][ticker_name] = {
                "code": code,
                "actions": [
                    {
                        "broker_korean": a.broker_korean,
                        "broker_english": a.broker_english,
                        "rating": a.rating,
                        "rating_english": a.rating_english,
                        "target_price_krw": a.target_price_krw,
                        "published_date_iso": a.published_date_iso,
                        "raw": a.raw_row_text,
                    }
                    for a in actions
                ],
                "n_actions": len(actions),
            }
            logger.info(f"Naver: {ticker_name} → {len(actions)} broker rows")
        except Exception as e:
            out["tickers"][ticker_name] = {
                "code": code,
                "actions": [],
                "n_actions": 0,
                "error": str(e),
            }
            logger.error(f"Naver: {ticker_name} failed: {e}")
    return out


def fetch_naver_with_retry() -> tuple[Optional[dict], dict]:
    """Top-level entry — wrapped in retry/cache policy."""
    return fetch_with_retry(
        source_id="Naver Finance",
        fetch_fn=fetch_all_naver_actions,
        criticality=Criticality.CRITICAL,  # Closes canonical Tier 2 signal
    )
