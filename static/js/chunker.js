// JavaScript copy of the generic rules in textproc.py (used for books the reader uploads).
// Keep both files in sync and bump RULES_VERSION in both when a rule changes.
export const RULES_VERSION = "v1";
const MIN = 80, MAX = 150, TINY = 40;
const START_RE = /\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*\*\*/i;
const END_RE = /\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK/i;
const HEADING_RE = /^(chapter|book|part|volume|act|scene|stave|letter|section|epilogue|prologue|preface|contents|illustrations|appendix)\b/i;
const ROMAN_RE = /^[IVXLCDM]+\.?$/;
const SENT_RE = /(?<=[.!?])\s+|(?<=[.!?][\u201d"\u2019)\]])\s+/;

export const wordCount = (s) => s.split(/\s+/).filter(Boolean).length;
const letterCount = (s) => (s.match(/\p{L}/gu) || []).length;

export function headerFields(raw) {
  const m = START_RE.exec(raw);
  const head = m ? raw.slice(0, m.index) : raw.slice(0, 6000);
  const out = {};
  for (const key of ["Title", "Author"]) {
    const f = new RegExp(`^${key}:\\s*(.+)$`, "im").exec(head);
    if (f) out[key.toLowerCase()] = f[1].trim();
  }
  return out;
}

export function stripBoilerplate(raw) {
  raw = raw.replace(/\r\n?/g, "\n").replace(/^\uFEFF/, "");
  const s = START_RE.exec(raw), e = END_RE.exec(raw);
  if (!s || !e || e.index <= s.index + s[0].length) return null;
  return raw.slice(s.index + s[0].length, e.index).replace(/^\n+|\n+$/g, "");
}

export function toParagraphs(text) {
  const out = [];
  for (const block of text.replace(/\r\n/g, "\n").split(/\n\s*\n/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.length) continue;
    const lens = lines.map((l) => l.length);
    const verse = lines.length >= 3 && lens.reduce((a, b) => a + b, 0) / lines.length < 48 && Math.max(...lens) <= 72;
    out.push(verse ? lines.join("\n") : lines.join(" "));
  }
  return out;
}

const cleanParagraph = (p) =>
  p.replace(/\[Illustration[^\]]*\]/g, "").replace(/\[\d+\]/g, "").replace(/_/g, "").replace(/[ \t]+/g, " ").trim();

export function isStory(par) {
  const letters = letterCount(par);
  if (letters < 2 || letters / par.length < 0.55 || par.toUpperCase() === par) return false;
  if (/^(illustration|footnote|transcriber)/i.test(par.replace(/^[\[ ]+/, ""))) return false;
  if (HEADING_RE.test(par) && wordCount(par) <= 12) return false;
  if (ROMAN_RE.test(par.trim()) || par.toLowerCase().includes("project gutenberg")) return false;
  return true;
}

function findStart(pars) {
  let run = 0;
  for (let i = 0; i < pars.length; i++) {
    run = wordCount(pars[i]) >= 25 && isStory(pars[i]) ? run + 1 : 0;
    if (run >= 3) return [i - 2, true];
  }
  return [0, false];
}

function findEnd(pars) {
  const n = pars.length;
  for (let i = Math.floor(n * 0.9); i < n; i++) {
    const p = pars[i].trim();
    if (/^(the end|finis)\.?$/i.test(p) || p.toLowerCase().startsWith("transcriber")) return i;
  }
  return n;
}

// Works on any plain text. Gutenberg wrapper lines are removed if present.
export function cleanBookText(raw) {
  const body = stripBoilerplate(raw) ?? raw.replace(/\r\n?/g, "\n");
  const pars = toParagraphs(body).map(cleanParagraph).filter(Boolean);
  const [start, confident] = findStart(pars);
  const end = findEnd(pars.slice(start)) + start;
  const story = pars.slice(start, end).filter(isStory);
  return { text: story.join("\n\n"), startFound: confident };
}

function group(units, sep, min = MIN, max = MAX) {
  const out = []; let cur = [], n = 0;
  for (const u of units) {
    const w = wordCount(u);
    if (cur.length && n + w > max && (n >= min || n + w > max * 1.3)) {
      if (n >= TINY) out.push(cur.join(sep));
      cur = []; n = 0;
    }
    cur.push(u); n += w;
  }
  if (cur.length) {
    if (n >= TINY) out.push(cur.join(sep));
    else if (out.length) out[out.length - 1] += sep + cur.join(sep);
  }
  return out;
}

export function splitPassages(text, min = MIN, max = MAX) {
  let out = [], run = [];
  for (const par of toParagraphs(text)) {
    if (wordCount(par) > max) {
      out = out.concat(group(run, "\n\n", min, max)); run = [];
      const verse = par.includes("\n");
      const units = (verse ? par.split("\n") : par.split(SENT_RE)).filter((u) => u.trim());
      out = out.concat(group(units, verse ? "\n" : " ", min, max));
    } else run.push(par);
  }
  return out.concat(group(run, "\n\n", min, max));
}
