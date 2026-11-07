# Literary Roulette 0.3.0

## What changed from the supplied 0.2.1 project

- Replaced the previous catalogue with the exact 40-title English list discussed for the prototype: 25 familiar-classic candidates and 15 discovery candidates.
- Kept the existing independent passage/book/reveal operations, browser-local history, saved books, local TXT/EPUB upload, and private SQLite analytics.
- Reserved a non-functional English / Deutsch / 中文 collection bar for a later multilingual patch. English is the only active collection in this version.
- Kept fictional demo books in separate files and disabled them by default so they are not mixed into the 40-title catalogue.
- Added clearer first-run guidance: import the full texts locally, inspect the verification report, then launch the website. Full ebook files are intentionally not bundled in this archive.
- Added explicit first-story markers for all 40 catalogue records, including anthology/novel starts that would otherwise be ambiguous. Tightened ingestion so a book is not marked verified when the configured marker is missing or the importer cannot confidently locate the story start. Correct the per-book `text_start_pattern` after reviewing the source edition, then re-run the importer.
- Updated the README and tests to reflect the catalogue and future multilingual extension point.

## Important scope note

The archive contains catalogue metadata and an importer, not all 40 full ebook texts. On the user's Mac, run `python3 tools/ingest_gutenberg.py` while connected to the internet. Review each entry in `data/texts/manifest.json` and the cleaned text before considering it ready for public/commercial use. Automated checks are not a substitute for a rights review.
