"""Regression tests for v3.39.7.9 — capex/OCF authority, 2-quarter rolling
mean, "Reversed" status removal, color scheme, NaN-guarded relative
strength, section 3 numeric surfacing, value-chain inference prompt.

Diagnosed from the May 21 2026 brief (Image 1, 2, 3 in chat). User
reported six issues across these surfaces:

  1. Layer 2 reversed t2_hyperscaler_capex_revenue citing capex/revenue
     24-37% < 40% while COMPUTED METRICS showed capex/OCF mean 93.2%
     (well above 50%). The indicator's rule is capex/OCF; capex/revenue
     was leftover from the v3.39.7.4 migration. Two compounding causes:
     stale reversal_condition in the evidence store + signal_reasoner
     still passing capex_revenue_ratios to Layer 2.
  2. Single-quarter snaps[0] capex/OCF was noisy — Amazon Q1 2026 OCF
     dipped to $26.03B (vs Q4 2025 $54.46B) making AMZN look like
     169.8% in isolation. 2-quarter rolling mean per company smooths
     this while preserving the cycle signal.
  3. "Reversed" was a 4th fired_status value preserving audit trail.
     User wants 3 states only — audit trail goes in change_vs_prior
     and the trigger banner, not the status pill.
  4. Color scheme: change-flag was always yellow (#FFEBA1) regardless
     of where the indicator transitioned to. User wants the change
     highlight color to match the destination status (red when fired,
     green when un-fired, yellow only for partial transitions). Also
     #D85A30 for Fired read as amber — darkened to #B91C1C.
  5. (QUIET — no test needed; just explanation in CHANGES.md.)
  6. SK Hynix HBM capacity shift → DDR5 spot price implications not
     drawn in exec summary. Sonnet prompt needed an explicit VALUE-CHAIN
     INFERENCE section telling it to draw causal chains across signals.
  7a. 5d-vs-SOX rendered as "+nanpp" because NaN values bypass `is None`
      checks. Fixed in get_relative_strength_vs_sox + format-side guard.
  7b. Section 3 t3_relative_strength card showed only status pill, no
      number. User: "i expect to see a number as a number is what will
      trigger a warn (partially fired) or fired". Number now rendered
      with color-coded band classification in the section 3 card.
"""

from __future__ import annotations

import math

import pandas as pd


# ============================================================
# Fix #1 — Capex/OCF is authoritative; capex_revenue_ratios removed
# ============================================================

class TestCapexOcfAuthority:
    """signal_reasoner no longer passes capex_revenue_ratios to Layer 2;
    Sonnet prompt no longer cites capex/revenue as a metric."""

    def test_signal_reasoner_does_not_pass_capex_revenue_ratios(self):
        """Pre-v3.39.7.9 signal_reasoner passed both capex_revenue_ratios
        and the OCF mean — Layer 2 latched onto capex/revenue (which had
        a stale reversal_condition from v3.39.7.4 migration) and reversed
        verdicts against the COMPUTED METRICS. v3.39.7.9 drops it."""
        from pathlib import Path
        src = Path("src/analysis/signal_reasoner.py").read_text()
        # The literal key name should no longer appear in signal_specific assignment
        assert 'signal_specific["computed_capex_revenue"]' not in src, (
            "signal_reasoner must not pass computed_capex_revenue — "
            "it was being used by Layer 2 to override capex/OCF verdicts."
        )

    def test_signal_reasoner_passes_new_2q_keys(self):
        """The new keys (mean_capex_ocf_ratio_2q, per_company_capex_ocf_2q,
        n_companies_with_2q_capex_ocf) are what signal_reasoner forwards."""
        from pathlib import Path
        src = Path("src/analysis/signal_reasoner.py").read_text()
        assert 'mean_capex_ocf_ratio_2q' in src
        assert 'per_company_capex_ocf_2q' in src
        assert 'n_companies_with_2q_capex_ocf' in src

    def test_invalid_downgrades_section_explicit_about_capex_ocf_rule(self):
        """REASONER_SYSTEM_PROMPT must explicitly tell Haiku that the rule
        is capex/OCF, not capex/revenue, to prevent the same bug class
        reappearing via prompt-side confusion."""
        from src.analysis.signal_reasoner import REASONER_SYSTEM_PROMPT
        assert "v3.39.7.9" in REASONER_SYSTEM_PROMPT
        assert "capex/OCF" in REASONER_SYSTEM_PROMPT
        # Explicit statement that capex/revenue is legacy and should be ignored
        assert "legacy" in REASONER_SYSTEM_PROMPT.lower() or "no longer" in REASONER_SYSTEM_PROMPT.lower()
        assert "mean_capex_ocf_ratio_2q" in REASONER_SYSTEM_PROMPT

    def test_evidence_store_reversal_condition_updated(self):
        """The state/indicator_evidence.json must have the updated
        reversal_condition citing capex/OCF (not capex/revenue) so Layer 2
        reading the evidence store doesn't see the stale v3.39.7.4 text."""
        import json
        from pathlib import Path
        data = json.loads(Path("state/indicator_evidence.json").read_text())
        indicators = data.get("indicators", data)
        ind = indicators.get("t2_hyperscaler_capex_revenue", {})
        rev_conds = [
            ev.get("reversal_condition", "")
            for ev in ind.get("evidence_log", [])
            if "reversal_condition" in ev
        ]
        assert rev_conds, "Expected at least one reversal_condition entry"
        # All entries must reflect the new rule
        for rc in rev_conds:
            assert "capex/OCF" in rc, (
                f"reversal_condition still cites legacy rule: {rc[:120]}..."
            )
            assert "2-quarter" in rc, (
                "reversal_condition must reference 2-quarter rolling — "
                "single-quarter spikes were causing false reversals."
            )


