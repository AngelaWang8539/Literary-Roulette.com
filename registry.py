"""The Book Registry: one stable identity per book, with rights, text, pool and provenance kept as separate facts.

* `books`               one row per book. `book_id` is stable (Gutenberg: pg-<lang>-<gutenberg id>, the same ids the shared
                        library already uses; authors: au-<lang>-<slug>). Rights, text and pool status are SEPARATE columns:
                        a technically valid text is not automatically licensed, and a licensed one is not automatically drawn.
* `provider_snapshots`  what each provider said about a book and when (normalized fields + a hash of the raw answer).
* `popularity_snapshots` / `popularity_latest`
                        every popularity observation: provider + metric + period + value + when. NULL when unknown. Rank and
                        percentile are computed per language, only from a COMPLETE comparable population (see percentile()).
* `sync_runs`           one row per provider run: state, counts, completeness, retries, error class, generation. The
                        "last good" run of a provider is what the status page shows; a failed run never replaces data.

The roulette itself never reads this file per spin: universe.py builds the read-optimised random index from it, and only the
small set of books that are NOT eligible is consulted while drawing. SQLite (WAL) is the durable layer for now; every table
maps one-to-one onto Postgres, which is how it moves to Supabase when more than one machine needs it (docs/REGISTRY.md).
"""
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import catalogue_csv
from library import RIGHTS_CLEARED

LANGUAGES = ("en", "fr", "zh")
POOL_STATUSES = ("pending", "active", "paused", "archived", "starter")
TEXT_OK = ("unverified", "verified")                 # a text we may try to prepare / serve
TEXT_STATUSES = ("pending", "unverified", "verified", "failed", "rejected")
RIGHTS_STATUSES = ("pending_verification", "public_domain_candidate", "approved", "source_licensed", "paused", "withdrawn")
SYSTEM = "auto:"                                      # reasons starting with this belong to the importer, never to an admin

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS books (
  book_id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_id TEXT NOT NULL, gutenberg_id INTEGER,
  language TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
  subjects TEXT NOT NULL DEFAULT '[]', bookshelves TEXT NOT NULL DEFAULT '[]',
  publication TEXT, description TEXT, links TEXT NOT NULL DEFAULT '{}', attribution TEXT,
  text_key TEXT, text_hash TEXT, revision INTEGER NOT NULL DEFAULT 1,
  metadata_status TEXT NOT NULL DEFAULT 'catalogue', text_status TEXT NOT NULL DEFAULT 'unverified', text_error TEXT,
  rights_status TEXT NOT NULL DEFAULT 'pending_verification', rights_territory TEXT, rights_commercial INTEGER,
  rights_expires TEXT, rights_note TEXT,
  pool_status TEXT NOT NULL DEFAULT 'pending', status_reason TEXT, confirmed_fields TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (provider, provider_id));
CREATE INDEX IF NOT EXISTS books_lang_pool ON books (language, pool_status);
CREATE INDEX IF NOT EXISTS books_gid ON books (gutenberg_id);
CREATE INDEX IF NOT EXISTS books_hash ON books (text_hash);
CREATE TABLE IF NOT EXISTS provider_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, book_id TEXT NOT NULL, fetched_at REAL NOT NULL,
  provider_timestamp TEXT, schema_version TEXT NOT NULL, status TEXT NOT NULL, normalized TEXT, raw_hash TEXT);
CREATE INDEX IF NOT EXISTS snaps_book ON provider_snapshots (book_id, provider, fetched_at);
CREATE TABLE IF NOT EXISTS popularity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, metric TEXT NOT NULL, period TEXT NOT NULL,
  language TEXT NOT NULL, book_id TEXT NOT NULL, value REAL, rank INTEGER, observed_at REAL NOT NULL, scope TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pop_book ON popularity_snapshots (book_id, provider, metric, observed_at);
CREATE TABLE IF NOT EXISTS popularity_latest (
  book_id TEXT NOT NULL, provider TEXT NOT NULL, metric TEXT NOT NULL, period TEXT NOT NULL, language TEXT NOT NULL,
  value REAL, rank INTEGER, observed_at REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY (book_id, provider, metric));
CREATE INDEX IF NOT EXISTS pop_latest_rank ON popularity_latest (language, provider, metric, value);
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, state TEXT NOT NULL,
  records INTEGER, completeness REAL, retries INTEGER NOT NULL DEFAULT 0, error_class TEXT, error TEXT, generation TEXT,
  source_hash TEXT, etag TEXT, last_modified TEXT);
