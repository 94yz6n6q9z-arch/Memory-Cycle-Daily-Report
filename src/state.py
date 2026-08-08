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
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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


# v3.41.18 — matches the auto-generated transition phrase so the collapse
# and the template can distinguish "no evidence attached" from an enriched
# description.
_GENERIC_TRANSITION_DESC_RE = re.compile(
    r"^Status changed from '.*' to '.*'\.$"
)


def collapse_trigger_log_for_display(entries: list[dict]) -> list[dict]:
    """Collapse the 24h log to one NET transition per indicator for display.

    v3.39.10. The stored 24h log keeps every individual transition (deduped
    by (indicator_id, new_status)) so cross-rerun stability works. But for
    the "NEW TRIGGER EVENT" banners we want each indicator shown at most
    once, as its NET move over the window — otherwise a round-trip (e.g.
    t3 went Not fired→Partially fired yesterday, then Partially fired→Not
    fired today) renders as TWO contradictory banners, as seen in the
    2026-06-06 brief.

    Per indicator: take the earliest old_status and the latest new_status
    within the window (ordered by fired_at_iso). If they are equal, the
    indicator round-tripped back to where it started → drop it (no net
    change to announce). Otherwise emit a single entry describing the net
    move. Output is sorted by the latest leg's fired_at_iso for stable,
    deterministic ordering.
    """
    by_indicator: dict[str, list[dict]] = {}
    for e in entries:
        if not isinstance(e, dict) or "indicator_id" not in e:
            continue
        by_indicator.setdefault(e["indicator_id"], []).append(e)

    collapsed: list[dict] = []
    for _iid, evs in by_indicator.items():
        evs_sorted = sorted(evs, key=lambda e: e.get("fired_at_iso", ""))
        earliest_old = evs_sorted[0].get("old_status")
        latest = evs_sorted[-1]
        latest_new = latest.get("new_status")
        if earliest_old == latest_new:
            continue  # round-trip nets to no change — nothing to announce
        # v3.41.18 — carry the freshest ENRICHED description through the
        # collapse instead of rebuilding the generic phrase (which discarded
        # the evidence v3.41.18's update path now attaches). Walk legs
        # newest→oldest; first non-generic description wins; none → the
        # generic phrase for the NET move, exactly as before.
        desc = f"Status changed from '{earliest_old}' to '{latest_new}'."
        for leg in reversed(evs_sorted):
            d = (leg.get("description") or "").strip()
            if d and not _GENERIC_TRANSITION_DESC_RE.match(d):
                desc = d
                break
        collapsed.append({
            "indicator_id": latest.get("indicator_id"),
            "indicator_name": latest.get("indicator_name"),
            "tier": latest.get("tier"),
            "old_status": earliest_old,
            "new_status": latest_new,
            "description": desc,
            "fired_at_iso": latest.get("fired_at_iso", ""),
        })

    collapsed.sort(key=lambda e: e.get("fired_at_iso", ""))
    return collapsed


# v3.39.10 — matches a leading sentence asserting that no signals fired or
# unfired (the contradictory opener Sonnet has produced; see Issue 1,
# 2026-06-06). Kept at module scope so tests can exercise it directly.
#
# v3.39.11 — regex hardened after the 2026-06-12 brief shipped with the
# phrasing "No tier-1-5 signals fired or unfired today — all 23 indicators
# hold their prior status; ..." which evaded the v3.39.10 regex on two
# counts: (a) "tier-1-5" with a hyphen didn't match the rigid `tier\s+`
# alternation; (b) the sentence ended with an em-dash (—) rather than `[.;]`.
# The new pattern allows any non-terminator characters between "no" / "signals"
# / "fired|unfired", and accepts em-dash and en-dash as sentence terminators.
# The {0,N} bounds prevent runaway matches across paragraph boundaries.
_FALSE_NO_SIGNAL_OPENER = re.compile(
    r"^\s*no\b[^.;—–\n]{0,80}\bsignals?\b[^.;—–\n]{0,80}"
    r"\b(?:fired|unfired)\b[^.;—–\n]{0,120}[.;—–]\s*",
    re.IGNORECASE,
)


