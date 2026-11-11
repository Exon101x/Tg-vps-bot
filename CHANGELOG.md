# Changelog

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
