"""Durable, session-scoped tool output; bounded views never replace the source."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid


RESULT_TOOLS = frozenset({'list_tool_results', 'read_tool_result', 'search_tool_result'})
HEADER = '[tool_result] '
RESULT_GUIDANCE = (
    '工具结果预览不等于全文。has_more=true 时可用 read_tool_result 按 next_offset 续读，'
    '或 search_tool_result 在保存的完整工具结果中搜索；忘记结果 ID 时用 list_tool_results。'
    '核对原文细节或判断内容不存在前，先补读相关范围；不要把未展示部分当成不存在。'
    '这里的完整仅指工具实际返回内容，不代表 PDF 所有页、OCR 或远端来源均已完整读取。'
)


def bounded_int(value, default, low, high):
    try:
        return min(high, max(low, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def result_header(content: str) -> dict | None:
    if not content.startswith(HEADER):
        return None
    try:
        value = json.loads(content[len(HEADER):].split('\n', 1)[0])
        if (isinstance(value, dict) and re.fullmatch(r'tr_[a-f0-9]{32}', str(value.get('result_id', '')))
                and isinstance(value.get('tool'), str) and isinstance(value.get('source'), dict)
                and type(value.get('total_chars')) is int and value['total_chars'] >= 0
                and value.get('view') in {'page', 'search', 'reference'}):
            return value
    except (ValueError, TypeError):
        pass
    return None


def reference_view(content: str) -> str:
    """A compressed view claims no visible body, even if an earlier view was full."""
    meta = result_header(content)
    if not meta:
        return ''
    reference = {key: meta[key] for key in ('result_id', 'tool', 'total_chars', 'source') if key in meta}
    reference.update(view='reference', offset=0, end=0, has_more=True, next_offset=0)
    return HEADER + json.dumps(reference, ensure_ascii=False) + '\n历史工具正文已省略；可按结果 ID 重新读取或搜索。'


class ToolResultStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    @contextmanager
    def connection(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS tool_results (
                result_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                run_id TEXT NOT NULL, tool TEXT NOT NULL, scope TEXT NOT NULL,
                source_json TEXT NOT NULL, content TEXT NOT NULL,
                total_chars INTEGER NOT NULL, created_at TEXT NOT NULL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_tool_results_session ON tool_results(session_id, created_at)')
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, *, session_id, run_id, tool, scope, source, content):
        record = dict(result_id='tr_' + uuid.uuid4().hex, session_id=session_id,
                      run_id=run_id, tool=tool, scope=scope, source=source,
                      total_chars=len(content), created_at=datetime.now(timezone.utc).isoformat())
        with self.connection() as conn:
            conn.execute('INSERT INTO tool_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (record['result_id'], session_id, run_id, tool, scope,
                          json.dumps(source, ensure_ascii=False), content,
                          len(content), record['created_at']))
        return record

    @staticmethod
    def access_clause(session_id, allowed_tools, scope):
        if not session_id:
            raise ValueError('缺少当前会话，不能读取工具结果')
        clause, values = 'session_id=?', [session_id]
        if scope:
            clause += ' AND scope=?'
            values.append(scope)
        if allowed_tools is not None:
            names = sorted(set(allowed_tools) - RESULT_TOOLS)
            clause += ' AND tool IN (' + ','.join('?' for _ in names) + ')'
            values.extend(names)
        return clause, values

    def get(self, result_id, *, session_id, allowed_tools=None, scope=''):
        if not re.fullmatch(r'tr_[a-f0-9]{32}', str(result_id)):
            raise ValueError('结果 ID 无效；请用 list_tool_results 查询当前会话目录')
        clause, values = self.access_clause(session_id, allowed_tools, scope)
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM tool_results WHERE ' + clause + ' AND result_id=?',
                               (*values, result_id)).fetchone()
        if row is None:
            raise ValueError('当前会话和权限范围内未找到该结果；请用 list_tool_results 查询，不能据此判断原文不存在')
        record = dict(row)
        record['source'] = json.loads(record.pop('source_json'))
        return record

    def list(self, *, session_id, allowed_tools=None, scope='', query='', offset=0, limit=10):
        clause, values = self.access_clause(session_id, allowed_tools, scope)
        query = str(query or '')[:300]
        if query:
            clause += ' AND (instr(lower(content),lower(?))>0 OR instr(lower(tool),lower(?))>0)'
            values.extend((query, query))
        with self.connection() as conn:
            total = conn.execute('SELECT count(*) FROM tool_results WHERE ' + clause, values).fetchone()[0]
            rows = conn.execute('SELECT result_id,tool,total_chars,created_at,source_json,substr(content,1,120) AS preview '
                                'FROM tool_results WHERE ' + clause + ' ORDER BY created_at DESC,result_id LIMIT ? OFFSET ?',
                                (*values, limit, offset)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record['source'] = json.loads(record.pop('source_json'))
            records.append(record)
        return {'total_results': total, 'offset': offset, 'has_more': offset + len(records) < total,
                'next_offset': offset + len(records) if offset + len(records) < total else None, 'results': records}


def source_reference(args):
    # Keep source locators, never credentials, raw arguments, or signed URLs.
    source = {}
    for key in ('paper_id_or_title', 'title', 'figure', 'page', 'task'):
        if args.get(key) is not None:
            value = ''.join(ch if ord(ch) >= 32 else ' ' for ch in str(args[key]))[:120]
            candidate = {**source, key: value}
            if len(json.dumps(candidate, ensure_ascii=False)) <= 450:
                source = candidate
    return source


def page_view(record, text, *, offset=0, limit=4000, budget=10000):
    total = record['total_chars']
    if not 0 <= offset <= total:
        raise ValueError(f'offset 超出范围 0–{total}')
    # Reserve space for the complete reference and continuation instructions.
    header_meta = {key: record[key] for key in ('result_id', 'tool', 'total_chars', 'source')}
    overhead = len(json.dumps(header_meta, ensure_ascii=False)) + len(RESULT_GUIDANCE) + 250
    if budget <= overhead:
        # Tiny third-party limits may fit a reference but no useful body. Keep
        # the ID intact, and let the bounded read tool choose the next window.
        compact = {**header_meta, 'source': {}, 'view': 'reference', 'has_more': True, 'next_offset': offset}
        pointer = HEADER + json.dumps(compact, ensure_ascii=False, separators=(',', ':')) + '\nread_tool_result'
        if len(pointer) <= budget:
            return pointer
        return '已保存；请用 list_tool_results 查询。' if budget >= 35 else '已保存'
    take = max(1, min(limit, budget - overhead))
    end = min(total, offset + take)
    # Prefer a nearby complete line; offsets always use the original Python text.
    if end < total:
        boundary = text.rfind('\n', offset + max(1, take // 2), end)
        if boundary > offset:
            end = boundary + 1
    header_meta.update(view='page', offset=offset, end=end, has_more=end < total,
                       next_offset=end if end < total else None)
    return (HEADER + json.dumps(header_meta, ensure_ascii=False) + '\n' + RESULT_GUIDANCE
            + '\n正文（字符范围为左闭右开）：\n' + text[offset:end])


def search_view(record, query, *, offset=0, limit=5):
    text = record['content']
    query = str(query or '')
    if not query.strip() or len(query) > 300:
        raise ValueError('请提供 1–300 字符的原文关键词；按字面搜索，不接受正则表达式')
    if not 0 <= offset <= len(text):
        raise ValueError(f'offset 超出范围 0–{len(text)}')
    hits = []
    more = False
    for match in re.finditer(re.escape(query), text[offset:], flags=re.I):
        start, end = offset + match.start(), offset + match.end()
        if len(hits) == limit:
            more = True
            break
        lo, hi = max(0, start - 160), min(len(text), end + 160)
        hits.append({'match_start': start, 'match_end': end, 'offset': lo, 'end': hi, 'text': text[lo:hi]})
    while len(hits) > 1 and len(json.dumps(hits, ensure_ascii=False)) > 6000:
        hits.pop()
        more = True
    meta = {key: record[key] for key in ('result_id', 'tool', 'total_chars', 'source')}
    meta.update(view='search', search_from=offset, has_more=more,
                next_offset=hits[-1]['match_end'] if more else None)
    return (HEADER + json.dumps(meta, ensure_ascii=False) + '\n'
            '以下是保存结果中的字面匹配片段，不是完整正文。未命中只表示此工具结果无该字面词，不证明原 PDF 不存在该内容。'
            '可用 read_tool_result 按命中 offset 扩展读取。\n' + json.dumps(hits, ensure_ascii=False))


def handle_result_tool(name, args, *, store, session_id, allowed_tools=None, scope=''):
    access = dict(session_id=session_id, allowed_tools=allowed_tools, scope=scope)
    offset = bounded_int(args.get('offset', 0), 0, 0, 2**63 - 1)
    if name == 'list_tool_results':
        data = store.list(**access, query=args.get('query', ''), offset=offset,
                          limit=bounded_int(args.get('limit', 10), 10, 1, 10))
        while len(data['results']) > 1 and len(json.dumps(data, ensure_ascii=False)) > 10500:
            data['results'].pop()
            data.update(has_more=True, next_offset=offset + len(data['results']))
        return '当前会话已保存的工具结果；可按 result_id 读取或搜索，目录未展示不等于没有结果。\n' + json.dumps(data, ensure_ascii=False)
    record = store.get(args.get('result_id', ''), **access)
    if name == 'read_tool_result':
        return page_view(record, record['content'], offset=offset,
                         limit=bounded_int(args.get('limit', 4000), 4000, 1, 8000))
    return search_view(record, args.get('query', ''), offset=offset,
                       limit=bounded_int(args.get('limit', 5), 5, 1, 5))