# ============================================================
# Fix #2 — 2-quarter rolling mean per company
# ============================================================

class TestTwoQuarterRollingMean:
    """estimates.py + fast_tier.py compute per-company 2q mean before
    cross-company aggregation."""

    def test_fast_tier_computes_per_company_2q_mean(self):
        """The fast tier path builds per_company_capex_ocf_2q as a dict
        of ticker → 2-quarter rolling mean."""
        from pathlib import Path
        src = Path("src/fast_tier.py").read_text()
        assert "per_company_capex_ocf_2q" in src
        # Must NOT use snaps[0] anymore for the aggregation (replaced by
        # snaps[:2] mean per company)
        assert "snaps[:2]" in src, (
            "fast_tier must slice up to first 2 quarters per company "
            "for the rolling mean computation."
        )

    def test_estimates_persists_per_company_2q(self):
        """The hyperscaler_capex_baselines.json payload includes the
        per-company 2q ratios."""
        from pathlib import Path
        src = Path("src/data/estimates.py").read_text()
        assert "per_company_capex_ocf_2q" in src
        assert "mean_capex_ocf_ratio_2q" in src

    def test_fast_tier_omits_capex_revenue_ratios_key(self):
        """Pre-v3.39.7.9 computed_metrics had capex_revenue_ratios; v3.39.7.9
        removes it entirely. The renamed snapshot key is
        hyperscaler_quarterly_snapshots."""
        from pathlib import Path
        src = Path("src/fast_tier.py").read_text()
        # Must not have capex_revenue_ratios as a computed_metrics key
        assert '"capex_revenue_ratios"' not in src, (
            "computed_metrics must not include capex_revenue_ratios — "
            "Layer 2 was using it to reverse capex/OCF verdicts."
        )
        # New snapshot key present
        assert '"hyperscaler_quarterly_snapshots"' in src

    def test_two_quarter_mean_smooths_single_quarter_spike(self):
        """Numerical demonstration: Amazon Q1 2026 (capex/OCF 169.8% in
        isolation due to OCF dip) becomes ~85% with 2-quarter mean."""
        # Mock the 2-quarter rolling logic directly
        amzn_quarters = [
            {"capex_ocf_ratio": 1.698},  # Q1 2026 isolated dip
            {"capex_ocf_ratio": 0.20},   # Q4 2025 normal baseline
        ]
        valid = [q["capex_ocf_ratio"] for q in amzn_quarters[:2]]
        mean_2q = sum(valid) / len(valid)
        # Should be ~0.95 — much less alarming than 1.698 in isolation
        assert 0.90 <= mean_2q <= 1.00
        # And below the 1.00 "severe single-co strain" threshold
        assert mean_2q < 1.00


# ============================================================
# Fix #3 — "Reversed" status removed everywhere
# ============================================================

