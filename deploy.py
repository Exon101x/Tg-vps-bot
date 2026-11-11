#!/usr/bin/env python3
"""Deployment setup wizard for vpsbot. Standard library only, so it runs before `pip install`.

    python deploy.py                  interactive: asks a few questions, writes .env, prepares the chosen target
    python deploy.py --check          validate an existing .env (and test the database)
    python deploy.py --yes --target docker --db postgres --api-id 123 --api-hash ... --bot-token ... --allowed-users 1,2
                                      the same without questions (CI / scripts); add --dry-run to only show what would happen

Targets: docker (compose), systemd (Linux service), render, paas (Heroku/Railway/Fly/Koyeb/...), local (virtualenv).
An existing SECRET_KEY is never replaced (it decrypts the saved servers); --rotate-key is the only way to do that.
"""
import argparse
import base64
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import pwd
except ImportError:  # Windows
    pwd = None

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = ("docker", "systemd", "render", "paas", "local")
DBS = ("sqlite", "postgres", "mongo", "external")
DB_SCHEMES = ("postgres", "postgresql", "mysql", "mariadb", "mongodb", "mongodb+srv", "sqlite")
SECTIONS = [
    ("Telegram (my.telegram.org and @BotFather)", ["API_ID", "API_HASH", "BOT_TOKEN", "ALLOWED_USERS"]),
    ("Encrypts saved servers: back this up, never change it", ["SECRET_KEY"]),
    ("Storage", ["DATABASE_URL", "POSTGRES_PASSWORD"]),
    ("Hosting", ["KEEP_ALIVE"]),
]
KNOWN = [k for _, keys in SECTIONS for k in keys]


# ---------------------------------------------------------------- validation
def valid_api_id(v):
    return bool(re.fullmatch(r"\d{3,12}", v or ""))


def valid_api_hash(v):
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", v or ""))


def valid_bot_token(v):
    return bool(re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", v or ""))


def parse_user_ids(v):
    """'123, 456' -> [123, 456]; raises ValueError otherwise."""
    ids = [p.strip() for p in (v or "").split(",") if p.strip()]
    if not ids or not all(p.isdigit() and int(p) > 0 for p in ids):
        raise ValueError("ALLOWED_USERS must be a comma-separated list of numeric Telegram user ids (ask @userinfobot for yours)")
    return [int(p) for p in ids]


def valid_db_url(v):
    m = re.match(r"^([a-z+]+)://", v or "")
    return bool(m) and m.group(1) in DB_SCHEMES


def valid_secret_key(v):
    return len(v or "") >= 16


def new_secret_key():
    """A valid Fernet key (urlsafe base64 of 32 random bytes)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def random_password(n=24):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def mask(v):
    v = v or ""
    return v if len(v) <= 8 else f"{v[:4]}...{v[-2:]}"


def validate_env(env):
    """-> (errors, warnings) for a settings dict."""
    errors, warnings = [], []
    if not valid_api_id(env.get("API_ID")):
        errors.append("API_ID must be the number from my.telegram.org")
    if not valid_api_hash(env.get("API_HASH")):
        errors.append("API_HASH must be the 32-character hex string from my.telegram.org")
    if not valid_bot_token(env.get("BOT_TOKEN")):
        errors.append("BOT_TOKEN does not look like a BotFather token (123456:ABC...)")
    try:
        parse_user_ids(env.get("ALLOWED_USERS"))
    except ValueError as e:
        errors.append(str(e))
    if not valid_secret_key(env.get("SECRET_KEY")):
        errors.append("SECRET_KEY is missing or shorter than 16 characters")
    url = env.get("DATABASE_URL", "")
    if url and not valid_db_url(url):
        errors.append("DATABASE_URL must start with sqlite://, postgresql://, mysql://, mongodb:// or mongodb+srv://")
    for k, v in env.items():
        if "${" in (v or ""):
            warnings.append(f"{k} contains '${{', which .env loaders expand as a variable; avoid it")
    return errors, warnings


# ---------------------------------------------------------------- .env reading and writing
def read_env(path):
    """Minimal .env parser: KEY=value, quotes, comments. Returns {} if the file does not exist."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if val[:1] == '"':
                m = re.match(r'"((?:[^"\\]|\\.)*)"', val)
                val = re.sub(r"\\(.)", r"\1", m.group(1)) if m else val
            elif val[:1] == "'":
                m = re.match(r"'([^']*)'", val)
                val = m.group(1) if m else val
            else:
                val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
            out[key] = val
    return out


def format_value(v):
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./~-]*", v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def render_env(env, extra=None):
    lines = ["# Generated by deploy.py. See .env.example for every option. Never commit this file.", ""]
    for title, keys in SECTIONS:
        present = [k for k in keys if k in env and env[k] != ""]
        if present:
            lines.append(f"# {title}")
            lines += [f"{k}={format_value(env[k])}" for k in present]
            lines.append("")
    leftovers = {k: v for k, v in {**(extra or {}), **env}.items() if k not in KNOWN}
    if leftovers:
        lines.append("# Other settings (kept from the previous .env)")
        lines += [f"{k}={format_value(v)}" for k, v in leftovers.items()]
        lines.append("")
    return "\n".join(lines)


