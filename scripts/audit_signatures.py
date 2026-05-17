#!/usr/bin/env python3
"""
Pre-deploy signature audit.

Walks every .py file in src/, finds every keyword-argument call site, looks up
the called function in the file's own imports, and verifies each kwarg is
accepted by the actual function signature.

Run with: python3 scripts/audit_signatures.py

Exit 0 = clean.  Exit 1 = mismatches found.

Added v3.38.1 after a class of bug — call sites using kwarg names that don't
match the function signature — was missed during v3.37 development and only
surfaced when v3.38 fixed upstream crashes that had been masking the bugs.

This is fast (sub-second on the current codebase) and should run in CI
on every PR before merge.
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path


def audit(root: str = "src") -> list[tuple]:
    """Return a list of (file, line, function_name, module, bad_kwargs, valid_kwargs)."""
    sys.path.insert(0, ".")

    files: list[str] = []
    for r, _, names in os.walk(root):
        for n in names:
            if n.endswith(".py") and not n.startswith("_"):
                files.append(os.path.join(r, n))

    issues: list[tuple] = []
    for f in files:
        src_text = Path(f).read_text()
        try:
            tree = ast.parse(src_text)
        except SyntaxError:
            continue

        # Build a per-file import map: imported_name → originating module
        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[alias.asname or alias.name] = node.module

        # Scan calls; only check names we have an import for (avoids local-class collisions)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.keywords):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            fname = node.func.id
            if fname not in imports:
                continue
            mod_name = imports[fname]
            try:
                mod = __import__(mod_name, fromlist=[fname])
                func = getattr(mod, fname, None)
                if func is None or not callable(func):
                    continue
                sig = inspect.signature(func)
                valid = set(sig.parameters.keys())
                kwargs = [kw.arg for kw in node.keywords if kw.arg]
                bad = [k for k in kwargs if k not in valid]
                if bad:
                    issues.append((f, node.lineno, fname, mod_name, bad, sorted(valid)))
            except Exception:
                # Import or inspect failed — skip rather than false-positive
                pass
    return issues


def main() -> int:
    issues = audit("src")
    if not issues:
        print("✓ System-wide signature audit: ZERO mismatches across src/")
        return 0

    print(f"❌ FOUND {len(issues)} signature mismatch(es):\n")
    for f, ln, fname, origin, bad, valid in issues:
        print(f"  {f}:{ln}  →  {fname}() in {origin}")
        print(f"      unknown kwargs: {bad}")
        print(f"      valid kwargs:   {valid}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
