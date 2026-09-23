import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from conversation_reference import (
    observe_assistant_message,
    observe_user_message,
    render_reference_context,
)
from context_engine import prepare_model_context
from session_store import SessionStore


class TestConversationReference(unittest.TestCase):
    def test_correction_moves_this_direction_to_the_replacement_paper(self):
        state, _ = observe_user_message({}, "请介绍论文A的内容。")
        state, correction = observe_user_message(state, "我记错了，应该是论文B的内容。")
        state, resolution = observe_user_message(state, "就按这个方向做吧。")

        self.assertEqual(correction.status, "none")
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.target_label, "论文B")
        self.assertEqual(state["corrections"], [{"old": "论文A", "new": "论文B"}])

    def test_second_direction_falls_back_to_ordered_papers_when_no_direction_menu_exists(self):
        state, _ = observe_user_message(state={}, user_text="请比较论文A、论文B和论文C。")
        state, resolution = observe_user_message(state, "按第二个方向来。")

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.target_label, "论文B")
        self.assertEqual(resolution.source, "用户列出的论文候选")
        self.assertIn("本轮已解析：按第二个方向来。 → 论文B", render_reference_context(state, resolution))

    def test_visible_direction_headings_override_paper_order(self):
        state, _ = observe_user_message({}, "请比较论文A、论文B和论文C。")
        state = observe_assistant_message(
            state,
            "【方向 1】复现论文A的设定\n【方向 2】扩展论文C的方法",
        )
        _state, resolution = observe_user_message(state, "按第二个方向来。")

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.target_kind, "direction")
        self.assertEqual(resolution.target_label, "扩展论文C的方法")

    def test_this_direction_asks_for_clarification_with_multiple_unselected_directions(self):
        state, _ = observe_user_message({}, "请比较论文A和论文B。")
        state = observe_assistant_message(
            state,
            "【方向 1】复现论文A\n【方向 2】比较论文B",
        )
        _state, resolution = observe_user_message(state, "就按这个方向做吧。")

        self.assertEqual(resolution.status, "clarify")
        self.assertIn("多个方向候选", resolution.reason)

    def test_reference_state_is_session_scoped_and_deleted_with_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                first = store.create()
                second = store.create()
                state, _ = observe_user_message({}, "请介绍论文A的内容。")
                store.save_reference_state(first["thread_id"], state)

                self.assertEqual(
                    store.get_reference_state(first["thread_id"])["focus"]["paper_id"],
                    state["focus"]["paper_id"],
                )
                self.assertEqual(store.get_reference_state(second["thread_id"]), {})
                self.assertTrue(store.delete(first["thread_id"]))
                self.assertEqual(store.get_reference_state(first["thread_id"]), {})
            finally:
                store.close()

    def test_session_message_index_recalls_visible_history_and_deletes_with_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                store.append_session_message(session["thread_id"], "user", "讨论 alpha-protocol 的实验约束")
                store.append_session_message(session["thread_id"], "assistant", "alpha-protocol 需要保留训练测试隔离")

                recalled = store.search_session_messages(session["thread_id"], "继续 alpha-protocol")
                self.assertTrue(recalled)
                self.assertIn("alpha-protocol", recalled[0]["content"])
                self.assertTrue(store.delete(session["thread_id"]))
                self.assertEqual(store.search_session_messages(session["thread_id"], "alpha-protocol"), [])
            finally:
                store.close()

    def test_agent_injects_persisted_resolution_before_model_call(self):
        from config import Config
        from research_agent import ResearchAgent

        class _FakeApp:
            state = None

            def invoke(self, state, config):
                self.state = state
                return {"messages": [{"role": "assistant", "content": "收到"}]}

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            app = _FakeApp()
            try:
                with patch.object(agent, "_build_app", return_value=app):
                    agent.step("请介绍论文A的内容。")
                    agent.step("我记错了，应该是论文B的内容。")
                    agent.step("就按这个方向做吧。")
                context = app.state["metadata"]["reference_context"]
                self.assertIn("当前论文焦点：论文B", context)
                self.assertIn("本轮已解析：就按这个方向做吧。 → 论文B", context)
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_agent_replays_relevant_prior_visible_message_only_for_reference_turn(self):
        from config import Config
        from research_agent import ResearchAgent

        class _FakeApp:
            state = None

            def invoke(self, state, config):
                self.state = state
                return {"messages": [{"role": "assistant", "content": "alpha-protocol 已记录"}]}

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            app = _FakeApp()
            try:
                with patch.object(agent, "_build_app", return_value=app):
                    agent.step("请记录 alpha-protocol 的实验约束")
                    agent.step("继续 alpha-protocol 的讨论")
                self.assertIn("按需召回的早期会话片段", app.state["metadata"]["session_recall"])
                self.assertIn("alpha-protocol", app.state["metadata"]["session_recall"])
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_deep_research_receives_resolved_reference_context(self):
        from config import Config
        from research_agent import ResearchAgent

        captured = {}

        class _FakeOrchestrator:
            def __init__(self, **_kwargs):
                pass

            def run(self, query, **kwargs):
                captured["query"] = query
                captured["context"] = kwargs["context"]
                return SimpleNamespace(
                    usage={"prompt": 0, "completion": 0, "total": 0, "calls": 0},
                    answer="研究报告",
                    status="completed",
                    run_id="research-test",
                    trace={},
                )

        class _FakeApp:
            def update_state(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            try:
                state, _ = observe_user_message({}, "请比较论文A、论文B和论文C。")
                agent.sessions.save_reference_state(agent.thread_id, state)
                with patch("research_agent.ResearchOrchestrator", _FakeOrchestrator), \
                     patch.object(agent, "_build_app", return_value=_FakeApp()):
                    self.assertEqual(agent.research("按第二个方向继续研究"), "研究报告")
                self.assertEqual(captured["query"], "按第二个方向继续研究")
                self.assertIn("本轮已解析：按第二个方向继续研究 → 论文B", captured["context"])
            finally:
                agent.memory.close()
                agent.sessions.close()

class TestModelContextEngine(unittest.TestCase):
    def test_compaction_keeps_checkpoint_style_head_and_recent_tail(self):
        messages = [
            {"role": "user", "content": "最初目标：比较三个调度方案"},
            {"role": "assistant", "content": "我会按证据比较。"},
        ]
        messages.extend(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"早期消息 {index} " + "x" * 900,
            }
            for index in range(10)
        )
        messages.append({"role": "user", "content": "现在请按第二个方向继续。"})

        prepared = prepare_model_context(
            messages,
            context_limit=1_000,
            protect_first_messages=2,
            protect_last_messages=3,
            tail_token_budget=160,
        )

        self.assertTrue(prepared.compacted)
        self.assertEqual(prepared.messages[0]["content"], "最初目标：比较三个调度方案")
        self.assertEqual(prepared.messages[-1]["content"], "现在请按第二个方向继续。")
        self.assertIn("早期对话已为本次模型调用压缩", prepared.archive_context)
        self.assertIn("早期消息", prepared.archive_context)
        self.assertLess(prepared.prepared_tokens, prepared.original_tokens)

    def test_graph_uses_compacted_view_without_rewriting_state_messages(self):
        from graph_builder import build_graph
        from llm_client import LLMClient

        class _Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def close():
                return None

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"role": "assistant", "content": "已继续"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
                }

        original = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"消息{index}" + "x" * 2_000}
            for index in range(28)
        ]
        client = LLMClient("test-key", "https://example.test/chat", max_retries=0)
        with patch("llm_client.requests.post", return_value=_Response()) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", enable_verify=False,
                allowed_tool_names=set(),
                token_usage={"context_limit": 100, "prompt": 0, "completion": 0, "total": 0, "calls": 0},
                llm_client=client,
            )
            try:
                result = app.invoke(
                    {"messages": original, "metadata": {}},
                    config={"configurable": {"thread_id": "compact-test"}},
                )
            finally:
                app.checkpointer.conn.close()

        payload = post.call_args.kwargs["json"]
        self.assertIn("早期对话已为本次模型调用压缩", payload["messages"][0]["content"])
        self.assertEqual(result["messages"][:len(original)], original)
        self.assertEqual(result["messages"][-1]["content"], "已继续")


if __name__ == "__main__":
    unittest.main()
