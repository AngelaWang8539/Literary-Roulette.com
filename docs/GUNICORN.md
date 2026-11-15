# Serving Literary Roulette with Gunicorn

Gunicorn runs several copies (workers) of the web app so many readers are served at once. This guide is for ONE machine.

## What runs where

```
nginx (optional)  ->  gunicorn: 3-4 web workers   (readers: draws, polls, reveals, cold-book preparation on request)
                      python worker.py: 1 process  (opens/refreshes the catalogue, grows the library in the background)
                      all share ONE folder: instance/   (catalogue index, library, job progress, secret key, analytics)
```

## Steps

```
python -m pip install -r requirements.txt -r requirements-prod.txt
python worker.py                                   # terminal 1: keep it running (opens the catalogue the first time)
gunicorn -c gunicorn.conf.py wsgi:app              # terminal 2: open http://127.0.0.1:8000
```

Start `worker.py` first on a fresh install: the web workers show the librarian's catalogue cart until it has opened the catalogue.
For a quick local trial with a saved catalogue file: `LR_CATALOG_CSV=/path/to/pg_catalog.csv.gz python worker.py`.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LR_BIND` | `127.0.0.1:8000` | Address Gunicorn listens on. |
| `LR_WEB_WORKERS` / `LR_WEB_THREADS` | `min(4, CPU cores)` / `4` | Processes and threads per process. |
| `LR_EMBEDDED_WORKER` | `0` (set by the config) | Web workers must NOT run the background jobs; `worker.py` does. |
| `LR_SECRET_KEY` | auto (`instance/secret.key`) | Signs tickets. On one machine the file is created once and shared. On several machines set the same value everywhere. |
| `LR_LIBRARY_TARGET` | `300` | Books to prepare per language in the background; `all` for the whole catalogue (tens of GB). |
| `LR_SOURCE_MIN_INTERVAL` | `2.0` | Seconds between downloads from Gutenberg, shared by every process. |
| `LR_MAX_COLD_WAIT` | `12` | Queue length (seconds) beyond which a new pick is swapped for a prepared book. |

## Notices

1. **One machine, one folder.** All processes must see the same `instance/` folder on a local disk. Do not put it on a network drive (SQLite locking). Several machines need a shared database and object storage (see "Next upgrades").
2. **Never use `--preload`.** Each worker starts its own small background threads (they prepare the books that readers ask for).
3. **Never run two `worker.py`.** The catalogue refresh and library growth are meant to run once.
4. **More workers do not mean more load on Gutenberg.** The gap between downloads is a machine-wide file lock, and identical requests are de-duplicated.
5. **First start after upgrading:** the library layout changed. Delete `instance/ready` (it is only a cache of public texts). The starter books are rebuilt automatically and other books are fetched again when drawn.
6. **Poll and draw share state.** A reader's progress poll can be answered by any worker: progress lives in `instance/ready/jobs.sqlite`.
7. **Health checks:** `/healthz` (up), `/api/ocean` (catalogue + library), `/api/shelf/status` (queue, breaker; add header `X-LR-Admin: <LR_ADMIN_TOKEN>` for job details).
8. **Circuit breaker:** after 6 temporary download failures in a row, new downloads pause for 2 minutes and draws are served from prepared books. `/api/shelf/status` shows `breaker_open`.
9. **Behind nginx:** proxy to Gunicorn, enable gzip, keep `proxy_read_timeout 30s`, add HTTPS. Static files are served by the app; cache `/static/` for a day in nginx if you like.
10. **Run on boot (systemd, Linux):** one service for `python worker.py`, one for `gunicorn -c gunicorn.conf.py wsgi:app`, both with the same `WorkingDirectory` and `Environment=LR_SECRET_KEY=...`.

## Quick load test

```
brew install hey      # macOS
hey -z 30s -c 50 -m POST -T application/json -d '{"count":3}' http://127.0.0.1:8000/api/draw
```
Watch `/api/shelf/status`: with a cold library most answers come back as pending picks, and when the queue is long the replies switch to
prepared books (`shed` in the answer). Re-run after the library has grown to see warm speed. Test 10, 100 and 10,000 simultaneous readers before promising more.

## Known limits and next upgrades

- Each process keeps a small record of every prepared book it has seen; with the library at "all" that is roughly 80 MB per process. Fix when needed: look books up by token in the library index instead of keeping them in memory.
- Several machines: move `instance/` data to a managed database (jobs, library index), object storage + CDN (books), and a shared analytics store. The modules `jobstate.py` and `shelf.py` are the two places to swap.
