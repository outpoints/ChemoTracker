// ChemoTracker: small progressive enhancements. Every page works without this file.
(() => {
  "use strict";
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const loadedAt = Date.now();

  // Theme (device / light / night), stored per device.
  const applyTheme = (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem("ct-theme", t); } catch (e) {} };
  const currentTheme = () => { try { return localStorage.getItem("ct-theme") || "auto"; } catch (e) { return "auto"; } };
  $$("[data-theme-pick]").forEach((input) => {
    input.checked = input.value === currentTheme();
    input.addEventListener("change", () => input.checked && applyTheme(input.value));
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
  $$("[data-keys]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const keys = btn.dataset.keys.split(",");
      keys.forEach((k) => {
        const box = document.querySelector(`input[name="keys[]"][value="${k}"]`);
        if (box) box.checked = true;
      });
      btn.setAttribute("aria-pressed", "true");
      btn.textContent = `${btn.textContent.replace(/ ✓$/, "")} ✓`;
    });
  });

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
