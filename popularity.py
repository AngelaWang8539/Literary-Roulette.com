"""Popularity for the reveal reward ("Jackpot!" ... "Off the beaten shelf"), from Project Gutenberg's own public most-read lists.

Only the top-100 lists are public, so the badge is honest about what it knows:
  top 10 this month  -> jackpot        top 100 this month -> popular right now        otherwise -> off the beaten shelf
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

    def payload(self, language, gid):
        """The badge for one book, or None when there is nothing true to say."""
        if not self.available():
            return None
        rank = self.rank(gid)
        if rank is not None and rank <= 10:
            tier, label = "jackpot", "Jackpot! One of Gutenberg's 10 most-read books this month"
        elif rank is not None:
            tier, label = "lucky", "Popular right now: in Gutenberg's top 100 this month"
        else:
            tier, label = "hidden-gem", "Off the beaten shelf: not among Gutenberg's most-read books this month"
        return {"tier": tier, "label": label, "detail": f"Ranked #{rank} in Gutenberg's monthly top 100" if rank is not None else "",
                "note": NOTE, "rank": rank, "source_url": TOP_URL, "snapshot_date": time.strftime("%Y-%m-%d", time.gmtime(self._data.get("updated", 0)))}

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
