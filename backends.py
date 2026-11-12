"""Storage backends. store.py talks to one of these; they only ever see ciphertext for secrets.

  sqlite:///bot.db                       (default; file)
  postgresql://user:pass@host/db         (also postgres:// as given by Heroku/Railway/Neon/Supabase)
  mysql://user:pass@host/db              (MySQL / MariaDB)
  mongodb://... or mongodb+srv://...     (MongoDB / Atlas; database from the URL path, or MONGO_DB, default "vpsbot")

All backends expose the same small interface (see SQLBackend); rows are plain dicts.
"""
import os
import time
from urllib.parse import urlparse

TABLES = ("servers", "active", "audit", "jobs", "kv", "users", "orders", "subs", "allocs")
ACTIVE_STATES = ("queued", "running")
ORDER_OPEN = ("pending", "awaiting")  # an order the user can still pay or that waits for an admin


def normalize_url(url):
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    if url.startswith(("mysql://", "mariadb://")):
        url = "mysql+pymysql://" + url.split("://", 1)[1]
    return url


def describe(url):
    """Label safe to log: scheme and host, never credentials."""
    u = urlparse(url)
    scheme = u.scheme.split("+")[0]
    return f"{scheme}://{u.hostname}" if u.hostname else f"{scheme}:{u.path.lstrip('/')}"


def open_backend(url):
    if url.startswith(("mongodb://", "mongodb+srv://")):
        return MongoBackend(url)
    return SQLBackend(url)


def _plain(v):
    return bytes(v).decode() if isinstance(v, (bytes, bytearray, memoryview)) else v


