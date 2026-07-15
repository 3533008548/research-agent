

<div align="center">

# 🔬 Research Assistant — 科研助手 Agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**搜索论文 · 阅读 PDF · 分析方向 · 科研建议**

</div>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 🔍 **论文搜索** | 支持 Semantic Scholar（含引用数/PDF链接）和 arXiv 双数据源 |
| 📄 **PDF 阅读** | 自动下载论文 PDF，用 PyMuPDF 提取文本，本地缓存避免重复下载 |
| 🧠 **智能分析** | 基于 DeepSeek API + Function Calling，多轮交互深度解读论文 |
| 📚 **本地管理** | 下载的 PDF 自动缓存至 `data/papers/`，支持查看已下载列表 |

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

创建 `.env` 文件（已 gitignore），填入你的 DeepSeek API Key：

```env
DEEPSEEK_API_KEY=sk-your******ere
```

> 也支持从 `DEEPSEEK_API_KEY` 环境变量读取。

### 3. 启动

```bash
python research_agent.py
```

## 💬 使用示例

```
>>> 帮我搜索强化学习在网络调度中的最新论文
>>> 精读第2篇
>>> 总结这篇论文的核心方法和技术路线
>>> 对比这三篇论文，分析未来的研究方向
```

支持命令：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/new` | 开始新对话 |
| `/papers` | 列出已下载论文 |
| `/model` | 查看当前模型 |
| `/quit` | 退出 |

## 🧱 技术架构

```
research_agent.py  (单文件)
│
├── ResearchAgent 类          ← 基于 DeepSeek Function Calling
│   ├── tool 工具注册机制
│   ├── 自动 tool_calls 推理循环
│   └── 多轮对话记忆
│
├── 工具函数
│   ├── search_papers()       ← Semantic Scholar + arXiv
│   ├── read_pdf()            ← PyMuPDF 提取 + 本地缓存
│   └── list_papers()         ← 查看缓存
│
└── CLI 交互入口
```

## 📦 项目结构

```
research_agent/
├── research_agent.py    # 主程序
├── requirements.txt     # 依赖清单
├── .gitignore           # 忽略 .env 和 PDF 缓存
├── 需求决策日志.md       # 功能需求与决策记录
└── data/
    └── papers/          # PDF 缓存目录（gitignore）
```

## 🧭 后续计划

- [ ] **持久化论文库** — SQLite 存储论文元数据、阅读笔记
- [ ] **RAG 检索增强** — 论文向量化，精准定位关键段落
- [ ] **PDF 解析增强** — 表格提取、双栏布局感知、公式保留
- [ ] **批量调研报告** — 一键生成文献综述 / PPT 大纲

## 📄 License

MIT
