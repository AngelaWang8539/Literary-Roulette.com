"""Keeps the catalogue index fresh: download pg_catalog.csv.gz, read it, rebuild universe.sqlite, tell everyone how far along it is.

* The first run (no index yet) happens right away; the page shows the librarian's catalogue cart with this class's
  REAL progress (bytes downloaded, rows read).
* After that the catalogue is refreshed in the background once a day; readers keep using the old index until the
  new one is complete (atomic swap).
* A failed attempt is reported honestly and retried later; it never takes down reading.
* Status is also written to a small JSON file, so a separate worker process and the web process agree.
"""
import json
import logging
import os
import shutil
import threading
import time
import urllib.request
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
                 opener=urllib.request.urlopen, max_age_hours=24.0, retry_seconds=300.0, clock=time.time):
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

    def run(self):
        """Download (or read the local copy), index, publish. Blocking. Returns True when a fresh index is live."""
        release = self._claim()
        if release is None:
            return False                                   # another process is already doing it
        def progress(message, percent=None):
            self._set(state="running", running=True, message=message, percent=None if percent is None else int(percent), error=None)
        try:
            progress("Connecting to Project Gutenberg catalogue\u2026", 1)
            if self.csv_path and self.csv_path.exists():
                path = self.csv_path
                progress("Using your saved catalogue file\u2026", 55)
            else:
                path = catalogue_csv.download(self.download_path, progress, urls=self.urls, opener=self.opener)
            records = catalogue_csv.read_records(path, progress)
            counts = universe_mod.build_from_records(records, self.rt.universe_path, blocked=self.rt.universe.blocked,
                                                     log=log.info, progress=progress)
            self.rt.universe.reload(force=True)
            self.rt.attach_popularity()
            total = sum(counts.values())
            self._set(state="ready", running=False, message=f"Gutenberg catalogue ready: {total:,} entries indexed.", percent=100, error=None)
            return True
        except Exception as exc:                           # report honestly, keep reading working, retry later
            log.warning("Catalogue sync failed: %s", exc)
            self._set(state="failed", running=False, percent=None, error=str(exc),
                      message="The catalogue could not be opened right now.")
            return False
        finally:
            if self.download_path.exists() and not (self.csv_path and self.csv_path.exists()):
                pass                                       # keep the small .gz: a re-read after a crash needs no download
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
        while not self._stop.is_set():
            wait = self.max_age
            try:
                if self.needs_run():
                    ok = self.run()
                    wait = self.max_age if ok else self.retry_seconds
                else:
                    age = self.age_seconds() or 0
                    wait = max(60.0, self.max_age - age)
            except Exception:
                log.exception("Catalogue loop error")
                wait = self.retry_seconds
            self._stop.wait(min(wait, 3600))