def reconcile_executive_brief_opener(
    brief: str, signal_changed_names: list[str]
) -> str:
    """Strip a false "no signals fired/unfired" opener when signals changed.

    v3.39.10. `signal_changed_names` must be the display names of tier-1-5
    SIGNALS that changed status today (watch overlays — Indicators 6/7/8 —
    excluded by the caller, since an overlay change does not make a
    "no signals fired" claim false). If that list is non-empty and `brief`
    opens with a blanket "no signals fired or unfired" claim, replace that
    opening sentence with a deterministic, accurate lead. Otherwise return
    `brief` unchanged. Pure function — no I/O, safe to unit-test.
    """
    if not (signal_changed_names and brief):
        return brief
    if not _FALSE_NO_SIGNAL_OPENER.search(brief):
        return brief
    n = len(signal_changed_names)
    names = ", ".join(signal_changed_names)
    replacement = (
        f"{n} signal status change{'s' if n != 1 else ''} today ({names}). "
    )
    return _FALSE_NO_SIGNAL_OPENER.sub(replacement, brief, count=1)


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
    evidence_by_id: Optional[dict[str, str]] = None,
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
        evidence_by_id: v3.41.18 — optional indicator_id → short evidence
            string (pre-SANITIZED and pre-capped by the caller). When a
            transition is logged and this map carries a non-empty string for
            the indicator, it becomes the entry's description — the "why"
            the banners lost when v3.39.11 made this log authoritative
            (Sonnet's own descriptions were replaced by the generic
            "Status changed from X to Y" phrase, which the template then
            rightly suppresses as adding nothing). Absent/empty → the
            generic phrase, exactly as before.

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

        evidence = (evidence_by_id or {}).get(indicator_id) or ""
        log.append({
            "indicator_id": indicator_id,
            "indicator_name": indicator_name,
            "tier": tier,
            "old_status": prior_status,
            "new_status": new_status,
            "description": (
                evidence.strip()
                or f"Status changed from '{prior_status}' to '{new_status}'."
            ),
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

# v3.4.1 Fix 3 — "Reversed" REMOVED from valid statuses. The v3.39.7.9
# release removed "Reversed" from the Sonnet prompt schema and the
# signal_reasoner output enum, but THIS validation gate was missed — so
# Layer 2 output containing "Reversed" was still being accepted at the
# state-update boundary and propagated downstream into trigger event
# banners and the evidence store. v3.4.1 closes the upstream gate: any
# Layer 2 verdict carrying "Reversed" now triggers the silent-skip path
# (which logs a warning and leaves the prior status untouched). Combined
# with render-time defensive conversion in template.py, this ensures
# "Reversed" cannot appear anywhere user-visible.
VALID_STATUSES = {"Fired", "Partially fired", "Not fired"}

# Legacy alias map for any historical state still carrying "Reversed".
# Used by load_indicator_evidence_store to migrate entries on read.
LEGACY_STATUS_MIGRATIONS = {"Reversed": "Not fired"}


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
    migrated_count = 0
    for ind_id, ind_data in indicators.items():
        try:
            entry = IndicatorEvidence.from_dict(ind_id, ind_data)
            # v3.4.1 Fix 3 — legacy migration. Pre-v3.39.7.9 store entries
            # may have current_status == "Reversed". Migrate on read so
            # downstream code never sees the forbidden vocabulary.
            if entry.current_status in LEGACY_STATUS_MIGRATIONS:
                old = entry.current_status
                entry.current_status = LEGACY_STATUS_MIGRATIONS[old]
                migrated_count += 1
                logger.info(
                    f"Legacy status migrated for {ind_id}: '{old}' → '{entry.current_status}'"
                )
            out[ind_id] = entry
        except Exception as e:
            logger.warning(f"Could not parse evidence for {ind_id}: {e}")
    if migrated_count:
        logger.warning(
            f"v3.4.1 legacy migration: {migrated_count} indicator(s) had "
            f"'Reversed' status — migrated to 'Not fired' in memory. Next "
            f"save_indicator_evidence_store call will persist the migration."
        )
    logger.info(f"Loaded indicator evidence store: {len(out)} indicators from {path}")
    return out


T4_BU_REPAIR_TAG = "REPAIR v3.41.15 (t4_layoffs BU scope)"       # v1 marker (kept for detection)
T4_BU_REPAIR_V2_TAG = "REPAIR v3.41.16 (t4_layoffs BU scope v2)"
T4_INVALIDATED_PREFIX = "INVALIDATED (non-memory BU — not firing evidence): "

_T4_LAYOFF_TERMS = ("layoff", "job cut", "workforce", "headcount")
_T4_NONMEMORY_BU = ("smartphone", "mobile", "handset", "mx division",
                    "consumer electronics", "appliance", "display division",
                    "texas headquarters", "texas hq")
_T4_MEMORY_CTX = ("memory", "dram", "nand", "hbm", "fab idl", "fab util",
                  "semiconductor division", "ds division", "ds-memory")
_T4_MARKER_SNIPPETS = ("repair v3.41.15", "repair v3.41.16", "invalidated (non-memory bu")


def _t4_scrub(text: str) -> str:
    """Lowercase + remove negated memory references so they can't fake
    memory context. v3.41.18: 'not memory' / 'no memory' added — the Jul-24
    Layer-2 transition note ('…smartphone division, not memory operations…')
    registered as memory context through the v3.41.16/17 scrub, which only
    knew the 'non-memory' spelling, and froze the repair on its own
    corrective annotation."""
    low = (text or "").lower()
    for neg in ("non-memory", "non memory", "not memory", "no memory"):
        low = low.replace(neg, "")
    return low


# v3.41.18 — absence statements. The May-9 bootstrap entry ("No layoffs or
# fab idling announced at any of the three majors") is a statement that
# NOTHING happened, yet its 'fab idl' substring passed the memory-context
# check and froze the repair from the day v1 shipped (v1/v2/v3 never ran
# once in production; the Jul-24 t4 revert came from Layer 2 alone). An
# absence entry asserts no event — it can be neither firing evidence nor
# proof of a genuine memory-side fire, so it is excluded from BOTH the
# poison and the memory-context predicates.
_T4_ABSENCE_RE = re.compile(
    r"\bno\s+(?:new\s+)?(?:layoffs?|fab\s+idling|evidence)\b", re.IGNORECASE
)


def _t4_is_absence(text: str) -> bool:
    return bool(_T4_ABSENCE_RE.search(text or ""))


def _t4_is_marker(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _T4_MARKER_SNIPPETS)


def _t4_is_nonmemory_samsung_layoff(text: str) -> bool:
    low = _t4_scrub(text)
    return (
        "samsung" in low
        and any(t in low for t in _T4_LAYOFF_TERMS)
        and any(t in low for t in _T4_NONMEMORY_BU)
        and not any(t in low for t in _T4_MEMORY_CTX)
    )


def repair_t4_layoffs_bu_misfire(store: dict) -> Optional[dict]:
    """One-shot v2 correction of the t4_layoffs BU misfire (v3.41.16).

    v3.41.15's v1 repair reverted the STATUS but left the Jul-20 evidence
    entry's persuasive pro-fire text in the store — Layer 2 read it as its
    prior context and RE-FIRED the signal in the same run, undoing the
    repair within minutes and leaving three sources of truth disagreeing
    (store annotation said reverted, assessments said Fired, exec prose
    narrated the annotation). v2 neutralizes the EVIDENCE, not just the
    status: every entry matching the non-memory-BU predicate is rewritten
    with an INVALIDATED prefix (the reasoner prompt's INVALID FIRES rule
    instructs Layer 2 that such entries are not evidence), and the status
    reverts if currently Fired.

    Scoping rule encoded here and in the spec: 'Samsung' + 'layoffs' is
    NEVER sufficient — firing requires explicit memory-operations
    attribution. Conservative guards: entries written by the repair itself
    (v1 annotation, v2 annotation, INVALIDATED rewrites) are excluded from
    all predicates ('non-memory' is scrubbed so the v1 annotation's own
    wording cannot register as memory context); if any NON-marker entry
    carries genuine memory context, the repair does nothing and logs for
    manual review; runs exactly once (v2 tag).

    Mutates the store in place; returns an info dict or None. The caller
    persists; a Fired→Not fired revert then surfaces through the normal
    24h trigger log (prior_state still says Fired) — visible, not silent.
    """
    try:
        ev = (store or {}).get("t4_layoffs")
        if ev is None:
            return None
        entries = list(getattr(ev, "evidence_log", None) or [])
        # v3.41.18 — absence statements excluded alongside repair markers:
        # they assert nothing happened and can support neither predicate.
        substantive = [
            e for e in entries
            if not _t4_is_marker(f"{getattr(e, 'value', '')} {getattr(e, 'source', '')}")
            and not _t4_is_absence(f"{getattr(e, 'value', '')} {getattr(e, 'source', '')}")
        ]
        # v3.41.17 — idempotent, runs EVERY day: the v2 run-once tag meant the
        # Jul-22/23 re-fire entries (written after v2 ran) were never cleaned.
        # Already-prefixed entries are markers and drop out of `substantive`,
        # so re-invalidation is naturally impossible; only NEW poison acts.
        poison_new = [
            e for e in substantive
            if _t4_is_nonmemory_samsung_layoff(
                f"{getattr(e, 'value', '')} {getattr(e, 'source', '')}")
        ]
        memory_ctx_present = any(
            any(t in _t4_scrub(f"{getattr(e, 'value', '')} {getattr(e, 'source', '')}")
                for t in _T4_MEMORY_CTX)
            for e in substantive
        )
        if memory_ctx_present:
            if poison_new:
                logger.warning(
                    "[t4-repair] non-memory-BU entries found ALONGSIDE genuine "
                    "memory-context evidence — not auto-repairing; review "
                    "state/indicator_evidence.json manually."
                )
            return None
        has_prior_invalidated = any(
            (getattr(e, "value", "") or "").startswith(T4_INVALIDATED_PREFIX)
            for e in entries
        )
        # Revert a standing Fired when it rests on poisoned or already-
        # invalidated evidence (and nothing memory-side supports it).
        should_revert = (
            getattr(ev, "current_status", "") == "Fired"
            and (poison_new or has_prior_invalidated)
        )
        if not poison_new and not should_revert:
            return None
        for e in poison_new:
            e.value = T4_INVALIDATED_PREFIX + (getattr(e, "value", "") or "")
        status_reverted = False
        if should_revert:
            ev.current_status = "Not fired"
            status_reverted = True
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ev.evidence_log.append(EvidenceEntry(
            date=today_iso,
            type="data_point",
            value=(
                f"{T4_BU_REPAIR_V2_TAG}: {len(poison_new)} evidence entr"
                f"{'y' if len(poison_new) == 1 else 'ies'} invalidated — Samsung "
                f"non-memory-BU (smartphone/consumer) job cuts are not firing "
                f"evidence for this signal. 'Samsung' + 'layoffs' alone is "
                f"never sufficient; firing requires explicit memory-operations "
                f"attribution (DRAM/NAND/HBM lines, memory fabs, DS-Memory "
                f"employees). "
                + ("Status reverted to Not fired."
                   if status_reverted else "Status was already not Fired.")
            ),
            source="v3.41.16+ BU-scope repair (idempotent since v3.41.17)",
            persistence="permanent",
            reversal_condition=(
                "Memory-operations layoffs or memory-fab idling are "
                "explicitly reported"
            ),
        ))
        return {
            "indicator_id": "t4_layoffs",
            "invalidated": len(poison_new),
            "status_reverted": status_reverted,
            "old_status": "Fired" if status_reverted else getattr(ev, "current_status", "?"),
            "new_status": "Not fired",
            "description": (
                f"Correction: {len(poison_new)} Samsung non-memory-BU "
                f"layoff entr{'y' if len(poison_new) == 1 else 'ies'} invalidated; "
                f"'Samsung'+'layoffs' is never sufficient — explicit "
                f"memory-operations attribution required."
                + (" Status reverted to Not fired." if status_reverted else "")
            ),
        }
    except Exception as e:
        logger.warning(f"[t4-repair] repair check failed (non-fatal): {e}")
        return None


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
# v3.45.0 — event-echo dedup: re-coverage of a stored event
            # corroborates; near-identical text must not append as new.
            _ntok = set(re.findall(r'[a-z0-9]+', str(transition_note).lower()))
            _dup = False
            for _ex in (current.evidence_log or [])[-20:]:
                _etok = set(re.findall(r'[a-z0-9]+', str(getattr(_ex, 'value', '') or '').lower()))
                if _ntok and _etok and len(_ntok & _etok) / max(len(_ntok | _etok), 1) >= 0.6:
                    _dup = True
                    logger.info("[evidence-dedup] corroboration of stored event — not appended as new")
                    break
            if not _dup:
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


# ============================================================
# v3.4.11 — Phase detector state (Phase 4 → Phase 5 transition)
# ============================================================
# The phase detector evaluates indicator status + SK Hynix price every
# fast-tier run and persists its verdict so anti-flicker hysteresis
# survives across runs. State file schema:
#   {
#     "current_phase": 4 | 5,
#     "current_phase_label": str,
#     "phase_5_detected_date_iso": str | null,
#     "cycle_peak_price_at_detection": float | null,
#     "anti_flicker_pending_condition": str | null,
#     "anti_flicker_pending_first_seen_iso": str | null,
#     "condition_a_first_seen_iso": str | null,
#     "last_evaluated_iso": str,
#     "last_summary_line": str
#   }
# First-run default: empty dict; the detector treats prior_phase=4 as
# implicit when current_phase is missing.

PHASE_STATE_FILE = Path("state/phase_state.json")


def load_phase_state() -> dict:
    """Load the v3.4.11 phase detector state.

    Returns {} on missing file or parse error (first-run / fresh-deploy
    semantics). The detector treats missing fields defensively.
    """
    if not PHASE_STATE_FILE.exists():
        return {}
    try:
        with open(PHASE_STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(
                f"{PHASE_STATE_FILE} root is not a dict (got {type(data).__name__}); "
                f"treating as empty"
            )
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load phase state: {e}")
        return {}


def save_phase_state(state: dict) -> None:
    """Persist the v3.4.11 phase detector state.

    Writes atomically via tmp + rename to survive concurrent fast-tier
    crons safely. The state file is committed by the workflow's git
    step so phase memory persists across GHA runner shutdowns.
    """
    PHASE_STATE_FILE.parent.mkdir(exist_ok=True)
    tmp_path = PHASE_STATE_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2, default=str)
        tmp_path.replace(PHASE_STATE_FILE)
        logger.info(f"Phase state persisted to {PHASE_STATE_FILE} (phase={state.get('current_phase')})")
    except OSError as e:
        logger.warning(f"Could not write {PHASE_STATE_FILE}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================================
# v3.4.12 — Per-quarter transcript digest store + QoQ diff cache
# ============================================================================
#
# Each company-quarter gets one file at PER_QUARTER_TRANSCRIPTS_DIR/{ticker_safe}/{YYYY-QN}.json
# holding the canonical digest + (after Q+1 lands) the cached QoQ diff.
# Sole writer: the digester via _persist_and_maybe_diff_quarter() in
# src/analysis/digester.py. The fast tier reads active diffs and threads
# them into the Sonnet prompt.
#
# Quarter key derivation uses calendar quarters from the event_date_iso.
# A handful of companies (Oracle, Micron, NVIDIA) report on fiscal calendars
# that don't align — we accept this imprecision since the diff is between
# the SAME company's consecutive reports, so as long as the keying is
# consistent per company, the lookup works.

PER_QUARTER_TRANSCRIPTS_DIR = STATE_DIR / "transcripts"


def _ticker_safe(ticker: str) -> str:
    """Make a ticker safe to use as a directory name.

    Matches the pattern korean_brokers + digester use for filename
    sanitization. E.g. "000660.KS" -> "000660.KS", "BRK.B" -> "BRK.B",
    "weird ticker / 5" -> "weird_ticker___5".
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker or "unknown")


def quarter_key_from_event_date(event_date_iso: str) -> Optional[str]:
    """Derive "YYYY-QN" from an ISO date string.

    Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec.

    Returns None if the date can't be parsed — caller decides what to do.
    Fiscal-calendar companies (Oracle, Micron, NVIDIA) get keyed by the
    calendar quarter their event lands in; as long as keying is consistent
    per company, the QoQ lookup chain still works because diff lookups
    walk back ONE quarter at a time.
    """
    if not event_date_iso:
        return None
    try:
        d = datetime.fromisoformat(event_date_iso[:10]).date()
    except (ValueError, TypeError):
        return None
    quarter = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{quarter}"


def get_prior_quarter_key(quarter_key: str) -> Optional[str]:
    """Compute the immediately preceding quarter key. Handles year boundary.

    "2026-Q2" -> "2026-Q1"
    "2026-Q1" -> "2025-Q4"
    """
    if not quarter_key or not re.match(r"^\d{4}-Q[1-4]$", quarter_key):
        return None
    year = int(quarter_key[:4])
    q = int(quarter_key[6])
    if q == 1:
        return f"{year - 1}-Q4"
    return f"{year}-Q{q - 1}"


def quarter_digest_path(ticker: str, quarter_key: str) -> Path:
    """Resolve the per-quarter file path for a ticker+quarter."""
    return PER_QUARTER_TRANSCRIPTS_DIR / _ticker_safe(ticker) / f"{quarter_key}.json"


def load_quarter_digest(ticker: str, quarter_key: str) -> Optional[dict]:
    """Load the per-quarter digest record. Returns None on missing/corrupt.

    Record shape:
      {
        "ticker": "000660.KS",
        "company": "SK Hynix",
        "quarter_key": "2026-Q2",
        "digest": {...},                  # the underlying Digest dict
        "diff_with_prior": {...} | null,  # cached TranscriptDiff dict (after Q+1 lands)
        "saved_at_iso": "...",
      }
    """
    path = quarter_digest_path(ticker, quarter_key)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(f"{path} root is not a dict; treating as missing")
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load quarter digest {path}: {e}")
        return None


def save_quarter_digest(
    ticker: str,
    company: str,
    quarter_key: str,
    digest: dict,
    diff_with_prior: Optional[dict] = None,
) -> Path:
    """Persist a per-quarter digest record. Atomic via tmp + rename.

    If diff_with_prior is provided it's stored inline; otherwise the slot
    is null and gets filled in on a later run when the Q+1 file triggers
    the Haiku diff call.
    """
    path = quarter_digest_path(ticker, quarter_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ticker": ticker,
        "company": company,
        "quarter_key": quarter_key,
        "digest": digest,
        "diff_with_prior": diff_with_prior,
        "saved_at_iso": datetime.utcnow().isoformat(),
    }
    tmp_path = path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(record, f, indent=2, default=str)
        tmp_path.replace(path)
        logger.info(
            f"Per-quarter digest saved: {ticker} {quarter_key} "
            f"(diff_cached={diff_with_prior is not None})"
        )
    except OSError as e:
        logger.warning(f"Could not write {path}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
    return path


def list_recent_quarter_diffs(max_quarters_back: int = 1) -> list[dict]:
    """Return active QoQ transcript diffs for fast-tier consumption.

    Walks PER_QUARTER_TRANSCRIPTS_DIR, returns the latest N quarter records
    per company that have a cached diff_with_prior. Skips records without
    a diff (most recent quarter has no diff until Q+1 lands).

    The fast tier passes these into analyze_with_llm so Sonnet sees the
    QoQ language shifts when narrating where_we_stand for transcript-driven
    indicators.
    """
    if not PER_QUARTER_TRANSCRIPTS_DIR.exists():
        return []
    out: list[dict] = []
    for ticker_dir in sorted(PER_QUARTER_TRANSCRIPTS_DIR.iterdir()):
        if not ticker_dir.is_dir():
            continue
        # Collect this ticker's quarter files sorted by quarter_key descending
        files = sorted(ticker_dir.glob("*.json"), reverse=True)
        kept = 0
        for f in files:
            if kept >= max_quarters_back:
                break
            try:
                with open(f) as fh:
                    record = json.load(fh)
                if not isinstance(record, dict):
                    continue
                if record.get("diff_with_prior"):
                    out.append(record)
                    kept += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not load quarter digest {f}: {e}")
    return out


# ============================================================
# v3.5.0 — Event deduplication log
# ============================================================
# state/event_log.json tracks every distinct real-world event so that
# coverage echoes (8-10 ingested items from different sources all
# reporting the SAME earnings call) are counted ONCE — not 8-10 times —
# by the t2_eps_revisions rolling-sum engine.
#
# Schema (one entry per event):
#   "<ticker>:earnings_call:<quarter_key>": {
#     "first_observed": ISO string,
#     "first_observation_source": str,
#     "primary_verdict": str,           # "bearish" | "bullish" | "neutral"
#     "primary_score": float,           # the score that was actually counted
#     "event_date_iso": str,            # YYYY-MM-DD
#     "observations": list[dict],       # all subsequent echoes
#     "divergence_flagged": bool,       # True if later obs contradicts primary
#     "n_observations": int
#   }
#
# Auto-pruned to last 180 days on each write.

EVENT_LOG_FILE = STATE_DIR / "event_log.json"
EVENT_LOG_PRUNE_DAYS = 180


def event_key_for_earnings(ticker: str, event_date_iso: str) -> str:
    """Canonical event key for an earnings-call event.

    All broker notes / transcripts / news items about the same earnings
    quarter for the same ticker resolve to the same key.

    >>> event_key_for_earnings("000660.KS", "2026-07-29")
    '000660.KS:earnings_call:2026-Q3'
    """
    qk = quarter_key_from_event_date(event_date_iso) or "unknown"
    return f"{ticker}:earnings_call:{qk}"


def load_event_log() -> dict:
    """Load state/event_log.json — returns {} on missing file or parse error."""
    if not EVENT_LOG_FILE.exists():
        return {}
    try:
        with open(EVENT_LOG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load event_log.json: {e}")
        return {}


def save_event_log(log: dict) -> None:
    """Persist event log atomically; prunes entries older than 180 days."""
    log = prune_event_log(log)
    EVENT_LOG_FILE.parent.mkdir(exist_ok=True)
    tmp = EVENT_LOG_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(log, f, indent=2, default=str)
        tmp.replace(EVENT_LOG_FILE)
    except OSError as e:
        logger.warning(f"Could not write event_log.json: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def prune_event_log(log: dict, days: int = EVENT_LOG_PRUNE_DAYS) -> dict:
    """Remove events older than `days` days."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return {k: v for k, v in log.items()
            if v.get("event_date_iso", "9999") >= cutoff}


def is_known_event(event_key: str, log: dict) -> bool:
    """True if this event already has a primary verdict in the log."""
    return event_key in log


def record_event_primary(
    event_key: str,
    source: str,
    primary_verdict: str,
    primary_score: float,
    event_date_iso: str,
    log: dict,
) -> None:
    """Record the first observation of an event as the primary verdict.

    Mutates `log` in-place.  Caller is responsible for save_event_log().
    """
    log[event_key] = {
        "first_observed": datetime.utcnow().isoformat(),
        "first_observation_source": source,
        "primary_verdict": primary_verdict,
        "primary_score": round(primary_score, 3),
        "event_date_iso": event_date_iso,
        "observations": [],
        "divergence_flagged": False,
        "n_observations": 1,
    }
    logger.info(f"EventLog: primary recorded for {event_key} ({primary_verdict}, score={primary_score:+.1f})")


def record_event_observation(
    event_key: str,
    source: str,
    summary: str,
    score_direction: str,    # "bearish" | "bullish" | "neutral"
    log: dict,
) -> bool:
    """Record a subsequent observation of a known event.

    Returns True if this observation DIVERGES from the primary verdict
    (i.e. primary is bearish but this observation is bullish or vice versa).
    Mutates `log` in-place.  Caller is responsible for save_event_log().
    """
    entry = log.get(event_key)
    if entry is None:
        logger.warning(f"EventLog.record_observation: unknown event_key {event_key}")
        return False
    entry["observations"].append({
        "source": source,
        "ts": datetime.utcnow().isoformat(),
        "summary": summary,
        "verdict": score_direction,
    })
    entry["n_observations"] = entry.get("n_observations", 1) + 1
    # Divergence: primary verdict disagrees with this observation
    primary = entry.get("primary_verdict", "neutral")
    diverges = (
        primary in ("bearish", "bullish")
        and score_direction in ("bearish", "bullish")
        and primary != score_direction
    )
    if diverges:
        entry["divergence_flagged"] = True
        logger.info(
            f"EventLog: divergence flagged for {event_key} "
            f"(primary={primary}, echo={score_direction}, source={source})"
        )
    return diverges


def get_divergent_events(log: dict, days: int = 30) -> list[dict]:
    """Return events with divergence_flagged=True that occurred in the last `days` days."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [
        {"event_key": k, **v}
        for k, v in log.items()
        if v.get("divergence_flagged") and v.get("event_date_iso", "0000") >= cutoff
    ]
