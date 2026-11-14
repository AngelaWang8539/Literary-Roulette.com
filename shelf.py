"""The shared ready shelf: prepared books that any visitor can draw from, many times over.

Each prepared book is two files per language folder:
  <id>.<hash8>.txt   all passages back to back (UTF-8)
  <id>.meta.json     the ready marker: passage byte offsets, edition key, content hash, segment version

Meta is written last and replaced atomically, so a half-written book is never "ready". Reading one
passage is a seek + read: no full-book parsing and nothing held in memory but the offsets.
Several processes (web workers, the background worker) may share one shelf folder.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
INTEREST_WEIGHTS = {"displayed": 1.0, "another": 2.0, "reveal": 2.0, "saved": 5.0, "link": 5.0}


def safe_name(book_id):
    return SAFE_CHARS.sub("_", book_id)


class ReadyShelf:
    def __init__(self, root, languages=("en", "fr", "zh"), scan_interval=2.0):
        self.root, self.languages = Path(root), tuple(languages)
        self.scan_interval = scan_interval
        self.version = 0
        self._lock = threading.RLock()
        self._index = {lang: {} for lang in self.languages}     # lang -> {book_id: meta}
        self._ids = {lang: () for lang in self.languages}
        self._stamps = {}
        self._next_check = 0.0
        self._interest = {}                                      # (lang, book_id) -> [score, last_seen]
        self.scan()

    # ---- discovery ---------------------------------------------------------------------------
    def _folder(self, language):
        return self.root / language

    def _valid_meta(self, folder, meta):
        try:
            path = folder / meta["file"]
            return (isinstance(meta.get("book_id"), str) and path.is_file()
                    and path.stat().st_size == meta["bytes"] and len(meta["offsets"]) == meta["passages"] + 1
                    and meta["passages"] > 0)
        except (KeyError, TypeError, OSError):
            return False

    def scan(self):
        """Rebuild the index from disk. Safe to call at any time."""
        changed = False
        with self._lock:
            for lang in self.languages:
                folder = self._folder(lang)
                found = {}
                try:
                    stamp = folder.stat().st_mtime_ns
                    names = sorted(folder.glob("*.meta.json"))
                except OSError:
                    stamp, names = None, []
                for path in names:
                    try:
                        meta = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(meta, dict) and self._valid_meta(folder, meta):
                        found[meta["book_id"]] = meta
                signature = {k: (m["content_hash"], m["segment_version"]) for k, m in found.items()}
                old = {k: (m["content_hash"], m["segment_version"]) for k, m in self._index[lang].items()}
                if signature != old:
                    changed = True
                self._index[lang] = found
                self._ids[lang] = tuple(sorted(found))
                self._stamps[lang] = stamp
            if changed:
                self.version += 1
        return changed

    def refresh_if_changed(self):
        """Cheap check (one stat per language, throttled) for books published by another process."""
        now = time.monotonic()
        if now < self._next_check:
            return False
        self._next_check = now + self.scan_interval
        for lang in self.languages:
            try:
                stamp = self._folder(lang).stat().st_mtime_ns
            except OSError:
                stamp = None
            if stamp != self._stamps.get(lang):
                return self.scan()
        return False

    # ---- reading -----------------------------------------------------------------------------
    def ids(self, language):
        return self._ids.get(language, ())

    def count(self, language):
        return len(self._ids.get(language, ()))

    def has(self, language, book_id):
        return book_id in self._index.get(language, {})

    def meta(self, language, book_id):
        return self._index.get(language, {}).get(book_id)

    def passage_count(self, language, book_id):
        meta = self.meta(language, book_id)
        return meta["passages"] if meta else 0

    def read_passage(self, language, book_id, index):
        """One passage by number, or None if the book or passage is missing."""
        meta = self.meta(language, book_id)
        if not meta or not 0 <= index < meta["passages"]:
            return None
        start, end = meta["offsets"][index], meta["offsets"][index + 1]
        try:
            with (self._folder(language) / meta["file"]).open("rb") as handle:
                handle.seek(start)
                return handle.read(end - start).decode("utf-8")
        except OSError:
            self.scan()          # the file vanished (replaced or retired by another process)
            return None

    def is_current(self, language, book_id, content_hash, segment_version):
        meta = self.meta(language, book_id)
        return bool(meta and meta["content_hash"] == content_hash and meta["segment_version"] == segment_version)

    def external_ids(self, language):
        return [k for k, m in self._index.get(language, {}).items() if m.get("external")]

    # ---- writing -----------------------------------------------------------------------------
    def publish(self, language, book_id, passages, *, edition_key, content_hash, segment_version,
                external=False, book=None):
        """Write a prepared book and only then mark it ready. Returns the meta."""
        if not passages:
            raise ValueError("Cannot publish a book with no passages.")
        folder = self._folder(language)
        folder.mkdir(parents=True, exist_ok=True)
        safe = safe_name(book_id)
        data_name = f"{safe}.{content_hash[:8]}.{segment_version}.txt"
        blobs = [p.encode("utf-8") for p in passages]
        offsets, total = [0], 0
        for blob in blobs:
            total += len(blob)
            offsets.append(total)
        pid = f"{os.getpid()}.{threading.get_ident()}"
        tmp_data = folder / f".{data_name}.{pid}.tmp"
        with tmp_data.open("wb") as handle:
            for blob in blobs:
                handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_data, folder / data_name)
        meta = {
            "book_id": book_id, "language": language, "file": data_name, "bytes": total, "passages": len(passages),
            "offsets": offsets, "edition_key": edition_key, "content_hash": content_hash,
            "segment_version": segment_version, "prepared_at": time.time(), "external": bool(external),
            "book": book if external else None,
        }
        meta_path = folder / f"{safe}.meta.json"
        previous = self.meta(language, book_id)
        tmp_meta = folder / f".{safe}.meta.{pid}.tmp"
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_meta, meta_path)               # <- the moment the book becomes ready
        with self._lock:
            self._index[language][book_id] = meta
            self._ids[language] = tuple(sorted(self._index[language]))
            self.version += 1
        if previous and previous.get("file") != data_name:
            try:
                (folder / previous["file"]).unlink()
            except OSError:
                pass
        return meta

    def retire(self, language, book_id):
        """Remove a prepared copy (cache policy or stale rights). The only thing removed is this shelf copy."""
        meta = self.meta(language, book_id)
        folder, safe = self._folder(language), safe_name(book_id)
        try:
            (folder / f"{safe}.meta.json").unlink()
        except OSError:
            pass
        if meta:
            try:
                (folder / meta["file"]).unlink()
            except OSError:
                pass
        with self._lock:
            self._index[language].pop(book_id, None)
            self._ids[language] = tuple(sorted(self._index[language]))
            self.version += 1

    # ---- interest and retention --------------------------------------------------------------
    def note_interest(self, language, book_id, kind):
        weight = INTEREST_WEIGHTS.get(kind, 0.0)
        if weight:
            with self._lock:
                entry = self._interest.setdefault((language, book_id), [0.0, 0.0])
                entry[0] += weight
                entry[1] = time.time()

    def interest_score(self, language, book_id):
        entry = self._interest.get((language, book_id))
        return entry[0] if entry else 0.0

    def evict_cold(self, language, max_cold):
        """Keep at most `max_cold` on-demand books; retire the OLDEST first. Age only: interest never decides
        what stays, so the lake remains an unbiased sample of the catalogue."""
        cold = self.external_ids(language)
        excess = len(cold) - max(0, int(max_cold))
        if excess <= 0:
            return []
        victims = sorted(cold, key=lambda bid: self._index[language][bid]["prepared_at"])[:excess]
        for bid in victims:
            self.retire(language, bid)
        return victims
