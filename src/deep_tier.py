"""
Deep tier entry point — runs 08:00 UTC daily.

What this does:
  1. Fetch earnings transcripts via a sequential cascade (v3.33):
       • Motley Fool (primary for US names — direct fetch, no proxy)
       • IR-site scrapers (Micron full; SK Hynix / Samsung materials-only)
       • Yahoo Finance (fallback ONLY for companies still missing a full
         transcript — runs through residential proxy when RESIDENTIAL_PROXY_URL
         is set; covers SK Hynix / Samsung that no other free source covers)
     (Replaced Quartr API in v3.31; Yahoo added v3.32; cascade order in v3.33.
     See transcript_sources.py for the full picture.)
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
  v3.38  (2026-05-17) — Added full DiagnosticsCollector wiring (no prior instrumentation existed for the deep tier). Imports DiagnosticsCollector, SourceFetchStatus, status_from_retry_meta, statuses_to_dict_list from src.diagnostics. New helpers `_record_transcript_source_rows()` decomposes the transcript cascade into per-source rows (Motley Fool / Yahoo Finance / IR scrapers aggregated) with per-availability detail; per-broker rows for Mirae / NH / Samsung Securities each via status_from_retry_meta; roll-up status row for the Haiku digester distinguishing ok / fail / quiet-day; `_write_deep_tier_diagnostics()` persists collector to state/deep_tier_diagnostics.json so the fast tier (separate run, hours later) can ingest into the email validation table.
  v3.33  (2026-05-16) — Step 1 docstring updated for sequential cascade; datetime.utcnow() → datetime.now(timezone.utc) (Python 3.12+ deprecation fix); transcript_payload now also surfaces `already_processed` results which the digester should skip
  v3.32  (2026-05-14) — Step 1 now passes YAHOO_TRANSCRIPT_TICKERS to fetch_transcripts_with_retry (Yahoo Finance is primary transcript source)
  v3.31  (2026-05-14) — Step 1 rewired to transcript_sources (was Quartr); writes state/transcript_status.json
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src.config import IR_SOURCES, MOTLEY_FOOL_TICKERS, YAHOO_TRANSCRIPT_TICKERS
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
from src.diagnostics import (
    DiagnosticsCollector, SourceFetchStatus,
    status_from_retry_meta, statuses_to_dict_list,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("memory_cycle_brief.deep_tier")


def run_deep_tier() -> int:
    """Execute the deep-tier pipeline. Returns exit code (0 = success).

    v3.39.3: idempotency check. The workflow now schedules 5 cron triggers
    in a 60-minute window (10:03 / 10:17 / 10:33 / 10:47 / 11:03 UTC) to
    dodge GitHub Actions cron drift. First successful run writes
    state/deep_tier_diagnostics.json; subsequent triggers see today's
    diagnostics file and exit cleanly without re-running expensive
    transcript fetches and Haiku digester calls.
    """
    today = date.today()
    logger.info(f"=== DEEP TIER START — {today.isoformat()} ===")

    # v3.39.3: idempotency check — if today's diagnostics file exists,
    # this is a duplicate trigger (one of the later cron slots after a
    # prior trigger already succeeded). Exit cleanly in ~10s instead of
    # re-running the full ~4-6 min pipeline. Manual workflow_dispatch
    # users wanting to force a re-run should delete the file first
    # (or just touch it forward).
    diagnostics_path = Path("state") / "deep_tier_diagnostics.json"
    if diagnostics_path.exists():
        try:
            with open(diagnostics_path) as f:
                existing = json.load(f)
            if existing.get("run_date") == today.isoformat():
                logger.info(
                    f"Idempotent no-op: state/deep_tier_diagnostics.json already "
                    f"dated {today.isoformat()} (prior trigger today succeeded). "
                    f"Exiting cleanly."
                )
                return 0
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                f"Could not parse existing diagnostics file ({e}); "
                f"proceeding with fresh run."
            )

    digests_dir_for(today)  # ensure directory exists

    # v3.38: collector for deep-tier sources. Statuses are persisted to
    # state/deep_tier_diagnostics.json at the end of the run; the fast tier
    # reads that file next morning and includes the rows in the email's
    # validation table. This closes the silent-failure surface for the
    # deep tier — every transcript source, every Korean broker feed, and
    # the Haiku digester all surface a row daily.
    deep_collector = DiagnosticsCollector()

    # ----- Step 1: Earnings transcripts (Yahoo Finance + Motley Fool + IR scrapers) -----
    logger.info("Fetching earnings transcripts (Yahoo Finance + Motley Fool + IR scrapers)...")
    transcript_payload, transcript_meta = fetch_transcripts_with_retry(
        IR_SOURCES, MOTLEY_FOOL_TICKERS, YAHOO_TRANSCRIPT_TICKERS
    )
    logger.info(f"Transcripts: status={transcript_meta.get('status')}")
    if transcript_meta.get("summary"):
        s = transcript_meta["summary"]
        logger.info(
            f"  → {s.get('full_transcript', 0)} full, "
            f"{s.get('materials_only', 0)} materials-only, "
            f"{s.get('already_processed', 0)} already-processed (cached), "
            f"{s.get('nothing', 0)} nothing"
        )
        yahoo_attempted = s.get("yahoo_attempted_for", [])
        if yahoo_attempted:
            logger.info(f"  → Yahoo fallback invoked for: {yahoo_attempted}")

    # v3.38: decompose the transcript cascade into per-source rows so the
    # email validation table shows which of Yahoo / Motley Fool / IR scrapers
    # actually delivered today. Without this you'd see ONE row for the whole
    # cascade and a Yahoo-proxy outage would be invisible if MF + IR happened
    # to cover the same companies.
    _record_transcript_source_rows(deep_collector, transcript_payload, transcript_meta)

    transcript_digests = []
    if transcript_payload:
        transcript_digests = digest_transcript_results(transcript_payload)
        logger.info(f"Wrote {len(transcript_digests)} transcript digests (incl. gap records)")

    # Persist a transcript-availability status file so the fast tier (separate
    # run) can surface commentary gaps in the email's validation section.
    _write_transcript_status(transcript_payload, transcript_meta)

    # ----- Step 2: Korean broker PDFs -----
    logger.info("Fetching Korean broker PDFs (Mirae + NH + Samsung Securities)...")
    mirae_payload,   mirae_meta   = fetch_mirae_with_retry()
    nh_payload,      nh_meta      = fetch_nh_with_retry()
    samsung_payload, samsung_meta = fetch_samsung_securities_with_retry()

    # v3.38: per-broker rows. Each broker independently fails / hits stale
    # cache / returns N notes — they're not interchangeable (Mirae covers
    # CXMT depth, Samsung Securities is the only Gmail-IMAP path).
    deep_collector.record(status_from_retry_meta(
        "Mirae Asset (EN broker PDFs)", "broker_pdf",
        mirae_payload, mirae_meta,
        items_count=len((mirae_payload or {}).get("notes", [])),
    ))
    deep_collector.record(status_from_retry_meta(
        "NH Investment (broker PDFs)", "broker_pdf",
        nh_payload, nh_meta,
        items_count=len((nh_payload or {}).get("notes", [])),
    ))
    deep_collector.record(status_from_retry_meta(
        "Samsung Securities (Gmail IMAP)", "broker_pdf",
        samsung_payload, samsung_meta,
        items_count=len((samsung_payload or {}).get("notes", [])),
    ))

    broker_payloads = [p for p in (mirae_payload, nh_payload, samsung_payload) if p]
    broker_digests = digest_korean_broker_notes(broker_payloads)
    logger.info(f"Wrote {len(broker_digests)} Korean broker note digests")

    # v3.38: roll-up status row for Anthropic Haiku digester. The digester
    # makes one call per (uncached) transcript + one per broker note. We
    # don't have inner instrumentation yet (would need to thread the
    # collector through digester.py — out of scope for v3.38), so this is
    # a single row counting digests written. Health flags:
    #   • ok      = digests written
    #   • warn    = zero digests AND zero sources reached (legitimate quiet day)
    #   • fail    = zero digests but sources had content (digester died)
    total_digests = len(transcript_digests) + len(broker_digests)
    sources_returned_content = any((transcript_payload, mirae_payload, nh_payload, samsung_payload))
    if total_digests > 0:
        deep_collector.record(SourceFetchStatus(
            source_name="Anthropic Haiku (digester)", source_type="llm",
            reached=True, items_returned=total_digests, latency_ms=0,
            detail=f"{len(transcript_digests)} transcript + {len(broker_digests)} broker note digests",
        ))
    elif sources_returned_content:
        deep_collector.record(SourceFetchStatus(
            source_name="Anthropic Haiku (digester)", source_type="llm",
            reached=False, items_returned=0, latency_ms=0,
            error="Sources had content but 0 digests written — digester failed",
            detail="check state/digests/{YYYY-MM-DD}/ and Anthropic API rate-limit logs",
        ))
    else:
        deep_collector.record(SourceFetchStatus(
            source_name="Anthropic Haiku (digester)", source_type="llm",
            reached=True, items_returned=0, latency_ms=0,
            detail="0 digests written — quiet day, no source content to digest",
        ))

    logger.info(f"=== DEEP TIER COMPLETE — {total_digests} digests written ===")

    # v3.38: persist deep-tier diagnostics for fast-tier ingestion. Written
    # unconditionally so the fast tier can detect 'deep tier didn't run' vs
    # 'deep tier ran and everything failed'.
    _write_deep_tier_diagnostics(deep_collector)

    # Non-zero only on catastrophic failure (Anthropic API completely unreachable
    # AND every source failed). Note: transcript gap-records still count as digests,
    # so a normal "no earnings this week" day produces gap digests, not zero.
    if total_digests == 0 and not sources_returned_content:
        logger.error("Deep tier produced 0 digests AND all sources failed — exit 1")
        return 1
    return 0


def _record_transcript_source_rows(collector, transcript_payload, transcript_meta) -> None:
    """v3.38 — emit per-source rows for the transcript cascade.

    Groups TranscriptResults by source_name (Motley Fool / Yahoo Finance /
    each "{company} IR" variant) and emits one SourceFetchStatus per group
    with: how many companies the source covered, how many full transcripts,
    materials-only, and gap (nothing) outcomes.

    If the cascade itself failed (transcript_payload is None), emit one
    FAIL row labeled with the wrapper error.
    """
    # Cascade-level failure first
    if transcript_payload is None:
        collector.record(SourceFetchStatus(
            source_name="Transcript cascade (Yahoo + Motley Fool + IR)",
            source_type="transcript",
            reached=False, items_returned=0, latency_ms=0,
            error=transcript_meta.get("reason", "unknown"),
            detail=f"status={transcript_meta.get('status', 'unknown')}",
        ))
        return

    results = transcript_payload.get("results", [])
    if not results:
        collector.record(SourceFetchStatus(
            source_name="Transcript cascade (Yahoo + Motley Fool + IR)",
            source_type="transcript",
            reached=True, items_returned=0, latency_ms=0,
            detail="cascade returned 0 results (no earnings window matched)",
        ))
        return

    # Group by source_name — coalesce all "{company} IR" rows into "IR scrapers"
    # for a sensible row count (5 IR rows is noise; 1 IR-aggregate is signal).
    by_source: dict[str, list[dict]] = {}
    for r in results:
        sn = r.get("source_name", "(unknown)")
        if sn.endswith(" IR") or "IR (newsroom RSS)" in sn or sn.endswith(" IR"):
            key = "IR scrapers (SK Hynix / Samsung / Micron)"
        else:
            key = sn  # "Motley Fool", "Yahoo Finance"
        by_source.setdefault(key, []).append(r)

    for source_label, rs in by_source.items():
        n_full = sum(1 for r in rs if r.get("availability") == "full_transcript")
        n_mat  = sum(1 for r in rs if r.get("availability") == "materials_only")
        n_done = sum(1 for r in rs if r.get("availability") == "already_processed")
        n_none = sum(1 for r in rs if r.get("availability") == "nothing")
        n_companies = len({r.get("company") for r in rs if r.get("company")})

        detail = (f"{n_companies} co. · {n_full} full / {n_mat} materials-only / "
                  f"{n_done} cached / {n_none} nothing")
        # Items returned counts as full + materials (real new content).
        items = n_full + n_mat
        # Reached = the source was invoked. Even if 0 new content this run,
        # we still want a row (it's not a failure). 'nothing' across the
        # board with no cached hits is a warn signal.
        all_nothing = (n_full == 0 and n_mat == 0 and n_done == 0 and n_none > 0)
        if all_nothing:
            collector.record(SourceFetchStatus(
                source_name=source_label, source_type="transcript",
                reached=True, items_returned=0, latency_ms=0,
                detail=f"{detail} — investigate if expected coverage this window",
            ))
        else:
            collector.record(SourceFetchStatus(
                source_name=source_label, source_type="transcript",
                reached=True, items_returned=items, latency_ms=0,
                detail=detail,
            ))


def _write_deep_tier_diagnostics(collector) -> None:
    """v3.38 — persist deep-tier collector to state/deep_tier_diagnostics.json
    so the fast tier (separate run, hours later) can include these rows in the
    email's validation table. Includes a written_at_iso so the fast tier can
    detect stale files (deep tier didn't run today)."""
    try:
        state_dir = Path("state")
        state_dir.mkdir(exist_ok=True)
        out_path = state_dir / "deep_tier_diagnostics.json"
        payload = {
            "written_at_iso": datetime.now(timezone.utc).isoformat(),
            "run_date": date.today().isoformat(),
            "summary_counts": collector.summary_counts(),
            "statuses": statuses_to_dict_list(collector.all()),
        }
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info(
            f"Wrote deep-tier diagnostics: {out_path} "
            f"({len(collector.all())} rows, summary={payload['summary_counts']})"
        )
    except Exception as e:
        logger.warning(f"Could not write deep-tier diagnostics: {e}")


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
        "written_at_iso": datetime.now(timezone.utc).isoformat(),
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
