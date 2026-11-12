"""Telegram Mini App backend: the bot's HTTP port serves the web UI (webapp/) plus a JSON API for it.

Authentication is passwordless: Telegram signs every request. The web client sends the WebApp initData
it received when the app opened (header X-Tg-Init-Data); we verify the HMAC with the bot token (the
algorithm from Telegram's Mini App docs), check the account is not banned, enforce the optional
FORCE_SUB channel, and require the user id to be in ALLOWED_USERS. No cookies, no sessions, no
secrets stored. On top of that every user is rate-limited (WEB_RATE requests per minute).

Layout of this module:
  check_init_data()   the signature check
  rate_ok()           per-user token bucket
  web_gate()          the force-subscribe gate
  configure()         wires the running bot's objects in (called from bot.main before the port opens)
  start()             the asyncio HTTP server (also answers /health and /healthz for platform checks)
  h_*()               one function per API route (POST /api/<name>); admin/* needs ADMINS / DEV_ID

Uploads from the app travel in chunks to a staging directory (resumable on flaky mobile links), then a
job is queued with src "web:<staged file>" and the existing queue engine streams it to the server with
the same retries, resume and duplicate policies as Telegram uploads. Staged files of failed jobs are
kept for /retry; everything else is removed when its job ends (or after a day untouched).

New env vars: WEBAPP_URL (public HTTPS URL of this port, for the /app button and the menu button),
WEB_MAX_MB (staging cap for app uploads, default 4096, 0 = unlimited), WEB_RATE (API requests per
minute per user, default 90), ADMINS / DEV_ID (who may use the admin panel), DEV_CHANNEL (developer
credit), FORCE_SUB (channel the users must join first).

The source link and developer credit (REPO_URL, DEV_ID, DEV_CHANNEL) are served to the UI so forks
keep pointing at the project - please leave them in place.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import secrets
import shlex
import shutil
import tempfile
import time
from urllib.parse import unquote, urlparse

import store
import subscriptions
import hosting
from perf import describe as describe_perf
from remote import Counting, Remote, fingerprint, load_pkey
from util import clean_name, clean_path, env_int, human

log = logging.getLogger("webapi")

MAX_AGE = 24 * 3600          # initData older than a day is treated as stale
MAX_JSON = 1024 * 1024       # request body cap for regular API routes
CHUNK = 4 * 1024 * 1024      # upload chunk size the client uses
GET_ATTEMPTS = 3
STALE_STAGE = 24 * 3600      # staged uploads not touched for a day are removed
RATE = env_int("WEB_RATE", 90, lo=10)  # API requests per minute per user (token bucket)

APP = None                    # pyrogram client (set by configure)
ENGINE = None                 # jobs.Engine
BOT_TOKEN = ""
ALLOWED = set()
ADMINS = set()                # Telegram ids allowed on the admin routes (bot.main passes DEV_ID in too)
WEBAPP_URL = ""
WEB_MAX = env_int("WEB_MAX_MB", 4096, lo=0) * 1024 * 1024  # 0 = no cap
STAGE_DIR = os.path.join(tempfile.gettempdir(), "vpsbot-webuploads")
UPLOADS = {}                  # token -> {uid, name, size, ts} for uploads being chunked in
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
STATIC = {"app.css": "text/css; charset=utf-8", "app.js": "application/javascript; charset=utf-8",
          "logo.png": "image/png"}

# Credit block served to the UI: forks keep the link, so people can find the source and the developer.
REPO_URL = "https://github.com/Exon101x/Tg-vps-bot"
DEV_ID = env_int("DEV_ID", 0)
DEV_CHANNEL = os.getenv("DEV_CHANNEL", "").strip().lstrip("@")


def credits():
    """The footer block the UI shows on every tab."""
    return {"repo": REPO_URL, "dev_id": DEV_ID, "dev_channel": DEV_CHANNEL}


class ApiError(Exception):
    """An error with an HTTP status; the message is shown to the user as-is."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- authentication
def check_init_data(init_data, token, max_age=MAX_AGE):
    """Verify Telegram's WebApp initData signature. Returns the parsed field dict (with 'user' still
    a JSON string) on success, None otherwise. Constant-time compare, freshness enforced."""
    try:
        pairs = {}
        for part in (init_data or "").split("&"):
            k, _, v = part.partition("=")
            pairs[unquote(k)] = unquote(v)
        received = pairs.pop("hash", "")
        if not received:
            return None
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if int(pairs.get("auth_date", "0")) < time.time() - max_age:
            return None
        return pairs
    except Exception:
        return None


