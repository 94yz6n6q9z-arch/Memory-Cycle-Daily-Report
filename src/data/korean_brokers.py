"""
Korean broker English research — Mirae Asset (HTML-based, v3.39.4).

The depth layer behind Naver Finance's structured signals. Naver tells us
WHEN a broker acted; the Mirae HTML body tells us WHY.

  - Listing:  https://securities.miraeasset.com/newir/view/pc/en/investor/researchReportsList.jsp
  - Detail:   https://securities.miraeasset.com/newir/view/pc/en/investor/researchReportsView.jsp?messageId={N}

Why HTML, not PDF (rationale from Phase D, 2026-05-18)
=====================================================
Until ~April 2026, Mirae's English research listing exposed PDF download links
on each row, and our scraper pulled the PDFs and ran pdfplumber over them
(with an OCR fallback path for ~20% of notes that were scanned-image PDFs).
The site has since restructured — the listing rows no longer carry direct
PDF hrefs; instead each row has `data-message-id="N"` on its `<a>` tags and a
separate `data-attachment-id` for the file. Mirae renders the full analytical
body server-side on the detail page at `researchReportsView.jsp?messageId=N`,
including author / date / stock code / target price / 600-word body — so we
fetch the detail page HTML directly and skip the PDF round-trip entirely.

Benefits of the HTML path:
  - No `pdfplumber` dependency (removed from requirements.txt in v3.39.4)
  - No OCR fallback needed; HTML body is first-class text every time
  - Faster: a detail page is ~30 KB vs ~3 MB for a typical 23-page PDF
  - The detail page exposes Stock Code, Author, Date, Rating, Target Price
    as discrete fields — we get them without regexing through PDF text

Phase D evidence (2026-05-18, production network)
================================================
Listing fetch from GitHub Actions runner: HTTP 200, 106,541 chars, EUC-KR.
The diagnostic confirmed:
  - 20 unique `data-message-id` values across the page (10 rows × 2 anchor
    types per row — `_sub` for the title, `_main` for the subtitle)
  - 10 unique `data-attachment-id` values (one per row)
  - All row anchors use `href="javascript:void(0);"` — clicks are wired to
    JavaScript that sets a hidden form and submits; we bypass the JS entirely
    by constructing the detail URL ourselves from the message ID
  - No `messageId=N` URL params or `goView()` handlers — they were the
    pre-v3.39.4 hypotheses, both ruled out by Phase D

Sample detail page (messageId=2334442, SK Inc. by Young-gun Kim, Sep 2025):
  Stock Code: 034730 / Rating: Buy / Target: W279,000 (raised from W275,000) /
  ~600-word analytical body across "Recommendation and valuation" and other
  bold-headed sections.

Filtering posture
=================
Mirae publishes English research on ~10 companies per day across many sectors.
We narrow with two complementary filters applied to the LISTING row title
(cheap pre-filter, no detail fetch wasted on irrelevant notes):
  1. Korean ticker code match: `(\\d{6})\\s*KS` against our tracked codes
     (SK Hynix 000660, Samsung Electronics 005930, SK Square 034730).
  2. Keyword match: hynix, memory, dram, hbm, semiconductor. ("samsung" was
     dropped in v3.39.4.1 — false-matched Samsung Fire & Marine etc; Samsung
     Electronics is captured by ticker-match on 005930 anyway.)
A row passing EITHER filter is fetched.

We cap at 5 detail fetches per run (same as pre-v3.39.4) so a busy publishing
day doesn't blow out the deep_tier budget.

Mirae is IMPORTANT criticality — stale cache acceptable on failure.

CHANGELOG (this file):
  v3.39.4.1 (2026-05-18) — Tighten the relevance filter. "samsung" removed from
    TRACKED_TITLE_KEYWORDS after Phase E dry-run output showed it pulling in
    Samsung Fire & Marine (000810, insurance) as a false positive on today's
    listing. The same too-loose keyword would have also caught Samsung SDI
    (006400, batteries), Samsung Biologics (207940, pharma), Samsung SDS
    (018260, IT services), and Samsung Heavy Industries (010140, shipbuilding)
    — none of which belong in a memory-cycle digest. Samsung Electronics
    (the one we DO want) keeps flowing through via ticker-match on its
    005930 KS code; Mirae's title template universally includes the ticker
    parenthetical, so no signal is lost. Net effect: ~1 false-positive
    note/day stripped from the deep_tier output, ~$0.05/day Haiku cost saved,
    digester output stays on-topic.
  v3.39.4   (2026-05-18) — Mirae HTML scraper rewrite. fetch_mirae_notes
    rebuilt against the v3.39.3-era page redesign uncovered by Phase D.
    Selector switched from PDF href regex to `data-message-id` attribute on
    <a> elements inside listing <tr> rows. Detail page fetched directly via
    constructed `researchReportsView.jsp?messageId={N}` URL — no JavaScript
    execution required. Body extraction replaces pdfplumber text extraction.
    New module-level helpers: `_parse_mirae_listing()`, `_fetch_mirae_detail()`,
    `_parse_mirae_detail()`, `MiraeListingRow` dataclass. Helper
    `_extract_pdf_text` and import of `pdfplumber` / `BytesIO` removed.
    Keyword filter unchanged; added Korean ticker code pre-filter for our
    three tracked names. The output dict shape consumed by
    `digester.digest_korean_broker_notes` is preserved — `extracted_text`,
    `title`, `published_date_iso`, `url` all populated identically; `pdf_path`
    now `None`, replaced by `html_path` saved to state/cache/broker_html/.
  v3.39.3.1 (2026-05-18) — Removed NH Investment and Samsung Securities
    sources (closed source / never configured respectively). Both produced
    perpetual WARN rows in the email's Data Source Assessment. NH's
    target/rating still captured via WiseReport's cTB24 (Naver path).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from src.state import fetch_with_retry, Criticality

logger = logging.getLogger("memory_cycle_brief.korean_brokers")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIRAE_PORTAL_BASE = "https://securities.miraeasset.com"
MIRAE_RESEARCH_LISTING_EN = (
    f"{MIRAE_PORTAL_BASE}/newir/view/pc/en/investor/researchReportsList.jsp"
)
MIRAE_RESEARCH_DETAIL_EN_TEMPLATE = (
    f"{MIRAE_PORTAL_BASE}/newir/view/pc/en/investor/researchReportsView.jsp?messageId={{message_id}}"
)

REQUEST_TIMEOUT_SEC = 60
POLITE_DELAY_SEC = 5  # between successive HTTP requests
MAX_DETAIL_FETCHES_PER_RUN = 5  # same cap as pre-v3.39.4

# v3.39.4: tracked Korean ticker codes for the pre-filter. These match the
# `(\d{6})\s*KS` pattern Mirae uses in row titles, e.g. "(034730 KS/Buy)".
# Keep in sync with NAVER_TICKERS in src/data/naver_finance.py.
TRACKED_KOREAN_TICKER_CODES = {
    "000660",  # SK Hynix
    "005930",  # Samsung Electronics
    "034730",  # SK Square / SK Inc.
}

# Keyword filter on row titles. Catches industry/sector notes that don't carry
# one of our tracked tickers in the title but cover memory broadly. Lowercased
# for case-insensitive matching.
# v3.39.4.1: "samsung" removed — it false-matched Samsung Fire & Marine
# (insurance, 000810), Samsung SDI (batteries, 006400), Samsung Biologics
# (pharma, 207940), etc. Samsung Electronics (the one we DO want) is captured
# by ticker-match on 005930 instead, since Mirae's title template always
# includes "(005930 KS/Rating)". See Phase E dry-run output 2026-05-18 17:22 UTC.
TRACKED_TITLE_KEYWORDS = (
    "hynix", "memory", "dram", "hbm", "semiconductor",
)

# Headers: same UA pattern as src/data/naver_finance.py — Mirae returned 200
# on all 3 Phase C fetches and the Phase D run, so no extra spoofing needed.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.6",
}

# Where to save raw HTML for debugging. Not consumed by the digester (it reads
# `extracted_text` from the in-memory note dict) but useful for forensic
# inspection when something parses weirdly.
HTML_CACHE_DIR = Path("./state/cache/broker_html")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BrokerNote:
    """Kept for backward compat with code that imports the symbol. The
    deep_tier doesn't use this dataclass directly — it consumes the dict
    output of fetch_mirae_notes — but a few callers reference the type."""
    broker: str
    ticker_or_topic: str
    title: str
    published_date_iso: str
    page_count: int
    pdf_path: str
    extracted_text: str
    was_scanned_ocr: bool = False


@dataclass
class MiraeListingRow:
    """One <tr> from the Mirae English research listing page."""
    message_id: str             # e.g. "2339922" — drives detail URL
    attachment_id: Optional[str]  # e.g. "2144703" — present on most rows
    category: str               # e.g. "Company Analysis" / "Industry Analysis"
    title: str                  # e.g. "SK Inc. (034730 KS/Buy)"
    subtitle: str               # e.g. "Ample upside potential"
    author: str                 # e.g. "Young-gun Kim"
    published_date_iso: str     # ISO date parsed from "05.18.2026" cell
    ticker_code_korean: Optional[str]  # 6-digit code extracted from title; None if not present


# ---------------------------------------------------------------------------
# HTTP helper — polite GET with shared encoding handling
# ---------------------------------------------------------------------------

def _polite_get(url: str) -> requests.Response:
    """GET with the inter-request politeness delay and Mirae-correct encoding."""
    time.sleep(POLITE_DELAY_SEC)
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    # Mirae serves text/html; charset=euc-kr. apparent_encoding picks this up
    # reliably via chardet; falling back to utf-8 would mangle Korean.
    r.encoding = r.apparent_encoding or "euc-kr"
    return r


# ---------------------------------------------------------------------------
# Listing parser
# ---------------------------------------------------------------------------

def _parse_mirae_date_cell(raw: str) -> str:
    """Parse Mirae's listing date format `MM.DD.YYYY` → ISO `YYYY-MM-DD`.
    Falls back to today's date if parsing fails — caller will surface the
    note with a usable date rather than None."""
    cleaned = (raw or "").strip()
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$", cleaned)
    if m:
        mm, dd, yy = m.groups()
        year = int(yy) + 2000 if len(yy) == 2 else int(yy)
        try:
            return f"{year:04d}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            pass
    logger.debug(f"Could not parse Mirae listing date {raw!r}; using today")
    return date.today().isoformat()


def _extract_korean_ticker_from_title(title: str) -> Optional[str]:
    """Pull the 6-digit Korean ticker code out of a title like
    'SK Inc. (034730 KS/Buy)'. Returns None if no match."""
    m = re.search(r"\((\d{6})\s*KS[/)]", title)
    return m.group(1) if m else None


def _parse_mirae_listing(html: str) -> list[MiraeListingRow]:
    """Parse the listing HTML and return a list of MiraeListingRow.

    Defensive: malformed rows are skipped (logged at DEBUG), never crashed on.
    Empty list is a legitimate result if the page returns content we don't
    recognize — caller decides whether to surface as WARN.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[MiraeListingRow] = []

    for tr in soup.find_all("tr"):
        # Skip header rows / rows without the message-id anchor pattern.
        sub_anchor = tr.find("a", attrs={"data-message-id": True, "id": re.compile(r"_sub$")})
        if not sub_anchor:
            continue

        try:
            message_id = sub_anchor["data-message-id"].strip()
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 4:
                continue

            # td[0] = date; td[1] = category; td[2] = title/subtitle box; td[3] = author; td[4] = file
            date_raw = tds[0].get_text(strip=True)
            published_iso = _parse_mirae_date_cell(date_raw)

            category = tds[1].get_text(" ", strip=True)

            # Title is inside the first anchor's <strong>; subtitle is in the
            # second anchor's <p class="ellipsis">.
            title_strong = sub_anchor.find("strong")
            title = title_strong.get_text(" ", strip=True) if title_strong else sub_anchor.get_text(" ", strip=True)

            main_anchor = tr.find("a", attrs={"data-message-id": message_id, "id": re.compile(r"_main$")})
            subtitle = ""
            if main_anchor:
                subtitle_p = main_anchor.find("p")
                subtitle = subtitle_p.get_text(" ", strip=True) if subtitle_p else main_anchor.get_text(" ", strip=True)

            author = tds[3].get_text(" ", strip=True) if len(tds) > 3 else ""

            attachment_anchor = tr.find("a", attrs={"data-attachment-id": True})
            attachment_id = attachment_anchor["data-attachment-id"].strip() if attachment_anchor else None

            ticker_code = _extract_korean_ticker_from_title(title)

            rows.append(MiraeListingRow(
                message_id=message_id,
                attachment_id=attachment_id,
                category=category,
                title=title,
                subtitle=subtitle,
                author=author,
                published_date_iso=published_iso,
                ticker_code_korean=ticker_code,
            ))
        except Exception as e:
            logger.debug(f"Skipping malformed Mirae listing row: {e}")
            continue

    return rows


