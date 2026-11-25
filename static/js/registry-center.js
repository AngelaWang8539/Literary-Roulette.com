(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let token = sessionStorage.getItem("lr-admin") || "";
  const note = (text, bad) => { $("note").textContent = text || ""; $("note").className = bad ? "bad" : ""; };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
  const pill = (v) => `<span class="pill ${esc(v)}">${esc(v)}</span>`;

  async function api(path, body) {
    const res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: { "X-LR-Admin": token, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.message || `Request failed (${res.status})`);
    return data;
  }

  async function summary() {
    const d = await api("/api/admin/registry/summary");
    const r = d.registry, w = d.worker, m = d.metrics.counts;
    const chip = (k, v) => `<span class="chip">${esc(k)} <b>${esc(v)}</b></span>`;
    $("summary").innerHTML = [
      chip("books", r.books.toLocaleString()), chip("drawable", Object.values(r.eligible).reduce((a, b) => a + b, 0).toLocaleString()),
      chip("queue", `${w.queued}+${w.active}`), chip("wait", d.queue_wait + " s"), chip("downloads paused", w.breaker_open ? "yes" : "no"),
      chip("ready-shelf fallbacks", m.fallback_served || 0), chip("percentile badges", Object.entries(d.percentile_ready).map(([k, v]) => k + (v ? " ✓" : " –")).join(" ")),
    ].join("");
  }

  async function books() {
    const q = new URLSearchParams({ q: $("q").value, language: $("f-language").value, pool: $("f-pool").value, rights: $("f-rights").value, limit: 50 });
    const d = await api("/api/admin/registry/books?" + q);
    $("books").innerHTML = "<tr><th>Book</th><th>Pool</th><th>Rights</th><th>Text</th><th>Actions</th></tr>" + d.books.map(row).join("") +
      `<tr><td colspan="5" class="muted">${d.books.length} of ${d.total.toLocaleString()}</td></tr>`;
  }

  function row(b) {
    const act = (a, label) => `<button data-act="${a}" data-id="${esc(b.book_id)}">${label}</button>`;
    return `<tr><td><b>${esc(b.title)}</b><br><span class="muted">${esc(b.author)} · ${esc(b.book_id)}</span></td>
      <td>${pill(b.pool_status)}<br><span class="muted">${esc(b.status_reason || "")}</span></td>
      <td>${pill(b.rights_status)}<br><span class="muted">${esc(b.rights_territory || "")} ${b.rights_expires ? "· " + esc(b.rights_expires) : ""}</span></td>
      <td>${pill(b.text_status)}<br><span class="muted">${esc(b.text_error || "")}</span></td>
      <td>${act("active", "Activate")} ${act("paused", "Pause")} ${act("archived", "Archive")}</td></tr>`;
  }

  async function imports() {
    const d = await api("/api/admin/registry/imports");
    $("imports").innerHTML = "<tr><th>Book</th><th>Status</th><th>Still needed before activation</th></tr>" + (d.books.map((b) =>
      `<tr><td><b>${esc(b.title)}</b><br><span class="muted">${esc(b.author)} · ${esc(b.book_id)} · revision ${esc(b.revision)}</span></td>
       <td>${pill(b.pool_status)} ${pill(b.rights_status)} ${pill(b.text_status)}</td><td>${b.blockers.length ? esc(b.blockers.join("; ")) : "nothing: ready to activate"}</td></tr>`).join("") ||
      '<tr><td colspan="3" class="muted">No author books yet.</td></tr>');
  }

  async function sources() {
    const d = await api("/api/admin/registry/sources");
    const rows = Object.entries(d.providers).map(([name, p]) => {
      const a = p.last_attempt || {}, g = p.last_good || {};
      return `<tr><td><b>${esc(name)}</b></td><td>${pill(a.state || "none")}<br><span class="muted">${esc(a.error || "")}</span></td>
        <td>${when(g.finished_at || g.started_at)}<br><span class="muted">${p.age_seconds == null ? "no good run yet" : Math.round(p.age_seconds / 60) + " min ago"}</span></td>
        <td>${esc(g.records ?? "")}</td><td>${g.completeness == null ? "" : Math.round(g.completeness * 100) + "%"}</td><td>${esc(a.retries ?? "")}</td><td>${esc(a.error_class || "")}</td></tr>`;
    });
    const guards = Object.values(d.guards).map((g) => `${esc(g.name)}: ${g.circuit_open ? "<b>circuit open</b> (" + g.circuit_seconds_left + " s)" : "closed"}, ${g.requests} requests, ${g.retries} retries`).join(" · ");
    $("sources").innerHTML = "<tr><th>Provider</th><th>Latest attempt</th><th>Last good run</th><th>Records</th><th>Complete</th><th>Retries</th><th>Error class</th></tr>" +
      rows.join("") + `<tr><td colspan="7" class="muted">${guards}. Gutendex census ${d.gutendex.census ? "on" : "off"}, every ${d.gutendex.interval_hours} h.</td></tr>`;
  }

  const views = { books, imports, sources };
  let tab = "books";
  async function show(name) {
    tab = name;
    for (const t of Object.keys(views)) { $("tab-" + t).hidden = t !== name; document.querySelector(`[data-tab="${t}"]`).classList.toggle("on", t === name); }
    note("");
    try { await Promise.all([summary(), views[name]()]); } catch (e) { note(e.message, true); }
  }

  $("tabs").addEventListener("click", (e) => { const t = e.target.closest("[data-tab]"); if (t) show(t.dataset.tab); });
  $("login").addEventListener("submit", (e) => { e.preventDefault(); token = $("token").value.trim(); sessionStorage.setItem("lr-admin", token); $("token").value = ""; show(tab); });
  $("filters").addEventListener("submit", (e) => { e.preventDefault(); books().catch((x) => note(x.message, true)); });
  document.addEventListener("click", async (e) => {
    const run = e.target.closest("[data-run]");
    if (run) { try { await api("/api/admin/registry/run/" + run.dataset.run, {}); note("Started " + run.dataset.run + ". Refresh in a moment."); } catch (x) { note(x.message, true); } return; }
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const status = b.dataset.act;
    let reason = "";
    if (status !== "active") { reason = window.prompt("Reason (shown to other admins):", "") ; if (reason === null) return; }
    try {
      const d = await api(`/api/admin/registry/books/${encodeURIComponent(b.dataset.id)}/pool`, { status, reason });
      note(`${d.book.title}: ${d.book.pool_status}` + (d.rebuild ? " (the index is being rebuilt so it can be drawn)" : ""));
      books(); summary();
    } catch (x) { note(x.message, true); }
  });
  if (token) show("books"); else note("Enter the admin token (LR_ADMIN_TOKEN) to unlock.");
})();
