#!/usr/bin/env python3
"""Render verified row data to a crochet symbol chart (PNG)."""
import json, re, sys
from PIL import Image, ImageDraw
from crochet_chart import (draw_symbol, draw_symbol_fitted, place_row,   # one source
                           CELL_W, CELL_H, SYM, CLEARANCE)
MARGIN_L, MARGIN_T, MARGIN_R, MARGIN_B = 95, 74, 30, 30
BG, INK, GRID, FAINT = (255, 255, 255), (25, 25, 30), (231, 231, 238), (150, 150, 160)
# One PNG dimension ceiling. The whole chart is built in a single in-memory image, so an
# absurdly wide pattern would otherwise die with a MemoryError instead of telling the user why.
# 20,000 px is ~760 stitch columns at 26 px — comfortably past any hand-worked pattern.
MAX_CHART_PX = 20000

rows, ordered, cols = {}, [], 0
try:                                          # only present when run as a script locally
    rows = {int(k): [tuple(x) for x in v] for k, v in json.load(open("rows.json")).items()}
    ordered = sorted(rows)
    cols = sum(s for _, s in rows[ordered[0]])
except Exception:
    pass                                      # imported as a library: caller supplies the rows


def render(rows_sel, col_from=0, col_to=None, path="chart.png", title="", legend=True):
    # render_chart loads rows.json from the cwd at import time. A library caller must set
    # render_chart.rows = its own rows first (app.py does this). If it does not, the missing
    # rows are silently skipped while the canvas still reserves space for them — producing a
    # half-blank chart that looks plausible. Refuse instead: a wrong chart is worse than an error.
    missing = [r for r in rows_sel if r not in rows]
    if missing:
        raise ValueError(
            f"render(): {len(missing)} of {len(rows_sel)} requested rows are not in the "
            f"module-level rows dict (e.g. {missing[:5]}). Set render_chart.rows = your_rows "
            f"before calling render(). Refusing to draw a partial chart.")
    present = [r for r in rows_sel if r in rows]
    # Derive the width from the rows actually being drawn. The module-level `cols` is only a
    # fallback: when this is used as a library the caller sets `rows` and `cols` stays 0, which
    # would otherwise collapse the chart to zero columns wide.
    if col_to is None:
        col_to = max((sum(s for _, s in rows[r]) for r in present), default=0) or cols
    ncols = col_to - col_from
    W = MARGIN_L + ncols * CELL_W + MARGIN_R

    # Legend: show only the symbols THIS chart actually uses. A 26-symbol key on a chart that
    # uses nine is noise; the point is that a chart should be readable on its own.
    kinds = _kinds_used(present) if legend else []
    legend_h = _legend_height(W, len(kinds))

    H = MARGIN_T + len(rows_sel) * CELL_H + MARGIN_B + legend_h
    if W > MAX_CHART_PX or H > MAX_CHART_PX:
        raise ValueError(
            f"this chart is {W}x{H} px, over the {MAX_CHART_PX} px limit — the pattern is too "
            f"wide ({ncols} stitch columns) to draw as one image. Split it into narrower "
            f"sections, or reduce the stitch column width.")
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # frame
    d.text((MARGIN_L, 18), title or "Crochet symbol chart", fill=INK)
    d.text((MARGIN_L, 38), f"rows {min(rows_sel)}-{max(rows_sel)}   columns {col_from+1}-{col_to}   "
                           f"(row 1 at the bottom; each row is worked in the opposite direction)",
           fill=(110, 110, 120))

    rows_desc = sorted([r for r in rows_sel if r in rows], reverse=True)   # top = highest row
    for r_i, rn in enumerate(rows_desc):
        y = MARGIN_T + r_i * CELL_H + CELL_H / 2
        d.text((MARGIN_L - 46, y - 7), f"R{rn}", fill=INK)
        d.line([MARGIN_L, y + CELL_H * 0.42, W - MARGIN_R, y + CELL_H * 0.42], fill=GRID, width=1)
        # place_row decides each symbol's size so neighbours never touch (they could not be told
        # apart when they did). Same call in paginate_pdf, so print == screen.
        for kind, x_px, cx, size, max_w in place_row(rows.get(rn, [])):
            if col_from <= x_px / CELL_W <= col_to:
                draw_symbol_fitted(img, d, kind, MARGIN_L + cx, y, size, max_w)

    for c in range(0, ncols + 1, 10):                   # faint 10-stitch guides
        gx = MARGIN_L + c * CELL_W
        d.line([gx, MARGIN_T - 6, gx, H - MARGIN_B], fill=FAINT if c % 50 else (205, 205, 215), width=1)
        if c % 50 == 0:
            d.text((gx + 2, MARGIN_T - 20), str(col_from + c + 1), fill=FAINT)
    if kinds:
        _draw_legend(d, kinds, W, MARGIN_T + len(rows_sel) * CELL_H + MARGIN_B)

    img.save(path)
    print(f"  wrote {path}  ({img.size[0]}x{img.size[1]})")
    return path


