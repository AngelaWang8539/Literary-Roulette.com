"""One catalogue per language: the authoritative record of what a book is and whether it may be served.

Metadata only. The catalogue never reads full texts to answer a visitor: prepared passages live in
the shared ready shelf (shelf.py). A book is eligible to be served only when BOTH hold:
  1. technical verification passed (its text exists, matches the manifest hash, and was prepared), and
  2. its rights status is cleared (see RIGHTS_CLEARED). "Downloaded" never means "cleared".
"""
import hashlib
import hmac
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

LINK_KINDS = ("read", "borrow", "buy")
LINK_LABELS = {"read": "Read online", "borrow": "Borrow", "buy": "Buy e-book"}
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+$")
RIGHTS_CLEARED = ("approved", "public_domain_candidate", "source_licensed")
RIGHTS_PENDING = "pending_verification"


def valid_https_url(value):
    if not isinstance(value, str) or not value or value != value.strip() or any(c.isspace() for c in value):
        return False
    try:
        p = urlparse(value)
        return p.scheme == "https" and "." in (p.hostname or "")
    except ValueError:
        return False


def load_json_list(path, problems):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{Path(path).name} was not found.")
        return []
    except (json.JSONDecodeError, OSError) as exc:
        problems.append(f"{Path(path).name} could not be read: {exc}")
        return []
    if not isinstance(data, list):
        problems.append(f"{Path(path).name} must contain a JSON list.")
        return []
    return data


