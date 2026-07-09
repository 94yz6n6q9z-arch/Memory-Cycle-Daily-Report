"""
Two-stage LLM analysis pipeline.

Stage 1 (Haiku): scrub all news, categorize relevance to indicators,
extract analyst broker actions (upgrade/downgrade/target change),
identify TrendForce price commentary, summarize key items.

Stage 2 (Sonnet): take Haiku's structured output + price/estimate data
+ prior assessments, produce tier-by-tier classification, executive
summary with probability calculations, and cycle position synthesis.

Falls back to deterministic synthesis if API unavailable.

CHANGELOG (this file):
  v3.39.7.3 (2026-05-20) — SYMMETRIC PROBABILITY RULES added to the Sonnet prompt. v3.39.7's PROBABILITY UPDATE RULES only covered "indicator fires today" (rule A); there was no rule for "indicator unfires today" (rule B). Result on the May 20 brief: 4 indicators went FIRED→Not fired, 1 went FIRED→Partially fired, 3 went Partially fired→Not fired — but bull/base/bear probabilities stayed byte-identical to May 18 (22%/50%/28%). The LLM literally wrote "no probability distribution changes warranted as no new tier signals fired", ignoring 8 unfires. Now adds: (B) explicit unfire rules — Tier 1 unfire = bear -2-4pp + bull +1-2pp + base peak extends 1mo; Tier 2 = bear -3-6pp + bull +2-4pp + 1-2mo; Tier 3 = bear -5-8pp + bull +3-5pp + 2-3mo; Tier 4 = bear -5-8pp + bull +5-8pp + 2-3mo. (C) MANDATORY RECALCULATION: probabilities stay byte-identical to prior only when EVERY indicator's status is unchanged — any status change in either direction requires recalc. (D) COMPUTED-METRIC OVERRIDE: when forward_pe / rel_strength_full / capex_revenue_ratios / eps_revisions show fired but Layer 2 returns Not fired with evidence_count=0, Sonnet must use the computed verdict, not Layer 2 — fixes the Forward P/E contradiction from the May 20 brief where all 3 names were below their thresholds per computed data but Layer 2 said Not fired.
  v3.39.7 (2026-05-20) — Cycle matrix Haiku extraction layer appended at bottom. Two new prompt+function pairs: extract_dram_signal_from_article (tone/segment/supplier/qoq structured fields → data/dram_contract_signals.csv) and extract_dram_forecasts_from_press_release (quarterly forecast list → data/dram_contract_quarterly_forecasts.csv). Both use HAIKU_MODEL with temperature=0. Pre-filtered by title in fast_tier.py to ~10 DRAM-relevant articles/run; ~$0.50/run cost ceiling. Allowed-value sets prevent free-text drift. Output validated and coerced into the CSV schemas. Reuses the existing Anthropic client pattern from this module.
  v3.38.5 (2026-05-17) — Real root cause of the 16:25 UTC May 17 Haiku triage failure found and fixed. The error pattern was "Failed to parse Haiku response as JSON: Expecting value (char 0)" → json_repair fallback returned str (not dict) → unrecoverable. This was NOT a prompt issue or a max_tokens issue. The bug was in `_strip_code_fence`: when the LLM returned a properly closed code fence (```json\n{...}\n```), the line `text.split("```", 2)[-1]` grabbed the empty string AFTER the closing fence instead of the JSON content BETWEEN the fences. The function returned `""`. json.loads("") fails with "Expecting value (char 0)"; json_repair.loads("") returns `""` (a str). Rewrote the function with explicit handling of three patterns (raw / closed-fence / unclosed-fence). Verified locally on all five common LLM-output shapes — all parse cleanly via stdlib now. Also tightened `build_haiku_triage_prompt` output format requirements (start with `{`, no markdown fences, no prose, no apologies) as belt-and-suspenders. The previous bump of HAIKU_MAX_TOKENS to 16000 and the json_repair fallback both remain — they fix related but distinct failure modes (truncation, unescaped chars).
  v3.38.4 (2026-05-17) — Both `haiku_triage_news` and `sonnet_analyze` now parse responses with a two-stage strategy: stdlib `json.loads(raw)` first (fast path for valid output), then `json_repair.loads(raw)` fallback on JSONDecodeError. Reason: the 15:32 UTC May 17 run had Haiku triage of 50 news items fail JSON parse at char 13926 (unterminated string — almost certainly unescaped quote/newline in a reasoning field), losing the entire triage. v3.38.3 had the same failure mode in Sonnet (char 27099) and Haiku (char 6839). Bumping max_tokens helps when the cause is truncation, but doesn't help when the cause is malformed string content — json_repair handles both. New explicit log at WARNING level when the fallback fires, with INFO-level confirmation of how much was recovered.
"""

from __future__ import annotations

import copy
import json
import logging
import math  # v3.39.7.9 — defensive NaN guard in COMPUTED METRICS rendering
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import json_repair  # v3.38.4 — fallback when stdlib json.loads fails on LLM output

from anthropic import Anthropic

from src.config import (
    HAIKU_MODEL,
    HAIKU_MAX_TOKENS,
    SONNET_MODEL,
    SONNET_MAX_TOKENS,
    LLM_TEMPERATURE,
    INDICATORS,
    TOP_5_INDICATORS,
    CYCLE_BASELINE,
    USE_BATCH_API,
    BATCH_POLL_CAP_SECONDS,
    USE_PROSE_CACHE,
    EARNINGS_LEAD_FRESH_DAYS,
    USE_STRUCTURED_TRIAGE,
)
from src.data.news import NewsItem
from src.data.estimates import EstimateSnapshot
from src.data.prices import PerformanceMetrics
from src.diagnostics import DiagnosticsCollector, SourceFetchStatus
from src.analysis.prose_cache import (
    load_prose_cache,
    save_prose_cache,
    seed_staggered_dates,
    evaluate_cache,
    apply_status_guard,
    apply_news_freshness_guard,
    get_cached_assessment_dict,
    summarize_decisions,
    update_cache_after_synthesis,
    split_regen_ids,
)

logger = logging.getLogger(__name__)

# v3.40.0 — id → Indicator lookup, used to fill static card fields
# (name/description/what_to_look_for) client-side instead of having Sonnet
# regenerate them in its output every run (trim a).
_INDICATOR_BY_ID = {ind.id: ind for ind in INDICATORS}


# ============================================================
# v3.41.1 — exec-summary / prose LEAK guard.
# ============================================================
# Sonnet was narrating framework MACHINERY into investor-facing prose — version
# tags ("per v3.39.7.6"), "operative state", "cluster", "requires resolution" —
# copied straight out of version-tagged prompt rule headers. The prompt now
# carries an OUTPUT HYGIENE instruction and the leaked headers are de-tagged,
# but those are best-effort; this render-time sanitizer is the DETERMINISTIC
# backstop applied to every prose field before it reaches the reader. It is
# deliberately conservative: it removes framework version tags and a small set
# of internal-mechanism phrases, and leaves everything else untouched.
import re as _re_leak

_VERSION_PAREN_RE = _re_leak.compile(r'\s*\((?:per\s+)?v\d+\.\d+(?:\.\d+)*[^)]*\)', _re_leak.I)
_VERSION_INLINE_RE = _re_leak.compile(r'[\s,;—-]*\b(?:per|see|cf\.?|under)\s+v\d+\.\d+(?:\.\d+)*\b', _re_leak.I)
_VERSION_BARE_RE = _re_leak.compile(r'\bv\d+\.\d+(?:\.\d+)*\b')
_JARGON_SUBS = [
    (_re_leak.compile(r'\bper the RECENCY ON CONTRADICTING EVENTS(?:\s+rule)?\b', _re_leak.I), ''),
    (_re_leak.compile(r'\boperative state of the world\b', _re_leak.I), 'current state'),
    (_re_leak.compile(r'\boperative state\b', _re_leak.I), 'current state'),
    (_re_leak.compile(r'\brequires resolution\b', _re_leak.I), 'remains unresolved'),
]


def _strip_internal_jargon(text):
    """Remove framework version tags + internal-mechanism vocabulary from a
    user-facing prose string. Non-strings pass through unchanged."""
    if not text or not isinstance(text, str):
        return text
    t = _VERSION_PAREN_RE.sub('', text)       # "(v3.39.7.6)", "(per v3.4.1 ...)"
    t = _VERSION_INLINE_RE.sub('', t)         # "... per v3.39.7.6"
    for rgx, repl in _JARGON_SUBS:
        t = rgx.sub(repl, t)
    t = _VERSION_BARE_RE.sub('', t)           # any leftover bare "v3.39.7.6"
    t = _re_leak.sub(r'\s{2,}', ' ', t)       # tidy doubled spaces
    t = _re_leak.sub(r'\s+([,.;:])', r'\1', t)  # tidy space-before-punct
    return t.strip()


# ============================================================
# Output dataclasses
# ============================================================

@dataclass
class IndicatorAssessment:
    """LLM-generated assessment of a single indicator's current status."""
    id: str
    tier: int
    name: str
    description: str
    what_to_look_for: str
    current_status: str       # "Fired" | "Partially fired" | "Not fired"  (v3.39.7.9: "Reversed" removed — audit trail in change_vs_prior)
    where_we_stand: str
    latest_developments: str
    relevant_links: list[dict[str, str]] = field(default_factory=list)
    change_vs_prior: Optional[str] = None


@dataclass
class CaseScenario:
    """Probability of a length-to-peak + price-to-peak scenario for SK Hynix.

    v3.39: renamed `upside_to_peak_pct` → `return_to_peak_pct`. The previous
    name implied a non-negative number, but the bear case legitimately needs
    to express a NEGATIVE value (peak already in → downside from current
    price). Now signed: positive = upside, zero = at peak, negative = downside.

    Framing:
      base — most probable scenario by invariant; signals continuing at
             current pace; peak shortens and return trims as signals fire
      bull — requires signals to un-fire or delay materially; longer peak,
             higher return (always positive)
      bear — requires faster degradation than expected; shorter peak (can
             be 0), can carry a NEGATIVE return when peak is already in
    """
    probability_pct: float
    peak_months_away: float
    peak_window_start: str  # e.g., "August 2026"
    peak_window_end: str
    return_to_peak_pct: float  # signed


@dataclass
class ExecutiveSummary:
    """The structured exec summary at the top of the email.

    v3.39.7.5 — `executive_brief` field added: a single consolidated
    5-8 sentence paragraph synthesizing today's signal changes, big
    news, big movers, and macro context. Replaces the three-block
    pattern (delta_from_prior_text / big_movers list / market_summary)
    that the May 20 brief surfaced as repetitive. Anchor links of the
    form `[→§N]` are emitted by Sonnet and substituted to HTML
    `<a href="#section-N">§N</a>` at template render time.

    The legacy fields (`delta_from_prior_text`, `urgent_attention_*`,
    and `market_summary` on BriefAnalysis) are KEPT but no longer
    rendered. Sonnet still populates them as JSON output for any
    future debugging/observability use; they're dead-letter fields
    from the email's perspective.
    """
    current_phase: int
    current_phase_label: str
    tier_status_summary: str
    new_triggers_today: list[dict[str, str]]
    base_case: CaseScenario
    bull_case: CaseScenario
    bear_case: CaseScenario
    delta_from_prior_text: str
    urgent_attention_needed: bool
    urgent_attention_text: str
    executive_brief: str = ""  # v3.39.7.5 — populated by Sonnet, rendered as the consolidated exec block


# ============================================================
# v3.39 — Scenario invariant enforcement
# ============================================================
# The Sonnet prompt asks for three scenarios (base / bull / bear) with
# probability_pct summing to 100 and base being the most probable. The LLM
# usually obeys but can drift on either dimension — e.g., the 17:43 May 17
# brief produced base 28% / bear 30% / bull 18% (sum 76%, bear > base). The
# enforcer below normalizes deterministically: probabilities sum to exactly
# 100, base is strictly the highest, return_to_peak_pct signs make sense.


def _tier_from_id(indicator_id: str) -> int:
    """Infer tier (1-4) from an indicator id of the form `tN_…`.

    v3.39.2: when Sonnet truncates mid-indicator and json_repair recovers
    a partial dict missing the `tier` field, defaulting tier to 0 caused
    the indicator to be silently dropped from the email — the template
    iterates `for tier in (1, 2, 3, 4)` and filters strictly. Since every
    indicator id is canonically prefixed `t1_…` through `t4_…`, we can
    almost always recover the tier from the id even when the explicit
    field is missing.

    Returns 0 only if the id doesn't conform to the prefix scheme; the
    caller defaults to that value (which still renders as a no-tier row
    visible nowhere, matching the prior behavior on truly malformed ids).
    """
    if (
        isinstance(indicator_id, str)
        and len(indicator_id) >= 2
        and indicator_id.startswith("t")
        and indicator_id[1].isdigit()
    ):
        n = int(indicator_id[1])
        if 1 <= n <= 4:
            return n
    return 0


def _compute_tier_status_summary(assessments: list) -> str:
    """Deterministically compute the `tier_status_summary` string from the
    parsed `indicator_assessments` list.

    v3.39.7.4 — Sonnet's self-counted `tier_status_summary` in the exec
    summary has historically diverged from the per-card statuses in the
    body of the same brief (e.g. May 20 2026 16:00 UTC brief: body showed
    4 Tier 1 indicators with `Fired` badges, but exec summary said
    `T1: 3/6 fired`; T2 had the symmetric problem where a `Partially fired`
    body card was counted as fired in the exec total `T2: 6/7`). Off-by-
    one arithmetic errors in the LLM's prose are not fixable by prompt
    tightening alone, so this helper recomputes the count post-parse from
    the structured assessments and overrides Sonnet's string.

    Counting convention (locked in 2026-05-20 with user):
    - `Fired` and `Partially fired` both count toward the tier total.
      Rationale: a Partially fired indicator has met the trigger criteria
      on at least one of multiple stitched dimensions, which is
      directionally the same as Fired. Treating partial as fired makes
      the tier count monotonic in "progress toward firing".
    - When ANY tier has Partially fired indicators (>0), append a
      "(N partial)" annotation so the reader knows the full-fire breakdown.
    - The denominator per tier is fixed: T1=6, T2=7, T3=5, T4=4.

    v3.4.7 (2026-05-23) — SECTION_4_ONLY_INDICATORS (DHCR + Competitor +
    ADR) are now EXCLUDED from the numerator. They were already excluded
    from the denominator (T1=6, not 9), but the numerator counted them.
    That mismatch produced the May 23 brief discrepancy: exec said
    "T1: 5/6 fired (1 partial)" while section 3 said "4/6 fired" —
    Competitor's partial-fire was counted in exec only.

    Output format examples:
      "T1: 3/6 fired | T2: 5/7 fired (1 partial) | T3: 0/5 fired | T4: 0/4 fired"
      "T1: 0/6 fired | T2: 0/7 fired | T3: 0/5 fired | T4: 0/4 fired"
    """
    # Import here to avoid module-load cycles (config imports from misc)
    from src.config import SECTION_4_ONLY_INDICATORS

    TIER_DENOMINATORS = {1: 6, 2: 7, 3: 5, 4: 4}
    counts = {1: [0, 0], 2: [0, 0], 3: [0, 0], 4: [0, 0]}  # [fired_total, partial_subset]
    for a in assessments:
        id_ = getattr(a, "id", "")
        # v3.4.7 — DHCR/Competitor/ADR are watch-only overlays and must
        # not contribute to the T1 firing count per Raul 2026-05-23.
        # Until their tier placement is settled, exclude entirely.
        if id_ in SECTION_4_ONLY_INDICATORS:
            continue
        tier = getattr(a, "tier", 0) or _tier_from_id(id_)
        if tier not in TIER_DENOMINATORS:
            continue
        status = (getattr(a, "current_status", "") or "").strip().lower()
        if status == "fired":
            counts[tier][0] += 1
        elif status.startswith("partial"):
            counts[tier][0] += 1
            counts[tier][1] += 1
    parts = []
    for tier in (1, 2, 3, 4):
        fired, partial = counts[tier]
        denom = TIER_DENOMINATORS[tier]
        seg = f"T{tier}: {fired}/{denom} fired"
        if partial > 0:
            seg += f" ({partial} partial)"
        parts.append(seg)
    return " | ".join(parts)


def _safe_indicator_assessment(a: dict) -> IndicatorAssessment:
    """Construct an IndicatorAssessment from a possibly-partial dict.

    Used after Sonnet output is parsed (or json_repair-recovered). When
    truncation hits mid-indicator, json_repair returns whatever it could
    salvage, often missing required fields like `latest_developments`.
    Prior code's `IndicatorAssessment(**a)` raised TypeError on missing
    fields and crashed the whole pipeline — see v3.39.1 fix notes.

    Every required field gets a safe default; `change_vs_prior` stays None
    if absent. The caller filters out dicts without an `id` upstream.

    v3.39.2: tier now falls back to `_tier_from_id(id)` before defaulting
    to 0. Previously a missing `tier` field meant the indicator was
    constructed cleanly but then filtered out by the template's
    `for tier in (1,2,3,4)` loop — defensive parsing produced a usable
    object that the renderer silently dropped. Inferring from the id
    prefix recovers the right tier in every realistic truncation case.
    """
    indicator_id = str(a.get("id", "unknown"))
    raw_tier = str(a.get("tier", "")).strip()
    if raw_tier.isdigit():
        tier = int(raw_tier)
    else:
        tier = _tier_from_id(indicator_id)
    # v3.40.0 trim (a) — fill the STATIC fields (description, what_to_look_for,
    # name) from config rather than from Sonnet's output. They're verbatim
    # copies of config every day; having Sonnet regenerate them wasted ~850
    # words of output/run. The prompt now tells Sonnet to omit them. Fall back
    # to Sonnet's value only if the id isn't in config (truncation / unknown id).
    _cfg = _INDICATOR_BY_ID.get(indicator_id)
    _name = (_cfg.name if _cfg else str(a.get("name", "")))
    _description = (_cfg.description if _cfg else str(a.get("description", "")))
    _what = (_cfg.what_to_look_for if _cfg else str(a.get("what_to_look_for", "")))
    # v3.40.0 trim (b) — cap relevant_links to 3 per card (was unbounded;
    # ~49 links/brief ≈ 3K output tokens). Keep the first 3 (Sonnet emits
    # most-relevant first).
    _links = a.get("relevant_links", [])
    if not isinstance(_links, list):
        _links = []
    _links = _links[:3]
    return IndicatorAssessment(
        id=indicator_id,
        tier=tier,
        name=_name,
        description=_description,
        what_to_look_for=_what,
        current_status=str(a.get("current_status", "Unknown (Sonnet output truncated)")),
        # v3.41.1 — strip framework version tags / mechanism vocab from prose
        where_we_stand=_strip_internal_jargon(str(a.get("where_we_stand", ""))),
        latest_developments=_strip_internal_jargon(str(a.get("latest_developments", ""))),
        relevant_links=_links,
        change_vs_prior=_strip_internal_jargon(a.get("change_vs_prior")) if a.get("change_vs_prior") else None,
    )


# ============================================================
# v3.41.2 — prose-cache live support: word-cap truncation, cache→assessment
# reconstruction, and the run-scope instruction injected into the prompt.
# ============================================================
_WORD_CAP_WHERE = 50          # where_we_stand, non-deep-dive (prompt: ≤50)
_WORD_CAP_WHERE_TOP5 = 90     # where_we_stand for the 5 deep-dive ids (prompt: ≤90)
_WORD_CAP_LATEST = 35         # latest_developments (prompt: ≤35)
_WORD_CAP_CHANGE = 40         # change_vs_prior (short status-delta note)


def _truncate_words(text: Optional[str], cap: int) -> Optional[str]:
    """Backstop hard word cap (Sonnet does not reliably honor the prompt caps).
    Cuts at a word boundary and appends an ellipsis only when over the cap.
    Pass-through for empty / non-string."""
    if not text or not isinstance(text, str):
        return text
    words = text.split()
    if len(words) <= cap:
        return text
    return " ".join(words[:cap]).rstrip(",;:") + "…"


def _truncate_assessment_prose(a: IndicatorAssessment) -> IndicatorAssessment:
    """Enforce per-field word caps on a FRESH (Sonnet-written) card. Cached cards
    were already capped when first written, so they are not re-truncated."""
    where_cap = _WORD_CAP_WHERE_TOP5 if a.id in TOP_5_INDICATORS else _WORD_CAP_WHERE
    a.where_we_stand = _truncate_words(a.where_we_stand, where_cap)
    a.latest_developments = _truncate_words(a.latest_developments, _WORD_CAP_LATEST)
    if a.change_vs_prior:
        a.change_vs_prior = _truncate_words(a.change_vs_prior, _WORD_CAP_CHANGE)
    return a


def _assessment_from_cache(ind_id: str, cached: dict) -> IndicatorAssessment:
    """Reconstruct an IndicatorAssessment from cached prose fields, sourcing the
    static fields (name/description/what_to_look_for/tier) from config — exactly
    as _safe_indicator_assessment does for fresh cards. Used for dormant cards
    that Sonnet did not regenerate this run."""
    cfg = _INDICATOR_BY_ID.get(ind_id)
    links = cached.get("relevant_links") or []
    if not isinstance(links, list):
        links = []
    return IndicatorAssessment(
        id=ind_id,
        tier=(cfg.tier if cfg else _tier_from_id(ind_id)),
        name=(cfg.name if cfg else ind_id),
        description=(cfg.description if cfg else ""),
        what_to_look_for=(cfg.what_to_look_for if cfg else ""),
        current_status=str(cached.get("current_status", "")),
        where_we_stand=str(cached.get("where_we_stand", "")),
        latest_developments=str(cached.get("latest_developments", "") or ""),
        relevant_links=links[:3],
        change_vs_prior=cached.get("change_vs_prior") or None,
    )


