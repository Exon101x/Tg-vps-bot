# Changelog

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
* **Logo & brand assets**: `assets/logo.png` (1024² robot mascot, also shipped as the lightweight
  `webapp/logo.png` app icon + favicon) and `assets/banner.png` (wide hero). The Mini App header
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
