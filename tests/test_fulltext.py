import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import analytics
import textproc
from catalogue import Catalogue
from tests.fixtures import fake_gutenberg, install_text_book
from tests.test_app import Base

ROOT = Path(__file__).resolve().parent.parent


def text_book(bid, gid, title="Test Tale", familiarity="classic"):
    return {"id": bid, "title": title, "author": "Ann Author", "genres": ["Test"], "description": f"About {title}.",
            "gutenberg_id": gid, "familiarity": familiarity, "rights_status": "pending_verification",
            "content_note": "A test note." if bid == "big" else None}


class FullTextCatalogueTests(Base):
    def build_texts(self, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.texts = Path(self.tmp.name) / "texts"
        self.sizes = {"big": 400, "small": 40}
        for i, (bid, n) in enumerate(self.sizes.items()):
            install_text_book(self.texts, bid, fake_gutenberg(f"Title {bid}", "Ann Author", paragraphs=n, seed=i))
        books = [text_book("big", 1, "Title big"), text_book("small", 2, "Title small", "discovery")]
        return self.build(books=books, passages=[], TEXTS_DIR=self.texts, MANIFEST=None, INSTANCE_DIR=Path(self.tmp.name), **cfg)

    def test_passages_come_from_stored_text_only(self):
        self.build_texts()
        cat = self.app.catalogue
        self.assertEqual(set(cat.book_ids), {"big", "small"})
        self.assertEqual(cat.passages_by_book, {})  # no full texts are loaded at startup
        cat.ensure_book_loaded("big")
        self.assertGreater(len(cat.passages_by_book["big"]), 5)
        self.assertTrue(all(p["id"].startswith("big:v1:") for p in cat.passages_by_book["big"]))

    def test_book_fairness_with_unequal_lengths(self):
        self.build_texts()
        cat, rng, n = self.app.catalogue, random.Random(5), 4000
        small = sum(cat.pick_first(rng)["book_id"] == "small" for _ in range(n))
        self.assertAlmostEqual(small / n, 0.5, delta=0.04)

    def test_api_flow_and_privacy_with_full_text(self):
        self.build_texts()
        raw = self.client.get("/api/passage/start").get_data(as_text=True)
        first = json.loads(raw)
        for secret in ("Title big", "Title small", "Ann Author", "Test", "gutenberg", "big:v1", "small:v1"):
            self.assertNotIn(secret, raw)
        rev = self.post("/api/book/reveal", token=first["token"])["book"]
        self.assertIn(rev["title"], ("Title big", "Title small"))
        self.assertEqual([l["kind"] for l in rev["links"]], ["read"])  # derived from the verified Gutenberg id
        self.assertTrue(rev["source_credit"]["url"].startswith("https://www.gutenberg.org/ebooks/"))
        redirect = self.client.get(rev["links"][0]["href"])
        self.assertEqual(redirect.status_code, 302)
        self.assertTrue(redirect.headers["Location"].startswith("https://www.gutenberg.org/ebooks/"))
        nxt = self.post("/api/passage/another", token=first["token"])
        self.assertNotEqual(nxt["text"], first["text"])
        self.assertEqual(nxt["book_token"], first["book_token"])
        spun = self.post("/api/book/spin", token=nxt["token"])
        self.assertNotEqual(spun["book_token"], first["book_token"])
        self.assertNotIn("book", spun)

    def test_token_is_stable_across_restarts(self):
        self.build_texts()
        first = self.start()
        from app import create_app
        again = create_app(self.app.config.copy()).test_client()
        res = again.post("/api/book/reveal", json={"book_token": first["book_token"]}).get_json()
        self.assertTrue(res["ok"])

    def test_history_passage_token_can_be_rehydrated_after_book_cache_eviction(self):
        self.build_texts()
        first = self.start()
        cat = self.app.catalogue
        cat.max_active_books = 1
        first_book_id = cat.book_of(cat.passage_by_token(first["token"]))["id"]
        other_id = "small" if first_book_id == "big" else "big"
        cat.ensure_book_loaded(other_id)  # evicts the first full-text passage array
        self.assertIsNone(cat.passage_by_token(first["token"]))
        next_passage = self.post("/api/passage/another", token=first["token"], book_token=first["book_token"])
        self.assertTrue(next_passage["ok"])
        self.assertNotEqual(next_passage["text"], first["text"])

    def test_spin_does_not_reload_the_previous_full_text(self):
        self.build_texts()
        first = self.start()
        cat = self.app.catalogue
        first_book_id = cat.book_of(cat.passage_by_token(first["token"]))["id"]
        other_id = "small" if first_book_id == "big" else "big"
        cat.max_active_books = 1
        cat.ensure_book_loaded(other_id)  # evict the currently displayed book
        original = cat.ensure_book_loaded
        def forbid_previous_book(bid):
            if bid == first_book_id:
                raise AssertionError("spin should not reload the previous book")
            return original(bid)
        cat.ensure_book_loaded = forbid_previous_book
        spun = self.post("/api/book/spin", token=first["token"], book_token=first["book_token"])
        self.assertTrue(spun["ok"])
        self.assertNotEqual(spun["book_token"], first["book_token"])

    def test_reveal_is_metadata_only_and_never_restores_the_whole_cache(self):
        self.build_texts()
        first = self.start()
        self.app.gutendex_pool.restore_cached_books = lambda *args: (_ for _ in ()).throw(AssertionError("bulk restore called"))
        result = self.post("/api/book/reveal", book_token=first["book_token"])
        self.assertTrue(result["ok"])
        self.assertIn(result["book"]["title"], ("Title big", "Title small"))

    def test_local_full_text_memory_is_limited_to_two_books(self):
        self.build_texts()
        cat = self.app.catalogue
        for bid in ("big", "small", "big", "small"):
            cat.ensure_book_loaded(bid)
        self.assertLessEqual(len(cat._active_books), 2)
        self.assertLessEqual(len(cat.passages_by_book), 2)

    def test_unverified_or_tampered_text_is_excluded_and_explained(self):
        self.build_texts()
        manifest = json.loads((self.texts / "manifest.json").read_text())
        manifest["small"]["verified"], manifest["small"]["problems"] = False, ["title mismatch"]
        (self.texts / "big.txt").write_text("tampered", encoding="utf-8")
        cat = Catalogue([text_book("big", 1), text_book("small", 2)], [], "s", None, None, self.texts, manifest)
        self.assertEqual(cat.book_ids, [])
        self.assertIn("title mismatch", cat.unavailable["small"])
        self.assertIn("changed since verification", cat.unavailable["big"])

    def test_missing_manifest_means_not_ready(self):
        cat = Catalogue([text_book("big", 1)], [], "s", None, None, Path("nowhere"), {})
        self.assertEqual(cat.book_ids, [])
        self.assertIn("tools/ingest_gutenberg.py", cat.unavailable["big"])

    def test_demo_fallback_only_while_no_real_text(self):
        demo = {"id": "d", "title": "Demo", "author": "X", "is_demo": True, "rights_status": "demo_placeholder"}
        dp = {"id": "d1", "book_id": "d", "text": "x", "rights_status": "demo_placeholder", "approved_for_public": True}
        cat = Catalogue([demo, text_book("big", 1)], [dp], "s", None, None, Path("nowhere"), {})
        self.assertEqual(cat.book_ids, ["d"])
        self.build_texts()
        cat = Catalogue([demo, text_book("big", 1)], [dp], "s", None, None, self.texts, json.loads((self.texts / "manifest.json").read_text()))
        self.assertEqual(cat.book_ids, ["big"])

    def test_new_events_and_group_comparison(self):
        self.build_texts()
        a = self.start()
        self.post("/api/book/reveal", token=a["token"])
        for ev in ("book_saved", "awareness_known"):
            self.assertTrue(self.post("/api/event", token=a["token"], event=ev)["ok"])
        self.assertEqual(self.client.post("/api/event", json={"token": a["token"], "event": "delete_everything"}).status_code, 400)
        totals = analytics.report(self.db)["totals"]
        self.assertEqual((totals["book_saved"], totals["awareness_known"]), (1, 1))

    def test_catalogue_json_has_40_distinct_titles(self):
        books = [b for b in json.loads((ROOT / "data" / "books.json").read_text(encoding="utf-8")) if b.get("gutenberg_id")]
        self.assertEqual(len(books), 40)
        self.assertEqual(sum(b["familiarity"] == "classic" for b in books), 25)
        self.assertEqual(sum(b["familiarity"] == "discovery" for b in books), 15)
        self.assertEqual(len({b["id"] for b in books}), 40)
        self.assertEqual(len({b["gutenberg_id"] for b in books}), 40)
        self.assertTrue(all(b.get("text_start_pattern") for b in books), "each title needs a reviewed story-start marker")
        names = {b["title"] for b in books}
        for must in (
            "The Adventures of Sherlock Holmes", "The Hound of the Baskervilles", "Alice's Adventures in Wonderland",
            "The Great Gatsby", "Pride and Prejudice", "Frankenstein", "Dracula",
            "The Strange Case of Dr. Jekyll and Mr. Hyde", "The Picture of Dorian Gray", "Romeo and Juliet",
            "A Midsummer Night's Dream", "Hamlet", "A Christmas Carol", "The Adventures of Tom Sawyer",
            "The Wonderful Wizard of Oz", "Treasure Island", "Little Women", "Jane Eyre", "The Secret Garden",
            "The Time Machine", "The War of the Worlds", "The Call of the Wild", "Moby-Dick; or, The Whale",
            "Anne of Green Gables", "A Tale of Two Cities", "The Prisoner of Zenda", "The Scarlet Pimpernel",
            "The Moonstone", "The Woman in White", "The Thirty-Nine Steps", "The Man Who Was Thursday",
            "The Lost World", "The Island of Doctor Moreau", "The King in Yellow", "The Mysterious Island",
            "Five Children and It", "The Railway Children", "Flatland: A Romance of Many Dimensions",
            "Raffles: The Amateur Cracksman", "Three Men in a Boat"):
            self.assertIn(must, names)
        self.assertNotIn("The Glass Orchard", names)  # fictional demos live in a separate opt-in file
        for b in books:
            self.assertTrue(b["description"] and b["publication"]["year"] and b["source"]["url"].startswith("https://www.gutenberg.org/ebooks/"))


class IngestToolTests(unittest.TestCase):
    def run_tool(self, data_dir, raw_dir, *extra):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "ingest_gutenberg.py"), "--data-dir", str(data_dir),
                               "--source-dir", str(raw_dir), *extra], capture_output=True, text=True, cwd=ROOT)

    def test_uncertain_story_start_is_not_marked_verified(self):
        # A valid Gutenberg wrapper and matching metadata are not enough if the
        # importer cannot identify where the actual work begins.
        from tools.ingest_gutenberg import verify
        raw = fake_gutenberg("The Good Tale", "Ann Author", paragraphs=120, seed=3)
        book = {"title": "The Good Tale", "author": "Ann Author", "min_words": 2000,
                "text_start_pattern": "THIS PHRASE DOES NOT EXIST IN THE SOURCE"}
        problems, cleaned, info = verify(raw, book)
        self.assertIsNotNone(cleaned)
        self.assertTrue(any("text_start_pattern was not found" in p for p in problems))
        self.assertTrue(any("Story start not located with confidence" in w for w in info["warnings"]))

    def test_verifies_good_files_and_rejects_bad_ones(self):
        # The current importer consumes already-cleaned local text plus an
        # explicit Gutenberg-ID mapping; it does not parse raw Gutenberg
        # headers or infer title/author from downloaded files.
        with tempfile.TemporaryDirectory() as tmp:
            data, raw = Path(tmp) / "data", Path(tmp) / "raw"
            data.mkdir(); raw.mkdir()
            books = [
                {"id": "good", "title": "The Good Tale", "author": "Ann Author", "gutenberg_id": 11,
                 "min_words": 2000, "text_start_pattern": "BEGIN GOOD STORY"},
                {"id": "mapping-mismatch", "title": "A Tale", "author": "Ann Author", "gutenberg_id": 12,
                 "min_words": 2000, "text_start_pattern": "BEGIN MISMATCH STORY"},
                {"id": "missing-marker", "title": "A Tale", "author": "Ann Author", "gutenberg_id": 13,
                 "min_words": 2000, "text_start_pattern": "THIS MARKER IS ABSENT"},
                {"id": "missing-file", "title": "A Tale", "author": "Ann Author", "gutenberg_id": 14,
                 "min_words": 2000, "text_start_pattern": "BEGIN MISSING FILE"},
                {"id": "short", "title": "A Tale", "author": "Ann Author", "gutenberg_id": 15,
                 "min_words": 90000, "text_start_pattern": "BEGIN SHORT STORY"},
            ]
            (data / "books.json").write_text(json.dumps(books))
            long_story = " ".join(["lantern orchard river whisper copper meadow."] * 500)
            short_story = "A few words only."
            sources = {
                "b001.txt": "Opening page\n\nBEGIN GOOD STORY\n\n" + long_story,
                "b002.txt": "BEGIN MISMATCH STORY\n\n" + long_story,
                "b003.txt": "BEGIN SHORT STORY\n\n" + short_story,
            }
            for filename, content in sources.items():
                (raw / filename).write_text(content, encoding="utf-8")
            mapping = {
                "schema_version": 1,
                "books": {
                    "good": {"gutenberg_id": 11, "source_file": "b001.txt"},
                    "mapping-mismatch": {"gutenberg_id": 999, "source_file": "b002.txt"},
                    "missing-marker": {"gutenberg_id": 13, "source_file": "b002.txt"},
                    "missing-file": {"gutenberg_id": 14, "source_file": "absent.txt"},
                    "short": {"gutenberg_id": 15, "source_file": "b003.txt"},
                },
                "not_mapped": {},
            }
            (raw / "book_file_map.json").write_text(json.dumps(mapping), encoding="utf-8")
            out = self.run_tool(data, raw)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            manifest = json.loads((data / "texts" / "manifest.json").read_text())
            self.assertTrue(manifest["good"]["verified"])
            self.assertTrue((data / "texts" / "good.txt").is_file())
            for book_id, phrase in (
                ("mapping-mismatch", "Mapping mismatch"),
                ("missing-marker", "text_start_pattern was not found"),
                ("missing-file", "Local source file is missing"),
                ("short", "requires at least"),
            ):
                self.assertFalse(manifest[book_id]["verified"], book_id)
                self.assertIn(phrase, " ".join(manifest[book_id]["problems"]), book_id)
                self.assertFalse((data / "texts" / f"{book_id}.txt").exists(), book_id)


class JavaScriptTests(unittest.TestCase):
    """Runs tests/js/run_tests.mjs with Node (skipped, clearly, when Node is not installed)."""

    def test_node_suite(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed; browser-side logic was NOT tested")
        with tempfile.TemporaryDirectory() as tmp:
            from tests.js_fixtures import build_fixtures
            build_fixtures(Path(tmp))
            out = subprocess.run([node, str(ROOT / "tests" / "js" / "run_tests.mjs"), tmp], capture_output=True, text=True, cwd=ROOT)
            print(out.stdout[-1500:])
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
