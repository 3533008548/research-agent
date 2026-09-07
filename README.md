

<div align="center">

# 🔬 Research Assistant — 科研助手 Agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![tests](https://github.com/3533008548/research-agent/actions/workflows/test.yml/badge.svg)](https://github.com/3533008548/research-agent/actions/workflows/test.yml)

**搜索论文 · 阅读 PDF · RAG 检索 · 多模态看图 · 每日速递 · 用户画像**

</div>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 🔍 **论文搜索** | OpenAlex 通用检索 + arXiv 预印本检索 + 可选 IEEE Xplore 元数据检索；结果统一去重 |
| 📄 **PDF 阅读** | PyMuPDF 文本块双栏重排 + pdfplumber 表格 + 图注感知图片提取 |
| 💬 **多会话管理** | 新建、切换、加载和永久删除会话；历史、摘要、Token 统计互相隔离 |
| 🧠 **RAG 检索** | 章节感知切块 + 语义/BM25 RRF + 可选本地重排 + 公式保护 |
| 🖼 **多模态看图** | DeepSeek 视觉模型描述 + 图注附带 + 描述缓存（省 token） |
| 📰 **每日速递** | OpenAlex + OpenAIRE + DBLP 并行限时的后台推送；配置 IEEE Key 后自动纳入 IEEE Xplore，支持重试和待读清单管理 |
| 📁 **研究档案** | 将研究方案、假设或决策记录保存为可检索 Markdown 与可下载 Word 文档；按需读取，不注入会话摘要或用户画像 |
| 👤 **用户画像** | Markdown 自动维护，Agent 从对话中学习偏好 |
| 🧩 **辅助记忆** | 当前会话摘要自动压缩注入；论文方法/结果以全局三元组按需检索，会话之间严格隔离 |
| ✅ **自动验证** | verify 分级（严重重生成/轻微提示）+ 跳过门控 + 8 秒限时熔断降级 |
| 📊 **Token 管理** | 按工具差异化截断 + 缓存命中统计 + 预算预警 + 费用估算 |
| 🛡️ **请求韧性** | 同模型重试、429 排队、端到端截止时间、半开熔断、流式中断恢复与用户主动取消；不自动降级模型 |
| 🔐 **工具运行时** | 统一授权范围、协作式取消、结果限长与脱敏工具事件；普通对话和深度研究共用同一执行边界 |

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

`.env` 中设置 API Key，`config.yaml` 管理其他设置：

```env
# OpenAlex 只填写原始 Key；不加 Bearer 前缀，也不要提交 .env
OPENALEX_API_KEY=your-openalex-key

# 可选：IEEE Xplore Metadata API Key。只查询元数据；仅对明确标记为
# Open Access 的记录展示直接 PDF 链接。
IEEE_API_KEY=your-ieee-api-key
```

每日推送默认使用 OpenAlex、OpenAIRE 与 DBLP；配置 `IEEE_API_KEY` 后自动加入 IEEE Xplore。`/daily search` 和“深度研究”的公开文献部分默认合并 OpenAlex 与 arXiv，同样会在配置 IEEE Key 后加入 IEEE 元数据。OpenAlex Key 未配置时接口仍可尝试匿名请求，但生产使用应配置该变量以获得稳定额度与可观测的限流响应。IEEE 的受限内容只保留落地页链接，不会被当作可下载全文。

```env
DEEPSEEK_API_KEY=sk-***
DEEPSEEK_VISION_MODEL=deepseek-v4-flash-vision-exp  # 可选，默认值
```

`DEEPSEEK_API_URL` 默认为官方 DeepSeek 地址；仅在自建兼容网关或本地测试模拟服务时覆盖，正常使用无需设置。

### 3. 启动（Web UI）

```bash
python web_ui.py                # 浏览器打开 http://localhost:7860
python web_ui.py -m deepseek-chat --port 8080
```

终端版：`python research_agent.py`

不传 `-m` 时，Web UI 和 CLI 都遵循统一配置优先级：命令行 > 环境变量 > `runtime/primary/settings.json` > `config.yaml` > 默认值。

### React 前端（渐进迁移）

