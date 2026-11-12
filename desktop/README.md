# Windows 桌面客户端

客户端使用 Electron 承载原有 React 三栏工作台，安装包自带 Python 后台。
桌面模式用 SQLite 持久化队列复用现有对话、研究和每日检索 worker；服务器模式继续使用 Redis。

## 使用

1. 运行 `release/Research Assistant Setup 0.2.0.exe` 安装，启动“Research Assistant”。
2. 在工作台左下角的“设置 → 通用”填写模型 API Key。密钥通过 Electron safeStorage 使用 Windows 系统加密，保存后自动重启后台。没有密钥也能整理、阅读 PDF 和写笔记。
3. “论文库”在工作台内打开。支持多文件导入、文件夹、重命名、移动、搜索、回收站和恢复。点击论文名称打开独立阅读标签。
4. 阅读器支持 25%～400% 缩放、Ctrl＋滚轮、适应宽度、整页显示、手形拖动、空格临时拖动、查找文字、划选翻译和页码批注。笔记自动保存，失败时保留本机草稿；版本冲突可另存为新笔记。
5. 每日检索、关键词和用户画像统一在设置标签页中。关闭窗口会隐藏到托盘，托盘菜单“退出”才结束后台。

原有数据不会自动搬移。首次默认使用 `%APPDATA%/research-assistant-desktop/runtime`；可在设置中选择原项目的 `runtime` 目录（包含 `primary` 的那一级）。切换只改变数据位置，不复制或删除原目录。不要同时用服务器和客户端写同一份数据。

导入的原文和笔记独立于 RAG：阅读无需等待索引。选择论文后，可在右侧点击“交给助手阅读”将索引指令填入对话，确认发送后由已有 `read_pdf` 流程解析、校验并建立索引。单纯导入文件不会自动下载模型或开始索引。

## 数据与恢复

- `primary/papers/library/`：按内容 SHA-256 保存 PDF 快照；原有顶层 PDF 自动登记，原文件保留。文件内容变化会登记新版本，旧笔记继续指向原版本。
- `primary/db/library.db`：文件夹、文件名、回收站、页码、缩放、笔记与归一化批注坐标。
- `primary/db/desktop-queue.db`：待执行任务、事件和取消记录。重启恢复尚未确认完成的任务；正在执行的任务会协作停止，异常退出可能从任务边界重试，并非从模型输出中间位置续跑。
- `credentials.bin`、`desktop.json`、`backend.log` 在客户端用户目录中，和可切换的论文数据目录分开。密钥不写入前端存储、命令行或安装包。

备份前从托盘退出客户端，再复制整个数据目录。卸载保留用户数据。

## 开发与构建

构建机需要 Windows、Node/npm，以及项目 Python 依赖。终端位于仓库根目录：

```powershell
python -m pip install -r requirements.lock.txt
python -m pip install -r desktop/requirements-build.txt
python scripts/build_desktop.py
```

可选的本地重排功能依赖 `sentence-transformers` 和 CPU 版 PyTorch；构建完整发行版时使用项目已经验证过的完整 Python 环境。构建脚本会收集这些包的元数据；不从项目 `.env` 或 `runtime` 打包凭据和用户资料。

产物位于 `desktop/release/`。`win-unpacked/Research Assistant.exe` 可直接启动，NSIS 安装程序用于分发。开发时先在 `frontend` 执行 `npm ci`、`npm run build`，再在 `desktop` 执行 `npm ci`、`npm start`。`DESKTOP_PYTHON` 可指定开发环境解释器。

```powershell
python -m unittest tests.test_desktop_library tests.test_ci_test_coverage
cd frontend
npm run build
npm test
```

`scripts/check_desktop_ui.py --url http://127.0.0.1:端口` 使用真实 PDF/API 验证阅读器。仅对隔离测试数据运行，并用 `DESKTOP_TEST_KEY` 提供测试令牌。

安装 Python 版 Playwright 后，`node scripts/check_desktop_app.cjs` 会启动 `win-unpacked` 的真实客户端，在隔离用户目录中验证首启、PDF、笔记、系统凭据加密、重启恢复、划词翻译、对话队列、托盘关闭和后台退出。测试使用本地模拟模型，并从被测客户端的 PATH 移除 Python/Node；不会调用付费模型或修改用户数据。加 `--dev` 可验证开发入口。

## 当前边界

- 本次提供 Windows x64 安装包，尚未制作 macOS/Linux 发行包，也未设置自动更新。
- 安装包尚未代码签名，Windows 可能显示发布者未知；对外发布前需配置正式签名证书。
- 翻译、对话及联网检索需要网络和有效服务密钥；本地 PDF 与笔记不需要。
- 扫描 PDF 可以阅读和写整页笔记；当前阅读器不自动 OCR，无法对没有文字层的页面划词。嵌入/重排模型首次使用仍可能需要下载。
- 回收站暂不提供永久删除；PDF 批注保存在应用中，暂不回写原 PDF。
