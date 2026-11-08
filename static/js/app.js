// Page logic. Public-catalogue passages come from the server; history, saved books and
// uploaded books live only in this browser (see storage.js).
import { createLibrary, idbAdapter, memoryAdapter } from "./storage.js";
import { readEbook, UploadError } from "./ebook.js";
import { splitPassages } from "./chunker.js";
import * as roulette from "./roulette.js";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

const ui = {
  text: $("passage-text"), note: $("passage-note"), start: $("start"), status: $("status"),
  another: $("btn-another"), reveal: $("btn-reveal"), spin: $("btn-spin"), hide: $("btn-hide"), save: $("btn-save"),
  revealLabel: $("reveal-label"), revealSub: $("reveal-sub"), details: $("book-details"), cover: $("cover"),
  demo: $("demo-notice"), title: $("book-title"), author: $("book-author"), popularity: $("book-popularity"), genres: $("book-genres"), desc: $("book-description"),
  pub: $("book-publication"), source: $("book-source"), links: $("book-links"), noLinks: $("no-links"),
  credit: $("credit"), contentNote: $("content-note"), awareness: $("awareness"), banner: $("mode-banner"),
};
const buttons = { start: ui.start, another: ui.another, reveal: ui.reveal, spin: ui.spin };
const counts = { passages: 0, books: 0, links: 0 };
const busy = new Set();
let latest = 0;

// What is on screen right now.
const S = { source: "catalogue", token: null, bookKey: null, passageKey: null, book: null, detailsOpen: false,
  up: { books: [], seenP: new Map(), seenB: new Set(), cur: null, revealed: null } };

// ---------- local library (IndexedDB, or memory if the browser blocks it) ----------
let lib = null;
const ready = (async () => {
  try {
    if (!globalThis.indexedDB) throw new Error("IndexedDB unavailable");
    const adapter = idbAdapter();
    await adapter.getAll("history");
    lib = createLibrary(adapter);
  } catch {
    lib = createLibrary(memoryAdapter());
    $("storage-warning").hidden = false;
  }
})();

