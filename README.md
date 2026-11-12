<div align="center">

<img src="assets/banner.svg" alt="vpsbot — move files between Telegram and your servers" width="100%">

# vpsbot

**Streaming file transfer with a persistent, resumable queue — plus a full SSH toolbox for your VPSes.**

One process, no temp disk for uploads, and it runs anywhere: Docker, systemd, Render,
Heroku or any VPS with Python 3.10+.

[![CI](https://github.com/Exon101x/Tg-vps-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Exon101x/Tg-vps-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230?logo=ruff&logoColor=white)](https://docs.astral.sh/ruff/)
[![Image](https://img.shields.io/badge/image-ghcr.io%2Fexon101x%2Ftg--vps--bot-2496ED?logo=docker&logoColor=white)](https://github.com/Exon101x/Tg-vps-bot/pkgs/container/tg-vps-bot)

[![Stars](https://img.shields.io/github/stars/Exon101x/Tg-vps-bot?style=social)](https://github.com/Exon101x/Tg-vps-bot/stargazers)
[![Forks](https://img.shields.io/github/forks/Exon101x/Tg-vps-bot?style=social)](https://github.com/Exon101x/Tg-vps-bot/forks)
[![Issues](https://img.shields.io/github/issues/Exon101x/Tg-vps-bot?style=social)](https://github.com/Exon101x/Tg-vps-bot/issues)
[![Last commit](https://img.shields.io/github/last-commit/Exon101x/Tg-vps-bot)](https://github.com/Exon101x/Tg-vps-bot/commits/main)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Commands](docs/COMMANDS.md) · [Configuration](docs/CONFIG.md) · [Deployment](docs/DEPLOY.md) ·
[Architecture](docs/ARCHITECTURE.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) ·
[Changelog](CHANGELOG.md)

</div>

---

## Contents

1. [Features at a glance](#features-at-a-glance)
2. [Quick start](#quick-start)
3. [Commands at a glance](#commands-at-a-glance)
4. [The Mini App](#the-mini-app)
5. [Big files: 2 GB, 4 GB and beyond](#big-files-2-gb-4-gb-and-beyond)
6. [Subscriptions & VPS hosting](#subscriptions--vps-hosting)
7. [Configuration](#configuration)
8. [Databases](#databases)
9. [Speed profiles](#speed-profiles)
10. [Deployment](#deployment)
11. [Security](#security)
12. [Development and CI](#development-and-ci)
13. [Repository layout](#repository-layout)
14. [FAQ](#faq)
15. [Limits and testing status](#limits-and-testing-status)
16. [License](#license)

## Features at a glance

| Capability | What you get |
|---|---|
| **Direct streaming** | files go Telegram → server with **no temp disk** — parallel windowed downloads, live speed and ETA |
| **Persistent queue** | survives restarts, retries with backoff, resumes from the server-side `.part` file |
| **Files up to 4 GB** | sent directly like any other file (Premium sender) — no volumes, no splitting |
| **Files beyond 4 GB** | `/multi` + `/done` joins split volumes **on the fly** into one file on any server |
| **Per-file server picker** | `/ask on` — forward or upload a file, tap the destination server, done |
| **Mini App** | `/app` — a web UI inside Telegram: file manager, uploads from your phone, SSH console, VPS stats; sign-in is your Telegram id, no password |
| **Admin panel** | `/admin` and the app's **Admin** tab — users and usage, orders and revenue, ban / unban, broadcast to everyone (`ADMINS` / `DEV_ID`) |
| **Subscriptions** | `/plans` — a sellable catalog (Free / PRO / VPS tiers, your prices via `SUBSCRIPTION_PLANS`): buyers pay, plans activate instantly |
| **Payment gateways** | **UPI** via a self-hosted [YouPI-PG](https://github.com/youpi-pg/YouPI-PG) (checkout page + QR + any UPI app, signed webhooks) or a direct `upi://pay` QR · **crypto** via [CryptAPI](https://cryptapi.io) (BTC, USDT, 100+ coins, paid to **your own wallets**) — both auto-activate the order the second the money lands; `/paid <ref>` remains the manual fallback |
| **Buy-a-VPS** | VPS plans include real SSH instances with disk quotas, provisioned on your host node — credentials delivered automatically, renewed by re-paying, removed on expiry |
| **Host your own bots** | buyers add their VPS back as a server and get the full toolbox: upload files from Telegram, `/sh` to set up, `/service` to keep it running — `/hosting` is the 60-second guide |
| **Force-subscribe** | set `FORCE_SUB` and users must join your channel first — join card with a recheck button in chat, a join screen in the app; admins are exempt |
| **Hardened API** | per-user rate limiting, admin-controlled ban switch, strict CSP, `initData` HMAC sign-in — nothing stored client-side |
| **URL downloads** | `/fetch <url>` pulls HTTP(S) straight to the server — resumable, SSRF-guarded |
| **Server → Telegram** | `/get` auto-splits into `.001 .002 …` volumes when needed, staging one at a time |
| **Remote file manager** | `/pwd` `/cd` `/ls` `/mkdir` `/mv` `/rm` |
| **Folders & duplicates** | per-user folder templates (`{user}/{date}`, `{type}/{year}-{month}`), `overwrite` / `skip` / `rename` policies |
| **VPS dashboard** | `/vps` with inline buttons: stats, processes, docker, ports, logins, updates, disk, firewall, failed services |
| **Shell access** | `/sh <command>` (working directory remembered) and a full `/term` terminal mode |
| **Monitoring & audit** | down / disk-full / memory-pressure alerts, a full `/audit` trail, SSH host-key pinning |
| **Databases** | SQLite (default) · PostgreSQL · MySQL/MariaDB · MongoDB — pick with one `DATABASE_URL` |
| **Runs anywhere** | Docker, systemd, Render, Heroku, Railway, Fly.io, Koyeb — health endpoint, DB-backed session snapshots, single-instance lock |

## Quick start

### Option A — the setup wizard (recommended)

```bash
git clone https://github.com/Exon101x/Tg-vps-bot && cd Tg-vps-bot
python deploy.py
```

The wizard needs nothing installed (pure standard library), asks a few questions, validates every value,
generates the `SECRET_KEY`, can start a bundled PostgreSQL or MongoDB, writes a mode-600 `.env` (with
backup) and prepares Docker / systemd / Render / another PaaS / a local venv. It never replaces an
existing `SECRET_KEY` and never runs anything without confirmation.

> [!TIP]
> Scripted / non-interactive:
> ```bash
> python deploy.py --yes --target docker --db postgres \
>   --api-id 12345 --api-hash 0123456789abcdef --bot-token 123:ABC \
>   --allowed-users 123456789 --run
>
> python deploy.py --check --verify   # validate .env, check the token with Telegram, test the DB
> ```

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
3. `/vps` — the management dashboard. `/help` — the full command list, always available in chat.

## Commands at a glance

Full syntax, arguments and examples: **[docs/COMMANDS.md](docs/COMMANDS.md)**.

| Group | Commands |
|---|---|
| Servers | `/add` `/servers` `/use` `/del` `/status` `/monitor` `/audit` `/about` |
| Plans & VPS | `/plans` `/mysub` `/myvps` `/paid` `/hosting` |
| VPS admin (SSH) | `/vps` `/stats` `/ps` `/ports` `/logins` `/updates` `/disk` `/firewall` `/failed` `/service` `/logs` `/docker` `/upgrade` `/reboot` |
| Shell | `/sh <cmd>` · `/term` (terminal mode) · `/app` (the Mini App) |
| Files | send any file (streamed, up to 4 GB direct with Premium) · `/fetch` `/get` `/join` `/multi`+`/done` |
| Remote filesystem | `/pwd` `/cd` `/ls` `/mkdir` `/mv` `/rm` |
| Upload queue | `/queue` `/retry` `/stop` `/usage` |
| Folders & duplicates | `/folder <template>` · `/ondup <policy>` · `/ask on|off` (server picker per file) |
| Misc | `/start` `/help` `/cancel` `/perf` |

## The Mini App

**`/app`** opens a web UI the bot serves on its own HTTP port — right inside Telegram, mobile-friendly.
Signing in is automatic: Telegram signs every request with your id, the bot verifies the signature and
applies the same `ALLOWED_USERS` gate as the chat. No passwords, no cookies, nothing stored.

| Tab | What you can do |
|---|---|
| **Files** | browse the active server, create folders, rename, delete; send any file to your Telegram chat (volumes for big ones); **upload from your phone** — chunked, resumable, queued exactly like chat uploads; fetch a URL on the server |
| **Servers** | add (connection tested, host key pinned), switch, delete; edit folder template, duplicate policy, monitoring |
| **Console** | run shell commands on the active SSH server — remembered working directory, the same dangerous-command confirmation as `/sh` |
| **Stats** | the `/stats` overview plus every `/vps` view: processes, ports, docker, disk, firewall, failed services, logins, updates |
| **Queue** | the upload queue with retry/stop, and the audit trail |
| **Hosting** | the plans catalog with Buy buttons, the checkout box (`/paid` reference), your orders — and your VPS instances with credentials (tap to reveal), specs and expiry |
| **Admin** | *(ADMINS / DEV_ID only)* live overview cards — users, servers, uploads and bytes (total / today / 7 days), subs, revenue, VPSes, uptime, database — the user list with detail sheets and ban / unban, **order approvals** (approve / decline right in the list) and a broadcast box |

With `FORCE_SUB` set, users who have not joined the channel get a dedicated **join screen** (join button + recheck)
instead of the app, until they subscribe. The header carries the bot logo (`webapp/logo.png`), and the footer keeps
the **GitHub fork link and the developer credit** (`DEV_CHANNEL`) — please leave them in place when you fork.

To enable the button, set `WEBAPP_URL` to the bot's public HTTPS address — PaaS platforms (Render,
Heroku, Railway, …) give you one automatically; on a plain VPS put nginx or Caddy in front (a two-line
Caddyfile is in [docs/DEPLOY.md](docs/DEPLOY.md#https-for-the-mini-app)). Uploads through the app are
capped by `WEB_MAX_MB` (default 4096). Without `WEBAPP_URL` everything still works from the chat.

## Big files: 2 GB, 4 GB and beyond

Telegram's documented limit is 2 GB per file; **Premium accounts can upload up to 4 GB**, and the bot
(MTProto, not the Bot API) downloads those whole — so 2–4 GB files transfer directly. Above 4 GB nothing
can carry one file in Telegram, hence the volume workflows.

| Direction | What happens |
|---|---|
| **Telegram → server**, up to 2 GB | streamed in parallel windows, queued, retried, resumed from the partial file |
| **Telegram → server**, 2–4 GB | **sent directly, like any other file** — the sender needs Telegram Premium (a Telegram rule, not a bot one); the bot streams it to any FTP/FTPS/SFTP server with no volumes and no temp disk, and the queue note marks it |
| **Telegram → server**, over 4 GB | split it once (7-Zip volumes, or `split -b 1900M -d -a 3 --numeric-suffixes=1 file file.`), then `/multi`, send all the `.001 .002 …` parts, `/done` — the bot joins them **on the fly into one file on any server**, resumable inside any volume, gaps and duplicates detected. Alternative on SSH servers: upload the parts normally and `/join file` (`--rm` deletes them afterwards; needs free space equal to the file size) |
| **URL → server** | `/fetch <url>` — no size limit by default (`MAX_FETCH_MB` can cap it), resumable via HTTP Range |
| **Server → Telegram** | `/get path` — one file up to `TG_MAX_MB` (default 1900 MB); anything bigger is sent as `.001 .002 …` volumes (`-v 500` picks the size in MB). One volume is staged on disk at a time, and volumes shrink to 85 % of the free disk. `/join` on an SSH server reassembles them |

> [!IMPORTANT]
> The direct 2–4 GB path uses the same windowed MTProto streaming as every other upload (64-bit
> offsets) but has not yet been exercised against a live Premium upload in production — try one big
> file first.

## Subscriptions & VPS hosting

Sell plans — including VPS instances your users can host their own bots on. Everything is driven by
environment variables ([docs/CONFIG.md](docs/CONFIG.md#subscriptions--vps-hosting)):

| Piece | How it works |
|---|---|
| **Catalog** | Free (3 servers) · PRO · VPS Starter (1× VPS: 1 vCPU / 1 GB / 15 GB) · VPS Pro (2× VPS: 2 vCPU / 2 GB / 40 GB each) — replace entirely with `SUBSCRIPTION_PLANS` (JSON). `SUBSCRIPTION_CURRENCY` sets the display currency |
| **Checkout, automatic** | `/plans` → **Buy** → pick a gateway: a **YouPI-PG** checkout link (UPI QR + app deep links, verified by its bank observers), a **direct UPI QR** from your `UPI_VPA`, or **crypto** (BTC/USDT/…, minted address, exact amount) — the signed webhook (`/pay/youpi/cb`, `/pay/cryptapi/cb` on the bot's own port) auto-approves the order: plan + VPS land in the buyer's chat with zero clicks from you |
| **Checkout, manual** | no gateway? `PAYMENT_INFO` carries your links, the buyer sends `/paid <reference>`, you get Approve/Decline buttons — both flows coexist |
| **Approval** | one tap (or fully automatic, above) activates the plan instantly, extends renewals from the current expiry, and provisions the plan's VPS instances on your host node — credentials land in the buyer's chat and in **Hosting → My VPS** |
| **The VPSes** | locked-down Linux users with disk quotas on the machine you set with `VPS_NODE_HOST` (override `VPS_PROVISION_CMD`/`VPS_DEPROVISION_CMD` for LXC, Docker or Proxmox). Passwords are Fernet-encrypted at rest, alnum-only (no shell injection), and instances are removed automatically when the subscription lapses |
| **Limits** | plans cap saved servers (`/add` and the app enforce it); without a subscription everyone is Free — nothing to configure for the core bot |
| **For buyers** | `/hosting` is the 60-second guide: add the VPS back with `/add myvps ssh://user@host`, send the bot's files straight from Telegram, run it with `/sh`, keep it alive with `/service` — a full hosting workflow without leaving the chat |
| **Admin view** | `/admin` shows active subs, revenue, open orders and VPS counts; `/admin orders` lists what needs a decision |

> [!IMPORTANT]
> **Gateway security.** Every webhook is verified before anything happens — YouPI's
> `X-Gateway-Signature` (HMAC-SHA256 of the raw body with `YOUPI_WEBHOOK_SECRET`) and CryptAPI's
> `X-CA-Signature` (RSA-SHA256 over the request URL, checked against their published pubkey) — then
> the amount must match the order (crypto: per-uuid totals within `CRYPTAPI_TOLERANCE` %), the
> per-order nonce must match, and approvals are idempotent: replayed or retried webhooks can never
> activate anything twice. Everything is audit-logged. YouPI rides the NPCI UPI rails (INR prices);
> CryptAPI forwards to **your own wallets** (verify the `WEBAPP_URL` domain once in their panel).

## Configuration

Everything is an environment variable; a `.env` file works too (real variables win). The complete
annotated list with types, defaults and examples is **[docs/CONFIG.md](docs/CONFIG.md)**; `.env.example`
is the template.

### Required

| Variable | Meaning |
|---|---|
| `API_ID`, `API_HASH` | your app credentials from <https://my.telegram.org> |
| `BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) |
| `SECRET_KEY` | encrypts saved passwords/keys: a Fernet key or any random string of 16+ characters — **back it up**, losing it makes saved servers unreadable |
| `ALLOWED_USERS` | comma-separated Telegram user ids; the bot refuses to start without it |

### Optional, by area

| Area | Variables |
|---|---|
| Core | `DATABASE_URL` (default `sqlite:///bot.db`) · `PERFORMANCE` (`auto` by default) · `LOG_LEVEL` |
| HTTP port / Mini App | `PORT` · `HEALTH_PORT` · `HEALTH_BIND` · `WEBAPP_URL` (public HTTPS address that enables the `/app` button) · `WEB_MAX_MB` (app upload cap, default 4096) · `WEB_RATE` (app API requests per minute per user, default 90) |
| Admin & community | `ADMINS` · `DEV_ID` (developer id: admin + force-sub exempt) · `DEV_CHANNEL` (developer credit) · `FORCE_SUB` (channel users must join) |
| Subscriptions & VPS hosting | `SUBSCRIPTION_PLANS` (JSON catalog) · `SUBSCRIPTION_CURRENCY` · `PAYMENT_INFO` · `VPS_NODE_HOST` `VPS_NODE_PORT` `VPS_NODE_USER` `VPS_NODE_PASS`/`VPS_NODE_KEY` (the host node) · `VPS_PUBLIC_HOST` · `VPS_SSH_PORT` · `VPS_PROVISION_CMD` · `VPS_DEPROVISION_CMD` |
| Payment gateways | `YOUPI_URL` `YOUPI_MERCHANT_ID` `YOUPI_API_KEY` `YOUPI_WEBHOOK_SECRET` (self-hosted UPI checkout) · `UPI_VPA` `UPI_NAME` (direct UPI QR) · `CRYPTAPI_<COIN>_ADDR` e.g. `CRYPTAPI_BTC_ADDR`, `CRYPTAPI_TRC20_USDT_ADDR` (crypto to your own wallets) · `CRYPTAPI_TOLERANCE` `CRYPTAPI_CONFIRMATIONS` `CRYPTAPI_API_KEY` `CRYPTAPI_BASE` |
| Transfer tuning | `TG_PARALLEL` · `TG_WINDOW_MB` · `SFTP_WINDOW_MB` · `FTP_BLOCK_KB` · `PARALLEL_PER_SERVER` |
| Size limits | `TG_MAX_MB` · `MAX_FETCH_MB` · `MAX_RETRIES` |
| Monitoring | `MONITOR_INTERVAL` · `DISK_ALERT` · `MEM_ALERT` |
| Session & lock | `KEEP_ALIVE` · `KEEP_ALIVE_URL` · `SESSION_DIR` · `SESSION_SYNC_INTERVAL` · `RESET_SESSION` · `LOCK_TTL` · `DISABLE_LOCK` |
| Robustness | `DB_CONNECT_RETRIES` |

> [!NOTE]
> A typo in a numeric variable logs a warning and uses the default — the bot never crashes at import
> over config.

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

## Speed profiles

The bot reads the CPU cores and RAM it is allowed to use (container limits included) and picks a
profile — `/perf` shows what was detected and chosen; `PERFORMANCE` forces one.

| Profile | Chosen when | Telegram windows | SFTP window | FTP block | Uploads per server |
|---|---|---|---|---|---|
| `light` | under 700 MB RAM or 1 core | 4 × 8 MB | 2 MB | 64 KB | 1 |
| `balanced` | in between | 8 × 8 MB | 8 MB | 256 KB | 1 |
| `turbo` | 4+ cores and 3+ GB RAM | 16 × 16 MB | 32 MB | 1 MB | 2 |

Buffers never exceed 1/8 of the RAM the process may use. The real transfer speed is still limited by
the network between Telegram, the bot's host and your server; a faster profile helps most on
high-latency links and big files.

## Deployment

| Where | How |
|---|---|
| Any of them | `python deploy.py` — interactive wizard, or flags for scripts ([docs/DEPLOY.md](docs/DEPLOY.md)) |
| **Render** | push to GitHub → New → Blueprint (uses `render.yaml`); free-plan caveats in [docs/DEPLOY.md](docs/DEPLOY.md#render) |
| **Docker / any VPS** | `docker compose up -d --build` — or pull the CI-published image `ghcr.io/exon101x/tg-vps-bot:main` |
| **systemd** | `deploy/vpsbot.service` (hardened unit, install commands in [docs/DEPLOY.md](docs/DEPLOY.md#systemd-bare-vps-raspberry-pi)) |
| **Heroku, Railway, Fly.io, Koyeb, Cloud Run, …** | run the `Dockerfile`, set the env vars — per-platform notes in [docs/DEPLOY.md](docs/DEPLOY.md#other-container-platforms) |

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Exon101x/Tg-vps-bot)

> [!IMPORTANT]
> Three rules for every host: **one running copy per bot token** (enforced by a database lock),
> **an external database** where the disk is wiped on restart, and **a backed-up `SECRET_KEY`**.

## Security

> [!WARNING]
> Only `ALLOWED_USERS` can use the bot, and each of them can run commands on every server they add —
> treat those ids like root access.

The design: SSH host keys pinned on first use, destructive commands ask for confirmation, every action
lands in `/audit`, credentials are encrypted at rest, URL fetches are SSRF-guarded (host resolved once,
connection pinned to the checked address), FTP paths are command-injection-safe. Prefer `ssh://`/`ftps://`
over plain `ftp://`. On top of that: the Mini App verifies Telegram's `initData` HMAC signature on every
request (24 h freshness, constant-time), rate-limits every user (`WEB_RATE`), enforces admin bans in chat
and app alike, and serves its pages with a strict CSP. Optional `FORCE_SUB` gates everything behind a
channel subscription. Full policy and hardening checklist: [SECURITY.md](SECURITY.md).

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

## Repository layout

<details>
<summary><b>How the code is organised</b> — click to expand. The full data-flow story is in
<a href="docs/ARCHITECTURE.md"><b>docs/ARCHITECTURE.md</b></a>.</summary>

```text
bot.py            Telegram handlers, start-up, keep-alive                         (~1470 lines)
webapi.py         the Mini App: HTTP server on the health port, initData auth, JSON API,
                  chunked resumable web uploads, console/stats/view endpoints
webapp/           the Mini App UI (vanilla HTML/CSS/JS, no build step, served by webapi.py)
jobs.py           persistent upload queue: retries, resume, stop/retry, speed/ETA
sources.py        streaming sources: parallel Telegram windows, HTTP with Range (SSRF-guarded), staged web uploads
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
assets/           banner.svg (README), logo.jpg (bot avatar / app icon), banner.jpg (wide hero)
tests/            14 check scripts run by pytest: local FTP + SSH servers, in-memory
                  databases, 24 security + 22 big-file + 51 mini-app + 40 admin checks
docs/             COMMANDS.md, CONFIG.md, DEPLOY.md, ARCHITECTURE.md
```

</details>

## FAQ

**Can I send a 3 GB file without splitting it?**
Yes, if the **sender** has Telegram Premium — Telegram itself refuses bigger uploads from free
accounts. Forward or upload the file; the bot streams it to the server directly (the queue message
marks it as an over-2 GB transfer). Above 4 GB Telegram has no single-file path at all: split and
use `/multi` + `/done`.

**Does the bot need a public IP, domain or webhook?**
No. It talks to Telegram over MTProto with outbound connections only. The HTTP health endpoint is
optional and for the hosting platform, not for Telegram.

**What happens when the host restarts mid-upload?**
The queue and progress live in the database. On start the bot re-enqueues pending jobs and resumes
from the `.part` file already on the server — no re-upload from Telegram for completed ranges of
`/fetch`, no lost jobs.

**Why did my saved servers become unreadable?**
The `SECRET_KEY` differs from the one that encrypted them. Restore it, or re-add the servers after
rotating (see [SECURITY.md](SECURITY.md)).

**Can two people use one bot?**
Yes — put both ids in `ALLOWED_USERS`. Each user has separate servers, folders, queue and audit trail.

**How do I move to a bigger host?**
Stop the bot, `python migrate.py <old-url> <new-url>`, copy `.env` (same `SECRET_KEY`!), start. The
Telegram session comes with the database snapshot, so no fresh login is needed.

**Free plan on Render — really?**
Yes, with caveats (sleeps after 15 min, ephemeral disk, expiring Postgres). The blueprint sets
`KEEP_ALIVE=1` and requires an external `DATABASE_URL`; details in [docs/DEPLOY.md](docs/DEPLOY.md#render).

## Limits and testing status

* `/get` stages one volume at a time in the temp directory; uploads never touch disk.
* Run one instance per bot token (enforced by the lock; `DISABLE_LOCK=1` opts out — single-copy
  setups only).
* Volume names go up to `.999` (999 volumes) — use a bigger `-v` beyond that.
* Tested here: SQLite, PostgreSQL, FTP, SSH/SFTP and an in-memory MongoDB stand-in, across the CI
  matrix. Not exercised: MySQL/MariaDB, a real MongoDB/Atlas, the published Docker image, live
  Telegram production traffic.

## License

<div align="center">

**MIT** — see [LICENSE](LICENSE) (Exon101)

</div>
