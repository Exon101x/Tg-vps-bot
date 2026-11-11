# vpsbot — Telegram bot for file transfer and VPS management

[![CI](https://github.com/Exon101x/Tg-vps-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Exon101x/Tg-vps-bot/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Exon101x/Tg-vps-bot)

Send files between Telegram and your **FTP / FTPS / SSH (SFTP)** servers, and manage your VPSes from a chat.
One process, no temp disk for uploads, a persistent resumable queue, and it runs anywhere — Docker, systemd,
Render, Heroku or any VPS with Python 3.10+.

| Documentation | What is in it |
|---|---|
| **[docs/COMMANDS.md](docs/COMMANDS.md)** | every command, argument and example |
| **[docs/CONFIG.md](docs/CONFIG.md)** | every environment variable, default and tuning note |
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | hosting guide: Render, Docker, systemd, Heroku, PaaS, troubleshooting |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | how the code works: modules, data flows, database schema, security model |
| [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) | policy, contributing, releases |

## Table of contents

1. [Highlights](#highlights)
2. [Quick start](#quick-start)
3. [Commands at a glance](#commands-at-a-glance)
4. [Configuration](#configuration)
5. [Databases](#databases)
6. [Moving large files (over 2 GB)](#moving-large-files-over-2-gb)
7. [Speed profiles](#speed-profiles)
8. [Deployment](#deployment)
9. [Repository layout](#repository-layout)
10. [Development and CI](#development-and-ci)
11. [Security](#security)
12. [Limits and testing status](#limits-and-testing-status)
13. [FAQ](#faq)
14. [License](#license)

## Highlights

**File transfer — Telegram ⇄ your servers**

* Files stream from Telegram **straight to the server** — no temp disk, no double copy, no disk-space surprises.
* A **persistent queue** per server survives restarts: jobs are retried with backoff, and interrupted uploads
  **resume from the partial file** (`.part`). Flaky servers and redeploys are fine.
* **Parallel windowed downloads** from Telegram (up to 16 windows × 16 MB on big hosts), with live speed and ETA.
* **Files bigger than Telegram's 2 GB limit travel in both directions:**
  * *to* a server: split once (7-Zip volumes or `split`), then `/multi` → send all parts → `/done` — the bot
    joins them **on the fly** into one file on any FTP/FTPS/SFTP server (resumable, still no temp disk);
  * *from* a server: `/get` splits automatically into `.001 .002 …` volumes, staged one at a time and
    shrunk to the free disk.
* `/fetch <url>` pulls any HTTP(S) download to the server (no size limit, resumable, SSRF-guarded).
* Per-user folders with templates (`{user}/{date}`, `{type}/{year}-{month}`), duplicate policies
  (`overwrite` / `skip` / `rename`), several saved servers, remote file manager (`/ls /cd /mkdir /mv /rm`).

**VPS management over SSH**

* Dashboard with inline buttons: stats, processes, docker, ports, logins, updates, disk, firewall, failed services.
* `/sh <command>` (working directory is remembered) and a full `/term` terminal mode (10-minute idle timeout).
* systemd service control, journal logs, apt upgrades, reboot with confirmation, monitoring alerts
  (down / disk full / memory pressure), a full `/audit` trail, and SSH host-key pinning.

**Runs anywhere**

* SQLite (default), PostgreSQL, MySQL/MariaDB or MongoDB — pick with one `DATABASE_URL`.
* Deploy targets: `docker compose`, plain Docker (image also published to GHCR), systemd, Render Blueprint,
  Heroku, Railway, Fly.io, Koyeb and friends. The `deploy.py` wizard sets any of them up.
* Health endpoint for platform checks, session snapshots in the database (survive wiped disks),
  single-instance lock for zero-downtime deploys.

## Quick start

### Option A — the setup wizard (recommended)

```bash
git clone https://github.com/Exon101x/Tg-vps-bot && cd Tg-vps-bot
python deploy.py
```

The wizard needs nothing installed (pure standard library), asks a few questions, validates every value,
generates the `SECRET_KEY`, can start a bundled PostgreSQL or MongoDB, writes a mode-600 `.env` (with backup)
and prepares Docker / systemd / Render / another PaaS / a local venv. It never replaces an existing
`SECRET_KEY` and never runs anything without confirmation.

Non-interactive / scripted:

```bash
python deploy.py --yes --target docker --db postgres \
  --api-id 12345 --api-hash 0123456789abcdef --bot-token 123:ABC \
  --allowed-users 123456789 --run

python deploy.py --check --verify   # validate an existing .env, check the token with Telegram, test the DB
```

### Option B — manual

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in API_ID, API_HASH, BOT_TOKEN, SECRET_KEY, ALLOWED_USERS
python bot.py
```

### First steps in Telegram

1. `/add vps1 ssh://root@203.0.113.5` — save a server (it becomes active; the bot asks for the password
   or an SSH key file, and pins the host key on first connect).
2. Send any file — it streams to the server's upload folder. A caption `> backups/` sets the destination.
3. `/help` — the full command list, always available in chat.
4. `/vps` — the management dashboard (SSH servers).

## Commands at a glance

Full syntax, arguments and examples: **[docs/COMMANDS.md](docs/COMMANDS.md)**.

| Group | Commands |
|---|---|
| Servers | `/add` `/servers` `/use` `/del` `/status` `/monitor` `/audit` `/about` |
| VPS admin (SSH) | `/vps` `/stats` `/ps` `/ports` `/logins` `/updates` `/disk` `/firewall` `/failed` `/service` `/logs` `/docker` `/upgrade` `/reboot` |
| Shell | `/sh <cmd>` · `/term` (terminal mode) |
| Files | send any file (streamed) · `/fetch` `/get` `/join` `/multi`+`/done` |
| Remote filesystem | `/pwd` `/cd` `/ls` `/mkdir` `/mv` `/rm` |
| Upload queue | `/queue` `/retry` `/stop` `/usage` |
| Folders & duplicates | `/folder <template>` · `/ondup <policy>` |
| Misc | `/start` `/help` `/cancel` `/perf` |

## Configuration

Everything is an environment variable; a `.env` file works too (real variables win). The complete annotated
list with types, defaults and examples is **[docs/CONFIG.md](docs/CONFIG.md)**; `.env.example` is the template.

### Required

| Variable | Meaning |
|---|---|
| `API_ID`, `API_HASH` | your app credentials from <https://my.telegram.org> |
| `BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) |
| `SECRET_KEY` | encrypts saved passwords/keys: a Fernet key or any random string of 16+ characters — **back it up**, losing it makes saved servers unreadable |
| `ALLOWED_USERS` | comma-separated Telegram user ids; the bot refuses to start without it |

### The rest, in one breath

`DATABASE_URL` (default `sqlite:///bot.db`), `PORT` / `HEALTH_PORT` / `HEALTH_BIND` (health endpoint),
`PERFORMANCE` (`auto` by default), `TG_PARALLEL`, `TG_WINDOW_MB`, `SFTP_WINDOW_MB`, `FTP_BLOCK_KB`,
`PARALLEL_PER_SERVER` (per-value tuning), `TG_MAX_MB`, `MAX_FETCH_MB`, `MAX_RETRIES`, `MONITOR_INTERVAL`,
`DISK_ALERT`, `MEM_ALERT`, `KEEP_ALIVE` / `KEEP_ALIVE_URL`, `SESSION_DIR`, `SESSION_SYNC_INTERVAL`,
`RESET_SESSION`, `LOCK_TTL`, `DISABLE_LOCK`, `DB_CONNECT_RETRIES`, `LOG_LEVEL`.
A typo in a numeric variable logs a warning and uses the default — the bot never crashes at import over config.

## Databases

| Database | `DATABASE_URL` |
|---|---|
| SQLite (default) | `sqlite:///bot.db` or `sqlite:////data/bot.db` (absolute path) |
| PostgreSQL | `postgresql://user:pass@host:5432/dbname` (`postgres://` also accepted) |
| MySQL / MariaDB | `mysql://user:pass@host:3306/dbname` |
| MongoDB / Atlas | `mongodb://user:pass@host:27017/dbname` or `mongodb+srv://…` |

* Tables are created automatically; older databases are upgraded in place — no migration step to run.
* Passwords and keys are **encrypted before they reach the database** (Fernet, keyed by `SECRET_KEY`).
* To move between databases or hosts: stop the bot, then `python migrate.py <from-url> <to-url>`
  (add `--force` to replace a non-empty target). Keep the same `SECRET_KEY`.
* Queries are synchronous — keep the database in the same region as the bot.

## Moving large files (over 2 GB)

Telegram's documented limit is 2 GB per file (4 GB for Premium senders; a bot account cannot send more than
2 GB). The bot routes around that in both directions:

| Direction | What happens |
|---|---|
| **Telegram → server**, up to 2 GB | streamed in parallel windows, queued, retried, resumed from the partial file |
| **Telegram → server**, over 2 GB | Telegram cannot carry one file that big, so split it once (7-Zip "split to volumes", or `split -b 1900M -d -a 3 --numeric-suffixes=1 file file.`). Then `/multi`, send all the `.001 .002 …` parts, `/done`: the bot joins them **on the fly into one file on any FTP/FTPS/SFTP server** — resumable inside any volume, no temp disk, gaps and duplicates detected. Alternative on SSH servers: upload the parts normally and run `/join file` (`--rm` deletes them afterwards; needs free space equal to the file size) |
| **URL → server** | `/fetch <url>` — no size limit by default (`MAX_FETCH_MB` can cap it), resumable via HTTP Range |
| **Server → Telegram** | `/get path` — one file up to `TG_MAX_MB` (default 1900 MB); anything bigger is sent as `.001 .002 …` volumes (`-v 500` picks the volume size in MB). Only one volume is staged on disk at a time, and volumes shrink to 85 % of the free disk. `/join` on an SSH server reassembles them again |

Reading a 4 GB file sent by a Premium user has not been exercised here; the safe path is volumes.

## Speed profiles

The bot reads the CPU cores and RAM it is allowed to use (container limits included) and picks a profile —
`/perf` shows what was detected and chosen; `PERFORMANCE` forces one.

| Profile | Chosen when | Telegram windows | SFTP window | FTP block | Uploads per server |
|---|---|---|---|---|---|
| `light` | under 700 MB RAM or 1 core | 4 × 8 MB | 2 MB | 64 KB | 1 |
| `balanced` | in between | 8 × 8 MB | 8 MB | 256 KB | 1 |
| `turbo` | 4+ cores and 3+ GB RAM | 16 × 16 MB | 32 MB | 1 MB | 2 |

Buffers never exceed 1/8 of the RAM the process may use. The real transfer speed is still limited by the
network between Telegram, the bot's host and your server; a faster profile helps most on high-latency links
and big files.

## Deployment

| Where | How |
|---|---|
| Any of them | `python deploy.py` — interactive wizard, or flags for scripts (see [docs/DEPLOY.md](docs/DEPLOY.md)) |
| **Render** | push to GitHub → New → Blueprint (uses `render.yaml`); free-plan caveats in [docs/DEPLOY.md](docs/DEPLOY.md#render) |
| **Docker / any VPS** | `docker compose up -d --build` — or pull the CI-published image `ghcr.io/exon101x/tg-vps-bot:main` |
| **systemd** | `deploy/vpsbot.service` (hardened unit, install commands in [docs/DEPLOY.md](docs/DEPLOY.md#systemd-bare-vps-raspberry-pi)) |
| **Heroku, Railway, Fly.io, Koyeb, Cloud Run, …** | run the `Dockerfile`, set the env vars — per-platform notes in [docs/DEPLOY.md](docs/DEPLOY.md#other-container-platforms) |

Three rules for every host: **one running copy per bot token** (enforced by a database lock),
**an external database** where the disk is wiped on restart, and **a backed-up `SECRET_KEY`**.

## Repository layout

```
bot.py            Telegram handlers, start-up, health endpoint            (~1350 lines)
jobs.py           persistent upload queue: retries, resume, stop/retry, speed/ETA
sources.py        streaming sources: parallel Telegram windows, HTTP with Range (SSRF-guarded)
largefiles.py     volume planning for /get, volume ordering for /multi, the /join script
remote.py         FTP / FTPS / SSH + SFTP client, host-key pinning, path sanitising
store.py          domain API + Fernet encryption
backends.py       SQLite / PostgreSQL / MySQL / MongoDB behind one interface
session_sync.py   Telegram session snapshots + single-instance lock
perf.py           host detection and speed profiles
deploy.py         deployment setup wizard (stdlib only)
migrate.py        copy data between databases
healthcheck.py    Docker HEALTHCHECK helper
Dockerfile, docker-compose.yml, render.yaml, heroku.yml, Procfile, deploy/vpsbot.service
tests/            pytest suite: 12 scripts (local FTP + SSH servers, in-memory databases, 24 security checks)
docs/             COMMANDS.md, CONFIG.md, DEPLOY.md, ARCHITECTURE.md
```

How the pieces fit together: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Development and CI

```bash
pip install -r requirements-dev.txt
make lint          # ruff
make test          # pytest: spawns a local FTP server, a local SSH server, in-memory databases
TEST_DATABASE_URL=postgresql://… pytest -q   # additionally exercises a real database
```

CI (`.github/workflows/ci.yml`) runs on every push and PR:

* **test** job — ruff + pytest on Python **3.10 / 3.11 / 3.12** against a PostgreSQL 16 service container;
* **docker** job — builds the image (buildx, GHA cache) and pushes it to `ghcr.io/<owner>/<repo>` on `main`.

Dependabot watches pip, Docker and GitHub Actions weekly; every bump goes through the same matrix.

## Security

Only `ALLOWED_USERS` can use the bot, and each of them can run commands on every server they add — treat those
ids like root access. The design: SSH host keys pinned on first use, destructive commands ask for confirmation,
every action lands in `/audit`, credentials are encrypted at rest, URL fetches are SSRF-guarded (host resolved
once, connection pinned to the checked address), FTP paths are command-injection-safe. Prefer `ssh://`/`ftps://`
over plain `ftp://`. Full policy and hardening checklist: [SECURITY.md](SECURITY.md).

## Limits and testing status

* `/get` stages one volume at a time in the temp directory; uploads never touch disk.
* Run one instance per bot token (enforced by the lock; `DISABLE_LOCK=1` opts out — single-copy setups only).
* Volume names go up to `.999` (999 volumes) — use a bigger `-v` beyond that.
* Tested here: SQLite, PostgreSQL, FTP, SSH/SFTP and an in-memory MongoDB stand-in, across the CI matrix.
  Not exercised: MySQL/MariaDB, a real MongoDB/Atlas, the published Docker image, live Telegram production traffic.

## FAQ

**Does the bot need a public IP, domain or webhook?**
No. It talks to Telegram over MTProto with outbound connections only. The HTTP health endpoint is optional
and for the hosting platform, not for Telegram.

**What happens when the host restarts mid-upload?**
The queue and progress live in the database. On start the bot re-enqueues pending jobs and resumes from the
`.part` file already on the server — no re-upload from Telegram for completed ranges of `/fetch`, no lost jobs.

**Why did my saved servers become unreadable?**
The `SECRET_KEY` differs from the one that encrypted them. Restore it, or re-add the servers after rotating
(see [SECURITY.md](SECURITY.md)).

**Can two people use one bot?**
Yes — put both ids in `ALLOWED_USERS`. Each user has separate servers, folders, queue and audit trail.

**How do I move to a bigger host?**
Stop the bot, `python migrate.py <old-url> <new-url>`, copy `.env` (same `SECRET_KEY`!), start. The Telegram
session comes with the database snapshot, so no fresh login is needed.

**Free plan on Render — really?**
Yes, with caveats (sleeps after 15 min, ephemeral disk, expiring Postgres). The blueprint sets `KEEP_ALIVE=1`
and requires an external `DATABASE_URL`; details in [docs/DEPLOY.md](docs/DEPLOY.md#render).

## License

MIT — see [LICENSE](LICENSE) (Exon101).
