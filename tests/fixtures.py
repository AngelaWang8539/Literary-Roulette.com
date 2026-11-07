"""Synthetic stand-ins for Project Gutenberg files. The words are invented; nothing here is a real book."""
import hashlib
import json
import random
import textwrap
from pathlib import Path

WORDS = "lantern orchard marble whisper copper river hollow thistle compass velvet harbour ember cinder meadow quartz anchor ribbon saddle winter lamp silver clock garden window letter shadow".split()


def sentence(rng, n=None):
    n = n or rng.randint(6, 16)
    s = " ".join(rng.choice(WORDS) for _ in range(n))
    return s[0].upper() + s[1:] + rng.choice([".", ".", ".", "!", "?"])


def paragraph(rng, sentences=None):
    return " ".join(sentence(rng) for _ in range(sentences or rng.randint(2, 7)))


def fake_gutenberg(title="Test Tale", author="Ann Author", paragraphs=90, seed=1, language="English", end_marker=True):
    rng = random.Random(seed)
    wrap = lambda t: "\n".join(textwrap.wrap(t, 70))
    body = [f"{title.upper()}\n\nby {author}", "CONTENTS\n\n  CHAPTER I.\n  CHAPTER II.\n  CHAPTER III.",
            "PREFACE", "A short note.", "CHAPTER I."]
    for i in range(paragraphs):
        if i % 25 == 24:
            body.append(f"CHAPTER {i // 25 + 2}.")
        if i == 10:
            body.append("\n".join(sentence(rng, 5) for _ in range(6)))      # short lines: verse-like
        elif i == 20:
            body.append(wrap(paragraph(rng, 30)))                              # one very long paragraph
        elif i % 7 == 3:
            body.append('"' + sentence(rng, 4) + '"')                          # dialogue
        else:
            body.append(wrap(paragraph(rng)))
    body += ["THE END", "Transcriber's Note: invented fixture."]
    head = f"The Project Gutenberg eBook of {title}\n\nTitle: {title}\nAuthor: {author}\nLanguage: {language}\n\n"
    text = head + f"*** START OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\n\n" + "\n\n".join(body) + "\n\n"
    if end_marker:
        text += f"*** END OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\n\nLicense text.\n"
    return text


def install_text_book(texts_dir, book_id, raw, verified=True, **book):
    """Clean `raw` like the ingest tool does and write text + manifest entry. Returns the cleaned text."""
    import textproc
    texts_dir = Path(texts_dir)
    texts_dir.mkdir(parents=True, exist_ok=True)
    text, _ = textproc.clean_book_text(raw, book.get("text_start_pattern"))
    (texts_dir / f"{book_id}.txt").write_text(text, encoding="utf-8")
    mpath = texts_dir / "manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
    manifest[book_id] = {"verified": verified, "problems": [] if verified else ["test failure"],
                         "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         "words": textproc.word_count(text), "passages": len(textproc.split_passages(text))}
    mpath.write_text(json.dumps(manifest))
    return text, manifest
