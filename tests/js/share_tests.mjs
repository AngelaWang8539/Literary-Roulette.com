// Share card tests. Run: node tests/js/share_tests.mjs
import assert from "node:assert/strict";
import { excerpt, wrapLines, drawCard, shareOrDownload, CARD } from "../../static/js/sharecard.js";
let passed = 0, failed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("  ok   " + name); } catch (e) { failed++; console.log("  FAIL " + name + "\n       " + (e.stack || e)); } }
const measure = (t) => t.length * 20;

function fakeCtx() {
  const calls = []; const ctx = { font: "", fillStyle: "", strokeStyle: "", lineWidth: 0, textBaseline: "", textAlign: "",
    fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
    measureText: (t) => ({ width: measure(t) }), fillText: (t, x, y) => calls.push({ t, x, y }) };
  return { ctx, calls };
}

await test("an excerpt is whole words, short enough, and ends on a sentence when one is near", () => {
  const long = ("The quick brown fox jumps over the lazy dog. " .repeat(30)).trim();
  const e = excerpt(long, 120); assert.ok(e.length <= 121); assert.ok(e.endsWith(".") || e.endsWith("\u2026"));
  assert.equal(excerpt("Short text.", 120), "Short text.");
  assert.ok(!/\s{2}/.test(excerpt("a   b \n\n c", 50)));
  const zh = excerpt("\u4f60\u597d\u3002".repeat(100), 60); assert.ok(zh.length <= 61);
});
await test("lines never exceed the width, a giant word is split, and too many lines end with an ellipsis", () => {
  const lines = wrapLines(measure, "alpha beta gamma delta epsilon zeta eta theta iota kappa", 200);
  assert.ok(lines.every((l) => measure(l) <= 200)); assert.equal(lines.join(" "), "alpha beta gamma delta epsilon zeta eta theta iota kappa");
  const huge = wrapLines(measure, "x".repeat(60), 200); assert.ok(huge.every((l) => measure(l) <= 200)); assert.equal(huge.join(""), "x".repeat(60));
  const cut = wrapLines(measure, "word ".repeat(80).trim(), 200, 3); assert.equal(cut.length, 3); assert.ok(cut[2].endsWith("\u2026"));
  const zh = wrapLines(measure, "\u4f60\u597d\u4e16\u754c".repeat(20), 200); assert.ok(zh.every((l) => measure(l) <= 200));
});
await test("the card holds the passage, title, author, badge and footer, all inside the picture", () => {
  const { ctx, calls } = fakeCtx();
  const used = drawCard(ctx, { text: "A passage from an unnamed book. ".repeat(40), title: "The Lost World", author: "Arthur Conan Doyle", badge: "Jackpot! One of Gutenberg's 10 most-read books this month", host: "127.0.0.1:5000" });
  const texts = calls.map((c) => c.t);
  assert.ok(texts.includes("The Lost World")); assert.ok(texts.includes("by Arthur Conan Doyle")); assert.ok(texts.some((t) => t.startsWith("Jackpot")));
  assert.ok(texts.includes("Reading has gone completely random.")); assert.ok(texts.includes("127.0.0.1:5000"));
  assert.ok(used.body.length >= 1 && used.body.length <= CARD.maxExcerptLines);
  assert.ok(calls.every((c) => c.x >= 0 && c.x + measure(c.t) <= CARD.width && c.y > 0 && c.y < CARD.height));
});
await test("nothing overlaps: passage, rule, title, author, badge and footer stack in order even with a long title and badge", () => {
  const { ctx, calls } = fakeCtx();
  const badge = "Off the beaten shelf: not among Gutenberg's most-read books this month";
  const used = drawCard(ctx, { text: "word ".repeat(120), title: "Freedom's Battle: Being a Comprehensive Collection of Writings and Speeches", author: "Gandhi, Mahatma", badge, host: "h" });
  const y = (t) => calls.find((c) => c.t === t || c.t.startsWith(t)).y;
  const lastBody = calls.filter((c) => used.body.includes(c.t)).at(-1).y;
  const titleFirst = y(used.title[0]); const author = y("by Gandhi"); const badge1 = y("Off the beaten"); const footer = y("Reading has gone");
  assert.ok(lastBody + 50 < titleFirst - 40); assert.ok(titleFirst < author); assert.ok(author + 20 < badge1 - 20); assert.ok(badge1 + 80 < footer);
  const ys = calls.map((c) => c.y); assert.equal(new Set(ys).size, ys.length);
});
await test("a very long title and a missing badge still fit", () => {
  const { ctx, calls } = fakeCtx();
  const used = drawCard(ctx, { text: "Short.", title: "A Very Long Title ".repeat(12), author: "Someone", host: "h" });
  assert.ok(used.title.length <= 2); assert.ok(calls.every((c) => c.y < CARD.height));
});
await test("sharing uses the device share sheet when it can and downloads otherwise; cancelling is respected", async () => {
  const blob = new Blob(["png"], { type: "image/png" }); const shared = [];
  const nav = { canShare: () => true, share: async (d) => { shared.push(d); } };
  assert.equal(await shareOrDownload(blob, { title: "T", url: "https://x" }, nav, {}), "shared"); assert.equal(shared[0].files[0].type, "image/png");
  assert.equal(await shareOrDownload(blob, { title: "T", url: "https://x" }, { canShare: () => true, share: async () => { const e = new Error("x"); e.name = "AbortError"; throw e; } }, {}), "cancelled");
  let clicked = 0; const doc = { createElement: () => ({ click: () => clicked++, remove() {} }), body: { append() {} } };
  globalThis.URL.createObjectURL = () => "blob:x"; globalThis.URL.revokeObjectURL = () => {};
  assert.equal(await shareOrDownload(blob, { title: "T", url: "https://x" }, {}, doc), "downloaded"); assert.equal(clicked, 1);
});
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
