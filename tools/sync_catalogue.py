"""Open (or refresh) the catalogue index: every Project Gutenberg text edition in English, French and Chinese.

    python tools/sync_catalogue.py                    # downloads pg_catalog.csv.gz (seconds) and builds the index
    python tools/sync_catalogue.py --csv FILE.csv.gz  # use a saved copy, e.g. your desktop app's data/pg_catalog.csv.gz
    python tools/sync_catalogue.py --export-seed      # also save data/universe_seed.sqlite so fresh copies open instantly

The web app does this by itself on first start; this tool just lets you watch it.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="use this pg_catalog.csv.gz instead of downloading")
    ap.add_argument("--export-seed", action="store_true", help="copy the built index to data/universe_seed.sqlite")
    args = ap.parse_args()
    app = create_app({"EMBEDDED_WORKER": False, "TESTING": True, "CATALOG_CSV": args.csv})
    rt = app.runtime
    last = [None]
    original = rt.sync._set
    def show(**changes):
        original(**changes)
        line = f"{changes.get('percent', ''):>3}%  {changes.get('message', '')}" if changes.get("message") else None
        if line and line != last[0]:
            last[0] = line
            print(line)
    rt.sync._set = show
    if not rt.sync.run():
        print("Failed:", rt.sync.status().get("error") or "another process is already syncing")
        return 1
    print(rt.sync.status()["message"])
    if args.export_seed:
        shutil.copyfile(rt.universe_path, rt.seed_path)
        print(f"Saved {rt.seed_path} ({rt.seed_path.stat().st_size / 1e6:.1f} MB). Commit it so a fresh clone opens instantly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
