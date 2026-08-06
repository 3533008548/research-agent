

<div align="center">

# 🔬 Research Assistant — 科研助手 Agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![tests](https://github.com/3533008548/research-agent/actions/workflows/test.yml/badge.svg)](https://github.com/3533008548/research-agent/actions/workflows/test.yml)

**搜索论文 · 阅读 PDF · RAG 检索 · 多模态看图 · 每日速递 · 科研笔记 · 用户画像**

</div>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 🔍 **论文搜索** | Semantic Scholar + arXiv + OpenAlex 三源全量搜索，合并去重 |
| 📄 **PDF 阅读** | pdfplumber 表格/双栏/章节 + 图注感知图片提取（保留子图关系） |
| 💬 **多会话管理** | 新建、切换、加载和永久删除会话；历史、摘要、Token 统计互相隔离 |
| 🧠 **RAG 检索** | 章节感知分层切块 + 章节加权 + 章节过滤 + 公式保护 |
| 🖼 **多模态看图** | GLM-4V 描述 + 图注附带 + 描述缓存（省 token） |
| 📰 **每日速递** | 自定义关键词，三源并行限时的后台自动/临时检索，支持重试和待读清单管理 |
| 📝 **科研笔记** | SQLite 按话题分组，关联论文 |
| 👤 **用户画像** | Markdown 自动维护，Agent 从对话中学习偏好 |
| ✅ **自动验证** | verify 分级（严重重生成/轻微提示）+ 跳过门控 + 8 秒限时熔断降级 |
| 📊 **Token 管理** | 按工具差异化截断 + 缓存命中统计 + 预算预警 + 费用估算 |
| 🛡️ **请求韧性** | 同模型重试、429 排队、端到端截止时间、半开熔断、流式中断恢复与用户主动取消；不自动降级模型 |

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

`.env` 中设置 API Key，`config.yaml` 管理其他设置：

```env
DEEPSEEK_API_KEY=sk-***
GLM_API_KEY=***         # 可选，用于看图
```

`DEEPSEEK_API_URL` 默认为官方 DeepSeek 地址；仅在自建兼容网关或本地测试模拟服务时覆盖，正常使用无需设置。

### 3. 启动（Web UI）

```bash
python web_ui.py                # 浏览器打开 http://localhost:7860
python web_ui.py -m deepseek-chat --port 8080
```

终端版：`python research_agent.py`

不传 `-m` 时，Web UI 和 CLI 都遵循统一配置优先级：命令行 > 环境变量 > `runtime/primary/settings.json` > `config.yaml` > 默认值。

### 运行时数据目录

代码、静态配置和用户数据相互隔离。默认数据目录为项目下的 `runtime/`，也可通过环境变量或启动参数改为其他位置：

```bash
APP_DATA_DIR=/path/to/research-agent-data python web_ui.py
python web_ui.py --data-dir ./runtime
```

`runtime/primary/` 保存 SQLite、论文 PDF、用户画像与设置；`runtime/derived/` 保存可由原始数据重建的 Chroma 索引和 PDF 图片。旧版根目录数据不会自动移动，可先预览再迁移：

```bash
python scripts/migrate_runtime.py
python scripts/migrate_runtime.py --apply
python scripts/backup_runtime.py --output backups/
```

### Docker Compose 部署

