"""Run all tests:  python -m unittest discover -s tests -t . -v"""
import hashlib
import json
import random
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import catalogue_csv
import draw
import prep
import segment
import source
import tickets
from app import create_app
from library import Catalogue
from shelf import ReadyShelf
from tests.fixtures import fake_gutenberg, install_text_book

SECRET = b"k" * 32


def book(i, **kw):
    return {"id": f"b{i}", "title": f"Title {i}", "author": "Ann", "gutenberg_id": 9000 + i,
            "rights_status": "approved", "language": "en", **kw}


class TicketTests(unittest.TestCase):
    def test_round_trip_and_tamper_and_expiry(self):
        t = tickets.issue(SECRET, "en", "abc123", 7, 60, now=1000)
        self.assertEqual(tickets.verify(SECRET, t, now=1010)["index"], 7)
        self.assertIsNone(tickets.verify(SECRET, t, now=1061))
        self.assertIsNone(tickets.verify(b"x" * 32, t, now=1010))
        body, sig = t.split(".")
        self.assertIsNone(tickets.verify(SECRET, body + "." + "A" * len(sig), now=1010))
        self.assertIsNone(tickets.verify(SECRET, "garbage", now=1010))

    def test_ticket_does_not_reveal_the_book(self):
        import base64
        t = tickets.issue(SECRET, "en", "opaque0token", 3, 60)
        raw = base64.urlsafe_b64decode(t.split(".")[0] + "==").decode()
        self.assertNotIn("Sherlock", raw)
        self.assertIn("opaque0token", raw)


class DrawTests(unittest.TestCase):
    def test_book_first_not_passage_weighted(self):
        rng = random.Random(1)
        counts = {"a": 0, "b": 0}
        for _ in range(4000):
            ids, _ = draw.pick_books(["a", "b"], lambda b: b, 1, set(), rng)
            counts[ids[0]] += 1
        self.assertAlmostEqual(counts["a"] / 4000, 0.5, delta=0.04)

    def test_distinct_books_avoid_current_and_restart(self):
        rng = random.Random(2)
        ids, restarted = draw.pick_books(["a", "b", "c"], lambda b: b, 3, set(), rng, avoid_ids={"a"})
        self.assertEqual(sorted(ids), ["b", "c"]); self.assertFalse(restarted)
        ids, restarted = draw.pick_books(["a", "b", "c"], lambda b: b, 1, {"a", "b", "c"}, rng, avoid_ids={"a"})
        self.assertTrue(restarted); self.assertIn(ids[0], ("b", "c"))
        self.assertEqual(draw.pick_books(["a"], lambda b: b, 1, set(), rng, avoid_ids={"a"}), ([], False))

    def test_passage_avoids_current_and_seen(self):
        rng = random.Random(3)
        for _ in range(50):
            self.assertNotIn(draw.pick_passage_index(4, lambda i: i, {1}, rng, exclude_index=0), (0, 1))
        self.assertIsNone(draw.pick_passage_index(1, lambda i: i, set(), rng, exclude_index=0))


class ShelfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def test_publish_read_reload_and_atomic_replace(self):
        s = ReadyShelf(self.tmp.name)
        s.publish("en", "x", ["héllo", "wörld"], edition_key="e", content_hash="a" * 64, segment_version="v1")
        self.assertEqual([s.read_passage("en", "x", i) for i in (0, 1, 2)], ["héllo", "wörld", None])
        self.assertEqual(ReadyShelf(self.tmp.name).ids("en"), ("x",))
        s.publish("en", "x", ["new"], edition_key="e", content_hash="b" * 64, segment_version="v1")
        self.assertEqual(len(list((Path(self.tmp.name) / "en").glob("*.txt"))), 1)

    def test_partial_book_is_never_ready(self):
        s = ReadyShelf(self.tmp.name)
        s.publish("en", "x", ["abc"], edition_key="e", content_hash="a" * 64, segment_version="v1")
        data = next((Path(self.tmp.name) / "en").glob("*.txt")); data.write_bytes(b"ab")   # truncated
        self.assertEqual(ReadyShelf(self.tmp.name).ids("en"), ())

    def test_other_process_changes_are_picked_up(self):
        a, b = ReadyShelf(self.tmp.name, scan_interval=0), ReadyShelf(self.tmp.name, scan_interval=0)
        a.publish("fr", "z", ["bonjour"], edition_key="e", content_hash="c" * 64, segment_version="v1")
        self.assertTrue(b.refresh_if_changed()); self.assertEqual(b.ids("fr"), ("z",))

    def test_evict_retires_oldest_by_age_only_and_never_local_books(self):
        s = ReadyShelf(self.tmp.name)
        for i in range(4):
            s.publish("en", f"c{i}", ["p"], edition_key="e", content_hash=str(i) * 64, segment_version="v", external=True, book={"id": f"c{i}"})
            s._index["en"][f"c{i}"]["prepared_at"] = 1000 + i
        s.publish("en", "local", ["p"], edition_key="e", content_hash="9" * 64, segment_version="v")
        s.note_interest("en", "c0", "saved")                       # interest must NOT keep the oldest alive
        self.assertEqual(sorted(s.evict_cold("en", 2)), ["c0", "c1"])
        self.assertIn("local", s.ids("en")); self.assertEqual(len(s.external_ids("en")), 2)


