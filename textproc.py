"""Turn Project Gutenberg plain text into clean reading text, then into roulette passages.

static/js/chunker.js is a JavaScript copy of the generic rules (used for uploaded books).
If you change a rule here, change it there too and bump RULES_VERSION.
"""
import re

RULES_VERSION = "v1"
MIN_WORDS, MAX_WORDS, TINY_WORDS = 80, 150, 40

START_RE = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*\*\*", re.I)
END_RE = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)
HEADING_RE = re.compile(r"^(chapter|book|part|volume|act|scene|stave|letter|section|epilogue|prologue|"
                        r"preface|contents|illustrations|appendix)\b", re.I)
ROMAN_RE = re.compile(r"^[IVXLCDM]+\.?$")
SENT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\u201d\"\u2019)\]])\s+")


def word_count(text):
    return len(text.split())


# ---- Gutenberg wrapper ---------------------------------------------------
def header_fields(raw):
    m = START_RE.search(raw)
    head = raw[: m.start()] if m else raw[:6000]
    fields = {}
    for key in ("Title", "Author", "Language", "Release date", "Translator"):
        found = re.search(rf"^{key}:\s*(.+)$", head, re.I | re.M)
        if found:
            fields[key.lower().replace(" ", "_")] = found.group(1).strip()
    return fields


def strip_boilerplate(raw):
    """Text between the START and END markers, or None if either marker is missing."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    s, e = START_RE.search(raw), END_RE.search(raw)
    if not s or not e or e.start() <= s.end():
        return None
    return raw[s.end(): e.start()].strip("\n")


# ---- paragraphs -----------------------------------------------------------
def to_paragraphs(text):
    """Split on blank lines. Re-join wrapped prose lines; keep line breaks in verse."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        lengths = [len(ln) for ln in lines]
        verse = len(lines) >= 3 and sum(lengths) / len(lines) < 48 and max(lengths) <= 72
        out.append("\n".join(lines) if verse else " ".join(lines))
    return out


def clean_paragraph(par):
    par = re.sub(r"\[Illustration[^\]]*\]", "", par)
    par = re.sub(r"\[\d+\]", "", par).replace("_", "")
    return re.sub(r"[ \t]+", " ", par).strip()


def is_story(par):
    letters = sum(c.isalpha() for c in par)
    if letters < 2 or letters / len(par) < 0.55 or par.upper() == par:
        return False
    if par.lstrip("[ ").lower().startswith(("illustration", "footnote", "transcriber")):
        return False
    if HEADING_RE.match(par) and word_count(par) <= 12:
        return False
    if ROMAN_RE.match(par.strip()) or "project gutenberg" in par.lower():
        return False
    return True


def find_story_start(pars, start_pattern=None):
    """Index of the first story paragraph and whether we are confident about it."""
    if start_pattern:
        rx = re.compile(start_pattern)
        for i, p in enumerate(pars):
            if rx.search(p):
                return i, True
        return 0, False
    run = 0
    for i, p in enumerate(pars):
        run = run + 1 if word_count(p) >= 25 and is_story(p) else 0
        if run >= 3:
            return i - 2, True
    return 0, False


def find_story_end(pars, end_pattern=None):
    n = len(pars)
    rx = re.compile(end_pattern) if end_pattern else None
    for i in range(int(n * 0.9), n):
        p = pars[i].strip()
        if (rx and rx.search(p)) or re.match(r"^(the end|finis)\.?$", p, re.I) or p.lower().startswith("transcriber"):
            return i
    return n


def clean_book_text(raw, start_pattern=None, end_pattern=None):
    """Returns (clean_text, info). info['warnings'] lists anything a human should check."""
    body = strip_boilerplate(raw)
    if body is None:
        return None, {"warnings": ["Project Gutenberg start/end markers not found."]}
    pars = [clean_paragraph(p) for p in to_paragraphs(body)]
    pars = [p for p in pars if p]
    start, confident = find_story_start(pars, start_pattern)
    end = find_story_end(pars[start:], end_pattern) + start
    story = [p for p in pars[start:end] if is_story(p)]
    warnings = []
    if not confident:
        warnings.append("Story start not located with confidence; review first_chars in the manifest.")
    text = "\n\n".join(story)
    return text, {"warnings": warnings, "first_chars": text[:140], "last_chars": text[-140:]}


# ---- passages --------------------------------------------------------------
def _group(units, sep, min_words=MIN_WORDS, max_words=MAX_WORDS):
    out, cur, n = [], [], 0
    for u in units:
        w = word_count(u)
        if cur and n + w > max_words and (n >= min_words or n + w > max_words * 1.3):
            if n >= TINY_WORDS:
                out.append(sep.join(cur))
            cur, n = [], 0
        cur.append(u)
        n += w
    if cur:
        if n >= TINY_WORDS:
            out.append(sep.join(cur))
        elif out:
            out[-1] += sep + sep.join(cur)
    return out


def split_passages(text, min_words=MIN_WORDS, max_words=MAX_WORDS):
    """Passages of about min..max words that respect paragraph boundaries."""
    out, run = [], []
    for par in to_paragraphs(text):
        if word_count(par) > max_words:
            out += _group(run, "\n\n", min_words, max_words)
            run = []
            verse = "\n" in par
            units = [u for u in (par.split("\n") if verse else SENT_RE.split(par)) if u.strip()]
            out += _group(units, "\n" if verse else " ", min_words, max_words)
        else:
            run.append(par)
    return out + _group(run, "\n\n", min_words, max_words)
