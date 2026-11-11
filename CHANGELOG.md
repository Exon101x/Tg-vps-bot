# Changelog

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
