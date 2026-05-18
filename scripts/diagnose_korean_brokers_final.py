"""
FINAL consolidated diagnostic — dumps everything inline so the workflow
log itself contains all the data we need. No artifact zip fetching.

Covers:
  1. Mirae: probe the newly discovered /newir/ path
  2. NH: fully inline its small homepage + mobile-redirect content
  3. WiseReport (Naver): probe the iframe URL for all 3 tickers + sibling tabs

For small pages (< 6 KB) it dumps the full decoded body inline.
For larger pages it dumps a structural summary: all link hrefs, all
script srcs, all iframe srcs, all table classes/dimensions, plus any
rows containing broker-consensus keywords.

After running, paste the log text back — that's all I need to write
the v3.39.3 fixes.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

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
# Fetch helpers
# ---------------------------------------------------------------------------

NAVER_REFERER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
    "Referer": "https://finance.naver.com/",
}
GENERIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.5",
}


def fetch(url: str, headers: dict | None = None) -> requests.Response | None:
    log(f"\nGET {url}")
    try:
        r = requests.get(url, headers=headers or GENERIC_HEADERS,
                         timeout=30, allow_redirects=True)
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
    return r


# ---------------------------------------------------------------------------
# Inline dump (small pages — show everything)
# ---------------------------------------------------------------------------

def dump_full_body(response: requests.Response) -> None:
    """Inline the full decoded text. Use for pages < 6 KB."""
    log("   ┌── FULL DECODED BODY ──")
    text = response.text
    for line in text.split("\n"):
        # Skip blank-only lines; trim very long lines
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        if len(stripped) > 400:
            stripped = stripped[:400] + " …[truncated]"
        log(f"   │ {stripped}")
    log("   └── END FULL BODY ──")


# ---------------------------------------------------------------------------
# Structural summary (larger pages)
# ---------------------------------------------------------------------------

def structural_summary(response: requests.Response) -> None:
    """Print all useful structural facts about a larger page."""
    soup = BeautifulSoup(response.text, "html.parser")
    base = response.url
    base_netloc = urlparse(base).netloc

    # Title
    if soup.title:
        log(f"   <title>: {soup.title.get_text(strip=True)[:120]!r}")

    # Frames + iframes — important for old-school sites
    for tag_name in ("frame", "iframe"):
        tags = soup.find_all(tag_name)
        if tags:
            log(f"   <{tag_name}> count: {len(tags)}")
            for i, t in enumerate(tags[:8]):
                src = t.get("src", "<no-src>")
                name = t.get("name", "")
                log(f"     [{i}] name={name!r} src={src!r}")

    # All <a> links — show same-domain ones, plus any external that look interesting
    all_links = soup.find_all("a", href=True)
    same_domain = []
    for a in all_links:
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        abs_href = urljoin(base, href)
        if urlparse(abs_href).netloc == base_netloc:
            text = a.get_text(strip=True)
            same_domain.append((text, abs_href))
    log(f"   total <a href>: {len(all_links)}, same-domain: {len(same_domain)}")
    if same_domain:
        log("   same-domain links (up to 30):")
        seen = set()
        n = 0
        for text, href in same_domain:
            key = (text[:60], href[:160])
            if key in seen:
                continue
            seen.add(key)
            log(f"     '{text[:50]}' -> {href[:140]}")
            n += 1
            if n >= 30:
                break

    # All <script src> — sometimes routes/configs are in JS files
    scripts = [s.get("src", "") for s in soup.find_all("script") if s.get("src")]
    if scripts:
        log(f"   <script src> count: {len(scripts)}")
        for s in scripts[:10]:
            log(f"     {s[:160]}")

    # Inline scripts: search for URL-like strings that may reveal SPA routes
    inline_scripts_text = "\n".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    if inline_scripts_text:
        # Strip down to URL-shaped strings
        url_matches = re.findall(r'["\'](/[a-zA-Z][\w/\-.]+\.(?:do|jsp|aspx|html|json)[^"\']*)["\']',
                                 inline_scripts_text)
        url_matches += re.findall(r'["\'](https?://[\w./\-?=&]+)["\']', inline_scripts_text)
        unique = sorted({u for u in url_matches if len(u) < 200})
        if unique:
            log(f"   URL-shaped strings in inline scripts: {len(unique)}")
            for u in unique[:20]:
                log(f"     {u}")

    # Tables — class + dimensions + broker-keyword sniff
    tables = soup.find_all("table")
    if tables:
        log(f"   <table> count: {len(tables)}")
        for i, t in enumerate(tables[:15]):
            cls = " ".join(t.get("class") or []) or "<no-class>"
            rows = t.find_all("tr")
            max_cells = max((len(tr.find_all(["td", "th"])) for tr in rows), default=0)
            ttext = t.get_text(" ", strip=True)
            has_broker = any(term in ttext for term in
                             ["목표주가", "투자의견", "증권사", "컨센서스",
                              "Buy", "Hold", "Sell", "매수", "보유", "매도"])
            marker = " 🎯BROKER" if has_broker else ""
            log(f"     [{i}] class={cls!r}, rows={len(rows)}, max_cells={max_cells}{marker}")

        # For any table with broker keywords, dump first 6 rows
        for i, t in enumerate(tables):
            ttext = t.get_text(" ", strip=True)
            if any(term in ttext for term in ["목표주가", "투자의견", "컨센서스", "매수"]):
                log(f"\n   ┌── 🎯 Table [{i}] — first 6 rows:")
                rows = t.find_all("tr")
                for j, tr in enumerate(rows[:6]):
                    cells = tr.find_all(["td", "th"])
                    if not cells:
                        continue
                    cells_txt = [c.get_text(" ", strip=True)[:50] for c in cells]
                    log(f"   │  row[{j}]: " + " | ".join(cells_txt))
                log("   └──")


def inspect(url: str, headers: dict | None = None, label: str = "") -> None:
    """Fetch + inline-or-summarize."""
    if label:
        subsection(label)
    r = fetch(url, headers=headers)
    if r is None:
        return
    size = len(r.content)
    if size < 6000:
        dump_full_body(r)
    else:
        structural_summary(r)


# ---------------------------------------------------------------------------
# 1. MIRAE — probe the /newir/ path discovered in the frameset
# ---------------------------------------------------------------------------

def diagnose_mirae() -> None:
    section("1. MIRAE — probe the new /newir/ IR portal")
    log("Phase 1+2 finding: /eng/ is a frameset pointing to /newir/view/pc/en/main.jsp")
    log("Hunting for the research listing inside the /newir/ tree.")

    candidates = [
        "https://securities.miraeasset.com/newir/view/pc/en/main.jsp",
        "https://securities.miraeasset.com/newir/view/pc/en/",
        "https://securities.miraeasset.com/newir/",
        # Educated guesses at where reports might live under newir
        "https://securities.miraeasset.com/newir/view/pc/en/research.jsp",
        "https://securities.miraeasset.com/newir/view/pc/en/report.jsp",
        "https://securities.miraeasset.com/newir/view/pc/en/material.jsp",
    ]
    for url in candidates:
        inspect(url, label=f"Mirae: {url}")
        time.sleep(2)


# ---------------------------------------------------------------------------
# 2. NH — inline its small pages, look for SPA routes
# ---------------------------------------------------------------------------

def diagnose_nh() -> None:
    section("2. NH — fully inline the small homepage + mobile redirect")
    log("Phase 2 finding: nhsec.com/ is 4 KB, m.nhsec.com/ is 399 bytes — both small")
    log("enough to dump fully. Looking for SPA bootstrap or JS-defined routes.")

    candidates = [
        "https://www.nhsec.com/",
        "https://m.nhsec.com/",
        # Try the Korean research path explicitly (nhsec is Korean)
        "https://www.nhsec.com/CC/research/list.nhd",
        "https://www.nhsec.com/CC/research/",
    ]
    for url in candidates:
        inspect(url, label=f"NH: {url}")
        time.sleep(2)


# ---------------------------------------------------------------------------
# 3. WISEREPORT — the Naver iframe target (Phase 3 contents)
# ---------------------------------------------------------------------------

def diagnose_wisereport() -> None:
    section("3. WISEREPORT — Naver's iframe target for per-ticker broker data")
    log("Iframe src found in coinfo.naver: navercomp.wisereport.co.kr/v2/company/c1010001.aspx")
    log("Probing main tab + 2 candidate sibling tabs for all 3 KOSPI tickers.")

    tickers = [("SK Hynix", "000660"), ("Samsung Electronics", "005930"), ("SK Square", "034730")]
    tabs = ["c1010001", "c1020001", "cF1002"]

    for name, code in tickers:
        for tab in tabs:
            url = f"https://navercomp.wisereport.co.kr/v2/company/{tab}.aspx?cmp_cd={code}"
            inspect(url, headers=NAVER_REFERER_HEADERS, label=f"{name} ({code}) / tab={tab}")
            time.sleep(2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    section("Korean broker diagnostic — FINAL consolidated round")
    log("Timestamp (UTC): " + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    log("")
    log("This dumps everything inline. No artifact zip needed afterwards —")
    log("just paste the workflow log back.")

    try:
        diagnose_mirae()
    except Exception as e:
        log(f"\nMirae diagnostic crashed: {e}")
    try:
        diagnose_nh()
    except Exception as e:
        log(f"\nNH diagnostic crashed: {e}")
    try:
        diagnose_wisereport()
    except Exception as e:
        log(f"\nWiseReport diagnostic crashed: {e}")

    section("Diagnostic complete")
    log("Copy the workflow log output and paste it back — that's all we need.")
