"""
Phase C verification — final pre-v3.39.3 sanity check.

Two independent tests, both gated on success before we commit code:

  PART 1 — Mirae reliability + parse
    a. Fetch the listing URL 3 times back-to-back with 5s spacing.
       All 3 must return >0 bytes and contain at least one messageId-shaped link.
       Looking for stability — if any of the 3 returns a "Loading..." shell or
       a session/login redirect, we know we need to handle session cookies or
       JS rendering.
    b. From a successful listing, extract all messageIds + title/date snippets.
       Count rows referencing SK Hynix (000660) and Samsung Electronics (005930).
    c. Fetch 2 detail pages by messageId. Parse the structured fields:
       title, author, date, stock code, rating, target price, body text length.
       This proves the detail-page scraper design works.

  PART 2 — WiseReport production-posture stress test
    Mirrors v3.39.3's exact settings: 12s politeness, 60s connect timeout,
    one retry on ConnectTimeout/ReadTimeout with 60s backoff. Hits all 3
    tickers (SK Hynix, Samsung, SK Square). Reports per-ticker row counts.

Output: workflow log only. No artifact upload. Paste the log back.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

# Force line-buffered stdout so progress is visible incrementally on a
# non-TTY runner. Without this, GitHub Actions buffers all output and
# we see nothing if the workflow is canceled.
sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPORT_LINES: list[str] = []


def log(line: str = "") -> None:
    print(line)
    REPORT_LINES.append(line)


def section(title: str) -> None:
    log("")
    log("=" * 78)
    log(f"  {title}")
    log("=" * 78)


def subsection(title: str) -> None:
    log("")
    log(f"--- {title} ---")


# ---------------------------------------------------------------------------
# PART 1 — Mirae reliability + parse
# ---------------------------------------------------------------------------

MIRAE_BASE = "https://securities.miraeasset.com"
MIRAE_LISTING_URL = f"{MIRAE_BASE}/newir/view/pc/en/investor/researchReportsList.jsp"
MIRAE_DETAIL_URL_FMT = f"{MIRAE_BASE}/newir/view/pc/en/investor/researchReportsView.jsp?messageId={{mid}}"

MIRAE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.5",
}


def fetch_mirae_listing() -> Optional[requests.Response]:
    """Single GET of the Mirae research listing page."""
    try:
        r = requests.get(MIRAE_LISTING_URL, headers=MIRAE_HEADERS,
                         timeout=30, allow_redirects=True)
        return r
    except Exception as e:
        log(f"   ERROR: {type(e).__name__}: {e}")
        return None


def extract_messageids_from_listing(html: str) -> list[dict]:
    """Pull all messageId references from the listing HTML.

    Returns list of {messageId, title (if findable), nearby_text}.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Pattern 1: explicit <a href> with messageId in URL
    rows: list[dict] = []
    seen: set[str] = set()

    for tag in soup.find_all(["a", "tr", "li", "div"]):
        # Search for messageId references in href, onclick, or surrounding text
        href = tag.get("href", "") if hasattr(tag, "get") else ""
        onclick = tag.get("onclick", "") if hasattr(tag, "get") else ""
        combined = href + " " + onclick
        m = re.search(r"messageId[=\s'\"]+(\d+)", combined)
        if not m:
            continue
        mid = m.group(1)
        if mid in seen:
            continue
        seen.add(mid)

        text = tag.get_text(" ", strip=True)[:200]
        rows.append({"messageId": mid, "nearby_text": text})

    # Pattern 2: also scan raw HTML for messageId references we may have missed
    for m in re.finditer(r"messageId[=\s'\"]+(\d+)", html):
        mid = m.group(1)
        if mid in seen:
            continue
        seen.add(mid)
        # Try to find the surrounding 100 chars of context
        start = max(0, m.start() - 80)
        end = min(len(html), m.end() + 80)
        context = re.sub(r"\s+", " ", html[start:end])
        rows.append({"messageId": mid, "nearby_text": f"[raw] {context[:200]}"})

    return rows


def count_ticker_references(rows: list[dict]) -> dict[str, int]:
    """Count rows mentioning SK Hynix or Samsung."""
    sk_count = 0
    sam_count = 0
    for row in rows:
        text = row["nearby_text"].lower()
        if "sk hynix" in text or "000660" in text or "hynix" in text:
            sk_count += 1
        if ("samsung electronics" in text or "005930" in text or
            "samsung elec" in text):
            sam_count += 1
    return {"sk_hynix": sk_count, "samsung": sam_count}


def fetch_mirae_detail(message_id: str) -> Optional[requests.Response]:
    """Fetch a single research report detail page."""
    url = MIRAE_DETAIL_URL_FMT.format(mid=message_id)
    log(f"  GET {url}")
    try:
        r = requests.get(url, headers=MIRAE_HEADERS, timeout=30, allow_redirects=True)
        log(f"    status: {r.status_code}, bytes: {len(r.content):,}")
        return r
    except Exception as e:
        log(f"    ERROR: {type(e).__name__}: {e}")
        return None


