# Starlit — crochet pattern to symbol chart

Turn a **written** crochet pattern (PDF) into a **verified symbol chart** (PNG), plus a set of A4
print sheets (PDF).

Chart-to-text is a solved problem; text-to-chart is not. Starlit reads the instructions a designer
actually wrote — `Row 10 (WS): SC in 1st ST, skip 3ST, 5DTR in next ST, CH1 …` — and draws the
conventional symbol chart a crocheter would expect.

## The one rule

> **The model proposes, the arithmetic disposes.**

Every row must consume *exactly* the stitch count the previous row established. A row that does not
reconcile is reported **UNVERIFIED, with a reason** — never drawn, never guessed.

An LLM (DeepSeek) is wired in as a **vocabulary widener only**: when a clause is not recognised it
proposes a structured reading, and that proposal is fed back through the *same* deterministic
verifier. If it does not reconcile, it is rejected like anything else. The LLM can widen the
parser's vocabulary; it never touches the arithmetic. The fallback is optional — with no key,
unrecognised rows are simply reported UNVERIFIED and everything else still works.

## How it works

```
pattern.pdf
   │  pdftotext -layout
   ▼
"Row N (WS|RS): …"  ──►  parser  ──►  [ (symbol, span), … ]
                                        │
                                        ▼
                             ┌── verifier ──────────────┐
                             │ row 1's printed count is │
                             │ carried forward; every   │
                             │ row must consume exactly │
                             │ that many positions      │
                             └──────────┬───────────────┘
                                        │  reject → UNVERIFIED + reason
                                        ▼  accept
                                 renderer ──► chart.png  (+ A4 print sheets)
```

| File | Role |
| --- | --- |
| `engine/pattern_to_chart.py` | PDF → rows → verified dict (+ `converted.json`) |
| `engine/crochet_chart.py` | the parser, the symbol library, and the glyph drawing |
| `engine/render_chart.py` | verified rows → the PNG; also `render_key()` for the symbol key |
| `engine/llm_fallback.py` | one unrecognised clause → candidate JSON (never trusted directly) |
| `engine/paginate_pdf.py` | verified rows → the A4 print sheets (PDF) |
| `app/app.py` | Flask app: invite-gated auth, upload, results, admin |
| `app/manage.py` | admin CLI for when the web admin is unreachable |

### The symbol library

Matched to the standard UK/US reference key, where crossing bars are the **yarn-overs**:

- `sc` — a cross · `hdc` — plain T · `dc` — 1 bar · `tr` — 2 · `dtr` — 3 · `trtr` — 4
- **shells / abanicos** — stems meet at one **base** (an increase)
- **clusters / piñas** — stems meet at the **top** (a decrease)

Grouped stitches scale with their leg count. A fan worked into one stitch still has to be
*readable* — drawn at single-stitch width its five stems collide into a smear, which is a bug that
a symbol *key* (roomy cells) will happily hide from you. Verify on the rendered chart.

## Requirements

- Python 3.12+
- **poppler-utils** (`pdftotext` is a hard requirement; `pdftoppm`/`pdfinfo` are used by the tests)
- optional: a DeepSeek API key for the LLM fallback

## Running it

### Local

No configuration is needed for a fresh clone — state defaults to `./data` beside the code.

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app/app.py            # binds 127.0.0.1:6090
```

Then open <http://127.0.0.1:6090>.

**There is no open registration** — the app can hold an API key, so anyone who could sign up could
spend it. Create the first account with an admin invitation:

```bash
./venv/bin/python app/manage.py invite "Your name" --admin
# prints a one-time code, e.g.  starlit-XXXXXXXXXXXX
```

Open `http://127.0.0.1:6090/enrol?code=<that code>`, choose a username and password, and scan the
authenticator QR it shows. After that, further invitations are issued from `/admin`.

### Docker

```bash
docker build -t starlit .
docker run --rm -p 6090:6090 -v starlit-data:/data starlit
```

or `docker compose up --build`. State (`app.db`, `charts/`, `uploads/`) lives in the `/data`
volume.

### Configuration

Every path is env-overridable; see `.env.example`. The defaults suit a git clone:

| Variable | Default | Meaning |
| --- | --- | --- |
| `STARLIT_DATA` | `./data` | `app.db`, `charts/`, `uploads/` |
| `STARLIT_ENGINE` | `./engine` | the engine package |
| `STARLIT_SECRETS` | `./secrets.env` | file holding `DEEPSEEK_API_KEY` |
| `STARLIT_HOST` | `127.0.0.1` | bind address |
| `STARLIT_PORT` | `6090` | port |
| `DEEPSEEK_API_KEY` | — | optional; enables the LLM fallback |

## The sample pattern

`sample/pattern.pdf` is a small **synthetic** pattern — not a published design — so you can try the
tool with nothing of your own. `sample/rows.json` is its verified output and is the fixture the
tests run against. Regenerate both with `make sample`.

## Tests

```bash
./venv/bin/python -m pytest -q                  # engine: parse/verify, the fallback seam, the guard
./venv/bin/python tests/paginate_pdf_test.py     # the A4 print sheets, checked against the artifact
```

Both run offline with no secrets. `paginate_pdf_test.py` renders `sample/rows.json` and asserts
coverage, page size, ink, and that a printed symbol sits on the same pixel as the on-screen chart.

## Auth

Deliberately not open registration — the app can hold an API key, so anyone who can sign up can
spend it.

- **Single-use invites.** An admin issues a code; the person redeems it and sets *their own*
  password (bcrypt) and authenticator (TOTP). The admin never sees either.
- **2-hour sliding idle timeout**, enforced in the app.
- **Admin** can reset a password/authenticator, disable an account, or force a re-register —
  without ever seeing a credential. Every action is written to an audit table.
- Secrets load from the file named by `STARLIT_SECRETS` (never in the repo, never sent to the
  browser). A setup QR is rendered **inline as a `data:` URI** — deliberately not an endpoint
  taking the secret in a query string, because that writes it to the access log.

## Storage

Kept files (charts, plus anything a crashed conversion stranded) are capped at **2 GiB**. The app
holds itself under it before each conversion — orphaned charts first, then stranded uploads, then
the oldest charts — and `/admin` offers the manual equivalents.

## Copyright

**No pattern PDFs are included, and none should be added.** Patterns are the designer's copyright —
a chart made for personal use is fine, but do not publish or share a generated chart of someone
else's pattern. The shipped `sample/pattern.pdf` is synthetic, written for this repository, and is
not anyone's design. Any tooling that needs a real pattern takes the PDF path as an argument, so a
pattern file never has to live in the repository.

## Licence

MIT — see [LICENSE](LICENSE). That covers the software; patterns and the charts generated from them
are the designer's copyright (see above).
