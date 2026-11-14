"""The universe: every eligible Project Gutenberg edition, per language, in one small SQLite file.

Built once from the official RDF snapshot (tools/sync_gutenberg_rdf.py), never per visitor. Each row has a
random position `n` (a fixed shuffle made at build time), so "draw number n" is a uniform random draw and an
O(1) lookup, with nothing held in memory. Full texts are NOT here: only metadata. The text of a drawn
edition is fetched by the background worker (prep.py) and kept in the lake (shelf.py).
"""
from __future__ import annotations

import bisect
import json
import math
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

import source

LANGUAGES = ("en", "fr", "zh")
LANGUAGE_LABELS = {"en": "English", "fr": "French", "zh": "Chinese"}
SCHEMA_VERSION = 3


def _rank(records):
    # Use binary search so ranking thousands of catalogue entries stays fast.
    values = sorted(max(0, int(r.get("download_count") or 0)) for r in records)
    total = len(values)
    for record in records:
        downloads = max(0, int(record.get("download_count") or 0))
        rank = 1 + total - bisect.bisect_right(values, downloads)  # ties share competition rank
        record["popularity_rank"] = rank
        record["popularity_total"] = total
        record["popularity_top_percent"] = max(1, min(100, math.ceil(rank * 100 / total))) if total else 100


def _popularity_text(language, tier, rank, total, downloads):
    """Reader-facing ranking text in the reader's language (en, fr, zh)."""
    if language == "fr":
        n = lambda v: f"{v:,}".replace(",", "\u202f")
        label = {
            "jackpot": "Jackpot ! Un favori du top 1 % de Project Gutenberg !",
            "lucky": "Belle trouvaille ! Parmi les 10 % les plus téléchargés !",
            "hidden-gem": "Perle rare ! Parmi les 10 % les moins téléchargés — envie d’explorer ?",
            "discovery": f"Une découverte · n° {n(rank)} par téléchargements sur les 30 derniers jours",
        }[tier]
        detail = (f"Rang n° {n(rank)} sur {n(total)} titres français indexés · "
                  f"environ {n(downloads)} téléchargements sur les 30 derniers jours")
        note = ("D’après les téléchargements approximatifs de Project Gutenberg sur les 30 derniers jours ; "
                "il s’agit de popularité, pas de qualité littéraire.")
    elif language == "zh":
        label = {
            "jackpot": "头奖！Project Gutenberg 前 1% 的热门读物！",
            "lucky": "幸运发现！下载量位列前 10%！",
            "hidden-gem": "遗珠！下载量最低的 10% 之一——想探索一下吗？",
            "discovery": f"新发现 · 近 30 天下载量排名第 {rank:,} 位",
        }[tier]
        detail = f"在已收录的 {total:,} 种中文书籍中排名第 {rank:,} · 近 30 天约 {downloads:,} 次下载"
        note = "依据 Project Gutenberg 近 30 天的大致下载量；这代表受欢迎程度，而非文学价值。"
    else:
        label = {
            "jackpot": "Jackpot! A top 1% Gutenberg favourite!",
            "lucky": "Lucky find! Top 10% most downloaded!",
            "hidden-gem": "Hidden gem alert! Among the least-downloaded 10% — would you like to explore?",
            "discovery": f"A new discovery · ranked #{rank:,} by downloads in the last 30 days",
        }[tier]
        name = LANGUAGE_LABELS.get(language, language)
        detail = f"Rank #{rank:,} of {total:,} indexed {name} titles · {downloads:,} approximate downloads in the last 30 days"
        note = ("Based on Project Gutenberg's approximate downloads in the previous 30 days; "
                "this is popularity, not literary quality.")
    return label, detail, note


def popularity_payload(language, gid, rank, total, pct, downloads, snapshot_date, partial=False):
    if partial:
        return None
    pct = max(1, int(pct or 100))
    total = int(total or 0)
    if total >= 100 and pct <= 1:
        tier = "jackpot"
    elif total >= 100 and pct <= 10:
        tier = "lucky"
    elif total >= 100 and pct >= 91:
        tier = "hidden-gem"
    else:
        tier = "discovery"
    label, detail, note = _popularity_text(language, tier, int(rank or 0), total, int(downloads or 0))
    return {"tier": tier, "label": label, "detail": detail, "note": note, "rank": int(rank or 0), "total": total,
            "downloads_30d": int(downloads or 0), "top_percent": pct, "snapshot_date": snapshot_date,
            "source_url": f"https://www.gutenberg.org/ebooks/{gid}"}


