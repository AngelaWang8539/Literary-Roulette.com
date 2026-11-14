# Changelog

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
