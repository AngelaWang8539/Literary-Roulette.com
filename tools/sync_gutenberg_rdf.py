#!/usr/bin/env python3
"""Build Literary Roulette's local metadata catalogue from Project Gutenberg's daily RDF archive.

Metadata only: title, author, language, subjects/bookshelves, text/plain URLs and
Project Gutenberg's approximate downloads in the last 30 days. Full book text is
still fetched by the web app only after a book is selected.

Run from the project root:
    python3 tools/sync_gutenberg_rdf.py
"""
from __future__ import annotations

import argparse
import bz2
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "instance" / "gutenberg_rdf" / "rdf-files.tar.bz2"
DEFAULT_OUTPUT = ROOT / "data" / "gutendex_catalog.json"
ARCHIVE_URL = "https://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2"
USER_AGENT = "LiteraryRoulette/0.5 (metadata catalogue importer; contact: project maintainer)"
LANGUAGES = ("en", "fr", "zh")

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
    "dcam": "http://purl.org/dc/dcam/",
}
RDF = "{" + NS["rdf"] + "}"
DCT = "{" + NS["dcterms"] + "}"
PG = "{" + NS["pgterms"] + "}"

LANGUAGE_ALIASES = {
    "en": "en", "eng": "en", "english": "en",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "français": "fr", "francais": "fr",
    "zh": "zh", "zho": "zh", "chi": "zh", "chinese": "zh", "mandarin": "zh",
    "simplified chinese": "zh", "traditional chinese": "zh", "中文": "zh", "汉语": "zh", "漢語": "zh",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _normalise_language(raw: str | None) -> str | None:
    if not raw:
        return None
    value = urllib.parse.unquote(str(raw)).strip().casefold()
    if value in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[value]
    # RFC 4646 subtags such as en-US, fr-CA, zh-Hans and zh-Hant.
    if value.startswith(("en-", "en_")):
        return "en"
    if value.startswith(("fr-", "fr_")):
        return "fr"
    if value.startswith(("zh-", "zh_")):
        return "zh"
    # RDF records may express ISO 639 codes in a URI, e.g. .../eng or .../fre.
    tail = re.split(r"[/#:_\s-]+", value)[-1]
    if tail in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[tail]
    # Some cataloguers use a language label inside a longer literal.
    for label, code in (("english", "en"), ("french", "fr"), ("français", "fr"),
                        ("chinese", "zh"), ("中文", "zh"), ("汉语", "zh"), ("漢語", "zh")):
        if label in value:
            return code
    return None


def _element_values(parent: ET.Element, tag: str) -> list[str]:
    values: list[str] = []
    for node in parent.iter(tag):
        if node.text and node.text.strip():
            values.append(node.text.strip())
        # A few RDF variants encode the value only as a resource/about URI.
        for attr in (RDF + "resource", RDF + "about"):
            value = node.get(attr)
            if value:
                values.append(value.strip())
    return values


def _text(parent: ET.Element, tag: str) -> str | None:
    node = parent.find(tag)
    if node is not None and node.text and node.text.strip():
        return node.text.strip()
    return None


def _ebook_id(root: ET.Element, member_name: str) -> int | None:
    for node in root.iter(PG + "ebook"):
        about = node.get(RDF + "about", "")
        match = re.search(r"(?:ebooks/|/)(\d+)(?:\.rdf)?/?$", about)
        if match:
            return int(match.group(1))
    match = re.search(r"(?:pg)?(\d+)\.rdf$", Path(member_name).name, re.I)
    return int(match.group(1)) if match else None


def _https_gutenberg_url(value: str | None) -> str | None:
    if not value:
        return None
    url = urllib.parse.urljoin("https://www.gutenberg.org/", value.strip())
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("gutenberg.org", "www.gutenberg.org") and not host.endswith(".gutenberg.org"):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    return urllib.parse.urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def _format_map(ebook: ET.Element) -> dict[str, str]:
    found: dict[str, str] = {}
    for has_format in ebook.findall(DCT + "hasFormat"):
        for file_node in has_format.iter(PG + "file"):
            about = file_node.get(RDF + "about")
            if not about:
                continue
            mime_values = _element_values(file_node, RDF + "value")
            for mime in mime_values:
                mime_clean = mime.strip()
                parsed_url = urllib.parse.urlparse(about)
                # Some RDF entries describe a ZIP container that contains text/plain.
                # Do not mistake that ZIP for a directly readable text response.
                is_direct_text_file = bool(re.search(r"\.txt(?:\.[a-z0-9_-]+)?$", parsed_url.path, re.I))
                if mime_clean.casefold().startswith("text/plain") and is_direct_text_file:
                    url = _https_gutenberg_url(about)
                    if url:
                        found[mime_clean] = url
    # The book reader selects the first text/plain URL. Prefer UTF-8 over
    # legacy encodings so accented French/CJK text is decoded reliably.
    def format_priority(item):
        label = item[0].casefold()
        if "charset=utf-8" in label or "charset=utf8" in label:
            return 0
        if "charset=us-ascii" in label or "charset=ascii" in label:
            return 1
        return 2
    return dict(sorted(found.items(), key=format_priority))


def parse_rdf_record(raw: bytes, member_name: str = "unknown.rdf") -> dict | None:
    """Return one Gutendex-compatible record, or None for ineligible/unsupported items."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    ebook = next(root.iter(PG + "ebook"), None)
    if ebook is None:
        return None
    gid = _ebook_id(root, member_name)
    if gid is None:
        return None

    title = _text(ebook, DCT + "title") or _text(ebook, "{http://purl.org/dc/elements/1.1/}title")
    if not title:
        title = f"Gutenberg eBook #{gid}"

    authors: list[dict[str, str]] = []
    for creator in ebook.findall(DCT + "creator"):
        names = _element_values(creator, PG + "name")
        if not names:
            names = _element_values(creator, RDF + "value")
        for name in names:
            if name and name not in [a["name"] for a in authors]:
                authors.append({"name": name})

    language_values: list[str] = []
    for language_node in ebook.findall(DCT + "language"):
        language_values.extend(_element_values(language_node, RDF + "value"))
        language_values.extend(_element_values(language_node, RDF + "Description"))
        for attr in (RDF + "resource", RDF + "about"):
            if language_node.get(attr):
                language_values.append(language_node.get(attr, ""))
        for node in language_node.iter():
            for attr in (RDF + "resource", RDF + "about"):
                if node.get(attr):
                    language_values.append(node.get(attr, ""))
    language_codes = sorted({code for value in language_values if (code := _normalise_language(value))})
    language_codes = [code for code in language_codes if code in LANGUAGES]
    if not language_codes:
        return None

    rights_text = " ".join(_element_values(ebook, DCT + "rights")).casefold()
    # Preserve the same conservative US public-domain rule used by the Gutendex query.
    if "public domain in the usa" in rights_text or "public domain in usa" in rights_text:
        copyright = False
    elif "copyrighted" in rights_text or "all rights reserved" in rights_text:
        copyright = True
    else:
        copyright = None
    if copyright is not False:
        return None

    downloads_text = _text(ebook, PG + "downloads")
    try:
        download_count = max(0, int(downloads_text or 0))
    except ValueError:
        download_count = 0

    formats = _format_map(ebook)
    if not formats:
        return None

    subjects = []
    for subject in ebook.findall(DCT + "subject"):
        values = _element_values(subject, RDF + "value")
        subjects.extend(values)
    bookshelves = []
    for shelf in ebook.findall(PG + "bookshelf"):
        values = _element_values(shelf, RDF + "value")
        if not values:
            resource = shelf.get(RDF + "resource") or ""
            if resource:
                values = [urllib.parse.unquote(resource.rstrip("/").split("/")[-1]).replace("_", " ")]
        bookshelves.extend(values)

    return {
        "id": gid,
        "title": title,
        "authors": authors,
        "subjects": list(dict.fromkeys(x for x in subjects if x)),
        "bookshelves": list(dict.fromkeys(x for x in bookshelves if x)),
        "languages": language_codes,
        "copyright": copyright,
        "download_count": download_count,
        "formats": formats,
    }


def parse_archive(archive_path: Path, languages: tuple[str, ...] = LANGUAGES) -> dict[str, list[dict]]:
    collected = {code: {} for code in languages}
    seen_rdf = 0
    with tarfile.open(archive_path, mode="r|bz2") as archive:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".rdf"):
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            raw = stream.read()
            seen_rdf += 1
            record = parse_rdf_record(raw, member.name)
            if record is None:
                continue
            for code in record["languages"]:
                if code not in collected:
                    continue
                prior = collected[code].get(record["id"])
                if prior is None or record["download_count"] > prior["download_count"]:
                    collected[code][record["id"]] = record
            if seen_rdf % 10000 == 0:
                print(f"  Parsed {seen_rdf:,} RDF records; eligible: " + ", ".join(f"{k}={len(v):,}" for k, v in collected.items()), flush=True)

    result = {}
    for code, rows in collected.items():
        result[code] = sorted(rows.values(), key=lambda row: (-row["download_count"], row["id"]))
    if not any(result.values()):
        raise RuntimeError("The RDF archive produced no eligible English, French or Chinese plain-text records. The old catalogue was left untouched.")
    for code in languages:
        if not result[code]:
            raise RuntimeError(f"The RDF archive produced zero eligible {code} records. The old catalogue was left untouched; check the language parser before retrying.")
    return result


def download_archive(archive_path: Path, timeout: int = 180, retries: int = 4, force: bool = False) -> str | None:
    """Download the official archive with conditional requests and resumable partial files."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial = archive_path.with_suffix(archive_path.suffix + ".part")
    state_path = archive_path.with_suffix(archive_path.suffix + ".http.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    last_error = None
    for attempt in range(retries + 1):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/x-bzip2, application/octet-stream, */*", "Connection": "close"}
        offset = partial.stat().st_size if partial.exists() else 0
        if offset:
            headers["Range"] = f"bytes={offset}-"
            # Do not append bytes from a newer archive to an older partial file.
            validator = state.get("etag") or state.get("last_modified")
            if validator:
                headers["If-Range"] = validator
        elif not force:
            if state.get("last_modified"):
                headers["If-Modified-Since"] = state["last_modified"]
            if state.get("etag"):
                headers["If-None-Match"] = state["etag"]
        req = urllib.request.Request(ARCHIVE_URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                if offset and status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise RuntimeError(f"Server returned an unexpected range response: {content_range!r}")
                    mode = "ab"
                else:
                    mode = "wb"
                with partial.open(mode) as target:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                last_modified = response.headers.get("Last-Modified")
                etag = response.headers.get("ETag")
            # A full BZip2 read verifies the downloaded archive before promoting it.
            try:
                with bz2.open(partial, "rb") as check:
                    while check.read(4 * 1024 * 1024):
                        pass
            except (OSError, EOFError) as exc:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded RDF archive failed its BZip2 integrity check: {exc}") from exc
            os.replace(partial, archive_path)
            _atomic_json(state_path, {"last_modified": last_modified or state.get("last_modified"), "etag": etag or state.get("etag"), "checked_at": now_iso()})
            print(f"  RDF archive downloaded and verified ({archive_path.stat().st_size / (1024 * 1024):.1f} MiB).", flush=True)
            return True, last_modified or state.get("last_modified")
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and archive_path.is_file():
                print("  Official RDF archive has not changed; reusing the verified local copy.", flush=True)
                return False, state.get("last_modified")
            last_error = f"HTTP {exc.code}"
            if exc.code == 416 and partial.exists():
                partial.unlink(missing_ok=True)
        except (urllib.error.URLError, TimeoutError, OSError, EOFError, RuntimeError) as exc:
            last_error = str(getattr(exc, "reason", exc))
        if attempt < retries:
            wait = min(60, 5 * (2 ** attempt))
            print(f"  RDF download interrupted ({last_error}); retry {attempt + 1}/{retries} in {wait}s.", file=sys.stderr, flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Could not download/verify Project Gutenberg's RDF archive after {retries + 1} attempts: {last_error}. Any partial download is kept for a range-resume retry.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a local metadata catalogue from Project Gutenberg's daily RDF archive")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=180, help="Per network operation timeout in seconds")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--force-download", action="store_true", help="Ignore the archive's HTTP freshness validators")
    args = parser.parse_args()
    if args.timeout < 30 or args.retries < 0:
        parser.error("--timeout must be >=30 and --retries must be >=0")

    print("Checking Project Gutenberg's official daily RDF metadata archive…", flush=True)
    archive_changed, source_updated = download_archive(args.archive, args.timeout, args.retries, args.force_download)
    if not archive_changed and args.output.is_file():
        # A 304 means the local archive and catalogue are still current. Avoid
        # decompressing and reparsing the entire archive on every daily check.
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
            if previous.get("sync_complete") is True and all(previous.get("counts", {}).get(code, 0) > 0 for code in LANGUAGES):
                os.utime(args.output, None)
                print("The saved catalogue already matches the current official archive; no re-parse needed.", flush=True)
                print(f"Saved catalogue: {args.output}")
                return 0
        except (OSError, ValueError, AttributeError):
            pass
    print("Parsing bibliographic records and download statistics…", flush=True)
    languages = parse_archive(args.archive)
    counts = {code: len(rows) for code, rows in languages.items()}

    # Avoid promoting a clearly incomplete parse over the last good snapshot.
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
            previous_rows = previous.get("languages", {}) if isinstance(previous, dict) else {}
            for code, count in counts.items():
                old_count = len(previous_rows.get(code, [])) if isinstance(previous_rows, dict) else 0
                if old_count >= 100 and count < old_count * 0.70:
                    raise RuntimeError(f"New {code} catalogue ({count:,}) is unexpectedly smaller than the previous snapshot ({old_count:,}); refusing to replace it.")
        except (OSError, ValueError):
            pass

    payload = {
        "provider": "Project Gutenberg official RDF metadata archive",
        "generated_at": now_iso(),
        "source_last_modified": source_updated,
        "sync_complete": True,
        "filters": {
            "copyright": "Public domain in the USA only; unknown/copyrighted records excluded",
            "text_format": "text/plain only",
            "languages": list(LANGUAGES),
            "max_pages": None,
            "popularity_scope": "Project Gutenberg approximate downloads in the last 30 days; updated daily",
            "percentile_scope": "Complete eligible RDF snapshot, compared separately within each language",
        },
        "counts": counts,
        "languages": languages,
    }
    _atomic_json(args.output, payload)
    print("\nCatalogue updated successfully:")
    for code in LANGUAGES:
        print(f"  {code}: {counts.get(code, 0):,} eligible text books")
    print(f"Saved: {args.output}")
    print("Full book text was NOT downloaded. The website will fetch and cache text only after a title is selected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
