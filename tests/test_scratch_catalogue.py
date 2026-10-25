"""Admission must stop the actual fault modules before any database I/O."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("AGENT_HISTORY_TEST_DSN", "AGENT_HISTORY_TEST_READER_DSN", "AGENT_HISTORY_TEST_ADMIN_DSN")
ROLES = ("ah_writer", "ah_reader", "postgres")


@pytest.mark.parametrize(
    "role,fault",
    [
        (role, fault)
        for role in range(3)
        for fault in ("missing", "appname_spoof", "user_spoof", "host", "hostaddr", "port")
    ]
    + [
        (0, "default_host"),
        (1, "default_port"),
        (2, "multihost"),
        (0, "service"),
        (2, "nested_db"),
        (1, "environment"),
    ],
)
def test_fault_modules_refuse_unsafe_targets_without_connecting(role, fault):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    for key, user in zip(KEYS, ROLES, strict=True):
        env[key] = f"host=127.0.0.1 port=5432 dbname=agent_history_test user={user}"
    dsn = env[KEYS[role]]
    if fault == "missing":
        dsn = ""
    elif fault == "appname_spoof":
        dsn = dsn.replace("dbname=agent_history_test", "dbname=ordinary application_name=agent_history_test")
    elif fault == "user_spoof":
        dsn = dsn.replace("dbname=agent_history_test", "dbname=ordinary").replace(ROLES[role], "agent_history_test")
    elif fault == "host":
        dsn = dsn.replace("127.0.0.1", "127.0.0.2")
    elif fault == "hostaddr":
        dsn += " hostaddr=127.0.0.2"
    elif fault == "port":
        dsn = dsn.replace("port=5432", "port=5433")
    elif fault == "default_host":
        dsn = dsn.replace("host=127.0.0.1 ", "")
    elif fault == "default_port":
        dsn = dsn.replace("port=5432 ", "")
    elif fault == "multihost":
        dsn = dsn.replace("127.0.0.1", "127.0.0.1,127.0.0.2")
    elif fault == "service":
        dsn += " service=untrusted"
    elif fault == "nested_db":
        dsn = dsn.replace("dbname=agent_history_test", "dbname='host=127.0.0.2 dbname=agent_history_test'")
    else:
        env["PGHOSTADDR"] = "127.0.0.2"
    env[KEYS[role]] = dsn
    # Mock only the connection edge. Run both real modules and their actual fixture graph;
    # the old imported writer fixture can otherwise connect before function-scoped guards.
    probe = """
import psycopg
import pytest
from agent_history import load
calls = []
def forbidden(*args, **kwargs):
    calls.append(True)
    raise AssertionError("database I/O before scratch admission")
psycopg.connect = forbidden
load.connect = forbidden
class Probe:
    def __init__(self):
        self.collected = 0
        self.skipped = 0
    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
    def pytest_runtest_logreport(self, report):
        if report.skipped:
            self.skipped += 1
p = Probe()
code = pytest.main(["-q", "-rs", "tests/test_catalogue_metrics_pg.py", "tests/test_loop_health_pg.py"], plugins=[p])
assert code == 0, code
assert p.collected > 0 and p.skipped == p.collected, (p.collected, p.skipped)
assert not calls, "unsafe fixture attempted a database connection"
print("all fault-module tests refused before connection")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all fault-module tests refused before connection" in result.stdout
