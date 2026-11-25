"""Gutendex enrichment: targeted batches by Gutenberg id, never a crawl, never on a visitor's request.

For ids the library actually holds (prepared books, the top lists) it asks Gutendex for up to 32 books per request and records,
per book: a dated provider snapshot (normalized fields + hash of the raw answer), a download-count observation (NULL when
absent), and catalogue fields (subjects, bookshelves) that an admin has not confirmed. A copyright flag holds a book for review.
Census mode (LR_GUTENDEX_CENSUS=1) widens the targets to the whole eligible catalogue, oldest observation first; only a finished
census lets percentile badges appear (registry.complete). Runs are recorded; a failed or partial run never removes data.
"""
import hashlib
import json
import time

import fetchguard

BASE = "https://gutendex.com/books"
BATCH = 32
SCHEMA = "gutendex-1"


def normalize(book):
    """Keep only what we use, with types checked. Bad or missing values become None, never 0."""
    count = book.get("download_count")
    summaries = [str(x)[:600] for x in (book.get("summaries") or [])[:2] if isinstance(x, str)]
    return {"id": int(book["id"]), "title": str(book.get("title") or "")[:300],
            "authors": [str(a.get("name"))[:120] for a in (book.get("authors") or []) if isinstance(a, dict) and a.get("name")][:5],
            "subjects": [str(x)[:120] for x in (book.get("subjects") or [])[:8] if isinstance(x, str)],
            "bookshelves": [str(x)[:80] for x in (book.get("bookshelves") or [])[:5] if isinstance(x, str)],
            "languages": [str(x) for x in (book.get("languages") or [])[:3]], "copyright": book.get("copyright") if isinstance(book.get("copyright"), bool) else None,
            "summaries": summaries, "download_count": int(count) if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None}


class Gutendex:
    NAME = "gutendex"

    def __init__(self, registry, guard, *, prefer=lambda: (), per_run=160, interval=6 * 3600, fresh_days=7, census=False, enabled=True, clock=time.time):
        self.registry, self.guard, self._prefer = registry, guard, prefer
        self.per_run, self.interval, self.fresh, self.census, self.enabled, self._clock = int(per_run), float(interval), fresh_days * 86400, census, enabled, clock

    def due(self):
        if not self.enabled:
            return False
        last = self.registry.last_run(self.NAME)
        return last is None or self._clock() - (last["finished_at"] or last["started_at"]) >= self.interval

    def fetch_batch(self, gids):
        """{gid: normalized book} for the ids Gutendex knows. Raises fetchguard.FetchError on any transport problem."""
        url, found, pages = f"{BASE}?ids={','.join(str(g) for g in gids)}", {}, 0
        while url and pages < 3:
            data = json.loads(self.guard.get(url, max_bytes=2_000_000).body.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise fetchguard.PermanentFetchError("Gutendex answered in an unexpected shape", error_class="schema")
            for book in data["results"]:
                if isinstance(book, dict) and str(book.get("id", "")).isdigit() and int(book["id"]) in gids:
                    found[int(book["id"])] = (normalize(book), hashlib.sha256(json.dumps(book, sort_keys=True).encode()).hexdigest())
            url, pages = data.get("next"), pages + 1
        return found

    def run(self):
        """One bounded run. Returns the recorded result dict."""
        run, retries_before = self.registry.start_run(self.NAME), self.guard.stats.get("retries", 0)
        targets = self.registry.enrichment_targets(limit=self.per_run, stale_before=self._clock() - self.fresh,
                                                   prefer=list(self._prefer()), census=self.census)
        done = missing = 0
        state, error = "ok", None
        try:
            for i in range(0, len(targets), BATCH):
                chunk = targets[i:i + BATCH]
                gids = {g for g, _b in chunk}
                found = self.fetch_batch(gids)
                rows = []
                for gid, bid in chunk:
                    hit = found.get(gid)
                    if hit is None:
                        missing += 1
                        self.registry.add_snapshot(self.NAME, bid, normalized=None, status="not_found", schema_version=SCHEMA)
                        rows.append((bid, self._language(bid), None, None, "not_found"))
                        continue
                    book, raw_hash = hit
                    self.registry.add_snapshot(self.NAME, bid, normalized=book, raw_hash=raw_hash, schema_version=SCHEMA)
                    self.registry.apply_enrichment(bid, subjects=book["subjects"], bookshelves=book["bookshelves"], copyright_flag=book["copyright"])
                    rows.append((bid, self._language(bid), book["download_count"], None, "ok"))
                    done += 1
                self.registry.add_popularity(rows, provider=self.NAME, metric="download_count", period="as_reported",
                                             scope="census" if self.census else "targeted")
        except (fetchguard.FetchError, ValueError) as exc:
            state, error = ("partial" if done or missing else "failed"), exc
        retries = self.guard.stats.get("retries", 0) - retries_before
        self.registry.finish_run(run, state, records=done + missing, completeness=(done + missing) / max(1, len(targets)), retries=retries,
                                 error_class=getattr(error, "error_class", type(error).__name__) if error else None, error=error)
        return {"state": state, "requested": len(targets), "enriched": done, "not_found": missing, "error": str(error) if error else None}

    def _language(self, book_id):
        return book_id.split("-")[1] if book_id.count("-") >= 2 else "en"
