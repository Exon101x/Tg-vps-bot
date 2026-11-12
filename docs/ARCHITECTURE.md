# Architecture

How the code is put together: modules, the main data flows, the database schema, the security model and
the testing strategy. For what you can type in chat see [COMMANDS.md](COMMANDS.md); for every setting see
[CONFIG.md](CONFIG.md).

## Table of contents

- [Design goals](#design-goals)
- [Module map](#module-map)
- [The main data flows](#the-main-data-flows)
- [Database schema](#database-schema)
- [Security model](#security-model)
- [Testing strategy](#testing-strategy)

## Design goals

1. **No temp disk for uploads.** Bytes flow from Telegram straight into the server connection; the only
   place files touch disk is `/get` staging one volume at a time.
2. **Survive restarts.** The queue, upload progress and the Telegram session live in the database, so a
   redeploy, a crash or a wiped-disk free-tier host loses nothing and forces no fresh login.
3. **One codebase, every host.** Pure-Python modules + one Dockerfile; CPU/RAM detection picks sane
   buffer sizes anywhere from a 512 MB free instance to a big server.
4. **Least surprise.** Destructive actions confirm first, every action is audited, credentials are
   encrypted at rest, and a config typo never crashes the bot at import.

## Module map

```
bot.py            The Telegram layer: auth gate, every /command handler, confirmation dialogs,
                  the monitoring loop, keep-alive. Everything user-facing lives here.

webapi.py         The Mini App: the bot's HTTP port serves the web UI (webapp/) and a JSON API.
                  Telegram initData HMAC authentication (no cookies, no sessions), the file
                  manager, the console, stats, and the chunked resumable web-upload path.

jobs.py           The persistent upload queue worker: picks jobs per server (PARALLEL_PER_SERVER),
                  drives the transfer, updates progress, retries with backoff, resumes from .part files.
                  Sources: tg: (a message), tgm: (volumes to join), web: (a staged Mini App upload),
                  http: (a URL).

sources.py        Streaming sources: parallel windowed downloads from Telegram (length-checked) and
                  HTTP(S) with Range resume. The SSRF guard lives here: host resolved once, connection
                  pinned to the address that passed the check, redirects re-validated hop by hop.

remote.py         Protocol clients: FTP/FTPS (ftplib), SSH + SFTP (paramiko). Host-key pinning,
                  sudo prefixing, path sanitising, the exists()/SIZE fallback, resume support.

store.py          Domain API over the data (servers, jobs, audit, usage, orders, subscriptions,
                  VPS allocations, blobs) + Fernet encryption of credentials before anything
                  is written.

backends.py       Four backends behind one interface: SQLite, PostgreSQL/MySQL (SQLAlchemy),
                  MongoDB (pymongo). Document-oriented rows, so all four behave identically.

subscriptions.py  The plan catalog (defaults or SUBSCRIPTION_PLANS JSON), the order lifecycle
                  (pending -> awaiting -> approved/rejected), plan limits (which plans may
                  hold how many servers / VPSes) and the admin revenue stats.

hosting.py        VPS provisioning on the owner's host node over SSH: alnum-only generated
                  credentials, admin-defined command templates, renewal extensions, and the
                  expiry sweep the monitor loop runs.

payments.py       Payment gateways, all optional: YouPI-PG (self-hosted UPI checkout, HMAC-SHA256
                  webhooks), direct UPI intent links + locally generated QR, and CryptAPI
                  (crypto to the owner's own wallets, RSA-SHA256 signed callbacks). Order
                  state lives in the kv store ("pay:<order id>": nonces, refs, received totals).

session_sync.py   Telegram session snapshots (encrypted into the kv table) and the single-instance
                  lock with heartbeats - the two mechanisms behind zero-downtime deploys.

largefiles.py     Pure logic for >2 GB files: volume planning (/get), volume ordering (/multi),
                  and the shell script /join runs server-side.

perf.py           CPU/RAM detection (cgroup v2 and v1 aware) and the speed profiles.

deploy.py         The setup wizard (standard library only): validates credentials, writes a safe .env,
                  can bundle PostgreSQL/MongoDB, prepares Docker/systemd/Render/PaaS.

migrate.py        One-shot data copy between two DATABASE_URLs.

healthcheck.py    What the Docker HEALTHCHECK runs: hits the health endpoint.
```

Dependency direction: `bot.py` → `jobs.py` → `sources.py` + `remote.py` → `store.py` → `backends.py`;
`subscriptions.py`, `hosting.py` and `payments.py` sit on `store.py` (hosting also uses `remote.py`
for the node; payments' webhook handlers call back into `bot.py`'s fulfillment through `webapi.py`).
`deploy.py` and `migrate.py` are standalone tools; `perf.py`, `largefiles.py`, `util.py` are leaf libraries.
`webapi.py` shares `bot.py`'s helpers through late imports (it runs inside the same process) and uses the
same `store`/`remote`/`jobs` layers, so a web action and the equivalent chat action behave identically.

## The main data flows

### 1. Upload (send a file in chat)

```
Telegram ──pyrogram──> bot.py handler
                        │  adds a job row (state=queued) ──> store.py ──> database
                        ▼
                    jobs.py worker (per server, PARALLEL_PER_SERVER slots)
                        │  opens a source: sources.py parallel windows (TG_PARALLEL × TG_WINDOW_MB)
                        │  opens a sink:   remote.py stream to FTP/FTPS/SFTP, dest name.  .part
                        ▼
                    server-side .part file grows; progress edits speed/ETA
                        │  failure → retry with backoff; resume = append to .part (size check first)
                        ▼
                    rename .part → final name, job row state=done
```

Nothing touches the bot's disk. Restart at any point: the job row is still `running`/`queued`, so the
worker picks it up again and resumes.

### 2. `/get` (server → Telegram, with volumes)

`bot.py` asks `remote.py` for the size, `largefiles.plan_volumes()` produces the volume plan
(index, offset, length), and each volume is: streamed server → local temp file → sent to Telegram →
deleted. Volume size = `TG_MAX_MB` or `-v`, shrunk to 85 % of free disk (min 1 MiB, max 999 volumes).
Status edits are best-effort, so deleting the progress message never aborts the remaining volumes.

### 3. `/multi` + `/done` (join volumes while streaming)

`/multi` opens a collection keyed by user; every part sent is buffered as a job referencing the Telegram
message. `/done` → `largefiles.order_volumes()` validates the set (same base name, no gaps, no duplicates)
→ the worker streams the parts **back-to-back from Telegram into one target file** on the server. Resume
works inside any volume; the queue keeps order.

### 4. Session sync and the single-instance lock

```
every SESSION_SYNC_INTERVAL s and on shutdown:
  sqlite session file ──backup()──> temp copy ──encrypt──> kv table
on start, if the session file is missing:
  kv table ──decrypt──> session file (a session from another bot token is ignored)
lock: a row in kv, heartbeaten every 15 s; a second copy waits until LOCK_TTL passes without a beat
```

That pair is what makes ephemeral-disk hosts (Render free tier) and zero-downtime deploys work.

### 5. The Mini App (`webapi.py`)

```
Telegram WebView ──HTTPS──> webapi.py on the bot's health port
   GET  /app, /static/*      the UI (vanilla HTML/JS, no build step)
   POST /api/<route>          every request carries the initData Telegram signed at open
        │  verify HMAC-SHA256(WebAppData, BOT_TOKEN), constant-time, auth_date < 24 h
        │  the user id must be in ALLOWED_USERS (the same gate as the chat)
        ▼
   routes: ls/cd/mkdir/rm/mv · term (the /sh guard + audit) · stats/view (the /vps views)
           send (volumes to the user's chat) · fetch · servers/* · jobs · audit
        │
        ▼  uploads: up/init → up/chunk (4 MiB, idempotent, resumable) → up/finish
   staged in tmpdir/vpsbot-webuploads ──> a job row with src web:<path>
        ▼
   jobs.py streams the staged file to the server like any upload (retries, resume, dup policy,
   folder templates); the staged file is deleted when the job ends (kept for /retry on failure,
   swept after a day otherwise)
```

`/health` and every unknown path still answer `200 {"status":"ok"}`, so platform health checks are
unaffected. `WEBAPP_URL` only feeds the `/app` button and the menu button - Telegram requires HTTPS.

## Database schema

Five document-shaped tables (`backends.py`, identical across all four databases):

| Table | Key | Contents |
|---|---|---|
| `servers` | `(uid, name)` unique | One row per saved server: scheme, host, port, user, **encrypted** password/key, pinned host key, cwd, monitoring flag, folder template, duplicate policy |
| `active` | `uid` unique | The user's currently active server name |
| `audit` | `(uid, ts)` indexed | One row per action: command, transfer, delete — what `/audit` shows |
| `jobs` | `(uid, state)` indexed | The upload queue: chat/message ids, source reference, destination, size, uploaded, state, error, timestamps |
| `kv` | key | Blobs: session snapshots, the instance lock |

Indexes mirror those constraints on every backend. Schema upgrades run in place on start — there is no
migration step to run by hand.

## Security model

| Layer | Mechanism |
|---|---|
| Access | Every handler sits behind the `ALLOWED_USERS` gate; the bot exits if the list is empty. A `group=-1` gate handler runs before every other handler: banned users are dropped, and with `FORCE_SUB` set, non-subscribers get a join card (membership checked via `get_chat_member`, cached 5 min; admins and `DEV_ID` exempt; a misconfigured channel fails open with an hourly warning) |
| Admin controls | `ADMINS` / `DEV_ID` unlock `/admin` and the app's Admin tab; a ban is one flag on the user row — enforced in the chat gate and in the API auth step (30 s worst-case cache) |
| Mini App auth | Every API request carries Telegram's `initData`; `webapi.check_init_data` verifies the HMAC-SHA256 signature constant-time with `BOT_TOKEN` and requires `auth_date` fresher than 24 h — no cookies, no sessions |
| Rate limiting | `webapi.rate_ok`: a per-user token bucket (`WEB_RATE`/min, default 90) in front of every route — `429` on burst |
| Web hardening | Strict CSP (own scripts + `telegram.org` only, `object-src 'none'`, `frame-ancestors` limited to `web.telegram.org`), `nosniff`, `Referrer-Policy: no-referrer`; static serving is a fixed whitelist |
| Credentials at rest | Fernet (`SECRET_KEY`) encrypts passwords/keys **before** they reach any database |
| SSH | Host keys pinned on first use; changed keys are rejected |
| URL fetches | SSRF guard: private/reserved addresses refused, the host is resolved **once** and the connection is pinned to that address (no DNS-rebinding TOCTOU), redirects re-validated hop by hop, `MAX_FETCH_MB` enforced while streaming |
| FTP | Paths are refused if they contain line breaks (command injection); `exists()` falls back to a listing when the server lacks `SIZE`, so `skip`/`rename` never silently degrade into `overwrite` |
| Path handling | Folder templates and caption `> dest` go through the same segment-wise sanitiser: no absolute paths, no `../`, no CRLF |
| Destructive actions | `/rm`, `/del`, reboot, service/docker control, `/upgrade`, big `/get` — all confirm first |
| Audit | Every action lands in the audit table (`/audit`) |
| Runtime | Docker image runs as non-root `bot` user with a HEALTHCHECK; the systemd unit ships hardening flags (`ProtectSystem=strict`, `NoNewPrivileges`, …) |

Reporting and operational guidance: [SECURITY.md](../SECURITY.md).

## Testing strategy

`tests/` is a pytest suite of self-contained `check_*.py` scripts, each run in its own process because
`store.py` reads `DATABASE_URL` from the environment at import.

* **Real servers, not mocks, for protocols**: the suite starts a local FTP server and a local SSH/SFTP
  server and transfers real bytes.
* **Contract test** (`check_contract.py`): every storage backend must satisfy the same interface contract —
  the reason all four databases behave identically.
* **Security regression suite** (`check_security.py`, 24 checks): covers every fixed vulnerability class
  (SSRF pinning, FTP injection, path traversal, duplicate-policy degradation, env-parse crashes, queue
  integrity) so none of them come back.
* **Admin panel suite** (`check_admin.py`, 40 checks): the users store, admin routes, ban enforcement on
  the API, the rate limiter, the FORCE_SUB join gate and the logo/credit assets.
* **In-memory databases** by default; `TEST_DATABASE_URL=…` points the suite at a real database — that is
  exactly what CI does with PostgreSQL 16 on every push, across Python 3.10/3.11/3.12.
* **Static analysis**: ruff (`make lint`), and CI refuses code that fails either layer.

CI additionally builds the Docker image with buildx on every PR and pushes it to GHCR on `main`.
