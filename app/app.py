#!/usr/bin/env python3
"""
Starlit — crochet PDF -> symbol chart converter.

Security posture (this service holds an API key, so the rules are deliberate):
  * Enrolment is INVITE-GATED. Anyone who can reach the page cannot create an account;
    a single-use code has to be handed over first. Otherwise a stranger on the tailnet
    could sign up and spend the DeepSeek balance.
  * Passwords are bcrypt-hashed. TOTP secrets are encrypted at rest (see _fernet).
  * Sessions live server-side with a 2-hour idle timeout, sliding on activity.
  * The API key never leaves the server: no endpoint returns it, no template can see it.
  * Per-account daily conversion cap, so a leaked session is bounded, not open-ended.
  * Uploads go to a temp dir and are deleted immediately after conversion.
"""
import os, io, re, json, time, base64, shutil, sqlite3, secrets, hashlib, tempfile, subprocess, sys
import threading
from datetime import datetime, timezone, timedelta

import bcrypt, pyotp, qrcode
from flask import (Flask, request, session, redirect, url_for, render_template,
                   send_file, abort, make_response, flash, jsonify)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(APP_DIR)
# Default to a data/ directory beside the code so a fresh clone runs with no configuration;
# a deployment points STARLIT_DATA at its own path (e.g. /var/lib/starlit).
DATA = os.environ.get("STARLIT_DATA") or os.path.join(REPO_DIR, "data")
DB = os.path.join(DATA, "app.db")
UPLOADS = os.path.join(DATA, "uploads")
OUT = os.path.join(DATA, "charts")
# The engine sits beside the app ({app,engine}) in the repo and in any deployed layout, so
# derive it rather than hardcoding — with an override for odd layouts.
ENGINE = os.environ.get("STARLIT_ENGINE") or os.path.join(REPO_DIR, "engine")
SECRETS = os.environ.get("STARLIT_SECRETS") or os.path.join(REPO_DIR, "secrets.env")
IDLE_SECONDS = 2 * 60 * 60    # two hours, sliding on activity: long enough to read a chart
                              # without being signed out mid-way. The idle bar in the header
                              # mirrors this value; change it here and both follow.
DAILY_CONVERSION_CAP = 40      # per account
PRINT_MM_PER_STITCH = 6.0    # A4 sheets: one stitch column prints 6 mm wide. The column is
                             # 26 px wide so symbols don't touch (see crochet_chart), and 6 mm
                             # keeps the printed symbol about the size it was at 4 mm.

os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, ENGINE)

app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=True, MAX_CONTENT_LENGTH=40 * 1024 * 1024)


# ------------------------------------------------------- conversion progress
# A conversion takes a few seconds (parse ~0.1 s, chart ~1.6 s, a batch of print sheets a few
# more) and the browser used to sit on a dead page for all of it — "feels like it froze".
# The convert request records which phase it is in and /progress reads that record, so the
# page can say what is actually happening. One record per user, behind a lock: the server is
# threaded, and a torn read here would show the wrong phase rather than no phase.
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()


def progress_begin(uid):
    with PROGRESS_LOCK:
        PROGRESS[uid] = {"stage": "reading the pattern", "done": None, "total": None,
                         "started": time.time(), "finished": None, "cid": None}


def progress_stage(uid, text, done=None, total=None):
    with PROGRESS_LOCK:
        p = PROGRESS.setdefault(uid, {"started": time.time()})
        p.update(stage=text, done=done, total=total)


def progress_end(uid, text="done", cid=None):
    with PROGRESS_LOCK:
        p = PROGRESS.setdefault(uid, {"started": time.time()})
        p.update(stage=text, done=None, total=None, finished=time.time(), cid=cid)


