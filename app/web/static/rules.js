let rules = { rules: [] };
const tbody = document.querySelector("#rules-table tbody");
const testMsgEl = document.getElementById("test-msg");

// Stable colour per group index, reused by the preview highlight + legend.
const GROUP_COLORS = [
  "#3b82f6", "#16a34a", "#d97706", "#a855f7",
  "#0ea5e9", "#e11d48", "#65a30d", "#db2777",
];
function groupColor(i) { return GROUP_COLORS[i % GROUP_COLORS.length]; }

// Color for a field within a rule, kept consistent with the preview highlight:
// the preview colors named groups by their order, so we mirror that. Falls back
// to the capture-group number for groups that didn't match (not yet named).
function fieldColorFor(diagRule, fieldName, captureNumber) {
  if (diagRule && fieldName) {
    const idx = (diagRule.groups || []).findIndex(g => g.name === fieldName);
    if (idx >= 0) return groupColor(idx);
  }
  return groupColor((captureNumber || 1) - 1);
}

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// match_keywords may arrive as an array (saved config) or a raw string (typed in
// the input). Normalise both directions.
function keywordsToText(kw) {
  if (Array.isArray(kw)) return kw.join(", ");
  return kw || "";
}
function keywordsToArray(kw) {
  if (Array.isArray(kw)) return kw.filter(Boolean);
  return (kw || "").split(/[,\n]/).map(s => s.trim()).filter(Boolean);
}

