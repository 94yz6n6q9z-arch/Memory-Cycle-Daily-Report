"""v3.41.0 — Layer 3 indicator-prose cache (dormancy + staggered weekly refresh).

Lets the daily Sonnet synthesis SKIP regenerating a per-indicator card's prose
when nothing material changed for that indicator, reusing the cached prose
instead. This cuts the dominant cost driver — Sonnet OUTPUT tokens — on quiet
days (most days), where today's pipeline re-writes full prose for all ~23
indicators even when zero of them changed.

Design (locked 2026-06-13):
  - Dormancy: an indicator's VERDICT-SHAPE fingerprint unchanged → reuse its
    cached card. The fingerprint hashes ONLY fired_status / consensus_direction
    / convergence (Option A, v3.41.1). evidence_count / evidence_sources /
    confidence are EXCLUDED — they track the daily news churn, not whether the
    verdict materially changed, and were flipping ~22/24 cards every day.
  - Staggered weekly refresh: each card carries last_full_regen_date and is
    force-regenerated when >= REFRESH_DAYS since its own last full rebuild.
    First-deploy dates are SEEDED offset across the prior week so ~1/7 of the
    cards come due on any given day — cost spread evenly, no weekly spike.
    A regeneration (for any reason) resets that card's clock, which keeps the
    stagger self-maintaining.
  - Consistency guard: a cached card is reused only if its fingerprint matches
    AND its stored status equals the current computed status; any mismatch
    forces regeneration. This prevents reusing stale prose that would
    contradict the banners/section-4 cards (the v3.39.10/11 lesson).

Pure logic + one JSON file. The orchestrator (fast_tier) decides WHEN to call
it. Rollout step (i) uses evaluate_cache() in SHADOW mode (log only) and
update_cache_after_synthesis() to populate the store, with NO behavior change.
Steps (ii)/(iii) act on the decisions and feed cached prose back to Sonnet.

Safe-failing throughout: any load/save/compute error is logged and degrades to
"regenerate everything" (the current behavior) — the brief always ships.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("memory_cycle_brief.prose_cache")

PROSE_CACHE_FILE = Path("state/indicator_prose_cache.json")
REFRESH_DAYS = 7

# Cacheable IndicatorAssessment prose fields. Static name/description/
# what_to_look_for are sourced from config at render time, never cached.
_PROSE_FIELDS = (
    "current_status",
    "where_we_stand",
    "latest_developments",
    "relevant_links",
    "change_vs_prior",
)
_PROSE_PRESENCE_FIELD = "where_we_stand"  # truthy => a usable cached card exists


def _today(today: Optional[date] = None) -> date:
    return today or datetime.now(timezone.utc).date()


def load_prose_cache() -> dict:
    """Return {indicator_id: {<prose fields>, fingerprint, status,
    last_full_regen_date, updated_date}}. Empty dict on missing/corrupt file."""
    try:
        if PROSE_CACHE_FILE.exists():
            data = json.loads(PROSE_CACHE_FILE.read_text())
            if isinstance(data, dict):
                return data.get("cards", {}) if "cards" in data else data
    except Exception as e:
        logger.warning(f"prose cache load failed ({e}); starting empty")
    return {}


def save_prose_cache(cache: dict) -> None:
    try:
        PROSE_CACHE_FILE.parent.mkdir(exist_ok=True)
        PROSE_CACHE_FILE.write_text(json.dumps({"cards": cache}, indent=2))
    except Exception as e:
        logger.warning(f"prose cache save failed ({e}); not persisted")


def compute_fingerprint(reasoning) -> str:
    """Option A (v3.41.1): hash only the VERDICT-SHAPE fields — fired_status,
    consensus_direction, convergence.

    Evidence count/sources and confidence are EXCLUDED. The daily news set
    churns (different items fetched each day), so evidence_count/evidence_sources
    move almost every run even when the verdict is unchanged — confirmed by the
    2026-06-14 shadow run: 22/24 cards flagged 'fingerprint_changed' on a quiet
    Sunday, and the ONLY reuse was the single indicator with zero evidence. A
    card now reuses its prose while its verdict shape holds; new-but-non-verdict-
    changing evidence is surfaced by the weekly refresh + the always-fresh exec
    brief, not by re-writing every card daily. Empty string on error → never
    matches a stored fingerprint → forces regeneration (safe).
    """
    try:
        parts = [
            str(getattr(reasoning, "fired_status", "")),
            str(getattr(reasoning, "consensus_direction", "")),
            str(getattr(reasoning, "convergence", "")),
        ]
        return hashlib.sha256("\u00a6".join(parts).encode("utf-8")).hexdigest()[:16]
    except Exception as e:
        logger.warning(f"fingerprint failed ({e}); forcing regen for this card")
        return ""


def seed_staggered_dates(cache: dict, indicator_ids: list, today: Optional[date] = None) -> None:
    """For indicators with no last_full_regen_date yet, assign dates offset
    across the prior REFRESH_DAYS days so weekly refreshes spread evenly rather
    than all firing on the same day. Mutates cache in place. Idempotent — only
    fills missing dates, never overwrites an existing one."""
    t = _today(today)
    for i, ind_id in enumerate(indicator_ids):
        if not ind_id:
            continue
        entry = cache.setdefault(ind_id, {})
        if not entry.get("last_full_regen_date"):
            offset = i % REFRESH_DAYS
            entry["last_full_regen_date"] = (t - timedelta(days=offset)).isoformat()


def _days_since(date_iso: Optional[str], t: date) -> Optional[int]:
    if not date_iso:
        return None
    try:
        return (t - date.fromisoformat(date_iso)).days
    except Exception:
        return None


def evaluate_cache(cache: dict, reasonings: list, today: Optional[date] = None,
                   refresh_days: int = REFRESH_DAYS) -> dict:
    """Per-indicator reuse/regenerate decision (before the status guard).

    Returns {indicator_id: {"action": "reuse"|"regen", "reason": str}} for every
    id present in `reasonings`. Order of precedence for 'regen': no usable cache
    → fingerprint changed → weekly refresh due. Otherwise 'reuse'.
    """
    t = _today(today)
    decisions: dict = {}
    for r in reasonings:
        ind_id = getattr(r, "signal_id", None)
        if not ind_id:
            continue
        entry = cache.get(ind_id) or {}
        cur_fp = compute_fingerprint(r)
        age = _days_since(entry.get("last_full_regen_date"), t)

        if not entry.get(_PROSE_PRESENCE_FIELD):
            decisions[ind_id] = {"action": "regen", "reason": "no_cache"}
        elif not cur_fp or cur_fp != entry.get("fingerprint"):
            decisions[ind_id] = {"action": "regen", "reason": "fingerprint_changed"}
        elif age is None or age >= refresh_days:
            decisions[ind_id] = {"action": "regen", "reason": f"weekly_refresh_due(age={age})"}
        else:
            decisions[ind_id] = {"action": "reuse", "reason": f"dormant(age={age})"}
    return decisions


def apply_status_guard(decisions: dict, cache: dict, current_status_by_id: dict) -> dict:
    """Consistency guard: flip any 'reuse' whose cached status disagrees with the
    current computed status to 'regen'. Mutates and returns `decisions`."""
    for ind_id, dec in decisions.items():
        if dec.get("action") != "reuse":
            continue
        cached_status = (cache.get(ind_id) or {}).get("status")
        cur_status = current_status_by_id.get(ind_id)
        if cur_status is not None and cached_status is not None and cached_status != cur_status:
            decisions[ind_id] = {
                "action": "regen",
                "reason": f"status_mismatch(cached={cached_status},computed={cur_status})",
            }
    return decisions


def get_cached_assessment_dict(cache: dict, ind_id: str) -> Optional[dict]:
    """Return the cached prose fields for an indicator (or None if absent)."""
    entry = cache.get(ind_id)
    if not entry or not entry.get(_PROSE_PRESENCE_FIELD):
        return None
    return {f: entry.get(f) for f in _PROSE_FIELDS}


def summarize_decisions(decisions: dict) -> str:
    """One-line summary for shadow logging."""
    reuse = [i for i, d in decisions.items() if d.get("action") == "reuse"]
    regen = {i: d["reason"] for i, d in decisions.items() if d.get("action") == "regen"}
    return (f"{len(reuse)} reuse / {len(regen)} regen | "
            f"reuse={sorted(reuse)} | regen={regen}")


def update_cache_after_synthesis(cache: dict, assessments: list, reasonings: list,
                                 *, regenerated_ids: set, clock_reset_ids: Optional[set] = None,
                                 today: Optional[date] = None) -> None:
    """Persist fresh prose + current fingerprint for each (re)generated card and
    reset last_full_regen_date for cards in `clock_reset_ids`. Reused cards are
    left untouched (their cached prose persists). Mutates cache in place.

    - regenerated_ids: ids Sonnet actually wrote this run → store their prose.
    - clock_reset_ids: subset whose weekly clock resets to today. Defaults to
      regenerated_ids. The caller passes regen ids EXCLUDING `no_cache` ones so
      first-population cards keep their SEEDED (staggered) date instead of all
      snapping to today and re-clustering the weekly refresh.
    """
    if clock_reset_ids is None:
        clock_reset_ids = set(regenerated_ids)
    t_iso = _today(today).isoformat()
    fp_by_id, status_by_id = {}, {}
    for r in reasonings:
        rid = getattr(r, "signal_id", None)
        if rid:
            fp_by_id[rid] = compute_fingerprint(r)
            status_by_id[rid] = getattr(r, "fired_status", None)

    assess_by_id = {getattr(a, "id", None): a for a in assessments}
    for ind_id in regenerated_ids:
        a = assess_by_id.get(ind_id)
        if a is None:
            continue
        entry = cache.setdefault(ind_id, {})
        for f in _PROSE_FIELDS:
            entry[f] = getattr(a, f, None)
        entry["fingerprint"] = fp_by_id.get(ind_id, "")
        entry["status"] = status_by_id.get(ind_id) or getattr(a, "current_status", None)
        entry["updated_date"] = t_iso
        if ind_id in clock_reset_ids:
            entry["last_full_regen_date"] = t_iso


def split_regen_ids(decisions: dict) -> tuple:
    """Helper for the caller: from evaluate_cache()/apply_status_guard() output,
    return (regenerated_ids, clock_reset_ids). clock_reset_ids excludes the
    'no_cache' regens so freshly-seeded staggered dates are preserved."""
    regen = {i for i, d in decisions.items() if d.get("action") == "regen"}
    reset = {i for i in regen
             if not str(decisions[i].get("reason", "")).startswith("no_cache")}
    return regen, reset