# ------------------------------------------------------------------ storage
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    create table if not exists users (
        id integer primary key, username text unique not null,
        pw_hash text not null, totp_secret blob, created text not null, active integer default 1);
    create table if not exists sessions (
        token text primary key, user_id integer not null, created text not null,
        last_seen real not null, ua text);
    create table if not exists invites (
        code text primary key, note text, created text not null, used_by integer, used_at text);
    create table if not exists conversions (
        id integer primary key, user_id integer not null, filename text, created text not null,
        verified integer, unverified integer, payload text);
    create table if not exists resets (
        code text primary key, user_id integer not null, created text not null,
        used_at text, issued_by text);
    create table if not exists audit (
        id integer primary key, at text not null, actor text, action text,
        target text, note text);
    """)
    # migrations for databases created before these columns existed
    for table, col, decl in (("users", "is_admin", "integer default 0"),
                             ("invites", "grants_admin", "integer default 0")):
        cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
        if col not in cols:
            con.execute(f"alter table {table} add column {col} {decl}")
    con.commit()
    con.close()


def audit(actor, action, target="", note=""):
    """Record who did what. An admin can take over an account, so this matters."""
    con = db()
    con.execute("insert into audit (at,actor,action,target,note) values (?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), actor, action, target, note))
    con.commit()
    con.close()


def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def inner(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if not u["is_admin"]:
            audit(u["username"], "admin.denied", request.path)
            abort(403)
        return fn(*a, **kw)
    return inner


def app_secret():
    """Flask session key: generated once, stored 600 next to the data."""
    p = os.path.join(DATA, "flask-secret")
    if not os.path.exists(p):
        with open(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600), "w") as fh:
            fh.write(secrets.token_hex(32))
    return open(p).read().strip()


STORAGE_CAP = 2 * 1024 ** 3          # 2 GiB — the ceiling for kept files (charts + leftovers)


def _dir_usage(path):
    """(bytes, file_count) for a tree, so a stranded upload dir is counted, not just top-level files."""
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def storage_stats():
    cb, cf = _dir_usage(OUT)
    ub, uf = _dir_usage(UPLOADS)
    return {"charts_bytes": cb, "charts_files": cf,
            "uploads_bytes": ub, "uploads_files": uf,
            "total_bytes": cb + ub, "cap_bytes": STORAGE_CAP}


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def sweep_uploads(max_age_hours=2):
    """Delete stranded upload working directories.

    A conversion removes its own upload, but if the process dies between mkdtemp and rmtree the
    whole PDF is left behind — one stranded upload here was 10.9 MB against 72 KB charts, so this
    (not the charts) is the real way the guest fills up.
    """
    freed = 0
    now = time.time()
    try:
        names = os.listdir(UPLOADS)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(UPLOADS, name)
        try:
            if os.path.isdir(p) and now - os.path.getmtime(p) > max_age_hours * 3600:
                freed += _dir_usage(p)[0]
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    return freed


def sweep_orphans(min_age_hours=1):
    """Delete chart PNGs that no conversion row points at.

    Removing an account deletes its rows but used to leave the images on disk, so the charts
    directory only ever grew — 8 files against 1 surviving row. The grace period keeps a chart
    safe while its conversion row is still being written.
    """
    con = db()
    referenced = set()
    for row in con.execute("select payload from conversions"):
        try:
            data = json.loads(row["payload"])
            referenced.update(data.get("images") or [])
            for key in ("pdf", "rows"):     # the print sheets and the saved rows live in the same
                if data.get(key):           # directory as the PNGs, so they must be named here or
                    referenced.add(data[key])   # the sweeper would delete them as orphans
        except Exception:
            pass
    con.close()

    cutoff = time.time() - min_age_hours * 3600
    freed = removed = 0
    for name in os.listdir(OUT):
        p = os.path.join(OUT, name)
        if name in referenced or not os.path.isfile(p):
            continue
        try:
            if os.path.getmtime(p) > cutoff:
                continue
            freed += os.path.getsize(p)
            os.remove(p)
            removed += 1
        except OSError:
            pass
    return freed, removed


def _tidy_rows():
    """Drop conversion rows whose images are gone, and trim the ones left partly intact.

    Deliberately separate from prune_charts: enforce_cap needs THIS after it deletes files, and
    calling prune_charts(older_than_days=0) instead would mean "older than zero days", i.e. every
    chart — which wiped the whole charts directory the first time.
    """
    con = db()
    for row in con.execute("select id, payload from conversions").fetchall():
        try:
            data = json.loads(row["payload"])
        except Exception:
            continue
        imgs = data.get("images") or []
        left = [i for i in imgs if os.path.exists(os.path.join(OUT, i))]
        if imgs and not left:
            con.execute("delete from conversions where id=?", (row["id"],))
        elif left != imgs:
            data["images"] = left
            con.execute("update conversions set payload=? where id=?",
                        (json.dumps(data), row["id"]))
    con.commit()
    con.close()


def prune_charts(older_than_days=None):
    """Delete chart files — all of them, or only those older than N days. Oldest first.

    Also clears conversion rows left pointing at nothing. Returns (bytes_freed, files_removed).
    This is the only thing that deletes a chart, and it runs on an explicit admin action.
    """
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    freed = 0
    removed = 0
    for name in os.listdir(OUT):
        p = os.path.join(OUT, name)
        if not os.path.isfile(p):
            continue
        if cutoff is not None and os.path.getmtime(p) >= cutoff:
            continue
        try:
            freed += os.path.getsize(p)
            os.remove(p)
            removed += 1
        except OSError:
            pass

    _tidy_rows()
    return freed, removed


def enforce_cap():
    """Keep kept storage under the cap: orphans, then stranded uploads, then oldest charts."""
    freed = sweep_orphans(min_age_hours=1)[0]
    st = storage_stats()
    if st["total_bytes"] <= STORAGE_CAP:
        return freed
    freed += sweep_uploads(max_age_hours=0)          # stranded uploads go before any chart
    st = storage_stats()
    if st["total_bytes"] <= STORAGE_CAP:
        return freed
    over = st["total_bytes"] - STORAGE_CAP
    got = 0
    entries = sorted((os.path.getmtime(os.path.join(OUT, n)), n) for n in os.listdir(OUT))
    for _m, n in entries:                            # oldest charts first
        if got >= over:
            break
        p = os.path.join(OUT, n)
        try:
            got += os.path.getsize(p)
            os.remove(p)
        except OSError:
            pass
    if got:
        _tidy_rows()                                 # rows pointing at what just went
    return freed + got


def qr_data_uri(uri):
    """Render the setup QR inline as a data: URI.

    Deliberately NOT a separate endpoint taking the secret in a query string: that writes the
    shared secret into the web-server access log, the browser history and any proxy in front.
    Inlining it keeps the secret on a single page render and out of every log.
    """
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def api_key():
    try:
        m = re.search(r'^\s*DEEPSEEK_API_KEY\s*=\s*(\S+)', open(SECRETS).read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def _fernet():
    """Encrypt TOTP secrets at rest. Key derived from the flask secret (both 600)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    k = base64.urlsafe_b64encode(hashlib.sha256(app_secret().encode()).digest())
    return Fernet(k)


