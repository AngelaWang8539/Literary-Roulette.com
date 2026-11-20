import unittest
from flask import Flask, jsonify
import hardening


def make(env=None):
    app = Flask(__name__); app.secret_key = "x"
    @app.get("/")
    def home(): return "hi"
    @app.post("/api/event")
    def ev(): return jsonify(ok=True)
    @app.get("/api/ocean")
    def oc(): return jsonify(ok=True)
    hardening.apply(app, env or {})
    return app


class Hardening(unittest.TestCase):
    def test_headers(self):
        r = make().test_client().get("/")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)
        self.assertNotIn("Content-Security-Policy", r.headers)
        self.assertNotIn("Strict-Transport-Security", r.headers)

    def test_csp_modes(self):
        self.assertIn("Content-Security-Policy", make({"LR_CSP": "enforce"}).test_client().get("/").headers)
        self.assertNotIn("Content-Security-Policy-Report-Only", make({"LR_CSP": "off"}).test_client().get("/").headers)

    def test_hsts_only_on_https_in_production(self):
        c = make({"LR_PRODUCTION": "1"}).test_client()
        self.assertNotIn("Strict-Transport-Security", c.get("/").headers)
        self.assertIn("Strict-Transport-Security", c.get("/", base_url="https://x.test").headers)

    def test_cookie_flags(self):
        self.assertTrue(make({"LR_PRODUCTION": "1"}).config["SESSION_COOKIE_SECURE"])
        a = make(); self.assertFalse(a.config["SESSION_COOKIE_SECURE"]); self.assertEqual(a.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_rate_limit_events_and_isolation(self):
        c = make({"LR_RATE_EVENT": "3", "LR_RATE_API": "100"}).test_client()
        codes = [c.post("/api/event", json={}).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        r = c.post("/api/event", json={}); self.assertEqual(r.get_json()["code"], "rate_limited"); self.assertIn("Retry-After", r.headers)
        self.assertEqual(c.get("/api/ocean").status_code, 200)         # other buckets are unaffected
        self.assertEqual(c.get("/").status_code, 200)                  # pages are never limited

    def test_rate_limit_off(self):
        c = make({"LR_RATE_EVENT": "0"}).test_client()
        self.assertTrue(all(c.post("/api/event", json={}).status_code == 200 for _ in range(50)))

    def test_body_cap(self):
        c = make().test_client()
        self.assertEqual(c.post("/api/event", data=b"x" * 70000, content_type="application/json").status_code, 413)

    def test_limiter_window_and_memory(self):
        t = [0.0]; rl = hardening.RateLimiter(clock=lambda: t[0], max_keys=10)
        self.assertTrue(rl.allow("a", 1)[0]); self.assertFalse(rl.allow("a", 1)[0])
        t[0] = 61; self.assertTrue(rl.allow("a", 1)[0])
        for i in range(30): rl.allow(i, 1)
        self.assertLess(len(rl.hits), 31)


if __name__ == "__main__":
    unittest.main()
