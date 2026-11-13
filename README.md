

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
| 🔍 **论文搜索** | OpenAlex 通用检索 + arXiv 预印本检索；结果统一去重 |
| 📄 **PDF 阅读** | PyMuPDF 文本块双栏重排 + pdfplumber 表格 + 图注感知图片提取 |
| 💬 **多会话管理** | 切换或新建会话时原任务继续；回复、草稿、附件与运行状态按会话隔离；支持加载和永久删除会话 |
| 🧠 **RAG 检索** | 章节与跨页续段感知切块 + 语义/BM25 RRF + 可选本地重排 + 公式保护 |
| 🖼 **多模态看图** | DeepSeek 视觉模型描述 + 图注附带 + 描述缓存（省 token） |
| 📰 **每日速递** | OpenAlex + OpenAIRE + DBLP 并行限时的后台推送，支持重试和待读清单管理 |
| 🧭 **深度研究** | 规划 → 三个角色研究员并行取证 → 结构化证据 → 综合 → 质检 → 最多一次修订；双层引用与定位精度审计 |
| 🔌 **MCP Server** | 把检索能力以标准 MCP 协议发布出去，5 个只读工具；其他 Agent 可直接接入本项目 |
| 🔌 **MCP Client** | 反向消费外部 MCP Server；外部工具经同一份工具目录与同一运行时，自动继承白名单/取消/限长 |
| 👤 **用户画像** | Markdown 持久化，支持条目去重、按章节和完整原文精确删除或修正 |
| 🧩 **辅助记忆** | 当前会话摘要自动压缩注入；论文方法/结果以全局三元组按需检索，会话之间严格隔离 |
| ✅ **自动验证** | verify 分级（严重重生成/轻微提示）+ 跳过门控 + 120 秒独立时限与熔断降级 |
| 📊 **Token 管理** | 按工具差异化截断 + 缓存命中统计 + 预算预警 + 费用估算 |
| 🛡️ **请求韧性** | 同模型重试、429 排队、端到端截止时间、半开熔断、流式中断恢复与用户主动取消；不自动降级模型 |
| 🔐 **工具运行时** | 统一授权范围、协作式取消、结果限长与脱敏工具事件；普通对话和深度研究共用同一执行边界 |

---

## 🚀 快速开始

Windows 桌面客户端支持安装后直接启动，无需用户手动运行 Python、Redis 或 Docker。论文库、PDF 阅读与笔记、每日检索和设置都在原三栏工作台内打开。安装、数据迁移和构建方法见 [桌面客户端说明](desktop/README.md)。

### 1. 安装依赖

复现项目已验证的环境（推荐，CI 与 Docker 镜像都用它）：

```bash
pip install -r requirements.lock.txt
```

只想装"最新兼容版本"、不追求与 CI 一致时：

```bash
pip install -r requirements.txt
```

> `requirements.txt` 是**想要什么**（人类维护），`requirements.lock.txt` 是**现在实际是什么**
> （机器生成，133 个包含全部传递依赖）。Dockerfile 与 CI 只认锁文件，因此镜像与
> 流水线可逐字节复现。
>
> **新增或升级依赖时**：改 `requirements.txt` → 运行 `bash scripts/regenerate_lock.sh`
> → 提交 `requirements.txt` 与两个 `*.lock.txt`。漏掉重新生成的话，CI 会继续使用旧锁，
> 等于这次改动根本没生效。脚本会顺带检查 GPU 依赖有没有漏进锁文件。
> torch 及其 GPU 依赖树被刻意排除在锁文件外（由 Dockerfile 从 PyTorch CPU 源单独安装），
> 原因见 `requirements-lock-exclusions.txt`。

### 2. 配置

`.env` 中设置 API Key，`config.yaml` 管理其他设置：

```env
# OpenAlex 只填写原始 Key；不加 Bearer 前缀，也不要提交 .env
OPENALEX_API_KEY=your-openalex-key

# IEEE Xplore 暂时停用；保留 Key 占位，恢复前不会发起该来源请求。
IEEE_API_KEY=your-ieee-api-key
```

每日推送默认使用 OpenAlex、OpenAIRE 与 DBLP；`/daily search` 和“深度研究”的公开文献部分默认合并 OpenAlex 与 arXiv。IEEE Xplore 因 API 暂时不可用已被禁用，即使配置 `IEEE_API_KEY` 也不会发起请求。OpenAlex Key 未配置时接口仍可尝试匿名请求，但生产使用应配置该变量以获得稳定额度与可观测的限流响应。

```env
DEEPSEEK_API_KEY=sk-***
DEEPSEEK_MODEL=deepseek-flash          # DeepSeek V4.1 Flash 的官方模型名
DEEPSEEK_VISION_MODEL=deepseek-flash  # 可选，默认值；DeepSeek V4.1 Flash 原生支持视觉输入
```

`DEEPSEEK_API_URL` 默认为官方 DeepSeek 地址；仅在自建兼容网关或本地测试模拟服务时覆盖，正常使用无需设置。

