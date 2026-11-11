# Changelog

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