def write_env(path, env, previous):
    """Write the file (mode 600), keeping a timestamped backup of the old one."""
    backup = None
    if os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup, n = f"{path}.bak-{stamp}", 1
        while os.path.exists(backup):  # never overwrite an earlier backup, it may hold the only copy of the old SECRET_KEY
            backup, n = f"{path}.bak-{stamp}-{n}", n + 1
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    extra = {k: v for k, v in previous.items() if k not in env}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(render_env(env, extra))
    os.chmod(path, 0o600)
    return backup


# ---------------------------------------------------------------- checks that talk to the outside world
def verify_token(token, base="https://api.telegram.org", timeout=10):
    """(True, '@name') | (False, reason) when Telegram refuses it | (None, reason) when it could not be reached."""
    try:
        with urllib.request.urlopen(f"{base}/bot{token}/getMe", timeout=timeout) as r:
            data = json.load(r)
        return True, "@" + data["result"]["username"]
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            return False, "Telegram rejected the token (revoked or mistyped)"
        return None, f"Telegram answered HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return None, f"could not reach Telegram ({getattr(e, 'reason', e)})"


def check_database(url):
    """(True, msg) | (False, reason) | (None, why it was not tested)."""
    sys.path.insert(0, HERE)
    try:
        from backends import describe, open_backend
    except ImportError:
        return None, "database drivers are not installed yet (pip install -r requirements.txt), so it was not tested"
    try:
        open_backend(url).is_populated()
        return True, f"connected to {describe(url)}"
    except Exception as e:  # noqa: BLE001 - any driver error is a failed check
        return False, f"{type(e).__name__}: {str(e)[:180]}"


# ---------------------------------------------------------------- deployment plans: ("run", [argv]) | ("write", path, text, mode) | ("note", text)
def compose_command():
    if shutil.which("docker"):
        try:
            if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
                return ["docker", "compose"]
        except OSError:
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def plan_docker(db, compose, workdir):
    if not compose:
        return [("note", "Docker with the compose plugin was not found: install Docker first (https://docs.docker.com/engine/install/).")]
    profile = {"postgres": ["--profile", "postgres"], "mongo": ["--profile", "mongo"]}.get(db, [])
    return [("run", compose + profile + ["up", "-d", "--build"]), ("note", f"Logs: {' '.join(compose)} logs -f bot   (in {workdir})")]


def render_unit(template, install_dir, user, env_file, home_protected=True):
    text = template.replace("/opt/vpsbot/venv/bin/python", os.path.join(install_dir, "venv", "bin", "python"))
    text = text.replace("/opt/vpsbot/.env", env_file).replace("/opt/vpsbot", install_dir)
    text = re.sub(r"^User=.*$", f"User={user}", text, flags=re.M)
    if not home_protected:  # code living under /home or /root would be invisible to the service otherwise
        text = text.replace("ProtectHome=true", "ProtectHome=false")
    return text


