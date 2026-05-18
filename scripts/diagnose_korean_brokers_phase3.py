"""
Phase 3 diagnostic: probe the WiseReport iframe endpoint.

Phase 1 revealed that Naver moved its broker consensus content from
the parent coinfo page into a third-party iframe served by
navercomp.wisereport.co.kr. Our scraper never followed the iframe,
which is why we got 0 broker rows.

Phase 3 fetches the WiseReport URL for each of our 3 tickers, then:
  - Reports HTTP status, encoding, page structure
  - Lists every table's class name and dimensions
  - Pulls out any rows that contain broker-consensus keywords
    (목표주가 = target price, 투자의견 = investment opinion, 증권사 = brokerage)
  - Lists same-domain navigation links — WiseReport likely has multiple
    tabs (company main / consensus / financials), and we want to find
    the right one if c1010001 isn't it

We also send a Referer header from Naver, in case WiseReport rejects
direct access without one.

Outputs:
  scripts/diagnose_korean_brokers_phase3_output.txt
  scripts/_diag_fixtures_phase3/wisereport_*.html
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

REPORT_LINES: list[str] = []
FIXTURE_DIR = Path(__file__).parent / "_diag_fixtures_phase3"
FIXTURE_DIR.mkdir(exist_ok=True)


def log(line: str = "") -> None:
    print(line)
    REPORT_LINES.append(line)


def section(title: str) -> None:
    log("")
    log("=" * 78)
    log(f"  {title}")
    log("=" * 78)


# WiseReport may gate on a Naver referer — mimic what the iframe would send.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
    "Referer": "https://finance.naver.com/",
}

TICKERS = [
    ("SK Hynix", "000660"),
    ("Samsung Electronics", "005930"),
    ("SK Square", "034730"),
]

# The known iframe URL. We also try alternate tab IDs in case c1010001
# is the wrong sub-page — WiseReport's "c{tab_id}" pattern is structured
# enough to enumerate likely tabs.
WISEREPORT_PATTERNS = [
    "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}",  # main (known)
    "https://navercomp.wisereport.co.kr/v2/company/c1020001.aspx?cmp_cd={code}",  # candidate: 2nd tab
    "https://navercomp.wisereport.co.kr/v2/company/cF1002.aspx?cmp_cd={code}",   # candidate: consensus (heuristic)
]


def fetch(url: str, fixture_name: str | None = None) -> requests.Response | None:
    log(f"\nGET {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception as e:
        log(f"   ERROR: {type(e).__name__}: {e}")
        return None
    log(f"   status: {r.status_code}")
    log(f"   final URL: {r.url}")
    log(f"   bytes: {len(r.content):,}")
    log(f"   content-type: {r.headers.get('content-type', '<none>')}")
    if r.history:
        log(f"   redirects: {' -> '.join(str(h.status_code) for h in r.history)}")

    try:
        r.encoding = r.apparent_encoding or "utf-8"
    except Exception:
        pass

    if fixture_name:
        with open(FIXTURE_DIR / fixture_name, "wb") as f:
            f.write(r.content)
        log(f"   saved: scripts/_diag_fixtures_phase3/{fixture_name}")

    snippet = r.text[:400].replace("\n", " ")
    log(f"   snippet: {snippet!r}")
    return r


def analyze(response: requests.Response, ticker_name: str) -> None:
    """Print structural facts about the WiseReport response."""
    soup = BeautifulSoup(response.text, "html.parser")
    text = response.text

    # 404 / error detection
    if re.search(r"404|not found|페이지를 찾을 수 없|오류", text[:2000], re.I):
        log("   ⚠  possible 404 / error page")

    # All tables — class + dimensions
    tables = soup.find_all("table")
    log(f"   <table> count: {len(tables)}")
    for i, t in enumerate(tables[:15]):
        cls = " ".join(t.get("class") or []) or "<no-class>"
        rows = t.find_all("tr")
        max_cells = max((len(tr.find_all(["td", "th"])) for tr in rows), default=0)
        # Sniff: does this table look like it has broker data?
        table_text = t.get_text(" ", strip=True)
        has_broker_term = any(term in table_text for term in
                              ["목표주가", "투자의견", "증권사", "컨센서스",
                               "Buy", "Hold", "Sell", "매수", "보유", "매도"])
        marker = " 🎯" if has_broker_term else ""
        log(f"     [{i}] class={cls!r}, rows={len(rows)}, max_cells={max_cells}{marker}")

    # Korean broker-consensus keyword density
    log("   Korean term counts:")
    for term in ["목표주가", "투자의견", "증권사", "컨센서스", "애널리스트"]:
        log(f"     '{term}': {text.count(term)}")

    # Same-domain navigation links — find other tabs to probe
    base_netloc = urlparse(response.url).netloc
    nav_links = set()
    for a in soup.find_all("a", href=True):
        abs_href = urljoin(response.url, a["href"])
        if urlparse(abs_href).netloc == base_netloc and abs_href != response.url:
            nav_links.add(abs_href[:200])
    log(f"   same-domain nav links: {len(nav_links)}")
    for lk in sorted(nav_links)[:15]:
        log(f"     {lk}")

    # Promising table content — if we find a table with broker terms,
    # dump its first few rows so we can design the parser
    for i, t in enumerate(tables):
        table_text = t.get_text(" ", strip=True)
        if any(term in table_text for term in ["목표주가", "투자의견"]):
            log(f"\n   ┌── 🎯 Table [{i}] has broker keywords — sample rows:")
            rows = t.find_all("tr")
            for j, tr in enumerate(rows[:6]):
                cells = tr.find_all(["td", "th"])
                if not cells:
                    continue
                cells_txt = [c.get_text(" ", strip=True)[:60] for c in cells]
                log(f"   │  row[{j}]: " + " | ".join(cells_txt))
            log("   └──")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    section("WiseReport iframe probe — Phase 3")
    log("Timestamp (UTC): " + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    log("")
    log("Probes the navercomp.wisereport.co.kr URL hardcoded in Naver's coinfo")
    log("iframe. If the main tab (c1010001) doesn't have broker consensus, we")
    log("also probe candidate sibling tabs.")

    for ticker_name, code in TICKERS:
        section(f"{ticker_name} ({code})")
        for i, pattern in enumerate(WISEREPORT_PATTERNS):
            url = pattern.format(code=code)
            tab_id = re.search(r"c([A-Za-z0-9]+)\.aspx", url)
            tab = tab_id.group(1) if tab_id else f"tab{i}"
            fname = f"wisereport_{code}_{tab}.html"
            log(f"\n--- {ticker_name} / tab={tab} ---")
            r = fetch(url, fixture_name=fname)
            if r and r.status_code == 200:
                analyze(r, ticker_name)
            time.sleep(3)

    section("Phase 3 complete")
    log(f"Fixtures saved to: {FIXTURE_DIR}")

    report_path = Path(__file__).parent / "diagnose_korean_brokers_phase3_output.txt"
    with open(report_path, "w") as f:
        f.write("\n".join(REPORT_LINES))
    print(f"\nReport written to: {report_path}")