def _build_scope_instruction(regen_ids) -> str:
    """The run-scope block injected into the prompt when the prose cache restricts
    output to a subset of indicators. Empty string => no restriction (write all)."""
    ids = sorted(regen_ids)
    if not ids:
        return (
            "RUN SCOPE — indicator_assessments (IMPORTANT):\n"
            "Every indicator is unchanged since the last brief and its prose is "
            "supplied from cache. Emit an EMPTY indicator_assessments array ([]) — "
            "do NOT write any indicator cards. You MUST still produce the "
            "executive_brief and all executive_summary fields, plus market_summary, "
            "key_news_summary and cycle_position_call, reflecting the full picture "
            "across every indicator and all of today's news.\n\n"
        )
    id_list = ", ".join(ids)
    return (
        "RUN SCOPE — indicator_assessments (IMPORTANT):\n"
        f"Only the following {len(ids)} indicator(s) changed since the last brief "
        "and need fresh prose. Emit indicator_assessments[] entries for THESE IDS ONLY:\n"
        f"  {id_list}\n"
        "OMIT every other indicator entirely — do NOT emit a card, placeholder, or "
        "empty entry for them. They are unchanged and their prose is supplied from "
        "cache; emitting them wastes output and will be discarded. This restriction "
        "applies ONLY to which indicator_assessments[] cards you write — the "
        "executive_brief, market_summary, key_news_summary, cycle_position_call and "
        "all executive_summary fields MUST still reflect the full picture across every "
        "indicator and all of today's news.\n\n"
    )


def _merge_prose_cards(reasonings, fresh_assessments, reuse_ids, cache):
    """Assemble the full indicator card set from a scoped Sonnet run:
      - dormant ids in `reuse_ids` → reconstructed from cache,
      - changed/due ids → the fresh Sonnet card, word-capped,
      - a regen id Sonnet dropped → cached prose as a stopgap (never vanishes),
      - any fresh card whose id isn't a Layer-2 indicator → kept as-is (capped).
    Pure function over its inputs. Returns (merged_list, n_stopgap)."""
    fresh_by_id = {a.id: a for a in fresh_assessments}
    merged: list = []
    handled: set = set()
    n_stopgap = 0
    for r in reasonings:
        ind_id = getattr(r, "signal_id", None)
        if not ind_id:
            continue
        handled.add(ind_id)
        if ind_id in reuse_ids:
            cached = get_cached_assessment_dict(cache, ind_id)
            if cached:
                merged.append(_assessment_from_cache(ind_id, cached))
                continue
        if ind_id in fresh_by_id:
            merged.append(_truncate_assessment_prose(fresh_by_id[ind_id]))
        else:
            cached = get_cached_assessment_dict(cache, ind_id)
            if cached:
                merged.append(_assessment_from_cache(ind_id, cached))
                n_stopgap += 1
            # else: absent everywhere → drop (legacy truncation behavior)
    for a in fresh_assessments:
        if a.id and a.id not in handled:
            merged.append(_truncate_assessment_prose(a))
    return merged, n_stopgap


def _prune_prior_state_prose(prior_state: dict) -> dict:
    """Return a copy of prior_state with free-text NARRATIVE fields removed.

    v3.41.5. prior_state (yesterday's brief) is fed to Sonnet for change
    DETECTION — which indicators changed status, the phase, base-case peak
    window, scenario probabilities. All of that is STRUCTURED. Its prose
    fields (executive_brief, per-card where_we_stand/latest_developments/
    change_vs_prior) were ALSO dumped verbatim, which let Sonnet echo a
    resolved story forward in today's exec brief — the weeks-old Samsung
    strike kept leading the summary because yesterday's paragraph was handed
    back as context every run, a self-perpetuating loop with no aging control.
    Stripping the prose removes the echo fuel while leaving change-tracking
    fully intact (the structured status fields the deterministic diff and the
    peak-window/probability comparisons read are untouched). The exec brief is
    regenerated from today's news + verdicts every run anyway, so it loses
    nothing it legitimately needs.
    """
    if not isinstance(prior_state, dict):
        return prior_state
    pruned = copy.deepcopy(prior_state)
    es = pruned.get("executive_summary")
    if isinstance(es, dict):
        for k in ("executive_brief", "delta_from_prior_text", "urgent_attention_text"):
            es.pop(k, None)
    cards = pruned.get("indicator_assessments")
    if isinstance(cards, list):
        for c in cards:
            if isinstance(c, dict):
                for k in ("where_we_stand", "latest_developments",
                          "change_vs_prior", "relevant_links"):
                    c.pop(k, None)
    return pruned


def _build_known_earnings_events_block(
    event_log: Optional[dict],
    today=None,
    fresh_days: int = EARNINGS_LEAD_FRESH_DAYS,
    lookback_days: int = 14,
) -> str:
    """Render recent earnings events from state/event_log.json for the Sonnet prompt.

    v3.41.9 — the coverage-echo guard's data feed. The exec brief kept leading
    with Micron's Q3 print for a full week: every day's feed carried fresh-DATED
    articles about the same week-old event (analyst notes, recaps, CEO-interview
    write-ups), each of which satisfied the v3.41.5 "fresh same-day news"
    exception by publish date. Sonnet had no way to know the print was one
    event, already featured, N echoes deep — the v3.5.0 event log knows exactly
    that but fed only t2_eps_revisions. This block surfaces it: one line per
    recent earnings event with its date, AGE, echo count, and a FRESH/STALE tag
    (STALE once age > fresh_days), plus a divergence note when a later write-up
    contradicts the initial read (a verdict reversal is a NEW fact and
    lead-eligible). The EARNINGS ECHO prompt rule tells Sonnet how to act on it.

    Earnings events only (keys `<ticker>:earnings_call:<quarter>`), last
    `lookback_days` days, newest first. Returns "" when there is nothing to
    show — the block simply doesn't appear on quiet stretches.
    """
    from datetime import date as _date

    if not event_log or not isinstance(event_log, dict):
        return ""
    if today is None:
        today = _date.today()

    rows = []
    for key, entry in event_log.items():
        if ":earnings_call:" not in key or not isinstance(entry, dict):
            continue
        date_iso = entry.get("event_date_iso") or ""
        try:
            event_date = _date.fromisoformat(str(date_iso)[:10])
        except (ValueError, TypeError):
            continue  # unparsable date — can't age it, skip
        age = (today - event_date).days
        if age < 0 or age > lookback_days:
            continue
        ticker = key.split(":", 1)[0]
        n_obs = entry.get("n_observations", 1)
        rows.append((event_date, age, ticker, key.rsplit(":", 1)[-1], n_obs,
                     bool(entry.get("divergence_flagged"))))

    if not rows:
        return ""

    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [
        "\nEARNINGS EVENTS ALREADY IN THE RECORD (coverage-echo guard — see the "
        "EARNINGS ECHO rule):"
    ]
    for event_date, age, ticker, quarter, n_obs, diverged in rows:
        day_word = "day" if age == 1 else "days"
        tag = (
            "FRESH — within the lead window; may lead."
            if age <= fresh_days
            else "STALE — past the lead window; do NOT lead absent a genuinely "
                 "new attributable fact (EARNINGS ECHO rule)."
        )
        lines.append(
            f"  - {ticker} earnings {event_date.isoformat()} ({quarter}) — "
            f"{age} {day_word} ago, {n_obs} write-up{'s' if n_obs != 1 else ''} "
            f"logged. {tag}"
        )
        if diverged:
            lines.append(
                "    DIVERGENCE FLAGGED: a later write-up contradicts the "
                "initial read — a verdict reversal is a NEW fact and is "
                "lead-eligible."
            )
    return "\n".join(lines) + "\n"


def _build_case_scenario(d: dict | None) -> CaseScenario:
    """Construct a CaseScenario from a dict, handling both the new field
    name (return_to_peak_pct) and the pre-v3.39 name (upside_to_peak_pct).
    Used when loading prior_state.json which may have older format, and
    when the LLM occasionally regresses to the older key name.
    """
    d = dict(d or {})
    if "return_to_peak_pct" not in d and "upside_to_peak_pct" in d:
        d["return_to_peak_pct"] = d.pop("upside_to_peak_pct")
    return CaseScenario(
        probability_pct=float(d.get("probability_pct", 0)),
        peak_months_away=float(d.get("peak_months_away", 0)),
        peak_window_start=str(d.get("peak_window_start", "")),
        peak_window_end=str(d.get("peak_window_end", "")),
        return_to_peak_pct=float(d.get("return_to_peak_pct", 0)),
    )


def _enforce_scenario_invariants(
    base: CaseScenario, bull: CaseScenario, bear: CaseScenario,
    current_phase: int = 4,
) -> tuple[CaseScenario, CaseScenario, CaseScenario]:
    """Apply hard invariants to the three scenarios:
      1. Probabilities are integers in [1, 98] summing to exactly 100.
      2. base.probability_pct is the strict maximum (> bear, > bull).
      3a. PHASE 4: base.return_to_peak_pct >= 0 (peak ahead of us).
      3b. PHASE 5: base.return_to_peak_pct <= 0 (decline ahead of us;
          the field is reinterpreted as 'return to trough', signed).
      4. PHASE 4: bull.return_to_peak_pct > base.return_to_peak_pct
         (higher upside in bull).
         PHASE 5: bull.return_to_peak_pct > base.return_to_peak_pct
         (shallower decline in bull — i.e., closer to zero / less negative).
      5. PHASE 4: bear.return_to_peak_pct <= base.return_to_peak_pct
         (lower upside / negative downside in bear).
         PHASE 5: bear.return_to_peak_pct < base.return_to_peak_pct
         (deeper trough in bear — more negative).

    v3.4.11 — Phase 4 and Phase 5 share the same monotonicity (bull > base
    > bear in signed return). What flips is the sign of base: Phase 4
    base >= 0, Phase 5 base <= 0. The bull-above-base, bear-below-base
    ordering is preserved in both phases.

    Algorithm (probabilities):
      a. Floor each at 1, then proportionally normalize sum to 100.
      b. For any scenario whose probability >= base, cap it at base-1 and
         redistribute the excess back into base (preserves sum=100, makes
         base strictly the largest).
      c. Round to integers; if rounding misses 100, adjust base for the
         residual. Final loop guards against rounding flipping the inequality.
    """
    # --- Probability invariants ---
    bp = max(1.0, float(base.probability_pct))
    blp = max(1.0, float(bull.probability_pct))
    brp = max(1.0, float(bear.probability_pct))

    # Step a: proportional normalize to sum=100
    total = bp + blp + brp
    bp = bp * 100.0 / total
    blp = blp * 100.0 / total
    brp = brp * 100.0 / total

    # Step b: cap bull/bear at base-1; redistribute excess into base
    if blp >= bp:
        excess = blp - (bp - 1.0)
        blp = bp - 1.0
        bp += excess
    if brp >= bp:
        excess = brp - (bp - 1.0)
        brp = bp - 1.0
        bp += excess
    # After redistribution, re-floor in case capping pushed below 1
    blp = max(1.0, blp)
    brp = max(1.0, brp)
    # Renormalize if floors changed the sum (very rare edge case)
    total = bp + blp + brp
    if total != 100.0:
        # Absorb residual into base (which is now strictly largest, so
        # this won't break the strict-max invariant)
        bp = 100.0 - blp - brp

    # Step c: round to integers, fix residual
    b_i = int(round(bp))
    bl_i = int(round(blp))
    br_i = int(round(brp))
    diff = 100 - (b_i + bl_i + br_i)
    b_i += diff
    # Final pp-by-pp loop in case rounding flipped strict inequalities
    while b_i <= bl_i:
        b_i += 1
        bl_i -= 1
    while b_i <= br_i:
        b_i += 1
        br_i -= 1
    while bl_i < 1:
        bl_i += 1
        b_i -= 1
    while br_i < 1:
        br_i += 1
        b_i -= 1

    # --- Return-to-peak/trough invariants ---
    base_r = float(base.return_to_peak_pct)
    bull_r = float(bull.return_to_peak_pct)
    bear_r = float(bear.return_to_peak_pct)

    if int(current_phase) >= 5:
        # PHASE 5 — "return_to_peak_pct" is reinterpreted as
        # "return_to_trough_pct". Base case projects a decline (≤ 0).
        # Bull = shallower decline (closer to zero / less negative).
        # Bear = deeper decline (more negative).
        base_r = min(0.0, base_r)
        # Bull is shallower (less negative / more positive) than base
        if bull_r <= base_r:
            bull_r = base_r + 5.0
        # Bear is deeper (more negative) than base
        if bear_r >= base_r:
            bear_r = base_r - 5.0
    else:
        # PHASE 4 (default) — original invariants.
        # Base never assumes peak is past
        base_r = max(0.0, base_r)
        # Bull strictly above base (always positive upside)
        if bull_r <= base_r:
            bull_r = base_r + 5.0
        # Bear no higher than base (can be negative when peak is in)
        if bear_r > base_r:
            bear_r = base_r

    return (
        CaseScenario(
            probability_pct=float(b_i),
            peak_months_away=base.peak_months_away,
            peak_window_start=base.peak_window_start,
            peak_window_end=base.peak_window_end,
            return_to_peak_pct=base_r,
        ),
        CaseScenario(
            probability_pct=float(bl_i),
            peak_months_away=bull.peak_months_away,
            peak_window_start=bull.peak_window_start,
            peak_window_end=bull.peak_window_end,
            return_to_peak_pct=bull_r,
        ),
        CaseScenario(
            probability_pct=float(br_i),
            peak_months_away=bear.peak_months_away,
            peak_window_start=bear.peak_window_start,
            peak_window_end=bear.peak_window_end,
            return_to_peak_pct=bear_r,
        ),
    )


@dataclass
class BigMover:
    """A ticker with >4% move in latest session."""
    name: str
    ticker: str
    change_1d_pct: float
    direction: str  # "up" | "down"


@dataclass
class BrokerAction:
    """A broker action extracted from news."""
    ticker: str
    broker: str
    action: str
    detail: str
    source_url: str


def _safe_construct_list(cls, items: list, source_name: str) -> list:
    """v3.4.8 — Safely construct a list of dataclass instances from
    Haiku-parsed dicts, skipping entries with missing or invalid fields.

    Why this exists: when Haiku's response exceeds its output token cap,
    json.loads fails on truncated text. v3.38.4 added json_repair as a
    fallback to recover what's parseable from the partial response. But
    a recovered partial response can contain dicts that are missing
    fields that the target dataclass requires (e.g. a broker_action
    dict missing `source_url` because the truncation happened mid-key).
    Constructing the dataclass with `cls(**partial_dict)` then raises
    TypeError, blowing up the entire pipeline.

    This helper unpacks each entry individually, logs a WARNING on any
    construction failure (with the specific field error from Python's
    TypeError message), and continues. The pipeline degrades gracefully:
    you lose a few broker_actions instead of the entire daily brief.

    Non-dict entries (e.g. if json_repair returned a string in place of
    an object) are also skipped with a separate log line.

    The 2026-05-23 production crash that motivated this:
      TypeError: BrokerAction.__init__() missing 1 required
                 positional argument: 'source_url'
    """
    result = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            logger.warning(
                f"Skipped non-dict {cls.__name__} entry at {source_name}[{i}]: "
                f"got {type(item).__name__} ({item!r:.80})"
            )
            continue
        try:
            result.append(cls(**item))
        except TypeError as e:
            logger.warning(
                f"Skipped malformed {cls.__name__} at {source_name}[{i}]: {e}. "
                f"Likely json_repair recovery of truncated Haiku output. "
                f"Available keys: {sorted(item.keys())}"
            )
    if result and len(result) < len(items):
        logger.info(
            f"_safe_construct_list({cls.__name__}, {source_name}): "
            f"kept {len(result)}/{len(items)} entries"
        )
    return result


@dataclass
class NewsTriage:
    """Output of Haiku stage 1: structured news pre-processing."""
    relevant_news: list[dict]
    broker_actions: list[BrokerAction]
    trendforce_commentary: list[dict]
    indicator_relevance: dict[str, list[int]]


@dataclass
class BriefAnalysis:
    """Complete analytical output."""
    executive_summary: ExecutiveSummary
    big_movers: list[BigMover]
    indicator_assessments: list[IndicatorAssessment]
    top_5_deep_dives: list[IndicatorAssessment]
    market_summary: str
    key_news_summary: list[dict[str, str]]
    cycle_position_call: str


# ============================================================
# Stage 1: Haiku news pre-processing
# ============================================================

# v3.41.11 — JSON Schema for the structured-outputs triage call. Passed as
# output_config.format so the API's grammar-constrained decoder can only emit
# schema-valid JSON: the unescaped-quote/raw-newline class that broke triage
# at chars 6839, 13926, and 28384 becomes impossible at the sampler level.
# Design constraints (per structured-outputs docs): additionalProperties
# false everywhere, fixed keys only, no recursion. Note there is NO
# `indicator_relevance` map here — dynamic keys don't fit a fixed schema, and
# the map died to truncation on Jul 9 precisely because it trailed the
# output. Routing instead travels INSIDE each item (`indicators` array), so
# every item that arrives carries its own routing; the aggregate map is
# derived in code by _merge_indicator_relevance.
TRIAGE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relevant_news": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "index": {"type": "integer"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "source": {"type": "string"},
                    "summary": {"type": "string"},
                    "indicators": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "title", "url", "source", "summary", "indicators"],
            },
        },
        "broker_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticker": {"type": "string"},
                    "broker": {"type": "string"},
                    "action": {"type": "string"},
                    "detail": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["ticker", "broker", "action", "detail", "source_url"],
            },
        },
        "trendforce_commentary": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product": {"type": "string"},
                    "price_level": {"type": "string"},
                    "move": {"type": "string"},
                    "language": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["product", "price_level", "move", "language", "source_url"],
            },
        },
    },
    "required": ["relevant_news", "broker_actions", "trendforce_commentary"],
}


def _merge_indicator_relevance(parsed: dict) -> tuple[dict, int]:
    """Build the indicator→news-indices map from BOTH sources.

    v3.41.11 — routing now travels inside each relevant_news item as an
    `indicators` array (survives partial output item-by-item; enforced by the
    structured schema). The legacy free-form path may still also emit the
    aggregate `indicator_relevance` map. This merges the two — map first,
    item tags unioned in — so every downstream consumer (Sonnet prompt,
    Section-4 card attach) keeps reading one map regardless of which call
    path produced the triage. Returns (merged_map, n_item_tags_merged).
    """
    merged: dict[str, list[int]] = {}
    for ind, idxs in (parsed.get("indicator_relevance") or {}).items():
        if isinstance(ind, str) and isinstance(idxs, list):
            merged[ind] = [i for i in idxs if isinstance(i, int)]
    n_tags = 0
    for item in (parsed.get("relevant_news") or []):
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        for ind in (item.get("indicators") or []):
            if not isinstance(ind, str) or not ind:
                continue
            bucket = merged.setdefault(ind, [])
            if isinstance(idx, int) and idx not in bucket:
                bucket.append(idx)
                n_tags += 1
    return merged, n_tags


def build_haiku_triage_prompt(news_items: list[NewsItem]) -> str:
    """Prompt Haiku to triage and categorize the news feed."""

    indicator_summary = "\n".join(
        f"- [{ind.id}] T{ind.tier}: {ind.name} (looks for: {ind.what_to_look_for[:100]})"
        for ind in INDICATORS
    )

    news_text = ""
    for i, n in enumerate(news_items[:120]):
        pub = n.published.strftime("%Y-%m-%d %H:%M") if n.published else "?"
        news_text += f"[{i}] ({n.source} | {pub}) {n.title}\n"
        if n.summary:
            news_text += f"     Summary: {n.summary[:300]}\n"
        news_text += f"     URL: {n.url}\n\n"

    prompt = f"""You are pre-processing a news feed for a memory semiconductor cycle tracker. Extract structured information from these news items so a senior analyst can quickly synthesize the cycle status.

INDICATORS THE ANALYST CARES ABOUT (be specific about which apply):
{indicator_summary}

YOUR TASKS:
1. Filter: Identify the news items relevant to memory/semiconductor cycle (SK Hynix, Micron, Samsung, ASML, AMAT, NVIDIA, Broadcom, TSMC, hyperscaler CapEx, DRAM/NAND/HBM prices, AI inference efficiency). Discard everything else.
2. Broker actions: Extract any analyst broker actions (upgrades, downgrades, target price changes, estimate revisions). Action types: "upgrade", "downgrade", "target_raised", "target_cut", "estimate_raised", "estimate_cut".
3. TrendForce / price commentary: Extract any DRAM, NAND, HBM, or memory pricing commentary. Pull out specific price levels, percentage moves, and demand language ("buyers cautious", "price cuts not stimulating demand", etc).
4. Indicator mapping: tag EVERY relevant news item inline via its `indicators` array with the indicator IDs it informs (use the exact IDs from the list above; empty array if none apply). Also return the aggregate `indicator_relevance` map.

OUTPUT FORMAT — STRICT REQUIREMENTS:
- Your entire response must be a single valid JSON object.
- Start your response with the opening `{{` character. End with the closing `}}`.
- Do NOT wrap the JSON in markdown code fences (```).
- Do NOT include any prose before or after the JSON.
- Do NOT include comments, explanations, or apologies.
- If you cannot triage some items, still return valid JSON — leave problematic items out of `relevant_news` rather than describing the problem in prose.

JSON SCHEMA:
{{
  "indicator_relevance": {{
    "t1_spot_decline": [5, 12, 23],
    "t2_eps_revisions": [3, 17],
    "t1_us_adr_event": [7]
  }},
  "relevant_news": [
    {{"index": 5, "title": "...", "url": "...", "source": "...", "summary": "1-line takeaway", "indicators": ["t1_spot_decline"]}}
  ],
  "broker_actions": [
    {{"ticker": "MU", "broker": "Mizuho", "action": "target_raised", "detail": "$545 -> $740", "source_url": "..."}}
  ],
  "trendforce_commentary": [
    {{"product": "DDR5 16Gb spot", "price_level": "$39", "move": "+0.34%", "language": "...", "source_url": "..."}}
  ]
}}

Be aggressive about filtering. If an item is not directly about memory/semi cycle, discard it.

NEWS ITEMS TO TRIAGE:
{news_text}
"""
    return prompt


