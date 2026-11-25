"""Keeps the catalogue index fresh: download pg_catalog.csv.gz, validate it, update the Book Registry, rebuild universe.sqlite
from the Registry, and tell everyone how far along it is. Also the one coordinator of the other background providers
(top lists, Gutendex enrichment): each has its own schedule and timestamps, and one failing never blocks another.

* The first run (no index yet) happens right away; the page shows the librarian's catalogue cart with this class's
  REAL progress (bytes downloaded, rows read).
* After that the catalogue is refreshed in the background once a day; readers keep using the old index until the
  new one is complete (atomic swap).
* A failed or REJECTED attempt (damaged, partial or suspicious file) is reported honestly and retried later; the Registry and
  the live index keep their last good data. An unchanged file (same hash) rebuilds nothing.
* Status is also written to a small JSON file, so a separate worker process and the web process agree.
"""
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import catalogue_csv
import universe as universe_mod

log = logging.getLogger("literary_roulette.catalogue")
STALE_LOCK_SECONDS = 30 * 60


def _parse_utc(text):
    try:
        import calendar
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


class CatalogueSync:
    def __init__(self, runtime, status_path, *, csv_path=None, urls=catalogue_csv.CATALOG_URLS,
                 opener=None, max_age_hours=24.0, retry_seconds=300.0, clock=time.time):
        self.rt = runtime
        self.status_path = Path(status_path)
        self.lock_path = self.status_path.with_suffix(".lock")
        self.download_path = self.status_path.parent / "pg_catalog.csv.gz"
        self.csv_path = Path(csv_path) if csv_path else None        # a local copy to use instead of downloading
        self.urls, self.opener = urls, opener
        self.max_age, self.retry_seconds, self._clock = float(max_age_hours) * 3600, float(retry_seconds), clock
        self._lock = threading.Lock()
        self._status = {"state": "idle", "message": "", "percent": None, "error": None, "running": False, "updated": 0}
        self._stop = threading.Event()
        self._thread = None

    # ---- status ------------------------------------------------------------------------------
    def _set(self, **changes):
        with self._lock:
            self._status.update(changes, updated=self._clock())
            snapshot = dict(self._status)
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_name(self.status_path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(snapshot), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError:
            pass

    def status(self):
        """This process's own status while it is working; otherwise whatever another process last wrote."""
        with self._lock:
            mine = dict(self._status)
        if mine["running"]:
            return mine
        try:
            other = json.loads(self.status_path.read_text(encoding="utf-8"))
            if isinstance(other, dict) and other.get("updated", 0) >= mine["updated"]:
                if other.get("running") and self._clock() - other.get("updated", 0) > STALE_LOCK_SECONDS:
                    other["running"], other["state"] = False, "failed"
                return other
        except (OSError, ValueError):
            pass
        return mine

    # ---- freshness ---------------------------------------------------------------------------
    def age_seconds(self):
        stamp = _parse_utc(self.rt.universe.generated_at) if self.rt.universe.available else None
        return None if stamp is None else max(0.0, self._clock() - stamp)

    def needs_run(self):
        age = self.age_seconds()
        return age is None or age > self.max_age

    # ---- one sync ----------------------------------------------------------------------------
    def _claim(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return lambda: self.lock_path.unlink(missing_ok=True)
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > STALE_LOCK_SECONDS:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                return None
        return None

    def _file_hash(self, path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def rebuild_index(self, progress=None):
        """Rebuild universe.sqlite from the Registry's eligible books (no download). Used after every catalogue import and after an
        admin activates or approves a book, because only a rebuild can ADD books to the index (pausing one takes effect at once)."""
        rows = {language: list(self.rt.registry.eligible_rows(language)) for language in universe_mod.LANGUAGES}
        counts = universe_mod.build(rows, self.rt.universe_path, blocked=self.rt.universe.blocked, log=log.info, progress=progress)
        self.rt.universe.reload(force=True)
        return counts

    def rebuild_in_background(self):
        threading.Thread(target=self._safe_rebuild, name="lr-rebuild", daemon=True).start()

    def _safe_rebuild(self):
        try:
            self.rebuild_index()
        except Exception:
            log.exception("Index rebuild failed; the previous index stays live")

    def run(self):
        """Download (or read the local copy), validate, update the Registry, rebuild the index. Blocking. True when a fresh index is live."""
        release = self._claim()
        if release is None:
            return False                                   # another process is already doing it
        registry, run_id = self.rt.registry, None
        def progress(message, percent=None):
            self._set(state="running", running=True, message=message, percent=None if percent is None else int(percent), error=None)
        def fresh(state, **kw):
            registry.finish_run(run_id, state, generation=self.rt.universe.generated_at, **kw)
            self._set(state="ready", running=False, percent=100, error=None, message="Gutenberg catalogue is up to date.")
            return True
        try:
            run_id = registry.start_run("gutenberg_csv")
            progress("Connecting to Project Gutenberg catalogue\u2026", 1)
            if self.csv_path and self.csv_path.exists():
                path = self.csv_path
                progress("Using your saved catalogue file\u2026", 55)
            else:
                try:
                    options = {"opener": self.opener} if self.opener else {}
                    path = catalogue_csv.download(self.download_path, progress, urls=self.urls, **options)
                except catalogue_csv.NotModified:
                    if self.rt.universe.available:             # unchanged since last time: just record that it is fresh
                        universe_mod.touch(self.rt.universe_path)
                        self.rt.universe.reload(force=True)
                        return fresh("unchanged")
                    path = self.download_path                    # (index missing but the file is there: rebuild from it)
            digest = self._file_hash(path)
            good = registry.last_run("gutenberg_csv", ("ok",))
            if good and good["source_hash"] == digest and self.rt.universe.available and registry.count():
                universe_mod.touch(self.rt.universe_path)        # same bytes as the last good import: nothing to rebuild
                self.rt.universe.reload(force=True)
                return fresh("unchanged", source_hash=digest)
            stats = {}
            records = catalogue_csv.read_records(path, progress, stats)
            previous = dict(self.rt.universe.counts) if self.rt.universe.available else None
            catalogue_csv.validate_records(records, stats, previous)               # raises ValidationError: nothing is replaced
            progress("Updating the Book Registry\u2026", 78)
            summary = registry.import_gutenberg(records, blocked=self.rt.universe.blocked)
            counts = self.rebuild_index(progress)
            total = sum(counts.values())
            registry.finish_run(run_id, "ok", records=len(records), completeness=1.0, generation=self.rt.universe.generated_at, source_hash=digest)
            log.info("Registry import: %s", summary)
            self._set(state="ready", running=False, message=f"Gutenberg catalogue ready: {total:,} entries indexed.", percent=100, error=None)
            return True
        except Exception as exc:                           # report honestly, keep reading working, retry later
            log.warning("Catalogue sync failed: %s", exc)
            rejected = isinstance(exc, catalogue_csv.ValidationError)
            if run_id is not None:
                registry.finish_run(run_id, "rejected" if rejected else "failed", error_class="validation" if rejected else type(exc).__name__, error=exc)
            self._set(state="failed", running=False, percent=None, error=str(exc),
                      message="The catalogue could not be opened right now." if not rejected else "The new catalogue file was rejected; the previous one stays live.")
            return False
        finally:
            release()

    # ---- background loop ---------------------------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="lr-catalogue-sync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        """One coordinator thread. Each provider task is independent: its own interval, its own recorded timestamps, its own
        try/except. Returns to sleep for at most an hour."""
        while not self._stop.is_set():
            wait = self.max_age
            for task in (self._task_scores, self._task_catalogue, self._task_gutendex, self._task_housekeeping):
                try:
                    wait = min(wait, task())
                except Exception:
                    log.exception("Coordinator task %s failed", task.__name__)
                    wait = min(wait, self.retry_seconds)
            self._stop.wait(min(max(wait, 30.0), 3600))

    def _task_scores(self):
        if getattr(self.rt, "popularity", None):              # best effort: the badge data refreshes about daily, never blocks anything
            self.rt.popularity.refresh_if_stale()
        return 3600.0

    def _task_catalogue(self):
        if self.needs_run():
            return self.max_age if self.run() else self.retry_seconds
        return max(60.0, self.max_age - (self.age_seconds() or 0))

    def _task_gutendex(self):
        provider = getattr(self.rt, "gutendex", None)
        if provider is None or not provider.enabled or not self.rt.universe.available:
            return 3600.0
        if provider.due():
            provider.run()
        return provider.interval

    def _task_housekeeping(self):
        self.rt.registry.prune()
        return 24 * 3600.0