def make_worker(tmp, books, fetcher, **settings):
    cat = Catalogue(books, "s", "en", allow_pending_rights=False)
    shelf = ReadyShelf(Path(tmp) / "ready")
    settings = dict(min_interval=0, backoff_base=0.01, check_interval=3600, **settings)
    clock = [1000.0]
    w = prep.PrepWorker(shelf, {"en": cat}, None, Path(tmp) / "ready", prep.Settings(**settings), fetcher=fetcher,
                        rng=random.Random(1), clock=lambda: clock[0])
    return w, shelf, cat, clock


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def text(self):
        return source.clean_for_language(fake_gutenberg(paragraphs=120), "en")

    def test_cold_book_is_fetched_once_for_many_requests(self):
        calls = []
        w, shelf, cat, _ = make_worker(self.tmp.name, [], lambda b: calls.append(b["id"]) or self.text())
        results = [w.enqueue(prep.COLD, "en", book(1), 5) for _ in range(100)]
        self.assertEqual(results.count(True), 1)
        w.run_pending()
        self.assertEqual(calls, ["b1"]); self.assertTrue(shelf.has("en", "b1")); self.assertIn("b1", cat.books)
        self.assertGreater(shelf.passage_count("en", "b1"), 3)

    def test_transient_failure_retries_with_backoff_then_succeeds(self):
        attempts = []
        def flaky(b):
            attempts.append(1)
            if len(attempts) < 3:
                raise source.TransientSourceError("HTTP 503")
            return self.text()
        w, shelf, _, clock = make_worker(self.tmp.name, [], flaky)
        w.enqueue(prep.COLD, "en", book(1), 5)
        for _ in range(5):
            w.run_pending(); clock[0] += 10_000
        self.assertEqual(len(attempts), 3); self.assertTrue(shelf.has("en", "b1")); self.assertEqual(w.status()["failed"], 0)

    def test_permanent_failure_is_remembered_and_not_retried(self):
        calls = []
        def gone(b):
            calls.append(1); raise source.PermanentSourceError("HTTP 404")
        w, shelf, _, clock = make_worker(self.tmp.name, [], gone)
        w.enqueue(prep.COLD, "en", book(1), 5); w.run_pending(); clock[0] += 99999; w.run_pending()
        self.assertEqual(len(calls), 1); self.assertFalse(shelf.has("en", "b1"))
        self.assertFalse(w.enqueue(prep.COLD, "en", book(1), 5))
        w2 = prep.PrepWorker(shelf, {"en": Catalogue([], "s")}, None, Path(self.tmp.name) / "ready", prep.Settings(min_interval=0))
        self.assertFalse(w2.enqueue(prep.COLD, "en", book(1), 5))      # survives a restart

    def test_failed_download_never_publishes_a_partial_book(self):
        w, shelf, _, _ = make_worker(self.tmp.name, [], lambda b: (_ for _ in ()).throw(source.PermanentSourceError("bad")))
        w.enqueue(prep.COLD, "en", book(1), 5); w.run_pending()
        self.assertEqual(shelf.ids("en"), ())

    def test_lock_file_stops_two_processes_preparing_the_same_edition(self):
        calls = []
        w, shelf, _, _ = make_worker(self.tmp.name, [], lambda b: calls.append(1) or self.text())
        key = w.job_key("en", "b1", prep.edition_key_for(book(1)))
        release = w._claim(key); self.assertIsNotNone(release)
        self.assertIsNone(w._claim(key))
        w.enqueue(prep.COLD, "en", book(1), 5); w.run_pending()
        self.assertEqual(calls, []); release()

    def test_source_concurrency_is_bounded(self):
        live, peak, lock = [0], [0], threading.Lock()
        def slow(b):
            with lock:
                live[0] += 1; peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
            return self.text()
        w, shelf, _, _ = make_worker(self.tmp.name, [], slow, workers=6, source_concurrency=2)
        for i in range(8):
            w.enqueue(prep.COLD, "en", book(i), 5)
        w.start()
        deadline = time.time() + 20
        while shelf.count("en") < 8 and time.time() < deadline:
            time.sleep(0.05)
        w.stop()
        self.assertEqual(shelf.count("en"), 8); self.assertLessEqual(peak[0], 2)

    def test_stale_segment_version_triggers_rebuild(self):
        folder = Path(self.tmp.name, "texts"); text, manifest = install_text_book(folder, "loc", fake_gutenberg(paragraphs=80))
        b = {"id": "loc", "title": "L", "author": "A", "rights_status": "approved", "text_file": "loc.txt", "language": "en"}
        cat = Catalogue([b], "s", "en", texts_dir=folder, manifest=manifest)
        shelf = ReadyShelf(Path(self.tmp.name) / "ready")
        w = prep.PrepWorker(shelf, {"en": cat}, None, Path(self.tmp.name) / "ready", prep.Settings(min_interval=0))
        self.assertEqual(w.seed_local(), 1); w.run_pending(); self.assertEqual(w.seed_local(), 0)
        meta = shelf.meta("en", "loc"); shelf.publish("en", "loc", ["x"], edition_key="e", content_hash=meta["content_hash"], segment_version="old")
        self.assertEqual(w.seed_local(), 1)


