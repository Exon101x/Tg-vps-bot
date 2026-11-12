"""Implicit state machines made explicit: every flow that used to die on restart now survives it.

A "restart" here is a fresh State object over the same database row - exactly what happens when
bot.py re-imports on boot. The checks below pin: confirm tokens, pending passwords, /multi
collections, /ask pickers, terminal mode, monitor issue state, job-cancel intents and Mini App
upload sessions, plus the expiry sweeps that keep the blobs bounded.
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import asyncio
import itertools
import tempfile
import time
from cryptography.fernet import Fernet

TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/state.db",
                  API_ID="1", API_HASH="x", BOT_TOKEN="1:x", ALLOWED_USERS="5",
                  SESSION_DIR=f"{TMP}/sess", LOG_LEVEL="CRITICAL", PERFORMANCE="turbo")
import logging; logging.disable(logging.CRITICAL)

import persist
import store
import bot
import jobs

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
UID = 4242

store.add_server(dict(uid=UID, name="srv", scheme="ftp", host="127.0.0.1", port=21, user="u",
                      auth="password", secret="pw", cwd="", hostkey=""))


def restart(state):
    """A new State over the same kv row = what boot does after a crash/deploy."""
    state.flush(force=True)
    return persist.State(state.name, ttl=state.ttl)


# ---------------------------------------------------------------- 1. confirm buttons survive a restart
async def t_confirm():
    m = FakeMsg()
    await bot.confirm(m, UID, "Run on srv?\n\nuptime", "sh", "srv", "uptime", True)
    token = next(iter(bot.PENDING.keys()))
    item = bot.PENDING[token]
    ok(item["uid"] == UID and item["act"] == "sh" and item["args"] == ["srv", "uptime", True],
       "1. confirm() stores a named action with JSON args, not a closure")
    ok("uptime" in m.text and token in m.keyboard, "1a. the confirm message carries the token on its buttons")

    bot.PENDING = restart(bot.PENDING)
    item2 = bot.PENDING.get(token)
    ok(item2 and item2["act"] == "sh", "1b. the pending confirmation is still there after a restart")

    item2["ts"] = time.time() - bot.CONFIRM_TTL - 1
    bot.PENDING.touch()
    bot.PENDING = restart(bot.PENDING)
    bot.PENDING.sweep()
    ok(bot.PENDING.get(token) is None, "1c. an expired confirmation is swept, not run")

    # the ok/no handler resolves through ACTIONS only
    ok(set(bot.ACTIONS) == {"sh", "reboot", "run", "ban", "broadcast", "del", "get"},
       "1d. ACTIONS registry holds exactly the seven confirmable actions")
    ok(all(callable(f) for f in bot.ACTIONS.values()), "1e. every action is a resolvable function")


# ---------------------------------------------------------------- 2. pending password survives a restart
async def t_auth():
    t = dict(scheme="ssh", host="h", port=22, user="root", secret=None, cwd="")
    bot.PENDING_AUTH[UID] = dict(name="newsrv", ts=time.time(), **t)
    bot.PENDING_AUTH = restart(bot.PENDING_AUTH)
    p = bot.PENDING_AUTH.get(UID)
    ok(p and p["host"] == "h" and p["secret"] is None,
       "2. a password prompt waiting mid-add survives the restart (no silent re-prompt loop)")

    p["ts"] = time.time() - 301
    bot.PENDING_AUTH.touch()
    bot.PENDING_AUTH = restart(bot.PENDING_AUTH)
    bot.PENDING_AUTH.sweep()
    ok(bot.PENDING_AUTH.get(UID) is None, "2b. auth entries expire after 5 minutes")


# ---------------------------------------------------------------- 3. /multi collections survive a restart
async def t_collect():
    bot.COLLECT[UID] = dict(name="movie.mkv", server="srv", items=[], status=None, ts=time.time())
    col = bot.COLLECT[UID]
    for mid, fn, sz in ((11, "movie.mkv.001", 100), (12, "movie.mkv.002", 200)):
        col["items"].append((mid, fn, sz))
        col["ts"] = time.time()
        bot.COLLECT.touch()
    bot.COLLECT = restart(bot.COLLECT)
    col2 = bot.COLLECT.get(UID)
    ok(col2 and [i[1] for i in col2["items"]] == ["movie.mkv.001", "movie.mkv.002"],
       "3. a half-finished /multi collection survives the restart")
    ordered, base, problem = bot.order_volumes(col2["items"])
    ok(problem is None and base == "movie.mkv" and [i[1] for i in ordered] == ["movie.mkv.001", "movie.mkv.002"],
       "3b. reloaded items (JSON lists) still order and validate like fresh tuples")

    col2["ts"] = time.time() - bot.COLLECT_IDLE - 1
    bot.COLLECT.touch()
    bot.COLLECT = restart(bot.COLLECT)
    bot.COLLECT.sweep()
    ok(bot.COLLECT.get(UID) is None, "3c. idle collections fall out after COLLECT_IDLE")


# ---------------------------------------------------------------- 4. /ask pickers survive a restart
async def t_pick():
    mine = bot.PICK.setdefault(UID, {"ts": 0.0, "m": {}})["m"]
    mine["77"] = dict(chat_id=UID, msg_id=55, name="f.bin", size=10, cap_dest=None, group="files",
                      ts=time.time())
    bot.PICK[UID]["ts"] = time.time()
    bot.PICK.touch()
    bot.PICK = restart(bot.PICK)
    mine = (bot.PICK.get(UID) or {}).get("m") or {}
    info = mine.get("77")
    ok(info and info["msg_id"] == 55 and info["name"] == "f.bin",
       "4. an unanswered server choice survives the restart (string keys, JSON round trip)")

    mine["77"]["ts"] = time.time() - bot.PICK_IDLE - 1
    bot.PICK[UID]["ts"] = time.time() - bot.PICK_IDLE - 1
    bot.PICK.touch()
    bot.PICK = restart(bot.PICK)
    bot.PICK.sweep()
    ok(bot.PICK.get(UID) is None, "4b. stale picker entries are swept (uid level, so the whole basket goes)")


# ---------------------------------------------------------------- 5. terminal mode survives a restart
async def t_term():
    bot.TERM[UID] = time.time()
    bot.TERM = restart(bot.TERM)
    ok(UID in bot.TERM and isinstance(bot.TERM[UID], float),
       "5. terminal mode survives the restart (the user is not silently dropped to chat mode)")

    bot.TERM[UID] = time.time() - bot.TERM_IDLE - 1
    bot.TERM.flush(force=True)
    bot.TERM = restart(bot.TERM)
    bot.TERM.sweep()
    ok(UID not in bot.TERM, "5b. terminal mode expires after TERM_IDLE idle seconds")


# ---------------------------------------------------------------- 6. monitor issue state survives a restart
async def t_monitor():
    d_new, d_cleared = bot.diff_issues({}, {"disk": "disk 91 full"})
    ok(d_new == ["disk 91 full"] and d_cleared == [], "6. diff_issues reports a new problem")
    d_new, d_cleared = bot.diff_issues({"disk": "disk 91 full"}, {"disk": "disk 91 full"})
    ok(d_new == [] and d_cleared == [], "6b. an unchanged problem is not re-reported")
    d_new, d_cleared = bot.diff_issues({"disk": "disk 91 full"}, {})
    ok(d_cleared == ["disk"], "6c. a solved problem is reported as recovered")

    st = persist.State("monitor")
    st[f"{UID}:srv"] = {"disk": "disk 91 full"}
    st2 = restart(st)
    ok(st2.get(f"{UID}:srv") == {"disk": "disk 91 full"},
       "6d. a monitor issue known before the restart is not re-alerted after it")


# ---------------------------------------------------------------- 7. stop intents survive a restart
async def t_cancel():
    jid = store.add_job(UID, "srv", UID, "tgm:1:2", "x.bin", "x.bin", 10)
    store.set_job(jid, state="running")
    jobs.CANCEL.add(jid)
    ok(jid in jobs.CANCEL and store.get_job(jid)["state"] == "running",
       "7. a stop intent is recorded without touching the running job row yet")

    # simulate the restart: the persisted blob is all a new process sees
    jobs.CANCEL._st.flush(force=True)
    eng = jobs.Engine(None, 0)
    stopped = jobs.CANCEL.drain()
    j = store.get_job(jid)
    ok(stopped == 1 and j["state"] == "cancelled" and "restart" in (j["error"] or ""),
       "7b. drain() cancels the job the user asked to stop before the restart")
    ok(jid not in jobs.CANCEL, "7c. the drained id is cleared from the live set")

    jid2 = store.add_job(UID, "srv", UID, "tgm:1:3", "y.bin", "y.bin", 10)
    jobs.CANCEL.drain()  # nothing pending: no side effects
    ok(store.get_job(jid2)["state"] == "queued", "7d. drain() with nothing pending leaves jobs alone")

    jobs.CANCEL.add(jid2)
    store.set_job(jid2, state="done")
    jobs.CANCEL._st.flush(force=True)
    ok(jobs.CANCEL.drain() == 0 and store.get_job(jid2)["state"] == "done",
       "7e. an id whose job finished meanwhile is dropped, not cancelled")


# ---------------------------------------------------------------- 8. web upload sessions survive a restart
async def t_uploads():
    import webapi
    os.makedirs(webapi.STAGE_DIR, exist_ok=True)  # up/init makes it before any chunk lands
    tok = "tok123"
    webapi.UPLOADS[tok] = {"uid": UID, "name": "a.iso", "size": 1000, "ts": time.time()}
    webapi.UPLOADS = restart(webapi.UPLOADS)
    up = webapi.UPLOADS.get(tok)
    ok(up and up["uid"] == UID and up["size"] == 1000,
       "8. an in-progress Mini App upload session survives the restart")

    # a token whose staged file vanished must not resurrect a from-zero upload
    open(webapi.stage_path(tok), "wb").write(b"partial")  # the survivor's bytes are still on disk
    webapi.UPLOADS["gone"] = {"uid": UID, "name": "b.iso", "size": 5, "ts": time.time()}
    webapi.UPLOADS.flush(force=True)
    webapi._prune_uploads()
    ok("gone" not in webapi.UPLOADS and tok in webapi.UPLOADS,
       "8b. prune drops only tokens with no staged file behind them")


class FakeMsg:
    """Just enough message for confirm(): a reply that remembers its keyboard."""
    _ids = itertools.count(900)

    def __init__(self):
        self.id = next(FakeMsg._ids)
        self.text = ""
        self.keyboard = ""

    async def reply(self, text, reply_markup=None, **kw):
        self.text = text
        if reply_markup is not None:
            self.keyboard = reply_markup.inline_keyboard[0][0].callback_data
        return self


async def main():
    await t_confirm()
    await t_auth()
    await t_collect()
    await t_pick()
    await t_term()
    await t_monitor()
    await t_cancel()
    await t_uploads()
    print("ALL STATE CHECKS PASSED")


asyncio.run(main())
