"""v3.4.7 — Test that the release-extract deploy workflow is present and valid.

Why this test exists: past deploys silently dropped files via the
GitHub web UI drag-drop bug. v3.4.7 introduces a Release-triggered
workflow that downloads the bundle zip from the release asset,
extracts it, and commits to main in one atomic git push.

This test pins the workflow file's existence + structural validity
so it can't accidentally be deleted by a future bundle or break
silently from a YAML typo.
"""

import unittest
import yaml
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy_from_release.yml"


class TestDeployFromReleaseWorkflow(unittest.TestCase):

    def test_workflow_file_exists(self):
        self.assertTrue(
            WORKFLOW_PATH.exists(),
            f"Workflow file missing: {WORKFLOW_PATH}. Without it, Release "
            f"publishes won't trigger auto-deploys and we're back to "
            f"drag-drop, which silently drops files."
        )

    def test_workflow_yaml_is_valid(self):
        with open(WORKFLOW_PATH) as f:
            parsed = yaml.safe_load(f)
        self.assertIsInstance(parsed, dict)

    def test_workflow_triggers_on_release_published(self):
        with open(WORKFLOW_PATH) as f:
            parsed = yaml.safe_load(f)
        # PyYAML parses `on: ...` as the Python boolean True (because
        # YAML 1.1 treats `on` as a boolean). Accept either form.
        triggers = parsed.get("on", parsed.get(True, {}))
        self.assertIn("release", triggers, "Workflow must trigger on release events")
        self.assertIn("published", triggers["release"]["types"],
            "Workflow must trigger only on the `published` event, not drafts")

    def test_workflow_has_contents_write_permission(self):
        with open(WORKFLOW_PATH) as f:
            parsed = yaml.safe_load(f)
        perms = parsed.get("permissions", {})
        self.assertEqual(perms.get("contents"), "write",
            "Workflow needs contents: write to push the deploy commit to main")

    def test_workflow_refuses_draft_and_prerelease(self):
        """The workflow's `if:` condition must filter out drafts and pre-releases."""
        src = WORKFLOW_PATH.read_text()
        # Look for the safety check in the job filter
        self.assertIn("draft == false", src,
            "Workflow must refuse to deploy drafts")
        self.assertIn("prerelease == false", src,
            "Workflow must refuse to deploy pre-releases")

    def test_workflow_preserves_state_directory(self):
        """`state/` holds production runtime data — must never be overwritten."""
        src = WORKFLOW_PATH.read_text()
        self.assertIn("--exclude='state/'", src,
            "rsync must exclude state/ so production runtime JSONs are preserved")

    def test_workflow_preserves_workflows_directory(self):
        """Defensive: don't let a bundle accidentally overwrite this workflow itself."""
        src = WORKFLOW_PATH.read_text()
        self.assertIn("--exclude='.github/workflows/'", src,
            "rsync must exclude .github/workflows/ so this workflow can't "
            "accidentally brick itself with a stale bundle copy")

    def test_workflow_does_not_use_delete_flag(self):
        """rsync MUST NOT use --delete, so files not in the bundle aren't
        silently removed from main. Removals should be manual & deliberate.

        Strip comment lines first since "without --delete" appears
        intentionally in the documentation block.
        """
        lines = WORKFLOW_PATH.read_text().splitlines()
        non_comment = "\n".join(
            line for line in lines
            if not line.strip().startswith("#")
        )
        self.assertNotIn("--delete", non_comment,
            "rsync must NOT use --delete in the executable command — "
            "bundle is additive only")

    def test_workflow_refuses_multiple_zip_assets(self):
        """Ambiguous asset count → refuse rather than guess which to deploy."""
        src = WORKFLOW_PATH.read_text()
        self.assertIn("Multiple memory_cycle_brief_", src,
            "Workflow must error out on multiple matching assets")


if __name__ == "__main__":
    unittest.main()
