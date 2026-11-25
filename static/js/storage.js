// Everything the reader keeps in THIS browser: history, saved books, uploaded books.
// It uses IndexedDB. Nothing here is sent to the server and nothing syncs between devices.
const STORES = ["history", "saved", "uploads"];
export const HISTORY_LIMIT = 70;   // history never piles up: the 70 most recent passages are kept (and the books among them are not drawn again)

const DB_NAME = "literary-roulette", SCHEMA_VERSION = 2;

// Open the database and make sure EVERY store exists. An older copy of this origin's database may be missing a store
// (for example "uploads" was added later without a version bump); that used to make every read fail and the page
// wrongly claim the browser was blocking storage. Missing stores are now added in place and the data is kept.
export function openDatabase(factory = globalThis.indexedDB, blockedMs = 4000) {
  return new Promise((resolve, reject) => {
    const attempt = (version) => {
      let blockedTimer = null;
      const req = version ? factory.open(DB_NAME, version) : factory.open(DB_NAME);
      req.onupgradeneeded = () => {
        for (const name of STORES) if (!req.result.objectStoreNames.contains(name)) req.result.createObjectStore(name, { keyPath: "id" });
      };
      req.onsuccess = () => {
        clearTimeout(blockedTimer);
        const db = req.result;
        db.onversionchange = () => db.close();                  // never block another tab's upgrade
        if (STORES.every((name) => db.objectStoreNames.contains(name))) return resolve(db);
        const next = db.version + 1; db.close(); attempt(next);   // a store is missing: add it, keep what is there
      };
      req.onerror = () => {
        clearTimeout(blockedTimer);
        if (version && req.error && req.error.name === "VersionError") { req.preventDefault?.(); return attempt(undefined); }   // the database is newer than we asked for: open it as it is
        reject(req.error || new Error("IndexedDB error"));
      };
      req.onblocked = () => { blockedTimer = setTimeout(() => reject(new Error("Another Literary Roulette tab is holding the old database open. Close it and reload.")), blockedMs); };
    };
    attempt(SCHEMA_VERSION);
  });
}

export function idbAdapter(factory = globalThis.indexedDB) {
  const opened = openDatabase(factory);
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
  const trimHistory = async () => {
    const all = (await db.getAll("history")).sort((a, b) => b.ts - a.ts || (a.id < b.id ? 1 : -1));
    await Promise.all(all.slice(HISTORY_LIMIT).map((entry) => db.remove("history", entry.id)));
  };
  const lib = {
    // ---- reading history ----
    async addHistory({ source, bookKey, passageKey, text, language = null }) {
      const entry = { id: uid(), ts: now(), source, bookKey, passageKey, text, language, revealed: false, title: null, author: null };
      await db.put("history", entry);
      await trimHistory();
      return entry;
    },
    async listHistory({ query = "", status = "all" } = {}) {
      await trimHistory(); // also removes overflow left by older app versions
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
    /** Distinct book keys of the recent history, OLDEST first (the order the pouch keeps them in), for one source and language.
     *  Entries saved before the language was recorded still count: a key that belongs to another language never matches anyway. */
    async recentBooks({ source = "catalogue", language = null, limit = HISTORY_LIMIT } = {}) {
      const all = (await db.getAll("history")).sort((a, b) => b.ts - a.ts || (a.id < b.id ? 1 : -1));
      const keys = [];
      for (const e of all) {
        if (e.source !== source || !e.bookKey || (language && e.language && e.language !== language)) continue;
        if (!keys.includes(e.bookKey)) keys.push(e.bookKey);
        if (keys.length >= limit) break;
      }
      return keys.reverse();
    },
    async markBookRevealed(source, bookKey, { title, author }) {
      for (const e of await db.getAll("history")) {
        if (e.source === source && e.bookKey === bookKey && !e.revealed) await db.put("history", { ...e, revealed: true, title, author });
      }
    },
    async wasRevealed(source, bookKey) {     // only the recent history is kept, so this means "recently", not "ever"
      return (await db.getAll("history")).some((e) => e.source === source && e.bookKey === bookKey && e.revealed);
    },
    removeHistory: (id) => db.remove("history", id),
    clearHistory: () => db.clear("history"),

    // ---- export / restore: a file the reader keeps, for when this browser's storage is cleared. Not a sync. ----
    // Catalogue history and saved books only: uploaded books stay on the device they were added on.
    async exportData() {
      const history = (await db.getAll("history")).filter((e) => e.source === "catalogue");
      const saved = (await db.getAll("saved")).filter((e) => e.source === "catalogue");
      return { app: "literary-roulette", version: 1, exportedAt: now(), history, saved };
    },
    async importData(data) {
      if (!data || data.app !== "literary-roulette" || !Array.isArray(data.history) || !Array.isArray(data.saved || [])) throw new Error("This is not a Literary Roulette history file.");
      const str = (v, max) => typeof v === "string" && v.length <= max;
      let history = 0, saved = 0;
      for (const e of data.history.slice(0, HISTORY_LIMIT)) {
        if (!e || e.source !== "catalogue" || !str(e.bookKey, 60) || !str(e.passageKey, 60) || !str(e.text, 6000) || !Number.isFinite(e.ts)) continue;
        await db.put("history", { id: str(e.id, 80) ? e.id : uid(), ts: e.ts, source: "catalogue", bookKey: e.bookKey, passageKey: e.passageKey, text: e.text,
          language: str(e.language, 8) ? e.language : null, revealed: e.revealed === true, title: str(e.title, 300) ? e.title : null, author: str(e.author, 300) ? e.author : null });
        history++;
      }
      for (const b of (data.saved || []).slice(0, 500)) {
        if (!b || b.source !== "catalogue" || !str(b.bookKey, 60) || !str(b.title, 300) || !str(b.author, 300)) continue;
        await db.put("saved", { source: "catalogue", bookKey: b.bookKey, title: b.title, author: b.author, genres: Array.isArray(b.genres) ? b.genres.filter((g) => str(g, 80)).slice(0, 8) : [],
          description: str(b.description, 2000) ? b.description : "", links: Array.isArray(b.links) ? b.links.slice(0, 6) : [], credit: b.credit || null, isUpload: false, isDemo: b.isDemo === true,
          contentNote: str(b.contentNote, 300) ? b.contentNote : null, id: lib.savedId("catalogue", b.bookKey), savedAt: Number.isFinite(b.savedAt) ? b.savedAt : now() });
        saved++;
      }
      await trimHistory();
      return { history, saved };
    },

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
