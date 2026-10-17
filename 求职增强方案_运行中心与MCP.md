# 求职增强方案：运行中心可视化 + MCP 双向

> 前提：阶段时间戳已落地（`run_contract.TIMING_METRIC_KEYS` + 两条编排链路的单调时钟），
> 前端时间轴不需要再猜耗时。本文档只讲剩下的部分。

---

## 0. 调研纠正：两条流程的阶段名都是真实的，不存在需要伪造的阶段

我在动手前重新核对了编排代码，此前提到"research 只有 llm/tools/verify"是**不完整的**。真实情况：

| 流程 | 入口 | 真实阶段（来自代码） |
|---|---|---|
| **深度研究** | `research_orchestrator.py::research()` | `planner` → `researchers` → `evidence_analysis` → `synthesis` → `critic` →（可选 `revision`） |
| **每日检索** | `daily_orchestrator.py::run()` | `planner` → `scouts` → `normalizer` → `quality_gate` → `curator` →（可选 `critic`）→ `delivery` |
| **单轮对话** | `graph_builder.py` | `llm` / `tools` / `verify`（LangGraph 节点） |

**这对求职是好事**：JD 里写的"Planner → 研究员 → 综合 → 质检"，
和 `research_orchestrator` 的 `planner/researchers/synthesis/critic` **一字不差**。
界面直接用真实阶段名，不需要为了演示而编阶段。

---

## 1. 已完成：第一步（后端阶段时间戳）

### 为什么这是唯一的阻塞项

界面要显示"每步耗时"，只有三个可能的来源：

| 来源 | 可行性 |
|---|---|
| 前端按事件到达顺序估算 | ❌ 不可信。网络抖动、批处理、SSE 重连都会让顺序失真 |
| SQLite 的 `created_at` 做差 | ⚠️ 只能算**相邻事件间隔**，会把上一个阶段的收尾算进下一个阶段 |
| 后端在阶段边界打单调时钟 | ✅ 唯一正确做法 |

### 改动内容

**契约层 `run_contract.py`**
```python
TIMING_METRIC_KEYS = ("at_ms", "duration_ms")
TERMINAL_STAGE_STATUSES = frozenset({
    "completed", "failed", "cancelled", "partial", "partial_failed", "skipped",
})
def bounded_timing_metrics(source) -> dict[str, float]: ...
```
- `TERMINAL_STAGE_STATUSES` 特别包含 `partial` / `partial_failed`：
  部分成功**同样消耗了真实时间**，当成非终态会导致阶段时钟泄漏、永不出耗时。
- `bounded_timing_metrics` 是**显式白名单**（只两个 numeric key），
  而不是过滤调用方传进来的一切 —— 时间轴不应该有能力碰到 prompt / 工具参数 / 论文正文。
- `at_ms` / `duration_ms` 本来就在 `_SAFE_METRIC_KEYS` 里，
  所以不需要为持久化放宽落库过滤，两个闸门都过。

**每日检索 `daily_orchestrator.py`**
- 新增按 `(run_id, agent)` 分键的阶段时钟。
  ⚠️ **一个 orchestrator 实例服务所有排队运行**，若按 agent 名分键，
  上一个 run 的 curator 耗时会串到下一个 run 的时间轴上。测试 `test_duration_does_not_leak_between_runs` 盯着这条。
- 新增 `_open_stage()`：`planner` / `quality_gate` / `delivery` / `normalizer` / `critic`
  这些**只发终态事件、不发 `running`** 的阶段，用它静默开表。
  **它不发事件**，所以事件流与改动前逐字节一致 —— 这是刻意的，见下。
- `_notify_progress()`：阶段内的进度条 ping（如"已完成 1/3 个关键词"）
  只带 `at_ms`（位置），**不带也不重置 `duration_ms`**。
  重复的 `running` 用 `setdefault`，不重置起点。