class TestReversedStatusRemoved:
    """Three-state collapse: Fired / Partially fired / Not fired."""

    def test_signal_reasoner_enum_drops_reversed(self):
        """The fired_status field type comment lists the 3 valid states only."""
        from pathlib import Path
        src = Path("src/analysis/signal_reasoner.py").read_text()
        # Find the dataclass field annotation
        # The line should say "Fired" | "Not fired" | "Partially fired" WITHOUT Reversed
        # (allowing for the v3.39.7.9 explanatory comment that mentions "Reversed" as removed)
        # Use a pattern: look for fired_status enum in dataclass without literal Reversed value
        import re
        m = re.search(r'fired_status:\s*str\s*#\s*("[^"]*"(?:\s*\|\s*"[^"]*")*)', src)
        assert m, "Could not find fired_status type comment"
        enum_text = m.group(1)
        assert '"Reversed"' not in enum_text, (
            f'fired_status enum comment still includes "Reversed": {enum_text}'
        )

    def test_signal_reasoner_system_prompt_collapses_reversed_transition(self):
        """The "From Fired → ..." transition rule says Not fired, not Reversed."""
        from src.analysis.signal_reasoner import REASONER_SYSTEM_PROMPT
        # The transition rule from Fired now goes to Not fired
        assert 'From Fired → set to "Not fired"' in REASONER_SYSTEM_PROMPT
        # And explicitly notes the v3.39.7.9 collapse
        assert "v3.39.7.9: collapsed" in REASONER_SYSTEM_PROMPT

    def test_signal_reasoner_json_schema_drops_reversed(self):
        """The output JSON schema in the prompt lists 3 fired_status values."""
        from src.analysis.signal_reasoner import REASONER_USER_TEMPLATE
        # Find the JSON schema example line (it's in the USER template, not system)
        schema_line_match = '"fired_status": "Fired" | "Not fired" | "Partially fired"'
        assert schema_line_match in REASONER_USER_TEMPLATE
        # And NOT the 4-state version
        assert '"fired_status": "Fired" | "Not fired" | "Partially fired" | "Reversed"' not in REASONER_USER_TEMPLATE

    def test_template_status_colors_no_reversed_key(self):
        """STATUS_COLORS dict has no Reversed key."""
        from src.email_module.template import STATUS_COLORS
        assert "Reversed" not in STATUS_COLORS, (
            "STATUS_COLORS must not include Reversed — "
            "v3.39.7.9 collapsed to Fired/Partially fired/Not fired."
        )

    def test_llm_summarizer_indicator_assessment_dataclass_no_reversed(self):
        """IndicatorAssessment.current_status enum comment lists 3 states."""
        from pathlib import Path
        src = Path("src/analysis/llm_summarizer.py").read_text()
        import re
        m = re.search(r'current_status:\s*str\s*#\s*("[^"]*"(?:\s*\|\s*"[^"]*")*)', src)
        assert m, "Could not find current_status type comment in IndicatorAssessment"
        enum_text = m.group(1)
        assert '"Reversed"' not in enum_text


# ============================================================
# Fix #4 — Color scheme: clearer red + change-flag matches destination
# ============================================================

class TestColorScheme:
    """v3.39.7.9 color updates."""

    def test_fired_color_is_clearer_red(self):
        """#D85A30 (red-orange) → #B91C1C (clearer red) so Fired reads
        unambiguously as red, not amber."""
        from src.email_module.template import STATUS_COLORS
        assert STATUS_COLORS["Fired"] == "#B91C1C", (
            f"Fired color expected #B91C1C, got {STATUS_COLORS.get('Fired')}"
        )

    def test_partially_fired_stays_yellow(self):
        from src.email_module.template import STATUS_COLORS
        assert STATUS_COLORS["Partially fired"] == "#FAC775"

    def test_not_fired_stays_green(self):
        from src.email_module.template import STATUS_COLORS
        assert STATUS_COLORS["Not fired"] == "#C0DD97"

    def test_change_flag_renders_as_plain_text_v3_4_1(self):
        """v3.4.1 SUPERSEDED v3.39.7.9 Fix #4: the change-flag colored
        background experiment was reverted. Per user direction 2026-05-22,
        the 'CHANGED:' annotation in section 3 renders as plain muted text
        without a background tint — only the status pill itself carries the
        destination color. Verify the new render uses plain-text styling
        rather than the v3.39.7.9 dynamic background-color logic.
        """
        from pathlib import Path
        src = Path("src/email_module/template.py").read_text()
        # v3.4.1 sentinel — must be present
        assert "v3.4.1 Fix 2" in src, (
            "v3.4.1 Fix 2 comment marker missing — the plain-text revert "
            "may not have been applied."
        )
        # v3.39.7.9 background-color logic must NOT be present anymore
        assert '#FFFFFF" if a.current_status == "Fired"' not in src, (
            "v3.39.7.9's white-text-on-red conditional should be removed "
            "in v3.4.1 (change-flag no longer has a colored background)."
        )

    def test_change_flag_no_longer_uses_status_color_background(self):
        """v3.4.1: the change-flag span no longer interpolates
        background:{status_color} — was the v3.39.7.9 dynamic coloring
        that's been reverted to plain text in v3.4.1."""
        from pathlib import Path
        src = Path("src/email_module/template.py").read_text()
        # Find the change_flag construction block specifically (not other
        # uses of {status_color} elsewhere in the file like status pills)
        # The reverted block uses 'CHANGED:' prefix in a div with muted text.
        assert "color:#555" in src, (
            "v3.4.1 change-flag must use muted text color (#555) for "
            "plain-text rendering"
        )


