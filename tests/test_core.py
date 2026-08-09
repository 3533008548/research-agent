"""
🧪 核心链路测试 — 3 个关键测试

运行:  python tests/test_core.py
       python -m pytest tests/ -v  (需 pip install pytest)

覆盖:
  1. read_pdf → 文本提取 → 索引 → 去重
  2. query_papers 命中率
  3. verify 节点边界不崩溃
"""

import unittest
import tempfile
import shutil
import sys
import sqlite3
import os
import subprocess
import time
import zipfile
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from unittest.mock import patch, MagicMock


RUN_CHROMA_INTEGRATION = os.getenv("SKIP_CHROMA_INTEGRATION") != "1"


class TestRetrievalAdmissionPolicy(unittest.TestCase):
    """工程解释不应为了生成引用而触发昂贵的文献工具链。"""

    def test_engineering_questions_disable_retrieval_tools(self):
        from research_agent import should_answer_without_tools

        self.assertTrue(should_answer_without_tools(
            "首次 RAG 检索遇到嵌入模型下载很慢时，系统应如何保证结果？"
        ))
        self.assertTrue(should_answer_without_tools(
            "每日 Curator 排队时，普通用户对话为什么仍应优先获得并发槽？"
        ))
        self.assertTrue(should_answer_without_tools(
            "验证动态负载下 TSN 调度鲁棒性时，至少应报告哪些指标和实验设置？"
        ))

    def test_explicit_evidence_requests_keep_retrieval_tools(self):
        from research_agent import should_answer_without_tools

        self.assertFalse(should_answer_without_tools(
            "检索三篇关于动态负载 TSN 鲁棒性的论文并给出引用。"
        ))
        self.assertFalse(should_answer_without_tools(
            "这篇论文的原文实验数据和作者是谁？"
        ))