def parse_mirae_detail(html: str) -> dict:
    """Extract structured fields from a Mirae detail page.

    Returns dict with whatever fields could be parsed; None for missing.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    out: dict = {
        "title": None,
        "author": None,
        "date": None,
        "stock_code": None,
        "rating": None,
        "target_price": None,
        "body_length_chars": len(text),
        "has_pdf_link": False,
    }

    # Title — h1/h2/h3 containing "(NNNNNN KS/Rating)" pattern
    for h in soup.find_all(["h1", "h2", "h3"]):
        h_text = h.get_text(" ", strip=True)
        m = re.search(r"\((\d{6})\s+KS/(\w+)\)", h_text)
        if m:
            out["title"] = h_text
            out["stock_code"] = m.group(1)
            out["rating"] = m.group(2)
            break

    # Author — label-based ("Author") or pattern
    m = re.search(r"Author\s*[:\n]\s*([A-Za-z][\w\s\-.']+)", text)
    if m:
        out["author"] = m.group(1).strip()[:80]

    # Date — anywhere in the page
    m = re.search(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\w+\s+\d{4}", text)
    if m:
        out["date"] = m.group(0)
    else:
        # Fallback: any YYYY-MM-DD or "January 30, 2026" pattern
        m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
        if m:
            out["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # Target price — "W1,370,000" pattern
    m = re.search(r"target price\D{0,40}W([\d,]+)", text, re.I)
    if not m:
        m = re.search(r"W([\d,]{7,})", text)
    if m:
        out["target_price"] = m.group(1)

    # PDF link presence — look for .pdf anywhere in href values
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if ".pdf" in href.lower() or "attachment" in href.lower():
            out["has_pdf_link"] = True
            break

    return out


def diagnose_mirae() -> None:
    section("PART 1 — Mirae reliability + parse")
    log(f"Listing URL: {MIRAE_LISTING_URL}")
    log("Strategy: 3 successive listing fetches → 5s between fetches.")
    log("All 3 must succeed for the source to be considered reliable.")

    # ---- 1a: 3 successive listing fetches ----
    listings: list[Optional[requests.Response]] = []
    for attempt in (1, 2, 3):
        subsection(f"Listing fetch attempt {attempt}/3")
        log(f"  GET {MIRAE_LISTING_URL}")
        r = fetch_mirae_listing()
        if r is not None:
            log(f"    status: {r.status_code}, bytes: {len(r.content):,}")
            log(f"    final URL: {r.url}")
            try:
                r.encoding = r.apparent_encoding or "utf-8"
            except Exception:
                pass
            # Detect "Loading..." shell or session redirect
            text = r.text
            if "Loading..." in text and len(text) < 1000:
                log("    ⚠  Very small response with 'Loading...' — likely JS-rendered or session-redirect")
            elif "session" in text.lower()[:2000] and r.status_code in (302, 401, 403):
                log("    ⚠  Session-required response")
        listings.append(r)
        if attempt < 3:
            time.sleep(5)

    n_successful = sum(1 for r in listings if r and r.status_code == 200 and len(r.content) > 1000)
    log(f"\n  Successful listing fetches: {n_successful}/3")

    if n_successful == 0:
        log("\n  ❌ ALL 3 LISTING FETCHES FAILED — Mirae listing isn't usable as-is.")
        log("     Will need a different strategy (e.g., session cookie, JS rendering, alternate URL).")
        return

    # ---- 1b: parse one of the successful listings ----
    listing_resp = next(r for r in listings if r and r.status_code == 200 and len(r.content) > 1000)
    listing_html = listing_resp.text

    subsection("Extracting messageIds from listing")
    rows = extract_messageids_from_listing(listing_html)
    log(f"  Total messageId references found: {len(rows)}")
    for row in rows[:15]:
        log(f"    messageId={row['messageId']:>10}  text={row['nearby_text'][:100]!r}")

    counts = count_ticker_references(rows)
    log(f"\n  Rows mentioning SK Hynix: {counts['sk_hynix']}")
    log(f"  Rows mentioning Samsung Electronics: {counts['samsung']}")

    if not rows:
        log("\n  ❌ NO messageIds extracted from listing — selector strategy needs review.")
        log("     Dumping first 2000 chars of listing for inspection:")
        log("    ┌── snippet ──")
        for line in listing_html[:2000].split("\n"):
            stripped = line.strip()
            if stripped:
                log(f"    │ {stripped[:200]}")
        log("    └──")
        return

    # ---- 1c: fetch 2 detail pages ----
    subsection("Sampling 2 detail pages for parser validation")
    sample_mids = [r["messageId"] for r in rows[:2]]
    for mid in sample_mids:
        r = fetch_mirae_detail(mid)
        if r and r.status_code == 200:
            try:
                r.encoding = r.apparent_encoding or "utf-8"
            except Exception:
                pass
            parsed = parse_mirae_detail(r.text)
            log(f"    parsed fields:")
            for k, v in parsed.items():
                log(f"      {k}: {v}")
        time.sleep(3)


# ---------------------------------------------------------------------------
# PART 2 — WiseReport production-posture stress test
# ---------------------------------------------------------------------------

WISEREPORT_URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"

WISEREPORT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
    "Referer": "https://finance.naver.com/",
}

POLITE_DELAY_SEC = 12
REQUEST_TIMEOUT_SEC = 60
RETRY_BACKOFF_SEC = 60


def fetch_wisereport_with_retry(code: str) -> tuple[Optional[requests.Response], str]:
    """Mirrors v3.39.3 posture exactly. Returns (response, status_label)."""
    url = WISEREPORT_URL.format(code=code)
    time.sleep(POLITE_DELAY_SEC)
    log(f"  GET {url}")
    log(f"    politeness sleep: {POLITE_DELAY_SEC}s already applied")
    try:
        r = requests.get(url, headers=WISEREPORT_HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        return r, "first_try_ok"
    except (requests.ConnectTimeout, requests.ReadTimeout) as e:
        log(f"    ⚠  Timeout on first try: {type(e).__name__}")
        log(f"    Backing off {RETRY_BACKOFF_SEC}s then retrying once...")
        time.sleep(RETRY_BACKOFF_SEC)
        try:
            r = requests.get(url, headers=WISEREPORT_HEADERS, timeout=REQUEST_TIMEOUT_SEC)
            r.raise_for_status()
            return r, "retry_ok"
        except Exception as retry_e:
            log(f"    ❌ Retry also failed: {type(retry_e).__name__}: {retry_e}")
            return None, "retry_failed"
    except Exception as e:
        log(f"    ❌ Non-timeout error: {type(e).__name__}: {e}")
        return None, "non_timeout_error"


def count_wisereport_actions(html: str) -> int:
    """Count data rows in table#cTB24."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="cTB24")
    if not table:
        return 0
    count = 0
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) != 7:
            continue
        first = cells[0].get_text(strip=True)
        if not first or first == "제공처":
            continue
        count += 1
    return count


