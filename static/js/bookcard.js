// Text helpers for the book card. Pure functions, no DOM: change how names, tags or the pull-quote are shaped here and nowhere else.
const ROLE_LABEL = { illustrator: "Illustrated by", translator: "Translated by", editor: "Edited by", compiler: "Compiled by" };
const YEARS = /,?\s*\(?\s*(\d{3,4})\??\s*-\s*(\d{3,4})?\??\s*\)?\s*$/;
const SUFFIX = /^(jr|sr|ii|iii|iv)\.?$/i;

function person(chunk) {
  let role = "";
  let s = chunk.replace(/\s*\[([^\]]+)\]\s*$/, (_, r) => { role = r.trim().toLowerCase(); return ""; });
  let years = "";
  s = s.replace(YEARS, (_, a, b) => { years = a + "\u2013" + (b || ""); return ""; });
  s = s.replace(/[,\s]+$/, "");
  const parts = s.split(/\s*,\s*/).filter(Boolean);
  let name = s;
  // "Last, First" becomes "First Last"; descriptors such as "Homer, active 9th century B.C." keep just the first part
  if (parts.length >= 2 && !/\d/.test(parts.slice(1).join(" ")) && !/^[a-z]/.test(parts[1])) {
    const rest = parts.slice(1);
    const suffix = SUFFIX.test(rest[rest.length - 1]) ? rest.pop() : "";
    name = [rest.join(" "), parts[0], suffix].filter(Boolean).join(" ");
  } else if (parts.length >= 2) name = parts[0];
  return { name, years, role };
}

/** "Standish, Burt L., 1866-1945; Owen, Robert Emmett, 1878-1957 [Illustrator]"
 *  -> { by: "Burt L. Standish", years: "1866\u20131945", roles: ["Illustrated by Robert Emmett Owen"] } */
export function formatAuthor(raw) {
  const text = String(raw == null ? "" : raw).replace(/\s+/g, " ").trim();
  if (!text) return { by: "", years: "", roles: [] };
  const people = text.split(/\s*;\s*/).filter(Boolean).map(person);
  let main = people.filter((p) => !p.role);
  const extra = main.length ? people.filter((p) => p.role) : [];
  if (!main.length) main = people;
  const names = main.map((p) => p.name);
  const by = names.length === 1 ? names[0] : names.length === 2 ? names.join(" & ") : names[0] + " et al.";
  const groups = new Map();
  for (const p of extra) groups.set(p.role, [...(groups.get(p.role) || []), p.name]);
  const roles = [...groups].map(([role, who]) => {
    const label = ROLE_LABEL[role] || role.charAt(0).toUpperCase() + role.slice(1) + ":";
    return `${label} ${who.join(" & ")}`;
  });
  return { by, years: main.length === 1 ? main[0].years : "", roles };
}

/** "Category: Humour" -> "Humour"; drops empties and repeats. */
export function cleanGenres(list) {
  const seen = new Set(), out = [];
  for (const g of Array.isArray(list) ? list : []) {
    const t = String(g).replace(/^\s*category\s*:\s*/i, "").trim();
    if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); out.push(t); }
  }
  return out;
}

/** The first complete, readable sentence of the passage the person just read (a pull-quote), or "" when there is none. */
export function teaserFrom(text) {
  const t = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  const cjk = /[\u3400-\u9fff]/.test(t);
  const min = cjk ? 14 : 40, max = cjk ? 90 : 170;
  for (const m of t.matchAll(/[^.!?\u3002\uff01\uff1f]+[.!?\u3002\uff01\uff1f]+["\u201d\u2019')\u300d]*/g)) {
    const s = m[0].trim();
    if (s.length >= min && s.length <= max && /^["\u201c\u2018'(\u300c]?[\p{Lu}\p{Lo}\d]/u.test(s)) return s;
  }
  return "";
}
