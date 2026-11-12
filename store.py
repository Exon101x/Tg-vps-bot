"""Server profiles, active-server selection, persistent job queue and audit log.

The storage engine is chosen by DATABASE_URL (SQLite, PostgreSQL, MySQL/MariaDB or MongoDB, see backends.py).
Secrets are Fernet-encrypted here, so no database ever sees a plaintext password or key.
"""
import base64
import hashlib
import logging
import os
import time

from cryptography.fernet import Fernet

from backends import describe, open_backend


def make_fernet(secret):
    """A real Fernet key is used as is; any other long random string (e.g. Render's generateValue) is stretched into one."""
    if len(secret) < 16:
        raise SystemExit("SECRET_KEY is missing or too short (use at least 16 random characters, "
                         "e.g. python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")")
    try:
        return Fernet(secret)
    except ValueError:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


fernet = make_fernet(os.environ.get("SECRET_KEY", ""))
URL = os.getenv("DATABASE_URL") or (f"sqlite:///{os.environ['BOT_DB']}" if os.getenv("BOT_DB") else "sqlite:///bot.db")


def _connect(url, tries=int(os.getenv("DB_CONNECT_RETRIES", "10"))):
    """Databases started alongside the bot (docker-compose, PaaS add-ons) are often not ready yet: wait for them."""
    for i in range(1, tries + 1):
        try:
            return open_backend(url)
        except ImportError:
            raise  # a missing driver won't fix itself
        except Exception as e:
            if i == tries:
                raise
            logging.warning("database not ready (%s), retry %d/%d in 3s", type(e).__name__, i, tries)
            time.sleep(3)


B = _connect(URL)
LABEL = describe(URL)

B.audit_purge(int(time.time()) - 90 * 86400)  # keep 90 days of audit log
B.jobs_purge(int(time.time()) - 30 * 86400)   # and 30 days of finished jobs


def _enc(text):
    return fernet.encrypt(text.encode()).decode()


def _unplain(v):
    return bytes(v).decode() if isinstance(v, (bytes, bytearray, memoryview)) else v  # old SQLite files stored BLOBs


def _dec(row):
    if row is None:
        return None
    d = dict(row)
    d["secret"] = fernet.decrypt(_unplain(d["secret"]).encode()).decode()
    d["hostkey"] = d.get("hostkey") or ""
    d["cwd"] = d.get("cwd") or ""
    d["folder"] = d.get("folder") or ""
    d["dup"] = d.get("dup") or "overwrite"
    d["monitor"] = d.get("monitor") or 0
    return d


# ---- servers
def add_server(c):
    """c: dict with uid, name, scheme, host, port, user, auth ('password'|'key'), secret (plain), cwd, hostkey.
    Re-adding a server keeps its folder template, duplicate policy and monitor flag."""
    old = B.server_get(c["uid"], c["name"]) or {}
    B.server_put(dict(
        uid=c["uid"], name=c["name"], scheme=c["scheme"], host=c["host"], port=c["port"], user=c["user"],
        auth=c["auth"], secret=_enc(c["secret"]), cwd=c.get("cwd") or "", hostkey=c.get("hostkey") or "",
        monitor=old.get("monitor") or 0, folder=old.get("folder") or "", dup=old.get("dup") or "overwrite",
    ))
    B.active_set(c["uid"], c["name"])


def active_name(uid):
    return B.active_get(uid)


def get_server(uid, name=None):
    name = name or active_name(uid)
    return _dec(B.server_get(uid, name)) if name else None


def list_servers(uid):
    rows = []
    for r in B.server_list(uid):
        r = dict(r)
        r.pop("secret", None)
        r.pop("hostkey", None)
        r["cwd"], r["folder"] = r.get("cwd") or "", r.get("folder") or ""
        r["dup"], r["monitor"] = r.get("dup") or "overwrite", r.get("monitor") or 0
        rows.append(r)
    return rows


def set_active(uid, name):
    if not B.server_get(uid, name):
        return False
    B.active_set(uid, name)
    return True


