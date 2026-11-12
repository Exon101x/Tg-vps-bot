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
ok(req("POST", "/api/me", "{}", {"Content-Type": "application/json"})[0] == 401, "3j. no signature -> 401")
ok(authed("me", extra=None)[0] == 200, "3k. signed request from an allowed user -> 200")
other = make_init(TOKEN, 8)  # uid 8 is NOT in ALLOWED_USERS {7}
ok(req("POST", "/api/me", "{}", {"X-Tg-Init-Data": other})[0] == 403, "3l. signed but not on ALLOWED_USERS -> 403")
ok(authed("nope")[0] == 404, "3m. unknown route -> 404")
ok(req("GET", "/api/me", headers={"X-Tg-Init-Data": good})[0] == 405, "3n. GET on an api route -> 405")

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
dockerfile = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Dockerfile")).read()
ok("COPY webapp/" in dockerfile, "12. the Docker image ships the webapp/ assets (else /app would 404 in containers)")
print("ALL WEBAPP CHECKS PASSED")
