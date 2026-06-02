const tbody = document.querySelector("#jobs tbody");

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ----------------------------------------------------------------- alert prefs
const ALERT_KEY = "pager_alert_enabled";
function alertEnabled() {
  const v = localStorage.getItem(ALERT_KEY);
  return v === null ? true : v === "1"; // overwritten by server default at init
}
function setAlertEnabled(on) { localStorage.setItem(ALERT_KEY, on ? "1" : "0"); }

// WebAudio beep (no asset file needed).
let audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.type = "square"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.15, audioCtx.currentTime);
    o.start();
    // two short pips
    o.frequency.setValueAtTime(660, audioCtx.currentTime + 0.18);
    g.gain.setValueAtTime(0.0001, audioCtx.currentTime + 0.36);
    o.stop(audioCtx.currentTime + 0.37);
  } catch (e) { /* autoplay may be blocked until user interacts */ }
}

// Friendly capcode alias (the configured label), falling back to the number.
function aliasFor(job) {
  const a = job.fields && job.fields.capcode_alias;
  return a && a !== job.capcode ? a : null;
}

function notify(job) {
  const name = aliasFor(job) || job.capcode;
  const title = `Page: ${name}${job.jobtype ? " (" + job.jobtype + ")" : ""}`;
  const body = job.message || "";
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, { body });
  }
}

// ----------------------------------------------------------------- filters
function filterParams() {
  const p = new URLSearchParams();
  const q = document.getElementById("f-q").value.trim();
  const cap = document.getElementById("f-capcode").value.trim();
  const jt = document.getElementById("f-jobtype").value.trim();
  const from = document.getElementById("f-from").value;
  const to = document.getElementById("f-to").value;
  const printed = document.getElementById("f-printed").value;
  if (q) p.set("q", q);
  if (cap) p.set("capcode", cap);
  if (jt) p.set("jobtype", jt);
  if (from) p.set("date_from", from);
  if (to) p.set("date_to", to);
  if (printed) p.set("printed", printed);
  return p;
}

// True when no filters are active — only then is it safe to live-prepend
// new SSE jobs (otherwise they might not match the current filter).
function filtersActive() { return [...filterParams().keys()].length > 0; }

// ----------------------------------------------------------------- jobs table
async function load() {
  const p = filterParams();
  p.set("limit", "200");
  const res = await api("GET", "/api/jobs?" + p.toString());
  const jobs = res.jobs || [];
  tbody.innerHTML = "";
  for (const j of jobs) tbody.appendChild(row(j));
  bindReprint();
  document.getElementById("job-count").textContent =
    `${jobs.length} shown of ${res.total} total`;
  // Keep the CSV export link in sync with the active filters.
  const csv = filterParams();
  document.getElementById("export-csv").href = "/api/jobs.csv" + (csv.toString() ? "?" + csv : "");
}

function row(j) {
  const tr = document.createElement("tr");
  if (j.is_test) tr.classList.add("test-row");
  const alias = aliasFor(j);
  const printed = j.printed
    ? '<span style="color:#4ade80">yes</span>'
    : (j.print_failed
        ? `<span style="color:#f87171" title="${esc(j.print_error)}">failed (${j.print_attempts})</span>`
        : "no");
  tr.innerHTML = `
    <td>${j.id}${j.is_test ? ' <span class="tag">TEST</span>' : ""}</td>
    <td>${esc(j.received_at)}</td>
    <td>${alias
        ? `${esc(alias)}<div class="capcode-num">${esc(j.capcode)}</div>`
        : esc(j.capcode)}</td>
    <td>${esc(j.jobtype || "")}</td>
    <td>${esc(j.message)}</td>
    <td>${printed}</td>
    <td>
      ${j.pdf_path ? `<a href="/api/jobs/${j.id}/pdf" target="_blank">PDF</a>` : ""}
      <button data-id="${j.id}" class="reprint small">Reprint</button>
    </td>`;
  return tr;
}

