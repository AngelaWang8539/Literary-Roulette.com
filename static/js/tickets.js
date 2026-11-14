// The visitor's small pouch of sealed passage tickets, kept full in the background.
//
// * Every draw is a uniform pick from the whole catalogue (server side). Books already in the shared "lake"
//   come back at once; fresh picks that still have to be prepared come back as `pending` handles.
// * Pending picks are polled with real stages and percentages. The first one that is ready becomes the
//   first passage; the others join the pouch when they arrive, so the next spins are instant.
// * While the catalogue is still being opened (first run), the wait is reported with the real progress.
// * A ticket counts as read only when it is displayed. "Seen" lists belong to this visitor and are sent with
//   each request; the server keeps no per-visitor state.
const sleepFor = (ms) => new Promise((r) => setTimeout(r, ms));

export function createTicketPouch({ api, language, batch = 3, lowWater = 1, maxSeenBooks = 120, maxSeenPassages = 200,
                                    pollMs = 700, sleep = sleepFor }) {
  let queue = [], seenBooks = [], current = null, refilling = null, companion = null, bg = null;
  const pending = new Map();                       // handle -> {startedAt, drawNumber, total, percent, stage, message}
  const seenPassages = new Map();
  const info = { mode: "ocean", total: 0 };

  const rememberBook = (token) => { seenBooks = seenBooks.filter((t) => t !== token).concat(token).slice(-maxSeenBooks); };
  const passageSet = (bookToken) => { if (!seenPassages.has(bookToken)) seenPassages.set(bookToken, new Set()); return seenPassages.get(bookToken); };
  const known = () => new Set([current?.book_token, ...queue.map((t) => t.book_token), ...pending.keys()].filter(Boolean));
  const addTicket = (t) => { if (t && !queue.some((q) => q.book_token === t.book_token) && current?.book_token !== t.book_token) queue.push(t); };

  function summary() {
    let best = null;
    for (const p of pending.values()) if (!best || p.percent > best.percent) best = p;
    return best ? { ...best, count: pending.size } : null;
  }

  function stopBackground() { if (bg) { clearInterval(bg); bg = null; } }
  function watchInBackground() {
    if (bg || !pending.size) return;
    bg = setInterval(async () => { await pollOnce().catch(() => {}); if (!pending.size) stopBackground(); }, 1500);
  }

  async function pollOnce() {
    if (!pending.size) return;
    const res = await api("/api/draw/poll", { language, handles: [...pending.keys()] });
    if (!res.ok) return;
    for (const r of res.results) {
      const p = pending.get(r.handle);
      if (!p) continue;
      if (r.state === "ready" && r.ticket) { addTicket(r.ticket); pending.delete(r.handle); }
      else if (r.state === "failed") pending.delete(r.handle);
      else Object.assign(p, { percent: r.percent ?? p.percent, stage: r.stage || p.stage, message: r.message || p.message });
    }
  }

  function refill() {
    if (refilling) return refilling;
    const avoid = [...known()].slice(-20);
    refilling = api("/api/draw", { language, count: batch, seen: seenBooks, avoid }).then((res) => {
      if (res.ok) {
        info.mode = res.mode || info.mode; info.total = res.total ?? info.total;
        if (res.restarted) seenBooks = [];                          // the visitor has met every book: a fresh round
        for (const t of res.tickets || []) addTicket(t);
        for (const p of res.pending || []) {
          if (!pending.has(p.handle)) pending.set(p.handle, { startedAt: Date.now(), drawNumber: p.draw_number, total: p.total, percent: 6, stage: "queued", message: "" });
        }
        watchInBackground();
      }
      return res;
    }).finally(() => { refilling = null; });
    return refilling;
  }

  return {
    info,
    get buffered() { return queue.length; },
    get waiting() { return pending.size; },
    get current() { return current; },
    refill, stop: stopBackground,
    reset() { queue = []; seenBooks = []; seenPassages.clear(); pending.clear(); current = null; companion = null; stopBackground(); },

    /** The next different-book ticket. Waits (with progress callbacks) only if the pouch ran dry. While the
     *  catalogue is still being opened (first run) it waits and reports the real progress through onOpening. */
    async next({ onProgress, onOpening } = {}) {
      for (let guard = 0; guard < 600; guard++) {
        if (queue.length) { return { ok: true, ticket: queue.shift() }; }
        if (!pending.size) {
          const res = await refill();
          if (!res.ok) {
            if (res.code === "catalogue_opening") { onOpening?.(res.progress || {}, info); await sleep(Math.max(300, (res.retry_after || 1) * 800)); continue; }
            return res;
          }
          if (!queue.length && !pending.size) return { ok: false, code: "only_one_book", message: "Only one usable book is available in this collection." };
          continue;
        }
        onProgress?.(summary(), info);
        await sleep(pollMs);
        await pollOnce();
      }
      return { ok: false, code: "preparing", message: "The librarian is taking longer than expected. Please try again shortly." };
    },

    /** Call when a ticket has actually been displayed. */
    shown(ticket) {
      current = ticket; rememberBook(ticket.book_token);
      if (queue.length + pending.size <= lowWater) refill().catch(() => {});     // quiet top-up
      const set = passageSet(ticket.book_token); set.add(ticket.token);
      if (set.size > maxSeenPassages) set.delete(set.values().next().value);
      companion = { bookToken: ticket.book_token, promise: null };
      companion.promise = api("/api/passage/another", { language, book_token: ticket.book_token, ticket: ticket.ticket, seen: [...set] })
        .catch(() => ({ ok: false }));
    },

    /** Another passage from the book on screen: the prefetched one if it is ready, else ask now. */
    async another(shownTicket) {
      const c = companion;
      companion = null;
      if (c && c.bookToken === shownTicket.book_token && c.promise) {
        const res = await c.promise;
        if (res.ok && !passageSet(c.bookToken).has(res.token)) return res;
      }
      return api("/api/passage/another", { language, book_token: shownTicket.book_token, ticket: shownTicket.ticket,
        seen: [...passageSet(shownTicket.book_token)] });
    },
  };
}
