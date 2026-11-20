"""Shared job state: one small SQLite file (WAL) that every web process and the worker can read and write.

Visitors poll the progress of a cold book; with several web processes the poll may land on a different process than the
one doing the work, so progress cannot live in one process's memory. The interface is deliberately tiny (upsert / view /
pending_cold / prune) so Redis or a database can replace this file later without touching the callers.
"""
import sqlite3
import threading
import time
from pathlib import Path


class JobStore:
    def __init__(self, path, clock=time.time):
        self.path, self._clock = Path(path), clock
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, language TEXT NOT NULL, token TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                stage TEXT, percent REAL, detail TEXT, error TEXT, prio INTEGER, queued_at REAL, updated REAL, moved REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS jobs_token ON jobs (language, token)")
            c.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs (state)")

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def upsert(self, key, language, token, kind, state, stage, percent, detail="", error=None, prio=5, queued_at=None, moved=None):
        now = self._clock()
        self._conn().execute(
            """INSERT INTO jobs (key, language, token, kind, state, stage, percent, detail, error, prio, queued_at, updated, moved)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET state=excluded.state, stage=excluded.stage, percent=excluded.percent,
                 detail=excluded.detail, error=excluded.error, prio=excluded.prio, updated=excluded.updated,
                 moved=COALESCE(excluded.moved, jobs.moved), queued_at=COALESCE(jobs.queued_at, excluded.queued_at)""",
            (key, language, token, kind, state, stage, percent, detail, error, prio, queued_at or now, now, moved or now))

    def view(self, language, token):
        """The newest job for this book token as a dict (with `ahead`), or None."""
        c = self._conn()
        row = c.execute("""SELECT key, state, stage, percent, detail, error, prio, queued_at, moved, kind FROM jobs
                           WHERE language=? AND token=? ORDER BY updated DESC LIMIT 1""", (language, token)).fetchone()
        if not row:
            return None
        key, state, stage, percent, detail, error, prio, queued_at, moved, kind = row
        ahead = 0
        if state == "queued":
            ahead = c.execute("""SELECT COUNT(*) FROM jobs WHERE kind='cold' AND key != ? AND (state='running' OR
                                 (state='queued' AND (prio < ? OR (prio = ? AND queued_at < ?))))""", (key, prio, prio, queued_at)).fetchone()[0]
        return {"key": key, "state": state, "stage": stage, "percent": percent, "detail": detail or "", "error": error,
                "moved": moved or 0, "ahead": ahead, "kind": kind}

    def pending_cold(self):
        return self._conn().execute("SELECT COUNT(*) FROM jobs WHERE kind='cold' AND state IN ('queued','running')").fetchone()[0]

    def prune(self, finished_after=600.0, abandoned_after=900.0):
        """Keep the table small: forget finished jobs, and fail jobs whose process disappeared."""
        now, c = self._clock(), self._conn()
        c.execute("DELETE FROM jobs WHERE state IN ('done','failed') AND updated < ?", (now - finished_after,))
        c.execute("UPDATE jobs SET state='failed', error='The worker stopped before finishing.', updated=? WHERE state IN ('queued','running') AND updated < ?",
                  (now, now - abandoned_after))
