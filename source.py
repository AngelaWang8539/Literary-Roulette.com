"""Download and clean one Project Gutenberg text. Used only by background workers.

One attempt per call: retries, back-off and rate limiting belong to the worker (prep.py), so
no visitor request ever waits on this module.
"""
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import textproc

START_RE = re.compile(r"\*{3,}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3,}", re.I)
END_RE = re.compile(r"\*{3,}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)
MIN_CHARS = {"zh": 300}
DEFAULT_MIN_CHARS = 500
CONNECT_TIMEOUT = 8          # seconds to connect / to wait for any silent server before trying the next address
TOTAL_DEADLINE = 45         # one download attempt never runs longer than this, however slowly it trickles
USER_AGENT = "LiteraryRoulette/0.6 (background text preparation; contact via the site's About page)"


class SourceError(Exception):
    """Base class. `permanent` tells the worker whether retrying can ever help."""
    permanent = False


class PermanentSourceError(SourceError):
    permanent = True


class TransientSourceError(SourceError):
    permanent = False


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


def trusted_url(value):
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        return p.scheme == "https" and (host == "gutenberg.org" or host.endswith(".gutenberg.org"))
    except (ValueError, TypeError):
        return False


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


def download(url, gid, *, timeout=CONNECT_TIMEOUT, deadline=TOTAL_DEADLINE, opener=urllib.request.urlopen, progress=None):
    """Fetch the raw bytes once. Raises Transient/PermanentSourceError.

    `progress(fraction, got_bytes)`: fraction is None when the server did not say how big the file is. A silent server
    is given `timeout` seconds, and the whole attempt at most `deadline` seconds, so a stall can never hang a reader."""
    started = time.monotonic()
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
            chunks, got = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
                if progress:
                    progress(got / size if size else None, got)
                if time.monotonic() - started > deadline:
                    raise TransientSourceError(f"eBook #{gid}: the server was too slow (over {deadline} s)")
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code in (408, 425, 429, 500, 502, 503, 504):
            raise TransientSourceError(f"eBook #{gid}: HTTP {exc.code}") from exc
        raise PermanentSourceError(f"eBook #{gid}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientSourceError(f"eBook #{gid}: {getattr(exc, 'reason', exc)}") from exc
    if content_type in ("text/html", "application/json", "application/xhtml+xml") or \
            raw[:512].lstrip().lower().startswith((b"<!doctype html", b"<html", b'{"error"')):
        raise PermanentSourceError(f"eBook #{gid} returned an error page instead of book text.")
    return raw


def fetch_clean_text(book, *, downloader=download, progress=None):
    """Download, decode and clean one book. Returns the clean text.

    Tries each known address for the book in turn (as the desktop app does); only when every address says
    "not found" is the book given up on for good. A temporary problem (busy server) is raised so the worker retries later."""
    language, gid = book["language"], book["gutenberg_id"]
    urls = [u for u in (book.get("text_urls") or [book.get("text_url")]) if u]
    raw, errors = None, []
    for url in urls:                     # a slow, silent or missing address just means: try the next one
        try:
            raw = downloader(url, gid, progress=progress) if progress else downloader(url, gid)
            break
        except SourceError as exc:
            errors.append(exc)
    if raw is None:
        if errors and all(e.permanent for e in errors):
            raise errors[-1]
        raise next((e for e in reversed(errors) if not e.permanent), None) or PermanentSourceError(f"eBook #{gid} has no trusted https plain-text URL.")
    text = clean_for_language(decode_bytes(raw), language)
    if len(text.strip()) < MIN_CHARS.get(language, DEFAULT_MIN_CHARS):
        raise PermanentSourceError(f"eBook #{gid} did not yield enough readable text.")
    return text
