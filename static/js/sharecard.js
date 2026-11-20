// The share card: a picture of the passage you drew, with the book it came from, drawn in the browser on a canvas.
// Nothing is uploaded: the text never leaves the device unless the visitor chooses to post the picture themselves.
// Two layouts from the same code: "landscape" for computers, "mobile" (portrait) for phones.
const EXCERPT_CHARS = 330;
const C = { bg: "#F7F3EA", card: "#FBF9F3", line: "#DDD6C6", navy: "#1F2A44", grey: "#6B6A66", gold: "#E4A62F", white: "#FFFFFF" };
const TONES = { gold: { bg: "#F6E7C1", border: "#E5A92E", icon: "#E7A93A" }, green: { bg: "#DCEBDD", border: "#5E9467", icon: "#6FAE7A" }, navy: { bg: "#DFE4F0", border: "#6F7FA8", icon: "#8EA0CF" } };
const TIER_LOOK = { jackpot: ["star", "gold"], lucky: ["spark", "gold"], discovery: ["spark", "green"], "hidden-gem": ["gem", "navy"] };
const SANS = "'LR Poppins', system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'Noto Sans CJK SC', sans-serif";
const SERIF = "'LR Lora', Georgia, 'Times New Roman', 'PingFang SC', 'Noto Serif CJK SC', serif";

// Every number below comes from the two design files (canvas pixels).
export const LAYOUT = {
  landscape: { W: 2400, H: 1260, head: { x: 96, y: 87, size: 34 }, pill: { right: 2303, y: 66, h: 44, size: 26 },
    left: { x: 96, y: 160, w: 1391, h: 1003, r: 48 }, right: { x: 1537, y: 160, w: 766, h: 1003, r: 48 },
    quote: { x: 178, base: 353, size: 190 }, text: { x: 178, w: 1232, top: 388, bottom: 1010, size: 72, min: 40, lh: 1.445 },
    source: { x: 178, y: 1078, size: 30 }, badge: { x: 1602, y: 226, h: 60, size: 30, align: "left" }, found: { x: 1602, y: 370, size: 28 },
    title: { x: 1602, w: 636, top: 406, size: 72, min: 44, lh: 1.08, maxH: 240 }, author: { size: 38, gap: 36 },
    meta: { size: 28, first: 57, lh: 40, maxLines: 2 }, button: { x: 1602, y: 1002, w: 636, h: 96, r: 22, size: 36 }, urlLine: { y: 1133, size: 27 } },
  mobile: { W: 1080, H: 1350, head: { x: 64, y: 87, size: 34 }, pill: { right: 1015, y: 66, h: 44, size: 26 },
    left: { x: 65, y: 145, w: 950, h: 588, r: 48 }, right: { x: 65, y: 767, w: 950, h: 390, r: 48 },
    quote: { x: 115, base: 302, size: 160 }, text: { x: 115, w: 850, top: 314, bottom: 625, size: 50, min: 30, lh: 1.44 },
    source: { x: 115, y: 665, size: 29 }, badge: { x: 965, y: 817, h: 50, size: 28, align: "right" }, found: { x: 115, y: 842, size: 29 },
    title: { x: 115, w: 850, top: 891, size: 60, min: 40, lh: 1.15, maxH: 110 }, author: { size: 34, gap: 18 },
    meta: { size: 29, first: 53, lh: 40, maxLines: 1 }, button: { x: 65, y: 1190, w: 950, h: 95, r: 24, size: 34 }, urlLine: { y: 1319, size: 27 } },
};

/** A readable excerpt: whole words, ends on a sentence if one is near, with an ellipsis when cut. */
export function excerpt(text, max = EXCERPT_CHARS) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  let cut = clean.slice(0, max);
  const sentence = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("? "), cut.lastIndexOf("! "), cut.lastIndexOf("\u3002"));
  if (sentence > max * 0.6) return cut.slice(0, sentence + 1);
  const space = cut.lastIndexOf(" ");
  cut = space > max * 0.6 ? cut.slice(0, space) : cut;                 // Chinese has no spaces: cut at the limit
  return cut.replace(/[\s,;:\u2014-]+$/, "") + "\u2026";
}

