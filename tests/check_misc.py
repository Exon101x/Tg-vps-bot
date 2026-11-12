import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio, http.server, importlib, random, tempfile, threading, types
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

# ---------------------------------------------------------------- long texts vs Telegram's 4096-unit cap
ok(bot.tlen("a") == 1 and bot.tlen("\U0001f5a5") == 2 and bot.tlen("a\U0001f5a5") == 3,
   "tlen counts UTF-16 units (an emoji costs 2)")
ok(bot.split_text("hello") == ["hello"], "a short text is one chunk, untouched")
big = "\n".join(f"line {i} of the manual - pack it with a few words" for i in range(200))
ch = bot.split_text(big)
ok(all(bot.tlen(c) <= 4000 for c in ch) and "\n".join(ch).split() == big.split(),
   f"a {bot.tlen(big)}-unit text splits into {len(ch)} chunks, every word preserved")
ok(all(c.startswith("line ") for c in ch), "chunks break at line boundaries")
e = "\U0001f5a5" * 3000  # 6000 UTF-16 units of pure emoji
ch = bot.split_text(e)
ok(all(bot.tlen(c) <= 4000 for c in ch) and "".join(ch) == e, "emoji-dense text: no emoji cut in half, nothing lost")
one = "x" * 9000
ch = bot.split_text(one)
ok(all(bot.tlen(c) <= 4000 for c in ch) and "".join(ch) == one, "a single 9000-char line is hard-cut, nothing lost")
ok(all(bot.tlen(c) <= 4096 for c in bot.split_text(bot.HELP)),
   f"HELP ({bot.tlen(bot.HELP)} UTF-16 units) fits Telegram after splitting - /start used to crash on it")
ok("\n".join(bot.split_text(bot.HELP)).split() == bot.HELP.split(), "HELP splits without losing a word")

class FakeMsg:
    """Stands in for a pyrogram Message: records every reply_long send through
    Message.reply - the only real path. (The old fake had Chat.send_message, the
    Telethon-style call that crashed /start live: Pyrogram Chats have no such method.)"""
    def __init__(self): self.sent = []
    async def reply(self, text, **kw): self.sent.append((text, kw))
fm = FakeMsg()
parts_big = bot.split_text(big)
asyncio.run(bot.reply_long(fm, big, reply_markup="KB"))
ok([t for t, _kw in fm.sent] == parts_big,
   "reply_long: every chunk goes out through Message.reply, in order - no Chat.send_message")
ok(fm.sent[0][1].get("reply_markup") == "KB", "reply_long: the keyboard rides the first chunk")
ok(all(kw == {"quote": False} for _t, kw in fm.sent[1:]),
   "reply_long: follow-ups are plain messages (quote=False, no keyboard)")
fm = FakeMsg()
asyncio.run(bot.reply_long(fm, "hi"))
ok([t for t, _kw in fm.sent] == ["hi"] and fm.sent[0][1] == {}, "reply_long: a short text is a single plain reply")

# ---------------------------------------------------------------- the /start card and the /help hub
ok(all(bot.tlen(bot.help_fill(body)) <= 4000 for _t, _b, body in bot.HELP_SECTIONS.values()),
   "every manual section is one page: fits a single message with room to spare")
ok(all(f"/{c}" in bot.HELP for c, _d in bot.COMMANDS),
   "every command in Telegram's menu has a place in the manual")
ok(bot.REPO_URL in bot.HELP, "the manual keeps the source link (forks are asked to keep it)")
ok("Nihal" in bot.welcome_text("Nihal") and "Hi" in bot.welcome_text("")
   and all(bot.tlen(bot.welcome_text(n)) <= 4000 for n in ("Nihal", "", "A" * 64)),
   "the /start card is personal and fits a single message")
flat = lambda kb: [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
hub_text, hub_kb = bot.help_hub(42)
ok(bot.tlen(hub_text) <= 4000, "the help hub fits a single message")
ok(set(flat(hub_kb)) == {f"help:{k}" for k in bot.HELP_SECTIONS if k != "admin"} | {"help:all"},
   "the hub buttons open exactly the non-admin sections, plus the whole-manual button")
bot.ADMINS.add(7)
ok("help:admin" in flat(bot.help_hub(7)[1]) and "help:admin" not in flat(bot.help_hub(8)[1]),
   "the admin section shows for admins only")
page_text, page_kb = bot.help_page("files", 42)
ok(page_text.startswith("📁 Files") and set(flat(page_kb)) == {"help:menu", "help:home"},
   "a section page has its title plus back-to-hub and back-to-start buttons")
ok(bot.help_page("nosuch", 42)[0] == bot.help_hub(42)[0], "an unknown section button falls back to the hub")
ok(bot.help_page("admin", 8)[0] == bot.help_hub(8)[0] and bot.help_page("admin", 7)[0] != bot.help_hub(7)[0],
   "the admin page: admins get the page, everyone else the hub")
ok({"help:menu", "pl:plans", "pl:vps", "pl:sub"} <= set(flat(bot.start_kb())),
   "the /start card links the manual hub, plans, my VPS and my plan")

print("ALL MISC PASSED")
