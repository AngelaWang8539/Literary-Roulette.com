// If the reading room cannot start (a broken file, a blocked script, a very old browser), say so instead of
// leaving "Opening the catalogue..." on screen forever. app.js sets window.__lrReady = true once it has started.
// On your own computer (localhost) the message also shows the real error, so you do not need the Console.
(function () {
  var local = /^(localhost|127\.0\.0\.1)$/.test(location.hostname);
  function fail(detail) {
    if (window.__lrReady) return;
    var box = document.getElementById("passage-text");
    if (!box) return;
    box.textContent = "The reading room did not load. Please reload the page. If it keeps happening, try again later." +
      (local && detail ? "\n\n[dev] " + detail : "");
  }
  window.addEventListener("error", function (e) {
    var t = e.target, file = e.filename || (t && t.src) || "";
    if (/\/static\/js\//.test(file)) fail((e.message || "failed to load") + " (" + file.split("/").pop() + (e.lineno ? ":" + e.lineno : "") + ")");
  }, true);
  setTimeout(function () { fail("no start signal after 6s"); }, 6000);
})();
