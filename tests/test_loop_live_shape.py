"""Producer JSONB shape at the pure projection and real collector boundaries."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from agent_history import loop_live
from agent_history.config import Config
from test_loop_live import AT, event, summary_fixture
from test_loop_live_pg import (
    DSN,
    clean as _clean_fixture,
    fake_infer,
    live_config as _config_fixture,
    psycopg,
    refresh,
    scratch_database,
    transcript as _transcript_fixture,
)

clean = _clean_fixture
live_config = _config_fixture
transcript = _transcript_fixture


@contextmanager
def summary_upstream(config, response):
    """Exercise the real worker HTTP path against a bounded local synthetic provider."""
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, body))
            result = fake_infer(config, "jev", {}) if self.path == "/jev" else response
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 5

        def serve():
            for _ in range(2):
                server.handle_request()

        worker = Thread(target=serve)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            yield replace(config, jev_url=base + "/jev", summary_url=base + "/summary"), captured
        finally:
            worker.join(timeout=12)
        assert not worker.is_alive()
    assert [path for path, _ in captured] == ["/jev", "/summary"]
    assert captured[1][1]["max_tokens"] == 2048
    assert captured[1][1]["reasoning_effort"] == "high"
    assert captured[1][1]["thinking"] == {"type": "enabled"}


def drain_summary(config, response):
    with summary_upstream(config, response) as (local_config, captured):
        with psycopg.connect(DSN, autocommit=True) as worker:
            assert loop_live.drain_one(worker, local_config)
            assert not loop_live.drain_one(worker, local_config)
    return captured


@pytest.mark.skipif(not scratch_database(DSN), reason="disposable agent_history_test database required")
def test_real_high_summary_success_applies_visible_text(clean, transcript, live_config, monkeypatch):
    config = replace(live_config, reasoning="high")
    monkeypatch.setattr(loop_live, "load_config", lambda: Config(loop_live=config))
    transcript["state"]("watch", "op=start what=gate 'deadline=2099-01-01T00:00:00Z'")
    uid, phase, _, error = refresh(clean, transcript)
    assert (phase, error) == ("gating", None)
    clean.commit()
    response = summary_fixture("visible")
    drain_summary(config, response)
    refresh(clean, transcript)
    assert clean.execute("SELECT live_phase,headline,summary,summary_error FROM ah.loops").fetchone() == (
        "gating",
        "Gate in progress",
        "The root is checking the candidate. The gate result is still pending.",
        None,
    )
    assert clean.execute(
        "SELECT response,error,final_summary FROM ah.loop_live_cache WHERE kind='summary'"
    ).fetchone() == (response, None, False)
    assert clean.execute("SELECT summary_generated_at IS NOT NULL,summary_model FROM ah.loops").fetchone() == (
        True,
        loop_live.SUMMARY_MODEL,
    )
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation WHERE launch_uid=%s", (uid,)).fetchone()[0] == 2
    assert clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0] == (
        loop_live.reservation("jev") + loop_live.reservation("summary")
    )


@pytest.mark.skipif(not scratch_database(DSN), reason="disposable agent_history_test database required")
@pytest.mark.parametrize("failure", ["length", "empty-stop", "timeout"])
def test_real_summary_failure_preserves_text_and_structural_phase_updates(
    clean, transcript, live_config, monkeypatch, failure
):
    config = replace(live_config, reasoning="high")
    monkeypatch.setattr(loop_live, "load_config", lambda: Config(loop_live=config))
    transcript["state"]("watch", "op=start what=gate 'deadline=2099-01-01T00:00:00Z'")
    uid = refresh(clean, transcript)[0]
    clean.commit()
    drain_summary(config, summary_fixture("visible"))
    refresh(clean, transcript)
    previous = clean.execute("SELECT headline,summary,summary_generated_at FROM ah.loops").fetchone()
    transcript["state"]("watch", "op=stop what=gate")
    transcript["state"]("dispatch", "lane=review agent=security-reviewer run=review")
    transcript["state"]("gate", "scope=composed sha=synthetic exit=1")
    refresh(clean, transcript)
    clean.commit()
    response = None
    expected_error = {"length": "incomplete_summary", "empty-stop": "invalid_summary_shape", "timeout": "timeout"}[
        failure
    ]
    if failure == "timeout":

        def fail(config, kind, state):
            if kind == "summary":
                raise TimeoutError("synthetic summary timeout")
            return fake_infer(config, kind, state)

        with psycopg.connect(DSN, autocommit=True) as worker:
            assert loop_live.drain_one(worker, config, fail)
            assert not loop_live.drain_one(worker, config, fail)
    else:
        response = summary_fixture("length" if failure == "length" else "visible")
        if failure == "empty-stop":
            response["choices"][0]["message"]["content"] = ""
        drain_summary(config, response)
    refresh(clean, transcript)
    assert clean.execute("SELECT live_phase,summary_error,last_gate->>'exit' FROM ah.loops").fetchone() == (
        "reviewing",
        expected_error,
        "1",
    )
    assert clean.execute("SELECT headline,summary,summary_generated_at FROM ah.loops").fetchone() == previous
    saved_response, saved_error, final_summary = clean.execute(
        "SELECT response,error,final_summary FROM ah.loop_live_cache WHERE kind='summary' AND error IS NOT NULL"
    ).fetchone()
    assert saved_response == response  # Full reasoning/usage survive even on truncation.
    assert loop_live._public_error(saved_error) == expected_error
    assert final_summary is False
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation WHERE launch_uid=%s", (uid,)).fetchone()[0] == 4
    charged = 2 * (loop_live.reservation("jev") + loop_live.reservation("summary"))
    assert clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0] == charged < Decimal("5")
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert not loop_live.drain_one(worker, config)
    refresh(clean, transcript)
    assert clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0] == charged


def test_projection_nulls_invalid_members_without_coercion():
    state = loop_live.project(
        [
            event("open"),
            event("dispatch", run="one", lane=42, task=False, title={"text": "invalid"}, agent=["reviewer"]),
            event("gate", sha=["synthetic"], scope=42, exit=True),
        ],
        AT,
    )
    assert state["active_lanes"] == [
        {"lane": None, "task": None, "title": None, "agent": None, "started_at": "2026-01-01T00:00:00.000000Z"}
    ]
    assert state["last_gate"] == {"sha": None, "scope": None, "exit": None, "at": "2026-01-01T00:00:00.000000Z"}


@pytest.mark.parametrize("value", ["x" * 1001, "bad\x00text", "\ud800", 42, True, [], {}, None])
def test_projection_invalid_lane_text_is_member_null(value):
    fields = {key: value for key in ("lane", "task", "title", "agent")}
    state = loop_live.project([event("dispatch", run="one", **fields)], AT)
    assert state["active_lanes"] == [{**dict.fromkeys(fields), "started_at": "2026-01-01T00:00:00.000000Z"}]


@pytest.mark.parametrize("value", ["", "界" * 1000])
def test_projection_valid_lane_text_keeps_exact_characters(value):
    fields = {key: value for key in ("lane", "task", "title", "agent")}
    state = loop_live.project([event("dispatch", run="one", **fields)], AT)
    assert state["active_lanes"][0] == {**fields, "started_at": "2026-01-01T00:00:00.000000Z"}


@pytest.mark.parametrize("exit_code", [None, True, False, "0", 0.0, -2147483649, 2147483648, [], {}])
def test_projection_invalid_gate_exit_is_null(exit_code):
    state = loop_live.project([event("gate", exit=exit_code)], AT)
    assert state["last_gate"]["exit"] is None


@pytest.mark.parametrize("exit_code", [-2147483648, 0, 2147483647])
def test_projection_int32_gate_exit_is_retained(exit_code):
    assert loop_live.project([event("gate", exit=exit_code)], AT)["last_gate"]["exit"] == exit_code


@pytest.mark.parametrize("value", [[], {}, 1, False, "bad\x00text", "\udfff"])
def test_projection_invalid_gate_strings_are_null(value):
    gate = loop_live.project([event("gate", sha=value, scope=value)], AT)["last_gate"]
    assert gate["sha"] is gate["scope"] is None


def test_projection_gate_strings_have_no_length_or_hash_format_limit():
    value = "界" * 2000
    gate = loop_live.project([event("gate", sha=value, scope="")], AT)["last_gate"]
    assert gate["sha"] == value and gate["scope"] == ""


def test_projection_absent_members_and_unobserved_containers_are_null():
    unknown = loop_live.project([], AT)
    assert unknown["active_lanes"] is unknown["last_gate"] is None
    opened = loop_live.project([event("open")], AT)
    assert opened["active_lanes"] == [] and opened["last_gate"] is None
    observed = loop_live.project([event("dispatch", run="one"), event("gate")], AT)
    assert observed["active_lanes"] == [
        {"lane": None, "task": None, "title": None, "agent": None, "started_at": "2026-01-01T00:00:00.000000Z"}
    ]
    assert observed["last_gate"] == {"sha": None, "scope": None, "exit": None, "at": "2026-01-01T00:00:00.000000Z"}


@pytest.mark.parametrize("count", [64, 65])
def test_projection_lane_container_limit_is_not_a_truncated_list(count):
    events = [event("dispatch", run=str(n), lane=str(n), agent="complex-worker") for n in range(count)]
    state = loop_live.project(events, AT)
    assert state["live_phase"] == "working"
    if count == 64:
        assert len(state["active_lanes"]) == 64
    else:
        assert state["active_lanes"] is None


def test_projection_unkeyed_dispatch_does_not_claim_known_empty_lanes():
    state = loop_live.project([event("open"), event("dispatch", lane=[], run={})], AT)
    assert state["active_lanes"] is None
    assert state["live_phase"] is None


def test_projection_timestamps_are_utc_microseconds_not_local_spelling():
    at = datetime(2026, 1, 1, 2, 0, 0, 123456, timezone(timedelta(hours=2)))
    state = loop_live.project([{"ev": "dispatch", "run": "one", "at": at}, {"ev": "gate", "at": at}], AT)
    assert state["active_lanes"][0]["started_at"] == state["last_gate"]["at"] == "2026-01-01T00:00:00.123456Z"


@pytest.mark.parametrize("value", [None, "2026-01-01T00:00:00.1234567Z", datetime(2026, 1, 1), 0])
def test_unknown_or_noncanonical_internal_time_is_not_guessed(value):
    assert loop_live._live_timestamp(value) is None
