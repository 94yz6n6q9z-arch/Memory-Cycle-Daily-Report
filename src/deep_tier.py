"""
Deep tier entry point — runs 03:00 UTC daily.

What this does:
  1. Fetch earnings transcripts published recently — from free sources:
       • Direct IR-site scrapers (SK Hynix, Samsung, Micron)
       • Motley Fool transcripts (US-listed names, English calls)
     (Replaces the gated Quartr API as of v3.31 — see transcript_sources.py)
  2. Fetch Korean broker English PDFs (Mirae + NH + Samsung Securities)
  3. Run Layer 1 digester (Haiku) over each new document
  4. Persist digests to state/digests/{YYYY-MM-DD}/

The fast tier reads from state/digests/ next morning. Decoupling means
the deep tier can be slow (10-25 min) without affecting brief delivery.

If the deep tier fails entirely, the fast tier still runs — it just
sees whatever digests existed from prior days and tags the assessment
with reduced confidence.

TRANSCRIPT AVAILABILITY: every transcript source returns a three-state flag
(full_transcript / materials_only / nothing). The digester records the state
on every digest, so the brief can say "commentary NOT available" explicitly
instead of silently omitting a company. Getting commentary right is the point;
a missing transcript is a tracked fact.

CHANGELOG (this file):
  v3.31  (2026-05-14) — Step 1 rewired to transcript_sources (was Quartr); writes state/transcript_status.json
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from src.config import IR_SOURCES, MOTLEY_FOOL_TICKERS
from src.data.transcript_sources import fetch_transcripts_with_retry
from src.data.korean_brokers import (
    fetch_mirae_with_retry,
    fetch_nh_with_retry,
    fetch_samsung_securities_with_retry,
)
from src.analysis.digester import (
    digest_transcript_results,
    digest_korean_broker_notes,
)
from src.state import digests_dir_for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("memory_cycle_brief.deep_tier")


def run_deep_tier() -> int:
    """Execute the deep-tier pipeline. Returns exit code (0 = success)."""
    today = date.today()
    logger.info(f"=== DEEP TIER START — {today.isoformat()} ===")

    digests_dir_for(today)  # ensure directory exists

    # ----- Step 1: Earnings transcripts (IR scrapers + Motley Fool) -----
    logger.info("Fetching earnings transcripts (IR scrapers + Motley Fool)...")
    transcript_payload, transcript_meta = fetch_transcripts_with_retry(
        IR_SOURCES, MOTLEY_FOOL_TICKERS
    )
    logger.info(f"Transcripts: status={transcript_meta.get('status')}")
    if transcript_meta.get("summary"):
        s = transcript_meta["summary"]
        logger.info(
            f"  → {s.get('full_transcript', 0)} full, "
            f"{s.get('materials_only', 0)} materials-only, "
            f"{s.get('nothing', 0)} nothing"
        )

    transcript_digests = []
    if transcript_payload:
        transcript_digests = digest_transcript_results(transcript_payload)
        logger.info(f"Wrote {len(transcript_digests)} transcript digests (incl. gap records)")

    # Persist a transcript-availability status file so the fast tier (separate
    # run) can surface commentary gaps in the email's validation section.
    _write_transcript_status(transcript_payload, transcript_meta)

    # ----- Step 2: Korean broker PDFs -----
    logger.info("Fetching Korean broker PDFs (Mirae + NH + Samsung Securities)...")
    mirae_payload, mirae_meta = fetch_mirae_with_retry()
    nh_payload, nh_meta = fetch_nh_with_retry()
    samsung_payload, samsung_meta = fetch_samsung_securities_with_retry()

    broker_payloads = [p for p in (mirae_payload, nh_payload, samsung_payload) if p]
    broker_digests = digest_korean_broker_notes(broker_payloads)
    logger.info(f"Wrote {len(broker_digests)} Korean broker note digests")

    total_digests = len(transcript_digests) + len(broker_digests)
    logger.info(f"=== DEEP TIER COMPLETE — {total_digests} digests written ===")

    # Non-zero only on catastrophic failure (Anthropic API completely unreachable
    # AND every source failed). Note: transcript gap-records still count as digests,
    # so a normal "no earnings this week" day produces gap digests, not zero.
    sources_ok = any((transcript_payload, mirae_payload, nh_payload, samsung_payload))
    if total_digests == 0 and not sources_ok:
        logger.error("Deep tier produced 0 digests AND all sources failed — exit 1")
        return 1
    return 0


def _write_transcript_status(transcript_payload, transcript_meta) -> None:
    """Write state/transcript_status.json so the fast tier can surface
    transcript-commentary availability in the email validation section.

    Records, per company: the availability state and any gap note. The fast
    tier reads this and adds one SourceFetchStatus row per transcript source,
    so 'commentary NOT available' is visible in the daily brief.
    """
    import json
    from pathlib import Path

    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    status_path = state_dir / "transcript_status.json"

    payload = {
        "written_at_iso": datetime.utcnow().isoformat(),
        "status": transcript_meta.get("status", "unknown"),
        "summary": (transcript_payload or {}).get("summary", {}),
        "results": [
            {
                "company": r.get("company"),
                "ticker": r.get("ticker"),
                "availability": r.get("availability"),
                "source_name": r.get("source_name"),
                "note": r.get("note"),
                "char_count": r.get("char_count", 0),
            }
            for r in (transcript_payload or {}).get("results", [])
        ],
    }
    try:
        with open(status_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"Wrote transcript status: {status_path}")
    except Exception as e:
        logger.warning(f"Could not write transcript status file: {e}")


if __name__ == "__main__":
    sys.exit(run_deep_tier())
