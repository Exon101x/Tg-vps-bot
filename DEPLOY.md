# Deploying

The bot talks to Telegram over MTProto and only makes **outbound** connections, so it needs no public IP, domain or
webhook. Anything that runs a Docker image or Python 3.10+ works. ## Fastest way: the setup wizard

```bash
python deploy.py                       # interactive
python deploy.py --check --verify      # validate .env, check the token with Telegram, test the database
```

| Target | What the wizard does |
|---|---|
| `docker` | writes `.env`, optionally adds a bundled PostgreSQL (random password, written to both `DATABASE_URL` and `POSTGRES_PASSWORD`) or MongoDB, runs `docker compose [--profile …] up -d --build` |
| `systemd` | renders `deploy/vpsbot.service` for your directory and user, creates the venv, installs requirements, installs and starts the service (needs root; shows every command first) |
| `render` / `paas` | writes `deploy/render.env` / `deploy/paas.env` (mode 600, git-ignored) ready to upload as environment variables |
| `local` | creates `.venv` and installs the requirements |

Safe by design: an existing `SECRET_KEY` is kept (only `--rotate-key` replaces it), every rewrite keeps a timestamped
`.env.bak-…`, `.env` is mode 600, nothing runs without confirmation (or `--run`), and `--dry-run` shows the plan without
changing anything. Scripted use: `python deploy.py --yes --target docker --db postgres --from-env --allowed-users 123`.

---------------------------------------------------------------------------------------------------------------

Three rules for every host:

1. **One running copy per bot token.** (The bot enforces this itself with a lock in the database, see below.)
2. **Use an external database** on hosts whose disk is wiped on restart (`DATABASE_URL`, see the README).
3. **Back up `SECRET_KEY`.** It encrypts your saved servers; lose it and they cannot be read.

---------------------------------------------------------------------------------------------------------------

## Render

`render.yaml` in the repository root is a Render Blueprint (Docker runtime, health check on `/healthz`).

1. Push this repository to GitHub/GitLab.
2. Render Dashboard → **New → Blueprint**, pick the repository.
3. Fill in the values Render prompts for:

   | Value | Where from |
   |---|---|
   | `API_ID`, `API_HASH` | https://my.telegram.org |
   | `BOT_TOKEN` | @BotFather |
   | `ALLOWED_USERS` | your numeric Telegram id (e.g. from @userinfobot); comma-separate several |
   | `DATABASE_URL` | an external database URL, e.g. `postgresql://user:pass@host/db`, `mysql://…`, `mongodb+srv://…` |

   `SECRET_KEY` is generated for you. **After the first deploy open the service's Environment tab and copy it to a
   password manager.**
4. Apply. In the service logs you should see `Started. database=… session=fresh login`. Message the bot `/about`.

### Free plan: what to expect (from Render's docs, render.com/docs/free)

* **It sleeps after 15 minutes without inbound traffic**, which would take the bot offline. The blueprint sets
  `KEEP_ALIVE=1`: the bot requests its own public URL (`RENDER_EXTERNAL_URL`) every 10 minutes, which counts as inbound
  traffic. Render does not officially support pings as a workaround, so treat it as best effort. The reliable fix is a
  paid plan: set `plan: starter` in `render.yaml` and `KEEP_ALIVE=0`.
* **The disk is ephemeral**: wiped on every redeploy, restart and spin-down. That is why `DATABASE_URL` must be external
  and why the Telegram session is snapshotted into the database (see "Sessions" below).
* **Render Postgres on the free plan expires after 30 days** (14 days to upgrade). A commented-out Render database is
  included in `render.yaml` for trying things out; for anything lasting use an external database.
* **Service-initiated traffic**: Render may suspend a free service that sends an unusually large volume of traffic over
  the public internet (it lists "accessing an external database" as an example). The bot's uploads and database queries
  count as such traffic, so heavy transfer use is better on a paid plan or a VPS.
* **Large files on small hosts:** uploads stream and use no disk, and `/get` stages one volume at a time and shrinks
  volumes to the free disk, so big files work even where the disk is small. Heavy traffic still counts towards the
  service-initiated traffic limit above, so big transfers belong on a paid plan or a VPS.
* **Render can restart a free service at any time.** The upload queue lives in the database, so unfinished uploads resume.
* Free instance hours are limited per month; one always-on service fits, two do not.

### Zero-downtime deploys

