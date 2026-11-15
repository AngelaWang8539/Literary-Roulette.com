#!/usr/bin/env python3
"""Download Chinese Project Gutenberg plain text, convert Traditional -> Simplified,
clean Gutenberg wrappers, and write roulette-ready UTF-8 files.

Run from the Literary Roulette project root:
    python3 tools/ingest_chinese.py
    python3 tools/ingest_chinese.py --only zh-b002 zh-b007
    python3 tools/ingest_chinese.py --refresh
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from opencc import OpenCC

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "zh"
RAW_DIR = DATA / "source_gutenberg"
OUT_DIR = DATA / "texts"
MANIFEST_PATH = OUT_DIR / "manifest.json"
UA = "LiteraryRoulette/0.3 (local public-domain text importer)"
CC = OpenCC("t2s")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
START_RE = re.compile(r"\*{3,}\s*START OF THE PROJECT GUTENBERG EBOOK.*?\*{3,}", re.I)
END_RE = re.compile(r"\*{3,}\s*END OF THE PROJECT GUTENBERG EBOOK.*?\*{3,}", re.I)


def download(book_id: int) -> bytes:
    # Gutenberg's canonical plain-text URL first; fallbacks cover older file layouts.
    urls = [
        f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt",
        f"https://www.gutenberg.org/files/{book_id}/{book_id}-0.txt",
        f"https://www.gutenberg.org/files/{book_id}/{book_id}.txt",
        f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt.utf8",
    ]
    errors = []
    for url in urls:
        for attempt in range(3):
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = response.read()
                    if response.status == 200 and data:
                        print(f"    downloaded: {url}")
                        return data
                    raise RuntimeError(f"HTTP {response.status}, empty response")
            except urllib.error.HTTPError as exc:
                errors.append(f"{url}: HTTP {exc.code}")
                if exc.code in (404, 410):
                    break
                if exc.code == 429:
                    time.sleep(8 * (attempt + 1))
                else:
                    time.sleep(2 * (attempt + 1))
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("No plain-text download succeeded. " + " | ".join(errors[-8:]))


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "big5", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def clean_gutenberg(raw_text: str) -> str:
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    start = START_RE.search(text)
    if start:
        text = text[start.end():]
    end = END_RE.search(text)
    if end:
        text = text[:end.start()]

    lines = text.splitlines()
    # Remove only obvious Gutenberg production/metadata lines at the beginning;
    # retain the book's own preface, poems, chapter titles and narrative.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and re.match(r"^(Produced by|Transcribed by|Proofread by|Distributed Proofreaders)\b", lines[0].strip(), re.I):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    text = "\n".join(lines)
    text = re.sub(r"[\t\u00a0 ]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import 13 Chinese Gutenberg plain-text editions")
    parser.add_argument("--only", nargs="*", help="only import selected IDs, e.g. zh-b002 zh-b007")
    parser.add_argument("--refresh", action="store_true", help="download source text again instead of using cache")
    args = parser.parse_args()

    catalog_path = DATA / "books.json"
    if not catalog_path.exists():
        raise SystemExit(f"Missing catalogue: {catalog_path}")
    books = json.loads(catalog_path.read_text(encoding="utf-8"))
    selected = set(args.only or [b["id"] for b in books])
    by_id = {b["id"]: b for b in books}
    unknown = selected - set(by_id)
    if unknown:
        raise SystemExit("Unknown book IDs: " + ", ".join(sorted(unknown)))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    if not isinstance(manifest, dict):
        manifest = {}

    ok = failed = 0
    for book in books:
        bid = book["id"]
        if bid not in selected:
            continue
        gid = int(book["gutenberg_id"])
        raw_path = RAW_DIR / f"{bid}_gutenberg_{gid}.txt"
        output_path = OUT_DIR / book.get("text_file", f"{bid}.txt")
        print(f"[{bid}] importing {book['title']} (Gutenberg #{gid}) ...", flush=True)
        entry = {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_provider": "Project Gutenberg plain text",
            "source_url": book["source_url"],
            "source_text_url": book["source_text_url"],
            "gutenberg_id": gid,
            "license": book.get("source_license"),
            "verified": False,
            "problems": [],
            "warnings": [],
        }
        try:
            if raw_path.exists() and not args.refresh:
                raw = raw_path.read_bytes()
            else:
                raw = download(gid)
                raw_path.write_bytes(raw)
            original = clean_gutenberg(decode_text(raw))
            simplified = CC.convert(original)
            simplified = simplified.replace("\r\n", "\n").strip()
            chars = len(CJK_RE.findall(simplified))
            if chars < 500:
                raise ValueError(f"Only {chars:,} Chinese characters after cleaning; refusing to mark as usable")
            output_path.write_text(simplified + "\n", encoding="utf-8")
            paragraphs = [p for p in re.split(r"\n\s*\n", simplified) if p.strip()]
            entry.update({
                "verified": True,
                "text_file": output_path.name,
                "characters": chars,
                "paragraphs": len(paragraphs),
                "text_sha256": hashlib.sha256(simplified.encode("utf-8")).hexdigest(),
                "conversion": "OpenCC t2s (Traditional Chinese to Simplified Chinese)",
                "first_chars": simplified[:300],
                "last_chars": simplified[-200:],
                "warnings": [
                    "This is the Project Gutenberg plain-text edition, not necessarily the exact edition named in the catalogue.",
                    "OpenCC conversion is automatic; inspect unusual historical terms and variant characters if they matter.",
                    "Project Gutenberg marks the ebook public domain in the USA; this is not worldwide legal advice.",
                ],
            })
            manifest[bid] = entry
            ok += 1
            print(f"[{bid}] READY: {chars:,} Simplified Chinese characters; {len(paragraphs):,} paragraphs", flush=True)
        except Exception as exc:
            entry["problems"] = [str(exc)]
            manifest[bid] = entry
            failed += 1
            print(f"[{bid}] FAILED: {exc}", flush=True)
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        time.sleep(1.5)  # Be polite to Gutenberg; this is not a high-concurrency downloader.

    print(f"\nFinished: {ok} ready, {failed} failed.")
    print(f"Manifest: {MANIFEST_PATH}")
    print(f"Raw download cache: {RAW_DIR}")
    print(f"Simplified output: {OUT_DIR}")
    if failed:
        print("Failed titles can be retried later with --only <book-id> or --refresh.")

if __name__ == "__main__":
    main()
