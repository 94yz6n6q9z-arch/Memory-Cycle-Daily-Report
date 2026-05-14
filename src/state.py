"""
State management for the two-tier pipeline.

Three things this module handles:

  1. Idempotency — `brief_sent_dates.json` tracks which days a brief was
     successfully sent. The 11:00 / 11:30 / 12:05 UTC cron triggers all
     check this on entry; only the first to succeed produces output.

  2. Source cache — per-source successful fetches are persisted with a
     timestamp. When a fetch fails after retries, the pipeline falls back
     to the cached payload and tags the source row as STALE in the
     validation table. Brief still ships.

  3. Retry policy — per-source retry counts and backoff schedule. Three
     tiers: CRITICAL (5x, banner on fail), IMPORTANT (3x, stale fallback),
     NICE_TO_HAVE (1x, skip on fail).

State files live under `state/`:
  state/brief_sent_dates.json     — idempotency
  state/cache/<source_id>.json    — per-source cache payload + timestamp
  state/digests/{YYYY-MM-DD}/     — Layer 1 document digests (deep tier output)
  state/prior_state.json          — yesterday's analysis (for change tracking)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger("memory_cycle_brief.state")

STATE_DIR = Path("./state")
CACHE_DIR = STATE_DIR / "cache"
DIGESTS_DIR = STATE_DIR / "digests"
BRIEF_SENT_FILE = STATE_DIR / "brief_sent_dates.json"
PRIOR_STATE_FILE = STATE_DIR / "prior_state.json"


# ============================================================
# Source criticality classification
# ============================================================

class Criticality(Enum):
    """Three-tier source criticality for retry + fallback policy.

    CRITICAL    — yfinance, Anthropic API, Naver Finance. 5x retry.
                  Failure after all retries = 🚨 banner in email + brief
                  ships with degraded confidence warning.

    IMPORTANT   — Finnhub, Quartr, Mirae/NH/Samsung PDFs, EDGAR feeds,
                  DART API. 3x retry. Failure = use cached prior payload,
                  tag STALE in validation table, brief ships normally.

    NICE_TO_HAVE — RSS feeds, web scrapers, Google News queries.
                  1x retry. Failure = skip silently, validation table
                  shows the gap, brief ships normally with less coverage.
    """
    CRITICAL = "critical"
    IMPORTANT = "important"
    NICE_TO_HAVE = "nice_to_have"


# Default retry counts per criticality tier
RETRY_COUNTS = {
    Criticality.CRITICAL: 5,
    Criticality.IMPORTANT: 3,
    Criticality.NICE_TO_HAVE: 1,
}

# Per-source criticality map. Default to NICE_TO_HAVE if not listed.
# Source ID format follows the validation-table convention.
SOURCE_CRITICALITY: dict[str, Criticality] = {
    # CRITICAL — brief is degraded without these
    "yfinance / prices":                Criticality.CRITICAL,
    "yfinance / estimates":             Criticality.CRITICAL,
    "Anthropic / Haiku":                Criticality.CRITICAL,
    "Anthropic / Sonnet":               Criticality.CRITICAL,
    "Naver Finance":                    Criticality.CRITICAL,

    # IMPORTANT — stale cached payload acceptable on failure
    "Finnhub":                          Criticality.IMPORTANT,
    "Quartr":                           Criticality.IMPORTANT,
    "Mirae Asset (EN)":                 Criticality.IMPORTANT,
    "NH Investment":                    Criticality.IMPORTANT,
    "Samsung Securities (Gmail)":       Criticality.IMPORTANT,
    "DART API":                         Criticality.IMPORTANT,
    "SEC EDGAR / SK Hynix F-1":         Criticality.IMPORTANT,
    "SEC EDGAR / SK Hynix F-1/A":       Criticality.IMPORTANT,
    "SEC EDGAR / SK Hynix 424":         Criticality.IMPORTANT,
    "SEC EDGAR / SK Hynix 6-K":         Criticality.IMPORTANT,
    "SEC EDGAR / SK Hynix 20-F":        Criticality.IMPORTANT,
    "SEC EDGAR / Micron Form 4":        Criticality.IMPORTANT,
    "Alpha Vantage":                    Criticality.IMPORTANT,
    # RSS / scrapers default to NICE_TO_HAVE
}


# ============================================================
# Idempotency
# ============================================================

def has_brief_been_sent_today(today: Optional[date] = None) -> bool:
    """Return True if a brief was already successfully sent today.

    Used at the entry of each fast-tier cron run (11:00 / 11:30 / 12:05 UTC).
    The first run to succeed marks the date; subsequent runs exit early.
    """
    today = today or date.today()
    if not BRIEF_SENT_FILE.exists():
        return False
    try:
        with open(BRIEF_SENT_FILE) as f:
            data = json.load(f)
        return today.isoformat() in data.get("dates", [])
    except Exception as e:
        logger.warning(f"Could not read {BRIEF_SENT_FILE}: {e}")
        return False  # err on the side of sending


def mark_brief_sent_today(today: Optional[date] = None) -> None:
    """Mark today's brief as sent. Called after successful email send."""
    today = today or date.today()
    STATE_DIR.mkdir(exist_ok=True)
    data = {"dates": []}
    if BRIEF_SENT_FILE.exists():
        try:
            with open(BRIEF_SENT_FILE) as f:
                data = json.load(f)
        except Exception:
            pass
    if today.isoformat() not in data["dates"]:
        data["dates"].append(today.isoformat())
        # Keep only last 90 days for hygiene
        cutoff = (today - timedelta(days=90)).isoformat()
        data["dates"] = [d for d in data["dates"] if d >= cutoff]
        with open(BRIEF_SENT_FILE, "w") as f:
            json.dump(data, f, indent=2)
    logger.info(f"Marked {today.isoformat()} as brief-sent.")


