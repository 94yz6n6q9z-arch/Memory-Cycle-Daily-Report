"""
Priority alerter — real-time mobile push for high-magnitude Tier A broker actions.

Runs every 30 minutes during Korean + US market hours (separate workflow from
the daily brief). Uses the SAME weighted scoring system as the daily brief —
but only fires push notifications when:

  - Broker is Tier A (Mirae, Goldman, Morgan Stanley, JPMorgan, Mizuho)
  - Ticker is one of our 3 (SK Hynix, Micron, Samsung)
  - AND the action passes one of:
      • Single-note score <= -6.0 (significant bearish)
      • Single-note score >= +8.0 (significant bullish)
      • Double-notch rating change (two-rung move, e.g. Buy → Sell skipping Hold)
      • Target price change magnitude > 15%

Pushes via ntfy.sh — free, no signup, install the ntfy mobile app and subscribe
to your topic. Idempotency via state/priority_alerts_sent.json so the same action
is never alerted twice across the 30-min cron cycles.

Latency budget:
  - Korean broker publishes → Korean press echo → Naver structured ingestion: 30-60 min
  - US broker publishes → Finnhub structured ingestion: 5-30 min
  - Our cron: every 30 min during market hours
  - End-to-end: typically 60-90 min from broker publish to your phone buzzing
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests

from src.analysis.signal_scoring import (
    BrokerAction,
    actions_from_finnhub,
    actions_from_naver,
    load_running_state,
    append_new_actions,
    score_action,
)
from src.data.finnhub import fetch_finnhub_with_retry
from src.data.naver_finance import fetch_naver_with_retry
from src.state import STATE_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("memory_cycle_brief.priority_alerter")


# ============================================================
# Alert thresholds
# ============================================================
# These are sharper than the daily brief's thresholds — we only want to wake
# the user up for genuinely material events. Tweak as you observe false
# positives over the first 2-4 weeks.

ALERT_TICKERS = {"000660.KS", "MU", "005930.KS"}  # SK Hynix, Micron, Samsung
ALERT_TIERS = {"A"}                                # Tier A only — no broker 25
ALERT_THRESHOLD_BEARISH_SCORE = -6.0                # alert if score <= this
ALERT_THRESHOLD_BULLISH_SCORE = +8.0                # alert if score >= this
ALERT_PT_MAGNITUDE_PCT        = 15.0                # alert on any PT move > this (even if score doesn't hit)
ALERT_ALWAYS_DOUBLE_RATING    = True                # any double_upgrade / double_downgrade always alerts

PRIORITY_ALERTS_STATE_FILE = STATE_DIR / "priority_alerts_sent.json"


# ============================================================
# Idempotency (don't alert twice on the same action)
# ============================================================

def _action_key(action: BrokerAction) -> str:
    """Stable string key for deduplication."""
    return f"{action.action_date_iso}|{action.broker_canonical or action.broker_raw_name}|{action.ticker}|{action.score}"


def load_sent_keys() -> set[str]:
    if not PRIORITY_ALERTS_STATE_FILE.exists():
        return set()
    try:
        with open(PRIORITY_ALERTS_STATE_FILE) as f:
            data = json.load(f)
        return set(data.get("sent_keys", []))
    except Exception as e:
        logger.warning(f"Could not load sent keys: {e}")
        return set()


def save_sent_keys(keys: set[str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    # Prune keys older than 14 days (we never re-alert the same action,
    # and keeping years of keys serves no purpose)
    cutoff_iso = (date.today() - timedelta(days=14)).isoformat()
    pruned = {k for k in keys if k.split("|")[0] >= cutoff_iso}
    with open(PRIORITY_ALERTS_STATE_FILE, "w") as f:
        json.dump({
            "sent_keys": sorted(pruned),
            "last_updated_iso": datetime.utcnow().isoformat(),
        }, f, indent=2)


# ============================================================
# Filter: does this action warrant a phone notification?
# ============================================================

def is_priority_action(action: BrokerAction) -> tuple[bool, list[str]]:
    """Return (should_alert, reasons_list). Reasons explain why we're alerting."""
    if action.ticker not in ALERT_TICKERS:
        return False, []
    if action.tier not in ALERT_TIERS:
        return False, []

    reasons: list[str] = []

    # Score thresholds
    if action.score <= ALERT_THRESHOLD_BEARISH_SCORE:
        reasons.append(f"Score {action.score:+.1f} ≤ {ALERT_THRESHOLD_BEARISH_SCORE} (significant bearish)")
    if action.score >= ALERT_THRESHOLD_BULLISH_SCORE:
        reasons.append(f"Score {action.score:+.1f} ≥ {ALERT_THRESHOLD_BULLISH_SCORE} (significant bullish)")

    # Double-notch rating changes always alert
    if ALERT_ALWAYS_DOUBLE_RATING:
        if action.rating_change in ("double_downgrade", "double_upgrade"):
            reasons.append(f"Two-notch rating change ({action.rating_change})")

    # Big PT moves regardless of score
    if action.target_price_pct_change is not None and abs(action.target_price_pct_change) >= ALERT_PT_MAGNITUDE_PCT:
        reasons.append(f"PT change {action.target_price_pct_change:+.1f}% ≥ ±{ALERT_PT_MAGNITUDE_PCT}%")

    return bool(reasons), reasons