def diagnose_wisereport() -> None:
    section("PART 2 — WiseReport production-posture stress test")
    log(f"Settings: politeness={POLITE_DELAY_SEC}s, timeout={REQUEST_TIMEOUT_SEC}s, retry_backoff={RETRY_BACKOFF_SEC}s")
    log("Goal: confirm 12s politeness gives reliable success across all 3 tickers.")

    results = []
    for name, code in [("SK Hynix", "000660"),
                       ("Samsung Electronics", "005930"),
                       ("SK Square", "034730")]:
        subsection(f"{name} ({code})")
        resp, status_label = fetch_wisereport_with_retry(code)
        n_rows = 0
        if resp is not None and resp.status_code == 200:
            try:
                resp.encoding = resp.apparent_encoding or "utf-8"
            except Exception:
                pass
            n_rows = count_wisereport_actions(resp.text)
            log(f"    ✓ {len(resp.content):,} bytes, {n_rows} broker rows in cTB24")
        results.append((name, code, status_label, n_rows))

    log("\n  Summary:")
    log(f"    {'Ticker':<24} {'Code':<8} {'Outcome':<18} {'Rows':<6}")
    for name, code, status_label, n_rows in results:
        log(f"    {name:<24} {code:<8} {status_label:<18} {n_rows:<6}")

    n_first_try = sum(1 for _, _, s, _ in results if s == "first_try_ok")
    n_retry = sum(1 for _, _, s, _ in results if s == "retry_ok")
    n_failed = sum(1 for _, _, s, _ in results if s in ("retry_failed", "non_timeout_error"))
    log(f"\n  First-try OK: {n_first_try}/3   Retry-OK: {n_retry}/3   Failed: {n_failed}/3")

    # Verdict
    sk_hynix_rows = next((n for nm, _, _, n in results if nm == "SK Hynix"), 0)
    samsung_rows = next((n for nm, _, _, n in results if nm == "Samsung Electronics"), 0)
    if sk_hynix_rows >= 15 and samsung_rows >= 15:
        log("\n  ✅ PASS — SK Hynix and Samsung both returned ≥15 broker rows.")
        log("     v3.39.3 posture is validated. Safe to ship.")
    elif sk_hynix_rows >= 1 and samsung_rows >= 1:
        log("\n  ⚠  PARTIAL — SK Hynix and Samsung both returned some rows but coverage is thin.")
        log("     Could be a transient WiseReport state. Re-run before deciding.")
    else:
        log("\n  ❌ FAIL — at least one of SK Hynix / Samsung returned 0 rows.")
        log("     Need to increase politeness further or switch to cached/proxied access.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    section("Phase C verification — pre-v3.39.3 sanity check")
    log("Timestamp (UTC): " + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    log("")
    log("Two independent tests: Mirae reliability + WiseReport production posture.")
    log("Both should PASS before we commit v3.39.3 code.")

    try:
        diagnose_mirae()
    except Exception as e:
        log(f"\nPART 1 (Mirae) crashed: {type(e).__name__}: {e}")

    try:
        diagnose_wisereport()
    except Exception as e:
        log(f"\nPART 2 (WiseReport) crashed: {type(e).__name__}: {e}")

    section("Verification complete")
    log("Paste the workflow log back. We'll write code only after both parts PASS.")
