#!/usr/bin/env python3
"""
PDF -> crochet chart.  The engine behind the web app.

  pdftotext -layout  ->  find "Row N (WS|RS):"  ->  join wrapped lines
                     ->  parse + verify against the row's own stitch count
                     ->  render

A row is only drawn if it reconciles with its stated stitch count. Rows that do not
are reported as UNVERIFIED with the reason, never silently rendered.
"""
import re, subprocess, sys, json
from crochet_chart import parse_row, resolve_fill, LEARNED


def pdf_text(pdf):
    r = subprocess.run(["pdftotext", "-layout", pdf, "-"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {r.stderr.strip()}")
    return r.stdout


def normalise(text):
    """Typographic punctuation to ASCII before parsing.

    pdftotext turns "doesn't" into "doesn’t" and quotes into curly ones, so a rule that expects a
    straight apostrophe never fires on a real designer's PDF and the row is reported unparsed. The
    pattern text is the authority; the punctuation it arrives in is not. Also folds the non-breaking
    spaces that justify the line endings in some PDFs.
    """
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'),
                 ("\u2013", "-"), ("\u2014", "-"), ("\u00a0", " ")):
        text = text.replace(a, b)
    return text


def find_rows(text):
    """Collect 'Row N (WS|RS):' blocks, joining wrapped lines. First occurrence wins."""
    text = normalise(text)
    rows, cur, buf = {}, None, []
    for ln in text.split("\n"):
        m = re.match(r'\s*Row (\d+)\s*\((WS|RS)\)\s*:?\s*(.*)$', ln)
        if m:
            if cur is not None and cur not in rows and buf:
                rows[cur] = " ".join(buf)
            cur, buf = int(m.group(1)), [m.group(3).strip()]
            continue
        if cur is None:
            continue
        s = ln.strip()
        if not s:
            continue
        if re.match(r'^(Row \d+|Border|Joining|Pattern Notes|Assembly|Finishing)', s):
            if cur not in rows and buf:
                rows[cur] = " ".join(buf)
            cur, buf = None, []
            continue
        buf.append(s)
    if cur is not None and cur not in rows and buf:
        rows[cur] = " ".join(buf)
    return rows


def chain_space_widths(text):
    """How many stitch positions each chain space in this row arches over.

    Row 13 says "CH2, skip 3ST" — that chain-2 space sits above 3 skipped stitches. When row 14
    then says "skip CH2", it is skipping that space, so it must consume those same 3 positions.
    Recorded as (chain size, width) pairs so a "skip CH3" cannot consume a CH2's width — chain
    spaces must be matched by size, and a space that spanned no skipped stitches consumes 0.
    """
    return [(int(sz), int(n))
            for sz, n in re.findall(r'CH(\d+),?\s*skip (?:next )?(\d+)\s*ST', text)]


def _resolve_skips(text, prev_widths):
    remaining = list(prev_widths)

    def _sub(mo):
        size = int(mo.group(1))
        for i, (sz, width) in enumerate(remaining):
            if sz == size:
                remaining.pop(i)
                return f"skip {width}ST"
        return "skip 0ST"          # a chain space arching over no skipped stitches
    return re.sub(r'skip CH(\d+)', _sub, text)


def _prep(text):
    """One row's raw text, collapsed and stripped of reader notes, ready to parse."""
    text = re.sub(r'\s+', ' ', text).strip()
    # trailing notes that are instructions to the reader, not stitches
    text = re.sub(r'Rows \d+[-\u2013]\d+:\s*repeat rows \d+[-\u2013]\d+\.?', '', text).strip()
    # prose that follows the final instruction is not part of the row
    return re.split(r'\bYou should have\b|\bRepeat all \d+ rows\b', text)[0].strip()


_LLM_SYMBOL_MAP = {"SKIP": "_SKIP", "NONE": "_NOP"}


def _unparsed_clauses(notes):
    """The clause texts out of a parse failure, so they can be handed to the LLM fallback."""
    out = []
    for note in notes:
        m = re.match(r"unparsed clause[^:]*:\s*(.+)$", note)
        if m:
            out.append(m.group(1).strip().strip("'\" "))
    return [c for c in out if c]


def learn_clauses(clauses):
    """Translate clauses the rules do not know, via the LLM fallback, and register them for this
    process. Nothing here is trusted: a learned clause is re-parsed and must reconcile through the
    same verifier as every other clause, or its row is refused exactly as before."""
    import llm_fallback
    learned = llm_fallback.learn(sorted(set(c for c in clauses if c)))
    applied = 0
    for clause, (sym, cols) in learned.items():
        if sym is None:                       # the model said: not a stitch instruction
            LEARNED[clause] = ("_NOP", 0)
            applied += 1
            continue
        if isinstance(cols, int) and cols >= 0:
            LEARNED[clause] = (_LLM_SYMBOL_MAP.get(sym, sym), cols)
            applied += 1
    return applied


def _prime_llm(rows, verbose=True):
    """Best-effort: widen the vocabulary for clauses the rules do not recognise.

    With no key or no network this does nothing, and every such row is reported UNVERIFIED exactly
    as before. Any clause it learns still has to survive the deterministic verifier afterwards.
    """
    pending = []
    for n in sorted(rows):
        text = _prep(rows[n])
        if re.match(r'^repeat row \d+$', text, re.I):     # control flow, not a stitch row
            continue
        _items, _count, notes = parse_row(_resolve_skips(text, []), count_hint=None)
        pending += _unparsed_clauses(notes)
    if not pending:
        return 0
    try:
        applied = learn_clauses(pending)
    except Exception as e:                    # no key, no network, a bad reply — all non-fatal
        if verbose:
            print(f"  LLM fallback unavailable — {e}")
        return 0
    if verbose and applied:
        print(f"  LLM fallback learned {applied} clause(s)")
    return applied


def convert(pdf, verbose=True, out_json="converted.json", use_llm=True):
    rows = find_rows(pdf_text(pdf))
    if use_llm:
        _prime_llm(rows, verbose)             # widen the vocabulary before parsing, if we can
    verified, unverified = {}, {}
    available = None                      # stitch count carried forward row to row
    prev_widths = []                      # chain-space widths carried forward row to row
    for n in sorted(rows):
        text = _prep(rows[n])

        m_rep = re.match(r'^repeat row (\d+)$', text, re.I)          # control flow, not a stitch row
        if m_rep:
            src = int(m_rep.group(1))
            if src in verified:
                verified[n] = verified[src]
                if verbose:
                    print(f"  R{n:<3} OK  copy of row {src}   [control flow]")
            else:
                unverified[n] = f"repeats row {src}, which is not itself verified"
                if verbose:
                    print(f"  R{n:<3} UNVERIFIED — repeats row {src}, not itself verified")
            continue

        # "skip CH2" only makes sense against the row below: resolve it to a real width
        resolved = _resolve_skips(text, prev_widths)

        items, count, notes = parse_row(resolved, count_hint=available)
        if items is None:
            unverified[n] = "; ".join(notes)
            if verbose:
                print(f"  R{n:<3} UNVERIFIED — {'; '.join(notes)}")
            continue
        if count and any(k == "_FILL" for k, _ in items):
            items = resolve_fill(items, count)
        total = sum(s for _, s in items)
        if count is not None and total != count:
            unverified[n] = f"parsed {total} != {count}"
            if verbose:
                print(f"  R{n:<3} UNVERIFIED — parsed {total} != count {count}")
            continue
        # a straight piece keeps its width, so carry the count forward
        if available is None and count is not None:
            available = count
        prev_widths = chain_space_widths(text)
        verified[n] = items
        if verbose:
            tag = ", ".join(notes)
            print(f"  R{n:<3} OK  {total:>4} columns   [{tag}]")
    json.dump({"verified": {str(k): v for k, v in verified.items()},
               "unverified": {str(k): v for k, v in unverified.items()}},
              open(out_json, "w"), indent=1)
    print(f"\n  {len(verified)} rows verified, {len(unverified)} unverified "
          f"(of {len(rows)} found) -> {out_json}")
    return verified, unverified


if __name__ == "__main__":
    pdf = sys.argv[1] if len(sys.argv) > 1 else "pattern.pdf"
    print(f"converting {pdf}\n")
    convert(pdf)