class RightsGateTests(unittest.TestCase):
    def test_pending_rights_are_not_served_unless_allowed_and_verified_is_not_cleared(self):
        books = [book(1, rights_status="pending_verification"), book(2), book(3, rights_status="review_required")]
        self.assertEqual(sorted(Catalogue(books, "s").books), ["b2"])
        self.assertEqual(sorted(Catalogue(books, "s", allow_pending_rights=True).books), ["b1", "b2"])


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name); self.texts = tmp / "texts"
        books, manifest = [], {}
        for i, title in enumerate(("Alpha", "Beta", "Gamma")):
            _t, manifest = install_text_book(self.texts, f"bk{i}", fake_gutenberg(title, seed=i + 1, paragraphs=100))
            books.append({"id": f"bk{i}", "title": title, "author": "Ann", "rights_status": "approved", "language": "en",
                          "text_file": f"bk{i}.txt", "links": {"read": f"https://books.example.org/{i}"}})
        self.config = dict(TESTING=True, INSTANCE_DIR=tmp / "inst", DB_PATH=tmp / "a.db", BOOKS=books, EN_TEXTS_DIR=self.texts,
                           TEXTS_DIR=self.texts, DATA_DIR=tmp / "data", RNG=random.Random(5), EMBEDDED_WORKER=False,
                           OFFLINE_STARTER=True)
        self.app = create_app(self.config); self.client = self.app.test_client()

    def prepare(self):
        self.app.runtime.worker.seed_local(); self.app.runtime.worker.run_pending()

    def post(self, path, **body):
        return self.client.post(path, json=body).get_json()

    def test_empty_shelf_gives_an_honest_status_and_never_blocks(self):
        res = self.client.post("/api/draw", json={}); self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()["code"], "empty_shelf")

    def test_draw_returns_distinct_sealed_tickets_without_titles(self):
        self.prepare()
        res = self.post("/api/draw", count=3)
        self.assertEqual(len({t["book_token"] for t in res["tickets"]}), 3)
        blob = json.dumps(res)
        for word in ("Alpha", "Beta", "Gamma", "Ann"):
            self.assertNotIn(word, blob)

    def test_reveal_resolves_the_token_and_reads_no_text(self):
        self.prepare()
        t = self.post("/api/draw", count=1)["tickets"][0]
        shelf = self.app.shelf; original = shelf.read_passage
        shelf.read_passage = lambda *a, **k: self.fail("reveal must not read passage text")
        try:
            book_ = self.post("/api/book/reveal", book_token=t["book_token"], ticket=t["ticket"])["book"]
        finally:
            shelf.read_passage = original
        self.assertIn(book_["title"], ("Alpha", "Beta", "Gamma")); self.assertTrue(book_["links"])
        self.assertTrue(self.post("/api/book/reveal", book_token="nonsense")["code"] == "unknown_book")

    def test_another_passage_differs_and_respects_seen(self):
        self.prepare()
        t = self.post("/api/draw", count=1)["tickets"][0]
        seen = [t["token"]]
        for _ in range(8):
            n = self.post("/api/passage/another", book_token=t["book_token"], ticket=t["ticket"], seen=seen)
            self.assertTrue(n["ok"]); self.assertNotIn(n["token"], seen); seen.append(n["token"])

    def test_visitors_are_independent_and_nothing_is_consumed(self):
        self.prepare()
        first = {t["book_token"] for _ in range(30) for t in self.post("/api/draw", count=3)["tickets"]}
        self.assertEqual(len(first), 3)                       # all three books remain drawable, repeatedly
        before = self.app.shelf.count("en"); self.post("/api/draw", count=3); self.assertEqual(self.app.shelf.count("en"), before)

    def test_seen_books_are_excluded_then_round_restarts(self):
        self.prepare()
        toks = [t["book_token"] for t in self.post("/api/draw", count=3)["tickets"]]
        res = self.post("/api/draw", count=1, seen=toks[:2])
        self.assertEqual(res["tickets"][0]["book_token"], toks[2]); self.assertFalse(res["restarted"])
        res = self.post("/api/draw", count=1, seen=toks, avoid=[toks[0]])
        self.assertTrue(res["restarted"]); self.assertNotEqual(res["tickets"][0]["book_token"], toks[0])

    def test_warm_spins_never_touch_the_source(self):
        self.prepare()
        called = []
        self.app.runtime.worker._fetch = lambda b: called.append(b) or ""
        for _ in range(20):
            self.post("/api/draw", count=3)
        self.assertEqual(called, [])

    def test_events_need_a_valid_book_and_forged_tickets_are_ignored(self):
        self.prepare()
        t = self.post("/api/draw", count=1)["tickets"][0]
        self.assertTrue(self.post("/api/event", event="passage_displayed", book_token=t["book_token"], ticket=t["ticket"])["ok"])
        self.assertFalse(self.post("/api/event", event="drop_table", book_token=t["book_token"])["ok"])
        self.assertFalse(self.post("/api/event", event="passage_displayed", book_token="zzz")["ok"])
        from analytics import report
        self.assertEqual(report(self.config["DB_PATH"])["totals"].get("passage_displayed"), 1)

    def test_go_redirects_by_book_token_only(self):
        self.prepare()
        t = self.post("/api/draw", count=1)["tickets"][0]
        r = self.client.get(f"/go/read/{t['book_token']}"); self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/go/read/nope").status_code, 404)

    def test_language_endpoints_and_shelf_status(self):
        self.prepare()
        self.assertEqual(self.post("/api/language", language="fr")["language"], "fr")
        self.assertEqual(self.client.get("/api/languages").get_json()["active"], "fr")
        status = self.client.get("/api/shelf/status").get_json()
        self.assertEqual(status["ready"]["en"], 3); self.assertNotIn("jobs", status)

    def test_page_has_librarian_slot_and_language_attribute(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="librarian"', html); self.assertIn('data-language="en"', html)


def write_catalog_csv(path, en=800, fr=60, zh=30, extra_rows=()):
    """A pg_catalog.csv.gz like Gutenberg's, with `en`/`fr`/`zh` text books plus some rows that must be ignored."""
    import csv, gzip, io
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Text#", "Type", "Issued", "Title", "Language", "Authors", "Subjects", "LoCC", "Bookshelves"])
    for lang, offset, n in (("en", 0, en), ("fr", 50000, fr), ("zh", 90000, zh)):
        for i in range(n):
            w.writerow([1000 + i + offset, "Text", "2020-01-01", f"Title {i + offset}", lang, f"Writer {i}, A.", "Fiction; Adventure", "PS", "Adventure"])
    w.writerow([5, "Sound", "2020-01-01", "A recording", "en", "X", "", "", ""])                       # not text
    w.writerow([6, "Text", "2020-01-01", "Mixed", "la; en", "X", "", "", ""])                            # first language is Latin
    w.writerow([1000, "Text", "2020-01-01", "Duplicate id", "en", "X", "", "", ""])                      # duplicate id
    for row in extra_rows:
        w.writerow(row)
    Path(path).write_bytes(gzip.compress(buf.getvalue().encode("utf-8")))
    return Path(path)


class CatalogueCsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)

    def test_only_text_rows_first_language_and_no_duplicates(self):
        import universe
        path = write_catalog_csv(self.dir / "c.csv.gz", en=50, fr=5, zh=3)
        records = catalogue_csv.read_records(path)
        self.assertEqual(len({r["gutenberg_id"] for r in records}), len(records))
        self.assertNotIn(5, {r["gutenberg_id"] for r in records})
        counts = universe.build_from_records(records, self.dir / "u.sqlite", log=lambda *a: None)
        self.assertEqual(counts, {"en": 50, "fr": 5, "zh": 3})                  # "la; en" is Latin first, so it is left out

    def test_download_reports_real_percentages_and_uses_the_alternate_server(self):
        import urllib.error
        raw = write_catalog_csv(self.dir / "src.csv.gz").read_bytes(); seen, calls = [], []
        def opener(req, timeout):
            calls.append(req.full_url)
            if "dev." not in req.full_url:
                raise urllib.error.URLError("main server down")
            return SourceTests.Resp(raw)
        out = catalogue_csv.download(self.dir / "got.csv.gz", lambda m, p: seen.append((m, p)), opener=opener)
        self.assertEqual(len(calls), 2); self.assertTrue(out.exists())
        pcts = [p for m, p in seen if m.startswith("Downloading")]
        self.assertTrue(pcts and pcts == sorted(pcts) and pcts[-1] >= 50)
        self.assertTrue(any("alternate server" in m for m, _ in seen))

    def test_both_servers_down_is_reported_not_hidden(self):
        import urllib.error
        def opener(req, timeout):
            raise urllib.error.URLError("down")
        with self.assertRaises(ConnectionError):
            catalogue_csv.download(self.dir / "x.csv.gz", opener=opener)

    def test_blocklist_and_text_urls(self):
        import universe
        records = catalogue_csv.read_records(write_catalog_csv(self.dir / "c.csv.gz", en=20, fr=1, zh=1))
        universe.build_from_records(records, self.dir / "u.sqlite", blocked=[1000, 1001], log=lambda *a: None)
        u = universe.Universe(self.dir / "u.sqlite")
        self.assertEqual(u.count("en"), 18)
        rec = u.get("en", 1005)
        self.assertEqual(rec["text_urls"][0], "https://dev.gutenberg.org/cache/epub/1005/pg1005.txt")
        self.assertTrue(all(source.trusted_url(x) for x in rec["text_urls"]))


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)

    def build(self, **kw):
        import universe
        records = catalogue_csv.read_records(write_catalog_csv(self.dir / "c.csv.gz", **kw))
        universe.build_from_records(records, self.dir / "u.sqlite", seed=1, log=lambda *a: None)
        return universe.Universe(self.dir / "u.sqlite", self.dir / "block.json")

    def test_draws_are_uniform_over_the_whole_catalogue(self):
        u = self.build(en=1000); rng = random.Random(7)
        gids = [u.random_candidates("en", 1, rng)[0]["gutenberg_id"] for _ in range(3000)]
        self.assertGreater(len(set(gids)), 900)
        self.assertAlmostEqual(sum(1 for g in gids if g < 1500) / 3000, 0.5, delta=0.05)

    def test_blocklist_is_honoured_without_rebuilding(self):
        u = self.build(en=20)
        (self.dir / "block.json").write_text(json.dumps([1000 + i for i in range(19)])); u.reload()
        for seed in range(60):
            self.assertEqual([c["gutenberg_id"] for c in u.random_candidates("en", 1, random.Random(seed))] or [1019], [1019])

    def test_csv_catalogue_carries_no_popularity_so_no_badge(self):
        self.assertIsNone(self.build(en=5).get("en", 1002)["popularity"])


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)
        self.csv = write_catalog_csv(self.dir / "c.csv.gz", en=40, fr=6, zh=3)
        self.config = dict(TESTING=True, INSTANCE_DIR=self.dir / "inst", DB_PATH=self.dir / "a.db", BOOKS=[], DATA_DIR=self.dir / "data",
                           EMBEDDED_WORKER=False, EN_TEXTS_DIR=self.dir / "t", TEXTS_DIR=self.dir / "t")

    def make(self, **extra):
        return create_app({**self.config, **extra}).runtime

    def test_first_run_reports_real_progress_then_opens_the_ocean(self):
        rt = self.make(CATALOG_CSV=str(self.csv))
        self.assertEqual(rt.mode("en"), "opening")
        seen = []
        original = rt.sync._set
        rt.sync._set = lambda **c: (seen.append((c.get("message"), c.get("percent"))), original(**c))[1]
        self.assertTrue(rt.sync.run())
        self.assertEqual(rt.mode("en"), "ocean"); self.assertEqual(rt.universe.count("en"), 40)
        pcts = [p for _m, p in seen if isinstance(p, int)]
        self.assertEqual(pcts, sorted(pcts)); self.assertEqual(pcts[-1], 100)
        self.assertIn("ready", rt.sync.status()["message"])

    def test_opening_state_is_served_honestly_to_the_page(self):
        app = create_app({**self.config, "CATALOG_CSV": str(self.csv)}); c = app.test_client()
        res = c.post("/api/draw", json={"count": 3}); body = res.get_json()
        self.assertEqual((res.status_code, body["code"]), (503, "catalogue_opening")); self.assertIn("message", body["progress"])
        info = c.get("/api/ocean").get_json(); self.assertEqual((info["mode"], info["total"]), ("opening", 0))

    def test_a_failed_sync_is_reported_and_starter_shelf_is_the_honest_fallback(self):
        import urllib.error
        def down(req, timeout): raise urllib.error.URLError("no internet")
        rt = self.make(SYNC_KWARGS={"opener": down})
        self.assertFalse(rt.sync.run())
        st = rt.sync.status(); self.assertEqual(st["state"], "failed"); self.assertIn("could not be reached", st["error"])
        self.assertEqual(rt.mode("en"), "starter")

    def test_a_shipped_seed_index_opens_the_ocean_instantly(self):
        import universe
        universe.build_from_records(catalogue_csv.read_records(self.csv), self.dir / "seed.sqlite", log=lambda *a: None)
        rt = self.make(UNIVERSE_SEED_PATH=self.dir / "seed.sqlite")
        self.assertEqual((rt.mode("en"), rt.universe.count("en")), ("ocean", 40))

    def test_stale_catalogue_is_refreshed_in_the_background_and_fresh_one_is_not(self):
        rt = self.make(CATALOG_CSV=str(self.csv)); rt.sync.run()
        self.assertFalse(rt.sync.needs_run())
        rt.sync._clock = lambda: time.time() + 3 * 86400
        self.assertTrue(rt.sync.needs_run())

    def test_two_syncs_at_once_do_one_download(self):
        rt = self.make(CATALOG_CSV=str(self.csv))
        release = rt.sync._claim(); self.assertIsNotNone(release)
        self.assertFalse(rt.sync.run()); release()


