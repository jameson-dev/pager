"""Overlay extracted fields onto the template PDF at configured coordinates."""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


def wrap_text(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Greedy word-wrap so long values don't overflow their box.

    Uses reportlab's stringWidth (the real PDF font metrics) so the layout
    editor can render the exact same line breaks as the printed output.
    """
    if max_width <= 0:
        return [text]
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if stringWidth(trial, font, size) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


# Backwards-compatible private alias (used within this module).
_wrap_text = wrap_text


def text_width(text: str, font: str, size: float) -> float:
    """Width of a single string in PDF points, via real font metrics."""
    try:
        return stringWidth(text, font, size)
    except Exception:  # noqa: BLE001  (unknown font, etc.)
        return stringWidth(text, "Helvetica", size)


def _hex_to_rgb(color: str) -> tuple[float, float, float] | None:
    """'#RRGGBB' -> (r, g, b) in 0..1, or None for empty/'none'."""
    if not color or color in ("none", "transparent"):
        return None
    s = color.lstrip("#")
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255)
    except ValueError:
        return None


def _draw_shapes(c, shapes: list[dict]) -> None:
    """Draw rectangles and lines (template scaffolding) in PDF coordinates.

    Rect:  {type:"rect", x, y, w, h, stroke, stroke_width, fill}
           (x, y) is the bottom-left corner, w/h positive (PDF up-is-positive).
    Line:  {type:"line", x1, y1, x2, y2, stroke, stroke_width}
    Colors are '#RRGGBB' (or 'none'); stroke defaults to black.
    """
    for s in shapes or []:
        stype = s.get("type")
        sw = float(s.get("stroke_width", 1) or 1)
        stroke = _hex_to_rgb(s.get("stroke", "#000000"))
        if stype == "rect":
            fill = _hex_to_rgb(s.get("fill"))
            x, y, w, h = float(s["x"]), float(s["y"]), float(s["w"]), float(s["h"])
            if fill is not None:
                c.setFillColorRGB(*fill)
                # Fill opacity 0..100 (%) -> alpha. Default 100 (opaque).
                alpha = max(0, min(100, int(s.get("fill_opacity", 100)))) / 100
                c.setFillAlpha(alpha)
            if stroke is not None:
                c.setStrokeColorRGB(*stroke)
                c.setLineWidth(sw)
            c.rect(x, y, w, h, stroke=1 if stroke is not None else 0,
                   fill=1 if fill is not None else 0)
            c.setFillAlpha(1)   # reset so following shapes/text aren't affected
        elif stype == "line":
            if stroke is None:
                stroke = (0, 0, 0)
            c.setStrokeColorRGB(*stroke)
            c.setLineWidth(sw)
            c.line(float(s["x1"]), float(s["y1"]), float(s["x2"]), float(s["y2"]))


def _build_overlay(layout: dict, context: dict, page_size=None) -> io.BytesIO:
    """Create an in-memory PDF containing just the placed text.

    `page_size` (width, height) overrides the layout's declared size — used to
    match the template's real page so the overlay and template align exactly.
    """
    if page_size is not None:
        width, height = page_size
    else:
        width = layout.get("page_width", A4[0])
        height = layout.get("page_height", A4[1])
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))

    # Draw shapes first so text/fields render on top of any shaded boxes.
    _draw_shapes(c, layout.get("shapes", []))

    for f in layout.get("fields", []):
        # A field with a literal `text` is a static custom-text field — it prints
        # that text verbatim instead of looking up an extracted/built-in value.
        if f.get("text"):
            value = str(f["text"])
        else:
            value = context.get(f["name"])
            if value is None or value == "":
                continue
            value = str(value)
        font = f.get("font", "Helvetica")
        size = f.get("size", 11)
        x = f["x"]
        y = f["y"]
        max_width = f.get("max_width", 0)
        align = f.get("align", "left")
        leading = size * 1.2

        c.setFont(font, size)
        lines = _wrap_text(value, font, size, max_width)
        for i, line in enumerate(lines):
            ly = y - i * leading
            # Align each line WITHIN the field's text box [x, x + max_width].
            # (Left = draw at x; center/right need a width to align against.)
            if align in ("center", "right") and max_width > 0:
                lw = stringWidth(line, font, size)
                if align == "center":
                    c.drawString(x + (max_width - lw) / 2, ly, line)
                else:
                    c.drawString(x + max_width - lw, ly, line)
            else:
                c.drawString(x, ly, line)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf


def render_job_pdf(template_path: str, layout: dict, context: dict, out_path: str) -> str:
    """
    Stamp `context` onto `template_path` using `layout`, write to `out_path`.

    The overlay (placed fields) is merged onto page 1. If the template has more
    pages, they are preserved after it, so a multi-page template prints in full.
    If the template is missing, a blank page sized to the layout is used so jobs
    still render. Returns out_path.
    """
    writer = PdfWriter()
    if template_path and Path(template_path).exists():
        template = PdfReader(template_path)
        first = template.pages[0]
        # Build the overlay at the TEMPLATE's real page size so field coordinates
        # land in the same place the editor shows them (the editor scales to the
        # template image). Otherwise a Letter template + A4 layout would misalign.
        box = first.mediabox
        page_size = (float(box.width), float(box.height))
        overlay_page = PdfReader(_build_overlay(layout, context, page_size)).pages[0]
        first.merge_page(overlay_page)
        writer.add_page(first)
        for page in template.pages[1:]:
            writer.add_page(page)
    else:
        overlay_page = PdfReader(_build_overlay(layout, context)).pages[0]
        writer.add_page(overlay_page)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return out_path
