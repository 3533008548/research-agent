"""
📚 论文向量存储 — ChromaDB + SentenceTransformer RAG 模块

功能：
  1. 论文自动分块（按段落 + 滑动窗口重叠）
  2. 向量化存储（SentenceTransformer → ChromaDB persistent）
  3. 语义检索（自然语言查询 → 返回最相关段落）
  4. 论文管理（列表、删除、元数据持久化）

用法：
  >>> store = PaperStore()
  >>> store.index_paper("论文全文...", title="Attention Is All You Need")
  >>> results = store.query("What is the loss function?", top_k=3)
  >>> store.list_papers()
"""

import os
import sys
import re
import math
import uuid
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    from chromadb.utils import embedding_functions
except ImportError:
    chromadb = None
    embedding_functions = None


# The local paper corpus is normally modest.  For unusually large collections,
# do not turn an interactive RAG request into an unbounded full-corpus scan.
HYBRID_LEXICAL_MAX_CHUNKS = 5_000
HYBRID_CANDIDATE_LIMIT = 20
RRF_K = 60
DEFAULT_RERANKER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
_PDF_READER_PREAMBLE = "📄 **PDF 解析完成**"


def clean_index_text(text: str) -> str:
    """Remove reader status text that is useful to people but not retrieval."""
    normalized = str(text or "").strip()
    if not normalized.startswith(_PDF_READER_PREAMBLE):
        return normalized
    _header, separator, body = normalized.partition("\n\n")
    return body.strip() if separator else normalized


# ═══════════════════════════════════════════════════════════════
#  Chunk 分块策略
# ═══════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 150,
) -> list[dict]:
    """
    将论文文本切分为有重叠的语义块。

    策略：
      1. 先按段落（双换行）切分
      2. 块大小控制在 chunk_size 左右，相邻块保持 overlap 字符重叠
      3. 公式区域（$$...$$ 和 $...$）标记为不可切割，切点自动避开

    返回: [{"text": "...", "section": "Method", "index": 0, "char_start": 0, "char_end": 580}, ...]
    """
    # 预扫描：找到所有公式的不可切割区间
    # [^$]* 允许 $$..$$ 空公式，避免正则漏掉边界情况
    _formula_pat = re.compile(r'\$\$[^$]*\$\$|\$[^$]+\$')
    _sent_end = re.compile(r'[。．.!?！？;；]')

    def _find_formula_ranges(para: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in _formula_pat.finditer(para)]

    def _safe_cut(para: str, ideal: int, forbidden: list[tuple[int, int]], para_len: int) -> int:
        """调整切点，避免落在公式区间内（公式区间已排序，二分查找）"""
        import bisect
        starts = [s for s, e in forbidden]
        idx = bisect.bisect_right(starts, ideal) - 1
        if idx >= 0:
            s, e = forbidden[idx]
            if s <= ideal <= e:
                return s if (ideal - s) < (e - ideal) else e
        return ideal

    def _sentence_cut(para: str, end: int, para_len: int) -> int:
        """把切点挪到最近的句子结束符后（在 ±30 字符窗口内）"""
        if end >= para_len:
            return end
        window_start = max(0, end - 30)
        window = para[window_start:end]
        # 找窗口内最后一个句子结束符
        last = None
        for m in _sent_end.finditer(window):
            last = m
        if last:
            return window_start + last.end()
        return end

    # ── 第1层：章节切分（## 和 ### 两级）──
    section_blocks = []  # [(section_label, text)]
    current_section = "未标注"
    current_sub = ""
    lines = text.split("\n")
    buf = []

    def flush_buf():
        nonlocal buf
        if buf:
            body = "\n".join(buf).strip()
            if body:
                label = current_section if not current_sub else f"{current_section} > {current_sub}"
                section_blocks.append((label, body))
        buf = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            flush_buf()
            if not stripped.startswith("### "):
                current_section = stripped[3:].strip()
                current_sub = ""
            else:
                current_sub = stripped[4:].strip()
        elif stripped.startswith("### "):
            flush_buf()
            current_sub = stripped[4:].strip()
        else:
            buf.append(line)
    flush_buf()

    # ── 第2/3层：段落切分 + 尺寸切分 ──
    chunks = []
    char_pos = 0

    for section_label, section_text in section_blocks:
        paragraphs = section_text.split("\n\n")
        # 前缀只取一级章节（## Method），子章节存 metadata 不注入文本
        prefix_label = section_label.split(" > ")[0]
        prefix = f"## {prefix_label}\n" if section_label != "未标注" else ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                char_pos += 2
                continue

            para_start = char_pos
            para_len = len(para)
            adaptive_size = 1500 if section_label != "未标注" else chunk_size
            formula_ranges = sorted(_find_formula_ranges(para))

            if para_len + len(prefix) <= adaptive_size:
                chunks.append({
                    "text": prefix + para,
                    "section": section_label,
                    "char_start": para_start,
                    "char_end": para_start + para_len,
                })
                char_pos += para_len + 2
                continue

            # 长段落：滑动窗口，切点避开公式 + 句子边界
            start = 0
            while start < para_len:
                ideal_end = min(start + adaptive_size - len(prefix), para_len)
                end = _safe_cut(para, ideal_end, formula_ranges, para_len)
                # end 必须 > start，防止巨大公式边界把 end 拨回 start 之前 → 1字符chunk
                if end <= start:
                    end = ideal_end
                end = min(max(end, start + 1), para_len)
                # 句子切割同样不能后退
                sent_end = _sentence_cut(para, end, para_len)
                if sent_end > start:
                    end = sent_end
                end = min(max(end, start + 1), para_len)

                chunks.append({
                    "text": prefix + para[start:end],
                    "section": section_label,
                    "char_start": para_start + start,
                    "char_end": para_start + end,
                })

                if end >= para_len:
                    break
                # 下一块起点：必须前进，防止公式边界把 start 拨回 → 死循环
                next_start = _safe_cut(para, ideal_end - overlap, formula_ranges, para_len)
                if next_start <= start:
                    next_start = end
                start = min(next_start, para_len - 1)

            char_pos += para_len + 2

    for i, chunk in enumerate(chunks):
        chunk["index"] = i

    return chunks


