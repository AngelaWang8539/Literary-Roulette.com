"""Project Gutenberg's published catalogue, the same single file the desktop app uses: pg_catalog.csv.gz.

One small download (seconds) and one pass over it (seconds) give every textual edition. Progress callbacks use
the app's wording and REAL numbers (bytes downloaded, bytes of the file read), never a timer.

    progress(message, percent)      percent is 0-100, or None when unknown
"""
import csv
import gzip
import json
import os
import time
import zlib
import urllib.error
import urllib.request
from pathlib import Path

import fetchguard
import source

MAX_CATALOGUE_BYTES = 200_000_000      # the real file is about 15-20 MB; anything near this is not the catalogue
CATALOG_URLS = (
    ("Project Gutenberg", "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"),
    ("Project Gutenberg alternate server", "https://dev.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"),
)
LANGUAGES = ("en", "fr", "zh")
USER_AGENT = source.USER_AGENT


class NotModified(Exception):
    """The server says the catalogue has not changed since the copy we already have."""


class ValidationError(ValueError):
    """The downloaded catalogue looks damaged or partial. The index and the Registry keep their last good data."""


# ---- small normalisers shared with the Registry and the index ----------------------------------------------------
def clean_title(title):
    parts = [p.strip() for p in str(title).replace("\r", "").split("\n") if p.strip()]
    return ": ".join(parts) if parts else str(title).strip()


def split_list(text, limit):
    return [p.strip() for p in str(text or "").replace("--", " \u2013 ").split(";") if p.strip()][:limit]


def first_language(value):
    return str(value or "").replace(",", ";").split(";")[0].strip().lower()


def _validators_path(dest):
    return Path(dest).with_name(Path(dest).name + ".validators.json")


def _say(progress, message, percent=None):
    if progress:
        progress(message, percent)


_SAFE_OPEN = fetchguard.safe_opener(fetchguard.GUTENBERG_HOSTS)


def verify_gzip(path):
    """Read the whole archive once: a truncated or corrupt download fails here, never halfway through an index build."""
    with gzip.open(path, "rb") as handle:
        while handle.read(1 << 20):
            pass


