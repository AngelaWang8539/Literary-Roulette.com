"""Test isolation: tests never read the real Gutenberg catalogue, touch the network, or write to instance/.

The real data/gutendex_catalog.json is large and makes a "random book" request try to download a
real text from gutenberg.org, which made the suite slow, flaky and dependent on your connection.
Every test that does not choose its own paths gets throw-away ones instead.
"""
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as _app  # noqa: E402

_ROOT = Path(tempfile.mkdtemp(prefix="lr-tests-"))
atexit.register(shutil.rmtree, _ROOT, True)
_real_create_app = _app.create_app


def _isolated_create_app(test_config=None):
    config = dict(test_config or {})
    config.setdefault("GUTENBERG_CATALOG_PATH", _ROOT / "no-catalogue.json")
    config.setdefault("GUTENBERG_CACHE_DIR", _ROOT / "text-cache")
    config.setdefault("AUTO_REFRESH_CATALOGUE", False)
    if "INSTANCE_DIR" not in config:
        config["INSTANCE_DIR"] = Path(tempfile.mkdtemp(dir=_ROOT))
    return _real_create_app(config)


_app.create_app = _isolated_create_app
