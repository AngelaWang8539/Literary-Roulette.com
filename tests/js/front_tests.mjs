// Front-end logic tests. Run: node tests/js/front_tests.mjs
import assert from "node:assert/strict";
import { createTicketPouch } from "../../static/js/tickets.js";
import * as Scene from "../../static/js/librarian-scene.js";

let passed = 0, failed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("  ok   " + name); } catch (e) { failed++; console.log("  FAIL " + name + "\n       " + (e.stack || e)); } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------- ticket pouch ----------------
function fakeServer({ pendingFirst = 0, stages = [] } = {}) {
  const calls = []; let n = 0, polls = 0; const waiting = [];
  const ticket = () => { n++; return { ticket: "t" + n, token: "p" + n, book_token: "b" + n, text: "text " + n }; };
  const api = async (path, body) => {
    calls.push([path, body]);
    if (path === "/api/draw") {
      if (body.starter) return { ok: true, mode: "starter", tickets: [{ ...ticket(), text: "starter" }], pending: [] };
      if (pendingFirst && !waiting.length && calls.filter((c) => c[0] === "/api/draw").length === 1) {
        const hs = Array.from({ length: pendingFirst }, (_, i) => { n++; waiting.push("h" + n); return { handle: "h" + n, draw_number: 40 + i, total: 800 }; });
        return { ok: true, mode: "ocean", total: 800, tickets: [], pending: hs, restarted: false };
      }
      return { ok: true, mode: "ocean", total: 800, restarted: false, pending: [], tickets: Array.from({ length: body.count }, ticket) };
    }
    if (path === "/api/draw/poll") {
      polls++;
      return { ok: true, results: body.handles.map((h) => polls < 3 ? { handle: h, state: "working", stage: stages[polls - 1] || "fetching", percent: 20 + polls * 20, message: "Fetching this edition" }
        : { handle: h, state: "ready", percent: 100, ticket: { ticket: "T" + h, token: "P" + h, book_token: "B" + h, text: "ready " + h } }) };
    }
    if (path === "/api/passage/another") { n++; return { ok: true, ticket: "t" + n, token: "p" + n, book_token: body.book_token, text: "more " + n }; }
  };
  return { api, calls };
}

await test("a warm lake: first spin waits once, later spins come from the pouch", async () => {
  const { api, calls } = fakeServer(); const pouch = createTicketPouch({ api, language: "en", batch: 3 });
  const a = await pouch.next(); assert.ok(a.ok); pouch.shown(a.ticket);
  const draws = () => calls.filter((c) => c[0] === "/api/draw").length;
  assert.equal(pouch.buffered, 2); assert.equal(draws(), 1);
  const b = await pouch.next(); pouch.shown(b.ticket); await sleep(5);
  assert.equal(draws(), 2); assert.notEqual(a.ticket.book_token, b.ticket.book_token); pouch.stop();
});

await test("a cold first pick: real stages are reported and the first ready book is shown", async () => {
  const { api } = fakeServer({ pendingFirst: 3, stages: ["fetching", "indexing"] });
  const pouch = createTicketPouch({ api, language: "en", pollMs: 5 }); const seen = [];
  const res = await pouch.next({ onProgress: (p) => seen.push(p) });
  assert.ok(res.ok); assert.ok(res.ticket.text.startsWith("ready"));
  assert.ok(seen.length >= 2); assert.equal(seen[0].drawNumber, 40); assert.equal(seen[0].total, 800);
  assert.ok(seen.every((p, i) => i === 0 || p.percent >= seen[i - 1].percent));
  assert.equal(pouch.buffered, 2);                       // the other two picks joined the pouch for instant spins
  pouch.stop();
});

await test("first run: while the catalogue opens the wait is reported with real progress, then the first book arrives", async () => {
  let draws = 0; const opening = [];
  const api = async (path, body) => {
    if (path === "/api/draw") {
      draws++;
      if (draws <= 2) return { ok: false, code: "catalogue_opening", retry_after: 0, progress: { message: `Downloading catalogue\u2026 ${draws * 20}%`, percent: draws * 20 } };
      return { ok: true, mode: "ocean", total: 800, restarted: false, pending: [], tickets: [{ ticket: "t1", token: "p1", book_token: "b1", text: "hello" }] };
    }
  };
  const pouch = createTicketPouch({ api, language: "en", pollMs: 5, sleep: () => sleep(1) });
  const res = await pouch.next({ onOpening: (p) => opening.push(p) });
  assert.ok(res.ok); assert.deepEqual(opening.map((p) => p.percent), [20, 40]); pouch.stop();
});

