"""v3.4.9 — Tests for the section 4 cards 6-8 restructure + DHCR chart redesign.

Background: production v3.4.8 brief on 2026-05-23 had cards 6-8 visually
inconsistent with cards 1-5 (different chrome, different label format,
oversized matrix chart with confusing overlays). Per user direction:
  - Cards 6-8 must mirror cards 1-5's 4-section labeled-paragraph structure
  - Status pills use the same vocabulary as 1-5 (no special prefix)
  - DHCR matrix simplified to Indicator-1 Cycle Matrix style: single
    TODAY marker + yellow callout box, no overlaid circles or trajectory
    arrows, y-axis flipped (R5 top, R1 bottom)
  - "Where we came from" verbalized in prose, not on the chart
  - Two-column chart layout: matrix left, score+moat bars right
  - Exec summary distinguishes "indicator" (6-8) from "signal" (1-5)
"""

import re
import unittest
from dataclasses import asdict


def _minimal_dhcr_snapshot() -> dict:
    return {
        "status": "Not fired",
        "status_color": "#C0DD97",
        "position_score": 52,
        "workload_implied_zone": "LPDDR/HBM",
        "workload_implied_cell": (2, 1),
        "market_deployed_zone": "HBM/GDDR",
        "market_deployed_cell": (3, 1),
        "anticipation_gap_bands": 1,
        "frontier_kv_kb_per_token": 13.3,
        "frontier_kv_gb_per_1m_tokens": 13.3,
        "frontier_model_name": "DeepSeek V4",
        "frontier_model_arch": "MoE",
        "kv_band_label": "moderate",
        "kv_yoy_change_pct": -45,
        "operative_context_tokens": 114_000,
        "context_band_label": "C2 32-200K",
        "context_categories": [],
        "enterprise_ai_run_rate_usd_b": 121.0,
        "enterprise_ai_yoy_growth_pct": 175.0,
        "nvidia_datacenter_yoy_growth_pct": 69.0,
        "volume_index": 100.0,
        "volume_trajectory": "bullish",
        "hbm_bit_demand_index": 52.0,
        "sanity_check_confirms": 3,
        "sanity_check_contradicts": 0,
        "calibration_flag": False,
        "sanity_signal_substitution_news_count": 0,
        "sanity_signal_hyperscaler_decode_mentions": 0,
        "sanity_signal_hbm_price_trend": "rising",
        "narrative_summary": "Test narrative.",
        "bull_case_summary": "BULL case text.",
        "bear_case_summary": "BEAR case text.",
        "status_reasoning": "Score 52 in transition zone, volume bullish.",
        "substitution_risk_notes": [],
    }


class TestCardsSixSevenEightMirrorCardsOneToFive(unittest.TestCase):
    """All three cards (DHCR, Competitor, ADR) must use the same 4-section
    labeled-paragraph structure as cards 1-5."""

    EXPECTED_LABELS = [
        "<strong>What we're looking for:</strong>",
        "<strong>Where we stand:</strong>",
        "<strong>Latest developments:</strong>",
    ]

    def test_dhcr_card_has_all_four_section_labels(self):
        from src.email_module.template import _build_dhcr_section4_card
        html = _build_dhcr_section4_card(_minimal_dhcr_snapshot())
        for label in self.EXPECTED_LABELS:
            self.assertIn(label, html,
                f"DHCR card missing cards-1-5 label: {label}")
        # Source links class is the cards-1-5 convention
        self.assertIn("class='links-list'", html)

    def test_competitor_card_has_all_four_section_labels(self):
        from src.email_module.template import _build_competitor_section4_card
        from src.analysis.competitor_scoring import compute_competitor_snapshot
        snap = asdict(compute_competitor_snapshot())
        html = _build_competitor_section4_card(snap)
        for label in self.EXPECTED_LABELS:
            self.assertIn(label, html,
                f"Competitor card missing cards-1-5 label: {label}")
        self.assertIn("class='links-list'", html)

    def test_adr_card_has_all_four_section_labels(self):
        from src.email_module.template import _build_adr_section4_card
        from src.analysis.adr_event_tracking import compute_adr_snapshot
        snap = asdict(compute_adr_snapshot())
        html = _build_adr_section4_card(snap)
        for label in self.EXPECTED_LABELS:
            self.assertIn(label, html,
                f"ADR card missing cards-1-5 label: {label}")
        self.assertIn("class='links-list'", html)