def auth(headers):
    """Authenticate one API request. Returns the user dict from initData (id, first_name, username)."""
    pairs = check_init_data(headers.get("x-tg-init-data", ""), BOT_TOKEN)
    if not pairs:
        raise ApiError("sign-in failed: open the app from Telegram (/app)", 401)
    try:
        user = json.loads(pairs.get("user", "{}"))
        user["id"] = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise ApiError("no Telegram user in this request", 401)
    if ALLOWED and user["id"] not in ALLOWED:  # the same gate as the chat: id must be in ALLOWED_USERS
        raise ApiError("this Telegram account is not on the bot's ALLOWED_USERS list", 403)
    if store.is_banned(user["id"]):  # the admin panel's ban switch, enforced in chat and app alike
        raise ApiError("this account is banned from this bot", 403)
    user["username"] = user.get("username") or ""
    return user


# ---------------------------------------------------------------- rate limiting
_bucks = {}  # uid -> [tokens, last refill] one token bucket per user


def rate_ok(uid):
    """Token bucket: RATE requests per minute per user (plus the burst the bucket holds). Keeps a
    runaway client - or a replayed initData - from hammering SSH consoles and the database."""
    now = time.monotonic()
    b = _bucks.get(uid)
    if not b or now - b[1] > 60:
        if len(_bucks) > 4096:  # a long-lived process: forget buckets that ran dry long ago
            for k in [k for k, v in _bucks.items() if now - v[1] > 300]:
                _bucks.pop(k, None)
        b = _bucks.setdefault(uid, [float(RATE), now])
    b[0] = min(RATE, b[0] + (now - b[1]) * RATE / 60.0)  # refill to the cap
    b[1] = now
    if b[0] < 1:
        return False
    b[0] -= 1
    return True


async def web_gate(user):
    """Force-subscribe gate for the API: None when the user may proceed, else a JSON 403 body the
    UI turns into a 'join the channel' screen. Admins and the developer are exempt."""
    import bot  # late import: the gate shares the chat-side check and its membership cache
    if not bot.FORCE_SUB or user["id"] in bot.ADMINS:
        return None
    if await bot.sub_ok(user["id"]):
        return None
    return jdump({"error": f"join {bot.FORCE_SUB_LABEL} to use the bot, then reopen the app",
                  "need_join": True, "channel": bot.FORCE_SUB_LABEL, "url": bot.FORCE_SUB_URL}, 403)


# ---------------------------------------------------------------- helpers
def jdump(obj, status=200):
    body = json.dumps(obj, default=str).encode()
    head = f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\nContent-Type: application/json\r\n" \
           f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n" \
           f"Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
    return head.encode() + body


def active_or_named(uid, name=None):
    c = store.get_server(uid, name)
    if not c:
        raise ApiError("no server selected. Add one in the Servers tab first.", 404)
    return c


def remote_do(c, fn):
    """Run fn(Remote) against the server in a worker thread (the bot's per-command pattern)."""

    async def run():
        def work():
            with Remote(c) as r:
                return fn(r)

        return await asyncio.to_thread(work)

    return run()


class WebUser:
    """A minimal stand-in for a pyrogram user, so the shared folder-template code can be reused."""

    def __init__(self, user):
        self.id, self.username = user["id"], user.get("username", "")


def web_dest(c, user, name, explicit=None):
    """Where an app upload lands: an explicit destination if given, else the server's folder
    template, else the server's current directory. Same sanitising as the chat path."""
    import bot  # late import: shares the chat path's logic without a circular import at load time
    if explicit and clean_path(explicit):
        return bot.caption_dest(explicit, name)
    dest = bot.render_dest(c, WebUser(user), name, bot.group_from_name(name))
    return posixpath.join(c.get("cwd") or "", dest) if not dest.startswith("/") else dest


def sweep_staged():
    """Drop staged uploads nobody has touched for a day (orphans after crashes, abandoned retries)."""
    try:
        os.makedirs(STAGE_DIR, exist_ok=True)
        now = time.time()
        for f in os.listdir(STAGE_DIR):
            p = os.path.join(STAGE_DIR, f)
            if os.path.isfile(p) and now - os.path.getmtime(p) > STALE_STAGE:
                os.remove(p)
    except OSError:
        pass


def stage_path(token):
    return os.path.join(STAGE_DIR, token + ".part")


def int_arg(data, key, default=0):
    try:
        return int(data.get(key) or default)
    except (TypeError, ValueError):
        return default


def staging_stats():
    """(number of staged files, bytes) in the upload staging directory (admin overview)."""
    files = bytes_ = 0
    try:
        for f in os.listdir(STAGE_DIR):
            p = os.path.join(STAGE_DIR, f)
            if os.path.isfile(p):
                files += 1
                bytes_ += os.path.getsize(p)
    except OSError:
        pass
    return files, bytes_


