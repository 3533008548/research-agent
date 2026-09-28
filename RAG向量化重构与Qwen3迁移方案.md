# RAG 向量化重构与 Qwen3 迁移方案

选定嵌入模型 **Qwen3-Embedding** 后的整体优化方案，围绕六个目标：
架构清晰、可读性强、运行速度、可维护、用户体验、功能提升。

## 实施状态

| 阶段 | 状态 | 实测结果 |
|---|---|---|
| P0 配置化与可插拔 | ✅ 完成 | `embeddings.py` + `config.yaml` 的 `rag.embedding.*`；197 测试通过 |
| P1 Qwen3 接入 | ✅ 模型已下载，❌ 实测后**不切换** | 权重在 `runtime/derived/cache/huggingface`（1.2 GB，离线可加载）。对照评测显示收益在噪声内，见下方"Qwen3 对照实测" |
| P1 按 token 分块 | ✅ 完成 | `limit_chunks_to_tokens()`，入库前用模型分词器复核 |
| P1 papers_v2 迁移 | ✅ 完成 | **788 → 1158 块；截断 45% → 0%；丢弃 token 13.4% → 0%**；`papers` 保留作回滚备份 |
| P2 嵌入通道提速 | ✅ 完成 | 复用 ONNX 会话 + 按批内最长填充；**单条查询编码 595 ms → 2.5 ms**，数值与 ChromaDB 完全一致（最大绝对误差 0.0），无需重建索引 |
| P2 词法检索倒排化 | ✅ 完成 | 倒排表缓存，**lexical 168 ms → 19 ms**；BM25 打分与全量扫描逐条一致 |
| P2.5 词法索引持久化 | ⬜ 未开始 | 索引目前是进程内缓存，冷启动首次查询仍需建表 |
| P3 类拆分 | ⬜ 未开始 | `PaperStore` 仍约 800 行、8 项职责 |
| P4 体验与功能增强 | ⬜ 未开始 | 进度反馈、预热、指令感知、Qwen3-Reranker |

> 说明：P1 的"消除截断"收益已通过**当前模型 + 按 token 分块**拿到，
> 不依赖 Qwen3 是否下载成功。Qwen3 带来的是检索质量提升，属于额外收益。

### 检索提速实测（2026-09-27，1158 块真实语料 / 34 条标注用例）

一直以为瓶颈是词法检索，实测拆开后完全是另一回事（`python -m scripts.profile_retrieval`）：

| 阶段 | 改前 | 改后 | 提速 |
|---|---|---|---|
| semantic（其中查询编码 627 ms） | 673 ms | 23 ms | 29× |
| lexical（其中全库 get 79 ms + 全量分词 20 ms） | 168 ms | 19 ms | 8.8× |
| **query_hybrid 合计** | **842 ms** | **50 ms** | **16.7×** |
| 单条查询编码 | 595 ms | 2.5 ms | 238× |
| 真实块入库吞吐 | 34.5 块/秒 | 57.1 块/秒 | 1.65× |

**根因（两个都是 ChromaDB 自带封装的开销，与模型无关）：**

1. `DefaultEmbeddingFunction.__call__` 每次都 `new ONNXMiniLM_L6_V2()`，
   即每次重新读 90 MB 图 + 重跑图优化：约 530 ms/次，真正的推理只有约 96 ms。
2. 它的分词器被写死为 `enable_padding(length=256)`，12 个 token 的查询也要按
   256 位算 —— 约 20 倍无用算力。（截断上限确实是 256，P1 的按 token 切块没有被浪费。）

**修复**：`MiniLMOnnxEncoder` 持有单个 ONNX 会话，并按批内最长序列填充。
填充位 `attention_mask=0`、不参与 mean pooling，所以**数学上完全等价**——
实测与 ChromaDB 输出最大绝对误差 **0.0**、余弦 1.0，**不需要重建任何索引**。
权重缺失时自动回退到 ChromaDB 原通道。

**副作用核对**：召回质量零变化，论文级 PaperR@1 仍为 **91.2%**（34 条用例），
延迟 P50 810 ms → 47.8 ms。

### Qwen3 对照实测（2026-09-27，CPU，34 条人工标注英文用例）

`papers_qwen3_probe` 与 `papers_v2` **块集合完全相同**（17 篇 / 1158 块，1:1），唯一变量是嵌入模型。

| 指标 | MiniLM-L6（papers_v2） | Qwen3-0.6B（probe） | 差异 |
|---|---|---|---|
| 论文级 PaperR@1 | 91.2% | 94.1% | +1 个用例（31/34 → 32/34） |
| 段落级 R@1 / MRR | 5.9% / 0.059 | 5.9% / 0.074 | 均失效，见下 |
| 检索延迟 P50 | 810 ms | 482 ms | 噪声大，不足以下结论 |
| **全库重建耗时** | **约 2 分钟** | **约 85 分钟** | **慢约 48 倍** |

结论：**不切换**。论文级提升只有 1/34，落在噪声范围内；代价是入库慢 48 倍、
查询侧也要跑 0.6B 模型。语料实测 100% 为英文，Qwen3 的多语言优势用不上。
权重保留在 runtime 里，改 `config.yaml` 一行仍可随时切换。

