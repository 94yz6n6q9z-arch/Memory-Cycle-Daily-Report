"""Anthropic Message Batches helper — submit / poll / retrieve with a
synchronous fallback (v3.40.0).

WHY THIS EXISTS
The daily brief makes two LLM stages that dominate cost: Layer 2 (22 Haiku
calls) and Layer 3 (1 Sonnet call). The Message Batches API bills tokens at
50% of the real-time rate, which is the single biggest cost lever for this
pipeline (see v3.40.0 release notes). Batches are asynchronous, so this helper
submits a batch, polls until it ends or a deadline passes, and hands the
caller back whatever succeeded plus the list of `custom_id`s that still need a
synchronous fallback.

DESIGN PRINCIPLES
  * The helper NEVER raises for an operational failure — submit error, poll
    error, timeout, results-retrieval error all resolve to "fall back for
    these ids". The brief must always ship.
  * The helper does NOT call the model synchronously itself. It returns the
    set of ids needing fallback; the caller owns the sync path (which is the
    pre-v3.40.0 code, unchanged, so the fallback is already battle-tested).
  * Pure-ish: all Anthropic interaction goes through the injected `client`, so
    unit tests pass a fake client and exercise every branch without network.

RATE LIMITS
The Batch API has separate, higher rate limits than the standard tokens/min
bucket. Layer 2's synchronous path throttles at 7s/call (≈2.5 min of sleep)
to respect Haiku's 50k tok/min org limit; the batch path has no such throttle,
so batching Layer 2 is both cheaper AND faster.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# User-approved 2026-06-13: 8-minute poll cap per batch. Bounds GitHub Actions
# runner minutes and falls back to synchronous quickly when a batch is slow.
DEFAULT_POLL_CAP_SECONDS = 8 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 15

# Anthropic batch processing_status terminal value.
_STATUS_ENDED = "ended"


def run_batch(
    client: Any,
    requests: list[dict],
    *,
    label: str = "batch",
    poll_cap_seconds: int = DEFAULT_POLL_CAP_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    _sleep=time.sleep,
    _monotonic=time.monotonic,
) -> tuple[dict[str, Any], list[str]]:
    """Submit `requests` as one Message Batch; poll up to the cap; return
    ``(succeeded, needs_fallback)``.

    Args:
        client: an ``anthropic.Anthropic`` client (or a compatible fake).
        requests: list of ``{"custom_id": str, "params": {...}}`` dicts, where
            ``params`` is a standard Messages create payload (model, max_tokens,
            messages, optionally system / temperature / cache_control).
        label: log prefix, e.g. ``"Layer 2"`` / ``"Layer 3"``.
        poll_cap_seconds: stop polling after this many seconds and fall back.
        poll_interval_seconds: seconds between status checks.

    Returns:
        succeeded: ``{custom_id: message}`` for requests that completed
            successfully (``message`` has the same shape as a normal
            ``messages.create`` response — ``.content[0].text`` works).
        needs_fallback: list of ``custom_id``s the caller must run
            synchronously (submit failed, timed out, errored, or missing).
    """
    all_ids = [r["custom_id"] for r in requests]
    if not requests:
        return {}, []

    # ---- submit ----
    try:
        batch = client.messages.batches.create(requests=requests)
        batch_id = batch.id
        logger.info(f"{label}: submitted batch {batch_id} ({len(requests)} request(s))")
    except Exception as e:
        logger.warning(
            f"{label}: batch submit failed ({e!r}); falling back to synchronous "
            f"for all {len(all_ids)} request(s)"
        )
        return {}, all_ids

    # ---- poll ----
    deadline = _monotonic() + poll_cap_seconds
    status: Optional[str] = None
    while _monotonic() < deadline:
        try:
            current = client.messages.batches.retrieve(batch_id)
            status = getattr(current, "processing_status", None)
        except Exception as e:
            logger.warning(f"{label}: poll error ({e!r}); retrying until cap")
            _sleep(poll_interval_seconds)
            continue
        if status == _STATUS_ENDED:
            break
        _sleep(poll_interval_seconds)

    if status != _STATUS_ENDED:
        logger.warning(
            f"{label}: batch {batch_id} not ended within {poll_cap_seconds}s "
            f"(status={status!r}); cancelling and falling back to synchronous"
        )
        try:
            client.messages.batches.cancel(batch_id)
        except Exception as e:
            logger.warning(f"{label}: batch cancel failed ({e!r}) — abandoning; "
                           f"batch bills only completed requests")
        return {}, all_ids

    # ---- retrieve results ----
    succeeded: dict[str, Any] = {}
    try:
        for entry in client.messages.batches.results(batch_id):
            cid = getattr(entry, "custom_id", None)
            result = getattr(entry, "result", None)
            rtype = getattr(result, "type", None)
            if cid is None:
                continue
            if rtype == "succeeded":
                succeeded[cid] = getattr(result, "message", None)
            else:
                logger.warning(f"{label}: request {cid} batch result type={rtype!r}; "
                               f"will fall back synchronously")
    except Exception as e:
        logger.warning(
            f"{label}: results retrieval failed ({e!r}); falling back to "
            f"synchronous for all {len(all_ids)} request(s)"
        )
        return {}, all_ids

    # Any submitted id that did not come back succeeded (errored, expired,
    # cancelled, or simply absent) goes to the synchronous fallback.
    needs_fallback = [cid for cid in all_ids if succeeded.get(cid) is None]
    if needs_fallback:
        # Drop any None placeholders so the caller's `succeeded` is clean.
        succeeded = {k: v for k, v in succeeded.items() if v is not None}
        logger.info(
            f"{label}: {len(succeeded)}/{len(all_ids)} via batch, "
            f"{len(needs_fallback)} need synchronous fallback"
        )
    else:
        logger.info(f"{label}: all {len(all_ids)} request(s) completed via batch")

    return succeeded, needs_fallback
