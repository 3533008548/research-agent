"""Transport diagnostics use real client/graph paths with deterministic HTTP fakes."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from llm_client import (
    LLMClient, LLMCircuitOpenError, LLMQueueFullError,
    LLMRequestFailedError, LLMRequestTimeoutError, RequestPolicy,
)


class Response:
    def __init__(self, status=200, *, headers=None, text="", lines=()):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.lines = lines
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)

    def close(self):
        self.closed = True

    def iter_lines(self, **_kwargs):
        yield from (self.lines() if callable(self.lines) else self.lines)


class TestLLMDiagnostics(unittest.TestCase):
    def test_nonstream_read_timeout_policy_does_not_change_chat_defaults(self):
        client = LLMClient("test-key", "https://example.test", read_timeout_seconds=30)
        policy = RequestPolicy(deadline_seconds=120, read_timeout_seconds=90, max_retries=0)
        with patch("llm_client.requests.post", return_value=Response()) as post:
            client.post({}, budget=client.new_request_budget(policy))
            self.assertEqual(post.call_args.kwargs["timeout"][1], 90)
            client.post({})
            self.assertEqual(post.call_args.kwargs["timeout"][1], 30)
        short = client.new_request_budget(RequestPolicy(deadline_seconds=5, read_timeout_seconds=90))
        with patch("llm_client.requests.post", return_value=Response()) as post:
            client.post({}, budget=short)
            self.assertLessEqual(post.call_args.kwargs["timeout"][1], 5)

    def test_401_preserves_status_and_request_id_without_copying_payload_or_credentials(self):
        client = LLMClient("sk-private-key", "https://private.example/with-secret", max_retries=2)
        response = Response(401, headers={"X-Request-ID": "req-401-safe", "Authorization": "Bearer secret"},
                            text="private upstream body and sk-private-key")
        with patch("llm_client.requests.post", return_value=response) as post:
            with self.assertRaises(LLMRequestFailedError) as raised:
                client.post({"messages": [{"content": "private prompt"}]})
        details = raised.exception.error_details
        self.assertEqual(details["code"], "LLM_HTTP_ERROR")
        self.assertEqual(details["http_status"], 401)
        self.assertEqual(details["phase"], "response_headers")
        self.assertEqual(details["cause_type"], "HTTPError")
        self.assertEqual(details["request_id"], "req-401-safe")
        self.assertEqual((details["attempts"], details["retries_used"]), (1, 0))
        self.assertEqual(post.call_count, 1)
        encoded = json.dumps(details)
        for secret in ("sk-private-key", "private prompt", "private upstream", "Authorization", "private.example"):
            self.assertNotIn(secret, encoded)

    def test_unavailable_retry_after_is_not_counted_as_an_executed_retry(self):
        client = LLMClient("test-key", "https://example.test", max_retries=2, request_deadline_seconds=1)
        response = Response(429, headers={"Retry-After": "120"})
        with patch("llm_client.requests.post", return_value=response) as post:
            with self.assertRaises(LLMRequestFailedError) as raised:
                client.post({})
        self.assertEqual(raised.exception.error_details["http_status"], 429)
        self.assertEqual(raised.exception.error_details["attempts"], 1)
        self.assertEqual(raised.exception.error_details["retries_used"], 0)
        self.assertEqual(post.call_count, 1)

    def test_retry_exhaustion_reports_the_last_real_http_response(self):
        client = LLMClient("test-key", "https://example.test", max_retries=1)
        with patch("llm_client.requests.post", side_effect=[
            Response(500, headers={"request-id": "req-first"}),
            Response(503, headers={"request-id": "req-last"}),
        ]), patch("llm_client.time.sleep"):
            with self.assertRaises(LLMRequestFailedError) as raised:
                client.post({})
        details = raised.exception.error_details
        self.assertEqual((details["http_status"], details["request_id"]), (503, "req-last"))
        self.assertEqual((details["attempts"], details["retries_used"]), (2, 1))

    def test_connect_and_response_timeout_have_no_invented_http_status(self):
        for error, code, phase in (
            (requests.ConnectTimeout("secret host"), "LLM_CONNECT_TIMEOUT", "connect"),
            (requests.ReadTimeout("secret host"), "LLM_RESPONSE_TIMEOUT", "response_headers"),
            (requests.ConnectionError("secret host"), "LLM_CONNECTION_ERROR", "connect"),
        ):
            with self.subTest(code=code):
                client = LLMClient("test-key", "https://example.test", max_retries=0)
                with patch("llm_client.requests.post", side_effect=error):
                    with self.assertRaises(LLMRequestTimeoutError) as raised:
                        client.post({})
                details = raised.exception.error_details
                self.assertEqual((details["code"], details["phase"]), (code, phase))
                self.assertNotIn("http_status", details)
                self.assertNotIn("request_id", details)
                self.assertNotIn("secret host", json.dumps(details))
                self.assertGreater(details["deadline_ms"], details["elapsed_ms"])

    def test_queue_timeout_and_full_queue_do_not_report_a_model_request(self):
        for queue_size, duration, expected in ((0, 1, "LLM_QUEUE_FULL"), (1, 0.01, "LLM_QUEUE_TIMEOUT")):
            with self.subTest(code=expected):
                client = LLMClient("test-key", "https://example.test", max_concurrency=1,
                                   queue_size=queue_size, request_deadline_seconds=duration)
                client._slots.acquire()
                try:
                    with patch("llm_client.requests.post") as post:
                        with self.assertRaises((LLMQueueFullError, LLMRequestTimeoutError)) as raised:
                            client.post({})
                    details = raised.exception.error_details
                    self.assertEqual((details["code"], details["phase"]), (expected, "queue"))
                    self.assertEqual((details["attempts"], details["retries_used"]), (0, 0))
                    self.assertNotIn("http_status", details)
                    post.assert_not_called()
                finally:
                    client._slots.release()

    def test_circuit_rejection_has_its_own_code_and_zero_attempts(self):
        client = LLMClient("test-key", "https://example.test", circuit_failure_threshold=1)
        client._circuit.record_failure()
        with patch("llm_client.requests.post") as post:
            with self.assertRaises(LLMCircuitOpenError) as raised:
                client.post({})
        self.assertEqual(raised.exception.error_details["code"], "LLM_CIRCUIT_OPEN")
        self.assertEqual(raised.exception.error_details["attempts"], 0)
        post.assert_not_called()

    def test_request_ids_are_bounded_tokens_from_allowlisted_headers(self):
        for request_id in ("sk-private-key", "prefix-sk-private-key-suffix", "Bearer-credential", "x" * 121, "id\nsecret", "https://secret.test"):
            with self.subTest(value=request_id):
                client = LLMClient("sk-private-key", "https://example.test", max_retries=0)
                response = Response(401, headers={"X-Request-ID": request_id, "X-Api-Key": "secret"})
                with patch("llm_client.requests.post", return_value=response):
                    with self.assertRaises(LLMRequestFailedError) as raised:
                        client.post({})
                self.assertNotIn("request_id", raised.exception.error_details)

    def _graph(self, client, response):
        from graph_builder import build_graph
        budget = client.new_request_budget()
        events = []
        tokens = []
        graph = build_graph(api_key="test-key", checkpoint_db=":memory:", enable_verify=False,
                            llm_client=client, stream_callback=tokens.append, event_callback=events.append)
        return graph, budget, events, tokens

    def test_stream_deadline_keeps_http_200_cause_and_actual_retry_count(self):
        client = LLMClient("test-key", "https://example.test", max_retries=2)
        graph, budget, events, _tokens = self._graph(client, None)

        def lines():
            # A model can send reasoning/keepalives without visible content.
            # The shared budget then expires before replay can issue HTTP #2.
            budget.deadline_at = time.monotonic() - 1
            yield 'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}'

        response = Response(headers={"request-id": "req-stream"}, lines=lines)
        try:
            with patch.object(client, "new_request_budget", return_value=budget), \
                 patch("llm_client.requests.post", return_value=response) as post:
                with self.assertRaises(LLMRequestTimeoutError) as raised:
                    graph.invoke({"messages": [{"role": "user", "content": "private prompt"}], "metadata": {}},
                                 config={"configurable": {"thread_id": "deadline"}})
            details = raised.exception.error_details
            self.assertEqual(details["code"], "LLM_DEADLINE_EXCEEDED")
            self.assertEqual((details["phase"], details["http_status"], details["cause_type"]),
                             ("stream_read", 200, "ReadTimeout"))
            self.assertEqual((details["attempts"], details["retries_used"]), (1, 0))
            self.assertEqual(details["request_id"], "req-stream")
            failures = [event for event in events if event["type"] == "llm_request_failed"]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["error_details"], details)
            self.assertEqual(post.call_count, 1)
            self.assertNotIn("private", json.dumps(details))
        finally:
            graph.checkpointer.conn.close()

    def test_stream_partial_answer_is_preserved_but_not_marked_model_success(self):
        client = LLMClient("test-key", "https://example.test", max_retries=0)
        graph, _budget, events, tokens = self._graph(client, None)
        response = Response(lines=['data: {"choices":[{"delta":{"content":"已有内容"}}]}'])
        try:
            with patch("llm_client.requests.post", return_value=response) as post:
                result = graph.invoke({"messages": [{"role": "user", "content": "问题"}], "metadata": {}},
                                      config={"configurable": {"thread_id": "partial"}})
            self.assertIn("已有内容", result["messages"][-1]["content"])
            self.assertIn("输出中断", "".join(tokens))
            self.assertEqual(post.call_count, 1)
            failures = [event for event in events if event["type"] == "llm_request_failed"]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["error_details"]["code"], "LLM_STREAM_INTERRUPTED")
            self.assertFalse(any(event["type"] == "llm_request_finished" for event in events))
        finally:
            graph.checkpointer.conn.close()

    def test_recovered_stream_does_not_leave_failure_diagnostics(self):
        client = LLMClient("test-key", "https://example.test", max_retries=1)
        graph, budget, events, _tokens = self._graph(client, None)
        failed = Response(headers={"request-id": "req-old"})
        recovered = Response(headers={"request-id": "req-new"}, lines=[
            'data: {"choices":[{"delta":{"content":"完成"}}]}', "data: [DONE]",
        ])
        try:
            with patch.object(client, "new_request_budget", return_value=budget), \
                 patch("llm_client.requests.post", side_effect=[failed, recovered]):
                result = graph.invoke({"messages": [{"role": "user", "content": "问题"}], "metadata": {}},
                                      config={"configurable": {"thread_id": "recovered"}})
            self.assertEqual(result["messages"][-1]["content"], "完成")
            self.assertFalse(any(event["type"] == "llm_request_failed" for event in events))
            self.assertEqual(budget.last_request_id, "req-new")
            self.assertEqual(budget.last_cause_type, "")
            self.assertTrue(any(event["type"] == "llm_request_finished" for event in events))
        finally:
            graph.checkpointer.conn.close()

    @staticmethod
    def _chunk(content="", *, reason=None, usage=None, tools=None, reasoning=None):
        delta = {"content": content}
        if tools is not None:
            delta["tool_calls"] = tools
        if reasoning is not None:
            delta["reasoning_content"] = reasoning
        return "data:" + json.dumps({"choices": [{"delta": delta, "finish_reason": reason}], "usage": usage})

    def _invoke_streams(self, responses, *, retries=2, verify=False, client=None, usage=None):
        from graph_builder import build_graph
        client = client or LLMClient("test-key", "https://example.test", max_retries=retries, max_concurrency=1)
        events, tokens = [], []
        graph = build_graph(api_key="test-key", checkpoint_db=":memory:", enable_verify=verify,
                            llm_client=client, stream_callback=tokens.append,
                            token_usage=usage, event_callback=events.append)
        try:
            with patch("llm_client.requests.post", side_effect=responses) as post:
                result = graph.invoke({"messages": [{"role": "user", "content": "question"}], "metadata": {}},
                                      config={"configurable": {"thread_id": "recovery"}})
            self.assertTrue(client._slots.acquire(blocking=False))
            client._slots.release()
            return result, events, tokens, post
        finally:
            graph.checkpointer.conn.close()

    def test_partial_stream_recovers_without_appending_duplicate_text(self):
        old = Response(lines=[self._chunk("old draft", reasoning="discard this")])
        new = Response(lines=[self._chunk("complete replacement", reason="stop"), "data: [DONE]"])
        result, events, tokens, post = self._invoke_streams([old, new])
        self.assertEqual(result["messages"][-1]["content"], "complete replacement")
        self.assertNotIn("reasoning_content", result["messages"][-1])
        self.assertEqual(tokens, ["old draft"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"], post.call_args_list[1].kwargs["json"])
        self.assertFalse(any(e["type"] == "llm_request_failed" for e in events))
        self.assertTrue(old.closed and new.closed)

    def test_stream_recovery_is_bounded_and_preserves_last_partial(self):
        responses = [Response(lines=[self._chunk("partial")]) for _ in range(3)]
        result, events, tokens, post = self._invoke_streams(responses, verify=True)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(tokens, ["partial"])
        self.assertIn("partial", result["messages"][-1]["content"])
        self.assertTrue(result["metadata"]["stream_incomplete"])
        self.assertEqual(sum(e["type"] == "llm_request_failed" for e in events), 1)
        self.assertFalse(any(e["type"] == "verify_request_started" for e in events))
        self.assertTrue(all(response.closed for response in responses))

    def test_replay_http_failure_preserves_original_partial(self):
        result, events, _, post = self._invoke_streams([
            Response(lines=[self._chunk("preserved draft")]), Response(status=401),
        ])
        self.assertIn("preserved draft", result["messages"][-1]["content"])
        self.assertEqual(post.call_count, 2)
        failure = next(e for e in events if e["type"] == "llm_request_failed")
        self.assertEqual(failure["error_details"]["http_status"], 401)

    def test_length_finish_recovers_with_compact_answer_instruction(self):
        result, events, _, post = self._invoke_streams([
            Response(lines=[self._chunk("truncated", reason="length"), "data: [DONE]"]),
            Response(lines=[self._chunk("complete", reason="stop"), "data: [DONE]"]),
        ])
        self.assertEqual(result["messages"][-1]["content"], "complete")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["messages"][-1]["role"], "system")
        self.assertFalse(any(e["type"] == "llm_request_failed" for e in events))

    def test_content_filter_does_not_trigger_automatic_replay(self):
        result, events, _, post = self._invoke_streams([
            Response(lines=[self._chunk("partial", reason="content_filter"), "data: [DONE]"]),
        ])
        self.assertEqual(post.call_count, 1)
        failure = next(e for e in events if e["type"] == "llm_request_failed")
        self.assertEqual(failure["error_details"]["code"], "LLM_CONTENT_FILTERED")

    def test_terminal_choice_survives_missing_done_or_network_close(self):
        for close_error in (False, True):
            def lines():
                yield self._chunk("finished", reason="stop")
                if close_error:
                    raise requests.exceptions.ChunkedEncodingError("closed after completion")
            result, _, _, post = self._invoke_streams([Response(lines=lines)])
            self.assertEqual(result["messages"][-1]["content"], "finished")
            self.assertEqual(post.call_count, 1)

    def test_null_usage_and_cumulative_usage_frames_count_once(self):
        usage = {}
        complete = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        result, _, _, _ = self._invoke_streams([Response(lines=[
            ": keep-alive", self._chunk("answer", usage=None),
            self._chunk(usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}),
            self._chunk(reason="stop", usage=complete), "data: [DONE]",
        ])], usage=usage)
        self.assertEqual(result["messages"][-1]["content"], "answer")
        self.assertEqual((usage["calls"], usage["total"]), (1, 13))

    def test_truncated_tool_arguments_are_discarded_before_execution(self):
        broken = {"index": 0, "id": "broken", "function": {"name": "list_papers", "arguments": '{"bad":'}}
        for reason in (None, "tool_calls"):
            with patch("graph_builder.execute_tool") as execute:
                result, _, _, post = self._invoke_streams([
                    Response(lines=[self._chunk(tools=[broken], reasoning="old", reason=reason)]),
                    Response(lines=[self._chunk("complete", reason="stop"), "data: [DONE]"]),
                ])
            execute.assert_not_called()
            self.assertNotIn("tool_calls", result["messages"][-1])
            self.assertEqual(post.call_count, 2)

    def test_completed_tool_is_not_reexecuted_when_final_answer_replays(self):
        tool = {"index": 0, "id": "once", "function": {"name": "list_papers", "arguments": '{}'}}
        with patch("graph_builder.execute_tool", return_value="saved tool evidence") as execute:
            result, _, _, post = self._invoke_streams([
                Response(lines=[self._chunk(tools=[tool], reason="tool_calls"), "data: [DONE]"]),
                Response(lines=[self._chunk("unfinished answer")]),
                Response(lines=[self._chunk("complete answer", reason="stop"), "data: [DONE]"]),
            ])
        execute.assert_called_once()
        self.assertEqual(result["messages"][-1]["content"], "complete answer")
        self.assertEqual(post.call_args_list[1].kwargs["json"], post.call_args_list[2].kwargs["json"])

    def test_new_default_allows_healthy_output_past_old_45_second_cutoff(self):
        from config import Config
        self.assertEqual(Config().api_request_deadline_seconds, 180)
        client = LLMClient("test-key", "https://example.test")
        budget = client.new_request_budget()
        def lines():
            budget.deadline_at -= 46
            yield self._chunk("long answer", reason="stop")
            yield "data: [DONE]"
        with patch.object(client, "new_request_budget", return_value=budget):
            result, _, _, post = self._invoke_streams([Response(lines=lines)], client=client)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["messages"][-1]["content"], "long answer")

    def test_expired_shared_deadline_does_not_restart_or_extend_budget(self):
        client = LLMClient("test-key", "https://example.test")
        budget = client.new_request_budget()
        def lines():
            yield self._chunk("partial")
            budget.deadline_at = time.monotonic() - 1
            yield ": keep-alive"
        with patch.object(client, "new_request_budget", return_value=budget):
            result, events, _, post = self._invoke_streams([Response(lines=lines)], client=client)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(result["metadata"]["stream_incomplete"])
        self.assertEqual(next(e for e in events if e["type"] == "llm_request_failed")["error_details"]["code"], "LLM_DEADLINE_EXCEEDED")

    def test_malformed_sse_is_recovered_instead_of_silently_dropping_content(self):
        result, _, _, post = self._invoke_streams([
            Response(lines=[self._chunk("prefix"), 'data: {invalid json}']),
            Response(lines=[self._chunk("complete", reason="stop"), "data: [DONE]"]),
        ])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["messages"][-1]["content"], "complete")

    def test_reasoning_without_answer_is_not_reported_as_success(self):
        result, _, tokens, post = self._invoke_streams([
            Response(lines=[self._chunk(reasoning="private reasoning", reason="stop"), "data: [DONE]"]),
            Response(lines=[self._chunk("complete", reason="stop"), "data: [DONE]"]),
        ])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(tokens, ["complete"])
        self.assertNotIn("reasoning_content", result["messages"][-1])

    def test_real_http_connection_drop_recovers_with_the_same_request(self):
        from graph_builder import build_graph
        bodies, tokens = [], []
        chunk = self._chunk
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                if len(bodies) == 1:
                    frame = chunk("未完成草稿")
                else:
                    # Providers may explicitly set optional delta fields null.
                    frame = 'data:' + json.dumps({"choices": [{"delta": {
                        "content": "完整回复", "tool_calls": None, "reasoning_content": None,
                    }, "finish_reason": "stop"}], "usage": None}) + '\n\ndata: [DONE]'
                encoded = (frame + '\n\n').encode("utf-8")
                self.wfile.write(f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n")
                if len(bodies) > 1:
                    self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = LLMClient("test-key", f"http://127.0.0.1:{server.server_port}", max_concurrency=1)
        graph = build_graph(api_key="test-key", checkpoint_db=":memory:", enable_verify=False,
                            llm_client=client, stream_callback=tokens.append)
        try:
            result = graph.invoke({"messages": [{"role": "user", "content": "question"}], "metadata": {}},
                                  config={"configurable": {"thread_id": "http-recovery"}})
            self.assertEqual(result["messages"][-1]["content"], "完整回复")
            self.assertEqual(tokens, ["未完成草稿"])
            self.assertEqual(len(bodies), 2)
            self.assertEqual(bodies[0], bodies[1])
            self.assertTrue(client._slots.acquire(blocking=False))
            client._slots.release()
        finally:
            graph.checkpointer.conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cancel_during_recovery_releases_slot_and_never_replays(self):
        from cancellation import RequestCancelledError
        from graph_builder import build_graph
        cancelled = threading.Event()
        client = LLMClient("test-key", "https://example.test", max_concurrency=1)
        response = Response(lines=[self._chunk("partial")])
        def event(event):
            if event["type"] == "llm_stream_recovery_started":
                cancelled.set()
        graph = build_graph(api_key="test-key", checkpoint_db=":memory:", enable_verify=False,
                            llm_client=client, stream_callback=lambda _token: None,
                            event_callback=event, cancel_event=cancelled)
        try:
            with patch("llm_client.requests.post", return_value=response) as post:
                with self.assertRaises(RequestCancelledError):
                    graph.invoke({"messages": [{"role": "user", "content": "question"}], "metadata": {}},
                                 config={"configurable": {"thread_id": "cancel-recovery"}})
            self.assertEqual(post.call_count, 1)
            self.assertTrue(response.closed)
            self.assertTrue(client._slots.acquire(blocking=False))
            client._slots.release()
        finally:
            graph.checkpointer.conn.close()

    def test_recovered_agent_run_persists_complete_answer_and_success(self):
        from config import Config
        from research_agent import ResearchAgent
        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False,
                                         data_dir=tmp, api_max_concurrency=1))
            session_id = agent.thread_id
            try:
                with patch("llm_client.requests.post", side_effect=[
                    Response(lines=[self._chunk("discarded draft")]),
                    Response(lines=[self._chunk("complete answer", reason="stop"), "data: [DONE]"]),
                ]) as post:
                    answer = agent.step("test recovery", on_token=lambda _token: None)
                self.assertEqual(answer, "complete answer")
                self.assertEqual(post.call_count, 2)
                self.assertEqual(agent.get_last_trace(session_id)["outcome"], "success")
                run = agent.sessions.list_chat_runs(session_id)[0]
                self.assertEqual((run["status"], run["answer"], run["error_type"]),
                                 ("completed", "complete answer", ""))
                events = agent.sessions.get_run_events(run["run_id"])
                recovery = next(e for e in events if e["status"] == "recovering")
                self.assertEqual(recovery["error_details"]["code"], "LLM_STREAM_INTERRUPTED")
                self.assertFalse(any(e["status"] == "failed" for e in events))
                self.assertEqual(agent.get_history(session_id)[-1]["content"], "complete answer")
            finally:
                agent.memory.close()
                agent.sessions.close()
            reopened = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            try:
                self.assertEqual(reopened.get_history(session_id)[-1]["content"], "complete answer")
                self.assertEqual(reopened.sessions.get_chat_run(run["run_id"])["status"], "completed")
            finally:
                reopened.memory.close()
                reopened.sessions.close()


if __name__ == "__main__":
    unittest.main()
