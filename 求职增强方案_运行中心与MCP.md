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

### ⚠️ 补完这一步之后才发现：时间戳到不了浏览器

第一版只做了测量，**没有做投递**。`/runs/{id}` 对 `daily-` 运行返回
`events: []` —— daily 的阶段事件存在 `daily_agent_events` 表里，
而 `get_run` 只对 `chat-/research-/experiment-` 去读 `agent_run_events`。
前端拿到的是空数组，界面照样画不出时间轴。

修法：新增 `_daily_timeline_events`，把 daily 事件投影成与其他 run 相同的
`{type, stage, status, at_ms, duration_ms}`，界面就只需**一个**归约函数。

→ **教训**：埋点要连着"谁读它"一起确认。只验证"写进去了"会漏掉整整一条链路。

---

## 2. 阶段二：拆 RunCenter + 阶段时间轴（已完成）

**直接对应 JD 的"执行过程可视化"**。复用已有 `runs` tab，不新建页面。

### 2.1 实际落地结构

```
frontend/src/
├── App.tsx                             2569 行（原 2582）
├── api.ts / types.ts                   数据层与类型
├── lib/
│   ├── timeline.ts                ★ 纯函数归约，13 个测试
│   └── timeline.test.ts
└── components/runcenter/
    ├── RunCenterList.tsx           运行列表 + Badcase 标记
    ├── RunDetailPanel.tsx          按需拉取事件、切换 daily/research 端点
    └── StageTimeline.tsx           甘特式时间轴 + 工具调用面板
```

**前端测试不引新依赖**：Node 24 内置 runner + 原生 `--experimental-strip-types`
就能跑纯 TS。`tsconfig.app.json` 排除 `*.test.ts`，所以测试不进应用构建。

### 2.2 时间轴的数据形状

`lib/timeline.ts` 是纯函数、不依赖 React、不发请求，因此最关键那条规则能被单独测：

```ts
export interface TimelineStage {
  stage: string;            // 后端原名，如 planner / synthesis
  status: string;
  startMs: number;          // 来自 at_ms
  durationMs: number | null;// 后端没给就是 null
  running: boolean;
}
```

**归约规则（与后端语义严格一致，否则界面会说谎）：**
1. 按 `stage` 分组，保持首次出现顺序。
2. `durationMs` **只取后端给的 `duration_ms`**，没有就是 `null` → 渲染 `—`，
   **绝不用 `at_ms` 差值兜底**。
3. `measuredTotalMs()` 只累加**已测量**的阶段，
   避免未测量阶段把合计撑大到超过整体。
4. 未执行的已知阶段补成 `pending`（灰色），而不是留空让人以为流程短。

### 2.3 两个实测出来的真 bug

**① `startMs === 0` 当哨兵是错的**
最初用 `0` 表示"未测量"，但 `at_ms` 的合法值就是 `0`。结果 planner 的起点
被后续 `running` 刷新从 0 拖到 900ms —— 一个只在特定数据下才出现的偏移错误。
改用独立的 `startsSeen: Set<string>`记录是否已见测量值。
→ 通用教训：**哨兵值不能与合法值重合**。

**② 前端不能假设所有 run kind 走同一个端点**
`/runs/{id}` 需要 Redis run manager；daily 改走 `/workspace/daily-runs/{id}`
（只依赖 scheduler）。这不是测试环境的问题 —— **没配 Redis 的部署本来就该能用时间轴**。
→ 这条是让 API 测试的 404 失败暴露出来的，属于意外收获。

### 2.4 界面表现

- 时间轴按 `at_ms + duration` 归一化，最长阶段占满宽度。
- 阶段名用**英文原名**（`planner`/`synthesis`/…）。理由：这些是真实代码里的
  stage 名，面试时可以直接翻代码对上，界面不能另起一套叫法。
- **未测量阶段用虚线框，不用实心条** —— 避免"没有数据"和"耗时为零"视觉混淆。
- 颜色按状态：`completed` 蓝、`running` 琥珀、`failed/partial` 红、`skipped/pending` 灰。

### 2.5 验收情况

| 项 | 结果 |
|---|---|
| 时间轴数值 | 后端 376 测试绿，含 API 端到端断言（`duration_ms`/`at_ms` 确实到 JSON） |
| 无耗时阶段 | 显示 `—`，虚线框 |
| 跳过阶段 | 灰色 `pending`/`skipped`，不占宽度 |
| 投影不泄露 | 测试断言 daily 事件只含 5 个白名单 key |
| 前端构建 | `tsc -b && vite build` 通过 |
| 前端测试 | 13 例，`npm run test`，已进 CI 新 job |
| App.tsx | 2582 → 2569 行（本阶段只搬移 RunCenter 列表） |

---

## 3. 阶段三：MCP Server（1–2 天，下一步）

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

### 4.3 实际落地（已完成）

`mcp_client.py` + `MCP_CLIENT.md`。核心结论与原方案一致，但有一处必须改写：

> 原方案写的是"熔断/超时/重试/降级**全部复用**"。实际落地时必须说清楚
> **哪些是复用、哪些是新增**——含混其辞会让人以为熔断也在 `ToolRuntime` 里。

| 边界 | 机制 | 位置 | 性质 |
|---|---|---|---|
| 能力范围 | `allowed_tool_names` | `tool_runtime.py` | **复用** |
| 取消 | `cancel_event` | `tool_runtime.py` | **复用** |
| 结果上限 | `result_limit` | `tool_runtime.py` | **复用** |
| 生命周期事件 | `event_callback` | `tool_runtime.py` | **复用** |
| 熔断 | `CircuitBreaker`（每服务一个） | `mcp_client.py` | 新增，但**复用 `resilience.CircuitBreaker` 这个既有原语** |
| 超时 | 每服务 `timeout_seconds` | `mcp_client.py` | 新增（`ToolRuntime.deadline_monotonic` 至今无人设置，是空壳） |