def delete_server(uid, name):
    found = B.server_delete(uid, name)
    B.active_clear(uid, name)
    return found


def update(uid, name, **fields):
    if not fields or not set(fields) <= {"cwd", "hostkey", "monitor", "folder", "dup"}:
        raise ValueError("bad fields")
    B.server_update(uid, name, fields)


def monitored():
    return [_dec(r) for r in B.server_monitored()]


# ---- audit log
def log(uid, server, action):
    B.audit_add(int(time.time()), uid, server, action[:300])


def recent_log(uid, n=20):
    return B.audit_recent(uid, n)


# ---- jobs (persistent upload queue)
_JOB_FIELDS = {"status_id", "size", "state", "attempts", "resume", "error", "dest"}


def add_job(uid, server, chat_id, src, name, dest, size=0):
    now = int(time.time())
    touch_user(uid)  # the admin panel's user list tracks who actually moves files
    return B.job_add(dict(uid=uid, server=server, chat_id=chat_id, status_id=0, src=src, name=name, dest=dest, size=size,
                          state="queued", attempts=0, resume=0, error="", created=now, updated=now))


def _job(j):
    if j is None:
        return None
    j = dict(j)
    j["error"] = j.get("error") or ""
    return j


def get_job(jid):
    return _job(B.job_get(jid))


def set_job(jid, **fields):
    if not fields or not set(fields) <= _JOB_FIELDS:
        raise ValueError("bad fields")
    B.job_update(jid, {**fields, "updated": int(time.time())})


def list_jobs(uid, states=None, limit=20):
    return [_job(j) for j in B.job_list(uid, states, limit)]


def pending_jobs():
    return [_job(j) for j in B.job_pending()]


def count_active(uid, server):
    return B.job_stats(uid, ("queued", "running"), server=server)[0]


def usage(uid):
    now = int(time.time())
    return {
        "today": B.job_stats(uid, ("done",), since=now - 86400),
        "week": B.job_stats(uid, ("done",), since=now - 7 * 86400),
        "pending": B.job_stats(uid, ("queued", "running"))[0],
        "failed": B.job_stats(uid, ("failed",))[0],
    }


# ---- users (the admin panel: who uses the bot, who is banned)
_seen = {}    # uid -> (last write ts, name, username) so hot paths stay cheap
_ban = {}     # uid -> (banned, ts) short cache so per-request gates stay cheap


def touch_user(uid, name="", username=""):
    """Record that a user was seen (from a chat or the Mini App); name/username stay fresh.
    Anonymous pings (every queued job, every API request) write at most once a minute per user,
    but a name or username change always lands right away."""
    now = int(time.time())
    name, username = (name or "")[:128], (username or "")[:64]
    hit = _seen.get(uid)
    if hit and now - hit[0] < 60 and hit[1] == name and hit[2] == username:
        return
    _seen[uid] = (now, name, username)
    B.user_upsert(uid, name, username)


def user(uid):
    return B.user_get(uid)


def users(limit=200):
    return B.user_list(limit)


def user_ids():
    return [r["uid"] for r in B.user_list(5000)]


def ban_user(uid, flag=True):
    B.user_upsert(uid)  # the row must exist for the flag to land
    B.user_ban(uid, flag)
    _ban.pop(uid, None)


def is_banned(uid):
    """Cached for 30s (a flag flip shows within half a minute at the latest)."""
    now = time.time()
    hit = _ban.get(uid)
    if hit and now - hit[1] < 30:
        return hit[0]
    row = B.user_get(uid)
    banned = bool(row and row.get("banned"))
    _ban[uid] = (banned, now)
    return banned


def user_counts():
    """(total known users, banned users)."""
    return B.user_counts()


def server_counts():
    """(saved servers across all users, monitored ones) - no credentials are loaded."""
    return B.server_counts()


def top_users(limit=10):
    """[(uid, done uploads, bytes)] biggest movers first."""
    return B.jobs_by_user(("done",), limit)


