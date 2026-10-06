#!/usr/bin/env python3
"""
LLM fallback: learn what an instruction clause means, when the rules don't know it.

The design rule here is: the model proposes, the arithmetic disposes.

The model's ONLY job is to translate one clause ("1DC in next 2ST") into the structured
form the rest of the pipeline already uses — a stitch symbol and how many stitch positions
it consumes. It never touches layout, never touches arithmetic, and never gets to produce
a chart. Whatever it proposes is fed back in as an ordinary clause and must survive the
same stitch-count reconciliation as everything else. A wrong answer is rejected, not drawn.

That is what makes an LLM safe to use here: it can only widen the parser's vocabulary, and
every widening is still checked by the deterministic verifier.
"""
import json, os, re, urllib.request

API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# the vocabulary the model is allowed to answer with
SYMBOLS = ["SS", "CH", "SC", "HDC", "DC", "TRC", "DTR", "DCC", "PICOT", "SKIP"]

SYSTEM = """You translate single crochet instruction clauses into structured JSON.

Reply with ONLY a JSON object, no prose, shaped exactly like:
{"symbol": "<one of SS,CH,SC,HDC,DC,TRC,DTR,DCC,PICOT>", "columns": <integer>, "note": "<short why>"}

Definitions:
- "columns" = how many stitch positions of the PREVIOUS row this clause consumes.
  A plain stitch worked into one stitch consumes 1. "in next 3ST" consumes 3.
  "skip 2ST" consumes 2 and makes no stitch (use symbol "SKIP").
  A chain space between stitches consumes 0 (use symbol "CH").
- A cluster/fan (e.g. "5DTR in next ST") consumes 1, whatever its height.
- If the clause is not a stitch instruction at all (fasten off, turn, a note), use
  {"symbol": "NONE", "columns": 0, "note": "..."}.

Use US terminology. SC is a cross, DC has one yarn-over, TRC two, DTR three."""


def _api_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for path, pattern in (("/etc/crochet/secrets.env", r'^\s*DEEPSEEK_API_KEY\s*=\s*(\S+)'),
                          (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "secrets.env"), r'^\s*DEEPSEEK_API_KEY\s*=\s*(\S+)')):
        try:
            with open(path) as fh:
                m = re.search(pattern, fh.read(), re.I | re.M)
            if m:
                return m.group(1).strip()
        except OSError:
            continue
    raise RuntimeError("no DeepSeek API key found (env DEEPSEEK_API_KEY, "
                       "secrets.env, or /etc/crochet/secrets.env)")


def learn(clauses, timeout=60):
    """clauses: list[str]. -> {clause: (symbol, columns)} for those the model understood."""
    if not clauses:
        return {}
    learned = {}
    # one call per batch, so a pattern costs a single request rather than one per clause
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(clauses))
    body = {
        "model": MODEL, "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content":
                      "Translate each clause. Reply with ONLY a JSON object mapping the "
                      "index to the result, e.g. {\"0\": {...}, \"1\": {...}}.\n\n" + listing}],
    }
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    text = payload["choices"][0]["message"]["content"].strip()
    text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.M).strip()
    got = json.loads(text)

    for idx, res in got.items():
        try:
            clause = clauses[int(idx)]
        except (ValueError, IndexError):
            continue
        sym = str(res.get("symbol", "")).upper().strip()
        cols = res.get("columns")
        if sym == "NONE":
            learned[clause] = (None, 0)
        elif sym in SYMBOLS and isinstance(cols, int) and cols >= 0:
            learned[clause] = (sym, cols)
    return learned


if __name__ == "__main__":
    import sys
    probes = sys.argv[1:] or ["1DC in next 2ST", "3SC around the post below", "skip 4ST"]
    for clause, (sym, cols) in learn(probes).items():
        print(f"  {clause!r:40} -> symbol={sym} columns={cols}")
