"""Speed and memory guarantees: constant-time spin/reveal, release on spin, non-blocking analytics."""
import json
import random
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analytics
import gutenberg_pool
from app import create_app

PARA = ("It was a quiet evening and the lamps along the street were being lit one by one while the "
        "travellers talked of old roads and older stories. ") * 6


def make_text(gid):
    return "\n\n".join(f"{PARA} Book {gid} section {i}." for i in range(200))


class FastPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        rows = [{"id": 200000 + i, "title": f"Title {i}", "authors": [{"name": f"Author {i}"}],
                 "download_count": i % 50,
                 "formats": {"text/plain; charset=utf-8": f"https://www.gutenberg.org/cache/epub/{200000+i}/pg.txt"}}
                for i in range(3000)]
        catalog = root / "catalog.json"
        catalog.write_text(json.dumps({"filters": {"max_pages": None}, "languages": {"en": rows, "fr": [], "zh": []}}))
        self._orig_fetch = gutenberg_pool.GutendexPool._fetch_text

        def fake_fetch(pool, book):
            return make_text(book["gutenberg_id"])

        gutenberg_pool.GutendexPool._fetch_text = fake_fetch
        self.app = create_app({
            "TESTING": True, "GUTENBERG_CATALOG_PATH": catalog, "GUTENBERG_CACHE_DIR": root / "cache",
            "INSTANCE_DIR": root / "instance", "DB_PATH": root / "a.db", "AUTO_REFRESH_CATALOGUE": False,
            "RNG": random.Random(3), "BOOKS": [], "PASSAGES": []})
        self.client = self.app.test_client()

    def tearDown(self):
        gutenberg_pool.GutendexPool._fetch_text = self._orig_fetch
        self.tmp.cleanup()

    def post(self, path, cur):
        return self.client.post(path, json={"token": cur["token"], "book_token": cur["book_token"]}).get_json()

    def test_selection_index_is_built_once_and_spin_does_not_rescan_the_catalogue(self):
        cat, pool = self.app.catalogue, self.app.gutendex_pool
        calls = []
        real = cat.token
        cat.token = lambda kind, ident: (calls.append(1), real(kind, ident))[1]
        cur = self.client.get("/api/passage/start").get_json()
        for _ in range(5):
            cur = self.post("/api/book/spin", cur)
            self.assertTrue(cur["ok"])
        # Per spin: a few passage tokens for the newly loaded book - never one hash per catalogue entry.
        per_spin = len(calls) / 6
        self.assertLess(per_spin, 1500)
        self.assertEqual(len(pool._rows_cache["en"][0]), 3000)

    def test_reveal_after_restart_needs_no_text_and_no_cache_scan(self):
        cur = self.client.get("/api/passage/start").get_json()
        cache_reads = []
        orig = gutenberg_pool.GutendexPool._fetch_text
        gutenberg_pool.GutendexPool._fetch_text = lambda *a: cache_reads.append(1) or orig(*a)
        try:
            fresh = create_app({
                "TESTING": True, "GUTENBERG_CATALOG_PATH": self.app.gutendex_pool.catalog_path,
                "GUTENBERG_CACHE_DIR": self.app.gutendex_pool.cache_dir, "INSTANCE_DIR": self.app.config["INSTANCE_DIR"],
                "DB_PATH": self.app.config["DB_PATH"], "AUTO_REFRESH_CATALOGUE": False, "BOOKS": [], "PASSAGES": []})
            started = time.perf_counter()
            data = fresh.test_client().post("/api/book/reveal", json={"book_token": cur["book_token"]}).get_json()
            elapsed = time.perf_counter() - started
        finally:
            gutenberg_pool.GutendexPool._fetch_text = orig
        self.assertTrue(data["ok"])
        self.assertTrue(data["book"]["title"].startswith("Title "))
        self.assertEqual(cache_reads, [])
        self.assertLess(elapsed, 0.25)

    def test_spinning_releases_the_previous_whole_book(self):
        cat = self.app.catalogue
        cur = self.client.get("/api/passage/start").get_json()
        first_book = cat.passage_by_token(cur["token"])["book_id"]
        for _ in range(6):
            cur = self.post("/api/book/spin", cur)
            self.assertTrue(cur["ok"])
        loaded = {p["book_id"] for p in cat.passages.values()}
        self.assertEqual(loaded, {cat.passage_by_token(cur["token"])["book_id"]})
        self.assertNotIn(first_book, cat.books)           # its record, id and token are gone too
        self.assertLessEqual(len(cat._active_books), 1)
        # A reveal for the book we left still works from metadata alone.
        self.assertTrue(self.client.post("/api/book/reveal", json={"book_token": cur["book_token"]}).get_json()["ok"])

    def test_disk_cache_default_is_small(self):
        pool = self.app.gutendex_pool
        self.assertLessEqual(pool.max_cache_books, 6)
        self.assertLessEqual(pool.max_cache_bytes, 32 * 1024 * 1024)


class AnalyticsWriterTests(unittest.TestCase):
    def test_background_writer_saves_every_event_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "events.db"
            analytics.init_db(db)
            analytics.start_writer(db)
            try:
                started = time.perf_counter()
                for i in range(300):
                    self.assertTrue(analytics.log_event(db, "book_revealed", f"b{i % 5}", None, False))
                queued_in = time.perf_counter() - started
                analytics.flush()
            finally:
                analytics.stop_writer()
            self.assertLess(queued_in, 0.5)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 300)
            self.assertEqual(analytics.report(db)["totals"]["book_revealed"], 300)

    def test_without_a_writer_events_are_saved_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "events.db"
            analytics.init_db(db)
            self.assertTrue(analytics.log_event(db, "passage_displayed", "b1", "p1", False))
            self.assertEqual(analytics.report(db)["totals"]["passage_displayed"], 1)


if __name__ == "__main__":
    unittest.main()
