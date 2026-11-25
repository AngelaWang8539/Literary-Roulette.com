"""Builds the shared pieces (Registry, catalogues, universe, lake/shelf, worker, providers) for the web app and worker.py."""
import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

import catalogue_sync
import fetchguard
import gutendex as gutendex_mod
import intake
import popularity as popularity_mod
import prep
import registry as registry_mod
import universe as universe_mod
from library import Catalogue, load_json_list, load_manifest
from shelf import ReadyShelf

LANGUAGES = {"en": "English", "fr": "Français", "zh": "中文"}
log = logging.getLogger("literary_roulette.runtime")


def load_secret(instance_dir):
    """The signing key. Use LR_SECRET_KEY on several machines. On one machine it is created once, atomically, so several
    Gunicorn workers starting at the same moment all end up with the SAME key (otherwise their tickets would not match)."""
    env = os.environ.get("LR_SECRET_KEY")
    if env:
        return env
    instance_dir.mkdir(parents=True, exist_ok=True)
    path = instance_dir / "secret.key"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(50):                                 # the creating process may still be writing
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
        time.sleep(0.05)
    raise RuntimeError("The secret key file is empty; delete instance/secret.key and start again.")


def settings_from_env(overrides=None):
    env, kw = os.environ, {}
    names = {"LR_FIRST_WAVE": "first_wave", "LR_MAX_WAVE": "max_wave", "LR_PREP_WORKERS": "workers",
             "LR_SOURCE_CONCURRENCY": "source_concurrency", "LR_SOURCE_MIN_INTERVAL": "min_interval",
             "LR_PREP_CHECK_SECONDS": "check_interval", "LR_MAX_COLD_WAIT": "max_cold_wait", "LR_QUEUE_CAP": "queue_cap"}
    for var, key in names.items():
        if env.get(var):
            kw[key] = env[var]
    target = env.get("LR_LIBRARY_TARGET")
    if target:
        kw["library_target"] = 10**9 if target.strip().lower() == "all" else target
    if env.get("LR_REPLENISH") == "0":
        kw["replenish"] = False
    kw.update(overrides or {})
    return prep.Settings(**kw)