class OceanBase(unittest.TestCase):
    """The whole flow on a synthetic Gutenberg catalogue: random picks, polling, waves, shedding."""
    EN, FR, ZH = 800, 60, 30
    SETTINGS = dict(min_interval=0, first_wave=2, max_wave=6, library_target=10)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); tmp = Path(self.tmp.name)
        folder = tmp / "texts"; books = []; manifest = {}
        for i in range(3):
            _t, manifest = install_text_book(folder, f"seed{i}", fake_gutenberg(f"Seed {i}", seed=i + 1, paragraphs=80))
            books.append({"id": f"seed{i}", "title": f"Seed {i}", "author": "Ann", "rights_status": "approved", "language": "en",
                          "gutenberg_id": 100 + i, "text_file": f"seed{i}.txt"})
        self.csv = write_catalog_csv(tmp / "c.csv.gz", en=self.EN, fr=self.FR, zh=self.ZH)
        self.fetched = []
        def fetcher(book):
            self.fetched.append(book["gutenberg_id"])
            return source.clean_for_language(fake_gutenberg(book["title"], seed=book["gutenberg_id"], paragraphs=60), "en")
        self.config = dict(TESTING=True, INSTANCE_DIR=tmp / "inst", DB_PATH=tmp / "a.db", BOOKS=books, EN_TEXTS_DIR=folder, TEXTS_DIR=folder,
                           DATA_DIR=tmp / "data", RNG=random.Random(11), EMBEDDED_WORKER=False, CATALOG_CSV=str(self.csv),
                           FETCHER=fetcher, PREP_SETTINGS=dict(self.SETTINGS))
        self.app = create_app(self.config); self.client = self.app.test_client(); self.rt = self.app.runtime
        self.rt.worker.rng = random.Random(5)
        self.rt.worker.seed_local(); self.rt.worker.run_pending()
        self.assertTrue(self.rt.sync.run())

    def post(self, path, **body):
        return self.client.post(path, json=body).get_json()


