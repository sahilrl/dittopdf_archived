/* dittopdf UI behaviour: filters, uploads, and the edit screen's per-field controls. */
(function () {
  "use strict";
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ---------------------------------------------------------------- uploads
  $$("form[data-upload]").forEach((form) => {
    const input = form.querySelector("input[type=file]");
    const zone = form.querySelector("[data-dropzone]");
    const mtime = form.querySelector("input[name=last_modified]");
    const record = () => {
      if (input.files && input.files[0] && mtime) mtime.value = String(input.files[0].lastModified || "");
    };
    input.addEventListener("change", record);
    if (zone) {
      ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => {
        e.preventDefault(); zone.classList.add("drag");
      }));
      ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("drag")));
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; record(); }
      });
    }
    form.addEventListener("submit", () => {
      record();
      const btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.textContent = "Inspecting…"; }
    });
  });

  // ---------------------------------------------------------------- long values
  function markOverflow(root = document) {
    $$(".val", root).forEach((el) => {
      if (el.dataset.checked || el.closest("details:not([open])")) return;
      el.dataset.checked = "1";
      if (el.scrollHeight > el.clientHeight + 4) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "link small more"; b.textContent = "show all";
        b.addEventListener("click", () => {
          const on = el.classList.toggle("expanded");
          b.textContent = on ? "show less" : "show all";
        });
        el.after(b);
      }
    });
  }
  markOverflow();
  document.addEventListener("toggle", (e) => { if (e.target.open) markOverflow(e.target); }, true);

  // ---------------------------------------------------------------- filters
  const bar = document.querySelector("[data-filters]");
  if (bar) {
    const text = bar.querySelector("[data-filter-text]");
    const absent = bar.querySelector("[data-filter-absent]");
    const diff = bar.querySelector("[data-filter-diff]");
    const ro = bar.querySelector("[data-filter-readonly]");
    const changed = bar.querySelector("[data-filter-changed]");
    const rows = $$("tr[data-row]");
    const apply = () => {
      const q = (text.value || "").trim().toLowerCase();
      rows.forEach((tr) => {
        let hide = false;
        if (absent && absent.checked && tr.dataset.present === "0") hide = true;
        if (diff && diff.checked && !["different", "only_original", "only_second", "unable_inspect"].includes(tr.dataset.status)) hide = true;
        if (ro && ro.checked && (tr.dataset.fixed === "1" || (tr.dataset.fixed === undefined && tr.dataset.cls === "readonly"))) hide = true;
        if (changed && changed.checked && !tr.classList.contains("changed")) hide = true;
        if (q && !tr.dataset.search.includes(q)) hide = true;
        tr.classList.toggle("hidden-by-filter", hide);
      });
      $$("details.grp").forEach((d) => {
        const any = d.querySelector("tr[data-row]:not(.hidden-by-filter)");
        d.classList.toggle("hidden-by-filter", !any);
        if (q && any) d.open = true;
      });
      $$("section.sec").forEach((s) => {
        s.classList.toggle("hidden-by-filter", !s.querySelector("details.grp:not(.hidden-by-filter)"));
      });
      // Sub-headings follow the visibility of the rows beneath them.
      $$("tr.subhead").forEach((h) => {
        let n = h.nextElementSibling, visible = false;
        while (n && !n.classList.contains("subhead")) {
          if (!n.classList.contains("hidden-by-filter")) { visible = true; break; }
          n = n.nextElementSibling;
        }
        h.classList.toggle("hidden-by-filter", !visible);
      });
    };
    [text, absent, diff, ro, changed].forEach((el) => el && el.addEventListener("input", apply));
    window.dittoApplyFilters = apply;
    apply();
    const ex = bar.querySelector("[data-expand-all]"), co = bar.querySelector("[data-collapse-all]");
    if (ex) ex.addEventListener("click", () => $$("details.grp").forEach((d) => (d.open = true)));
    if (co) co.addEventListener("click", () => $$("details.grp").forEach((d) => (d.open = false)));
  }

  // ---------------------------------------------------------------- edit screen
  const form = document.querySelector("[data-edit-form]");
  if (!form) return;

  const STATE = {
    copy: ["copy", "Copy"], keep: ["keep", "Keep second's"], remove: ["remove", "Remove"],
    custom: ["custom", "Manual override"],
  };

  function editorValue(ctrl) {
    const ed = ctrl.querySelector("[data-editor]");
    return ed ? ed.value.replace(/\r\n/g, "\n") : null;
  }

  function sync(ctrl) {
    const sel = ctrl.querySelector("[data-act]");
    const tr = ctrl.closest("tr");
    const editor = ctrl.querySelector(".editor");
    const act = sel.value;
    const [cls, text] = STATE[act] || ["copy", act];
    const badge = ctrl.querySelector("[data-state]");
    badge.className = "badge b-" + cls;
    badge.textContent = act === "copy" && ctrl.dataset.origPresent === "0" ? "Absent, like original" : text;
    if (editor) editor.hidden = !(act === "copy" || act === "custom");
    ctrl.querySelector("[data-keep-text]").hidden = act !== "keep";
    ctrl.querySelector("[data-remove-text]").hidden = act !== "remove";
    tr.classList.toggle("overridden", act === "custom");
    tr.classList.toggle("changed", act !== ctrl.dataset.default);
    updateCount();
  }

  function onEdit(ctrl) {
    const sel = ctrl.querySelector("[data-act]");
    const hasCustom = Array.from(sel.options).some((o) => o.value === "custom");
    if (!hasCustom) return;
    const v = editorValue(ctrl);
    const absent = ctrl.dataset.origPresent === "0";
    if ((absent && v !== "") || (!absent && v !== ctrl.dataset.orig)) sel.value = "custom";
    else if (sel.value === "custom") sel.value = "copy";
    sync(ctrl);
  }

  function resetEditor(ctrl) {
    const ed = ctrl.querySelector("[data-editor]");
    if (!ed) return;
    ed.value = ctrl.dataset.orig;
    ed.dispatchEvent(new Event("ditto-reset"));
  }

  const ctrls = $$("[data-ctrl]");
  ctrls.forEach((ctrl) => {
    const sel = ctrl.querySelector("[data-act]");
    sel.addEventListener("change", () => {
      if (sel.value === "copy") resetEditor(ctrl);
      if (sel.value === "custom") {
        const ed = ctrl.querySelector("[data-editor]");
        if (ed) setTimeout(() => ed.focus(), 0);
      }
      sync(ctrl);
    });
    const ed = ctrl.querySelector("[data-editor]");
    if (ed) ed.addEventListener("input", () => onEdit(ctrl));
    if (ed && ed.tagName === "SELECT") ed.addEventListener("change", () => onEdit(ctrl));
    setupDate(ctrl);
    setupXml(ctrl);
    setupJson(ctrl);
    sync(ctrl);
  });

  function updateCount() {
    const el = document.querySelector("[data-override-count]");
    if (!el) return;
    const n = $$("tr.changed").length;
    el.hidden = n === 0;
    el.textContent = n + " field" + (n === 1 ? "" : "s") + " changed";
  }

  const copyAll = document.querySelector("[data-copy-all]");
  if (copyAll) copyAll.addEventListener("click", () => {
    ctrls.forEach((ctrl) => {
      ctrl.querySelector("[data-act]").value = ctrl.dataset.default;
      resetEditor(ctrl);
      sync(ctrl);
    });
  });

  // Only changed fields are submitted; untouched ones use their default on the server.
  form.addEventListener("submit", () => {
    ctrls.forEach((ctrl) => {
      const sel = ctrl.querySelector("[data-act]");
      const unchanged = sel.value === ctrl.dataset.default &&
        (sel.value !== "custom" || editorValue(ctrl) === ctrl.dataset.orig);
      if (unchanged) {
        $$("[name]", ctrl).forEach((el) => (el.disabled = true));
      } else if (sel.value !== "custom") {
        const ed = ctrl.querySelector("[data-editor]");
        if (ed) ed.disabled = true;
      }
    });
    const btn = form.querySelector("button[type=submit]");
    if (btn) { btn.disabled = true; btn.textContent = "Writing PDF…"; }
  });
  // Re-enable controls if the user comes back with the browser's back button.
  window.addEventListener("pageshow", () => $$("[disabled]", form).forEach((el) => (el.disabled = false)));

  const addBtn = document.querySelector("[data-add-info-row]");
  if (addBtn) addBtn.addEventListener("click", () => {
    const body = document.querySelector("[data-add-info] tbody");
    const row = body.lastElementChild.cloneNode(true);
    $$("input", row).forEach((i) => (i.value = ""));
    body.appendChild(row);
  });

  const enc = document.querySelector("[data-encryption]");
  if (enc) {
    const fields = enc.querySelector("[data-enc-fields]");
    const upd = () => {
      const v = (enc.querySelector("input[name=opt_encryption]:checked") || {}).value;
      fields.hidden = v !== "original";
    };
    $$("input[name=opt_encryption]", enc).forEach((r) => r.addEventListener("change", upd));
    upd();
  }

  // ---------------------------------------------------------------- dates
  function setupDate(ctrl) {
    const picker = ctrl.querySelector("[data-date-picker]");
    if (!picker) return;
    const ed = ctrl.querySelector("[data-editor]");
    const re = /^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(.*)$/;
    const fill = () => {
      const m = re.exec(ed.value.trim());
      if (!m) return;
      picker.value = `${m[1]}-${m[2] || "01"}-${m[3] || "01"}T${m[4] || "00"}:${m[5] || "00"}:${m[6] || "00"}`;
    };
    fill();
    ed.addEventListener("ditto-reset", fill);
    picker.addEventListener("change", () => {
      const v = picker.value; // YYYY-MM-DDTHH:MM(:SS)
      if (!v) return;
      const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(v);
      if (!m) return;
      const old = re.exec(ed.value.trim());
      const tz = old && old[7] ? old[7] : "Z";
      ed.value = `D:${m[1]}${m[2]}${m[3]}${m[4]}${m[5]}${m[6] || "00"}${tz}`;
      onEdit(ctrl);
    });
  }

  // ---------------------------------------------------------------- XML
  function setupXml(ctrl) {
    const ta = ctrl.querySelector("[data-xml]");
    if (!ta) return;
    const status = ctrl.querySelector("[data-xml-status]");
    const check = () => {
      if (!ta.value.trim()) { status.textContent = "Empty: the output will have no XMP packet."; status.className = "xml-status"; return; }
      const doc = new DOMParser().parseFromString(ta.value.replace(/^﻿/, ""), "application/xml");
      const err = doc.getElementsByTagName("parsererror")[0];
      status.textContent = err ? "Not well-formed XML: " + err.textContent.split("\n")[0] : "Well-formed XML";
      status.className = "xml-status " + (err ? "bad" : "ok");
    };
    ta.addEventListener("input", check);
    ta.addEventListener("ditto-reset", check);
    check();
  }

  // ---------------------------------------------------------------- typed JSON (PDF objects)
  // Strings are "u:text" or "b:hex", names "/Name", references "N G R" (qpdf JSON conventions).
  function typeOf(v) {
    if (v === null) return "null";
    if (Array.isArray(v)) return "array";
    if (typeof v === "object") return "dict";
    if (typeof v === "boolean") return "bool";
    if (typeof v === "number") return "number";
    if (v.startsWith("/")) return "name";
    if (v.startsWith("u:")) return "text";
    if (v.startsWith("b:")) return "hex";
    if (/^\d+ \d+ R$/.test(v)) return "ref";
    return "invalid";
  }
  const DEFAULTS = { text: "u:", name: "/New", number: 0, bool: false, null: null, array: [], dict: {}, hex: "b:" };

  function setupJson(ctrl) {
    const ta = ctrl.querySelector("[data-json]");
    if (!ta) return;
    const status = ctrl.querySelector("[data-json-status]");
    const view = ctrl.querySelector("[data-json-tree-view]");
    const toggle = ctrl.querySelector("[data-json-tree]");
    let data;
    const validate = () => {
      try { data = JSON.parse(ta.value); status.textContent = ""; status.className = "xml-status"; return true; }
      catch (e) { status.textContent = "Invalid JSON: " + e.message; status.className = "xml-status bad"; return false; }
    };
    const write = () => {
      ta.value = JSON.stringify(data, null, 1);
      onEdit(ctrl);
    };
    const render = () => {
      if (view.hidden) return;
      view.textContent = "";
      if (!validate()) { view.textContent = "Fix the JSON to use the tree editor."; return; }
      view.appendChild(node(data, (v) => { data = v; write(); render(); }));
    };
    toggle.addEventListener("click", () => { view.hidden = !view.hidden; render(); });
    ta.addEventListener("input", () => { validate(); if (!view.hidden) render(); });
    ta.addEventListener("ditto-reset", () => { validate(); render(); });
    validate();

    function node(v, set) {
      const t = typeOf(v);
      if (t === "array" || t === "dict") {
        const wrap = document.createElement("span");
        const label = document.createElement("span");
        label.className = "t";
        label.textContent = t === "array" ? `array [${v.length}]` : `dictionary (${Object.keys(v).length})`;
        wrap.appendChild(label);
        const ul = document.createElement("ul");
        const entries = t === "array" ? v.map((x, i) => [i, x]) : Object.entries(v);
        entries.forEach(([k, child]) => {
          const li = document.createElement("li");
          const key = document.createElement("span");
          key.className = "k";
          key.textContent = (t === "array" ? `[${k}]` : k) + " ";
          li.appendChild(key);
          li.appendChild(node(child, (nv) => {
            if (t === "array") v[k] = nv; else v[k] = nv;
            set(v);
          }));
          const del = document.createElement("button");
          del.type = "button"; del.textContent = "✕"; del.title = "Remove";
          del.addEventListener("click", () => {
            if (t === "array") v.splice(k, 1); else delete v[k];
            set(v);
          });
          li.appendChild(document.createTextNode(" "));
          li.appendChild(del);
          ul.appendChild(li);
        });
        const add = document.createElement("li");
        const sel = document.createElement("select");
        Object.keys(DEFAULTS).forEach((k) => { const o = document.createElement("option"); o.value = k; o.textContent = k; sel.appendChild(o); });
        const btn = document.createElement("button");
        btn.type = "button"; btn.textContent = "+ add";
        btn.addEventListener("click", () => {
          const nv = JSON.parse(JSON.stringify(DEFAULTS[sel.value]));
          if (t === "array") v.push(nv);
          else {
            let key = prompt("Key name (starting with /)", "/NewKey");
            if (!key) return;
            if (!key.startsWith("/")) key = "/" + key;
            v[key] = nv;
          }
          set(v);
        });
        add.appendChild(sel); add.appendChild(btn);
        ul.appendChild(add);
        wrap.appendChild(ul);
        return wrap;
      }
      const span = document.createElement("span");
      const tag = document.createElement("span");
      tag.className = "t";
      tag.textContent = " " + t;
      if (t === "bool") {
        const s = document.createElement("select");
        ["true", "false"].forEach((x) => { const o = document.createElement("option"); o.value = x; o.textContent = x; s.appendChild(o); });
        s.value = String(v);
        s.addEventListener("change", () => set(s.value === "true"));
        span.appendChild(s);
      } else if (t === "null") {
        span.textContent = "null";
      } else {
        const inp = document.createElement("input");
        const prefix = { text: "u:", hex: "b:" }[t] || "";
        inp.value = t === "number" ? String(v) : String(v).slice(prefix.length);
        inp.size = Math.min(Math.max(inp.value.length + 2, 8), 60);
        inp.addEventListener("change", () => {
          if (t === "number") { const n = Number(inp.value); if (!Number.isNaN(n)) set(n); }
          else if (t === "name") set(inp.value.startsWith("/") ? inp.value : "/" + inp.value);
          else set(prefix + inp.value);
        });
        span.appendChild(inp);
      }
      span.appendChild(tag);
      return span;
    }
  }
})();