// ---------- small helpers ----------
function setStatus(message, kind) {
  ui.status.textContent = (kind === "error" ? "Problem: " : "") + message;
  ui.status.className = "status" + (kind ? " " + kind : "");
}
function updateCounts() { $("count-passages").textContent = counts.passages; $("count-books").textContent = counts.books; $("count-links").textContent = counts.links; }
function setPanelEnabled(on) { [ui.another, ui.reveal, ui.spin].forEach((b) => { b.disabled = !on; }); }
function lock(name, on) { buttons[name].setAttribute("aria-disabled", on ? "true" : "false"); buttons[name].classList.toggle("is-busy", on); }
async function api(path, body) {
  try {
    const res = await fetch(path, { method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
    return await res.json();
  } catch { return { ok: false, code: "network", message: "Could not reach the site. Check your connection and try again." }; }
}

// Run one server action. Only that action is locked; the other buttons stay usable.
async function act(name, path, body, onOk) {
  if (busy.has(name)) return;
  busy.add(name); lock(name, true);
  const mine = ++latest;
  setStatus("Loading\u2026");
  const data = await api(path, body);
  busy.delete(name); lock(name, false);
  if (mine !== latest) return;
  if (!data.ok) {
    const soft = data.code === "no_other_passage" || data.code === "only_one_book";
    setStatus(data.message || "Something went wrong. Please try again.", soft ? "note" : "error");
    if (data.code === "empty_catalogue") { ui.text.textContent = data.message; setPanelEnabled(false); ui.start.hidden = false; }
    if (data.code === "unknown_passage") ui.start.hidden = false; // keep the text readable, offer a fresh spin
    return;
  }
  onOk(data);
}

// ---------- showing a passage ----------
const fromApi = (d) => ({ source: "catalogue", token: d.token, bookKey: d.book_token, passageKey: d.token, text: d.text,
  notes: [d.content_warning && "Content note: " + d.content_warning, d.spoiler_level && d.spoiler_level !== "none" && "Spoiler level: " + d.spoiler_level].filter(Boolean), book: d.book || null });

function showPassage(p, { record = true } = {}) {
  Object.assign(S, { source: p.source, token: p.token || null, bookKey: p.bookKey, passageKey: p.passageKey });
  ui.start.hidden = true;
  ui.text.textContent = p.text;
  if (!reduceMotion()) { ui.text.classList.remove("swap"); void ui.text.offsetWidth; ui.text.classList.add("swap"); }
  ui.note.textContent = (p.notes || []).join(". "); ui.note.hidden = !(p.notes && p.notes.length);
  ui.banner.hidden = p.source !== "upload";
  const radio = document.querySelector(`input[name=source][value=${p.source}]`); if (radio) radio.checked = true;
  setPanelEnabled(true);
  if (p.book) openDetails(p.book); else closeDetails(false);
  if (record) {
    counts.passages++; updateCounts();
    ready.then(async () => {
      await lib.addHistory({ source: p.source, bookKey: p.bookKey, passageKey: p.passageKey, text: p.text });
      if (p.book) await lib.markBookRevealed(p.source, p.bookKey, p.book);
    }).catch(() => {});
  }
}

// ---------- book details ----------
function renderBook(b) {
  ui.title.textContent = b.title; ui.author.textContent = "by " + b.author; ui.desc.textContent = b.description || "";
  ui.demo.hidden = !b.is_demo;
  ui.popularity.replaceChildren();
  ui.popularity.className = "popularity-badge";
  if (b.popularity && b.popularity.label) {
    const tier = ["jackpot", "lucky", "discovery", "hidden-gem"].includes(b.popularity.tier)
      ? b.popularity.tier : "discovery";
    ui.popularity.classList.add(`tier-${tier}`);
    ui.popularity.append(el("span", "popularity-label", b.popularity.label));
    if (b.popularity.detail) ui.popularity.append(el("small", "popularity-detail", b.popularity.detail));
    ui.popularity.hidden = false;
    ui.popularity.title = "Based on Project Gutenberg's approximate downloads in the previous 30 days; this is popularity, not literary quality.";
    ui.popularity.setAttribute("aria-label", `${b.popularity.label}. ${b.popularity.detail || ""}`.trim());
  } else {
    ui.popularity.hidden = true;
    ui.popularity.removeAttribute("title");
    ui.popularity.removeAttribute("aria-label");
  }
  ui.genres.replaceChildren(...(b.genres || []).map((g) => el("li", "", g)));
  ui.pub.textContent = b.publication || ""; ui.pub.hidden = !b.publication;
  const src = [b.source?.edition, b.source?.location].filter(Boolean).join(", ");
  ui.source.textContent = src ? "This passage: " + src + "." : ""; ui.source.hidden = !src;
  ui.cover.replaceChildren();
  if (b.cover_url) { const img = el("img"); img.src = b.cover_url; img.alt = ""; ui.cover.append(img); }
  else ui.cover.append(el("span", "monogram", b.title.replace(/^(the|a|an)\s+/i, "").charAt(0)));
  ui.links.replaceChildren(...(b.links || []).map((l) => {
    const li = el("li"), a = el("a", "btn link"); a.href = l.href; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.append(el("span", "", l.label), el("span", "sub", "Opens in a new tab")); li.append(a); return li;
  }));
  ui.noLinks.hidden = (b.links || []).length > 0;
  ui.credit.replaceChildren();
  if (b.source_credit) {
    const a = el("a", "", b.source_credit.label); a.href = b.source_credit.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    ui.credit.append(a, " \u2013 " + b.source_credit.note);
  }
  ui.credit.hidden = !b.source_credit;
  ui.contentNote.textContent = b.content_note ? "Content note: " + b.content_note : ""; ui.contentNote.hidden = !b.content_note;
}
async function refreshSaveButton() {
  await ready;
  const saved = S.book && (await lib.isSaved(S.source, S.bookKey));
  ui.save.textContent = saved ? "Saved. Remove from saved" : "Save this book";
  ui.save.setAttribute("aria-pressed", saved ? "true" : "false");
}
function openDetails(book) {
  S.book = book; renderBook(book);
  ui.details.hidden = false; S.detailsOpen = true;
  ui.reveal.setAttribute("aria-expanded", "true");
  ui.revealLabel.textContent = "Hide book details"; ui.revealSub.textContent = "Keep the passage, put the title away";
  refreshSaveButton();
}
function closeDetails(tellServer) {
  ui.details.hidden = true; S.detailsOpen = false; ui.awareness.hidden = true;
  ui.reveal.setAttribute("aria-expanded", "false");
  ui.revealLabel.textContent = "Reveal this book"; ui.revealSub.textContent = "See the title, author and where to read it";
  if (tellServer && S.source === "catalogue" && S.token) api("/api/event", { token: S.token, book_token: S.bookKey, event: "book_hidden" });
}
function afterReveal(book) {
  counts.books++; updateCounts(); openDetails(book);
  ready.then(() => lib.markBookRevealed(S.source, S.bookKey, book)).catch(() => {});
  ui.awareness.hidden = S.source !== "catalogue";
  setStatus("Book revealed. Your passage has not changed.");
  ui.details.focus({ preventScroll: true });
  ui.details.scrollIntoView({ block: "nearest", behavior: reduceMotion() ? "auto" : "smooth" });
}

// ---------- uploaded books: the three actions, run locally ----------
const upBook = (id) => S.up.books.find((b) => b.id === id);
const seenSet = (id) => { if (!S.up.seenP.has(id)) S.up.seenP.set(id, new Set()); return S.up.seenP.get(id); };
async function loadUploads() { await ready; S.up.books = await lib.getAllUploads(); }
function upPayload(rec) {
  return { title: rec.title, author: rec.author || "Unknown author", genres: [], is_upload: true, links: [], publication: null,
    description: `Uploaded from your device (${rec.fileName}). Stored only in this browser.`, source: {} };
}
function showUp(bookId, index, extra = {}) {
  const rec = upBook(bookId);
  S.up.cur = { bookId, index }; seenSet(bookId).add(index);
  showPassage({ source: "upload", token: null, bookKey: bookId, passageKey: index, text: rec.passages[index], book: S.up.revealed === bookId ? upPayload(rec) : null, ...extra });
}
async function startUploads() {
  await loadUploads();
  if (!S.up.books.length) { setStatus("You have not uploaded any books yet.", "error"); return false; }
  S.up.seenP = new Map(); S.up.seenB = new Set(); S.up.revealed = null;
  const { bookId, index } = roulette.pickFirst(S.up.books);
  S.up.seenB.add(bookId); showUp(bookId, index);
  setStatus("Passage loaded from your uploaded books."); return true;
}
function anotherUp() {
  const { bookId, index } = S.up.cur, idx = roulette.pickOtherPassage(upBook(bookId), index, seenSet(bookId));
  if (idx === null) return setStatus("This book has only one passage, so there is no other to show. You can still reveal it or spin another book.", "note");
  showUp(bookId, idx); setStatus("New passage from the same book.");
}
function spinUp() {
  const pick = roulette.pickOtherBook(S.up.books, S.up.cur.bookId, S.up.seenB);
  if (!pick) return setStatus("You have only uploaded one book, so there is nothing else to spin to.", "note");
  if (pick.restarted) { S.up.seenB = new Set(); S.up.seenP = new Map(); }
  S.up.seenB.add(pick.bookId); S.up.revealed = null;
  showUp(pick.bookId, roulette.pickInBook(upBook(pick.bookId), seenSet(pick.bookId)));
  setStatus(pick.restarted ? "You have now met every uploaded book once. Starting a fresh round." : "A different book, hidden again. Read on.");
}

// ---------- the three actions ----------
const startCatalogue = () => act("start", "/api/passage/start", undefined, (d) => { showPassage(fromApi(d)); setStatus("Your passage is ready. Choose any option."); });
function onAnother() {
  if (!S.bookKey) return;
  if (S.source === "upload") return anotherUp();
  act("another", "/api/passage/another", { token: S.token, book_token: S.bookKey }, (d) => { showPassage(fromApi(d)); setStatus("New passage from the same book." + (S.detailsOpen ? " Book details are still showing." : "")); });
}
function onSpin() {
  if (!S.bookKey) return;
  if (S.source === "upload") return spinUp();
  act("spin", "/api/book/spin", { token: S.token, book_token: S.bookKey }, (d) => { showPassage(fromApi(d)); setStatus(d.notice || "A different book, hidden again. Read on."); });
}
async function doReveal() {
  if (!S.bookKey) return;
  if (S.source === "upload") { S.up.revealed = S.up.cur.bookId; return afterReveal(upPayload(upBook(S.up.cur.bookId))); }
  return act("reveal", "/api/book/reveal", { book_token: S.bookKey }, (d) => afterReveal(d.book));
}
function onReveal() {
  if (!S.bookKey) return;
  if (S.detailsOpen) { closeDetails(true); if (S.source === "upload") S.up.revealed = null; setStatus("Book details hidden. Your passage has not changed."); return; }
  doReveal();
}
ui.start.addEventListener("click", startCatalogue);
ui.another.addEventListener("click", onAnother);
ui.spin.addEventListener("click", onSpin);
ui.reveal.addEventListener("click", onReveal);
ui.hide.addEventListener("click", () => { onReveal(); ui.reveal.focus(); });
ui.links.addEventListener("click", (e) => { if (e.target.closest("a")) { counts.links++; updateCounts(); } });
ui.awareness.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-answer]"); if (!b || !S.token) return;
  api("/api/event", { token: S.token, book_token: S.bookKey, event: "awareness_" + b.dataset.answer });
  ui.awareness.hidden = true; setStatus("Thanks for answering.");
});
ui.save.addEventListener("click", async () => {
  if (!S.book) return;
  await ready;
  const id = lib.savedId(S.source, S.bookKey);
  if (await lib.isSaved(S.source, S.bookKey)) { await lib.removeSaved(id); setStatus("Removed from My Saved Books."); }
  else {
    const b = S.book;
    await lib.saveBook({ source: S.source, bookKey: S.bookKey, title: b.title, author: b.author, genres: b.genres || [], description: b.description || "",
      links: b.links || [], credit: b.source_credit || null, isUpload: !!b.is_upload, isDemo: !!b.is_demo, contentNote: b.content_note || null });
    if (S.source === "catalogue" && S.token) api("/api/event", { token: S.token, book_token: S.bookKey, event: "book_saved" });
    setStatus("Saved to My Saved Books on this device.");
  }
  refreshSaveButton();
});