class OceanTests(OceanBase):
    def test_first_draw_is_a_fresh_random_pick_not_a_starter_book(self):
        res = self.post("/api/draw", count=3)
        self.assertEqual(res["mode"], "ocean"); self.assertEqual(res["tickets"], []); self.assertEqual(len(res["pending"]), 3)
        handles = [p["handle"] for p in res["pending"]]
        first = self.post("/api/draw/poll", handles=handles)["results"]
        self.assertTrue(all(r["state"] == "working" and 0 < r["percent"] < 100 for r in first))
        self.rt.worker.run_pending()
        done = self.post("/api/draw/poll", handles=handles)["results"]
        self.assertEqual([r["state"] for r in done], ["ready"] * 3)
        self.assertTrue(all(len(r["ticket"]["text"]) > 100 for r in done))
        self.assertEqual(len(set(self.fetched)), 3); self.assertFalse(set(self.fetched) & {100, 101, 102})

    def test_handles_and_draw_numbers_do_not_reveal_the_book(self):
        res = self.post("/api/draw", count=3); blob = json.dumps(res)
        for p in res["pending"]:
            self.assertNotRegex(blob, r"Title \d+"); self.assertLessEqual(p["draw_number"], p["total"])
        self.rt.worker.run_pending()
        for r in self.post("/api/draw/poll", handles=[p["handle"] for p in res["pending"]])["results"]:
            self.assertNotRegex(json.dumps(r["ticket"]), r"Title \d+")

    def test_picks_span_the_whole_catalogue_not_a_small_shelf(self):
        gids = set()
        for _ in range(60):
            for p in self.post("/api/draw", count=5)["pending"]:
                gids.add(p["draw_number"])
        self.assertGreater(len(gids), 150)                                      # of 800 books, in 60 draws

    def test_a_book_prepared_once_is_instant_for_the_next_reader(self):
        record = self.rt.universe.random_candidates("en", 1, random.Random(3))[0]
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        before = len(self.fetched)
        ready = self.rt.ready_book_id("en", record["gutenberg_id"])
        self.assertEqual(ready, record["id"])
        self.assertTrue(self.rt.shelf.has("en", ready)); self.assertEqual(len(self.fetched), before)

    def test_library_grows_in_waves_then_stops_at_its_target(self):
        queued = []
        for _ in range(12):
            queued.append(self.rt.worker.replenish()); self.rt.worker.run_pending()
        self.assertEqual(len(self.rt.worker.lake_ids("en")), 10)                 # library_target
        self.assertEqual(self.rt.worker.replenish(), 0 + self.rt.worker.replenish() * 0)
        self.assertEqual(len(self.fetched), len(set(self.fetched)))              # no edition fetched twice
        self.assertGreater(queued[0], 0)

    def test_overload_swaps_cold_picks_for_random_prepared_books(self):
        for _ in range(3):                                                        # put a few books in the library first
            self.post("/api/draw", count=3); self.rt.worker.run_pending()
        self.assertGreaterEqual(len(self.rt.lake("en")), 6)
        self.rt.worker.settings.max_cold_wait = 0.0; self.rt.worker.settings.min_interval = 5.0
        before = len(self.fetched)
        for _ in range(10):
            self.rt.worker.enqueue(prep.COLD, "en", self.rt.universe.random_candidates("en", 1, random.Random(_ + 50))[0], 5)
        res = self.post("/api/draw", count=3)
        self.assertGreaterEqual(res["shed"], 1); self.assertGreaterEqual(len(res["tickets"]), 1)
        self.assertTrue(all(t["text"] for t in res["tickets"]))

    def test_estimated_wait_follows_the_queue(self):
        w = self.rt.worker; w.settings.min_interval = 2.0
        self.assertEqual(w.estimated_wait(), 0)
        for i in range(5):
            w.enqueue(prep.COLD, "en", self.rt.universe.random_candidates("en", 1, random.Random(i))[0], 5)
        self.assertEqual(w.estimated_wait(), 10.0)

    def test_the_library_is_trimmed_by_age_only_when_it_outgrows_its_target(self):
        w = self.rt.worker; w.settings.library_target, w.settings.max_wave = 3, 0
        for i in range(6):
            self.rt.shelf.publish("en", f"pg-en-{900 + i}", ["p"], edition_key="e", content_hash=str(i) * 64, segment_version="v",
                                  external=True, book={"id": f"pg-en-{900 + i}", "title": "t", "text_url": "x", "rights_status": "approved"})
            self.rt.shelf._index["en"][f"pg-en-{900 + i}"]["prepared_at"] = 1000 + i
        self.rt.shelf.note_interest("en", "pg-en-900", "saved")                  # interest must not keep the oldest
        w.maintenance()
        self.assertEqual(sorted(w.lake_ids("en")), ["pg-en-903", "pg-en-904", "pg-en-905"])

    def test_one_cold_book_wanted_by_many_visitors_is_fetched_once(self):
        record = self.rt.universe.random_candidates("en", 1, random.Random(1))[0]
        results = [self.rt.worker.enqueue(prep.COLD, "en", record, 0) for _ in range(100)]
        self.assertEqual(results.count(True), 1)
        self.rt.worker.run_pending(); self.assertEqual(self.fetched.count(record["gutenberg_id"]), 1)

    def test_a_failed_pick_is_reported_so_the_page_can_draw_again(self):
        self.rt.worker._fetch = lambda b: (_ for _ in ()).throw(source.PermanentSourceError("HTTP 404"))
        res = self.post("/api/draw", count=2); self.rt.worker.run_pending()
        polled = self.post("/api/draw/poll", handles=[p["handle"] for p in res["pending"]])["results"]
        self.assertEqual([r["state"] for r in polled], ["failed", "failed"])
        again = self.post("/api/draw", count=2, seen=[p["handle"] for p in res["pending"]])
        self.assertEqual(len(again["pending"]), 2)

    def test_ocean_endpoint_reports_the_whole_catalogue_and_library(self):
        info = self.client.get("/api/ocean").get_json()
        self.assertEqual((info["mode"], info["total"]), ("ocean", 800)); self.assertEqual(info["totals"]["fr"], 60)
        self.assertEqual(info["catalogue"]["state"], "ready")

    def test_history_is_capped_at_75(self):
        self.assertIn("HISTORY_LIMIT = 75", Path("static/js/storage.js").read_text(encoding="utf-8"))

    def test_stage_progress_never_goes_backwards(self):
        job = prep.Job("k", prep.COLD, "en", {"id": "x"}, 0); w = self.rt.worker
        w._set_stage(job, "fetching", 12); w._set_stage(job, "fetching", 40); w._set_stage(job, "fetching", 30)
        self.assertEqual(job.percent, 40)
        w._set_stage(job, "cleaning", 68); w._set_stage(job, "indexing", 82)
        self.assertEqual((job.stage, job.percent), ("indexing", 82))


