# vpsbot: Telegram bot for file transfer and VPS management

[![CI](https://github.com/Exon101x/Tg-vps-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Exon101x/Tg-vps-bot/actions)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Exon101x/Tg-vps-bot)

Send files to your FTP / FTPS / SFTP servers straight from Telegram, and run your VPS from a chat.

**File transfer**
* Files stream from Telegram to the server with **no temp disk**, through a persistent **queue** with **retries and
  resume** (flaky servers are fine, restarts don't lose jobs).
* **Large files:** downloads from Telegram run in parallel windows (about 4x one stream) with speed and ETA; files of
  any size can be fetched from a URL; `/get` splits anything over Telegram's 2 GB limit into `.001 .002 …` volumes
  (staged one at a time, shrunk to the free disk), and `/multi` + `/done` take the volumes of a file bigger than 2 GB
  and stream them back-to-back into **one file on any FTP / FTPS / SFTP server** with no temp disk. Together that moves
  files **bigger than 2 GB in both directions**.
* **Speed profile:** the bot detects CPU cores and RAM (container limits included) and picks `light`, `balanced` or
  `turbo` automatically: parallel Telegram windows, SFTP window, FTP block size and uploads per server scale with the host.
* Several servers per user, folder templates (`{user}/{date}`, `{type}/{year}-{month}`), duplicate policies,
  `/fetch <url>`, `/get`, `/ls /cd /mkdir /mv /rm`.

**VPS management (SSH)**
* `/stats`, `/sh` (remembers the directory) and terminal mode, systemd services, logs, docker, apt updates, reboot,
  disk/firewall views, alerts when a server goes down or fills up, an audit log, pinned SSH host keys.

**Runs anywhere**
* SQLite, PostgreSQL, MySQL/MariaDB or MongoDB. Docker, docker compose, systemd, **Render**, Heroku and other PaaS.

## Quick start

```bash
python deploy.py            # the setup wizard: asks a few questions, writes .env, prepares Docker / systemd / Render / ...
```

It needs nothing installed (standard library only), validates what you type, generates the encryption key, can start a
bundled PostgreSQL or MongoDB, and never replaces an existing `SECRET_KEY`. Non-interactive: `python deploy.py --yes
--target docker --db postgres --api-id ... --api-hash ... --bot-token ... --allowed-users 123 --run`;
`python deploy.py --check [--verify]` validates an existing `.env` (and checks the token with Telegram).

Manual alternative:

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in API_ID, API_HASH, BOT_TOKEN, SECRET_KEY, ALLOWED_USERS
python bot.py
```

Then message the bot `/help`, and add a server: `/add vps1 ssh://root@203.0.113.5`.

## Deploy

| Where | How |
|---|---|
| Any of them | `python deploy.py` (interactive wizard, or flags for scripts) |
| **Render** | push to GitHub, New → Blueprint (uses `render.yaml`), see [docs/DEPLOY.md](docs/DEPLOY.md) for the free-plan details |
| Docker / any VPS | `docker compose up -d --build` |
| systemd | `deploy/vpsbot.service` |
| Heroku, Railway, Fly.io, Koyeb, … | run the `Dockerfile`, set the env vars, see [docs/DEPLOY.md](docs/DEPLOY.md) |

## Configuration

Environment variables (a `.env` file works too; real variables win). Everything is listed in `.env.example`.

| Variable | Meaning |
|---|---|
| `API_ID`, `API_HASH` | from my.telegram.org |
| `BOT_TOKEN` | from @BotFather |
| `SECRET_KEY` | encrypts saved passwords/keys: a Fernet key or any random string of 16+ characters. **Back it up** |
| `ALLOWED_USERS` | comma-separated Telegram user ids; the bot refuses to start without it |
| `DATABASE_URL` | storage, see below (default `sqlite:///bot.db`) |
| `PORT` / `HEALTH_PORT` | if set, the bot answers HTTP health checks (`GET /` → 200) on it |
| `PERFORMANCE` | `auto` (default), `light`, `balanced` or `turbo`; `/perf` shows what was detected and chosen |
| `TG_PARALLEL`, `TG_WINDOW_MB`, `SFTP_WINDOW_MB`, `FTP_BLOCK_KB`, `PARALLEL_PER_SERVER`, `TG_MAX_MB`, `MAX_FETCH_MB` | override single speed/limit values, see `.env.example` |
| `KEEP_ALIVE=1` | ping the public URL every 10 min so free web services don't sleep (`KEEP_ALIVE_URL`, or Render's `RENDER_EXTERNAL_URL`) |
| `SESSION_DIR`, `RESET_SESSION`, `LOCK_TTL`, `DISABLE_LOCK` | session and single-instance handling, see [docs/DEPLOY.md](docs/DEPLOY.md) |

### Databases

| Database | `DATABASE_URL` |
|---|---|
| SQLite (default) | `sqlite:///bot.db` or `sqlite:////data/bot.db` (absolute) |
| PostgreSQL | `postgresql://user:pass@host:5432/dbname` (`postgres://` also accepted) |
| MySQL / MariaDB | `mysql://user:pass@host:3306/dbname` |
| MongoDB / Atlas | `mongodb://user:pass@host:27017/dbname` or `mongodb+srv://…` |

Tables are created automatically and older databases are upgraded in place. Passwords and keys are encrypted by the
bot before they reach any database. To move between databases or hosts, stop the bot and run
`python migrate.py <from-url> <to-url>` (add `--force` to replace a non-empty target); keep the same `SECRET_KEY`.
Queries are synchronous, so keep the database in the same region as the bot.

## Large files

| Direction | What happens |
|---|---|
| Telegram → server, up to 2 GB | streamed in parallel windows, queued, retried, resumed from the partial file |
| Telegram → server, over 2 GB | Telegram will not carry one file that big, so split it first (7-Zip "split to volumes", or `split -b 1900M -d -a 3 --numeric-suffixes=1 file file.`). Then send `/multi`, all the `.001 .002 …` parts, and `/done`: the bot joins them **on the fly into one file on any FTP/FTPS/SFTP server** (resumable, no temp disk). Alternative on SSH servers: upload the parts normally and run `/join file` (`--rm` deletes them; needs free space equal to the file size) |
| URL → server | `/fetch <url>`: no size limit by default, resumable (HTTP Range) |
| Server → Telegram | `/get path`: one file up to 1900 MB, otherwise `.001 .002 …` volumes (`-v 500` picks the volume size in MB); only one volume is staged on disk at a time and volumes shrink to fit the free disk |

Telegram's documented limit is 2 GB per file (4 GB for Premium senders). The bot account cannot send more than 2 GB, and
reading a 4 GB file from a Premium user has not been tried here.

### Speed profiles

| Profile | Chosen when | Telegram windows | SFTP window | FTP block | Uploads per server |
|---|---|---|---|---|---|
| `light` | under 700 MB RAM or 1 core | 4 x 8 MB | 2 MB | 64 KB | 1 |
| `balanced` | in between | 8 x 8 MB | 8 MB | 256 KB | 1 |
| `turbo` | 4+ cores and 3+ GB RAM | 16 x 16 MB | 32 MB | 1 MB | 2 |

Buffers never exceed 1/8 of the RAM the process may use. The real transfer speed is still limited by the network
between Telegram, the bot's host and your server; a faster profile helps most on high-latency links and big files.

## Repository layout

```
bot.py            Telegram handlers, start-up, health endpoint
jobs.py           persistent upload queue: retries, resume, stop/retry, speed/ETA
sources.py        streaming sources (parallel Telegram windows, HTTP with Range)
largefiles.py     volume planning for /get, volume ordering for /multi, the /join script
perf.py           host detection and speed profiles
remote.py         FTP / FTPS / SSH + SFTP client, host-key pinning
store.py          domain API + encryption; backends.py: SQLite/Postgres/MySQL/MongoDB
session_sync.py   Telegram session snapshots + single-instance lock
migrate.py        copy data between databases
deploy.py         deployment setup wizard (stdlib only): .env, secrets, database, Docker/systemd/Render/PaaS
Dockerfile, docker-compose.yml, render.yaml, heroku.yml, Procfile, deploy/vpsbot.service
tests/            pytest suite (local FTP + SSH servers, in-memory databases)
docs/DEPLOY.md    detailed hosting guide and troubleshooting
```

## Development

```bash
pip install -r requirements-dev.txt
make lint && make test       # ruff + pytest; TEST_DATABASE_URL=... also tests a real database
```

CI (`.github/workflows/ci.yml`) runs lint and tests on Python 3.10–3.12 against PostgreSQL and builds the Docker image,
pushing it to GHCR on `main`.

## Security

Only `ALLOWED_USERS` can use the bot, and they can run commands on every server they add: treat those ids like root
access. SSH host keys are pinned on first use, destructive commands ask for confirmation, every action is in `/audit`,
and secrets are encrypted at rest. Prefer `ssh://`/`ftps://` over plain `ftp://`. See [SECURITY.md](SECURITY.md).

## Limits

* `/get` (server to Telegram) stages one volume at a time in the temp directory; uploads never touch disk.
* Run one instance per bot token (enforced by a lock in the database).
* Tested here against SQLite, PostgreSQL, FTP, SSH/SFTP and an in-memory MongoDB stand-in. MySQL/MariaDB, real
  MongoDB/Atlas, the Docker image and live Telegram have not been exercised.

## License

MIT, see [LICENSE](LICENSE) (put your name in the copyright line).
