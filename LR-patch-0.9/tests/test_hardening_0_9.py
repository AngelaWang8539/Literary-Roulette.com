"""Tests for the 0.9 source-safety patch. Offline: no network, no real Gutenberg. Run from the project folder:
    python3 tests/test_hardening_0_9.py -v
"""
import os
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prep      # noqa: E402
import runtime   # noqa: E402
import source    # noqa: E402

BOOK = {"id": "pg-en-1342", "language": "en", "gutenberg_id": 1342}
TEXT = ("*** START OF THE PROJECT GUTENBERG EBOOK 1342 ***\n" + "It is a truth universally acknowledged. " * 60 +
        "\n*** END OF THE PROJECT GUTENBERG EBOOK 1342 ***").encode()


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


def http_error(code, retry_after=None):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://x/", code, "err", h, None)


class FakeResponse:
    def __init__(self, body=b"hello", length=None, ctype="text/plain"):
        self.body, self.pos = body, 0
        self.headers = SimpleNamespace(get_content_type=lambda: ctype, get=lambda k, d=None: (str(length) if length is not None and k == "Content-Length" else d))
    def read(self, n):
        chunk = self.body[self.pos:self.pos + n]; self.pos += n; return chunk
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Urls(unittest.TestCase):
    def setUp(self):
        for k in ("LR_MIRROR_BASES", "LR_ALLOW_WWW"): os.environ.pop(k, None)

    def test_default_chain_is_official_mirrors_and_never_www(self):
        self.assertEqual(source.mirror_urls(1342), ["https://gutenberg.pglaf.org/cache/epub/1342/pg1342.txt",
                                                    "https://aleph.gutenberg.org/cache/epub/1342/pg1342.txt"])

    def test_extra_mirror_uses_classic_tree_and_www_is_last_when_allowed(self):
        os.environ["LR_MIRROR_BASES"] = "https://m.example.org/pg/, http://insecure.example/x"
        os.environ["LR_ALLOW_WWW"] = "1"
        urls = source.mirror_urls(1342)
        self.assertIn("https://m.example.org/pg/1/3/4/1342/1342-0.txt", urls)
        self.assertIn("https://m.example.org/pg/1/3/4/1342/1342.txt", urls)
        self.assertFalse(any("insecure.example" in u for u in urls))
        self.assertTrue(urls[-1].startswith("https://www.gutenberg.org/cache/epub/1342/"))

    def test_trusted_url(self):
        for ok in ("https://gutenberg.pglaf.org/x", "https://aleph.gutenberg.org/x", "https://www.gutenberg.org/x"):
            self.assertTrue(source.trusted_url(ok), ok)
        for bad in ("http://gutenberg.pglaf.org/x", "https://evilgutenberg.org/x", "https://gutenberg.org.evil.com/x", "https://m.example.org/x"):
            self.assertFalse(source.trusted_url(bad), bad)
        os.environ["LR_MIRROR_BASES"] = "https://m.example.org/pg"
        self.assertTrue(source.trusted_url("https://m.example.org/pg/1/2"))

    def test_redirects_must_stay_trusted(self):
        handler, req = source._TrustedRedirects(), mock.Mock(full_url="https://gutenberg.pglaf.org/a")
        with self.assertRaises(source.PermanentSourceError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/book.txt")


class Download(unittest.TestCase):
    def run_dl(self, opener):
        return source.download("https://gutenberg.pglaf.org/cache/epub/1/pg1.txt", 1, opener=opener)

    def test_http_codes(self):
        def raising(code, ra=None):
            def opener(req, timeout): raise http_error(code, ra)
            return opener
        with self.assertRaises(source.BlockedSourceError) as c: self.run_dl(raising(403))
        self.assertTrue(c.exception.blocked and not c.exception.permanent)
        with self.assertRaises(source.BlockedSourceError) as c: self.run_dl(raising(429, 90))
        self.assertEqual((c.exception.code, c.exception.retry_after), (429, 90.0))
        with self.assertRaises(source.TransientSourceError) as c: self.run_dl(raising(503, 30))
        self.assertEqual(c.exception.retry_after, 30.0)
        with self.assertRaises(source.PermanentSourceError): self.run_dl(raising(404))

    def test_size_cap(self):
        with self.assertRaises(source.PermanentSourceError): self.run_dl(lambda req, timeout: FakeResponse(length=source.MAX_BYTES + 1))
        with mock.patch.object(source, "MAX_BYTES", 100), self.assertRaises(source.PermanentSourceError):
            self.run_dl(lambda req, timeout: FakeResponse(body=b"x" * 500))   # no Content-Length: still stopped while streaming


class Health(unittest.TestCase):
    def make(self, path=None):
        self.clock = Clock()
        return source.SourceHealth(path, clock=self.clock, rng=SimpleNamespace(random=lambda: 0.5))   # jitter factor 1.0

    def test_rest_escalates_then_probe_then_clears(self):
        h = self.make()
        for _ in range(2): h.fail("a")
        self.assertTrue(h.reserve("a"))                         # two failures: still asked
        h.fail("a"); self.assertAlmostEqual(h.rest_left("a"), 120, delta=1); self.assertFalse(h.reserve("a"))
        self.clock.t += 121
        self.assertTrue(h.reserve("a")); self.assertFalse(h.reserve("a"))     # exactly one probe
        h.fail("a"); self.assertAlmostEqual(h.rest_left("a"), 240, delta=1)   # probe failed: doubled
        for _ in range(20): self.clock.t += 4000; h.reserve("a"); h.fail("a")
        self.assertLessEqual(h.rest_left("a"), 3600)                          # capped at an hour
        self.clock.t += 4000; h.reserve("a"); h.ok("a")
        self.assertTrue(h.reserve("a")); self.assertEqual(h.snapshot(), {})

    def test_429_and_double_403_rest_long_and_retry_after_wins(self):
        h = self.make()
        h.fail("a", blocked=True, immediate=True); self.assertAlmostEqual(h.rest_left("a"), 1800, delta=1)
        h.fail("b", blocked=True); self.assertTrue(h.reserve("b"))             # a single stray 403 does not rest a mirror
        h.fail("b", blocked=True); self.assertAlmostEqual(h.rest_left("b"), 1800, delta=1)
        h.fail("c", retry_after=600); self.assertAlmostEqual(h.rest_left("c"), 600, delta=1)

    def test_state_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "health.json"
            h = self.make(path)
            self.assertEqual(h.state(["a", "b"]), "ok")
            for _ in range(3): h.fail("a")
            self.assertEqual(h.state(["a", "b"]), "degraded")
            for _ in range(3): h.fail("b")
            self.assertEqual(h.state(["a", "b"]), "down"); self.assertGreater(h.rest_left_all(["a", "b"]), 0)
            again = source.SourceHealth(path, clock=self.clock)                # a restart must not forget the rest
            self.assertEqual(again.state(["a", "b"]), "down")


class Chain(unittest.TestCase):
    def setUp(self):
        for k in ("LR_MIRROR_BASES", "LR_ALLOW_WWW"): os.environ.pop(k, None)
        self.health = source.SourceHealth(None, clock=Clock())

    def test_second_mirror_serves_when_first_fails(self):
        seen = []
        def dl(url, gid, **kw):
            seen.append(source._host(url))
            if "pglaf" in url: raise source.TransientSourceError("HTTP 503")
            return TEXT
        text = source.fetch_clean_text(BOOK, downloader=dl, health=self.health)
        self.assertGreater(len(text), 500)
        self.assertEqual(seen, ["gutenberg.pglaf.org", "aleph.gutenberg.org"])

    def test_resting_hosts_are_not_asked_and_do_not_count_against_the_book(self):
        calls = []
        def dl(url, gid, **kw): calls.append(url); raise source.TransientSourceError("HTTP 503")
        for _ in range(3):
            with self.assertRaises(source.TransientSourceError): source.fetch_clean_text(BOOK, downloader=dl, health=self.health)
        calls.clear()
        with self.assertRaises(source.SourceResting) as c: source.fetch_clean_text(BOOK, downloader=dl, health=self.health)
        self.assertEqual(calls, []); self.assertGreater(c.exception.wait, 0)

    def test_not_found_everywhere_is_permanent_but_not_if_a_host_was_skipped(self):
        def nf(url, gid, **kw): raise source.PermanentSourceError("HTTP 404")
        with self.assertRaises(source.PermanentSourceError): source.fetch_clean_text(BOOK, downloader=nf, health=self.health)
        for _ in range(3): self.health.fail("aleph.gutenberg.org")
        with self.assertRaises(source.SourceResting): source.fetch_clean_text(BOOK, downloader=nf, health=self.health)

    def test_404_marks_the_host_healthy_and_403_is_never_permanent(self):
        def blocked(url, gid, **kw): raise source.BlockedSourceError("HTTP 403", code=403)
        with self.assertRaises(source.TransientSourceError) as c: source.fetch_clean_text(BOOK, downloader=blocked, health=self.health)
        self.assertFalse(c.exception.permanent)


class PrepAndRuntime(unittest.TestCase):
    def worker(self):
        w = prep.PrepWorker.__new__(prep.PrepWorker)              # no database or shelf needed for these checks
        w._clock = Clock()
        w.settings = prep.Settings()
        w._fail_streak, w._breaker_until, w._breaker_level = 0, 0.0, 0
        w.health = source.SourceHealth(None, clock=w._clock)
        return w

    def test_breaker_pauses_double_and_reset_on_success(self):
        w = self.worker()
        def trip():
            for _ in range(w.settings.breaker_threshold): w._note_result(False, transient=True)
            return round(w.breaker_remaining())
        self.assertEqual(trip(), 120)
        w._clock.t += 121; self.assertEqual(trip(), 240)
        for _ in range(12): w._clock.t += 4000; last = trip()
        self.assertEqual(last, 3600)
        w._note_result(True); w._clock.t += 4000; self.assertEqual(trip(), 120)

    def test_source_state_follows_health(self):
        w = self.worker()
        self.assertEqual(w.source_state(), "ok")
        for _ in range(3): w.health.fail("gutenberg.pglaf.org")
        self.assertEqual(w.source_state(), "degraded"); self.assertFalse(w.breaker_open())
        for _ in range(3): w.health.fail("aleph.gutenberg.org")
        self.assertEqual(w.source_state(), "down"); self.assertTrue(w.breaker_open())

    def test_runtime_notice_and_pool(self):
        rt = runtime.Runtime.__new__(runtime.Runtime)
        rt.worker = self.worker()
        self.assertEqual(rt.source_notice(), "")
        for host in source.source_hosts():
            for _ in range(3): rt.worker.health.fail(host)
        self.assertIn("resting", rt.source_notice())
        rt.lake, rt.starters = lambda l: ("a",), lambda l: ("b", "c")
        self.assertEqual(rt.fallback_pool("en"), ("a", "b", "c"))


if __name__ == "__main__":
    unittest.main()