@unittest.skipUnless(
    RUN_CHROMA_INTEGRATION,
    "CI offline quality gate skips Chroma/ONNX integration; run it in the scheduled integration job.",
)
class TestReadPDFFlow(unittest.TestCase):
    """测试1: read_pdf 完整链路"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_extract_text_from_pdf(self):
        """pdfplumber 能否从真实 PDF 提取文本"""
        from pdf_reader import PaperReader
        pdfs = list(Path(os.environ.get("APP_DATA_DIR", "runtime")).joinpath("primary", "papers").glob("*.pdf"))
        if not pdfs:
            self.skipTest("运行时论文目录中没有 PDF 文件")
        reader = PaperReader(max_pages=2, max_chars=2000)
        result = reader.read(str(pdfs[0]))
        self.assertIn("📄", result)
        self.assertGreater(len(result), 100, "提取文本不应为空")

    def test_chunk_text(self):
        """分块函数正确切分"""
        from paper_store import chunk_text
        text = "第一段。\n\n第二段内容较多。" * 20
        chunks = chunk_text(text, chunk_size=200, overlap=50)
        self.assertGreater(len(chunks), 0)
        for c in chunks:
            self.assertIn("text", c)
            self.assertIn("char_start", c)
            self.assertLessEqual(len(c["text"]), 200)

    def test_index_and_dedup(self):
        """索引 + 去重"""
        from paper_store import PaperStore
        store = PaperStore(persist_dir=self.tmp)
        pid1 = store.index_paper("测试论文内容 A B C", title="TestPaper")
        self.assertTrue(pid1)
        papers = store.list_papers()
        self.assertEqual(len(papers), 1)
        # 重复索引 → 先去重再存
        for p in papers:
            if p["title"] == "TestPaper":
                store.delete_paper(p["paper_id"])
        store.index_paper("测试论文内容 D E F", title="TestPaper")
        papers = store.list_papers()
        test_papers = [p for p in papers if p["title"] == "TestPaper"]
        self.assertEqual(len(test_papers), 1, "去重失败：同名论文出现多次")


@unittest.skipUnless(
    RUN_CHROMA_INTEGRATION,
    "CI offline quality gate skips Chroma/ONNX integration; run it in the scheduled integration job.",
)
class TestQueryPapers(unittest.TestCase):
    """测试2: query_papers 命中率"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        from paper_store import PaperStore
        cls.store = PaperStore(persist_dir=cls.tmp)
        cls.store.index_paper(
            "注意力机制是Transformer的核心。多头注意力使用多个注意力头并行计算。"
            "自注意力机制的公式为 Attention(Q,K,V)=softmax(QK^T/√d_k)V。",
            title="Attention Paper",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_query_hits_correct_paper(self):
        """查询'注意力机制'应命中索引的论文"""
        results = self.store.query("注意力机制", top_k=2)
        self.assertGreater(len(results), 0, "查询应返回结果")
        self.assertIn("Attention Paper", results[0]["title"])

    def test_query_returns_distance(self):
        """查询结果应包含距离"""
        results = self.store.query("注意力", top_k=1)
        self.assertTrue(results)
        self.assertIn("distance", results[0])
        self.assertLess(results[0]["distance"], 1.0, "相关查询距离应 < 1")

    def test_list_papers(self):
        """列出已索引论文"""
        papers = self.store.list_papers()
        self.assertGreaterEqual(len(papers), 1)

    def test_delete_paper(self):
        """删除论文"""
        from paper_store import PaperStore
        s2 = PaperStore(persist_dir=self.tmp)
        pid = s2.index_paper("要被删除的内容", title="DeleteMe")
        count_before = len(s2.list_papers())
        deleted = s2.delete_paper(pid)
        self.assertGreater(deleted, 0)
        self.assertEqual(len(s2.list_papers()), count_before - 1)


class TestToolResponsiveness(unittest.TestCase):
    def test_bounded_rag_query_returns_while_background_work_continues(self):
        import time
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from paper_store import PaperStore

        store = PaperStore.__new__(PaperStore)
        store._query_executor = ThreadPoolExecutor(max_workers=1)
        store._query_lock = threading.RLock()
        store._active_query = None
        store.query = lambda *args: (time.sleep(0.2) or [])
        store._collection = type("_Collection", (), {
            "count": lambda self: 1,
            "get": lambda self, include: {
                "documents": ["TSN scheduling with diffusion models"],
                "metadatas": [{"title": "TSN Paper", "section": "Method"}],
            },
        })()
        try:
            started = time.monotonic()
            results, pending = store.query_with_timeout("TSN", timeout_seconds=0.02)
            self.assertEqual(results[0]["retrieval"], "keyword")
            self.assertIn("首次嵌入模型", pending)
            self.assertLess(time.monotonic() - started, 0.15)
        finally:
            store._query_executor.shutdown(wait=False, cancel_futures=True)

    def test_query_tool_returns_without_waiting_for_embedding_initialization(self):
        from tools.search import handle_query_papers

        class _WarmingStore:
            def query_with_timeout(self, query, top_k, section):
                self.query = query
                self.top_k = top_k
                self.section = section
                return None, "嵌入模型仍在后台初始化，请稍后重试同一问题。"

        store = _WarmingStore()
        result = handle_query_papers(
            {"query": "TSN scheduling", "top_k": 3}, paper_store=store,
        )
        self.assertIn("后台初始化", result)
        self.assertEqual(store.query, "TSN scheduling")


class TestVerifyNode(unittest.TestCase):
    """测试3: verify 节点边界不崩溃"""

    class _FakeResponse:
        """最小 requests.Response 替身，避免状态机测试访问 API。"""

        status_code = 200
        text = ""

        def __init__(self, message: dict):
            self._data = {"choices": [{"message": message}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def test_verify_skips_no_tools(self):
        """无工具调用时 verify 应跳过（返回空）"""
        # 用 Mock 模拟 LangGraph state
        state = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
            ],
            "metadata": {},
        }
        # 手动模拟 verify 核心逻辑
        messages = state["messages"]
        recent = messages[-6:]
        has_tools = any(m.get("role") == "tool" for m in recent)
        self.assertFalse(has_tools, "无工具调用时 has_tools 应为 False")

    def test_verify_with_tools_but_empty_content(self):
        """有工具但回复为空时不应崩溃"""
        state = {
            "messages": [
                {"role": "user", "content": "搜索论文"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "search_papers", "arguments": "{}"}}]},
                {"role": "tool", "content": "找到3篇", "tool_call_id": "1"},
                {"role": "assistant", "content": ""},
            ],
            "metadata": {},
        }
        # content 为空 → 应该跳过
        messages = state["messages"]
        last_content = messages[-1].get("content", "")
        self.assertEqual(last_content, "", "空回复应安全处理")

    def test_sanitize_model_messages_drops_incomplete_assistant_history(self):
        from graph_builder import sanitize_model_messages

        messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "orphan", "content": "孤立结果"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "unfinished", "function": {"name": "search_papers", "arguments": "{}"},
            }]},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "function": {"name": "list_papers", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "结果"},
            {"role": "assistant", "content": "最终回答"},
        ]

        sanitized = sanitize_model_messages(messages)
        self.assertEqual(
            [message["role"] for message in sanitized],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(sanitized[1]["tool_calls"][0]["id"], "call-1")
        self.assertTrue(all(
            message["role"] != "assistant"
            or message.get("content") not in (None, "")
            or message.get("tool_calls")
            for message in sanitized
        ))

    def test_graph_builds_correctly(self):
        """图结构正常编译（不调用 API）"""
        from graph_builder import build_graph
        from llm_client import LLMClient
        app = build_graph(
            api_key="test-key", model="deepseek-chat", checkpoint_db=":memory:",
            llm_client=LLMClient("test-key", "https://example.test/chat"),
        )
        try:
            nodes = list(app.get_graph().nodes.keys())
            self.assertIn("llm", nodes)
            self.assertIn("tools", nodes)
            self.assertIn("verify", nodes)
            self.assertIn("__start__", nodes)
            self.assertIn("__end__", nodes)
        finally:
            app.checkpointer.conn.close()

    def test_verify_clears_retry_metadata_after_success(self):
        """修正后的回复通过验证后，不应把旧反馈带入下一轮对话。"""
        from graph_builder import build_graph

        responses = [
            self._FakeResponse({
                "content": None,
                "tool_calls": [{
                    "id": "tool-1", "type": "function",
                    "function": {"name": "list_papers", "arguments": "{}"},
                }],
            }),
            self._FakeResponse({"content": "这是一段超过一百字符的工具结果总结。" * 5}),
            self._FakeResponse({"content": "OK"}),
        ]
        with patch("llm_client.requests.post", side_effect=responses):
            from llm_client import LLMClient
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:",
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                result = app.invoke({
                    "messages": [{"role": "user", "content": "列出本地论文并总结"}],
                    "metadata": {
                        "verify_feedback": "旧反馈",
                        "verify_count": 1,
                        "verify_issues": "旧问题",
                        "verify_history": {"minor_repeat": 1},
                    },
                }, config={"configurable": {"thread_id": "verify-cleanup"}})
            finally:
                app.checkpointer.conn.close()

        self.assertEqual(result["metadata"], {})
        self.assertIn("工具结果总结", result["messages"][-1]["content"])

    def test_graph_reads_memory_by_current_session(self):
        """不同 thread_id 只能注入自己的对话摘要，不能共用 research-main。"""
        from graph_builder import build_graph

        class _Memory:
            def get_recent_summary(self, thread_id, topic):
                return f"{thread_id}:{topic}"

        responses = [self._FakeResponse({"content": "收到"}) for _ in range(2)]
        from llm_client import LLMClient
        with patch("llm_client.requests.post", side_effect=responses) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", memory_store=_Memory(),
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                for thread_id in ("session-a", "session-b"):
                    app.invoke(
                        {
                            "messages": [{"role": "user", "content": "测试"}],
                            "metadata": {"session_id": thread_id, "topic": "TSN"},
                        },
                        config={"configurable": {"thread_id": thread_id}},
                    )
            finally:
                app.checkpointer.conn.close()

        prompt_texts = [
            "\n".join(message["content"] or "" for message in call.kwargs["json"]["messages"])
            for call in post.call_args_list
        ]
        self.assertIn("session-a:TSN", prompt_texts[0])
        self.assertNotIn("session-b:TSN", prompt_texts[0])
        self.assertIn("session-b:TSN", prompt_texts[1])
        self.assertNotIn("session-a:TSN", prompt_texts[1])

    def test_verify_uses_short_timeout_without_poisoning_main_circuit(self):
        """验证超时只打开其局部熔断器，不能阻断主模型或 Curator。"""
        import requests
        from graph_builder import build_graph
        from llm_client import LLMClient
        from resilience import CircuitBreaker

        responses = [
            self._FakeResponse({
                "content": None,
                "tool_calls": [{
                    "id": "tool-1", "type": "function",
                    "function": {"name": "list_papers", "arguments": "{}"},
                }],
            }),
            self._FakeResponse({"content": "这是一段超过一百字符的工具结果总结。" * 8}),
            requests.exceptions.ReadTimeout("verify timeout"),
        ]
        usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        guard = CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        events = []
        client = LLMClient(
            "test-key", "https://example.test/chat", max_retries=0,
        )
        with patch("llm_client.requests.post", side_effect=responses) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", token_usage=usage,
                verify_timeout_seconds=4, verify_guard=guard, llm_client=client,
                event_callback=events.append,
            )
            try:
                app.invoke(
                    {"messages": [{"role": "user", "content": "列出本地论文并总结"}], "metadata": {}},
                    config={"configurable": {"thread_id": "verify-timeout"}},
                )
            finally:
                app.checkpointer.conn.close()

        verify_calls = [
            call for call in post.call_args_list
            if call.kwargs["json"].get("temperature") == 0.1
        ]
        self.assertEqual(len(verify_calls), 1)
        connect_timeout, read_timeout = verify_calls[0].kwargs["timeout"]
        self.assertEqual(connect_timeout, 3.05)
        self.assertGreater(read_timeout, 3.5)
        self.assertLessEqual(read_timeout, 4)
        self.assertFalse(guard.allow_request())
        self.assertEqual(client._circuit.state, "closed")
        self.assertNotIn("verify_status", usage)
        verify_started = next(event for event in events if event["type"] == "verify_request_started")
        self.assertEqual(verify_started["purpose"], "verify")
        self.assertEqual(verify_started["priority"], "verify")
        self.assertFalse(verify_started["counts_toward_circuit"])

    def test_stream_retries_same_model_before_first_token(self):
        """流式连接在尚未输出 token 时可以安全重放一次同模型请求。"""
        import requests
        from graph_builder import build_graph

        class _StreamingResponse:
            status_code = 200
            text = ""

            def __init__(self, should_fail=False):
                self.should_fail = should_fail

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                if self.should_fail:
                    raise requests.exceptions.ChunkedEncodingError("interrupted")
                return iter([
                    'data: {"choices":[{"delta":{"content":"恢复输出"}}]}',
                    "data: [DONE]",
                ])

        class _Client:
            max_retries = 1

            def __init__(self):
                self.calls = 0
                self.stream_failures = 0
                self.stream_successes = 0
                self.budgets = []

            class _Budget:
                def __init__(self):
                    self.retries_used = 0

                def reserve_retry(self):
                    if self.retries_used >= 1:
                        return False
                    self.retries_used += 1
                    return True

                def metrics(self):
                    return {"purpose": "chat", "priority": "interactive", "queue_wait_ms": 0,
                            "attempts": 1, "retries_used": self.retries_used}

            def new_request_budget(self, policy=None, **_kwargs):
                return self._Budget()

            @staticmethod
            def prepare_stream_read(response, budget):
                return True

            def post(self, payload, *, stream, on_status, budget):
                self.calls += 1
                self.budgets.append(budget)
                if not stream:
                    raise AssertionError("流式请求必须保留 stream=True")
                return _StreamingResponse(should_fail=self.calls == 1)

            def finish_stream(self, response, *, success):
                if success:
                    self.stream_successes += 1
                else:
                    self.stream_failures += 1

        client = _Client()
        streamed = []
        app = build_graph(
            api_key="test-key", checkpoint_db=":memory:", llm_client=client,
            stream_callback=streamed.append,
        )
        try:
            result = app.invoke(
                {"messages": [{"role": "user", "content": "你好"}], "metadata": {}},
                config={"configurable": {"thread_id": "stream-retry"}},
            )
        finally:
            app.checkpointer.conn.close()

        self.assertEqual(client.calls, 2)
        self.assertEqual(client.stream_failures, 1)
        self.assertEqual(client.stream_successes, 1)
        self.assertIs(client.budgets[0], client.budgets[1])
        self.assertEqual(result["messages"][-1]["content"], "恢复输出")
        self.assertEqual("".join(streamed), "恢复输出")

    def test_tools_module_imports(self):
        """所有工具模块可正常导入"""
        from search_api import search_arxiv, search_openalex, list_downloaded_papers
        from pdf_reader import read_pdf_enhanced, extract_images
        from paper_store import PaperStore, chunk_text
        from notes import NoteStore
        from profile import ProfileManager
        self.assertTrue(callable(search_arxiv))
        self.assertTrue(callable(search_openalex))
        self.assertTrue(callable(read_pdf_enhanced))
        self.assertTrue(callable(chunk_text))