### 3. 启动（浏览器工作台）

```powershell
docker compose up -d --build
```

浏览器打开 `http://localhost:7860/`，API 文档位于 `http://localhost:7860/docs`。
终端版仍可使用：`python research_agent.py`。

浏览器工作台和 CLI 都遵循统一配置优先级：命令行 > 环境变量 > `runtime/primary/settings.json` > `config.yaml` > 默认值。

可通过 `.env` 中的 `CHAT_RUN_CONCURRENCY=2`、`RESEARCH_RUN_CONCURRENCY=2` 设置任务并发（范围 1–8，重启 worker 生效）；设为 1 可恢复每类任务串行。模型请求总并发仍由 `api_resilience.max_concurrency` 控制。保持一个 `api-worker` 实例，它通过数据目录内的操作系统文件锁拒绝第二个实例。

Docker 的 `api-worker` 会在开始消费任务前预热本地 RAG 嵌入模型；因此首次用户检索不会承担 ONNX 模型加载或下载的等待。预热异常时服务仍可启动，并暂时使用本地关键词检索回退。

### React 前端

`frontend/` 是独立的 React + TypeScript + Vite 客户端；它只调用 FastAPI 的版本化接口，
不直接访问 SQLite、Redis 或 Agent 实现。当前它覆盖会话、对话、深度研究（来源范围选择、继续未完成任务）、SSE 流式输出、
取消任务、PDF/图片上传、论文库、每日检索（执行、重试、继续、阅读状态、
按次报告标签页）、用户画像和运行中心 / Badcase。运行中心还显示当前会话的持久化 Token 统计。
失败或取消的普通对话可在运行检查器中重新发送；对任务的新补充会在下一编排节点生效。
切换或新建会话不会取消原会话的任务，可在其他会话继续提交问题；切回后显示各自的进度和回复，刷新页面后会重新连接所选会话仍在运行的任务。只有显式点击“暂停任务”或删除所属会话才会请求取消任务。草稿与暂存附件在当前页面内按会话保留。标准 Redis 部署默认允许两个普通对话任务和两个深度研究任务并行；同一会话按顺序执行，所有任务仍共享模型请求并发额度。会话列表显示当前页面已观察任务的排队、执行与完成状态。
普通对话的模型失败可在运行检查器或历史运行中查看具体错误代码、阶段、最后 HTTP 状态、耗时与预算、实际重试次数及请求 ID。诊断会持久化；旧记录缺失的信息不会自动补回。HTTP 200 仅代表收到响应头，后续流式读取仍可能超时。

输入框工具区可选择“对话”或“多 Agent 研究”；研究进度、暂停／恢复操作和阶段报告仅在选择研究模式后显示在中间对话区，右侧保留运行检查器。输入框默认单行高度，随输入自动增高至上限，清空后缩回；消息滚动区域延伸至右侧栏边缘，滚动条轨道透明、滑块灰色。用户消息以气泡区分，助手回复使用无框正文，不再显示身份文字和头像。工作台标签行两端的按钮可独立显示／隐藏会话侧栏和运行检查器；桌面可拖动侧栏内边界调宽，也可聚焦边界后用方向键、Home／End 调整。宽度与显隐偏好保存在当前浏览器，已移除恢复默认布局菜单。大于 760px 的窗口保持横向三栏，缩小时压缩栏宽、放大时恢复期望宽度；手机宽度使用纵向排列。长研究问题在页面内换行选择，公式、代码和宽表格在内容块内部滚动。侧栏文字随宽度换行，档案内部布局按工作台实际宽度适配。问题记录和验证范围见[前端布局优化与回归防护方案](前端布局优化与回归防护方案.md)。

助手回复顶部显示本轮计时（含排队、工具处理），结束后保留用时；刷新可从后端运行记录恢复当前轮计时。多 Agent 工作台显示研究历时（包含暂停和等待）。流式输出仅在用户停留底部时自动跟随，向上阅读后保留位置，回到底部或发送新问题后恢复跟随。

右侧运行检查器显示当前活动和工具调用记录：工具中文名与原始名称、调用角色、执行状态、实际耗时及失败类型。普通对话实时推送，多 Agent 研究每 2 秒刷新；相同工具的并行调用独立记录，刷新页面可恢复已保存活动。工具返回明确失败结果时显示失败，不再一律标为完成；运行已结束而缺少调用结束记录时明确标注。旧运行未保存的工具信息无法补回。活动记录不包含工具参数或返回正文。

事实核验使用 `agent.verify_timeout_seconds`（默认 120 秒）的独立请求预算，包含排队与最多一次瞬时失败重试；非流式核验的读取等待也沿用此预算，保留模型既有推理模式。超时后保留回复，并标记“事实核验未完成”，不将请求失败当作发现事实错误。阶段记录可展开查看实际耗时、超时预算、请求次数及错误代码；等待模型服务不再显示为等待用户材料。核验可能增加回复最终完成前的等待时间；上游持续故障仍可能导致失败，无法保证每次核验都成功。

