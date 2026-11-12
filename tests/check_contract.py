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

# --- users (admin panel: tracking + ban switch; every backend)
first = store.user(BIG)
ok(first and first["joined"] > 0 and first["banned"] == 0, f"[{label}] add_job tracks the user (auto row)")
store.touch_user(BIG, "Alice", "alice_x")
u = store.user(BIG)
ok(u["name"] == "Alice" and u["username"] == "alice_x" and u["joined"] == first["joined"], "touch_user refreshes name/username, keeps joined")
store.touch_user(BIG, "Alicia", "alice_x")
ok(store.user(BIG)["name"] == "Alicia", "touch_user updates again")
store.ban_user(BIG, True)
ok(store.user(BIG)["banned"] == 1 and store.is_banned(BIG), "ban_user flips the flag and is_banned sees it at once (cache invalidated)")
store.ban_user(BIG, False)
ok(not store.is_banned(BIG), "unban clears it")
other = BIG + 7
store.touch_user(other, "Bob", "")
ok(sorted(r["uid"] for r in store.users(10)) == sorted([BIG, other]) and sorted(store.user_ids()) == sorted([BIG, other]), "users/user_ids list everyone")
ok(store.user_counts() == (2, 0), "user_counts")
ok(store.user(other)["name"] == "Bob" and store.user(BIG + 8) is None, "user() by id, missing -> None")
ok(store.server_counts() == (1, 0), "server_counts across users (the re-add after delete reset monitor)")
top = store.top_users(10)
ok(top and top[0] == (BIG, 1, 5_000_000_000), f"top_users biggest mover first {top}")
ok(store.global_jobs(("done",)) == (1, 5_000_000_000), "global_jobs totals across users")
states = store.jobs_by_state()
ok(states.get("done") == 1 and states.get("failed") == 1 and states.get("queued") == 4, f"jobs_by_state {states}")
ok(store.global_jobs(("done",), since=int(time.time()) + 60) == (0, 0), "global_jobs respects since")

# --- blobs (session snapshot)
blob = os.urandom(200_000) + b"\x00\x01\x02"
store.save_blob("session", blob); ok(store.load_blob("session") == blob and store.load_blob("nope") is None, "encrypted binary blob round-trip (200 KB)")
store.kv_set("session_token", "abc"); ok(store.kv_get("session_token") == "abc", "kv")

# --- subscriptions: orders, sub rows, VPS allocations (every backend)
uid = BIG + 11
o1 = store.order_add(uid, "vps1", 500, "USD")
o = store.order_get(o1)
ok(o and o["plan"] == "vps1" and o["cents"] == 500 and o["status"] == "pending" and o["ref"] == "" and o["currency"] == "USD", f"[{label}] order_add/get defaults {o}")
store.order_set(o1, ref="UPI123456", status="awaiting")
o = store.order_get(o1); ok(o["ref"] == "UPI123456" and o["status"] == "awaiting", "order_set updates fields")
o2 = store.order_add(uid, "pro", 250, "USD")
ok(o2 == o1 + 1, f"order ids auto-increment ({o1}, {o2})")
store.order_set(o2, status="cancelled", decided=int(time.time()))
ok([x["id"] for x in store.order_list(uid)] == [o2, o1], "order_list newest first, per user")
ok(store.orders_pending(10) and store.orders_pending(10)[0]["id"] == o1, "orders_pending returns open orders only")
counts, (n, cents) = store.order_stats()
ok(counts.get("awaiting") == 1 and n == 0 and cents == 0, f"order_stats before approval {counts} {(n, cents)}")
store.order_set(o1, status="approved", decided=int(time.time()))
counts, (n, cents) = store.order_stats()
ok(counts.get("approved") == 1 and n == 1 and cents == 500, f"order_stats revenue after approval {counts} {(n, cents)}")
ok(store.order_get(99999) is None and store.order_list(BIG) == [], "orders: missing -> None, per user")

store.sub_set(uid, "vps1", int(time.time()) + 30 * 86400)
s = store.sub_get(uid)
ok(s and s["plan"] == "vps1" and s["expires"] > int(time.time()) + 29 * 86400, "sub_set/get round-trip")
store.sub_set(uid, "vps1", int(time.time()) + 60 * 86400)
ok(store.sub_get(uid)["expires"] > int(time.time()) + 59 * 86400, "sub_set replaces (one sub row per user)")
ok(store.sub_get(BIG) is None, "sub rows are per user")

a1 = store.alloc_add(uid, "vps1", "node.example", 22, "tg1234abcd", "Sup3rSecret9pass", 1, 1024, 15, int(time.time()) + 30 * 86400)
a = store.alloc_get(a1)
ok(a and a["user"] == "tg1234abcd" and a["secret"] == "Sup3rSecret9pass" and a["host"] == "node.example"
   and a["status"] == "active" and a["cpu"] == 1 and a["ram"] == 1024 and a["disk"] == 15, "alloc_add/get round-trips, secret decrypts")
ok(store.B.alloc_get(a1)["secret"] != "Sup3rSecret9pass", "allocation password is Fernet-encrypted at rest")
a2 = store.alloc_add(uid, "vps1", "node.example", 22, "tg5678efgh", "Another9pass", 2, 2048, 40, int(time.time()) + 30 * 86400)
ok(a2 == a1 + 1, f"alloc ids auto-increment ({a1}, {a2})")
ok(len(store.alloc_list(uid)) == 2 and store.alloc_list(uid)[0]["id"] == a1, "alloc_list in id order, per user")
ok(store.alloc_active_count(uid) == 2, "alloc_active_count")
store.alloc_set(a2, expires=int(time.time()) - 10)   # still active, but past its time
ok([x["id"] for x in store.allocs_expiring(int(time.time()))] == [a2], "allocs_expiring returns past-expiry actives only")
store.alloc_set(a2, status="expired")
ok(store.alloc_active_count(uid) == 1 and store.alloc_get(a2)["status"] == "expired", "alloc_set updates status")
store.alloc_set(a2, secret="Rotated9pass")
ok(store.alloc_get(a2)["secret"] == "Rotated9pass" and store.B.alloc_get(a2)["secret"] != "Rotated9pass", "alloc_set re-encrypts secrets")
ok(store.alloc_counts() == (2, 1), "alloc_counts across users")
ok(store.alloc_get(99999) is None, "missing allocation -> None")
try: store.alloc_set(a1, evil=1); ok(False, "bad alloc field")
except ValueError: ok(True, "alloc_set rejects unknown fields")
try: store.order_set(o1, evil=1); ok(False, "bad order field")
except ValueError: ok(True, "order_set rejects unknown fields")
print(f"CONTRACT OK [{label}]")
