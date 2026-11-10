"""Tiny, privacy-conscious event log in SQLite.

Stores only: time, event name, internal book id, internal passage id, demo flag.
No IP addresses, no user agents, no accounts, no cookies of its own.
If anything goes wrong the site keeps working and the event is simply dropped.
"""
import atexit
import logging
import queue
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("literary_roulette.analytics")
_writer = None  # (database path, queue, thread) once start_writer() has been called

EVENTS = (
    "passage_displayed", "another_passage_requested", "book_revealed", "book_hidden",
    "another_book_requested", "read_link_clicked", "borrow_link_clicked", "buy_link_clicked",
    "book_saved", "awareness_known", "awareness_unknown", "awareness_unsure",
)


def init_db(path):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path), timeout=2)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, "
                "book_id TEXT, passage_id TEXT, is_demo INTEGER NOT NULL DEFAULT 0)")
        return True
    except (sqlite3.Error, OSError) as exc:
        log.warning("Analytics unavailable: %s", exc)
        return False


def _drain(path, q):
    """Background writer: one connection, batched commits, so a click never waits on the disk."""
    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except (sqlite3.Error, OSError) as exc:
        log.warning("Analytics writer could not open the database: %s", exc)
    while True:
        item = q.get()
        batch = [item]
        while len(batch) < 200:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        stop = None in batch
        rows = [b for b in batch if b is not None]
        try:
            if conn is not None and rows:
                with conn:
                    conn.executemany(
                        "INSERT INTO events (ts, event, book_id, passage_id, is_demo) VALUES (?,?,?,?,?)", rows)
        except sqlite3.Error as exc:
            log.warning("Could not record %d analytics event(s): %s", len(rows), exc)
        for _ in batch:
            q.task_done()
        if stop:
            break
    if conn is not None:
        conn.close()


def start_writer(path):
    """Record events on a background thread. Without this, log_event() writes immediately."""
    global _writer
    stop_writer()
    q = queue.Queue(maxsize=5000)
    thread = threading.Thread(target=_drain, args=(path, q), daemon=True, name="lr-analytics")
    thread.start()
    _writer = (str(path), q, thread)


def flush():
    if _writer:
        _writer[1].join()


def stop_writer():
    global _writer
    if _writer:
        _, q, thread = _writer
        _writer = None
        q.put(None)
        thread.join(timeout=5)


atexit.register(stop_writer)


def log_event(path, event, book_id=None, passage_id=None, is_demo=False):
    """Returns True if saved (or queued). Never raises."""
    if event not in EVENTS:
        return False
    if _writer and _writer[0] == str(path):
        row = (datetime.now(timezone.utc).isoformat(timespec="seconds"), event,
               book_id, passage_id, 1 if is_demo else 0)
        try:
            _writer[1].put_nowait(row)
            return True
        except queue.Full:
            return False
    try:
        with closing(sqlite3.connect(str(path), timeout=2)) as conn, conn:
            conn.execute(
                "INSERT INTO events (ts, event, book_id, passage_id, is_demo) VALUES (?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), event,
                 book_id, passage_id, 1 if is_demo else 0))
        return True
    except (sqlite3.Error, OSError) as exc:
        log.warning("Could not record event %s: %s", event, exc)
        return False


def report(path):
    """Aggregate counts: {'totals': {event: n}, 'by_book': {book_id: {event: n}}}."""
    totals, by_book = {}, {}
    with closing(sqlite3.connect(str(path), timeout=2)) as conn:
        for event, book_id, n in conn.execute(
                "SELECT event, COALESCE(book_id,'(none)'), COUNT(*) FROM events GROUP BY 1, 2"):
            totals[event] = totals.get(event, 0) + n
            by_book.setdefault(book_id, {})[event] = n
    return {"totals": totals, "by_book": by_book}
