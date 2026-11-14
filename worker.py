"""Stand-alone background worker: opens/refreshes the catalogue and grows the shared library.

Use it when you run more than one web process (e.g. Gunicorn with several workers). Start the web app with
LR_EMBEDDED_WORKER=0, then run once:

    python worker.py

Web and worker share instance/ (catalogue index, prepared books). They never download the same edition twice:
jobs are deduplicated by (language, book, edition) with lock files.
"""
import logging
import signal
import threading

from app import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = create_app({"EMBEDDED_WORKER": False})
    rt = app.runtime
    logging.info("Worker starting: %s", rt.worker.settings.__dict__)
    logging.info("Queued %d local book(s) for preparation.", rt.worker.seed_local())
    rt.worker.start()
    rt.sync.start()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(30):
        s, c = rt.worker.status(), rt.sync.status()
        logging.info("library %s | queued %d, active %d, failed %d | catalogue %s", {k: v["lake"] for k, v in s["languages"].items()},
                     s["queued"], s["active"], s["failed"], c.get("state"))
    rt.worker.stop()
    rt.sync.stop()


if __name__ == "__main__":
    main()
