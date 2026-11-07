# Literary Roulette 0.3.0 — English full-text web prototype

**Created by:** [Add your preferred public name]

Read a passage first. Reveal the book only when you choose to. This is a separate Flask website project; it does not modify the Tkinter desktop app.

## What this version includes

- The agreed **40-book English catalogue**: 25 familiar-classic candidates and 15 discovery candidates.
- Full-text ingestion and local storage. Once imported and reviewed, full texts are kept in `data/texts/`; a normal spin does not contact Project Gutenberg.
- Passages generated from the stored text, targeting roughly 80–150 words while respecting paragraphs and sentences where possible.
- Three independent actions: another passage from this book, reveal this book, and spin another book.
- My History with search, revealed/unrevealed filters, reopen/reveal/remove actions, and clear-history confirmation.
- My Saved Books.
- Browser-local `.txt` and `.epub` uploads, stored in IndexedDB and never sent to the server.
- Lightweight SQLite event analytics and a local `stats.py` report.
- A **reserved language bar**: English is active; Deutsch and 中文 are visual placeholders for a future patch, not working filters yet.

## Important: full texts are not bundled in this ZIP

The archive contains the catalogue and import tool, but not the 40 complete book files. Downloading the complete source corpus was not possible in the code-building environment. You need to run the importer on your Mac while connected to the internet.

The importer downloads the selected Project Gutenberg plain-text edition (with a pause between requests), checks the title, author, language, Gutenberg wrapper, and minimum text length, removes the standard wrapper, writes cleaned text locally, and records results in `data/texts/manifest.json`. A book is available to the roulette only if its local text passes the automated checks and its checksum still matches the manifest.

**Automated text checks are not a complete legal rights review.** Project Gutenberg marks many editions public domain in the US, but status can differ by country and may differ for translations or other added material. Review `data/texts/manifest.json`, the source edition, and its terms before public or commercial use. The catalogue is initially US-focused and should not be described as having worldwide rights clearance.

## Run in PyCharm on macOS

1. Extract the ZIP and open the `literary_roulette` folder in PyCharm.
2. Create or select a virtual environment for this project.
3. Open PyCharm's Terminal and install the dependency:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

4. In that same terminal, run the importer:

   ```bash
   python3 tools/ingest_gutenberg.py
   ```

   The importer may take a while because it pauses between downloads. You can import one title first to test:

   ```bash
   python3 tools/ingest_gutenberg.py --only adventures-of-sherlock-holmes
   ```

5. Check the results:

   ```bash
   python3 tools/catalogue_status.py
   ```

   Review `data/texts/manifest.json`. Check `first_chars`, `last_chars`, `warnings`, and source metadata. A failed book is intentionally unavailable; do not force it to load or replace it with invented text.

6. Start the website:

   ```bash
   python3 app.py
   ```

7. Open <http://127.0.0.1:5000> in your browser. Stop Flask with `Ctrl+C` in the terminal.

If `python3 tools/ingest_gutenberg.py` fails for a title because the source is temporarily unavailable, you can download that title's Plain Text UTF-8 file from its linked Project Gutenberg page and save it into `data/texts_raw/` as `pg<ID>.txt`, then run the importer again for that book. Do not repeatedly retry a blocked source; use the source's guidance and available mirrors.

## Demo content (optional, explicitly enabled)

Five fictional passages are separated into `data/demo_books.json` and `data/demo_passages.json`. They are not part of the agreed 40-book catalogue and are **disabled by default**.

For UI testing before real texts have been imported, run:

```bash
LR_ALLOW_DEMO=1 python3 app.py
```

A visible notice identifies fictional demo books. Do not use demo records as evidence of book-selling effectiveness. To run only imported real titles, leave `LR_ALLOW_DEMO` unset (the default) or set it to `0`.

## Where data lives

- `data/books.json`: the 40-book catalogue, source edition IDs, descriptions, language (`en`), category (`classic` or `discovery`), and story-start hints.
- `data/passages.json`: currently empty for public-approved excerpts; full-text passages are generated from `data/texts/*.txt` at server startup.
- `data/texts_raw/`: downloaded source text cache used by the importer.
- `data/texts/`: cleaned, complete local texts plus `manifest.json` verification details.
- `instance/analytics.db`: aggregate product event counts. It does not store a visitor's full browsing history or raw IP address.
- Browser IndexedDB: reader's history, saved books, and personal uploads. These stay in the current browser and do not sync across devices.

Do not put API secrets or personal information in the public catalogue. A local full-text library should not automatically be included in a public GitHub repository; decide what to distribute and how to meet the source licence obligations before publishing the repository.

## Reading experience

- **Another passage from this book**: chooses a different passage from the same title where one exists.
- **Reveal this book**: returns the current book details without changing the passage.
- **Spin another book**: selects a different title and returns to anonymous reading.
- **My History**: search passage words, filter by reveal status, reopen an exact passage, reveal it deliberately, remove an entry, or clear history.
- **My Saved Books**: save an identified book and find it later.
- **Upload my book**: process `.txt` and `.epub` locally in the browser (15 MB limit). DRM-protected files are not circumvented. Uploaded books stay private to this browser.

History and saved books are device-local; clearing browser site data may erase them. The local uploads are never sent to the server.

## Analytics

Run `python3 stats.py` from the project folder after people have used the site. It summarizes passage displays, reveals, saved books, title-awareness responses, and read/borrow/buy-link clicks by catalogue segment and title. A click is only evidence of interest, not proof of a purchase. Do not draw strong conclusions from a small number of visitors. The report is a local developer tool, not a public admin page.

## Extend later

Every book already has a `language` field set to `en`, and the header reserves space for language choices. A future multilingual patch can add German (`de`) and Chinese collections and make the selector functional without redesigning the core passage experience. Chinese text will need language-aware passage splitting; the current text splitter is designed for English punctuation.

To add a new book later, add a record to `data/books.json`, set a unique ID and source, add a verified `gutenberg_id` where applicable, and run the importer for that ID. For publisher-authorized material, build a separate approved-content workflow and record the permission, territory, and expiration conditions rather than pretending it is a Gutenberg book.

## Tests

Install Flask first, then run:

```bash
python3 -m unittest discover -s tests -t . -v
```

The suite includes Flask routes, full-text import checks, book-level selection fairness, and browser-side logic tests (Node.js is required for the latter). Run the suite again after changing the catalogue or passage-splitting rules.

## Before any public launch

- [ ] All 40 book texts have been downloaded and individually reviewed.
- [ ] `catalogue_status.py` shows the intended titles as ready; no source mismatch remains unexplained.
- [ ] Story starts contain no unwanted title page, preface, or contents list; endings contain no boilerplate.
- [ ] Rights and territorial use have been reviewed for every text and edition.
- [ ] No placeholder or fictional demo book is mixed into the real catalogue.
- [ ] Read/borrow/purchase URLs are genuine and tested.
- [ ] Privacy and hosting logs are reviewed for the public service.
- [ ] History, upload isolation, keyboard navigation, and mobile layout are tested in a real browser.
