"""webapi: the Mini App backend - initData signature, the HTTP router's auth gate, the server/file
APIs against a real local SSH server, and the chunked resumable upload path end-to-end through the
job queue (src "web:<staged file>"), including cleanup and the lost-staging failure."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import asyncio, hashlib, hmac, http.client, json, tempfile, threading, time, urllib.parse
from cryptography.fernet import Fernet
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/web.db",
                  BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx51W2po", API_ID="1", API_HASH="0123456789abcdef0123456789abcdef",
                  ALLOWED_USERS="7", WEB_RATE="600", LOG_LEVEL="WARNING", SESSION_DIR=os.path.join(TMP, "sess"))
import asyncssh, store, webapi  # noqa: E402
import bot  # noqa: E402,F401  (webapi imports it lazily at call time: it must load here, with the env above)
import hosting  # noqa: E402
import jobs  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
TOKEN, UID = os.environ["BOT_TOKEN"], 7

# ---------------------------------------------------------------- 1. initData signature
def make_init(token, uid, age=0, query_id="AAF17CE0"):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": query_id,
              "user": json.dumps({"id": uid, "first_name": "Tester", "username": "tester_" + str(uid)})}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    sig = hmac.new(hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest(), dcs.encode(),
                  hashlib.sha256).hexdigest()
    return "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in fields.items()) + "&hash=" + sig

good = make_init(TOKEN, UID)
pairs = webapi.check_init_data(good, TOKEN)
ok(pairs and json.loads(pairs["user"])["id"] == UID, "1. valid initData verifies and parses")
ok(webapi.check_init_data(good + "x", TOKEN) is None, "1b. tampered initData rejected")
ok(webapi.check_init_data(make_init(TOKEN, UID, age=2 * 86400), TOKEN) is None, "1c. stale auth_date rejected")
ok(webapi.check_init_data(good, TOKEN + "x") is None, "1d. a different bot token's signature rejected")
ok(webapi.check_init_data(good.split("&hash=")[0], TOKEN) is None, "1e. initData without a hash rejected")
ok(webapi.check_init_data(good.replace("query_id", "queryid"), TOKEN) is None, "1f. renamed field breaks the signature (dcs is exact)")

class StubEngine:
    seen = []
    def enqueue(self, jid): StubEngine.seen.append(jid)
    async def retry(self, jid): return False
    async def stop(self, jid): pass

webapi.configure(app=None, engine=StubEngine(), token=TOKEN, allowed={UID}, webapp_url="")
u = webapi.auth({"x-tg-init-data": good})
ok(u and u["id"] == UID and u["username"] == "tester_7", "2. auth() returns the signed-in Telegram user")

# ---------------------------------------------------------------- 2. HTTP server + auth gate
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
fut = asyncio.run_coroutine_threadsafe(webapi.start(0, "127.0.0.1"), loop)
srv = fut.result(10)
PORT = srv.sockets[0].getsockname()[1]

def req(method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r.status, data
    try:
        return r.status, json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return r.status, data

def authed(route, data=None, extra=None):
    return req("POST", "/api/" + route, json.dumps(data or {}),
               {"X-Tg-Init-Data": good, "Content-Type": "application/json", **(extra or {})})

def reqh(method, path, body=None, headers=None):
    """req(), but returns the response headers too (CORS checks need them)."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    hs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hs, data

status, body = req("GET", "/health")
ok(status == 200 and body == {"status": "ok"}, "3. /health still answers the platform JSON")
ok(req("GET", "/healthz")[0] == 200, "3b. /healthz answers (render.yaml's path)")
status, page = req("GET", "/app", raw=True)
ok(status == 200 and b"vpsbot" in page and b"/static/app.js" in page, "3c. /app serves the mini app page")
c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
c.request("GET", "/app"); r = c.getresponse(); r.read(); csp = r.getheader("Content-Security-Policy") or ""; c.close()
ok("script-src 'self' https://telegram.org" in csp, "3d. CSP only allows own scripts + telegram-web-app.js")
ok(req("GET", "/static/app.js", raw=True)[0] == 200, "3e. /static/app.js served")
ok(req("GET", "/static/webapi.py", raw=True)[0] == 404, "3f. non-whitelisted paths are 404 (no traversal)")
ok(req("GET", "/static/../../bot.py", raw=True)[0] == 404, "3g. traversal attempts are 404")
ok(req("GET", "/")[0] == 200, "3h. unknown paths still answer 200 (old health behaviour)")
ok(authed("me")[1]["servers"] == [], "3i. /api/me lists the user's (empty) server set")

