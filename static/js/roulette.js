// Random choices for books the reader uploaded (the public catalogue is chosen on the server).
// Same rules as catalogue.py: pick a BOOK uniformly first, then a passage, and avoid repeats.
const pick = (list, rng) => list[Math.floor(rng() * list.length)];

function pickUnseen(indices, seen, rng) {
  const fresh = indices.filter((i) => !seen.has(i));
  return pick(fresh.length ? fresh : indices, rng);
}
const range = (n) => Array.from({ length: n }, (_, i) => i);

// books: [{id, passages:[...]}]  ->  {bookId, index}
export function pickFirst(books, rng = Math.random) {
  const book = pick(books, rng);
  return { bookId: book.id, index: pick(range(book.passages.length), rng) };
}

// Another passage from the same book, or null when the book has no other passage.
export function pickOtherPassage(book, currentIndex, seen, rng = Math.random) {
  const options = range(book.passages.length).filter((i) => i !== currentIndex);
  return options.length ? pickUnseen(options, seen, rng) : null;
}

// Returns null when there is only one book, else {bookId, restarted}.
export function pickOtherBook(books, currentId, seenBooks, rng = Math.random) {
  const others = books.filter((b) => b.id !== currentId);
  if (!others.length) return null;
  const fresh = others.filter((b) => !seenBooks.has(b.id));
  return { bookId: pick(fresh.length ? fresh : others, rng).id, restarted: fresh.length === 0 };
}

export function pickInBook(book, seen, rng = Math.random) {
  return pickUnseen(range(book.passages.length), seen, rng);
}
