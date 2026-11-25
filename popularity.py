"""Popularity for the reveal reward ("Jackpot!" ... "Off the beaten shelf"), from Project Gutenberg's own public most-read lists.

Two honest sources, used in this order (they are different signals and are never mixed into one number):
  1. Gutenberg's public top-100 list for the month: top 10 -> jackpot, rest of the top 100 -> lucky. A TREND signal.
  2. A Gutendex download-count percentile (`deep`, built by deep_payload from the Registry), per language, and ONLY when every
     eligible book of that language has a fresh observation: top 1% jackpot, top 10% lucky, bottom 10% hidden gem, the middle 80%
     discovery. A partial population says nothing about who is least popular, so there is no percentile until it is complete.
Anything else is neutral: a book outside the top 100 with no complete percentile gets the "off the beaten shelf" line, which only
states that it is not in the top 100. Absence is never turned into a rank. Every badge names its source and period.
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

import fetchguard
import source

TOP_URL = "https://www.gutenberg.org/browse/scores/top"
LISTS = {"month": "last 30 days", "week": "last 7 days", "day": "yesterday"}
MIN_ENTRIES = 20                    # a list shorter than this means the page changed shape: do not trust it
NOTE = "Source: Project Gutenberg's public most-read list, last 30 days. This is popularity, not literary quality."
LANGUAGE_NAMES = {"en": "English", "fr": "French", "zh": "Chinese"}

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


def _parse(html):
    found = {}
    for part in re.split(r"(?i)<h2\b", html)[1:]:
        heading, _sep, body = part.partition("</h2>")
        heading = re.sub(r"<[^>]+>", " ", heading).lower()
        if "ebook" not in heading:
            continue
        for key, phrase in LISTS.items():
            if phrase in heading and key not in found:
                entries = {}
                for m in re.finditer(r'(?is)<a\b[^>]*href="[^"]*?/ebooks/(\d+)"[^>]*>(.*?)</a>', body):
                    gid = int(m.group(1))
                    if gid in entries:
                        continue
                    count = re.search(r"\(([\d,]{1,12})\)\s*$", re.sub(r"<[^>]+>", "", m.group(2)).strip())
                    entries[gid] = int(count.group(1).replace(",", "")) if count else None
                found[key] = list(entries.items())[:100]
    return {k: v for k, v in found.items() if len(v) >= MIN_ENTRIES}


def parse_top_page(html):
    """{'month': {gutenberg_id: rank}, ...} from the 'Top 100 EBooks ...' lists on Gutenberg's top page. Unrecognised lists are left out."""
    return {key: {gid: rank for rank, (gid, _c) in enumerate(entries, start=1)} for key, entries in _parse(html).items()}


def parse_top_counts(html):
    """{'month': {gutenberg_id: visible download count or None}, ...} - the number shown beside each title, when there is one."""
    return {key: dict(entries) for key, entries in _parse(html).items()}


def deep_payload(language, rank, total, top_percent, downloads, observed_at, *, gid=None):
    """The percentile badge for one book, from a COMPLETE per-language population (registry.percentile). Texts in en / fr / zh."""
    pct, total, rank, downloads = max(1, int(top_percent or 100)), int(total or 0), int(rank or 0), int(downloads or 0)
    if total >= 100 and pct <= 1:
        tier = "jackpot"
    elif total >= 100 and pct <= 10:
        tier = "lucky"
    elif total >= 100 and pct >= 91:
        tier = "hidden-gem"
    else:
        tier = "discovery"
    date = time.strftime("%Y-%m-%d", time.gmtime(observed_at)) if observed_at else ""
    if language == "fr":
        n = lambda v: f"{v:,}".replace(",", "\u202f")
        label = {"jackpot": "Jackpot ! Un favori du top 1 % de Project Gutenberg !",
                 "lucky": "Belle trouvaille ! Parmi les 10 % les plus téléchargés !",
                 "hidden-gem": "Perle rare ! Parmi les 10 % les moins téléchargés — envie d’explorer ?",
                 "discovery": f"Une découverte · n° {n(rank)} par nombre de téléchargements"}[tier]
        detail = f"Rang n° {n(rank)} sur {n(total)} titres français · {n(downloads)} téléchargements déclarés par Gutendex"
        note = "Source : nombre de téléchargements déclaré par Gutendex (d’après Project Gutenberg), état au " + date + ". Popularité, pas qualité littéraire."
    elif language == "zh":
        label = {"jackpot": "头奖！Project Gutenberg 前 1% 的热门读物！", "lucky": "幸运发现！下载量位列前 10%！",
                 "hidden-gem": "遗珠！下载量最低的 10% 之一——想探索一下吗？", "discovery": f"新发现 · 下载量排名第 {rank:,} 位"}[tier]
        detail = f"在 {total:,} 种中文书籍中排名第 {rank:,} · Gutendex 报告的下载次数 {downloads:,}"
        note = f"来源：Gutendex 报告的下载量（数据来自 Project Gutenberg），截至 {date}。这代表受欢迎程度，而非文学价值。"
    else:
        label = {"jackpot": "Jackpot! A top 1% Gutenberg favourite!", "lucky": "Lucky find! Top 10% most downloaded!",
                 "hidden-gem": "Hidden gem alert! Among the least-downloaded 10% — would you like to explore?",
                 "discovery": f"A new discovery · ranked #{rank:,} by downloads"}[tier]
        detail = f"Rank #{rank:,} of {total:,} {LANGUAGE_NAMES.get(language, language)} titles · {downloads:,} downloads as reported by Gutendex"
        note = f"Source: download counts reported by Gutendex (from Project Gutenberg), as of {date}. This is popularity, not literary quality."
    return {"tier": tier, "label": label, "detail": detail, "note": note, "rank": rank, "total": total, "top_percent": pct,
            "downloads": downloads, "snapshot_date": date, "source": "Gutendex download counts", "period": "as reported",
            "source_url": f"https://www.gutenberg.org/ebooks/{gid}" if gid else TOP_URL}


