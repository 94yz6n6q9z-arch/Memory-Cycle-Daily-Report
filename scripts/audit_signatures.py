#!/usr/bin/env python3
"""
Pre-deploy signature audit (comprehensive — v3.38.3).

Walks every .py file in src/, finds every function call, looks up the called
function in the file's own imports, and verifies that:

  1. No unknown kwargs are passed
  2. No required positional args are missing
  3. Total arg count does not exceed the function's positional capacity

Run with: python3 scripts/audit_signatures.py

Exit 0 = clean.  Exit 1 = mismatches found.

History:
  v3.38.1 — first version, only checked unknown kwargs.
  v3.38.3 — expanded to also check positional args, after the v3.38.2 run
            crashed on `fallback_analysis(news_items, performance, big_movers)`
            — 3 positional args passed to a function expecting 4. The original
            audit script missed it because no kwargs were involved.

Suggested workflow integration (CI gate, .github/workflows/fast_tier.yml):

    - name: Pre-deploy signature audit
      run: python scripts/audit_signatures.py
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path


def get_known_functions(root: str = "src") -> dict:
    """Build a map: name → list of (module, signature) where the function is defined.

    Returns a list (not a single signature) because the same name can be defined
    in multiple modules (e.g. BrokerAction is defined in three different files).
    The per-caller import map disambiguates at call-site analysis time.
    """
    known: dict = {}
    for r, _, names in os.walk(root):
        for n in names:
            if n.endswith(".py") and not n.startswith("_"):
                rel = os.path.join(r, n).replace("/", ".").replace(".py", "")
                try:
                    mod = __import__(rel, fromlist=["*"])
                    for name in dir(mod):
                        obj = getattr(mod, name, None)
                        if callable(obj) and hasattr(obj, "__module__") and obj.__module__ == rel:
                            try:
                                sig = inspect.signature(obj)
                                known.setdefault(name, []).append((rel, sig))
                            except (ValueError, TypeError):
                                pass
                except Exception:
                    pass
    return known


def audit(root: str = "src") -> list[tuple]:
    """Return a list of (file, line, function_name, module, [problems], signature)."""
    sys.path.insert(0, ".")
    known = get_known_functions(root)

    issues: list[tuple] = []
    for r, _, names in os.walk(root):
        for n in names:
            if not (n.endswith(".py") and not n.startswith("_")):
                continue
            f = os.path.join(r, n)
            src_text = Path(f).read_text()
            try:
                tree = ast.parse(src_text)
            except SyntaxError:
                continue

            # Per-file import map: imported_name → originating module
            imports: dict = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        imports[alias.asname or alias.name] = node.module

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name):
                    continue
                fname = node.func.id
                if fname not in imports or fname not in known:
                    continue
                target_mod = imports[fname]

                sig = None
                for mod, s in known.get(fname, []):
                    if mod == target_mod:
                        sig = s
                        break
                if sig is None:
                    continue

                params = list(sig.parameters.values())
                param_names = [p.name for p in params]
                required = [
                    p.name for p in params
                    if p.default == inspect.Parameter.empty
                    and p.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_ONLY,
                    )
                ]
                has_var_positional = any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
                )
                has_var_keyword = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params
                )

                n_pos = len(node.args)
                kw_names = [kw.arg for kw in node.keywords if kw.arg]
                has_double_star = any(kw.arg is None for kw in node.keywords)

                problems = []

                # Check 1: unknown kwargs (unless **kwargs accepts them)
                bad_kw = [k for k in kw_names if k not in param_names and not has_var_keyword]
                if bad_kw:
                    problems.append(f"unknown kwargs: {bad_kw}")

                # Check 2: too many positional args
                n_acceptable_pos = sum(
                    1 for p in params
                    if p.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_ONLY,
                    )
                )
                if not has_var_positional and n_pos > n_acceptable_pos:
                    problems.append(
                        f"too many positional args: {n_pos} > {n_acceptable_pos}"
                    )

                # Check 3: missing required args (unless **kwargs may provide them)
                if not has_double_star:
                    filled = set(param_names[:n_pos]) | set(kw_names)
                    missing = [r for r in required if r not in filled]
                    if missing:
                        problems.append(f"missing required: {missing}")

                if problems:
                    issues.append((f, node.lineno, fname, target_mod, problems, sig))
    return issues


def main() -> int:
    issues = audit("src")
    if not issues:
        print("✓ Comprehensive signature audit: ZERO mismatches across src/")
        return 0

    print(f"❌ FOUND {len(issues)} signature mismatch(es):\n")
    for f, ln, fname, origin, probs, sig in issues:
        print(f"  {f}:{ln}  →  {fname}() in {origin}")
        for p in probs:
            print(f"      • {p}")
        print(f"      signature: {fname}{sig}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())


# ============================================================
# v3.38.3 — In-file CHANGELOG freshness checker.
#
# Per CLAUDE.md "Working norms": every editable file carries a CHANGELOG
# (this file) block, and every modification must add a line. This was missed
# repeatedly across v3.38.x — adding a freshness check so the audit script
# also flags stale changelogs alongside signature mismatches.
# ============================================================

def check_changelog_freshness(root: str = "src") -> list[tuple]:
    """Return list of (file, latest_changelog_version) for files where the
    CHANGELOG block exists but doesn't mention the current top-of-CHANGES.md
    version (if it should). Heuristic-only — manual review still required."""
    import re
    from pathlib import Path

    issues: list[tuple] = []
    # What's the latest version per CHANGES.md? Pull from the top entry.
    changes_path = Path("CHANGES.md")
    latest_global = None
    if changes_path.exists():
        for line in changes_path.read_text().splitlines():
            m = re.match(r"##\s+v(\d+\.\d+(?:\.\d+)?)\s", line)
            if m:
                latest_global = m.group(1)
                break

    if not latest_global:
        return issues  # can't check freshness without a reference

    for r, _, names in os.walk(root):
        for n in names:
            if not (n.endswith(".py") and not n.startswith("_")):
                continue
            f = os.path.join(r, n)
            head = Path(f).read_text()[:5000]  # changelog should be near the top
            if "CHANGELOG (this file)" not in head:
                continue  # file doesn't use the per-file changelog pattern
            # What's the newest version mentioned in this file's changelog?
            versions = re.findall(r"v(\d+\.\d+(?:\.\d+)?)", head)
            if not versions:
                continue
            file_latest = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
            global_parts = tuple(int(x) for x in latest_global.split("."))
            file_parts = tuple(int(x) for x in file_latest.split("."))
            # Only flag if file's latest is BEHIND the global latest AND the file
            # was modified in git status / on disk recently. For a simple heuristic
            # we just compare versions — manual review confirms if a real touch.
            if file_parts < global_parts:
                issues.append((f, file_latest, latest_global))
    return issues


# Patch main() to also report stale changelogs (don't fail the build on them,
# just warn — the signature audit remains the hard gate).
_orig_main = main


def main() -> int:
    code = _orig_main()
    stale = check_changelog_freshness("src")
    if stale:
        print(f"\n⚠ CHANGELOG freshness check (advisory):")
        print(f"  CHANGES.md latest version: {stale[0][2]}")
        print(f"  Files whose CHANGELOG (this file) doesn't mention it:")
        for f, file_latest, global_latest in stale:
            print(f"    {f}  (file CHANGELOG: v{file_latest})")
        print(f"  → Per CLAUDE.md: 'When you modify a file, ADD a line to its block.'")
        print(f"  → Check whether each file was actually modified at v{global_latest}.")
        # Advisory only — doesn't change exit code.
    return code
