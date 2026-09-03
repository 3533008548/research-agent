"""Persistent, user-owned research documents outside chat memory.

Each document keeps Markdown as the queryable source and a Word export for
people.  The store is deliberately file based: personal deployments do not
need another database just to maintain a small collection of research plans.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from runtime_paths import RuntimePaths


_DOCUMENT_ID = re.compile(r"research-doc-[a-f0-9]{12}\Z")
_store_lock = RLock()


class ResearchDocumentStore:
    """Store small, durable research plans without involving conversation memory."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.paths.ensure_initialized()
        self.directory = paths.research_documents_dir

    def save(self, title: str, content: str, document_id: str = "") -> dict[str, Any]:
        """Create a document, or replace the complete content of an existing one."""
        title = " ".join(str(title or "").split()).strip()
        content = str(content or "").strip()
        if not title:
            raise ValueError("文档标题不能为空")
        if not content:
            raise ValueError("文档内容不能为空")

        with _store_lock:
            existing = self._load_metadata(document_id) if document_id else None
            if document_id and existing is None:
                raise ValueError("未找到要更新的研究文档")
            document_id = document_id or f"research-doc-{secrets.token_hex(6)}"
            document_dir = self.directory / document_id
            document_dir.mkdir(parents=True, exist_ok=True)
            now = _now()
            metadata = {
                "version": 1,
                "document_id": document_id,
                "title": title[:160],
                "summary": _summary(content),
                "created_at": existing.get("created_at", now) if existing else now,
                "updated_at": now,
                "revision": int(existing.get("revision", 0)) + 1 if existing else 1,
                "markdown_file": "document.md",
                "docx_file": "document.docx",
            }
            markdown_path = document_dir / metadata["markdown_file"]
            docx_path = document_dir / metadata["docx_file"]
            self._write_docx(docx_path, metadata["title"], content)
            _write_text(markdown_path, content + "\n")
            _write_json(document_dir / "metadata.json", metadata)
            return self._with_paths(metadata)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """List newest documents without reading their full Markdown bodies."""
        limit = max(1, min(int(limit), 200))
        with _store_lock:
            documents = [
                self._with_paths(metadata)
                for metadata_path in self.directory.glob("research-doc-*/metadata.json")
                if (metadata := self._read_metadata_file(metadata_path)) is not None
            ]
        return sorted(documents, key=lambda item: str(item["updated_at"]), reverse=True)[:limit]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search titles and Markdown bodies; an empty query lists recent documents."""
        query = " ".join(str(query or "").split()).strip()
        query_folded = query.casefold()
        limit = max(1, min(int(limit), 10))
        matches: list[tuple[int, dict[str, Any]]] = []
        for metadata in self.list(limit=200):
            content = self._read_content(metadata)
            title = str(metadata["title"])
            if not query:
                matches.append((0, {**metadata, "excerpt": _summary(content)}))
                continue
            title_hits = title.casefold().count(query_folded)
            content_hits = content.casefold().count(query_folded)
            if not title_hits and not content_hits:
                continue
            matches.append((title_hits * 8 + content_hits, {
                **metadata,
                "excerpt": _excerpt(content, query_folded),
            }))
        matches.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
        return [item for _, item in matches[:limit]]

    def read(self, document_id: str) -> dict[str, Any] | None:
        """Read one document only when it was explicitly selected by its ID."""
        with _store_lock:
            metadata = self._load_metadata(document_id)
            if metadata is None:
                return None
            metadata = self._with_paths(metadata)
            return {**metadata, "content": self._read_content(metadata)}

    def _load_metadata(self, document_id: str) -> dict[str, Any] | None:
        document_id = str(document_id or "").strip()
        if not _DOCUMENT_ID.fullmatch(document_id):
            return None
        return self._read_metadata_file(self.directory / document_id / "metadata.json")

    @staticmethod
    def _read_metadata_file(path: Path) -> dict[str, Any] | None:
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        document_id = str(metadata.get("document_id") or "")
        if not _DOCUMENT_ID.fullmatch(document_id):
            return None
        if not str(metadata.get("title") or "").strip():
            return None
        return metadata

    def _read_content(self, metadata: dict[str, Any]) -> str:
        document_id = str(metadata["document_id"])
        filename = str(metadata.get("markdown_file") or "document.md")
        path = self.paths.safe_child(self.directory / document_id, filename)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _with_paths(self, metadata: dict[str, Any]) -> dict[str, Any]:
        document_id = str(metadata["document_id"])
        directory = self.directory / document_id
        return {
            **metadata,
            "markdown_path": str(directory / str(metadata.get("markdown_file") or "document.md")),
            "docx_path": str(directory / str(metadata.get("docx_file") or "document.docx")),
        }

    @staticmethod
    def _write_docx(path: Path, title: str, content: str) -> None:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt

        document = Document()
        section = document.sections[0]
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.85)
        section.right_margin = Inches(0.85)
        normal = document.styles["Normal"]
        normal.font.name = "Aptos"
        normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        normal.font.size = Pt(11)

        title_paragraph = document.add_paragraph()
        title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_run = title_paragraph.add_run(title)
        title_run.bold = True
        title_run.font.name = "Aptos Display"
        title_run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        title_run.font.size = Pt(20)

        first_heading = True
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading = re.match(r"^(#{1,3})\s+(.+)$", line)
            if heading:
                heading_text = heading.group(2).strip()
                if first_heading and heading_text == title:
                    first_heading = False
                    continue
                first_heading = False
                document.add_heading(heading_text, level=len(heading.group(1)))
                continue
            first_heading = False
            bullet = re.match(r"^[-*]\s+(.+)$", line)
            numbered = re.match(r"^\d+[.)]\s+(.+)$", line)
            if bullet:
                document.add_paragraph(bullet.group(1), style="List Bullet")
            elif numbered:
                document.add_paragraph(numbered.group(1), style="List Number")
            else:
                document.add_paragraph(line)

        temporary = path.with_suffix(".docx.tmp")
        document.core_properties.title = title
        document.core_properties.author = "Research Agent"
        document.save(str(temporary))
        os.replace(temporary, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(content: str, limit: int = 240) -> str:
    compact = " ".join(line.strip("#> -*\t ") for line in content.splitlines() if line.strip())
    return compact[:limit].strip()


def _excerpt(content: str, query: str, radius: int = 180) -> str:
    compact = " ".join(content.split())
    index = compact.casefold().find(query)
    if index < 0:
        return _summary(compact)
    start = max(0, index - radius)
    end = min(len(compact), index + len(query) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    return prefix + compact[start:end].strip() + suffix


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
