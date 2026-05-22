"""DHCR Accelerator Catalog — Layer 1 extraction aggregator (v3.4.5).

Reads `new_accelerator_announcement` extractions from the daily extraction
log and appends to `state/hbm_per_accelerator_state.json` under the correct
vendor section (nvidia / amd / custom_silicon / non_hbm_accelerators).

Dedup by (name, vendor). Safe-failing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/hbm_per_accelerator.yaml")
STATE_FILE = Path("state/hbm_per_accelerator_state.json")
EXTRACTION_LOG_DIR = Path("state")
EXTRACTION_LOG_GLOB = "extraction_log_*.jsonl"

TIER_A_SOURCES = {"nvidia", "amd", "gtc", "ai_day", "google", "aws", "microsoft", "ignite", "re_invent"}
TIER_B_SOURCES = {"semianalysis", "toms_hardware", "anandtech"}


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
                ts_str = e.get("date") or e.get("extracted_at") or e.get("timestamp")
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
        return {
            "_metadata": {},
            "nvidia": [], "amd": [], "custom_silicon": [], "non_hbm_accelerators": [],
        }
    with open(STATE_FILE) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def aggregate_accelerator_extractions(lookback_days: Optional[int] = None) -> dict:
    """Append newly-disclosed accelerators to state/hbm_per_accelerator_state.json."""
    try:
        with open(THRESHOLDS_FILE) as f:
            thresholds = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("hbm_per_accelerator.yaml missing — accelerator aggregator skipped")
        return {}

    if lookback_days is None:
        lookback_days = thresholds.get("aggregator_config", {}).get("extraction_log_lookback_days", 180)

    state = _load_state()
    additions = 0

    # Collect existing keys across all vendor sections
    existing_keys: set[tuple] = set()
    for section in ("nvidia", "amd", "custom_silicon", "non_hbm_accelerators"):
        for entry in state.get(section, []) or []:
            existing_keys.add((entry.get("name"), entry.get("vendor", section)))

    try:
        entries = _iter_extraction_log_entries(lookback_days)
        by_key: dict[tuple, tuple[int, dict, dict]] = {}
        for e in entries:
            arch_sigs = e.get("architecture_signals") or {}
            announcement = arch_sigs.get("new_accelerator_announcement")
            if not announcement or not isinstance(announcement, dict):
                continue
            name = announcement.get("name")
            vendor = announcement.get("vendor", "").lower()
            if not name or not vendor:
                continue
            key = (name, vendor)
            if (name, vendor) in existing_keys or (name, announcement.get("category")) in existing_keys:
                continue
            tier = _source_tier(e.get("source", ""))
            prior = by_key.get(key)
            if prior is None or tier < prior[0]:
                by_key[key] = (tier, announcement, e)

        for (name, vendor), (tier, ann, source_entry) in by_key.items():
            category = ann.get("category") or _category_from_vendor(vendor)
            new_entry = {
                "name": name,
                "vendor": vendor,
                "hbm_gb": ann.get("hbm_gb"),
                "release_date": ann.get("release_date"),
                "generation": ann.get("generation", ""),
                "notes": ann.get("notes") or f"Auto-added by dhcr_accelerator_aggregator from {source_entry.get('source_id', 'unknown')} (Tier {['A','B','C'][min(tier-1, 2)]}).",
            }
            section = state.setdefault(category, [])
            section.append(new_entry)
            additions += 1
            logger.info(f"  accelerator catalog: added {vendor}/{name} ({ann.get('release_date')}) → {category}")

        state["_metadata"]["last_aggregator_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["_metadata"]["last_run_additions"] = additions
        _save_state(state)
        logger.info(f"DHCR accelerator aggregator: {additions} new accelerator(s); {len(entries)} extraction entries scanned.")
    except Exception as e:
        logger.error(f"DHCR accelerator aggregator failed (state preserved): {e}", exc_info=True)

    return state


def _category_from_vendor(vendor: str) -> str:
    v = (vendor or "").lower()
    if v == "nvidia":
        return "nvidia"
    if v == "amd":
        return "amd"
    if v in {"google", "aws", "amazon", "meta", "microsoft", "tesla", "apple", "intel"}:
        return "custom_silicon"
    return "custom_silicon"  # default
