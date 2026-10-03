"""Owned worker and driver contracts, decoded at the real local OTLP HTTP boundary."""

import json
import os
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_history import cli, collect_git, embed, journal_sync, load, telemetry

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif("agent_history_test" not in DSN, reason="disposable catalogue required")


@pytest.fixture
def capture(monkeypatch):
    telemetry.shutdown()
    requests = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("localhost", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://localhost:{server.server_port}")
    try:
        yield requests
    finally:
        telemetry.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def decode(requests):
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    traces = [ExportTraceServiceRequest.FromString(body) for path, body in requests if path == "/v1/traces"]
    logs = [ExportLogsServiceRequest.FromString(body) for path, body in requests if path == "/v1/logs"]
    spans = [s for r in traces for resource in r.resource_spans for scope in resource.scope_spans for s in scope.spans]
    records = [
        r
        for request in logs
        for resource in request.resource_logs
        for scope in resource.scope_logs
        for r in scope.log_records
    ]
    return spans, records, "".join(str(r) for r in traces + logs)


def test_real_driver_contract_and_failure(capture):
    telemetry.setup("agent-history-index")
    marker = "synthetic-private-" + "payload"
    with telemetry.db_connect(DSN) as conn:
        conn.execute("CREATE TEMP TABLE telemetry_fixture (body text)")
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO telemetry_fixture VALUES (%s)", [(marker,), (marker,)])
            with cursor.copy("COPY telemetry_fixture (body) FROM STDIN") as copy:
                copy.write_row((marker,))
        conn.commit()
        with conn.cursor(name="fixture") as cursor:
            cursor.execute("SELECT body FROM telemetry_fixture")
            assert cursor.fetchall() == [(marker,)] * 3
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute("SELECT %s::integer", (marker,))
        conn.rollback()
    telemetry.shutdown()
    spans, records, decoded = decode(capture)
    assert {"db.connect", "db.query", "db.copy", "db.commit", "db.rollback"} <= {s.name for s in spans}
    assert marker not in decoded
    assert all(not s.events and not s.status.message for s in spans)
    assert any(r.body.string_value == "outbound.call.failed" for r in records)


def test_actual_owned_worker_paths(capture, tmp_path, monkeypatch):
    marker = "synthetic-private-" + "transcript"
    secret = "gh" + "p_" + "Q" * 36
    with load.connect(DSN) as conn:
        load.apply_schema(conn, force=True)
        conn.execute("TRUNCATE " + ",".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
        conn.execute(
            "DELETE FROM ah.meta WHERE key IN ('embedding_model', 'rebuild_in_progress') OR key LIKE 'embed_tokens_%'"
        )
        conn.execute("DELETE FROM ah.embedding WHERE model = 'fixture'")
        conn.commit()
    source = tmp_path / "source"
    projects = source / "projects" / "fixture"
    projects.mkdir(parents=True)
    (projects / "fixture.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "fixture",
                "timestamp": "2026-09-20T10:00:00Z",
                "message": {"role": "user", "content": marker + secret},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "content.txt").write_text(marker)
    subprocess.run(["git", "-C", str(repo), "add", "content.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=export",
            "-c",
            "user.email=export@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'dsn = "{DSN}"\n[git]\nrepos = ["{repo}"]\n[sources]\nclaude-fixture = "{source}"\n[collector]\nlock_file = "{tmp_path / "collect.lock"}"\n'
    )
    base = ["--config", str(config)]
    assert cli.main(base + ["index"]) == 0
    with load.connect(DSN) as conn:
        assert any(marker in row[0] and secret in row[0] for row in conn.execute("SELECT text FROM ah.message"))
    assert cli.main(base + ["postpass"]) == 0

    class Provider:
        model = "fixture"
        dimensions = 1024
        batch = 64
        usage = {"tokens": 0}

        def embed(self, inputs):
            assert any(marker in text for text in inputs)
            self.usage["tokens"] += len(inputs)
            return [[0.1] * self.dimensions for _ in inputs]

    monkeypatch.setattr(embed, "provider_from_config", lambda _: Provider())
    assert cli.main(base + ["embed"]) == 0
    journal = tmp_path / "journal.sqlite"
    with sqlite3.connect(journal) as db:
        db.execute(f"CREATE TABLE {journal_sync.VIEW} ({','.join(c + ' TEXT' for c in journal_sync.COLUMNS)})")
        from test_journal_sync_pg import make_row

        row = make_row(
            session_uid="fixture",
            namespace="claude-fixture",
            title=marker,
            objective=secret,
            narrative=marker + secret,
            topics_json="[]",
        )
        db.execute(
            f"INSERT INTO {journal_sync.VIEW} VALUES ({','.join('?' for _ in journal_sync.COLUMNS)})",
            [row[column] for column in journal_sync.COLUMNS],
        )
    assert cli.main(base + ["journal-sync", "--source-db", str(journal)]) == 0
    assert cli.main(base + ["collect-git"]) == 0
    monkeypatch.setattr(collect_git, "machine_name", lambda: ("fixture", "fixture"))
    try:
        assert cli.main(base + ["collect", "--dry-run"]) == 0
    finally:
        __import__("signal").alarm(0)
    spans, records, decoded = decode(capture)
    names = [s.name for s in spans]
    for name in ("index.pass", "embed.pass", "journal_sync.pass", "collect_git.pass", "collect.pass"):
        assert names.count(name) == 1
    assert names.count("postpass.pass") == 2
    index = next(s for s in spans if s.name == "index.pass")
    assert any(s.name == "postpass.pass" and s.parent_span_id == index.span_id for s in spans)
    for span in spans:
        if span.name.endswith(".pass"):
            assert any(r.span_id == span.span_id and r.trace_id == span.trace_id for r in records)
    assert marker not in decoded and secret not in decoded
    assert "journal.read" in names and "git.read" in names


def test_subprocess_edges_content_and_status(capture, monkeypatch):
    marker = "synthetic-private-" + "process-output"
    telemetry.setup("agent-history-collect")
    monkeypatch.setattr(collect_git, "GITHUB_OWNERS", ["export"])
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "[]", marker),
            subprocess.CompletedProcess([], 0, "[]", marker),
            subprocess.CompletedProcess([], 7, marker, marker),
            subprocess.CompletedProcess([], 7, marker, marker),
        ]
    )
    monkeypatch.setattr(collect_git.subprocess, "run", lambda *args, **kwargs: next(responses))
    assert collect_git.github_forks() == set()
    assert collect_git.ci_runs("github.com/export/fixture") == []
    assert collect_git.github_forks() is None
    with pytest.raises(collect_git.CICollectionError):
        collect_git.ci_runs("github.com/export/fixture")
    telemetry.shutdown()
    spans, records, decoded = decode(capture)
    assert [s.name for s in spans].count("github.list_repositories") == 2
    assert [s.name for s in spans].count("github.list_runs") == 2
    assert len(records) == 2
    assert marker not in decoded


