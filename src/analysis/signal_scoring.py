"""
Weighted broker scoring for Indicator 3 — the canonical Tier 2 EPS revisions signal.

Operationalizes the framework laid out in config.py:
  BROKER_AUTHORITY_TIERS, TIER_MULTIPLIERS, RATING_CHANGE_POINTS,
  _magnitude_points(), EPS_SCORING_THRESHOLDS, EPS_REVISIONS_TICKERS.

Inputs to the scoring engine come from three streams:
  1. Naver Finance scraper output (Korean + US brokers covering KRX tickers)
  2. Finnhub API (US brokers covering US-listed tickers: MU primarily)
  3. Korean broker PDF digests (Mirae/NH/Samsung Securities — adds FY27 EPS context)

For each detected broker action, we compute a score and append to a rolling
window. The rolling 7d and 30d sums drive the trigger logic.

State is persisted to state/eps_revisions_running.json so we have trajectory
across days, not just a single snapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.config import (
    BROKER_AUTHORITY_TIERS,
    EPS_REVISIONS_TICKERS,
    EPS_SCORING_THRESHOLDS,
    RATING_CHANGE_POINTS,
    TIER_MULTIPLIERS,
    _magnitude_points,
)
from src.state import STATE_DIR

logger = logging.getLogger("memory_cycle_brief.signal_scoring")


EPS_RUNNING_STATE_FILE = STATE_DIR / "eps_revisions_running.json"


# ============================================================
# Schema
# ============================================================

@dataclass
class BrokerAction:
    """One broker action on one ticker, scored.

    All fields optional except date/broker/ticker — different sources
    expose different subsets. Naver gives PT + rating; Korean broker PDFs
    add FY27 EPS revision; Finnhub gives PT + rating for US tickers.
    """
    action_date_iso: str          # YYYY-MM-DD
    broker_raw_name: str          # as captured from source (Korean or English)
    broker_canonical: Optional[str]   # mapped English name
    tier: str                     # "A" | "B" | "C"
    ticker: str
    rating_change: Optional[str]      # initiate_buy / upgrade / downgrade / maintained / None
    rating_change_label: Optional[str]  # human-readable "Buy → Hold" or "Mr ₩1,280K → ₩1,370K"
    target_price_pct_change: Optional[float]   # signed %, e.g. -19.6
    target_price_prior: Optional[float]
    target_price_new: Optional[float]
    fy27_eps_pct_change: Optional[float]       # signed %
    score: float                              # final weighted score
    score_breakdown: dict[str, Any]           # transparent breakdown for the brief
    source: str                              # "Naver" | "Finnhub" | "Mirae PDF" | etc.
    note_excerpt: Optional[str] = None        # short verbatim quote (Layer 1 digest)
    # v3.5.0 — event deduplication fields
    event_type: str = "broker_action"         # "broker_action" | "earnings_coverage"
    event_key: Optional[str] = None           # set for earnings_coverage actions

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EpsRevisionsState:
    """Rolling state for t2_eps_revisions signal."""
    actions: list[BrokerAction] = field(default_factory=list)
    last_updated_iso: str = ""

    def to_dict(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "last_updated_iso": self.last_updated_iso,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EpsRevisionsState":
        actions = [BrokerAction(**a) for a in d.get("actions", [])]
        return cls(actions=actions, last_updated_iso=d.get("last_updated_iso", ""))


# ============================================================
# Scoring engine
# ============================================================

def canonicalize_broker_name(raw: str) -> tuple[Optional[str], str]:
    """Map a raw broker name (Korean characters or English variant) to canonical English + tier.

    Returns (canonical_english_name_or_None, tier_letter).
    Tier defaults to "C" for unknown brokers — that's correct, they get baseline weight.
    """
    if not raw:
        return None, "C"
    cleaned = raw.strip()
    # Direct hit
    if cleaned in BROKER_AUTHORITY_TIERS:
        tier = BROKER_AUTHORITY_TIERS[cleaned]
        # Try to find the English version for display
        if any(c >= chr(0xAC00) for c in cleaned):  # Korean
            # Find a tier-matching English entry
            for k, v in BROKER_AUTHORITY_TIERS.items():
                if v == tier and not any(c >= chr(0xAC00) for c in k):
                    return k, tier
            return cleaned, tier
        return cleaned, tier
    # Try fuzzy match on lowercased
    lc = cleaned.lower()
    for k, v in BROKER_AUTHORITY_TIERS.items():
        if k.lower() == lc:
            return k, v
    return cleaned, "C"


def score_action(
    *,
    action_date_iso: str,
    broker_raw_name: str,
    ticker: str,
    rating_change: Optional[str] = None,
    target_price_pct_change: Optional[float] = None,
    target_price_prior: Optional[float] = None,
    target_price_new: Optional[float] = None,
    fy27_eps_pct_change: Optional[float] = None,
    source: str = "unknown",
    rating_change_label: Optional[str] = None,
    note_excerpt: Optional[str] = None,
) -> BrokerAction:
    """Compute the weighted score for a single broker action.

    Returns a BrokerAction with .score and .score_breakdown populated.
    The breakdown is structured for transparent display in the brief.
    """
    canonical, tier = canonicalize_broker_name(broker_raw_name)
    tier_mult = TIER_MULTIPLIERS.get(tier, 1.0)

    # Component points
    rating_points = RATING_CHANGE_POINTS.get(rating_change or "maintained", 0.0)
    pt_points = 0.0
    eps_points = 0.0
    if target_price_pct_change is not None:
        direction = 1 if target_price_pct_change > 0 else -1 if target_price_pct_change < 0 else 0
        pt_points = direction * _magnitude_points(target_price_pct_change)
    if fy27_eps_pct_change is not None:
        direction = 1 if fy27_eps_pct_change > 0 else -1 if fy27_eps_pct_change < 0 else 0
        eps_points = direction * _magnitude_points(fy27_eps_pct_change)

    # Combination bonus — when multiple action types move same direction
    components_present = sum([
        1 if (rating_change and rating_change != "maintained") else 0,
        1 if (target_price_pct_change is not None and abs(target_price_pct_change) >= 5) else 0,
        1 if (fy27_eps_pct_change is not None and abs(fy27_eps_pct_change) >= 5) else 0,
    ])
    # Check directional alignment for combo bonus
    directions = [
        rating_points and (1 if rating_points > 0 else -1),
        pt_points and (1 if pt_points > 0 else -1),
        eps_points and (1 if eps_points > 0 else -1),
    ]
    directions = [d for d in directions if d != 0]
    aligned = len(set(directions)) == 1 if directions else False

    combo_multiplier = 1.0
    if aligned and components_present >= 2:
        combo_multiplier += 0.5  # 2 aligned components = +50%
    if aligned and components_present >= 3:
        combo_multiplier += 0.5  # 3 aligned = additional +50% (total ×2.0)

    # Final score
    raw_sum = rating_points + pt_points + eps_points
    final_score = raw_sum * tier_mult * combo_multiplier

    breakdown = {
        "broker_canonical": canonical,
        "tier": tier,
        "tier_multiplier": tier_mult,
        "rating_points": round(rating_points, 2),
        "pt_points": round(pt_points, 2),
        "eps_points": round(eps_points, 2),
        "components_present": components_present,
        "aligned": aligned,
        "combo_multiplier": combo_multiplier,
        "raw_sum_before_weighting": round(raw_sum, 2),
        "final_score": round(final_score, 2),
    }
    return BrokerAction(
        action_date_iso=action_date_iso,
        broker_raw_name=broker_raw_name,
        broker_canonical=canonical,
        tier=tier,
        ticker=ticker,
        rating_change=rating_change,
        rating_change_label=rating_change_label,
        target_price_pct_change=target_price_pct_change,
        target_price_prior=target_price_prior,
        target_price_new=target_price_new,
        fy27_eps_pct_change=fy27_eps_pct_change,
        score=round(final_score, 2),
        score_breakdown=breakdown,
        source=source,
        note_excerpt=note_excerpt,
    )


# ============================================================
# Rolling state persistence
# ============================================================

def load_running_state() -> EpsRevisionsState:
    if not EPS_RUNNING_STATE_FILE.exists():
        return EpsRevisionsState()
    try:
        with open(EPS_RUNNING_STATE_FILE) as f:
            return EpsRevisionsState.from_dict(json.load(f))
    except Exception as e:
        logger.warning(f"Could not load running EPS state: {e}")
        return EpsRevisionsState()


def save_running_state(state: EpsRevisionsState) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    state.last_updated_iso = datetime.utcnow().isoformat()
    # Prune actions older than 60 days — we only need rolling 30d window + small history
    cutoff_iso = (date.today() - timedelta(days=60)).isoformat()
    state.actions = [a for a in state.actions if a.action_date_iso >= cutoff_iso]
    with open(EPS_RUNNING_STATE_FILE, "w") as f:
        json.dump(state.to_dict(), f, indent=2, default=str)


def append_new_actions(new_actions: list[BrokerAction]) -> EpsRevisionsState:
    """Merge new actions into running state, deduplicating on (date, broker, ticker)."""
    state = load_running_state()
    existing_keys = {
        (a.action_date_iso, a.broker_canonical or a.broker_raw_name, a.ticker)
        for a in state.actions
    }
    for a in new_actions:
        key = (a.action_date_iso, a.broker_canonical or a.broker_raw_name, a.ticker)
        if key not in existing_keys:
            state.actions.append(a)
            existing_keys.add(key)
    state.actions.sort(key=lambda x: x.action_date_iso)
    save_running_state(state)
    return state


# ============================================================
# Rolling windows + trigger evaluation
# ============================================================

def compute_rolling_score(
    state: EpsRevisionsState,
    window_days: int,
    ticker: Optional[str] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Compute the rolling sums (bearish, bullish, net) over the last `window_days`.

    Bearish and bullish are tracked SEPARATELY so the firing logic uses bearish-only
    (a recent -20 cluster doesn't get masked by older bullish actions in the same
    30-day window). The net score is preserved for context / convergence assessment.

    Returns:
      - score_bearish: sum of all negative-score actions (≤ 0)
      - score_bullish: sum of all positive-score actions (≥ 0)
      - score_net:     net (bearish + bullish)
      - action_count:  total actions in window
      - tier_breakdown: dict tier → {bearish, bullish}
      - top_impact_actions: top 5 by absolute score
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=window_days)).isoformat()
    relevant = [
        a for a in state.actions
        if a.action_date_iso >= cutoff
        and (ticker is None or a.ticker == ticker)
    ]
    score_bearish = sum(a.score for a in relevant if a.score < 0)
    score_bullish = sum(a.score for a in relevant if a.score > 0)
    score_net = score_bearish + score_bullish

    tier_breakdown = {"A": {"bearish": 0.0, "bullish": 0.0},
                      "B": {"bearish": 0.0, "bullish": 0.0},
                      "C": {"bearish": 0.0, "bullish": 0.0}}
    for a in relevant:
        bucket = "bearish" if a.score < 0 else "bullish"
        tier_breakdown.setdefault(a.tier, {"bearish": 0.0, "bullish": 0.0})[bucket] += a.score

    # Round for clean display
    for tier_data in tier_breakdown.values():
        tier_data["bearish"] = round(tier_data["bearish"], 2)
        tier_data["bullish"] = round(tier_data["bullish"], 2)

    top_impact = sorted(relevant, key=lambda a: -abs(a.score))[:5]
    return {
        "window_days": window_days,
        "ticker": ticker,
        "score_bearish": round(score_bearish, 2),
        "score_bullish": round(score_bullish, 2),
        "score_net": round(score_net, 2),
        "action_count": len(relevant),
        "tier_breakdown": tier_breakdown,
        "top_impact_actions": [a.to_dict() for a in top_impact],
    }


def evaluate_trigger(state: EpsRevisionsState, today: Optional[date] = None) -> dict[str, Any]:
    """Evaluate t2_eps_revisions trigger conditions across all monitored tickers.

    Fires on BEARISH-only score (sharper than net — a recent -20 cluster doesn't
    get masked by older bullish actions in the same window).

    Returns a structured verdict for Layer 2 / Layer 3 consumption:
      - per-ticker rolling 7d and 30d scores (bearish/bullish/net)
      - per-ticker fired_status verdict (based on bearish-only)
      - aggregate signal status across all tickers
    """
    today = today or date.today()
    per_ticker: dict[str, dict[str, Any]] = {}
    for ticker in EPS_REVISIONS_TICKERS:
        roll_7d  = compute_rolling_score(state, 7, ticker=ticker, today=today)
        roll_30d = compute_rolling_score(state, 30, ticker=ticker, today=today)
        # Single-note flags (today's actions)
        recent = [
            a for a in state.actions
            if a.action_date_iso >= (today - timedelta(days=1)).isoformat()
            and a.ticker == ticker
        ]
        single_note_flags = [
            a for a in recent
            if abs(a.score) > EPS_SCORING_THRESHOLDS["single_action_flag_score"]
            or (a.tier == "A" and abs(a.score) > EPS_SCORING_THRESHOLDS["tier_a_single_flag_score"])
        ]
        # Determine fired status — uses BEARISH-only scores against negative thresholds
        # and BULLISH-only against positive bull threshold
        if roll_30d["score_bearish"] < EPS_SCORING_THRESHOLDS["rolling_30d_fire_threshold"]:
            fired_status = "FIRED"
        elif roll_7d["score_bearish"] < EPS_SCORING_THRESHOLDS["rolling_7d_early_warning"]:
            fired_status = "EARLY_WARNING"
        elif roll_30d["score_bullish"] > EPS_SCORING_THRESHOLDS["rolling_30d_bull_threshold"]:
            fired_status = "BULLISH_CONFIRM"
        else:
            fired_status = "Not fired"
        per_ticker[ticker] = {
            "rolling_7d": roll_7d,
            "rolling_30d": roll_30d,
            "single_note_flags_today": [a.to_dict() for a in single_note_flags],
            "fired_status": fired_status,
        }

    # Aggregate verdict: most-severe ticker drives overall status
    severity_order = ["FIRED", "EARLY_WARNING", "Not fired", "BULLISH_CONFIRM"]
    overall_severity = "Not fired"
    for ticker_data in per_ticker.values():
        s = ticker_data["fired_status"]
        if severity_order.index(s) < severity_order.index(overall_severity):
            overall_severity = s
    return {
        "evaluated_at_iso": datetime.utcnow().isoformat(),
        "per_ticker": per_ticker,
        "overall_status": overall_severity,
    }


# ============================================================
# Ingestion adapters — translate Naver / Finnhub / Korean broker
# digest payloads into BrokerAction objects.
# ============================================================

def actions_from_naver(naver_payload: dict, prior_state: EpsRevisionsState) -> list[BrokerAction]:
    """Convert Naver scraper output into scored BrokerAction list.

    Naver gives target price + rating + date. NO FY27 EPS (that's in PDF body).
    Target price % change is computed against the broker's prior entry in
    running state (so first time we see a broker, no PT change is recorded —
    they appear in the panel but contribute 0 PT points until next revision).
    """
    new_actions: list[BrokerAction] = []
    if not naver_payload:
        return new_actions

    # Build lookup of broker's last target on this ticker for PT % computation
    prior_targets: dict[tuple[str, str], float] = {}
    for a in sorted(prior_state.actions, key=lambda x: x.action_date_iso):
        if a.target_price_new and a.broker_canonical:
            prior_targets[(a.broker_canonical, a.ticker)] = a.target_price_new

    tickers_data = naver_payload.get("tickers", {})
    for ticker_name, ticker_data in tickers_data.items():
        # Map display name to ticker code we use internally
        code = ticker_data.get("code")
        ticker_internal = f"{code}.KS" if code else ticker_name
        for action in ticker_data.get("actions", []):
            canonical, tier = canonicalize_broker_name(action.get("broker_korean", ""))
            pt_new = action.get("target_price_krw")
            published_iso = action.get("published_date_iso", date.today().isoformat())

            pt_prior = prior_targets.get((canonical, ticker_internal)) if canonical else None
            pt_pct = None
            if pt_new and pt_prior and pt_prior > 0:
                pt_pct = round((pt_new - pt_prior) / pt_prior * 100, 2)

            # Map rating
            rating_change = None
            rating_label = None
            rating_eng = action.get("rating_english", "")
            if rating_eng:
                # Without prior rating we can't classify upgrade/downgrade — treat as "maintained"
                # unless this is the broker's first appearance (initiation)
                rating_change = "maintained"
                rating_label = rating_eng

            scored = score_action(
                action_date_iso=published_iso,
                broker_raw_name=action.get("broker_korean", "") or action.get("broker_english", "") or "Unknown",
                ticker=ticker_internal,
                rating_change=rating_change,
                target_price_pct_change=pt_pct,
                target_price_prior=pt_prior,
                target_price_new=pt_new,
                fy27_eps_pct_change=None,
                source="Naver Finance",
                rating_change_label=f"PT ₩{pt_prior:,.0f} → ₩{pt_new:,.0f}" if (pt_prior and pt_new) else (rating_label or "Initiated"),
            )
            new_actions.append(scored)
    return new_actions


def actions_from_finnhub(finnhub_payload: dict) -> list[BrokerAction]:
    """Convert Finnhub recommendation_trends + price_target into BrokerAction list.

    Finnhub aggregates analyst ratings monthly. We compute MoM direction
    (more Buy this month vs last month) and emit ONE consolidated action
    per ticker representing the net change.
    """
    new_actions: list[BrokerAction] = []
    if not finnhub_payload:
        return new_actions

    today_iso = date.today().isoformat()
    for ticker, ticker_data in finnhub_payload.get("tickers", {}).items():
        direction = ticker_data.get("recent_direction")
        if not direction or direction == "flat":
            continue
        rating_change = "upgrade" if direction == "up" else "downgrade"
        pt_data = ticker_data.get("price_target", {}) or {}
        # Finnhub gives mean/high/low PT — we don't have prior snapshot to diff easily,
        # so this action carries only the rating direction signal, not PT magnitude.
        scored = score_action(
            action_date_iso=today_iso,
            broker_raw_name="Finnhub consensus aggregate",
            ticker=ticker,
            rating_change=rating_change,
            target_price_pct_change=None,
            target_price_prior=None,
            target_price_new=pt_data.get("targetMean"),
            fy27_eps_pct_change=None,
            source="Finnhub",
            rating_change_label=f"Consensus net direction: {direction}",
        )
        # Override tier — this is a consensus aggregate, not a single broker; treat as Tier C
        scored.tier = "C"
        scored.score_breakdown["tier"] = "C"
        scored.score_breakdown["note"] = "Consensus aggregate (Finnhub), not single broker"
        new_actions.append(scored)
    return new_actions


def actions_from_broker_pdf_digests(digests: list[dict]) -> list[BrokerAction]:
    """Convert Layer 1 digests of Korean broker PDFs into BrokerAction list.

    These digests include the FY27 EPS revision and target price extracted
    by Haiku from the PDF body. Higher signal than Naver since we get EPS too.
    """
    new_actions: list[BrokerAction] = []
    for digest in digests:
        if digest.get("source_type") != "korean_broker_pdf":
            continue
        tpa = digest.get("target_price_action")
        if not tpa:
            continue
        broker = tpa.get("broker") or digest.get("source_id", "").split("/")[0].strip()
        ticker = tpa.get("ticker", "")
        if not ticker:
            # Try to infer ticker from title
            title = digest.get("title", "").lower()
            if "sk hynix" in title or "skhynix" in title:
                ticker = "000660.KS"
            elif "samsung" in title:
                ticker = "005930.KS"
            elif "micron" in title:
                ticker = "MU"
            else:
                continue
        published_iso = digest.get("published_date_iso", date.today().isoformat())
        new_pt = tpa.get("new_pt")
        prior_pt = tpa.get("prior_pt")
        pt_pct = None
        if new_pt and prior_pt and prior_pt > 0:
            pt_pct = round((new_pt - prior_pt) / prior_pt * 100, 2)

        # Rating direction
        direction = tpa.get("direction", "maintained")
        rating_change_map = {
            "raised": "upgrade",
            "lowered": "downgrade",
            "maintained": "maintained",
            "initiated": "initiate_buy",  # assume buy on initiate unless we have signal otherwise
        }
        rating_change = rating_change_map.get(direction, "maintained")

        # FY27 EPS — pulled from key_metrics by Haiku
        fy27_eps_pct = None
        km = digest.get("key_metrics", {})
        for k in ("fy27_eps_revision_pct", "fy27_eps_change_pct", "fy27_eps_pct_change"):
            v = km.get(k)
            if v is not None:
                try:
                    fy27_eps_pct = float(v)
                    break
                except (ValueError, TypeError):
                    pass

        excerpt = (digest.get("key_quotes") or [None])[0]

        # v3.5.0 — detect earnings-related digests so ingest_and_evaluate()
        # can deduplicate coverage echoes via the event log.
        # A PDF digest is earnings-related when it carries explicit EPS revision
        # or revenue beat/miss fields — meaning the broker was responding to a
        # specific earnings event, not issuing an independent sector note.
        _is_earnings = any(
            km.get(k) is not None
            for k in ("fy27_eps_revision_pct", "fy27_eps_change_pct", "fy27_eps_pct_change",
                      "fy27_eps_new", "eps_beat_miss_direction", "eps_surprise_pct")
        )

        scored = score_action(
            action_date_iso=published_iso,
            broker_raw_name=broker,
            ticker=ticker,
            rating_change=rating_change,
            target_price_pct_change=pt_pct,
            target_price_prior=prior_pt,
            target_price_new=new_pt,
            fy27_eps_pct_change=fy27_eps_pct,
            source=f"{broker} PDF",
            rating_change_label=f"{direction.capitalize()} {('₩' + format(prior_pt, ',.0f') + ' → ₩' + format(new_pt, ',.0f')) if (prior_pt and new_pt) else ''}".strip(),
            note_excerpt=excerpt,
        )
        # v3.5.0 — tag event type so ingest_and_evaluate() can deduplicate
        # earnings-period coverage echoes via the event log.
        if _is_earnings:
            from src.state import event_key_for_earnings
            scored.event_type = "earnings_coverage"
            scored.event_key = event_key_for_earnings(ticker, published_iso)
        new_actions.append(scored)
    return new_actions


# ============================================================
# Top-level entry point for fast tier
# ============================================================

def ingest_and_evaluate(
    naver_payload: Optional[dict],
    finnhub_payload: Optional[dict],
    broker_pdf_digests: list[dict],
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Ingest all sources, update running state, evaluate triggers.

    v3.5.0 — event deduplication layer added BEFORE append_new_actions().
    Earnings-coverage actions (tagged by actions_from_broker_pdf_digests)
    are checked against the event log:
      - First observation → score it normally + record in log as primary
      - Subsequent echoes → record as observation only; DO NOT re-score
    This prevents a single earnings event covered by 8-10 sources from
    compounding into a false t2_eps_revisions fire.

    Divergence detection: if a later echo contradicts the primary verdict
    (bearish echo after bullish primary or vice versa), divergence_flagged
    is set in the log and surfaced in the verdict for template rendering.

    Returns the dict that Layer 2's t2_eps_revisions reasoner will see.
    """
    from src.state import (
        load_event_log, save_event_log,
        is_known_event, record_event_primary, record_event_observation,
        get_divergent_events,
    )

    running_state = load_running_state()
    new_actions: list[BrokerAction] = []

    if naver_payload:
        new_actions.extend(actions_from_naver(naver_payload, running_state))
    if finnhub_payload:
        new_actions.extend(actions_from_finnhub(finnhub_payload))
    if broker_pdf_digests:
        new_actions.extend(actions_from_broker_pdf_digests(broker_pdf_digests))

    # ── v3.5.0 event deduplication ───────────────────────────────────────────
    event_log = load_event_log()
    actions_to_score: list[BrokerAction] = []
    echoes_suppressed: int = 0
    divergences_found: int = 0

    for action in new_actions:
        if action.event_type != "earnings_coverage" or not action.event_key:
            # Not an earnings-coverage action — pass through unchanged
            actions_to_score.append(action)
            continue

        ekey = action.event_key
        score_direction = "bearish" if action.score < 0 else ("bullish" if action.score > 0 else "neutral")

        if not is_known_event(ekey, event_log):
            # First observation for this event — score it and record as primary
            actions_to_score.append(action)
            verdict_label = f"{score_direction} coverage of {action.ticker} earnings"
            record_event_primary(
                event_key=ekey,
                source=action.source,
                primary_verdict=score_direction,
                primary_score=action.score,
                event_date_iso=action.action_date_iso,
                log=event_log,
            )
            logger.info(
                f"EventDedup: new event {ekey} — first observation scored "
                f"(source={action.source}, score={action.score:+.1f})"
            )
        else:
            # Echo — suppress from scoring, record observation, check divergence
            summary = (action.note_excerpt or action.rating_change_label or
                       f"score={action.score:+.1f}")
            diverged = record_event_observation(
                event_key=ekey,
                source=action.source,
                summary=summary[:120],
                score_direction=score_direction,
                log=event_log,
            )
            echoes_suppressed += 1
            if diverged:
                divergences_found += 1
            logger.info(
                f"EventDedup: echo suppressed for {ekey} "
                f"(source={action.source}, score={action.score:+.1f})"
                + (" ← DIVERGENCE" if diverged else "")
            )

    try:
        save_event_log(event_log)
    except Exception as _el_err:
        logger.warning(f"EventDedup: could not save event_log.json: {_el_err}")

    # ── Score only de-duplicated actions ─────────────────────────────────────
    running_state = append_new_actions(actions_to_score)
    verdict = evaluate_trigger(running_state, today=today)

    today_iso = (today or date.today()).isoformat()
    verdict["new_actions_today"] = [
        a.to_dict() for a in actions_to_score
        if a.action_date_iso == today_iso
    ]
    verdict["total_actions_in_state"] = len(running_state.actions)
    verdict["dedup_stats"] = {
        "total_new_actions": len(new_actions),
        "actions_scored": len(actions_to_score),
        "echoes_suppressed": echoes_suppressed,
        "divergences_found": divergences_found,
    }
    verdict["divergent_events"] = get_divergent_events(event_log, days=30)
    return verdict

