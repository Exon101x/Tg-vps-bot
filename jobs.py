"""Persistent upload queue.

Streams Telegram media / URLs / Mini App uploads straight to the server, retries transient failures
with backoff and resumes from the partial file already on the server. Jobs live in the database, so a
queue survives restarts: the source is (chat_id, message_id), a URL, or a file the Mini App staged
under the temp directory (src "web:<path>").
"""
import asyncio
import errno
import ftplib
import logging
import os
import posixpath
import time
import urllib.error

import paramiko
from pyrogram.errors import FloodWait

import store
from perf import SETTINGS
from remote import HostKeyChanged, Remote, RenameFailed
from sources import Cancelled, HttpReader, MultiTgReader, SourceGone, StagedReader, TgReader
from util import bar, env_int, eta, human

MAX_RETRIES = env_int("MAX_RETRIES", 5, lo=1)
PARALLEL = SETTINGS["per_server"]
BACKOFF = (5, 15, 45, 120, 300)  # seconds to wait before attempt 2, 3, ...
CANCEL = set()  # ids of running jobs the user asked to stop


def is_permanent(e):
    """True if retrying can't help (bad login, permission denied, source gone, ...)."""
    if isinstance(e, (Cancelled, SourceGone, RenameFailed, HostKeyChanged, ValueError, PermissionError,
                      paramiko.AuthenticationException, ftplib.error_perm)):
        return True
    if isinstance(e, urllib.error.HTTPError):
        return e.code < 500 and e.code not in (408, 429)
    if isinstance(e, OSError) and e.errno in (errno.ENOENT, errno.EACCES, errno.ENOSPC, errno.EROFS):
        return True
    return False


