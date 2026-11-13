# Changelog

## 1.9.3 — from a bare VPS to https://bot.example.com

* **New DEPLOY.md section**: the full from-scratch chain for the bot's public HTTPS
  address — the thing `WEBAPP_URL` needs and the split setup pastes into Netlify as
  `BOT_API_URL`. Domain choice, the DNS A record (+ `dig` check), firewall (ufw and
  cloud security groups), a Caddy TLS proxy (two lines) with an nginx+certbot
  alternative, the `.env` wiring (`HEALTH_PORT` + `HEALTH_BIND` + `WEBAPP_URL`),
  Docker's loopback port publishing, and curl verification. The existing "HTTPS for
  the Mini App" section stays as the quick recipe and links to it; `.env.example`'s
  `BOT_API_URL` block points there too.
* **docker-compose.yml**: a commented `ports:` block (`127.0.0.1:8080:8080`) plus a
  note that the bot's HTTP server only starts with `HEALTH_PORT`/`PORT` set — Docker
  users previously had no documented path to a public address at all.
* check_infra: new checks pin the section's key facts (loopback-only publishing,
  `HEALTH_PORT` gate, TOC anchor, Caddy/nginx snippets).

## 1.9.2 — .env.example: the complete A-to-Z template

* **Rewritten `.env.example`**: every environment variable the bot reads (55+), grouped
  into eleven sections — required, database, admin & community, web/Mini App, HTTP &
  health, VPS hosting, subscriptions & payments, session & single-instance, logging,
  transfer tuning, and the static-host block for the split Netlify setup (`BOT_API_URL`,
  with a note that the bot itself never reads it at runtime). Previously undocumented
  vars (`CRYPTAPI_BASE`, `MONGO_DB`, `BOT_DB`, `KEEP_ALIVE_INTERVAL`, `ALLOWED_ORIGINS`,
  `HEALTH_BIND`/`HEALTH_PORT`/`PORT`, `DB_CONNECT_RETRIES`) now have a home with their
  type, default and range.
* **Drift-proof test (group 20)**: an AST scan collects every `os.getenv`/`os.environ`
  literal in the runtime and tooling code and asserts it appears in the template — a new
  variable without a template entry fails CI. It caught `CRYPTAPI_BASE` on its first run.
  The same test runs both real `.env` parsers (`deploy.py`, `setup_gateway.py`) over the
  template and pins the five required keys as present-and-empty.
* Verified end to end: a filled copy of the template passes `python deploy.py --check`;
  93 webapp checks, suite 19 passed / 2 skipped, ruff + links clean.

## 1.9.1 — Netlify: one env var, deploy on every push

* **`netlify.toml` in the repo**: a Netlify site connected to the GitHub repo now builds and
  publishes `webapp-dist/` on every push — no local build, no drag and drop. Set
  `BOT_API_URL` (the bot's public HTTPS address) once in Site settings → Environment
  variables; that is the only site-level setting. The build script reads `BOT_API_URL` as
  its default base locally too (the CLI argument still wins).
* **A guard for the stateless trap**: on Netlify build machines a missing `BOT_API_URL` —
  or one pointing at the Netlify site itself — fails the deploy with the fix in the
  message, instead of shipping an app whose every API call hits the static host and dies
  on the `/index.html` fallback. Local builds are unaffected.
* Docs: DEPLOY.md spells out the Netlify flow (drag-and-drop and repo-connected) and why
  the whole bot can't be serverless (long-running Pyrogram session, SQLite on disk,
  minutes-long SSH jobs, timers). Tests 19h–19l cover the guard, the env-var default and
  the `netlify.toml` contents.

## 1.9.0 — host the Mini App anywhere, keep the bot on your VPS

* **Split hosting, natively supported.** The Mini App's four static files can now live on
  any static host (Cloudflare Pages, Netlify, Vercel, your own nginx) while the bot and its
  API stay on the VPS. `python scripts/build_webapp.py https://your-vps` writes a ready-to-
  drop `webapp-dist/` — asset paths rewritten, the API base injected, cache + CSP headers
  and a platform-by-platform README inside. The bot gains CORS: `OPTIONS` preflights and
  `Access-Control-Allow-*` headers on every API answer, gated by the new `ALLOWED_ORIGINS`
  env var (comma list; `WEBAPP_URL` is always allowed). API auth is unchanged — Telegram's
  initData signature is verified whatever origin the app is opened from, so allowing an
  origin grants no rights the app doesn't already have.
* The frontend's fetches now go through one `API_BASE` (same-origin by default — the
  bot-served app behaves exactly as before; the build injects the bot's address when the
  app is hosted elsewhere). `webapp/` sources stay untouched; only the build output differs.
* `setup_gateway.py` can edit and validate `ALLOWED_ORIGINS` alongside `WEBAPP_URL`.
* Tests: 17 new checks in `tests/check_webapp.py` — the CORS preflight dance (refusal,
  allowlist, upload-chunk headers, error responses stay CORS-labelled, WEBAPP_URL and
  trailing-slash normalisation) and the build output (injection, CSP, https-only guard,
  same-origin variant, source untouched). Docs: DEPLOY.md gets a "Hosting the Mini App
  elsewhere" section with the three-step recipe; CONFIG.md documents `ALLOWED_ORIGINS`.

## 1.8.9 — commands and buttons: no more dead ends

* **Unknown commands get an answer, not silence.** Every unrecognized `/command` now
  replies with a short card: up to three "did you mean …?" suggestions (prefix match
  first, then close matches — `server` → `/servers`, `strats` → `/stats`), plus
  buttons for the command hub and the server list. Case-insensitive, `@botname`
  suffixes ignored, and each unknown word is answered once per hour per user so a
  stuck client cannot spam the chat.
* **`/use` without a name is now useful**: it shows the same tap-to-switch buttons
  as `/servers` instead of "No such server". Tapping a server button on any card
  refreshes that card's ▶️ marker in place, so the list always shows what is active.
* **Every button answers.** A tap on a button the bot no longer knows (old messages
  after an update, non-admins tapping admin buttons, stale cards after a restart)
  now gets an explaining toast instead of an endless spinner — the callback handler
  has a fallback answer at the end of its chain.