**深度研究 `research_orchestrator.py`**
- 时钟放在**闭包**里而非 `self` 上：两个并发研究运行互不干扰，
  状态随请求消失而不是泄漏。
- 复用同一个 `TERMINAL_STAGE_STATUSES`。
- 顺手去掉了重构中暴露的重复调用：`_persisted_progress` 里
  `external_progress(event)` 原本会被 `_progress` 再调一次，实际是双发。

**流式层 `api/redis_runs.py::_safe_status_event` + `api/sse.py::public_sse_event`**
- 之前两者都会剥掉耗时，改为转发 `bounded_timing_metrics(event)`。
- 保持"未知 event_type 退回 `{type:status,status:running}` 且不带任何附加字段"，
  避免公共词汇表守卫变成计时侧信道（`test_unknown_event_type_still_gets_no_timing`）。

### 三条必须说清的取舍

1. **为什么用 `perf_counter()` 而不是 `created_at` 做差**
   `created_at` 是墙钟，会被 NTP 校时和夏令时影响；`perf_counter` 是单调钟。
   跨阶段的 `at_ms` 仍然基于同一个 run 起点做差，所以两者的可信度不同 ——
   这一点在代码注释里写明了。

2. **为什么 `metrics.update(timing)` 而不是替换**
   原来的 `details` 参数携带 source stats 等内容。虽然 `safe_metrics` 会过滤掉非数值字段，
   但保留 merge 语义意味着不改变既有过滤行为，减少一层变量。

3. **为什么测试要专门测"验证工具自己"**
   沿用重构期的教训：`test_open_stage_measures_without_emitting_an_event` 断言
   `_open_stage` 之后终态事件**确实**带耗时。如果哪天 `_open_stage` 被误删，
   表现为 planner/quality_gate/delivery 静默失去耗时 —— 而这种缺失在界面上
   是"某几个阶段不显示耗时"，很容易被当成正常现象放过。

### 验收

`tests/test_stage_timing.py` 16 个用例，其中一个是**真实的离线端到端 daily 运行**
（用 `evals/fixtures/providers` + `FixtureSourceRouter`，不打网络），
断言耗时既进了 SSE 回调，也**作为数字落进了 SQLite**（防止只活在回调里、刷新即丢）。

```
373 tests OK  (206 → +16)
```

---

## 2. 阶段二：拆 RunCenter + 阶段时间轴（2–3 天）

**直接对应 JD 的"执行过程可视化"**。复用已有 `runs` tab，不新建页面。

### 2.1 组件拆分（顺带解决 App.tsx 2582 行）

现状：前端 6 个文件，`App.tsx` 独占 2582 行。

```
frontend/src/
├── App.tsx                    瘦身为壳：只做 tab 编排与路由
├── api.ts                     数据层（已有 399 行，仅追加端点）
├── types.ts                   类型（已有 290 行）
├── lib/
│   └── timeline.ts            纯函数：events[] → TimelineStage[]
│                                （可单测，不依赖 React）
└── components/
    ├── runcenter/
    │   ├── RunCenter.tsx      运行列表 + 筛选
    │   ├── StageTimeline.tsx  ★ 甘特式时间轴
    │   ├── ToolTrace.tsx      工具调用与单次耗时
    │   └── StageCard.tsx      单阶段详情
    └── ...（现有组件）
```

⚠️ **拆分纪律**：`App.tsx` 现在同时是页面容器和状态中心。
拆分必须**纯搬移**，不夹带行为变更 —— 与 `paper_store` 重构同一条纪律。
先搬后改，一步一提交，每步 `npm run build` 通过。

### 2.2 时间轴的数据形状

`lib/timeline.ts` 是核心，**纯函数、可单测、不依赖 React**：