def haiku_triage_news(
    api_key: str,
    news_items: list[NewsItem],
    collector: Optional[DiagnosticsCollector] = None,
) -> Optional[NewsTriage]:
    """Run Haiku to pre-process the news feed."""
    if not api_key:
        if collector is not None:
            collector.record_skipped(
                f"Anthropic API / {HAIKU_MODEL} (stage 1 triage)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
        return None

    client = Anthropic(api_key=api_key)
    prompt = build_haiku_triage_prompt(news_items)

    # v3.41.11 — structured-outputs call. output_config.format compiles
    # TRIAGE_JSON_SCHEMA into a grammar that constrains decoding: the model
    # cannot emit JSON-breaking characters (the char-6839/13926/28384 class).
    # Passed via extra_body so any SDK >=0.39 works. If the API rejects the
    # parameter or the call errors, fall back to the legacy free-form call
    # within the same run — the brief never depends on the new path.
    triage_path = "legacy"

    def _create(structured: bool):
        kwargs = dict(
            model=HAIKU_MODEL,
            max_tokens=HAIKU_MAX_TOKENS,
            temperature=LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        if structured:
            kwargs["extra_body"] = {
                "output_config": {
                    "format": {"type": "json_schema", "schema": TRIAGE_JSON_SCHEMA}
                }
            }
        return client.messages.create(**kwargs)

    def _call_with_fallback():
        if USE_STRUCTURED_TRIAGE:
            try:
                return _create(True), "structured"
            except Exception as e:
                logger.warning(
                    f"[triage] structured-output call failed ({e}); "
                    f"falling back to legacy free-form call"
                )
        return _create(False), "legacy"

    response = None
    try:
        if collector is not None:
            with collector.track(
                f"Anthropic API / {HAIKU_MODEL} (stage 1 triage)",
                "llm",
            ) as t:
                response, triage_path = _call_with_fallback()
                # items_returned = #news items the model successfully classified
                # (estimated from input volume; actual count set after parse)
                t.items_returned = len(news_items)
                t.detail = f"input: {len(news_items)} news items ({triage_path})"
        else:
            response, triage_path = _call_with_fallback()
    except Exception as e:
        logger.error(f"Haiku triage call failed: {e}")
        return None

    stop_reason = getattr(response, "stop_reason", None)
    logger.info(f"[triage] path={triage_path} stop_reason={stop_reason}")
    if stop_reason == "max_tokens":
        logger.warning(
            "[triage] response hit max_tokens — output truncated by the length "
            "cap (structured mode guarantees format, not completeness); "
            "item-level `indicators` tags preserve routing for every item "
            "that did arrive"
        )

    raw = _strip_code_fence(response.content[0].text)
    # v3.38.4: two-stage parse. Stdlib json first (fast path for valid output);
    # json_repair fallback for the cases that broke runs at char 6839, 13926,
    # 27099. json_repair recovers from unescaped quotes/newlines in string
    # fields AND from truncation (returns best-effort dict for as much as
    # parsed). If even json_repair returns empty/non-dict, log and bail.
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            f"Haiku JSON parse failed at {e.msg} (char {e.pos}); "
            f"attempting json_repair recovery..."
        )
        try:
            parsed = json_repair.loads(raw)
            if not isinstance(parsed, dict):
                logger.error(
                    f"json_repair returned {type(parsed).__name__}, not dict — "
                    f"Haiku triage unrecoverable"
                )
                return None
            n_recovered = len(parsed.get("relevant_news", []))
            logger.info(
                f"json_repair recovered Haiku triage: {n_recovered} relevant_news items"
            )
        except Exception as repair_err:
            logger.error(
                f"json_repair also failed: {repair_err}; Haiku triage unrecoverable"
            )
            return None

    _relevance_merged = _log_triage_relevance(parsed, triage_path)

    return NewsTriage(
        relevant_news=parsed.get("relevant_news", []),
        # v3.4.8 — Defensive BrokerAction construction. The previous
        # comprehension `[BrokerAction(**a) for a in ...]` crashed with
        # TypeError when json_repair recovered a truncated Haiku output
        # that had a broker_action entry missing the required
        # `source_url` field. Production crash on 2026-05-23 was
        # exactly this. Now we skip-with-warning on bad entries.
        broker_actions=_safe_construct_list(
            BrokerAction, parsed.get("broker_actions", []), "broker_actions"
        ),
        trendforce_commentary=parsed.get("trendforce_commentary", []),
        # v3.41.11 — map ∪ item-level `indicators` tags. The structured path
        # emits only item tags (no dynamic-key map in the schema); the legacy
        # path may emit both. Merging here keeps every downstream consumer
        # on one map.
        indicator_relevance=_relevance_merged,
    )


def _log_triage_relevance(parsed: dict, triage_path: str) -> dict:
    merged, n_tags = _merge_indicator_relevance(parsed)
    n_mappings = sum(len(v) for v in merged.values())
    logger.info(
        f"[triage] indicator_relevance: {len(merged)} indicators / "
        f"{n_mappings} mappings (item-tags merged: {n_tags}, path={triage_path})"
    )
    return merged


# ============================================================
# Stage 2: Sonnet analysis + exec summary
# ============================================================

def build_sonnet_analysis_prompt(
    triage: NewsTriage,
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    big_movers: list[BigMover],
    prior_state: dict,
    digests: Optional[list[dict]] = None,
    signal_reasonings: Optional[list] = None,
    computed_metrics: Optional[dict] = None,
    news_items: Optional[list[NewsItem]] = None,
    phase_state: Optional[dict] = None,
    transcript_diffs: Optional[list[dict]] = None,
    authoritative_24h_log: Optional[list[dict]] = None,
    event_log: Optional[dict] = None,
) -> str:
    """Construct the Sonnet analysis prompt using Haiku-triaged data.

    v3.39.5: New `digests` parameter. Threads Layer 1 deep-tier broker note
    and transcript digests into Sonnet's prompt as a dedicated section.
    Closes the architectural void where Mirae note bodies were written to
    state/digests/ but never reached Sonnet — Sonnet was citing Korean news
    aggregator coverage of Mirae's W3.2M raise instead of Mirae directly.

    v3.39.6: New `signal_reasonings` and `computed_metrics` parameters.
    Threads Layer 2 per-signal Haiku verdicts AND the numerical computed
    metrics (relative strength 5-day spreads, hyperscaler CapEx ratios,
    forward P/E, DART materials) into Sonnet's prompt. Closes the void
    where Sonnet was speculating about signals it had no numbers for —
    e.g., May 18 brief's t3_relative_strength prose hallucinated "almost
    certainly past -10% threshold" from single-day news headlines when
    the actual 5-day computed spread was below threshold. Sonnet now sees
    the verdict directly and is instructed to LEAD with the numbers and
    keep commentary to ≤1/3 of indicator text.

    v3.39.7.6: New `news_items` parameter. The Haiku triage output
    (`relevant_news`) carries source/title/url/summary but NOT the
    published timestamp — and without timestamps Sonnet cannot apply
    the RECENCY ON CONTRADICTING EVENTS rule. When `news_items` is
    passed in, we build a URL→published lookup and stamp each triage
    item with its timestamp in the prompt. The May 21 2026 brief
    surfaced this when Sonnet wrote about a Samsung strike "commencing
    May 21" as a live event despite a later Korea Herald article in
    the same feed reporting the strike was called off one hour before
    launch — Sonnet had both items but no way to tell which was later.
    """

    # v3.39.7.6 — URL → published-timestamp lookup. Joins the original
    # NewsItem objects back to Haiku's triaged subset. URL is the join
    # key (Haiku doesn't mangle URLs in its output schema). Falls back
    # to title-match for items where URL is missing or differs.
    published_by_url: dict[str, str] = {}
    published_by_title: dict[str, str] = {}
    if news_items:
        for n in news_items:
            if n.published:
                ts = n.published.strftime("%Y-%m-%d %H:%M UTC")
                if n.url:
                    published_by_url[n.url] = ts
                if n.title:
                    published_by_title[n.title] = ts

    perf_text = "PERFORMANCE DATA (Yahoo Finance):\n"
    for p in performance_data:
        perf_text += (
            f"- {p.name} ({p.ticker}): {p.current_price:.2f} {p.currency} | "
            f"1D {_fmt_pct(p.change_1d)} | 1W {_fmt_pct(p.change_1w)} | "
            f"1M {_fmt_pct(p.change_1m)} | 3M {_fmt_pct(p.change_3m)} | "
            f"6M {_fmt_pct(p.change_6m)} | 1Y {_fmt_pct(p.change_1y)}\n"
        )

    est_text = "\nANALYST ESTIMATES (FY+1 = ~FY27, FY+2 = ~FY28):\n"
    for name, e in estimates_data.items():
        est_text += (
            f"- {name} ({e.ticker}): "
            f"FY+1 EPS est: {e.fy_next_eps_estimate} | "
            f"FY+2 EPS est: {e.fy_next2_eps_estimate} | "
            f"# analysts: {e.num_analysts} | "
            f"Fwd P/E: {e.forward_pe} | "
            f"Avg target: {e.avg_target_price}"
        )
        if e.upgrades_30d is not None or e.downgrades_30d is not None:
            est_text += f" | Up30d: {e.upgrades_30d} | Down30d: {e.downgrades_30d}"
        est_text += "\n"

    triage_text = f"\nNEWS TRIAGE OUTPUT (pre-processed by Haiku):\n"
    triage_text += f"\nRelevant news items ({len(triage.relevant_news)}):\n"
    triage_text += (
        "Each item shows its PUBLISHED TIMESTAMP. When two items reference the\n"
        "same event with contradictory direction, the LATER timestamp is the\n"
        "current state of the story; treat earlier items as background.\n"
    )
    for item in triage.relevant_news[:50]:
        url = item.get("url", "")
        title = item.get("title", "")
        # v3.39.7.6 — published timestamp lookup. URL match first (stable
        # join key); title match second; "?" if both miss.
        pub_str = (
            published_by_url.get(url)
            or published_by_title.get(title)
            or "?"
        )
        triage_text += f"- [{item.get('source', '?')} | published {pub_str}] {title}\n"
        triage_text += f"  Takeaway: {item.get('summary', '')}\n"
        triage_text += f"  URL: {url}\n"

    triage_text += f"\nBroker actions detected ({len(triage.broker_actions)}):\n"
    for ba in triage.broker_actions[:30]:
        triage_text += f"- {ba.broker} | {ba.ticker} | {ba.action} | {ba.detail}\n"

    triage_text += f"\nTrendForce / price commentary ({len(triage.trendforce_commentary)}):\n"
    for tc in triage.trendforce_commentary[:20]:
        triage_text += f"- {tc.get('product', '')}: {tc.get('price_level', '')} ({tc.get('move', '')}) - {tc.get('language', '')}\n"

    triage_text += "\nIndicator relevance map:\n"
    for ind_id, indices in triage.indicator_relevance.items():
        triage_text += f"- {ind_id}: news indices {indices}\n"

    movers_text = "\nBIG MOVERS today (>+/-4% in 1 session):\n"
    if big_movers:
        for m in big_movers:
            movers_text += f"- {m.name} ({m.ticker}): {m.change_1d_pct:+.2f}%\n"
    else:
        movers_text += "(none)\n"

    indicator_text = "\nINDICATOR FRAMEWORK (assess each):\n"
    for ind in INDICATORS:
        indicator_text += (
            f"\n[{ind.id}] Tier {ind.tier}: {ind.name}\n"
            f"  Description: {ind.description}\n"
            f"  Trigger: {ind.what_to_look_for}\n"
        )

    prior_text = "\nPRIOR STATE (yesterday's brief — STRUCTURED fields only, for change detection):\n"
    if prior_state:
        prior_text += json.dumps(_prune_prior_state_prose(prior_state), indent=2, default=str)
    else:
        prior_text += "(no prior state - this is the first run)\n"

    # v3.41.9 — earnings coverage-echo guard: surface the v3.5.0 event log's
    # recent earnings events (date, age, echo count, FRESH/STALE) so Sonnet
    # can tell a new event from the 43rd write-up of a week-old print.
    # Auto-loads from state when not passed (None); pass {} to mean "no events".
    if event_log is None:
        try:
            from src.state import load_event_log
            event_log = load_event_log()
        except Exception:
            event_log = {}
    earnings_events_text = _build_known_earnings_events_block(event_log)

    # v3.39.5: Build the deep-tier digests block (Layer 1 output from
    # overnight Haiku digestion of earnings transcripts and Korean broker
    # notes). These are PRIMARY SOURCES — the actual analytical body of
    # broker reports, written by the broker's analysts, not Korean news
    # aggregators' coverage of those reports. When today's news triage
    # mentions a broker action that overlaps with one of these digests,
    # the broker is the source of record, not the aggregator.
    #
    # Schema reference: src/analysis/digester.py Digest dataclass.
    # Fields: source_id, source_type, published_date_iso, title,
    # key_metrics (dict), signal_relevance (list of indicator IDs),
    # key_quotes (list of <15-word quotes, max 1 per doc), target_price_action
    # (dict with broker/ticker/new_pt/prior_pt/direction — broker notes only),
    # fired_status_evidence, raw_url, digested_at_iso.
    digest_text = ""
    if digests:
        broker_digests = [d for d in digests if d.get("source_type") == "korean_broker_pdf"]
        transcript_digests = [d for d in digests if d.get("source_type") == "earnings_transcript"]

        digest_text += "\nDEEP-TIER BROKER NOTES (PRIMARY SOURCE — horse's mouth):\n"
        if broker_digests:
            digest_text += (
                f"({len(broker_digests)} broker notes digested overnight by Haiku.\n"
                "These are the actual analytical bodies of broker reports — PRIMARY\n"
                "sources that take precedence over news aggregator coverage.)\n\n"
            )
            for d in broker_digests:
                tpa = d.get("target_price_action") or {}
                km = d.get("key_metrics") or {}
                digest_text += f"- TITLE: {d.get('title', '')}\n"
                digest_text += f"  PUBLISHED: {d.get('published_date_iso', '?')}\n"
                digest_text += f"  SOURCE URL: {d.get('raw_url', '')}\n"
                if tpa:
                    digest_text += (
                        f"  TARGET PRICE ACTION: {tpa.get('broker', '?')} "
                        f"{tpa.get('direction', '?')} {tpa.get('ticker', '?')} target → "
                        f"{tpa.get('new_pt', '?')} (prior: {tpa.get('prior_pt', '?')})\n"
                    )
                if km.get("fy27_eps_revision_pct") is not None:
                    digest_text += f"  FY27 EPS revision: {km['fy27_eps_revision_pct']:+.1f}%"
                    if km.get("fy27_eps_new") is not None and km.get("fy27_eps_prior") is not None:
                        digest_text += f" ({km['fy27_eps_prior']} → {km['fy27_eps_new']})"
                    digest_text += "\n"
                if km.get("fy28_eps_revision_pct") is not None:
                    digest_text += f"  FY28 EPS revision: {km['fy28_eps_revision_pct']:+.1f}%\n"
                key_quotes = d.get("key_quotes") or []
                if key_quotes:
                    digest_text += "  ANALYTICAL EXCERPT:\n"
                    for kq in key_quotes[:3]:
                        digest_text += f"    \"{kq}\"\n"
                sr = d.get("signal_relevance") or []
                if sr:
                    digest_text += f"  INFORMS INDICATORS: {', '.join(sr[:8])}\n"
                fse = d.get("fired_status_evidence", "")
                if fse:
                    digest_text += f"  FIRED-STATUS EVIDENCE: {fse}\n"
                digest_text += "\n"
        else:
            digest_text += "(no broker notes digested in window)\n"

        digest_text += "\nDEEP-TIER EARNINGS TRANSCRIPTS (PRIMARY SOURCE):\n"
        if transcript_digests:
            digest_text += f"({len(transcript_digests)} transcripts digested overnight.)\n\n"
            for d in transcript_digests:
                km = d.get("key_metrics") or {}
                digest_text += f"- TITLE: {d.get('title', '')}\n"
                digest_text += f"  PUBLISHED: {d.get('published_date_iso', '?')}\n"
                digest_text += f"  SOURCE URL: {d.get('raw_url', '')}\n"
                # Surface the most cycle-relevant metrics if present
                metric_lines = []
                if km.get("supplier_inventory_weeks") is not None:
                    metric_lines.append(f"supplier inventory: {km['supplier_inventory_weeks']} weeks")
                if km.get("lead_time_weeks") is not None:
                    metric_lines.append(f"lead time: {km['lead_time_weeks']} weeks")
                if km.get("asp_yoy_pct") is not None:
                    metric_lines.append(f"ASP YoY: {km['asp_yoy_pct']:+.1f}%")
                if km.get("contract_price_qoq_pct") is not None:
                    metric_lines.append(f"contract QoQ: {km['contract_price_qoq_pct']:+.1f}%")
                if km.get("capex_2027_guidance_usd_b") is not None:
                    metric_lines.append(f"2027 CapEx guidance: ${km['capex_2027_guidance_usd_b']}B")
                if metric_lines:
                    digest_text += "  METRICS: " + " | ".join(metric_lines) + "\n"
                # Architecture signals (v3.3 captive-ratio feedstock — surface salient ones)
                arch = km.get("architecture_signals") or {}
                arch_lines = []
                if arch.get("hbm_capex_pct") is not None:
                    arch_lines.append(f"HBM/CapEx: {arch['hbm_capex_pct']}%")
                if arch.get("memory_pct_of_capex") is not None:
                    arch_lines.append(f"memory/CapEx: {arch['memory_pct_of_capex']}%")
                if arch.get("wafer_allocation_language"):
                    arch_lines.append(f"wafer: {arch['wafer_allocation_language']}")
                if arch.get("architecture_freetext"):
                    arch_lines.append(arch["architecture_freetext"])
                if arch_lines:
                    digest_text += "  ARCHITECTURE: " + " | ".join(arch_lines) + "\n"
                key_quotes = d.get("key_quotes") or []
                if key_quotes:
                    digest_text += "  EXCERPT:\n"
                    for kq in key_quotes[:3]:
                        digest_text += f"    \"{kq}\"\n"
                sr = d.get("signal_relevance") or []
                if sr:
                    digest_text += f"  INFORMS INDICATORS: {', '.join(sr[:8])}\n"
                digest_text += "\n"
        else:
            digest_text += "(no new earnings transcripts in window)\n"
    else:
        digest_text = "\nDEEP-TIER DIGESTS: (deep_tier digests not threaded — running in legacy mode)\n"

    # v3.39.6: COMPUTED METRICS BLOCK. The numerical verdicts the fast_tier
    # ALREADY computed deterministically (no LLM involved) — relative strength
    # spreads, hyperscaler CapEx/revenue ratios, forward P/Es, DART materials.
    # Threading these in lets Sonnet QUOTE exact numbers instead of estimating
    # from news headlines. This is the cure for the May 18 brief's
    # t3_relative_strength hallucination: Sonnet wrote "5-day cumulative
    # underperformance has almost certainly crossed the >10% threshold" when
    # the actual computed spread was below threshold (banner_text was None
    # in the fast_tier log, meaning overall_severity was None).
    computed_text = "\nCOMPUTED METRICS (deterministic numerical verdicts — quote these EXACTLY):\n"
    if computed_metrics:
        rs = computed_metrics.get("relative_strength_full") or {}
        if rs:
            computed_text += "\n  t3_relative_strength (5-day rolling window vs SOX / SMH):\n"
            skhx = rs.get("skhx_5d_vs_sox_pp")
            mu = rs.get("mu_5d_vs_sox_pp")
            dram = rs.get("dram_etf_vs_smh") or {}
            # v3.39.7.9 — defensive NaN guard (belt + braces). Pre-v3.39.7.9
            # `if x is not None` allowed NaN floats through and produced
            # "+nanpp" in the brief. get_relative_strength_vs_sox() now
            # returns None on NaN, but defensive check stays here in case
            # an upstream path bypasses the guard.
            def _valid(v):
                return v is not None and not (isinstance(v, float) and math.isnan(v))
            if _valid(skhx):
                computed_text += f"    SK Hynix 5d vs SOX:      {skhx:+.2f}pp\n"
            else:
                computed_text += "    SK Hynix 5d vs SOX:      (not computed — SOX or SK Hynix price cell missing in lookback window)\n"
            if _valid(mu):
                computed_text += f"    Micron 5d vs SOX:        {mu:+.2f}pp\n"
            else:
                computed_text += "    Micron 5d vs SOX:        (not computed — SOX or Micron price cell missing in lookback window)\n"
            if dram.get("spread_pp") is not None:
                computed_text += (
                    f"    DRAM ETF 5d vs SMH:      {dram['spread_pp']:+.2f}pp "
                    f"(DRAM {dram.get('dram_return_pct', '?')}%, SMH {dram.get('smh_return_pct', '?')}%)\n"
                )
            computed_text += (
                f"    Overall severity:        {rs.get('overall_severity') or 'None'}\n"
                f"    Overall fired (red):     {rs.get('overall_fired', False)}\n"
                "    Thresholds:              red ≤ -10pp on ANY sub-trigger | yellow ≤ -5pp\n"
            )
            sub = rs.get("fired_sub_triggers") or []
            if sub:
                computed_text += "    Fired sub-triggers:      " + "; ".join(
                    f"{s[0]}: {s[1]}" if isinstance(s, (list, tuple)) and len(s) >= 2 else str(s)
                    for s in sub
                ) + "\n"

        fpe = computed_metrics.get("forward_pe") or {}
        if fpe:
            computed_text += "\n  Forward P/E by ticker:\n"
            for ticker, val in fpe.items():
                if val is None:
                    continue
                try:
                    computed_text += f"    {ticker:<12} {float(val):.2f}\n"
                except (TypeError, ValueError):
                    computed_text += f"    {ticker:<12} {val}\n"

        quarterly_snapshots = computed_metrics.get("hyperscaler_quarterly_snapshots") or {}
        if quarterly_snapshots:
            # v3.39.7.9 — CapEx/OCF is the ONLY trigger metric. CapEx/revenue
            # was removed entirely from the prompt (pre-v3.39.7.9 it was kept
            # "for context" and Layer 2 reversed verdicts against it). Mean
            # is now 2-quarter rolling per company per user direction
            # 2026-05-21 to filter out single-quarter OCF dip noise.
            mean_co_ratio = computed_metrics.get("mean_capex_ocf_ratio_2q")
            per_company = computed_metrics.get("per_company_capex_ocf_2q") or {}
            n_co = computed_metrics.get("n_companies_with_2q_capex_ocf", 0)
            computed_text += (
                "\n  Hyperscaler CapEx/OCF strain (PRIMARY and ONLY trigger for t2_hyperscaler_capex_revenue;\n"
                "  the legacy ratio of CapEx over revenue is NOT used as a trigger — do not cite or reason from it):\n"
            )
            if mean_co_ratio is not None:
                # Determine band relative to thresholds
                if mean_co_ratio > 0.50:
                    band = "FIRED (>50%)"
                elif mean_co_ratio > 0.40:
                    band = "PARTIALLY FIRED (>40%)"
                else:
                    band = "Not fired (<40%)"
                computed_text += (
                    f"    Aggregate (cross-company mean of 2-quarter rolling per-company means, {n_co} hyperscalers): "
                    f"{mean_co_ratio*100:.1f}%  →  {band}\n"
                    f"    Historical reference: mean >40% in all 3 memory crashes "
                    f"(2014/2018/2022); stays <40% in non-crash years.\n"
                )
                if per_company:
                    computed_text += "    Per-company 2-quarter rolling mean CapEx/OCF:\n"
                    for ticker, r in per_company.items():
                        computed_text += f"      {ticker:<6} {r*100:.1f}%\n"
            else:
                computed_text += "    Aggregate: insufficient data (no last-quarter ratios available).\n"
            computed_text += "\n  Per-company quarterly snapshots (raw — for narrative context only; trigger uses 2q mean above):\n"
            for ticker, quarters in quarterly_snapshots.items():
                if not quarters:
                    continue
                # Snapshots sorted newest-first; show up to 2 quarters
                for i, q in enumerate(quarters[:2]):
                    co_r = q.get('capex_ocf_ratio')
                    co_s = f"{co_r*100:.1f}%" if co_r is not None else "n/a"
                    strain_flag = "  ⚠ severe single-q strain >100%" if q.get('fired_threshold') else ""
                    label = "latest" if i == 0 else "prior"
                    computed_text += (
                        f"    {ticker:<6} {q.get('quarter_iso', '?')} ({label}): "
                        f"CapEx ${q.get('capex_usd_b', '?')}B / "
                        f"OCF ${q.get('ocf_usd_b', 'n/a')}B = CapEx/OCF {co_s}{strain_flag}\n"
                    )

        dart = computed_metrics.get("dart_material_disclosures") or {}
        if dart:
            n_companies = len(dart)
            total_mat = sum(len(v) if isinstance(v, list) else 0 for v in dart.values())
            computed_text += f"\n  DART material disclosures: {total_mat} across {n_companies} companies\n"

        # v3.4.0: DECODE HBM CAPTIVE RATIO (DHCR) — direction monitor
        # New Tier 1 indicator. Composite of (a) frontier KV efficiency,
        # (b) HBM-per-accelerator capacity, (c) enterprise AI demand growth.
        # The CORE QUESTION: is enterprise demand-pull growing faster than
        # the supply-side capacity-creation rate? If yes → HBM tight (bullish);
        # if no → HBM slack (bearish).
        #
        # Mental model rejects the naive "efficiency vs context" trade-off.
        # Efficiency UNLOCKS economic context capacity. Enterprise workloads
        # expand to consume what's economically possible. The binding question
        # is whether enterprise demand grows fast enough to fill the unlocked
        # headroom.
        dhcr = computed_metrics.get("dhcr_snapshot") or {}
        if dhcr:
            computed_text += (
                "\n  t1_decode_hbm_captive_ratio (direction monitor — NEW v3.4.0):\n"
            )
            cap_tokens = dhcr.get("implied_context_capacity_per_accelerator_tokens", 0) or 0
            if cap_tokens >= 1_000_000:
                cap_s = f"{cap_tokens/1_000_000:.1f}M"
            elif cap_tokens >= 1_000:
                cap_s = f"{cap_tokens/1_000:.0f}K"
            else:
                cap_s = str(cap_tokens)
            computed_text += (
                f"    Frontier KV efficiency: {dhcr.get('frontier_kv_kb_per_token', 0):.0f} KB/tok "
                f"({dhcr.get('frontier_model_name', '?')}, {dhcr.get('frontier_model_arch', '?')})\n"
                f"    Frontier HBM/accelerator: {dhcr.get('frontier_hbm_gb_per_accelerator', 0):.0f} GB "
                f"({dhcr.get('frontier_accelerator_name', '?')})\n"
                f"    Implied context capacity: ~{cap_s} tokens per accelerator at frontier efficiency\n"
                f"    YoY KV change: {dhcr.get('kv_yoy_change_pct', 0) or 0:+.0f}% "
                f"(negative = efficiency improving)\n"
                f"    YoY HBM-per-accelerator change: {dhcr.get('hbm_per_accelerator_yoy_change_pct', 0) or 0:+.0f}%\n"
                f"    Composite capacity-creation rate: {dhcr.get('capacity_creation_yoy_pct', 0) or 0:+.0f}% YoY\n"
                f"    Enterprise AI run-rate (MSFT+AWS+GCP+Oracle): "
                f"${dhcr.get('enterprise_ai_aggregate_run_rate_usd_b', 0):.0f}B annualized\n"
                f"    Enterprise AI YoY: {dhcr.get('enterprise_ai_yoy_growth_pct', 0) or 0:+.0f}%\n"
                f"    NVIDIA datacenter YoY: {dhcr.get('nvidia_datacenter_yoy_growth_pct', 0) or 0:+.0f}%\n"
                f"    Hyperscaler CapEx YoY: {dhcr.get('hyperscaler_capex_yoy_growth_pct', 0) or 0:+.0f}%\n"
                f"    DIRECTION READ: {dhcr.get('direction', 'unknown').upper()} "
                f"(confidence: {dhcr.get('confidence', '?')})\n"
                f"    Dominant force: {dhcr.get('dominant_force', '?')}\n"
            )
            subst = dhcr.get("substitution_risk_notes") or []
            if subst:
                computed_text += "    Substitution-risk flags (non-HBM AI accelerators):\n"
                for note in subst[:5]:
                    computed_text += f"      • {note}\n"
            computed_text += (
                "    Interpretation guidance: This is a DIRECTION MONITOR, not a "
                "fire/not-fire signal. Always render current_status='Not fired' for "
                "this indicator in v3.4.0. Use 'where_we_stand' field to describe "
                "the direction read and what's driving it. The mental model is that "
                "efficiency gains UNLOCK context capacity which enterprise workloads "
                "then consume — so DECELERATION in enterprise revenue growth is the "
                "actual bearish signal, not efficiency improvements per se.\n"
            )
    else:
        computed_text += "  (computed_metrics not threaded — running in legacy mode)\n"

    # v3.39.6: LAYER 2 SIGNAL REASONINGS BLOCK. Haiku already evaluated each
    # of the 22 indicators with full access to digests, naver_data, finnhub,
    # computed_metrics, and fast_tier news. Its verdicts are authoritative
    # PER-SIGNAL. Sonnet should CITE these (not re-derive them).
    layer2_text = "\nLAYER 2 PER-SIGNAL VERDICTS (Haiku, with full evidence access — DEFER to these on individual signal status):\n"
    if signal_reasonings:
        layer2_text += (
            f"({len(signal_reasonings)} signals evaluated. Each verdict is the result\n"
            "of Haiku reading ALL evidence for that one signal — broker digests,\n"
            "transcripts, Naver rows, Finnhub direction, computed metrics, news. When\n"
            "writing indicator_assessments[].current_status and change_vs_prior,\n"
            "use these verdicts as the source of truth.)\n\n"
        )
        for r in signal_reasonings:
            rd = r.to_dict() if hasattr(r, "to_dict") else r
            _status = rd.get("fired_status", "?")
            _change = rd.get("evidence_for_change_vs_yesterday") or ""
            _change_lower = _change.lower().strip()
            _is_noop = (
                not _change_lower
                or _change_lower in ("no change", "none", "no", "n/a")
                or _change_lower.startswith("no change")
            )
            # v3.40.0 input trim — a dormant signal (Not fired AND no change
            # vs yesterday) gets a single line; Sonnet isn't writing about it
            # and doesn't need its evidence bullets. Fired / Partially fired,
            # or any real change, keep the full detail below.
            if _status == "Not fired" and _is_noop:
                layer2_text += (
                    f"  [{rd.get('signal_id', '?')}] T{rd.get('tier', '?')} "
                    f"{rd.get('signal_name', '?')}: Not fired (unchanged)\n\n"
                )
                continue
            layer2_text += (
                f"  [{rd.get('signal_id', '?')}] T{rd.get('tier', '?')} "
                f"{rd.get('signal_name', '?')}\n"
                f"    Verdict: {rd.get('fired_status', '?')} "
                f"(confidence: {rd.get('confidence', '?')}, "
                f"convergence: {rd.get('convergence', '?')}, "
                f"evidence_count: {rd.get('evidence_count', 0)})\n"
            )
            fse = rd.get("fired_status_evidence") or []
            if fse:
                layer2_text += "    Evidence for current status:\n"
                for ev in fse[:4]:
                    layer2_text += f"      - {ev}\n"
            if _change and not _is_noop:
                layer2_text += f"    Change vs yesterday: {_change}\n"
            layer2_text += "\n"
    else:
        layer2_text += "  (signal_reasonings not threaded — running in legacy mode)\n"

    # v3.4.12 — QoQ TRANSCRIPT DIFFS BLOCK. For each company-quarter that
    # has a cached Haiku diff vs prior quarter, Sonnet sees the structured
    # change set (indicator deltas, language dropped/added, metric moves,
    # overall tone shift). The most cycle-relevant signal is LANGUAGE DROPPED
    # — when a CFO stops using a prior commitment ("sold out through 2027"),
    # that's the canonical cycle-turn signal that the framework would
    # otherwise miss by reacting to each call in isolation.
    #
    # Use this block when narrating where_we_stand for transcript-driven
    # indicators. Quote verbatim from the diff's prior/current quote pairs
    # when calling out specific shifts.
    qoq_text = "\nQoQ TRANSCRIPT DIFFS (v3.4.12 — Haiku-computed changes vs prior quarter for each company):\n"
    if transcript_diffs:
        # Lazy import — keep module-level deps minimal
        try:
            from src.analysis.transcript_diff import render_diff_for_sonnet_prompt
            qoq_text += (
                f"({len(transcript_diffs)} active diff(s). Pay particular attention to\n"
                "LANGUAGE DROPPED — when a CFO STOPS using prior cycle-affirming\n"
                "commitments, that's a Phase-4-to-5 transition signal. Reference\n"
                "these explicitly in where_we_stand for transcript-driven indicators\n"
                "and in executive_brief when they shift the overall tone.)\n\n"
            )
            for d in transcript_diffs:
                # transcript_diffs entries are quarter_record dicts — extract the diff
                diff_payload = d.get("diff_with_prior") if isinstance(d, dict) else None
                if diff_payload:
                    qoq_text += render_diff_for_sonnet_prompt(diff_payload)
        except Exception as e:
            qoq_text += f"  (diff render failed: {e})\n"
    else:
        qoq_text += "  (no active QoQ diffs — either first run, or no companies yet have 2 consecutive quarters of digests)\n"

    # v3.4.11 — Phase context block. The deterministic phase detector runs
    # in fast_tier BEFORE this prompt builder; its verdict is passed in via
    # `phase_state`. If phase=5, swap the framework context and JSON example
    # so Sonnet stops emitting Phase 4 boilerplate and writes Phase 5
    # ("return to trough") semantics. If phase=4 or phase_state is None,
    # preserve the original Phase 4 prompt verbatim.
    detected_phase = 4
    detected_phase_label = 'Boom / Euphoria (late stage)'
    phase_5_detected_date = None
    cycle_peak_price = None
    if phase_state and isinstance(phase_state, dict):
        detected_phase = int(phase_state.get('current_phase', 4))
        detected_phase_label = phase_state.get(
            'current_phase_label', detected_phase_label,
        )
        phase_5_detected_date = phase_state.get('phase_5_detected_date_iso')
        cycle_peak_price = phase_state.get('cycle_peak_price_at_detection')

    if detected_phase >= 5:
        # PHASE 5 ACTIVE — the deterministic detector has flipped us to Decline.
        # Override the Phase 4 framework context, scenario framing,
        # invariants, and JSON example with their Phase 5 counterparts.
        framework_context_block = f"""ANALYTICAL FRAMEWORK CONTEXT (v3.4.11 — PHASE 5 ACTIVE):
- The deterministic phase detector has flipped to Phase 5 ("Decline"). Detection date: {phase_5_detected_date or '(not recorded)'}; cycle peak price at detection: {('%.0f' % cycle_peak_price) if cycle_peak_price else '(not recorded)'} KRW.
- We are NO LONGER ascending to a peak. The peak is BEHIND US. Forward expectation is a decline to the cycle trough.
- Per memory_supercycle_analysis.docx Part 1, Phase 5 (Decline) typically lasts 12-18 months. Stocks decline 40-60% in Year 1, ASPs -40 to -65%; Year 2 EPS goes negative.
- Per Part 2, six prior memory cycles saw memory pure-plays decline 50-65% peak-to-trough.
- The case-scenario semantics flip: `return_to_peak_pct` is reinterpreted as `return_to_trough_pct` (still signed). Base case = expected decline (negative). Bull case = shallower decline (closer to zero / less negative). Bear case = deeper decline (more negative).
- The peak window fields (`peak_window_start` / `peak_window_end`) become TROUGH window fields semantically. Project when the cycle trough is most likely to occur.
- Tier framework still applies but is mostly for tracking depth of decline: T3 (concurrent) and T4 (lagging) indicators firing confirm the decline; T1/T2 unfiring would suggest reversion potential.
- The DEFAULT trough anchors (used as Sonnet's starting prior, then adjusted by signal evidence): base around -40% from cycle peak, bear around -60%, bull around -25%."""

        scenario_framing_block = """SCENARIO FRAMING — PHASE 5 (v3.4.11):
The three scenarios are signal-conditional projections for the depth and timing of the cycle TROUGH. The peak is behind us; the question is how deep the decline goes and when the trough lands.

• base_case = the EXPECTED scenario given Phase 5 unfolding at its current pace. Most probable scenario by invariant. `return_to_peak_pct` ≤ 0 (it's the expected DECLINE from current price to the trough). As T3/T4 signals fire more aggressively, base TRIMS DEEPER (more negative); as T1/T2 signals unfire, base RESTORES TOWARD ZERO.

• bull_case = scenario where decline is SHALLOWER than expected (soft landing, resilience surprise). Probability GROWS as positive surprises appear (Tier 2 unfires, T1 unfires). `return_to_peak_pct` is closer to zero / less negative than base — always above base.

• bear_case = scenario where decline ACCELERATES beyond current trajectory. Probability GROWS as more T3/T4 indicators fire or T2 deepens. `return_to_peak_pct` is more negative than base. Bear can reach historical -55 to -65% magnitude per the 2017-18 and 2020-22 references.

HARD INVARIANTS (Phase 5 — enforced post-output):
  1. probability_pct sums to EXACTLY 100 across the three scenarios.
  2. base_case.probability_pct > bear_case.probability_pct.
  3. base_case.probability_pct > bull_case.probability_pct.
  4. base_case.return_to_peak_pct <= 0 (Phase 5: decline ahead — base never assumes peak is in front of us).
  5. bull_case.return_to_peak_pct > base_case.return_to_peak_pct (Phase 5: bull = shallower decline = closer to zero).
  6. bear_case.return_to_peak_pct < base_case.return_to_peak_pct (Phase 5: bear = deeper decline = more negative).

Phase 5 signal-conditional updates:
  - Additional T3/T4 firing → bear DEEPER (more negative), base modestly deeper, bull SHRINKS.
  - T2 unfiring → potential re-acceleration signal, bull probability +2-4pp.
  - New T1 signals firing → typically already priced; small bear shift.
"""

        json_example_phase_line = f'    "current_phase": {detected_phase},'
        json_example_phase_label = f'    "current_phase_label": "{detected_phase_label}",'
        # Phase 5 JSON example — base/bull/bear all negative or zero, base ≤ 0
        json_example_case_block = (
            '    "base_case": {{"probability_pct": 50, "peak_months_away": 9, "peak_window_start": "Q1 2027", "peak_window_end": "Q3 2027", "return_to_peak_pct": -40}},\n'
            '    "bull_case": {{"probability_pct": 22, "peak_months_away": 15, "peak_window_start": "Q4 2027", "peak_window_end": "Q2 2028", "return_to_peak_pct": -25}},\n'
            '    "bear_case": {{"probability_pct": 28, "peak_months_away": 6, "peak_window_start": "Q4 2026", "peak_window_end": "Q2 2027", "return_to_peak_pct": -60}},'
        )
    else:
        # PHASE 4 (default) — original framework context preserved verbatim.
        framework_context_block = """ANALYTICAL FRAMEWORK CONTEXT:
- Memory cycle has 5 phases. Currently late Phase 4 ("Boom / Euphoria").
- Stock peak typically leads fundamental peak (peak EPS) by 3-6 months.
- Tier 1 signals lead stock peak by 3-6 months.
- Tier 2 signals lead stock peak by 2-4 months.
- Tier 3 signals are concurrent with stock peak.
- Tier 4 signals lag stock peak.
- Most likely peak window per current evidence: Q3 2026 - Q1 2027."""

        scenario_framing_block = """SCENARIO FRAMING — READ CAREFULLY:
The three scenarios are signal-CONDITIONAL probabilities for the length-to-peak
and price-to-peak in SK Hynix stock. They are NOT three symmetric outcomes.

• base_case = the EXPECTED scenario given signals continuing at their current
  pace. By definition the most probable scenario. As Tier 2/3/4 signals fire,
  update base case's PARAMETERS (peak_months_away SHORTENS, return_to_peak_pct
  TRIMS) — do NOT let bear probability exceed base.

• bull_case = scenario where signals UN-FIRE (those that fired reverse) or
  fire much later than expected. Requires abnormal positive surprise. Always
  has positive return_to_peak_pct. Probability SHRINKS as more signals fire
  (becomes less plausible).

• bear_case = scenario where degradation accelerates BEYOND current signal
  trajectory. Requires abnormal negative surprise. Peak shortens to 0; the
  return_to_peak_pct can be NEGATIVE (peak already in → downside from here).
  Probability GROWS as more signals fire — but capped strictly below base.

HARD INVARIANTS (enforced post-output; produce values that already satisfy):
  1. probability_pct sums to EXACTLY 100 across the three scenarios.
  2. base_case.probability_pct > bear_case.probability_pct.
  3. base_case.probability_pct > bull_case.probability_pct.
  4. base_case.return_to_peak_pct >= 0 (positive — base never assumes peak passed).
  5. bull_case.return_to_peak_pct > base_case.return_to_peak_pct (always higher upside).
  6. bear_case.return_to_peak_pct <= base_case.return_to_peak_pct (lower; can be negative).
"""

        json_example_phase_line = '    "current_phase": 4,'
        json_example_phase_label = '    "current_phase_label": "Boom / Euphoria (late stage)",'
        json_example_case_block = (
            '    "base_case": {{"probability_pct": 50, "peak_months_away": 4, "peak_window_start": "August 2026", "peak_window_end": "October 2026", "return_to_peak_pct": 18}},\n'
            '    "bull_case": {{"probability_pct": 22, "peak_months_away": 12, "peak_window_start": "Q1 2027", "peak_window_end": "Q2 2027", "return_to_peak_pct": 55}},\n'
            '    "bear_case": {{"probability_pct": 28, "peak_months_away": 0, "peak_window_start": "May 2026", "peak_window_end": "May 2026", "return_to_peak_pct": -8}},'
        )

    # v3.39.11 — AUTHORITATIVE 24h transition block. Computed deterministically
    # in fast_tier (load_trigger_log_24h + collapse) and passed in here so the
    # prompt itself tells Sonnet the ground truth BEFORE it writes prose. Prior
    # to this, Sonnet computed transitions via its own prior_state diff, which
    # could miss yesterday's transitions still inside the 24h window — the
    # 2026-06-12 brief opened with "No tier-1-5 signals fired or unfired today"
    # while a tier-3 transition from yesterday was still in the banner list
    # (a "today" vs "last 24h" time-frame split).
    if authoritative_24h_log:
        _entries_block = "\n".join(
            f"  • Tier {e.get('tier','?')} — {e.get('indicator_id','?')} "
            f"({e.get('indicator_name','?')}): "
            f"{e.get('old_status','?')} → {e.get('new_status','?')} "
            f"[at {e.get('fired_at_iso','?')}]"
            for e in authoritative_24h_log
        )
        _signal_count = sum(
            1 for e in authoritative_24h_log
            if e.get("indicator_id") not in (
                "t1_competitor_dynamics",       # Indicator 7 (overlay)
                "t1_us_adr_event",              # Indicator 8 (overlay)
                "t1_decode_hbm_captive_ratio",  # Indicator 6 (DHCR, overlay)
            )
        )
        authoritative_block = f"""
AUTHORITATIVE INDICATOR TRANSITIONS — LAST 24 HOURS (deterministic, do not contradict):

The system has computed the following indicator status transitions over the last 24 hours. This list is the SOURCE OF TRUTH for the banners shown to the reader at the top of the brief. Your executive_brief and per-indicator commentary MUST be consistent with this list — do not write prose that contradicts these transitions.

{_entries_block}

Signal-grade (tier 1-5) transitions in this list: {_signal_count}

HARD RULE: Because this list contains {len(authoritative_24h_log)} transition(s) ({_signal_count} of which are tier-1-5 signals), you may NOT open the executive_brief with any claim equivalent to "no signals fired or unfired" / "all indicators hold prior status" / "no tier-1-5 signals fired or unfired today". Such a claim would directly contradict the banners the reader sees above the brief. Instead, OPEN by naming the actual transition(s) — for example: "Relative strength (t3) reverted to 'Not fired' as the 5d spread tightened..." — then weave in the rest of today's news.

If the signal-grade count is ZERO but watch overlays (Indicators 6/7/8) appear in the list, you MAY use the legitimate phrasing "no tier-1-5 signals fired/unfired today; {{N}} watch overlay(s) updated" — but you must still name those overlay changes explicitly.
"""
    else:
        authoritative_block = """
AUTHORITATIVE INDICATOR TRANSITIONS — LAST 24 HOURS (deterministic, do not contradict):

No indicator transitions in the last 24 hours. You may open the executive_brief with "no signals fired or unfired today" or equivalent — that claim matches the deterministic state.
"""

    prompt = f"""You are a senior memory/semiconductor cycle analyst preparing a daily institutional briefing. Use the Haiku-pre-processed news + market data + indicator framework below to produce a comprehensive structured analysis.

{framework_context_block}

{authoritative_block}

YOUR JOB:
1. Build the EXECUTIVE SUMMARY in the structured format below — including the consolidated `executive_brief` paragraph that anchors the brief (see detailed spec in section EXECUTIVE BRIEF SPECIFICATION below).
2. Assess EVERY indicator (status, where we stand, latest developments with links).
3. Provide DEEP DIVES for the top 5 indicators.
4. Compose the cycle position synthesis paragraph.
5. Identify NEW TRIGGERS that fired today (compare against prior state).
6. Calculate UPDATED PROBABILITY DISTRIBUTION based on new evidence.
7. Compare current peak estimate vs prior to flag if peak has shortened or extended.

EXECUTIVE BRIEF SPECIFICATION (v3.39.7.5, length cap v3.4.1):

The `executive_brief` field is the consolidated paragraph at the top of the brief — the FIRST thing the reader sees after the phase line and trigger callouts. It replaces three separate v3.39.7.4 blocks (delta_from_prior_text prose, big_movers list, market_summary) into a single coherent narrative.

Length: **HARD CAP 250 WORDS** (v3.4.1 — previous unbounded brief reached 411 words on May 21 2026). The cap is a discipline, not a sacrifice — compress, don't cut. Target 5-8 sentences, ONE paragraph. Remove filler words ("notably", "interestingly", "with respect to"), nest clauses tightly, prefer active voice, lose redundant qualifiers. If a sentence is 30+ words, split it or trim it. The reader uses this to decide whether to read on; details are in later sections — point them to §N rather than restating.

Self-check before emitting: count your words. If >250, compress further. Look first for: (a) intensifiers ("dramatically", "substantially"); (b) restated context already in the indicator status pills; (c) hedges ("appears to", "seems to indicate"); (d) numerical detail that belongs in §3 / §4 cards, not the headline paragraph; (e) anchor link explanations like "see §3 for more" — the anchor itself IS the explanation.

Content — the brief's SPINE is developments along the signals and indicators (the memory/semi/AI thesis). Its job is to SUMMARIZE what is happening on that thesis today — not to narrate the market, and not to manufacture a cause for every price move. Blend the elements below into prose (NOT labeled bullets), in roughly this priority:
  (i)  Signal/indicator developments — what changed (or what notably held) along the framework today, and the fresh news / trigger value behind it. This is the lead. If nothing changed, say so plainly, then lead with the most material fresh development on the thesis.
       **v3.4.9 — VOCABULARY DISCIPLINE for cards 6-8 (DHCR, Competitor, ADR):**
       Indicators 6, 7, 8 are WATCH-ONLY OVERLAYS — they do NOT count toward Tier 1's `X/6 fired` totals. A status change in one of these is an INDICATOR change, NOT a signal firing. Use this exact vocabulary:
         - For status changes in indicators 1-5: "signal fires" / "signal unfires" / "one new signal fires today" — correct.
         - For status changes in cards 6, 7, 8: "indicator changed status" / "DHCR moved to 'Partially fired'" / "Indicator 7 (Competitor dynamics) updated to..." — DO NOT say "signal fires today" or "new signal" for these. They are framework overlays for context, not triggers in the firing count.
       Concrete examples of correct vs incorrect phrasing:
         WRONG: "One new signal fires today: t1_competitor_dynamics upgrades to 'Partially fired' as CXMT enters DDR5..."
         RIGHT: "One indicator changed status today: Competitor dynamics (Indicator 7) moved to 'Partially fired' as CXMT enters DDR5..." OR "No new tier-1-5 signals fired today; one watch overlay (Competitor dynamics, Indicator 7) updated to 'Partially fired' as CXMT enters DDR5..."
       The reason for the distinction: the T1 firing count is the institutional read for "are we in peak"; mixing watch-overlay status changes into that signal triggers false-positive reads.
       **v3.39.10 — "NO SIGNALS FIRED" IS A CONDITIONAL CLAIM:** You may write "no signals fired or unfired today" (or any equivalent blanket no-change claim) ONLY when ZERO tier-1-5 signals changed status in EITHER direction. If ANY tier-1-5 signal fired OR unfired today — including a reversion to 'Not fired' — that phrasing is FALSE and FORBIDDEN. Do NOT open with "no signals fired or unfired" and then describe a signal change in the same paragraph (e.g. "No signals fired or unfired today; t3_relative_strength reverted to 'Not fired'" is a self-contradiction and will be overwritten at render time). Lead instead with the actual change: "Relative strength (t3) reverted to 'Not fired' as ...". A change in a watch overlay (Indicators 6/7/8) alone does NOT make the tier-1-5 claim false — those are overlays, not signals — but state the overlay change explicitly rather than burying it.
  (ii) Fresh news bearing on a signal — PRIORITIZE items that align with a framework signal even when they don't formally fire it. Example: a single-firm "Sell SK Hynix" rating doesn't fire t2_insider_sales (which requires actual insider transactions), but in a market of 26-up / 0-down EPS revisions it IS the first sell-side dissent — a psychological inflection the framework cares about deeply. Surface these as "directional confirmation of [signal name]" or "first dissent from the [signal name] consensus".
  (iii) Notable moves — name specific tickers with 1D % moves WHEN the move carries thesis information. A memory pure-play DIVERGING from the semi tape is signal — lead with it. A broad co-move, where the memory names rise/fall in step with NVDA/AVGO/TSM/the SOX, is NOT a stock-specific event: report it as market-wide and route it per DRIVERS & ATTRIBUTION below; do NOT pin it on any single name's micro story.
  (iv) Macro — a macro development (rates, inflation, the Fed, oil, major geopolitics, China export policy, AI-capex commitments) earns a mention ONLY when it has a credible transmission channel INTO one of our indicators. When it does, NAME the channel, not just the headline — e.g. easing inflation / a pulled-forward rate-cut path re-rates tech and lifts the memory names, which bears on relative-strength-vs-SOX (t3) and the forward multiple (t2_pe_compressed), while cheaper capital supports hyperscaler capex (t2). A market-wide move with NO read-through to an indicator is noted as macro and dropped — it is backdrop, not a framework development.

DRIVERS & ATTRIBUTION (v3.41.5):
  - SUMMARIZE; do not fabricate a driver. For a routine move, or a broad/market-wide move, state what happened and (where relevant) route it to the indicator it touches — do NOT invent a stock-specific cause.
  - Attribute a specific cause to a specific move ONLY when there is fresh, same-day news in today's feed that supports it. No fresh supporting item → state the move without a "because".
  - Never explain a broad, market-wide move with a single company's micro story. When the whole basket moves together, the driver is the market or a macro factor, not the name — trace it to the indicator it touches (iv) rather than attaching it to whichever company is convenient.

NO STALE CARRYOVER (v3.41.5):
  - The brief covers TODAY. Do NOT re-narrate a prior-day story unless there is fresh, same-day news about it in today's feed. A story that resolved days or weeks ago is not today's news and must not lead — or even appear in — the summary absent a fresh development.
  - Yesterday's framing is not evidence. Anchor on today's news and the computed verdicts, never on how the thesis happened to be described in a previous brief.

EARNINGS ECHO — new information vs repackaging (v3.41.9):
An earnings print is a SINGLE event, and a fresh publish date on an article does NOT make an old event today's news. After a print has appeared in a prior brief, ask of every item referencing it: does it carry a NEW, ATTRIBUTABLE fact, or does it only restate what was already known? Consult the EARNINGS EVENTS ALREADY IN THE RECORD block (below the news) — it lists each recent print with its age, write-up count, and a FRESH/STALE tag.

  LEADS — something that did not exist in yesterday's information set:
    • the COMPANY discloses a new figure or decision tied to the print — revised guidance, a newly disclosed number, a buyback, a capex change, a customer/product commitment; OR
    • a specific NAMED party takes a NEW action with a concrete value — an analyst raises/cuts a price target to a stated level, changes a rating, revises an estimate; the first sell-side dissent in a one-sided consensus; or a verdict reversal (the events block flags these as DIVERGENCE).
    In these cases the new fact is the ACTION or FIGURE, not the print itself — it is legitimately today's news even though it references last week's earnings, and it may lead on ITS day.

  DOES NOT LEAD — coverage echo, no new attributable fact (route to §5 as background at most):
    • recaps, explainers, and round-ups restating the beat with no new party, action, or number ("Micron had a great quarter", "analysts remain bullish on the print");
    • the Nth same-direction confirmation once that direction is already the established consensus — it adds to the revision COUNT (t2_eps_revisions), it does not re-lead the brief.

  Concrete test: new named actor + new value/decision = information; restatement of the known result = echo. "Morgan Stanley raises Micron's price target to $200 from $160" leads on its day; "Micron had a great quarter" four days after the print does not. A print tagged STALE in the events block must NOT lead absent one of the LEADS conditions above; when nothing qualifies, lead with what actually changed today — a signal transition, a genuinely new event, or the most material new item on the thesis.

FORBIDDEN VOCABULARY (v3.4.1):
The word "Reversed" is BANNED in all output fields — `executive_brief`, `new_triggers_today`, `change_vs_prior`, `where_we_stand`, `latest_developments`. Use "Not fired" for reversals. If an indicator transitioned from "Fired" to "Not fired", phrase it as: "t2_X reverted to 'Not fired' after [reason]" — NEVER "Status changed from 'Fired' to 'Reversed'". This is a hard rule — any output containing "Reversed" will be post-processed to "Not fired" at render time, but writing it correctly the first time avoids ambiguity.

Anchor links — when referencing detail elsewhere in the brief, emit hyperlinks of the form `[→§N]` where N is the section number (1-7):
  §1 = Performance overview (price table)
  §2 = Where we stand in the cycle (phase chart)
  §3 = Tier indicators — daily status (summary cards)
  §4 = Top 8 indicators — deep dive (5 cycle indicators + Indicator 7 Competitor + Indicator 8 ADR + DHCR)
  §5 = Latest news from the memory & semi market
  §6 = Calendar of events — next 90 days
  §7 = Data source assessment

The template substitutes `[→§N]` into clickable HTML anchors. Use them liberally — every claim that has a detail elsewhere should link. Example:
  "...ASML +5.5% [→§1] led equipment names higher on strong demand commentary [→§5], while SK Hynix flat amid first sell-side dissent (directional confirmation of t2_insider_sales [→§3])..."

Tone: institutional analyst, declarative, no hedging language ("might", "could", "perhaps"). State the facts as observed.

OUTPUT HYGIENE — investor-facing prose: every prose field you write (executive_brief, where_we_stand, latest_developments, change_vs_prior) is read directly by an investor. NEVER expose internal framework machinery in them: no version tags ("v3.x", "per v..."), no internal rule names, and no mechanism vocabulary ("operative state", "cluster", "fingerprint", "requires resolution", "supersede", "Layer 2", "Layer 3", "tier signal"). Describe what is happening in the world and your conclusion in plain analyst language — never how the framework arrived at it.

OUTPUT HYGIENE — URLs and links: every source link goes in a STRUCTURED field only — the per-indicator `relevant_links` array ({{"title": "...", "url": "..."}}) or `key_news_summary[].url`. NEVER write a URL or a markdown/parenthetical link such as "([label](https://...))" inline inside any prose field (executive_brief, market_summary, where_we_stand, latest_developments, change_vs_prior, cycle_position_call). An inline link in prose breaks the JSON output mid-string — everything after it is lost, including the news block — and even when it parses it renders as raw text, not a clickable link. The prose states the claim; the structured field carries the link.

HANDLING CONFLICTING NEWS ON THE SAME EVENT:

When two or more news items in today's feed reference the SAME real-world event but report contradictory direction (e.g. one says "Samsung strike to launch", another says "Samsung union calls off strike"), you MUST resolve the contradiction by published timestamp, not by item count.

Operational rules:
  (a) Identify event clusters — items are in the same cluster when they reference the same company/entity AND the same event window (a strike, a wage negotiation, an earnings beat, a regulatory ruling, a deal closure, etc.).
  (b) Within a cluster, the item with the LATEST published timestamp is the current state. Earlier items in the cluster are out of date — treat them as background context, not as current facts.
  (c) If the cluster contains a REVERSAL (event was on, then called off; or event was averted, then re-triggered), the executive_brief MUST explicitly note the reversal. Do not write as if the older state still holds. Example: do NOT write "the Samsung 45,000-worker strike commencing May 21 tightens HBM supply" if a later item in the same cluster reports the strike was called off — the correct framing is "the planned Samsung strike was called off one hour before launch, removing the near-term HBM supply tightening risk that was previously priced in".
  (d) The reversal itself is news — surface it explicitly. The framework cares about state changes, not historical states.
  (e) This rule applies to the executive_brief paragraph AND to the key_news_summary block AND to per-indicator commentary. Apply it everywhere news drives narrative.

This rule does NOT change the relevance filter or which items reach you — it changes how you SYNTHESIZE items that all reach you. Multiple contradicting items in your context window is the normal case for fast-moving stories; you are responsible for resolving them.

VALUE-CHAIN INFERENCE:

When two or more signals in today's feed are causally linked through the memory value chain, draw the chain EXPLICITLY in the executive_brief and per-indicator commentary. Reporting upstream and downstream signals as if they're independent loses the framework's core analytical value. Examples of inference chains you should draw when the evidence is present in today's brief:

  CAPACITY-MIX SHIFTS:
  - SK Hynix or Samsung announces HBM capacity expansion / wafer reallocation toward HBM → less DDR5 / LPDDR / NAND wafer allocation → spot price tightening short-term + contract price acceleration 1-2 quarters out. Conversely a HBM capacity cut → DDR5 spot loosens. State the chain explicitly: "X capacity shift to Y means Z effect on the W market in T months".
  - Korean fab outage / labor disruption affecting Samsung or SK Hynix → near-term HBM AND DDR5/LPDDR spot tightening → spread between spot and contract narrows → t2_spot_contract_spread input direction shifts.

  DEMAND CASCADES:
  - Hyperscaler ups AI CapEx guidance (Tier C) → NVIDIA / AMD / Broadcom backlog increases (Tier B) → SK Hynix HBM revenue 1-2 quarters out → memory pure-play earnings beat / guidance raise.
  - AI lab signs multi-billion infrastructure deal (OpenAI / Anthropic) → chip designer capacity commitment (Cerebras / NVIDIA) → memory order book → DRAM/HBM cycle support.
  - Conversely a hyperscaler CapEx cut warning → downstream HBM order moderation 6-12 months out → bearish for the cycle peak timing.
  - DEMAND-DESTRUCTION INTENSIFICATION → PEAK-CALLER TRANSMISSION (the chain that actually times the stock peak): OEM price pass-through (device makers — Apple, Microsoft, Sony — raising end-prices or cutting memory content to offset memory costs) is the LEADING edge of demand destruction. It transmits forward as: price pass-through → sell-side models the revenue/EPS hit to the memory makers → FIRST SK Hynix/Micron estimate cut (t2_eps_revisions flips from all-up consensus to first dissent) → forward multiple compresses (t2_pe_compressed) → only LATER, capex moderation (t2_capex_parabolic). When demand-destruction news intensifies, say so AND name what to watch next on this chain — the estimate-revision flip is the next domino, NOT capex (capex is the last to turn; makers keep building until they can't deny it). Frame it forward: "demand destruction is intensifying at the end market via OEM price pass-through; watching for transmission into SK Hynix/Micron estimate revisions (first sell-side dissent) — capex moderation is the later, lagging confirmation." This keeps the brief predictive, not just a re-note of an already-fired signal.

  COMPETITIVE / SUBSTITUTION:
  - CXMT / ChangXin capacity milestone or yield improvement → commodity DDR4/DDR5 oversupply risk → Samsung/SK Hynix spot pricing pressure on those nodes. State the timing: short-term impact (1-3mo) on spot, medium-term (3-9mo) on contract.
  - Cerebras WSE-3 deployment news or in-house ASIC production milestone (TPU v7, Trainium 2, MTIA v3) → potential HBM substitution at the margin → captive-ratio risk to SK Hynix HBM revenue trajectory.

  PRICE-COMMENTARY LINKS:
  - TrendForce spot print + earnings commentary on contract / inventory → connect to t1_spot_decline, t2_spot_contract_spread, and t1_oem_inventory together rather than as isolated data points.

Inference style: "X implies Y because Z" rather than "X happened. Y happened." Make the causal mechanism visible — even one sentence is enough. The framework's job is connecting upstream and downstream signals, not just listing them.

LIMITS:
  - Draw inferences ONLY when both ends of the chain have evidence in today's brief or a recent (≤7d) carried-forward evidence entry from the indicator evidence store. Do not speculate from one end with no anchor at the other.
  - Inferences that are "common knowledge" without specific evidence in the brief don't belong in the brief — they belong in the framework documentation.
  - If you draw an inference, cite the evidence anchors for both ends inline.

OUTPUT FORMAT - respond with ONLY a JSON object:

{{
  "executive_summary": {{
{json_example_phase_line}
{json_example_phase_label}
    "tier_status_summary": "T1: X/6 fired | T2: Y/7 fired | T3: Z/5 fired | T4: W/4 fired",
    "new_triggers_today": [
      {{"tier": 2, "indicator_id": "...", "indicator_name": "...", "description": "..."}}
    ],
    "executive_brief": "5-8 sentence consolidated paragraph per EXECUTIVE BRIEF SPECIFICATION above. Lead with signal/indicator developments; summarize what's happening on the thesis (do NOT fabricate a driver for routine or broad/market-wide moves); bring in macro only when it has a channel into an indicator; no stale carryover. Use anchor links of the form [→§N] for cross-references.",
{json_example_case_block}
    "delta_from_prior_text": "Legacy field (v3.39.7.5: no longer rendered, but keep emitting for back-compat / observability). Short description of probability/peak-window shifts vs prior brief."
  }},
  "market_summary": "≤40 words on today's memory/semi market action",
  "key_news_summary": [
    {{"headline": "...", "url": "...", "why_it_matters": "≤25 words"}}
  ],
  "indicator_assessments": [
    {{
      "id": "t1_spot_decline",
      "tier": 1,
      "current_status": "Fired | Partially fired | Not fired",
      "where_we_stand": "≤50 words (≤90 for the 5 deep-dive ids below). Lead with the number/status, not narrative.",
      "latest_developments": "≤35 words. No inline URLs or markdown links — cite sources in relevant_links. If nothing new today, return an empty string — do NOT pad.",
      "relevant_links": [{{"title": "...", "url": "..."}}],
      "change_vs_prior": "ONLY if status changed vs prior; else null. Omit entirely when unchanged."
    }}
  ],
  "cycle_position_call": "≤80 words: where we are in cycle today, what changed since prior brief, what to watch next"
}}

v3.40.0 OUTPUT DISCIPLINE (do NOT emit `name`, `description`, or
`what_to_look_for` for any indicator — the system fills those from config;
emitting them wastes output tokens and they are discarded). Cap
`relevant_links` at 3 per indicator (most-relevant first; extras are dropped).
Respect the word caps above — they are hard limits, not targets. The status
pill and the numbers carry the signal; prose is supporting, not the product.

DEEP-DIVE DEPTH (v3.39.1, capped v3.40.0): these 5 indicator IDs may use up to
~90 words in `where_we_stand` (vs ≤50 for the rest) — fuller, but still tight.
The client synthesizes the "Top 5 deep dives" section by plucking these IDs
from indicator_assessments, so do NOT emit a separate top_5_deep_dives array
(was costing ~10K tokens and triggering output truncation):
  t1_spot_decline, t2_capex_parabolic, t2_eps_revisions,
  t3_supplier_inventory_rebuild, t3_relative_strength

{scenario_framing_block}

PROBABILITY UPDATE RULES (signal-conditional):

A. WHEN AN INDICATOR FIRES TODAY (Not fired → Partially fired, or → Fired):
- New Tier 2 trigger fired today: bear probability +3-6pp, base shortens
  peak_months_away by ~1mo and trims return_to_peak_pct by ~3-5pp. Bull -1-3pp.
- New Tier 3 trigger fired today: bear probability +5-8pp (still under base),
  base peak_months_away drops to ≤2mo, base return_to_peak_pct trims ~5-10pp.
  Bull case shrinks meaningfully (-3-5pp).
- New Tier 4 trigger: same magnitude as Tier 3 but with bear return_to_peak_pct
  going negative (e.g., -5 to -15pp) — confirms peak is in.
- Bull confirms (e.g., hyperscaler 2027 CapEx >2026): bull probability +3-5pp,
  bear -2-4pp, base nudges peak_months_away by ~+1mo.

B. WHEN AN INDICATOR UNFIRES TODAY (v3.39.7.3 — SYMMETRIC MIRROR OF RULE A):
Indicators going FIRED → Partially fired, or FIRED → Not fired, or Partially
fired → Not fired are EQUALLY material as new fires. Yesterday's signal
distribution is no longer valid. You MUST recalculate probabilities in the
OPPOSITE direction:
- Tier 1 indicator unfires today: bear probability -2-4pp, bull +1-2pp,
  base extends peak_months_away by ~1mo, base return_to_peak_pct restores
  by 2-3pp.
- Tier 2 indicator unfires today: bear -3-6pp, bull +2-4pp, base peak
  extends 1-2mo, return_to_peak_pct restores ~3-5pp.
- Tier 3 indicator unfires today: bear -5-8pp, bull +3-5pp, base peak
  extends 2-3mo, return_to_peak_pct restores ~5-10pp.
- Tier 4 indicator unfires today: bear -5-8pp (clamp at min 5%), bull +5-8pp,
  base peak extends 2-3mo, return_to_peak_pct restores ~5-15pp.

MANDATORY RECALCULATION INSTRUCTION (v3.39.7.3 — DO NOT SKIP):
Inertia is FORBIDDEN. If the count of {{Fired, Partially fired, Not fired}}
indicators differs from prior_state in ANY direction (more fires OR more
unfires OR mixed), you MUST recalculate base/bull/bear probabilities by
walking through each status change and applying rules A or B above. The
ONLY case where probabilities stay byte-identical to prior_state is when
EVERY single indicator's status is unchanged. Even one status change
requires a recalculation.

Reasoning template for delta_from_prior_text when status counts changed:
  "Indicator distribution shifted vs prior: {{fired_then}}→{{fired_now}} fired,
   {{partial_then}}→{{partial_now}} partial. Applied [rule A or B] for [list
   indicators with old→new status]; bear {{pp_change}}pp, bull {{pp_change}}pp,
   base peak {{month_change}}mo."

If the LLM is tempted to write "no probability distribution changes
warranted" because no NEW fires happened, this is a BUG — check for UNFIRES.
A reduction in fired count is equally probability-moving as an increase.

C. WHEN COMPUTED METRICS CONTRADICT LAYER 2 VERDICTS (v3.39.7.3, updated v3.39.7.9):
For indicators whose status is primarily driven by computed numerical
data (rel_strength_full, forward_pe, mean_capex_ocf_ratio_2q, eps_revisions
weighted scoring), the COMPUTED VERDICT is authoritative. If Layer 2
returns "Not fired" with evidence_count=0 BUT the computed data shows
the threshold is crossed, REJECT Layer 2 and use the computed verdict.
Explicitly note this in `where_we_stand`: "Computed verdict: Fired
(P/E 4.56x < 5x threshold). Layer 2 returned Not fired with evidence_count=0
but is overridden per v3.39.7.3 computed-metric authority rule."

D. BEAR CASE MAGNITUDE CALIBRATION (v3.39.7.4 — DO NOT SKIP):

The May 20 2026 brief produced bear `return_to_peak_pct` = -12% with 4 T1
+ 2 T2 indicators fired. This was empirically 3-4x too mild. Rules A/B/C
above govern PROBABILITY shifts; this block governs MAGNITUDE.

The bear case is calibrated to historical SK Hynix peak-to-trough drawdowns
in completed memory cycles:
- 2014 NAND mini-cycle: -36%
- 2018 DRAM cycle: -43%
- 2022 DRAM/NAND cycle: -49%
- AVERAGE: -43%

The bear case is NOT a small downward drift from base case. It is the
AVERAGE historical drawdown scaled by current signal density. As more
indicators fire, bear `return_to_peak_pct` must approach this historical
norm.

Bear `return_to_peak_pct` magnitude bands (apply ON TOP of A/B/C):

| Fired-indicator regime                  | Bear return_to_peak_pct |
|-----------------------------------------|-------------------------|
| 0-1 T1 + 0-2 T2 fired (early cycle)     | -10 to -20%             |
| 2-3 T1 OR 3-4 T2 (mid escalation)       | -25 to -35%             |
| 4+ T1 OR 5+ T2 (late, peak imminent)    | -35 to -45%             |
| Any T3 fired (post-peak confirmation)   | -40 to -50%             |
| Any T4 fired (cycle bottom approaching) | -45 to -55%             |

When you write bear `return_to_peak_pct`, refer to these bands directly.
If you find yourself writing a number above -20% with majority-T1 fired,
that is a BUG — go back and apply the band rules.

Anchoring instruction: cite the historical reference verbatim in
`delta_from_prior_text` when bear case changes magnitude band:
  "Bear case calibrated to historical average drawdown (-43%, mean of
   2014/-36%, 2018/-43%, 2022/-49% SK Hynix peak-to-trough)."

PEAK MONTH ESTIMATE RULES:
- Compare against prior_state.executive_summary.base_case.peak_months_away
- If shorter: "shortened by X months due to [specific trigger]"
- If longer: "extended by X months due to [specific trigger]"
- If unchanged: "Base case peak window unchanged vs yesterday"
- If first run: "No prior comparison available (first brief)"

URGENT ATTENTION TRIGGER RULES:
Set urgent_attention_needed=true if ANY of:
- A previously "Not fired" indicator becomes "Fired" or "Partially fired" today
- Multiple Tier 2/3 indicators changed status
- Big movers include SK Hynix, Micron, or Samsung with >4% move
- Probability distribution shifted significantly (e.g., bear case +10pp vs prior)

PRIMARY-SOURCE PRECEDENCE (v3.39.5):
When today's NEWS TRIAGE mentions a broker action (target price change,
rating change, estimate revision) that ALSO appears in DEEP-TIER BROKER
NOTES, the broker IS the primary source. Cite the broker (from the
TARGET PRICE ACTION line, e.g. "Mirae Asset Securities") in your
latest_developments and key_news_summary — NOT the Korean news
aggregator (Seoul Economic, AD HOC NEWS, abhs.in) that echoed the
broker's report. Use the broker's SOURCE URL (from the digest's raw_url
field), not the aggregator's URL.

MAJOR BROKER ACTIONS IN KEY NEWS (v3.39.5):
A target-price change of ≥10% or a rating change is material market-moving
news. If a DEEP-TIER BROKER NOTE shows a TARGET PRICE ACTION with such a
move, it MUST appear in your `key_news_summary` array — even if news
triage didn't surface it. Format the headline as: "{{broker}}
raises/lowers {{ticker}} target to {{new_pt}} from {{prior_pt}}". Use
the broker's ANALYTICAL EXCERPT lines to write the why_it_matters field.

NUMBER-FIRST INDICATOR FORMAT (v3.39.6 — STRICT, length cap v3.4.1):
For every indicator_assessments[] entry, the `where_we_stand` field
MUST follow this template when the indicator has computed numerical
evidence (rel-strength spreads, hyperscaler CapEx/OCF ratios, forward P/E,
EPS revisions, single-day moves, etc.):

  1. LEAD with the exact numbers. Quote them from COMPUTED METRICS.
     Example: "5-day vs SOX: SK Hynix -3.21pp, Micron -2.84pp, DRAM
     ETF vs SMH -1.10pp. Severity: None (yellow threshold -5pp,
     red threshold -10pp — neither crossed)."
  2. THEN a SHORT trend/context note (≤1/3 of the words). Example:
     "Spread has widened ~2pp this week as memory drawdowns
     accelerated; would cross yellow if today's relative weakness
     persists 2 more sessions as the May 11-12 positive days roll off."
  3. NO speculative "almost certainly past threshold" or "highly
     probable when measured" — the metric IS measured. If you don't
     see it in COMPUTED METRICS, say "(not threaded)" — do NOT
     reason backward from news headlines.

**HARD CAP: 120 WORDS per where_we_stand field** (v3.4.1).
Per the May 21 2026 audit, where_we_stand was sometimes empty (notably
t2_capex_parabolic on May 21) because Sonnet was hitting its
max_tokens budget late in the output and silently truncating fields.
The cap is two-sided protection:
  - Each individual where_we_stand must be ≤120 words so the schema
    fits comfortably in the token budget across all 23 indicators.
  - For indicators with rich evidence (t2_capex_parabolic regularly
    has 5+ named announcements in the evidence store), use the cap
    to PRIORITIZE — pick the 2-3 most material items, name them with
    dollar amounts, link the source. Don't try to list everything;
    that's what the evidence store is for.

CRITICAL — NEVER LEAVE where_we_stand EMPTY (v3.4.1 Fix 5):
If today's news has no fresh update on this indicator, where_we_stand
must STILL be populated with the carried-forward evidence. Pull from
LAYER 2 PER-SIGNAL VERDICTS or the evidence_store entries for that
indicator and write a 1-2 sentence summary of "where we currently
stand based on accumulated evidence" — what's fired, since when, and
the dollar amounts/dates that drove it. Example for t2_capex_parabolic
when today has no new CapEx print: "Fired since Mar 2025: $725B aggregate
2026 hyperscaler CapEx (MSFT+GOOGL+AMZN+META) confirmed via Q4 2025
earnings cycle, latest NVIDIA Q1 FY27 $81.6B revenue with $1T cumulative
hyperscaler CapEx by 2027 (Tom's Hardware [→§5]) reinforces the trend."
NEVER emit empty or sub-30-character where_we_stand — that's a bug.

`latest_developments` should be similarly tight: 2-4 sentences max
with embedded links, NOT multi-paragraph essays. The reader scans
this column on mobile; brevity is the design intent.

For indicator status (`current_status`, `change_vs_prior`), DEFER
to LAYER 2 PER-SIGNAL VERDICTS when present. Haiku already weighed
the full evidence for that one signal — re-deriving the verdict in
Sonnet's prose risks contradicting Layer 2 and looks bad in the
dashboard where Layer 2 status renders alongside your text.

Cycle baseline (use as your prior, adjust per today's evidence):
{json.dumps(CYCLE_BASELINE, indent=2)}

Top 5 indicators for deep dives: {", ".join(TOP_5_INDICATORS)}

NOW THE DATA:

{perf_text}
{movers_text}
{est_text}
{indicator_text}
{prior_text}
{digest_text}
{computed_text}
{layer2_text}
{qoq_text}
{triage_text}
{earnings_events_text}
"""
    return prompt


def _news_fallback_from_triage(
    triage: NewsTriage,
    news_items: Optional[list[NewsItem]] = None,
    top_n: int = 8,
) -> list[dict]:
    """Build a Section-5 news list from Haiku-triaged items.

    v3.41.7 — safety net for when Sonnet's curated `key_news_summary` comes
    back empty. The dominant cause is a malformed-JSON break (unterminated
    string) mid-output: json_repair salvages the fields BEFORE the break and
    drops everything after it, and key_news_summary sits late in the object,
    so it is the first casualty. Rather than render an empty news section when
    relevant news plainly exists, fall back to the Haiku triage's
    `relevant_news` — already filtered to the memory/semi-relevant subset with
    a one-line takeaway per item, so this is faithful, not degraded. Prefers
    triage.relevant_news; last-resorts to the raw feed.
    """
    out: list[dict] = []
    for item in (getattr(triage, "relevant_news", None) or [])[:top_n]:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "headline": title,
            "url": (item.get("url") or "").strip() or "#",
            "why_it_matters": (item.get("summary") or "").strip(),
        })
    if out:
        return out
    for n in (news_items or [])[:top_n]:
        title = (getattr(n, "title", "") or "").strip()
        if not title:
            continue
        out.append({
            "headline": title,
            "url": (getattr(n, "url", "") or "").strip() or "#",
            "why_it_matters": "",
        })
    return out


