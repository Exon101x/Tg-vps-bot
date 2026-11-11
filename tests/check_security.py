"""Regression checks for the security and robustness fixes:
- SEC-1  URL fetch pins the connection to the address that passed the SSRF check (one DNS lookup for both)
- SEC-1  redirects are re-validated hop by hop
- SEC-2  caption destinations are sanitised by clean_path (no ../, no absolute paths, no CRLF)
- SEC-3  FTP paths containing CR/LF are refused instead of being smuggled onto the control connection
- BUG-1  exists() falls back to a listing when the server has no SIZE (skip/rename can't degrade into overwrite)
- BUG-2  MAX_FETCH_MB is enforced even when the server sends no Content-Length
- BUG-5  a typo in an optional numeric env var can't crash the bot at import
- SC-4   healthcheck.py exits 0/1 as the Dockerfile HEALTHCHECK expects
"""
import os
import socket
import subprocess
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import http.server
import tempfile

from cryptography.fernet import Fernet

os.environ.update(SECRET_KEY=Fernet.generate_key().decode(),
                  DATABASE_URL="sqlite:///" + os.path.join(tempfile.mkdtemp(), "s.db"),
                  API_ID="1", API_HASH="x", BOT_TOKEN="1:x", ALLOWED_USERS="5", SESSION_DIR=tempfile.mkdtemp())

import ftplib
import sources
import util

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

# ---------------------------------------------------------------- SEC-2: clean_path
ok(util.clean_path("/etc/cron.d/x.sh") == "etc/cron.d/x.sh", "clean_path drops the leading /")
ok(util.clean_path("../../etc/passwd") == "etc/passwd", "clean_path neutralises ../ traversal")
ok(util.clean_path("dir/") == "dir", "clean_path keeps the directory (trailing / dropped)")
ok(util.clean_path("..") == "" and util.clean_path("/") == "" and util.clean_path(".") == "", "clean_path of traversal-only input is empty")
ok(util.clean_path("a/../b") == "a/b", "clean_path removes .. segments in the middle")
ok("\\" not in util.clean_path("C:\\boot\\evil.ini"), "clean_path cannot smuggle backslash paths")
ok(util.env_int("X_NOPE", 7) == 7, "env_int default when unset")
os.environ["X_NOPE"] = "banana"
ok(util.env_int("X_NOPE", 7) == 7, "env_int survives a non-numeric value")
os.environ["X_NOPE"] = "9"
ok(util.env_int("X_NOPE", 7, hi=5) == 7, "env_int clamps out-of-range values to the default")
del os.environ["X_NOPE"]

# ---------------------------------------------------------------- SEC-3: FTP path guard
import remote
ok(remote._ftp_path("plain/file.txt") == "plain/file.txt", "_ftp_path passes clean paths through")
try:
    remote._ftp_path("evil.txt\nNOOP")
    ok(False, "_ftp_path refuses CRLF")
except ValueError:
    ok(True, "_ftp_path refuses CRLF (FTP command injection)")

# ---------------------------------------------------------------- BUG-1: exists() without SIZE
class FakeFTPNoSize:
    """An FTP control connection of a server that does not implement SIZE (550 for everything)."""
    def size(self, path):
        raise ftplib.error_perm("550 SIZE not available")

    def nlst(self, arg):
        ok(arg == ".", "fallback lists the parent directory")
        return ["/srv/data/present.txt", "/srv/data/other.bin", "plain-name.txt"]

r = remote.Remote({"scheme": "ftp", "host": "h", "port": 21, "user": "u", "auth": "password", "secret": "s"})
r.cli = FakeFTPNoSize()
ok(r.exists("present.txt") is True, "exists() True for an existing file even without SIZE")
ok(r.exists("absent.txt") is False, "exists() False for a missing file even without SIZE")

# ---------------------------------------------------------------- helpers for the HTTP tests
DATA = b"x" * 300_000


class H(http.server.BaseHTTPRequestHandler):
    behaviour = "plain"   # plain | nolength | redirect_evil | redirect_ok
    def log_message(self, *a):
        pass

    def do_GET(self):
        if H.behaviour == "redirect_evil" and self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/evil")  # loopback: must be re-checked and refused
            self.end_headers()
            return
        if H.behaviour == "redirect_ok" and self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        if H.behaviour == "nolength":
            self.send_response(200)  # no Content-Length: size only becomes known while streaming
            self.end_headers()
            try:
                self.wfile.write(DATA)  # the reader may hang up mid-body when the cap trips: that's the test
            except (ConnectionResetError, BrokenPipeError):
                pass
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()
        try:
            self.wfile.write(DATA)
        except (ConnectionResetError, BrokenPipeError):
            pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://localhost:{srv.server_port}/f.bin"

