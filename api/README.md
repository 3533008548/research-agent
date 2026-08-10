# FastAPI 服务说明

Gradio 页面仍可通过 `http://localhost:7860/` 访问；交互式 API 文档位于
`http://localhost:7860/docs`。

## 统一运行任务模型

对话、深度研究和每日论文检索都采用同一个可持久化的运行任务模型，并由独立的
`api-worker` 进程执行：

```text
API 请求 -> SQLite 运行记录（queued）-> Redis 优先级队列 -> api-worker
                                                        |-> Redis SSE 事件
```

- 普通队列项仅在 worker 执行期间保存请求文本；每日任务只传递 `run_id`，检索计划已持久化。
- SSE 事件固定为 `status`、`token`、`tool`、`done`、`error`，保留 24 小时。
  进度事件只包含脱敏的阶段名与状态；工具事件只包含工具标识和生命周期状态。
- 最终回答、安全聚合指标和审计时间线保存在 SQLite。因此 Redis 事件过期后，
  `GET /api/v1/runs/{run_id}` 仍可查看任务结果。
- 取消操作会写入 Redis 取消标记；worker 监测到标记后，将现有的协作式取消令牌传给 Agent。
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
| `GET` | `/api/v1/sessions/{session_id}/runs` | 列出该会话最近的聊天与深度研究任务 |
| `POST` | `/api/v1/chat` | 兼容用聊天入口：创建一个聊天任务 |
| `POST` | `/api/v1/runs` | 创建 `chat`、`research` 或 `daily` 任务 |
| `GET` | `/api/v1/runs/{run_id}` | 查看状态、安全指标与最终结果 |
| `GET` | `/api/v1/runs/{run_id}/events` | 读取 SSE 事件流 |
| `POST` | `/api/v1/runs/{run_id}/cancel` | 请求跨进程协作式取消 |

`POST /api/v1/sessions/{session_id}/chat-runs` 保留为兼容别名，客户端应逐步迁移到
统一的 `/api/v1/runs` 接口。

每日任务的 `daily_kind` 支持 `daily`、`retry`、`search`、`resume`：其中 `resume`
会使用原 `run_id` 重新排队最近一次可恢复的每日任务。

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

# 持续读取运行事件
curl -N -H "X-API-Key: $TOKEN" \
  http://localhost:7860/api/v1/runs/<run_id>/events

# 抓取脱敏的运行指标
curl -H "X-API-Key: $TOKEN" http://localhost:7860/api/v1/metrics
```