* **New guessable aliases**: `/download` → `/get`, `/delete` and `/remove` → `/del`,
  `/commands` → `/help` (the thing people type before they know about `/help`).
* Tests: 11 new checks in `tests/check_misc.py` (suggestions, the unknown-command
  card, the switch keyboard), full suite 19 passed / 2 skipped. Docs:
  COMMANDS.md rows updated for the new aliases and behaviors.

## 1.8.8 — DEPLOY.md rebuilt as the complete from-scratch deployment guide

* **A real walkthrough, in order.** The old page was organized by platform; the new one
  walks a first-time deployer through every step in order: what to gather → create the bot
  with @BotFather (including `/setuserpic` etc. and getting your numeric id) → the
  my.telegram.org `API_ID`/`API_HASH` step with a do-not-commit warning → a host-picking
  table with the SQLite-vs-external-DB decision rule → the four deploy paths (wizard,
  Render blueprint, Docker/compose/ghcr, hardened systemd) → a five-check verification
  checklist (`Started.` log line, `/health`, `/start`, `/add` + file round-trip, `/vps`)
  → what to do in the first hour (back up `SECRET_KEY`, nightly DB dump, optional env
  vars for admins/Mini App/payments/VPS node).
* **Troubleshooting rebuilt.** "The 8 classics" in narrative form (unhealthy deploy, the
  lock wait, `AUTH_KEY_DUPLICATED`, free-plan sleep, `SECRET_KEY` loss, DB not ready,
  wiped-disk data loss, bad API pair) then an expanded symptom table — new rows cover
  `/add` timeouts, FTP-vs-SFTP port blocks, uploads stuck in `queued`, the 2 GB
  non-Premium wall, Mini App blank/sign-in failures, payment webhooks not landing,
  `/admin` access, and "nothing in the logs at all". Plus a log-reading cheat sheet and
  a four-step last-resort ladder ending in `deploy.py --check --verify` and
  `RESET_SESSION=1`.
* **Tips and tricks section**: forcing speed profiles and which knob helps on
  high-latency links, free-tier survival, multi-user/admin setup, `setup_gateway.py`
  for real payments, the `/multi` over-2 GB streaming join, `/monitor on` alerts,
  disk hygiene, a 5-minute host-migration recipe (same `SECRET_KEY` + `DATABASE_URL`
  → session and queue restore), and pinning the CI image by commit SHA.
* **Link hygiene, enforced.** New `scripts/check_links.py` validates every intra-repo
  markdown link (file + heading anchor, GitHub slug rules). It immediately caught a
  pre-existing broken anchor: `ARCHITECTURE.md#5-the-mini-app-webapi` — GitHub's real
  slug for the backticked heading is `#5-the-mini-app-webapipy`; fixed in CONFIG.md
  and DEPLOY.md. All four external anchors into DEPLOY.md (`#render`,
  `#systemd-bare-vps-raspberry-pi`, `#other-container-platforms`,
  `#https-for-the-mini-app`) are preserved, so no other page needed changes.
* README's doc index and quick start now point at the guide.

## 1.8.7 — chat UI redesign: every card aligned, every keyboard a grid

* **A shared formatting kit.** Four small helpers now shape everything the
  bot prints: `kv()` renders aligned two-column rows (`label ······ value` —
  Telegram's proportional font eats space padding, dots survive it; over-long
  rows fall back to `label: value` instead of squeezing),
  `kvrows()` stacks them, `head()` makes the one-line card headers with a
  muted sub-line, `progress_line()` is the one-line bar. `/status`, `/about`,
  `/usage`, the admin overview, `/stats` and the plan/VPS cards all use it —
  the bot finally looks like one product instead of five writing styles.
* **The /start card and the manual read like a product page.** Bold section
  headers, aligned feature rows, numbered quick start; the manual pages
  (`/help` → section buttons) group commands under bold sub-headings instead
  of one undifferentiated wall; the hub lists one line per section. Section
  bodies now go out as HTML (parse mode travels with `help_page`'s return,
  so the plain-text hub and `/help all` are untouched).
