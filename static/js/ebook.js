// Read .txt and .epub files in the browser. No file is ever sent to the server.
import { cleanBookText, headerFields, wordCount } from "./chunker.js";

export const MAX_BYTES = 15 * 1024 * 1024;
const MAX_ENTRY_BYTES = 30 * 1024 * 1024;
const MIN_WORDS = 300;
const FONT_OBFUSCATION = ["http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"];
const SKIP_NAMES = /(cover|title|toc|nav|copyright|colophon|imprint|licen[cs]e|dedication|front|about|gutenberg|acknowledg|contents)/i;
const SKIP_GUIDE = /^(cover|title-page|toc|copyright-page|dedication|acknowledgements|colophon)$/i;

export class UploadError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const fail = (code, message) => { throw new UploadError(code, message); };

// ---------- text files ----------
function decodeText(buf) {
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); }
  catch { text = new TextDecoder("windows-1252").decode(buf); }
  const bad = (text.match(/[\u0000-\u0008\u000e-\u001f]/g) || []).length;
  if (bad > text.length * 0.01 + 3) fail("corrupted", "This does not look like a plain-text book. It may be damaged or in a different format.");
  return text;
}

// ---------- minimal ZIP reader (EPUB is a ZIP file) ----------
async function inflate(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function readZipDirectory(buf) {
  const v = new DataView(buf);
  let eocd = -1;
  for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 65557); i--) {
    if (v.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) fail("corrupted", "This file could not be read as an EPUB. It may be damaged.");
  const count = v.getUint16(eocd + 10, true);
  let pos = v.getUint32(eocd + 16, true);
  if (count === 0xffff || pos === 0xffffffff) fail("unsupported", "Very large (ZIP64) EPUB files are not supported.");
  const entries = new Map();
  for (let n = 0; n < count; n++) {
    if (pos + 46 > buf.byteLength || v.getUint32(pos, true) !== 0x02014b50) fail("corrupted", "This EPUB's file list is damaged.");
    const method = v.getUint16(pos + 10, true), csize = v.getUint32(pos + 20, true), usize = v.getUint32(pos + 24, true);
    const nlen = v.getUint16(pos + 28, true), elen = v.getUint16(pos + 30, true), clen = v.getUint16(pos + 32, true);
    const offset = v.getUint32(pos + 42, true);
    const name = new TextDecoder().decode(new Uint8Array(buf, pos + 46, nlen));
    entries.set(name, { method, csize, usize, offset });
    pos += 46 + nlen + elen + clen;
  }
  return entries;
}

async function zipRead(buf, entry) {
  const v = new DataView(buf);
  if (entry.usize > MAX_ENTRY_BYTES) fail("too_large", "A part of this EPUB is unusually large, so it was not opened.");
  if (entry.offset + 30 > buf.byteLength || v.getUint32(entry.offset, true) !== 0x04034b50) fail("corrupted", "This EPUB is damaged.");
  const start = entry.offset + 30 + v.getUint16(entry.offset + 26, true) + v.getUint16(entry.offset + 28, true);
  const raw = new Uint8Array(buf, start, entry.csize);
  if (entry.method === 0) return raw;
  if (entry.method === 8) {
    try { return await inflate(raw); } catch { fail("corrupted", "This EPUB contains damaged content."); }
  }
  return fail("unsupported", "This EPUB uses a compression method that is not supported.");
}

// ---------- EPUB parsing (regular expressions, so it also runs outside a browser) ----------
const attr = (tag, name) => { const m = new RegExp(`\\b${name}\\s*=\\s*["']([^"']*)["']`, "i").exec(tag); return m ? m[1] : null; };
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", mdash: "\u2014", ndash: "\u2013", hellip: "\u2026",
  lsquo: "\u2018", rsquo: "\u2019", ldquo: "\u201c", rdquo: "\u201d" };
const decodeEntities = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
  if (e[0] === "#") { const c = e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10); return c > 0 && c < 0x110000 ? String.fromCodePoint(c) : ""; }
  return ENTITIES[e.toLowerCase()] ?? m;
});

function htmlToText(html) {
  return decodeEntities(html
    .replace(/<head[\s\S]*?<\/head>/gi, "").replace(/<(script|style)[\s\S]*?<\/\1>/gi, "")
    .replace(/<h[1-6][\s\S]*?<\/h[1-6]>/gi, "\n\n")          // chapter titles are not story text
    .replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|li|blockquote|tr|section)>/gi, "\n\n")
    .replace(/<[^>]+>/g, "")).replace(/[ \t\u00a0]+/g, " ").replace(/\n[ \t]+/g, "\n");
}