> ⚠️ **顺带发现（比模型选择更要紧）**：P1 按 token 重建 `papers_v2` 后，
> **34 个标注用例的段落级标签已与新的块结构脱节** —— 旧 `papers` 集合
> 段落级 R@1=50.0% / R@3=73.5%，`papers_v2` 掉到 5.9%。
> **2026-09-28 修正**：主因不是标注过期，而是 **256 token 上限把块切碎了**
> （详见下方"token 上限实测"）——放开到 512 后段落级 R@1 回升到 38.2%。

### token 上限实测（2026-09-28）：256 是约定，512 才是模型上限

起因是用户质疑"token 守卫切碎了语义，变相增加召回/重排压力"——这个判断是对的。

1. **原始逻辑块中位 247 token、最大不超过 512**（736 块 / 161343 token）。
   256 上限把它们硬切成 1052 块，平均只剩 153 token/块。
2. **MiniLM 的 ONNX 位置编码是 512，不是 256**：512 token 输入正常，
   514 报 `512 by 514` broadcast 失败。256 是 sentence-transformers/ChromaDB 的约定。
3. 放开到 512 → 736 块，**守卫基本不用切**，回到语义边界切分。

对照评测（17 篇对齐 / 34 条金标 / hybrid / 不开重排）：

| 指标 | papers_v2（256） | 512 探针 | 变化 |
|---|---|---|---|
| 块数 | 1158 | **794** | −31% |
| 段落级 R@1 / R@3 / R@5 | 5.9% / 5.9% / 5.9% | **38.2% / 61.8% / 64.7%** | +32pp |
| MRR | 0.0588 | **0.5025** | ×8.5 |
| 论文级 R@1 | 91.2% | 88.2% | −1 个用例（噪声） |
| 论文级 R@3 / R@5 | 91.2% / 91.2% | **94.1% / 97.1%** | +3 / +6pp |
| P50 | 48.8 ms | 49.3 ms | 持平 |

**结论：512 明显更好**，且块数少 31% 也能减轻重排压力。
`embeddings.py` 已支持 `max_length`（钳制到 512）；切换需重建索引（约 48 秒）。

> ⚠️ **建探针/重建索引必须对齐论文集合**：`papers_v2` 只有 **17 篇**，
> 而 document_map 有 **43 篇**（用户确认那 26 篇有意不进向量库）。
> 用 `scripts/build_length_probe.py --match-collection papers_v2`，
> 否则多出的论文会让评测虚高。

---

## 一、现状诊断（问题 → 代码位置 → 影响）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | `PaperStore` 上帝类：约 800 行、28 个方法，同时承担分块、索引、检索、词法、RRF 融合、rerank、关系扩展、CRUD 共 8 项职责 | `paper_store.py:216-1013` | 架构/可读/可维护 |
| 2 | 嵌入模型**硬编码** `DefaultEmbeddingFunction()`，而 reranker 模型却可从 config 传入 | `paper_store.py:257` vs `research_agent.py:204-210` | 可维护、无法换模型 |
| 3 | `config.yaml` 有 `rag.reranker.model`，**没有 `rag.embedding.*`**，两者不对称 | `config.yaml:27-33` | 可维护、用户体验 |
| 4 | 按**字符**切块；实测 token/字符密度在 0.21–0.73 间波动（3.5 倍），字符数无法预测 token 数 | `paper_store.py:63-215` | 功能（静默截断） |
| 5 | 当前 256 token 上限导致 **45% 切块被截断、整体丢弃 13.4% token** | 实测（见 `scripts/audit_chunks.py`） | 功能、召回质量 |
| 6 | ~~`query_lexical` 每次调用 `collection.get()` 拉全库并**全量分词**（可达 5000 块）~~ → P2 已改为倒排表缓存 | `paper_store.py` | ~~运行速度~~ ✅ 已修（168 ms → 19 ms） |
| 6b | `DefaultEmbeddingFunction` 每次调用重建 ONNX 会话（约 530 ms），且分词器写死填充到 256 位 | `embeddings.py` | ✅ 已修（查询编码 595 ms → 2.5 ms），这才是真正的首要瓶颈 |
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
1. **LexicalIndex 倒排化** ✅ 已完成：一次建倒排表并缓存，语料变更（add/delete）时失效重建；
   每次查询的全库 `get()` + 全量分词降为按词表命中的 O(命中数)。168 ms → 19 ms。
   BM25 统计量仍按"过滤后的候选集"计算，打分与全量扫描逐条一致（有测试守护）。
2. **嵌入通道提速** ✅ 已完成（这才是真正的首要瓶颈，见上方"检索提速实测"）：
   复用 ONNX 会话 + 按批内最长填充，查询编码 595 ms → 2.5 ms。
3. 查询向量缓存（同 query 复用）。
4. 索引持久化：倒排表目前是进程内缓存，冷启动首次查询仍需建表。
5. Qwen3 批处理（与 MiniLM 不同，Qwen3 上增大 batch 确实提速）。
6. 可选：用 MRL 把粗排向量降到 256/512 维，减少内存与搜索耗时。

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
