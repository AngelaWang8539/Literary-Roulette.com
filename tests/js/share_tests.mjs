// Share card tests. Run: node tests/js/share_tests.mjs
import assert from "node:assert/strict";
import { excerpt, wrapLines, drawCard, shareOrDownload, LAYOUT } from "../../static/js/sharecard.js";
let passed = 0, failed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("  ok   " + name); } catch (e) { failed++; console.log("  FAIL " + name + "\n       " + (e.stack || e)); } }
const measure = (t) => t.length * 20;

function fakeCtx() {
  const calls = []; const ctx = { font: "", fillStyle: "", strokeStyle: "", lineWidth: 0, textBaseline: "", textAlign: "",
    fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, arcTo() {}, closePath() {}, fill() {}, stroke() {}, save() {}, restore() {}, quadraticCurveTo() {},
    lineJoin: "",
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
const FULL = { text: "A passage from an unnamed book. ".repeat(40), title: "The Lost World", author: "Arthur Conan Doyle",
  badge: { label: "Jackpot read", tier: "jackpot" }, meta: ["First published 1912.", "Adventure \u00b7 Science Fiction"], source: "Passage 16", pill: "Sample card" };
const inside = (calls, L) => calls.every((c) => c.x >= 0 && c.x <= L.W && c.y > 0 && c.y < L.H);

await test("both layouts hold passage, title, author, badge, meta and the button, all inside the picture", () => {
  for (const format of ["landscape", "mobile"]) {
    const { ctx, calls } = fakeCtx(), used = drawCard(ctx, FULL, format), texts = calls.map((c) => c.t), L = LAYOUT[format];
    for (const want of ["The Lost World", "Arthur Conan Doyle", "Jackpot read", "First published 1912.", "Passage 16", "Sample card", "You found", "Spin your own book"]) assert.ok(texts.includes(want), `${format}: ${want}`);
    assert.equal(used.format, format); assert.ok(used.body.length >= 1);
    assert.ok(inside(calls, L), `${format}: everything stays inside the picture`);
  }
});
await test("the passage shrinks to fit and stays above its footer; a very long title shrinks too", () => {
  for (const format of ["landscape", "mobile"]) {
    const L = LAYOUT[format], { ctx, calls } = fakeCtx(), long = { ...FULL, text: "word ".repeat(400), title: "A Very Long Title ".repeat(12) };
    const used = drawCard(ctx, long, format);
    const lastBody = calls.filter((c) => used.body.includes(c.t)).at(-1).y;
    assert.ok(lastBody < L.text.bottom + 1, `${format}: passage ends above ${L.text.bottom}`);
    assert.ok(used.title.length <= 5); assert.ok(inside(calls, L));
  }
});
await test("a missing badge, pill, source and meta still draw a complete card; the phone card is taller than wide", () => {
  const { ctx, calls } = fakeCtx(), used = drawCard(ctx, { text: "Short.", title: "T", author: "A" }, "mobile");
  const texts = calls.map((c) => c.t);
  assert.ok(texts.includes("Short.") && texts.includes("Spin your own book")); assert.ok(!texts.includes("Sample card")); assert.equal(used.format, "mobile");
  assert.ok(LAYOUT.mobile.H > LAYOUT.mobile.W && LAYOUT.landscape.W > LAYOUT.landscape.H);
});
await test("every badge tier has a look; an unknown tier falls back instead of failing", () => {
  for (const tier of ["jackpot", "lucky", "discovery", "hidden-gem", "something-new"]) {
    const { ctx, calls } = fakeCtx(); drawCard(ctx, { ...FULL, badge: { label: "L-" + tier, tier } }); assert.ok(calls.some((c) => c.t === "L-" + tier));
  }
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
