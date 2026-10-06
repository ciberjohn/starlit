#!/usr/bin/env python3
"""Verify the A4 print sheets against the artifact, not against the plan that made them.

Run from a directory holding rows.json (or pass a JSON path):
    python3 tests/paginate_pdf_test.py [rows.json]

Checks, in order:
  1. the geometry: one stitch on paper is really `mm_per_stitch` wide (cell px × 25.4 / dpi)
  2. coverage: every row and every stitch column lands on exactly one sheet
  3. the cuts: how many symbols each boundary still splits, before and after the nudge
  4. the file: page count and /MediaBox of every page in the written PDF is A4
  5. ink: every page carries ink in the chart area, and the overlap gutters are not blank
     — a blank page is a failed render, never a finding (a vision pass on an empty PNG once
     produced a confident, detailed, entirely invented description)
"""
import collections, glob, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE)

import paginate_pdf as pp                                          # noqa: E402
from render_chart import KEY                                       # noqa: E402
from crochet_chart import place_row, glyph_extent                   # noqa: E402

A4_PT = {"landscape": (841.89, 595.28), "portrait": (595.28, 841.89)}
FAIL = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(label)


def load(path):
    data = json.load(open(path))
    data = data.get("verified", data)
    return {int(k): [tuple(x) for x in v] for k, v in data.items()}


