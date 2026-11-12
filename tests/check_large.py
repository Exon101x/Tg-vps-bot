"""Large files: the parallel Telegram reader, ranged server reads (FTP + SFTP), /get volumes and the /join script."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio, collections, io, random, tempfile, threading, types
from cryptography.fernet import Fernet
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/l.db", API_ID="1", API_HASH="x",
                  BOT_TOKEN="1:x", ALLOWED_USERS="5", SESSION_DIR=f"{TMP}/sess", LOG_LEVEL="CRITICAL")
import logging; logging.disable(logging.CRITICAL)
import asyncssh, store, sources, largefiles
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import ThreadedFTPServer
import bot
from remote import Remote
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
MiB = 1024 * 1024

# ---------------------------------------------------------------- 1. parallel Telegram reader (tiny chunks keep it fast)
loop = asyncio.new_event_loop(); threading.Thread(target=loop.run_forever, daemon=True).start()
sources.CHUNK = 4096
C = sources.CHUNK
class Media: value = "document"
def fake_msg(n): return types.SimpleNamespace(media=Media(), document=types.SimpleNamespace(file_size=n))
class FakeClient:
    def __init__(self, data, jitter=0.02, short_at=None, boom_at=None):
        self.data, self.jitter, self.short_at, self.boom_at = data, jitter, short_at, boom_at
        self.active = self.max_active = self.cancelled = 0
    async def stream_media(self, msg, limit=0, offset=0):
        self.active += 1; self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(random.random() * self.jitter)
            if offset == self.boom_at: raise ConnectionResetError("boom")
            end = len(self.data) if not limit else min(len(self.data), (offset + limit) * C)
            for i in range(offset * C, end, C):
                piece = self.data[i:min(i + C, end)]
                yield piece[:-1] if offset == self.short_at and i == offset * C else piece
        except asyncio.CancelledError:
            self.cancelled += 1; raise
        finally: self.active -= 1
def slurp(r, step):
    out = bytearray()
    while True:
        d = r.read(step)
        if not d: return bytes(out)
        out += d
rnd = random.Random(5); cases = bad = 0
for size in (1, C - 1, C, C + 1, 5 * C + 17, 37 * C + 5):
    data = rnd.randbytes(size)
    for offset in sorted({0, 1, C - 1, C, 3 * C + 123, size - 1, size} ):
        if offset > size: continue
        for workers, window in ((1, 1), (4, 2), (4, 8)):
            cl = FakeClient(data); r = sources.TgReader(cl, fake_msg(size), loop, offset, lambda *a: None, lambda: False, workers, window)
            got = slurp(r, 1000 if cases % 2 else 65536); r.close(); cases += 1
            if got != data[offset:] or cl.max_active > workers: bad += 1; print("  mismatch", size, offset, workers, window, len(got))
ok(bad == 0, f"1. TgReader returns exactly data[offset:] in {cases} size/offset/worker combinations, never exceeding the worker limit")
data = rnd.randbytes(60 * C); cl = FakeClient(data)
r = sources.TgReader(cl, fake_msg(len(data)), loop, 0, lambda *a: None, lambda: False, 4, 1); d = r.read(-1); r.close()
ok(d == data and cl.max_active >= 2, f"1b. downloads really are parallel (up to {cl.max_active} windows in flight)")
cl = FakeClient(data, short_at=5); r = sources.TgReader(cl, fake_msg(len(data)), loop, 0, lambda *a: None, lambda: False, 4, 1)
try: slurp(r, 65536); ok(False, "short window accepted")
except IOError as e: ok("Telegram returned" in str(e), "1c. a short window is detected instead of silently corrupting the file")
cl = FakeClient(data, boom_at=3); r = sources.TgReader(cl, fake_msg(len(data)), loop, 0, lambda *a: None, lambda: False, 4, 1)
try: slurp(r, 65536); ok(False, "error swallowed")
except ConnectionResetError: ok(True, "1d. a failing window raises to the caller (the job engine then retries and resumes)")
cl = FakeClient(data, jitter=3.0); r = sources.TgReader(cl, fake_msg(len(data)), loop, 0, lambda *a: None, lambda: False, 4, 1)
r._fill(); import time; time.sleep(0.3); r.close(); time.sleep(0.3)
ok(cl.cancelled >= 1 and not r._pending, f"1e. close() cancels in-flight windows ({cl.cancelled} cancelled)")
cl = FakeClient(data); r = sources.TgReader(cl, fake_msg(0), loop, 0, lambda *a: None, lambda: False)   # size unknown -> sequential
ok(r._agen is not None, "1f. unknown size falls back to a sequential stream")
sources.CHUNK = MiB

# ---------------------------------------------------------------- servers: FTP + SSH/SFTP
FROOT, SROOT = tempfile.mkdtemp(), tempfile.mkdtemp()
a = DummyAuthorizer(); a.add_user("u", "p", FROOT, perm="elradfmwMT")
ftp = ThreadedFTPServer(("127.0.0.1", 0), type("H", (FTPHandler,), {"authorizer": a})); FPORT = ftp.address[1]
threading.Thread(target=ftp.serve_forever, daemon=True).start()
class Srv(asyncssh.SSHServer):
    def password_auth_supported(self): return True
    def validate_password(self, u, p): return u == "root" and p == "pw"
async def handle(process):
    proc = await asyncio.create_subprocess_exec("bash", "-c", process.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=SROOT)
    out, _ = await proc.communicate(); process.stdout.write(out.decode(errors="replace")); process.exit(proc.returncode)
ready, info = threading.Event(), {}
def serve():
    lp = asyncio.new_event_loop(); asyncio.set_event_loop(lp); key = asyncssh.generate_private_key("ssh-ed25519")
    async def go():
        s = await asyncssh.listen("127.0.0.1", 0, server_factory=Srv, server_host_keys=[key], process_factory=handle, sftp_factory=True)
        info["port"] = s.sockets[0].getsockname()[1]; ready.set()
    lp.run_until_complete(go()); lp.run_forever()
threading.Thread(target=serve, daemon=True).start(); ready.wait(10)
UID = 777
def add(name, **kw):
    c = dict(uid=UID, name=name, scheme="ftp", host="127.0.0.1", port=FPORT, user="u", auth="password", secret="p", cwd="", hostkey="")
    c.update(kw); store.add_server(c); return store.get_server(UID, name)
cf = add("ftp1")
cs = add("vps", scheme="ssh", port=info["port"], user="root", secret="pw", cwd=SROOT)

# ---------------------------------------------------------------- 2. ranged reads on a real FTP server and a real SFTP server
DATA = random.Random(8).randbytes(3 * MiB + 123)
os.makedirs(f"{FROOT}/d"); os.makedirs(f"{SROOT}/d")
open(f"{FROOT}/d/big.bin", "wb").write(DATA); open(f"{SROOT}/d/big.bin", "wb").write(DATA)
n = len(DATA)
for label, c in (("FTP", cf), ("SFTP", cs)):
    good = True
    with Remote(c) as r:
        for off, ln in ((0, 100), (1000, 5000), (n - 100, 100), (0, n), (n - 1, 1), (MiB, MiB)):
            buf = io.BytesIO()
            try: r.read_range("d/big.bin", off, ln, buf)
            except Exception as e: good = False; print("  err", label, off, ln, repr(e)); continue
            good &= buf.getvalue() == DATA[off:off + ln]
    ok(good, f"2. {label}: read_range returns the exact bytes for 6 different ranges (REST / seek)")
    with Remote(c) as r:
        try: r.read_range("d/big.bin", n - 10, 50, io.BytesIO()); ok(False, "short read accepted")
        except EOFError: ok(True, f"2b. {label}: range beyond the end of the file is an error, not a silent short file")

# ---------------------------------------------------------------- 3. planning helpers
ok(largefiles.plan_volumes(10, 4) == [(0, 0, 4), (1, 4, 4), (2, 8, 2)] and largefiles.plan_volumes(8, 4)[-1] == (1, 4, 4), "3. plan_volumes covers the file exactly")
ok(largefiles.volume_name("a.zip", 0) == "a.zip.001" and largefiles.volume_name("a.zip", 11) == "a.zip.012", "3b. 7-Zip/split style names")
GB = 1024 ** 3
ok(largefiles.choose_volume(int(1.9 * GB), 100 * GB) == int(1.9 * GB), "3c. plenty of disk: Telegram's limit")
ok(largefiles.choose_volume(int(1.9 * GB), 1 * GB) == int(0.85 * GB), "3d. small disk: volume shrinks to 85% of the free space")
ok(largefiles.choose_volume(int(1.9 * GB), 100 * GB, 500 * MiB) == 500 * MiB, "3e. -v override honoured")
for args in ((GB, 10 * MiB // 100),):
    try: largefiles.choose_volume(*args); ok(False, "tiny disk accepted")
    except ValueError: ok(True, "3f. no disk space at all is a clear error")
try: largefiles.plan_volumes(2000 * MiB, MiB); ok(False, "too many volumes")
except ValueError: ok(True, "3g. more than 999 volumes is refused with advice")

# ---------------------------------------------------------------- 4. /get end to end: volumes staged one at a time, reassemble to the original
class FakeStatus:
    def __init__(self, log): self.log = log
    async def edit_text(self, t): self.log.append(t)
    async def delete(self): self.log.append("<deleted>")
class FakeMsg:
    def __init__(self): self.sent, self.texts, self.fail_next = collections.OrderedDict(), [], False
    async def reply(self, text, **kw): self.texts.append(text); return FakeStatus(self.texts)
    async def reply_document(self, path, file_name=None, progress=None):
        if self.fail_next: self.fail_next = False; raise ConnectionError("send failed")
        self.sent[file_name] = open(path, "rb").read()
def run_get(name, override=None, path="d/big.bin", msg=None):
    msg = msg or FakeMsg(); asyncio.run(bot.send_from_server(msg, UID, name, path, override)); return msg
for label, name in (("FTP", "ftp1"), ("SFTP", "vps")):
    m = run_get(name)
    ok(list(m.sent) == ["big.bin"] and m.sent["big.bin"] == DATA, f"4. {label}: a normal file is sent whole under its own name")
    m = run_get(name, override=MiB)
    ok(list(m.sent) == [f"big.bin.00{i}" for i in (1, 2, 3, 4)] and b"".join(m.sent.values()) == DATA and all(len(v) <= MiB for v in m.sent.values()),
       f"4b. {label}: -v 1 sends 4 volumes big.bin.001-.004, each <= 1 MiB, that reassemble byte-for-byte")
    ok(any("To reassemble" in t for t in m.texts), f"4c. {label}: reassembly instructions are sent")
    m = FakeMsg(); m.fail_next = True; run_get(name, override=MiB, msg=m)
    ok(b"".join(m.sent.values()) == DATA and len(m.sent) == 4, f"4d. {label}: a failed send is retried without duplicating or skipping a volume")
free_small = types.SimpleNamespace(total=0, used=0, free=int(2.2 * MiB / 0.85))
real_du = bot.shutil.disk_usage; bot.shutil.disk_usage = lambda p: free_small
m = run_get("vps"); bot.shutil.disk_usage = real_du
ok(len(m.sent) == 2 and b"".join(m.sent.values()) == DATA and all(len(v) <= 2.2 * MiB for v in m.sent.values()), "4e. low free disk: volumes shrink automatically so the file still goes out")
m = run_get("vps", path="d/missing.bin"); ok(not m.sent and any("does not exist" in t for t in m.texts), "4f. a missing file is a clear message")
big = random.Random(1).randbytes(6 * MiB + 5); open(f"{SROOT}/d/seven.bin", "wb").write(big)
asked = []
async def fake_confirm(msg, uid, label, action, *args): asked.append((label, action, args))
real_confirm = bot.confirm; bot.confirm = fake_confirm
m = run_get("vps", override=MiB, path="d/seven.bin"); bot.confirm = real_confirm
ok(len(asked) == 1 and "7 volumes" in asked[0][0] and not m.sent, "4g. more than 5 volumes asks for confirmation first")
m2 = FakeMsg()
asyncio.run(bot.ACTIONS[asked[0][1]](m2, UID, *asked[0][2]))
ok(len(m2.sent) == 7 and b"".join(m2.sent.values()) == big, "4h. confirming (via the ACTIONS registry) sends all 7 volumes")

# ---------------------------------------------------------------- 5. /join script on a real SSH server
def vols(base, parts):
    for i, p in enumerate(parts, 1): open(os.path.join(SROOT, f"{base}.{i:03d}"), "wb").write(p)
def join(base, rm=False, cwd=SROOT):
    with Remote(cs) as r: return r.shell(largefiles.join_script(base, rm), cwd, 60)[:2]
parts = [random.Random(i).randbytes(5000 + i) for i in range(4)]; vols("video.mkv", parts)
out, rc = join("video.mkv")
ok(rc == 0 and open(f"{SROOT}/video.mkv", "rb").read() == b"".join(parts) and os.path.exists(f"{SROOT}/video.mkv.003"), f"5. volumes joined in order, volumes kept ({out.strip()})")
vols("a.bin", parts[:3]); out, rc = join("a.bin", rm=True)
ok(rc == 0 and open(f"{SROOT}/a.bin", "rb").read() == b"".join(parts[:3]) and not os.path.exists(f"{SROOT}/a.bin.001"), "5b. --rm deletes the volumes only after a verified join")
vols("m.bin", parts[:1]); open(f"{SROOT}/m.bin.003", "wb").write(b"x"); out, rc = join("m.bin")
ok(rc == 7 and "missing" in out and not os.path.exists(f"{SROOT}/m.bin"), "5c. a missing middle volume is detected and nothing is created")
vols("e.bin", parts[:2]); open(f"{SROOT}/e.bin", "wb").write(b"old"); out, rc = join("e.bin")
ok(rc == 3 and open(f"{SROOT}/e.bin", "rb").read() == b"old", "5d. an existing target is never overwritten")
out, rc = join("nothing.bin"); ok(rc == 2 and "no volumes" in out, "5e. no volumes is a clear message")
vols("my file (1).bin", parts[:2]); out, rc = join("my file (1).bin")
ok(rc == 0 and open(f"{SROOT}/my file (1).bin", "rb").read() == b"".join(parts[:2]), "5f. names with spaces and brackets work")
os.makedirs(f"{SROOT}/sub"); open(f"{SROOT}/sub/z.bin.001", "wb").write(b"AA"); open(f"{SROOT}/sub/z.bin.002", "wb").write(b"BB")
out, rc = join("z.bin", cwd=f"{SROOT}/sub"); ok(rc == 0 and open(f"{SROOT}/sub/z.bin", "rb").read() == b"AABB", "5g. runs in the current directory of the chat")
print("ALL LARGE PASSED")
