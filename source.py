"""Safe Project Gutenberg text fetching and source-health management.

All downloads run in background workers. The source module tries trusted mirrors,
keeps redirects on trusted hosts, caps response size, and remembers unhealthy hosts.
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fetchguard
import textproc
from fetchguard import RateGate, SharedRateGate  # re-exported for existing callers

START_RE = re.compile(r"\*{3,}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3,}", re.I)
END_RE = re.compile(r"\*{3,}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)
MIN_CHARS = {"zh": 300}
DEFAULT_MIN_CHARS = 500
CONNECT_TIMEOUT = 8
TOTAL_DEADLINE = 45
MAX_BYTES = 60_000_000
MAX_TEXT_BYTES = MAX_BYTES  # backwards-compatible name
USER_AGENT = fetchguard.user_agent()

DEFAULT_MIRRORS = (
    "https://gutenberg.pglaf.org",
    "https://aleph.gutenberg.org",
)
OFFICIAL_HOSTS = {
    "gutenberg.pglaf.org",
    "aleph.gutenberg.org",
    "gutenberg.org",
    "www.gutenberg.org",
    "dev.gutenberg.org",
}


class SourceError(Exception):
    """Base source error. Permanent errors are not retried; transient errors may be."""
    permanent = False

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentSourceError(SourceError):
    permanent = True


class TransientSourceError(SourceError):
    permanent = False


class BlockedSourceError(TransientSourceError):
    """A provider refused the request (typically HTTP 403 or 429)."""
    def __init__(self, message, *, code=None, retry_after=None):
        super().__init__(message, retry_after=retry_after)
        self.blocked = True
        self.code = code


class SourceResting(TransientSourceError):
    """All usable mirrors are temporarily resting; wait before retrying this book."""
    def __init__(self, wait):
        self.wait = max(0.0, float(wait))
        super().__init__(f"Project Gutenberg mirrors are resting; retry in about {self.wait:.0f} seconds.",
                         retry_after=self.wait)


def _custom_bases():
    bases = []
    for raw in (os.environ.get("LR_MIRROR_BASES") or "").split(","):
        value = raw.strip().rstrip("/")
        if not value:
            continue
        try:
            p = urlparse(value)
            if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
                continue
            bases.append(value)
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(bases))


def _base_matches(value, base):
    try:
        p, b = urlparse(value), urlparse(base)
        if p.scheme != "https" or (p.hostname or "").lower() != (b.hostname or "").lower():
            return False
        if p.port not in (None, 443) or p.username or p.password:
            return False
        prefix = (b.path or "").rstrip("/")
        path = p.path or "/"
        return not prefix or path == prefix or path.startswith(prefix + "/")
    except (TypeError, ValueError):
        return False


def trusted_url(value):
    """Accept HTTPS URLs on the official mirrors or under an explicitly configured HTTPS mirror base."""
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not host or p.username or p.password or p.port not in (None, 443):
            return False
        if host in OFFICIAL_HOSTS or host.endswith(".gutenberg.org"):
            return True
        return any(_base_matches(value, base) for base in _custom_bases())
    except (TypeError, ValueError):
        return False


def _host(value):
    try:
        return (urlparse(value).hostname or "").lower()
    except (TypeError, ValueError):
        return ""


def source_hosts():
    hosts = [_host(x) for x in DEFAULT_MIRRORS]
    hosts.extend(_host(base) for base in _custom_bases())
    if (os.environ.get("LR_ALLOW_WWW") or "").strip().lower() in {"1", "true", "yes", "on"}:
        hosts.append("www.gutenberg.org")
    return tuple(dict.fromkeys(h for h in hosts if h))


def mirror_urls(gid):
    """Return the preferred mirror chain; www is opt-in and always last."""
    try:
        number = int(gid)
    except (TypeError, ValueError):
        raise ValueError("Gutenberg id must be a positive integer")
    if number <= 0:
        raise ValueError("Gutenberg id must be a positive integer")
    gid_text = str(number)
    urls = [f"{base}/cache/epub/{number}/pg{number}.txt" for base in DEFAULT_MIRRORS]

    # Additional HTTPS mirrors use Gutenberg's classic digit-tree layout.
    directory = "/".join(gid_text[:-1])
    relative_dir = f"{directory + '/' if directory else ''}{gid_text}/"
    for base in _custom_bases():
        root = base.rstrip("/") + "/"
        urls.append(urljoin(root, relative_dir + f"{gid_text}-0.txt"))
        urls.append(urljoin(root, relative_dir + f"{gid_text}.txt"))

    allow_www = (os.environ.get("LR_ALLOW_WWW") or "").strip().lower() in {"1", "true", "yes", "on"}
    if allow_www:
        urls.append(f"https://www.gutenberg.org/cache/epub/{number}/pg{number}.txt")
    return list(dict.fromkeys(urls))


def plain_text_url(formats):
    for key, value in (formats or {}).items():
        if str(key).lower().startswith("text/plain") and isinstance(value, str) and trusted_url(value):
            return value
    return None


class _TrustedRedirects(urllib.request.HTTPRedirectHandler):
    """Reject redirects that leave the trusted official/configured mirror set."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(getattr(req, "full_url", ""), newurl)
        if not trusted_url(target):
            raise PermanentSourceError(f"Blocked an untrusted redirect target: {target}")
        return super().redirect_request(req, fp, code, msg, headers, target)


