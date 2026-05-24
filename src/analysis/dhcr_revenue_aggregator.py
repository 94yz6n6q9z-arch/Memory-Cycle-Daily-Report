"""DHCR Enterprise AI Revenue — Layer 1 extraction aggregator (v3.4.5).

Reads `enterprise_ai_quarterly_revenue` extractions from the daily extraction
log and appends to `state/enterprise_ai_revenue_state.json` under the
correct category (microsoft_azure_ai / aws_ai_services / google_cloud_ai /
oracle_ai_infra / nvidia_datacenter).

Dedup by (category, quarter). Most recent / highest-tier source wins on
collision. Safe-failing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/enterprise_ai_revenue.yaml")
STATE_FILE = Path("state/enterprise_ai_revenue_state.json")
EXTRACTION_LOG_DIR = Path("state")
EXTRACTION_LOG_GLOB = "extraction_log_*.jsonl"

TIER_A_SOURCES = {"msft_earnings", "amzn_earnings", "googl_earnings", "orcl_earnings",
                  "nvda_earnings", "msft", "amzn", "googl", "orcl", "nvda",
                  "microsoft_earnings", "google_earnings", "oracle_earnings",
                  "amazon_earnings", "nvidia_earnings", "sec_10q", "sec_10k"}
TIER_B_SOURCES = {"mirae_asset", "kb_securities", "semianalysis"}

CATEGORIES = ("microsoft_azure_ai", "aws_ai_services", "google_cloud_ai",
              "oracle_ai_infra", "nvidia_datacenter")


def _source_tier(source_str: str) -> int:
    s = (source_str or "").lower()
    if any(t in s for t in TIER_A_SOURCES):
        return 1
    if any(t in s for t in TIER_B_SOURCES):
        return 2
    return 3


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
        except Exception as ex:
            logger.warning(f"Could not read extraction log {path}: {ex}")
    return entries


def _load_state() -> dict:
    if not STATE_FILE.exists():
        state = {"_metadata": {}}
        for cat in CATEGORIES:
            state[cat] = []
        return state
    with open(STATE_FILE) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def aggregate_revenue_extractions(lookback_days: Optional[int] = None) -> dict:
    """Append new quarterly revenue points to state/enterprise_ai_revenue_state.json."""
    try:
        with open(THRESHOLDS_FILE) as f:
            thresholds = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("enterprise_ai_revenue.yaml missing — revenue aggregator skipped")
        return {}

    if lookback_days is None:
        lookback_days = thresholds.get("aggregator_config", {}).get("extraction_log_lookback_days", 120)

    state = _load_state()
    additions = 0
    updates = 0

    # Existing (category, quarter) → existing entry pointer
    existing: dict[tuple, dict] = {}
    for cat in CATEGORIES:
        for entry in state.get(cat, []) or []:
            existing[(cat, entry.get("quarter"))] = entry

    try:
        entries = _iter_extraction_log_entries(lookback_days)
        # Group by (category, quarter); keep highest-tier source
        by_key: dict[tuple, tuple[int, dict, dict]] = {}
        for e in entries:
            # v3.5.1 — extraction_log writer puts arch signals under "signals" key (see digester._log_architecture_signals);
            # the "architecture_signals" fallback covers any legacy entries.
            arch_sigs = e.get("signals") or e.get("architecture_signals") or {}
            rev = arch_sigs.get("enterprise_ai_quarterly_revenue")
            if not rev or not isinstance(rev, dict):
                continue
            cat = rev.get("category")
            quarter = rev.get("quarter")
            if cat not in CATEGORIES or not quarter:
                continue
            key = (cat, quarter)
            tier = _source_tier(e.get("source", ""))
            prior = by_key.get(key)
            if prior is None or tier < prior[0]:
                by_key[key] = (tier, rev, e)

        for (cat, quarter), (tier, rev, source_entry) in by_key.items():
            if (cat, quarter) in existing:
                existing[(cat, quarter)].update({
                    "revenue_usd_b": rev.get("revenue_usd_b", existing[(cat, quarter)].get("revenue_usd_b")),
                    "qoq_growth_pct": rev.get("qoq_growth_pct", existing[(cat, quarter)].get("qoq_growth_pct")),
                    "yoy_growth_pct": rev.get("yoy_growth_pct", existing[(cat, quarter)].get("yoy_growth_pct")),
                    "source_quote": rev.get("source_quote") or existing[(cat, quarter)].get("source_quote"),
                })
                updates += 1
                logger.info(f"  revenue catalog: updated {cat}/{quarter} (tier {tier})")
            else:
                new_entry = {
                    "quarter": quarter,
                    "revenue_usd_b": rev.get("revenue_usd_b"),
                    "qoq_growth_pct": rev.get("qoq_growth_pct"),
                    "yoy_growth_pct": rev.get("yoy_growth_pct"),
                    "source_quote": rev.get("source_quote", ""),
                    "source_id": source_entry.get("source_id", "auto"),
                }
                state.setdefault(cat, []).append(new_entry)
                additions += 1
                logger.info(f"  revenue catalog: added {cat}/{quarter}")

        # Sort each category by quarter for consistent downstream consumption
        for cat in CATEGORIES:
            state[cat] = sorted(state.get(cat, []) or [], key=lambda e: e.get("quarter", ""))

        state["_metadata"]["last_aggregator_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["_metadata"]["last_run_additions"] = additions
        state["_metadata"]["last_run_updates"] = updates
        _save_state(state)
        logger.info(
            f"DHCR revenue aggregator: {additions} new + {updates} updated; "
            f"{len(entries)} extraction entries scanned."
        )
    except Exception as e:
        logger.error(f"DHCR revenue aggregator failed (state preserved): {e}", exc_info=True)

    return state
