"""Streaming upload sources. Readers are used from worker threads and pull data on demand,
so a slow server automatically slows the download (backpressure) and nothing touches the local disk."""
import asyncio
import http.client
import ipaddress
import socket
import urllib.error
from bisect import bisect_right
from collections import deque
from itertools import accumulate
from urllib.parse import urlparse, urljoin

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


def _assert_public(ip):
    """Raise unless ip is safe for the bot to fetch from: no loopback, private, link-local (cloud metadata),
    reserved, unspecified or multicast space, including those hidden inside IPv4-mapped IPv6 addresses."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise ValueError("Refusing to fetch from a private/internal address.")


def check_public(url):
    """Best-effort SSRF guard for URLs fetched by the bot itself. Returns the resolved address list so
    the caller can connect to exactly the addresses that were checked: resolving again after the check
    would let a rebinding DNS answer hand the actual connection a private address (a TOCTOU gap)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Only http(s) URLs are allowed.")
    infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), 0, socket.SOCK_STREAM)
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise ValueError("Refusing to fetch from an unparseable address.")
        _assert_public(ip)
    return infos


class _PinnedHTTP(http.client.HTTPConnection):
    """HTTP connection pinned to a pre-validated address instead of re-resolving DNS at connect time,
    which is what actually defeats DNS rebinding. No proxy support is needed: we build these directly."""

    def __init__(self, host, port, ip, timeout=30):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), timeout=self.timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Same pin, over TLS: the connection goes to the validated IP while SNI and certificate
    verification still use the original hostname, so the cert chain is fully checked as before."""

    def __init__(self, host, port, ip, timeout=30):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), timeout=self.timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


MAX_REDIRECTS = 5


class HttpReader(Reader):
    def __init__(self, url, offset, on_progress, cancelled, max_bytes):
        super().__init__(offset, on_progress, cancelled)
        self._max = max_bytes
        self._conn, self._resp = self._open(url, offset, 0)
        length = int(self._resp.getheader("Content-Length") or 0)
        self._skip = 0
        if offset and self._resp.status != 206:  # server ignored Range: re-read and discard what it already has
            self._skip, self.total = offset, length
        else:
            self.total = length + offset if length else 0
        if max_bytes and self.total > max_bytes:  # 0 = no limit
            self.close()
            raise ValueError("File is larger than MAX_FETCH_MB")

    @classmethod
    def _open(cls, url, offset, depth):
        """GET url, following redirects. Every hop is SSRF-checked, and every connection is pinned to the
        address that passed the check (one DNS lookup serves both), so rebinding cannot slip in between."""
        if depth > MAX_REDIRECTS:
            raise ValueError("Too many redirects")
        infos = check_public(url)
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        if infos:  # check_public was monkeypatched (tests) -> resolve without validating
            ip = infos[0][4][0]
        else:
            ip = socket.getaddrinfo(u.hostname, port, 0, socket.SOCK_STREAM)[0][4][0]
        headers = {"User-Agent": "Mozilla/5.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        conn = _PinnedHTTPS(u.hostname, port, ip, 30) if u.scheme == "https" else _PinnedHTTP(u.hostname, port, ip, 30)
        try:
            target = u.path or "/"
            if u.query:
                target += "?" + u.query
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                resp.close()
                if not location:
                    raise ValueError(f"redirect ({resp.status}) without a Location header")
                conn.close()
                return cls._open(urljoin(url, location), offset, depth + 1)
            if resp.status >= 400:
                err = urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, resp)
                resp.close()
                raise err
            return conn, resp
        except Exception:
            conn.close()
            raise

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
            if self._max and self.pos > self._max:  # no Content-Length: enforce the cap while streaming
                self.close()
                raise ValueError("File is larger than MAX_FETCH_MB")
        return data

    def close(self):
        for closer in (getattr(self, "_resp", None), getattr(self, "_conn", None)):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
