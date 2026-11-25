"""Independent-author TXT intake: private text store + the checks a file must pass before it can ever be drawn.

A file that passes is stored (hash-named, outside static/, never served directly) and the book is recorded as PENDING with its
claimed permission. Passing the checks does not license anything: an operator reviews the evidence and approves the rights
(Registry.set_rights) and only then activates the book (Registry.set_pool_status). Missing permission can never enter the pool.
"""
import hashlib
import os
import re
from pathlib import Path

import segment
from registry import RegistryError

MAX_BYTES = 20 * 1024 * 1024
MIN_CHARS = {"zh": 2000}
DEFAULT_MIN_CHARS = 6000
MIN_PASSAGES = 5
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


class TextStore:
    """Private storage for author texts: <dir>/<book_id>.<hash12>.txt. The registry stores only the key. Swapping this class is
    all it takes to move texts to object storage later."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, book_id, text):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = f"{book_id}.{digest[:12]}.txt"
        tmp = self.dir / (key + f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.dir / key)
        return key, digest

    def read(self, key, expect_hash=None):
        if not key or "/" in key or "\\" in key or key.startswith("."):
            raise ValueError("bad text key")
        text = (self.dir / key).read_text(encoding="utf-8")
        if expect_hash and hashlib.sha256(text.encode("utf-8")).hexdigest() != expect_hash:
            raise ValueError("the stored text no longer matches its recorded hash")
        return text

    def drop_other_revisions(self, book_id, keep_key):
        for path in self.dir.glob(f"{book_id}.*.txt"):
            if path.name != keep_key:
                path.unlink(missing_ok=True)


def normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"\n{4,}", "\n\n\n", text).strip() + "\n"


def check_txt(raw, language):
    """(text or None, problems, passage_count). Never raises on bad input: every problem becomes a sentence."""
    problems = []
    if len(raw) > MAX_BYTES:
        return None, [f"the file is larger than {MAX_BYTES // 1048576} MB"], 0
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ["the file is not valid UTF-8 (re-save it as UTF-8 text)"], 0
    text = normalize_text(text)
    if text.count("\ufffd") > 5:
        problems.append("the text contains replacement characters (a sign of a wrong encoding)")
    if len(text) < MIN_CHARS.get(language, DEFAULT_MIN_CHARS):
        problems.append("the text is too short to offer readers passages")
    passages = len(segment.split(text, language)) if not problems else 0
    if not problems and passages < MIN_PASSAGES:
        problems.append(f"only {passages} readable passage(s) were found (at least {MIN_PASSAGES} are needed)")
    return (None if problems else text), problems, passages


def intake(registry, store, path, *, slug, language, title, author, permission=None, description="", links=None, publication=None):
    """Validate and record one author TXT. Returns {'book_id', 'ok', 'problems', 'passages', 'revision'}.

    `permission` holds what the author granted (all optional here; approval later requires them): granted_by, scope, territory,
    commercial (bool), attribution, expires ('YYYY-MM-DD' or 'perpetual'), evidence. Re-running with the same slug is a revision."""
    permission = permission or {}
    if not SLUG.match(slug or ""):
        raise RegistryError("The slug must be 3-61 characters: lowercase letters, digits and hyphens.")
    if language not in ("en", "fr", "zh"):
        raise RegistryError("Language must be en, fr or zh.")
    if not (title or "").strip() or not (author or "").strip():
        raise RegistryError("Title and author are required.")
    book_id = f"au-{language}-{slug}"
    text, problems, passages = check_txt(Path(path).read_bytes(), language)
    existing = registry.get(book_id)
    if text is not None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        twin = registry.find_hash(digest)
        if twin and twin != book_id:
            problems.append(f"the same text is already in the Registry as {twin}")
    if problems:
        return {"book_id": book_id, "ok": False, "problems": problems, "passages": passages, "revision": existing["revision"] if existing else 0}
    key, digest = store.put(book_id, text)
    revision = (existing["revision"] + 1) if existing and existing.get("text_hash") != digest else (existing["revision"] if existing else 1)
    note = permission.get("evidence") or permission.get("scope") or None
    rights = {"territory": permission.get("territory"), "expires": permission.get("expires"), "note": note,
              "commercial": None if permission.get("commercial") is None else int(bool(permission["commercial"]))}
    if existing and existing["provider"] != "author":
        raise RegistryError(f"{book_id} already exists as a {existing['provider']} book.")
    registry.add_author_book(book_id=book_id, slug=slug, language=language, title=title.strip(), author=author.strip(), text_key=key,
                             text_hash=digest, attribution=permission.get("attribution"), links=links, description=description,
                             publication=publication, rights=rights, revision=revision,
                             status_note="awaiting rights review" if not existing else None)
    store.drop_other_revisions(book_id, key)
    return {"book_id": book_id, "ok": True, "problems": [], "passages": passages, "revision": revision}