def service_user(install_dir):
    """(user, must_create): the owner of the install dir, or a dedicated 'vpsbot' user when that owner is root."""
    if pwd is not None:
        try:
            owner = pwd.getpwuid(os.stat(install_dir).st_uid).pw_name
            if owner != "root":
                return owner, False
        except (KeyError, OSError):
            pass
    return "vpsbot", True


def plan_systemd(install_dir, env_file, user, create_user, template, python="python3"):
    protected = not install_dir.startswith(("/home/", "/root"))
    unit = render_unit(template, install_dir, user, env_file, protected)
    steps = []
    if create_user:
        steps.append(("run", ["useradd", "--system", "--home-dir", install_dir, "--shell", "/usr/sbin/nologin", user]))
    steps += [
        ("run", [python, "-m", "venv", os.path.join(install_dir, "venv")]),
        ("run", [os.path.join(install_dir, "venv", "bin", "pip"), "install", "-r", os.path.join(install_dir, "requirements.txt")]),
        ("run", ["chown", "-R", f"{user}:", install_dir]),
        ("write", "/etc/systemd/system/vpsbot.service", unit, 0o644),
        ("run", ["systemctl", "daemon-reload"]),
        ("run", ["systemctl", "enable", "--now", "vpsbot"]),
        ("note", "Logs: journalctl -u vpsbot -f"),
    ]
    return steps


def plan_local(workdir):
    venv = os.path.join(workdir, ".venv")
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    return [
        ("run", [sys.executable, "-m", "venv", venv]),
        ("run", [os.path.join(venv, bin_dir, "pip"), "install", "-r", os.path.join(workdir, "requirements.txt")]),
        ("note", f"Start the bot with: {os.path.join(venv, bin_dir, 'python')} {os.path.join(workdir, 'bot.py')}"),
    ]


def plan_hosted(target, env, out_dir):
    """Render / other PaaS: a ready-to-upload env file instead of commands."""
    hosted = {k: v for k, v in env.items() if k in KNOWN and v != "" and k != "POSTGRES_PASSWORD"}
    if target == "render":
        hosted["KEEP_ALIVE"] = "1"
    path = os.path.join(out_dir, f"{target}.env")
    steps = [("write", path, render_env(hosted), 0o600)]
    if target == "render":
        steps.append(("note", f"Render: push the repo, New > Blueprint (render.yaml), and fill the prompts from {path}. For a manual service use "
                              "Environment > Add from .env and upload that file. The blueprint generates its own SECRET_KEY: if you already have saved "
                              "servers (or a database you are moving), overwrite SECRET_KEY on Render with the value in that file, otherwise copy Render's "
                              "generated value somewhere safe."))
    else:
        steps.append(("note", f"Set the variables from {path} in your platform (Heroku: heroku config:set / Railway, Fly.io, Koyeb: dashboard or CLI "
                              "secrets), deploy the Dockerfile, run exactly one instance. See docs/DEPLOY.md."))
    steps.append(("note", f"{path} contains secrets: it is git-ignored, delete it once the variables are set."))
    return steps


def execute(steps, dry_run=False, out=print):
    for step in steps:
        kind = step[0]
        if kind == "note":
            out(f"  note: {step[1]}")
        elif kind == "write":
            _, path, text, mode = step
            out(f"  write {path}")
            if not dry_run:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.chmod(path, mode)
        else:
            out("  $ " + " ".join(step[1]))
            if not dry_run and subprocess.run(step[1]).returncode != 0:
                out("  ! that command failed, stopping here")
                return False
    return True


