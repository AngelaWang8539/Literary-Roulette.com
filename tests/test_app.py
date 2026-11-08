"""Run all tests with:  python -m unittest discover -s tests -v"""
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

import analytics
from app import create_app
from catalogue import Catalogue, valid_https_url


def make_books():
    return [
        {"id": "A", "title": "Alpha Tale", "author": "Ann Author", "genres": ["Mystery"], "description": "About alpha.",
         "rights_status": "approved", "links": {"read": "https://books.example.org/alpha", "borrow": "http://insecure.example.org/x", "buy": "not a url"}},
        {"id": "B", "title": "Beta Tale", "author": "Bob Writer", "genres": [], "description": "About beta.", "rights_status": "approved"},
        {"id": "C", "title": "Gamma Tale", "author": "Cy Pen", "genres": [], "description": "About gamma.", "rights_status": "approved"},
    ]


def make_passages(a=3, b=20, c=1):
    out = []
    for bid, n in (("A", a), ("B", b), ("C", c)):
        for i in range(n):
            out.append({"id": f"{bid}{i}", "book_id": bid, "text": f"Passage {bid}{i} unique-text", "rights_status": "approved",
                        "approved_for_public": True, "source": {"edition": "Test ed.", "location": f"Ch {i}"}})
    return out


class Base(unittest.TestCase):
    def build(self, books=None, passages=None, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = dict(TESTING=True, INSTANCE_DIR=Path(self.tmp.name), DB_PATH=Path(self.tmp.name) / "a.db",
                      BOOKS=books if books is not None else make_books(),
                      PASSAGES=passages if passages is not None else make_passages(),
                      RNG=random.Random(1), ALLOW_DEMO=False)
        config.update(cfg)
        self.app = create_app(config)
        self.client = self.app.test_client()
        self.db = config["DB_PATH"]
        return self.client

    def post(self, path, **body):
        return self.client.post(path, json=body).get_json()

    def start(self):
        return self.client.get("/api/passage/start").get_json()


class RouteTests(Base):
    def test_language_bar_is_reserved_but_not_a_working_catalogue_switch(self):
        self.build()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="language-bar"', html)
        self.assertIn('<html lang="en">', html)
        self.assertIn("Layout reserved for the future multilingual-catalogue patch", html)
        self.assertIn("Only English works in this version", html)

    def test_start_is_anonymous_and_page_hides_books(self):
        self.build()
        raw = self.client.get("/api/passage/start").get_data(as_text=True)
        data = json.loads(raw)
        self.assertTrue({"ok", "token", "book_token", "text", "content_warning", "spoiler_level"}.issubset(data))
        self.assertEqual(data["language"], "en")
        self.assertEqual(data["language_label"], "English")
        for secret in ("Alpha", "Beta", "Gamma", "Ann Author", "Mystery", "Test ed."):
            self.assertNotIn(secret, raw)
        html = self.client.get("/").get_data(as_text=True)
        for secret in ("Alpha", "Beta", "Gamma", "unique-text"):
            self.assertNotIn(secret, html)

    def test_another_passage_same_book_and_different(self):
        self.build()
        first = self.start()
        title = self.post("/api/book/reveal", token=first["token"])["book"]["title"]
        cur = first
        for _ in range(2):
            nxt = self.post("/api/passage/another", token=cur["token"])
            self.assertNotEqual(nxt["text"], cur["text"])
            self.assertEqual(self.post("/api/book/reveal", token=nxt["token"])["book"]["title"], title)
            cur = nxt

    def test_no_repeat_until_book_exhausted(self):
        self.build(passages=make_passages(a=4, b=4, c=4))
        cur = self.start()
        seen = {cur["text"]}
        for _ in range(3):
            cur = self.post("/api/passage/another", token=cur["token"])
            seen.add(cur["text"])
        self.assertEqual(len(seen), 4)

    def test_reveal_keeps_passage_and_survives_another_passage(self):
        self.build()
        first = self.start()
        rev = self.post("/api/book/reveal", token=first["token"])
        self.assertNotIn("text", rev)
        self.assertEqual(rev["token"], first["token"])
        nxt = self.post("/api/passage/another", token=first["token"])
        self.assertIn("book", nxt)  # already revealed -> details stay available
        self.assertEqual(nxt["book"]["title"], rev["book"]["title"])
        self.post("/api/event", token=nxt["token"], event="book_hidden")
        again = self.post("/api/passage/another", token=nxt["token"])
        self.assertNotIn("book", again)

    def test_spin_changes_book_and_hides_identity(self):
        self.build()
        cur = self.start()
        title = self.post("/api/book/reveal", token=cur["token"])["book"]["title"]
        for _ in range(6):
            spun = self.post("/api/book/spin", token=cur["token"])
            self.assertTrue(spun["ok"])
            self.assertNotIn("book", spun)
            new_title = self.post("/api/book/reveal", token=spun["token"])["book"]["title"]
            self.assertNotEqual(new_title, title)
            self.post("/api/event", token=spun["token"], event="book_hidden")
            cur, title = spun, new_title

    def test_spin_cycle_restarts_with_notice(self):
        self.build()
        cur, notices = self.start(), 0
        for _ in range(5):
            cur = self.post("/api/book/spin", token=cur["token"])
            notices += 1 if cur.get("notice") else 0
        self.assertGreaterEqual(notices, 1)

    def test_single_passage_book_message(self):
        self.build(books=make_books()[:1], passages=make_passages(a=1, b=0, c=0))
        cur = self.start()
        res = self.post("/api/passage/another", token=cur["token"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "no_other_passage")
        self.assertTrue(self.post("/api/book/reveal", token=cur["token"])["ok"])  # other actions still work
        self.assertEqual(self.post("/api/book/spin", token=cur["token"])["code"], "only_one_book")

    def test_links_filtered_and_tracked(self):
        self.build(passages=make_passages(a=2, b=0, c=0), books=make_books()[:1])
        cur = self.start()
        links = self.post("/api/book/reveal", token=cur["token"])["book"]["links"]
        self.assertEqual([l["kind"] for l in links], ["read"])  # insecure/invalid ones dropped
        resp = self.client.get(links[0]["href"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "https://books.example.org/alpha")
        self.assertEqual(self.client.get(f"/go/buy/{cur['token']}").status_code, 404)
        self.assertEqual(self.client.get("/go/read/nonsense").status_code, 404)
        self.assertEqual(analytics.report(self.db)["totals"]["read_link_clicked"], 1)

    def test_book_without_links_has_empty_list(self):
        self.build(books=make_books()[1:2], passages=make_passages(a=0, b=2, c=0))
        cur = self.start()
        self.assertEqual(self.post("/api/book/reveal", token=cur["token"])["book"]["links"], [])

    def test_bad_input_and_empty_catalogue(self):
        self.build()
        self.assertEqual(self.client.post("/api/passage/another", json={"token": "zzz"}).status_code, 400)
        self.assertEqual(self.client.post("/api/book/reveal", data="not json").status_code, 400)
        self.assertEqual(self.client.post("/api/event", json={"token": "x", "event": "x"}).status_code, 400)
        self.build(books=[], passages=[])
        res = self.client.get("/api/passage/start")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()["code"], "empty_catalogue")

    def test_unapproved_content_excluded(self):
        books = make_books()
        books[0]["rights_status"] = "pending"
        passages = make_passages(a=2, b=2, c=0)
        passages[2]["approved_for_public"] = False
        self.build(books=books, passages=passages)
        cat = self.app.catalogue
        self.assertEqual(cat.book_ids, ["B"])
        self.assertEqual(len(cat.passages_by_book["B"]), 1)

    def test_demo_content_only_when_allowed(self):
        demo_book = {"id": "D", "title": "Demo", "author": "Fiction", "is_demo": True, "rights_status": "demo_placeholder"}
        demo_pass = {"id": "D1", "book_id": "D", "text": "x", "rights_status": "demo_placeholder", "approved_for_public": True}
        self.build(books=[demo_book], passages=[demo_pass], ALLOW_DEMO=False)
        self.assertEqual(self.app.catalogue.book_ids, [])
        self.build(books=[demo_book], passages=[demo_pass], ALLOW_DEMO=True)
        self.assertEqual(self.app.catalogue.book_ids, ["D"])


class SelectionTests(unittest.TestCase):
    def test_book_level_fairness(self):
        cat = Catalogue(make_books()[:2], make_passages(a=1, b=30, c=0), "s")
        rng = random.Random(7)
        n = 6000
        hits = sum(cat.pick_first(rng)["book_id"] == "A" for _ in range(n))
        self.assertAlmostEqual(hits / n, 0.5, delta=0.03)  # a passage-level pick would give about 3%

    def test_spin_fairness_among_others(self):
        cat = Catalogue(make_books(), make_passages(a=1, b=30, c=2), "s")
        rng = random.Random(3)
        picks = [cat.pick_other_book("A", set(), rng)[0] for _ in range(4000)]
        self.assertAlmostEqual(picks.count("B") / 4000, 0.5, delta=0.04)

    def test_url_validation(self):
        good = ["https://example.com", "https://shop.example.co.uk/p?id=1"]
        bad = ["http://example.com", "javascript:alert(1)", "https://", "https://localhost", "", None, 5,
               "https://exa mple.com", " https://example.com", "//example.com", "ftp://example.com"]
        self.assertTrue(all(valid_https_url(u) for u in good))
        self.assertFalse(any(valid_https_url(u) for u in bad))

    def test_bad_records_are_reported_not_fatal(self):
        cat = Catalogue([{"id": "X"}, {"id": "Y", "title": "t", "author": "a", "rights_status": "approved"}],
                        [{"id": "p", "book_id": "nope", "text": "t"}, {"id": "q", "book_id": "Y", "text": "  "}], "s")
        self.assertTrue(cat.problems)
        self.assertEqual(cat.book_ids, [])


class AnalyticsTests(Base):
    def test_events_and_report(self):
        self.build(passages=make_passages(a=3, b=3, c=3))
        cur = self.start()
        self.post("/api/passage/another", token=cur["token"])
        self.post("/api/book/reveal", token=cur["token"])
        self.post("/api/book/spin", token=cur["token"])
        totals = analytics.report(self.db)["totals"]
        self.assertEqual(totals["passage_displayed"], 3)
        for name in ("another_passage_requested", "book_revealed", "another_book_requested"):
            self.assertEqual(totals[name], 1)
        columns = [r[1] for r in sqlite3.connect(self.db).execute("PRAGMA table_info(events)")]
        self.assertEqual(columns, ["id", "ts", "event", "book_id", "passage_id", "is_demo"])  # no IP column

    def test_site_works_if_analytics_fails(self):
        blocker = tempfile.TemporaryDirectory()
        self.addCleanup(blocker.cleanup)
        Path(blocker.name, "missing").write_text("a file, so the folder cannot be created")
        self.build(DB_PATH=Path(blocker.name) / "missing" / "dir" / "x.db")
        cur = self.start()
        self.assertTrue(cur["ok"])
        self.assertTrue(self.post("/api/passage/another", token=cur["token"])["ok"])


if __name__ == "__main__":
    unittest.main()
