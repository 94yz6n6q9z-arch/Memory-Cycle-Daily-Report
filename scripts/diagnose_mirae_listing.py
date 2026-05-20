"""
Phase D diagnostic — dump the Mirae Asset English research listing page so we
can see what selector format the row links actually use.

Context. The v3.39.3 Phase C verification confirmed that the Mirae listing URL
is reachable and stable (3/3 fetches at 106 KB each, no Loading shells, no
session redirects). But the parser's `messageId=\\d+` regex found ZERO matches
in the response body — meaning the listing's row links don't use URL-style
`?messageId=N` references. The handover hypothesizes one of:
  - JavaScript handlers like `javascript:goView('2337613')`
  - Data attributes like `data-seq=` or `data-message-id=`
  - Form-based POST submissions with hidden inputs

This diagnostic dumps enough HTML and runs enough probes to tell us which.

Run via `.github/workflows/diagnose_mirae_listing.yml` (workflow_dispatch). The
output goes to the Actions log — copy-paste the result into the next session.
The diagnostic does not modify any state, does not write to disk, and does
not affect production. Safe to run repeatedly.

CHANGELOG (this file):
  v3.39.3.1 (2026-05-18) — NEW. Phase D probe for the v3.39.4 Mirae scraper.
"""

from __future__ import annotations

import re
import sys
from collections import Counter

import requests
from bs4 import BeautifulSoup

LISTING_URL = (
    "https://securities.miraeasset.com/newir/view/pc/en/"
    "investor/researchReportsList.jsp"
)

# Same UA the Phase C verification used; matches the v3.39.3 naver_finance.py
# convention of pretending to be embedded iframe traffic.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.6",
}

# How much context to show around each match.
CONTEXT_CHARS = 200
# Cap matches per probe so the log doesn't explode if the page is content-heavy.
MAX_MATCHES_PER_PROBE = 5