# ---------------------------------------------------------------- 3j-3q. CORS: the app hosted elsewhere
# The default test bot has no WEBAPP_URL and no ALLOWED_ORIGINS: every origin must be refused.
st, hs, _ = reqh("OPTIONS", "/api/me", headers={"Origin": "https://stranger.example",
                                                "Access-Control-Request-Method": "POST"})
ok(st == 403 and "access-control-allow-origin" not in hs,
   "3j. preflight from an unlisted origin is refused (no ACAO header)")
st, hs, body = reqh("POST", "/api/me", json.dumps({}), {"Origin": "https://stranger.example",
                                                        "X-Tg-Init-Data": good})
ok(st == 200 and "access-control-allow-origin" not in hs and b"servers" in body,
   "3k. a real request from an unlisted origin still works but carries no CORS headers (curl-like: no browser involved)")
# Now allow an origin like a split setup would, and check the full preflight dance.
webapi.ALLOWED_ORIGINS.add("https://bot-app.pages.dev")
st, hs, _ = reqh("OPTIONS", "/api/me", headers={"Origin": "https://bot-app.pages.dev",
                                                "Access-Control-Request-Method": "POST",
                                                "Access-Control-Request-Headers": "content-type, x-tg-init-data"})
ok(st == 204 and hs.get("access-control-allow-origin") == "https://bot-app.pages.dev"
   and "x-tg-init-data" in hs.get("access-control-allow-headers", "").lower()
   and hs.get("access-control-max-age") == "86400",
   "3l. preflight from the allowlisted origin: 204, the origin echoed, app headers allowed, cacheable")
st, hs, _ = reqh("OPTIONS", "/api/up/chunk", headers={"Origin": "https://bot-app.pages.dev",
                                                      "Access-Control-Request-Method": "POST"})
ok("x-up-offset" in hs.get("access-control-allow-headers", "").lower(),
   "3m. the upload chunk headers (X-Up-Token/X-Up-Offset) are allowed too - big uploads survive CORS")
st, hs, body = reqh("POST", "/api/me", json.dumps({}), {"Origin": "https://bot-app.pages.dev",
                                                        "X-Tg-Init-Data": good})
ok(st == 200 and hs.get("access-control-allow-origin") == "https://bot-app.pages.dev",
   "3n. an authed cross-origin POST carries the CORS header (errors too: the header is on every /api answer)")
st, hs, body = reqh("POST", "/api/me", json.dumps({}), {"Origin": "https://bot-app.pages.dev"})
ok(st == 401 and hs.get("access-control-allow-origin") == "https://bot-app.pages.dev",
   "3o. a cross-origin POST without initData: 401, still CORS-labelled (the browser can read the error)")
webapi.WEBAPP_URL = "https://vps.example.com"
st, hs, _ = reqh("OPTIONS", "/api/me", headers={"Origin": "https://vps.example.com",
                                                "Access-Control-Request-Method": "POST"})
ok(st == 204, "3p. WEBAPP_URL is allowed without repeating it in ALLOWED_ORIGINS")
st, hs, _ = reqh("OPTIONS", "/api/me", headers={"Origin": "https://vps.example.com/",
                                                "Access-Control-Request-Method": "POST"})