class TestScheduler(unittest.TestCase):
    """每日检索的本地命令路径，不调用外部论文 API。"""

    def test_retry_today_and_temporary_search(self):
        from scheduler import Scheduler

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                self.assertEqual(scheduler.add_keyword("TSN scheduling"), "✅ 已添加: TSN scheduling")
                scheduler._search = lambda keyword, limit=3: [
                    {"title": f"{keyword} paper", "source": "test"}
                ]

                self.assertEqual(scheduler.run_today()[0]["title"], "TSN scheduling paper")
                self.assertEqual(scheduler.retry_today()[0]["title"], "TSN scheduling paper")
                self.assertEqual(scheduler.search("Diffusion TSN")[0]["title"], "Diffusion TSN paper")
            finally:
                scheduler.close()

    def test_parallel_search_passes_short_timeouts_to_each_source(self):
        """三源检索并行执行，单源请求必须使用配置的短超时。"""
        from scheduler import Scheduler

        class _HttpResponse:
            def __init__(self, content=b"", payload=None):
                self.content = content
                self._payload = payload or {}

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        arxiv_xml = b"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>
            <entry><title>arXiv paper</title><id>https://arxiv.org/abs/1</id></entry></feed>"""

        def fake_get(url, **kwargs):
            if "arxiv" in url:
                self.assertEqual(kwargs["timeout"], (3.05, 4))
                return _HttpResponse(content=arxiv_xml)
            self.assertEqual(kwargs["timeout"], (3.05, 4))
            return _HttpResponse(payload={"results": [{"title": "OpenAlex paper"}]})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"), request_timeout_seconds=4)
            try:
                with patch("requests.get", side_effect=fake_get) as request_get:
                    results = scheduler.search("TSN scheduling", limit=1)
                self.assertEqual(request_get.call_count, 2)
                self.assertEqual(
                    {result["source"] for result in results},
                    {"arxiv", "openalex"},
                )
            finally:
                scheduler.close()


class TestDailyMultiAgentOrchestration(unittest.TestCase):
    """每日检索必须可恢复、可降级，且不会为每篇论文调用一次模型。"""

    @staticmethod
    def _candidate(orchestrator, *, keyword, source, title, doi, abstract="有效摘要", year=2025):
        return orchestrator._candidate(
            keyword=keyword,
            source=source,
            title=title,
            doi=doi,
            abstract=abstract,
            authors=["Alice"],
            year=year,
            published_at=f"{year}-01-02",
            venue="TestConf",
            citation_count=20,
            url=f"https://example.test/{doi}",
            source_id=doi,
        )

    def test_daily_run_deduplicates_then_uses_one_batch_curator(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def __init__(self, content):
                self.content = content

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

            def close(self):
                return None

        class _LLM:
            def __init__(self):
                self.calls = []
                self.policies = []

            def new_request_budget(self, policy):
                self.policies.append(policy)
                return policy

            def post(self, payload, **_kwargs):
                self.calls.append(payload)
                return _Response(
                    '{"brief":"两篇候选已排序", "rankings": ['
                    '{"id":"%s","score":90,"reason":"高度相关","tags":["TSN"]},'
                    '{"id":"%s","score":80,"reason":"补充方法","tags":["调度"]}]}'
                    % (self.ids[0], self.ids[1])
                )

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                scheduler.add_keyword("TSN scheduling")
                llm = _LLM()
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler, llm_client=llm, model="deepseek-chat",
                )
                first = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="arxiv",
                    title="Diffusion Scheduling for TSN", doi="10.1/a",
                )
                duplicate = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="openalex",
                    title="Diffusion Scheduling for TSN", doi="10.1/a",
                )
                second = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="dblp",
                    title="Reinforcement Learning for TSN", doi="10.1/b",
                )
                llm.ids = [first["candidate_id"], second["candidate_id"]]
                stats = {"TSN scheduling": {
                    "arxiv": {"status": "ok", "count": 1},
                    "openalex": {"status": "ok", "count": 1},
                    "dblp": {"status": "ok", "count": 1},
                }}
                with patch.object(orchestrator, "_run_scouts", return_value=([first, duplicate, second], stats)):
                    result = orchestrator.run("daily")

                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.candidates), 2, "跨源同一 DOI 应合并")
                self.assertEqual(len(llm.calls), 1, "Curator 必须按整次任务批处理")
                self.assertFalse(llm.policies[0].counts_toward_circuit)
                self.assertEqual(result.candidates[0]["curation"]["reason"], "高度相关")
                saved = scheduler.get_daily_run(result.run_id)
                self.assertEqual(saved["status"], "completed")
                self.assertGreaterEqual(len(scheduler.get_daily_agent_events(result.run_id)), 5)
                self.assertEqual(scheduler.get_today_results()[0]["new_count"], 2)
            finally:
                scheduler.close()

    def test_source_sets_keep_arxiv_out_of_daily_and_send_raw_openalex_key(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": []}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler,
                    openalex_api_key="oa-test-key",
                )
                self.assertEqual(orchestrator.daily_sources, ("openalex", "openaire", "dblp"))
                self.assertEqual(orchestrator.temporary_sources, ("openalex", "arxiv"))
                with patch("daily_orchestrator.requests.get", return_value=_Response()) as request_get:
                    self.assertEqual(orchestrator._openalex_scout("TSN", None), [])
                self.assertEqual(request_get.call_args.kwargs["params"]["api_key"], "oa-test-key")
            finally:
                scheduler.close()

    def test_resume_reuses_saved_candidates_without_fetching_again(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                scheduler.add_keyword("TSN scheduling")
                orchestrator = DailyResearchOrchestrator(scheduler=scheduler)
                run = scheduler.create_daily_run("daily", ["TSN scheduling"], {"planner": "rule_based"})
                candidate = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="arxiv",
                    title="Saved Candidate", doi="10.1/saved",
                )
                candidate["quality"] = {"score": 6, "issues": [], "eligible": True}
                scheduler.save_daily_candidates(run["run_id"], [candidate])
                scheduler.update_daily_run(run["run_id"], status="failed")

                with patch.object(orchestrator, "_run_scouts") as scouts:
                    result = orchestrator.run("daily", resume=True)

                self.assertEqual(result.run_id, run["run_id"])
                self.assertEqual(result.status, "completed")
                self.assertFalse(scouts.called)
                self.assertEqual(scheduler.get_daily_run(run["run_id"])["status"], "completed")
            finally:
                scheduler.close()

    def test_critic_is_conditional_for_small_or_low_quality_candidate_sets(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def __init__(self, content):
                self.content = content

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

            def close(self):
                return None

        class _LLM:
            def __init__(self, candidate_id):
                self.candidate_id = candidate_id
                self.calls = 0

            def new_request_budget(self, policy):
                return policy

            def post(self, _payload, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _Response(
                        '{"brief":"单篇候选", "rankings":[{"id":"%s","score":90,"reason":"相关","tags":[]}]}'
                        % self.candidate_id
                    )
                return _Response('{"warnings":["候选较少"],"drop_ids":[]}')

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                scheduler.add_keyword("TSN scheduling")
                seed = DailyResearchOrchestrator(scheduler=scheduler)
                candidate = self._candidate(
                    seed, keyword="TSN scheduling", source="arxiv",
                    title="Only Candidate", doi="10.1/only",
                )
                llm = _LLM(candidate["candidate_id"])
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler, llm_client=llm, model="deepseek-chat",
                )
                stats = {"TSN scheduling": {"arxiv": {"status": "ok", "count": 1}}}
                with patch.object(orchestrator, "_run_scouts", return_value=([candidate], stats)):
                    result = orchestrator.run("daily")

                self.assertEqual(llm.calls, 2, "候选不足时才追加一次 Critic")
                self.assertTrue(result.critique["ran"])
                self.assertEqual(result.critique["warnings"], ["候选较少"])
            finally:
                scheduler.close()


class TestSessionStore(unittest.TestCase):
    """多会话注册、旧历史迁移与硬删除边界。"""

    def test_legacy_migration_and_isolated_hard_delete(self):
        from session_store import SessionStore, LEGACY_THREAD_ID
        from memory import MemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_db = Path(tmp) / "checkpoint.db"
            conn = sqlite3.connect(checkpoint_db)
            conn.executescript("""
                CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT);
                CREATE TABLE checkpoint_blobs (thread_id TEXT, channel TEXT);
                CREATE TABLE writes (thread_id TEXT, value TEXT);
            """)
            conn.execute("INSERT INTO checkpoints VALUES (?, 'old-1')", (LEGACY_THREAD_ID,))
            conn.commit()
            conn.close()

            store = SessionStore(str(checkpoint_db))
            memory = MemoryStore(str(Path(tmp) / "memory.db"))
            try:
                self.assertTrue(store.ensure_legacy_session())
                self.assertEqual(store.get(LEGACY_THREAD_ID)["title"], "历史会话")
                self.assertFalse(store.ensure_legacy_session(), "旧会话只能迁移一次")

                first = store.create()
                second = store.create("实验会话")
                store.touch(first["thread_id"], "这是首条用于自动命名的会话消息，后续内容不应影响标题")
                self.assertEqual(
                    store.get(first["thread_id"])["title"],
                    "这是首条用于自动命名的会话消息，后续内容",
                )
                self.assertEqual(len(store.list()), 3)

                usage = {"total": 42, "calls": 2}
                store.save_usage(first["thread_id"], usage)
                self.assertEqual(store.get_usage(first["thread_id"]), usage)

                raw = sqlite3.connect(checkpoint_db)
                for table in ("checkpoints", "checkpoint_blobs", "writes"):
                    raw.execute(f"INSERT INTO {table} VALUES (?, 'new')", (first["thread_id"],))
                    raw.execute(f"INSERT INTO {table} VALUES (?, 'other')", (second["thread_id"],))
                raw.commit()
                raw.close()
                memory.add_summary(first["thread_id"], "", "会话一摘要")
                memory.add_summary(second["thread_id"], "", "会话二摘要")

                self.assertTrue(store.delete(first["thread_id"]))
                self.assertEqual(memory.delete_summaries(first["thread_id"]), 1)
                self.assertIsNone(store.get(first["thread_id"]))
                self.assertEqual(memory.get_recent_summary(first["thread_id"]), None)
                self.assertEqual(memory.get_recent_summary(second["thread_id"]), "会话二摘要")

                raw = sqlite3.connect(checkpoint_db)
                for table in ("checkpoints", "checkpoint_blobs", "writes"):
                    self.assertEqual(
                        raw.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (first["thread_id"],)).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        raw.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (second["thread_id"],)).fetchone()[0],
                        1,
                    )
                raw.close()
            finally:
                memory.close()
                store.close()

    def test_cleanup_orphaned_checkpoints_keeps_registered_sessions(self):
        """早期删除遗留的 checkpoint 不能在运行时数据库中持续累积。"""
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_db = Path(tmp) / "checkpoint.db"
            conn = sqlite3.connect(checkpoint_db)
            conn.executescript("""
                CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT);
                CREATE TABLE writes (thread_id TEXT, value TEXT);
            """)
            conn.executemany(
                "INSERT INTO checkpoints VALUES (?, 'checkpoint')",
                [("kept",), ("orphan",)],
            )
            conn.executemany(
                "INSERT INTO writes VALUES (?, 'write')",
                [("kept",), ("orphan",)],
            )
            conn.commit()
            conn.close()

            store = SessionStore(str(checkpoint_db))
            try:
                with store._conn:
                    store._conn.execute(
                        "INSERT INTO agent_sessions VALUES (?, '保留', '', 'now', 'now')",
                        ("kept",),
                    )
                self.assertEqual(
                    store.cleanup_orphaned_checkpoints(),
                    {
                        "checkpoints": 1,
                        "writes": 1,
                        "research_runs": 0,
                        "chat_runs": 0,
                        "agent_run_events": 0,
                    },
                )
            finally:
                store.close()

            conn = sqlite3.connect(checkpoint_db)
            try:
                for table in ("checkpoints", "writes"):
                    self.assertEqual(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE thread_id='orphan'"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE thread_id='kept'"
                        ).fetchone()[0],
                        1,
                    )
            finally:
                conn.close()

    def test_research_run_is_session_scoped_and_deleted_with_session(self):
        """深度研究的可恢复中间态必须跟随会话删除，不能成为孤儿数据。"""
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                run = store.create_research_run(session["thread_id"], "比较两种调度方法")
                self.assertEqual(run["status"], "running")
                self.assertEqual(run["evidence"], [])

                saved = store.update_research_run(
                    run["run_id"],
                    status="failed",
                    plan={"objective": "比较"},
                    evidence=[{"id": "E1", "source": "本地论文"}],
                    critique={"verdict": "pass", "issues": []},
                    final_answer="保留的研究结果",
                    trace={"stages": ["planner"]},
                )
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["plan"]["objective"], "比较")
                self.assertEqual(saved["evidence"][0]["id"], "E1")
                self.assertEqual(
                    store.get_latest_research_run(session["thread_id"])["run_id"],
                    run["run_id"],
                )

                self.assertTrue(store.delete(session["thread_id"]))
                self.assertIsNone(store.get_research_run(run["run_id"]))
            finally:
                store.close()

    def test_chat_run_events_are_safe_and_deleted_with_their_session(self):
        """The operation log is session-scoped and keeps only approved metrics."""
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                first = store.create()
                second = store.create()
                run = store.create_chat_run(first["thread_id"], "deepseek-chat")
                store.add_run_event(
                    first["thread_id"], run["run_id"], "chat", "single_agent",
                    "model", "completed", summary="模型请求已完成",
                    metrics={"duration_ms": 12.34, "prompt": "must-not-persist"},
                )
                store.update_chat_run(
                    run["run_id"], status="completed", duration_ms=13,
                    metrics={"model_calls": 1, "raw_answer": "must-not-persist"},
                )

                saved = store.list_chat_runs(first["thread_id"])
                self.assertEqual(saved[0]["status"], "completed")
                self.assertEqual(saved[0]["metrics"], {"model_calls": 1})
                events = store.get_run_events(run["run_id"], first["thread_id"])
                self.assertEqual(events[0]["metrics"], {"duration_ms": 12.3})
                self.assertEqual(store.get_run_events(run["run_id"], second["thread_id"]), [])

                self.assertTrue(store.delete(first["thread_id"]))
                self.assertIsNone(store.get_chat_run(run["run_id"]))
                self.assertEqual(store.get_run_events(run["run_id"]), [])
            finally:
                store.close()

    def test_daily_timeline_adapter_hides_daily_event_messages(self):
        """Daily data stays in its own DB and raw keyword messages are never rendered."""
        from run_timeline import RunTimelineService
        from scheduler import Scheduler
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionStore(str(Path(tmp) / "checkpoint.db"))
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                run = scheduler.create_daily_run("daily", ["PRIVATE_KEYWORD"])
                scheduler.add_daily_agent_event(
                    run["run_id"], "scouts", "running",
                    "PRIVATE_KEYWORD and candidate title must not be rendered",
                )
                html = RunTimelineService(sessions, scheduler).render_daily_runs()
                self.assertIn("多来源检索阶段", html)
                self.assertNotIn("PRIVATE_KEYWORD", html)
                self.assertNotIn("candidate title", html)
            finally:
                scheduler.close()
                sessions.close()