每轮对话最多两次核验、一次定向修正。只有核验器明确标为“严重且需修正”、影响核心结论的事实问题才触发修正；轻微问题或“可忽略”项仅追加提示。复核只针对先前的阻断问题，仍有问题则明确标为待确认并停止自动重写。核验使用普通回答全文（最多 24000 字符）；更长回答及超长证据保留首尾并明确标注程序摘录，不能把输入省略误判为回答未完成。空白、被截断或无法解析的核验结果不当作通过，也不自动触发重写。侧栏分别显示核验轮次、修正决定和最终核验状态；修正稿完成后整体替换初稿，避免把多版回复连续拼接。

前端按**数据归属**拆分：每个业务域一个 hook（`useConversation` 拥有会话与正在跑的 run，
`useDailyWorkspace` / `useRunCenter` / `useDocuments` 各自拥有自己的状态），
工作台面板负责展示与交互，栏宽和显隐由独立的 `useWorkspaceLayout` 管理；hook 之间通过显式参数共享必要状态。

生产构建由 FastAPI 从根路径 `/` 托管。深度研究、每日检索和跨进程可恢复的对话需要 Redis 与独立 worker；没有 Redis 时仅保留进程内普通对话，不适合作为标准部署。最简部署方式为上面的 Docker Compose。前端开发时，在三个终端中运行：

```powershell
# 终端 1：任务 worker
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
python api_worker.py

# 终端 2：FastAPI
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
python -m uvicorn api_server:app --host 127.0.0.1 --port 7860

# 终端 3：React 热更新开发服务器
cd frontend
npm ci
npm run dev
```

浏览器访问 `http://localhost:5173/`。Vite 会把 `/api` 请求转发到 FastAPI；如果启用了
`API_AUTH_REQUIRED=1`，在左下角填写本地 `.env` 中的 `API_AUTH_TOKEN`。该值仅保存在当前
浏览器标签页的 `sessionStorage`。执行 `npm run build` 后，FastAPI 会在 `http://localhost:7860/`
提供同一套前端；Docker 镜像构建会自动完成此步骤。

### 运行时数据目录

代码、静态配置和用户数据相互隔离。默认数据目录为项目下的 `runtime/`，也可通过环境变量或启动参数改为其他位置：

```powershell
$env:APP_DATA_DIR = "D:\\research-agent-data"
python -m uvicorn api_server:app --host 127.0.0.1 --port 7860
```

`runtime/primary/` 保存 SQLite、论文 PDF、用户画像、已确认论文关系与设置；`runtime/derived/` 中的 Chroma 索引和 PDF 图片可以重建，但该目录下并非所有数据都是缓存。Compose 把 Redis 的 AOF/RDB 持久化文件挂载在 `runtime/derived/redis/`，其中保存活动任务队列与运行事件，不能按缓存删除。`runtime/primary/paper_relations.json` 是小型、可审阅的论文关系索引；`runtime/primary/db/badcases.db` 是本地 Badcase 候选池，只保存脱敏运行快照和人工分类。旧版根目录数据不会自动移动，可先预览再迁移：

```bash
python scripts/migrate_runtime.py
python scripts/migrate_runtime.py --apply
python scripts/backup_runtime.py --output backups/
```

`backup_runtime.py` 默认备份 primary 数据和运行时元信息；即使指定 `--include-derived`，也只额外包含 Chroma 与图片，**不包含 Redis 持久化目录**。需要保存队列状态时，应另外按以下维护顺序操作：

1. 停止接收新任务并暂停定时投递，保留 worker 运行，等待队列任务排空；确认没有 `queued`、`running`、`cancelling` 运行，也没有待确认的 Redis 消费组任务。若仍有任务，先等待或通过正常取消流程处理，不删除队列来代替排空。
2. 停止应用和 worker，再正常停止 Redis，等待进程退出，使持久化写入完成。不要在 Redis 运行中直接复制正在变化的 AOF/RDB。
3. 在同一停机时间点运行 primary 备份，并单独复制整个 `runtime/derived/redis/` 到备份位置；保留 Redis 的多文件 AOF 目录及清单、RDB 和原有权限，不只挑选某个 `.aof` 文件。
4. 恢复时保持应用和 worker 停止，使用同一停机时间点的 primary 与 Redis 备份，先启动兼容版本的 Redis 并检查持久化加载，再启动应用和 worker。不要把旧队列与较新的 SQLite 混用，以免任务重放或状态不一致；只有 primary 备份时不能宣称恢复了未完成队列。

清理或重建索引时应明确选中 `derived/chroma/`、`derived/images/` 等目标，不要递归清空整个 `derived/`。上面的维护流程需要运维人员主动执行，启动、迁移和索引重建不会自动替用户备份或删除 Redis 数据。

PDF 阅读器或分块规则升级后，重建本地索引：

