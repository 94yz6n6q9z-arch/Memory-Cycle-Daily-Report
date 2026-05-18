"""
Naver Finance broker consensus — via WiseReport (the iframe target).

Background
==========
Until ~Apr/May 2026, broker target prices on KOSPI tickers were rendered
inline on Naver Finance's coinfo page (`finance.naver.com/item/coinfo.naver`)
under `table.tb_type1`. Naver subsequently moved that data into a
third-party iframe served by `navercomp.wisereport.co.kr`, leaving the
coinfo page with only structural placeholders. Our previous scraper
silently returned 0 rows because `tb_type1` no longer existed on the
parent page.

v3.39.3 fetches the WiseReport URL directly (the same URL Naver embeds
in its iframe), parsing two stable tables:

  - `table#cTB15` — consensus summary
    Columns: 투자의견 | 목표주가(원) | EPS(원) | PER(배) | 추정기관수
    Average analyst opinion score, average target price, EPS estimate,
    PER, and the count of estimating institutions.

  - `table#cTB24` — per-broker actions (~25 rows)
    Columns: 제공처 | 최종일자 | 목표가 | 직전목표가 | 변동률(%) |
             투자의견 | 직전투자의견
    Each row is one broker's latest action: target, prior target,
    current rating, prior rating, date. This is the unit Layer 2
    consumes for `t2_eps_revisions` weighted scoring.

The WiseReport response includes prior_target_krw and prior_rating —
fields we previously had to leave as None. This means we can now compute
`direction` ("raised"/"lowered"/"maintained"/"initiated") deterministically
without relying on day-over-day state.

Anti-throttle posture
=====================
WiseReport rate-limits aggressively. The Phase 3 diagnostic observed:
  - request 1 to a host → succeeds
  - request 2-3 within ~3 sec → ConnectTimeout
  - irregular recovery thereafter

We mitigate with:
  1. 12-second inter-request politeness (was 7)
  2. 60-second connect timeout (was 30)
  3. One retry on ConnectTimeout/ReadTimeout with 60-sec backoff
  4. Per-ticker isolation: a failure on ticker N never blocks ticker N+1

Naver's iframe sends `Referer: https://finance.naver.com/`; we mimic that
so the request looks like normal embedded traffic rather than a direct hit.

SK Square (034730) has thin analyst coverage — WiseReport may 404 or
just be slow. The scraper handles this gracefully: empty result for
SK Square does not crash; SK Hynix + Samsung are the priority anyway.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from src.state import fetch_with_retry, Criticality

logger = logging.getLogger("memory_cycle_brief.naver_finance")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAVER_TICKERS = {
    "SK Hynix":            "000660",
    "Samsung Electronics": "005930",
    "SK Square":           "034730",
}

WISEREPORT_BASE = "https://navercomp.wisereport.co.kr"
WISEREPORT_URL_TEMPLATE = WISEREPORT_BASE + "/v2/company/c1010001.aspx?cmp_cd={code}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
    # Naver's iframe naturally sends this — making the request look embedded.
    "Referer": "https://finance.naver.com/",
}

POLITE_DELAY_SEC = 12
REQUEST_TIMEOUT_SEC = 60
RETRY_BACKOFF_SEC = 60


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BrokerAction:
    """A single broker's published rating action on a ticker.

    All fields preserved across the v3.39.3 rewrite; previously-empty
    `prior_target_krw`, `target_change_pct`, and `direction` are now
    populated from the WiseReport response.
    """
    ticker_name: str
    ticker_code: str
    broker_korean: str
    broker_english: Optional[str]
    rating: str
    rating_english: Optional[str]
    target_price_krw: Optional[int]
    prior_target_krw: Optional[int]
    target_change_pct: Optional[float]
    published_date_iso: str
    direction: Optional[str]   # "raised" | "lowered" | "maintained" | "initiated"
    prior_rating: Optional[str]  # NEW in v3.39.3 — WiseReport exposes this directly
    raw_row_text: str


# ---------------------------------------------------------------------------
# Broker name mapping
# ---------------------------------------------------------------------------
#
# WiseReport uses SHORT Korean names ("KB", "하나", "현대차") rather than
# the full form ("KB증권", "하나증권", "현대차증권"). We map both so that
# downstream code can use either as a key.

KOREAN_TO_ENGLISH_BROKER = {
    # Short forms (as they appear in WiseReport)
    "KB":      "KB Securities",
    "하나":     "Hana Securities",
    "현대차":   "Hyundai Motor Securities",
    "BNK투자":  "BNK Investment & Securities",
    "LS":      "LS Securities",
    "키움":     "Kiwoom Securities",
    "미래에셋":  "Mirae Asset Securities",
    "대신":     "Daishin Securities",
    "SK":      "SK Securities",
    "유진투자":  "Eugene Investment & Securities",
    "유안타":   "Yuanta Securities",
    "한국투자":  "Korea Investment & Securities",
    "iM":      "iM Securities",
    "NH투자":   "NH Investment & Securities",
    "DB":      "DB Financial Investment",
    "교보":     "Kyobo Securities",
    "한화투자":  "Hanwha Investment & Securities",
    "신한투자":  "Shinhan Investment",
    "IBK투자":  "IBK Investment & Securities",
    "다올투자":  "Daol Investment & Securities",
    "삼성":     "Samsung Securities",
    "메리츠":   "Meritz Securities",
    "DS투자":   "DS Investment & Securities",
    "상상인":   "Sangsangin Securities",
    "흥국":     "Heungkuk Securities",
    "한양":     "Hanyang Securities",
    "케이프":   "Cape Investment & Securities",

    # Long forms (legacy — preserved for backward compat)
    "미래에셋증권":     "Mirae Asset Securities",
    "삼성증권":         "Samsung Securities",
    "NH투자증권":       "NH Investment & Securities",
    "한국투자증권":     "Korea Investment & Securities",
    "하나증권":         "Hana Securities",
    "KB증권":           "KB Securities",
    "신한투자증권":     "Shinhan Investment",
    "대신증권":         "Daishin Securities",
    "키움증권":         "Kiwoom Securities",
    "메리츠증권":       "Meritz Securities",
    "유진투자증권":     "Eugene Investment & Securities",
    "현대차증권":       "Hyundai Motor Securities",
    "유안타증권":       "Yuanta Securities",
    "DB금융투자":       "DB Financial Investment",
    "교보증권":         "Kyobo Securities",
    "BNK투자증권":      "BNK Investment & Securities",
    "IBK투자증권":      "IBK Investment & Securities",
}


# Rating canonicalization. WiseReport emits a mix of Korean and English
# forms (BUY / Buy / 매수 / STRONG BUY / Outperform / 보유 etc).
KOREAN_RATING_TO_ENGLISH = {
    # Korean
    "매수":        "Buy",
    "강력매수":    "Strong Buy",
    "적극매수":    "Strong Buy",
    "보유":        "Hold",
    "중립":        "Hold",
    "유지":        "Hold",
    "매도":        "Sell",
    "비중확대":    "Overweight",
    "비중축소":    "Underweight",
    # English (already-English entries — normalize casing/spacing)
    "buy":             "Buy",
    "strong buy":      "Strong Buy",
    "hold":            "Hold",
    "neutral":         "Hold",
    "outperform":      "Buy",
    "marketperform":   "Hold",
    "market perform":  "Hold",
    "underperform":    "Underweight",
    "sell":            "Sell",
    "overweight":      "Overweight",
    "underweight":     "Underweight",
}


def normalize_rating(raw: str) -> Optional[str]:
    """Canonicalize a rating string to one of {Strong Buy, Buy, Hold,
    Sell, Overweight, Underweight}. Returns None for unrecognized inputs."""
    if not raw:
        return None
    return KOREAN_RATING_TO_ENGLISH.get(raw) \
        or KOREAN_RATING_TO_ENGLISH.get(raw.lower().strip())


# ---------------------------------------------------------------------------
# HTTP layer — politeness + retry
# ---------------------------------------------------------------------------

def _polite_get_with_retry(url: str) -> requests.Response:
    """GET with the per-request politeness delay and one timeout-retry."""
    time.sleep(POLITE_DELAY_SEC)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        return r
    except (requests.ConnectTimeout, requests.ReadTimeout) as e:
        logger.warning(
            f"WiseReport timeout (1st try) for {url}: {e}; "
            f"backing off {RETRY_BACKOFF_SEC}s and retrying once"
        )
        time.sleep(RETRY_BACKOFF_SEC)
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_krw_int(raw: str) -> Optional[int]:
    """Parse a KRW value like '3,000,000' or '137만' into an int. None on failure."""
    if not raw:
        return None
    cleaned = raw.strip().replace(",", "").replace("원", "")
    if not cleaned or cleaned in ("-", "N/A", "—"):
        return None
    # Handle Korean 만 (10,000)
    if "만" in cleaned:
        cleaned = cleaned.replace("만", "")
        try:
            return int(float(cleaned) * 10000)
        except ValueError:
            return None
    try:
        return int(float(cleaned))  # float() handles "3000000.0" defensively
    except ValueError:
        return None


def _parse_pct(raw: str) -> Optional[float]:
    """Parse a percentage like '57.14' into a float. None on failure."""
    if not raw:
        return None
    cleaned = raw.strip().replace("%", "").replace(",", "")
    if not cleaned or cleaned in ("-", "N/A", "—"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_wisereport_date(raw: str) -> str:
    """Parse a date like '26/05/15' (YY/MM/DD) → '2026-05-15' ISO.

    Falls back to today's date if parsing fails — Layer 2 will still see
    a usable timestamp rather than a None.
    """
    if not raw:
        return datetime.now(timezone.utc).date().isoformat()
    cleaned = raw.strip().replace(".", "/").replace("-", "/")
    parts = cleaned.split("/")
    try:
        if len(parts) == 3:
            yy, mm, dd = parts
            yy = yy.strip()
            year = int(yy) + 2000 if len(yy) == 2 else int(yy)
            return f"{year:04d}-{int(mm):02d}-{int(dd):02d}"
    except (ValueError, IndexError):
        pass
    logger.debug(f"Could not parse WiseReport date {raw!r}; using today")
    return datetime.now(timezone.utc).date().isoformat()


def _compute_direction(target: Optional[int], prior: Optional[int]) -> Optional[str]:
    """Classify a target-price change. Returns 'initiated' when no prior is known."""
    if target is None:
        return None
    if prior is None or prior == 0:
        return "initiated"
    if target > prior:
        return "raised"
    if target < prior:
        return "lowered"
    return "maintained"


def _parse_action_row(cells: list, ticker_name: str, ticker_code: str) -> Optional[BrokerAction]:
    """Parse one <tr> from `table#cTB24` into a BrokerAction. None on failure.

    Expected 7 cells:
      0 제공처 (broker)
      1 최종일자 (date)
      2 목표가 (target price KRW)
      3 직전목표가 (prior target KRW)
      4 변동률% (change %)
      5 투자의견 (current rating)
      6 직전투자의견 (prior rating)
    """
    if len(cells) != 7:
        return None
    raw_texts = [c.get_text(" ", strip=True) for c in cells]
    broker_korean, date_raw, tgt_raw, prior_raw, pct_raw, rating_raw, prior_rating_raw = raw_texts

    if not broker_korean or broker_korean == "제공처":  # header row
        return None

    target_price_krw = _parse_krw_int(tgt_raw)
    prior_target_krw = _parse_krw_int(prior_raw)
    target_change_pct = _parse_pct(pct_raw)
    published_date_iso = _parse_wisereport_date(date_raw)

    return BrokerAction(
        ticker_name=ticker_name,
        ticker_code=ticker_code,
        broker_korean=broker_korean,
        broker_english=KOREAN_TO_ENGLISH_BROKER.get(broker_korean),
        rating=rating_raw,
        rating_english=normalize_rating(rating_raw),
        target_price_krw=target_price_krw,
        prior_target_krw=prior_target_krw,
        target_change_pct=target_change_pct,
        published_date_iso=published_date_iso,
        direction=_compute_direction(target_price_krw, prior_target_krw),
        prior_rating=normalize_rating(prior_rating_raw),
        raw_row_text=" | ".join(raw_texts),
    )


def parse_wisereport_actions(html: str, ticker_name: str, ticker_code: str) -> list[BrokerAction]:
    """Parse `table#cTB24` from WiseReport HTML into a list of BrokerAction.

    Defensive: bad rows are skipped (logged at DEBUG), not crashed on.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="cTB24")
    if not table:
        logger.warning(f"WiseReport: cTB24 table not found for {ticker_name}")
        return []

    out: list[BrokerAction] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        try:
            action = _parse_action_row(cells, ticker_name, ticker_code)
            if action is not None:
                out.append(action)
        except Exception as e:
            logger.debug(f"Skipping malformed WiseReport row for {ticker_name}: {e}")
    return out


