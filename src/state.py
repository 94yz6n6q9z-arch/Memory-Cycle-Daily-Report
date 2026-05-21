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
# v3.39.7.5 — 24h ephemeral trigger event log. Each indicator status
# transition (e.g., t1_demand_destruction: Not fired → Fired) gets one
# entry keyed on (indicator_id, new_status), timestamped with when the
# transition was FIRST observed within the rolling 24h window. Lets
# multiple manual brief reruns during the day keep showing the same
# trigger callouts (which would otherwise disappear after the first run
# updated prior_state.json). Entries auto-prune past 24h on every load.
TRIGGER_EVENTS_24H_FILE = STATE_DIR / "trigger_events_24h.json"


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
# 24h ephemeral trigger event log (v3.39.7.5)
# ============================================================
#
# Problem solved: with idempotent reruns, the prior brief-sent path was
# "compute trigger events as diff(today, prior_state)". This meant the
# FIRST run of the day computed the diff correctly, persisted prior_state,
# and on subsequent manual reruns the diff was empty (because prior_state
# already reflected today's status). Users running multiple manual briefs
# during the day would see "No new trigger events today" on every rerun
# after the first.
#
# The 24h log decouples display from prior_state. Each transition gets
# logged on first observation; subsequent reruns within 24h read from the
# log and re-display the same events. Tomorrow's first run sees them
# pruned out (>24h old) so they become "the new normal" naturally without
# needing a separate "clear on calendar day change" mechanism.
#
# Dedup key: (indicator_id, new_status). So if t1_demand_destruction
# transitions Not fired → Fired in the morning, then later in the same
# day transitions Fired → Partially fired, BOTH entries appear (different
# new_status). If the morning's transition is logged twice (e.g., due to
# a race), only the first sticks (same key).


def load_trigger_log_24h(now_utc: Optional[datetime] = None) -> list[dict]:
    """Load the 24h ephemeral trigger event log, pruning entries older than 24h.

    Returns a list of dicts: each has keys {indicator_id, indicator_name,
    tier, old_status, new_status, description, fired_at_iso}. The
    fired_at_iso is the timestamp of FIRST observation within the rolling
    24h window (so the same event displayed across multiple manual reruns
    keeps a stable timestamp).

    `now_utc` defaults to datetime.utcnow() — overridable for testing.
    Missing file or parse error → returns empty list (logged at warn).
    """
    if now_utc is None:
        now_utc = datetime.utcnow()
    if not TRIGGER_EVENTS_24H_FILE.exists():
        return []
    try:
        with open(TRIGGER_EVENTS_24H_FILE) as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load {TRIGGER_EVENTS_24H_FILE}: {e}")
        return []
    if not isinstance(entries, list):
        logger.warning(
            f"{TRIGGER_EVENTS_24H_FILE} root is not a list (got {type(entries).__name__}); "
            f"treating as empty"
        )
        return []
    cutoff_iso = (now_utc - timedelta(hours=24)).isoformat()
    pruned = [
        e for e in entries
        if isinstance(e, dict) and e.get("fired_at_iso", "") > cutoff_iso
    ]
    return pruned


