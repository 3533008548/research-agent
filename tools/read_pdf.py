"""Download, parse, and index PDFs inside the managed runtime directory."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import sys
import urllib.parse
import uuid
from pathlib import Path

import requests

from pdf_reader import read_pdf_enhanced
from runtime_paths import get_runtime_paths


MAX_PDF_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 100
PDF_DOWNLOAD_TIMEOUT = (3.05, 20)
MAX_REDIRECTS = 3


class PDFDownloadError(RuntimeError):
    """A remote file could not be safely accepted as a managed PDF."""


def _validate_public_https_url(raw_url: str) -> urllib.parse.ParseResult:
    """Reject local-network URLs before an LLM-directed download is sent.

    ``read_pdf`` is a read tool, but without this check it can become an SSRF
    primitive when a prompt-injected page tells the model to fetch an internal
    service. Redirects are validated again by ``_download_pdf``.
    """
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise PDFDownloadError("仅支持公网 HTTPS 的 PDF 链接")
    if parsed.username or parsed.password or (parsed.port not in (None, 443)):
        raise PDFDownloadError("PDF 链接不能包含账号信息或非标准端口")

    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise PDFDownloadError("不允许访问本机或局域网地址")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise PDFDownloadError("无法解析 PDF 链接的域名") from exc
    if not addresses:
        raise PDFDownloadError("无法解析 PDF 链接的域名")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise PDFDownloadError("不允许访问本机、内网或保留地址")
        except ValueError as exc:
            raise PDFDownloadError("PDF 链接的解析结果无效") from exc
    return parsed


def _normalise_arxiv_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if host not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return raw_url
    if "/abs/" in parsed.path:
        identifier = parsed.path.split("/abs/", 1)[1].strip("/")
        match = re.match(r"^(.+?)v\d+$", identifier)
        if match:
            identifier = match.group(1)
        if identifier:
            return f"https://arxiv.org/pdf/{urllib.parse.quote(identifier, safe='/.')}.pdf"
    if "/pdf/" in parsed.path and not parsed.path.lower().endswith(".pdf"):
        return raw_url.rstrip("/") + ".pdf"
    return raw_url


def _download_filename(url: str) -> str:
    """Generate a stable, host-safe cache name for an arbitrary PDF URL."""
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    if name.lower().endswith(".pdf"):
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
        if safe_name:
            return safe_name[:120]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"paper_{digest}.pdf"


def _download_pdf(url: str, destination: Path) -> int:
    """Download a bounded PDF with validated redirects and atomic replacement."""
    current_url = _normalise_arxiv_url(url)
    temp_path: Path | None = None
    for _ in range(MAX_REDIRECTS + 1):
        _validate_public_https_url(current_url)
        response = None
        try:
            response = requests.get(
                current_url,
                timeout=PDF_DOWNLOAD_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (ResearchAssistant/1.0)"},
                stream=True,
                allow_redirects=False,
            )
            status_code = int(response.status_code)
            if 300 <= status_code < 400:
                location = response.headers.get("Location", "")
                if not location:
                    raise PDFDownloadError("PDF 下载重定向缺少目标地址")
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            response.raise_for_status()

            content_length = response.headers.get("Content-Length", "")
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = 0
            if declared_size > MAX_PDF_DOWNLOAD_BYTES:
                raise PDFDownloadError("PDF 文件过大（上限 50MB）")
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("text/") or "text/html" in content_type:
                raise PDFDownloadError("下载链接返回的不是 PDF 文件")

            temp_path = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
            total = 0
            with temp_path.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_PDF_DOWNLOAD_BYTES:
                        raise PDFDownloadError("PDF 文件过大（上限 50MB）")
                    stream.write(chunk)
            with temp_path.open("rb") as stream:
                signature = stream.read(8)
            if not signature.startswith(b"%PDF-"):
                raise PDFDownloadError("下载内容不是有效的 PDF 文件")
            temp_path.replace(destination)
            return total
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
    raise PDFDownloadError("PDF 下载重定向次数过多")


def _resolve_local_pdf(path_text: str, papers_dir: Path) -> Path:
    """Only accept PDFs already copied into the managed papers directory."""
    requested = Path(path_text).expanduser()
    base = papers_dir.resolve()
    candidate = (requested.resolve() if requested.is_absolute() else (base / requested.name).resolve())
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("本地 PDF 必须先上传或放入运行时 papers 目录") from exc
    if candidate.suffix.lower() != ".pdf" or not candidate.is_file():
        raise ValueError("未找到可读取的 PDF 文件")
    return candidate


def handle_read_pdf(args: dict, paper_store=None, memory_store=None, **_kwargs) -> str:
    raw_input = str(args.get("url_or_path", "") or "").strip()
    if not raw_input or len(raw_input) > 2048:
        return "❌ 请提供有效的 PDF 链接或运行时论文目录中的文件名。"
    try:
        max_pages = max(1, min(MAX_PDF_PAGES, int(args.get("max_pages", 15))))
    except (TypeError, ValueError):
        return "❌ max_pages 必须是 1 到 100 之间的整数。"

    paths = get_runtime_paths()
    pdf_dir = paths.papers_dir
    if raw_input.startswith(("http://", "https://")):
        url = _normalise_arxiv_url(raw_input)
        try:
            _validate_public_https_url(url)
        except PDFDownloadError as exc:
            return f"❌ PDF 下载被拒绝: {exc}"
        pdf_path = paths.safe_child(pdf_dir, _download_filename(url))
        if not pdf_path.exists():
            print("      📥 正在下载 PDF...", file=sys.stderr)
            try:
                downloaded = _download_pdf(url, pdf_path)
            except (PDFDownloadError, requests.RequestException) as exc:
                return f"❌ PDF 下载失败: {exc}"
            print(f"      ✅ 下载完成 ({downloaded / 1024:.0f} KB)", file=sys.stderr)
        else:
            print(f"      📂 使用缓存: {pdf_path.name}", file=sys.stderr)
    else:
        try:
            pdf_path = _resolve_local_pdf(raw_input, pdf_dir)
        except ValueError as exc:
            return f"❌ {exc}"

    try:
        result = read_pdf_enhanced(str(pdf_path), max_pages=max_pages, max_chars=None)
    except ImportError as exc:
        return f"❌ {exc}"
    except Exception as exc:
        return f"❌ PDF 解析失败: {type(exc).__name__}: {exc}"

    if result and not result.startswith("❌"):
        try:
            from pdf_reader import extract_images

            images = extract_images(
                str(pdf_path), max_pages=max_pages, output_dir=paths.images_dir,
            )
            if images:
                result += "\n\n🖼️ **提取的图片**:\n" + "\n".join(f"  - {item}" for item in images)
        except Exception as exc:
            print(f"      ⚠️ 图片提取失败: {exc}", file=sys.stderr)

    if paper_store and result and not result.startswith("❌"):
        try:
            title = pdf_path.stem.replace("_", " ")
            for paper in paper_store.list_papers():
                if paper["title"] == title:
                    paper_store.delete_paper(paper["paper_id"])
                    print(f"      🗑️ 已删除旧索引: {title}", file=sys.stderr)
                    break
            paper_store.index_paper(result, title=title)
            print("      📎 已索引到论文库", file=sys.stderr)
            result += (
                "\n\n---\n"
                "🧵 **请对这篇论文生成一个结构化摘要卡片**，覆盖以下维度（控制在 10 行以内）：\n"
                "1. **核心问题**\n2. **方法一句话**\n3. **关键公式**\n"
                "4. **实验结论**\n5. **局限性**\n6. **与已读论文的关系**\n\n"
                "论文全文已索引到本地库；后续细节请使用 query_papers 检索。"
            )
        except Exception as exc:
            print(f"      ⚠️ RAG 索引失败: {exc}", file=sys.stderr)

    if paper_store:
        try:
            from memory import MemoryStore

            title = pdf_path.stem.replace("_", " ")
            owns_memory_store = memory_store is None
            store = memory_store or MemoryStore(str(paths.memory_db))
            methods = re.findall(
                r"(?:使用|采用|基于|提出|方法[是为]|model[ is]|method[ is]|approach[ is])\s*[（(]*\s*(.{10,60})",
                result[:5000],
            )
            for method in methods[:3]:
                store.add_triple(title, "uses_method", method.strip().rstrip("，。,."))
            for number in re.findall(r"(\d+\.?\d*\s*%)", result[:5000])[:2]:
                store.add_triple(title, "achieves", number.strip())
            if owns_memory_store:
                store.close()
        except Exception:
            pass

    return result
