"""Proxied Claude Code usage: unknown, never zero, and filled from LiteLLM spend logs by request id."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agent_history import litellm_usage, load
from test_loop_identity_pg import clean, conn  # noqa: F401 - real disposable catalogue fixture

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(
    not all("agent_history_test" in dsn for dsn in (DSN, ADMIN_DSN)), reason="scratch databases not set"
)
SCHEMA = "litellm_fixture"
SID = "33333333-3333-4333-8333-333333333333"
ZERO = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


@pytest.fixture
def spend_logs():
    """A disposable stand-in for the proxy's spend-log table, read through its own connection."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.execute(f"CREATE SCHEMA {SCHEMA}")
        admin.execute(
            f'CREATE TABLE {SCHEMA}."LiteLLM_SpendLogs" (request_id text PRIMARY KEY, prompt_tokens integer, '
            "completion_tokens integer, metadata jsonb)"
        )
    source = psycopg.connect(ADMIN_DSN, options=f"-c search_path={SCHEMA}")
    try:
        yield source
    finally:
        source.close()
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def spend(source, request_id, prompt, completion, cached, reasoning=None):
    usage = {"prompt_tokens_details": {"cached_tokens": cached}}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    source.execute(
        'INSERT INTO "LiteLLM_SpendLogs" VALUES (%s,%s,%s,%s)',
        (request_id, prompt, completion, json.dumps({"usage_object": usage})),
    )
    source.commit()


def transcript(root, at, blocks):
    records = []
    for n, (mid, stop, usage) in enumerate(blocks):
        ts = (at + timedelta(seconds=n)).isoformat().replace("+00:00", "Z")
        records.append(
            {
                "type": "assistant",
                "sessionId": SID,
                "uuid": f"a-{n}",
                "timestamp": ts,
                "cwd": "/work/project",
                "message": {
                    "id": mid,
                    "role": "assistant",
                    "model": "synthetic-proxied",
                    "stop_reason": stop,
                    "usage": usage or ZERO,
                    "content": [{"type": "text", "text": f"block {n}"}],
                },
            }
        )
    path = root / "projects" / "-work-project" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def usage(db, response_id):
    return db.execute(
        "SELECT input_uncached, cache_read, cache_write_5m, cache_write_1h, output, reasoning "
        "FROM ah.llm_call WHERE response_id = %s",
        (response_id,),
    ).fetchone()


def test_unknown_proxied_usage_is_filled_from_spend_logs(clean, spend_logs, tmp_path):  # noqa: F811
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    filled, missing, complete = (f"msg_{uuid.uuid4()}" for _ in range(3))
    real = {"input_tokens": 4, "output_tokens": 9, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0}
    transcript(
        tmp_path,
        at,
        [(filled, None, None), (filled, None, None), (missing, None, None), (complete, "end_turn", real)],
    )
    spend(spend_logs, filled, 1300, 77, 1000, reasoning=12)
    spend(spend_logs, complete, 999, 999, 0)
    sources = {"claude-proxied": tmp_path}

    # Without a source the counts are unknown, not a measured zero.
    assert load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    assert usage(clean, filled) == (None,) * 6
    assert usage(clean, missing) == (None,) * 6

    def fill(db):
        return litellm_usage.fill(db, spend_logs, ["claude-proxied"], days=3)

    stats = load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None, usage_fill=fill)
    assert stats.errors == 0
    assert usage(clean, filled) == (300, 1000, 0, 0, 77, 12)
    assert usage(clean, missing) == (None,) * 6
    # A complete transcript count is never overwritten by the spend log.
    assert usage(clean, complete)[:5] == (4, 40, 0, None, 9)
    # The fill marked the session dirty, so the same pass's rollup includes the filled call.
    assert clean.execute(
        "SELECT r.output FROM ah.session_rollup r JOIN ah.session s ON s.id = r.session_id WHERE s.session_uid = %s",
        (SID,),
    ).fetchone() == (86,)


def test_zero_rows_indexed_before_the_unknown_rule_are_backfilled(clean, spend_logs, tmp_path):  # noqa: F811
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=10)
    old = f"msg_{uuid.uuid4()}"
    transcript(tmp_path, at, [(old, None, None)])
    assert load.refresh(clean, sources={"claude-proxied": tmp_path}, textfile=None, log=lambda *_: None).errors == 0
    # A catalogue indexed by an earlier release stored these counts as zero.
    clean.execute(
        "UPDATE ah.llm_call SET input_uncached=0, cache_read=0, cache_write_5m=0, cache_write_1h=0, output=0 "
        "WHERE response_id=%s",
        (old,),
    )
    clean.commit()
    spend(spend_logs, old, 50, 5, 20)
    assert litellm_usage.fill(clean, spend_logs, ["claude-proxied"], days=3) == {
        "candidates": 0,
        "filled": 0,
        "missing": 0,
        "unusable": 0,
    }
    assert litellm_usage.fill(clean, spend_logs, ["claude-other"], days=90)["candidates"] == 0
    assert litellm_usage.fill(clean, spend_logs, ["claude-proxied"], days=90, batch=1)["filled"] == 1
    clean.commit()
    assert usage(clean, old) == (30, 20, 0, 0, 5, None)


