"""v3.42.0 — the computed-verdict table.

The Jul-29 brief carried the same indicator with opposite statuses in the
same email (EPS-revisions banner FIRED on the deterministic score while the
assessment said Partially fired; P/E red on top, green in the exec pills).
Root disease: multiple components each applying their own fire definition.
v3.41.18 fixed it for P/E alone; this module generalizes the fix — for every
indicator with a deterministic verdict available in the run, ONE evaluator
here owns the status, and fast_tier applies it to BOTH legs:

    pre-store  → Layer 2 signal_reasonings (so the evidence store records it)
    post-Sonnet → indicator_assessments (so the render/prior_state/trigger
                  log see it)

Enforcement direction per indicator:
    two_sided  — the computed number fully determines status (pure numeric).
    up_only    — the evaluator can FORCE a fire but never force a relax;
                 relaxation nuance (cadence rules, partial states) stays
                 with Layer 2. Used where the deterministic data covers the
                 fire condition but not the full state space.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

EPS_SCORE_FIRE = -15.0     # canonical: 30d weighted score < -15 → Fired
CAPEX_OCF_FIRE = 0.50      # locked: mean >50% Fired
CAPEX_OCF_PARTIAL = 0.40   # locked: mean >40% Partially fired


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def evaluate_pe(computed_metrics: dict) -> Optional[dict]:
    pe = (computed_metrics or {}).get("forward_pe") or {}
    thresholds = [("SK Hynix", "000660.KS", 5.0), ("Samsung", "005930.KS", 6.0),
                  ("Micron", "MU", 8.0)]
    readings, breaches, n = [], [], 0
    for label, tk, thr in thresholds:
        v = _num(pe.get(tk))
        if v is None:
            continue
        n += 1
        readings.append(f"{label} {v:.2f}x({'<' if v < thr else '>='}{thr:g})")
        if v < thr:
            breaches.append(label)
    if n == 0:
        return None  # no data → assessment applier defers to Layer 2 (v3.41.18 rule)
    return {"status": "Fired" if breaches else "Not fired",
            "mode": "two_sided",
            "basis": f"computed forward P/E — {'; '.join(readings)}"}


def evaluate_eps_revisions(signal_reasonings: list) -> Optional[dict]:
    """From Layer 2's own weighted-scoring engine output (the same numbers
    the bespoke EPS banner renders). up_only: a 30d score through the fire
    threshold FORCES Fired; above the threshold, Layer 2's cadence rules
    (early-signal partials on Tier-A target cuts, etc.) legitimately govern."""
    for r in signal_reasonings or []:
        if getattr(r, "signal_id", None) != "t2_eps_revisions":
            continue
        try:
            sig = (getattr(r, "raw_evidence_dump", {}) or {}).get(
                "signal_specific_data", {}) or {}
            verdict = sig.get("weighted_scoring_verdict") or {}
        except Exception:
            return None
        worst, worst_tk = None, None
        per = verdict.get("per_ticker") or verdict.get("tickers") or {}
        if isinstance(per, dict):
            for tk, row in per.items():
                if not isinstance(row, dict):
                    continue
                v = _num(row.get("net_30d", row.get("score_30d", row.get("r30"))))
                if v is not None and (worst is None or v < worst):
                    worst, worst_tk = v, tk
        if worst is None:
            worst = _num(verdict.get("net_30d") or verdict.get("score_30d"))
            worst_tk = "basket"
        if worst is None:
            return None
        if worst < EPS_SCORE_FIRE:
            return {"status": "Fired", "mode": "up_only",
                    "basis": (f"30d weighted revision score {worst:+.1f} "
                              f"({worst_tk}) < {EPS_SCORE_FIRE:g} canonical threshold")}
        return None
    return None


def evaluate_rel_strength(computed_metrics: dict) -> Optional[dict]:
    rs = (computed_metrics or {}).get("relative_strength_full") or {}
    sev = rs.get("overall_severity")
    if not rs:
        return None
    subs = "; ".join(d for _, d in (rs.get("fired_sub_triggers") or [])) or "no spread breach"
    status = {"red": "Fired", "yellow": "Partially fired"}.get(sev, "Not fired")
    return {"status": status, "mode": "two_sided",
            "basis": f"computed relative-strength spreads — {subs}"}


def evaluate_capex_ocf(computed_metrics: dict) -> Optional[dict]:
    cm = computed_metrics or {}
    mean = None
    for key in ("mean_capex_ocf_ratio_2q", "mean_capex_ocf_ratio_latest_q"):
        mean = _num(cm.get(key))
        if mean is not None:
            break
    if mean is None:
        ratios = cm.get("capex_ocf_ratios") or {}
        vals = [_num(v) for v in ratios.values() if _num(v) is not None]
        mean = sum(vals) / len(vals) if vals else None
    if mean is None:
        return None
    if mean > CAPEX_OCF_FIRE:
        status = "Fired"
    elif mean > CAPEX_OCF_PARTIAL:
        status = "Partially fired"
    else:
        status = "Not fired"
    return {"status": status, "mode": "two_sided",
            "basis": (f"mean hyperscaler capex/OCF {mean*100:.1f}% vs "
                      f"{CAPEX_OCF_PARTIAL*100:.0f}%/{CAPEX_OCF_FIRE*100:.0f}% thresholds")}


# Indicator id → evaluator. t1_spot_decline keeps its existing dedicated
# matrix override (predates this table, works, not churned). t3_eps_miss
# determinization is gated on an earnings-actuals feed (Release B); until
# then its Layer-2 spec carries the Jul-29 named counterexample (config.py).
def evaluate_all(computed_metrics: dict, signal_reasonings: list) -> dict:
    out = {}
    for ind_id, fn in (
        ("t2_pe_compressed", lambda: evaluate_pe(computed_metrics)),
        ("t2_eps_revisions", lambda: evaluate_eps_revisions(signal_reasonings)),
        ("t3_relative_strength", lambda: evaluate_rel_strength(computed_metrics)),
        ("t2_hyperscaler_capex_revenue", lambda: evaluate_capex_ocf(computed_metrics)),
    ):
        try:
            v = fn()
        except Exception as e:
            logger.warning(f"[computed-verdict] {ind_id} evaluator failed (non-fatal): {e}")
            v = None
        if v is not None:
            out[ind_id] = v
    return out


def apply_to_objects(objs: list, verdicts: dict, status_attr: str,
                     id_attr: str, note_attr: Optional[str],
                     stage: str) -> None:
    """Apply the verdict table to reasonings (pre-store) or assessments
    (post-Sonnet). up_only verdicts never move a status downward."""
    by_id = {getattr(o, id_attr, None): o for o in (objs or [])}
    for ind_id, v in (verdicts or {}).items():
        obj = by_id.get(ind_id)
        if obj is None:
            continue
        old = getattr(obj, status_attr, None)
        new = v["status"]
        if old == new:
            continue
        if v.get("mode") == "up_only":
            rank = {"Not fired": 0, "Partially fired": 1, "Fired": 2}
            if rank.get(new, 0) <= rank.get(old, 0):
                continue
        setattr(obj, status_attr, new)
        if note_attr:
            note = f"Status set deterministically ({v['basis']})."
            prior = getattr(obj, note_attr, None)
            setattr(obj, note_attr, note if not prior else f"{note} {prior}")
        logger.warning(f"[computed-verdict] {stage}: {ind_id} {old!r} → {new!r} ({v['basis']})")
