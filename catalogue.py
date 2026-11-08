"""Language-aware catalogue and passage selection for Literary Roulette."""
import hashlib
import hmac
import json
import random
import re
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import textproc

LINK_KINDS = ("read", "borrow", "buy")
LINK_LABELS = {"read": "Read online", "borrow": "Borrow", "buy": "Buy e-book"}
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+$")


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


def _safe_text_name(book):
    name = book.get("text_file") or f"{book['id']}.txt"
    if not isinstance(name, str) or not SAFE_FILENAME.fullmatch(name):
        raise ValueError(f"Unsafe text filename for {book.get('id')!r}")
    return name


def _split_chinese(text, target=780, maximum=1150):
    """Group Chinese paragraphs/sentences into passage-sized blocks, preserving punctuation."""
    blocks = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    units = []
    for block in blocks:
        if len(block) <= maximum:
            units.append(block)
        else:
            units.extend(x.strip() for x in re.split(r"(?<=[。！？；])", block) if x.strip())
    passages, current = [], ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if current and len(candidate) > maximum and len(current) >= 300:
            passages.append(current)
            current = unit
        else:
            current = candidate
        if len(current) >= target:
            passages.append(current)
            current = ""
    if current.strip():
        if passages and len(current) < 260 and len(passages[-1]) + len(current) <= maximum:
            passages[-1] += "\n\n" + current
        else:
            passages.append(current)
    return [p for p in passages if len(p.strip()) >= 120]