await test("refill avoids the book on screen and sends the visitor's own seen list", async () => {
  const { api, calls } = fakeServer(); const pouch = createTicketPouch({ api, language: "fr", batch: 2 });
  const a = await pouch.next(); pouch.shown(a.ticket); const b = await pouch.next(); pouch.shown(b.ticket); await sleep(5);
  const last = calls.filter((c) => c[0] === "/api/draw").pop()[1];
  assert.equal(last.language, "fr"); assert.ok(last.seen.includes(a.ticket.book_token)); assert.ok(last.avoid.includes(b.ticket.book_token)); pouch.stop();
});

await test("another passage uses the prefetched companion without a second request", async () => {
  const { api, calls } = fakeServer(); const pouch = createTicketPouch({ api, language: "en" });
  const a = await pouch.next(); pouch.shown(a.ticket); await sleep(5);
  const before = calls.length; const res = await pouch.another(a.ticket);
  assert.ok(res.ok); assert.equal(calls.length, before); pouch.stop();
});

await test("a ticket counts as read only when shown", async () => {
  const { api } = fakeServer(); const pouch = createTicketPouch({ api, language: "en" });
  const a = await pouch.next(); assert.equal(pouch.current, null); pouch.shown(a.ticket); assert.equal(pouch.current.ticket, a.ticket.ticket); pouch.stop();
});

// ---------------- the librarian drawing ----------------
await test("every scene draws; walking changes frame to frame; the trolley collects more books each pass", () => {
  for (const mode of ["catalogue", "load", "import"]) {
    const s = Scene.newScene(560); Scene.startScene(s, mode, 560);
    const f1 = Scene.drawScene(s, 560); Scene.tickScene(s, 560); Scene.tickScene(s, 560);
    assert.ok(f1.includes("<line") && f1.includes("<ellipse")); assert.notEqual(f1, Scene.drawScene(s, 560));
  }
  const s = Scene.newScene(560); Scene.startScene(s, "catalogue", 560); const start = s.books;
  for (let i = 0; i < 200; i++) Scene.tickScene(s, 560);
  assert.ok(s.books > start && s.books <= 7);
});
await test("terminal scenes: INDEXED! stamp on success, dropped book and speech bubble on failure, then they hold still", () => {
  const ok = Scene.newScene(560); Scene.finishScene(ok, true); assert.ok(Scene.drawScene(ok, 560).includes("INDEXED!"));
  const bad = Scene.newScene(560); Scene.finishScene(bad, false); assert.ok(Scene.drawScene(bad, 560).includes("Oops! A book escaped."));
  for (let i = 0; i < Scene.TERMINAL_FRAMES + 2; i++) Scene.tickScene(ok, 560); assert.equal(ok.mode, "complete_still");
});

// ---------------- librarian overlay: the one-second rule ----------------
function makeEl() {
  const kids = {};
  const el = { hidden: true, textContent: "", innerHTML: "", clientWidth: 600, style: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, querySelector(sel) { return kids[sel] || (kids[sel] = makeEl()); } };
  return el;
}
const root = makeEl();
globalThis.document = { getElementById: () => root };
const Librarian = await import("../../static/js/librarian.js");
const caption = () => root.querySelector(".caption").textContent;

await test("a task that finishes before one second never shows the librarian", async () => {
  const op = Librarian.begin("load", "x"); await sleep(150); op.done(); await sleep(50);
  assert.equal(root.hidden, true);
});
await test("after one second it appears with the real caption and percentage; done shows the stamp, then hides", async () => {
  const op = Librarian.begin("catalogue", "Opening the catalogue"); await sleep(Librarian.SHOW_AFTER_MS + 100);
  assert.equal(root.hidden, false); assert.equal(caption(), "Opening the catalogue");
  op.update("Fetching this edition", { mode: "load", percent: 42 });
  assert.equal(caption(), "Fetching this edition"); assert.equal(root.querySelector(".pct").textContent, "42%");
  assert.equal(root.querySelector(".bar i").style.width, "42%");
  op.done(); assert.ok(root.querySelector("svg").innerHTML.includes("INDEXED!"));
  await sleep(1700); assert.equal(root.hidden, true);
});
await test("a wait that is already known to be real (a cold book) shows the librarian at once, not after one second", async () => {
  const op = Librarian.begin("catalogue", "Opening the catalogue"); op.show();
  assert.equal(root.hidden, false); assert.equal(caption(), "Opening the catalogue");
  op.update("Downloading catalogue from Project Gutenberg\u2026 42%", { percent: 30 }); assert.equal(root.querySelector(".pct").textContent, "30%");
  op.done(); await sleep(1700); assert.equal(root.hidden, true);
});
await test("a fast failure shows no drawing; a slow failure shows the friendly dropped-book scene", async () => {
  Librarian.begin("load", "x").fail("nope"); assert.equal(root.hidden, true);
  const op = Librarian.begin("load", "x"); await sleep(Librarian.SHOW_AFTER_MS + 100); op.fail("Could not reach the site.");
  assert.ok(root.querySelector("svg").innerHTML.includes("Oops! A book escaped.")); assert.equal(caption(), "Could not reach the site.");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