def _font(size=13, bold=False):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


# (symbol kind, US abbr, English name, Spanish name)  — from standard UK/US and ES/EN reference keys
KEY = [
    ("SS",        "ss",    "slip stitch",            "punto enano"),
    ("CH",        "ch",    "chain",                  "cadeneta"),
    ("SC",        "sc",    "single crochet",         "medio punto"),
    ("HDC",       "hdc",   "half double crochet",    "media vareta"),
    ("DC",        "dc",    "double crochet",         "vareta"),
    ("TRC",       "tr",    "treble crochet",         "vareta doble"),
    ("DTR",       "dtr",   "double treble",          "vareta triple"),
    ("FAN5DTR",   "5dtr",  "5 dtr in one stitch",    "5 varetas en un punto"),
    ("SHELL5",    "5dc",   "5 dc shell",             "abanico de 5 varetas"),
    ("SHELL4C",   "2dc1ch2dc", "2dc, ch1, 2dc shell", "abanico con 1 cadena"),
    ("CLU3",      "3dc-dec", "3 dc cluster (decrease)", "piña de 3 varetas"),
    ("DCC",       "dcc",   "dc cluster",             "piña de varetas"),
    ("VST",       "v-st",  "V stitch",               "punto en V"),
    ("YST",       "y-st",  "Y stitch",               "punto en Y"),
    ("YINV",      "inv-y", "inverted Y stitch",      "punto en Y invertido"),
    ("XCROSS",    "x-dc",  "1 over 1 dc cross",      "vareta cruzada"),
    ("XCROSSTR",  "x-tr",  "1 over 1 treble cross",  "vareta doble cruzada"),
    ("ARCH3",     "ch3-sp","chain-3 arch",           "arco de 3 cadenas"),
    ("ARCH5",     "ch5-sp","chain-5 arch",           "arco de 5 cadenas"),
    ("ARCH8",     "ch8-sp","chain-8 arch",           "arco de 8 cadenas"),
    ("RING",      "ring",  "ring / magic circle",    "anillo"),
    ("PICOT",     "picot", "open picot",             "picot abierto"),
    ("PICOT3",    "ch3pic","chain-3 picot",          "picot de 3 cadenas"),
    ("PICOT3SC",  "sc-pic","ch3 sc picot",           "picot cerrado con medio punto"),
    ("BULLION",   "bullion","bullion stitch",        "punto rococó"),
    ("LADDER",    "ladder","ladder stitch",          "punto escalera"),
]


# --- legend (drawn onto every chart) ------------------------------------------------------
# Show only the symbols THIS chart uses: a 26-symbol key stamped onto a chart that uses nine is
# noise. The aim is that a chart can be read on its own, without hunting for the key sheet.

LEG_CELL_W, LEG_CELL_H, LEG_PAD, LEG_HEAD = 330, 88, 28, 96


def _kinds_used(present):
    """The symbol kinds this chart actually draws, in the KEY's reading order."""
    seen = set()
    for rn in sorted(present):
        for kind, _span in rows.get(rn, []):
            if kind.startswith("_"):          # _SKIP and friends are spacing, not symbols
                continue
            seen.add(kind)
    order = {k: i for i, (k, *_rest) in enumerate(KEY)}
    return sorted(seen, key=lambda k: (order.get(k, len(order)), k))


def _legend_name(kind):
    """(abbr, english, note) for any kind, including the dynamic FAN5DTR / SHELL4C / CLU3."""
    # Decide the note first, so a kind gets it whether or not it also appears in KEY.
    if kind.startswith(("FAN", "SHELL")):
        note = "increase - stems meet at the base"
    elif kind.startswith("CLU"):
        note = "decrease - stems meet at the top"
    elif kind == "DCC":
        # This pattern's own terms define DCC as a dc2tog, so say what it means rather
        # than leaving a reader to guess which end the stems meet.
        note = "decrease - 2 dc closed at the top"
    else:
        note = ""
    for k, abbr, en, _es in KEY:
        if k == kind:
            return abbr, en, note
    m = re.match(r"FAN(\d+)([A-Z]+)$", kind)
    if m:
        return (f"{m.group(1)}{m.group(2).lower()}",
                f"{m.group(1)} {m.group(2)} in one stitch", note)
    m = re.match(r"SHELL(\d+)(C?)$", kind)
    if m:
        tail = " + ch" if m.group(2) else ""
        return (f"{m.group(1)}dc{tail}",
                f"shell of {m.group(1)} dc{tail}", note)
    m = re.match(r"CLU(\d+)(T?)$", kind)
    if m:
        return (f"{m.group(1)}dc-dec", f"{m.group(1)} dc cluster", note)
    return kind, kind, note