class SmallCatalogueTests(OceanBase):
    EN, FR, ZH = 6, 4, 4
    SETTINGS = dict(min_interval=0, library_target=50)

    def test_once_every_book_is_prepared_every_spin_is_instant_and_touches_no_source(self):
        for _ in range(3):
            self.post("/api/draw", count=5); self.rt.worker.run_pending()
        for record in self.rt.universe.random_candidates("en", 6, random.Random(1)):
            self.rt.worker.enqueue(prep.COLD, "en", record, 0)
        self.rt.worker.run_pending()
        before = len(self.fetched)
        for _ in range(15):
            res = self.post("/api/draw", count=3)
            self.assertEqual(res["pending"], []); self.assertGreaterEqual(len(res["tickets"]), 2)
        self.assertEqual(len(self.fetched), before)


class OfflineTests(OceanBase):
    def test_if_the_catalogue_cannot_be_opened_the_starter_shelf_is_the_honest_fallback(self):
        self.rt.universe_path.unlink(); self.rt.universe.reload(force=True)
        self.rt.sync._set(state="failed", running=False, error="no internet", message="The catalogue could not be opened right now.")
        info = self.client.get("/api/ocean").get_json(); self.assertEqual(info["mode"], "starter")
        res = self.post("/api/draw", count=3); self.assertEqual(res["mode"], "starter"); self.assertEqual(len(res["tickets"]), 3)