def banner(title: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{title}\n{bar}")


def show_context(label: str, body: str, positions: list[int]) -> None:
    """Print up to MAX_MATCHES_PER_PROBE windows around each position."""
    if not positions:
        print(f"  ({label}: 0 matches)")
        return
    print(f"  ({label}: {len(positions)} matches; showing first {min(len(positions), MAX_MATCHES_PER_PROBE)})")
    for i, pos in enumerate(positions[:MAX_MATCHES_PER_PROBE]):
        lo = max(0, pos - 40)
        hi = min(len(body), pos + CONTEXT_CHARS)
        window = body[lo:hi].replace("\n", " ").replace("\t", " ")
        # Collapse runs of spaces so the log stays compact
        window = re.sub(r"\s+", " ", window)
        print(f"  [{i+1}] @char {pos}: …{window}…")


def main() -> int:
    banner(f"Phase D: Mirae listing diagnostic — fetching {LISTING_URL}")

    try:
        resp = requests.get(LISTING_URL, headers=HEADERS, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"FATAL: fetch failed: {e!r}")
        return 1

    resp.encoding = resp.apparent_encoding or "utf-8"
    body = resp.text
    print(f"HTTP {resp.status_code} | {len(body):,} chars | "
          f"encoding={resp.encoding} | "
          f"content-type={resp.headers.get('content-type', '?')}")

    # --------------------------------------------------------------------
    # Section 1 — Raw HTML window (skip head/CSS, dump rendered chunk)
    # --------------------------------------------------------------------
    banner("Section 1 — Raw HTML chars 2000-10000 (skipping head/CSS)")
    chunk = body[2000:10000]
    print("```html")
    print(chunk)
    print("```")

    # --------------------------------------------------------------------
    # Section 2 — JavaScript handler probes
    # --------------------------------------------------------------------
    banner("Section 2 — JavaScript handler probes")

    for handler in ("goView(", "goLink(", "goDetail(", "fnView(", "viewDetail("):
        positions = [m.start() for m in re.finditer(re.escape(handler), body)]
        show_context(f"`{handler}` substring", body, positions)

    # --------------------------------------------------------------------
    # Section 3 — Data-attribute survey
    # --------------------------------------------------------------------
    banner("Section 3 — Data-attribute survey")
    data_attrs = re.findall(r'data-[A-Za-z_-]+=', body)
    counts = Counter(data_attrs)
    if counts:
        print(f"Unique data-* attribute names: {len(counts)}")
        for attr, n in counts.most_common(20):
            print(f"  {n:4d} × {attr}")
    else:
        print("  (no data-* attributes found)")

    # Specifically probe the high-likelihood candidates
    for candidate in ("data-seq", "data-message-id", "data-messageid",
                      "data-id", "data-no", "data-idx", "data-key"):
        positions = [m.start() for m in re.finditer(re.escape(candidate + "="), body)]
        show_context(f"`{candidate}=` attribute", body, positions)

    # --------------------------------------------------------------------
    # Section 4 — 7-8 digit number probe (likely messageId values)
    # --------------------------------------------------------------------
    banner("Section 4 — Standalone 7-8 digit numbers (likely messageId values)")
    # Bound on both sides with non-digit so we don't pick up dates like 20260518
    long_digits = re.findall(r"(?<!\d)(\d{7,8})(?!\d)", body)
    if long_digits:
        unique_digits = Counter(long_digits)
        print(f"Unique 7-8 digit numbers: {len(unique_digits)} "
              f"(total occurrences: {len(long_digits)})")
        for n, count in unique_digits.most_common(15):
            print(f"  {count:3d} × {n}")
    else:
        print("  (no 7-8 digit numbers found anywhere)")

    # --------------------------------------------------------------------
    # Section 5 — BeautifulSoup structural probe
    # --------------------------------------------------------------------
    banner("Section 5 — BeautifulSoup structural probe — <a>, <tr>, <li> with 7-8 digit content")
    soup = BeautifulSoup(body, "html.parser")
    digit_pattern = re.compile(r"(?<!\d)\d{7,8}(?!\d)")

    for tag_name in ("a", "tr", "li"):
        matches = []
        for tag in soup.find_all(tag_name):
            tag_str = str(tag)
            if digit_pattern.search(tag_str):
                matches.append(tag_str)

        if not matches:
            print(f"\n  <{tag_name}>: 0 elements contain a 7-8 digit number")
            continue

        print(f"\n  <{tag_name}>: {len(matches)} elements contain a 7-8 digit number; first 3:")
        for i, m in enumerate(matches[:3]):
            # Truncate so each element fits on a few lines
            compact = re.sub(r"\s+", " ", m)[:600]
            print(f"  [{i+1}] {compact}")

    # --------------------------------------------------------------------
    # Section 6 — Forms (in case rows are POST submissions)
    # --------------------------------------------------------------------
    banner("Section 6 — Forms on the listing page")
    forms = soup.find_all("form")
    if forms:
        print(f"  {len(forms)} <form> element(s) found")
        for i, form in enumerate(forms[:3]):
            action = form.get("action", "(no action)")
            method = form.get("method", "(no method)")
            hidden = form.find_all("input", type="hidden")
            print(f"  Form #{i+1}: action={action!r} method={method!r} "
                  f"hidden inputs={len(hidden)}")
            for inp in hidden[:8]:
                print(f"    <input type=hidden name={inp.get('name')!r} "
                      f"value={inp.get('value')!r}>")
    else:
        print("  (no <form> elements)")

    # --------------------------------------------------------------------
    # Section 7 — Inline <script> snippets that look row-related
    # --------------------------------------------------------------------
    banner("Section 7 — Inline <script> blocks mentioning view/link/seq/message")
    scripts = soup.find_all("script")
    interesting = []
    keywords = ("goView", "goLink", "messageId", "seq", "fnView", "viewDetail")
    for s in scripts:
        text = s.string or ""
        if any(k in text for k in keywords):
            interesting.append(text)

    if interesting:
        print(f"  {len(interesting)} interesting inline scripts; first 2 (max 600 chars each):")
        for i, t in enumerate(interesting[:2]):
            compact = re.sub(r"\s+", " ", t.strip())[:600]
            print(f"  [{i+1}] {compact}")
    else:
        print("  (no inline scripts contain the keyword set — rows may be rendered "
              "client-side or hydrated via XHR; consider the network tab next)")

    banner("Phase D complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
