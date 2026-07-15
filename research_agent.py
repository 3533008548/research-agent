"""
╔══════════════════════════════════════════════════════════════════╗
║               🔬 科研助手 Agent  — Research Assistant           ║
║                                                                  ║
║  基于 DeepSeek API + Function Calling 的学术研究助手             ║
║  功能：论文搜索 → PDF阅读 → 方向分析 → 科研建议                 ║
║                                                                  ║
║  用法：python research_agent.py                                  ║
║  依赖：pip install requests PyMuPDF python-dotenv                ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

# ── HTTP 请求 ──
import requests

# ── 环境变量 ──
try:
    from dotenv import load_dotenv
    # 尝试多个可能的 .env 位置
    for env_path in [".env", "../cli-chatbot/.env", str(Path.home() / ".env")]:
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
except ImportError:
    pass

# ═══════════════════════════════════════════════════════════════
#  工具函数 — 论文搜索 & PDF 阅读
# ═══════════════════════════════════════════════════════════════

# ── arXiv API 命名空间 ──
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


def search_arxiv(query: str, max_results: int = 5) -> str:
    """通过 arXiv API 搜索论文（免费，无需 API Key）"""
    safe_q = urllib.parse.quote(query)
    url = f"http://export.arxiv.org/api/query?search_query=all:{safe_q}&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"❌ arXiv API 请求失败: {e}"

    root = ET.fromstring(resp.content)
    entries = root.findall("atom:entry", ARXIV_NS)

    if not entries:
        return "📭 arXiv 未找到相关论文。"

    lines = [f"📚 **arXiv 搜索结果** — 查询: 「{query}」\n"]
    for i, entry in enumerate(entries, 1):
        title = entry.find("atom:title", ARXIV_NS)
        title = title.text.strip().replace("\n", " ") if title is not None else "N/A"

        summary = entry.find("atom:summary", ARXIV_NS)
        summary = summary.text.strip().replace("\n", " ") if summary is not None else ""

        authors = [a.find("atom:name", ARXIV_NS).text for a in entry.findall("atom:author", ARXIV_NS) if a.find("atom:name", ARXIV_NS) is not None]

        link = entry.find("atom:id", ARXIV_NS)
        link = link.text.strip() if link is not None else ""

        published = entry.find("atom:published", ARXIV_NS)
        published = published.text[:10] if published is not None else ""

        # arXiv ID
        arxiv_id = link.split("/")[-1] if link else ""

        # 截取摘要前 250 字符
        summary_short = summary[:250] + "..." if len(summary) > 250 else summary

        lines.append(f"\n  {i}. **{title}**")
        lines.append(f"     作者: {', '.join(authors[:5])}{' et al.' if len(authors) > 5 else ''}")
        lines.append(f"     日期: {published}  |  arXiv: {arxiv_id}")
        lines.append(f"     链接: {link}")
        lines.append(f"     摘要: {summary_short}")

    return "\n".join(lines)


def search_semantic_scholar(query: str, limit: int = 5) -> str:
    """通过 Semantic Scholar API 搜索论文（免费，含引用数、PDF链接）"""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(limit, 10),
        "fields": "title,authors,year,abstract,citationCount,externalIds,openAccessPdf,url,venue",
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"❌ Semantic Scholar API 请求失败: {e}"

    papers = data.get("data", [])
    if not papers:
        return "📭 Semantic Scholar 未找到相关论文。"

    lines = [f"📚 **Semantic Scholar 搜索结果** — 查询: 「{query}」\n"]
    for i, p in enumerate(papers, 1):
        title = p.get("title", "N/A")
        authors = [a.get("name", "") for a in p.get("authors", [])[:5]]
        year = p.get("year", "N/A")
        citations = p.get("citationCount", 0)
        venue = p.get("venue", "") or ""
        abstract = p.get("abstract") or "无摘要"
        abstract_short = abstract[:200] + "..." if len(abstract) > 200 else abstract

        pdf_info = p.get("openAccessPdf") or {}
        pdf_url = pdf_info.get("url", "") if pdf_info else ""
        url_link = p.get("url", "")

        lines.append(f"\n  {i}. **{title}**")
        lines.append(f"     作者: {', '.join(authors)}{' et al.' if len(authors) > 5 else ''}")
        lines.append(f"     年份: {year}  |  引用: {citations}  |  期刊: {venue if venue else 'N/A'}")
        if pdf_url:
            lines.append(f"     📄 PDF: {pdf_url}")
        if url_link:
            lines.append(f"     🔗 链接: {url_link}")
        lines.append(f"     摘要: {abstract_short}")

    return "\n".join(lines)


def read_pdf(url_or_path: str, max_pages: int = 15) -> str:
    """下载 PDF（如果是 URL）并提取文本内容"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return (
            "❌ 需要安装 PyMuPDF 才能解析 PDF：\n"
            "   pip install PyMuPDF"
        )

    # 确保缓存目录存在
    pdf_dir = Path("data/papers")
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # 判断是 URL 还是本地路径
    if url_or_path.startswith(("http://", "https://")):
        # 从 URL 提取文件名
        filename = url_or_path.split("/")[-1].split("?")[0]
        if not filename.endswith(".pdf"):
            # 尝试从 arXiv 链接推断 PDF 地址
            if "arxiv.org/abs/" in url_or_path:
                arxiv_id = url_or_path.split("/abs/")[-1].split("v")[0]
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                filename = f"{arxiv_id}.pdf"
                url_or_path = pdf_url
            elif "arxiv.org/pdf/" in url_or_path:
                filename = url_or_path.split("/pdf/")[-1].split("?")[0]
                if not filename.endswith(".pdf"):
                    filename += ".pdf"
            else:
                filename = f"paper_{hash(url_or_path) & 0xFFFFFFFF:08x}.pdf"

        pdf_path = pdf_dir / filename

        # 下载（若缓存不存在）
        if not pdf_path.exists():
            print(f"      📥 正在下载 PDF...", file=sys.stderr)
            try:
                resp = requests.get(url_or_path, timeout=60, headers={
                    "User-Agent": "Mozilla/5.0 (ResearchAssistant/1.0)"
                })
                resp.raise_for_status()
                pdf_path.write_bytes(resp.content)
                size_kb = len(resp.content) / 1024
                print(f"      ✅ 下载完成 ({size_kb:.0f} KB)", file=sys.stderr)
            except requests.RequestException as e:
                return f"❌ PDF 下载失败: {e}"
        else:
            print(f"      📂 使用缓存: {pdf_path.name}", file=sys.stderr)
    else:
        # 本地路径
        pdf_path = Path(url_or_path)
        if not pdf_path.exists():
            return f"❌ 本地文件不存在: {url_or_path}"

    # ── 提取文本 ──
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        return f"❌ PDF 解析失败: {e}"

    total_pages = len(doc)
    pages_to_read = min(total_pages, max_pages)

    text_parts = []
    for i in range(pages_to_read):
        page = doc[i]
        text = page.get_text().strip()
        if text:
            text_parts.append(f"━━━ 第 {i+1} 页 ━━━\n{text}")

    doc.close()

    full_text = "\n".join(text_parts)
    char_count = len(full_text)

    if total_pages > max_pages:
        full_text += f"\n\n...（共 {total_pages} 页，已读取前 {max_pages} 页）"

    # 如果文本太长，截取前 8000 字符
    if char_count > 8000:
        full_text = full_text[:8000] + "\n\n...（内容过长，已截断至前 8000 字符）"
        char_count = 8000

    return (
        f"📄 **PDF 解析完成**\n"
        f"   文件名: {pdf_path.name}\n"
        f"   总页数: {total_pages} | 已读: {pages_to_read} | 提取: {char_count} 字符\n\n"
        f"{full_text}"
    )