ok(st == 204, "3q. a trailing slash on the origin matches (origins are normalised)")
webapi.WEBAPP_URL = ""
webapi.ALLOWED_ORIGINS.clear()
ok(req("POST", "/api/me", "{}", {"Content-Type": "application/json"})[0] == 401, "3r. no signature -> 401")
ok(authed("me", extra=None)[0] == 200, "3s. signed request from an allowed user -> 200")
other = make_init(TOKEN, 8)  # uid 8 is NOT in ALLOWED_USERS {7}
ok(req("POST", "/api/me", "{}", {"X-Tg-Init-Data": other})[0] == 403, "3t. signed but not on ALLOWED_USERS -> 403")
ok(authed("nope")[0] == 404, "3u. unknown route -> 404")
ok(req("GET", "/api/me", headers={"X-Tg-Init-Data": good})[0] == 405, "3v. GET on an api route -> 405")

# ---------------------------------------------------------------- 3. local SSH server + server APIs
ROOT = tempfile.mkdtemp()
class Srv(asyncssh.SSHServer):
    def password_auth_supported(self): return True
    def validate_password(self, u, p): return u == "root" and p == "pw"

async def handle(process):
    proc = await asyncio.create_subprocess_exec("bash", "-c", process.command, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, cwd=ROOT)
    out, _ = await proc.communicate()
    process.stdout.write(out.decode(errors="replace")); process.exit(proc.returncode)

ready, info = threading.Event(), {}
def serve():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    key = asyncssh.generate_private_key("ssh-ed25519")
    async def go():
        s = await asyncssh.listen("127.0.0.1", 0, server_factory=Srv, server_host_keys=[key],
                                  process_factory=handle, sftp_factory=True)
        info["port"] = s.sockets[0].getsockname()[1]; ready.set()
    loop.run_until_complete(go()); loop.run_forever()
threading.Thread(target=serve, daemon=True).start(); ready.wait(10)

st, r = authed("servers/add", {"name": "vps", "target": f"ssh://root:pw@127.0.0.1:{info['port']}"})
ok(st == 200 and r["name"] == "vps" and r["hostkey"].startswith("SHA256:"), "4. servers/add tests the connection and returns the pinned host-key fingerprint")
ok(any(s["name"] == "vps" for s in authed("me")[1]["servers"]), "4b. the new server shows in /api/me")
ok(authed("servers/add", {"target": "ftp://"})[0] == 400, "4c. a bad target is a clean 400")
ok(authed("term", {"cmd": "   "})[0] == 400, "4d. an empty console command is a clean 400")

st, r = authed("term", {"cmd": "echo hello && pwd"})
ok(st == 200 and "hello" in r["out"] and r["rc"] == 0, "5. term runs a command and returns merged output")
st, r = authed("term", {"cmd": "cd /tmp && pwd"})
ok(st == 200 and r["cwd"] == "/tmp", "5b. the working directory is tracked across commands")
authed("cd", {"path": ROOT})  # back to the fixture root for the file tests
st, r = authed("term", {"cmd": "true; kill -9 -l"})
ok(st == 200 and r.get("confirm"), "5c. a dangerous-looking command is held for confirmation")
st, r = authed("term", {"cmd": "true; kill -9 -l", "force": True})
ok(st == 200 and r["rc"] in (0, 1) and "HUP" in (r["out"] or ""), "5d. the confirmed dangerous command runs")
st, r = authed("stats", {})
ok(st == 200 and "host" in r and "mem_total" in r, "6. stats returns the parsed key/value set")
st, r = authed("view", {"key": "ps"})
ok(st == 200 and "PID" in (r["out"] or ""), "6b. views (ps) return the command output")
st, r = authed("mkdir", {"path": "webtest"})
ok(st == 200 and any(row["n"] == "webtest" and row["d"] for row in authed("ls")[1]["rows"]), "7. mkdir + ls list the new folder")
ok(authed("rm", {"path": "webtest"})[1].get("confirm") == "webtest", "7b. rm without force asks for confirmation")
ok("removed" in authed("rm", {"path": "webtest", "force": True})[1], "7c. rm with force deletes")

