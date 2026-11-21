# Literoulette 0.9 patch: source safety, backup chain, PostHog hook

Nothing here touches git or GitHub. Work in a scratch copy, test, then decide.

## 1. Try it in a scratch copy (macOS Terminal)
```bash
cp -R ~/PycharmProjects/YOUR-WEB-FOLDER ~/lr-trial      # your real folder is never modified
cd ~/lr-trial
P=~/Downloads/LR-patch-0.9                                # wherever you unzipped this
for f in source.py prep.py runtime.py; do patch --dry-run -p0 "$f" < "$P/diffs/$f.diff"; done   # "dry-run": nothing changes yet
cp "$P"/patched/{source.py,prep.py,runtime.py} .
cp "$P"/patched/tickets.js static/js/tickets.js
cp "$P"/static_js/analytics*.js static/js/
mkdir -p tests tools && cp "$P"/tests/* tests/ && cp "$P"/tools/preflight.py tools/
python3 tests/test_hardening_0_9.py -v                    # 16 tests, offline
node tests/tickets.test.mjs                               # needs Node; skip if you have none
python3 -m unittest discover -s tests                     # your OLD tests: see "Things that may need a touch"
python3 tools/preflight.py                                # read-only upload check
```
Undo: delete `~/lr-trial`. Your original is untouched.

## 2. Which line, which change (line numbers refer to YOUR uploaded files)
**source.py**
- 15-21 -> User-Agent now carries `LR_CONTACT_EMAIL`; `MAX_BYTES` cap (30 MB, `LR_MAX_BOOK_MB`).
- 85-91 `trusted_url` -> also trusts `pglaf.org` (your old rule rejected Gutenberg's own mirror) and any https mirror you list.
- NEW `sources()`, `mirror_urls()`, `SourceHealth` -> the chain: gutenberg.pglaf.org -> aleph.gutenberg.org -> your `LR_MIRROR_BASES` -> (www only if `LR_ALLOW_WWW=1`).
- 133 `download` -> redirects may only land on trusted hosts; size cap; **403/429 are no longer "permanent"** (before, one block would have blacklisted every book for good in failed.json); Retry-After honoured.
- 173-194 `fetch_clean_text` -> walks the chain, skips resting hosts; "not found everywhere" is permanent only if no host was skipped.

**prep.py**
- 49-54 settings: `breaker_cap` (pause doubles 2 min -> 1 h instead of a fixed 120 s forever).
- 79 -> `self.health` (shared by all processes in `instance/ready/source_health.json`), default fetcher uses it.
- 246-258 breaker: escalating pauses; `breaker_open()` is also true when every host is resting.
- 280 -> new `except SourceResting`: requeue with NO attempt counted and NO network call.
- 394-400 `estimated_wait`, 459 `replenish` (stops queueing while the source rests), 498 `status()` (adds `source.state`).

**runtime.py**: 24 short `LR_SECRET_KEY` rejected in production; 97 catalogue refresh weekly (was daily; `LR_CATALOGUE_MAX_AGE_HOURS`); end of file: `source_state()`, `source_notice()`, `fallback_pool(language)`.

**tickets.js**: 8-second budget -> asks for prepared books (`ready_only`), gives up politely ~5 s later instead of up to 70 s; stops polling a book after 2 min; exposes `info.notice` / `info.sourceState`.

**Analytics (new files)**: `static/js/analytics-config.js` (paste PostHog key) + `analytics.js`. Inert while `key` is empty.

## 3. The three-tier plan, as built
1. Main: Gutenberg's official mirrors (the policy-friendly route; www.gutenberg.org is for humans).
2. Backup: second official mirror, then any independent mirror from `LR_MIRROR_BASES` (e.g. `https://gutenberg.nabasny.com/`, listed on Gutenberg's mirror list; test it first, step 5).
3. Everything resting: no requests at all, `source_state()` = "down", visitors get `source_notice()` and books from `fallback_pool()` (prepared library + starter shelf).
Internet Archive is NOT wired in: I could not confirm its file layout. Test before building on it.

## 4. Two small edits in files I have NOT seen (send app.py, static/js/app.js and I will wire them exactly)
- app.py, where `/api/draw` builds its reply: add `"source_state": rt.source_state(), "notice": rt.source_notice()`; and when `request.json.get("ready_only")` or `rt.source_state() == "down"`, choose from `rt.fallback_pool(language)` and enqueue nothing new.
- base template, before `</body>`: `<script src="/static/js/analytics-config.js"></script><script defer src="/static/js/analytics.js"></script>`; then in app.js call `window.lrTrack?.("spin", {language, book_id})` beside each existing `/api/event` call (events: spin, reveal, another, save, share).
- catalogue_sync.py (the "Catalogue sync failed" lines) is also unseen: send it and it gets the same host-rest treatment.

## 5. Test the real sources FROM YOUR SERVER (my sandbox cannot reach them)
```bash
curl -sI -A "LiteraryRoulette-test (you@example.com)" https://gutenberg.pglaf.org/cache/epub/1342/pg1342.txt | head -3
curl -sI https://aleph.gutenberg.org/cache/epub/1342/pg1342.txt | head -3
curl -sI https://gutenberg.nabasny.com/1/3/4/1342/1342-0.txt | head -3
```
200 = usable. If a mirror 404s or errors, leave it out of `LR_MIRROR_BASES`.

## 6. Launch settings (environment on the server)
```
LR_PRODUCTION=1  LR_TRUSTED_PROXIES=1  LR_SECRET_KEY=<64 hex chars>
LR_CONTACT_EMAIL=you@yourdomain  LR_SITE_URL=https://your-domain
LR_LIBRARY_TARGET=300  LR_SOURCE_CONCURRENCY=1  LR_SOURCE_MIN_INTERVAL=3
```
`LR_LIBRARY_TARGET=all` makes the worker keep pulling random books forever, which looks like bulk harvesting. Start at 300 and raise it later.

## 7. PostHog
Free tier: 1M events/month, no card. Paste your key into `analytics-config.js`. Defaults: no cookie/localStorage (`persist: "memory"`), no autocapture, no session recording, anonymous events only, Do Not Track respected, only ids/counts are ever sent (never passage text, search text or file names). Add to Privacy: "anonymous usage events (spin, reveal, save, share) go to PostHog (US or EU, as you chose)". Check in 2 minutes: open the site, DevTools -> Network, filter `posthog`, spin once; the event appears under Live events in PostHog. I could not test against live PostHog from my side.
Content-Security-Policy (report mode): allow `script-src` + `connect-src` for `https://us-assets.i.posthog.com` and `https://us.i.posthog.com`.

## Things that may need a touch
- Your existing source tests that feed `https://www.gutenberg.org/...` URLs to a fake downloader will now see mirror URLs (built from `gutenberg_id`); update those URLs.
- `download()` default opener changed from `urllib.request.urlopen` to a redirect-checking one; tests that pass `opener=` are unaffected.
- Read-only history check for big files: `git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | awk '$1=="blob" && $3>50000000'`