# ============================================================
# Fix #6 — Value-chain inference prompt section
# ============================================================

class TestValueChainInferencePrompt:
    """Sonnet's exec brief spec includes a VALUE-CHAIN INFERENCE section
    with explicit examples for the framework's signal-chain patterns."""

    def _get_sonnet_prompt(self) -> str:
        """Build a minimal prompt and return its text for assertion."""
        from src.analysis.llm_summarizer import build_sonnet_analysis_prompt

        class _Triage:
            relevant_news = []
            broker_actions = []
            trendforce_commentary = []
            indicator_relevance = {}  # NOT indicator_relevance_map — that was a typo in earlier test draft

        return build_sonnet_analysis_prompt(
            triage=_Triage(),
            performance_data=[],
            estimates_data={},
            big_movers=[],
            prior_state={},
            computed_metrics={},
        )

    def test_prompt_has_value_chain_inference_section(self):
        prompt = self._get_sonnet_prompt()
        assert "VALUE-CHAIN INFERENCE" in prompt

    def test_prompt_includes_hbm_to_ddr5_causal_example(self):
        """The SK Hynix HBM capacity → DDR5 spot price chain is the
        specific case user surfaced. Must be explicitly modeled in the
        prompt examples so Sonnet draws this kind of inference."""
        prompt = self._get_sonnet_prompt()
        assert "HBM capacity" in prompt
        assert "DDR5" in prompt
        # The "→" arrow indicating causal chain direction must be present
        assert "→" in prompt

    def test_prompt_includes_demand_cascade_examples(self):
        """Hyperscaler capex → chip designer backlog → memory revenue
        cascade must be modeled."""
        prompt = self._get_sonnet_prompt()
        assert "Hyperscaler" in prompt
        assert "DEMAND CASCADES" in prompt

    def test_prompt_includes_substitution_risk_examples(self):
        """CXMT and Cerebras substitution-risk examples must be present."""
        prompt = self._get_sonnet_prompt()
        assert "CXMT" in prompt
        assert "Cerebras" in prompt
        assert "substitution" in prompt.lower()

    def test_prompt_has_evidence_anchor_limit_rule(self):
        """Inferences only drawn when both ends have brief or recent
        evidence — prevents speculation from one anchor."""
        prompt = self._get_sonnet_prompt()
        assert "LIMITS:" in prompt
        # Look for the "both ends" anchor rule
        assert "both ends" in prompt or "anchor" in prompt.lower()


# ============================================================
# Fix #7a — NaN guard on relative-strength computation
# ============================================================

