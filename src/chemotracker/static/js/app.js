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
    $$('meta[name="theme-color"]').forEach((m) => { if (currentTheme() === "auto") m.content = m.media.includes("dark") ? "#081114" : "#ebf0ef"; else if (canvas) m.content = canvas; });
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

  // One-time messages: close with the X or Escape. The greeting fades on its own unless you're on it.
  $$(".toast").forEach((toast) => {
    const close = () => {
      const hadFocus = toast.contains(document.activeElement);
      toast.remove();
      const main = document.getElementById("main");
      if (hadFocus && main) { main.setAttribute("tabindex", "-1"); main.focus({ preventScroll: true }); }
    };
    const x = toast.querySelector("[data-toast-close]");
    if (x) x.addEventListener("click", (e) => { e.preventDefault(); close(); });
    toast.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    if (toast.hasAttribute("data-toast-auto")) {
      let timer;
      const start = () => { clearTimeout(timer); timer = setTimeout(() => { toast.classList.add("is-leaving"); setTimeout(close, 260); }, 8000); };
      const stop = () => clearTimeout(timer);
      toast.addEventListener("mouseenter", stop); toast.addEventListener("mouseleave", start);
      toast.addEventListener("focusin", stop); toast.addEventListener("focusout", start);
      start();
    }
  });

  // Controls that need this script stay hidden without it.
  $$("[data-js-only]").forEach((el) => { el.hidden = false; });
  // Copy buttons (the reminder name).
  $$("[data-copy]").forEach((btn) => {
    const src = document.getElementById(btn.dataset.copy);
    if (!src || !navigator.clipboard) return;
    btn.hidden = false;
    btn.addEventListener("click", () => navigator.clipboard.writeText(src.textContent.trim()).then(() => {
      btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy"; }, 2000);
    }));
  });

  // Organizer lids on Today jump to their compartment and open it.
  $$(".lids a.lid").forEach((a) => a.addEventListener("click", () => {
    const target = document.getElementById((a.getAttribute("href") || "").slice(1));
    if (target && target.tagName === "DETAILS") target.open = true;
  }));

  // "Other time" clocks filled in with the time the page was drawn (data-now) follow the real time until someone
  // changes them, so accepting the clock on a page left open never back-dates a dose. A planned time (an earlier
  // day) has no data-now and stays. The time is the one drawn plus the time since, so it is the server's time even
  // when the phone is set to another zone. Past midnight the page is a day old: the clock is emptied to pick.
  const pad = (n) => String(n).padStart(2, "0");
  const clockNow = (input) => {
    const [h, m] = input.dataset.now.split(":").map(Number);
    const mins = h * 60 + m + Math.floor((Date.now() - loadedAt) / 60000);
    return mins < 24 * 60 ? `${pad(Math.floor(mins / 60))}:${pad(mins % 60)}` : "";
  };
  const syncClock = (input) => {
    if (input.dataset.typed || input === document.activeElement) return;
    const shown = "auto" in input.dataset ? input.dataset.auto : input.dataset.now;
    if (input.value !== shown) { input.dataset.typed = "1"; return; }  // changed by hand, or kept from a page before
    input.value = input.dataset.auto = clockNow(input);
  };
  const liveClocks = (root = document) => $$("input[type=time][data-now]", root);
  liveClocks().forEach((input) => ["input", "change"].forEach((ev) => input.addEventListener(ev, () => { input.dataset.typed = "1"; })));
  // Log a dose: choosing Other time sets an untouched clock to now.
  $$('input[name="ago"][value="pick"]').forEach((r) => r.addEventListener("change", () => liveClocks(r.form).forEach(syncClock)));
  // While a clock is on screen it keeps up (a panel left open). Hidden ones are set when they open.
  if (liveClocks().length) setInterval(() => liveClocks().forEach((i) => { if (i.offsetParent !== null) syncClock(i); }), 20000);

  // "When was it taken?" panels: one open at a time, and the panel is scrolled into view when it opens.
  $$("details[data-take]").forEach((d) => d.addEventListener("toggle", () => {
    if (!d.open) return;
    $$("details[data-take][open]").forEach((o) => { if (o !== d) o.open = false; });
    liveClocks(d).forEach(syncClock);
    const panel = d.querySelector(".take-panel");
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }));

  // "Log a dose": picking a medicine fills in its dose and what the label says. A clinic medicine on a day
  // it isn't given asks first, in a sheet from the bottom. Without scripts the server asks on a check page.
  const medSel = document.querySelector("select[data-med-select]");
  if (medSel) {
    const form = medSel.form;
    const dose = form.querySelector("[data-dose-input]");
    const facts = form.querySelector("[data-med-facts]");
    const ok = form.querySelector("[data-clinic-ok]");
    const sheet = document.getElementById("clinic-sheet");
    let doseTyped = false;
    if (dose) dose.addEventListener("input", () => { doseTyped = true; });
    const fill = () => {
      const o = medSel.selectedOptions[0];
      if (!o || !o.value) return;
      if (dose && !doseTyped) dose.value = o.dataset.dose || "";
      const line = [o.dataset.directions ? "Label: " + o.dataset.directions : "", o.dataset.status || ""].filter(Boolean).join(". ");
      if (facts) { facts.textContent = line; facts.hidden = !line; }
      if (ok) ok.value = "";
    };
    medSel.addEventListener("change", () => {
      fill();
      const note = medSel.selectedOptions[0] && medSel.selectedOptions[0].dataset.clinicNote;
      if (note && sheet && typeof sheet.showModal === "function") {
        sheet.querySelector("[data-sheet-text]").textContent = note;
        sheet.returnValue = "";
        sheet.showModal();
      }
    });
    if (sheet) {
      sheet.addEventListener("close", () => {
        if (sheet.returnValue === "go") { if (ok) ok.value = "1"; return; }
        medSel.value = "";  // "Pick another medicine", Escape, or a tap outside
        if (facts) facts.hidden = true;
        medSel.focus();
      });
      // A tap on the dimmed page closes it (the form fills the sheet, so only the backdrop hits the dialog).
      sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });
    }
  }

  // Optional choices ("How was the dose?"): tap the chosen answer again to clear it. Saving with none picked stores none.
  $$(".segmented[data-clearable]").forEach((group) => {
    let wasChecked = null;
    group.addEventListener("pointerdown", (e) => {
      const label = e.target.closest("label");
      const input = label && label.querySelector("input[type=radio]");
      wasChecked = input && input.checked ? input : null;
    });
    group.addEventListener("click", (e) => {
      if (e.target.matches("input[type=radio]") && e.target === wasChecked) e.target.checked = false;
      if (e.target.matches("input[type=radio]")) wasChecked = null;
    });
  });

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

  // As-needed countdown rings tick down without a reload. At zero the ring never says "OK to take now" by itself:
  // someone may have logged one on another phone since the page was drawn. It says "Checking…" and the page is
  // drawn again once nothing would be lost (nothing open, typed or waiting to be read), so the server answers.
  const C = 113.1;
  const minsLeft = (sec) => Math.max(0, Math.floor(sec / 60));  // rounded down, like the server draws them
  const fmt = (sec) => {
    const m = minsLeft(sec);
    if (m >= 120) return `${Math.floor(m / 60)}h`;
    if (m >= 60) return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
    return `${m}m`;
  };
  const words = (sec) => {
    const m = minsLeft(sec);
    if (m < 1) return "in less than a minute";
    const h = Math.floor(m / 60), r = m % 60;
    return "in " + (h && r ? `${h} hr ${r} min` : h ? `${h} hr` : `${r} min`);
  };
  const changed = (el) => {
    if (el.type === "hidden" || "now" in el.dataset) return false;  // live clocks follow the time on their own
    if (el.type === "radio" || el.type === "checkbox") return el.checked !== el.defaultChecked;
    if (el.tagName === "SELECT") {
      const first = Array.from(el.options).findIndex((o) => o.defaultSelected);
      return el.selectedIndex !== Math.max(first, 0);
    }
    return el.value !== el.defaultValue;
  };
  const busy = () => {
    const a = document.activeElement;
    return Boolean((a && /INPUT|TEXTAREA|SELECT/.test(a.tagName)) || $$("input, textarea, select").some(changed) ||
      document.querySelector("details[data-take][open], dialog[open], .toast:not([data-toast-auto])"));
  };
  let recheck = null, asking = false;
  const askServer = () => {
    if (asking || document.visibilityState !== "visible" || busy()) return;
    asking = true;  // a page that can't be reached stays as it is ("Checking…") instead of an error page
    fetch(location.href, { cache: "no-store", credentials: "same-origin" })
      .then((r) => { if (r.ok) location.reload(); else asking = false; })
      .catch(() => { asking = false; });
  };
  const tick = () => {
    const elapsed = (Date.now() - loadedAt) / 1000;
    $$("svg.countdown[data-remaining]").forEach((svg) => {
      const left = parseFloat(svg.dataset.remaining) - elapsed;
      const total = parseFloat(svg.dataset.total) || 1;
      const bar = svg.querySelector(".bar");
      const label = svg.querySelector("[data-left]");
      const box = svg.closest(".prn-row");
      const w = box && box.querySelector("[data-wait]");
      if (left <= 0) {
        svg.removeAttribute("data-remaining");
        bar.setAttribute("stroke-dashoffset", "0");
        if (label) label.textContent = "";
        const s = box && box.querySelector("[data-status]"); if (s) s.textContent = "Checking…";
        if (w) w.textContent = "";
        if (!recheck) recheck = setInterval(askServer, 5000);
        askServer();
        return;
      }
      bar.setAttribute("stroke-dashoffset", (C * (left / total)).toFixed(2));
      if (label) label.textContent = fmt(left);
      if (w) w.textContent = words(left);
    });
  };
  if (document.querySelector("svg.countdown[data-remaining]")) setInterval(tick, 20000);

  // Coming back to the app after a while: refresh so "due" states are current.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (recheck) askServer();
    liveClocks().forEach((i) => { if (i.offsetParent !== null) syncClock(i); });
    if (Date.now() - loadedAt > 3 * 60 * 1000 && document.querySelector(".today")) {
      const editing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      if (!editing) location.reload();
    }
  });
})();
