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
