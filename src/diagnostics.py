"""
Pipeline data-source diagnostics.

Every external fetch (RSS, scraper, EDGAR, yfinance, FMP, Anthropic API)
records a SourceFetchStatus into a shared collector. The email template
renders these as a validation table at the bottom of the brief, so we
can verify each source was actually reached and parsed — instead of
silently failing for months while the brief looks healthy.

Usage:

    collector = DiagnosticsCollector()
    with collector.track("TrendForce RSS", "rss") as t:
        items = feedparser.parse(url)
        t.items_returned = len(items.entries)

If the block raises, the status is recorded as `reached=False` with the
error message. Otherwise `reached=True` and `items_returned` defaults
to 0 unless the caller sets it.

CHANGELOG (this file):
  v3.38  (2026-05-17) — Three new helpers added at module bottom for cross-tier diagnostics. (a) `status_from_retry_meta(source_name, source_type, payload, meta, items_count, items_detail)` — converts a fetch_with_retry result (payload, meta) tuple into a SourceFetchStatus, mapping meta.status (ok / cached_stale / failed / skipped / unknown) to the right health flag and detail string. Used by both fast tier and deep tier for any fetch_*_with_retry call. cached_stale stays health=ok but includes a loud "USING STALE CACHE — N day(s) old" in detail. (b) `statuses_to_dict_list(statuses)` — serialize for JSON persistence across pipeline tiers. (c) `statuses_from_dict_list(payload)` — re-hydrate; tolerates missing/malformed entries by skipping them rather than raising.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SourceFetchStatus:
    """A single source-fetch outcome captured at runtime."""
    source_name: str       # human label, e.g. "TrendForce RSS"
    source_type: str       # "rss" | "google_news" | "scraper" | "edgar" | "yfinance" | "fmp" | "llm"
    reached: bool          # did the request complete without raising
    items_returned: int    # rows / articles / data points; 0 = nothing came back
    latency_ms: int
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: Optional[str] = None  # optional short note (e.g. "skipped, no API key")
    skipped: bool = False         # True if intentionally not called (e.g. no API key)

    @property
    def health(self) -> str:
        """Four-state health classification used by the email template.

        - 'skipped' = intentionally not queried (e.g. FMP with no API key);
          surfaces in the validation table so a never-configured source
          can't hide for months
        - 'fail'    = network/auth/parse error
        - 'warn'    = reached but returned 0 items (could be legitimate empty
          window, or a parsing issue worth investigating)
        - 'ok'      = reached and returned items
        """
        if self.skipped:
            return "skipped"
        if not self.reached:
            return "fail"
        if self.items_returned == 0:
            return "warn"
        return "ok"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["health"] = self.health
        return d


class _TrackHandle:
    """Mutable handle returned from `track()` so callers can set items_returned."""
    def __init__(self):
        self.items_returned: int = 0
        self.detail: Optional[str] = None


class DiagnosticsCollector:
    """Collects SourceFetchStatus entries across the pipeline run."""

    def __init__(self):
        self.statuses: list[SourceFetchStatus] = []

    def record(self, status: SourceFetchStatus) -> None:
        self.statuses.append(status)

    def record_skipped(self, source_name: str, source_type: str, reason: str) -> None:
        """For sources intentionally not queried (e.g. FMP without API key)."""
        self.statuses.append(SourceFetchStatus(
            source_name=source_name,
            source_type=source_type,
            reached=False,
            items_returned=0,
            latency_ms=0,
            error=None,
            detail=f"skipped: {reason}",
            skipped=True,
        ))

    @contextmanager
    def track(self, source_name: str, source_type: str):
        """Context manager that times the block and records outcome.

        Yields a handle on which the caller sets `.items_returned`.
        Re-raises after recording a failure status, so caller logic
        is unchanged from the non-instrumented version.
        """
        handle = _TrackHandle()
        start = time.monotonic()
        error: Optional[str] = None
        reached = True
        try:
            yield handle
        except Exception as e:
            reached = False
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            self.statuses.append(SourceFetchStatus(
                source_name=source_name,
                source_type=source_type,
                reached=reached,
                items_returned=handle.items_returned,
                latency_ms=latency_ms,
                error=error,
                detail=handle.detail,
            ))

    def all(self) -> list[SourceFetchStatus]:
        return list(self.statuses)

    def summary_counts(self) -> dict[str, int]:
        ok = sum(1 for s in self.statuses if s.health == "ok")
        warn = sum(1 for s in self.statuses if s.health == "warn")
        fail = sum(1 for s in self.statuses if s.health == "fail")
        skipped = sum(1 for s in self.statuses if s.health == "skipped")
        return {
            "ok": ok, "warn": warn, "fail": fail, "skipped": skipped,
            "total": len(self.statuses),
        }


# ============================================================
# v3.38 — helpers for the (payload, meta) wrapper convention
# ============================================================

def status_from_retry_meta(
    source_name: str,
    source_type: str,
    payload,
    meta: dict,
    items_count: int = 0,
    items_detail: Optional[str] = None,
) -> SourceFetchStatus:
    """Build a SourceFetchStatus from a fetch_with_retry-style (payload, meta) result.

    Wrappers like fetch_naver_with_retry / fetch_finnhub_with_retry /
    fetch_dart_with_retry / fetch_*_with_retry in korean_brokers return
    (payload_or_None, meta_dict) where meta has:
      - status: "ok" | "cached_stale" | "failed" | "skipped" | "error"
      - attempts: int
      - cache_age_days: int (if cached_stale)
      - error: str (if failed)
      - reason: str (if skipped)

    This converts that into a SourceFetchStatus with the right health flag.

    Note on cached_stale: classified as health="ok" because we have data,
    but the detail field makes the stale fetch loud — "USING STALE CACHE (N
    days old)" — so the validation table reader sees it. The fail count
    stays clean for genuine failures.
    """
    status = meta.get("status", "unknown")
    attempts = meta.get("attempts", 1)

    if status == "ok":
        return SourceFetchStatus(
            source_name=source_name, source_type=source_type,
            reached=True, items_returned=items_count, latency_ms=0,
            detail=items_detail or (f"{attempts} attempt(s)" if attempts > 1 else None),
        )
    if status == "cached_stale":
        live_err = (meta.get("error") or "")[:80]
        return SourceFetchStatus(
            source_name=source_name, source_type=source_type,
            reached=True, items_returned=items_count, latency_ms=0,
            detail=(f"USING STALE CACHE — {meta.get('cache_age_days', '?')} day(s) old"
                    f" (live fetch failed: {live_err})"),
        )
    if status == "skipped":
        return SourceFetchStatus(
            source_name=source_name, source_type=source_type,
            reached=False, items_returned=0, latency_ms=0,
            detail=f"skipped: {meta.get('reason', 'no reason given')}",
            skipped=True,
        )
    # status in {"failed", "error", anything else}
    return SourceFetchStatus(
        source_name=source_name, source_type=source_type,
        reached=False, items_returned=0, latency_ms=0,
        error=(meta.get("error") or meta.get("reason") or status),
        detail=f"failed after {attempts} attempt(s)",
    )


def statuses_to_dict_list(statuses: list[SourceFetchStatus]) -> list[dict]:
    """Serialize a list of statuses for cross-tier JSON persistence."""
    return [s.to_dict() for s in statuses]


def statuses_from_dict_list(payload: list[dict]) -> list[SourceFetchStatus]:
    """Re-hydrate a list of statuses written by `statuses_to_dict_list`.

    Drops the computed `health` key (recomputed from the other fields).
    Tolerates missing keys (returns empty list if shape is wrong).
    """
    out: list[SourceFetchStatus] = []
    for d in payload or []:
        if not isinstance(d, dict):
            continue
        try:
            kwargs = {k: v for k, v in d.items() if k != "health"}
            out.append(SourceFetchStatus(**kwargs))
        except (TypeError, ValueError):
            continue
    return out
