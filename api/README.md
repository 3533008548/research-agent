# FastAPI service layer

The Gradio UI remains available at `http://localhost:7860/`; the OpenAPI
document is at `http://localhost:7860/docs`.

## Unified run boundary

Chat, deep research, and daily paper discovery all use one durable run model
and a separate `api-worker` process:

```text
API request -> SQLite run metadata (queued) -> Redis priority streams -> api-worker
                                                                 |-> Redis SSE events
```

- Queue entries hold request text only while a worker needs it; daily jobs
  carry only their run ID because their plan is already persisted.
- Redis event streams contain only the fixed `status`, `token`, `tool`,
  `done`, and `error` events for 24 hours. Progress includes safe stage/status
  metadata only; tool events include a tool identifier and lifecycle only.
- Final answers, safe metrics and the audit timeline remain in SQLite, so
  `GET /api/v1/runs/{run_id}` still works after event expiry.
- Cancellation writes a Redis marker. The worker monitors it and supplies the
  existing cooperative cancellation token to the agent.
- A Redis consumer group keeps unacknowledged work pending after a worker
  crash; the next worker claims it after two minutes.

The Compose profile deliberately keeps one worker, because SQLite remains the
primary user-data store. API replicas can scale safely because they only
enqueue to the shared Redis streams. Inside the one worker process, dedicated
chat and background consumers share one model client, so its existing
interactive-reserved permits prevent a long background run from starving a
chat request. Do not scale `api-worker` until SQLite is replaced by a
multi-writer job/state store and a Redis distributed model semaphore is added.

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
| `GET` | `/api/v1/sessions/{session_id}/runs` | list recent chat and research runs |
| `POST` | `/api/v1/chat` | compatibility chat facade that creates a chat run |
| `POST` | `/api/v1/runs` | create a `chat`, `research`, or `daily` run |
| `GET` | `/api/v1/runs/{run_id}` | inspect status, safe metrics and final answer |
| `GET` | `/api/v1/runs/{run_id}/events` | read SSE `status`, `token`, `tool`, `done`, `error` events |
| `POST` | `/api/v1/runs/{run_id}/cancel` | request cross-process cooperative cancellation |

`POST /api/v1/sessions/{session_id}/chat-runs` remains a compatibility alias
while clients migrate to the canonical endpoint.

Example:

```bash
TOKEN='your-long-random-secret'

curl -X POST http://localhost:7860/api/v1/sessions \
  -H "X-API-Key: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"API session"}'

curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"chat","session_id":"<session_id>","message":"Explain the RAG cold-start fallback."}'

curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"research","session_id":"<session_id>","query":"Compare RAG retrieval reranking methods."}'

curl -X POST http://localhost:7860/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"daily","daily_kind":"search","keyword":"retrieval augmented generation"}'

curl -N -H "X-API-Key: $TOKEN" \
  http://localhost:7860/api/v1/runs/<run_id>/events
```
