"""v3.4.12 — Transcript memory + quarter-over-quarter diff.

Problem
=======
The brief reacts to each earnings call IN ISOLATION. The signal that matters
is CHANGE — what the CFO says today vs what the CFO said LAST quarter. If
SK Hynix Q3 2026 CFO drops "sold out through Q1 2027" language for the first
time, that's the single most important T2 signal in the cycle and the
pre-v3.4.12 framework can't see it.

Design (cost-efficient — process once, cache, diff only on new content)
=======================================================================
Already in place from v3.39.x:
  - Layer 1 digester runs in DEEP TIER, once per transcript
  - state/digests/YYYY-MM-DD/*.json persists each digest
  - state/cache/transcripts_seen.json prevents re-fetching

v3.4.12 adds:
  1. Per-quarter store: state/transcripts/{ticker_safe}/{YYYY-QN}.json
     Sole writer: digester when ticker + quarter is determinable.
  2. Diff trigger: when a new quarter file lands AND a prior quarter (Q-1)
     file exists AND the diff hasn't been computed for THIS digest version
     yet, run ONE Haiku call comparing the two digests. Cache the result
     inline in the same quarter file.
  3. Sonnet prompt receives active diffs as a new context block — narrates
     QoQ language shifts in where_we_stand for transcript-driven indicators.

Cost: ~10 companies × 4 quarters/year × $0.005/Haiku diff call = ~$0.20/year.
Trivial. The expensive part (original digest) is already paid; the diff is
pure savings.

Module layout
=============
This file holds the Haiku diff prompt + parser. The per-quarter state I/O
lives in src/state.py (load_quarter_digest / save_quarter_digest /
quarter_key_from_event_date / get_prior_quarter_key). The digester hook
lives in src/analysis/digester.py (calls into both).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("memory_cycle_brief.transcript_diff")


# ============================================================
# Result dataclasses
# ============================================================

@dataclass
class IndicatorDelta:
    """A single indicator-vocabulary change between two transcripts."""
    indicator_id: str
    direction: str             # "strengthening" | "weakening" | "unchanged" | "new" | "dropped"
    summary: str               # short human-readable description of the change
    prior_quote: Optional[str] = None
    current_quote: Optional[str] = None


@dataclass
class MetricDelta:
    """A named numeric key_metric that moved between quarters."""
    metric_name: str
    prior_value: Optional[str] = None     # stringified for cross-type compat
    current_value: Optional[str] = None
    direction: str = "unchanged"  # "up" | "down" | "unchanged" | "new" | "dropped"


@dataclass
class TranscriptDiff:
    """Structured QoQ diff result between two transcript digests."""
    ticker: str
    company: str
    current_quarter: str       # "2026-Q2"
    prior_quarter: str         # "2026-Q1"
    current_digest_event_date: str
    prior_digest_event_date: str
    indicator_deltas: list[IndicatorDelta] = field(default_factory=list)
    metric_deltas: list[MetricDelta] = field(default_factory=list)
    language_dropped: list[str] = field(default_factory=list)
    language_added: list[str] = field(default_factory=list)
    overall_tone_shift: str = "unchanged"  # "more bullish" | "more bearish" | "unchanged" | "mixed"
    computed_at_iso: str = ""
    haiku_model: str = ""
    haiku_input_tokens: int = 0
    haiku_output_tokens: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["computed_at_iso"]:
            d["computed_at_iso"] = datetime.utcnow().isoformat()
        return d


# ============================================================
# Haiku diff prompt
# ============================================================

DIFF_SYSTEM_PROMPT = """You are an analytical assistant comparing two consecutive earnings-call digests for the same company. Your job is to identify CHANGES — not to re-summarize either call.

Output schema is strict. Identify:
1. INDICATOR DELTAS — for each memory-cycle indicator referenced in either digest's `signal_relevance`, is the language strengthening (more bearish-for-cycle), weakening (less bearish-for-cycle), unchanged, newly present, or dropped between the two calls?
2. METRIC DELTAS — for each named key_metric in either digest, did the value move? Direction = up / down / unchanged / new / dropped.
3. LANGUAGE DROPPED — phrases or specific commitments present in prior call that are ABSENT in current (e.g., "sold out through 2027" → not said this time).
4. LANGUAGE ADDED — new specific language in current call not in prior (e.g., "softening in some end-markets").
5. OVERALL TONE SHIFT — more bullish / more bearish / unchanged / mixed.