def _row_is_relevant(row: MiraeListingRow) -> bool:
    """v3.39.4 pre-filter: keep rows that match EITHER a tracked Korean ticker
    code OR one of the topic keywords. Cheap; runs before any detail fetch."""
    if row.ticker_code_korean and row.ticker_code_korean in TRACKED_KOREAN_TICKER_CODES:
        return True
    haystack = f"{row.title} {row.subtitle}".lower()
    return any(k in haystack for k in TRACKED_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

def _parse_mirae_kst_date(raw: str) -> str:
    """Parse a date like 'Mon Sep 08 00:00:00 KST 2025' → ISO 'YYYY-MM-DD'.
    Falls back to today's date if parsing fails."""
    cleaned = (raw or "").strip()
    # Strip the timezone label since strptime handles 'KST' poorly across platforms.
    cleaned_no_tz = re.sub(r"\s+KST\s+", " ", cleaned)
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned_no_tz, fmt).date().isoformat()
        except ValueError:
            continue
    logger.debug(f"Could not parse Mirae detail date {raw!r}; using today")
    return date.today().isoformat()


def _parse_mirae_detail(html: str) -> dict:
    """Parse a Mirae English research detail page.

    Returns a dict with keys:
      - title           : e.g. 'SK Inc. (034730 KS/Buy) Ample upside potential'
      - category        : e.g. 'Company Analysis'
      - author          : e.g. 'Young-gun Kim'
      - published_date_iso  : ISO date parsed from the 'Date' field
      - stock_code      : e.g. '034730' (None if absent)
      - rating          : e.g. 'Buy' (extracted from title, None if absent)
      - target_price_krw : int or None (regex against body)
      - body_text       : full analytical body, newlines preserved

    Missing fields degrade to "" (string fields) or None (numeric/stock_code).
    """
    soup = BeautifulSoup(html, "html.parser")

    out = {
        "title": "",
        "category": "",
        "author": "",
        "published_date_iso": date.today().isoformat(),
        "stock_code": None,
        "rating": None,
        "target_price_krw": None,
        "body_text": "",
    }

    # ---- Title (h1/h2/h3 with the "(NNNNNN KS/Rating)" pattern) ----
    title_re = re.compile(r"\(\s*\d{6}\s*[A-Z]{2}\s*/\s*\w[\w\s]*?\s*\)")
    for header in soup.find_all(["h1", "h2", "h3"]):
        text = header.get_text(" ", strip=True)
        if title_re.search(text):
            out["title"] = text
            break

    # ---- Category (the "Company Analysis" / "Industry Analysis" label) ----
    for candidate in soup.find_all(["p", "h2", "h3", "div"]):
        text = candidate.get_text(" ", strip=True)
        if text in ("Company Analysis", "Industry Analysis", "Sector Note",
                    "Strategy", "Economy", "Fixed Income", "Derivatives"):
            out["category"] = text
            break

    # ---- Metadata block: Author / Date / Stock Code ----
    # The page renders each label as <strong>Author</strong> followed by the
    # value as a sibling text node or adjacent element. Walk every <strong>
    # and consume the following text up to the next <strong> / block boundary.
    for strong in soup.find_all("strong"):
        label = strong.get_text(" ", strip=True)
        if label not in ("Author", "Date", "Stock Code"):
            continue
        value_parts = []
        for sib in strong.next_siblings:
            if getattr(sib, "name", None) == "strong":
                break
            text = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
            if text:
                value_parts.append(text)
        value = " ".join(value_parts).strip()
        if label == "Author":
            out["author"] = value
        elif label == "Date":
            out["published_date_iso"] = _parse_mirae_kst_date(value)
        elif label == "Stock Code":
            m = re.search(r"\d{6}", value)
            if m:
                out["stock_code"] = m.group(0)

    # ---- Rating: pulled from the title parenthetical "(034730 KS/Buy)" ----
    if out["title"]:
        m = re.search(r"\(\s*\d{6}\s*[A-Z]{2}\s*/\s*([\w\s]+?)\s*\)", out["title"])
        if m:
            out["rating"] = m.group(1).strip()

    # ---- Body text ----
    body_text = _extract_largest_text_block(soup)
    out["body_text"] = body_text

    # ---- Target price (regex against body) ----
    # Common phrasings observed across real Mirae notes (validated against
    # SK Hynix Oct 30 2025 + May 11 2026, SK Inc. Sep 8 2025):
    #   "We maintain our target price for SK Hynix at W2,700,000"
    #   "We raise our target price for SK Hynix by 30.7% to W680,000"
    #   "lower our target price to W137,000"
    #   "target price of W2,700,000"
    #   "12-month TP of W1,370,000"
    # We allow ANY chars (lazy) up to 40 between trigger and W{number}, since
    # raise-by-X% / lower-by-X% phrasings put digits BETWEEN the trigger and
    # the new TP. \bTP\b avoids false positives against ASP/OTP/GTP.
    # Use the FIRST match — Mirae always states the *new* TP up front.
    tp_match = re.search(
        r"(?:target price|\bTP\b)[\s\S]{1,40}?W\s*([\d,]+)",
        body_text, re.IGNORECASE,
    )
    if tp_match:
        digits = tp_match.group(1).replace(",", "")
        try:
            out["target_price_krw"] = int(digits)
        except ValueError:
            pass

    return out