// ---------- My History ----------
const hist = { q: $("h-query"), f: $("h-filter"), list: $("h-list"), empty: $("h-empty"), count: $("h-count"), cache: new Map() };
const when = (ts) => new Date(ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
async function renderHistory() {
  await ready;
  const entries = await lib.listHistory({ query: hist.q.value, status: hist.f.value });
  hist.cache = new Map(entries.map((e) => [e.id, e]));
  hist.list.replaceChildren(...entries.map((e) => {
    const li = el("li", "card"); li.dataset.id = e.id;
    const preview = e.text.length > 200 ? e.text.slice(0, 200).replace(/\s+\S*$/, "") + "\u2026" : e.text;
    li.append(el("p", "preview", preview));
    const meta = el("p", "meta"); meta.append(el("span", "", when(e.ts) + ". "),
      el("strong", "", e.revealed ? `Book revealed: ${e.title}${e.author ? " by " + e.author : ""}` : "Book not revealed"));
    if (e.source === "upload") meta.append(el("span", "", " (your upload)"));
    const row = el("div", "row");
    for (const [act_, label] of [["open", "Open passage"], ["reveal", e.revealed ? "Show book details" : "Reveal this book"], ["remove", "Remove"]]) {
      const b = el("button", "btn quiet", label); b.type = "button"; b.dataset.act = act_; row.append(b);
    }
    li.append(meta, row); return li;
  }));
  hist.empty.hidden = entries.length > 0 || !!hist.q.value || hist.f.value !== "all";
  hist.count.textContent = entries.length ? `${entries.length} passage${entries.length === 1 ? "" : "s"} shown.` : (hist.empty.hidden ? "No passages match." : "");
}
async function openEntry(entry, reveal) {
  if (entry.source === "upload") {
    await loadUploads();
    const rec = upBook(entry.bookKey);
    if (!rec) return setStatus("That uploaded book has been removed.", "error");
    S.up.cur = { bookId: rec.id, index: Number(entry.passageKey) };
    S.up.revealed = null; S.up.seenB.add(rec.id);
    seenSet(rec.id).add(S.up.cur.index);
    showPassage({ source: "upload", token: null, bookKey: rec.id, passageKey: entry.passageKey, text: entry.text, book: null }, { record: false });
  } else {
    showPassage({ source: "catalogue", token: entry.passageKey, bookKey: entry.bookKey, passageKey: entry.passageKey, text: entry.text, book: null }, { record: false });
  }
  location.hash = ""; route();
  setStatus("Passage reopened from your history. Its book is still hidden unless you reveal it.");
  if (reveal) await doReveal();
}
hist.list.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const entry = hist.cache.get(b.closest("li").dataset.id); if (!entry) return;
  if (b.dataset.act === "remove") { await lib.removeHistory(entry.id); renderHistory(); }
  else openEntry(entry, b.dataset.act === "reveal");
});
hist.q.addEventListener("input", renderHistory);
hist.f.addEventListener("change", renderHistory);
$("h-clear").addEventListener("click", async () => {
  await ready;
  if (confirm("Clear your whole reading history on this device? This cannot be undone.")) { await lib.clearHistory(); renderHistory(); }
});