def test_periodic_real_index_two_passes(capture, tmp_path, monkeypatch):
    from pathlib import Path
    from opentelemetry import trace

    config = tmp_path / "config.toml"
    config.write_text(f'dsn = "{DSN}"\n')
    with load.connect(DSN) as conn:
        load.apply_schema(conn, force=True)
        conn.commit()
    monkeypatch.setattr(
        cli,
        "Path",
        lambda value: tmp_path / "textfiles" if value == "/var/lib/alloy/textfile-agent-history" else Path(value),
    )
    iterations = []

    def sleep(_):
        assert not trace.get_current_span().is_recording()
        iterations.append(telemetry._active)
        if len(iterations) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["--config", str(config), "index", "--archive-root", str(tmp_path / "empty"), "--every", "1"])
    assert iterations[0] is iterations[1]
    spans, records, _ = decode(capture)
    passes = [span for span in spans if span.name == "index.pass"]
    assert len(passes) == 2
    assert all(span.end_time_unix_nano > span.start_time_unix_nano for span in passes)
    assert (
        len(
            [
                record
                for record in records
                if record.body.string_value == "worker.pass.completed"
                and record.span_id in {span.span_id for span in passes}
            ]
        )
        == 2
    )


def test_command_failures_are_correlated_without_content(capture, tmp_path):
    marker = "synthetic-private-" + "configuration"
    config = tmp_path / "config.toml"
    config.write_text(f'unknown = "{marker}"\n')
    for command in ("index", "postpass", "embed", "journal-sync", "collect-git"):
        assert cli.main(["--config", str(config), command]) == 2
    with pytest.raises(Exception):
        collect_git.main(["--config", str(config)])
    spans, records, decoded = decode(capture)
    passes = [span for span in spans if span.name.endswith(".pass")]
    assert len(passes) == 6
    assert all(span.status.code == 2 for span in passes)
    assert all(
        any(
            record.span_id == span.span_id
            and record.trace_id == span.trace_id
            and record.body.string_value == "worker.pass.failed"
            for record in records
        )
        for span in passes
    )
    assert marker not in decoded
