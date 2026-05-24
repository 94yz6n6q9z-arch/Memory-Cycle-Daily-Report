"""Context-length product-doc scraper (v3.4.14).

Scrapes official provider documentation pages monthly to track published
context-window limits per workload category and update
state/context_length_state.json.

4-status observability taxonomy (same pattern as v3.4.13 LPDDR scraper):
  "data_extracted"          → page fetched and context figure parsed
  "no_context_spec_found"   → page loaded but no recognisable context-window
                              figure present (e.g. page restructured without
                              the expected keywords)
  "parser_no_match"         → keyword found but none of the value-extraction
                              regexes matched → likely a BUG in the patterns
  "fetch_failed"            → HTTP error or network timeout

Hard sentinel at state/context_length_scraper_consecutive_parser_no_match.json.
After 3 consecutive parser_no_match runs the diagnostic escalates so the
failure is not silently ignored.

Run via the monthly context_length_refresh.yml GHA workflow, or manually:
    python -m src.data.context_length_scraper

Each category result feeds into state/context_length_state.json which
dhcr.py:load_dhcr_data() reads instead of the static YAML values.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── State / sentinel paths ────────────────────────────────────────────────────

CONTEXT_STATE_FILE = Path("state/context_length_state.json")
CONTEXT_SCRAPER_SENTINEL_FILE = Path(
    "state/context_length_scraper_consecutive_parser_no_match.json"
)
CONTEXT_SCRAPER_SENTINEL_THRESHOLD = 3

# Minimum fractional change before we bother recording a new history entry
# (avoids writing noise when the page just reworded "128K" as "131,072 tokens")
MIN_CHANGE_FRACTION = 0.05   # 5%

# ── Category definitions ──────────────────────────────────────────────────────
# Each category maps to a list of providers.  Weights within a category must
# sum to 1.0.  The final category context = Σ(provider_value × weight).
# A provider with fetch_failed / no_context_spec_found contributes nothing to
# the weighted sum; remaining weights are renormalised at runtime.

CATEGORY_PROVIDERS: dict[str, list[dict]] = {
    "consumer_chat": [
        {
            "name": "OpenAI (gpt-4o / o3)",
            "url": "https://platform.openai.com/docs/models",
            "weight": 0.60,
            # Patterns tried in order; first match wins.
            # Each must yield a raw digit-string (after stripping commas).
            "patterns": [
                r"gpt-4o[^\n]{0,200}?([\d]{3,}[,\d]*)\s*tokens",
                r"128[,\s]*000\s*tokens",        # known gpt-4o context
                r"context\s+window[^:]*:\s*([\d,]+)",
            ],
            "known_value": 128_000,   # fallback if page unreachable
        },
        {
            "name": "Anthropic (claude-3.7 / claude-sonnet-4)",
            "url": "https://docs.anthropic.com/en/docs/about-claude/models",
            "weight": 0.20,
            "patterns": [
                r"200[,\s]*000\s*tokens",
                r"context\s+window[^:]*:\s*([\d,]+)",
                r"([\d]{3,}[,\d]*)\s*token\s+context",
            ],
            "known_value": 200_000,
        },
        {
            "name": "Google (Gemini 2.0 Flash)",
            "url": "https://ai.google.dev/gemini-api/docs/models",
            "weight": 0.15,
            "patterns": [
                r"1[,\s]*000[,\s]*000\s*tokens",   # 1M context
                r"1M\s*(?:token|context)",
                r"context\s*(?:window|length)[^:]*:\s*([\d,]+)",
            ],
            "known_value": 1_000_000,
        },
        {
            "name": "Others (Mistral, xAI, etc.)",
            "url": None,                # no scrape; static estimate
            "weight": 0.05,
            "patterns": [],
            "known_value": 32_000,
        },
    ],

    "coding_agents": [
        {
            "name": "Cursor",
            "url": "https://docs.cursor.com/context/codebase-indexing",
            "weight": 0.35,
            "patterns": [
                r"([\d,]+)\s*token\s+context",
                r"context\s+window[^:]*:\s*([\d,]+)",
                r"200[,\s]*000",
            ],
            "known_value": 200_000,
        },
        {
            "name": "Claude Code",
            "url": "https://docs.anthropic.com/en/docs/claude-code/overview",
            "weight": 0.30,
            "patterns": [
                r"500[,\s]*000\s*tokens",
                r"([\d,]+)\s*token\s+context",
                r"context\s+window[^:]*:\s*([\d,]+)",
            ],
            "known_value": 500_000,
        },
        {
            "name": "GitHub Copilot Workspace",
            "url": "https://githubnext.com/projects/copilot-workspace",
            "weight": 0.20,
            "patterns": [
                r"([\d,]+)\s*token",
                r"context\s*:\s*([\d,]+)",
            ],
            "known_value": 200_000,
        },
        {
            "name": "Devin / Cognition",
            "url": "https://docs.devin.ai",
            "weight": 0.15,
            "patterns": [
                r"([\d,]+)\s*token\s+context",
                r"1[,\s]*000[,\s]*000",
            ],
            "known_value": 500_000,
        },
    ],

    "enterprise_vertical_agents": [
        {
            "name": "Microsoft Copilot Enterprise",
            "url": "https://learn.microsoft.com/en-us/copilot/microsoft-365/microsoft-365-copilot-overview",
            "weight": 0.40,
            "patterns": [
                r"([\d,]+)\s*token\s+context",
                r"context\s+window[^:]*:\s*([\d,]+)",
                r"1[,\s]*000[,\s]*000",
            ],
            "known_value": 1_000_000,
        },
        {
            "name": "Anthropic Enterprise",
            "url": "https://www.anthropic.com/enterprise",
            "weight": 0.35,
            "patterns": [
                r"200[,\s]*000",
                r"([\d,]+)\s*token\s*(?:context|window)",
            ],
            "known_value": 500_000,
        },
        {
            "name": "Harvey / Hebbia / Sierra (case-study estimate)",
            "url": None,               # anecdotal; static known_value
            "weight": 0.25,
            "patterns": [],
            "known_value": 1_000_000,
        },
    ],

    "developer_raw_api": [
        {
            "name": "OpenAI API",
            "url": "https://platform.openai.com/docs/models",
            "weight": 0.55,
            "patterns": [
                r"gpt-4o[^\n]{0,200}?([\d]{3,}[,\d]*)\s*tokens",
                r"128[,\s]*000\s*tokens",
            ],
            "known_value": 128_000,
        },
        {
            "name": "Anthropic API",
            "url": "https://docs.anthropic.com/en/docs/about-claude/models",
            "weight": 0.35,
            "patterns": [
                r"200[,\s]*000\s*tokens",
                r"([\d,]+)\s*token\s+context",
            ],
            "known_value": 200_000,
        },
        {
            "name": "Other providers",
            "url": None,
            "weight": 0.10,
            "patterns": [],
            "known_value": 32_000,
        },
    ],

    "other_creative_rag": [
        {
            "name": "Perplexity",
            "url": "https://docs.perplexity.ai/guides/model-cards",
            "weight": 0.50,
            "patterns": [
                r"([\d,]+)\s*token\s*(?:context|window)",
                r"128[,\s]*000",
            ],
            "known_value": 127_000,
        },
        {
            "name": "Generic RAG / embedded chat",
            "url": None,
            "weight": 0.50,
            "patterns": [],
            "known_value": 32_000,
        },
    ],
}

CATEGORIES = list(CATEGORY_PROVIDERS.keys())


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class CategoryScrapeResult:
    category: str
    status: str          # data_extracted | no_context_spec_found | parser_no_match | fetch_failed
    context_tokens: Optional[int] = None
    providers_scraped: list[str] = field(default_factory=list)
    providers_failed: list[str] = field(default_factory=list)
    detail: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_context_number(raw: str) -> Optional[int]:
    """Convert raw digit string (possibly with commas/K/M suffix) to int tokens."""
    raw = raw.strip().replace(",", "").replace(" ", "")
    if raw.upper().endswith("M"):
        try:
            return int(float(raw[:-1]) * 1_000_000)
        except ValueError:
            return None
    if raw.upper().endswith("K"):
        try:
            return int(float(raw[:-1]) * 1_000)
        except ValueError:
            return None
    try:
        v = int(raw)
        return v if v >= 1_000 else None    # sanity: ignore tiny numbers
    except ValueError:
        return None


def _extract_context_from_html(html: str, patterns: list[str]) -> tuple[str, Optional[int]]:
    """Try each pattern; return (status, value).

    status is one of:
      "data_extracted"         → a pattern matched and yielded a plausible number
      "parser_no_match"        → keyword heuristic found but no pattern matched
      "no_context_spec_found"  → no context-related keyword on page at all
    """
    lower = html.lower()
    context_keyword_present = any(kw in lower for kw in
                                  ["token", "context window", "context length"])

    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            # Strip anything that isn't a digit, comma, K, or M
            raw = re.sub(r"[^\dKMkm,.]", "", raw)
            value = _parse_context_number(raw)
            if value is not None:
                return "data_extracted", value

    if context_keyword_present:
        return "parser_no_match", None
    return "no_context_spec_found", None


def _fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch URL; return HTML text or None on error."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    try:
        # Try curl_cffi first (Cloudflare bypass) then fall back to stdlib requests
        try:
            from curl_cffi import requests as curl_requests
            resp = curl_requests.get(
                url, headers=headers, timeout=timeout, impersonate="chrome120"
            )
        except ImportError:
            import requests
            resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"Context scraper: HTTP {resp.status_code} for {url}")
            return None
        return resp.text
    except Exception as exc:
        logger.warning(f"Context scraper: fetch failed for {url}: {exc}")
        return None


def _scrape_provider(provider: dict) -> tuple[str, Optional[int]]:
    """Scrape a single provider. Returns (status, context_tokens)."""
    url = provider.get("url")
    known = provider.get("known_value")

    if not url:
        # Static-only provider: return known_value as data_extracted
        return "data_extracted", known

    html = _fetch_page(url)
    if html is None:
        return "fetch_failed", None

    status, value = _extract_context_from_html(html, provider.get("patterns", []))
    if status == "data_extracted":
        return "data_extracted", value
    if status == "parser_no_match":
        return "parser_no_match", None
    # no_context_spec_found — page restructured; fall back to known_value
    return "no_context_spec_found", None


def _compute_category_result(
    category: str,
    providers: list[dict],
) -> CategoryScrapeResult:
    """Scrape all providers in a category; compute weighted average."""
    result = CategoryScrapeResult(category=category, status="fetch_failed")
    weighted_sum = 0.0
    weight_total = 0.0
    any_parser_no_match = False
    any_data = False

    for prov in providers:
        name = prov["name"]
        weight = prov["weight"]
        time.sleep(0.5)     # be polite between requests

        status, value = _scrape_provider(prov)
        logger.info(f"  context scraper [{category}] {name}: {status} → {value}")

        if status == "data_extracted" and value is not None:
            weighted_sum += value * weight
            weight_total += weight
            result.providers_scraped.append(name)
            any_data = True
        elif status == "parser_no_match":
            any_parser_no_match = True
            result.providers_failed.append(f"{name}:parser_no_match")
        elif status == "fetch_failed":
            # Still use known_value at reduced weight so one failed provider
            # doesn't zero out the category
            known = prov.get("known_value")
            if known:
                weighted_sum += known * weight
                weight_total += weight
            result.providers_failed.append(f"{name}:fetch_failed")
        else:   # no_context_spec_found — page restructured
            known = prov.get("known_value")
            if known:
                weighted_sum += known * weight
                weight_total += weight
            result.providers_failed.append(f"{name}:no_spec")

    if any_data:
        # At least one provider returned a real scraped value.
        result.context_tokens = int(round(weighted_sum / weight_total))
        result.status = "data_extracted"
    elif weight_total > 0:
        # All providers fell back to known_value. We DO have a number, but it's
        # entirely from hardcoded fallbacks — flag the status accordingly so the
        # diagnostic doesn't pretend the scrape succeeded.
        result.context_tokens = int(round(weighted_sum / weight_total))
        if any_parser_no_match:
            result.status = "parser_no_match"
            result.detail = "100% fallback values; some providers had parser_no_match"
        else:
            result.status = "no_context_spec_found"
            result.detail = "100% fallback values; pages loaded without recognizable context spec"
    elif any_parser_no_match:
        result.status = "parser_no_match"
        result.detail = "All providers failed; some had parser_no_match"
    else:
        result.status = "fetch_failed"
        result.detail = "All providers failed with fetch errors"

    return result


# ── Sentinel ──────────────────────────────────────────────────────────────────

def _load_sentinel() -> int:
    try:
        data = json.loads(CONTEXT_SCRAPER_SENTINEL_FILE.read_text())
        return int(data.get("consecutive_parser_no_match", 0))
    except Exception:
        return 0


def _save_sentinel(count: int) -> None:
    try:
        CONTEXT_SCRAPER_SENTINEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONTEXT_SCRAPER_SENTINEL_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"consecutive_parser_no_match": count}, indent=2))
        tmp.replace(CONTEXT_SCRAPER_SENTINEL_FILE)
    except Exception as exc:
        logger.warning(f"Could not save context scraper sentinel: {exc}")


# ── State helpers ─────────────────────────────────────────────────────────────

def _bootstrap_state_from_yaml() -> dict:
    """Seed state from data/context_length_signals.yaml on first run."""
    try:
        import yaml
        yaml_path = Path("data/context_length_signals.yaml")
        raw = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else {}
    except Exception:
        raw = {}

    cats = raw.get("categories", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    typical: dict = {}
    for cat in CATEGORIES:
        cat_data = cats.get(cat, {})
        val = int(cat_data.get("typical_context_tokens", 0))
        typical[cat] = {
            "current": val,
            "history": [{"date": today, "value": val,
                         "source": "yaml_baseline", "providers": {}}],
        }

    committed_shares: dict = {}
    for cat in CATEGORIES:
        cat_data = cats.get(cat, {})
        committed_shares[cat] = float(cat_data.get("volume_share_pct", 0))

    return {
        "_metadata": {"schema_version": "1.0"},
        "typical_context_tokens": typical,
        "volume_shares": {
            "committed": committed_shares,
            "proposed": None,
            "proposal_evidence": [],
            "proposal_status": "none",
            "proposal_created": None,
            "history": [{"date": today, "shares": dict(committed_shares),
                         "trigger": "baseline"}],
        },
        "operative_context_history": [],
    }


def _load_context_state() -> dict:
    if not CONTEXT_STATE_FILE.exists():
        return _bootstrap_state_from_yaml()
    try:
        return json.loads(CONTEXT_STATE_FILE.read_text())
    except Exception as exc:
        logger.warning(f"Could not read context_length_state.json: {exc}; bootstrapping")
        return _bootstrap_state_from_yaml()


def _save_context_state(state: dict) -> None:
    CONTEXT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTEXT_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(CONTEXT_STATE_FILE)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_context_length_scraper() -> dict:
    """Scrape all categories and update state/context_length_state.json.

    Returns a summary dict for diagnostics / logging.
    """
    state = _load_context_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    overall_parser_no_match = False
    categories_updated: list[str] = []
    categories_unchanged: list[str] = []
    categories_failed: list[str] = []

    for category, providers in CATEGORY_PROVIDERS.items():
        logger.info(f"Context scraper: scraping category '{category}'")
        cat_result = _compute_category_result(category, providers)

        if cat_result.status == "parser_no_match":
            overall_parser_no_match = True
            categories_failed.append(category)
            logger.warning(
                f"Context scraper [{category}]: parser_no_match — "
                f"failed providers: {cat_result.providers_failed}"
            )
            continue

        # v3.5.1 — fetch_failed AND no_context_spec_found both mean every
        # provider in the category was unable to return real data. Even if
        # context_tokens is set (computed from fallback known_value), we
        # MUST NOT write it as history — that would silently freeze stale
        # values into the state file under a "scraper" source label.
        if cat_result.status in ("fetch_failed", "no_context_spec_found"):
            categories_failed.append(category)
            logger.warning(
                f"Context scraper [{category}]: {cat_result.status} — "
                f"{cat_result.detail or ''}"
            )
            continue

        if cat_result.context_tokens is None:
            categories_failed.append(category)
            continue

        # Compare against current value; only record if change is significant
        cat_state = state.setdefault("typical_context_tokens", {}).setdefault(
            category, {"current": cat_result.context_tokens, "history": []}
        )
        current_value = cat_state.get("current", 0)
        new_value = cat_result.context_tokens

        if current_value > 0:
            change_fraction = abs(new_value - current_value) / current_value
        else:
            change_fraction = 1.0  # always write on first data point

        if change_fraction >= MIN_CHANGE_FRACTION:
            cat_state["current"] = new_value
            cat_state.setdefault("history", []).append({
                "date": today,
                "value": new_value,
                "source": "scraper",
                "providers": {p: "ok" for p in cat_result.providers_scraped},
                "prior_value": current_value,
                "change_pct": round(change_fraction * 100, 1),
            })
            categories_updated.append(f"{category}:{current_value}→{new_value}")
            logger.info(
                f"Context scraper [{category}]: updated "
                f"{current_value:,} → {new_value:,} tokens (+{change_fraction*100:.1f}%)"
            )
        else:
            categories_unchanged.append(category)
            logger.info(
                f"Context scraper [{category}]: no significant change "
                f"({new_value:,} vs {current_value:,}, {change_fraction*100:.1f}%)"
            )

    # Sentinel tracking
    if overall_parser_no_match:
        sentinel_count = _load_sentinel() + 1
        _save_sentinel(sentinel_count)
        if sentinel_count >= CONTEXT_SCRAPER_SENTINEL_THRESHOLD:
            logger.error(
                f"Context scraper: {sentinel_count} consecutive parser_no_match runs — "
                f"BUG SUSPECTED. Check regex patterns against live page HTML."
            )
        else:
            logger.warning(
                f"Context scraper: parser_no_match "
                f"({sentinel_count}/{CONTEXT_SCRAPER_SENTINEL_THRESHOLD})"
            )
    else:
        _save_sentinel(0)   # reset on any non-parser_no_match run

    # Persist updated state
    state.setdefault("_metadata", {})["last_scraper_run"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _save_context_state(state)

    summary = {
        "categories_updated": categories_updated,
        "categories_unchanged": categories_unchanged,
        "categories_failed": categories_failed,
        "overall_status": (
            "data_extracted" if categories_updated or categories_unchanged
            else ("parser_no_match" if overall_parser_no_match else "fetch_failed")
        ),
    }
    logger.info(
        f"Context scraper complete: updated={len(categories_updated)} "
        f"unchanged={len(categories_unchanged)} failed={len(categories_failed)}"
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    result = run_context_length_scraper()
    print(f"\nScraper result: {result}")
