# 科研 Agent 评测集

`research_tasks.json` 定义 14 个版本化任务，覆盖论文精读、RAG 检索、跨论文对比、证据约束、每日速递、会话隔离/删除、同模型请求韧性、RAG 初始化降级，以及深度研究证据闭环、每日来源故障恢复、OpenAlex 鉴权边界、交互优先级和长任务取消。

所有任务使用 `fixtures/synthetic_corpus.json` 的合成科研语料，或明确给出模拟状态；不会读取 `runtime/`、个人论文、API Key，也不会调用真实模型 API。因此它适合作为每次重构后的稳定回归基线。

## 从真实问题沉淀 Badcase（不直接复制内容）

网页运行中心可将某个 `run_id` 标记为本地 Badcase 候选。候选库只保留脱敏运行元数据和人工分类，不能直接作为评测集，也不会自动提交到 Git。审核后运行：

```powershell
python scripts/export_badcase_template.py --candidate-id bc-xxxxxxxxxxxx
```

导出的 JSON 刻意不包含真实输入、回答、PDF 或工具结果；请手工填写合成语料、预期行为和稳定断言，然后再将经审核的样例加入版本化评测。这样既能让真实失败驱动改进，也不会把用户数据带进代码库。

## 统一发布质量门禁

日常开发与 CI 推荐只运行下面这一条命令。它会串联 14 项能力基准和 5 项真实编排可靠性回放，生成一个紧凑的通过/失败结论；报告不包含 prompt、论文正文、候选论文或密钥。

```powershell
docker compose exec -T research-agent python -m evals.release_gate --strict
```

需要留档或和上一版比较时：

```powershell
docker compose exec -T research-agent python -m evals.release_gate --write-report --strict
docker compose exec -T research-agent python -m evals.release_gate --compare evals/reports/release-gate-previous.json --write-report --strict
```

门禁报告分别给出 `capability_success_rate`、`runtime_success_rate`、硬规则通过率和可靠性回放 P95 时延。每日来源的单点失败是已覆盖的恢复场景，不会被错误地判为整套门禁失败。

## 使用方式

## 本地 RAG 召回评测（人工金标）

现有能力基准只验证 RAG 工具是否被正确调用、是否可降级；它**不**衡量“正确证据段是否被召回”。如需评估本地论文库，请单独使用段落级召回评测：

```powershell
python scripts/run_rag_retrieval_eval.py `
  --cases runtime/derived/rag_eval/current_local_cases.json `
  --mode timed_hybrid
```

它不会调用模型、下载论文或修改向量库，只读取当前 Chroma 索引。默认报告写入被 Git 忽略的 `evals/reports/rag-retrieval-*.json`。报告分开给出：

- `paper_recall_at_k`：前 k 条中是否出现正确论文，用于定位跨论文选择错误；
- `passage_recall_at_k`：前 k 条中是否出现人工核验的正确块，才是 RAG 证据召回的主指标；
- `MRR`：首个正确证据块的平均倒数排名；
- `latency_ms` 与 `fallback_case_count`：检索体验和降级情况。

金标文件必须包含论文标题、块序号及原文锚点 `text_contains`；只标论文标题会被拒绝。标注应先阅读原始 PDF 或已解析原文，再查看检索结果，避免用当前排序反向制造“正确答案”。真实本地论文的金标文件只放在 `runtime/derived/rag_eval/`，不得提交 Git。

可额外运行关键词对照，以确认混合检索的收益是否真实存在：

```powershell
python scripts/run_rag_retrieval_eval.py `
  --cases runtime/derived/rag_eval/current_local_cases.json `
  --mode keyword `
  --output evals/reports/rag-retrieval-keyword-baseline.json
