"""Offline runtime and real writer smoke test. Run: python tests/container_writer.py IMAGE.

Images must already be built/pulled. Both containers have networking disabled; PostgreSQL is
reached only through a disposable Unix socket volume. No host config, credentials or DSNs enter.
"""

import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

DATABASE_IMAGE = "paradedb/paradedb:18-v0.25.10@sha256:188591a0bc317beb2c6d6d3f9ef0cb3e859d09ecc15a71dda5e9a027876686cf"
ROOT = Path(__file__).resolve().parents[1]


def docker(*args, input=None):
    return subprocess.run(["docker", *args], input=input, text=True, capture_output=True, timeout=180, check=False)


def ok(result):
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return result.stdout.strip()


def main(image):
    name = "container-writer-" + uuid.uuid4().hex[:12]
    volume = name + "-socket"
    failures = []
    try:
        ok(docker("volume", "create", volume))
        ok(
            docker(
                "run",
                "-d",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--name",
                name,
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_DB=agent_history_test",
                "-v",
                volume + ":/var/run/postgresql",
                DATABASE_IMAGE,
                "-c",
                "listen_addresses=",
                "-c",
                "unix_socket_directories=/var/run/postgresql",
            )
        )
        for _ in range(90):
            ready = docker(
                "exec",
                name,
                "sh",
                "-c",
                '[ "$(head -n 1 /proc/1/comm)" = postgres ] && pg_isready -U postgres -d agent_history_test',
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError("disposable database did not become ready")

        def sql(statement):
            return ok(
                docker(
                    "exec",
                    "-i",
                    name,
                    "psql",
                    "-XAt",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    "postgres",
                    "-d",
                    "agent_history_test",
                    input=statement,
                )
            )

        roles = (ROOT / "src/agent_history/sql/roles.sql").read_text()
        sql("\\set writer_password synthetic\n\\set reader_password synthetic\n" + roles)

        def app(*args, python=False, config=None):
            command = ["run", "--rm", "--pull=never", "-i", "--network", "none", "-v", volume + ":/var/run/postgresql"]
            if python:
                command += ["--entrypoint", "/opt/venv/bin/python"]
            command += [image]
            if config is not None:
                command += ["--config", "/dev/stdin"]
            return docker(*command, *args, input=config)

        # Use the installed interpreter and installed package, never a host source mount.
        runtime = app(
            "-c",
            'from zoneinfo import ZoneInfo; print(ZoneInfo("Europe/London")); '
            'import agent_history.collect_git; print("collect_git imported")',
            python=True,
        )
        if runtime.returncode or "collect_git imported" not in runtime.stdout:
            failures.append("runtime: " + runtime.stdout + runtime.stderr)

        dsn = "host=/var/run/postgresql dbname=agent_history_test user=ah_writer connect_timeout=5"
        ok(app("--dsn", dsn, "init"))
        config = '[git.repo_aliases]\n"old/widget" = "new/widget"\n'
        published = app("--dsn", dsn, "collect-git", config=config)
        retained = sql("SELECT value FROM ah.meta WHERE key='git_repo_aliases';")
        if published.returncode or not retained or json.loads(retained) != {"old/widget": "new/widget"}:
            failures.append(
                f"publication: exit={published.returncode}, retained={retained!r}, "
                f"stdout={published.stdout!r}, stderr={published.stderr!r}"
            )
        # A conflicting mapping must not report success or overwrite the first publication.
        failed = app("--dsn", dsn, "collect-git", config=config.replace("new/widget", "other/widget"))
        if (
            failed.returncode != 1
            or failed.stdout
            or failed.stderr.strip() != "agent-history: collect-git failed (error: ValueError)"
        ):
            failures.append(
                f"failed publication: exit={failed.returncode}, stdout={failed.stdout!r}, stderr={failed.stderr!r}"
            )
        if retained and sql("SELECT value FROM ah.meta WHERE key='git_repo_aliases';") != retained:
            failures.append("failed publication changed retained aliases")
        assert not failures, "\n".join(failures)
        print("PASS: Europe/London, collect_git import, writer publication/readback, nonzero conflict")
    finally:
        docker("rm", "-f", "-v", name)
        docker("volume", "rm", volume)


if __name__ == "__main__":
    main(sys.argv[1])
