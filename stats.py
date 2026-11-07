"""Developer-only report of aggregate events.  Run:  python stats.py

Compares familiar classics with discovery candidates. A link click shows interest only;
it is NOT a sale. Uploaded books and reading history are never part of this data.
"""
import json
import sys
from pathlib import Path

import analytics

BASE = Path(__file__).resolve().parent
DB = BASE / "instance" / "analytics.db"
EVENTS = ["passage_displayed", "another_passage_requested", "book_revealed", "another_book_requested", "book_saved",
          "read_link_clicked", "borrow_link_clicked", "buy_link_clicked", "awareness_known", "awareness_unknown", "awareness_unsure"]


def group_of(book):
    if not book:
        return "unknown book"
    return "demo (ignore)" if book.get("is_demo") else book.get("familiarity", "unlabelled")


def summarise(by_book, books):
    groups = {}
    for bid, counts in by_book.items():
        g = groups.setdefault(group_of(books.get(bid)), {"books": 0, **{e: 0 for e in EVENTS}})
        g["books"] += 1
        for e in EVENTS:
            g[e] += counts.get(e, 0)
    return groups


def pct(a, b):
    return f"{a / b:.0%}" if b else "n/a"


def main():
    if not DB.exists():
        sys.exit("No analytics yet. Run the site and click around first.")
    books = {b["id"]: b for b in json.loads((BASE / "data" / "books.json").read_text(encoding="utf-8"))}
    demo_path = BASE / "data" / "demo_books.json"
    if demo_path.exists():
        books.update({b["id"]: b for b in json.loads(demo_path.read_text(encoding="utf-8"))})
    rep = analytics.report(DB)
    print("TOTAL EVENTS")
    for name in EVENTS + ["book_hidden"]:
        print(f"  {name:28} {rep['totals'].get(name, 0)}")
    print("\nCLASSICS vs DISCOVERY (books that had at least one event)")
    for name, g in sorted(summarise(rep["by_book"], books).items()):
        shown, answers = g["passage_displayed"], g["awareness_known"] + g["awareness_unknown"] + g["awareness_unsure"]
        clicks = g["read_link_clicked"] + g["borrow_link_clicked"] + g["buy_link_clicked"]
        print(f"  {name}: {g['books']} books, {shown} passages shown")
        print(f"     reveals per passage shown : {pct(g['book_revealed'], shown)}")
        print(f"     saves per reveal          : {pct(g['book_saved'], g['book_revealed'])}")
        print(f"     link clicks per reveal    : {pct(clicks, g['book_revealed'])}  (read {g['read_link_clicked']}, borrow {g['borrow_link_clicked']}, buy {g['buy_link_clicked']})")
        print(f"     had heard of the book     : {pct(g['awareness_known'], answers)} of {answers} answers")
    print("\nBY BOOK")
    for bid, c in sorted(rep["by_book"].items()):
        b = books.get(bid, {})
        print(f"  {b.get('title', bid)} [{group_of(b)}]  shown={c.get('passage_displayed', 0)} revealed={c.get('book_revealed', 0)} "
              f"saved={c.get('book_saved', 0)} clicks={sum(c.get(k, 0) for k in EVENTS[5:8])}")
    print("\nSmall numbers prove little. Clicks are interest, not purchases.")


if __name__ == "__main__":
    main()
