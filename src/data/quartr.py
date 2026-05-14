"""
Quartr API — DEPRECATED as of v3.31.

Quartr discontinued its free/self-serve developer tier. The API is now an
enterprise product ("contact us" pricing, hedge-fund / financial-platform
oriented). This was a known risk flagged in the original handover doc
("Quartr alternative if their API access becomes gated").

REPLACEMENT: src/data/transcript_sources.py
  - Direct IR-site scrapers for SK Hynix, Samsung, Micron
  - Motley Fool transcripts for US-listed names
  - EDGAR 8-K earnings exhibits (via existing EDGAR_FEEDS)

This file is kept ONLY so any lingering import doesn't crash. Both functions
below return the "skipped" shape. There is nothing to configure — QUARTR_API_KEY
is no longer used anywhere and can be removed from your environment / secrets.

If Quartr ever reintroduces an affordable tier, this file is where the real
implementation would be restored — see git history for the original.

CHANGELOG (this file):
  v3.31  (2026-05-14) — Replaced with deprecation shim — Quartr free tier discontinued; see transcript_sources.py
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("memory_cycle_brief.quartr")

# Kept for backward-compat with any code still importing it.
QUARTR_TICKERS: list[str] = []


def fetch_all_quartr() -> dict:
    """Deprecated. Returns an empty payload. Use transcript_sources instead."""
    logger.info(
        "Quartr is deprecated (v3.31). Transcripts now come from "
        "src/data/transcript_sources.py — IR scrapers + Motley Fool."
    )
    return {"fetched_at_iso": None, "tickers": {}, "upcoming_events_all": [], "deprecated": True}


def fetch_quartr_with_retry() -> tuple[Optional[dict], dict]:
    """Deprecated shim. Always returns (None, skipped)."""
    logger.info("Quartr deprecated (v3.31) — skipping. See transcript_sources.py.")
    return None, {"status": "skipped", "reason": "Quartr deprecated in v3.31 — replaced by transcript_sources"}