外部工具**没有绕开 `ToolRuntime`**，所以前四项是白捡的；
后两项必须新写，但用的是项目既有的熔断器原语，不是另起炉灶。

**命名空间**：`mcp__<服务>__<工具>`。且**内置工具优先**——
外部 Server 恰好用了同名工具也替换不掉内置定义（`result_limit` 之类不受影响）。

**降级**：服务起不来 → 记录错误、跳过、Agent 照常用内置工具。
已连接的会话掉线**也计入熔断**——否则会在每轮工具调用重试一次，每次付满超时。

**信任边界**：聊天提示词新增「工具结果的信任边界」一节；
研究 worker 提示词补充 `mcp__` 输出的定性。外部工具只授予 `public` 角色，
`local` 与 `boundary` 角色拿不到。

### 4.4 实测踩到的坑

**`stdio_client(...).__aenter__()` 会被 GC。**
`stdio_client` 是 `@asynccontextmanager` 的异步生成器；只调 `__aenter__()`
而不持有对象，生成器被 GC 时**终结并拆掉子进程**。
症状是「握手成功、工具数 0、错误只有一句 `Connection closed`」——
错误信息完全指错方向，读日志查不出来。
修法：`AsyncExitStack` 持有上下文，让生命周期覆盖整个会话。

> 这类故障唯一的解法是**缩小变量**：先固定 session 只调 `list_tools`，
> 再固定 loop 只跑一次握手，才定位到是对象生命周期而非协议问题。

**测试自己也要能失败。** 熔断测试最初构造 registry 时没给 `specs`，
熔断器根本没被创建，于是"连续失败 2 次"永远开不了闸——
测试是绿的，熔断却不存在。

**同步/异步桥接**：工具路径全同步、MCP SDK 全异步。
用专用后台线程持有事件循环与活动会话，同步侧用
`run_coroutine_threadsafe` 提交并等待。没有另起线程池，
也没有把图改成异步（那会波及所有编排器）。

### 4.5 验收情况

| 项 | 结果 |
|---|---|
| 外部工具被模型看见 | `mcp__self__retrieval_metrics` 出现在 `get_tool_schemas()` |
| 经由普通分发器可达 | `execute_tool("mcp__self__...", {})` 返回服务端真实载荷 |
| 继承结果上限 | 外部工具的返回同样被 `result_limit` 截断 |
| 继承取消 | 已取消的运行不会调用外部工具 |
| 继承生命周期事件 | 外部工具同样发 `tool_started`/`tool_finished`/`tool_failed` |
| 熔断 | 连续失败开闸；已连接但掉线也计入 |
| 降级 | 起不来的服务被跳过，Agent 仍可用内置工具 |
| 配置 | 默认关闭；畸形条目被跳过而不是拖垮整个配置 |
| 测试 | 边界 25 + 传输 9 + 接线 6，全过 |
| 后端回归 | **448 绿** |

---

## 5. 里程碑与顺序

| 阶段 | 内容 | 验收 | 风险 |
|---|---|---|---|
| ✅ 第一步 | 后端阶段时间戳 + daily 事件投递 | 376 测试绿 | 已完成 |
| ✅ 第二步 | RunCenter 拆分 + 时间轴 | 数值与 SQLite 一致、前端 13 例 | 已完成 |
| ✅ 三 | MCP Server | 5 tool 可 inspect、结果一致 | 已完成（`a6dc4a1`/`301258d`） |
| ✅ 四 | MCP Client | 外部 tool 自动获得熔断 | 已完成（`MCP_CLIENT.md`） |
| ✅ 五 | `App.tsx` 剩余部分收尾 | 组件均 < 500 行 | 已完成（`App.tsx` 2569 → 512） |

**顺序理由**：二和三都能独立交付、独立演示，风险都低。
四放最后，因为它唯一依赖外部系统；如果时间不够，前三步已经能覆盖 JD 的两条要求。

**当前进度**：一至五全部完成。

阶段五的结果：`App.tsx` 2569 → 512 行，App 组件本体 478 行，所有组件与 hook 均 < 500 行。拆法按**数据归属**而非字母：

```
App.tsx（组合根）
  ├─ hooks/useConversation      会话/消息/输入框/正在跑的 run（拥有共享 run 状态）
  ├─ hooks/useDailyWorkspace    每日检索（从 conversation 借 run 状态）
  ├─ hooks/useRunCenter         运行中心（从 daily 借）
  ├─ hooks/useDocuments         科研档案（依赖 chat 的 sendMessage）
  ├─ hooks/useExperiments       论文复现（依赖 chat 的 setDraft）
  ├─ hooks/useWorkspaceChrome   面板开合与内容加载
  └─ components/workspace/*     六个面板 + 侧栏 + 聊天面板
```

hook 调用顺序由编译器盯着：顺序写反直接编译不过。

---

## 6. 回滚策略

- 阶段二、三、四**各自独立提交**，回滚 = 撤销对应提交，不涉及数据迁移。
- 第一步的时间戳是**纯增量字段**：`at_ms`/`duration_ms` 本就在落库白名单，
  旧数据没有这些字段，前端按"无耗时"处理即可。
  **没有 schema 变更，不需要重建数据库。**
- MCP Server 独立进程，出问题直接停进程，不影响 Web 服务。