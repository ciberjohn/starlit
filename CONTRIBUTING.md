# Contributing

Thanks for looking. Starlit is a small, focused tool; the design rules below are load-bearing, so
changes that break them will be declined even if they "work".

## The two rules

1. **The model proposes, the arithmetic disposes.** Nothing is drawn unless its row consumes
   exactly the stitch count the pattern established. If you extend the parser or the LLM fallback,
   the new reading must go back through the same verifier — never bypass it.
2. **No pattern PDFs, ever.** They are the designer's copyright. Use `sample/pattern.pdf` (the
   synthetic fixture) for anything you need to test against.

## Getting set up

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt pytest
make sample        # regenerate sample/rows.json + sample/pattern.pdf from the synthetic pattern
```

You also need **poppler-utils** (`pdftotext`).

## Before you open a PR

```bash
./venv/bin/python -m pytest -q
./venv/bin/python tests/paginate_pdf_test.py
```

Both must pass. The A4 test checks the printed sheet against the on-screen chart pixel for pixel, so
if you change glyph geometry or placement, expect it to catch you.

## Style

- Keep the comments honest: they explain *why*, and several record a bug that was actually hit.
  If you change a value, change the comment that explains it.
- Verify a chart by its ink, not by eye — a vision read of a blank or malformed chart has produced
  confident, detailed nonsense before.
