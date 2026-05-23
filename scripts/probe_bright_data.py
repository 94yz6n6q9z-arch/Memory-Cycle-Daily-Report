#!/usr/bin/env python3
"""v3.4.10 — Standalone Bright Data residential proxy probe.

Manual diagnostic: hits Yahoo Finance earnings-calls index URLs for 5
tickers through the Bright Data residential proxy (no caching, no
seen_urls short-circuit, no transcript-window gate). Reports per-ticker:

  - HTTP status code
  - Response time (ms)
  - Body length (bytes)
  - Anchor count + whether a transcript link was found in the index
  - Exception class + message (if the fetch failed)

Use this to differentiate "Bright Data is broken systemically" from
"Yahoo blocked one specific ticker." Output is concise enough to paste
into a chat or capture in a GitHub Actions log.

USAGE
-----
Local:
    export RESIDENTIAL_PROXY_URL='http://USER:PASS@brd.superproxy.io:33335'
    python scripts/probe_bright_data.py

In CI (GitHub Actions): run via `workflow_dispatch` from the
"Bright Data probe" workflow file (.github/workflows/probe_bright_data.yml).
The workflow injects RESIDENTIAL_PROXY_URL from repository secrets.

EXIT CODES
----------
    0  → all 5 tickers returned HTTP 200 with non-empty body (proxy healthy)
    1  → at least one ticker failed (proxy degraded or per-ticker block)
    2  → RESIDENTIAL_PROXY_URL not set (configuration error)
"""

from __future__ import annotations

import os
import random
import sys
import time
from typing import Optional


# Probe tickers — mix of large-cap US names with stable Yahoo coverage.
# Deliberately includes the ticker that triggered the v3.4.10 investigation
# (ORCL) so we can confirm whether it was a transient or persistent issue.
PROBE_TICKERS = [
    ("Oracle",       "ORCL"),
    ("Nvidia",       "NVDA"),
    ("Micron",       "MU"),
    ("Microsoft",    "MSFT"),
    ("Applied Mat.", "AMAT"),
]

YAHOO_EARNINGS_INDEX = "https://finance.yahoo.com/calendar/earnings?symbol={ticker}"
REQUEST_TIMEOUT_SEC = 30
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}


def _inject_session_id(proxy_url: str, session_id: str) -> str:
    """Add `-session-{id}` to the proxy username. Mirrors the production
    helper in src/data/transcript_sources.py._http_get so we exercise the
    same code path."""
    try:
        scheme, rest = proxy_url.split("://", 1)
        userpass, host_part = rest.split("@", 1)
        user, _, password = userpass.partition(":")
        user = f"{user}-session-{session_id}"
        return f"{scheme}://{user}:{password}@{host_part}"
    except (ValueError, IndexError):
        # If parsing fails, return original URL (probe will run, just without rotation)
        return proxy_url


def probe_one(company: str, ticker: str, proxy_url: str) -> dict:
    """Run one Yahoo earnings-index fetch through Bright Data. Returns a
    structured result dict — never raises (catches all exceptions)."""
    import requests

    url = YAHOO_EARNINGS_INDEX.format(ticker=ticker)
    session_id = str(random.randint(100000, 999999))
    proxy_with_session = _inject_session_id(proxy_url, session_id)
    proxies = {"http": proxy_with_session, "https": proxy_with_session}

    result = {
        "company": company,
        "ticker": ticker,
        "url": url,
        "session_id": session_id,
        "status": None,
        "elapsed_ms": None,
        "body_length": None,
        "anchor_count": None,
        "has_transcript_link": None,
        "exception": None,
    }

    t0 = time.time()
    try:
        r = requests.get(
            url,
            timeout=REQUEST_TIMEOUT_SEC,
            headers=BROWSER_HEADERS,
            proxies=proxies,
            allow_redirects=True,
            verify=False,  # Bright Data intercepts HTTPS with its own cert
        )
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        result["status"] = r.status_code
        body = r.text or ""
        result["body_length"] = len(body)

        # Light parse — count anchors and check for transcript link without bs4 dep.
        # Real production code uses bs4; for probe we just want a sanity signal.
        body_lower = body.lower()
        result["anchor_count"] = body_lower.count("<a ")
        result["has_transcript_link"] = (
            "/earnings/" in body_lower and "earnings_call" in body_lower
        )
    except Exception as e:
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        result["exception"] = f"{type(e).__name__}: {e}"

    return result


def fmt_row(r: dict) -> str:
    """Format one probe result as a single readable line."""
    status_str = (
        f"HTTP {r['status']}" if r["status"] is not None
        else "FAIL"
    )
    body_str = (
        f"{r['body_length']:>7,}B" if r["body_length"] is not None
        else "    n/a"
    )
    anchors = r["anchor_count"]
    anchors_str = f"{anchors:>4}" if anchors is not None else " n/a"
    has_link = (
        "✓transcript" if r["has_transcript_link"]
        else ("no transcript" if r["has_transcript_link"] is False else "  n/a")
    )
    excerpt = ""
    if r["exception"]:
        # Trim long exception strings to keep the line readable
        e = r["exception"]
        excerpt = f"  EXC: {e[:120]}{'...' if len(e) > 120 else ''}"
    return (
        f"  {r['ticker']:<6} {status_str:<10} {r['elapsed_ms']:>5}ms  "
        f"body={body_str}  anchors={anchors_str}  {has_link}{excerpt}"
    )


def main() -> int:
    proxy_url = os.environ.get("RESIDENTIAL_PROXY_URL", "").strip()
    if not proxy_url:
        print("ERROR: RESIDENTIAL_PROXY_URL not set in env", file=sys.stderr)
        print(
            "Set the env var before running:\n"
            "  export RESIDENTIAL_PROXY_URL='http://USER:PASS@brd.superproxy.io:33335'",
            file=sys.stderr,
        )
        return 2

    # Mask the URL for logging (don't leak the password)
    try:
        scheme, rest = proxy_url.split("://", 1)
        userpass, host_part = rest.split("@", 1)
        user, _, _password = userpass.partition(":")
        masked = f"{scheme}://{user}:***@{host_part}"
    except (ValueError, IndexError):
        masked = "(unparseable proxy URL)"

    print("=" * 72)
    print("Bright Data Yahoo Finance Probe (v3.4.10)")
    print("=" * 72)
    print(f"Proxy:   {masked}")
    print(f"Tickers: {', '.join(t for _, t in PROBE_TICKERS)}")
    print()
    print(f"  {'TICKER':<6} {'STATUS':<10} {'TIME':>7}  {'BODY':>14}  {'ANCH':<10}  TRANSCRIPT")
    print("  " + "-" * 70)

    results = []
    for i, (company, ticker) in enumerate(PROBE_TICKERS):
        if i > 0:
            time.sleep(2)  # gentle inter-ticker delay to avoid hammering the proxy
        r = probe_one(company, ticker, proxy_url)
        results.append(r)
        print(fmt_row(r))

    print()
    # Summary
    ok = sum(1 for r in results if r["status"] == 200 and (r["body_length"] or 0) > 1000)
    failed = len(results) - ok
    print(f"SUMMARY: {ok}/{len(results)} OK (HTTP 200, body >1KB), {failed} degraded/failed")

    if failed == 0:
        print("→ Bright Data proxy is HEALTHY. Any specific-ticker Yahoo failures")
        print("  in the production pipeline are likely per-ticker (URL pattern,")
        print("  ticker-specific Yahoo block, or transcript page structure).")
        return 0
    elif ok == 0:
        print("→ Bright Data proxy appears BROKEN for all probe tickers. Check:")
        print("  - Bright Data account credit / billing status")
        print("  - RESIDENTIAL_PROXY_URL secret is current (rotates rarely)")
        print("  - Bright Data status page")
        return 1
    else:
        print(f"→ MIXED: {ok} OK, {failed} failed. Likely Yahoo tightening per-ticker")
        print("  detection. Look at the failed tickers — patterns?")
        return 1


if __name__ == "__main__":
    sys.exit(main())