def _extract_largest_text_block(soup: BeautifulSoup) -> str:
    """Find the <div>/<article>/<section> containing the most text and return
    its text content. Mirae's template puts the analytical body inside a
    content container; we don't depend on a specific class name since those
    churn. Skips wrappers (containers with many descendant tags) so the
    selected block is content, not chrome."""
    candidates = []
    for tag in soup.find_all(["article", "div", "section"]):
        # Skip wrappers that contain other large blocks — we want a LEAF-ish
        # container that holds the bulk of the article text. Tags with >80
        # descendant tags are almost always wrappers, not content blocks.
        if len(tag.find_all(True)) > 80:
            continue
        text = tag.get_text("\n", strip=True)
        if len(text) > 300:
            candidates.append((len(text), text))
    if not candidates:
        # Last-resort: dump the body, stripped
        body = soup.find("body")
        return body.get_text("\n", strip=True) if body else ""
    candidates.sort(reverse=True)
    body_text = candidates[0][1]
    # Trim everything from the Prev/Next/List navigation onward — those mark
    # the end of the article body on Mirae's template.
    for marker in ("\nPrev\n", "\nNext\n", "\nList\n"):
        idx = body_text.find(marker)
        if idx > 0:
            body_text = body_text[:idx].rstrip()
            break
    return body_text


