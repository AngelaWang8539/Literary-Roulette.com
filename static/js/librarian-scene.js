// The desktop app's black-and-white stick librarian, redrawn as SVG: same figure, trolley, big book,
// indexing desk, stamp and dropped book, same geometry and walking rhythm (85 ms per frame).
// Pure functions: given a mode and a frame number they return SVG markup (easy to test, nothing to load).
export const TICK_MS = 85;
export const HEIGHT = 86;
export const TERMINAL_FRAMES = 25;
const INK = "#202020", PAPER = "#ffffff", PENCIL = "#555555", FLOOR = 74;
const TAU = Math.PI * 2;

const f = (n) => Math.round(n * 100) / 100;
const line = (x1, y1, x2, y2, color = INK, w = 1) => `<line x1="${f(x1)}" y1="${f(y1)}" x2="${f(x2)}" y2="${f(y2)}" stroke="${color}" stroke-width="${w}" stroke-linecap="round"/>`;
const oval = (x1, y1, x2, y2, fill, stroke, w = 1) => `<ellipse cx="${f((x1 + x2) / 2)}" cy="${f((y1 + y2) / 2)}" rx="${f((x2 - x1) / 2)}" ry="${f((y2 - y1) / 2)}" fill="${fill}" stroke="${stroke}" stroke-width="${w}"/>`;
const rect = (x1, y1, x2, y2, fill, stroke, w = 1) => `<rect x="${f(x1)}" y="${f(y1)}" width="${f(x2 - x1)}" height="${f(y2 - y1)}" fill="${fill}" stroke="${stroke}" stroke-width="${w}"/>`;
const poly = (pts, fill, stroke, w = 1) => `<polygon points="${pts.map(f).join(" ")}" fill="${fill}" stroke="${stroke}" stroke-width="${w}" stroke-linejoin="round"/>`;
const topArc = (x1, y1, x2, y2, w = 2) => { const cy = (y1 + y2) / 2, rx = (x2 - x1) / 2, ry = (y2 - y1) / 2; return `<path d="M${f(x2)} ${f(cy)} A${f(rx)} ${f(ry)} 0 0 0 ${f(x1)} ${f(cy)}" fill="none" stroke="${INK}" stroke-width="${w}" stroke-linecap="round"/>`; };
const text = (x, y, s, { anchor = "start", color = INK, size = 12, style = "italic", weight = "normal" } = {}) =>
  `<text x="${f(x)}" y="${f(y)}" text-anchor="${anchor}" dominant-baseline="${y < 20 ? "hanging" : "middle"}" fill="${color}" font-family="Georgia, 'Times New Roman', serif" font-size="${size}" font-style="${style}" font-weight="${weight}">${s}</text>`;

export function newScene(width = 420) {
  return { mode: "idle", frame: 0, position: width + 30, books: 2 };
}

export function startScene(scene, mode, width) {
  scene.mode = ["catalogue", "load", "import"].includes(mode) ? mode : "load";
  scene.frame = 0; scene.position = width + 30; scene.books = 2;
}
export function finishScene(scene, ok) { scene.mode = ok ? "complete" : "failure"; scene.frame = 0; }

export function tickScene(scene, width) {
  scene.frame += 1;
  if (scene.mode === "catalogue") {
    scene.position -= 7;
    if (scene.position < -205) { scene.position = width + 30; scene.books = Math.min(7, scene.books + 1); }
  } else if (scene.mode === "load") {
    scene.position -= 8;
    if (scene.position < -135) scene.position = width + 30;
  } else if ((scene.mode === "complete" || scene.mode === "failure") && scene.frame >= TERMINAL_FRAMES) {
    scene.mode = scene.mode === "complete" ? "complete_still" : "failure_still";
  }
}

const bob = (scene, walking) => (walking ? -Math.abs(Math.sin(scene.frame * (TAU / 8) * 2)) * 1.35 : 0);

function legs(scene, px, bodyFloor, walking) {
  const hips = [px - 5, px + 5];
  if (!walking) {
    return line(hips[0], bodyFloor - 15, px - 9, FLOOR - 3, INK, 2) + line(hips[1], bodyFloor - 15, px + 9, FLOOR - 3, INK, 2) +
      line(px - 12, FLOOR - 2, px - 5, FLOOR - 2, INK, 2) + line(px + 6, FLOOR - 2, px + 14, FLOOR - 2, INK, 2);
  }
  const phase = scene.frame * (TAU / 8);
  return hips.map((hipX, i) => {
    const swing = Math.sin(phase + (i ? Math.PI : 0)), lift = Math.max(0, swing) * 3.8;
    const kx = hipX + swing * 3.8, ky = FLOOR - 9 - Math.max(0, swing), fx = hipX + swing * 7, fy = FLOOR - 2 - lift;
    return line(hipX, bodyFloor - 15, kx, ky, INK, 2) + line(kx, ky, fx, fy, INK, 2) + line(fx - 4, fy, fx + 2, fy - 0.5, INK, 2);
  }).join("");
}