# ═══════════════════════════════════════════════════════════════
#  PaperStore 类
# ═══════════════════════════════════════════════════════════════

class PaperStore:
    """
    论文向量存储 — 基于 ChromaDB + SentenceTransformer。

    存储结构:
      APP_DATA_DIR/derived/chroma/
        └── collection "papers"
              ├── embedding: 384维向量 (all-MiniLM-L6-v2)
              ├── metadata: {paper_id, title, chunk_index, char_start, char_end}
              └── document: 块文本
    """

    COLLECTION_NAME = "papers"

    def __init__(
        self,
        persist_dir: str | None = None,
        *,
        reranker_enabled: bool = False,
        reranker_model: str = DEFAULT_RERANKER_MODEL,
        reranker_candidate_limit: int = HYBRID_CANDIDATE_LIMIT,
    ):
        if chromadb is None or embedding_functions is None:
            raise ImportError(
                "需要安装 chromadb:\n   pip install chromadb"
            )

        if persist_dir is None:
            from runtime_paths import get_runtime_paths
            persist_dir = str(get_runtime_paths().chroma_dir)
        persist_dir = Path(persist_dir)
        persist_dir = persist_dir.resolve()
        self.persist_dir = str(persist_dir)

        # 使用 ChromaDB 内置 ONNX 嵌入（all-MiniLM-L6-v2, ~80MB, 纯 CPU）
        # 首次运行自动下载模型，之后本地缓存，无需安装 sentence-transformers
        print(
            f"      [Embedding] 加载 ChromaDB ONNX 模型...",
            end="", flush=True, file=sys.stderr,
        )
        self._embed_fn = embedding_functions.DefaultEmbeddingFunction()
        print(" 完成", file=sys.stderr)

        # 初始化 ChromaDB（持久化模式）
        self._chroma = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

        # 获取或创建 collection（注入嵌入函数）
        self._collection = self._chroma.get_or_create_collection(
            name=self.COLLECTION_NAME,
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        self._query_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rag-query",
        )
        self._query_lock = threading.RLock()
        self._active_query: Future | None = None
        self._reranker_enabled = reranker_enabled
        self._reranker_model_name = reranker_model
        self._reranker_candidate_limit = max(
            1, min(int(reranker_candidate_limit), HYBRID_CANDIDATE_LIMIT),
        )
        self._reranker = None
        self._reranker_load_attempted = False
        self._reranker_lock = threading.Lock()

    # ── 公开接口 ──

    def index_paper(
        self,
        text: str,
        title: str = "Unknown",
        paper_id: Optional[str] = None,
    ) -> str:
        """
        将一篇论文全文分块并存入向量库。

        返回: paper_id (若未传入则自动生成)
        """
        text = clean_index_text(text)
        if not text:
            return ""

        return self._index_chunks(chunk_text(text), title=title, paper_id=paper_id)

    def index_document_map(
        self,
        document_map: dict[str, Any],
        *,
        title: str = "Unknown",
        paper_id: Optional[str] = None,
    ) -> str:
        """Index page-scoped chunks created by ``paper_artifacts.build_document_map``."""
        resolved_title = str(document_map.get("title") or title)
        resolved_id = str(document_map.get("paper_id") or paper_id or "") or None
        chunks = list(document_map.get("chunks") or [])
        return self._index_chunks(chunks, title=resolved_title, paper_id=resolved_id)

    def _index_chunks(
        self,
        chunks: list[dict[str, Any]],
        *,
        title: str,
        paper_id: Optional[str],
    ) -> str:
        """Persist already segmented chunks while keeping old text-only callers compatible."""
        resolved_id = paper_id or f"paper_{uuid.uuid4().hex[:12]}"
        usable = [chunk for chunk in chunks if str(chunk.get("text") or "").strip()]
        if not usable:
            return resolved_id

        texts = []
        metadatas = []
        chunk_ids = []
        char_pos = 0
        for index, chunk in enumerate(usable):
            text = str(chunk["text"]).strip()
            start = int(chunk.get("char_start", char_pos))
            end = int(chunk.get("char_end", start + len(text)))
            page = chunk.get("page")
            metadatas.append({
                "paper_id": resolved_id,
                "title": title,
                "section": str(chunk.get("section") or "未标注"),
                "chunk_index": index,
                "char_start": start,
                "char_end": end,
                "page": int(page) if isinstance(page, int) and page > 0 else -1,
                "element_id": str(chunk.get("id") or ""),
                "element_kind": str(chunk.get("kind") or "text"),
                "indexed_at": datetime.now().isoformat(),
            })
            texts.append(text)
            chunk_ids.append(f"{resolved_id}_chunk_{index}")
            char_pos = end + 2

        self._collection.add(ids=chunk_ids, documents=texts, metadatas=metadatas)
        return resolved_id

    def query(
        self,
        query_text: str,
        top_k: int = 3,
        paper_ids: Optional[list[str]] = None,
        section: Optional[str] = None,
    ) -> list[dict]:
        """
        在已索引论文中检索最相关的段落。

        参数:
          query_text: 查询文本（自然语言）
          top_k: 返回结果数
          paper_ids: 限定检索范围（None 表示全部论文）
          section: 限定章节（如 "Method"），None 表示全部

        返回: [{"text": "...", "section": "...", "title": "...",
                 "distance": 0.23}, ...]
        """
        top_k = max(1, min(int(top_k), 20))
        collection_count = self._collection.count()
        if collection_count == 0:
            return []

        # 构建过滤条件
        where = None
        conds = []
        if paper_ids:
            if len(paper_ids) == 1:
                conds.append({"paper_id": paper_ids[0]})
            else:
                conds.append({"paper_id": {"$in": paper_ids}})
        if section:
            conds.append({"section": section})
        if len(conds) == 1:
            where = conds[0]
        elif len(conds) > 1:
            where = {"$and": conds}

        # 检索
        raw = self._collection.query(
            query_texts=[query_text],
            n_results=min(collection_count, top_k * 3),  # 多取一些用于章节加权
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        # 整理结果 + 章节加权（Method/Experiments 优先）
        WEIGHT_SECTIONS = {"method", "experiment", "evaluation", "result", "proposed"}
        results = []
        if raw["ids"] and raw["ids"][0]:
            for i, doc_id in enumerate(raw["ids"][0]):
                meta = raw["metadatas"][0][i] if raw["metadatas"] and raw["metadatas"][0] else {}
                dist = raw["distances"][0][i] if raw["distances"] and raw["distances"][0] else 0
                sec = meta.get("section", "未标注")
                # 章节加权：Method 类章节距离减 0.05
                weight_penalty = 0.05 if any(w in sec.lower() for w in WEIGHT_SECTIONS) else 0
                results.append({
                    "text": raw["documents"][0][i] if raw["documents"] and raw["documents"][0] else "",
                    "section": sec,
                    "title": meta.get("title", "Unknown"),
                    "paper_id": meta.get("paper_id", ""),
                    "chunk_index": meta.get("chunk_index", 0),
                    "char_start": meta.get("char_start", 0),
                    "char_end": meta.get("char_end", 0),
                    "page": meta.get("page", -1),
                    "element_id": meta.get("element_id", ""),
                    "element_kind": meta.get("element_kind", "text"),
                    "distance": round(max(dist - weight_penalty, 0), 4),
                })

        # A section bonus only takes effect after sorting. Returning the raw
        # Chroma order also leaked up to ``top_k * 3`` chunks into the prompt.
        return sorted(
            results,
            key=lambda item: (item["distance"], item["title"], item["chunk_index"]),
        )[:top_k]

    def query_with_timeout(
        self,
        query_text: str,
        top_k: int = 3,
        paper_ids: Optional[list[str]] = None,
        section: Optional[str] = None,
        timeout_seconds: float = 8.0,
    ) -> tuple[list[dict] | None, str | None]:
        """有界执行 RAG 查询，初始化期间立即退回本地关键词检索。"""
        with self._query_lock:
            active = self._active_query
            if active is not None and not active.done():
                return self.query_lexical(query_text, top_k, paper_ids, section), (
                    "嵌入模型仍在后台初始化，以下为本地关键词候选结果。"
                )
            future = self._query_executor.submit(
                self.query_hybrid, query_text, top_k, paper_ids, section,
            )
            self._active_query = future

            def _clear_finished(done: Future) -> None:
                with self._query_lock:
                    if self._active_query is done:
                        self._active_query = None

            future.add_done_callback(_clear_finished)

        try:
            return future.result(timeout=max(0.1, timeout_seconds)), None
        except FutureTimeout:
            return self.query_lexical(query_text, top_k, paper_ids, section), (
                "首次嵌入模型正在后台下载或初始化，以下为本地关键词候选结果；"
                "完成后会自动恢复语义检索。"
            )
        except Exception as exc:
            return self.query_lexical(query_text, top_k, paper_ids, section), (
                f"RAG 语义检索暂不可用，已改用本地关键词检索："
                f"{type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _lexical_tokens(text: str) -> list[str]:
        """Tokenize text for the small local BM25 index."""
        latin_terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}", text.lower())
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        return latin_terms + [cjk[i:i + 2] for i in range(max(len(cjk) - 1, 0))]

    @classmethod
    def _query_terms(cls, query_text: str) -> list[str]:
        """提取适合中英文论文文本的去重检索词。"""
        return list(dict.fromkeys(cls._lexical_tokens(query_text)))

    @staticmethod
    def _result_key(result: dict) -> tuple[str, str, int]:
        """Return a stable key for merging semantic and lexical candidates."""
        return (
            str(result.get("paper_id") or result.get("title") or ""),
            str(result.get("title") or ""),
            int(result.get("chunk_index") or 0),
        )

    def _get_reranker(self):
        """Load the optional local cross-encoder only when a query needs it."""
        if not getattr(self, "_reranker_enabled", False):
            return None
        if self._reranker is not None or self._reranker_load_attempted:
            return self._reranker
        with self._reranker_lock:
            if self._reranker is not None or self._reranker_load_attempted:
                return self._reranker
            self._reranker_load_attempted = True
            try:
                from sentence_transformers import CrossEncoder

                self._reranker = CrossEncoder(
                    self._reranker_model_name,
                    max_length=512,
                    local_files_only=True,
                )
            except Exception as exc:
                print(
                    f"      ⚠️ RAG 重排器未启用（{type(exc).__name__}: {exc}）",
                    file=sys.stderr,
                )
        return self._reranker

    def _rerank(self, query_text: str, candidates: list[dict]) -> list[dict]:
        """Use a cross-encoder to reorder only the already retrieved candidates."""
        reranker = self._get_reranker()
        if reranker is None or not candidates:
            return candidates
        try:
            scores = reranker.predict(
                [(query_text, item["text"]) for item in candidates],
                batch_size=8,
                show_progress_bar=False,
            )
        except Exception as exc:
            print(
                f"      ⚠️ RAG 重排失败，保留 RRF 结果（{type(exc).__name__}: {exc}）",
                file=sys.stderr,
            )
            return candidates

        ranked = []
        for item, score in zip(candidates, scores):
            ranked.append({
                **item,
                "reranked": True,
                "reranker_score": round(float(score), 6),
            })
        return sorted(
            ranked,
            key=lambda item: (
                -item["reranker_score"],
                -item.get("hybrid_score", 0.0),
                item.get("title", ""),
                item.get("chunk_index", 0),
            ),
        )

    def query_hybrid(
        self,
        query_text: str,
        top_k: int = 3,
        paper_ids: Optional[list[str]] = None,
        section: Optional[str] = None,
    ) -> list[dict]:
        """Fuse semantic and lexical candidates with reciprocal-rank fusion.

        The two retrieval scores are not comparable: Chroma returns distances,
        while lexical retrieval returns term counts.  RRF therefore merges only
        their ranks, keeps ties deterministic, and needs no extra dependency or
        persisted index.  On a large local corpus we retain semantic retrieval
        rather than scanning every chunk on the request path.
        """
        top_k = max(1, min(int(top_k), 20))
        candidate_k = min(
            HYBRID_CANDIDATE_LIMIT,
            max(
                top_k * 4,
                getattr(self, "_reranker_candidate_limit", 0)
                if getattr(self, "_reranker_enabled", False) else 0,
            ),
        )
        semantic = self.query(query_text, candidate_k, paper_ids, section)
        collection_count = self._collection.count()
        if collection_count > HYBRID_LEXICAL_MAX_CHUNKS:
            return [
                {**item, "retrieval": "semantic"}
                for item in semantic[:top_k]
            ]

        try:
            lexical = self.query_lexical(query_text, candidate_k, paper_ids, section)
        except Exception:
            # A working vector result is more useful than failing the complete
            # request because a best-effort local keyword scan is unavailable.
            return [
                {**item, "retrieval": "semantic"}
                for item in semantic[:top_k]
            ]

        if not semantic:
            return lexical[:top_k]
        if not lexical:
            return [
                {**item, "retrieval": "semantic"}
                for item in semantic[:top_k]
            ]

        fused: dict[tuple[str, str, int], dict] = {}
        for source, candidates in (("semantic", semantic), ("keyword", lexical)):
            for rank, candidate in enumerate(candidates, start=1):
                key = self._result_key(candidate)
                item = fused.setdefault(key, dict(candidate))
                item[f"{source}_rank"] = rank
                item["hybrid_score"] = round(
                    float(item.get("hybrid_score", 0.0)) + 1.0 / (RRF_K + rank), 6,
                )

        results = []
        for item in fused.values():
            item["retrieval"] = "hybrid"
            results.append(item)
        ranked = sorted(
            results,
            key=lambda item: (
                -item["hybrid_score"],
                min(item.get("semantic_rank", 99_999), item.get("keyword_rank", 99_999)),
                item.get("title", ""),
                item.get("chunk_index", 0),
            ),
        )
        rerank_limit = (
            getattr(self, "_reranker_candidate_limit", 0)
            if getattr(self, "_reranker_enabled", False) else 0
        )
        reranked = self._rerank(query_text, ranked[:rerank_limit]) if rerank_limit else ranked
        return reranked[:top_k]

    def query_lexical(
        self,
        query_text: str,
        top_k: int = 3,
        paper_ids: Optional[list[str]] = None,
        section: Optional[str] = None,
    ) -> list[dict]:
        """基于 BM25 的本地正文检索，用于混合召回和嵌入模型初始化期间。"""
        if self._collection.count() == 0:
            return []
        terms = self._query_terms(query_text)
        if not terms:
            return []

        raw = self._collection.get(include=["documents", "metadatas"])
        candidates = []
        for doc, meta in zip(raw.get("documents") or [], raw.get("metadatas") or []):
            meta = meta or {}
            if paper_ids and meta.get("paper_id") not in paper_ids:
                continue
            if section and meta.get("section") != section:
                continue
            candidates.append({
                "text": doc or "",
                "section": meta.get("section", "未标注"),
                "title": meta.get("title", "Unknown"),
                "paper_id": meta.get("paper_id", ""),
                "chunk_index": meta.get("chunk_index", 0),
                "char_start": meta.get("char_start", 0),
                "char_end": meta.get("char_end", 0),
                "page": meta.get("page", -1),
                "element_id": meta.get("element_id", ""),
                "element_kind": meta.get("element_kind", "text"),
                "retrieval": "keyword",
            })

        if not candidates:
            return []

        tokenized = [self._lexical_tokens(item["text"]) for item in candidates]
        document_count = len(tokenized)
        average_length = sum(len(tokens) for tokens in tokenized) / document_count
        document_frequency = {
            term: sum(term in set(tokens) for tokens in tokenized)
            for term in terms
        }
        for item, tokens in zip(candidates, tokenized):
            length = len(tokens)
            score = 0.0
            for term in terms:
                frequency = tokens.count(term)
                if not frequency:
                    continue
                idf = math.log(1 + (document_count - document_frequency[term] + 0.5) /
                               (document_frequency[term] + 0.5))
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * length / average_length)
                score += idf * frequency * 2.5 / denominator
            item["keyword_score"] = round(score, 6)

        candidates = [item for item in candidates if item["keyword_score"] > 0]
        return sorted(
            candidates,
            key=lambda item: (-item["keyword_score"], item["title"], item["chunk_index"]),
        )[:top_k]

    def list_papers(self) -> list[dict]:
        """列出所有已索引的论文（去重）"""
        if self._collection.count() == 0:
            return []

        # 获取所有元数据
        raw = self._collection.get(include=["metadatas"])
        metadatas = raw.get("metadatas", [])

        # 按 paper_id 去重，统计每篇的块数
        paper_map = {}
        for meta in metadatas:
            pid = meta.get("paper_id", "")
            if pid not in paper_map:
                paper_map[pid] = {
                    "paper_id": pid,
                    "title": meta.get("title", "Unknown"),
                    "chunks": 0,
                    "indexed_at": meta.get("indexed_at", ""),
                }
            paper_map[pid]["chunks"] += 1

        return list(paper_map.values())

    def delete_paper(self, paper_id: str) -> int:
        """删除一篇论文的所有块。返回删除的块数。"""
        # 查找该论文的所有 chunk ids
        raw = self._collection.get(
            where={"paper_id": paper_id},
            include=["metadatas"],
        )
        ids_to_delete = raw.get("ids", [])
        if ids_to_delete:
            self._collection.delete(ids=ids_to_delete)
        return len(ids_to_delete)

    def get_paper_chunks(self, paper_id: str) -> list[dict]:
        """获取一篇论文的所有块（按 char_start 排序）"""
        raw = self._collection.get(
            where={"paper_id": paper_id},
            include=["documents", "metadatas"],
        )
        chunks = []
        if raw["ids"]:
            for i, doc_id in enumerate(raw["ids"]):
                meta = raw["metadatas"][i] if raw["metadatas"] else {}
                chunks.append({
                    "text": raw["documents"][i] if raw["documents"] else "",
                    "char_start": meta.get("char_start", 0),
                    "char_end": meta.get("char_end", 0),
                    "chunk_index": meta.get("chunk_index", 0),
                    "page": meta.get("page", -1),
                    "element_id": meta.get("element_id", ""),
                    "element_kind": meta.get("element_kind", "text"),
                })
        chunks.sort(key=lambda c: c["char_start"])
        return chunks

    @property
    def paper_count(self) -> int:
        """已索引的论文数"""
        return len(self.list_papers())

    @property
    def chunk_count(self) -> int:
        """总块数"""
        return self._collection.count()


