#!/usr/bin/env python3
"""
Written crochet instructions -> symbol chart.

First working slice. The key idea: every row in this pattern family ends with a stitch
count, e.g. "... (120)". That count is an ORACLE — we solve the repeat count from it and
refuse to render a row we cannot make add up. A chart you cannot verify is worse than none.

Layout model: one column per stitch position consumed on the row below.
  - a plain stitch consumes 1 column
  - "skip 3ST" consumes 3 columns and draws nothing
  - a chain in the middle of a row (CH1 between two fans) consumes 0 columns
  - "X in next ST, CH2 & skip 2ST" consumes 3 columns (the stitch + the 2 it jumps)
"""
import re
from PIL import Image, ImageDraw, ImageFont

# Glyph geometry — the ONE place these live; render_chart and paginate_pdf import them.
# Why 26 px per stitch column (it was 15): a chart can run to hundreds of stitches wide and some
# patterns put a ch-1 between almost every stitch, so two symbols share one column — a 10 px
# stitch beside an 11 px chain needs ~25 px to sit apart at all. At 15 px they met at 1 px and a
# row read as one lattice. Measured across a sample of rows, before and after.
CELL_W, CELL_H = 26, 46          # one stitch column, one row
SYM = 10                          # symbol glyph size
CLEARANCE = 3                     # px of white that must remain between two symbols (see place_row)
MARGIN_L, MARGIN_T = 90, 60
BG, INK = (255, 255, 255), (25, 25, 30)
GRID = (232, 232, 238)
HILITE = (150, 60, 60)


# ---------------------------------------------------------------- symbols
# Canonical international set, matching the standard UK/US key exactly:
#   cross (stem + bar at the MIDDLE)  = US sc   / UK dc
#   T (stem + top cap only)           = US hdc  / UK htr
#   T + 1 crossing bar below the cap  = US dc   / UK tr
#   T + 2 crossing bars               = US tr   / UK dtr
#   T + 3 crossing bars               = US dtr  / UK trtr
# Crossing bars == number of yarn-overs in the stitch.
CROSSING_BARS = {"SC": None, "HDC": 0, "DC": 1, "TRC": 2, "DTR": 3, "TRTR": 4}
# US term -> UK term, so the chart can be labelled in either convention
US_TO_UK = {"SC": "dc", "HDC": "htr", "DC": "tr", "TRC": "dtr", "DTR": "trtr"}


def draw_post(d, cx, cy, size, kind, width=2):
    """A 'post' stitch: vertical stem, top cap, and N crossing bars (or the sc cross)."""
    h, w = size, size * 0.86
    top, bot = cy - h / 2, cy + h / 2
    d.line([cx, top, cx, bot], fill=INK, width=width)
    if CROSSING_BARS.get(kind) is None:            # US sc / UK dc -> cross, bar at middle
        d.line([cx - w / 2, cy, cx + w / 2, cy], fill=INK, width=width)
        return
    d.line([cx - w / 2, top, cx + w / 2, top], fill=INK, width=width)      # top cap
    n = CROSSING_BARS[kind]
    for i in range(n):                             # crossing bars, evenly spread below the cap
        by = top + (i + 1) * (h / (n + 1))
        d.line([cx - w / 2, by, cx + w / 2, by], fill=INK, width=width)


# --- grouped stitches: shells, clusters, crossed, arches -------------------
# These come from the standard Spanish/English symbol dictionary.
# The distinguishing feature is WHERE the stems meet:
#   shell / fan     -> one shared BASE, an arc across the tops      (an increase)
#   cluster (dec)   -> one shared TOP, stems splayed at the base    (a decrease)
#   V stitch        -> one shared base, two stems, no arc
#   crossed         -> two stems crossing in an X

def _stem(d, x_top, y_top, x_base, y_base, bars=1, width=2, tick=None):
    d.line([x_top, y_top, x_base, y_base], fill=INK, width=width)
    t = tick if tick is not None else max(2.0, abs(y_base - y_top) * 0.30)
    for b in range(bars):
        by = y_top + (b + 1) * (abs(y_base - y_top) / (bars + 1))
        d.line([x_top - t / 2, by, x_top + t / 2, by], fill=INK, width=1)