def sonnet_analyze(
    api_key: str,
    triage: NewsTriage,
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    big_movers: list[BigMover],
    prior_state: dict,
    collector: Optional[DiagnosticsCollector] = None,
    digests: Optional[list[dict]] = None,
    signal_reasonings: Optional[list] = None,
    computed_metrics: Optional[dict] = None,
    news_items: Optional[list[NewsItem]] = None,
    phase_state: Optional[dict] = None,
    transcript_diffs: Optional[list[dict]] = None,
    authoritative_24h_log: Optional[list[dict]] = None,
) -> Optional[BriefAnalysis]:
    """Run Sonnet to produce the full analysis with exec summary.

    v3.39.5: Accepts `digests` (deep-tier Layer 1 output).
    v3.39.6: Accepts `signal_reasonings` (Layer 2 Haiku verdicts) and
    `computed_metrics` (numerical deterministic verdicts). Threaded into
    the prompt so Sonnet quotes exact numbers and defers to Layer 2 on
    per-signal status, rather than speculating from news headlines.
    v3.39.7.6: Accepts `news_items` (raw NewsItem list). Threaded into
    `build_sonnet_analysis_prompt` so triage items in the Sonnet prompt
    carry their published timestamps — required for the RECENCY ON
    CONTRADICTING EVENTS prompt rule to be actionable.
    v3.4.11: Accepts `phase_state` (deterministic phase detector output).
    Threaded into the prompt so Sonnet swaps to Phase 5 framework
    semantics ('return to trough', negative base case) when the detector
    has flipped us into Phase 5.
    v3.4.12: Accepts `transcript_diffs` (Haiku QoQ diffs vs prior quarter
    per company). Threaded into the prompt so Sonnet narrates language
    shifts and CFO commitment drops as cycle-relevant signals rather than
    reacting to each call in isolation.
    """
    if not api_key:
        if collector is not None:
            collector.record_skipped(
                f"Anthropic API / {SONNET_MODEL} (stage 2 analysis)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
        return None

    client = Anthropic(api_key=api_key)
    prompt = build_sonnet_analysis_prompt(
        triage, performance_data, estimates_data, big_movers, prior_state,
        digests=digests,
        signal_reasonings=signal_reasonings,
        computed_metrics=computed_metrics,
        news_items=news_items,
        phase_state=phase_state,
        transcript_diffs=transcript_diffs,
        authoritative_24h_log=authoritative_24h_log,
    )

    # ---- v3.41.2 prose cache: decide reuse vs regenerate BEFORE Sonnet runs ----
    # Restrict Sonnet to writing only the cards whose verdict shape changed (or
    # are due a weekly refresh); dormant cards are filled from cache after parse.
    # Safe-failing: ANY error disables the cache for this run (regenerate all),
    # so the brief always ships unchanged from legacy behavior.
    _pc_cache = None
    _pc_reuse_ids: set = set()
    _pc_regen_ids: set = set()
    _pc_reset_ids: set = set()
    _pc_reasonings = signal_reasonings or []
    if USE_PROSE_CACHE and _pc_reasonings:
        try:
            _pc_cache = load_prose_cache()
            _reasoning_ids = [getattr(r, "signal_id", None) for r in _pc_reasonings]
            _reasoning_ids = [i for i in _reasoning_ids if i]
            seed_staggered_dates(_pc_cache, _reasoning_ids)
            _decisions = evaluate_cache(_pc_cache, _pc_reasonings)
            _status_by_id = {
                getattr(r, "signal_id", None): getattr(r, "fired_status", None)
                for r in _pc_reasonings
            }
            apply_status_guard(_decisions, _pc_cache, _status_by_id)
            # v3.41.6 — un-freeze dormant cards that got fresh evidence today
            apply_news_freshness_guard(_decisions, _pc_cache, _pc_reasonings)
            _pc_regen_ids, _pc_reset_ids = split_regen_ids(_decisions)
            _pc_reuse_ids = {
                i for i, d in _decisions.items()
                if d.get("action") == "reuse" and get_cached_assessment_dict(_pc_cache, i)
            }
            # any "reuse" whose cache turned out unusable → force regen instead
            for i, d in _decisions.items():
                if d.get("action") == "reuse" and i not in _pc_reuse_ids:
                    _pc_regen_ids.add(i)
            logger.info(f"[prose-cache] {summarize_decisions(_decisions)}")
            # Inject the scope restriction ONLY when it actually saves output
            # (some cards are being reused). The replace() is a no-op if the
            # anchor ever changes → degrades safely to "write all" (no savings).
            if _pc_reuse_ids:
                prompt = prompt.replace(
                    "OUTPUT FORMAT - respond with ONLY a JSON object:",
                    _build_scope_instruction(_pc_regen_ids)
                    + "OUTPUT FORMAT - respond with ONLY a JSON object:",
                    1,
                )
        except Exception as e:
            logger.warning(f"[prose-cache] disabled this run (non-fatal): {e}")
            _pc_cache = None
            _pc_reuse_ids, _pc_regen_ids, _pc_reset_ids = set(), set(), set()

    # v3.39: switch from messages.create to messages.stream. Two motivations:
    #
    # (1) Bypass the SDK's client-side wall-time guard. messages.create raises
    #     "Streaming is required for operations that may take longer than 10
    #     minutes" when SDK-estimated throughput * max_tokens exceeds 600s.
    #     That threshold fired in the 16:25 May 17 run at max_tokens=24000,
    #     prompting v3.38.5 to lower the cap to 16000 — which caused the
    #     truncation seen in the 17:43 run (output cut at ~33K chars / ~16K
    #     tokens mid-string; json_repair recovered only 12/22 indicators,
    #     dropping Tier 3, Tier 4, top-5, and selected_news sections from
    #     the email). Streaming has no such guard.
    #
    # (2) Stability under transient 503s. The 17:43 run hit one Anthropic
    #     503 mid-call that the SDK auto-retried, but a streaming call lets
    #     us observe partial progress and surfaces network issues earlier.
    #
    # We accumulate chunks into a final message via stream.get_final_message().
    # The SDK exposes .content[0].text on that object identical to the
    # non-streaming path, so downstream parsing is unchanged.

    # v3.4.2 Fix A — reliability hardening for 529 Overloaded errors.
    # The May 22 03:50 run hit Anthropic API overload: Sonnet got 529 on
    # first attempt, SDK retried 2x with 0.4s/0.8s backoff (~3s total),
    # all 3 failed, fallback analysis triggered, degraded brief shipped.
    # The API was overloaded for far longer than 3 seconds; SDK backoff
    # is insufficient for sustained overload windows.
    #
    # Three-tier strategy:
    #   1. SDK built-in retry (handled by anthropic client) for transient
    #      single-call failures
    #   2. App-level retry: 3 attempts at 30s / 90s / 240s = up to ~6min
    #      additional backoff budget for sustained 529 overload
    #   3. Final fallback: switch to Haiku 4.5 for synthesis if all
    #      Sonnet attempts fail. Lower narrative quality but produces a
    #      populated brief with all sections including DHCR card
    SONNET_RETRY_BACKOFFS = [30, 90, 240]  # seconds; total ~6min worst case
    HAIKU_FALLBACK_MODEL = "claude-haiku-4-5-20251001"

    def _call_sonnet_with_streaming(model_to_use: str):
        """Wrap the streaming call so we can retry with backoff."""
        if collector is not None:
            with collector.track(
                f"Anthropic API / {model_to_use} (stage 2 analysis, streaming)",
                "llm",
            ) as t:
                with client.messages.stream(
                    model=model_to_use,
                    max_tokens=SONNET_MAX_TOKENS,
                    temperature=LLM_TEMPERATURE,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    resp = stream.get_final_message()
                t.items_returned = 1
                t.detail = f"prompt: {len(prompt)} chars, max_tokens={SONNET_MAX_TOKENS}, model={model_to_use}"
                return resp
        else:
            with client.messages.stream(
                model=model_to_use,
                max_tokens=SONNET_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                return stream.get_final_message()

    # v3.40.0 — Message Batches path for Layer 3 (50% token cost). Submit the
    # single Sonnet call as a batch of one and poll up to the cap. On success,
    # use that response and skip the synchronous streaming path. On any
    # failure/timeout, `response` stays None and we fall through to the proven
    # synchronous streaming path below (which keeps its own 529 retry + Haiku
    # fallback). No prompt caching here — a once-daily single call has no
    # cross-call cache reuse within the TTL, so caching would only add a write
    # premium.
    response = None
    last_error = None

    if USE_BATCH_API:
        try:
            from src.analysis.batch_client import run_batch
            _l3_requests = [{
                "custom_id": "layer3",
                "params": {
                    "model": SONNET_MODEL,
                    "max_tokens": SONNET_MAX_TOKENS,
                    "temperature": LLM_TEMPERATURE,
                    "messages": [{"role": "user", "content": prompt}],
                },
            }]
            _succeeded, _needs_fallback = run_batch(
                client, _l3_requests, label="Layer 3",
                poll_cap_seconds=BATCH_POLL_CAP_SECONDS,
            )
            if _succeeded.get("layer3") is not None:
                response = _succeeded["layer3"]
                logger.info("Layer 3 synthesis via batch (50% token cost)")
        except Exception as e:
            logger.warning(f"Layer 3 batch attempt failed ({e!r}); using synchronous streaming")
            response = None

        # v3.40.2 — diagnostic recording moved OUT of the batch-acquisition try
        # above and into its own non-fatal block. ROOT CAUSE of the v3.40.1
        # Sonnet double-billing: the old code called collector.record() here
        # with a signature it does not have (positional name/type/status +
        # detail= + items_returned=); the real API is record(SourceFetchStatus).
        # The resulting TypeError was caught by the batch `except` above, which
        # set response=None and triggered a FULL-PRICE synchronous re-run — on
        # top of the batch call that had ALREADY succeeded and been billed
        # (~2x Sonnet cost every run). Now: correct SourceFetchStatus, and
        # isolated so a bookkeeping error can NEVER discard an already-paid
        # batch result and re-run Sonnet.
        if response is not None and collector is not None:
            try:
                collector.record(SourceFetchStatus(
                    source_name=f"Anthropic API / {SONNET_MODEL} (stage 2 analysis, BATCH)",
                    source_type="llm",
                    reached=True,
                    items_returned=1,
                    latency_ms=0,
                    detail=f"prompt: {len(prompt)} chars, max_tokens={SONNET_MAX_TOKENS}, batched (50% cost)",
                ))
            except Exception as _rec_err:
                logger.warning(f"Layer 3 batch diagnostic record failed (non-fatal): {_rec_err}")

    # Attempt 1 (synchronous streaming) — only if the batch path above did not
    # already produce a response.
    if response is None:
      try:
        response = _call_sonnet_with_streaming(SONNET_MODEL)
      except Exception as e:
        last_error = e
        err_str = str(e).lower()
        is_overloaded = "529" in err_str or "overloaded" in err_str
        if is_overloaded:
            logger.warning(
                f"Sonnet streaming attempt 1 failed (overloaded): {e}. "
                f"Entering app-level retry-with-backoff."
            )
            import time as _time
            for attempt_idx, backoff_seconds in enumerate(SONNET_RETRY_BACKOFFS, start=2):
                logger.info(
                    f"Sonnet retry attempt {attempt_idx}/{len(SONNET_RETRY_BACKOFFS)+1}: "
                    f"sleeping {backoff_seconds}s before next try"
                )
                _time.sleep(backoff_seconds)
                try:
                    response = _call_sonnet_with_streaming(SONNET_MODEL)
                    logger.info(f"Sonnet attempt {attempt_idx} succeeded after backoff")
                    break
                except Exception as retry_e:
                    last_error = retry_e
                    err_str = str(retry_e).lower()
                    if "529" not in err_str and "overloaded" not in err_str:
                        logger.error(f"Sonnet attempt {attempt_idx} failed with non-overload error: {retry_e}")
                        break
                    logger.warning(f"Sonnet attempt {attempt_idx} still overloaded: {retry_e}")
        else:
            logger.error(f"Sonnet streaming failed with non-overload error: {e}")

    # Final fallback: Haiku 4.5 if all Sonnet attempts failed
    if response is None:
        logger.warning(
            f"All Sonnet attempts failed. Falling back to {HAIKU_FALLBACK_MODEL} "
            f"for synthesis. Brief will ship with lower narrative quality but "
            f"all sections (including DHCR matrix) populated. Last error: {last_error}"
        )
        try:
            import time as _time
            _time.sleep(30)  # give the API a moment to recover before fallback
            response = _call_sonnet_with_streaming(HAIKU_FALLBACK_MODEL)
            logger.info(
                f"Haiku fallback succeeded — degraded but populated brief will ship"
            )
        except Exception as fallback_e:
            logger.error(
                f"Haiku fallback also failed: {fallback_e}. Returning None — "
                f"fast_tier will trigger the analysis fallback path."
            )
            return None

    # v3.39.1: surface Sonnet token usage in the run log. The 20:30 May 17
    # run truncated silently — without this log we had to infer cap-hit
    # from json_repair recovery counts. With usage logged, we can compare
    # output_tokens directly to SONNET_MAX_TOKENS and know immediately if
    # we're capping out.
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_t = getattr(usage, "input_tokens", 0)
            out_t = getattr(usage, "output_tokens", 0)
            cap_hit = " (HIT MAX_TOKENS CAP)" if out_t >= SONNET_MAX_TOKENS - 100 else ""
            logger.info(
                f"Sonnet usage: {in_t:,} input + {out_t:,} output tokens"
                f" (max_tokens={SONNET_MAX_TOKENS:,}){cap_hit}"
            )
    except Exception:
        pass  # usage logging is non-essential; don't let it fail the run

    raw = _strip_code_fence(response.content[0].text)
    # v3.38.4: same json_repair fallback pattern as Haiku triage. Sonnet
    # produces a deeper nested structure (22 indicator assessments + exec
    # summary + scenarios + news synthesis) so it has more surface area for
    # malformed-string failures. If json_repair returns an empty dict or
    # something unrecognizable, bail to fallback_analysis (which IS now
    # callable post v3.38.3 — the kwarg fix at fast_tier.py:399).
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            f"Sonnet JSON parse failed at {e.msg} (char {e.pos}); "
            f"attempting json_repair recovery..."
        )
        logger.debug(f"Raw response around fail point: ...{raw[max(0, e.pos-200):e.pos+200]}...")
        try:
            parsed = json_repair.loads(raw)
            if not isinstance(parsed, dict) or not parsed:
                logger.error(
                    f"json_repair returned {type(parsed).__name__} (empty or wrong type) — "
                    f"Sonnet analysis unrecoverable"
                )
                return None
            n_indicators = len(parsed.get("indicator_assessments", []))
            has_exec = bool(parsed.get("executive_summary"))
            logger.info(
                f"json_repair recovered Sonnet analysis: "
                f"{n_indicators}/22 indicators, exec_summary present={has_exec}"
            )
        except Exception as repair_err:
            logger.error(
                f"json_repair also failed: {repair_err}; "
                f"Sonnet analysis unrecoverable, will use fallback"
            )
            return None

    exec_data = parsed.get("executive_summary", {})
    # v3.39: build CaseScenario via the migration-aware helper (handles
    # legacy upside_to_peak_pct from older prior_state.json files), then
    # apply invariants so probabilities sum to 100 and base is the strict
    # maximum. See _enforce_scenario_invariants for the full rule set.
    base_raw = _build_case_scenario(exec_data.get("base_case"))
    bull_raw = _build_case_scenario(exec_data.get("bull_case"))
    bear_raw = _build_case_scenario(exec_data.get("bear_case"))
    # v3.4.11 — invariant enforcer is now phase-conditional. Sonnet may emit
    # current_phase=5 once the prompt receives PHASE_5_ACTIVE context (set
    # by the deterministic detector upstream in fast_tier). In Phase 5,
    # base.return_to_peak_pct can go negative; the field is reinterpreted
    # as 'return to trough'.
    parsed_phase = int(exec_data.get("current_phase", 4))
    base_fixed, bull_fixed, bear_fixed = _enforce_scenario_invariants(
        base_raw, bull_raw, bear_raw, current_phase=parsed_phase,
    )
    # Log any non-trivial correction so it's visible during debugging.
    if (
        abs(base_fixed.probability_pct - base_raw.probability_pct) > 0.5
        or abs(bull_fixed.probability_pct - bull_raw.probability_pct) > 0.5
        or abs(bear_fixed.probability_pct - bear_raw.probability_pct) > 0.5
    ):
        logger.info(
            f"Scenario invariants applied: "
            f"base {base_raw.probability_pct:.0f}→{base_fixed.probability_pct:.0f}, "
            f"bull {bull_raw.probability_pct:.0f}→{bull_fixed.probability_pct:.0f}, "
            f"bear {bear_raw.probability_pct:.0f}→{bear_fixed.probability_pct:.0f}"
        )
    exec_summary = ExecutiveSummary(
        current_phase=exec_data.get("current_phase", 4),
        current_phase_label=exec_data.get("current_phase_label", ""),
        tier_status_summary=exec_data.get("tier_status_summary", ""),
        new_triggers_today=exec_data.get("new_triggers_today", []),
        base_case=base_fixed,
        bull_case=bull_fixed,
        bear_case=bear_fixed,
        delta_from_prior_text=exec_data.get("delta_from_prior_text", ""),
        # v3.39.7.5 — URGENT banner killed; fields kept on dataclass for back-
        # compat with prior_state.json but Sonnet no longer populates them
        # and template no longer renders them.
        urgent_attention_needed=False,
        urgent_attention_text="",
        # v3.39.7.5 — consolidated exec paragraph replacing the old 3-block
        # pattern (delta prose / big movers / today's market). Anchor links
        # of the form `[→§N]` are substituted into clickable HTML at render.
        executive_brief=_strip_internal_jargon(exec_data.get("executive_brief", "")),
    )

    # v3.39.1: defensive construction of IndicatorAssessment objects from the
    # (possibly json_repair-recovered) parsed dict. The 20:30 May 17 run had
    # Sonnet truncate mid-14th-indicator, json_repair recovered 14 dicts but
    # the 14th was missing `latest_developments` — the prior code did
    # `IndicatorAssessment(**a)` which raised TypeError on the missing
    # required field, crashing the whole pipeline (no email sent, GitHub
    # issue auto-created). Helper fills every required field with a safe
    # default so partial dicts produce a usable (if incomplete) row instead
    # of crashing. Indicators without an `id` are dropped entirely.
    #
    # Separately: top_5_deep_dives no longer requested from Sonnet — it was
    # duplicating 5 indicators with richer content, costing ~10K output
    # tokens. Now synthesized client-side by plucking the TOP_5_INDICATORS
    # IDs from indicator_assessments. The deep-dive ranking is fixed in
    # config (TOP_5_INDICATORS), so client-side synthesis preserves intent.
    raw_assessments = parsed.get("indicator_assessments", [])
    assessments = [
        _safe_indicator_assessment(a)
        for a in raw_assessments
        if isinstance(a, dict) and a.get("id")
    ]
    if len(assessments) < len(raw_assessments):
        logger.warning(
            f"Dropped {len(raw_assessments) - len(assessments)} indicator(s) "
            f"with missing or invalid id from Sonnet output."
        )

    # ---- v3.41.2 prose cache: merge fresh (regen) + cached (dormant) cards ----
    # Build the full card set: dormant cards from cache, changed/due cards from
    # Sonnet (word-capped). A regen card missing from Sonnet's output falls back
    # to its cached prose rather than vanishing. Safe-failing → fresh-only.
    if _pc_cache is not None:
        try:
            assessments, _n_stopgap = _merge_prose_cards(
                _pc_reasonings, assessments, _pc_reuse_ids, _pc_cache
            )
            if _n_stopgap:
                logger.warning(
                    f"[prose-cache] {_n_stopgap} regen card(s) missing from Sonnet "
                    f"output; used cached prose as stopgap"
                )
            logger.info(
                f"[prose-cache] merged {len(assessments)} cards "
                f"({len(_pc_reuse_ids)} reused, {len(_pc_regen_ids)} regenerated)"
            )
        except Exception as e:
            logger.warning(f"[prose-cache] merge failed, using fresh-only (non-fatal): {e}")

    # Client-side top_5 synthesis from the (merged) list (no separate Sonnet call)
    top_5 = [a for a in assessments if a.id in TOP_5_INDICATORS]

    # v3.39.7.4 — deterministic tier_status_summary override.
    # Sonnet's self-counted exec summary string has historically diverged
    # from the per-card statuses in the body of the same brief (off-by-one
    # arithmetic in the LLM's prose). Recompute from the parsed
    # indicator_assessments and override. Log when Sonnet's value differed
    # so we have signal on the underlying prompt quality.
    sonnet_summary = exec_summary.tier_status_summary
    deterministic_summary = _compute_tier_status_summary(assessments)
    if sonnet_summary != deterministic_summary:
        logger.warning(
            f"tier_status_summary override (Sonnet→deterministic): "
            f"{sonnet_summary!r} → {deterministic_summary!r}"
        )
    exec_summary.tier_status_summary = deterministic_summary

    # ---- v3.41.2 prose cache: persist fresh prose + fingerprints ----
    # Stores prose for the regenerated ids (from the merged `assessments`) and
    # resets the weekly clock for clock_reset_ids; reused cards are left as-is.
    # Safe-failing — a cache write failure never blocks the brief.
    if _pc_cache is not None:
        try:
            update_cache_after_synthesis(
                _pc_cache, assessments, _pc_reasonings,
                regenerated_ids=_pc_regen_ids, clock_reset_ids=_pc_reset_ids,
            )
            save_prose_cache(_pc_cache)
        except Exception as e:
            logger.warning(f"[prose-cache] cache update/save failed (non-fatal): {e}")

    # v3.41.7 — Section-5 safety net. If Sonnet's key_news_summary is empty
    # (typically because a malformed-JSON break dropped it — it sits late in
    # the object), fall back to the Haiku-triaged relevant_news so the news
    # section is never blank when relevant news exists.
    _key_news = parsed.get("key_news_summary") or []
    if not _key_news:
        _key_news = _news_fallback_from_triage(triage, news_items)
        if _key_news:
            logger.warning(
                "[news-fallback] Sonnet key_news_summary empty/lost; "
                f"populated Section 5 from {len(_key_news)} Haiku-triaged "
                "items instead (likely a JSON-break dropping the field)."
            )

    return BriefAnalysis(
        executive_summary=exec_summary,
        big_movers=big_movers,
        indicator_assessments=assessments,
        top_5_deep_dives=top_5,
        market_summary=parsed.get("market_summary", ""),
        key_news_summary=_key_news,
        cycle_position_call=parsed.get("cycle_position_call", ""),
    )


# ============================================================
# Top-level pipeline
# ============================================================

# v3.41.10 — Section-4 card news feed. The Indicator 7 (Competitor) and
# Indicator 8 (ADR) cards render a deterministic "Latest developments" line
# from sub["recent_news"] — a field NOTHING in the pipeline ever populated
# (consumer with no producer since v3.4.4), so both cards printed the
# "No new developments today" filler on every brief ever sent, even when the
# same brief's exec summary and §5 carried an obviously relevant story
# (observed Jul 6: CXMT DDR5-8000+ validation in exec + §5, card 7 blank;
# the F-1/A amendment in §5, card 8 blank). This is the producer: it takes
# the Haiku triage's indicator_relevance map (news → indicator ids) and
# attaches the mapped items to the snapshot dicts, routing to sub-indicators
# by keyword with a card-level fallback so nothing mapped is dropped.

_COMPETITOR_SUB_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("7A", ("cxmt",)),
    ("7B", ("samsung",)),   # Samsung HBM4/4E threat — any Samsung-competitive item
    ("7C", ("micron",)),
    ("7D", ("hbf", "solidigm", "high bandwidth flash", "high-bandwidth flash")),
]

_ADR_SUB_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("8A", ("f-1", "f‑1", "424b", "edgar", "filing", "amendment", "prospectus",
            "listing", "nasdaq", "oversubscri", "spo")),
    ("8B", ("roadshow", "bookbuild", "book-build", "investor demand", "order book")),
    ("8C", ("priced at", "prices its", "pricing day", "offering priced")),
    ("8D", ("insider", "stake sale", "share sale by")),
]