// ----------------------------------------------------------------- rules table
function render() {
  tbody.innerHTML = "";
  rules.rules.forEach((r, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="reorder">
        <button data-up="${i}" title="Move up" ${i === 0 ? "disabled" : ""}>▲</button>
        <button data-down="${i}" title="Move down" ${i === rules.rules.length - 1 ? "disabled" : ""}>▼</button>
      </td>
      <td><input value="${esc(r.name)}" data-i="${i}" data-k="name"></td>
      <td><input class="kw-input" value="${esc(keywordsToText(r.match_keywords))}"
                 data-i="${i}" data-k="match_keywords" spellcheck="false"
                 placeholder="e.g. CFSRES, INC:">
          <div class="kw-state" data-kw="${i}"></div></td>
      <td><input class="pattern-input" style="width:100%" value="${esc(r.pattern)}"
                 data-i="${i}" data-k="pattern" spellcheck="false">
          <div class="pattern-err" data-err="${i}"></div></td>
      <td class="fields-cell" data-fields="${i}">
        <div class="group-assign" data-assign="${i}">
          <span class="muted">Type a pattern with ( ) groups…</span>
        </div>
      </td>
      <td><button data-del="${i}" class="danger">✕</button></td>`;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("input").forEach(inp =>
    inp.addEventListener("input", e => {
      const { i, k } = e.target.dataset;
      rules.rules[i][k] = e.target.value;
      scheduleTest();
    }));
  tbody.querySelectorAll("[data-del]").forEach(b =>
    b.addEventListener("click", () => { rules.rules.splice(+b.dataset.del, 1); render(); scheduleTest(); }));
  tbody.querySelectorAll("[data-up]").forEach(b =>
    b.addEventListener("click", () => move(+b.dataset.up, -1)));
  tbody.querySelectorAll("[data-down]").forEach(b =>
    b.addEventListener("click", () => move(+b.dataset.down, +1)));
}

function move(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= rules.rules.length) return;
  [rules.rules[i], rules.rules[j]] = [rules.rules[j], rules.rules[i]];
  render();
  scheduleTest(); // order changes which rule wins
}

// ----------------------------------------------------------------- live testing
let testTimer = null;
function scheduleTest() {
  clearTimeout(testTimer);
  testTimer = setTimeout(runTest, 200); // debounce while typing
}

async function runTest() {
  const message = testMsgEl.value;
  let diag;
  try {
    diag = await api("POST", "/api/rules/test", { message, rules: rules.rules });
  } catch (e) {
    document.getElementById("match-preview").textContent = "Error: " + e.message;
    return;
  }
  lastDiag = diag;
  renderPreview(diag);
  renderDiagnostics(diag);
  annotateRows(diag);
}

// --- highlighted match preview (the winning rule's captures, inline) ---
function renderPreview(diag) {
  const preview = document.getElementById("match-preview");
  const winner = document.getElementById("winner-label");
  const msg = diag.message || "";

  if (diag.matched_index === null || diag.matched_index === undefined) {
    winner.textContent = diag.selection_reason ? `— ${diag.selection_reason}` : "— no rule matched";
    preview.innerHTML = `<span class="nomatch-text">${esc(msg)}</span>`;
    return;
  }
  const rule = diag.rules[diag.matched_index];
  // Show both WHICH rule and WHY it was chosen (the selector reason).
  winner.textContent = `— matched by “${esc(rule.name)}”`
    + (diag.selection_reason ? ` · ${esc(diag.selection_reason)}` : "");

  // Build non-overlapping highlighted segments from the winning rule's groups.
  const groups = rule.groups
    .filter(g => g.start !== null && g.end !== null && g.end > g.start)
    .sort((a, b) => a.start - b.start);

  let html = "", cursor = 0;
  const colorByName = {};
  rule.groups.forEach((g, idx) => { colorByName[g.name] = groupColor(idx); });

  for (const g of groups) {
    if (g.start < cursor) continue; // skip overlaps (e.g. nested groups)
    html += esc(msg.slice(cursor, g.start));
    const c = colorByName[g.name];
    // Inline highlight with a small field-name label, so the colour key lives
    // in the preview itself (no separate legend needed).
    html += `<span class="cap" style="background:${c}22;border-color:${c};color:${c}"
                   title="${esc(g.name)}"><span class="cap-name" style="color:${c}">${esc(g.name)}</span>${esc(msg.slice(g.start, g.end))}</span>`;
    cursor = g.end;
  }
  html += esc(msg.slice(cursor));
  preview.innerHTML = html;
}

// --- per-rule diagnostics list ---
function renderDiagnostics(diag) {
  const ul = document.getElementById("diag-list");
  ul.innerHTML = "";
  diag.rules.forEach(r => {
    const li = document.createElement("li");
    const isWinner = r.index === diag.matched_index;
    let badge, detail;
    if (r.status === "error") {
      badge = `<span class="badge err">invalid regex</span>`;
      detail = `<span class="diag-detail err">${esc(r.error)}</span>`;
    } else if (r.status === "empty") {
      badge = `<span class="badge muted-badge">empty</span>`;
      detail = `<span class="diag-detail muted">no pattern</span>`;
    } else if (r.status === "skipped") {
      // Selector excluded this rule: its keywords weren't found in the message.
      badge = `<span class="badge skipped">skipped</span>`;
      detail = `<span class="diag-detail muted">no keyword in message (${esc((r.keywords || []).join(", ")) || "—"})</span>`;
    } else if (r.status === "match") {
      badge = isWinner
        ? `<span class="badge win">✓ matched (used)</span>`
        : `<span class="badge match-shadow">matches (shadowed)</span>`;
      const names = r.groups.map(g => g.name).join(", ") || "no named groups";
      detail = `<span class="diag-detail">${esc(names)}</span>`;
    } else {
      // nomatch: distinguish "keyword hit but regex didn't fit" (likely a
      // malformed page of this format) from a plain non-match.
      const hint = r.keyword_hit
        ? `<span class="diag-detail warn">keyword “${esc(r.keyword_hit)}” matched but pattern didn’t fit</span>`
        : "";
      badge = `<span class="badge no">no match</span>`;
      detail = hint;
    }
    // Small keyword tag so the selector is visible at a glance per rule.
    const kwTag = (r.keywords && r.keywords.length)
      ? `<span class="kw-chip" title="Applies when message contains one of these">🔑 ${esc(r.keywords.join(", "))}</span>`
      : `<span class="kw-chip muted" title="No keywords — considered for every message">any</span>`;
    li.className = "diag-item" + (isWinner ? " winner" : "");
    li.innerHTML = `<b>${esc(r.name)}</b> ${badge} ${kwTag} ${detail}`;
    ul.appendChild(li);
  });
}

// Common field names offered in the per-group dropdown. "message/capcode/date/
// time/datetime" are built-ins and don't need capturing, so they're not listed.
const COMMON_FIELDS = ["incident", "date", "time", "jobtype", "priority",
  "location", "mapref", "description", "units", "address", "crossstreet", "details"];

// --- annotate the editing rows: regex error + per-group field assignment ---
function annotateRows(diag) {
  diag.rules.forEach(r => {
    const errEl = tbody.querySelector(`[data-err="${r.index}"]`);
    const patInput = tbody.querySelector(`.pattern-input[data-i="${r.index}"]`);
    if (errEl) errEl.textContent = r.status === "error" ? r.error : "";
    if (patInput) patInput.classList.toggle("invalid", r.status === "error");
    // Keyword-state line: does the selector admit the current sample message?
    const kwEl = tbody.querySelector(`[data-kw="${r.index}"]`);
    if (kwEl) {
      if (!r.keywords || !r.keywords.length) {
        kwEl.textContent = "applies to all messages";
        kwEl.className = "kw-state muted";
      } else if (r.keyword_hit) {
        kwEl.textContent = `✓ “${r.keyword_hit}” in sample`;
        kwEl.className = "kw-state ok-text";
      } else {
        kwEl.textContent = "not in sample → skipped";
        kwEl.className = "kw-state warn";
      }
    }
    renderGroupAssign(r);
  });
}

// Render the "Group N → matched text → [field ▾]" controls for one rule.
function renderGroupAssign(r) {
  const box = tbody.querySelector(`[data-assign="${r.index}"]`);
  if (!box) return;
  const groups = r.capture_groups || [];
  if (groups.length === 0) {
    box.innerHTML = r.status === "error"
      ? `<span class="muted">Fix the pattern to assign fields.</span>`
      : `<span class="muted">Add ( ) around the text you want, then pick a field.</span>`;
    return;
  }
  box.innerHTML = groups.map(g => {
    const matched = g.value === null || g.value === undefined
      ? `<span class="g-nomatch">— no match —</span>`
      : `<span class="g-val">${esc(g.value)}</span>`;
    const current = g.field || "";
    const isCustom = current && !COMMON_FIELDS.includes(current);
    const opts = [`<option value="">(ignore)</option>`]
      .concat(COMMON_FIELDS.map(f =>
        `<option value="${f}" ${f === current ? "selected" : ""}>${f}</option>`))
      .concat([`<option value="__custom__" ${isCustom ? "selected" : ""}>custom…</option>`])
      .join("");
    const customInput = isCustom
      ? `<input class="g-custom" data-gi="${r.index}" data-gn="${g.number}"
                value="${esc(current)}" placeholder="field name">`
      : "";
    // Colored tag for the assigned field, matching the preview highlight color.
    const tag = current
      ? (() => {
          const c = fieldColorFor(r, current, g.number);
          return `<span class="g-tag" style="background:${c}22;border-color:${c};color:${c}">${esc(current)}</span>`;
        })()
      : "";
    return `<div class="g-row">
      <span class="g-num">${g.number}</span>
      ${matched}
      <span class="g-arrow">→</span>
      <select class="g-select" data-gi="${r.index}" data-gn="${g.number}">${opts}</select>
      ${customInput}
      ${tag}
    </div>`;
  }).join("");

  // Wire the dropdowns + custom inputs to the rule's groups map.
  box.querySelectorAll(".g-select").forEach(sel =>
    sel.addEventListener("change", e => {
      const { gi, gn } = e.target.dataset;
      assignGroup(+gi, gn, e.target.value === "__custom__" ? "" : e.target.value);
      if (e.target.value === "__custom__") {
        // Re-render so a custom text box appears.
        const rule = rules.rules[+gi];
        (rule.groups = rule.groups || {})[gn] = "";
        renderGroupAssign(findDiag(+gi));
      } else {
        scheduleTest();
      }
    }));
  box.querySelectorAll(".g-custom").forEach(inp =>
    inp.addEventListener("input", e => {
      assignGroup(+e.target.dataset.gi, e.target.dataset.gn, e.target.value.trim());
      scheduleTest();
    }));
}

function assignGroup(ruleIndex, groupNum, field) {
  const rule = rules.rules[ruleIndex];
  rule.groups = rule.groups || {};
  if (field) {
    // A field can only name ONE group — assigning it elsewhere would produce an
    // invalid "redefinition of group name" regex. Take it off any other group.
    for (const k of Object.keys(rule.groups)) {
      if (k !== String(groupNum) && rule.groups[k] === field) delete rule.groups[k];
    }
    rule.groups[groupNum] = field;
  } else {
    delete rule.groups[groupNum];
  }
}

// Keep the last diagnostics so a local re-render (custom box) has the group data.
let lastDiag = null;
function findDiag(ruleIndex) {
  return (lastDiag?.rules || []).find(x => x.index === ruleIndex) || { index: ruleIndex, capture_groups: [] };
}

// ----------------------------------------------------------------- buttons
document.getElementById("add-rule").addEventListener("click", () => {
  rules.rules.push({ name: "New rule", pattern: "" });
  render();
  scheduleTest();
});

// Canonical save payload (also used for dirty-state comparison).
function cleanRules() {
  return { rules: rules.rules.map(r => {
    const o = { name: r.name, pattern: r.pattern };
    const kws = keywordsToArray(r.match_keywords);
    if (kws.length) o.match_keywords = kws;
    if (r.jobtype_default) o.jobtype_default = r.jobtype_default;
    const groups = Object.fromEntries(
      Object.entries(r.groups || {}).filter(([, v]) => v));
    if (Object.keys(groups).length) o.groups = groups;
    return o;
  }) };
}

let rulesSnapshot = "";
async function saveRules() {
  await api("PUT", "/api/rules", cleanRules());
  rulesSnapshot = JSON.stringify(cleanRules());
}

document.getElementById("save-rules").addEventListener("click", async () => {
  try {
    await saveRules();
    const m = document.getElementById("rules-msg");
    m.textContent = "Saved ✓";
    setTimeout(() => m.textContent = "", 2000);
  } catch (e) { document.getElementById("rules-msg").textContent = "Error: " + e.message; }
});

testMsgEl.addEventListener("input", scheduleTest);

// ----------------------------------------------------------------- init
async function init() {
  rules = await api("GET", "/api/rules");
  if (!rules.rules) rules.rules = [];
  render();
  runTest();
  rulesSnapshot = JSON.stringify(cleanRules());
  if (window.Dirty) {
    Dirty.setChecker(() => JSON.stringify(cleanRules()) !== rulesSnapshot);
    Dirty.onSave(saveRules);
  }
}
init();
