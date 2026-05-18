"""
Diagnostic for the four Korean broker data sources.

Run locally:  python scripts/diagnose_korean_brokers.py
Output:       scripts/diagnose_korean_brokers_output.txt (also stdout)

What it does:
  - Hits each endpoint with the SAME headers/UA the production code uses
  - Prints HTTP status, response size, and structural facts (table classes,
    link patterns, iframe/script counts) — not just "did it parse"
  - For Samsung Sec: reports credential presence and inbox structure
  - Saves raw HTML to scripts/_diag_fixtures/ so we can build unit-test fixtures

Read-only: no project state touched, no caches written.
Idempotent: safe to re-run.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPORT_LINES: list[str] = []
FIXTURE_DIR = Path(__file__).parent / "_diag_fixtures"
FIXTURE_DIR.mkdir(exist_ok=True)


def log(line: str = "") -> None:
    """Append to both stdout and the captured report."""
    print(line)
    REPORT_LINES.append(line)


def section(title: str) -> None:
    log("")
    log("=" * 78)
    log(f"  {title}")
    log("=" * 78)


def save_fixture(name: str, content: bytes | str) -> Path:
    """Save raw response so we can build unit-test fixtures later."""
    path = FIXTURE_DIR / name
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
}
GENERIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; memory-cycle-brief)"
}


def probe(url: str, headers: dict, label: str, fixture_name: str | None = None) -> dict | None:
    """Fetch a URL with details. Returns a small struct for downstream analysis."""
    log(f"\nGET {url}")
    log(f"   UA: {headers.get('User-Agent', '<none>')[:60]}...")
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    except Exception as e:
        log(f"   ERROR: {type(e).__name__}: {e}")
        return None
    log(f"   status: {r.status_code}")
    log(f"   final URL: {r.url}")
    log(f"   bytes: {len(r.content):,}")
    log(f"   content-type: {r.headers.get('content-type', '<none>')}")
    if r.history:
        log(f"   redirects: {' -> '.join(str(h.status_code) for h in r.history)}")

    # Detect Korean encoding
    try:
        r.encoding = r.apparent_encoding or "utf-8"
    except Exception:
        pass

    text = r.text
    if fixture_name:
        save_fixture(fixture_name, r.content)
        log(f"   saved: scripts/_diag_fixtures/{fixture_name}")

    # First 400 chars (after the doctype/head boilerplate) — clue about page type
    snippet = text[:400].replace("\n", " ")
    log(f"   snippet: {snippet!r}")

    return {"status": r.status_code, "url": r.url, "text": text, "headers": dict(r.headers)}


def analyze_html_structure(text: str, label: str) -> None:
    """Print structural facts about the HTML — what selectors WOULD match."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("   (bs4 not installed — skipping structural analysis)")
        return

    soup = BeautifulSoup(text, "html.parser")

    # Page-type signals
    has_captcha = bool(re.search(r"captcha|robot|verify you are human|access denied",
                                 text, re.I))
    has_login = bool(re.search(r"<form[^>]*login|please sign in|로그인", text, re.I))
    is_naver_block = bool(re.search(r"naver\.com.*?(차단|abuse|이용 제한)", text, re.I))
    if has_captcha:
        log("   ⚠  CAPTCHA / anti-bot markers detected in response")
    if has_login:
        log("   ⚠  login-wall markers detected")
    if is_naver_block:
        log("   ⚠  Naver-specific abuse-block markers detected")

    # All <table> classes — pinpoints selector rot
    tables = soup.find_all("table")
    log(f"   <table> count: {len(tables)}")
    if tables:
        seen_classes: dict[str, int] = {}
        for t in tables:
            cls = " ".join(t.get("class") or []) or "<no-class>"
            seen_classes[cls] = seen_classes.get(cls, 0) + 1
        for cls, n in sorted(seen_classes.items(), key=lambda kv: -kv[1])[:10]:
            log(f"     class={cls!r}: {n}")

    # iframes — indicates JS-rendered embedded content
    iframes = soup.find_all("iframe")
    if iframes:
        log(f"   <iframe> count: {len(iframes)}")
        for i, fr in enumerate(iframes[:5]):
            src = fr.get("src", "<no-src>")
            log(f"     [{i}] src={src!r}")

    # Script tag count + suspicious frameworks
    scripts = soup.find_all("script")
    log(f"   <script> count: {len(scripts)}")
    if re.search(r"\b(React|Vue|Angular|Next\.js|Nuxt)\b", text):
        frameworks = re.findall(r"\b(React|Vue|Angular|Next\.js|Nuxt)\b", text)
        log(f"   JS frameworks mentioned: {set(frameworks)}")

    # All <a> link patterns that look like PDFs or downloads
    pdf_like = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf|download|fileDown|file_down|attach", href, re.I):
            pdf_like.append((a.get_text(strip=True)[:60], href[:120]))
    if pdf_like:
        log(f"   PDF-like links found: {len(pdf_like)}")
        for title, href in pdf_like[:8]:
            log(f"     '{title}' -> {href}")
    else:
        log("   PDF-like links: NONE")


