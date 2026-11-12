"""Durable, index-independent PDF library. Originals and annotations are primary data."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import RuntimePaths


def now():
    return datetime.now(timezone.utc).isoformat()


class LibraryConflict(ValueError):
    pass


class LibraryStore:
    def __init__(self, paths: RuntimePaths):
        self.paths = paths
        paths.ensure_initialized()
        self.db_path = paths.database_dir / "library.db"
        self.files = paths.papers_dir / "library"
        self.files.mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    parent_id TEXT REFERENCES folders(id), created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS folder_name ON folders(COALESCE(parent_id,''),name);
                CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL, pages INTEGER NOT NULL,
                    folder_id TEXT REFERENCES folders(id), created_at TEXT NOT NULL,
                    trashed INTEGER NOT NULL DEFAULT 0, progress TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, document_id TEXT NOT NULL
                    REFERENCES documents(id), content TEXT NOT NULL, anchor TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS notes_document ON notes(document_id);
                CREATE TABLE IF NOT EXISTS discovered(path TEXT PRIMARY KEY, signature TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def name(value):
        value = str(value).strip()
        if not value or len(value) > 240 or any(ord(c) < 32 for c in value):
            raise ValueError("名称不能为空，且最多 240 个字符")
        return value

    def import_file(self, filename, stream, folder_id=None):
        if Path(filename).suffix.lower() != ".pdf":
            raise ValueError("论文库目前支持 PDF 文件")
        temp = self.files / f".import-{uuid.uuid4().hex}"
        try:
            with temp.open("xb") as target:
                size = 0
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise ValueError("PDF 最大为 100 MB")
                    target.write(chunk)
            return self.register(temp, Path(filename.replace('\\', '/')).name, folder_id)
        finally:
            temp.unlink(missing_ok=True)

    def register(self, source: Path, name: str, folder_id=None):
        import fitz
        name = self.name(name)
        try:
            if source.stat().st_size > 100 * 1024 * 1024:
                raise ValueError('PDF 最大为 100 MB')
            # PyMuPDF may keep a failed path-open handle alive on Windows. A
            # bounded byte buffer ensures a malformed upload can be removed.
            with fitz.open(stream=source.read_bytes(), filetype='pdf') as pdf:
                if not pdf.is_pdf or pdf.needs_pass or not pdf.page_count:
                    raise ValueError("请导入未加密且包含页面的 PDF")
                pages = pdf.page_count
        except (RuntimeError, OSError) as exc:
            raise ValueError("无法读取此 PDF，请检查文件是否损坏") from exc
        with source.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        identifier = 'doc_' + digest
        destination = self.files / f"{identifier}.pdf"
        with self.db() as db:
            # Serialize duplicate imports and protect an existing display name/folder.
            db.execute('BEGIN IMMEDIATE')
            if folder_id and not db.execute('SELECT 1 FROM folders WHERE id=?', (folder_id,)).fetchone():
                raise ValueError("目标文件夹不存在")
            if not destination.exists():
                staging = self.files / f'.copy-{uuid.uuid4().hex}'
                try:
                    shutil.copyfile(source, staging)
                    os.replace(staging, destination)
                finally:
                    staging.unlink(missing_ok=True)
            db.execute('INSERT OR IGNORE INTO documents(id,name,sha256,size_bytes,pages,folder_id,created_at) VALUES(?,?,?,?,?,?,?)',
                       (identifier, name, digest, source.stat().st_size, pages, folder_id, now()))
        return self.get(identifier, include_trash=True)

    def discover(self):
        """Snapshot legacy managed PDFs once, without moving or changing the originals."""
        errors = []
        for path in self.paths.papers_dir.glob('*.pdf'):
            if path.is_symlink() or path.name.startswith('.'):
                continue
            signature = f'{path.stat().st_size}:{path.stat().st_mtime_ns}'
            with self.db() as db:
                previous = db.execute('SELECT signature FROM discovered WHERE path=?', (path.name,)).fetchone()
            if previous and previous[0] == signature:
                continue
            try:
                self.register(path, path.name)
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO discovered VALUES(?,?)', (path.name, signature))
            except (ValueError, OSError) as exc:
                errors.append({'name': path.name, 'error': str(exc)})
        return errors

    @staticmethod
    def document(row):
        value = dict(row)
        value['progress'] = json.loads(value['progress'])
        value['trashed'] = bool(value['trashed'])
        return value

    def list(self):
        with self.db() as db:
            return {'documents': [self.document(r) for r in db.execute('SELECT * FROM documents ORDER BY created_at DESC')],
                    'folders': [dict(r) for r in db.execute('SELECT * FROM folders ORDER BY name')]}

    def get(self, identifier, *, include_trash=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM documents WHERE id=?', (identifier,)).fetchone()
        if not row or (row['trashed'] and not include_trash):
            raise KeyError(identifier)
        return self.document(row)

    def file(self, identifier):
        self.get(identifier)
        path = self.files / f'{identifier}.pdf'
        if path.resolve().parent != self.files.resolve() or not path.is_file():
            raise KeyError(identifier)
        return path

    def update(self, identifier, updates):
        self.get(identifier, include_trash=True)
        changes = {}
        for key in ('name', 'folder_id', 'trashed', 'progress'):
            if key in updates:
                value = updates[key]
                changes[key] = self.name(value) if key == 'name' else json.dumps(value) if key == 'progress' else value
        with self.db() as db:
            if changes:
                try:
                    db.execute('UPDATE documents SET '+','.join(k+'=?' for k in changes)+' WHERE id=?', (*changes.values(), identifier))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('目标文件夹不存在') from exc
        return self.get(identifier, include_trash=True)

    def folder(self, name, parent_id=None, identifier=None):
        name = self.name(name)
        with self.db() as db:
            try:
                if identifier:
                    if not db.execute('SELECT 1 FROM folders WHERE id=?', (identifier,)).fetchone():
                        raise KeyError(identifier)
                    db.execute('UPDATE folders SET name=? WHERE id=?', (name, identifier))
                else:
                    identifier = 'folder_' + uuid.uuid4().hex
                    db.execute('INSERT INTO folders VALUES(?,?,?,?)', (identifier, name, parent_id, now()))
            except sqlite3.IntegrityError as exc:
                raise ValueError('同级已有同名文件夹，或上级文件夹不存在') from exc
        return identifier

    def delete_folder(self, identifier):
        with self.db() as db:
            try:
                if not db.execute('DELETE FROM folders WHERE id=?', (identifier,)).rowcount:
                    raise KeyError(identifier)
            except sqlite3.IntegrityError as exc:
                raise ValueError('请先移出文件及子文件夹，再删除空文件夹') from exc

    def notes(self, identifier):
        self.get(identifier, include_trash=True)
        with self.db() as db:
            return [{**dict(r), 'anchor': json.loads(r['anchor'])} for r in db.execute(
                'SELECT * FROM notes WHERE document_id=? ORDER BY updated_at DESC', (identifier,))]

    def save_note(self, document_id, identifier, content, anchor, revision):
        self.get(document_id, include_trash=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM notes WHERE id=?', (identifier,)).fetchone()
            if previous and (previous['document_id'] != document_id or previous['revision'] != revision):
                raise LibraryConflict('笔记已被其他页面修改，请刷新后合并，当前草稿仍保留')
            if not previous and revision != 0:
                raise LibraryConflict('笔记版本已变化，请刷新后重试')
            db.execute('INSERT OR REPLACE INTO notes VALUES(?,?,?,?,?,?)',
                       (identifier, document_id, content, json.dumps(anchor), revision + 1, now()))
        return next(n for n in self.notes(document_id) if n['id'] == identifier)

    def delete_note(self, document_id, identifier):
        with self.db() as db:
            db.execute('DELETE FROM notes WHERE document_id=? AND id=?', (document_id, identifier))