PDF 解析会保留超过半页的正常正文，并对有算法标题和边框、但缺少文本层的算法区域执行本地 OCR；扫描页也会尝试 OCR。算法正文独立切块，保留标题、页码、区域坐标、提取方式及原页裁剪图。OCR 转写不保证数学符号完全准确，逐行解释时应通过 `describe_image` 核对原图。无法定位算法正文、OCR 失败或原生文本丢失会阻止入库，备用文本解析也不能绕过检查；原有索引保留。该检测依赖可识别的版式，不保证发现任意 PDF 的所有漏读。

Docker 镜像包含 Tesseract 和中英文语言包。直接运行 Python 时需安装 Tesseract，或将 `TESSDATA_PREFIX` 指向含 `eng.traineddata` 的目录；`PDF_OCR_LANGUAGE` 默认 `eng`，中文扫描内容可设为 `eng+chi_sim`（需相应语言包）。升级代码不会自动修复旧索引，需重建后再检索。

写入前会按嵌入模型的 token 上限复核切块，超长块拆分后使用独立子块 ID，并保留父块、页码和原文元素引用。文档映射与入库自检以最终子块清单为准；回读校验同时核对数量、ID 和正文，失败时报告具体差异并回滚，不能把这类写入一致性错误解释为 PDF 无文本层。

限定论文的查询若明确指定 `Algorithm 4` 或“算法四”，会优先读取对应编号的算法正文，避免被其他算法的相似描述挤出结果。重建脚本使用当前配置的嵌入模型和集合，可追加 `--file 完整文件名.pdf` 只修复一篇论文。

```powershell
python scripts/reindex_local_papers.py --data-dir runtime --max-pages 20
```

重建会为每篇论文生成页面级 `document_map.json`：原始正文、表格、图片、图注和相邻正文始终保留页级关联；另会派生仅供检索的跨页逻辑正文块。只有相邻页、同一章节、下一页未从标题开始且前页未以完整句末结束的正文才会合并；一次最多跨两页，避免将连续页面链式拼接成难以审计的宽范围证据。逻辑块以 1,200 字符为目标、1,320 字符为软硬边界继续切分，并保留起止页与全部源元素 ID，因此引用会显示为 `p.3–4`，仍可回查原页。`query_papers` 命中表或图时，会额外返回图表说明与必要的邻近正文；旧索引仍可查询，但不会具备这项补充上下文能力。PDF 阅读器或切块规则升级后，应执行上面的重建命令以应用新规则。

本地论文还可建立轻量“论文关系增强检索”：只保存用户确认且带页码或 chunk 锚点的**一跳**关系（方法相似/改进、实验可比、结果冲突、明确引用）。关系不构成论文事实，也不会自动由模型猜测生成；检索先完成语义 + BM25 RRF，再从已有相关论文的一跳关系中补充仍能匹配当前问题的候选，并给予有限加分，最后交给可选 reranker。它不引入图数据库、图遍历或 GraphRAG；删除论文会同步删除相关关系边。

当前仓库配置使用 `intfloat/multilingual-e5-small` 嵌入模型（384 维、512 token），集合为 `papers_e5`；旧 MiniLM 与 Qwen3 保留为可选后端。切换嵌入模型需要新集合并重新索引，不能直接复用旧向量空间。有效配置仍以启动时的配置来源为准。

可选的中文/英文本地重排器首次下载和自检后，才在 `config.yaml` 中开启 `rag.reranker.enabled`：

```powershell
python scripts/warm_reranker.py
```

### Docker Compose 部署

Docker Desktop 启动后，先根据示例创建本地密钥文件，再构建并启动服务：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY；IEEE_API_KEY 当前无需填写
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

需要对一个问题做有边界的深度研究时，在对话框输入问题后点击 **🧭 深度研究**（可选择“本地论文 + 公开文献 / 仅本地论文 / 仅公开文献”）。它会先规划问题，再由三名角色研究员并行收集证据——本地证据研究员、文献与反例研究员、反证与边界研究员——最后经过结构化证据分析、综合、质检和最多一次修订；最终报告附带 `[E1]` 等证据编号、跨来源证据矩阵与引用溯源审计。研究只保存用户问题和最终报告，不会把内部工具消息写进会话历史。中途点击“停止”会保留已经收集的证据；在同一会话点击“继续上次研究”或输入 `/research continue` 可从这些证据继续。

深度研究使用受限的工具集，三名研究员的权限互不相同：本地研究员只能读取当前论文库，两个公开角色只能检索公开论文；**外部 MCP 工具只授予文献与反例研究员**，避免能力意外扩散。若需要新上传文件，请先在普通对话中完成上传和索引，再发起研究。

普通对话中可以明确要求“为这篇论文生成证据卡”。它会读取已索引的本地 PDF，生成 `paper-card.md` 与 `source_map.json`；卡片中的论文事实以 `【论文 p.N · Sxxx】` 定位到提取块，模型推断会标记为 `【分析】`。PDF 文本不足或模型暂不可用时，系统会保留可追溯的证据草稿，而不会补写未提取到的实验或页码。

