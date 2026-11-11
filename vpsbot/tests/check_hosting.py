"""Hosting behaviour: SECRET_KEY handling, single-instance lock, dead-session recovery, keep-alive, and the whole
start-up/shutdown order of bot.main() with a fake Telegram client."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio, base64, json, socket, sqlite3, tempfile, threading, time, urllib.request, http.server
from cryptography.fernet import Fernet, InvalidToken
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/h.db", BOT_TOKEN="123:abc",
                  SESSION_DIR=f"{TMP}/sess", API_ID="1", API_HASH="x", ALLOWED_USERS="5", LOG_LEVEL="WARNING")
import store, session_sync
import bot  # first: Pyrogram grabs the event loop at import time
from pyrogram.errors import AuthKeyDuplicated, Unauthorized
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

# ---- 1. SECRET_KEY: Fernet key, Render's generateValue (plain base64), or any passphrase
k = Fernet.generate_key().decode()
ok(Fernet(k).decrypt(store.make_fernet(k).encrypt(b"y")) == b"y", "1. a real Fernet key is used unchanged")
render_style = base64.b64encode(os.urandom(32)).decode()
ok(store.make_fernet(render_style).decrypt(store.make_fernet(render_style).encrypt(b"x")) == b"x", "1b. Render generateValue-style key works and is stable")
phrase = "correct horse battery staple"
ok(store.make_fernet(phrase).decrypt(store.make_fernet(phrase).encrypt(b"x")) == b"x", "1c. any passphrase works and is stable")
try: store.make_fernet("another long random phrase!").decrypt(store.make_fernet(phrase).encrypt(b"x")); ok(False, "wrong key")
except InvalidToken: ok(True, "1d. a different key cannot decrypt")
for weak in ("", "short"):
    try: store.make_fernet(weak); ok(False, "weak key accepted")
    except SystemExit: ok(True, f"1e. weak SECRET_KEY {weak!r} refused")

# ---- 2. single-instance lock (zero-downtime deploys start the new copy before stopping the old one)
real_time = time
class FastTime:                       # 20x faster sleeps so the test is quick
    time = staticmethod(real_time.time)
    sleep = staticmethod(lambda s: real_time.sleep(s / 20))
session_sync.time = FastTime
me = session_sync.INSTANCE
session_sync.acquire_lock(); ok(session_sync._read_lock()[0] == me, "2. free lock is acquired")
store.kv_set("lock", f"other|{real_time.time()}")
done = threading.Event(); t = threading.Thread(target=lambda: (session_sync.acquire_lock(), done.set()), daemon=True); t.start()
real_time.sleep(1.0); ok(not done.is_set(), "2b. a fresh lock held by another copy blocks us (we wait, session untouched)")
store.kv_set("lock", f"|{real_time.time()}")        # the old copy released it on shutdown
ok(done.wait(5) and session_sync._read_lock()[0] == me, "2c. we take over as soon as it is released")
store.kv_set("lock", f"ghost|{real_time.time() - 1000}")
session_sync.acquire_lock(); ok(session_sync._read_lock()[0] == me, "2d. an abandoned (stale) lock is taken over")
session_sync.release_lock(); ok(session_sync._read_lock()[0] == "", "2e. release frees it")
store.kv_set("lock", f"other|{real_time.time()}"); session_sync.release_lock()
ok(session_sync._read_lock()[0] == "other", "2f. we never release a lock someone else holds")
session_sync.HEARTBEAT = 0.05
lost = []; asyncio.run(session_sync.heartbeat_loop(lambda: lost.append(1))); ok(lost == [1], "2g. losing the lock is detected by the heartbeat")
store.kv_set("lock", f"{me}|1.0")
async def beat():
    try: await asyncio.wait_for(session_sync.heartbeat_loop(lambda: lost.append(2)), 0.4)
    except asyncio.TimeoutError: pass
asyncio.run(beat()); ok(session_sync._read_lock()[1] > 1000 and lost == [1], "2h. heartbeat refreshes our lock")

# ---- 3. dead-session recovery
class FakeApp:
    def __init__(self, fail_first=None, boom=None): self.calls, self.fail_first, self.boom, self.lock_owner = [], fail_first, boom, None
    async def start(self):
        self.calls.append("start"); self.lock_owner = session_sync._read_lock()[0]
        if self.boom: raise self.boom
        if self.fail_first and self.calls.count("start") == 1: raise self.fail_first()
        os.makedirs(session_sync.DIR, exist_ok=True)
        c = sqlite3.connect(session_sync.PATH); c.execute("CREATE TABLE IF NOT EXISTS peers(id INTEGER)"); c.execute("INSERT INTO peers VALUES(1)"); c.commit(); c.close()
    async def stop(self): self.calls.append("stop")
    async def set_bot_commands(self, cmds): self.calls.append("commands")
os.makedirs(session_sync.DIR, exist_ok=True); open(session_sync.PATH, "wb").write(b"dead session"); store.kv_set("session_token", "tok")
for exc in (AuthKeyDuplicated, Unauthorized):
    open(session_sync.PATH, "wb").write(b"dead session"); store.kv_set("session_token", "tok")
    bot.app = FakeApp(fail_first=exc); asyncio.run(bot.start_client())
    ok(bot.app.calls == ["start", "stop", "start"] and open(session_sync.PATH, "rb").read()[:6] == b"SQLite", f"3. {exc.__name__}: bad session dropped, fresh login retried")
    ok(store.kv_get("session_token") == "" and bot.SESSION_SOURCE.startswith("fresh login"), "3b. stored snapshot invalidated so it is not restored again")
bot.app = FakeApp(); asyncio.run(bot.start_client()); ok(bot.app.calls == ["start"], "3c. healthy start does not retry")
bot.app = FakeApp(boom=ConnectionError("network down"))
try: asyncio.run(bot.start_client()); ok(False, "swallowed")
except ConnectionError: ok(bot.app.calls == ["start"], "3d. unrelated errors are not swallowed (and the session is kept)")

# ---- 4. keep-alive pings
hits = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): hits.append(self.path); self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
async def ka(url, secs):
    task = asyncio.create_task(bot.keep_alive(url, 0.1)); await asyncio.sleep(secs); alive = not task.done(); task.cancel(); return alive
ok(asyncio.run(ka(f"http://127.0.0.1:{srv.server_port}/ping", 0.65)) and len(hits) >= 3 and set(hits) == {"/ping"}, f"4. keep-alive pings its URL repeatedly ({len(hits)} hits)")
s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
ok(asyncio.run(ka(f"http://127.0.0.1:{dead}/", 0.4)), "4b. a failing ping never kills the loop")

# ---- 5. the whole bot.main(): health first, lock, restore, start, run, stop, snapshot, release
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
os.environ.update(PORT=str(port), KEEP_ALIVE="1", KEEP_ALIVE_URL=f"http://127.0.0.1:{port}/keepalive", KEEP_ALIVE_INTERVAL="1")
store.kv_set("lock", ""); store.kv_set("session", ""); store.kv_set("session_token", "")
if os.path.exists(session_sync.PATH): os.remove(session_sync.PATH)
app = bot.app = FakeApp(); seen, pings = {}, []
real_urlopen = urllib.request.urlopen
def counting(url, *a, **k):
    if isinstance(url, str) and url.endswith("/keepalive"): pings.append(url)
    return real_urlopen(url, *a, **k)
urllib.request.urlopen = counting
async def fake_idle():
    seen["health"] = json.loads(await asyncio.to_thread(lambda: real_urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read()))
    seen["lock"] = session_sync._read_lock()[0]; await asyncio.sleep(2.4)
bot.idle = fake_idle
asyncio.run(bot.main()); urllib.request.urlopen = real_urlopen
ok(seen["health"] == {"status": "ok"} and seen["lock"] == session_sync.INSTANCE, "5. health endpoint up and lock held while running")
ok(app.calls == ["start", "commands", "stop"] and app.lock_owner == session_sync.INSTANCE, f"5b. lock taken BEFORE Telegram start; order {app.calls}")
ok(bot.SESSION_SOURCE == "fresh login", "5c. session source reported")
ok(len(pings) >= 2, f"5d. keep-alive pinged the public URL while running ({len(pings)} pings)")
ok(session_sync._read_lock()[0] == "" , "5e. lock released on shutdown so the next copy can start at once")
blob = store.load_blob("session"); open(f"{TMP}/restored.session", "wb").write(blob)
ok(sqlite3.connect(f"{TMP}/restored.session").execute("SELECT COUNT(*) FROM peers").fetchone() == (1,), "5f. final session snapshot saved to the database")
try: socket.create_connection(("127.0.0.1", port), timeout=1); ok(False, "health still open")
except OSError: ok(True, "5g. health server closed on shutdown")
print("ALL HOSTING PASSED")
