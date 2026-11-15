"""Gunicorn settings for Literary Roulette. Start with:  gunicorn -c gunicorn.conf.py wsgi:app

Everything can be changed with environment variables, so the same file works on a laptop and on a server.
Read docs/GUNICORN.md first (start the background worker, keep one shared instance folder, set LR_SECRET_KEY).
"""
import multiprocessing
import os

# The web processes only serve readers and prepare the books those readers ask for. Growing the library and keeping
# the catalogue fresh is done ONCE by `python worker.py`, so more web processes never mean more downloads.
os.environ.setdefault("LR_EMBEDDED_WORKER", "0")

bind = os.environ.get("LR_BIND", "127.0.0.1:8000")
workers = int(os.environ.get("LR_WEB_WORKERS", min(4, multiprocessing.cpu_count())))
worker_class = "gthread"                                  # threads: a poll never waits behind another reader
threads = int(os.environ.get("LR_WEB_THREADS", 4))
timeout = 30
graceful_timeout = 20
keepalive = 5
max_requests = 2000                                       # recycle workers now and then (keeps memory flat)
max_requests_jitter = 200
preload_app = False                                       # each worker must start its own background threads
accesslog = "-"
errorlog = "-"
