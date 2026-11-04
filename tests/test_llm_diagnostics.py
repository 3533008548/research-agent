"""Transport diagnostics use real client/graph paths with deterministic HTTP fakes."""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import patch

import requests

from llm_client import (
    LLMClient, LLMCircuitOpenError, LLMQueueFullError,
    LLMRequestFailedError, LLMRequestTimeoutError,
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
        client = LLMClient("test-key", "https://example.test", max_retries=2)
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


if __name__ == "__main__":
    unittest.main()