# ---------------------------------------------------------------- API routes
async def h_me(user, data):
    uid = user["id"]
    store.touch_user(uid, user.get("first_name") or "", user.get("username") or "")
    return {"user": {"id": uid, "name": user.get("first_name") or user.get("username") or str(uid)},
            "servers": store.list_servers(uid), "active": store.active_name(uid),
            "ask": store.kv_get(f"ask:{uid}") == "1",
            "max_mb": WEB_MAX // (1024 * 1024) if WEB_MAX else 0,
            "admin": uid in ADMINS, "credit": credits()}


async def h_add(user, data):
    import bot
    name, target = (data.get("name") or "").strip(), (data.get("target") or "").strip()
    if not target:
        raise ApiError("target is required, e.g. ssh://root@203.0.113.5 or ftp://user:pass@host:21")
    try:
        t = bot.parse_target(target)
    except ValueError as e:
        raise ApiError(str(e))
    name = clean_name(name or t["host"]) or t["host"]
    if not bot.NAME_RE.fullmatch(name):
        raise ApiError("name: letters, digits, . _ - (max 24)")
    if not store.get_server(user["id"], name):  # replacing an existing server never needs a new slot
        reached, p = subscriptions.server_limit_reached(user["id"])
        if reached:
            raise ApiError(f"the {p.name} plan holds {p.max_servers} servers max - delete one first, or pick a bigger plan "
                           "in the Hosting tab", 402)
    key = (data.get("key") or "").strip()
    if t["scheme"] == "ssh" and t["secret"] is None:
        if key:
            try:
                load_pkey(key)
            except ValueError as e:
                raise ApiError(f"private key: {e}")
            kind, secret = "key", key
        elif data.get("password"):
            kind, secret = "password", data["password"]
        else:
            raise ApiError("SSH needs a password or a private key")
    else:
        kind, secret = "password", t["secret"] or ""
    c = dict(uid=user["id"], name=name, scheme=t["scheme"], host=t["host"], port=t["port"], user=t["user"],
             auth=kind, secret=secret, cwd=t["cwd"], hostkey="")

    def test():
        with Remote(c) as r:
            return r.probe()

    try:
        c["cwd"] = await asyncio.to_thread(test)
    except Exception as e:
        raise ApiError(f"connection test failed: {type(e).__name__}: {e}")
    store.add_server(c)
    fp = fingerprint(c["hostkey"]) if c["hostkey"] else ""
    store.log(user["id"], name, f"web: added {c['scheme']}://{c['user']}@{c['host']}:{c['port']}")
    return {"name": name, "hostkey": fp, "cwd": c["cwd"]}


async def h_use(user, data):
    name = clean_name(data.get("name") or "")
    if not store.set_active(user["id"], name):
        raise ApiError("no such server", 404)
    return {"active": name}


async def h_del(user, data):
    name = clean_name(data.get("name") or "")
    if not store.delete_server(user["id"], name):
        raise ApiError("no such server", 404)
    store.log(user["id"], name, "web: deleted")
    return {"deleted": name}


async def h_set(user, data):
    fields = {}
    if "folder" in data:
        fields["folder"] = clean_path(data["folder"]) if data["folder"] else ""
    if "dup" in data:
        if data["dup"] not in ("overwrite", "skip", "rename"):
            raise ApiError("dup must be overwrite, skip or rename")
        fields["dup"] = data["dup"]
    if "monitor" in data:
        fields["monitor"] = 1 if data["monitor"] else 0
    if not fields:
        raise ApiError("nothing to set")
    c = active_or_named(user["id"], clean_name(data.get("name") or "") or None)
    store.update(user["id"], c["name"], **fields)
    store.log(user["id"], c["name"], f"web: set {', '.join(fields)}")
    return {"server": c["name"], **fields}


async def h_ls(user, data):
    c = active_or_named(user["id"], clean_name(data.get("name") or "") or None)
    path = clean_path(data.get("path") or "")
    try:
        rows = await remote_do(c, lambda r: r.ls(path))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    store.log(user["id"], c["name"], f"web: ls {path or c['cwd'] or '/'}")
    return {"cwd": c.get("cwd") or "", "path": path, "rows": [{"n": n, "d": d, "s": s} for n, d, s in rows]}


async def h_cd(user, data):
    c = active_or_named(user["id"])
    path = (data.get("path") or "").strip()
    if not path:
        raise ApiError("path is required (~ for the home directory)")
    try:
        new = await remote_do(c, lambda r: r.cd(path))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    store.update(user["id"], c["name"], cwd=new)
    return {"cwd": new}