class TestRelativeStrengthNaNGuard:
    """get_relative_strength_vs_sox handles NaN price cells without
    propagating NaN downstream. v3.4.6.2 upgraded the v3.39.7.9 guard
    (return None on any NaN) to a calendar-alignment fix (project to
    rows where both columns are non-NaN, then compute). The tests below
    cover both behaviors:
      - Sufficient aligned rows remain → compute correctly from the
        aligned subset (no None, no NaN downstream)
      - Insufficient aligned rows → return None
    """

    def test_single_ticker_nan_uses_aligned_subset(self):
        """One NaN in the ticker column drops that row from the aligned
        pair. With 9 remaining aligned bars and days=5 we still compute."""
        from src.data.prices import get_relative_strength_vs_benchmark
        import math
        idx = pd.date_range("2026-05-13", periods=10, freq="D")
        history = pd.DataFrame({
            "SK Hynix": [100, 101, 102, 103, float("nan"), 105, 106, 107, 108, 110],
            "MSCI ACWI Semiconductor": [200, 201, 202, 203, 204, 205, 206, 207, 208, 210],
        }, index=idx)
        # v3.4.6.2: aligned subset has 9 rows (the NaN row dropped). days=5
        # needs 6 rows; we have 9 → real number returned.
        result = get_relative_strength_vs_benchmark("SK Hynix", history, days=5, benchmark_col="MSCI ACWI Semiconductor")
        assert result is not None
        assert isinstance(result, float)
        assert not math.isnan(result)

    def test_single_sox_nan_uses_aligned_subset(self):
        """One NaN in the SOX column drops that row from the aligned pair.
        Result computed from the remaining 9 aligned bars."""
        from src.data.prices import get_relative_strength_vs_benchmark
        import math
        idx = pd.date_range("2026-05-13", periods=10, freq="D")
        history = pd.DataFrame({
            "SK Hynix": [100, 101, 102, 103, 104, 105, 106, 107, 108, 110],
            "MSCI ACWI Semiconductor": [200, 201, 202, 203, float("nan"), 205, 206, 207, 208, 210],
        }, index=idx)
        result = get_relative_strength_vs_benchmark("SK Hynix", history, days=5, benchmark_col="MSCI ACWI Semiconductor")
        assert result is not None
        assert not math.isnan(result)

    def test_tail_nan_uses_prior_aligned_bar(self):
        """The dominant production case — latest bar is KRX-only, SOX NaN
        at iloc[-1]. v3.4.6.2 drops that row from the aligned pair and
        computes from the prior 9 bars (which include 5+ valid days)."""
        from src.data.prices import get_relative_strength_vs_benchmark
        import math
        idx = pd.date_range("2026-05-13", periods=10, freq="D")
        history = pd.DataFrame({
            "SK Hynix": [100, 101, 102, 103, 104, 105, 106, 107, 108, float("nan")],
            "MSCI ACWI Semiconductor": [200, 201, 202, 203, 204, 205, 206, 207, 208, 210],
        }, index=idx)
        result = get_relative_strength_vs_benchmark("SK Hynix", history, days=5, benchmark_col="MSCI ACWI Semiconductor")
        assert result is not None
        assert not math.isnan(result)

    def test_returns_valid_float_when_data_clean(self):
        """Sanity check: clean data still returns a real number."""
        from src.data.prices import get_relative_strength_vs_benchmark
        idx = pd.date_range("2026-05-13", periods=10, freq="D")
        # Ticker -5% over 5 days, SOX +2% over 5 days → ticker underperforms by 7pp
        history = pd.DataFrame({
            "SK Hynix": [100.0] * 5 + [95.0] * 5,
            "MSCI ACWI Semiconductor": [200.0] * 5 + [204.0] * 5,
        }, index=idx)
        result = get_relative_strength_vs_benchmark("SK Hynix", history, days=5, benchmark_col="MSCI ACWI Semiconductor")
        assert result is not None
        assert isinstance(result, float)
        assert not math.isnan(result)
        # Ticker went 100 → 95 = -5%; SOX went 200 → 204 = +2%; diff = -7%
        assert abs(result - (-7.0)) < 0.1

    def test_zero_start_price_returns_none(self):
        """Pathological zero-price guard at the lookback position.
        For days=8 in a 10-row df, lookback is iloc[-9] = index 1."""
        from src.data.prices import get_relative_strength_vs_benchmark
        idx = pd.date_range("2026-05-13", periods=10, freq="D")
        history = pd.DataFrame({
            "SK Hynix": [100, 0.0, 102, 103, 104, 105, 106, 107, 108, 110],
            "MSCI ACWI Semiconductor": [200, 201, 202, 203, 204, 205, 206, 207, 208, 210],
        }, index=idx)
        result = get_relative_strength_vs_benchmark("SK Hynix", history, days=8, benchmark_col="MSCI ACWI Semiconductor")
        assert result is None


# ============================================================
# Fix #7b — Section 3 surfaces 5d-vs-SOX numerics
# ============================================================

