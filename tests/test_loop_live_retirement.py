"""Retirement applies to existing enabled deployments, not just new defaults."""

import os
import subprocess
import sys
from contextlib import nullcontext
from datetime import date, datetime, timezone
from types import SimpleNamespace

from agent_history import loop_live
from agent_history.config import LoopLive


def test_queued_work_cannot_be_claimed_or_paid(monkeypatch):
    mutations, providers = [], []
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class QueuedConnection:
        autocommit = True
        info = SimpleNamespace(transaction_status=loop_live.psycopg.pq.TransactionStatus.IDLE)

        def transaction(self):
            return nullcontext()

        def execute(self, sql, params=None):
            if sql.startswith("SELECT (clock_timestamp()"):
                row = (date(2026, 1, 1),)
            elif "SELECT j.launch_uid" in sql:
                row = ("retained-launch", "retained-digest", {}, False, at)
            elif sql.startswith("UPDATE ah.loop_live_job SET claimed_at"):
                mutations.append(sql)
                row = (at,)
            elif sql.startswith("SELECT response,error") or sql.startswith("SELECT 1 FROM"):
                row = None
            else:
                mutations.append(sql)
                row = (date(2026, 1, 1),)
            return SimpleNamespace(fetchone=lambda: row)

    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic")
    config = LoopLive(
        enabled=True,
        api_key_env="SYNTHETIC_LOOP_TOKEN",
        jev_url="https://example.invalid/jev",
        summary_url="https://example.invalid/summary",
        reasoning="off",
    )

    def infer(*args):
        providers.append(args)
        raise TimeoutError("synthetic provider edge")

    drained = loop_live.drain_one(QueuedConnection(), config, infer)
    assert drained is False, f"retired work was admitted: {len(mutations)} mutations, {len(providers)} provider calls"
    assert mutations == []
    assert providers == []


def test_retired_worker_entrypoints_do_not_load_config_or_connect(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("retired worker must not inspect config, DSN or credentials")

    monkeypatch.setattr(loop_live, "load_config", forbidden)
    monkeypatch.setattr(loop_live.psycopg, "connect", forbidden)
    loop_live.run_pass()
    assert loop_live.main([]) == 0
    assert loop_live.main(["--every", "1"]) == 0
    config = tmp_path / "enabled.toml"
    config.write_text(
        'dsn = "synthetic-unusable-dsn"\n[sources]\n[loop_live]\nenabled = true\n'
        'jev_url = "https://example.invalid/jev"\nsummary_url = "https://example.invalid/summary"\n'
        'api_key_env = "SYNTHETIC_LOOP_TOKEN"\nreasoning = "off"\n'
    )
    # The supported module invocation must exit promptly even when its scheduler asks to repeat.
    result = subprocess.run(
        [sys.executable, "-m", "agent_history.loop_live", "--every", "1"],
        env={**os.environ, "AGENT_HISTORY_CONFIG": str(config)},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
