"""Regression tests for the reveal metadata and popularity badge."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalogue import Catalogue
from gutenberg_pool import GutendexPool


def test_internal_edition_note_is_not_exposed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        texts = root / "texts"
        texts.mkdir()
        text = ("這是一段文學內容。" * 100) + "\n\n" + ("另一段故事。" * 100)
        (texts / "zh-b014.txt").write_text(text, encoding="utf-8")
        manifest = {"zh-b014": {"verified": True, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}}
        book = {
            "id": "zh-b014", "title": "閱微草堂筆記", "author": "紀昀", "language": "zh",
            "text_file": "zh-b014.txt", "gutenberg_id": 23817,
            "rights_status": "public_domain_candidate", "source_provider": "Project Gutenberg",
            "edition_note": "文言笔记体，建议以段落为单位生成 passage，避免拆散短篇。",
        }
        cat = Catalogue([book], [], "test-secret", False, texts_dir=texts, manifest=manifest, language="zh")
        assert cat.book_ids
        cat.ensure_book_loaded("zh-b014")  # texts are read lazily, only when a title is selected
        passage = cat.passages_by_book["zh-b014"][0]
        payload = cat.book_payload(book, passage)
        assert "文言笔记体" not in str(payload)
        assert payload["source"]["edition"] is None


def test_popularity_tiers_and_partial_snapshot_guard():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = []
        # 1,000 descending, unique download counts. ID 64317 models The Great Gatsby.
        for i in range(1, 1001):
            rows.append({"id": 64317 if i == 1 else 70000 + i,
                         "download_count": 1001 - i,
                         "formats": {"text/plain; charset=utf-8": f"https://www.gutenberg.org/cache/epub/{64317 if i == 1 else 70000+i}/book.txt"}})
        full_path = root / "full.json"
        full_path.write_text(json.dumps({"filters": {"max_pages": None}, "languages": {"en": rows, "fr": [], "zh": []}}), encoding="utf-8")
        pool = GutendexPool(full_path, root / "cache")
        record = pool.by_gid["en"][64317]
        badge = pool.popularity_payload(record, "en")
        assert badge["tier"] == "jackpot" and "top 1%" in badge["label"].lower()

        # Rank 50 is inside the 10%-90% middle band.
        mid = pool.by_gid["en"][70500]
        assert pool.popularity_payload(mid, "en")["tier"] == "discovery"
        # Rank 950 is in the bottom 10%.
        low = pool.by_gid["en"][70950]
        low_badge = pool.popularity_payload(low, "en")
        assert low_badge["tier"] == "hidden-gem" and "would you like to explore" in low_badge["label"]

        preview_path = root / "preview.json"
        preview_path.write_text(json.dumps({"filters": {"max_pages": 2}, "languages": {"en": rows[:64], "fr": [], "zh": []}}), encoding="utf-8")
        preview = GutendexPool(preview_path, root / "cache")
        assert preview.is_partial_snapshot
        assert preview.popularity_payload(preview.by_gid["en"][64317], "en") is None


if __name__ == "__main__":
    test_internal_edition_note_is_not_exposed()
    test_popularity_tiers_and_partial_snapshot_guard()
    print("PASS: internal note is hidden; Gatsby-style top-1%, middle and hidden-gem tiers; partial snapshots suppressed")
