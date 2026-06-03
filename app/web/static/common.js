async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || res.statusText);
  return data;
}

function fmtAge(s) {
  if (s === null || s === undefined) return "never";
  if (s < 90) return s + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

// Lightweight transient toast for confirmations (save, copy, reprint…).
let _toastTimer = null;
function toast(msg) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}
window.toast = toast;

// ---------------------------------------------------------------- theme toggle
// light-dark() in CSS follows the OS by default; a manual override is stored in
// localStorage and applied as data-theme on <html>.
(function initTheme() {
  const KEY = "pager_theme";
  const root = document.documentElement;
  const saved = localStorage.getItem(KEY);
  if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    // Cycle: current effective theme -> the other one.
    const effective = root.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = effective === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem(KEY, next);
  });
})();

// Header print-status pill.
async function refreshPrintPill() {
  const el = document.getElementById("print-status");
  if (!el) return;
  try {
    const conf = await api("GET", "/api/config");
    const on = !!conf.global_print_enabled;
    el.textContent = on ? "Printing ON" : "Printing OFF";
    el.className = "pill " + (on ? "on" : "off");
  } catch (e) { el.textContent = "status?"; }
}

// Header feed-health pill (watchdog).
async function refreshHealthPill() {
  const el = document.getElementById("health-status");
  if (!el) return;
  try {
    const h = await api("GET", "/api/health");
    if (h.stale) {
      el.textContent = `feed STALE (last line ${fmtAge(h.last_line_age_seconds)})`;
      el.className = "pill off";
      el.title = "No decoder output recently — check the SDR / reader.sh / multimon-ng.";
    } else {
      el.textContent = `feed ok (page ${fmtAge(h.last_page_age_seconds)})`;
      el.className = "pill on";
      el.title = `${h.total_pages} pages, ${h.total_lines} lines since start.`;
    }
  } catch (e) { el.textContent = "feed: ?"; el.className = "pill"; }
}

// ---------------------------------------------------------------- unsaved-changes guard
// Pages with editable state register a save handler and call Dirty.mark()/clear().
// - Closing the tab / external navigation triggers the browser's native
//   "leave site?" prompt (its wording is fixed by the browser).
// - Clicking an in-app nav link shows a custom Save / Discard / Cancel dialog.
const Dirty = (function () {
  let dirty = false;
  let saveFn = null;     // async () => void
  let checker = null;    // optional () => bool, overrides the manual flag

  function mark() { dirty = true; }
  function clear() { dirty = false; }
  // A page can supply a checker that derives dirtiness from state comparison.
  function setChecker(fn) { checker = fn; }
  function isDirty() { return checker ? checker() : dirty; }
  function onSave(fn) { saveFn = fn; }

  // Native prompt on tab close / reload / external navigation.
  window.addEventListener("beforeunload", e => {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";   // required for the browser to show its dialog
    return "";
  });

  // Custom dialog: returns "save" | "discard" | "cancel".
  function confirmDialog() {
    return new Promise(resolve => {
      const ov = document.createElement("div");
      ov.className = "dirty-overlay";
      ov.innerHTML = `
        <div class="dirty-modal">
          <h3>Unsaved changes</h3>
          <p>You have unsaved changes on this page. What would you like to do?</p>
          <div class="dirty-actions">
            <button class="primary" data-act="save">Save changes</button>
            <button class="danger" data-act="discard">Discard changes</button>
            <button data-act="cancel">Cancel</button>
          </div>
        </div>`;
      document.body.appendChild(ov);
      ov.addEventListener("click", e => {
        const act = e.target.dataset && e.target.dataset.act;
        if (!act) return;           // clicks inside the modal body
        ov.remove();
        resolve(act);
      });
    });
  }

  // Navigate to `href`, guarding unsaved changes first.
  async function guardedGo(href) {
    if (!isDirty()) { window.location.href = href; return; }
    const act = await confirmDialog();
    if (act === "cancel") return;
    if (act === "save") {
      if (saveFn) { try { await saveFn(); } catch (e) { alert("Save failed: " + e.message); return; } }
    }
    dirty = false;
    checker = null;   // suppress further prompts during this navigation
    window.location.href = href;
  }

  // Intercept the header nav links (and logout) for the custom flow.
  document.querySelectorAll("header nav a").forEach(a => {
    a.addEventListener("click", e => {
      if (!isDirty()) return;
      e.preventDefault();
      guardedGo(a.getAttribute("href"));
    });
  });

  return { mark, clear, isDirty, onSave, setChecker, guardedGo };
})();
window.Dirty = Dirty;

refreshPrintPill();
refreshHealthPill();
setInterval(refreshHealthPill, 15000);
setInterval(refreshPrintPill, 15000);

// Show the Sign-out link only when password auth is enabled.
(async function initAuthUI() {
  const link = document.getElementById("logout-link");
  if (!link) return;
  try {
    const s = await api("GET", "/api/auth/status");
    if (s.enabled) {
      link.style.display = "";
      link.addEventListener("click", async (e) => {
        e.preventDefault();
        // beforeunload will still warn on unsaved changes before this navigates.
        await api("POST", "/api/logout");
        location.href = "/login";
      });
    }
  } catch (e) { /* ignore */ }
})();
