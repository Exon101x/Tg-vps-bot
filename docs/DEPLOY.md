# Deploying vpsbot — the complete from-scratch guide

This page takes you from "nothing installed" to "bot running, verified, and monitored" on any of four
hosts, then helps you fix anything that goes wrong. Every command has been checked against the
repository as it is; nothing here is aspirational.

The bot talks to Telegram over MTProto and only makes **outbound** connections, so it needs no public
IP, no domain and no webhook. Anything that runs a Docker image or Python 3.10+ works.

> Read [CONFIG.md](CONFIG.md) for what every environment variable does, and
> [ARCHITECTURE.md](ARCHITECTURE.md) if you want to know *why* the pieces fit together this way.
> This page is the *how*.

**Table of contents**

- [What you need before starting](#what-you-need-before-starting)
- [Step 1 — Create the Telegram bot](#step-1--create-the-telegram-bot)
- [Step 2 — Get your API credentials](#step-2--get-your-api-credentials)
- [Step 3 — Pick the host](#step-3--pick-the-host)
- [Step 4 — Deploy](#step-4--deploy)
  - [Path A: the setup wizard (any host)](#path-a-the-setup-wizard-any-host)
  - [Render](#render)
  - [Docker / docker compose (any VPS)](#docker--docker-compose-any-vps)
  - [systemd (bare VPS, Raspberry Pi)](#systemd-bare-vps-raspberry-pi)
  - [Other container platforms](#other-container-platforms)
- [Step 5 — Verify the deployment](#step-5--verify-the-deployment)
- [Step 6 — After the first run](#step-6--after-the-first-run)
- [Sessions, locking and restarts](#sessions-locking-and-restarts)
- [HTTPS for the Mini App](#https-for-the-mini-app)
- [Troubleshooting](#troubleshooting)
- [Tips and tricks](#tips-and-tricks)

## What you need before starting

Gather these four things — the deployment itself takes under ten minutes once you have them.

| You need | What it is | Where to get it |
|---|---|---|
| A Telegram account | the owner account; it becomes the first allowed user | any Telegram client |
| `API_ID` + `API_HASH` | your app's MTProto credentials (not the bot token) | https://my.telegram.org → *API development tools* |
| `BOT_TOKEN` | the bot's identity and login | [@BotFather](https://t.me/BotFather) |
| A host | anywhere that runs Docker or Python 3.10+ | Render (free tier works), a $4 VPS, a Raspberry Pi, your laptop |

Also decide **where the database lives**. The default SQLite file is perfect for a VPS or a Pi with a
disk that survives restarts. On platforms that wipe the disk on every deploy (Render free, Heroku,
Railway, Cloud Run…) you **must** point `DATABASE_URL` at an external database — free tiers of
[Neon](https://neon.tech), [Supabase](https://supabase.com), [MongoDB Atlas](https://mongodb.com/atlas)
or [PlanetScale](https://planetscale.com) all work. PostgreSQL, MySQL/MariaDB and MongoDB URLs are
accepted as is.

## Step 1 — Create the Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Pick a display name (anything, e.g. "My server bot"), then a username ending in `bot`
   (e.g. `myserver_files_bot`).
3. BotFather replies with a token like `1234567:AAE4…`. That is `BOT_TOKEN`. Treat it like a
   password: anyone holding it can run the bot as you.
4. Recommended, same chat:
   - `/setuserpic` — a face for the bot;
   - `/setdescription` — what people see on the empty chat screen;
   - `/setabouttext` — the profile text;
   - `/setcommands` — optional: the bot uploads its own command list to Telegram on every
     startup, so this one is cosmetic only (BotFather's list is replaced at first boot).

5. **Get your numeric Telegram id** — you will need it for `ALLOWED_USERS`: open
   [@userinfobot](https://t.me/userinfobot) and send it anything; it replies with your id
   (e.g. `123456789`).

> [!NOTE]
> The bot is private by design: only ids in `ALLOWED_USERS` (plus `DEV_ID`/`ADMINS`) can use it.
> Everyone else gets silence. Do not publish the bot's link if you do not want users.

## Step 2 — Get your API credentials

The bot logs into MTProto with your **app** credentials (not the bot token):

1. Go to https://my.telegram.org and log in with your phone number and the login code Telegram
   sends to your app.
2. Click **API development tools**. The first time, you must create an "app": any title/short name
   works — this is *your* credential pair, not something users see.
3. Copy **App api_id** (a number, e.g. `12345678`) and **App api_hash** (32 hex characters). That
   is your `API_ID` / `API_HASH`.

> [!WARNING]
> `API_ID`/`API_HASH` are per-developer MTProto credentials. **Never commit them** — the `.env`
> file is git-ignored, keep it that way. And do not paste them into random "free API" sites:
> anything that holds them can act as your Telegram app.

## Step 3 — Pick the host

| Host | Best for | Watch out for |
|---|---|---|
| **Render** (blueprint) | zero-infra, free tier available | free sleeps after 15 min idle (bot sets `KEEP_ALIVE=1`), disk wiped per deploy |
| **Docker on a VPS** | full control, big files, 24/7 | you manage TLS yourself for the Mini App |
| **systemd on a VPS/Pi** | no Docker, minimal footprint | Python 3.10+ and git on the host |
| **Heroku / Railway / Fly.io / Koyeb / Cloud Run** | existing platform habits | run one instance, external DB, min instances = 1 where it scales to zero |

Decision rule: **if the host's disk survives restarts** (VPS, Pi, your laptop) → SQLite is fine.
**If anything wipes it** → external `DATABASE_URL` (and the session is still safe: it is snapshotted
into the database, see [Sessions, locking and restarts](#sessions-locking-and-restarts)).

## Step 4 — Deploy

### Path A: the setup wizard (any host)

```bash
git clone https://github.com/Exon101x/Tg-vps-bot && cd Tg-vps-bot
python deploy.py                       # interactive
python deploy.py --check --verify      # validate .env, check the token with Telegram, test the database
```

`deploy.py` is standard library only — no venv needed just to run it.

| Target | What the wizard does |
|---|---|
| `docker` | writes `.env`, optionally adds a bundled PostgreSQL (random password, written to both `DATABASE_URL` and `POSTGRES_PASSWORD`) or MongoDB, runs `docker compose [--profile …] up -d --build` |
| `systemd` | renders `deploy/vpsbot.service` for your directory and user, creates the venv, installs requirements, installs and starts the service (needs root; shows every command first) |
| `render` / `paas` | writes `deploy/render.env` / `deploy/paas.env` (mode 600, git-ignored) ready to upload as environment variables |
| `local` | creates `.venv` and installs the requirements |

Safe by design: an existing `SECRET_KEY` is kept (only `--rotate-key` replaces it), every rewrite keeps a
timestamped `.env.bak-…`, `.env` is mode 600, nothing runs without confirmation (or `--run`), and
`--dry-run` shows the plan without changing anything.

Scripted use:

```bash
python deploy.py --yes --target docker --db postgres --from-env \
  --allowed-users 123456789 --api-id 12345 --api-hash 0123456789abcdef \
  --bot-token 123:ABC --run
```

If you prefer doing it by hand, or want to understand each moving part, use one of the paths below.

### Render

`render.yaml` in the repository root is a Render Blueprint (Docker runtime, health check on `/healthz`).

1. Push this repository to GitHub/GitLab (fork it, or push your clone — Render builds from your repo).
2. Render Dashboard → **New → Blueprint**, pick the repository.
3. Fill in the values Render prompts for:

   | Value | Where from |
   |---|---|
   | `API_ID`, `API_HASH` | https://my.telegram.org |
   | `BOT_TOKEN` | @BotFather |
   | `ALLOWED_USERS` | your numeric Telegram id (e.g. from @userinfobot); comma-separate several |
   | `DATABASE_URL` | an external database URL, e.g. `postgresql://user:pass@host/db`, `mysql://…`, `mongodb+srv://…` |

   `SECRET_KEY` is generated for you. **After the first deploy open the service's Environment tab and copy
   it to a password manager.**
4. Apply. In the service logs you should see `Started. database=… session=fresh login`. Message the bot
   `/about` to confirm it is alive — the full checklist is in
   [Step 5 — Verify the deployment](#step-5--verify-the-deployment).

#### Free plan: what to expect (from Render's docs, render.com/docs/free)

* **It sleeps after 15 minutes without inbound traffic**, which would take the bot offline. The blueprint
  sets `KEEP_ALIVE=1`: the bot requests its own public URL (`RENDER_EXTERNAL_URL`) every 10 minutes, which
  counts as inbound traffic. Render does not officially support pings as a workaround, so treat it as best
  effort. The reliable fix is a paid plan: set `plan: starter` in `render.yaml` and `KEEP_ALIVE=0`.
* **The disk is ephemeral**: wiped on every redeploy, restart and spin-down. That is why `DATABASE_URL`
  must be external and why the Telegram session is snapshotted into the database
  (see [Sessions, locking and restarts](#sessions-locking-and-restarts)).
* **Render Postgres on the free plan expires after 30 days** (14 days to upgrade). A commented-out Render
  database is included in `render.yaml` for trying things out; for anything lasting use an external database.
* **Service-initiated traffic**: Render may suspend a free service that sends an unusually large volume of
  traffic over the public internet (it lists "accessing an external database" as an example). The bot's
  uploads and database queries count as such traffic, so heavy transfer use is better on a paid plan or a VPS.
* **Large files on small hosts:** uploads stream and use no disk, and `/get` stages one volume at a time
  and shrinks volumes to the free disk, so big files work even where the disk is small. Heavy traffic still
  counts towards the service-initiated traffic limit above, so big transfers belong on a paid plan or a VPS.
* **Render can restart a free service at any time.** The upload queue lives in the database, so unfinished
  uploads resume.
* Free instance hours are limited per month; one always-on service fits, two do not.

#### Zero-downtime deploys

Render starts the new instance before stopping the old one. The bot's health endpoint answers immediately,
then the new copy **waits for the old copy to release the database lock** before it touches Telegram, so
the two never share a session. Expect the new instance to log `another instance (…) is running; waiting for
it to stop` for a few seconds. A crashed instance's lock expires after `LOCK_TTL` seconds (default 60).

### Docker / docker compose (any VPS)

Works on x86_64 and ARM (Raspberry Pi, Graviton, Apple silicon) — `tgcrypto` is compiled in the image's
build stage.

**By hand:**

```bash
# 1. clone and enter
git clone https://github.com/Exon101x/Tg-vps-bot && cd Tg-vps-bot

# 2. configure (the four required values + SECRET_KEY)
cp .env.example .env && nano .env
# generate SECRET_KEY with:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# (that needs cryptography installed; ANY random string of 16+ characters also works)

# 3. start (pick one)
docker compose up -d --build                       # SQLite in a volume
docker compose --profile postgres up -d --build    # + PostgreSQL: DATABASE_URL=postgresql://bot:botpass@postgres:5432/bot
docker compose --profile mongo up -d --build       # + MongoDB:    DATABASE_URL=mongodb://mongo:27017/bot

# 4. watch it come up
docker compose logs -f bot
```

**Or skip the build with the image CI publishes on every push to `main`:**

```bash
docker run -d --name vpsbot --env-file .env -v vpsbot-data:/data \
  ghcr.io/exon101x/tg-vps-bot:main
docker logs -f vpsbot
```

The image runs as a non-root user, has a `HEALTHCHECK` baked in (pokes its own `/health` endpoint
every 60 s), and stores SQLite and the session under `/data` — mount it as a volume and restarts lose
nothing.

Updating later:

```bash
git pull && docker compose up -d --build     # from-source
docker pull ghcr.io/exon101x/tg-vps-bot:main && docker compose up -d   # hmm — see note below
```

> [!NOTE]
> `docker compose up -d` uses the **locally built** image. If you run the ghcr image with plain
> `docker run`, update with `docker pull … && docker rm -f vpsbot && docker run -d …` (same command
> as the first start). The lock in the database means the old container must fully stop first —
> expect a short `waiting for it to stop` in the new container's logs.

### systemd (bare VPS, Raspberry Pi)

No Docker needed — just Python 3.10+ and git. The bundled unit ships with hardening
(`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`,
`ReadWritePaths=/opt/vpsbot`) — keep those lines when you edit it.

```bash
sudo useradd --system --home /opt/vpsbot vpsbot
sudo git clone https://github.com/Exon101x/Tg-vps-bot /opt/vpsbot && cd /opt/vpsbot
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env && sudo chmod 600 .env
sudo chown -R vpsbot /opt/vpsbot
sudo cp deploy/vpsbot.service /etc/systemd/system/ && sudo systemctl enable --now vpsbot
journalctl -u vpsbot -f
```

Updating later:

```bash
cd /opt/vpsbot && sudo -u vpsbot git pull
sudo -u vpsbot venv/bin/pip install -r requirements.txt   # only if requirements.txt changed
sudo systemctl restart vpsbot
```

### Other container platforms

Railway, Fly.io, Koyeb, Northflank, Back4app, Zeabur, Cloud Run and similar all run the `Dockerfile` as is:

* set the environment variables from `.env.example` in the platform's UI/CLI (secrets as secrets);
* give it an external `DATABASE_URL` if its disk is not persistent;
* if it needs an HTTP port, it sets `PORT` and the bot answers on it (any path returns `{"status":"ok"}`);
* run exactly **one** instance and prefer a "recreate" deployment strategy (the lock covers rolling deploys
  anyway);
* Cloud Run note: it scales to zero — set *min instances = 1* (or accept cold starts after 15 min idle),
  and remember the Telegram session survives only because it is snapshotted into the database.

Heroku: `heroku stack:set container`, set config vars, `git push heroku main` (uses `heroku.yml`; the
`Procfile` is for buildpack deploys and runs the bot as a `worker`).

## Step 5 — Verify the deployment

Do not trust the green dashboard alone — walk this list once. It takes two minutes.

**The logs say it started.** Every host, one way or another:

```bash
docker compose logs --tail 50 bot        # docker compose
docker logs --tail 50 vpsbot             # plain docker
journalctl -u vpsbot -n 50 --no-pager    # systemd
# Render/PaaS: the dashboard's Logs tab
```

You want a line like `Started. database=sqlite:////data/bot.db session=fresh login` (or
`session=restored …`). Any traceback before that goes to
[Troubleshooting](#troubleshooting) — the table maps every common one.

**The health endpoint answers.** On any host with a port set (`PORT`/`HEALTH_PORT`):

```bash
curl http://127.0.0.1:8080/health        # from the host itself -> {"status":"ok"}
curl https://your-app.onrender.com/health # from anywhere, on a PaaS
```

No port configured (a pure chat bot on a VPS)? That is fine — skip this check; the log line above is
the signal.

**Telegram answers.** From your allowed account, in the bot's chat:

1. `/start` — the welcome card with buttons arrives.
2. `/about` — shows uptime, database, session source, speed profile.
3. `/add test ssh://root@YOUR_SERVER` (or any ftp:// address) — the bot tests the connection, asks
   for the password if it needs one, and replies with the pinned host key card.
4. Send any small file — it uploads; then `/queue` shows the job green.
5. `/vps` (SSH servers) — the dashboard with ten buttons; tap **Stats**.

All five pass → the bot is fully alive. If any fail, the
[Troubleshooting](#troubleshooting) table below covers the symptom.

**One more thing to do now (not later):** open the service's environment settings and copy
`SECRET_KEY` to a password manager. It encrypts every saved server credential; losing it makes them
unreadable (see the troubleshooting row *saved servers vanish*).

## Step 6 — After the first run

* **Back up `SECRET_KEY`** (if you have not yet) and your database credentials. The database holds
  the audit log, upload queue and the encrypted session snapshot — a nightly `pg_dump`/`mongodump`
  or a copy of the SQLite file (bot stopped or [VACUUM INTO](https://sqlite.org/lang_vacuum.html))
  is enough.
* **Updates**: `git pull` + restart (Docker: rebuild; systemd: restart). The database lock makes
  restarts safe — the new copy waits for the old one. The queue resumes unfinished uploads.
* **Optional extras**, each one env var away (details in [CONFIG.md](CONFIG.md)):
  * `DEV_ID` + `ADMINS` — the admin panel (`/admin`): users, orders, broadcast.
  * `DEV_CHANNEL` — your channel, credited in `/about` and the Mini App footer.
  * `FORCE_SUB` — gate usage on channel membership (add the bot as channel admin!).
  * `WEBAPP_URL` — the Mini App (needs HTTPS; see [HTTPS for the Mini App](#https-for-the-mini-app)).
  * `SUBSCRIPTION_CURRENCY` + `SUBSCRIPTION_PLANS` + `PAYMENT_INFO` — sell plans; or a payment
    gateway: `python3 setup_gateway.py` (guided wizard for UPI Gateway / UPI direct / CryptAPI).
  * `VPS_NODE_*` — provision real VPSes for buyers on your own node (root or passwordless sudo).
  * `MONITOR_INTERVAL`, `DISK_ALERT`, `MEM_ALERT` — the watchdog cadence and thresholds.
* **Watch it for a day**: `docker stats` / `htop` (memory should stay roughly flat), and the bot's
  own `/usage` + `/admin` overview for traffic.

## Sessions, locking and restarts

* The Telegram session file also stores which users the bot has seen. It is copied, encrypted, into the
  database every `SESSION_SYNC_INTERVAL` seconds and on shutdown, and restored when the file is missing, so
  a wiped disk does not force a fresh login and queued uploads keep working after a redeploy.
* A copy of the session from a *different* bot token is ignored. `RESET_SESSION=1` forces a fresh login. If
  Telegram rejects the stored session (revoked or duplicated), the bot discards it and logs in again with
  `BOT_TOKEN`.
* While running, the bot refreshes a lock row in the database every 15 s. If another copy takes the lock,
  this one shuts down instead of fighting over the session. `DISABLE_LOCK=1` turns this off (single-copy
  setups only).

## HTTPS for the Mini App

The Mini App (`/app`) is served by the bot's own HTTP port, but Telegram only opens web apps from an
**HTTPS** address with a valid certificate. The two-step recipe:

1. Expose the port over HTTPS:

   * **Render, Heroku, Railway, Fly.io, Koyeb, Cloud Run** — you already have an HTTPS URL; nothing to do.
   * **Your own VPS (Docker or systemd)** — set `HEALTH_BIND=127.0.0.1` so the port is not exposed raw,
     and proxy it with a certificate from Let's Encrypt:

     ```nginx
     # nginx: proxy the app on https://vps.example.com
     location / {
         proxy_pass http://127.0.0.1:8080;      # your HEALTH_PORT
         proxy_set_header Host $host;
         client_max_body_size 0;                # uploads through the app: no nginx cap
     }
     ```

     ```caddy
     # Caddy (automatic HTTPS) — a two-line Caddyfile
     vps.example.com {
         reverse_proxy 127.0.0.1:8080
     }
     ```

2. Point the bot at it: `WEBAPP_URL=https://vps.example.com` (on Render, just the service's public URL).

Now `/app` sends the open button, the chat menu button opens the app too, and users sign in with their
Telegram id automatically (the bot verifies Telegram's signature — see
[ARCHITECTURE.md](ARCHITECTURE.md#5-the-mini-app-webapipy)). Without `WEBAPP_URL` everything still works
from the chat; the button just isn't there.

Notes: keep-alive pings (`KEEP_ALIVE_URL`) can point at the same URL. If a firewall sits in front, allow
the proxy to reach `HEALTH_PORT`. Uploads through the app are capped by `WEB_MAX_MB` (default 4096) and
staged in the temp directory, so leave some disk headroom on the bot's host.

## Troubleshooting

Start here: the eight symptoms every deployment hits, then the full table. **The log line is always the
diagnosis** — `docker compose logs bot` / `journalctl -u vpsbot` / the PaaS log tab.

### The 8 classics

1. **Deploy "unhealthy" on the platform dashboard.** The container's `HEALTHCHECK` pokes
   `127.0.0.1:$PORT` — check the platform routes its port to the container's `$PORT` and that you did
   not set `PORT` to something else yourself. On Render the health check path is `/healthz` (both
   `/health` and `/healthz` answer `{"status":"ok"}`).
2. **`another instance (…) is running; waiting for it to stop` for minutes.** An old copy still holds
   the database lock. Either it is genuinely running (stop it: `docker rm -f` / `systemctl stop`), or it
   crashed less than `LOCK_TTL` (default 60 s) ago — wait one minute. "Forever" means a **second copy
   somewhere else** (your laptop while the cloud copy is live is the classic). Pick one.
3. **`AUTH_KEY_DUPLICATED` in logs.** Same cause as 2: two copies share a Telegram session. Stop one,
   then restart the survivor so Telegram re-issues it cleanly. If it persists after stopping everything:
   `RESET_SESSION=1` once, remove it after the first successful start.
4. **Bot silent on a free plan.** The service slept. Check `KEEP_ALIVE=1` and that
   `RENDER_EXTERNAL_URL` (or `KEEP_ALIVE_URL`) points at the service's public URL. If the platform
   suspends for "service-initiated traffic" (heavy uploads count), move to paid or a VPS.
5. **Saved servers vanished / `InvalidToken` / credentials unreadable.** `SECRET_KEY` changed — the
   old one is the only key that can decrypt the saved passwords and the session snapshot. Restore it
   (this is why Step 5 says to back it up immediately) or `/add` the servers again with the new key.
6. **`database not ready … retry` at startup.** The database container is still booting. The bot retries
   for ~30 s (`DB_CONNECT_RETRIES`, 10 tries × 3 s); if your DB is slower to accept connections, raise
   it in `.env`.
7. **Bot worked, then stopped after a redeploy on Render free.** The disk was wiped and SQLite was in
   use. Fix: external `DATABASE_URL`. The session is already safe (snapshotted into the DB), only the
   SQLite file was lost.
8. **`Connection to Telegram failed` / startup crash mentioning `api_id`.** Wrong `API_ID`/`API_HASH`
   pair (a stale pair from another account, or swapped fields). Re-copy both from
   https://my.telegram.org → *API development tools*. Check for trailing spaces in `.env`.

### The full table

| Symptom | Cause / fix |
|---|---|
| Deploy "unhealthy" | Check `PORT` is not overridden to something the platform doesn't route; the bot listens on `$PORT`/`HEALTH_PORT` |
| `another instance … waiting` forever | An old copy is still running, or crashed less than `LOCK_TTL` s ago. Stop it or wait |
| `AUTH_KEY_DUPLICATED` in logs | Two copies share a session (e.g. a local run while the cloud copy is live). Stop one |
| Bot goes silent on a free plan | Service slept: check `KEEP_ALIVE=1` and `RENDER_EXTERNAL_URL`, or move to a paid plan |
| Saved servers vanish after a restart | SQLite on an ephemeral disk. Set `DATABASE_URL` to an external database |
| `InvalidToken` / servers unreadable | `SECRET_KEY` differs from the one that encrypted them |
| `database not ready … retry` | The database is still starting; the bot retries for ~30 s (`DB_CONNECT_RETRIES`) |
| Credentials rejected but they are right | Saved host key changed (server reinstalled) — `/del` and `/add` the server to re-pin, that refusal is the feature |
| `/add` fails, connection test times out | The host cannot reach your server (firewall, wrong port), or the credentials are wrong — the error text carries the reason; check from the host itself: `docker compose exec bot python -c "import socket; socket.create_connection(('YOUR_SERVER', 22), 5)"` |
| FTP works, SFTP times out | Port 22 blocked between the hosts, or the SSH server forbids password auth for that user. Try `ssh user@host` from the bot's host |
| Bot cannot see the channel (`FORCE_SUB`) | Add the bot as **admin** in the channel; the gate fails open (with an hourly warning in the logs) otherwise |
| Uploads stuck in `queued` | A previous copy was stopped mid-upload; the queue resumes on start. `/queue` → **Retry failed** for the red ones. Still stuck → `/stop all`, then re-send |
| Upload fails at 2 GB on FTP | Telegram caps what it *sends the bot* at 2 GB without Premium — split with `/multi` + `/done` (the bot joins volumes while streaming, no extra disk needed) |
| Mini App opens blank / "how to enable" | `WEBAPP_URL` missing or not HTTPS. Set it to the bot's public HTTPS address; on your own VPS proxy `HEALTH_BIND=127.0.0.1` through nginx/Caddy first |
| Mini App sign-in fails | The app must be opened from Telegram (the `/app` button), not a browser tab — the signature comes from Telegram's client. Clock skew > 5 min also breaks it: sync time (`timedatectl status`) |
| Payments not auto-activating | Gateway webhook did not arrive. Check the gateway's dashboard; the bot also self-heals via a polling backstop. For CryptAPI, the `WEBAPP_URL` domain must be verified once at cryptapi.io |
| `/admin` says admins only | `DEV_ID` or `ADMINS` not set to your id, or set with a typo (must be numeric). Check with `/about` — it shows the admin line only to admins |
| Nothing in logs at all | The process died before logging (bad `.env`, missing `SECRET_KEY` entirely, Python < 3.10). Run `python bot.py` in the foreground once and read the first error |

### Reading the logs like a pro

```bash
docker compose logs -f bot --tail 100        # follow live
docker compose logs bot | grep -iE "error|fail|warn"   # just the pain
journalctl -u vpsbot -f                      # systemd, live
journalctl -u vpsbot --since "1 hour ago"    # systemd, window
```

The log line you want to see on every start: `Started. database=… session=…`. Anything between the
container start and that line is either the lock wait (normal on rolling deploys) or the failure you
are hunting.

### When all else fails

1. `python deploy.py --check --verify` — validates `.env` shape, checks the token **with Telegram
   directly**, and tests the database connection. It is the fastest split of "my config" vs "the world".
2. Stop everything, then start one copy in the foreground with `LOG_LEVEL=DEBUG`:
   `docker compose run --rm bot` or, on systemd, `sudo systemctl stop vpsbot && sudo -u vpsbot venv/bin/python bot.py`.
   The first traceback is the real error; everything after it is noise.
3. `RESET_SESSION=1` (one start) fixes stale-session states after token changes or moves between hosts.
4. Still stuck? [Open an issue](https://github.com/Exon101x/Tg-vps-bot/issues) with: the deploy path,
   the log lines around the failure, and `.env` **with secrets redacted**.

## Tips and tricks

**Speed.** `/perf` shows the profile in use; the bot picks `light`/`balanced`/`turbo` from the cores
and RAM it can see (container limits included). Force one with `PERFORMANCE=turbo`, or tune single
values (`TG_PARALLEL`, `SFTP_WINDOW_MB`, `FTP_BLOCK_KB`) — see
[CONFIG.md → Performance tuning](CONFIG.md#performance-tuning). `SFTP_WINDOW_MB` helps most on
high-latency links; `TG_PARALLEL × TG_WINDOW_MB` is roughly the RAM the downloader will use.

**Free-tier survival.** `KEEP_ALIVE=1` (default on Render) pings the service's own URL every 10 min.
Keep the database external, keep `SECRET_KEY` backed up, and expect the queue to resume after every
spin-up — that is the design, not luck.

**One bot, many users.** `ALLOWED_USERS` comma-separates ids; `ADMINS` comma-separates admin ids
(`DEV_ID` is an admin automatically). The admin panel (`/admin`) shows users, usage, orders and has
broadcast; the Mini App's Admin tab shows the same with live cards.

**Selling plans.** The default catalog is in `SUBSCRIPTION_PLANS`; `setup_gateway.py` configures a
real payment gateway (UPI Gateway, UPI direct QR, or CryptAPI crypto) with live tests. Without a
gateway the assisted flow works: buyer pays manually and sends `/paid <reference>`, you approve with
one tap on the admin card.

**The `/multi` trick for > 2 GB (no Premium).** Send the split volumes (.001, .002, …) to the chat
with `/multi [name]` first, then `/done`: the bot joins them **while streaming**, so it never needs
the combined file's disk space, and lands it on any FTP/FTPS/SFTP server. `7-Zip` volumes or
`split -b 1900M -d -a 3 --numeric-suffixes=1 file file.` both work.

**Monitoring without paying attention.** `/monitor on` per server sends disk-full / memory-pressure /
unreachable alerts to your chat on `MONITOR_INTERVAL` (default 5 min, thresholds `DISK_ALERT` /
`MEM_ALERT`). The audit log (`/audit`) records every action with a UTC timestamp.

**Disk hygiene on the bot's host.** Uploads stream and use ~no disk. The two things that grow:
the SQLite file (fine for years on a VPS) and Mini App upload staging in `$TMPDIR` — leave a few GB
headroom, or cap uploads with `WEB_MAX_MB`.

**Move hosts in 5 minutes.** New host: deploy (any path) → set the same `SECRET_KEY` +
`DATABASE_URL` + `BOT_TOKEN` → start. The session restores from the database snapshot, the queue
resumes, saved servers decrypt. The old copy must be fully stopped (the lock enforces it — that is
the `waiting` line you will see, and it is correct).

**Keep the Docker image honest.** CI builds and publishes `ghcr.io/exon101x/tg-vps-bot:main` on
every push. Pin to a specific commit SHA (`ghcr.io/exon101x/tg-vps-bot:sha-83b23db`) if you want
reproducible updates instead of tracking `main`.
