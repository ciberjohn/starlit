#!/usr/bin/env python3
"""Slice verified rows into printable A4 pages (PDF) — and keep the rows for later re-slicing.

The screen chart is one enormous PNG (thousands of pixels wide for a wide row). Printed on a single
A4 sheet every stitch would come out under a millimetre wide, so the chart is redrawn here onto
A4 pages at a chosen stitch size.

Two rules shape the cuts:

* **Cut between stitch groups, not through them.** A shell or a cluster is one shape; a sheet
  edge through its middle is a reading error waiting to happen. Boundaries are nudged (by at
  most `jitter` stitches) to the position that crosses the fewest groups in the most rows.
* **6 mm per stitch, not 4.** The stitch column grew from 15 px to 26 px so that neighbouring
  symbols stop touching, and 6 mm keeps the printed symbol the size it was at 4 mm — the same
  paper, divided with room between the stitches instead of without it.

* **Repeat two stitches across every cut.** Whatever a nudge cannot avoid still appears, whole,
  on both sheets. A dashed line marks where the neighbour takes over.

Coverage is asserted, not assumed: every stitch column and every row is drawn on exactly one
sheet, or the module refuses to write a PDF. A printable chart that quietly omits a column is
worse than no chart.

**Why the symbols are drawn small and magnified rather than drawn big.** The symbol library
(`crochet_chart.draw_symbol`) works in screen pixels and hardcodes a 2 px stroke. Drawing it at
print size would give a spindly hairline that some printers drop out. Each page is therefore
composed in the *screen chart's own geometry* (26 px per stitch, 46 px per row, 10 px glyph,
2 px stroke) and then resampled by the scale factor, so line weight stays proportional to the
glyph and a printed page is exactly the chart seen on screen. Labels, guides and cut marks are
drawn afterwards at native print resolution so the text stays crisp.
"""
import json, os
from PIL import Image, ImageDraw

from crochet_chart import draw_symbol, draw_symbol_fitted, place_row, cramped_rows

# --- screen geometry, imported from render_chart: one source of truth, print == screen ------
from render_chart import CELL_W, CELL_H, SYM, CLEARANCE, KEY, _legend_name
BG, INK, GRID, FAINT = (255, 255, 255), (25, 25, 30), (231, 231, 238), (150, 150, 160)
CUT_INK = (150, 60, 60)

A4_MM = {"portrait": (210.0, 297.0), "landscape": (297.0, 210.0)}

try:                                   # Pillow >= 9.1; the bare Image.LANCZOS alias is gone in 12
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:                 # pragma: no cover - older Pillow
    RESAMPLE = getattr(Image, "LANCZOS", 1)


def _font(size, bold=False):
    from PIL import ImageFont
    size = max(7, int(round(size)))
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- page planning
def _group_bounds(items):
    """Occupied column ranges of the drawn groups in one row, as (start0, span).

    `_SKIP` is spacing: nothing is drawn there, so a cut through it cannot split a symbol.
    A zero-span item (a chain mid-row) occupies its boundary and no columns.
    """
    x, out = 0, []
    for kind, span in items:
        if kind == "_SKIP":
            x += span
            continue
        if span:
            out.append((x, span))
        x += span
    return out


def _crossings(boundaries, bounds):
    """How many rows have a group straddling each candidate boundary (0-based col index)."""
    hits = {b: 0 for b in boundaries}
    for row_bounds in bounds:
        for s, span in row_bounds:
            for b in boundaries:
                if s < b < s + span:
                    hits[b] += 1
    return hits


def _snap(b, bounds, lo, hi, jitter):
    """Move a cut off any stitch group. Ties break to the least movement, then the lowest index."""
    cand = [c for c in range(b - jitter, b + jitter + 1) if lo <= c <= hi]
    if not cand:
        return b
    hits = _crossings(cand, bounds)
    return sorted(cand, key=lambda c: (hits[c], abs(c - b), c))[0]