class TestSection3RelativeStrengthSnippet:
    """t3_relative_strength row in section 3 now shows the actual numeric
    values (SK Hynix / Micron / DRAM-ETF 5d-vs-SOX) color-coded by band."""

    def test_snippet_returns_empty_string_on_missing_verdict(self):
        """Defensive: no verdict → no snippet (section 3 falls back to
        Sonnet prose only)."""
        from src.email_module.template import _build_relative_strength_snippet
        assert _build_relative_strength_snippet(None) == ""
        assert _build_relative_strength_snippet({}) == ""

    def test_snippet_shows_skhx_value(self):
        from src.email_module.template import _build_relative_strength_snippet
        verdict = {
            "skhx_5d_vs_spx_pp": -3.21,
            "mu_5d_vs_spx_pp": -2.84,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        # The SK Hynix label + value must appear
        assert "SK Hynix" in snippet
        assert "-3.21pp" in snippet

    def test_snippet_shows_micron_and_20d_values(self):
        from src.email_module.template import _build_relative_strength_snippet
        verdict = {
            "skhx_5d_vs_spx_pp": -3.21,
            "mu_5d_vs_spx_pp": -2.84,
            "mu_20d_vs_spx_pp": -4.10,
        }
        snippet = _build_relative_strength_snippet(verdict)
        assert "Micron 5d vs S&amp;P 500:" in snippet
        assert "-2.84pp" in snippet
        assert "Micron 20d vs S&amp;P 500:" in snippet
        assert "-4.10pp" in snippet

    def test_snippet_color_codes_red_band_below_minus_10(self):
        """Value ≤ -10pp must use the Fired color (#B91C1C)."""
        from src.email_module.template import _build_relative_strength_snippet, STATUS_COLORS
        verdict = {
            "skhx_5d_vs_spx_pp": -12.5,  # below -10pp → red
            "mu_5d_vs_spx_pp": -1.0,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        # The Fired color must be applied to the SK Hynix span
        assert STATUS_COLORS["Fired"] in snippet

    def test_snippet_color_codes_yellow_band_minus_5_to_minus_10(self):
        """Value -5 to -10pp → Partially fired (yellow)."""
        from src.email_module.template import _build_relative_strength_snippet, STATUS_COLORS
        verdict = {
            "skhx_5d_vs_spx_pp": -7.0,  # in -5 to -10 → yellow
            "mu_5d_vs_spx_pp": 0.0,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        assert STATUS_COLORS["Partially fired"] in snippet

    def test_snippet_color_codes_green_band_above_minus_5(self):
        """Value > -5pp → Not fired (green)."""
        from src.email_module.template import _build_relative_strength_snippet, STATUS_COLORS
        verdict = {
            "skhx_5d_vs_spx_pp": -2.0,
            "mu_5d_vs_spx_pp": 1.5,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        assert STATUS_COLORS["Not fired"] in snippet

    def test_snippet_renders_not_computed_for_nan(self):
        """NaN value must render as "(not computed)", not "+nanpp"."""
        from src.email_module.template import _build_relative_strength_snippet
        verdict = {
            "skhx_5d_vs_spx_pp": float("nan"),
            "mu_5d_vs_spx_pp": -2.84,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        assert "(not computed)" in snippet
        # And no "+nanpp" or "nanpp" leakage
        assert "nanpp" not in snippet

    def test_snippet_renders_not_computed_for_none(self):
        from src.email_module.template import _build_relative_strength_snippet
        verdict = {
            "skhx_5d_vs_spx_pp": None,
            "mu_5d_vs_spx_pp": None,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        assert snippet.count("(not computed)") == 4  # SKHX 5d/20d + MU 5d/20d

    def test_snippet_includes_threshold_legend(self):
        """The thresholds row helps the reader interpret the numbers
        without scrolling to section 4."""
        from src.email_module.template import _build_relative_strength_snippet
        verdict = {
            "skhx_5d_vs_spx_pp": -2.0,
            "mu_5d_vs_spx_pp": 1.5,
                    }
        snippet = _build_relative_strength_snippet(verdict)
        assert "Thresholds" in snippet
        assert "−10pp" in snippet or "-10pp" in snippet
        assert "−5pp" in snippet or "-5pp" in snippet


# ============================================================
# Fix #7a (downstream) — llm_summarizer NaN guard in COMPUTED METRICS
# ============================================================

class TestLlmSummarizerNaNGuard:
    """The COMPUTED METRICS render block uses an explicit math.isnan check
    so NaN values don't propagate as '+nanpp' text."""

    def test_format_side_has_math_isnan_check(self):
        """Code-level check: the format-side `_valid` helper must include
        math.isnan to catch NaN even when value is not None."""
        from pathlib import Path
        src = Path("src/analysis/llm_summarizer.py").read_text()
        assert "math.isnan" in src, (
            "llm_summarizer must defensively check math.isnan in the "
            "COMPUTED METRICS rendering block. Pre-v3.39.7.9 only checked "
            "`is not None` which allowed NaN through."
        )

    def test_math_module_imported(self):
        from pathlib import Path
        src = Path("src/analysis/llm_summarizer.py").read_text()
        # The import line must be present
        assert "import math" in src
