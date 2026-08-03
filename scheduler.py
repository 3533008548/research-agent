"""
📰 每日论文检索调度器

数据库: daily.db
  searches   (id, keyword, searched_at, results_json, new_count)
  engagement (id, keyword, paper_title, read_yn, date)
  keywords   (id, keyword, active, added_at, skip_streak)

用法:
  s = Scheduler()
  s.add_keyword("TSN scheduling reinforcement learning")
  s.toggle(True)
  results = s.run_today()  # 当天未检索则执行，返回 [(title, url, summary, is_new)]
"""

import sqlite3
import json
from datetime import datetime, date
from typing import Optional


class Scheduler:
    """每日论文检索调度器"""

    def __init__(self, db_path: str = "daily.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
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
        """)
        self._conn.commit()
        # 兼容旧表
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(keywords)")]
        if "search_status" not in cols:
            self._conn.execute("ALTER TABLE keywords ADD COLUMN search_status TEXT DEFAULT 'idle'")
            self._conn.commit()

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
                self._conn.execute("ROLLBACK")
                self._conn.execute("UPDATE keywords SET search_status='idle' WHERE keyword=?", (kw,))
                self._conn.commit()

        return all_new[:max_new] if len(all_new) > max_new else all_new

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
        """全量搜索：Semantic Scholar + arXiv + OpenAlex，合并去重"""
        import time
        all_results = []
        source_counts = {}  # 跟踪每个源的返回数

        # ── 1. Semantic Scholar ──
        try:
            from search_api import search_semantic_scholar
            raw = search_semantic_scholar(keyword, limit=limit)
            count = 0
            for line in raw.split("\n"):
                if "**" in line and len(line.strip("-* ")) > 10:
                    all_results.append({"title": line.strip("-* "), "source": "semantic_scholar"})
                    count += 1
            source_counts["SS"] = count
        except Exception as e:
            source_counts["SS"] = f"失败: {e}"

        time.sleep(1)

        # ── 2. arXiv ──
        try:
            import urllib.parse, xml.etree.ElementTree as ET, requests
            url = f"http://export.arxiv.org/api/query?search_query=all:{urllib.parse.quote(keyword)}&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            count = 0
            for entry in root.findall("a:entry", ns):
                title = entry.find("a:title", ns)
                ttl = title.text.strip().replace("\n", " ") if title is not None else ""
                link = entry.find("a:id", ns)
                url_link = link.text.strip() if link is not None else ""
                if ttl:
                    all_results.append({"title": ttl, "source": "arxiv", "url": url_link})
                    count += 1
            source_counts["arXiv"] = count
        except Exception as e:
            source_counts["arXiv"] = f"失败: {e}"

        time.sleep(1)

        # ── 3. OpenAlex ──
        try:
            import requests as req
            resp = req.get("https://api.openalex.org/works",
                           params={"search": keyword, "per_page": limit, "sort": "publication_date:desc"}, timeout=30)
            resp.raise_for_status()
            count = 0
            for w in resp.json().get("results", []):
                ttl = w.get("title", "")
                doi = w.get("doi", "")
                if ttl:
                    all_results.append({"title": ttl, "source": "openalex", "url": f"https://doi.org/{doi}" if doi else ""})
                    count += 1
            source_counts["OpenAlex"] = count
        except Exception as e:
            source_counts["OpenAlex"] = f"失败: {e}"

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
