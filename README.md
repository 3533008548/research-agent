# Research Assistant · 科研助手

一个支持边读论文边提问的科研工作台。导入 PDF 后，可以划选原文、记录页码批注，也可以让助手为论文建立索引，追问某个结论出自哪一页。跨论文研究支持限定资料来源，报告中的证据编号可以回溯到引用材料。

## 开始使用

**Windows 桌面端**适合在个人电脑上管理论文。下载安装程序，打开「Research Assistant」，在「设置 → 通用」填写模型 API Key。阅读 PDF 和写批注可以直接使用。桌面端自带 Python 后端，任务保存在本机 SQLite 队列中。[下载 Windows 安装包](https://github.com/3533008548/research-agent/releases/latest)

**Docker Compose** 适合在浏览器中使用完整工作台。从仓库根目录执行以下命令（PowerShell）。先复制配置模板，在生成的 `.env` 中填写 `DEEPSEEK_API_KEY`，再启动服务。

```powershell
Copy-Item .env.example .env
# 填写 .env 后执行
docker compose up -d --build
docker compose ps
```

打开 [本机工作台](http://localhost:7860/)。Compose 会启动 FastAPI、Redis 和消费任务的 Worker。页面可打开但任务一直排队时，运行 `docker compose logs --tail=100 api-worker redis` 查看后台状态。接口列表与请求示例可在运行中的 [API 文档](http://localhost:7860/docs) 查看。

## 从一篇论文开始

1. **阅读并做笔记。** 在论文库导入 PDF，打开阅读标签。选中原文可翻译、添加批注或附到当前对话。把对话标签拖到右栏，即可对照原页阅读回复。
2. **查询论文证据。** 点击「交给助手阅读」并发送，触发解析与索引。可以问「表 2 的结果由哪段正文解释？请给出页码」，也可以指定两篇论文比较。检索会结合语义匹配、BM25 和可选的本地重排。
3. **研究一个问题。** 在多 Agent 研究模式中选择本地论文、公开文献或两者。工作台显示取证、综合和质检进度，可查看证据、暂停任务或继续研究。
4. **跟踪新论文。** 在设置中维护每日检索关键词。每日任务从 OpenAlex、OpenAIRE 和 DBLP 收集候选论文，后台保存运行进度。

## 配置与数据

| 设置 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 模型服务密钥。桌面端可在设置中保存，Compose 从 `.env` 读取 |
| `UI_PORT` | Compose 在宿主机监听的端口，默认 `7860` |
| `API_AUTH_REQUIRED`、`API_AUTH_TOKEN` | 对外提供 API 时启用鉴权，并为客户端配置令牌 |
| `APP_DATA_DIR` | 源码运行时的数据根目录，默认 `./runtime`。Compose 将宿主机的 `./runtime` 挂载到容器 |
| `config.yaml` | 模型、RAG、每日检索和外部 MCP 服务配置。[查看默认配置](config.yaml) |

`runtime/primary/` 保存原始 PDF、批注数据库、会话和设置。`runtime/derived/` 包含索引、图片和模型缓存，其中的 `redis/` 还保存队列持久化文件。迁移完整工作区时，先停止使用这份数据的服务，再复制整个 `runtime/`。Windows 桌面端默认使用 `%APPDATA%/research-assistant-desktop/runtime`，也可在设置中选择现有数据目录。同一份数据应由一个部署写入。

## 源码开发与接入

后端 CI 使用 Python 3.11，前端使用 Node.js 24。以下命令均从仓库根目录执行。安装 Python 依赖后，可直接启动终端对话。

```powershell
python -m pip install -r requirements.lock.txt
python research_agent.py
```

开发浏览器界面时，准备一个本地 Redis 实例，在两个终端分别设置 `REDIS_URL`（例如 `redis://127.0.0.1:6379/0`），然后分别执行 `python api_worker.py` 和 `python -m uvicorn api_server:app --port 7860`。第三个终端运行下面的命令，打开 [前端开发服务](http://localhost:5173/)。Vite 会将 API 请求转发到后端。

```powershell
npm --prefix frontend ci
npm --prefix frontend run dev
```

**MCP 接入。** 本项目可以作为 stdio 服务提供公开文献搜索、论文库查询与索引列表。在 MCP 客户端中将 `command` 设为项目 Python 解释器的绝对路径，`args` 设为根目录 `mcp_server.py` 的绝对路径数组，`APP_DATA_DIR` 指向目标论文库。要让科研助手调用外部服务，在 `config.yaml` 的 `mcp` 下启用服务并配置 `servers` 和允许的工具。[MCP 服务实现](integrations/mcp_server.py) · [MCP 客户端](integrations/mcp_client.py)

### 去哪里改代码

| 想调整的行为 | 实现位置 |
| --- | --- |
| 解析 PDF、匹配指定论文、调整检索排序 | [阅读器](papers/reader.py) · [论文范围](papers/scope.py) · [混合检索](papers/hybrid.py) |
| 改变模型可见的工具、执行权限或结果 | [工具目录](tools/catalog.py) · [执行边界](tools/runtime.py) · [分发](tools/dispatch.py) |
| 修改普通对话、深度研究或每日任务恢复 | [对话图](agent/graph.py) · [深度研究](agent/research/) · [每日任务恢复](agent/daily/startup.py) |
| 修改会话、运行记录、数据目录或模型调用 | [会话与记忆](sessions/) · [运行契约](runs/contract.py) · [数据路径](infra/runtime_paths.py) · [外部服务](integrations/) |
| 修改 HTTP 接口、阅读界面或桌面启动 | [API](api/) · [React 前端](frontend/src/) · [Electron 主进程](desktop/main.cjs) |

Python 调用方可以从 `agent.service` 导入 `ResearchAgent`，从 `papers.store` 导入 `PaperStore`。原有的 `paper_store.PaperStore` 导入及 `python pdf_reader.py <PDF>` 命令继续可用。

### 测试与桌面构建

```powershell
python -m unittest tests.test_paper_scope tests.test_run_dispatcher
python -m evals.release_gate --strict
npm --prefix frontend ci
npm --prefix frontend test
node --test desktop/tests/updater.test.cjs
```

严格发布门禁使用离线夹具和模拟状态。完整单测、浏览器回归及容器检查使用的依赖和命令见 [CI 配置](.github/workflows/test.yml)。

在 Windows 上运行以下命令构建桌面安装包。产物位于 `desktop/release/`。校验命令会检查版本、SHA-512 和更新清单。发布脚本默认创建 GitHub 草稿，可读取本机 `desktop/releases/<版本>.md`，缺省时使用 GitHub 生成的发行说明。

```powershell
python -m pip install -r desktop/requirements-build.txt
python scripts/build_desktop.py
node scripts/publish_desktop_release.cjs --verify-only
```

[MIT 许可证](LICENSE)