# ============================================================
# ntfy.sh push notification
# ============================================================

def _ticker_display(ticker: str) -> str:
    return {"000660.KS": "SK Hynix", "MU": "Micron", "005930.KS": "Samsung"}.get(ticker, ticker)


def send_push_notification(
    ntfy_topic_url: str,
    action: BrokerAction,
    reasons: list[str],
) -> bool:
    """Push to ntfy.sh. Returns True on success.

    ntfy.sh free service: anyone with the topic name can read these,
    so don't include credentials. The data is broker actions on public stocks —
    not sensitive. For privacy-conscious: self-host ntfy or use Pushover ($5).
    """
    broker = action.broker_canonical or action.broker_raw_name
    ticker_disp = _ticker_display(action.ticker)
    pt_str = f"{action.target_price_pct_change:+.1f}%" if action.target_price_pct_change is not None else "—"
    rating_str = action.rating_change_label or action.rating_change or ""

    # ntfy.sh uses HTTP headers for title/priority/tags
    is_bearish = action.score < 0
    emoji_tag = "rotating_light" if is_bearish else "rocket"
    priority = "urgent" if abs(action.score) >= 12 or "double" in (action.rating_change or "") else "high"

    title = f"🚨 {broker} — {ticker_disp} (Score {action.score:+.1f})"
    body_lines = [
        f"Action: {rating_str}" if rating_str else "Action: (see details)",
        f"PT change: {pt_str}",
        f"Source: {action.source}",
        f"Tier: A · {action.action_date_iso}",
        "",
        "Why alerting:",
    ]
    for r in reasons:
        body_lines.append(f"  • {r}")
    body = "\n".join(body_lines)

    try:
        resp = requests.post(
            ntfy_topic_url,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": emoji_tag,
                "Click": "https://finance.naver.com/item/main.naver?code=" + (action.ticker.split(".")[0] if ".KS" in action.ticker else action.ticker),
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"ntfy push sent: {title}")
        return True
    except Exception as e:
        logger.error(f"ntfy push failed for {title}: {e}")
        return False


# ============================================================
# Orchestrator
# ============================================================

def run_priority_alerter() -> int:
    """Main entry. Returns exit code."""
    ntfy_url = os.environ.get("NTFY_TOPIC_URL", "")
    if not ntfy_url:
        logger.error("NTFY_TOPIC_URL not set in env — cannot push alerts")
        return 1

    started_at = datetime.utcnow()
    logger.info(f"=== PRIORITY ALERTER START — {started_at.isoformat()} ===")

    # ----- Fetch fast sources only (no Quartr, no PDFs, no LLM) -----
    logger.info("Fetching Naver Finance...")
    naver_data, _ = fetch_naver_with_retry()

    logger.info("Fetching Finnhub...")
    finnhub_data, _ = fetch_finnhub_with_retry()

    if not (naver_data or finnhub_data):
        logger.warning("Both Naver and Finnhub failed; nothing to alert on")
        return 0

    # ----- Convert to scored BrokerAction list, append to running state -----
    state = load_running_state()
    new_actions: list[BrokerAction] = []
    if naver_data:
        new_actions.extend(actions_from_naver(naver_data, state))
    if finnhub_data:
        new_actions.extend(actions_from_finnhub(finnhub_data))

    state = append_new_actions(new_actions)
    logger.info(f"Total state actions: {len(state.actions)}; new this run: {len(new_actions)}")

    # ----- Filter to priority alerts -----
    sent_keys = load_sent_keys()
    push_count = 0
    skipped_dedupe = 0
    for action in new_actions:
        should, reasons = is_priority_action(action)
        if not should:
            continue
        key = _action_key(action)
        if key in sent_keys:
            skipped_dedupe += 1
            continue
        success = send_push_notification(ntfy_url, action, reasons)
        if success:
            sent_keys.add(key)
            push_count += 1

    save_sent_keys(sent_keys)

    duration = (datetime.utcnow() - started_at).total_seconds()
    logger.info(
        f"=== PRIORITY ALERTER COMPLETE in {duration:.0f}s "
        f"({push_count} push, {skipped_dedupe} skipped dedupe, {len(new_actions)} processed) ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_priority_alerter())
