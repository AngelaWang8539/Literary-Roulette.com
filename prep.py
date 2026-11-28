"""Background preparation: fetch -> validate -> clean -> segment -> store -> publish.

The worker owns every slow thing. A visitor request never waits on it.

* Jobs are keyed (language, book_id, edition_key, segmenter version); the same key is queued once, and a
  lock file stops two processes preparing the same edition at the same time. For author books the edition key carries the
  text hash, so a key is book + text hash + segmenter version.
* Downloads share one bounded concurrency limit and a minimum gap, whatever the number of visitors.
* Transient failures retry later with exponential back-off and jitter; permanent ones (404, rights,
  unreadable text) are remembered and not retried.
* A book becomes ready only after it is fully written and verified (ReadyShelf.publish).
* The LIBRARY (the "lake"): every book prepared so far, kept for good and shared by everyone. Each spin is a uniform
  draw from the WHOLE catalogue (universe.py); a book already in the library is instant, a cold one is prepared once
  (visitors wait with the librarian) and then reused by every later reader. Waves of random picks (3, 5, 8, 12 ...)
  grow the library in the background within the source's courtesy limits, up to `library_target` books per language
  ("all" in production). The queue is bounded: past `max_cold_wait` or while downloads are paused, /api/draw serves the ready
shelf and SAYS so (admit_cold), never silently.
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

import jobstate
import segment
import source

log = logging.getLogger("literary_roulette.prep")
LOCAL, COLD = "local", "cold"
STALE_LOCK_SECONDS = 15 * 60


class Settings:
    """Library and source-courtesy knobs."""

    def __init__(self, **kw):
        self.library_target = int(kw.get("library_target", 100))     # prepared books wanted per language (10**9 = all)
        self.first_wave = int(kw.get("first_wave", 3))
        self.max_wave = int(kw.get("max_wave", 12))
        self.max_cold_wait = float(kw.get("max_cold_wait", 12.0))     # seconds of queue a visitor may be asked to wait
        self.queue_cap = int(kw.get("queue_cap", 40))                 # hard bound on waiting cold jobs (all processes)
        self.background_slots = int(kw.get("background_slots", 1))    # library-growing jobs that may run at once (a visitor's pick never waits behind a crowd of them)
        self.workers = int(kw.get("workers", 3))
        self.source_concurrency = int(kw.get("source_concurrency", 2))
        self.min_interval = float(kw.get("min_interval", 2.0))       # seconds between downloads (Gutenberg asks for ~2 s)
        self.max_attempts = int(kw.get("max_attempts", 4))
        self.backoff_base = float(kw.get("backoff_base", 20.0))
        self.backoff_cap = float(kw.get("backoff_cap", 900.0))
        self.check_interval = float(kw.get("check_interval", 3.0))
        self.failed_keep = int(kw.get("failed_keep", 300))
        self.replenish = bool(kw.get("replenish", True))
        self.breaker_threshold = int(kw.get("breaker_threshold", 6))   # consecutive temporary failures that pause fetching
        self.breaker_seconds = float(kw.get("breaker_seconds", 120))


def edition_key_for(book):
    if book.get("provider") == "author":
        return f"author-{book['id']}-{str(book.get('text_hash') or '')[:12]}"
    gid = book.get("gutenberg_id")
    return f"pg-{gid}-plain-text" if gid else f"local-{book['id']}"


def _gutenberg_id(book_id):
    """The Gutenberg number inside a pg-<lang>-<number> id; None for every other kind of book id."""
    parts = str(book_id).split("-")
    return int(parts[2]) if len(parts) == 3 and parts[0] == "pg" and parts[2].isdigit() else None


BACKGROUND = 5      # priority of the jobs that only grow the library; a visitor's own pick is priority 0


class Job:
    def __init__(self, key, kind, language, book, priority):
        self.key, self.kind, self.language, self.book, self.priority = key, kind, language, book, priority
        self.state, self.attempts, self.error = "queued", 0, None
        self.queued_at, self.not_before = time.time(), 0.0
        self.stage, self.percent = "queued", 6
        self.detail, self.indeterminate, self.last_move = "", False, time.time()
        self.retry_after, self.withdrawn = None, False


class PrepWorker:
    def __init__(self, shelf, catalogues, universe, state_dir, settings=None, *, fetcher=None, sleep=time.sleep,
                 rng=None, clock=time.time, registry=None, text_store=None):
        self.shelf, self.catalogues, self.universe = shelf, catalogues, universe
        self.registry, self.text_store, self.downloads = registry, text_store, 0
        self.settings = settings or Settings()
        self.state_dir = Path(state_dir)
        self.lock_dir = self.state_dir / ".locks"
        self.failed_path = self.state_dir / "failed.json"
        self._fetch = fetcher or source.fetch_clean_text
        self._fetch_reports_progress = fetcher is None
        self.wave = {lang: self.settings.first_wave for lang in catalogues}
        self._sleep, self._clock = sleep, clock
        self.rng = rng or random.Random()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = jobstate.JobStore(self.state_dir / "jobs.sqlite", clock=clock)     # progress every process can see
        self.gate = source.SharedRateGate(self.state_dir / ".gate", self.settings.min_interval)   # courtesy gap, machine-wide
        self._fail_streak, self._breaker_until, self._breaker_level = 0, 0.0, 0
        self.health = source.SourceHealth(self.state_dir / "source-health.json", clock=clock)
        self._downloads = threading.BoundedSemaphore(max(1, self.settings.source_concurrency))
        self._cv = threading.Condition()
        self._admit_lock = threading.Lock()
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
    def job_key(language, book_id, edition_key, revision=None):
        return f"{language}|{book_id}|{edition_key}|{revision or segment.segment_version(language)}"

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
        self._publish(job)
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
                parked, picked = [], None
                while self._heap:
                    entry = heapq.heappop(self._heap)
                    job = self._jobs.get(entry[2])
                    if not job or job.state != "queued":
                        continue
                    if entry[0] >= BACKGROUND and self._background_held(now):
                        parked.append(entry)                    # not now: a visitor is waiting, or the background slot is taken
                        continue
                    picked = job
                    break
                for entry in parked:
                    heapq.heappush(self._heap, entry)
                if picked:
                    picked.state = "running"
                    self._busy += 1
                    return picked
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                nap = remaining
                if self._delayed:
                    nap = min(nap, max(0.05, min(d[0] for d in self._delayed) - now))
                self._cv.wait(timeout=nap)
        return None

    def _background_held(self, now):
        """Library-growing jobs yield to people. They wait while any visitor's pick is queued or running (a pick that is only
        waiting for a retry does not count), and only `background_slots` of them run at once. Called with the lock held.
        Without this, a fresh server's first minute is spent on its own waves and the first reader waits behind them."""
        running = 0
        for job in self._jobs.values():
            if job.priority >= BACKGROUND:
                running += job.state == "running"
            elif job.state == "running" or (job.state == "queued" and job.not_before <= now):
                return True
        return running >= self.settings.background_slots

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

    def breaker_remaining(self):
        local_left = max(0.0, self._breaker_until - self._clock())
        health = getattr(self, "health", None)
        source_left = health.rest_left_all(source.source_hosts()) if health else 0.0
        return max(local_left, source_left)

    def breaker_open(self):
        health = getattr(self, "health", None)
        source_down = bool(health and health.state(source.source_hosts()) == "down")
        return self._clock() < self._breaker_until or source_down

    def source_state(self):
        health = getattr(self, "health", None)
        return health.state(source.source_hosts()) if health else "ok"

    def _note_result(self, ok, transient=False):
        """Pause repeated transient failures with an escalating, capped cooldown."""
        if ok:
            self._fail_streak = 0
            self._breaker_until = 0.0
            self._breaker_level = 0
        elif transient:
            self._fail_streak += 1
            if self._fail_streak >= self.settings.breaker_threshold:
                level = max(0, getattr(self, "_breaker_level", 0))
                delay = min(3600.0, self.settings.breaker_seconds * (2 ** level))
                self._breaker_until = self._clock() + delay
                self._breaker_level = level + 1
                self._fail_streak = 0
                log.warning("Too many temporary failures in a row: pausing downloads for %.0fs", delay)

    def _process(self, job):
        try:
            if job.kind == COLD and self.breaker_open():       # paused: do not start a download, look again when it closes
                self._finish(job, requeue_after=max(1.0, self.breaker_remaining()), count_attempt=False)
                return
            release = self._claim(job.key)
            if release is None:                 # another process is preparing this edition right now
                self._finish(job, requeue_after=3.0, count_attempt=False)
                return
            try:
                self.shelf.refresh_if_changed()
                if job.kind == COLD and self.shelf.has(job.language, job.book["id"]):
                    job.stage, job.percent, job.detail, job.indeterminate = "ready", 100, "", False    # someone else already prepared it
                    self._finish(job, done=True)
                    return
                job.attempts += 1
                self._publish(job, "running")
                if job.kind == COLD and self.registry and not self.registry.is_eligible(job.book["id"]):
                    job.withdrawn = True
                    raise source.PermanentSourceError("This book was taken out of the pool before it could be prepared.")
                self._prepare(job)
                self._note_result(True)
                self._finish(job, done=True)
            except source.SourceError as exc:
                job.error, job.retry_after = str(exc), exc.retry_after
                if job.kind == COLD:
                    self._note_result(False, transient=not exc.permanent)
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
                self._cv.notify_all()                    # background jobs may have been waiting for this visitor
                outcome = ("done", None)
            elif requeue_after is not None:
                job.state, job.not_before = "queued", self._clock() + requeue_after
                if not count_attempt:
                    job.attempts = max(0, job.attempts - 1)
                self._push(job)
                self._cv.notify()
                outcome = ("queued", None)
            elif permanent or job.attempts >= self.settings.max_attempts:
                if permanent and self.registry and job.kind == COLD and not job.withdrawn:
                    self.registry.mark_text(job.book["id"], "failed", error=job.error)    # never drawn again until cleared
                self._jobs.pop(job.key, None)
                self._failed[job.key] = {"error": job.error, "permanent": True, "attempts": job.attempts,
                                         "at": self._clock(), "book": job.book["id"], "language": job.language}
                self._save_failed()
                self._cv.notify_all()
                log.warning("Giving up on %s: %s", job.key, job.error)
                outcome = ("failed", job.error)
            else:
                delay = min(self.settings.backoff_cap, self.settings.backoff_base * (2 ** (job.attempts - 1)))
                delay *= 0.5 + self.rng.random()                 # jitter
                if job.retry_after:
                    delay = min(self.settings.backoff_cap, max(delay, job.retry_after))     # the server's own advice wins
                job.state, job.not_before = "queued", self._clock() + delay
                self._push(job)
                self._cv.notify()
                log.info("Will retry %s in %.0fs: %s", job.key, delay, job.error)
                outcome = ("queued", job.error)
        self._publish(job, outcome[0], outcome[1])

    def _publish(self, job, state=None, error=None):
        """Write this job's visible state to the shared store (so any process can answer a visitor's poll)."""
        try:
            self.store.upsert(job.key, job.language, self.catalogues[job.language].book_token(job.book["id"]), job.kind,
                              state or job.state, job.stage, None if job.indeterminate else job.percent, job.detail,
                              error if error is not None else job.error, job.priority, job.queued_at, job.last_move)
            job._published = time.monotonic()
        except Exception:                      # progress is a nicety: never let it break preparing a book
            log.exception("Could not write job state")

    def _set_stage(self, job, stage, percent, detail="", indeterminate=False):
        changed = (stage != job.stage) or (indeterminate != job.indeterminate)
        job.stage, job.percent = stage, max(job.percent, percent) if stage == job.stage else percent
        job.detail, job.indeterminate, job.last_move = detail, indeterminate, time.time()
        if changed or time.monotonic() - getattr(job, "_published", 0) > 0.25:
            self._publish(job)

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
        elif book.get("provider") == "author":
            if not self.text_store:
                raise source.PermanentSourceError("author texts are not available here")
            self._set_stage(job, "cleaning", 40)
            try:
                text = self.text_store.read(book.get("text_key"), expect_hash=book.get("text_hash"))
            except (OSError, ValueError) as exc:
                raise source.PermanentSourceError(f"the stored author text cannot be used: {exc}") from exc
            content_hash = book["text_hash"]
        else:
            self._set_stage(job, "queued", 6, "waiting for the librarian's turn")        # honest: not downloading yet
            with self._downloads:               # bounded concurrency toward the source
                self.gate.wait()
                self._set_stage(job, "fetching", 12)
                self.downloads += 1
                if self._fetch_reports_progress:
                    def report(fraction, got=None):
                        if fraction is None:        # the server gave no size: report bytes, not an invented percentage
                            self._set_stage(job, "fetching", job.percent, f"{(got or 0) / 1e6:.1f} MB", True)
                        else:
                            self._set_stage(job, "fetching", 12 + int(48 * min(1.0, max(0.0, fraction))))
                    text = self._fetch(book, progress=report, health=self.health)
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
            if self.registry:
                self.registry.mark_text(book["id"], "verified", text_hash=content_hash)
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
        """Seconds a newly queued visitor pick would wait: every process's cold jobs ahead of it (shared store), one download
        start per gap. While the circuit breaker is open the wait is reported as long, so draws shed to prepared books."""
        wait = self.store.pending_cold() * self.settings.min_interval
        if self.breaker_open():
            wait += (self._breaker_until - self._clock()) + self.settings.max_cold_wait + 1
        return wait

    def request_cold(self, language, record):
        """Admit AND queue one visitor's cold pick atomically (so concurrent requests cannot overshoot the queue bound).
        Returns (True, None) with the job queued/bumped to the front, or (False, reason) with nothing queued."""
        with self._admit_lock:
            admitted, reason = self.admit_cold()
            if admitted and not self.enqueue(COLD, language, record, priority=0):
                self.bump(language, record["id"])
            return admitted, reason

    def queue_in_background(self, language, record):
        """Queue the original pick at low priority while the visitor is served from the ready shelf, if the bound allows."""
        with self._admit_lock:
            return self.has_room() and self.enqueue(COLD, language, record, priority=5)

    def has_room(self):
        """May one more cold job join the queue at all (hard bound, shared by every process)?"""
        return not self.breaker_open() and self.store.pending_cold() < self.settings.queue_cap

    def admit_cold(self):
        """(True, None) when a visitor may wait for a cold pick; otherwise (False, reason) with reason 'source_paused' (downloads
        are paused after repeated failures) or 'busy' (the queue is longer than max_cold_wait). The caller must TELL the visitor."""
        if self.breaker_open():
            return False, "source_paused"
        if self.store.pending_cold() >= self.settings.queue_cap or self.estimated_wait() > self.settings.max_cold_wait:
            return False, "busy"
        return True, None

    def job_view(self, language, book_token):
        """Progress of the newest job for this book token, from the shared store (any process can answer)."""
        return self.store.view(language, book_token)

    def lake_deficit(self, language):
        return max(0, self.lake_target(language) - len(self.lake_ids(language)) - self.pending_cold(language))

    def known_gids(self, language):
        """Editions that must not be drawn again right now: in the lake, queued, failed for good, or a starter book."""
        gids = set()
        for bid in self.lake_ids(language):
            if _gutenberg_id(bid) is not None:
                gids.add(_gutenberg_id(bid))
        with self._cv:
            gids.update(int(j.book["gutenberg_id"]) for j in self._jobs.values()
                        if j.language == language and str(j.book.get("gutenberg_id") or "").isdigit())
            gids.update(_gutenberg_id(f.get("book")) for f in self._failed.values()
                        if f.get("language") == language and _gutenberg_id(f.get("book")) is not None)
        gids.update(int(b["gutenberg_id"]) for b in self.catalogues[language].all_books()
                    if str(b.get("gutenberg_id") or "").isdigit() and b["id"] in self.catalogues[language].local_texts)
        return gids

    def known_book_ids(self, language):
        """Ids (any provider) already in the lake, queued or failed: the non-Gutenberg twin of known_gids."""
        with self._cv:
            return set(self.lake_ids(language)) | {j.book["id"] for j in self._jobs.values() if j.language == language} | \
                {f.get("book") for f in self._failed.values() if f.get("language") == language}

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

    def ahead_of(self, job):
        """How many cold books are being fetched or waiting in front of this one."""
        with self._cv:
            return sum(1 for j in self._jobs.values() if j is not job and j.kind == COLD and (
                j.state == "running" or (j.state == "queued" and (j.priority, j.queued_at) < (job.priority, job.queued_at))))

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
            skip, known = self.known_gids(language), self.known_book_ids(language)
            picks = self.universe.random_candidates(language, min(deficit, self.wave[language]), self.rng,
                                                    reject=lambda r: r["gutenberg_id"] in skip or r["id"] in known)
            for record in picks:
                if self.enqueue(COLD, language, record, priority=5):
                    queued += 1
            self.wave[language] = min(self.settings.max_wave, max(self.wave[language] + 1, int(self.wave[language] * 1.6)))
        return queued

    def maintenance(self):
        """Only if the library has grown past its target (plus one wave): retire the OLDEST books, by age alone."""
        self.store.prune()
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
                "failed": len(self._failed), "breaker_open": self.breaker_open(),
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
        if self.registry:
            self.registry.reset_failed()