```ts
export interface TimelineStage {
  stage: string;          // "planner" | "researchers" | ...
  status: string;         // completed | running | partial | failed | skipped
  startMs: number;        // at_ms
  durationMs: number | null;  // null = 未结束或后端未测量
  agent: string;          // "orchestrator" | "researcher" | ...
}

export function buildTimeline(events: RunEvent[]): TimelineStage[]
```

**归约规则（必须与后端语义一致，否则界面会说谎）：**
1. 按 `stage` 分组，保持首次出现顺序。
2. `durationMs` **只取后端给的 `duration_ms`**。
   没有就是 `null`，界面显示"—"，**绝不用 `at_ms` 差值兜底**。
3. `status` 取该 stage 最后一个事件的状态。
4. 未配对的 `running` → `durationMs: null`，界面显示"进行中"并持续增长（增长用本地时钟，
   但**只用于动画，收敛值仍以后端为准**）。

⚠️ 规则 2 是原则问题：一旦前端开始自己估算耗时，界面就和后端日志对不上，
排查问题时会出现两个真相。我在前端代码里会写明这一点。

### 2.3 界面布局

```
┌─ 运行中心 ────────────────────────────────────────────┐
│ [运行列表]          │  深度研究 · run_7f3a            │
│ ● 完成  research    │                                  │
│ ● 运行中 daily      │  ▓▓▓▓▓▓░░░░  1.2s  planner      │
│ ○ 失败  chat        │  ▓▓▓▓▓▓▓▓▓▓ 0.4s  researchers   │
│                    │  ▓▓░░░░░░░░  0.1s  evidence_analysis
│                    │  ▓▓▓▓▓▓▓▓▓▓ 2.8s  synthesis     │
│                    │  ░░░░░░░░░░     —     critic (skipped)
│                    │                                  │
│                    │  ── 工具 ──────────────────────  │
│                    │  query_papers        612ms   ✓   │
│                    │  search_papers       88ms    ✓   │
│                    │  rerank(cross-enc)   603ms   ✓   │
│                    │                                  │
│                    │  ── 结果 ──────────────────────  │
│                    │  证据 8 · 引用 5 · 全文 2.1k字    │
└────────────────────┴──────────────────────────────────┘
```

- 时间轴宽度按 `at_ms + duration` 归一化，最长阶段 100%。
- 阶段名用**英文原名**（`planner`/`synthesis`/...），配中文 tooltip。
  理由：这些是真实代码里的 stage 名，面试时可以直接翻代码对上，界面不能另起一套叫法。

### 2.4 实时性

现有 SSE 已能收到带耗时的 status 事件（本次已打通），所以：
- 运行中：SSE 事件到达即更新对应阶段，不轮询。
- 刷新页面：从 `run.events` 归约，历史时间轴完整恢复。

### 2.5 验收标准

| 项 | 标准 |
|---|---|
| 时间轴数值正确 | 选 3 个真实 run，界面耗时与 SQLite `details_json` 逐项一致 |
| 无耗时阶段 | 显示 "—"，**不是 0ms** |
| 跳过阶段 | `critic/skipped` 灰色显示，不占宽度 |
| 并发运行 | 同时开 daily + research，互不串时间轴 |
| 回归 | 现有 373 个后端测试 + `npm run build` + `npm run lint` 全绿 |
| 不夹带变更 | 拆分提交与功能提交分开，`git diff` 可审查 |

---

## 3. 阶段三：MCP Server（1–2 天）

**直接对应 JD 的 MCP 要求。** 优先做 Server，因为它把本项目**已有的能力对外暴露**，
不需要新的模型决策，风险最低、见效最快。

### 3.1 暴露什么

按"已有且可靠"排序，**不新增检索逻辑**：

| MCP Tool | 底层 | 价值 |
|---|---|---|
| `search_papers` | `tools/search.py` | 语义+词法混合检索（已重排） |
| `query_paper_context` | `paper_artifacts.related_context` | 给定 paper_id 取关联上下文 |
| `list_papers` | 已有工具 | 列出已索引论文 |
| `get_retrieval_metrics` | `papers/metrics.py` | 本次重构产出的阶段耗时指标 |

