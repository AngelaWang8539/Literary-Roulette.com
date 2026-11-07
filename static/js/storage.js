// Everything the reader keeps in THIS browser: history, saved books, uploaded books.
// It uses IndexedDB. Nothing here is sent to the server and nothing syncs between devices.
const STORES = ["history", "saved", "uploads"];

export function idbAdapter(factory = globalThis.indexedDB) {
  const opened = new Promise((resolve, reject) => {
    const req = factory.open("literary-roulette", 1);
    req.onupgradeneeded = () => STORES.forEach((s) => req.result.createObjectStore(s, { keyPath: "id" }));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("blocked"));
  });
  const run = async (store, mode, fn) => {
    const db = await opened;
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const request = fn(tx.objectStore(store));
      tx.oncomplete = () => resolve(request ? request.result : undefined);
      tx.onerror = tx.onabort = () => reject(tx.error);
    });
  };
  return {
    getAll: (s) => run(s, "readonly", (o) => o.getAll()),
    get: (s, id) => run(s, "readonly", (o) => o.get(id)),
    put: (s, obj) => run(s, "readwrite", (o) => o.put(obj)),
    remove: (s, id) => run(s, "readwrite", (o) => o.delete(id)),
    clear: (s) => run(s, "readwrite", (o) => o.clear()),
  };
}

// Same interface, kept in memory. Used when the browser blocks IndexedDB, and in the tests.
export function memoryAdapter() {
  const data = Object.fromEntries(STORES.map((s) => [s, new Map()]));
  const copy = (v) => (v === undefined ? v : structuredClone(v));
  return {
    getAll: async (s) => [...data[s].values()].map(copy),
    get: async (s, id) => copy(data[s].get(id)),
    put: async (s, obj) => { data[s].set(obj.id, copy(obj)); },
    remove: async (s, id) => { data[s].delete(id); },
    clear: async (s) => { data[s].clear(); },
  };
}

let counter = 0;
const defaultId = () => (globalThis.crypto?.randomUUID ? crypto.randomUUID() : `${Date.now()}-${++counter}-${Math.random().toString(36).slice(2)}`);

export function createLibrary(db, { now = () => Date.now(), uid = defaultId } = {}) {
  const lib = {
    // ---- reading history ----
    async addHistory({ source, bookKey, passageKey, text }) {
      const entry = { id: uid(), ts: now(), source, bookKey, passageKey, text, revealed: false, title: null, author: null };
      await db.put("history", entry);
      return entry;
    },
    async listHistory({ query = "", status = "all" } = {}) {
      const q = query.trim().toLowerCase();
      const all = (await db.getAll("history")).sort((a, b) => b.ts - a.ts || (a.id < b.id ? 1 : -1));
      return all.filter((e) => {
        if (status === "revealed" && !e.revealed) return false;
        if (status === "hidden" && e.revealed) return false;
        if (!q) return true;
        // A hidden book's title is never searchable, so searching cannot reveal it.
        const hay = e.text.toLowerCase() + (e.revealed ? ` ${e.title} ${e.author}`.toLowerCase() : "");
        return hay.includes(q);
      });
    },
    async markBookRevealed(source, bookKey, { title, author }) {
      for (const e of await db.getAll("history")) {
        if (e.source === source && e.bookKey === bookKey && !e.revealed) await db.put("history", { ...e, revealed: true, title, author });
      }
    },
    removeHistory: (id) => db.remove("history", id),
    clearHistory: () => db.clear("history"),

    // ---- saved books ----
    savedId: (source, bookKey) => `${source}:${bookKey}`,
    async saveBook(book) {
      const entry = { ...book, id: lib.savedId(book.source, book.bookKey), savedAt: now() };
      await db.put("saved", entry);
      return entry;
    },
    async listSaved() { return (await db.getAll("saved")).sort((a, b) => b.savedAt - a.savedAt); },
    async isSaved(source, bookKey) { return !!(await db.get("saved", lib.savedId(source, bookKey))); },
    removeSaved: (id) => db.remove("saved", id),

    // ---- uploaded books (the full text lives only here) ----
    async addUpload({ title, author, fileName, size, passages }) {
      const rec = { id: uid(), title, author, fileName, size, addedAt: now(), passages };
      await db.put("uploads", rec);
      return rec.id;
    },
    async listUploads() {
      return (await db.getAll("uploads")).sort((a, b) => b.addedAt - a.addedAt)
        .map(({ passages, ...meta }) => ({ ...meta, passageCount: passages.length }));
    },
    getUpload: (id) => db.get("uploads", id),
    getAllUploads: async () => db.getAll("uploads"),
    // Removes the book, its text, and every history/saved entry that came from it.
    async removeUpload(id) {
      await db.remove("uploads", id);
      for (const e of await db.getAll("history")) if (e.source === "upload" && e.bookKey === id) await db.remove("history", e.id);
      await db.remove("saved", lib.savedId("upload", id));
    },
  };
  return lib;
}
