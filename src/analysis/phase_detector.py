"""v3.4.11 — Deterministic Phase 4 → Phase 5 transition detector.

Background
==========
The cycle phase (Phase 4 = Boom, Phase 5 = Decline) drives several
downstream behaviors:
  - The exec summary's "We are currently in Phase N (...)" line
  - The case-scenario invariant in llm_summarizer (Phase 4 requires
    base.return_to_peak_pct >= 0; Phase 5 drops this clamp)
  - The supercycle chart's phase shading anchor (Phase 4 END vs Phase 5 END)
  - The template's "return to peak" vs "return to trough" labels

Pre-v3.4.11, the phase number was either hardcoded in CYCLE_BASELINE or
emitted freely by Sonnet with no rule to advance from 4 to 5. The framework
shipped without a Phase 5 path at all — every assumption assumed peak is in
the future.

v3.4.11 adds three composite OR-conditions for Phase 5 entry, anti-flicker
hysteresis, and an asymmetric reversion path. Conditions encode the tier
framework's lead-time semantics (T3 = concurrent with peak, T4 = post-peak)
plus price-action confirmation.

The three conditions
====================
Phase 5 is entered when ANY of A/B/C trips on >= 2 consecutive runs:

  A — Concurrent + price:
      >= 2 of 5 T3 indicators Fired
      AND SK Hynix close >= 10% below trailing 90-day high
      AND both held >= 5 calendar days

  B — Lagging confirmation:
      >= 1 of 4 T4 indicators Fired
      AND t2_eps_revisions Fired
      AND >= 2 other T2 indicators Fired (>= 3 T2 total)
      AND SK Hynix close >= 15% below trailing 90-day high

  C — Decisive price action:
      SK Hynix close >= 20% below trailing 90-day high
      AND that 90-day high is >= 45 days old
      AND t2_eps_revisions Fired

Reversion (Phase 5 → Phase 4) requires BOTH:
  - SK Hynix sets a new 90-day high (previous "peak" was not in fact the peak)
  - < 2 T3 indicators still Fired (concurrent confirmation has dissipated)

Tunable parameters live in config.PHASE_5_DETECTION so they can be adjusted
without code change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger("memory_cycle_brief.phase_detector")


# ============================================================
# Result dataclasses
# ============================================================

@dataclass
class ConditionEval:
    """Per-condition evaluation result. Each field is informational —
    `met` is the boolean that drives the cascade."""
    name: str               # "A" | "B" | "C"
    met: bool
    detail: dict[str, Any]  # opaque per-condition diagnostic dict


@dataclass
class PhaseDetectionResult:
    """v3.4.11 — Output of evaluate_phase_for_run().

    Carries enough information to (a) override exec_summary.current_phase
    downstream, (b) update persisted state, (c) log a one-line summary of
    why the phase is what it is.
    """
    current_phase: int               # 4 or 5
    current_phase_label: str         # "Boom / Euphoria (late stage)" | "Decline"
    flipped_this_run: bool           # True only on the run when phase changes
    pending_condition: Optional[str] # "A" | "B" | "C" | None — anti-flicker
    pending_first_seen_iso: Optional[str]
    phase_5_detected_date_iso: Optional[str]
    cycle_peak_price_at_detection: Optional[float]
    condition_evals: list[ConditionEval]
    reversion_eligible: bool          # only meaningful when current_phase == 5
    summary_line: str                 # one-line human-readable diagnostic


# ============================================================
# Helpers — trailing high, T-tier counting
# ============================================================

def compute_trailing_high(
    sk_hynix_history: pd.Series,
    window_days: int,
    as_of: Optional[date] = None,
) -> Optional[dict[str, Any]]:
    """Return {high_price, high_date, current_price, drawdown_pct, days_since_high}
    for the trailing-window relative to as_of (defaults to today).

    Returns None if history is empty or all-NaN. Defensive: NaN-skip via
    pandas dropna; assumes daily-resolution input.
    """
    if sk_hynix_history is None or len(sk_hynix_history) == 0:
        return None

    if as_of is None:
        as_of = datetime.utcnow().date()
    elif isinstance(as_of, datetime):
        as_of = as_of.date()

    # Slice to the trailing window. The index may be timezone-aware
    # DatetimeIndex or plain DatetimeIndex; coerce both to naive dates for the
    # comparison to avoid tz-mismatch crashes.
    try:
        idx = pd.to_datetime(sk_hynix_history.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        series = pd.Series(sk_hynix_history.values, index=idx).dropna()
    except Exception as e:
        logger.warning(f"compute_trailing_high: could not normalize index ({e}); using as-is")
        series = sk_hynix_history.dropna()

    if len(series) == 0:
        return None

    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=window_days)
    window = series[series.index >= cutoff]
    if len(window) == 0:
        # Fall back to all available history if the window is empty
        window = series

    high_price = float(window.max())
    high_date = window.idxmax()
    if hasattr(high_date, "date"):
        high_date_iso = high_date.date().isoformat()
        days_since_high = (pd.Timestamp(as_of) - high_date).days
    else:
        high_date_iso = str(high_date)
        days_since_high = 0

    current_price = float(series.iloc[-1])
    if high_price > 0:
        drawdown_pct = (current_price - high_price) / high_price * 100.0
    else:
        drawdown_pct = 0.0

    return {
        "high_price": high_price,
        "high_date_iso": high_date_iso,
        "current_price": current_price,
        "drawdown_pct": drawdown_pct,        # negative when below high
        "days_since_high": int(days_since_high),
        "window_days": window_days,
    }


def count_fired_in_tier(
    assessments: list,
    tier: int,
    indicator_ids: Optional[set[str]] = None,
    include_partial: bool = False,
) -> int:
    """Count indicators in a given tier with status 'Fired' (or 'Partially fired'
    if include_partial=True).

    If `indicator_ids` is provided, restrict to that subset; otherwise count
    all indicators in the tier.

    Defensive: each assessment must have .tier, .id, .current_status; missing
    fields → skipped.
    """
    count = 0
    for a in assessments or []:
        try:
            a_tier = getattr(a, "tier", None)
            a_id = getattr(a, "id", None)
            a_status = getattr(a, "current_status", "")
        except Exception:
            continue
        if a_tier != tier:
            continue
        if indicator_ids is not None and a_id not in indicator_ids:
            continue
        if a_status == "Fired":
            count += 1
        elif include_partial and a_status == "Partially fired":
            count += 1
    return count


def is_indicator_fired(assessments: list, indicator_id: str) -> bool:
    """True iff the named indicator has current_status == 'Fired'."""
    for a in assessments or []:
        try:
            if getattr(a, "id", None) == indicator_id:
                return getattr(a, "current_status", "") == "Fired"
        except Exception:
            continue
    return False


# ============================================================
# Per-condition evaluators
# ============================================================

def _evaluate_condition_a(
    assessments: list,
    trailing_high: Optional[dict],
    prior_state: dict,
    params: dict,
    today_iso: str,
) -> ConditionEval:
    """Concurrent + price.

    The anti-whipsaw '5-day held' clause is enforced via prior_state's
    `condition_a_first_seen_iso`: when the threshold is met for the first
    time we stamp it; on subsequent runs we check that the stamp is at
    least `params['anti_whipsaw_days']` old before declaring met=True.

    Note: this is a SEPARATE hysteresis from the 2-consecutive-run
    anti-flicker (handled at the cascade level). Anti-whipsaw guards
    condition A specifically because its threshold is the loosest.
    """
    t3_threshold = params["t3_fire_count_threshold"]
    drawdown_threshold = params["drawdown_threshold_a_pct"]
    held_days = params["anti_whipsaw_days_a"]

    t3_fires = count_fired_in_tier(assessments, tier=3)
    dd = trailing_high["drawdown_pct"] if trailing_high else 0.0
    threshold_met_today = (
        t3_fires >= t3_threshold
        and trailing_high is not None
        and dd <= -drawdown_threshold
    )

    first_seen_iso = prior_state.get("condition_a_first_seen_iso")
    if threshold_met_today and not first_seen_iso:
        # First time meeting the raw threshold — stamp but don't fire yet
        new_first_seen = today_iso
        held_long_enough = False
    elif threshold_met_today and first_seen_iso:
        try:
            first_dt = datetime.fromisoformat(first_seen_iso).date()
            today_dt = datetime.fromisoformat(today_iso).date()
            age_days = (today_dt - first_dt).days
            held_long_enough = age_days >= held_days
            new_first_seen = first_seen_iso  # preserved
        except (ValueError, TypeError):
            held_long_enough = False
            new_first_seen = today_iso
    else:
        # threshold not met today — clear the stamp
        held_long_enough = False
        new_first_seen = None

    return ConditionEval(
        name="A",
        met=bool(threshold_met_today and held_long_enough),
        detail={
            "t3_fires": t3_fires,
            "t3_threshold": t3_threshold,
            "drawdown_pct": dd,
            "drawdown_threshold_pct": -drawdown_threshold,
            "threshold_met_today": threshold_met_today,
            "first_seen_iso": new_first_seen,
            "held_long_enough": held_long_enough,
            "anti_whipsaw_days": held_days,
        },
    )


def _evaluate_condition_b(
    assessments: list,
    trailing_high: Optional[dict],
    params: dict,
) -> ConditionEval:
    """Lagging confirmation. No per-condition hysteresis (covered by
    the cascade-level 2-consecutive-run anti-flicker)."""
    t4_threshold = params["t4_fire_count_threshold"]
    t2_total_threshold = params["t2_total_fire_count_threshold"]
    drawdown_threshold = params["drawdown_threshold_b_pct"]

    t4_fires = count_fired_in_tier(assessments, tier=4)
    t2_fires_total = count_fired_in_tier(assessments, tier=2)
    eps_fired = is_indicator_fired(assessments, "t2_eps_revisions")
    dd = trailing_high["drawdown_pct"] if trailing_high else 0.0

    met = (
        t4_fires >= t4_threshold
        and eps_fired
        and t2_fires_total >= t2_total_threshold
        and trailing_high is not None
        and dd <= -drawdown_threshold
    )

    return ConditionEval(
        name="B",
        met=met,
        detail={
            "t4_fires": t4_fires,
            "t4_threshold": t4_threshold,
            "t2_eps_revisions_fired": eps_fired,
            "t2_fires_total": t2_fires_total,
            "t2_total_threshold": t2_total_threshold,
            "drawdown_pct": dd,
            "drawdown_threshold_pct": -drawdown_threshold,
        },
    )


def _evaluate_condition_c(
    assessments: list,
    trailing_high: Optional[dict],
    params: dict,
) -> ConditionEval:
    """Decisive price action."""
    drawdown_threshold = params["drawdown_threshold_c_pct"]
    stale_high_min_days = params["stale_high_min_days_c"]

    eps_fired = is_indicator_fired(assessments, "t2_eps_revisions")
    if trailing_high is None:
        return ConditionEval(name="C", met=False, detail={"reason": "no_trailing_high"})

    dd = trailing_high["drawdown_pct"]
    days_since_high = trailing_high["days_since_high"]

    met = (
        dd <= -drawdown_threshold
        and days_since_high >= stale_high_min_days
        and eps_fired
    )

    return ConditionEval(
        name="C",
        met=met,
        detail={
            "drawdown_pct": dd,
            "drawdown_threshold_pct": -drawdown_threshold,
            "days_since_high": days_since_high,
            "stale_high_min_days": stale_high_min_days,
            "t2_eps_revisions_fired": eps_fired,
        },
    )


# ============================================================
# Reversion (Phase 5 → Phase 4)
# ============================================================

def _evaluate_reversion(
    assessments: list,
    trailing_high: Optional[dict],
    prior_state: dict,
    params: dict,
) -> tuple[bool, dict]:
    """Reversion requires BOTH:
      - New 90-day high (current price >= prior 90-day high). Tolerance: 0%
        because a single day at the same price is enough to invalidate the
        "previous peak was the peak" thesis.
      - < t3_fire_count_threshold T3 indicators still Fired.

    Returns (reversion_should_fire, detail_dict).
    """
    if trailing_high is None:
        return False, {"reason": "no_trailing_high"}

    # "New high" means current_price >= the (90d-window) high, i.e., drawdown
    # is approximately zero or positive. We use 0% as the cutoff.
    drawdown_pct = trailing_high["drawdown_pct"]
    new_high = drawdown_pct >= 0.0

    t3_threshold = params["t3_fire_count_threshold"]
    t3_fires = count_fired_in_tier(assessments, tier=3)
    t3_dissipated = t3_fires < t3_threshold

    return new_high and t3_dissipated, {
        "drawdown_pct": drawdown_pct,
        "new_high": new_high,
        "t3_fires": t3_fires,
        "t3_threshold": t3_threshold,
        "t3_dissipated": t3_dissipated,
    }


# ============================================================
# Public entry point — single run evaluator
# ============================================================

PHASE_4_LABEL = "Boom / Euphoria (late stage)"
PHASE_5_LABEL = "Decline"


def evaluate_phase_for_run(
    assessments: list,
    sk_hynix_history: Optional[pd.Series],
    prior_phase_state: dict,
    params: dict,
    as_of: Optional[date] = None,
) -> PhaseDetectionResult:
    """v3.4.11 — Single-run phase detector entry point.

    Args:
        assessments: list of IndicatorAssessment-like objects (must have
            .id, .tier, .current_status). Typically
            analysis.indicator_assessments from llm_summarizer.
        sk_hynix_history: pandas Series of SK Hynix close prices indexed by
            date. Typically price_history["SK Hynix"] from fast_tier.
            None or empty → trailing_high is None → A/B/C all evaluate to
            False, phase stays at current (or 4 if no prior).
        prior_phase_state: dict loaded from state/phase_state.json. Empty
            dict on first run.
        params: dict from CYCLE_BASELINE / config.PHASE_5_DETECTION. Keys
            documented in src/config.py.
        as_of: optional date override for testing. Defaults to today UTC.

    Returns PhaseDetectionResult with the new phase, whether it flipped
    this run, the next state to persist, and per-condition diagnostics.
    """
    if as_of is None:
        as_of = datetime.utcnow().date()
    today_iso = as_of.isoformat() if isinstance(as_of, date) else str(as_of)

    # Resolve current phase (default 4 on first run)
    prior_phase = int(prior_phase_state.get("current_phase", 4))

    # Trailing high computation
    window_days = params["trailing_high_window_days"]
    trailing_high = compute_trailing_high(sk_hynix_history, window_days=window_days, as_of=as_of)

    # Evaluate the three conditions
    cond_a = _evaluate_condition_a(assessments, trailing_high, prior_phase_state, params, today_iso)
    cond_b = _evaluate_condition_b(assessments, trailing_high, params)
    cond_c = _evaluate_condition_c(assessments, trailing_high, params)
    condition_evals = [cond_a, cond_b, cond_c]

    any_met = any(c.met for c in condition_evals)
    met_names = [c.name for c in condition_evals if c.met]

    # Defaults — values that may be overridden below
    new_phase = prior_phase
    flipped = False
    new_pending_condition = None
    new_pending_first_seen = None
    new_detected_date = prior_phase_state.get("phase_5_detected_date_iso")
    new_peak_price = prior_phase_state.get("cycle_peak_price_at_detection")

    if prior_phase == 4:
        if any_met:
            prior_pending = prior_phase_state.get("anti_flicker_pending_condition")
            prior_pending_first_seen = prior_phase_state.get("anti_flicker_pending_first_seen_iso")
            # Anti-flicker: require N consecutive runs where ANY condition is met.
            # We don't require the SAME condition each run — a switch from A
            # to B mid-flicker still counts as continuous "any-met" signal.
            anti_flicker_runs = params["anti_flicker_consecutive_runs"]
            if anti_flicker_runs <= 1:
                # No anti-flicker — flip immediately
                new_phase = 5
                flipped = True
                new_detected_date = today_iso
                new_peak_price = trailing_high["high_price"] if trailing_high else None
                new_pending_condition = None
                new_pending_first_seen = None
            elif prior_pending and prior_pending_first_seen:
                # We were pending — now flipping (anti_flicker = 2 = "pending
                # one run + flip on the next" matches the default spec).
                new_phase = 5
                flipped = True
                new_detected_date = today_iso
                new_peak_price = trailing_high["high_price"] if trailing_high else None
                new_pending_condition = None
                new_pending_first_seen = None
            else:
                # First run with any condition met — mark pending, stay Phase 4
                new_pending_condition = ",".join(met_names)
                new_pending_first_seen = today_iso
                new_phase = 4
                flipped = False
        else:
            # No condition met — clear pending if any
            new_pending_condition = None
            new_pending_first_seen = None
            new_phase = 4
            flipped = False
    else:
        # Currently Phase 5 — check reversion
        revert, revert_detail = _evaluate_reversion(assessments, trailing_high, prior_phase_state, params)
        if revert:
            new_phase = 4
            flipped = True
            new_detected_date = None
            new_peak_price = None
            new_pending_condition = None
            new_pending_first_seen = None
            logger.warning(
                f"Phase 5 → Phase 4 REVERSION fired: "
                f"new_high={revert_detail.get('new_high')}, "
                f"t3_fires={revert_detail.get('t3_fires')}"
            )
        else:
            new_phase = 5
            flipped = False
            # Preserve detection date and peak price; clear pending (we're already in 5)
            new_pending_condition = None
            new_pending_first_seen = None

    label = PHASE_5_LABEL if new_phase == 5 else PHASE_4_LABEL

    # Build summary line
    if flipped and new_phase == 5:
        summary = (
            f"Phase 4 → Phase 5 (conditions met: {','.join(met_names)}; "
            f"drawdown={trailing_high['drawdown_pct']:.1f}% off "
            f"{window_days}d high={trailing_high['high_price']:.0f})"
            if trailing_high else
            f"Phase 4 → Phase 5 (conditions met: {','.join(met_names)})"
        )
    elif flipped and new_phase == 4:
        summary = "Phase 5 → Phase 4 reversion (new high + T3 dissipated)"
    elif new_phase == 5:
        summary = f"Phase 5 maintained (detected {new_detected_date})"
    elif any_met:
        summary = f"Phase 4 — conditions {','.join(met_names)} met today, pending anti-flicker"
    else:
        summary = (
            f"Phase 4 — no conditions met "
            f"(T3 fires={cond_a.detail.get('t3_fires', 0)}, "
            f"drawdown={trailing_high['drawdown_pct']:.1f}% off "
            f"{window_days}d high)"
            if trailing_high else
            f"Phase 4 — no conditions met (no price history)"
        )

    # Reversion eligibility (for downstream diagnostics in Phase 5 mode)
    reversion_eligible = False
    if prior_phase == 5:
        revert_check, _ = _evaluate_reversion(assessments, trailing_high, prior_phase_state, params)
        reversion_eligible = revert_check

    return PhaseDetectionResult(
        current_phase=new_phase,
        current_phase_label=label,
        flipped_this_run=flipped,
        pending_condition=new_pending_condition,
        pending_first_seen_iso=new_pending_first_seen,
        phase_5_detected_date_iso=new_detected_date,
        cycle_peak_price_at_detection=new_peak_price,
        condition_evals=condition_evals,
        reversion_eligible=reversion_eligible,
        summary_line=summary,
    )


def result_to_state_dict(
    result: PhaseDetectionResult,
    prior_state: Optional[dict] = None,
) -> dict:
    """Serialize a PhaseDetectionResult to the on-disk state dict shape.

    The state file shape (see src/state.py:save_phase_state):
      {
        "current_phase": int,
        "current_phase_label": str,
        "phase_5_detected_date_iso": str | null,
        "cycle_peak_price_at_detection": float | null,
        "anti_flicker_pending_condition": str | null,
        "anti_flicker_pending_first_seen_iso": str | null,
        "condition_a_first_seen_iso": str | null,
        "last_evaluated_iso": str,
        "last_summary_line": str,
      }
    """
    # Pull condition_a's first_seen out of its detail dict for persistence
    cond_a_first_seen = None
    for c in result.condition_evals:
        if c.name == "A":
            cond_a_first_seen = c.detail.get("first_seen_iso")
            break

    return {
        "current_phase": int(result.current_phase),
        "current_phase_label": result.current_phase_label,
        "phase_5_detected_date_iso": result.phase_5_detected_date_iso,
        "cycle_peak_price_at_detection": result.cycle_peak_price_at_detection,
        "anti_flicker_pending_condition": result.pending_condition,
        "anti_flicker_pending_first_seen_iso": result.pending_first_seen_iso,
        "condition_a_first_seen_iso": cond_a_first_seen,
        "last_evaluated_iso": datetime.utcnow().isoformat(),
        "last_summary_line": result.summary_line,
    }
