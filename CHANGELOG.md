## 1.3 - book card, phone first screen (includes 1.2)

- Book card rebuilt to show only what helps: status badges + "Hide" on the top row; title; "By First Last" with years and illustrator as a small muted line; up to 3 tinted tags without the "Category:" prefix; a pull-quote from the passage just read; one primary "Read online" button and three quiet ghost buttons (Save, Share, Copy link); the "Had you heard of this book?" poll as a warm band with a thank-you after the answer.
- New `static/js/bookcard.js` (names, tags, pull-quote; pure functions with tests). The card is styled once, in the last block of `static/style.css`; `mobile.css` only adds phone tweaks. About 34 old card rules were removed.
- Phone first screen (patch 1.2): 56px header, 45px language bar, 13px intro, reading card about 60% of the screen, compact tray, notices moved under the tray, small footer.

## 1.0 - phone reading layout (computer look unchanged)

- New `static/mobile.css`, loaded only on screens up to 700px wide (`media` attribute in `base.html`): Lora/Poppins fonts, dark header with a rounded reading sheet, compact language pill, icon tray for the three actions, clean book card. `static/style.css` (the computer look) is untouched apart from the cover leftovers.
- Cover pictures removed everywhere (template, `app.js`, `library.py`, `runtime.py`, CSS).
- Popularity badge shortened ("Top 1%", "Top 10 this month", "Last 10%", "Discovery") by `static/js/badge.js` from the same data; full sentence in the tooltip. Share card uses the longer wording.

## 0.10 - one badge, one share card

- Badge: every tier has its own tone and mark, several wordings (a book always keeps its line), a real rank and download fact for the quiet tiers, and a "New to you" chip from this device's recent history. Books outside Gutenberg's monthly top 100 now use the catalogue's own download percentile instead of one repeated line.
- Share card: the new wide (computer) and tall (phone) designs replace the old card inside `static/js/sharecard.js`; fonts load only when a card is made. Demo books are labelled "Sample card". Still drawn on the visitor's device.
- Tests updated (share card) and added (badge lines).

# Changelog

## 0.9.0 - Book Registry, hardened providers, truthful popularity
- **Book Registry** (`registry.py`, SQLite): one stable `book_id` per book; rights, text and pool status are separate; provider snapshots; popularity observations; provider runs. The random index (`universe.py`, now schema 4) is built from it, so paused / withdrawn / failed books are never drawn (immediately, without a rebuild). The shipped index is upgraded locally, no network.
- **Registry Center** (`/admin/registry`, `LR_ADMIN_TOKEN`): Books, Imports, Sources & Sync. CLI twin: `tools/registry_admin.py`.
- **Author TXT intake** (`intake.py`): strict UTF-8, length and passage checks, duplicate hash, private hash-named store; a file that passes is only PENDING until rights are approved (territory, commercial scope, attribution, expiry) and the book is activated.
- **fetchguard.py**: https + host allowlist (redirects re-checked), User-Agent with contact (`LR_CONTACT_EMAIL`), timeouts, size and content-type limits, Retry-After, back-off with jitter, per-provider circuit breaker, shared rate gate. Catalogue download verifies the whole gzip, caps size, and a damaged / shrunken / duplicate-heavy file is REJECTED while the last good data stays live; an unchanged file rebuilds nothing.
- **Gutendex** enrichment (`gutendex.py`): targeted batches of 32 ids, dated snapshots, NULL for unknown counts, copyright flag holds a book for review, optional census.
- **Popularity is truthful**: the old placeholder percentile is gone. Percentile badges need a COMPLETE, fresh per-language population; a book outside the top 100 gets a neutral "off the beaten shelf" (it was labelled a hidden gem). Every badge names its source and period; Gutendex counts are not called "30-day" figures.
- **Draw**: the original cold pick stays a pending handle; overload or paused downloads fall back to the ready shelf and SAY so; the queue is bounded atomically; a new round starts only when every eligible book has been seen.
- **History**: 70 entries, language recorded, recent books seeded into the pouch BEFORE the first draw and kept when the catalogue reopens; export / restore file (not a sync).
- Production warns when `LR_SECRET_KEY` is not set. Constant-time admin token check, admin rate bucket. `tools/loadtest.py` for the concurrency gates.

## 0.8.0 - one catalogue source, honest first run
- Catalogue now comes from `pg_catalog.csv.gz`, the same single file the desktop app uses (seconds, not minutes). First run opens the catalogue
  with the librarian's cart and REAL progress; a prebuilt `data/universe_seed.sqlite` or `LR_CATALOG_CSV` makes it instant. Daily background refresh.
- Starter books are an emergency only (catalogue could not be opened, or all queues overloaded), never the default.
- Every spin is a uniform draw from the whole catalogue; prepared books are kept (the library grows toward the whole catalogue); cold picks show the
  librarian at once with real stages; overload swaps cold picks for random prepared books.
- Fixed the false "browser is blocking local storage" banner: an older IndexedDB missing a store is now repaired in place; the banner states the real reason.
- Removed the RDF snapshot pipeline, the rotating lake and the idle logic.

## 0.7.0
- Universe, lake, waves, minimalist look, 75-entry history cap, librarian drawing.

## 0.6.0
- Ready shelf, background worker, sealed ticket batches.