* **Keyboards became grids.** `/servers` and `/plans` buttons pair up
  two per row (the active server's button is marked `▶️`), the VPS dashboard
  is a clean 2×5 grid, the admin panel is 2×2. One server or one paid plan
  still gets a full-width row.
* **Every list card was rebuilt.** `/servers`: bold names with the
  scheme://user@host:port in monospace under each; `/queue`: state icon,
  bold job id, name → server, errors in italics; admin users/orders:
  two lines per entry (identity bold, details indented); the plan approval
  delivery to buyers (`_fulfill`) and `/add`'s success card use the same
  card shape; `/folder`'s help shows Set/Clear/Variables/Examples as
  grouped lines.
* **Typography consistency**: em dashes everywhere (no more mixed `-` and
  `—`), `·` as the intra-line separator, and the hosting guide re-indented
  to a hanging-number layout.
* **Pinned in `tests/check_misc.py`**: `help_page` now returns its parse
  mode (the page carries `<b>` headers and must be sent as HTML); the kit's
  fallback rule and the welcome card's dotted alignment have their own
  checks.

## 1.8.6 — documentation catch-up: the docs match the code again

* **Every doc refreshed against the code after the 1.8.5 state-machine work.** The
  docs had drifted: the module maps missed five modules that had grown in
  (`persist.py`, `subscriptions.py`, `hosting.py`, `payments.py`, `util.py`),
  the schema table still said "five tables" when the database creates nine, the
  test-suite counts were two scripts and a hundred checks behind, and
  COMMANDS.md still claimed the `/ask` picker "does not survive a restart" —
  the exact thing 1.8.5 made it do.
* **ARCHITECTURE.md gained a sixth data flow** describing the state machines:
  which `State` holds what, its TTL and who writes it (confirmations, pending
  passwords, `/multi` collections, `/ask` pickers, terminal mode, monitor
  issues, job-stop intents, Mini App upload sessions), how the debounce, boot
  re-hydration, TTL sweep and cancel drain interact, and the plain-JSON rules
  that keep a blob reloadable forever. The dependency graph, the design goals
  and the testing-strategy section now include `persist.py` and its suite.
* **The database schema section now documents all nine tables** — the four
  commerce tables (`users`, `orders`, `subs`, `allocs`) that had never been
  listed, plus what the `kv` table really holds (session snapshots, the lock
  *and* the state machines).
* **Fixed `tests/check_state.py` crashing when run standalone** on a fresh
  machine: the upload-session check wrote a staged `.part` file without
  creating the staging directory first (in CI, `check_webapp.py` had happened
  to create it earlier in the run). It now creates the directory exactly the
  way the real `up/init` endpoint does.
* **README's repository layout, feature table and FAQ caught up**: a
  "Restart-proof flows" row, the mid-upload restart answer now covers mid-chat
  flows and the restart-time stop, the test counts read 18 scripts / 26
  security + 19 big-file + 68 mini-app + 40 admin + 26 state checks, and the
  security blurb notes that a password mid-prompt in `/add` is encrypted at
  rest like everything else. SECURITY.md's credentials-at-rest row and
  CONTRIBUTING.md's project-layout and testing notes say the same thing, so
  the three documents cannot drift apart silently again.

## 1.8.5 — implicit state machines made explicit: every mid-chat flow survives a restart

* **Seven flows that used to live only in RAM are persisted now.** Confirmations
  (`ok:`/`no:` buttons), pending server passwords/keys from `/add`, `/multi`
  collections, `/ask` server pickers, terminal mode, the monitor's issue state
  and job-stop intents each used to die on every restart — a deploy mid-flow
  silently dropped the state and the flow continued as if nothing happened (the
  worst case: a `/term` user's next plain message ran as a shell command after a
  restart, and a stop pressed during a restart was lost, so the job kept
  running). New `persist.py` keeps each machine as an encrypted kv blob with its
  own TTL, re-hydrates on boot and sweeps expired entries from the monitor
  loop, so the bot resumes mid-chat flows instead of forgetting them.
* **Confirm buttons carry names, not closures.** `confirm()` used to capture the
  whole action — command text, server names, user ids — inside a Python closure
  in a dict: unreloadable, and credentials sat in RAM after the button was
  forgotten. The pending token now stores `{uid, ts, act, args}` with `act`
  resolved through a seven-entry `ACTIONS` registry (`sh`, `reboot`, `run`,
  `ban`, `broadcast`, `del`, `get`); every target re-reads its server/user from
  the database, so a button pressed after a restart still works, and a token
  from an older bot version degrades to "run the command again" instead of
  crashing.
* **Stop intents are honoured across restarts.** `Engine.start()` drains the
  persisted cancel set first: any job that was queued or running when the old
  copy died is marked cancelled ("stopped: the bot restarted") instead of
  resuming against the user's wishes. Jobs that finished meanwhile are left
  alone, and the `/ondup skip` note is now written and read through a shared
  `SKIP_MARK` constant instead of a magic string.
* **Mini App chunked uploads resume after a restart.** The `UPLOADS` token map
  is a `State` now, so a phone upload in progress when the bot restarted no
  longer fails with "unknown upload token"; tokens whose staged `.part` file no
  longer exists (temp dir wiped, new machine) are pruned instead of silently
  restarting the byte stream from zero.
* **The monitor no longer re-alerts known problems after a restart.** Issue
  state is persisted per `(uid, server)`, and the new/cleared diff is computed
  by a pure `diff_issues()` — extracted so it is tested directly. A server that
  was already reported as down before a deploy is not re-alerted when the bot
  comes back.
* **Pickers use string message ids and a sweepable basket.** `/ask` picker
  entries are keyed by the picker message id (JSON round-trips them as strings
  — the producer and the `pick:`/`pickc:` handlers now agree), and each user's
  basket carries its own `ts`, so the State's TTL sweep drops idle baskets
  whole. `/multi`'s live status message is kept as `(chat_id, message_id)`
  instead of a Pyrogram object, making collections fully JSON-reloadable.
* **`tests/check_state.py`** pins all of it: 26 checks over restart simulation
  (fresh `State` over the same row), expiry sweeps, the ACTIONS registry, the
  cancel drain and the upload prune.

## 1.8.4 — chat UI: a welcome card, a button-driven manual - and the follow-up crash fixed

* **Fixed the second `/start` crash** (reported minutes after 1.8.3): `reply_long`
  sent its follow-up chunks with `msg.chat.send_message(...)`, but Pyrogram `Chat`
  objects have no such method — that is Telethon's API — so any text long enough to
  split still died, now with `AttributeError`. Follow-ups now go through the public
  API `Message.reply(part, quote=False)`: the first chunk carries the keyboard and
  any other options, the rest are plain follow-up messages.
* **`/start` is a welcome card now**, not a wall of text: a short hello (by name,
  plus the active server when you have one), a 3-step quick start, and buttons —
  Commands, Plans, My VPS, My plan, and the Mini App (a real web-app button when
  `WEBAPP_URL` is live, a how-to-enable page otherwise).
* **`/help` is a hub now**: the manual is seven sections (Servers, Files, VPS,
  Plans, Mini App, Admin, About) — one line per section and a button each; pages
  open in place with back-to-hub and back-to-start buttons, the Admin section
  appears for admins only, and `📄 All in one message` (or `/help all`) still
  sends the whole manual as messages. Every page fits a single message by
  construction — the whole manual is 3983 UTF-16 units, under the cap again.
* **Tests**: `tests/check_misc.py` grows 13 checks — `reply_long` now exercised
  through the real `Message.reply` path (chunks in order, keyboard on the first,
  `quote=False` follow-ups), and the new UI: section pages under the cap, every
  Telegram-menu command has a place in the manual, hub buttons match the sections
  exactly, admin gating for hub and pages, unknown-section fallback, start-card
  buttons.

## 1.8.3 — /start crashed: any text over Telegram's message cap now splits safely

* **Fixed the `MESSAGE_TOO_LONG` crash on `/start`** (reported live from a fresh VPS
  deploy): `HELP` had grown to 4101 UTF-16 code units — over Telegram's hard cap of
  4096 (which counts UTF-16 units, so emojis cost 2) — and one long feature batch
  pushed it over. New `split_text`/`tlen`/`reply_long` in `bot.py`: any reply routed
  through `reply_long` arrives as consecutive messages, each safely under the cap,
  cut at line boundaries where possible, never splitting an emoji or dropping a word.
* **Every growable text now goes through it**: `/start`/`/help` (HELP), `/plans`
  (custom `SUBSCRIPTION_PLANS` catalogs can be arbitrarily long), `/mysub`, `/myvps`,
  the `pl:` plan screens and the hosting guide, and the admin panel (`/admin`
  overview, the 100-user list, orders) — the keyboard rides the first chunk, the
  rest follow into the chat. `/sh`-style command output keeps its existing
  document-fallback path.
* **Correct manual deployment steps**: the README's "Option B" told you to run
  `pip install -r requirements.txt` straight — on current Ubuntu/Debian the system
  pip is locked (PEP 668, externally managed) and refuses. The steps now create a
  venv first (`sudo apt install python3-venv`, `python3 -m venv myenv`),
  matching what a fresh VPS actually needs.
* **Tests**: `tests/check_misc.py` grows 11 checks — UTF-16 counting (emoji = 2
  units), line-boundary splitting with no word lost, no emoji cut in half,
  9000-char single-line hard cuts, the real formatted HELP splitting under the cap,
  and `reply_long` chunk ordering with a fake chat (first chunk carries the
  keyboard, the rest follow).

## 1.8.2 — gateway setup tool: one command, every gateway

* **`setup_gateway.py`** — the full payment-gateway setup, one stdlib-only command next to
  `deploy.py`. The guided wizard (`python3 setup_gateway.py`) walks through every method
  (UPI Gateway, direct UPI, CryptAPI crypto), validates each value as it is typed
  (UPI ids, https URLs, wallet address families per coin, numeric ranges), offers to
  generate the webhook signing secret, live-tests what it can without creating state
  (the gateway's merchant credentials via a harmless order lookup, the bot's webhook port
  via `/healthz`, CryptAPI's pubkey endpoint), prints the exact webhook URL to register in
  the gateway's dashboard and writes only the gateway keys to `.env` — comments, blanks and
  unrelated keys survive untouched, values update in place, secrets are masked in its own
  output. Runs before the bot exists: no database, no `SECRET_KEY`, no bot imports.
* **Non-interactive modes**: `--check` diagnoses a running setup (method-by-method
  ready/partial/off, missing keys, live probes, QR pipeline health; exit 1 when a
  half-configured gateway or unreachable endpoint needs attention), `--write KEY=V…`
  batch-writes validated values (scripted setups), `--gen-secret` mints a strong secret,
  `--qr` renders any text as a PNG, `--env PATH` targets another file, `--no-probe` skips
  the network. Validation is strict on purpose: typos and test values are rejected with a
  reason, `http://` is only accepted for localhost.
* **Tests**: `tests/check_setup.py` — 25 checks over a fake gateway that answers like the
  real one (401 for wrong merchant credentials, 404 for right ones, `/healthz`, `/pubkey/`):
  secret generation, batch writes (in-place updates, preservation, no duplicates, rejected
  batches write nothing), the checker on empty/partial/full setups, wrong credentials, dead
  gateways, QR rendering, and the wizard piped empty input.

## 1.8.1 — UPI integration moved to the author's own UPI Gateway

* **The auto-verified UPI provider is now [upi-gateway](https://github.com/Exon101x/upi-gateway)**
  (the author's self-hosted multi-merchant UPI payment gateway: dynamic QR codes, payment
  verification, account rotation, timestamped webhook signatures) — replacing the previous
  YouPI-PG integration, with the integration built against the gateway's real API:
  orders are created with `POST /api/v1/orders` (`X-Api-Key`/`X-Api-Secret`, unique
  `client_order_id` per bot order — a retried create reuses the gateway's existing order
  instead of failing) and the buyer gets the gateway's checkout page **plus** its
  `upi_intent` deep link rendered as a scannable QR with the amount pre-filled.
* **Config**: `UPIGW_URL` + `UPIGW_API_KEY` + `UPIGW_API_SECRET` + `UPIGW_WEBHOOK_SECRET`
  (from the gateway's Dashboard → API & Webhooks; `UPIGW_MAX_SKEW`, default 300 s, tunes the
  timestamp window) — all optional, independent, INR prices only. The old `YOUPI_*` variables
  are gone.
* **Webhooks exactly as the gateway sends them**: `POST {WEBAPP_URL}/pay/upigw/cb` with
  `X-Signature: t=<unix-seconds>,v1=<HMAC-SHA256 of t + "." + raw body>`. The timestamp must
  be fresh, the signature is compared constant-time, the amount must match the order to the
  paisa, and delivery semantics are honored precisely: **at-least-once** (every `event_id` is
  remembered — replays are ack'd, never re-applied), **out-of-order safe**
  (`event_sequence` keeps older events from rewinding newer state), and `order.paid` wins over
  any earlier state — mirroring the gateway's own transition rules, so a late payment inside
  its 24-hour recovery window still activates even after an `order.expired`.
  `order.under_review` notifies the buyer their UTR is being verified; `order.expired`
  notifies them nothing was charged; an amount mismatch alerts the admins instead of
  activating.
* **Self-healing kept**: if the webhook never lands, the Mini App's status poll
  (`GET /api/v1/orders/{id}`) sees the gateway's live `paid` state and activates from there —
  the response field is now the provider-neutral `gateway`.
* **Better gateway errors**: 4xx/5xx answers from either gateway are surfaced with their JSON
  reason (`{"message": …}`) instead of a raw `HTTPError`, and transport failures say
  "cannot reach the gateway".
* **Tests**: `tests/check_payments.py` now runs a miniature of the real gateway (Laravel-style
  `POST /api/v1/orders` + `GET /api/v1/orders/{id}`, `X-Api-Key`/`X-Api-Secret` auth, unique
  `client_order_id`) over real HTTP — covering order creation, duplicate-create reuse, signed
  `order.paid`/`order.under_review`/`order.expired` handling, replayed `event_id`s,
  out-of-order sequences, paid-after-expired, stale timestamps, forged signatures, amount
  mismatches, wrong merchant credentials, the Mini App pay routes and the lost-webhook
  self-heal — 41 checks in total, all over live sockets.

## 1.8.0 — Payment gateways: UPI and crypto, auto-confirming

* **Payment gateway system** (`payments.py`): three optional, independent ways to take money —
  all configured with env vars, all surfacing as buttons under `/plans` → **Buy** in chat and in
  the Mini App's Hosting tab, all coexisting with the classic `/paid <reference>` assisted flow.
* **UPI, auto-verified — YouPI-PG** (the self-hosted gateway from
  [github.com/youpi-pg/YouPI-PG](https://github.com/youpi-pg/YouPI-PG)): `YOUPI_URL` +
  `YOUPI_WEBHOOK_SECRET` create a checkout session (`POST /api/v1/payments`, `X-Merchant-Id`,
  `X-Merchant-Api-Key`, idempotency key per order) and hand the buyer a checkout link — dynamic
  QR, one-tap deep links into GPay/PhonePe/Paytm/CRED, WhatsApp OTP, whatever the owner's instance
  does. When YouPI's banking observers see the money, the signed webhook
  (`X-Gateway-Signature: sha256=<HMAC-SHA256 of the raw body>`, verified constant-time) lands on
  `{WEBAPP_URL}/pay/youpi/cb` and the order activates itself: plan live, VPS provisioned,
  credentials in the buyer's chat, receipt to the admins — zero clicks. Amounts are checked
  against the order to the paisa; a mismatch alerts the admins instead of activating. If the
  webhook never arrives, the Mini App's status poll self-heals the activation from the gateway's
  live session status. INR prices (`SUBSCRIPTION_CURRENCY=INR`).
* **UPI, direct**: `UPI_VPA` / `UPI_NAME` render a proper `upi://pay` intent link (pa/pn/am/cu/tr)
  plus a QR (generated locally, pure-python `qrcode` + `pypng`, no Pillow) — pay in any UPI app,
  then confirm with `/paid <UTR>` as before.
* **Crypto, auto-verified — CryptAPI** ([cryptapi.io](https://cryptapi.io), no signup, no KYC,
  100+ coins, payouts to the owner's **own wallets**): `CRYPTAPI_<COIN>_ADDR` (e.g.
  `CRYPTAPI_BTC_ADDR`, `CRYPTAPI_TRC20_USDT_ADDR`) adds a coin button. Each purchase mints a
  payment address with a per-order nonce; the price converts to the coin at the live rate
  (`/convert/`). Callbacks (`{WEBAPP_URL}/pay/cryptapi/cb`) are verified with `X-CA-Signature` —
  RSA-SHA256 over the exact request URL against CryptAPI's published pubkey, the scheme their
  official plugins use — then nonce-checked, accumulated per transaction uuid (partial payments
  count, replays don't), and the confirmed total within `CRYPTAPI_TOLERANCE` % (default 1)
  activates the order automatically. Pending transactions notify the buyer first; every ack is
  the literal `*ok*` their retry loop expects.
* **Verified end-to-end**: every webhook is signature-checked before anything happens, amounts
  must match, approvals are idempotent (replayed/retried callbacks can never activate twice) and
  everything lands in the audit log. 26 new checks in `tests/check_payments.py` cover the intent
  link, QR output, YouPI HMAC accept/reject/amount-mismatch/replay, CryptAPI RSA accept/reject/
  nonce/replay/partial payments, and the Mini App pay routes — run over real HTTP.
* **Chat UX**: the Buy button now opens a payment picker (gateway buttons + the manual
  reference path); YouPI checkouts edit in place with an open-checkout button; UPI/crypto send
  a QR photo with one-tap copy targets. The `buy:`/`pay:`/`pl:`/`ord:` callback kinds are now
  all wired (they silently did nothing before — a real bug), and `/plans`, `/mysub`, `/myvps`,
  `/paid`, `/hosting` are in the Telegram command menu.
* **Mini App**: the Hosting tab pay box grows payment-method buttons, a live payment card
  (QR, address/amount with copy chips, open-checkout / open-UPI-app buttons) and a 5-second
  status poll that refreshes into the plans view the moment the order activates.
* **Docs**: README (feature row + checkout table + security callout), `docs/CONFIG.md` (new
  *Payment gateways* section), `.env.example`, `/help`. Dependencies: `qrcode==8.2` +
  `pypng==0.20220715.0` (pure-python QR → PNG, no Pillow).

## 1.7.0 — Subscriptions: sell plans, VPSes and bot hosting

* **A full subscription system** (`subscriptions.py`): a plan catalog every owner can sell —
  Free (3 servers) · PRO · VPS Starter · VPS Pro by default, replaced entirely with
  `SUBSCRIPTION_PLANS` (JSON: keys, prices, periods, server caps, VPS specs). Bad JSON never
  takes the bot down; the Free plan is always there as the fallback.
* **Buying flow**: `/plans` shows the catalog with Buy buttons → the buyer pays however the
  owner accepts (`PAYMENT_INFO` carries the links, `{amount}`/`{currency}`/`{plan}`/`{days}`
  placeholders) → `/paid <reference>` attaches it to the open order → the admins get
  Approve/Decline buttons. One tap activates the plan instantly; renewals extend from the
  current expiry. Telegram Stars needs a newer Bot-API layer than the MTProto stack, so
  payments use this classic assisted flow — with a full audit trail.
* **VPS plans include real instances** (`hosting.py`): approved orders provision SSH users
  with disk quotas on the owner's host node (`VPS_NODE_HOST` and friends) and deliver the
  credentials to the buyer automatically. Usernames and passwords are alnum-only (no shell
  injection possible), Fernet-encrypted at rest, and shown only to their owner (`/myvps`).
  Override `VPS_PROVISION_CMD` / `VPS_DEPROVISION_CMD` for LXC, Docker or Proxmox. Expired
  subscriptions are swept by the monitor loop: instance removed, buyer notified, plan lapsed
  back to Free.
* **Host your own bots**: `/hosting` is the 60-second guide — buy a VPS, `/add` it back as a
  server, stream the bot's files from Telegram, run it with `/sh`, watch it with `/stats`.
  `/mysub` tracks limits and days left; plan server caps are enforced in `/add` and the app.
* **Mini App gets a Hosting tab**: plan cards with gradient prices and Buy buttons, the
  checkout box (paste the payment reference), order history with live status chips, and My
  VPS cards with credentials behind a show/hide toggle. The Admin tab gains subs/revenue/VPS
  cards and an order list with one-tap approve/decline — the same code path as the chat
  buttons.
* **Admin panel**: `/admin` now shows active subs per plan, revenue, open orders, VPS counts
  and the host-node status; `/admin orders` lists everything needing a decision.
* **Storage** (`backends.py`, `store.py`): three new tables — `orders`, `subs`, `allocs` — on
  every backend (SQLite, PostgreSQL, MySQL/MariaDB, MongoDB), covered by the contract test
  and carried along by `migrate.py`. Prices are stored in whole cents; VPS passwords only
  ever exist as Fernet ciphertext in a database.

## 1.6.1 — Mini App redesign: modern glass UI

* **A fresh, modern look for the whole Mini App** — the flat white 2023-style UI is gone. The app
  now ships a cohesive glass design: aurora gradient backdrop, frosted translucent header and
  tab dock, gradient accent buttons with glow shadows, rounded glass cards, and smooth
  slide/fade transitions. Everything still follows the Telegram client theme — dark and light
  are detected from `themeParams` (with a system-preference fallback) and live-switch with the
  client; the header color matches Telegram for a native feel.
* **New navigation**: a floating pill dock with SVG icons (Files, Servers, Console, Stats, Queue,
  Admin) — the active tab lights up with the brand gradient. Haptic feedback on tab switches,
  chips and toasts (`HapticFeedback`).
* **Files**: per-type gradient icon tiles (folder, PDF, archive, image, video, audio, code,
  document), circular icon action buttons (send / rename / delete), a breadcrumb-style path bar,
  and an upload card with an animated striped progress bar, live percentage and byte counter.
  Skeleton shimmer loaders replace “loading...” text everywhere; empty and error states got
  icons. Button taps inside a row no longer leak into the row action (previously tapping
  rename/delete could also trigger a Telegram send).
* **Servers**: scheme tiles for SSH/FTP, active + monitored badges inline, a compact settings
  sheet, and a hint card listing the supported target formats.
* **Console**: a real terminal window — traffic-light dots, the active server in the title bar,
  colored command lines and a `❯` prompt.
* **Stats**: bento cards with icon chips and usage meters for RAM and disk (green / amber / red
  thresholds), a full-width host card, and a scrollable segmented control for the inspector
  views (processes, ports, docker, disk, firewall, failed, logins, updates) with a gradient
  active pill.
* **Queue & Admin**: colored status chips with pulse dots, retry/stop icon buttons, gradient
  avatars for users, compact stat cards. Long values wrap at word boundaries (no more
  mid-letter breaks); the footer credit line now scrolls with the content.
* Zero new dependencies, zero API changes — same `webapi.py` contract, same CSP (self-hosted
  only: no CDNs, no web fonts), all 15 test scripts still pass.

## 1.6.0 — admin panel, force-subscribe, hardened API, logo & credits

* **Admin panel with the details** (`ADMINS` / `DEV_ID`): `/admin` in chat and a new **Admin** tab in
  the Mini App — live overview cards (users, banned, servers, monitored, uploads and bytes total /
  today / 7 days, queue states, staging files, uptime, database, force-sub status, top movers), the
  known-users list with name, username, id, uploads, volume and last-seen, per-user detail sheets
  (usage, their servers, their audit trail), **ban / unban** and **broadcast to everyone**. User rows
  are tracked automatically from chats, uploads and app sessions (a new `users` table on every
  backend: SQLite, PostgreSQL, MySQL, MongoDB — same contract, migrate.py carries it over).
* **Force-subscribe** (`FORCE_SUB`): set a channel and users must join it before using the bot — in
  chat they get a join card with a recheck button, in the Mini App a dedicated join screen with the
  channel link; buttons and callbacks are gated too. Membership answers are cached for 5 minutes;
  admins and `DEV_ID` are exempt. Add the bot as **admin** in the channel so it can check members —
  a misconfigured channel fails open with an hourly log warning instead of bricking the bot.
* **More security protection**: the Mini App API now **rate-limits every user** (`WEB_RATE`,
  default 90 requests/minute, token bucket → `429`), **enforces admin bans** at the auth step
  (403 in the app, dropped messages in chat), and ships **stricter headers**: CSP with
  `object-src 'none'`, `base-uri 'self'`, `frame-ancestors` limited to Telegram (the old CSP would
  have allowed any framer if `img-src` tricks were found; now only web.telegram.org may embed the
  app), plus `Referrer-Policy: no-referrer` on every response.
* **Ban switch**: one flag locks an account out of the chat and the app at once (30 s worst-case in
  the API cache, instant after the flip through the panel); admins cannot be banned through the
  panel; every ban/unban lands in the audit log.
* **Logo & brand assets**: `assets/logo.jpg` (1024² robot mascot, also shipped as the lightweight
  `webapp/logo.png` app icon + favicon) and `assets/banner.jpg` (wide hero). The Mini App header
  shows the logo.
* **Fork-friendly credits**: the GitHub source link and the developer credit (`DEV_CHANNEL`,
  `DEV_ID`) are served to the Mini App and shown in the footer of every tab, in `/start` and
  `/about` — please leave them in place when you fork.
* **New env vars**: `ADMINS`, `DEV_ID`, `DEV_CHANNEL`, `FORCE_SUB`, `WEB_RATE` (documented in
  `.env.example`, README and docs/CONFIG.md).
* New `tests/check_admin.py` — 40 checks (users store, admin routes, ban enforcement, rate
  limiter, force-sub gate, logo/credit assets) and the users-table contract added to
  `check_contract.py` for every backend. Suite is now 14 check scripts.

## 1.5.0 — the Mini App: a web UI for your servers, inside Telegram

* **`/app` opens a Mini App** served by the bot's own HTTP port (the old one-line health endpoint grew
  into a small router; `/health`, `/healthz` and any unknown path still answer `200 {"status":"ok"}`, so
  platform health checks are unaffected). Five tabs, mobile-first, themed to the user's Telegram palette:
  * **Files** — browse the active server, mkdir / rename / delete (with confirmations), send any file to
    your Telegram chat (big ones arrive as volumes like `/get`), fetch a URL on the server, and **upload
    files straight from your phone**: 4 MiB chunks, idempotent retries, resumable on flaky mobile links.
    A finished upload is queued with `src web:<staged file>` and streamed out by the existing queue —
    same retries, resume from `.part`, duplicate policies and folder templates as chat uploads. The
    staged file is deleted when the job ends, kept for `/retry` on failure, swept after a day otherwise;
    if a redeploy wiped it, the job fails with "upload the file again" instead of guessing.
  * **Servers** — add a server (connection tested, host key pinned, same formats as `/add`), switch,
    delete, and edit folder template / duplicate policy / monitoring.
  * **Console** — shell commands on the active SSH server with the working directory remembered and the
    same dangerous-command confirmation as `/sh`.
  * **Stats** — the `/stats` cards plus every `/vps` view (processes, ports, docker, disk, firewall,
    failed services, logins, updates).
  * **Queue** — the upload queue with retry/stop and the audit trail. Every web action lands in `/audit`
    with a `web:` prefix.
* **Passwordless sign-in**: the app sends the initData Telegram signed at open; the bot verifies the
  HMAC-SHA256 signature with `BOT_TOKEN` (constant-time, `auth_date` fresher than 24 h) and then applies
  the same `ALLOWED_USERS` gate as the chat. No cookies, no sessions, no stored secrets; unauthenticated
  requests get 401, allowed signature but wrong id gets 403.
* **New env vars**: `WEBAPP_URL` (the bot's public HTTPS address — enables the `/app` button and sets the
  chat menu button; Telegram refuses plain-HTTP web apps, so `http://` values are ignored) and
  `WEB_MAX_MB` (staging cap for app uploads, default 4096, 0 = unlimited). `.env.example`,
  CONFIG.md, DEPLOY.md (nginx + Caddy snippets) and ARCHITECTURE.md document them.
* **Docker**: the image now ships the `webapp/` assets (the old `COPY *.py` would have 404'd the app);
  a check guards this.
* New `tests/check_webapp.py` — 51 checks: the signature scheme (valid/tampered/stale/wrong token/no
  hash/renamed field), the HTTP router (health paths, static whitelist + traversal, CSP header, 401/403/
  404/405), the server and file APIs against a real local SSH server, the full chunked-upload path
  through the real queue engine (byte-for-byte verification server-side, idempotent chunk retries,
  skip-ahead refused, staged-file cleanup), and the lost-staging failure. Suite is now 13 check scripts.

## 1.4.0 — direct 2–4 GB uploads, per-file server picker

* **Files up to 4 GB now upload directly.** Premium accounts can send up to 4 GB to Telegram, and because
  the bot speaks MTProto (not the Bot API) it downloads those whole: forward or upload the file and it
  streams to any FTP/FTPS/SFTP server like any other file — same parallel windows, queue, retries and
  resume, no volumes and no temp disk. Files between 2 and 4 GB get a "streaming directly (Premium
  upload)" note in the queue message. A defensive guard refuses anything past Telegram's absolute 4 GB
  ceiling with split + `/multi` guidance instead of a confusing mid-transfer failure.
* **New `/ask on|off`**: when on, every file you send gets buttons under it to pick the destination
  server — forward a file, tap a server, done; no `/use` switching needed. The tapped server's folder
  template and `"> destination"` caption handling apply as usual. Pending choices expire after 30 minutes
  or on restart (the files stay in your chat, so just forward them again). With fewer than two servers
  saved the buttons don't appear. `/ask off` (the default) keeps the previous automatic behavior.
* The upload handler's caption parsing was extracted into `caption_dest()` and its queueing tail into
  `queue_upload()` — identical semantics (regression-tested: folder vs explicit path, CRLF, traversal,
  empty captions), now shared by the direct path and the picker.
* The README was redesigned: a banner and badge header, a features-at-a-glance matrix, GitHub
  callout notes (tip / note / important / warning), an optional-variables table grouped by area,
  the repository layout folded into an expandable section, and a tightened table of contents.
  Adds `assets/banner.svg`. Facts re-checked against the code (bot.py is now ~1450 lines; the
  suite is 12 check scripts with 24 security + 22 big-file checks).
* Help text, the bot command menu, README, COMMANDS.md and the FAQ were updated for the 4 GB tiers
  (direct ≤ 4 GB with Premium, volumes beyond) and the new `/ask` flow.
* New `tests/check_bigfile.py` (22 checks): the 4 GiB ceiling and its refusal message, BIGINT job-size
  round-trips across every 32-bit boundary (2^31, 2^32, 2^32+1, 4 GiB), ask-mode persistence, caption
  semantics, picker expiry bookkeeping and the 64-byte callback-data limit. `tests/check_contract.py`
  gained the same size round-trips so every storage backend (incl. PostgreSQL in CI) proves them.

## 1.3.2 — documentation overhaul

* The README was rewritten and organised: table of contents, feature highlights, quick start (wizard and
  manual), a commands-at-a-glance table, a configuration summary, the large-file guide (both directions),
  speed profiles, a deployment matrix, development/CI notes, security summary, limits, and an FAQ.
* New **`docs/COMMANDS.md`** — every command with syntax, arguments and examples, grouped by area
  (servers, transfer, filesystem, queue, VPS admin, shell, files over 2 GB), including the `/add` target
  formats and what each queue state icon means.
* New **`docs/CONFIG.md`** — every environment variable with type, default, allowed range and tuning
  notes, including previously undocumented ones (`MONGO_DB`, `BOT_DB`, `KEEP_ALIVE_INTERVAL`), plus the
  performance-profile table with its override variables.
* New **`docs/ARCHITECTURE.md`** — design goals, module map with dependency direction, the five main data
  flows (upload, `/get`, `/multi` join, session sync/lock), the database schema, the security-model table
  and the testing strategy.
* `docs/DEPLOY.md` was reorganised with a table of contents, a fixed glued heading, a `docker run` example
  for the CI-published GHCR image, a Cloud Run min-instances note, a pointer to keep the systemd unit's
  hardening flags, and two new troubleshooting rows (changed host key, unhealthy deploys).
* `SECURITY.md` was expanded: reporting guidance with what to include, the security-model table, a
  deployment hardening checklist, an incident-response table (leaked key / token / database), and a scope
  section stating that `ALLOWED_USERS` gates *who* and the server-side SSH user gates *what*.
* `CONTRIBUTING.md` was expanded: dev environment, code-style rules (tolerant env parsing, security tests
  as a must), testing structure with the real-FTP/SSH servers, the backend contract test, the
  `uv pip compile` check for version-conditional pins, a PR checklist and release notes.

## 1.3.1 — bug fixes, CI repair, hardening
* **CI fixed**: the docker job failed on every push and every dependabot PR because the Dockerfile ran
  `chown bot /data /app` before `/app` existed (`WORKDIR` came one line too late). `/app` is now created first,
  so builds and image pushes to ghcr.io succeed again. The workflow also gained job timeouts, read permissions
  for the test job, concurrency cancellation, and a fork-PR-safe build cache (`cache-to` only on pushes).
* `sources.py`: URL fetches now resolve the host **once** and connect to exactly the address that passed the
  SSRF check (pinned `http.client` connections; TLS still verifies the original hostname), closing the
  DNS-rebinding window between check and connect. Redirects are re-validated hop by hop, and 4xx/5xx answers
  again surface as `HTTPError` (retryable vs. permanent logic unchanged).
* `MAX_FETCH_MB` is now enforced while streaming when the server sends no `Content-Length` (previously the cap
  only worked for sized responses).
* `bot.py`: caption destinations (`> some/dir/`) go through the same segment-wise sanitising as folder templates —
  no more absolute paths, `../` traversal or CRLF smuggling into FTP commands. Private-key upload prompts expire
  after 300 s like the password prompt. The monitoring loop forgets state of deleted/unmonitored servers.
  Optional numeric env vars (`MONITOR_INTERVAL`, `DISK_ALERT`, …) are parsed tolerantly: a typo logs a warning and
  uses the default instead of crashing the bot at import.
* `remote.py`: FTP paths containing line breaks are refused (FTP command injection guard), and `exists()` falls
  back to a directory listing on servers without `SIZE`, so the `skip`/`rename` duplicate policies can no longer
  silently degrade into `overwrite`.
* Docker image gained a `HEALTHCHECK` (new `healthcheck.py`), and the health endpoint's bind address is
  configurable with `HEALTH_BIND` (default `0.0.0.0` for PaaS; use `127.0.0.1` on a VPS).
* `requirements*.txt` are fully pinned to the versions the CI matrix verified; the accidentally committed
  `.venv312/` was removed from the repository and `.gitignore` now covers `.venv*`.
* Pinned `SQLAlchemy==2.1.8` / `pymongo==4.19.0` broke Python 3.10 installs (both require 3.11+). They are now
  conditional pins: 3.11+ keeps them, 3.10 gets the last compatible `SQLAlchemy==2.0.57` / `pymongo==4.18.0`
  (the same versions the unpinned requirements had been resolving to on 3.10 all along).
* `session_sync.py`: `SESSION_SYNC_INTERVAL` / `LOCK_TTL` typos no longer crash the bot at import (same tolerant
  `env_int` parsing as the other numeric variables), and a failed session snapshot can no longer leak the temporary
  database connection it was copying to.
* `bot.py` `/get`: deleting the progress message mid-transfer no longer aborts a multi-volume download — status edits
  are best-effort, so remaining volumes are still sent.
* New regression suite `tests/check_security.py` (24 checks) covering all of the above.

## 1.3.0
* `deploy.py`: standard-library setup wizard (interactive or flags) that validates credentials, generates and protects
  `SECRET_KEY`, configures the database (bundled PostgreSQL/MongoDB with generated passwords, or an external URL), writes
  a mode-600 `.env` with backups, and prepares Docker, systemd, Render, other PaaS or a local virtualenv; `--check`,
  `--verify`, `--dry-run`, `--rotate-key`.

## 1.2.0
* `/multi` + `/done`: volumes of a file over 2 GB are joined on the fly into ONE file on any FTP/FTPS/SFTP server
  (resumable inside any volume, no temp disk, gaps/duplicates detected).
* Speed profiles (`PERFORMANCE=auto|light|balanced|turbo`, `/perf`): the host's CPU and RAM decide Telegram download
  parallelism, SFTP window, FTP block size and uploads per server.

## 1.1.0
* Large files: parallel windowed downloads from Telegram (length-checked), speed and ETA in progress messages,
  unlimited `/fetch` by default, `/get` sends files over Telegram's limit as `.001 .002 …` volumes (one staged at a
  time, shrunk to the free disk, confirmation above 5 volumes), `/get -v MB`, and `/join` to reassemble volumes on SSH
  servers, so files over 2 GB can move in both directions.

## 1.0.0
* File transfer to FTP / FTPS / SFTP servers: streamed straight from Telegram (no temp disk), persistent queue, retries
  with backoff, resume from `.part` files, folder templates, duplicate policies, `/fetch` from URLs.
* VPS management over SSH: stats, shell and terminal mode, services, logs, docker, updates, reboot, monitoring alerts,
  host-key pinning, audit log.
* Storage on SQLite, PostgreSQL, MySQL/MariaDB or MongoDB (`DATABASE_URL`), `migrate.py`, encrypted credentials.
* Hosting anywhere: Dockerfile, docker-compose, systemd unit, Render Blueprint, Heroku/Procfile, health endpoint,
  session snapshots in the database, single-instance lock for zero-downtime deploys, opt-in keep-alive.
