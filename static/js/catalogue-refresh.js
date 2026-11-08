/* Librarian robot: unobtrusive status for the metadata refresh running on the server. */
(() => {
  "use strict";
  const API = "/api/catalogue/status";
  const POLL_MS = 4000;
  let previousState = null;
  let successUntil = 0;
  let timer = null;
  let banner;
  let message;
  let detail;

  function installStyles() {
    if (document.getElementById("lr-catalogue-refresh-style")) return;
    const style = document.createElement("style");
    style.id = "lr-catalogue-refresh-style";
    style.textContent = `
      .lr-catalogue-refresh{display:flex;align-items:center;gap:.8rem;margin:.65rem auto;padding:.7rem 1rem;max-width:68rem;border:1px solid #d9c69b;border-radius:14px;background:#fbf5e7;color:#234b3d;box-shadow:0 2px 8px #32281412;font:500 .92rem/1.4 system-ui,sans-serif}
      .lr-catalogue-refresh[hidden]{display:none}
      .lr-catalogue-refresh .lr-robot{font-size:1.8rem;line-height:1;display:inline-block;transform-origin:50% 85%}
      .lr-catalogue-refresh.is-running .lr-robot{animation:lr-librarian-bob 1.25s ease-in-out infinite}
      .lr-catalogue-refresh .lr-shelf{display:flex;gap:2px;align-items:flex-end;height:22px;min-width:37px}
      .lr-catalogue-refresh .lr-book{display:block;width:8px;border:1px solid #234b3d;border-radius:2px 2px 0 0;background:#d6b87c;animation:lr-shelve 1.1s ease-in-out infinite}
      .lr-catalogue-refresh .lr-book:nth-child(1){height:15px;animation-delay:0s}
      .lr-catalogue-refresh .lr-book:nth-child(2){height:20px;background:#98b5a2;animation-delay:.12s}
      .lr-catalogue-refresh .lr-book:nth-child(3){height:12px;background:#dcae87;animation-delay:.24s}
      .lr-catalogue-refresh .lr-copy{min-width:0;flex:1}
      .lr-catalogue-refresh strong{display:block;font-weight:750}
      .lr-catalogue-refresh small{display:block;color:#53665b;font-size:.82em;margin-top:.1rem}
      .lr-catalogue-refresh.is-failed{border-color:#cda68e;background:#fbf0e8;color:#74422e}
      @keyframes lr-librarian-bob{0%,100%{transform:translateY(0) rotate(-4deg)}50%{transform:translateY(-4px) rotate(4deg)}}
      @keyframes lr-shelve{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
      @media(prefers-reduced-motion:reduce){.lr-catalogue-refresh *{animation:none!important}}
      @media(max-width:540px){.lr-catalogue-refresh{margin:.45rem .5rem;padding:.6rem .7rem;gap:.55rem;font-size:.85rem}.lr-catalogue-refresh .lr-robot{font-size:1.5rem}}
    `;
    document.head.append(style);
  }

  function mount() {
    if (banner) return;
    installStyles();
    banner = document.createElement("aside");
    banner.className = "lr-catalogue-refresh";
    banner.hidden = true;
    banner.setAttribute("role", "status");
    banner.setAttribute("aria-live", "polite");
    banner.innerHTML = `<span class="lr-robot" aria-hidden="true">🤖</span><span class="lr-shelf" aria-hidden="true"><i class="lr-book"></i><i class="lr-book"></i><i class="lr-book"></i></span><span class="lr-copy"><strong></strong><small></small></span>`;
    message = banner.querySelector("strong");
    detail = banner.querySelector("small");
    // Keep the current website layout intact: this is a separate status strip.
    document.body.insertBefore(banner, document.body.firstChild);
  }

  function show(state, title, note, cls = "") {
    banner.className = `lr-catalogue-refresh ${cls}`.trim();
    banner.hidden = false;
    message.textContent = title;
    detail.textContent = note;
  }

  function hide() { if (banner) banner.hidden = true; }

  async function poll() {
    mount();
    try {
      const response = await fetch(API, { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const state = data.state || "missing";
      if (state === "running") {
        show(state, "The librarian robot is arranging the shelves…", data.has_snapshot
          ? "You can keep reading from the last saved catalogue while today's update runs."
          : "The first catalogue is being prepared. Your existing ready books remain available.", "is-running");
      } else if (state === "failed") {
        show(state, "The catalogue could not be refreshed this time.", data.has_snapshot
          ? "No books were removed. Literary Roulette is keeping its last verified catalogue."
          : "A catalogue is not available yet. Please try again later.", "is-failed");
      } else if (state === "success") {
        // Show the completion notice only if this page actually observed a
        // running refresh; a previous success is not a new event.
        if (previousState === "running") successUntil = Date.now() + 9000;
        if (Date.now() < successUntil) {
          const total = Object.values(data.counts || {}).reduce((a, b) => a + Number(b || 0), 0);
          show(state, "Fresh catalogue shelved!", `${total.toLocaleString()} eligible titles are ready across the three language shelves.`);
          // Do not trigger a spin automatically: the reader may already be
          // enjoying a passage while the background refresh completes.
        } else hide();
      } else if (state === "missing") {
        hide();
      } else {
        hide();
      }
      previousState = state;
      clearTimeout(timer);
      timer = setTimeout(poll, state === "running" ? POLL_MS : 12000);
    } catch (_error) {
      // Status polling is decorative; never interrupt the reading controls.
      clearTimeout(timer);
      timer = setTimeout(poll, 15000);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", poll, { once: true });
  else poll();
})();
