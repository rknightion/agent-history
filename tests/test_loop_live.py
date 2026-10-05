"""Synthetic root evidence, never state files or real transcripts."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent_history import loop_live
from agent_history.config import ConfigError, parse_config

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(ev, minutes=0, **fields):
    return {"ev": ev, "at": AT + timedelta(minutes=minutes), **fields}


def test_frozen_hybrid_rules_and_watch_heartbeat():
    dispatch = event("dispatch", lane="one", task="TASK-1", agent="complex-worker", run="one")
    review = event("dispatch", lane="two", task="TASK-2", agent="security-reviewer", run="two")
    assert loop_live.project([event("open")], AT)["live_phase"] == "preparing"
    assert loop_live.project([dispatch, review], AT)["live_phase"] == "working"
    assert loop_live.project([review], AT)["live_phase"] == "reviewing"
    assert loop_live.project([dispatch], AT + timedelta(minutes=61))["live_phase"] == "waiting"
    heartbeat = event("heartbeat", 60)
    assert loop_live.project([dispatch, heartbeat], AT + timedelta(minutes=61))["live_phase"] == "working"
    returned = event("return", 1, run="one")
    watch = event("watch", 2, op="start", what="CI proof", deadline=(AT + timedelta(minutes=30)).isoformat())
    assert loop_live.project([dispatch, returned, watch], AT + timedelta(minutes=20))["live_phase"] == "gating"
    assert loop_live.project([dispatch, returned, watch], AT + timedelta(minutes=31))["live_phase"] == "waiting"
    assert (
        loop_live.project(
            [dispatch, returned, watch, event("watch", 3, op="stop", what="CI proof")], AT + timedelta(minutes=20)
        )["live_phase"]
        == "waiting"
    )
    assert loop_live.project([dispatch, event("close", 1)], AT)["live_phase"] == "closing"


def test_hybrid_latest_note_probabilities_and_whole_phase_are_separate():
    events = [event("dispatch", agent="complex-worker", run="one"), event("return", 1, run="one")]
    state = loop_live.project(events, AT + timedelta(minutes=20))
    assert loop_live.hybrid_decide(state["phase_input"], {"root_watching_gate": 0.8}) == "gating"
    assert loop_live.hybrid_decide(state["phase_input"], {"closeout": 0.9}) == "closing"
    assert loop_live.hybrid_decide(state["phase_input"], {"root_implementing": 0.9}) == "working"
    assert loop_live.hybrid_decide(state["phase_input"], {"parked_waiting": 0.9}) == "waiting"


def test_unknown_is_not_zero_and_structured_bounds():
    assert loop_live.project([], AT)["live_phase"] is None
    assert loop_live.project([], AT)["tasks_admitted"] is None
    fields = loop_live.project([event("open"), event("judgement", text="x" * 800)], AT)
    assert fields["tasks_admitted"] == 0
    assert fields["last_judgement"] == "x" * 500
    assert fields["active_lanes"] == []
    assert fields["ops_state"] is None


def test_append_uses_successful_final_simple_command_and_exact_recorded_target():
    target = "/tmp/synthetic/codex/report-synthetic-loop1.md"
    command = "loop-state append codex/state-synthetic-loop1.jsonl judgement 'text=Fix the failing gate'"
    parsed = loop_live.append_event(command, "/tmp/synthetic", target, AT)
    assert parsed == event("judgement", text="Fix the failing gate")
    assert loop_live.append_event(command, "/tmp/other", target, AT) is None
    assert loop_live.append_event(command + "; true", "/tmp/synthetic", target, AT) is None
    assert loop_live.append_event("echo " + command, "/tmp/synthetic", target, AT) is None
    assert loop_live.append_event(command + " | true", "/tmp/synthetic", target, AT) is None
    assert loop_live.append_event("cd /tmp/other; " + command, "/tmp/synthetic", target, AT) is None
    absolute = command.replace("codex/state", "/tmp/synthetic/codex/state")
    assert loop_live.append_event("true; " + absolute, "/tmp/other", target, AT)["ev"] == "judgement"
    assert loop_live.append_event("true\n" + absolute, "/tmp/other", target, AT)["ev"] == "judgement"
    multiline = command.replace("Fix the failing gate", "First line\nSecond line")
    assert loop_live.append_event(multiline, "/tmp/synthetic", target, AT)["text"] == "First line\nSecond line"


def test_digest_is_compact_and_change_driven_without_redaction():
    events = [event("open"), event("judgement", text="Synthetic sensitive text stays verbatim.")]
    state = loop_live.project(events, AT)
    later = loop_live.project(events, AT + timedelta(minutes=1))
    assert loop_live.digest_key(state) == loop_live.digest_key(later)
    body = loop_live.digest(state)
    assert len(json.dumps(body).encode()) <= loop_live.DIGEST_BYTES
    assert "Synthetic sensitive text stays verbatim." in json.dumps(body)
    assert loop_live.digest_key(state) != loop_live.digest_key(loop_live.project(events, AT + timedelta(minutes=20)))


def test_heartbeat_is_activity_evidence_not_a_paid_change_in_the_same_quiet_bucket():
    events = [event("open"), event("dispatch", lane="one", task="TASK-1", agent="complex-worker", run="one")]
    before = loop_live.project(events, AT + timedelta(minutes=1))
    after = loop_live.project(events + [event("heartbeat", 5)], AT + timedelta(minutes=6))
    assert before["evidence_at"] != after["evidence_at"]
    assert before["phase_input"]["timing"]["quiet_for"] == after["phase_input"]["timing"]["quiet_for"]
    assert loop_live.digest_key(before) == loop_live.digest_key(after)


def test_closed_digest_does_not_pay_again_for_elapsed_quiet_buckets():
    closed = [event("open"), event("close", 1)]
    assert loop_live.digest_key(loop_live.project(closed, AT + timedelta(minutes=2))) == loop_live.digest_key(
        loop_live.project(closed, AT + timedelta(hours=2))
    )


def test_json_rejects_nonfinite_constants():
    assert loop_live._json('{"exit":NaN}') is None
    assert loop_live._json('{"exit":Infinity}') is None


def test_summary_validation_and_no_special_secret_scrubbing():
    summary = {"headline": "Synthetic sensitive text", "summary": "The root is working. A gate is still pending."}
    assert loop_live.validate_summary(summary) == summary
    for invalid in (
        {**summary, "headline": "x" * 121},
        {**summary, "summary": "One sentence."},
        {**summary, "summary": "x" * 601},
    ):
        with pytest.raises(ValueError):
            loop_live.validate_summary(invalid)


def test_destructive_proof_guard_checks_database_not_another_dsn_component():
    from test_loop_live_pg import scratch_database

    assert scratch_database("dbname=agent_history_test user=synthetic")
    assert not scratch_database("dbname=production user=agent_history_test")
    assert not scratch_database("not a valid DSN")
    assert not scratch_database("")


@pytest.mark.parametrize("shape", ["native", "cf-direct", "cf-run-record"])
def test_documented_jev_native_envelope_and_authoritative_returned_version(monkeypatch, shape):
    from agent_history.config import LoopLive
    from test_loop_live_pg import fake_infer

    state = loop_live.digest(loop_live.project([event("open")], AT))
    native = fake_infer(None, "jev", state)["result"]
    documented = native
    if shape == "cf-direct":
        documented = {"success": True, "result": native, "errors": [], "messages": []}
    elif shape == "cf-run-record":
        documented = {"success": True, "result": {"state": "Completed", "result": native}, "errors": [], "messages": []}
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, limit):
            return json.dumps(documented).encode()

    class Opener:
        def open(self, request, timeout):
            captured.append(json.loads(request.data))
            assert request.get_header("User-agent") == "agent-history-loop-live/1.0"
            assert timeout == 20
            return Response()

    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic")
    monkeypatch.setattr(loop_live.urllib.request, "build_opener", lambda *_: Opener())
    config = LoopLive(enabled=True, jev_url="https://example.invalid/jev", api_key_env="SYNTHETIC_LOOP_TOKEN")
    response = loop_live.request(config, "jev", state)
    assert captured == [
        {
            "input": {
                "state": state["phase_input"],
                "questions": {**loop_live.HYBRID_QUESTIONS, "phase": loop_live.WHOLE_PHASE_QUESTION},
            }
        }
    ]
    assert loop_live.jev_answers(response)[1] == "waiting"


@pytest.mark.parametrize("kind", ["jev", "summary"])
@pytest.mark.parametrize(
    "text", ["x" * 40000, "\u0001" * 8000, "\U00010348" * 8192], ids=["ascii", "escaped-control", "multibyte"]
)
def test_complete_wire_byte_limit_rejects_before_http(monkeypatch, text, kind):
    from agent_history.config import LoopLive

    state = loop_live.digest(loop_live.project([event("open")], AT))
    state["phase_input"]["latest_root_notes"] = [text]
    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic")
    reached_http = []

    def forbidden_http(*_):
        reached_http.append(True)
        raise AssertionError("oversized input reached HTTP")

    monkeypatch.setattr(loop_live.urllib.request, "build_opener", forbidden_http)
    config = LoopLive(
        enabled=True,
        jev_url="https://example.invalid/jev",
        summary_url="https://example.invalid/compat",
        api_key_env="SYNTHETIC_LOOP_TOKEN",
        reasoning="off",
    )
    with pytest.raises(ValueError, match="request_too_large"):
        loop_live.request(config, kind, state)
    assert not reached_http


@pytest.mark.parametrize("kind", ["jev", "summary"])
def test_complete_wire_limit_is_exact_not_a_character_proxy(monkeypatch, kind):
    from agent_history.config import LoopLive

    state = loop_live.digest(loop_live.project([event("open")], AT))
    state["phase_input"]["latest_root_notes"] = [""]
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, limit):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            captured.append(request.data)
            return Response()

    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic")
    monkeypatch.setattr(loop_live.urllib.request, "build_opener", lambda *_: Opener())
    config = LoopLive(
        enabled=True,
        jev_url="https://example.invalid/jev",
        summary_url="https://example.invalid/compat",
        api_key_env="SYNTHETIC_LOOP_TOKEN",
        reasoning="off",
    )
    loop_live.request(config, kind, state)
    padding = loop_live.REQUEST_BYTES - len(captured[0])
    assert padding > 0
    state["phase_input"]["latest_root_notes"] = ["x" * padding]
    loop_live.request(config, kind, state)
    assert len(captured[-1]) == loop_live.REQUEST_BYTES
    state["phase_input"]["latest_root_notes"] = ["x" * (padding + 1)]
    with pytest.raises(ValueError, match="request_too_large"):
        loop_live.request(config, kind, state)
    assert len(captured) == 2


@pytest.mark.parametrize("model", [None, "jev-latest", "jev-1.12.0", "jev-1.13.1"])
def test_native_jev_rejects_unknown_alias_or_wrong_returned_version(model):
    from test_loop_live_pg import fake_infer

    native = fake_infer(None, "jev", {})["result"]
    native["model"] = model
    with pytest.raises(ValueError):
        loop_live.jev_answers(native)


@pytest.mark.parametrize("success", [False, None, 0, 1, "true"])
def test_cf_success_must_be_authoritative_true_even_with_valid_payload(success):
    from test_loop_live_pg import fake_infer

    native = fake_infer(None, "jev", {})["result"]
    # Retaining model/answers at the outer level must not bypass a failed envelope.
    payload = {**native, "success": success, "result": native}
    with pytest.raises(ValueError, match="invalid_jev_success"):
        loop_live.jev_answers(payload)


@pytest.mark.parametrize("shape", ["cf-direct", "cf-run-record"])
@pytest.mark.parametrize("model", [None, "jev-latest", "jev-1.12.0", "jev-1.13.1"])
def test_cf_envelopes_reject_unknown_or_wrong_model(shape, model):
    from test_loop_live_pg import fake_infer

    native = fake_infer(None, "jev", {})["result"]
    native["model"] = model
    result = native if shape == "cf-direct" else {"state": "Completed", "result": native}
    with pytest.raises(ValueError, match="invalid_jev_model_or_state"):
        loop_live.jev_answers({"success": True, "result": result})


@pytest.mark.parametrize("shape", ["native", "cf-direct"])
@pytest.mark.parametrize(
    "defect",
    [
        "missing-question",
        "wrong-noul-type",
        "wrong-choice-type",
        "wrong-choice",
        "wrong-phase-keys",
        "boolean-probability",
        "nonfinite-probability",
        "bad-total",
    ],
)
def test_cf_documented_answer_schema_is_not_replaced_by_loose_numeric_fields(defect, shape):
    from test_loop_live_pg import fake_infer

    native = fake_infer(None, "jev", {})["result"]
    answers = native["answers"]
    if defect == "missing-question":
        answers.pop("root_implementing")
    elif defect == "wrong-noul-type":
        answers["root_implementing"]["type"] = "score"
    elif defect == "wrong-choice-type":
        answers["phase"]["type"] = "noul"
    elif defect == "wrong-choice":
        answers["phase"]["choice"] = "unknown"
    elif defect == "wrong-phase-keys":
        answers["phase"]["probabilities"].pop("waiting")
    elif defect == "boolean-probability":
        answers["root_implementing"]["noul"] = True
    elif defect == "nonfinite-probability":
        answers["root_implementing"]["noul"] = float("nan")
    else:
        answers["phase"]["probabilities"]["working"] = 1.0
    with pytest.raises(ValueError):
        loop_live.jev_answers(native if shape == "native" else {"success": True, "result": native})


def test_verified_conservative_reservations_and_disabled_configuration():
    assert loop_live.reservation("jev") == Decimal(65536) * Decimal("0.042") / Decimal(1000000) * Decimal("1.05")
    assert loop_live.reservation("summary") == (
        Decimal(1048576) * Decimal("0.30") + Decimal(loop_live.OUTPUT_LIMIT) * Decimal("1.20")
    ) / Decimal(1000000) * Decimal("1.05")
    config = parse_config({"sources": {}, "loop_live": {"enabled": False}})
    assert config.loop_live.enabled is False
    with pytest.raises(ConfigError):
        parse_config({"sources": {}, "loop_live": {"enabled": True}})
    with pytest.raises(ConfigError):
        parse_config({"sources": {}, "loop_live": {"daily_cap": 50}})
