"""Context-length volume-share aggregator (v3.4.14).

Reads `context_volume_signals` entries accumulated by Layer 1 Haiku
from hyperscaler / AI-lab earnings transcripts and proposes updates to
the volume_share_pct values in state/context_length_state.json.

Design:
  - Runs every fast-tier pass (cheap: only reads extraction logs + state)
  - ≥ 3 consistent directional signals for a category → proposal written
  - Max delta ≤ 10pp per category AND ≥ 5 signals → auto-commit
  - Delta > 10pp per category → written as pending_confirmation, surfaced
    in the brief diagnostic section
  - Shares are always rebalanced to sum to 100 after any delta
  - Safe-failing: exception preserves prior state
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONTEXT_STATE_FILE = Path("state/context_length_state.json")
EXTRACTION_LOG_DIR = Path("state")
EXTRACTION_LOG_GLOB = "extraction_log_*.jsonl"

CATEGORIES = [
    "consumer_chat",
    "coding_agents",
    "enterprise_vertical_agents",
    "developer_raw_api",
    "other_creative_rag",
]

# Minimum number of signals before we write any proposal
MIN_SIGNALS_FOR_PROPOSAL = 3
# Auto-commit threshold: max pp change per category (inclusive)
AUTO_COMMIT_MAX_DELTA_PP = 10
# Minimum signals for auto-commit
MIN_SIGNALS_FOR_AUTO_COMMIT = 5

# Magnitude → approximate pp adjustment (directional heuristic)
_MAGNITUDE_PP: list[tuple[re.Pattern, float]] = [
    (re.compile(r"tripl|3x|300%", re.I), 7.0),
    (re.compile(r"doubl|2x|200%", re.I), 5.0),
    (re.compile(r"rapid|surge|explod|signific", re.I), 4.0),
    (re.compile(r"grow|increas|expand|ramp|acceler", re.I), 3.0),
    (re.compile(r"slight|moderat|gradual", re.I), 2.0),
    (re.compile(r"declin|shrink|compress|fall|drop", re.I), -3.0),
    (re.compile(r"rapid.*declin|sharp.*fall|collaps", re.I), -5.0),
]


# ── Extraction helpers ────────────────────────────────────────────────────────

def _iter_extraction_log_entries(lookback_days: int) -> list[dict]:
    entries = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    for path in sorted(EXTRACTION_LOG_DIR.glob(EXTRACTION_LOG_GLOB)):
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts_str = (e.get("extracted_at_iso") or e.get("published_date_iso") or e.get("date") or e.get("extracted_at") or e.get("timestamp"))
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if ts >= cutoff:
                    entries.append(e)
        except Exception as exc:
            logger.warning(f"Could not read extraction log {path}: {exc}")
    return entries


def _extract_volume_signals(entries: list[dict]) -> list[dict]:
    """Pull context_volume_signals from architecture_signals in each entry."""
    signals = []
    for e in entries:
        # v3.5.1 — extraction_log writer puts arch signals under "signals" key.
        arch = e.get("signals") or e.get("architecture_signals") or {}
        sig = arch.get("context_volume_signals")
        if not sig or not isinstance(sig, dict):
            continue
        category = sig.get("category")
        direction = sig.get("direction")
        if category not in CATEGORIES or direction not in ("growing", "shrinking", "stable"):
            continue
        signals.append({
            "category": category,
            "direction": direction,
            "signal_type": sig.get("signal_type", ""),
            "magnitude_hint": sig.get("magnitude_hint", ""),
            "source_company": sig.get("source_company", ""),
            "date": e.get("extracted_at_iso") or e.get("published_date_iso") or e.get("date") or "",
        })
    return signals


# ── Share adjustment logic ────────────────────────────────────────────────────

def _magnitude_to_pp(hint: str, direction: str) -> float:
    """Map magnitude_hint string to a pp adjustment (positive = growing)."""
    for pattern, pp in _MAGNITUDE_PP:
        if pattern.search(hint):
            return abs(pp) if direction == "growing" else -abs(pp)
    # Default if no magnitude keyword
    return 3.0 if direction == "growing" else -3.0


def _compute_direction_summary(signals: list[dict]) -> dict[str, dict]:
    """Per-category: count growing / shrinking / stable and sum pp adjustments."""
    summary: dict[str, dict] = {cat: {
        "growing": 0, "shrinking": 0, "stable": 0,
        "total": 0, "net_pp": 0.0, "sources": [], "evidence_quotes": [],
    } for cat in CATEGORIES}

    for sig in signals:
        cat = sig["category"]
        direction = sig["direction"]
        summary[cat][direction] += 1
        summary[cat]["total"] += 1
        pp = _magnitude_to_pp(sig.get("magnitude_hint", ""), direction)
        summary[cat]["net_pp"] += pp
        src = sig.get("source_company", "")
        if src and src not in summary[cat]["sources"]:
            summary[cat]["sources"].append(src)
        hint = sig.get("magnitude_hint", "")
        if hint:
            summary[cat]["evidence_quotes"].append(
                f"{src}: {hint}" if src else hint
            )

    return summary


def _dominant_direction(cat_summary: dict) -> Optional[str]:
    """Return 'growing' or 'shrinking' if one direction dominates, else None."""
    g = cat_summary["growing"]
    s = cat_summary["shrinking"]
    st = cat_summary["stable"]
    total = g + s + st
    if total == 0:
        return None
    if g >= 2 * max(s, st) and g >= MIN_SIGNALS_FOR_PROPOSAL:
        return "growing"
    if s >= 2 * max(g, st) and s >= MIN_SIGNALS_FOR_PROPOSAL:
        return "shrinking"
    return None


def _propose_shares(
    committed: dict[str, float],
    direction_summary: dict[str, dict],
) -> tuple[Optional[dict[str, float]], list[str]]:
    """Compute proposed shares from committed + directional signals.

    Returns (proposed_shares_dict, evidence_list) or (None, []) if not enough signals.
    """
    adjustments: dict[str, float] = {}
    evidence: list[str] = []

    for cat in CATEGORIES:
        cat_sum = direction_summary[cat]
        direction = _dominant_direction(cat_sum)
        if direction is None:
            adjustments[cat] = 0.0
            continue
        # Use mean pp across consistent-direction signals
        pp_adj = cat_sum["net_pp"] / max(cat_sum["total"], 1)
        # Cap per-category adjustment at 7pp to avoid wild single-quarter swings
        pp_adj = max(-7.0, min(7.0, pp_adj))
        adjustments[cat] = pp_adj
        quotes = cat_sum["evidence_quotes"][:2]     # max 2 quotes per category
        if quotes:
            evidence.append(f"{cat} ({direction}): {'; '.join(quotes)}")

    # Check if any category has a meaningful adjustment
    if not any(abs(v) >= 1.5 for v in adjustments.values()):
        return None, []

    # Apply adjustments to committed shares
    proposed = {cat: committed.get(cat, 0.0) + adjustments[cat] for cat in CATEGORIES}

    # Clamp each share to [1, 90]
    proposed = {cat: max(1.0, min(90.0, v)) for cat, v in proposed.items()}

    # Renormalise to 100
    total = sum(proposed.values())
    if total > 0:
        scale = 100.0 / total
        proposed = {cat: round(v * scale, 1) for cat, v in proposed.items()}

    # Final check: must sum within 0.5 of 100
    if abs(sum(proposed.values()) - 100.0) > 0.5:
        logger.warning("Volume share proposal doesn't sum to 100 after renormalisation; skipping")
        return None, []

    return proposed, evidence


def _max_delta_pp(committed: dict, proposed: dict) -> float:
    return max(abs(proposed.get(cat, 0) - committed.get(cat, 0)) for cat in CATEGORIES)


def _total_signals(direction_summary: dict) -> int:
    return sum(d["total"] for d in direction_summary.values())


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_context_state() -> dict:
    if not CONTEXT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(CONTEXT_STATE_FILE.read_text())
    except Exception as exc:
        logger.warning(f"context_length_state.json unreadable: {exc}")
        return {}


def _save_context_state(state: dict) -> None:
    CONTEXT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTEXT_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(CONTEXT_STATE_FILE)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_context_volume_aggregator(lookback_days: int = 90) -> dict:
    """Read extraction logs, propose or commit volume share updates.

    Returns a summary dict for diagnostics.
    """
    try:
        state = _load_context_state()
        if not state:
            logger.info("Context volume aggregator: no state file yet; skipping")
            return {"status": "no_state"}

        entries = _iter_extraction_log_entries(lookback_days)
        signals = _extract_volume_signals(entries)
        logger.info(
            f"Context volume aggregator: {len(entries)} log entries, "
            f"{len(signals)} volume signals found"
        )

        if not signals:
            return {"status": "no_signals", "signals_found": 0}

        direction_summary = _compute_direction_summary(signals)
        committed = (state.get("volume_shares") or {}).get("committed") or {}

        if not committed:
            logger.warning("Context volume aggregator: no committed shares in state; skipping")
            return {"status": "no_committed_shares"}

        proposed, evidence = _propose_shares(committed, direction_summary)
        if proposed is None:
            logger.info("Context volume aggregator: insufficient signal for a proposal")
            return {"status": "insufficient_signal", "signals_found": len(signals)}

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        n_signals = _total_signals(direction_summary)
        max_delta = _max_delta_pp(committed, proposed)

        vs = state.setdefault("volume_shares", {})

        if max_delta <= AUTO_COMMIT_MAX_DELTA_PP and n_signals >= MIN_SIGNALS_FOR_AUTO_COMMIT:
            # Auto-commit
            vs["history"] = vs.get("history") or []
            vs["history"].append({
                "date": today,
                "shares": dict(committed),
                "trigger": f"auto_commit_{n_signals}_signals",
            })
            vs["committed"] = proposed
            vs["proposed"] = None
            vs["proposal_evidence"] = []
            vs["proposal_status"] = "none"
            vs["proposal_created"] = None
            logger.info(
                f"Context volume aggregator: AUTO-COMMITTED shares "
                f"(max_delta={max_delta:.1f}pp, signals={n_signals})"
            )
            status = "auto_committed"
        else:
            # Write as pending — requires manual confirmation
            vs["proposed"] = proposed
            vs["proposal_evidence"] = evidence
            vs["proposal_status"] = "pending_confirmation"
            vs["proposal_created"] = today
            logger.info(
                f"Context volume aggregator: proposal written as pending_confirmation "
                f"(max_delta={max_delta:.1f}pp, signals={n_signals})"
            )
            status = "pending_confirmation"

        state.setdefault("_metadata", {})["last_aggregator_run"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        _save_context_state(state)
        return {
            "status": status,
            "signals_found": n_signals,
            "max_delta_pp": max_delta,
            "evidence_count": len(evidence),
        }

    except Exception as exc:
        logger.error(
            f"Context volume aggregator failed (state preserved): {exc}", exc_info=True
        )
        return {"status": "error", "error": str(exc)}