def _legend_cols(W):
    return max(1, (W - 2 * LEG_PAD) // LEG_CELL_W)


def _legend_height(W, n):
    if n <= 0:
        return 0
    cols = _legend_cols(W)
    return LEG_HEAD + ((n + cols - 1) // cols) * LEG_CELL_H + LEG_PAD


def _draw_legend(d, kinds, W, y0):
    """A strip below the chart: only the symbols used, and what each one means."""
    cols = _legend_cols(W)
    f_t, f_a, f_n = _font(21, bold=True), _font(15, bold=True), _font(13)
    d.rectangle([LEG_PAD, y0 + 6, W - LEG_PAD, y0 + _legend_height(W, len(kinds)) - 6],
                outline=(228, 224, 234), width=2)
    d.text((LEG_PAD + 22, y0 + 22), "Symbols used in this chart", fill=INK, font=f_t)
    d.text((LEG_PAD + 22, y0 + 58),
           "Crossing bars = yarn-overs (sc is a cross, then 1, 2, 3).      "
           "Stems meeting at the BASE = an increase (fan / shell).      "
           "Stems meeting at the TOP = a decrease (cluster).",
           fill=(124, 124, 140), font=f_n)
    for i, kind in enumerate(kinds):
        c, r = i % cols, i // cols
        x = LEG_PAD + 22 + c * LEG_CELL_W
        y = y0 + LEG_HEAD + r * LEG_CELL_H
        draw_symbol(d, kind, x + 24, y + LEG_CELL_H / 2 - 2, 27)
        abbr, en, note = _legend_name(kind)
        d.text((x + 60, y + 12), abbr, fill=INK, font=f_a)
        d.text((x + 60, y + 34), en, fill=(88, 80, 84), font=f_n)
        if note:
            d.text((x + 60, y + 53), note, fill=(152, 142, 146), font=f_n)


def render_key(path="symbol-key.png", cols=4):
    from crochet_chart import draw_symbol
    cw, ch = 300, 92
    rows = (len(KEY) + cols - 1) // cols
    W = 60 + cols * cw
    H = 118 + rows * ch + 30
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_t, f_c, f_s = _font(17), _font(13), _font(12)
    d.text((60, 26), "Symbol key — Starlit crochet charts", fill=INK, font=f_t)
    d.text((60, 52), "Drawn from standard UK/US symbol references. "
                     "Crossing bars = yarn-overs:  sc is a cross, hdc a plain T, then 1, 2, 3.",
           fill=(110, 110, 120), font=f_s)
    d.text((60, 74), "shell / abanico = stems meet at one BASE (increase)   ·   "
                     "cluster / piña = stems meet at the TOP (decrease)",
           fill=(110, 110, 120), font=f_s)
    for i, (kind, abbr, en, es) in enumerate(KEY):
        c, r = i % cols, i // cols
        x = 60 + c * cw
        y = 118 + r * ch
        d.rectangle([x, y, x + cw - 18, y + ch - 14], outline=(238, 232, 226))
        draw_symbol(d, kind, x + 42, y + ch / 2 + 6, 26)
        d.text((x + 88, y + 18), abbr, fill=INK, font=f_c)
        d.text((x + 88, y + 37), en, fill=(90, 80, 82), font=f_s)
        d.text((x + 88, y + 53), es, fill=(150, 140, 140), font=f_s)
    img.save(path)
    print(f"  wrote {path}  ({img.size[0]}x{img.size[1]}, {len(KEY)} symbols)")
    return path


if __name__ == "__main__":
    # Run engine/crochet_chart.py first: it writes rows.json into this directory, which this then
    # draws as the full chart, a motif window and the symbol key.
    if not ordered:
        raise SystemExit("no rows.json here — run `python engine/crochet_chart.py` first")
    render(ordered, path="chart_full.png", title="Starlit sample — all rows, full width")
    render(ordered[:4], col_from=0, col_to=30, path="chart_motif.png",
           title="Starlit sample — first rows, first 30 stitches")
    render_key()
