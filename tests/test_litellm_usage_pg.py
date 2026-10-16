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