# ═══════════════════════════════════════════════════════════════
#  NoOpStore — ChromaDB 不可用时的降级后备
# ═══════════════════════════════════════════════════════════════

class NoOpStore:
    """空操作存储 — ChromaDB 离线时保持 Agent 可用"""
    def query(self, *a, **kw): return []
    def query_hybrid(self, *a, **kw): return []
    def query_with_timeout(self, *a, **kw): return [], None
    def index_paper(self, *a, **kw): return ""
    def index_document_map(self, *a, **kw): return ""
    def list_papers(self): return []
    def delete_paper(self, *a, **kw): return 0
    paper_count = 0
    chunk_count = 0


# ═══════════════════════════════════════════════════════════════
#  独立测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("  PaperStore 功能测试")
    print("=" * 50)

    import sys
    try:
        store = PaperStore(persist_dir="./chroma_test")
    except ImportError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # 模拟两篇论文
    paper_a = """Abstract: We propose a novel transformer-based approach...
    Introduction: Attention mechanisms have become ubiquitous in deep learning...
    Method: Our key innovation is the multi-head scaled dot-product attention...
    Experiments: We evaluated on WMT 2014 English-to-German translation...
    Results: Our model achieves 28.4 BLEU score, surpassing the previous state of the art...
    Conclusion: We introduced a new simple network architecture, the Transformer..."""

    paper_a = paper_a.replace("    ", "\n\n")

    paper_b = """Abstract: Diffusion models have emerged as powerful generative models...
    Introduction: Denoising diffusion probabilistic models (DDPMs) learn to reverse
    a gradual noising process...
    Method: The forward process adds Gaussian noise according to a variance schedule...
    Experiments: On CIFAR-10, our model achieves FID score of 3.17...
    Discussion: We observe that larger models benefit from more diffusion steps...
    Conclusion: We presented high-quality image generation using diffusion..."""

    paper_b = paper_b.replace("    ", "\n\n")

    # 索引
    print("\n📝 索引论文 A (Transformer)...")
    pid_a = store.index_paper(paper_a, title="Attention Is All You Need")
    print(f"   paper_id: {pid_a}")

    print("\n📝 索引论文 B (Diffusion)...")
    pid_b = store.index_paper(paper_b, title="Denoising Diffusion Probabilistic Models")
    print(f"   paper_id: {pid_b}")

    # 列表
    print(f"\n📚 已索引论文: {store.paper_count} 篇, 共 {store.chunk_count} 个块")
    for p in store.list_papers():
        print(f"   {p['title']} ({p['chunks']} chunks)")

    # 查询
    print("\n🔍 查询: 'How does attention work?'")
    results = store.query("How does attention work?", top_k=3)
    for i, r in enumerate(results, 1):
        print(f"  {i}. [{r['title']}] (距离: {r['distance']})")
        print(f"     {r['text'][:100]}...")

    print("\n🔍 查询: 'What evaluation metrics were used?'")
    results = store.query("What evaluation metrics were used?", top_k=3)
    for i, r in enumerate(results, 1):
        print(f"  {i}. [{r['title']}] (距离: {r['distance']})")
        print(f"     {r['text'][:100]}...")

    # 清理：删除一篇论文
    print(f"\n🗑️ 删除论文 B ({pid_b})")
    deleted = store.delete_paper(pid_b)
    print(f"   删除了 {deleted} 个块")
    print(f"   剩余: {store.paper_count} 篇论文")

    print("\n✅ 测试完成")