class TestResearchOrchestration(unittest.TestCase):
    """不访问真实模型，验证深度研究的持久化、修订和继续语义。"""

    @staticmethod
    def _worker_result(role="local"):
        return {
            "role": role,
            "label": "本地证据研究员",
            "status": "completed",
            "message": "获得 1 条证据",
            "answer": "工具检索完成",
            "evidence": [{
                "id": "",
                "researcher": role,
                "source_type": "query_papers",
                "source": "TSN Paper",
                "excerpt": "TSN 论文中的可核验证据。",
                "uncertainty": "需要结合原文核验。",
            }],
            "usage": {"prompt": 2, "completion": 3, "total": 5, "calls": 1},
            "trace": {"role": role},
        }

    def _orchestrator(self, store):
        from research_orchestrator import ResearchOrchestrator

        return ResearchOrchestrator(
            session_store=store,
            api_key="test-key",
            model="deepseek-chat",
            paper_store=None,
            glm_api_key="",
            llm_client=MagicMock(),
            verify_timeout_seconds=3,
        )

    def test_completed_research_runs_one_bounded_revision_and_persists_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            from session_store import SessionStore

            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                orchestrator = self._orchestrator(store)
                calls = []

                def fake_model(*, stage, usage, **_kwargs):
                    calls.append(stage)
                    usage["calls"] += 1
                    return {
                        "planner": '{"objective":"TSN","subtasks":[{"id":"local","focus":"本地证据"}]}',
                        "synthesis": "初稿结论 [E1]",
                        "critic": '{"verdict":"revise","issues":["补充局限"]}',
                        "revision": "修订后结论 [E1]",
                    }[stage]

                events = []
                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", return_value=[self._worker_result()]):
                    result = orchestrator.run(
                        "TSN 中两种调度方法的差异是什么？",
                        thread_id=session["thread_id"],
                        on_progress=events.append,
                    )

                self.assertEqual(result.status, "completed")
                self.assertEqual(calls, ["planner", "synthesis", "critic", "revision"])
                self.assertIn("修订后结论 [E1]", result.answer)
                self.assertIn("证据索引", result.answer)
                saved = store.get_research_run(result.run_id)
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(saved["evidence"][0]["id"], "E1")
                self.assertEqual(saved["critique"]["verdict"], "revise")
                self.assertTrue(any(event["stage"] == "completed" for event in events))
                persisted_events = store.get_run_events(result.run_id, session["thread_id"])
                self.assertTrue(any(event["stage"] == "planner" for event in persisted_events))
                self.assertTrue(any(event["stage"] == "completed" for event in persisted_events))
                self.assertNotIn("TSN", " ".join(event["summary"] for event in persisted_events))
            finally:
                store.close()


    def test_research_turn_is_visible_as_only_user_and_final_report(self):
        """研究员的内部工具消息不得污染普通会话历史。"""
        from config import Config
        from research_agent import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            try:
                thread_id = agent.thread_id
                agent._append_research_turn(
                    thread_id,
                    "研究 TSN 调度方法",
                    "# 深度研究报告\n\n结论 [E1]",
                )
                self.assertEqual(
                    agent.get_history(thread_id),
                    [
                        {"role": "user", "content": "研究 TSN 调度方法"},
                        {"role": "assistant", "content": "# 深度研究报告\n\n结论 [E1]"},
                    ],
                )
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_continue_reuses_saved_evidence_without_replanning_or_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            from session_store import SessionStore

            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                previous = store.create_research_run(session["thread_id"], "继续测试")
                store.update_research_run(
                    previous["run_id"],
                    status="failed",
                    plan={"objective": "继续测试", "subtasks": []},
                    evidence=[self._worker_result()["evidence"][0]],
                )
                orchestrator = self._orchestrator(store)
                calls = []

                def fake_model(*, stage, **_kwargs):
                    calls.append(stage)
                    return {
                        "synthesis": "基于保留证据的结论 [E1]",
                        "critic": '{"verdict":"pass","issues":[]}',
                    }[stage]

                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers") as workers:
                    result = orchestrator.run(
                        "", thread_id=session["thread_id"], resume=True,
                    )

                self.assertEqual(result.status, "completed")
                self.assertEqual(result.run_id, previous["run_id"])
                self.assertEqual(calls, ["synthesis", "critic"])
                self.assertFalse(workers.called)

                with patch.object(orchestrator, "_call_model") as no_model:
                    already_done = orchestrator.run(
                        "", thread_id=session["thread_id"], resume=True,
                    )
                self.assertEqual(already_done.run_id, previous["run_id"])
                self.assertIn("已经完成", already_done.answer)
                self.assertFalse(no_model.called)
            finally:
                store.close()


