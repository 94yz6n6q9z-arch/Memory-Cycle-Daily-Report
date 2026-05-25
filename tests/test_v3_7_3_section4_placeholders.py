"""Tests for v3.7.3 — loud failure rendering for section-4 deep-dive cards.

Production bug 2026-05-25: DHCR card vanished from the brief because
`src/fast_tier.py:675` called `build_narrative_summary(snap_dict)` with both
symbols undefined in that scope. The bare `except Exception` swallowed the
`NameError` into `dhcr_snapshot = None`. `template.py:308`'s
`if dhcr_snapshot: parts.append(...)` then silently skipped card 6. The brief
jumped from indicator 5 to indicator 7 with zero visible signal to the
operator — the failure only surfaced in the GitHub Actions workflow log.

v3.7.3 changes addressed by these tests:
  1. The orphaned line at fast_tier.py:675 is removed (regression guard).
  2. Section-4 cards 6/7/8 always render — either the normal card or a
     "Computation failed" placeholder with the actual error message visible.
  3. `fast_tier.py`'s three section-4 try/except blocks capture the error
     string into `computed_metrics["{indicator}_error_message"]`.
  4. `build_email_html` accepts and threads the three new kwargs.
  5. Error messages are HTML-escaped before rendering (XSS guard against
     adversarial content in error strings).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Regression guard: the exact NameError from the 2026-05-25 production bug
# ══════════════════════════════════════════════════════════════════════════════

class TestNoOrphanedSymbols:
    """If anyone ever re-introduces the orphaned call, these tests fail."""

    def test_fast_tier_does_not_reference_build_narrative_summary_in_code(self):
        """fast_tier.py must not call build_narrative_summary anywhere.
        The function is internal to dhcr.py and is invoked by compute_dhcr_snapshot
        itself; the orphaned call at line 675 is what crashed production."""
        text = Path("src/fast_tier.py").read_text()
        # Strip comment lines so we only check executable code
        code_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_text = "\n".join(code_lines)
        assert "build_narrative_summary" not in code_text, (
            "fast_tier.py must not reference build_narrative_summary in "
            "executable code — that's the v3.7.3 regression we just fixed"
        )

    def test_fast_tier_does_not_reference_undefined_snap_dict(self):
        """`snap_dict` is a local variable inside compute_dhcr_snapshot,
        not in fast_tier's scope. Referencing it crashes."""
        text = Path("src/fast_tier.py").read_text()
        code_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_text = "\n".join(code_lines)
        assert "snap_dict[" not in code_text, (
            "fast_tier.py must not subscript snap_dict — that's the variable "
            "that doesn't exist in this scope"
        )

    def test_dhcr_snapshot_already_carries_narrative_summary(self):
        """Compute_dhcr_snapshot populates narrative_summary internally.
        This is why the orphaned line was redundant and removable."""
        # The DHCRSnapshot dataclass should declare narrative_summary
        dhcr_text = Path("src/analysis/dhcr.py").read_text()
        assert "narrative_summary: str" in dhcr_text, (
            "DHCRSnapshot.narrative_summary field is expected to exist"
        )
        # And compute_dhcr_snapshot is expected to populate it
        assert 'snap_dict["narrative_summary"] = build_narrative_summary' in dhcr_text


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.3 — Always-render placeholder for section-4 cards
# ══════════════════════════════════════════════════════════════════════════════

