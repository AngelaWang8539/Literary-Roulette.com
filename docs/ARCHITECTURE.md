# Architecture (0.9)

```
Browser: ticket pouch (3 sealed tickets), seen-lists, History (max 70) in IndexedDB, seeded into the pouch before the first draw
  -> Read API (Flask, stateless)  /api/draw  /api/draw/poll  /api/passage/another  /api/book/reveal  /api/event  /api/ocean
       -> CATALOGUE  instance/universe.sqlite  random index of every ELIGIBLE book (built from the Registry), draw position n
       -> REGISTRY   instance/registry.sqlite  one stable id per book; rights / text / pool status; provider snapshots; popularity; sync runs
       -> LIBRARY    instance/ready/<lang>/    every book prepared so far, shared, kept (+ a small starter shelf)
Background worker (prep.py): fetch -> clean -> segment -> publish, waves that grow the library, shedding under overload
Coordinator (catalogue_sync.py): pg_catalog.csv.gz -> validate -> Registry -> universe.sqlite (daily, real progress); top lists; Gutendex batches;
  each provider on its own schedule; every upstream call goes through fetchguard.py
```

## The idea (built to scale)
The spin is a uniform draw from the WHOLE catalogue: a cheap lookup that any number of stateless web servers can do. The
expensive part, turning a book into prepared passages, happens once per edition ever, and the result is shared by everyone.
As the library grows toward the whole catalogue (tens of GB, `LR_LIBRARY_TARGET=all`), almost every spin is a warm, instant
read from storage, and the librarian shows up mainly for books nobody has opened yet. The courtesy limit toward Gutenberg
(one download start per 2 s) only governs how fast the library grows, never how many readers it can serve.

## Flow
1. Open: `/api/ocean` (mode, totals, catalogue progress) -> `/api/draw`.
2. `mode = opening` (no index yet): 503 `catalogue_opening` with the sync's real message and percent; the page shows the cart at once.
3. `mode = ocean`: three uniform picks. Prepared ones come back as tickets; cold ones as `pending` handles polled via `/api/draw/poll`
   (real stages: queued, fetching with bytes, cleaning, indexing). The librarian shows at once because the wait is certain.
4. Overload: the original random pick stays a `pending` handle while the wait is bearable. Past `LR_MAX_COLD_WAIT` / `LR_QUEUE_CAP`, or while downloads are
   paused, the pick is served from the ready shelf and flagged (`fallback`, per ticket). The original pick is still prepared later, within the queue bound.
5. `mode = starter` only when the catalogue could not be opened at all (said out loud on the page).
6. While the visitor reads, the pouch refills quietly, so the next spin is usually instant.

## Limits that keep data from piling up
History 70 - seen-lists 120 books / 200 passages - failed list 300 - tickets expire (2 h) - library trimmed (oldest first) only above its target.

See docs/REGISTRY.md for the Registry, rights gate, popularity rules and author intake.

## Known limits
Cold-pick preparation runs in the web process (lock files dedupe with a separate worker.py). Many web machines would need a shared
queue (Redis/RQ), object storage + CDN for the library and a managed database; the module boundaries are drawn for that swap.
Load-test at 10 / 100 / 10,000 simultaneous readers before claiming more.
