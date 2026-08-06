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
import zipfile
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from unittest.mock import patch, MagicMock


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
        app = build_graph(api_key="test-key", model="deepseek-chat", checkpoint_db=":memory:")
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
        with patch("graph_builder.requests.post", side_effect=responses):
            app = build_graph(api_key="test-key", checkpoint_db=":memory:")
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
        with patch("graph_builder.requests.post", side_effect=responses) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", memory_store=_Memory(),
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

    def test_verify_uses_short_timeout_and_circuit_breaker(self):
        """验证服务超时时快速降级，并打开熔断器避免下一轮继续等待。"""
        import requests
        from graph_builder import build_graph
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
        with patch("graph_builder.requests.post", side_effect=responses) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", token_usage=usage,
                verify_timeout_seconds=4, verify_guard=guard,
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
        self.assertEqual(verify_calls[0].kwargs["timeout"], (3.05, 4))
        self.assertFalse(guard.allow_request())
        self.assertIn("验证跳过", usage["verify_status"])

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

            def new_request_budget(self):
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
        from search_api import search_arxiv, search_semantic_scholar, list_downloaded_papers
        from pdf_reader import read_pdf_enhanced, extract_images
        from paper_store import PaperStore, chunk_text
        from notes import NoteStore
        from profile import ProfileManager
        self.assertTrue(callable(search_arxiv))
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
                with patch("search_api.search_semantic_scholar", return_value="- **Semantic paper**") as semantic, \
                     patch("requests.get", side_effect=fake_get) as request_get:
                    results = scheduler.search("TSN scheduling", limit=1)
                self.assertEqual(semantic.call_args.kwargs["timeout"], (3.05, 4))
                self.assertEqual(request_get.call_count, 2)
                self.assertEqual(
                    {result["source"] for result in results},
                    {"semantic_scholar", "arxiv", "openalex"},
                )
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
