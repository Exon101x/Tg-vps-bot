# Contributing

Thanks for helping — pull requests, bug reports and doc fixes are all welcome.

## Development environment

```bash
git clone https://github.com/Exon101x/Tg-vps-bot && cd Tg-vps-bot
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

make lint     # ruff check .
make test     # pytest -q: spawns a local FTP server, a local SSH server and in-memory databases
make run      # python bot.py (needs .env; `python deploy.py --target local` can write one)
```

Python 3.10–3.12 are supported and all three are tested in CI on every push and PR.

## Project layout

A one-page map of how the pieces fit: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Short version: `bot.py` (Telegram layer) → `jobs.py` (queue worker) → `sources.py`/`remote.py`
(transfer) → `store.py` → `backends.py` (SQLite/Postgres/MySQL/MongoDB). `subscriptions.py`,
`hosting.py` and `payments.py` handle plans, VPS provisioning and gateways on top of `store.py`;
`persist.py` is the state-machine backbone behind every mid-chat flow. `deploy.py` and `migrate.py`
are standalone tools; `perf.py`, `largefiles.py`, `util.py` are leaf libraries.

## Code style

* `ruff` is the linter and the only formatter gate — `make lint` must pass (config in `pyproject.toml`).
* Plain modules, no package scaffolding; the entry point is `python bot.py`.
* Match the existing style: docstrings on every public function, `asyncio` handlers in `bot.py`,
  synchronous code in `store.py`/`backends.py` (queries are deliberately synchronous).
* New configuration goes through `util.env_int` / tolerant parsing — a typo in an optional variable must
  never crash the bot at import.
* Anything security-relevant (paths, URLs, credentials, protocol strings) gets a test in
  `tests/check_security.py` — that suite is the memory of every fixed vulnerability.

## Testing

* Each `tests/check_*.py` script runs in its own process, because `store.py` reads its database from the
  environment at import — add new checks as separate scripts, not into existing ones.
* **Interactive state** (`persist.py`): anything a handler remembers between messages — a confirm
  token, a pending password, a picker — must go into a `State` (plain JSON values, its own `ts`, a
  TTL) so the flow survives a restart. `tests/check_state.py` re-loads each machine over the same
  database row and must keep passing; add a check there when you add or change a machine.
* The suite stands up a **real local FTP server and a real local SSH/SFTP server** and moves real bytes;
  prefer that over mocks for anything touching `remote.py`.
* **Storage backends** implement the interface in `backends.py` and must pass `tests/check_contract.py`
  unchanged — that contract test is what keeps SQLite, PostgreSQL, MySQL and MongoDB behaving identically.
* To also test against a real database (what CI does with PostgreSQL 16):

  ```bash
  TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/postgres pytest -q
  ```

  Use a throwaway database — the suite writes to it.
* Python-version-specific code paths (the 3.10 vs 3.11+ dependency pins, for example) should be checked
  with `uv pip compile --python-version 3.10 requirements.txt` before touching `requirements*.txt`.

## Pull request checklist

- [ ] `make lint && make test` pass locally
- [ ] New behavior has a check in `tests/check_*.py` (security-relevant changes: in `check_security.py`)
- [ ] `README.md` / `docs/` updated if the change is user-visible
- [ ] `CHANGELOG.md` has a line under an *Unreleased* heading (or the version being prepared)
- [ ] No secrets, tokens or real server addresses in the diff
- [ ] `requirements*.txt` changes keep all pins working on 3.10 **and** 3.11+ (CI will tell you, but `uv` catches it first)

CI runs ruff + the suite on 3.10/3.11/3.12 against PostgreSQL, then builds the Docker image —
all of it must be green before merge.

## Releasing

* Version bumps go in `CHANGELOG.md` (keep the existing entry style: what changed and why, user-relevant
  wording) and in the README badges only if something structural changed.
* Dependabot handles dependency bumps weekly; each bump goes through the same CI matrix — merge them when
  green, and expect `SQLAlchemy`/`pymongo`-style pins to need the conditional `python_version` markers
  when a library drops 3.10.
