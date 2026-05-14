"""
DART API — Korean Financial Supervisory Service disclosure system.

Free regulatory feed for SK Hynix (000660) and Samsung Electronics
(005930) material disclosures. Korean equivalent of SEC EDGAR.

Covers t2_insider_sales (Korean side), t2_ltas_signed (DART filings
of material contracts), and Indicator 6 (large competitive moves
require regulatory disclosure in Korea).

Free API key registration: opendart.fss.or.kr/uss/umt/EgovMberInsertView.do
Free tier limit: 10,000 calls/day — comfortably within our use.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, date, timedelta
from typing import Optional

import requests

from src.state import fetch_with_retry, Criticality

logger = logging.getLogger("memory_cycle_brief.dart")

DART_API_KEY = os.environ.get("DART_API_KEY", "")
BASE = "https://opendart.fss.or.kr/api"
REQUEST_TIMEOUT_SEC = 15

# Corp codes from DART's corp-code registry (8-digit DART-internal IDs)
# These are stable; loaded once.
DART_CORP_CODES = {
    "SK Hynix":            "00164779",
    "Samsung Electronics": "00126380",
    "SK Square":           "01515323",
}

# Material disclosure types we want
# (DART has dozens of report types; these are the relevant ones)
RELEVANT_REPORT_TYPES = [
    "A",  # 정기공시 (periodic disclosures: annual/quarterly/semi-annual)
    "B",  # 주요사항보고 (material event reports — insider sales, LTAs, M&A)
    "C",  # 발행공시 (issuance disclosures — relevant for ADR/SPO indicator 7)
    "D",  # 지분공시 (ownership disclosures — insider stake changes)
    "I",  # 외부감사관련 (external audit — surprise disclosures here are red flags)
]


def _get(endpoint: str, params: dict) -> dict:
    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY not set in env")
    params = {**params, "crtfc_key": DART_API_KEY}
    r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def fetch_disclosures_for_corp(corp_code: str, days_back: int = 3) -> list[dict]:
    """List recent disclosures for a corp code. Returns raw DART list."""
    today = date.today()
    bgn_de = (today - timedelta(days=days_back)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    all_items: list[dict] = []
    for pblntf_ty in RELEVANT_REPORT_TYPES:
        try:
            resp = _get("list.json", {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": pblntf_ty,
                "page_count": 100,
            })
            if resp.get("status") == "000":
                all_items.extend(resp.get("list", []))
            elif resp.get("status") == "013":
                # 013 = no results — normal, not an error
                pass
            else:
                logger.warning(f"DART {pblntf_ty}: status {resp.get('status')} — {resp.get('message')}")
        except Exception as e:
            logger.warning(f"DART {pblntf_ty} fetch failed: {e}")
    return all_items


def fetch_all_dart() -> dict:
    """Fetch recent DART disclosures across our tracked KRX issuers."""
    out = {
        "fetched_at_iso": datetime.utcnow().isoformat(),
        "tickers": {},
    }
    for name, corp_code in DART_CORP_CODES.items():
        try:
            items = fetch_disclosures_for_corp(corp_code, days_back=3)
            # Filter / categorize for the Haiku digester
            relevant = []
            for item in items:
                report_nm = item.get("report_nm", "")
                # Skip routine quarterly/annual unless mentions material keywords
                is_material = any(kw in report_nm for kw in (
                    "주식양도",       # share transfer
                    "임원변경",       # executive change
                    "단일판매",       # large single sale
                    "공급계약",       # supply contract
                    "공시",          # general disclosure
                    "유상증자",       # paid-in capital increase (~ADR-relevant)
                    "감자",          # capital reduction
                    "합병",          # merger
                ))
                relevant.append({
                    "report_nm": report_nm,
                    "rcept_dt": item.get("rcept_dt"),
                    "rcept_no": item.get("rcept_no"),
                    "flr_nm": item.get("flr_nm"),
                    "is_material": is_material,
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no')}",
                })
            out["tickers"][name] = {
                "corp_code": corp_code,
                "n_disclosures": len(items),
                "n_material": sum(1 for r in relevant if r["is_material"]),
                "disclosures": relevant,
            }
            logger.info(f"DART: {name} → {len(items)} disclosures "
                        f"({sum(1 for r in relevant if r['is_material'])} material)")
        except Exception as e:
            out["tickers"][name] = {"corp_code": corp_code, "error": str(e)}
            logger.error(f"DART: {name} failed: {e}")
    return out


def fetch_dart_with_retry() -> tuple[Optional[dict], dict]:
    if not DART_API_KEY:
        logger.info("DART_API_KEY not set; skipping")
        return None, {"status": "skipped", "reason": "no API key"}
    return fetch_with_retry(
        source_id="DART API",
        fetch_fn=fetch_all_dart,
        criticality=Criticality.IMPORTANT,
    )