# ---------------------------------------------------------------------------
# Probes — Naver Finance
# ---------------------------------------------------------------------------

def diagnose_naver() -> None:
    section("1. NAVER FINANCE — Korean broker consensus")
    log("Code path: src/data/naver_finance.py::_fetch_one_ticker")
    log("Expected selector: table.tb_type1 / table.tb_type1_ifrs")

    # The 3 production tickers
    for name, code in [
        ("SK Hynix", "000660"),
        ("Samsung Electronics", "005930"),
        ("SK Square", "034730"),
    ]:
        log(f"\n--- {name} ({code}) — primary URL ---")
        result = probe(
            f"https://finance.naver.com/item/coinfo.naver?code={code}",
            NAVER_HEADERS,
            label=f"naver_coinfo_{code}",
            fixture_name=f"naver_coinfo_{code}.html",
        )
        if result and result["status"] == 200:
            analyze_html_structure(result["text"], label=f"naver_{code}")
        time.sleep(7)  # politeness

        log(f"\n--- {name} ({code}) — fallback URL ---")
        result = probe(
            f"https://finance.naver.com/research/company_list.naver?keyword={code}",
            NAVER_HEADERS,
            label=f"naver_research_{code}",
            fixture_name=f"naver_research_{code}.html",
        )
        if result and result["status"] == 200:
            analyze_html_structure(result["text"], label=f"naver_research_{code}")
        time.sleep(7)


# ---------------------------------------------------------------------------
# Probes — Mirae Asset
# ---------------------------------------------------------------------------

def diagnose_mirae() -> None:
    section("2. MIRAE ASSET — English research listing")
    log("Code path: src/data/korean_brokers.py::fetch_mirae_notes")
    log("Expected selector: table.tb_list tr + a[href*='.pdf'|'fileDown']")

    result = probe(
        "https://securities.miraeasset.com/eng/bbs/board/message/list.do?MENU_ID=2310",
        GENERIC_HEADERS,
        label="mirae_listing",
        fixture_name="mirae_listing.html",
    )
    if result and result["status"] == 200:
        analyze_html_structure(result["text"], label="mirae_listing")

    # Try the English root as a sanity check — has the path moved entirely?
    log("\n--- Sanity: Mirae English research root ---")
    result = probe(
        "https://securities.miraeasset.com/eng/research/main.do",
        GENERIC_HEADERS,
        label="mirae_root",
    )
    if result and result["status"] == 200:
        analyze_html_structure(result["text"], label="mirae_root")


# ---------------------------------------------------------------------------
# Probes — NH Investment
# ---------------------------------------------------------------------------

def diagnose_nh() -> None:
    section("3. NH INVESTMENT — English research listing")
    log("Code path: src/data/korean_brokers.py::fetch_nh_notes")
    log("Expected: a[href*='.pdf'|'download'|'fileDown'] on m.nhqv.com/research/list/global")

    result = probe(
        "https://m.nhqv.com/research/list/global",
        GENERIC_HEADERS,
        label="nh_listing",
        fixture_name="nh_listing.html",
    )
    if result and result["status"] == 200:
        analyze_html_structure(result["text"], label="nh_listing")

    # Sanity: desktop portal — has the English research moved back from mobile?
    log("\n--- Sanity: NH desktop English research ---")
    result = probe(
        "https://www.nhqv.com/research/global",
        GENERIC_HEADERS,
        label="nh_desktop",
    )
    if result and result["status"] == 200:
        analyze_html_structure(result["text"], label="nh_desktop")


# ---------------------------------------------------------------------------
# Probes — Samsung Securities (Gmail IMAP)
# ---------------------------------------------------------------------------