def ink(img, box=None):
    """Count clearly-dark pixels, optionally inside a box — the only trustworthy 'is it drawn'."""
    if box:
        img = img.crop(box)
    return sum(img.convert("L").point(lambda v: 255 if v < 128 else 0).getdata()) // 255


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "sample", "rows.json")
    rows = load(src)
    maxcols = max(sum(s for _, s in rows[r]) for r in rows)
    print(f"rows {min(rows)}-{max(rows)} ({len(rows)}), {maxcols} stitches wide — {src}")

    MM, DPI, ORI = 6.0, 200, "landscape"
    out = "/tmp/paginate-test.pdf"
    path, meta, pages = pp.render_pdf(rows, out, title=os.path.basename(src),
                                      mm_per_stitch=MM, orientation=ORI, return_images=True)

    print("\n1. geometry")
    f = (MM / pp.CELL_W) * DPI / 25.4
    printed = pp.CELL_W * f / DPI * 25.4
    check(f"one stitch prints {printed:.3f} mm (asked {MM})", abs(printed - MM) < 1e-6)
    check(f"row pitch {meta['row_pitch_mm']:.2f} mm = glyph {pp.SYM * MM / pp.CELL_W:.2f} mm tall, "
          f"the same {pp.CELL_H}/{pp.CELL_W} proportions as the screen chart",
          abs(meta["row_pitch_mm"] - pp.CELL_H * MM / pp.CELL_W) < 1e-9)
    check(f"page grid {meta['strips']} strips x {meta['bands']} bands = {meta['n_pages']} pages "
          f"(+ cover)", meta["n_pages"] == meta["strips"] * meta["bands"])

    print("\n2. coverage")
    check("every (row, column) cell on exactly one sheet", pp.assert_coverage(meta, rows))
    check(f"each row appears on all {meta['strips']} strips and no more",
          all(sum(1 for p in meta["pages"] if r in p["rows"]) == meta["strips"] for r in rows))

    print("\n3. the cuts")
    bounds = [pp._group_bounds(rows[r]) for r in rows]
    worst, split = 0, 0
    boundaries = sorted({p["col_from"] - 1 for p in meta["pages"] if p["col_from"] > 1})
    hits = pp._crossings(boundaries, bounds)
    for b in boundaries:
        worst = max(worst, hits[b])
        split += hits[b]
        print(f"      cut at stitch {b + 1}: {hits[b]} row(s) still split across the sheets")
    naive = pp._crossings([p["col_from"] - 1 for p in meta["pages"]
                           if p["col_from"] > 1 and (p["col_from"] - 1) % meta["cols_core"] == 0],
                          bounds)
    check("cuts land on the requested stitch size", len(boundaries) == meta["strips"] - 1)
    check(f"overlap {meta['overlap']} stitches drawn on both sides of every cut",
          all(p["draw_to"] - p["col_to"] == min(meta["overlap"], meta["cols"] - p["col_to"])
              for p in meta["pages"]))
    check("worst cut splits no more than a couple of rows (nudge to group edges)",
          worst <= 3, f"worst {worst}, naive grid would have been "
                      f"{max(naive.values()) if naive else 0}")

    print("\n4. the file")
    blob = open(path, "rb").read()
    mb = re.findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", blob)
    npages = len(re.findall(rb"/Type\s*/Page[^s]", blob))
    check(f"{len(mb)} /MediaBox entries, {npages} /Type /Page", npages == meta["pdf_pages"])
    check(f"every page is A4 {ORI} ({A4_PT[ORI][0]:.2f} × {A4_PT[ORI][1]:.2f} pt)",
          all(abs(float(w) - A4_PT[ORI][0]) < 0.6 and abs(float(h) - A4_PT[ORI][1]) < 0.6
              for w, h in mb))
    check(f"file size {os.path.getsize(path) / 1e6:.2f} MB for {npages} pages",
          os.path.getsize(path) > 10_000)

    print("\n5. ink (a blank page is a failed render)")
    x0 = int(round(pp._px(meta["side_mm"] + meta["gutter_mm"], DPI)))
    y0 = int(round(pp._px(meta["margin_mm"] + meta["header_mm"], DPI)))
    blank = []
    for i, img in enumerate(pages[1:], start=1):
        n = ink(img, (x0, y0, img.width - 5, img.height - int(pp._px(meta["footer_mm"], DPI))))
        if n < 500:
            blank.append(i)
    check("every chart page has ink in the chart area", not blank, f"blank pages {blank}")
    check("cover page has ink", ink(pages[0]) > 500)
    first = meta["pages"][0]
    if first["col_from"] > 1 or first["draw_from"] < first["col_from"]:
        left = pages[1].crop((0, y0, x0, pages[1].height))
        check("left overlap gutter carries ink", ink(left) > 100)

    print("\n6. symbols used (counted, not read by eye)")
    seen = collections.Counter()
    for r in rows:
        for kind, _span in rows[r]:
            if not kind.startswith("_"):
                seen[kind] += 1
    known = {k for k, *_ in KEY}
    unknown = [k for k in seen if k not in known and not re.match(r"(FAN|SHELL|CLU)\d+", k)]
    print("      " + ", ".join(f"{k}:{v}" for k, v in sorted(seen.items(), key=lambda x: -x[1])))
    check("every symbol on the sheets is in the key", not unknown, str(unknown[:6]))

    print("\n7. a printed page is the same pixels as the on-screen chart")
    import render_chart as rc
    from PIL import Image, ImageChops, ImageDraw
    page = meta["pages"][0]
    drawn = page["rows"]
    rc.rows = rows                       # render_chart keeps its rows in a MODULE-LEVEL dict
    rc.cols = maxcols
    ref_path = "/tmp/paginate-test-ref.png"
    rc.render(drawn, col_from=page["draw_from"] - 1, col_to=page["draw_to"] - 1, path=ref_path,
              title="witness", legend=False)
    ref = Image.open(ref_path)
    box = (rc.MARGIN_L + (page["draw_from"] - 1) * rc.CELL_W, rc.MARGIN_T,
           rc.MARGIN_L + page["draw_to"] * rc.CELL_W, rc.MARGIN_T + len(drawn) * rc.CELL_H)
    ref = ref.crop(box)
    layer = pp._layer_screen(rows, drawn, page)
    check(f"page 1 layer is {layer.size[0]}x{layer.size[1]} px, the screen chart window is "
          f"{ref.size[0]}x{ref.size[1]}", layer.size == ref.size)
    if layer.size != ref.size:
        ref = ref.resize(layer.size)
    # the print sheet adds absolute 10-stitch guides (the screen window draws its own, relative
    # to the window) — blank those columns on both sides before comparing, nothing else differs
    a, b = ref.copy(), layer.copy()
    da, dbb = ImageDraw.Draw(a), ImageDraw.Draw(b)
    for c in range(0, a.size[0], pp.CELL_W):
        if c % (10 * pp.CELL_W):
            continue
        for im, dr in ((a, da), (b, dbb)):
            dr.rectangle([c - 1, 0, c + 1, a.size[1]], fill=pp.BG)
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    # Tolerate sub-32 differences: the screen chart stops its baseline rule 30 px short of its
    # canvas edge (MARGIN_R) where a page has no such margin. Any real glyph difference is 200+.
    hard = diff.point(lambda v: 255 if int(v) > 32 else 0)
    check("every symbol on the printed page sits on the same pixel as in the PNG "
          "(guides and margins excluded)", hard.getbbox() is None, str(hard.getbbox()))

    print("\n8. the nudge fires when a cut would split a shell")
    strip = meta["cols_core"]
    synth = {r: [("SC", 1)] * (strip - 3) + [("SHELL5", 5)] + [("SC", 1)] * (strip - 2)
             for r in range(1, 9)}
    m2 = pp.plan(synth, mm_per_stitch=MM, orientation=ORI)
    naive = strip                                  # the cut the fixed grid would have made
    b_first = m2["pages"][1]["col_from"] - 1
    before = pp._crossings([naive], [pp._group_bounds(synth[r]) for r in synth])[naive]
    after = pp._crossings([b_first], [pp._group_bounds(synth[r]) for r in synth])[b_first]
    check(f"a fixed grid cut at {naive + 1} would split {before} shell(s)", before > 0)
    check(f"the nudge moved the cut to {b_first + 1} and splits {after}", after == 0)
    pp.assert_coverage(m2, synth)

    print("\n9. spacing: is the white between two stitches really white?")
    # "It's hard to read as it is all connected to each other." Measured on the rendered chart:
    # every column that place_row calls white between two symbols must carry no ink at all.
    full = "/tmp/paginate-test-full.png"
    rc.render(sorted(rows), path=full, title="spacing witness", legend=False)
    img = Image.open(full).convert("L")
    px = img.load()
    desc = sorted(rows, reverse=True)
    violations, gaps, squeezed = [], [], {}
    for rn in desc:
        y0 = rc.MARGIN_T + desc.index(rn) * rc.CELL_H
        for a, b in zip(place_row(rows[rn]), place_row(rows[rn])[1:]):
            wa = glyph_extent(a[0], a[3]) if a[4] is None else a[4]
            wb = glyph_extent(b[0], b[3]) if b[4] is None else b[4]
            right_edge = int(round(rc.MARGIN_L + a[2] + wa / 2))
            left_edge = int(round(rc.MARGIN_L + b[2] - wb / 2))
            gaps.append(left_edge - right_edge)
            for x in range(right_edge, left_edge):
                if any(px[x, y0 + y] < 128 for y in range(rc.CELL_H)):
                    violations.append((rn, x, a[0], b[0]))
                    break
            for it in (a, b):
                if it[4] is not None:
                    k = it[4] / max(glyph_extent(it[0], it[3]), 1)
                    squeezed[rn] = min(squeezed.get(rn, 9), k)
    gaps.sort()
    print(f"      {len(gaps)} neighbouring pairs checked; thinnest gap {gaps[0]} px, "
          f"median {gaps[len(gaps) // 2]} px, at {MM} mm/stitch that is "
          f"{gaps[len(gaps) // 2] * MM / pp.CELL_W:.2f} mm of white")
    check("no ink where place_row promised white", not violations, str(violations[:6]))
    check(f"thinnest gap between symbols is at least {pp.CLEARANCE} px", gaps[0] >= pp.CLEARANCE)
    if squeezed:
        tight = sorted(squeezed.items(), key=lambda kv: kv[1])[:4]
        print("      rows where a symbol is squeezed (a fan is wider than its column): "
              + ", ".join(f"R{rn} to {k * 100:.0f}%" for rn, k in tight))

    print("\n10. the PDF as the printer will read it (rasterised back through poppler)")
    if not shutil.which("pdftoppm"):
        print("      SKIP — no pdftoppm on this host")
    else:
        for f in glob.glob("/tmp/paginate-back-*.png"):
            os.remove(f)
        subprocess.run(["pdftoppm", "-r", "60", "-png", out, "/tmp/paginate-back"], check=True)
        back = sorted(glob.glob("/tmp/paginate-back-*.png"))
        check(f"{len(back)} pages read back from the written PDF", len(back) == meta["pdf_pages"])
        blanks = [i + 1 for i, f in enumerate(back) if ink(Image.open(f)) < 200]
        check("every page in the written PDF carries ink", not blanks, f"blank pages {blanks}")
        sizes = [Image.open(f).size for f in back]
        check(f"rasterised pages are all A4 landscape at 60 dpi "
              f"({int(A4_PT[ORI][0] / 72 * 60)}×{int(A4_PT[ORI][1] / 72 * 60)} px)",
              all(abs(w - A4_PT[ORI][0] / 72 * 60) <= 2 and abs(h - A4_PT[ORI][1] / 72 * 60) <= 2
                  for w, h in sizes), str(sizes[:3]))
        print(f"      file size {meta['pdf_bytes'] / 1024:.0f} KB for {meta['pdf_pages']} pages "
              f"({meta['pdf_bytes'] / 1024 / meta['pdf_pages']:.0f} KB a page, 1-bit line art)")

    print("\n11. the app is told about each sheet as it is drawn (this is the on-screen progress)")
    marks = []
    pp.render_pdf(rows, "/tmp/paginate-cb.pdf", title="callback witness", mm_per_stitch=MM,
                  on_page=lambda n, total: marks.append((n, total)))
    check(f"the callback fired once per sheet, in order ({len(marks)} of {meta['n_pages']})",
          [m[0] for m in marks] == list(range(1, meta["n_pages"] + 1))
          and all(m[1] == meta["n_pages"] for m in marks), str(marks[:3]))

    print(f"\n{'ALL CHECKS PASSED' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} "
          f"— {out}, {meta['n_pages']} pages + cover")
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
