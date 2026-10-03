"""Worker stdout/stderr is shipped as logs: failures must be loud but carry no row, path or credential text.

Each test runs the real console entry point in a subprocess and plants synthetic markers where an
exception message, a skip reason or a provider response body would carry them.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")


def disposable(dsn: str) -> bool:
    """Only a database whose own name marks it disposable: the shared fixtures truncate tables."""
    if not dsn:
        return False
    try:
        from psycopg.conninfo import conninfo_to_dict

        return "agent_history_test" in (conninfo_to_dict(dsn).get("dbname") or "")
    except Exception:
        return False


DISPOSABLE = disposable(DSN)
needs_db = pytest.mark.skipif(not DISPOSABLE, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")
FAILURE_LINE = re.compile(r"agent-history: [a-z-]+ failed \([a-z]+: [A-Za-z_]+\)")


def marker(kind: str) -> str:
    # Built at run time so no scanner sees a fixed literal.
    return f"synthetic{kind}" + secrets.token_hex(8)


def environment(**extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OTEL_", "AGENT_HISTORY_")) and key not in ("OPENAI_API_KEY",)
    }
    env.update(extra)
    return env


def entry(name: str) -> list[str]:
    script = Path(sys.executable).with_name(name)
    assert script.exists(), f"console script {name} is not installed next to {sys.executable}"
    return [str(script)]


def command(how: str) -> list[str]:
    # "console" is the installed script that the container ENTRYPOINT and launchd run.
    return [sys.executable, "-m", "agent_history.cli"] if how == "module" else entry("agent-history")


def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


def assert_absent(result: subprocess.CompletedProcess, *markers: str) -> None:
    output = result.stdout + result.stderr
    for value in markers:
        assert value not in output
    assert "Traceback" not in result.stderr


def malformed_dsn(secret: str) -> str:
    # An unquoted space in a password: libpq echoes the token after it in its parse error.
    return f"host=127.0.0.1 port=1 password=part {secret}"


@pytest.mark.parametrize("how", ["module", "console"])
def test_single_shot_worker_failure_is_one_bounded_line(tmp_path, how):
    secret = marker("cred")
    private_dir = marker("path")
    result = run(
        [
            *command(how),
            "--config",
            str(tmp_path / "absent.toml"),
            "--dsn",
            malformed_dsn(secret),
            "journal-sync",
            "--source-db",
            str(tmp_path / private_dir / "app.db"),
        ],
        environment(),
        tmp_path,
    )
    assert result.returncode == 1
    assert_absent(result, secret, private_dir)
    assert result.stdout == ""
    assert FAILURE_LINE.fullmatch(result.stderr.strip()), result.stderr


def test_collector_summary_reports_the_exception_type_only(tmp_path):
    secret = marker("cred")
    config = tmp_path / "config.toml"
    config.write_text(f'[collector]\nlock_file = "{tmp_path / "collect.lock"}"\n')
    for argv, env in (
        ([*command("module"), "--config", str(config), "--dsn", malformed_dsn(secret), "collect"], environment()),
        (
            [*entry("agent-history-collect"), "--config", str(config)],
            environment(AGENT_HISTORY_INGEST_DSN=malformed_dsn(secret)),
        ),
    ):
        result = run(argv, env, tmp_path)
        assert result.returncode == 1
        assert_absent(result, secret)
        summary = json.loads(result.stdout)
        assert summary["ok"] is False
        assert summary["error"] == "ProgrammingError"


@needs_db
def test_journal_skip_reasons_carry_no_path_or_driver_text(tmp_path):
    import sqlite3

    from agent_history import journal_sync

    private_dir = marker("path")
    private_column = marker("column")
    directory = tmp_path / private_dir
    directory.mkdir()
    garbage = directory / "garbage.db"
    garbage.write_text("not a database " + private_dir)
    partial = directory / "partial.db"
    db = sqlite3.connect(partial)
    db.execute(f"CREATE TABLE t ({private_column} TEXT)")
    columns = ", ".join(f"{private_column} AS {name}" for name in journal_sync.COLUMNS[:-1])
    db.execute(
        f"CREATE VIEW {journal_sync.VIEW} AS SELECT {columns}, missing_{private_column} AS app_instance_id FROM t"
    )
    db.commit()
    db.close()
    for source in (directory / "absent.db", garbage, partial):
        result = run(
            [
                *command("console"),
                "--config",
                str(tmp_path / "absent.toml"),
                "--dsn",
                DSN,
                "journal-sync",
                "--source-db",
                str(source),
            ],
            environment(),
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert_absent(result, private_dir, private_column)
        assert json.loads(result.stdout)["journal_skipped_reason"]


@needs_db
def test_single_shot_index_rejects_a_source_without_echoing_it(tmp_path):
    private_dir = marker("path")
    for source in (str(tmp_path / private_dir), f"bad{private_dir}={tmp_path}"):
        result = run(
            [*command("console"), "--config", str(tmp_path / "absent.toml"), "--dsn", DSN, "index", "--source", source],
            environment(),
            tmp_path,
        )
        assert result.returncode == 1
        assert_absent(result, private_dir)
        assert result.stderr.startswith("--source ")


@needs_db
def test_single_shot_embed_withholds_provider_body_and_token(db, upstream, tmp_path):
    from test_embed_pg import MODEL

    provider, response = upstream
    body_marker = marker("body")
    token = marker("token")
    response.update(status=401, body={"error": {"message": f"echoed input {body_marker}"}})
    config = tmp_path / "config.toml"
    config.write_text(
        "[embedding]\nenabled = true\n"
        f'base_url = "{provider.base_url}"\nmodel = "{MODEL}"\napi_key_env = "SYNTHETIC_EMBED_KEY"\n'
    )
    result = run(
        [*command("console"), "--config", str(config), "--dsn", DSN, "embed", "--cap", "0", "--daily-cap", "0"],
        environment(SYNTHETIC_EMBED_KEY=token),
        tmp_path,
    )
    assert result.returncode == 1
    assert_absent(result, body_marker, token)
    assert FAILURE_LINE.fullmatch(result.stderr.strip().splitlines()[-1]), result.stderr


if DISPOSABLE:
    from test_embed_pg import db, upstream  # noqa: E402,F401  (fixtures shared with the embedder tests)
