# FastAPI 服务说明

React 页面构建完成后，FastAPI 会在 `http://localhost:7860/` 提供工作台；交互式 API
文档位于 `http://localhost:7860/docs`。不再保留第二套 Gradio 页面。

## 统一运行任务模型

对话、深度研究、每日论文检索和论文代码复现都采用同一个可持久化的运行任务模型，并由独立的
`api-worker` 进程执行：

```text
API 请求 -> SQLite 运行记录（queued）-> Redis 优先级队列 -> api-worker
                                                        |-> Redis SSE 事件
```

- 普通队列项仅在 worker 执行期间保存请求文本；每日任务只传递 `run_id`，检索计划已持久化。
- SSE 事件固定为 `status`、`token`、`tool`、`done`、`error`，保留 24 小时。
  进度事件只包含脱敏的阶段名与状态；工具事件只包含工具标识和生命周期状态。
- SQLite 会持久化低频审计事件 `status`、`tool`、`done`、`error`，以及模型、运行器、
  工具集和研究范围组成的执行指纹；`token` 只保留在 Redis，避免把完整回答流复制进数据库。
  审计事件只包含固定阶段说明、聚合耗时/计数和错误类型，不包含提示词、回答、关键词、
  论文内容、工具参数或工具原始结果。因此 Redis 事件过期后，
  `GET /api/v1/runs/{run_id}` 仍可查看任务结果与安全时间线。
- 取消操作会写入 Redis 取消标记；worker 监测到标记后，将现有的协作式取消令牌传给 Agent。
- 对话与深度研究运行中可追加一条文本补充。补充会持久化到 SQLite，并只在下一个模型或研究
  节点被消费；它不会中断已经发出的模型请求，也不会写入普通会话历史或 SSE 事件内容。
- Redis Consumer Group 会保留 worker 异常退出时未确认的任务；其他 worker 会在两分钟后认领。
- `GET /api/v1/metrics` 输出 Prometheus 文本指标：Redis 队列积压、worker 心跳、
  各类运行任务的状态计数和 API 进程运行时长。它不输出用户输入、模型回答、论文信息或工具参数。

当前 Compose 配置故意只运行一个 `api-worker`，因为 SQLite 仍是主要用户数据存储。
FastAPI API 实例可以横向扩展：它们只向共享 Redis 队列投递任务。单个 worker 进程中，
聊天消费者和后台消费者共享同一个模型客户端，已有的交互保留并发槽可避免长时间后台任务
挤占聊天请求。SQLite 尚未替换为多写入者的任务/状态存储前，请不要扩容 `api-worker`；
届时还应增加 Redis 分布式模型信号量。

## 鉴权与网络边界

除 `GET /api/v1/health` 外，所有 `/api/v1/*` 路由均接受下列任一种鉴权方式：

```http
X-API-Key: <API_AUTH_TOKEN>
```

或：

```http
Authorization: Bearer <API_AUTH_TOKEN>
```

对外部署前，请在未纳入 Git 的 `.env` 文件中设置：

```env
API_AUTH_TOKEN=use-a-long-random-secret
API_AUTH_REQUIRED=1
```

如果配置了令牌，系统会强制鉴权；若 `API_AUTH_REQUIRED=1` 但缺少令牌，启动会立即失败。
Compose 默认将 7860 端口绑定到 `127.0.0.1`。公开访问时，请在 TLS 反向代理后部署，
并保持 API 鉴权开启。

