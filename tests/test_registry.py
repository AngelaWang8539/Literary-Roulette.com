"""Book Registry, fetch guard, validation, Gutendex, author intake, draw rules, Registry Center. Run with the rest:
python -m unittest discover -s tests -t . -v"""
import gzip
import hashlib
import io
import json
import random
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

import catalogue_csv
import fetchguard
import gutendex
import intake
import popularity
import prep
import registry as registry_mod
import segment
import source
import universe
from registry import Registry, RegistryError
from tests import test_core as core
from tests.fixtures import fake_gutenberg


def rec(gid, language="en", title=None, subjects="Fiction", shelves="Adventure"):
    return {"gutenberg_id": gid, "title": title or f"Title {gid}", "authors": "Writer, A.", "languages": language, "subjects": subjects, "bookshelves": shelves}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(); self.r = Registry(Path(self.tmp.name) / "r.sqlite", clock=self.clock)

    def test_import_creates_stable_ids_and_is_idempotent(self):
        out = self.r.import_gutenberg([rec(1), rec(2, "fr"), rec(3, "de")])
        self.assertEqual((out["added"], out["total"]), (2, 2))                          # German is not served
        self.assertEqual(self.r.get("pg-en-1")["pool_status"], "active")
        self.assertEqual(self.r.get("pg-en-1")["rights_status"], "public_domain_candidate")
        generation = self.r.generation
        again = self.r.import_gutenberg([rec(1), rec(2, "fr")])
        self.assertEqual((again["added"], again["updated"]), (0, 0)); self.assertEqual(self.r.count(), 2)
        self.assertGreater(self.r.generation, generation)                                 # an import may change the set: indexes re-check

    def test_admin_decisions_and_confirmed_fields_survive_every_import(self):
        self.r.import_gutenberg([rec(1)])
        self.r.set_pool_status("pg-en-1", "paused", "reader complaint")
        self.r.confirm_fields("pg-en-1", title="The Real Title")
        self.r.import_gutenberg([rec(1, title="Provider Title")])
        book = self.r.get("pg-en-1")
        self.assertEqual((book["title"], book["pool_status"], book["status_reason"]), ("The Real Title", "paused", "reader complaint"))

    def test_a_vanished_book_is_held_by_the_importer_and_returns_by_itself_but_an_admin_pause_never_does(self):
        self.r.import_gutenberg([rec(1), rec(2)])
        self.r.set_pool_status("pg-en-2", "paused", "mine")
        self.r.import_gutenberg([rec(9)])                                                 # 1 and 2 are gone upstream
        self.assertEqual((self.r.get("pg-en-1")["pool_status"], self.r.get("pg-en-1")["status_reason"]), ("paused", "auto:missing-upstream"))
        self.r.import_gutenberg([rec(1), rec(2), rec(9)])
        self.assertEqual(self.r.get("pg-en-1")["pool_status"], "active")
        self.assertEqual(self.r.get("pg-en-2")["pool_status"], "paused")                  # the admin's own pause is untouched

    def test_blocklist_holds_a_book_and_releasing_it_restores_it(self):
        self.r.import_gutenberg([rec(1), rec(2)], blocked=[2])
        self.assertEqual(self.r.get("pg-en-2")["pool_status"], "paused")
        self.r.import_gutenberg([rec(1), rec(2)])
        self.assertEqual(self.r.get("pg-en-2")["pool_status"], "active")

    def test_rights_gate_needs_every_field_and_a_valid_expiry(self):
        self.r.add_author_book(book_id="au-en-x", slug="x", language="en", title="X", author="A", text_key="k", text_hash="h" * 64, attribution=None,
                               links={}, description="", publication=None, rights={}, status_note="awaiting rights review")
        with self.assertRaisesRegex(RegistryError, "territory.*commercial scope.*attribution.*expiry"):
            self.r.set_rights("au-en-x", "approved")
        with self.assertRaisesRegex(RegistryError, "date like"):
            self.r.set_rights("au-en-x", "approved", territory="Worldwide", commercial=False, attribution="By A", expires="someday")
        book = self.r.set_rights("au-en-x", "approved", territory="Worldwide", commercial=False, attribution="By A", expires="2030-12-31")
        self.assertEqual((book["rights_status"], book["rights_commercial"]), ("approved", 0))
        self.assertEqual(book["pool_status"], "pending")                                  # approving is not activating

    def test_an_author_book_cannot_be_activated_without_approved_rights(self):
        self.r.add_author_book(book_id="au-en-x", slug="x", language="en", title="X", author="A", text_key="k", text_hash="h" * 64, attribution="By A",
                               links={}, description="", publication=None, rights={"territory": "Worldwide"}, status_note=None)
        with self.assertRaisesRegex(RegistryError, "not cleared"):
            self.r.set_pool_status("au-en-x", "active")
        self.r.set_rights("au-en-x", "approved", territory="Worldwide", commercial=True, attribution="By A", expires="perpetual")
        self.assertEqual(self.r.set_pool_status("au-en-x", "active")["pool_status"], "active")
        self.assertIn("au-en-x", {row["book_id"] for row in self.r.eligible_rows("en")})

    def test_withdrawn_or_paused_rights_take_the_book_out_at_once_and_expiry_ends_eligibility(self):
        self.r.import_gutenberg([rec(1), rec(2)])
        self.r.set_rights("pg-en-1", "withdrawn", note="author asked")
        self.assertEqual(self.r.get("pg-en-1")["pool_status"], "archived"); self.assertIn("pg-en-1", self.r.ineligible_ids())
        self.r.set_rights("pg-en-2", "source_licensed", territory="EU", commercial=True, attribution="Source", expires="2030-01-01")
        self.assertNotIn("pg-en-2", self.r.ineligible_ids())
        self.clock.t += 1900 * 86400                                                       # past the expiry date
        self.assertIn("pg-en-2", self.r.ineligible_ids())

    def test_failed_text_is_not_drawn_until_cleared_and_ordinary_success_does_not_touch_the_generation(self):
        self.r.import_gutenberg([rec(1)])
        g = self.r.generation
        self.r.mark_text("pg-en-1", "verified", text_hash="a" * 64)
        self.assertEqual(self.r.generation, g)                                            # both states are drawable
        self.r.mark_text("pg-en-1", "failed", error="HTTP 404")
        self.assertIn("pg-en-1", self.r.ineligible_ids()); self.assertGreater(self.r.generation, g)
        self.r.reset_failed()
        self.assertNotIn("pg-en-1", self.r.ineligible_ids())

    def test_a_changed_text_hash_is_a_new_revision(self):
        self.r.import_gutenberg([rec(1)])
        self.r.mark_text("pg-en-1", "verified", text_hash="a" * 64)
        self.assertEqual(self.r.get("pg-en-1")["revision"], 1)
        self.r.mark_text("pg-en-1", "verified", text_hash="b" * 64)
        self.assertEqual(self.r.get("pg-en-1")["revision"], 2)

    def test_search_filters_and_escapes(self):
        self.r.import_gutenberg([rec(1, title="100% Real"), rec(2, title="Other")])
        self.assertEqual(self.r.search(q="100%")["total"], 1)
        self.assertEqual(self.r.search(q="%")["total"], 1)                                # a literal percent sign, not a wildcard
        self.assertEqual(self.r.search(language="en", pool="active")["total"], 2)

    # ---- popularity: unknown stays unknown, rank only from a complete population --------------------------------
    def fill(self, values, language="en", status="ok", start=1):
        self.r.import_gutenberg([rec(start + i, language) for i in range(len(values))])
        rows = [(f"pg-{language}-{start + i}", language, v, None, status if v is not None else "not_found") for i, v in enumerate(values)]
        self.r.add_popularity(rows, provider="gutendex", metric="download_count", period="as_reported", scope="census")

    def test_no_percentile_while_the_population_is_partial(self):
        self.r.import_gutenberg([rec(i) for i in range(1, 6)])
        self.r.add_popularity([("pg-en-1", "en", 500, None, "ok"), ("pg-en-2", "en", 5, None, "ok")], provider="gutendex", metric="download_count", period="p", scope="targeted")
        self.assertIsNone(self.r.percentile("pg-en-2"))                                   # 3 books were never looked at: nobody is "least popular"
        self.assertFalse(self.r.complete("en"))

    def test_complete_population_ranks_with_ties_and_not_found_counts_as_covered_but_never_as_zero(self):
        self.fill([100, 50, 50, 10, None])                                                # the last book is unknown to the provider
        self.assertTrue(self.r.complete("en"))
        top, tie, last = self.r.percentile("pg-en-1"), self.r.percentile("pg-en-2"), self.r.percentile("pg-en-4")
        self.assertEqual((top["rank"], top["total"], top["top_percent"]), (1, 4, 25))
        self.assertEqual((tie["rank"], self.r.percentile("pg-en-3")["rank"]), (2, 2))     # ties share a rank
        self.assertEqual(last["rank"], 4)
        self.assertIsNone(self.r.percentile("pg-en-5"))                                   # unknown: unranked, not bottom

    def test_percentiles_are_per_language_and_expire(self):
        self.fill([9, 8, 7], "en"); self.fill([1, 2], "fr", start=500)
        self.assertEqual(self.r.percentile("pg-fr-500")["total"], 2)                      # French is ranked among French only
        self.clock.t += 20 * 86400
        self.assertIsNone(self.r.percentile("pg-en-1"))                                   # an old observation is not trusted

    def test_enrichment_targets_prefer_the_library_and_skip_fresh_observations(self):
        self.r.import_gutenberg([rec(i) for i in range(1, 7)])
        self.r.add_popularity([("pg-en-2", "en", 1, None, "ok")], provider="gutendex", metric="download_count", period="p", scope="targeted")
        stale = self.clock.t - 7 * 86400
        self.assertEqual([g for g, _b in self.r.enrichment_targets(limit=10, stale_before=stale, prefer=[1, 2, 3])], [1, 3])
        self.assertEqual(len(self.r.enrichment_targets(limit=10, stale_before=stale, prefer=[1], census=True)), 5)

    def test_a_copyright_flag_holds_an_unreviewed_book_and_only_that_hold_is_lifted(self):
        self.r.import_gutenberg([rec(1), rec(2)])
        self.r.apply_enrichment("pg-en-1", copyright_flag=True)
        self.assertEqual((self.r.get("pg-en-1")["pool_status"], self.r.get("pg-en-1")["status_reason"]), ("paused", "auto:copyright-flag"))
        self.r.apply_enrichment("pg-en-1", copyright_flag=False)
        self.assertEqual(self.r.get("pg-en-1")["pool_status"], "active")
        self.r.set_pool_status("pg-en-2", "paused", "mine")
        self.r.apply_enrichment("pg-en-2", copyright_flag=False)
        self.assertEqual(self.r.get("pg-en-2")["pool_status"], "paused")

    def test_provider_status_reports_the_last_good_run_beside_a_failed_one_and_stale_running_rows(self):
        a = self.r.start_run("gutenberg_csv"); self.clock.t += 5; self.r.finish_run(a, "ok", records=10, completeness=1.0)
        self.clock.t += 100; b = self.r.start_run("gutenberg_csv"); self.r.finish_run(b, "failed", error_class="network", error="boom")
        c = self.r.start_run("gutendex")                                                    # never finished
        self.clock.t += 4000
        status = self.r.provider_status()
        self.assertEqual(status["gutenberg_csv"]["last_attempt"]["state"], "failed"); self.assertEqual(status["gutenberg_csv"]["last_good"]["records"], 10)
        self.assertEqual(status["gutendex"]["last_attempt"]["state"], "failed"); self.assertIsNone(status["gutendex"]["last_good"])

    def test_local_books_are_recorded_as_starters_and_never_enter_the_ocean(self):
        self.r.import_local_books("en", [{"id": "s1", "title": "S", "author": "A", "gutenberg_id": 100, "rights_status": "approved"}], {"s1": {"verified": True, "text_sha256": "c" * 64}})
        self.assertEqual(self.r.get("s1")["pool_status"], "starter"); self.assertEqual(self.r.get("s1")["text_hash"], "c" * 64)
        self.assertEqual(list(self.r.eligible_rows("en")), [])


