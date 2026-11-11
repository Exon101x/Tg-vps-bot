"""Keep the Pyrogram session (auth key + peer cache) in the database so the bot survives hosts with ephemeral disks.

On start-up, if there is no local session file, the last snapshot is restored from the database. While running, a
consistent copy of the session file is saved (encrypted) every few minutes and on shutdown.

Run only ONE instance per bot: two processes sharing a session get AUTH_KEY_DUPLICATED. On platforms that do rolling
deploys, use a "recreate"/stop-before-start strategy.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
import tempfile
import time
import uuid

import store
from util import env_int

NAME = "vpsbot"
DIR = os.getenv("SESSION_DIR", ".")
INTERVAL = env_int("SESSION_SYNC_INTERVAL", 300, lo=10)  # a typo must not crash the bot at import (same rule as bot.py)
PATH = os.path.join(DIR, NAME + ".session")
LOCK_TTL = env_int("LOCK_TTL", 60, lo=5)  # a lock nobody refreshed for this long is considered abandoned
HEARTBEAT = 15
INSTANCE = uuid.uuid4().hex[:12]
_last = [0.0]


def _token_id():
    return hashlib.sha256(os.environ["BOT_TOKEN"].encode()).hexdigest()[:16]


def restore():
    """Call before creating the Client. Returns a short description of where the session came from."""
    os.makedirs(DIR, exist_ok=True)
    if os.path.exists(PATH):
        return "local session file"
    if os.getenv("RESET_SESSION"):
        return "fresh login (RESET_SESSION set)"
    blob = store.load_blob("session")
    if blob and store.kv_get("session_token") == _token_id():  # a snapshot of a different bot token is useless
        with open(PATH, "wb") as f:
            f.write(blob)
        return "restored from database"
    return "fresh login"


def snapshot(force=False):
    """Save a consistent copy of the live session file into the database. Returns True if something was saved."""
    if not os.path.exists(PATH):
        return False
    mtime = os.path.getmtime(PATH)
    if not force and mtime == _last[0]:
        return False
    src = sqlite3.connect(PATH, timeout=10)
    fd, tmp = tempfile.mkstemp(suffix=".session")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)  # safe while Pyrogram is using the file
        finally:
            dst.close()
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        src.close()
        os.remove(tmp)
    store.save_blob("session", data)
    store.kv_set("session_token", _token_id())
    _last[0] = mtime
    return True


async def sync_loop():
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await asyncio.to_thread(snapshot)
        except Exception:
            logging.exception("session snapshot failed")


# ---- single-instance lock
# Platforms with zero-downtime deploys (Render, ...) start the new copy while the old one is still connected. Two
# processes on one Telegram session get AUTH_KEY_DUPLICATED, which can invalidate the session. So the new copy waits
# here (its health endpoint is already up) until the old one has released the lock on shutdown or gone quiet.
def _read_lock():
    owner, _, ts = (store.kv_get("lock") or "").partition("|")
    try:
        return owner, float(ts)
    except ValueError:
        return "", 0.0


def acquire_lock():
    """Block until this process owns the bot."""
    if os.getenv("DISABLE_LOCK"):
        return
    announced = False
    while True:
        owner, ts = _read_lock()
        if not owner or owner == INSTANCE or time.time() - ts > LOCK_TTL:
            store.kv_set("lock", f"{INSTANCE}|{time.time()}")
            time.sleep(3)  # if another copy raced us, whoever wrote last wins; confirm that it was us
            if _read_lock()[0] == INSTANCE:
                return
        elif not announced:
            logging.info("another instance (%s) is running; waiting for it to stop", owner)
            announced = True
        time.sleep(3)


def release_lock():
    if os.getenv("DISABLE_LOCK"):
        return
    if _read_lock()[0] == INSTANCE:
        store.kv_set("lock", f"|{time.time()}")


async def heartbeat_loop(on_lost):
    while not os.getenv("DISABLE_LOCK"):
        await asyncio.sleep(HEARTBEAT)
        try:
            if (await asyncio.to_thread(_read_lock))[0] != INSTANCE:
                return on_lost()
            await asyncio.to_thread(store.kv_set, "lock", f"{INSTANCE}|{time.time()}")
        except Exception:
            logging.exception("lock heartbeat failed")


def discard():
    """Forget a session Telegram rejected (revoked, duplicated), locally and in the database."""
    if os.path.exists(PATH):
        os.remove(PATH)
    store.kv_set("session_token", "")
