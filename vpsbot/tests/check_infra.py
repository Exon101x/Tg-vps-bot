import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import asyncio, os, sqlite3, sys, tempfile, urllib.request, json
from cryptography.fernet import Fernet
KEY = Fernet.generate_key().decode()
TMP = tempfile.mkdtemp()
OLD = os.path.join(TMP, "old.db")
os.environ.update(SECRET_KEY=KEY, DATABASE_URL=f"sqlite:///{OLD}", BOT_TOKEN="123:abc", SESSION_DIR=os.path.join(TMP, "sess"),
                  API_ID="1", API_HASH="x", ALLOWED_USERS="5", LOG_LEVEL="WARNING")
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

# ---- 1. a database written by the FIRST version of the bot (no folder/dup columns, no jobs table, BLOB secrets)
f = Fernet(KEY)
con = sqlite3.connect(OLD)
con.executescript("""CREATE TABLE servers(uid INTEGER, name TEXT, scheme TEXT, host TEXT, port INTEGER, user TEXT, auth TEXT, secret BLOB,
  cwd TEXT DEFAULT '', hostkey TEXT DEFAULT '', monitor INTEGER DEFAULT 0, PRIMARY KEY(uid, name));
  CREATE TABLE active(uid INTEGER PRIMARY KEY, name TEXT);
  CREATE TABLE audit(ts INTEGER, uid INTEGER, server TEXT, action TEXT);""")
con.execute("INSERT INTO servers VALUES(?,?,?,?,?,?,?,?,?,?,?)", (5, "legacy", "ssh", "h.example", 22, "root", "password", f.encrypt(b"oldpw"), "/home", "ssh-ed25519 KEY", 1))
con.execute("INSERT INTO active VALUES(5,'legacy')"); con.commit(); con.close()
import store
g = store.get_server(5)
ok(g and g["secret"] == "oldpw" and g["folder"] == "" and g["dup"] == "overwrite" and g["monitor"] == 1 and g["hostkey"] == "ssh-ed25519 KEY", "1. old SQLite database opens: BLOB secret decrypts, new columns added with defaults")
store.update(5, "legacy", folder="{user}", dup="skip"); ok(store.get_server(5)["folder"] == "{user}", "1b. new columns writable on the upgraded table")
jid = store.add_job(5, "legacy", 5, "tg:5:1", "a", "a", 1); ok(jid == 1, "1c. jobs table created alongside the old tables")

# ---- 2. session snapshot / restore (ephemeral-disk hosting)
import session_sync
os.makedirs(session_sync.DIR, exist_ok=True)
c = sqlite3.connect(session_sync.PATH); c.execute("CREATE TABLE peers(id INTEGER PRIMARY KEY, access_hash INTEGER)"); c.executemany("INSERT INTO peers VALUES(?,?)", [(i, i * 7) for i in range(500)]); c.commit(); c.close()
ok(session_sync.snapshot(True), "2. snapshot saved to the database")
ok(not session_sync.snapshot(), "2b. unchanged file is not re-uploaded")
os.remove(session_sync.PATH)
ok(session_sync.restore() == "restored from database", "2c. missing session file restored from the database")
rows = sqlite3.connect(session_sync.PATH).execute("SELECT COUNT(*), SUM(access_hash) FROM peers").fetchone()
ok(rows == (500, sum(i * 7 for i in range(500))), "2d. restored session content intact (peer cache survives)")
os.remove(session_sync.PATH); os.environ["BOT_TOKEN"] = "999:other"
ok(session_sync.restore() == "fresh login" and not os.path.exists(session_sync.PATH), "2e. snapshot of a different bot token is ignored")
os.environ["BOT_TOKEN"] = "123:abc"; os.environ["RESET_SESSION"] = "1"
ok("RESET_SESSION" in session_sync.restore(), "2f. RESET_SESSION forces a fresh login"); del os.environ["RESET_SESSION"]
ok(open(session_sync.PATH, "rb").read() == b"" if os.path.exists(session_sync.PATH) else True, "") if False else None

# ---- 3. health endpoint for PaaS port checks
import bot
async def health():
    srv = await bot.health_server(0); port = srv.sockets[0].getsockname()[1]
    body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read())
    srv.close(); return json.loads(body)
ok(asyncio.run(health()) == {"status": "ok"}, "3. health endpoint answers 200 {'status': 'ok'}")

# ---- 4. migrate between database types (ciphertext copied as-is)
import backends, migrate, mongomock, pymongo
src = backends.open_backend(f"sqlite:///{TMP}/src.db")
src.server_put(dict(uid=7_123_456_789, name="vps", scheme="ssh", host="h", port=22, user="root", auth="key", secret="CIPHERTEXT", cwd="/x", hostkey="k", monitor=1, folder="{user}", dup="rename"))
src.active_set(7_123_456_789, "vps"); src.audit_add(1700000000, 7_123_456_789, "vps", "cmd ü")
for i in range(3): src.job_add(dict(uid=7_123_456_789, server="vps", chat_id=7_123_456_789, status_id=5, src="tg:1:2", name=f"n{i}", dest=f"d{i}", size=2**33, state="done", attempts=1, resume=0, error="", created=1, updated=1))
src.kv_set("session", "BLOB"); src.kv_set("session_token", "t")
def norm(d): return {k: sorted(v, key=lambda r: sorted((str(a), str(b)) for a, b in r.items())) for k, v in d.items()}
want = norm(src.dump())
pymongo.MongoClient = mongomock.MongoClient
mongo = backends.open_backend("mongodb://localhost/migr")
mongo.load(src.dump()); ok(norm(mongo.dump()) == want, "4. SQLite -> MongoDB copy is lossless")
nid = mongo.job_add(dict(uid=1, server="s", chat_id=1, status_id=0, src="x", name="n", dest="d", size=0, state="queued", attempts=0, resume=0, error="", created=1, updated=1))
ok(nid == 4, "4b. Mongo job id counter continues after migration (next id %d)" % nid)
back = backends.open_backend(f"sqlite:///{TMP}/back.db"); back.load(mongo.dump())
ok(norm({k: v for k, v in back.dump().items() if k != "jobs"}) == {k: v for k, v in want.items() if k != "jobs"}, "4c. MongoDB -> SQLite round trip")
try: migrate.main([f"sqlite:///{TMP}/src.db", f"sqlite:///{TMP}/back.db"]); ok(False, "refuse non-empty")
except SystemExit as e: ok("--force" in str(e), "4d. migrate.py refuses to overwrite a non-empty target without --force")
migrate.main([f"sqlite:///{TMP}/src.db", f"sqlite:///{TMP}/back.db", "--force"]); ok(norm(backends.open_backend(f"sqlite:///{TMP}/back.db").dump()) == want, "4e. migrate.py --force replaces the target")

# ---- 5. MySQL/MariaDB DDL sanity (no server available here, so check the generated SQL)
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
problems = []
for name, t in src.t.items():
    ddl = str(CreateTable(t).compile(dialect=mysql.dialect()))
    for line in ddl.splitlines():
        if ("TEXT" in line and "DEFAULT" in line) or ("TEXT" in line and "PRIMARY" in line): problems.append(line.strip())
    for col in t.primary_key.columns:
        if "TEXT" in str(col.type.compile(dialect=mysql.dialect())): problems.append(f"{name}.{col.name} is TEXT in a key")
ok(not problems, "5. MySQL DDL: no TEXT defaults/keys %s" % problems)
ok("BIGINT" in str(CreateTable(src.t['servers']).compile(dialect=mysql.dialect())), "5b. Telegram ids are BIGINT on MySQL")
print("ALL INFRA PASSED")