/** The home page link. A PNG can never be clicked, so the link is (1) printed on the card and (2) sent as text with the picture. */
export function homeLink(doc = globalThis.document) {
  try { return doc.defaultView.location.origin + "/"; } catch { return ""; }
}
/** What is printed on the card: host and path only ("literary-roulette.com"). */
export function linkLabel(url) {
  try { const u = new URL(url); return (u.host + u.pathname).replace(/\/$/, ""); } catch { return ""; }
}
/** What travels as text: the same link plus ?ref=card so the site can count visits that came from a card. */
export function trackedLink(url) {
  try { const u = new URL(url); if (!u.searchParams.has("ref")) u.searchParams.set("ref", "card"); return u.toString(); } catch { return url || ""; }
}
/** A playful link: the home page plus the first five words of the shared passage,
 *  e.g. https://yoursite.com/?ref=card&from=we-agreed-that-as-we  (the server ignores both; people enjoy reading it). */
export function teaser(text, n = 5) {
  const t = String(text || "").trim(), spaced = /\s/.test(t);
  const parts = (spaced ? t.split(/\s+/) : Array.from(t)).map((w) => w.replace(/[^\p{L}\p{N}]+/gu, "")).filter(Boolean).slice(0, n);
  return parts.join(spaced ? "-" : "").toLowerCase().slice(0, 40);
}
export function shareLink(passage, doc = globalThis.document) {
  try {
    const u = new URL(homeLink(doc)); u.searchParams.set("ref", "card");
    const t = teaser(passage); if (t) u.searchParams.set("from", t);
    return u.toString();
  } catch { return ""; }
}
/** Greedy line breaking with the canvas's own measurement. Words that are too long are split so nothing overflows. */
export function wrapLines(measure, text, maxWidth, maxLines = Infinity) {
  const tokens = /\s/.test(text) ? text.split(/\s+/) : Array.from(text);       // words, or single characters for Chinese
  const glue = /\s/.test(text) ? " " : "";
  const lines = []; let line = "";
  for (let token of tokens) {
    while (measure(token) > maxWidth && token.length > 1) {                       // a single huge word
      let n = token.length; while (n > 1 && measure(token.slice(0, n)) > maxWidth) n--;
      if (line) { lines.push(line); line = ""; }
      lines.push(token.slice(0, n)); token = token.slice(n);
    }
    const trial = line ? line + glue + token : token;
    if (measure(trial) <= maxWidth) line = trial; else { if (line) lines.push(line); line = token; }
  }
  if (line) lines.push(line);
  if (lines.length > maxLines) { const kept = lines.slice(0, maxLines); kept[maxLines - 1] = kept[maxLines - 1].replace(/[\s,;:.\u2014-]+$/, "") + "\u2026"; return kept; }
  return lines;
}


const FONT_FILES = [["LR Poppins", 400, "poppins-latin-400-normal.woff2"], ["LR Poppins", 500, "poppins-latin-500-normal.woff2"],
  ["LR Poppins", 600, "poppins-latin-600-normal.woff2"], ["LR Poppins", 700, "poppins-latin-700-normal.woff2"],
  ["LR Lora", 400, "lora-latin-400-normal.woff2"], ["LR Lora", 700, "lora-latin-700-normal.woff2"]];