// ---------- My Saved Books ----------
async function renderSaved() {
  await ready;
  const items = await lib.listSaved();
  $("s-list").replaceChildren(...items.map((b) => {
    const li = el("li", "card"); li.dataset.id = b.id;
    li.append(el("h3", "", b.title), el("p", "author", "by " + b.author));
    if (b.genres?.length) li.append(el("ul", "chips", "")), li.lastChild.append(...b.genres.map((g) => el("li", "", g)));
    li.append(el("p", "description", b.description));
    if (b.contentNote) li.append(el("p", "meta", "Content note: " + b.contentNote));
    if (b.isUpload) li.append(el("p", "meta", "Your upload. Stored only in this browser."));
    if (b.links?.length) {
      const ul = el("ul", "links");
      for (const l of b.links) {
        const x = el("li"), a = el("a", "btn link");
        // Older saved catalogue links point at a passage token that may be gone; use the stable book token instead.
        a.href = b.source === "catalogue" && b.bookKey && l.kind ? `/go/${l.kind}/${b.bookKey}` : l.href;
        a.target = "_blank"; a.rel = "noopener noreferrer";
        a.append(el("span", "", l.label), el("span", "sub", "Opens in a new tab"));
        x.append(a); ul.append(x);
      }
      li.append(ul);
    }
    const rm = el("button", "btn quiet", "Remove from saved"); rm.type = "button"; rm.dataset.act = "remove"; li.append(rm);
    return li;
  }));
  $("s-empty").hidden = items.length > 0;
}
$("s-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act=remove]"); if (!b) return;
  await lib.removeSaved(b.closest("li").dataset.id); renderSaved(); refreshSaveButton();
});