async def h_mkdir(user, data):
    c = active_or_named(user["id"])
    path = clean_path(data.get("path") or "")
    if not path:
        raise ApiError("path is required")
    await remote_do(c, lambda r: r.makedirs(path))
    store.log(user["id"], c["name"], f"web: mkdir {path}")
    return {"made": path}


async def h_rm(user, data):
    c = active_or_named(user["id"])
    path = clean_path(data.get("path") or "")
    if not path:
        raise ApiError("path is required")
    if not data.get("force"):
        return {"confirm": path}  # the UI asks; the confirmed request comes back with force
    try:
        await remote_do(c, lambda r: r.remove(path))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    store.log(user["id"], c["name"], f"web: rm {path}")
    return {"removed": path}


async def h_mv(user, data):
    c = active_or_named(user["id"])
    src, dst = clean_path(data.get("src") or ""), clean_path(data.get("dst") or "")
    if not src or not dst:
        raise ApiError("src and dst are required")
    try:
        await remote_do(c, lambda r: r.rename(src, dst))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    store.log(user["id"], c["name"], f"web: mv {src} -> {dst}")
    return {"moved": src}


async def h_term(user, data):
    import bot
    c = active_or_named(user["id"])
    if c["scheme"] != "ssh":
        raise ApiError("the console needs an SSH server")
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        raise ApiError("cmd is required")
    if not data.get("force") and bot.DANGEROUS.search(cmd):
        return {"confirm": cmd}  # same guard as /sh: the UI asks, the user confirms
    store.log(user["id"], c["name"], f"web: sh: {cmd[:120]}")
    try:
        out, rc, cwd = await remote_do(c, lambda r: r.shell(cmd, c["cwd"], 300))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    if cwd and cwd != c["cwd"]:
        store.update(user["id"], c["name"], cwd=cwd)
    return {"out": out[-20000:], "rc": rc, "cwd": cwd or c["cwd"]}


async def h_stats(user, data):
    import bot
    c = active_or_named(user["id"])
    if c["scheme"] != "ssh":
        raise ApiError("stats need an SSH server")
    try:
        out, _ = await remote_do(c, lambda r: r.exec(bot.STATS_CMD, 30))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    return bot.parse_kv(out)


async def h_view(user, data):
    import bot
    key = data.get("key") or ""
    if key not in bot.VIEWS:
        raise ApiError("unknown view")
    c = active_or_named(user["id"])
    if c["scheme"] != "ssh":
        raise ApiError("this view needs an SSH server")
    title, cmd = bot.VIEWS[key]
    cmd = cmd.replace("SUDO ", bot.sudo_prefix(c))
    store.log(user["id"], c["name"], f"web: {title}")
    try:
        out, rc = await remote_do(c, lambda r: r.exec(cmd, 90))
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}")
    return {"title": title, "out": out[-60000:], "rc": rc}


async def h_send(user, data):
    """Send a server file to the user's Telegram chat (big files arrive as .001 .002 volumes)."""
    c = active_or_named(user["id"])
    path = clean_path(data.get("path") or "")
    if not path:
        raise ApiError("path is required")
    vol = int_arg(data, "vol")
    asyncio.create_task(send_to_chat(user["id"], c["name"], path, vol))
    return {"queued": path, "note": "the file will arrive in your Telegram chat"}


async def send_to_chat(uid, name, path, vol_mb):
    """The web variant of /get: volumes are staged one at a time and sent to the user's chat."""
    from largefiles import choose_volume, plan_volumes, volume_name

    async def fail(e):
        log.warning("web send %s failed: %s", path, e)
        try:
            await APP.send_message(uid, f"web: {path} failed: {e}")
        except Exception:
            pass

    def pull(c, off, length, local):
        def run():
            with open(local, "wb") as f, Remote(c) as r:
                r.read_range(path, off, length, Counting(f, lambda n: None))

        return run  # run via asyncio.to_thread below

    try:
        c = store.get_server(uid, name)
        if not c:
            return fail("server deleted")
        size = await remote_do(c, lambda r: r.size(path))
        if not size:
            return await APP.send_message(uid, f"web: {path} is empty or missing")
        import bot
        vol = choose_volume(bot.TG_MAX, shutil.disk_usage(tempfile.gettempdir()).free, vol_mb)
        plan = plan_volumes(size, vol)
        base = clean_name(path) or "file"
        store.log(uid, name, f"web: get {path} ({len(plan)} volume{'s' if len(plan) > 1 else ''})")
        for i, off, length in plan:
            label = base if len(plan) == 1 else volume_name(base, i)
            for attempt in range(1, GET_ATTEMPTS + 1):
                with tempfile.TemporaryDirectory() as tmp:
                    local = os.path.join(tmp, label)
                    try:
                        await asyncio.to_thread(pull(c, off, length, local))
                        await APP.send_document(uid, local, file_name=label)
                        break
                    except Exception as e:
                        if attempt == GET_ATTEMPTS:
                            await fail(e)
                        else:
                            await asyncio.sleep(3)
        if len(plan) > 1:
            await APP.send_message(uid, f"To reassemble on an SSH server: /join {base}")
    except Exception as e:
        await fail(e)


