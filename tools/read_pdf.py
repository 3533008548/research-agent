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

from paper_artifacts import attach_image_assets, build_document_map, remove_document_map, write_document_map
from paper_quality import prepare_document_map, quality_failure_summary, verify_index_round_trip
from pdf_reader import PaperReader, extract_images
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


def _quality_checked_document_map(
    parsed_document: dict,
    *,
    paper_id: str,
    title: str,
    source_file: str,
    max_pages: int,
) -> tuple[dict, dict]:
    document_map = build_document_map(
        parsed_document, paper_id=paper_id, title=title, source_file=source_file,
    )
    return prepare_document_map(document_map, requested_pages=max_pages)


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

    title = pdf_path.stem.replace("_", " ")
    existing_ids = [
        str(paper.get("paper_id") or "")
        for paper in (paper_store.list_papers() if paper_store else [])
        if str(paper.get("title") or "") == title and str(paper.get("paper_id") or "")
    ]
    paper_id = f"paper_{uuid.uuid4().hex[:12]}"
    selected: tuple[PaperReader, dict, dict, dict, str] | None = None
    failed_attempts: list[str] = []

    # A second pass must use a materially different strategy. Re-running the
    # same parser cannot repair a deterministic layout or table-detection bug.
    for label, table_aware in (("表格感知解析", True), ("备用文本解析", False)):
        try:
            reader = PaperReader(max_pages=max_pages, max_chars=None)
            parsed_document = (
                reader.parse_document(str(pdf_path))
                if table_aware else reader.parse_document(str(pdf_path), extract_tables=False)
            )
            document_map, quality = _quality_checked_document_map(
                parsed_document,
                paper_id=paper_id,
                title=title,
                source_file=pdf_path.name,
                max_pages=max_pages,
            )
        except ImportError as exc:
            failed_attempts.append(f"{label}: {exc}")
            continue
        except Exception as exc:
            failed_attempts.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        if quality["accepted"]:
            selected = (reader, parsed_document, document_map, quality, label)
            break
        failed_attempts.append(f"{label}: {quality_failure_summary(quality)}")

    if selected is None:
        detail = "；".join(failed_attempts[:2]) or "未获得可用解析结果"
        return f"❌ PDF 未索引：切块质量检查失败，已使用备用解析策略重试。{detail}"

    reader, parsed_document, document_map, quality, parser_label = selected
    images: list[str] = []
    try:
        images = extract_images(
            str(pdf_path), max_pages=max_pages, output_dir=paths.images_dir,
        )
        if images:
            attach_image_assets(parsed_document, images)
            document_map, quality = _quality_checked_document_map(
                parsed_document,
                paper_id=paper_id,
                title=title,
                source_file=pdf_path.name,
                max_pages=max_pages,
            )
    except Exception as exc:
        print(f"      ⚠️ 图片提取失败: {exc}", file=sys.stderr)

    quality["attempts"] = 2 if parser_label == "备用文本解析" else 1
    quality["parser"] = parser_label
    result = reader.render_document(parsed_document)
    if images:
        result += "\n\n🖼️ **提取的图片**:\n" + "\n".join(f"  - {item}" for item in images)

    if paper_store:
        try:
            indexed_id = paper_store.index_document_map(
                document_map, title=title, paper_id=paper_id,
            )
            if indexed_id != paper_id:
                raise RuntimeError("向量库未返回新论文 ID")

            if hasattr(paper_store, "get_paper_chunks"):
                index_check = verify_index_round_trip(
                    document_map, paper_store.get_paper_chunks(paper_id),
                )
                quality["index_round_trip"] = index_check
                if not index_check["accepted"]:
                    paper_store.delete_paper(paper_id)
                    raise RuntimeError("向量库入库后自检失败，已回滚新索引")
            else:
                quality["index_round_trip"] = {"accepted": True, "skipped": True}

            document_map_path = write_document_map(
                paths, paper_id=paper_id, document_map=document_map,
            )
            for existing_id in existing_ids:
                if existing_id != paper_id:
                    paper_store.delete_paper(existing_id)
                    remove_document_map(paths, existing_id)
            if existing_ids:
                print(f"      🔄 新索引通过检查后已替换旧索引: {title}", file=sys.stderr)
            print("      📎 已索引到论文库", file=sys.stderr)
            quality_note = (
                f"已自动合并 {quality['repairs']['merged_orphan_text_chunks']} 个碎片块"
                if quality.get("status") == "repaired" else "质量检查通过"
            )
            result += (
                "\n\n---\n"
                f"📌 已索引论文 ID：`{paper_id}`。{quality_note}；解析策略：{parser_label}。\n"
                f"页面元素映射：`{document_map_path}`。\n"
                "如需完整的可追溯精读，请明确要求“生成论文证据卡”；"
                "系统会把结论保存为带页码、图表和来源块锚点的本地 Markdown 文件。"
            )
        except Exception as exc:
            try:
                paper_store.delete_paper(paper_id)
            except Exception:
                pass
            print(f"      ⚠️ RAG 索引失败: {exc}", file=sys.stderr)
            return f"❌ PDF 未索引：入库自检失败，原有论文索引未改动。{type(exc).__name__}: {exc}"

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