def parse_wisereport_summary(html: str) -> dict:
    """Parse `table#cTB15` consensus summary into a dict.

    Returns {} if the table isn't present (gracefully degrades — actions
    can still be used without the summary).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="cTB15")
    if not table:
        return {}

    rows = table.find_all("tr")
    if len(rows) < 2:
        return {}

    # Find the row that contains numeric data (skip header).
    # Layout observed: first <td> is the avg opinion score (a number like "4.00"),
    # followed by avg target, avg EPS, avg PER, and num estimates.
    header_terms = ("투자의견", "목표주가 (원)", "EPS (원)",
                    "목표주가(원)", "EPS(원)", "PER(배)", "PER (배)", "추정기관수")
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        text_cells = [c.get_text(" ", strip=True) for c in cells]
        # Skip rows that contain a header keyword
        if any(h in text_cells for h in header_terms):
            continue
        # Need at least 5 numeric-looking cells
        if len(text_cells) < 5:
            continue
        try:
            return {
                "avg_opinion_score": float(text_cells[0].replace(",", "")),
                "avg_target_price_krw": _parse_krw_int(text_cells[1]),
                "avg_eps_krw": _parse_krw_int(text_cells[2]),
                "avg_per": float(text_cells[3].replace(",", "")) if text_cells[3] else None,
                "num_estimating_institutions": int(text_cells[4].replace(",", "")) if text_cells[4] else None,
            }
        except (ValueError, IndexError):
            continue
    return {}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _fetch_one_ticker(ticker_name: str, ticker_code: str) -> dict:
    """Fetch WiseReport HTML for one ticker and parse both tables.

    Returns a dict with 'actions', 'summary', and 'n_actions'. On HTTP
    failure, returns a dict with 'error' and empty lists — caller decides
    whether to surface as WARN or skip.
    """
    url = WISEREPORT_URL_TEMPLATE.format(code=ticker_code)
    try:
        resp = _polite_get_with_retry(url)
    except Exception as e:
        logger.warning(f"WiseReport: {ticker_name} fetch failed: {e}")
        return {
            "code": ticker_code,
            "actions": [],
            "n_actions": 0,
            "summary": {},
            "error": str(e),
        }

    # WiseReport explicitly serves utf-8; respect content-type.
    resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text

    actions = parse_wisereport_actions(html, ticker_name, ticker_code)
    summary = parse_wisereport_summary(html)

    return {
        "code": ticker_code,
        "actions": [
            {
                "broker_korean": a.broker_korean,
                "broker_english": a.broker_english,
                "rating": a.rating,
                "rating_english": a.rating_english,
                "prior_rating": a.prior_rating,
                "target_price_krw": a.target_price_krw,
                "prior_target_krw": a.prior_target_krw,
                "target_change_pct": a.target_change_pct,
                "direction": a.direction,
                "published_date_iso": a.published_date_iso,
                "raw": a.raw_row_text,
            }
            for a in actions
        ],
        "n_actions": len(actions),
        "summary": summary,
    }


def fetch_all_naver_actions() -> dict:
    """Fetch broker actions for every ticker in NAVER_TICKERS.

    Returns a JSON-serializable dict; per-ticker failure is contained.
    Caller (fast_tier) wraps this in `fetch_with_retry` for stale-cache fallback.
    """
    out = {
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "source": "WiseReport via navercomp.wisereport.co.kr (Naver iframe target)",
        "tickers": {},
    }
    for ticker_name, code in NAVER_TICKERS.items():
        result = _fetch_one_ticker(ticker_name, code)
        out["tickers"][ticker_name] = result
        if result.get("error"):
            logger.error(f"Naver/WiseReport: {ticker_name} → {result['error']}")
        else:
            logger.info(f"Naver/WiseReport: {ticker_name} → {result['n_actions']} broker rows")
    return out


def fetch_naver_with_retry() -> tuple[Optional[dict], dict]:
    """Top-level entry — wrapped in retry/cache policy.

    External interface unchanged from pre-v3.39.3 — callers in
    fast_tier.py do not need to change.
    """
    return fetch_with_retry(
        source_id="Naver Finance",
        fetch_fn=fetch_all_naver_actions,
        criticality=Criticality.CRITICAL,  # Closes canonical Tier 2 signal
    )
