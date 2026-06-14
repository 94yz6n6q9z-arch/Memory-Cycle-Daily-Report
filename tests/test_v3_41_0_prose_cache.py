"""v3.41.0 — tests for the Layer 3 indicator-prose cache logic."""

import unittest
from datetime import date, timedelta
from types import SimpleNamespace

import src.analysis.prose_cache as pc


def fake_reasoning(signal_id, fired_status="Not fired", direction="neutral",
                   convergence="low", confidence="low", evidence_count=0,
                   evidence_sources=None, fse=None, kq=None, change=""):
    return SimpleNamespace(
        signal_id=signal_id, fired_status=fired_status, consensus_direction=direction,
        convergence=convergence, confidence=confidence, evidence_count=evidence_count,
        evidence_sources=evidence_sources or [], fired_status_evidence=fse or [],
        key_quotes_consolidated=kq or [], evidence_for_change_vs_yesterday=change,
    )


def fake_assessment(id, status="Not fired", where="where text", latest="",
                    links=None, change=None):
    return SimpleNamespace(id=id, current_status=status, where_we_stand=where,
                           latest_developments=latest, relevant_links=links or [],
                           change_vs_prior=change)


class TestFingerprint(unittest.TestCase):
    def test_stable_for_same_verdict_shape(self):
        a = fake_reasoning("t1_x", evidence_count=2, evidence_sources=["A", "B"])
        b = fake_reasoning("t1_x", evidence_count=2, evidence_sources=["B", "A"])  # order
        self.assertEqual(pc.compute_fingerprint(a), pc.compute_fingerprint(b))

    def test_ignores_free_text_jitter(self):
        a = fake_reasoning("t1_x", fse=["old wording"], kq=["q1"], change="")
        b = fake_reasoning("t1_x", fse=["totally different wording"], kq=["q2", "q3"],
                           change="some change text")
        self.assertEqual(pc.compute_fingerprint(a), pc.compute_fingerprint(b))

    def test_flips_on_status_change(self):
        a = fake_reasoning("t1_x", fired_status="Not fired")
        b = fake_reasoning("t1_x", fired_status="Fired")
        self.assertNotEqual(pc.compute_fingerprint(a), pc.compute_fingerprint(b))

    def test_direction_and_convergence_do_NOT_flip(self):
        # Option B (v3.41.3): direction/convergence were jittering run-to-run and
        # starving the reuse rate (18/24 churned on a quiet day). They are no
        # longer in the fingerprint — only fired_status drives reuse. The
        # consistency guard + weekly refresh catch any prose drift.
        base = fake_reasoning("t1_x", direction="neutral", convergence="low")
        diff_dir = fake_reasoning("t1_x", direction="bearish", convergence="low")
        diff_conv = fake_reasoning("t1_x", direction="neutral", convergence="high")
        self.assertEqual(pc.compute_fingerprint(base), pc.compute_fingerprint(diff_dir))
        self.assertEqual(pc.compute_fingerprint(base), pc.compute_fingerprint(diff_conv))

    def test_evidence_and_confidence_changes_do_NOT_flip(self):
        # Option A (v3.41.1): the daily news churn (evidence count/sources) and
        # confidence jitter must NOT trigger a regeneration — that was the
        # 22/24-cards-churning bug. Verdict shape identical → same fingerprint.
        a = fake_reasoning("t1_x", evidence_count=1, evidence_sources=["A"], confidence="low")
        b = fake_reasoning("t1_x", evidence_count=5, evidence_sources=["A", "B", "C"],
                           confidence="high")
        self.assertEqual(pc.compute_fingerprint(a), pc.compute_fingerprint(b))


class TestSeedStagger(unittest.TestCase):
    def test_spreads_across_week_and_idempotent(self):
        today = date(2026, 6, 13)
        ids = [f"t{i}" for i in range(14)]
        cache = {}
        pc.seed_staggered_dates(cache, ids, today=today)
        offsets = {(today - date.fromisoformat(cache[i]["last_full_regen_date"])).days
                   for i in ids}
        self.assertEqual(offsets, set(range(7)))  # spread over 0..6 days
        # idempotent: a second seed must not move existing dates
        snapshot = {i: cache[i]["last_full_regen_date"] for i in ids}
        pc.seed_staggered_dates(cache, ids, today=today)
        self.assertEqual({i: cache[i]["last_full_regen_date"] for i in ids}, snapshot)


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 13)

    def test_no_cache_regen(self):
        d = pc.evaluate_cache({}, [fake_reasoning("t1_x")], today=self.today)
        self.assertEqual(d["t1_x"]["action"], "regen")
        self.assertEqual(d["t1_x"]["reason"], "no_cache")

    def test_dormant_reuse(self):
        r = fake_reasoning("t1_x")
        cache = {"t1_x": {"where_we_stand": "cached", "fingerprint": pc.compute_fingerprint(r),
                          "status": "Not fired",
                          "last_full_regen_date": (self.today - timedelta(days=2)).isoformat()}}
        d = pc.evaluate_cache(cache, [r], today=self.today)
        self.assertEqual(d["t1_x"]["action"], "reuse")

    def test_fingerprint_changed_regen(self):
        r_old = fake_reasoning("t1_x", fired_status="Not fired")
        r_new = fake_reasoning("t1_x", fired_status="Fired")
        cache = {"t1_x": {"where_we_stand": "cached", "fingerprint": pc.compute_fingerprint(r_old),
                          "status": "Not fired",
                          "last_full_regen_date": (self.today - timedelta(days=1)).isoformat()}}
        d = pc.evaluate_cache(cache, [r_new], today=self.today)
        self.assertEqual(d["t1_x"]["action"], "regen")
        self.assertEqual(d["t1_x"]["reason"], "fingerprint_changed")

    def test_weekly_refresh_due_regen(self):
        r = fake_reasoning("t1_x")
        cache = {"t1_x": {"where_we_stand": "cached", "fingerprint": pc.compute_fingerprint(r),
                          "status": "Not fired",
                          "last_full_regen_date": (self.today - timedelta(days=7)).isoformat()}}
        d = pc.evaluate_cache(cache, [r], today=self.today)
        self.assertEqual(d["t1_x"]["action"], "regen")
        self.assertIn("weekly_refresh_due", d["t1_x"]["reason"])