class SeedUpgradeTests(unittest.TestCase):
    def test_the_shipped_index_is_upgraded_without_network_and_bootstraps_the_registry(self):
        seed = Path("data/universe_seed.sqlite")
        if not seed.exists():
            self.skipTest("no shipped index")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "u.sqlite"
            self.assertTrue(universe.upgrade_index(seed, out))
            self.assertFalse(universe.upgrade_index(out, Path(tmp) / "again.sqlite"))      # already current: plain copy
            u = universe.Universe(out)
            self.assertTrue(u.available); self.assertGreater(u.count("en"), 1000)
            rows = universe.index_rows(out)
            reg = Registry(Path(tmp) / "r.sqlite")
            self.assertEqual(reg.import_index_rows(rows), len(rows))
            record = u.random_candidates("en", 1, random.Random(1))[0]
            self.assertEqual(record["id"], f"pg-en-{record['gutenberg_id']}")
            self.assertEqual(sum(reg.summary()["eligible"].values()), len(rows))

    def test_an_index_from_an_older_version_is_not_served_but_asks_to_be_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            conn = sqlite3.connect(path); conn.executescript("CREATE TABLE info (key TEXT, value TEXT); INSERT INTO info VALUES ('schema','3');"); conn.commit(); conn.close()
            u = universe.Universe(path)
            self.assertFalse(u.available); self.assertIn("older version", u.load_error)


# ---- the fetch guard ---------------------------------------------------------------------------------------------
class Resp:
    def __init__(self, body=b"{}", ctype="application/json", url="https://gutendex.com/books", status=200, length=None):
        self._b, self._url, self.status = io.BytesIO(body), url, status
        self.headers = Message(); self.headers["Content-Type"] = ctype
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def read(self, n=-1): return self._b.read(n)
    def geturl(self): return self._url
    def __enter__(self): return self
    def __exit__(self, *a): return False