class Runtime:
    def __init__(self, config):
        base = Path(config["BASE_DIR"])
        data_dir, instance_dir = Path(config["DATA_DIR"]), Path(config["INSTANCE_DIR"])
        self.secret = load_secret(instance_dir)
        self.registry = registry_mod.Registry(Path(config.get("REGISTRY_PATH") or instance_dir / "registry.sqlite"))
        self.shelf_dir = Path(config.get("SHELF_DIR") or instance_dir / "ready")
        self.problems, self.catalogues = [], {}
        for language in LANGUAGES:
            folder = data_dir if language == "en" else data_dir / language
            texts_dir = Path(config.get(f"{language.upper()}_TEXTS_DIR") or folder / "texts")
            if language == "en" and config.get("TEXTS_DIR"):
                texts_dir = Path(config["TEXTS_DIR"])
            override = config.get("BOOKS") if language == "en" else None
            books = override if override is not None else load_json_list(folder / "books.json", self.problems)
            self.catalogues[language] = Catalogue(
                books, self.secret, language, texts_dir=texts_dir, manifest=load_manifest(texts_dir),
                allow_pending_rights=config.get("ALLOW_PENDING_RIGHTS", False))
        self.universe_path = Path(config.get("UNIVERSE_PATH") or instance_dir / "universe.sqlite")
        self.seed_path = Path(config.get("UNIVERSE_SEED_PATH") or data_dir / "universe_seed.sqlite")
        if not self.universe_path.exists() and self.seed_path.exists():
            # A prebuilt index shipped with the project: the very first open is already the ocean.
            self.universe_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.universe_path.with_name(self.universe_path.name + ".seed.tmp")
            universe_mod.upgrade_index(self.seed_path, tmp)          # copies, converting an older index layout without any network
            os.replace(tmp, self.universe_path)
        if self.registry.count() == 0 and self.universe_path.exists():
            try:                                                      # first start: the Registry begins with what the index already holds
                self.registry.import_index_rows(universe_mod.index_rows(self.universe_path))
            except Exception:
                log.exception("Could not bootstrap the Registry from the index; the next catalogue sync will fill it")
        for language, cat in self.catalogues.items():
            self.registry.import_local_books(language, cat.all_books(), {bid: {"verified": True, "text_sha256": info["sha256"]} for bid, info in cat.local_texts.items()})
        self.universe = universe_mod.Universe(self.universe_path, config.get("BLOCKLIST_PATH") or data_dir / "blocklist.json", self.registry)
        self._build_lock = threading.Lock()
        self.shelf = ReadyShelf(self.shelf_dir, tuple(LANGUAGES))
        self._synced_version, self._cache = -1, {}
        self.seed_gids = {}                       # language -> {gutenberg_id: book_id} for starter books
        for language, cat in self.catalogues.items():
            self.seed_gids[language] = {int(b["gutenberg_id"]): b["id"] for b in cat.all_books()
                                        if str(b.get("gutenberg_id") or "").isdigit() and b["id"] in cat.local_texts}
        self.sync_external()
        self.status_path = instance_dir / "catalogue_status.json"
        self.csv_override = config.get("CATALOG_CSV") or os.environ.get("LR_CATALOG_CSV")
        self.max_age_hours = float(config.get("CATALOGUE_MAX_AGE_HOURS", 24))
        self.sync = None
        self.settings = settings_from_env(config.get("PREP_SETTINGS"))
        env = os.environ
        self.guards = fetchguard.make_guards(self.shelf_dir / ".gate", min_interval=self.settings.min_interval)   # same gate as book downloads
        self.popularity = popularity_mod.Popularity(instance_dir / "popularity.json", guard=self.guards["gutenberg"], registry=self.registry)
        self.text_store = intake.TextStore(Path(config.get("AUTHOR_TEXTS_DIR") or instance_dir / "registry" / "texts"))
        self.gutendex = gutendex_mod.Gutendex(
            self.registry, self.guards["gutendex"], prefer=self.gutendex_targets, enabled=env.get("LR_GUTENDEX", "1") != "0",
            per_run=int(env.get("LR_GUTENDEX_PER_RUN", 160)), interval=float(env.get("LR_GUTENDEX_HOURS", 6)) * 3600,
            census=env.get("LR_GUTENDEX_CENSUS") == "1")
        self.worker = prep.PrepWorker(self.shelf, self.catalogues, self.universe, self.shelf_dir, self.settings,
                                      fetcher=config.get("FETCHER"), registry=self.registry, text_store=self.text_store)
        self.sync = self.make_sync(**(config.get("SYNC_KWARGS") or {}))
        self.offline_starter = bool(config.get("OFFLINE_STARTER", False))

    # ---- the catalogue (universe) ------------------------------------------------------------
    def make_sync(self, **kw):
        return catalogue_sync.CatalogueSync(self, self.status_path, csv_path=self.csv_override,
                                            max_age_hours=self.max_age_hours, **kw)

    def gutendex_targets(self):
        """Gutenberg ids worth a fresh Gutendex look first: books in the library and on Gutenberg's top lists."""
        ids = set(self.popularity.listed_ids())
        for language in LANGUAGES:
            ids.update(g for g in map(prep._gutenberg_id, self.shelf.external_ids(language)) if g)
        return ids

    # ---- what can be served ------------------------------------------------------------------
    def sync_external(self):
        """Register lake titles found on disk (prepared earlier, or by another process)."""
        if self._synced_version == self.shelf.version:
            return
        for language, cat in self.catalogues.items():
            for bid in self.shelf.external_ids(language):
                if bid not in cat.books:
                    book = (self.shelf.meta(language, bid) or {}).get("book")
                    if book:
                        cat.register_external(book)
        self._synced_version = self.shelf.version

    def _cached(self, kind, language, build):
        cat = self.catalogues[language]
        key = (self.shelf.version, len(cat.books))
        hit = self._cache.get((kind, language))
        if hit and hit[0] == key:
            return hit[1]
        value = build(cat)
        self._cache[(kind, language)] = (key, value)
        return value

    def lake(self, language):
        """Prepared random books (drawn from the whole catalogue), cleared in the catalogue."""
        ext = set(self.shelf.external_ids(language))
        return self._cached("lake", language, lambda cat: tuple(b for b in self.shelf.ids(language) if b in ext and b in cat.books))

    def starters(self, language):
        """The small bundled starter shelf. Used only while the full catalogue is closed or a fetch is slow."""
        ext = set(self.shelf.external_ids(language))
        return self._cached("starters", language, lambda cat: tuple(b for b in self.shelf.ids(language) if b not in ext and b in cat.books))

    def mode(self, language):
        """"ocean": the whole catalogue is open. "opening": it is being opened right now (the page shows the librarian
        with real progress). "starter": it could not be opened, so a small bundled shelf stands in, and says so."""
        if self.universe.available and self.universe.count(language) > 0:
            return "ocean"
        if self.offline_starter or (self.sync and self.sync.status().get("state") == "failed"):
            return "starter"
        return "opening"

    def ready_book_id(self, language, ref):
        """Book id if this exact edition is already prepared (in the lake or a starter), else None. `ref` is a drawn record
        (any provider) or a Gutenberg number."""
        gid = ref.get("gutenberg_id") if isinstance(ref, dict) else ref
        bid = ref["id"] if isinstance(ref, dict) else f"pg-{language}-{int(ref)}"
        if self.shelf.has(language, bid) and bid in self.catalogues[language].books:
            return bid
        seed = self.seed_gids[language].get(int(gid)) if gid else None
        return seed if seed and self.shelf.has(language, seed) and seed in self.catalogues[language].books else None

    def ready_counts(self):
        return {code: len(self.lake(code)) + len(self.starters(code)) for code in LANGUAGES}
