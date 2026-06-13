"""v3.40.2 — regression guard for the Layer 3 Sonnet double-billing bug.

The v3.40.1 batch path recorded its diagnostic with a signature that does not
exist (collector.record(name, type, status, detail=, items_returned=)). The
resulting TypeError was caught by the batch except, which nulled the response
and triggered a full-price synchronous re-run on top of the already-billed
batch call (~2x Sonnet cost per run).

These tests assert (a) the SourceFetchStatus the batch path now builds is
valid and records cleanly, and (b) the source file no longer contains the
broken positional-record call inside the Layer 3 batch path.
"""

import re
import unittest

from src.diagnostics import DiagnosticsCollector, SourceFetchStatus


class TestBatchDiagnosticRecordIsValid(unittest.TestCase):
    def test_sourcefetchstatus_constructs_and_records(self):
        # Exactly the construction the v3.40.2 Layer 3 batch path performs.
        collector = DiagnosticsCollector()
        status = SourceFetchStatus(
            source_name="Anthropic API / claude-sonnet-4-6 (stage 2 analysis, BATCH)",
            source_type="llm",
            reached=True,
            items_returned=1,
            latency_ms=0,
            detail="prompt: 60000 chars, max_tokens=28000, batched (50% cost)",
        )
        # record() must accept the object and the row must classify as healthy.
        collector.record(status)
        self.assertEqual(len(collector.statuses), 1)
        self.assertEqual(collector.statuses[0].health, "ok")

    def test_old_broken_record_signature_would_raise(self):
        # Documents WHY the bug happened: the old positional+kwarg call is not a
        # valid record() signature. This is the exact call that nulled the
        # response and forced the redundant sync re-run.
        collector = DiagnosticsCollector()
        with self.assertRaises(TypeError):
            collector.record(  # type: ignore[call-arg]
                "Anthropic API / sonnet (stage 2 analysis, BATCH)",
                "llm", "ok",
                detail="x",
                items_returned=1,
            )


class TestSourceLevelGuard(unittest.TestCase):
    def test_layer3_batch_block_has_no_broken_record(self):
        src = open("src/analysis/llm_summarizer.py", encoding="utf-8").read()
        # The broken pattern was: collector.record( "...BATCH...", "llm", "ok",
        # i.e. a string literal first arg + a bare "ok" status arg. The correct
        # form passes a SourceFetchStatus(...) object. Assert the broken shape
        # is gone.
        broken = re.search(
            r'collector\.record\(\s*\n?\s*f?"Anthropic API[^\n]*BATCH[^\n]*"\s*,\s*\n?\s*"llm"\s*,\s*"ok"',
            src,
        )
        self.assertIsNone(broken, "broken positional collector.record() call still present")
        # And the correct form (record a SourceFetchStatus) is present.
        self.assertIn("collector.record(SourceFetchStatus(", src)

    def test_diagnostic_record_is_isolated_from_batch_response(self):
        # The fix must keep the diagnostic in its own try guarded on
        # `response is not None`, so a record failure cannot null a good batch
        # response. Assert that guard + the non-fatal note exist.
        src = open("src/analysis/llm_summarizer.py", encoding="utf-8").read()
        self.assertIn("if response is not None and collector is not None:", src)
        self.assertIn("batch diagnostic record failed (non-fatal)", src)


if __name__ == "__main__":
    unittest.main()
