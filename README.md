# Literary Roulette - web 0.8 (the random ocean)

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
8. **Tests:** `python -m unittest discover -s tests -t . -v` (60 tests). For the IndexedDB tests also run `cd tests/js && npm install` once.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LR_CATALOG_CSV` | unset | Use this `pg_catalog.csv.gz` instead of downloading one. |
| `LR_CATALOGUE_MAX_AGE_HOURS` | `24` | Refresh the catalogue in the background when the index is older. |
| `LR_LIBRARY_TARGET` | `300` | Prepared books to grow per language. Use `all` in production to prepare the entire catalogue (tens of GB). |
| `LR_MAX_COLD_WAIT` | `12` | If the queue of new books would make a visitor wait longer than this (seconds), the pick is swapped for a random prepared book. |
| `LR_FIRST_WAVE` / `LR_MAX_WAVE` | `3` / `12` | Background wave sizes. |
| `LR_SOURCE_CONCURRENCY` / `LR_SOURCE_MIN_INTERVAL` | `2` / `2.0` | Simultaneous downloads / seconds between downloads (Gutenberg courtesy). |
| `LR_EMBEDDED_WORKER` | `1` | `1`: this process also keeps the catalogue fresh and grows the library. `0`: run `python worker.py` for that. |
| `LR_OFFLINE_STARTER` | `0` | `1`: always use the small bundled starter shelf (no catalogue). |
| `LR_SECRET_KEY` | auto | Keep it to keep old History/Saved tokens valid. |

## Rights

The catalogue lists what Project Gutenberg distributes; it states US public-domain status, and rules differ by country. Add Gutenberg IDs to
`data/blocklist.json` (a JSON list) to remove titles at once. Have the commercial plan checked before launch.

## Files
`app.py` routes - `catalogue_csv.py` download + read the catalogue - `catalogue_sync.py` first run, daily refresh, real progress -
`universe.py` the whole catalogue (SQLite) - `prep.py` worker, waves, shedding - `shelf.py` prepared books - `source.py` download a book -
`segment.py` splitting - `draw.py` fair selection - `tickets.py` sealed tickets - `library.py` catalogue + rights gate - `runtime.py` wiring -
`static/js/librarian-scene.js` the desktop app's drawing - `librarian.js` the overlay + progress bar - `tickets.js` the ticket pouch - `storage.js` History/Saved.