def diagnose_samsung_gmail() -> None:
    section("4. SAMSUNG SECURITIES — Gmail IMAP probe")
    log("Code path: src/data/korean_brokers.py::fetch_samsung_securities_notes")
    log("Filter: FROM 'samsungpop.com' SINCE last 3 days")

    user = os.environ.get("SAMSUNG_GMAIL_USER", "")
    pw = os.environ.get("SAMSUNG_GMAIL_APP_PW", "")

    if not user or not pw:
        log("\n   STATUS: credentials NOT set in environment")
        log("   To probe Gmail set these locally:")
        log("     export SAMSUNG_GMAIL_USER='<dedicated-inbox>@gmail.com'")
        log("     export SAMSUNG_GMAIL_APP_PW='<gmail-app-password>'")
        log("   Then re-run.")
        log("   Also verify both are set as GitHub Secrets — workflow plumbing is in")
        log("   .github/workflows/deep_tier.yml lines 47-48.")
        return

    log(f"   user: {user[:3]}***@{user.split('@')[-1] if '@' in user else '?'}")
    log(f"   pw set: yes ({len(pw)} chars)")

    try:
        import imaplib
        import email as email_lib
        from datetime import date, timedelta

        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(user, pw)
            imap.select("INBOX")

            # Total count
            status, count_data = imap.search(None, "ALL")
            total_n = len(count_data[0].split()) if status == "OK" and count_data[0] else 0
            log(f"   inbox total messages: {total_n}")

            # Recent (last 7d) — wider window to see if anything's arriving at all
            since_7d = (date.today() - timedelta(days=7)).strftime("%d-%b-%Y")
            status, recent_data = imap.search(None, f'(SINCE "{since_7d}")')
            recent_n = len(recent_data[0].split()) if status == "OK" and recent_data[0] else 0
            log(f"   messages in last 7d: {recent_n}")

            # Production filter: samsungpop.com + 3d
            since_3d = (date.today() - timedelta(days=3)).strftime("%d-%b-%Y")
            status, prod_data = imap.search(
                None, f'(FROM "samsungpop.com" SINCE "{since_3d}")'
            )
            prod_n = len(prod_data[0].split()) if status == "OK" and prod_data[0] else 0
            log(f"   matches FROM 'samsungpop.com' SINCE last 3d: {prod_n}  <-- production filter")

            # Broader: any From address with "samsung" in last 14d
            since_14d = (date.today() - timedelta(days=14)).strftime("%d-%b-%Y")
            status, broad_data = imap.search(
                None, f'(FROM "samsung" SINCE "{since_14d}")'
            )
            broad_n = len(broad_data[0].split()) if status == "OK" and broad_data[0] else 0
            log(f"   matches FROM '*samsung*' SINCE last 14d: {broad_n}")

            # If we have any recent messages, show the From addresses to spot
            # whether Samsung's distribution moved off samsungpop.com
            if recent_n > 0:
                log("\n   --- From addresses in last 7d (top 15) ---")
                from_counter: dict[str, int] = {}
                ids = recent_data[0].split()[-min(50, recent_n):]
                for msg_id in ids:
                    _, msg_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                    if msg_data and msg_data[0]:
                        m = email_lib.message_from_bytes(msg_data[0][1])
                        f = (m.get("From") or "").strip()
                        # Extract domain portion
                        domain_match = re.search(r"@([\w.-]+)", f)
                        domain = domain_match.group(1) if domain_match else f[:40]
                        from_counter[domain] = from_counter.get(domain, 0) + 1
                for domain, n in sorted(from_counter.items(), key=lambda kv: -kv[1])[:15]:
                    log(f"     {n:3d}  @{domain}")

    except Exception as e:
        log(f"   ERROR: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    section("Korean broker channels — diagnostic")
    log("Run timestamp (UTC): " + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    log("Running from: " + os.getcwd())
    log("This probes each source the SAME way production does, then dumps")
    log("structural facts so we can pinpoint what's broken.")
    log("")
    log("⚠  Note: this runs from your LOCAL IP. If Korean sites block GitHub")
    log("   Actions IP ranges specifically, local will succeed and production")
    log("   will still fail — different problem, different fix.")

    try:
        diagnose_naver()
    except Exception as e:
        log(f"\nNaver diagnostic crashed: {e}")

    try:
        diagnose_mirae()
    except Exception as e:
        log(f"\nMirae diagnostic crashed: {e}")

    try:
        diagnose_nh()
    except Exception as e:
        log(f"\nNH diagnostic crashed: {e}")

    try:
        diagnose_samsung_gmail()
    except Exception as e:
        log(f"\nSamsung Gmail diagnostic crashed: {e}")

    section("Diagnostic complete")
    log(f"Fixtures saved to: {FIXTURE_DIR}")
    log("Paste the contents of scripts/diagnose_korean_brokers_output.txt back")
    log("for analysis. Also paste the relevant .html fixtures if requested.")

    # Save the full report
    report_path = Path(__file__).parent / "diagnose_korean_brokers_output.txt"
    with open(report_path, "w") as f:
        f.write("\n".join(REPORT_LINES))
    print(f"\nReport written to: {report_path}")
