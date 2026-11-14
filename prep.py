"""Background preparation: fetch -> validate -> clean -> segment -> store -> publish.

The worker owns every slow thing. A visitor request never waits on it.

* Jobs are keyed (language, book_id, edition_key, revision); the same key is queued once, and a
  lock file stops two processes preparing the same edition at the same time.
* Downloads share one bounded concurrency limit and a minimum gap, whatever the number of visitors.
* Transient failures retry later with exponential back-off and jitter; permanent ones (404, rights,
  unreadable text) are remembered and not retried.
* A book becomes ready only after it is fully written and verified (ReadyShelf.publish).
* The LIBRARY (the "lake"): every book prepared so far, kept for good and shared by everyone. Each spin is a uniform
  draw from the WHOLE catalogue (universe.py); a book already in the library is instant, a cold one is prepared once
  (visitors wait with the librarian) and then reused by every later reader. Waves of random picks (3, 5, 8, 12 ...)
  grow the library in the background within the source's courtesy limits, up to `library_target` books per language
  ("all" in production). Under overload the worker sheds cold picks (see estimated_wait) instead of queueing forever.
"""
import hashlib
import heapq
import json
import logging
import os
import random
import threading
import time
from pathlib import Path

import segment
import source

log = logging.getLogger("literary_roulette.prep")
LOCAL, COLD = "local", "cold"
STALE_LOCK_SECONDS = 15 * 60


class Settings:
    """Library and source-courtesy knobs."""

    def __init__(self, **kw):
        self.library_target = int(kw.get("library_target", 300))     # prepared books wanted per language (10**9 = all)
        self.first_wave = int(kw.get("first_wave", 3))
        self.max_wave = int(kw.get("max_wave", 12))
        self.max_cold_wait = float(kw.get("max_cold_wait", 12.0))     # seconds of queue a visitor may be asked to wait
        self.workers = int(kw.get("workers", 3))
        self.source_concurrency = int(kw.get("source_concurrency", 2))
        self.min_interval = float(kw.get("min_interval", 2.0))       # seconds between downloads (Gutenberg asks for ~2 s)
        self.max_attempts = int(kw.get("max_attempts", 4))
        self.backoff_base = float(kw.get("backoff_base", 20.0))
        self.backoff_cap = float(kw.get("backoff_cap", 900.0))
        self.check_interval = float(kw.get("check_interval", 3.0))
        self.failed_keep = int(kw.get("failed_keep", 300))
        self.replenish = bool(kw.get("replenish", True))


def edition_key_for(book):
    gid = book.get("gutenberg_id")
    return f"pg-{gid}-plain-text" if gid else f"local-{book['id']}"


class Job:
    def __init__(self, key, kind, language, book, priority):
        self.key, self.kind, self.language, self.book, self.priority = key, kind, language, book, priority
        self.state, self.attempts, self.error = "queued", 0, None
        self.queued_at, self.not_before = time.time(), 0.0
        self.stage, self.percent = "queued", 6


