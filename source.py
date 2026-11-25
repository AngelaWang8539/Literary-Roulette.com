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

import fetchguard
import textproc
from fetchguard import RateGate, SharedRateGate  # noqa: F401  (re-exported: prep.py and tests import them from here)

START_RE = re.compile(r"\*{3,}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3,}", re.I)
END_RE = re.compile(r"\*{3,}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)
MIN_CHARS = {"zh": 300}
DEFAULT_MIN_CHARS = 500
CONNECT_TIMEOUT = 8          # seconds to connect / to wait for any silent server before trying the next address
TOTAL_DEADLINE = 45         # one download attempt never runs longer than this, however slowly it trickles
MAX_TEXT_BYTES = 60_000_000    # the longest real book is well below this; anything bigger is not a book
USER_AGENT = fetchguard.user_agent()


class SourceError(Exception):
    """Base class. `permanent` tells the worker whether retrying can ever help."""
    permanent = False
    retry_after = None           # seconds the server asked us to wait (Retry-After), if it said


class PermanentSourceError(SourceError):
    permanent = True


class TransientSourceError(SourceError):
    permanent = False


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


def download(url, gid, *, timeout=CONNECT_TIMEOUT, deadline=TOTAL_DEADLINE, opener=fetchguard.safe_opener(fetchguard.GUTENBERG_HOSTS), progress=None):
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
            if size > MAX_TEXT_BYTES:
                raise PermanentSourceError(f"eBook #{gid}: the file is far larger than any book.")
            chunks, got = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
                if got > MAX_TEXT_BYTES:
                    raise PermanentSourceError(f"eBook #{gid}: the file is far larger than any book.")
                if progress:
                    progress(got / size if size else None, got)
                if time.monotonic() - started > deadline:
                    raise TransientSourceError(f"eBook #{gid}: the server was too slow (over {deadline} s)")
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code in (408, 425, 429, 500, 502, 503, 504):
            err = TransientSourceError(f"eBook #{gid}: HTTP {exc.code}")
            err.retry_after = fetchguard.parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise err from exc
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
