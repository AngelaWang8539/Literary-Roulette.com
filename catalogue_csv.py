"""Project Gutenberg's published catalogue, the same single file the desktop app uses: pg_catalog.csv.gz.

One small download (seconds) and one pass over it (seconds) give every textual edition. Progress callbacks use
the app's wording and REAL numbers (bytes downloaded, bytes of the file read), never a timer.

    progress(message, percent)      percent is 0-100, or None when unknown
"""
import csv
import gzip
import os
import urllib.error
import urllib.request
from pathlib import Path

import source

CATALOG_URLS = (
    ("Project Gutenberg", "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"),
    ("Project Gutenberg alternate server", "https://dev.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"),
)
LANGUAGES = ("en", "fr", "zh")
USER_AGENT = source.USER_AGENT


def _say(progress, message, percent=None):
    if progress:
        progress(message, percent)


def download(dest, progress=None, *, urls=CATALOG_URLS, opener=urllib.request.urlopen, timeout=60):
    """Download the catalogue to `dest` (atomically). Tries the alternate server if the main one fails."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".{os.getpid()}.download")
    last = None
    for index, (name, url) in enumerate(urls):
        try:
            _say(progress, f"Connecting to {name} catalogue\u2026" if index == 0 else
                 f"The main catalogue server is unavailable; trying {name}\u2026", 2)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
            with opener(req, timeout=timeout) as response:
                try:
                    size = int(response.headers.get("Content-Length") or 0)
                except ValueError:
                    size = 0
                got = 0
                with tmp.open("wb") as out:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        got += len(chunk)
                        if size:
                            pct = int(got * 100 / size)
                            _say(progress, f"Downloading catalogue from {name}\u2026 {pct}%", 5 + int(pct * 0.5))
            gzip.open(tmp, "rb").read(64)                      # must really be gzip data
            os.replace(tmp, dest)
            return dest
        except (urllib.error.URLError, OSError, EOFError, TimeoutError, gzip.BadGzipFile) as exc:
            last = exc
            tmp.unlink(missing_ok=True)
    raise ConnectionError("Project Gutenberg's catalogue could not be reached from either server. "
                          f"Please try again later. ({getattr(last, 'reason', last)})")


def _first(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def read_records(csv_gz_path, progress=None):
    """Textual editions only, one record each. Reports real progress through the file."""
    csv_gz_path = Path(csv_gz_path)
    total = max(1, csv_gz_path.stat().st_size)
    records, seen = [], set()
    with csv_gz_path.open("rb") as raw, gzip.open(raw, mode="rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("The downloaded catalogue has no CSV header.")
        for number, row in enumerate(reader, start=1):
            if _first(row, "Type", "type").casefold() != "text":
                continue
            id_text = _first(row, "Text#", "Text #", "Text", "EBook ID", "eBook ID")
            if not id_text.isdigit() or int(id_text) in seen:
                continue
            gid = int(id_text)
            seen.add(gid)
            records.append({
                "gutenberg_id": gid,
                "title": _first(row, "Title", "title") or f"Untitled ebook #{gid}",
                "authors": _first(row, "Authors", "Author", "authors"),
                "languages": _first(row, "Language", "Languages", "language"),
                "subjects": _first(row, "Subjects", "subjects"),
                "bookshelves": _first(row, "Bookshelves", "bookshelves"),
            })
            if number % 10000 == 0:
                _say(progress, f"Reading catalogue metadata\u2026 {number:,} rows checked", 55 + int(raw.tell() * 25 / total))
    if not records:
        raise ValueError("No textual entries were found in the catalogue. The catalogue format may have changed; check the CSV headers.")
    return records