CREATE INDEX IF NOT EXISTS runs_provider ON sync_runs (provider, started_at);
CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY CHECK (id = 1), v INTEGER NOT NULL);
INSERT OR IGNORE INTO counter VALUES (1, 0);
"""

BOOK_COLUMNS = ("book_id, provider, provider_id, gutenberg_id, language, title, author, subjects, bookshelves, publication, "
                "description, links, attribution, text_key, text_hash, revision, metadata_status, text_status, text_error, "
                "rights_status, rights_territory, rights_commercial, rights_expires, rights_note, pool_status, status_reason, "
                "confirmed_fields, created_at, updated_at")
JSON_FIELDS = ("subjects", "bookshelves", "publication", "links", "confirmed_fields")


class RegistryError(ValueError):
    """A change the Registry refuses. The message is written for an admin and is safe to show."""


def _book(row):
    out = dict(zip(BOOK_COLUMNS.replace("\n", " ").split(", "), row))
    for key in JSON_FIELDS:
        if out.get(key):
            try:
                out[key] = json.loads(out[key])
            except ValueError:
                pass
    return out


class Registry:
    def __init__(self, path, clock=time.time):
        self.path, self._clock = Path(path), clock
        self._local = threading.local()
        self._complete_cache = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn().executescript(SCHEMA_SQL)

    # ---- plumbing ----------------------------------------------------------------------------------------------
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self, bump=False):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            if bump:
                c.execute("UPDATE counter SET v = v + 1 WHERE id = 1")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        self._complete_cache.clear()

    @property
    def generation(self):
        """Changes whenever the SET of drawable books may have changed (cheap: one tiny SELECT)."""
        return self._conn().execute("SELECT v FROM counter WHERE id = 1").fetchone()[0]

    def today(self):
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _eligible_sql(self, alias=""):
        a = alias + "." if alias else ""
        marks = ",".join("?" * len(RIGHTS_CLEARED))
        sql = (f"({a}pool_status = 'active' AND {a}rights_status IN ({marks}) AND {a}text_status IN ('unverified','verified') "
               f"AND ({a}rights_expires IS NULL OR {a}rights_expires = 'perpetual' OR {a}rights_expires >= ?))")
        return sql, (*RIGHTS_CLEARED, self.today())

    def count(self):
        return self._conn().execute("SELECT COUNT(*) FROM books").fetchone()[0]

    # ---- importing ---------------------------------------------------------------------------------------------
    def import_gutenberg(self, records, *, blocked=()):
        """Upsert the Gutenberg catalogue (catalogue_csv.read_records). One transaction, all or nothing.

        New books arrive active (rights: public_domain_candidate, i.e. what Gutenberg states for the USA). Existing books get
        fresh catalogue metadata except fields an admin confirmed; admin decisions (pool, rights) are never overwritten.
        A book that vanished from the catalogue is paused with an `auto:` reason and comes back by itself if it reappears."""
        blocked, now = {int(b) for b in blocked}, self._clock()
        seen, added, updated, restored = set(), 0, 0, 0
        with self._tx(bump=True) as c:
            existing = {r[1]: r for r in c.execute(
                "SELECT book_id, provider_id, title, author, subjects, bookshelves, confirmed_fields, pool_status, status_reason, "
                "metadata_status FROM books WHERE provider = 'gutenberg'")}
            inserts, changes, pool_moves = [], [], []
            for r in records:
                language = catalogue_csv.first_language(r.get("languages"))
                if language not in LANGUAGES:
                    continue
                gid = int(r["gutenberg_id"])
                pid = str(gid)
                seen.add(pid)
                title = catalogue_csv.clean_title(r.get("title"))
                author = "; ".join(catalogue_csv.split_list(r.get("authors"), 3)) or "Author not listed"
                subjects = json.dumps(catalogue_csv.split_list(r.get("subjects"), 8), ensure_ascii=False)
                shelves = json.dumps(catalogue_csv.split_list(r.get("bookshelves"), 5), ensure_ascii=False)
                held = gid in blocked
                old = existing.get(pid)
                if old is None:
                    inserts.append((f"pg-{language}-{gid}", "gutenberg", pid, gid, language, title, author, subjects, shelves,
                                    "public_domain_candidate", "US", "paused" if held else "active", SYSTEM + "blocklist" if held else None,
                                    now, now))
                    continue
                bid, _pid, o_title, o_author, o_subjects, o_shelves, conf, pool, reason, meta = old
                confirmed = set(json.loads(conf or "[]"))
                keep = lambda name, new, current: current if name in confirmed else new
                values = (keep("title", title, o_title), keep("author", author, o_author),
                          keep("subjects", subjects, o_subjects), keep("bookshelves", shelves, o_shelves))
                if values != (o_title, o_author, o_subjects, o_shelves) or meta == "missing_upstream":
                    changes.append((*values, "catalogue" if meta == "missing_upstream" else meta, now, bid))
                    updated += 1
                if held and pool == "active":
                    pool_moves.append(("paused", SYSTEM + "blocklist", now, bid))
                elif not held and pool == "paused" and reason in (SYSTEM + "blocklist", SYSTEM + "missing-upstream"):
                    pool_moves.append(("active", None, now, bid))
                    restored += 1
            c.executemany("""INSERT INTO books (book_id, provider, provider_id, gutenberg_id, language, title, author, subjects, bookshelves,
                             rights_status, rights_territory, pool_status, status_reason, created_at, updated_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", inserts)
            c.executemany("UPDATE books SET title=?, author=?, subjects=?, bookshelves=?, metadata_status=?, updated_at=? WHERE book_id=?", changes)
            c.executemany("UPDATE books SET pool_status=?, status_reason=?, updated_at=? WHERE book_id=?", pool_moves)
            gone = [(now, v[0]) for pid, v in existing.items() if pid not in seen and v[9] != "missing_upstream"]
            c.executemany("UPDATE books SET metadata_status='missing_upstream', updated_at=? WHERE book_id=?", gone)
            c.executemany("UPDATE books SET pool_status='paused', status_reason=? WHERE book_id=? AND pool_status='active'",
                          [(SYSTEM + "missing-upstream", g[1]) for g in gone])
            added = len(inserts)
        return {"added": added, "updated": updated, "missing": len(gone), "restored": restored, "total": len(seen)}

    def import_index_rows(self, rows):
        """Bootstrap from an existing random index (no network): insert-only, so nothing an admin decided is touched.
        rows: (language, book_id, gutenberg_id, title, author, genres_json)."""
        now = self._clock()
        data = [(bid, "gutenberg", str(gid), gid, lang, title, author, "[]", genres or "[]", "public_domain_candidate", "US", "active", now, now)
                for lang, bid, gid, title, author, genres in rows if gid]
        with self._tx(bump=True) as c:
            before = c.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            c.executemany("""INSERT OR IGNORE INTO books (book_id, provider, provider_id, gutenberg_id, language, title, author, subjects,
                             bookshelves, rights_status, rights_territory, pool_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", data)
            return c.execute("SELECT COUNT(*) FROM books").fetchone()[0] - before

    def import_local_books(self, language, books, manifest=None):
        """The curated books.json titles: recorded for provenance and kept in the `starter` pool, which is only used when the
        whole catalogue cannot be opened. They never join the ocean draw."""
        now, manifest = self._clock(), manifest or {}
        with self._tx() as c:
            for b in books:
                bid = b.get("id")
                if not isinstance(bid, str) or not b.get("title"):
                    continue
                entry = manifest.get(bid) or {}
                verified = entry.get("verified") is True
                links = json.dumps(b["links"], ensure_ascii=False) if isinstance(b.get("links"), dict) else "{}"
                gid = int(b["gutenberg_id"]) if str(b.get("gutenberg_id") or "").isdigit() else None
                c.execute("""INSERT INTO books (book_id, provider, provider_id, gutenberg_id, language, title, author, description, links,
                             attribution, text_key, text_hash, text_status, rights_status, pool_status, created_at, updated_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(provider, provider_id) DO UPDATE SET text_hash=excluded.text_hash, text_status=excluded.text_status,
                               text_key=excluded.text_key, updated_at=excluded.updated_at""",
                          (bid, "local", bid, gid, language, b["title"], b.get("author") or "", b.get("description") or "", links,
                           b.get("source_credit"), b.get("text_file"), entry.get("text_sha256"), "verified" if verified else "unverified",
                           b.get("rights_status") or "pending_verification", "starter", now, now))

    # ---- reading -----------------------------------------------------------------------------------------------
    def get(self, book_id):
        row = self._conn().execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE book_id = ?", (book_id,)).fetchone()
        return _book(row) if row else None

    def find_hash(self, text_hash):
        row = self._conn().execute("SELECT book_id FROM books WHERE text_hash = ? LIMIT 1", (text_hash,)).fetchone()
        return row[0] if row else None

    def eligible_rows(self, language):
        """What the random index is built from: active, rights-cleared, text-eligible books of one language."""
        sql, params = self._eligible_sql()
        cur = self._conn().execute(
            f"SELECT book_id, provider, gutenberg_id, title, author, subjects, bookshelves, rights_status, links, attribution, "
            f"description, text_key, text_hash, publication FROM books WHERE language = ? AND {sql}", (language, *params))
        for bid, provider, gid, title, author, subjects, shelves, rights, links, attribution, description, text_key, text_hash, publication in cur:
            genres = json.loads(shelves or "[]")[:3] or json.loads(subjects or "[]")[:2]
            extra = None
            if provider != "gutenberg":
                extra = {"links": json.loads(links or "{}"), "attribution": attribution, "description": description or "",
                         "text_key": text_key, "text_hash": text_hash, "publication": json.loads(publication) if publication else None}
            yield {"book_id": bid, "provider": provider, "gid": gid, "title": title, "author": author, "genres": genres,
                   "rights": rights, "extra": extra}

    def is_eligible(self, book_id):
        sql, params = self._eligible_sql()
        return self._conn().execute(f"SELECT 1 FROM books WHERE book_id = ? AND {sql}", (book_id, *params)).fetchone() is not None

    def reset_failed(self):
        """Give every book whose text failed another chance (the admin's 'clear failures')."""
        with self._tx(bump=True) as c:
            c.execute("UPDATE books SET text_status='unverified', text_error=NULL WHERE text_status='failed' AND provider='gutenberg'")

    def ineligible_ids(self):
        """Books that must not be drawn right now (paused, withdrawn, failed text, rights not cleared or expired). Small."""
        sql, params = self._eligible_sql()
        return {r[0] for r in self._conn().execute(f"SELECT book_id FROM books WHERE NOT {sql}", params)}

    def search(self, *, q="", language=None, pool=None, rights=None, provider=None, text=None, limit=50, offset=0):
        where, params = ["1=1"], []
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' OR book_id LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        for column, value in (("language", language), ("pool_status", pool), ("rights_status", rights), ("provider", provider), ("text_status", text)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = " AND ".join(where)
        c = self._conn()
        total = c.execute(f"SELECT COUNT(*) FROM books WHERE {clause}", params).fetchone()[0]
        rows = c.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE {clause} ORDER BY updated_at DESC, book_id LIMIT ? OFFSET ?",
                         (*params, max(1, min(int(limit), 200)), max(0, int(offset)))).fetchall()
        return {"total": total, "books": [_book(r) for r in rows]}

    def summary(self):
        c = self._conn()
        sql, params = self._eligible_sql()
        by = lambda column: dict(c.execute(f"SELECT {column}, COUNT(*) FROM books GROUP BY {column}").fetchall())
        eligible = {lang: n for lang, n in c.execute(f"SELECT language, COUNT(*) FROM books WHERE {sql} GROUP BY language", params)}
        return {"books": self.count(), "eligible": eligible, "by_provider": by("provider"), "by_language": by("language"),
                "by_pool": by("pool_status"), "by_rights": by("rights_status"), "by_text": by("text_status"),
                "by_metadata": by("metadata_status"), "generation": self.generation}

    # ---- decisions: rights gate, pool, text -------------------------------------------------------------------
    def _activation_blockers(self, book):
        problems = []
        if book["rights_status"] not in RIGHTS_CLEARED:
            problems.append(f"rights are {book['rights_status']!r}, not cleared")
        if book["text_status"] not in TEXT_OK:
            problems.append(f"the text is {book['text_status']!r}")
        if book["provider"] == "author":
            if book["rights_status"] != "approved":
                problems.append("author books need rights status 'approved' with territory, commercial scope, attribution and expiry")
            if not book.get("text_hash"):
                problems.append("no verified text is stored")
        return problems

    def blockers(self, book_id):
        """Why this book cannot be activated yet (empty list: it can)."""
        book = self.get(book_id)
        return self._activation_blockers(book) if book else ["unknown book"]

    def set_pool_status(self, book_id, status, reason=None):
        if status not in ("active", "paused", "archived"):
            raise RegistryError("Pool status must be active, paused or archived.")
        book = self.get(book_id)
        if not book:
            raise RegistryError(f"Unknown book {book_id!r}.")
        if status == "active":
            problems = self._activation_blockers(book)
            if problems:
                raise RegistryError("Cannot activate: " + "; ".join(problems) + ".")
        with self._tx(bump=True) as c:
            c.execute("UPDATE books SET pool_status=?, status_reason=?, updated_at=? WHERE book_id=?",
                      (status, (reason or "").strip() or None, self._clock(), book_id))
        return self.get(book_id)

    def set_rights(self, book_id, status, *, territory=None, commercial=None, attribution=None, expires=None, note=None):
        """The rights gate. 'approved' and 'source_licensed' must record territory, commercial scope, attribution and expiry
        (a date, or the word 'perpetual'). 'paused' / 'withdrawn' also take the book out of the pool at once."""
        if status not in RIGHTS_STATUSES:
            raise RegistryError("Unknown rights status.")
        book = self.get(book_id)
        if not book:
            raise RegistryError(f"Unknown book {book_id!r}.")
        territory = (territory or book.get("rights_territory") or "").strip() or None
        attribution = (attribution or book.get("attribution") or "").strip() or None
        expires = (expires or book.get("rights_expires") or "").strip() or None
        commercial = book.get("rights_commercial") if commercial is None else (1 if commercial in (True, 1, "yes", "true", "1") else 0)
        if status in ("approved", "source_licensed"):
            missing = [n for n, v in (("territory", territory), ("commercial scope", commercial), ("attribution", attribution), ("expiry", expires)) if v in (None, "")]
            if missing:
                raise RegistryError("Cannot approve rights without: " + ", ".join(missing) + ".")
            if expires != "perpetual":
                try:
                    time.strptime(expires, "%Y-%m-%d")
                except ValueError:
                    raise RegistryError("Expiry must be a date like 2030-12-31, or the word 'perpetual'.") from None
        pool = {"paused": ("paused", "rights paused"), "withdrawn": ("archived", "rights withdrawn")}.get(status)
        with self._tx(bump=True) as c:
            c.execute("""UPDATE books SET rights_status=?, rights_territory=?, rights_commercial=?, rights_expires=?, rights_note=?,
                         attribution=?, updated_at=? WHERE book_id=?""",
                      (status, territory, commercial, expires, (note or book.get("rights_note") or "").strip() or None, attribution, self._clock(), book_id))
            if pool:
                c.execute("UPDATE books SET pool_status=?, status_reason=? WHERE book_id=?", (*pool, book_id))
        return self.get(book_id)

    def mark_text(self, book_id, status, *, error=None, text_hash=None):
        """Record what happened to a book's text. Only a change between eligible and not-eligible touches `generation`."""
        if status not in TEXT_STATUSES:
            raise RegistryError("Unknown text status.")
        book = self.get(book_id)
        if not book:
            return None
        new_hash = text_hash or book.get("text_hash")
        if (book["text_status"], book.get("text_hash")) == (status, new_hash) and not error:
            return book
        revision = book["revision"] + (1 if text_hash and book.get("text_hash") and text_hash != book["text_hash"] else 0)
        flips = (book["text_status"] in TEXT_OK) != (status in TEXT_OK)
        with self._tx(bump=flips) as c:
            c.execute("UPDATE books SET text_status=?, text_error=?, text_hash=?, revision=?, updated_at=? WHERE book_id=?",
                      (status, (error or None) and str(error)[:300], new_hash, revision, self._clock(), book_id))
        return self.get(book_id)

    def apply_enrichment(self, book_id, *, subjects=None, bookshelves=None, copyright_flag=None):
        """Fill catalogue fields from a provider without overwriting what an admin confirmed.

        A Gutendex copyright flag of True holds a still-unreviewed book out of the pool (`auto:copyright-flag`) until an admin
        approves its rights; if the flag later clears, only that automatic hold is lifted."""
        book = self.get(book_id)
        if not book:
            return
        confirmed, now = set(book["confirmed_fields"] or []), self._clock()
        sets, params = [], []
        for name, value in (("subjects", subjects), ("bookshelves", bookshelves)):
            if value and name not in confirmed:
                sets.append(f"{name}=?"); params.append(json.dumps(value[:8], ensure_ascii=False))
        flips = False
        if copyright_flag is True and book["rights_status"] == "public_domain_candidate":
            sets += ["rights_status='pending_verification'", "rights_note=?", "pool_status='paused'", "status_reason=?"]
            params += ["Gutendex flags this edition as under copyright", SYSTEM + "copyright-flag"]
            flips = True
        elif copyright_flag is not True and book["status_reason"] == SYSTEM + "copyright-flag" and book["rights_status"] == "pending_verification":
            sets += ["rights_status='public_domain_candidate'", "rights_note=NULL", "pool_status='active'", "status_reason=NULL"]
            flips = True
        if book["metadata_status"] not in ("confirmed",):
            sets.append("metadata_status='enriched'")
        if not sets:
            return
        with self._tx(bump=flips) as c:
            c.execute(f"UPDATE books SET {', '.join(sets)}, updated_at=? WHERE book_id=?", (*params, now, book_id))

    def confirm_fields(self, book_id, **fields):
        """An admin or the author fixes a field: it is then never overwritten by a provider refresh."""
        allowed = {"title", "author", "description", "subjects", "bookshelves", "publication", "attribution"}
        book = self.get(book_id)
        if not book or not fields or set(fields) - allowed:
            raise RegistryError("Unknown book or field.")
        confirmed = set(book["confirmed_fields"] or []) | set(fields)
        sets = ", ".join(f"{k}=?" for k in fields)
        values = [json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v for k, v in fields.items()]
        with self._tx(bump=True) as c:
            c.execute(f"UPDATE books SET {sets}, confirmed_fields=?, metadata_status='confirmed', updated_at=? WHERE book_id=?",
                      (*values, json.dumps(sorted(confirmed)), self._clock(), book_id))

    def add_author_book(self, *, book_id, slug, language, title, author, text_key, text_hash, attribution, links, description,
                        publication, rights, status_note, revision=1, text_status="verified"):
        """Create (or revise) an author book. It starts pending/unreviewed: rights review and activation are separate steps."""
        now = self._clock()
        with self._tx(bump=True) as c:
            c.execute("""INSERT INTO books (book_id, provider, provider_id, language, title, author, description, links, attribution,
                         publication, text_key, text_hash, revision, text_status, rights_status, rights_territory, rights_commercial,
                         rights_expires, rights_note, pool_status, status_reason, metadata_status, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(provider, provider_id) DO UPDATE SET text_key=excluded.text_key, text_hash=excluded.text_hash,
                           revision=excluded.revision, text_status=excluded.text_status, updated_at=excluded.updated_at""",
                      (book_id, "author", slug, language, title, author, description or "", json.dumps(links or {}), attribution,
                       json.dumps(publication) if publication else None, text_key, text_hash, revision, text_status,
                       "pending_verification", rights.get("territory"), rights.get("commercial"), rights.get("expires"), rights.get("note"),
                       "pending", status_note, "confirmed", now, now))
        return self.get(book_id)

    # ---- provider snapshots and popularity ---------------------------------------------------------------------
    def add_snapshot(self, provider, book_id, *, normalized, status="ok", provider_timestamp=None, raw_hash=None, schema_version="1"):
        with self._tx() as c:
            c.execute("""INSERT INTO provider_snapshots (provider, book_id, fetched_at, provider_timestamp, schema_version, status, normalized, raw_hash)
                         VALUES (?,?,?,?,?,?,?,?)""", (provider, book_id, self._clock(), provider_timestamp, schema_version, status,
                                                         json.dumps(normalized, ensure_ascii=False) if normalized is not None else None, raw_hash))
            c.execute("""DELETE FROM provider_snapshots WHERE book_id=? AND provider=? AND id NOT IN
                         (SELECT id FROM provider_snapshots WHERE book_id=? AND provider=? ORDER BY id DESC LIMIT 3)""",
                      (book_id, provider, book_id, provider))

    def snapshots(self, book_id, provider=None):
        sql, params = "SELECT provider, fetched_at, status, normalized, raw_hash FROM provider_snapshots WHERE book_id=?", [book_id]
        if provider:
            sql, params = sql + " AND provider=?", params + [provider]
        return [{"provider": p, "fetched_at": t, "status": s, "normalized": json.loads(n) if n else None, "raw_hash": h}
                for p, t, s, n, h in self._conn().execute(sql + " ORDER BY id DESC", params)]

    def add_popularity(self, rows, *, provider, metric, period, scope):
        """rows: (book_id, language, value or None, rank or None, status). NULL stays NULL: it never becomes 'least popular'."""
        now = self._clock()
        rows = list(rows)
        with self._tx() as c:
            c.executemany("""INSERT INTO popularity_snapshots (provider, metric, period, language, book_id, value, rank, observed_at, scope)
                             VALUES (?,?,?,?,?,?,?,?,?)""", [(provider, metric, period, lang, bid, value, rank, now, scope) for bid, lang, value, rank, _s in rows])
            c.executemany("""INSERT INTO popularity_latest (book_id, provider, metric, period, language, value, rank, observed_at, status)
                             VALUES (?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(book_id, provider, metric) DO UPDATE SET period=excluded.period, value=excluded.value,
                               rank=excluded.rank, observed_at=excluded.observed_at, status=excluded.status""",
                          [(bid, provider, metric, period, lang, value, rank, now, status) for bid, lang, value, rank, status in rows])
        return len(rows)

    def add_top_list(self, period, entries):
        """Gutenberg's top-100 list for one period. entries: (gutenberg_id, rank, visible_count or None). Books we do not
        hold (other languages, not catalogued) are skipped. Stored with provider 'gutenberg_top'; it is a trend signal only."""
        entries = list(entries)
        if not entries:
            return 0
        marks = ",".join("?" * len(entries))
        known = {gid: (bid, lang) for bid, lang, gid in self._conn().execute(
            f"SELECT book_id, language, gutenberg_id FROM books WHERE provider='gutenberg' AND gutenberg_id IN ({marks})", [e[0] for e in entries])}
        rows = [(known[gid][0], known[gid][1], count, rank, "ok") for gid, rank, count in entries if gid in known]
        return self.add_popularity(rows, provider="gutenberg_top", metric="rank", period=period, scope="trend")

    def enrichment_targets(self, *, limit, stale_before, prefer=(), census=False):
        """Gutenberg book ids (gutenberg_id, book_id) that need a fresh Gutendex observation: preferred ids first (books in the
        library, the top lists), then - only in census mode - everything else, oldest observation first."""
        sql, params = self._eligible_sql("b")
        c, out, taken = self._conn(), [], set()
        prefer = [int(g) for g in prefer if str(g).isdigit()]
        for i in range(0, len(prefer), 500):
            chunk = prefer[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for gid, bid in c.execute(
                    f"""SELECT b.gutenberg_id, b.book_id FROM books b LEFT JOIN popularity_latest p
                        ON p.book_id=b.book_id AND p.provider='gutendex' AND p.metric='download_count'
                        WHERE b.provider='gutenberg' AND b.gutenberg_id IN ({marks}) AND {sql} AND (p.observed_at IS NULL OR p.observed_at < ?)""",
                    (*chunk, *params, stale_before)):
                if gid not in taken and len(out) < limit:
                    out.append((gid, bid)); taken.add(gid)
        if census and len(out) < limit:
            for gid, bid in c.execute(
                    f"""SELECT b.gutenberg_id, b.book_id FROM books b LEFT JOIN popularity_latest p
                        ON p.book_id=b.book_id AND p.provider='gutendex' AND p.metric='download_count'
                        WHERE b.provider='gutenberg' AND {sql} AND (p.observed_at IS NULL OR p.observed_at < ?)
                        ORDER BY p.observed_at IS NOT NULL, p.observed_at LIMIT ?""", (*params, stale_before, limit)):
                if gid not in taken and len(out) < limit:
                    out.append((gid, bid)); taken.add(gid)
        return out

    def observed(self, book_id, provider="gutendex", metric="download_count"):
        row = self._conn().execute("SELECT value, observed_at, status FROM popularity_latest WHERE book_id=? AND provider=? AND metric=?",
                                   (book_id, provider, metric)).fetchone()
        return {"value": None if row[0] is None else int(row[0]), "observed_at": row[1], "status": row[2]} if row else None

    def complete(self, language, *, provider="gutendex", metric="download_count", max_age_days=14):
        """True only when EVERY eligible book of the language has a fresh observation (a value, or an explicit 'not found').
        Until then there are no percentile badges: a partial population says nothing about who is least popular."""
        key = (language, provider, metric)
        hit = self._complete_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        sql, params = self._eligible_sql("b")
        c, fresh = self._conn(), self._clock() - max_age_days * 86400
        eligible = c.execute(f"SELECT COUNT(*) FROM books b WHERE b.language=? AND {sql}", (language, *params)).fetchone()[0]
        covered = c.execute(f"""SELECT COUNT(*) FROM books b JOIN popularity_latest p ON p.book_id=b.book_id AND p.provider=? AND p.metric=?
                                WHERE b.language=? AND {sql} AND p.observed_at >= ?""", (provider, metric, language, *params, fresh)).fetchone()[0]
        answer = eligible > 0 and covered >= eligible
        self._complete_cache[key] = (time.monotonic() + 60, answer)
        return answer

    def percentile(self, book_id, *, provider="gutendex", metric="download_count", max_age_days=14):
        """{'rank', 'total', 'top_percent', 'value', 'observed_at'} for one book - only from a complete, fresh population of its
        own language; otherwise None (unranked)."""
        row = self._conn().execute("SELECT language, value, observed_at, status FROM popularity_latest WHERE book_id=? AND provider=? AND metric=?",
                                   (book_id, provider, metric)).fetchone()
        if not row or row[3] != "ok" or row[1] is None or row[2] < self._clock() - max_age_days * 86400:
            return None
        language, value, observed, _status = row
        if not self.complete(language, provider=provider, metric=metric, max_age_days=max_age_days):
            return None
        sql, params = self._eligible_sql("b")
        base = f"""FROM popularity_latest p JOIN books b ON b.book_id=p.book_id
                   WHERE p.provider=? AND p.metric=? AND p.language=? AND p.status='ok' AND p.value IS NOT NULL AND {sql}"""
        c = self._conn()
        total = c.execute(f"SELECT COUNT(*) {base}", (provider, metric, language, *params)).fetchone()[0]
        above = c.execute(f"SELECT COUNT(*) {base} AND p.value > ?", (provider, metric, language, *params, value)).fetchone()[0]
        if not total:
            return None
        rank = above + 1                                     # ties share a competition rank
        return {"rank": rank, "total": total, "top_percent": max(1, min(100, math.ceil(rank * 100 / total))),
                "value": int(value), "observed_at": observed, "language": language}

    def prune(self, max_age_days=90):
        with self._tx() as c:
            c.execute("DELETE FROM popularity_snapshots WHERE observed_at < ?", (self._clock() - max_age_days * 86400,))
            c.execute("DELETE FROM sync_runs WHERE started_at < ?", (self._clock() - max_age_days * 86400,))

    # ---- provider runs -----------------------------------------------------------------------------------------
    def start_run(self, provider):
        with self._tx() as c:
            return c.execute("INSERT INTO sync_runs (provider, started_at, state) VALUES (?,?,?)", (provider, self._clock(), "running")).lastrowid

    def finish_run(self, run_id, state, *, records=None, completeness=None, retries=0, error_class=None, error=None, generation=None,
                   source_hash=None, etag=None, last_modified=None):
        with self._tx() as c:
            c.execute("""UPDATE sync_runs SET finished_at=?, state=?, records=?, completeness=?, retries=?, error_class=?, error=?, generation=?,
                         source_hash=COALESCE(?, source_hash), etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) WHERE id=?""",
                      (self._clock(), state, records, completeness, retries, error_class, (error or None) and str(error)[:300], generation,
                       source_hash, etag, last_modified, run_id))

    def last_run(self, provider, states=None):
        sql, params = ("SELECT id, started_at, finished_at, state, records, completeness, retries, error_class, error, generation, source_hash, "
                       "etag, last_modified FROM sync_runs WHERE provider=?"), [provider]
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params += list(states)
        row = self._conn().execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        keys = ("id", "started_at", "finished_at", "state", "records", "completeness", "retries", "error_class", "error", "generation",
                "source_hash", "etag", "last_modified")
        return dict(zip(keys, row)) if row else None

    def provider_status(self, stale_after=1800):
        """Per provider: the newest attempt, the last GOOD run and its age. A 'running' row that never finished is reported failed."""
        now, out = self._clock(), {}
        providers = [r[0] for r in self._conn().execute("SELECT DISTINCT provider FROM sync_runs")]
        for provider in providers:
            attempt, good = self.last_run(provider), self.last_run(provider, ("ok", "unchanged"))
            if attempt and attempt["state"] == "running" and now - attempt["started_at"] > stale_after:
                attempt = {**attempt, "state": "failed", "error": "The run stopped before finishing."}
            out[provider] = {"last_attempt": attempt, "last_good": good, "age_seconds": None if not good else round(now - (good["finished_at"] or good["started_at"]))}
        return out