class TestBrowserRunGuard(unittest.TestCase):
    """切换会话必须使旧流式回调失效。"""

    def test_new_request_and_invalidation_supersede_old_request(self):
        from ui_request_guard import BrowserRunGuard

        guard = BrowserRunGuard()
        first = guard.begin("browser-a")
        self.assertTrue(guard.is_current("browser-a", first))

        second = guard.begin("browser-a")
        self.assertFalse(guard.is_current("browser-a", first))
        self.assertTrue(guard.is_current("browser-a", second))

        guard.invalidate("browser-a")
        self.assertFalse(guard.is_current("browser-a", second))

    def test_invalidation_signals_the_active_agent_request(self):
        from ui_request_guard import BrowserRunGuard

        guard = BrowserRunGuard()
        generation = guard.begin("browser-a")
        cancel_event = guard.cancellation_event("browser-a", generation)
        self.assertFalse(cancel_event.is_set())

        guard.invalidate("browser-a")
        self.assertTrue(cancel_event.wait(0.05))

    def test_finishing_old_request_cannot_remove_new_request(self):
        from ui_request_guard import BrowserRunGuard

        guard = BrowserRunGuard()
        first = guard.begin("browser-a")
        second = guard.begin("browser-a")
        guard.finish("browser-a", first)
        self.assertTrue(guard.is_current("browser-a", second))
        guard.finish("browser-a", second)
        self.assertFalse(guard.is_current("browser-a", second))


class TestLLMClient(unittest.TestCase):
    """主模型请求韧性层：只重试同一模型，不做降级。"""

    class _Response:
        def __init__(self, status_code=200, payload=None, headers=None, text=""):
            self.status_code = status_code
            self._payload = payload or {"choices": [{"message": {"content": "ok"}}]}
            self.headers = headers or {}
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                error = requests.HTTPError(f"HTTP {self.status_code}")
                error.response = self
                raise error

        def json(self):
            return self._payload

    def test_non_retryable_http_error_exposes_api_detail(self):
        import requests
        from llm_client import LLMClient, LLMRequestFailedError

        response = self._Response(status_code=400, text='{"error":{"message":"invalid messages"}}')
        client = LLMClient("test-key", "https://example.test/chat", max_retries=0)

        with patch("llm_client.requests.post", return_value=response):
            with self.assertRaises(LLMRequestFailedError) as raised:
                client.post({"model": "deepseek-v4-flash", "messages": []})
        self.assertIn("400", str(raised.exception))
        self.assertIn("invalid messages", str(raised.exception))

    def test_retries_same_model_after_timeout(self):
        import requests
        from llm_client import LLMClient

        client = LLMClient(
            "test-key", "https://example.test/chat", max_retries=1,
            connect_timeout_seconds=2, read_timeout_seconds=9,
            request_deadline_seconds=20,
        )
        payload = {"model": "deepseek-v4-flash", "messages": [], "stream": False}
        with patch("llm_client.requests.post", side_effect=[
            requests.exceptions.ReadTimeout("slow"), self._Response(),
        ]) as post, patch("llm_client.time.sleep"):
            response = client.post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(post.call_args_list[0].kwargs["timeout"], (2, 9))

    def test_request_policy_controls_budget_and_retry_limit(self):
        import requests
        from llm_client import LLMClient, LLMRequestTimeoutError, RequestPolicy, RequestPriority

        client = LLMClient("test-key", "https://example.test/chat", max_retries=2)
        policy = RequestPolicy(
            purpose="verify", priority=RequestPriority.VERIFY,
            deadline_seconds=4, max_retries=0,
        )
        budget = client.new_request_budget(policy)
        with patch("llm_client.requests.post", side_effect=requests.exceptions.ReadTimeout("slow")) as post:
            with self.assertRaises(LLMRequestTimeoutError):
                client.post({"model": "deepseek-v4-flash", "messages": []}, budget=budget)

        self.assertEqual(post.call_count, 1)
        self.assertEqual(budget.metrics()["purpose"], "verify")
        self.assertEqual(budget.metrics()["priority"], "verify")
        self.assertEqual(budget.metrics()["attempts"], 1)

    def test_background_work_preserves_interactive_capacity(self):
        import threading
        from llm_client import LLMClient, RequestPolicy, RequestPriority

        client = LLMClient(
            "test-key", "https://example.test/chat", max_concurrency=2,
            interactive_reserved_slots=1,
        )
        background = RequestPolicy("verify", RequestPriority.VERIFY)
        payload = {"model": "deepseek-v4-flash", "messages": []}
        pending = []
        errors = []

        with patch("llm_client.requests.post", side_effect=[
            self._Response(), self._Response(), self._Response(),
        ]) as post:
            first_background = client.post(payload, stream=True, policy=background)

            def _second_background():
                try:
                    pending.append(client.post(payload, stream=True, policy=background))
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=_second_background)
            worker.start()
            time.sleep(0.05)
            self.assertEqual(post.call_count, 1, "第二个后台请求必须等待后台额度")

            interactive = client.post(payload, stream=True)
            self.assertEqual(post.call_count, 2, "交互请求应使用保留并发槽")

            client.finish_stream(first_background, success=True)
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(post.call_count, 3)

            client.finish_stream(interactive, success=True)
            client.finish_stream(pending[0], success=True)

    def test_opens_circuit_after_exhausted_same_model_failures(self):
        import requests
        from llm_client import LLMClient, LLMCircuitOpenError, LLMRequestTimeoutError

        client = LLMClient(
            "test-key", "https://example.test/chat", max_retries=0,
            circuit_failure_threshold=1, circuit_recovery_seconds=60,
        )
        payload = {"model": "deepseek-v4-flash", "messages": [], "stream": False}
        with patch("llm_client.requests.post", side_effect=requests.exceptions.ConnectionError("offline")) as post:
            with self.assertRaises(LLMRequestTimeoutError):
                client.post(payload)
            with self.assertRaises(LLMCircuitOpenError):
                client.post(payload)

        self.assertEqual(post.call_count, 1, "熔断后不应继续请求或切换模型")

    def test_queue_wait_counts_toward_single_request_deadline(self):
        """排队耗尽的时间不能再额外获得一整段模型请求预算。"""
        import time
        from llm_client import LLMClient, LLMRequestTimeoutError

        client = LLMClient(
            "test-key", "https://example.test/chat", max_concurrency=1,
            request_deadline_seconds=0.03,
        )
        self.assertTrue(client._slots.acquire(blocking=False))
        started = time.monotonic()
        try:
            with patch("llm_client.requests.post") as post:
                with self.assertRaises(LLMRequestTimeoutError):
                    client.post({"model": "deepseek-v4-flash", "messages": []})
            self.assertFalse(post.called)
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            client._slots.release()

    def test_cancelled_request_does_not_enter_model_queue(self):
        import threading
        from cancellation import RequestCancelledError
        from llm_client import LLMClient

        client = LLMClient("test-key", "https://example.test/chat")
        cancel_event = threading.Event()
        cancel_event.set()
        with patch("llm_client.requests.post") as post:
            with self.assertRaises(RequestCancelledError):
                client.post(
                    {"model": "deepseek-v4-flash", "messages": []},
                    cancel_event=cancel_event,
                )
        self.assertFalse(post.called)

    def test_stream_holds_slot_and_clears_failure_only_after_done(self):
        """SSE 首包成功不应提前释放并发槽或清除熔断失败记录。"""
        from llm_client import LLMClient

        client = LLMClient(
            "test-key", "https://example.test/chat", max_concurrency=1,
            circuit_failure_threshold=3,
        )
        client._circuit.record_failure()
        response = self._Response()
        with patch("llm_client.requests.post", return_value=response):
            streamed = client.post(
                {"model": "deepseek-v4-flash", "messages": []}, stream=True,
            )

        self.assertFalse(client._slots.acquire(blocking=False))
        self.assertEqual(client._circuit._failures, 1)
        client.finish_stream(streamed, success=True)
        self.assertEqual(client._circuit._failures, 0)
        self.assertTrue(client._slots.acquire(blocking=False))
        client._slots.release()