科研档案功能及其 Agent 工具、API 和前端入口已移除。旧版保存的 `runtime/primary/research_documents/` 文件保留在磁盘，备份脚本仍会收录；升级不会自动删除已有资料。

代码复现功能已移除，包括实验项目页、仓库准备和按论文方法还原。旧版生成的 `runtime/primary/experiment_projects/` 文件保留在磁盘，备份脚本仍会收录；升级不会自动删除已有研究资料。

### 🔌 作为 MCP Server：把检索能力发布出去

本项目可以作为一个标准 MCP Server 被其他 Agent 接入，暴露 5 个**只读**工具：

| 工具 | 作用 |
|------|------|
| `search_papers` | 搜索学术论文元数据（OpenAlex / arXiv），返回标题、作者、年份、链接与摘要 |
| `query_papers` | 在本地已索引论文库中做混合检索（语义 + 词法 + 重排），返回带页码与章节的原文段落 |
| `list_papers` | 列出已下载到本地的论文 |
| `list_indexed_papers` | 列出已进入向量库、可被混合检索的论文及其块数 |
| `retrieval_metrics` | 返回本进程检索各阶段的聚合耗时统计（语义 / 词法 / 融合 / 重排） |

```bash
python mcp_server.py          # 以 stdio 传输启动
```

**刻意不暴露写工具。** MCP 是对外公开面，而写操作没有配额、审计与门控——把「删除论文」「修改档案」这类能力放到公开接口上，代价与收益不成比例。详见 [MCP_SERVER.md](MCP_SERVER.md)。

### 🔌 作为 MCP Client：消费外部 MCP Server

反过来，本项目也可以接入**别人**的 MCP Server，让 Agent 调用本项目没有实现的能力：

```yaml
# config.yaml —— 默认关闭，连到外部进程是运维的决定
mcp:
  enabled: true
  tool_timeout_seconds: 30
  circuit_failure_threshold: 3
  circuit_recovery_seconds: 120
  servers:
    - name: arxiv
      command: uvx
      args: ["arxiv-mcp-server"]
      allowed_tools: ["search_papers"]   # 白名单；留空 = 该服务全部工具
      timeout_seconds: 30
```

外部工具以 `mcp__<服务名>__<工具名>` 暴露，且**注册进同一份 `tool_catalog`、经同一个 `tool_runtime` 执行**——因此自动继承能力白名单、协作式取消、结果长度上限与生命周期事件，不需要另写分发器。内置工具优先：外部 Server 恰好用了同名工具也替换不掉内置定义。

信任边界上，外部工具的输出是**数据而不是指令**：聊天系统提示词里有专门的「工具结果的信任边界」一节，研究 worker 提示词也要求把 `mcp__` 输出标为第三方主张。外部工具只授予 `public` 研究员角色。详见 [MCP_CLIENT.md](MCP_CLIENT.md)。

### 标记 Badcase，形成可维护的回归样例


在 **Agent 运行中心 → 标记问题（Badcase 候选）** 中选择问题类型；默认关联当前会话最新任务，也可在“手动标记”中粘贴运行卡片里的 `run_id`。候选只记录运行类型、状态、耗时、受限指标和脱敏事件时间线，绝不会自动复制问题、模型回答、论文正文或工具参数/结果。可选备注由你手动填写，因此也不能粘贴原始内容或密钥。

候选先保存在本机的 `badcases.db`，不会自动进入 Git 或评测集。审核后导出一个空的合成夹具草稿，再手工填写可公开、可复现的输入和断言：

```powershell
python scripts/export_badcase_template.py --candidate-id bc-xxxxxxxxxxxx
```

删除会话时，关联的 Badcase 候选也会一并删除；如需长期保留某个问题，应先将其转写为合成评测样例。

论文库和设置在中间工作台中以标签页打开；每日检索与用户画像位于设置中。论文库支持打开原文与交给助手阅读。普通对话、深度研究和每日检索均进入统一运行中心，可暂停、查看事件和在适用时重试或继续。

