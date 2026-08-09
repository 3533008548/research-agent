# FastAPI service layer

The Gradio UI remains available at `http://localhost:7860/`; the OpenAPI
document is at `http://localhost:7860/docs`.

## Phase two boundary

Interactive chat runs now use a durable Redis Stream and a separate
`api-worker` process:

```text
API request -> SQLite run metadata (queued) -> Redis Stream -> api-worker
                                                       |-> Redis SSE events
```

- Queue entries hold the request text only while a worker needs it.
- Redis event streams contain transient status/token events for 24 hours.
- Final answers, safe metrics and the audit timeline remain in SQLite, so
  `GET /api/v1/runs/{run_id}` still works after event expiry.
- Cancellation writes a Redis marker. The worker monitors it and supplies the
  existing cooperative cancellation token to the agent.
- A Redis consumer group keeps unacknowledged work pending after a worker
  crash; the next worker claims it after two minutes.

The Compose profile deliberately keeps one worker, because SQLite remains the
primary user-data store. This phase makes API delivery and cancellation
cross-process; it does **not** claim arbitrary multi-worker SQLite writes are
safe. Daily retrieval and deep-research jobs remain out of the public API.

## Authentication and network boundary

All `/api/v1/*` routes except `GET /api/v1/health` accept either:

```http
X-API-Key: <API_AUTH_TOKEN>
```

or:

```http
Authorization: Bearer <API_AUTH_TOKEN>
```

Set these in the untracked `.env` file before a public deployment:

```env
API_AUTH_TOKEN=use-a-long-random-secret
API_AUTH_REQUIRED=1
```

If a token is present it is enforced. `API_AUTH_REQUIRED=1` also makes startup
fail fast when the token is absent. Compose binds port 7860 to `127.0.0.1` by
default; use a TLS reverse proxy and keep authentication enabled before
publishing it to a network.

## Endpoints

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | unauthenticated health check |
| `GET`, `POST` | `/api/v1/sessions` | list or create a session |
| `DELETE` | `/api/v1/sessions/{session_id}` | cancel active runs and delete a session |
| `GET` | `/api/v1/sessions/{session_id}/runs` | list recent chat runs |
| `POST` | `/api/v1/sessions/{session_id}/chat-runs` | enqueue a chat run and return its SSE URL |
| `GET` | `/api/v1/runs/{run_id}` | inspect status, safe metrics and final answer |
| `GET` | `/api/v1/runs/{run_id}/events` | read SSE `status`, `token`, `done`, `error` events |
| `POST` | `/api/v1/runs/{run_id}/cancel` | request cross-process cooperative cancellation |

Example:

```bash
TOKEN='your-long-random-secret'

curl -X POST http://localhost:7860/api/v1/sessions \
  -H "X-API-Key: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"API session"}'

curl -X POST http://localhost:7860/api/v1/sessions/<session_id>/chat-runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Explain the RAG cold-start fallback."}'

curl -N -H "X-API-Key: $TOKEN" \
  http://localhost:7860/api/v1/runs/<run_id>/events
```