# v3.41.11 — card-level SELECTORS for the keyword floor. When the relevance
# map yields nothing for a card (map lost to a break, or Haiku didn't tag),
# these decide which relevant_news items belong to the card at all. They are
# deliberately NARROW — unambiguous, card-specific vocabulary only. Bare
# "samsung"/"micron" are intentionally ABSENT: those names blanket the
# memory feed and would flood card 7 with earnings/pricing noise; they stay
# router-only (used to place an item already selected by relevance/keywords
# into a sub), never selectors.
_COMPETITOR_CARD_SELECTORS: tuple[str, ...] = (
    "cxmt", "hbf", "solidigm", "high bandwidth flash", "high-bandwidth flash",
)
_ADR_CARD_SELECTORS: tuple[str, ...] = (
    "f-1", "f‑1", "424b", "edgar", "prospectus", "adr",
    "nasdaq listing", "us listing", "secondary offering", "spo", "oversubscri",
    "roadshow", "bookbuild", "insider sale", "insider selling",
    "priced at", "offering priced",
)


def _route_to_sub(text: str, routes: list[tuple[str, tuple[str, ...]]]) -> Optional[str]:
    low = text.lower()
    for sub_id, keywords in routes:
        if any(k in low for k in keywords):
            return sub_id
    return None


