"""A pass that ends with file errors still writes its metrics instead of raising."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from agent_history import load
from test_litellm_usage_pg import transcript
from test_loop_identity_pg import clean, conn  # noqa: F401 - real disposable catalogue fixture

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif("agent_history_test" not in DSN, reason="scratch database not set")


def test_a_pass_with_file_errors_keeps_the_previous_success_time(clean, tmp_path):  # noqa: F811
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    real = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    transcript(tmp_path, at, [("msg_synthetic-metrics", "end_turn", real)])
    sources = {"claude-metrics": tmp_path}
    assert load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    # An indexed file under an older parser version is refused, which counts as a file error.
    clean.execute("UPDATE ah.source_file SET parser_version = 'older' WHERE namespace = 'claude-metrics'")
    clean.commit()
    prom = tmp_path / "metrics.prom"
    prom.write_text("agent_history_last_success_timestamp_seconds 1700000000\n")
    stats = load.refresh(clean, sources=sources, textfile=prom, log=lambda *_: None)
    assert stats.errors == 1
    text = prom.read_text()
    assert "agent_history_run_success 0" in text
    assert "agent_history_last_success_timestamp_seconds 1700000000" in text