def save_trigger_log_24h(entries: list[dict]) -> None:
    """Persist the trigger event log. Caller pre-prunes to last 24h."""
    STATE_DIR.mkdir(exist_ok=True)
    try:
        with open(TRIGGER_EVENTS_24H_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except OSError as e:
        logger.warning(f"Could not write {TRIGGER_EVENTS_24H_FILE}: {e}")


def update_trigger_log_24h(
    prior_status_by_id: dict[str, str],
    current_assessments: list,
    now_utc: Optional[datetime] = None,
) -> list[dict]:
    """Merge today's observed transitions into the 24h log, prune, save, return.

    Args:
        prior_status_by_id: maps indicator_id → prior current_status string
            (e.g. {"t1_demand_destruction": "Not fired", ...}). Built from
            prior_state.json by the caller — passed in instead of read here
            to avoid a circular import on state.py.
        current_assessments: list of IndicatorAssessment-like objects from
            the just-computed brief. Each must have .id, .name, .tier,
            .current_status. Pass directly from analysis.indicator_assessments.
        now_utc: optional datetime override for testing.

    Returns the final pruned + saved log — what should be passed to
    ExecutiveSummary.new_triggers_today. Each dict has keys: indicator_id,
    indicator_name, tier, old_status, new_status, description, fired_at_iso.

    The dedup key is (indicator_id, new_status). Same event observed across
    multiple reruns within 24h keeps the first fired_at_iso; the entry
    is NOT re-timestamped.
    """
    if now_utc is None:
        now_utc = datetime.utcnow()

    log = load_trigger_log_24h(now_utc=now_utc)
    existing_keys = {(e["indicator_id"], e["new_status"]) for e in log}
    now_iso = now_utc.isoformat()

    new_count = 0
    for a in current_assessments:
        try:
            indicator_id = a.id
            indicator_name = a.name
            tier = a.tier
            new_status = a.current_status
        except AttributeError:
            # Skip malformed entries — defensive, shouldn't happen with
            # IndicatorAssessment which guarantees these via dataclass
            continue

        prior_status = prior_status_by_id.get(indicator_id)
        if prior_status is None:
            continue  # No prior baseline — not a transition
        if prior_status == new_status:
            continue  # Unchanged
        key = (indicator_id, new_status)
        if key in existing_keys:
            continue  # Already in log from earlier rerun today

        log.append({
            "indicator_id": indicator_id,
            "indicator_name": indicator_name,
            "tier": tier,
            "old_status": prior_status,
            "new_status": new_status,
            "description": f"Status changed from '{prior_status}' to '{new_status}'.",
            "fired_at_iso": now_iso,
        })
        existing_keys.add(key)
        new_count += 1

    if new_count > 0:
        logger.info(
            f"Trigger event log: {new_count} new transitions added; "
            f"{len(log)} total entries in 24h window"
        )

    save_trigger_log_24h(log)
    return log


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


# ============================================================================
# Indicator evidence store (v3.39.7.3)
# ============================================================================
#
# Persistent record of WHY each indicator is in its current state. Solves the
# v3.39.6/v3.39.7 regression where Layer 2 (Haiku) had no carry-forward of
# prior firing evidence — silence in today's news caused indicators with
# cumulative evidence (capex, LTAs, P/E, insider sales) to inappropriately
# downgrade to "Not fired".
#
# Schema (state/indicator_evidence.json):
#   {
#     "_metadata": {...},
#     "indicators": {
#       "<indicator_id>": {
#         "current_status": "Fired" | "Partially fired" | "Not fired" | "Reversed",
#         "fired_since": "YYYY-MM-DD" | null,
#         "last_updated": "YYYY-MM-DD",
#         "evidence_log": [<EvidenceEntry>, ...],
#         "next_check_due": "YYYY-MM-DD" | null,
#         "notes": "optional",
#         "display_name": "optional override"
#       }
#     }
#   }
#
# EvidenceEntry:
#   {
#     "date": "YYYY-MM-DD",
#     "type": "announcement" | "forecast" | "computed" | "data_point" | "transaction",
#     "value": "human-readable description of the data",
#     "source": "where the data came from",
#     "persistence": "permanent" | "quarterly" | "durable_contract" | "live" | "event_window",
#     "reversal_condition": "what would constitute reversal of this evidence"
#   }
#
# Persistence semantics:
#   permanent        — Historical event, can't be reversed (e.g., TurboQuant
#                      announcement). Status persists indefinitely.
#   quarterly        — Refreshes at next earnings/forecast season. Status
#                      persists until next_check_due is past.
#   durable_contract — Binding agreement until expiry (e.g., multi-year LTAs).
#                      Status persists until explicit cancellation news.
#   live             — Recomputed every run from current data (e.g., forward
#                      P/E, relative strength, cycle matrix verdict). The
#                      latest run's verdict always wins.
#   event_window     — Decays after fixed window (e.g., 5d relative strength).
#                      Auto-reset after window expires.

INDICATOR_EVIDENCE_PATH = STATE_DIR / "indicator_evidence.json"

PERSISTENCE_CATEGORIES = {
    "permanent", "quarterly", "durable_contract", "live", "event_window",
}

EVIDENCE_TYPES = {
    "announcement", "forecast", "computed", "data_point", "transaction",
}

VALID_STATUSES = {"Fired", "Partially fired", "Not fired", "Reversed"}


@dataclass
class EvidenceEntry:
    """Single piece of evidence supporting an indicator's fired state."""
    date: str  # ISO date when this evidence was recorded
    type: str  # announcement | forecast | computed | data_point | transaction
    value: str  # human-readable description
    source: str  # provenance string
    persistence: str  # permanent | quarterly | durable_contract | live | event_window
    reversal_condition: str  # specific data that would unfire this evidence

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceEntry":
        return cls(
            date=d.get("date", ""),
            type=d.get("type", "data_point"),
            value=d.get("value", ""),
            source=d.get("source", ""),
            persistence=d.get("persistence", "quarterly"),
            reversal_condition=d.get("reversal_condition", ""),
        )

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "type": self.type,
            "value": self.value,
            "source": self.source,
            "persistence": self.persistence,
            "reversal_condition": self.reversal_condition,
        }