@pytest.mark.parametrize(
    ("section", "label"),
    [
        ({"dsn_file": "{root}/absent/litellm.dsn"}, "litellm.config"),
        ({"dsn": "host=127.0.0.1 port=1 connect_timeout=2"}, "litellm.connect"),
        # A reachable database without the spend-log table: the query fails.
        ({"dsn": ADMIN_DSN}, "litellm.query"),
    ],
)
def test_an_unavailable_litellm_leaves_usage_unknown_and_the_pass_succeeds(
    clean,  # noqa: F811
    tmp_path,
    monkeypatch,
    section,
    label,
):
    from agent_history.config import LiteLLM

    monkeypatch.delenv("AGENT_HISTORY_LITELLM_DSN", raising=False)
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    unknown = f"msg_{uuid.uuid4()}"
    transcript(tmp_path / "home", at, [(unknown, None, None)])
    values = {key: value.format(root=tmp_path) for key, value in section.items()}
    fill = litellm_usage.filler(LiteLLM(namespaces=("claude-proxied",), **values))
    logged = []
    stats = load.refresh(
        clean, sources={"claude-proxied": tmp_path / "home"}, textfile=None, log=logged.append, usage_fill=fill
    )
    assert stats.errors == 0 and stats.usage_fill_failed == label
    assert usage(clean, unknown) == (None,) * 6
    assert clean.execute("SELECT ok FROM ah.refresh_log ORDER BY refresh_id DESC LIMIT 1").fetchone() == (True,)
    assert json.loads(logged[0]) == {"usage_fill": "failed", "source": "litellm", "stage": label.split(".")[1]}
    assert "absent" not in "".join(logged) and "port=1" not in "".join(logged)


def test_an_unexpected_fill_error_never_fails_the_pass(clean, tmp_path):  # noqa: F811
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    transcript(tmp_path, at, [(f"msg_{uuid.uuid4()}", None, None)])

    def broken(_conn):
        raise RuntimeError("synthetic")

    stats = load.refresh(
        clean, sources={"claude-proxied": tmp_path}, textfile=None, log=lambda *_: None, usage_fill=broken
    )
    assert stats.errors == 0 and stats.usage_fill_failed == "usage_fill.error"


def test_the_periodic_index_line_reports_unknown_litellm_usage(clean, tmp_path, monkeypatch, capsys):  # noqa: F811
    from agent_history import cli

    monkeypatch.delenv("AGENT_HISTORY_LITELLM_DSN", raising=False)
    (tmp_path / "home").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'dsn = "{DSN}"\n[sources]\nclaude-proxied = "{tmp_path / "home"}"\n'
        f'[litellm]\ndsn_file = "{tmp_path / "absent.dsn"}"\nnamespaces = ["claude-proxied"]\n'
    )
    from pathlib import Path

    monkeypatch.setattr(
        cli,
        "Path",
        lambda value: tmp_path / "textfiles" if value == "/var/lib/alloy/textfile-agent-history" else Path(value),
    )

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["--config", str(config), "index", "--every", "60"])
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("agent-history:")]
    assert lines == ["agent-history: index succeeded (usage unknown: litellm.config)"]


@pytest.mark.parametrize("command", ["embed", "journal-sync"])
def test_other_workers_pass_with_an_unreadable_litellm_dsn(clean, tmp_path, monkeypatch, command):  # noqa: F811
    from agent_history import cli

    monkeypatch.delenv("AGENT_HISTORY_LITELLM_DSN", raising=False)
    monkeypatch.setenv("SYNTHETIC_EMBED_KEY", "synthetic")
    config = tmp_path / "config.toml"
    config.write_text(
        f'dsn = "{DSN}"\n'
        '[embedding]\nenabled = true\nbase_url = "http://127.0.0.1:1/v1"\napi_key_env = "SYNTHETIC_EMBED_KEY"\n'
        f'[litellm]\ndsn_file = "{tmp_path / "absent.dsn"}"\nnamespaces = ["claude-proxied"]\n'
    )
    # An earlier suite test may have recorded its own embedding model; this one only proves config
    # loading, so it steps around that record and puts it back for later tests.
    prior = clean.execute("SELECT value FROM ah.meta WHERE key = 'embedding_model'").fetchone()
    clean.execute("DELETE FROM ah.meta WHERE key = 'embedding_model'")
    clean.commit()
    extra = ["--source-db", str(tmp_path / "absent.db")] if command == "journal-sync" else []
    try:
        assert cli.main(["--config", str(config), command, *extra]) == 0
    finally:
        clean.execute("DELETE FROM ah.meta WHERE key = 'embedding_model'")
        if prior:
            clean.execute("INSERT INTO ah.meta VALUES ('embedding_model', %s)", prior)
        clean.commit()