# ---------------------------------------------------------------------------
# Detail fetcher
# ---------------------------------------------------------------------------

def _fetch_mirae_detail(row: MiraeListingRow) -> Optional[dict]:
    """Fetch + parse one detail page. Returns a note dict ready to feed the
    digester, OR None on hard failure (caller logs the error)."""
    url = MIRAE_RESEARCH_DETAIL_EN_TEMPLATE.format(message_id=row.message_id)
    try:
        resp = _polite_get(url)
    except Exception as e:
        logger.warning(f"Mirae detail fetch failed for messageId={row.message_id}: {e}")
        return None

    detail = _parse_mirae_detail(resp.text)

    # Save the raw HTML for debugging — analogous to how the old PDF flow
    # saved PDFs under state/cache/broker_pdfs/.
    HTML_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^A-Za-z0-9._-]", "_", (detail.get("title") or row.title or row.message_id))[:80]
    html_path = HTML_CACHE_DIR / f"mirae_{row.published_date_iso}_{row.message_id}_{safe_title}.html"
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(resp.text)
    except OSError as e:
        logger.debug(f"Could not save Mirae detail HTML to {html_path}: {e}")
        html_path = None  # not fatal

    body_text = detail.get("body_text") or ""

    return {
        # Fields the digester reads:
        "title": detail.get("title") or row.title,
        "published_date_iso": detail.get("published_date_iso") or row.published_date_iso,
        "extracted_text": body_text,
        "url": url,
        # Decorative / debugging fields (digester ignores these):
        "html_path": str(html_path) if html_path else None,
        "message_id": row.message_id,
        "attachment_id": row.attachment_id,
        "category": detail.get("category") or row.category,
        "author": detail.get("author") or row.author,
        "stock_code": detail.get("stock_code"),
        "rating": detail.get("rating"),
        "target_price_krw": detail.get("target_price_krw"),
        # Compat with old PDF fields — preserved as None so callers that
        # look for these keys via .get() don't KeyError:
        "pdf_path": None,
        "page_count": 1,
        "was_scanned_ocr": False,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_mirae_notes(days_back: int = 3) -> dict:
    """Discover recent Mirae English notes and fetch their HTML detail pages.

    Output shape is preserved from the pre-v3.39.4 PDF-based scraper so
    `digester.digest_korean_broker_notes` works without modification.

    Args:
        days_back: keep listing rows whose published_date_iso is within this
            many days of today. Default 3 matches pre-v3.39.4 behavior.
    """
    out = {
        "broker": "Mirae Asset Securities",
        "notes": [],
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "source": "Mirae Asset English research portal (HTML via messageId)",
    }

    # ---- Step 1: listing ----
    try:
        listing_resp = _polite_get(MIRAE_RESEARCH_LISTING_EN)
    except Exception as e:
        out["error"] = f"Listing fetch failed: {e}"
        logger.error(f"Mirae listing fetch failed: {e}")
        return out

    rows = _parse_mirae_listing(listing_resp.text)
    logger.info(f"Mirae: parsed {len(rows)} rows from listing")

    # ---- Step 2: date window ----
    cutoff_iso = (date.today() - __import__("datetime").timedelta(days=days_back)).isoformat()
    recent = [r for r in rows if r.published_date_iso >= cutoff_iso]

    # ---- Step 3: relevance filter ----
    relevant = [r for r in recent if _row_is_relevant(r)]
    logger.info(
        f"Mirae: {len(rows)} listed → {len(recent)} within {days_back}d → "
        f"{len(relevant)} relevant (ticker- or keyword-matched)"
    )

    # ---- Step 4: detail fetches ----
    for row in relevant[:MAX_DETAIL_FETCHES_PER_RUN]:
        note = _fetch_mirae_detail(row)
        if note is None:
            continue
        if len(note.get("extracted_text", "")) < 200:
            logger.warning(
                f"Mirae detail messageId={row.message_id} returned "
                f"{len(note.get('extracted_text', ''))} chars body — skipping "
                f"(digester needs >=200 chars). Title: {note.get('title', '?')[:80]}"
            )
            continue
        out["notes"].append(note)

    logger.info(f"Mirae: returning {len(out['notes'])} notes with body text")
    return out


def fetch_mirae_with_retry() -> tuple[Optional[dict], dict]:
    """Top-level entry used by deep_tier.py. Wraps fetch_mirae_notes with the
    shared retry/cache policy."""
    return fetch_with_retry(
        source_id="Mirae Asset (EN)",
        fetch_fn=lambda: fetch_mirae_notes(days_back=3),
        criticality=Criticality.IMPORTANT,
    )
