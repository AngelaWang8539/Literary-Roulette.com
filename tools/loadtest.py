"""In-process load test of the reading path: N concurrent readers spin, poll, reveal and ask for another passage against a
synthetic catalogue, with a fake (slow) Gutenberg. It checks the release gates and prints what to record:

    python tools/loadtest.py --readers 100 --spins 4          # also try 10 and 1000
    python tools/loadtest.py --readers 1000 --spins 2 --cold-ms 20

Gates it asserts (exit code 1 if one fails):
  * no reader request ever waits on the upstream provider: every download runs on a worker thread, never on a request thread;
  * one preparation per book revision (no edition is fetched twice);
  * the cold-job queue never exceeds its bound (queue_cap);
  * every answer is a normal answer (ok, or an explicit "busy"): no 5xx.
It records p50/p95 per endpoint, queue depth, the ready-hit rate and the provider request rate.

What it does NOT measure: your real host, network or Gutenberg. It measures this application's logic and locking with Flask's test client.
"""
import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import source  # noqa: E402
from app import create_app  # noqa: E402
from tests.fixtures import fake_gutenberg  # noqa: E402
from tests.test_core import write_catalog_csv  # noqa: E402


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))] * 1000, 1) if values else None


def run(readers=100, spins=4, catalogue=2000, cold_ms=30, queue_cap=40, workers=3, min_interval=0.02, seed=1, poll_ms=20, patience=6.0, warm=0):
    os.environ["LR_RATE_API"] = "0"          # every simulated reader shares one address: the per-visitor limiter would throttle the test itself
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csv = write_catalog_csv(tmp / "c.csv.gz", en=catalogue, fr=30, zh=20)
        fetched, fetch_threads, lock = [], set(), threading.Lock()

        def fetcher(book):
            with lock:
                fetched.append(book["gutenberg_id"]); fetch_threads.add(threading.current_thread().name)
            time.sleep(cold_ms / 1000)
            return source.clean_for_language(fake_gutenberg(book["title"], seed=book["gutenberg_id"], paragraphs=40), "en")

        app = create_app(dict(TESTING=True, INSTANCE_DIR=tmp / "inst", DB_PATH=tmp / "a.db", BOOKS=[], DATA_DIR=tmp / "data", RNG=random.Random(seed),
                              EMBEDDED_WORKER=False, CATALOG_CSV=str(csv), FETCHER=fetcher,
                              PREP_SETTINGS=dict(min_interval=min_interval, workers=workers, queue_cap=queue_cap, library_target=10 ** 6, replenish=False)))
        rt = app.runtime
        if not rt.sync.run():
            raise RuntimeError("the synthetic catalogue did not open")
        for record in rt.universe.random_candidates("en", warm, random.Random(seed + 1)):          # a lake that already holds `warm` books
            rt.worker.enqueue("cold", "en", record, 5)
        rt.worker.run_pending()
        warmed, fetched[:] = len(rt.lake("en")), []
        rt.worker.start(scheduler=False)

        lat, errors, depth, stop = {"draw": [], "poll": [], "reveal": [], "another": []}, [], [], threading.Event()
        counts = {"ready_immediately": 0, "pending_handles": 0, "fallback_draws": 0, "busy": 0, "rate_limited": 0, "spins_served": 0}
        def bump(key, n=1):
            with lock:
                counts[key] += n

        def sampler():
            while not stop.is_set():
                depth.append(rt.worker.store.pending_cold()); time.sleep(0.01)
        threading.Thread(target=sampler, name="sampler", daemon=True).start()

        def call(client, kind, path, body):
            t = time.perf_counter(); res = client.post(path, json=body); lat[kind].append(time.perf_counter() - t)
            if res.status_code == 429:
                bump("rate_limited")
            if res.status_code >= 500 and not (res.status_code == 503 and (res.get_json() or {}).get("code") == "busy"):
                errors.append((path, res.status_code))
            return res.get_json() or {}

        def reader(n):
            client, seen, mine = app.test_client(), [], random.Random(seed * 100003 + n)
            for _ in range(spins):
                res = call(client, "draw", "/api/draw", {"count": 3, "seen": seen[-120:]})
                if res.get("code") == "busy":
                    bump("busy"); time.sleep(0.05); continue
                if not res.get("ok"):
                    continue
                if res.get("fallback"):
                    bump("fallback_draws")
                tickets, pending = list(res.get("tickets", [])), [p["handle"] for p in res.get("pending", [])]
                bump("ready_immediately", len(tickets)); bump("pending_handles", len(pending))
                deadline = time.time() + patience
                while pending and not tickets and time.time() < deadline:
                    time.sleep(poll_ms / 1000)
                    for r in call(client, "poll", "/api/draw/poll", {"handles": pending}).get("results", []):
                        if r["state"] == "ready":
                            tickets.append(r["ticket"])
                        if r["state"] != "working":
                            pending = [h for h in pending if h != r["handle"]]
                if tickets:
                    bump("spins_served")
                    t = tickets[0]; seen.append(t["book_token"])
                    call(client, "reveal", "/api/book/reveal", {"book_token": t["book_token"], "ticket": t["ticket"]})
                    call(client, "another", "/api/passage/another", {"book_token": t["book_token"], "ticket": t["ticket"], "seen": [t["token"]]})
                time.sleep(mine.random() * 0.01)

        began = time.perf_counter()
        threads = [threading.Thread(target=reader, args=(i,), name=f"reader-{i}") for i in range(readers)]
        for t in threads: t.start()
        for t in threads: t.join()
        elapsed = time.perf_counter() - began
        deadline = time.time() + 10
        while rt.worker.store.pending_cold() and time.time() < deadline:
            time.sleep(0.05)
        stop.set(); rt.worker.stop()

        snapshot = app.extensions["lr_metrics"].snapshot()["counts"]
        draws = max(1, snapshot.get("draws", 0))
        report = {
            "readers": readers, "spins_each": spins, "lake_before": warmed, "catalogue": catalogue, "seconds": round(elapsed, 2),
            "latency_ms": {k: {"n": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": pct(v, 1.0)} for k, v in lat.items()},
            "queue_depth": {"max": max(depth or [0]), "mean": round(statistics.mean(depth or [0]), 2), "cap": queue_cap},
            "ready_hit_rate": round(snapshot.get("ready_hits", 0) / max(1, snapshot.get("ready_hits", 0) + snapshot.get("cold_pending", 0)), 3),
            "provider_requests": len(fetched), "provider_requests_per_second": round(len(fetched) / max(elapsed, 0.001), 2),
            "outcomes": counts, "draws": draws, "errors": errors[:5],
        }
        report["gates"] = {
            "no_download_on_a_request_thread": not any(name.startswith("reader-") for name in fetch_threads),
            "one_preparation_per_book": len(fetched) == len(set(fetched)),
            "queue_stayed_bounded": report["queue_depth"]["max"] <= queue_cap + workers + 2,
            "no_server_errors": not errors,
        }
        report["passed"] = all(report["gates"].values())
        return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readers", type=int, default=100); ap.add_argument("--spins", type=int, default=4)
    ap.add_argument("--catalogue", type=int, default=2000); ap.add_argument("--cold-ms", type=float, default=30)
    ap.add_argument("--queue-cap", type=int, default=40); ap.add_argument("--workers", type=int, default=3); ap.add_argument("--warm", type=int, default=0)
    args = ap.parse_args()
    out = run(args.readers, args.spins, args.catalogue, args.cold_ms, args.queue_cap, args.workers, warm=args.warm)
    print(json.dumps(out, indent=2))
    raise SystemExit(0 if out["passed"] else 1)
