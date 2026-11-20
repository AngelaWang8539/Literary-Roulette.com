"""Fetch Project Gutenberg's most-read lists and show what was found, so you can check the badge data is real.

    python tools/sync_popularity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402


def main():
    rt = create_app({"EMBEDDED_WORKER": False, "TESTING": True}).runtime
    if not rt.popularity.refresh():
        print("Could not read the lists:", rt.popularity.last_error)
        print("No badge will be shown until this works. (The page layout may have changed; send me the page's HTML headings.)")
        return 1
    data = rt.popularity._data
    for key in ("month", "week", "day"):
        ranks = data.get(key) or {}
        print(f"{key}: {len(ranks)} books; first three ids: {list(ranks)[:3]}")
    print(f"Saved to {rt.popularity.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
