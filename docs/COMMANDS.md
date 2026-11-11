# Command reference

Every command the bot understands, with arguments and examples. `/help` in chat prints a condensed version.
Only users whose Telegram id is in `ALLOWED_USERS` can use anything (see [CONFIG.md](CONFIG.md)).

**Conventions:** `<angle>` = required, `[square]` = optional. "Active server" is the one you last added or
switched to with `/use` — file and VPS commands all act on it. Every command works in private chat with the bot.

## Table of contents

- [Servers](#servers)
- [File transfer](#file-transfer)
- [Remote filesystem](#remote-filesystem)
- [The upload queue](#the-upload-queue)
- [Folders and duplicate policies](#folders-and-duplicate-policies)
- [VPS management (SSH servers)](#vps-management-ssh-servers)
- [Shell access](#shell-access)
- [Files over 2 GB](#files-over-2-gb)
- [Bot status and misc](#bot-status-and-misc)

## Servers

| Command | What it does |
|---|---|
| `/add <name> <target>` (alias `/addvps`) | Save a server and make it active. The bot then asks for the password (or waits for an SSH key file) and pins the host key on first connect |
| `/servers` | List your servers with inline buttons — tap to switch |
| `/use <name>` | Switch the active server |
| `/del <name>` | Delete a saved server (asks for confirmation) |
| `/status` | Active server info: scheme, host, cwd, disk, monitoring state |
| `/monitor on\|off` | Turn monitoring alerts on/off for the active server |
| `/audit` | Your action log (recent commands, transfers, deletes) |
| `/about` | Bot status: uptime, version, database, queue length, session state |

### Target formats for `/add`

```
ssh://user@host[:port]        asks for the password, or an SSH key file
ssh://user:pass@host          password in the URL (still fine, it is encrypted at rest)
ftp://user:pass@host[:port][/upload-dir]
ftps://user:pass@host[:port][/upload-dir]
```

`ssh://` servers get the full VPS management feature set; `ftp://` and `ftps://` servers get file transfer
and the remote filesystem commands. Prefer `ftps://` and `ssh://` over plain `ftp://` (see [SECURITY.md](../SECURITY.md)).

## File transfer

### Sending files (no command needed)

Send any file, photo, video or document — it streams straight to the active server's upload folder.
Albums and bulk sends are fine. What happens under the hood:

* no temp disk — bytes flow from Telegram into the server connection;
* the job is queued persistently per server (restarts don't lose it);
* failures retry with backoff (`MAX_RETRIES`), interrupted uploads resume from the server-side `.part` file;
* live progress: speed, ETA, uploaded/total.

Optional caption routing:

```
> backups/photos.zip       upload as backups/photos.zip
> backups/2026/            upload into that folder (name kept)
```

Caption destinations go through the same sanitising as folder templates — no absolute paths, no `../`.

| Command | What it does |
|---|---|
| `/fetch <url> [name]` | Download an HTTP(S) URL to the active server. On **SSH** servers the VPS pulls the file itself (curl/wget, nothing flows through the bot); on **FTP/FTPS** the bot streams it through the queue — resumable, no temp disk, `MAX_FETCH_MB` cap (0 = unlimited). SSRF-guarded: private addresses are refused, redirects re-validated hop by hop, the connection is pinned to the address that passed the check |
| `/get <path> [-v MB]` | Send a file **from** the server to this chat. Up to `TG_MAX_MB` (default 1900) it goes as one file; bigger files are sent as `.001 .002 …` volumes of `-v` MB (default: Telegram's limit, shrunk to 85 % of free disk). Above 5 volumes the bot asks for confirmation. See [files over 2 GB](#files-over-2-gb) |
| `/join <name> [--rm]` | (SSH) Reassemble `name.001 name.002 …` in the current directory on the server into `name`. The generated script checks for missing volumes, free space and size before writing anything; `--rm` deletes the volumes afterwards |

## Remote filesystem

All of these operate on the active server; paths are relative to your current directory (`/pwd`).

| Command | What it does |
|---|---|
| `/pwd` | Print the current directory |
| `/cd <dir>` | Change directory |
| `/ls [dir]` | List a directory (default: current) |
| `/mkdir <dir>` | Create a directory |
| `/mv <from> <to>` | Move/rename |
| `/rm <path>` | Delete a file — asks for confirmation first |

## The upload queue

| Command | What it does |
|---|---|
| `/queue` | List your jobs with state icons: ⏳ queued · ⬆️ running · ✅ done · ❌ failed · ✖ cancelled. Failed jobs show the error |
| `/retry [id]` | Retry a failed job (or all failed jobs, with one tap on the button) |
| `/stop <id\|all>` | Cancel a job or everything queued |
| `/usage` | Your upload totals: files and bytes today and over the last 7 days, plus jobs waiting/running and failed |

The queue lives in the database: restarts, redeploys and crashes keep it intact. Retries back off
exponentially; a job that keeps failing is marked failed and stays retryable.

## Folders and duplicate policies

| Command | What it does |
|---|---|
| `/folder <template>` | Auto-organise uploads into folders. Variables: `{user}` `{uid}` `{date}` `{year}` `{month}` `{day}` `{type}` `{ext}`. Examples: `/folder {user}/{date}` · `/folder backups/{type}` · `/folder /uploads/{user}` · `/folder off` disables |
| `/ondup overwrite\|skip\|rename` | What to do when the target file already exists: overwrite it, skip the upload, or save under a new name (`photo (1).jpg`). Default: `overwrite` |

## VPS management (SSH servers)

### `/vps` — the dashboard

Inline buttons: 📊 Stats · ⚙️ Processes · 🐳 Docker · 🔌 Ports · 🔐 Logins · 📦 Updates · 💾 Disk ·
🛡 Firewall · 🧯 Failed · 🔄 Reboot. Each runs the matching read-only command and shows the output.
(`/menu` is an alias.)

### Single commands

| Command | What it does |
|---|---|
| `/stats` | Host overview: OS, uptime, load + cores, RAM, swap, disk |
| `/ps` | Top processes by CPU (`ps aux`) |
| `/ports` | Listening ports (`ss -tulpn`) |
| `/logins` | Recent logins (`last`, `who`) |
| `/updates` | Upgradable packages (apt-based systems) |
| `/disk` | Disk usage per filesystem + largest top-level directories |
| `/firewall` | ufw status (or iptables rules) |
| `/failed` | Failed systemd units |
| `/service <name> [action]` | systemd control: `status` (default) `start` `stop` `restart` `reload` `enable` `disable` |
| `/logs <unit> [lines]` | journalctl for a unit (default 40 lines) |
| `/docker [ps\|start\|stop\|restart\|logs <name>]` | Docker control (default: `ps`) |
| `/upgrade` | `apt update && apt upgrade` — asks for confirmation, output streamed |
| `/reboot` | Reboot the server — asks for confirmation |

Non-root SSH users need passwordless sudo for the service/docker/reboot actions; the bot prefixes those
commands with sudo automatically when needed.

## Shell access

| Command | What it does |
|---|---|
| `/sh <command>` | Run one command on the active SSH server. The working directory is remembered between calls (see `/cd`). Output is streamed back; long output is truncated with the tail kept |
| `/term` | Terminal mode: every message you send is a command, until you send `/term` again or stay idle for 10 minutes. Start a line with `$ ` when it begins with `/` (so Telegram does not eat it as a command) |

Both log to `/audit`. `/cancel` bails out of an open password/key prompt or a `/multi` collection.

## Files over 2 GB

Telegram caps a single file at 2 GB (4 GB for Premium senders; a bot cannot send more than 2 GB).
Two directions, two mechanisms — both end-to-end resumable:

### Telegram → server (big upload)

1. Split the file once on your computer: 7-Zip → "Split to volumes", or
   `split -b 1900M -d -a 3 --numeric-suffixes=1 bigfile bigfile.`
2. `/multi bigfile` (optional — the name is also inferred from `bigfile.001`-style names)
3. Send all the `.001 .002 …` parts as documents
4. `/done`

The bot joins them **while streaming** into ONE file on any FTP/FTPS/SFTP server — no temp disk, resumable
inside any volume, and gaps/duplicates are detected and reported. On SSH servers you can instead upload the
parts normally and use `/join bigfile --rm` (needs free space equal to the file size).

### Server → Telegram (big download)

`/get /srv/backup.iso` — the bot plans volumes (default: as large as Telegram allows, shrunk to 85 % of the
free disk; `-v 500` for 500 MB volumes), streams one volume at a time and deletes it after sending. Reassemble
locally with 7-Zip (open `name.001`), `cat name.0* > name` on Linux/macOS, or `copy /b` on Windows — or send
them to an SSH server and `/join` there.

## Bot status and misc

| Command | What it does |
|---|---|
| `/start`, `/help` | The in-chat help |
| `/perf` | Detected CPU/RAM and the active speed profile (see [CONFIG.md](CONFIG.md#performance-tuning)) |
| `/cancel` | Cancel an open prompt (password/key entry), a `/multi` collection or a pending confirmation |
| `/about` | Version, uptime, database, queue length, session state |
