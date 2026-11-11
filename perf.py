"""Performance profile: parallelism and buffer sizes chosen from what the host has (CPU cores, RAM), so the same code
runs on a free 512 MB instance and on a big server.

PERFORMANCE=auto|light|balanced|turbo (default auto). Individual variables always win over the profile:
TG_PARALLEL, TG_WINDOW_MB, SFTP_WINDOW_MB, FTP_BLOCK_KB, PARALLEL_PER_SERVER.
"""
import math
import os

MiB = 1024 * 1024

PROFILES = {
    #            Telegram windows in flight, MiB per window, SFTP window MiB, FTP block KiB, uploads per server
    "light":    dict(tg_parallel=4, tg_window_mb=8, sftp_window_mb=2, ftp_block_kb=64, per_server=1),
    "balanced": dict(tg_parallel=8, tg_window_mb=8, sftp_window_mb=8, ftp_block_kb=256, per_server=1),
    "turbo":    dict(tg_parallel=16, tg_window_mb=16, sftp_window_mb=32, ftp_block_kb=1024, per_server=2),
}
ENV_OVERRIDES = {"tg_parallel": "TG_PARALLEL", "tg_window_mb": "TG_WINDOW_MB", "sftp_window_mb": "SFTP_WINDOW_MB",
                 "ftp_block_kb": "FTP_BLOCK_KB", "per_server": "PARALLEL_PER_SERVER"}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def detect(read=_read, cpu_count=None):
    """(cores, ram_bytes) as this process may use them, honouring container limits (cgroup v2 and v1)."""
    if cpu_count is None:
        cpu_count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    cores = cpu_count
    quota = None
    v2 = (read("/sys/fs/cgroup/cpu.max") or "").split()
    if len(v2) == 2 and v2[0] != "max":
        quota = int(v2[0]) / int(v2[1])
    else:
        q, p = read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and p and int(q) > 0:
            quota = int(q) / int(p)
    if quota:
        cores = min(cores, max(1, math.ceil(quota)))

    ram = None
    for line in (read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            ram = int(line.split()[1]) * 1024
    host = ram or 4 * 1024 * MiB  # unknown host: assume a modest machine
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        v = read(path)
        if v and v.isdigit() and int(v) < host:
            host = int(v)
    return cores, host


def choose_profile(cores, ram):
    if ram < 700 * MiB or cores < 2:
        return "light"
    if ram >= 3 * 1024 * MiB and cores >= 4:
        return "turbo"
    return "balanced"


def settings(env=None, read=_read, cpu_count=None):
    env = os.environ if env is None else env
    cores, ram = detect(read, cpu_count)
    name = (env.get("PERFORMANCE") or "auto").lower()
    if name not in PROFILES:
        name = choose_profile(cores, ram)
    s = dict(PROFILES[name])
    while s["tg_parallel"] > 1 and s["tg_parallel"] * s["tg_window_mb"] * MiB > ram // 8:  # never let buffers eat the RAM
        s["tg_parallel"] -= 1
    for key, var in ENV_OVERRIDES.items():
        if env.get(var):
            s[key] = max(1, int(env[var]))
    s.update(profile=name, cores=cores, ram=ram)
    return s


SETTINGS = settings()


def describe(s=None):
    s = s or SETTINGS
    return (f"profile: {s['profile']} ({s['cores']} cores, {s['ram'] / 1024 / MiB:.1f} GB RAM)\n"
            f"Telegram downloads: {s['tg_parallel']} windows x {s['tg_window_mb']} MB in flight\n"
            f"SFTP window: {s['sftp_window_mb']} MB · FTP block: {s['ftp_block_kb']} KB · uploads per server: {s['per_server']}")