def list_downloaded_papers() -> str:
    """列出本地已下载的论文 PDF"""
    pdf_dir = Path("data/papers")
    if not pdf_dir.exists():
        return "📂 尚未下载任何论文。"

    pdfs = list(pdf_dir.glob("*.pdf"))
    if not pdfs:
        return "📂 data/papers/ 目录中没有 PDF 文件。"

    total_size = sum(f.stat().st_size for f in pdfs)
    lines = [f"📂 本地已下载论文（共 {len(pdfs)} 篇，{total_size / 1024 / 1024:.1f} MB）\n"]
    for i, f in enumerate(pdfs, 1):
        size_kb = f.stat().st_size / 1024
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {i}. {f.name}  ({size_kb:.0f} KB, {mtime})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Agent 核心 — 基于 DeepSeek Function Calling
# ═══════════════════════════════════════════════════════════════

class ResearchAgent:
    """科研助手 Agent — 自动搜索论文、阅读 PDF、分析研究方向"""

    def __init__(self, api_key: Optional[str] = None, model: str = "deepseek-chat"):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError(
                "❌ 未找到 DEEPSEEK_API_KEY！\n"
                "   请在 .env 文件中设置 DEEPSEEK_API_KEY=your_key_here\n"
                "   或通过环境变量 DEEPSEEK_API_KEY 传入"
            )

        self.api_url = "https://api.deepseek.com/chat/completions"
        self.model = model
        self._messages: list[dict] = []
        self._tools: list[dict] = []
        self._tool_functions: dict[str, callable] = {}

        # 注册内置工具
        self._register_builtin_tools()
        # 设置系统提示词
        self._init_system_prompt()

    # ── 工具注册 ──

    def _register_builtin_tools(self):
        """注册所有科研工具"""

        tools_spec = [
            {
                "name": "search_papers",
                "description": "搜索学术论文。支持两个数据源：arxiv（免费、全面）和 semantic_scholar（含引用数、PDF链接）。默认使用 semantic_scholar",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词，如 'reinforcement learning TSN scheduling'"
                        },
                        "source": {
                            "type": "string",
                            "enum": ["semantic_scholar", "arxiv"],
                            "description": "数据源：semantic_scholar（推荐，引用数+PDF链接）或 arxiv"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回结果数量（1-10）",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "read_pdf",
                "description": "下载论文PDF并提取文本内容。支持 arXiv 链接（https://arxiv.org/abs/... 或 https://arxiv.org/pdf/...）和其他 PDF 直链，也支持本地文件路径。会自动缓存已下载的PDF",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url_or_path": {
                            "type": "string",
                            "description": "PDF的URL或本地文件路径"
                        },
                        "max_pages": {
                            "type": "integer",
                            "description": "最多读取的页数（默认15，越大内容越全但消耗更多token）",
                            "default": 15
                        }
                    },
                    "required": ["url_or_path"]
                }
            },
            {
                "name": "list_papers",
                "description": "列出本地已下载缓存的论文PDF文件",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            },
        ]

        # 工具名 → 函数映射
        func_map = {
            "search_papers": self._search_papers_wrapper,
            "read_pdf": read_pdf,
            "list_papers": lambda **_: list_downloaded_papers(),
        }

        for spec in tools_spec:
            self._tools.append({
                "type": "function",
                "function": spec,
            })
            self._tool_functions[spec["name"]] = func_map[spec["name"]]

    def _search_papers_wrapper(self, query: str, source: str = "semantic_scholar", limit: int = 5) -> str:
        """搜索论文的统一包装"""
        if source == "arxiv":
            result = search_arxiv(query, max_results=limit)
        else:
            result = search_semantic_scholar(query, limit=limit)
        return result

    def _init_system_prompt(self):
        """设置系统提示词——定义Agent的科研助手身份和行为规范"""
        self._messages = [
            {
                "role": "system",
                "content": (
                    "你是一位顶级的科研助手（Research Assistant AI），专精于帮助研究人员进行文献调研和方向分析。\n\n"
                    "## 核心能力\n"
                    "1. **论文搜索** — 用 search_papers 工具搜索相关论文\n"
                    "2. **PDF阅读** — 用 read_pdf 工具下载并阅读论文全文\n"
                    "3. **研究方向分析** — 综合多篇论文，总结技术趋势、对比方法优劣、提出见解\n"
                    "4. **科研建议** — 基于文献调研给出研究选题、实验设计等建议\n\n"
                    "## 工作方法\n"
                    "- **搜索阶段**：先用 search_papers 搜索，返回结果后询问用户想深入看哪篇\n"
                    "- **精读阶段**：用户选定后，用 read_pdf 下载PDF并提取内容，然后基于内容做详细解读\n"
                    "- **分析阶段**：综合多篇论文，做技术对比、趋势分析、优劣势评估\n\n"
                    "## 行为准则\n"
                    "- 用**中文**回答用户问题，但论文标题、专有名词、术语保持英文\n"
                    "- 搜索结果用结构化格式展示（编号、标题、作者、年份、引用数等）\n"
                    "- 解读论文时覆盖：解决了什么问题、方法核心思想、实验设置与结果、局限性\n"
                    "- 分析研究方向时给出清晰的对比表和渐进式研究路线\n"
                    "- 每次回复末尾可以主动建议下一步动作（如「是否要精读第X篇？」）\n"
                    "- 如果用户需求模糊，主动追问澄清具体方向\n\n"
                    "## 可用工具\n"
                    "- `search_papers(query, source, limit)` — 搜索论文\n"
                    "- `read_pdf(url_or_path, max_pages)` — 下载并阅读PDF\n"
                    "- `list_papers()` — 查看已下载的论文\n\n"
                    "开始吧！请用户告诉你想研究什么方向。"
                )
            }
        ]

    # ── API 调用 ──

    def _call_api(self, messages: list[dict]) -> dict:
        """调用 DeepSeek API（非流式）"""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": self._tools,
            "tool_choice": "auto",
            "stream": False,
            "temperature": 0.7,
        }

        resp = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )

        if resp.status_code == 429:
            return {"error": "⚠️ API 限流（429），请稍后再试"}
        elif resp.status_code == 401:
            return {"error": "❌ API Key 无效，请检查 DEEPSEEK_API_KEY"}

        resp.raise_for_status()
        return resp.json()

    # ── 推理循环 ──

    def step(self, user_input: str) -> str:
        """一次完整的推理 + 工具调用循环"""
        self._messages.append({"role": "user", "content": user_input})

        max_turns = 10
        for turn in range(max_turns):
            response = self._call_api(self._messages)

            # 错误处理
            if "error" in response:
                return response["error"]

            choice = response["choices"][0]
            msg = choice["message"]

            if msg.get("tool_calls"):
                # 1) 追加 assistant 消息（含 tool_calls）
                self._messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            }
                        }
                        for tc in msg["tool_calls"]
                    ]
                })

                # 2) 执行每个工具
                for tc in msg["tool_calls"]:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        func_args = {}

                    print(f"      🔧 调用工具: {func_name}({json.dumps(func_args, ensure_ascii=False)})", file=sys.stderr)

                    func = self._tool_functions.get(func_name)
                    if func:
                        try:
                            result = func(**func_args)
                        except Exception as e:
                            result = f"❌ 工具执行出错: {type(e).__name__}: {e}"
                    else:
                        result = f"❌ 未知工具: {func_name}"

                    # 截断过长的结果（避免token爆炸）
                    if isinstance(result, str) and len(result) > 12000:
                        result = result[:12000] + "\n\n...（结果过长，已截断至 12000 字符）"

                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
            else:
                # 3) LLM 返回最终回答
                self._messages.append({"role": "assistant", "content": msg["content"]})
                return msg["content"]

        return "⚠️ 已达到最大推理轮次，请尝试更具体的指令。"

    def chat(self, user_input: str):
        """打印助手回复"""
        print(f"\n{'='*60}")
        print(f"  🔬 科研助手 | {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        result = self.step(user_input)
        print(f"\n{result}")

    def reset(self):
        """重置对话历史（保留系统提示词）"""
        sys_prompt = self._messages[0]
        self._messages = [sys_prompt]

    def add_system_instruction(self, instruction: str):
        """追加系统指令（在已有系统提示词后追加）"""
        current = self._messages[0]["content"]
        self._messages[0]["content"] = current + "\n\n" + instruction


# ═══════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════

def print_banner():
    """打印启动 Banner"""
    banner = r"""
╔══════════════════════════════════════════════════════╗
║     🔬  Research Assistant  科研助手 Agent           ║
║     搜索论文 · 阅读 PDF · 分析方向 · 科研建议        ║
║                                                      ║
║  输入你想研究的方向，开始探索！                        ║
║  输入 /help 查看命令  |  输入 /quit 退出              ║
╚══════════════════════════════════════════════════════╝
"""
    print(banner)


def print_help():
    """打印帮助信息"""
    help_text = """
📖 **命令列表**

  /help          显示此帮助
  /new           开始新对话（重置历史）
  /papers        列出已下载论文
  /model         显示当前使用的模型
  /quit          退出程序

💡 **使用示例**

  • "帮我搜索强化学习在TSN调度中的最新论文"
  • "用arXiv搜索 diffusion model routing"
  • "精读第2篇论文"（搜索后）
  • "总结这三篇论文的核心方法和技术路线"
  • "分析一下这个领域未来的研究方向"
  • "我研究的是网络调度，最近有什么热点？"

📦 **数据缓存**
  下载的PDF保存在 data/papers/ 目录
"""
    print(help_text)


def main():
    """主入口"""
    # 确保数据目录存在
    Path("data/papers").mkdir(parents=True, exist_ok=True)

    # 检查API Key
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("❌ 未设置 DEEPSEEK_API_KEY")
        print("   请在 .env 文件中添加：")
        print("   DEEPSEEK_API_KEY=your_api_key_here")
        print("   或设置环境变量：$env:DEEPSEEK_API_KEY='your_key'")
        sys.exit(1)

    # 初始化 Agent
    try:
        agent = ResearchAgent()
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print_banner()

    # 是否首次对话
    first_turn = True

    while True:
        try:
            if first_turn:
                user_input = input("\n💡 你想研究什么方向？\n  >>> ").strip()
                first_turn = False
            else:
                user_input = input("\n  >>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n👋 再见！")
            break

        if not user_input:
            continue

        # ── 命令处理 ──
        if user_input.lower() in ("/quit", "/exit", "/q"):
            print("\n👋 再见！科研顺利！")
            break

        if user_input.lower() == "/help":
            print_help()
            continue

        if user_input.lower() in ("/new", "/reset"):
            agent.reset()
            first_turn = True
            print("\n🔄 已开始新对话！")
            continue

        if user_input.lower() == "/papers":
            print(f"\n{list_downloaded_papers()}")
            continue

        if user_input.lower() == "/model":
            print(f"\n🤖 当前模型: {agent.model}")
            continue

        # ── 正常对话 ──
        try:
            agent.chat(user_input)
        except requests.RequestException as e:
            print(f"\n❌ 网络请求失败: {e}")
            print("   请检查网络连接和 API Key 是否有效")
        except Exception as e:
            print(f"\n❌ 发生错误: {type(e).__name__}: {e}")
            print("   输入 /new 重新开始")


if __name__ == "__main__":
    main()
