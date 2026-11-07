"""Import French Project Gutenberg editions into data/fr/texts/ (no English importer)."""
import hashlib, json, re, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "fr"
START = re.compile(r"\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}", re.I | re.S)
END = re.compile(r"\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}", re.I | re.S)


def fetch(gid):
    urls = [f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
            f"https://gutenberg.pglaf.org/cache/epub/{gid}/pg{gid}.txt",
            f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8"]
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LiteraryRoulette/0.3 French importer"})
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read().decode("utf-8-sig"), url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors))


def clean(raw, book):
    language = re.search(r"(?im)^Language:\s*(.+)$", raw[:12000])
    if not language or not any(x in language.group(1).lower() for x in ("french", "français")):
        raise ValueError("Gutenberg header does not identify this edition as French")
    start, end = START.search(raw), END.search(raw)
    if not start or not end or end.start() <= start.end():
        raise ValueError("Gutenberg text boundaries not found")
    body = raw[start.end():end.start()].replace("\r", "\n").replace("\f", "\n")
    hint = book.get("start_hint", "")
    pos = body.casefold().find(hint.casefold()) if hint else -1
    if pos < 0:
        raise ValueError(f"start_hint not found: {hint!r}; inspect this edition and edit data/fr/books.json")
    body = re.sub(r"\n{3,}", "\n\n", body[pos:])
    body = re.sub(r"[ \t]+\n", "\n", body).strip()
    words = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’\-][A-Za-zÀ-ÖØ-öø-ÿ]+)*", body))
    paragraphs = [p for p in re.split(r"\n\s*\n+", body) if len(p.strip()) > 20]
    if words < book.get("min_words", 5000):
        raise ValueError(f"only {words:,} French words after cleaning; minimum {book['min_words']:,}")
    if len(paragraphs) < 8:
        raise ValueError("too few narrative paragraphs after cleaning")
    return body, words, len(paragraphs)


def main():
    books = json.loads((DATA / "books.json").read_text(encoding="utf-8"))
    raw_dir, out = DATA / "source_texts", DATA / "texts"
    raw_dir.mkdir(parents=True, exist_ok=True); out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    ok = bad = 0
    for book in books:
        bid, gid = book["id"], book["gutenberg_id"]
        raw_path, target = raw_dir / f"pg{gid}.txt", out / book["text_file"]
        entry = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source_provider": "Project Gutenberg", "gutenberg_id": gid,
                 "source_url": book["source_url"], "verified": False, "problems": []}
        try:
            if raw_path.exists():
                raw, source = raw_path.read_text(encoding="utf-8-sig"), f"local cache {raw_path.name}"
            else:
                raw, source = fetch(gid); raw_path.write_text(raw, encoding="utf-8"); time.sleep(1.5)
            text, words, paragraphs = clean(raw, book)
            target.write_text(text, encoding="utf-8")
            entry.update(verified=True, source_file=raw_path.name, downloaded_from=source,
                         words=words, paragraphs=paragraphs,
                         text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         first_chars=text[:350], last_chars=text[-250:], rules="fr-pg-clean-1")
            ok += 1; print(f"[{bid}] VERIFIED · {words:,} mots · {paragraphs} paragraphes")
        except Exception as exc:
            entry["problems"] = [str(exc)]; bad += 1; print(f"[{bid}] NOT VERIFIED · {exc}")
        manifest[bid] = entry
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nFinished: {ok} verified, {bad} failed. Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