## 接口列表

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/health` | 无需鉴权的健康检查 |
| `GET` | `/api/v1/metrics` | Prometheus 聚合运行指标，需鉴权 |
| `GET`、`POST` | `/api/v1/sessions` | 列出或创建会话 |
| `DELETE` | `/api/v1/sessions/{session_id}` | 取消会话中的活跃任务并删除会话 |
| `GET` | `/api/v1/sessions/{session_id}/messages` | 恢复该会话的用户可见对话 |
| `GET` | `/api/v1/sessions/{session_id}/usage` | 读取该会话持久化的输入、输出与合计 Token 统计 |
| `GET` | `/api/v1/sessions/{session_id}/runs` | 列出该会话最近的聊天、深度研究与论文复现任务 |
| `GET` | `/api/v1/workspace/research-documents` | 列出研究档案元数据 |
| `GET` | `/api/v1/workspace/research-documents/{id}` | 按需读取一份研究档案的 Markdown、四章节目录与版本摘要 |
| `GET` | `/api/v1/workspace/research-documents/{id}/sections` | 读取四个固定章节的元数据与校验哈希，不含正文 |
| `GET` | `/api/v1/workspace/research-documents/{id}/versions` | 列出当前版本和可恢复的历史快照 |
| `GET` | `/api/v1/workspace/research-documents/{id}/ledger` | 读取独立的证据与假设账本及章节关联新鲜度 |
| `POST` | `/api/v1/workspace/research-documents/{id}/route` | 将当前档案的自然语言请求路由为流程提示，不执行写入 |
| `GET` | `/api/v1/workspace/research-documents/{id}/download` | 下载该档案的 Word 文档 |
| `GET` | `/api/v1/workspace/profile` | 读取独立于会话历史的用户画像 |
| `GET` | `/api/v1/workspace/papers` | 列出本地已索引论文与片段数 |
| `GET` | `/api/v1/workspace/paper-relations` | 查看用户确认的论文关系；可用 `paper_id` 过滤 |
| `GET` | `/api/v1/workspace/experiments` | 列出已生成的论文复现实验项目 |
| `GET` | `/api/v1/workspace/experiments/{project_id}` | 读取实验规格、静态验证结果与文件清单 |
| `GET` | `/api/v1/workspace/experiments/{project_id}/files/{path}` | 按需读取一个项目文件 |
| `POST` | `/api/v1/workspace/experiments/{project_id}/repository-candidates` | 按论文标题检索 GitHub 候选仓库，所有结果均为待核验候选 |
| `PUT` | `/api/v1/workspace/experiments/{project_id}/repository` | 连接一个 GitHub 仓库并记录当前 commit |
| `POST` | `/api/v1/workspace/experiments/{project_id}/method-reconstruction` | 显式选择按论文方法还原，不将其称为原作者代码复现 |
| `DELETE` | `/api/v1/workspace/experiments/{project_id}` | 删除项目、代码文件与历史修订 |
| `POST` | `/api/v1/workspace/uploads` | 上传 PDF 或图片，返回仅当前服务可用的脱敏上传 ID |
| `GET`、`POST` | `/api/v1/workspace/daily-keywords` | 列出或添加每日检索关键词 |
| `DELETE` | `/api/v1/workspace/daily-keywords/{keyword}` | 删除一个每日检索关键词 |
| `GET` | `/api/v1/workspace/daily-digest` | 读取当天论文与本地阅读状态 |
| `PUT` | `/api/v1/workspace/daily-papers/status` | 更新当天论文为想读、已读或跳过 |
| `GET` | `/api/v1/workspace/daily-runs` | 列出最近每日检索运行 |
| `GET` | `/api/v1/workspace/daily-runs/{run_id}` | 读取某次检索的摘要、质量提示与论文推荐，用于报告标签页 |
| `GET` | `/api/v1/workspace/badcases` | 列出内容安全的本地 Badcase 候选 |
| `GET`、`PUT` | `/api/v1/workspace/settings` | 读取或保存运行时设置（重启后生效） |
| `POST` | `/api/v1/chat` | 兼容用聊天入口：创建一个聊天任务 |
| `POST` | `/api/v1/runs` | 创建 `chat`、`research`、`daily` 或 `experiment` 任务 |
| `GET` | `/api/v1/runs/{run_id}` | 查看状态、安全指标与最终结果 |
| `GET` | `/api/v1/runs/{run_id}/events` | 读取 SSE 事件流 |
| `POST` | `/api/v1/runs/{run_id}/steers` | 向排队中或运行中的对话/深度研究追加文本补充 |
| `POST` | `/api/v1/runs/{run_id}/cancel` | 请求跨进程协作式取消 |
| `POST` | `/api/v1/runs/{run_id}/badcases` | 标记为本地 Badcase 审核候选 |

`POST /api/v1/sessions/{session_id}/chat-runs` 保留为兼容别名，客户端应逐步迁移到
统一的 `/api/v1/runs` 接口。

## 科研档案的安全修改

研究档案是 Markdown 源文件，并同步导出 Word。新档案固定采用四个章节：研究背景与研究现状、
研究内容与创新、研究方案与可行性、研究展望与计划。HTTP 接口只提供只读查看；写入由 Agent 的
受限工具完成：它先读取目标章节，携带 `revision` 与章节 `content_hash` 提交局部补丁。版本或哈希
不匹配时，存储层拒绝写入而不会尝试整篇覆盖。每次成功写入前，旧正文会作为完整历史快照保存；
恢复也会创建新修订，不会抹掉之后的历史。

“论文 × 科研档案”比对同样仅由 Agent 工具执行，会扫描全部四个章节和所选本地论文的全部切块，
返回带页码/章节定位的词汇重合候选。它不是自动写入流程，词汇未命中也不能证明不存在概念关系。

每份档案还可带有 `research_ledger.json`。它是正文之外的研究判断索引：每项关联一个固定章节，
记录研究问题、假设、创新候选或决策，以及可定位论文证据、认识状态和可证伪条件。账本写入通过
Agent 工具完成，需携带账本修订号和关联章节哈希；`supported` 状态缺少论文定位会被拒绝。正文发生
变化后，旧条目不会被静默删除，而会显示为待复核。创新性审查也由 Agent 工具完成，按研究问题、
核心假设、方法机制、适用条件、评价计划五维汇集全量词汇扫描和已有混合检索候选；它只产出创新候选
及区分实验建议，不替代系统性文献综述，也不自动改写档案或账本。

档案页的自然语言请求会先经过 `POST /workspace/research-documents/{id}/route` 的轻量规则路由：
`new_paper_impact_review`、`safe_patch`、`paper_comparison`、`ledger`、`innovation_review` 或 `consult`。其中
“新导入论文 + 修改档案”是明确的产品级复合意图：Agent 必须先唯一定位论文，再调用一次只读综合审查工具，产出重叠/借鉴、可行性影响、创新性候选和逐章节拟修改项；用户确认前不得写入。它只返回包含
`document_id`、原请求和流程提示的消息，不读取全文、不创建任务也不修改任何数据；随后普通对话任务仍由
Agent 的工具约束、版本号和章节哈希校验执行。规则与用户原话冲突或无法判断时，Agent 应以用户原话为准并澄清。

## 论文关系增强检索

`runtime/primary/paper_relations.json` 只保存用户明确确认的论文关系，并要求每条关系有两端已索引论文、
简短说明和至少一个页码或检索片段锚点。HTTP 仅提供读取接口；新增、更新和删除由 Agent 工具执行，写入前
必须向用户展示候选关系并获得确认。关系是检索提示而不是论文事实：本地 RAG 先做语义 + BM25 RRF，随后才从
第一轮相关论文的一跳关系中扩展仍匹配当前问题的候选，并施加有限加分；没有主题匹配的关联论文不会被注入结果。
它不使用图数据库、持久化向量图或 GraphRAG；删除论文时会一并移除相关关系。

每日任务的 `daily_kind` 支持 `daily`、`retry`、`search`、`resume`：其中 `resume`
会使用原 `run_id` 重新排队最近一次可恢复的每日任务。

深度研究可在请求体中传入 `scope`（`both`、`local` 或 `public`）限定来源范围。传入
`{"kind":"research","session_id":"…","resume":true}` 会继续当前会话最近一项未完成研究，
沿用其 `run_id` 与已持久化证据；若该研究已经完成，则直接返回已有结果，不会重复执行。

运行中补充使用 `POST /api/v1/runs/{run_id}/steers`，请求体为
`{"message":"…"}`。只有状态为 `queued` 或 `running` 的 `chat`、`research` 任务接受该操作。
返回的运行详情会在 `steers` 中标明该补充是 `pending` 还是已在何处 `consumed`。SSE 仍只发送
`status`、`token`、`tool`、`done`、`error`：补充的文本不会出现在 SSE 或持久化事件日志中。
页面中的“暂停任务”调用的是协作式停止，当前不提供暂停后原地恢复；深度研究已收集的证据会保留，
可通过“继续上次研究”另行续跑。

论文复现请求使用 `{"kind":"experiment","session_id":"…","paper_id":"…"}`。`paper_id`
必须对应已索引且已有页面级解析证据的本地 PDF。任务会创建独立项目到
`runtime/primary/experiment_projects/`，保存来源定位、待确认项、代码文件、静态语法校验
与版本快照；当前版本不会下载数据、执行训练或声称已达到论文报告指标。

GitHub 候选检索使用公开 GitHub 元数据 API；候选仅供用户核验，绝不自动标记为论文官方实现。可选 `GITHUB_TOKEN` 用于提高 API 限额。仓库连接会保存 URL、默认分支与查询时的 commit。随后可通过统一运行接口提交 `{"kind":"experiment","session_id":"…","project_id":"…","experiment_action":"prepare_repository"}`：worker 会将该 commit 克隆到项目的 `repository/source/`，静态提取 README、依赖声明与训练入口候选。依赖安装、数据下载和训练仍属于后续显式步骤。

若用户主动选择仅按论文方法还原，可提交 `{"kind":"experiment","session_id":"…","project_id":"…","experiment_action":"reconstruct_method"}`。worker 会只使用页面级论文证据与已确认事实，生成可编辑的 `src/method.py` 参考实现、组件证据定位、工程假设和待确认项。该操作不从“未找到候选仓库”推断论文闭源，不安装依赖、下载数据或执行代码，结果固定标为近似方法还原而非作者原始实现。

### 持久运行事件

`GET /api/v1/runs/{run_id}` 返回的 `events` 是低频审计时间线。每条事件包含数据库
排序 id、`event_type`、`stage`、`status`、聚合 `metrics` 和可选 `metadata`。其中
`metadata` 只允许 `protocol`、`run_kind`、`model`、`runner`、`scope`、`toolset`，可用于
比较执行环境；其他字段会在写入前丢弃。客户端若需要逐 token 展示，仍应连接 SSE，不能
依赖该数组复原模型输出。

### Badcase 候选

`POST /api/v1/runs/{run_id}/badcases` 接受受限的 `category` 和至多 500 字的人工脱敏备注。它从既有运行记录投影出内容安全的快照：运行类型、状态、模型标识、耗时、聚合指标和安全事件字段；不会存储或返回 prompt、回答、PDF、论文候选、工具参数或工具原始结果。响应只含候选 ID、分类、来源、状态、去重指纹和出现次数，候选实际保存在本地 `badcases.db`。

相同 `run_id + category` 重复提交不会创建新候选，而是增加 `occurrence_count`。删除所属会话时会同时删除其候选；如需把问题沉淀为 Git 内的回归测试，应使用导出脚本生成空的合成夹具草稿，再人工填写公开、可复现的数据。

## 调用示例

```bash
TOKEN='your-long-random-secret'