Windows 桌面端 0.2.2 起支持“设置 → 通用 → 应用更新”：检查版本、下载、重启安装。更新源使用 GitHub Releases，保留论文、笔记和配置；旧客户端需手动安装带更新能力的版本一次。构建和发布流程见 [桌面客户端说明](desktop/README.md#应用内更新)。

---

画像保存在 `APP_DATA_DIR/primary/profile.md`。可在对话中要求保存、删除或纠正条目；Agent 先通过 `read_profile(section)` 取得原文，再调用 `update_profile` 的 `remove_entry` 或 `replace_entry`（参数为 `section`、原文 `content`，修正另传 `replacement`）。原文不含 `- ` 列表前缀，仅忽略首尾空白，不做子串或语义匹配；找不到原文时明确报错。原有新增和偏好设置操作继续可用，同章节相同条目重复新增不会重复写入，操作涉及的历史重复副本会合并处理。未知操作会报错，同内容操作返回未变更；只有工具实际执行后才会持久化。

## 🧪 验证

```bash
# 后端离线测试（模块列表与 CI 的 offline-quality job 一致）
python -m unittest tests.test_core tests.test_chunking \
  tests.test_lexical tests.test_rerank tests.test_hybrid \
  tests.test_retrieval_metrics tests.test_metrics_chain \
  tests.test_stage_timing tests.test_mcp_server tests.test_mcp_stdio \
  tests.test_dependency_lock_coverage tests.test_mcp_client \
  tests.test_mcp_client_e2e tests.test_mcp_client_wiring \
  tests.test_research_evidence tests.test_research_gates \
  tests.test_conversation_reference tests.test_paper_matching \
  tests.test_paper_scope tests.test_relation_review \
  tests.test_ci_test_coverage tests.test_llm_diagnostics \
  tests.test_error_diagnostics_contract tests.test_user_profile

# 前端纯函数测试（时间轴归约 + 标签词汇）
cd frontend && npm test

# 发布质量门禁
python -m evals.release_gate --strict
```

> **测试模块必须与 CI 列表一一对应。**
> `tests/test_ci_test_coverage.py` 会比对磁盘上的测试模块与
> `.github/workflows/test.yml` 里 offline job 列出的模块——漏加一个，
> 那个模块就永远不会在 CI 里跑，**而绿色的 CI 徽章会显得比实际保证得更多**。
> 这个守卫不是形式：它存在之前，有 8 个模块从未在 CI 跑过，
> 其中 `test_research_evidence.py` 里有两个坏测试已经坏了很久而无人察觉。

核心回归测试覆盖 PDF 提取、分块/RAG、论文关系的确认/锚点/一跳检索约束、验证重试状态清理、每日多 Agent 检索（跨源去重、单次批量 Curator、可恢复运行）、多会话隔离和硬删除、统一数据目录、同模型重试、端到端截止时间、熔断、流式中断恢复、深度研究的证据持久化/继续/修订与阶段边界、引用与定位精度审计、工具白名单、MCP Server 与 Client 的端到端接线、Badcase 脱敏快照/会话删除，以及取消令牌从浏览器到模型/工具节点的传播。

浏览器回归测试会启动临时 FastAPI + React 服务，确认浏览器通过统一运行接口创建任务、消费 SSE，并在切换会话时取消旧任务。它不读取用户 `runtime/`，也不访问真实模型。首次运行需安装 Chromium，并先构建前端：

```bash
pip install -r requirements-dev.lock.txt
python -m playwright install chromium
cd frontend && npm ci && npm run build && cd ..
python -m unittest tests.test_mounted_api_e2e
```

GitHub Actions 有四个常规 job：离线质量门禁、前端归约测试、Docker 构建与健康检查、浏览器与 FastAPI 集成测试。另有条件运行的 `chroma-integration`：每周一定时运行，或手动触发 workflow 并启用 `run_chroma_integration` 时运行；普通 push / pull request 不运行该集成 job。离线质量门禁设置 `SKIP_CHROMA_INTEGRATION=1`，避免下载 Chroma 的 ONNX 模型。

FastAPI 同时提供受 API 令牌保护的 `GET /api/v1/metrics` Prometheus 文本端点，用于
监控 Redis 队列积压、各类 worker 心跳和运行状态计数。指标只包含固定标签与聚合数字，
不包含用户输入、模型回答、论文信息或工具参数。

另提供 14 项版本化科研 Agent 能力任务和 5 项离线可靠性回放，使用合成语料和模拟状态，不读取个人运行数据、不调用真实模型 API。`python -m evals.release_gate --strict` 会输出统一的发布质量门禁；任务、预期证据、工具轨迹、性能门槛和人工评分量表位于 [evals/README.md](evals/README.md)。小样本真实模型评测必须通过 `python scripts/run_real_eval.py --task T04 ...` 执行：它使用临时隔离运行时并仅保存脱敏报告，避免评测会话出现在网页列表。面向真实用户的 19 项研究旅程验收集使用 `python scripts/run_user_acceptance.py --suite core` 建立可人工复核、可前后比较的模型效果基线。

---

## 🧱 技术架构

项目里有 **三条彼此独立的编排链路** 和 **两个方向的 MCP 接口**。它们共用同一份工具目录与同一个工具运行时，但编排方式完全不同——这也是本项目最想说明清楚的一件事。

```
▌对话（graph_builder.py）
│  LangGraph 循环，同一张图反复走：
│    START → LLM ⇄ Tools → Verify(分级) → END
│  工具：全部 29 个（可按角色收窄）

▌深度研究（research_orchestrator.py）
│  父级是普通 Python 函数（不是图），并行驱动若干独立图实例：
│    Planner ──▶ Researchers ×3（各自 build_graph 独立实例）
│                  ├─ local     本地库只读
│                  ├─ public    公共检索 + 外部 MCP 工具
│                  └─ boundary  专门找反例与适用边界
│            ──▶ Evidence Analysis（纯规则，无模型）
│            ──▶ Synthesis ──▶ Critic ──▶ Revision（最多一次）
│  只有 Researcher 跑图；其余四个阶段是无工具的纯文本阶段

▌每日检索（daily_orchestrator.py）
│  Rule Planner ──▶ Scouts ×3（OpenAlex ∥ OpenAIRE ∥ DBLP，HTTP 抓取）
│                ──▶ Normalizer/Deduper ──▶ Quality Gate
│                ──▶ Curator（整次任务一次） ──▶ Critic（条件式）
│                ──▶ Delivery
│  只有 Curator 与 Critic 用模型；两者失败都回落到可解释规则排序

▌MCP Server（mcp_server.py）
│  把本项目的检索能力发布成标准 MCP 协议：
│    search_papers · query_papers · list_papers
│    list_indexed_papers · retrieval_metrics
│  只读、不暴露写工具——MCP 是对外公开面，写操作没有配额/审计/门控

▌MCP Client（mcp_client.py）
│  消费外部 MCP Server，外部工具注册进同一份 tool_catalog，
│  经同一个 tool_runtime 执行——因此自动继承白名单、取消、
│  结果限长与生命周期事件。唯一的例外是熔断与超时，因为
│  ToolRuntime.deadline_monotonic 至今无人设置，是空壳。
```

### 多 Agent 的权限模型

**权限不靠提示词约束，靠运行时强制。** 每个角色拿到的工具集合在 `ToolExecutionContext.allowed_tool_names` 里声明，`tool_runtime` 在每次执行前校验，不在白名单就返回 `Tool is not available in this {run_kind} role`。

| 角色 | 工具白名单 | 职责 |
|------|-----------|------|
| `local` 本地证据研究员 | `query_papers` `list_papers` `list_indexed_papers` | 只读本地已索引库 |
| `public` 文献与反例研究员 | `search_papers` **+ 外部 `mcp__` 工具** | 公开文献取证，并独占外部 MCP 工具权限 |
| `boundary` 反证与边界研究员 | `search_papers` | 专门找反例、失败模式、泛化边界 |

外部 MCP 工具**只授予 `public`**——能力不该意外扩散，所以必须点名授予。`public` 与 `boundary` 都可以通过 `search_papers` 访问公开论文来源；独占的是外部 MCP 权限。

### 阶段边界：每个阶段只交付一件事

越界是丢掉审计链最便宜的方式，所以每个阶段都有明确的禁止清单：

| 阶段 | 边界 |
|------|------|
| Researcher | 不下结论、不排序、不写综述；取不到原文定位就报「无法定位」而不是改写成发现 |
| Synthesis | 不重跑检索、不引入证据列表外的 ID、**不自证已验证**（验证归下一阶段） |
| Critic | 只返回判决，**不改写草稿**、不加主张 |
| Revision | 只删改无支撑的主张、保留限制；不引入新主张/新 ID/新章节 |

两个阶段各有一道**机器可判**的关卡，落在提示词之外：

- **规划关卡**：planner 的子任务 id 必须落到某个研究员角色上，否则规划会被静默丢弃
- **引用关卡**：不存在的证据编号或无法支撑的精确定位会强制触发修订，修订后仍不成立则运行状态降级为 `partial_failed`

### 引用与定位的诚实性

深度研究采用**双层引用**：可见的 `[E1]` + 隐藏的 `<!--ref:E1--><!--anchor:page:7-->`，让「结论 → 证据 → 原文位置」可以被机械校验。

关键取舍是**缺失的定位如实标 `none`**，不伪造成看起来可信的页码：

> 「缺失 ≠ 可解析」。伪装成看起来可信的定位符比承认 none 更危险。

审计会专门抓「定位升级」——声称有精确锚点但底层证据其实没解析出位置。

**审计是门控，不只是报告。** 引用不存在的证据编号或把弱定位升级成精确定位，会强制触发一次修订；修订后仍有这两类问题时，运行状态为 `partial_failed`，并保留报告。存在未被正文引用的证据时，引用摘要会列出数量与编号；未引用本身是覆盖情况提示，不作为失败条件，也不自动证明综合遗漏。

### 降级保留产物

三条链路失败时都保留可恢复产物：研究保留已找到的证据、每日检索保留已抓到的候选、对话保留已给出的答案。研究 critic 超时、调用异常或输出无法解析时，判决为 `unknown`；综合回退报告也标为 `unknown`。最终质检仍未知时以 `partial_failed` 交付报告，并提示人工复核。

研究员单次执行仍只有一轮工具调用。某个角色成功结束但没有取到工具证据时，会在原 scope 与工具权限内注明查询改写理由，至多再补检索一次；模型或基础设施错误仍按各自失败策略处理，不借此重复调用。这不会自动切换来源范围。`/research continue` 沿用已保存证据重跑后续分析与综合，不主动补充新检索。

研究运行记录与会话共用 `checkpoint.db`，以 `thread_id` 关联；删除会话会同步删除可恢复的计划、证据、质检结果和最终报告。

每日运行记录保存在 `daily.db`；来源请求按域名限流，任务停止或失败时可从保存的候选恢复。Curator 使用共享 LLMClient 的低优先级请求，模型暂不可用时只保留可解释的规则排序，不切换模型。公开论文检索默认合并 OpenAlex 与 arXiv。系统按 DOI、arXiv ID、标题相似度去重，并保留每条记录的来源、相关性依据和部分来源失败信息。IEEE Xplore 因 API 暂时不可用而被禁用。

Verify 失败时仅将反馈保存在当前重试链路；本轮完成、跳过或达到重试上限后自动清理，避免影响后续对话。

### 运行时数据层（APP_DATA_DIR，默认 ./runtime）

```
primary/db/          → checkpoint、memory、daily、badcases SQLite 数据
primary/papers/      → 原始论文 PDF
primary/experiment_projects/ → 旧版复现产物（仅保留与备份，不再提供功能入口）
primary/paper_relations.json → 用户确认的论文关系：一跳检索提示与证据锚点
primary/profile.md   → 用户画像
primary/settings.json→ Web UI 用户设置
derived/chroma/      → 论文向量库（可重建）
derived/images/      → PDF 提取图片与图注（可重建）
derived/paper_artifacts/ → 页面元素映射、论文来源映射、证据卡与审计结果（可重建）
derived/redis/       → Redis AOF/RDB：活动队列与运行事件（不能按缓存清理）
```

---

## 📦 项目结构

```
research_agent/
├── prompts.py            # 系统提示词 + 少样本范例
├── tool_schemas.py       # Agent 工具 JSON Schema
├── tool_catalog.py       # 工具声明目录：名称、Schema、展示名与结果限长
├── tool_runtime.py       # 工具授权/取消/限长/脱敏事件的统一执行边界
├── graph_builder.py      # LangGraph 图定义 + verify 分级
├── pdf_reader.py         # PDF 页面级元素提取（正文/表格/图片/图注/相邻关系）
├── paper_store.py        # ChromaDB RAG（跨页逻辑块、语义+BM25 RRF、可选重排）
├── paper_relations.py    # 用户确认的论文关系存储与一跳检索提示
├── paper_artifacts.py    # 页面映射、跨页逻辑块、关联上下文、证据卡与锚点审计
├── research_evidence.py  # 证据卡、证据单元、边界矩阵、引用与定位精度审计
├── paper_records.py      # 公开检索/每日检索共用的论文规范化与去重
├── search_api.py         # OpenAlex / arXiv 交互式与深度研究检索 API
├── ieee_xplore.py        # IEEE Xplore Metadata API 参数与响应规范化（当前禁用，保留恢复路径）
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
├── mcp_server.py         # 以 MCP 协议发布检索能力（5 个只读工具）
├── mcp_client.py         # 消费外部 MCP Server，外部工具经同一运行时执行
├── config.py / config.yaml  # 统一配置
├── logger.py             # 结构化日志
│
├── tools/                # 工具实现
│   ├── search.py         # 搜索/查询/列表/删除
│   ├── read_pdf.py       # 下载+提取+图片+索引+摘要
│   ├── describe.py       # DeepSeek 视觉模型看图 (缓存+图注)
│   └── profile_tool.py   # 画像更新
│
├── research_agent.py     # 终端 CLI 入口
├── research_orchestrator.py # 有边界的 Planner / Researcher / Critic 闭环
├── session_store.py       # 会话目录、研究运行记录、Token 统计与 checkpoint 联动删除
├── api_server.py         # FastAPI 入口与 React 生产构建托管
├── api_worker.py         # Redis 队列消费者：对话、研究、每日检索
├── frontend/             # React + TypeScript 客户端（根路径 /，仅经 FastAPI 调用后端）
│   └── src/
│       ├── hooks/        # useConversation / useDocuments / useDailyWorkspace
│       │                 # useRunCenter / useDocuments / useWorkspaceChrome
│       ├── components/   # workspace/ 下的面板、侧栏、聊天面板 + runcenter/
│       └── lib/          # 纯函数：timeline 归约、labels 词汇、workspace 类型
├── scripts/              # 数据迁移、索引重建、重排器预热、备份和 Badcase 夹具导出
├── tests/                # 核心回归 + MCP 端到端 + Playwright 浏览器会话隔离测试
├── .github/workflows/    # CI 自动测试
│
├── MCP_SERVER.md         # 作为 MCP Server 发布的接口与边界说明
├── MCP_CLIENT.md         # 作为 MCP Client 消费外部 Server 的接线与信任边界
├── 工程约定与经验沉淀.md   # 从本项目真实踩坑提炼的工程约定
├── 需求决策日志.md         # 功能需求与决策记录 (条目 001-025)
├── 重构说明.md            # 代码变动文档
└── requirements.txt
```

深度研究多 Agent 设计的**已知未修问题**记在
[求职增强方案_运行中心与MCP.md](求职增强方案_运行中心与MCP.md) 第 7 节；
接入本项目时最该先读的是[工程约定与经验沉淀.md](工程约定与经验沉淀.md)。

---

## 📄 License

MIT