# ---------------------------------------------------------------- 4. chunked upload -> queue -> server, end to end
DATA = bytes(range(256)) * 4 + os.urandom(512)  # 1536 bytes, not a multiple of anything handy
st, r = authed("up/init", {"name": "sample.bin", "size": len(DATA)})
ok(st == 200 and r["token"] and r["chunk"] >= 1024 * 1024, "8. up/init opens a staging session")
token = r["token"]
def chunk(off, data):
    return req("POST", "/api/up/chunk", data, {"X-Tg-Init-Data": good, "X-Up-Token": token,
                                               "X-Up-Offset": str(off), "Content-Type": "application/octet-stream"})
st, r = chunk(0, DATA[:700])
ok(st == 200 and r["got"] == 700, "8b. first chunk lands")
ok(chunk(0, DATA[:700])[1]["got"] == 700, "8c. a retried chunk is idempotent (truncate then append)")
ok(chunk(1000, DATA[1000:])[0] == 400, "8d. chunks may not skip ahead")
st, r = chunk(700, DATA[700:])
ok(st == 200 and r["got"] == len(DATA), "8e. the rest completes the staging file")
ok(req("POST", "/api/up/chunk", b"x", {"X-Tg-Init-Data": good, "X-Up-Token": "nonsense", "X-Up-Offset": "0"})[0] == 404,
   "8f. an unknown upload token is 404")
staged = webapi.stage_path(token)
ok(open(staged, "rb").read() == DATA, "8g. staged bytes round-trip")
st, r = authed("up/finish", {"token": token})
ok(st == 200 and r["id"] == StubEngine.seen[-1] and r["dest"].endswith("sample.bin"), "8h. up/finish queues the job")
jid = r["id"]
job = store.get_job(jid)
ok(job["src"].startswith("web:") and job["src"][4:] == staged and job["state"] == "queued", "8i. the job source is the staged file")

async def drive(jid, timeout=30):
    eng = jobs.Engine(None, 0)
    eng.enqueue(jid)
    end = time.time() + timeout
    try:
        while time.time() < end:
            j = store.get_job(jid)
            if j["state"] in ("done", "failed", "cancelled"):
                return j
            await asyncio.sleep(0.05)
        raise AssertionError("job did not finish in time")
    finally:
        for t in eng.tasks:
            t.cancel()

j = asyncio.run(drive(jid))
ok(j["state"] == "done" and j["error"] == "", f"9. web job streams to the server via the queue (state={j['state']} {j['error']})")
ok(not os.path.exists(staged), "9b. the staged file is removed once the job is done")
c = store.get_server(UID, "vps")
from remote import Remote
with Remote(c) as rmt:
    got = rmt.size(j["dest"])
    with open(os.path.join(ROOT, os.path.basename(j["dest"])), "rb") as f:
        same = f.read() == DATA
ok(same and got == len(DATA), "9c. the uploaded file on the server matches byte for byte")

# lost staging (redeploy wiped the temp disk): the job fails with a clear reason, not a crash
lost = store.add_job(UID, "vps", UID, "web:/nonexistent/lost.part", "lost.bin", "lost.bin", 10)
j = asyncio.run(drive(lost))
ok(j["state"] == "failed" and "upload the file again" in j["error"], "10. a lost staged file fails with guidance, no retries")

old = os.path.join(webapi.STAGE_DIR, "ancient.part")
open(old, "wb").close()
os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
webapi.sweep_staged()
ok(not os.path.exists(old), "10b. day-old staging leftovers are swept away")

# audit trail picked up the web actions
ok(any("web:" in (a["action"] or "") for a in store.recent_log(UID, 30)), "11. every web action landed in the audit log")
ok(any(a["server"] == "vps" and "echo hello" in a["action"] for a in store.recent_log(UID, 30)), "11b. console commands are audited too")