def attach_triage_news_to_snapshots(
    triage: NewsTriage,
    news_items: Optional[list[NewsItem]],
    competitor_snapshot: Optional[dict],
    adr_snapshot: Optional[dict],
    per_sub_cap: int = 2,
    card_cap: int = 3,
) -> dict:
    """Populate sub/card `recent_news` on the two Section-4 snapshot dicts.

    Mutates the snapshot dicts in place (they are the same objects
    fast_tier later passes to build_email_html, so the attachment flows to
    the template with no signature changes). Items come from
    triage.relevant_news, selected via triage.indicator_relevance for
    t1_competitor_dynamics / t1_us_adr_event; each attaches as
    {"title", "url", "date"} with the date resolved from the raw NewsItem
    (matched by URL) when available. Quiet days — no mapped items — leave
    the snapshots untouched, so the template's filler line stays and is
    then true. Never raises: any malformed input degrades to a no-op.
    """
    by_index: dict[int, dict] = {}
    for item in (getattr(triage, "relevant_news", None) or []):
        idx = item.get("index")
        if isinstance(idx, int):
            by_index[idx] = item

    url_to_date: dict[str, str] = {}
    for n in (news_items or []):
        url = (getattr(n, "url", "") or "").strip()
        pub = getattr(n, "published", None)
        if url and pub:
            url_to_date[url] = str(pub)[:10]

    relevance = getattr(triage, "indicator_relevance", None) or {}
    stats: dict[str, dict] = {}

    def _attach(indicator_id: str, snapshot: Optional[dict],
                routes: list[tuple[str, tuple[str, ...]]],
                selectors: tuple[str, ...]) -> None:
        rel_indices = [
            i for i in (relevance.get(indicator_id) or []) if isinstance(i, int)
        ]
        # v3.41.11 — keyword floor. When the relevance map has nothing for
        # this card (map lost to an output break, or Haiku didn't tag),
        # select candidates deterministically from the already-triaged
        # relevant_news using the card's narrow selector vocabulary. This is
        # the path that would have pinned "SK Hynix $28B Nasdaq listing …
        # F-1/A filed" to card 8 on Jul 9 regardless of the broken map.
        kw_indices: list[int] = []
        if not rel_indices:
            for item in (getattr(triage, "relevant_news", None) or []):
                if not isinstance(item, dict):
                    continue
                text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
                if any(k in text for k in selectors):
                    idx = item.get("index")
                    if isinstance(idx, int):
                        kw_indices.append(idx)
        indices = rel_indices or kw_indices
        n_attached = 0
        if isinstance(snapshot, dict):
            subs = snapshot.get("subs") or []
            subs_by_id = {}
            for s in subs:
                if isinstance(s, dict):
                    sid = s.get("sub_id") or s.get("id")
                    if sid:
                        subs_by_id[str(sid)] = s
            seen_urls: set = set()
            for idx in indices:
                item = by_index.get(idx)
                if not item:
                    continue
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                url = (item.get("url") or "").strip()
                if url and url in seen_urls:
                    continue
                entry = {
                    "title": title,
                    "url": url,
                    "date": url_to_date.get(url, ""),
                }
                sub_id = _route_to_sub(f"{title} {item.get('summary', '')}", routes)
                target_sub = subs_by_id.get(sub_id) if sub_id else None
                if target_sub is not None:
                    bucket = target_sub.setdefault("recent_news", [])
                    if len(bucket) >= per_sub_cap:
                        continue
                    bucket.append(entry)
                else:
                    bucket = snapshot.setdefault("recent_news", [])
                    if len(bucket) >= card_cap:
                        continue
                    bucket.append(entry)
                if url:
                    seen_urls.add(url)
                n_attached += 1
        # v3.41.11 — ALWAYS log, including the zero case. The Jul 9 failure
        # was invisible precisely because this only spoke on success.
        logger.info(
            f"[card-news] {indicator_id}: relevance={len(rel_indices)} "
            f"keyword={len(kw_indices)} attached={n_attached}"
        )
        stats[indicator_id] = {
            "relevance": len(rel_indices),
            "keyword": len(kw_indices),
            "attached": n_attached,
        }

    try:
        _attach("t1_competitor_dynamics", competitor_snapshot,
                _COMPETITOR_SUB_ROUTES, _COMPETITOR_CARD_SELECTORS)
        _attach("t1_us_adr_event", adr_snapshot,
                _ADR_SUB_ROUTES, _ADR_CARD_SELECTORS)
    except Exception as e:  # never let card decoration break the analysis
        logger.warning(f"[card-news] attach failed (non-fatal): {e}")
    return stats


