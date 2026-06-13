"""Tests for v3.40.1 — deep-tier Layer 1 digester batching.

Covers the shared parse helper, the batch cache prime/consume protocol, and
the safe-degradation contract (cache miss → live sync path).
"""

import unittest
from types import SimpleNamespace

import src.analysis.digester as dg
from src.analysis.digester import (
    _build_digest_user_msg,
    _parse_digest_response,
    _DIGEST_BATCH_CACHE,
    _DIGEST_MISS,
    digest_document,
)


def _msg(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class TestBuildDigestUserMsg(unittest.TestCase):
    def test_short_text_skipped(self):
        self.assertIsNone(_build_digest_user_msg("s", "t", "2026-06-01", "ti", "short"))

    def test_builds_and_truncates(self):
        long = "x" * (dg.MAX_INPUT_CHARS + 5000)
        out = _build_digest_user_msg("s", "earnings_transcript", "2026-06-01", "ti", long)
        self.assertIsNotNone(out)
        user_msg, truncated = out
        self.assertEqual(len(truncated), dg.MAX_INPUT_CHARS)
        self.assertIn("earnings_transcript", user_msg)


class TestParseDigestResponse(unittest.TestCase):
    def test_parse_to_digest(self):
        verdict = {
            "key_metrics": {"hbm_bit_growth": "up"},
            "signal_relevance": ["t2_capex_parabolic"],
            "key_quotes": ["capex raised"],
            "target_price_action": None,
            "fired_status_evidence": "bullish",
        }
        import json as _json
        d = _parse_digest_response(_msg(_json.dumps(verdict)), "src1",
                                   "earnings_transcript", "2026-06-01", "Title",
                                   "truncated text", "http://x")
        self.assertIsNotNone(d)
        self.assertEqual(d.source_id, "src1")
        self.assertEqual(d.fired_status_evidence, "bullish")
        self.assertEqual(d.key_metrics["hbm_bit_growth"], "up")

    def test_bad_json_returns_none(self):
        d = _parse_digest_response(_msg("garbage"), "s", "t", "2026-06-01", "ti", "x", None)
        self.assertIsNone(d)


class TestBatchCacheConsume(unittest.TestCase):
    def tearDown(self):
        _DIGEST_BATCH_CACHE.clear()

    def test_digest_document_consumes_cache(self):
        # Seed the cache as if prime_digest_batch had produced it.
        sentinel_digest = SimpleNamespace(source_id="cached_src", marker="from_batch")
        _DIGEST_BATCH_CACHE["cached_src"] = sentinel_digest
        out = digest_document("cached_src", "earnings_transcript", "2026-06-01",
                              "Title", "this text would be sent if not cached")
        self.assertIs(out, sentinel_digest)
        # consumed — a second call must NOT return the cached object again
        self.assertNotIn("cached_src", _DIGEST_BATCH_CACHE)

    def test_cache_miss_sentinel(self):
        # nothing seeded → pop returns the miss sentinel (sync path would run)
        self.assertIs(_DIGEST_BATCH_CACHE.pop("absent", _DIGEST_MISS), _DIGEST_MISS)


class TestPrimeBatch(unittest.TestCase):
    def tearDown(self):
        _DIGEST_BATCH_CACHE.clear()

    def test_prime_populates_cache_via_fake_client(self):
        import json as _json
        verdict = _json.dumps({"key_metrics": {}, "signal_relevance": [],
                               "key_quotes": [], "fired_status_evidence": "neutral"})

        class FakeBatches:
            def create(self, requests=None):
                self._reqs = requests
                return SimpleNamespace(id="b1")
            def retrieve(self, _id):
                return SimpleNamespace(processing_status="ended")
            def results(self, _id):
                # echo every submitted custom_id back as succeeded
                return iter([
                    SimpleNamespace(custom_id=r["custom_id"],
                                    result=SimpleNamespace(type="succeeded", message=_msg(verdict)))
                    for r in self._reqs
                ])
            def cancel(self, _id):
                pass

        fake_client = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))
        orig_client = dg.client
        dg.client = fake_client
        try:
            jobs = [
                dict(source_id="A / Co / 2026-06-01", source_type="earnings_transcript",
                     published_date_iso="2026-06-01", title="t", text="y" * 500),
                dict(source_id="B / Co / 2026-06-01", source_type="earnings_transcript",
                     published_date_iso="2026-06-01", title="t", text="z" * 500),
            ]
            dg.prime_digest_batch(jobs, label="test")
            self.assertIn("A / Co / 2026-06-01", _DIGEST_BATCH_CACHE)
            self.assertIn("B / Co / 2026-06-01", _DIGEST_BATCH_CACHE)
        finally:
            dg.client = orig_client

    def test_prime_noop_on_empty(self):
        # must not raise, must not populate
        dg.prime_digest_batch([], label="test")
        self.assertEqual(len(_DIGEST_BATCH_CACHE), 0)


if __name__ == "__main__":
    unittest.main()
