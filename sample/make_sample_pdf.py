#!/usr/bin/env python3
"""Write sample/pattern.txt and sample/pattern.pdf from the synthetic pattern in crochet_chart.

No third-party dependency: the PDF is assembled by hand (Helvetica text stream, correct xref
offsets). A wide page keeps each instruction on one line so `pdftotext -layout` does not wrap a
clause mid-way.

    python3 sample/make_sample_pdf.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))
from crochet_chart import ROWS                                  # noqa: E402


def instruction_lines():
    """ROWS as the PDF a designer would hand over: 'Row N (RS|WS): ...', row 1 right side."""
    out = []
    for i, n in enumerate(sorted(ROWS)):
        side = "RS" if i % 2 == 0 else "WS"
        out.append(f"Row {n} ({side}): {ROWS[n]}")
    return out


def build_pdf(lines, path):
    def esc(s):
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    body = ["BT", "/F1 9 Tf", "12 TL", "30 760 Td"]
    for ln in lines:
        body.append(f"({esc(ln)}) Tj T*")
    body.append("ET")
    stream = "\n".join(body).encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1600 792] /Resources "
        b"<< /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref}\n%%EOF\n").encode()
    with open(path, "wb") as fh:
        fh.write(out)
    return path


if __name__ == "__main__":
    lines = instruction_lines()
    txt = os.path.join(HERE, "pattern.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("Synthetic sample pattern (not a published design) — Starlit demo fixture.\n")
        fh.write("Rows alternate right side / wrong side.\n\n")
        fh.write("\n".join(lines) + "\n")
    pdf = build_pdf(lines, os.path.join(HERE, "pattern.pdf"))
    print(f"wrote {txt}\nwrote {pdf} ({os.path.getsize(pdf)} bytes, {len(lines)} rows)")