# ---------------------------------------------------------------- interactive helpers
def ask(prompt, default=None, secret=False, validator=None, error="invalid value"):
    shown = f" [{default}]" if default else ""
    while True:
        text = f"{prompt}{shown}: "
        value = (getpass.getpass(text) if secret and sys.stdin.isatty() else input(text)).strip() or (default or "")
        if validator is None or validator(value):
            return value
        print(f"  ! {error}")


def choose(prompt, options, default=0):
    print(prompt)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    while True:
        raw = input(f"Choose 1-{len(options)} [{default + 1}]: ").strip() or str(default + 1)
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  ! enter one of the numbers")


def yes(prompt, default=False):
    raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not raw else raw.startswith("y")


def db_kind(url):
    if not url:
        return None
    if url.startswith("sqlite"):
        return "sqlite"
    if "@postgres:" in url:
        return "postgres"
    if "//mongo:" in url:
        return "mongo"
    return "external"


# ---------------------------------------------------------------- commands
def build_database(args, existing, target, interactive):
    """-> (DATABASE_URL, extra env, warnings, db kind)"""
    current = db_kind(existing.get("DATABASE_URL"))
    kind = args.db
    if kind is None and interactive:
        options = [("sqlite", "SQLite file (simplest; needs a disk that survives restarts)")]
        if target == "docker":
            options += [("postgres", "PostgreSQL started by docker compose"), ("mongo", "MongoDB started by docker compose")]
        options.append(("external", "Existing database URL (PostgreSQL, MySQL/MariaDB, MongoDB/Atlas)"))
        default = next((i for i, (k, _) in enumerate(options) if k == current), 0)
        kind = choose("\nWhere should the bot keep its data?", options, default)
    kind = kind or current or "sqlite"
    warnings = []
    if kind in ("postgres", "mongo") and target != "docker":
        raise SystemExit(f"--db {kind} starts a database with docker compose, so it needs --target docker. Use --db external with a URL for other targets.")
    if kind == "sqlite":
        if target in ("render", "paas"):
            warnings.append("SQLite on a hosted platform is wiped on every restart (saved servers and the queue would be lost): use an external database")
        return ("sqlite:////data/bot.db" if target == "docker" else "sqlite:///bot.db"), {}, warnings, kind
    if kind == "postgres":
        pw = existing.get("POSTGRES_PASSWORD") if current == "postgres" else None
        pw = pw or random_password()  # reusing it matters: Postgres sets the password once, when its volume is created
        return f"postgresql://bot:{pw}@postgres:5432/bot", {"POSTGRES_PASSWORD": pw}, warnings, kind
    if kind == "mongo":
        return "mongodb://mongo:27017/bot", {}, warnings, kind
    url = args.database_url or (existing.get("DATABASE_URL") if current == "external" else "")
    if interactive and not args.database_url:
        url = ask("Database URL (postgresql://..., mysql://..., mongodb+srv://...)", url or None, secret=True, validator=valid_db_url,
                  error="must start with postgresql://, mysql://, mongodb:// or mongodb+srv://")
    if not valid_db_url(url or ""):
        raise SystemExit("--db external needs --database-url (postgresql://, mysql://, mongodb://, mongodb+srv://)")
    return url, {}, warnings, kind


