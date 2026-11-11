"""Blocking FTP / FTPS / SSH (SFTP + shell) client. Call from asyncio.to_thread."""
import base64
import errno
import ftplib
import hashlib
import io
import posixpath
import shlex
import socket
import stat
import time

import paramiko

import store
from perf import MiB, SETTINGS

MAX_OUT = 1_000_000  # bytes of command output kept
SFTP_WINDOW = SETTINGS["sftp_window_mb"] * MiB  # bigger window = faster SFTP over high-latency links
FTP_BLOCK = SETTINGS["ftp_block_kb"] * 1024
MARK = "__BOT_CWD__"


class HostKeyChanged(Exception):
    pass


class RenameFailed(Exception):
    """The data is on the server as <name>.part but the server refused the final rename."""


def fingerprint(hostkey):
    try:
        raw = base64.b64decode(hostkey.split()[1])
        return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    except Exception:
        return "?"


def load_pkey(text):
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text))
        except Exception:
            continue
    raise ValueError("Unsupported or passphrase-protected key. Use an unencrypted ed25519/RSA/ECDSA private key.")


class _TOFU(paramiko.MissingHostKeyPolicy):
    """Trust on first use: pin the host key on first connect, refuse the connection if it ever changes.
    Runs during key exchange, i.e. before any credentials are sent."""

    def __init__(self, c):
        self.c = c

    def missing_host_key(self, client, hostname, key):
        seen = f"{key.get_name()} {key.get_base64()}"
        known = self.c.get("hostkey")
        if not known:
            self.c["hostkey"] = seen
            store.update(self.c["uid"], self.c["name"], hostkey=seen)  # no-op while the server is being added
        elif known != seen:
            raise HostKeyChanged(
                f"HOST KEY CHANGED for {hostname}. Pinned {fingerprint(known)}, got {fingerprint(seen)}. "
                "Possible man-in-the-middle, or the server was reinstalled (re-add it with /add to trust the new key)."
            )


class Counting:
    """File wrapper reporting cumulative bytes read/written (for progress bars)."""

    def __init__(self, f, cb):
        self.f, self.cb, self.n = f, cb, 0

    def read(self, size=-1):
        data = self.f.read(size)
        self.n += len(data)
        self.cb(self.n)
        return data

    def write(self, data):
        self.f.write(data)
        self.n += len(data)
        self.cb(self.n)
        return len(data)


