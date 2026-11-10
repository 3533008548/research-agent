"""Durable research questions, work queue and budget ledger.

SQLite is the authority; Redis only delivers wakeups. Transactions never include
model or tool calls. Records deliberately have no ``thread_id`` column: deleting
a conversation must not delete the research it originally launched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ResearchBudgetExceeded(RuntimeError):
    pass


class ResearchProjects:
    def __init__(self, db_path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS research_projects (
                project_id TEXT PRIMARY KEY, origin_session_id TEXT NOT NULL,
                document TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_jobs (
                job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER NOT NULL,
                role TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
                sent_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS research_jobs_project ON research_jobs(project_id, state);
            CREATE TABLE IF NOT EXISTS research_attempts (
                attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
                version INTEGER NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, completed_at TEXT);
            CREATE TABLE IF NOT EXISTS research_events (
                event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_reservations (
                reservation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
                tokens INTEGER NOT NULL, actual INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_reports (
                report_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER NOT NULL,
                snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _get(self, project_id):
        row = self.conn.execute("SELECT document FROM research_projects WHERE project_id=?", (project_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, project_id):
        with self._lock:
            return self._get(project_id)

    def list(self, session_id=None):
        with self._lock:
            sql = "SELECT document FROM research_projects"
            args = ()
            if session_id:
                sql += " WHERE origin_session_id=?"
                args = (session_id,)
            return [json.loads(row[0]) for row in self.conn.execute(sql + " ORDER BY updated_at DESC LIMIT 100", args)]

    def _save(self, project):
        project["updated_at"] = now()
        self.conn.execute("UPDATE research_projects SET document=?, updated_at=? WHERE project_id=?",
                          (json.dumps(project, ensure_ascii=False), project["updated_at"], project["project_id"]))

    def _event(self, project_id, kind, payload, event_id=None):
        return self.conn.execute("INSERT OR IGNORE INTO research_events VALUES (?,?,?,?,?)",
                                 (project_id + ":" + (event_id or uuid.uuid4().hex), project_id, kind,
                                  json.dumps(payload, ensure_ascii=False), now())).rowcount != 0

    def _queue(self, project, role, payload):
        job_id = "rjob-" + uuid.uuid4().hex
        self.conn.execute("INSERT INTO research_jobs(job_id,project_id,version,role,state,payload,created_at) VALUES (?,?,?,?,?,?,?)",
                          (job_id, project["project_id"], project["goal_version"], role, "queued",
                           json.dumps(payload, ensure_ascii=False), now()))
        return job_id

    def create(self, project_id, session_id, question, scope="both", *, max_calls=48, max_tokens=400000):
        with self.transaction():
            existing = self._get(project_id)
            if existing:
                return existing
            project = dict(project_id=project_id, origin_session_id=session_id, question=question,
                           scope=scope, goal_version=1, execution_state="active", end_reason="",
                           criteria=[], constraints=[], claims=[], gaps=[], evidence=[], decisions=[],
                           report="", critique={}, waiting_for=[], task_history=[], no_progress=0,
                           report_version=0, claims_version=0,
                           delivery_status="pending", partial_report=None, last_failure=None,
                           budget=dict(max_calls=max_calls, max_tokens=max_tokens, calls=0, tokens=0,
                                       report_reserve_calls=3, report_reserve_tokens=min(96000, max_tokens // 4)),
                           created_at=now(), updated_at=now())
            self.conn.execute("INSERT INTO research_projects VALUES (?,?,?,?)",
                              (project_id, session_id, json.dumps(project, ensure_ascii=False), project["updated_at"]))
            self._event(project_id, "created", {"question": question})
            self._queue(project, "lead", {})
            return project

    def jobs(self, project_id):
        with self._lock:
            return [self._job(row) for row in self.conn.execute("SELECT * FROM research_jobs WHERE project_id=? ORDER BY created_at", (project_id,))]

    def reports(self, project_id):
        with self._lock:
            return [dict(row) for row in self.conn.execute(
                "SELECT report_id,version,created_at FROM research_reports WHERE project_id=? ORDER BY created_at DESC", (project_id,))]

    def report(self, project_id, report_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM research_reports WHERE project_id=? AND report_id=?", (project_id, report_id)).fetchone()
            return {**dict(row), "snapshot": json.loads(row["snapshot"])} if row else None

    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        job["payload"] = json.loads(job["payload"])
        job["result"] = json.loads(job["result"])
        return job

    def outbox(self):
        with self._lock:
            return [self._job(row) for row in self.conn.execute(
                "SELECT * FROM research_jobs WHERE state='queued' AND sent_at<? ORDER BY created_at LIMIT 100", (time.time() - 60,))]

    def mark_sent(self, job_id):
        with self.transaction():
            self.conn.execute("UPDATE research_jobs SET sent_at=? WHERE job_id=?", (time.time(), job_id))

    def claim(self, job_id):
        """Called under the dispatcher's per-job ownership lock, including replay.

        A crash may leave an attempt running. Its reservations remain charged;
        replay gets a new attempt and can only repeat read-only research tools.
        """
        with self.transaction():
            job = self._job(self.conn.execute("SELECT * FROM research_jobs WHERE job_id=?", (job_id,)).fetchone())
            if not job or job["state"] not in {"queued", "running"}:
                return None
            project = self._get(job["project_id"])
            if project["execution_state"] != "active" or project["goal_version"] != job["version"]:
                self.conn.execute("UPDATE research_jobs SET state='stale' WHERE job_id=?", (job_id,))
                return None
            if job["attempts"] >= 2:
                self.conn.execute("UPDATE research_attempts SET state='interrupted',completed_at=? WHERE job_id=? AND state='running'",
                                  (now(), job_id))
                self._finish(project, job, {"error": "attempt_limit", "evidence": []})
                return None
            job["attempts"] += 1
            job["attempt_id"] = f"{job_id}:{job['attempts']}"
            job["state"] = "running"
            self.conn.execute("UPDATE research_attempts SET state='interrupted',completed_at=? WHERE job_id=? AND state='running'",
                              (now(), job_id))
            self.conn.execute("INSERT INTO research_attempts(attempt_id,project_id,job_id,version,state,created_at) VALUES (?,?,?,?,?,?)",
                              (job["attempt_id"], job["project_id"], job_id, job["version"], "running", now()))
            self.conn.execute("UPDATE research_jobs SET state='running',attempts=? WHERE job_id=?", (job["attempts"], job_id))
            self._event(job["project_id"], "attempt_started", {"job_id": job_id, "attempt": job["attempts"]})
            return job

    def active(self, project_id, version):
        project = self.get(project_id)
        return bool(project and project["execution_state"] == "active" and project["goal_version"] == version)

    def reserve(self, job, tokens):
        with self.transaction():
            project = self._get(job["project_id"])
            if project["execution_state"] != "active" or project["goal_version"] != job["version"]:
                from cancellation import RequestCancelledError
                raise RequestCancelledError("研究已暂停或目标已更新")
            budget = project["budget"]
            reporting = job["role"] in {"report", "critic"} or bool(job["payload"].get("review"))
            calls_cap = budget["max_calls"] - (0 if reporting else budget["report_reserve_calls"])
            tokens_cap = budget["max_tokens"] - (0 if reporting else budget["report_reserve_tokens"])
            if budget["calls"] + 1 > calls_cap or budget["tokens"] + tokens > tokens_cap:
                raise ResearchBudgetExceeded("研究总预算不足")
            reservation_id = uuid.uuid4().hex
            self.conn.execute("INSERT INTO research_reservations VALUES (?,?,?,?,NULL,?)",
                              (reservation_id, job["project_id"], job["job_id"], tokens, now()))
            budget["calls"] += 1
            budget["tokens"] += tokens
            self._save(project)
            return reservation_id

    def settle(self, reservation_id, actual):
        with self.transaction():
            row = self.conn.execute("SELECT * FROM research_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if not row or row["actual"] is not None:
                return
            project = self._get(row["project_id"])
            # Unknown remote usage stays conservatively charged, even after restart.
            if type(actual) is not int or actual < 0:
                return  # Keep unknown usage distinguishable from provider usage.
            project["budget"]["tokens"] += actual - row["tokens"]
            self.conn.execute("UPDATE research_reservations SET actual=? WHERE reservation_id=?", (actual, reservation_id))
            self._save(project)

    def complete(self, job, result):
        with self.transaction():
            current = self._job(self.conn.execute("SELECT * FROM research_jobs WHERE job_id=?", (job["job_id"],)).fetchone())
            if not current or current["state"] != "running" or current["attempts"] != job["attempts"]:
                return
            project = self._get(job["project_id"])
            stale = project["goal_version"] != job["version"] or project["execution_state"] != "active"
            self.conn.execute("UPDATE research_attempts SET state=?,result=?,completed_at=? WHERE attempt_id=?",
                ("stale" if stale else "failed" if result.get("error") else "completed",
                 json.dumps(result, ensure_ascii=False), now(), job.get("attempt_id", f"{job['job_id']}:{job['attempts']}")))
            if project["goal_version"] != job["version"] or project["execution_state"] != "active":
                self.conn.execute("UPDATE research_jobs SET state='stale',result=? WHERE job_id=?",
                                  (json.dumps(result, ensure_ascii=False), job["job_id"]))
                self._event(job["project_id"], "late_result", {"job_id": job["job_id"]})
                return
            self._finish(project, job, result)

    def _finish(self, project, job, result):
        self.conn.execute("UPDATE research_jobs SET state=?,result=? WHERE job_id=?",
                          ("failed" if result.get("error") else "done", json.dumps(result, ensure_ascii=False), job["job_id"]))
        self._event(job["project_id"], "result", {"job_id": job["job_id"], "role": job["role"]})
        added = self._merge_evidence(project, job, result) if job["role"].startswith("researcher") else 0
        if result.get("error"):
            project["decisions"].append({"version": job["version"], "error": result["error"], "role": job["role"]})
            project["last_failure"] = {"version": job["version"], "role": job["role"],
                "error": result["error"], "error_details": result.get("error_details", {})}
            if job["role"] == "report" and result.get("partial_answer"):
                project["partial_report"] = {"version": job["version"], "text": result["partial_answer"],
                                             "error": result["error"]}
            retryable = result["error"] in {"LLMRequestTimeoutError", "LLMQueueFullError",
                "empty_response", "output_truncated", "invalid_response", "invalid_decision"}
            details = result.get("error_details") or {}
            if result["error"] == "LLMRequestFailedError":
                retryable = details.get("http_status") in {429, 500, 502, 503, 504} or details.get("code") == "LLM_CONNECTION_ERROR"
            budget = project["budget"]
            if retryable and job["attempts"] < 2 and budget["calls"] < budget["max_calls"] and budget["tokens"] < budget["max_tokens"]:
                # Replay the same logical job once. Attempts and reservations
                # remain durable; Redis duplicates cannot reset this limit.
                self.conn.execute("UPDATE research_jobs SET state='queued',sent_at=0 WHERE job_id=?", (job["job_id"],))
                self._event(job["project_id"], "retry_scheduled", {"job_id": job["job_id"], "error": result["error"]})
                self._save(project)
                return
            if result["error"] == "budget":
                project["end_reason"] = "budget"
            if job["role"] == "lead" and result["error"] == "budget" and not project["critique"]:
                self._queue(project, "report", {"reason": "budget"})
                self._save(project)
                return
            if job["role"] == "lead" and project["evidence"] and project["report_version"] != job["version"]:
                self._queue(project, "report", {"reason": "lead_failed"})
                self._save(project)
                return
            if job["role"] in {"lead", "report", "critic"}:
                project["execution_state"] = "ended"
                if result["error"] == "budget":
                    project["execution_state"] = "budget_exhausted"
                project["end_reason"] = "budget" if result["error"] == "budget" else "unverified"
                if not project["report"]:
                    project["report"] = "研究尚未形成可核验结论。已保留证据和任务记录。"
                    project["report_version"] = job["version"]
                    project["delivery_status"] = "incomplete"
                self._snapshot(project)
                self._save(project)
                return
        if job["role"].startswith("researcher"):
            project["no_progress"] = 0 if added else project["no_progress"] + 1
            project["task_history"].append({"job_id": job["job_id"], **job["payload"],
                "new_evidence": added, "finding": result.get("answer", "")[:6000], "error": result.get("error", "")})
            pending = self.conn.execute("SELECT 1 FROM research_jobs WHERE project_id=? AND version=? AND state IN ('queued','running') AND role LIKE 'researcher%'",
                                        (job["project_id"], job["version"])).fetchone()
            if not pending:
                self._queue(project, "lead", {})
        elif job["role"] == "lead":
            self._decision(project, result)
        elif job["role"] == "report":
            project["report"] = result.get("answer", "")
            project["report_version"] = job["version"]
            project["delivery_status"] = "draft"
            project["partial_report"] = None
            project["critique"] = {}
            self._queue(project, "critic", {"report": project["report"]})
        elif job["role"] == "critic":
            project["critique"] = result
            project["delivery_status"] = "reviewed" if result.get("verdict") == "pass" else "needs_revision"
            self._snapshot(project)
            project["decisions"].append({"version": job["version"], "review": result})
            # The critic cannot dispatch work; the lead must justify any next action.
            self._queue(project, "lead", {"review": True})
        self._save(project)

    @staticmethod
    def _merge_evidence(project, job, result):
        added = 0
        for card in result.get("evidence", []):
            stable_id = "ev-" + fingerprint([card.get("source"), card.get("excerpt"), card.get("source_anchor")])[:24]
            if any(item["stable_id"] == stable_id for item in project["evidence"]):
                continue
            added += 1
            evidence_id = f"E{len(project['evidence']) + 1}"
            stored = {**card, "stable_id": stable_id, "id": evidence_id, "goal_version": job["version"],
                "job_id": job["job_id"], "content_hash": fingerprint(card.get("excerpt", "")),
                "source_version": card.get("source_version", "unknown")}
            if isinstance(card.get("evidence_unit"), dict):
                stored["evidence_unit"] = {**card["evidence_unit"], "id": stable_id, "evidence_id": evidence_id}
            project["evidence"].append(stored)
        return added

    def _decision(self, project, decision):
        project["decisions"].append({"version": project["goal_version"], **decision})
        if project["end_reason"] == "budget" and project["report"] and project["critique"]:
            project["execution_state"] = "budget_exhausted"
            return
        for name in ("claims", "gaps"):
            if isinstance(decision.get(name), list):
                project[name] = decision[name]
                if name == "claims":
                    project["claims_version"] = project["goal_version"]
        if not project["criteria"] and isinstance(decision.get("criteria"), list):
            project["criteria"] = decision["criteria"]
        action = decision.get("action")
        if action == "research" and project["no_progress"] < 4 and project["end_reason"] != "budget":
            tasks = decision.get("tasks", [])[:2]
            queued = 0
            batch_keys = set()
            for task in tasks:
                if not isinstance(task, dict) or not all(task.get(key) for key in ("gap", "question", "expected", "difference")):
                    continue
                key = fingerprint([task["gap"], task["question"], task["expected"]])
                if key in batch_keys or key in [entry.get("key") for entry in project["task_history"]]:
                    continue
                # Dependent work is proposed again after its prerequisite has evidence.
                dependencies = task.get("depends_on", [])
                done = {entry["job_id"] for entry in project["task_history"] if not entry.get("error")}
                if not isinstance(dependencies, list) or any(dep not in done for dep in dependencies):
                    continue
                self._queue(project, f"researcher{queued + 1}", {**task, "key": key})
                batch_keys.add(key)
                queued += 1
            if queued:
                return
            project["end_reason"] = "no_feasible_path"
        elif action == "wait" and isinstance(decision.get("waiting_for"), list) and decision["waiting_for"]:
            project["execution_state"] = "waiting"
            project["waiting_for"] = [str(item)[:500] for item in decision["waiting_for"]][:10]
            return
        elif action == "ask" and decision.get("question"):
            project["execution_state"] = "needs_input"
            project["waiting_for"] = [str(decision["question"])[:2000]]
            return
        elif action == "finish" and project["report"] and project["critique"]:
            project["execution_state"] = "ended"
            passed = project["critique"].get("verdict") == "pass" and bool(project["evidence"])
            project["end_reason"] = project["end_reason"] or ("fulfilled" if passed and decision.get("criteria_met") is True else "insufficient_evidence")
            if project["end_reason"] == "budget":
                project["execution_state"] = "budget_exhausted"
            return
        if project["no_progress"] >= 4:
            project["end_reason"] = "no_progress"
        # Every proposed delivery is reviewed, including budget/no-progress exits.
        self._queue(project, "report", {"reason": project["end_reason"] or decision.get("reason", "")})

    def _snapshot(self, project):
        self.conn.execute("INSERT INTO research_reports VALUES (?,?,?,?,?)",
                          (uuid.uuid4().hex, project["project_id"], project["goal_version"], json.dumps({
                              "report": project["report"], "critique": project["critique"],
                              "evidence": project["evidence"], "claims": project["claims"],
                              "criteria": project["criteria"], "question": project["question"],
                              "constraints": project["constraints"], "report_version": project["report_version"],
                              "delivery_status": project.get("delivery_status"),
                              "partial_report": project.get("partial_report")}, ensure_ascii=False), now()))

    def control(self, project_id, action, *, message="", event_id=None):
        with self.transaction():
            project = self._get(project_id)
            if not project:
                raise KeyError(project_id)
            if action not in {"pause", "stop", "resume", "steer", "material"}:
                raise ValueError("invalid research action")
            if action in {"steer", "material"} and not message.strip():
                raise ValueError("message is required")
            if not self._event(project_id, action, {"message": message}, event_id):
                return project
            if action in {"pause", "stop"}:
                project["execution_state"] = "paused" if action == "pause" else "stopped"
                project["end_reason"] = "user_stop" if action == "stop" else ""
                self.conn.execute("UPDATE research_jobs SET state='stale' WHERE project_id=? AND state='queued'", (project_id,))
            elif action == "material":
                project.setdefault("materials", []).append(message)
                # Materials do not revive a user stop or a completed question.
                if project["execution_state"] == "waiting" and message in project["waiting_for"]:
                    project["execution_state"] = "active"
                    project["waiting_for"] = []
                    self._queue(project, "lead", {})
            else:
                if action == "steer":
                    project["constraints"].append(message)
                    project["decisions"].append({"version": project["goal_version"], "previous_criteria": project["criteria"]})
                    project["criteria"] = []  # Only a user goal change may reset acceptance criteria.
                if action == "resume" or project["execution_state"] not in {"paused", "stopped", "ended"}:
                    project["execution_state"] = "active"
                if action == "resume":
                    project["end_reason"] = ""
                project["goal_version"] += 1
                project["critique"] = {}
                project["waiting_for"] = []
                project["no_progress"] = 0
                self.conn.execute("UPDATE research_jobs SET state='stale' WHERE project_id=? AND state='queued'", (project_id,))
                if project["execution_state"] == "active":
                    self._queue(project, "lead", {})
            self._save(project)
            return project

    def close(self):
        self.conn.close()
