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
from typing import Any, Optional, Protocol
from datetime import datetime

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    from chromadb.utils import embedding_functions
except ImportError:
    chromadb = None
    embedding_functions = None

# The embedding backend is injected rather than hard-coded, so switching models
# is a configuration change.  See embeddings.py for the contract.
from embeddings import DEFAULT_EMBEDDING_MODEL, Embedder, build_embedder
from logger import get_logger
from papers.lexical import build_index as build_lexical_index
from papers.lexical import iter_documents as lexical_documents
from papers.lexical import query_terms as lexical_query_terms
from papers.lexical import scan_score as scan_lexical
from papers.lexical import score_index as score_lexical_index
from papers.lexical import tokenize as lexical_tokenize
from papers.metrics import StageTimer
from papers.rerank import load_cross_encoder
from papers.rerank import rerank_with

# Structured logging instead of print(): print() carries no level, timestamp or
# module name, so an incident ends up as an unstructured blob nobody can query.
# Phase 1 of the five-dimension refactor converts this module's calls; the rest
# of the codebase still uses print and is converted module by module.
log = get_logger("paper_store")


# The local paper corpus is normally modest.  For unusually large collections,
# do not turn an interactive RAG request into an unbounded full-corpus scan.
HYBRID_LEXICAL_MAX_CHUNKS = 5_000
HYBRID_CANDIDATE_LIMIT = 20
RRF_K = 60
DEFAULT_RERANKER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RELATION_EXPANSION_SEED_LIMIT = 3
RELATION_EXPANSION_MAX_PAPERS = 8
RELATION_EXPANSION_RRF_FACTOR = 0.5

# Chunking moved to papers/chunking.py during the five-dimension refactor: it is
# pure text processing with no IO, so it can be tested without constructing a
# PaperStore (which would require ChromaDB, an embedder and a data directory).
# Re-exported here because 20 call sites across tests and scripts import these
# names from paper_store; the forwarding layer can be retired later without
# touching any of them.
from papers.chunking import (  # noqa: E402
    _overlap_chars,
    _longest_prefix_chars,
    _tokenize_is_unavailable,
    chunk_text,
    clean_index_text,
    limit_chunks_to_tokens,
    split_oversized_text,
)


class PaperStoreProtocol(Protocol):
    """论文库的公共契约。

    :class:`PaperStore` 与 :class:`NoOpStore` 都必须提供这些方法。
    把它显式写出来，是为了让"接口"有唯一定义处：新增能力时只改这里，
    并由 tests 校验两个实现都补齐，避免两份方法列表手工同步时漏改。
    """

    paper_count: int
    chunk_count: int

    def query(
        self,
        query_text: str,
        top_k: int = 3,
        paper_ids: Optional[list[str]] = None,
        section: Optional[str] = None,
    ) -> list[dict]: ...

    def query_hybrid(
        self,
        query_text: str,
        top_k: int = 3,
        paper_ids: Optional[list[str]] = None,
        section: Optional[str] = None,
    ) -> list[dict]: ...

    def query_with_timeout(self, query_text: str, **kwargs: Any) -> tuple[list[dict], Any]: ...

    def start_embedding_warmup(self) -> Any: ...

    def index_paper(
        self,
        text: str,
        title: str = "Unknown",
        paper_id: Optional[str] = None,
    ) -> str: ...

    def index_document_map(self, document_map: dict, **kwargs: Any) -> str: ...

    def list_papers(self) -> list[dict]: ...

    def delete_paper(self, paper_id: str) -> int: ...

    def get_paper_chunks(self, paper_id: str) -> list[dict]: ...


# 集合名可配置，便于换模型时并行建新集合（如 papers_v2）再切换，旧集合留作回滚备份。
DEFAULT_COLLECTION_NAME = "papers"

# ── 检索调参：集中定义，避免魔数散落在各个方法里 ──
# 稠密召回的过取倍数：多取候选，供章节加权重排后再截断。
SEMANTIC_CANDIDATE_FACTOR = 3
# 混合召回的过取倍数：需要足够候选喂给 RRF 融合与精排。
HYBRID_CANDIDATE_FACTOR = 4
# 命中这些关键词的章节视为"方法/实验类"，在排序时给一点距离折扣。
SECTION_PRIORITY_TERMS = ("method", "experiment", "evaluation", "result", "proposed")
SECTION_PRIORITY_BONUS = 0.05
# BM25_K1 / BM25_B / BM25_SCALE moved to papers/lexical.py with the formula that
# uses them.  Tunable in one place instead of two that could drift apart.