class SourceHealth:
    """Persistent per-host circuit state with one recovery probe and exponential resting periods."""
    BASE_REST = 120.0
    MAX_REST = 3600.0
    BLOCK_REST = 1800.0
    FAILURE_THRESHOLD = 3

    def __init__(self, path=None, *, clock=time.time, rng=None):
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._records = {}
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._records = {
                    str(host): {
                        "failures": max(0, int(rec.get("failures", 0))),
                        "blocked": max(0, int(rec.get("blocked", 0))),
                        "level": max(0, int(rec.get("level", 0))),
                        "rest_until": float(rec.get("rest_until", 0.0)),
                        "probing": bool(rec.get("probing", False)),
                    }
                    for host, rec in data.items() if isinstance(rec, dict)
                }
        except (OSError, ValueError, TypeError):
            self._records = {}

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._records, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            # Health reporting must never crash a background book-preparation worker.
            pass

    def fail(self, host, *, blocked=False, immediate=False, retry_after=None):
        host = str(host or "").lower()
        if not host:
            return
        with self._lock:
            now = self._clock()
            rec = self._records.setdefault(host, {"failures": 0, "blocked": 0, "level": 0,
                                                   "rest_until": 0.0, "probing": False})
            was_probe = rec["probing"]
            rec["probing"] = False
            if retry_after is not None:
                delay = min(self.MAX_REST, max(0.0, float(retry_after)))
                rec["rest_until"] = now + delay
            elif immediate:
                rec["rest_until"] = now + self.BLOCK_REST
            elif blocked:
                rec["blocked"] += 1
                if rec["blocked"] >= 2:
                    rec["rest_until"] = now + self.BLOCK_REST
            elif was_probe:
                rec["level"] = max(1, rec["level"] + 1)
                delay = min(self.MAX_REST, self.BASE_REST * (2 ** (rec["level"] - 1)))
                rec["rest_until"] = now + delay * (0.5 + self._rng.random())
            else:
                rec["failures"] += 1
                if rec["failures"] >= self.FAILURE_THRESHOLD:
                    rec["level"] = max(1, rec["level"])
                    delay = min(self.MAX_REST, self.BASE_REST * (2 ** (rec["level"] - 1)))
                    rec["rest_until"] = now + delay * (0.5 + self._rng.random())
            self._save()

    def ok(self, host):
        with self._lock:
            self._records.pop(str(host or "").lower(), None)
            self._save()

    def reserve(self, host):
        host = str(host or "").lower()
        if not host:
            return False
        with self._lock:
            rec = self._records.get(host)
            if not rec:
                return True
            now = self._clock()
            if rec["rest_until"] > now:
                return False
            if rec["probing"]:
                return False
            if rec["rest_until"] > 0:
                rec["probing"] = True
                self._save()
            return True

    def rest_left(self, host):
        with self._lock:
            rec = self._records.get(str(host or "").lower())
            return max(0.0, rec["rest_until"] - self._clock()) if rec else 0.0

    def rest_left_all(self, hosts):
        return max((self.rest_left(host) for host in hosts), default=0.0)

    def state(self, hosts):
        hosts = tuple(dict.fromkeys(str(h).lower() for h in hosts if h))
        if not hosts:
            return "ok"
        with self._lock:
            now = self._clock()
            unavailable = []
            for host in hosts:
                rec = self._records.get(host)
                unavailable.append(bool(rec and (rec["rest_until"] > now or rec["probing"])))
            if all(unavailable):
                return "down"
            if any(unavailable):
                return "degraded"
            return "ok"

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._records))


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


