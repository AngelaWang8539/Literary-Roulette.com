"""One guarded way to talk to every upstream provider (Project Gutenberg, Gutendex, ...).

Nothing a visitor does ever reaches this module: it is used only by background jobs. For each provider a `Guard` gives:

* an https-only, per-provider host allowlist (redirects are re-checked, so a redirect cannot leave the allowlist);
* an explicit User-Agent with a contact (set LR_CONTACT_EMAIL, and LR_SITE_URL if you have one);
* connect/read timeout, a total deadline, a response-size cap and a content-type check;
* Retry-After is respected; transient failures (network, 408/425/429/5xx) are retried with exponential back-off and
  jitter; permanent ones (404, other 4xx, wrong content, too large) are never retried;
* a circuit breaker per provider (repeated transient failures pause the provider; callers keep serving last-good data);
* a shared rate gate (one request start per gap, machine-wide through a lock file) and bounded concurrency.
"""
import email.utils
import http.client
import os
import random
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

VERSION = "0.9"
GUTENBERG_HOSTS = ("gutenberg.org", "www.gutenberg.org", "dev.gutenberg.org")
GUTENDEX_HOSTS = ("gutendex.com",)
TRANSIENT_STATUS = (408, 425, 429, 500, 502, 503, 504)


def user_agent(env=os.environ):
    """LR_CONTACT_EMAIL (and optionally LR_SITE_URL) go into every request, so a provider can reach a person instead of blocking us."""
    email = (env.get("LR_CONTACT_EMAIL") or env.get("LR_CONTACT") or "").strip()
    site = (env.get("LR_SITE_URL") or "").strip()
    contact = ", ".join(x for x in (site, email) if x)
    if contact:
        return f"LiteraryRoulette/{VERSION} (+{contact})"
    return f"LiteraryRoulette/{VERSION} (background data preparation; contact via the site's About page)"


class FetchError(Exception):
    """Base class. `permanent` says whether retrying can ever help; `retry_after` is the server's own advice (seconds)."""
    permanent = False
    error_class = "fetch"

    def __init__(self, message, *, retry_after=None, error_class=None):
        super().__init__(message)
        self.retry_after = retry_after
        if error_class:
            self.error_class = error_class


class PermanentFetchError(FetchError):
    permanent = True
    error_class = "permanent"


class TransientFetchError(FetchError):
    error_class = "transient"


class CircuitOpen(TransientFetchError):
    error_class = "circuit_open"


def host_ok(url, hosts):
    try:
        p = urlparse(url)
        return p.scheme == "https" and (p.hostname or "").lower() in hosts
    except (ValueError, TypeError):
        return False


def parse_retry_after(value, now=None, cap=3600.0):
    """Seconds from a Retry-After header (a number, or an HTTP date). None when absent or unreadable."""
    if not value:
        return None
    value = str(value).strip()
    try:
        return min(cap, max(0.0, float(value)))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return min(cap, max(0.0, when - (time.time() if now is None else now)))


# ---- courtesy gaps ---------------------------------------------------------------------------------------------
class RateGate:
    """Keeps a minimum gap between request starts across all threads of this process."""

    def __init__(self, min_interval=1.5, clock=time.monotonic, sleep=time.sleep):
        self.min_interval, self._clock, self._sleep = float(min_interval), clock, sleep
        self._next, self._lock = 0.0, threading.Lock()

    def wait(self):
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.min_interval
        if delay:
            self._sleep(delay)


