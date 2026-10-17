"""Stage-timeline timing: the numbers the run center draws must be measured.

The UI is going to render a timeline from these events, so the only defensible
source of "how long did this stage take" is the backend clock.  These tests pin
the measurement itself — pairing, isolation between runs, and the refusal to
forward anything that is not one of the two bounded numbers.
"""

from __future__ import annotations

import unittest

from api.redis_runs import _safe_status_event
from api.sse import format_sse, public_sse_event
from run_contract import (
    TERMINAL_STAGE_STATUSES,
    bounded_timing_metrics,
    safe_metrics,
)


class BoundedTimingMetricsTests(unittest.TestCase):
    def test_keeps_only_the_two_timing_keys(self):
        projected = bounded_timing_metrics({
            "at_ms": 12.34, "duration_ms": 5, "prompt": "secret", "answer": "secret",
        })
        self.assertEqual(projected, {"at_ms": 12.3, "duration_ms": 5.0})

    def test_ignores_bool_and_non_numeric(self):
        # bool is an int subclass; True must never become a 1ms duration.
        self.assertEqual(bounded_timing_metrics({"at_ms": True}), {})
        self.assertEqual(bounded_timing_metrics({"at_ms": "12"}), {})
        self.assertEqual(bounded_timing_metrics(None), {})

    def test_survives_safe_metrics_allow_list(self):
        # The durable-event filter is a separate gate; timing must pass both or
        # it would be silently dropped on the way to SQLite.
        self.assertEqual(safe_metrics({"at_ms": 1.0, "duration_ms": 2.0}), {"at_ms": 1.0, "duration_ms": 2.0})


class TerminalStageStatusTests(unittest.TestCase):
    def test_partial_success_still_closes_a_stage(self):
        # "partial" consumed real wall-clock time; treating it as non-terminal
        # would leak the stage clock and never emit a duration.
        self.assertIn("partial", TERMINAL_STAGE_STATUSES)
        self.assertIn("partial_failed", TERMINAL_STAGE_STATUSES)
        self.assertIn("skipped", TERMINAL_STAGE_STATUSES)

    def test_running_is_not_terminal(self):
        self.assertNotIn("running", TERMINAL_STAGE_STATUSES)
        self.assertNotIn("queued", TERMINAL_STAGE_STATUSES)


class SafeStatusEventTests(unittest.TestCase):
    def test_forwards_timing_alongside_stage(self):
        payload = _safe_status_event({
            "stage": "curator", "status": "completed", "at_ms": 900.0, "duration_ms": 120.5,
            "message": "drop me",
        })
        self.assertEqual(payload["stage"], "curator")
        self.assertEqual(payload["at_ms"], 900.0)
        self.assertEqual(payload["duration_ms"], 120.5)
        self.assertNotIn("message", payload)

    def test_absent_timing_is_simply_omitted(self):
        payload = _safe_status_event({"stage": "planner", "status": "running"})
        self.assertEqual(payload, {"type": "status", "stage": "planner", "status": "running"})

    def test_sse_status_event_carries_timing(self):
        public = public_sse_event({
            "type": "status", "status": "completed", "stage": "scouts",
            "at_ms": 10.0, "duration_ms": 4.0,
        })
        self.assertEqual(public["at_ms"], 10.0)
        self.assertEqual(public["duration_ms"], 4.0)

    def test_format_sse_emits_parseable_timing(self):
        frame = format_sse({"type": "status", "status": "completed", "duration_ms": 3.14159})
        self.assertIn('"duration_ms":3.1', frame)

    def test_unknown_event_type_still_gets_no_timing(self):
        # The public vocabulary guard must not become a timing side channel.
        public = public_sse_event({"type": "internal_secret", "duration_ms": 5.0})
        self.assertEqual(public, {"type": "status", "status": "running"})