# 创建会话
curl -X POST http://localhost:7860/api/v1/sessions \
  -H "X-API-Key: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"API 会话"}'

# 创建聊天任务
curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"chat","session_id":"<session_id>","message":"解释 RAG 冷启动时的降级策略。"}'

# 创建深度研究任务
curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"research","session_id":"<session_id>","query":"比较 RAG 检索重排序方法。"}'

# 创建一次临时每日检索任务
curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"daily","daily_kind":"search","keyword":"检索增强生成"}'

# 为已索引论文创建代码复现实验项目
curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"experiment","session_id":"<session_id>","paper_id":"<paper_id>"}'

# 持续读取运行事件
curl -N -H "X-API-Key: $TOKEN" \
  http://localhost:7860/api/v1/runs/<run_id>/events

# 在任务运行期间补充约束或新线索（下一节点才会使用）
curl -X POST http://localhost:7860/api/v1/runs/<run_id>/steers \
  -H "X-API-Key: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"补充：未经核验的内容请明确标为待确认。"}'

# 标记一个已完成或失败的任务；备注必须由调用方自行脱敏
curl -X POST http://localhost:7860/api/v1/runs/<run_id>/badcases \
  -H "X-API-Key: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"category":"citation_quality","note":"已脱敏：引用未覆盖结论"}'

# 抓取脱敏的运行指标
curl -H "X-API-Key: $TOKEN" http://localhost:7860/api/v1/metrics
```
