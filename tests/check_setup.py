"""setup_gateway.py - the gateway setup tool: guided/batch writes to .env (validating every
value, preserving everything unrelated), secret generation, QR rendering, and --check
diagnostics with live probes against a fake gateway that answers like the real one."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import http.server, json, re, subprocess, tempfile, threading
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PY = sys.executable
TOOL = os.path.join(ROOT, "setup_gateway.py")

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
TMP = tempfile.mkdtemp()
run = lambda *a, **kw: subprocess.run([PY, TOOL, *a], capture_output=True, text=True,
                                      cwd=ROOT, timeout=120, **kw)

# ---------------------------------------------------------------- 1. secrets
r = run("--gen-secret")
s = r.stdout.strip()
ok(r.returncode == 0 and re.fullmatch(r"[A-Za-z0-9_\-]{43}", s) is not None,
   "1. --gen-secret prints one strong 43-char url-safe secret")
ok(len({run("--gen-secret").stdout.strip() for _ in range(3)}) == 3, "1b. every secret is different")

# ---------------------------------------------------------------- 2. a fake gateway + a fake bot port
GW_KEY, GW_SECRET = "mk_live_9f2b", "ms_live_77c1d0e5"

class Handler(http.server.BaseHTTPRequestHandler):
    """A miniature of the upi-gateway + the bot's own webhook port on one server:
    /api/v1/orders/* answers 401 for wrong merchant credentials and 404 for right ones
    (the setup probe looks up an order that cannot exist); /healthz answers 200."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return
        if self.path == "/pubkey/":  # what CryptAPI's pubkey endpoint answers
            body = json.dumps({"pubkey": "-----BEGIN PUBLIC KEY-----"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/v1/orders/"):
            if self.headers.get("X-Api-Key") != GW_KEY or self.headers.get("X-Api-Secret") != GW_SECRET:
                body = json.dumps({"error": "invalid credentials"}).encode()
                self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# ---------------------------------------------------------------- 3. batch writes validate + preserve
env1 = os.path.join(TMP, "one.env")
open(env1, "w").write("# my bot\nAPI_ID=12345\nSECRET_KEY=keep-this-untouched-please\n\n# notes survive\n")
r = run("--env", env1, "--write", "WEBAPP_URL=" + BASE, "UPIGW_URL=" + BASE,
        f"UPIGW_API_KEY={GW_KEY}", f"UPIGW_API_SECRET={GW_SECRET}",
        "UPIGW_WEBHOOK_SECRET=whsec_0123456789abcdef0123456789abcdef",
        "UPI_VPA=owner@okicici", "UPI_NAME=VPS Owner",
        "CRYPTAPI_BASE=" + BASE,
        "CRYPTAPI_BTC_ADDR=bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        "CRYPTAPI_TRC20_USDT_ADDR=TXk8r3tDgnyykVy5nPcmZ7wzstZyVHYmvF",
        "CRYPTAPI_TOLERANCE=2")
ok(r.returncode == 0 and "11 added" in r.stdout, "3. a full valid setup writes 11 keys, exit 0")
txt = open(env1).read()
ok("API_ID=12345" in txt and "SECRET_KEY=keep-this-untouched-please" in txt and "# notes survive" in txt,
   "3b. unrelated lines and comments survive untouched")
ok(txt.index("API_ID") < txt.index("# ---- payment gateways"), "3c. new keys are appended at the end")
ok(txt.count("UPI_VPA=") == 1, "3d. every key appears exactly once")
r = run("--env", env1, "--write", "UPI_VPA=9765432109@paytm")
txt = open(env1).read()
ok(r.returncode == 0 and "UPI_VPA=9765432109@paytm" in txt and "UPI_VPA=owner@okicici" not in txt
   and txt.count("UPI_VPA=") == 1, "3e. a re-write updates in place, no duplicates")
ok("SECRET_KEY=keep-this-untouched-please" in txt, "3f. in-place updates still preserve other keys")

# ---------------------------------------------------------------- 4. bad values are refused, nothing written
env2 = os.path.join(TMP, "two.env")
r = run("--env", env2, "--write", "UPI_VPA=not-a-vpa", "WEBAPP_URL=http://pay.remote.example.com",
        "UPIGW_WEBHOOK_SECRET=short", "UNKNOWN_KEY=1",
        "CRYPTAPI_BTC_ADDR=" + "X" * 26,
        "CRYPTAPI_TRC20_USDT_ADDR=" + "X" * 34,
        "CRYPTAPI_ETH_ADDR=0x" + "G" * 40, "CRYPTAPI_TOLERANCE=99")
ok(r.returncode == 2, "4. invalid batch -> exit 2")
for frag in ("UPI_VPA must be a UPI id", "must be https://", "16+ chars", "unknown key UNKNOWN_KEY",
             "Bitcoin address", "TRON address", "EVM address", "between 0 and 10"):
    ok(frag in r.stdout, f"4x. reports: {frag}")
ok(not os.path.exists(env2), "4b. a rejected batch writes nothing at all")
r = run("--env", env2, "--write", "WEBAPP_URL=" + BASE)
ok(r.returncode == 0, "4c. http:// is allowed for localhost (dev/tests), refused for public hosts")

# ---------------------------------------------------------------- 5. --check: empty, partial, full
env3 = os.path.join(TMP, "three.env")
open(env3, "w").write("API_ID=1\n")
r = run("--env", env3, "--check", "--no-probe")
ok(r.returncode == 0 and "no gateway configured yet" in r.stdout, "5. --check on an unconfigured bot: fine, exit 0")
r = run("--env", env3, "--write", "UPIGW_URL=" + BASE)
r = run("--env", env3, "--check", "--no-probe")
ok(r.returncode == 1 and "PARTIAL" in r.stdout and "missing: UPIGW_API_KEY" in r.stdout,
   "5b. a half-configured gateway is a problem: exit 1 with the missing keys")
r = run("--env", env1, "--check")
ok(r.returncode == 0 and r.stdout.count("READY") == 3 and "QR pipeline" in r.stdout
   and "webhooks can reach the bot" in r.stdout and "accepted the credentials" in r.stdout,
   "5c. full setup + live probes: 3 methods READY, webhook port answers, credentials accepted")

# ---------------------------------------------------------------- 6. live probes: wrong credentials, dead gateway
env4 = os.path.join(TMP, "four.env")
run("--env", env4, "--write", "WEBAPP_URL=" + BASE, "UPIGW_URL=" + BASE, "UPIGW_API_KEY=mk_wrong",
    "UPIGW_API_SECRET=ms_wrong", "UPIGW_WEBHOOK_SECRET=whsec_0123456789abcdef0123456789abcdef")
r = run("--env", env4, "--check")
ok(r.returncode == 1 and "refused the API key/secret" in r.stdout, "6. wrong credentials: the probe says so, exit 1")
env5 = os.path.join(TMP, "five.env")
run("--env", env5, "--write", "WEBAPP_URL=http://127.0.0.1:1",
    "UPIGW_URL=http://127.0.0.1:1", "UPIGW_API_KEY=mk_x", "UPIGW_API_SECRET=ms_x",
    "UPIGW_WEBHOOK_SECRET=whsec_0123456789abcdef0123456789abcdef")
r = run("--env", env5, "--check")
ok(r.returncode == 1 and "cannot reach" in r.stdout, "6b. a dead gateway/port: reported, exit 1")

# ---------------------------------------------------------------- 7. QR mode renders a real PNG
png = os.path.join(ROOT, "qr_test.png")
r = run("--qr", "upi://pay?pa=setup@okicici&pn=vpsbot&am=1.00&cu=INR&tr=vpsbotsetup")
data = open(png, "rb").read()
ok(r.returncode == 0 and data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 400,
   "7. --qr renders the text into a real PNG")
os.remove(png)

# ---------------------------------------------------------------- 8. the wizard is safe without a TTY
r = subprocess.run([PY, TOOL], input="", capture_output=True, text=True, cwd=ROOT, timeout=60)
ok(r.returncode == 0 and "Nothing to write" in r.stdout,
   "8. the interactive wizard piped empty input writes nothing, exits clean")

srv.shutdown()
print("\nALL SETUP CHECKS PASSED")
