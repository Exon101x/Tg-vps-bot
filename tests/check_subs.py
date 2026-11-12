"""Subscriptions & VPS hosting: plan catalog, order lifecycle, plan limits, provisioning, security.
End-to-end through subscriptions.py, hosting.py (fake SSH node), store.py and the bot's approve flow."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio, json, tempfile, time
from cryptography.fernet import Fernet
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/s.db", BOT_TOKEN="123:abc",
                  SESSION_DIR=f"{TMP}/sess", API_ID="1", API_HASH="x", ALLOWED_USERS="5,777", LOG_LEVEL="WARNING",
                  VPS_NODE_HOST="10.66.0.1", VPS_NODE_USER="root", VPS_NODE_PASS="node-pass")
import store, subscriptions, hosting
import bot  # registers the handlers, like check_hosting.py; never connects to Telegram here
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
UID = 555000111
store.touch_user(UID, "Sam", "sam_buyer")   # the admin panel enumerates known users


class FakeRemote:
    """Stands in for remote.Remote on the host node: records commands, returns a canned result."""
    calls = []
    rc, out = 0, ""

    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def exec(self, cmd, timeout):
        FakeRemote.calls.append(cmd)
        return (FakeRemote.out, FakeRemote.rc)


hosting.Remote = FakeRemote

# ---- 1. plan catalog: defaults, env override, bad input falls back
ok(len(subscriptions.PLANS) == 4 and subscriptions.plan("free") and subscriptions.plan("vps2"), "1. default catalog: free + pro + two VPS plans")
ok(subscriptions.plan("free").free and not subscriptions.plan("vps1").free, "1b. free is free, VPS plans are paid")
ok(subscriptions.plans()[0].key == "free", "1c. free sorts first")
custom = subscriptions._parse_plans(json.dumps([
    {"key": "free", "name": "Free", "price": 0, "max_servers": 5},
    {"key": "vip", "name": "VIP", "price": 9.99, "days": 7, "vps_slots": 3, "vps_cpu": 4, "vps_ram": 8192, "vps_disk": 100}]))
ok([p.key for p in custom] == ["free", "vip"] and custom[1].vps_slots == 3 and custom[1].cents == 999, "2. SUBSCRIPTION_PLANS JSON override parses (price kept in cents)")
ok(len(subscriptions._parse_plans('[{"name": "NoKey"}]')) == 4, "2b. malformed plans fall back to the defaults")
ok(subscriptions._parse_plans("not json at all") == subscriptions._default_plans(), "2c. garbage env falls back safely")
ok(subscriptions.plan("vps2").vps_slots == 2 and subscriptions.plan("vps1").vps_disk == 15, "2d. VPS specs parsed")

# ---- 3. no subscription = free, with limits
ok(subscriptions.effective(UID).key == "free", "3. a stranger gets the free plan")
for i in range(3):
    store.add_server(dict(uid=UID, name=f"s{i}", scheme="ssh", host="h", port=22, user="root", auth="password",
                          secret="x", cwd="", hostkey=""))
reached, p = subscriptions.server_limit_reached(UID)
ok(reached and p.key == "free", "3b. the free plan's server limit trips at 3 (this is the /add gate)")
left, _ = subscriptions.vps_slots_left(UID)
ok(left == 0, "3c. free plan includes no VPS")

# ---- 4. order lifecycle: create -> pay ref -> admin approves -> plan active + VPS provisioned
o, fresh = subscriptions.order_create(UID, subscriptions.plan("vps1"))
ok(fresh and o["status"] == "pending" and o["cents"] == 500, f"4. order created pending at 500 cents {o}")
o2, fresh2 = subscriptions.order_create(UID, subscriptions.plan("vps1"))
ok(not fresh2 and o2["id"] == o["id"], "4b. re-picking the same plan reuses the open order")
o3, _ = subscriptions.order_create(UID, subscriptions.plan("vps2"))
ok(o3["id"] != o["id"] and store.order_get(o["id"])["status"] == "cancelled", "4c. picking another plan cancels the old basket")
o, _ = subscriptions.order_create(UID, subscriptions.plan("vps1"))   # back to the VPS Starter basket
ok(o["status"] == "pending" and store.order_get(o3["id"])["status"] == "cancelled", "4c2. one open basket: switching back cancels the other")
o, error = subscriptions.order_submit_ref(UID, "  UPI-8837291  ")
ok(not error and o["status"] == "awaiting" and o["ref"] == "UPI-8837291", "4d. /paid attaches the reference (whitespace-normalised)")
dup, error = subscriptions.order_submit_ref(UID, "UPI-2")
ok(error and "already" in error, "4e. double /paid while awaiting says so instead of a second order")
none, error = subscriptions.order_submit_ref(UID + 9, "UPI-3")
ok(error and "No pending order" in error, "4f. /paid without an order points at /plans")
ok(bool(subscriptions.order_submit_ref(UID, "")[1]), "4g. /paid without a reference is refused")

before = int(time.time())
order, error = asyncio.run(bot.approve_order(o["id"], 777, True))
ok(not error and order["status"] == "approved", f"5. admin approves the order {order}")
row = store.sub_get(UID)
ok(row and row["plan"] == "vps1" and subscriptions.effective(UID).key == "vps1", "5b. the plan is active immediately")
ok(row["expires"] - before >= 30 * 86400 - 60, "5c. 30-day period")
reached, p = subscriptions.server_limit_reached(UID)
ok(not reached and p.max_servers == 25, "5d. the VPS plan lifts the server limit (25)")
allocs = store.alloc_list(UID)
ok(len(allocs) == 1 and allocs[0]["status"] == "active", "5e. the included VPS was provisioned")
a = allocs[0]
ok(a["host"] == "10.66.0.1" and a["cpu"] == 1 and a["ram"] == 1024 and a["disk"] == 15, f"5f. allocation carries the node + plan specs {a}")
ok(a["expires"] == row["expires"], "5g. the VPS lives exactly as long as the subscription")
cmd = FakeRemote.calls[0]
ok("useradd -m -s /bin/bash " + a["user"] in cmd and "chpasswd" in cmd, f"5h. provision command ran on the node: {cmd}")
ok(a["user"] in cmd and a["secret"] in cmd, "5i. the node command carries the exact generated credentials")
shape = all(u.isalnum() and u[0].isalpha() and 6 <= len(u) <= 16 and pw.isalnum() and len(pw) == 16
            for u, pw in (hosting.new_credentials(UID) for _ in range(50)))
ok(shape, "5j. 50 credential pairs: all alnum (letter-first user, 16-char password) - nothing can inject into a shell")
ok(store.B.alloc_get(a["id"])["secret"] != a["secret"], "5k. the VPS password is Fernet-encrypted at rest")

# ---- 6. renewal: extends from where you are, keeps the VPS, tops up slots
old_expiry = row["expires"]
o, _ = subscriptions.order_create(UID, subscriptions.plan("vps2"))
subscriptions.order_submit_ref(UID, "UPI-9911")
order, error = asyncio.run(bot.approve_order(o["id"], 777, True))
ok(not error, "6. renewal order approved")
row = store.sub_get(UID)
ok(row["plan"] == "vps2" and row["expires"] == old_expiry + 30 * 86400, "6b. renewal extends from the old expiry")
allocs = store.alloc_list(UID)
ok(len(allocs) == 2 and all(x["status"] == "active" for x in allocs), "6c. the old VPS was kept (expiry moved) and the 2nd slot filled")
ok(allocs[0]["expires"] == row["expires"] == allocs[1]["expires"], "6d. both VPSes now run until the new expiry")

# ---- 7. decline + double decision + stats
o, _ = subscriptions.order_create(UID, subscriptions.plan("pro"))
subscriptions.order_submit_ref(UID, "UPI-BAD")
order, error = asyncio.run(bot.approve_order(o["id"], 777, False))
ok(not error and order["status"] == "rejected", "7. decline works, the buyer is notified (send fails silently here)")
_, _, error = subscriptions.order_decide(o["id"], True, 777)
ok(error and "already rejected" in error, "7b. deciding twice refuses")
stats = subscriptions.stats()
ok(stats["subs_active"] == 1 and stats["approved"] == 2 and stats["vps_active"] == 2 and "USD" in stats["revenue"], f"7c. admin stats {stats}")

# ---- 8. the chat UI text builders render
cases = [
    ("plans_text", lambda: bot.plans_text(UID), "VPS Starter"),
    ("mysub_text", lambda: bot.mysub_text(UID), "VPS Pro"),
    ("myvps_text", lambda: bot.myvps_text(UID), "10.66.0.1"),
    ("admin_overview_text", lambda: bot.admin_overview_text(), "revenue"),
    ("admin_orders_text", lambda: bot.admin_orders_text(), "No open orders"),
    ("HOSTING_GUIDE", lambda: bot.HOSTING_GUIDE, "60-second"),
    ("payment_info", lambda: subscriptions.payment_info(subscriptions.plan("vps1")), "/paid"),
    ("order_text", lambda: bot.order_text(store.order_get(o["id"]), subscriptions.plan("pro")), "How to pay"),
]
for name, get, needle in cases:
    text = get()
    ok(needle in text, f"8. {name} renders with {needle!r}")

# ---- 9. expiry sweep: VPS removed from the node, buyer notified, sub lapsed
store.sub_set(UID, "vps2", int(time.time()) - 10)
subscriptions._cache.pop(UID, None)   # the plan cache holds for 60s: simulate the window passing
for a in store.alloc_list(UID):
    store.alloc_set(a["id"], expires=int(time.time()) - 10)
FakeRemote.calls.clear()
notes = hosting.sweep()
ok(len(notes) == 2 and all(x[0] == UID for x in notes) and "removed" in notes[0][1], "9. sweep returns a note per expired VPS")
live = store.alloc_list(UID)
ok(all("userdel -rf " + a["user"] in FakeRemote.calls[i] for i, a in enumerate(live)), "9b. deprovision ran for each instance")
ok(all(x["status"] == "expired" for x in live), "9c. allocations marked expired")
ok(subscriptions.effective(UID).key == "free", "9d. lapsed sub falls back to free")

# ---- 10. provisioning failure: reported to the admin, never blocks the approval
FakeRemote.rc, FakeRemote.out = 3, "useradd: cannot lock /etc/passwd"
o, _ = subscriptions.order_create(UID, subscriptions.plan("vps1"))
subscriptions.order_submit_ref(UID, "UPI-42")
order, error = asyncio.run(bot.approve_order(o["id"], 777, True))
ok(not error and order["status"] == "approved" and subscriptions.effective(UID).key == "vps1", "10. a failing node still approves the plan")
ok(store.alloc_active_count(UID) == 0, "10b. no allocation is recorded when the node says no")
FakeRemote.rc, FakeRemote.out = 0, ""

# ---- 11. unconfigured node: order flows, plan activates, VPS waits
saved = hosting.NODE_HOST
hosting.NODE_HOST = ""
ok(hosting.node_config() is None, "11. node_config is None when the node is not configured")
o, _ = subscriptions.order_create(UID, subscriptions.plan("vps1"))
subscriptions.order_submit_ref(UID, "UPI-43")
order, error = asyncio.run(bot.approve_order(o["id"], 777, True))
ok(not error and subscriptions.effective(UID).key == "vps1" and store.alloc_active_count(UID) == 0, "11b. without VPS_NODE_HOST the plan still activates (the admin sees the warning)")
try:
    hosting.provision(UID, subscriptions.plan("vps1"), int(time.time()) + 86400)
    ok(False, "11c. provision without a node raises")
except RuntimeError as e:
    ok("not configured" in str(e), "11c. provision without a node raises a clear error")
hosting.NODE_HOST = saved

# ---- 12. non-root node users get sudo; templates render exactly
saved_user = hosting.NODE_USER
hosting.NODE_USER = "deploy"
ok(hosting._render("id {user}", "tguser", "pw1234567890abc", 1, 1, 1).startswith("sudo -n id tguser"), "12. non-root node users get sudo -n")
hosting.NODE_USER = saved_user
cmd = hosting._render("echo {user} {pass} {cpu} {ram} {disk}", "tguser", "pw1234567890abc", 2, 2048, 40)
ok(cmd == "echo tguser pw1234567890abc 2 2048 40", f"12b. templates render exactly: {cmd}")

# ---- 13. store hygiene: one user's orders never leak into another's views
ok(store.order_list(UID + 5) == [] and store.sub_get(UID + 5) is None and store.alloc_list(UID + 5) == [], "13. orders/subs/allocs are per user")

print("SUBS OK")
