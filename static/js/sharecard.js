// The share card: a picture of the passage you drew, with the book it came from, drawn in the browser on a canvas.
// Nothing is uploaded: the text never leaves the device unless the visitor chooses to post the picture themselves.
export const CARD = { width: 1080, height: 1350, margin: 96, excerptChars: 330, maxExcerptLines: 11 };
const PAPER = "#faf7f0", INK = "#202020", MUTED = "#555555", TAN = "#ece4d6", LINE = "#d0d0d0";
const SERIF = 'Georgia, "Times New Roman", serif';

/** A readable excerpt: whole words, ends on a sentence if one is near, with an ellipsis when cut. */
export function excerpt(text, max = CARD.excerptChars) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  let cut = clean.slice(0, max);
  const sentence = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("? "), cut.lastIndexOf("! "), cut.lastIndexOf("\u3002"));
  if (sentence > max * 0.6) return cut.slice(0, sentence + 1);
  const space = cut.lastIndexOf(" ");
  cut = space > max * 0.6 ? cut.slice(0, space) : cut;                 // Chinese has no spaces: cut at the limit
  return cut.replace(/[\s,;:\u2014-]+$/, "") + "\u2026";
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

/** Draw the card. `data` = { text, title, author, badge?, host }. Returns the lines used (for tests). */
export function drawCard(ctx, data, size = CARD) {
  const { width, height, margin } = size, inner = width - margin * 2;
  const font = (px, style = "normal", weight = "normal") => { ctx.font = `${style} ${weight} ${px}px ${SERIF}`; return (t) => ctx.measureText(t).width; };
  ctx.fillStyle = PAPER; ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = INK; ctx.lineWidth = 3; ctx.strokeRect(36, 36, width - 72, height - 72);
  ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";

  ctx.fillStyle = INK; font(34, "normal", "bold"); ctx.fillText("LITERARY ROULETTE", margin, 130);
  ctx.fillStyle = MUTED; font(26, "italic"); ctx.fillText("A library of stories. One draw chooses the book.", margin, 172);
  ctx.strokeStyle = LINE; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(margin, 205); ctx.lineTo(width - margin, 205); ctx.stroke();

  // Lay the lower block out from the bottom up (footer, badge, author, title), so a long title or badge can never collide
  // with the footer; the passage then gets whatever room is left above it.
  const measureBadge = font(28);
  const badgeLines = data.badge ? wrapLines(measureBadge, data.badge, inner - 48, 2) : [];
  const measureTitle = font(54, "normal", "bold");
  const title = wrapLines(measureTitle, data.title || "", inner, 2);
  let y = height - 175;
  const boxH = badgeLines.length ? 20 + badgeLines.length * 38 : 0;
  if (boxH) { y -= boxH; var boxTop = y; y -= 30; }
  const authorY = y; y -= 58;
  const lastTitleY = y, firstTitleY = lastTitleY - 62 * (title.length - 1), ruleY = firstTitleY - 74;

  const measureBody = font(46);
  const room = Math.max(1, Math.floor((ruleY - 45 - 290) / 68) + 1);
  const body = wrapLines(measureBody, excerpt(data.text), inner, Math.min(room, size.maxExcerptLines ?? CARD.maxExcerptLines));
  ctx.fillStyle = INK; let by = 290; body.forEach((l) => { ctx.fillText(l, margin, by); by += 68; });

  ctx.strokeStyle = INK; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(margin, ruleY); ctx.lineTo(margin + 120, ruleY); ctx.stroke();
  font(54, "normal", "bold"); ctx.fillStyle = INK; title.forEach((l, i) => ctx.fillText(l, margin, firstTitleY + i * 62));
  font(36, "italic"); ctx.fillStyle = MUTED; ctx.fillText("by " + (data.author || ""), margin, authorY);
  if (boxH) {
    ctx.fillStyle = TAN; ctx.fillRect(margin, boxTop, inner, boxH);
    font(28); ctx.fillStyle = INK; badgeLines.forEach((l, i) => ctx.fillText(l, margin + 24, boxTop + 14 + 28 + i * 38));
  }
  ctx.fillStyle = INK; font(30, "italic"); ctx.fillText("Reading has gone completely random.", margin, height - 118);
  ctx.fillStyle = MUTED; font(26); ctx.fillText(data.host || "", margin, height - 76);
  return { body, title };
}

/** Build the PNG in the browser. */
export async function cardBlob(data, doc = document) {
  if (doc.fonts && doc.fonts.ready) { try { await doc.fonts.ready; } catch { /* draw with what we have */ } }
  const canvas = doc.createElement("canvas"); canvas.width = CARD.width; canvas.height = CARD.height;
  drawCard(canvas.getContext("2d"), data);
  return new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not make the picture."))), "image/png"));
}

/** Share the picture with the device's own share sheet if it can; otherwise download it. Returns "shared" | "downloaded" | "cancelled". */
export async function shareOrDownload(blob, { title, url, filename = "literary-roulette-discovery.png" }, nav = navigator, doc = document) {
  const file = new File([blob], filename, { type: "image/png" });
  if (nav.canShare && nav.canShare({ files: [file] }) && nav.share) {
    try { await nav.share({ files: [file], title, text: "Spin your own book: " + url, url }); return "shared"; }
    catch (err) { if (err && err.name === "AbortError") return "cancelled"; /* otherwise fall back to download */ }
  }
  const a = doc.createElement("a"); a.href = URL.createObjectURL(blob); a.download = filename;
  doc.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  return "downloaded";
}