async function readEpub(buf) {
  const zip = readZipDirectory(buf);
  const text = async (name) => new TextDecoder().decode(await zipRead(buf, zip.get(name)));
  if (zip.has("META-INF/rights.xml")) fail("drm", "This EPUB is protected by DRM. Literary Roulette cannot open protected books and does not try to remove DRM.");
  if (zip.has("META-INF/encryption.xml")) {
    const algos = [...(await text("META-INF/encryption.xml")).matchAll(/Algorithm\s*=\s*["']([^"']+)["']/gi)].map((m) => m[1]);
    if (algos.some((a) => !FONT_OBFUSCATION.includes(a))) fail("drm", "This EPUB is protected by DRM. Literary Roulette cannot open protected books and does not try to remove DRM.");
  }
  if (!zip.has("META-INF/container.xml")) fail("corrupted", "This does not look like a valid EPUB (no container file).");
  const opfPath = attr((await text("META-INF/container.xml")).match(/<rootfile\b[^>]*>/i)?.[0] ?? "", "full-path");
  if (!opfPath || !zip.has(opfPath)) fail("corrupted", "This EPUB's book description file is missing.");
  const opf = await text(opfPath);
  const dir = opfPath.includes("/") ? opfPath.slice(0, opfPath.lastIndexOf("/") + 1) : "";

  const items = new Map();
  for (const tag of opf.match(/<item\b[^>]*>/gi) || []) items.set(attr(tag, "id"), { href: attr(tag, "href"), type: attr(tag, "media-type") || "", props: attr(tag, "properties") || "" });
  const guideSkip = new Set((opf.match(/<reference\b[^>]*>/gi) || []).filter((t) => SKIP_GUIDE.test(attr(t, "type") || "")).map((t) => decodeURIComponent((attr(t, "href") || "").split("#")[0])));
  const meta = (tag) => decodeEntities((new RegExp(`<dc:${tag}\\b[^>]*>([\\s\\S]*?)</dc:${tag}>`, "i").exec(opf)?.[1] ?? "").replace(/<[^>]+>/g, "")).trim();

  const parts = [];
  for (const ref of opf.match(/<itemref\b[^>]*>/gi) || []) {
    const item = items.get(attr(ref, "idref"));
    if (!item || !/x?html/i.test(item.type) || /\bnav\b/.test(item.props) || attr(ref, "linear") === "no") continue;
    const href = decodeURIComponent(item.href.split("#")[0]);
    if (SKIP_NAMES.test(href.split("/").pop()) || guideSkip.has(href)) continue;
    const entry = zip.get(dir + href);
    if (!entry) continue;
    parts.push(htmlToText(new TextDecoder().decode(await zipRead(buf, entry))));
  }
  while (parts.length > 1 && wordCount(parts[0]) < 250) parts.shift(); // short leading pages are front matter
  return { text: parts.join("\n\n"), title: meta("title"), author: meta("creator") };
}

// ---------- public entry point ----------
// `file` needs: name, size, arrayBuffer()  (a browser File works).
export async function readEbook(file, { maxBytes = MAX_BYTES } = {}) {
  const ext = (file.name || "").toLowerCase().split(".").pop();
  if (!["txt", "epub"].includes(ext)) fail("unsupported", "That file type is not supported yet. Choose a .txt or .epub file.");
  if (!file.size) fail("empty", "That file is empty.");
  if (file.size > maxBytes) fail("too_large", `That file is larger than ${Math.round(maxBytes / 1048576)} MB, which is the limit for uploads.`);
  const buf = await file.arrayBuffer();
  const base = file.name.replace(/\.[^.]+$/, "");
  let title = base, author = "", raw;
  if (ext === "txt") {
    raw = decodeText(buf);
    const h = headerFields(raw);
    title = h.title || base; author = h.author || "";
  } else {
    const epub = await readEpub(buf);
    raw = epub.text; title = epub.title || base; author = epub.author;
  }
  const { text } = cleanBookText(raw);
  if (wordCount(text) < MIN_WORDS) fail("empty", "Not enough readable story text was found in this file.");
  return { title, author, text };
}