CRITICAL RULES:
- Focus on what CHANGED, not what stayed the same. Unchanged language is implicit; don't restate it.
- For indicator deltas: only emit deltas for indicator_ids that appear in either digest's signal_relevance. Don't speculate about indicators neither digest tagged.
- Direction semantics: "strengthening" = the evidence for this indicator firing got stronger between calls; "weakening" = the evidence got weaker.
- Prior + current quotes: keep verbatim, under 15 words each. Use the digest's key_quotes when possible; if needed phrases aren't in either digest's quote list, omit the quote (set to null) — DO NOT fabricate.
- Language dropped is most important: when a CFO STOPS using a previous commitment (e.g., "sold out through Q1 2027") that's a cycle-turn signal. Be specific about what was dropped.
"""


DIFF_USER_TEMPLATE = """Compare these two digests for {company} ({ticker}).

PRIOR QUARTER ({prior_quarter}, event date {prior_event_date}):
```json
{prior_digest_json}
```

CURRENT QUARTER ({current_quarter}, event date {current_event_date}):
```json
{current_digest_json}
```

Respond with ONLY a JSON object matching this schema:
{{
  "indicator_deltas": [
    {{"indicator_id": "t2_eps_revisions", "direction": "strengthening", "summary": "...", "prior_quote": "...", "current_quote": "..."}}
  ],
  "metric_deltas": [
    {{"metric_name": "hbm_revenue_yoy_pct", "prior_value": "120", "current_value": "85", "direction": "down"}}
  ],
  "language_dropped": ["sold out through 2027 commitment"],
  "language_added": ["softening orders from PC OEMs in Q4"],
  "overall_tone_shift": "more bearish"
}}

