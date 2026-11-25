# Literary Roulette - web 0.9 (the random ocean + Book Registry)

Read a random passage with no title attached. Keep reading, reveal the book, or spin to a different one.
Every spin is a uniform random draw from **every** English, French and Chinese text edition in Project Gutenberg's catalogue
(about 78,000 books, the same `pg_catalog.csv.gz` your desktop app uses). A book that has been prepared before is instant;
a new one is fetched once while the black-and-white librarian works with a real stage and a real percentage, and then it stays
in the shared library for every later reader.

## Run it (macOS / PyCharm terminal)

```
cd literary-roulette-web
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py                        # open http://127.0.0.1:5000
```

That is all. On the very first start there is no catalogue index yet, so the page opens the catalogue itself: the librarian's
cart appears at once with "Connecting to Project Gutenberg catalogue... Downloading catalogue... 42%... Reading catalogue
metadata... Updating the local index...". It takes seconds, then your first book is drawn.

Faster still, reuse the catalogue your desktop app already downloaded (no network needed for this step):

```
LR_CATALOG_CSV="/path/to/Literary-Roulette/data/pg_catalog.csv.gz" python app.py
```

To make fresh copies of the project open instantly, build the index once and commit it:

```
python tools/sync_catalogue.py --export-seed     # creates data/universe_seed.sqlite (a few MB)
```

## How to test it

1. **First run, catalogue:** delete `instance/` and run `python app.py`. Open the page straight away: you should see the cart animation with a
   percentage bar that moves only with real progress, then the first draw.
2. **First book:** after the catalogue opens, the librarian carries the big book ("Drawing number N of T", "Fetching this edition",
   "Cleaning the text", "Indexing passages") until your passage appears. The title stays hidden until you press Reveal.
3. **Random ocean:** spin 15 times and reveal a few. Titles should mostly be unknown to you. The line under the title says how many stories are waiting and how many you have met.
4. **Next spins:** while you read, the next books are fetched in the background, so many spins are instant; spin quickly and you will see the librarian again.
5. **Library growing:** open `http://127.0.0.1:5000/api/ocean` (`library` counts prepared books) and `/api/shelf/status`.
6. **Catalogue status / failures:** `/api/ocean` -> `catalogue.state` is `ready`, `running` or `failed` (with the reason). With no internet the page says so and opens the small starter shelf.
7. **Local storage banner:** it should no longer appear. If it does, it now ends with the exact reason in brackets, e.g. `(NotFoundError)`.
8. **Tests:** `python -m unittest discover -s tests -t . -v` (about 170 tests). For the IndexedDB tests also run `cd tests/js && npm install` once, then `for f in tests/js/*.mjs; do node $f; done`.
9. **Registry Center:** `LR_ADMIN_TOKEN=pick-a-long-secret python app.py`, then open `http://127.0.0.1:5000/admin/registry` and paste the token (Books / Imports / Sources & Sync). Without the token variable the page and its API answer 404.
10. **History memory:** spin until you have seen a few books, reload the page, and check in the browser's Network tab that the first `/api/draw` request carries those books in `seen`. My History -> *Export my history* saves a file; *Restore from a file* brings it back.
11. **Load gates:** `python tools/loadtest.py --readers 100 --spins 4 --warm 400` (also try 10 and 1000).

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LR_CATALOG_CSV` | unset | Use this `pg_catalog.csv.gz` instead of downloading one. |
| `LR_CATALOGUE_MAX_AGE_HOURS` | `24` | Refresh the catalogue in the background when the index is older. |
| `LR_LIBRARY_TARGET` | `300` | Prepared books to grow per language. Use `all` in production to prepare the entire catalogue (tens of GB). |
| `LR_MAX_COLD_WAIT` | `12` | If the queue of new books would make a visitor wait longer than this (seconds), or downloads are paused, the pick is served from the ready shelf **and the page says so** (`fallback` in the answer). The original pick is still prepared later. |
| `LR_QUEUE_CAP` | `40` | Hard bound on waiting cold jobs, shared by every process. |
| `LR_CONTACT_EMAIL` / `LR_SITE_URL` | unset | Your contact e-mail (and site) put in the User-Agent of every request to Gutenberg and Gutendex. Set them before going public (`tools/preflight.py` checks). |
| `LR_ADMIN_TOKEN` | unset | Turns on the Registry Center (`/admin/registry`) and the detailed `/api/shelf/status`. Pick a long random value. |
| `LR_RATE_ADMIN` | `60` | Admin requests per minute per address. |
| `LR_GUTENDEX` | `1` | `0` turns Gutendex enrichment off. |
| `LR_GUTENDEX_HOURS` / `LR_GUTENDEX_PER_RUN` | `6` / `160` | How often, and how many books (32 per request), one enrichment run looks at. |
| `LR_GUTENDEX_CENSUS` | `0` | `1` widens enrichment to the whole eligible catalogue, oldest observation first. Only a finished census lets percentile badges appear. Polite but long: about 2,400 requests per language-wide pass. |
| `LR_FIRST_WAVE` / `LR_MAX_WAVE` | `3` / `12` | Background wave sizes. |
| `LR_SOURCE_CONCURRENCY` / `LR_SOURCE_MIN_INTERVAL` | `2` / `2.0` | Simultaneous downloads / seconds between downloads (Gutenberg courtesy). |
| `LR_EMBEDDED_WORKER` | `1` | `1`: this process also keeps the catalogue fresh and grows the library. `0`: run `python worker.py` for that. |
| `LR_OFFLINE_STARTER` | `0` | `1`: always use the small bundled starter shelf (no catalogue). |
| `LR_SECRET_KEY` | auto | **Set it in production.** Readers' History keeps opaque book keys signed with it; if it changes (a redeploy on a disk that is not kept creates a new one) the keys stop matching and "do not repeat" forgets old books. |

## Rights

The catalogue lists what Project Gutenberg distributes; it states US public-domain status, and rules differ by country. Add Gutenberg IDs to
`data/blocklist.json` (a JSON list) to remove titles at once. Have the commercial plan checked before launch.

## Files
`app.py` routes - `catalogue_csv.py` download + read the catalogue - `catalogue_sync.py` first run, daily refresh, real progress -
`universe.py` the whole catalogue as a random index (SQLite, built from the Registry) - `registry.py` the Book Registry - `intake.py` author TXT intake + private text store - `fetchguard.py` every upstream request (allowlist, limits, retries, circuit breaker) - `gutendex.py` enrichment batches - `prep.py` worker, waves, shedding - `shelf.py` prepared books - `source.py` download a book -
`segment.py` splitting - `draw.py` fair selection - `tickets.py` sealed tickets - `library.py` catalogue + rights gate - `runtime.py` wiring -
`tools/registry_admin.py` operate the Registry - `tools/loadtest.py` concurrency gates - `docs/REGISTRY.md` how the Registry works -
`static/js/librarian-scene.js` the desktop app's drawing - `librarian.js` the overlay + progress bar - `tickets.js` the ticket pouch - `storage.js` History/Saved.
