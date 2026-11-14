"""Literary Roulette web 0.8: a random ocean of Project Gutenberg books, prepared once and shared by everyone.

Every spin is a uniform draw from the whole catalogue (universe.py, built from Gutenberg's pg_catalog.csv.gz). A book
that has been prepared before is instant; a cold one is fetched once by a background worker while the visitor watches
the librarian with real progress, then it stays in the shared library for every later reader. See README.md.
"""
import os
import random
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session

import analytics
import draw
import prep
import tickets
from library import LINK_KINDS
from runtime import LANGUAGES, Runtime

SITE = {"creator_name": "[Your Name]", "contact_email": "[rights contact email to be added]"}
EVENT_NAMES = ("passage_displayed", "another_passage_requested", "another_book_requested", "book_hidden",
               "book_saved", "awareness_known", "awareness_unknown", "awareness_unsure")
EVENT_INTEREST = {"passage_displayed": "displayed", "another_passage_requested": "another", "book_saved": "saved"}
MAX_SEEN_TOKENS = 300
STAGE_TEXT = {"queued": "Waiting in the queue", "fetching": "Fetching this edition", "cleaning": "Cleaning the text",
              "indexing": "Indexing passages", "ready": "Ready"}


def _env_flag(name, default):
    return os.environ.get(name, "1" if default else "0") == "1"


