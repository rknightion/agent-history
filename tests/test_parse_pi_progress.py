"""Synthetic progress snapshots, not transcript captures. Counters are events, never tokens."""

import copy

from agent_history.model import FileContext, LinePos
from agent_history.parse_pi import PiParser

SID = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"
BASE_MS = 1_790_000_000_000
CTX = FileContext("/tmp/toy.jsonl", "pi-toy/sessions/toy/toy.jsonl", "pi-toy", "pi", "toy", None, "main")
POS = LinePos(0, 100, 1)


def progress(**changes):
    group = {"started": 0, "deltas": 0, "completed": 0, "firstAt": None, "lastAt": None}
    record = {
        "v": 1,
        "session": SID,
        "request": REQUEST,
        "turn": 1,
        "pid": 123,
        "provider": "toy",
        "model": "toy-model",
        "api": "openai-responses",
        "state": "active",
        "stopReason": None,
        "startedAt": BASE_MS,
        "endedAt": None,
        "writtenAt": BASE_MS + 5000,
        "ageMs": 5000,
        "providerResponses": 0,
        "firstResponseAt": None,
        "providerRequestId": None,
        "modelEvents": 0,
        "firstModelEventAt": None,
        "lastModelEventAt": None,
        "reasoning": copy.deepcopy(group),
        "text": copy.deepcopy(group),
        "toolCalls": copy.deepcopy(group),
        "providerEvents": None,
        "lastProviderEventAt": None,
        "keepalives": None,
        "lastKeepaliveAt": None,
    }
    record.update(changes)
    return record


def assistant(p=None, **metadata):
    message = {
        "role": "assistant",
        "model": "toy-model",
        "responseId": "toy-response",
        "stopReason": "stop",
        "timestamp": BASE_MS,
        "content": [],
        "usage": {"input": 1, "output": 0},
    }
    if p is not None:
        message["loopPiProgress"] = p
    message.update(metadata)
    return {"type": "message", "id": "answer", "timestamp": "2026-09-21T14:13:25Z", "message": message}


def parse(record):
    parser = PiParser(CTX, {})
    list(parser.line({"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"}, POS))
    return list(parser.line(record, POS))


def table(rows, name):
    return [row for row in rows if row.TABLE == name]


def test_progress_final_and_recovery_are_independently_recorded():
    p = progress(
        state="ended",
        endedAt=BASE_MS + 5000,
        stopReason="stop",
        reasoning={"started": 1, "deltas": 0, "completed": 1, "firstAt": BASE_MS + 1, "lastAt": BASE_MS + 2},
        modelEvents=2,
        firstModelEventAt=BASE_MS + 1,
        lastModelEventAt=BASE_MS + 2,
    )
    rows = parse(
        assistant(
            p,
            thinkingLevel="high",
            loopPiRecovery={
                "attemptId": "33333333-3333-4333-8333-333333333333",
                "code": "stream_incomplete",
                "status": 502,
                "disposition": "retry",
                "reason": "interrupted-local-response",
                "outputObserved": True,
                "requestPortable": True,
                "toolsExecuted": False,
            },
        )
    )
    result = table(rows, "request_progress")[0]
    assert result.request_uid == REQUEST
    assert result.reasoning_completed == 1 and result.reasoning_deltas == 0
    assert result.provider_events is None and result.keepalives is None
    assert result.response_id == "toy-response" and result.effort == "high"
    recovery = table(rows, "request_recovery")[0]
    assert recovery.attempt_uid != result.request_uid
    assert recovery.reason == "interrupted-local-response" and recovery.tools_executed is False
    assert recovery.episode_limit_ms is None
    assert table(rows, "llm_call")[0].reasoning is None


def test_old_and_unsupported_versions_have_no_progress():
    assert not table(parse(assistant()), "request_progress")
    for version in (2, True, "1", None):
        assert not table(parse(assistant(progress(v=version))), "request_progress")
    assert not table(parse(assistant(progress(session="different"))), "request_progress")


def test_invalid_fields_unknown_independently_and_no_content_projection():
    p = progress(
        modelEvents=True,
        providerEvents=-1,
        keepalives=2**63,
        providerRequestId="secret-shaped header value",
        ageMs="5000",
    )
    p["reasoning"]["completed"] = 2**53
    p["reasoning"]["text"] = "not telemetry"
    result = table(parse(assistant(p)), "request_progress")[0]
    assert result.model_events is None and result.provider_events is None and result.keepalives is None
    assert result.reasoning_completed is None and result.age_ms is None
    assert result.provider_request_id is None
    assert "not telemetry" not in str(result.columns())
    assert "secret-shaped" not in str(result.columns())


def test_timestamps_do_not_use_neighbour_or_snapshot_time_as_activity():
    p = progress(
        lastModelEventAt=BASE_MS + 6000, firstModelEventAt=True, lastKeepaliveAt=BASE_MS - 1, writtenAt=BASE_MS + 5000
    )
    result = table(parse(assistant(p)), "request_progress")[0]
    assert result.last_model_event_at is None and result.first_model_event_at is None
    assert result.last_keepalive_at is None
    assert result.written_at is not None
    assert not table(parse(assistant(progress(writtenAt=None))), "request_progress")


def test_recovery_allowlists_and_incident_no_inferred_request_join():
    rows = parse(
        assistant(
            loopPiRecovery={
                "attemptId": "invalid",
                "reason": "arbitrary text",
                "code": "arbitrary text",
                "disposition": "stop",
                "status": True,
                "outputObserved": "true",
                "toolsExecuted": False,
            }
        )
    )
    result = table(rows, "request_recovery")[0]
    assert result.reason is None and result.code is None and result.status is None
    assert result.output_observed is None and result.tools_executed is False
    rows = parse(
        {
            "type": "custom_message",
            "id": "incident",
            "customType": "loop-request-incident",
            "timestamp": "2026-09-21T14:13:25Z",
            "content": "toy notice",
            "details": {"kind": "wall-clock", "attempt": 2, "exhausted": True, "elapsedMs": 840001},
        }
    )
    result = table(rows, "request_incident")[0]
    assert result.timeout_source == "request-wall-clock-ceiling"
    assert result.follow_up_count == 2 and result.elapsed_ms == 840001
    assert result.request_uid is None and result.wall_clock_limit_ms is None and result.follow_up_limit is None
