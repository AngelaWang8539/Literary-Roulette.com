// Browser-side logic tests. Run by tests/test_fulltext.py (or:  node tests/js/run_tests.mjs <fixtures-dir>).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { cleanBookText, splitPassages, wordCount } from "../../static/js/chunker.js";
import * as roulette from "../../static/js/roulette.js";
import { createLibrary, memoryAdapter } from "../../static/js/storage.js";
import { readEbook, UploadError } from "../../static/js/ebook.js";

const dir = process.argv[2];
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log("  ok   " + name); }
  catch (e) { failed++; console.log("  FAIL " + name + "\n       " + (e.stack || e).split("\n").slice(0, 3).join("\n       ")); }
}
function seeded(seed) { let s = seed; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32); }
const file = (name, bytes, size) => ({ name, size: size ?? bytes.length, arrayBuffer: async () => (bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)) });
const fromDisk = (name) => file(name, new Uint8Array(readFileSync(`${dir}/${name}`)));
const rejects = async (p, code) => { try { await p; } catch (e) { assert.ok(e instanceof UploadError, String(e)); assert.equal(e.code, code, e.message); return e; } assert.fail("expected " + code); };

await test("JS cleaning and passage splitting match the Python output exactly", () => {
  const expected = JSON.parse(readFileSync(`${dir}/expected.json`, "utf8"));
  const { text } = cleanBookText(readFileSync(`${dir}/sample.txt`, "utf8"));
  assert.equal(text, expected.text);
  assert.deepEqual(splitPassages(text), expected.passages);
});

await test("roulette: book chosen before passage (1-passage book vs 30-passage book)", () => {
  const books = [{ id: "a", passages: ["x"] }, { id: "b", passages: Array.from({ length: 30 }, (_, i) => "p" + i) }];
  const rng = seeded(7); let a = 0; const n = 6000;
  for (let i = 0; i < n; i++) if (roulette.pickFirst(books, rng).bookId === "a") a++;
  assert.ok(Math.abs(a / n - 0.5) < 0.03, `share ${a / n}`);
});

await test("roulette: another passage avoids repeats, reports a one-passage book, spin avoids current book and restarts", () => {
  const rng = seeded(3), book = { id: "b", passages: ["0", "1", "2", "3"] };
  const seen = new Set([0]); let cur = 0;
  for (let i = 0; i < 3; i++) { cur = roulette.pickOtherPassage(book, cur, seen, rng); assert.ok(!seen.has(cur)); seen.add(cur); }
  assert.equal(roulette.pickOtherPassage({ id: "s", passages: ["only"] }, 0, new Set(), rng), null);
  const books = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.equal(roulette.pickOtherBook([{ id: "a" }], "a", new Set(), rng), null);
  const first = roulette.pickOtherBook(books, "a", new Set(["a"]), rng);
  assert.notEqual(first.bookId, "a"); assert.equal(first.restarted, false);
  const again = roulette.pickOtherBook(books, "a", new Set(["a", "b", "c"]), rng);
  assert.equal(again.restarted, true); assert.notEqual(again.bookId, "a");
});

await test("history: add, persist in the same store, search, filter, reveal, remove, clear", async () => {
  const db = memoryAdapter(); let t = 1000;
  const lib = createLibrary(db, { now: () => t++ });
  const e1 = await lib.addHistory({ source: "catalogue", bookKey: "bk1", passageKey: "p1", text: "The orchard hummed at dusk." });
  await lib.addHistory({ source: "catalogue", bookKey: "bk2", passageKey: "p2", text: "A census taker knocked." });
  const reopened = createLibrary(db);                                   // simulates a refresh: new library, same storage
  assert.equal((await reopened.listHistory()).length, 2);
  assert.equal((await reopened.listHistory())[0].passageKey, "p2");      // newest first
  assert.equal((await lib.listHistory({ query: "ORCHARD" })).length, 1);
  assert.equal((await lib.listHistory({ query: "Glass Orchard" })).length, 0);   // title not known yet, so not searchable
  await lib.markBookRevealed("catalogue", "bk1", { title: "Glass Orchard", author: "M. Tennant" });
  assert.equal((await lib.listHistory({ query: "glass orchard" })).length, 1);
  assert.equal((await lib.listHistory({ status: "revealed" })).length, 1);
  assert.equal((await lib.listHistory({ status: "hidden" })).length, 1);
  assert.equal((await lib.listHistory({ status: "hidden" }))[0].title, null);   // other book stays anonymous
  await lib.removeHistory(e1.id);
  assert.equal((await lib.listHistory()).length, 1);
  await lib.clearHistory();
  assert.equal((await lib.listHistory()).length, 0);
});


