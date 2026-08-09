# 科研 Agent 评测集

`research_tasks.json` 定义 15 个版本化任务，覆盖论文精读、RAG 检索、跨论文对比、证据约束、科研笔记、每日速递、会话隔离/删除、同模型请求韧性、RAG 初始化降级，以及深度研究证据闭环、每日来源故障恢复、OpenAlex 鉴权边界、交互优先级和长任务取消。

所有任务使用 `fixtures/synthetic_corpus.json` 的合成科研语料，或明确给出模拟状态；不会读取 `runtime/`、个人论文、API Key，也不会调用真实模型 API。因此它适合作为每次重构后的稳定回归基线。

## 统一发布质量门禁

日常开发与 CI 推荐只运行下面这一条命令。它会串联 15 项能力基准和 5 项真实编排可靠性回放，生成一个紧凑的通过/失败结论；报告不包含 prompt、论文正文、候选论文或密钥。

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

`example_results.json` 是评分器的合成格式样例，预期得到 15/15；它不代表真实模型表现。真实评测应保存一次实际 Agent 运行产生的回答、工具轨迹和状态快照，再使用同一命令评分。

`ResearchAgent.get_last_trace()` 可取得一轮脱敏追踪：模型名、总耗时、首 token 时间、工具名/耗时、RAG 是否走关键词候选、错误类型和 token 增量。它不包含 API Key、完整 prompt 或工具原文。`result_from_trace()` 会自动携带模型调用次数；每日任务可额外传入 `source_stats`。对带性能门槛的任务，评分器会校验 `metrics`；例如 T10 在走 RAG 工具时要求其在 8.5 秒内返回关键词候选或语义结果。

T04（鲁棒性评估设计）与 T10（RAG 初始化降级）是工程/方法解释题：若用户没有要求论文、引用或具体实验数据，Agent 可以直接说明项目约束，不必为制造引用调用工具。工具轨迹在这两题是可选证据；其他明确要求取证的任务仍保留强制工具断言。

真实执行一项任务时，可直接转换为评分输入：

```python
from evals.capture import result_from_trace

answer = agent.step(task["prompt"], session_id=session_id)
result = result_from_trace(task["id"], answer, agent.get_last_trace(session_id))
```

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