# ---------------------------------------------------------------- 5. plans / orders / VPS hosting, end to end
st, r = authed("plans")
ok(st == 200 and any(p["key"] == "vps1" for p in r["plans"]) and r["current"]["key"] == "free"
   and r["orders"] == [] and r["slots_used"] == 0 and r["host_node"] is False,
   "13. plans: the catalog, the free current plan, no orders, node unconfigured")
ok(authed("plans/order", {"plan": "free"})[0] == 400, "13b. the free plan cannot be bought")
ok(authed("plans/order", {"plan": "nope"})[0] == 400, "13c. an unknown plan is a clean 400")
st, r = authed("plans/order", {"plan": "vps1"})
ok(st == 200 and r["order"]["status"] == "pending" and r["order"]["price"] == 5 and "/paid" in r["payment_info"],
   "13d. Buy opens a pending order with the payment instructions")
oid = r["order"]["id"]
ok(authed("plans/paid", {"ref": ""})[0] == 400, "13e. an empty reference is a clean 400")
st, r = authed("plans/paid", {"ref": "UPI-WEB-1"})
ok(st == 200 and r["order"]["status"] == "awaiting" and r["order"]["ref"] == "UPI-WEB-1", "13f. the reference lands on the order")
ok(authed("vps")[1]["vps"] == [], "13g. no VPS before approval")

# non-admins get 403 on the admin order routes
ok(authed("admin/orders")[0] == 403, "14. admin/orders is admins-only")
ok(authed("admin/order", {"id": oid, "action": "approve"})[0] == 403, "14b. admin/order is admins-only")

# become the admin (same id, now in webapi.ADMINS) and point the host node at the local SSH server
hosting.NODE_HOST, hosting.NODE_PORT, hosting.NODE_USER, hosting.NODE_PASS = "127.0.0.1", info["port"], "root", "pw"
hosting.PUBLIC_HOST = "127.0.0.1"
hosting.PROVISION_CMD = "echo provisioned {user} {pass} {cpu} {ram} {disk}"
hosting.DEPROVISION_CMD = "echo deprovisioned {user}"
webapi.configure(app=None, engine=webapi.ENGINE, token=TOKEN, allowed={UID}, webapp_url="", admins={UID})
st, r = authed("admin/orders")
ok(st == 200 and any(o["id"] == oid and o["status"] == "awaiting" for o in r["orders"]), "15. the awaiting order shows in admin/orders")
ok(authed("admin/order", {"id": oid, "action": "maybe"})[0] == 400, "15b. a bad action is a clean 400")
st, r = authed("admin/order", {"id": oid, "action": "approve"})
ok(st == 200 and r["order"]["status"] == "approved", "15c. admin/order approves the order")
st, r = authed("plans")
ok(r["current"]["key"] == "vps1" and r["days_left"] >= 29 and r["slots_used"] == 1, "16. the buyer is on vps1 with 1 VPS slot used")
st, r = authed("vps")
ok(st == 200 and len(r["vps"]) == 1 and r["vps"][0]["status"] == "active" and r["vps"][0]["host"] == "127.0.0.1"
   and r["vps"][0]["password"].isalnum() and len(r["vps"][0]["password"]) == 16,
   f"16b. the VPS was provisioned over real SSH and lists with credentials {r['vps']}")
ok(r["vps"][0]["cpu"] == 1 and r["vps"][0]["ram"] == 1024 and r["vps"][0]["disk"] == 15, "16c. the plan's specs are on the instance")
st, r = authed("admin/overview")
ok(st == 200 and r["subs"]["subs_active"] == 1 and r["subs"]["vps_active"] == 1 and "USD" in r["subs"]["revenue"], "16d. the admin overview counts subs, revenue and VPSes")

# expiry sweep removes the instance from the node and notifies the buyer
for a in store.alloc_list(UID):
    store.alloc_set(a["id"], expires=int(time.time()) - 5)
notes = hosting.sweep()
ok(len(notes) == 1 and notes[0][0] == UID and "removed" in notes[0][1] and store.alloc_list(UID)[0]["status"] == "expired",
   "17. the sweep deprovisions the expired instance and returns the buyer's notification")