@dataclass
class IndicatorEvidence:
    """Per-indicator evidence state.

    Tracks the indicator's current_status, when it first fired, and the
    accumulated evidence_log of what caused it to fire. Used by Layer 2
    to determine whether today's evidence should preserve, modify, or
    reverse the current status.
    """
    indicator_id: str
    current_status: str
    fired_since: Optional[str]
    last_updated: str
    evidence_log: list[EvidenceEntry] = field(default_factory=list)
    next_check_due: Optional[str] = None
    notes: str = ""
    display_name: str = ""

    @classmethod
    def from_dict(cls, indicator_id: str, d: dict) -> "IndicatorEvidence":
        return cls(
            indicator_id=indicator_id,
            current_status=d.get("current_status", "Not fired"),
            fired_since=d.get("fired_since"),
            last_updated=d.get("last_updated", ""),
            evidence_log=[EvidenceEntry.from_dict(e) for e in d.get("evidence_log", [])],
            next_check_due=d.get("next_check_due"),
            notes=d.get("notes", ""),
            display_name=d.get("display_name", ""),
        )

    def to_dict(self) -> dict:
        out = {
            "current_status": self.current_status,
            "fired_since": self.fired_since,
            "last_updated": self.last_updated,
            "evidence_log": [e.to_dict() for e in self.evidence_log],
            "next_check_due": self.next_check_due,
        }
        if self.notes:
            out["notes"] = self.notes
        if self.display_name:
            out["display_name"] = self.display_name
        return out

    def active_evidence(self, today: Optional[date] = None) -> list[EvidenceEntry]:
        """Return evidence entries that are still in effect today.

        Filters out:
          - permanent entries with explicit aging (e.g., insider sales >12mo)
          - quarterly entries past their refresh window without renewal
          - event_window entries where the window has expired

        Live entries always pass through (their value is re-evaluated each run).
        """
        today = today or date.today()
        active: list[EvidenceEntry] = []
        for e in self.evidence_log:
            if e.persistence == "live":
                active.append(e)
                continue
            if e.persistence == "permanent":
                active.append(e)
                continue
            if e.persistence in ("quarterly", "durable_contract"):
                # Always pass through unless explicit reversal is recorded.
                # Aging happens via reversal_condition match, not date math.
                active.append(e)
                continue
            if e.persistence == "event_window":
                # Event windows decay; rely on explicit window expiry recorded
                # in reversal_condition. For now, treat as inactive if the
                # entry is >35 days old and no live re-confirmation exists.
                try:
                    entry_date = date.fromisoformat(e.date)
                    age_days = (today - entry_date).days
                    if age_days <= 35:
                        active.append(e)
                except ValueError:
                    active.append(e)  # malformed date — keep visible
        return active

    def format_for_layer2_prompt(self) -> str:
        """Render evidence_log as compact markdown for the Layer 2 prompt."""
        if not self.evidence_log:
            return "(no prior evidence on file)"
        lines = [f"Current status: {self.current_status}"]
        if self.fired_since:
            lines.append(f"Fired since: {self.fired_since}")
        if self.next_check_due:
            lines.append(f"Next scheduled re-check: {self.next_check_due}")
        lines.append("\nEvidence log (oldest first):")
        for i, e in enumerate(self.evidence_log, 1):
            lines.append(
                f"  [{i}] {e.date} ({e.type}, persistence={e.persistence}): {e.value}"
            )
            lines.append(f"      Source: {e.source}")
            lines.append(f"      Reversal condition: {e.reversal_condition}")
        return "\n".join(lines)