async def h_fetch(user, data):
    url = (data.get("url") or "").strip()
    if not re.match(r"https?://", url):
        raise ApiError("url must be http(s)")
    name = clean_name(data.get("name") or "") or clean_name(unquote(urlparse(url).path)) or "download"
    c = active_or_named(user["id"])
    store.log(user["id"], c["name"], f"web: fetch {url[:150]} -> {name}")
    if c["scheme"] == "ssh":  # the server downloads it itself; nothing flows through the bot
        q = shlex.quote
        cmd = (f"(command -v curl >/dev/null && curl -fLsS --retry 2 --max-time 3600 -o {q(name)} {q(url)}) "
               f"|| wget -q -O {q(name)} {q(url)}; ls -lh {q(name)}")
        asyncio.create_task(_fetch_ssh(user["id"], c["name"], cmd))
        return {"queued": name, "note": f"{c['name']} is downloading {name} itself"}
    jid = store.add_job(user["id"], c["name"], user["id"], "http:" + url, name, name, 0)
    ENGINE.enqueue(jid)
    return {"queued": name, "note": "queued; see the Queue tab"}


async def _fetch_ssh(uid, name, cmd):
    try:
        c = store.get_server(uid, name)
        await remote_do(c, lambda r: r.shell(cmd, c["cwd"], 3700))
    except Exception as e:
        log.warning("web fetch failed: %s", e)
        try:
            await APP.send_message(uid, f"web fetch failed: {e}")
        except Exception:
            pass


# ---- chunked, resumable uploads: phone -> bot (staged) -> server (the queue)
async def h_up_init(user, data):
    name = clean_name(data.get("name") or "")
    size = int_arg(data, "size")
    if not name or size <= 0:
        raise ApiError("name and size are required")
    if WEB_MAX and size > WEB_MAX:
        raise ApiError(f"{human(size)} is over the WEB_MAX_MB cap of {human(WEB_MAX)}")
    sweep_staged()
    token = secrets.token_urlsafe(18)
    os.makedirs(STAGE_DIR, exist_ok=True)
    with open(stage_path(token), "wb"):
        pass
    UPLOADS[token] = {"uid": user["id"], "name": name, "size": size, "ts": time.time()}
    return {"token": token, "chunk": CHUNK}


async def h_up_chunk(user, data, ctx):
    token = ctx["headers"].get("x-up-token", "")
    up = UPLOADS.get(token)
    if not up or up["uid"] != user["id"]:
        raise ApiError("unknown upload token", 404)
    try:
        offset = int(ctx["headers"].get("x-up-offset", ""))
    except ValueError:
        raise ApiError("X-Up-Offset must be the byte offset of this chunk")
    if time.time() - up["ts"] > STALE_STAGE:
        UPLOADS.pop(token, None)
        raise ApiError("upload session expired", 410)
    body = ctx["body"]
    p = stage_path(token)
    have = os.path.getsize(p) if os.path.exists(p) else 0
    if offset > have:  # the client may not skip ahead
        raise ApiError(f"offset {offset} is past what was received ({have})")
    if have - offset + len(body) > up["size"]:
        raise ApiError("this chunk would push the upload past its declared size")
    with open(p, "r+b" if offset else "wb") as f:  # truncate to offset then append: retries are idempotent
        f.truncate(offset)
        f.seek(0, os.SEEK_END)
        f.write(body)
    up["ts"] = time.time()
    return {"got": os.path.getsize(p), "size": up["size"]}


async def h_up_status(user, data):
    token = data.get("token") or ""
    up = UPLOADS.get(token)
    if not up or up["uid"] != user["id"]:
        raise ApiError("unknown upload token", 404)
    p = stage_path(token)
    return {"got": os.path.getsize(p) if os.path.exists(p) else 0, "size": up["size"], "name": up["name"]}


async def h_up_finish(user, data):
    token = data.get("token") or ""
    up = UPLOADS.get(token)
    if not up or up["uid"] != user["id"]:
        raise ApiError("unknown upload token", 404)
    p = stage_path(token)
    got = os.path.getsize(p) if os.path.exists(p) else 0
    if got != up["size"]:
        raise ApiError(f"incomplete upload: {human(got)} of {human(up['size'])}; send the remaining chunks first")
    c = active_or_named(user["id"], clean_name(data.get("name") or "") or None)
    dest = web_dest(c, user, up["name"], data.get("dest"))
    jid = store.add_job(user["id"], c["name"], user["id"], "web:" + p, up["name"], dest, up["size"])
    store.log(user["id"], c["name"], f"web: upload {dest}")
    UPLOADS.pop(token, None)
    ENGINE.enqueue(jid)
    return {"id": jid, "dest": dest, "note": "queued; see the Queue tab"}