class Catalogue:
    def __init__(self, books, passages, secret, allow_demo=False, covers_dir=None, texts_dir=None,
                 manifest=None, language="en"):
        self.language = language
        self.covers_dir = Path(covers_dir) if covers_dir else None
        self.texts_dir = Path(texts_dir) if texts_dir else None
        self.manifest = manifest or {}
        self.unavailable, self.problems = {}, []
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.books, self.passages, self.passages_by_book = {}, {}, {}
        self.text_books = set()
        self._active_books = OrderedDict()
        self.max_active_books = 2

        for b in books:
            if isinstance(b, dict) and isinstance(b.get("id"), str) and (b.get("gutenberg_id") or b.get("text_file")):
                reason = self._text_problem(b)
                if reason:
                    self.unavailable[b["id"]] = reason
                else:
                    self.text_books.add(b["id"])

        self.allow_demo = (not self.text_books) if allow_demo is None else allow_demo
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
            if bid in self.text_books or self._rights_ok(b.get("rights_status"), b):
                self.books[bid] = b

        for p in passages:
            pid = p.get("id") if isinstance(p, dict) else None
            if not isinstance(pid, str) or not pid or pid in self.passages:
                self.problems.append(f"Missing or duplicate passage id {pid!r}; skipped.")
                continue
            if not isinstance(p.get("text"), str) or not p["text"].strip():
                continue
            bid = p.get("book_id")
            book = self.books.get(bid)
            if book and p.get("approved_for_public") is True and self._rights_ok(p.get("rights_status"), book):
                self.passages[pid] = p
                self.passages_by_book.setdefault(bid, []).append(p)

        # Verified full texts are indexed as available, but read and split only
        # when a reader actually selects that title.
        self.book_ids = [bid for bid in self.books
                         if self.passages_by_book.get(bid) or bid in self.text_books]
        self._p_tokens = {self.token("p", pid): pid for pid in self.passages}
        self._b_tokens = {self.token("b", bid): bid for bid in self.book_ids}

    def _touch_active(self, bid):
        self._active_books.pop(bid, None)
        self._active_books[bid] = None
        while len(self._active_books) > self.max_active_books:
            old_bid, _ = self._active_books.popitem(last=False)
            self._evict_passages(old_bid)

    def _evict_passages(self, bid):
        # Keep small approved excerpts resident; evict generated full-book passages.
        existing = self.passages_by_book.get(bid, [])
        generated = [p for p in existing if p.get("generated")]
        if not generated:
            return
        for passage in generated:
            self.passages.pop(passage["id"], None)
            self._p_tokens.pop(self.token("p", passage["id"]), None)
        retained = [p for p in existing if not p.get("generated")]
        if retained:
            self.passages_by_book[bid] = retained
        else:
            self.passages_by_book.pop(bid, None)

    def ensure_book_loaded(self, bid):
        """Load one verified local title and retain no more than two full books."""
        if bid not in self.books:
            raise KeyError(f"Unknown book: {bid}")
        existing = self.passages_by_book.get(bid, [])
        has_generated = any(p.get("generated") for p in existing)
        if has_generated:
            if bid in self.text_books or self.books[bid].get("text_url"):
                self._touch_active(bid)
            return bid
        if bid not in self.text_books:
            raise ValueError(f"No locally verified text is available for {bid}.")
        text = self._text_path(self.books[bid]).read_text(encoding="utf-8")
        split = _split_chinese(text) if self.language == "zh" else textproc.split_passages(text)
        if not split:
            raise ValueError(f"Verified text for {bid} did not yield readable passages.")
        book = self.books[bid]
        source_meta = book.get("source") if isinstance(book.get("source"), dict) else {}
        public_edition = book.get("source_edition") or book.get("source_name") or source_meta.get("name")
        generated = []
        for i, body in enumerate(split):
            pid = f"{bid}:{getattr(textproc, 'RULES_VERSION', 'multi-1')}:{i:05d}"
            passage = {"id": pid, "book_id": bid, "text": body, "generated": True,
                       "source": {"edition": public_edition, "location": None}}
            generated.append(passage)
            self.passages[pid] = passage
            self._p_tokens[self.token("p", pid)] = pid
        retained = [p for p in self.passages_by_book.get(bid, []) if not p.get("generated")]
        self.passages_by_book[bid] = retained + generated
        self._touch_active(bid)
        return bid

    def add_external_book(self, book, text):
        """Register one downloaded Gutenberg title and generate its passages."""
        bid = book["id"]
        if bid in self.books and self.passages_by_book.get(bid):
            self._touch_active(bid)
            return bid
        if not isinstance(text, str) or not text.strip():
            raise ValueError("The selected title did not contain readable text.")
        split = _split_chinese(text) if self.language == "zh" else textproc.split_passages(text)
        if not split:
            raise ValueError("The selected title did not yield passages long enough to spin.")
        self.books[bid] = book
        generated = []
        for i, body in enumerate(split):
            pid = f"{bid}:gutendex:{i:05d}"
            passage = {"id": pid, "book_id": bid, "text": body, "generated": True,
                       "source": {"edition": f"Project Gutenberg eBook #{book.get('gutenberg_id')}",
                                  "location": f"Passage {i + 1}"}}
            generated.append(passage)
            self.passages[pid] = passage
            self._p_tokens[self.token("p", pid)] = pid
        self.passages_by_book[bid] = generated
        if bid not in self.book_ids:
            self.book_ids.append(bid)
        self._b_tokens[self.token("b", bid)] = bid
        self._touch_active(bid)
        return bid

    def _text_path(self, book):
        return self.texts_dir / _safe_text_name(book)

    def _text_problem(self, book):
        if not self.texts_dir:
            return "no text folder configured"
        entry = self.manifest.get(book["id"])
        if not entry:
            return "no manifest record for this book"
        if entry.get("verified") is not True:
            return "; ".join(entry.get("problems", ["text not verified"]))
        path = self._text_path(book)
        if not path.is_file():
            return f"cleaned text file {path.name} is missing"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry.get("text_sha256"):
            return "cleaned text changed after verification; rerun the language importer"
        return None

    def _rights_ok(self, status, book):
        if status in ("approved", "public_domain_candidate", "source_licensed"):
            return True
        return self.allow_demo and book.get("is_demo") is True and status == "demo_placeholder"

    def token(self, kind, ident):
        digest = hmac.new(self._secret, f"{kind}:{ident}".encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()[:12]

    def passage_by_token(self, token, book_id=None):
        if book_id:
            self.ensure_book_loaded(book_id)
        pid = self._p_tokens.get(token) if isinstance(token, str) else None
        return self.passages.get(pid)

    def book_of(self, passage):
        return self.books[passage["book_id"]]

    def book_token(self, book):
        return self.token("b", book["id"])

    def passage_token(self, passage):
        return self.token("p", passage["id"])

    def _pick_unseen(self, options, seen, rng):
        unseen = [p for p in options if self.passage_token(p) not in seen]
        return rng.choice(unseen or options)

    def pick_first(self, rng=random):
        bid = rng.choice(self.book_ids)
        self.ensure_book_loaded(bid)
        return rng.choice(self.passages_by_book[bid])

    def pick_other_passage(self, current, seen, rng=random):
        self.ensure_book_loaded(current["book_id"])
        options = [p for p in self.passages_by_book[current["book_id"]] if p["id"] != current["id"]]
        return self._pick_unseen(options, seen, rng) if options else None

    def pick_other_book(self, current_book_id, seen_book_tokens, rng=random):
        others = [b for b in self.book_ids if b != current_book_id]
        if not others:
            return None, False
        fresh = [b for b in others if self.token("b", b) not in seen_book_tokens]
        return rng.choice(fresh or others), not fresh

    def pick_in_book(self, book_id, seen, rng=random):
        self.ensure_book_loaded(book_id)
        return self._pick_unseen(self.passages_by_book[book_id], seen, rng)

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

    def book_payload(self, book, passage=None):
        pub = book.get("publication") if isinstance(book.get("publication"), dict) else {}
        bits = []
        if pub.get("year"):
            bits.append(f"First published {pub['year']}.")
        if pub.get("publisher"):
            bits.append(f"Publisher: {pub['publisher']}.")
        src = passage.get("source") if isinstance(passage, dict) and isinstance(passage.get("source"), dict) else {}
        btoken = self.book_token(book)
        return {
            "id": book["id"], "language": book.get("language", self.language),
            "title": book["title"], "author": book["author"],
            "genres": [g for g in book.get("genres", []) if isinstance(g, str)],
            "description": book.get("description", ""),
            "publication": " ".join(bits) or None,
            "is_demo": book.get("is_demo") is True,
            "content_note": book.get("content_note") or None,
            "source_credit": self._credit(book), "cover_url": self.cover_url(book),
            "source": {"edition": src.get("edition"), "location": src.get("location")},
            "popularity": book.get("popularity"),
            "links": [{"kind": k, "label": LINK_LABELS[k], "href": f"/go/{k}/{btoken}"} for k in self.links_for(book)],
        }