def cmd_setup(args):
    env_file = os.path.abspath(args.env_file)
    out_dir = os.path.join(os.path.dirname(env_file), "deploy")
    interactive = not args.yes
    previous = read_env(env_file)
    src = dict(previous)
    if args.from_env:
        src.update({k: os.environ[k] for k in KNOWN if os.environ.get(k)})
    for flag, key in (("api_id", "API_ID"), ("api_hash", "API_HASH"), ("bot_token", "BOT_TOKEN"), ("allowed_users", "ALLOWED_USERS")):
        if getattr(args, flag):
            src[key] = getattr(args, flag)

    print("vpsbot deployment setup\n")
    target = args.target
    if target is None and interactive:
        target = choose("Where will the bot run?", [
            ("docker", "Docker / docker compose (recommended for a VPS)"), ("systemd", "systemd service (Linux server, Raspberry Pi)"),
            ("render", "Render"), ("paas", "Another platform (Heroku, Railway, Fly.io, Koyeb, ...)"), ("local", "This machine, in a virtualenv")])
    target = target or "docker"

    env = {}
    if interactive:
        print("\nTelegram credentials (https://my.telegram.org and @BotFather):")
        env["API_ID"] = ask("API_ID", src.get("API_ID"), validator=valid_api_id, error="the numeric API ID from my.telegram.org")
        env["API_HASH"] = ask("API_HASH", src.get("API_HASH"), secret=True, validator=valid_api_hash, error="32 hexadecimal characters")
        env["BOT_TOKEN"] = ask("BOT_TOKEN", src.get("BOT_TOKEN"), secret=True, validator=valid_bot_token, error="like 123456:ABC-DEF... from @BotFather")
        env["ALLOWED_USERS"] = ask("ALLOWED_USERS (numeric Telegram ids, comma-separated; @userinfobot tells you yours)",
                                   src.get("ALLOWED_USERS"), validator=lambda v: _ok(parse_user_ids, v), error="numbers separated by commas")
    else:
        env.update({k: src.get(k, "") for k in ("API_ID", "API_HASH", "BOT_TOKEN", "ALLOWED_USERS")})

    old_key = previous.get("SECRET_KEY") or src.get("SECRET_KEY")
    if args.rotate_key or not old_key:
        env["SECRET_KEY"] = new_secret_key()
        print("\nSECRET_KEY: " + ("ROTATED. Servers saved with the old key can no longer be read; add them again." if old_key else "generated. Back up the .env file (or just this value)."))
    else:
        env["SECRET_KEY"] = old_key
        print("\nSECRET_KEY: kept (changing it would make saved servers unreadable).")

    url, extra, warnings, db = build_database(args, previous, target, interactive)
    env["DATABASE_URL"] = url
    env.update(extra)
    errors, more = validate_env(env)
    warnings += more
    if errors:
        print("\nCannot continue:")
        for e in errors:
            print(f"  - {e}")
        return 2

    print("\nSummary:")
    print(f"  target      {target}")
    print(f"  database    {db} ({url if db == 'sqlite' else _where(url)})")
    print(f"  bot token   {mask(env['BOT_TOKEN'])}   api id {env['API_ID']}   allowed users {env['ALLOWED_USERS']}")
    print(f"  .env        {env_file}" + ("  (dry run: nothing is written)" if args.dry_run else ""))
    for w in warnings:
        print(f"  warning: {w}")
    if interactive and not args.dry_run and not yes("\nWrite these settings?", True):
        print("Nothing written.")
        return 1

    if not args.dry_run:
        backup = write_env(env_file, env, previous)
        print(f"\nWrote {env_file} (mode 600)" + (f"; previous version saved as {backup}" if backup else ""))

    if args.verify or (interactive and yes("Check the bot token with Telegram and test the database now?", True)):
        ok, msg = verify_token(env["BOT_TOKEN"])
        print(f"  bot token: {'OK ' + msg if ok else ('FAILED: ' if ok is False else 'not checked: ') + msg}")
        if ok is False:
            print("  The settings were saved; fix the token and run this again.")
            return 1
        if db in ("postgres", "mongo") or (db == "sqlite" and target == "docker"):
            print("  database: not tested from here (it lives inside the containers)")
        else:
            ok, msg = check_database(url)
            print(f"  database: {'OK ' + msg if ok else ('FAILED: ' if ok is False else 'not checked: ') + msg}")
            if ok is False:
                return 1

    print(f"\nPlan for '{target}':")
    if target == "docker":
        steps = plan_docker(db, compose_command(), os.path.dirname(env_file))
    elif target == "systemd":
        install_dir = os.path.dirname(env_file)
        user, create = service_user(install_dir)
        with open(os.path.join(HERE, "deploy", "vpsbot.service"), encoding="utf-8") as f:
            steps = plan_systemd(install_dir, env_file, user, create, f.read())
    elif target == "local":
        steps = plan_local(os.path.dirname(env_file))
    else:
        steps = plan_hosted(target, env, out_dir)
    runnable = any(s[0] in ("run", "write") for s in steps) and target not in ("render", "paas")
    run_now = False
    if runnable and not args.dry_run:
        run_now = args.run or (interactive and yes("\nRun these steps now?" + (" (needs root)" if target == "systemd" else ""), False))
    if target in ("render", "paas"):
        run_now = not args.dry_run  # writing the env file is the whole job
    if not execute(steps, dry_run=args.dry_run or not run_now):
        return 1
    if runnable and not run_now and not args.dry_run:
        print("\nNothing was run. Re-run with --run, or execute the commands above yourself.")
    return 0