# ============================================================ SQL (SQLite / PostgreSQL / MySQL)
class SQLBackend:
    def __init__(self, url):
        from sqlalchemy import (BigInteger, Column, Index, Integer, MetaData, String, Table, Text, create_engine, event,
                                make_url)

        url = normalize_url(url)
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            os.makedirs(os.path.dirname(os.path.abspath(parsed.database)), exist_ok=True)
        self.engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)
        self.dialect = self.engine.dialect.name
        if self.dialect == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _pragmas(conn, _):  # many threads write (uploads, monitor, handlers)
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        # Column choices keep MySQL happy: indexed columns are VARCHAR(n), TEXT columns never carry defaults,
        # and Telegram ids (> 2^31) are BIGINT.
        md = MetaData()
        self.t = {
            "servers": Table(
                "servers", md,
                Column("uid", BigInteger, primary_key=True, autoincrement=False),
                Column("name", String(64), primary_key=True),
                Column("scheme", String(8)), Column("host", String(255)), Column("port", Integer),
                Column("user", String(128)), Column("auth", String(16)), Column("secret", Text),
                Column("cwd", String(1024), server_default=""), Column("hostkey", Text),
                Column("monitor", Integer, server_default="0"),
                Column("folder", String(512), server_default=""),
                Column("dup", String(16), server_default="overwrite"),
            ),
            "active": Table("active", md, Column("uid", BigInteger, primary_key=True, autoincrement=False),
                            Column("name", String(64))),
            "audit": Table("audit", md, Column("ts", BigInteger), Column("uid", BigInteger), Column("server", String(64)),
                           Column("action", String(300)), Index("ix_audit_uid_ts", "uid", "ts")),
            "jobs": Table(
                "jobs", md,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("uid", BigInteger), Column("server", String(64)), Column("chat_id", BigInteger),
                Column("status_id", BigInteger, server_default="0"), Column("src", Text), Column("name", String(512)),
                Column("dest", Text), Column("size", BigInteger, server_default="0"), Column("state", String(16)),
                Column("attempts", Integer, server_default="0"), Column("resume", Integer, server_default="0"),
                Column("error", Text), Column("created", BigInteger), Column("updated", BigInteger),
                Index("ix_jobs_uid_state", "uid", "state"),
            ),
            "kv": Table("kv", md, Column("k", String(64), primary_key=True), Column("v", Text)),
            "users": Table(
                "users", md,
                Column("uid", BigInteger, primary_key=True, autoincrement=False),
                Column("name", String(128), server_default=""), Column("username", String(64), server_default=""),
                Column("joined", BigInteger, server_default="0"), Column("last_seen", BigInteger, server_default="0"),
                Column("banned", Integer, server_default="0"),
            ),
            "orders": Table(  # subscription orders: pending -> awaiting (ref submitted) -> approved / rejected
                "orders", md,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("uid", BigInteger), Column("plan", String(32)),
                Column("cents", Integer, server_default="0"), Column("currency", String(8), server_default="USD"),
                Column("ref", String(128), server_default=""), Column("status", String(16)),
                Column("created", BigInteger), Column("decided", BigInteger, server_default="0"),
                Index("ix_orders_uid_status", "uid", "status"),
            ),
            "subs": Table(  # one active subscription per user: which plan, until when
                "subs", md, Column("uid", BigInteger, primary_key=True, autoincrement=False),
                Column("plan", String(32)), Column("expires", BigInteger), Column("updated", BigInteger)),
            "allocs": Table(  # VPS instances provisioned for buyers (secret = Fernet ciphertext via store.py)
                "allocs", md,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("uid", BigInteger), Column("plan", String(32)),
                Column("host", String(255)), Column("port", Integer), Column("user", String(32)),
                Column("secret", Text), Column("cpu", Integer, server_default="1"), Column("ram", Integer, server_default="1024"),
                Column("disk", Integer, server_default="10"), Column("status", String(16)),
                Column("created", BigInteger), Column("expires", BigInteger),
                Index("ix_allocs_uid_status", "uid", "status"),
            ),
        }
        md.create_all(self.engine)
        self._migrate()

    def _migrate(self):
        """Databases created by older versions lack a couple of server columns."""
        from sqlalchemy import inspect, text

        have = {c["name"] for c in inspect(self.engine).get_columns("servers")}
        with self.engine.begin() as c:
            if "folder" not in have:
                c.execute(text("ALTER TABLE servers ADD COLUMN folder VARCHAR(512) DEFAULT ''"))
            if "dup" not in have:
                c.execute(text("ALTER TABLE servers ADD COLUMN dup VARCHAR(16) DEFAULT 'overwrite'"))

    def _all(self, stmt):
        with self.engine.connect() as c:
            return [{k: _plain(v) for k, v in r.items()} for r in c.execute(stmt).mappings()]

    def _one(self, stmt):
        rows = self._all(stmt)
        return rows[0] if rows else None

    # ---- servers
    def server_get(self, uid, name):
        from sqlalchemy import select
        t = self.t["servers"]
        return self._one(select(t).where(t.c.uid == uid, t.c.name == name))

    def server_list(self, uid):
        from sqlalchemy import select
        t = self.t["servers"]
        return self._all(select(t).where(t.c.uid == uid).order_by(t.c.name))

    def server_put(self, row):
        from sqlalchemy import delete, insert
        t = self.t["servers"]
        with self.engine.begin() as c:  # portable upsert
            c.execute(delete(t).where(t.c.uid == row["uid"], t.c.name == row["name"]))
            c.execute(insert(t).values(**row))

    def server_update(self, uid, name, fields):
        from sqlalchemy import update
        t = self.t["servers"]
        with self.engine.begin() as c:
            c.execute(update(t).where(t.c.uid == uid, t.c.name == name).values(**fields))

    def server_delete(self, uid, name):
        from sqlalchemy import delete
        t = self.t["servers"]
        with self.engine.begin() as c:
            return c.execute(delete(t).where(t.c.uid == uid, t.c.name == name)).rowcount > 0

    def server_monitored(self):
        from sqlalchemy import select
        t = self.t["servers"]
        return self._all(select(t).where(t.c.monitor == 1))

    def server_counts(self):
        """(number of servers, number with monitoring on) across all users."""
        from sqlalchemy import func, select
        t = self.t["servers"]
        with self.engine.connect() as c:
            total = c.execute(select(func.count()).select_from(t)).scalar()
            mon = c.execute(select(func.count()).select_from(t).where(t.c.monitor == 1)).scalar()
        return int(total), int(mon)

    # ---- active server per user
    def active_get(self, uid):
        from sqlalchemy import select
        t = self.t["active"]
        r = self._one(select(t).where(t.c.uid == uid))
        return r["name"] if r else None

    def active_set(self, uid, name):
        from sqlalchemy import delete, insert
        t = self.t["active"]
        with self.engine.begin() as c:
            c.execute(delete(t).where(t.c.uid == uid))
            c.execute(insert(t).values(uid=uid, name=name))

    def active_clear(self, uid, name):
        from sqlalchemy import delete
        t = self.t["active"]
        with self.engine.begin() as c:
            c.execute(delete(t).where(t.c.uid == uid, t.c.name == name))

    # ---- audit
    def audit_add(self, ts, uid, server, action):
        from sqlalchemy import insert
        with self.engine.begin() as c:
            c.execute(insert(self.t["audit"]).values(ts=ts, uid=uid, server=server, action=action))

    def audit_recent(self, uid, n):
        from sqlalchemy import select
        t = self.t["audit"]
        return self._all(select(t).where(t.c.uid == uid).order_by(t.c.ts.desc()).limit(n))

    def audit_purge(self, before):
        from sqlalchemy import delete
        t = self.t["audit"]
        with self.engine.begin() as c:
            c.execute(delete(t).where(t.c.ts < before))

    # ---- jobs
    def job_add(self, row):
        from sqlalchemy import insert
        with self.engine.begin() as c:
            return c.execute(insert(self.t["jobs"]).values(**row)).inserted_primary_key[0]

    def job_get(self, jid):
        from sqlalchemy import select
        t = self.t["jobs"]
        return self._one(select(t).where(t.c.id == jid))

    def job_update(self, jid, fields):
        from sqlalchemy import update
        t = self.t["jobs"]
        with self.engine.begin() as c:
            c.execute(update(t).where(t.c.id == jid).values(**fields))

    def job_list(self, uid, states, limit):
        from sqlalchemy import select
        t = self.t["jobs"]
        q = select(t).where(t.c.uid == uid)
        if states:
            q = q.where(t.c.state.in_(list(states)))
        return self._all(q.order_by(t.c.id.desc()).limit(limit))

    def job_pending(self):
        from sqlalchemy import select
        t = self.t["jobs"]
        return self._all(select(t).where(t.c.state.in_(list(ACTIVE_STATES))).order_by(t.c.id))

    def job_stats(self, uid, states, since=None, server=None):
        """(number of jobs, total bytes) matching the filters."""
        from sqlalchemy import func, select
        t = self.t["jobs"]
        q = select(func.count(), func.coalesce(func.sum(t.c.size), 0)).where(t.c.uid == uid, t.c.state.in_(list(states)))
        if since is not None:
            q = q.where(t.c.updated >= since)
        if server is not None:
            q = q.where(t.c.server == server)
        with self.engine.connect() as c:
            n, b = c.execute(q).one()
        return int(n), int(b)

    def jobs_purge(self, before):
        from sqlalchemy import delete
        t = self.t["jobs"]
        with self.engine.begin() as c:
            c.execute(delete(t).where(t.c.state.in_(["done", "cancelled"]), t.c.updated < before))

    def jobs_by_user(self, states, limit):
        """[(uid, number of jobs, total bytes)] per user, biggest movers first (admin panel)."""
        from sqlalchemy import func, select
        t = self.t["jobs"]
        q = (select(t.c.uid, func.count(), func.coalesce(func.sum(t.c.size), 0))
             .where(t.c.state.in_(list(states))).group_by(t.c.uid)
             .order_by(func.coalesce(func.sum(t.c.size), 0).desc()).limit(limit))
        with self.engine.connect() as c:
            return [(int(a), int(b), int(s)) for a, b, s in c.execute(q).all()]

    def jobs_totals(self, states, since=None):
        """(number of jobs, total bytes) across all users."""
        from sqlalchemy import func, select
        t = self.t["jobs"]
        q = select(func.count(), func.coalesce(func.sum(t.c.size), 0)).where(t.c.state.in_(list(states)))
        if since is not None:
            q = q.where(t.c.updated >= since)
        with self.engine.connect() as c:
            n, b = c.execute(q).one()
        return int(n), int(b)

    def jobs_state_counts(self):
        """{state: number of jobs} across all users (queued, running, done, failed, ...)."""
        from sqlalchemy import func, select
        t = self.t["jobs"]
        with self.engine.connect() as c:
            return {s: int(n) for s, n in c.execute(select(t.c.state, func.count()).group_by(t.c.state)).all()}

    # ---- key/value (session snapshot, ...)
    def kv_get(self, k):
        from sqlalchemy import select
        t = self.t["kv"]
        r = self._one(select(t).where(t.c.k == k))
        return r["v"] if r else None

    def kv_set(self, k, v):
        from sqlalchemy import delete, insert
        t = self.t["kv"]
        with self.engine.begin() as c:
            c.execute(delete(t).where(t.c.k == k))
            c.execute(insert(t).values(k=k, v=v))

    # ---- users (admin panel: who uses the bot, who is banned)
    def user_upsert(self, uid, name="", username=""):
        """First sight: insert with joined=now. Later: refresh name/username/last_seen; keep joined and banned."""
        from sqlalchemy import insert, update
        t = self.t["users"]
        now = int(time.time())
        vals = {"last_seen": now}
        if name:
            vals["name"] = name[:128]
        if username:
            vals["username"] = username[:64]
        with self.engine.begin() as c:
            r = c.execute(update(t).where(t.c.uid == uid).values(**vals))
            if r.rowcount == 0:
                c.execute(insert(t).values(uid=uid, name=(name or "")[:128], username=(username or "")[:64],
                                         joined=now, last_seen=now, banned=0))

    def user_get(self, uid):
        from sqlalchemy import select
        t = self.t["users"]
        return self._one(select(t).where(t.c.uid == uid))

    def user_list(self, limit):
        from sqlalchemy import select
        t = self.t["users"]
        return self._all(select(t).order_by(t.c.last_seen.desc()).limit(limit))

    def user_ban(self, uid, flag):
        from sqlalchemy import update
        t = self.t["users"]
        with self.engine.begin() as c:
            c.execute(update(t).where(t.c.uid == uid).values(banned=1 if flag else 0))

    def user_counts(self):
        """(total users, banned users)."""
        from sqlalchemy import func, select
        t = self.t["users"]
        with self.engine.connect() as c:
            total = c.execute(select(func.count()).select_from(t)).scalar()
            banned = c.execute(select(func.count()).select_from(t).where(t.c.banned == 1)).scalar()
        return int(total), int(banned)

    # ---- subscription orders / plans / VPS allocations (see subscriptions.py + hosting.py)
    def order_add(self, row):
        from sqlalchemy import insert
        with self.engine.begin() as c:
            return c.execute(insert(self.t["orders"]).values(**row)).inserted_primary_key[0]

    def order_get(self, oid):
        from sqlalchemy import select
        return self._one(select(self.t["orders"]).where(self.t["orders"].c.id == oid))

    def order_update(self, oid, fields):
        from sqlalchemy import update
        t = self.t["orders"]
        with self.engine.begin() as c:
            c.execute(update(t).where(t.c.id == oid).values(**fields))

    def order_list(self, uid, limit):
        from sqlalchemy import select
        t = self.t["orders"]
        return self._all(select(t).where(t.c.uid == uid).order_by(t.c.id.desc()).limit(limit))

    def orders_pending(self, limit):
        from sqlalchemy import select
        t = self.t["orders"]
        return self._all(select(t).where(t.c.status.in_(list(ORDER_OPEN))).order_by(t.c.id.desc()).limit(limit))

    def order_counts(self):
        from sqlalchemy import func, select
        t = self.t["orders"]
        with self.engine.connect() as c:
            return {s: int(n) for s, n in c.execute(select(t.c.status, func.count()).group_by(t.c.status)).all()}

    def order_revenue(self):
        """(number of approved orders, total cents) for the admin panel."""
        from sqlalchemy import func, select
        t = self.t["orders"]
        with self.engine.connect() as c:
            n, cents = c.execute(select(func.count(), func.coalesce(func.sum(t.c.cents), 0))
                                  .where(t.c.status == "approved")).one()
        return int(n), int(cents)

    def sub_get(self, uid):
        from sqlalchemy import select
        return self._one(select(self.t["subs"]).where(self.t["subs"].c.uid == uid))

    def sub_set(self, row):
        from sqlalchemy import delete, insert
        t = self.t["subs"]
        with self.engine.begin() as c:  # portable upsert
            c.execute(delete(t).where(t.c.uid == row["uid"]))
            c.execute(insert(t).values(**row))

    def alloc_add(self, row):
        from sqlalchemy import insert
        with self.engine.begin() as c:
            return c.execute(insert(self.t["allocs"]).values(**row)).inserted_primary_key[0]

    def alloc_get(self, aid):
        from sqlalchemy import select
        return self._one(select(self.t["allocs"]).where(self.t["allocs"].c.id == aid))

    def alloc_list(self, uid):
        from sqlalchemy import select
        t = self.t["allocs"]
        return self._all(select(t).where(t.c.uid == uid).order_by(t.c.id))

    def alloc_update(self, aid, fields):
        from sqlalchemy import update
        t = self.t["allocs"]
        with self.engine.begin() as c:
            c.execute(update(t).where(t.c.id == aid).values(**fields))

    def alloc_active_count(self, uid):
        from sqlalchemy import func, select
        t = self.t["allocs"]
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).select_from(t)
                                 .where(t.c.uid == uid, t.c.status == "active")).scalar())

    def alloc_counts(self):
        """(total instances, active ones) across all users."""
        from sqlalchemy import func, select
        t = self.t["allocs"]
        with self.engine.connect() as c:
            total = c.execute(select(func.count()).select_from(t)).scalar()
            active = c.execute(select(func.count()).select_from(t).where(t.c.status == "active")).scalar()
        return int(total), int(active)

    def allocs_expiring(self, before):
        """Active allocations whose subscription has run out - hosting.py deprovisions them."""
        from sqlalchemy import select
        t = self.t["allocs"]
        return self._all(select(t).where(t.c.status == "active", t.c.expires < before))

    # ---- bulk copy (migrate.py)
    def dump(self):
        from sqlalchemy import select
        return {name: self._all(select(self.t[name])) for name in TABLES}

    def is_populated(self):
        return any(self.dump()[n] for n in ("servers", "jobs", "kv"))

    def load(self, data, replace=False):
        from sqlalchemy import delete, insert, text
        with self.engine.begin() as c:
            if replace:
                for name in TABLES:
                    c.execute(delete(self.t[name]))
            for name in TABLES:
                rows = data.get(name) or []
                if rows:
                    c.execute(insert(self.t[name]), rows)
            if self.dialect == "postgresql":  # explicit ids were inserted: move the sequences past them
                for name in ("jobs", "orders", "allocs"):
                    c.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}','id'), "
                                   f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)"))