class TestSection4PlaceholderHelper:
    def test_helper_renders_placeholder_for_none_snapshot(self):
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=6,
            indicator_title="Decode HBM Captive Ratio (DHCR)",
            error_message="NameError: name 'build_narrative_summary' is not defined",
            normal_card_builder=lambda x: "<div>NORMAL CARD CONTENT</div>",
        )
        assert "Computation failed" in result
        assert "Indicator 6 of 8" in result
        assert "NameError" in result    # the actual exception type
        assert "build_narrative_summary" in result    # the actual error text
        assert "NORMAL CARD CONTENT" not in result    # normal builder not invoked

    def test_helper_uses_normal_builder_when_snapshot_present(self):
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot={"status": "Not fired", "score": 42},
            indicator_number=6,
            indicator_title="DHCR",
            error_message=None,
            normal_card_builder=lambda x: f"<div>card status={x['status']}</div>",
        )
        assert "card status=Not fired" in result
        assert "Computation failed" not in result
        assert "Indicator 6 of 8" not in result    # no placeholder text

    def test_helper_falls_through_to_placeholder_on_builder_exception(self):
        """Defense in depth: even if a builder bug crashes mid-render, the
        placeholder fires with the builder's exception as the diagnostic."""
        from src.email_module.template import _build_section4_card_or_placeholder

        def crashing_builder(snap):
            raise RuntimeError("simulated card builder bug")

        result = _build_section4_card_or_placeholder(
            snapshot={"status": "Not fired"},
            indicator_number=7,
            indicator_title="Competitor dynamics",
            error_message=None,    # no upstream error captured
            normal_card_builder=crashing_builder,
        )
        assert "Computation failed" in result
        assert "RuntimeError" in result
        assert "simulated card builder bug" in result

    def test_helper_falls_through_when_builder_returns_empty_string(self):
        """A builder returning "" was the legacy 'silent skip' mechanism.
        v3.7.3 treats that as a failure and fires the placeholder."""
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot={"status": "Not fired"},
            indicator_number=6,
            indicator_title="DHCR",
            error_message=None,
            normal_card_builder=lambda x: "",
        )
        assert "Computation failed" in result
        assert "returned empty string" in result

    def test_helper_html_escapes_error_messages(self):
        """Error strings come from raw exception messages and could in theory
        contain anything. The placeholder must HTML-escape them."""
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=8,
            indicator_title="ADR",
            error_message="<script>alert('xss')</script>",
            normal_card_builder=lambda x: "",
        )
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_helper_provides_log_keyword_for_grep(self):
        """The placeholder body tells the operator what to grep for in the
        workflow log to find the underlying traceback."""
        from src.email_module.template import (
            _build_section4_card_or_placeholder,
            _indicator_log_keyword,
        )
        assert _indicator_log_keyword(6) == "DHCR computation failed"
        assert _indicator_log_keyword(7) == "Indicator 7 computation failed"
        assert _indicator_log_keyword(8) == "Indicator 8 computation failed"

        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=6,
            indicator_title="DHCR",
            error_message="anything",
            normal_card_builder=lambda x: "",
        )
        assert "DHCR computation failed" in result

    def test_helper_falls_through_with_default_error_when_none_provided(self):
        """If no error message was captured upstream, the placeholder still
        renders with a generic message — never a blank or crash."""
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=6,
            indicator_title="DHCR",
            error_message=None,
            normal_card_builder=lambda x: "",
        )
        assert "Computation failed" in result
        assert "no specific error message was captured" in result


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.3 — build_email_html accepts new error_message kwargs
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildEmailHtmlAcceptsErrorMessages:
    def test_accepts_dhcr_error_message(self):
        import inspect
        from src.email_module.template import build_email_html
        sig = inspect.signature(build_email_html)
        assert "dhcr_error_message" in sig.parameters

    def test_accepts_competitor_error_message(self):
        import inspect
        from src.email_module.template import build_email_html
        sig = inspect.signature(build_email_html)
        assert "competitor_error_message" in sig.parameters

    def test_accepts_adr_error_message(self):
        import inspect
        from src.email_module.template import build_email_html
        sig = inspect.signature(build_email_html)
        assert "adr_error_message" in sig.parameters

    def test_error_message_kwargs_default_to_none(self):
        """Existing callers should not need updating — the kwargs are optional."""
        import inspect
        from src.email_module.template import build_email_html
        sig = inspect.signature(build_email_html)
        for k in ("dhcr_error_message", "competitor_error_message", "adr_error_message"):
            assert sig.parameters[k].default is None


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.3 — Silent-skip guards removed from section-4
# ══════════════════════════════════════════════════════════════════════════════