`frontend/` 是独立的 React + TypeScript + Vite 客户端；它只调用 FastAPI 的版本化接口，
不直接访问 SQLite、Redis 或 Agent 实现。当前它覆盖会话、对话、深度研究（来源范围选择、继续未完成任务）、SSE 流式输出、
取消任务、PDF/图片上传、研究档案、论文库、每日检索（执行、重试、继续、阅读状态、
按次报告标签页）和运行中心 / Badcase。运行中心还显示当前会话的持久化 Token 统计。
Gradio 仍保留在根路径，作为旧工作流的回退入口。

本地开发时先启动 API 服务，再启动 Vite。若只验证页面和普通对话，可只启动 API；深度研究和
每日任务还需要 Redis 与独立 worker（最省事的方式仍是 `docker compose up -d`）。不使用 Docker
时，在两个终端中分别运行：

```powershell
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
python api_worker.py

# 另开一个终端
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
python -m uvicorn api_server:app --host 127.0.0.1 --port 7860
cd frontend
npm install
npm run dev
```

浏览器访问 `http://localhost:5173/`。Vite 会把 `/api` 请求转发到 FastAPI；如果启用了
`API_AUTH_REQUIRED=1`，在左下角填写本地 `.env` 中的 `API_AUTH_TOKEN`。该值仅保存在当前
浏览器标签页的 `sessionStorage`。执行 `npm run build` 后，FastAPI 会在 `http://localhost:7860/app/`
提供同一套前端；Docker 镜像构建会自动完成此步骤。原 Gradio UI 继续位于根路径 `/`，可用于
尚未迁移的功能和回退。

### 运行时数据目录

代码、静态配置和用户数据相互隔离。默认数据目录为项目下的 `runtime/`，也可通过环境变量或启动参数改为其他位置：

```bash
APP_DATA_DIR=/path/to/research-agent-data python web_ui.py
python web_ui.py --data-dir ./runtime
```

`runtime/primary/` 保存 SQLite、论文 PDF、用户画像与设置；`runtime/derived/` 保存可由原始数据重建的 Chroma 索引和 PDF 图片。`runtime/primary/db/badcases.db` 是本地 Badcase 候选池，只保存脱敏运行快照和人工分类。旧版根目录数据不会自动移动，可先预览再迁移：

```bash
python scripts/migrate_runtime.py
python scripts/migrate_runtime.py --apply
python scripts/backup_runtime.py --output backups/
```

PDF 阅读器或分块规则升级后，重建本地索引：

```powershell
python scripts/reindex_local_papers.py --data-dir runtime --max-pages 20
```

重建会为每篇论文生成页面级 `document_map.json`：正文、表格、图片、图注和相邻正文保留在同一页的关联中。`query_papers` 命中表或图时，会额外返回图表说明与必要的邻近正文；旧索引仍可查询，但不会具备这项补充上下文能力。

可选的中文/英文本地重排器首次下载和自检后，才在 `config.yaml` 中开启 `rag.reranker.enabled`：

```powershell
python scripts/warm_reranker.py
```

### Docker Compose 部署

Docker Desktop 启动后，先根据示例创建本地密钥文件，再构建并启动服务：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY；如需 IEEE 检索，再填 IEEE_API_KEY
docker compose up --build -d
```

镜像以非 root 用户运行，并已为 Chroma/ONNX 等运行时依赖配置独立的可写缓存目录；首次构建向量索引时会下载所需模型文件，耗时会略长于后续启动。下载或初始化超过 8 秒时，`query_papers` 会立即改用本地关键词候选并让语义索引继续在后台完成，避免聊天页面一直等待；该缓存位于 `runtime/derived/`，重建容器后仍会保留。

浏览器访问 `http://localhost:7860`。所有会话、论文、用户画像和设置都会写入宿主机的 `runtime/`，因此可安全执行 `docker compose down` 或升级镜像而不丢失数据。常用运维命令：

```powershell
docker compose logs -f
docker compose ps
docker compose down             # 仅停止并删除容器，不删除 runtime 数据
docker compose up -d --build    # 代码更新后重建并启动
```

本版本默认启用本地 RAG 重排器。镜像重建后先在容器内预热一次；模型缓存保存在挂载的 `runtime/derived/cache/`，后续重启不会重复下载：

```powershell
docker compose exec research-agent python scripts/warm_reranker.py
docker compose restart research-agent api-worker
```