class SharedRateGate:
    """A RateGate that holds across processes on one machine (several Gunicorn workers + worker.py): a lock file keeps
    the time of the last request start, so the courtesy gap toward a provider is global, not per process."""

    def __init__(self, path, min_interval=2.0, sleep=time.sleep):
        self.path, self.min_interval, self._sleep = path, float(min_interval), sleep
        self._fallback = RateGate(min_interval, sleep=sleep)
        try:
            import fcntl
            self._fcntl = fcntl
        except ImportError:                       # e.g. Windows: fall back to a per-process gate
            self._fcntl = None

    def wait(self):
        if not self._fcntl or self.min_interval <= 0:
            return self._fallback.wait()
        with open(self.path, "a+") as handle:
            self._fcntl.flock(handle, self._fcntl.LOCK_EX)         # one request start at a time, machine-wide
            try:
                handle.seek(0)
                try:
                    last = float(handle.read().strip() or 0)
                except ValueError:
                    last = 0.0
                delay = last + self.min_interval - time.time()
                if delay > 0:
                    self._sleep(delay)
                handle.seek(0); handle.truncate(); handle.write(repr(time.time())); handle.flush()
            finally:
                self._fcntl.flock(handle, self._fcntl.LOCK_UN)


class Breaker:
    """Opens after `threshold` transient failures in a row and stays open for `cooldown` seconds."""

    def __init__(self, threshold=5, cooldown=120.0, clock=time.monotonic):
        self.threshold, self.cooldown, self._clock = int(threshold), float(cooldown), clock
        self._streak, self._open_until, self._lock = 0, 0.0, threading.Lock()

    def allow(self):
        return self._clock() >= self._open_until

    @property
    def seconds_left(self):
        return max(0.0, self._open_until - self._clock())

    def success(self):
        with self._lock:
            self._streak = 0

    def failure(self):
        with self._lock:
            self._streak += 1
            if self._streak >= self.threshold:
                self._open_until, self._streak = self._clock() + self.cooldown, 0