```

## 能力基准评分

先验证任务清单：

```powershell
python -m evals.benchmark
```

将一次人工或自动运行的结果整理为 JSON 数组。每一项包含：

```json
{
  "task_id": "T01",
  "answer": "Agent 的最终回答",
  "tool_trace": [{"type": "tool_finished", "tool": "query_papers", "duration_ms": 350}],
  "state": {"可选状态断言": "值"},
  "metrics": {"duration_ms": 2100, "first_token_ms": 720, "max_tool_duration_ms": 350, "model_calls": 1},
  "source_stats": {"keyword": {"openalex": {"status": "ok", "count": 3}}}
}
```

再做可重复的硬规则评分：

```powershell
python -m evals.benchmark --results evals/example_results.json
```

`example_results.json` 是评分器的合成格式样例，预期得到 14/14；它不代表真实模型表现。真实评测应保存一次实际 Agent 运行产生的回答、工具轨迹和状态快照，再使用同一命令评分。

`ResearchAgent.get_last_trace()` 可取得一轮脱敏追踪：模型名、总耗时、首 token 时间、工具名/耗时、RAG 是否走关键词候选、错误类型和 token 增量。它不包含 API Key、完整 prompt 或工具原文。`result_from_trace()` 会自动携带模型调用次数；每日任务可额外传入 `source_stats`。对带性能门槛的任务，评分器会校验 `metrics`；例如 T10 在走 RAG 工具时要求其在 8.5 秒内返回关键词候选或语义结果。

T04（鲁棒性评估设计）与 T10（RAG 初始化降级）是工程/方法解释题：若用户没有要求论文、引用或具体实验数据，Agent 可以直接说明项目约束，不必为制造引用调用工具。工具轨迹在这两题是可选证据；其他明确要求取证的任务仍保留强制工具断言。

## 小样本真实模型评测（隔离运行时）

不要在 Python 交互环境中直接对主 Agent 调用 `create_session()` 再执行评测任务。那会把
`真实评测 T*` 会话、checkpoint 和运行记录写入网页正在使用的 `runtime/`，造成会话列表污染。

必须使用统一入口；它默认创建系统临时运行时、默认关闭 RAG，并在结束后删除运行数据库。
只有脱敏后的报告会保存到被 Git 忽略的 `evals/reports/`：

```powershell
python scripts/run_real_eval.py --task T04 --task T10 --task T13
```

先只校验任务和隔离策略、不会调用模型：

```powershell
python scripts/run_real_eval.py --task T04 --task T13 --dry-run
```

T11 会实际执行深度研究；默认仅使用公开来源，避免依赖或修改个人论文库：

```powershell
python scripts/run_real_eval.py --task T11 --research-scope public
```

如需保留某次评测现场复盘，显式指定项目外的目录并保留它：

```powershell
python scripts/run_real_eval.py --task T04 --data-dir D:\temp\research-agent-eval --keep-runtime
```

脚本拒绝使用项目主 `runtime/` 或容器 `/app/runtime`。只有同时传入
`--keep-runtime --allow-production-runtime` 才能绕过此保护；这会污染网页会话，通常不应使用。
脚本只自动记录实际执行可观察到的状态，不会为复杂夹具任务伪造通过状态；仍需人工完成
报告中的 `manual_rubric`。

每日任务完成后可直接保留来源健康指标，而不保存候选论文详情：

```python
from evals.capture import result_from_daily_run

