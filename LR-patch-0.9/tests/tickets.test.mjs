// Run from the project folder:  node tests/tickets.test.mjs
import assert from "node:assert/strict";
import { createTicketPouch } from "../static/js/tickets.js";

function harness(handler) {
  let t = 0; const calls = [];
  const api = async (path, body) => { calls.push({ path, body }); return handler(path, body, t); };
  const sleep = async (ms) => { t += ms; };
  const pouch = createTicketPouch({ api, language: "en", sleep, now: () => t });
  return { pouch, calls, clock: () => t };
}
const ticket = (n) => ({ book_token: "b" + n, token: "t" + n, ticket: "k" + n });

// 1. Source is stuck: after 8 s the pouch asks for prepared books only, and the visitor gets them.
{
  const h = harness((path, body) => {
    if (path === "/api/draw" && !body.ready_only) return { ok: true, pending: [{ handle: "h1", draw_number: 1, total: 1 }], source_state: "down", notice: "resting" };
    if (path === "/api/draw" && body.ready_only) return { ok: true, tickets: [ticket(9)], source_state: "down", notice: "resting" };
    if (path === "/api/draw/poll") return { ok: true, results: [{ handle: "h1", state: "queued", percent: 6 }] };
  });
  const notices = [];
  const r = await h.pouch.next({ onNotice: (m) => notices.push(m) });
  assert.equal(r.ok, true); assert.equal(r.ticket.book_token, "b9");
  assert.ok(h.clock() >= 8000 && h.clock() < 10000, "switched at about 8 s, got " + h.clock());
  assert.equal(notices.length, 1); assert.equal(h.pouch.info.notice, "resting"); assert.equal(h.pouch.info.sourceState, "down");
  h.pouch.stop();
}
// 2. Server ignores ready_only and still sends only cold picks: the visitor is told within ~14 s, not 70 s.
{
  const h = harness((path) => path === "/api/draw" ? { ok: true, pending: [{ handle: "h" + Math.random(), draw_number: 1, total: 1 }] }
    : { ok: true, results: [] });
  const r = await h.pouch.next();
  assert.equal(r.ok, false); assert.equal(r.code, "slow"); assert.ok(h.clock() < 16000, "gave up at " + h.clock());
  h.pouch.stop();
}
// 3. Normal case: a cold book arrives quickly, nothing changes.
{
  let polls = 0;
  const h = harness((path) => path === "/api/draw" ? { ok: true, pending: [{ handle: "h1", draw_number: 1, total: 1 }] }
    : { ok: true, results: [{ handle: "h1", state: ++polls < 3 ? "running" : "ready", percent: 50, ticket: ticket(1) }] });
  const r = await h.pouch.next();
  assert.equal(r.ok, true); assert.ok(h.clock() < 8000);
  assert.equal(h.calls.filter((c) => c.body?.ready_only).length, 0);
  h.pouch.stop();
}
console.log("tickets.js: 3 scenarios OK");