class Remote:
    def __init__(self, c):
        self.c = c
        self.ssh = c["scheme"] == "ssh"
        self._sftp_cli = None

    # ---- connection
    def __enter__(self):
        c = self.c
        if self.ssh:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(_TOFU(c))
            kw = dict(hostname=c["host"], port=c["port"], username=c["user"], timeout=20, banner_timeout=20,
                      auth_timeout=20, allow_agent=False, look_for_keys=False)
            if c["auth"] == "key":
                kw["pkey"] = load_pkey(c["secret"])
            else:
                kw["password"] = c["secret"]
            self.client.connect(**kw)
            self.client.get_transport().set_keepalive(30)
        else:
            tls = c["scheme"] == "ftps"  # explicit TLS (AUTH TLS)
            self.cli = (ftplib.FTP_TLS if tls else ftplib.FTP)()
            self.cli.encoding = "utf-8"
            self.cli.connect(c["host"], c["port"], timeout=60)
            # long transfers leave the control connection idle; keepalives stop NATs/firewalls from dropping it
            self.cli.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.cli.login(c["user"], c["secret"])
            if tls:
                self.cli.prot_p()
            self.cli.voidcmd("TYPE I")  # many servers refuse SIZE (needed for resume) in ASCII mode
            if c.get("cwd"):
                self.cli.cwd(c["cwd"])
        return self

    def __exit__(self, *exc):
        try:
            if self.ssh:
                self.client.close()
            else:
                self.cli.quit()
        except Exception:
            pass

    def _sftp(self):
        # opened lazily so shell-only VPSes with SFTP disabled still work
        if self._sftp_cli is None:
            cli = paramiko.SFTPClient.from_transport(self.client.get_transport(), window_size=SFTP_WINDOW)
            if cli is None:
                raise paramiko.SSHException("the server did not open an SFTP channel")
            cwd = self.c.get("cwd")
            if cwd:
                try:
                    cli.chdir(cwd)
                except IOError as e:
                    raise FileNotFoundError(errno.ENOENT, f"saved directory {cwd} is not accessible ({e}); use /cd to pick another") from e
            self._sftp_cli = cli  # only cached once the chdir worked, so a failure can't be silently skipped
        return self._sftp_cli

    # ---- navigation
    def probe(self):
        """Connectivity check; returns the starting directory."""
        if self.ssh:
            cwd = self.c.get("cwd")
            out, rc = self.exec(f"cd {shlex.quote(cwd)} && pwd" if cwd else "pwd", 20)
            if rc:
                raise RuntimeError(out.strip() or "cd failed")
            return out.strip()
        return self.cli.pwd()

    def cd(self, path):
        """Change directory; returns the new absolute path."""
        if self.ssh:
            cwd = self.c.get("cwd")
            pre = f"cd {shlex.quote(cwd)} 2>/dev/null; " if cwd else ""
            target = "" if path == "~" else shlex.quote(path)
            out, rc = self.exec(f"{pre}cd {target} && pwd", 20)
            if rc:
                raise RuntimeError(out.strip() or f"cannot cd to {path}")
            return out.strip()
        self.cli.cwd(path)
        return self.cli.pwd()

    def ls(self, path=""):
        """Returns [(name, is_dir, size)], folders first."""
        if self.ssh:
            rows = [(a.filename, stat.S_ISDIR(a.st_mode), a.st_size) for a in self._sftp().listdir_attr(path or ".")]
        else:
            try:
                rows = [(n, f.get("type") == "dir", int(f.get("size", 0)))
                        for n, f in self.cli.mlsd(path, facts=["type", "size"]) if f.get("type") in ("file", "dir")]
            except ftplib.error_perm:  # server has no MLSD
                rows = [(n, False, 0) for n in (self.cli.nlst(path) if path else self.cli.nlst())]
        return sorted(rows, key=lambda r: (not r[1], r[0].lower()))

    # ---- file operations
    def size(self, path):
        """Size in bytes, or 0 if the file does not exist."""
        try:
            if self.ssh:
                return self._sftp().stat(path).st_size
            return self.cli.size(path) or 0
        except ftplib.error_perm:
            return 0
        except IOError as e:
            if e.errno == errno.ENOENT:
                return 0
            raise

    def exists(self, path):
        try:
            if self.ssh:
                self._sftp().stat(path)
                return True
            return self.cli.size(path) is not None
        except ftplib.error_perm:
            return False
        except IOError as e:
            if e.errno == errno.ENOENT:
                return False
            raise

    def makedirs(self, path):
        """mkdir -p, relative to the current directory unless path is absolute."""
        cur = "/" if path.startswith("/") else ""
        for part in (p for p in path.split("/") if p):
            cur = posixpath.join(cur, part)
            if self.ssh:
                try:
                    self._sftp().stat(cur)
                except IOError:
                    self._sftp().mkdir(cur)
            else:
                try:
                    self.cli.mkd(cur)
                except ftplib.error_perm:
                    pass  # already exists (or no permission - the upload will report it)

    def unique_name(self, dest):
        """dest, or 'name (1).ext', 'name (2).ext'... if it already exists."""
        if not self.exists(dest):
            return dest
        stem, ext = posixpath.splitext(dest)
        for i in range(1, 100):
            cand = f"{stem} ({i}){ext}"
            if not self.exists(cand):
                return cand
        raise ValueError(f"too many copies of {dest}")

    def upload(self, make_reader, dest, resume=False):
        """Stream into <dest>.part, verify the size, then rename to dest.
        make_reader(offset) must return an object with read(n), close() and .total.
        With resume=True an existing .part is continued from where it stopped."""
        part = dest + ".part"
        offset = self.size(part) if resume else 0
        reader = make_reader(offset)
        try:
            if reader.total and offset > reader.total:  # stale, larger partial: start over
                reader.close()
                offset, reader = 0, make_reader(0)
            if not (reader.total and offset == reader.total):
                self._stream(reader, part, offset)
            got = self.size(part)
            if reader.total and got and got != reader.total:
                raise IOError(f"size mismatch on server ({got} of {reader.total} bytes)")
        finally:
            reader.close()
        self._finalize(part, dest)

    def _stream(self, reader, part, offset):
        if self.ssh:
            f = self._sftp().open(part, "ab" if offset else "wb")
            try:
                f.set_pipelined(True)
                while True:
                    data = reader.read(1024 * 1024)
                    if not data:
                        break
                    f.write(data)
            finally:
                f.close()
        else:
            self.cli.storbinary(f"{'APPE' if offset else 'STOR'} {part}", reader, FTP_BLOCK)

    def _finalize(self, part, dest):
        try:
            if self.ssh:
                try:
                    self._sftp().posix_rename(part, dest)  # atomic, replaces dest
                except IOError:
                    try:
                        self._sftp().remove(dest)
                    except IOError:
                        pass
                    self._sftp().rename(part, dest)
            else:
                try:
                    self.cli.delete(dest)
                except ftplib.error_perm:
                    pass
                self.cli.rename(part, dest)
        except Exception as e:
            raise RenameFailed(f"uploaded, but the server refused to rename {part} to {dest}: {e}") from e

    def put(self, fileobj, dest):
        if self.ssh:
            self._sftp().putfo(fileobj, dest)
        else:
            self.cli.storbinary(f"STOR {dest}", fileobj, 64 * 1024)

    def get(self, path, fileobj):
        if self.ssh:
            self._sftp().getfo(path, fileobj)
        else:
            self.cli.retrbinary(f"RETR {path}", fileobj.write, 64 * 1024)

    def read_range(self, path, offset, length, fileobj):
        """Copy `length` bytes starting at `offset` into fileobj (SFTP: seek + prefetch; FTP: REST)."""
        remaining = length
        if self.ssh:
            with self._sftp().open(path, "rb") as f:
                f.seek(offset)
                f.prefetch(offset + length)  # pipelined read-ahead: far faster than one request per block
                while remaining:
                    data = f.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    fileobj.write(data)
                    remaining -= len(data)
        else:
            conn = self.cli.transfercmd(f"RETR {path}", rest=offset or None)
            try:
                while remaining:
                    data = conn.recv(min(FTP_BLOCK, remaining))
                    if not data:
                        break
                    fileobj.write(data)
                    remaining -= len(data)
            finally:
                conn.close()
            try:
                self.cli.voidresp()  # 226, or 426/451 when we stopped before the end of the file
            except (ftplib.Error, EOFError, OSError):
                pass
        if remaining:
            raise EOFError(f"{path} is shorter than expected ({remaining} bytes missing)")

    def mkdir(self, path):
        if self.ssh:
            self._sftp().mkdir(path)
        else:
            self.cli.mkd(path)

    def remove(self, path):
        """Delete a file, or an empty directory."""
        if self.ssh:
            try:
                self._sftp().remove(path)
            except IOError:
                self._sftp().rmdir(path)
        else:
            try:
                self.cli.delete(path)
            except ftplib.error_perm:
                self.cli.rmd(path)

    def rename(self, src, dst):
        (self._sftp() if self.ssh else self.cli).rename(src, dst)

    # ---- shell (ssh only)
    def exec(self, cmd, timeout=60):
        """Run a command; returns (combined stdout+stderr, exit code)."""
        if not self.ssh:
            raise RuntimeError("Shell commands need an SSH server (ssh:// or sftp://).")
        chan = self.client.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(cmd)
        buf, end = bytearray(), time.time() + timeout
        while True:
            if chan.recv_ready():
                data = chan.recv(65536)
                if not data:
                    break
                buf += data
                if len(buf) > MAX_OUT:
                    chan.close()
                    return buf.decode(errors="replace") + "\n[output truncated]", 1
            elif chan.exit_status_ready():
                break
            elif time.time() > end:
                chan.close()
                return buf.decode(errors="replace") + f"\n[timed out after {timeout}s]", 124
            else:
                time.sleep(0.05)
        while chan.recv_ready():
            buf += chan.recv(65536)
        return buf.decode(errors="replace"), chan.recv_exit_status()

    def shell(self, cmd, cwd=None, timeout=120):
        """Run cmd inside cwd and report the directory it ended in. Returns (output, rc, new_cwd)."""
        pre = f"cd {shlex.quote(cwd)} 2>/dev/null; " if cwd else ""
        script = f'{pre}{{ {cmd}\n}}; __rc=$?; echo; echo {MARK}"$(pwd)"; exit $__rc'
        out, rc = self.exec(script, timeout)
        newcwd = None
        i = out.rfind(MARK)
        if i != -1:
            tail = out[i + len(MARK):].strip().splitlines()
            newcwd = tail[0] if tail else None
            out = out[:i].rstrip("\n")
        return out, rc, newcwd
