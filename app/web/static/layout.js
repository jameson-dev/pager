let layout = null;
let activeName = null;
let availableFields = [];   // built-ins + fields the parsing rules produce

const img = document.getElementById("template-img");
const overlay = document.getElementById("overlay");
const shapeOverlay = document.getElementById("shape-overlay");
const wrap = document.getElementById("canvas-wrap");

// Shape editor state.
let activeShape = null;     // index into layout.shapes, or null
let drawTool = "select";    // "select" | "box" | "line"
let listFilter = "all";     // object-list filter: "all" | "fields" | "shapes"
let fieldsHidden = false;   // hide field boxes on the canvas (shape editing)

// A shape's display name (custom name, else "Box N" / "Line N").
function shapeName(s, i) {
  return s.name || (s.type === "rect" ? `Box ${i + 1}` : `Line ${i + 1}`);
}

// A representative pager message used to drive the live previews. The editor
// runs the real parsing rules against this to fill each field with its actual
// extracted value (see loadSampleValues), so boxes show true output.
const SAMPLE_MESSAGE =
  "MFS: *CFSRES INC:S0101 01/6/26 19:22 RESPOND TREE DOWN P1 : APPROX 500M EAST OF " +
  "JUBILEE HWY ROUNDABOUT MOUNT GAMBIER MAP:,MGB 122D 2210 ,== TREE PARTIALLY OVER " +
  "ROAD, WEST BOUND SIDE :MTG20_12:";

// Fallback samples for built-ins (capcode/datetime/message) and any field the
// rules don't produce. Rule-extracted values override these at runtime.
const SAMPLE = {
  date: "01/06/2026", time: "19:22", datetime: "01/06/2026 19:22",
  capcode: "1234567", capcode_alias: "Station 1 Dispatch",
  message: "full message text…",
  jobtype: "TREE DOWN", address: "12 MAIN ST", details: "STRUCTURE FIRE",
};

// Populated from the parsing rules run against SAMPLE_MESSAGE.
let sampleValues = {};

async function loadSampleValues() {
  try {
    const r = await api("POST", "/api/rules/test", { message: SAMPLE_MESSAGE });
    sampleValues = r.fields || {};
  } catch (e) { sampleValues = {}; }
}

// Real extracted value if a rule produced one, else a built-in/fallback sample.
function sampleFor(name) {
  if (sampleValues[name]) return sampleValues[name];
  return SAMPLE[name] || name.toUpperCase();
}

// True for a static custom-text field (carries its own literal `text`).
function isCustom(field) { return field && typeof field.text === "string"; }

// The text shown/printed for a field: literal text for custom fields, else the
// rule/built-in sample value.
function displayText(field) {
  return isCustom(field) ? (field.text || " ") : sampleFor(field.name);
}

// Scale factor between PDF points and on-screen pixels. Use the image width when
// a template is shown, otherwise the blank canvas width (no-template path).
function scale() {
  const rendered = img.clientWidth || wrap.clientWidth || 600;
  return rendered / layout.page_width;
}

// PDF point (bottom-left origin) -> pixel (top-left origin).
function ptToPx(field) {
  const s = scale();
  return { left: field.x * s, top: (layout.page_height - field.y) * s };
}
// pixel -> PDF point.
function pxToPt(leftPx, topPx) {
  const s = scale();
  return { x: Math.round(leftPx / s), y: Math.round(layout.page_height - topPx / s) };
}

// --- snapping (in PDF-point space) ---
const EDGE_SNAP_PT = 6; // snap to another field's x/y within this many points

function gridEnabled() { return document.getElementById("grid-toggle").checked; }
function gridSize() { return Math.max(2, Number(document.getElementById("grid-size").value) || 10); }

// Snap a dragged field's anchor. Other-field edges take priority over the grid.
// Returns {x, y, snapX, snapY} where snapX/snapY are the point value an axis
// snapped to a neighbouring field (for drawing the alignment guide), else null.
function snapPoint(field, x, y) {
  let snapX = null, snapY = null;
  for (const o of layout.fields) {
    if (o.name === field.name) continue;
    if (snapX === null && Math.abs(o.x - x) <= EDGE_SNAP_PT) { x = o.x; snapX = o.x; }
    if (snapY === null && Math.abs(o.y - y) <= EDGE_SNAP_PT) { y = o.y; snapY = o.y; }
  }
  if (gridEnabled()) {
    const g = gridSize();
    if (snapX === null) x = Math.round(x / g) * g;
    if (snapY === null) y = Math.round(y / g) * g;
  }
  x = Math.min(Math.max(x, 0), layout.page_width);
  y = Math.min(Math.max(y, 0), layout.page_height);
  return { x, y, snapX, snapY };
}

// Show alignment guides where the field snapped to another field's edge.
function drawGuides({ snapX, snapY }) {
  const s = scale();
  const gv = document.getElementById("guide-v");
  const gh = document.getElementById("guide-h");
  if (snapX !== null) { gv.style.left = (snapX * s) + "px"; gv.style.display = "block"; }
  else gv.style.display = "none";
  if (snapY !== null) { gh.style.top = ((layout.page_height - snapY) * s) + "px"; gh.style.display = "block"; }
  else gh.style.display = "none";
}
function hideGuides() {
  document.getElementById("guide-v").style.display = "none";
  document.getElementById("guide-h").style.display = "none";
}

// =====================================================================
// SHAPES — rectangles & lines for building a template from scratch.
// Stored in layout.shapes in PDF coordinates (bottom-left origin):
//   rect: {type:"rect", x, y, w, h, stroke, stroke_width, fill}
//   line: {type:"line", x1, y1, x2, y2, stroke, stroke_width}
// =====================================================================
function shapes() { return (layout.shapes = layout.shapes || []); }

// All snappable X and Y guide values in PDF points: page edges + center,
// field anchors, and every shape edge — except the shape being moved (skipIdx).
function snapTargets(skipIdx) {
  const xs = [0, layout.page_width, layout.page_width / 2];
  const ys = [0, layout.page_height, layout.page_height / 2];
  for (const f of layout.fields) { xs.push(f.x); ys.push(f.y); }
  shapes().forEach((s, i) => {
    if (i === skipIdx) return;
    if (s.type === "rect") {
      xs.push(s.x, s.x + s.w); ys.push(s.y, s.y + s.h);
    } else {
      xs.push(s.x1, s.x2); ys.push(s.y1, s.y2);
    }
  });
  return { xs, ys };
}