def _clean_title(title):
    parts = [p.strip() for p in str(title).replace("\r", "").split("\n") if p.strip()]
    return ": ".join(parts) if parts else str(title).strip()


def _split_list(text, limit):
    return [p.strip() for p in str(text or "").replace("--", " \u2013 ").split(";") if p.strip()][:limit]


def text_urls_for(gid):
    """Where to find one book's plain text, in the order the desktop app tries them."""
    gid = int(gid)
    return [f"https://dev.gutenberg.org/cache/epub/{gid}/pg{gid}.txt", f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
            f"https://www.gutenberg.org/files/{gid}/{gid}-0.txt", f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8"]


def build_from_records(records, db_path, *, blocked=(), seed=None, log=print, progress=None, generated_at=None):
    """Turn catalogue records (catalogue_csv.read_records) into universe.sqlite atomically. Returns {language: count}.

    A book belongs to the first language Gutenberg lists for it. Chinese titles are converted to Simplified when
    OpenCC is installed. Blocked IDs are left out. The old file stays in use until the new one is complete.
    """
    db_path = Path(db_path)
    rng, blocked = random.Random(seed), {int(b) for b in blocked}
    convert = None
    try:
        from opencc import OpenCC
        convert = OpenCC("t2s").convert
    except ImportError:
        log("OpenCC is not installed; Chinese titles stay as published.")
    by_lang = {code: [] for code in LANGUAGES}
    for r in records:
        first = str(r.get("languages") or "").replace(",", ";").split(";")[0].strip().lower()
        if first in by_lang and int(r["gutenberg_id"]) not in blocked:
            by_lang[first].append(r)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + f".{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            CREATE TABLE books (lang TEXT NOT NULL, n INTEGER NOT NULL, gid INTEGER NOT NULL, title TEXT, author TEXT,
                genres TEXT, rank INTEGER, total INTEGER, pct INTEGER, downloads INTEGER, text_url TEXT,
                PRIMARY KEY (lang, n));
            CREATE UNIQUE INDEX books_gid ON books (lang, gid);
            CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT);
        """)
        counts = {}
        for language in LANGUAGES:
            rows = by_lang[language]
            rng.shuffle(rows)                                    # the fixed shuffle that makes `n` a uniform draw
            batch = []
            for n, r in enumerate(rows):
                gid = int(r["gutenberg_id"])
                title = _clean_title(r.get("title"))
                author = "; ".join(_split_list(r.get("authors"), 3)) or "Author not listed"
                genres = _split_list(r.get("bookshelves"), 3) or _split_list(r.get("subjects"), 2)
                if language == "zh" and convert:
                    title, author, genres = convert(title), convert(author), [convert(g) for g in genres]
                batch.append((language, n, gid, title, author, json.dumps(genres, ensure_ascii=False), 0, len(rows), 100, 0,
                              text_urls_for(gid)[1]))
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


class Universe:
    """Read-only view of universe.sqlite. Cheap to create; safe across threads; reloads when the file changes."""

    def __init__(self, db_path, blocklist_path=None):
        self.path, self.blocklist_path = Path(db_path), Path(blocklist_path) if blocklist_path else None
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
    def _record(self, language, row):
        _lang, n, gid, title, author, genres, rank, total, pct, downloads, text_url = row
        popularity = popularity_payload(language, gid, rank, total, pct, downloads, self.generated_at, self.partial) if rank else None
        return {
            "id": f"pg-{language}-{gid}", "title": title, "author": author, "genres": json.loads(genres or "[]"),
            "description": "", "language": language, "gutenberg_id": gid, "draw_number": n + 1,
            "rights_status": "public_domain_candidate", "source_provider": "Project Gutenberg",
            "source_url": f"https://www.gutenberg.org/ebooks/{gid}",
            "source_credit": f"Text source: Project Gutenberg eBook #{gid}",
            "source_license_note": "Project Gutenberg records this title as public domain in the USA. Copyright status can differ by country.",
            "text_url": text_url, "text_urls": text_urls_for(gid), "popularity": popularity,
        }

    _COLUMNS = "lang, n, gid, title, author, genres, rank, total, pct, downloads, text_url"

    def get(self, language, gid):
        row = self._conn().execute(f"SELECT {self._COLUMNS} FROM books WHERE lang=? AND gid=?", (language, int(gid))).fetchone()
        return self._record(language, row) if row else None

    def popularity(self, language, gid):
        record = self.get(language, gid)
        return record["popularity"] if record else None

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
            if not row or int(row[2]) in self.blocked:
                continue
            record = self._record(language, row)
            if reject and reject(record):
                continue
            picked.append(record)
        return picked