class TestStatusGuard(unittest.TestCase):
    def test_status_mismatch_forces_regen(self):
        decisions = {"t1_x": {"action": "reuse", "reason": "dormant(age=1)"}}
        cache = {"t1_x": {"status": "Not fired"}}
        out = pc.apply_status_guard(decisions, cache, {"t1_x": "Fired"})
        self.assertEqual(out["t1_x"]["action"], "regen")
        self.assertIn("status_mismatch", out["t1_x"]["reason"])

    def test_status_match_stays_reuse(self):
        decisions = {"t1_x": {"action": "reuse", "reason": "dormant(age=1)"}}
        cache = {"t1_x": {"status": "Fired"}}
        out = pc.apply_status_guard(decisions, cache, {"t1_x": "Fired"})
        self.assertEqual(out["t1_x"]["action"], "reuse")


class TestUpdate(unittest.TestCase):
    def test_stores_prose_for_regen_only_and_bumps_clock(self):
        today = date(2026, 6, 13)
        cache = {
            "t_reuse": {"where_we_stand": "old", "fingerprint": "f", "status": "Not fired",
                        "last_full_regen_date": (today - timedelta(days=3)).isoformat()},
            "t_regen": {"where_we_stand": "old", "fingerprint": "f", "status": "Not fired",
                        "last_full_regen_date": (today - timedelta(days=8)).isoformat()},
        }
        reasonings = [fake_reasoning("t_reuse", fired_status="Not fired"),
                      fake_reasoning("t_regen", fired_status="Fired")]
        assessments = [fake_assessment("t_reuse", where="freshA"),
                       fake_assessment("t_regen", status="Fired", where="freshB")]
        pc.update_cache_after_synthesis(cache, assessments, reasonings,
                                        regenerated_ids={"t_regen"}, today=today)
        # only the regenerated card is rewritten; reused card untouched
        self.assertEqual(cache["t_regen"]["where_we_stand"], "freshB")
        self.assertEqual(cache["t_reuse"]["where_we_stand"], "old")
        self.assertEqual(cache["t_regen"]["last_full_regen_date"], today.isoformat())
        self.assertEqual(cache["t_reuse"]["last_full_regen_date"],
                         (today - timedelta(days=3)).isoformat())

    def test_no_cache_regen_preserves_seeded_date(self):
        # First population: card has a SEEDED staggered date but no prose.
        today = date(2026, 6, 13)
        seeded = (today - timedelta(days=4)).isoformat()
        cache = {"t_new": {"last_full_regen_date": seeded}}
        decisions = {"t_new": {"action": "regen", "reason": "no_cache"}}
        regen, reset = pc.split_regen_ids(decisions)
        self.assertEqual(regen, {"t_new"})
        self.assertEqual(reset, set())  # no_cache excluded from clock reset
        pc.update_cache_after_synthesis(
            cache, [fake_assessment("t_new", where="fresh")],
            [fake_reasoning("t_new")],
            regenerated_ids=regen, clock_reset_ids=reset, today=today)
        self.assertEqual(cache["t_new"]["where_we_stand"], "fresh")
        # seeded staggered date preserved (NOT snapped to today)
        self.assertEqual(cache["t_new"]["last_full_regen_date"], seeded)

    def test_split_regen_ids_resets_real_rewrites(self):
        decisions = {
            "a": {"action": "reuse", "reason": "dormant(age=1)"},
            "b": {"action": "regen", "reason": "no_cache"},
            "c": {"action": "regen", "reason": "fingerprint_changed"},
            "d": {"action": "regen", "reason": "weekly_refresh_due(age=7)"},
        }
        regen, reset = pc.split_regen_ids(decisions)
        self.assertEqual(regen, {"b", "c", "d"})
        self.assertEqual(reset, {"c", "d"})  # b (no_cache) excluded

    def test_roundtrip_load_save(self):
        import tempfile, os
        cache = {"t1": {"where_we_stand": "x", "fingerprint": "abc", "status": "Fired",
                        "last_full_regen_date": "2026-06-10"}}
        with tempfile.TemporaryDirectory() as d:
            orig = pc.PROSE_CACHE_FILE
            pc.PROSE_CACHE_FILE = __import__("pathlib").Path(d) / "cache.json"
            try:
                pc.save_prose_cache(cache)
                loaded = pc.load_prose_cache()
                self.assertEqual(loaded["t1"]["where_we_stand"], "x")
                self.assertEqual(loaded["t1"]["fingerprint"], "abc")
            finally:
                pc.PROSE_CACHE_FILE = orig


if __name__ == "__main__":
    unittest.main()
