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
- [Subscriptions & VPS hosting](#subscriptions--vps-hosting)
- [Payment gateways](#payment-gateways)
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
| `WEB_RATE` | `90` | Mini App API requests per minute per user (token bucket). A runaway client or a replayed `initData` gets `429` |
| `ADMINS` | unset | Comma-separated Telegram ids with access to the admin panel (`/admin` and the app's **Admin** tab) |
| `DEV_ID` | unset | The developer's Telegram id: an admin automatically, and exempt from `FORCE_SUB`. Shown with `DEV_CHANNEL` in the credits |
| `DEV_CHANNEL` | unset | Your channel username — shown as the developer credit in `/about`, `/start` and the Mini App footer |
| `FORCE_SUB` | unset | Channel (`@name`, a `t.me/` link or a `-100…` id) users must join before they can use the bot — in chat **and** in the Mini App. Add the bot as **admin** in the channel so it can check membership; admins and `DEV_ID` are exempt |
| `KEEP_ALIVE` | unset | `1` pings a URL every `KEEP_ALIVE_INTERVAL` seconds so free web services (Render) don't sleep. Set `0` on paid plans |
| `KEEP_ALIVE_URL` | `RENDER_EXTERNAL_URL` | The URL to ping; defaults to the one Render injects |
| `KEEP_ALIVE_INTERVAL` | `600` | Seconds between pings |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SESSION_DIR` | `.` | Where the Telegram session file lives. Docker uses `/data` (the volume) |

## Monitoring

Applies to servers with `/monitor on` (SSH). The loop runs every `MONITOR_INTERVAL` seconds and alerts when:
the server is unreachable, disk usage ≥ `DISK_ALERT` %, memory ≥ `MEM_ALERT` %, or load > 2 × cores.
State is per server: an alert fires once when it appears and once more when it clears, not every cycle.
The same loop sweeps expired VPS instances (see below): they are removed from the host node and
the buyer is notified.

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `MONITOR_INTERVAL` | `300` | ≥ 30 | Seconds between checks |
| `DISK_ALERT` | `90` | 1–100 | Disk % that triggers an alert |
| `MEM_ALERT` | `90` | 1–100 | Memory % that triggers an alert |

## Subscriptions & VPS hosting

`/plans` sells tiers — including plans with real VPS instances buyers can host their own bots on.
Everything is optional: with none of these set, users are simply on the Free plan (capped at 3 saved
servers) and `/plans` shows the catalog without a way to pay.

The flow: `/plans` → **Buy** (an order opens) → the buyer pays → the order activates. With a **payment
gateway** configured the payment verifies itself (below); otherwise the classic assisted flow applies:
`/paid <reference>` → you (or any `ADMINS`) get Approve/Decline buttons → approval activates the plan,
extends renewals from the current expiry, provisions the plan's VPS instances and delivers credentials
to the buyer. Both flows coexist — the gateway buttons appear next to the reference box.

| Variable | Default | Meaning |
|---|---|---|
| `SUBSCRIPTION_PLANS` | built-in catalog | JSON array replacing the whole catalog. Fields: `key`, `name`, `price`, `days`, `max_servers` (`-1` = unlimited), `vps_slots`, `vps_cpu`, `vps_ram` (MB), `vps_disk` (GB), `tag`, `desc`, `order`. The `free` key is always present (it is what users fall back to). Bad JSON logs a warning and keeps the defaults |
| `SUBSCRIPTION_CURRENCY` | `USD` | Display currency for prices and orders (prices are stored in whole cents) |
| `PAYMENT_INFO` | generic text | Checkout instructions; supports `{amount}`, `{currency}`, `{plan}`, `{days}`. Put your UPI / PayPal / crypto / Stars links here |
| `VPS_NODE_HOST` | unset | **The host node** — the machine included VPSes are created on. Without it, VPS plans still activate but instances wait (the admin panel shows the warning) |
| `VPS_NODE_PORT` | `22` | SSH port of the host node |
| `VPS_NODE_USER` | `root` | Node login; anything other than `root` gets `sudo -n ` in front of every command (needs passwordless sudo) |
| `VPS_NODE_PASS` | unset | Node password — or use a key instead |
| `VPS_NODE_KEY` | unset | Path to a private key file for the node (read at provision time) |
| `VPS_PUBLIC_HOST` | `VPS_NODE_HOST` | The address buyers connect to — set it when the node sits behind NAT / a proxy |
| `VPS_SSH_PORT` | `22` | The SSH port of the created instances (what buyers use) |
| `VPS_PROVISION_CMD` | `useradd …` | Provisioning template. Placeholders: `{user}`, `{pass}`, `{cpu}`, `{ram}`, `{disk}`. Default creates a Linux user with a password and a `setquota` disk quota (best effort) |
| `VPS_DEPROVISION_CMD` | `userdel -rf {user}` | Removal template, run when a subscription lapses or an admin revokes |

Security notes: the node is **your** infrastructure, configured by env vars — never a user-supplied
address. Instance usernames (lowercase alnum, letter-first, ≤ 16 chars) and passwords (16 chars,
letters+digits ≈ 95 bits) are alnum-only, so nothing a buyer controls can inject into the provisioning
shell command. Passwords are Fernet-encrypted at rest (`SECRET_KEY`), shown only to their owner
(`/myvps` and the app's **Hosting** tab). Override the two templates to provision LXC containers,
Docker sandboxes or Proxmox CTs instead of system users.

Default catalog (all replaceable via `SUBSCRIPTION_PLANS`):

| Key | Name | Price | Servers | VPS |
|---|---|---|---|---|
| `free` | Free | — | 3 | — |
| `pro` | PRO | 2.50 / 30 d | 15 | — |
| `vps1` | VPS Starter | 5.00 / 30 d | 25 | 1 × (1 vCPU / 1 GB / 15 GB) |
| `vps2` | VPS Pro | 10.00 / 30 d | 50 | 2 × (2 vCPU / 2 GB / 40 GB) |

> Telegram Stars invoicing needs a Bot-API layer newer than this bot's MTProto stack (pyrogram),
> so payments use the gateways below plus the assisted flow. Every order, decision and provisioning
> event is in the audit log and the admin panel (`/admin orders`, revenue in the overview).

## Payment gateways

Set any combination — each configured gateway becomes a button under **Buy** (chat and Mini App alike).
All of them need `WEBAPP_URL` (the bot's public HTTPS address) for their webhooks to reach you.

| Variable | Default | Meaning |
|---|---|---|
| `UPI_VPA` | unset | **Direct UPI** — your UPI id (`name@bank`). The buyer gets a `upi://pay` QR + one-tap app button; confirmation stays manual (`/paid <UTR>`) because a personal UPI id has no bank feed |
| `UPI_NAME` | `vpsbot` | The payee name shown in the UPI app |
| `YOUPI_URL` | unset | **YouPI-PG** ([self-hosted UPI gateway](https://github.com/youpi-pg/YouPI-PG)) base URL, e.g. `https://pay.yourdomain.com`. The buyer gets a checkout session (dynamic QR + GPay/PhonePe/Paytm/CRED deep links + WhatsApp OTP, whatever your instance is set up for) and pays your merchant VPA — **INR prices only** (`SUBSCRIPTION_CURRENCY=INR`) |
| `YOUPI_MERCHANT_ID` | default merchant | The merchant id from your YouPI panel (`X-Merchant-Id` header) |
| `YOUPI_API_KEY` | unset | Your YouPI merchant API key (`X-Merchant-Api-Key` header) |
| `YOUPI_WEBHOOK_SECRET` | unset | The shared secret YouPI signs its webhooks with (`X-Gateway-Signature: sha256=…`, HMAC-SHA256 of the raw body). Point your YouPI merchant webhook at `{WEBAPP_URL}/pay/youpi/cb` — a verified `SUCCESS` with a matching amount **auto-activates the order**; `FAILED`/`EXPIRED`/`CANCELLED` tell the buyer nothing was charged |
| `CRYPTAPI_<COIN>_ADDR` | unset | **CryptAPI** ([cryptapi.io](https://cryptapi.io), no signup, fees 1→0.25 %, paid out to **your own wallets**). One variable per coin: `CRYPTAPI_BTC_ADDR=bc1…`, `CRYPTAPI_TRC20_USDT_ADDR=T…` (tokens are `<CHAIN>_<TOKEN>` → `trc20/usdt`). Each purchase mints a payment address with a per-order nonce; the callback (signed `X-CA-Signature`, RSA-SHA256 over the full request URL, verified against CryptAPI's `pubkey` endpoint — the same scheme their official plugins use) **auto-activates the order** when the confirmed total reaches the expected amount |
| `CRYPTAPI_TOLERANCE` | `1` | Percent of underpayment still accepted (0 = exact, max 10). Partial payments accumulate per transaction id — the buyer can pay in several sends |
| `CRYPTAPI_CONFIRMATIONS` | `1` | Blockchain confirmations before activation (pending transactions notify the buyer first) |
| `CRYPTAPI_API_KEY` | unset | BlockBee pro key — switches the API base to `api.blockbee.io` (optional) |
| `CRYPTAPI_BASE` | `api.cryptapi.io` | Override the API base (rarely needed) |

Setup notes:

- **CryptAPI requires a verified domain**: in their control panel verify the `WEBAPP_URL` domain once
  (DNS TXT record or an HTTPS file) — payment addresses are only created for verified callback domains.
- **YouPI requires a reachable webhook**: your instance runs somewhere that can POST to
  `{WEBAPP_URL}/pay/youpi/cb`; HTTPS everywhere.
- The webhooks are verified first (signature), then the **amount must match the order** (crypto: the
  per-uuid confirmed total within `CRYPTAPI_TOLERANCE` %), then the **per-order nonce** (crypto), and
  approvals are **idempotent** — replayed, retried or duplicated callbacks can never activate twice.
- The buyer's chat gets: payment seen (pending) → confirmed + plan active + VPS credentials; the owner
  gets an audit line and a receipt. A missed YouPI webhook even self-heals: the Mini App polls the
  gateway while the checkout is open.

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

### Free-plan server cap

Without a subscription every user is on the Free plan: **3 saved servers**. `/add` (and the Mini App)
refuse the 4th with an upgrade card. Paid tiers lift the cap; `max_servers: -1` in `SUBSCRIPTION_PLANS`
means unlimited.

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