若需改宿主机端口，在 `.env` 设置 `UI_PORT=8080`，随后访问 `http://localhost:8080`。`.env` 不会被复制到镜像或提交到 Git。

若构建阶段无法访问 Docker Hub（如 `auth.docker.io:443` 超时），这属于网络或 Docker Desktop 代理问题，而非项目依赖问题。优先在 Docker Desktop 的 **Settings → Resources → Proxies** 配置当前网络可用的 HTTP/HTTPS 代理；也可以在 `.env` 设置可访问镜像仓库中的 Python 3.11-slim 地址，例如：

```env
PYTHON_IMAGE=<你的镜像仓库>/library/python:3.11-slim
```

保存后重新执行 `docker compose up --build -d`。该变量只替换基础镜像来源，不影响应用镜像名称和 `runtime/` 数据卷。

---

## 💬 使用示例

```
>>> 搜索 diffusion model 在网络调度中的最新论文
>>> 精读第1篇                      ← 自动下载+索引+图片提取+摘要卡片
>>> 为第1篇生成论文证据卡          ← 保存带页码/正文/图表锚点的精读 Markdown
>>> 表 2 的结果由哪段正文解释？     ← 返回表格、图表说明与相邻正文
>>> 这篇的损失函数是什么？          ← RAG 章节与页面感知检索
>>> 描述 Figure 3                  ← DeepSeek 视觉模型看图（带图注）
>>> 对比已读论文的技术路线          ← 跨论文分析
```

Web UI 顶部的会话栏可新建、切换和删除会话。删除前必须勾选确认；旧版固定的 `research-main` 历史会自动迁移为“历史会话”。CLI 中仍可用 `/new` 新建会话。

需要对一个问题做有边界的深度研究时，在对话框输入问题后点击 **🧭 深度研究**（可选择“本地论文 + 公开文献 / 仅本地论文 / 仅公开文献”）。它会先规划问题，再由最多两名研究员并行收集证据，最后完成综合、质检和最多一次修订；最终报告附带 `[E1]` 等证据编号与来源索引。研究只保存用户问题和最终报告，不会把内部工具消息写进会话历史。中途点击“停止”会保留已经收集的证据；在同一会话点击“继续上次研究”或输入 `/research continue` 可从这些证据继续。

深度研究使用受限的工具集：本地研究员只能读取当前论文库，公开研究员只能检索公开论文；若需要新上传文件，请先在普通对话中完成上传和索引，再发起研究。

普通对话中可以明确要求“为这篇论文生成证据卡”。它会读取已索引的本地 PDF，生成 `paper-card.md` 与 `source_map.json`；卡片中的论文事实以 `【论文 p.N · Sxxx】` 定位到提取块，模型推断会标记为 `【分析】`。PDF 文本不足或模型暂不可用时，系统会保留可追溯的证据草稿，而不会补写未提取到的实验或页码。

需要把暂定研究方案留在对话之外时，直接说“把这份方案保存为研究文档”。Agent 会生成自包含的 Markdown 原稿和同版本 `.docx`，放入 `runtime/primary/research_documents/`；页面左侧的“研究档案”面板可预览原稿并下载 Word 文档。之后说“查一下之前保存的 TSN 方案”即可按关键词检索，只有选中的文档会被读取，不会自动塞入会话摘要或用户画像。文档保存的是工作草稿，不能替代论文证据或引用。

### 标记 Badcase，形成可维护的回归样例

在 **Agent 运行中心 → 标记问题（Badcase 候选）** 中选择问题类型；默认关联当前会话最新任务，也可在“手动标记”中粘贴运行卡片里的 `run_id`。候选只记录运行类型、状态、耗时、受限指标和脱敏事件时间线，绝不会自动复制问题、模型回答、论文正文或工具参数/结果。可选备注由你手动填写，因此也不能粘贴原始内容或密钥。

候选先保存在本机的 `badcases.db`，不会自动进入 Git 或评测集。审核后导出一个空的合成夹具草稿，再手工填写可公开、可复现的输入和断言：

```powershell
python scripts/export_badcase_template.py --candidate-id bc-xxxxxxxxxxxx
```

删除会话时，关联的 Badcase 候选也会一并删除；如需长期保留某个问题，应先将其转写为合成评测样例。

常用 Web UI 命令：

