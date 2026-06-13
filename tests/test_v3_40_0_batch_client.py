"""Tests for src/analysis/batch_client.py (v3.40.0).

Exercises every operational branch of run_batch with a fake Anthropic client:
full success, partial success, submit failure, poll timeout, and
results-retrieval failure. No network.
"""

import unittest
from types import SimpleNamespace

from src.analysis.batch_client import run_batch


def _msg(text):
    """Fake a messages.create-shaped response."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _result(custom_id, rtype="succeeded", text="{}"):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type=rtype, message=_msg(text) if rtype == "succeeded" else None),
    )


class FakeBatches:
    def __init__(self, *, submit_exc=None, statuses=None, results=None,
                 retrieve_exc=None, results_exc=None):
        self.submit_exc = submit_exc
        # statuses: list consumed one-per-retrieve; last value repeats.
        self._statuses = list(statuses or ["ended"])
        self._results = results or []
        self.retrieve_exc = retrieve_exc
        self.results_exc = results_exc
        self.cancelled = False
        self.created = False

    def create(self, requests=None):
        self.created = True
        if self.submit_exc:
            raise self.submit_exc
        self._requests = requests
        return SimpleNamespace(id="batch_test_123")

    def retrieve(self, batch_id):
        if self.retrieve_exc:
            raise self.retrieve_exc
        s = self._statuses[0] if len(self._statuses) == 1 else self._statuses.pop(0)
        return SimpleNamespace(processing_status=s)

    def results(self, batch_id):
        if self.results_exc:
            raise self.results_exc
        return iter(self._results)

    def cancel(self, batch_id):
        self.cancelled = True


class FakeClient:
    def __init__(self, batches):
        self.messages = SimpleNamespace(batches=batches)


def _reqs(*ids):
    return [{"custom_id": i, "params": {"model": "m", "max_tokens": 10,
                                        "messages": [{"role": "user", "content": "x"}]}}
            for i in ids]


# no-op sleep/clock so tests don't actually wait
_NOSLEEP = lambda s: None


class TestRunBatch(unittest.TestCase):

    def test_full_success(self):
        fc = FakeClient(FakeBatches(
            statuses=["ended"],
            results=[_result("a", text='{"v":1}'), _result("b", text='{"v":2}')],
        ))
        succeeded, fallback = run_batch(fc, _reqs("a", "b"), label="L2",
                                        _sleep=_NOSLEEP)
        self.assertEqual(set(succeeded), {"a", "b"})
        self.assertEqual(fallback, [])
        self.assertEqual(succeeded["a"].content[0].text, '{"v":1}')

    def test_partial_success_one_errored(self):
        fc = FakeClient(FakeBatches(
            statuses=["ended"],
            results=[_result("a"), _result("b", rtype="errored")],
        ))
        succeeded, fallback = run_batch(fc, _reqs("a", "b"), label="L2",
                                        _sleep=_NOSLEEP)
        self.assertEqual(set(succeeded), {"a"})
        self.assertEqual(fallback, ["b"])

    def test_missing_result_goes_to_fallback(self):
        # 'b' simply absent from results
        fc = FakeClient(FakeBatches(statuses=["ended"], results=[_result("a")]))
        succeeded, fallback = run_batch(fc, _reqs("a", "b"), label="L2",
                                        _sleep=_NOSLEEP)
        self.assertEqual(set(succeeded), {"a"})
        self.assertEqual(fallback, ["b"])

    def test_submit_failure_falls_back_all(self):
        fc = FakeClient(FakeBatches(submit_exc=RuntimeError("503")))
        succeeded, fallback = run_batch(fc, _reqs("a", "b"), label="L3",
                                        _sleep=_NOSLEEP)
        self.assertEqual(succeeded, {})
        self.assertEqual(fallback, ["a", "b"])

    def test_timeout_cancels_and_falls_back(self):
        # never reports 'ended'; clock advances past the cap quickly
        clock = {"t": 0.0}
        def fake_monotonic():
            clock["t"] += 100.0  # each call jumps 100s
            return clock["t"]
        fb = FakeBatches(statuses=["in_progress"])
        fc = FakeClient(fb)
        succeeded, fallback = run_batch(fc, _reqs("a"), label="L2",
                                        poll_cap_seconds=120,
                                        _sleep=_NOSLEEP, _monotonic=fake_monotonic)
        self.assertEqual(succeeded, {})
        self.assertEqual(fallback, ["a"])
        self.assertTrue(fb.cancelled, "a timed-out batch must be cancelled")

    def test_results_retrieval_failure_falls_back(self):
        fc = FakeClient(FakeBatches(statuses=["ended"],
                                    results_exc=RuntimeError("network")))
        succeeded, fallback = run_batch(fc, _reqs("a", "b"), label="L2",
                                        _sleep=_NOSLEEP)
        self.assertEqual(succeeded, {})
        self.assertEqual(fallback, ["a", "b"])

    def test_poll_error_then_success(self):
        # first retrieve raises, but we keep polling until cap; here it never
        # recovers (retrieve_exc persists) so it should time out + fall back.
        clock = {"t": 0.0}
        def fake_monotonic():
            clock["t"] += 100.0
            return clock["t"]
        fc = FakeClient(FakeBatches(retrieve_exc=RuntimeError("blip")))
        succeeded, fallback = run_batch(fc, _reqs("a"), label="L2",
                                        poll_cap_seconds=120,
                                        _sleep=_NOSLEEP, _monotonic=fake_monotonic)
        self.assertEqual(fallback, ["a"])

    def test_empty_requests_noop(self):
        fc = FakeClient(FakeBatches())
        succeeded, fallback = run_batch(fc, [], label="L2", _sleep=_NOSLEEP)
        self.assertEqual(succeeded, {})
        self.assertEqual(fallback, [])
        self.assertFalse(fc.messages.batches.created)


if __name__ == "__main__":
    unittest.main()
