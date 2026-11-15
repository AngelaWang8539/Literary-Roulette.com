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
await test("history is capped at 75 and keeps the newest", async () => {
  assert.equal(HISTORY_LIMIT, 75);
  let clock = 1000, n = 0;
  const lib = createLibrary(idbAdapter(fresh()), { now: () => ++clock, uid: () => "id" + (++n) });
  for (let i = 0; i < 90; i++) await lib.addHistory({ source: "catalogue", bookKey: "b", passageKey: "p" + i, text: "t" + i });
  const rows = await lib.listHistory(); assert.equal(rows.length, 75);
  assert.ok(rows.some((r) => r.passageKey === "p89")); assert.ok(!rows.some((r) => r.passageKey === "p0"));
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