function trolley(x, books) {
  let s = line(x + 72, FLOOR - 26, x + 86, FLOOR - 29, INK, 2) + line(x + 12, FLOOR - 27, x + 77, FLOOR - 27, INK, 2) +
    line(x + 12, FLOOR - 27, x + 17, FLOOR - 12, INK, 2) + line(x + 77, FLOOR - 27, x + 72, FLOOR - 12, INK, 2) +
    line(x + 11, FLOOR - 12, x + 74, FLOOR - 12, INK, 2) + oval(x + 14, FLOOR - 10, x + 26, FLOOR + 2, PAPER, INK, 2) +
    oval(x + 60, FLOOR - 10, x + 72, FLOOR + 2, PAPER, INK, 2);
  for (let i = 0; i < Math.max(1, Math.min(7, books)); i++) {
    const w = 7 + (i % 2), bx = x + 17 + i * 8, top = FLOOR - 29 - (18 + (i % 3) * 3);
    s += rect(bx, top, bx + w, FLOOR - 29, PAPER, INK, 1) + line(bx + 2, top + 4, bx + 2, FLOOR - 32, PENCIL, 1);
    if (i % 2 === 0) s += line(bx + 3, top + 7, bx + w - 1, top + 7, PENCIL, 1);
  }
  return s;
}

function head(px, bf, bun = true) {
  return oval(px - 10, bf - 58, px + 10, bf - 37, PAPER, INK, 2) + topArc(px - 11, bf - 62, px + 11, bf - 40) +
    (bun ? oval(px + 7, bf - 57, px + 14, bf - 50, PAPER, INK, 1) : "") +
    oval(px - 5, bf - 49, px - 1, bf - 45, INK, INK) + oval(px + 2, bf - 49, px + 6, bf - 45, INK, INK) +
    line(px - 1, bf - 42, px + 3, bf - 41, INK, 1);
}

function librarian(scene, px, pose) {
  const walking = scene.mode === "catalogue" && pose === "push", bf = FLOOR + bob(scene, walking);
  let s = head(px, bf) + poly([px - 12, bf - 38, px + 11, bf - 38, px + 10, bf - 15, px - 9, bf - 15], PAPER, INK, 2) +
    line(px, bf - 36, px, bf - 17, PENCIL, 1) + oval(px - 2, bf - 31, px, bf - 29, INK, INK) + oval(px - 2, bf - 25, px, bf - 23, INK, INK);
  if (pose === "wave") {
    const lift = (Math.floor(scene.frame / 3) % 2) * 2;
    s += line(px - 9, bf - 33, px - 22, bf - 25, INK, 2) + line(px + 9, bf - 33, px + 19, bf - 48 - lift, INK, 2) +
      oval(px + 15, bf - 53 - lift, px + 23, bf - 46 - lift, PAPER, INK, 2);
  } else if (pose === "stamp") {
    const drop = scene.mode === "import" && Math.floor(scene.frame / 2) % 2 ? 2 : 0;
    s += line(px - 9, bf - 33, px - 20, bf - 20, INK, 2) + line(px + 9, bf - 33, px + 2, bf - 20 + drop, INK, 2);
  } else {
    s += line(px - 10, bf - 33, px - 29, bf - 28, INK, 2) + oval(px - 33, bf - 31, px - 27, bf - 25, PAPER, INK, 1) +
      line(px + 9, bf - 33, px + 17, bf - 23, INK, 2) + oval(px + 14, bf - 25, px + 20, bf - 19, PAPER, INK, 1);
  }
  return s + legs(scene, px, bf, walking);
}

