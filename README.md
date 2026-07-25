

<div align="center">

# 🔬 Research Assistant — 科研助手 Agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**搜索论文 · 阅读 PDF · RAG 检索 · 多模态看图 · 方向分析 · 科研笔记**

</div>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 🔍 **论文搜索** | Semantic Scholar + arXiv 双数据源 |
| 📄 **PDF 阅读** | pdfplumber 表格感知 + 双栏排序 + 章节标注 + PyMuPDF 图片提取 |
| 🧠 **RAG 检索** | ChromaDB ONNX 向量库，全文索引，语义精准定位 |
| 🖼 **多模态看图** | GLM-4V API 自动描述架构图、流程图、实验图 |
| 📝 **科研笔记** | SQLite 按研究方向分话题，支持长文本，关联论文 |
| 👤 **用户画像** | Markdown 自动维护，Agent 从对话中学习偏好 |
| ✅ **自动验证** | LangGraph verify 节点 + 反馈重试，检查公式来源、标记 |

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

---

## 💬 使用示例

```
>>> 搜索 diffusion model 在网络调度中的最新论文
>>> 精读第1篇                      ← 自动下载+索引+图片提取+摘要卡片
>>> 这篇的损失函数是什么？          ← RAG 精准检索
>>> 描述 Figure 3                  ← GLM-4V 看图
>>> 对比已读论文的技术路线          ← 跨论文分析
```

Web UI 命令：`/help` `/model` `/tokens` `/profile` `/note` `/new`

---

## 🧱 技术架构

```
LangGraph ReAct 循环 (graph_builder.py)
  START → LLM ⇄ Tools → Verify → END
  
存储层:
  checkpoint.db  → 对话历史 (SQLite, 自动剪裁)
  chroma_data/   → 论文向量库 (ChromaDB ONNX)
  notes.db       → 科研笔记 (SQLite)
  profile.md     → 用户画像 (Markdown, 人+Agent 共维护)
```

---

## 📦 项目结构

```
research_agent/
├── prompts.py            # 系统提示词 + 少样本范例
├── tool_schemas.py       # 8 个工具 JSON Schema
├── graph_builder.py      # LangGraph 图定义 (280 行)
├── pdf_reader.py         # PDF 增强提取 (表格+双栏+章节+图片)
├── paper_store.py        # ChromaDB RAG 向量库 + NoOpStore 降级
├── search_api.py         # arXiv / Semantic Scholar API
├── notes.py              # 科研笔记 (SQLite)
├── profile.py            # 用户画像 (Markdown)
├── config.py / config.yaml  # 统一配置 (.env + YAML + CLI)
├── logger.py             # 结构化日志
│
├── tools/                # 工具实现 (按领域拆)
│   ├── search.py         # 搜索/查询/列表/删除
│   ├── read_pdf.py       # 下载+提取+图片+索引+摘要
│   ├── describe.py       # GLM-4V 看图
│   └── profile_tool.py   # 画像更新
│
├── research_agent.py     # 终端 CLI 入口
├── web_ui.py             # Web UI (Gradio 4 Tab)
├── tests/test_core.py    # 11 个核心测试
│
├── 需求决策日志.md         # 功能需求与决策记录 (条目 001-016)
├── 重构说明.md            # 代码变动文档
└── requirements.txt
```

---

## 📄 License

MIT