Render starts the new instance before stopping the old one. The bot's health endpoint answers immediately, then the new
copy **waits for the old copy to release the database lock** before it touches Telegram, so the two never share a
session. Expect the new instance to log `another instance (…) is running; waiting for it to stop` for a few seconds.
A crashed instance's lock expires after `LOCK_TTL` seconds (default 60).

---------------------------------------------------------------------------------------------------------------

## Docker / docker compose (any VPS)

```bash
cp .env.example .env && nano .env
docker compose up -d --build                       # SQLite in a volume
docker compose --profile postgres up -d --build    # + PostgreSQL: DATABASE_URL=postgresql://bot:botpass@postgres:5432/bot
docker compose --profile mongo up -d --build       # + MongoDB:    DATABASE_URL=mongodb://mongo:27017/bot
docker compose logs -f bot
```

The image is also published to GHCR by the CI workflow on pushes to `main` (`ghcr.io/<owner>/<repo>`), so platforms
that deploy from a registry image can use that.

## systemd (bare VPS, Raspberry Pi)

```bash
sudo useradd --system --home /opt/vpsbot vpsbot
sudo git clone <your-repo> /opt/vpsbot && cd /opt/vpsbot
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env && sudo chmod 600 .env
sudo chown -R vpsbot /opt/vpsbot
sudo cp deploy/vpsbot.service /etc/systemd/system/ && sudo systemctl enable --now vpsbot
journalctl -u vpsbot -f
```

## Other container platforms

Railway, Fly.io, Koyeb, Northflank, Back4app, Zeabur, Cloud Run and similar all run the `Dockerfile` as is:

* set the environment variables from `.env.example` in the platform's UI/CLI (secrets as secrets);
* give it an external `DATABASE_URL` if its disk is not persistent;
* if it needs an HTTP port, it sets `PORT` and the bot answers on it (any path returns `{"status":"ok"}`);
* run exactly **one** instance and prefer a "recreate" deployment strategy (the lock covers rolling deploys anyway).

Heroku: `heroku stack:set container`, set config vars, `git push heroku main` (uses `heroku.yml`; the `Procfile` is
for buildpack deploys and runs the bot as a `worker`).

---------------------------------------------------------------------------------------------------------------

## Big servers

Nothing needs configuring: the bot reads the CPU cores and RAM it may use (container limits included) and picks a speed
profile; `/perf` shows it. With 4+ cores and 3+ GB RAM it runs `turbo`. `PERFORMANCE=turbo` forces it, and single values
(`TG_PARALLEL`, `SFTP_WINDOW_MB`, ...) can be set in `.env`. Files over 2 GB reach FTP servers through `/multi` + `/done`
on any host, because the bot joins the volumes while streaming and never needs the disk space.

## Sessions, locking and restarts

* The Telegram session file also stores which users the bot has seen. It is copied, encrypted, into the database every
  `SESSION_SYNC_INTERVAL` seconds and on shutdown, and restored when the file is missing, so a wiped disk does not force
  a fresh login and queued uploads keep working after a redeploy.
* A copy of the session from a *different* bot token is ignored. `RESET_SESSION=1` forces a fresh login. If Telegram
  rejects the stored session (revoked or duplicated), the bot discards it and logs in again with `BOT_TOKEN`.
* While running, the bot refreshes a lock row in the database every 15 s. If another copy takes the lock, this one
  shuts down instead of fighting over the session. `DISABLE_LOCK=1` turns this off (single-copy setups only).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Deploy "unhealthy" | Check `PORT` is not overridden to something the platform doesn't route; the bot listens on `$PORT`/`HEALTH_PORT` |
| `another instance … waiting` forever | An old copy is still running, or crashed less than `LOCK_TTL` s ago. Stop it or wait |
| `AUTH_KEY_DUPLICATED` in logs | Two copies share a session (e.g. a local run while the cloud copy is live). Stop one |
| Bot goes silent on a free plan | Service slept: check `KEEP_ALIVE=1` and `RENDER_EXTERNAL_URL`, or move to a paid plan |
| Saved servers vanish after a restart | SQLite on an ephemeral disk. Set `DATABASE_URL` to an external database |
| `InvalidToken` / servers unreadable | `SECRET_KEY` differs from the one that encrypted them |
| `database not ready … retry` | The database is still starting; the bot retries for ~30 s (`DB_CONNECT_RETRIES`) |
