# Production server settings.  Run:  gunicorn -c gunicorn.conf.py wsgi:app
# Keep ONE process with several threads while the shelf, rate gate and download queue live inside each process:
# more processes means more simultaneous downloads from Project Gutenberg and duplicated memory.
import os
bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("LR_THREADS", "8"))
worker_class = "gthread"
timeout = 60
graceful_timeout = 30
keepalive = 5
max_requests = 2000            # recycle the process now and then to keep memory flat
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
