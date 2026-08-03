

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
| 🧠 **RAG 检索** | 章节感知分层切块 + 章节加权 + 章节过滤 + 公式保护 |
| 🖼 **多模态看图** | GLM-4V 描述 + 图注附带 + 描述缓存（省 token） |
| 📰 **每日速递** | 自定义关键词，三源每日自动/临时检索，支持重试和待读清单管理 |
| 📝 **科研笔记** | SQLite 按话题分组，关联论文 |
| 👤 **用户画像** | Markdown 自动维护，Agent 从对话中学习偏好 |
| ✅ **自动验证** | verify 分级（严重重生成/轻微提示）+ 跳过门控 + 增量验证 |
| 📊 **Token 管理** | 按工具差异化截断 + 缓存命中统计 + 预算预警 + 费用估算 |

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

### 3. 启动（Web UI）

```bash
python web_ui.py                # 浏览器打开 http://localhost:7860
python web_ui.py -m deepseek-chat --port 8080
```

终端版：`python research_agent.py`

不传 `-m` 时，Web UI 和 CLI 都遵循统一配置优先级：命令行 > 环境变量 > `config.yaml` > 默认值。

---

## 💬 使用示例

```
>>> 搜索 diffusion model 在网络调度中的最新论文
>>> 精读第1篇                      ← 自动下载+索引+图片提取+摘要卡片
>>> 这篇的损失函数是什么？          ← RAG 章节感知检索
>>> 描述 Figure 3                  ← GLM-4V 看图（带图注）
>>> 对比已读论文的技术路线          ← 跨论文分析
```

常用 Web UI 命令：

| 命令 | 说明 |
|------|------|
| `/new` `/model` `/tokens` `/profile` | 会话、模型和用量管理 |
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
```

当前有 13 个核心回归测试，覆盖 PDF 提取、分块/RAG、图结构构建、验证重试状态清理、每日检索重试与临时检索。CI 在 GitHub Actions 中自动运行该命令。

---

## 🧱 技术架构

```
LangGraph ReAct 循环 (graph_builder.py)
  START → LLM ⇄ Tools → Verify(分级) → END

Verify 失败时仅将反馈保存在当前重试链路；本轮完成、跳过或达到重试上限后自动清理，避免影响后续对话。

存储层:
  checkpoint.db  → 对话历史 (SQLite, 自动剪裁)
  chroma_data/   → 论文向量库 (ChromaDB ONNX, 章节感知 chunk)
  notes.db       → 科研笔记 (SQLite)
  memory.db      → 三元组 + 对话摘要 + 图片描述缓存
  daily.db       → 每日检索记录 (SQLite)
  profile.md     → 用户画像 (Markdown, 人+Agent 共维护)
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
├── web_ui.py             # Web UI (Gradio 4 Tab)
├── tests/test_core.py    # 13 个核心回归测试
├── .github/workflows/    # CI 自动测试
│
├── 需求决策日志.md         # 功能需求与决策记录 (条目 001-025)
├── 重构说明.md            # 代码变动文档
└── requirements.txt
```

---

## 📄 License

MIT