def download(dest, progress=None, *, urls=CATALOG_URLS, opener=_SAFE_OPEN, timeout=10, deadline=180, conditional=True,
             max_bytes=MAX_CATALOGUE_BYTES):
    """Download the catalogue to `dest` (atomically). Tries the alternate server if the main one fails."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".{os.getpid()}.download")
    last = None
    for index, (name, url) in enumerate(urls):
        started = time.monotonic()
        if not fetchguard.host_ok(url, fetchguard.GUTENBERG_HOSTS):
            last = ValueError(f"{url} is not an https address on an allowed host")
            continue
        try:
            _say(progress, f"Connecting to {name} catalogue\u2026" if index == 0 else
                 f"The main catalogue server is unavailable; trying {name}\u2026", 2)
            headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            if dest.exists() and conditional:                  # ask "has it changed?" first: usually it has not, and then nothing is downloaded
                try:
                    known = json.loads(_validators_path(dest).read_text(encoding="utf-8"))
                    if known.get("etag"):
                        headers["If-None-Match"] = known["etag"]
                    if known.get("last_modified"):
                        headers["If-Modified-Since"] = known["last_modified"]
                except (OSError, ValueError):
                    pass
            req = urllib.request.Request(url, headers=headers)
            with opener(req, timeout=timeout) as response:
                try:
                    size = int(response.headers.get("Content-Length") or 0)
                except ValueError:
                    size = 0
                if size > max_bytes:
                    raise OSError(f"{name} announced a file of {size:,} bytes, which is not the catalogue")
                got = 0
                with tmp.open("wb") as out:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        got += len(chunk)
                        if got > max_bytes:
                            raise OSError(f"{name} sent more than {max_bytes:,} bytes")
                        if size:
                            pct = int(got * 100 / size)
                            _say(progress, f"Downloading catalogue from {name}\u2026 {pct}%", 5 + int(pct * 0.5))
                        else:                                   # the server did not say how big: show bytes, never a stuck number
                            _say(progress, f"Downloading catalogue from {name}\u2026 {got / 1e6:.1f} MB", None)
                        if time.monotonic() - started > deadline:
                            raise TimeoutError(f"{name} was too slow (over {deadline} s)")
            verify_gzip(tmp)                                   # must really be complete gzip data, end to end
            os.replace(tmp, dest)
            try:
                _validators_path(dest).write_text(json.dumps({"etag": response.headers.get("ETag"),
                                                              "last_modified": response.headers.get("Last-Modified")}), encoding="utf-8")
            except OSError:
                pass
            return dest
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and dest.exists():
                raise NotModified() from exc
            last = exc
            tmp.unlink(missing_ok=True)
        except (urllib.error.URLError, OSError, EOFError, TimeoutError, gzip.BadGzipFile, zlib.error) as exc:
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


def read_records(csv_gz_path, progress=None, stats=None):
    """Textual editions only, one record each. Reports real progress through the file. `stats` (a dict) is filled with
    what was seen, so validate_records can judge the file."""
    csv_gz_path = Path(csv_gz_path)
    total = max(1, csv_gz_path.stat().st_size)
    records, seen = [], set()
    stats = stats if stats is not None else {}
    stats.update(rows=0, text_rows=0, duplicates=0, bad_ids=0)
    with csv_gz_path.open("rb") as raw, gzip.open(raw, mode="rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("The downloaded catalogue has no CSV header.")
        lowered = {str(name).strip().lower() for name in reader.fieldnames}
        missing = [name for name in ("title", "language") if name not in lowered and name + "s" not in lowered]
        if missing:
            raise ValidationError("The catalogue is missing the expected column(s): " + ", ".join(missing) + ".")
        for number, row in enumerate(reader, start=1):
            stats["rows"] = number
            if _first(row, "Type", "type").casefold() != "text":
                continue
            stats["text_rows"] += 1
            id_text = _first(row, "Text#", "Text #", "Text", "EBook ID", "eBook ID")
            if not id_text.isdigit():
                stats["bad_ids"] += 1
                continue
            if int(id_text) in seen:
                stats["duplicates"] += 1
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


def validate_records(records, stats=None, previous_counts=None):
    """Judge a freshly read catalogue BEFORE anything is replaced. Raises ValidationError; returns a small report.

    Checks: ids and languages are sane, duplicate and empty-title rates are low, English is present, and no language
    shrank suddenly compared with the index that is live now (a partial file must never replace a good one)."""
    stats = stats or {}
    counts = {code: 0 for code in LANGUAGES}
    empty_title = no_language = 0
    for r in records:
        code = first_language(r.get("languages"))
        if code in counts:
            counts[code] += 1
        elif not code:
            no_language += 1
        if str(r.get("title", "")).startswith("Untitled ebook #"):
            empty_title += 1
    total = len(records)
    problems = []
    if not counts["en"]:
        problems.append("no English entries were found")
    text_rows = max(1, stats.get("text_rows", total))
    if text_rows >= 200:                                      # rates mean something only for a real-sized file
        if stats.get("duplicates", 0) / text_rows > 0.05:
            problems.append(f"{stats['duplicates']:,} duplicate ids")
        if stats.get("bad_ids", 0) / text_rows > 0.01:
            problems.append(f"{stats['bad_ids']:,} rows without a numeric id")
        if empty_title / max(1, total) > 0.05:
            problems.append(f"{empty_title:,} entries without a title")
        if no_language / max(1, total) > 0.10:
            problems.append(f"{no_language:,} entries without a language")
    for code, before in (previous_counts or {}).items():
        if before >= 100 and counts.get(code, 0) < before * 0.8:
            problems.append(f"{code}: {counts.get(code, 0):,} entries now against {before:,} in the live index")
    if problems:
        raise ValidationError("The downloaded catalogue was not used: " + "; ".join(problems) + ". The previous catalogue stays live.")
    return {"counts": counts, "total": total, "duplicates": stats.get("duplicates", 0), "empty_titles": empty_title}
