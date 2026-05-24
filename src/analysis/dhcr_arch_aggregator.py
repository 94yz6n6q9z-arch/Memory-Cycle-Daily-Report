"""DHCR Frontier Model Catalog — Layer 1 extraction aggregator (v3.4.5).

Reads accumulated `state/extraction_log_*.jsonl` entries for the
`new_frontier_model_release` field, dedupes by (name, release_date), and
appends new entries to `state/inference_arch_state.json` under open_models
or closed_models.

Runs as a daily step in the fast-tier pipeline BEFORE `compute_dhcr_snapshot()`.
Safe-failing — exception or empty extraction log preserves prior state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

THRESHOLDS_FILE = Path("data/inference_arch_reference.yaml")
STATE_FILE = Path("state/inference_arch_state.json")
EXTRACTION_LOG_DIR = Path("state")
EXTRACTION_LOG_GLOB = "extraction_log_*.jsonl"

# Source authority tiers (kept in sync with the YAML)
TIER_A_SOURCES = {"arxiv", "huggingface", "ai.meta.com", "deepseek.com", "mistral.ai", "qwenlm.github.io"}
TIER_B_SOURCES = {"semianalysis", "paperswithcode"}


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
        return {"_metadata": {}, "open_models": [], "closed_models": []}
    with open(STATE_FILE) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def aggregate_arch_extractions(lookback_days: Optional[int] = None) -> dict:
    """Append newly-disclosed frontier models to state/inference_arch_state.json.

    Dedup key: (name, release_date). When same key already exists, prefer the
    Tier-A source entry (which carries more authoritative architecture specs).
    """
    try:
        with open(THRESHOLDS_FILE) as f:
            thresholds = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("inference_arch_reference.yaml missing — arch aggregator skipped")
        return {}

    if lookback_days is None:
        lookback_days = thresholds.get("aggregator_config", {}).get("extraction_log_lookback_days", 180)

    state = _load_state()
    open_models = state.setdefault("open_models", [])
    closed_models = state.setdefault("closed_models", [])
    open_keys = {(m.get("name"), m.get("release_date")) for m in open_models}
    closed_keys = {(m.get("name"), m.get("release_date")) for m in closed_models}
    additions = 0

    try:
        entries = _iter_extraction_log_entries(lookback_days)
        # Group by (name, release_date) and keep highest-tier source
        by_key: dict[tuple, tuple[int, dict, dict]] = {}
        for e in entries:
            # v3.5.1 — extraction_log writer puts arch signals under "signals" key (see digester._log_architecture_signals);
            # the "architecture_signals" fallback covers any legacy entries.
            arch_sigs = e.get("signals") or e.get("architecture_signals") or {}
            release = arch_sigs.get("new_frontier_model_release")
            if not release or not isinstance(release, dict):
                continue
            name = release.get("name")
            rdate = release.get("release_date")
            if not name or not rdate:
                continue
            key = (name, rdate)
            if key in open_keys or key in closed_keys:
                continue  # already catalogued
            tier = _source_tier(e.get("source", ""))
            prior = by_key.get(key)
            if prior is None or tier < prior[0]:
                by_key[key] = (tier, release, e)

        for (name, rdate), (tier, release, source_entry) in by_key.items():
            new_entry = {
                "name": name,
                "release_date": rdate,
                "architecture_family": release.get("architecture_family", "GQA"),
                "num_layers": release.get("num_layers"),
                "num_kv_heads": release.get("num_kv_heads"),
                "head_dim": release.get("head_dim"),
                "bytes_per_element": release.get("bytes_per_element"),
                "compression_factor": release.get("compression_factor", 1.0),
                "source_url": release.get("source_url", ""),
                "notes": f"Auto-added by dhcr_arch_aggregator from {source_entry.get('source_id', 'unknown')} (Tier {['A','B','C'][min(tier-1, 2)]} source).",
            }
            if release.get("kv_kb_per_token") is not None:
                new_entry["kv_kb_per_token"] = release["kv_kb_per_token"]
            if release.get("is_open", True):
                open_models.append(new_entry)
            else:
                new_entry["confidence_band"] = "medium"
                closed_models.append(new_entry)
            additions += 1
            logger.info(f"  arch catalog: added {name} ({rdate})")

        state["_metadata"]["last_aggregator_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["_metadata"]["last_run_additions"] = additions
        _save_state(state)
        logger.info(f"DHCR arch aggregator: {additions} new model(s) added; {len(entries)} extraction entries scanned.")
    except Exception as e:
        logger.error(f"DHCR arch aggregator failed (state preserved): {e}", exc_info=True)

    return state
