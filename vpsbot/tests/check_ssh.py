import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio, hashlib, random, tempfile, threading
from cryptography.fernet import Fernet
os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "s.db")
import asyncssh, store, remote, sources
from remote import Remote, HostKeyChanged

ROOT = tempfile.mkdtemp()
class Srv(asyncssh.SSHServer):
    def password_auth_supported(self): return True
    def validate_password(self, u, p): return u == "root" and p == "pw"

async def handle(process):
    proc = await asyncio.create_subprocess_exec("bash", "-c", process.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=ROOT)
    out, _ = await proc.communicate()
    process.stdout.write(out.decode(errors="replace")); process.exit(proc.returncode)

ready, info = threading.Event(), {}
def serve():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    key = asyncssh.generate_private_key("ssh-ed25519")
    async def go():
        s = await asyncssh.listen("127.0.0.1", 0, server_factory=Srv, server_host_keys=[key], process_factory=handle, sftp_factory=True)
        info["port"] = s.sockets[0].getsockname()[1]; ready.set()
    loop.run_until_complete(go()); loop.run_forever()
threading.Thread(target=serve, daemon=True).start(); ready.wait(10)

UID = 1
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
store.add_server(dict(uid=UID, name="vps", scheme="ssh", host="127.0.0.1", port=info["port"], user="root", auth="password", secret="pw", cwd="", hostkey=""))
c = store.get_server(UID, "vps")
with Remote(c) as r: ok(r.probe() == ROOT, "probe works")
c = store.get_server(UID, "vps")
ok(c["hostkey"].startswith("ssh-ed25519 "), "host key pinned on first connect: " + remote.fingerprint(c["hostkey"]))
with Remote(c) as r:
    out, rc, cwd = r.shell("mkdir -p a/b && cd a/b && pwd && echo hi >&2; false", None)
    ok(rc == 1 and "hi" in out and cwd.endswith("/a/b"), "shell: exit code, stderr merged, cwd tracked")
    out, rc, cwd2 = r.shell("pwd; ls", cwd); ok(out.splitlines()[0] == cwd and cwd2 == cwd, "next command starts in the remembered directory")
c["cwd"] = cwd
with Remote(c) as r:
    ok(r.cd("..") == f"{ROOT}/a", "cd .. resolves relative to the stored cwd")
    out, rc = r.exec("sleep 5", timeout=1); ok(rc == 124 and "timed out" in out, "command timeout enforced")
    out, rc = r.exec("yes | head -c 3000000", timeout=20); ok("truncated" in out and len(out) < 1_100_000, "huge output capped")
bad = store.get_server(UID, "vps"); bad["hostkey"] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPP"
try:
    with Remote(bad): ok(False, "should have refused")
except HostKeyChanged as e: ok("HOST KEY CHANGED" in str(e), "changed host key refused before credentials are sent")

DATA = random.Random(2).randbytes(3_000_000 + 4321)
class R(sources.Reader):
    total = len(DATA)
    def __init__(self, off, stop_at=None):
        super().__init__(off, lambda *a: None, lambda: False); self.i = off; self.stop_at = stop_at
    def read(self, n=-1):
        n = n if n > 0 else 1 << 20
        if self.stop_at is not None and self.i >= self.stop_at: raise ConnectionResetError("drop")
        d = DATA[self.i:self.i + n]; self.i += len(d); return d
c = store.get_server(UID, "vps"); c["cwd"] = ROOT
with Remote(c) as r:
    r.makedirs("up/x/y"); ok(r.exists("up/x/y"), "makedirs over SFTP")
    try: r.upload(lambda off: R(off, stop_at=1_700_000), "up/x/y/f.bin", resume=False)
    except ConnectionResetError: pass
    part = r.size("up/x/y/f.bin.part"); ok(0 < part < len(DATA), f"partial left on the server: {part} bytes")
with Remote(c) as r:
    r.upload(lambda off: R(off), "up/x/y/f.bin", resume=True)
    ok(not r.exists("up/x/y/f.bin.part") and r.size("up/x/y/f.bin") == len(DATA), "resumed + renamed")
    ok(r.unique_name("up/x/y/f.bin") == "up/x/y/f (1).bin", "unique_name over SFTP")
    ok(any(n == "f.bin" for n, d, s in r.ls("up/x/y")), "ls shows the file")
ok(hashlib.sha256(open(f"{ROOT}/up/x/y/f.bin", "rb").read()).hexdigest() == hashlib.sha256(DATA).hexdigest(), "SFTP resumed file is byte-identical")
with Remote(c) as r:
    r.upload(lambda off: R(off), "up/x/y/f.bin", resume=False); ok(r.size("up/x/y/f.bin") == len(DATA), "overwrite replaces an existing file")
gone = store.get_server(UID, "vps"); gone["cwd"] = ROOT + "/does-not-exist"
with Remote(gone) as r:
    try: r.makedirs("zzz"); ok(False, "should fail")
    except FileNotFoundError as e: ok("not accessible" in str(e), "vanished saved directory -> clear permanent error, not a silent upload into $HOME")
print("ALL SSH PASSED")
