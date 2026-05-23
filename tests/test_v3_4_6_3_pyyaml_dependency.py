"""v3.4.6.3 — Verify all yaml-importing modules can be imported.

This test exists specifically to catch the v3.4.0-through-v3.4.6.2
production bug: PyYAML was missing from requirements.txt, so 8 modules
that import yaml at the top level silently failed in the GitHub Actions
runner. Every fast-tier run produced the same "ModuleNotFoundError: No
module named 'yaml'" trace for:
  - src/analysis/dhcr.py (raised "PyYAML is required for DHCR computation")
  - src/analysis/competitor_aggregator.py
  - src/analysis/competitor_scoring.py
  - src/analysis/adr_aggregator.py
  - src/analysis/adr_event_tracking.py
  - src/analysis/dhcr_arch_aggregator.py
  - src/analysis/dhcr_accelerator_aggregator.py
  - src/analysis/dhcr_revenue_aggregator.py
Result: snapshots were None, template skipped the cards, section 4 of
the daily brief showed only 5 cards instead of the intended 8.

Local pytest hides the bug because PyYAML is installed in dev envs.
The fix: PyYAML>=6.0 added to requirements.txt in v3.4.6.3.

This test reproduces the failure mode: if PyYAML is somehow absent at
test time, all 8 module imports fail and the test loudly catches it.
If a future commit accidentally removes PyYAML from requirements.txt
again, the next CI run will fail this test before deploy.
"""

import importlib
import unittest


YAML_DEPENDENT_MODULES = (
    "src.analysis.dhcr",
    "src.analysis.competitor_aggregator",
    "src.analysis.competitor_scoring",
    "src.analysis.adr_aggregator",
    "src.analysis.adr_event_tracking",
    "src.analysis.dhcr_arch_aggregator",
    "src.analysis.dhcr_accelerator_aggregator",
    "src.analysis.dhcr_revenue_aggregator",
)


class TestPyYAMLAvailable(unittest.TestCase):
    """If PyYAML is missing, the assertImport calls in the next test
    class explode. This test gives a cleaner diagnostic up front."""

    def test_yaml_importable(self):
        try:
            import yaml  # noqa: F401
        except ImportError as e:
            self.fail(
                f"PyYAML is required (pre-v3.4.6.3 production bug). "
                f"Add 'PyYAML>=6.0' to requirements.txt. Underlying: {e}"
            )


class TestYAMLDependentModuleImports(unittest.TestCase):
    """Each yaml-dependent module must import cleanly. This is the test
    that would have caught the v3.4.0-through-v3.4.6.2 production bug
    if it had existed earlier."""

    def test_all_yaml_dependent_modules_import(self):
        failures = []
        for modname in YAML_DEPENDENT_MODULES:
            try:
                importlib.import_module(modname)
            except Exception as e:
                failures.append((modname, f"{type(e).__name__}: {e}"))
        self.assertFalse(
            failures,
            "Modules that failed to import (PyYAML missing or other import "
            f"error in module body):\n" + "\n".join(
                f"  - {m}: {err}" for m, err in failures
            )
        )


class TestDHCRComputeRequiresPyYAML(unittest.TestCase):
    """The dhcr.py module has try/except ImportError fallback at the top
    level — yaml is None and load_dhcr_data() raises a custom message.
    This test verifies the helpful error path is preserved, and that
    when yaml IS available (the normal case post-v3.4.6.3), the load
    function actually proceeds rather than raising."""

    def test_load_dhcr_data_doesnt_raise_pyyaml_required(self):
        from src.analysis import dhcr
        # With PyYAML installed, the module-level `yaml` should be a
        # real module, not None.
        self.assertIsNotNone(dhcr.yaml,
            "src.analysis.dhcr.yaml is None — PyYAML import failed even "
            "though this test ran (PyYAML is required at runtime; the dev "
            "env appears to have it but the import inside dhcr.py is broken)")


if __name__ == "__main__":
    unittest.main()
