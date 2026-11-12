"""
Telegram bot: file transfer (FTP / FTPS / SFTP) + VPS management over SSH.

Env vars (see env.example):
  API_ID, API_HASH, BOT_TOKEN, SECRET_KEY, ALLOWED_USERS
  optional: MONITOR_INTERVAL (s, default 300), DISK_ALERT (%, 90), MEM_ALERT (%, 90), MAX_FETCH_MB (2048)
  admin / community: ADMINS, DEV_ID (admin panel + force-sub exemptions), DEV_CHANNEL (credit),
  FORCE_SUB (channel users must join first), WEB_RATE (Mini App requests per minute)

Source: https://github.com/Exon101x/Tg-vps-bot - fork it, run your own, keep the link so
others can find this. Developer credit: DEV_CHANNEL.
"""
import asyncio
import html
import io
import logging
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

load_dotenv()  # a local .env file can configure everything; real environment variables win

from pyrogram import Client, ContinuePropagation, filters, idle  # noqa: E402
from pyrogram.enums import ParseMode
from pyrogram.errors import AuthKeyDuplicated, Unauthorized
from pyrogram.types import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo

import session_sync
import store
import subscriptions
import hosting
import payments
import webapi
from jobs import Engine, is_permanent
from largefiles import choose_volume, join_script, order_volumes, plan_volumes, volume_name
from perf import describe as describe_perf
from remote import Counting, Remote, fingerprint, load_pkey
from util import clean_name, clean_path, env_int, human

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

try:
    API_ID = int(os.environ["API_ID"])
except KeyError:
    raise
except ValueError:
    raise SystemExit("API_ID must be the numeric api_id from my.telegram.org (got " + repr(os.environ["API_ID"]) + ")")
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = set()
for _uid in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(","):
    if not _uid:
        continue
    try:
        ALLOWED.add(int(_uid))
    except ValueError:
        logging.warning("Ignoring non-numeric ALLOWED_USERS entry %r", _uid)
if not ALLOWED:
    raise SystemExit("Set ALLOWED_USERS. The bot stores credentials and can run commands on your servers, so it must not be public.")

# ---- admin / community (all optional)
DEV_ID = env_int("DEV_ID", 0)                     # the developer's Telegram id: admin + exempt from FORCE_SUB
ADMINS = set()
for _uid in os.getenv("ADMINS", "").replace(" ", "").split(","):
    if not _uid:
        continue
    try:
        ADMINS.add(int(_uid))
    except ValueError:
        logging.warning("Ignoring non-numeric ADMINS entry %r", _uid)
if DEV_ID:
    ADMINS.add(DEV_ID)
DEV_CHANNEL = os.getenv("DEV_CHANNEL", "").strip().lstrip("@")   # shown as the developer credit
REPO_URL = "https://github.com/Exon101x/Tg-vps-bot"               # kept in the UI for people who fork
FORCE_SUB = os.getenv("FORCE_SUB", "").strip()                   # channel the users must join to use the bot
if FORCE_SUB.startswith("https://t.me/"):
    FORCE_SUB = FORCE_SUB.rsplit("/", 1)[-1].strip("/")           # a full link works too
FORCE_SUB = FORCE_SUB.lstrip("@")
FORCE_SUB_LABEL = ("@" + FORCE_SUB) if FORCE_SUB and not FORCE_SUB.startswith("-") else FORCE_SUB
FORCE_SUB_URL = f"https://t.me/{FORCE_SUB}" if FORCE_SUB and not FORCE_SUB.startswith("-") else ""

MONITOR_INTERVAL = env_int("MONITOR_INTERVAL", 300, lo=30)
DISK_ALERT = env_int("DISK_ALERT", 90, lo=1, hi=100)
MEM_ALERT = env_int("MEM_ALERT", 90, lo=1, hi=100)
MAX_FETCH = env_int("MAX_FETCH_MB", 0, lo=0) * 1024 * 1024  # 0 = no limit for /fetch downloads
TG_MAX = env_int("TG_MAX_MB", 1900, lo=1) * 1024 * 1024  # largest single file /get sends; Telegram allows 2 GB for bots
TG_IN_MAX = 4 * 1024 ** 3  # the biggest file Telegram itself can carry (Premium accounts upload up to 4 GB): 2-4 GB files stream directly, no volumes needed
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")  # public HTTPS address of the mini app (the /app button and the menu button)

DEFAULT_PORTS = {"ftp": 21, "ftps": 21, "ssh": 22}
NAME_RE = re.compile(r"[\w.-]{1,24}")
UNIT_RE = re.compile(r"(?!-)[A-Za-z0-9@._:-]{1,64}")
SVC_ACTIONS = {"status", "start", "stop", "restart", "reload", "enable", "disable"}
NO_SERVER = "No server selected. Add one with /add (see /help)."
SEM = asyncio.Semaphore(2)  # simultaneous /get downloads (uploads go through the job queue)

PENDING_AUTH = {}  # uid -> server being added, waiting for password / key
PENDING = {}       # token -> (uid, created, async action) for confirm buttons
TERM = {}          # uid -> last activity (terminal mode)
COLLECT = {}       # uid -> volumes being collected by /multi
COLLECT_IDLE = 7200
TERM_IDLE = 600
PICK = {}          # uid -> {picker message id: (chat_id, msg_id, name, size, cap_dest, group, ts)} for /ask
PICK_IDLE = 1800   # unanswered server choices fall out of memory after this long

# Guard against fat-fingering. This is NOT a security boundary - the user already has a shell.
DANGEROUS = re.compile(
    r"\brm\s+(-\S*\s+)*-\S*[rf]|\bmkfs|\bdd\s+.*\bof=|\b(shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b"
    r"|\b(chmod|chown)\s+(-\S+\s+)*-\S*R|:\(\)\s*\{|>\s*/dev/(sd|nvme|vd)|\b(fdisk|parted|wipefs)\b"
    r"|\bsystemctl\s+(stop|disable|mask)\b|\bufw\s+(disable|reset)\b|\biptables\s+(-\S+\s+)*-F|\buserdel\b|(^|[;&|]\s*)(sudo\s+)?passwd\b"
    r"|\bkill\s+-9|\b(pkill|killall)\b",
    re.I,
)

SESSION_SOURCE = "starting"  # set in main(): local file, restored from the database, or fresh login
STARTED = time.time()
app = Client(session_sync.NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=session_sync.DIR,
             parse_mode=ParseMode.DISABLED)
engine = Engine(app, MAX_FETCH)
auth = filters.private & filters.user(list(ALLOWED))
MEDIA = filters.document | filters.video | filters.audio | filters.photo | filters.voice | filters.animation | filters.video_note

HELP = """🖥 VPS + file transfer bot

SERVERS
/add <name> <target> - save a server (becomes active)
   ssh://user@host[:port]  (asks for password or key file)
   ssh://user:pass@host · ftp://user:pass@host[:port][/dir] · ftps://...
/servers - list and switch · /use <name> · /del <name>
/status - active server info · /monitor on|off - alerts · /audit - action log · /about - bot status

VPS (ssh servers)
/vps - dashboard buttons · /stats · /ps · /ports · /logins
/sh <cmd> - run a command (dir is remembered) · /term - terminal mode
/service <name> [status|start|stop|restart|reload|enable|disable]
/logs <unit> [lines] · /docker [ps|start|stop|restart|logs <name>]
/disk · /firewall · /failed · /updates · /upgrade (apt) · /reboot
Non-root users need passwordless sudo for service/docker/reboot actions.

FILES (all servers)
Send any file: it streams straight to the server (no temp disk), is queued per server,
retried on failures and resumed from the partial file. Albums and bulk sends are fine.
Files up to 4 GB stream directly (the sender needs Telegram Premium above 2 GB).
Caption "> path/name.ext" (or "> dir/") sets the destination.
/ask on|off - when on, every file you send gets buttons to pick the destination server
/queue - jobs · /retry [id] · /stop <id|all> · /usage - upload totals
/folder <template> - auto folders, e.g. {user}/{date} or {type}/{year}-{month}
/ondup overwrite|skip|rename - what to do if the file already exists
/pwd · /cd <dir> · /ls [dir] · /mkdir <dir> · /mv <from> <to> · /rm <path>
/get <path> [-v MB] - send a server file to this chat; over 2 GB it is split into .001 .002 volumes
/join <name> [--rm] - (ssh) reassemble name.001 name.002 ... on the server
/multi [name] ... /done - collect the volumes of a file over 4 GB (or without Premium) and upload them as ONE file to any server
/perf - detected CPU/RAM and the speed profile in use
/fetch <url> [name] - the server downloads a URL (ssh) or the bot streams it (ftp); no size limit
/app - the Mini App: manage servers, browse and upload files, a shell console and
VPS stats from a web UI inside Telegram (needs WEBAPP_URL, the bot's public HTTPS address)
Files over 4 GB (or over 2 GB without Premium): split them first (7-Zip volumes, or `split -b 1900M -d -a 3 --numeric-suffixes=1 file file.`),
then /multi, send all the .001 .002 ... parts, and /done: one file lands on any FTP/FTPS/SFTP server.

PLANS & VPS HOSTING (buy your own VPS - host your own bots on it)
/plans - the plans: more servers, or a VPS of your own to host bots 24/7
/mysub - your plan, limits and expiry · /myvps - your VPS instances
/paid <reference> - confirm a payment (the UPI/PayPal/crypto reference)
/hosting - the 60-second guide: your bot, running on your VPS, tonight
UPI (checkout page, QR, any UPI app) and crypto (BTC, USDT, 100+ coins) are accepted
when the owner set up the gateways: the Buy button takes real payments and the plan
activates the second the money lands - no waiting, no /paid needed. Otherwise the
reference flow above still works.
VPS plans include SSH instances with a disk quota - buy, get credentials in
seconds, add the VPS back as a server (/add myvps ssh://user@host) and manage
it right from this chat (files, shell, services, the Mini App console).

MINI APP (/app)
A web UI served by the bot itself on its health port. Sign-in is automatic: Telegram signs
the request and the bot checks the signature, so there is no extra password. Browse files, upload
from your phone (resumable, queued like any other upload), run shell commands, watch VPS stats -
and the buttons appear once WEBAPP_URL points at the bot's public HTTPS address (PaaS platforms
provide one automatically; on a plain VPS put nginx or Caddy in front for HTTPS).

ADMIN (ADMINS / DEV_ID only)
/admin - panel: overview, users and usage, orders and revenue, ban / unban, broadcast
/admin orders - approve or decline payments (the Mini App has the same)
The Mini App has the same panel as a tab (/app), with live cards per user.

SOURCE
{REPO_URL} - fork it and run your own; please keep the link in place.
{DEV_LINE}"""
HELP = HELP.replace("{REPO_URL}", REPO_URL)
HELP = HELP.replace("{DEV_LINE}", f"Developer: @{DEV_CHANNEL}" if DEV_CHANNEL else "")


# ---------------------------------------------------------------- helpers
def err(e):
    return f"❌ {type(e).__name__}: {e}" if str(e) else f"❌ {type(e).__name__}"


