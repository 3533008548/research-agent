# FastAPI 服务层（第一阶段）

容器启动后，Gradio 界面仍位于 `http://localhost:7860/`；FastAPI 的
OpenAPI 文档位于 `http://localhost:7860/docs`，健康检查为：

```text
GET /api/v1/health
```

当前提供的 API 是交互式对话的稳定服务边界：

| 接口 | 用途 |
|---|---|
| `GET` / `POST /api/v1/sessions` | 列出或创建会话 |
| `DELETE /api/v1/sessions/{session_id}` | 删除会话；会先向本进程运行中的请求发送取消信号 |
| `POST /api/v1/sessions/{session_id}/chat-runs` | 创建异步对话运行，返回 `run_id` 与 SSE 地址 |
| `GET /api/v1/runs/{run_id}` | 查询状态、脱敏指标和最终回答 |
| `GET /api/v1/runs/{run_id}/events` | SSE：`status`、`token`、`done`、`error` |
| `POST /api/v1/runs/{run_id}/cancel` | 协作式取消当前进程中的运行 |

创建会话与运行示例：

```bash
curl -X POST http://localhost:7860/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"title":"API 会话"}'

curl -X POST http://localhost:7860/api/v1/sessions/<session_id>/chat-runs \
  -H 'Content-Type: application/json' \
  -d '{"message":"解释 RAG 冷启动的降级策略"}'

curl -N http://localhost:7860/api/v1/runs/<run_id>/events
```

第二条请求返回 `stream_url`。使用 `GET <stream_url>` 建立 SSE 连接；完成后可用
`GET /api/v1/runs/<run_id>` 获取最终回答。最终回答会与运行元数据一起保存，使 SSE
断开后仍可读取结果；提示词、工具参数和 API Key 不写入 API 运行记录。

## 当前部署边界

这一阶段刻意采用单个 Uvicorn 进程，让 Gradio 与 API 共用同一个
`ResearchAgent`、会话锁和模型并发槽。不要增加 `--workers`，也不要把长时间的每日
检索或深度研究放入 FastAPI `BackgroundTasks`：那会使内存中的取消令牌和并发槽失去
进程间一致性。

在启用公网访问、多副本部署或每日/深度研究 API 前，下一阶段需要加入认证、共享队列
和 Redis 等跨进程并发控制；目前仅适用于受信任网络中的单实例服务。