Only output the JSON object."""


# ============================================================
# Public entry point
# ============================================================

def compute_diff_with_prior(
    current_digest: dict,
    prior_digest: dict,
    ticker: str,
    company: str,
    current_quarter: str,
    prior_quarter: str,
    anthropic_api_key: str,
    haiku_model: str = "claude-haiku-4-5",
    max_tokens: int = 2000,
) -> Optional[TranscriptDiff]:
    """Run Haiku to compute the QoQ diff between two transcript digests.

    Returns a TranscriptDiff on success, or a TranscriptDiff with .error
    set on Haiku failure (parser will still see structured data with empty
    deltas — safe to render). Returns None only on missing API key or
    truly unrecoverable errors.

    Cost: ~5K input tokens + ~1-2K output tokens per call. At Haiku 4.5
    pricing (~$0.001/Ktok input, $0.005/Ktok output), each call is roughly
    $0.005. Caller is responsible for caching the result so re-runs don't
    re-call Haiku.
    """
    if not anthropic_api_key:
        logger.warning("No ANTHROPIC_API_KEY — cannot compute transcript diff")
        return None

    # Lazy import to keep module-level deps minimal
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.error("anthropic SDK not available")
        return None

    # Trim digests to just the fields the prompt cares about (keeps input size
    # tight and avoids feeding the model irrelevant metadata)
    def _trim(digest: dict) -> dict:
        return {
            "title": digest.get("title", ""),
            "published_date_iso": digest.get("published_date_iso", ""),
            "key_metrics": digest.get("key_metrics", {}),
            "signal_relevance": digest.get("signal_relevance", []),
            "key_quotes": digest.get("key_quotes", []),
            "fired_status_evidence": digest.get("fired_status_evidence", "neutral"),
        }

    prior_trim = _trim(prior_digest)
    current_trim = _trim(current_digest)

    user_msg = DIFF_USER_TEMPLATE.format(
        company=company,
        ticker=ticker,
        prior_quarter=prior_quarter,
        current_quarter=current_quarter,
        prior_event_date=prior_digest.get("published_date_iso", "?"),
        current_event_date=current_digest.get("published_date_iso", "?"),
        prior_digest_json=json.dumps(prior_trim, indent=2),
        current_digest_json=json.dumps(current_trim, indent=2),
    )

    diff = TranscriptDiff(
        ticker=ticker,
        company=company,
        current_quarter=current_quarter,
        prior_quarter=prior_quarter,
        current_digest_event_date=current_digest.get("published_date_iso", ""),
        prior_digest_event_date=prior_digest.get("published_date_iso", ""),
        haiku_model=haiku_model,
    )

    try:
        client = Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=haiku_model,
            max_tokens=max_tokens,
            system=DIFF_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        # Capture usage for observability
        if hasattr(response, "usage"):
            diff.haiku_input_tokens = getattr(response.usage, "input_tokens", 0)
            diff.haiku_output_tokens = getattr(response.usage, "output_tokens", 0)

        text = response.content[0].text.strip() if response.content else ""
        if not text:
            diff.error = "empty_response"
            return diff

        # Strip markdown fences if Haiku ever adds them
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"Diff JSON parse failed for {ticker} {current_quarter}: {e}")
            diff.error = f"json_parse_error: {e}"
            return diff

        # Populate deltas defensively
        for d in parsed.get("indicator_deltas", []) or []:
            if not isinstance(d, dict) or not d.get("indicator_id"):
                continue
            diff.indicator_deltas.append(IndicatorDelta(
                indicator_id=str(d.get("indicator_id", "")),
                direction=str(d.get("direction", "unchanged")),
                summary=str(d.get("summary", ""))[:300],
                prior_quote=d.get("prior_quote"),
                current_quote=d.get("current_quote"),
            ))
        for m in parsed.get("metric_deltas", []) or []:
            if not isinstance(m, dict) or not m.get("metric_name"):
                continue
            diff.metric_deltas.append(MetricDelta(
                metric_name=str(m.get("metric_name", "")),
                prior_value=str(m.get("prior_value")) if m.get("prior_value") is not None else None,
                current_value=str(m.get("current_value")) if m.get("current_value") is not None else None,
                direction=str(m.get("direction", "unchanged")),
            ))
        diff.language_dropped = [
            str(x)[:200] for x in (parsed.get("language_dropped") or []) if x
        ][:10]
        diff.language_added = [
            str(x)[:200] for x in (parsed.get("language_added") or []) if x
        ][:10]
        diff.overall_tone_shift = str(parsed.get("overall_tone_shift", "unchanged"))

        return diff

    except Exception as e:
        logger.warning(f"Transcript diff Haiku call failed for {ticker} {current_quarter}: {e}")
        diff.error = f"haiku_error: {str(e)[:200]}"
        return diff


def render_diff_for_sonnet_prompt(diff: dict) -> str:
    """Render a stored TranscriptDiff dict as a compact prompt block.

    Used by build_sonnet_analysis_prompt to inject QoQ diff context into
    the Sonnet prompt so the model narrates the language shifts in
    where_we_stand for transcript-driven indicators.

    Keeps it short — single paragraph per diff, indicator deltas as
    bullet lines. Sonnet quotes verbatim from this block; we keep it tight.
    """
    if not isinstance(diff, dict):
        return ""

    company = diff.get("company", "?")
    cq = diff.get("current_quarter", "?")
    pq = diff.get("prior_quarter", "?")
    tone = diff.get("overall_tone_shift", "unchanged")

    if diff.get("error"):
        return f"  [{company} {pq}→{cq}: diff unavailable ({diff['error']})]\n"

    lines = [f"  [{company} {pq}→{cq} | overall tone shift: {tone}]"]

    # Indicator deltas
    for d in (diff.get("indicator_deltas") or [])[:8]:
        ind = d.get("indicator_id", "?")
        direction = d.get("direction", "?")
        summary = d.get("summary", "")[:200]
        lines.append(f"    • {ind} ({direction}): {summary}")
        if d.get("current_quote") and d.get("prior_quote"):
            lines.append(f'      prior: "{d["prior_quote"]}" → current: "{d["current_quote"]}"')

    # Language dropped (most important signal — CFO stopped saying something)
    dropped = diff.get("language_dropped") or []
    if dropped:
        lines.append(f"    LANGUAGE DROPPED (prior said, current did NOT):")
        for x in dropped[:5]:
            lines.append(f"      - {x}")

    # Language added
    added = diff.get("language_added") or []
    if added:
        lines.append(f"    LANGUAGE ADDED (new in current call):")
        for x in added[:5]:
            lines.append(f"      - {x}")

    # Metric deltas (just the named-and-moved ones)
    moved_metrics = [
        m for m in (diff.get("metric_deltas") or [])
        if m.get("direction") in ("up", "down")
    ]
    if moved_metrics:
        lines.append(f"    KEY METRICS THAT MOVED:")
        for m in moved_metrics[:5]:
            name = m.get("metric_name", "?")
            prior = m.get("prior_value", "?")
            current = m.get("current_value", "?")
            direction = m.get("direction", "?")
            lines.append(f"      - {name}: {prior} → {current} ({direction})")

    return "\n".join(lines) + "\n"