let fontsReady = null;
/** Load the card fonts once, only when someone makes a card. If anything fails the card uses system fonts. */
function loadFonts(doc) {
  const Face = globalThis.FontFace;
  if (fontsReady || !Face || !doc.fonts) return fontsReady || (fontsReady = Promise.resolve());
  return (fontsReady = Promise.all(FONT_FILES.map(([family, weight, file]) =>
    new Face(family, `url(${new URL("../fonts/share/" + file, import.meta.url)}) format("woff2")`, { weight: String(weight) })
      .load().then((face) => doc.fonts.add(face)).catch(() => {}))));
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
/** Shrink the font until the wrapped text fits the box; if it never fits, end with an ellipsis. */
function fitText(ctx, text, weight, family, size, min, maxW, maxH, ratio) {
  const measure = (t) => ctx.measureText(t).width;
  for (;;) {
    ctx.font = `${weight} ${size}px ${family}`;
    const lh = size * ratio;
    if (wrapLines(measure, text, maxW).length * lh <= maxH || size <= min) {
      return { lines: wrapLines(measure, text, maxW, Math.max(1, Math.floor(maxH / lh))), size, lh };
    }
    size -= 2;
  }
}
function drawIcon(ctx, kind, cx, cy, s, color) {
  ctx.save(); ctx.lineWidth = Math.max(2, s / 10); ctx.lineJoin = "round"; ctx.strokeStyle = C.navy; ctx.fillStyle = color; ctx.beginPath();
  if (kind === "star") {
    for (let i = 0; i < 10; i++) { const a = -Math.PI / 2 + i * Math.PI / 5, r = i % 2 ? s * 0.22 : s * 0.5; ctx[i ? "lineTo" : "moveTo"](cx + Math.cos(a) * r, cy + Math.sin(a) * r); }
  } else if (kind === "gem") {
    ctx.moveTo(cx - s * 0.5, cy - s * 0.12); ctx.lineTo(cx - s * 0.28, cy - s * 0.4); ctx.lineTo(cx + s * 0.28, cy - s * 0.4); ctx.lineTo(cx + s * 0.5, cy - s * 0.12); ctx.lineTo(cx, cy + s * 0.5);
  } else {
    ctx.moveTo(cx, cy - s * 0.5); ctx.quadraticCurveTo(cx, cy, cx + s * 0.5, cy); ctx.quadraticCurveTo(cx, cy, cx, cy + s * 0.5);
    ctx.quadraticCurveTo(cx, cy, cx - s * 0.5, cy); ctx.quadraticCurveTo(cx, cy, cx, cy - s * 0.5);
  }
  ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.restore();
}

/** Draw the card. `data` = { text, title, author, badge?: {label, tier}, meta?: [..], source?, pill?, cta?, url? }. Returns the lines used (for tests). */
export function drawCard(ctx, data, format = "landscape") {
  const L = LAYOUT[format] || LAYOUT.landscape, setFont = (w, px, fam) => { ctx.font = `${w} ${px}px ${fam}`; };
  const measure = (t) => ctx.measureText(t).width, one = (t, w) => wrapLines(measure, String(t || ""), w, 1)[0] || "";
  ctx.textBaseline = "middle"; ctx.textAlign = "left";
  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, L.W, L.H);

  ctx.fillStyle = C.navy; setFont(600, L.head.size, SANS); ctx.fillText("Literary Roulette", L.head.x, L.head.y);
  if (data.pill) {
    setFont(500, L.pill.size, SANS);
    const pw = measure(data.pill) + 36, px = L.pill.right - pw;
    roundRect(ctx, px, L.pill.y, pw, L.pill.h, L.pill.h / 2); ctx.strokeStyle = "#D8D2C2"; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = C.grey; ctx.fillText(data.pill, px + 18, L.pill.y + L.pill.h / 2 + 1);
  }
  for (const c of [L.left, L.right]) {
    roundRect(ctx, c.x, c.y, c.w, c.h, c.r); ctx.fillStyle = C.card; ctx.fill(); ctx.strokeStyle = C.line; ctx.lineWidth = 2; ctx.stroke();
  }

  // the passage
  ctx.textBaseline = "alphabetic"; ctx.fillStyle = C.gold; setFont(700, L.quote.size, SERIF); ctx.fillText("\u201C", L.quote.x, L.quote.base);
  ctx.textBaseline = "middle"; ctx.fillStyle = C.navy;
  const t = L.text, body = fitText(ctx, excerpt(data.text), 400, SERIF, t.size, t.min, t.w, t.bottom - t.top, t.lh);
  setFont(400, body.size, SERIF); body.lines.forEach((ln, i) => ctx.fillText(ln, t.x, t.top + body.lh * (i + 0.5)));
  if (data.source) { setFont(400, L.source.size, SANS); ctx.fillStyle = C.grey; ctx.fillText(one(data.source, t.w), L.source.x, L.source.y); }

  // the badge: a tone and a mark per tier
  if (data.badge && data.badge.label) {
    const [kind, toneName] = TIER_LOOK[data.badge.tier] || TIER_LOOK.discovery, tone = TONES[toneName], b = L.badge;
    setFont(600, b.size, SANS);
    const label = one(data.badge.label, L.right.w - 150), iconW = b.h * 0.5 + 14, bw = measure(label) + iconW + 52, bx = b.align === "right" ? b.x - bw : b.x;
    roundRect(ctx, bx, b.y, bw, b.h, b.h / 2); ctx.fillStyle = tone.bg; ctx.fill(); ctx.strokeStyle = tone.border; ctx.lineWidth = 3; ctx.stroke();
    drawIcon(ctx, kind, bx + 26 + b.h * 0.25, b.y + b.h / 2, b.h * 0.5, tone.icon);
    ctx.fillStyle = C.navy; ctx.fillText(label, bx + 26 + iconW, b.y + b.h / 2 + 1);
  }

  // the book
  setFont(400, L.found.size, SANS); ctx.fillStyle = C.grey; ctx.fillText("You found", L.found.x, L.found.y);
  const ti = L.title, title = fitText(ctx, data.title || "", 700, SANS, ti.size, ti.min, ti.w, ti.maxH, ti.lh);
  setFont(700, title.size, SANS); ctx.fillStyle = C.navy; title.lines.forEach((ln, i) => ctx.fillText(ln, ti.x, ti.top + title.lh * (i + 0.5)));
  const authorY = ti.top + title.lines.length * title.lh + L.author.gap + L.author.size / 2;
  setFont(400, L.author.size, SANS); ctx.fillStyle = C.grey; ctx.fillText(one(data.author, ti.w), ti.x, authorY);
  setFont(400, L.meta.size, SANS);
  const meta = (data.meta || []).flatMap((m) => wrapLines(measure, String(m), ti.w, L.meta.maxLines)).slice(0, L.meta.maxLines + 1);
  meta.forEach((ln, i) => ctx.fillText(ln, ti.x, authorY + L.meta.first + i * L.meta.lh));

  const bt = L.button;
  roundRect(ctx, bt.x, bt.y, bt.w, bt.h, bt.r); ctx.fillStyle = C.navy; ctx.fill();
  setFont(600, bt.size, SANS); ctx.fillStyle = C.white; ctx.textAlign = "center"; ctx.fillText(data.cta || "Spin your own book", bt.x + bt.w / 2, bt.y + bt.h / 2 + 2);
  const label = linkLabel(data.url);
  if (label) { setFont(500, L.urlLine.size, SANS); ctx.fillStyle = C.grey; ctx.fillText(one(label, bt.w), bt.x + bt.w / 2, L.urlLine.y); }
  ctx.textAlign = "left";
  return { body: body.lines, title: title.lines, format: LAYOUT[format] ? format : "landscape" };
}

