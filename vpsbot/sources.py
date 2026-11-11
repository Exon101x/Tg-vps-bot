"""Streaming upload sources. Readers are used from worker threads and pull data on demand,
so a slow server automatically slows the download (backpressure) and nothing touches the local disk."""
import asyncio
import ipaddress
import socket
import urllib.request
from bisect import bisect_right
from collections import deque
from itertools import accumulate
from urllib.parse import urlparse

from perf import SETTINGS

CHUNK = 1024 * 1024  # Telegram's stream_media chunk size
TG_PARALLEL = SETTINGS["tg_parallel"]  # windows downloaded at the same time
TG_WINDOW = SETTINGS["tg_window_mb"]   # MiB per window


class Cancelled(Exception):
    pass


class SourceGone(Exception):
    """The source can't be read any more (message deleted, ...). Retrying won't help."""


class Reader:
    """File-like source. `total` is the full size in bytes (0 if unknown); `pos` counts from the start of the
    file, so progress stays correct when a transfer resumes at an offset."""

    total = 0

    def __init__(self, offset, on_progress, cancelled):
        self.pos = offset
        self.on_progress = on_progress
        self.cancelled = cancelled

    def _advance(self, n):
        self.pos += n
        if self.cancelled():
            raise Cancelled()
        self.on_progress(self.pos, self.total)

    def close(self):
        pass


class TgReader(Reader):
    """Streams a Telegram file. Windows of TG_WINDOW MiB are requested TG_PARALLEL at a time and consumed in order,
    which is several times faster than one sequential stream for large files while memory stays at roughly
    TG_PARALLEL x TG_WINDOW MiB. Every window is length-checked, so a short answer can never silently corrupt a file."""

    def __init__(self, client, msg, loop, offset, on_progress, cancelled, workers=None, window=None):
        super().__init__(offset, on_progress, cancelled)
        media = getattr(msg, msg.media.value)
        self.total = getattr(media, "file_size", 0) or 0
        self._client, self._msg, self._loop = client, msg, loop
        self._workers, self._window = workers or TG_PARALLEL, window or TG_WINDOW
        first = offset // CHUNK  # requests can only start on a chunk boundary
        self._skip = offset - first * CHUNK  # bytes of that chunk the server already has
        self._chunks = -(-self.total // CHUNK) if self.total else 0
        self._next = first  # first chunk of the next window to request
        self._pending = deque()  # (future, expected length), oldest first
        self._buf = bytearray()
        self._eof = False
        self._agen = None if self.total else client.stream_media(msg, offset=first)  # unknown size: plain sequential stream

    async def _fetch_window(self, start, count):
        parts = []
        async for chunk in self._client.stream_media(self._msg, limit=count, offset=start):
            parts.append(chunk)
        return b"".join(parts)

    async def _next_sequential(self):
        try:
            return await self._agen.__anext__()
        except StopAsyncIteration:
            return None

    def _fill(self):
        while len(self._pending) < self._workers and self._next < self._chunks:
            count = min(self._window, self._chunks - self._next)
            expected = min(count * CHUNK, self.total - self._next * CHUNK)
            fut = asyncio.run_coroutine_threadsafe(self._fetch_window(self._next, count), self._loop)
            self._pending.append((fut, expected))
            self._next += count

    def _more(self):
        if self._agen is not None:
            return asyncio.run_coroutine_threadsafe(self._next_sequential(), self._loop).result(timeout=180)
        self._fill()
        if not self._pending:
            return None
        fut, expected = self._pending.popleft()
        data = fut.result(timeout=300)
        self._fill()  # keep the pipeline full while this window is consumed
        if len(data) != expected:
            raise IOError(f"Telegram returned {len(data)} bytes for a window of {expected}")
        return data

    def read(self, size=-1):
        while not self._eof and (size < 0 or len(self._buf) < size):
            chunk = self._more()
            if chunk is None:
                self._eof = True
                break
            if self._skip:
                drop = min(self._skip, len(chunk))
                chunk, self._skip = chunk[drop:], self._skip - drop
            self._buf += chunk
        if size < 0 or size > len(self._buf):
            size = len(self._buf)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        if out:
            self._advance(len(out))
        return out

    def close(self):
        for fut, _ in self._pending:
            fut.cancel()
        self._pending.clear()
        if self._agen is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._agen.aclose(), self._loop).result(10)
            except Exception:
                pass


class MultiTgReader(Reader):
    """Several Telegram files read back to back as ONE logical file: the volumes of something bigger than Telegram's
    2 GB limit. Supports resuming at any byte offset (it jumps straight into the right volume)."""

    def __init__(self, client, msgs, loop, offset, on_progress, cancelled):
        super().__init__(offset, on_progress, cancelled)
        self._client, self._loop, self._msgs = client, loop, list(msgs)
        self._sizes = [getattr(getattr(m, m.media.value), "file_size", 0) or 0 for m in self._msgs]
        if not self._sizes or not all(self._sizes):
            raise ValueError("a volume has an unknown size")
        self.total = sum(self._sizes)
        self._starts = list(accumulate([0] + self._sizes[:-1]))
        self._idx, self._cur = len(self._msgs), None
        self._open_at(offset)

    def _open_at(self, pos):
        if pos >= self.total:
            self._idx, self._cur = len(self._msgs), None
            return
        self._idx = bisect_right(self._starts, pos) - 1
        start = self._starts[self._idx]
        self._cur = TgReader(self._client, self._msgs[self._idx], self._loop, pos - start,
                             lambda p, t, s=start: self._sub_progress(s + p), self.cancelled)

    def _sub_progress(self, absolute):
        self.pos = absolute
        self.on_progress(self.pos, self.total)

    def read(self, size=-1):
        out = bytearray()
        while self._cur is not None and (size < 0 or len(out) < size):
            data = self._cur.read(-1 if size < 0 else size - len(out))
            if data:
                out += data
                continue
            end = self._starts[self._idx] + self._sizes[self._idx]  # this volume is exhausted: it must be complete
            if self.pos != end:
                raise IOError(f"volume {self._idx + 1} ended early ({self.pos - self._starts[self._idx]} of {self._sizes[self._idx]} bytes)")
            self._cur.close()
            self._open_at(end)
        return bytes(out)

    def close(self):
        if self._cur is not None:
            self._cur.close()


def check_public(url):
    """Best-effort SSRF guard for URLs fetched by the bot itself."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Only http(s) URLs are allowed.")
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Refusing to fetch from a private/internal address.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpReader(Reader):
    def __init__(self, url, offset, on_progress, cancelled, max_bytes):
        super().__init__(offset, on_progress, cancelled)
        check_public(url)
        headers = {"User-Agent": "Mozilla/5.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        self._resp = urllib.request.build_opener(_SafeRedirect).open(urllib.request.Request(url, headers=headers), timeout=30)
        length = int(self._resp.headers.get("Content-Length") or 0)
        self._skip = 0
        if offset and self._resp.status != 206:  # server ignored Range: re-read and discard what it already has
            self._skip, self.total = offset, length
        else:
            self.total = length + offset if length else 0
        if max_bytes and self.total > max_bytes:  # 0 = no limit
            raise ValueError("File is larger than MAX_FETCH_MB")

    def read(self, size=-1):
        size = size if size and size > 0 else CHUNK
        while self._skip:
            dropped = self._resp.read(min(self._skip, 256 * 1024))
            if not dropped:
                break
            self._skip -= len(dropped)
        data = self._resp.read(size)
        if data:
            self._advance(len(data))
        return data

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass
