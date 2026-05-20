"""
Phase E dry-run — exercise the v3.39.4 Mirae HTML scraper end-to-end against
production Mirae, with full verbosity, before committing v3.39.4 to deep_tier.

Goal: prove the scraper works on real data, not just on the synthetic fixtures
embedded in tests/test_mirae_asset.py. Two modes:

  Default mode  — fetch today's listing, parse it, run the date filter
                  (widened to days_back=14 so May 11 SK Hynix is in window if
                  still on page 1), run the relevance filter, fetch each
                  relevant detail page, print every extracted field. Useful
                  as a "show me what production would see" preview.

  Targeted mode — if env var DRY_RUN_MESSAGE_ID is set (e.g. via the workflow
                  input), skip the listing entirely and just fetch +
                  parse that one detail page. Useful to validate against a
                  specific known note like SK Hynix Oct 30 (2335435).

The script writes NOTHING to repo state — it doesn't commit, doesn't update
state/cache except for an ephemeral HTML cache that gets discarded with the
runner. Safe to run as many times as you want.

CHANGELOG (this file):
  v3.39.4 (2026-05-18) — NEW. Phase E pre-deployment validation harness.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, timedelta


# --- Path + dependency setup -------------------------------------------------
# Run from repo root in the workflow. Allow direct invocation locally too.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Stub src.state so we don't trip its retry/cache machinery — the dry-run
# wants to see the RAW behavior of fetch_mirae_notes' inner helpers without
# the retry wrapper masking transient errors.
if "src.state" not in sys.modules:
    state_stub = types.ModuleType("src.state")

    class _Criticality:
        CRITICAL = "CRITICAL"
        IMPORTANT = "IMPORTANT"

    def _stub_fetch_with_retry(**kwargs):
        return (None, {})

    state_stub.Criticality = _Criticality
    state_stub.fetch_with_retry = _stub_fetch_with_retry
    sys.modules["src.state"] = state_stub

# Now we can import the real scraper module without dragging in src.deep_tier
# or anthropic etc.
from src.data.korean_brokers import (
    HEADERS,
    MIRAE_RESEARCH_DETAIL_EN_TEMPLATE,
    MIRAE_RESEARCH_LISTING_EN,
    REQUEST_TIMEOUT_SEC,
    TRACKED_KOREAN_TICKER_CODES,
    TRACKED_TITLE_KEYWORDS,
    MiraeListingRow,
    _extract_korean_ticker_from_title,
    _fetch_mirae_detail,
    _parse_mirae_detail,
    _parse_mirae_listing,
    _row_is_relevant,
)
import requests


# --- Output helpers ----------------------------------------------------------

def banner(title: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{title}\n{bar}")


def show_row(prefix: str, row: MiraeListingRow) -> None:
    """Compact one-line row dump for log readability."""
    rating_hint = ""
    if row.title:
        # Extract rating from "(NNNNNN KS/Rating)" if present
        import re as _re
        m = _re.search(r"/([A-Za-z][A-Za-z\s]*?)\)", row.title)
        if m:
            rating_hint = f" rating={m.group(1).strip()}"
    print(
        f"  {prefix} msgId={row.message_id} "
        f"date={row.published_date_iso} "
        f"ticker={row.ticker_code_korean or '----':6s} "
        f"category={row.category[:18]:18s} "
        f"author={row.author[:20]:20s} "
        f"attach={row.attachment_id or '----'}"
        f"{rating_hint}"
    )
    print(f"      title=    {row.title}")
    if row.subtitle:
        print(f"      subtitle= {row.subtitle}")


def show_extracted_note(note: dict, idx: int) -> None:
    print(f"\n  [Note #{idx}]")
    print(f"    title:        {note.get('title')}")
    print(f"    author:       {note.get('author')}")
    print(f"    published:    {note.get('published_date_iso')}")
    print(f"    stock_code:   {note.get('stock_code')}")
    print(f"    rating:       {note.get('rating')}")
    print(f"    target_price: {note.get('target_price_krw')}")
    print(f"    category:     {note.get('category')}")
    print(f"    url:          {note.get('url')}")
    print(f"    msgId:        {note.get('message_id')}")
    print(f"    attachId:     {note.get('attachment_id')}")
    body = note.get("extracted_text") or ""
    print(f"    body_length:  {len(body)} chars")
    if body:
        preview = body[:600].replace("\n", " | ")
        print(f"    body[:600]:   {preview}")


# --- Targeted-mode -----------------------------------------------------------

def targeted_mode(message_id: str) -> int:
    banner(f"Targeted mode — fetching detail page for messageId={message_id}")

    # Build a minimal MiraeListingRow and call the real _fetch_mirae_detail
    # so the dry-run goes through EXACTLY the same code path production will.
    row = MiraeListingRow(
        message_id=message_id,
        attachment_id=None,
        category="(targeted dry-run — category not from listing)",
        title="(targeted dry-run — title not from listing)",
        subtitle="",
        author="(unknown — bypassed listing)",
        published_date_iso=date.today().isoformat(),
        ticker_code_korean=None,
    )

    note = _fetch_mirae_detail(row)
    if note is None:
        print("\n  FATAL: _fetch_mirae_detail returned None (HTTP error).")
        return 1
    show_extracted_note(note, 1)

    body_len = len(note.get("extracted_text") or "")
    banner("Targeted run summary")
    if body_len >= 200:
        print(f"  ✅ Body is {body_len} chars — would clear the digester's 200-char minimum.")
    else:
        print(f"  ⚠️  Body is only {body_len} chars — digester would reject this note.")
    return 0


# --- Default mode ------------------------------------------------------------

def default_mode(days_back: int) -> int:
    banner(f"Default mode — listing fetch + filter + detail fetches (days_back={days_back})")

    # ---- Step 1: listing fetch (use plain requests so we can show response details
    # before handing off to the parser) ----
    print(f"\n  GET {MIRAE_RESEARCH_LISTING_EN}")
    try:
        resp = requests.get(MIRAE_RESEARCH_LISTING_EN, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
    except Exception as e:
        print(f"  FATAL: listing fetch failed: {e!r}")
        return 1

    resp.encoding = resp.apparent_encoding or "euc-kr"
    print(f"  HTTP {resp.status_code} | {len(resp.text):,} chars | "
          f"encoding={resp.encoding} | "
          f"content-type={resp.headers.get('content-type', '?')}")

    # ---- Step 2: parse listing ----
    banner("Parsed listing rows (full set, before any filter)")
    rows = _parse_mirae_listing(resp.text)
    print(f"  Total rows parsed: {len(rows)}")
    for r in rows:
        show_row("•", r)

    if not rows:
        print("\n  ⚠️  Zero rows parsed — selector may have changed since Phase D.")
        print("  Re-run scripts/diagnose_mirae_listing.py and compare.")
        return 1

    # ---- Step 3: date filter ----
    cutoff_iso = (date.today() - timedelta(days=days_back)).isoformat()
    banner(f"After date filter (cutoff {cutoff_iso}, days_back={days_back})")
    recent = [r for r in rows if r.published_date_iso >= cutoff_iso]
    print(f"  Rows surviving date filter: {len(recent)} / {len(rows)}")
    for r in rows:
        verdict = "KEEP" if r.published_date_iso >= cutoff_iso else "skip"
        print(f"  {verdict:5s} {r.published_date_iso}  {r.title[:70]}")

    # ---- Step 4: relevance filter ----
    banner("After relevance filter (ticker code OR keyword match)")
    print(f"  Tracked tickers: {sorted(TRACKED_KOREAN_TICKER_CODES)}")
    print(f"  Tracked keywords: {list(TRACKED_TITLE_KEYWORDS)}")
    print()
    relevant = []
    for r in recent:
        if r.ticker_code_korean and r.ticker_code_korean in TRACKED_KOREAN_TICKER_CODES:
            reason = f"ticker-match ({r.ticker_code_korean})"
            relevant.append(r)
        elif _row_is_relevant(r):
            haystack = f"{r.title} {r.subtitle}".lower()
            hit_keyword = next(k for k in TRACKED_TITLE_KEYWORDS if k in haystack)
            reason = f"keyword-match ({hit_keyword!r})"
            relevant.append(r)
        else:
            reason = "rejected (no ticker, no keyword)"
        print(f"  {('KEEP' if r in relevant else 'skip'):5s} {reason:35s}  {r.title[:60]}")

    print(f"\n  Rows surviving relevance filter: {len(relevant)} / {len(recent)}")

    if not relevant:
        banner("Default-run summary")
        print(f"  ℹ️  Zero relevant rows in the {days_back}-day window.")
        print(f"  This is a legitimate empty day if Mirae didn't publish any tracked-ticker")
        print(f"  or memory/HBM/DRAM-keyword notes recently. Try widening days_back, or")
        print(f"  pass DRY_RUN_MESSAGE_ID for a specific known note.")
        return 0

    # ---- Step 5: detail fetches ----
    # In production, fetch_mirae_notes caps at MAX_DETAIL_FETCHES_PER_RUN=5.
    # The dry-run lifts that cap so we can see everything in this window.
    banner(f"Detail fetches (all {len(relevant)} relevant rows, dry-run lifts the 5-cap)")
    notes = []
    for i, row in enumerate(relevant, 1):
        url = MIRAE_RESEARCH_DETAIL_EN_TEMPLATE.format(message_id=row.message_id)
        print(f"\n  [{i}/{len(relevant)}] GET {url}")
        note = _fetch_mirae_detail(row)
        if note is None:
            print(f"      FAILED (HTTP error or fetch exception)")
            continue
        notes.append(note)
        show_extracted_note(note, i)

    # ---- Step 6: summary ----
    banner("Default-run summary")
    digester_ok = [n for n in notes if len(n.get("extracted_text") or "") >= 200]
    print(f"  Listing rows:                {len(rows)}")
    print(f"  After date filter ({days_back}d):    {len(recent)}")
    print(f"  After relevance filter:      {len(relevant)}")
    print(f"  Detail pages fetched:        {len(notes)}")
    print(f"  Body ≥ 200 chars (digester-ready): {len(digester_ok)}")
    print()
    if digester_ok:
        print("  Per-note headline fields:")
        for n in digester_ok:
            print(f"    {n.get('published_date_iso')} | "
                  f"{(n.get('stock_code') or '------'):6s} | "
                  f"{(n.get('rating') or 'n/a'):10s} | "
                  f"TP={n.get('target_price_krw') or 'n/a'} | "
                  f"{(n.get('title') or '')[:50]}")
    return 0


# --- Entry point -------------------------------------------------------------

def main() -> int:
    target_msg_id = (os.environ.get("DRY_RUN_MESSAGE_ID") or "").strip()
    if target_msg_id:
        return targeted_mode(target_msg_id)

    raw_days = (os.environ.get("DRY_RUN_DAYS_BACK") or "14").strip()
    try:
        days_back = max(1, int(raw_days))
    except ValueError:
        days_back = 14
    return default_mode(days_back)


if __name__ == "__main__":
    sys.exit(main())