def create_app(test_config=None):
    base = Path(__file__).resolve().parent
    app = Flask(__name__)
    app.config.update(
        BASE_DIR=base, DATA_DIR=base / "data", INSTANCE_DIR=base / "instance", SHELF_DIR=None, DB_PATH=None,
        DEFAULT_LANGUAGE="en", RNG=None, SITE=SITE,
        # Rights stay a separate gate from technical checks. While books.json still says
        # "pending_verification", this lets you test locally. Set LR_ALLOW_PENDING_RIGHTS=0 to go public.
        ALLOW_PENDING_RIGHTS=_env_flag("LR_ALLOW_PENDING_RIGHTS", True),
        EMBEDDED_WORKER=_env_flag("LR_EMBEDDED_WORKER", True),
        AUTO_REFRESH_CATALOGUE=True, CATALOGUE_MAX_AGE_HOURS=float(os.environ.get("LR_CATALOGUE_MAX_AGE_HOURS", "24")),
        TICKET_TTL=int(os.environ.get("LR_TICKET_TTL", "7200")),
        BATCH_SIZE=int(os.environ.get("LR_BATCH_SIZE", "3")), MAX_BATCH=5,
        CATALOG_CSV=None, OFFLINE_STARTER=_env_flag("LR_OFFLINE_STARTER", False),
        PREP_SETTINGS=None, FETCHER=None, ADMIN_TOKEN=os.environ.get("LR_ADMIN_TOKEN"),
    )
    if test_config:
        app.config.update(test_config)

    rt = Runtime(app.config)
    app.runtime, app.catalogues, app.shelf = rt, rt.catalogues, rt.shelf
    app.secret_key = rt.secret
    secret_bytes = rt.secret.encode("utf-8")
    instance_dir = Path(app.config["INSTANCE_DIR"])
    db_path = app.config["DB_PATH"] or instance_dir / "analytics.db"
    for language, cat in rt.catalogues.items():
        app.logger.info("%s: %d books cleared; %d with verified local text; %d prepared and ready.",
                        language, len(cat.books), len(cat.local_texts), len(rt.starters(language)) + len(rt.lake(language)))
        for problem in cat.problems:
            app.logger.warning("%s catalogue: %s", language, problem)
    if app.config["ALLOW_PENDING_RIGHTS"]:
        app.logger.warning("Serving books whose rights_status is 'pending_verification' (LR_ALLOW_PENDING_RIGHTS=1). "
                           "Clear each book's rights before a public launch.")
    analytics.init_db(db_path)
    if not app.testing:
        analytics.start_writer(db_path)

    if not app.testing:
        # Threads that prepare books always run here, so a visitor's cold pick is never stranded. With
        # LR_EMBEDDED_WORKER=1 (default) this process also opens/refreshes the catalogue and grows the library;
        # with 0, run `python worker.py` once for that.
        rt.worker.seed_local()
        rt.worker.start(scheduler=app.config["EMBEDDED_WORKER"])
        if app.config["EMBEDDED_WORKER"] and app.config["AUTO_REFRESH_CATALOGUE"]:
            rt.sync.start()

    _next_meta_check = [0.0]
    _meta_lock = threading.Lock()

    @app.before_request
    def pick_up_changes_from_other_processes():
        if not request.path.startswith("/api/"):
            return
        if rt.shelf.refresh_if_changed():
            rt.sync_external()
        now = time.monotonic()
        if now >= _next_meta_check[0] and _meta_lock.acquire(blocking=False):
            try:
                _next_meta_check[0] = now + 10
                if rt.universe.reload():
                    rt.attach_popularity()
            finally:
                _meta_lock.release()

    def rng():
        return app.config["RNG"] or random

    def active_language():
        code = session.get("language", app.config["DEFAULT_LANGUAGE"])
        return code if code in LANGUAGES else "en"

    def body_json():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    def language_from(body):
        code = body.get("language")
        return code if code in LANGUAGES else active_language()

    def fail(code, message, status=400, **extra):
        return jsonify(ok=False, code=code, message=message, **extra), status

    def clean_tokens(value, limit=MAX_SEEN_TOKENS):
        if not isinstance(value, list):
            return []
        return [v for v in value[:limit] if isinstance(v, str) and len(v) <= 40]

    def find_book(book_token, language=None):
        """(language, catalogue, book) from an opaque book token. A dictionary lookup, no file I/O."""
        order = [language] if language in rt.catalogues else []
        order += [code for code in rt.catalogues if code not in order]
        for code in order:
            book = rt.catalogues[code].book_by_token(book_token)
            if book and rt.shelf.has(code, book["id"]):
                return code, rt.catalogues[code], book
        return None

    def not_ready(language):
        return fail("empty_shelf", f"No {LANGUAGES[language]} books are ready yet. The librarian is opening the catalogue; "
                    "try again in a moment, or run `python tools/prepare_shelf.py`.", 503)

    def ocean_state(language):
        sync = rt.sync.status()
        return {"mode": rt.mode(language), "language": language, "total": rt.universe.count(language),
                "totals": {code: rt.universe.count(code) for code in LANGUAGES},
                "library": len(rt.lake(language)), "library_target": rt.worker.lake_target(language),
                "catalogue": {"state": sync.get("state"), "running": bool(sync.get("running")), "message": sync.get("message"),
                              "percent": sync.get("percent"), "error": sync.get("error")}}

    def make_ticket(language, cat, bid, index):
        text = rt.shelf.read_passage(language, bid, index)
        if text is None:
            return None
        book_token = cat.book_token(bid)
        return {"ticket": tickets.issue(secret_bytes, language, book_token, index, app.config["TICKET_TTL"]),
                "token": cat.passage_key(bid, index), "book_token": book_token, "language": language, "text": text}

    # ---- pages -------------------------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", site=app.config["SITE"], language=active_language())

    @app.get("/about")
    def about():
        return render_template("about.html", site=app.config["SITE"], language=active_language())

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, ready=rt.ready_counts(), mode={code: rt.mode(code) for code in LANGUAGES})

    # ---- status ------------------------------------------------------------------------------
    @app.get("/api/catalogue/status")
    def catalogue_refresh_status():
        return jsonify(rt.sync.status())

    @app.get("/api/shelf/status")
    def shelf_status():
        detail = bool(app.config["ADMIN_TOKEN"]) and request.headers.get("X-LR-Admin") == app.config["ADMIN_TOKEN"]
        data = rt.worker.status(detail=detail)
        data["ready"] = rt.ready_counts()
        data["mode"] = {code: rt.mode(code) for code in LANGUAGES}
        return jsonify(ok=True, **data)

    @app.get("/api/languages")
    def languages():
        return jsonify(ok=True, active=active_language(), languages=[
            {"code": code, "label": label, "ready_books": rt.universe.count(code) if rt.mode(code) == "ocean" else len(rt.starters(code))}
            for code, label in LANGUAGES.items()])

    @app.post("/api/language")
    def set_language():
        code = body_json().get("language")
        if code not in LANGUAGES:
            return fail("unknown_language", "Choose English, French or Chinese.")
        session["language"] = code
        return jsonify(ok=True, language=code, label=LANGUAGES[code], ready_books=rt.universe.count(code) if rt.mode(code) == "ocean" else len(rt.starters(code)))

    # ---- the read path -----------------------------------------------------------------------
    @app.get("/api/ocean")
    def ocean():
        language = request.args.get("language")
        return jsonify(ok=True, **ocean_state(language if language in LANGUAGES else active_language()))

    def random_ticket(language, cat, bid):
        total = rt.shelf.passage_count(language, bid)
        index = draw.pick_passage_index(total, lambda i: cat.passage_key(bid, i), set(), rng()) if total > 1 else (0 if total else None)
        return make_ticket(language, cat, bid, index) if index is not None else None

    @app.post("/api/draw")
    def draw_tickets():
        """A few sealed, anonymous tickets: different books, each a uniform draw from the whole catalogue.

        A book prepared before comes back at once. A cold one is queued for the worker and returned as a `pending`
        handle to poll while the librarian works. If the queue is already too long to be fair to the visitor
        (estimated_wait > max_cold_wait), the pick is swapped for a random book from the shared library instead."""
        body = body_json()
        language = language_from(body)
        cat = rt.catalogues[language]
        try:
            count = int(body.get("count") or app.config["BATCH_SIZE"])
        except (TypeError, ValueError):
            count = app.config["BATCH_SIZE"]
        count = max(1, min(count, app.config["MAX_BATCH"]))
        seen = set(clean_tokens(body.get("seen")))
        avoid = {b["id"] for t in clean_tokens(body.get("avoid"), 20) if (b := cat.book_by_token(t))}
        mode = rt.mode(language)
        if mode == "opening":
            sync = rt.sync.status()
            return fail("catalogue_opening", "The librarian is opening the catalogue.", 503, retry_after=1,
                        progress={"message": sync.get("message") or "Opening the catalogue\u2026", "percent": sync.get("percent")})
        out, pending, restarted, shed = [], [], False, 0
        if mode == "starter":                    # the catalogue could not be opened: a small bundled shelf, said out loud
            ids = rt.starters(language)
            if not ids:
                return not_ready(language)
            picked, restarted = draw.pick_books(ids, cat.book_token, count, seen, rng(), avoid)
            out = [t for t in (random_ticket(language, cat, bid) for bid in picked) if t]
        else:
            records = rt.universe.random_candidates(language, count, rng(), reject=lambda r: cat.book_token(r["id"]) in seen or r["id"] in avoid)
            total = rt.universe.count(language)
            for record in records:
                ready = rt.ready_book_id(language, record["gutenberg_id"])
                if ready:
                    ticket = random_ticket(language, cat, ready)
                    if ticket:
                        out.append(ticket)
                elif rt.worker.estimated_wait() <= rt.worker.settings.max_cold_wait:
                    if not rt.worker.enqueue(prep.COLD, language, record, priority=0):
                        rt.worker.bump(language, record["id"])
                    pending.append({"handle": cat.book_token(record["id"]), "draw_number": record["draw_number"], "total": total})
                else:
                    shed += 1
            if shed:                              # overload protection: still random, from what is already prepared
                taken = {t["book_token"] for t in out} | {p["handle"] for p in pending}
                lake = rt.lake(language)
                picked, _ = draw.pick_books(lake, cat.book_token, shed, seen | taken, rng(), avoid)
                out += [t for t in (random_ticket(language, cat, bid) for bid in picked) if t]
                if not out and not pending:       # nothing prepared yet and the queue is long: the bundled shelf, said out loud
                    picked, _ = draw.pick_books(rt.starters(language), cat.book_token, 1, seen, rng(), avoid)
                    out = [t for t in (random_ticket(language, cat, bid) for bid in picked) if t]
        if not out and not pending:
            return fail("only_one_book", "Only one usable book is available in this collection.", 200)
        return jsonify(ok=True, mode=mode, language=language, tickets=out, pending=pending, shed=shed,
                       restarted=restarted, total=rt.universe.count(language), ready_books=len(rt.lake(language)))

    @app.post("/api/draw/poll")
    def draw_poll():
        """Progress of picks that are still being prepared. Real stages, real percentages."""
        body = body_json()
        language = language_from(body)
        cat = rt.catalogues[language]
        results = []
        for handle in clean_tokens(body.get("handles"), 8):
            book = cat.book_by_token(handle)
            if book and rt.shelf.has(language, book["id"]):
                ticket = random_ticket(language, cat, book["id"])
                results.append({"handle": handle, "state": "ready" if ticket else "failed", "percent": 100, "ticket": ticket})
                continue
            job = rt.worker.find_job(language, handle)
            if job:
                results.append({"handle": handle, "state": "working", "stage": job.stage, "percent": job.percent,
                                "message": STAGE_TEXT.get(job.stage, "Working")})
            else:
                results.append({"handle": handle, "state": "failed",
                                "message": "That edition could not be fetched, so the librarian is drawing another."})
        return jsonify(ok=True, results=results)

    @app.post("/api/passage/another")
    def another_passage():
        body = body_json()
        found = find_book(body.get("book_token"), body.get("language"))
        if not found:
            return fail("unknown_book", "That book is no longer on the shelf. Spin for a new one.")
        language, cat, book = found
        bid, current = book["id"], None
        payload = tickets.verify(secret_bytes, body.get("ticket"))
        if payload and payload["book_token"] == body.get("book_token"):
            current = payload["index"]
        total = rt.shelf.passage_count(language, bid)
        index = draw.pick_passage_index(total, lambda i: cat.passage_key(bid, i),
                                        set(clean_tokens(body.get("seen"))), rng(), current)
        if index is None:
            return fail("no_other_passage", "This book has no other passage available. Reveal it or spin another book.", 200,
                        language=language)
        ticket = make_ticket(language, cat, bid, index)
        if not ticket:
            return fail("unknown_book", "That book is no longer on the shelf. Spin for a new one.")
        return jsonify(ok=True, **ticket)

    @app.post("/api/book/reveal")
    def reveal():
        """Metadata only. Never touches the book text."""
        body = body_json()
        found = find_book(body.get("book_token"), body.get("language"))
        if not found:
            return fail("unknown_book", "That book is no longer available. Spin for a new one.")
        language, cat, book = found
        payload = tickets.verify(secret_bytes, body.get("ticket"))
        index = payload["index"] if payload and payload["book_token"] == body.get("book_token") else None
        analytics.log_event(db_path, "book_revealed", book["id"], f"{book['id']}:{index}" if index is not None else None)
        rt.shelf.note_interest(language, book["id"], "reveal")
        return jsonify(ok=True, language=language, book=cat.book_payload(book, index))

    @app.post("/api/event")
    def event():
        body = body_json()
        name = body.get("event")
        if name not in EVENT_NAMES:
            return fail("unknown_event", "That event is not accepted.")
        found, passage_id = find_book(body.get("book_token"), body.get("language")), None
        payload = tickets.verify(secret_bytes, body.get("ticket"))
        if payload and found and payload["book_token"] == body.get("book_token"):
            passage_id = f"{found[2]['id']}:{payload['index']}"
        if not found:
            return fail("unknown_book", "That book is not on the shelf.")
        language, _cat, book = found
        analytics.log_event(db_path, name, book["id"], passage_id, book.get("is_demo") is True)
        if name in EVENT_INTEREST:
            rt.shelf.note_interest(language, book["id"], EVENT_INTEREST[name])
        return jsonify(ok=True, language=language)

    @app.get("/go/<kind>/<token>")
    def go(kind, token):
        found = find_book(token) if kind in LINK_KINDS else None
        url = found[1].links_for(found[2]).get(kind) if found else None
        if not url:
            return render_template("about.html", site=app.config["SITE"], missing_link=True,
                                   language=active_language()), 404
        language, _cat, book = found
        analytics.log_event(db_path, f"{kind}_link_clicked", book["id"], None, book.get("is_demo") is True)
        rt.shelf.note_interest(language, book["id"], "link")
        return redirect(url, 302)

    @app.after_request
    def no_store_for_api(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


if __name__ == "__main__":
    create_app().run(debug=False, port=5000, threaded=True)
