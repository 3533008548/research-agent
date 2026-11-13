"""End-to-end contracts for lossless storage and bounded, retrievable views."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from context_engine import prepare_model_context
from tool_results import ToolResultStore, result_header
from tool_runtime import ToolRuntime, ToolExecutionContext


class TestToolResults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'checkpoint.db')
        self.raw = ('算法行 αβ 中文😀\x00\n' * 1400) + 'FINAL_WITNESS 最后一行完整保留'

    def runtime(self, session='a', allowed=None, scope='', raw=None):
        return ToolRuntime(ToolExecutionContext(session_id=session, result_db=self.db,
            allowed_tool_names=allowed, research_scope=scope), executor=MagicMock(return_value=self.raw if raw is None else raw))

    def saved(self, runtime=None, name='query_papers'):
        runtime = runtime or self.runtime()
        preview = runtime.execute(name, {'paper_id_or_title': '论文 A'})
        meta = result_header(preview)
        result_id = meta['result_id'] if meta else json.loads(runtime.execute('list_tool_results', {}).split('\n', 1)[1])['results'][0]['result_id']
        return runtime, preview, result_id

    @staticmethod
    def body(view):
        return view.split('正文（字符范围为左闭右开）：\n', 1)[1]

    def test_every_character_can_be_recovered_after_restart_without_gaps(self):
        _, preview, result_id = self.saved()
        self.assertNotIn('FINAL_WITNESS', preview)
        runtime = self.runtime()
        pieces, offset = [], 0
        while True:
            view = runtime.execute('read_tool_result', {'result_id': result_id, 'offset': offset, 'limit': 937})
            meta = result_header(view)
            self.assertLessEqual(len(view), 10000)
            self.assertEqual(meta['offset'], offset)
            pieces.append(self.body(view))
            if not meta['has_more']:
                break
            self.assertGreater(meta['next_offset'], offset)
            offset = meta['next_offset']
        self.assertEqual(''.join(pieces), self.raw)

    def test_search_reaches_tail_and_never_calls_original_tool_again(self):
        runtime, _, result_id = self.saved()
        view = runtime.execute('search_tool_result', {'result_id': result_id, 'query': 'FINAL_WITNESS'})
        self.assertIn('最后一行完整保留', view)
        self.assertIn(f'"match_start": {self.raw.index("FINAL_WITNESS")}', view)
        self.assertIn('不是完整正文', view)
        runtime._executor.assert_called_once()
        listing = runtime.execute('list_tool_results', {})
        self.assertIn('"total_results": 1', listing)
        self.assertIn(result_id, runtime.execute('list_tool_results', {'query': 'FINAL_WITNESS'}))

    def test_directory_searches_full_result_and_is_paginated(self):
        runtime = self.runtime(raw='prefix ' * 200 + 'CATALOG_TAIL')
        ids = [self.saved(runtime)[2] for _ in range(12)]
        first = runtime.execute('list_tool_results', {'query': 'CATALOG_TAIL', 'limit': 5})
        data = json.loads(first.split('\n', 1)[1])
        self.assertEqual(data['total_results'], 12)
        self.assertEqual(data['next_offset'], 5)
        second = runtime.execute('list_tool_results', {'offset': 5, 'limit': 10})
        second = json.loads(second.split('\n', 1)[1])
        self.assertFalse(second['has_more'])
        self.assertEqual({x['result_id'] for x in data['results'] + second['results']}, set(ids))

    def test_session_and_origin_tool_and_research_scope_are_enforced(self):
        _, _, result_id = self.saved(self.runtime(allowed=frozenset({'query_papers'}), scope='local'))
        for other in (self.runtime('b'), self.runtime(allowed=frozenset({'search_papers'})),
                      self.runtime(scope='public', allowed=frozenset({'query_papers'}))):
            self.assertIn('❌', other.execute('read_tool_result', {'result_id': result_id, 'session_id': 'a'}))
            self.assertNotIn(result_id, other.execute('list_tool_results', {}))
        no_tools = self.runtime(allowed=frozenset())
        self.assertIn('not available', no_tools.execute('list_tool_results', {}))
        self.assertIn('FINAL_WITNESS', self.runtime().execute('search_tool_result', {'result_id': result_id, 'query': 'FINAL_WITNESS'}))

    def test_helpers_are_available_to_restricted_workers_without_widening_sources(self):
        from tool_catalog import get_tool_schemas
        names = {x['function']['name'] for x in get_tool_schemas({'query_papers'})}
        self.assertEqual(names, {'query_papers', 'list_tool_results', 'read_tool_result', 'search_tool_result'})
        self.assertEqual(get_tool_schemas(set()), [])

    def test_all_existing_tool_limits_preserve_full_source(self):
        from tool_catalog import get_tool_definition
        for name in ('read_pdf', 'query_papers', 'describe_image', 'search_papers'):
            runtime, view, result_id = self.saved(self.runtime(), name)
            self.assertLessEqual(len(view), get_tool_definition(name).result_limit)
            self.assertEqual(result_header(view)['total_chars'], len(self.raw))
            self.assertIn('FINAL_WITNESS', runtime.execute('search_tool_result', {'result_id': result_id, 'query': 'FINAL_WITNESS'}))

    def test_compaction_preserves_current_evidence_and_archived_reference(self):
        runtime, old, old_id = self.saved()
        current = runtime.execute('read_tool_result', {'result_id': old_id, 'offset': len(self.raw) - 6000, 'limit': 8000})
        messages = [{'role': 'user', 'content': '最初目标'}, {'role': 'assistant', 'content': '继续'}]
        messages += [{'role': 'assistant', 'tool_calls': [{'id': 'old', 'function': {'name': 'query_papers'}}]},
                     {'role': 'tool', 'tool_call_id': 'old', 'content': old}]
        messages += [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': '历史' * 1200} for i in range(100)]
        messages += [{'role': 'user', 'content': '核对最后一行'},
                     {'role': 'assistant', 'tool_calls': [{'id': 'new', 'function': {'name': 'read_tool_result'}}]},
                     {'role': 'tool', 'tool_call_id': 'new', 'content': current}]
        before = copy.deepcopy(messages)
        view = prepare_model_context(messages, context_limit=131072)
        self.assertTrue(view.compacted)
        self.assertIn(old_id, view.archive_context)
        self.assertIn('list_tool_results', view.archive_context)
        self.assertEqual(view.messages[-1]['content'], current)
        self.assertIn('FINAL_WITNESS', view.messages[-1]['content'])
        self.assertEqual(messages, before)
        for following in ({'role': 'system', 'content': '用户补充：仔细核对符号'},
                          {'role': 'assistant', 'content': '等待核验的答复'}):
            steered = prepare_model_context(messages + [following], context_limit=131072)
            self.assertEqual(steered.messages[-2]['content'], current)
            self.assertIn('FINAL_WITNESS', steered.messages[-2]['content'])

    def test_legacy_truncated_message_never_claims_full_original_is_preserved(self):
        from context_engine import _slim_old_message
        result = _slim_old_message({'role': 'tool', 'content': 'old ' * 2000 + '截断至 12000'})
        self.assertIn('旧版', result['content'])
        self.assertNotIn('完整原文仍在', result['content'])

    def test_long_current_turn_compacts_old_rounds_without_losing_user_or_latest_result(self):
        _, value, _ = self.saved()
        messages = [{'role': 'user', 'content': '当前任务必须保留'}]
        for i in range(100):
            messages += [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': str(i), 'function': {'name': 'query_papers'}}]},
                         {'role': 'tool', 'tool_call_id': str(i), 'content': value}]
        prepared = prepare_model_context(messages, context_limit=131072)
        self.assertTrue(prepared.compacted)
        self.assertEqual(prepared.messages[0], messages[0])
        self.assertEqual(prepared.messages[-2:], messages[-2:])
        self.assertLess(prepared.prepared_tokens, prepared.original_tokens)

    def test_save_failure_is_explicit_and_does_not_repeat_side_effect(self):
        runtime = self.runtime()
        with patch.object(ToolResultStore, 'save', side_effect=OSError('disk full')):
            result = runtime.execute('generate_paper_card', {})
        self.assertTrue(result.startswith('❌'))
        self.assertIn('保存失败', result)
        self.assertIsNone(result_header(result))
        runtime._executor.assert_called_once()

    def test_search_pagination_offsets_use_original_unicode_text(self):
        runtime, _, result_id = self.saved(self.runtime(raw=('中文😀 MATCH\n' * 30)))
        offset, starts = 0, []
        while True:
            view = runtime.execute('search_tool_result', {'result_id': result_id, 'query': 'MATCH', 'offset': offset, 'limit': 5})
            meta = result_header(view)
            hits = json.loads(view.split('\n')[-1])
            starts.extend(h['match_start'] for h in hits)
            if not meta['has_more']:
                break
            offset = meta['next_offset']
        self.assertEqual(starts, [i * len('中文😀 MATCH\n') + len('中文😀 ') for i in range(30)])

    def test_invalid_offsets_missing_result_and_literal_regex_are_safe(self):
        runtime, _, result_id = self.saved()
        self.assertIn('❌', runtime.execute('read_tool_result', {'result_id': '../private'}))
        self.assertIn('❌', runtime.execute('read_tool_result', {'result_id': result_id, 'offset': len(self.raw) + 1}))
        result = runtime.execute('search_tool_result', {'result_id': result_id, 'query': '.*'})
        self.assertTrue(result.endswith('[]'))

    def test_deleting_session_removes_results_and_keeps_other_sessions(self):
        from session_store import SessionStore
        sessions = SessionStore(self.db)
        try:
            a = sessions.create(title='A')['thread_id']
            b = sessions.create(title='B')['thread_id']
            _, _, first = self.saved(self.runtime(a))
            _, _, second = self.saved(self.runtime(b))
            self.assertTrue(sessions.delete(a))
            self.assertIn('❌', self.runtime(a).execute('read_tool_result', {'result_id': first}))
            self.assertIn(second, self.runtime(b).execute('list_tool_results', {}))
        finally:
            sessions.close()

    def test_trace_contains_no_tool_content_or_result_id(self):
        events = []
        runtime = self.runtime()
        runtime._event_callback = lambda event_type, **data: events.append(data)
        _, _, result_id = self.saved(runtime)
        self.assertNotIn('算法行', json.dumps(events))
        self.assertNotIn(result_id, json.dumps(events))

    def test_worker_result_belongs_to_parent_session_and_keeps_scope(self):
        from session_store import SessionStore
        sessions = SessionStore(self.db)
        try:
            parent = sessions.create(title='Parent')['thread_id']
            run = sessions.create_research_run(parent, 'question')['run_id']
            owner = sessions.tool_result_owner(run)
            self.assertEqual(owner, parent)
            worker = ToolRuntime(ToolExecutionContext(session_id='worker-a', result_session_id=owner,
                run_id=run, result_db=self.db, research_scope='local', allowed_tool_names=frozenset({'query_papers'})),
                executor=MagicMock(return_value=self.raw))
            _, _, result_id = self.saved(worker)
            self.assertIn(result_id, self.runtime(parent).execute('list_tool_results', {}))
            sessions.delete(parent)
            self.assertIn('❌', worker.execute('read_tool_result', {'result_id': result_id}))
        finally:
            sessions.close()

    def test_research_cards_preserve_result_reference_without_mistaking_header_for_source(self):
        from research_orchestrator import ResearchOrchestrator
        runtime, page, result_id = self.saved(self.runtime(raw='[Original Paper · p.4] Native evidence.\n' * 300))
        search = runtime.execute('search_tool_result', {'result_id': result_id, 'query': 'Native evidence'})
        messages = []
        for name, content in [('query_papers', page), ('search_tool_result', search), ('list_tool_results', runtime.execute('list_tool_results', {}))]:
            messages += [{'role': 'assistant', 'tool_calls': [{'id': name, 'function': {'name': name}}]},
                         {'role': 'tool', 'tool_call_id': name, 'content': content}]
        cards = ResearchOrchestrator._evidence_from_messages(messages, 'local', unlimited=True)
        self.assertTrue(cards)
        for card in cards:
            self.assertEqual(card['source_type'], 'query_papers')
            self.assertNotIn('[tool_result]', card['excerpt'])
            self.assertNotEqual(card['source'], 'tool_result')
            self.assertEqual(card['source_anchor']['tool_result']['result_id'], result_id)

    def test_long_metadata_and_control_characters_keep_views_bounded(self):
        runtime = self.runtime(raw='\x01' * 15000)
        for _ in range(10):
            value = runtime.execute('describe_image', {'title': '\\' * 400, 'paper_id_or_title': '长' * 600, 'figure': '4'})
            self.assertLessEqual(len(value), 2000)
        listing = runtime.execute('list_tool_results', {})
        self.assertLessEqual(len(listing), 12000)
        result_id = result_header(value)['result_id']
        found = runtime.execute('search_tool_result', {'result_id': result_id, 'query': '\x01' * 300})
        self.assertLessEqual(len(found), 8000)

    def test_existing_sqlite_backup_contains_complete_tool_results(self):
        from scripts.backup_runtime import _backup_sqlite
        _, _, result_id = self.saved()
        backup = Path(self.tmp.name) / 'backup.db'
        _backup_sqlite(Path(self.db), backup)
        recovered = ToolResultStore(backup).get(result_id, session_id='a')
        self.assertEqual(recovered['content'], self.raw)

    def test_concurrent_results_remain_unique_and_session_scoped(self):
        from concurrent.futures import ThreadPoolExecutor
        def run(index):
            _, _, result_id = self.saved(self.runtime('parallel-' + str(index)))
            return index, result_id
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(run, range(12)))
        self.assertEqual(len({result_id for _, result_id in records}), 12)
        for index, result_id in records:
            listing = self.runtime('parallel-' + str(index)).execute('list_tool_results', {})
            self.assertIn(result_id, listing)
            self.assertIn('"total_results": 1', listing)

    def test_graph_can_recover_saved_tail_after_restart_and_history_compaction(self):
        from graph_builder import build_graph
        from llm_client import LLMClient
        _, old, result_id = self.saved()
        calls = []
        def model_request(*args, **kwargs):
            payload = kwargs['json']
            calls.append(payload)
            names = {t['function']['name'] for t in payload['tools']}
            self.assertIn('search_tool_result', names)
            responses = [m for m in payload['messages'] if m['role'] == 'tool']
            found = next((m for m in responses if '"view": "search"' in m['content']), None)
            read = next((m for m in responses if 'FINAL_WITNESS' in m['content'] and '"view": "page"' in m['content']), None)
            if read:
                self.assertIn('最后一行完整保留', read['content'])
                message = {'role': 'assistant', 'content': '已核对最后一行 FINAL_WITNESS'}
            else:
                if found:
                    name, tool_args = 'read_tool_result', {'result_id': result_id, 'offset': len(self.raw) - 1500, 'limit': 2000}
                else:
                    self.assertIn(result_id, payload['messages'][0]['content'])
                    name, tool_args = 'search_tool_result', {'result_id': result_id, 'query': 'FINAL_WITNESS'}
                message = {'role': 'assistant', 'content': None, 'reasoning_content': '核对原始证据',
                           'tool_calls': [{'id': 'call-' + str(len(calls)), 'type': 'function',
                                           'function': {'name': name, 'arguments': json.dumps(tool_args)}}]}
            response = MagicMock(status_code=200)
            response.json.return_value = {'choices': [{'message': message}], 'usage': {'prompt_tokens': 700, 'completion_tokens': 10, 'total_tokens': 710}}
            return response
        messages = [{'role': 'user', 'content': '原始任务'}, {'role': 'assistant', 'content': '继续'},
                    {'role': 'assistant', 'content': None, 'reasoning_content': '读取论文',
                     'tool_calls': [{'id': 'old', 'function': {'name': 'query_papers', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'old', 'content': old}]
        messages.extend({'role': 'user' if i % 2 == 0 else 'assistant', 'content': '历史' * 1200} for i in range(100))
        messages.append({'role': 'user', 'content': '核对刚才论文的最后一行'})
        with patch('llm_client.requests.post', side_effect=model_request):
            app = build_graph(api_key='test', checkpoint_db=self.db, enable_verify=False,
                allowed_tool_names={'query_papers'}, llm_client=LLMClient('test', 'https://example.test/chat', max_retries=0),
                tool_context=ToolExecutionContext(session_id='a', result_db=self.db, allowed_tool_names=frozenset({'query_papers'})))
            try:
                result = app.invoke({'messages': messages, 'metadata': {}}, config={'configurable': {'thread_id': 'a'}})
            finally:
                app.checkpointer.conn.close()
        self.assertEqual(len(calls), 3)
        self.assertIn('已核对最后一行', result['messages'][-1]['content'])
        self.assertEqual(result['messages'][:len(messages)], messages)


if __name__ == '__main__':
    unittest.main()
