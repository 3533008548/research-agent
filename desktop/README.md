# Windows 桌面客户端

客户端使用 Electron 承载原有 React 三栏工作台，安装包自带 Python 后台。
桌面模式用 SQLite 持久化队列复用现有对话、研究和每日检索 worker；服务器模式继续使用 Redis。

## 使用

1. 运行 `release/Research-Assistant-Setup-0.2.5.exe` 安装，启动“Research Assistant”。
2. 在工作台左下角的“设置 → 通用”填写模型 API Key。密钥通过 Electron safeStorage 使用 Windows 系统加密，保存后自动重启后台。没有密钥也能整理、阅读 PDF 和写笔记。
3. “论文库”在工作台内打开。支持多文件导入、文件夹、重命名、移动、搜索、回收站和恢复。点击论文名称打开独立阅读标签。
4. 阅读器支持 25%～400% 缩放、Ctrl＋滚轮、适应宽度、整页显示、手形拖动、空格临时拖动、查找文字、划选翻译和页码批注。笔记自动保存，失败时保留本机草稿；版本冲突可另存为新笔记。
5. 每日检索、关键词和用户画像统一在设置标签页中。关闭窗口会隐藏到托盘，托盘菜单“退出”才结束后台。

工作台顶部的对话标签可直接拖到右栏，与中央论文并排显示；拖回顶部恢复中央对话。右栏可拉宽至 800px，并根据窗口宽度保留阅读区域。停靠位置、宽度和显隐会在刷新或重启后恢复。阅读辅助和运行信息仍可通过右栏页签查看，笔记和翻译入口会自动切换到对应工具。

原有数据不会自动搬移。首次默认使用 `%APPDATA%/research-assistant-desktop/runtime`；可在设置中选择原项目的 `runtime` 目录（包含 `primary` 的那一级）。切换只改变数据位置，不复制或删除原目录。不要同时用服务器和客户端写同一份数据。

导入的原文和笔记独立于 RAG：阅读无需等待索引。选择论文后，可在右侧点击“交给助手阅读”将索引指令填入对话，确认发送后由已有 `read_pdf` 流程解析、校验并建立索引。单纯导入文件不会自动下载模型或开始索引。

0.2.1 起，桌面端也支持在对话中提供本机 PDF 绝对路径（含带引号路径和 `file:///` URI），后台先复制到论文库再读取，原文件保持不变。网页服务仍需先上传。`list_papers` 包含论文库导入的文件，并返回可直接使用的相对路径。

安装包内置 Tesseract 英文、简体中文 OCR 语言数据，解析图片/矢量算法区域无需另装 Tesseract。默认识别英文；源码运行可用 `PDF_OCR_LANGUAGE=eng+chi_sim` 处理中文扫描件。构建时按固定版本和 SHA-256 校验下载，随包附带 Apache-2.0 许可。源码运行先执行 `python scripts/fetch_ocr_data.py`；OCR 转写保留页码和原图，公式符号仍需核对。

0.2.5 起，指定论文询问算法编号或伪代码时，会从原 PDF 获取完整算法区域，兼容仅含标题的旧索引，不按检索条数截掉算法末尾。自动附上原页裁图供逐行转写；OCR 或区域识别失败时提供整页备用图，并明确报告尚未恢复的内容。

## 数据与恢复

- `primary/papers/library/`：按内容 SHA-256 保存 PDF 快照；原有顶层 PDF 自动登记，原文件保留。文件内容变化会登记新版本，旧笔记继续指向原版本。
- `primary/db/library.db`：文件夹、文件名、回收站、页码、缩放、笔记与归一化批注坐标。
- `primary/db/desktop-queue.db`：待执行任务、事件和取消记录。重启恢复尚未确认完成的任务；正在执行的任务会协作停止，异常退出可能从任务边界重试，并非从模型输出中间位置续跑。
- `credentials.bin`、`desktop.json`、`backend.log` 在客户端用户目录中，和可切换的论文数据目录分开。密钥不写入前端存储、命令行或安装包。
- `renderer.log` 记录捕获的界面异常（错误与调用栈），仅保存在本机，不自动上传。阅读器加载失败时会显示恢复入口，不再让未处理的阅读器异常清空整个工作台。

备份前从托盘退出客户端，再复制整个数据目录。卸载保留用户数据。

## 应用内更新

0.2.2 起，在“设置 → 通用 → 应用更新”检查版本、查看说明、下载更新，完成当前工作后点击“重启并安装”。客户端也会在启动后检查一次版本，但不会自动下载或强制安装。下载失败可重试；运行中和排队中的任务会阻止安装。安装更新保留用户数据和系统加密的凭据。

0.2.1 及更早版本没有更新模块，需要手动安装 0.2.2 一次。更新包由客户端下载并校验，仍需联网和下载流量；无需再打开浏览器寻找安装包。当前更新源为本仓库 GitHub Releases，只有正式发布并包含 `latest.yml` 的版本才可见；单独推送 Git 提交不会触发客户端升级。未发布版本或网络不可达时，客户端显示原因并允许重试。

开发者每次发布仍需构建新版本，可按以下流程操作：

1. 在 `desktop` 执行 `npm version patch --no-git-tag-version`，添加 `desktop/releases/<版本>.md` 更新说明。
2. 从仓库根目录执行 `python scripts/build_desktop.py`，完成测试后提交并推送相应代码。
3. 执行 `node scripts/publish_desktop_release.cjs --verify-only` 验证产物。
4. 执行 `node scripts/publish_desktop_release.cjs --publish`，使用已登录的 GitHub CLI 将安装包、`.blockmap` 和 `latest.yml` 上传为草稿，上传全部成功后再正式发布。不加 `--publish` 则保持草稿，供人工核对。

构建默认 `--publish never`，不会意外发布。若上传中断，已有草稿不会影响更新客户端；补齐三个产物并核对后再发布。保留旧版 Release 及 `.blockmap` 可支持差分下载，无法差分时自动回退完整包；首个带更新能力的版本通常需要完整下载。

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

`node scripts/check_desktop_ingestion.cjs [PDF路径] [算法编号]` 专门验证打包后的入库链路：外部绝对路径导入、内置 OCR、真实 Chroma 写入、重复索引替换和算法检索。默认生成含图片算法的测试 PDF，算法编号默认 4。测试使用独立数据目录、本地模拟模型，并故意设置不存在的系统 OCR 目录，确保客户端确实使用随包资源。

`node scripts/check_desktop_pdf.cjs` 验证仅有标题的旧索引能自动恢复无冒号标题的矢量算法，返回最后一行并将原页裁图交给专用转写任务；原索引和论文 ID 保持不变。使用真实本地 OCR 和模拟视觉模型，`--dev` 验证开发入口。

`node --test desktop/tests/*.test.cjs` 验证更新状态、重复点击、失败重试和安装时机。`node scripts/check_desktop_update.cjs` 使用隔离客户端与本地更新服务器验证实际下载、校验、设置界面和安装前后台退出；截获安装器启动，不覆盖机器上的真实安装。

## 当前边界

- 本次提供 Windows x64 安装包及应用内更新，尚未制作 macOS/Linux 发行包。
- 安装包尚未代码签名，Windows 可能显示发布者未知；对外发布前需配置正式签名证书。
- 翻译、对话及联网检索需要网络和有效服务密钥；本地 PDF 与笔记不需要。
- 扫描 PDF 可以阅读和写整页笔记；当前阅读器不自动 OCR，无法对没有文字层的页面划词。嵌入/重排模型首次使用仍可能需要下载。
- 回收站暂不提供永久删除；PDF 批注保存在应用中，暂不回写原 PDF。
