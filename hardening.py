"""Launch hardening in one place: security headers, cookie flags, proxy awareness, body-size cap, rate limits.

Wire it in with ONE line inside create_app(), just before `return app`:
    import hardening; hardening.apply(app)

Everything is switched by environment variables, so local development stays exactly as it is:
    LR_PRODUCTION=1        Secure session cookie, HSTS on https
    LR_TRUSTED_PROXIES=1   how many proxies sit in front (Cloudflare/nginx/the host's router); fixes client IP and https
    LR_CSP=report|enforce|off   default "report": violations are only logged in the browser Console, nothing breaks
    LR_RATE_EVENT=120      /api/event requests per minute per visitor (0 = off)
    LR_RATE_API=240        all other /api/ requests per minute per visitor (0 = off)
The rate limiter lives in each server process. It is a seatbelt; the real limit belongs at the proxy or CDN.
"""
import os
import time
from collections import deque

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class RateLimiter:
    """Sliding one-minute window per (visitor, bucket). Bounded memory."""
    def __init__(self, clock=time.monotonic, max_keys=20000):
        self.clock, self.max_keys, self.hits = clock, max_keys, {}

    def allow(self, key, limit, window=60.0):
        if limit <= 0:
            return True, 0
        now = self.clock()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, int(window - (now - q[0])) + 1)
        q.append(now)
        if len(self.hits) > self.max_keys:                       # bounded memory: quiet visitors first, then the oldest
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > window]:
                self.hits.pop(k, None)
            while len(self.hits) > self.max_keys:
                self.hits.pop(next(iter(self.hits)), None)
        return True, 0


def apply(app, env=os.environ):
    from flask import jsonify, request
    production = env.get("LR_PRODUCTION") == "1"

    # 1) cookies: never readable by scripts, not sent on cross-site requests, https-only in production
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=production)

    # 2) the API only ever receives small JSON bodies (uploaded books never leave the browser)
    if not app.config.get("MAX_CONTENT_LENGTH"):
        app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    # 3) behind a proxy: real client address and https scheme
    proxies = _int("LR_TRUSTED_PROXIES", 0) if env is os.environ else int(env.get("LR_TRUSTED_PROXIES", 0))
    if proxies > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)

    # 4) rate limits
    limiter = RateLimiter()
    app.extensions["lr_limiter"] = limiter
    rate_event, rate_api = int(env.get("LR_RATE_EVENT", 120)), int(env.get("LR_RATE_API", 240))

    @app.before_request
    def _limit():
        path = request.path
        if not path.startswith("/api/"):
            return None
        cap = app.config["MAX_CONTENT_LENGTH"]
        if cap and request.content_length and request.content_length > cap:       # refuse oversized bodies before reading them
            resp = jsonify(ok=False, code="too_large", message="That request is too large.")
            resp.status_code = 413
            return resp
        bucket, limit = ("event", rate_event) if path == "/api/event" else ("api", rate_api)
        ok, wait = limiter.allow((request.remote_addr or "?", bucket), limit)
        if ok:
            return None
        resp = jsonify(ok=False, code="rate_limited", message="Too many requests. Please slow down for a moment.")
        resp.status_code = 429
        resp.headers["Retry-After"] = str(wait)
        return resp

    # 5) security headers on every response
    csp_mode = env.get("LR_CSP", "report")

    @app.after_request
    def _headers(resp):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if csp_mode == "enforce":
            h.setdefault("Content-Security-Policy", CSP)
        elif csp_mode == "report":
            h.setdefault("Content-Security-Policy-Report-Only", CSP)
        if request.is_secure and production:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp
    return app
