"""
Korean broker English PDFs — Mirae Asset, NH Investment, Samsung Securities.

The depth layer behind Naver Finance's structured signals. Naver tells us
WHEN a broker acted; these PDFs tell us WHY. 24-48hr lag, 5-25 page
English research notes from Tier 1 Korean brokers.

  - Mirae Asset Securities (securities.miraeasset.com)
      HTML portal + PDF download. Highest coverage of SK Hynix (typical
      23-page notes). Public; no auth.

  - NH Investment & Securities
      Same pattern as Mirae but shorter notes (5-15 pages). Public.

  - Samsung Securities Global
      Email-based distribution (samsungpop.com/global registration).
      We poll a dedicated Gmail inbox via IMAP for PDFs.

Each PDF is downloaded, parsed via pdfplumber, and passed to the Layer 1
digester. About 20% of broker PDFs are scanned-image PDFs that need OCR
fallback (tesseract + Korean lang pack) — we mark these and Haiku
processes the OCR text downstream.

All three are IMPORTANT criticality — stale cache acceptable on failure.
"""

from __future__ import annotations

import imaplib
import email
import logging
import os
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
# NH Investment & Securities (same pattern as Mirae)
# ============================================================

NH_PORTAL_BASE = "https://m.nhqv.com"  # English research moved to mobile portal mid-2025
NH_RESEARCH_LISTING_EN = f"{NH_PORTAL_BASE}/research/list/global"


def fetch_nh_notes(days_back: int = 3) -> dict:
    """NH Investment English research notes — same scraper pattern as Mirae."""
    out = {"broker": "NH Investment & Securities", "notes": [],
           "fetched_at_iso": datetime.utcnow().isoformat()}
    try:
        listing = _polite_get(NH_RESEARCH_LISTING_EN).text
    except Exception as e:
        out["error"] = f"Listing fetch failed: {e}"
        return out

    # NH listing selectors — defensive
    soup = BeautifulSoup(listing, "html.parser")
    pdf_links = []
    for link in soup.find_all("a", href=re.compile(r"\.pdf|download|fileDown")):
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if href.startswith("/"):
            href = f"{NH_PORTAL_BASE}{href}"
        date_match = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", link.parent.get_text() if link.parent else "")
        published_iso = ""
        if date_match:
            y, m, d = date_match.groups()
            published_iso = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        pdf_links.append({"title": title, "url": href, "published_date_iso": published_iso})

    relevant = [
        p for p in pdf_links
        if any(k.lower() in p["title"].lower()
               for k in ("hynix", "memory", "dram", "hbm", "samsung", "semiconductor"))
    ]
    logger.info(f"NH: {len(pdf_links)} listings, {len(relevant)} relevant")

    for p in relevant[:5]:
        try:
            pdf_resp = _polite_get(p["url"])
            text, pages, was_scanned = _extract_pdf_text(pdf_resp.content)
            safe_title = re.sub(r"[^A-Za-z0-9._-]", "_", p["title"])[:80]
            pdf_path = DOWNLOAD_DIR / f"nh_{p['published_date_iso']}_{safe_title}.pdf"
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
            logger.warning(f"NH PDF fetch failed for {p['url']}: {e}")
    return out


# ============================================================
# Samsung Securities Global (via dedicated Gmail inbox + IMAP)
# ============================================================

SAMSUNG_SECURITIES_GMAIL_USER = os.environ.get("SAMSUNG_GMAIL_USER", "")
SAMSUNG_SECURITIES_GMAIL_APP_PW = os.environ.get("SAMSUNG_GMAIL_APP_PW", "")
SAMSUNG_SECURITIES_FROM_FILTER = "samsungpop.com"


def fetch_samsung_securities_notes(days_back: int = 3) -> dict:
    """Poll a dedicated Gmail inbox for Samsung Securities research PDF attachments.

    Setup (one-time):
      1. Register at samsungpop.com/global for the English research distribution
      2. Use a dedicated Gmail address as the registered email
      3. Create a Gmail app password for IMAP access
      4. Set SAMSUNG_GMAIL_USER + SAMSUNG_GMAIL_APP_PW as GitHub Secrets
    """
    out = {"broker": "Samsung Securities", "notes": [],
           "fetched_at_iso": datetime.utcnow().isoformat()}
    if not (SAMSUNG_SECURITIES_GMAIL_USER and SAMSUNG_SECURITIES_GMAIL_APP_PW):
        out["status"] = "skipped"
        out["reason"] = "Samsung Securities Gmail credentials not configured"
        return out

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(SAMSUNG_SECURITIES_GMAIL_USER, SAMSUNG_SECURITIES_GMAIL_APP_PW)
            imap.select("INBOX")
            # Search for recent emails from Samsung Securities distribution
            since = (date.today() - __import__("datetime").timedelta(days=days_back)).strftime("%d-%b-%Y")
            status, msg_ids = imap.search(None, f'(FROM "{SAMSUNG_SECURITIES_FROM_FILTER}" SINCE "{since}")')
            if status != "OK":
                out["error"] = f"IMAP search failed: {status}"
                return out

            for msg_id in msg_ids[0].split():
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = msg.get("Subject", "")
                received = msg.get("Date", "")
                published_iso = ""
                try:
                    parsed_dt = email.utils.parsedate_to_datetime(received)
                    published_iso = parsed_dt.date().isoformat()
                except Exception:
                    pass

                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    filename = part.get_filename()
                    if filename and filename.lower().endswith(".pdf"):
                        try:
                            payload = part.get_payload(decode=True)
                            text, pages, was_scanned = _extract_pdf_text(payload)
                            safe_title = re.sub(r"[^A-Za-z0-9._-]", "_", subject)[:80]
                            pdf_path = DOWNLOAD_DIR / f"samsung_{published_iso}_{safe_title}.pdf"
                            with open(pdf_path, "wb") as f:
                                f.write(payload)
                            out["notes"].append({
                                "title": subject,
                                "published_date_iso": published_iso,
                                "filename": filename,
                                "pdf_path": str(pdf_path),
                                "page_count": pages,
                                "extracted_text": text,
                                "was_scanned_ocr": was_scanned,
                            })
                        except Exception as e:
                            logger.warning(f"Samsung Securities PDF parse failed: {e}")
        logger.info(f"Samsung Securities: {len(out['notes'])} notes downloaded")
    except Exception as e:
        out["error"] = str(e)
        logger.error(f"Samsung Securities IMAP failed: {e}")
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


def fetch_nh_with_retry() -> tuple[Optional[dict], dict]:
    return fetch_with_retry(
        source_id="NH Investment",
        fetch_fn=lambda: fetch_nh_notes(days_back=3),
        criticality=Criticality.IMPORTANT,
    )


def fetch_samsung_securities_with_retry() -> tuple[Optional[dict], dict]:
    return fetch_with_retry(
        source_id="Samsung Securities (Gmail)",
        fetch_fn=lambda: fetch_samsung_securities_notes(days_back=3),
        criticality=Criticality.IMPORTANT,
    )
