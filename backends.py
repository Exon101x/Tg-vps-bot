"""Storage backends. store.py talks to one of these; they only ever see ciphertext for secrets.

  sqlite:///bot.db                       (default; file)
  postgresql://user:pass@host/db         (also postgres:// as given by Heroku/Railway/Neon/Supabase)
  mysql://user:pass@host/db              (MySQL / MariaDB)
  mongodb://... or mongodb+srv://...     (MongoDB / Atlas; database from the URL path, or MONGO_DB, default "vpsbot")

All backends expose the same small interface (see SQLBackend); rows are plain dicts.
"""
import os
from urllib.parse import urlparse

TABLES = ("servers", "active", "audit", "jobs", "kv")
ACTIVE_STATES = ("queued", "running")


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
            if self.dialect == "postgresql":  # explicit ids were inserted: move the sequence past them
                c.execute(text("SELECT setval(pg_get_serial_sequence('jobs','id'), COALESCE((SELECT MAX(id) FROM jobs), 0) + 1, false)"))


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

    # ---- key/value
    def kv_get(self, k):
        d = self.c["kv"].find_one({"_id": k})
        return d["v"] if d else None

    def kv_set(self, k, v):
        self.c["kv"].replace_one({"_id": k}, {"_id": k, "v": v}, upsert=True)

    # ---- bulk copy
    def dump(self):
        out = {n: [self._clean(d) for d in self.c[n].find({})] for n in ("servers", "active", "audit")}
        out["jobs"] = [self._job(d) for d in self.c["jobs"].find({})]
        out["kv"] = [{"k": d["_id"], "v": d["v"]} for d in self.c["kv"].find({})]
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
