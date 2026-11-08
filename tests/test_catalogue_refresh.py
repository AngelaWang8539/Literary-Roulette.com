import json
import tempfile
import unittest
from pathlib import Path

from catalogue_refresh import _snapshot_is_current


class SnapshotFreshnessTests(unittest.TestCase):
    def test_old_gutendex_snapshot_is_not_current_even_if_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(json.dumps({
                "provider": "Gutendex API",
                "filters": {"max_pages": None},
                "languages": {"en": [{"id": 1}], "fr": [{"id": 2}], "zh": [{"id": 3}]},
                "counts": {"en": 1, "fr": 1, "zh": 1},
            }), encoding="utf-8")
            self.assertFalse(_snapshot_is_current(path))

    def test_partial_rdf_snapshot_is_not_current(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(json.dumps({
                "provider": "Project Gutenberg official RDF metadata archive",
                "sync_complete": False,
                "languages": {"en": [{"id": 1}], "fr": [{"id": 2}], "zh": [{"id": 3}]},
                "counts": {"en": 1, "fr": 1, "zh": 1},
            }), encoding="utf-8")
            self.assertFalse(_snapshot_is_current(path))

    def test_complete_rdf_snapshot_with_all_three_shelves_is_current(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(json.dumps({
                "provider": "Project Gutenberg official RDF metadata archive",
                "sync_complete": True,
                "languages": {"en": [{"id": 1}], "fr": [{"id": 2}], "zh": [{"id": 3}]},
                "counts": {"en": 1, "fr": 1, "zh": 1},
            }), encoding="utf-8")
            self.assertTrue(_snapshot_is_current(path))


if __name__ == "__main__":
    unittest.main()