let lastCard = { key: "", promise: null };
/** Build the PNG in the browser. Computers get the wide card, phones the tall one.
 *  The last card is remembered, so calling warmCard() while the visitor reads makes the Share tap instant
 *  (iPhones and iPads only allow sharing very soon after a tap). */
export function cardBlob(data, doc = document) {
  const width = doc.defaultView && doc.defaultView.innerWidth;
  const full = { ...data, url: data.url || homeLink(doc) };
  const key = JSON.stringify([full, width]);
  if (lastCard.key === key && lastCard.promise) return lastCard.promise;
  const promise = (async () => {
    await loadFonts(doc);
    if (doc.fonts && doc.fonts.ready) { try { await doc.fonts.ready; } catch { /* draw with what we have */ } }
    const format = full.format || (width < 760 ? "mobile" : "landscape"), size = LAYOUT[format] || LAYOUT.landscape;
    const canvas = doc.createElement("canvas"); canvas.width = size.W; canvas.height = size.H;
    drawCard(canvas.getContext("2d"), full, format);
    return new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not make the picture."))), "image/png"));
  })();
  lastCard = { key, promise };
  promise.catch(() => { if (lastCard.promise === promise) lastCard = { key: "", promise: null }; });
  return promise;
}
/** Start drawing in the background (call it when a book is revealed). Never throws. */
export function warmCard(data, doc = document) { try { cardBlob(data, doc).catch(() => {}); } catch { /* ignore */ } }