def _grouped_size(size, n):
    """Room for a grouped stitch (fan/shell/cluster).

    A fan worked into ONE stitch still has to be legible, and at single-stitch glyph size its
    stems are ~2px apart and collide into a smear. Real charts let a fan visually overlap the
    stitches it arches over, so scale the glyph with the number of legs.
    """
    return size * min(3.2, 0.55 + 0.5 * n)


def draw_shell(d, cx, cy, size, n=5, arc=True, centred_chain=False):
    """n stitches worked into ONE base — a fan/abanico. Arc across the tops."""
    h, spread = size, size * 0.42
    base_y, top_y = cy + h / 2, cy - h / 2
    xs = [cx + (i - (n - 1) / 2) * spread for i in range(n)]
    if centred_chain:                              # 2dc, ch1, 2dc — leave a gap in the middle
        xs = [cx + (i - (n - 1) / 2) * spread for i in range(n)]
    for x in xs:
        _stem(d, x, top_y, cx, base_y, bars=1)
    if arc:
        d.arc([xs[0] - 1, top_y - h * 0.45, xs[-1] + 1, top_y + h * 0.45],
              180, 360, fill=INK, width=2)
    if centred_chain:
        d.ellipse([cx - 2.4, cy + h * 0.10, cx + 2.4, cy + h * 0.30], outline=INK, width=1)


def draw_cluster(d, cx, cy, size, n=3, bars=1):
    """n stitches joined at the TOP — a decrease (pina / cluster)."""
    h, spread = size, size * 0.34
    base_y, top_y = cy + h / 2, cy - h / 2
    for i in range(n):
        x = cx + (i - (n - 1) / 2) * spread
        _stem(d, x, top_y + h * 0.18, cx, base_y, bars=bars)
    d.line([cx - spread * 0.6, top_y, cx + spread * 0.6, top_y], fill=INK, width=2)


def draw_crossed(d, cx, cy, size, bars=1):
    """Two stitches crossing over one another (punto cruzado)."""
    h, w = size, size * 0.30
    top_y, base_y = cy - h / 2, cy + h / 2
    d.line([cx - w, base_y, cx + w, top_y], fill=INK, width=2)
    d.line([cx + w, base_y, cx - w, top_y], fill=INK, width=2)
    for x, off in ((cx - w, 1), (cx + w, -1)):
        for b in range(bars):
            by = top_y + (b + 1) * (h / (bars + 1))
            d.line([x - 2, by, x + 2, by], fill=INK, width=1)


def draw_v(d, cx, cy, size):
    """V stitch: two stitches from one base, no arc."""
    h = size
    base_y, top_y = cy + h / 2, cy - h / 2
    for sgn in (-1, 1):
        _stem(d, cx + sgn * h * 0.30, top_y, cx, base_y, bars=1)


def draw_y(d, cx, cy, size, inverted=False):
    """Y stitch, and its inverse."""
    h, w = size, size * 0.26
    if inverted:
        d.line([cx, cy - h / 2, cx, cy + h * 0.15], fill=INK, width=2)
        for sgn in (-1, 1):
            _stem(d, cx + sgn * w, cy + h * 0.6, cx, cy + h * 0.15, bars=1)
    else:
        d.line([cx, cy + h / 2, cx, cy - h * 0.15], fill=INK, width=2)
        for sgn in (-1, 1):
            _stem(d, cx + sgn * w, cy - h / 2, cx, cy - h * 0.15, bars=1)


def draw_arch(d, cx, cy, size, chains=3):
    """A chain space drawn as an arch (arco), wider with more chains."""
    w = size * (0.45 + 0.16 * chains)
    h = size * 0.55
    d.arc([cx - w, cy + h * 0.4, cx + w, cy + h * 0.4 + h], 180, 360, fill=INK, width=2)
    d.line([cx - w, cy + h * 0.4, cx - w, cy + h * 0.4], fill=INK, width=2)


def draw_ring(d, cx, cy, size):
    r = size * 0.62
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=INK, width=2)


def draw_picot(d, cx, cy, size, rings=1, sc=False):
    """Picot: 'open' is a bare loop; ch3 closes three chain loops; with an sc base."""
    r = size * 0.20
    pos = {1: [(0, 0)], 3: [(-size * 0.24, size * 0.16), (0, -size * 0.16), (size * 0.24, size * 0.16)]}[rings]
    for dx, dy in pos:
        d.ellipse([cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r], outline=INK, width=2)
    if sc:
        pass                                       # placeholder for the small anchor cross


