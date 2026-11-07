"""Show which of the catalogue's books have verified full text.   python tools/catalogue_status.py"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
books = json.loads((ROOT / "data" / "books.json").read_text(encoding="utf-8"))
try:
    manifest = json.loads((ROOT / "data" / "texts" / "manifest.json").read_text(encoding="utf-8"))
except OSError:
    manifest = {}
real = [b for b in books if b.get("gutenberg_id")]
ready = 0
for group in ("classic", "discovery"):
    print(f"\n{group.upper()} ({sum(b['familiarity'] == group for b in real)} titles)")
    for b in real:
        if b["familiarity"] != group:
            continue
        m = manifest.get(b["id"])
        path = ROOT / "data" / "texts" / f"{b['id']}.txt"
        if m and m.get("verified") and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == m.get("text_sha256"):
            state, ready = f"READY  {m['words']:>7} words {m['passages']:>5} passages", ready + 1
        elif m:
            state = "FAILED " + "; ".join(m.get("problems", []))
        else:
            state = "not fetched yet"
        print(f"  #{b['gutenberg_id']:<6} {b['title'][:42]:42} {state}")
print(f"\n{ready} of {len(real)} titles have verified, ready-to-use full text.")
