"""
📄 read_pdf 工具 — 下载 + 提取 + 图片 + 索引 + 摘要卡片
"""

import sys
import requests
from pathlib import Path
from pdf_reader import read_pdf_enhanced


def handle_read_pdf(args: dict, paper_store=None, **kw) -> str:
    url_or_path = args.get("url_or_path", "")
    max_pages = args.get("max_pages", 15)

    pdf_dir = Path("data/papers")
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # ── URL 解析 & 下载 ──
    if url_or_path.startswith(("http://", "https://")):
        filename = url_or_path.split("/")[-1].split("?")[0]
        if not filename.endswith(".pdf"):
            if "arxiv.org/abs/" in url_or_path:
                arxiv_id = url_or_path.split("/abs/")[-1].split("v")[0]
                url_or_path = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                filename = f"{arxiv_id}.pdf"
            elif "arxiv.org/pdf/" in url_or_path:
                filename = url_or_path.split("/pdf/")[-1].split("?")[0]
                if not filename.endswith(".pdf"):
                    filename += ".pdf"
            else:
                filename = f"paper_{hash(url_or_path) & 0xFFFFFFFF:08x}.pdf"
        pdf_path = pdf_dir / filename
        if not pdf_path.exists():
            print(f"      📥 正在下载 PDF...", file=sys.stderr)
            try:
                resp = requests.get(url_or_path, timeout=60,
                    headers={"User-Agent": "Mozilla/5.0 (ResearchAssistant/1.0)"})
                resp.raise_for_status()
                pdf_path.write_bytes(resp.content)
                print(f"      ✅ 下载完成 ({len(resp.content)/1024:.0f} KB)", file=sys.stderr)
            except requests.RequestException as e:
                return f"❌ PDF 下载失败: {e}"
        else:
            print(f"      📂 使用缓存: {pdf_path.name}", file=sys.stderr)
    else:
        pdf_path = Path(url_or_path)
        if not pdf_path.exists():
            alt = pdf_dir / pdf_path.name
            if alt.exists():
                pdf_path = alt
            else:
                return f"❌ 本地文件不存在: {url_or_path}\n   已尝试: data/papers/{pdf_path.name}"

    # ── 文本提取 ──
    try:
        result = read_pdf_enhanced(str(pdf_path), max_pages=max_pages, max_chars=None)
    except ImportError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ PDF 解析失败: {type(e).__name__}: {e}"

    # ── 图片提取 ──
    if result and not result.startswith("❌"):
        try:
            from pdf_reader import extract_images
            imgs = extract_images(str(pdf_path), max_pages=max_pages)
            if imgs:
                result += "\n\n🖼 **提取的图片**:\n" + "\n".join(f"  - {i}" for i in imgs)
        except Exception as e:
            print(f"      ⚠ 图片提取失败: {e}", file=sys.stderr)

    # ── 索引 + 去重 + 摘要卡片 ──
    if paper_store and result and not result.startswith("❌"):
        try:
            title = pdf_path.stem.replace("_", " ")
            for p in paper_store.list_papers():
                if p["title"] == title:
                    paper_store.delete_paper(p["paper_id"])
                    print(f"      🗑️ 已删除旧索引: {title}", file=sys.stderr)
                    break
            paper_store.index_paper(result, title=title)
            print(f"      📚 已索引到论文库", file=sys.stderr)
            result += (
                "\n\n---\n"
                "📋 **请对这篇论文生成一个结构化摘要卡片**，覆盖以下维度（控制在 10 行以内）：\n"
                "1. **核心问题** — 这篇论文要解决什么\n"
                "2. **方法一句话** — 核心思路是什么\n"
                "3. **关键公式** — 最重要的 1-2 个公式（可用 query_papers 检索确认）\n"
                "4. **实验结论** — 主要实验结果\n"
                "5. **局限性/未解决的问题**\n"
                "6. **与本方向的其他论文关系**（如有已读论文）\n\n"
                "论文全文已自动索引到本地库，后续追问细节时请用 query_papers 精准检索。"
            )
        except Exception as e:
            print(f"      ⚠️ RAG 索引失败: {e}", file=sys.stderr)

    # ── 轻量三元组提取 ──
    if paper_store:
        try:
            title = pdf_path.stem.replace("_", " ")
            # 从文本中提取方法关键词和实验结果
            from memory import MemoryStore
            ms = MemoryStore()
            # 简单关键词匹配（不需要模型）
            import re
            methods = re.findall(r'(?:使用|采用|基于|提出|方法[是为]|model[ is]|method[ is]|approach[ is])\s*[：:]*\s*(.{10,60})', result[:5000])
            for m in methods[:3]:
                ms.add_triple(title, "uses_method", m.strip().rstrip("，。.,"))
            nums = re.findall(r'(\d+\.?\d*\s*%)', result[:5000])
            for n in nums[:2]:
                ms.add_triple(title, "achieves", n.strip())
        except Exception:
            pass

    return result