| 命令 | 说明 |
|------|------|
| `/model` `/tokens` `/profile` | 查看模型、当前会话用量和用户画像 |
| `/retry` | 重发当前进程中最后一个模型请求；流式中断后可用 |
| `/indexed` | 查看已索引论文 |
| `/daily add <关键词>` | 添加每日检索关键词 |
| `/daily search <关键词>` | 立即执行一次不落库的 OpenAlex + arXiv 临时检索；配置 IEEE Key 后加上 IEEE Xplore |
| `/daily retry` | 清除当日检索记录后重新检索已启用关键词 |
| `/daily resume` | 从上次停止或失败时已保存的候选继续，不重复访问来源 API |
| `/daily unread` | 查看最近三天的待读清单 |
| `/research <问题>` | 以默认的本地 + 公开来源启动深度研究 |
| `/research --sources=local <问题>` | 只检索已索引的本地论文（也可用 `public`） |
| `/research continue` | 在当前会话中继续上次未完成的深度研究 |

---

## 🧪 验证

```bash
python tests/test_core.py
python -m evals.release_gate --strict
```

核心回归测试覆盖 PDF 提取、分块/RAG、图结构构建、验证重试状态清理、每日多 Agent 检索（跨源去重、单次批量 Curator、可恢复运行）、多会话隔离和硬删除、统一数据目录、同模型重试、端到端截止时间、熔断、流式中断恢复、深度研究的证据持久化/继续/修订、Badcase 脱敏快照/会话删除，以及取消令牌从浏览器到模型/工具节点的传播。

浏览器回归测试覆盖两类场景：会话隔离测试会启动本地 SSE 模拟服务，复现“旧会话流式输出中，新建会话并发送第一条命令”的竞态；挂载 API 测试会启动临时 FastAPI + Gradio 服务，确认 Gradio 通过 HTTP 创建运行任务、消费 SSE，并在切换会话时取消旧任务。两者都不读取用户 `runtime/`，也不访问真实模型。首次运行需安装 Chromium：

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest tests.test_session_e2e tests.test_mounted_api_e2e
```

GitHub Actions 会分别运行核心回归和两类浏览器回归测试。

FastAPI 同时提供受 API 令牌保护的 `GET /api/v1/metrics` Prometheus 文本端点，用于
监控 Redis 队列积压、各类 worker 心跳和运行状态计数。指标只包含固定标签与聚合数字，
不包含用户输入、模型回答、论文信息或工具参数。

另提供 14 项版本化科研 Agent 能力任务和 5 项离线可靠性回放，使用合成语料和模拟状态，不读取个人运行数据、不调用真实模型 API。`python -m evals.release_gate --strict` 会输出统一的发布质量门禁；任务、预期证据、工具轨迹、性能门槛和人工评分量表位于 [evals/README.md](evals/README.md)。小样本真实模型评测必须通过 `python scripts/run_real_eval.py --task T04 ...` 执行：它使用临时隔离运行时并仅保存脱敏报告，避免评测会话出现在网页列表。面向真实用户的 19 项研究旅程验收集使用 `python scripts/run_user_acceptance.py --suite core` 建立可人工复核、可前后比较的模型效果基线。

---

## 🧱 技术架构

```
LangGraph ReAct 循环 (graph_builder.py)
  START → LLM ⇄ Tools → Verify(分级) → END

深度研究闭环 (research_orchestrator.py)
  Planner → 本地证据研究员 ∥ 公开文献研究员 → Synthesis → Critic → Revision(最多一次)

研究运行记录与会话共用 checkpoint.db，以 thread_id 关联；删除会话会同步删除可恢复的计划、证据、质检结果和最终报告。

每日检索闭环 (daily_orchestrator.py)
  Rule Planner → OpenAlex Scout ∥ OpenAIRE Scout ∥ DBLP Scout ∥ IEEE Scout（配置 Key 时）
  → Normalizer/Deduper → Quality Gate → Curator(整次任务一次) → Conditional Critic → Delivery

每日运行记录保存在 daily.db；来源请求按域名限流，任务停止或失败时可从保存的候选恢复。Curator 使用共享 LLMClient 的低优先级请求，模型暂不可用时只保留可解释的规则排序，不切换模型。公开论文检索默认合并 OpenAlex 与 arXiv；配置 IEEE Key 后加入 IEEE Xplore 元数据。系统按 DOI、arXiv ID、标题相似度去重，并保留每条记录的来源、相关性依据和部分来源失败信息。IEEE 记录只有 `accessType=Open Access` 时才会额外展示直接 PDF 链接。