def draw_bullion(d, cx, cy, size):
    """Bullion / rococo: a long stem wrapped in coils."""
    h, w = size, size * 0.24
    d.line([cx, cy - h / 2, cx, cy + h / 2], fill=INK, width=2)
    coils = 6
    for i in range(coils):
        y = cy - h * 0.32 + i * (h * 0.64 / (coils - 1))
        d.line([cx - w / 2, y, cx + w / 2, y - 1.5], fill=INK, width=1)


def draw_ladder(d, cx, cy, size):
    """Ladder stitch: two rails with rungs."""
    h, w = size, size * 0.24
    for sgn in (-1, 1):
        d.line([cx + sgn * w, cy - h / 2, cx + sgn * w, cy + h / 2], fill=INK, width=2)
    for i in range(4):
        y = cy - h * 0.34 + i * (h * 0.68 / 3)
        d.line([cx - w, y, cx + w, y + 2], fill=INK, width=1)


def draw_symbol(d, kind, cx, cy, size=SYM):
    """Draw one crochet symbol. `kind` is a US stitch abbreviation."""
    r = size / 2
    if kind == "CH":                                        # open oval
        d.ellipse([cx - r, cy - r * 0.5, cx + r, cy + r * 0.5], outline=INK, width=2)
    elif kind == "SS":                                      # filled dot
        d.ellipse([cx - r * 0.5, cy - r * 0.5, cx + r * 0.5, cy + r * 0.5], fill=INK)
    elif kind == "PICOT":                                   # small open ring
        d.ellipse([cx - r * 0.4, cy - r * 0.4, cx + r * 0.4, cy + r * 0.4], outline=INK, width=2)
    elif kind == "DCC":
        # The pattern's own terms define "DCC - DC Cluster" as:
        #   *YO, insert hook, YO, pull up a loop, YO, pull through 2 loops* TWICE,
        #   YO and pull through all remaining loops
        # That is a 2-dc cluster whose legs MEET AT THE TOP (a decrease shape). Both halves are
        # worked into the same stitch ("DCC in next ST" consumes one position), so it fits one
        # cell. Drawing the legs meeting at the BASE would read as a fan/shell — an increase —
        # and would be exactly wrong. Cluster = tops together; fan/shell = bases together.
        draw_cluster(d, cx, cy, size * 1.15, 2, bars=1)
    elif kind.startswith("FAN"):                            # n tall stitches worked into ONE stitch
        fm = re.match(r"FAN(\d+)(\w*)", kind)
        n = int(fm.group(1))
        leg = fm.group(2) or "DTR"
        gs = _grouped_size(size, n)                         # scaled so the stems stay distinct
        gr = gs / 2
        for i in range(n):
            lx = cx + (i - (n - 1) / 2) * (gs / n) * 1.15
            d.line([lx, cy - gr, cx, cy + gr], fill=INK, width=2)
            d.line([lx - gs * 0.13, cy - gr, lx + gs * 0.13, cy - gr], fill=INK, width=2)
            nb = CROSSING_BARS.get(leg) or 0
            for b in range(nb):
                by = cy - gr + (b + 1) * (gs / (nb + 1))
                d.line([lx - gs * 0.13, by, lx + gs * 0.13, by], fill=INK, width=1)
    elif kind.startswith("SHELL"):                          # n stitches worked into ONE stitch, arc on top
        m = re.match(r"SHELL(\d+)(C?)", kind)
        draw_shell(d, cx, cy, _grouped_size(size, int(m.group(1))), int(m.group(1)),
                   centred_chain=bool(m.group(2)))
    elif kind.startswith("CLU"):                            # n sts joined at the TOP (decrease)
        m = re.match(r"CLU(\d+)(T?)", kind)
        draw_cluster(d, cx, cy, _grouped_size(size, int(m.group(1))), int(m.group(1)),
                     bars=2 if m.group(2) else 1)
    elif kind.startswith("ARCH"):                           # chain space drawn as an arch
        draw_arch(d, cx, cy, size, int(kind[4:] or 3))
    elif kind == "VST":
        draw_v(d, cx, cy, size)
    elif kind == "YST":
        draw_y(d, cx, cy, size, inverted=False)
    elif kind == "YINV":
        draw_y(d, cx, cy, size, inverted=True)
    elif kind == "XCROSS":
        draw_crossed(d, cx, cy, size, bars=1)
    elif kind == "XCROSSTR":
        draw_crossed(d, cx, cy, size, bars=2)
    elif kind == "RING":
        draw_ring(d, cx, cy, size)
    elif kind == "PICOT3":
        draw_picot(d, cx, cy, size, rings=3)
    elif kind == "PICOT3SC":
        draw_picot(d, cx, cy, size, rings=3, sc=True)
    elif kind == "BULLION":
        draw_bullion(d, cx, cy, size)
    elif kind == "LADDER":
        draw_ladder(d, cx, cy, size)
    elif kind in CROSSING_BARS:
        draw_post(d, cx, cy, size, kind)
    else:
        d.text((cx - 5, cy - 5), kind[:3], fill=HILITE)