class PrepWorker:
    def __init__(self, shelf, catalogues, universe, state_dir, settings=None, *, fetcher=None, sleep=time.sleep,
                 rng=None, clock=time.time):
        self.shelf, self.catalogues, self.universe = shelf, catalogues, universe
        self.settings = settings or Settings()
        self.state_dir = Path(state_dir)
        self.lock_dir = self.state_dir / ".locks"
        self.failed_path = self.state_dir / "failed.json"
        self._fetch = fetcher or source.fetch_clean_text
        self._fetch_reports_progress = fetcher is None
        self.wave = {lang: self.settings.first_wave for lang in catalogues}
        self._sleep, self._clock = sleep, clock
        self.rng = rng or random.Random()
        self.gate = source.RateGate(self.settings.min_interval)
        self._downloads = threading.BoundedSemaphore(max(1, self.settings.source_concurrency))
        self._cv = threading.Condition()
        self._heap, self._delayed, self._seq = [], [], 0
        self._jobs = {}                 # key -> Job (queued / running / waiting retry)
        self._failed = self._load_failed()
        self._stop = threading.Event()
        self._threads = []
        self._busy = 0
        self.completed = 0

    # ---- bookkeeping -------------------------------------------------------------------------
    def _load_failed(self):
        try:
            value = json.loads(self.failed_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_failed(self):
        if len(self._failed) > self.settings.failed_keep:      # never let the list pile up
            newest = sorted(self._failed.items(), key=lambda kv: kv[1].get("at", 0))[-self.settings.failed_keep:]
            self._failed = dict(newest)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.failed_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._failed, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.failed_path)
        except OSError as exc:
            log.warning("Could not save failed-job list: %s", exc)

    @staticmethod
    def job_key(language, book_id, edition_key, revision="1"):
        return f"{language}|{book_id}|{edition_key}|{revision}"

    def _claim(self, key):
        """Cross-process lock for one edition. Returns a release callable, or None if someone else has it."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        path = self.lock_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".lock")
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return lambda: path.unlink(missing_ok=True)
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS:
                        path.unlink(missing_ok=True)      # a crashed worker left it behind
                        continue
                except OSError:
                    pass
                return None
        return None

    # ---- queueing ----------------------------------------------------------------------------
    def enqueue(self, kind, language, book, priority=5):
        """Queue one preparation. Returns False when the same edition is already queued, running or failed for good."""
        key = self.job_key(language, book["id"], edition_key_for(book))
        with self._cv:
            if key in self._jobs:
                return False
            failure = self._failed.get(key)
            if failure and failure.get("permanent"):
                return False
            job = Job(key, kind, language, book, priority)
            self._jobs[key] = job
            self._push(job)
            self._cv.notify()
        return True

    def _push(self, job):
        self._seq += 1
        if job.not_before > self._clock():
            self._delayed.append((job.not_before, self._seq, job.key))       # a retry that is not due yet
        else:
            heapq.heappush(self._heap, (job.priority, self._seq, job.key))

    def seed_local(self):
        """Queue every verified local text whose prepared copy is missing or stale. Returns how many."""
        queued = 0
        for language, cat in self.catalogues.items():
            version = segment.segment_version(language)
            for bid, info in cat.local_texts.items():
                if self.shelf.is_current(language, bid, info["sha256"], version):
                    continue
                if self.enqueue(LOCAL, language, cat.books[bid], priority=0):
                    queued += 1
        return queued

    # ---- running -----------------------------------------------------------------------------
    def start(self, threads=None, scheduler=True):
        if self._threads:
            return
        self._stop.clear()
        for i in range(threads or self.settings.workers):
            t = threading.Thread(target=self._loop, name=f"lr-prep-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        if scheduler:
            sched = threading.Thread(target=self._scheduler, name="lr-prep-scheduler", daemon=True)
            sched.start()
            self._threads.append(sched)

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []

    def _next_job(self, wait):
        with self._cv:
            deadline = time.monotonic() + wait
            while not self._stop.is_set():
                now = self._clock()
                if self._delayed:
                    due = [d for d in self._delayed if d[0] <= now]
                    if due:
                        self._delayed = [d for d in self._delayed if d[0] > now]
                        for _when, seq, key in due:
                            queued = self._jobs.get(key)
                            if queued:
                                heapq.heappush(self._heap, (queued.priority, seq, key))
                while self._heap:
                    _prio, _seq, key = heapq.heappop(self._heap)
                    job = self._jobs.get(key)
                    if job and job.state == "queued":
                        job.state = "running"
                        self._busy += 1
                        return job
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                nap = remaining
                if self._delayed:
                    nap = min(nap, max(0.05, min(d[0] for d in self._delayed) - now))
                self._cv.wait(timeout=nap)
        return None

    def run_pending(self, limit=1000):
        """Run queued jobs in the calling thread (tests, tools/prepare_shelf.py). Returns jobs processed."""
        done = 0
        while done < limit:
            job = self._next_job(wait=0)
            if job is None:
                break
            self._process(job)
            done += 1
        return done

    def _loop(self):
        while not self._stop.is_set():
            job = self._next_job(wait=1.0)
            if job:
                self._process(job)

    def _process(self, job):
        release = self._claim(job.key)
        try:
            if release is None:                 # another process is preparing this edition right now
                self._finish(job, requeue_after=20.0, count_attempt=False)
                return
            try:
                job.attempts += 1
                self._prepare(job)
                self._finish(job, done=True)
            except source.SourceError as exc:
                job.error = str(exc)
                self._finish(job, permanent=exc.permanent)
            except Exception as exc:            # a bug or odd text: record it, never kill the worker
                job.error = f"{type(exc).__name__}: {exc}"
                log.exception("Preparation of %s failed", job.key)
                self._finish(job, permanent=isinstance(exc, (ValueError, KeyError)))
            finally:
                release()
        finally:
            with self._cv:
                self._busy = max(0, self._busy - 1)

    def _finish(self, job, done=False, permanent=False, requeue_after=None, count_attempt=True):
        with self._cv:
            if done:
                self._jobs.pop(job.key, None)
                self._failed.pop(job.key, None)
                self.completed += 1
                return
            if requeue_after is not None:
                job.state, job.not_before = "queued", self._clock() + requeue_after
                if not count_attempt:
                    job.attempts = max(0, job.attempts - 1)
                self._push(job)
                self._cv.notify()
                return
            if permanent or job.attempts >= self.settings.max_attempts:
                self._jobs.pop(job.key, None)
                self._failed[job.key] = {"error": job.error, "permanent": True, "attempts": job.attempts,
                                         "at": self._clock(), "book": job.book["id"], "language": job.language}
                self._save_failed()
                log.warning("Giving up on %s: %s", job.key, job.error)
                return
            delay = min(self.settings.backoff_cap, self.settings.backoff_base * (2 ** (job.attempts - 1)))
            delay *= 0.5 + self.rng.random()                     # jitter
            job.state, job.not_before = "queued", self._clock() + delay
            self._push(job)
            self._cv.notify()
            log.info("Will retry %s in %.0fs: %s", job.key, delay, job.error)

    def _set_stage(self, job, stage, percent):
        job.stage, job.percent = stage, max(job.percent, percent) if stage == job.stage else percent

    def _prepare(self, job):
        language, book = job.language, job.book
        version = segment.segment_version(language)
        if job.kind == LOCAL:
            cat = self.catalogues[language]
            info = cat.local_texts.get(book["id"])
            if info is None:
                raise source.PermanentSourceError("local text is no longer verified")
            text = cat.read_local_text(book["id"])
            content_hash = info["sha256"]
        else:
            self._set_stage(job, "fetching", 12)
            with self._downloads:               # bounded concurrency toward the source
                self.gate.wait()
                if self._fetch_reports_progress:
                    report = lambda fraction: self._set_stage(job, "fetching", 12 + int(48 * min(1.0, max(0.0, fraction))))
                    text = self._fetch(book, progress=report)
                else:
                    text = self._fetch(book)
            self._set_stage(job, "cleaning", 68)
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._set_stage(job, "indexing", 82)
        passages = segment.split(text, language)
        if not passages:
            raise source.PermanentSourceError("the text did not yield readable passages")
        cold = job.kind == COLD
        self.shelf.publish(language, book["id"], passages, edition_key=edition_key_for(book),
                           content_hash=content_hash, segment_version=version, external=cold,
                           book=book if cold else None)
        if cold:
            self.catalogues[language].register_external(book)
        job.stage, job.percent = "ready", 100

    # ---- the lake ----------------------------------------------------------------------------
    def pending_cold(self, language):
        with self._cv:
            return sum(1 for j in self._jobs.values() if j.kind == COLD and j.language == language)

    def lake_ids(self, language):
        return self.shelf.external_ids(language)

    def lake_target(self, language):
        """How many prepared books this language's library should hold (bounded by its catalogue)."""
        if not self.universe or not self.universe.available:
            return 0
        return min(self.settings.library_target, max(0, self.universe.count(language)))

    def estimated_wait(self):
        """Seconds a newly queued visitor pick would wait: the cold jobs ahead of it, one download start per gap."""
        with self._cv:
            ahead = sum(1 for j in self._jobs.values() if j.kind == COLD and j.state in ("queued", "running"))
        return ahead * self.settings.min_interval / max(1, 1)

    def lake_deficit(self, language):
        return max(0, self.lake_target(language) - len(self.lake_ids(language)) - self.pending_cold(language))

    def known_gids(self, language):
        """Editions that must not be drawn again right now: in the lake, queued, failed for good, or a starter book."""
        gids = set()
        for bid in self.lake_ids(language):
            tail = bid.rsplit("-", 1)[-1]
            if tail.isdigit():
                gids.add(int(tail))
        with self._cv:
            gids.update(int(j.book["gutenberg_id"]) for j in self._jobs.values()
                        if j.language == language and str(j.book.get("gutenberg_id") or "").isdigit())
            gids.update(int(f["book"].rsplit("-", 1)[-1]) for f in self._failed.values()
                        if f.get("language") == language and str(f.get("book", "")).rsplit("-", 1)[-1].isdigit())
        gids.update(int(b["gutenberg_id"]) for b in self.catalogues[language].all_books()
                    if str(b.get("gutenberg_id") or "").isdigit() and b["id"] in self.catalogues[language].local_texts)
        return gids

    def bump(self, language, book_id):
        """A visitor is waiting on this edition: move it to the front of the queue."""
        with self._cv:
            for job in self._jobs.values():
                if job.language == language and job.book["id"] == book_id and job.state == "queued" and job.priority > 0:
                    job.priority, job.not_before = 0, 0.0
                    self._push(job)
                    self._cv.notify()
                    return True
        return False

    def find_job(self, language, book_token):
        cat = self.catalogues[language]
        with self._cv:
            for job in self._jobs.values():
                if job.language == language and cat.book_token(job.book["id"]) == book_token:
                    return job
            for key, failure in self._failed.items():
                if failure.get("language") == language and cat.book_token(failure.get("book", "")) == book_token:
                    return None
        return None

    def has_failed(self, language, book_token):
        cat = self.catalogues[language]
        with self._cv:
            return any(f.get("language") == language and cat.book_token(f.get("book", "")) == book_token
                       for f in self._failed.values())

    def replenish(self):
        """Queue the next wave of fresh random books wherever a lake is below its target. Returns books queued."""
        if not self.settings.replenish or not self.universe or not self.universe.available:
            return 0
        queued = 0
        for language in self.catalogues:
            deficit = self.lake_deficit(language)
            if deficit <= 0:
                self.wave[language] = self.settings.first_wave                  # lake is full: next time start small again
                continue
            if self.pending_cold(language) > self.wave[language] // 2:
                continue                                                          # the last wave is still on its way
            skip = self.known_gids(language)
            picks = self.universe.random_candidates(language, min(deficit, self.wave[language]), self.rng,
                                                    reject=lambda r: r["gutenberg_id"] in skip)
            for record in picks:
                if self.enqueue(COLD, language, record, priority=5):
                    queued += 1
            self.wave[language] = min(self.settings.max_wave, max(self.wave[language] + 1, int(self.wave[language] * 1.6)))
        return queued

    def maintenance(self):
        """Only if the library has grown past its target (plus one wave): retire the OLDEST books, by age alone."""
        for language, cat in self.catalogues.items():
            for victim in self.shelf.evict_cold(language, self.settings.library_target + self.settings.max_wave):
                cat.unregister_external(victim)

    def _scheduler(self):
        while not self._stop.wait(self.settings.check_interval):
            try:
                self.shelf.refresh_if_changed()
                self.seed_local()
                self.replenish()
                self.maintenance()
            except Exception:
                log.exception("Lake check failed; will try again")

    # ---- reporting ---------------------------------------------------------------------------
    def status(self, detail=False):
        with self._cv:
            jobs = list(self._jobs.values())
            out = {
                "running": bool(self._threads), "busy": self._busy, "completed": self.completed,
                "queued": sum(1 for j in jobs if j.state == "queued"),
                "active": sum(1 for j in jobs if j.state == "running"),
                "failed": len(self._failed),
                "languages": {
                    lang: {"lake": len(self.lake_ids(lang)), "target": self.lake_target(lang),
                           "pending": sum(1 for j in jobs if j.language == lang),
                           "starter": max(0, self.shelf.count(lang) - len(self.lake_ids(lang)))}
                    for lang in self.catalogues},
            }
            if detail:
                out["jobs"] = [{"key": j.key, "state": j.state, "stage": j.stage, "attempts": j.attempts, "error": j.error} for j in jobs]
                out["failures"] = self._failed
        return out

    def clear_failures(self):
        with self._cv:
            self._failed.clear()
        self._save_failed()