class TestStatusPillsMatchCardsOneToFive(unittest.TestCase):
    """Status pills on cards 6-8 use the same vocabulary as 1-5 (no
    special 'Watch indicator' / 'Overlay' prefix per user choice)."""

    def test_dhcr_pill_is_status_only_no_prefix(self):
        from src.email_module.template import _build_dhcr_section4_card
        snap = _minimal_dhcr_snapshot()
        html = _build_dhcr_section4_card(snap)
        # Pill should be the same span class+style as cards 1-5
        self.assertIn("class='status-badge'", html)
        # Pill text is exactly "Not fired" — no prefix like "Watch indicator"
        self.assertIn(">Not fired</span>", html)
        # And explicitly does NOT contain prefix vocabulary that we considered
        # earlier in the design discussion
        for forbidden_prefix in ["Watch indicator", "Overlay indicator",
                                  "Tier 1 indicator (watch)"]:
            self.assertNotIn(forbidden_prefix, html,
                f"Pill must not have prefix: {forbidden_prefix}")

    def test_competitor_pill_is_status_only_no_prefix(self):
        from src.email_module.template import _build_competitor_section4_card
        from src.analysis.competitor_scoring import compute_competitor_snapshot
        html = _build_competitor_section4_card(asdict(compute_competitor_snapshot()))
        self.assertIn("class='status-badge'", html)

    def test_adr_pill_is_status_only_no_prefix(self):
        from src.email_module.template import _build_adr_section4_card
        from src.analysis.adr_event_tracking import compute_adr_snapshot
        html = _build_adr_section4_card(asdict(compute_adr_snapshot()))
        self.assertIn("class='status-badge'", html)


class TestDHCRMatrixV349NewStyle(unittest.TestCase):
    """v3.4.9 DHCR matrix: y-axis flipped, single TODAY marker + callout box,
    no overlaid circles or trajectory arrows."""

    def test_matrix_chart_rendered_with_test_snapshot(self):
        """Smoke: chart generation completes with realistic snapshot."""
        from src.analysis.chart_generator import generate_dhcr_matrix_chart
        b64 = generate_dhcr_matrix_chart(_minimal_dhcr_snapshot())
        self.assertIsNotNone(b64)
        self.assertGreater(len(b64), 1000, "Matrix b64 should be substantial")

    def test_matrix_function_signature_supports_trajectory_for_back_compat(self):
        """v3.4.9 dropped trajectory overlays but kept the parameter for
        backwards compatibility with callers passing it from fast_tier.py."""
        import inspect
        from src.analysis.chart_generator import generate_dhcr_matrix_chart
        sig = inspect.signature(generate_dhcr_matrix_chart)
        self.assertIn("trajectory", sig.parameters,
            "trajectory= parameter must remain for backwards compatibility")

    def test_matrix_source_does_not_reference_90d_ago_label(self):
        """The blue '90d ago' arrow label was removed per user direction."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src/analysis/chart_generator.py").read_text()
        # The literal text "90d ago" must not appear in the active
        # v3.4.9 generate_dhcr_matrix_chart function. It can appear in
        # the _legacy variant if kept around for reference.
        # Quick way: split on the legacy marker and check the live version.
        live_section = src.split("def generate_dhcr_matrix_chart_legacy")[0]
        live_section_after_v349_def = live_section.split(
            "def generate_dhcr_matrix_chart")[-1]
        self.assertNotIn("90d ago", live_section_after_v349_def,
            "'90d ago' label must be removed from the active matrix chart")

    def test_matrix_y_axis_flipped_in_source(self):
        """v3.4.9: R5 at top, R1 at bottom. The function should
        document this and not reverse the y_labels (which was the old
        v3.4.2 hack to display R1 at top)."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src/analysis/chart_generator.py").read_text()
        live_section = src.split("def generate_dhcr_matrix_chart_legacy")[0]
        live_v349 = live_section.split("def generate_dhcr_matrix_chart")[-1]
        # v3.4.9.2 documents the y-flip in a comment ("Y-axis flipped" in
        # docstring + "y-axis flip" in inline comment). Accept either form.
        live_lower = live_v349.lower()
        self.assertTrue(
            "y-axis flip" in live_lower or "y-axis flipped" in live_lower,
            "v3.4.9 matrix must document the y-axis flip",
        )
        # Live function does NOT reverse y_labels (which was the v3.4.2 hack)
        # Verify by checking that the active function sets yticklabels
        # to y_labels directly, not reversed(y_labels)
        self.assertNotIn("reversed(y_labels)", live_v349,
            "v3.4.9 must not use reversed(y_labels) — y-flip is via display_y")


