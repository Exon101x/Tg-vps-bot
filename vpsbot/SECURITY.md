# Security policy

This bot stores server credentials and can run commands on your servers, so please report problems privately.

**Reporting a vulnerability:** open a private report through the repository's *Security → Report a vulnerability*
tab (GitHub security advisories). Please do not file public issues for security problems. Include what you found,
how to reproduce it, and which version or commit.

**Deploying safely**

* Keep `ALLOWED_USERS` to people you would give root on your servers to.
* Back up `SECRET_KEY` and never commit `.env`. Anyone with the database and the key can read saved credentials.
* Use `ssh://` or `ftps://` instead of plain `ftp://`, and a dedicated SSH key rather than a root password.
* Revoke the bot token in @BotFather and rotate `SECRET_KEY` if you suspect a leak (re-add your servers afterwards).
