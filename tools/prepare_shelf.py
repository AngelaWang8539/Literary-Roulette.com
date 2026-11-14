"""Prepare the ready shelf from the verified local texts, so the first page load is instant.

    python tools/prepare_shelf.py            # prepare anything missing or stale
    python tools/prepare_shelf.py --force    # rebuild every local book
    python tools/prepare_shelf.py --lang fr  # one language only

Safe to run while the site is running. Books already ready (same text, same splitting rules) are skipped.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="rebuild even books that are already ready")
    ap.add_argument("--lang", choices=("en", "fr", "zh"), help="only this language")
    args = ap.parse_args()
    app = create_app({"EMBEDDED_WORKER": False, "TESTING": True})
    rt = app.runtime
    if args.force:
        for language, cat in rt.catalogues.items():
            if args.lang in (None, language):
                for bid in cat.local_texts:
                    rt.shelf.retire(language, bid)
    if args.lang:
        rt.worker.catalogues = {args.lang: rt.catalogues[args.lang]}
    queued = rt.worker.seed_local()
    started = time.time()
    done = rt.worker.run_pending()
    for language in (rt.worker.catalogues):
        cat = rt.catalogues[language]
        print(f"{language}: {len(rt.starters(language))} starter books ready ({len(cat.local_texts)} verified local, "
              f"{len(cat.books) - len(cat.local_texts)} waiting for text or on-demand)")
    status = rt.worker.status(detail=True)
    for key, info in status.get("failures", {}).items():
        print("FAILED", key, info["error"])
    print(f"Prepared {done} of {queued} queued book(s) in {time.time() - started:.1f}s. Shelf: {rt.shelf.root}")


if __name__ == "__main__":
    main()
