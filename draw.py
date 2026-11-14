"""Fair random selection. Book first, then a passage inside it.

Choosing from one giant list of passages would over-represent long books, so every draw picks a
book uniformly (among eligible, ready, not-recently-seen books) and only then a passage.
"""
import random


def pick_books(ids, token_of, count, seen_tokens, rng=random, avoid_ids=()):
    """Up to `count` distinct books. Returns (book_ids, restarted).

    `seen_tokens` is the visitor's own recent history (never shared between visitors). When the
    visitor has met everything, the round restarts: `restarted` is True and seen is ignored.
    """
    avoid, picked, n = set(avoid_ids), [], len(ids)
    if not n:
        return [], False

    def usable(bid):
        return bid not in avoid and bid not in picked and token_of(bid) not in seen_tokens

    if n > 256:                                    # big shelf: a few random probes beat scanning it all
        for _ in range(count * 30):
            if len(picked) >= count:
                break
            bid = rng.choice(ids)
            if usable(bid):
                picked.append(bid)
    if len(picked) < count:
        fresh = [b for b in ids if usable(b)]
        rng.shuffle(fresh)
        picked += fresh[:count - len(picked)]
    restarted = False
    if len(picked) < count:
        rest = [b for b in ids if b not in avoid and b not in picked]
        rng.shuffle(rest)
        extra = rest[:count - len(picked)]
        restarted = bool(extra)
        picked += extra
    return picked, restarted


def pick_passage_index(total, key_of, seen_keys, rng=random, exclude_index=None):
    """A passage number within one book, avoiding `exclude_index` and passages already seen."""
    if total <= 0 or (total == 1 and exclude_index == 0):
        return None
    for _ in range(24):
        i = rng.randrange(total)
        if i != exclude_index and key_of(i) not in seen_keys:
            return i
    fresh = [i for i in range(total) if i != exclude_index and key_of(i) not in seen_keys]
    pool = fresh or [i for i in range(total) if i != exclude_index]
    return rng.choice(pool) if pool else None