# ---------------------------------------------------------------- spacing between symbols
# The stitches were hard to tell apart: neighbouring glyphs' bars met at 1 px and a row read as
# one lattice. Two rules fix that, and neither is a guess:
#   * a symbol that consumes a column of its own keeps its size — the column is its room;
#   * a *guest* symbol (a chain mid-row consumes 0 columns) is the one that gives way, because it
#     has no column to give. It is sized to the space actually free beside it.
# Extents are MEASURED by drawing the glyph into a scratch canvas, not read off a table that would
# rot the first time a glyph changed.

_EXTENT = {}


def glyph_edges(kind, size):
    """(left, right) ink offsets of one glyph from its centre, measured — glyph ink is NOT
    symmetric about the centre (a dc spans -5..+4), and assuming it is left 1 px of overlap.
    The offsets are to the FIRST and LAST ink pixel, so the white between two glyphs is
    (b.cx + b.left) - (a.cx + a.right) - 1."""
    key = (kind, round(size * 2) / 2)
    if key not in _EXTENT:
        box = int(size * 3) + 12
        cx = cy = box / 2
        im = Image.new("RGB", (box, box), BG)
        draw_symbol(ImageDraw.Draw(im), kind, cx, cy, key[1])
        bb = im.convert("L").point(lambda v: 255 if int(v) < 128 else 0).getbbox()
        _EXTENT[key] = (bb[0] - cx, bb[2] - 1 - cx) if bb else (0.0, 0.0)
    return _EXTENT[key]


def glyph_extent(kind, size):
    """Ink width of one glyph at one size, measured. Cached at half-pixel steps."""
    left, right = glyph_edges(kind, size)
    return right - left + 1


def _half_width(kind, size):
    """Half the ink width, taking the wider side — conservative, since ink is lopsided."""
    left, right = glyph_edges(kind, size)
    return max(right, -left) + 1


try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:                             # pragma: no cover - Pillow < 9.1
    RESAMPLE = getattr(Image, "LANCZOS", 1)


