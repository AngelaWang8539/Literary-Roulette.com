// Local storage tests (needs `npm install` inside tests/js for fake-indexeddb; skipped otherwise).
import assert from "node:assert/strict";
let fake;
try { fake = await import("fake-indexeddb"); } catch { console.log("SKIP: fake-indexeddb is not installed (cd tests/js && npm install)"); process.exit(0); }
const { openDatabase, idbAdapter, createLibrary, HISTORY_LIMIT } = await import("../../static/js/storage.js");
let passed = 0, failed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("  ok   " + name); } catch (e) { failed++; console.log("  FAIL " + name + "\n       " + (e.stack || e)); } }
const fresh = () => new fake.IDBFactory();

function seed(factory, version, stores, rows = {}) {
  return new Promise((resolve, reject) => {
    const req = factory.open("literary-roulette", version);
    req.onupgradeneeded = () => stores.forEach((s) => req.result.createObjectStore(s, { keyPath: "id" }));
    req.onsuccess = () => {
      const db = req.result, names = Object.keys(rows);
      if (!names.length) { db.close(); return resolve(); }
      const tx = db.transaction(names, "readwrite");
      for (const n of names) for (const r of rows[n]) tx.objectStore(n).put(r);
      tx.oncomplete = () => { db.close(); resolve(); };
    };
    req.onerror = () => reject(req.error);
  });
}

await test("a brand-new browser gets every store", async () => {
  const db = await openDatabase(fresh()); assert.deepEqual([...db.objectStoreNames].sort(), ["history", "saved", "uploads"]); db.close();
});
await test("an older database missing a store is repaired in place and keeps its data (the banner bug)", async () => {
  const f = fresh(); await seed(f, 1, ["history", "saved"], { history: [{ id: "h1", text: "kept" }] });
  const a = idbAdapter(f);
  assert.equal((await a.getAll("history"))[0].text, "kept");
  await a.put("uploads", { id: "u1" }); assert.equal((await a.getAll("uploads")).length, 1);
});
await test("a database that is already newer than we ask for is opened as it is", async () => {
  const f = fresh(); await seed(f, 7, ["history", "saved", "uploads"], { saved: [{ id: "s1" }] });
  const a = idbAdapter(f); assert.equal((await a.getAll("saved")).length, 1);
});
await test("a newer database that lacks a store is repaired too", async () => {
  const f = fresh(); await seed(f, 9, ["history"]);
  const a = idbAdapter(f); await a.put("saved", { id: "x" }); assert.equal((await a.getAll("saved")).length, 1);
});
await test("history is capped at 70 and keeps the newest", async () => {
  assert.equal(HISTORY_LIMIT, 70);
  let clock = 1000, n = 0;
  const lib = createLibrary(idbAdapter(fresh()), { now: () => ++clock, uid: () => "id" + (++n) });
  for (let i = 0; i < 90; i++) await lib.addHistory({ source: "catalogue", bookKey: "b", passageKey: "p" + i, text: "t" + i });
  const rows = await lib.listHistory(); assert.equal(rows.length, 70);
  assert.ok(rows.some((r) => r.passageKey === "p89")); assert.ok(!rows.some((r) => r.passageKey === "p0"));
});

await test("a recorded language and the recent books: distinct, oldest first, only this language", async () => {
  let clock = 1000, n = 0;
  const lib = createLibrary(idbAdapter(fresh()), { now: () => ++clock, uid: () => "id" + (++n) });
  await lib.addHistory({ source: "catalogue", bookKey: "A", passageKey: "p1", text: "t", language: "en" });
  await lib.addHistory({ source: "catalogue", bookKey: "B", passageKey: "p2", text: "t", language: "fr" });
  await lib.addHistory({ source: "catalogue", bookKey: "A", passageKey: "p3", text: "t", language: "en" });   // same book again
  await lib.addHistory({ source: "catalogue", bookKey: "C", passageKey: "p4", text: "t" });                    // saved before languages were recorded
  await lib.addHistory({ source: "upload", bookKey: "U", passageKey: "p5", text: "t", language: "en" });
  assert.deepEqual(await lib.recentBooks({ source: "catalogue", language: "en" }), ["A", "C"]);   // B is French; A counted once, newest position
  assert.deepEqual(await lib.recentBooks({ source: "catalogue", language: "fr" }), ["B", "C"]);
});
await test("the recent books never exceed the history that is kept (70)", async () => {
  let clock = 1000, n = 0;
  const lib = createLibrary(idbAdapter(fresh()), { now: () => ++clock, uid: () => "id" + (++n) });
  for (let i = 0; i < 90; i++) await lib.addHistory({ source: "catalogue", bookKey: "b" + i, passageKey: "p" + i, text: "t", language: "en" });
  const keys = await lib.recentBooks({ source: "catalogue", language: "en" });
  assert.equal(keys.length, 70); assert.equal(keys[69], "b89"); assert.equal(keys[0], "b20");
});
await test("export then restore brings history and saved books back, and nothing else", async () => {
  let clock = 1000, n = 0;
  const a = createLibrary(idbAdapter(fresh()), { now: () => ++clock, uid: () => "a" + (++n) });
  await a.addHistory({ source: "catalogue", bookKey: "A", passageKey: "p1", text: "hello", language: "en" });
  await a.addHistory({ source: "upload", bookKey: "U", passageKey: "p2", text: "private upload text" });
  await a.markBookRevealed("catalogue", "A", { title: "T", author: "X" });
  await a.saveBook({ source: "catalogue", bookKey: "A", title: "T", author: "X", genres: ["g"], description: "d", links: [] });
  const file = JSON.parse(JSON.stringify(await a.exportData()));
  assert.equal(file.history.length, 1); assert.ok(!JSON.stringify(file).includes("private upload"));   // uploads never leave the device
  const b = createLibrary(idbAdapter(fresh()), { now: () => ++clock, uid: () => "b" + (++n) });
  assert.deepEqual(await b.importData(file), { history: 1, saved: 1 });
  const rows = await b.listHistory(); assert.equal(rows[0].text, "hello"); assert.equal(rows[0].title, "T");
  assert.ok(await b.isSaved("catalogue", "A"));
});
await test("a restore file that is not ours, or has bad entries, is refused or skipped safely", async () => {
  const lib = createLibrary(idbAdapter(fresh()));
  await assert.rejects(lib.importData({ app: "something-else", history: [] }));
  await assert.rejects(lib.importData(null));
  const done = await lib.importData({ app: "literary-roulette", history: [{ source: "catalogue", bookKey: "x".repeat(200), passageKey: "p", text: "t", ts: 1 },
    { source: "catalogue", bookKey: "ok", passageKey: "p", text: "t", ts: 5 }, "junk", { source: "upload", bookKey: "u", passageKey: "p", text: "t", ts: 1 }], saved: [{ source: "catalogue" }] });
  assert.deepEqual(done, { history: 1, saved: 0 });
});
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
