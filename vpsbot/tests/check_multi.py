"""Files over 2 GB: volumes joined on the fly into ONE file on FTP and SFTP (MultiTgReader, /multi, /done)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio, itertools, random, tempfile, threading, time, types
from cryptography.fernet import Fernet
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/m.db", API_ID="1", API_HASH="x", BOT_TOKEN="1:x",
                  ALLOWED_USERS="5", SESSION_DIR=f"{TMP}/sess", LOG_LEVEL="CRITICAL", PERFORMANCE="turbo", TG_WINDOW_MB="1")
import logging; logging.disable(logging.CRITICAL)
import asyncssh, store, sources, jobs, largefiles
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import ThreadedFTPServer
import bot
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
MiB = 1024 * 1024

class Media: value = "document"
class Msg:
    """A Telegram message holding a file."""
    ids = itertools.count(1000)
    def __init__(self, data, name="x", declared=None, empty=False):
        self.id, self.data, self.empty, self.media = next(Msg.ids), data, empty, Media()
        self.document = types.SimpleNamespace(file_name=name, file_size=len(data) if declared is None else declared, mime_type="application/octet-stream")
class Client:
    def __init__(self, msgs=(), fail_msg=None, fail_chunk=2):
        self.reg = {m.id: m for m in msgs}; self.calls = []; self.edits = []; self.sent = []
        self.fail_msg, self.fail_chunk, self.failed = fail_msg, fail_chunk, False
    def add(self, m): self.reg[m.id] = m; return m
    async def get_messages(self, chat_id, ids):
        got = [self.reg.get(i) or types.SimpleNamespace(empty=True, media=None) for i in (ids if isinstance(ids, list) else [ids])]
        return got if isinstance(ids, list) else got[0]
    async def edit_message_text(self, chat_id, mid, text): self.edits.append(text)
    async def send_message(self, chat_id, text): self.sent.append(text)
    async def stream_media(self, msg, limit=0, offset=0):
        self.calls.append((msg.id, offset, limit)); C = sources.CHUNK
        end = len(msg.data) if not limit else min(len(msg.data), (offset + limit) * C)
        for idx, i in enumerate(range(offset * C, end, C)):
            if msg.id == self.fail_msg and not self.failed and offset + idx >= self.fail_chunk:
                self.failed = True; self.fail_index = len(self.calls); raise ConnectionResetError("simulated drop")
            await asyncio.sleep(0)
            yield msg.data[i:min(i + C, end)]

# ---------------------------------------------------------------- 1. MultiTgReader (tiny chunks keep the exhaustive check fast)
loop = asyncio.new_event_loop(); threading.Thread(target=loop.run_forever, daemon=True).start()
sources.CHUNK = 4096; C = sources.CHUNK
rnd = random.Random(11)
parts = [rnd.randbytes(n) for n in (3 * C + 7, C, 5 * C - 1, 2, 4 * C)]
whole = b"".join(parts); total = len(whole)
starts = list(itertools.accumulate([0] + [len(p) for p in parts[:-1]]))
offsets = sorted({0, 1, total - 1, total} | {s + d for s in starts for d in (-1, 0, 1, C // 2) if 0 <= s + d <= total})
bad = 0; cases = 0
for off in offsets:
    for step in (777, 65536, -1):
        msgs = [Msg(p) for p in parts]; cl = Client(msgs); seen = []
        r = sources.MultiTgReader(cl, msgs, loop, off, lambda pos, tot: seen.append((pos, tot)), lambda: False)
        out = bytearray()
        while (d := r.read(step)): out += d
        r.close(); cases += 1
        if bytes(out) != whole[off:] or (seen and seen[-1] != (total, total)) or any(b[0] < a[0] for a, b in zip(seen, seen[1:])):
            bad += 1; print("  mismatch at offset", off, "step", step)
ok(bad == 0, f"1. MultiTgReader == concatenation[offset:] for {cases} offset/read-size combinations (every boundary +-1), progress monotonic and ends at the total")
msgs = [Msg(p) for p in parts]; r = sources.MultiTgReader(Client(msgs), msgs, loop, 0, lambda *a: None, lambda: True)
try: r.read(10); ok(False, "cancel ignored")
except sources.Cancelled: ok(True, "1b. cancelling stops a multi-volume read")
short = [Msg(parts[0]), Msg(parts[1][:-5], declared=len(parts[1]))]; r = sources.MultiTgReader(Client(short), short, loop, 0, lambda *a: None, lambda: False)
try:
    while r.read(65536): pass
    ok(False, "short volume accepted")
except IOError: ok(True, "1c. a volume that ends early is an error, never a silently shorter file")
try: sources.MultiTgReader(Client(), [Msg(b"abc", declared=0)], loop, 0, lambda *a: None, lambda: False); ok(False, "unknown size")
except ValueError: ok(True, "1d. a volume of unknown size is refused")
sources.CHUNK = MiB

# ---------------------------------------------------------------- 2. ordering and validation of volumes
it = lambda *names: [(i + 10, n, 5) for i, n in enumerate(names)]
o, base, prob = largefiles.order_volumes(it("a.iso.003", "a.iso.001", "a.iso.002"))
ok([x[1] for x in o] == ["a.iso.001", "a.iso.002", "a.iso.003"] and base == "a.iso" and prob is None, "2. numbered volumes are sorted and the base name is found")
ok("002" in largefiles.order_volumes(it("a.001", "a.003"))[2] and "missing" in largefiles.order_volumes(it("a.001", "a.003"))[2], "2b. a missing volume is reported")
ok("003" in largefiles.order_volumes(it("a.002", "a.003"))[2] or "001" in largefiles.order_volumes(it("a.002", "a.003"))[2], "2c. not starting at .001 is reported")
ok("twice" in largefiles.order_volumes(it("a.001", "a.001"))[2], "2d. a duplicate volume is reported")
ok("different files" in largefiles.order_volumes(it("a.001", "b.002"))[2], "2e. volumes of different files are reported")
o, base, prob = largefiles.order_volumes([(30, "z.bin", 1), (10, "y.bin", 1), (20, "x", 1)])
ok([x[0] for x in o] == [10, 20, 30] and base is None and prob is None, "2f. unnumbered files keep the order they were sent in")

# ---------------------------------------------------------------- servers
FROOT, SROOT = tempfile.mkdtemp(), tempfile.mkdtemp()
a = DummyAuthorizer(); a.add_user("u", "p", FROOT, perm="elradfmwMT")
ftp = ThreadedFTPServer(("127.0.0.1", 0), type("H", (FTPHandler,), {"authorizer": a})); FPORT = ftp.address[1]
threading.Thread(target=ftp.serve_forever, daemon=True).start()
class Srv(asyncssh.SSHServer):
    def password_auth_supported(self): return True
    def validate_password(self, u, p): return u == "root" and p == "pw"
ready, info = threading.Event(), {}
def serve():
    lp = asyncio.new_event_loop(); asyncio.set_event_loop(lp); key = asyncssh.generate_private_key("ssh-ed25519")
    async def go():
        s = await asyncssh.listen("127.0.0.1", 0, server_factory=Srv, server_host_keys=[key], sftp_factory=True)
        info["port"] = s.sockets[0].getsockname()[1]; ready.set()
    lp.run_until_complete(go()); lp.run_forever()
threading.Thread(target=serve, daemon=True).start(); ready.wait(10)
UID = 4242
def add(name, **kw):
    c = dict(uid=UID, name=name, scheme="ftp", host="127.0.0.1", port=FPORT, user="u", auth="password", secret="p", cwd="", hostkey="")
    c.update(kw); store.add_server(c)
add("ftp1"); add("sftp1", scheme="ssh", port=info["port"], user="root", secret="pw", cwd=SROOT)

async def wait(jid, t=90):
    end = time.time() + t
    while time.time() < end:
        j = store.get_job(jid)
        if j["state"] in ("done", "failed", "cancelled"): return j
        await asyncio.sleep(0.1)
    raise TimeoutError(store.get_job(jid))
def job_for(server, msgs, name, chat=UID):
    ids = ",".join(str(m.id) for m in msgs)
    jid = store.add_job(UID, server, chat, f"tgm:{chat}:{ids}", name, name, sum(len(m.data) for m in msgs)); store.set_job(jid, status_id=9); return jid

DATA = [random.Random(i).randbytes(n) for i, n in enumerate((3 * MiB + 500, 2 * MiB + 17, 4 * MiB + 3))]
WHOLE = b"".join(DATA)
async def engine_tests():
    for server, root, label in (("ftp1", FROOT, "FTP"), ("sftp1", SROOT, "SFTP (32 MiB window)")):
        msgs = [Msg(d) for d in DATA]; cl = Client(msgs); eng = jobs.Engine(cl, 0)
        j = job_for(server, msgs, f"{label[:3]}-big.iso"); eng.enqueue(j); r = await wait(j)
        ok(r["state"] == "done" and open(f"{root}/{label[:3]}-big.iso", "rb").read() == WHOLE and r["size"] == len(WHOLE), f"3. {label}: 3 volumes arrive as ONE byte-identical file ({len(WHOLE)} bytes)")
    msgs = [Msg(d) for d in DATA]; cl = Client(msgs, fail_msg=msgs[1].id, fail_chunk=1); eng = jobs.Engine(cl, 0)
    j = job_for("ftp1", msgs, "resume.iso"); eng.enqueue(j); r = await wait(j)
    after = [c[0] for c in cl.calls[cl.fail_index:]]
    ok(r["state"] == "done" and r["attempts"] == 2 and open(f"{FROOT}/resume.iso", "rb").read() == WHOLE, "3b. a drop in the middle of volume 2 is retried and the file is still byte-identical")
    ok(msgs[0].id not in after, "3c. the retry resumed inside volume 2 and did not download volume 1 again")
    msgs = [Msg(d) for d in DATA]; cl = Client(msgs); del cl.reg[msgs[1].id]; eng = jobs.Engine(cl, 0)
    j = job_for("ftp1", msgs, "gone.iso"); eng.enqueue(j); r = await wait(j)
    ok(r["state"] == "failed" and r["attempts"] == 1 and "deleted" in r["error"] and not os.path.exists(f"{FROOT}/gone.iso"), "3d. a deleted volume fails fast with a clear message and leaves no half file")

    # ------------------------------------------------------------ 4. the real chat flow through the bot's handlers
    class Status:
        _ids = itertools.count(5000)
        def __init__(self): self.id = next(Status._ids); self.texts = []
        async def edit_text(self, t): self.texts.append(t)
    class Chat:
        _ids = itertools.count(100)
        def __init__(self, text=None, data=None, name=None, caption=None):
            self.id = next(Chat._ids); self.from_user = types.SimpleNamespace(id=UID, username="tester"); self.chat = types.SimpleNamespace(id=UID)
            self.text, self.caption, self.replies, self.data = text, caption, [], data
            for k in ("document", "video", "audio", "animation", "voice", "video_note", "photo"): setattr(self, k, None)
            self.media = None
            if data is not None:
                self.media = Media(); self.document = types.SimpleNamespace(file_name=name, file_size=len(data), mime_type="application/octet-stream")
        async def reply(self, text, **kw): self.replies.append(text); return Status()
    cl = Client(); bot.engine = jobs.Engine(cl, 0)
    def file_msg(name, data):
        m = Chat(data=data, name=name); fake = Msg(data, name); fake.id = m.id; cl.reg[m.id] = fake; return m
    store.set_active(UID, "ftp1")
    pieces = [random.Random(50 + i).randbytes(n) for i, n in enumerate((2 * MiB + 9, MiB + 3, 3 * MiB))]
    await bot.multi_cmd(None, Chat("/multi"))
    for idx in (2, 0, 1):                                       # volumes arrive out of order
        m = file_msg(f"movie.mkv.{idx + 1:03d}", pieces[idx]); await bot.upload(None, m)
    ok(len(bot.COLLECT[UID]["items"]) == 3 and not store.list_jobs(UID, ("queued", "running")), "4. while /multi is active, files are collected, not uploaded one by one")
    done = Chat("/done"); await bot.done_cmd(None, done); await asyncio.sleep(0.2)
    jid = store.list_jobs(UID, limit=1)[0]["id"]; r = await wait(jid)
    ok(r["state"] == "done" and r["name"] == "movie.mkv" and open(f"{FROOT}/movie.mkv", "rb").read() == b"".join(pieces) and UID not in bot.COLLECT, "4b. /done joined the volumes in numeric order into movie.mkv on the FTP server")

    await bot.multi_cmd(None, Chat("/multi"))
    for n in ("p.001", "p.003"): await bot.upload(None, file_msg(n, b"data"))
    d = Chat("/done"); await bot.done_cmd(None, d)
    ok("missing" in d.replies[0] and UID in bot.COLLECT, "4c. a missing volume is reported and the collection stays open")
    await bot.upload(None, file_msg("p.002", b"more")); d = Chat("/done"); await bot.done_cmd(None, d); jid = store.list_jobs(UID, limit=1)[0]["id"]; await wait(jid)
    ok(open(f"{FROOT}/p", "rb").read() == b"datamoredata", "4d. after sending the missing volume /done works (volumes 1-2-3 in order)")

    await bot.multi_cmd(None, Chat("/multi joined.bin"))
    a_, b_ = file_msg("first.part", b"AAAA"), file_msg("second.part", b"BBBB"); await bot.upload(None, b_); await bot.upload(None, a_)
    await bot.done_cmd(None, Chat("/done")); jid = store.list_jobs(UID, limit=1)[0]["id"]; await wait(jid)
    ok(open(f"{FROOT}/joined.bin", "rb").read() == b"AAAABBBB", "4e. unnumbered files are joined in the order they were SENT (message order, even if handled out of order), under the name given to /multi")
    await bot.multi_cmd(None, Chat("/multi")); await bot.upload(None, file_msg("x.bin", b"1")); d = Chat("/done"); await bot.done_cmd(None, d)
    ok("name" in d.replies[0].lower() and UID in bot.COLLECT, "4f. unnumbered files without a name ask for one")
    await bot.cancel(None, Chat("/cancel")); ok(UID not in bot.COLLECT, "4g. /cancel ends the collection")
    before = len(store.list_jobs(UID, limit=100)); await bot.upload(None, file_msg("single.bin", b"solo")); jid = store.list_jobs(UID, limit=1)[0]["id"]; await wait(jid)
    ok(len(store.list_jobs(UID, limit=100)) == before + 1 and open(f"{FROOT}/single.bin", "rb").read() == b"solo", "4h. outside /multi a file is uploaded normally")
    p = Chat("/perf"); await bot.perf_cmd(None, p); ok("profile: turbo" in p.replies[0], "4i. /perf reports the profile in use")
asyncio.run(engine_tests())
print("ALL MULTI PASSED")