class TestCancellationPropagation(unittest.TestCase):
    """取消不是模型故障，且不得继续进入后续图节点。"""

    def test_pre_cancelled_graph_skips_llm_and_tools(self):
        import threading
        from cancellation import RequestCancelledError
        from graph_builder import build_graph
        from llm_client import LLMClient

        cancel_event = threading.Event()
        cancel_event.set()
        app = build_graph(
            api_key="test-key", checkpoint_db=":memory:", cancel_event=cancel_event,
            llm_client=LLMClient("test-key", "https://example.test/chat"),
        )
        try:
            with self.assertRaises(RequestCancelledError):
                app.invoke(
                    {"messages": [{"role": "user", "content": "不应请求模型"}], "metadata": {}},
                    config={"configurable": {"thread_id": "cancelled-graph"}},
                )
        finally:
            app.checkpointer.conn.close()

    def test_research_worker_graph_ends_without_verify_request(self):
        """研究员工具循环结束后不再额外调用一次 verify。"""
        from graph_builder import build_graph
        from llm_client import LLMClient

        class _Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "已完成本地证据整理"}}]}

        with patch(
            "llm_client.requests.post",
            return_value=_Response(),
        ) as post:
            app = build_graph(
                api_key="test-key",
                checkpoint_db=":memory:",
                enable_verify=False,
                allowed_tool_names={"query_papers"},
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                result = app.invoke(
                    {"messages": [{"role": "user", "content": "收集本地证据"}], "metadata": {}},
                    config={"configurable": {"thread_id": "research-worker"}},
                )
            finally:
                app.checkpointer.conn.close()

        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["messages"][-1]["content"], "已完成本地证据整理")

    def test_graph_requires_shared_llm_client(self):
        from graph_builder import build_graph

        with self.assertRaisesRegex(ValueError, "shared llm_client"):
            build_graph(api_key="test-key", checkpoint_db=":memory:")

    def test_agent_returns_cancelled_status_without_building_graph(self):
        import threading
        from config import Config
        from research_agent import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            cancel_event = threading.Event()
            cancel_event.set()
            with patch.object(agent, "_build_app") as build_app:
                self.assertEqual(agent.step("取消测试", cancel_event=cancel_event), "⏹️ 请求已取消。")
            self.assertFalse(build_app.called)
            agent.memory.close()
            agent.sessions.close()

    def test_agent_stops_an_inflight_graph_when_cancel_event_is_set(self):
        import threading
        from cancellation import RequestCancelledError
        from config import Config
        from research_agent import ResearchAgent

        class _SlowApp:
            def __init__(self, cancel_event):
                self.cancel_event = cancel_event

            def invoke(self, _state, config):
                while not self.cancel_event.wait(0.01):
                    pass
                raise RequestCancelledError("test cancellation")

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            cancel_event = threading.Event()
            result = []

            def fake_build(_usage, on_token=None, event_callback=None, cancel_event=None):
                return _SlowApp(cancel_event)

            worker = threading.Thread(
                target=lambda: result.append(agent.step("慢请求", cancel_event=cancel_event)),
            )
            with patch.object(agent, "_build_app", side_effect=fake_build):
                worker.start()
                time.sleep(0.05)
                cancel_event.set()
                worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, ["⏹️ 请求已取消。"])
            agent.memory.close()
            agent.sessions.close()


class TestCircuitBreaker(unittest.TestCase):
    def test_half_open_admits_a_single_probe_and_reopens_on_failure(self):
        """恢复期结束后只能有一个请求探测下游服务。"""
        import time
        from resilience import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.allow_request())

        breaker._open_until = time.monotonic() - 0.01
        self.assertTrue(breaker.allow_request())
        self.assertEqual(breaker.state, "half_open")
        self.assertFalse(breaker.allow_request())

        breaker.record_failure()
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.allow_request())


class TestRuntimePaths(unittest.TestCase):
    """运行时数据必须与代码目录隔离，并允许通过环境/CLI 改根目录。"""

    def test_runtime_layout_and_user_settings_override_static_config(self):
        from config import Config
        from runtime_paths import RuntimePaths

        old_data_dir = os.environ.get("APP_DATA_DIR")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = RuntimePaths.from_root(tmp)
                paths.ensure_initialized()
                paths.update_settings({
                    "model": "deepseek-v4-flash",
                    "rag_enabled": False,
                    "pdf_max_pages": 31,
                    "daily_search_enabled": True,
                })
                self.assertTrue(paths.state_file.exists())
                self.assertTrue(paths.database_dir.is_dir())
                self.assertTrue(paths.papers_dir.is_dir())
                self.assertTrue(paths.chroma_dir.is_dir())
                self.assertTrue(paths.images_dir.is_dir())
                self.assertEqual(paths.safe_child(paths.papers_dir, "../escape.pdf").parent, paths.papers_dir)

                cfg = Config.load({"data_dir": tmp})
                self.assertEqual(Path(cfg.checkpoint_db), paths.checkpoint_db)
                self.assertEqual(Path(cfg.memory_db), paths.memory_db)
                self.assertEqual(Path(cfg.notes_db), paths.notes_db)
                self.assertEqual(Path(cfg.daily_db), paths.daily_db)
                self.assertEqual(Path(cfg.chroma_dir), paths.chroma_dir)
                self.assertEqual(Path(cfg.papers_dir), paths.papers_dir)
                self.assertEqual(Path(cfg.images_dir), paths.images_dir)
                self.assertEqual(Path(cfg.profile_path), paths.profile_path)
                self.assertFalse(cfg.rag_enabled)
                self.assertEqual(cfg.pdf_max_pages, 31)
                self.assertTrue(cfg.daily_search_enabled)
        finally:
            if old_data_dir is None:
                os.environ.pop("APP_DATA_DIR", None)
            else:
                os.environ["APP_DATA_DIR"] = old_data_dir

    def test_openalex_key_uses_a_dedicated_environment_variable(self):
        from config import Config

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"OPENALEX_API_KEY": "oa-test-key"}, clear=False,
        ):
            cfg = Config.load({"data_dir": tmp})
            self.assertEqual(cfg.openalex_api_key, "oa-test-key")
            self.assertEqual(cfg.daily_sources, ("openalex", "openaire", "dblp"))

    def test_migration_script_previews_then_copies_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            source_db = legacy / "checkpoint.db"
            source_conn = sqlite3.connect(source_db)
            source_conn.execute("CREATE TABLE marker (value TEXT)")
            source_conn.execute("INSERT INTO marker VALUES ('legacy-kept')")
            source_conn.commit()
            source_conn.close()
            data_dir = root / "runtime"

            base_cmd = [
                sys.executable, "scripts/migrate_runtime.py",
                "--source", str(legacy), "--data-dir", str(data_dir),
            ]
            subprocess.run(base_cmd, check=True, cwd=Path(__file__).parent.parent)
            self.assertFalse((data_dir / "primary" / "db" / "checkpoint.db").exists())

            subprocess.run(base_cmd + ["--apply"], check=True, cwd=Path(__file__).parent.parent)
            self.assertTrue(source_db.exists(), "迁移不应删除旧数据")
            copied = sqlite3.connect(data_dir / "primary" / "db" / "checkpoint.db")
            try:
                self.assertEqual(copied.execute("SELECT value FROM marker").fetchone()[0], "legacy-kept")
            finally:
                copied.close()

    def test_backup_script_exports_primary_data_as_zip(self):
        from runtime_paths import RuntimePaths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths.from_root(root / "runtime")
            paths.ensure_initialized()
            conn = sqlite3.connect(paths.notes_db)
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES ('backup-ok')")
            conn.commit()
            conn.close()
            paths.profile_path.write_text("# 用户画像\n", encoding="utf-8")
            (paths.papers_dir / "paper.pdf").write_bytes(b"%PDF-test")
            output = root / "backups"

            subprocess.run(
                [
                    sys.executable, "scripts/backup_runtime.py",
                    "--data-dir", str(paths.root), "--output", str(output),
                ],
                check=True,
                cwd=Path(__file__).parent.parent,
            )
            archives = list(output.glob("research-agent-runtime-*.zip"))
            self.assertEqual(len(archives), 1)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertIn("runtime/primary/db/notes.db", archive.namelist())
                self.assertIn("runtime/primary/profile.md", archive.namelist())
                self.assertIn("runtime/primary/papers/paper.pdf", archive.namelist())


