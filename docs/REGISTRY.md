# The Book Registry

One place that says **which books exist, who may be shown, and where each fact came from**. The roulette never reads it per spin:
`universe.py` builds a small random index from it, and only the (small) set of books that are *not* eligible is consulted while drawing.

## Identity and the three separate gates

`book_id` is stable: Gutenberg `pg-<lang>-<gutenberg id>` (the ids the library already uses), author `au-<lang>-<slug>`, curated starters keep
their own ids. Each book carries three independent statuses. A book is drawable only when **all three** allow it:

| Gate | Values | Meaning |
|---|---|---|
| `rights_status` | `pending_verification`, `public_domain_candidate`, `approved`, `source_licensed`, `paused`, `withdrawn` | May we show it? `approved` / `source_licensed` must record **territory, commercial scope, attribution and expiry** (a date or `perpetual`); an expired date stops drawing by itself. Gutenberg books are `public_domain_candidate`: what Gutenberg states for the USA, **not** a worldwide or commercial clearance. |
| `text_status` | `pending`, `unverified`, `verified`, `failed`, `rejected` | Is there a usable text? `failed` (permanent, e.g. HTTP 404) is never drawn again until an admin clears failures. Temporary trouble never marks a book failed. |
| `pool_status` | `pending`, `active`, `paused`, `archived`, `starter` | Is it in the draw? `starter` = the small emergency shelf only. Reasons starting `auto:` belong to the importer (blocklist, missing upstream, copyright flag); the importer lifts only its own holds, never an admin's. |

Provider refreshes never overwrite fields an admin confirmed (`confirmed_fields`) and never touch rights or pool decisions.

## Data flows

```
pg_catalog.csv.gz --download(fetchguard checks, full gzip verify)--> validate --> Registry.import_gutenberg --> universe.build --> universe.sqlite
Gutenberg top lists --> instance/popularity.json (last good) + Registry snapshots (rank, visible count, period, date)
Gutendex (32 ids / request) --> provider_snapshots + popularity_latest + subjects/shelves + copyright flag
author TXT --> intake checks --> private store + PENDING row --> (review) approve rights --> activate --> rebuild index --> prepared from the store
```

* A rejected catalogue (damaged, shrunk by more than 20% in a language, no English, many duplicates / untitled) leaves the Registry and the index untouched; the run is recorded as `rejected`.
* Pausing, archiving, withdrawing and failed texts take effect at once (the index skips them). **Activating** or approving something that was not in the index needs a rebuild (the admin actions start one; `registry_admin.py rebuild` does it by hand).
* The Registry is bootstrapped from the shipped index on first start (no network), then filled properly by the first catalogue sync.

## Popularity: what is said, and what is not

* **Top 100** (Gutenberg's own list, last 30 days) is a trend signal: top 10 = jackpot, rest = lucky.
* **Gutendex `download_count`** is stored per book with the date it was observed. A lone count is shown only as a labelled fact. A **percentile** (top 1% / 10% / bottom 10%) exists only when *every* eligible book of that language has a fresh observation (a value or an explicit "not found"): run `LR_GUTENDEX_CENSUS=1` until the Registry Center shows the language as complete.
* Unknown stays unknown (`NULL`): never zero, never "least popular". Not being in the top 100 only ever says "not in the top 100".
* Not built yet: Literary Roulette's own reader-interest metrics (the tables already take `provider = 'literary_roulette'`).

## Author TXT: the order of things

1. `python tools/registry_admin.py intake FILE --slug ... --language ... --title ... --author ... [permission flags]`: strict UTF-8, enough text and passages, not a duplicate; stored under `instance/registry/texts/` (hash-named, never served directly). Result: **pending**.
2. Check the permission evidence yourself. `approve-rights BOOK_ID` records the decision; it fails if territory, commercial scope, attribution or expiry is missing.
3. `activate BOOK_ID` (refused unless rights are approved and the text is verified), which rebuilds the index. From then on it is drawn like any book, prepared from the private store (hash re-checked every time) with the author's credit and links.
4. `rights BOOK_ID withdrawn` or `pause BOOK_ID` stops it at once.

## Where it lives, and the move to Supabase

Today: `instance/registry.sqlite` (SQLite, WAL). On a host whose disk is thrown away at each deploy, **mount a persistent disk or the Registry (and author texts) start over**. Every table maps one-to-one onto Postgres; `Registry` is the only code that touches SQL, so a Postgres/Supabase version replaces that one class (and `TextStore` becomes object storage). That port is **not built or tested yet**. Move when more than one machine must share decisions, not before.

## Provider guards

`fetchguard.py` wraps Gutenberg (catalogue, top lists) and Gutendex. Set `LR_CONTACT_EMAIL`. Circuit state and per-provider results are in the Registry Center -> Sources & Sync. Book-text downloads keep their own worker-side retry rules (`prep.py`) and now also honour Retry-After, cap the size and re-check redirects.