# ---- safe redirects --------------------------------------------------------------------------------------------
class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = tuple(hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_ok(newurl, self.hosts):
            raise urllib.error.HTTPError(newurl, 403, "redirect to a host that is not allowed", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener(hosts):
    """A callable(req, timeout) like urllib.request.urlopen, but redirects can never leave `hosts`."""
    opener = urllib.request.build_opener(_SafeRedirect(hosts))
    return lambda req, timeout=None: opener.open(req, timeout=timeout)


class Response:
    def __init__(self, body, headers, url, attempts):
        self.body, self.headers, self.url, self.attempts = body, headers, url, attempts


class Guard:
    def __init__(self, name, hosts, *, max_bytes=5_000_000, content_types=None, min_interval=2.0, concurrency=1,
                 gate_path=None, breaker=None, opener=None, sleep=time.sleep, rng=None, ua=None, clock=time.monotonic):
        self.name, self.hosts = name, tuple(h.lower() for h in hosts)
        self.max_bytes, self.content_types = int(max_bytes), tuple(content_types or ())
        self.ua = ua or user_agent()
        self.gate = SharedRateGate(gate_path, min_interval, sleep=sleep) if gate_path else RateGate(min_interval, sleep=sleep)
        self.breaker = breaker or Breaker(clock=clock)
        self._open = opener or safe_opener(self.hosts)
        self._slots = threading.BoundedSemaphore(max(1, int(concurrency)))
        self._sleep, self._clock, self._rng = sleep, clock, rng or random.Random()
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "retries": 0, "failures": 0, "last_error": None, "last_success": None}

    # ---- one attempt -------------------------------------------------------------------------------------------
    def _once(self, url, headers, timeout, deadline, max_bytes, types):
        started = self._clock()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "identity", **(headers or {})})
        try:
            with self._open(req, timeout=timeout) as response:
                final = response.geturl() if hasattr(response, "geturl") else url
                if not host_ok(final, self.hosts):
                    raise PermanentFetchError(f"{self.name}: the answer came from a host that is not allowed", error_class="host")
                status = getattr(response, "status", 200)
                if status != 200:
                    raise PermanentFetchError(f"{self.name}: unexpected HTTP {status}", error_class="status")
                ctype = response.headers.get_content_type().lower() if hasattr(response.headers, "get_content_type") else ""
                if types and ctype not in types:
                    raise PermanentFetchError(f"{self.name}: unexpected content type {ctype or 'unknown'}", error_class="content_type")
                try:
                    declared = int(response.headers.get("Content-Length") or 0)
                except ValueError:
                    declared = 0
                if declared > max_bytes:
                    raise PermanentFetchError(f"{self.name}: the answer is larger than {max_bytes:,} bytes", error_class="too_large")
                chunks, got = [], 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
                    if got > max_bytes:
                        raise PermanentFetchError(f"{self.name}: the answer is larger than {max_bytes:,} bytes", error_class="too_large")
                    if self._clock() - started > deadline:
                        raise TransientFetchError(f"{self.name}: too slow (over {deadline:g} s)", error_class="timeout")
                return Response(b"".join(chunks), response.headers, final, 1)
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_STATUS:
                raise TransientFetchError(f"{self.name}: HTTP {exc.code}", error_class=f"http_{exc.code}",
                                          retry_after=parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)) from exc
            raise PermanentFetchError(f"{self.name}: HTTP {exc.code}", error_class=f"http_{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise TransientFetchError(f"{self.name}: {getattr(exc, 'reason', exc)}", error_class="network") from exc

    # ---- the public call ---------------------------------------------------------------------------------------
    def get(self, url, *, headers=None, timeout=8.0, deadline=30.0, max_bytes=None, retries=3, backoff=2.0, backoff_cap=60.0):
        if not host_ok(url, self.hosts):
            raise PermanentFetchError(f"{self.name}: {url!r} is not an https address on an allowed host", error_class="host")
        if not self.breaker.allow():
            left = self.breaker.seconds_left
            raise CircuitOpen(f"{self.name} is paused after repeated failures ({left:.0f} s left)", retry_after=left)
        limit, last = int(max_bytes or self.max_bytes), None
        for attempt in range(1, int(retries) + 2):
            try:
                self.gate.wait()
                with self._slots:
                    with self._lock:
                        self.stats["requests"] += 1
                    response = self._once(url, headers, timeout, deadline, limit, self.content_types)
                self.breaker.success()
                with self._lock:
                    self.stats["last_success"] = time.time()
                response.attempts = attempt
                return response
            except PermanentFetchError as exc:                 # a clear "no": never retried, and not a sign the provider is unhealthy
                with self._lock:
                    self.stats["failures"] += 1
                    self.stats["last_error"] = str(exc)
                raise
            except TransientFetchError as exc:
                self.breaker.failure()
                last = exc
                with self._lock:
                    self.stats["failures"] += 1
                    self.stats["last_error"] = str(exc)
                if attempt > retries or not self.breaker.allow():
                    break
                if exc.retry_after is not None and exc.retry_after > backoff_cap:
                    break                                       # the server asked for a longer pause than we will wait here: report it
                delay = min(backoff_cap, backoff * (2 ** (attempt - 1))) * (0.5 + self._rng.random())   # exponential + jitter
                self._sleep(max(delay, exc.retry_after or 0.0))
                with self._lock:
                    self.stats["retries"] += 1
        last.attempts = attempt
        raise last

    def status(self):
        with self._lock:
            out = dict(self.stats)
        out.update(name=self.name, circuit_open=not self.breaker.allow(), circuit_seconds_left=round(self.breaker.seconds_left, 1))
        return out


def make_guards(gate_path, *, min_interval=2.0, env=os.environ, **overrides):
    """The standard guards. Gutenberg's gate file is the same one the text downloader uses, so the courtesy gap toward
    gutenberg.org is shared by catalogue, top lists and book downloads alike."""
    ua = user_agent(env)
    gutenberg = Guard("gutenberg", GUTENBERG_HOSTS, max_bytes=200_000_000, min_interval=min_interval, gate_path=gate_path, ua=ua,
                      content_types=("text/html", "text/plain", "application/gzip", "application/x-gzip", "application/octet-stream"),
                      **overrides)
    gutendex = Guard("gutendex", GUTENDEX_HOSTS, max_bytes=5_000_000, min_interval=max(min_interval, 2.0),
                     gate_path=str(gate_path) + "-gutendex" if gate_path else None, ua=ua,
                     content_types=("application/json",), **overrides)
    return {"gutenberg": gutenberg, "gutendex": gutendex}
