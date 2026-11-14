"""Language-aware passage splitting. Bump the version string whenever a rule changes.

The segment version is stored with every prepared book, so a rule change makes old
prepared copies stale and the background worker rebuilds them (visitors never wait).
"""
import re

import textproc


def segment_version(language):
    return f"zh-{textproc.RULES_VERSION}" if language == "zh" else f"prose-{textproc.RULES_VERSION}"


def _split_chinese(text, target=780, maximum=1150):
    """Group Chinese paragraphs/sentences into passage-sized blocks, preserving punctuation."""
    blocks = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    units = []
    for block in blocks:
        if len(block) <= maximum:
            units.append(block)
        else:
            units.extend(x.strip() for x in re.split(r"(?<=[。！？；])", block) if x.strip())
    passages, current = [], ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if current and len(candidate) > maximum and len(current) >= 300:
            passages.append(current)
            current = unit
        else:
            current = candidate
        if len(current) >= target:
            passages.append(current)
            current = ""
    if current.strip():
        if passages and len(current) < 260 and len(passages[-1]) + len(current) <= maximum:
            passages[-1] += "\n\n" + current
        else:
            passages.append(current)
    return [p for p in passages if len(p.strip()) >= 120]


def split(text, language):
    return _split_chinese(text) if language == "zh" else textproc.split_passages(text)
