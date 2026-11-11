import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

"""Runs the same scenario against whatever DATABASE_URL points to (called by run_backends.sh)."""
import os, sys, time
from cryptography.fernet import Fernet
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())
if os.environ.get("USE_MONGOMOCK"):
    import mongomock, pymongo
    pymongo.MongoClient = mongomock.MongoClient
import store

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
import random
BIG = 7_000_000_000 + random.randrange(10**9)   # Telegram ids exceed 2^31: must be BIGINT
label = store.LABEL

def srv(**kw):
    c = dict(uid=BIG, name="vps-ü", scheme="ssh", host="h.example", port=22, user="root", auth="password", secret="s3cr€t", cwd="/root", hostkey="ssh-ed25519 AAAA")
    c.update(kw); return c

# --- servers
store.add_server(srv())
g = store.get_server(BIG, "vps-ü")
ok(g and g["secret"] == "s3cr€t" and g["hostkey"] == "ssh-ed25519 AAAA" and g["folder"] == "" and g["dup"] == "overwrite", f"[{label}] add/get server, secret decrypts, defaults, BIGINT uid, unicode name")
ok(store.active_name(BIG) == "vps-ü", "active server set on add")
store.update(BIG, "vps-ü", folder="{user}/{date}", dup="rename", monitor=1, cwd="/srv")
store.add_server(srv(secret="new-pass"))                       # re-add keeps settings
g = store.get_server(BIG, "vps-ü")
ok(g["secret"] == "new-pass" and g["folder"] == "{user}/{date}" and g["dup"] == "rename" and g["monitor"] == 1, "re-adding keeps folder/dup/monitor, replaces secret")
rows = store.list_servers(BIG)
ok(len(rows) == 1 and "secret" not in rows[0] and "hostkey" not in rows[0] and rows[0]["folder"] == "{user}/{date}", "list_servers never exposes secrets")
store.add_server(srv(name="ftp1", scheme="ftp", port=21))
ok([r["name"] for r in store.list_servers(BIG)] == sorted(["ftp1", "vps-ü"]), "multiple servers listed")
ok(store.set_active(BIG, "vps-ü") and store.active_name(BIG) == "vps-ü" and not store.set_active(BIG, "nope"), "set_active")
ok([s["name"] for s in store.monitored()] == ["vps-ü"], "monitored() returns only monitored servers (decrypted)")
ok(store.monitored()[0]["secret"] == "new-pass", "monitored() decrypts")
ok(store.delete_server(BIG, "ftp1") and not store.delete_server(BIG, "ftp1"), "delete_server")
store.delete_server(BIG, "vps-ü"); ok(store.active_name(BIG) is None and store.get_server(BIG) is None, "deleting the active server clears it")
try: store.update(BIG, "x", evil="1"); ok(False, "bad field")
except ValueError: ok(True, "update() rejects unknown fields")

# --- jobs
store.add_server(srv())
j1 = store.add_job(BIG, "vps-ü", BIG, f"tg:{BIG}:5", "naïve-ファイル.txt", "dir/naïve-ファイル.txt", 1234)
j2 = store.add_job(BIG, "vps-ü", BIG, "url:https://x/y", "y.bin", "y.bin", 0)
ok(j2 == j1 + 1, f"job ids auto-increment ({j1}, {j2})")
j = store.get_job(j1)
ok(j["name"] == "naïve-ファイル.txt" and j["chat_id"] == BIG and j["state"] == "queued" and j["error"] == "" and j["size"] == 1234, "get_job round-trips unicode + BIGINT")
store.set_job(j1, state="running", attempts=2, status_id=99, size=5_000_000_000, dest="a (1).txt", resume=1)
j = store.get_job(j1); ok(j["state"] == "running" and j["attempts"] == 2 and j["size"] == 5_000_000_000 and j["dest"] == "a (1).txt" and j["resume"] == 1 and j["status_id"] == 99, "set_job updates (incl. >2^32 size)")
ok([x["id"] for x in store.pending_jobs()] == [j1, j2], "pending_jobs in id order")
ok(store.count_active(BIG, "vps-ü") == 2 and store.count_active(BIG, "other") == 0, "count_active")
store.set_job(j1, state="done"); store.set_job(j2, state="failed", error="boom")
ok([x["id"] for x in store.list_jobs(BIG)] == [j2, j1], "list_jobs newest first")
ok([x["id"] for x in store.list_jobs(BIG, ("failed",))] == [j2] and store.list_jobs(BIG, ("failed",))[0]["error"] == "boom", "list_jobs filters by state")
u = store.usage(BIG); ok(u["today"] == (1, 5_000_000_000) and u["week"] == (1, 5_000_000_000) and u["failed"] == 1 and u["pending"] == 0, f"usage {u}")
ok(store.get_job(99999) is None, "missing job -> None")
ok(store.list_jobs(BIG + 1) == [], "jobs are per user")

# --- direct 2-4 GB transfers (Premium files): sizes past every 32-bit boundary insert and round-trip
for n, size in enumerate((2 ** 31, 2 ** 32, 4 * 1024 ** 3, 4 * 1024 ** 3 + 1)):
    jn = store.add_job(BIG, "vps-ü", BIG, f"tg:{BIG}:{80 + n}", f"big{n}.bin", f"big{n}.bin", size)
    ok(store.get_job(jn)["size"] == size, f"add_job stores size {size} exactly (BIGINT on every backend)")
store.kv_set("ask:42", "1"); ok(store.kv_get("ask:42") == "1", "kv (per-user settings like /ask)")

# --- audit
for i in range(5): store.log(BIG, "vps-ü", f"cmd {i}"); time.sleep(0.01)
ok(len(store.recent_log(BIG, 3)) == 3 and store.recent_log(BIG, 3)[0]["action"].startswith("cmd"), "audit log recent")

# --- blobs (session snapshot)
blob = os.urandom(200_000) + b"\x00\x01\x02"
store.save_blob("session", blob); ok(store.load_blob("session") == blob and store.load_blob("nope") is None, "encrypted binary blob round-trip (200 KB)")
store.kv_set("session_token", "abc"); ok(store.kv_get("session_token") == "abc", "kv")
print(f"CONTRACT OK [{label}]")
