"""Performance profile: resource detection (with fake cgroup/meminfo files), profile choice, overrides, wiring."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import subprocess, tempfile
import perf
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
MiB, GiB = 1024 * 1024, 1024 ** 3
fake = lambda files: (lambda path: files.get(path))
MEM16 = "MemTotal:       16384000 kB\nMemFree:  1 kB\n"

ok(perf.detect(fake({}), cpu_count=8) == (8, 4 * GiB), "1. no information at all: assume 8 cores given, 4 GiB RAM")
ok(perf.detect(fake({"/proc/meminfo": MEM16}), 4) == (4, 16384000 * 1024), "1b. RAM from /proc/meminfo")
ok(perf.detect(fake({"/proc/meminfo": MEM16, "/sys/fs/cgroup/memory.max": "536870912"}), 4)[1] == 512 * MiB, "1c. cgroup v2 memory limit wins over host RAM")
ok(perf.detect(fake({"/proc/meminfo": MEM16, "/sys/fs/cgroup/memory.max": "max"}), 4)[1] == 16384000 * 1024, "1d. cgroup v2 'max' (unlimited) is ignored")
ok(perf.detect(fake({"/proc/meminfo": MEM16, "/sys/fs/cgroup/memory/memory.limit_in_bytes": "268435456"}), 4)[1] == 256 * MiB, "1e. cgroup v1 memory limit")
ok(perf.detect(fake({"/proc/meminfo": MEM16, "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712"}), 4)[1] == 16384000 * 1024, "1f. cgroup v1 'unlimited' value is ignored")
ok(perf.detect(fake({"/sys/fs/cgroup/cpu.max": "50000 100000"}), 8)[0] == 1, "1g. half a CPU of quota -> 1 core")
ok(perf.detect(fake({"/sys/fs/cgroup/cpu.max": "200000 100000"}), 8)[0] == 2, "1h. 2 CPUs of quota on an 8 core host -> 2")
ok(perf.detect(fake({"/sys/fs/cgroup/cpu.max": "max 100000"}), 8)[0] == 8, "1i. no CPU quota -> all cores")
ok(perf.detect(fake({"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "300000", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"}), 8)[0] == 3, "1j. cgroup v1 CPU quota")
ok(perf.detect(fake({"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"}), 8)[0] == 8, "1k. cgroup v1 quota -1 means unlimited")

for cores, ram, want in ((1, 8 * GiB, "light"), (8, 512 * MiB, "light"), (2, 1 * GiB, "balanced"), (4, 2 * GiB, "balanced"), (4, 3 * GiB, "turbo"), (16, 64 * GiB, "turbo")):
    ok(perf.choose_profile(cores, ram) == want, f"2. {cores} cores / {ram // MiB} MiB -> {want}")

big = {"/proc/meminfo": "MemTotal: 33554432 kB\n"}
s = perf.settings({}, fake(big), 16); ok(s["profile"] == "turbo" and s["tg_parallel"] == 16 and s["sftp_window_mb"] == 32, "3. a big host gets turbo automatically")
s = perf.settings({}, fake({"/proc/meminfo": "MemTotal: 524288 kB\n"}), 1); ok(s["profile"] == "light" and s["tg_parallel"] == 4, "3b. a 512 MB host gets light")
s = perf.settings({"PERFORMANCE": "turbo"}, fake({"/proc/meminfo": "MemTotal: 524288 kB\n"}), 4)
ok(s["profile"] == "turbo" and s["tg_parallel"] * s["tg_window_mb"] * MiB <= 512 * MiB // 8, f"3c. forcing turbo on 512 MB still caps buffers to 1/8 of RAM (parallel={s['tg_parallel']})")
s = perf.settings({"PERFORMANCE": "turbo", "TG_PARALLEL": "3", "SFTP_WINDOW_MB": "1"}, fake(big), 16); ok(s["tg_parallel"] == 3 and s["sftp_window_mb"] == 1, "3d. explicit variables beat the profile")
ok(perf.settings({"PERFORMANCE": "nonsense"}, fake(big), 16)["profile"] == "turbo", "3e. an unknown profile name falls back to auto")
ok(perf.settings({"PERFORMANCE": "LIGHT"}, fake(big), 16)["profile"] == "light", "3f. names are case-insensitive")
ok(perf.settings({"TG_PARALLEL": "0"}, fake(big), 16)["tg_parallel"] == 1, "3g. values are clamped to at least 1")
real = perf.settings(); ok(real["cores"] >= 1 and real["ram"] > 0 and real["profile"] in perf.PROFILES, f"4. this machine: {real['profile']} ({real['cores']} cores, {real['ram'] // MiB} MiB)")
ok("profile:" in perf.describe() and "windows" in perf.describe(), "4b. describe() is readable")

env = dict(os.environ, PERFORMANCE="turbo", SECRET_KEY="x" * 40, DATABASE_URL="sqlite:///" + os.path.join(tempfile.mkdtemp(), "p.db"))
out = subprocess.run([sys.executable, "-c", "import remote, sources, jobs; print(remote.SFTP_WINDOW, remote.FTP_BLOCK, sources.TG_PARALLEL, jobs.PARALLEL)"],
                     cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), env=env, capture_output=True, text=True).stdout.split()
ok(out[:2] == [str(32 * MiB), str(1024 * 1024)] and out[3] == "2", f"5. the profile reaches SFTP window, FTP block, Telegram windows and uploads-per-server: {out}")
print("ALL PERF PASSED")