// Snap a single (x, y) point against the targets + grid. Returns snapped coords
// and which axis snapped to a target (for guides).
function snapXY(x, y, skipIdx) {
  const { xs, ys } = snapTargets(skipIdx);
  let snapX = null, snapY = null;
  for (const tx of xs) if (Math.abs(tx - x) <= EDGE_SNAP_PT) { x = tx; snapX = tx; break; }
  for (const ty of ys) if (Math.abs(ty - y) <= EDGE_SNAP_PT) { y = ty; snapY = ty; break; }
  if (gridEnabled()) {
    const g = gridSize();
    if (snapX === null) x = Math.round(x / g) * g;
    if (snapY === null) y = Math.round(y / g) * g;
  }
  x = Math.min(Math.max(x, 0), layout.page_width);
  y = Math.min(Math.max(y, 0), layout.page_height);
  return { x, y, snapX, snapY };
}

// PDF rect -> screen box {left, top, width, height} (top-left origin).
function rectToPx(s) {
  const sc = scale();
  return {
    left: s.x * sc,
    top: (layout.page_height - (s.y + s.h)) * sc,
    width: s.w * sc,
    height: s.h * sc,
  };
}

// Fill color as rgba() honoring the shape's fill_opacity (0..100, default 100).
function fillRgba(s) {
  const hex = (s.fill || "#dddddd").replace("#", "");
  const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  const a = Math.max(0, Math.min(100, s.fill_opacity == null ? 100 : s.fill_opacity)) / 100;
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

// ---- render all shapes ----
function renderShapes() {
  shapeOverlay.innerHTML = "";
  shapes().forEach((s, i) => {
    const el = document.createElement("div");
    el.className = "shape " + s.type + (i === activeShape ? " active" : "");
    el.dataset.idx = i;
    if (s.type === "rect") {
      const b = rectToPx(s);
      const bw = Math.max(1, (s.stroke_width || 1) * scale());
      // Center the border on the rect's true edge (offset by half the stroke and
      // grow by a full stroke) so two boxes sharing an edge overlap into ONE line
      // instead of two adjacent inside-borders that look doubled/thick. Matches
      // the PDF, which strokes centered on the path.
      const half = bw / 2;
      el.style.left = (b.left - half) + "px"; el.style.top = (b.top - half) + "px";
      el.style.width = (b.width + bw) + "px"; el.style.height = (b.height + bw) + "px";
      el.style.borderColor = s.stroke || "#000";
      el.style.borderWidth = bw + "px";
      // Fill must sit inside the centered border, so inset the background.
      el.style.background = s.fill ? fillRgba(s) : "transparent";
      el.style.backgroundClip = "padding-box";
      // Hit-testing: a filled box is clickable anywhere; an UNFILLED box is only
      // selectable by its border (its hollow centre is click-through so you can
      // reach fields/canvas behind it). Edge hit-strips provide the border target.
      if (s.fill) {
        el.addEventListener("mousedown", e => startShapeDrag(e, i));
      } else {
        el.style.pointerEvents = "none";
        addEdgeHitStrips(el, i);
      }
    } else {
      styleLineEl(el, s);
      el.addEventListener("mousedown", e => startShapeDrag(e, i));
    }
    shapeOverlay.appendChild(el);
    if (i === activeShape) addShapeHandles(el, s, i);
  });
}

// Add 4 transparent, clickable strips along a (hollow) rect's edges so it can be
// selected/dragged by its border while its interior stays click-through.
function addEdgeHitStrips(el, i) {
  const T = 8; // hit thickness (px) straddling the border
  const strips = [
    { cls: "top", css: { left: "0", right: "0", top: `-${T / 2}px`, height: `${T}px` } },
    { cls: "bottom", css: { left: "0", right: "0", bottom: `-${T / 2}px`, height: `${T}px` } },
    { cls: "left", css: { top: "0", bottom: "0", left: `-${T / 2}px`, width: `${T}px` } },
    { cls: "right", css: { top: "0", bottom: "0", right: `-${T / 2}px`, width: `${T}px` } },
  ];
  for (const st of strips) {
    const d = document.createElement("div");
    d.className = "shape-edge";
    Object.assign(d.style, { position: "absolute", pointerEvents: "auto", cursor: "move" }, st.css);
    d.addEventListener("mousedown", e => startShapeDrag(e, i));
    el.appendChild(d);
  }
}

// Position a line element (rotated div) between its two PDF endpoints.
function styleLineEl(el, s) {
  const sc = scale();
  const x1 = s.x1 * sc, y1 = (layout.page_height - s.y1) * sc;
  const x2 = s.x2 * sc, y2 = (layout.page_height - s.y2) * sc;
  const len = Math.hypot(x2 - x1, y2 - y1);
  const ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
  el.style.left = x1 + "px"; el.style.top = y1 + "px";
  el.style.width = len + "px";
  el.style.height = "0px";
  el.style.borderTopWidth = Math.max(1, (s.stroke_width || 1) * sc) + "px";
  el.style.borderTopColor = s.stroke || "#000";
  el.style.transform = `rotate(${ang}deg)`;
  el.style.transformOrigin = "left top";
}

// ---- selection ----
function selectShape(i) {
  deselect();              // clear any field selection
  activeShape = i;
  renderShapes();
  renderList();            // refresh active row in the object list
  showShapePanel(shapes()[i]);
}
function deselectShape() {
  if (activeShape === null) return;
  activeShape = null;
  document.getElementById("shape-edit").style.display = "none";
  renderShapes();
  renderList();
}

// ---- drawing a new shape (click-drag on the canvas) ----
function setTool(tool) {
  drawTool = tool;
  document.querySelectorAll(".draw-tools .tool").forEach(b =>
    b.classList.toggle("active", b.id === "tool-" + tool));
  document.getElementById("draw-hint").style.display = tool === "select" ? "none" : "block";
  wrap.style.cursor = tool === "select" ? "" : "crosshair";
}

// Convert a mouse event to PDF point coords within the canvas.
function evToPt(e) {
  const r = wrap.getBoundingClientRect();
  return pxToPt(e.clientX - r.left, e.clientY - r.top);
}

function startDraw(e) {
  if (drawTool === "select") return false;
  e.preventDefault();
  const start = (()=>{const _p=evToPt(e);return snapXY(_p.x,_p.y,-1);})();
  const isBox = drawTool === "box";
  const shape = isBox
    ? { type: "rect", x: start.x, y: start.y, w: 0, h: 0, stroke: "#000000", stroke_width: 1 }
    : { type: "line", x1: start.x, y1: start.y, x2: start.x, y2: start.y, stroke: "#000000", stroke_width: 1 };
  shapes().push(shape);
  const idx = shapes().length - 1;

  const move = ev => {
    const p = (()=>{const _p=evToPt(ev);return snapXY(_p.x,_p.y,idx);})();
    if (isBox) {
      shape.x = Math.min(start.x, p.x); shape.w = Math.abs(p.x - start.x);
      shape.y = Math.min(start.y, p.y); shape.h = Math.abs(p.y - start.y);
    } else {
      shape.x2 = p.x; shape.y2 = p.y;
    }
    drawGuides(p);
    renderShapes();
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    hideGuides();
    // Drop zero-size shapes (a click without a drag).
    if (isBox && shape.w < 2 && shape.h < 2) { shapes().splice(idx, 1); }
    else if (!isBox && Math.hypot(shape.x2 - shape.x1, shape.y2 - shape.y1) < 2) { shapes().splice(idx, 1); }
    else { activeShape = shapes().indexOf(shape); }
    setTool("select");
    renderShapes();
    if (activeShape >= 0 && activeShape !== null) showShapePanel(shapes()[activeShape]);
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
  return true;
}

// ---- move an existing shape ----
function startShapeDrag(e, i) {
  if (drawTool !== "select") return;
  e.preventDefault(); e.stopPropagation();
  if (e.target.classList.contains("shape-handle")) return; // handle drag is separate
  selectShape(i);
  const s = shapes()[i];
  const startPt = evToPt(e);
  const orig = JSON.parse(JSON.stringify(s));
  const move = ev => {
    const p = evToPt(ev);
    let dx = p.x - startPt.x, dy = p.y - startPt.y;
    // Snap the shape's primary anchor (rect bottom-left / line start) after move.
    if (s.type === "rect") {
      const sn = snapXY(orig.x + dx, orig.y + dy, i);
      s.x = sn.x; s.y = sn.y; s.w = orig.w; s.h = orig.h;
      drawGuides(sn);
    } else {
      const sn = snapXY(orig.x1 + dx, orig.y1 + dy, i);
      const adx = sn.x - orig.x1, ady = sn.y - orig.y1;
      s.x1 = orig.x1 + adx; s.y1 = orig.y1 + ady;
      s.x2 = orig.x2 + adx; s.y2 = orig.y2 + ady;
      drawGuides(sn);
    }
    renderShapes();
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    hideGuides();
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

// ---- resize handles ----
function addShapeHandles(el, s, i) {
  if (s.type === "rect") {
    [["nw", 0, 0], ["ne", 1, 0], ["sw", 0, 1], ["se", 1, 1]].forEach(([name, fx, fy]) => {
      const h = mkHandle("shape-handle " + name);
      el.appendChild(h);
      h.addEventListener("mousedown", e => startRectResize(e, i, fx, fy));
    });
  } else {
    ["a", "b"].forEach(end => {
      const h = mkHandle("shape-handle line-" + end);
      const sc = scale();
      const px = end === "a"
        ? { left: 0, top: 0 }
        : { left: Math.hypot((s.x2 - s.x1) * sc, (s.y2 - s.y1) * sc), top: 0 };
      h.style.left = px.left + "px"; h.style.top = px.top + "px";
      el.appendChild(h);
      h.addEventListener("mousedown", e => startLineResize(e, i, end));
    });
  }
}
function mkHandle(cls) { const d = document.createElement("div"); d.className = cls; return d; }

// Nudge the selected shape by (dx, dy) PDF points, clamped to the page.
function nudgeShape(dx, dy) {
  if (activeShape === null) return;
  const s = shapes()[activeShape];
  const clampX = v => Math.min(Math.max(v, 0), layout.page_width);
  const clampY = v => Math.min(Math.max(v, 0), layout.page_height);
  if (s.type === "rect") { s.x = clampX(s.x + dx); s.y = clampY(s.y + dy); }
  else { s.x1 = clampX(s.x1 + dx); s.x2 = clampX(s.x2 + dx); s.y1 = clampY(s.y1 + dy); s.y2 = clampY(s.y2 + dy); }
  renderShapes();
}

function startRectResize(e, i, fx, fy) {
  e.preventDefault(); e.stopPropagation();
  const s = shapes()[i];
  const ox = s.x, oy = s.y, ow = s.w, oh = s.h;
  // The corner being dragged in PDF space; the opposite corner stays fixed.
  const fixedX = ox + (fx ? 0 : ow);    // fx=1 means dragging right edge, left stays
  const fixedY = oy + (fy ? oh : 0);    // fy=1 means dragging bottom (lower y)
  const move = ev => {
    const p = (()=>{const _p=evToPt(ev);return snapXY(_p.x,_p.y,i);})();
    s.x = Math.min(fixedX, p.x); s.w = Math.abs(p.x - fixedX);
    s.y = Math.min(fixedY, p.y); s.h = Math.abs(p.y - fixedY);
    drawGuides(p);
    renderShapes();
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    hideGuides();
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

function startLineResize(e, i, end) {
  e.preventDefault(); e.stopPropagation();
  const s = shapes()[i];
  const move = ev => {
    const p = (()=>{const _p=evToPt(ev);return snapXY(_p.x,_p.y,i);})();
    if (end === "a") { s.x1 = p.x; s.y1 = p.y; } else { s.x2 = p.x; s.y2 = p.y; }
    drawGuides(p);
    renderShapes();
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    hideGuides();
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

// ---- style panel ----
function showShapePanel(s) {
  const panel = document.getElementById("shape-edit");
  panel.style.display = "block";
  document.getElementById("shape-kind").textContent = s.type === "rect" ? "(box)" : "(line)";
  document.getElementById("shape-name").value = s.name || "";
  document.getElementById("shape-name").placeholder = shapeName(s, activeShape);
  document.getElementById("shape-stroke").value = s.stroke || "#000000";
  document.getElementById("shape-stroke-w").value = s.stroke_width || 1;
  // Fill (and its opacity) only apply to rectangles.
  const isRect = s.type === "rect";
  document.getElementById("shape-fill-row").style.display = isRect ? "flex" : "none";
  document.getElementById("shape-fill-on").checked = !!s.fill;
  document.getElementById("shape-fill").value = s.fill || "#dddddd";
  const op = s.fill_opacity == null ? 100 : s.fill_opacity;
  document.getElementById("shape-opacity").value = op;
  document.getElementById("shape-opacity-val").textContent = op + "%";
  // Opacity slider only makes sense when the rect has a fill.
  document.getElementById("shape-opacity-row").style.display = (isRect && s.fill) ? "flex" : "none";
}

function bindShapePanel() {
  const cur = () => activeShape !== null ? shapes()[activeShape] : null;
  document.getElementById("shape-name").addEventListener("input", e => {
    const s = cur();
    if (s) { s.name = e.target.value; renderList(); }
  });
  document.getElementById("shape-stroke").addEventListener("input", e => {
    const s = cur(); if (s) { s.stroke = e.target.value; renderShapes(); }
  });
  document.getElementById("shape-stroke-w").addEventListener("input", e => {
    const s = cur(); if (s) { s.stroke_width = Math.max(0, Number(e.target.value) || 0); renderShapes(); }
  });
  document.getElementById("shape-fill").addEventListener("input", e => {
    const s = cur();
    if (s && document.getElementById("shape-fill-on").checked) { s.fill = e.target.value; renderShapes(); }
  });
  document.getElementById("shape-fill-on").addEventListener("change", e => {
    const s = cur();
    if (!s) return;
    s.fill = e.target.checked ? document.getElementById("shape-fill").value : null;
    document.getElementById("shape-opacity-row").style.display = (s.fill) ? "flex" : "none";
    renderShapes();
  });
  document.getElementById("shape-opacity").addEventListener("input", e => {
    const s = cur();
    if (!s) return;
    s.fill_opacity = Number(e.target.value);
    document.getElementById("shape-opacity-val").textContent = s.fill_opacity + "%";
    renderShapes();
  });
  document.getElementById("shape-delete").addEventListener("click", () => {
    if (activeShape === null) return;
    shapes().splice(activeShape, 1);
    deselectShape();
  });
}

// ---- tool buttons + page outline ----
function bindShapeTools() {
  // Box/Line toggle: clicking an active tool returns to the default select mode.
  document.getElementById("tool-box").addEventListener("click", () =>
    setTool(drawTool === "box" ? "select" : "box"));
  document.getElementById("tool-line").addEventListener("click", () =>
    setTool(drawTool === "line" ? "select" : "line"));
  document.getElementById("add-outline").addEventListener("click", () => {
    const m = 36; // 0.5" margin
    shapes().push({ type: "rect", x: m, y: m,
      w: layout.page_width - 2 * m, h: layout.page_height - 2 * m,
      stroke: "#000000", stroke_width: 1.5, name: "Page outline" });
    activeShape = shapes().length - 1;
    deselect();
    renderShapes();
    renderList();
    showShapePanel(shapes()[activeShape]);
  });
  bindShapePanel();

  // Object-list filter (All / Fields / Shapes).
  document.querySelectorAll(".obj-filter button").forEach(b =>
    b.addEventListener("click", () => {
      listFilter = b.dataset.filter;
      document.querySelectorAll(".obj-filter button").forEach(x =>
        x.classList.toggle("active", x === b));
      renderList();
    }));

  // Hide field boxes on the canvas while editing shapes.
  document.getElementById("hide-fields").addEventListener("change", e => {
    fieldsHidden = e.target.checked;
    renderBoxes();
  });
}

// field name -> its box element, so we can move/highlight a box in place
// without tearing down the whole overlay (which would orphan an in-flight drag).
const boxes = new Map();

// Rebuild the overlay from scratch. Only call this on structural changes
// (initial render, add/delete field, resize) — NOT during a drag or while
// editing coordinates, or the element being manipulated gets destroyed.
function renderBoxes() {
  overlay.innerHTML = "";
  boxes.clear();
  // Field overlay can be hidden to focus on shape editing.
  overlay.style.display = fieldsHidden ? "none" : "";
  for (const f of layout.fields) {
    const el = document.createElement("div");
    el.className = "field-box" + (f.name === activeName ? " active" : "");
    el.dataset.name = f.name;

    // Small field-type label, just above the rendered value. Custom-text fields
    // show a "TEXT" tag instead of a field name.
    const tag = document.createElement("div");
    tag.className = "fb-tag";
    tag.textContent = isCustom(f) ? "text" : f.name;

    // The value, rendered in the field's actual font/size (scaled to canvas).
    // The text lives in .fb-text as explicit per-line divs whose breaks come
    // from the server (reportlab metrics) so they match the printed PDF exactly.
    const val = document.createElement("div");
    val.className = "fb-val";
    const text = document.createElement("div");
    text.className = "fb-text";
    text.textContent = displayText(f);   // placeholder until lines arrive
    val.appendChild(text);

    el.appendChild(tag);
    el.appendChild(val);
    // Resize handles (shown only when the field is active): east = width
    // (max_width), south-east = font size (height).
    const hE = document.createElement("div");
    hE.className = "fb-handle e";
    const hSE = document.createElement("div");
    hSE.className = "fb-handle se";
    val.appendChild(hE);
    val.appendChild(hSE);
    makeResizable(hE, f, "width");
    makeResizable(hSE, f, "both");

    styleBox(el, f);
    positionBox(el, f);
    makeDraggable(el, f);
    el.addEventListener("click", e => { e.stopPropagation(); selectField(f.name); });
    overlay.appendChild(el);
    boxes.set(f.name, el);
  }
  renderList();
  if (activeName) positionToolbar();
  relayoutText();
  renderShapes();   // keep shapes in sync with field/scale re-renders
}

// Fetch server-computed wrapped lines (reportlab metrics) for every field and
// render them, so the editor's line breaks are identical to the printed PDF.
// Debounced; safe to call liberally (after render, resize, font/size change).
let relayoutTimer = null;
function relayoutText() {
  clearTimeout(relayoutTimer);
  relayoutTimer = setTimeout(doRelayoutText, 80);
}
async function doRelayoutText() {
  const fields = layout.fields;
  if (!fields.length) return;
  const items = fields.map(f => ({
    text: displayText(f),
    font: f.font || "Helvetica",
    size: f.size || 11,
    max_width: f.max_width || 0,
  }));
  let res;
  try {
    res = await api("POST", "/api/measure-text", items);
  } catch (e) { return; }   // leave the CSS placeholder text on failure
  fields.forEach((f, i) => {
    const el = boxes.get(f.name);
    if (!el) return;
    const textEl = el.querySelector(".fb-text");
    if (!textEl) return;
    const lines = (res[i] && res[i].lines) || [displayText(f)];
    // One div per line; no auto-wrap so breaks exactly match the server. The
    // first line wraps its text in a baseline ruler span so we can measure the
    // real baseline position and align it to the PDF anchor (no metric guessing).
    textEl.innerHTML = lines.map((ln, li) => {
      // A zero-size inline ruler after the first line's text sits on the
      // baseline; measuring it gives the exact baseline position.
      const ruler = li === 0 ? `<i class="fb-baseline"></i>` : "";
      return `<div class="fb-line">${esc(ln) || "&nbsp;"}${ruler}</div>`;
    }).join("");
    positionBox(el, f);   // re-anchor now that real text (and its baseline) exists
  });
}

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// --- font model: reportlab font name <-> {family, bold, italic} ---
// reportlab's Standard-14 naming is irregular, so map explicitly.
const FONT_FAMILIES = ["Helvetica", "Times", "Courier"];

function parseFont(name) {
  name = name || "Helvetica";
  const family = /Times/.test(name) ? "Times" : /Courier/.test(name) ? "Courier" : "Helvetica";
  const bold = /Bold/.test(name);
  const italic = /Italic|Oblique/.test(name);
  return { family, bold, italic };
}

function buildFont({ family, bold, italic }) {
  if (family === "Times") {
    if (bold && italic) return "Times-BoldItalic";
    if (bold) return "Times-Bold";
    if (italic) return "Times-Italic";
    return "Times-Roman";
  }
  // Helvetica / Courier share the same suffix scheme (-Oblique for italic).
  const suffix = (bold ? "Bold" : "") + (italic ? "Oblique" : "");
  return suffix ? `${family}-${suffix}` : family;
}

// Apply the field's font/size/style/wrap to the value text so it mirrors the PDF.
function styleBox(el, field) {
  const s = scale();
  const val = el.querySelector(".fb-val");
  const { family, bold, italic } = parseFont(field.font);
  const sizePt = field.size || 11;
  const px = Math.max(6, sizePt * s);
  val.style.fontSize = px + "px";
  val.style.fontFamily = cssFont(family);
  val.style.fontWeight = bold ? "700" : "400";
  val.style.fontStyle = italic ? "italic" : "normal";
  // Match pdfgen's leading (size * 1.2) exactly so multi-line spacing lines up.
  val.style.lineHeight = (sizePt * 1.2 * s) + "px";
  // The server pre-wraps the text into explicit lines, so the box itself never
  // soft-wraps. An explicit width (when max_width set) makes the box edge — and
  // the resize handle on it — represent the real wrap width.
  const maxw = field.max_width || 0;
  val.style.whiteSpace = "nowrap";
  val.style.maxWidth = "none";
  val.style.width = maxw > 0 ? (maxw * s) + "px" : "auto";
  // Text alignment WITHIN the box (left/center/right), against max_width. With
  // no max_width there's nothing to align against, so it's effectively left.
  const textEl = el.querySelector(".fb-text");
  if (textEl) textEl.style.textAlign = field.align || "left";
  // Box height is an INDEPENDENT layout property (does not change the font).
  // When set, the box is drawn at that fixed height; 0/unset = fit the text.
  const h = field.height || 0;
  val.style.height = h > 0 ? (h * s) + "px" : "auto";
}

// Map a reportlab font family to a CSS font-family.
function cssFont(family) {
  if (family === "Times") return "Georgia, 'Times New Roman', serif";
  if (family === "Courier") return "'Courier New', monospace";
  return "Helvetica, Arial, sans-serif";
}

// The 2px visual padding inside .fb-val (border->text gap). The box is shifted
// to compensate so the TEXT — not the padded box — lands on the PDF anchor.
const FB_PAD = 2;

// Per-family ascent as a fraction of the em (baseline -> top of text). These
// match the reportlab Standard-14 metrics closely so the editor baseline aligns
// with where pdfgen draws the text.
function ascentRatio(family) {
  if (family === "Times") return 0.683;
  if (family === "Courier") return 0.629;
  return 0.718; // Helvetica
}

// Place a box so the first line's text BASELINE sits at the field's (x, y) anchor
// (pdfgen draws from the baseline) and the left text edge sits at x.
//
// The baseline is found by MEASURING the rendered text (a zero-size ruler span
// with vertical-align:baseline whose bottom edge lies exactly on the baseline),
// rather than estimating from font metrics — so it's exact for any font/size.
function positionBox(el, field) {
  const { left, top } = ptToPx(field);  // top = baseline target in px
  // Provisional placement (also covers the case where no text/ruler exists yet:
  // fall back to an estimated baseline so the box isn't wildly off pre-measure).
  const s = scale();
  const sizePx = (field.size || 11) * s;
  const leadPx = sizePx * 1.2;
  const estBaseline = (leadPx - sizePx) / 2 + ascentRatio(parseFont(field.font).family) * sizePx;
  el.style.left = (left - FB_PAD) + "px";
  el.style.top = (top - estBaseline - FB_PAD) + "px";

  // Correct using the measured baseline if the ruler is present.
  const ruler = el.querySelector(".fb-baseline");
  if (ruler) {
    const wrapTop = wrap.getBoundingClientRect().top;
    const baselinePx = ruler.getBoundingClientRect().bottom - wrapTop; // baseline screen-y
    const drift = baselinePx - top;            // how far the baseline is from target
    el.style.top = (parseFloat(el.style.top) - drift) + "px";
  }
}

function makeDraggable(el, field) {
  let startX, startY, origLeft, origTop, dragging;
  el.addEventListener("mousedown", e => {
    e.preventDefault();
    // Select WITHOUT re-rendering: a full renderBoxes() here would replace `el`
    // mid-drag, leaving the move handler updating a detached element.
    selectField(field.name, { rerender: false });
    // Use the field's anchor in px (what el.style.left/top represent) rather than
    // getBoundingClientRect(), which returns the post-transform visual rect and
    // would make the box jump by its height on the first drag.
    const anchor = ptToPx(field);
    origLeft = anchor.left;
    origTop = anchor.top;
    startX = e.clientX; startY = e.clientY;
    dragging = false;
    const move = ev => {
      dragging = true;
      const s = scale();
      // Clamp the anchor to the page area so a field can't be dragged off the PDF.
      const maxLeft = layout.page_width * s;
      const maxTop = layout.page_height * s;
      const left = Math.min(Math.max(origLeft + (ev.clientX - startX), 0), maxLeft);
      const top = Math.min(Math.max(origTop + (ev.clientY - startY), 0), maxTop);
      // Snap in point-space (grid + other fields' edges), then back to px.
      const raw = pxToPt(left, top);
      const snapped = snapPoint(field, raw.x, raw.y);
      field.x = snapped.x; field.y = snapped.y;
      positionBox(el, field);   // applies the same baseline/ascent offset as render
      drawGuides(snapped);
      // Update the edit panel + list text live, but don't rebuild the overlay.
      if (field.name === activeName) fillEdit(field);
      updateListLabel(field);
      positionToolbar();
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      hideGuides();
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

// Resize a field by dragging a handle. `mode` = "width" (east edge -> max_width)
// or "both" (SE corner -> max_width + font size). Operates in PDF points/pt so
// the values match what the renderer uses.
function makeResizable(handle, field, mode) {
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    e.stopPropagation();           // don't start a box drag
    selectField(field.name, { rerender: false });
    const s = scale();
    const startX = e.clientX, startY = e.clientY;
    const startW = field.max_width || 0;
    const el = boxes.get(field.name);
    // Box height is an independent property (does NOT touch font size). Seed it
    // from the current rendered height so the first drag continues smoothly.
    const startH = field.height && field.height > 0
      ? field.height
      : (el ? Math.round(el.querySelector(".fb-val").offsetHeight / s) : 12);

    const move = ev => {
      if (mode === "width" || mode === "both") {
        // Pixel delta -> points; keep width on-page and non-negative.
        const wPt = Math.round(startW + (ev.clientX - startX) / s);
        field.max_width = Math.min(Math.max(wPt, 10), layout.page_width);
      }
      if (mode === "both") {
        // Independent box height in points (font size is untouched). The box is
        // top-anchored, so it grows downward toward the cursor automatically.
        const hPt = Math.round(startH + (ev.clientY - startY) / s);
        field.height = Math.min(Math.max(hPt, 6), layout.page_height);
      }
      if (el) styleBox(el, field);
      positionBox(el, field);
      fillEdit(field);
      positionToolbar();
      // Width changes re-wrap the text (height doesn't affect line breaks).
      if (mode === "width" || mode === "both") relayoutText();
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

// Object list with an All / Fields / Shapes filter; all rows selectable.
function renderList() {
  const ul = document.getElementById("object-list");
  ul.innerHTML = "";
  const showFields = listFilter !== "shapes";
  const showShapes = listFilter !== "fields";

  if (showFields) for (const f of layout.fields) {
    const li = document.createElement("li");
    li.dataset.name = f.name;
    li.className = "obj-row" + (activeShape === null && f.name === activeName ? " active" : "");
    const icon = isCustom(f) ? "🅣" : "🄵";
    const labelText = isCustom(f) ? `“${(f.text || "").slice(0, 18)}”` : f.name;
    li.innerHTML = `<span class="obj-icon">${icon}</span>
      <span class="field-label obj-label">${escAttr(labelText)}</span>`;
    li.querySelector(".obj-label").addEventListener("click", () => selectField(f.name));
    li.appendChild(mkDelBtn(() => deleteField(f.name)));
    ul.appendChild(li);
  }

  if (showShapes) shapes().forEach((s, i) => {
    const li = document.createElement("li");
    li.dataset.shape = i;
    li.className = "obj-row" + (i === activeShape ? " active" : "");
    const icon = s.type === "rect" ? "▭" : "／";
    li.innerHTML = `<span class="obj-icon shape-ico">${icon}</span>
      <span class="obj-label">${escAttr(shapeName(s, i))}</span>`;
    li.querySelector(".obj-label").addEventListener("click", () => selectShape(i));
    li.appendChild(mkDelBtn(() => { shapes().splice(i, 1); deselectShape(); renderBoxes(); }));
    ul.appendChild(li);
  });

  const nf = layout.fields.length, ns = shapes().length;
  const shown = (showFields ? nf : 0) + (showShapes ? ns : 0);
  document.getElementById("object-count").textContent =
    listFilter === "all" ? (nf + ns ? `(${nf + ns})` : "") : `(${shown})`;
}

function mkDelBtn(onClick) {
  const del = document.createElement("button");
  del.className = "field-del danger small";
  del.title = "Delete";
  del.textContent = "✕";
  del.addEventListener("click", e => { e.stopPropagation(); onClick(); });
  return del;
}
function escAttr(s) { return (s ?? "").toString().replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c])); }

// Update just one list row's label in place (used during drag/edit).
function updateListLabel(field) {
  const label = document.querySelector(`#object-list li[data-name="${field.name}"] .obj-label`);
  if (label && !isCustom(field)) label.textContent = field.name;
}

// Clear the current selection: hide the edit panel + floating toolbar.
function deselect() {
  if (!activeName) return;
  activeName = null;
  document.getElementById("field-edit").style.display = "none";
  document.getElementById("field-toolbar").style.display = "none";
  highlightActive();
}

// Remove a field (from the list's ✕ button).
function deleteField(name) {
  layout.fields = layout.fields.filter(x => x.name !== name);
  if (activeName === name) {
    activeName = null;
    document.getElementById("field-edit").style.display = "none";
    document.getElementById("field-toolbar").style.display = "none";
  }
  renderBoxes();
  refreshFieldPicker();
}

// Toggle the .active highlight on boxes + list rows without a full rebuild.
function highlightActive() {
  for (const [name, el] of boxes) el.classList.toggle("active", name === activeName);
  document.querySelectorAll("#object-list li").forEach(li =>
    li.classList.toggle("active", li.dataset.name === activeName && activeShape === null));
}

function selectField(name, { rerender = true } = {}) {
  // Selecting a field clears any shape selection (mutually exclusive).
  if (activeShape !== null) { activeShape = null; document.getElementById("shape-edit").style.display = "none"; renderShapes(); }
  // If fields were hidden, reveal them so the selection is visible.
  if (fieldsHidden) { fieldsHidden = false; document.getElementById("hide-fields").checked = false; }
  activeName = name;
  const f = layout.fields.find(x => x.name === name);
  fillEdit(f);
  document.getElementById("field-edit").style.display = "block";
  document.getElementById("edit-name").textContent = isCustom(f) ? "custom text" : name;
  // Full rebuild only when the structure may have changed (e.g. clicking a
  // list item). During a drag we pass rerender:false and just re-highlight.
  if (rerender) renderBoxes(); else highlightActive();
  positionToolbar();
}

function fillEdit(f) {
  // Custom-text fields expose an editable Text input.
  const textRow = document.getElementById("edit-text-row");
  if (isCustom(f)) {
    textRow.style.display = "block";
    document.getElementById("edit-text").value = f.text || "";
  } else {
    textRow.style.display = "none";
  }
  document.getElementById("edit-x").value = f.x;
  document.getElementById("edit-y").value = f.y;
  document.getElementById("edit-size").value = f.size || 11;
  document.getElementById("edit-maxw").value = f.max_width || 0;
  syncFontUI(f);
  syncAlignUI(f);
}

// Reflect a field's font family/bold/italic/size into BOTH the right panel and
// the floating toolbar, so they never drift.
function syncFontUI(f) {
  const { family, bold, italic } = parseFont(f.font);
  document.getElementById("edit-family").value = family;
  document.getElementById("edit-bold").classList.toggle("on", bold);
  document.getElementById("edit-italic").classList.toggle("on", italic);
  document.getElementById("tb-family").value = family;
  document.getElementById("tb-bold").classList.toggle("on", bold);
  document.getElementById("tb-italic").classList.toggle("on", italic);
  document.getElementById("tb-size").value = f.size || 11;
}

// Central font/size mutation: update the field, re-render its box, re-sync UIs.
function setFieldFont(part, value) {
  const f = layout.fields.find(x => x.name === activeName);
  if (!f) return;
  const cur = parseFont(f.font);
  if (part === "family") cur.family = value;
  if (part === "bold") cur.bold = value;
  if (part === "italic") cur.italic = value;
  f.font = buildFont(cur);
  const el = boxes.get(f.name);
  if (el) styleBox(el, f);
  syncFontUI(f);
  positionToolbar();
  relayoutText();   // font change alters line breaks
}

function setFieldSize(size) {
  const f = layout.fields.find(x => x.name === activeName);
  if (!f) return;
  f.size = Math.min(Math.max(size, 4), 200);
  const el = boxes.get(f.name);
  if (el) { styleBox(el, f); positionBox(el, f); }
  document.getElementById("edit-size").value = f.size;
  document.getElementById("tb-size").value = f.size;
  positionToolbar();
  relayoutText();   // size change alters line breaks
}

// Text alignment within the field box (left/center/right).
function setFieldAlign(align) {
  const f = layout.fields.find(x => x.name === activeName);
  if (!f) return;
  f.align = align;
  const el = boxes.get(f.name);
  if (el) { styleBox(el, f); positionBox(el, f); }
  syncAlignUI(f);
}

function syncAlignUI(f) {
  const cur = f.align || "left";
  document.querySelectorAll("#align-row [data-align]").forEach(b =>
    b.classList.toggle("on", b.dataset.align === cur));
}

function bindEdit() {
  const map = { "edit-x": "x", "edit-y": "y", "edit-size": "size", "edit-maxw": "max_width" };
  for (const [id, key] of Object.entries(map)) {
    document.getElementById(id).addEventListener("input", e => {
      const f = layout.fields.find(x => x.name === activeName);
      if (!f) return;
      let v = Number(e.target.value);
      // Keep x/y within the page so a typed value can't push a field off the PDF.
      if (key === "x") v = Math.min(Math.max(v, 0), layout.page_width);
      if (key === "y") v = Math.min(Math.max(v, 0), layout.page_height);
      f[key] = v;
      const el = boxes.get(f.name);
      if (key === "x" || key === "y") {
        if (el) positionBox(el, f);
        updateListLabel(f);
      } else if (el) {
        // size / max_width change the rendered look — restyle in place.
        styleBox(el, f);
        positionBox(el, f);
      }
      if (key === "size") document.getElementById("tb-size").value = f.size;
      positionToolbar();
      if (key === "size" || key === "max_width") relayoutText();
    });
  }

  // Custom-text content editing: update the literal text and re-wrap live.
  document.getElementById("edit-text").addEventListener("input", e => {
    const f = layout.fields.find(x => x.name === activeName);
    if (!f || !isCustom(f)) return;
    f.text = e.target.value;
    relayoutText();
  });

  // Right-panel font controls.
  document.getElementById("edit-family").addEventListener("change", e => setFieldFont("family", e.target.value));
  document.getElementById("edit-bold").addEventListener("click", () =>
    setFieldFont("bold", !parseFont(currentField()?.font).bold));
  document.getElementById("edit-italic").addEventListener("click", () =>
    setFieldFont("italic", !parseFont(currentField()?.font).italic));

  // Text-align buttons.
  document.querySelectorAll("#align-row [data-align]").forEach(b =>
    b.addEventListener("click", () => setFieldAlign(b.dataset.align)));

  // Floating toolbar controls.
  document.getElementById("tb-family").addEventListener("change", e => setFieldFont("family", e.target.value));
  document.getElementById("tb-bold").addEventListener("click", () =>
    setFieldFont("bold", !parseFont(currentField()?.font).bold));
  document.getElementById("tb-italic").addEventListener("click", () =>
    setFieldFont("italic", !parseFont(currentField()?.font).italic));
  document.getElementById("tb-size").addEventListener("input", e => setFieldSize(Number(e.target.value)));
  document.getElementById("tb-size-up").addEventListener("click", () => setFieldSize((currentField()?.size || 11) + 1));
  document.getElementById("tb-size-down").addEventListener("click", () => setFieldSize((currentField()?.size || 11) - 1));
}

function currentField() { return layout.fields.find(x => x.name === activeName); }

// Move the selected field by (dx, dy) PDF points, clamped to the page, and
// refresh its box + the editing UIs in place.
function nudgeField(dx, dy) {
  const f = currentField();
  if (!f) return;
  f.x = Math.min(Math.max(f.x + dx, 0), layout.page_width);
  f.y = Math.min(Math.max(f.y + dy, 0), layout.page_height);
  const el = boxes.get(f.name);
  if (el) positionBox(el, f);
  fillEdit(f);
  updateListLabel(f);
  positionToolbar();
}

// Arrow keys nudge the selected field: 1pt, or a grid step with Shift.
// (Ignored while typing in an input/select so number fields still work.)
document.addEventListener("keydown", e => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea") return;

  // Shape shortcuts: Esc cancels an active draw tool / deselects; Delete removes.
  if (e.key === "Escape") {
    if (drawTool !== "select") { e.preventDefault(); setTool("select"); return; }
    if (activeShape !== null) { e.preventDefault(); deselectShape(); return; }
  }
  if (activeShape !== null && (e.key === "Delete" || e.key === "Backspace")) {
    e.preventDefault();
    shapes().splice(activeShape, 1);
    deselectShape();
    return;
  }
  // Arrow keys nudge a selected shape too.
  if (activeShape !== null && e.key.startsWith("Arrow")) {
    e.preventDefault();
    const st = e.shiftKey ? gridSize() : 1;
    const d = { ArrowLeft: [-st, 0], ArrowRight: [st, 0], ArrowUp: [0, st], ArrowDown: [0, -st] }[e.key];
    nudgeShape(d[0], d[1]);
    return;
  }

  if (!activeName) return;
  if (e.key === "Escape") { e.preventDefault(); deselect(); return; }
  const step = e.shiftKey ? gridSize() : 1;
  const moves = {
    ArrowLeft: [-step, 0], ArrowRight: [step, 0],
    // PDF y increases upward, so ArrowUp adds to y.
    ArrowUp: [0, step], ArrowDown: [0, -step],
  };
  const m = moves[e.key];
  if (!m) return;
  e.preventDefault();
  nudgeField(m[0], m[1]);
});

// Position the floating toolbar just above the selected field's box, using the
// box's ACTUAL rendered rect so it respects font size / wrapping / the tag
// (the box is shifted up by its own height, so its visual top is the real top).
function positionToolbar() {
  const tb = document.getElementById("field-toolbar");
  const f = currentField();
  const el = f && boxes.get(f.name);
  if (!f || !el) { tb.style.display = "none"; return; }
  tb.style.display = "flex";
  const box = el.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const left = box.left - wrapRect.left;
  let topPx = box.top - wrapRect.top - tb.offsetHeight - 6;
  // If there's no room above (field near the top), drop the toolbar below.
  if (topPx < 0) topPx = box.bottom - wrapRect.top + 6;
  tb.style.left = Math.max(0, Math.min(left, wrap.clientWidth - tb.offsetWidth)) + "px";
  tb.style.top = topPx + "px";
}

// Populate the Add-field dropdown with rule/built-in fields not already placed.
function refreshFieldPicker() {
  const sel = document.getElementById("new-field-name");
  const placed = new Set(layout.fields.map(f => f.name));
  const addable = availableFields.filter(n => !placed.has(n));
  sel.innerHTML = addable.length
    ? addable.map(n => `<option value="${n}">${n}</option>`).join("")
    : `<option value="">— all fields placed —</option>`;
  document.getElementById("add-field").disabled = addable.length === 0;

  // Flag any placed field that no rule produces (custom-text fields are exempt —
  // they carry their own literal text).
  const stale = layout.fields
    .filter(f => !isCustom(f) && !availableFields.includes(f.name))
    .map(f => f.name);
  const hint = document.getElementById("fields-hint");
  hint.innerHTML = stale.length
    ? `⚠ Placed but no rule produces: <b>${stale.map(s => s).join(", ")}</b> — these stay blank on the PDF.`
    : "";
  hint.className = "hint" + (stale.length ? " warn" : "");
}

document.getElementById("add-field").addEventListener("click", () => {
  const name = document.getElementById("new-field-name").value.trim();
  if (!name) return;
  if (layout.fields.some(f => f.name === name)) { alert("Field exists"); return; }
  layout.fields.push({ name, x: 90, y: 700, font: "Helvetica", size: 12, max_width: 300 });
  selectField(name);
  refreshFieldPicker();
});

// Add a static custom-text field (prints literal text, not a regex/built-in).
document.getElementById("add-custom-text").addEventListener("click", () => {
  let i = 1;
  while (layout.fields.some(f => f.name === `text_${i}`)) i++;
  const name = `text_${i}`;
  layout.fields.push({
    name, text: "Custom text", x: 90, y: 700,
    font: "Helvetica", size: 12, max_width: 300,
  });
  selectField(name);
  renderBoxes();
  refreshFieldPicker();
  // Focus the text input for immediate editing.
  const t = document.getElementById("edit-text");
  if (t) { t.focus(); t.select(); }
});

// Clicking the canvas background (not a field box / toolbar) deselects.
// Start drawing a shape when a draw tool is active (capture so it beats the
// field/shape mousedown handlers).
wrap.addEventListener("mousedown", e => {
  if (drawTool !== "select") { startDraw(e); }
});

wrap.addEventListener("click", e => {
  if (e.target.closest(".field-box") || e.target.closest("#field-toolbar")) return;
  if (e.target.closest(".shape") || e.target.closest("#shape-edit")) return;
  deselect();
  deselectShape();
});

document.getElementById("save-layout").addEventListener("click", async () => {
  try {
    await saveLayout();   // also refreshes the snapshot -> clears the dirty guard
    document.getElementById("save-msg").textContent = "Saved ✓";
    setTimeout(() => document.getElementById("save-msg").textContent = "", 2000);
  } catch (e) { document.getElementById("save-msg").textContent = "Error: " + e.message; }
});

document.getElementById("preview-btn").addEventListener("click", async () => {
  // Save current layout first so the preview reflects unsaved edits.
  try { await saveLayout(); } catch (e) { /* ignore */ }
  const message = document.getElementById("preview-msg").value;
  const res = await fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) { alert("Preview failed: " + res.status); return; }
  const blob = await res.blob();
  window.open(URL.createObjectURL(blob), "_blank");
});

// No template image available: show a plain white page (sized to the layout
// aspect ratio) so fields are still draggable on a correctly-scaled canvas.
window.templateMissing = function () {
  img.removeAttribute("src");
  img.style.display = "none";
  wrap.classList.add("no-template"); // gives the wrap a white, page-shaped area
  document.getElementById("no-template").style.display = "flex";
  renderBoxes();
};

// Pull any field whose anchor sits off the page back onto it (with a small
// margin). Old layouts had stray coords like x:-865 / y:1190 that landed boxes
// outside the canvas. Returns how many fields were moved.
function clampFieldsToPage() {
  const M = 5; // keep the anchor at least this many points from each edge
  let moved = 0;
  for (const f of layout.fields) {
    const x = Math.min(Math.max(f.x, M), layout.page_width - M);
    const y = Math.min(Math.max(f.y, M), layout.page_height - M);
    if (x !== f.x || y !== f.y) { f.x = x; f.y = y; moved++; }
  }
  return moved;
}

async function init() {
  // Seed the "Open preview PDF" box with the SAME sample message the WYSIWYG
  // uses, so the editor and the PDF preview render identical field values.
  document.getElementById("preview-msg").value = SAMPLE_MESSAGE;
  layout = await api("GET", "/api/layout");
  // Fields available to place = built-ins + whatever the parsing rules produce.
  try {
    availableFields = (await api("GET", "/api/fields")).fields || [];
  } catch (e) { availableFields = []; }
  // Real extracted values for the WYSIWYG previews (rules vs. the sample message).
  await loadSampleValues();

  // Ask the server about the active template (image availability + REAL page
  // size). The renderer merges the overlay onto the template's page, so the
  // editor must use the template's size as its coordinate space — otherwise a
  // Letter template with an A4 layout (or vice-versa) misaligns every field.
  let status = { image_available: false };
  try { status = await api("GET", "/api/template/status"); } catch (e) { /* blank */ }
  if (status.page_width && status.page_height) {
    if (status.page_width !== layout.page_width || status.page_height !== layout.page_height) {
      layout.page_width = status.page_width;
      layout.page_height = status.page_height;
      document.getElementById("save-msg").textContent =
        `Page size set to template (${Math.round(status.page_width)}×${Math.round(status.page_height)}pt) — Save to keep.`;
    }
  }

  // Drive the blank-page aspect ratio from the actual page size.
  wrap.style.setProperty("--page-aspect", `${layout.page_width} / ${layout.page_height}`);
  const moved = clampFieldsToPage();
  if (moved > 0) {
    const m = document.getElementById("save-msg");
    m.textContent = `Moved ${moved} off-page field${moved === 1 ? "" : "s"} onto the page — Save to keep.`;
  }
  refreshFieldPicker();
  bindEdit();
  bindShapeTools();
  img.addEventListener("load", renderBoxes);
  window.addEventListener("resize", renderBoxes);

  if (status.image_available) {
    img.src = "/api/template/image"; // onload -> renderBoxes (onerror -> templateMissing)
  } else {
    window.templateMissing(); // plain white placeholder page
  }
  // Render immediately too so boxes appear before the image finishes loading.
  renderBoxes();

  // Unsaved-changes guard: dirty when the layout differs from the last save.
  setLayoutSnapshot();
  if (window.Dirty) {
    Dirty.setChecker(() => JSON.stringify(layout) !== layoutSnapshot);
    Dirty.onSave(saveLayout);
  }
}

// Snapshot the saved layout state so we can detect unsaved edits.
let layoutSnapshot = "";
function setLayoutSnapshot() { layoutSnapshot = JSON.stringify(layout); }

async function saveLayout() {
  await api("PUT", "/api/layout", layout);
  setLayoutSnapshot();
}

init();
