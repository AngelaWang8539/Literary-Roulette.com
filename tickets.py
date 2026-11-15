"""Sealed passage tickets.

A ticket proves that the server itself drew a passage. It carries only opaque values
(language, the book's opaque token, a passage number, an expiry) and is signed, so the
browser can hold a few unopened tickets without learning the title or being able to forge one.
"""
import base64
import hashlib
import hmac
import json
import time

SIG_BYTES = 16


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(secret, body):
    return hmac.new(secret, body, hashlib.sha256).digest()[:SIG_BYTES]


def issue(secret, language, book_token, index, ttl_seconds, now=None):
    now = time.time() if now is None else now
    payload = {"l": language, "t": book_token, "i": int(index), "x": int(now + ttl_seconds)}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _b64(body) + "." + _b64(_sig(secret, body))


def verify(secret, ticket, now=None):
    """The payload dict, or None if the ticket is malformed, forged or expired."""
    if not isinstance(ticket, str) or ticket.count(".") != 1 or len(ticket) > 300:
        return None
    try:
        body_part, sig_part = ticket.split(".")
        body, sig = _unb64(body_part), _unb64(sig_part)
        if not hmac.compare_digest(sig, _sig(secret, body)):
            return None
        payload = json.loads(body.decode("utf-8"))
        now = time.time() if now is None else now
        if not isinstance(payload, dict) or int(payload["x"]) < now:
            return None
        return {"language": payload["l"], "book_token": payload["t"], "index": int(payload["i"]), "expires": int(payload["x"])}
    except (ValueError, KeyError, TypeError):
        return None