const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function blobToDataUrl(blob) {
  return new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => no(r.error); r.readAsDataURL(blob); });
}
/** Phones and tablets: their share sheets are good. Computers: the system "Copy" in the share sheet pastes TWO pictures. */
function isTouchDevice(nav, doc) {
  try { return nav.maxTouchPoints > 0 && doc.defaultView.matchMedia("(pointer: coarse)").matches; } catch { return false; }
}
/** Put ONE clipboard item holding the picture in three forms: image (Messages, Slack, Discord...), a clickable
 *  picture-link (Mail, Docs, Word, Notes...) and plain text (any text box). An app takes one form, so nothing doubles. */
async function copyPicture(blob, link, message, alt, nav) {
  const Item = globalThis.ClipboardItem;
  if (!Item || !nav.clipboard || !nav.clipboard.write) return false;
  const html = blobToDataUrl(blob).then((u) => new Blob([`<a href="${esc(link)}"><img src="${u}" width="720" alt="${esc(alt)}"></a>`], { type: "text/html" }));
  try { await nav.clipboard.write([new Item({ "image/png": blob, "text/html": html, "text/plain": new Blob([message], { type: "text/plain" }) })]); return true; }
  catch { return false; }
}
/** The sentence to show after shareOrDownload(). */
export function describeShare(how) {
  return { shared: "Shared.", copied: "Copied the picture and the link. Paste them anywhere.",
           downloaded: "Picture saved. The link is copied too: paste it with the picture.", cancelled: "" }[how] || "";
}

/** Share the picture and the home page link together.
 *  Phones and tablets: the device share sheet (one picture + one text line with the link).
 *  Computers: one clipboard copy (picture + clickable version + link text), so pasting never gives two pictures.
 *  Otherwise: the picture is downloaded and the link copied. Pass { prefer: "share" } to force the share sheet.
 *  Returns "shared" | "copied" | "downloaded" | "cancelled". */
export async function shareOrDownload(blob, { title, text, url, prefer, filename = "literary-roulette-discovery.png" } = {}, nav = navigator, doc = document) {
  const link = trackedLink(url || homeLink(doc));
  const message = [text || title, link].filter(Boolean).join("\n");          // the link lives inside the text: one field, so nothing is shown twice
  if (prefer !== "share" && !isTouchDevice(nav, doc) && await copyPicture(blob, link, message, title || "Literary Roulette", nav)) return "copied";
  let file = null;
  try { file = new File([blob], filename, { type: "image/png" }); } catch { /* very old browser: download below */ }
  if (file && nav.share && nav.canShare && nav.canShare({ files: [file] })) {
    try { await nav.share({ files: [file], title, text: message }); return "shared"; }
    catch (err) { if (err && err.name === "AbortError") return "cancelled"; /* otherwise fall back to download */ }
  }
  const a = doc.createElement("a"); a.href = URL.createObjectURL(blob); a.download = filename;
  doc.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  try { if (link && nav.clipboard && nav.clipboard.writeText) await nav.clipboard.writeText(link); } catch { /* the picture still carries the address */ }
  return "downloaded";
}