"""Run offline, or set E2E_REDIS_URL to exercise the same cases on Redis 7."""
from __future__ import annotations

import itertools
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from api.redis_runs import (
    QueuedChatRun, QueueUnavailableError, RedisChatRunBroker, RedisChatRunManager,
    RedisChatRunWorker, RedisResearchRunBroker, RedisResearchRunManager, RedisResearchRunWorker,
)
from api.run_dispatcher import RunDispatcher, worker_process_lock
from session_store import SessionStore


class _Broker:
    sequence = itertools.count(1)

    def __init__(self):
        self.items = {}
        self.pending = {}
        self.cancelled = set()
        self.lock = threading.RLock()

    def enqueue(self, run_id, session_id, message):
        with self.lock:
            ident = f"{next(self.sequence)}-0"
            self.items[ident] = QueuedChatRun(ident, run_id, session_id, message)
            return ident

    def reserve(self, consumer, **kwargs):
        with self.lock:
            for ident, job in self.items.items():
                if ident not in self.pending:
                    self.pending[ident] = (consumer, time.monotonic())
                    return job

    def claim_stale(self, consumer, *, min_idle_ms):
        with self.lock:
            for ident, (_, updated) in self.pending.items():
                if (time.monotonic() - updated) * 1000 >= min_idle_ms:
                    self.pending[ident] = (consumer, time.monotonic())
                    return self.items[ident]

    def renew(self, consumer, message_ids):
        with self.lock:
            owned = {ident for ident in message_ids if self.pending.get(ident, (None,))[0] == consumer}
            for ident in owned:
                self.pending[ident] = (consumer, time.monotonic())
            return owned

    def acknowledge_owned(self, consumer, ident):
        with self.lock:
            if self.pending.get(ident, (None,))[0] != consumer:
                return False
            self.pending.pop(ident)
            self.items.pop(ident)
            return True

    def queue_depth(self):
        with self.lock:
            return len(self.items)

    def request_cancel(self, run_id):
        self.cancelled.add(run_id)

    def cancel_requested(self, run_id):
        return run_id in self.cancelled

    def clear_cancel(self, run_id):
        self.cancelled.discard(run_id)

    def publish(self, *args):
        pass

    def heartbeat(self, *args):
        pass

    def publish_retrieval_metrics(self):
        pass


class _Agent:
    model = "test"

    def __init__(self, store):
        self.sessions = store
        self.started = {name: threading.Event() for name in ("A1", "A2", "A3", "B", "R")}
        self.release = {name: threading.Event() for name in self.started}
        self.calls = []
        self.traces = {}

    def step(self, message, *, session_id, cancel_event, on_token, **kwargs):
        self.calls.append(message)
        self.started[message].set()
        on_token(message)
        deadline = time.monotonic() + 12
        while not self.release[message].wait(0.02):
            if cancel_event.is_set():
                self.traces[session_id] = {"outcome": "cancelled"}
                return "cancelled"
            if time.monotonic() > deadline:
                raise TimeoutError("test did not release task")
        self.traces[session_id] = {"outcome": "success"}
        return f"answer:{message}"

    def get_last_trace(self, session_id):
        return self.traces.get(session_id, {})

    def research(self, query, *, session_id, run_id, cancel_event, **kwargs):
        answer = self.step(query, session_id=session_id, cancel_event=cancel_event, on_token=lambda _: None)
        self.sessions.update_research_run(run_id, status="completed", final_answer=answer)


class TestRunDispatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(str(Path(self.tmp.name) / "state.db"))
        self.agent = _Agent(self.store)
        self.broker = self.make_broker()
        self.worker = RedisChatRunWorker(self.agent, self.broker, consumer="test", claim_idle_ms=1000)
        self.manager = RedisChatRunManager(self.agent, self.broker)
        self.a = self.store.create("A")["thread_id"]
        self.b = self.store.create("B")["thread_id"]
        self.stop = threading.Event()
        self.thread = None
        self.project_stores = []

    def make_broker(self):
        return _Broker()

    def tearDown(self):
        self.stop.set()
        for release in self.agent.release.values():
            release.set()
        if self.thread:
            self.thread.join(timeout=8)
            self.assertFalse(self.thread.is_alive(), "dispatcher did not drain")
        for projects in self.project_stores:
            projects.close()
        self.store.close()
        self.tmp.cleanup()

    def start(self, limit=2, workers=None):
        self.dispatcher = RunDispatcher(workers or {"chat": (self.worker, limit)}, poll_seconds=0.01)
        self.thread = threading.Thread(target=self.dispatcher.run_forever, args=(self.stop,))
        self.thread.start()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true")

    def test_different_sessions_overlap_but_same_session_keeps_order(self):
        self.manager.start(self.a, "A1")
        self.manager.start(self.a, "A2")
        self.manager.start(self.a, "A3")
        self.manager.start(self.b, "B")
        self.start()
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.assertTrue(self.agent.started["B"].wait(3))
        self.assertFalse(self.agent.started["A2"].is_set())
        self.agent.release["A1"].set()
        self.assertTrue(self.agent.started["A2"].wait(3))
        self.assertFalse(self.agent.started["A3"].is_set())
        self.agent.release["A2"].set()
        self.assertTrue(self.agent.started["A3"].wait(3))

    def test_capacity_one_queues_other_session(self):
        self.manager.start(self.a, "A1")
        self.manager.start(self.b, "B")
        self.start(limit=1)
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.assertFalse(self.agent.started["B"].wait(0.3))
        self.agent.release["A1"].set()
        self.assertTrue(self.agent.started["B"].wait(3))

    def test_cancel_a_does_not_cancel_b(self):
        a = self.manager.start(self.a, "A1")
        b = self.manager.start(self.b, "B")
        self.start()
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.assertTrue(self.agent.started["B"].wait(3))
        self.manager.cancel(a["run_id"])
        self.wait_for(lambda: self.manager.get(a["run_id"])["status"] == "cancelled")
        self.assertEqual(self.manager.get(b["run_id"])["status"], "running")
        self.agent.release["B"].set()
        self.wait_for(lambda: self.manager.get(b["run_id"])["status"] == "completed")

    def test_cancel_queued_turn_does_not_wait_for_full_executor(self):
        self.manager.start(self.a, "A1")
        queued = self.manager.start(self.a, "A2")
        self.start(limit=1)
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.manager.cancel(queued["run_id"])
        self.wait_for(lambda: self.manager.get(queued["run_id"])["status"] == "cancelled")
        self.assertFalse(self.agent.started["A2"].is_set())

    def test_intake_buffer_is_bounded(self):
        for _ in range(12):
            self.manager.start(self.a, "A1")
        self.dispatcher = RunDispatcher({"chat": (self.worker, 2)}, pending_per_kind=3)
        self.thread = threading.Thread(target=self.dispatcher.run_forever, args=(self.stop,))
        self.thread.start()
        self.assertTrue(self.agent.started["A1"].wait(3))
        time.sleep(0.2)
        with self.dispatcher._lock:
            self.assertLessEqual(len(self.dispatcher._entries), 5)
        self.assertEqual(self.broker.queue_depth(), 12)

    def test_long_active_and_waiting_runs_are_renewed(self):
        self.manager.start(self.a, "A1")
        self.manager.start(self.a, "A2")
        self.start()
        self.assertTrue(self.agent.started["A1"].wait(3))
        time.sleep(2.2)  # More than twice the configured recovery threshold.
        self.assertIsNone(self.broker.claim_stale("outsider", min_idle_ms=1000))
        self.assertEqual(self.agent.calls, ["A1"])

    def test_duplicate_delivery_does_not_repeat_agent_call(self):
        run = self.manager.start(self.a, "A1")
        self.broker.enqueue(run["run_id"], self.a, "A1")
        self.agent.release["A1"].set()
        self.start()
        self.wait_for(lambda: self.broker.queue_depth() == 0)
        self.assertEqual(self.agent.calls, ["A1"])

    def test_recover_dead_consumer(self):
        self.manager.start(self.a, "A1")
        self.assertIsNotNone(self.broker.reserve("dead", block_ms=1))
        time.sleep(1.1)
        self.agent.release["A1"].set()
        self.start()
        self.wait_for(lambda: self.broker.queue_depth() == 0)
        self.assertEqual(self.agent.calls, ["A1"])

    def test_restart_does_not_let_new_turn_overtake_unclaimed_old_turn(self):
        self.manager.start(self.a, "A1")
        self.broker.reserve("dead", block_ms=1)
        self.manager.start(self.a, "A2")
        self.manager.start(self.b, "B")
        self.start()
        self.assertTrue(self.agent.started["B"].wait(2))
        self.assertFalse(self.agent.started["A2"].is_set())
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.assertFalse(self.agent.started["A2"].is_set())
        self.agent.release["A1"].set()
        self.assertTrue(self.agent.started["A2"].wait(3))

    def test_publish_failure_after_completion_preserves_answer_and_retries_ack(self):
        run = self.manager.start(self.a, "A1")
        original = self.broker.publish
        failed = threading.Event()

        def fail_once(run_id, event):
            if event["type"] == "done" and not failed.is_set():
                failed.set()
                raise QueueUnavailableError("simulated publication outage")
            return original(run_id, event)

        self.agent.release["A1"].set()
        with patch.object(self.broker, "publish", side_effect=fail_once):
            self.start()
            self.wait_for(lambda: failed.is_set() and self.broker.queue_depth() == 0)
        self.assertEqual(self.agent.calls, ["A1"])
        self.assertEqual(self.manager.get(run["run_id"])["answer"], "answer:A1")
        self.assertEqual(self.manager.get(run["run_id"])["status"], "completed")

    def test_unpersisted_failure_is_not_acknowledged(self):
        self.manager.start(self.a, "A1")
        with patch.object(self.worker, "_process", side_effect=QueueUnavailableError("storage unavailable")):
            with self.assertRaises(QueueUnavailableError):
                self.worker.run_once(block_ms=1)
        self.assertEqual(self.broker.queue_depth(), 1)

    def test_process_lock_rejects_a_second_owner_and_releases(self):
        path = Path(self.tmp.name) / "worker.lock"
        with worker_process_lock(path):
            with self.assertRaises(RuntimeError):
                with worker_process_lock(path):
                    self.fail("second owner entered")
        with worker_process_lock(path):
            pass


