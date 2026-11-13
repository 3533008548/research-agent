"""Regressions for partial-input false alarms and repeated verifier rewrites."""
import unittest
from unittest.mock import patch

from graph_builder import build_graph, merge_messages, sanitize_model_messages
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
        self.assertIn("仍有事实争议", result["messages"][-1]["content"])
        self.assertNotIn("[严重]", result["messages"][-1]["content"])
        self.assertEqual(sum(m.get("role") == "assistant" for m in result["messages"]), 1)
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
        self.assertNotIn("messages", result)
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
        self.assertIn("只核对了部分内容", result["messages"][-1]["content"])
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
        self.assertEqual([m["content"] for m in result["messages"] if m["role"] == "assistant"], [repaired])

    def test_previous_turn_tools_do_not_trigger_review_of_new_task(self):
        events = []
        app = self.app(events)
        state = self.state()
        state["messages"] += [{"role": "user", "content": "只说明安装方法"},
                              {"role": "assistant", "content": "安装说明。" * 40}]
        with patch("llm_client.requests.post") as post:
            app.get_graph().nodes["verify"].data.invoke(state)
        post.assert_not_called()

    def test_replacement_preserves_other_turns_and_tool_pairs(self):
        previous = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "import"}, {"role": "assistant", "content": "draft"},
                    {"role": "assistant", "content": "checking", "tool_calls": [{"id": "t"}]},
                    {"role": "tool", "tool_call_id": "t", "content": "evidence"}]
        merged = merge_messages(previous, [{"role": "assistant", "content": "full final", "_replace_answer": True}])
        self.assertEqual(merged[:3], previous[:3])
        self.assertEqual(merged[3:5], previous[4:])
        self.assertEqual(merged[-1], {"role": "assistant", "content": "full final"})
        self.assertEqual(previous[3]["content"], "draft")

    def test_legacy_review_notes_are_not_in_model_context(self):
        previous = [{"role": "assistant", "content": "Original answer\n\n核验提示（不触发自动重写）：\n[轻微] old feedback"}]
        self.assertEqual(sanitize_model_messages(previous)[0]["content"], "Original answer")
        self.assertIn("old feedback", previous[0]["content"])

    def test_repair_prompt_requires_full_answer_to_current_user(self):
        app = self.app([])
        with patch("llm_client.requests.post", return_value=Response("[严重] x=3 与 x=2 冲突。[需修正]")):
            result = app.get_graph().nodes["verify"].data.invoke(self.state())
        feedback = result["metadata"]["verify_feedback"]
        self.assertIn("完整最终答复", feedback)
        self.assertIn("不要只输出勘误", feedback)

    def test_checkpoint_and_next_turn_keep_only_complete_repaired_answer(self):
        app = self.app([])
        draft = "初稿声称 x=3。" * 30
        final = "入库成功。以下为完整摘要，原文给出的 x=2。" * 15
        config = {"configurable": {"thread_id": "two-turns"}}
        with patch("llm_client.requests.post", side_effect=[Response(draft),
                   Response("[严重] x=3 与 x=2 矛盾。[需修正]"), Response(final), Response("OK"),
                   Response("已完成本轮操作。")]) as post:
            app.invoke(self.state(), config=config)
            saved = app.get_state(config).values["messages"]
            self.assertEqual([m["content"] for m in saved if m["role"] == "assistant"], [final])
            app.invoke({"messages": [{"role": "user", "content": "接下来只整理文件"}], "metadata": {}}, config=config)
        payload = str(post.call_args.kwargs["json"]["messages"])
        self.assertNotIn(draft, payload)
        self.assertNotIn("[严重]", payload)
        self.assertIn(final, payload)
        self.assertEqual(post.call_count, 5)
