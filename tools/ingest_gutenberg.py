"""Build the website-ready text library from already-cleaned local bNNN.txt files.

This importer is for the situation where the input files in data/book_texts have
ALREADY had the Project Gutenberg header/footer, book title/author page and TOC
removed. It does not download anything and does not call textproc.clean_book_text,
which expects original Gutenberg-format input.

Run from the Literary Roulette project root:
    python3 tools/ingest_gutenberg.py

Input:
    data/books.json
    data/book_texts/book_file_map.json
    data/book_texts/b001.txt, b002.txt, ...

Output:
    data/texts/<website-book-id>.txt
    data/texts/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path, expected_type: type) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Required file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, expected_type):
        raise SystemExit(f"Expected {path} to contain {expected_type.__name__} data.")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w]+(?:['’][\w]+)?\b", text, flags=re.UNICODE))


def _find_pattern(pattern: str, text: str, label: str):
    try:
        return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"Invalid {label} regular expression {pattern!r}: {exc}") from exc


def prepare_already_cleaned_text(raw: str, book: dict[str, Any]) -> tuple[str, list[str]]:
    """Trim to the configured narrative start; do not expect Gutenberg wrapper tags."""
    problems: list[str] = []
    text = raw.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()

    start_pattern = str(book.get("text_start_pattern") or "").strip()
    if start_pattern:
        match = _find_pattern(start_pattern, text, "text_start_pattern")
        if not match:
            problems.append(
                "text_start_pattern was not found in this already-cleaned file. "
                "Check that the file is the correct edition and that its opening narrative is present."
            )
            return "", problems
        # Discard any remaining opening story heading / blank material before the narrative.
        text = text[match.start():]
    else:
        # No marker means we cannot safely identify the narrative boundary.
        problems.append(
            "text_start_pattern is blank. Set it to a distinctive phrase at the beginning "
            "of the actual narrative, or confirm the source text begins exactly at the narrative."
        )
        return "", problems

    end_pattern = str(book.get("text_end_pattern") or "").strip()
    if end_pattern:
        end_match = _find_pattern(end_pattern, text, "text_end_pattern")
        if end_match:
            text = text[:end_match.start()]

    # Remove a Gutenberg end wrapper if one survived the user's earlier cleanup.
    footer = re.search(
        r"\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if footer:
        text = text[:footer.start()]

    text = text.strip()
    if not text:
        problems.append("No narrative text remained after applying the story markers.")
    return text, problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import already-cleaned local bNNN.txt files; no network downloads."
    )
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--source-dir", default=None)
    parser.add_argument("--map-file", default=None)
    parser.add_argument("--only", nargs="*", help="website book IDs to process")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    source_dir = Path(args.source_dir).expanduser().resolve() if args.source_dir else data_dir / "book_texts"
    map_file = Path(args.map_file).expanduser().resolve() if args.map_file else source_dir / "book_file_map.json"
    books = read_json(data_dir / "books.json", list)
    mapping = read_json(map_file, dict)
    mapped_books = mapping.get("books", {})
    not_mapped = mapping.get("not_mapped", {})
    if not isinstance(mapped_books, dict):
        raise SystemExit("book_file_map.json must contain a 'books' object.")

    output_dir = data_dir / "texts"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest = read_json(manifest_path, dict) if manifest_path.exists() else {}

    only = set(args.only or [])
    if only:
        known = {str(book.get("id")) for book in books if isinstance(book, dict)}
        unknown = only - known
        if unknown:
            raise SystemExit("Unknown website book ID(s): " + ", ".join(sorted(unknown)))

    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import textproc
    except ImportError as exc:
        raise SystemExit(f"Could not import project textproc.py: {exc}") from exc

    processed = verified = failed = skipped = 0
    print(f"Input:  {source_dir}")
    print(f"Output: {output_dir}")
    print("Network downloads: DISABLED; reading existing local text files only.\n")

    for book in books:
        if not isinstance(book, dict) or not book.get("id") or not book.get("gutenberg_id"):
            continue
        book_id = str(book["id"])
        if only and book_id not in only:
            continue
        processed += 1
        gid = int(book["gutenberg_id"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry: dict[str, Any] = {
            "checked_at": now,
            "gutenberg_id": gid,
            "source_page": f"https://www.gutenberg.org/ebooks/{gid}",
            "verified": False,
            "problems": [],
        }

        map_entry = mapped_books.get(book_id)
        if not isinstance(map_entry, dict):
            detail = not_mapped.get(book_id, {}) if isinstance(not_mapped, dict) else {}
            reason = detail.get("reason") if isinstance(detail, dict) else None
            reason = reason or "No compatible local source file is mapped."
            entry["problems"] = [reason]
            manifest[book_id] = entry
            skipped += 1
            print(f"[{book_id}] SKIPPED: {reason}")
            continue

        mapped_gid = map_entry.get("gutenberg_id")
        filename = str(map_entry.get("source_file") or "")
        if mapped_gid != gid:
            entry["problems"] = [
                f"Mapping mismatch: books.json has Gutenberg #{gid}, mapping has #{mapped_gid}."
            ]
        elif not filename or Path(filename).name != filename:
            entry["problems"] = [f"Unsafe or empty source filename: {filename!r}"]
        else:
            source_path = source_dir / filename
            entry["source_file"] = filename
            entry["source_catalogue_id"] = map_entry.get("source_catalogue_id")
            if not source_path.is_file():
                entry["problems"] = [f"Local source file is missing: {filename}"]
            else:
                print(f"[{book_id}] Using {filename} (Gutenberg #{gid}) ... ", end="", flush=True)
                try:
                    raw = source_path.read_text(encoding="utf-8-sig", errors="replace")
                    entry["raw_sha256"] = sha256_bytes(raw.encode("utf-8"))
                    cleaned, problems = prepare_already_cleaned_text(raw, book)
                    count = word_count(cleaned)
                    minimum = int(book.get("min_words", 8000) or 8000)
                    if count < minimum:
                        problems.append(
                            f"Only {count:,} narrative words remain; this catalogue entry requires at least {minimum:,}."
                        )
                    paragraphs = textproc.split_passages(cleaned) if cleaned else []
                    if not paragraphs:
                        problems.append("No passages could be generated from the cleaned narrative.")

                    entry["words"] = count
                    entry["warnings"] = []
                    if not problems:
                        output_path = output_dir / f"{book_id}.txt"
                        output_path.write_text(cleaned.rstrip() + "\n", encoding="utf-8")
                        output_bytes = output_path.read_bytes()
                        output_text = output_bytes.decode("utf-8")
                        passage_count = len(textproc.split_passages(output_text))
                        entry.update(
                            verified=True,
                            passages=passage_count,
                            rules_version=getattr(textproc, "RULES_VERSION", "unknown"),
                            text_sha256=sha256_bytes(output_bytes),
                            first_chars=output_text[:240],
                            last_chars=output_text[-240:],
                            rights_note=(
                                "Technical text checks only; not a legal rights determination or permission "
                                "for commercial redistribution."
                            ),
                        )
                        verified += 1
                        print(f"VERIFIED ({count:,} words; {passage_count:,} passages)")
                    else:
                        entry["problems"] = problems
                        failed += 1
                        print("NOT VERIFIED: " + "; ".join(problems))
                except (OSError, UnicodeError, ValueError, re.error) as exc:
                    entry["problems"] = [f"Could not process local source file: {exc}"]
                    failed += 1
                    print(f"FAILED: {exc}")

        if not entry.get("verified") and not entry.get("problems"):
            entry["problems"] = ["No text was verified."]
            failed += 1
        manifest[book_id] = entry
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # Include an explicit entry for any website catalogue book which lacks a map.
    for book_id, detail in (not_mapped.items() if isinstance(not_mapped, dict) else []):
        if only and book_id not in only:
            continue
        if book_id not in manifest:
            reason = detail.get("reason", "No local source file mapped.") if isinstance(detail, dict) else "No local source file mapped."
            manifest[book_id] = {
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "verified": False,
                "problems": [reason],
            }

    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print("\nFinished.")
    print(f"Processed: {processed} | Verified: {verified} | Failed: {failed} | Skipped/unmapped: {skipped}")
    print(f"Manifest: {manifest_path}")
    print("Check each verified entry's first_chars and inspect the matching text file before public use.")
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