dockerfile = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Dockerfile")).read()
ok("COPY webapp/" in dockerfile, "18. the Docker image ships the webapp/ assets (else /app would 404 in containers)")

# ---------------------------------------------------------------- 19. the static-hosting build (scripts/build_webapp.py)
import subprocess
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
out = os.path.join(REPO, "webapp-dist")
subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py"), "https://vps.example.com"],
               check=True, capture_output=True, timeout=60)
built_html = open(os.path.join(out, "index.html")).read()
ok('window.API_BASE="https://vps.example.com"' in built_html
   and built_html.index("window.API_BASE") < built_html.index("/static/app.js"),
   "19. the build injects API_BASE before app.js (a static host serves the app, the VPS answers the API)")
built_js = open(os.path.join(out, "static", "app.js")).read()
ok('API_BASE + "/api/"' in built_js and 'API_BASE + "/api/up/chunk"' in built_js
   and built_js.count('fetch("/api/') == 0,
   "19b. the app's fetches all go through API_BASE (same-origin by default, the bot's address when split)")
hdrs = open(os.path.join(out, "_headers")).read()
ok("connect-src 'self' https://vps.example.com" in hdrs and "frame-ancestors" in hdrs,
   "19c. the build's _headers carries a CSP that allows the API host (like the bot's own CSP, plus connect-src)")
ok(open(os.path.join(out, "_redirects")).read().strip().endswith("200")
   and os.path.exists(os.path.join(out, "README.md")) and os.path.exists(os.path.join(out, "static", "logo.png")),
   "19d. _redirects, README and the logo ship in the build")
r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py"), "http://plain.example"],
                   capture_output=True, text=True, timeout=60)
ok(r.returncode != 0 and "https" in r.stderr,
   "19e. a non-https API base is refused (Telegram only opens https web apps)")
subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py"), ""], check=True, capture_output=True, timeout=60)
ok('window.API_BASE' not in open(os.path.join(out, "index.html")).read()
   and 'API_BASE + "/api/"' in open(os.path.join(out, "static", "app.js")).read(),
   "19f. an empty base builds the same-origin variant (the bot serving the app itself needs no injection)")
src_html = open(os.path.join(REPO, "webapp", "index.html")).read()
ok('window.API_BASE' not in src_html,
   "19g. the webapp/ source stays untouched: only the build output carries the injected base")

# Netlify's build machines (netlify.toml runs this script): a build that would ship an app
# calling the static site instead of the bot must fail the deploy, with the fix in the message.
def netlify_env(extra=None):
    return dict(os.environ, NETLIFY="true", URL="https://bot-app.netlify.app", **(extra or {}))

r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py")],
                   capture_output=True, text=True, timeout=60, env=netlify_env())
ok(r.returncode != 0 and "BOT_API_URL" in r.stderr,
   "19h. on Netlify a missing BOT_API_URL fails the build (never ship an app that calls the static site)")
r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py")],
                   capture_output=True, text=True, timeout=60,
                   env=netlify_env({"BOT_API_URL": "https://bot-app.netlify.app"}))
ok(r.returncode != 0 and "BOT's" in r.stderr,
   "19i. on Netlify a BOT_API_URL equal to the site's own address fails the build (it must be the bot's)")
r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py")],
                   capture_output=True, text=True, timeout=60,
                   env=netlify_env({"BOT_API_URL": "https://vps.example.com"}))
ok(r.returncode == 0 and 'window.API_BASE="https://vps.example.com"' in open(os.path.join(out, "index.html")).read(),
   "19j. on Netlify the bot's address in BOT_API_URL builds cleanly (env var, not an argument)")
r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_webapp.py")],
                   capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, BOT_API_URL="https://env-bot.example.com"))
ok(r.returncode == 0 and 'window.API_BASE="https://env-bot.example.com"' in open(os.path.join(out, "index.html")).read(),
   "19k. locally, BOT_API_URL works as the default base (argument still wins when given)")