// ---------- Upload my book ----------
const upStatus = (m, kind) => { $("u-status").textContent = (kind === "error" ? "Problem: " : "") + m; $("u-status").className = "status" + (kind ? " " + kind : ""); };
async function renderUploads() {
  await ready;
  const items = await lib.listUploads();
  $("u-list").replaceChildren(...items.map((u) => {
    const li = el("li", "card"); li.dataset.id = u.id;
    li.append(el("h3", "", u.title), el("p", "meta", `${u.author ? "by " + u.author + ". " : ""}File: ${u.fileName}. About ${u.passageCount} passages. ${Math.max(1, Math.round(u.size / 1024))} KB.`));
    const rm = el("button", "btn quiet", "Remove this book and its local data"); rm.type = "button"; rm.dataset.act = "remove"; li.append(rm);
    return li;
  }));
  $("u-empty").hidden = items.length > 0;
  $("u-radio-upload").disabled = items.length === 0;
}
$("u-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (!file) return;
  upStatus("Reading your book on this device\u2026");
  await new Promise((r) => setTimeout(r, 30)); // let the message appear before the heavy work
  try {
    const book = await readEbook(file);
    const passages = splitPassages(book.text);
    if (passages.length < 3) throw new UploadError("empty", "Not enough readable story text was found in this file.");
    await ready;
    await lib.addUpload({ title: book.title, author: book.author, fileName: file.name, size: file.size, passages });
    upStatus(`Added \u201c${book.title}\u201d with about ${passages.length} passages. It is stored only in this browser.`);
    await renderUploads();
  } catch (err) {
    upStatus(err instanceof UploadError ? err.message : "Something went wrong while reading this file. It may be damaged or too large for your browser.", "error");
  }
  e.target.value = "";
});
$("u-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act=remove]"); if (!b) return;
  const id = b.closest("li").dataset.id;
  if (!confirm("Remove this book and everything stored about it in this browser, including its history and saved entry?")) return;
  await lib.removeUpload(id);
  upStatus("Book removed from this browser.");
  const showing = S.source === "upload" && S.up.cur?.bookId === id;
  await renderUploads();
  if (showing) { S.up.cur = null; startCatalogue(); }
});
document.querySelectorAll("input[name=source]").forEach((r) => r.addEventListener("change", async () => {
  if (r.value === "upload") { if (await startUploads()) { location.hash = ""; route(); } else document.querySelector("input[name=source][value=catalogue]").checked = true; }
  else { await startCatalogue(); location.hash = ""; route(); }
}));

// ---------- views ----------
const VIEWS = { "": "view-roulette", "#history": "view-history", "#saved": "view-saved", "#upload": "view-upload" };
function route() {
  const h = location.hash in VIEWS ? location.hash : "";
  for (const id of Object.values(VIEWS)) $(id).hidden = id !== VIEWS[h];
  document.querySelectorAll(".main-nav a[data-view]").forEach((a) => (a.dataset.view === h ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  if (h === "#history") renderHistory(); else if (h === "#saved") renderSaved(); else if (h === "#upload") renderUploads();
  const heading = { "#history": "h-title", "#saved": "s-title", "#upload": "u-title" }[h];
  if (heading) $(heading).focus({ preventScroll: false });
}
addEventListener("hashchange", route);
route();
startCatalogue();
