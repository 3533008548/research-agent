import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import APIKeyAuthenticator
from api.library_routes import router
from api.local_queue import LocalBroker, LocalDailyBroker, LocalResearchBroker
from library_store import LibraryConflict, LibraryStore
from runtime_paths import RuntimePaths
from tests import test_run_dispatcher as dispatcher_tests


def pdf_bytes(text='Reading with traceable evidence'):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((70, 80), text)
    value = document.tobytes()
    document.close()
    return value


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(self.temp.name)
        self.store = LibraryStore(self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def test_notes_survive_duplicate_import_rename_move_trash_and_restart(self):
        data = pdf_bytes()
        paper = self.store.import_file('first.pdf', io.BytesIO(data))
        note = self.store.save_note(paper['id'], 'note-1', 'Important result', {'page': 1, 'quote': 'evidence', 'rects': []}, 0)
        folder = self.store.folder('方法')
        self.store.update(paper['id'], {'folder_id': folder, 'name': 'renamed.pdf', 'trashed': True})
        duplicate = self.store.import_file('second.pdf', io.BytesIO(data))
        self.assertEqual(duplicate['id'], paper['id'])
        self.assertEqual(duplicate['name'], 'renamed.pdf')
        self.assertTrue(duplicate['trashed'])
        self.store.update(paper['id'], {'trashed': False})
        reopened = LibraryStore(self.paths)
        self.assertEqual(reopened.notes(paper['id'])[0], note)
        self.assertTrue(reopened.file(paper['id']).is_file())
        with self.assertRaises(ValueError):
            reopened.delete_folder(folder)

    def test_snapshot_preserves_original_version_when_legacy_file_changes(self):
        source = self.paths.papers_dir / 'old.pdf'
        source.write_bytes(pdf_bytes('version one'))
        self.assertEqual(self.store.discover(), [])
        first = self.store.list()['documents'][0]
        original = self.store.file(first['id']).read_bytes()
        source.write_bytes(pdf_bytes('version two is changed'))
        self.store.discover()
        self.assertEqual(len(self.store.list()['documents']), 2)
        self.assertEqual(self.store.file(first['id']).read_bytes(), original)

    def test_conflicting_edits_do_not_overwrite_a_newer_note(self):
        doc = self.store.import_file('test.pdf', io.BytesIO(pdf_bytes()))
        self.store.save_note(doc['id'], 'note', 'first', None, 0)
        with self.assertRaises(LibraryConflict):
            self.store.save_note(doc['id'], 'note', 'stale', None, 0)
        self.assertEqual(self.store.notes(doc['id'])[0]['content'], 'first')

    def test_invalid_file_does_not_enter_the_library(self):
        with self.assertRaises(ValueError):
            self.store.import_file('../bad.pdf', io.BytesIO(b'not a PDF'))
        self.assertEqual(self.store.list()['documents'], [])
        self.assertEqual(list(self.store.files.iterdir()), [])

    def test_assistant_can_resolve_library_snapshot_but_not_escape_papers(self):
        from tools.read_pdf import _resolve_local_pdf
        doc = self.store.import_file('test.pdf', io.BytesIO(pdf_bytes()))
        self.assertEqual(_resolve_local_pdf(f"library/{doc['id']}.pdf", self.paths.papers_dir), self.store.file(doc['id']))
        with self.assertRaises(ValueError):
            _resolve_local_pdf('../outside.pdf', self.paths.papers_dir)

    def test_authenticated_content_ranges_and_note_validation(self):
        app = FastAPI()
        app.state.library_store = self.store
        app.state.api_authenticator = APIKeyAuthenticator('test', required=True)
        app.include_router(router)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/v1/library').status_code, 401)
            headers = {'X-API-Key': 'test'}
            response = client.post('/api/v1/library/documents', files={'file': ('paper.pdf', pdf_bytes(), 'application/pdf')}, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            doc = response.json()
            response = client.get(f"/api/v1/library/documents/{doc['id']}/file", headers={**headers, 'Range': 'bytes=0-9'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.content), 10)
            self.assertNotIn(str(self.temp.name), response.headers.get('content-disposition', ''))
            response = client.put(f"/api/v1/library/documents/{doc['id']}/notes/n", headers=headers,
                                  json={'content': 'note', 'anchor': {'page': 3}, 'revision': 0})
            self.assertEqual(response.status_code, 422)

    def test_translation_uses_selected_passage_and_closes_model_response(self):
        doc = self.store.import_file('test.pdf', io.BytesIO(pdf_bytes()))
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '可追溯的证据'}}]}
        client = Mock()
        client.post.return_value = response
        app = FastAPI()
        app.state.library_store = self.store
        app.state.api_authenticator = APIKeyAuthenticator('test', required=True)
        app.state.agent = SimpleNamespace(cfg=SimpleNamespace(deepseek_key='test-only'), model='test-model', llm_client=client)
        app.include_router(router)
        with TestClient(app) as http:
            result = http.post(f"/api/v1/library/documents/{doc['id']}/translate", headers={'X-API-Key': 'test'},
                               json={'page': 1, 'text': 'Traceable evidence'})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['text'], '可追溯的证据')
            self.assertEqual(client.post.call_args.args[0]['messages'][-1]['content'], 'Traceable evidence')
            response.close.assert_called_once()
            client.post.side_effect = RuntimeError('private provider detail')
            failed = http.post(f"/api/v1/library/documents/{doc['id']}/translate", headers={'X-API-Key': 'test'},
                               json={'page': 1, 'text': 'Traceable evidence'})
            self.assertEqual(failed.status_code, 502)
            self.assertNotIn('private provider detail', failed.text)


class LocalQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.db'
        self.broker = LocalBroker(self.path)

    def tearDown(self):
        self.doCleanups()
        self.temp.cleanup()

    def test_two_consumers_cannot_reserve_the_same_job(self):
        self.broker.enqueue('run', 'session', 'message')
        barrier = threading.Barrier(2)
        jobs = []
        def take(owner):
            broker = LocalBroker(self.path)
            barrier.wait()
            jobs.append(broker.reserve(owner, block_ms=1))
        threads = [threading.Thread(target=take, args=(owner,)) for owner in ('one', 'two')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_recovery_fences_old_owner_and_keeps_events_and_cancellation(self):
        identifier = self.broker.enqueue('run', 'session', 'persist me')
        self.broker.reserve('old')
        self.broker.publish('run', {'type': 'token', 'text': 'partial'})
        self.broker.request_cancel('run')
        reopened = LocalBroker(self.path)
        reopened.recover_abandoned()
        recovered = reopened.reserve('new')
        self.assertEqual(recovered.message, 'persist me')
        self.assertFalse(self.broker.acknowledge_owned('old', identifier))
        self.assertEqual(self.broker.renew('old', [identifier]), set())
        self.assertTrue(reopened.cancel_requested('run'))
        events, cursor = reopened.events('run', block_ms=1)
        self.assertEqual(events, [{'type': 'token', 'text': 'partial'}])
        self.assertEqual(reopened.events('run', cursor, block_ms=1)[0], [])
        self.assertTrue(reopened.acknowledge_owned('new', identifier))
        self.assertEqual(reopened.queue_depth(), 0)

    def test_research_and_daily_job_contracts(self):
        research = LocalResearchBroker(self.path)
        daily = LocalDailyBroker(self.path)
        research.enqueue('r', 's', 'query', 'both', resume=True, job_id='outbox')
        daily.enqueue('d', 'resume')
        self.assertTrue(research.reserve('worker').resume)
        self.assertEqual(daily.reserve('worker').kind, 'resume')
        self.assertIsNone(self.broker.reserve('worker', block_ms=1))

    def test_daily_worker_completes_persisted_local_job(self):
        from api.redis_runs import RedisDailyRunManager, RedisDailyRunWorker
        from scheduler import Scheduler
        scheduler = Scheduler(str(Path(self.temp.name) / 'daily.db'))
        self.addCleanup(scheduler.close)
        broker = LocalDailyBroker(self.path)
        def run(kind, *, run_id, **kwargs):
            scheduler.update_daily_run(run_id, status='completed', results={'selected': []})
            return SimpleNamespace(status='completed')
        manager = RedisDailyRunManager(scheduler, broker)
        started = manager.start('daily')
        worker = RedisDailyRunWorker(SimpleNamespace(run=run), scheduler, broker, consumer='test')
        self.assertTrue(worker.run_once(block_ms=1))
        self.assertEqual(manager.get(started['run_id'])['status'], 'completed')
        events, _ = broker.events(started['run_id'], block_ms=1)
        self.assertTrue(any(event['type'] == 'done' for event in events))
        self.assertEqual(broker.queue_depth(), 0)


class TestSQLiteRunDispatcher(dispatcher_tests.TestRunDispatcher):
    """Run the existing ordering, cancellation and recovery suite on real SQLite."""
    def make_broker(self):
        return LocalBroker(Path(self.tmp.name) / 'queue.db')

    def test_chat_and_research_share_session_exclusion(self):
        from api.redis_runs import RedisResearchRunManager, RedisResearchRunWorker
        broker = LocalResearchBroker(self.broker.path)
        manager = RedisResearchRunManager(self.agent, broker, persistent=False)
        worker = RedisResearchRunWorker(self.agent, broker, consumer='research', claim_idle_ms=1000)
        self.project_stores.extend([manager.projects, worker.project_manager.projects])
        self.manager.start(self.a, 'A1')
        run = manager.start(self.a, 'R')
        self.manager.start(self.b, 'B')
        self.start(workers={'chat': (self.worker, 2), 'research': (worker, 2)})
        self.assertTrue(self.agent.started['A1'].wait(3))
        self.assertTrue(self.agent.started['B'].wait(3))
        self.assertFalse(self.agent.started['R'].is_set())
        self.agent.release['A1'].set()
        self.assertTrue(self.agent.started['R'].wait(3))
        self.agent.release['R'].set()
        self.wait_for(lambda: manager.get(run['run_id'])['status'] == 'completed')


if __name__ == '__main__':
    unittest.main()