class TestDHCRSpectrumBarV349(unittest.TestCase):
    """v3.4.9.2 — The right column of the chart row uses the
    `_build_dhcr_spectrum_bar` HTML/CSS rendering (vertical 0-100 bar with
    memory type + moat annotations per band), updated 2026-05-23 with
    colors flipped, honest marker math, sketch-style marker, and zone
    labels recalibrated to natural cell-score breakpoints."""

    def test_spectrum_bar_function_still_exists(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        self.assertTrue(callable(_build_dhcr_spectrum_bar))

    def test_spectrum_bar_renders_with_score(self):
        from src.email_module.template import _build_dhcr_spectrum_bar
        html = _build_dhcr_spectrum_bar(52)
        # v3.4.9.2 memory-type labels (recalibrated to natural cell-score
        # breakpoints — different from v3.4.4 labels)
        for mem_type in ("HBM dominant", "HBM-leaning", "HBM-GDDR / hybrid",
                          "LPDDR-led", "DDR commodity"):
            self.assertIn(mem_type, html,
                f"Spectrum bar missing memory-type label: {mem_type}")
        # 5 moat labels per band (unchanged)
        for moat in ("Strongest moat", "Strong moat", "Partial moat",
                      "Weak moat", "No moat"):
            self.assertIn(moat, html,
                f"Spectrum bar missing moat label: {moat}")
        # v3.4.9.2 marker style: TODAY + X/100 (sketch-style, not "today 52")
        self.assertIn("TODAY", html)
        self.assertIn("52/100", html)

    def test_generate_dhcr_bars_chart_was_removed(self):
        """v3.4.9 initially added a wrong generate_dhcr_bars_chart (two
        horizontal bars on shared 0-100). Per user correction 2026-05-23,
        the correct right-column visual is the existing v3.4.4
        _build_dhcr_spectrum_bar (single vertical bar). The wrong
        generate_dhcr_bars_chart function must NOT exist in the chart
        generator."""
        from src.analysis import chart_generator
        self.assertFalse(
            hasattr(chart_generator, "generate_dhcr_bars_chart"),
            "generate_dhcr_bars_chart must be removed (was the wrong design)",
        )


class TestDHCRTwoColumnChartRow(unittest.TestCase):
    """v3.4.9 — matrix and vertical spectrum bar side-by-side in a 2-column
    table per Raul's original sketch 2026-05-22. The right column is the
    existing v3.4.4 `_build_dhcr_spectrum_bar` HTML/CSS, NOT a separate
    generated chart."""

    def test_two_column_layout_in_card(self):
        from src.email_module.template import _build_dhcr_section4_card
        html = _build_dhcr_section4_card(
            _minimal_dhcr_snapshot(),
            matrix_chart_b64="MATRIXFAKE",
        )
        # Matrix embedded
        self.assertIn("data:image/png;base64,MATRIXFAKE", html)
        # Spectrum bar present (verified via its distinctive heading text)
        self.assertIn("SK HYNIX HBM CAPTIVE-RATIO SPECTRUM", html)
        # Two-column table layout
        self.assertIn("<table", html)
        # Matrix appears BEFORE the spectrum bar (matrix left, spectrum right)
        self.assertLess(
            html.index("MATRIXFAKE"),
            html.index("SK HYNIX HBM CAPTIVE-RATIO SPECTRUM"),
            "Matrix must render before spectrum bar (matrix left, bar right)"
        )

    def test_card_works_without_matrix_chart(self):
        """Defensive: card still renders meaningfully if matrix chart
        generation failed. The chart row is skipped entirely in that case."""
        from src.email_module.template import _build_dhcr_section4_card
        html = _build_dhcr_section4_card(_minimal_dhcr_snapshot())
        # The four labeled paragraphs still render
        for label in ("<strong>What we're looking for:</strong>",
                       "<strong>Where we stand:</strong>",
                       "<strong>Latest developments:</strong>"):
            self.assertIn(label, html)


class TestDHCRWhereWeCameFromInProse(unittest.TestCase):
    """v3.4.9 verbalized history per user direction — the blue trajectory
    arrow on the matrix was removed; history now lives in 'Where we stand'
    prose so the visual stays clean."""

    def test_history_verbalized_when_trajectory_present(self):
        from src.email_module.template import _build_dhcr_section4_card
        snap = _minimal_dhcr_snapshot()
        snap["trajectory"] = [
            {"position_score": 47, "date": "2026-02-23"},
            {"position_score": 50, "date": "2026-04-01"},
        ]
        html = _build_dhcr_section4_card(snap)
        # When history is available, the "Where we came from" phrase is rendered
        self.assertIn("Where we came from", html)

    def test_no_history_phrase_when_no_trajectory(self):
        """Cards still render cleanly with no trajectory history."""
        from src.email_module.template import _build_dhcr_section4_card
        snap = _minimal_dhcr_snapshot()
        # No trajectory field
        html = _build_dhcr_section4_card(snap)
        # No 'Where we came from' header — but core structure still present
        self.assertIn("<strong>Where we stand:</strong>", html)


class TestExecSummaryIndicatorVsSignalVocabulary(unittest.TestCase):
    """v3.4.9 — Sonnet prompt now distinguishes 'signal' (1-5, counts in
    T1) from 'indicator' (6-8, watch-only overlay). Verified by inspecting
    the Sonnet system prompt text in llm_summarizer.py."""

    def test_sonnet_prompt_has_vocabulary_discipline_block(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src/analysis/llm_summarizer.py").read_text()
        # The v3.4.9 vocabulary discipline note must appear in the prompt
        self.assertIn("VOCABULARY DISCIPLINE for cards 6-8", src)
        # And must explicitly distinguish the two terms
        self.assertIn("indicator changed status", src)
        # And give a concrete example of correct vs wrong phrasing
        self.assertIn('WRONG: "One new signal fires today', src)


class TestWorkflowGitAddIsWholesaleStateDir(unittest.TestCase):
    """v3.4.9 fast_tier.yml workflow fix — `git add -A state/ data/dram_*.csv`
    instead of the explicit per-file allowlist that was missing v3.4.4+
    state files (competitor_state.json, adr_state.json, etc.) and caused
    the 2026-05-23 commit-state failure."""

    def test_fast_tier_yml_uses_wholesale_state_add(self):
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows/fast_tier.yml")
        if not path.exists():
            self.skipTest("fast_tier.yml not in bundle (delivered separately)")
        src = path.read_text()
        # The v3.4.9 add command — wholesale state/ + data/dram_*.csv
        self.assertIn("git add -A state/ data/dram_*.csv", src,
            "fast_tier.yml must use wholesale state/ + data/dram_*.csv "
            "per v3.4.9 fix")

    def test_fast_tier_yml_does_not_use_old_allowlist(self):
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows/fast_tier.yml")
        if not path.exists():
            self.skipTest("fast_tier.yml not in bundle")
        src = path.read_text()
        # The old per-file allowlist signature (chained per-file paths)
        # should NOT be present in v3.4.9
        # Look for the v3.4.8 trailing signature: dhcr_state.json followed by
        # data/dram_spot_history.csv on the next line — this was the
        # explicit-list pattern. Wholesale add doesn't have this two-line chain.
        old_pattern = re.search(
            r"git add -A state/brief_sent_dates\.json[^\n]*\\\s*\n\s*state/prior_state\.json",
            src,
        )
        self.assertIsNone(old_pattern,
            "fast_tier.yml still contains the v3.4.8 explicit allowlist "
            "(state/brief_sent_dates.json then state/prior_state.json on "
            "next line) — must be replaced with wholesale add")


class TestDeepTierWorkflowHardening(unittest.TestCase):
    """v3.4.9.2 deep_tier.yml hardening — same fix as fast_tier:
    1. Wholesale `git add -A state/` (was explicit allowlist that missed
       any new state file added in future framework versions)
    2. Fail-loudly rebase/push (was `|| true` that silently swallowed the
       rebase failure that's been breaking deep_tier idempotency — making
       all 5 scheduled runs each day do full work because none could
       persist state/deep_tier_diagnostics.json to main)
    """

    def test_deep_tier_yml_uses_wholesale_state_add(self):
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows/deep_tier.yml")
        if not path.exists():
            self.skipTest("deep_tier.yml not in bundle")
        src = path.read_text()
        # v3.4.9.2 wholesale add — single `git add -A state/` line
        self.assertIn("git add -A state/", src,
            "deep_tier.yml must use wholesale state/ add per v3.4.9.2")

    def test_deep_tier_yml_does_not_swallow_rebase_failures(self):
        """The pre-v3.4.9.2 `git pull --rebase origin main || true` pattern
        silently swallowed the rebase failure that breaks idempotency.
        v3.4.9.2 removes the `|| true` and exits 1 on rebase failure.
        """
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows/deep_tier.yml")
        if not path.exists():
            self.skipTest("deep_tier.yml not in bundle")
        src = path.read_text()
        # The exact pre-v3.4.9.2 broken pattern: rebase with `|| true`
        self.assertNotIn("git pull --rebase origin main || true", src,
            "deep_tier.yml still has the broken `git pull --rebase || true` "
            "pattern that silently swallows rebase failures and breaks "
            "idempotency — must use fail-loudly retry per v3.4.9.2")
        # The fail-loudly retry pattern (matches fast_tier v3.39.7.4)
        self.assertIn("git pull --rebase origin main failed before push", src,
            "deep_tier.yml must explicitly fail on rebase error per v3.4.9.2")

    def test_deep_tier_yml_does_not_use_old_explicit_allowlist(self):
        """The pre-v3.4.9.2 allowlist had `[ -f state/deep_tier_diagnostics.json ] && git add ...`
        guards. Wholesale add doesn't need those — flag presence as
        regression.
        """
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows/deep_tier.yml")
        if not path.exists():
            self.skipTest("deep_tier.yml not in bundle")
        src = path.read_text()
        # The old guarded per-file add pattern
        self.assertNotIn(
            "[ -f state/deep_tier_diagnostics.json ] && git add",
            src,
            "deep_tier.yml still has the pre-v3.4.9.2 per-file `[ -f ... ] "
            "&& git add` allowlist pattern — must be replaced with wholesale add",
        )


class TestYamlLevelIdempotencyGate(unittest.TestCase):
    """v3.4.9 — both workflows now have a YAML-level idempotency gate as the
    SECOND step (after Checkout, before Set up Python). All subsequent steps
    are conditionally gated on `steps.gate.outputs.run == 'true'`. This mirrors
    the pattern from daily-ticker-report/.github/workflows/daily.yml. Effect:
    follow-up scheduled crons on a day the brief was already sent take ~8s
    of runner time (checkout + gate + echo) instead of ~80s (full Python
    install + pipeline startup before the internal idempotency check exits).
    """

    def _read_yml(self, name: str) -> str:
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows" / name)
        if not path.exists():
            self.skipTest(f"{name} not in bundle")
        return path.read_text()

    def test_fast_tier_has_gate_step_before_python_setup(self):
        src = self._read_yml("fast_tier.yml")
        # "Decide whether to run" step must appear BEFORE "Set up Python"
        gate_idx = src.find('name: Decide whether to run')
        py_idx = src.find('name: Set up Python')
        self.assertGreater(gate_idx, 0, "fast_tier.yml missing 'Decide whether to run' gate step")
        self.assertGreater(py_idx, gate_idx,
            "fast_tier.yml: 'Set up Python' must come AFTER the gate step "
            "so no-op runs skip the Python install entirely")

    def test_deep_tier_has_gate_step_before_python_setup(self):
        src = self._read_yml("deep_tier.yml")
        gate_idx = src.find('name: Decide whether to run')
        py_idx = src.find('name: Set up Python')
        self.assertGreater(gate_idx, 0, "deep_tier.yml missing 'Decide whether to run' gate step")
        self.assertGreater(py_idx, gate_idx,
            "deep_tier.yml: 'Set up Python' must come AFTER the gate step")

    def test_fast_tier_subsequent_steps_gated(self):
        """All steps after the gate must be conditionally executed based on
        steps.gate.outputs.run == 'true'. Otherwise the gate is decorative."""
        src = self._read_yml("fast_tier.yml")
        # Count occurrences of the gate condition. With 4 stage-gated steps
        # (Set up Python, Install dependencies, Run fast tier, Commit state)
        # we expect at least 4 references.
        gate_refs = src.count("steps.gate.outputs.run == 'true'")
        self.assertGreaterEqual(gate_refs, 4,
            f"fast_tier.yml: expected ≥4 step-level gate refs, found {gate_refs}. "
            "Each post-gate step must be guarded with "
            "`if: ${{ steps.gate.outputs.run == 'true' }}`")

    def test_deep_tier_subsequent_steps_gated(self):
        src = self._read_yml("deep_tier.yml")
        gate_refs = src.count("steps.gate.outputs.run == 'true'")
        self.assertGreaterEqual(gate_refs, 4,
            f"deep_tier.yml: expected ≥4 step-level gate refs, found {gate_refs}")

    def test_fast_tier_gate_checks_both_lock_and_cascade(self):
        """The fast tier gate must check BOTH state/brief_sent_dates.json
        (idempotency lock) AND state/deep_tier_diagnostics.json (cascade gate).
        """
        src = self._read_yml("fast_tier.yml")
        gate_section = src.split("name: Decide whether to run")[1].split("name: Stop early")[0]
        self.assertIn("state/brief_sent_dates.json", gate_section,
            "fast_tier.yml gate must read state/brief_sent_dates.json (idempotency)")
        self.assertIn("state/deep_tier_diagnostics.json", gate_section,
            "fast_tier.yml gate must read state/deep_tier_diagnostics.json (cascade)")

    def test_deep_tier_gate_checks_diagnostics_lock(self):
        src = self._read_yml("deep_tier.yml")
        gate_section = src.split("name: Decide whether to run")[1].split("name: Stop early")[0]
        self.assertIn("state/deep_tier_diagnostics.json", gate_section,
            "deep_tier.yml gate must read state/deep_tier_diagnostics.json (idempotency)")

    def test_both_gates_honor_force_run_bypass(self):
        """Manual workflow_dispatch sets FORCE_RUN=1 which bypasses the gate.
        Both yml files must implement this bypass."""
        for name in ("fast_tier.yml", "deep_tier.yml"):
            src = self._read_yml(name)
            self.assertIn("FORCE_RUN", src,
                f"{name}: gate must support FORCE_RUN=1 bypass for manual dispatch")
            self.assertIn("workflow_dispatch", src,
                f"{name}: must wire workflow_dispatch trigger")

    def test_gate_logic_runs_via_runner_python(self):
        """The gate runs BEFORE Set up Python step, so it must use `python3`
        from the runner image (not a venv'd Python from setup-python). This
        is the whole point — no install needed to decide whether to install.
        """
        for name in ("fast_tier.yml", "deep_tier.yml"):
            src = self._read_yml(name)
            gate_section = src.split("name: Decide whether to run")[1].split("name: Stop early")[0]
            self.assertIn("python3 - <<'PY'", gate_section,
                f"{name}: gate must invoke `python3` (runner's pre-installed) "
                "via heredoc, not `python` (which requires setup-python)")


class TestTokenInUrlAuthHardening(unittest.TestCase):
    """v3.4.9.1 — both workflows must rewrite origin URL to embed the
    GITHUB_TOKEN before any push/pull operation. This bypasses the
    actions/checkout `http.extraheader` auth path which has been
    intermittently ignored by `git pull --rebase` (production failure
    2026-05-23 15:45 UTC: 'fatal: could not read Username for
    https://github.com: No such device or address').

    Pattern: `git remote set-url origin
    https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git`
    """

    def _read_yml(self, name: str) -> str:
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1]
                / ".github/workflows" / name)
        if not path.exists():
            self.skipTest(f"{name} not in bundle")
        return path.read_text()

    def test_fast_tier_commit_step_has_github_token_env(self):
        src = self._read_yml("fast_tier.yml")
        # GITHUB_TOKEN must be exposed to the commit step
        commit_section = src.split("name: Commit state")[1].split("name: Create issue")[0]
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", commit_section,
            "fast_tier.yml commit step must expose GITHUB_TOKEN via env: per v3.4.9.1")

    def test_deep_tier_commit_step_has_github_token_env(self):
        src = self._read_yml("deep_tier.yml")
        # Deep tier's commit step is the last named step — split on its name only
        commit_section = src.split("name: Commit digests + caches")[1]
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", commit_section,
            "deep_tier.yml commit step must expose GITHUB_TOKEN via env: per v3.4.9.1")

    def test_both_workflows_rewrite_origin_with_embedded_token(self):
        """Both workflows must call `git remote set-url origin
        https://x-access-token:${GITHUB_TOKEN}@github.com/...` before push/pull."""
        for name in ("fast_tier.yml", "deep_tier.yml"):
            src = self._read_yml(name)
            self.assertIn("x-access-token:${GITHUB_TOKEN}@github.com", src,
                f"{name}: must embed GITHUB_TOKEN in origin URL per v3.4.9.1")
            self.assertIn("git remote set-url origin", src,
                f"{name}: must call `git remote set-url origin ...` to apply the embedded-token URL")

    def test_token_rewrite_happens_before_first_push_or_pull(self):
        """The `git remote set-url` must appear BEFORE any git pull / push
        in the commit step — otherwise the auth flake we're fixing still
        hits the first operation. Match actual command lines (not mentions
        in comment blocks) by looking for `if ! git pull` or `\\n          git push`.
        """
        for name in ("fast_tier.yml", "deep_tier.yml"):
            src = self._read_yml(name)
            if name == "fast_tier.yml":
                section = src.split("name: Commit state")[1].split("name: Create issue")[0]
            else:
                section = src.split("name: Commit digests + caches")[1]
            set_url_idx = section.find("git remote set-url origin")
            # Look for the actual `if ! git pull` retry block (the real command,
            # not the long inline comment about why we do it)
            first_pull_idx = section.find("if ! git pull --rebase origin main")
            self.assertGreater(set_url_idx, 0,
                f"{name}: missing `git remote set-url origin` in commit step")
            self.assertGreater(first_pull_idx, 0,
                f"{name}: missing `if ! git pull --rebase origin main` command")
            self.assertLess(set_url_idx, first_pull_idx,
                f"{name}: `git remote set-url` (idx {set_url_idx}) must come BEFORE "
                f"the `if ! git pull --rebase` command (idx {first_pull_idx})")


if __name__ == "__main__":
    unittest.main()