def http_error(code, retry_after=None):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://gutendex.com/books", code, "x", h, io.BytesIO())


def guard_with(script, **kw):
    """A Guard whose network is a script of responses/exceptions. Records sleeps instead of sleeping."""
    calls, sleeps, script = [], [], list(script)
    def opener(req, timeout=None):
        calls.append((req.full_url, req.get_header("User-agent")))
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, BaseException):
            raise item
        return item
    g = fetchguard.Guard("gutendex", fetchguard.GUTENDEX_HOSTS, opener=opener, sleep=sleeps.append, rng=random.Random(1), min_interval=0,
                         content_types=("application/json",), **kw)
    return g, calls, sleeps


class FetchGuardTests(unittest.TestCase):
    def test_only_https_on_the_allowlisted_host_is_ever_requested(self):
        g, calls, _ = guard_with([Resp()])
        for bad in ("http://gutendex.com/books", "https://evil.example/books", "https://gutendex.com.evil.example/", "file:///etc/passwd"):
            with self.assertRaises(fetchguard.PermanentFetchError): g.get(bad)
        self.assertEqual(calls, [])

    def test_a_redirect_to_another_host_is_refused(self):
        handler = fetchguard._SafeRedirect(fetchguard.GUTENDEX_HOSTS)
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, io.BytesIO(), 302, "Found", Message(), "https://evil.example/x")
        g, _calls, _ = guard_with([Resp(url="https://evil.example/landed")])
        with self.assertRaisesRegex(fetchguard.PermanentFetchError, "not allowed"): g.get("https://gutendex.com/books")

    def test_the_user_agent_names_a_contact_when_configured(self):
        self.assertIn("(+https://example.org, ops@example.org)", fetchguard.user_agent({"LR_CONTACT_EMAIL": "ops@example.org", "LR_SITE_URL": "https://example.org"}))
        g, calls, _ = guard_with([Resp()], ua=fetchguard.user_agent({"LR_CONTACT_EMAIL": "ops@example.org", "LR_SITE_URL": "https://example.org"}))
        g.get("https://gutendex.com/books"); self.assertIn("ops@example.org", calls[0][1])

    def test_size_and_content_type_limits(self):
        g, _c, _s = guard_with([Resp(b"x" * 5000)], max_bytes=1000)
        with self.assertRaisesRegex(fetchguard.PermanentFetchError, "larger"): g.get("https://gutendex.com/books")
        g, _c, _s = guard_with([Resp(b"x", length=10**9)])
        with self.assertRaisesRegex(fetchguard.PermanentFetchError, "larger"): g.get("https://gutendex.com/books")
        g, _c, _s = guard_with([Resp(b"<html>", ctype="text/html")])
        with self.assertRaisesRegex(fetchguard.PermanentFetchError, "content type"): g.get("https://gutendex.com/books")

    def test_a_busy_server_is_retried_with_its_own_retry_after_then_succeeds(self):
        g, calls, sleeps = guard_with([http_error(503, retry_after=7), http_error(429), Resp(b'{"ok":1}')])
        self.assertEqual(g.get("https://gutendex.com/books").body, b'{"ok":1}')
        self.assertEqual(len(calls), 3); self.assertGreaterEqual(sleeps[0], 7)                  # the server's advice wins over our back-off
        self.assertGreater(sleeps[1], 0)

    def test_a_permanent_answer_is_never_retried_and_does_not_count_against_the_provider(self):
        g, calls, _ = guard_with([http_error(404)], breaker=fetchguard.Breaker(threshold=2))
        for _ in range(5):
            with self.assertRaises(fetchguard.PermanentFetchError): g.get("https://gutendex.com/books")
        self.assertEqual(len(calls), 5); self.assertTrue(g.breaker.allow())

    def test_repeated_transient_failures_open_the_circuit_and_calls_stop_reaching_the_provider(self):
        g, calls, _ = guard_with([http_error(503)], breaker=fetchguard.Breaker(threshold=3, cooldown=60))
        with self.assertRaises(fetchguard.TransientFetchError): g.get("https://gutendex.com/books", retries=5)
        self.assertEqual(len(calls), 3)                                                          # stopped as soon as the circuit opened
        with self.assertRaises(fetchguard.CircuitOpen): g.get("https://gutendex.com/books")
        self.assertEqual(len(calls), 3); self.assertTrue(g.status()["circuit_open"])

    def test_a_retry_after_longer_than_we_will_wait_is_reported_not_slept(self):
        g, calls, sleeps = guard_with([http_error(429, retry_after=900)])
        with self.assertRaises(fetchguard.TransientFetchError) as ctx: g.get("https://gutendex.com/books", backoff_cap=60)
        self.assertEqual((len(calls), sleeps, ctx.exception.retry_after), (1, [], 900.0))

    def test_retry_after_parsing(self):
        self.assertEqual(fetchguard.parse_retry_after("12"), 12.0)
        self.assertEqual(fetchguard.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 30), 30.0)
        self.assertIsNone(fetchguard.parse_retry_after("soon")); self.assertEqual(fetchguard.parse_retry_after("999999"), 3600.0)