function bigBook(scene, x) {
  const px = x + 91, walking = scene.mode === "load", bf = FLOOR + bob(scene, walking);
  return poly([x + 4, bf - 51, x + 52, bf - 47, x + 56, bf - 17, x + 7, bf - 20], PAPER, INK, 2) +
    line(x + 30, bf - 49, x + 32, bf - 19, INK, 1) + line(x + 10, bf - 42, x + 26, bf - 41, PENCIL, 1) +
    line(x + 35, bf - 40, x + 49, bf - 39, PENCIL, 1) + line(x + 10, bf - 36, x + 26, bf - 35, PENCIL, 1) +
    head(px, bf, false) + poly([px - 12, bf - 38, px + 10, bf - 38, px + 10, bf - 15, px - 9, bf - 15], PAPER, INK, 2) +
    line(px - 10, bf - 33, x + 49, bf - 36, INK, 3) + oval(x + 45, bf - 40, x + 53, bf - 32, PAPER, INK, 2) +
    line(px + 9, bf - 33, x + 48, bf - 23, INK, 3) + oval(x + 44, bf - 27, x + 52, bf - 19, PAPER, INK, 2) + legs(scene, px, bf, walking);
}

function desk(scene, width) {
  const left = Math.floor(width * 0.40), right = Math.floor(width * 0.75), top = FLOOR - 18;
  let s = librarian(scene, right + 20, "stamp") + line(left, top, right, top, INK, 3) + line(left + 10, top, left + 7, FLOOR, INK, 2) +
    line(right - 8, top, right - 5, FLOOR, INK, 2) + rect(right - 34, top - 7, right - 23, top, PAPER, INK, 1);
  for (let i = 0; i < 3; i++) {
    const travel = (scene.frame * 8 + i * 145) % (width + 120), x = width + 15 - travel, y = top - 12 - (i % 2) * 3;
    s += poly([x, y, x + 25, y + 1, x + 23, y + 12, x - 1, y + 11], PAPER, INK, 1) + line(x + 4, y + 4, x + 19, y + 5, PENCIL, 1) + line(x + 4, y + 7, x + 16, y + 8, PENCIL, 1);
  }
  return s;
}

export const LABELS = { catalogue: "CATALOGUE CART", load: "ONE VERY IMPORTANT BOOK", import: "INDEXING DESK", idle: "THE READING ROOM" };

/** The complete drawing for the scene's current frame, `width` pixels wide. */
export function drawScene(scene, width, label) {
  let s = rect(0, 0, width, HEIGHT, PAPER, PAPER) + line(8, FLOOR, width - 8, FLOOR, INK, 1);
  for (let x = 18; x < width; x += 58) s += line(x, FLOOR + 3, x + 9, FLOOR + 1, "#b8b8b8", 1);
  const top = (t, anchor = "end", x = width - 14) => text(x, 12, t, { anchor, color: PENCIL, size: 12 });
  const mode = scene.mode;
  if (mode === "catalogue") s += trolley(scene.position, scene.books) + librarian(scene, scene.position + 107, "push") + top(label || LABELS.catalogue);
  else if (mode === "load") s += bigBook(scene, scene.position) + top(label || LABELS.load);
  else if (mode === "import") s += desk(scene, width) + top(label || LABELS.import, "start", 13);
  else if (mode === "complete" || mode === "complete_still") {
    const cx = width * 0.31, wave = Math.floor(scene.frame / 3) % 2 === 0;
    const sx = width * 0.67, sy = FLOOR - 38;
    s += trolley(cx, 6) + librarian(scene, cx + 107, wave ? "wave" : "push") + rect(sx, sy, sx + 105, sy + 28, PAPER, INK, 2) +
      text(sx + 52, sy + 14, "INDEXED!", { anchor: "middle", size: 16, style: "normal", weight: "bold" }) + line(sx + 12, sy + 31, sx + 93, sy + 31, INK, 2) +
      top("ALL SHELVED \u2014 THANK YOU!");
  } else if (mode === "failure" || mode === "failure_still") {
    const cx = width * 0.34, bx = cx + 177, by = FLOOR - 30, bubble = Math.min(width - 210, cx + 240);
    s += trolley(cx, 3) + librarian(scene, cx + 107, "push") + poly([bx, by + 2, bx + 25, by - 5, bx + 31, by + 17, bx + 6, by + 23], PAPER, INK, 2) +
      line(bx + 11, by, bx + 17, by + 21, INK, 1) + oval(bubble, 8, bubble + 195, 38, PAPER, INK, 1) +
      poly([bubble + 20, 33, bubble + 30, 33, bubble + 22, 42], PAPER, INK, 1) + text(bubble + 97, 23, "Oops! A book escaped.", { anchor: "middle", size: 12 });
  } else {
    const x = width * 0.38;
    s += trolley(x, 3) + librarian(scene, x + 107, "wave") + top(LABELS.idle);
  }
  return s;
}
