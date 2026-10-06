#!/usr/bin/env python3
"""
Starlit admin CLI — run as root on the guest:
    manage.py invite [note]     create a single-use enrolment code
    manage.py invites           list codes and whether they've been used
    manage.py users             list accounts
    manage.py del-user NAME     remove an account and its charts
"""
import sys, os, secrets, sqlite3
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import db, init_db      # noqa: E402  (app.py is safe to import: init_db is idempotent)


def cmd_invite(*rest):
    """invite [note] [--admin]"""
    args = list(rest)
    admin = "--admin" in args
    args = [a for a in args if a != "--admin"]
    note = " ".join(args).strip()
    init_db()
    code = "starlit-" + secrets.token_urlsafe(12)
    con = db()
    con.execute("insert into invites (code,note,created,grants_admin) values (?,?,?,?)",
                (code, note, datetime.now(timezone.utc).isoformat(), 1 if admin else 0))
    con.commit()
    con.close()
    print(code)
    return 0


def cmd_invites():
    con = db()
    for r in con.execute("select code,note,created,used_by from invites order by created"):
        state = "USED" if r["used_by"] else "open"
        print(f"  {r['code']}  {state:4}  {r['created'][:16]}  {r['note'] or ''}")
    con.close()
    return 0


def cmd_users():
    con = db()
    for r in con.execute("select id,username,created,active,totp_secret,is_admin from users order by id"):
        print(f"  [{r['id']:>2}] {r['username']:<20} "
              f"{'ADMIN ' if r['is_admin'] else '      '}"
              f"2fa={'yes' if r['totp_secret'] else 'NO '} "
              f"{'active' if r['active'] else 'disabled'} created={r['created'][:16]}")
    con.close()
    return 0


def cmd_del_user(name):
    con = db()
    row = con.execute("select id from users where username=?", (name,)).fetchone()
    if not row:
        print(f"  no such user: {name}")
        return 1
    uid = row["id"]

    # Remove the chart IMAGES as well as the rows. Deleting only the rows used to leave every
    # PNG on disk for good, so the charts directory grew without limit across account churn.
    import json as _json
    from app import OUT
    files = 0
    for r in con.execute("select payload from conversions where user_id=?", (uid,)):
        try:
            data = _json.loads(r["payload"])
            names = list(data.get("images") or [])
            names += [data[k] for k in ("pdf", "rows") if data.get(k)]   # the A4 sheets and the
            for n in names:                                             # saved rows live here too
                p = os.path.join(OUT, os.path.basename(n))
                if os.path.isfile(p):
                    os.remove(p)
                    files += 1
        except Exception:
            pass

    con.execute("delete from sessions where user_id=?", (uid,))
    con.execute("delete from conversions where user_id=?", (uid,))
    con.execute("delete from users where id=?", (uid,))
    con.execute("update invites set used_by=null, used_at=null where used_by=?", (uid,))
    con.commit()
    con.close()
    print(f"  removed {name} (id {uid}), {files} chart file(s)")
    return 0


def cmd_reset(name):
    """Issue a one-time reset code for a user, from the CLI (locked out of the web admin)."""
    init_db()
    con = db()
    row = con.execute("select id from users where username=?", (name,)).fetchone()
    if not row:
        con.close()
        print(f"  no such user: {name}")
        return 1
    code = "reset-" + secrets.token_urlsafe(12)
    con.execute("insert into resets (code,user_id,created,issued_by) values (?,?,?,?)",
                (code, row["id"], datetime.now(timezone.utc).isoformat(), "cli"))
    # the old authenticator can never be used again
    con.execute("update users set totp_secret=NULL where id=?", (row["id"],))
    con.execute("delete from sessions where user_id=?", (row["id"],))
    con.commit()
    con.close()
    print(code)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    cmd, rest = args[0], args[1:]
    fn = {"invite": cmd_invite, "invites": cmd_invites, "users": cmd_users,
          "del-user": cmd_del_user, "reset": cmd_reset}.get(cmd)
    if not fn:
        print(__doc__)
        sys.exit(2)
    sys.exit(fn(*rest))
