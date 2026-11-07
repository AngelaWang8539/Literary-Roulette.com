#!/usr/bin/env python3
"""Resumable, metadata-only Gutendex synchronisation for Literary Roulette.

The public API can occasionally stall. This version retries slow requests, saves
progress after every page, and resumes from the last successful page next time.
It writes the final catalogue only after every requested language is complete.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUTPUT = DATA / "gutendex_catalog.json"
PREVIEW_OUTPUT = DATA / "gutendex_catalog.preview.json"
CHECKPOINT = DATA / "gutendex_catalog.sync_progress.json"
API = "https://gutendex.com/books/"
CURATED_FILES = {
    "en": DATA / "books.json",
    "fr": DATA / "fr" / "books.json",
    "zh": DATA / "zh" / "books.json",
}
LANGUAGES = ("en", "fr", "zh")
UA = "LiteraryRoulette/0.4 (catalogue metadata sync; respectful retries)"
BACKOFF_SECONDS = (3, 7, 15, 30, 45, 60, 60)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_checkpoint() -> dict:
    if not CHECKPOINT.exists():
        return {"version": 1, "jobs": {}, "curated": {}}
    try:
        value = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("jobs"), dict):
            value.setdefault("curated", {})
            return value
    except (OSError, ValueError):
        pass
    # Preserve a damaged checkpoint for inspection instead of deleting it.
    backup = CHECKPOINT.with_suffix(".corrupt.json")
    if not backup.exists():
        CHECKPOINT.replace(backup)
    return {"version": 1, "jobs": {}, "curated": {}}


def fetch_json(url: str, timeout: int, retries: int) -> dict:
    last_error = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("results"), list):
                raise ValueError("Gutendex returned an unexpected JSON structure.")
            return value
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            retryable = exc.code in (408, 425, 429, 500, 502, 503, 504)
            if not retryable:
                raise RuntimeError(f"Gutendex returned HTTP {exc.code}: {url}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = str(reason)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            last_error = f"Invalid Gutendex response: {exc}"

        if attempt < retries:
            wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            print(
                f"  Gutendex request failed ({last_error}). Retry {attempt + 1}/{retries} "
                f"in {wait}s; timeout={timeout}s.",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"Gutendex did not respond after {retries + 1} attempts ({last_error}). "
        "Your network may be timing out or the public API may be temporarily unavailable. "
        f"Progress is saved in {CHECKPOINT}; run the same command again to resume."
    )


def compact(record: dict) -> dict | None:
    try:
        gid = int(record["id"])
    except (KeyError, TypeError, ValueError):
        return None
    formats = {
        key: value
        for key, value in (record.get("formats") or {}).items()
        if str(key).lower().startswith("text/plain")
        and isinstance(value, str)
        and value.startswith("https://")
    }
    if not formats:
        return None
    try:
        downloads = max(0, int(record.get("download_count") or 0))
    except (TypeError, ValueError):
        downloads = 0
    return {
        "id": gid,
        "title": str(record.get("title") or f"Gutenberg eBook #{gid}"),
        "authors": [
            {"name": str(a.get("name") or "")}
            for a in (record.get("authors") or [])
            if isinstance(a, dict)
        ],
        "subjects": [str(x) for x in (record.get("subjects") or []) if x],
        "bookshelves": [str(x) for x in (record.get("bookshelves") or []) if x],
        "languages": [str(x) for x in (record.get("languages") or []) if x],
        "copyright": record.get("copyright"),
        "download_count": downloads,
        "formats": formats,
    }


def curated_gutenberg_ids(language: str) -> list[int]:
    path = CURATED_FILES[language]
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = set()
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            try:
                gid = int(row.get("gutenberg_id"))
                if gid > 0:
                    found.add(gid)
            except (TypeError, ValueError):
                continue
    return sorted(found)


def add_curated_records(language: str, by_id: dict, state: dict, timeout: int, retries: int) -> None:
    """Look up existing curated IDs first, in small requests, so matching IDs are included."""
    ids = curated_gutenberg_ids(language)
    done = set(state.get("curated", {}).get(language, []))
    for start in range(0, len(ids), 12):
        chunk = ids[start:start + 12]
        if all(gid in done for gid in chunk):
            continue
        params = {"ids": ",".join(map(str, chunk))}
        payload = fetch_json(API + "?" + urllib.parse.urlencode(params), timeout, retries)
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue
            if item.get("copyright") is not False or language not in (item.get("languages") or []):
                continue
            row = compact(item)
            if row:
                by_id[row["id"]] = row
        done.update(chunk)
        state.setdefault("curated", {})[language] = sorted(done)
        atomic_json(CHECKPOINT, state)
        print(f"  Checked curated {language} IDs: {len(done)}/{len(ids)}", flush=True)
        time.sleep(0.25)


def collect_topic(language: str, topic: str | None, max_pages: int | None,
                  checkpoint: dict, timeout: int, retries: int) -> tuple[dict, bool]:
    topic_key = topic or "__all__"
    job_key = f"{language}|{topic_key}"
    job = checkpoint.setdefault("jobs", {}).setdefault(job_key, {
        "rows": {},
        "next_url": None,
        "pages_done": 0,
        "complete": False,
    })
    if job.get("complete"):
        print(f"  Resuming: {language}/{topic_key} was already completed.", flush=True)
        return job.get("rows", {}), True

    if not job.get("next_url"):
        params = {"languages": language, "copyright": "false"}
        if topic:
            params["topic"] = topic
        # Gutendex defaults to popularity ordering. Filtering text/plain locally
        # avoids an extra server-side MIME filter that can slow the query.
        job["next_url"] = API + "?" + urllib.parse.urlencode(params)

    run_pages = 0
    while job.get("next_url"):
        if max_pages is not None and run_pages >= max_pages:
            print(f"  Reached --max-pages={max_pages}; this language sample is partial.", flush=True)
            atomic_json(CHECKPOINT, checkpoint)
            return job.get("rows", {}), False

        url = job["next_url"]
        payload = fetch_json(url, timeout, retries)
        for item in payload.get("results", []):
            if not isinstance(item, dict) or item.get("copyright") is not False:
                continue
            row = compact(item)
            if row:
                old = job["rows"].get(str(row["id"]))
                if old is None or row["download_count"] > int(old.get("download_count") or 0):
                    job["rows"][str(row["id"])] = row

        job["pages_done"] = int(job.get("pages_done") or 0) + 1
        run_pages += 1
        job["next_url"] = payload.get("next")
        job["complete"] = not bool(job["next_url"])
        atomic_json(CHECKPOINT, checkpoint)  # save after every successful page
        print(
            f"  {language}/{topic_key}: page {job['pages_done']} · "
            f"{len(job['rows']):,} eligible text books collected",
            flush=True,
        )
        if job["next_url"]:
            time.sleep(0.35)

    return job.get("rows", {}), True


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable metadata-only Gutendex sync")
    parser.add_argument("--language", nargs="+", choices=LANGUAGES, default=list(LANGUAGES))
    parser.add_argument("--topic", action="append", default=[], help="Optional Gutendex subject/bookshelf filter; repeatable")
    parser.add_argument("--max-pages", type=int, default=None, help="Testing limit; writes a partial preview")
    parser.add_argument("--output", type=Path, default=None, help="Optional explicit output path")
    parser.add_argument("--timeout", type=int, default=90, help="Per-request timeout in seconds (default: 90)")
    parser.add_argument("--retries", type=int, default=6, help="Retries after a failed request (default: 6)")
    args = parser.parse_args()
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    if args.timeout < 10 or args.retries < 0:
        parser.error("--timeout must be >=10 and --retries must be >=0")

    output_path = args.output or (PREVIEW_OUTPUT if args.max_pages is not None else OUTPUT)
    checkpoint = load_checkpoint()

    existing_languages = {code: [] for code in LANGUAGES}
    if output_path.exists():
        try:
            old = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("languages"), dict):
                existing_languages.update({k: v for k, v in old["languages"].items() if k in LANGUAGES and isinstance(v, list)})
        except (OSError, ValueError):
            print("Existing catalogue could not be read; a new one will be written after successful sync.", file=sys.stderr)

    languages = dict(existing_languages)
    all_complete = True
    for language in args.language:
        print(f"Syncing {language} metadata (timeout {args.timeout}s, {args.retries} retries)…", flush=True)
        by_id = {int(r["id"]): r for r in languages.get(language, []) if isinstance(r, dict) and str(r.get("id", "")).isdigit()}

        # Query curated IDs before the broad catalogue. These smaller requests
        # guarantee existing curated works are considered even when their rank
        # places them outside an early popular-page sample.
        add_curated_records(language, by_id, checkpoint, args.timeout, args.retries)

        topics = args.topic or [None]
        language_complete = True
        for topic in topics:
            rows, complete = collect_topic(language, topic, args.max_pages, checkpoint, args.timeout, args.retries)
            for key, value in rows.items():
                gid = int(key)
                old = by_id.get(gid)
                if old is None or int(value.get("download_count") or 0) > int(old.get("download_count") or 0):
                    by_id[gid] = value
            language_complete = language_complete and complete

        languages[language] = sorted(by_id.values(), key=lambda row: (-int(row.get("download_count") or 0), int(row["id"])))
        print(f"  {len(languages[language]):,} eligible books in {language} snapshot.", flush=True)
        if not language_complete:
            all_complete = False

    is_preview = args.max_pages is not None
    if is_preview or all_complete:
        payload = {
            "provider": "Gutendex / Project Gutenberg",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sync_complete": bool(all_complete and not is_preview),
            "filters": {
                "copyright": "false (US public-domain classification)",
                "text_format": "text/plain filtered locally from Gutendex formats",
                "topics": args.topic,
                "max_pages": args.max_pages,
                "percentile_scope": "Per-language saved snapshot; partial when page limits or topic filters are used.",
            },
            "languages": languages,
        }
        atomic_json(output_path, payload)
        print(f"Metadata catalogue saved: {output_path}", flush=True)
        if is_preview:
            print("This is a PARTIAL PREVIEW; do not use it for final popularity percentiles.", flush=True)
        elif all_complete:
            print("Full metadata synchronisation completed. Full texts remain on-demand.", flush=True)
    else:
        print("Synchronisation stopped with an incomplete language. Successful pages remain saved in the checkpoint.", file=sys.stderr)
        print("Run the same command again; it will resume from the last successful page.", file=sys.stderr)
        return 2

    # Keep the checkpoint until a full sync has completed, allowing a safe retry
    # if the final output write or later operation is interrupted.
    if all_complete and not is_preview:
        try:
            CHECKPOINT.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