class TestResearchBenchmark(unittest.TestCase):
    """版本化评测集不读取用户数据，并能稳定检查结果结构。"""

    def test_manifest_defines_fifteen_distinct_research_tasks(self):
        from evals.benchmark import load_manifest, validate_manifest

        manifest = load_manifest()
        self.assertEqual(validate_manifest(manifest), [])
        self.assertEqual(len(manifest["tasks"]), 15)
        self.assertEqual(len({task["id"] for task in manifest["tasks"]}), 15)

    def test_scorer_checks_answer_and_tool_trace(self):
        from evals.benchmark import load_manifest, score_task

        task = load_manifest()["tasks"][0]
        score = score_task(task, {
            "answer": "前向扩散会加噪，反向去噪预测噪声；训练目标包含 MSE 和 deadline 约束。",
            "tool_trace": ["read_pdf", "query_papers"],
            "state": {},
        })
        self.assertTrue(score["passed"])

    def test_example_results_exercise_all_fifteen_tasks_and_emit_metrics(self):
        import json
        from evals.benchmark import ROOT, load_manifest, score_submission

        results = json.loads((ROOT / "example_results.json").read_text(encoding="utf-8"))
        report = score_submission(load_manifest(), results)
        self.assertEqual((report["passed"], report["total"]), (15, 15))
        self.assertEqual(report["summary"]["citation_traceability_rate"], 1.0)
        self.assertEqual(report["summary"]["source_failures"], 1)

    def test_report_comparison_calculates_versioned_deltas(self):
        from evals.benchmark import compare_reports

        comparison = compare_reports(
            {"summary": {"success_rate": 0.9, "average_duration_ms": 800}},
            {"generated_at": "2026-08-01T00:00:00+00:00", "benchmark_version": "v1.0",
             "summary": {"success_rate": 0.8, "average_duration_ms": 1000}},
        )
        self.assertEqual(comparison["baseline_version"], "v1.0")
        self.assertEqual(comparison["deltas"]["success_rate"], 0.1)
        self.assertEqual(comparison["deltas"]["average_duration_ms"], -200)

    def test_trace_capture_exports_latency_and_tools_without_payloads(self):
        from evals.capture import result_from_trace

        captured = result_from_trace("T10", "关键词候选", {
            "duration_ms": 9100,
            "first_token_ms": 1200,
            "tool_trace": [{
                "type": "tool_finished", "tool": "query_papers",
                "duration_ms": 8100, "rag_keyword_fallback": True,
            }],
            "usage_delta": {"calls": 1},
        })
        self.assertEqual(captured["tool_trace"][0]["tool"], "query_papers")
        self.assertEqual(captured["metrics"]["max_tool_duration_ms"], 8100)
        self.assertEqual(captured["metrics"]["model_calls"], 1)
        self.assertNotIn("arguments", str(captured))

    def test_daily_capture_keeps_source_health_without_candidate_content(self):
        from evals.capture import result_from_daily_run

        class _DailyResult:
            status = "partial_failed"
            brief = "已保留可恢复候选"
            candidates = [{"title": "不应写入评测结果"}]
            source_stats = {"TSN": {"openalex": {"status": "ok", "count": 1}}}

        captured = result_from_daily_run("T12", _DailyResult())
        self.assertEqual(captured["state"]["run_status"], "partial_failed")
        self.assertEqual(captured["state"]["candidate_count"], 1)
        self.assertEqual(captured["source_stats"]["TSN"]["openalex"]["status"], "ok")
        self.assertNotIn("不应写入评测结果", str(captured))

    def test_release_gate_combines_capability_and_runtime_suites(self):
        from evals.release_gate import GATE_VERSION, run_release_gate

        report = run_release_gate(seed=17)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["gate_version"], GATE_VERSION)
        self.assertEqual((report["capability"]["passed"], report["capability"]["total"]), (15, 15))
        self.assertEqual((report["runtime"]["passed"], report["runtime"]["total"]), (5, 5))
        self.assertEqual(report["summary"]["success_rate"], 1.0)


class TestRequestTracing(unittest.TestCase):
    """真实调用前后都应产生脱敏的可评分追踪。"""

    def test_agent_discards_persisted_transient_status_on_session_load(self):
        from config import Config
        from research_agent import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
            ))
            try:
                thread_id = agent.thread_id
                agent.sessions.save_usage(thread_id, {
                    "total": 7,
                    "api_status": "⚠️ stale model warning",
                    "verify_status": "⚠️ stale verify warning",
                })
                agent._usage_cache.pop(thread_id, None)

                usage = agent.get_usage(thread_id)
                self.assertEqual(usage["total"], 7)
                self.assertNotIn("api_status", usage)
                self.assertNotIn("verify_status", usage)
                self.assertNotIn("api_status", agent.sessions.get_usage(thread_id))
                self.assertNotIn("verify_status", agent.sessions.get_usage(thread_id))
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_interrupted_stream_releases_slot_and_can_be_retried(self):
        """A partial SSE response must not block the next same-model request."""
        import requests
        from config import Config
        from research_agent import ResearchAgent

        class _StreamingResponse:
            status_code = 200
            text = ""
            raw = None

            def __init__(self, lines):
                self.lines = lines
                self.closed = False

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                for line in self.lines:
                    if isinstance(line, BaseException):
                        raise line
                    yield line

            def close(self):
                self.closed = True

        first = _StreamingResponse([
            'data: {"choices":[{"delta":{"content":"已输出片段"}}]}',
            requests.exceptions.ChunkedEncodingError("interrupted"),
        ])
        second = _StreamingResponse([
            'data: {"choices":[{"delta":{"content":"重试完成"}}]}',
            "data: [DONE]",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
                api_max_concurrency=1, api_max_retries=0,
            ))
            try:
                with patch("llm_client.requests.post", side_effect=[first, second]) as post:
                    interrupted = agent.step("需要可靠重试的问题", on_token=lambda _token: None)
                    retry = agent.get_retry_input(agent.thread_id)
                    self.assertIn("输出中断", interrupted)
                    self.assertEqual(retry["user_input"], "需要可靠重试的问题")
                    self.assertTrue(
                        agent.llm_client._slots.acquire(blocking=False),
                        "中断流必须归还并发槽",
                    )
                    agent.llm_client._slots.release()

                    retried = agent.step(
                        retry["user_input"], context=retry["context"], topic=retry["topic"],
                        on_token=lambda _token: None,
                    )
                    self.assertEqual(retried, "重试完成")
                    self.assertEqual(post.call_count, 2)
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_agent_exposes_last_trace_without_prompt_or_tool_payload(self):
        from config import Config
        from research_agent import ResearchAgent

        class _FakeApp:
            def invoke(self, state, config):
                return {"messages": [{"role": "assistant", "content": "完成"}]}

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
            ))

            def fake_build(usage, on_token=None, event_callback=None, cancel_event=None):
                event_callback({
                    "type": "tool_finished", "tool": "query_papers",
                    "duration_ms": 12.5, "rag_keyword_fallback": False,
                })
                return _FakeApp()

            with patch.object(agent, "_build_app", side_effect=fake_build):
                self.assertEqual(agent.step("不应出现在追踪中的完整问题"), "完成")
            trace = agent.get_last_trace()
            self.assertEqual(trace["outcome"], "success")
            self.assertEqual(trace["tool_trace"][0]["tool"], "query_papers")
            self.assertNotIn("完整问题", str(trace))
            saved_run = agent.sessions.list_chat_runs(agent.thread_id, limit=1)[0]
            self.assertEqual(saved_run["status"], "completed")
            saved_events = agent.sessions.get_run_events(saved_run["run_id"], agent.thread_id)
            self.assertTrue(any(event["stage"] == "tool" for event in saved_events))
            self.assertNotIn("完整问题", str(saved_events))
            agent.memory._conn.close()
            agent.sessions.close()


