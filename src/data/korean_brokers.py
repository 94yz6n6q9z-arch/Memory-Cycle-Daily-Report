"""
Korean broker English PDFs — Mirae Asset.

The depth layer behind Naver Finance's structured signals. Naver tells us
WHEN a broker acted; the PDF tells us WHY. 24-48hr lag, 5-25 page
English research notes from Mirae Asset.

  - Mirae Asset Securities (securities.miraeasset.com)
      HTML portal + PDF download. Highest coverage of SK Hynix (typical
      23-page notes). Public; no auth.

The PDF is downloaded, parsed via pdfplumber, and passed to the Layer 1
digester. About 20% of broker PDFs are scanned-image PDFs that need OCR
fallback (tesseract + Korean lang pack) — we mark these and Haiku
processes the OCR text downstream.

Mirae is IMPORTANT criticality — stale cache acceptable on failure.

CHANGELOG (this file):
  v3.39.3.1 (2026-05-18) — Removed NH Investment and Samsung Securities
    sources. Rationale: NH's English research portal is closed
    (nhsec.com — all /research/ paths 404, content moved fully behind
    subscriber paywall mid-2025) so no public scrape is possible; Samsung
    Securities Global requires a registered Gmail+IMAP setup that was
    never configured. Both produced perpetual WARN rows in the email's
    Data Source Assessment with zero items returned. NH's target/rating
    is still captured via WiseReport's cTB24 (Naver path), so removal
    is signal-neutral for Tier 2 scoring; only the analytical text
    behind the call is lost — and NH's text was never available
    publicly anyway. Removed:
      • fetch_nh_notes, fetch_nh_with_retry, NH_PORTAL_BASE,
        NH_RESEARCH_LISTING_EN
      • fetch_samsung_securities_notes,
        fetch_samsung_securities_with_retry,
        SAMSUNG_SECURITIES_GMAIL_USER / _GMAIL_APP_PW / _FROM_FILTER
      • Imports made dead by the deletions: imaplib, email, os
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except ImportError:  # pragma: no cover — pdfplumber added to requirements.txt
    pdfplumber = None

from src.state import fetch_with_retry, Criticality

logger = logging.getLogger("memory_cycle_brief.korean_brokers")

REQUEST_TIMEOUT_SEC = 60  # PDFs are large
POLITE_DELAY_SEC = 5
DOWNLOAD_DIR = Path("./state/cache/broker_pdfs")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class BrokerNote:
    """A parsed Korean broker English research note."""
    broker: str
    ticker_or_topic: str
    title: str
    published_date_iso: str
    page_count: int
    pdf_path: str
    extracted_text: str
    was_scanned_ocr: bool = False  # True if tesseract was needed


def _polite_get(url: str) -> requests.Response:
    time.sleep(POLITE_DELAY_SEC)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; memory-cycle-brief)"}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r


def _extract_pdf_text(pdf_bytes: bytes) -> tuple[str, int, bool]:
    """Extract text from a PDF. Returns (text, page_count, was_scanned)."""
    if pdfplumber is None:
        logger.warning("pdfplumber not installed; PDF extraction unavailable")
        return "", 0, False

    pages_text = []
    page_count = 0
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                t = page.extract_text() or ""
                pages_text.append(t)
    except Exception as e:
        logger.warning(f"pdfplumber failed: {e}")
        return "", 0, False

    full = "\n\n".join(pages_text).strip()
    # Heuristic: if extracted text < 200 chars on a >2-page PDF, treat as scanned.
    was_scanned = page_count > 2 and len(full) < 200
    if was_scanned:
        logger.warning(f"PDF appears to be scanned image ({page_count} pages, "
                       f"{len(full)} chars extracted). OCR fallback not implemented; "
                       f"Haiku will see partial extract only.")
    return full, page_count, was_scanned


# ============================================================
# Mirae Asset Securities
# ============================================================

MIRAE_PORTAL_BASE = "https://securities.miraeasset.com"
MIRAE_RESEARCH_LISTING_EN = f"{MIRAE_PORTAL_BASE}/eng/bbs/board/message/list.do?MENU_ID=2310"


def _find_recent_mirae_pdfs(html: str) -> list[dict]:
    """Parse the Mirae Asset research listing page to find recent PDFs.

    The listing is a table of report titles + dates + PDF download links.
    Mirae redesigns the portal ~1-2x/year; selectors here will need
    occasional maintenance.
    """
    soup = BeautifulSoup(html, "html.parser")
    pdfs = []
    for row in soup.select("table.tb_list tr, table tbody tr"):
        link = row.find("a", href=re.compile(r"\.pdf|fileDown"))
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if href.startswith("/"):
            href = f"{MIRAE_PORTAL_BASE}{href}"
        # Try to extract date from any cell
        date_match = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", row.get_text())
        published_iso = ""
        if date_match:
            y, m, d = date_match.groups()
            published_iso = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        pdfs.append({"title": title, "url": href, "published_date_iso": published_iso})
    return pdfs


def fetch_mirae_notes(days_back: int = 3) -> dict:
    """Discover recent Mirae Asset notes and download their PDFs."""
    out = {"broker": "Mirae Asset Securities", "notes": [],
           "fetched_at_iso": datetime.utcnow().isoformat()}
    try:
        listing = _polite_get(MIRAE_RESEARCH_LISTING_EN).text
        pdfs = _find_recent_mirae_pdfs(listing)
    except Exception as e:
        out["error"] = f"Listing fetch failed: {e}"
        return out

    cutoff_iso = (date.today() - __import__("datetime").timedelta(days=days_back)).isoformat()
    recent = [p for p in pdfs if p["published_date_iso"] >= cutoff_iso] if pdfs else []
    # Filter to memory/SK Hynix relevance
    relevant = [
        p for p in recent
        if any(k.lower() in p["title"].lower()
               for k in ("hynix", "memory", "dram", "hbm", "samsung", "semiconductor"))
    ]
    logger.info(f"Mirae: {len(pdfs)} listings, {len(recent)} recent, {len(relevant)} relevant")

    for p in relevant[:5]:  # cap at 5 per run
        try:
            pdf_resp = _polite_get(p["url"])
            text, pages, was_scanned = _extract_pdf_text(pdf_resp.content)
            safe_title = re.sub(r"[^A-Za-z0-9._-]", "_", p["title"])[:80]
            pdf_path = DOWNLOAD_DIR / f"mirae_{p['published_date_iso']}_{safe_title}.pdf"
            with open(pdf_path, "wb") as f:
                f.write(pdf_resp.content)
            out["notes"].append({
                "title": p["title"],
                "published_date_iso": p["published_date_iso"],
                "url": p["url"],
                "pdf_path": str(pdf_path),
                "page_count": pages,
                "extracted_text": text,
                "was_scanned_ocr": was_scanned,
            })
        except Exception as e:
            logger.warning(f"Mirae PDF fetch failed for {p['url']}: {e}")
    return out


# ============================================================
# Top-level wrappers with retry/cache
# ============================================================

def fetch_mirae_with_retry() -> tuple[Optional[dict], dict]:
    return fetch_with_retry(
        source_id="Mirae Asset (EN)",
        fetch_fn=lambda: fetch_mirae_notes(days_back=3),
        criticality=Criticality.IMPORTANT,
    )