⚠️ **不暴露**：`read_pdf`（需要上传通道）、写操作（会绕过质量门控）。
理由：MCP Server 是给**外部客户端**用的，写操作没有对应的配额和审计。

### 3.2 实现要点

- 独立进程 `mcp_server.py`，用官方 `mcp` Python SDK 的 stdio transport。
  **stdio 而非 HTTP**：本项目是单机自用，stdio 免端口免鉴权，且客户端能拉起子进程。
- **复用现有 store 构造路径**，不新建 `PaperStore` 变体。
  ⚠️ 陷阱：容器里模型已预热；若 MCP Server 在宿主机裸跑，
  会触发 ONNX 图重优化（约 530ms/次，见项目记忆）。所以 MCP Server 应**跑在容器内**。
- 每个 tool 的返回值走 `bounded_timing_metrics` 同款白名单思路：
  **只返回文本与引用，不返回 chunk id 之外的内部 id、不返回向量**。

### 3.3 验收

- `inspect` 能看到 4 个 tool 与各自 schema。
- 4 个 tool 各跑一次，与直接调内部函数结果**逐字一致**。
- 断网（无 DEEPSEEK_API_KEY）时，检索类 tool 仍可用 —— MCP 不该依赖 LLM。

---

## 4. 阶段四：MCP Client（1–2 天）

让本项目**作为客户端**使用外部 MCP Server（例如让 Agent 去调真实的
`arxiv-mcp-server`）。这一步证明的是另一件事：会**消费**别人的 MCP。

### 4.1 接入点

工具层是 `tool_runtime.py` + `graph_builder` 的 `tool_node`。
新增 `mcp_client.py`：
- 启动时按配置拉起/连接外部 MCP Server，列出其 tools。
- 把它们包装成与现有工具**同构**的 tool definition，
  这样 `graph_builder` 的工具循环、预算、超时、熔断**全部复用，不需要改图**。

⚠️ 这是本方案里我最看重的一点：
**外部 MCP 工具接入后必须自动获得现有的熔断/超时/重试/降级能力**。
如果需要单独写一套，那就说明抽象没找对位置。

### 4.2 风险与诚实说明

- 外部 MCP Server 的 schema 不可控，可能与内置工具同名 → 需命名空间隔离。
- 外部 tool 的返回可能是恶意指令文本 → 走现有的"工具结果不当指令"防线。
- **接入失败必须降级而非报错**：MCP Server 起不来时，Agent 仍能用内置工具正常工作。

---

## 5. 里程碑与顺序

| 阶段 | 内容 | 验收 | 风险 |
|---|---|---|---|
| ✅ 第一步 | 后端阶段时间戳 | 373 测试绿 | 已完成 |
| 二 | RunCenter 拆分 + 时间轴 | 时间轴与 SQLite 逐项一致 | 低（纯前端） |
| 三 | MCP Server | 4 tool 可 inspect、结果一致 | 低（暴露已有能力） |
| 四 | MCP Client | 外部 tool 自动获得熔断 | **中**（外部不可控） |
| 五 | `App.tsx` 剩余部分收尾 | 组件均 < 500 行 | 低 |

**顺序理由**：二和三都能独立交付、独立演示，风险都低。
四放最后，因为它唯一依赖外部系统；如果时间不够，前三步已经能覆盖 JD 的两条要求。

---

## 6. 回滚策略

- 阶段二、三、四**各自独立提交**，回滚 = 撤销对应提交，不涉及数据迁移。
- 第一步的时间戳是**纯增量字段**：`at_ms`/`duration_ms` 本就在落库白名单，
  旧数据没有这些字段，前端按"无耗时"处理即可。
  **没有 schema 变更，不需要重建数据库。**
- MCP Server 独立进程，出问题直接停进程，不影响 Web 服务。