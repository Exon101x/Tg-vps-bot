import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import asyncio, hashlib, os, random, sys, tempfile, threading, time, types
from cryptography.fernet import Fernet
os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["TG_WINDOW_MB"] = "1"  # one chunk per request window, so a failure can land exactly between chunks
if not os.environ.get("DATABASE_URL"):
    os.environ["BOT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import logging; logging.disable(logging.CRITICAL)
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import ThreadedFTPServer
import store, jobs, sources
jobs.BACKOFF = (1, 1, 1)
ROOT = tempfile.mkdtemp()
a = DummyAuthorizer(); a.add_user("u", "p", ROOT, perm="elradfmwMT")
srv = ThreadedFTPServer(("127.0.0.1", 0), type("H", (FTPHandler,), {"authorizer": a})); PORT = srv.address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
UID = 7_000_000_000 + __import__("random").randrange(10**9)
def add_server(pw="p", dup="overwrite"):
    store.add_server(dict(uid=UID, name="ftp1", scheme="ftp", host="127.0.0.1", port=PORT, user="u", auth="password", secret=pw, cwd="", hostkey=""))
    store.update(UID, "ftp1", dup=dup)
DATA = random.Random(1).randbytes(5 * 1024 * 1024 + 123457)
class Media: value = "document"
class FakeMsg:
    empty = False; media = Media()
    def __init__(self, n): self.document = types.SimpleNamespace(file_size=n)
class FakeApp:
    """Stands in for the Pyrogram client: stream_media(msg, limit, offset) in 1 MiB chunks, like the real one."""
    def __init__(self, data, fail_at=None, delay=0.0, gone=False):
        self.data, self.fail_at, self.delay, self.gone = data, fail_at, delay, gone
        self.calls = []; self.fail_index = None; self.edits = []; self.sent = []
    async def get_messages(self, chat_id, msg_id):
        m = FakeMsg(len(self.data)); m.empty = self.gone; return m
    async def edit_message_text(self, chat_id, mid, text): self.edits.append(text)
    async def send_message(self, chat_id, text): self.sent.append(text)
    async def stream_media(self, msg, limit=0, offset=0):
        self.calls.append((offset, limit))
        end = len(self.data) if not limit else min(len(self.data), (offset + limit) * sources.CHUNK)
        for idx, i in enumerate(range(offset * sources.CHUNK, end, sources.CHUNK)):
            if self.fail_at is not None and self.fail_index is None and offset + idx >= self.fail_at:
                self.fail_index = len(self.calls); raise ConnectionResetError("simulated drop")   # only the first time
            if self.delay: await asyncio.sleep(self.delay)
            yield self.data[i:min(i + sources.CHUNK, end)]
def new_job(name="big.bin", dest=None, size=None):
    jid = store.add_job(UID, "ftp1", UID, f"tg:{UID}:1", name, dest or name, len(DATA) if size is None else size)
    store.set_job(jid, status_id=99); return jid
async def wait_state(jid, states=("done", "failed", "cancelled"), t=60):
    end = time.time() + t
    while time.time() < end:
        j = store.get_job(jid)
        if j["state"] in states: return j
        await asyncio.sleep(0.1)
    raise TimeoutError(store.get_job(jid))
sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
H = hashlib.sha256(DATA).hexdigest()
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
async def main():
    add_server(); small = b"hello world" * 1000
    app = FakeApp(DATA); eng = jobs.Engine(app, 10**9)
    j = new_job(); eng.enqueue(j); r = await wait_state(j)
    ok(r["state"] == "done" and sha(f"{ROOT}/big.bin") == H, f"[{store.LABEL}] streamed upload byte-identical")
    os.remove(f"{ROOT}/big.bin")
    app = FakeApp(DATA, fail_at=3); eng = jobs.Engine(app, 10**9)
    j = new_job(); eng.enqueue(j); r = await wait_state(j)
    ok(r["state"] == "done" and r["attempts"] == 2 and sha(f"{ROOT}/big.bin") == H, "retried once, file identical")
    after = [o for o, l in app.calls[app.fail_index:] if o is not None]
    ok(app.fail_index and min(after) >= 2, f"second attempt resumed at chunk {min(after)} instead of 0")
    app = FakeApp(small); eng = jobs.Engine(app, 10**9)
    j = new_job("a.txt", "alice/2026/a.txt", size=len(small)); eng.enqueue(j); await wait_state(j)
    store.update(UID, "ftp1", dup="rename")
    j = new_job("a.txt", "alice/2026/a.txt", size=len(small)); eng.enqueue(j); await wait_state(j)
    ok(os.path.exists(f"{ROOT}/alice/2026/a (1).txt") and store.get_job(j)["dest"].endswith("a (1).txt"), "folders + rename policy")
    store.update(UID, "ftp1", dup="skip")
    j = new_job("a.txt", "alice/2026/a.txt", size=len(small)); eng.enqueue(j); r = await wait_state(j)
    ok(r["error"].startswith("skipped"), "skip policy")
    add_server(pw="wrong")
    j = new_job("x.bin", size=len(small)); eng.enqueue(j); r = await wait_state(j)
    ok(r["state"] == "failed" and r["attempts"] == 1 and any("failed" in s for s in app.sent), "bad password fails fast + notifies")
    add_server(); ok(await eng.retry(j) and (await wait_state(j))["state"] == "done", "/retry works")
    app = FakeApp(DATA, delay=0.2); eng = jobs.Engine(app, 10**9)
    j = new_job("speed.bin"); eng.enqueue(j); await wait_state(j)
    ok(any("/s" in e for e in app.edits), "progress shows speed: " + next(e for e in app.edits if "/s" in e).replace("\n", " | "))
    app = FakeApp(DATA, delay=2.0); eng = jobs.Engine(app, 10**9)
    j = new_job("slow.bin"); eng.enqueue(j); await wait_state(j, ("running",)); await asyncio.sleep(1.0); await eng.stop(j)
    ok((await wait_state(j))["state"] == "cancelled", "stop running job")
    store.set_job(j, state="running"); app = FakeApp(DATA); eng = jobs.Engine(app, 10**9); eng.start()
    r = await wait_state(j); ok(r["state"] == "done" and sha(f"{ROOT}/slow.bin") == H, "restart recovery resumes the interrupted job")
    big = random.Random(9).randbytes(48 * 1024 * 1024 + 777)     # 48 MiB, parallel windows reassembled in order
    app = FakeApp(big); eng = jobs.Engine(app, 10**9)
    j = store.add_job(UID, "ftp1", UID, f"tg:{UID}:1", "huge.bin", "huge.bin", len(big)); store.set_job(j, status_id=99); eng.enqueue(j)
    r = await wait_state(j, t=120)
    ok(r["state"] == "done" and sha(f"{ROOT}/huge.bin") == hashlib.sha256(big).hexdigest(), f"48 MiB streamed through parallel windows is byte-identical ({len(app.calls)} window requests)")
asyncio.run(main()); print("ALL E2E PASSED")
