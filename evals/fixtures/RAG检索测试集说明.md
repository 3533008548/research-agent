# RAG 检索测试集说明

基于当前已索引的 17 篇论文（788 个 chunk）生成，全部 label 取自真实 chunk，无一编造。

## 文件

| 文件 | 用途 |
|---|---|
| `rag_retrieval_cases.json` | **中文主集**，34 条。query 为中文表述 + 英文术语，贴近真实提问 |
| `rag_retrieval_cases_en.json` | **英文对照集**，34 条。label 完全相同，仅 query 改为英文 |

两集合 label 一致，因此可直接对比——用于量化"中文提问相对于英文提问损失多少召回"。

## 生成方式

```bash
# 1. 采样真实 chunk 并生成骨架（label 已填好，query 待写）
python scripts/generate_rag_eval_cases.py --per-paper 2 --output skeleton.json

# 2. 填好 query 后评测
python scripts/run_rag_retrieval_eval.py --cases evals/fixtures/rag_retrieval_cases.json
```

label 的三个字段都必须真实，否则评测永远无法命中：

- `paper_title`：归一化后必须等于检索结果里的标题
- `chunk_index`：必须精确
- `text_contains`：必须是该 chunk 文本的连续子串

**注意**：PDF 里的换行连字符会让 `deadline- aware` 这样的写法出现（中间有空格），
`text_contains` 必须照抄原文形式，写成 `deadline-aware` 会匹配失败。

## 基线数据（timed_hybrid，k=1/3/5）

| query 语言 | 段落级 R@1 | R@3 | R@5 | MRR | 论文级 R@5 |
|---|---|---|---|---|---|
| 中文 | 11.8% | 20.6% | 32.4% | 0.183 | 82.3% |
| 英文 | 50.0% | 73.5% | **79.4%** | 0.615 | **97.1%** |

检索延迟 P50≈355ms，无降级。

### 结论：跨语言检索损失非常大

段落级召回从 79.4% 掉到 32.4%，MRR 从 0.61 掉到 0.18——**中文提问只能拿到英文提问约 40% 的段落级召回**。
论文级召回差距小得多（82.3% vs 97.1%），说明"找到对的论文"基本没问题，
问题出在"从论文里定位到对的段落"。

根因：默认 embedding 是 ChromaDB 内置的 `all-MiniLM-L6-v2`，纯英文模型，中文语义向量质量差。

### 三种检索模式的区分度（中文集）

| 模式 | 段落级 R@5 | 论文级 R@5 |
|---|---|---|
| keyword | 32.4% | 76.5% |
| hybrid | 29.4% | 79.4% |
| timed_hybrid | 29.4% | 79.4% |

三者差距不大，说明当前瓶颈在 embedding 质量而非融合策略——**换模型比调融合策略收益大得多**。

## 可选的改进方向（按预期收益排序）

1. **换多语言 embedding 模型**：如 `paraphrase-multilingual-MiniLM-L12-v2` 或 `bge-small-zh-v1.5`。
   这是唯一能直接解决根因的做法，预期把中文段落级召回拉到 60-75%。
2. **查询翻译**：检索前把中文 query 译成英文再编码。不改模型，但增加一次调用延迟。
3. **中文查询扩展**：用论文标题/术语表做中文→英文术语映射后拼接查询。成本低，收益中等。

改完之后**用这两个集合重跑**即可量化收益——这正是建这个测试集的目的。

## 维护约定

- 换论文后重跑生成脚本，重新填写 query；旧 case 的 `chunk_index` 会失效，不要沿用。
- 报告默认写入 `evals/reports/`（已被 Git 忽略），不要提交。
- 新增 case 时保持中英两集 label 同步，否则对照失效。