def analyze_with_llm(
    api_key: str,
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    news_items: list[NewsItem],
    big_movers: list[BigMover],
    prior_state: dict,
    collector: Optional[DiagnosticsCollector] = None,
    digests: Optional[list[dict]] = None,
    signal_reasonings: Optional[list] = None,
    computed_metrics: Optional[dict] = None,
    phase_state: Optional[dict] = None,
    transcript_diffs: Optional[list[dict]] = None,
    authoritative_24h_log: Optional[list[dict]] = None,
) -> Optional[BriefAnalysis]:
    """Two-stage LLM pipeline: Haiku triage -> Sonnet analysis.

    v3.39.5: New `digests` parameter (deep-tier Layer 1 broker notes +
    earnings transcripts).
    v3.39.6: New `signal_reasonings` (Layer 2 Haiku verdicts on each of the
    22 indicators) and `computed_metrics` (numerical verdicts: 5-day
    relative strength, hyperscaler CapEx ratios, forward P/E, DART
    materials). Threaded through so Sonnet quotes the exact numbers
    and defers to Layer 2 on per-signal status — fixing the May 18
    brief's t3_relative_strength prose that speculated "almost certainly
    past -10% threshold" from news headlines when the actual computed
    spread was below threshold.
    v3.4.11: New `phase_state` (output of phase_detector.evaluate_phase_for_run
    serialized via result_to_state_dict). Threaded into Sonnet's prompt
    so when current_phase == 5 the framework context + scenario framing +
    invariants flip to Decline semantics.
    v3.4.12: New `transcript_diffs` (Haiku QoQ diffs vs prior quarter per
    company, computed in the deep-tier digester and persisted to
    state/transcripts/{ticker}/{YYYY-QN}.json). Threaded into Sonnet's
    prompt so the model narrates language shifts (dropped commitments,
    new caveats, metric moves) as cycle-relevant signals.
    """
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set; falling back to deterministic synthesis")
        if collector is not None:
            collector.record_skipped(
                f"Anthropic API / {HAIKU_MODEL} (stage 1 triage)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
            collector.record_skipped(
                f"Anthropic API / {SONNET_MODEL} (stage 2 analysis)",
                "llm",
                "no ANTHROPIC_API_KEY set",
            )
        return None

    logger.info(f"Stage 1: Haiku triaging {len(news_items)} news items...")
    triage = haiku_triage_news(api_key, news_items, collector=collector)
    if triage is None:
        logger.warning("Haiku triage failed; using empty triage")
        triage = NewsTriage(relevant_news=[], broker_actions=[], trendforce_commentary=[], indicator_relevance={})
    else:
        logger.info(
            f"Haiku triage: {len(triage.relevant_news)} relevant news | "
            f"{len(triage.broker_actions)} broker actions | "
            f"{len(triage.trendforce_commentary)} price commentary items"
        )

    # v3.41.10 — feed the Section-4 deterministic cards (Indicator 7 / 8):
    # attach triage-mapped news to the competitor / ADR snapshot dicts so the
    # cards' "Latest developments" line reflects today's relevant items. The
    # dicts inside computed_metrics are the same objects fast_tier passes to
    # the template, so this flows to the email without signature changes; it
    # runs regardless of whether Sonnet later succeeds.
    if computed_metrics:
        attach_triage_news_to_snapshots(
            triage,
            news_items,
            computed_metrics.get("competitor_snapshot"),
            computed_metrics.get("adr_snapshot"),
        )

    n_digests = len(digests) if digests else 0
    n_reasonings = len(signal_reasonings) if signal_reasonings else 0
    n_diffs = len(transcript_diffs) if transcript_diffs else 0
    has_cm = "yes" if computed_metrics else "no"
    phase_info = (
        f" | phase={phase_state.get('current_phase', '?')}"
        if phase_state else " | phase=?"
    )
    logger.info(
        f"Stage 2: Sonnet analysis (digests={n_digests} | "
        f"layer2_verdicts={n_reasonings} | computed_metrics={has_cm} | "
        f"qoq_diffs={n_diffs}{phase_info})..."
    )
    analysis = sonnet_analyze(
        api_key, triage, performance_data, estimates_data, big_movers, prior_state,
        collector=collector,
        digests=digests,
        signal_reasonings=signal_reasonings,
        computed_metrics=computed_metrics,
        news_items=news_items,
        phase_state=phase_state,
        transcript_diffs=transcript_diffs,
        authoritative_24h_log=authoritative_24h_log,
    )
    return analysis


def fallback_analysis(
    performance_data: list[PerformanceMetrics],
    estimates_data: dict[str, EstimateSnapshot],
    news_items: list[NewsItem],
    big_movers: list[BigMover],
) -> BriefAnalysis:
    """Deterministic fallback if the LLM is unavailable."""
    assessments = []
    for ind in INDICATORS:
        assessments.append(IndicatorAssessment(
            id=ind.id,
            tier=ind.tier,
            name=ind.name,
            description=ind.description,
            what_to_look_for=ind.what_to_look_for,
            current_status="Unknown (LLM unavailable)",
            where_we_stand="Automated classification unavailable; please review manually.",
            latest_developments="See news feed below.",
            relevant_links=[],
            change_vs_prior=None,
        ))

    top_5 = [a for a in assessments if a.id in TOP_5_INDICATORS]

    news_summary = []
    for n in news_items[:10]:
        news_summary.append({
            "headline": n.title,
            "url": n.url,
            "why_it_matters": "Auto-classification unavailable.",
        })

    exec_summary = ExecutiveSummary(
        current_phase=CYCLE_BASELINE["current_phase"],
        current_phase_label=CYCLE_BASELINE["current_phase_label"],
        tier_status_summary="LLM unavailable; manual review needed",
        new_triggers_today=[],
        base_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["base_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["base_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            return_to_peak_pct=CYCLE_BASELINE["base_case_return_to_peak_pct"],
        ),
        bull_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["bull_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["bull_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            return_to_peak_pct=CYCLE_BASELINE["bull_case_return_to_peak_pct"],
        ),
        bear_case=CaseScenario(
            probability_pct=CYCLE_BASELINE["bear_case_probability_pct"],
            peak_months_away=CYCLE_BASELINE["bear_case_peak_months_away"],
            peak_window_start="(LLM unavailable)",
            peak_window_end="(LLM unavailable)",
            return_to_peak_pct=CYCLE_BASELINE["bear_case_return_to_peak_pct"],
        ),
        delta_from_prior_text="LLM unavailable - cannot compute deltas",
        urgent_attention_needed=False,
        urgent_attention_text="",
        # v3.39.7.5 — fallback exec brief when Sonnet is unavailable
        executive_brief=(
            "LLM analysis is unavailable for this brief; please review raw data manually. "
            f"Performance data captured for {len(performance_data)} tickers — see §1 for the price table. "
            "Indicator statuses default to 'Unknown' in §3; tier counts and trigger event detection "
            "require the Sonnet path. Today's news feed is summarized in §5."
        ),
    )

    return BriefAnalysis(
        executive_summary=exec_summary,
        big_movers=big_movers,
        indicator_assessments=assessments,
        top_5_deep_dives=top_5,
        market_summary=f"Performance data captured for {len(performance_data)} tickers. LLM analysis unavailable.",
        key_news_summary=news_summary,
        cycle_position_call="Cycle position synthesis unavailable in fallback mode.",
    )


def identify_big_movers(
    performance_data: list[PerformanceMetrics],
    threshold_pct: float = 4.0,
) -> list[BigMover]:
    """Identify tickers with absolute 1D move >= threshold."""
    movers = []
    for p in performance_data:
        if p.change_1d is None:
            continue
        if abs(p.change_1d) >= threshold_pct:
            movers.append(BigMover(
                name=p.name,
                ticker=p.ticker,
                change_1d_pct=p.change_1d,
                direction="up" if p.change_1d > 0 else "down",
            ))
    movers.sort(key=lambda m: abs(m.change_1d_pct), reverse=True)
    return movers


# ============================================================
# Helpers
# ============================================================

def _strip_code_fence(text: str) -> str:
    """Strip markdown code fences from an LLM response.

    Handles three patterns:
      1. Raw JSON (no fence)               → returned unchanged
      2. ```json\\n{...}\\n``` (closed)    → inner content
      3. ```json\\n{...  (unclosed)        → body after the opening marker

    v3.38.5: the previous implementation had a bug — on closed fences,
    `text.split("```", 2)[-1]` returned the empty string AFTER the closing
    fence (not the middle content), causing `json.loads` to fail at char 0
    with "Expecting value", which then fed an empty string to json_repair
    (which returned `""` rather than a dict). This was the actual root cause
    of the 16:25 UTC May 17 Haiku failure, not a prompt issue.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    # Strip opening fence + optional language tag, up to the first newline.
    first_nl = text.find("\n")
    if first_nl >= 0:
        text = text[first_nl + 1:]
    else:
        text = text[3:]
        if text.startswith("json"):
            text = text[4:]
    # Strip trailing fence if present (closed-fence case).
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3].rstrip()
    return text


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


# ============================================================================
# v3.39.7 — Cycle matrix Haiku extraction
# ============================================================================
# Two extraction tasks consumed by data/dram_contract_*.csv:
#   1. extract_dram_signal_from_article — tone/segment/supplier/qoq fields
#      from any DRAM news article → dram_contract_signals.csv
#   2. extract_dram_forecasts_from_press_release — quarterly forecasts
#      from TrendForce press releases → dram_contract_quarterly_forecasts.csv
#
# Both use HAIKU_MODEL (already imported above) and the existing Anthropic
# client pattern. Pre-filtered by title in fast_tier.py to limit cost.
# ============================================================================

import re as _re_v3397
from datetime import date as _date_v3397


SIGNAL_EXTRACTION_SYSTEM_PROMPT_V3397 = """You are a structured-data extractor for DRAM industry news. Your job is to read a news article and return a single JSON object describing what it says about DRAM contract pricing momentum.

Output strictly valid JSON. No preamble, no markdown fences. The object must have exactly these fields:

{
  "tone_direction": one of ["rising", "stable", "softening", "cooling", "declining"] or null,
  "tone_keywords": array of exact phrases from the article that informed the tone judgment (max 5),
  "segment_mentioned": one of ["server_ddr5", "pc_dram", "hbm", "mobile_dram", "graphics_dram", "nand_flash", "general"] or null,
  "qoq_pct_mentioned": number (explicit QoQ % cited) or null,
  "qoq_quarter_mentioned": string like "Q1_2026", "Q2_2026" or null,
  "supplier_mentioned": one of ["samsung", "sk_hynix", "micron", "kingston", "transcend", "trendforce", "multiple"] or "" if none,
  "is_quarterly_forecast": true if the article cites a forward-looking quarterly forecast with explicit % range, else false,
  "forecast_low_pct": number or null (only if is_quarterly_forecast),
  "forecast_high_pct": number or null (only if is_quarterly_forecast)
}

Rules:
- tone_direction reflects DRAM PRICING MOMENTUM specifically:
  * "rising" = prices increasing, supply tightening, demand strong, suppliers raising
  * "stable" = flat, sustained, balanced, holding
  * "softening" = momentum slowed, modest deceleration, mild pullback
  * "cooling" = cooling negotiations, ending seller's market, transactions slowing
  * "declining" = prices falling, demand destruction, price cuts failing
- tone_keywords are EXACT verbatim phrases from the article, not paraphrases.
- segment_mentioned reflects the PRIMARY segment. "general" if mixed.
- qoq_pct_mentioned: only include if a specific % cited. If a range is given, use midpoint AND populate forecast_low_pct/high_pct.
- is_quarterly_forecast: TRUE only if a forward-looking forecast is TIED TO A SPECIFIC QUARTER.
- supplier_mentioned: most prominently discussed supplier. "multiple" if equally featured. "" if none singled out.
- Return strict JSON parseable by json.loads(). No trailing commas, no markdown."""


FORECAST_EXTRACTION_SYSTEM_PROMPT_V3397 = """You are a structured-data extractor for TrendForce press releases announcing DRAM contract price forecasts.

A typical press release contains sentences like:
- "Conventional DRAM contract prices are expected to rise 58-63% QoQ in 2Q26"
- "Server DRAM prices are projected to surge by more than 60% QoQ"
- "1Q26 DRAM contract prices revised upward from 55-60% to 90-95%"

Extract every distinct quarterly forecast announcement into a JSON array. Output strictly valid JSON, no preamble or markdown:

{
  "forecasts": [
    {
      "quarter": "Q1_2026" | "Q2_2026" | "Q3_2026" | "Q4_2026",
      "segment": "conventional_dram_aggregate" | "server_ddr5" | "pc_dram" | "hbm" | "mobile_dram" | "graphics_dram" | "samsung_specific" | "sk_hynix_specific" | "micron_specific",
      "low_pct": number,
      "high_pct": number,
      "is_revision": true if explicitly described as a revision of a prior forecast, else false,
      "exact_quote": the exact sentence containing the forecast
    }
  ]
}

Rules:
- One forecast per (quarter, segment). Both initial and revised values for the same (quarter, segment) -> include both as separate entries with is_revision flagged appropriately.
- "conventional_dram_aggregate" applies to phrases like "conventional DRAM", general "DRAM contract prices" (not segment-specific). Use "server_ddr5" only when explicitly server/RDIMM/hyperscaler/AI-server.
- For ranges like "58-63%", low_pct=58, high_pct=63.
- For "more than 60%", treat as point estimate: low_pct=60, high_pct=60.
- If no forecasts present, return {"forecasts": []}.
- exact_quote should be the source sentence verbatim, useful for audit."""


_ALLOWED_TONE_V3397 = {"rising", "stable", "softening", "cooling", "declining"}
_ALLOWED_SEGMENTS_V3397 = {
    "server_ddr5", "pc_dram", "hbm", "mobile_dram", "graphics_dram",
    "nand_flash", "general",
}
_ALLOWED_SUPPLIERS_V3397 = {
    "samsung", "sk_hynix", "micron", "kingston", "transcend", "trendforce",
    "multiple", "",
}
_ALLOWED_FORECAST_SEGMENTS_V3397 = {
    "conventional_dram_aggregate", "server_ddr5", "pc_dram",
    "hbm", "mobile_dram", "graphics_dram",
    "samsung_specific", "sk_hynix_specific", "micron_specific",
}


def _call_haiku_json_v3397(client, system_prompt: str, user_msg: str,
                            max_tokens: int = 1024) -> Optional[dict]:
    """Internal: Haiku call returning a parsed JSON dict (None on failure)."""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=max_tokens,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        logger.error(f"Haiku call failed (v3.39.7 extraction): {e}")
        return None
    if not response.content:
        return None
    text = response.content[0].text.strip()
    text = _re_v3397.sub(r"^```(?:json)?\s*", "", text)
    text = _re_v3397.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Haiku v3.39.7 returned invalid JSON: {e}\nResponse: {text[:500]}")
        return None


def _safe_num_v3397(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def extract_dram_signal_from_article(
    client, title: str, article_body: str, url: str,
    published_date: Optional[_date_v3397] = None,
) -> Optional[dict]:
    """Extract a structured DRAM signal from an article via Haiku.

    Returns a dict matching the dram_contract_signals.csv schema, ready to
    pass to upsert_contract_signals() in news.py. None if extraction failed
    or article is too short.
    """
    if not article_body or len(article_body) < 100:
        logger.debug(f"Skipping article with insufficient body: {url}")
        return None

    body = article_body[:6000]
    user_msg = (
        f"Title: {title}\n"
        f"Published: {published_date.isoformat() if published_date else 'unknown'}\n"
        f"URL: {url}\n\n"
        f"Article body:\n{body}\n\n"
        f"Return the JSON object."
    )
    parsed = _call_haiku_json_v3397(client, SIGNAL_EXTRACTION_SYSTEM_PROMPT_V3397, user_msg)
    if parsed is None:
        return None

    tone = parsed.get("tone_direction")
    if isinstance(tone, str):
        tone = tone.lower()
        if tone not in _ALLOWED_TONE_V3397:
            tone = ""
    else:
        tone = ""

    kw = parsed.get("tone_keywords", [])
    keywords_str = ",".join(str(k).strip() for k in kw if k)[:500] if isinstance(kw, list) else ""

    segment = parsed.get("segment_mentioned")
    if isinstance(segment, str):
        segment = segment.lower()
        if segment not in _ALLOWED_SEGMENTS_V3397:
            segment = "general"
    else:
        segment = ""

    supplier = parsed.get("supplier_mentioned", "") or ""
    if isinstance(supplier, str):
        supplier = supplier.lower()
        if supplier not in _ALLOWED_SUPPLIERS_V3397:
            supplier = ""
    else:
        supplier = ""

    qoq_pct = _safe_num_v3397(parsed.get("qoq_pct_mentioned"))
    is_forecast = bool(parsed.get("is_quarterly_forecast", False))
    article_date = published_date or _date_v3397.today()

    url_l = url.lower()
    if "trendforce.com/presscenter" in url_l:
        source_name = "trendforce_press_release"
    elif "trendforce.com" in url_l:
        source_name = "trendforce_news"
    elif "digitimes.com" in url_l:
        source_name = "digitimes"
    elif "etnews.com" in url_l:
        source_name = "electronic_times"
    elif "chosun.com" in url_l:
        source_name = "chosunbiz"
    elif "businesskorea" in url_l:
        source_name = "businesskorea"
    elif "theelec" in url_l:
        source_name = "theelec"
    else:
        source_name = "unknown"

    return {
        "date": article_date.isoformat(),
        "source_name": source_name,
        "source_url": url,
        "headline": title,
        "tone_direction": tone,
        "tone_keywords": keywords_str,
        "segment_mentioned": segment,
        "qoq_pct_mentioned": qoq_pct,
        "supplier_mentioned": supplier,
        "raw_snippet": article_body[:500],
        "is_quarterly_forecast": is_forecast,
    }


def extract_dram_forecasts_from_press_release(
    client, title: str, article_body: str, url: str,
    published_date: Optional[_date_v3397] = None,
) -> list[dict]:
    """Extract structured quarterly forecasts from a TrendForce press release.

    Returns a list of dicts matching dram_contract_quarterly_forecasts.csv
    schema, ready to pass to upsert_contract_forecasts() in news.py. Empty
    list if nothing found.
    """
    if not article_body or len(article_body) < 100:
        return []
    body = article_body[:8000]
    user_msg = (
        f"Title: {title}\n"
        f"Published: {published_date.isoformat() if published_date else 'unknown'}\n"
        f"URL: {url}\n\n"
        f"Article body:\n{body}\n\n"
        f"Return the JSON object."
    )
    parsed = _call_haiku_json_v3397(
        client, FORECAST_EXTRACTION_SYSTEM_PROMPT_V3397, user_msg, max_tokens=1500,
    )
    if parsed is None:
        return []
    raw = parsed.get("forecasts", [])
    if not isinstance(raw, list):
        return []
    pub_date = (published_date or _date_v3397.today()).isoformat()

    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        quarter = entry.get("quarter", "")
        if not isinstance(quarter, str) or not _re_v3397.match(r"^Q[1-4]_\d{4}$", quarter):
            continue
        segment = entry.get("segment", "")
        if segment not in _ALLOWED_FORECAST_SEGMENTS_V3397:
            continue
        low = _safe_num_v3397(entry.get("low_pct"))
        high = _safe_num_v3397(entry.get("high_pct"))
        if low is None or high is None:
            continue
        is_rev = bool(entry.get("is_revision", False))
        notes = ("REVISION: " if is_rev else "") + str(entry.get("exact_quote", ""))[:500]
        out.append({
            "quarter": quarter, "segment": segment,
            "low_pct": low, "high_pct": high,
            "central_pct": (low + high) / 2.0,
            "source": "trendforce_press_release",
            "source_url": url,
            "published_date": pub_date,
            "supersedes_id": None,
            "notes": notes,
        })
    return out