def load_manifest(folder):
    try:
        value = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _safe_text_name(book):
    name = book.get("text_file") or f"{book['id']}.txt"
    if not isinstance(name, str) or not SAFE_FILENAME.fullmatch(name):
        raise ValueError(f"Unsafe text filename for {book.get('id')!r}")
    return name


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Catalogue:
    def __init__(self, books, secret, language="en", *, texts_dir=None, manifest=None, covers_dir=None,
                 allow_pending_rights=False):
        self.language = language
        self.texts_dir = Path(texts_dir) if texts_dir else None
        self.covers_dir = Path(covers_dir) if covers_dir else None
        self.manifest = manifest or {}
        self.allow_pending_rights = bool(allow_pending_rights)
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._lock = threading.RLock()
        self.books = {}          # id -> metadata, rights-cleared only
        self.local_texts = {}    # id -> {"path": Path, "sha256": str}: technically verified local texts
        self.unavailable = {}    # id -> reason it cannot be prepared / served
        self.problems = []
        self._b_tokens, self._id_tokens = {}, {}

        known = set()
        for b in books:
            bid = b.get("id") if isinstance(b, dict) else None
            if not isinstance(bid, str) or not bid:
                self.problems.append("A book record has no string id; skipped.")
                continue
            if bid in known:
                self.problems.append(f"Duplicate book id {bid!r}; skipped.")
                continue
            known.add(bid)
            if not b.get("title") or not b.get("author"):
                self.problems.append(f"Book {bid!r} needs title and author; skipped.")
                continue
            if not self.rights_ok(b):
                self.unavailable[bid] = f"rights status {b.get('rights_status')!r} is not cleared for public use"
                continue
            self.books[bid] = b
            self._index(bid)
            if b.get("gutenberg_id") or b.get("text_file"):
                reason = self._text_problem(b)
                if reason:
                    self.unavailable[bid] = reason
                else:
                    self.local_texts[bid] = {"path": self._text_path(b), "sha256": self.manifest[bid]["text_sha256"]}

    # ---- rights gate -------------------------------------------------------------------------
    def rights_ok(self, book):
        status = book.get("rights_status")
        return status in RIGHTS_CLEARED or (status == RIGHTS_PENDING and self.allow_pending_rights)

    # ---- tokens ------------------------------------------------------------------------------
    def token(self, kind, ident):
        digest = hmac.new(self._secret, f"{kind}:{ident}".encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()[:12]

    def _index(self, bid):
        token = self.token("b", bid)
        self._b_tokens[token] = bid
        self._id_tokens[bid] = token

    def book_token(self, bid):
        return self._id_tokens.get(bid) or self.token("b", bid)

    def passage_key(self, bid, index):
        return self.token("p", f"{bid}:{index}")

    def book_by_token(self, token):
        bid = self._b_tokens.get(token) if isinstance(token, str) else None
        return self.books.get(bid) if bid else None

    # ---- books -------------------------------------------------------------------------------
    def all_books(self):
        with self._lock:
            return list(self.books.values())

    def register_external(self, book):
        """Add a prepared on-demand title (already rights-checked upstream). False if refused."""
        bid = book.get("id")
        if not isinstance(bid, str) or not bid or not book.get("title") or not self.rights_ok(book):
            return False
        with self._lock:
            if bid not in self.books:
                self.books[bid] = book
                self._index(bid)
        return True

    def unregister_external(self, bid):
        """Forget a retired on-demand title so nothing piles up."""
        with self._lock:
            if bid in self.books and bid not in self.local_texts and self.books[bid].get("text_url"):
                self.books.pop(bid, None)
                token = self._id_tokens.pop(bid, None)
                self._b_tokens.pop(token, None)

    def _text_path(self, book):
        return self.texts_dir / _safe_text_name(book)

    def _text_problem(self, book):
        if not self.texts_dir:
            return "no text folder configured"
        entry = self.manifest.get(book["id"])
        if not entry:
            return "no manifest record for this book; run tools/ingest_gutenberg.py to verify its local text"
        if entry.get("verified") is not True:
            return "; ".join(entry.get("problems", ["text not verified"]))
        path = self._text_path(book)
        if not path.is_file():
            return f"cleaned text file {path.name} is missing"
        if file_sha256(path) != entry.get("text_sha256"):
            return "cleaned text changed since verification; rerun the language importer"
        return None

    def read_local_text(self, bid):
        info = self.local_texts[bid]
        return info["path"].read_text(encoding="utf-8")

    # ---- reader-facing payloads --------------------------------------------------------------
    def links_for(self, book):
        links = book.get("links") if isinstance(book.get("links"), dict) else {}
        found = {k: links[k] for k in LINK_KINDS if valid_https_url(links.get(k))}
        source_meta = book.get("source") if isinstance(book.get("source"), dict) else {}
        source_url = book.get("source_url") or source_meta.get("url")
        if "read" not in found and valid_https_url(source_url):
            found["read"] = source_url
        if "read" not in found and book.get("gutenberg_id"):
            found["read"] = f"https://www.gutenberg.org/ebooks/{book['gutenberg_id']}"
        return {k: found[k] for k in LINK_KINDS if k in found}

    def cover_url(self, book):
        cover = book.get("cover")
        if not (isinstance(cover, dict) and cover.get("file") and cover.get("usage_rights")):
            return None
        name = cover["file"]
        if not SAFE_FILENAME.fullmatch(name) or not self.covers_dir or not (self.covers_dir / name).is_file():
            return None
        return f"/static/covers/{name}"

    def _credit(self, book):
        provider = book.get("source_provider", "Project Gutenberg" if book.get("gutenberg_id") else "Source archive")
        url = book.get("source_url")
        if not url and book.get("gutenberg_id"):
            url = f"https://www.gutenberg.org/ebooks/{book['gutenberg_id']}"
        license_note = book.get("source_license_note") or "Verify the source edition and local copyright rules before redistribution."
        return {"label": book.get("source_credit") or f"Text source: {provider}", "url": url, "note": license_note}

    def book_payload(self, book, passage_index=None):
        pub = book.get("publication") if isinstance(book.get("publication"), dict) else {}
        bits = []
        if pub.get("year"):
            bits.append(f"First published {pub['year']}.")
        if pub.get("publisher"):
            bits.append(f"Publisher: {pub['publisher']}.")
        btoken = self.book_token(book["id"])
        location = f"Passage {passage_index + 1}" if isinstance(passage_index, int) else None
        return {
            "id": book["id"], "language": book.get("language", self.language),
            "title": book["title"], "author": book["author"],
            "genres": [g for g in book.get("genres", []) if isinstance(g, str)],
            "description": book.get("description", ""),
            "publication": " ".join(bits) or None,
            "is_demo": book.get("is_demo") is True,
            "content_note": book.get("content_note") or None,
            "source_credit": self._credit(book), "cover_url": self.cover_url(book),
            "source": {"edition": f"Project Gutenberg eBook #{book['gutenberg_id']}" if book.get("gutenberg_id") else None,
                       "location": location},
            "popularity": book.get("popularity"),
            "links": [{"kind": k, "label": LINK_LABELS[k], "href": f"/go/{k}/{btoken}"} for k in self.links_for(book)],
        }