async def h_jobs(user, data):
    rows = []
    for j in store.list_jobs(user["id"], limit=25):
        rows.append({k: j.get(k) for k in ("id", "name", "dest", "server", "state", "attempts", "size", "error")})
    return {"jobs": rows}


async def h_jobs_action(user, data):
    jid, op = int_arg(data, "id"), (data.get("op") or "").lower()
    j = store.get_job(jid)
    if not j or j["uid"] != user["id"]:
        raise ApiError("no such job", 404)
    if op == "retry":
        if not await ENGINE.retry(jid):
            raise ApiError("only failed or stopped jobs can be retried")
    elif op == "stop":
        await ENGINE.stop(jid)
    else:
        raise ApiError("op must be retry or stop")
    return {"done": op}


async def h_audit(user, data):
    rows = store.recent_log(user["id"], 30)
    return {"rows": [{"ts": r["ts"], "server": r["server"], "action": r["action"]} for r in rows]}


# ---------------------------------------------------------------- plans & VPS hosting
def _order_json(o):
    p = subscriptions.plan(o["plan"])
    return {"id": o["id"], "uid": o["uid"], "plan": o["plan"], "name": p.name if p else o["plan"],
            "price": o["cents"] / 100, "currency": o["currency"], "status": o["status"],
            "ref": o["ref"], "created": o["created"]}