def _where(url):
    """scheme://host, never the credentials."""
    m = re.match(r"^([a-z+]+)://(?:[^@/]*@)?([^/?:]+)", url or "")
    return f"{m.group(1)}://{m.group(2)}" if m else "(hidden)"


def _ok(fn, v):
    try:
        fn(v)
        return True
    except ValueError:
        return False


def cmd_check(args):
    env_file = os.path.abspath(args.env_file)
    env = read_env(env_file)
    if not env:
        print(f"[!!] {env_file} does not exist or is empty. Run: python deploy.py")
        return 1
    errors, warnings = validate_env(env)
    for e in errors:
        print(f"[!!] {e}")
    for w in warnings:
        print(f"[??] {w}")
    mode = os.stat(env_file).st_mode & 0o777
    if os.name != "nt" and mode & 0o077:
        print(f"[??] {env_file} is readable by other users (mode {oct(mode)[2:]}); run: chmod 600 {env_file}")
    if not errors:
        print("[ok] required settings present and well-formed")
    if args.verify and not errors:
        ok, msg = verify_token(env["BOT_TOKEN"])
        print(f"[{'ok' if ok else '!!' if ok is False else '??'}] bot token: {msg}")
        errors += [] if ok is not False else ["token rejected"]
    url = env.get("DATABASE_URL")
    if url and not errors and not args.no_db:
        if db_kind(url) in ("postgres", "mongo") or url.startswith("sqlite:////data/"):
            print("[??] database lives inside Docker containers; not tested from here")
        else:
            ok, msg = check_database(url)
            print(f"[{'ok' if ok else '!!' if ok is False else '??'}] database: {msg}")
            if ok is False:
                errors.append("database")
    return 1 if errors else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="vpsbot deployment setup wizard", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--target", choices=TARGETS, help="where the bot will run")
    p.add_argument("--db", choices=DBS, help="storage: sqlite, bundled postgres/mongo (docker only), or external URL")
    p.add_argument("--database-url", help="URL for --db external")
    p.add_argument("--api-id")
    p.add_argument("--api-hash")
    p.add_argument("--bot-token")
    p.add_argument("--allowed-users", help="comma-separated numeric Telegram user ids")
    p.add_argument("--env-file", default=os.path.join(HERE, ".env"), help="where to write/read the settings (default: .env next to deploy.py)")
    p.add_argument("--from-env", action="store_true", help="also read API_ID, BOT_TOKEN, ... from the process environment")
    p.add_argument("--yes", "-y", action="store_true", help="no questions: use flags, the existing .env and defaults")
    p.add_argument("--dry-run", action="store_true", help="show what would be written/run, change nothing")
    p.add_argument("--run", action="store_true", help="execute the deployment steps without asking")
    p.add_argument("--verify", action="store_true", help="check the bot token with Telegram and test the database")
    p.add_argument("--rotate-key", action="store_true", help="generate a NEW SECRET_KEY (saved servers become unreadable)")
    p.add_argument("--check", action="store_true", help="validate the existing .env and exit")
    p.add_argument("--no-db", action="store_true", help="with --check: skip the database connection test")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return cmd_check(args) if args.check else cmd_setup(args)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was changed after the last confirmation.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