def load_indicator_evidence_store(
    path: Optional[Path] = None,
) -> dict[str, IndicatorEvidence]:
    """Load the persistent evidence store keyed by indicator_id.

    Returns an empty dict (not error) if the file doesn't exist — the
    caller can decide whether to bootstrap from scratch. Returns the
    INDICATORS portion of the JSON; metadata is silently dropped.
    """
    path = path or INDICATOR_EVIDENCE_PATH
    if not path.exists():
        logger.warning(
            f"Indicator evidence store not found at {path}. "
            f"Layer 2 will run without prior-evidence carry-forward this run."
        )
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception as e:
        logger.error(f"Could not load {path}: {e}. Returning empty store.")
        return {}
    indicators = raw.get("indicators", {})
    if not isinstance(indicators, dict):
        logger.error(f"{path}: 'indicators' is not a dict. Returning empty store.")
        return {}
    out: dict[str, IndicatorEvidence] = {}
    for ind_id, ind_data in indicators.items():
        try:
            out[ind_id] = IndicatorEvidence.from_dict(ind_id, ind_data)
        except Exception as e:
            logger.warning(f"Could not parse evidence for {ind_id}: {e}")
    logger.info(f"Loaded indicator evidence store: {len(out)} indicators from {path}")
    return out


def save_indicator_evidence_store(
    store: dict[str, IndicatorEvidence],
    path: Optional[Path] = None,
    bootstrap_source: str = "runtime update",
) -> None:
    """Persist the evidence store back to disk.

    Atomically writes to a temp file then renames, so a crash mid-write
    cannot leave a corrupted JSON. Preserves the _metadata section if
    present in the existing file.
    """
    path = path or INDICATOR_EVIDENCE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve metadata from existing file if present
    metadata: dict = {
        "schema_version": "1.0",
        "bootstrap_source": bootstrap_source,
        "last_full_update": date.today().isoformat(),
    }
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
            existing_meta = existing.get("_metadata", {})
            if existing_meta:
                # Keep schema_version + bootstrap_source from original; refresh date
                metadata = {**existing_meta, "last_full_update": date.today().isoformat()}
        except Exception:
            pass

    out = {
        "_metadata": metadata,
        "indicators": {ind_id: ev.to_dict() for ind_id, ev in store.items()},
    }
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(out, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp_path.replace(path)
    logger.info(f"Indicator evidence store saved: {len(store)} indicators -> {path}")


def update_evidence_after_layer2(
    store: dict[str, IndicatorEvidence],
    layer2_results: list,  # list[SignalReasoning], avoid import cycle
    today: Optional[date] = None,
) -> dict[str, IndicatorEvidence]:
    """Update the evidence store based on Layer 2's verdicts.

    For each Layer 2 result:
      - If status unchanged AND no new evidence: just update last_updated
      - If status changed (Fired→Not fired or vice versa): record the
        transition with the date and Layer 2's reasoning
      - If new evidence detected (different from existing log): append it

    Returns the mutated store (also mutates in place). Does NOT write to
    disk — caller is responsible for save_indicator_evidence_store().
    """
    today = today or date.today()
    today_iso = today.isoformat()

    for result in layer2_results:
        ind_id = getattr(result, "signal_id", None)
        if not ind_id:
            continue
        new_status = getattr(result, "fired_status", "Not fired")
        if new_status not in VALID_STATUSES:
            logger.warning(
                f"Layer 2 returned invalid status '{new_status}' for {ind_id}; skipping store update"
            )
            continue

        if ind_id not in store:
            # Create new entry — first time seeing this indicator
            store[ind_id] = IndicatorEvidence(
                indicator_id=ind_id,
                current_status=new_status,
                fired_since=today_iso if new_status in ("Fired", "Partially fired") else None,
                last_updated=today_iso,
                evidence_log=[],
            )
            continue

        current = store[ind_id]
        prior_status = current.current_status
        current.last_updated = today_iso

        if prior_status != new_status:
            # Status transition — record what Layer 2 saw
            transition_note = (
                f"Layer 2 transition {prior_status} -> {new_status}. "
                f"Reasoning: {getattr(result, 'consensus_direction', '')[:200]}"
            )
            if new_status in ("Fired", "Partially fired") and not current.fired_since:
                current.fired_since = today_iso
            if new_status == "Not fired":
                # Don't clear fired_since immediately — keep the audit trail
                # but mark the transition in the log
                pass
            current.current_status = new_status
            current.evidence_log.append(EvidenceEntry(
                date=today_iso,
                type="data_point",
                value=transition_note,
                source=f"Layer 2 reasoner ({len(getattr(result, 'evidence_sources', []) or [])} sources)",
                persistence="live",  # transitions get re-checked next run
                reversal_condition=(
                    "Subsequent Layer 2 run reverses this transition with new evidence"
                ),
            ))
    return store


# ============================================================
# v3.4.0 — DHCR (Decode HBM Captive Ratio) state persistence
# ============================================================
# DHCR is a direction monitor. Each run we compute a snapshot from
# the YAML reference files and persist it to state/dhcr_state.json
# so the next run can compare current vs prior (QoQ direction
# transitions). The full historical trajectory is recomputed from
# YAML each run — it doesn't need persistence.

DHCR_STATE_FILE = Path("state/dhcr_state.json")


def load_dhcr_prior_state() -> Optional[dict]:
    """Load the most recent persisted DHCR snapshot for QoQ comparison.

    Returns None if no prior state exists (first run, or fresh deploy).
    The structure mirrors the DHCRSnapshot dataclass from dhcr.py.
    """
    if not DHCR_STATE_FILE.exists():
        return None
    try:
        with open(DHCR_STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load DHCR prior state: {e}")
        return None


def save_dhcr_state(snapshot_dict: dict) -> None:
    """Persist the current DHCR snapshot for next-run comparison.

    Accepts a dict (typically asdict(DHCRSnapshot)) so this module
    doesn't depend on importing dhcr.py (avoids circular import risk).
    Adds a history entry to dhcr_state.json's `recent_snapshots` list
    (capped at 12 entries for QoQ trend windowing).
    """
    DHCR_STATE_FILE.parent.mkdir(exist_ok=True)
    payload = {
        "current": snapshot_dict,
        "saved_at_iso": datetime.utcnow().isoformat(),
    }

    # Append to recent_snapshots history (cap at 12 entries)
    prior = load_dhcr_prior_state()
    if prior and isinstance(prior, dict):
        history = prior.get("recent_snapshots", [])
        if isinstance(history, list):
            history.append(snapshot_dict)
            payload["recent_snapshots"] = history[-12:]
        else:
            payload["recent_snapshots"] = [snapshot_dict]
    else:
        payload["recent_snapshots"] = [snapshot_dict]

    with open(DHCR_STATE_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    logger.info(f"DHCR snapshot persisted to {DHCR_STATE_FILE}")
