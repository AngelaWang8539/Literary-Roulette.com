"""Metadata-first Gutendex library with on-demand Project Gutenberg text caching."""
from __future__ import annotations
import bisect, json, math, os, random, re, threading, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlparse
import textproc

LANGUAGE_LABELS = {"en": "English", "fr": "French", "zh": "Chinese"}
START_RE = re.compile(r"\*{3,}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3,}", re.I)
END_RE = re.compile(r"\*{3,}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)


def _trusted_url(value):
    try:
        p = urlparse(value); host = (p.hostname or "").lower()
        return p.scheme == "https" and (host == "gutenberg.org" or host.endswith(".gutenberg.org"))
    except ValueError:
        return False


def clean_gutenberg_text(raw):
    text = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    start, end = START_RE.search(text), END_RE.search(text)
    if start: text = text[start.end():]
    if end := END_RE.search(text): text = text[:end.start()]
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def _plain_text_url(formats):
    for key, value in formats.items():
        if str(key).lower().startswith("text/plain") and isinstance(value, str) and _trusted_url(value):
            return value
    return None


def _rank(records):
    # Use binary search so ranking thousands of catalogue entries stays fast.
    values = sorted(max(0, int(r.get("download_count") or 0)) for r in records)
    total = len(values)
    for record in records:
        downloads = max(0, int(record.get("download_count") or 0))
        rank = 1 + total - bisect.bisect_right(values, downloads)  # ties share competition rank
        record["popularity_rank"] = rank
        record["popularity_total"] = total
        record["popularity_top_percent"] = max(1, min(100, math.ceil(rank * 100 / total))) if total else 100


class GutendexPool:
    def __init__(self, catalog_path, cache_dir, max_cache_files=8,
                 max_cache_bytes=64 * 1024 * 1024, cache_ttl_seconds=14 * 24 * 3600,
                 max_download_bytes=32 * 1024 * 1024):
        self.catalog_path, self.cache_dir = Path(catalog_path), Path(cache_dir)
        self.max_cache_files = max(1, int(max_cache_files))
        self.max_cache_bytes = max(1, int(max_cache_bytes))
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds))
        self.max_download_bytes = max(1024 * 1024, int(max_download_bytes))
        self.snapshot = {}
        self.records = {code: [] for code in LANGUAGE_LABELS}
        self.by_gid = {code: {} for code in LANGUAGE_LABELS}
        self.load_error = None
        self.is_partial_snapshot = False
        self.partial_reason = None
        self._lock = threading.Lock()
        self._book_token_index = {}
        try:
            data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            self.snapshot = data if isinstance(data, dict) else {}
            filters = self.snapshot.get("filters", {}) if isinstance(self.snapshot, dict) else {}
            filters = filters if isinstance(filters, dict) else {}
            self.is_partial_snapshot = filters.get("max_pages") is not None
            if self.is_partial_snapshot:
                self.partial_reason = f"metadata was limited to {filters.get('max_pages')} page(s)"
            langs = self.snapshot.get("languages", {})
            for code in LANGUAGE_LABELS:
                rows = langs.get(code, []) if isinstance(langs, dict) else []
                rows = [r for r in rows if isinstance(r, dict) and str(r.get("id", "")).isdigit()]
                _rank(rows)
                self.records[code] = rows
                self.by_gid[code] = {int(r["id"]): r for r in rows}
        except FileNotFoundError:
            self.load_error = "Project Gutenberg metadata has not been synced yet."
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = f"Project Gutenberg metadata could not be read: {exc}"
        self.prune_cache()

    def count(self, language):
        return len(self.records.get(language, []))

    def popularity_payload(self, record, language):
        # A page-limited preview is not a valid ranking denominator. Hide its
        # percentiles instead of presenting a misleading "top X%" claim.
        if self.is_partial_snapshot:
            return None
        pct = max(1, int(record.get("popularity_top_percent") or 100))
        rank = int(record.get("popularity_rank") or 0)
        total = int(record.get("popularity_total") or self.count(language))
        downloads = int(record.get("download_count") or 0)
        language_name = LANGUAGE_LABELS.get(language, language)

        # Extreme labels are only meaningful with a reasonably sized collection.
        if total >= 100 and pct <= 1:
            tier = "jackpot"
            label = "Jackpot! A top 1% Gutenberg favourite!"
        elif total >= 100 and pct <= 10:
            tier = "lucky"
            label = "Lucky find! Top 10% most downloaded!"
        elif total >= 100 and pct >= 91:
            tier = "hidden-gem"
            label = "Hidden gem alert! Among the least-downloaded 10% — would you like to explore?"
        else:
            tier = "discovery"
            label = f"A new discovery · ranked #{rank:,} by downloads in the last 30 days"

        return {
            "tier": tier, "label": label,
            "detail": f"Rank #{rank:,} of {total:,} indexed {language_name} titles · {downloads:,} approximate downloads in the last 30 days",
            "rank": rank, "total": total, "downloads_30d": downloads, "top_percent": pct,
            "snapshot_date": self.snapshot.get("generated_at"),
            "source_url": f"https://www.gutenberg.org/ebooks/{record['id']}",
        }

    def attach_popularity(self, catalogues):
        missing = []
        token_index = {}
        for language, cat in catalogues.items():
            for bid, book in cat.books.items():
                token_index[cat.book_token(book)] = ("curated", language, bid)
                gid = book.get("gutenberg_id")
                if str(gid or "").isdigit():
                    record = self.by_gid.get(language, {}).get(int(gid))
                    if record:
                        payload = self.popularity_payload(record, language)
                        if payload:
                            book["popularity"] = payload
                        else:
                            book.pop("popularity", None)
                    else:
                        missing.append({"language": language, "title": book.get("title"), "gutenberg_id": int(gid)})
            for record in self.records.get(language, []):
                bid = f"pg-{language}-{int(record['id'])}"
                token_index[cat.token("b", bid)] = ("gutenberg", language, record)
        self._book_token_index = token_index
        return missing

    def resolve_book_token(self, token, catalogues):
        """Metadata-only stable book lookup; this never reads or downloads full text."""
        entry = self._book_token_index.get(token) if isinstance(token, str) else None
        if not entry:
            return None
        kind, language, value = entry
        cat = catalogues.get(language)
        if cat is None:
            return None
        if kind == "curated":
            book = cat.books.get(value)
            return (language, cat, book) if book else None
        return language, cat, self._book_record(value, language)

    def prune_cache(self):
        """Bound persistent downloaded full-text cache by age, file count and bytes."""
        if not self.cache_dir.is_dir():
            return
        now = time.time()
        candidates = []
        for path in self.cache_dir.rglob("*"):
            # Clean abandoned atomic-write leftovers, but leave recent temporary
            # files alone because another worker may still be writing one.
            if path.is_file() and path.name.startswith("pg") and path.name.endswith(".txt.tmp"):
                try:
                    if now - path.stat().st_mtime > 3600:
                        path.unlink()
                except OSError:
                    pass
                continue
            if not path.is_file() or not re.fullmatch(r"pg\d+\.txt", path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > self.cache_ttl_seconds or stat.st_size > self.max_cache_bytes:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            candidates.append((path, stat.st_mtime, stat.st_size))
        candidates.sort(key=lambda row: row[1], reverse=True)
        total_bytes = 0
        kept = 0
        for path, _mtime, size in candidates:
            within_bytes = total_bytes + size <= self.max_cache_bytes
            if kept < self.max_cache_files and within_bytes:
                kept += 1
                total_bytes += size
            else:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _book_record(self, record, language):
        gid = int(record["id"])
        formats = record.get("formats") if isinstance(record.get("formats"), dict) else {}
        text_url = _plain_text_url(formats)
        if not text_url: raise RuntimeError(f"Gutenberg eBook #{gid} has no trusted plain-text URL.")
        authors = [a.get("name", "") for a in (record.get("authors") or []) if isinstance(a, dict) and a.get("name")]
        author = ", ".join(authors) or "Author not listed"
        genres = record.get("bookshelves") or record.get("subjects") or []
        title = str(record.get("title") or f"Gutenberg eBook #{gid}")
        if language == "zh":
            try:
                from opencc import OpenCC
            except ImportError as exc:
                raise RuntimeError("Chinese conversion needs OpenCC. Run: python3 -m pip install opencc-python-reimplemented") from exc
            convert = OpenCC("t2s").convert
            title, author = convert(title), convert(author)
            genres = [convert(str(g)) for g in genres]
        return {
            "id": f"pg-{language}-{gid}", "title": title, "author": author,
            "genres": [str(g) for g in genres[:3]], "description": "", "language": language,
            "gutenberg_id": gid, "rights_status": "public_domain_candidate",
            "source_provider": "Project Gutenberg", "source_url": f"https://www.gutenberg.org/ebooks/{gid}",
            "source_credit": f"Text source: Project Gutenberg eBook #{gid}",
            "source_license_note": "Project Gutenberg records this title as public domain in the USA. Copyright status can differ by country.",
            "text_url": text_url, "popularity": self.popularity_payload(record, language),
        }

    def _cache_path(self, language, gid):
        folder = self.cache_dir / language
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"pg{gid}.txt"

    def _fetch_text(self, book):
        language, gid = book["language"], int(book["gutenberg_id"])
        cache = self._cache_path(language, gid)
        if cache.is_file():
            try:
                os.utime(cache, None)
            except OSError:
                pass
            return cache.read_text(encoding="utf-8")
        with self._lock:
            if cache.is_file():
                try:
                    os.utime(cache, None)
                except OSError:
                    pass
                return cache.read_text(encoding="utf-8")
            req = urllib.request.Request(book["text_url"], headers={
                "User-Agent": "LiteraryRoulette/0.5 (on-demand text reader)",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            last_error = None
            raw = None
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        content_type = response.headers.get_content_type().lower()
                        length_header = response.headers.get("Content-Length")
                        if length_header and int(length_header) > self.max_download_bytes:
                            raise RuntimeError(
                                f"Project Gutenberg eBook #{gid} exceeds the "
                                f"{self.max_download_bytes // (1024 * 1024)} MiB per-book text limit."
                            )
                        raw = response.read(self.max_download_bytes + 1)
                    if len(raw) > self.max_download_bytes:
                        raise RuntimeError(
                            f"Project Gutenberg eBook #{gid} exceeds the "
                            f"{self.max_download_bytes // (1024 * 1024)} MiB per-book text limit."
                        )
                    if content_type in ("text/html", "application/json", "application/xhtml+xml"):
                        raise RuntimeError(f"Project Gutenberg eBook #{gid} returned {content_type}, not plain text.")
                    probe = raw[:512].lstrip().lower()
                    if probe.startswith((b"<!doctype html", b"<html", b"{\"error\"")):
                        raise RuntimeError(f"Project Gutenberg eBook #{gid} returned an error page instead of book text.")
                    time.sleep(1.0)  # polite gap between full-text downloads
                    break
                except urllib.error.HTTPError as exc:
                    last_error = f"HTTP {exc.code}"
                    if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                        raise RuntimeError(f"Project Gutenberg eBook #{gid} returned HTTP {exc.code}.") from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_error = str(getattr(exc, "reason", exc))
                if attempt < 3:
                    time.sleep((2, 5, 12)[attempt])
            if raw is None:
                raise RuntimeError(f"Could not retrieve Project Gutenberg eBook #{gid} after 4 attempts: {last_error}")
            text = None
            for encoding in ("utf-8-sig", "utf-8", "big5", "gb18030"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="replace")
            if language == "zh":
                text = clean_gutenberg_text(text)
            else:
                # Reuse the web project's existing Gutenberg cleaner/story-start heuristic.
                cleaned, _info = textproc.clean_book_text(text)
                text = cleaned if cleaned and len(cleaned.strip()) >= 500 else clean_gutenberg_text(text)
            if language == "zh":
                try:
                    from opencc import OpenCC
                except ImportError as exc:
                    raise RuntimeError("Chinese conversion needs OpenCC. Run: python3 -m pip install opencc-python-reimplemented") from exc
                text = OpenCC("t2s").convert(text)
            if len(text.strip()) < (300 if language == "zh" else 500):
                raise RuntimeError(f"Project Gutenberg eBook #{gid} did not yield enough readable text.")
            tmp = cache.with_suffix(".txt.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, cache)
            self.prune_cache()
            return text

    def _candidate_records(self, language, cat):
        rows, local_by_gid, matched = [], {}, set()
        for bid in cat.book_ids:
            book = cat.books[bid]; gid = book.get("gutenberg_id")
            if str(gid or "").isdigit(): local_by_gid[int(gid)] = bid
            else: rows.append({"book_id": bid, "record": None})
        for record in self.records.get(language, []):
            gid = int(record["id"]); local_id = local_by_gid.get(gid)
            if local_id: matched.add(local_id)
            rows.append({"book_id": local_id, "record": record})
        for bid in set(local_by_gid.values()) - matched:
            rows.append({"book_id": bid, "record": None})
        return rows

    def available_count(self, language, cat):
        return len(self._candidate_records(language, cat))

    def restore_cached_book(self, language, cat, gid):
        """Rehydrate only the requested text from disk."""
        record = self.by_gid.get(language, {}).get(int(gid))
        if not record:
            return None
        cache = self._cache_path(language, int(gid))
        if not cache.is_file():
            return None
        book = self._book_record(record, language)
        text = cache.read_text(encoding="utf-8")
        try:
            os.utime(cache, None)
        except OSError:
            pass
        return cat.add_external_book(book, text)

    def restore_cached_books(self, language, cat):
        """Backward-compatible fallback that restores only the newest cached title."""
        folder = self.cache_dir / language
        if not folder.is_dir():
            return None
        candidates = []
        for cache in folder.glob("pg*.txt"):
            match = re.fullmatch(r"pg(\d+)\.txt", cache.name)
            if not match:
                continue
            try:
                candidates.append((cache.stat().st_mtime, int(match.group(1))))
            except OSError:
                pass
        for _mtime, gid in sorted(candidates, reverse=True):
            restored = self.restore_cached_book(language, cat, gid)
            if restored:
                return restored
        return None

    def ensure_external_book_loaded(self, language, cat, book):
        """Load exactly one remote book into the bounded in-memory passage cache."""
        bid = book["id"]
        if cat.passages_by_book.get(bid):
            cat._touch_loaded_book(bid)
            return True
        gid = book.get("gutenberg_id")
        if not str(gid or "").isdigit():
            return False
        restored = self.restore_cached_book(language, cat, int(gid))
        if restored:
            return True
        cat.add_external_book(book, self._fetch_text(book))
        return bool(cat.passages_by_book.get(bid))

    def pick_passage(self, language, cat, rng=random, seen_book_tokens=None, current_book_id=None):
        seen_book_tokens = set(seen_book_tokens or ())
        rows = self._candidate_records(language, cat)
        if not rows: return None, False
        def token_and_id(row):
            bid = row.get("book_id") or f"pg-{language}-{int(row['record']['id'])}"
            return cat.token("b", bid), bid
        fresh = [r for r in rows if token_and_id(r)[0] not in seen_book_tokens and token_and_id(r)[1] != current_book_id]
        restarted = False
        choices = fresh
        if not choices:
            choices = [r for r in rows if token_and_id(r)[1] != current_book_id]
            restarted = True
        if not choices: return None, False
        selected = rng.choice(choices)
        bid, record = selected.get("book_id"), selected.get("record")
        if record:
            if not bid:
                book = self._book_record(record, language)
                bid = cat.add_external_book(book, self._fetch_text(book))
            else:
                cat.books[bid]["popularity"] = self.popularity_payload(record, language)
                cat.ensure_book_loaded(bid)
        passages = cat.passages_by_book.get(bid, [])
        if not passages: raise RuntimeError(f"No usable passages were generated for {bid}.")
        return rng.choice(passages), restarted