nt = open(os.path.join(REPO, "netlify.toml")).read()
ok("scripts/build_webapp.py" in nt and "webapp-dist" in nt,
   "19l. netlify.toml builds webapp-dist (a Git-connected Netlify site redeploys the app on every push)")

# ------------------------------------------- 20. the .env template covers every variable the code reads
import ast
import re as _re

# runtime + tooling files that read the environment; tests themselves are exempt
env_files = ["bot.py", "webapi.py", "payments.py", "hosting.py", "store.py", "subscriptions.py",
             "session_sync.py", "perf.py", "healthcheck.py", "jobs.py", "util.py",
             "deploy.py", "setup_gateway.py", "scripts/build_webapp.py"]
read_vars, envattr = set(), ("getenv", "environ")
for rel in env_files:
    tree = ast.parse(open(os.path.join(REPO, rel), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in envattr and node.args and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
            if isinstance(name, str):  # plain string literal: BOT_TOKEN, WEB_RATE, ...
                read_vars.add(name)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                and node.value.attr == "environ" and isinstance(node.value.value, ast.Name) \
                and node.value.value.id == "os" and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):  # os.environ["API_ID"]
                read_vars.add(node.slice.value)

# variables of other services / the platform, not of this bot - not template material
skip = {"PATH", "HOME", "USER", "PWD", "TMPDIR", "PYTHONUNBUFFERED", "NETLIFY", "URL",
        "DEPLOY_PRIME_URL", "RENDER", "RENDER_EXTERNAL_URL", "POSTGRES_PASSWORD",
        "CRYPTAPI_BTC_ADDR", "CRYPTAPI_TRC20_USDT_ADDR"}  # last two: examples of the <COIN> pattern

template = open(os.path.join(REPO, ".env.example"), encoding="utf-8").read()
tkeys = {m.group(1) for m in _re.finditer(r"^#?([A-Z][A-Z0-9_]*)=", template, _re.M)}
missing = sorted(v for v in read_vars - skip if v not in tkeys)
ok(not missing, f"20. every env var the code reads is in .env.example (missing: {missing or 'none'})")

# the two parsers that read/write .env must both swallow the template whole
sys.path.insert(0, REPO)
import deploy as d  # noqa: E402  (stdlib-only module; imported late like a test fixture)
import setup_gateway as sg  # noqa: E402
dv, sv = d.read_env(os.path.join(REPO, ".env.example")), sg.read_env(os.path.join(REPO, ".env.example"))
ok(all(dv.get(k, "") == "" for k in ("API_ID", "API_HASH", "BOT_TOKEN", "SECRET_KEY", "ALLOWED_USERS")),
   "20b. the template ships with every required var empty (deploy.py reads it as a fresh start)")
ok(all(k in tkeys for k in sv)
   and set(sv.values()) <= {"", "10", "90", "4096", "0.0.0.0", "300", "90", ".", "60", "INFO", "0", "1900", "5", "auto"},
   "20c. setup_gateway.py parses the template: every value empty or the code's own default, no secrets")
# required block sanity: the 5 keys exist uncommented, and every active value is a safe default
active = {m.group(1): m.group(2) for m in _re.finditer(r"^([A-Z][A-Z0-9_]*)=(.*)$", template, _re.M)}
ok({"API_ID", "API_HASH", "BOT_TOKEN", "SECRET_KEY", "ALLOWED_USERS"} <= set(active),
   "20d. the 5 required keys are present and uncommented (the ones the bot refuses to start without)")
ok(active.get("SESSION_DIR") == "." and active.get("DB_CONNECT_RETRIES") == "10"
   and active.get("LOG_LEVEL") == "INFO" and active.get("PERFORMANCE") == "auto",
   "20e. active tuning defaults match the code's own (SESSION_DIR, DB_CONNECT_RETRIES, LOG_LEVEL, PERFORMANCE)")
print("ALL WEBAPP CHECKS PASSED")