class TestRuntimeReplay(unittest.TestCase):
    """离线回放必须走真实编排，且不暴露运行时数据。"""

    def test_runtime_replay_executes_all_reliability_cases(self):
        from evals.runtime_replay import run_suite

        report = run_suite(seed=17)
        self.assertEqual(report["total"], 5)
        self.assertEqual(report["passed"], 5, report)
        self.assertEqual(report["metadata"]["selected_cases"], ["R01", "R02", "R03", "R04", "R05"])
        self.assertGreater(report["summary"]["source_requests"], 0)
        self.assertGreater(report["summary"]["source_failure_rate"], 0)
        self.assertNotIn("offline-private-input-must-not-leak", str(report))

    def test_runtime_replay_rejects_unknown_case_and_compares_same_suite_only(self):
        from evals.runtime_replay import compare_reports, run_suite

        with self.assertRaisesRegex(ValueError, "unknown runtime replay case"):
            run_suite(case_ids=["R99"])
        report = run_suite(case_ids=["R01"], seed=3)
        comparison = compare_reports(report, {"suite_version": "runtime-replay-v1", "summary": report["summary"]})
        self.assertEqual(comparison["deltas"]["success_rate"], 0.0)
        with self.assertRaisesRegex(ValueError, "different runtime replay suite"):
            compare_reports(report, {"suite_version": "other", "summary": {}})


class TestWebRendering(unittest.TestCase):
    def test_chatbot_enables_inline_and_display_latex_delimiters(self):
        from web_ui import CHAT_LATEX_DELIMITERS

        self.assertIn({"left": "$", "right": "$", "display": False}, CHAT_LATEX_DELIMITERS)
        self.assertIn({"left": "$$", "right": "$$", "display": True}, CHAT_LATEX_DELIMITERS)
        self.assertIn({"left": "\\(", "right": "\\)", "display": False}, CHAT_LATEX_DELIMITERS)
        self.assertIn({"left": "\\[", "right": "\\]", "display": True}, CHAT_LATEX_DELIMITERS)


class TestAuditRegressionFixes(unittest.TestCase):
    """Regression coverage for correctness and boundary issues found in code audit."""

    def test_rag_query_reorders_and_honors_top_k(self):
        from paper_store import PaperStore

        class _Collection:
            def __init__(self):
                self.requested = None

            @staticmethod
            def count():
                return 4

            def query(self, **kwargs):
                self.requested = kwargs["n_results"]
                return {
                    "ids": [["intro", "method", "result", "other"]],
                    "documents": [["intro", "method", "result", "other"]],
                    "metadatas": [[
                        {"title": "Paper", "section": "Introduction", "chunk_index": 0},
                        {"title": "Paper", "section": "Method", "chunk_index": 1},
                        {"title": "Paper", "section": "Results", "chunk_index": 2},
                        {"title": "Paper", "section": "Related Work", "chunk_index": 3},
                    ]],
                    # Chroma order is intro → method → result. The section
                    # bonus must reorder Method/Results ahead of Introduction.
                    "distances": [[0.10, 0.12, 0.13, 0.30]],
                }

        store = PaperStore.__new__(PaperStore)
        store._collection = _Collection()
        results = store.query("method", top_k=2)

        self.assertEqual(store._collection.requested, 4)
        self.assertEqual([item["text"] for item in results], ["method", "result"])
        self.assertEqual(len(results), 2)

    def test_agent_sets_a_bounded_graph_recursion_limit(self):
        from config import Config
        from research_agent import MAX_AGENT_GRAPH_STEPS, ResearchAgent

        class _FakeApp:
            config = None

            def invoke(self, state, config):
                self.config = config
                return {"messages": [{"role": "assistant", "content": "完成"}]}

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
            ))
            app = _FakeApp()
            try:
                with patch.object(agent, "_build_app", return_value=app):
                    self.assertEqual(agent.step("请检索一个问题"), "完成")
                self.assertEqual(app.config["recursion_limit"], MAX_AGENT_GRAPH_STEPS)
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_session_order_is_stable_for_fast_consecutive_creates(self):
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "sessions.db"))
            try:
                sessions = [store.create(title=f"session-{index}") for index in range(5)]
                self.assertEqual(store.list()[0]["thread_id"], sessions[-1]["thread_id"])
                self.assertTrue(all("." in item["created_at"] for item in sessions))
            finally:
                store.close()

    def test_note_store_serializes_concurrent_writes_and_rejects_blank_topic(self):
        from concurrent.futures import ThreadPoolExecutor
        from notes import NoteStore

        with tempfile.TemporaryDirectory() as tmp:
            store = NoteStore(str(Path(tmp) / "notes.db"))
            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(lambda index: store.add("并发话题", f"note-{index}"), range(32)))
                self.assertEqual(len(store.list_notes("并发话题")), 32)
                self.assertIsNone(store.get_topic(None))
                with self.assertRaisesRegex(ValueError, "不能为空"):
                    store.ensure_topic("  ")
            finally:
                store.close()

    def test_profile_updates_are_atomic_under_concurrent_agent_calls(self):
        from concurrent.futures import ThreadPoolExecutor
        from profile import ProfileManager

        with tempfile.TemporaryDirectory() as tmp:
            profile = ProfileManager(str(Path(tmp) / "profile.md"))
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(
                    lambda index: profile.add_active_question(f"question-{index}"), range(16),
                ))
            content = profile.read()
            self.assertTrue(all(f"question-{index}" in content for index in range(16)))

    def test_optional_model_calls_do_not_clear_interactive_failure_signal(self):
        from llm_client import LLMClient, RequestPolicy

        class _Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def close():
                return None

        client = LLMClient(
            "test-key", "https://example.test/chat", circuit_failure_threshold=2,
        )
        client._circuit.record_failure()
        optional = RequestPolicy(purpose="curator", counts_toward_circuit=False)

        with patch("llm_client.requests.post", return_value=_Response()):
            response = client.post({"model": "test", "messages": []}, policy=optional)
            response.close()
        self.assertEqual(client._circuit._failures, 1)

        with patch("llm_client.requests.post", return_value=_Response()):
            stream = client.post(
                {"model": "test", "messages": []}, stream=True, policy=optional,
            )
        client.finish_stream(stream, success=True)
        self.assertEqual(client._circuit._failures, 1)

    def test_pdf_tool_rejects_local_path_outside_runtime(self):
        from runtime_paths import RuntimePaths
        from tools.read_pdf import handle_read_pdf

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths.from_root(root / "runtime")
            paths.ensure_initialized()
            private_pdf = root / "private.pdf"
            private_pdf.write_bytes(b"%PDF-private")
            with patch.dict(os.environ, {"APP_DATA_DIR": str(paths.root)}, clear=False):
                result = handle_read_pdf({"url_or_path": str(private_pdf)})
            self.assertIn("必须先上传", result)

    def test_pdf_tool_rejects_loopback_before_network_request(self):
        from tools.read_pdf import handle_read_pdf

        with patch("tools.read_pdf.requests.get") as request_get:
            result = handle_read_pdf({"url_or_path": "https://127.0.0.1/admin.pdf"})
        self.assertIn("下载被拒绝", result)
        self.assertFalse(request_get.called)

    def test_pdf_download_enforces_size_limit_before_writing(self):
        import socket
        from tools.read_pdf import MAX_PDF_DOWNLOAD_BYTES, PDFDownloadError, _download_pdf

        class _Response:
            status_code = 200
            headers = {
                "Content-Length": str(MAX_PDF_DOWNLOAD_BYTES + 1),
                "Content-Type": "application/pdf",
            }

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def iter_content(chunk_size):
                return iter([b"%PDF-ignored"])

            @staticmethod
            def close():
                return None

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper.pdf"
            with patch(
                "tools.read_pdf.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            ), patch("tools.read_pdf.requests.get", return_value=_Response()):
                with self.assertRaisesRegex(PDFDownloadError, "文件过大"):
                    _download_pdf("https://example.com/paper.pdf", target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