# ============================================================
# Source cache (stale-fallback support)
# ============================================================

@dataclass
class CachedFetch:
    """A successfully-fetched payload + when it was fetched."""
    source_id: str
    payload: Any  # JSON-serializable
    fetched_at_iso: str

    @property
    def fetched_at(self) -> datetime:
        return datetime.fromisoformat(self.fetched_at_iso)

    @property
    def age_days(self) -> int:
        return (datetime.utcnow() - self.fetched_at).days


def _cache_path(source_id: str) -> Path:
    """Map source ID to a filesystem-safe cache file path."""
    safe = source_id.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    return CACHE_DIR / f"{safe}.json"


def cache_fetch(source_id: str, payload: Any) -> None:
    """Persist a successful fetch's payload for future stale-fallback use."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CachedFetch(
        source_id=source_id,
        payload=payload,
        fetched_at_iso=datetime.utcnow().isoformat(),
    )
    try:
        with open(_cache_path(source_id), "w") as f:
            json.dump({
                "source_id": cached.source_id,
                "payload": cached.payload,
                "fetched_at_iso": cached.fetched_at_iso,
            }, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Could not cache {source_id}: {e}")


def load_cached(source_id: str) -> Optional[CachedFetch]:
    """Load the most recent cached payload for a source, or None if absent."""
    path = _cache_path(source_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return CachedFetch(**data)
    except Exception as e:
        logger.warning(f"Could not load cache for {source_id}: {e}")
        return None


# ============================================================
# Retry with backoff
# ============================================================

T = TypeVar("T")


def fetch_with_retry(
    source_id: str,
    fetch_fn: Callable[[], T],
    criticality: Optional[Criticality] = None,
    base_delay: float = 1.0,
) -> tuple[Optional[T], dict[str, Any]]:
    """Execute `fetch_fn` with retry + cached-fallback policy.

    Returns (payload_or_None, metadata) where metadata describes:
      - attempts: how many retries were used
      - status: "ok" | "cached_stale" | "failed"
      - cache_age_days: int (only if status == cached_stale)
      - error: str (only if status == failed)

    The validation table is built from this metadata.
    """
    if criticality is None:
        criticality = SOURCE_CRITICALITY.get(source_id, Criticality.NICE_TO_HAVE)
    max_attempts = RETRY_COUNTS[criticality]

    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            payload = fetch_fn()
            cache_fetch(source_id, payload)
            return payload, {
                "attempts": attempt,
                "status": "ok",
                "criticality": criticality.value,
            }
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                delay = base_delay * (3 ** (attempt - 1))  # 1s, 3s, 9s, 27s, 81s
                logger.warning(
                    f"{source_id} attempt {attempt}/{max_attempts} failed ({e}); "
                    f"retrying in {delay:.0f}s"
                )
                time.sleep(delay)

    # All retries exhausted. Try cached fallback for IMPORTANT and CRITICAL.
    if criticality in (Criticality.IMPORTANT, Criticality.CRITICAL):
        cached = load_cached(source_id)
        if cached is not None:
            logger.warning(
                f"{source_id} failed after {max_attempts} attempts; "
                f"using stale cache from {cached.age_days} day(s) ago"
            )
            return cached.payload, {
                "attempts": max_attempts,
                "status": "cached_stale",
                "cache_age_days": cached.age_days,
                "criticality": criticality.value,
                "error": str(last_error),
            }

    # Total failure (no cache available, or NICE_TO_HAVE that we don't fall back for)
    logger.error(f"{source_id} failed after {max_attempts} attempts; no fallback")
    return None, {
        "attempts": max_attempts,
        "status": "failed",
        "criticality": criticality.value,
        "error": str(last_error),
    }


# ============================================================
# Digest directory helpers (Layer 1 outputs from deep tier)
# ============================================================

def digests_dir_for(d: Optional[date] = None) -> Path:
    """Return state/digests/{YYYY-MM-DD}/ for the given date (default: today)."""
    d = d or date.today()
    p = DIGESTS_DIR / d.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_digest(name: str, payload: dict, d: Optional[date] = None) -> Path:
    """Write a Layer 1 digest JSON. Returns the path."""
    out = digests_dir_for(d) / f"{name}.json"
    with open(out, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return out


def read_digests_in_window(days_back: int = 2) -> list[dict]:
    """Load all digests from the last `days_back` days.

    Fast tier calls this to gather Layer 1 outputs from the deep tier
    (typically same day or yesterday if deep tier failed).
    """
    out: list[dict] = []
    today = date.today()
    for delta in range(days_back + 1):
        d = today - timedelta(days=delta)
        d_dir = DIGESTS_DIR / d.isoformat()
        if not d_dir.exists():
            continue
        for f in sorted(d_dir.glob("*.json")):
            try:
                with open(f) as fh:
                    digest = json.load(fh)
                digest["_digest_date"] = d.isoformat()
                digest["_digest_age_days"] = delta
                out.append(digest)
            except Exception as e:
                logger.warning(f"Could not load digest {f}: {e}")
    return out