# ============================================================ MongoDB
class MongoBackend:
    def __init__(self, url):
        from pymongo import ASCENDING, MongoClient

        self.client = MongoClient(url, serverSelectionTimeoutMS=15000)
        db = self.client.get_default_database(default=os.getenv("MONGO_DB", "vpsbot"))
        self.c = {n: db[n] for n in TABLES}
        self.counters = db["counters"]
        self.dialect = "mongodb"
        self.c["servers"].create_index([("uid", ASCENDING), ("name", ASCENDING)], unique=True)
        self.c["active"].create_index("uid", unique=True)
        self.c["audit"].create_index([("uid", ASCENDING), ("ts", ASCENDING)])
        self.c["jobs"].create_index([("uid", ASCENDING), ("state", ASCENDING)])
        self.c["orders"].create_index([("uid", ASCENDING), ("status", ASCENDING)])
        self.c["allocs"].create_index([("uid", ASCENDING), ("status", ASCENDING)])
        self.c["subs"].create_index("uid", unique=True)

    @staticmethod
    def _clean(doc):
        if doc is None:
            return None
        d = dict(doc)
        d.pop("_id", None)
        return d

    @staticmethod
    def _job(doc):
        if doc is None:
            return None
        d = dict(doc)
        d["id"] = d.pop("_id")
        return d

    @staticmethod
    def _user(doc):
        if doc is None:
            return None
        d = dict(doc)
        d["uid"] = d.pop("_id")
        return d

    # ---- servers
    def server_get(self, uid, name):
        return self._clean(self.c["servers"].find_one({"uid": uid, "name": name}))

    def server_list(self, uid):
        return [self._clean(d) for d in self.c["servers"].find({"uid": uid}).sort("name", 1)]

    def server_put(self, row):
        self.c["servers"].replace_one({"uid": row["uid"], "name": row["name"]}, row, upsert=True)

    def server_update(self, uid, name, fields):
        self.c["servers"].update_one({"uid": uid, "name": name}, {"$set": fields})

    def server_delete(self, uid, name):
        return self.c["servers"].delete_one({"uid": uid, "name": name}).deleted_count > 0

    def server_monitored(self):
        return [self._clean(d) for d in self.c["servers"].find({"monitor": 1})]

    def server_counts(self):
        total = self.c["servers"].count_documents({})
        mon = self.c["servers"].count_documents({"monitor": 1})
        return int(total), int(mon)

    # ---- active server per user
    def active_get(self, uid):
        d = self.c["active"].find_one({"uid": uid})
        return d["name"] if d else None

    def active_set(self, uid, name):
        self.c["active"].replace_one({"uid": uid}, {"uid": uid, "name": name}, upsert=True)

    def active_clear(self, uid, name):
        self.c["active"].delete_one({"uid": uid, "name": name})

    # ---- audit
    def audit_add(self, ts, uid, server, action):
        self.c["audit"].insert_one({"ts": ts, "uid": uid, "server": server, "action": action})

    def audit_recent(self, uid, n):
        return [self._clean(d) for d in self.c["audit"].find({"uid": uid}).sort("ts", -1).limit(n)]

    def audit_purge(self, before):
        self.c["audit"].delete_many({"ts": {"$lt": before}})

    # ---- jobs
    def _next_id(self):
        from pymongo import ReturnDocument
        return self.counters.find_one_and_update({"_id": "jobs"}, {"$inc": {"n": 1}}, upsert=True,
                                                 return_document=ReturnDocument.AFTER)["n"]

    def job_add(self, row):
        jid = self._next_id()
        self.c["jobs"].insert_one({"_id": jid, **row})
        return jid

    def job_get(self, jid):
        return self._job(self.c["jobs"].find_one({"_id": jid}))

    def job_update(self, jid, fields):
        self.c["jobs"].update_one({"_id": jid}, {"$set": fields})

    def job_list(self, uid, states, limit):
        q = {"uid": uid}
        if states:
            q["state"] = {"$in": list(states)}
        return [self._job(d) for d in self.c["jobs"].find(q).sort("_id", -1).limit(limit)]

    def job_pending(self):
        return [self._job(d) for d in self.c["jobs"].find({"state": {"$in": list(ACTIVE_STATES)}}).sort("_id", 1)]

    def job_stats(self, uid, states, since=None, server=None):
        q = {"uid": uid, "state": {"$in": list(states)}}
        if since is not None:
            q["updated"] = {"$gte": since}
        if server is not None:
            q["server"] = server
        r = list(self.c["jobs"].aggregate([{"$match": q}, {"$group": {"_id": None, "n": {"$sum": 1}, "b": {"$sum": "$size"}}}]))
        return (int(r[0]["n"]), int(r[0]["b"])) if r else (0, 0)

    def jobs_purge(self, before):
        self.c["jobs"].delete_many({"state": {"$in": ["done", "cancelled"]}, "updated": {"$lt": before}})

    def jobs_by_user(self, states, limit):
        r = list(self.c["jobs"].aggregate([
            {"$match": {"state": {"$in": list(states)}}},
            {"$group": {"_id": "$uid", "n": {"$sum": 1}, "b": {"$sum": "$size"}}},
            {"$sort": {"b": -1}}, {"$limit": limit}]))
        return [(int(d["_id"]), int(d["n"]), int(d["b"])) for d in r]

    def jobs_totals(self, states, since=None):
        q = {"state": {"$in": list(states)}}
        if since is not None:
            q["updated"] = {"$gte": since}
        r = list(self.c["jobs"].aggregate([{"$match": q}, {"$group": {"_id": None, "n": {"$sum": 1}, "b": {"$sum": "$size"}}}]))
        return (int(r[0]["n"]), int(r[0]["b"])) if r else (0, 0)

    def jobs_state_counts(self):
        r = list(self.c["jobs"].aggregate([{"$group": {"_id": "$state", "n": {"$sum": 1}}}]))
        return {d["_id"]: int(d["n"]) for d in r}

    # ---- key/value
    def kv_get(self, k):
        d = self.c["kv"].find_one({"_id": k})
        return d["v"] if d else None

    def kv_set(self, k, v):
        self.c["kv"].replace_one({"_id": k}, {"_id": k, "v": v}, upsert=True)

    # ---- users
    def user_upsert(self, uid, name="", username=""):
        now = int(time.time())
        d = self.c["users"].find_one({"_id": uid})
        if not d:
            self.c["users"].insert_one({"_id": uid, "name": (name or "")[:128], "username": (username or "")[:64],
                                        "joined": now, "last_seen": now, "banned": 0})
        else:
            upd = {"last_seen": now}
            if name:
                upd["name"] = name[:128]
            if username:
                upd["username"] = username[:64]
            self.c["users"].update_one({"_id": uid}, {"$set": upd})

    def user_get(self, uid):
        return self._user(self.c["users"].find_one({"_id": uid}))

    def user_list(self, limit):
        return [self._user(d) for d in self.c["users"].find({}).sort("last_seen", -1).limit(limit)]

    def user_ban(self, uid, flag):
        self.c["users"].update_one({"_id": uid}, {"$set": {"banned": 1 if flag else 0}})

    def user_counts(self):
        return int(self.c["users"].count_documents({})), int(self.c["users"].count_documents({"banned": 1}))

    # ---- subscription orders / plans / VPS allocations
    def _next(self, name):
        from pymongo import ReturnDocument
        return self.counters.find_one_and_update({"_id": name}, {"$inc": {"n": 1}}, upsert=True,
                                                 return_document=ReturnDocument.AFTER)["n"]

    def order_add(self, row):
        oid = self._next("orders")
        self.c["orders"].insert_one({"_id": oid, **row})
        return oid

    def order_get(self, oid):
        return self._job(self.c["orders"].find_one({"_id": oid}))

    def order_update(self, oid, fields):
        self.c["orders"].update_one({"_id": oid}, {"$set": fields})

    def order_list(self, uid, limit):
        return [self._job(d) for d in self.c["orders"].find({"uid": uid}).sort("_id", -1).limit(limit)]

    def orders_pending(self, limit):
        return [self._job(d) for d in self.c["orders"].find({"status": {"$in": list(ORDER_OPEN)}})
                .sort("_id", -1).limit(limit)]

    def order_counts(self):
        r = list(self.c["orders"].aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]))
        return {d["_id"]: int(d["n"]) for d in r}

    def order_revenue(self):
        r = list(self.c["orders"].aggregate([{"$match": {"status": "approved"}},
                                             {"$group": {"_id": None, "n": {"$sum": 1}, "c": {"$sum": "$cents"}}}]))
        return (int(r[0]["n"]), int(r[0]["c"])) if r else (0, 0)

    def sub_get(self, uid):
        d = self.c["subs"].find_one({"_id": uid})
        return self._user(d) if d else None

    def sub_set(self, row):
        d = dict(row)
        d["_id"] = d.pop("uid")
        self.c["subs"].replace_one({"_id": d["_id"]}, d, upsert=True)

    def alloc_add(self, row):
        aid = self._next("allocs")
        self.c["allocs"].insert_one({"_id": aid, **row})
        return aid

    def alloc_get(self, aid):
        return self._job(self.c["allocs"].find_one({"_id": aid}))

    def alloc_list(self, uid):
        return [self._job(d) for d in self.c["allocs"].find({"uid": uid}).sort("_id", 1)]

    def alloc_update(self, aid, fields):
        self.c["allocs"].update_one({"_id": aid}, {"$set": fields})

    def alloc_active_count(self, uid):
        return int(self.c["allocs"].count_documents({"uid": uid, "status": "active"}))

    def alloc_counts(self):
        return int(self.c["allocs"].count_documents({})), int(self.c["allocs"].count_documents({"status": "active"}))

    def allocs_expiring(self, before):
        return [self._job(d) for d in self.c["allocs"].find({"status": "active", "expires": {"$lt": before}}).sort("_id", 1)]

    # ---- bulk copy
    def dump(self):
        out = {n: [self._clean(d) for d in self.c[n].find({})] for n in ("servers", "active", "audit")}
        out["jobs"] = [self._job(d) for d in self.c["jobs"].find({})]
        out["kv"] = [{"k": d["_id"], "v": d["v"]} for d in self.c["kv"].find({})]
        out["users"] = [self._user(d) for d in self.c["users"].find({})]
        out["orders"] = [self._job(d) for d in self.c["orders"].find({})]
        out["subs"] = [self._user(d) for d in self.c["subs"].find({})]
        out["allocs"] = [self._job(d) for d in self.c["allocs"].find({})]
        return out

    def is_populated(self):
        return any(self.c[n].count_documents({}, limit=1) for n in ("servers", "jobs", "kv"))

    def load(self, data, replace=False):
        if replace:
            for n in TABLES:
                self.c[n].delete_many({})
        for n in ("servers", "active", "audit"):
            if data.get(n):
                self.c[n].insert_many([dict(r) for r in data[n]])
        if data.get("jobs"):
            docs = []
            for r in data["jobs"]:
                d = dict(r)
                d["_id"] = d.pop("id")
                docs.append(d)
            self.c["jobs"].insert_many(docs)
            self.counters.replace_one({"_id": "jobs"}, {"_id": "jobs", "n": max(d["_id"] for d in docs)}, upsert=True)
        if data.get("kv"):
            self.c["kv"].insert_many([{"_id": r["k"], "v": r["v"]} for r in data["kv"]])
        if data.get("users"):
            docs = []
            for r in data["users"]:
                d = dict(r)
                d["_id"] = d.pop("uid")
                docs.append(d)
            self.c["users"].insert_many(docs)
        for name in ("orders", "allocs"):  # autoincrement tables: id -> _id + counter
            if data.get(name):
                docs = []
                for r in data[name]:
                    d = dict(r)
                    d["_id"] = d.pop("id")
                    docs.append(d)
                self.c[name].insert_many(docs)
                self.counters.replace_one({"_id": name}, {"_id": name, "n": max(d["_id"] for d in docs)}, upsert=True)
        if data.get("subs"):  # uid-keyed table, like users
            docs = []
            for r in data["subs"]:
                d = dict(r)
                d["_id"] = d.pop("uid")
                docs.append(d)
            self.c["subs"].insert_many(docs)