def global_jobs(states, since=None):
    """(number of jobs, total bytes) across all users."""
    return B.jobs_totals(states, since)


def jobs_by_state():
    return B.jobs_state_counts()


# ---- subscription orders / plans / VPS allocations (subscriptions.py + hosting.py)
_ORDER_FIELDS = {"ref", "status", "decided", "cents", "currency"}
_ALLOC_FIELDS = {"status", "expires", "host", "port", "user", "secret", "cpu", "ram", "disk"}


def order_add(uid, plan, cents, currency):
    now = int(time.time())
    return B.order_add(dict(uid=uid, plan=plan, cents=int(cents), currency=currency[:8], ref="",
                            status="pending", created=now, decided=0))


def _order(o):
    if not o:
        return None
    o = dict(o)
    o["ref"] = o.get("ref") or ""
    o["cents"], o["currency"] = int(o.get("cents") or 0), o.get("currency") or "USD"
    o["decided"] = o.get("decided") or 0
    return o


def order_get(oid):
    return _order(B.order_get(oid))


def order_set(oid, **fields):
    if not fields or not set(fields) <= _ORDER_FIELDS:
        raise ValueError("bad fields")
    B.order_update(oid, fields)


def order_list(uid, limit=10):
    return [_order(o) for o in B.order_list(uid, limit)]


def orders_pending(limit=20):
    """Orders waiting for payment or for an admin's decision."""
    return [_order(o) for o in B.orders_pending(limit)]


def order_stats():
    """({status: count}, (approved orders, approved cents)) for the admin panel."""
    return B.order_counts(), B.order_revenue()


def sub_get(uid):
    """The user's subscription row, or None. Expiry is checked by subscriptions.effective()."""
    r = B.sub_get(uid)
    return dict(plan=r["plan"], expires=int(r.get("expires") or 0), updated=int(r.get("updated") or 0)) if r else None


def sub_set(uid, plan, expires):
    B.sub_set(dict(uid=uid, plan=plan, expires=int(expires), updated=int(time.time())))


def alloc_add(uid, plan, host, port, user, secret, cpu, ram, disk, expires):
    now = int(time.time())
    return B.alloc_add(dict(uid=uid, plan=plan, host=host, port=int(port), user=user, secret=_enc(secret),
                            cpu=int(cpu), ram=int(ram), disk=int(disk), status="active", created=now,
                            expires=int(expires)))


def _alloc(a):
    if not a:
        return None
    a = dict(a)
    a["secret"] = fernet.decrypt(_unplain(a["secret"]).encode()).decode()
    a["cpu"], a["ram"], a["disk"] = int(a.get("cpu") or 1), int(a.get("ram") or 1024), int(a.get("disk") or 10)
    a["expires"], a["created"] = int(a.get("expires") or 0), int(a.get("created") or 0)
    return a


def alloc_get(aid):
    return _alloc(B.alloc_get(aid))


def alloc_list(uid):
    return [_alloc(a) for a in B.alloc_list(uid)]


def alloc_set(aid, **fields):
    if not fields or not set(fields) <= _ALLOC_FIELDS:
        raise ValueError("bad fields")
    if "secret" in fields:
        fields["secret"] = _enc(fields["secret"])
    B.alloc_update(aid, fields)


def alloc_active_count(uid):
    return B.alloc_active_count(uid)


def alloc_counts():
    """(total instances, active ones) - no credentials loaded."""
    return B.alloc_counts()


def allocs_expiring(before):
    """Active allocations past their expiry: hosting.sweep() deprovisions them."""
    return [_alloc(a) for a in B.allocs_expiring(int(before))]


# ---- binary blobs (the Pyrogram session snapshot), encrypted at rest
def save_blob(key, data):
    B.kv_set(key, _enc_bytes(data))


def load_blob(key):
    v = B.kv_get(key)
    return fernet.decrypt(v.encode()) if v else None


def _enc_bytes(data):
    return fernet.encrypt(data).decode()


def kv_get(key):
    return B.kv_get(key)


def kv_set(key, value):
    B.kv_set(key, value)