class Engine:
    def __init__(self, app, max_fetch):
        self.app, self.max_fetch = app, max_fetch
        self.queues = {}  # (uid, server) -> asyncio.Queue; one runner per server keeps FTP connection limits happy
        self.tasks = []

    # ---- lifecycle
    def start(self):
        """Re-queue everything that was waiting or mid-transfer when the bot last stopped."""
        for j in store.pending_jobs():
            resume = int(j["state"] == "running" or j["attempts"] > 0 or j["resume"])
            store.set_job(j["id"], state="queued", attempts=0, resume=resume)
            self.enqueue(j["id"])

    def enqueue(self, jid):
        job = store.get_job(jid)
        key = (job["uid"], job["server"])
        if key not in self.queues:
            q = self.queues[key] = asyncio.Queue()
            for _ in range(PARALLEL):
                self.tasks.append(asyncio.create_task(self._runner(q)))
        self.queues[key].put_nowait(jid)

    async def stop(self, jid):
        job = store.get_job(jid)
        if not job:
            return
        if job["state"] == "queued":
            store.set_job(jid, state="cancelled")  # the runner skips it when its turn comes
            await self.edit(job, f"✖ {job['name']} stopped")
        elif job["state"] == "running":
            CANCEL.add(jid)

    async def retry(self, jid):
        job = store.get_job(jid)
        if not job or job["state"] not in ("failed", "cancelled"):
            return False
        store.set_job(jid, state="queued", attempts=0, resume=1, error="")
        await self.edit(job, f"⏳ {job['name']} re-queued")
        self.enqueue(jid)
        return True

    # ---- internals
    async def edit(self, job, text):
        if not job.get("status_id"):
            return
        try:
            await self.app.edit_message_text(job["chat_id"], job["status_id"], f"#{job['id']} {text}")
        except Exception:
            pass  # message deleted / not modified / flood wait

    async def _runner(self, q):
        while True:
            jid = await q.get()
            try:
                await self._run(jid)
            except Exception:
                logging.exception("job %s crashed", jid)
                store.set_job(jid, state="failed", error="internal error, see the bot log")

    async def _run(self, jid):
        job = store.get_job(jid)
        if not job or job["state"] != "queued":
            return  # stopped while waiting
        started, attempt = time.time(), 0
        while True:
            attempt += 1
            job = store.get_job(jid)
            if jid in CANCEL:
                return await self._finish(job, "cancelled")
            c = store.get_server(job["uid"], job["server"])
            if not c:
                return await self._finish(job, "failed", "the server was deleted")
            store.set_job(jid, state="running", attempts=attempt)
            job["attempts"] = attempt
            await self.edit(job, f"🔌 {job['name']}: connecting to {c['name']}…")
            try:
                note = await self._attempt(job, c, resume=bool(job["resume"]) or attempt > 1)
                return await self._finish(job, "done", note or "", started)
            except Cancelled:
                return await self._finish(job, "cancelled")
            except Exception as e:
                logging.warning("job %s attempt %s failed: %r", jid, attempt, e)
                reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                if is_permanent(e) or attempt >= MAX_RETRIES:
                    return await self._finish(job, "failed", reason)
                delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                if isinstance(e, FloodWait):
                    delay = max(delay, int(e.value) + 1)
                await self.edit(job, f"⚠️ {job['name']}\n{reason[:200]}\nRetrying in {delay}s (attempt {attempt + 1}/{MAX_RETRIES})")
                for _ in range(delay):
                    if jid in CANCEL:
                        break
                    await asyncio.sleep(1)

    async def _attempt(self, job, c, resume):
        loop, jid = asyncio.get_running_loop(), job["id"]
        last = [0.0]

        def cancelled():
            return jid in CANCEL

        began = [time.time(), None]  # when this attempt started, and how many bytes the server already had then

        def tick(done, total):  # runs in the worker thread
            now = time.time()
            if began[1] is None:
                began[1] = done
            if now - last[0] < 3:
                return
            last[0] = now
            tries = f" (attempt {job['attempts']}/{MAX_RETRIES})" if job["attempts"] > 1 else ""
            rate = (done - began[1]) / max(now - began[0], 0.001)
            speed = f"\n{human(rate)}/s" + (f" · ETA {eta((total - done) / rate)}" if rate > 0 and total else "")
            asyncio.run_coroutine_threadsafe(self.edit(job, f"⬆️ {job['name']}{tries}\n{bar(done, total)}{speed}"), loop)

        if job["src"].startswith("tgm:"):  # volumes of a file bigger than 2 GB, joined on the fly
            _, chat_id, ids = job["src"].split(":", 2)
            id_list, msgs = [int(x) for x in ids.split(",")], []
            for i in range(0, len(id_list), 100):
                msgs += list(await self.app.get_messages(int(chat_id), id_list[i:i + 100]))
            if len(msgs) != len(id_list) or any(m.empty or not m.media for m in msgs):
                raise SourceGone("one of the volumes was deleted from the chat, so the file can no longer be assembled")
            new_reader = lambda off: MultiTgReader(self.app, msgs, loop, off, tick, cancelled)  # noqa: E731
        elif job["src"].startswith("web:"):  # a file the Mini App staged under the temp directory
            path = job["src"][4:]
            if not os.path.isfile(path):  # the staging disk was wiped (redeploy, container restart)
                raise SourceGone("the uploaded copy was lost when the bot restarted - upload the file again")
            new_reader = lambda off: StagedReader(path, off, tick, cancelled)  # noqa: E731
        elif job["src"].startswith("tg:"):
            _, chat_id, msg_id = job["src"].split(":")
            msg = await self.app.get_messages(int(chat_id), int(msg_id))
            if msg.empty or not msg.media:
                raise SourceGone("the original message was deleted, so there is nothing left to stream")
            new_reader = lambda off: TgReader(self.app, msg, loop, off, tick, cancelled)  # noqa: E731
        else:
            url = job["src"][4:]
            new_reader = lambda off: HttpReader(url, off, tick, cancelled, self.max_fetch)  # noqa: E731

        def make_reader(off):
            began[1] = off  # speed is measured from where this attempt (re)started
            r = new_reader(off)
            if r.total and r.total != job["size"]:
                job["size"] = r.total
                store.set_job(jid, size=r.total)
            return r

        def run():
            with Remote(c) as r:
                dest = job["dest"]
                folder = posixpath.dirname(dest)
                if folder:
                    r.makedirs(folder)
                note, policy = "", c.get("dup") or "overwrite"
                if policy != "overwrite" and not resume:
                    if policy == "skip" and r.exists(dest):
                        return f"skipped, {dest} already exists"
                    if policy == "rename":
                        new = r.unique_name(dest)
                        if new != dest:
                            dest, note = new, f"saved as {new}"
                            store.set_job(jid, dest=new)  # later attempts must resume the same .part
                            job["dest"] = new
                r.upload(make_reader, dest, resume)
                return note

        return await asyncio.to_thread(run)

    async def _finish(self, job, state, note="", started=None):
        CANCEL.discard(job["id"])
        store.set_job(job["id"], state=state, error=note)
        if state in ("done", "cancelled") and job["src"].startswith("web:"):
            for path in (job["src"][4:], job["src"][4:] + ".part"):
                try:
                    os.remove(path)
                except OSError:
                    pass  # failed jobs keep their staged file so /retry can use it
        j = store.get_job(job["id"])
        took = f", {int(time.time() - started)}s" if started else ""
        if state == "done" and note.startswith("skipped"):
            text = f"⏭ {j['name']}: {note}"
        elif state == "done":
            text = f"✅ {j['dest']} → {j['server']} ({human(j['size'])}{took})" + (f"\n{note}" if note else "")
        elif state == "cancelled":
            text = f"✖ {j['name']} stopped"
        else:
            text = f"❌ {j['name']}\n{note[:300]}\n/retry {j['id']} to try again"
        await self.edit(j, text)
        if state == "failed":  # a fresh message, so the phone actually buzzes
            try:
                await self.app.send_message(j["chat_id"], f"❌ #{j['id']} {j['name']} failed: {note[:300]}\n/retry {j['id']}")
            except Exception:
                pass
