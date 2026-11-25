"""Operate the Book Registry from the terminal (the Registry Center page does the same, read-mostly, in a browser).

    python tools/registry_admin.py status
    python tools/registry_admin.py list --q holmes --pool active --limit 20
    python tools/registry_admin.py show pg-en-1342

    # independent-author TXT: intake -> review the evidence -> approve rights -> activate
    python tools/registry_admin.py intake story.txt --slug harbour-lights --language en --title "Harbour Lights" --author "Ann Author" \
        --granted-by "Ann Author" --scope "show passages, link to her page" --territory Worldwide --commercial no \
        --attribution "Ann Author, shared with permission" --expires perpetual --evidence "email of 2026-11-02" \
        --link buy=https://example.org/harbour-lights
    python tools/registry_admin.py approve-rights au-en-harbour-lights
    python tools/registry_admin.py activate au-en-harbour-lights      # rebuilds the index so it can be drawn

    python tools/registry_admin.py pause pg-en-1342 --reason "reader report"     # takes effect at once, no rebuild
    python tools/registry_admin.py rights pg-en-1342 withdrawn --note "rights holder asked"
    python tools/registry_admin.py rebuild                                          # index from the Registry, no download
    python tools/registry_admin.py gutendex                                         # one Gutendex enrichment batch now

Nothing here talks to a visitor-facing path, and nothing is pushed anywhere: it edits this machine's instance/ folder only.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intake  # noqa: E402
from app import create_app  # noqa: E402
from registry import RegistryError  # noqa: E402


def show(book):
    keys = ("book_id", "title", "author", "language", "provider", "pool_status", "status_reason", "rights_status", "rights_territory", "rights_commercial",
            "rights_expires", "rights_note", "attribution", "text_status", "text_error", "revision", "metadata_status")
    for key in keys:
        print(f"  {key:18} {book.get(key)}")


def yes(value):
    return None if value is None else str(value).strip().lower() in ("yes", "y", "true", "1")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("list")
    for name in ("q", "pool", "rights", "provider", "language"):
        p.add_argument("--" + name)
    p.add_argument("--limit", type=int, default=20)
    sub.add_parser("show").add_argument("book_id")
    p = sub.add_parser("intake")
    p.add_argument("file")
    for name in ("slug", "language", "title", "author"):
        p.add_argument("--" + name, required=True)
    for name in ("description", "granted_by", "scope", "territory", "commercial", "attribution", "expires", "evidence"):
        p.add_argument("--" + name.replace("_", "-"), dest=name)
    p.add_argument("--link", action="append", default=[], metavar="KIND=URL")
    p = sub.add_parser("approve-rights")
    p.add_argument("book_id")
    for name in ("territory", "commercial", "attribution", "expires", "note"):
        p.add_argument("--" + name)
    p = sub.add_parser("rights")
    p.add_argument("book_id"); p.add_argument("status"); p.add_argument("--note")
    for cmd in ("activate", "pause", "archive"):
        p = sub.add_parser(cmd)
        p.add_argument("book_id"); p.add_argument("--reason")
    sub.add_parser("rebuild")
    sub.add_parser("gutendex")
    args = ap.parse_args(argv)

    app = create_app({"EMBEDDED_WORKER": False, "TESTING": True})
    rt = app.runtime
    reg = rt.registry
    try:
        if args.cmd == "status":
            print(json.dumps({"registry": reg.summary(), "providers": reg.provider_status()}, indent=2, default=str))
        elif args.cmd == "list":
            found = reg.search(q=args.q or "", pool=args.pool, rights=args.rights, provider=args.provider, language=args.language, limit=args.limit)
            for b in found["books"]:
                print(f"{b['book_id']:28} {b['pool_status']:9} {b['rights_status']:24} {b['text_status']:10} {b['title'][:50]}")
            print(f"{len(found['books'])} of {found['total']}")
        elif args.cmd == "show":
            book = reg.get(args.book_id)
            if not book:
                print("Unknown book."); return 1
            show(book); print("  blockers           ", reg.blockers(args.book_id) or "none: can be activated")
        elif args.cmd == "intake":
            links = dict(item.split("=", 1) for item in args.link if "=" in item)
            permission = {"granted_by": args.granted_by, "scope": args.scope, "territory": args.territory, "commercial": yes(args.commercial),
                          "attribution": args.attribution, "expires": args.expires, "evidence": args.evidence}
            out = intake.intake(reg, rt.text_store, args.file, slug=args.slug, language=args.language, title=args.title, author=args.author,
                                permission={k: v for k, v in permission.items() if v is not None}, description=args.description or "", links=links)
            if not out["ok"]:
                print("Not accepted:"); [print("  -", p) for p in out["problems"]]; return 1
            print(f"Recorded {out['book_id']} (revision {out['revision']}, {out['passages']} passages): PENDING.")
            print("Next: check the permission evidence, then `approve-rights`, then `activate`.")
        elif args.cmd == "approve-rights":
            show(reg.set_rights(args.book_id, "approved", territory=args.territory, commercial=yes(args.commercial), attribution=args.attribution,
                                expires=args.expires, note=args.note))
            print("Rights approved. The book is not drawable until it is activated.")
        elif args.cmd == "rights":
            show(reg.set_rights(args.book_id, args.status, note=args.note)); rt.universe.reload()
        elif args.cmd in ("activate", "pause", "archive"):
            status = {"activate": "active", "pause": "paused", "archive": "archived"}[args.cmd]
            show(reg.set_pool_status(args.book_id, status, args.reason))
            if status == "active":
                print("Rebuilding the index so it can be drawn...", rt.sync.rebuild_index())
        elif args.cmd == "rebuild":
            print(rt.sync.rebuild_index(progress=lambda m, p=None: None))
        elif args.cmd == "gutendex":
            print(rt.gutendex.run())
    except RegistryError as exc:
        print("Refused:", exc); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
