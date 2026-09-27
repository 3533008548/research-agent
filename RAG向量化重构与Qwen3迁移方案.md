# RAG 向量化重构与 Qwen3 迁移方案

选定嵌入模型 **Qwen3-Embedding** 后的整体优化方案，围绕六个目标：
架构清晰、可读性强、运行速度、可维护、用户体验、功能提升。

## 实施状态

| 阶段 | 状态 | 实测结果 |
|---|---|---|
| P0 配置化与可插拔 | ✅ 完成 | `embeddings.py` + `config.yaml` 的 `rag.embedding.*`；181 测试通过 |
| P1 Qwen3 接入 | ⚠️ 代码就绪，模型未下载 | 本机代理拦截 HF（huggingface 502 / hf-mirror 403）。`Qwen3Embedder` 已实现（含 MRL 降维、指令前缀、不可用时优雅回退），联网后改 config 重跑迁移脚本即可 |
| P1 按 token 分块 | ✅ 完成 | `limit_chunks_to_tokens()`，入库前用模型分词器复核 |
| P1 papers_v2 迁移 | ✅ 完成 | **788 → 1158 块；截断 45% → 0%；丢弃 token 13.4% → 0%**；`papers` 保留作回滚备份 |
| P2 词法检索倒排化 | ⬜ 未开始 | 仍是每次查询全库扫描，规模变大后是首要瓶颈 |
| P3 类拆分 | ⬜ 未开始 | `PaperStore` 仍约 800 行、8 项职责 |
| P4 体验与功能增强 | ⬜ 未开始 | 进度反馈、预热、指令感知、Qwen3-Reranker |

> 说明：P1 的"消除截断"收益已通过**当前模型 + 按 token 分块**拿到，
> 不依赖 Qwen3 是否下载成功。Qwen3 带来的是检索质量提升，属于额外收益。

---

## 一、现状诊断（问题 → 代码位置 → 影响）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | `PaperStore` 上帝类：约 800 行、28 个方法，同时承担分块、索引、检索、词法、RRF 融合、rerank、关系扩展、CRUD 共 8 项职责 | `paper_store.py:216-1013` | 架构/可读/可维护 |
| 2 | 嵌入模型**硬编码** `DefaultEmbeddingFunction()`，而 reranker 模型却可从 config 传入 | `paper_store.py:257` vs `research_agent.py:204-210` | 可维护、无法换模型 |
| 3 | `config.yaml` 有 `rag.reranker.model`，**没有 `rag.embedding.*`**，两者不对称 | `config.yaml:27-33` | 可维护、用户体验 |
| 4 | 按**字符**切块；实测 token/字符密度在 0.21–0.73 间波动（3.5 倍），字符数无法预测 token 数 | `paper_store.py:63-215` | 功能（静默截断） |
| 5 | 当前 256 token 上限导致 **45% 切块被截断、整体丢弃 13.4% token** | 实测（见 `scripts/audit_chunks.py`） | 功能、召回质量 |
| 6 | `query_lexical` 每次调用 `collection.get()` 拉全库并**全量分词**（可达 5000 块） | `paper_store.py:870-896` | 运行速度（首要瓶颈） |
| 7 | 模块 docstring 仍写"SentenceTransformer"，实际是 ChromaDB ONNX，与事实不符 | `paper_store.py:1-15` | 可读性 |
| 8 | `NoOpStore` 手工复制一遍接口方法，接口变更需同步改两处 | `paper_store.py:1015-1024` | 可维护 |
| 9 | 魔数散落：章节加权 0.05、`top_k*3`/`top_k*4`、BM25 的 k1=1.5/b=0.75、系数 2.5 | 多处 | 可读性 |

---

## 二、目标架构（模块拆分）

```
调用方 (tools / agent / routes)
        ↓
Retriever 编排层：多路召回 → RRF 融合 → 精排
        ↓
┌──────────────┬─────────────────┬──────────────┐
│ VectorIndex  │ LexicalIndex    │ Reranker     │
│ (Chroma 封装) │ (倒排 + BM25)   │ (Qwen3-Rer.) │
└──────────────┴─────────────────┴──────────────┘
        ↓
Embedder（Protocol）→ Qwen3Embedder / MiniLMEmbedder（可插拔）
        ↓
Chunker（按 token 计数，保留公式保护） + ChromaDB 持久化
```

各模块单一职责：

- **Embedder**（新）：抽象 `encode(texts) -> ndarray`，暴露 `dimensions` / `max_length`。
  Qwen3 与旧 MiniLM 各一个实现，靠配置切换 —— 换模型不再改业务代码。
- **Chunker**（拆出）：按 token 切，保留现有的公式区间保护与句子边界对齐逻辑。
- **VectorIndex**：只负责 Chroma 集合的增删查与元数据过滤。
- **LexicalIndex**（新）：维护增量倒排表，替代每查一次全库扫描。
- **Retriever**：召回融合（RRF）+ 精排编排。
- **PaperRepository**：论文级 CRUD。

对外统一由 `Store` Protocol 约束，`NoOpStore` 实现该协议，消除手工复制。

