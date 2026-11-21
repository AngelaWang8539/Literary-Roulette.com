// Optional PostHog hook, loaded after analytics-config.js. Does nothing without a key, honours Do Not Track / Global Privacy
// Control, and loads PostHog only when the browser is idle. Everything else on the site calls  window.lrTrack(name, props).
(function () {
  "use strict";
  var cfg = window.LR_ANALYTICS || {};
  var host = String(cfg.host || "https://us.i.posthog.com").replace(/\/+$/, "");
  var optedOut = navigator.doNotTrack === "1" || window.doNotTrack === "1" || navigator.globalPrivacyControl === true;
  var enabled = !!cfg.key && !optedOut;
  // Only these property names are ever sent: ids, counts and states. Never passage text, search text or file names.
  var SAFE = { language: 1, book_id: 1, kind: 1, mode: 1, state: 1, ok: 1, ms: 1, count: 1 };
  var waiting = [], loaded = false, started = false;

  function clean(props) {
    var out = {};
    for (var k in props || {}) {
      if (!SAFE[k]) continue;
      var v = props[k];
      if (typeof v === "string") v = v.slice(0, 80);
      if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") out[k] = v;
    }
    return out;
  }

  function start() {
    if (started || !enabled) return;
    started = true;
    // Same stub PostHog's own snippet installs: the library finds it, replays what was queued, and takes over.
    var ph = window.posthog = window.posthog || [];
    ph._i = ph._i || [];
    ph.__SV = 1;
    ph.init = function (key, config, name) { ph._i.push([key, config, name]); };
    var s = document.createElement("script");
    s.async = true;
    s.crossOrigin = "anonymous";
    s.src = host.replace(".i.posthog.com", "-assets.i.posthog.com") + "/static/array.js";
    s.onerror = function () { waiting.length = 0; enabled = false; };      // blocked or offline: stay silent
    document.head.appendChild(s);
    ph.init(cfg.key, {
      api_host: host, defaults: "2025-05-24", person_profiles: "identified_only",
      autocapture: false, disable_session_recording: true, disable_surveys: true,
      persistence: cfg.persist || "memory",
      loaded: function (p) { loaded = true; waiting.splice(0).forEach(function (e) { p.capture(e[0], e[1]); }); },
    });
  }

  window.lrTrack = function (name, props) {
    if (!enabled || !name) return;
    var e = [String(name).slice(0, 60), clean(props)];
    if (loaded && window.posthog && window.posthog.capture) window.posthog.capture(e[0], e[1]);
    else if (waiting.length < 50) waiting.push(e);
  };

  if (enabled) {
    if ("requestIdleCallback" in window) window.requestIdleCallback(start, { timeout: 3000 });
    else setTimeout(start, 1500);
  }
})();