async def h_plans(user, data):
    """The Hosting tab: plan catalog, the user's current plan and orders."""
    uid = user["id"]
    cur = subscriptions.effective(uid)
    row = store.sub_get(uid)
    days = max(0, (row["expires"] - int(time.time())) // 86400) if row and row["expires"] > time.time() else 0
    return {
        "plans": [subscriptions.plan_json(p, current=p.key == cur.key) for p in subscriptions.plans()],
        "current": subscriptions.plan_json(cur, current=True), "days_left": days,
        "servers": len(store.list_servers(uid)), "slots_used": store.alloc_active_count(uid),
        "orders": [_order_json(o) for o in store.order_list(uid, 10)],
        "host_node": hosting.configured(),
    }


async def h_plan_order(user, data):
    """Open an order for a plan (the Buy button)."""
    p = subscriptions.plan((data.get("plan") or "").strip())
    if not p or p.free:
        raise ApiError("pick a paid plan")
    o, _fresh = subscriptions.order_create(user["id"], p)
    return {"order": _order_json(o), "payment_info": subscriptions.payment_info(p)}


async def h_plan_paid(user, data):
    """The buyer confirms the payment with a reference; the admins get the decision buttons."""
    import bot
    o, error = subscriptions.order_submit_ref(user["id"], (data.get("ref") or "").strip())
    if error or not o:
        raise ApiError(error or "no pending order")
    p = subscriptions.plan(o["plan"])
    known = store.user(user["id"]) or {}
    who = ("@" + known["username"]) if known.get("username") else (known.get("name") or str(user["id"]))
    kb = bot.InlineKeyboardMarkup([[bot.Btn("✅ Approve", callback_data=f"ord:{o['id']}:a"),
                                    bot.Btn("❌ Decline", callback_data=f"ord:{o['id']}:r")]])
    await bot.notify_admins(f"🧾 Order #{o['id']} needs a decision\n"
                            f"👤 {who} ({user['id']})\n💠 {p.name} · {p.price:g} {subscriptions.CURRENCY} / {p.days}d\n"
                            f"🧾 ref: {o['ref']} (from the Mini App)", kb)
    return {"order": _order_json(store.order_get(o["id"]))}


async def h_vps(user, data):
    """The buyer's VPS instances - credentials included, shown to the owner only."""
    uid = user["id"]
    return {
        "vps": [{"id": a["id"], "user": a["user"], "host": a["host"], "port": a["port"],
                 "password": a["secret"], "cpu": a["cpu"], "ram": a["ram"], "disk": a["disk"],
                 "status": a["status"], "expires": a["expires"], "plan": a["plan"]}
                for a in store.alloc_list(uid)],
        "slots": subscriptions.effective(uid).vps_slots,
    }


def need_admin(user):
    """Admin routes (ADMINS / DEV_ID only)."""
    if user["id"] not in ADMINS:
        raise ApiError("admins only (set ADMINS or DEV_ID to your Telegram id)", 403)


async def h_admin_overview(user, data):
    import bot
    need_admin(user)
    now = int(time.time())
    users_, banned = store.user_counts()
    servers, monitored = store.server_counts()
    stage_files, stage_bytes = staging_stats()
    return {
        "users": {"total": users_, "banned": banned, "allowed_list": len(ALLOWED)},
        "servers": {"total": servers, "monitored": monitored},
        "jobs": store.jobs_by_state(),
        "done": {"total": list(store.global_jobs(("done",))),
                 "day": list(store.global_jobs(("done",), since=now - 86400)),
                 "week": list(store.global_jobs(("done",), since=now - 7 * 86400))},
        "staging": {"files": stage_files, "bytes": stage_bytes, "cap": WEB_MAX},
        "bot": {"uptime": int(now - bot.STARTED), "database": store.LABEL, "instance": bot.SESSION_SOURCE,
                "force_sub": bot.FORCE_SUB_LABEL or "off", "webapp_url": WEBAPP_URL or "off",
                "perf": describe_perf()},
        "top": [{"uid": u, "uploads": n, "bytes": b} for u, n, b in store.top_users(10)],
        "subs": subscriptions.stats(),
    }


async def h_admin_users(user, data):
    need_admin(user)
    top = {u: (n, b) for u, n, b in store.top_users(1000)}
    rows = []
    for r in store.users(int_arg(data, "limit", 100) or 100):
        n, b = top.get(r["uid"], (0, 0))
        rows.append({"uid": r["uid"], "name": r.get("name") or "", "username": r.get("username") or "",
                     "joined": r.get("joined") or 0, "last_seen": r.get("last_seen") or 0,
                     "banned": bool(r.get("banned")), "uploads": n, "bytes": b,
                     "servers": len(store.list_servers(r["uid"]))})
    return {"users": rows}


async def h_admin_user(user, data):
    need_admin(user)
    uid = int_arg(data, "uid")
    if not uid:
        raise ApiError("uid is required")
    r = store.user(uid)
    if not r:
        raise ApiError("no such user (they have not used the bot yet)", 404)
    return {"user": {"uid": r["uid"], "name": r.get("name") or "", "username": r.get("username") or "",
                     "joined": r.get("joined") or 0, "last_seen": r.get("last_seen") or 0,
                     "banned": bool(r.get("banned"))},
            "servers": store.list_servers(uid), "usage": store.usage(uid),
            "audit": store.recent_log(uid, 15)}


async def h_admin_ban(user, data):
    need_admin(user)
    uid = int_arg(data, "uid")
    if not uid:
        raise ApiError("uid is required")
    if uid in ADMINS:
        raise ApiError("cannot ban an admin")
    flag = bool(data.get("flag"))
    store.ban_user(uid, flag)
    store.log(user["id"], "admin", f"web: {'banned' if flag else 'unbanned'} user {uid}")
    return {"uid": uid, "banned": flag}


async def h_admin_broadcast(user, data):
    need_admin(user)
    import bot
    text = (data.get("text") or "").strip()
    if not text:
        raise ApiError("text is required")
    if len(text) > 3500:
        raise ApiError("message too long (3500 characters max)")
    ids = store.user_ids()
    asyncio.create_task(bot.broadcast(text, user["id"]))
    store.log(user["id"], "admin", f"web: broadcast to {len(ids)} users")
    return {"queued": len(ids), "note": "sending now; the report arrives in your chat"}


async def h_admin_orders(user, data):
    need_admin(user)
    return {"orders": [_order_json(o) for o in store.orders_pending(20)]}


async def h_admin_order(user, data):
    """Approve or decline an order from the Mini App - the same code path as the chat buttons."""
    need_admin(user)
    import bot
    oid = int_arg(data, "id")
    action = (data.get("action") or "").strip().lower()
    if not oid or action not in ("approve", "reject"):
        raise ApiError("id and action (approve|reject) are required")
    o, error = await bot.approve_order(oid, user["id"], action == "approve")
    if error:
        raise ApiError(error, 404 if "no such order" in error else 400)
    return {"order": _order_json(o), "action": action}


ROUTES = {
    "me": h_me, "servers/add": h_add, "servers/use": h_use, "servers/del": h_del, "servers/set": h_set,
    "ls": h_ls, "cd": h_cd, "mkdir": h_mkdir, "rm": h_rm, "mv": h_mv,
    "term": h_term, "stats": h_stats, "view": h_view, "send": h_send, "fetch": h_fetch,
    "up/init": h_up_init, "up/chunk": h_up_chunk, "up/status": h_up_status, "up/finish": h_up_finish,
    "jobs": h_jobs, "jobs/action": h_jobs_action, "audit": h_audit,
    "plans": h_plans, "plans/order": h_plan_order, "plans/paid": h_plan_paid, "vps": h_vps,
    "admin/overview": h_admin_overview, "admin/users": h_admin_users, "admin/user": h_admin_user,
    "admin/ban": h_admin_ban, "admin/broadcast": h_admin_broadcast,
    "admin/orders": h_admin_orders, "admin/order": h_admin_order,
}


# ---------------------------------------------------------------- HTTP plumbing
async def read_body(reader, length, cap):
    if length < 0 or (cap and length > cap):
        raise ApiError(f"body too large (max {human(cap) if cap else 'unlimited'})", 413)
    out = bytearray()
    while len(out) < length:
        piece = await asyncio.wait_for(reader.read(min(65536, length - len(out))), 600)
        if not piece:
            break
        out += piece
    return bytes(out)


def static_response(name):
    if name not in (*STATIC, "index.html"):  # fixed whitelist: no traversal, no listing
        return b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nX-Content-Type-Options: nosniff\r\n" \
               b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    try:
        with open(os.path.join(UI_DIR, name), "rb") as f:
            body = f.read()
    except OSError:
        return b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nX-Content-Type-Options: nosniff\r\n" \
               b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    ctype = b"text/html; charset=utf-8" if name == "index.html" else STATIC[name].encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype + b"\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
            + b"Content-Security-Policy: default-src 'self'; script-src 'self' https://telegram.org; "
              b"style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; "
              b"frame-ancestors https://web.telegram.org https://*.telegram.org; form-action 'none'\r\n"
            + b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n" + body)


async def api_response(headers, body):
    """Authenticate, gate, dispatch, answer. Errors are JSON with a message the UI shows as-is."""
    try:
        route = headers["__path"][len("/api/"):]
        fn = ROUTES.get(route)
        if not fn:
            raise ApiError("no such route", 404)
        user = auth(headers)
        if not rate_ok(user["id"]):
            raise ApiError("too many requests - wait a moment and try again", 429)
        blocked = await web_gate(user)  # FORCE_SUB: the UI shows a 'join the channel' screen
        if blocked is not None:
            return blocked
        if route == "up/chunk":
            data = {}
        else:
            try:
                data = json.loads(body.decode() or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError("request body must be JSON")
        result = await fn(user, data, ctx={"headers": headers, "body": body}) if route == "up/chunk" \
            else await fn(user, data)
        return jdump(result)
    except ApiError as e:
        return jdump({"error": str(e)}, e.status)
    except Exception as e:  # a bug in a handler must not take the bot down
        log.exception("webapi route crashed")
        return jdump({"error": f"{type(e).__name__}: {e}"}, 500)


async def handle(reader, writer):
    try:
        parts = (await asyncio.wait_for(reader.readline(), 15)).decode(errors="replace").split()
        if len(parts) < 2:
            raise ValueError("bad request line")
        method, path = parts[0].upper(), parts[1].split("?")[0]
        headers = {"__path": path}
        while True:
            h = await asyncio.wait_for(reader.readline(), 15)
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode(errors="replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        clen = int(headers.get("content-length", "0") or "0")
        known = path == "/app" or path.startswith("/static/") or path.startswith("/api/")
        if not known:  # /health, /healthz and anything else: the same answer the one-line server gave
            writer.write(jdump({"status": "ok"}))
        elif path == "/app":
            writer.write(static_response("index.html"))
        elif path.startswith("/static/"):
            writer.write(static_response(path[len("/static/"):]))
        elif path.startswith("/api/") and method != "POST":
            writer.write(jdump({"error": "POST only"}, 405))
        else:
            cap = (CHUNK + 65536) if path == "/api/up/chunk" else MAX_JSON
            body = await read_body(reader, clen, cap) if clen else b""
            writer.write(await api_response(headers, body))
        await writer.drain()
    except Exception:
        pass  # a half request (health prober) or a dropped socket: nothing to do
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start(port, bind=None):
    """Serve the mini app and the API on the bot's HTTP port. Called from bot.main instead of the old
    one-line health endpoint; /health and unknown paths still answer 200 for platform checks."""
    bind = bind or os.getenv("HEALTH_BIND") or "0.0.0.0"
    return await asyncio.start_server(handle, bind, port)


def configure(app, engine, token, allowed, webapp_url="", admins=()):
    """Called from bot.main before the port opens. Kept as one function so tests can wire stubs in."""
    global APP, ENGINE, BOT_TOKEN, ALLOWED, WEBAPP_URL, ADMINS
    APP, ENGINE, BOT_TOKEN, ALLOWED, WEBAPP_URL = app, engine, token, set(allowed), (webapp_url or "")
    ADMINS = set(admins)
    sweep_staged()
