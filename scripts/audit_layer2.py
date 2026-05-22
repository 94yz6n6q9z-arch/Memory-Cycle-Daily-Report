#!/usr/bin/env python3
"""Layer 2 audit digest — summarize recent Layer 2 activity.

v3.4.3 — companion to the per-call audit logging added to
src/analysis/signal_reasoner.py. Reads state/layer2_audit_*.jsonl
files and produces a summary report answering:

  - How many Layer 2 calls per indicator over the trailing N days?
  - How often does each indicator preserve status vs change status?
  - Distribution of verdict_convergence + verdict_confidence per indicator
  - Status transitions actually observed (Fired → Not fired, etc.)
  - Sample reasoning quotes for any transition

Usage:
    python3 scripts/audit_layer2.py [--days 7] [--indicator t2_x]

Output is human-readable text to stdout. Pipe to a file or grep.

Run locally on a clone of the repo after pulling fresh state/ files —
the audit JSONL is committed alongside the other state files via the
v3.4.3 fast_tier.yml commit step.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


def find_audit_files(state_dir: Path, days: int) -> list[Path]:
    """Find layer2_audit_YYYY-MM-DD.jsonl files within trailing N days."""
    today = date.today()
    cutoff = today - timedelta(days=days)
    files = []
    for p in state_dir.glob("layer2_audit_*.jsonl"):
        try:
            date_part = p.stem.replace("layer2_audit_", "")
            file_date = datetime.fromisoformat(date_part).date()
            if file_date >= cutoff:
                files.append(p)
        except ValueError:
            continue
    return sorted(files)


def load_records(files: list[Path]) -> list[dict]:
    records = []
    for f in files:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def summarize(records: list[dict], filter_indicator: str | None = None) -> None:
    if filter_indicator:
        records = [r for r in records if r.get("signal_id") == filter_indicator]

    if not records:
        print("No audit records found in the specified window.")
        return

    print(f"=== Layer 2 audit summary — {len(records)} records ===\n")

    # By indicator
    by_indicator = defaultdict(list)
    for r in records:
        by_indicator[r.get("signal_id", "?")].append(r)

    print(f"{'INDICATOR':<40} {'CALLS':>6} {'CONV.':>6} {'CONF.':>6}  STATUS TRANSITIONS")
    print("-" * 110)
    for sig_id in sorted(by_indicator):
        sig_records = by_indicator[sig_id]
        n_calls = len(sig_records)
        avg_conv = sum(1 for r in sig_records if r.get("verdict_convergence") == "high") / max(n_calls, 1)
        avg_conf = sum(1 for r in sig_records if r.get("verdict_confidence") == "high") / max(n_calls, 1)
        transitions = Counter(r.get("status_transition") for r in sig_records if r.get("status_transition") and r.get("status_transition") != "no_change")
        trans_str = ", ".join(f"{k}×{v}" for k, v in transitions.most_common(3)) or "all preserved"
        print(f"{sig_id:<40} {n_calls:>6} {avg_conv:>6.1%} {avg_conf:>6.1%}  {trans_str}")

    # Status transitions detail
    transitions = [r for r in records if r.get("status_transition") and r.get("status_transition") != "no_change"]
    if transitions:
        print(f"\n=== {len(transitions)} status transitions in window ===")
        for r in transitions:
            print(f"\n[{r.get('ts','?')[:19]}] {r.get('signal_id','?')}: {r.get('status_transition')}")
            reason = r.get("verdict_evidence_for_change") or "(no reason recorded)"
            print(f"  reason: {reason[:300]}")
            consensus = r.get("verdict_consensus_direction") or ""
            if consensus:
                print(f"  consensus: {consensus[:300]}")

    # Reliability — did we have any prompt-input zero days?
    print(f"\n=== Reliability check ===")
    zero_evidence = [r for r in records if r.get("n_digests_relevant", 0) == 0 and r.get("n_news_relevant", 0) == 0]
    print(f"Calls with zero digests + zero news inputs: {len(zero_evidence)} of {len(records)} ({len(zero_evidence)/max(len(records),1):.1%})")
    print(f"  (these are the calls where Layer 2 has nothing to update from — should preserve status)")

    # Prompt size distribution
    prompt_sizes = [r.get("prompt_total_chars", 0) for r in records]
    if prompt_sizes:
        print(f"Prompt size: min={min(prompt_sizes)}, max={max(prompt_sizes)}, "
              f"avg={sum(prompt_sizes)/len(prompt_sizes):.0f} chars")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="Trailing window in days (default: 7)")
    ap.add_argument("--indicator", type=str, default=None, help="Filter to single signal_id")
    ap.add_argument("--state-dir", type=Path, default=Path("state"), help="state/ directory (default: ./state)")
    args = ap.parse_args()

    files = find_audit_files(args.state_dir, args.days)
    if not files:
        print(f"No layer2_audit_*.jsonl files found in {args.state_dir} for trailing {args.days} days")
        return 1

    print(f"Reading {len(files)} audit file(s): {[f.name for f in files]}\n")
    records = load_records(files)
    summarize(records, filter_indicator=args.indicator)
    return 0


if __name__ == "__main__":
    sys.exit(main())
