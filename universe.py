"""The universe: the read-optimised random index of every drawable book, per language, in one small SQLite file.

Built from the Book Registry (registry.py) - never per visitor. Each row has a random position `n` (a fixed shuffle made at build
time), so "draw number n" is a uniform random draw and an O(1) lookup, with nothing held in memory. Full texts are NOT here: only
metadata. The text of a drawn edition is prepared by the background worker (prep.py) and kept in the library (shelf.py).

Books that stop being drawable AFTER a build (paused, withdrawn, text failed, rights expired) are skipped at draw time through
`ineligible` (read from the Registry): rejecting them keeps the draw uniform over everything that is still eligible.
Popularity does not live here: it is looked up fresh from the Registry when a book is revealed.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import catalogue_csv
import source

LANGUAGES = ("en", "fr", "zh")
LANGUAGE_LABELS = {"en": "English", "fr": "French", "zh": "Chinese"}
SCHEMA_VERSION = 4
CREATE_SQL = """
    CREATE TABLE books (lang TEXT NOT NULL, n INTEGER NOT NULL, book_id TEXT NOT NULL, gid INTEGER, title TEXT, author TEXT,
        genres TEXT, provider TEXT NOT NULL, rights TEXT, extra TEXT, text_url TEXT, PRIMARY KEY (lang, n));
    CREATE UNIQUE INDEX books_id ON books (lang, book_id);
    CREATE INDEX books_gid ON books (lang, gid);
    CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT);
