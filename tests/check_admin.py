"""Admin panel, security gates and credits: the users store, the admin API routes (ADMINS/DEV_ID),
the ban switch enforced on the API, the per-user rate limiter, the FORCE_SUB join gate, the
logo/static assets and the fork + developer credit links (kept for people who fork the bot)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import asyncio, hashlib, hmac, http.client, json, tempfile, threading, time, urllib.parse
from cryptography.fernet import Fernet
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/admin.db",
                  BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx51W2po", API_ID="1", API_HASH="0123456789abcdef0123456789abcdef",
                  ALLOWED_USERS="7", ADMINS="", DEV_ID="7", DEV_CHANNEL="testdev", WEB_RATE="600",
                  LOG_LEVEL="WARNING", SESSION_DIR=os.path.join(TMP, "sess"))
import store, webapi  # noqa: E402
import bot  # noqa: E402,F401  (webapi imports it lazily: it must load here, with the env above)

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
TOKEN, ADMIN, USER = os.environ["BOT_TOKEN"], 7, 8

# ---------------------------------------------------------------- 1. bot-level config
ok(bot.ADMINS == {ADMIN}, "1. DEV_ID lands in bot.ADMINS (the panel owner)")
ok(webapi.DEV_ID == ADMIN and webapi.DEV_CHANNEL == "testdev", "1b. webapi reads the developer credit from the env")
ok(bot.FORCE_SUB == "" and bot.FORCE_SUB_LABEL == "" and bot.FORCE_SUB_URL == "", "1c. FORCE_SUB off by default: no gate")
ok(any(c[0] == "admin" for c in bot.COMMANDS), "1d. /admin is in the bot command list")
bot.FORCE_SUB, bot.FORCE_SUB_LABEL, bot.FORCE_SUB_URL = "testchan", "@testchan", "https://t.me/testchan"
kb = bot.join_kb()
ok(kb.inline_keyboard[0][0].url == "https://t.me/testchan", "1e. the join card links to the channel")
bot.FORCE_SUB, bot.FORCE_SUB_LABEL, bot.FORCE_SUB_URL = "", "", ""

# ---------------------------------------------------------------- 2. HTTP server, two users, one admin
def make_init(token, uid, age=0):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAF17CE0",
              "user": json.dumps({"id": uid, "first_name": "User " + str(uid), "username": "user_" + str(uid)})}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    sig = hmac.new(hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest(), dcs.encode(),
                  hashlib.sha256).hexdigest()
    return "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in fields.items()) + "&hash=" + sig

class StubEngine:
    def enqueue(self, jid): pass
    async def retry(self, jid): return False
    async def stop(self, jid): pass

webapi.configure(app=None, engine=StubEngine(), token=TOKEN, allowed={ADMIN, USER}, webapp_url="", admins={ADMIN})
admin_init, user_init = make_init(TOKEN, ADMIN), make_init(TOKEN, USER)

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
srv = asyncio.run_coroutine_threadsafe(webapi.start(0, "127.0.0.1"), loop).result(10)
PORT = srv.sockets[0].getsockname()[1]

def req(method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r.status, data, r
    try:
        return r.status, json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return r.status, data

def as_(init, route, data=None):
    return req("POST", "/api/" + route, json.dumps(data or {}),
               {"X-Tg-Init-Data": init, "Content-Type": "application/json"})

# ---------------------------------------------------------------- 3. /api/me: admin flag + credit block
st, r = as_(admin_init, "me")
ok(st == 200 and r["admin"] is True and r["credit"]["repo"] == "https://github.com/Exon101x/Tg-vps-bot"
   and r["credit"]["dev_channel"] == "testdev" and r["credit"]["dev_id"] == ADMIN,
   "2. me returns the admin flag and the credit block (repo + developer)")
st, r = as_(user_init, "me")
ok(st == 200 and r["admin"] is False, "2b. a non-admin sees admin: false")

# ---------------------------------------------------------------- 4. admin routes are admin-only
ok(as_(user_init, "admin/overview")[0] == 403, "3. admin/overview refuses a non-admin (403)")
ok(as_(user_init, "admin/users")[0] == 403 and as_(user_init, "admin/ban", {"uid": ADMIN})[0] == 403
   and as_(user_init, "admin/broadcast", {"text": "hi"})[0] == 403, "3b. every admin route refuses a non-admin")
st, r = as_(admin_init, "admin/overview")
ok(st == 200 and set(r) >= {"users", "servers", "jobs", "done", "staging", "bot", "top"},
   "4. admin/overview returns every panel section")
ok(r["users"]["total"] == 2 and r["users"]["banned"] == 0 and r["users"]["allowed_list"] == 2, "4b. user counts")
ok(r["bot"]["database"].startswith("sqlite") and r["bot"]["force_sub"] in ("off", ""), "4c. bot section")
ok(r["done"]["total"] == [0, 0] and r["staging"]["cap"] == webapi.WEB_MAX, "4d. totals start at zero")
dummy = os.path.join(webapi.STAGE_DIR, "adm-dummy.part")
os.makedirs(webapi.STAGE_DIR, exist_ok=True)
open(dummy, "wb").write(b"x" * 1024)
st, r = as_(admin_init, "admin/overview")
ok(r["staging"]["files"] >= 1 and r["staging"]["bytes"] >= 1024, "4e. staging stats see the staged files")
os.remove(dummy)

st, r = as_(admin_init, "admin/users")
ok(st == 200 and sorted(u["uid"] for u in r["users"]) == [ADMIN, USER], "5. admin/users lists every known user")
row = [u for u in r["users"] if u["uid"] == USER][0]
ok(row["name"] == "User 8" and row["username"] == "user_8" and row["banned"] is False and "joined" in row,
   "5b. user rows carry name, username, joined/seen and the ban flag")
st, r = as_(admin_init, "admin/user", {"uid": USER})
ok(st == 200 and set(r) >= {"user", "servers", "usage", "audit"}, "6. admin/user detail: profile, servers, usage, audit")
ok(as_(admin_init, "admin/user", {"uid": 999})[0] == 404 and as_(admin_init, "admin/user")[0] == 400,
   "6b. unknown user 404, missing uid 400")

# ---------------------------------------------------------------- 5. ban switch, enforced on the API
ok(as_(admin_init, "admin/ban", {"uid": ADMIN})[0] == 400, "7. an admin cannot be banned through the panel")
st, r = as_(admin_init, "admin/ban", {"uid": USER, "flag": True})
ok(st == 200 and r["banned"] is True and store.is_banned(USER), "7b. admin/ban flips the flag")
st, r = as_(user_init, "me")
ok(st == 403 and "banned" in r["error"], "7c. a banned user is locked out of the API at once")
st, r = as_(admin_init, "admin/ban", {"uid": USER, "flag": False})
ok(st == 200 and not store.is_banned(USER) and as_(user_init, "me")[0] == 200, "7d. unban restores access")
ok(any(a["server"] == "admin" and "banned user 8" in a["action"] for a in store.recent_log(ADMIN, 30)),
   "7e. ban/unban lands in the audit log")

# ---------------------------------------------------------------- 6. broadcast
sent = []
async def fake_broadcast(text, admin_uid):
    sent.append((text, admin_uid))
    return 1, 0
real_broadcast = bot.broadcast
bot.broadcast = fake_broadcast
ok(as_(admin_init, "admin/broadcast", {})[0] == 400, "8. broadcast needs text")
ok(as_(admin_init, "admin/broadcast", {"text": "x" * 3501})[0] == 400, "8b. broadcast rejects 3501+ chars")
st, r = as_(admin_init, "admin/broadcast", {"text": "hello everyone"})
ok(st == 200 and r["queued"] == len(store.user_ids()), "8c. broadcast queues to every known user")
for _ in range(100):
    if sent:
        break
    time.sleep(0.05)
ok(sent and sent[0] == ("hello everyone", ADMIN), "8d. the broadcast task runs with the admin's id")
bot.broadcast = real_broadcast

# ---------------------------------------------------------------- 7. rate limiter
webapi.RATE = 5
webapi._bucks.clear()
ok(all(webapi.rate_ok(12345) for _ in range(5)) and not webapi.rate_ok(12345),
   "9. the token bucket gives RATE requests, then refuses")
fresh = make_init(TOKEN, 88)  # uid 88 not in ALLOWED: refused earlier, so use a fresh bucket on USER
webapi._bucks.clear()
st = [as_(user_init, "me")[0] for _ in range(6)]
ok(st[:5] == [200] * 5 and st[5] == 429, f"9b. the 6th request in a burst gets 429 ({st})")
webapi.RATE = 600
webapi._bucks.clear()

# ---------------------------------------------------------------- 8. FORCE_SUB join gate
bot.FORCE_SUB, bot.FORCE_SUB_LABEL, bot.FORCE_SUB_URL = "testchan", "@testchan", "https://t.me/testchan"
async def no(uid):
    return False
async def yes(uid):
    return True
real_sub_ok = bot.sub_ok
bot.sub_ok = no
st, r = as_(user_init, "me")
ok(st == 403 and r.get("need_join") and r.get("channel") == "@testchan" and r.get("url") == "https://t.me/testchan",
   "10. a non-subscriber gets a 403 join card (channel + link)")
ok(as_(admin_init, "me")[0] == 200, "10b. admins are exempt from the force-subscribe gate")
bot.sub_ok = yes
ok(as_(user_init, "me")[0] == 200, "10c. a subscriber passes the gate")
bot.sub_ok = real_sub_ok
bot.FORCE_SUB, bot.FORCE_SUB_LABEL, bot.FORCE_SUB_URL = "", "", ""
ok(as_(user_init, "me")[0] == 200, "10d. gate off again: everyone back in")

# ---------------------------------------------------------------- 9. logo, credits, security headers
st, body, resp = req("GET", "/static/logo.png", raw=True)
ok(st == 200 and resp.getheader("Content-Type") == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"
   and len(body) > 1000, "11. the bot logo is served as a real PNG")
st, page, resp = req("GET", "/app", raw=True)
csp = resp.getheader("Content-Security-Policy") or ""
ok(st == 200 and b"github.com/Exon101x/Tg-vps-bot" in page and b"/static/logo.png" in page
   and b"Fork on GitHub" in page, "12. /app shows the logo and keeps the fork link in the footer")
ok("object-src 'none'" in csp and "frame-ancestors https://web.telegram.org" in csp,
   "12b. CSP: no plugins, only Telegram may frame the app")
ok((resp.getheader("Referrer-Policy") or "") == "no-referrer", "12c. Referrer-Policy on static pages")
st, body, resp = req("POST", "/api/me", "{}", {"X-Tg-Init-Data": admin_init}, raw=True)
ok((resp.getheader("Referrer-Policy") or "") == "no-referrer", "12d. Referrer-Policy on API answers")
html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp", "index.html")).read()
ok('href="https://github.com/Exon101x/Tg-vps-bot"' in html and 'id="tab-admin"' in html and 'id="joingate"' in html,
   "13. the page ships the admin tab, the join gate and the credit links")
js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp", "app.js")).read()
ok("admin/overview" in js and "admin/broadcast" in js and "needJoin" in js and "dev_channel" in js,
   "13b. the app script drives the admin tab, the join gate and the credits")

print("ALL ADMIN CHECKS PASSED")