class DailyStageClockTests(unittest.TestCase):
    """The clock itself, exercised without a scheduler or any network."""

    def _orchestrator(self):
        from daily_orchestrator import DailyResearchOrchestrator

        return DailyResearchOrchestrator(scheduler=None, llm_client=None)

    def test_running_then_completed_yields_duration(self):
        orchestrator = self._orchestrator()
        opened = orchestrator._stage_timing("run-1", "scouts", "running")
        closed = orchestrator._stage_timing("run-1", "scouts", "completed")
        self.assertIn("at_ms", opened)
        # A terminal event with no matching opener still reports position, but
        # must not invent a duration out of thin air.
        self.assertNotIn("duration_ms", opened)
        self.assertIn("duration_ms", closed)
        self.assertGreaterEqual(closed["duration_ms"], 0.0)
        self.assertGreaterEqual(closed["at_ms"], opened["at_ms"])

    def test_repeated_running_does_not_reset_the_clock(self):
        orchestrator = self._orchestrator()
        orchestrator._stage_timing("run-1", "curator", "running")
        orchestrator._stage_timing("run-1", "curator", "running")
        first = orchestrator._stage_started[("run-1", "curator")]
        orchestrator._stage_timing("run-1", "curator", "running")
        self.assertEqual(first, orchestrator._stage_started[("run-1", "curator")])

    def test_open_stage_measures_without_emitting_an_event(self):
        orchestrator = self._orchestrator()
        orchestrator._open_stage("run-1", "quality_gate")
        timing = orchestrator._stage_timing("run-1", "quality_gate", "completed")
        self.assertIn("duration_ms", timing)

    def test_duration_does_not_leak_between_runs(self):
        # One orchestrator serves every queued run.  If the clock were keyed by
        # agent alone, run-2's planner would inherit run-1's elapsed time.
        orchestrator = self._orchestrator()
        orchestrator._stage_timing("run-1", "planner", "running")
        orchestrator._stage_timing("run-2", "planner", "completed")
        self.assertNotIn("duration_ms", orchestrator._stage_timing("run-2", "planner", "completed"))

    def test_reset_clears_only_the_named_run(self):
        orchestrator = self._orchestrator()
        orchestrator._open_stage("run-1", "delivery")
        orchestrator._open_stage("run-2", "delivery")
        orchestrator._reset_stage_timing("run-1")
        self.assertNotIn(("run-1", "delivery"), orchestrator._stage_started)
        self.assertIn(("run-2", "delivery"), orchestrator._stage_started)
        self.assertNotIn("run-1", orchestrator._run_started)
        self.assertIn("run-2", orchestrator._run_started)


class DailyRunTimelineTests(unittest.TestCase):
    """End-to-end: a real offline daily run must yield measurable stages."""

    def test_offline_run_records_per_stage_durations(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from daily_orchestrator import DailyResearchOrchestrator
        from evals.runtime_fakes import FixtureSourceRouter
        from scheduler import Scheduler

        fixtures = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "providers"
        if not fixtures.is_dir():
            self.skipTest("daily source fixtures are unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            store = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                store.add_keyword("offline timeline")
                router = FixtureSourceRouter(fixtures, failed_sources={"openaire"}, seed=7)
                orchestrator = DailyResearchOrchestrator(
                    scheduler=store, llm_client=None, model="",
                    max_keyword_concurrency=1, max_results_per_keyword=1,
                )
                seen: list[dict] = []
                with patch("daily_orchestrator.requests.get", side_effect=router.get):
                    result = orchestrator.run("daily", on_progress=seen.append)
            finally:
                store.close()

        events = seen
        self.assertTrue(events, "the run reported no progress events")
        for event in events:
            self.assertIn("at_ms", event)
        with_duration = [e for e in events if "duration_ms" in e]
        self.assertTrue(with_duration, "no stage reported a duration")

        # Durations must be real persisted numbers, not just callback-local.
        with tempfile.TemporaryDirectory() as tmp:
            store = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                store.add_keyword("offline timeline")
                router = FixtureSourceRouter(fixtures, failed_sources=set(), seed=7)
                orchestrator = DailyResearchOrchestrator(
                    scheduler=store, llm_client=None, model="",
                    max_keyword_concurrency=1, max_results_per_keyword=1,
                )
                with patch("daily_orchestrator.requests.get", side_effect=router.get):
                    persisted_run = orchestrator.run("daily")
                rows = store.get_daily_agent_events(persisted_run.run_id)
            finally:
                store.close()

        measured = [row for row in rows if isinstance(row.get("details"), dict) and "duration_ms" in row["details"]]
        self.assertTrue(measured, "no durable event kept a duration")
        for row in measured:
            # safe_metrics must keep it a number, not a string.
            self.assertIsInstance(row["details"]["duration_ms"], (int, float))
            self.assertNotIsInstance(row["details"]["duration_ms"], bool)
            json.dumps(row["details"])


if __name__ == "__main__":
    unittest.main()