def draw_symbol_fitted(img, d, kind, cx, cy, size, max_w=None):
    """Draw one symbol, compressing it sideways if it is wider than the space it may use.

    Only the width is squeezed: a 5-dtr fan is wider than a single stitch column, and squashing
    its *height* too would make a tall stitch look short. The complaint was that the stitches
    ran into each other; the fix is to give each one its own room, not to shrink the stitches.
    """
    if max_w is None or max_w >= glyph_extent(kind, size):
        draw_symbol(d, kind, cx, cy, size)
        return
    pad = 4
    box = int(size * 3) + 2 * pad
    tile = Image.new("RGB", (box, box), BG)
    draw_symbol(ImageDraw.Draw(tile), kind, box / 2, box / 2, size)
    bb = tile.convert("L").point(lambda v: 255 if int(v) < 128 else 0).getbbox()
    if not bb:
        return
    ink = tile.crop(bb)
    w = max(2, int(round(max_w)))
    ink = ink.resize((w, ink.height), RESAMPLE)     # sideways only
    # Resampling leaves a 1 px grey halo that the ink measurement would count as ink, and a
    # squeezed symbol would then come back within a pixel of its neighbour. Hard-edge it.
    ink = ink.point(lambda v: 255 if int(v) >= 128 else 0)
    # Paste from an INTEGER centre with integer division, never round(cx - w/2): that rounding
    # depends on where the caller's origin happens to be, so the same symbol landed one pixel
    # apart on the print page and in the PNG (caught by the pixel-identity test).
    img.paste(ink, (int(cx) - w // 2, int(cy) - ink.height // 2))


def place_row(items, cell_w=CELL_W, sym=SYM, clearance=CLEARANCE):
    """Each symbol's centre and the width it may use, so that no two symbols ever touch.

    Returns [(kind, x_px, cx_px, size, max_w)] with x measured from the row's first column and
    max_w None when the glyph already fits. Where neighbours cannot all have their natural width,
    they give way in proportion — the stitch keeps its height, only its spread narrows.
    """
    placed, x = [], 0.0
    for kind, span in items:
        if kind.startswith("_"):                     # _SKIP and friends are spacing, not symbols
            x += span * cell_w
            continue
        placed.append({"kind": kind, "span": span, "x": x,
                       "cx": float(round(x + span * cell_w / 2)),   # integer centre: see paste
                       "size": float(sym), "half": _half_width(kind, sym)})
        x += span * cell_w

    factor = [1.0] * len(placed)
    for i in range(len(placed) - 1):
        a, b = placed[i], placed[i + 1]
        d = b["cx"] - a["cx"]
        need = a["half"] + b["half"] + clearance
        if d <= 0 or need <= d:
            continue
        k = max(0.1, (d - clearance) / (need - clearance))
        factor[i] = min(factor[i], k)
        factor[i + 1] = min(factor[i + 1], k)

    out = []
    for p, k in zip(placed, factor):
        if k >= 0.999:
            out.append((p["kind"], p["x"], p["cx"], p["size"], None))
        else:
            out.append((p["kind"], p["x"], p["cx"], p["size"],
                        max(3.0, glyph_extent(p["kind"], p["size"]) * k)))
    return out


def _placed_width(item):
    kind, _x, _cx, size, max_w = item
    return glyph_extent(kind, size) if max_w is None else max_w


def min_gap_in_row(placed):
    """The thinnest white space between two neighbouring symbols in a placed row, in px."""
    gaps = []
    for a, b in zip(placed, placed[1:]):
        gaps.append((b[2] - _placed_width(b) / 2) - (a[2] + _placed_width(a) / 2))
    return min(gaps, default=float("inf"))


def cramped_rows(rows, cell_w=CELL_W, sym=SYM, clearance=CLEARANCE, floor=0.6):
    """Rows where a symbol had to be squeezed past `floor` of its natural width.

    A 5-dtr fan is wider than one stitch column, so a row of "fan, ch-1, fan" cannot give every
    symbol its own room at any pitch this chart uses. Those rows are reported rather than quietly
    drawn on top of each other: a wider column is the fix, and that is the user's call.
    """
    out = {}
    for rn, items in rows.items():
        placed = place_row(items, cell_w=cell_w, sym=sym, clearance=clearance)
        worst = {}
        for kind, _x, _cx, size, max_w in placed:
            if max_w is None:
                continue
            k = max_w / max(glyph_extent(kind, size), 1)
            if k < floor:
                worst[kind] = min(worst.get(kind, 9), k)
        if worst:
            out[rn] = worst
    return out


# ---------------------------------------------------------------- parsing
def stated_count(text):
    m = re.findall(r'\((\d+)\)', text)
    return int(m[-1]) if m else None

# Each rule: (regex on one clause) -> (symbol, how many columns it consumes)
#  None as the span means "whatever the count says is left over" (a fill).
CLAUSE_RULES = [
    (r"^CH3 \(counts as 1st?\s*DC\)",                     ("DC", 1)),
    (r"^CH4 \(counts as 1st?\s*TRC\)",                    ("TRC", 1)),
    (r"^CH5 \(counts as 1st?\s*DTR\)",                    ("DTR", 1)),
    (r"^CH1 \(doesn'?t count",                            ("_SKIP", 0)),
    (r"^SC in 1st? ST$",                                  ("SC", 1)),
    (r"^SC in same ST$",                                  ("SC", 1)),
    (r"^SC in next (\d+)ST$",                             ("SC", None)),   # n stitches
    (r"^SC in last (\d+)\s?ST$",                          ("SC", None)),
    (r"^SC in next ST$",                                  ("SC", 1)),
    (r"^SC in last ST$",                                  ("SC", 1)),
    (r"^DC in last ST$",                                  ("DC", 1)),
    (r"^DC in next 2ST$",                                 ("DC", 2)),
    (r"^TRC in last 3ST$",                                ("TRC", 3)),
    (r"^TRC in next 2ST$",                                ("TRC", 2)),
    (r"^DTR in next 2ST$",                                ("DTR", 2)),
    (r"^DTR in next ST$",                                 ("DTR", 1)),
    (r"^DTR in last 3ST$",                                ("DTR", 3)),
    (r"^DCC in next to last ST$",                         ("DCC", 1)),
    (r"^DCC in next ST$",                                 ("DCC", 1)),
    (r"^skip (\d+)ST$",                                   ("_SKIP", None)),
    (r"^skip next (\d+)ST$",                              ("_SKIP", None)),
    (r"^skip 1SC$",                                       ("_SKIP", 1)),
    (r"^skip CH2$",                                       ("_SKIP", 2)),
    (r"^CH(\d+) & skip 2ST$",                             ("CH2SKIP", 2)),
    (r"^(\d+)(DTR|TRC|DC|HDC|SC) in next ST$",            ("_FAN_N", None)),
    (r"^(\d+)SC around (?:CH cord|cord) from prev row$",  ("_CLUSTER", 1)),
    (r"^1SC in CH space from prev row$",                  ("SC", 1)),
    (r"^starting from \d+(?:nd|st|rd|th) ST$",            ("_NOP", 0)),
    (r"^FO$",                                             ("_NOP", 0)),
    (r"^Fasten off$",                                     ("_NOP", 0)),
    (r"^turn$",                                           ("_NOP", 0)),
    (r"^cut and weave in your tail$",                     ("_NOP", 0)),
    (r"^and weave in your tail$",                         ("_NOP", 0)),
    (r"^(\d+)SC in next (\d+)ST$",                        ("_NSC", None)),
    (r"^(SC|DC|TRC|DTR) in (?:last|next) (\d+)ST$",       ("_MULTI", None)),
    (r"^(\d+)(SC|DC|TRC|DTR) around CH\d* ?from prev row$",("_CLUSTER", 1)),
    (r"^(\d+)(SC|DC|TRC|DTR) around cord from prev row$", ("_CLUSTER", 1)),
    (r"^SC in CH space from prev row$",                   ("SC", 1)),
    (r"^DC in next ST$",                                  ("DC", 1)),
    (r"^skip SC$",                                        ("_SKIP", 1)),
    (r"^skip CH\d+$",                                     ("_SKIP", 0)),   # a chain space is not a stitch
    (r"^CH(\d+)$",                                        ("_CHAINSPACE", 0)),
]


CHAIN_TO_STITCH = {1: "SC", 2: "HDC", 3: "DC", 4: "TRC", 5: "DTR", 6: "TRTR"}

# Clauses the LLM fallback has taught this process, keyed by their exact text:
#   clause -> (kind, span). pattern_to_chart fills this in when a clause is not recognised, and
# parse_clause consults it FIRST, so a learned reading still goes through the SAME verifier as
# everything else — a learned clause that does not reconcile is refused like any other. Learned
# entries are session-local and never persisted.
LEARNED = {}


def parse_clause(c, at_row_start=False):
    c = c.strip().rstrip('.')
    if c in LEARNED:
        return [LEARNED[c]]
    for pat, (kind, span) in CLAUSE_RULES:
        m = re.match(pat, c)
        if not m:
            continue
        # kind-specific handling comes FIRST — these may legitimately carry span=None
        if kind == "_MULTI":                            # "DC in last 3ST" -> 3 separate stitches
            return [(m.group(1), 1)] * int(m.group(2))
        if kind == "_NSC":                              # "1SC in next 2ST" -> 2 stitches
            return [("SC", 1)] * int(m.group(2))
        if kind == "_CHAINSPACE":
            # A bare CHn at the START of a row is the turning chain: CH3 and taller counts as
            # the first stitch of the row, CH1/CH2 do not count (the pattern says so explicitly).
            # Mid-row, a CHn is a chain space and consumes no stitch position at all.
            n = int(m.group(1))
            if at_row_start and n >= 3:
                return [(CHAIN_TO_STITCH.get(n, "DC"), 1)]
            return [("CH", 0)]
        if kind == "CH2SKIP":
            return [("CH", 1), ("_SKIP", span - 1)]     # chain + the stitches it jumps
        if kind == "_FAN_N":                            # n stitches worked into ONE stitch
            n, st = int(m.group(1)), m.group(2)
            return [(st, 1)] if n == 1 else [(f"FAN{n}{st}", 1)]
        if kind == "_CLUSTER":                          # several sts around one cord/space
            st = m.group(2) if m.lastindex and m.lastindex >= 2 else "SC"
            return [(st if st in ("SC", "DC", "TRC", "DTR") else "SC", 1)]
        if kind == "_NOP":
            return []
        if kind == "_SKIP":
            n = span if span is not None else int(m.group(1))
            return [] if n == 0 else [("_SKIP", n)]
        if span is None:                                # explicit "N stitches"
            n = int(m.group(1))
            if kind in ("SC", "DC", "TRC", "DTR"):
                return [(kind, 1)] * n
            return [("_SKIP", n)]
        return [(kind, span)]
    return None                                # unparsed -> caller must refuse


def parse_row(text, count_hint=None):
    """-> (items, count, notes) or (None, count, [reason]) if it cannot be made to add up.

    `count_hint` is the stitch count carried forward from the previous row, used when the
    pattern does not print a count on this row. On a straight piece (constant width) this is
    a real check: the row must still consume exactly that many stitch positions.
    """
    notes = []
    count = stated_count(text)
    if count is None:
        count = count_hint
    if count is not None:
        notes.append("printed" if stated_count(text) else f"carried fwd ({count})")
    body = re.sub(r'\(\d+\)\s*$', '', text).strip().rstrip('.')
    body = re.sub(r'\(every CH2 space[^)]*\)', '', body).strip().rstrip('.')
    body = re.sub(r'\(here ST count will be off[^)]*\)', '', body)
    body = re.sub(r'\(counts as 1st?[^)]*\)', lambda m: m.group(0), body)

    # pull out a repeat block: "repeat *A, B, C* until N ST remain"
    rr = re.search(r'repeat \*(.+?)\* until (\d+)ST remain', body)
    if not rr:
        rr2 = re.search(r'Repeat \*(.+?)\* until end of row', body)
        return _parse_linear(body, count, notes) if not rr2 else (None, count, ["burst border row not modelled"])

    block_txt, remain = rr.group(1), int(rr.group(2))
    block = []
    for c in re.split(r',(?!\d)', block_txt):
        got = parse_clause(c)
        if got is None:
            return None, count, [f"unparsed clause in repeat: {c.strip()!r}"]
        block += got
    block_span = sum(s for _, s in block)
    if block_span == 0:
        return None, count, ["repeat block consumes 0 columns"]

    head_txt = body[:rr.start()].strip().rstrip(',').strip()
    tail_txt = body[rr.end():].strip().rstrip('.').strip().lstrip(',').strip()

    head, tail = [], []
    for i, c in enumerate(re.split(r',(?!\d)', head_txt) if head_txt else []):
        got = parse_clause(c, at_row_start=(i == 0))
        if got is None:
            return None, count, [f"unparsed clause before repeat: {c.strip()!r}"]
        head += got
    for c in re.split(r',(?!\d)', tail_txt) if tail_txt else []:
        got = parse_clause(c)
        if got is None:
            return None, count, [f"unparsed clause after repeat: {c.strip()!r}"]
        tail += got

    head_span = sum(s for _, s in head)
    tail_span = sum(s for _, s in tail)
    if count is None:
        return None, None, ["no stitch count to verify against"]
    left = count - head_span - tail_span
    if left % block_span != 0:
        return None, count, [f"{left} columns left is not a multiple of the {block_span}-column repeat"]
    n = left // block_span
    notes.append(f"repeat x{n}")
    return head + block * n + tail, count, notes


def _parse_linear(body, count, notes):
    """Rows with no repeat block: plain runs, or 'all remaining' fills."""
    items, filled = [], False
    for i, c in enumerate(re.split(r',(?!\d)', body)):
        c = c.strip()
        if not c:
            continue
        if re.search(r'all remaining', c):
            kind = "DC" if c.startswith("BLO") or c.startswith("BLODC") else \
                   "SC" if c.startswith("FLO") or c.startswith("FLOSC") or c.startswith("SC") else "DC"
            items.append(("_FILL", kind))
            filled = True
            continue
        if re.match(r'^(BLODC|DC) in next and in all remaining', c):
            items.append(("_FILL", "DC")); filled = True; continue
        got = parse_clause(c, at_row_start=(i == 0))
        if got is None:
            if re.search(r'all remaining', c):
                items.append(("_FILL", "SC")); filled = True; continue
            return None, count, [f"unparsed clause: {c.strip()!r}"]
        items += got
    if not filled and count is not None:
        span = sum(s for _, s in items)
        if span != count:
            return None, count, [f"clauses total {span} but the row says {count}"]
    elif not filled:
        return None, count, ["no stitch count and no fill"]
    return items, count, notes


def resolve_fill(items, count):
    """Turn a _FILL placeholder into the exact number of stitches the count demands."""
    out = []
    for idx, (kind, span) in enumerate(items):
        if kind != "_FILL":
            out.append((kind, span))
            continue
        others = sum(s for k, s in items if k != "_FILL")
        n_fills = sum(1 for k, _ in items if k == "_FILL")
        each = (count - others) // n_fills
        out += [(span, 1)] * each            # here span carries the symbol kind
    return out


# ---------------------------------------------------------------- the pattern
# A small SYNTHETIC sample pattern, shipped so the demo and the test suite have something to run
# on without anyone's real pattern. It is not a published design; it exercises the parser at a
# manageable width — printed counts, an "all remaining" fill, a repeat block, a fan row, a
# cluster/skip row, and a control-flow "repeat row N".
ROWS = {
 1: "SC in 2nd CH from hook and in all remaining CH. (36)",
 2: "CH3 (counts as 1st DC), DC in next and in all remaining ST until next to last one, DC in last ST. (36)",
 3: "CH1 (doesn't count as ST here and throughout), SC in 1st ST, SC in all remaining ST. (36)",
 4: "CH1, SC in 1st ST, SC in next 2ST, repeat *SC in next ST, skip 3ST, 5DTR in next ST, CH1, 5DTR in next ST, skip 3ST, SC in next ST* until 3ST remain, SC in last 3ST. (36)",
 5: "CH1, SC in 1st ST, SC in all remaining ST. (36)",
 6: "repeat row 3",
 7: "CH3, starting from 2nd ST repeat *DCC in next ST, CH2 & skip 2ST* until 2ST remain, DCC in next to last ST, DC in last ST. (36)",
 8: "CH1, SC in 1st ST, SC in all remaining ST. (36)",
 9: "CH1, SC in 1st ST, SC in next 2ST, repeat *SC in next ST, skip 3ST, 5DTR in next ST, CH1, 5DTR in next ST, skip 3ST, SC in next ST* until 3ST remain, SC in last 3ST. (36)",
 10: "CH3 (counts as 1st DC), DC in next and in all remaining ST until next to last one, DC in last ST. (36)",
}

if __name__ == "__main__":
    print(f"{'row':>4}  {'stated':>7}  {'parsed':>7}  verdict")
    parsed = {}
    for n in sorted(ROWS):
        m_rep = re.match(r'^repeat row (\d+)$', ROWS[n], re.I)
        if m_rep and int(m_rep.group(1)) in parsed:
            src = int(m_rep.group(1))
            parsed[n] = parsed[src]
            print(f"{n:>4}  {'-':>7}  {'copy':>7}  OK   control flow (copy of row {src})")
            continue
        items, count, notes = parse_row(ROWS[n])
        if items is None:
            print(f"{n:>4}  {str(count):>7}  {'-':>7}  REFUSED — {'; '.join(notes)}")
            continue
        items = resolve_fill(items, count) if count else items
        total = sum(s for _, s in items)
        ok = (count is None) or (total == count)
        parsed[n] = items
        verdict = "OK" if ok else f"MISMATCH ({total} vs {count})"
        print(f"{n:>4}  {str(count):>7}  {total:>7}  {verdict}   {', '.join(notes)}")

    import json
    json.dump({str(k): v for k, v in parsed.items()}, open("rows.json", "w"))
    print(f"\n{len(parsed)} rows parsed and verified -> rows.json")
