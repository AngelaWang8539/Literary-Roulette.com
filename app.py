"""Literary Roulette 0.3.0 — English, French and Chinese collections."""
import json
import os
import random
import secrets
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session

import analytics
from catalogue import LINK_KINDS, Catalogue, load_json_list
from gutenberg_pool import GutendexPool
from catalogue_refresh import catalogue_status, reload_pool_if_changed, start_catalogue_refresh

SITE = {"creator_name": "[Your Name]", "contact_email": "[rights contact email to be added]"}
MAX_SEEN = 150
LANGUAGES = {"en": "English", "fr": "Français", "zh": "中文"}


def load_secret(instance_dir):
    env = os.environ.get("LR_SECRET_KEY")
    if env:
        return env
    instance_dir.mkdir(parents=True, exist_ok=True)
    path = instance_dir / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _manifest(folder):
    try:
        value = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def create_app(test_config=None):
    base = Path(__file__).resolve().parent
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=base / "data", INSTANCE_DIR=base / "instance", DB_PATH=None,
        ALLOW_DEMO=os.environ.get("LR_ALLOW_DEMO", "0") == "1",
        DEFAULT_LANGUAGE="en", RNG=None, SITE=SITE,
        GUTENBERG_CATALOG_PATH=None, GUTENBERG_CACHE_DIR=None,
        AUTO_REFRESH_CATALOGUE=True, CATALOGUE_MAX_AGE_HOURS=24,
        MAX_LOADED_BOOKS_PER_LANGUAGE=2, GUTENBERG_CACHE_MAX_FILES=8,
        GUTENBERG_CACHE_MAX_BYTES=64 * 1024 * 1024,
        GUTENBERG_CACHE_TTL_DAYS=14,
    )
    if test_config:
        app.config.update(test_config)

    data_dir = Path(app.config["DATA_DIR"])
    instance_dir = Path(app.config["INSTANCE_DIR"])
    app.secret_key = load_secret(instance_dir)
    db_path = app.config["DB_PATH"] or Path(app.config["INSTANCE_DIR"]) / "analytics.db"
    problems = []
    catalogues = {}
    gutendex_path = Path(app.config.get("GUTENBERG_CATALOG_PATH") or data_dir / "gutendex_catalog.json")
    gutendex_cache = Path(app.config.get("GUTENBERG_CACHE_DIR") or Path(app.config["INSTANCE_DIR"]) / "gutenberg_cache")
    gutendex = GutendexPool(
        gutendex_path, gutendex_cache,
        max_cache_files=int(app.config["GUTENBERG_CACHE_MAX_FILES"]),
        max_cache_bytes=int(app.config["GUTENBERG_CACHE_MAX_BYTES"]),
        cache_ttl_seconds=int(app.config["GUTENBERG_CACHE_TTL_DAYS"]) * 24 * 3600,
    )

    for language, label in LANGUAGES.items():
        folder = data_dir if language == "en" else data_dir / language
        texts_dir = Path(app.config.get(f"{language.upper()}_TEXTS_DIR") or folder / "texts")
        if language == "en" and app.config.get("TEXTS_DIR"):
            texts_dir = Path(app.config["TEXTS_DIR"])
        books_override = app.config.get("BOOKS") if language == "en" else None
        passages_override = app.config.get("PASSAGES") if language == "en" else None
        books = books_override if books_override is not None else load_json_list(folder / "books.json", problems)
        passages = passages_override if passages_override is not None else load_json_list(folder / "passages.json", problems)
        if language == "en" and app.config["ALLOW_DEMO"] and books_override is None:
            books += load_json_list(folder / "demo_books.json", problems)
            if passages_override is None:
                passages += load_json_list(folder / "demo_passages.json", problems)
        cat = Catalogue(
            books, passages, app.secret_key, app.config["ALLOW_DEMO"],
            base / "static" / "covers", texts_dir, _manifest(texts_dir), language=language,
            lazy_text=True,
            max_loaded_books=int(app.config["MAX_LOADED_BOOKS_PER_LANGUAGE"]),
        )
        catalogues[language] = cat
        app.logger.info("%s: %d books ready; %d waiting for verified text.", label, len(cat.book_ids), len(cat.unavailable))
        for problem in cat.problems:
            app.logger.warning("%s catalogue: %s", language, problem)

    app.catalogues = catalogues
    app.catalogue = catalogues["en"]  # backward-compatible alias for existing tests/extensions
    app.gutendex_pool = gutendex
    unmatched_popularity = gutendex.attach_popularity(catalogues)
    if gutendex.is_partial_snapshot:
        app.logger.warning(
            "Project Gutenberg metadata is a page-limited preview (%s); popularity badges are hidden. "
            "Run `python3 tools/sync_gutenberg_rdf.py` for a complete catalogue and full rankings.",
            gutendex.partial_reason,
        )
    for item in unmatched_popularity:
        app.logger.warning(
            "No matching Project Gutenberg popularity record found for curated book %s (Gutenberg #%s, language=%s). "
            "Check the metadata refresh and the record's language, rights and text-format eligibility.",
            item["title"], item["gutenberg_id"], item["language"],
        )
    for language, cat in catalogues.items():
        if gutendex.count(language):
            app.logger.info("%s Project Gutenberg metadata: %d titles available for on-demand selection.", language, gutendex.count(language))
    analytics.init_db(db_path)

    # The last verified metadata snapshot is served immediately. A single background
    # worker refreshes it only when older than the configured threshold.
    catalogue_status_path = instance_dir / "gutenberg_catalogue_status.json"
    catalogue_lock_path = instance_dir / "gutenberg_catalogue_refresh.lock"

    def reload_catalogue_after_refresh():
        reload_pool_if_changed(gutendex, catalogues, force=True)

    start_catalogue_refresh(
        project_root=base,
        snapshot_path=gutendex_path,
        archive_path=instance_dir / "gutenberg_rdf" / "rdf-files.tar.bz2",
        status_path=catalogue_status_path,
        lock_path=catalogue_lock_path,
        after_success=reload_catalogue_after_refresh,
        logger=app.logger,
        max_age_hours=float(app.config.get("CATALOGUE_MAX_AGE_HOURS", 24)),
        enabled=bool(app.config.get("AUTO_REFRESH_CATALOGUE", True)) and not app.testing,
    )

    @app.before_request
    def reload_catalogue_if_another_worker_updated_it():
        if reload_pool_if_changed(gutendex, catalogues):
            app.logger.info("Loaded a newly published Gutenberg metadata snapshot.")

    def rng():
        return app.config["RNG"] or random

    def active_language():
        code = session.get("language", app.config["DEFAULT_LANGUAGE"])
        return code if code in LANGUAGES else "en"

    @app.before_request
    def migrate_legacy_english_session():
        # Preserve English seen/revealed state from the single-language 0.3.0 app.
        for old_key, new_key in (("seen_p", "seen_p:en"), ("seen_b", "seen_b:en"), ("revealed", "revealed:en")):
            if old_key in session and new_key not in session:
                session[new_key] = session[old_key]
            session.pop(old_key, None)

    def session_key(kind, language):
        return f"{kind}:{language}"

    def seen(kind, language):
        return session.get(session_key(kind, language), [])

    def remember(kind, language, value):
        key = session_key(kind, language)
        items = [v for v in seen(kind, language) if v != value] + [value]
        session[key] = items[-MAX_SEEN:]

    def fail(code, message, status=400):
        return jsonify(ok=False, code=code, message=message), status

    def find_passage(token, book_token=None):
        if not isinstance(token, str):
            return None

        # Fast path: the current passage is already in the small active cache.
        for language, cat in catalogues.items():
            passage = cat.passage_by_token(token)
            if passage:
                return language, cat, passage

        # A passage token can be rehydrated from its stable book token. This is
        # targeted: only that one book is loaded, never every cached book file.
        if isinstance(book_token, str):
            resolved = gutendex.resolve_book_token(book_token, catalogues)
            if resolved:
                language, cat, book = resolved
                bid = book["id"]
                if bid in cat.text_books:
                    cat.ensure_book_loaded(bid)
                else:
                    gutendex.ensure_external_book_loaded(language, cat, book)
                passage = cat.passage_by_token(token)
                if passage:
                    return language, cat, passage

        # Legacy clients may send only passage tokens. Keep a very small,
        # best-effort fallback for the newest text cache file.
        for language, cat in catalogues.items():
            gutendex.restore_cached_books(language, cat)
            passage = cat.passage_by_token(token)
            if passage:
                return language, cat, passage
        return None

    def current_passage():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None
        return find_passage(body.get("token"), body.get("book_token"))

    def record(cat, event, passage):
        book = cat.book_of(passage)
        analytics.log_event(db_path, event, book["id"], passage["id"], book.get("is_demo") is True)

    def show(language, cat, passage, **extra):
        remember("seen_p", language, cat.passage_token(passage))
        book = cat.book_of(passage)
        record(cat, "passage_displayed", passage)
        data = {
            "ok": True, "language": language, "language_label": LANGUAGES[language],
            "token": cat.passage_token(passage), "text": passage["text"],
            "book_token": cat.book_token(book),
            "content_warning": passage.get("content_warning") or None,
            "spoiler_level": passage.get("spoiler_level") or None,
        }
        if session.get(session_key("revealed", language)) == cat.book_token(book):
            data["book"] = cat.book_payload(book, passage)
        data.update(extra)
        return jsonify(data)

    @app.get("/")
    def index():
        return render_template("index.html", site=app.config["SITE"], language=active_language())

    @app.get("/about")
    def about():
        return render_template("about.html", site=app.config["SITE"], language=active_language())

    @app.get("/api/catalogue/status")
    def catalogue_refresh_status():
        return jsonify(catalogue_status(catalogue_status_path, gutendex_path, gutendex))

    @app.get("/api/languages")
    def languages():
        return jsonify(ok=True, active=active_language(), languages=[
            {"code": code, "label": label, "ready_books": gutendex.available_count(code, catalogues[code])}
            for code, label in LANGUAGES.items()
        ])

    @app.post("/api/language")
    def set_language():
        code = (request.get_json(silent=True) or {}).get("language")
        if code not in LANGUAGES:
            return fail("unknown_language", "Choose English, French or Chinese.")
        session["language"] = code
        return jsonify(ok=True, language=code, label=LANGUAGES[code],
                       ready_books=gutendex.available_count(code, catalogues[code]))

    @app.get("/api/passage/start")
    def start():
        language = active_language()
        cat = catalogues[language]
        if not cat.book_ids and not gutendex.count(language):
            return fail("empty_catalogue", f"No {LANGUAGES[language]} books are ready yet. The metadata catalogue may still be downloading; wait for the librarian status to finish, then try again.", 503)
        session[session_key("seen_p", language)] = []
        session[session_key("seen_b", language)] = []
        session[session_key("revealed", language)] = None
        try:
            if gutendex.count(language):
                passage, _restarted = gutendex.pick_passage(language, cat, rng(), set())
                if passage is None:
                    passage = cat.pick_first(rng())
            else:
                passage = cat.pick_first(rng())
        except Exception as exc:
            app.logger.warning("On-demand Gutenberg selection failed; trying the verified local shelf: %s", exc)
            if not cat.book_ids:
                app.logger.exception("No local/cached fallback book is available")
                return fail("gutenberg_fetch_failed", "The selected book could not be loaded and there is no saved book ready in this language yet. Please try again shortly.", 502)
            passage = cat.pick_first(rng())
            remember("seen_b", language, cat.book_token(cat.book_of(passage)))
            return show(language, cat, passage, notice="One library text was temporarily unavailable, so we opened a book from the saved shelf instead.")
        remember("seen_b", language, cat.book_token(cat.book_of(passage)))
        return show(language, cat, passage)

    @app.post("/api/passage/another")
    def another_passage():
        found = current_passage()
        if not found:
            return fail("unknown_passage", "That passage is no longer available. Spin for a new one.")
        language, cat, passage = found
        record(cat, "another_passage_requested", passage)
        nxt = cat.pick_other_passage(passage, set(seen("seen_p", language)), rng())
        if nxt is None:
            return jsonify(ok=False, code="no_other_passage", language=language,
                           message="This book has no other passage available. Reveal it or spin another book.")
        return show(language, cat, nxt)

    @app.post("/api/book/reveal")
    def reveal():
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        book_token = body.get("book_token")
        found = find_passage(body.get("token"), book_token) if body.get("token") else None

        # Reveal is a metadata operation. It can succeed with the stable book
        # token alone, without fetching or splitting the complete book text.
        resolved = gutendex.resolve_book_token(book_token, catalogues) if book_token else None
        if resolved:
            language, cat, book = resolved
            passage = found[2] if found and found[0] == language and found[1].book_of(found[2])["id"] == book["id"] else None
        elif found:
            language, cat, passage = found
            book = cat.book_of(passage)
        else:
            return fail("unknown_book", "That book is no longer available. Spin for a new passage.")

        session[session_key("revealed", language)] = cat.book_token(book)
        if passage:
            record(cat, "book_revealed", passage)
        else:
            analytics.log_event(db_path, "book_revealed", book["id"], None, book.get("is_demo") is True)
        return jsonify(
            ok=True, language=language, token=cat.passage_token(passage) if passage else body.get("token"),
            book_token=cat.book_token(book), book=cat.book_payload(book, passage),
        )

    @app.post("/api/book/spin")
    def spin():
        found = current_passage()
        if not found:
            return fail("unknown_passage", "That passage is no longer available. Spin for a new one.")
        language, cat, passage = found
        record(cat, "another_book_requested", passage)
        extra = {}
        try:
            if gutendex.count(language):
                nxt, restarted = gutendex.pick_passage(
                    language, cat, rng(), set(seen("seen_b", language)), current_book_id=passage["book_id"]
                )
            else:
                bid, restarted = cat.pick_other_book(
                    passage["book_id"], set(seen("seen_b", language)), rng()
                )
                nxt = cat.pick_in_book(bid, set(seen("seen_p", language)), rng()) if bid else None
        except Exception as exc:
            app.logger.warning("On-demand Gutenberg spin failed; trying the verified local shelf: %s", exc)
            gutendex.restore_cached_books(language, cat)
            bid, restarted = cat.pick_other_book(
                passage["book_id"], set(seen("seen_b", language)), rng()
            )
            nxt = cat.pick_in_book(bid, set(seen("seen_p", language)), rng()) if bid else None
            if nxt is not None:
                extra["notice"] = "One library text was temporarily unavailable, so we opened another saved book instead."
            else:
                app.logger.exception("No alternative local/cached book is available")
                return fail("gutenberg_fetch_failed", "The next library text could not be loaded. Your current passage is safe; please try spinning again shortly.", 502)
        if nxt is None:
            return jsonify(ok=False, code="only_one_book", language=language,
                           message="Only one usable book is available in this collection.")
        if restarted:
            session[session_key("seen_b", language)] = []
            session[session_key("seen_p", language)] = []
            round_notice = "You have met every book in this language. Starting a fresh round."
            extra["notice"] = (extra.get("notice", "") + " " + round_notice).strip()
        remember("seen_b", language, cat.book_token(cat.book_of(nxt)))
        session[session_key("revealed", language)] = None
        return show(language, cat, nxt, **extra)

    @app.post("/api/event")
    def event():
        found = current_passage()
        if not found:
            return fail("unknown_passage", "That passage is no longer available. Spin for a new one.")
        language, cat, passage = found
        name = (request.get_json(silent=True) or {}).get("event")
        allowed = ("book_hidden", "book_saved", "awareness_known", "awareness_unknown", "awareness_unsure")
        if name not in allowed:
            return fail("unknown_event", "That event is not accepted.")
        if name == "book_hidden":
            session[session_key("revealed", language)] = None
        record(cat, name, passage)
        return jsonify(ok=True, language=language)

    @app.get("/go/<kind>/<token>")
    def go(kind, token):
        if kind not in LINK_KINDS:
            return render_template("about.html", site=app.config["SITE"], missing_link=True, language=active_language()), 404

        # New links carry a stable book token and survive text-cache eviction.
        resolved = gutendex.resolve_book_token(token, catalogues)
        if resolved:
            language, cat, book = resolved
            url = cat.links_for(book).get(kind)
            if not url:
                return render_template("about.html", site=app.config["SITE"], missing_link=True, language=language), 404
            analytics.log_event(db_path, f"{kind}_link_clicked", book["id"], None, book.get("is_demo") is True)
            return redirect(url, 302)

        # Legacy links containing passage tokens remain valid while their passage
        # is available (or while its book can be reconstructed from old session data).
        found = find_passage(token)
        if not found:
            return render_template("about.html", site=app.config["SITE"], missing_link=True, language=active_language()), 404
        language, cat, passage = found
        url = cat.links_for(cat.book_of(passage)).get(kind)
        if not url:
            return render_template("about.html", site=app.config["SITE"], missing_link=True, language=language), 404
        record(cat, f"{kind}_link_clicked", passage)
        return redirect(url, 302)

    @app.after_request
    def add_language_switcher(response):
        if request.path in ("/", "/about") and response.status_code == 200 and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            scripts = (
                '<script src="/static/js/language-switcher.js" defer></script>',
                '<script src="/static/js/catalogue-refresh.js" defer></script>',
            )
            changed = False
            for script in scripts:
                if script not in html:
                    html = html.replace("</body>", script + "</body>") if "</body>" in html else html + script
                    changed = True
            if changed:
                response.set_data(html)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=5000)