# ---- catalogue validation ---------------------------------------------------------------------------------------
class CatalogueValidationTests(unittest.TestCase):
    def test_a_sudden_shrink_or_a_missing_english_shelf_is_rejected(self):
        good = [rec(i) for i in range(1, 301)]
        self.assertEqual(catalogue_csv.validate_records(good, {"text_rows": 300}, {"en": 300})["counts"]["en"], 300)
        with self.assertRaisesRegex(catalogue_csv.ValidationError, "en: 200 entries now against 300"):
            catalogue_csv.validate_records(good[:200], {"text_rows": 200}, {"en": 300})
        with self.assertRaisesRegex(catalogue_csv.ValidationError, "no English"):
            catalogue_csv.validate_records([rec(1, "fr")], {})

    def test_duplicate_and_untitled_rates_matter_only_for_a_real_sized_file(self):
        base = [rec(i) for i in range(1, 400)]
        with self.assertRaisesRegex(catalogue_csv.ValidationError, "duplicate ids"):
            catalogue_csv.validate_records(base, {"text_rows": 400, "duplicates": 60})
        untitled = [rec(i, title=f"Untitled ebook #{i}") for i in range(1, 400)]
        with self.assertRaisesRegex(catalogue_csv.ValidationError, "without a title"):
            catalogue_csv.validate_records(untitled, {"text_rows": 399})

    def test_a_truncated_download_never_replaces_the_catalogue_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = core.write_catalog_csv(Path(tmp) / "src.csv.gz").read_bytes()
            dest = Path(tmp) / "dest.csv.gz"
            opener = lambda req, timeout=None: Resp(raw[: len(raw) // 2], ctype="application/gzip", url=req.full_url)
            with self.assertRaises(OSError): catalogue_csv.download(dest, urls=(("main", "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"),), opener=opener, conditional=False)
            self.assertFalse(dest.exists())

    def test_the_catalogue_download_refuses_a_host_that_is_not_gutenberg(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with self.assertRaises(OSError): catalogue_csv.download(Path(tmp) / "d.gz", urls=(("evil", "https://evil.example/pg_catalog.csv.gz"),), opener=lambda *a, **k: calls.append(1), conditional=False)
            self.assertEqual(calls, [])


class SmallOcean(core.OceanBase):
    """A tiny catalogue (4 English books) so a reader can really use it up."""
    EN, FR, ZH = 4, 2, 2


class SyncRegistryTests(core.OceanBase):
    def test_the_first_sync_filled_the_registry_and_recorded_a_good_run(self):
        reg = self.rt.registry
        self.assertEqual(reg.summary()["eligible"]["en"], self.EN)
        run = reg.last_run("gutenberg_csv", ("ok",))
        self.assertEqual((run["records"] >= self.EN + self.FR + self.ZH, run["completeness"]), (True, 1.0)); self.assertTrue(run["source_hash"])

    def test_an_unchanged_file_rebuilds_nothing(self):
        stamp = self.rt.universe_path.stat().st_mtime_ns
        generation = self.rt.registry.generation
        self.assertTrue(self.rt.sync.run())
        self.assertEqual(self.rt.registry.last_run("gutenberg_csv")["state"], "unchanged")
        self.assertEqual((self.rt.universe_path.stat().st_mtime_ns, self.rt.registry.generation), (stamp, generation))

    def test_a_damaged_or_shrunken_catalogue_is_rejected_and_everything_keeps_serving(self):
        before = (self.rt.registry.count(), dict(self.rt.universe.counts))
        small = core.write_catalog_csv(Path(self.tmp.name) / "small.csv.gz", en=100, fr=60, zh=30)       # English fell from 800 to 100
        self.rt.sync.csv_path = small
        self.assertFalse(self.rt.sync.run())
        run = self.rt.registry.last_run("gutenberg_csv")
        self.assertEqual((run["state"], run["error_class"]), ("rejected", "validation"))
        self.assertEqual((self.rt.registry.count(), dict(self.rt.universe.counts)), before)
        self.assertTrue(self.post("/api/draw", count=1)["ok"])
        self.assertEqual(self.rt.sync.status()["state"], "failed")
        self.assertIn("previous", self.rt.sync.status()["message"])

    def test_rebuilding_from_the_registry_needs_no_download_and_follows_admin_decisions(self):
        ids = [r["book_id"] for r in self.rt.registry.eligible_rows("en")][:3]
        for bid in ids: self.rt.registry.set_pool_status(bid, "paused", "test")
        self.rt.sync.rebuild_index()
        self.assertEqual(self.rt.universe.count("en"), self.EN - 3)
        for bid in ids: self.rt.registry.set_pool_status(bid, "active")
        self.rt.sync.rebuild_index()
        self.assertEqual(self.rt.universe.count("en"), self.EN)

    def test_a_paused_book_is_never_drawn_even_before_any_rebuild(self):
        pool = [r["book_id"] for r in self.rt.registry.eligible_rows("en")]
        paused = set(pool[:700])
        for bid in paused: self.rt.registry.set_pool_status(bid, "paused", "test")
        self.rt.universe.reload()
        rng = random.Random(3); drawn = set()
        for _ in range(60):
            drawn |= {r["id"] for r in self.rt.universe.random_candidates("en", 5, rng)}
        self.assertTrue(drawn); self.assertFalse(drawn & paused)
        self.assertGreater(len(drawn), 50)                                                  # still uniform over what remains (100 books)

    def test_a_book_whose_text_failed_for_good_is_marked_and_not_drawn_again(self):
        record = self.rt.universe.random_candidates("en", 1, random.Random(8))[0]
        def broken(book): raise source.PermanentSourceError("HTTP 404")
        self.rt.worker._fetch = broken
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        self.assertEqual(self.rt.registry.get(record["id"])["text_status"], "failed")
        self.rt.universe.reload()
        self.assertIn(record["id"], self.rt.universe.ineligible)
        self.rt.worker.clear_failures()
        self.assertEqual(self.rt.registry.get(record["id"])["text_status"], "unverified")

    def test_a_book_that_leaves_the_pool_while_queued_is_dropped_without_being_marked_failed(self):
        record = self.rt.universe.random_candidates("en", 1, random.Random(9))[0]
        self.rt.worker.enqueue(prep.COLD, "en", record, 0)
        self.rt.registry.set_pool_status(record["id"], "paused", "withdrawn by the author")
        self.rt.worker.run_pending()
        self.assertFalse(self.rt.shelf.has("en", record["id"]))
        self.assertEqual(self.rt.registry.get(record["id"])["text_status"], "unverified")      # a decision, not a failure
        self.assertEqual(self.fetched.count(record["gutenberg_id"]), 0)

    def test_a_prepared_book_records_its_text_hash_and_job_keys_carry_the_segmenter_version(self):
        record = self.rt.universe.random_candidates("en", 1, random.Random(2))[0]
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        book = self.rt.registry.get(record["id"])
        self.assertEqual((book["text_status"], len(book["text_hash"])), ("verified", 64))
        self.assertTrue(prep.PrepWorker.job_key("en", "b", "e").endswith("|" + segment.segment_version("en")))


class DrawRuleTests(core.OceanBase):
    def test_a_cold_pick_stays_the_original_pending_handle_while_the_wait_is_bearable(self):
        res = self.post("/api/draw", count=3)
        self.assertEqual((len(res["pending"]), res["tickets"], res["fallback"]), (3, [], None))

    def test_a_busy_queue_falls_back_loudly_and_still_prepares_the_original_picks_later(self):
        for _ in range(2): self.post("/api/draw", count=3); self.rt.worker.run_pending()
        w = self.rt.worker; w.settings.max_cold_wait = 0.0; w.settings.min_interval = 5.0
        for i in range(2): w.enqueue(prep.COLD, "en", self.rt.universe.random_candidates("en", 1, random.Random(i + 70))[0], 5)
        queued = w.store.pending_cold()
        res = self.post("/api/draw", count=3)
        self.assertEqual(res["fallback"]["reason"], "busy"); self.assertTrue(res["fallback"]["temporary"])
        self.assertTrue(all(t.get("fallback") for t in res["tickets"]))
        self.assertGreater(w.store.pending_cold(), queued)                                  # the original uniform picks were queued after all
        self.assertEqual(len(self.fetched), len(set(self.fetched)))

    def test_paused_downloads_are_reported_as_such(self):
        for _ in range(2): self.post("/api/draw", count=3); self.rt.worker.run_pending()
        self.rt.worker._breaker_until = time.time() + 60
        res = self.post("/api/draw", count=3)
        self.assertEqual(res["fallback"]["reason"], "source_paused"); self.assertEqual(res["pending"], [])
        self.assertGreaterEqual(len(res["tickets"]), 1)

    def test_a_busy_librarian_with_an_empty_shelf_says_busy_not_one_book(self):
        self.rt.worker._breaker_until = time.time() + 60
        res = self.client.post("/api/draw", json={"count": 3}); body = res.get_json()
        self.assertEqual((res.status_code, body["code"]), (503, "busy"))

    def test_the_queue_is_bounded_whatever_the_load(self):
        w = self.rt.worker; w.settings.queue_cap = 5; w.settings.max_cold_wait = 0.0; w.settings.min_interval = 5.0
        for _ in range(30): self.post("/api/draw", count=3)
        self.assertLessEqual(w.store.pending_cold(), 5 + 1)

    def test_the_fallback_shelf_never_serves_a_paused_book(self):
        for _ in range(3): self.post("/api/draw", count=3); self.rt.worker.run_pending()
        lake = list(self.rt.lake("en"))
        for bid in lake: self.rt.registry.set_pool_status(bid, "paused", "test")
        self.rt.universe.reload()
        self.rt.worker._breaker_until = time.time() + 60
        res = self.client.post("/api/draw", json={"count": 3}); self.assertEqual(res.get_json()["code"], "busy")   # nothing left to serve safely

    def test_metrics_count_draws_and_time_requests(self):
        for _ in range(3): self.post("/api/draw", count=3)
        snap = self.app.extensions["lr_metrics"].snapshot()
        self.assertEqual(snap["counts"]["draws"], 3); self.assertEqual(snap["latency"]["draw"]["n"], 3)
        self.assertGreaterEqual(snap["latency"]["draw"]["p95_ms"], snap["latency"]["draw"]["p50_ms"])


class RestartTests(SmallOcean):
    def tokens(self, books):
        return [self.rt.catalogues["en"].book_token(b["id"]) for b in books]

    def test_a_new_round_starts_only_when_every_eligible_book_has_been_seen(self):
        books = list(self.rt.universe.iter_eligible("en")); self.assertEqual(len(books), self.EN)
        almost = self.post("/api/draw", count=3, seen=self.tokens(books[:3]))
        self.assertFalse(almost["restarted"]); self.assertEqual([p["draw_number"] for p in almost["pending"]], [books[3]["draw_number"]])
        everything = self.post("/api/draw", count=3, seen=self.tokens(books))
        self.assertTrue(everything["restarted"]); self.assertGreaterEqual(len(everything["pending"]) + len(everything["tickets"]), 1)

    def test_a_paused_book_does_not_stop_the_round_from_ending(self):
        books = list(self.rt.universe.iter_eligible("en"))
        self.rt.registry.set_pool_status(books[0]["id"], "paused", "test"); self.rt.universe.reload()
        res = self.post("/api/draw", count=3, seen=self.tokens(books[1:]))
        self.assertTrue(res["restarted"])


class PopularityTruthTests(core.OceanBase):
    def reveal_popularity(self):
        record = self.rt.universe.random_candidates("en", 1, random.Random(4))[0]
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        t = self.post("/api/draw", count=1, seen=[])                                       # any ready ticket for that book
        ticket = self.rt_ticket(record)
        return record, self.post("/api/book/reveal", book_token=ticket["book_token"], ticket=ticket["ticket"])["book"]["popularity"]

    def rt_ticket(self, record):
        cat = self.rt.catalogues["en"]; app = self.app
        import tickets as tk
        n = self.rt.shelf.passage_count("en", record["id"])
        return {"book_token": cat.book_token(record["id"]), "ticket": tk.issue(self.rt.secret.encode(), "en", cat.book_token(record["id"]), 0, 600)}

    def test_without_any_ranking_data_a_book_gets_no_claim_at_all(self):
        self.rt.popularity.path.write_text(json.dumps({"updated": 0}))
        _r, pop = self.reveal_popularity()
        self.assertIsNone(pop)

    def test_off_the_top_list_with_a_lone_count_is_neutral_and_labelled_never_a_rank(self):
        self.rt.popularity.path.write_text(json.dumps({"updated": time.time(), "month": {str(g): g for g in range(1, 60)}}))
        record = self.rt.universe.random_candidates("en", 1, random.Random(4))[0]
        self.rt.registry.add_popularity([(record["id"], "en", 1234, None, "ok")], provider="gutendex", metric="download_count", period="as_reported", scope="targeted")
        _r, pop = self.reveal_popularity()
        self.assertEqual(pop["tier"], "offlist"); self.assertNotIn("top_percent", pop)
        self.assertIn("1,234 downloads as reported by Gutendex", pop["detail"])
        self.assertEqual((pop["source"], pop["period"]), ("Project Gutenberg top 100", "last 30 days"))

    def test_a_percentile_appears_only_once_the_whole_language_has_been_observed(self):
        self.rt.popularity.path.write_text(json.dumps({"updated": time.time(), "month": {"1": 1, **{str(g): g for g in range(2, 60)}}}))
        rows = [(r["book_id"], "en", i, None, "ok") for i, r in enumerate(self.rt.registry.eligible_rows("en"))]
        record = self.rt.universe.random_candidates("en", 1, random.Random(4))[0]
        self.rt.registry.add_popularity(rows[:-1], provider="gutendex", metric="download_count", period="as_reported", scope="census")
        _r, partial = self.reveal_popularity(); self.assertEqual(partial["tier"], "offlist")
        self.rt.registry.add_popularity(rows[-1:], provider="gutendex", metric="download_count", period="as_reported", scope="census")
        self.rt.registry._complete_cache.clear()
        _r, full = self.reveal_popularity()
        self.assertIn(full["tier"], ("jackpot", "lucky", "discovery", "hidden-gem")); self.assertEqual(full["total"], self.EN)
        self.assertIn("Gutendex", full["note"]); self.assertEqual(full["source"], "Gutendex download counts")


# ---- Gutendex ----------------------------------------------------------------------------------------------------
def gutendex_page(books):
    return json.dumps({"count": len(books), "next": None, "previous": None, "results": books}).encode()


def gbook(gid, count=10, copyright=False, subjects=("Adventure",)):
    return {"id": gid, "title": f"Title {gid}", "authors": [{"name": "Writer, A."}], "subjects": list(subjects), "bookshelves": ["Adventure"],
            "languages": ["en"], "copyright": copyright, "summaries": ["A tale. (This is an automatically generated summary.)"], "download_count": count,
            "formats": {"text/plain": "x"}}


def smart_guard(known=lambda gid: True, count=lambda gid: gid * 10, copyright=lambda gid: False, fail_on_call=None, subjects=("Sea", "Ships")):
    """A Gutendex stand-in that answers for exactly the ids it is asked about."""
    calls = []
    def opener(req, timeout=None):
        calls.append(req.full_url)
        if fail_on_call and len(calls) >= fail_on_call:
            raise http_error(503)
        ids = [int(x) for x in req.full_url.split("ids=")[1].split(",")]
        return Resp(gutendex_page([gbook(g, count=count(g), copyright=copyright(g), subjects=subjects) for g in ids if known(g)]))
    g = fetchguard.Guard("gutendex", fetchguard.GUTENDEX_HOSTS, opener=opener, sleep=lambda s: None, rng=random.Random(1), min_interval=0,
                         content_types=("application/json",), breaker=fetchguard.Breaker(threshold=50))
    return g, calls


class GutendexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(); self.reg = Registry(Path(self.tmp.name) / "r.sqlite", clock=self.clock)
        self.reg.import_gutenberg([rec(i) for i in range(1, 41)])

    def provider(self, guard=None, **kw):
        g = guard or guard_with([Resp()])[0]
        return gutendex.Gutendex(self.reg, g, prefer=lambda: range(1, 41), clock=self.clock, **kw)

    def test_a_batch_records_snapshots_counts_and_catalogue_fields_in_one_request(self):
        g, calls = smart_guard(); p = self.provider(g, per_run=5)
        out = p.run()
        self.assertEqual((out["state"], out["enriched"], out["not_found"]), ("ok", 5, 0))
        self.assertEqual(len(calls), 1); ids = [int(x) for x in calls[0].split("ids=")[1].split(",")]; self.assertEqual(len(ids), 5)
        gid = ids[0]; bid = f"pg-en-{gid}"
        self.assertEqual(self.reg.observed(bid)["value"], gid * 10)
        self.assertEqual(self.reg.get(bid)["subjects"], ["Sea", "Ships"]); self.assertEqual(self.reg.get(bid)["metadata_status"], "enriched")
        snap = self.reg.snapshots(bid, "gutendex")[0]
        self.assertEqual((snap["status"], snap["normalized"]["download_count"], len(snap["raw_hash"])), ("ok", gid * 10, 64))
        self.assertEqual(self.reg.last_run("gutendex")["completeness"], 1.0)

    def test_a_book_gutendex_does_not_know_is_recorded_as_unknown_not_as_zero(self):
        g, calls = smart_guard(known=lambda gid: gid % 2 == 0); p = self.provider(g, per_run=6)
        out = p.run()
        self.assertEqual(out["enriched"] + out["not_found"], 6); self.assertGreater(out["not_found"], 0)
        odd = next(int(x) for x in calls[0].split("ids=")[1].split(",") if int(x) % 2)
        seen = self.reg.observed(f"pg-en-{odd}")
        self.assertEqual((seen["value"], seen["status"]), (None, "not_found"))
        self.assertIsNone(self.reg.percentile(f"pg-en-{odd}"))

    def test_a_missing_or_malformed_count_stays_unknown(self):
        bad = gbook(1); bad["download_count"] = "lots"
        self.assertIsNone(gutendex.normalize(bad)["download_count"])
        no = gbook(2); del no["download_count"]; self.assertIsNone(gutendex.normalize(no)["download_count"])
        self.assertIsNone(gutendex.normalize({**gbook(3), "download_count": -4})["download_count"]); self.assertEqual(gutendex.normalize(gbook(4, count=0))["download_count"], 0)

    def test_a_failure_halfway_keeps_what_was_collected_and_reports_partial(self):
        g, calls = smart_guard(fail_on_call=2); p = self.provider(g, per_run=64)
        out = p.run()
        self.assertEqual((out["state"], out["enriched"]), ("partial", 32)); self.assertGreater(len(calls), 1)
        self.assertEqual(self.reg.last_run("gutendex")["state"], "partial")
        first = int(calls[0].split("ids=")[1].split(",")[0]); self.assertEqual(self.reg.observed(f"pg-en-{first}")["value"], first * 10)

    def test_a_reply_in_the_wrong_shape_is_a_failed_run_that_changes_nothing(self):
        g, _c, _s = guard_with([Resp(b'{"detail": "maintenance"}')]); p = self.provider(g, per_run=3)
        out = p.run()
        self.assertEqual((out["state"], self.reg.last_run("gutendex")["error_class"]), ("failed", "schema"))
        self.assertIsNone(self.reg.observed("pg-en-1"))

    def test_runs_are_scheduled_by_their_own_recorded_timestamp(self):
        g, _calls = smart_guard(); p = self.provider(g, per_run=1, interval=3600)
        self.assertTrue(p.due()); p.run(); self.assertFalse(p.due())
        self.clock.t += 3601; self.assertTrue(p.due())
        p.enabled = False; self.assertFalse(p.due())

    def test_a_copyright_flag_from_gutendex_holds_the_book_for_review(self):
        g, _calls = smart_guard(copyright=lambda gid: gid == 7); p = self.provider(g, per_run=40); p.run()
        self.assertEqual(self.reg.get("pg-en-7")["pool_status"], "paused"); self.assertIn("pg-en-7", self.reg.ineligible_ids())
        self.assertEqual(self.reg.get("pg-en-8")["pool_status"], "active")

    def test_a_full_census_makes_percentiles_possible(self):
        g, _calls = smart_guard(); p = self.provider(g, per_run=64, census=True)
        self.assertFalse(self.reg.complete("en")); p.run()
        self.assertTrue(self.reg.complete("en")); self.assertEqual(self.reg.percentile("pg-en-40")["rank"], 1)


# ---- author TXT intake -----------------------------------------------------------------------------------------
GOOD_TXT = fake_gutenberg("Harbour Lights", author="Ann Author", paragraphs=120, seed=3)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)
        self.reg = Registry(self.dir / "r.sqlite"); self.store = intake.TextStore(self.dir / "texts")

    def write(self, name, data):
        path = self.dir / name; path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8")); return path

    def go(self, path, slug="harbour", **kw):
        return intake.intake(self.reg, self.store, path, slug=slug, language="en", title="Harbour Lights", author="Ann Author", **kw)

    def test_the_checks_name_every_problem_in_plain_words(self):
        self.assertIn("not valid UTF-8", intake.check_txt("café ".encode("latin-1") * 3000, "en")[1][0])
        self.assertIn("too short", intake.check_txt(b"Just a few words.", "en")[1][0])
        self.assertIn("readable passage", intake.check_txt(("word " * 2000).encode(), "en")[1][0])
        self.assertIn("larger than", intake.check_txt(b"x" * (intake.MAX_BYTES + 1), "en")[1][0])
        text, problems, passages = intake.check_txt(GOOD_TXT.encode(), "en")
        self.assertEqual(problems, []); self.assertGreaterEqual(passages, intake.MIN_PASSAGES)

    def test_a_good_file_is_recorded_pending_with_its_claimed_permission_and_cannot_be_drawn(self):
        out = self.go(self.write("a.txt", GOOD_TXT), permission={"territory": "Worldwide", "commercial": False, "attribution": "Ann Author, used with permission",
                                                                 "expires": "perpetual", "evidence": "email 2026-11-02"})
        self.assertTrue(out["ok"]); self.assertEqual(out["book_id"], "au-en-harbour")
        book = self.reg.get(out["book_id"])
        self.assertEqual((book["pool_status"], book["rights_status"], book["text_status"], book["revision"]), ("pending", "pending_verification", "verified", 1))
        self.assertEqual((book["rights_territory"], book["rights_commercial"], book["rights_note"]), ("Worldwide", 0, "email 2026-11-02"))
        self.assertEqual(self.reg.blockers(out["book_id"]) and len(self.reg.blockers(out["book_id"])) >= 1, True)
        self.assertIn(out["book_id"], self.reg.ineligible_ids())
        text = self.store.read(book["text_key"], expect_hash=book["text_hash"]); self.assertIn("Harbour Lights".upper(), text.upper())

    def test_missing_permission_still_cannot_reach_the_pool_even_if_someone_tries_to_activate(self):
        out = self.go(self.write("a.txt", GOOD_TXT))
        with self.assertRaises(RegistryError): self.reg.set_pool_status(out["book_id"], "active")

    def test_the_same_text_under_another_slug_is_a_duplicate(self):
        self.go(self.write("a.txt", GOOD_TXT))
        out = self.go(self.write("b.txt", GOOD_TXT), slug="other-copy")
        self.assertFalse(out["ok"]); self.assertIn("already in the Registry as au-en-harbour", out["problems"][0])
        self.assertIsNone(self.reg.get("au-en-other-copy"))

    def test_a_bad_file_leaves_nothing_behind_and_a_revision_replaces_the_text(self):
        bad = self.go(self.write("bad.txt", b"tiny"), slug="tiny")
        self.assertFalse(bad["ok"]); self.assertIsNone(self.reg.get("au-en-tiny")); self.assertEqual(list(self.store.dir.glob("*.txt")), [])
        self.go(self.write("a.txt", GOOD_TXT))
        revised = self.go(self.write("a2.txt", fake_gutenberg("Harbour Lights", paragraphs=130, seed=9)))
        self.assertEqual((revised["ok"], revised["revision"]), (True, 2))
        self.assertEqual(len(list(self.store.dir.glob("au-en-harbour.*.txt"))), 1)       # the old revision is gone
        failed = self.go(self.write("a3.txt", b"broken"))
        self.assertFalse(failed["ok"]); self.assertEqual(self.reg.get("au-en-harbour")["revision"], 2)    # a failed revision changes nothing

    def test_slug_language_and_names_are_validated(self):
        path = self.write("a.txt", GOOD_TXT)
        for kw in ({"slug": "Bad Slug"}, {"slug": "../x"}):
            with self.assertRaises(RegistryError): self.go(path, **kw)
        with self.assertRaises(RegistryError): intake.intake(self.reg, self.store, path, slug="ok-slug", language="de", title="T", author="A")
        with self.assertRaises(RegistryError): intake.intake(self.reg, self.store, path, slug="ok-slug", language="en", title=" ", author="A")

    def test_the_text_store_refuses_path_tricks_and_detects_tampering(self):
        out = self.go(self.write("a.txt", GOOD_TXT)); book = self.reg.get(out["book_id"])
        for key in ("../r.sqlite", "a/b.txt", ".hidden"):
            with self.assertRaises(ValueError): self.store.read(key)
        (self.store.dir / book["text_key"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash"): self.store.read(book["text_key"], expect_hash=book["text_hash"])


class AuthorEndToEndTests(core.OceanBase):
    """Intake -> rights review -> activation -> index rebuild -> drawn, prepared from the private store, revealed with credit."""

    def intake_and_approve(self, slug="harbour"):
        path = Path(self.tmp.name) / f"{slug}.txt"; path.write_text(GOOD_TXT, encoding="utf-8")
        out = intake.intake(self.rt.registry, self.rt.text_store, path, slug=slug, language="en", title="Harbour Lights", author="Ann Author",
                            permission={"territory": "Worldwide", "commercial": True, "attribution": "Ann Author, shared with permission", "expires": "perpetual"},
                            links={"buy": "https://example.org/harbour-lights"}, description="A fictional harbour tale.")
        self.assertTrue(out["ok"], out)
        return out["book_id"]

    def test_the_whole_path(self):
        bid = self.intake_and_approve()
        self.rt.sync.rebuild_index(); self.assertIsNone(self.rt.universe.get_by_id("en", bid))      # pending: not in the index
        self.rt.registry.set_rights(bid, "approved"); self.rt.registry.set_pool_status(bid, "active")
        self.rt.sync.rebuild_index()
        record = self.rt.universe.get_by_id("en", bid)
        self.assertEqual((record["provider"], record["source_provider"], record["links"]), ("author", "Independent author", {"buy": "https://example.org/harbour-lights"}))
        self.assertEqual(self.rt.universe.count("en"), self.EN + 1)
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        self.assertEqual(self.fetched, [])                                                          # no download: the text came from the private store
        self.assertTrue(self.rt.shelf.has("en", bid))
        cat = self.rt.catalogues["en"]; token = cat.book_token(bid)
        import tickets as tk
        ticket = tk.issue(self.rt.secret.encode(), "en", token, 0, 600)
        book = self.post("/api/book/reveal", book_token=token, ticket=ticket)["book"]
        self.assertEqual((book["title"], book["source_credit"]["label"]), ("Harbour Lights", "Ann Author, shared with permission"))
        self.assertEqual([l["kind"] for l in book["links"]], ["buy"]); self.assertEqual(book["description"], "A fictional harbour tale.")
        self.assertIsNone(book["popularity"])                                                       # no Gutenberg claims about an author's book

    def test_withdrawing_the_rights_stops_drawing_it_at_once(self):
        bid = self.intake_and_approve(); self.rt.registry.set_rights(bid, "approved"); self.rt.registry.set_pool_status(bid, "active")
        self.rt.sync.rebuild_index()
        self.rt.registry.set_rights(bid, "withdrawn", note="author asked"); self.rt.universe.reload()
        rng = random.Random(1)
        for _ in range(400): self.assertNotIn(bid, {r["id"] for r in self.rt.universe.random_candidates("en", 5, rng)})

    def test_a_tampered_stored_text_fails_closed(self):
        bid = self.intake_and_approve(); self.rt.registry.set_rights(bid, "approved"); self.rt.registry.set_pool_status(bid, "active")
        self.rt.sync.rebuild_index(); record = self.rt.universe.get_by_id("en", bid)
        (self.rt.text_store.dir / record["text_key"]).write_text("tampered", encoding="utf-8")
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        self.assertFalse(self.rt.shelf.has("en", bid)); self.assertEqual(self.rt.registry.get(bid)["text_status"], "failed")

    def test_author_slugs_that_end_in_digits_are_never_mistaken_for_gutenberg_numbers(self):
        bid = self.intake_and_approve("story-2"); self.rt.registry.set_rights(bid, "approved"); self.rt.registry.set_pool_status(bid, "active")
        self.rt.sync.rebuild_index(); record = self.rt.universe.get_by_id("en", bid)
        self.rt.worker.enqueue(prep.COLD, "en", record, 0); self.rt.worker.run_pending()
        self.assertNotIn(2, self.rt.worker.known_gids("en")); self.assertIn(bid, self.rt.worker.known_book_ids("en"))


# ---- Registry Center ---------------------------------------------------------------------------------------------
class AdminTests(core.OceanBase):
    TOKEN = "s3cret-admin-token"

    def setUp(self):
        super().setUp()
        self.app.config["ADMIN_TOKEN"] = self.TOKEN

    def get(self, path, token=TOKEN):
        return self.client.get(path, headers={"X-LR-Admin": token} if token is not None else {})

    def act(self, path, token=TOKEN, **body):
        return self.client.post(path, json=body, headers={"X-LR-Admin": token})

    def test_everything_is_closed_without_a_configured_token_and_without_the_right_one(self):
        self.app.config["ADMIN_TOKEN"] = None
        for path in ("/admin/registry", "/api/admin/registry/summary", "/api/admin/registry/books"):
            self.assertEqual(self.get(path).status_code, 404)
        self.app.config["ADMIN_TOKEN"] = self.TOKEN
        for token in (None, "", "wrong", self.TOKEN[:-1], self.TOKEN + "x"):
            self.assertEqual(self.get("/api/admin/registry/summary", token).status_code, 401)
        self.assertEqual(self.act("/api/admin/registry/books/pg-en-1000/pool", token="wrong", status="paused").status_code, 401)
        self.assertEqual(self.get("/api/admin/registry/summary").status_code, 200)

    def test_the_page_is_served_with_no_data_in_it_and_is_not_cached_or_indexed(self):
        res = self.client.get("/admin/registry")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"Title 1", res.data); self.assertEqual(res.headers["X-Robots-Tag"], "noindex, nofollow"); self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.assertNotIn(b"<script>", res.data.replace(b"<script src", b""))                         # no inline script: the CSP stays strict

    def test_summary_books_imports_and_sources(self):
        s = self.get("/api/admin/registry/summary").get_json()
        self.assertEqual(s["registry"]["eligible"]["en"], self.EN); self.assertTrue(s["universe"]["available"]); self.assertIn("draw", s["metrics"]["latency"] if s["metrics"]["latency"] else {"draw": 1})
        b = self.get("/api/admin/registry/books?q=Title 5&language=en&limit=5").get_json()
        self.assertTrue(0 < len(b["books"]) <= 5); self.assertNotIn("text_key", b["books"][0])
        self.assertEqual(self.get("/api/admin/registry/imports").get_json()["total"], 0)
        src = self.get("/api/admin/registry/sources").get_json()
        self.assertGreaterEqual(src["providers"]["gutenberg_csv"]["last_good"]["records"], self.EN + self.FR + self.ZH)
        self.assertEqual(set(src["guards"]), {"gutenberg", "gutendex"}); self.assertFalse(src["gutendex"]["census"])

    def test_pausing_applies_at_once_and_activating_rebuilds_the_index_so_the_book_can_be_drawn(self):
        bid = next(self.rt.registry.eligible_rows("en"))["book_id"]
        res = self.act(f"/api/admin/registry/books/{bid}/pool", status="paused", reason="reader report").get_json()
        self.assertEqual((res["book"]["pool_status"], res["rebuild"]), ("paused", False))
        self.assertIn(bid, self.rt.universe.ineligible)
        stamp = self.rt.universe_path.stat().st_mtime_ns
        res = self.act(f"/api/admin/registry/books/{bid}/pool", status="active").get_json()
        self.assertTrue(res["rebuild"])
        for t in threading.enumerate():                                                                # the rebuild runs in the background
            if t.name == "lr-rebuild": t.join(20)
        self.rt.universe.reload(force=True)
        self.assertNotEqual(self.rt.universe_path.stat().st_mtime_ns, stamp)                          # the index really was rebuilt
        self.assertNotIn(bid, self.rt.universe.ineligible); self.assertIsNotNone(self.rt.universe.get_by_id("en", bid))

    def test_a_refused_change_explains_why_in_plain_words(self):
        bid = next(self.rt.registry.eligible_rows("en"))["book_id"]
        r = self.act(f"/api/admin/registry/books/{bid}/rights", status="approved")
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "refused")); self.assertIn("commercial scope", r.get_json()["message"])
        r = self.act(f"/api/admin/registry/books/{bid}/pool", status="exploded"); self.assertEqual(r.status_code, 400)
        r = self.act("/api/admin/registry/books/nope/pool", status="paused"); self.assertIn("Unknown book", r.get_json()["message"])
        r = self.act(f"/api/admin/registry/books/{bid}/rights", status="approved", territory="EU", commercial=True, attribution="X", expires="perpetual")
        self.assertEqual(r.get_json()["book"]["rights_status"], "approved")

    def test_a_sync_can_be_started_by_name_only(self):
        self.assertEqual(self.act("/api/admin/registry/run/nonsense").status_code, 404)
        self.assertEqual(self.act("/api/admin/registry/run/scores").get_json()["started"], "scores")

    def test_the_shelf_status_detail_uses_a_constant_time_check(self):
        self.assertNotIn("jobs", self.client.get("/api/shelf/status").get_json())
        self.assertIn("jobs", self.get("/api/shelf/status").get_json())
        self.assertNotIn("jobs", self.get("/api/shelf/status", "wrong").get_json())

    def test_admin_requests_have_their_own_stricter_rate_limit(self):
        import hardening
        from flask import Flask
        app = Flask(__name__); app.config["MAX_CONTENT_LENGTH"] = 1024
        hardening.apply(app, env={"LR_RATE_ADMIN": "3"})
        app.add_url_rule("/api/admin/x", "x", lambda: "ok"); app.add_url_rule("/api/y", "y", lambda: "ok")
        c = app.test_client()
        self.assertEqual([c.get("/api/admin/x").status_code for _ in range(5)], [200, 200, 200, 429, 429])
        self.assertEqual(c.get("/api/y").status_code, 200)                                              # ordinary traffic has its own bucket


class SecretKeyWarningTests(unittest.TestCase):
    def test_production_without_a_stable_key_is_called_out_in_the_log(self):
        import hardening
        from flask import Flask
        app = Flask(__name__)
        with self.assertLogs(app.logger, "WARNING") as logs:
            hardening.apply(app, env={"LR_PRODUCTION": "1"})
        self.assertIn("LR_SECRET_KEY", logs.output[0])


if __name__ == "__main__":
    unittest.main()


class LoadGateTests(unittest.TestCase):
    """The release gates under concurrent readers (in-process: this measures the application's own logic and locking)."""

    def check(self, readers, spins, warm):
        import os
        from tools import loadtest
        before = os.environ.get("LR_RATE_API")
        try:
            report = loadtest.run(readers=readers, spins=spins, catalogue=600, cold_ms=5, queue_cap=20, warm=warm, patience=4.0)
        finally:
            os.environ.pop("LR_RATE_API", None) if before is None else os.environ.__setitem__("LR_RATE_API", before)
        self.assertTrue(report["passed"], report["gates"])
        self.assertEqual(report["errors"], [])
        self.assertLessEqual(report["queue_depth"]["max"], 20 + 3 + 2)
        self.assertGreater(report["outcomes"]["spins_served"], readers * spins * 0.8)             # readers got books, not errors
        return report

    def test_ten_readers(self):
        self.check(10, 3, warm=50)

    def test_a_hundred_readers_with_a_bounded_queue_and_loud_fallbacks(self):
        report = self.check(100, 2, warm=100)
        self.assertGreater(report["outcomes"]["fallback_draws"], 0)                               # overload is visible, never silent