---

## 三、配置化设计（config.yaml）

```yaml
rag:
  enabled: true
  embedding:
    model: Qwen/Qwen3-Embedding-0.6B   # 0.6B=1024维 / 4B=2560 / 8B=4096
    dimensions: 1024                    # MRL：可降到 512 甚至 256
    max_length: 8192                    # 实际可到 32k，按需设
    batch_size: 64                      # Qwen3 上批处理才真正有效
    device: cpu                         # 有 GPU 时改 cuda
  reranker:
    enabled: true
    model: Qwen/Qwen3-Reranker-0.6B     # 与嵌入同族，效果一致
    candidate_limit: 20
```

`Config` 增加对应字段，`PaperStore` 构造函数接收 `embedding_model` / `embedding_dimensions`，
与现有 `reranker_*` 参数保持同一风格。

---

## 四、分阶段实施（按风险从低到高）

### P0 — 配置化与可插拔（低风险，建议先做）
1. 抽出 `Embedder` Protocol + `MiniLMEmbedder`（包装现有实现，行为不变）。
2. `config.yaml` 增加 `rag.embedding.*`，`Config` 与构造函数透传。
3. 修正模块 docstring；把魔数提取为命名常量（`SECTION_BONUS`、`BM25_K1`、`BM25_B` 等）。
4. 定义 `Store` Protocol，`NoOpStore` 改为实现它。
> 此阶段**不改变任何行为**，纯结构性改造，可独立回滚。

### P1 — 接入 Qwen3 并迁移集合
1. 新增 `Qwen3Embedder`（首次下载约 0.6–1.2GB，需网络）。
2. 维度 384 → 1024，ChromaDB **必须新建集合**（维度创建时固定）：
   - 建 `papers_v2` → 全量重索引 → 校验条数 → 原子切换 → 保留 `papers` 作回滚备份。
3. 改造 `scripts/reindex_local_papers.py` 支持指定目标集合与模型，作为迁移入口。
4. Chunker 改为按 token 计数（解决 45% 截断）。

### P2 — 速度优化
1. **LexicalIndex 倒排化**：建一次倒排表并增量更新，把每次查询的全库 `get()` + 全量分词
   降为按词表命中的 O(命中数)。这是当前最确定的提速点。
2. 查询向量缓存（同 query 复用）。
3. Qwen3 批处理（与 MiniLM 不同，Qwen3 上增大 batch 确实提速）。
4. 可选：用 MRL 把粗排向量降到 256/512 维，减少内存与搜索耗时。

### P3 — 类拆分重构
按第二节把 `PaperStore` 拆成 6 个模块，保持外部 API 不变，逐步迁移调用点。

### P4 — 体验与功能增强
1. **重建索引进度反馈**：Qwen3 在 CPU 上比 MiniLM 慢约 20–40 倍，必须让用户看到进度与预计耗时。
2. **启动预热**：沿用现有 `start_embedding_warmup` 思路，后台加载，避免首次查询卡顿。
3. **指令感知**：Qwen3 支持给查询加 instruction 前缀（如"检索与该方法相关的论文段落"），
   官方称可带来 1%–5% 提升。
4. **长上下文**：32k 上下文后可按"章节/整段"索引，不再被迫碎切成 1000 字符。
5. Reranker 换同族 `Qwen3-Reranker`，与嵌入模型语义空间更一致。

---

## 五、预期收益

| 维度 | 现状（实测） | 迁移后（预期） |
|---|---|---|
| 截断切块 | 45%（358/788 块） | 0%（8192 上限 > 实测最大 876 token） |
| 丢弃 token | 13.4% | 0% |
| 吞吐 | ~49 块/秒（MiniLM CPU） | 慢 20–40 倍（估算），788 块重建为分钟级 |
| 索引体积 | 1.2 MB（384 维） | 3.2 MB（1024 维）；用 MRL 512 维可减半 |
| 换模型成本 | 需改代码 | 仅改 config |

> 说明：吞吐倍数与 MRL 收益为厂商/公开基准的估算，非本机实测；
> 截断与丢弃比例为 `scripts/audit_chunks.py` 在本机真实语料上测得。

---

## 六、风险与回滚

| 风险 | 应对 |
|---|---|
| 模型体积大、需联网下载 | 预先下载到本地缓存；失败则回退 P0 的 MiniLMEmbedder |
| 维度变化导致集合不兼容 | 并建 `papers_v2`，旧集合保留，切换失败即切回 |
| CPU 上重建变慢 20–40 倍 | 提供进度条；量大时建议 GPU |
| 重构引入回归 | P0 不改行为；每阶段跑 `tests/test_core.py` 与 `scripts/run_rag_retrieval_eval.py` |

---

## 七、验收方式

1. `python scripts/audit_chunks.py` —— 截断率应归零。
2. `python scripts/run_rag_retrieval_eval.py` —— 迁移前后召回指标对比（关键验收）。
3. `python scripts/bench_embedding.py` —— 记录新模型在本机的真实吞吐，替换估算值。
4. `python -m unittest tests.test_core` —— 行为回归。
