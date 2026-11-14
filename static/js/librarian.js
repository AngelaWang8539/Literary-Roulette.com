// The librarian: the desktop app's stick figure, shown ONLY when something takes longer than one second.
// While visible it shows the real task: a caption, the real stage, and a progress bar with a real percentage.
//
//   const op = begin("catalogue", "Opening the catalogue");   // starts a 1-second timer
//   op.show();   // when the wait is certain (a cold book), don't wait for the 1-second timer
//   op.update("Fetching this edition", { mode: "load", percent: 42 });
//   op.done();   // quick tasks never showed anything; slow ones get the INDEXED! stamp
//   op.fail("Could not reach the site.");   // slow ones get the friendly dropped-book scene
//
// Modes (same names as the desktop app): "catalogue" cart, "load" big book, "import" indexing desk.
import { newScene, startScene, finishScene, tickScene, drawScene, TICK_MS, HEIGHT } from "./librarian-scene.js";

export const SHOW_AFTER_MS = 1000;
const DONE_MS = 1500, FAIL_MS = 4500, MIN_WIDTH = 560;

let root = null, els = null, timer = null, hideTimer = null, width = MIN_WIDTH, shownMode = null, terminal = false;
const scene = newScene(MIN_WIDTH);
const active = new Set();
const reduced = () => !!(globalThis.matchMedia && globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches);
const translate = (s) => (globalThis.LR_I18N ? globalThis.LR_I18N.t(s) : s);

function ensure() {
  if (els) return true;
  root = globalThis.document && document.getElementById("librarian");
  if (!root) return false;
  root.innerHTML = '<p class="caption"></p><svg class="scene" role="img" aria-hidden="true" focusable="false"></svg>' +
    '<div class="meter"><div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div><span class="pct"></span></div>';
  els = { caption: root.querySelector(".caption"), svg: root.querySelector("svg"), meter: root.querySelector(".meter"),
          bar: root.querySelector(".bar"), fill: root.querySelector(".bar i"), pct: root.querySelector(".pct") };
  return true;
}

function measure() {
  const w = (root && root.clientWidth) || MIN_WIDTH;
  width = Math.max(MIN_WIDTH, Math.round(w));
  els.svg.setAttribute("viewBox", `0 0 ${width} ${HEIGHT}`);
}

function paint() {
  if (!els) return;
  els.svg.innerHTML = drawScene(scene, width);
}

function stopTicker() { clearInterval(timer); timer = null; }
function startTicker() {
  stopTicker();
  if (reduced()) { paint(); return; }                   // reduced motion: one still frame, no movement
  timer = setInterval(() => { tickScene(scene, width); paint(); }, TICK_MS);
}

function setMeter(percent) {
  if (typeof percent !== "number") { els.meter.hidden = true; return; }
  const p = Math.max(0, Math.min(100, Math.round(percent)));
  els.meter.hidden = false; els.fill.style.width = p + "%"; els.pct.textContent = p + "%";
  els.bar.setAttribute("aria-valuenow", String(p));
}

function show(op) {
  if (!ensure()) return;
  clearTimeout(hideTimer);
  root.hidden = false; measure();
  if (shownMode !== op.mode || terminal) {
    startScene(scene, op.mode, width);
    if (reduced()) { scene.position = width * 0.32; }
    shownMode = op.mode; terminal = false;
    paint();
    startTicker();
  }
  els.caption.textContent = translate(op.message || "");
  setMeter(op.percent);
  if (reduced()) paint();
}

function showTerminal(ok, message, ms) {
  if (!ensure()) return;
  clearTimeout(hideTimer);
  root.hidden = false; measure();
  finishScene(scene, ok); terminal = true; shownMode = null;
  paint();                                              // draw the stamp / dropped book at once, then animate
  startTicker();
  els.caption.textContent = translate(message);
  setMeter(ok ? 100 : undefined);
  if (reduced()) { scene.frame = 99; tickScene(scene, width); paint(); }
  hideTimer = setTimeout(() => { if (![...active].some((o) => o.visible)) hide(); }, ms);
}

function hide() { stopTicker(); clearTimeout(hideTimer); if (root) root.hidden = true; shownMode = null; terminal = false; }
const latestVisible = () => [...active].reverse().find((o) => o.visible);

export function begin(mode = "load", message = "") {
  const op = { mode, message, percent: undefined, visible: false, ended: false, timer: null };
  op.timer = setTimeout(() => { if (!op.ended) { op.visible = true; show(op); } }, SHOW_AFTER_MS);
  active.add(op);
  const finish = () => { op.ended = true; clearTimeout(op.timer); active.delete(op); };
  return {
    get visible() { return op.visible; },
    /** The wait is already known to be real (a cold book is being fetched): show the librarian now, not after 1 s. */
    show() { if (!op.ended && !op.visible) { clearTimeout(op.timer); op.visible = true; show(op); } },
    update(text, opts = {}) {
      if (text != null) op.message = text;
      if (opts.mode) op.mode = opts.mode;
      if ("percent" in opts) op.percent = opts.percent;
      if (op.visible && !op.ended) show(op);
    },
    done(text) {
      const was = op.visible; finish();
      if (!was) return;                                              // it never took long enough to deserve a drawing
      const other = latestVisible();
      if (other) return show(other);
      showTerminal(true, text || "All shelved \u2014 thank you!", DONE_MS);
    },
    fail(text) {
      const was = op.visible; finish();
      if (!was) return;
      showTerminal(false, text || "Something went wrong.", FAIL_MS);
    },
  };
}

export function isVisible() { return !!root && !root.hidden; }
