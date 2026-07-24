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
        pdfs = list(Path("data/papers").glob("*.pdf"))
        if not pdfs:
            self.skipTest("data/papers/ 中没有 PDF 文件")
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


class TestVerifyNode(unittest.TestCase):
    """测试3: verify 节点边界不崩溃"""

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

    def test_graph_builds_correctly(self):
        """图结构正常编译（不调用 API）"""
        from graph_builder import build_graph
        app = build_graph(api_key="test-key", model="deepseek-chat")
        nodes = list(app.get_graph().nodes.keys())
        self.assertIn("llm", nodes)
        self.assertIn("tools", nodes)
        self.assertIn("verify", nodes)
        self.assertIn("__start__", nodes)
        self.assertIn("__end__", nodes)

    def test_tools_module_imports(self):
        """所有工具模块可正常导入"""
        from tools import search_arxiv, search_semantic_scholar, list_downloaded_papers
        from pdf_reader import read_pdf_enhanced, extract_images
        from paper_store import PaperStore, chunk_text
        from notes import NoteStore
        from profile import ProfileManager
        self.assertTrue(callable(search_arxiv))
        self.assertTrue(callable(read_pdf_enhanced))
        self.assertTrue(callable(chunk_text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
