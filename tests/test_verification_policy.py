"""Regressions for partial-input false alarms and repeated verifier rewrites."""
import unittest
from unittest.mock import patch

from graph_builder import build_graph
from llm_client import LLMClient
from verification import classify_review, verification_prompt


class Response:
    status_code = 200
    text = ""

    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}, "finish_reason": self.finish_reason}]}


class TestVerificationPolicy(unittest.TestCase):
    def test_full_answer_tail_and_evidence_are_visible(self):
        answer = "正文推导。" * 400 + "完整结论：仅在明确列出的模型假设下成立。"
        prompt = verification_prompt("说明适用范围", answer, ["原文：" + "内容" * 1200 + "末尾的限制条件"])
        self.assertIn(answer, prompt)
        self.assertIn("末尾的限制条件", prompt)
        self.assertIn("完整回答", prompt)

    def test_long_input_is_explicitly_excerpted_and_keeps_the_conclusion(self):
        answer = "a" * 30000 + "Final conclusion"
        prompt = verification_prompt("q", answer, ["evidence"])
        self.assertIn("中间内容由程序省略", prompt)
        self.assertIn("未覆盖全文", prompt)
        self.assertIn("Final conclusion", prompt)
        self.assertLess(len(prompt), 28000)

    def test_minor_and_ignorable_severe_issues_do_not_request_rewrites(self):
        for review in ("[轻微] 末尾结论不够充分。[需修正]", "[严重] 与本次问题无关。[可忽略]"):
            self.assertEqual(classify_review(review).result, "advisory")
        decision = classify_review("[严重] 不相关。[可忽略]\n\n[轻微] 措辞改进。[需修正]")
        self.assertEqual(decision.blocking, ())

    def test_unparseable_or_empty_review_is_not_a_pass_or_rewrite(self):
        for review in ("", "分析如下，但没有结论", "OK but the formula is wrong", "[严重]"):
            self.assertEqual(classify_review(review).result, "inconclusive")
        self.assertEqual(classify_review("OK").result, "passed")

    def app(self, events):
        app = build_graph(api_key="test", checkpoint_db=":memory:", event_callback=events.append,
                          llm_client=LLMClient("test", "https://example.test/chat"))
        self.addCleanup(app.checkpointer.conn.close)
        return app

    @staticmethod
    def state():
        return {"messages": [
            {"role": "user", "content": "原文的公式是什么，适用范围是什么？"},
            {"role": "tool", "content": "📚 原文表明 x=2。", "tool_call_id": "t"},
            {"role": "assistant", "content": "推导过程与假设条件。" * 30 + "x=3。完整的适用范围。"},
        ], "metadata": {"session_id": "retained-session"}}

    def test_one_targeted_repair_then_stop_even_when_verifier_keeps_objecting(self):
        events = []
        app = self.app(events)
        state = self.state()
        replies = [Response("[严重] 回答 x=3 与原文 x=2 矛盾。[需修正]\n\n[轻微] 文风冗长。[需修正]"),
                   Response("已修正的回答正文。" * 30 + "x=2。"),
                   Response("[严重] 仍有矛盾，需对照原文确认。[需修正]")]
        with patch("llm_client.requests.post", side_effect=replies) as post:
            # Begin at the real verifier node, then follow its real feedback through the graph.
            update = app.get_graph().nodes["verify"].data.invoke(state)
            self.assertNotIn("文风冗长", update["metadata"]["verify_feedback"])
            state["metadata"] = update["metadata"]
            result = app.invoke(state, config={"configurable": {"thread_id": "bounded"}})
        self.assertEqual(post.call_count, 3)  # Two reviews, one repair; no third review.
        self.assertEqual(result["metadata"], {"session_id": "retained-session"})
        self.assertIn("不再自动重写", result["messages"][-1]["content"])
        self.assertEqual([e["verification_round"] for e in events if e["type"] == "verify_request_started"], [1, 2])
        self.assertEqual(events[-1]["type"], "verify_unresolved")
        retry_prompt = post.call_args_list[-1].kwargs["json"]["messages"][0]["content"]
        self.assertIn("Check only", retry_prompt)
        self.assertNotIn("文风冗长", retry_prompt)

    def test_minor_only_finishes_without_calling_writer(self):
        events = []
        app = self.app(events)
        with patch("llm_client.requests.post", return_value=Response("[轻微] 表述有待完善。[需修正]")) as post:
            result = app.get_graph().nodes["verify"].data.invoke(self.state())
        self.assertEqual(post.call_count, 1)
        self.assertFalse(result.get("metadata", {}).get("verify_feedback"))
        self.assertIn("不触发自动重写", result["messages"][-1]["content"])
        self.assertEqual(events[-1]["type"], "verify_advisory")

    def test_truncated_ok_is_inconclusive(self):
        events = []
        app = self.app(events)
        with patch("llm_client.requests.post", return_value=Response("OK", "length")):
            result = app.get_graph().nodes["verify"].data.invoke(self.state())
        self.assertIn("未返回可用结论", result["messages"][-1]["content"])
        self.assertEqual(events[-1]["type"], "verify_inconclusive")

    def test_excerpt_review_cannot_pass_the_entire_answer(self):
        events = []
        app = self.app(events)
        state = self.state()
        state["messages"][-1]["content"] = "超长回答" * 7000
        with patch("llm_client.requests.post", return_value=Response("OK")):
            result = app.get_graph().nodes["verify"].data.invoke(state)
        self.assertIn("未覆盖全文", result["messages"][-1]["content"])
        self.assertEqual(events[-1]["type"], "verify_inconclusive")

    def test_repaired_stream_is_not_appended_to_visible_draft(self):
        import json
        class Stream(Response):
            def close(self):
                pass

            def iter_lines(self, **kwargs):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": self.content}, "finish_reason": None}]})
                yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield "data: [DONE]"
        streamed = []
        app = build_graph(api_key="test", checkpoint_db=":memory:", stream_callback=streamed.append,
                          llm_client=LLMClient("test", "https://example.test/chat"))
        self.addCleanup(app.checkpointer.conn.close)
        draft, repaired = "初稿 x=3。" * 30, "修正稿 x=2。" * 30
        with patch("llm_client.requests.post", side_effect=[Stream(draft), Response("[严重] x=3 与原文 x=2 矛盾。[需修正]"), Stream(repaired), Response("OK")]):
            result = app.invoke(self.state(), config={"configurable": {"thread_id": "stream-repair"}})
        self.assertEqual("".join(streamed), draft)
        self.assertEqual(result["messages"][-1]["content"], repaired)
