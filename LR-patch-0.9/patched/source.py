"""Download and clean one Project Gutenberg text. Used only by background workers.

One attempt per call: retries, back-off and rate limiting belong to the worker (prep.py), so
no visitor request ever waits on this module.

Where texts come from (0.9). Project Gutenberg's robot policy says www.gutenberg.org is for human visitors and that
programs should use its mirrors, so the worker asks the official high-speed mirrors and never touches www.gutenberg.org
(LR_ALLOW_WWW=1 adds it as a last resort). Extra mirrors: LR_MIRROR_BASES. Every host has a health record
(SourceHealth): after repeated failures it rests, the rest doubles up to an hour, a 429 (or two 403s in a row) rests it
for half an hour or more, and when a rest is over ONE probe request decides whether the host is back. A whole-site
outage therefore costs a few requests an hour instead of a steady hammering.
"""
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

import textproc

START_RE = re.compile(r"\*{3,}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3,}", re.I)
END_RE = re.compile(r"\*{3,}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)
MIN_CHARS = {"zh": 300}
DEFAULT_MIN_CHARS = 500
CONNECT_TIMEOUT = 8          # seconds to connect / to wait for any silent server before trying the next address
TOTAL_DEADLINE = 45         # one download attempt never runs longer than this, however slowly it trickles
MAX_BYTES = int(float(os.environ.get("LR_MAX_BOOK_MB") or 30) * 1_000_000)   # a plain-text book is a few MB: never buffer more
log = logging.getLogger("literary_roulette.source")
OWN_DOMAINS = ("gutenberg.org", "pglaf.org")            # Project Gutenberg's own domains (and their subdomains)
OFFICIAL_MIRRORS = ("https://gutenberg.pglaf.org", "https://aleph.gutenberg.org")   # Gutenberg-run, serve cache/epub/
WWW = "https://www.gutenberg.org"


def _user_agent():
    """A real contact address is the single most useful thing for staying welcome: set LR_CONTACT_EMAIL (and LR_SITE_URL)."""
    site, email = os.environ.get("LR_SITE_URL", "").strip(), os.environ.get("LR_CONTACT_EMAIL", "").strip()
    contact = f"contact: {email}" if email else "contact via the site's About page"
    return f"LiteraryRoulette/0.9 (background text preparation; {site + '; ' if site else ''}{contact})"


USER_AGENT = _user_agent()


class SourceError(Exception):
    """Base class. `permanent` tells the worker whether retrying can ever help."""
    permanent = False


class PermanentSourceError(SourceError):
    permanent = True


class TransientSourceError(SourceError):
    """A temporary problem. `retry_after` (seconds) is the source's own "come back later"; `blocked` means it told us to stop."""
    permanent = False

    def __init__(self, message="", *, retry_after=None, blocked=False):
        super().__init__(message)
        self.retry_after, self.blocked = retry_after, blocked


class BlockedSourceError(TransientSourceError):
    """403 / 429: the host is telling us to stop. That is not the book's fault, so it is never a reason to give the book up."""

    def __init__(self, message="", *, code=403, retry_after=None):
        super().__init__(message, retry_after=retry_after, blocked=True)
        self.code = code


class SourceResting(TransientSourceError):
    """Every address for this book is resting after earlier failures: nothing was asked, so nothing counts against the book."""

    def __init__(self, wait, hosts=()):
        super().__init__(f"all sources are resting ({', '.join(hosts) or 'none configured'}); next try in {wait:.0f}s")
        self.wait, self.hosts = float(wait), tuple(hosts)


class RateGate:
    """Keeps a minimum gap between downloads across all worker threads."""

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
    the time of the last download start, so the courtesy gap toward Gutenberg is global, not per process."""

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
            self._fcntl.flock(handle, self._fcntl.LOCK_EX)         # one download start at a time, machine-wide
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


def sources():
    """Ordered [(base_url, layout)]. "epub" = <base>/cache/epub/N/pgN.txt (the official mirrors); "main" = the classic
    <base>/1/3/4/1342/1342-0.txt tree that most third-party mirrors carry. Add mirrors with LR_MIRROR_BASES, comma-separated,
    each optionally prefixed epub: or main: (default main:). https only."""
    out = [(base, "epub") for base in OFFICIAL_MIRRORS]
    for item in (os.environ.get("LR_MIRROR_BASES") or "").split(","):
        item = item.strip()
        if not item:
            continue
        layout = "main"
        if item[:5] in ("main:", "epub:"):
            layout, item = item[:4], item[5:]
        base = item.rstrip("/")
        parts = urlparse(base)
        if parts.scheme == "https" and parts.hostname:
            out.append((base, layout))
        else:
            log.warning("Ignoring LR_MIRROR_BASES entry %r: it must be an https address", item)
    if os.environ.get("LR_ALLOW_WWW", "").strip().lower() in ("1", "true", "yes", "on"):
        out.append((WWW, "epub"))
    return out


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        return ""


def source_hosts():
    """The hosts the worker may ask, first choice first."""
    return list(dict.fromkeys(_host(base) for base, _layout in sources()))


def trusted_url(value):
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not host:
            return False
        if any(host == d or host.endswith("." + d) for d in OWN_DOMAINS):
            return True
        return host in {_host(base) for base, _layout in sources()}        # a mirror you configured yourself
    except (ValueError, TypeError):
        return False


def mirror_urls(gid):
    """Plain-text addresses for one ebook, best first. Built from the Gutenberg number, so they never depend on a stored URL."""
    try:
        n = int(gid)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    g, urls = str(n), []
    for base, layout in sources():
        if layout == "epub":
            urls.append(f"{base}/cache/epub/{g}/pg{g}.txt")
        elif n >= 10:                                               # classic tree: all digits but the last form the folders
            tree = "/".join(g[:-1])
            urls += [f"{base}/{tree}/{g}/{g}-0.txt", f"{base}/{tree}/{g}/{g}.txt"]
    return urls


def candidate_urls(book):
    urls = mirror_urls(book.get("gutenberg_id"))
    if not urls:                         # no Gutenberg number (odd or local records): use what the record says
        urls = [u for u in (book.get("text_urls") or [book.get("text_url")]) if u]
    return list(dict.fromkeys(urls))


class SourceHealth:
    """Per-host memory of "can I ask this host right now?", shared between processes through a small JSON file.

    * 3 failures in a row start a rest of 2 min; every further failure doubles it, up to 1 h. One success clears everything.
    * 429 (rate limited) rests the host for 30 min at once; two 403s in a row do the same; repeats double it, up to 6 h.
      A Retry-After from the host is always honoured (up to 1 h).
    * When a rest is over, exactly ONE request (a probe) is allowed; until it answers, everyone else keeps waiting.
    The file is best effort: if it cannot be written the in-memory record still protects this process."""

    FAILS_BEFORE_REST = 3
    BASE_REST, MAX_REST = 120.0, 3600.0
    BLOCK_REST, MAX_BLOCK_REST = 1800.0, 21600.0
    PROBE_LEASE = TOTAL_DEADLINE + 15.0

    def __init__(self, path=None, clock=time.time, rng=None):
        self.path = Path(path) if path else None
        self._clock, self._rng = clock, rng or random.Random()
        self._lock, self._hosts, self._mtime = threading.Lock(), {}, None
        with self._lock:
            self._refresh()

    # -- file ----------------------------------------------------------------------------------------------
    def _refresh(self):
        if not self.path:
            return
        try:
            mtime = self.path.stat().st_mtime_ns
            if mtime == self._mtime:
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self._hosts = value if isinstance(value, dict) else {}
            self._mtime = mtime
        except (OSError, ValueError):
            pass

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._hosts, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime_ns
        except OSError as exc:
            log.warning("Could not save source health: %s", exc)

    # -- decisions -----------------------------------------------------------------------------------------
    def rest_left(self, host):
        with self._lock:
            self._refresh()
            return max(0.0, float((self._hosts.get(host) or {}).get("until", 0)) - self._clock())

    def reserve(self, host):
        """May a request to `host` go out now? Also takes the single probe slot when a rest has just ended."""
        with self._lock:
            self._refresh()
            h, now = self._hosts.get(host), self._clock()
            if not h:
                return True
            if float(h.get("until", 0)) > now:
                return False
            if h.get("rested"):                                      # rest over: one probe at a time
                if float(h.get("probe_until", 0)) > now:
                    return False
                h["probe_until"] = now + self.PROBE_LEASE
                self._save()
            return True

    def ok(self, host):
        with self._lock:
            self._refresh()
            if self._hosts.pop(host, None) is not None:
                self._save()

    def fail(self, host, *, retry_after=None, blocked=False, immediate=False, error=""):
        with self._lock:
            self._refresh()
            h, now = self._hosts.setdefault(host, {}), self._clock()
            h["fails"], h["probe_until"], h["last_error"] = int(h.get("fails", 0)) + 1, 0, str(error)[:160]
            rest = 0.0
            if blocked:
                h["blocks"] = int(h.get("blocks", 0)) + 1
                if immediate or h["blocks"] >= 2:
                    n = max(1, h["blocks"] - (0 if immediate else 1))
                    rest = min(self.MAX_BLOCK_REST, self.BLOCK_REST * 2 ** (n - 1))
            if not rest and h["fails"] >= self.FAILS_BEFORE_REST:
                rest = min(self.MAX_REST, self.BASE_REST * 2 ** (h["fails"] - self.FAILS_BEFORE_REST))
            if retry_after:
                rest = max(rest, min(float(retry_after), self.MAX_REST))
            if rest:
                h["until"], h["rested"] = now + rest * (0.9 + 0.2 * self._rng.random()), True      # jitter
                log.warning("Source %s rests for %.0fs after %d failure(s): %s", host, rest, h["fails"], error)
            self._save()

    # -- reporting -----------------------------------------------------------------------------------------
    def state(self, hosts):
        """"ok" (first choice usable) | "degraded" (first choice resting, another usable) | "down" (every host resting)."""
        hosts = list(hosts)
        if not hosts:
            return "ok"
        free = [h for h in hosts if self.rest_left(h) <= 0]
        if hosts[0] in free:
            return "ok"
        return "degraded" if free else "down"

    def rest_left_all(self, hosts):
        """Seconds until ANY of `hosts` may be asked again; 0 when one can be asked now."""
        hosts = list(hosts)
        left = [self.rest_left(h) for h in hosts]
        return 0.0 if not left or min(left) <= 0 else min(left)

    def snapshot(self):
        with self._lock:
            self._refresh()
            now = self._clock()
            return {h: {"fails": int(v.get("fails", 0)), "rest_left": round(max(0.0, float(v.get("until", 0)) - now)),
                        "last_error": v.get("last_error", "")} for h, v in self._hosts.items()}


def plain_text_url(formats):
    for key, value in (formats or {}).items():
        if str(key).lower().startswith("text/plain") and isinstance(value, str) and trusted_url(value):
            return value
    return None


def clean_gutenberg_text(raw):
    text = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    start = START_RE.search(text)
    if start:
        text = text[start.end():]
    end = END_RE.search(text)
    if end:
        text = text[:end.start()]
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def decode_bytes(raw):
    for encoding in ("utf-8-sig", "utf-8", "big5", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def clean_for_language(text, language):
    if language == "zh":
        text = clean_gutenberg_text(text)
        try:
            from opencc import OpenCC
        except ImportError as exc:
            raise PermanentSourceError("Chinese conversion needs OpenCC: python3 -m pip install opencc-python-reimplemented") from exc
        return OpenCC("t2s").convert(text)
    cleaned, _info = textproc.clean_book_text(text)
    return cleaned if cleaned and len(cleaned.strip()) >= DEFAULT_MIN_CHARS else clean_gutenberg_text(text)


class _TrustedRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect may only lead to another trusted https host (urllib would otherwise follow it anywhere)."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if not trusted_url(target):
            raise PermanentSourceError(f"redirected to an untrusted address ({_host(target) or 'unknown host'})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_TrustedRedirects())


def _open(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def _retry_after(exc):
    """Seconds from a numeric Retry-After header, capped at an hour; None when absent or in date form."""
    try:
        value = (exc.headers.get("Retry-After") or "").strip() if exc.headers else ""
        return min(3600.0, float(value)) if value.isdigit() else None
    except Exception:
        return None


def download(url, gid, *, timeout=CONNECT_TIMEOUT, deadline=TOTAL_DEADLINE, opener=None, progress=None):
    """Fetch the raw bytes once. Raises Transient/PermanentSourceError.

    `progress(fraction, got_bytes)`: fraction is None when the server did not say how big the file is. A silent server
    is given `timeout` seconds, and the whole attempt at most `deadline` seconds, so a stall can never hang a reader."""
    started, opener = time.monotonic(), opener or _open
    if not trusted_url(url):
        raise PermanentSourceError(f"eBook #{gid} has no trusted https plain-text URL.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"})
    try:
        with opener(req, timeout=timeout) as response:
            content_type = response.headers.get_content_type().lower()
            try:
                size = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                size = 0
            if size > MAX_BYTES:
                raise PermanentSourceError(f"eBook #{gid} is larger than {MAX_BYTES // 1_000_000} MB, so it is skipped.")
            chunks, got = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
                if got > MAX_BYTES:
                    raise PermanentSourceError(f"eBook #{gid} is larger than {MAX_BYTES // 1_000_000} MB, so it is skipped.")
                if progress:
                    progress(got / size if size else None, got)
                if time.monotonic() - started > deadline:
                    raise TransientSourceError(f"eBook #{gid}: the server was too slow (over {deadline} s)")
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        wait = _retry_after(exc)
        if exc.code in (403, 429):               # told to stop: never the book's fault, never "failed for good"
            raise BlockedSourceError(f"eBook #{gid}: HTTP {exc.code} (the source asked us to slow down or stop)",
                                     code=exc.code, retry_after=wait) from exc
        if exc.code in (408, 425, 500, 502, 503, 504):
            raise TransientSourceError(f"eBook #{gid}: HTTP {exc.code}", retry_after=wait) from exc
        raise PermanentSourceError(f"eBook #{gid}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientSourceError(f"eBook #{gid}: {getattr(exc, 'reason', exc)}") from exc
    if content_type in ("text/html", "application/json", "application/xhtml+xml") or \
            raw[:512].lstrip().lower().startswith((b"<!doctype html", b"<html", b'{"error"')):
        raise PermanentSourceError(f"eBook #{gid} returned an error page instead of book text.")
    return raw


def _note(health, host, exc):
    if isinstance(exc, PermanentSourceError):
        health.ok(host)                      # it answered (e.g. 404 for one file name): the host is fine
    else:
        health.fail(host, retry_after=exc.retry_after, blocked=exc.blocked,
                    immediate=getattr(exc, "code", None) == 429, error=str(exc))


def fetch_clean_text(book, *, downloader=download, progress=None, health=None):
    """Download, decode and clean one book. Returns the clean text.

    Tries each known address in order (official mirrors first, then any you configured), skipping hosts that are resting
    (see SourceHealth). Only when every address answered "not found" is the book given up on for good; a busy, slow or
    blocked source is raised as a temporary problem so the worker retries later, and if every host is resting
    SourceResting says how long to wait without anything having been asked."""
    language, gid = book["language"], book["gutenberg_id"]
    raw, errors, skipped = None, [], []
    for url in candidate_urls(book):
        host = _host(url)
        if health is not None and not health.reserve(host):
            skipped.append(host)
            continue
        try:
            raw = downloader(url, gid, progress=progress) if progress else downloader(url, gid)
            if health is not None:
                health.ok(host)
            break
        except SourceError as exc:       # a slow, silent, blocked or missing address just means: try the next one
            errors.append(exc)
            if health is not None:
                _note(health, host, exc)
    if raw is None:
        if skipped and not [e for e in errors if not e.permanent]:   # a resting host might still have it: not "gone"
            raise SourceResting(health.rest_left_all(skipped) or 5.0, dict.fromkeys(skipped))
        if errors and all(e.permanent for e in errors):
            raise errors[-1]
        raise next((e for e in reversed(errors) if not e.permanent), None) or PermanentSourceError(f"eBook #{gid} has no trusted https plain-text URL.")
    text = clean_for_language(decode_bytes(raw), language)
    if len(text.strip()) < MIN_CHARS.get(language, DEFAULT_MIN_CHARS):
        raise PermanentSourceError(f"eBook #{gid} did not yield enough readable text.")
    return text
