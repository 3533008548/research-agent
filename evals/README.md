# 科研 Agent 评测集

`research_tasks.json` 定义 10 个版本化任务，覆盖论文精读、RAG 检索、跨论文对比、证据约束、科研笔记、每日速递、会话隔离/删除、同模型请求韧性和 RAG 初始化降级。

所有任务使用 `fixtures/synthetic_corpus.json` 的合成科研语料，或明确给出模拟状态；不会读取 `runtime/`、个人论文、API Key，也不会调用真实模型 API。因此它适合作为每次重构后的稳定回归基线。

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
  "metrics": {"duration_ms": 2100, "first_token_ms": 720, "max_tool_duration_ms": 350}
}
```

再做可重复的硬规则评分：

```powershell
python -m evals.benchmark --results evals/example_results.json
```

`example_results.json` 是评分器的合成格式样例，预期得到 10/10；它不代表真实模型表现。真实评测应保存一次实际 Agent 运行产生的回答、工具轨迹和状态快照，再使用同一命令评分。

`ResearchAgent.get_last_trace()` 可取得一轮脱敏追踪：模型名、总耗时、首 token 时间、工具名/耗时、RAG 是否走关键词候选、错误类型和 token 增量。它不包含 API Key、完整 prompt 或工具原文。对带性能门槛的任务，评分器会校验 `metrics`；例如 T10 要求 `query_papers` 在 8.5 秒内返回。

真实执行一项任务时，可直接转换为评分输入：

```python
from evals.capture import result_from_trace

answer = agent.step(task["prompt"], session_id=session_id)
result = result_from_trace(task["id"], answer, agent.get_last_trace(session_id))
```

评分通过后可显式落盘报告：

```powershell
python -m evals.benchmark --results results.json --write-report
```

报告写入被 Git 忽略的 `evals/reports/`，用于比较每次版本的通过率和延迟。

输出中的 `manual_rubric` 仍需人工按 0–2 分评估事实正确性、引用充分性和表达质量；这样不会把关键词命中误当成高质量回答。面试展示时，应保留每次运行的 JSON 报告，并说明通过数、失败任务和修复动作。
