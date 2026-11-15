"""The shared library of prepared books, indexed in one SQLite file (WAL) so it stays light however large it grows.

Per language folder, per book:
  <id>.<hash8>.<segver>.txt   all passages back to back (UTF-8)
  <id>.<hash8>.<segver>.idx   passage byte offsets (unsigned 32-bit array)
and one row in library.sqlite, written LAST in a single transaction: that row is the "ready" marker, so a half-written
book is never served. Nothing is held in memory but a small cache of recently read offset tables; start-up reads no
book files at all. Several processes (web workers, the background worker) can share one folder safely.
"""
import json
import os
import re
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from pathlib import Path

SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
INTEREST_WEIGHTS = {"displayed": 1.0, "another": 2.0, "reveal": 2.0, "saved": 5.0, "link": 5.0}
OFFSET_CACHE = 256
COLUMNS = "book_id, file, bytes, passages, content_hash, segment_version, edition_key, prepared_at, external, book"


def safe_name(book_id):
    return SAFE_CHARS.sub("_", book_id)


class ReadyShelf:
    def __init__(self, root, languages=("en", "fr", "zh"), scan_interval=0.0):
        self.root, self.languages = Path(root), tuple(languages)
        self.root.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._cache = OrderedDict()                     # idx file -> offsets array (small LRU)
        self._cache_lock = threading.Lock()
        self._ids, self._ids_version = {}, {}
        self._interest = {}                             # (lang, book_id) -> [score, last_seen]
        self._seen_version = -1
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS books (lang TEXT NOT NULL, book_id TEXT NOT NULL, file TEXT NOT NULL,
                bytes INTEGER, passages INTEGER, content_hash TEXT, segment_version TEXT, edition_key TEXT, prepared_at REAL,
                external INTEGER, book TEXT, PRIMARY KEY (lang, book_id))""")
            c.execute("CREATE INDEX IF NOT EXISTS books_age ON books (lang, external, prepared_at)")
            c.execute("CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY CHECK (id = 1), v INTEGER)")
            c.execute("INSERT OR IGNORE INTO counter VALUES (1, 0)")

    # ---- plumbing ----------------------------------------------------------------------------
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.root / "library.sqlite", timeout=10, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _folder(self, language):
        return self.root / language

    @property
    def version(self):
        """Changes whenever any process publishes or retires a book (cheap: one tiny SELECT)."""
        return self._conn().execute("SELECT v FROM counter WHERE id=1").fetchone()[0]

    def refresh_if_changed(self):
        """True when another process changed the library since this was last called."""
        v = self.version
        changed = v != self._seen_version
        self._seen_version = v
        return changed

    def scan(self):
        return self.refresh_if_changed()

    @staticmethod
    def _meta(language, row):
        book_id, file, size, passages, chash, segver, edition, prepared, external, book = row
        return {"book_id": book_id, "language": language, "file": file, "bytes": size, "passages": passages,
                "content_hash": chash, "segment_version": segver, "edition_key": edition, "prepared_at": prepared,
                "external": bool(external), "book": json.loads(book) if book else None}

    # ---- reading -----------------------------------------------------------------------------
    def ids(self, language):
        v = self.version
        if self._ids_version.get(language) != v:
            rows = self._conn().execute("SELECT book_id FROM books WHERE lang=? ORDER BY book_id", (language,)).fetchall()
            self._ids[language], self._ids_version[language] = tuple(r[0] for r in rows), v
        return self._ids[language]

    def external_ids(self, language):
        return [r[0] for r in self._conn().execute("SELECT book_id FROM books WHERE lang=? AND external=1", (language,))]

    def count(self, language):
        return self._conn().execute("SELECT COUNT(*) FROM books WHERE lang=?", (language,)).fetchone()[0]

    def has(self, language, book_id):
        return self._conn().execute("SELECT 1 FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone() is not None

    def meta(self, language, book_id):
        row = self._conn().execute(f"SELECT {COLUMNS} FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        return self._meta(language, row) if row else None

    def passage_count(self, language, book_id):
        row = self._conn().execute("SELECT passages FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        return row[0] if row else 0

    def _offsets(self, path):
        with self._cache_lock:
            hit = self._cache.get(path)
            if hit is not None:
                self._cache.move_to_end(path)
                return hit
        offsets = array("I")
        with open(path, "rb") as handle:
            offsets.frombytes(handle.read())
        with self._cache_lock:
            self._cache[path] = offsets
            while len(self._cache) > OFFSET_CACHE:
                self._cache.popitem(last=False)
        return offsets

    def read_passage(self, language, book_id, index):
        """One passage by number, or None if the book or passage is missing."""
        row = self._conn().execute("SELECT file, passages, bytes FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        if not row or not 0 <= index < row[1]:
            return None
        folder, name = self._folder(language), row[0]
        try:
            offsets = self._offsets(folder / (name[:-4] + ".idx"))
            with (folder / name).open("rb") as handle:
                if os.fstat(handle.fileno()).st_size != row[2]:       # truncated or damaged: never serve half a book
                    raise OSError("size mismatch")
                handle.seek(offsets[index])
                return handle.read(offsets[index + 1] - offsets[index]).decode("utf-8")
        except (OSError, IndexError, UnicodeDecodeError):
            self.retire(language, book_id)           # the files vanished or are damaged: forget this book so it can be rebuilt
            return None

    def is_current(self, language, book_id, content_hash, segment_version):
        row = self._conn().execute("SELECT content_hash, segment_version FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        return bool(row and row[0] == content_hash and row[1] == segment_version)

    # ---- writing -----------------------------------------------------------------------------
    def publish(self, language, book_id, passages, *, edition_key, content_hash, segment_version, external=False, book=None):
        """Write a prepared book and only then mark it ready. Returns its meta."""
        if not passages:
            raise ValueError("Cannot publish a book with no passages.")
        folder = self._folder(language)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{safe_name(book_id)}.{content_hash[:8]}.{segment_version}"
        blobs = [p.encode("utf-8") for p in passages]
        offsets, total = array("I", [0]), 0
        for blob in blobs:
            total += len(blob)
            offsets.append(total)
        pid = f"{os.getpid()}.{threading.get_ident()}"
        tmp_txt, tmp_idx = folder / f".{stem}.{pid}.txt.tmp", folder / f".{stem}.{pid}.idx.tmp"
        with tmp_txt.open("wb") as handle:
            for blob in blobs:
                handle.write(blob)
            handle.flush(); os.fsync(handle.fileno())
        tmp_idx.write_bytes(offsets.tobytes())
        os.replace(tmp_txt, folder / (stem + ".txt"))
        os.replace(tmp_idx, folder / (stem + ".idx"))
        previous = self._conn().execute("SELECT file FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        now = time.time()
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("""INSERT OR REPLACE INTO books (lang, book_id, file, bytes, passages, content_hash, segment_version, edition_key,
                         prepared_at, external, book) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (language, book_id, stem + ".txt", total, len(passages), content_hash, segment_version, edition_key, now,
                       1 if external else 0, json.dumps(book, ensure_ascii=False) if external and book else None))
            c.execute("UPDATE counter SET v = v + 1 WHERE id = 1")
            c.execute("COMMIT")                       # <- the moment the book becomes ready, for every process
        except Exception:
            c.execute("ROLLBACK")
            raise
        if previous and previous[0] != stem + ".txt":
            self._remove_files(folder, previous[0])
        return self.meta(language, book_id)

    def _remove_files(self, folder, name):
        for suffix in (name, name[:-4] + ".idx"):
            try:
                (folder / suffix).unlink()
            except OSError:
                pass
        with self._cache_lock:
            self._cache.pop(folder / (name[:-4] + ".idx"), None)

    def retire(self, language, book_id):
        """Remove a prepared copy (cache policy, damaged files, or withdrawn rights). Only this library copy goes."""
        c = self._conn()
        row = c.execute("SELECT file FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM books WHERE lang=? AND book_id=?", (language, book_id))
            c.execute("UPDATE counter SET v = v + 1 WHERE id = 1")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        if row:
            self._remove_files(self._folder(language), row[0])

    # ---- interest and retention --------------------------------------------------------------
    def note_interest(self, language, book_id, kind):
        weight = INTEREST_WEIGHTS.get(kind, 0.0)
        if weight:
            entry = self._interest.setdefault((language, book_id), [0.0, 0.0])
            entry[0] += weight
            entry[1] = time.time()

    def interest_score(self, language, book_id):
        entry = self._interest.get((language, book_id))
        return entry[0] if entry else 0.0

    def evict_cold(self, language, max_cold):
        """Keep at most `max_cold` on-demand books; retire the OLDEST first. Age only: interest never decides what stays,
        so the library remains an unbiased sample of the catalogue."""
        c = self._conn()
        total = c.execute("SELECT COUNT(*) FROM books WHERE lang=? AND external=1", (language,)).fetchone()[0]
        excess = total - max(0, int(max_cold))
        if excess <= 0:
            return []
        victims = [r[0] for r in c.execute("SELECT book_id FROM books WHERE lang=? AND external=1 ORDER BY prepared_at LIMIT ?", (language, excess))]
        for bid in victims:
            self.retire(language, bid)
        return victims

    def _set_prepared_at(self, language, book_id, stamp):      # used by tests
        self._conn().execute("UPDATE books SET prepared_at=? WHERE lang=? AND book_id=?", (stamp, language, book_id))
