import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import http.server, importlib, random, tempfile, threading, types
from cryptography.fernet import Fernet
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL="sqlite:///" + os.path.join(tempfile.mkdtemp(), "m.db"),
                  API_ID="1", API_HASH="x", BOT_TOKEN="1:x", ALLOWED_USERS="5", SESSION_DIR=tempfile.mkdtemp())
import sources
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

DATA = random.Random(3).randbytes(1_500_000)
class H(http.server.BaseHTTPRequestHandler):
    honor_range = True
    def log_message(self, *a): pass
    def do_GET(self):
        rng = self.headers.get("Range"); start = 0
        if rng and H.honor_range: start = int(rng.split("=")[1].rstrip("-")); self.send_response(206)
        else: self.send_response(200)
        body = DATA[start:]
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/f.bin"
real_check = sources.check_public
sources.check_public = lambda u: None          # loopback is blocked in production; allow it for this test
def slurp(r):
    out = b""
    while (d := r.read(65536)): out += d
    return out
for honor in (True, False):
    H.honor_range = honor
    r = sources.HttpReader(url, 700_001, lambda *a: None, lambda: False, 10**9)
    ok(r.total == len(DATA) and DATA[700_001:] == slurp(r), f"HttpReader resume is exact (server honours Range={honor})")
try: sources.HttpReader(url, 0, lambda *a: None, lambda: False, 1000); ok(False, "size cap")
except ValueError as e: ok("MAX_FETCH_MB" in str(e), "MAX_FETCH_MB enforced")
importlib.reload(sources)
for bad in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "ftp://example.com/x"):
    try: sources.check_public(bad); ok(False, f"ssrf {bad}")
    except ValueError: ok(True, f"SSRF guard refuses {bad}")

import bot
U = types.SimpleNamespace(username="Nihal_S", id=42)
c = {"folder": ""}
ok(bot.render_dest(c, U, "a.jpg", "images") == "a.jpg", "no template -> plain name")
c["folder"] = "{user}/{date}"; d = bot.render_dest(c, U, "a.jpg", "images")
ok(d.startswith("Nihal_S/20") and d.endswith("/a.jpg") and d.count("/") == 2, f"template {{user}}/{{date}} -> {d}")
c["folder"] = "/uploads/{type}/{year}-{month}"; d = bot.render_dest(c, U, "a.jpg", "images")
ok(d.startswith("/uploads/images/20") and d.endswith("/a.jpg"), f"absolute template kept: {d}")
c["folder"] = "../../{user}/../etc"; d = bot.render_dest(c, U, "a.jpg", "images"); ok(".." not in d.split("/"), f"path traversal neutralised: {d}")
c["folder"] = "{nope}"; ok(bot.render_dest(c, U, "a.jpg", "images") == "a.jpg", "bad template falls back safely")
c["folder"] = "{user}"; ok(bot.render_dest(c, types.SimpleNamespace(username=None, id=42), "a.jpg", "x") == "42/a.jpg", "no username -> uid")
ok(bot.group_from_name("x.mp4") == "videos" and bot.group_from_name("x.png") == "images" and bot.group_from_name("x.zip") == "files", "group_from_name")
t = bot.parse_target("ssh://root@1.2.3.4:2222"); ok(t["port"] == 2222 and t["secret"] is None and t["user"] == "root", "parse ssh target")
t = bot.parse_target("ftps://bob:p%40ss@h.example/up"); ok(t["secret"] == "p@ss" and t["cwd"] == "/up" and t["scheme"] == "ftps", "parse ftps url with encoded password")
ok(bot.parse_target("sftp://u@h")["scheme"] == "ssh", "sftp:// is an alias for ssh")
for cmd, danger in [("ls -la", False), ("rm -rf /tmp/x", True), ("cat /etc/passwd", False), ("sudo passwd bob", True), ("systemctl restart nginx", False), ("docker ps", False), ("reboot", True), ("dd if=/dev/zero of=/dev/sda", True)]:
    ok(bool(bot.DANGEROUS.search(cmd)) == danger, f"confirm-guard: {cmd!r} -> {danger}")
print("ALL MISC PASSED")