function bindReprint() {
  tbody.querySelectorAll(".reprint").forEach(b =>
    b.addEventListener("click", () => reprint(b.dataset.id)));
}

async function reprint(id) {
  try {
    const r = await api("POST", `/api/jobs/${id}/reprint`);
    if (!r.ok) alert("Failed: " + (r.error || "unknown"));
    load();
  } catch (e) { alert("Failed: " + e.message); }
}

// ----------------------------------------------------------------- failed banner
async function refreshFailBanner(count) {
  if (count === undefined) {
    const r = await api("GET", "/api/prints/failed");
    count = r.count;
  }
  const banner = document.getElementById("fail-banner");
  if (count > 0) {
    document.getElementById("fail-text").textContent =
      `${count} print job${count === 1 ? "" : "s"} failed and will be retried automatically.`;
    banner.style.display = "flex";
  } else {
    banner.style.display = "none";
  }
}

document.getElementById("retry-all").addEventListener("click", async () => {
  const r = await api("POST", "/api/prints/retry-all");
  await refreshFailBanner(r.remaining_failed);
  load();
});

// ----------------------------------------------------------------- SSE live stream
function connectSSE() {
  const pill = document.getElementById("live-pill");
  const es = new EventSource("/api/events");
  es.addEventListener("hello", () => { pill.textContent = "live"; pill.className = "pill on"; });
  es.addEventListener("new_job", e => {
    const { job } = JSON.parse(e.data);
    // With filters active, prepending could insert a non-matching row; reload
    // instead so the list stays consistent with the filter.
    if (filtersActive()) load();
    else { tbody.prepend(row(job)); bindReprint(); }
    if (alertEnabled()) { beep(); notify(job); }
  });
  es.addEventListener("print_status", e => refreshFailBanner(JSON.parse(e.data).failed));
  es.addEventListener("print_recovered", () => load());
  es.onerror = () => { pill.textContent = "live: reconnecting…"; pill.className = "pill off"; };
}

// ----------------------------------------------------------------- test page
document.getElementById("send-test").addEventListener("click", async () => {
  const capcode = document.getElementById("test-capcode").value.trim();
  const message = document.getElementById("test-message").value.trim();
  const msg = document.getElementById("test-msg");
  if (!capcode || !message) { msg.textContent = "Enter capcode and message."; return; }
  try {
    await api("POST", "/api/test-page", { capcode, message });
    msg.textContent = "Sent ✓ (appears in the list; prints if that capcode/type is enabled)";
  } catch (e) { msg.textContent = "Error: " + e.message; }
});

// ----------------------------------------------------------------- init
async function init() {
  // Seed alert default from server if the user hasn't set a local preference.
  if (localStorage.getItem(ALERT_KEY) === null) {
    try {
      const conf = await api("GET", "/api/config");
      setAlertEnabled(conf.alert_enabled_default !== false);
    } catch (e) { setAlertEnabled(true); }
  }
  const toggle = document.getElementById("alert-toggle");
  toggle.checked = alertEnabled();
  toggle.addEventListener("change", () => {
    setAlertEnabled(toggle.checked);
    if (toggle.checked && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  });
  document.getElementById("test-sound").addEventListener("click", beep);

  await load();
  await refreshFailBanner();
  connectSSE();
}
document.getElementById("refresh").addEventListener("click", load);

// Filter controls: debounce text inputs, immediate for selects/dates.
let filterTimer = null;
function scheduleFilter() { clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); }
["f-q", "f-capcode", "f-jobtype"].forEach(id =>
  document.getElementById(id).addEventListener("input", scheduleFilter));
["f-from", "f-to", "f-printed"].forEach(id =>
  document.getElementById(id).addEventListener("change", load));
document.getElementById("f-clear").addEventListener("click", () => {
  ["f-q", "f-capcode", "f-jobtype", "f-from", "f-to"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("f-printed").value = "";
  load();
});

init();
