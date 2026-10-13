"""Retrieval instrumentation tests.

The interesting assertions are the ones about *recording behaviour*, not the
clock: a stage that raised must still be counted (otherwise averages drift
exactly when latency matters), unknown stage names must be rejected (otherwise
a typo becomes an unbounded metric label), and no query text may ever be
recorded.
"""

import json
import time
import unittest

from papers import metrics


class StageTimerTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_records_duration_on_success(self):
        with metrics.StageTimer("semantic"):
            time.sleep(0.01)
        snap = metrics.snapshot()
        self.assertEqual(snap["stages"]["semantic"]["count"], 1)
        self.assertGreater(snap["stages"]["semantic"]["total_ms"], 5)

    def test_records_duration_when_stage_raises(self):
        """A failed stage is a slow stage; dropping it would bias the mean down."""
        with self.assertRaises(RuntimeError):
            with metrics.StageTimer("rerank"):
                raise RuntimeError("model missing")
        snap = metrics.snapshot()
        self.assertEqual(snap["stages"]["rerank"]["count"], 1)
        self.assertEqual(snap["stages"]["rerank"]["errors"], 1)

    def test_exception_is_not_swallowed(self):
        """__exit__ returns False, so the caller still sees the failure."""
        try:
            with metrics.StageTimer("lexical"):
                raise ValueError("boom")
        except ValueError:
            pass
        else:
            self.fail("StageTimer swallowed the exception")

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(ValueError):
            metrics.StageTimer("semanticc")

    def test_query_counter_follows_semantic_stage(self):
        for _ in range(3):
            with metrics.StageTimer("semantic"):
                pass
        self.assertEqual(metrics.snapshot()["queries"], 3)

    def test_stages_are_independent(self):
        with metrics.StageTimer("semantic"):
            pass
        with metrics.StageTimer("lexical"):
            pass
        snap = metrics.snapshot()
        self.assertEqual(snap["stages"]["semantic"]["count"], 1)
        self.assertEqual(snap["stages"]["lexical"]["count"], 1)
        self.assertEqual(snap["stages"]["rerank"]["count"], 0)

    def test_reset_clears_everything(self):
        with metrics.StageTimer("fusion"):
            pass
        metrics.reset()
        snap = metrics.snapshot()
        self.assertEqual(snap["queries"], 0)
        for stage in metrics.STAGES:
            self.assertEqual(snap["stages"][stage]["count"], 0)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_snapshot_contains_no_free_text(self):
        """Privacy contract: stage names and numbers only."""
        with metrics.StageTimer("semantic"):
            pass
        blob = json.dumps(metrics.snapshot())
        for banned in ("query", "attention", "transformer", "paper_id", "title"):
            self.assertNotIn(banned, blob)

    def test_mean_is_zero_without_samples(self):
        self.assertEqual(metrics.mean_ms("rerank"), 0.0)

    def test_mean_is_total_over_count(self):
        """Mean is total over count, checked without depending on the clock.

        An earlier version slept 20ms twice and compared with ``places=3``,
        which demands an error below 0.0005 -- the size of the function's own
        3-decimal rounding. It therefore failed or passed depending on where the
        wall-clock value happened to land. Deterministic inputs remove both the
        flakiness and the need to choose a tolerance.
        """
        # StageTimer reads perf_counter twice per use (__enter__ and __exit__),
        # so each iteration consumes a pair: 0->10ms, 20->30ms, 40->50ms.
        readings = iter((0.0, 0.010, 0.020, 0.030, 0.040, 0.050))
        original = time.perf_counter
        try:
            time.perf_counter = lambda: next(readings)
            for _ in range(3):
                with metrics.StageTimer("semantic"):
                    pass
        finally:
            time.perf_counter = original

        snap = metrics.snapshot()
        stage = snap["stages"]["semantic"]
        self.assertEqual(stage["count"], 3)
        self.assertAlmostEqual(stage["total_ms"], 30.0, delta=0.01)
        self.assertAlmostEqual(metrics.mean_ms("semantic"), 10.0, delta=0.001)


class FakeRedis:
    """Minimal in-memory stand-in exposing only get/set."""

    def __init__(self, *, raises=False):
        self.store = {}
        self.raises = raises

    def set(self, key, value, ex=None):
        if self.raises:
            raise ConnectionError("redis down")
        self.store[key] = value

    def get(self, key):
        if self.raises:
            raise ConnectionError("redis down")
        return self.store.get(key)


class TransportTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        with metrics.StageTimer("rerank"):
            pass

    def test_publish_round_trip(self):
        client = FakeRedis()
        self.assertTrue(metrics.publish(client))
        loaded = metrics.load_published(client)
        self.assertEqual(loaded["stages"]["rerank"]["count"], 1)

    def test_publish_survives_redis_outage(self):
        """Instrumentation must never break the query that produced it."""
        self.assertFalse(metrics.publish(FakeRedis(raises=True)))
        self.assertIsNone(metrics.load_published(FakeRedis(raises=True)))

    def test_publish_with_none_client_is_a_no_op(self):
        self.assertFalse(metrics.publish(None))
        self.assertIsNone(metrics.load_published(None))

    def test_load_tolerates_corrupt_payload(self):
        client = FakeRedis()
        client.store[metrics._METRICS_KEY] = b"not json"
        self.assertIsNone(metrics.load_published(client))

    def test_load_returns_none_for_missing_key(self):
        self.assertIsNone(metrics.load_published(FakeRedis()))


class RenderTest(unittest.TestCase):
    def test_empty_payload_renders_nothing(self):
        self.assertEqual(metrics.render_prometheus(None), [])
        self.assertEqual(metrics.render_prometheus({}), [])

    def test_render_includes_each_recorded_stage(self):
        client = FakeRedis()
        with metrics.StageTimer("rerank"):
            time.sleep(0.005)
        metrics.publish(client)
        lines = metrics.render_prometheus(metrics.load_published(client))
        joined = "\n".join(lines)
        self.assertIn("research_agent_retrieval_stage_ms", joined)
        self.assertIn('stage="rerank"', joined)
        self.assertIn('stat="mean"', joined)

    def test_stage_with_zero_samples_is_omitted(self):
        payload = {"queries": 0, "stages": {
            "semantic": {"total_ms": 0.0, "count": 0, "errors": 0},
        }}
        lines = metrics.render_prometheus(payload)
        self.assertEqual(lines, [])

    def test_render_is_text_exposition_compatible(self):
        """Every line is either a comment, a gauge, or a name+value."""
        client = FakeRedis()
        with metrics.StageTimer("semantic"):
            time.sleep(0.005)
        metrics.publish(client)
        for line in metrics.render_prometheus(metrics.load_published(client)):
            if line.startswith("#"):
                continue
            self.assertRegex(line, r"^[a-zA-Z_][a-zA-Z0-9_]*(\{.*\})? [-0-9.]+$")


if __name__ == "__main__":
    unittest.main()