"""End-to-end check of the retrieval instrumentation path.

Walks the whole chain the way production does it:

    PaperStore.query_hybrid  ->  papers.metrics accumulator
                              ->  broker publish (Redis, TTL'd)
                              ->  observability render
                              ->  Prometheus text

The point is the seams.  A unit test on each piece can pass while the chain is
broken between them -- and the one seam that actually bit this project before
was the process boundary: retrieval runs in the worker, ``/metrics`` is served
by the web process, so without the publish/load hop the endpoint reports a
permanent zero.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import observability
from papers import metrics


class FakeBroker:
    """Stands in for RedisChatRunBroker: get/set only, no Redis."""

    def __init__(self):
        self.store = {}

    def publish_retrieval_metrics(self):
        return metrics.publish(self)

    def load_retrieval_metrics(self):
        return metrics.load_published(self)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class FakeManager:
    def __init__(self, broker):
        self.broker = broker


class FakeState:
    """Minimal app.state: only what observability actually reads."""

    def __init__(self, **managers):
        import time as _time

        self.api_started_at = _time.monotonic()
        self.__dict__.update(managers)


class FakeApp:
    def __init__(self, state):
        self.state = state


class RetrievalMetricsChainTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        self.broker = FakeBroker()
        self.app = FakeApp(FakeState(chat_run_manager=FakeManager(self.broker)))

    def test_metrics_survive_the_process_boundary(self):
        """The regression this whole module exists to prevent."""
        with metrics.StageTimer("rerank"):
            pass
        self.broker.publish_retrieval_metrics()

        lines = observability._retrieval_lines(self.app)
        self.assertTrue(lines, "web process rendered no retrieval series")
        joined = "\n".join(lines)
        self.assertIn('stage="rerank"', joined)
        self.assertIn('stat="count"', joined)

    def test_no_worker_report_yields_no_series(self):
        """A cold worker must not fabricate zero-latency series."""
        self.assertEqual(observability._retrieval_lines(self.app), [])

    def test_full_text_output_is_parseable(self):
        with metrics.StageTimer("semantic"):
            pass
        with metrics.StageTimer("rerank"):
            pass
        self.broker.publish_retrieval_metrics()

        text = observability.render_prometheus_metrics(self.app)
        self.assertIn("research_agent_api_up", text)
        self.assertIn("research_agent_retrieval_stage_ms", text)
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            name, _, value = line.partition(" ")
            self.assertRegex(name, r"^[a-zA-Z_][a-zA-Z0-9_]*(\{.*\})?$")
            self.assertRegex(value, r"^[-0-9.eE+]+$")

    def test_render_tolerates_unreachable_broker(self):
        """A scrape must never fail because telemetry is unavailable."""

        class Broken(FakeBroker):
            def load_retrieval_metrics(self):
                raise ConnectionError("redis down")

        app = FakeApp(FakeState(chat_run_manager=FakeManager(Broken())))
        text = observability.render_prometheus_metrics(app)
        self.assertIn("research_agent_api_up", text)
        self.assertNotIn("research_agent_retrieval_stage_ms", text)

    def test_no_user_data_reaches_the_scrape(self):
        """The privacy contract, asserted at the last point before exposure.

        Only the metric *lines* are checked, not the HELP prose -- words like
        "query" legitimately appear in descriptions such as "queue messages".
        What must never appear is user content: an actual query string, paper
        id, chunk id or title.
        """
        with metrics.StageTimer("semantic"):
            pass
        self.broker.publish_retrieval_metrics()

        text = observability.render_prometheus_metrics(self.app)
        series_lines = [
            line for line in text.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertTrue(series_lines)
        for line in series_lines:
            lowered = line.lower()
            for banned in ('"query"', "paper_id", "chunk", "title", "answer"):
                self.assertNotIn(banned, lowered)


if __name__ == "__main__":
    unittest.main()