def plan(rows, mm_per_stitch=6.0, orientation="landscape", overlap=2, dpi=200,
         margin_mm=8.0, gutter_mm=9.0, header_mm=11.0, footer_mm=9.0, jitter=2):
    """Work out the page grid: which rows and which columns land on each sheet.

    `rows` is {row_number: [(kind, span), ...]}. Page 1 of the chart pages holds the lowest rows
    — work starts at row 1 and goes up, so the sheets are ordered the way the work is.
    """
    if orientation not in A4_MM:
        raise ValueError(f"orientation must be one of {sorted(A4_MM)}")
    if not rows:
        raise ValueError("no rows to paginate")
    pw_mm, ph_mm = A4_MM[orientation]
    row_pitch_mm = CELL_H * mm_per_stitch / CELL_W
    overlap_mm = overlap * mm_per_stitch
    side_mm = max(margin_mm, overlap_mm + 4.0)        # room to draw the overlap outside the core
    avail_w_mm = pw_mm - 2 * side_mm - gutter_mm
    avail_h_mm = ph_mm - 2 * margin_mm - header_mm - footer_mm
    cols_core = int((avail_w_mm - overlap_mm) // mm_per_stitch)
    rows_pp = int(avail_h_mm // row_pitch_mm)
    if cols_core < 4 or rows_pp < 2:
        raise ValueError(f"page too small for {mm_per_stitch} mm per stitch on A4 {orientation}")

    ordered = sorted(rows)
    maxcols = max(sum(s for _, s in rows[r]) for r in ordered)
    bounds = [_group_bounds(rows[r]) for r in ordered]

    # column strips: cut, then nudge the cut off the stitch groups it would have split
    strips, start = [], 1
    while start <= maxcols:
        end = min(start + cols_core - 1, maxcols)
        if end < maxcols:
            end = _snap(end + 1, bounds, start + 3, maxcols - 3, jitter)
        end = max(end, start)
        strips.append((start, end))
        start = end + 1

    # row bands: top of the chart first (as drawn), then re-ordered so the lowest rows lead
    desc = sorted(rows, reverse=True)
    bands = [desc[i:i + rows_pp] for i in range(0, len(desc), rows_pp)]
    bands = list(reversed(bands))                     # row 1's band becomes the first page

    pages, n = [], 0
    for band in bands:
        for c_from, c_to in strips:
            n += 1
            pages.append({"n": n, "rows": sorted(band), "col_from": c_from, "col_to": c_to,
                          "draw_from": max(1, c_from - overlap),
                          "draw_to": min(maxcols, c_to + overlap)})
    return {"pages": pages, "n_pages": len(pages), "cols": maxcols, "n_rows": len(ordered),
            "mm_per_stitch": mm_per_stitch, "orientation": orientation, "overlap": overlap,
            "dpi": dpi, "row_pitch_mm": row_pitch_mm, "cols_core": cols_core, "rows_pp": rows_pp,
            "side_mm": side_mm, "gutter_mm": gutter_mm, "margin_mm": margin_mm,
            "header_mm": header_mm, "footer_mm": footer_mm, "page_w_mm": pw_mm,
            "page_h_mm": ph_mm, "strips": len(strips), "bands": len(bands)}


def assert_coverage(meta, rows):
    """Every (row, stitch column) cell of the chart appears on exactly one sheet. Refuse otherwise.

    A row is drawn once per column strip — that is the point of a strip — so the invariant is
    per cell, not per row: no cell twice, and no cell of any row left off every sheet.
    """
    ordered = sorted(rows)
    widths = {r: sum(s for _, s in rows[r]) for r in ordered}
    seen, dupes = {}, []
    for p in meta["pages"]:
        for r in p["rows"]:
            for c in range(p["col_from"], min(p["col_to"], widths[r]) + 1):
                if (r, c) in seen:
                    dupes.append((r, c, seen[(r, c)]))
                seen[(r, c)] = p["n"]
    missing = [(r, c) for r in ordered for c in range(1, widths[r] + 1) if (r, c) not in seen]
    if missing or dupes:
        raise AssertionError(
            f"cell coverage wrong — {len(missing)} cell(s) missing (e.g. {missing[:6]}), "
            f"{len(dupes)} drawn twice (e.g. {dupes[:6]})")
    per_row = {r: sum(1 for p in meta["pages"] if r in p["rows"]) for r in ordered}
    if set(per_row.values()) != {meta["strips"]}:
        raise AssertionError(f"rows are not spread over all {meta['strips']} strips: {per_row}")
    return True


# ---------------------------------------------------------------- drawing
def _px(mm, dpi):
    return mm * dpi / 25.4


def _dashed_v(d, x, y0, y1, fill, width, dash, gap):
    y = y0
    while y < y1:
        d.line([x, y, x, min(y + dash, y1)], fill=fill, width=width)
        y += dash + gap


def _layer_screen(rows, drawn, page):
    """One page's stitches in the screen chart's own units — the same pixels as the PNG."""
    width_s = (page["draw_to"] - page["draw_from"] + 1) * CELL_W
    height_s = len(drawn) * CELL_H
    layer = Image.new("RGB", (max(width_s, 1), max(height_s, 1)), BG)
    d = ImageDraw.Draw(layer)
    for i, rn in enumerate(sorted(drawn, reverse=True)):
        cy = i * CELL_H + CELL_H / 2
        d.line([0, cy + CELL_H * 0.42, width_s, cy + CELL_H * 0.42], fill=GRID, width=1)
        # the SAME placement the screen chart uses — sizes included, so a page cannot drift from
        # the PNG (tests/paginate_pdf_test.py compares the two pixel for pixel)
        for kind, x_px, cx, size, max_w in place_row(rows.get(rn, [])):
            col = x_px / CELL_W + 1                                  # 1-based first column
            if page["draw_from"] <= col <= page["draw_to"]:
                draw_symbol_fitted(layer, d, kind, cx - (page["draw_from"] - 1) * CELL_W, cy,
                                   size, max_w)
    return layer


def _symbol_layer(rows, drawn, page, meta):
    """The page's stitches, drawn in screen units and magnified to the printed stitch size.

    Returns the magnified layer and the factor, so a caller can prove the magnification is the
    ONLY transformation: `_layer_screen` is pixel-identical to the screen chart's own window.
    """
    f = (meta["mm_per_stitch"] / CELL_W) * meta["dpi"] / 25.4      # print px per screen px
    layer = _layer_screen(rows, drawn, page)
    return layer.resize((max(int(round(layer.width * f)), 1),
                         max(int(round(layer.height * f)), 1)), RESAMPLE), f


def _draw_chart_page(page, rows, meta, title, dpi):
    W, H = int(round(_px(meta["page_w_mm"], dpi))), int(round(_px(meta["page_h_mm"], dpi)))
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f = (meta["mm_per_stitch"] / CELL_W) * dpi / 25.4
    cell_px, row_px = CELL_W * f, CELL_H * f
    hair = max(1, int(round(0.25 * dpi / 25.4)))
    x0 = _px(meta["side_mm"] + meta["gutter_mm"], dpi)
    y0 = _px(meta["margin_mm"] + meta["header_mm"], dpi)

    drawn = [r for r in page["rows"] if r in rows]
    missing = [r for r in page["rows"] if r not in rows]
    if missing:
        raise ValueError(f"page {page['n']}: rows {missing[:6]} are not in the row set")

    layer, f = _symbol_layer(rows, drawn, page, meta)
    img.paste(layer, (int(round(x0)), int(round(y0))))
    # the row rules are redrawn here, at print resolution and darker than the screen's faint grey,
    # because the PDF is saved as 1-bit line art and anything lighter than the threshold vanishes
    for i in range(len(drawn)):
        ry = y0 + (i + 1) * row_px - row_px * 0.08
        d.line([x0 - _px(2, dpi), ry, x0 + layer.width, ry], fill=(150, 150, 160), width=hair)

    content_w, content_h = layer.size
    y_bot = y0 + content_h
    f_title, f_sub, f_lab = _font(16 * f, True), _font(11 * f), _font(13 * f)

    d.text((x0, _px(meta["margin_mm"] - 2.5, dpi)), (title or "Crochet symbol chart")[:68],
           fill=INK, font=f_title)
    d.text((x0, _px(meta["margin_mm"] + 5.5, dpi)),
           f"rows {min(drawn)}-{max(drawn)}   stitches {page['col_from']}-{page['col_to']}"
           f"   page {page['n']} of {meta['n_pages']}   (row 1 at the bottom; each row is worked "
           f"in the opposite direction)", fill=(110, 110, 120), font=f_sub)

    for i, rn in enumerate(sorted(drawn, reverse=True)):
        cy = y0 + i * row_px + row_px / 2
        lab = f"R{rn}"
        d.text((x0 - _px(3, dpi) - d.textlength(lab, font=f_lab), cy - 9 * f), lab,
               fill=INK, font=f_lab)

    # 10-stitch guides, numbered every 50, in absolute chart columns
    first = ((page["draw_from"] - 1) // 10) * 10 + 1
    last = page["draw_to"] + 10
    for c in range(first, last, 10):
        gx = x0 + (c - page["col_from"]) * cell_px
        if gx < x0 - cell_px or gx > x0 + content_w:
            continue
        strong = c % 50 == 0
        d.line([gx, y0 - _px(2, dpi), gx, y_bot], fill=(205, 205, 215) if strong else FAINT,
               width=hair)
        if strong:
            d.text((gx + 2, y0 - _px(6, dpi)), str(c), fill=FAINT, font=f_sub)

    # the cuts: dashed line + a signpost to the sheet on the other side
    def cut(x, tag):
        _dashed_v(d, x, y0 - _px(2, dpi), y_bot, CUT_INK, hair, 6 * f, 5 * f)
        d.text((x + 0.6 * f, y_bot + _px(1.5, dpi)), tag, fill=CUT_INK, font=f_sub)

    if page["col_from"] > 1:
        prev = next((p for p in meta["pages"] if p["n"] == page["n"] - 1), None)
        cut(x0 - _px(0.5, dpi), f"< page {prev['n']}" if prev else "< continued")
    if page["col_to"] < meta["cols"]:
        nxt = next((p for p in meta["pages"] if p["n"] == page["n"] + 1), None)
        cut(x0 + (page["col_to"] - page["col_from"] + 1) * cell_px,
            f"page {nxt['n']} >" if nxt else "continued >")
    if page["col_from"] > 1:            # the overlap stitches belong to the previous sheet
        d.text((x0 - _px(meta["side_mm"] - 2, dpi), y0 - _px(6, dpi)),
               f"overlap: st {page['draw_from']}-{page['col_from'] - 1}", fill=CUT_INK, font=f_sub)

    d.text((x0, H - _px(meta["footer_mm"] - 3, dpi)),
           f"Starlit · one stitch = {meta['mm_per_stitch']} mm · the two stitches either side of a "
           f"dashed cut are repeated on both sheets, so no symbol is left half-drawn",
           fill=(150, 150, 160), font=f_sub)
    return img


def _kinds_used(rows, present):
    seen = set()
    for rn in sorted(present):
        for kind, _span in rows.get(rn, []):
            if kind.startswith("_"):
                continue
            seen.add(kind)
    order = {k: i for i, (k, *_rest) in enumerate(KEY)}
    return sorted(seen, key=lambda k: (order.get(k, len(order)), k))


def _legend_swatch(kind, dpi, mm_per_stitch, box_mm=9.0):
    """One symbol, drawn in screen units and magnified, for the cover's key."""
    f = (mm_per_stitch / CELL_W) * dpi / 25.4
    s = int(round(_px(box_mm, dpi)))
    side = max(int(round(CELL_H * f)), 1)
    tile = Image.new("RGB", (side, side), BG)
    draw_symbol(ImageDraw.Draw(tile), kind, tile.width / 2, tile.height / 2, SYM)
    tile = tile.resize((s, s), RESAMPLE)
    return tile


def _draw_cover(rows, meta, title, dpi):
    f = (meta["mm_per_stitch"] / CELL_W) * dpi / 25.4
    W, H = int(round(_px(meta["page_w_mm"], dpi))), int(round(_px(meta["page_h_mm"], dpi)))
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    pad = int(round(_px(18, dpi)))
    f_t, f_h, f_b, f_n = _font(26, True), _font(17, True), _font(13, True), _font(12)
    y = pad
    d.text((pad, y), "Starlit — print sheets", fill=INK, font=f_t); y += int(_px(11, dpi))
    d.text((pad, y), (title or "crochet symbol chart")[:80], fill=(90, 84, 90), font=f_n)
    y += int(_px(8, dpi))
    hi = max(max(p["rows"]) for p in meta["pages"])
    lo = min(min(p["rows"]) for p in meta["pages"])
    d.text((pad, y), f"{meta['n_pages']} chart pages · one stitch prints {meta['mm_per_stitch']} mm "
                     f"wide ({meta['row_pitch_mm']:.1f} mm per row) · A4 {meta['orientation']} · "
                     f"rows {lo}-{hi} × {meta['cols']} stitches",
           fill=(110, 110, 120), font=f_n); y += int(_px(11, dpi))

    d.text((pad, y), "How the sheets go together", fill=INK, font=f_h); y += int(_px(7.5, dpi))
    for line in ("Sheets are ordered from the bottom of the pattern upwards: page 1 holds the lowest "
                 "rows, at the left edge. Work across, then take the next sheet to the right.",
                 "Every cut repeats two stitches from the sheet next door and is marked with a dashed "
                 "line, so a shell or a cluster is never lost to the edge.",
                 "Column numbers along the top and the R-numbers down the side are absolute — they "
                 "match the full-screen chart and the pattern's own stitch counts."):
        d.text((pad, y), line[:140], fill=(90, 84, 90), font=f_n); y += int(_px(6.2, dpi))
    y += int(_px(4, dpi))

    d.text((pad, y), "Page map", fill=INK, font=f_h); y += int(_px(8, dpi))
    cols, row_h = 3, int(_px(6.2, dpi))
    cw = (W - 2 * pad) // cols
    for i, p in enumerate(meta["pages"]):
        c, r = i % cols, i // cols
        d.text((pad + c * cw, y + r * row_h),
               f"{p['n']:>2d}  rows {min(p['rows'])}-{max(p['rows'])} · st {p['col_from']}-{p['col_to']}",
               fill=(90, 84, 90), font=f_n)
    y += ((len(meta["pages"]) + cols - 1) // cols) * row_h + int(_px(6, dpi))

    kinds = _kinds_used(rows, sorted(rows))
    if kinds:
        d.text((pad, y), "Symbols used in this chart", fill=INK, font=f_h); y += int(_px(9, dpi))
        cell_w, cell_h = (W - 2 * pad) // 3, int(_px(13, dpi))
        for i, kind in enumerate(kinds):
            c, r = i % 3, i // 3
            x, yy = pad + c * cell_w, y + r * cell_h
            sw = _legend_swatch(kind, dpi, meta["mm_per_stitch"])
            img.paste(sw, (x, yy + (cell_h - sw.height) // 2))
            abbr, en, note = _legend_name(kind)
            d.text((x + sw.width + int(_px(3, dpi)), yy + int(_px(1.5, dpi))), abbr,
                   fill=INK, font=f_b)
            d.text((x + sw.width + int(_px(3, dpi)), yy + int(_px(5.5, dpi))),
                   (en + (f" — {note}" if note else ""))[:46], fill=(88, 80, 84), font=f_n)
        y += ((len(kinds) + 2) // 3) * cell_h + int(_px(4, dpi))
    d.text((pad, min(y, H - pad - int(_px(6, dpi)))),
           "Print at 100 % (no 'fit to page') or the stitch size will not match the pattern.",
           fill=(150, 150, 160), font=f_n)
    return img


# ---------------------------------------------------------------- entry points
def write_rows(rows, path):
    """Persist the verified rows beside the chart so it can be re-sliced later at another scale."""
    with open(path, "w") as fh:
        json.dump({str(k): [[kind, span] for kind, span in v] for k, v in sorted(rows.items())},
                  fh, separators=(",", ":"))
    return path


def render_pdf(rows, path, title="", mm_per_stitch=6.0, orientation="landscape", overlap=2,
               dpi=200, cover=True, progress=False, return_images=False, bit_threshold=190,
               on_page=None):
    """Write the A4 PDF for `rows`. Returns (path, meta). Refuses to write a partial chart.

    `on_page(n, total)` is called after each sheet is drawn. The web app turns it into real
    progress on screen: the sheets are the slow part of a conversion, and a few seconds of
    nothing happening reads as a frozen page.
    """
    meta = plan(rows, mm_per_stitch=mm_per_stitch, orientation=orientation, overlap=overlap,
                dpi=dpi)
    assert_coverage(meta, rows)
    pages = []
    if cover:
        pages.append(_draw_cover(rows, meta, title, dpi))
    for p in meta["pages"]:
        pages.append(_draw_chart_page(p, rows, meta, title, dpi))
        if progress:
            print(f"    page {p['n']}/{meta['n_pages']}", flush=True)
        if on_page:
            on_page(p["n"], meta["n_pages"])
    # 1-bit line art (PDF CCITT G4): ~14 KB a page against 258 KB as JPEG, and no JPEG ringing on
    # what is a line drawing. The threshold is 190, chosen so the grey labels and guides survive
    # while the faintest screen-only greys drop out — verified by rasterising the PDF back.
    pages = [p.convert("L").point(lambda v: 255 if int(v) >= bit_threshold else 0).convert("1")
             for p in pages]
    pages[0].save(path, "PDF", resolution=dpi, save_all=True, append_images=pages[1:])
    meta["pdf_pages"] = len(pages)
    meta["pdf_bytes"] = os.path.getsize(path)
    if return_images:
        return path, meta, pages
    return path, meta


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "rows.json"
    data = json.load(open(src))
    data = data.get("verified", data)
    rows = {int(k): [tuple(x) for x in v] for k, v in data.items()}
    out, m = render_pdf(rows, sys.argv[2] if len(sys.argv) > 2 else "print-a4.pdf",
                        title=src, progress=True)
    print(f"wrote {out}: {m['n_pages']} chart pages + cover, "
          f"{m['strips']}x{m['bands']} grid, {m['mm_per_stitch']} mm per stitch")