# ═══════════════════════════════════════════════════════════════
#  PaperStore 类
# ═══════════════════════════════════════════════════════════════

class PaperStore:
    """
    论文向量存储 — ChromaDB + 可插拔嵌入后端。

    向量化由注入的 Embedder 完成（见 embeddings.py），本类不绑定任何具体模型。
    默认后端是 ChromaDB 内置 ONNX all-MiniLM-L6-v2（384 维，纯 CPU）；
    换成 Qwen3-Embedding 只需改 config.yaml 的 rag.embedding，无需改动本文件。

    存储结构:
      APP_DATA_DIR/derived/chroma/
        └── collection "papers"
              ├── embedding: 维度由 embedder 决定（当前默认 384 维）
              ├── metadata: {paper_id, title, chunk_index, char_start, char_end}
              └── document: 块文本
    """

    COLLECTION_NAME = "papers"

    # Lexical cache defaults, so a partially constructed store (tests, protocol
    # stubs) can still call ``query_lexical``.
    _lexical_index: dict | None = None
    _lexical_revision = 0
    _lexical_signature: tuple[int, int] | None = None
    _lexical_lock = threading.Lock()

    def __init__(
        self,
        persist_dir: str | None = None,
        *,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dimensions: int = 0,
        embedding_max_length: int = 0,
        embedding_batch_size: int = 32,
        embedding_device: str = "cpu",
        embedder: Embedder | None = None,
        collection_name: str = DEFAULT_COLLECTION_NAME,
        reranker_enabled: bool = False,
        reranker_model: str = DEFAULT_RERANKER_MODEL,
        reranker_candidate_limit: int = HYBRID_CANDIDATE_LIMIT,
        reranker_max_length: int = 0,
        relation_store: Any | None = None,
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

        # 嵌入后端可插拔：未显式注入时按配置名构建。
        # 默认后端（ChromaDB 内置 ONNX）在首次调用时才真正加载权重，之后本地缓存。
        self._embed_fn: Embedder = embedder or build_embedder(
            embedding_model,
            dimensions=embedding_dimensions,
            max_length=embedding_max_length,
            batch_size=embedding_batch_size,
            device=embedding_device,
        )
        self.embedding_model_name = self._embed_fn.model_name
        self.embedding_dimensions = self._embed_fn.dimensions
        self.embedding_max_length = self._embed_fn.max_length
        log.info(
            "embedding backend ready: %s (%s dims / %s tokens)",
            self.embedding_model_name,
            self.embedding_dimensions,
            self.embedding_max_length,
        )

        # 初始化 ChromaDB（持久化模式）
        self._chroma = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

        # 获取或创建 collection（注入嵌入函数）
        self.collection_name = collection_name or DEFAULT_COLLECTION_NAME
        self._collection = self._chroma.get_or_create_collection(
            name=self.collection_name,
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
        # Truncation length for the cross-encoder.  Most chunks are far shorter
        # than the model's 512 ceiling, so trimming it is nearly free — measured
        # 512 -> 256 cut 20-candidate reranking by 30%.
        self._reranker_max_length = max(64, int(reranker_max_length or 0) or 512)
        self._reranker = None
        self._reranker_load_attempted = False
        self._reranker_lock = threading.Lock()
        # A lightweight, user-confirmed relation store.  It is deliberately
        # optional so the vector store stays usable in isolation and in tests.
        self.relation_store = relation_store
        # Injected at agent startup; None means relation review stays disabled.
        self.relation_reviewer = None
        # Lexical retrieval used to re-read and re-tokenize the whole corpus on
        # every request (~160 ms of the ~840 ms hybrid query).  A postings index
        # is built once and reused until the corpus changes.  Declared on the
        # class so partially constructed stores (tests, protocol stubs) work.
        self._lexical_index: dict | None = None
        self._lexical_revision = 0
        self._lexical_signature: tuple[int, int] | None = None

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

        return self._index_chunks(
            self._fit_chunks(chunk_text(text)), title=title, paper_id=paper_id,
        )

    def index_chunks(
        self,
        chunks: list[dict],
        *,
        title: str = "Unknown",
        paper_id: Optional[str] = None,
    ) -> str:
        """Index pre-built chunks (used by the migration script).

        与 index_paper 的区别只是跳过分块；token 守卫仍会执行。
        """
        if not chunks:
            return ""
        return self._index_chunks(self._fit_chunks(list(chunks)), title=title, paper_id=paper_id)

    def _fit_chunks(self, chunks: list[dict]) -> list[dict]:
        """Re-split chunks the embedding model would otherwise truncate.

        分块是按字符切的，而模型按 token 截断，两者无法换算（密度波动约 3.5 倍），
        所以入库前必须用模型自己的分词器复核一次。
        """
        tokenize = getattr(self._embed_fn, "tokenize", None)
        if tokenize is None:
            return chunks
        return limit_chunks_to_tokens(chunks, tokenize, self.embedding_max_length)

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
        chunks = self._fit_chunks(list(document_map.get("chunks") or []))
        resolved_id = self._index_chunks(chunks, title=resolved_title, paper_id=resolved_id)
        self._queue_relation_review(resolved_id, resolved_title, document_map)
        return resolved_id

    def _queue_relation_review(
        self, paper_id: str, title: str, document_map: dict[str, Any],
    ) -> None:
        """Ask the reviewer to examine only this newly indexed paper.

        Queued after indexing so a failed index never leaves a job behind, and
        deliberately fire-and-forget: review must not slow down or fail import.
        """
        reviewer = getattr(self, "relation_reviewer", None)
        if reviewer is None or not paper_id:
            return
        try:
            from paper_relation_review import paper_fingerprint

            fingerprint = paper_fingerprint(
                title=title,
                chunk_count=len(list(document_map.get("chunks") or [])),
                source_file=str(document_map.get("source_file") or ""),
            )
            reviewer.enqueue(paper_id, fingerprint)
        except Exception as exc:  # noqa: BLE001 - review is best-effort
            log.warning(
                "论文关系审查未入队 (exc_type=%s, exc=%s)",
                type(exc).__name__, exc,
            )

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
            page_value = int(page) if isinstance(page, int) and page > 0 else -1
            chunk_page_end = chunk.get("page_end", page)
            page_end_value = (
                int(chunk_page_end)
                if isinstance(chunk_page_end, int) and chunk_page_end >= page_value > 0
                else page_value
            )
            metadatas.append({
                "paper_id": resolved_id,
                "title": title,
                "section": str(chunk.get("section") or "未标注"),
                "chunk_index": index,
                "char_start": start,
                "char_end": end,
                "page": page_value,
                "page_end": page_end_value,
                "element_id": str(chunk.get("id") or ""),
                "element_kind": str(chunk.get("kind") or "text"),
                "indexed_at": datetime.now().isoformat(),
            })
            texts.append(text)
            chunk_ids.append(f"{resolved_id}_chunk_{index}")
            char_pos = end + 2

        self._collection.add(ids=chunk_ids, documents=texts, metadatas=metadatas)
        self._invalidate_lexical_index()
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
            n_results=min(collection_count, top_k * SEMANTIC_CANDIDATE_FACTOR),
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        # 整理结果 + 章节加权（Method/Experiments 优先）
        results = []
        if raw["ids"] and raw["ids"][0]:
            for i, doc_id in enumerate(raw["ids"][0]):
                meta = raw["metadatas"][0][i] if raw["metadatas"] and raw["metadatas"][0] else {}
                dist = raw["distances"][0][i] if raw["distances"] and raw["distances"][0] else 0
                sec = meta.get("section", "未标注")
                # 章节加权：方法/实验类章节给一点距离折扣（余弦距离越小越近）
                weight_penalty = (
                    SECTION_PRIORITY_BONUS
                    if any(term in sec.lower() for term in SECTION_PRIORITY_TERMS)
                    else 0
                )
                results.append({
                    "text": raw["documents"][0][i] if raw["documents"] and raw["documents"][0] else "",
                    "section": sec,
                    "title": meta.get("title", "Unknown"),
                    "paper_id": meta.get("paper_id", ""),
                    "chunk_index": meta.get("chunk_index", 0),
                    "char_start": meta.get("char_start", 0),
                    "char_end": meta.get("char_end", 0),
                    "page": meta.get("page", -1),
                    "page_end": meta.get("page_end", meta.get("page", -1)),
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

    def start_embedding_warmup(self) -> Future | None:
        """Start loading the local query encoder before the worker handles runs.

        The same one-worker executor and active-future marker are used by
        interactive retrieval.  While startup warming is underway, requests
        retain the existing lexical fallback instead of competing for a second
        encoder initialization.
        """
        with self._query_lock:
            active = self._active_query
            if active is not None and not active.done():
                return None
            future = self._query_executor.submit(self._warm_embedding)
            self._active_query = future

            def _clear_finished(done: Future) -> None:
                with self._query_lock:
                    if self._active_query is done:
                        self._active_query = None

            future.add_done_callback(_clear_finished)
            return future

    def _warm_embedding(self) -> None:
        """Force the first encoder call without querying or mutating papers."""
        self._embed_fn(["Research Agent startup embedding warmup."])

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
        """Tokenize text for the small local BM25 index.

        Delegates to ``papers.lexical``; tests and benchmarks still reach this
        method, so the name stays as the compatibility seam.
        """
        return lexical_tokenize(text)

    @classmethod
    def _query_terms(cls, query_text: str) -> list[str]:
        """提取适合中英文论文文本的去重检索词。"""
        return lexical_query_terms(query_text)

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
                self._reranker = load_cross_encoder(
                    self._reranker_model_name,
                    max_length=self._reranker_max_length,
                    local_files_only=True,
                )
            except Exception as exc:
                log.warning(
                    "RAG 重排器未启用 (exc_type=%s, exc=%s)",
                    type(exc).__name__, exc,
                )
        return self._reranker

    def _rerank(self, query_text: str, candidates: list[dict]) -> list[dict]:
        """Use a cross-encoder to reorder only the already retrieved candidates.

        The ordering and the degradation path live in ``papers.rerank``; only the
        model instance and its lock stay here, because those are per-store state.
        """
        reranker = self._get_reranker()
        if reranker is None or not candidates:
            return candidates
        return rerank_with(query_text, candidates, reranker.predict)

    @staticmethod
    def _sort_hybrid_results(results: list[dict]) -> list[dict]:
        return sorted(
            results,
            key=lambda item: (
                -float(item.get("hybrid_score", 0.0)),
                min(item.get("semantic_rank", 99_999), item.get("keyword_rank", 99_999)),
                item.get("title", ""),
                item.get("chunk_index", 0),
            ),
        )

    @staticmethod
    def _relation_seed_ids(ranked: list[dict]) -> list[str]:
        """Use only already relevant first-pass papers as one-hop relation seeds."""
        seeds = []
        for item in ranked:
            paper_id = str(item.get("paper_id") or "").strip()
            if paper_id and paper_id not in seeds:
                seeds.append(paper_id)
            if len(seeds) >= RELATION_EXPANSION_SEED_LIMIT:
                break
        return seeds

    @staticmethod
    def _attach_relation_hint(item: dict, hint: dict[str, Any]) -> None:
        """Expose an auditable retrieval hint without changing paper content."""
        item["relation_boost"] = round(float(hint.get("boost") or 0.0), 6)
        item["relation_ids"] = list(hint.get("relation_ids") or [])
        item["relation_types"] = list(hint.get("relation_types") or [])
        item["relation_seed_paper_ids"] = list(hint.get("seed_paper_ids") or [])
        item["relation_seed_titles"] = list(hint.get("seed_titles") or [])

    def _expand_relation_candidates(
        self,
        query_text: str,
        fused: dict[tuple[str, str, int], dict],
        ranked: list[dict],
        *,
        candidate_k: int,
        section: Optional[str],
    ) -> None:
        """Add only one-hop, topic-matched candidates from confirmed relations.

        Relation traversal never replaces retrieval: a related paper contributes
        only chunks that also score in semantic or lexical retrieval.  The
        semantic cutoff is the first-pass candidate boundary, so a disconnected
        paper's merely "least bad" chunk cannot enter because of a relation.
        """
        relation_store = getattr(self, "relation_store", None)
        if relation_store is None or not ranked:
            return
        seeds = self._relation_seed_ids(ranked)
        if not seeds:
            return
        try:
            hints = relation_store.related_paper_boosts(seeds)
        except Exception as exc:
            log.warning(
                "论文关系检索已跳过 (exc_type=%s, exc=%s)",
                type(exc).__name__, exc,
            )
            return
        hints = {
            paper_id: hint for paper_id, hint in hints.items()
            if paper_id not in set(seeds)
        }
        if not hints:
            return
        related_ids = sorted(hints)[:RELATION_EXPANSION_MAX_PAPERS]
        if not related_ids:
            return

        # Existing results can receive the small, explainable tie-break signal.
        for item in fused.values():
            hint = hints.get(str(item.get("paper_id") or ""))
            if hint:
                self._attach_relation_hint(item, hint)
                item["hybrid_score"] = round(
                    float(item.get("hybrid_score", 0.0)) + float(hint["boost"]), 6,
                )
                item["_relation_score_applied"] = True

        try:
            related_semantic = self.query(
                query_text, candidate_k, paper_ids=related_ids, section=section,
            )
            related_lexical = self.query_lexical(
                query_text, candidate_k, paper_ids=related_ids, section=section,
            )
        except Exception as exc:
            log.warning(
                "论文关系候选扩展已跳过 (exc_type=%s, exc=%s)",
                type(exc).__name__, exc,
            )
            return

        semantic_distances = [
            float(item["distance"]) for item in ranked
            if item.get("distance") is not None
        ]
        semantic_cutoff = max(semantic_distances) if semantic_distances else None

        def merge_related(source: str, candidates: list[dict]) -> None:
            for rank, candidate in enumerate(candidates, start=1):
                if source == "semantic" and semantic_cutoff is not None:
                    distance = candidate.get("distance")
                    if distance is None or float(distance) > semantic_cutoff:
                        continue
                paper_id = str(candidate.get("paper_id") or "")
                hint = hints.get(paper_id)
                if hint is None:
                    continue
                key = self._result_key(candidate)
                # A chunk created here reached the results only through a
                # relation, so it must stay distinguishable from a direct hit.
                is_expansion_only = key not in fused
                item = fused.setdefault(key, {
                    **candidate,
                    "hybrid_score": 0.0,
                    "retrieval": "hybrid",
                })
                if is_expansion_only:
                    item["relation_expanded"] = True
                # This is a secondary retrieval source, not an extra normal
                # RRF vote.  It cannot dominate a directly retrieved answer.
                extra_score = RELATION_EXPANSION_RRF_FACTOR / (RRF_K + rank)
                item["hybrid_score"] = round(
                    float(item.get("hybrid_score", 0.0)) + extra_score, 6,
                )
                item[f"relation_{source}_rank"] = rank
                self._attach_relation_hint(item, hint)

        merge_related("semantic", related_semantic)
        merge_related("keyword", related_lexical)
        for item in fused.values():
            hint = hints.get(str(item.get("paper_id") or ""))
            if hint is not None and "relation_boost" in item:
                # New candidates get their one bounded relation adjustment here;
                # existing candidates were adjusted before candidate expansion.
                if "_relation_score_applied" not in item:
                    item["hybrid_score"] = round(
                        float(item.get("hybrid_score", 0.0)) + float(hint["boost"]), 6,
                    )
                    item["_relation_score_applied"] = True
            item.pop("_relation_score_applied", None)

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

        Every exit path is timed into one of four stages.  That includes the
        early returns: skipping them would bias the averages, and the degraded
        paths (huge corpus, lexical failure) are exactly when an operator wants
        to see that a stage stopped being reached.
        """
        top_k = max(1, min(int(top_k), 20))
        candidate_k = min(
            HYBRID_CANDIDATE_LIMIT,
            max(
                top_k * HYBRID_CANDIDATE_FACTOR,
                getattr(self, "_reranker_candidate_limit", 0)
                if getattr(self, "_reranker_enabled", False) else 0,
            ),
        )
        with StageTimer("semantic"):
            semantic = self.query(query_text, candidate_k, paper_ids, section)
        collection_count = self._collection.count()
        if collection_count > HYBRID_LEXICAL_MAX_CHUNKS:
            return [
                {**item, "retrieval": "semantic"}
                for item in semantic[:top_k]
            ]

        try:
            with StageTimer("lexical"):
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
        ranked = self._sort_hybrid_results(results)
        # Paper relations are a post-hybrid one-hop expansion.  Explicitly
        # scoped retrieval must remain scoped, so it never walks outside its
        # supplied paper IDs.
        # An empty scope is still a scope: never widen it into an unscoped
        # search that walks relations across the whole library.
        with StageTimer("fusion"):
            if not paper_ids:
                self._expand_relation_candidates(
                    query_text, fused, ranked, candidate_k=candidate_k, section=section,
                )
                ranked = self._sort_hybrid_results(list(fused.values()))
        rerank_limit = (
            getattr(self, "_reranker_candidate_limit", 0)
            if getattr(self, "_reranker_enabled", False) else 0
        )
        if rerank_limit:
            with StageTimer("rerank"):
                reranked = self._rerank(query_text, ranked[:rerank_limit])
        else:
            reranked = ranked
        return reranked[:top_k]

    def _invalidate_lexical_index(self) -> None:
        """Drop the cached BM25 structures after any corpus mutation."""
        self._lexical_revision += 1
        with self._lexical_lock:
            self._lexical_index = None
            self._lexical_signature = None

    def _build_lexical_index(self) -> dict:
        """Build postings once so a query only touches the terms it asks for.

        Scanning and tokenizing every chunk per request measured ~160 ms on the
        1158-chunk corpus, which is most of the hybrid latency once the encoder
        is fast.  The postings map a term to ``{position: term frequency}``.

        Only the document *source* stays here: the postings are produced by
        ``papers.lexical``, which is why that module needs no vector store.
        """
        return build_lexical_index(lexical_documents(self._collection))
    def _get_lexical_index(self) -> dict | None:
        """Return the cached postings index, rebuilding it after a corpus change.

        ``None`` means the corpus is past the interactive scan budget, so the
        caller keeps the original streaming behaviour instead.
        """
        if self._collection.count() > HYBRID_LEXICAL_MAX_CHUNKS:
            return None
        signature = (self._collection.count(), self._lexical_revision)
        if self._lexical_index is not None and self._lexical_signature == signature:
            return self._lexical_index
        with self._lexical_lock:
            signature = (self._collection.count(), self._lexical_revision)
            if self._lexical_index is not None and self._lexical_signature == signature:
                return self._lexical_index
            index = self._build_lexical_index()
            self._lexical_index = index
            self._lexical_signature = signature
            return index

    def _query_lexical_indexed(
        self,
        index: dict,
        terms: list[str],
        top_k: int,
        paper_ids: Optional[list[str]],
        section: Optional[str],
    ) -> list[dict]:
        """Score only the chunks the query terms actually occur in."""
        return score_lexical_index(index, terms, top_k, paper_ids, section)

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
        index = self._get_lexical_index()
        if index is not None:
            return self._query_lexical_indexed(index, terms, top_k, paper_ids, section)
        return self._query_lexical_scan(terms, top_k, paper_ids, section)

    def _query_lexical_scan(
        self,
        terms: list[str],
        top_k: int,
        paper_ids: Optional[list[str]],
        section: Optional[str],
    ) -> list[dict]:
        """Full-corpus fallback for collections past the interactive budget."""
        return scan_lexical(
            lexical_documents(self._collection), terms, top_k, paper_ids, section,
        )

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
            self._invalidate_lexical_index()
        relation_store = getattr(self, "relation_store", None)
        if relation_store is not None:
            try:
                relation_store.remove_paper(paper_id)
            except Exception as exc:
                log.warning(
                    "已删除论文，但未能清理关系边 (exc_type=%s, exc=%s)",
                    type(exc).__name__, exc,
                )
        reviewer = getattr(self, "relation_reviewer", None)
        if reviewer is not None:
            try:
                reviewer.store.cancel(paper_id)
                reviewer.store.drop_paper(paper_id)
            except Exception as exc:  # noqa: BLE001 - deletion must not fail on derived state
                log.warning(
                    "已删除论文，但未能清理关系审查状态 (exc_type=%s, exc=%s)",
                    type(exc).__name__, exc,
                )
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
                    "page_end": meta.get("page_end", meta.get("page", -1)),
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
    """空操作存储 — ChromaDB 离线时保持 Agent 可用。

    实现 :class:`PaperStoreProtocol` 的全部方法。
    每个方法都返回"空但类型正确"的值，避免调用方拿到 None 后崩溃；
    新增协议方法时必须同步补这里（有测试校验，见 tests/test_core.py）。
    """
    paper_count = 0
    chunk_count = 0

    def query(self, *a, **kw): return []
    def query_hybrid(self, *a, **kw): return []
    def query_with_timeout(self, *a, **kw): return [], None
    def start_embedding_warmup(self): return None
    def index_paper(self, *a, **kw): return ""
    def index_document_map(self, *a, **kw): return ""
    def list_papers(self): return []
    def delete_paper(self, *a, **kw): return 0
    def get_paper_chunks(self, *a, **kw): return []


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