Docker Desktop 启动后，先根据示例创建本地密钥文件，再构建并启动服务：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
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
>>> 这篇的损失函数是什么？          ← RAG 章节感知检索
>>> 描述 Figure 3                  ← GLM-4V 看图（带图注）
>>> 对比已读论文的技术路线          ← 跨论文分析
```

Web UI 顶部的会话栏可新建、切换和删除会话。删除前必须勾选确认；旧版固定的 `research-main` 历史会自动迁移为“历史会话”。CLI 中仍可用 `/new` 新建会话。

常用 Web UI 命令：

| 命令 | 说明 |
|------|------|
| `/model` `/tokens` `/profile` | 查看模型、当前会话用量和用户画像 |
| `/retry` | 重发当前进程中最后一个模型请求；流式中断后可用 |
| `/indexed` | 查看已索引论文 |
| `/note ...` | 管理按话题归档的科研笔记 |
| `/daily add <关键词>` | 添加每日检索关键词 |
| `/daily search <关键词>` | 立即执行一次不落库的三源临时检索 |
| `/daily retry` | 清除当日检索记录后重新检索已启用关键词 |
| `/daily unread` | 查看最近三天的待读清单 |

---

## 🧪 验证

```bash
python tests/test_core.py
python -m evals.benchmark
```

核心回归测试覆盖 PDF 提取、分块/RAG、图结构构建、验证重试状态清理、每日检索、多会话隔离和硬删除、统一数据目录、同模型重试、端到端截止时间、熔断、流式中断恢复，以及取消令牌从浏览器到模型/工具节点的传播。

会话隔离另有真实浏览器回归测试：它会启动本地 SSE 模拟服务，复现“旧会话流式输出中，新建会话并发送第一条命令”的竞态，确保旧历史不会重新出现。首次运行需安装 Chromium：

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest tests.test_session_e2e
```

GitHub Actions 会分别运行核心回归和浏览器会话隔离测试。

另提供 10 项版本化科研 Agent 评测任务，使用合成语料和模拟状态，不读取个人运行数据、不调用真实模型 API。任务、预期证据、工具轨迹、性能门槛和人工评分量表位于 [evals/README.md](evals/README.md)。真实运行可通过 `ResearchAgent.get_last_trace()` 自动采集脱敏链路数据，并保存带时间戳的评测报告，用于每次重构后的可复现对比与面试展示。

---

## 🧱 技术架构

```
LangGraph ReAct 循环 (graph_builder.py)
  START → LLM ⇄ Tools → Verify(分级) → END

Verify 失败时仅将反馈保存在当前重试链路；本轮完成、跳过或达到重试上限后自动清理，避免影响后续对话。

运行时数据层（APP_DATA_DIR，默认 ./runtime）:
  primary/db/          → checkpoint、notes、memory、daily SQLite 数据
  primary/papers/      → 原始论文 PDF
  primary/profile.md   → 用户画像
  primary/settings.json→ Web UI 用户设置
  derived/chroma/      → 论文向量库（可重建）
  derived/images/      → PDF 提取图片与图注（可重建）
```

---

## 📦 项目结构

```
research_agent/
├── prompts.py            # 系统提示词 + 少样本范例
├── tool_schemas.py       # 9 个工具 JSON Schema
├── graph_builder.py      # LangGraph 图定义 + verify 分级
├── pdf_reader.py         # PDF 增强提取 (表格/双栏/章节/图注图片)
├── paper_store.py        # ChromaDB RAG (章节感知切块) + NoOpStore
├── search_api.py         # arXiv / Semantic Scholar / OpenAlex API
├── scheduler.py          # 每日论文检索调度器（自动/临时/重试）
├── notes.py              # 科研笔记 (SQLite)
├── profile.py            # 用户画像 (Markdown)
├── memory.py             # 记忆模块 (三元组+摘要+图片缓存)
├── llm_client.py         # 主模型并发/重试/端到端预算/取消（不做模型降级）
├── cancellation.py       # 浏览器请求的协作式取消原语
├── resilience.py         # 通用三态熔断器（closed/open/half_open）
├── runtime_paths.py       # 运行时数据边界、版本与用户设置
├── config.py / config.yaml  # 统一配置
├── logger.py             # 结构化日志
│
├── tools/                # 工具实现
│   ├── search.py         # 搜索/查询/列表/删除
│   ├── read_pdf.py       # 下载+提取+图片+索引+摘要
│   ├── describe.py       # GLM-4V 看图 (缓存+图注)
│   └── profile_tool.py   # 画像更新
│
├── research_agent.py     # 终端 CLI 入口
├── session_store.py       # 会话目录、Token 统计与 checkpoint 联动删除
├── web_ui.py             # Web UI：受控 Chatbot 状态，避免跨会话回写
├── scripts/              # 旧数据迁移与运行时备份
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
