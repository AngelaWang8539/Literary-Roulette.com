(() => {
  const labels = { en: "English", fr: "Français", zh: "中文" };
  const box = document.createElement("label");
  box.setAttribute("aria-label", "Choose reading language");
  Object.assign(box.style, {
    position: "fixed", top: "12px", right: "16px", zIndex: "99999",
    display: "flex", alignItems: "center", gap: "7px", padding: "7px 10px",
    border: "1px solid #b9b3a8", borderRadius: "8px", background: "#fffdf8",
    color: "#29251f", font: "13px system-ui, sans-serif", boxShadow: "0 2px 8px #0002"
  });
  const caption = document.createElement("span");
  caption.textContent = "Language";
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Language");
  Object.assign(select.style, {
    border: "0", background: "transparent", color: "inherit", font: "inherit",
    cursor: "pointer", maxWidth: "155px", outlineOffset: "3px"
  });
  box.append(caption, select);
  document.body.appendChild(box);

  fetch("/api/languages", { headers: { Accept: "application/json" } })
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(data => {
      for (const item of data.languages || []) {
        const option = document.createElement("option");
        option.value = item.code;
        const label = labels[item.code] || item.label;
        option.textContent = `${label} · ${item.ready_books} ready`;
        select.appendChild(option);
      }
      select.value = data.active || "en";
    })
    .catch(() => { select.innerHTML = '<option value="en">English</option><option value="fr">Français</option><option value="zh">中文</option>'; });

  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      const response = await fetch("/api/language", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ language: select.value })
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || "Language change failed.");
      window.location.reload();
    } catch (error) {
      window.alert(error.message || "Could not change language.");
      select.disabled = false;
    }
  });
})();

// Dock the language selector into the page and hide the book counts
(function dockLanguageSwitcher() {
  const SELECTOR = [
  ".language-switch",                                    // after it has been docked once
  'label[aria-label="Choose reading language"]',         // English
  'label[aria-label="选择阅读语言"]',                     // 中文
  'label[aria-label="Choisir la langue de lecture"]',    // Français
].join(",");
  const COUNT = /\s*[·•\-–—|(\[]\s*\d+\s*ready\s*[)\]]?\s*$/i;
  function dock() {
    const box = document.querySelector(SELECTOR);
    const bar = document.querySelector(".language-bar");
    if (!box || !bar) return;
    if (!bar.contains(box)) {
      box.removeAttribute("style");
      box.classList.add("language-switch");
      bar.append(box);
    }
    for (const o of box.querySelectorAll("option")) {
      const clean = o.textContent.replace(COUNT, "");
      if (clean !== o.textContent) o.textContent = clean;
    }
  }
  dock();
  new MutationObserver(dock).observe(document.body, { childList: true, subtree: true, characterData: true });
})();