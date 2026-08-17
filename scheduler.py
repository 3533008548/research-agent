"""
📰 每日论文检索调度器

数据库: APP_DATA_DIR/primary/db/daily.db
  searches   (id, keyword, searched_at, results_json, new_count)
  engagement (id, keyword, paper_title, read_yn, date)
  keywords   (id, keyword, active, added_at, skip_streak)

用法:
  s = Scheduler()
  s.add_keyword("TSN scheduling reinforcement learning")
  s.toggle(True)
  results = s.run_today()  # 当天未检索则执行，返回 [(title, url, summary, is_new)]
"""

import os
import sqlite3
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from run_contract import (
    RUN_EVENT_SCHEMA_VERSION,
    daily_event_summary,
    infer_persisted_event_type,
    safe_metadata,
    safe_metrics,
)
from runtime_paths import get_runtime_paths


class Scheduler:
    """每日论文检索调度器"""

    def __init__(self, db_path: str | None = None, request_timeout_seconds: int = 8):
        self.db_path = db_path or str(get_runtime_paths().daily_db)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.request_timeout_seconds = max(3, int(request_timeout_seconds))
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    def close(self):
        """关闭数据库连接（用于应用退出和短生命周期任务）。"""
        self._conn.close()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL,
                active INTEGER DEFAULT 1,
                added_at TEXT NOT NULL,
                skip_streak INTEGER DEFAULT 0,
                hit_count INTEGER DEFAULT 0,
                read_count INTEGER DEFAULT 0,
                search_status TEXT DEFAULT 'idle'  -- idle / searching / done
            );
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                searched_at TEXT NOT NULL,
                results_json TEXT DEFAULT '[]',
                new_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS engagement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT,
                paper_title TEXT,
                status TEXT DEFAULT 'skipped',  -- skipped / want_read / read
                date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_runs (
                run_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                plan_json TEXT NOT NULL DEFAULT '{}',
                source_stats_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '[]',
                critique_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_daily_runs_updated
                ON daily_runs(updated_at DESC);
            CREATE TABLE IF NOT EXISTS daily_candidates (
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                quality_json TEXT NOT NULL DEFAULT '{}',
                rank INTEGER,
                selected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id, candidate_id),
                FOREIGN KEY(run_id) REFERENCES daily_runs(run_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS daily_agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'status',
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL DEFAULT '{}',
                schema_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES daily_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_daily_agent_events_run
                ON daily_agent_events(run_id, id);
        """)
        self._conn.commit()
        # 兼容旧表
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(keywords)")]
        if "search_status" not in cols:
            self._conn.execute("ALTER TABLE keywords ADD COLUMN search_status TEXT DEFAULT 'idle'")
            self._conn.commit()
        daily_event_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(daily_agent_events)")
        }
        for column, definition in (
            ("event_type", "TEXT NOT NULL DEFAULT 'status'"),
            ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in daily_event_columns:
                self._conn.execute(
                    f"ALTER TABLE daily_agent_events ADD COLUMN {column} {definition}"
                )
        self._conn.commit()

    # ── 多 Agent 每日运行记录 ──

    @staticmethod
    def _decode_json(raw: str | None, fallback):
        try:
            value = json.loads(raw or "")
        except (TypeError, json.JSONDecodeError):
            return fallback
        return value

    @classmethod
    def _run_row(cls, row: sqlite3.Row) -> dict:
        item = dict(row)
        for key, fallback in (
            ("keywords_json", []), ("plan_json", {}), ("source_stats_json", {}),
            ("result_json", []), ("critique_json", {}),
        ):
            item[key.removesuffix("_json")] = cls._decode_json(item.pop(key, None), fallback)
        return item

    def create_daily_run(
        self,
        kind: str,
        keywords: list[str],
        plan: dict | None = None,
        *,
        status: str = "running",
    ) -> dict:
        if status not in {"queued", "running", "cancelling"}:
            raise ValueError(f"每日检索运行的初始状态无效: {status}")
        run_id = f"daily-{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT INTO daily_runs (run_id, kind, status, keywords_json, plan_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, kind, status, json.dumps(keywords, ensure_ascii=False),
                    json.dumps(plan or {}, ensure_ascii=False), now, now,
                ),
            )
        return self.get_daily_run(run_id) or {}

    def get_daily_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM daily_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return self._run_row(row) if row else None

    def list_daily_runs(self, limit: int = 20) -> list[dict]:
        """Return daily-run summaries for the operational timeline.

        Callers that render these records must not expose ``keywords`` or result
        payloads: those remain owned by the daily-search UI.
        """
        rows = self._conn.execute(
            "SELECT run_id, kind, status, created_at, updated_at FROM daily_runs "
            "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return [self._run_row(row) for row in rows]

    def daily_run_status_counts(self) -> dict[str, int]:
        """Return only global status aggregates for the Prometheus endpoint."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS count FROM daily_runs GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def get_latest_resumable_run(self, kind: str | None = None) -> dict | None:
        where = "WHERE status IN ('running', 'partial_failed', 'failed', 'cancelled')"
        values: list[str] = []
        if kind:
            where += " AND kind=?"
            values.append(kind)
        row = self._conn.execute(
            f"SELECT * FROM daily_runs {where} ORDER BY updated_at DESC LIMIT 1", values,
        ).fetchone()
        return self._run_row(row) if row else None

    def update_daily_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        source_stats: dict | None = None,
        results: list[dict] | None = None,
        critique: dict | None = None,
        plan: dict | None = None,
        error_text: str | None = None,
    ) -> dict | None:
        values: dict[str, object] = {"run_id": run_id, "updated_at": datetime.now().isoformat()}
        sets = ["updated_at=:updated_at"]
        updates = {
            "status": status,
            "source_stats_json": json.dumps(source_stats, ensure_ascii=False) if source_stats is not None else None,
            "result_json": json.dumps(results, ensure_ascii=False) if results is not None else None,
            "critique_json": json.dumps(critique, ensure_ascii=False) if critique is not None else None,
            "plan_json": json.dumps(plan, ensure_ascii=False) if plan is not None else None,
            "error_text": error_text,
        }
        for column, value in updates.items():
            if value is not None:
                values[column] = value
                sets.append(f"{column}=:{column}")
        with self._conn:
            self._conn.execute(
                f"UPDATE daily_runs SET {', '.join(sets)} WHERE run_id=:run_id", values,
            )
        return self.get_daily_run(run_id)

    def save_daily_candidates(self, run_id: str, candidates: list[dict]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM daily_candidates WHERE run_id=?", (run_id,))
            self._conn.executemany(
                "INSERT INTO daily_candidates (run_id, candidate_id, candidate_json, quality_json, rank, selected) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id, str(candidate["candidate_id"]),
                        json.dumps(candidate, ensure_ascii=False),
                        json.dumps(candidate.get("quality", {}), ensure_ascii=False),
                        candidate.get("rank"), int(bool(candidate.get("selected"))),
                    )
                    for candidate in candidates
                ],
            )

    def get_daily_candidates(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT candidate_json, quality_json, rank, selected FROM daily_candidates "
            "WHERE run_id=? ORDER BY selected DESC, rank ASC, candidate_id ASC", (run_id,),
        ).fetchall()
        candidates = []
        for row in rows:
            item = self._decode_json(row["candidate_json"], {})
            if not isinstance(item, dict):
                continue
            item["quality"] = self._decode_json(row["quality_json"], {})
            item["rank"] = row["rank"]
            item["selected"] = bool(row["selected"])
            candidates.append(item)
        return candidates

    def add_daily_agent_event(
        self,
        run_id: str,
        agent: str,
        status: str,
        message: str,
        details: dict | None = None,
        *,
        event_type: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Append one bounded operational event without retaining task content.

        ``message`` and ``details`` remain parameters for the live UI callback
        compatibility path, but persistence intentionally stores only a fixed
        stage label, aggregate metrics and a small execution fingerprint.
        """
        safe_event_type = infer_persisted_event_type(
            status=str(status or "running"),
            stage=str(agent or "orchestrator"),
            event_type=event_type,
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO daily_agent_events "
                "(run_id, agent, event_type, status, message, details_json, payload_json, schema_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, str(agent or "orchestrator")[:80], safe_event_type, str(status or "running")[:40],
                    daily_event_summary(str(agent), str(status)),
                    json.dumps(safe_metrics(details), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(safe_metadata(metadata), ensure_ascii=False, separators=(",", ":")),
                    RUN_EVENT_SCHEMA_VERSION, datetime.now().isoformat(),
                ),
            )

    def get_daily_agent_events(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, agent, event_type, status, message, details_json, payload_json, schema_version, created_at "
            "FROM daily_agent_events WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            # Older local databases may contain unsanitized legacy values.  Do
            # not re-expose them through this read boundary after migration.
            item["message"] = daily_event_summary(item["agent"], item["status"])
            item["details"] = safe_metrics(self._decode_json(item.pop("details_json", "{}"), {}))
            item["metadata"] = safe_metadata(self._decode_json(item.pop("payload_json", "{}"), {}))
            events.append(item)
        return events

    # ── 关键词管理 ──

    def validate_keyword(self, kw: str) -> Optional[str]:
        """校验关键词，返回 None 表示有效，否则返回错误信息"""
        kw = kw.strip()
        if not kw:
            return "关键词不能为空"
        if len(kw.replace(" ", "").replace("AND", "").replace("OR", "")) < 3:
            return "关键词太短（至少 3 个有效字符）"
        if all(c in " \t\n.,;:!?，。；：！？" for c in kw):
            return "关键词不能全为标点符号"
        return None

    def add_keyword(self, keyword: str) -> str:
        """添加关键词，返回状态消息"""
        err = self.validate_keyword(keyword)
        if err:
            return f"❌ {err}"
        kw = keyword.strip()
        try:
            self._conn.execute(
                "INSERT INTO keywords (keyword, added_at) VALUES (?, ?)",
                (kw, datetime.now().isoformat()),
            )
            self._conn.commit()
            return f"✅ 已添加: {kw}"
        except sqlite3.IntegrityError:
            return f"⚠️ 关键词已存在: {kw}"

    def remove_keyword(self, keyword: str):
        self._conn.execute("DELETE FROM keywords WHERE keyword=?", (keyword,))
        self._conn.commit()

    def list_keywords(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM keywords ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_keywords(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT keyword FROM keywords WHERE active=1"
        ).fetchall()
        return [r["keyword"] for r in rows]

    def toggle_keyword(self, keyword: str, active: bool):
        self._conn.execute(
            "UPDATE keywords SET active=? WHERE keyword=?", (int(active), keyword)
        )
        self._conn.commit()

    # ── 检索与去重 ──

    def searched_today(self, keyword: str) -> bool:
        today = date.today().isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM searches WHERE keyword=? AND searched_at LIKE ?",
            (keyword, f"{today}%"),
        ).fetchone()
        return row is not None

    def _dedup_against_local(self, results: list[dict], paper_store=None) -> tuple[list[dict], list[dict]]:
        """返回 (new, existing)"""
        if not paper_store:
            return results, []
        try:
            local_titles = {p["title"].lower() for p in paper_store.list_papers()}
        except Exception:
            return results, []
        new, existing = [], []
        for r in results:
            title = r.get("title", "").lower()
            if any(title in lt or lt in title for lt in local_titles if len(lt) > 20):
                existing.append(r)
            else:
                new.append(r)
        return new, existing

    def run_today(self, paper_store=None, max_new: int = 5) -> list[dict]:
        """执行今日检索（逐关键词 + 事务 + 断点恢复），返回新论文列表"""
        active = self.get_active_keywords()
        if not active:
            return []

        # 恢复上次中断的 searching 状态 → 重搜
        self._conn.execute("UPDATE keywords SET search_status='idle' WHERE search_status='searching'")
        self._conn.commit()

        all_new = []
        total = len(active)
        for i, kw in enumerate(active):
            if self.searched_today(kw):
                continue
            try:
                # 标记 searching → 搜索 → 标记 done（事务包裹）
                self._conn.execute("BEGIN")
                self._conn.execute("UPDATE keywords SET search_status='searching' WHERE keyword=?", (kw,))
                self._conn.commit()

                results = self._search(kw)
                new, existing = self._dedup_against_local(results, paper_store)
                all_new.extend(new)

                now = datetime.now().isoformat()
                self._conn.execute("BEGIN")
                self._conn.execute(
                    "INSERT INTO searches (keyword, searched_at, results_json, new_count) VALUES (?, ?, ?, ?)",
                    (kw, now, json.dumps([r.get("title", "") for r in new], ensure_ascii=False), len(new)),
                )
                self._conn.execute("UPDATE keywords SET search_status='done', hit_count=hit_count+?, skip_streak=0 WHERE keyword=?",
                                   (len(new), kw)) if new else self._conn.execute(
                    "UPDATE keywords SET search_status='done', skip_streak=skip_streak+1 WHERE keyword=?", (kw,))
                self._conn.commit()

                # 死关键词检测
                row = self._conn.execute("SELECT skip_streak FROM keywords WHERE keyword=?", (kw,)).fetchone()
                if row and row["skip_streak"] >= 3:
                    self._conn.execute("UPDATE keywords SET active=0, search_status='idle' WHERE keyword=?", (kw,))
                    self._conn.commit()
            except Exception:
                # 前面的状态更新可能已经提交；用连接 API 回滚不会因“无事务”再抛异常。
                self._conn.rollback()
                self._conn.execute("UPDATE keywords SET search_status='idle' WHERE keyword=?", (kw,))
                self._conn.commit()

        return all_new[:max_new] if len(all_new) > max_new else all_new

    def prepare_daily_keywords(self, *, retry: bool = False) -> list[str]:
        """为多 Agent 编排器返回本次应处理的关键词，并保留原有“每日一次”语义。"""
        if retry:
            today = date.today().isoformat()
            with self._conn:
                self._conn.execute(
                    "DELETE FROM searches WHERE searched_at LIKE ?", (f"{today}%",)
                )
                self._conn.execute(
                    "UPDATE keywords SET search_status='idle' WHERE active=1"
                )
        return [keyword for keyword in self.get_active_keywords() if not self.searched_today(keyword)]

    def record_daily_keyword_result(self, keyword: str, results: list[dict]) -> None:
        """将编排器已筛选的结果写回旧的每日概览表，兼容 /daily 等现有命令。"""
        now = datetime.now().isoformat()
        payload = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
        with self._conn:
            self._conn.execute(
                "INSERT INTO searches (keyword, searched_at, results_json, new_count) VALUES (?, ?, ?, ?)",
                (keyword, now, payload, len(results)),
            )
            if results:
                self._conn.execute(
                    "UPDATE keywords SET search_status='done', hit_count=hit_count+?, skip_streak=0 WHERE keyword=?",
                    (len(results), keyword),
                )
            else:
                self._conn.execute(
                    "UPDATE keywords SET search_status='done', skip_streak=skip_streak+1 WHERE keyword=?",
                    (keyword,),
                )
            row = self._conn.execute(
                "SELECT skip_streak FROM keywords WHERE keyword=?", (keyword,)
            ).fetchone()
            if row and row["skip_streak"] >= 3:
                self._conn.execute(
                    "UPDATE keywords SET active=0, search_status='idle' WHERE keyword=?", (keyword,)
                )

    def retry_today(self, paper_store=None, max_new: int = 5) -> list[dict]:
        """清除今天的检索记录后重新执行，用于用户主动重试。"""
        today = date.today().isoformat()
        self._conn.execute(
            "DELETE FROM searches WHERE searched_at LIKE ?", (f"{today}%",)
        )
        self._conn.execute(
            "UPDATE keywords SET search_status='idle' WHERE active=1"
        )
        self._conn.commit()
        return self.run_today(paper_store=paper_store, max_new=max_new)

    def search(self, keyword: str, limit: int = 3) -> list[dict]:
        """执行一次不写入每日记录的临时多源检索。"""
        err = self.validate_keyword(keyword)
        if err:
            raise ValueError(err)
        return self._search(keyword.strip(), limit=limit)

    def get_progress(self) -> str:
        """返回当前检索进度"""
        rows = self._conn.execute(
            "SELECT keyword, search_status FROM keywords WHERE active=1"
        ).fetchall()
        done = sum(1 for r in rows if r["search_status"] == "done")
        searching = sum(1 for r in rows if r["search_status"] == "searching")
        total = len(rows)
        if searching:
            return f"📰 搜索中 {done}/{total}"
        if done == total and total > 0:
            return f"📰 已完成 {done}/{total}"
        if done > 0:
            return f"📰 部分完成 {done}/{total}"
        return ""

    def _search(self, keyword: str, limit: int = 3) -> list[dict]:
        """并行查询三源，并限制每个源的网络等待时间。"""
        import urllib.parse
        import xml.etree.ElementTree as ET
        import requests

        def arxiv() -> tuple[str, list[dict]]:
            url = (
                "https://export.arxiv.org/api/query?search_query=all:"
                f"{urllib.parse.quote(keyword)}&start=0&max_results={limit}"
                "&sortBy=relevance&sortOrder=descending"
            )
            resp = requests.get(url, timeout=(3.05, self.request_timeout_seconds))
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            results = []
            for entry in root.findall("a:entry", ns):
                title = entry.find("a:title", ns)
                ttl = title.text.strip().replace("\n", " ") if title is not None else ""
                link = entry.find("a:id", ns)
                url_link = link.text.strip() if link is not None else ""
                if ttl:
                    results.append({"title": ttl, "source": "arxiv", "url": url_link})
            return "arXiv", results

        def openalex() -> tuple[str, list[dict]]:
            resp = requests.get(
                "https://api.openalex.org/works",
                params={
                    "search": keyword, "per_page": limit,
                    "sort": "publication_date:desc",
                    **({"api_key": os.getenv("OPENALEX_API_KEY", "")} if os.getenv("OPENALEX_API_KEY") else {}),
                },
                timeout=(3.05, self.request_timeout_seconds),
            )
            resp.raise_for_status()
            results = []
            for work in resp.json().get("results", []):
                title = work.get("title", "")
                doi = work.get("doi", "")
                if title:
                    results.append({
                        "title": title, "source": "openalex",
                        "url": f"https://doi.org/{doi}" if doi else "",
                    })
            return "OpenAlex", results

        all_results = []
        source_counts = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="daily-search") as executor:
            futures = {
                executor.submit(fn): name
                for name, fn in (("arXiv", arxiv), ("OpenAlex", openalex))
            }
            for future in as_completed(futures):
                source_name = futures[future]
                try:
                    reported_name, results = future.result()
                    all_results.extend(results)
                    source_counts[reported_name] = len(results)
                except Exception as e:
                    source_counts[source_name] = f"失败: {type(e).__name__}"

        # ── 去重 ──
        deduped = []
        for r in all_results:
            t = r["title"].lower()
            if not any(self._title_similar(t, d["title"].lower()) > 0.8 for d in deduped):
                deduped.append(r)
        if deduped:
            deduped[0]["diagnostic"] = f"源统计: {source_counts}"
        return deduped[:limit * 3]

    @staticmethod
    def _title_similar(a: str, b: str) -> float:
        """简单的标题相似度：共同词比例"""
        wa = set(a.split())
        wb = set(b.split())
        if not wa or not wb:
            return 0
        return len(wa & wb) / max(len(wa), len(wb))

    def mark_want_read(self, keyword: str, paper_title: str):
        self._update_status(keyword, paper_title, "want_read")

    def mark_read(self, keyword: str, paper_title: str):
        self._update_status(keyword, paper_title, "read")
        self._conn.execute("UPDATE keywords SET read_count=read_count+1 WHERE keyword=?", (keyword,))
        self._conn.commit()

    def mark_skip(self, keyword: str, paper_title: str):
        self._update_status(keyword, paper_title, "skipped")

    def _update_status(self, keyword: str, paper_title: str, status: str):
        today = date.today().isoformat()
        existing = self._conn.execute(
            "SELECT id FROM engagement WHERE keyword=? AND paper_title=? AND date=?",
            (keyword, paper_title, today),
        ).fetchone()
        if existing:
            self._conn.execute("UPDATE engagement SET status=? WHERE id=?", (status, existing["id"]))
        else:
            self._conn.execute(
                "INSERT INTO engagement (keyword, paper_title, status, date) VALUES (?, ?, ?, ?)",
                (keyword, paper_title, status, today),
            )
        self._conn.commit()

    def get_unread_papers(self, days: int = 3) -> list[dict]:
        """获取最近 N 天标记为 want_read 的论文"""
        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT DISTINCT keyword, paper_title, status FROM engagement "
            "WHERE status='want_read' AND date >= ? ORDER BY date DESC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_today_results(self) -> list[dict]:
        today = date.today().isoformat()
        rows = self._conn.execute(
            "SELECT * FROM searches WHERE searched_at LIKE ? ORDER BY searched_at DESC",
            (f"{today}%",),
        ).fetchall()
        return [dict(r) for r in rows]
