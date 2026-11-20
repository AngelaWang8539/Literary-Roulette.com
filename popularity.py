"""Popularity for the reveal reward ("Jackpot!" ... "Off the beaten shelf"), from Project Gutenberg's own public most-read lists.

Two honest sources, used in this order:
  1. Gutenberg's public top-100 list for the month: top 10 -> jackpot, rest of the top 100 -> lucky.
  2. The catalogue's own download percentile for every other book (`deep`, from universe.py): top 1% jackpot, top 10% lucky,
     bottom 10% hidden gem, the middle 80% discovery. Without it a book outside the top 100 gets a neutral "off the shelf" line.
Each tier has a few wordings; the book's id picks one, so a book always wears the same line but a run of spins does not repeat it.
Refreshed about once a day. If the page cannot be fetched or no longer looks as expected, the last good data is kept; with no
data at all there is simply no badge. A badge never invents a number. Pluggable: any other source only has to produce
{"month": {gutenberg_id: rank}} (see parse_top_page) to replace this one.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import source

TOP_URL = "https://www.gutenberg.org/browse/scores/top"
LISTS = {"month": "last 30 days", "week": "last 7 days", "day": "yesterday"}
MIN_ENTRIES = 20                    # a list shorter than this means the page changed shape: do not trust it
NOTE = "From Project Gutenberg's public most-read lists for the last 30 days. This is popularity, not literary quality."

# (panel line, short card line). The panel line is translated by static/js/i18n.js; the card line is drawn in English.
LINES = {
    "jackpot": [("Jackpot! A Gutenberg favourite right now", "Jackpot read"),
                ("Jackpot! Readers are flocking to this one", "Jackpot read")],
    "lucky": [("Lucky find! A reader favourite this month", "Reader favourite"),
              ("Lucky find! Plenty of readers are enjoying this one", "Reader favourite")],
    "discovery": [("A fresh discovery: a book from the quieter shelves", "Fresh discovery"),
                  ("A quiet find from the middle of the stacks", "Quiet find"),
                  ("Nice find: a book away from the crowd", "Fresh discovery")],
    "gem": [("Hidden gem alert! Among the least-downloaded 10%: would you like to explore?", "Hidden gem"),
            ("Deep-shelf find! Few readers have opened this one lately", "Deep-shelf find"),
            ("Rare draw! One of the quietest books in the library", "Rare draw")],
    "offlist": [("Off the beaten shelf: outside Gutenberg's monthly top 100", "Off the beaten shelf"),
                ("Away from the crowd: not in Gutenberg's monthly top 100", "Away from the crowd")],
}


def pick_line(kind, gid):
    pool = LINES[kind]
    return pool[int(gid) % len(pool)]


def parse_top_page(html):
    """{'month': {gutenberg_id: rank}, ...} from the 'Top 100 EBooks ...' lists on Gutenberg's top page. Unrecognised lists are left out."""
    found = {}
    for part in re.split(r"(?i)<h2\b", html)[1:]:
        heading, _sep, body = part.partition("</h2>")
        heading = re.sub(r"<[^>]+>", " ", heading).lower()
        if "ebook" not in heading:
            continue
        for key, phrase in LISTS.items():
            if phrase in heading and key not in found:
                ids = []
                for m in re.finditer(r"/ebooks/(\d+)", body):
                    gid = int(m.group(1))
                    if gid not in ids:
                        ids.append(gid)
                found[key] = {gid: rank for rank, gid in enumerate(ids[:100], start=1)}
    return {k: v for k, v in found.items() if len(v) >= MIN_ENTRIES}


class Popularity:
    def __init__(self, path, clock=time.time, max_age_hours=24.0):
        self.path, self._clock, self.max_age = Path(path), clock, float(max_age_hours) * 3600
        self.last_error = None
        self._data, self._stamp = {}, None

    def _load(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            self._data, self._stamp = {}, None
            return
        if stamp != self._stamp:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
            self._stamp = stamp

    def rank(self, gid):
        self._load()
        return (self._data.get("month") or {}).get(str(int(gid)))

    def available(self):
        self._load()
        return bool(self._data.get("month"))

    def payload(self, language, gid, deep=None):
        """The badge for one book, or None when there is nothing true to say. `deep` is the catalogue's percentile badge, if any."""
        have_month = self.available()
        rank = self.rank(gid) if have_month else None
        if rank is not None:
            tier = "jackpot" if rank <= 10 else "lucky"
            kind, detail, note, url = tier, f"Ranked #{rank} in Gutenberg's monthly top 100", NOTE, TOP_URL
        elif deep and deep.get("tier") in ("jackpot", "lucky", "discovery", "hidden-gem"):
            tier = deep["tier"]
            kind = "gem" if tier == "hidden-gem" else tier
            rank, detail, note, url = deep.get("rank"), deep.get("detail") or "", deep.get("note") or NOTE, deep.get("source_url") or TOP_URL
        elif have_month:
            tier, kind, detail, note, url = "hidden-gem", "offlist", "", NOTE, TOP_URL
        else:
            return None
        label, short = pick_line(kind, gid)
        stamp = self._data.get("updated") if have_month else None
        snapshot = time.strftime("%Y-%m-%d", time.gmtime(stamp)) if stamp else (deep or {}).get("snapshot_date", "")
        return {"tier": tier, "label": label, "short": short, "detail": detail, "note": note, "rank": rank,
                "source_url": url, "snapshot_date": snapshot}

    def stale(self):
        self._load()
        return self._clock() - float(self._data.get("updated", 0)) > self.max_age

    def refresh(self, opener=urllib.request.urlopen, timeout=15):
        """Fetch and store the lists. True on success; on any problem the previous data stays and last_error says why."""
        try:
            req = urllib.request.Request(TOP_URL, headers={"User-Agent": source.USER_AGENT})
            with opener(req, timeout=timeout) as response:
                html = response.read(3_000_000).decode("utf-8", errors="replace")
            lists = parse_top_page(html)
            if "month" not in lists:
                raise ValueError("the page no longer shows the 30-day top list in the expected form")
            data = {"updated": self._clock(), **{k: {str(g): r for g, r in v.items()} for k, v in lists.items()}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
            self.last_error = None
            return True
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            self.last_error = str(getattr(exc, "reason", exc))
            return False

    def refresh_if_stale(self, **kw):
        return self.refresh(**kw) if self.stale() else False
