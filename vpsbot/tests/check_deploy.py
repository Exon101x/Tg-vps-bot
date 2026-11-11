"""deploy.py: validation, .env writing, secrets handling, every target's plan, token/DB checks, scripted interactive run."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import contextlib, http.server, io, json, stat, subprocess, tempfile, threading
from dotenv import dotenv_values
from cryptography.fernet import Fernet
import deploy
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.chdir(tempfile.mkdtemp())  # relative sqlite paths (sqlite:///bot.db) must not create files inside the repository
API_HASH, TOKEN = "0123456789abcdef0123456789abcdef", "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
base = ["--yes", "--api-id", "123456", "--api-hash", API_HASH, "--bot-token", TOKEN, "--allowed-users", "111,222"]

def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try: code = deploy.main(argv)
        except SystemExit as e: code = e.code if isinstance(e.code, int) else 2; buf.write(f"EXIT: {e.code}\n")
    return code, buf.getvalue()
def tmp_env(): return os.path.join(tempfile.mkdtemp(), ".env")

# ---- 1. validators
ok(deploy.valid_api_id("123456") and not deploy.valid_api_id("12ab") and not deploy.valid_api_id(""), "1. API_ID")
ok(deploy.valid_api_hash(API_HASH) and not deploy.valid_api_hash("xyz") and not deploy.valid_api_hash(API_HASH + "0"), "1b. API_HASH is 32 hex characters")
ok(deploy.valid_bot_token(TOKEN) and not deploy.valid_bot_token("123:short") and not deploy.valid_bot_token("nocolon" * 6), "1c. BOT_TOKEN shape")
ok(deploy.parse_user_ids("1, 22 ,333") == [1, 22, 333], "1d. ALLOWED_USERS parsed")
for bad in ("", "abc", "1,x", "0", "-5"):
    try: deploy.parse_user_ids(bad); ok(False, f"accepted {bad!r}")
    except ValueError: pass
ok(True, "1e. ALLOWED_USERS rejects empty, text, zero and negative ids")
ok(all(deploy.valid_db_url(u) for u in ("postgres://a@h/d", "postgresql://a@h/d", "mysql://a@h/d", "mongodb+srv://a@h/d", "sqlite:///x.db")) and not deploy.valid_db_url("http://x") and not deploy.valid_db_url("nonsense"), "1f. database URL schemes")
k = deploy.new_secret_key(); Fernet(k); ok(deploy.new_secret_key() != k and len(deploy.random_password()) == 24, "1g. generated SECRET_KEY is a valid Fernet key; passwords are random")

# ---- 2. .env round trip through python-dotenv (what the bot itself loads)
tricky = {"API_ID": "123456", "API_HASH": API_HASH, "BOT_TOKEN": TOKEN, "ALLOWED_USERS": "1,2", "SECRET_KEY": k,
          "DATABASE_URL": "postgresql://u:p%40ss@h:5432/db?sslmode=require", "EXTRA_SPACE": "has space", "EXTRA_HASH": "a#b", "EXTRA_Q": "q\"uote'd", "EXTRA_SLASH": "back\\slash"}
p = tmp_env(); deploy.write_env(p, tricky, {})
ok(dotenv_values(p) == tricky and deploy.read_env(p) == tricky, "2. awkward values (spaces, #, quotes, backslashes, URL escapes) load back identically with python-dotenv and with deploy.py")
ok(stat.S_IMODE(os.stat(p).st_mode) == 0o600, "2b. .env is written with mode 600")
b = deploy.write_env(p, tricky, tricky); ok(b and os.path.exists(b) and stat.S_IMODE(os.stat(b).st_mode) == 0o600 and dotenv_values(b) == tricky, "2c. the previous .env is backed up (mode 600) before being replaced")
deploy.write_env(p, {"API_ID": "1"}, {"CUSTOM": "keep me", "API_ID": "old"}); ok(dotenv_values(p).get("CUSTOM") == "keep me" and dotenv_values(p)["API_ID"] == "1", "2d. unknown settings from the old .env survive, new values win")
errs, warns = deploy.validate_env({**tricky, "X": "a${HOME}b"}); ok(not errs and any("${" in w for w in warns), "2e. '${' triggers a warning (dotenv loaders would expand it)")

# ---- 3. setup runs
code, out = run(["--yes", "--env-file", tmp_env(), "--dry-run"]); ok(code == 2 and "API_ID" in out and "BOT_TOKEN" in out, "3. --yes without credentials fails with a clear list instead of writing a broken file")
code, out = run(base + ["--env-file", tmp_env(), "--dry-run", "--target", "docker"]); ok(code == 0 and "nothing is written" in out and "SECRET_KEY" in out, "3b. --dry-run succeeds and writes nothing")
deploy.compose_command = lambda: ["docker", "compose"]
env = tmp_env(); code, out = run(base + ["--env-file", env, "--target", "docker", "--db", "postgres"])
e1 = dotenv_values(env)
ok(code == 0 and e1["DATABASE_URL"] == f"postgresql://bot:{e1['POSTGRES_PASSWORD']}@postgres:5432/bot" and len(e1["POSTGRES_PASSWORD"]) == 24, "3c. bundled PostgreSQL: DATABASE_URL and POSTGRES_PASSWORD agree")
ok("docker compose --profile postgres up -d --build" in out and "Nothing was run" in out, "3d. docker plan shows the compose command and does not run it without --run")
code, out = run(base + ["--env-file", env, "--target", "docker", "--db", "postgres"]); e2 = dotenv_values(env)
ok(e2["SECRET_KEY"] == e1["SECRET_KEY"] and e2["POSTGRES_PASSWORD"] == e1["POSTGRES_PASSWORD"] and "kept" in out, "3e. re-running keeps SECRET_KEY and the Postgres password (changing either would break existing data)")
code, out = run(base + ["--env-file", env, "--target", "docker", "--db", "postgres", "--rotate-key"]); e3 = dotenv_values(env)
ok(e3["SECRET_KEY"] != e1["SECRET_KEY"] and "ROTATED" in out and e3["POSTGRES_PASSWORD"] == e1["POSTGRES_PASSWORD"], "3f. --rotate-key is the only way the key changes, and it warns")
ok(len([f for f in os.listdir(os.path.dirname(env)) if ".bak-" in f]) >= 2, "3g. every rewrite leaves a backup")
open(env, "a").write("LOG_LEVEL=DEBUG\nMY_EXTRA=1\n"); run(base + ["--env-file", env, "--target", "docker", "--db", "postgres"])
ok(dotenv_values(env).get("LOG_LEVEL") == "DEBUG" and dotenv_values(env).get("MY_EXTRA") == "1", "3h. custom settings added by hand survive a re-run")
env = tmp_env(); code, out = run(base + ["--env-file", env, "--target", "docker", "--db", "sqlite"]); ok(dotenv_values(env)["DATABASE_URL"] == "sqlite:////data/bot.db", "3i. docker + SQLite uses the /data volume")
env = tmp_env(); run(base + ["--env-file", env, "--target", "local"]); ok(dotenv_values(env)["DATABASE_URL"] == "sqlite:///bot.db", "3j. local + SQLite uses a file next to the bot")
env = tmp_env(); run(base + ["--env-file", env, "--target", "docker", "--db", "mongo"]); ok(dotenv_values(env)["DATABASE_URL"] == "mongodb://mongo:27017/bot", "3k. bundled MongoDB")
code, out = run(base + ["--env-file", tmp_env(), "--target", "systemd", "--db", "postgres", "--dry-run"]); ok(code != 0 and "--target docker" in out, "3l. a bundled database on a non-docker target is refused with the fix")
code, out = run(base + ["--env-file", tmp_env(), "--target", "render", "--db", "external", "--dry-run"]); ok(code != 0 and "--database-url" in out, "3m. --db external without a URL is refused")
code, out = run(base + ["--env-file", tmp_env(), "--target", "render", "--db", "external", "--database-url", "http://nope", "--dry-run"]); ok(code != 0, "3n. a URL with a wrong scheme is refused")
code, out = run(base + ["--env-file", tmp_env(), "--target", "render", "--db", "external", "--database-url", "postgres://u:topsecret@db.example.com/bot", "--dry-run"])
ok("postgres://db.example.com" in out and "topsecret" not in out, "3o0. the summary names the database host and never prints its password")
code, out = run(base + ["--env-file", tmp_env(), "--target", "render", "--dry-run"]); ok("wiped on every restart" in out, "3o. SQLite on Render warns that the disk is ephemeral")
env = tmp_env(); code, out = run(base + ["--env-file", env, "--target", "render", "--db", "external", "--database-url", "postgres://u:p@db.example/x"])
rp = os.path.join(os.path.dirname(env), "deploy", "render.env"); r = dotenv_values(rp)
ok(code == 0 and r["KEEP_ALIVE"] == "1" and r["DATABASE_URL"].startswith("postgres://") and "POSTGRES_PASSWORD" not in r and r["SECRET_KEY"] == dotenv_values(env)["SECRET_KEY"] and stat.S_IMODE(os.stat(rp).st_mode) == 0o600, "3p. Render: a ready-to-upload deploy/render.env (mode 600, KEEP_ALIVE on, same SECRET_KEY)")
env = tmp_env(); run(base + ["--env-file", env, "--target", "paas", "--db", "external", "--database-url", "mongodb+srv://u:p@c.example/x"])
ok("KEEP_ALIVE" not in dotenv_values(os.path.join(os.path.dirname(env), "deploy", "paas.env")), "3q. other PaaS: env file without the Render-only keep-alive")
os.environ["API_ID"], os.environ["BOT_TOKEN"] = "777777", TOKEN
env = tmp_env(); run(["--yes", "--from-env", "--api-hash", API_HASH, "--allowed-users", "5", "--env-file", env, "--target", "local"])
ok(dotenv_values(env).get("API_ID") == "777777", "3r. --from-env reads credentials from the process environment (CI)")

# ---- 4. plans
ok(deploy.plan_docker("mongo", ["docker", "compose"], "/x")[0] == ("run", ["docker", "compose", "--profile", "mongo", "up", "-d", "--build"]), "4. docker plan with the mongo profile")
ok(deploy.plan_docker("sqlite", ["docker-compose"], "/x")[0][1] == ["docker-compose", "up", "-d", "--build"], "4b. legacy docker-compose binary")
ok(deploy.plan_docker("sqlite", None, "/x")[0][0] == "note", "4c. no Docker: a clear note instead of a failing command")
template = open(os.path.join(ROOT, "deploy", "vpsbot.service")).read()
steps = deploy.plan_systemd("/srv/bot", "/srv/bot/.env", "alice", False, template)
unit = [s for s in steps if s[0] == "write"][0][2]
ok("/opt/vpsbot" not in unit and "User=alice" in unit and "ExecStart=/srv/bot/venv/bin/python bot.py" in unit and "EnvironmentFile=/srv/bot/.env" in unit and "ReadWritePaths=/srv/bot" in unit and "ProtectHome=true" in unit, "4d. systemd unit rendered for the real directory, user and env file")
kinds = [s[1][0] if s[0] == "run" else s[0] for s in steps]
ok(kinds[:4] == ["python3", os.path.join("/srv/bot", "venv", "bin", "pip"), "chown", "write"] and kinds[-3:] == ["systemctl", "systemctl", "note"] and steps[-3][1] == ["systemctl", "daemon-reload"], f"4e. systemd steps in a sane order: {kinds}")
steps = deploy.plan_systemd("/home/bob/vpsbot", "/home/bob/vpsbot/.env", "vpsbot", True, template)
ok(steps[0][1][:2] == ["useradd", "--system"] and "ProtectHome=false" in [s for s in steps if s[0] == "write"][0][2], "4f. dedicated user is created when needed, and code under /home gets ProtectHome=false so the service can read it")
d = tempfile.mkdtemp(); target_file = os.path.join(d, "etc", "vpsbot.service")
buf = []; deploy.execute([("write", target_file, "x", 0o644), ("note", "hi")], dry_run=True, out=buf.append); ok(not os.path.exists(target_file), "4g. dry-run executes nothing")
deploy.execute([("write", target_file, "x", 0o640)], out=buf.append); ok(open(target_file).read() == "x" and stat.S_IMODE(os.stat(target_file).st_mode) == 0o640, "4h. write steps create directories and set the mode")
ok(deploy.execute([("run", [sys.executable, "-c", "raise SystemExit(3)"]), ("note", "never shown")], out=buf.append) is False and not any("never shown" in b for b in buf), "4i. a failing command stops the run")
ok(deploy.execute([("run", [sys.executable, "-c", "pass"])], out=buf.append) is True, "4j. successful commands are run")
ok(deploy.service_user(ROOT)[0] != "" and isinstance(deploy.service_user("/")[1], bool), "4k. service user is chosen from the directory owner (or a dedicated user when that is root)")

# ---- 5. talking to Telegram (a local stand-in for api.telegram.org) and databases
class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        code, body = {"/bot111:good/getMe": (200, {"ok": True, "result": {"username": "my_bot"}}), "/bot111:bad/getMe": (401, {"ok": False}), "/bot111:boom/getMe": (500, {})}.get(self.path, (404, {}))
        self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(body).encode())
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{srv.server_port}"
ok(deploy.verify_token("111:good", base_url) == (True, "@my_bot"), "5. token accepted: the bot's username is shown")
ok(deploy.verify_token("111:bad", base_url)[0] is False and "rejected" in deploy.verify_token("111:bad", base_url)[1], "5b. a revoked/mistyped token is a hard failure")
ok(deploy.verify_token("111:boom", base_url)[0] is None, "5c. a Telegram outage is 'not checked', not a failure")
ok(deploy.verify_token("111:good", "http://127.0.0.1:1", timeout=2)[0] is None, "5d. no network is 'not checked', not a failure")
ok(deploy.check_database("sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db"))[0] is True, "5e. database check connects to SQLite")
res = deploy.check_database("postgresql://u:p@127.0.0.1:1/db"); ok(res[0] is False and res[1], f"5f. an unreachable database is a clear failure ({res[1][:50]})")

# ---- 6. --check
env = tmp_env(); run(base + ["--env-file", env, "--target", "local"])
code, out = run(["--check", "--env-file", env]); ok(code == 0 and "[ok] required settings" in out and "database" in out, "6. --check passes on a freshly generated .env")
os.chmod(env, 0o644); code, out = run(["--check", "--env-file", env, "--no-db"]); ok("chmod 600" in out, "6b. --check warns about a world-readable .env")
broken = tmp_env(); open(broken, "w").write("API_ID=abc\nBOT_TOKEN=nope\nSECRET_KEY=short\n")
code, out = run(["--check", "--env-file", broken]); ok(code == 1 and out.count("[!!]") >= 4, "6c. --check lists every problem and exits 1")
code, out = run(["--check", "--env-file", tmp_env()]); ok(code == 1 and "python deploy.py" in out, "6d. a missing .env tells you what to run")

# ---- 7. a scripted interactive session (what a person typing would do), including a typo that gets re-asked
env = tmp_env()
answers = "\n".join(["1", "12ab", "123456", API_HASH, TOKEN, "nope", "111,222", "2", "y", "n", "n", ""])   # docker, bad id then good, bad users then good, postgres, write, no verify, no run
r = subprocess.run([sys.executable, os.path.join(ROOT, "deploy.py"), "--env-file", env], input=answers, capture_output=True, text=True, cwd=ROOT)
e = dotenv_values(env)
ok(r.returncode == 0 and e.get("API_ID") == "123456" and e.get("ALLOWED_USERS") == "111,222" and e["DATABASE_URL"].startswith("postgresql://bot:"), f"7. scripted interactive run writes the right .env (exit {r.returncode})")
ok("invalid" in r.stdout or "!" in r.stdout, "7b. invalid answers are rejected and asked again")
r = subprocess.run([sys.executable, os.path.join(ROOT, "deploy.py"), "--env-file", tmp_env()], input="1\n123456\n", capture_output=True, text=True, cwd=ROOT)
ok(r.returncode == 1 and "Cancelled" in r.stdout, "7c. closing the input halfway cancels cleanly without a traceback")
env = tmp_env(); r = subprocess.run([sys.executable, os.path.join(ROOT, "deploy.py"), "--env-file", env], input=answers.replace("\ny\nn\nn\n", "\nn\n"), capture_output=True, text=True, cwd=ROOT)
ok(not os.path.exists(env) and "Nothing written" in r.stdout, "7d. answering 'no' at the confirmation writes nothing")
print("ALL DEPLOY PASSED")