def arg_text(m):
    parts = (m.text or m.caption or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


GROUP = {"photo": "images", "video": "videos", "animation": "videos", "video_note": "videos", "audio": "audio", "voice": "audio"}


def media_group(m):
    kind = m.media.value
    mime = getattr(getattr(m, kind), "mime_type", "") or ""
    for prefix, group in (("image/", "images"), ("video/", "videos"), ("audio/", "audio")):
        if mime.startswith(prefix):
            return group
    return GROUP.get(kind, "files")


def caption_dest(raw, name):
    """Parse the "> destination" of a caption: an explicit path, or a folder that gets the file name appended.
    Same segment-wise sanitising as the folder template: no ../, no absolute paths, no CRLF."""
    raw = raw.strip()
    if not raw:
        return name
    raw = re.sub(r"[\x00-\x1f]", "", raw.splitlines()[0])
    wanted_dir = raw.rstrip("/").rstrip("\\") != raw or not raw.strip("/\\")
    dest = clean_path(raw)
    if not dest:
        return name
    return dest + "/" + name if wanted_dir else dest


def size_verdict(size):
    """None when a file of this size can be uploaded directly, otherwise the reason it can't."""
    if size and size > TG_IN_MAX:
        return (f"{human(size)} is past Telegram's absolute ceiling of 4 GB per file, so forwarding cannot carry it either. "
                "Split it (7-Zip volumes, or `split -b 1900M -d -a 3 --numeric-suffixes=1 file file.`) and use /multi + /done.")
    return None


def group_from_name(name):
    mime = mimetypes.guess_type(name)[0] or ""
    return {"image": "images", "video": "videos", "audio": "audio"}.get(mime.split("/")[0], "files")


def render_dest(c, user, name, group):
    """Apply the server's folder template (/folder) to get the destination path of a file."""
    tpl = (c.get("folder") or "").strip()
    if not tpl:
        return name
    now = datetime.now(timezone.utc)
    ext = os.path.splitext(name)[1].lstrip(".").lower() or "bin"
    who = clean_name(user.username or "") or str(user.id)
    try:
        path = tpl.format(user=who, uid=user.id, date=now.strftime("%Y-%m-%d"), year=now.year,
                          month=f"{now.month:02d}", day=f"{now.day:02d}", type=group, ext=ext)
    except (KeyError, IndexError, ValueError):
        return name
    parts = [p for p in (clean_name(x) for x in path.split("/")) if p]
    return ("/" if path.startswith("/") else "") + "/".join(parts + [name])


EXT = {"photo": "jpg", "voice": "ogg", "video_note": "mp4", "video": "mp4", "audio": "mp3", "animation": "mp4"}


def media_name(m):
    media = m.document or m.video or m.audio or m.animation or m.voice or m.video_note or m.photo
    name = clean_name(getattr(media, "file_name", None) or "")
    if not name:
        kind = m.media.value
        name = f"{kind}_{m.id}.{EXT.get(kind, 'bin')}"
    return name


def sudo_prefix(c):
    return "" if c["user"] == "root" else "sudo -n "


def lines_arg(s, default=50):
    try:
        return max(1, min(int(s), 500))
    except (TypeError, ValueError):
        return default


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def tlen(s):
    """How long Telegram thinks s is: the message cap counts UTF-16 code units - an
    emoji (astral char) costs 2. len(s) undercounts emoji-heavy texts."""
    return len(s.encode("utf-16-le")) // 2


MAX_TEXT = 4000  # Telegram's hard cap is 4096 units per message; keep headroom


def split_text(text, limit=MAX_TEXT):
    """Split any text into chunks that each fit one Telegram message, breaking at line
    boundaries when possible (a single line longer than the limit is hard-cut)."""
    out, start = [], 0
    while start < len(text):
        units, hard = 0, start
        for i in range(start, len(text)):  # how far can this chunk reach?
            units += 2 if ord(text[i]) > 0xFFFF else 1
            if units > limit:
                break
            hard = i + 1
        if hard >= len(text):
            out.append(text[start:])
            break
        cut = text.rfind("\n", start, hard)  # prefer to break at a line boundary
        if cut <= start:
            cut = hard
        out.append(text[start:cut].rstrip("\n"))
        start = cut
        while start < len(text) and text[start] == "\n":
            start += 1
    return out


async def reply_long(msg, text, **kw):
    """Reply with any text, however long: chunks go out as consecutive messages (the
    keyboard, if any, rides the first). Without this, any text past Telegram's 4096-unit
    cap crashes the handler with MESSAGE_TOO_LONG - /start grew into exactly that."""
    parts = split_text(text)
    for i, part in enumerate(parts):
        await (msg.reply(part, **kw) if not i else msg.chat.send_message(part))


async def send_output(msg, text, title=""):
    text = ANSI.sub("", text).strip() or "(no output)"
    if len(text) <= 3400:
        head = f"<b>{html.escape(title)}</b>\n" if title else ""
        await msg.reply(f"{head}<pre>{html.escape(text)}</pre>", parse_mode=ParseMode.HTML)
    else:
        bio = io.BytesIO(text.encode())
        bio.name = "output.txt"
        await msg.reply_document(bio, caption=title[:200] or None)


async def need(uid, msg, ssh=False):
    c = store.get_server(uid)
    if not c:
        await msg.reply(NO_SERVER)
        return None
    if ssh and c["scheme"] != "ssh":
        await msg.reply(f"'{c['name']}' is an FTP server and this needs SSH. Add one with /add or switch with /servers.")
        return None
    return c


def ask_mode(uid):
    """Per-user setting: when on, every file sent to the bot gets buttons to pick the destination server."""
    return store.kv_get(f"ask:{uid}") == "1"


def pick_keyboard(servers):
    rows = [[Btn(f"📤 {name}", callback_data=f"pick:{name}")] for name in servers]
    rows.append([Btn("✖ Cancel", callback_data="pickc:1")])
    return InlineKeyboardMarkup(rows)


async def queue_upload(uid, chat_id, msg_id, c, name, dest, size, status):
    """The tail the direct path and the /ask picker share: create the job and hand it to the worker."""
    jid = store.add_job(uid, c["name"], chat_id, f"tg:{chat_id}:{msg_id}", name, dest, size)
    store.set_job(jid, status_id=status.id)
    store.log(uid, c["name"], f"upload {dest}")
    ahead = store.count_active(uid, c["name"])
    note = "\n📦 over 2 GB — streaming directly (Premium upload)" if size > 2 * 1024 ** 3 else ""
    try:
        await status.edit_text(f"#{jid} ⏳ {name} → {c['name']}:{dest}" + (f"\n{ahead} ahead in the queue" if ahead else "") + note)
    except Exception:
        pass
    engine.enqueue(jid)


async def remote(c, fn):
    def run():
        with Remote(c) as r:
            return fn(r)

    return await asyncio.to_thread(run)


async def confirm(msg, uid, label, action):
    now = time.time()
    for t in [t for t, v in PENDING.items() if now - v[1] > 300]:
        PENDING.pop(t, None)
    token = secrets.token_hex(4)
    PENDING[token] = (uid, now, action)
    kb = InlineKeyboardMarkup([[Btn("✅ Confirm", callback_data=f"ok:{token}"), Btn("✖ Cancel", callback_data=f"no:{token}")]])
    await msg.reply(f"⚠️ {label}", reply_markup=kb)


def make_progress(status, label, name):
    last = [0.0]

    async def cb(cur, total):
        if time.time() - last[0] < 3 and cur < total:
            return
        last[0] = time.time()
        bar = f"{'█' * (cur * 10 // total)}{'░' * (10 - cur * 10 // total)} {cur * 100 // total}%  ({human(cur)}/{human(total)})" if total else human(cur)
        try:
            await status.edit_text(f"{label}: {name}\n{bar}")
        except Exception:
            pass

    return cb


def make_tick(status, label, name, loop):
    """Progress callback usable from worker threads."""
    cb = make_progress(status, label, name)
    last = [0.0]

    def tick(cur, total):
        if time.time() - last[0] >= 3:
            last[0] = time.time()
            asyncio.run_coroutine_threadsafe(cb(cur, total), loop)

    return tick


# ---------------------------------------------------------------- VPS helpers
STATS_CMD = r'''
echo "host=$(hostname)"
echo "os=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "uptime=$(uptime -p 2>/dev/null)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "cores=$(nproc)"
free -m | awk '/^Mem:/{print "mem_used="$3; print "mem_total="$2} /^Swap:/{print "swap_used="$3; print "swap_total="$2}'
df -Pk / | awk 'NR==2{print "disk_pct="$5; print "disk_used="$3; print "disk_total="$2}'
'''

VIEWS = {
    "ps": ("Top processes", "ps aux --sort=-%cpu | head -n 12 | cut -c1-160"),
    "ports": ("Listening ports", "SUDO ss -tulpn | head -n 40"),
    "logins": ("Recent logins", "last -n 10 -a 2>/dev/null | head -n 12; echo; who"),
    "disk": ("Disk usage", r"""df -hT -x tmpfs -x devtmpfs -x squashfs 2>/dev/null; echo; echo '--- largest top-level directories ---'; SUDO timeout 45 du -xh --max-depth=1 / 2>/dev/null | sort -rh | head -n 12"""),
    "firewall": ("Firewall", r"""if command -v ufw >/dev/null; then SUDO ufw status verbose 2>&1; else SUDO iptables -S 2>&1 | head -n 30; fi"""),
    "failed": ("Failed services", "systemctl --failed --no-pager --plain 2>&1 | head -n 30"),
    "docker": ("Docker containers", r"""SUDO docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1"""),
    "updates": ("Upgradable packages", r'''if command -v apt >/dev/null; then apt list --upgradable 2>/dev/null | tail -n +2 | awk '{n++; if(n<=40)print} END{print n+0" package(s) upgradable"}'; else echo "apt not found (Debian/Ubuntu only)"; fi'''),
}


def parse_kv(text):
    d = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def fmt_stats(d):
    try:
        mem_pct = int(d["mem_used"]) * 100 // int(d["mem_total"])
        lines = [
            f"🖥 {d.get('host', '?')}  {d.get('os', '')}",
            f"⏱ {d.get('uptime', '?')}",
            f"⚙️ load {d.get('load', '?')}  ({d.get('cores', '?')} cores)",
            f"🧠 RAM {d['mem_used']}/{d['mem_total']} MB ({mem_pct}%)",
            f"💾 disk {int(d['disk_used']) / 1048576:.1f}/{int(d['disk_total']) / 1048576:.1f} GB ({d['disk_pct']})",
        ]
        if d.get("swap_total", "0") != "0":
            lines.append(f"🔁 swap {d['swap_used']}/{d['swap_total']} MB")
        return "\n".join(lines)
    except (KeyError, ValueError, ZeroDivisionError):
        return "Could not parse stats:\n" + "\n".join(f"{k}={v}" for k, v in d.items())


def stats_issues(d):
    issues = {}
    try:
        if int(d["disk_pct"].rstrip("%")) >= DISK_ALERT:
            issues["disk"] = f"disk {d['disk_pct']} full"
        mem = int(d["mem_used"]) * 100 // int(d["mem_total"])
        if mem >= MEM_ALERT:
            issues["mem"] = f"memory at {mem}%"
        load1 = float(d["load"].split()[0])
        if load1 > 2 * int(d["cores"]):
            issues["load"] = f"load {load1} on {d['cores']} cores"
    except (KeyError, ValueError, ZeroDivisionError):
        pass
    return issues


async def run_cmd(msg, uid, c, title, cmd, timeout=60):
    cmd = cmd.replace("SUDO ", sudo_prefix(c))
    store.log(uid, c["name"], title)
    try:
        out, rc = await remote(c, lambda r: r.exec(cmd, timeout))
    except Exception as e:
        return await msg.reply(err(e))
    await send_output(msg, out + (f"\n[exit {rc}]" if rc else ""), f"{c['name']} - {title}")


async def run_named(msg, uid, name, title, cmd, timeout=60):
    c = store.get_server(uid, name)  # re-fetch so no credentials sit inside confirm closures
    if c:
        await run_cmd(msg, uid, c, title, cmd, timeout)


async def run_shell(msg, uid, name, cmd, force=False):
    if not cmd:
        return
    if not force and DANGEROUS.search(cmd):
        return await confirm(msg, uid, f"Run on {name}?\n\n{cmd}", lambda mm: run_shell(mm, uid, name, cmd, True))
    c = store.get_server(uid, name)
    if not c or c["scheme"] != "ssh":
        return await msg.reply("Server not found.")
    store.log(uid, name, f"sh: {cmd}")
    try:
        out, rc, cwd = await remote(c, lambda r: r.shell(cmd, c["cwd"]))
    except Exception as e:
        return await msg.reply(err(e))
    if cwd and cwd != c["cwd"]:
        store.update(uid, name, cwd=cwd)
    await send_output(msg, out + (f"\n[exit {rc}]" if rc else ""), f"{c['user']}@{c['host']}:{cwd or c['cwd']}$ {cmd[:80]}")


async def do_stats(msg, uid):
    c = await need(uid, msg, ssh=True)
    if not c:
        return
    try:
        out, _ = await remote(c, lambda r: r.exec(STATS_CMD, 30))
    except Exception as e:
        return await msg.reply(err(e))
    await msg.reply(fmt_stats(parse_kv(out)))


async def do_view(msg, uid, key):
    c = await need(uid, msg, ssh=True)
    if c:
        await run_cmd(msg, uid, c, *VIEWS[key])


async def do_reboot(msg, uid):
    c = await need(uid, msg, ssh=True)
    if c:
        name = c["name"]
        await confirm(msg, uid, f"Reboot {name} ({c['host']})?", lambda mm: reboot_now(mm, uid, name))


async def reboot_now(msg, uid, name):
    c = store.get_server(uid, name)
    if not c:
        return
    store.log(uid, name, "reboot")
    try:
        await remote(c, lambda r: r.exec(f"nohup sh -c 'sleep 2; {sudo_prefix(c)}reboot' >/dev/null 2>&1 &", 10))
    except Exception as e:
        return await msg.reply(err(e))
    await msg.reply(f"🔄 Reboot command sent to {name}. Give it a minute, then try /stats.")


def menu_kb():
    return InlineKeyboardMarkup([
        [Btn("📊 Stats", callback_data="q:stats"), Btn("⚙️ Processes", callback_data="q:ps")],
        [Btn("🐳 Docker", callback_data="q:docker"), Btn("🔌 Ports", callback_data="q:ports")],
        [Btn("🔐 Logins", callback_data="q:logins"), Btn("📦 Updates", callback_data="q:updates")],
        [Btn("💾 Disk", callback_data="q:disk"), Btn("🛡 Firewall", callback_data="q:firewall"), Btn("🧯 Failed", callback_data="q:failed")],
        [Btn("🔄 Reboot", callback_data="q:reboot")],
    ])


# ---------------------------------------------------------------- gates: force-subscribe + banned users
SUB_OK = {}       # uid -> (joined, ts) cached membership answers (5 min)
SUB_WARNED = 0.0  # last log line about a broken FORCE_SUB setup (once an hour, not per request)


async def sub_ok(uid):
    """Force-subscribe gate. True when FORCE_SUB is off, the user joined the channel, or the user is
    exempt (admins / the developer). Answers are cached for 5 minutes so the API limits stay safe.
    A misconfigured channel (bot not added as admin) fails OPEN with a hourly warning - the gate
    must protect the channel, not brick the bot."""
    if not FORCE_SUB or uid in ADMINS:
        return True
    now = time.time()
    hit = SUB_OK.get(uid)
    if hit and now - hit[1] < 300:
        return hit[0]
    joined = False
    try:
        m = await app.get_chat_member(FORCE_SUB, uid)
        status = getattr(getattr(m, "status", None), "value", "")
        joined = status in ("owner", "administrator", "member") or (status == "restricted" and getattr(m, "is_member", False))
    except Exception as e:
        if "participant" in type(e).__name__.lower():  # the user is simply not in the channel
            joined = False
        else:  # the bot cannot see the channel (not an admin there?), a flood wait, ...
            global SUB_WARNED
            joined = True
            if now - SUB_WARNED > 3600:
                SUB_WARNED = now
                logging.warning("FORCE_SUB membership check failed (%s: %s); allowing requests. "
                                "Add the bot as ADMIN in the channel %s so it can check members.",
                                type(e).__name__, e, FORCE_SUB_LABEL)
    SUB_OK[uid] = (joined, now)
    return joined


def join_kb():
    rows = [[Btn("📢 Join the channel", url=FORCE_SUB_URL)]] if FORCE_SUB_URL else []
    rows.append([Btn("✅ I joined - recheck", callback_data="subchk:1")])
    return InlineKeyboardMarkup(rows)


@app.on_message(auth, group=-1)
async def gate(_, m):
    """First in line for every private message from an allowed user (group -1 runs before all the
    group-0 handlers): banned users are dropped, and with FORCE_SUB set, users who have not joined
    the channel get a join card instead of the command they sent."""
    uid = m.from_user.id
    if store.is_banned(uid):
        return await m.reply("⛔ You are banned from this bot.")
    if not await sub_ok(uid):
        return await m.reply(f"🔒 To use this bot, join {FORCE_SUB_LABEL} first - then tap recheck.",
                             reply_markup=join_kb())
    raise ContinuePropagation  # all checks passed: the normal handlers take over


# ---------------------------------------------------------------- general commands
@app.on_message(auth & filters.command(["start", "help"]))
async def start(_, m):
    store.touch_user(m.from_user.id, m.from_user.first_name or "", m.from_user.username or "")
    await reply_long(m, HELP)


@app.on_message(auth & filters.command("app"))
async def app_cmd(_, m):
    """Open the Mini App: the web UI the bot serves on its own HTTP port."""
    if WEBAPP_URL.startswith("https://"):
        await m.reply("📱 vpsbot Mini App: your servers, files, a shell console and VPS stats, right inside Telegram.",
                      reply_markup=InlineKeyboardMarkup([[Btn("📱 Open the app", web_app=WebAppInfo(url=WEBAPP_URL))]]))
    else:
        await m.reply(
            "The Mini App is served by the bot itself, but Telegram only opens web apps from an HTTPS address.\n"
            "Set WEBAPP_URL to the bot's public HTTPS URL (Render, Heroku, Railway and friends give you one "
            "automatically - the same address as the health port; on a plain VPS put nginx or Caddy in front) "
            "and then /app will have the button.\nUntil then, everything works from this chat.")


def parse_target(arg):
    if "://" not in arg:
        arg = "ssh://" + arg
    u = urlparse(arg)
    scheme = {"sftp": "ssh"}.get(u.scheme, u.scheme)
    if scheme not in DEFAULT_PORTS or not u.hostname:
        raise ValueError("Use ssh://user@host[:port], ftp://user:pass@host or ftps://user:pass@host")
    try:
        port = u.port or DEFAULT_PORTS[scheme]
    except ValueError:
        raise ValueError("Invalid port")
    pwd = unquote(u.password) if u.password is not None else None
    user = unquote(u.username) if u.username else ("root" if scheme == "ssh" else "anonymous")
    if scheme != "ssh" and pwd is None:
        pwd = ""
    path = unquote(u.path) if u.path not in ("", "/") else ""
    return dict(scheme=scheme, host=u.hostname, port=port, user=user, secret=pwd, cwd=path)


async def finish_add(msg, uid, name, t, auth_kind, secret):
    note = await msg.reply("🔌 Testing connection…")
    c = dict(uid=uid, name=name, scheme=t["scheme"], host=t["host"], port=t["port"], user=t["user"],
             auth=auth_kind, secret=secret, cwd=t["cwd"], hostkey="")

    def test():
        with Remote(c) as r:
            return r.probe()

    try:
        c["cwd"] = await asyncio.to_thread(test)
    except Exception as e:
        return await note.edit_text(err(e))
    store.add_server(c)
    store.log(uid, name, f"added {c['scheme']}://{c['user']}@{c['host']}:{c['port']}")
    fp = f"\n🔐 Host key pinned: {fingerprint(c['hostkey'])}\nCompare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server if you want to be sure." if c["hostkey"] else ""
    await note.edit_text(f"✅ Saved '{name}' and made it active.\n{c['scheme']}://{c['user']}@{c['host']}:{c['port']}\n📂 {c['cwd'] or '/'}{fp}")


@app.on_message(auth & filters.command(["add", "addvps", "setftp"]))
async def add(_, m):
    args = m.command[1:]
    usage = "Usage: /add <name> <target>\n e.g. /add vps1 ssh://root@203.0.113.5\n      /add files ftps://user:pass@host:21/uploads"
    if len(args) == 1 and "://" in args[0]:
        name, target = None, args[0]
    elif len(args) >= 2:
        name, target = args[0], args[1]
    else:
        return await m.reply(usage)
    try:
        t = parse_target(target)
    except ValueError as e:
        return await m.reply(f"❌ {e}")
    name = name or t["host"]
    if not NAME_RE.fullmatch(name):
        return await m.reply("Name: letters, digits, . _ - (max 24).")
    uid = m.from_user.id
    if not store.get_server(uid, name):  # replacing an existing server never needs a new slot
        reached, p = subscriptions.server_limit_reached(uid)
        if reached:
            return await m.reply(
                f"🧱 The {p.name} plan holds {p.max_servers} server{'s' if p.max_servers != 1 else ''} max - you are at the limit.\n"
                "Free a slot with /del - or /plans for a bigger one.",
                reply_markup=InlineKeyboardMarkup([[Btn("💎 Plans", callback_data="pl:plans")]]))
    try:
        await m.delete()  # don't leave a password in the chat
    except Exception:
        pass
    if t["scheme"] == "ssh" and t["secret"] is None:
        PENDING_AUTH[uid] = dict(name=name, ts=time.time(), **t)
        return await m.reply(
            f"🔑 Send the password for {t['user']}@{t['host']} as a message, or upload your private key file "
            "(unencrypted ed25519/RSA/ECDSA; a key made just for this bot is best). I'll delete it right away. /cancel to abort."
        )
    await finish_add(m, uid, name, t, "password", t["secret"] or "")


@app.on_message(auth & filters.command("cancel"))
async def cancel(_, m):
    PENDING_AUTH.pop(m.from_user.id, None)
    TERM.pop(m.from_user.id, None)
    COLLECT.pop(m.from_user.id, None)
    await m.reply("Cancelled.")


@app.on_message(auth & filters.command("servers"))
async def servers(_, m):
    uid = m.from_user.id
    rows = store.list_servers(uid)
    if not rows:
        return await m.reply(NO_SERVER)
    active = store.active_name(uid)
    lines = [f"{'▶️' if r['name'] == active else '•'} {r['name']} - {r['scheme']}://{r['user']}@{r['host']}:{r['port']}{' 🔔' if r['monitor'] else ''}" for r in rows]
    kb = InlineKeyboardMarkup([[Btn(r["name"], callback_data=f"use:{r['name']}")] for r in rows])
    await m.reply("\n".join(lines) + "\n\nTap to switch:", reply_markup=kb)


@app.on_message(auth & filters.command("use"))
async def use(_, m):
    name = arg_text(m)
    ok = store.set_active(m.from_user.id, name)
    await m.reply(f"▶️ Now using {name}" if ok else "No such server. See /servers.")


@app.on_message(auth & filters.command("ask"))
async def ask(_, m):
    """Per-user toggle: when on, every file sent to the bot shows buttons to pick the destination server."""
    uid, a = m.from_user.id, arg_text(m).strip().lower()
    if a in ("on", "1", "yes", "true"):
        store.kv_set(f"ask:{uid}", "1")
        n = len(store.list_servers(uid))
        if n < 2:
            await m.reply(f"📤 Ask mode on, but you have {n or 'no'} server{'s' if n == 1 else ''} saved — the buttons appear once you add a second one with /add.")
        else:
            await m.reply("📤 Ask mode on: every file you send now gets buttons under it to choose the destination server. "
                          'The folder template and the "> destination" caption of the server you pick still apply. /ask off to return to automatic.')
    elif a in ("off", "0", "no", "false"):
        store.kv_set(f"ask:{uid}", "")
        await m.reply("▶️ Ask mode off: files go to the active server again (/servers to switch, /use to pick one).")
    else:
        await m.reply(f"Ask mode is {'on 📤' if ask_mode(uid) else 'off'}.\nUsage: /ask on|off — when on, every file you send shows "
                      "buttons to pick which of your servers receives it. Tip: turn it off before bulk sends.")


@app.on_message(auth & filters.command("del"))
async def delete(_, m):
    name = arg_text(m)
    ok = store.delete_server(m.from_user.id, name)
    await m.reply(f"🗑 Deleted {name} (credentials erased)." if ok else "No such server. See /servers.")


@app.on_message(auth & filters.command("status"))
async def status_cmd(_, m):
    c = await need(m.from_user.id, m)
    if not c:
        return
    text = f"{c['name']}: {c['scheme']}://{c['user']}@{c['host']}:{c['port']}\n📂 {c['cwd'] or '/'}\nauth: {c['auth']}"
    if c["hostkey"]:
        text += f"\n🔐 {fingerprint(c['hostkey'])}"
    text += f"\n🔔 monitoring {'on' if c['monitor'] else 'off'}\n📁 folder: {c['folder'] or '(none)'}   ♻️ duplicates: {c['dup']}"
    await m.reply(text)


@app.on_message(auth & filters.command("audit"))
async def audit(_, m):
    rows = store.recent_log(m.from_user.id, 25)
    text = "\n".join(f"{time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} [{r['server']}] {r['action']}" for r in rows)
    await send_output(m, text or "(empty)", "Audit log (UTC)")


@app.on_message(auth & filters.command("about"))
async def about(_, m):
    up = int(time.time() - STARTED)
    lines = ["🤖 VPS + file transfer bot", f"uptime: {up // 3600}h {up % 3600 // 60}m", f"database: {store.LABEL}",
             f"session: {SESSION_SOURCE}", f"instance: {session_sync.INSTANCE}",
             f"active uploads: {len(store.pending_jobs())}", describe_perf(),
             f"source: {REPO_URL}"]
    if DEV_CHANNEL:
        lines.append(f"developer: @{DEV_CHANNEL}" + (f" (id {DEV_ID})" if DEV_ID else ""))
    if m.from_user.id in ADMINS:
        lines.append(f"admin: yes ({len(ADMINS)} admin(s), {store.user_counts()[0]} known user(s))")
    await m.reply("\n".join(lines))


# ---------------------------------------------------------------- plans, buying, VPS hosting
CURRENCY = subscriptions.CURRENCY


def plan_card(p, current=False):
    """One plan as a compact card: badge, price, what's in it."""
    price = "free" if p.free else f"{p.price:g} {CURRENCY} / {p.days}d"
    head = f"{'🆓' if p.free else '💠'} {p.name}" + (f" · {p.tag}" if p.tag else "") + ("  ✅ yours" if current else "")
    lim = "∞ servers" if p.max_servers < 0 else f"{p.max_servers} servers"
    if p.vps_slots:
        lim += f" · {p.vps_slots}x VPS ({p.vps_cpu} vCPU · {p.vps_ram} MB · {p.vps_disk} GB)"
    lines = [head, f"   {price}", f"   {lim}"]
    if p.desc:
        lines.append(f"   {p.desc}")
    return "\n".join(lines)


def plans_kb():
    rows = [[Btn(f"💠 {p.name} · {p.price:g} {CURRENCY}", callback_data=f"buy:{p.key}")]
            for p in subscriptions.plans(paid_only=True)]
    rows.append([Btn("💎 My plan", callback_data="pl:sub"), Btn("🚀 Host a bot", callback_data="pl:host")])
    return InlineKeyboardMarkup(rows)


def plans_text(uid):
    cur = subscriptions.effective(uid)
    lines = ["💎 Plans", ""]
    lines += [plan_card(p, current=p.key == cur.key) for p in subscriptions.plans()]
    lines += ["", "Free is automatic - no card, no sub. Paid tiers add more servers and VPSes of your own:",
              "a real Linux box with SSH and a disk quota, to host your bots on, 24/7.",
              "Tap a button to buy - approval by the owner usually takes minutes. /hosting for the guide."]
    return "\n".join(lines)


def order_text(o, p):
    return (f"🧾 Order #{o['id']} · {p.name}\n"
            f"   {p.price:g} {CURRENCY} / {p.days} days\n\n"
            f"💳 How to pay:\n{subscriptions.payment_info(p)}\n\n"
            f"When it is paid, send:\n/paid <transaction reference>\n"
            f"(order #{o['id']} is open - it is what the reference attaches to)")


def mysub_text(uid):
    p, days = subscriptions.sub_row(uid)
    n = len(store.list_servers(uid))
    slots = store.alloc_active_count(uid)
    cap = "∞" if p.max_servers < 0 else str(p.max_servers)
    lines = [f"💎 Your plan: {p.name}"]
    if p.free:
        lines += ["Free - no expiry. Upgrade any time: /plans"]
    else:
        lines += [f"✅ active · {days} day{'s' if days != 1 else ''} left "
                  f"(until {time.strftime('%Y-%m-%d', time.gmtime(store.sub_get(uid)['expires']))})",
                  "Renewing extends from where you are, and keeps your VPS alive - /plans"]
    lines += [f"🖥 servers: {n} / {cap}"]
    if p.vps_slots or slots:
        lines.append(f"🚀 VPS: {slots} active" + (f" / {p.vps_slots} included" if p.vps_slots else ""))
    return "\n".join(lines)


def vps_card(a):
    days = max(0, (a["expires"] - int(time.time())) // 86400)
    state = "🟢 active" if a["status"] == "active" and a["expires"] > time.time() else f"🔴 {a['status']}"
    lines = [f"🚀 {a['user']}@{a['host']}  ·  {state}",
             f"   SSH port {a['port']} · {a['cpu']} vCPU · {a['ram']} MB · {a['disk']} GB"
             + (f" · {days}d left" if a["status"] == "active" else ""),
             f"   user: {a['user']} · password: {a['secret']}",
             f"   included with the {subscriptions.plan(a['plan']).name if subscriptions.plan(a['plan']) else a['plan']} plan"]
    if a["status"] == "active":
        lines.append("   host a bot on it: /hosting - files go straight from this chat")
    return "\n".join(lines)


def myvps_text(uid):
    rows = store.alloc_list(uid)
    if not rows:
        return ("🚀 No VPS yet.\n\nPlans with an included VPS (real SSH instances with a disk quota): /plans\n"
                "Buy, get credentials in seconds, host your own bots on it 24/7.")
    return "🚀 Your VPS instances\n\n" + "\n\n".join(vps_card(a) for a in rows) + "\n\nRenew before expiry to keep them: /plans"


HOSTING_GUIDE = """🚀 Host your own bot on a VPS from this bot - the 60-second guide

1️⃣ /plans - pick a plan with an included VPS, tap Buy, pay, send /paid <reference>
2️⃣ Approved = credentials land here instantly (also /myvps anytime)
3️⃣ Make the VPS a server here: /add myvps ssh://USER@HOST
   (it becomes an SSH server - you never leave Telegram)
4️⃣ Send your bot's files to this chat - they stream straight onto the VPS
   (needs a folder? caption it "> mybot/" - or /cd, /mkdir first)
5️⃣ Set it up and run it:
   /sh python3 -m venv ~/venv
   /sh ~/venv/bin/pip install -r ~/mybot/requirements.txt
   /sh cd ~/mybot && nohup ~/venv/bin/python3 bot.py &
   (for auto-start on reboot: a systemd unit via /service, or ask in the channel)
6️⃣ Watch it: /stats · /ps · /logs - all from this chat

That is it - your bot, on your VPS, managed from Telegram.
No VPS yet? /plans - the VPS Starter plan has one included."""


async def notify_admins(text, kb=None):
    for a in ADMINS:
        try:
            await app.send_message(a, text, reply_markup=kb)
        except Exception:
            pass


async def approve_order(oid, admin_uid, approve):
    """Decide an order: on approve, activate the plan, extend/provision its VPS instances and
    deliver everything to the buyer. Returns (order, error_text)."""
    o, p, error = subscriptions.order_decide(oid, approve, admin_uid)
    if error or not o:
        return o, error
    if not approve:
        try:
            await app.send_message(o["uid"], f"❌ Order #{oid} ({p.name}) was declined.\n"
                                             f"Questions? The channel bio has the owner's contacts - or /plans for another plan.")
        except Exception:
            pass
        return o, None
    await _fulfill(o, p)
    return o, None


async def _fulfill(o, p):
    """Activate a just-approved order: start the plan, extend/provision VPS instances, deliver."""
    expires = subscriptions.activate(o["uid"], p)
    until = time.strftime("%Y-%m-%d", time.gmtime(expires))
    notes, creds = [], []
    if p.vps_slots > 0:
        for a in store.alloc_list(o["uid"]):  # keep the instances the buyer already has
            if a["status"] == "active":
                store.alloc_set(a["id"], expires=expires)
        left, _ = subscriptions.vps_slots_left(o["uid"])
        for _ in range(max(0, left)):
            try:
                creds.append(await asyncio.to_thread(hosting.provision, o["uid"], p, expires))
            except Exception as e:
                notes.append(f"⚠️ VPS provisioning failed: {err(e)}")
                break
    text = [f"✅ {p.name} is active - until {until} ({p.days} days).", "💳 /mysub · /myvps · servers: "
             + ("∞" if p.max_servers < 0 else str(p.max_servers))]
    for a in creds:
        text += ["", "🚀 Your VPS is ready",
                 f"Host: {a['host']} · SSH port {a['port']}",
                 f"User: {a['user']}", f"Password: {a['secret']}",
                 f"Spec: {a['cpu']} vCPU · {a['ram']} MB · {a['disk']} GB · until {until}",
                 "Host your own bot on it: /hosting (files go straight from this chat)."]
    text += notes + ["", "Renew before it ends to keep everything: /plans (it extends from where you are)."]
    try:
        await app.send_message(o["uid"], "\n".join(text))
    except Exception:
        pass


async def gateway_paid(oid, provider, ref):
    """A payment gateway webhook (UPI Gateway or CryptAPI crypto) confirmed the money: activate
    instantly. Idempotent - replays and retries can never activate twice."""
    o, error = subscriptions.order_gateway_paid(oid, provider, ref)
    if not o or error:
        return o, error
    p = subscriptions.plan(o["plan"])
    if not p or p.free:
        return o, "unknown plan"
    await _fulfill(o, p)
    try:
        await notify_admins(f"✅ Order #{oid} ({p.name}) paid via {provider} - ref {ref[:32] or 'n/a'}. "
                            "Activated automatically, no action needed.")
    except Exception:
        pass
    return o, None


async def gateway_note(oid, text):
    """One-off buyer notifications from the webhook pipeline (payment seen, short, expired...)."""
    o = store.order_get(oid)
    if not o:
        return
    try:
        await app.send_message(o["uid"], text)
    except Exception:
        pass


@app.on_message(auth & filters.command(["plans", "pricing"]))
async def plans_cmd(_, m):
    await reply_long(m, plans_text(m.from_user.id), reply_markup=plans_kb())


@app.on_message(auth & filters.command(["mysub", "mysubscription"]))
async def mysub_cmd(_, m):
    kb = InlineKeyboardMarkup([[Btn("💎 Plans", callback_data="pl:plans"), Btn("🚀 My VPS", callback_data="pl:vps")]])
    await reply_long(m, mysub_text(m.from_user.id), reply_markup=kb)


@app.on_message(auth & filters.command("myvps"))
async def myvps_cmd(_, m):
    kb = InlineKeyboardMarkup([[Btn("💎 Plans", callback_data="pl:plans")]]) \
        if not store.alloc_list(m.from_user.id) else None
    await reply_long(m, myvps_text(m.from_user.id), reply_markup=kb)


@app.on_message(auth & filters.command("paid"))
async def paid_cmd(_, m):
    uid = m.from_user.id
    o, error = subscriptions.order_submit_ref(uid, arg_text(m))
    if error or not o:
        kb = InlineKeyboardMarkup([[Btn("💎 Plans", callback_data="pl:plans")]])
        return await m.reply(f"❌ {error}", reply_markup=kb)
    p = subscriptions.plan(o["plan"])
    try:
        await m.delete()  # the reference may carry payment details: do not leave it in the chat
    except Exception:
        pass
    await m.reply(f"🧾 Order #{o['id']} · {p.name} - reference received, waiting for the owner's approval.\n"
                  f"The moment it is approved, your plan (and VPS, if the plan has one) activates automatically.")
    known = store.user(uid) or {}
    who = ("@" + known["username"]) if known.get("username") else (known.get("name") or str(uid))
    kb = InlineKeyboardMarkup([[Btn("✅ Approve", callback_data=f"ord:{o['id']}:a"),
                                Btn("❌ Decline", callback_data=f"ord:{o['id']}:r")]])
    await notify_admins(f"🧾 Order #{o['id']} needs a decision\n"
                        f"👤 {who} ({uid})\n💠 {p.name} · {p.price:g} {CURRENCY} / {p.days}d\n"
                        f"🧾 ref: {o['ref']}", kb)


@app.on_message(auth & filters.command(["hosting", "host"]))
async def hosting_cmd(_, m):
    kb = InlineKeyboardMarkup([[Btn("💎 Plans", callback_data="pl:plans")],
                               [Btn("🚀 My VPS", callback_data="pl:vps")]])
    await m.reply(HOSTING_GUIDE, reply_markup=kb)


# ---------------------------------------------------------------- admin panel (ADMINS / DEV_ID)
async def broadcast(text, admin_uid):
    """Message every known user (from the admin panel). A report lands in the admin's chat."""
    ids = store.user_ids()
    sent = failed = 0
    for uid in ids:
        try:
            await app.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20/s, under Telegram's ~30 messages/s ceiling
    try:
        await app.send_message(admin_uid, f"📣 Broadcast done: {sent} delivered, {failed} failed ({len(ids)} users).")
    except Exception:
        pass
    return sent, failed


async def _do_ban(mm, admin_uid, target, flag):
    store.ban_user(target, flag)
    store.log(admin_uid, "admin", f"{'banned' if flag else 'unbanned'} user {target}")
    await mm.reply(f"⛔ User {target} banned." if flag else f"✅ User {target} unbanned.")


def admin_overview_text():
    now = int(time.time())
    users_total, users_banned = store.user_counts()
    servers, monitored = store.server_counts()
    states = store.jobs_by_state()
    done_n, done_b = store.global_jobs(("done",))
    day_n, day_b = store.global_jobs(("done",), since=now - 86400)
    up = now - int(STARTED)
    stage_files, stage_bytes = webapi.staging_stats()
    lines = [
        "👑 Admin panel",
        f"⏱ uptime {up // 3600}h {up % 3600 // 60}m · 🗄 {store.LABEL} · session: {SESSION_SOURCE}",
        f"👥 users: {users_total} known ({users_banned} banned) · allowed list: {len(ALLOWED)}",
        f"🖥 servers: {servers} ({monitored} monitored)",
        f"📦 uploads: {states.get('done', 0)} done ({human(done_b)}) · today {day_n} ({human(day_b)})",
        f"   queued {states.get('queued', 0)} · running {states.get('running', 0)} · failed {states.get('failed', 0)}",
        f"📂 app staging: {stage_files} file(s), {human(stage_bytes)}",
        f"🔒 force-sub: {FORCE_SUB_LABEL or 'off'} · 📱 Mini App: {WEBAPP_URL or 'off'}",
    ]
    st = subscriptions.stats()
    per_plan = ", ".join(f"{subscriptions.plan(k).name if subscriptions.plan(k) else k} {v}"
                         for k, v in st["per_plan"].items() if v) or "none"
    lines += [f"💳 subs: {st['subs_active']} active ({per_plan}) · revenue {st['revenue']} · "
              f"orders: {st['orders'].get('awaiting', 0)} awaiting / {st['orders'].get('pending', 0)} pending",
              f"🚀 VPS: {st['vps_active']} active / {st['vps_total']} total · host node: "
              + ("configured" if hosting.configured() else "NOT configured (set VPS_NODE_HOST - VPS plans need it)")]
    top = store.top_users(5)
    if top:
        known = {r["uid"]: r for r in store.users(500)}
        lines.append("📈 top users:")
        for u, n, b in top:
            who = known.get(u) or {}
            name = ("@" + who["username"]) if who.get("username") else (who.get("name") or str(u))
            lines.append(f"   • {name}: {n} uploads, {human(b)}")
    lines.append("\n/admin users · /admin user <id> · /admin ban|unban <id> · /admin broadcast <text> · or the Admin tab in /app")
    return "\n".join(lines)


def admin_users_text():
    known = store.users(100)
    if not known:
        return "No known users yet - they appear when someone /start's the bot or opens the Mini App."
    top = {u: (n, b) for u, n, b in store.top_users(1000)}
    lines = [f"👥 {len(known)} known user(s)"]
    for r in known:
        who = ("@" + r["username"]) if r.get("username") else (r.get("name") or str(r["uid"]))
        n, b = top.get(r["uid"], (0, 0))
        seen = time.strftime("%m-%d %H:%M", time.gmtime(r.get("last_seen") or 0))
        lines.append(f"{'⛔' if r.get('banned') else '•'} {who} ({r['uid']}) - {n} upload(s), {human(b)}, last seen {seen}")
    lines.append("\nDetail: /admin user <id> · ban: /admin ban <id>")
    return "\n".join(lines)


def admin_user_text(uid):
    r = store.user(uid)
    if not r:
        return f"No data for {uid} yet (nobody with that id has used the bot)."
    who = ("@" + r["username"]) if r.get("username") else (r.get("name") or str(uid))
    u = store.usage(uid)
    lines = [
        f"👤 {who} ({uid})" + (" ⛔ BANNED" if r.get("banned") else ""),
        f"joined {time.strftime('%Y-%m-%d', time.gmtime(r.get('joined') or 0))} · last seen "
        f"{time.strftime('%m-%d %H:%M', time.gmtime(r.get('last_seen') or 0))}",
        f"uploads: {u['today'][0]} today ({human(u['today'][1])}) · {u['week'][0]} in 7 days ({human(u['week'][1])})",
        f"waiting/running: {u['pending']} · failed: {u['failed']}",
    ]
    servers = store.list_servers(uid)
    if servers:
        lines.append("servers: " + ", ".join(f"{s['name']} ({s['scheme']}://{s['host']})" for s in servers))
    else:
        lines.append("servers: none")
    acts = store.recent_log(uid, 8)
    if acts:
        lines.append("recent actions:\n" + "\n".join(
            f"  {time.strftime('%m-%d %H:%M', time.gmtime(a['ts']))} [{a['server']}] {a['action']}" for a in acts))
    return "\n".join(lines)


def admin_orders_text():
    rows = store.orders_pending(15)
    if not rows:
        return "🧾 No open orders. They appear here (and in your chat) the moment a buyer sends /paid."
    lines = ["🧾 Open orders (newest first)"]
    for o in rows:
        p = subscriptions.plan(o["plan"])
        known = store.user(o["uid"]) or {}
        who = ("@" + known["username"]) if known.get("username") else (known.get("name") or str(o["uid"]))
        state = "🟡 awaiting approval" if o["status"] == "awaiting" else "⚪ waiting for payment"
        lines.append(f"#{o['id']} 👤 {who} ({o['uid']}) · 💠 {p.name if p else o['plan']} · "
                     f"{o['cents'] / 100:g} {o['currency']} · {state}"
                     + (f"\n   ref: {o['ref']}" if o["ref"] else ""))
    lines.append("\nTap the buttons to decide an awaiting order - /admin orders refreshes.")
    return "\n".join(lines)


def admin_orders_kb():
    rows = [[Btn(f"✅ #{o['id']}", callback_data=f"ord:{o['id']}:a"),
             Btn(f"❌ #{o['id']}", callback_data=f"ord:{o['id']}:r")]
            for o in store.orders_pending(15) if o["status"] == "awaiting"]
    return InlineKeyboardMarkup(rows) if rows else None


@app.on_message(auth & filters.command("admin"))
async def admin_cmd(_, m):
    uid = m.from_user.id
    if uid not in ADMINS:
        return await m.reply("👑 Admins only. Set ADMINS (or DEV_ID) to your Telegram id to open the panel.")
    arg = arg_text(m).strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    if not arg:
        kb = InlineKeyboardMarkup([[Btn("📊 Refresh", callback_data="adm:overview")],
                                   [Btn("👥 Users", callback_data="adm:users")],
                                   [Btn("🧾 Orders", callback_data="adm:orders")]])
        return await reply_long(m, admin_overview_text(), reply_markup=kb)
    if head == "overview":
        return await reply_long(m, admin_overview_text())
    if head == "users":
        return await reply_long(m, admin_users_text())
    if head == "orders":
        return await reply_long(m, admin_orders_text(), reply_markup=admin_orders_kb())
    if head == "user" and rest.strip().lstrip("-").isdigit():
        return await m.reply(admin_user_text(int(rest.strip())))
    if head in ("ban", "unban") and rest.strip().lstrip("-").isdigit():
        target = int(rest.strip())
        if target in ADMINS:
            return await m.reply("That is an admin - remove them from ADMINS first.")
        if head == "ban":
            return await confirm(m, uid, f"Ban user {target}? They lose access to the chat and the Mini App.",
                                 lambda mm: _do_ban(mm, uid, target, True))
        await _do_ban(m, uid, target, False)
        return
    if head == "broadcast" and rest.strip():
        text = rest.strip()[:3500]
        n = len(store.user_ids())
        return await confirm(m, uid, f"📣 Send this to {n} user(s)?\n\n{text[:600]}",
                             lambda mm: broadcast_and_report(mm, uid, text))
    await m.reply("Usage: /admin [overview | users | user <id> | orders | ban <id> | unban <id> | broadcast <text>]")


async def broadcast_and_report(mm, uid, text):
    sent, failed = await broadcast(text, uid)
    await mm.reply(f"📣 Broadcast: {sent} delivered, {failed} failed.")


@app.on_message(auth & filters.command("monitor"))
async def monitor_cmd(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    arg = arg_text(m).lower()
    if arg not in ("on", "off"):
        return await m.reply(f"Usage: /monitor on|off  (currently {'on' if c['monitor'] else 'off'})")
    store.update(uid, c["name"], monitor=1 if arg == "on" else 0)
    if arg == "on":
        await m.reply(f"🔔 Watching {c['name']} every {MONITOR_INTERVAL // 60 or 1} min. I'll message you if it goes unreachable, disk ≥ {DISK_ALERT}%, memory ≥ {MEM_ALERT}% or load is high.")
    else:
        await m.reply(f"🔕 Monitoring off for {c['name']}.")


# ---------------------------------------------------------------- file commands
@app.on_message(auth & filters.command("pwd"))
async def pwd(_, m):
    c = await need(m.from_user.id, m)
    if c:
        await m.reply(f"📂 {c['name']}:{c['cwd'] or '/'}")


@app.on_message(auth & filters.command("cd"))
async def cd(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    target = arg_text(m)
    if not target:
        return await m.reply(f"📂 {c['cwd'] or '/'}")
    try:
        new = await remote(c, lambda r: r.cd(target))
    except Exception as e:
        return await m.reply(err(e))
    store.update(uid, c["name"], cwd=new)
    await m.reply(f"📂 {new}")


@app.on_message(auth & filters.command("ls"))
async def ls(_, m):
    c = await need(m.from_user.id, m)
    if not c:
        return
    path = arg_text(m)
    try:
        rows = await remote(c, lambda r: r.ls(path))
    except Exception as e:
        return await m.reply(err(e))
    lines = [("📁 " if d else "📄 ") + n + ("" if d else f"  ({human(s)})") for n, d, s in rows]
    await send_output(m, "\n".join(lines) or "(empty)", f"{c['name']}:{path or c['cwd'] or '/'}")


@app.on_message(auth & filters.command("mkdir"))
async def mkdir(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    path = arg_text(m)
    if not path:
        return await m.reply("Usage: /mkdir <dir>")
    try:
        await remote(c, lambda r: r.mkdir(path))
    except Exception as e:
        return await m.reply(err(e))
    store.log(uid, c["name"], f"mkdir {path}")
    await m.reply(f"📁 Created {path}")


@app.on_message(auth & filters.command("mv"))
async def mv(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    try:
        a, b = shlex.split(arg_text(m))
    except ValueError:
        return await m.reply('Usage: /mv <from> <to>  (quote paths with spaces: /mv "my file" new)')
    try:
        await remote(c, lambda r: r.rename(a, b))
    except Exception as e:
        return await m.reply(err(e))
    store.log(uid, c["name"], f"mv {a} {b}")
    await m.reply(f"✅ {a} → {b}")


@app.on_message(auth & filters.command("rm"))
async def rm(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    path = arg_text(m)
    if not c:
        return
    if not path:
        return await m.reply("Usage: /rm <file or empty dir>")
    name = c["name"]

    async def do(mm):
        cc = store.get_server(uid, name)
        store.log(uid, name, f"rm {path}")
        try:
            await remote(cc, lambda r: r.remove(path))
            await mm.reply(f"🗑 Deleted {path}")
        except Exception as e:
            await mm.reply(err(e))

    await confirm(m, uid, f"Delete {path} on {name}?", do)


GET_ATTEMPTS = 3


async def send_from_server(msg, uid, name, path, override=None, confirmed=False):
    """Send a server file to the chat. Files over Telegram's limit go out as name.001, name.002, ... volumes, staged on
    disk one volume at a time (and shrunk to fit the free disk), so even huge files need little temp space."""
    c = store.get_server(uid, name)
    if not c:
        return await msg.reply("Server not found.")
    status = await msg.reply(f"⏳ {path}")
    try:
        size = await remote(c, lambda r: r.size(path))
        if not size:
            return await status.edit_text(f"❌ {path} is empty or does not exist")
        vol = choose_volume(TG_MAX, shutil.disk_usage(tempfile.gettempdir()).free, override)
        plan = plan_volumes(size, vol)
    except Exception as e:
        return await status.edit_text(err(e))
    if len(plan) > 5 and not confirmed:
        await status.delete()
        return await confirm(msg, uid, f"{path} is {human(size)}: it will be sent as {len(plan)} volumes of up to {human(vol)}. Continue?",
                             lambda mm: send_from_server(mm, uid, name, path, override, True))
    base = clean_name(path) or "file"
    store.log(uid, name, f"get {path} ({len(plan)} volume{'s' if len(plan) > 1 else ''})")
    loop = asyncio.get_running_loop()

    async def status_edit(text):
        try:
            await status.edit_text(text)
        except Exception:  # the user deleted the progress message: not a reason to abort the remaining volumes
            pass

    async with SEM:
        for i, off, length in plan:
            label = base if len(plan) == 1 else volume_name(base, i)
            where = f" [{i + 1}/{len(plan)}]" if len(plan) > 1 else ""
            for attempt in range(1, GET_ATTEMPTS + 1):
                with tempfile.TemporaryDirectory() as tmp:
                    local = os.path.join(tmp, label)
                    tick = make_tick(status, f"⬇️ Downloading{where}", label, loop)

                    def run(local=local, off=off, length=length, tick=tick):
                        with open(local, "wb") as f, Remote(c) as r:
                            r.read_range(path, off, length, Counting(f, lambda n: tick(n, length)))

                    try:
                        await asyncio.to_thread(run)
                        await msg.reply_document(local, file_name=label, progress=make_progress(status, f"⬆️ Sending{where}", label))
                        break
                    except Exception as e:
                        logging.exception("get failed")
                        if attempt == GET_ATTEMPTS or is_permanent(e):
                            return await status_edit(f"{err(e)}\n{label}")
                        await status_edit(f"⚠️ {label}: {e}\nretrying ({attempt + 1}/{GET_ATTEMPTS})")
                        await asyncio.sleep(5)
    if len(plan) == 1:
        try:
            return await status.delete()
        except Exception:
            return
    await status_edit(f"✅ Sent {len(plan)} volumes of {base} ({human(size)})")
    await msg.reply(f"To reassemble: /join {base} on an SSH server, or on your computer: Linux/macOS `cat {base}.0* > {base}`, "
                    f"Windows `copy /b {base}.001+{base}.002+… {base}`, or open {base}.001 in 7-Zip.")


@app.on_message(auth & filters.command("get"))
async def get(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    raw = arg_text(m)
    override = None
    flag = re.search(r"\s-v\s+(\d+)\s*$", raw)
    if flag:
        override, raw = int(flag.group(1)) * 1024 * 1024, raw[:flag.start()].strip()
    if not raw:
        return await m.reply("Usage: /get <remote path> [-v <volume size in MB>]\n"
                             "Files bigger than Telegram allows are sent as .001 .002 ... volumes; reassemble with /join.")
    await send_from_server(m, uid, c["name"], raw, override)


@app.on_message(auth & filters.command("join"))
async def join_cmd(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    raw = arg_text(m)
    remove = bool(re.search(r"(^|\s)--rm\s*$", raw))
    raw = re.sub(r"(^|\s)--rm\s*$", "", raw).strip()
    if not raw:
        return await m.reply("Usage: /join <name> [--rm]\nJoins name.001, name.002, ... in the current directory into name on the "
                             "server (--rm deletes the volumes afterwards). Needs free space equal to the file size.")
    base = re.sub(r"\.\d{3}$", "", raw)
    status = await m.reply(f"⏳ Joining {base} on {c['name']}…")
    store.log(uid, c["name"], f"join {base}{' --rm' if remove else ''}")
    try:
        out, rc, _ = await remote(c, lambda r: r.shell(join_script(base, remove), c["cwd"], 7200))
    except Exception as e:
        return await status.edit_text(err(e))
    await status.delete()
    await send_output(m, out + (f"\n[exit {rc}]" if rc else ""), f"{c['name']} - join {base}")


@app.on_message(auth & filters.command("fetch"))
async def fetch(_, m):
    uid = m.from_user.id
    try:
        parts = shlex.split(arg_text(m))
    except ValueError:
        parts = []
    if not parts or not re.match(r"https?://", parts[0]):
        return await m.reply("Usage: /fetch <http(s) url> [filename]")
    c = await need(uid, m)
    if not c:
        return
    url = parts[0]
    name = clean_name(parts[1]) if len(parts) > 1 else clean_name(unquote(urlparse(url).path)) or "download"
    store.log(uid, c["name"], f"fetch {url[:150]} -> {name}")

    if c["scheme"] == "ssh":  # the VPS pulls the file itself; nothing flows through the bot
        q = shlex.quote
        cmd = (f"(command -v curl >/dev/null && curl -fLsS --retry 2 --max-time 3600 -o {q(name)} {q(url)}) "
               f"|| wget -q -O {q(name)} {q(url)}; ls -lh {q(name)}")
        status = await m.reply(f"⏳ {c['name']} is downloading {name}…")
        try:
            out, rc, _ = await remote(c, lambda r: r.shell(cmd, c["cwd"], 3700))
        except Exception as e:
            return await status.edit_text(err(e))
        return await status.edit_text(f"{'✅' if rc == 0 else '❌'} {name}\n{out.strip()[-1500:]}")

    # FTP/FTPS: stream the download straight into the server through the queue (no temp disk, resumable)
    dest = render_dest(c, m.from_user, name, group_from_name(name))
    status = await m.reply("⏳ Queued…")
    ahead = store.count_active(uid, c["name"])
    jid = store.add_job(uid, c["name"], m.chat.id, f"url:{url}", name, dest, 0)
    store.set_job(jid, status_id=status.id)
    await status.edit_text(f"#{jid} ⏳ {name} → {c['name']}:{dest}" + (f"\n{ahead} ahead in the queue" if ahead else ""))
    engine.enqueue(jid)


# ---------------------------------------------------------------- VPS commands
@app.on_message(auth & filters.command(["vps", "menu"]))
async def vps(_, m):
    c = await need(m.from_user.id, m, ssh=True)
    if c:
        await m.reply(f"🖥 {c['name']} ({c['user']}@{c['host']})", reply_markup=menu_kb())


@app.on_message(auth & filters.command("stats"))
async def stats(_, m):
    await do_stats(m, m.from_user.id)


@app.on_message(auth & filters.command(["ps", "ports", "logins", "updates", "disk", "firewall", "failed"]))
async def views(_, m):
    await do_view(m, m.from_user.id, m.command[0].lower().split("@")[0])


@app.on_message(auth & filters.command("reboot"))
async def reboot(_, m):
    await do_reboot(m, m.from_user.id)


@app.on_message(auth & filters.command("upgrade"))
async def upgrade(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    name = c["name"]
    cmd = ("if command -v apt-get >/dev/null; then "
           "SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
           "SUDO env DEBIAN_FRONTEND=noninteractive apt-get -y -qq upgrade 2>&1 | tail -n 25; "
           "else echo 'apt-get not found (Debian/Ubuntu only)'; fi")
    await confirm(m, uid, f"Run apt update + upgrade on {name}? This can take a few minutes and may restart services.",
                  lambda mm: run_named(mm, uid, name, "apt upgrade", cmd, 1200))


@app.on_message(auth & filters.command("sh"))
async def sh(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    cmd = arg_text(m)
    if not cmd:
        return await m.reply("Usage: /sh <command>   (the working directory is remembered between commands)")
    await run_shell(m, uid, c["name"], cmd)


@app.on_message(auth & filters.command("term"))
async def term(_, m):
    uid = m.from_user.id
    if uid in TERM:
        TERM.pop(uid)
        return await m.reply("💻 Terminal mode off.")
    c = await need(uid, m, ssh=True)
    if c:
        TERM[uid] = time.time()
        await m.reply(f"💻 Terminal mode on for {c['name']}: every message you send runs as a shell command. "
                      f"Ends after {TERM_IDLE // 60} idle minutes or with /term. Start a line with '$ ' if it begins with '/'.")


@app.on_message(auth & filters.command("service"))
async def service(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    a = m.command[1:]
    if not a or not UNIT_RE.fullmatch(a[0]) or (len(a) > 1 and a[1] not in SVC_ACTIONS):
        return await m.reply("Usage: /service <name> [status|start|stop|restart|reload|enable|disable]")
    unit, act = a[0], (a[1] if len(a) > 1 else "status")
    if act == "status":
        cmd = f"SUDO systemctl status {unit} --no-pager -n 12 2>&1"
    else:
        cmd = f'SUDO systemctl {act} {unit} 2>&1; echo "-> now: $(systemctl is-active {unit})"'
    title, name = f"systemctl {act} {unit}", c["name"]
    if act in ("stop", "disable"):
        warn = "\n⚠️ This can cut off your SSH access!" if unit.startswith("ssh") else ""
        return await confirm(m, uid, f"{act.capitalize()} {unit} on {name}?{warn}", lambda mm: run_named(mm, uid, name, title, cmd))
    await run_cmd(m, uid, c, title, cmd)


@app.on_message(auth & filters.command("logs"))
async def logs(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    a = m.command[1:]
    if not a or not UNIT_RE.fullmatch(a[0]):
        return await m.reply("Usage: /logs <systemd unit> [lines]")
    n = lines_arg(a[1] if len(a) > 1 else None)
    await run_cmd(m, uid, c, f"logs {a[0]}", f"SUDO journalctl -u {a[0]} -n {n} --no-pager 2>&1")


@app.on_message(auth & filters.command("docker"))
async def docker(_, m):
    uid = m.from_user.id
    c = await need(uid, m, ssh=True)
    if not c:
        return
    a = m.command[1:]
    sub = a[0] if a else "ps"
    if sub == "ps":
        return await run_cmd(m, uid, c, *VIEWS["docker"])
    if sub in ("start", "stop", "restart", "logs") and len(a) >= 2 and UNIT_RE.fullmatch(a[1]):
        ctr, name = a[1], c["name"]
        if sub == "logs":
            n = lines_arg(a[2] if len(a) > 2 else None)
            return await run_cmd(m, uid, c, f"docker logs {ctr}", f"SUDO docker logs --tail {n} {ctr} 2>&1")
        cmd, title = f"SUDO docker {sub} {ctr} 2>&1", f"docker {sub} {ctr}"
        if sub == "stop":
            return await confirm(m, uid, f"Stop container {ctr} on {name}?", lambda mm: run_named(mm, uid, name, title, cmd))
        return await run_cmd(m, uid, c, title, cmd)
    await m.reply("Usage: /docker [ps | start|stop|restart|logs <container> [lines]]")


# ---------------------------------------------------------------- uploads, text, buttons
async def handle_key(m, uid):
    p = PENDING_AUTH[uid]
    if time.time() - p["ts"] > 300:
        PENDING_AUTH.pop(uid, None)
        return await m.reply("That request expired. Run /add again.")
    if m.document.file_size > 20_000:
        return await m.reply("That doesn't look like a private key (file too large). Try again or /cancel.")
    data = await m.download(in_memory=True)
    text = data.getvalue().decode(errors="replace")
    try:
        await m.delete()
    except Exception:
        pass
    try:
        load_pkey(text)
    except ValueError as e:
        return await m.reply(f"❌ {e}\nSend another key or /cancel.")
    PENDING_AUTH.pop(uid, None)
    await finish_add(m, uid, p["name"], p, "key", text)


@app.on_message(auth & MEDIA)
async def upload(_, m):
    uid = m.from_user.id
    if uid in PENDING_AUTH and m.document:
        return await handle_key(m, uid)
    c = await need(uid, m)
    if not c:
        return
    col = COLLECT.get(uid)
    if col and time.time() - col["ts"] > COLLECT_IDLE:
        COLLECT.pop(uid)
        col = None
        await m.reply("The /multi collection expired, so this file is uploaded on its own.")
    if col:
        return await collect_part(m, col)
    name = media_name(m)
    cap = (m.caption or "").strip()
    cap_dest = caption_dest(cap[1:], name) if cap.startswith(">") and cap[1:].strip() else None
    group = media_group(m)
    size = getattr(getattr(m, m.media.value), "file_size", 0) or 0
    bad = size_verdict(size)
    if bad:
        return await m.reply(f"⚠️ {name} can't be uploaded directly.\n{bad}", quote=True)
    if ask_mode(uid):  # /ask on: the user picks the destination server under each file
        servers = [r["name"] for r in store.list_servers(uid)]
        if len(servers) > 1:
            mine = PICK.setdefault(uid, {})
            now = time.time()
            for mid in [mid for mid, info in mine.items() if now - info[-1] > PICK_IDLE]:
                del mine[mid]
            status = await m.reply(f"📤 {name} ({human(size)}) — send to which server?", quote=True,
                                   reply_markup=pick_keyboard(servers))
            mine[status.id] = (m.chat.id, m.id, name, size, cap_dest, group, now)
            return
        if servers:  # exactly one server saved: nothing to choose
            c = store.get_server(uid, servers[0])
    dest = cap_dest if cap_dest is not None else render_dest(c, m.from_user, name, group)
    status = await m.reply("⏳ Queued…", quote=True)
    await queue_upload(uid, m.chat.id, m.id, c, name, dest, size, status)


async def collect_part(m, col):
    """While /multi is active, files are not uploaded one by one: they are collected into one big file."""
    media = getattr(m, m.media.value)
    if all(i[0] != m.id for i in col["items"]):
        col["items"].append((m.id, media_name(m), getattr(media, "file_size", 0) or 0))
    col["ts"] = time.time()
    n, total = len(col["items"]), sum(i[2] for i in col["items"])
    text = f"📎 {n} part{'s' if n > 1 else ''} collected ({human(total)}). Send more, or /done to upload them as one file. /cancel to abort."
    if col["status"] is None:
        col["status"] = await m.reply(text, quote=True)
    else:
        try:
            await col["status"].edit_text(text)
        except Exception:
            pass


@app.on_message(auth & filters.command("multi"))
async def multi_cmd(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    name = clean_name(arg_text(m)) or None
    COLLECT[uid] = dict(name=name, server=c["name"], items=[], status=None, ts=time.time())
    await m.reply(
        f"📚 Collecting volumes for {c['name']}{' as ' + name if name else ''}. Send the parts of the big file: "
        "name.001, name.002, ... are put in order automatically, any other files keep the order you sent them. "
        "I join them on the fly into ONE file on the server, so files of any size work on FTP, FTPS and SFTP alike "
        "(for files up to 4 GB that Telegram accepts in one piece, just send the file directly instead). "
        "Then send /done. (/cancel to abort)")


@app.on_message(auth & filters.command("done"))
async def done_cmd(_, m):
    uid = m.from_user.id
    col = COLLECT.get(uid)
    if not col:
        return await m.reply("Nothing is being collected. Start with /multi [name].")
    if not col["items"]:
        return await m.reply("No parts yet. Send the files first, then /done.")
    ordered, base, problem = order_volumes(col["items"])
    if problem:
        return await m.reply(f"❌ {problem}")
    name = col["name"] or base
    if not name:
        return await m.reply("Give the file a name: /multi <name> (or send volumes named name.001, name.002, ...).")
    c = store.get_server(uid, col["server"])
    COLLECT.pop(uid, None)
    if not c:
        return await m.reply("That server no longer exists.")
    dest = render_dest(c, m.from_user, name, group_from_name(name))
    ids, size = ",".join(str(i[0]) for i in ordered), sum(i[2] for i in ordered)
    status = await m.reply("⏳ Queued…")
    ahead = store.count_active(uid, c["name"])
    jid = store.add_job(uid, c["name"], m.chat.id, f"tgm:{m.chat.id}:{ids}", name, dest, size)
    store.set_job(jid, status_id=status.id)
    store.log(uid, c["name"], f"multi upload {dest} ({len(ordered)} parts)")
    await status.edit_text(f"#{jid} ⏳ {name} ({len(ordered)} parts, {human(size)}) → {c['name']}:{dest}" + (f"\n{ahead} ahead in the queue" if ahead else ""))
    engine.enqueue(jid)


@app.on_message(auth & filters.command("perf"))
async def perf_cmd(_, m):
    await m.reply("⚙️ " + describe_perf() + "\n\nSet PERFORMANCE=light|balanced|turbo (or TG_PARALLEL, TG_WINDOW_MB, SFTP_WINDOW_MB, FTP_BLOCK_KB, "
                  "PARALLEL_PER_SERVER) and restart to change it.")


ICON = {"queued": "⏳", "running": "⬆️", "done": "✅", "failed": "❌", "cancelled": "✖"}


async def retry_jobs(uid, arg):
    if arg.isdigit():
        j = store.get_job(int(arg))
        ids = [j["id"]] if j and j["uid"] == uid else []
    else:
        ids = [j["id"] for j in store.list_jobs(uid, ("failed",), 200)]
    return sum([1 for i in ids if await engine.retry(i)])


async def stop_jobs(uid, arg):
    if arg.isdigit():
        j = store.get_job(int(arg))
        ids = [j["id"]] if j and j["uid"] == uid else []
    elif arg == "all":
        ids = [j["id"] for j in store.list_jobs(uid, ("queued", "running"), 200)]
    else:
        return None
    for i in ids:
        await engine.stop(i)
    return len(ids)


@app.on_message(auth & filters.command("queue"))
async def queue_cmd(_, m):
    rows = store.list_jobs(m.from_user.id, limit=15)
    if not rows:
        return await m.reply("Nothing queued yet. Send me a file.")
    lines = []
    for j in rows:
        line = f"{ICON.get(j['state'], '•')} #{j['id']} {j['name']} → {j['server']}" + (f" ({human(j['size'])})" if j["size"] else "")
        if j["state"] in ("queued", "running") and j["attempts"] > 1:
            line += f"  [attempt {j['attempts']}]"
        if j["state"] == "failed" and j["error"]:
            line += f"\n      {j['error'][:100]}"
        lines.append(line)
    kb = InlineKeyboardMarkup([[Btn("↻ Retry failed", callback_data="jr:all"), Btn("✖ Stop all", callback_data="js:all")]])
    await m.reply("\n".join(lines), reply_markup=kb)


@app.on_message(auth & filters.command("retry"))
async def retry_cmd(_, m):
    n = await retry_jobs(m.from_user.id, arg_text(m))
    await m.reply(f"↻ Re-queued {n} job(s). Partial files are resumed." if n else "Nothing to retry. See /queue.")


@app.on_message(auth & filters.command("stop"))
async def stop_cmd(_, m):
    n = await stop_jobs(m.from_user.id, arg_text(m).lower())
    await m.reply("Usage: /stop <job id> | /stop all" if n is None else f"✖ Stopping {n} job(s).")


@app.on_message(auth & filters.command("usage"))
async def usage_cmd(_, m):
    u = store.usage(m.from_user.id)
    await m.reply(
        f"📈 Uploads\ntoday: {u['today'][0]} files, {human(u['today'][1])}\nlast 7 days: {u['week'][0]} files, {human(u['week'][1])}\n"
        f"waiting/running: {u['pending']}   failed: {u['failed']}"
    )


@app.on_message(auth & filters.command("folder"))
async def folder_cmd(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    arg = arg_text(m)
    if not arg:
        return await m.reply(
            f"Folder template for {c['name']}: {c['folder'] or '(none, files go to the current directory)'}\n\n"
            "Set: /folder <template>   Clear: /folder off\n"
            "Variables: {user} {uid} {date} {year} {month} {day} {type} {ext}\n"
            "Examples: /folder {user}/{date}   ·   /folder backups/{type}   ·   /folder /uploads/{user}\n"
            "A caption like \"> some/dir/\" on a file overrides it."
        )
    if arg.lower() == "off":
        store.update(uid, c["name"], folder="")
        return await m.reply("Folder template cleared.")
    try:
        sample_path = arg.format(user="alice", uid=1, date="2026-01-01", year=2026, month="01", day="01", type="images", ext="jpg")
    except (KeyError, IndexError, ValueError) as e:
        return await m.reply(f"❌ Bad template ({e}). Use only the variables listed in /folder.")
    store.update(uid, c["name"], folder=arg)
    await m.reply(f"📁 Uploads to {c['name']} now go to: {arg}\n(e.g. {sample_path}/file.jpg)")


@app.on_message(auth & filters.command("ondup"))
async def ondup_cmd(_, m):
    uid = m.from_user.id
    c = await need(uid, m)
    if not c:
        return
    arg = arg_text(m).lower()
    if arg not in ("overwrite", "skip", "rename"):
        return await m.reply(f"When a file already exists on {c['name']}: {c['dup']}\nChange with /ondup overwrite | skip | rename")
    store.update(uid, c["name"], dup=arg)
    await m.reply(f"✅ Existing files will be: {arg}d" if arg != "skip" else "✅ Existing files will be skipped")


@app.on_message(auth & filters.text)
async def text_in(_, m):
    uid = m.from_user.id
    p = PENDING_AUTH.get(uid)
    if p:  # password for a server being added (may start with "/" - real commands were handled above)
        PENDING_AUTH.pop(uid)
        try:
            await m.delete()
        except Exception:
            pass
        if time.time() - p["ts"] > 300:
            return await m.reply("That request expired. Run /add again.")
        return await finish_add(m, uid, p["name"], p, "password", m.text.strip())
    if m.text.startswith("/"):
        return
    if uid in TERM:
        if time.time() - TERM[uid] > TERM_IDLE:
            TERM.pop(uid)
            return await m.reply("💻 Terminal mode timed out. /term to start again.")
        TERM[uid] = time.time()
        c = await need(uid, m, ssh=True)
        if c:
            cmd = m.text[2:].strip() if m.text.startswith("$ ") else m.text.strip()
            await run_shell(m, uid, c["name"], cmd)


@app.on_callback_query(filters.user(list(ALLOWED)))
async def on_callback(_, cq):
    kind, _, arg = (cq.data or "").partition(":")
    uid, msg = cq.from_user.id, cq.message
    if store.is_banned(uid):
        return await cq.answer("⛔ You are banned from this bot.", show_alert=True)
    if kind == "subchk":  # 'I joined - recheck' under the force-subscribe card
        await cq.answer()
        SUB_OK.pop(uid, None)  # drop the cached answer and really check again
        if await sub_ok(uid):
            await msg.reply("✅ Thanks! Send your command again.")
        else:
            await msg.reply(f"🔒 You still need to join {FORCE_SUB_LABEL} first.")
        return
    if not await sub_ok(uid):  # the same gate as messages, for every button
        if FORCE_SUB_URL:
            return await cq.answer(f"Join {FORCE_SUB_LABEL} first", url=FORCE_SUB_URL)
        return await cq.answer(f"Join {FORCE_SUB_LABEL} first", show_alert=True)
    if kind in ("ok", "no"):
        item = PENDING.pop(arg, None)
        if not item or item[0] != uid or time.time() - item[1] > 300:
            return await cq.answer("This request expired.", show_alert=True)
        try:
            await msg.edit_reply_markup()
        except Exception:
            pass
        if kind == "no":
            await cq.answer("Cancelled")
            return await msg.reply("✖ Cancelled.")
        await cq.answer("Running…")
        return await item[2](msg)
    if kind == "use":
        ok = store.set_active(uid, arg)
        return await cq.answer(f"Now using {arg}" if ok else "No such server", show_alert=not ok)
    if kind in ("pick", "pickc"):  # /ask: choose the destination server for a file sent earlier
        mine = PICK.get(uid) or {}
        info = mine.pop(msg.id, None)
        if not info:
            return await cq.answer("This choice is no longer pending (it expired or the bot restarted) — send the file again.",
                                    show_alert=True)
        chat_id, msg_id, name, size, cap_dest, group, _ = info
        if kind == "pickc":
            await cq.answer("Cancelled")
            try:
                await msg.edit_text(f"✖ {name} cancelled")
            except Exception:
                pass
            return
        c = store.get_server(uid, arg)
        if not c:
            mine[msg.id] = info  # let the user pick another button instead of losing the file
            return await cq.answer(f"Server '{arg}' no longer exists — pick another one.", show_alert=True)
        await cq.answer(f"Sending to {c['name']}…")
        try:
            await msg.edit_reply_markup()
        except Exception:
            pass
        dest = cap_dest if cap_dest is not None else render_dest(c, cq.from_user, name, group)
        await queue_upload(uid, chat_id, msg_id, c, name, dest, size, msg)
        return
    if kind in ("jr", "js"):
        await cq.answer()
        n = await (retry_jobs if kind == "jr" else stop_jobs)(uid, arg)
        return await msg.reply(f"{'↻ Re-queued' if kind == 'jr' else '✖ Stopping'} {n or 0} job(s).")
    if kind == "q":
        await cq.answer()
        if arg == "stats":
            return await do_stats(msg, uid)
        if arg == "reboot":
            return await do_reboot(msg, uid)
        if arg in VIEWS:
            return await do_view(msg, uid, arg)
    if kind == "buy":  # a /plans button: open an order and choose how to pay
        p = subscriptions.plan(arg)
        if not p or p.free:
            return await cq.answer("Pick a paid plan - /plans", show_alert=True)
        o, _fresh = subscriptions.order_create(uid, p)
        opts = payments.methods(p)
        if not opts:  # no gateway configured: the assisted flow with PAYMENT_INFO
            await cq.answer()
            return await msg.edit_text(order_text(o, p))
        rows = [[Btn(m["label"], callback_data=f"pay:{o['id']}:{m['key']}")] for m in opts]
        rows.append([Btn("🧾 Paid another way? Send the reference", callback_data="pl:ref")])
        await cq.answer()
        return await msg.edit_text(f"🧾 Order #{o['id']} · {p.name} · {p.price:g} {CURRENCY} / {p.days}d\n\n"
                                   "How do you want to pay?", reply_markup=InlineKeyboardMarkup(rows))
    if kind == "pay":  # pay:<oid>:<method> - start the payment on the chosen gateway
        oid_s, _, mkey = arg.partition(":")
        try:
            o = store.order_get(int(oid_s))
        except ValueError:
            o = None
        if not o or o["uid"] != uid:
            return await cq.answer("That order is not yours - /plans", show_alert=True)
        if o["status"] not in ("pending", "awaiting"):
            return await cq.answer(f"Order #{o['id']} is {o['status']}.", show_alert=True)
        p = subscriptions.plan(o["plan"])
        if not p:
            return await cq.answer("Unknown plan", show_alert=True)
        await cq.answer("Creating the payment…")
        try:
            r = await asyncio.to_thread(payments.start, o["id"], p, mkey,
                                        (cq.from_user.first_name or "")[:32], CURRENCY)
        except Exception as e:
            return await msg.reply(f"❌ {e}\nPick another way to pay: /plans")
        if r["mode"] == "upigw":
            try:
                rows = [[Btn("💳 Open the checkout page", url=r["checkout_url"])]]
                if r.get("intent"):
                    rows.append([Btn("📲 Open the UPI app", url=r["intent"])])
                await msg.edit_text(f"🧾 Order #{o['id']} · {p.name} · {p.price:g} {CURRENCY}\n\n"
                                    "💳 Pay on the gateway's checkout page (dynamic QR + any UPI app). "
                                    "It confirms here automatically - no /paid needed.\n⏳ The checkout "
                                    "window is short: pay right after you open it.",
                                    reply_markup=InlineKeyboardMarkup(rows))
            except Exception:
                pass
            if r.get("qr"):  # the gateway's UPI deep link as a scannable QR, amount pre-filled
                await msg.reply_photo(r["qr"], caption="Or scan this in any UPI app - the amount is "
                                                       "filled in already.")
        elif r["mode"] == "upi":
            await msg.reply_photo(r["qr"], caption=f"🧾 Order #{o['id']} · {p.name} · {r['amount']} INR\n\n"
                                 f"Payee: {r['vpa']}\nScan, or tap the button below, pay in any UPI app,\n"
                                 "then send /paid <UTR reference> - the owner confirms it.")
            try:
                await msg.edit_reply_markup()
            except Exception:
                pass
            await msg.reply("📲 One-tap:", reply_markup=InlineKeyboardMarkup(
                [[Btn("Open the UPI app", url=r["intent"])]]))
        else:  # crypto
            coin_label = r["coin"].replace("/", " ").upper()
            await msg.reply_photo(r["qr"], caption=f"🧾 Order #{o['id']} · {p.name}\n\n{coin_label}\n"
                                 f"Address: {r['address']}\nAmount: {r['amount']} (exact)\n\n"
                                 "It confirms on-chain automatically - I'll message you when it clears. "
                                 "Partial payments count: send the rest to the same address.")
            try:
                await msg.edit_reply_markup()
            except Exception:
                pass
        return
    if kind == "pl":  # the plans / hosting buttons on several screens
        await cq.answer()
        if arg == "plans":
            return await reply_long(msg, plans_text(uid), reply_markup=plans_kb())
        if arg == "sub":
            return await reply_long(msg, mysub_text(uid), reply_markup=InlineKeyboardMarkup(
                [[Btn("💎 Plans", callback_data="pl:plans"), Btn("🚀 My VPS", callback_data="pl:vps")]]))
        if arg == "vps":
            return await reply_long(msg, myvps_text(uid))
        if arg == "host":
            return await reply_long(msg, HOSTING_GUIDE, reply_markup=InlineKeyboardMarkup(
                [[Btn("💎 Plans", callback_data="pl:plans"), Btn("🚀 My VPS", callback_data="pl:vps")]]))
        if arg == "ref":
            return await msg.reply("Send /paid <transaction reference> right here (the UTR from your UPI "
                                   "app or the crypto tx id) - the owner approves it.")
        return
    if kind == "ord":  # the order decision buttons (from /paid or the panel)
        if uid not in ADMINS:
            return await cq.answer("Admins only.", show_alert=True)
        oid_s, _, act = arg.rpartition(":")
        try:
            oid = int(oid_s)
        except ValueError:
            return await cq.answer("Bad button", show_alert=True)
        o, error = await approve_order(oid, uid, act == "a")
        if error:
            return await cq.answer(error, show_alert=True)
        await cq.answer("✅ Approved - the buyer got everything." if act == "a" else "Declined.")
        try:
            await msg.edit_reply_markup()
        except Exception:
            pass
        return
    if kind == "adm" and uid in ADMINS:  # the /admin panel's buttons
        await cq.answer()
        if arg == "overview":
            return await reply_long(msg, admin_overview_text())
        if arg == "users":
            return await reply_long(msg, admin_users_text())


# ---------------------------------------------------------------- monitoring
def sample(c):
    with Remote(c) as r:
        return r.exec(STATS_CMD, 30)[0]


async def monitor_loop():
    state = {}  # (uid, server) -> {issue_key: text}
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        try:
            servers_ = store.monitored()
        except Exception:
            continue
        live = set()
        for c in servers_:
            if c["scheme"] != "ssh":
                continue
            key = (c["uid"], c["name"])
            live.add(key)
            try:
                issues = stats_issues(parse_kv(await asyncio.wait_for(asyncio.to_thread(sample, c), 90)))
            except Exception as e:
                issues = {"down": f"unreachable ({type(e).__name__})"}
            old = state.get(key, {})
            state[key] = issues
            new = [v for k, v in issues.items() if k not in old]
            cleared = [k for k in old if k not in issues]
            try:
                if new:
                    await app.send_message(c["uid"], f"🚨 {c['name']}: " + "; ".join(new))
                if cleared:
                    await app.send_message(c["uid"], f"✅ {c['name']}: recovered ({', '.join(cleared)})")
            except Exception:
                logging.exception("monitor notify failed")
        for gone in [k for k in state if k not in live]:  # deleted servers / monitoring turned off
            del state[gone]


COMMANDS = [
    ("vps", "VPS dashboard"), ("stats", "CPU, RAM, disk, uptime"), ("sh", "Run a shell command"),
    ("term", "Terminal mode"), ("service", "Manage systemd services"), ("logs", "Service logs"),
    ("docker", "Manage containers"), ("ls", "List remote directory"), ("cd", "Change directory"),
    ("multi", "Upload a file over 4 GB from volumes"), ("perf", "Speed profile"), ("get", "Send a server file to this chat"), ("join", "Join .001 .002 volumes on the server"), ("fetch", "Download a URL to the server"),
    ("queue", "Upload queue"), ("retry", "Retry failed uploads"), ("folder", "Auto-folder template"),
    ("servers", "Saved servers"), ("add", "Add a server"), ("monitor", "Alerts on/off"),
    ("ask", "Pick the server per file"), ("audit", "Action log"), ("about", "Bot status"), ("help", "All commands"),
    ("app", "Web UI for servers, files and console"), ("admin", "Admin panel (admins)"),
    ("plans", "Plans: more servers, your own VPS"), ("mysub", "Your plan and expiry"),
    ("myvps", "Your VPS instances"), ("paid", "Confirm a payment"), ("hosting", "Host your bot - the guide"),
]


async def health_server(port):
    """The bot's HTTP port: the Mini App plus its JSON API, and /health (or any unknown path) with a 200
    so PaaS health checks still pass. On your own VPS set HEALTH_BIND=127.0.0.1 and put a TLS proxy in front."""
    return await webapi.start(port)


async def start_client():
    """Start Telegram. If a restored/old session was rejected, drop it and log in again with the bot token."""
    global SESSION_SOURCE
    try:
        await app.start()
    except (Unauthorized, AuthKeyDuplicated) as e:
        logging.warning("Telegram rejected the stored session (%s); logging in again with the bot token", type(e).__name__)
        try:
            await app.stop()
        except Exception:
            pass
        await asyncio.to_thread(session_sync.discard)
        SESSION_SOURCE = "fresh login (stored session was rejected)"
        await app.start()


async def keep_alive(url, every):
    """Free PaaS web services sleep after ~15 min without inbound HTTP traffic, which would take the bot offline.
    A tiny request to our own public URL counts as inbound traffic. Opt-in: KEEP_ALIVE=1."""
    def ping():
        try:
            urllib.request.urlopen(url, timeout=20).read(64)
        except Exception as e:
            logging.warning("keep-alive ping failed: %s", e)

    while True:
        await asyncio.sleep(every)
        await asyncio.to_thread(ping)


def lock_lost():
    logging.error("Another copy of the bot took over the instance lock; shutting down")
    os.kill(os.getpid(), signal.SIGTERM)


async def main():
    global SESSION_SOURCE
    port = env_int("HEALTH_PORT", 0, lo=0, hi=65535) or env_int("PORT", 0, lo=0, hi=65535)
    webapi.configure(app, engine, BOT_TOKEN, ALLOWED, WEBAPP_URL, ADMINS)  # before the port opens: the app answers at once
    health = await health_server(port) if port else None  # up first, so the platform's health check passes at once
    await asyncio.to_thread(session_sync.acquire_lock)  # waits while an old copy is still shutting down
    SESSION_SOURCE = session_sync.restore()  # after the lock, so the old copy's final snapshot is what we restore
    await start_client()
    for uid in ALLOWED:  # seed the admin panel's user list even before anyone says /start
        store.touch_user(uid)
    await app.set_bot_commands([BotCommand(c, d) for c, d in COMMANDS])
    if WEBAPP_URL.startswith("https://"):  # the menu button opens the Mini App from any chat
        try:
            await app.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open app", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception as e:
            logging.warning("could not set the menu button: %s", e)
    engine.start()  # resume anything that was queued when the bot last stopped
    await asyncio.to_thread(session_sync.snapshot, True)
    if os.getenv("RENDER") and store.URL.startswith("sqlite"):
        logging.warning("Running on Render with SQLite: its disk is wiped on every restart, so saved servers and the queue "
                        "would be lost. Set DATABASE_URL to an external PostgreSQL/MySQL/MongoDB database.")
    logging.info("Started. database=%s session=%s health_port=%s instance=%s", store.LABEL, SESSION_SOURCE, port or "off", session_sync.INSTANCE)
    tasks = [asyncio.create_task(monitor_loop()), asyncio.create_task(session_sync.sync_loop()),
             asyncio.create_task(session_sync.heartbeat_loop(lock_lost))]
    if os.getenv("KEEP_ALIVE", "").lower() in ("1", "true", "yes"):
        url = os.getenv("KEEP_ALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL")
        if url:
            every = env_int("KEEP_ALIVE_INTERVAL", 600, lo=1)
            tasks.append(asyncio.create_task(keep_alive(url, every)))
            logging.info("Keep-alive: pinging %s every %ss", url, every)
        else:
            logging.warning("KEEP_ALIVE is on but there is no KEEP_ALIVE_URL / RENDER_EXTERNAL_URL to ping")
    await idle()  # returns on SIGINT/SIGTERM, so containers stop cleanly
    for t in tasks:
        t.cancel()
    await app.stop()
    await asyncio.to_thread(session_sync.snapshot, True)
    await asyncio.to_thread(session_sync.release_lock)  # lets the next copy start immediately
    if health:
        health.close()


if __name__ == "__main__":
    app.run(main())
