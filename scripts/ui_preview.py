"""Print every redesigned chat card side by side, and validate the HTML ones parse."""
import html.parser
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY="x" * 32, DATABASE_URL=f"sqlite:///{TMP}/ui.db", API_ID="1", API_HASH="x",
                  BOT_TOKEN="1:x", ALLOWED_USERS="5", LOG_LEVEL="CRITICAL")
import logging; logging.disable(logging.CRITICAL)

import bot
import store

store.add_server(dict(uid=5, name="alpha", scheme="ssh", host="203.0.113.5", port=22, user="root",
                      auth="password", secret="pw", cwd="/root", hostkey="", monitor=1))
store.add_server(dict(uid=5, name="backups", scheme="ftps", host="backups.example", port=21, user="u",
                      auth="password", secret="pw", cwd="/files", hostkey="", monitor=0))
store.set_active(5, "alpha")


class V(html.parser.HTMLParser):
    VOID = {"br", "b", "i", "u", "s", "a", "code", "pre", "tg-spoiler", "blockquote"}
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, _attrs):
        if tag in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"mismatched </{tag}>")


def show(title, text, is_html=False):
    print("\n" + "=" * 62)
    print(f"-- {title}")
    print("=" * 62)
    print(text)
    if is_html:
        v = V()
        v.feed(text)
        leftover = [t for t in v.stack]
        status = "HTML OK" if not v.errors and not leftover else f"HTML BAD: {v.errors} leftover={leftover}"
        print(f"[{status}]")


show("welcome_text", bot.welcome_text("Aisha"), True)
show("welcome_text (returning user)", bot.welcome_text("Aisha") + "\n\n🔌 Active server ··· <b>alpha</b> — /servers to switch", True)
show("help_hub", bot.help_hub(5)[0])
show("help_page files", bot.help_page("files", 5)[0], True)
show("help_page servers", bot.help_page("servers", 5)[0], True)
show("help_page pay", bot.help_page("pay", 5)[0], True)

# servers card, reproduced from the handler
rows = store.list_servers(5)
active = store.active_name(5)
lines = [bot.head("🖥", "Your servers", f"{len(rows)} saved · tap a button to switch"), ""]
for r in rows:
    mark = "▶️" if r["name"] == active else "⚪"
    target = f"{r['scheme']}://{r['user']}@{r['host']}:{r['port']}"
    lines.append(f"{mark} <b>{html.escape(r['name'])}</b>" + ("  🔔" if r["monitor"] else ""))
    lines.append(f"   <code>{html.escape(target)}</code>")
lines += ["", "Tap to switch · /status for the active one's card"]
show("servers card", "\n".join(lines), True)

c = store.get_server(5)
lines = [bot.head("🔌", c["name"], f"{c['scheme']}://{c['user']}@{c['host']}:{c['port']}"), "",
         bot.kvrows([("directory", c["cwd"] or "/"), ("auth", c["auth"]),
                     ("monitoring", "on 🔔" if c["monitor"] else "off"),
                     ("folder", c["folder"] or "(none)"), ("duplicates", c["dup"])])]
show("status card", "\n".join(lines))

show("plans_text", bot.plans_text(5), True)
show("mysub_text", bot.mysub_text(5))
show("myvps_text", bot.myvps_text(5))
show("admin_overview_text", bot.admin_overview_text(), True)
show("admin_orders_text", bot.admin_orders_text(), True)
show("HOSTING_GUIDE", bot.HOSTING_GUIDE)
show("fmt_stats", bot.fmt_stats({"host": "alpha", "os": "Debian 12", "uptime": "up 12 days",
                                 "load": "0.10, 0.20, 0.15", "cores": "4", "mem_used": "2100",
                                 "mem_total": "8192", "disk_used": str(21 * 1048576),
                                 "disk_total": str(100 * 1048576), "disk_pct": "21%"}))
print("\nALL RENDERED")