class TestSilentSkipRemoved:
    def test_template_no_longer_silent_skips_dhcr(self):
        """The legacy `if dhcr_snapshot:` guard at template.py:308 is replaced
        by an unconditional call to the or_placeholder helper."""
        text = Path("src/email_module/template.py").read_text()
        # The legacy pattern should not exist (verbatim)
        assert "    if dhcr_snapshot:\n        parts.append(_build_dhcr_section4_card" not in text, (
            "Legacy silent-skip pattern for DHCR must not exist; "
            "the v3.7.3 or_placeholder helper replaces it"
        )

    def test_template_no_longer_silent_skips_competitor(self):
        text = Path("src/email_module/template.py").read_text()
        assert "    if competitor_snapshot:\n        parts.append(_build_competitor_section4_card" not in text

    def test_template_no_longer_silent_skips_adr(self):
        text = Path("src/email_module/template.py").read_text()
        assert "    if adr_snapshot:\n        parts.append(_build_adr_section4_card" not in text

    def test_template_uses_or_placeholder_helper_three_times(self):
        """All three section-4 deep-dive cards now route through the helper."""
        text = Path("src/email_module/template.py").read_text()
        # Count the helper invocations in the body (not in its own definition)
        usage_pattern = re.compile(
            r"parts\.append\(\s*_build_section4_card_or_placeholder\(",
            re.MULTILINE,
        )
        usages = usage_pattern.findall(text)
        assert len(usages) == 3, (
            f"Expected 3 helper invocations (DHCR, Competitor, ADR); "
            f"found {len(usages)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.3 — fast_tier captures error messages into computed_metrics
# ══════════════════════════════════════════════════════════════════════════════

class TestFastTierErrorCapture:
    def test_dhcr_except_handler_records_error_message(self):
        """When DHCR compute raises, the error string must be stashed into
        computed_metrics['dhcr_error_message'] so the placeholder can show it."""
        text = Path("src/fast_tier.py").read_text()
        assert 'computed_metrics["dhcr_error_message"]' in text

    def test_competitor_except_handler_records_error_message(self):
        text = Path("src/fast_tier.py").read_text()
        assert 'computed_metrics["competitor_error_message"]' in text

    def test_adr_except_handler_records_error_message(self):
        text = Path("src/fast_tier.py").read_text()
        assert 'computed_metrics["adr_error_message"]' in text

    def test_fast_tier_threads_error_messages_to_template(self):
        """The build_email_html call must pass the error_message kwargs."""
        text = Path("src/fast_tier.py").read_text()
        assert "dhcr_error_message=computed_metrics.get(\"dhcr_error_message\")" in text
        assert "competitor_error_message=computed_metrics.get(\"competitor_error_message\")" in text
        assert "adr_error_message=computed_metrics.get(\"adr_error_message\")" in text


# ══════════════════════════════════════════════════════════════════════════════
# v3.7.3 — End-state requirement: section 4 always numbers 6/7/8 visibly
# ══════════════════════════════════════════════════════════════════════════════

class TestSection4IndicatorNumbering:
    """The actual operator-visible requirement: no jump from 5 to 7."""

    def test_placeholder_shows_indicator_6_of_8_label(self):
        """The DHCR placeholder must explicitly say 'Indicator 6 of 8' so
        the operator knows the card is structurally supposed to be there."""
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=6,
            indicator_title="Decode HBM Captive Ratio (DHCR)",
            error_message="any error",
            normal_card_builder=lambda x: "",
        )
        assert "Indicator 6 of 8" in result

    def test_placeholder_shows_indicator_7_of_8_label(self):
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=7,
            indicator_title="Competitor dynamics",
            error_message="any error",
            normal_card_builder=lambda x: "",
        )
        assert "Indicator 7 of 8" in result

    def test_placeholder_shows_indicator_8_of_8_label(self):
        from src.email_module.template import _build_section4_card_or_placeholder
        result = _build_section4_card_or_placeholder(
            snapshot=None,
            indicator_number=8,
            indicator_title="US ADR / SPO event",
            error_message="any error",
            normal_card_builder=lambda x: "",
        )
        assert "Indicator 8 of 8" in result
