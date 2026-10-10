// ChemoTracker: small progressive enhancements. Every page works without this file.
(() => {
  "use strict";
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const loadedAt = Date.now();

  // Theme (automatic / light / dark), stored per device.
  const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const currentTheme = () => { try { return localStorage.getItem("ct-theme") || "auto"; } catch (e) { return "auto"; } };
  const effectiveTheme = () => {
    const t = currentTheme();
    return t === "auto" ? (darkQuery && darkQuery.matches ? "dark" : "light") : t;
  };
  const syncTheme = () => {
    const eff = effectiveTheme();
    // Browser bars (iPhone status bar, Android toolbar) follow the chosen theme, not only the device.
    const canvas = getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim();
    $$('meta[name="theme-color"]').forEach((m) => { if (currentTheme() === "auto") m.content = m.media.includes("dark") ? "#0c161a" : "#f2f5f4"; else if (canvas) m.content = canvas; });
    $$("[data-theme-toggle] [data-when]").forEach((el) => { el.hidden = el.dataset.when !== eff; });
    $$("[data-theme-pick]").forEach((input) => { input.checked = input.value === currentTheme(); });
  };
  const applyTheme = (t) => {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("ct-theme", t); } catch (e) {}
    syncTheme();
  };
  $$("[data-theme-pick]").forEach((input) => {
    input.addEventListener("change", () => input.checked && applyTheme(input.value));
  });
  $$("[data-theme-toggle]").forEach((btn) => {
    btn.hidden = false;
    btn.addEventListener("click", () => applyTheme(effectiveTheme() === "dark" ? "light" : "dark"));
  });
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener("change", syncTheme);
  syncTheme();

  // Confirm destructive actions.
  $$("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); });
  });
  $$("[data-confirm-btn]").forEach((btn) => {
    btn.addEventListener("click", (e) => { if (!confirm(btn.dataset.confirmBtn)) e.preventDefault(); });
  });

  // Stop double submits (two taps on "Taken").
  $$("form[method=post]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (form.dataset.sent) { e.preventDefault(); return; }
      if (!e.defaultPrevented) form.dataset.sent = "1";
      setTimeout(() => delete form.dataset.sent, 4000);
    });
  });

  // Medicine picker: search filter + regimen shortcuts.
  const filter = document.querySelector("[data-filter]");
  if (filter) {
    const empty = document.querySelector("[data-no-results]");
    filter.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      let shown = 0;
      $$("[data-group]").forEach((group) => {
        let any = false;
        $$(".pick", group).forEach((pick) => {
          const hit = !q || pick.dataset.text.includes(q) || pick.querySelector("input").checked;
          pick.hidden = !hit;
          if (hit) { any = true; shown++; }
        });
        group.hidden = !any;
      });
      if (empty) empty.hidden = shown > 0;
    });
  }
  // Regimen shortcuts toggle: tap to tick that plan's chemo medicines, tap again to untick them.
  $$("[data-js-only]").forEach((el) => { el.hidden = false; });
  const shortcuts = $$("[data-shortcut]");
  const boxFor = (k) => document.querySelector(`input[name="keys[]"][value="${k}"]`);
  const keysOf = (btn) => btn.dataset.shortcut.split(",").filter(boxFor);
  const pressed = (btn) => btn.getAttribute("aria-pressed") === "true";
  const refreshShortcuts = () => shortcuts.forEach((btn) => {
    const keys = keysOf(btn);
    btn.setAttribute("aria-pressed", String(keys.length > 0 && keys.every((k) => boxFor(k).checked)));
  });
  shortcuts.forEach((btn) => {
    btn.addEventListener("click", () => {
      const turnOn = !pressed(btn);
      const others = shortcuts.filter((b) => b !== btn && pressed(b)).flatMap(keysOf);
      keysOf(btn).forEach((k) => {
        const box = boxFor(k);
        if (turnOn) box.checked = true;
        else if (!others.includes(k) && !box.dataset.rxTicked) box.checked = false;
      });
      refreshShortcuts();
      const filterBox = document.querySelector("[data-filter]");
      if (filterBox && filterBox.value) filterBox.dispatchEvent(new Event("input"));
    });
  });
  document.addEventListener("change", (e) => { if (e.target.matches('input[name="keys[]"]')) refreshShortcuts(); });
  refreshShortcuts();

  // Schedule form: show times only for scheduled medicines; add/remove time rows.
  $$("form").forEach((form) => {
    const section = form.querySelector('[data-kind-section="scheduled"]');
    if (section) {
      const sync = () => {
        const picked = form.querySelector('input[name="kind"]:checked');
        section.hidden = !picked || picked.value !== "scheduled";
      };
      $$('input[name="kind"]', form).forEach((r) => r.addEventListener("change", sync));
      sync();
    }
    const list = form.querySelector("[data-times]");
    const add = form.querySelector("[data-add-time]");
    if (list && add) {
      add.addEventListener("click", () => {
        const row = list.querySelector(".time-row").cloneNode(true);
        row.querySelector("input").value = "";
        list.appendChild(row);
        row.querySelector("input").focus();
      });
      list.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-remove-time]");
        if (!btn) return;
        const rows = $$(".time-row", list);
        if (rows.length > 1) btn.closest(".time-row").remove();
        else rows[0].querySelector("input").value = "";
      });
    }
  });

  // Prescriptions: read pasted label text as you type and tick the matching medicines in the list.
  const rx = document.querySelector("[data-rx]");
  if (rx && window.fetch) {
    const input = rx.querySelector("[data-rx-input]");
    const out = rx.querySelector("[data-rx-preview]");
    const readBtn = rx.querySelector("[data-rx-read]");
    let timer = null, seq = 0;
    const syncPicker = () => {
      const keys = new Set($$("[data-rx-key]", out).filter((b) => b.checked && b.dataset.rxKey).map((b) => b.dataset.rxKey));
      $$('input[name="keys[]"]').forEach((box) => {
        const pick = box.closest(".pick");
        const badge = pick && pick.querySelector(".rx-from");
        if (keys.has(box.value)) {
          if (!box.dataset.rxTicked && !box.checked) box.dataset.rxTicked = "1";
          box.checked = true;
          if (pick && !badge) { const b = document.createElement("span"); b.className = "rx-from"; b.textContent = "From your prescription"; pick.querySelector(".n").after(b); }
        } else {
          if (box.dataset.rxTicked) { box.checked = false; delete box.dataset.rxTicked; }
          if (badge) badge.remove();
        }
      });
    };
    const read = async () => {
      const text = input.value.trim();
      const mine = ++seq;
      if (!text) { out.innerHTML = ""; syncPicker(); return; }
      out.innerHTML = '<p class="rx-loading">Reading…</p>';
      const body = new FormData();
      body.set("rx_text", input.value);
      try {
        const res = await fetch("/rx/preview", { method: "POST", body, credentials: "same-origin" });
        if (mine !== seq) return;
        out.innerHTML = res.ok ? await res.text() : "";
        syncPicker();
        refreshShortcuts();
      } catch (e) { out.innerHTML = ""; }
    };
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(read, 450); });
    out.addEventListener("change", (e) => { if (e.target.matches("[data-rx-key]")) { syncPicker(); refreshShortcuts(); } });
    if (readBtn) readBtn.addEventListener("click", (e) => { e.preventDefault(); clearTimeout(timer); read(); });
    if (out.children.length) syncPicker();
  }

  // Cycle page on phones: start with the sheet folded and the picked day centred in the strip.
  const strip = document.querySelector(".day-strip");
  if (strip) {
    const picked = strip.querySelector("[aria-current]");
    if (picked) strip.scrollLeft = picked.offsetLeft - strip.clientWidth / 2 + picked.clientWidth / 2;
    const sheet = document.querySelector(".sheet-toggle");
    if (sheet && window.matchMedia("(max-width: 959.98px)").matches) sheet.open = false;
  }

  // Temperature unit picker: show the fever line in the unit just chosen.
  $$("[data-unit]").forEach((radio) => radio.addEventListener("change", () => {
    const form = radio.form, field = form.querySelector("[data-fever]"), label = form.querySelector("[data-unit-label]");
    if (!field) return;
    const typed = parseFloat(field.value.replace(",", "."));
    const wasC = !isNaN(typed) && typed < 60;
    let f = isNaN(typed) ? parseFloat(field.dataset.feverF) : (wasC ? typed * 9 / 5 + 32 : typed);
    field.value = radio.value === "C" ? ((f - 32) * 5 / 9).toFixed(1) : f.toFixed(1);
    if (label) label.textContent = radio.value === "C" ? "°C" : "°F";
  }));

  // Setup: add another person row.
  $$("[data-add-person]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = btn.closest("[data-people]");
      const tpl = box.querySelector("[data-person-template]");
      const i = $$("[data-person-row]", box).length;
      const holder = document.createElement("div");
      holder.innerHTML = tpl.innerHTML.replaceAll("__i__", String(i));
      const row = holder.firstElementChild;
      btn.before(row);
      row.querySelector("input[type=text]").focus();
    });
  });

  // Count stepper.
  $$("[data-step]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = btn.parentElement.querySelector("input");
      const next = Math.max(0, Math.min(99, (parseInt(input.value, 10) || 0) + parseInt(btn.dataset.step, 10)));
      input.value = String(next);
    });
  });

  // Report: changing the cycle picker reloads the preview.
  $$("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => {
    if (el.value) { const f = el.form; $$('input[type="date"]', f).forEach((d) => (d.disabled = true)); f.submit(); }
  }));

  // As-needed countdown rings tick down without a reload.
  const C = 113.1;
  const fmt = (sec) => {
    const m = Math.max(0, Math.ceil(sec / 60));
    if (m >= 120) return `${Math.floor(m / 60)}h`;
    if (m >= 60) return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
    return `${m}m`;
  };
  const words = (sec) => {
    const m = Math.max(0, Math.ceil(sec / 60));
    const h = Math.floor(m / 60), r = m % 60;
    return "in " + (h && r ? `${h} hr ${r} min` : h ? `${h} hr` : `${r} min`);
  };
  const tick = () => {
    const elapsed = (Date.now() - loadedAt) / 1000;
    $$("svg.countdown[data-remaining]").forEach((svg) => {
      const left = parseFloat(svg.dataset.remaining) - elapsed;
      const total = parseFloat(svg.dataset.total) || 1;
      const bar = svg.querySelector(".bar");
      const label = svg.querySelector("[data-left]");
      const box = svg.closest(".prn-when");
      if (left <= 0) {
        svg.classList.add("ok");
        svg.removeAttribute("data-remaining");
        bar.setAttribute("stroke-dashoffset", "0");
        if (label) label.remove();
        const t = document.createElementNS("http://www.w3.org/2000/svg", "path");
        t.setAttribute("class", "tick"); t.setAttribute("d", "M15 22.5l4.5 4.5 9-10");
        svg.appendChild(t);
        if (box) {
          box.classList.add("ok");
          const s = box.querySelector("[data-status]"); if (s) s.textContent = "OK to take now";
          const w = box.querySelector("[data-wait]"); if (w) w.textContent = "";
          const btn = box.parentElement.querySelector("[data-prn-button]");
          if (btn) { btn.classList.remove("btn-secondary"); btn.classList.add("btn-primary"); }
        }
        return;
      }
      bar.setAttribute("stroke-dashoffset", (C * (left / total)).toFixed(2));
      if (label) label.textContent = fmt(left);
      const w = box && box.querySelector("[data-wait]"); if (w) w.textContent = words(left);
    });
  };
  if (document.querySelector("svg.countdown[data-remaining]")) setInterval(tick, 20000);

  // Coming back to the app after a while: refresh so "due" states are current.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - loadedAt > 3 * 60 * 1000 && document.querySelector(".today")) {
      const editing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      if (!editing) location.reload();
    }
  });
})();
