"""pytest entry point. Each check_*.py script needs its own process (store.py reads its database from the
environment at import), so they are run as subprocesses; any FAIL line fails the test.

Extra databases: set TEST_DATABASE_URL (e.g. postgresql://postgres:postgres@localhost/postgres) to also run the
database contract and the upload engine against it. Use a throwaway database."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXTERNAL = os.getenv("TEST_DATABASE_URL")


def run(script, **env):
    r = subprocess.run([sys.executable, os.path.join(HERE, script)], capture_output=True, text=True,
                       env={**os.environ, **env}, timeout=900)
    assert r.returncode == 0, (r.stdout[-4000:] + r.stderr[-2000:])


@pytest.mark.parametrize("script", ["check_engine.py", "check_large.py", "check_multi.py", "check_perf.py", "check_deploy.py", "check_ssh.py", "check_misc.py", "check_infra.py", "check_hosting.py", "check_subs.py", "check_security.py", "check_bigfile.py", "check_webapp.py", "check_admin.py"])
def test_script(script):
    run(script, **({"DATABASE_URL": ""} if script == "check_engine.py" else {}))


def test_contract_sqlite(tmp_path):
    run("check_contract.py", DATABASE_URL=f"sqlite:///{tmp_path}/contract.db")


def test_contract_mongodb_stand_in():
    run("check_contract.py", USE_MONGOMOCK="1", DATABASE_URL="mongodb://localhost/vpsbot_test")


@pytest.mark.skipif(not EXTERNAL, reason="TEST_DATABASE_URL not set")
def test_contract_external_database():
    run("check_contract.py", DATABASE_URL=EXTERNAL)


@pytest.mark.skipif(not EXTERNAL, reason="TEST_DATABASE_URL not set")
def test_engine_external_database():
    run("check_engine.py", DATABASE_URL=EXTERNAL)