def download(url, gid, *, timeout=CONNECT_TIMEOUT, deadline=TOTAL_DEADLINE, opener=None, progress=None):
    """Fetch one trusted mirror once, with redirect validation and a hard response-size cap."""
    started = time.monotonic()
    if not trusted_url(url):
        raise PermanentSourceError(f"eBook #{gid} has no trusted https plain-text URL.")
    if opener is None:
        built = urllib.request.build_opener(_TrustedRedirects())
        opener = lambda req, timeout=None: built.open(req, timeout=timeout)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"})
    try:
        with opener(req, timeout=timeout) as response:
            headers = response.headers
            content_type = headers.get_content_type().lower() if hasattr(headers, "get_content_type") else ""
            try:
                size = int(headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                size = 0
            if size > MAX_BYTES:
                raise PermanentSourceError(f"eBook #{gid}: the file is larger than the {MAX_BYTES:,}-byte safety limit.")
            chunks, got = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_BYTES:
                    raise PermanentSourceError(f"eBook #{gid}: the file is larger than the {MAX_BYTES:,}-byte safety limit.")
                chunks.append(chunk)
                if progress:
                    progress(got / size if size else None, got)
                if time.monotonic() - started > deadline:
                    raise TransientSourceError(f"eBook #{gid}: the server was too slow (over {deadline} s)")
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        retry_after = fetchguard.parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        if exc.code in (403, 429):
            raise BlockedSourceError(f"eBook #{gid}: HTTP {exc.code}", code=exc.code, retry_after=retry_after) from exc
        if exc.code in (408, 425, 500, 502, 503, 504):
            raise TransientSourceError(f"eBook #{gid}: HTTP {exc.code}", retry_after=retry_after) from exc
        raise PermanentSourceError(f"eBook #{gid}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientSourceError(f"eBook #{gid}: {getattr(exc, 'reason', exc)}") from exc
    if content_type in ("text/html", "application/json", "application/xhtml+xml") or \
            raw[:512].lstrip().lower().startswith((b"<!doctype html", b"<html", b'{"error"')):
        raise PermanentSourceError(f"eBook #{gid} returned an error page instead of book text.")
    return raw


def fetch_clean_text(book, *, downloader=download, progress=None, health=None):
    """Try official mirrors and configured extras; only mark missing after every mirror was actually checked."""
    language, gid = book["language"], int(book["gutenberg_id"])
    urls = mirror_urls(gid)
    supplied = book.get("text_urls") or ([book.get("text_url")] if book.get("text_url") else [])
    for url in supplied:
        if isinstance(url, str) and trusted_url(url) and url not in urls:
            urls.append(url)

    raw, errors, skipped = None, [], False
    for url in urls:
        host = _host(url)
        if health is not None and not health.reserve(host):
            skipped = True
            continue
        try:
            raw = downloader(url, gid, progress=progress) if progress else downloader(url, gid)
            if health is not None:
                health.ok(host)
            break
        except SourceError as exc:
            errors.append(exc)
            if health is not None:
                if isinstance(exc, BlockedSourceError):
                    health.fail(host, blocked=True, immediate=(exc.code == 429), retry_after=exc.retry_after)
                elif isinstance(exc, TransientSourceError):
                    health.fail(host, retry_after=exc.retry_after)
                else:
                    # A 404 proves that this host answered; the book may simply not exist on this mirror.
                    health.ok(host)

    if raw is None:
        if skipped:
            wait = health.rest_left_all(source_hosts()) if health is not None else 1.0
            raise SourceResting(max(1.0, wait))
        if errors and all(e.permanent for e in errors):
            raise errors[-1]
        transient = next((e for e in reversed(errors) if not e.permanent), None)
        if transient:
            raise transient
        raise PermanentSourceError(f"eBook #{gid} has no trusted https plain-text URL.")

    text = clean_for_language(decode_bytes(raw), language)
    if len(text.strip()) < MIN_CHARS.get(language, DEFAULT_MIN_CHARS):
        raise PermanentSourceError(f"eBook #{gid} did not yield enough readable text.")
    return text