Verify 失败时仅将反馈保存在当前重试链路；本轮完成、跳过或达到重试上限后自动清理，避免影响后续对话。

运行时数据层（APP_DATA_DIR，默认 ./runtime）:
  primary/db/          → checkpoint、memory、daily、badcases SQLite 数据
  primary/papers/      → 原始论文 PDF
  primary/research_documents/ → 用户研究档案：Markdown 原稿与 Word 导出
  primary/profile.md   → 用户画像
  primary/settings.json→ Web UI 用户设置
  derived/chroma/      → 论文向量库（可重建）
  derived/images/      → PDF 提取图片与图注（可重建）
  derived/paper_artifacts/ → 页面元素映射、论文来源映射、证据卡与审计结果（可重建）
```

---

## 📦 项目结构

```
research_agent/
├── prompts.py            # 系统提示词 + 少样本范例
├── tool_schemas.py       # 12 个工具 JSON Schema
├── tool_catalog.py       # 工具声明目录：名称、Schema、展示名与结果限长
├── tool_runtime.py       # 工具授权/取消/限长/脱敏事件的统一执行边界
├── graph_builder.py      # LangGraph 图定义 + verify 分级
├── pdf_reader.py         # PDF 页面级元素提取（正文/表格/图片/图注/相邻关系）
├── paper_store.py        # ChromaDB RAG（页面元素块、语义+BM25 RRF、可选重排）
├── paper_artifacts.py    # 页面映射、关联上下文、证据卡与锚点审计
├── research_documents.py # 用户研究档案：可检索 Markdown + Word 导出
├── paper_records.py      # 公开检索/每日检索共用的论文规范化与去重
├── search_api.py         # OpenAlex / arXiv / IEEE Xplore 交互式与深度研究检索 API
├── ieee_xplore.py        # IEEE Xplore Metadata API 参数与响应规范化（含访问权限边界）
├── scheduler.py          # 每日论文检索调度器（自动/临时/重试）
├── daily_orchestrator.py # 每日多 Agent 编排、候选质量门控与恢复
├── user_profile.py       # 用户画像 (Markdown)
├── memory.py             # 记忆模块 (三元组+摘要+图片缓存)
├── conversation_memory.py # 对话摘要门控、生成与持久化策略
├── llm_client.py         # 主模型并发/重试/端到端预算/取消（不做模型降级）
├── cancellation.py       # 浏览器请求的协作式取消原语
├── resilience.py         # 通用三态熔断器（closed/open/half_open）
├── run_contract.py       # 对话、研究、每日任务共用的脱敏运行事件契约
├── badcase_store.py      # 本地 Badcase 候选池与合成夹具草稿导出
├── runtime_paths.py       # 运行时数据边界、版本与用户设置
├── config.py / config.yaml  # 统一配置
├── logger.py             # 结构化日志
│
├── tools/                # 工具实现
│   ├── search.py         # 搜索/查询/列表/删除
│   ├── read_pdf.py       # 下载+提取+图片+索引+摘要
│   ├── research_documents.py # 保存/检索/读取研究档案
│   ├── describe.py       # DeepSeek 视觉模型看图 (缓存+图注)
│   └── profile_tool.py   # 画像更新
│
├── research_agent.py     # 终端 CLI 入口
├── research_orchestrator.py # 有边界的 Planner / Researcher / Critic 闭环
├── session_store.py       # 会话目录、研究运行记录、Token 统计与 checkpoint 联动删除
├── web_ui.py             # Web UI：受控 Chatbot 状态，深度研究进度/停止/继续
├── frontend/             # React + TypeScript 客户端（/app/，仅经 FastAPI 调用后端）
├── scripts/              # 数据迁移、索引重建、重排器预热、备份和 Badcase 夹具导出
├── tests/                # 核心回归 + Playwright 浏览器会话隔离测试
├── .github/workflows/    # CI 自动测试
│
├── 需求决策日志.md         # 功能需求与决策记录 (条目 001-025)
├── 重构说明.md            # 代码变动文档
└── requirements.txt
```

---

## 📄 License

MIT
