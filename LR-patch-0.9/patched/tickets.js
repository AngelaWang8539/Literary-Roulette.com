// The visitor's small pouch of sealed passage tickets, kept full in the background.
// * Every draw is a uniform pick from the whole catalogue (server side). Books already in the shared "lake"
//   come back at once; fresh picks that still have to be prepared come back as `pending` handles.
// * Pending picks are polled with real stages and percentages. The first one that is ready becomes the
//   first passage; the others join the pouch when they arrive, so the next spins are instant.
// * While the catalogue is still being opened (first run), the wait is reported with the real progress.
// * A ticket counts as read only when it is displayed. "Seen" lists belong to this visitor and are sent with
//   each request; the server keeps no per-visitor state.
const sleepFor = (ms) => new Promise((r) => setTimeout(r, ms));

export function createTicketPouch({ api, language, batch = 3, lowWater = 1, maxSeenBooks = 120, maxSeenPassages = 200,
                                    pollMs = 700, sleep = sleepFor, stallMs = 25000, maxWaitMs = 70000,
                                    coldBudgetMs = 8000, graceMs = 5000, maxPendingMs = 120000, now = Date.now }) {
  let queue = [], seenBooks = [], current = null, refilling = null, companion = null, bg = null;
  const pending = new Map();                       // handle -> {startedAt, drawNumber, total, percent, stage, message}
  const seenPassages = new Map();
  const info = { mode: "ocean", total: 0, sourceState: "ok", notice: "" };

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
    bg = setInterval(async () => {
      for (const [h, p] of [...pending]) if (now() - p.startedAt > maxPendingMs) pending.delete(h);   // never poll forever for one book
      await pollOnce().catch(() => {});
      if (!pending.size) stopBackground();
    }, 1500);
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
      else {
        const next = { percent: r.percent === undefined ? p.percent : r.percent, stage: r.stage || p.stage, message: r.message || p.message, ahead: r.ahead || 0 };
        if (next.percent !== p.percent || next.stage !== p.stage || next.message !== p.message) p.movedAt = now();   // it moved: not stalled
        Object.assign(p, next);
      }
    }
  }

  /** `readyOnly`: ask for books that are already prepared (the server skips fresh downloads). */
  function refill({ readyOnly = false } = {}) {
    if (refilling) return refilling;
    const avoid = [...known()].slice(-20);
    const body = { language, count: batch, seen: seenBooks, avoid };
    if (readyOnly) body.ready_only = true;
    refilling = api("/api/draw", body).then((res) => {
      if (res.ok) {
        info.mode = res.mode || info.mode; info.total = res.total ?? info.total;
        info.sourceState = res.source_state || "ok"; info.notice = res.notice || "";
        if (res.restarted) seenBooks = [];                          // the visitor has met every book: a fresh round
        for (const t of res.tickets || []) addTicket(t);
        for (const p of res.pending || []) {
          if (!pending.has(p.handle)) pending.set(p.handle, { startedAt: now(), movedAt: now(), drawNumber: p.draw_number, total: p.total, percent: 6, stage: "queued", message: "", ahead: 0 });
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
    async next({ onProgress, onOpening, onNotice } = {}) {
      const began = now();
      let warmAt = 0;
      for (let guard = 0; guard < 600; guard++) {
        if (queue.length) { return { ok: true, ticket: queue.shift() }; }
        if (now() - began > maxWaitMs) return { ok: false, code: "slow", message: "Project Gutenberg is very slow right now. Please try another spin in a moment." };
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
        await pollOnce().catch(() => {});
        if (queue.length) continue;
        // 8-second budget: a fresh download is taking too long, so switch to books that are already prepared. Those pending
        // downloads keep going in the background and join the pouch if they finish.
        if (!warmAt && now() - began > coldBudgetMs) {
          warmAt = now();
          onNotice?.("The book source is slow, so the librarian is picking from books already on the shelf.");
          await refill({ readyOnly: true }).catch(() => null);
          if (queue.length) continue;
        }
        if (warmAt && now() - warmAt > graceMs) return { ok: false, code: "slow", message: "The book source is very slow right now. Please try another spin in a moment." };
        for (const [handle, p] of [...pending]) {      // watchdog: a book that has not moved for a long time is replaced, never waited on forever
          if (now() - p.movedAt > stallMs) { pending.delete(handle); onNotice?.("Gutenberg is slow for this one, so the librarian is drawing another book."); }
        }
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
