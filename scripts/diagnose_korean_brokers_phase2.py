"""
Phase 2 diagnostic: discover the new Mirae & NH research URLs.

Phase 1 confirmed:
  - Mirae's old URL (/eng/bbs/board/message/list.do?MENU_ID=2310) is 404
  - NH rebranded from nhqv.com → nhsec.com; new URLs also 404 on old paths

This script crawls each broker's English homepage and reports every
link whose text or URL hints at research/reports/analyst content, so we
can spot the new path quickly without manual browsing.

Run via the same workflow_dispatch as the Phase 1 diagnostic — just
swap the script name.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPORT_LINES: list[str] = []
FIXTURE_DIR = Path(__file__).parent / "_diag_fixtures_phase2"
FIXTURE_DIR.mkdir(exist_ok=True)

GENERIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; memory-cycle-brief diagnostic)",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.5",
}


def log(line: str = "") -> None:
    print(line)
    REPORT_LINES.append(line)


def section(title: str) -> None:
    log("")
    log("=" * 78)
    log(f"  {title}")
    log("=" * 78)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Words that suggest a link goes to research content (English + Korean)
RESEARCH_HINT_TERMS = [
    # English
    "research", "report", "analyst", "equity", "company", "industry",
    "strategy", "daily", "weekly", "morning", "outlook", "forecast",
    "global", "memory", "semiconductor", "tech",
    # Korean
    "리서치", "보고서", "분석", "리포트", "전망", "투자정보",
]
HINT_RE = re.compile("|".join(RESEARCH_HINT_TERMS), re.I)


def fetch(url: str, fixture_name: str | None = None) -> requests.Response | None:
    """GET with logging. Return Response or None on failure."""
    log(f"\nGET {url}")
    try:
        r = requests.get(url, headers=GENERIC_HEADERS, timeout=30, allow_redirects=True)
    except Exception as e:
        log(f"   ERROR: {type(e).__name__}: {e}")
        return None
    log(f"   status: {r.status_code}")
    log(f"   final URL: {r.url}")
    log(f"   bytes: {len(r.content):,}")
    if r.history:
        log(f"   redirects: {' -> '.join(str(h.status_code) for h in r.history)}")

    try:
        r.encoding = r.apparent_encoding or "utf-8"
    except Exception:
        pass

    if fixture_name:
        with open(FIXTURE_DIR / fixture_name, "wb") as f:
            f.write(r.content)
        log(f"   saved: scripts/_diag_fixtures_phase2/{fixture_name}")
    return r


def detect_404(response: requests.Response) -> bool:
    """Detect 404 — both by status and by Korean error-page title strings."""
    if response.status_code == 404:
        return True
    text = response.text
    if re.search(r"방문하시려는 페이지를 찾을 수 없습니다|오류페이지|페이지를 찾을 수 없", text):
        return True
    if "<title>" in text.lower():
        title_match = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        if title_match:
            title = title_match.group(1).strip()
            if any(s in title.lower() for s in ("not found", "error", "404")):
                return True
    return False


def list_research_links(response: requests.Response, base_url: str) -> None:
    """Print all anchor links on the page that look research-related."""
    soup = BeautifulSoup(response.text, "html.parser")
    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str]] = []  # (text, href)

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"].strip()
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        # Absolutize
        abs_href = urljoin(base_url, href)
        # Filter to same domain
        if urlparse(abs_href).netloc != urlparse(base_url).netloc:
            continue
        key = (text[:80], abs_href[:200])
        if key in seen:
            continue
        seen.add(key)
        # Score: does text or href contain a research hint?
        if HINT_RE.search(text) or HINT_RE.search(href):
            candidates.append((text, abs_href))

    log(f"   internal links: {len(soup.find_all('a', href=True))}, research-hinting: {len(candidates)}")
    for text, href in candidates[:40]:
        # Truncate but keep useful detail
        t = (text or "<no-text>")[:60]
        h = href[:130]
        log(f"     '{t}' -> {h}")


def visit_and_summarize(url: str, fixture_name: str, label: str) -> None:
    """Fetch + print 404 detection + research link summary."""
    log(f"\n--- {label}: {url} ---")
    r = fetch(url, fixture_name=fixture_name)
    if r is None:
        return
    if detect_404(r):
        log(f"   ⚠  404 / error page detected")
        return
    list_research_links(r, r.url)


# ---------------------------------------------------------------------------
# Mirae Asset — find the new research portal
# ---------------------------------------------------------------------------

def hunt_mirae() -> None:
    section("MIRAE ASSET — hunt for new English research URL")
    log("Old URL: /eng/bbs/board/message/list.do?MENU_ID=2310 → 404")
    log("Strategy: crawl the English homepage and follow research-hinting links.")

    candidates = [
        "https://securities.miraeasset.com/eng/",
        "https://securities.miraeasset.com/eng/index.do",
        "https://securities.miraeasset.com/",
        "https://us.miraeasset.com/",  # US sister site, sometimes hosts global research
    ]
    for url in candidates:
        fname = "mirae_" + re.sub(r"[^A-Za-z0-9]", "_", url)[-60:] + ".html"
        visit_and_summarize(url, fname, "Mirae homepage candidate")
        time.sleep(2)


# ---------------------------------------------------------------------------
# NH Investment — find the new research portal on nhsec.com
# ---------------------------------------------------------------------------

def hunt_nh() -> None:
    section("NH INVESTMENT — hunt for new English research URL")
    log("Old domain: nhqv.com → permanently redirects to nhsec.com")
    log("Old path: /research/list/global → 404 on new domain")
    log("Strategy: crawl nhsec.com homepages and follow research-hinting links.")

    candidates = [
        "https://www.nhsec.com/",
        "https://www.nhsec.com/eng/",
        "https://m.nhsec.com/",
        "https://www.nhsec.com/research",
        "https://www.nhsec.com/research/global",
        # Also try old domain explicitly — redirect chain may carry useful hints
        "https://www.nhqv.com/research/global",
    ]
    for url in candidates:
        fname = "nh_" + re.sub(r"[^A-Za-z0-9]", "_", url)[-60:] + ".html"
        visit_and_summarize(url, fname, "NH homepage candidate")
        time.sleep(2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    section("Korean broker URL archaeology — Phase 2")
    log("Timestamp (UTC): " + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    log("")
    log("This crawls each broker's English homepage and lists every link that")
    log("smells like research/reports, so we can spot the new path quickly.")

    try:
        hunt_mirae()
    except Exception as e:
        log(f"\nMirae hunt crashed: {e}")

    try:
        hunt_nh()
    except Exception as e:
        log(f"\nNH hunt crashed: {e}")

    section("Phase 2 complete")
    log(f"Fixtures saved to: {FIXTURE_DIR}")

    report_path = Path(__file__).parent / "diagnose_korean_brokers_phase2_output.txt"
    with open(report_path, "w") as f:
        f.write("\n".join(REPORT_LINES))
    print(f"\nReport written to: {report_path}")