result = result_from_daily_run("T12", daily_result)
```

评分通过后可显式落盘报告：

```powershell
python -m evals.benchmark --results results.json --write-report
```

报告会汇总成功率、硬规则通过率、证据引用可追溯率、平均/P95 耗时、首 token、模型调用次数，以及每日来源请求/失败率。引用可追溯率只检查 `[E1]` 等证据编号覆盖，不替代人工事实核验。

报告写入被 Git 忽略的 `evals/reports/`。将新结果和上一份报告比较时：

```powershell
python -m evals.benchmark --results results.json --compare evals/reports/benchmark-previous.json --write-report
```

输出的 `comparison.deltas` 直接给出成功率、延迟、模型调用和来源失败率的变化，便于每次重构后确认收益与回归。

输出中的 `manual_rubric` 仍需人工按 0–2 分评估事实正确性、引用充分性和表达质量；这样不会把关键词命中误当成高质量回答。面试展示时，应保留每次运行的 JSON 报告，并说明通过数、失败任务和修复动作。

## 真实用户验收集（推荐作为下一阶段基线）

能力基准验证预先定义的系统能力；真实用户验收集则以接近实际使用的研究旅程检查：
研究问题收敛、公开文献研究、趋势梳理、鲁棒性实验设计、研究计划、多轮上下文、会话隔离、每日发现、证据约束问答、密钥边界、韧性、RAG 降级、取消语义、会话删除和复现产物整理。

场景定义在 `user_acceptance_tasks.json`，当前共 19 个，分为：

- `core`：首次可感知的科研问答与研究结论质量；
- `research`：深度研究、证据、实验设计与多轮上下文；
- `workflow`：每日发现、会话和研究产物；
- `safety`：密钥、超时、RAG 降级和取消边界。

它会调用真实模型与公开来源，因此**不进入 CI，也不能替代离线发布门禁**。每次执行都使用临时隔离运行时、默认关闭 RAG；报告仅保留回答、脱敏轨迹、状态摘要和人工评分模板，不保存原始 prompt、运行数据库、候选论文或密钥。

先用核心场景建立第一份基线：

```powershell
python scripts/run_user_acceptance.py --suite core
```

先核对场景选择和隔离策略、不会调用模型或外部来源：

```powershell
python scripts/run_user_acceptance.py --suite core --dry-run
```

深度研究场景使用公开来源；每日发现场景会访问公开论文源，建议拆分运行并记录外部失败：

```powershell
python scripts/run_user_acceptance.py --scenario UA02 --scenario UA11 --research-scope public
python scripts/run_user_acceptance.py --suite workflow
```

运行结束后，在输出报告的每个 `manual_review` 条目中填写 `score`（只能为 `0`、`1`、`2`）和简短的 `notes`：

- 0：不满足，存在明显错误、遗漏、误导或不可接受风险；
- 1：部分满足，基本可用但有可修正缺口；
- 2：满足，准确、可追溯并符合任务边界。

填写后生成带人工汇总的新报告：

```powershell
python scripts/summarize_user_acceptance.py evals/reports/user-acceptance-20260812T000000Z.json
```

与上一份相同版本、已完成评分的报告比较：

```powershell
python scripts/run_user_acceptance.py --suite core --compare evals/reports/user-acceptance-baseline.json
python scripts/summarize_user_acceptance.py evals/reports/user-acceptance-current.json --compare evals/reports/user-acceptance-baseline-reviewed.json
```

自动检查适合发现超时、工具轨迹缺失、证据数量不足、会话边界和配置密钥泄露；人工评分才用于判断事实准确性、引用是否真正支撑结论、相关性和表达质量。优化时优先处理：关键人工项低于 1.5 分、自动检查失败，或相对基线退化的场景。

## 清理历史误入的评测会话

历史版本可能已把 `真实评测 T*` 或编码损坏的 `???? T11 ????` 写入主会话列表。清理脚本
默认只预览匹配对象和关联运行数，不会删除任何数据：

```powershell
python scripts/cleanup_eval_sessions.py
```

确认预览无误后才执行删除。脚本会先对 `checkpoint.db` 做 SQLite 在线备份，再使用项目的
`SessionStore.delete()` 语义清理会话、运行、事件和 checkpoint，并清理对应会话摘要：

```powershell
python scripts/cleanup_eval_sessions.py --apply
```

如需额外指定某条会话，使用精确 ID；不要直接删除 SQLite 表行：

```powershell
python scripts/cleanup_eval_sessions.py --session-id session-xxxxxxxxxxxx --apply
```

## 运行时真实回放（离线）

能力基准验证“给定输入的结果是否满足规则”；`runtime_replay` 单独验证工程可靠性。它会为每个场景创建临时 SQLite 目录，真实执行会话删除、`/research` 已存证据恢复、每日来源解析/候选持久化与取消传播。模型调用和 HTTP 传输边界都由离线替身替换，因此不会读取 `runtime/`、调用真实模型或访问学术 API。

```powershell
docker compose exec research-agent python -m evals.runtime_replay --strict
```

当前包含 5 个场景：`R01` 会话隔离与删除、`R02` 工具轨迹脱敏、`R03` 深度研究恢复、`R04` 每日来源局部失败后恢复、`R05` 取消后保留候选且不发送投递。可只回放某一个故障路径，并固定夹具调度顺序：

```powershell
docker compose exec research-agent python -m evals.runtime_replay --case R04 --seed 42 --strict
```

需要留存可比较报告时显式写入被 Git 忽略的目录；报告仅含通过断言、耗时、来源健康度、种子、运行环境和 Git 提交号（镜像环境可通过 `APP_GIT_REVISION` 注入），不含 prompt、论文候选、工具正文或密钥。

```powershell
docker compose exec research-agent python -m evals.runtime_replay --write-report --strict
docker compose exec research-agent python -m evals.runtime_replay --compare evals/reports/runtime-replay-previous.json --write-report
```
### 评测可选重排器

默认评测只测当前的向量 + BM25 RRF。首次下载由单独命令完成，避免普通查询因网络不可用而卡住；之后可与基线使用相同金标比较：

```powershell
pip install -r requirements.txt
python scripts/warm_reranker.py
python scripts/run_rag_retrieval_eval.py --cases runtime/derived/rag_eval/current_local_cases.json --mode timed_hybrid --ks 1,3,5,10,20 --reranker
```

`--reranker` 只重排混合召回的前 20 条候选，不扩大给模型的上下文；运行时只从本地缓存加载 `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`。确认指标有收益后，再将 `config.yaml` 的 `rag.reranker.enabled` 改为 `true`。
