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
import uuid
import json
from pathlib import Path
from typing import Optional
from datetime import datetime

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
except ImportError:
    chromadb = None

from chromadb.utils import embedding_functions


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
      chroma_data/
        └── collection "papers"
              ├── embedding: 384维向量 (all-MiniLM-L6-v2)
              ├── metadata: {paper_id, title, chunk_index, char_start, char_end}
              └── document: 块文本
    """

    COLLECTION_NAME = "papers"

    def __init__(self, persist_dir: str = "./chroma_data"):
        if chromadb is None:
            raise ImportError(
                "需要安装 chromadb:\n   pip install chromadb"
            )

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
        if not text or not text.strip():
            return ""

        paper_id = paper_id or f"paper_{uuid.uuid4().hex[:12]}"
        chunks = chunk_text(text)

        if not chunks:
            return paper_id

        # 构建文档、元数据和ID
        texts = [c["text"] for c in chunks]
        metadatas = []
        chunk_ids = []
        for chunk in chunks:
            metadatas.append({
                "paper_id": paper_id,
                "title": title,
                "section": chunk.get("section", "未标注"),
                "chunk_index": chunk["index"],
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
                "indexed_at": datetime.now().isoformat(),
            })
            chunk_ids.append(f"{paper_id}_chunk_{chunk['index']}")

        # 存入 ChromaDB（嵌入由 DefaultEmbeddingFunction 自动处理）
        self._collection.add(
            ids=chunk_ids,
            documents=texts,
            metadatas=metadatas,
        )

        return paper_id

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
        if self._collection.count() == 0:
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
            n_results=top_k * 3,  # 多取一些用于章节加权
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
                    "distance": round(max(dist - weight_penalty, 0), 4),
                })

        return results

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
    def index_paper(self, *a, **kw): return ""
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
