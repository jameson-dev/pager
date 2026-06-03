let conf = {};

function renderCapcodes() {
  const tb = document.querySelector("#capcode-table tbody");
  tb.innerHTML = "";
  (conf.capcodes || []).forEach((c, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input value="${c.code ?? ""}" data-i="${i}" data-k="code"></td>
      <td><input value="${(c.label ?? "").replace(/"/g, "&quot;")}" data-i="${i}" data-k="label" placeholder="e.g. Station 1 Dispatch"></td>
      <td><input type="checkbox" ${c.print_enabled ? "checked" : ""} data-i="${i}" data-k="print_enabled">
          <input type="number" min="1" style="width:3.5rem" title="copies" value="${c.copies ?? 1}" data-i="${i}" data-k="copies"></td>
      <td><button data-del="${i}" class="danger">✕</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("input").forEach(inp => inp.addEventListener("input", e => {
    const { i, k } = e.target.dataset;
    if (k === "print_enabled") conf.capcodes[i][k] = e.target.checked;
    else if (k === "copies") conf.capcodes[i][k] = Number(e.target.value) || 1;
    else conf.capcodes[i][k] = e.target.value;
  }));
  tb.querySelectorAll("[data-del]").forEach(b =>
    b.addEventListener("click", () => { conf.capcodes.splice(+b.dataset.del, 1); renderCapcodes(); }));
}

