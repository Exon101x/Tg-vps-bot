# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
make lint     # ruff
make test     # pytest (spawns a local FTP server, a local SSH server and in-memory databases)
```

* To also test against a real database: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/postgres pytest -q`
  (use a throwaway database).
* Each `tests/check_*.py` script runs in its own process because `store.py` reads its database from the environment at import.
* New storage backends implement the interface in `backends.py` and must pass `tests/check_contract.py` unchanged.