"""


def text_urls_for(gid):
    """Where to find one book's plain text, in the order the desktop app tries them."""
    gid = int(gid)
    return [f"https://dev.gutenberg.org/cache/epub/{gid}/pg{gid}.txt", f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
            f"https://www.gutenberg.org/files/{gid}/{gid}-0.txt", f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8"]


def rows_from_records(records, blocked=()):
    """CSV records (catalogue_csv.read_records) -> index rows, grouped by language. A book belongs to the first language listed."""
    blocked, by_lang = {int(b) for b in blocked}, {code: [] for code in LANGUAGES}
    for r in records:
        language, gid = catalogue_csv.first_language(r.get("languages")), int(r["gutenberg_id"])
        if language in by_lang and gid not in blocked:
            genres = catalogue_csv.split_list(r.get("bookshelves"), 3) or catalogue_csv.split_list(r.get("subjects"), 2)
            by_lang[language].append({"book_id": f"pg-{language}-{gid}", "provider": "gutenberg", "gid": gid,
                                      "title": catalogue_csv.clean_title(r.get("title")),
                                      "author": "; ".join(catalogue_csv.split_list(r.get("authors"), 3)) or "Author not listed",
                                      "genres": genres, "rights": "public_domain_candidate", "extra": None})
    return by_lang


def build(rows_by_lang, db_path, *, blocked=(), seed=None, log=print, progress=None, generated_at=None):
    """Turn index rows into universe.sqlite atomically (staging file, then rename). Returns {language: count}.

    Chinese Gutenberg titles are converted to Simplified when OpenCC is installed. Blocked IDs are left out. The old file
    stays in use until the new one is complete, and an empty result never replaces a good index."""
    db_path = Path(db_path)
    rng, blocked = random.Random(seed), {int(b) for b in blocked}
    convert = None
    try:
        from opencc import OpenCC
        convert = OpenCC("t2s").convert
    except ImportError:
        log("OpenCC is not installed; Chinese titles stay as published.")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + f".{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(CREATE_SQL)
        counts = {}
        for language in LANGUAGES:
            rows = [r for r in rows_by_lang.get(language, []) if r.get("gid") not in blocked]
            rng.shuffle(rows)                                    # the fixed shuffle that makes `n` a uniform draw
            batch = []
            for n, r in enumerate(rows):
                title, author, genres = r["title"], r["author"], list(r["genres"])
                if language == "zh" and convert and r["provider"] == "gutenberg":
                    title, author, genres = convert(title), convert(author), [convert(g) for g in genres]
                gid = r.get("gid")
                batch.append((language, n, r["book_id"], gid, title, author, json.dumps(genres, ensure_ascii=False), r["provider"], r.get("rights"),
                              json.dumps(r["extra"], ensure_ascii=False) if r.get("extra") else None,
                              text_urls_for(gid)[1] if gid and r["provider"] == "gutenberg" else None))
            conn.executemany("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            counts[language] = len(rows)
            if progress:
                progress(f"Updating the local index with {sum(counts.values()):,} text entries\u2026", 80 + 18 * (len(counts) / len(LANGUAGES)))
        stamp = generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for key, value in {"schema": SCHEMA_VERSION, "generated_at": stamp, "partial": "0", "counts": json.dumps(counts)}.items():
            conn.execute("INSERT INTO info VALUES (?,?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()
    if not any(counts.values()):
        tmp.unlink(missing_ok=True)
        raise ValueError("The catalogue produced no eligible books; the old index was left untouched.")
    os.replace(tmp, db_path)
    log("Catalogue index built: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
    return counts


def build_from_records(records, db_path, *, blocked=(), **kw):
    """Build straight from CSV records (tools and tests). The app itself goes through the Registry."""
    return build(rows_from_records(records, blocked), db_path, blocked=blocked, **kw)


def upgrade_index(src, dst):
    """Copy an older (schema 3) index to the current schema without any network: same rows, same shuffle. Returns True if converted."""
    src, dst = Path(src), Path(dst)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        info = dict(s.execute("SELECT key, value FROM info").fetchall())
        if int(info.get("schema", 0)) >= SCHEMA_VERSION:
            shutil.copyfile(src, dst)
            return False
        rows = s.execute("SELECT lang, n, gid, title, author, genres, text_url FROM books")
        out = sqlite3.connect(dst)
        try:
            out.executescript(CREATE_SQL)
            out.executemany("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            ((lang, n, f"pg-{lang}-{gid}", gid, title, author, genres, "gutenberg", "public_domain_candidate", None, url)
                             for lang, n, gid, title, author, genres, url in rows))
            info["schema"] = str(SCHEMA_VERSION)
            out.executemany("INSERT INTO info VALUES (?,?)", info.items())
            out.commit()
        finally:
            out.close()
        return True
    finally:
        s.close()


def index_rows(db_path):
    """(language, book_id, gid, title, author, genres_json) for every Gutenberg row of an index: the Registry's no-network bootstrap."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        info = dict(conn.execute("SELECT key, value FROM info").fetchall())
        if int(info.get("schema", 0)) != SCHEMA_VERSION:
            return []
        return conn.execute("SELECT lang, book_id, gid, title, author, genres FROM books WHERE provider='gutenberg'").fetchall()
    finally:
        conn.close()


def touch(db_path, generated_at=None):
    """The catalogue was checked and is unchanged: record 'fresh as of now' without rebuilding anything."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("UPDATE info SET value=? WHERE key='generated_at'", (generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
        conn.commit()
    finally:
        conn.close()


class Universe:
    """Read-only view of universe.sqlite. Cheap to create; safe across threads; reloads when the file changes."""

    def __init__(self, db_path, blocklist_path=None, registry=None):
        self.path, self.blocklist_path = Path(db_path), Path(blocklist_path) if blocklist_path else None
        self.registry, self._registry_generation, self.ineligible = registry, None, set()
        self._local = threading.local()
        self._stamp, self._block_stamp = None, None
        self.counts, self.generated_at, self.partial = {code: 0 for code in LANGUAGES}, "", False
        self.blocked = set()
        self.load_error = None
        self.reload(force=True)

    # ---- state -------------------------------------------------------------------------------
    def _conn(self):
        stamp = getattr(self._local, "stamp", None)
        if getattr(self._local, "conn", None) is None or stamp != self._stamp:
            old = getattr(self._local, "conn", None)
            if old:
                old.close()
            self._local.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._local.stamp = self._stamp
        return self._local.conn

    def reload(self, force=False):
        """Re-read counts if the file (or the blocklist) changed. Returns True when something changed."""
        changed = False
        try:
            if self.registry is not None:
                generation = self.registry.generation
                if force or generation != self._registry_generation:
                    self.ineligible, self._registry_generation, changed = self.registry.ineligible_ids(), generation, True
            if self.blocklist_path:
                stamp = self.blocklist_path.stat().st_mtime_ns if self.blocklist_path.exists() else None
                if force or stamp != self._block_stamp:
                    self._block_stamp, changed = stamp, True
                    self.blocked = self._read_blocklist()
            stamp = self.path.stat().st_mtime_ns if self.path.exists() else None
            if stamp is None:
                self.counts, self._stamp = {code: 0 for code in LANGUAGES}, None
                self.load_error = "The Project Gutenberg catalogue has not been opened yet."
                return changed
            if not force and stamp == self._stamp:
                return changed
            self._stamp = stamp
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                info = dict(conn.execute("SELECT key, value FROM info").fetchall())
            finally:
                conn.close()
            if int(info.get("schema", 0)) != SCHEMA_VERSION:
                self.counts, self.load_error = {code: 0 for code in LANGUAGES}, "The catalogue index is from an older version and will be rebuilt."
                return changed
            self.counts = {code: int(v) for code, v in json.loads(info.get("counts", "{}")).items()}
            for code in LANGUAGES:
                self.counts.setdefault(code, 0)
            self.generated_at, self.partial = info.get("generated_at", ""), info.get("partial") == "1"
            self.load_error = None
            return True
        except (sqlite3.Error, ValueError, OSError) as exc:
            self.counts, self.load_error = {code: 0 for code in LANGUAGES}, f"The catalogue could not be read: {exc}"
            return changed

    def _read_blocklist(self):
        try:
            value = json.loads(self.blocklist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        ids = value if isinstance(value, list) else [i for v in (value or {}).values() if isinstance(v, list) for i in v]
        return {int(i) for i in ids if str(i).isdigit()}

    @property
    def available(self):
        return self.load_error is None and any(self.counts.values())

    def count(self, language):
        return self.counts.get(language, 0)

    @property
    def is_partial_snapshot(self):
        return self.partial

    # ---- reading -----------------------------------------------------------------------------
    _COLUMNS = "lang, n, book_id, gid, title, author, genres, provider, rights, extra, text_url"

    def _record(self, language, row):
        _lang, n, book_id, gid, title, author, genres, provider, rights, extra, text_url = row
        record = {"id": book_id, "title": title, "author": author, "genres": json.loads(genres or "[]"), "description": "",
                  "language": language, "gutenberg_id": gid, "draw_number": n + 1, "provider": provider,
                  "rights_status": rights or "public_domain_candidate"}
        if provider == "gutenberg":
            record.update({
                "source_provider": "Project Gutenberg", "source_url": f"https://www.gutenberg.org/ebooks/{gid}",
                "source_credit": f"Text source: Project Gutenberg eBook #{gid}",
                "source_license_note": "Project Gutenberg records this title as public domain in the USA. Copyright status can differ by country.",
                "text_url": text_url, "text_urls": text_urls_for(gid)})
        else:
            data = json.loads(extra or "{}")
            record.update({"source_provider": "Independent author", "links": data.get("links") or {}, "description": data.get("description") or "",
                           "source_credit": data.get("attribution") or "Text supplied by the author",
                           "source_license_note": "Shared with the author's written permission.",
                           "text_key": data.get("text_key"), "text_hash": data.get("text_hash"), "publication": data.get("publication")})
        return record

    def get(self, language, gid):
        row = self._conn().execute(f"SELECT {self._COLUMNS} FROM books WHERE lang=? AND gid=?", (language, int(gid))).fetchone()
        return self._record(language, row) if row else None

    def get_by_id(self, language, book_id):
        row = self._conn().execute(f"SELECT {self._COLUMNS} FROM books WHERE lang=? AND book_id=?", (language, book_id)).fetchone()
        return self._record(language, row) if row else None

    def _drawable(self, row):
        return row[2] not in self.ineligible and (row[3] is None or int(row[3]) not in self.blocked)

    def iter_eligible(self, language):
        """Every drawable record. Only for small catalogues (exhaustion checks); the real draw never scans."""
        for row in self._conn().execute(f"SELECT {self._COLUMNS} FROM books WHERE lang=? ORDER BY n", (language,)):
            if self._drawable(row):
                yield self._record(language, row)

    def random_candidates(self, language, count, rng=random, reject=None, max_tries=60):
        """`count` distinct uniform draws from the whole eligible catalogue. `reject(record)` skips unwanted ones."""
        total = self.count(language)
        picked, seen_n = [], set()
        if not total:
            return picked
        conn = self._conn()
        for _ in range(max(count * max_tries, 1)):
            if len(picked) >= count or len(seen_n) >= total:
                break
            n = rng.randrange(total)
            if n in seen_n:
                continue
            seen_n.add(n)
            row = conn.execute(f"SELECT {self._COLUMNS} FROM books WHERE lang=? AND n=?", (language, n)).fetchone()
            if not row or not self._drawable(row):
                continue
            record = self._record(language, row)
            if reject and reject(record):
                continue
            picked.append(record)
        return picked