function renderJobtypes() {
  const tb = document.querySelector("#jobtype-table tbody");
  tb.innerHTML = "";
  const entries = Object.entries(conf.jobtypes || {});
  entries.forEach(([name, v]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input value="${name}" data-orig="${name}" class="jt-name"></td>
      <td><input type="checkbox" ${v.print_enabled ? "checked" : ""} class="jt-print" data-name="${name}"></td>
      <td><button data-del="${name}" class="danger">✕</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll(".jt-name").forEach(inp => inp.addEventListener("change", e => {
    const orig = e.target.dataset.orig, val = e.target.value.trim();
    if (val && val !== orig) {
      conf.jobtypes[val] = conf.jobtypes[orig];
      delete conf.jobtypes[orig];
      renderJobtypes();
    }
  }));
  tb.querySelectorAll(".jt-print").forEach(inp => inp.addEventListener("change", e => {
    conf.jobtypes[e.target.dataset.name].print_enabled = e.target.checked;
  }));
  tb.querySelectorAll("[data-del]").forEach(b =>
    b.addEventListener("click", () => { delete conf.jobtypes[b.dataset.del]; renderJobtypes(); }));
}

async function renderPrinters() {
  const sel = document.getElementById("printer-select");
  const data = await api("GET", "/api/printers");
  sel.innerHTML = '<option value="">(system default)</option>';
  if (!data.cups_available) {
    sel.insertAdjacentHTML("beforeend", '<option disabled>CUPS not available on host</option>');
  }
  for (const p of data.installed) {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = `${p.name} ${p.location ? "— " + p.location : ""}`;
    sel.appendChild(o);
  }
  sel.value = conf.printer_name || "";
}

document.getElementById("discover-btn").addEventListener("click", async () => {
  const out = document.getElementById("discover-out");
  out.textContent = "Scanning…";
  try {
    const r = await api("GET", "/api/printers/discover");
    if (!r.devices.length) { out.textContent = "No network/USB devices found."; return; }
    out.innerHTML = "";
    for (const d of r.devices) {
      const div = document.createElement("div");
      div.className = "row";
      div.innerHTML = `<span>${d.make_model || d.info} <span class="muted">${d.uri}</span></span>`;
      const btn = document.createElement("button");
      btn.textContent = "Add as queue";
      btn.addEventListener("click", async () => {
        const name = prompt("Queue name for this printer:", (d.make_model || "printer").replace(/\W+/g, "_"));
        if (!name) return;
        const res = await api("POST", "/api/printers/add", { name, uri: d.uri });
        alert(res.ok ? "Added. Re-select it in the dropdown." : "Failed: " + res.message);
        renderPrinters();
      });
      div.appendChild(btn);
      out.appendChild(div);
    }
  } catch (e) { out.textContent = "Error: " + e.message; }
});

// --------------------------------------------------------------- templates
function renderTemplates() {
  conf.templates = conf.templates || [];
  const tb = document.querySelector("#template-table tbody");
  tb.innerHTML = "";
  conf.templates.forEach((t, i) => {
    const tr = document.createElement("tr");
    const locked = !!t.protected;
    // The PDF path is server-managed (uploads land in <config>/templates/), so
    // it's shown read-only as a filename rather than an editable path field. The
    // built-in default is fully locked and can't be re-uploaded or deleted.
    const fileName = t.path ? t.path.replace(/\\/g, "/").split("/").pop() : "";
    tr.innerHTML = `
      <td><input value="${(t.name ?? "").replace(/"/g, "&quot;")}" data-i="${i}" data-k="name" class="tpl-f"${locked ? " disabled" : ""}>${locked ? ' <span class="muted">built-in</span>' : ""}</td>
      <td>
        ${locked ? '<span class="muted">blank white page</span>'
                 : `<span class="tpl-file ${fileName ? "" : "muted"}" data-file="${i}">${fileName ? escapeHtml(fileName) : "no PDF yet"}</span>
                    <button data-upload="${i}" class="small">${fileName ? "Replace…" : "Upload PDF…"}</button>
                    <span class="tpl-upmsg" data-upmsg="${i}"></span>`}
      </td>
      <td>${locked ? '<span class="muted" title="The default template cannot be deleted">🔒</span>'
                   : `<button data-del="${i}" class="danger">✕</button>`}</td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll(".tpl-f").forEach(inp => inp.addEventListener("input", e => {
    const { i, k } = e.target.dataset;
    conf.templates[i][k] = e.target.value;
    if (k === "name") renderTemplateSelect();
  }));
  tb.querySelectorAll("[data-upload]").forEach(b =>
    b.addEventListener("click", () => uploadTemplate(+b.dataset.upload)));
  tb.querySelectorAll("[data-del]").forEach(b =>
    b.addEventListener("click", () => {
      if (conf.templates[+b.dataset.del]?.protected) return;  // safety: never delete the default
      conf.templates.splice(+b.dataset.del, 1); renderTemplates(); renderTemplateSelect();
    }));
  renderTemplateSelect();
}

// Open a file picker, upload the chosen PDF, and fill the row's path (and name).
function uploadTemplate(i) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/pdf,.pdf";
  input.addEventListener("change", async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    const msg = document.querySelector(`[data-upmsg="${i}"]`);
    if (msg) { msg.textContent = "Uploading…"; msg.className = "tpl-upmsg muted"; }
    try {
      const fd = new FormData();
      fd.append("file", f);
      const res = await fetch("/api/template/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      conf.templates[i].path = data.path;
      if (!conf.templates[i].name) conf.templates[i].name = data.name;
      renderTemplates();
      renderTemplateSelect();
      const m2 = document.querySelector(`[data-upmsg="${i}"]`);
      if (m2) { m2.textContent = "Uploaded ✓ — Save settings to apply"; m2.className = "tpl-upmsg ok-text"; }
    } catch (e) {
      if (msg) { msg.textContent = "Error: " + e.message; msg.className = "tpl-upmsg err-text"; }
    }
  });
  input.click();
}

function renderTemplateSelect() {
  const sel = document.getElementById("active-template");
  sel.innerHTML = "";
  (conf.templates || []).forEach(t => {
    const o = document.createElement("option");
    o.value = t.name; o.textContent = t.name || "(unnamed)";
    sel.appendChild(o);
  });
  sel.value = conf.active_template || (conf.templates[0] && conf.templates[0].name) || "";
}

// Upload straight into a fresh template row (name auto-filled from the file).
document.getElementById("upload-template").addEventListener("click", () => {
  conf.templates = conf.templates || [];
  conf.templates.push({ name: "", path: "" });
  renderTemplates();
  uploadTemplate(conf.templates.length - 1);
});

document.getElementById("run-cleanup").addEventListener("click", async () => {
  const msg = document.getElementById("cleanup-msg");
  msg.textContent = "Running…";
  try {
    const r = await api("POST", "/api/retention/run");
    msg.textContent = r.enabled
      ? `Deleted ${r.deleted_pdfs} PDFs, ${r.deleted_rows} rows (cutoff ${r.cutoff}).`
      : "Retention disabled (set days > 0).";
  } catch (e) { msg.textContent = "Error: " + e.message; }
});

document.getElementById("add-capcode").addEventListener("click", () => {
  conf.capcodes = conf.capcodes || [];
  conf.capcodes.push({ code: "", label: "", print_enabled: true });
  renderCapcodes();
});
document.getElementById("add-jobtype").addEventListener("click", () => {
  conf.jobtypes = conf.jobtypes || {};
  let n = "NEWTYPE", i = 1;
  while (conf.jobtypes[n]) n = "NEWTYPE" + (i++);
  conf.jobtypes[n] = { print_enabled: false };
  renderJobtypes();
});

// Build the config patch from the current form state (also used for dirty-check).
function settingsPatch() {
  return {
    global_print_enabled: document.getElementById("global-print").checked,
    printer_name: document.getElementById("printer-select").value,
    capcodes: conf.capcodes || [],
    jobtypes: conf.jobtypes || {},
    log_file: document.getElementById("log-file").value,
    ingest_source: document.getElementById("ingest-source").value,
    pagermon_db_path: document.getElementById("pagermon-db-path").value.trim(),
    pagermon_db_mapping: dbMappingOverride(),
    output_dir: document.getElementById("output-dir").value,
    database: document.getElementById("database").value.trim() || "data/jobs.db",
    print_max_attempts: Number(document.getElementById("print-max-attempts").value) || 5,
    print_retry_interval_seconds: Number(document.getElementById("print-retry-interval").value) || 60,
    templates: conf.templates || [],
    active_template: document.getElementById("active-template").value,
    alert_enabled_default: document.getElementById("alert-default").checked,
    watchdog_stale_seconds: Number(document.getElementById("watchdog-seconds").value) || 3600,
    retention_days: Number(document.getElementById("retention-days").value) || 0,
    retention_delete_pdf: document.getElementById("retention-pdf").checked,
    retention_delete_rows: document.getElementById("retention-rows").checked,
    timezone: document.getElementById("timezone").value.trim(),
    alert_webhook_url: document.getElementById("webhook-url").value.trim(),
    poppler_path: document.getElementById("poppler-path").value.trim(),
  };
}

let settingsSnapshot = "";
async function saveSettings() {
  conf = await api("PATCH", "/api/config", settingsPatch());
  settingsSnapshot = JSON.stringify(settingsPatch());
}

document.getElementById("save-settings").addEventListener("click", async () => {
  try {
    await saveSettings();
    document.getElementById("settings-msg").textContent = "Saved ✓ (some changes apply on restart)";
    setTimeout(() => document.getElementById("settings-msg").textContent = "", 4000);
  } catch (e) { document.getElementById("settings-msg").textContent = "Error: " + e.message; }
});

// Import a settings bundle (config + rules + layout) from a JSON file.
document.getElementById("import-settings").addEventListener("click", () => {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.addEventListener("change", async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    const msg = document.getElementById("backup-msg");
    if (!confirm("Import will overwrite your current config, rules, and layout. Continue?")) return;
    try {
      const bundle = JSON.parse(await f.text());
      const r = await api("POST", "/api/settings/import", bundle);
      msg.textContent = "Imported: " + r.applied.join(", ") + " — reloading…";
      msg.className = "ok-text";
      setTimeout(() => location.reload(), 900);
    } catch (e) { msg.textContent = "Error: " + e.message; msg.className = "err-text"; }
  });
  input.click();
});

// --------------------------------------------------------------- poppler status
async function refreshPopplerState() {
  const el = document.getElementById("poppler-state");
  try {
    const s = await api("GET", "/api/template/status");
    el.textContent = s.poppler_available
      ? "✓ poppler detected — PDF preview available"
      : "⚠ poppler not found — PDF preview image unavailable";
    el.className = s.poppler_available ? "ok-text" : "err-text";
  } catch (e) { el.textContent = ""; }
}

// --------------------------------------------------------------- access password
async function refreshAuthState() {
  try {
    const s = await api("GET", "/api/auth/status");
    document.getElementById("auth-state").textContent =
      s.enabled ? "Currently ENABLED." : "Currently disabled (site is open).";
    document.getElementById("current-pw-row").style.display = s.enabled ? "block" : "none";
  } catch (e) { /* ignore */ }
}

document.getElementById("save-password").addEventListener("click", async () => {
  const msg = document.getElementById("password-msg");
  const body = {
    new_password: document.getElementById("new-password").value,
    current_password: document.getElementById("current-password").value,
  };
  try {
    const r = await api("POST", "/api/auth/password", body);
    msg.textContent = r.enabled ? "Password set ✓" : "Password disabled ✓";
    msg.className = "ok-text";
    document.getElementById("new-password").value = "";
    document.getElementById("current-password").value = "";
    refreshAuthState();
  } catch (e) { msg.textContent = "Error: " + e.message; msg.className = "err-text"; }
});

// --------------------------------------------------------------- ingest source
function toggleIngestPanel() {
  const isDb = document.getElementById("ingest-source").value === "pagermon_db";
  document.getElementById("pagermon-db-panel").style.display = isDb ? "block" : "none";
}

function dbMappingOverride() {
  // Only send keys the user actually filled — blanks mean "use auto-detect".
  const m = {};
  const t = document.getElementById("map-table").value.trim();          if (t) m.table = t;
  const c = document.getElementById("map-capcode").value.trim();        if (c) m.capcode_col = c;
  const msg = document.getElementById("map-message").value.trim();      if (msg) m.message_col = msg;
  const ts = document.getElementById("map-timestamp").value.trim();     if (ts) m.timestamp_col = ts;
  const tk = document.getElementById("map-timestamp-kind").value;       if (tk) m.timestamp_kind = tk;
  return m;
}

function loadDbMappingOverride(m) {
  document.getElementById("map-table").value = m.table || "";
  document.getElementById("map-capcode").value = m.capcode_col || "";
  document.getElementById("map-message").value = m.message_col || "";
  document.getElementById("map-timestamp").value = m.timestamp_col || "";
  document.getElementById("map-timestamp-kind").value = m.timestamp_kind || "";
}

async function probeDb() {
  const msgEl = document.getElementById("probe-msg");
  const out = document.getElementById("probe-result");
  msgEl.textContent = "Probing…"; msgEl.className = "muted";
  try {
    const r = await api("POST", "/api/pagermon-db/probe", {
      path: document.getElementById("pagermon-db-path").value.trim(),
      mapping: dbMappingOverride(),
    });
    const m = r.mapping || {};
    if (!m.detected) {
      msgEl.textContent = "Not detected"; msgEl.className = "err-text";
      out.innerHTML = `<span class="err-text">${escapeHtml(m.note || "Could not read database.")}</span>`;
      return;
    }
    msgEl.textContent = "Detected ✓"; msgEl.className = "ok-text";
    const labelSrc = m.alias_fk_col
      ? `${m.capcodes_table}.${m.capcodes_label_col} (via ${m.alias_fk_col})`
      : (m.alias_text_col || "— none —");
    let html = `<b>Detected mapping</b><br>` +
      `table <code>${escapeHtml(m.table)}</code>, ` +
      `capcode <code>${escapeHtml(m.capcode_col)}</code>, ` +
      `message <code>${escapeHtml(m.message_col)}</code>, ` +
      `timestamp <code>${escapeHtml(m.timestamp_col || "-")}</code> ` +
      `(<code>${escapeHtml(m.timestamp_kind)}</code>), ` +
      `label ${escapeHtml(labelSrc)}`;
    if (r.sample && r.sample.length) {
      html += `<br><b>Recent rows:</b><ul style="margin:.3rem 0">` +
        r.sample.map(s => `<li><code>${escapeHtml(s.capcode)}</code>` +
          (s.alias ? ` (${escapeHtml(s.alias)})` : "") +
          ` — ${escapeHtml(s.message)} <span class="muted">${escapeHtml(s.received_at)}</span></li>`).join("") +
        `</ul>`;
    } else {
      html += `<br><span class="muted">No recent rows to sample.</span>`;
    }
    out.innerHTML = html;
  } catch (e) {
    msgEl.textContent = "Error"; msgEl.className = "err-text";
    out.textContent = e.message;
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

document.getElementById("ingest-source").addEventListener("change", toggleIngestPanel);
document.getElementById("probe-db").addEventListener("click", probeDb);

async function init() {
  conf = await api("GET", "/api/config");
  refreshAuthState();
  document.getElementById("global-print").checked = !!conf.global_print_enabled;
  document.getElementById("log-file").value = conf.log_file || "";
  document.getElementById("ingest-source").value = (conf.ingest_source === "pagermon_db") ? "pagermon_db" : "log";
  document.getElementById("pagermon-db-path").value = conf.pagermon_db_path || "";
  loadDbMappingOverride(conf.pagermon_db_mapping || {});
  toggleIngestPanel();
  document.getElementById("output-dir").value = conf.output_dir || "";
  // database / retry defaults come pre-filled from /api/config (which supplies
  // the effective default when the key is absent), so the form is never blank.
  document.getElementById("database").value = conf.database || "data/jobs.db";
  document.getElementById("print-max-attempts").value = conf.print_max_attempts ?? 5;
  document.getElementById("print-retry-interval").value = conf.print_retry_interval_seconds ?? 60;
  document.getElementById("alert-default").checked = conf.alert_enabled_default !== false;
  document.getElementById("watchdog-seconds").value = conf.watchdog_stale_seconds || 3600;
  document.getElementById("retention-days").value = conf.retention_days ?? 0;
  document.getElementById("retention-pdf").checked = conf.retention_delete_pdf !== false;
  document.getElementById("retention-rows").checked = conf.retention_delete_rows !== false;
  // Timezone is a fixed <select>; if the saved value isn't a preset option, add
  // it so the user's choice always stays shown (rather than snapping to blank).
  const tzSel = document.getElementById("timezone");
  const tz = conf.timezone || "";
  if (tz && ![...tzSel.options].some(o => o.value === tz)) {
    tzSel.add(new Option(tz, tz));
  }
  tzSel.value = tz;
  document.getElementById("webhook-url").value = conf.alert_webhook_url || "";
  document.getElementById("poppler-path").value = conf.poppler_path || "";
  refreshPopplerState();
  renderCapcodes();
  renderJobtypes();
  renderTemplates();
  await renderPrinters();

  // Unsaved-changes guard (compare the form's patch against the saved baseline).
  settingsSnapshot = JSON.stringify(settingsPatch());
  if (window.Dirty) {
    Dirty.setChecker(() => JSON.stringify(settingsPatch()) !== settingsSnapshot);
    Dirty.onSave(saveSettings);
  }
}
init();