class Popularity:
    def __init__(self, path, clock=time.time, max_age_hours=24.0, guard=None, registry=None):
        self.path, self._clock, self.max_age = Path(path), clock, float(max_age_hours) * 3600
        self.guard, self.registry = guard, registry
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

    def listed_ids(self):
        """Every Gutenberg id on any stored top list."""
        self._load()
        return {int(g) for key in LISTS for g in (self._data.get(key) or {})}

    def available(self):
        self._load()
        return bool(self._data.get("month"))

    def payload(self, language, gid, deep=None, observed=None):
        """The badge for one book, or None when there is nothing true to say. `deep` is a complete-population percentile badge
        (deep_payload), if any. `observed` ({'value', 'observed_at'}) is a lone Gutendex count: shown as a labelled fact, never ranked."""
        have_month = self.available()
        rank = self.rank(gid) if have_month else None
        if rank is not None:
            tier = "jackpot" if rank <= 10 else "lucky"
            kind, detail, note, url = tier, f"Ranked #{rank} in Gutenberg's monthly top 100 (last 30 days)", NOTE, TOP_URL
            source_name, period = "Project Gutenberg top 100", LISTS["month"]
        elif deep and deep.get("tier") in ("jackpot", "lucky", "discovery", "hidden-gem"):
            tier = deep["tier"]
            kind = "gem" if tier == "hidden-gem" else tier
            rank, detail, note, url = deep.get("rank"), deep.get("detail") or "", deep.get("note") or NOTE, deep.get("source_url") or TOP_URL
            source_name, period = deep.get("source") or "Gutendex download counts", deep.get("period") or "as reported"
        elif have_month:
            tier, kind, note, url = "offlist", "offlist", NOTE, TOP_URL        # neutral: only says "not in the top 100"
            detail = ""
            if observed and observed.get("value") is not None:
                when = time.strftime("%Y-%m-%d", time.gmtime(observed["observed_at"])) if observed.get("observed_at") else ""
                detail = f"{int(observed['value']):,} downloads as reported by Gutendex" + (f" on {when}" if when else "")
            source_name, period = "Project Gutenberg top 100", LISTS["month"]
        else:
            return None
        label, short = pick_line(kind, gid)
        stamp = self._data.get("updated") if have_month else None
        snapshot = time.strftime("%Y-%m-%d", time.gmtime(stamp)) if stamp else (deep or {}).get("snapshot_date", "")
        out = {"tier": tier, "label": label, "short": short, "detail": detail, "note": note, "rank": rank,
               "source_url": url, "snapshot_date": snapshot, "source": source_name, "period": period}
        if deep and tier == deep.get("tier") and rank == deep.get("rank"):
            out.update(total=deep.get("total"), top_percent=deep.get("top_percent"))
        return out

    def stale(self):
        self._load()
        return self._clock() - float(self._data.get("updated", 0)) > self.max_age

    def _fetch(self, opener, timeout):
        if opener is None and self.guard is not None:
            return self.guard.get(TOP_URL, timeout=timeout, max_bytes=3_000_000, retries=2).body.decode("utf-8", errors="replace")
        req = urllib.request.Request(TOP_URL, headers={"User-Agent": source.USER_AGENT})
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as response:
            return response.read(3_000_000).decode("utf-8", errors="replace")

    def refresh(self, opener=None, timeout=15):
        """Fetch and store the lists. True on success; on any problem the previous data stays and last_error says why.
        Each outcome is also recorded as a Registry run, and each good list becomes a dated snapshot per period."""
        run = self.registry.start_run("gutenberg_top") if self.registry else None
        try:
            html = self._fetch(opener, timeout)
            parsed = _parse(html)
            if "month" not in parsed:
                raise ValueError("the page no longer shows the 30-day top list in the expected form")
            lists = {k: {gid: rank for rank, (gid, _c) in enumerate(v, start=1)} for k, v in parsed.items()}
            data = {"updated": self._clock(), **{k: {str(g): r for g, r in v.items()} for k, v in lists.items()}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
            self.last_error = None
            if self.registry:
                for key, entries in parsed.items():
                    self.registry.add_top_list(key, ((gid, rank, count) for rank, (gid, count) in enumerate(entries, start=1)))
                self.registry.finish_run(run, "ok", records=sum(len(v) for v in parsed.values()), completeness=1.0)
            return True
        except (urllib.error.URLError, OSError, ValueError, TimeoutError, fetchguard.FetchError) as exc:
            self.last_error = str(getattr(exc, "reason", exc))
            if self.registry:
                self.registry.finish_run(run, "failed", error_class=getattr(exc, "error_class", type(exc).__name__), error=self.last_error)
            return False

    def refresh_if_stale(self, **kw):
        return self.refresh(**kw) if self.stale() else False
