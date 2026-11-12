# Security policy

This bot stores server credentials and can run commands on your servers. Please take a minute to read
both halves of this page: how to report problems, and how to run it safely.

## Reporting a vulnerability

**Please report privately, not in public issues.** Open a private report through the repository's
*Security → Report a vulnerability* tab (GitHub security advisories). Include:

* what you found and its impact,
* step-by-step reproduction (chat transcript, `.env` values involved — **redact real credentials**),
* the version or commit you tested.

You will get an acknowledgement; fixes land with the normal CI gate plus a regression test, and the
advisory credits you unless you prefer otherwise.

## Supported versions

The `main` branch is what is developed and tested; security fixes land there. Tagged releases (if you cut
them) should be patched by backporting the fix commit.

## Security model — what the code does for you

| Layer | Mechanism |
|---|---|
| Access control | Every handler sits behind the `ALLOWED_USERS` gate; the bot refuses to start with an empty list. `FORCE_SUB` (optional) additionally requires a channel subscription — enforced for chat commands, buttons and the Mini App alike |
| Admin controls | `ADMINS` / `DEV_ID` unlock the admin panel; bans flip one flag and lock a user out of the chat **and** the app at once (admin accounts cannot be banned through the panel) |
| Mini App auth | Every request carries Telegram's `initData`; the HMAC-SHA256 signature is verified constant-time with `BOT_TOKEN`, `auth_date` must be fresher than 24 h — no cookies, no sessions, no secrets stored client-side |
| Rate limiting | The app's API is a per-user token bucket (`WEB_RATE`, default 90/min) — a runaway client or a replayed `initData` gets `429` |
| Web hardening | Static pages ship with a strict CSP (own scripts only + `telegram.org`, `object-src 'none'`, `frame-ancestors` limited to Telegram), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`; the static route is a fixed whitelist (no traversal, no listing) |
| Credentials at rest | Server passwords and private keys are Fernet-encrypted with `SECRET_KEY` **before** they reach any database — a stolen database alone reveals nothing |
| SSH trust | Host keys are pinned on first use; a changed key is rejected (`/del` + `/add` re-pins deliberately) |
| URL fetching | SSRF guard: private/reserved addresses refused, the host resolved **once** with the connection pinned to that exact address (closes DNS-rebinding TOCTOU), redirects re-validated hop by hop, `MAX_FETCH_MB` enforced while streaming |
| FTP safety | Paths with line breaks are refused (FTP command injection); duplicate policies never silently degrade from `skip`/`rename` to `overwrite` |
| Path handling | Folder templates and caption destinations pass the same segment-wise sanitiser — no absolute paths, no `../` traversal, no CRLF |
| Dangerous actions | Deletes, reboot, service/docker control, apt upgrades and big multi-volume downloads all require an explicit confirmation tap |
| Audit trail | Every action lands in the audit table, visible with `/audit` |
| Runtime hardening | Docker: non-root `bot` user, `HEALTHCHECK`, no image secrets. systemd: `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, `ProtectHome` |
| Config robustness | Optional numeric env vars parse tolerantly (warn + default) — no crash loops over typos |

The full technical write-up of these mechanisms: [docs/ARCHITECTURE.md → Security model](docs/ARCHITECTURE.md#security-model).

## Deploying safely — a hardening checklist

- [ ] Keep `ALLOWED_USERS` to people you would hand root on your servers — that is effectively what they get.
- [ ] Generate `SECRET_KEY` with the wizard (`deploy.py`) or `Fernet.generate_key()`; **back it up** outside the host.
- [ ] Never commit `.env`; it is git-ignored by default — keep it that way.
- [ ] Prefer `ssh://` and `ftps://` over plain `ftp://` (FTP sends passwords and data in the clear).
- [ ] Use a **dedicated SSH key** (an ed25519 key with a forced command or limited shell if you can) rather than a root password.
- [ ] On your own VPS set `HEALTH_BIND=127.0.0.1` so the health endpoint is not exposed publicly.
- [ ] Keep the host's OS updated — the bot can help (`/updates`, `/upgrade`).
- [ ] If the host is shared, run under the provided systemd unit or Docker (non-root) rather than a root shell.

## If something went wrong

| Scenario | What to do |
|---|---|
| `SECRET_KEY` may have leaked | Rotate it (`deploy.py --rotate-key`), which re-encrypts saved credentials, and update your backups |
| Bot token leaked | Revoke it in @BotFather immediately (`/revoke`), set the new one in `.env`, restart |
| Server credentials leaked | Change them **on the servers** (the bot only stores what you gave it — it cannot change passwords) |
| Database dumped | The credentials inside are ciphertext — but rotate `SECRET_KEY` and the SSH passwords anyway if the key could be gone with it |
| Lost `SECRET_KEY` | Saved servers are unreadable by design. `/del` them and re-`/add` with the new key |

## Scope and non-goals

* Anyone in `ALLOWED_USERS` can run arbitrary commands on every server they add — the bot gates *who*, not *what*. If you need command restrictions, use a limited SSH user/forced command on the server side.
* `/sh` and `/term` are as powerful as the SSH account they run as. Least privilege is configured on the server, not in the bot.
* Plain FTP is supported for old servers, but its protocol has no confidentiality — treat it as you would telnet.