class SourceTests(unittest.TestCase):
    class Resp:
        def __init__(self, body, ctype="text/plain", length=True):
            import email.message
            self._b, self._pos = body, 0
            self.headers = email.message.Message(); self.headers["Content-Type"] = ctype
            if length:
                self.headers["Content-Length"] = str(len(body))
        def read(self, n=-1):
            chunk = self._b[self._pos:self._pos + n] if n and n > 0 else self._b[self._pos:]; self._pos += len(chunk); return chunk
        def __enter__(self): return self
        def __exit__(self, *a): return False

    URL = "https://www.gutenberg.org/cache/epub/1/pg1.txt"

    def test_download_reports_real_progress(self):
        seen = []
        raw = source.download(self.URL, 1, opener=lambda req, timeout: self.Resp(b"x" * 200_000), progress=seen.append)
        self.assertEqual(len(raw), 200_000); self.assertEqual(seen[-1], 1.0); self.assertEqual(seen, sorted(seen))

    def test_errors_are_classified_for_the_worker(self):
        import urllib.error
        def http(code):
            def opener(req, timeout):
                raise urllib.error.HTTPError(self.URL, code, "x", {}, None)
            return opener
        with self.assertRaises(source.TransientSourceError): source.download(self.URL, 1, opener=http(503))
        with self.assertRaises(source.PermanentSourceError): source.download(self.URL, 1, opener=http(404))
        with self.assertRaises(source.PermanentSourceError): source.download("https://evil.example.com/a.txt", 1)
        with self.assertRaises(source.PermanentSourceError):
            source.download(self.URL, 1, opener=lambda req, timeout: self.Resp(b"<html>nope</html>", "text/html"))


class SourceFallbackTests(unittest.TestCase):
    def test_each_known_address_is_tried_and_only_all_missing_gives_up_for_good(self):
        text = fake_gutenberg(paragraphs=60); tried = []
        book = {"language": "en", "gutenberg_id": 7, "text_urls": ["https://dev.gutenberg.org/a.txt", "https://www.gutenberg.org/b.txt"]}
        def dl(url, gid, **kw):
            tried.append(url)
            if "dev." in url:
                raise source.PermanentSourceError("HTTP 404")
            return text.encode("utf-8")
        self.assertGreater(len(source.fetch_clean_text(book, downloader=dl)), 500); self.assertEqual(len(tried), 2)
        def none(url, gid, **kw): raise source.PermanentSourceError("HTTP 404")
        with self.assertRaises(source.PermanentSourceError): source.fetch_clean_text(book, downloader=none)
        def busy(url, gid, **kw): raise source.TransientSourceError("HTTP 503")
        with self.assertRaises(source.TransientSourceError): source.fetch_clean_text(book, downloader=busy)


class FrontEndTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_javascript_logic(self):
        for script in ("front_tests.mjs", "storage_tests.mjs"):
            out = subprocess.run(["node", f"tests/js/{script}"], capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
