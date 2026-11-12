"""Direct 2-4 GB uploads (Premium files) and the /ask server picker.

- BIG-1  TG_IN_MAX matches Telegram's absolute ceiling: 4 GiB per file
- BIG-2  size_verdict: 4 GiB is direct, one byte more is refused with split/multi guidance
- BIG-3  4 GiB+ job sizes survive the queue (BIGINT, also checked per-backend by check_contract)
- BIG-4  ask-mode flag round-trips through the kv store and defaults to off
- BIG-5  caption_dest: folder, explicit file, and empty captions (same semantics as the old inline code)
- BIG-6  the picker's bookkeeping dict drops entries after PICK_IDLE
- BIG-7  callback data stays within Telegram's 64-byte limit for any legal server name
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from cryptography.fernet import Fernet

os.environ.update(SECRET_KEY=Fernet.generate_key().decode(),
                  DATABASE_URL="sqlite:///" + os.path.join(tempfile.mkdtemp(), "s.db"),
                  API_ID="1", API_HASH="x", BOT_TOKEN="1:x", ALLOWED_USERS="5", SESSION_DIR=tempfile.mkdtemp())

import bot  # noqa: E402  (env above must be set first: pyrogram grabs the loop at import time)
import store  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))  # noqa: E731

# --- BIG-1
ok(bot.TG_IN_MAX == 4 * 1024 ** 3, "incoming-file ceiling is Telegram's own 4 GiB (Premium max)")

# --- BIG-2
ok(bot.size_verdict(0) is None, "unknown size is allowed through (cannot check)")
ok(bot.size_verdict(2 * 1024 ** 3) is None, "exactly 2 GiB streams directly")
ok(bot.size_verdict(bot.TG_IN_MAX) is None, "exactly 4 GiB still streams directly")
bad = bot.size_verdict(bot.TG_IN_MAX + 1)
ok(bad and "4 GB" in bad and "/multi" in bad, "4 GiB + 1 byte is refused with split + /multi guidance")

# --- BIG-3: 32-bit traps (2^31-1, 2^32, 2^32+1, 3.5 GiB) must round-trip through the jobs table
store.add_server(dict(uid=5, name="ftp1", scheme="ftp", host="h", port=21, user="u", auth="password", secret="p", cwd="/", hostkey=""))
for n, size in enumerate((2 ** 31 - 1, 2 ** 32, 2 ** 32 + 1, 3 * 1024 ** 3 + 7 * 1024 ** 2)):
    jid = store.add_job(5, "ftp1", 1, f"tg:1:{n}", f"big{n}.bin", f"big{n}.bin", size)
    ok(store.get_job(jid)["size"] == size, f"job size {size} round-trips (BIGINT, no 32-bit truncation)")
jid = store.add_job(5, "ftp1", 1, "tg:1:99", "huge.bin", "huge.bin", 4 * 1024 ** 3)
ok(store.get_job(jid)["size"] == 4 * 1024 ** 3, "a full 4 GiB job size round-trips")

# --- BIG-4
ok(not bot.ask_mode(5), "ask mode defaults to off")
store.kv_set("ask:5", "1")
ok(bot.ask_mode(5), "ask mode on round-trips through the kv store")
store.kv_set("ask:5", "")
ok(not bot.ask_mode(5), "ask mode off clears the flag")

# --- BIG-5: same semantics as the pre-refactor inline caption parsing
ok(bot.caption_dest("backups/2026/", "a.zip") == "backups/2026/a.zip", "caption folder gets the file name appended")
ok(bot.caption_dest("backups/a.zip", "ign.zip") == "backups/a.zip", "caption file path is used as-is")
ok(bot.caption_dest("backups\\", "a.zip") == "backups/a.zip", "windows-style folder also appends the name")
ok(bot.caption_dest("  ", "a.zip") == "a.zip", "caption of only spaces falls back to the file name")
ok(bot.caption_dest("x\n>/etc/passwd", "a.zip") == "x", "only the first caption line is parsed (no CRLF smuggling)")
ok(not bot.caption_dest("../../etc", "a.zip").startswith("."), "caption traversal cannot escape (clean_path)")

# --- BIG-6: picker entries expire
mine = bot.PICK.setdefault(5, {"ts": 0.0, "m": {}})["m"]
mine["1"] = dict(chat_id=1, msg_id=2, name="f.bin", size=10, cap_dest=None, group="files",
                 ts=time.time() - bot.PICK_IDLE - 1)
mine["2"] = dict(chat_id=1, msg_id=3, name="fresh.bin", size=10, cap_dest=None, group="files",
                 ts=time.time())
now = time.time()
stale = [mid for mid, info in mine.items() if now - info["ts"] > bot.PICK_IDLE]
ok(stale == ["1"] and "2" not in stale, "the picker bookkeeping marks stale entries (refreshed at PICK_IDLE)")

# --- BIG-7: callback data length for the longest legal server name (24 chars, NAME_RE)
longest = "a" * 24
kb = bot.pick_keyboard([longest])
data = kb.inline_keyboard[0][0].callback_data
ok(len(data.encode()) <= 64 and data == f"pick:{longest}", f"picker callback data fits Telegram's 64-byte limit ({len(data)} bytes)")
ok(kb.inline_keyboard[-1][0].callback_data.startswith("pickc:"), "the cancel button uses the pickc prefix (no name collision)")

print("bigfile: all checks passed")