await test("history stores complete passage text but retains only the newest 80 entries", async () => {
  let clock = 1, id = 0;
  const lib = createLibrary(memoryAdapter(), { now: () => clock++, uid: () => "h" + (++id) });
  for (let i = 0; i < 85; i++) {
    await lib.addHistory({ source: "catalogue", bookKey: "b" + i, passageKey: "p" + i, text: "FULL PASSAGE " + i });
  }
  const entries = await lib.listHistory();
  assert.equal(entries.length, 80);
  assert.equal(entries[0].text, "FULL PASSAGE 84");
  assert.equal(entries.at(-1).text, "FULL PASSAGE 5");
  await lib.markBookRevealed("catalogue", "b84", { title: "Revealed title", author: "Author" });
  assert.equal((await lib.listHistory())[0].revealed, true);
  assert.equal((await lib.listHistory())[0].text, "FULL PASSAGE 84");
});

await test("saved books: save, list, check, remove", async () => {
  const lib = createLibrary(memoryAdapter());
  await lib.saveBook({ source: "catalogue", bookKey: "bk1", title: "T", author: "A", links: [] });
  assert.equal(await lib.isSaved("catalogue", "bk1"), true);
  assert.equal((await lib.listSaved()).length, 1);
  await lib.removeSaved(lib.savedId("catalogue", "bk1"));
  assert.equal(await lib.isSaved("catalogue", "bk1"), false);
});

await test("uploads: removing a book deletes its text, history and saved entry", async () => {
  const lib = createLibrary(memoryAdapter());
  const id = await lib.addUpload({ title: "Mine", author: "", fileName: "m.txt", size: 10, passages: ["a", "b"] });
  const keep = await lib.addUpload({ title: "Other", author: "", fileName: "o.txt", size: 10, passages: ["c"] });
  await lib.addHistory({ source: "upload", bookKey: id, passageKey: 0, text: "a" });
  await lib.addHistory({ source: "upload", bookKey: keep, passageKey: 0, text: "c" });
  await lib.saveBook({ source: "upload", bookKey: id, title: "Mine" });
  assert.equal((await lib.listUploads())[1].passageCount === 2 || (await lib.listUploads())[0].passageCount === 2, true);
  assert.equal("passages" in (await lib.listUploads())[0], false);       // listing never loads the full text
  await lib.removeUpload(id);
  assert.equal(await lib.getUpload(id), undefined);
  assert.deepEqual((await lib.listHistory()).map((e) => e.bookKey), [keep]);
  assert.equal(await lib.isSaved("upload", id), false);
  assert.equal((await lib.listUploads()).length, 1);
});

await test("upload .txt (Gutenberg-style): front matter and wrapper removed, title read", async () => {
  const book = await readEbook(fromDisk("sample.txt"));
  assert.equal(book.title, "Parity Tale"); assert.equal(book.author, "Ann Author");
  for (const junk of ["CONTENTS", "PREFACE", "Project Gutenberg", "THE END"]) assert.ok(!book.text.includes(junk), junk);
  assert.ok(splitPassages(book.text).length > 10);
});

await test("upload .epub: front matter skipped, headings dropped, entities decoded, metadata read", async () => {
  const book = await readEbook(fromDisk("good.epub"));
  assert.equal(book.title, "The Fixture & Friends"); assert.equal(book.author, "Una Uploader");
  for (const junk of ["COVER PAGE", "NAV LIST", "TITLE PAGE", "COPYRIGHT NOTICE", "Chapter 1"]) assert.ok(!book.text.includes(junk), junk);
  assert.ok(book.text.includes("Café & \u201cquoted\u201d."));
  assert.ok(wordCount(book.text) > 1000);
});

await test("upload .epub with only font obfuscation is accepted", async () => { assert.ok((await readEbook(fromDisk("fonts.epub"))).text.length > 1000); });

await test("upload errors are clear: DRM, damaged, wrong type, empty, too large, binary", async () => {
  assert.match((await rejects(readEbook(fromDisk("drm.epub")), "drm")).message, /DRM/);
  await rejects(readEbook(fromDisk("adept.epub")), "drm");
  await rejects(readEbook(fromDisk("truncated.epub")), "corrupted");
  await rejects(readEbook(fromDisk("random.epub")), "corrupted");
  await rejects(readEbook(file("book.pdf", new Uint8Array([1, 2, 3]))), "unsupported");
  await rejects(readEbook(file("empty.txt", new Uint8Array([]))), "empty");
  await rejects(readEbook(file("short.txt", new TextEncoder().encode("Only a few words here."))), "empty");
  await rejects(readEbook(file("huge.txt", new Uint8Array([65]), 50 * 1024 * 1024)), "too_large");
  await rejects(readEbook(fromDisk("binary.txt")), "corrupted");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
