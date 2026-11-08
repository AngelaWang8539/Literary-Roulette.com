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
    def __init__(self, catalog_path, cache_dir):
        self.catalog_path, self.cache_dir = Path(catalog_path), Path(cache_dir)
        self.snapshot = {}
        self.records = {code: [] for code in LANGUAGE_LABELS}
        self.by_gid = {code: {} for code in LANGUAGE_LABELS}
        self.load_error = None
        self.is_partial_snapshot = False
        self.partial_reason = None
        self._lock = threading.Lock()
        self._rows_cache = {}          # language -> (rows, by_token); built once per metadata snapshot
        self._rows_lock = threading.Lock()
        self.max_cache_books = int(os.environ.get("LR_TEXT_CACHE_BOOKS", "6"))
        self.max_cache_bytes = int(float(os.environ.get("LR_TEXT_CACHE_MB", "32")) * 1024 * 1024)
        self.cache_max_age_seconds = 14 * 24 * 60 * 60
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
        # Also clean up leftovers from older runs when the server starts.
        self._prune_cache()

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
        for language, cat in catalogues.items():
            for book in cat.books.values():
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
        return missing

    def _book_record(self, record, language, require_text_url=True):
        gid = int(record["id"])
        formats = record.get("formats") if isinstance(record.get("formats"), dict) else {}
        text_url = _plain_text_url(formats)
        if require_text_url and not text_url: raise RuntimeError(f"Gutenberg eBook #{gid} has no trusted plain-text URL.")
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
            os.utime(cache, None)
            return cache.read_text(encoding="utf-8")
        with self._lock:
            if cache.is_file():
                os.utime(cache, None)
                return cache.read_text(encoding="utf-8")
            if not book.get("text_url"):
                raise RuntimeError(f"Project Gutenberg eBook #{gid} has no trusted plain-text download URL.")
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
                        raw = response.read()
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
            self._prune_cache()
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

    def _rows(self, language, cat):
        """Selectable books for one language as (book_id, token, record) rows, plus a token index.

        Built once per metadata snapshot. Spinning and revealing then cost a few dictionary
        lookups instead of re-scanning (and re-hashing) the whole Gutenberg catalogue.
        """
        cached = self._rows_cache.get(language)
        if cached is not None:
            return cached
        with self._rows_lock:
            cached = self._rows_cache.get(language)
            if cached is not None:
                return cached
            local_by_gid, rows = {}, []
            for bid, book in cat.books.items():
                if "text_url" in book:      # an on-demand title loaded earlier; rebuilt from its record below
                    continue
                if bid not in cat.book_ids and not cat.passages_by_book.get(bid):
                    continue
                gid = book.get("gutenberg_id")
                if str(gid or "").isdigit():
                    local_by_gid[int(gid)] = bid
                else:
                    rows.append([bid, None])
            matched = set()
            for record in self.records.get(language, []):
                gid = int(record["id"])
                local_id = local_by_gid.get(gid)
                if local_id:
                    matched.add(local_id)
                rows.append([local_id or f"pg-{language}-{gid}", record])
            for bid in set(local_by_gid.values()) - matched:
                rows.append([bid, None])
            tokens = cat.tokens("b", [r[0] for r in rows])
            built = [(r[0], tok, r[1]) for r, tok in zip(rows, tokens)]
            result = (built, {row[1]: row for row in built})
            self._rows_cache[language] = result
            return result

    def prewarm(self, catalogues):
        """Build the selection index at start-up / after a metadata refresh, not on a reader's click."""
        for language, cat in catalogues.items():
            self._rows(language, cat)

    def available_count(self, language, cat):
        return len(self._rows(language, cat)[0])

    def _prune_cache(self):
        """Keep only a few recent on-demand texts on disk (default 6 books / 32 MB)."""
        now = time.time()
        files = [p for p in self.cache_dir.glob("*/*.txt") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        total_bytes = kept = 0
        for path in files:
            try:
                size = path.stat().st_size
                expired = now - path.stat().st_mtime > self.cache_max_age_seconds
                over = kept >= self.max_cache_books or total_bytes + size > self.max_cache_bytes
                if expired or over:
                    path.unlink(missing_ok=True)
                else:
                    kept += 1
                    total_bytes += size
            except OSError:
                continue
        for folder in self.cache_dir.glob("*"):
            if folder.is_dir():
                try: folder.rmdir()
                except OSError: pass

    def book_for_token(self, language, cat, token):
        """Resolve a book from metadata only (one dict lookup); never loads its full text."""
        bid = cat._b_tokens.get(token)
        if bid:
            book = cat.books.get(bid)
            if book:
                return book
        row = self._rows(language, cat)[1].get(token)
        if not row:
            return None
        bid, _token, record = row
        book = cat.books.get(bid)
        if book:
            return book
        return self._book_record(record, language, require_text_url=False) if record else None

    def ensure_book_loaded(self, language, cat, book):
        """Load only the requested title into the catalogue's two-book memory LRU."""
        bid = book["id"]
        if cat.passages_by_book.get(bid):
            cat.ensure_book_loaded(bid)
            return bid
        if bid in cat.text_books:
            return cat.ensure_book_loaded(bid)
        return cat.add_external_book(book, self._fetch_text(book))

    def restore_cached_books(self, language, cat):
        """Deprecated compatibility shim; deliberately does not restore all cached texts."""
        return None

    def pick_passage(self, language, cat, rng=random, seen_book_tokens=None, current_book_id=None):
        seen_book_tokens = seen_book_tokens or ()
        rows = self._rows(language, cat)[0]
        if not rows: return None, False

        def usable(row):
            return row[1] not in seen_book_tokens and row[0] != current_book_id

        chosen = None
        for _ in range(24):                 # a handful of random draws is enough for a big catalogue
            row = rng.choice(rows)
            if usable(row):
                chosen = row
                break
        restarted = False
        if chosen is None:                  # tiny catalogue, or nearly everything already seen
            fresh = [r for r in rows if usable(r)]
            if not fresh:
                fresh = [r for r in rows if r[0] != current_book_id]
                restarted = True
            if not fresh: return None, False
            chosen = rng.choice(fresh)

        bid, _token, record = chosen
        if record:
            book = cat.books.get(bid)
            if book is None:
                book = self._book_record(record, language)
                bid = cat.add_external_book(book, self._fetch_text(book))
            else:
                book["popularity"] = self.popularity_payload(record, language)
        if bid in cat.books:
            self.ensure_book_loaded(language, cat, cat.books[bid])
        if not cat.passages_by_book.get(bid):
            raise RuntimeError(f"No usable passages were generated for {bid}.")
        return cat.pick_in_book(bid, set(), rng), restarted