@unittest.skipUnless(os.getenv("E2E_REDIS_URL"), "set E2E_REDIS_URL for real Redis regression")
class TestRedisRunDispatcher(TestRunDispatcher):
    def make_broker(self):
        self.prefix = f"dispatcher-test-{uuid.uuid4().hex}"
        return RedisChatRunBroker(os.environ["E2E_REDIS_URL"], prefix=self.prefix)

    def tearDown(self):
        super().tearDown()
        # Only this test's randomly generated prefix; never FLUSHDB.
        keys = list(self.broker._redis.scan_iter(match=f"{self.prefix}:*"))
        if keys:
            self.broker._redis.delete(*keys)

    def test_chat_and_research_share_session_exclusion(self):
        research = RedisResearchRunBroker(os.environ["E2E_REDIS_URL"], prefix=self.prefix)
        manager = RedisResearchRunManager(self.agent, research, persistent=False)
        worker = RedisResearchRunWorker(self.agent, research, consumer="research", claim_idle_ms=1000)
        self.project_stores.extend([manager.projects, worker.project_manager.projects])
        self.manager.start(self.a, "A1")
        time.sleep(0.005)
        manager.start(self.a, "R")
        self.manager.start(self.b, "B")
        self.start(workers={"chat": (self.worker, 2), "research": (worker, 2)})
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.assertTrue(self.agent.started["B"].wait(3))
        self.assertFalse(self.agent.started["R"].is_set())
        self.agent.release["A1"].set()
        self.assertTrue(self.agent.started["R"].wait(3))

    def test_two_research_sessions_overlap(self):
        broker = RedisResearchRunBroker(os.environ["E2E_REDIS_URL"], prefix=self.prefix)
        manager = RedisResearchRunManager(self.agent, broker, persistent=False)
        worker = RedisResearchRunWorker(self.agent, broker, consumer="research", claim_idle_ms=1000)
        self.project_stores.extend([manager.projects, worker.project_manager.projects])
        runs = [manager.start(sid, query) for sid, query in ((self.a, "A1"), (self.b, "B"))]
        self.start(workers={"research": (worker, 2)})
        self.assertTrue(self.agent.started["A1"].wait(3))
        self.assertTrue(self.agent.started["B"].wait(3))
        self.assertTrue(all(manager.get(run["run_id"])["status"] == "running" for run in runs))
        self.agent.release["A1"].set()
        self.agent.release["B"].set()
        self.wait_for(lambda: broker.queue_depth() == 0)
        self.assertTrue(all(manager.get(run["run_id"])["status"] == "completed" for run in runs))

    def test_stale_owner_cannot_renew_or_ack_a_reclaimed_message(self):
        self.manager.start(self.a, "A1")
        job = self.broker.reserve("old", block_ms=1)
        time.sleep(0.03)
        self.assertIsNotNone(self.broker.claim_stale("new", min_idle_ms=1))
        self.assertEqual(self.broker.renew("old", [job.message_id]), set())
        self.assertFalse(self.broker.acknowledge_owned("old", job.message_id))
        self.assertEqual(self.broker.queue_depth(), 1)
        self.assertEqual(self.broker.renew("new", [job.message_id]), {job.message_id})


