"""Single-host durable SQLite broker implementing the existing worker contract.

Desktop runs use the same managers, leases, cancellation and dispatcher as the
server. Redis remains the server deployment option; no Redis emulator is used.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from api.redis_runs import (QueueUnavailableError, RedisChatRunBroker,
                            RedisResearchRunBroker, RedisDailyRunBroker)


class LocalBroker:
    kind = 'chat'
    runner_name = 'desktop-worker'
    decode = staticmethod(RedisChatRunBroker._decode_job)

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,payload TEXT NOT NULL,owner TEXT, renewed REAL);
              CREATE INDEX IF NOT EXISTS jobs_kind ON jobs(kind,owner,renewed);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
              CREATE TABLE IF NOT EXISTS cancellations(run_id TEXT PRIMARY KEY,created REAL);
              CREATE TABLE IF NOT EXISTS workers(kind TEXT,owner TEXT,expires REAL,PRIMARY KEY(kind,owner));
              CREATE TABLE IF NOT EXISTS telemetry(id INTEGER PRIMARY KEY,payload TEXT,expires REAL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        except sqlite3.Error as exc:
            raise QueueUnavailableError('本地任务队列暂时不可用') from exc
        finally:
            db.close()

    def ping(self):
        with self.db() as db:
            db.execute('SELECT 1').fetchone()

    def put(self, fields):
        with self.db() as db:
            return str(db.execute('INSERT INTO jobs(kind,payload) VALUES(?,?)',
                                 (self.kind, json.dumps(fields))).lastrowid)

    def enqueue(self, run_id, session_id, message):
        return self.put(dict(run_id=run_id, session_id=session_id, message=message))

    def _take(self, consumer, stale=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            where, args = ('owner IS NULL', ()) if stale is None else ('owner IS NOT NULL AND renewed<?', (time.time()-stale/1000,))
            row = db.execute(f'SELECT * FROM jobs WHERE kind=? AND {where} ORDER BY id LIMIT 1', (self.kind, *args)).fetchone()
            if not row:
                return None
            db.execute('UPDATE jobs SET owner=?,renewed=? WHERE id=?', (consumer, time.time(), row['id']))
            return self.decode(str(row['id']), json.loads(row['payload']))

    def reserve(self, consumer, *, block_ms=1000):
        deadline = time.monotonic() + max(0, block_ms) / 1000
        while True:
            result = self._take(consumer)
            if result or time.monotonic() >= deadline:
                return result
            time.sleep(min(.025, max(0, deadline-time.monotonic())))

    def claim_stale(self, consumer, *, min_idle_ms=120000):
        return self._take(consumer, min_idle_ms)

    def recover_abandoned(self):
        """Only called at startup AFTER acquiring the exclusive worker file lock."""
        with self.db() as db:
            db.execute('UPDATE jobs SET owner=NULL,renewed=NULL WHERE kind=?', (self.kind,))
            db.execute('DELETE FROM workers WHERE kind=?', (self.kind,))

    def renew(self, consumer, message_ids):
        owned = set()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for identifier in message_ids:
                if db.execute('UPDATE jobs SET renewed=? WHERE id=? AND kind=? AND owner=?',
                              (time.time(), identifier, self.kind, consumer)).rowcount:
                    owned.add(str(identifier))
        return owned

    def acknowledge_owned(self, consumer, message_id):
        with self.db() as db:
            return bool(db.execute('DELETE FROM jobs WHERE id=? AND kind=? AND owner=?',
                                   (message_id, self.kind, consumer)).rowcount)

    def acknowledge(self, message_id):
        with self.db() as db:
            db.execute('DELETE FROM jobs WHERE id=? AND kind=?', (message_id, self.kind))

    def publish(self, run_id, event):
        with self.db() as db:
            identifier = db.execute('INSERT INTO events(run_id,payload,created) VALUES(?,?,?)',
                                    (run_id, json.dumps(event), time.time())).lastrowid
            # Same bounded event history as the Redis deployment.
            db.execute('DELETE FROM events WHERE run_id=? AND id NOT IN (SELECT id FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1000)', (run_id, run_id))
        return f'{identifier}-0'

    def events(self, run_id, cursor='0-0', *, block_ms=1000):
        since = int(str(cursor).split('-')[0])
        deadline = time.monotonic() + max(0, block_ms) / 1000
        while True:
            with self.db() as db:
                rows = db.execute('SELECT id,payload FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 100', (run_id, since)).fetchall()
            if rows:
                return [json.loads(r['payload']) for r in rows], f"{rows[-1]['id']}-0"
            if time.monotonic() >= deadline:
                return [], cursor
            time.sleep(.05)

    def request_cancel(self, run_id):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO cancellations VALUES(?,?)', (run_id, time.time()))

    def cancel_requested(self, run_id):
        with self.db() as db:
            return bool(db.execute('SELECT 1 FROM cancellations WHERE run_id=?', (run_id,)).fetchone())

    def clear_cancel(self, run_id):
        with self.db() as db:
            db.execute('DELETE FROM cancellations WHERE run_id=?', (run_id,))

    def heartbeat(self, consumer, *, ttl_seconds=15):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO workers VALUES(?,?,?)', (self.kind, consumer, time.time()+ttl_seconds))
            db.execute('DELETE FROM events WHERE created<?', (time.time()-86400,))
            db.execute('DELETE FROM cancellations WHERE created<? AND run_id NOT IN (SELECT json_extract(payload,\'$.run_id\') FROM jobs)', (time.time()-86400,))

    def queue_depth(self):
        with self.db() as db:
            return db.execute('SELECT COUNT(*) FROM jobs WHERE kind=?', (self.kind,)).fetchone()[0]

    def live_worker_count(self):
        with self.db() as db:
            return db.execute('SELECT COUNT(*) FROM workers WHERE kind=? AND expires>?', (self.kind, time.time())).fetchone()[0]

    def publish_retrieval_metrics(self):
        from papers.metrics import snapshot
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO telemetry VALUES(1,?,?)', (json.dumps(snapshot()), time.time()+60))

    def load_retrieval_metrics(self):
        with self.db() as db:
            row = db.execute('SELECT payload FROM telemetry WHERE id=1 AND expires>?', (time.time(),)).fetchone()
            return json.loads(row[0]) if row else None


class LocalResearchBroker(LocalBroker):
    kind = 'research'
    decode = staticmethod(RedisResearchRunBroker._decode_job)

    def enqueue(self, run_id, session_id, query, scope, *, resume=False, job_id=''):
        return self.put(dict(run_id=run_id, session_id=session_id, query=query, scope=scope,
                             resume='1' if resume else '0', job_id=job_id))


class LocalDailyBroker(LocalBroker):
    kind = 'daily'
    decode = staticmethod(RedisDailyRunBroker._decode_job)

    def enqueue(self, run_id, kind):
        return self.put(dict(run_id=run_id, kind=kind))