def enc(b: bytes) -> bytes:
    f = _fernet()
    return f.encrypt(b) if f else b


def dec(b: bytes) -> bytes:
    f = _fernet()
    try:
        return f.decrypt(b) if f else b
    except Exception:
        return b


# ------------------------------------------------------------------ sessions
def current_user():
    tok = request.cookies.get("starlit_session")
    if not tok:
        return None
    con = db()
    row = con.execute("select * from sessions where token=?", (tok,)).fetchone()
    if not row:
        con.close()
        return None
    if time.time() - row["last_seen"] > IDLE_SECONDS:
        con.execute("delete from sessions where token=?", (tok,))
        con.commit()
        con.close()
        return None
    con.execute("update sessions set last_seen=? where token=?", (time.time(), tok))
    con.commit()
    u = con.execute("select * from users where id=? and active=1", (row["user_id"],)).fetchone()
    con.close()
    return u


def start_session(user_id):
    tok = secrets.token_urlsafe(32)
    con = db()
    con.execute("insert into sessions (token,user_id,created,last_seen,ua) values (?,?,?,?,?)",
                (tok, user_id, datetime.now(timezone.utc).isoformat(), time.time(),
                 (request.headers.get("User-Agent") or "")[:200]))
    con.commit()
    con.close()
    return tok


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def inner(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return inner


# ------------------------------------------------------------------ routes: auth
@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        otp = request.form.get("otp", "").strip().replace(" ", "")
        con = db()
        row = con.execute("select * from users where username=? and active=1", (u,)).fetchone()
        con.close()
        # a deliberately vague message: do not reveal which factor was wrong
        ok = row and bcrypt.checkpw(pw.encode(), row["pw_hash"].encode())
        if ok:
            sec = dec(row["totp_secret"]).decode() if row["totp_secret"] else None
            ok = bool(sec) and pyotp.TOTP(sec).verify(otp, valid_window=1)
        if not ok:
            time.sleep(0.7)                      # blunt the obvious timing probe
            err = "That didn't match. Check your password and the 6-digit code."
        else:
            tok = start_session(row["id"])
            resp = redirect(request.args.get("next") or url_for("index"))
            resp.set_cookie("starlit_session", tok, httponly=True, samesite="Lax",
                            secure=True, max_age=12 * 3600)
            return resp
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    tok = request.cookies.get("starlit_session")
    if tok:
        con = db()
        con.execute("delete from sessions where token=?", (tok,))
        con.commit()
        con.close()
    resp = redirect(url_for("login"))
    resp.delete_cookie("starlit_session")
    return resp


@app.route("/enrol", methods=["GET", "POST"])
def enrol():
    code = (request.values.get("code") or "").strip()
    con = db()
    inv = con.execute("select * from invites where code=?", (code,)).fetchone()
    if not inv or inv["used_by"]:
        con.close()
        return render_template("enrol.html", bad=True, code=code)
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if not re.fullmatch(r"[a-z0-9._-]{3,32}", u or ""):
            return render_template("enrol.html", code=code, err="Pick a username of 3-32 letters.")
        if len(pw) < 10:
            return render_template("enrol.html", code=code, err="Use at least 10 characters.")
        if pw != pw2:
            return render_template("enrol.html", code=code, err="The two passwords differ.")
        if con.execute("select 1 from users where username=?", (u,)).fetchone():
            return render_template("enrol.html", code=code, err="That username is taken.")
        secret = pyotp.random_base32()
        con.execute("insert into users (username,pw_hash,totp_secret,created,is_admin) "
                    "values (?,?,?,?,?)",
                    (u, bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode(),
                     enc(secret.encode()), datetime.now(timezone.utc).isoformat(),
                     inv["grants_admin"] if "grants_admin" in inv.keys() else 0))
        uid = con.execute("select id from users where username=?", (u,)).fetchone()["id"]
        con.execute("update invites set used_by=?, used_at=? where code=?",
                    (uid, datetime.now(timezone.utc).isoformat(), code))
        con.commit()
        con.close()
        uri = pyotp.TOTP(secret).provisioning_uri(name=u, issuer_name="Starlit Crochet")
        return render_template("enrol_done.html", username=u, code=code, secret=secret,
                               qr=qr_data_uri(uri))
    con.close()
    return render_template("enrol.html", code=code)


# The /qr endpoint was removed deliberately: it took the TOTP secret as a query parameter, so
# the secret was written into the web-server access log (and browser history). The setup QR is
# now rendered inline as a data: URI, which keeps the secret on one page render.
# Do not reintroduce a URL-based QR for anything derived from a secret.


@app.route("/auth/verify")
def auth_verify():
    """nginx auth_request target — one login guards the file manager too."""
    return ("", 200) if current_user() else ("", 401)


# ------------------------------------------------------------------ routes: admin
@app.route("/admin")
@admin_required
def admin():
    me = current_user()
    con = db()
    users = con.execute("select * from users order by id").fetchall()
    resets = con.execute("select code,user_id,created,used_at,issued_by from resets "
                         "order by created desc limit 20").fetchall()
    logs = con.execute("select * from audit order by id desc limit 40").fetchall()
    conv = {r["user_id"]: r["c"] for r in
            con.execute("select user_id,count(*) c from conversions group by user_id")}
    con.close()
    st = storage_stats()
    st["total_h"] = human(st["total_bytes"])
    st["charts_h"] = human(st["charts_bytes"])
    st["uploads_h"] = human(st["uploads_bytes"])
    st["cap_h"] = human(STORAGE_CAP)
    st["pct"] = min(100, round(100.0 * st["total_bytes"] / STORAGE_CAP, 1))
    return render_template("admin.html", user=me, users=users, resets=resets, logs=logs,
                           conv=conv, st=st)


@app.route("/admin/clear", methods=["POST"])
@admin_required
def admin_clear():
    """Free space on demand. Nothing else in the app deletes a chart."""
    me = current_user()
    action = (request.form.get("action") or "").strip()
    if action == "uploads":
        of, on = sweep_orphans(min_age_hours=0)
        freed = sweep_uploads(max_age_hours=0) + of
        detail = f"{human(freed)} of leftovers ({on} orphaned charts, plus stranded uploads)"
    elif action == "old":
        freed, n = prune_charts(older_than_days=30)
        detail = f"{human(freed)} ({n} charts older than 30 days)"
    elif action == "all":
        freed, n = prune_charts(older_than_days=None)
        of, on = sweep_orphans(min_age_hours=0)
        freed += sweep_uploads(max_age_hours=0) + of
        detail = f"{human(freed)} ({n} charts, plus leftovers)"
    elif action == "cap":
        freed = enforce_cap()
        detail = f"{human(freed)} reclaimed to get back under the cap"
    else:
        flash("Unknown action.")
        return redirect(url_for("admin"))
    audit(me["username"], f"storage.{action}", detail)
    flash(f"Freed {detail}.")
    return redirect(url_for("admin"))


@app.route("/admin/reset/<int:uid>", methods=["POST"])
@admin_required
def admin_reset(uid):
    """Issue a one-time reset code. Keeps the account and its charts; clears the OLD
    authenticator (so the previous one can never be used again) and any live session."""
    me = current_user()
    con = db()
    t = con.execute("select * from users where id=?", (uid,)).fetchone()
    if not t:
        con.close()
        abort(404)
    code = "reset-" + secrets.token_urlsafe(12)
    con.execute("insert into resets (code,user_id,created,issued_by) values (?,?,?,?)",
                (code, uid, datetime.now(timezone.utc).isoformat(), me["username"]))
    con.execute("update users set totp_secret=NULL where id=?", (uid,))
    con.execute("delete from sessions where user_id=?", (uid,))
    con.commit()
    con.close()
    audit(me["username"], "admin.reset_issued", t["username"])
    return render_template("admin_code.html", user=me, code=code, target=t["username"])


@app.route("/admin/toggle/<int:uid>", methods=["POST"])
@admin_required
def admin_toggle(uid):
    me = current_user()
    con = db()
    t = con.execute("select * from users where id=?", (uid,)).fetchone()
    if not t:
        con.close()
        abort(404)
    if t["id"] == me["id"]:
        con.close()
        flash("You cannot disable your own account.")
        return redirect(url_for("admin"))
    new = 0 if t["active"] else 1
    con.execute("update users set active=? where id=?", (new, uid))
    con.execute("delete from sessions where user_id=?", (uid,))
    con.commit()
    con.close()
    audit(me["username"], "admin.activate" if new else "admin.deactivate", t["username"])
    flash(f"{t['username']} is now {'active' if new else 'disabled'}.")
    return redirect(url_for("admin"))


@app.route("/admin/reinvite/<int:uid>", methods=["POST"])
@admin_required
def admin_reinvite(uid):
    """Force a full re-registration. Removes the old account AND its charts so the username
    is free again — stated plainly on the button, because the charts cannot be recovered."""
    me = current_user()
    con = db()
    t = con.execute("select * from users where id=?", (uid,)).fetchone()
    if not t:
        con.close()
        abort(404)
    if t["id"] == me["id"]:
        con.close()
        flash("You cannot force a re-register on your own account.")
        return redirect(url_for("admin"))
    name = t["username"]
    con.execute("delete from sessions where user_id=?", (uid,))
    con.execute("delete from conversions where user_id=?", (uid,))
    con.execute("delete from resets where user_id=?", (uid,))
    con.execute("delete from users where id=?", (uid,))
    code = "starlit-" + secrets.token_urlsafe(12)
    con.execute("insert into invites (code,note,created,grants_admin) values (?,?,?,0)",
                (code, f"re-register for {name}", datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    audit(me["username"], "admin.reinvite_issued", name, "account and charts removed")
    return render_template("admin_code.html", user=me, code=code, target=name, reinvite=True)


@app.route("/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    """Create an invitation from the admin screen and get a link to send."""
    me = current_user()
    note = (request.form.get("note") or "").strip()[:80]
    as_admin = 1 if request.form.get("admin") else 0
    code = "starlit-" + secrets.token_urlsafe(12)
    con = db()
    con.execute("insert into invites (code,note,created,grants_admin) values (?,?,?,?)",
                (code, note, datetime.now(timezone.utc).isoformat(), as_admin))
    con.commit()
    con.close()
    audit(me["username"], "admin.invite_created", note or "(no note)",
          "admin" if as_admin else "user")
    return render_template("admin_code.html", user=me, code=code, invite=True,
                           target=note or "a new person")


# ------------------------------------------------------------------ routes: reset
@app.route("/reset", methods=["GET", "POST"])
def reset():
    """Used with a one-time code from an admin. The user sets their OWN password and
    authenticator, so an admin can unlock the account without ever learning either."""
    code = (request.values.get("code") or "").strip()
    con = db()
    r = con.execute("select * from resets where code=?", (code,)).fetchone()
    if not r or r["used_at"]:
        con.close()
        return render_template("reset.html", bad=True)
    t = con.execute("select * from users where id=?", (r["user_id"],)).fetchone()
    con.close()
    if not t:
        return render_template("reset.html", bad=True)

    if request.method == "POST":
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw) < 10:
            return render_template("reset.html", code=code, username=t["username"],
                                   err="Use at least 10 characters.")
        if pw != pw2:
            return render_template("reset.html", code=code, username=t["username"],
                                   err="The two passwords differ.")
        secret = pyotp.random_base32()
        con = db()
        con.execute("update users set pw_hash=?, totp_secret=?, active=1 where id=?",
                    (bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode(),
                     enc(secret.encode()), t["id"]))
        con.execute("update resets set used_at=? where code=?",
                    (datetime.now(timezone.utc).isoformat(), code))
        con.execute("delete from sessions where user_id=?", (t["id"],))
        con.commit()
        con.close()
        audit(t["username"], "reset.completed", t["username"])
        uri = pyotp.TOTP(secret).provisioning_uri(name=t["username"], issuer_name="Starlit Crochet")
        return render_template("enrol_done.html", username=t["username"], code=code, secret=secret,
                               qr=qr_data_uri(uri))
    return render_template("reset.html", code=code, username=t["username"])


# ------------------------------------------------------------------ routes: app
@app.route("/")
@login_required
def index():
    u = current_user()
    con = db()
    rows = con.execute("select id,filename,created,verified,unverified from conversions "
                       "where user_id=? order by id desc limit 20", (u["id"],)).fetchall()
    today = con.execute("select count(*) c from conversions where user_id=? and created > ?",
                        (u["id"], datetime.now(timezone.utc).date().isoformat())).fetchone()["c"]
    con.close()
    return render_template("index.html", user=u, rows=rows, today=today,
                           cap=DAILY_CONVERSION_CAP, idle=IDLE_SECONDS)


@app.route("/convert", methods=["POST"])
@login_required
def convert():
    u = current_user()
    f = request.files.get("pdf")
    if not f or not f.filename:
        flash("Choose a PDF first.")
        return redirect(url_for("index"))
    con = db()
    today = con.execute("select count(*) c from conversions where user_id=? and created > ?",
                        (u["id"], datetime.now(timezone.utc).date().isoformat())).fetchone()["c"]
    con.close()
    if today >= DAILY_CONVERSION_CAP:
        flash(f"You've reached today's limit of {DAILY_CONVERSION_CAP} conversions.")
        return redirect(url_for("index"))

    # keep the guest from filling up: stranded uploads go first, then the oldest charts
    enforce_cap()

    import pattern_to_chart
    progress_begin(u["id"])
    workdir = tempfile.mkdtemp(prefix="starlit-", dir=UPLOADS)
    pdf_path = os.path.join(workdir, "pattern.pdf")
    f.save(pdf_path)
    try:
        # engine runs with the API key in its environment only when it needs the fallback
        if api_key():
            os.environ.setdefault("DEEPSEEK_API_KEY", api_key())
        progress_stage(u["id"], "reading the pattern")
        verified, unverified = pattern_to_chart.convert(pdf_path, verbose=False,
                                                        out_json=os.path.join(workdir, "out.json"))
    except Exception as e:
        progress_end(u["id"], "could not be read")
        flash(f"That file could not be read as a pattern: {e}")
        return redirect(url_for("index"))
    finally:
        pass

    # render a chart per verified block of rows, and the A4 print sheets
    images = []
    extra = {}
    try:
        sys.path.insert(0, ENGINE)
        import render_chart as rc
        import paginate_pdf as pg
        rc.rows = {int(k): [tuple(x) for x in v] for k, v in verified.items()}
        if rc.rows:
            ordered = sorted(rc.rows)
            base = f"c{int(time.time())}"
            p1 = os.path.join(OUT, f"{base}-full.png")
            progress_stage(u["id"], "drawing the chart")
            rc.render(ordered, path=p1, title=f"{f.filename} — verified rows")
            images.append(os.path.basename(p1))
            # Keep the verified rows. They used to die with the temp dir, which made the print
            # scale a one-way door: a chart could never be re-sliced at another stitch size
            # without the pattern being uploaded again.
            rows_name = f"{base}-rows.json"
            pg.write_rows(rc.rows, os.path.join(OUT, rows_name))
            extra["rows"] = rows_name
            # The print sheets. A chart without them is still useful, so a failure here reports
            # itself and keeps the PNG rather than throwing the whole conversion away.
            try:
                pdf_name = f"{base}-print-a4.pdf"
                progress_stage(u["id"], "making the print sheets")
                _pdf, pmeta = pg.render_pdf(
                    rc.rows, os.path.join(OUT, pdf_name), title=f.filename,
                    mm_per_stitch=PRINT_MM_PER_STITCH,
                    on_page=lambda n, total: progress_stage(u["id"], "making the print sheets",
                                                            n, total))
                extra.update(pdf=pdf_name, pdf_pages=pmeta["n_pages"],
                             mm_per_stitch=pmeta["mm_per_stitch"],
                             glyph_mm=round(pg.SYM * pmeta["mm_per_stitch"] / pg.CELL_W, 1),
                             crowded={str(k): round(min(v.values()), 2)
                                      for k, v in pg.cramped_rows(rc.rows).items()})
            except Exception as e:
                progress_stage(u["id"], "the chart is drawn; the sheets failed")
                flash(f"The chart is drawn, but the A4 print sheets could not be made: {e}")
    except Exception as e:
        progress_end(u["id"], "could not be drawn")
        flash(f"Rows were read but the chart could not be drawn: {e}")

    payload = json.dumps({"verified_rows": sorted(int(x) for x in verified),
                          "unverified": {str(k): v for k, v in unverified.items()},
                          "images": images, **extra})
    con = db()
    cur = con.execute("insert into conversions (user_id,filename,created,verified,unverified,payload)"
                      " values (?,?,?,?,?,?)",
                      (u["id"], f.filename, datetime.now(timezone.utc).isoformat(),
                       len(verified), len(unverified), payload))
    con.commit()
    cid = cur.lastrowid
    con.close()
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)      # the upload does not linger
    progress_end(u["id"], "done", cid)
    return redirect(url_for("result", cid=cid))


@app.route("/progress")
@login_required
def progress():
    """What the conversion is doing right now — polled by the page while it waits."""
    u = current_user()
    with PROGRESS_LOCK:
        p = dict(PROGRESS.get(u["id"]) or {"stage": "idle", "started": None, "finished": None})
    if p.get("started") and not p.get("finished"):
        p["elapsed"] = round(time.time() - p["started"], 1)
    return jsonify(p)


@app.route("/result/<int:cid>")
@login_required
def result(cid):
    u = current_user()
    con = db()
    row = con.execute("select * from conversions where id=? and user_id=?", (cid, u["id"])).fetchone()
    con.close()
    if not row:
        abort(404)
    data = json.loads(row["payload"])
    return render_template("result.html", user=u, row=row, data=data)


@app.route("/chart/<path:name>")
@login_required
def chart(name):
    safe = os.path.basename(name)
    p = os.path.join(OUT, safe)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="image/png")


@app.route("/download/<path:name>")
@login_required
def download(name):
    """The A4 print sheets (PDF) and the saved rows. Images keep their own route."""
    safe = os.path.basename(name)
    p = os.path.join(OUT, safe)
    if not os.path.exists(p):
        abort(404)
    mime = ("application/pdf" if safe.endswith(".pdf") else
            "application/json" if safe.endswith(".json") else "application/octet-stream")
    return send_file(p, mimetype=mime, as_attachment=safe.endswith(".pdf"),
                     download_name=safe)


app.secret_key = app_secret()
init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("STARLIT_HOST", "127.0.0.1"),
            port=int(os.environ.get("STARLIT_PORT", "6090")), debug=False)
