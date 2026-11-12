# Configuration reference

Everything is configured with environment variables. A `.env` file next to the bot works too —
real environment variables win over it (the file is a fallback, not an override).
`.env.example` in the repository root is the annotated template.

**Robust by design:** every *optional* numeric variable is parsed tolerantly — a typo logs a warning and
falls back to the default. The bot never crashes at import over a config mistake; only a broken *required*
variable stops startup, with a clear message.

## Table of contents

- [Required](#required)
- [Database](#database)
- [Hosting and runtime](#hosting-and-runtime)
- [Monitoring](#monitoring)
- [Mini App authentication](#mini-app-authentication)
- [Transfer limits](#transfer-limits)
- [Performance tuning](#performance-tuning)
- [Session and single-instance lock](#session-and-single-instance-lock)
- [Docker-specific values](#docker-specific-values)

## Required

| Variable | Type | Meaning |
|---|---|---|
| `API_ID` | int | App id from <https://my.telegram.org> (API development tools) |
| `API_HASH` | string | Its companion hash, same page |
| `BOT_TOKEN` | string | From [@BotFather](https://t.me/BotFather) |
| `SECRET_KEY` | string | Encrypts saved server passwords/keys (Fernet). A Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) or any random string of 16+ characters. **Back it up** — losing it makes every saved server unreadable; changing it the same |
| `ALLOWED_USERS` | csv ints | Telegram user ids allowed to use the bot, comma-separated (`123456789,987654321`). The bot refuses to start without at least one. Get your id from [@userinfobot](https://t.me/userinfobot) |

## Database

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///bot.db` | `sqlite:///relative.db` or `sqlite:////absolute/path.db` · `postgresql://user:pass@host:5432/db` (`postgres://` accepted) · `mysql://user:pass@host:3306/db` · `mongodb://user:pass@host:27017/db` or `mongodb+srv://…` (Atlas) |
| `MONGO_DB` | `vpsbot` | Database name for MongoDB URLs that carry no path (`mongodb://host:27017`) |
| `BOT_DB` | — | Alternative to `DATABASE_URL` for SQLite: just the file path |
| `DB_CONNECT_RETRIES` | `10` | Retries (3 s apart) while the database is still starting — matters on PaaS where the DB container starts in parallel with the app |

Tables are created automatically and older databases upgrade in place. To move between databases:
`python migrate.py <from-url> <to-url>` (same `SECRET_KEY`!).

## Hosting and runtime

| Variable | Default | Meaning |
|---|---|---|
| `PORT` / `HEALTH_PORT` | unset | If set, the bot runs an HTTP server on that port: it serves the **Mini App** (`/app`, `/static/…`, `POST /api/…`) and still answers `200 {"status":"ok"}` on any other path (Render checks `/healthz`, Docker `/health`). PaaS platforms set `PORT` themselves; `HEALTH_PORT` is for hosts that don't |
| `HEALTH_BIND` | `0.0.0.0` | Bind address of that server. `0.0.0.0` suits PaaS; use `127.0.0.1` on your own VPS and put a TLS proxy in front |
| `WEBAPP_URL` | unset | The bot's public **HTTPS** address (e.g. `https://your-app.onrender.com`). When set, `/app` gets its button and the chat menu button opens the Mini App. Telegram refuses plain-HTTP web apps, so an `http://` value is ignored |
| `WEB_MAX_MB` | `4096` | Size cap for files uploaded through the Mini App (they are staged in the temp directory before the queue streams them out; `0` = no limit) |
| `KEEP_ALIVE` | unset | `1` pings a URL every `KEEP_ALIVE_INTERVAL` seconds so free web services (Render) don't sleep. Set `0` on paid plans |
| `KEEP_ALIVE_URL` | `RENDER_EXTERNAL_URL` | The URL to ping; defaults to the one Render injects |
| `KEEP_ALIVE_INTERVAL` | `600` | Seconds between pings |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SESSION_DIR` | `.` | Where the Telegram session file lives. Docker uses `/data` (the volume) |

## Monitoring

Applies to servers with `/monitor on` (SSH). The loop runs every `MONITOR_INTERVAL` seconds and alerts when:
the server is unreachable, disk usage ≥ `DISK_ALERT` %, memory ≥ `MEM_ALERT` %, or load > 2 × cores.
State is per server: an alert fires once when it appears and once more when it clears, not every cycle.

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `MONITOR_INTERVAL` | `300` | ≥ 30 | Seconds between checks |
| `DISK_ALERT` | `90` | 1–100 | Disk % that triggers an alert |
| `MEM_ALERT` | `90` | 1–100 | Memory % that triggers an alert |

## Mini App authentication

No configuration of its own: the web app signs in with the initData Telegram produces when it opens.
The backend verifies the HMAC-SHA256 signature with `BOT_TOKEN` (the algorithm from Telegram's docs,
constant-time compare, and `auth_date` must be fresher than 24 h), then applies the same `ALLOWED_USERS`
gate as the chat. There are no cookies, no sessions and no stored secrets; every API request carries
its own verified signature. See [ARCHITECTURE.md](ARCHITECTURE.md#the-mini-app-webapi) for the flow.

## Transfer limits

| Variable | Default | Meaning |
|---|---|---|
| `TG_MAX_MB` | `1900` | Largest single file `/get` sends; anything bigger goes out as `.001 .002 …` volumes. Telegram's hard cap is 2 GB for bots — leave headroom |
| `MAX_FETCH_MB` | `0` | Cap in MB for `/fetch` downloads streamed **through** the bot (FTP/FTPS targets). `0` = no limit. SSH servers download by themselves and ignore this. Enforced even without a `Content-Length` |
| `MAX_RETRIES` | `5` | Attempts per queued upload before it is marked failed (retryable via `/retry`) |

## Performance tuning

`PERFORMANCE=auto` (the default) detects CPU cores and RAM — honouring cgroup container limits — and picks
a profile. `/perf` shows what was detected and chosen.

| Profile | Chosen when | `TG_PARALLEL` | `TG_WINDOW_MB` | `SFTP_WINDOW_MB` | `FTP_BLOCK_KB` | `PARALLEL_PER_SERVER` |
|---|---|---|---|---|---|---|
| `light` | < 700 MB RAM or 1 core | 4 | 8 | 2 | 64 | 1 |
| `balanced` | in between | 8 | 8 | 8 | 256 | 1 |
| `turbo` | 4+ cores and 3+ GB RAM | 16 | 16 | 32 | 1024 | 2 |

Each column is also an environment variable that **overrides** the profile for that one value:

| Variable | Meaning |
|---|---|
| `TG_PARALLEL` | Simultaneous download windows from Telegram |
| `TG_WINDOW_MB` | MB per window; peak memory ≈ `TG_PARALLEL × TG_WINDOW_MB` |
| `SFTP_WINDOW_MB` | SFTP pipelining window — bigger helps on high-latency links |
| `FTP_BLOCK_KB` | FTP send block size |
| `PARALLEL_PER_SERVER` | Simultaneous uploads per server (queue stays fair per user either way) |

Guard rail: buffers never exceed 1/8 of the RAM the process may use, whatever you set.

## Session and single-instance lock

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `SESSION_SYNC_INTERVAL` | `300` | ≥ 10 | Seconds between Telegram-session snapshots into the database (and on every shutdown). Lets a wiped-disk host log back in without @BotFather |
| `RESET_SESSION` | unset | — | Set to `1` to ignore the stored session and log in fresh with `BOT_TOKEN` |
| `LOCK_TTL` | `60` | ≥ 5 | Seconds after which an unrefreshed single-instance lock counts as abandoned (crashed copy) |
| `DISABLE_LOCK` | unset | — | `1` skips the single-instance lock entirely. Only for setups where you are certain one copy runs. With the lock on, a second copy waits politely instead of fighting over the Telegram session — that is what makes zero-downtime deploys safe |

## Docker-specific values

The `Dockerfile` presets `SESSION_DIR=/data` and `DATABASE_URL=sqlite:////data/bot.db`, declares the
`/data` volume, and runs `healthcheck.py` as the container health check. Relevant overrides:

* `docker compose --profile postgres up -d --build` starts a bundled PostgreSQL (password via
  `POSTGRES_PASSWORD`, or everything generated by `python deploy.py --target docker`);
* `docker compose --profile mongo up -d --build` the same for MongoDB;
* the health endpoint is off unless you set `PORT`/`HEALTH_PORT` — platforms that need it set it themselves.