# The production guard refuses loopback; the tests below only need the *mechanism*, so the
# guard is replaced by the same resolution without the address validation. (AF_INET so the
# first resolved address is the 127.0.0.1 the test server listens on, not ::1.)
def loopback_allowed(u):
    from urllib.parse import urlparse
    p = urlparse(u)
    return socket.getaddrinfo(p.hostname, p.port or 80, socket.AF_INET, socket.SOCK_STREAM)


real_check = sources.check_public
sources.check_public = loopback_allowed

# ---------------------------------------------------------------- SEC-1: single DNS resolution
HOST = "localhost"
dns_lookups = []
real_getaddrinfo = socket.getaddrinfo


def counting_getaddrinfo(host, *a, **kw):
    if host == HOST:  # count lookups of the hostname only, not of the already-pinned IP literal
        dns_lookups.append(host)
    return real_getaddrinfo(host, *a, **kw)


socket.getaddrinfo = counting_getaddrinfo
r = sources.HttpReader(url, 0, lambda *a: None, lambda: False, 10**9)
got = b""
while chunk := r.read(65536):
    got += chunk
r.close()
socket.getaddrinfo = real_getaddrinfo
ok(got == DATA, "HttpReader still reads the whole body")
ok(len(dns_lookups) == 1, f"hostname resolved exactly once (got {len(dns_lookups)}): no rebinding window between check and connect")

# ---------------------------------------------------------------- SEC-1: redirects re-validated
sources.check_public = real_check


def guard(u):
    if "evil" in u:
        raise ValueError("blocked redirect target")
    return loopback_allowed(u)


sources.check_public = guard
try:
    H.behaviour = "redirect_evil"
    r = sources.HttpReader(f"http://localhost:{srv.server_port}/hop", 0, lambda *a: None, lambda: False, 10**9)
    r.close()
    ok(False, "redirect to a refused target is blocked")
except ValueError:
    ok(True, "redirect to a refused target is blocked")
H.behaviour = "redirect_ok"
sources.check_public = loopback_allowed
r = sources.HttpReader(f"http://localhost:{srv.server_port}/hop", 0, lambda *a: None, lambda: False, 10**9)
r.close()
ok(True, "a redirect to an allowed target is followed")

# ---------------------------------------------------------------- BUG-2: cap without Content-Length
H.behaviour = "nolength"
r = sources.HttpReader(url, 0, lambda *a: None, lambda: False, 100_000)
try:
    while r.read(65536):
        pass
    ok(False, "MAX_FETCH_MB enforced without Content-Length")
except ValueError as e:
    ok("MAX_FETCH_MB" in str(e), "MAX_FETCH_MB enforced without Content-Length")
r.close()
importlib = __import__("importlib")
importlib.reload(sources)  # undo the patches for the next imports

# ---------------------------------------------------------------- BUG-5: bad env values at import
env_bad = dict(os.environ, TG_PARALLEL="banana", TG_WINDOW_MB="??", MAX_RETRIES="lots",
               MONITOR_INTERVAL="soon", DISK_ALERT="ninety", MAX_FETCH_MB="big", KEEP_ALIVE_INTERVAL="later")
p = subprocess.run([sys.executable, "-c", "import bot"], capture_output=True, text=True,
                   env=env_bad, cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), timeout=120)
ok(p.returncode == 0, f"a typo in any optional numeric env var doesn't crash the import ({(p.stderr or p.stdout).strip()[-120:]})")

# ---------------------------------------------------------------- SC-4: healthcheck.py
here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
p = subprocess.run([sys.executable, os.path.join(here, "healthcheck.py")], capture_output=True,
                   env=dict(os.environ, HEALTH_PORT="", PORT=""))
ok(p.returncode == 0, "healthcheck exits 0 when no port is configured")
p = subprocess.run([sys.executable, os.path.join(here, "healthcheck.py")], capture_output=True,
                   env=dict(os.environ, HEALTH_PORT=str(srv.server_port + 1)))
ok(p.returncode == 1, "healthcheck exits 1 when nothing listens")
p = subprocess.run([sys.executable, os.path.join(here, "healthcheck.py")], capture_output=True,
                   env=dict(os.environ, HEALTH_PORT=str(srv.server_port)))
ok(p.returncode == 0, "healthcheck exits 0 when the endpoint answers")
srv.shutdown()
print("ALL SECURITY PASSED")