class TestConcurrentAgentPersistence(unittest.TestCase):
    def test_real_graph_checkpoints_and_usage_are_session_scoped(self):
        from concurrent.futures import ThreadPoolExecutor
        from config import Config
        from research_agent import ResearchAgent

        barrier = threading.Barrier(2)

        class Response:
            status_code = 200
            text = ""
            raw = None

            def __init__(self, answer):
                self.answer = answer

            def raise_for_status(self):
                pass

            def iter_lines(self, **kwargs):
                barrier.wait(timeout=5)
                yield "data: " + json.dumps({"choices": [{"delta": {"content": self.answer}}]})
                prompt = 11 if self.answer == "reply-A" else 22
                yield "data: " + json.dumps({"choices": [], "usage": {
                    "prompt_tokens": prompt, "completion_tokens": 3, "total_tokens": prompt + 3,
                }})
                yield "data: [DONE]"

            def close(self):
                pass

        def respond(*args, **kwargs):
            message = next(m["content"] for m in reversed(kwargs["json"]["messages"]) if m["role"] == "user")
            return Response("reply-A" if "hello A" in message else "reply-B")

        with tempfile.TemporaryDirectory() as directory:
            agent = ResearchAgent(Config(deepseek_key="test", rag_enabled=False, data_dir=directory, api_max_retries=0))
            try:
                sessions = [agent.create_session(label)["thread_id"] for label in ("A", "B")]
                with patch("llm_client.requests.post", side_effect=respond) as post, ThreadPoolExecutor(2) as pool:
                    futures = [pool.submit(agent.step, f"hello {label}", session_id=sid, on_token=lambda _: None)
                               for label, sid in zip(("A", "B"), sessions)]
                    self.assertEqual([future.result(timeout=10) for future in futures], ["reply-A", "reply-B"])
                self.assertEqual(post.call_count, 2)
                for label, sid in zip(("A", "B"), sessions):
                    history = agent.get_history(sid)
                    self.assertEqual(history[-1]["content"], f"reply-{label}")
                    self.assertEqual(agent.sessions.get_usage(sid)["calls"], 1)
                    self.assertEqual(agent.sessions.get_usage(sid)["prompt"], 11 if label == "A" else 22)
                    self.assertEqual(agent.sessions.list_chat_runs(sid)[0]["status"], "completed")
            finally:
                agent.memory.close()
                agent.sessions.close()


if __name__ == "__main__":
    unittest.main()
