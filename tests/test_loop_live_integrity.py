"""Append attribution challenges: bounded local shell data, never a state writer."""

import subprocess

import pytest

from agent_history import loop_live
from test_loop_live import AT, event
from test_loop_live_native import REPORT, append


def observed(command, output, successful=True):
    return loop_live.append_events(command, "/tmp/synthetic", REPORT, AT, output, successful=successful)


def shell_output(command):
    # These shell fixtures put apparent state commands only in skipped branches or cat's data.
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("ev", ["admit", "land", "gate", "close"])
@pytest.mark.parametrize("branch", ["false &&", "true ||"])
def test_skipped_append_cannot_use_a_later_unrelated_seq_receipt(ev, branch):
    command = f"{branch} {append(ev, 'task=TASK-1')}; printf 'seq=1\\n'"
    events = observed(command, shell_output(command))
    assert not any(e["ev"] == ev for e in events)
    state = loop_live.project([event("open")] + events, AT)
    if ev == "admit":
        assert state["tasks_admitted"] is None
    if ev == "land":
        assert state["tasks_landed"] is None
    if ev == "gate":
        assert state["last_gate"] is None
    assert not loop_live.project(events, AT)["close_recorded"]


@pytest.mark.parametrize("delimiter", ["E'OF'", "'E'OF", 'E"OF"', '"E"OF'])
def test_full_quoted_concatenated_heredoc_word_is_consumed_before_tokenisation(delimiter):
    command = (
        f"cat <<{delimiter}\nE\n"
        f"{append('admit', 'task=TASK-1', target='/tmp/synthetic/codex/state-synthetic-loop1.jsonl')}\n"
        "EOF\nprintf 'seq=1\\n'"
    )
    events = observed(command, shell_output(command))
    assert not any(e["ev"] == "admit" for e in events)
    assert loop_live.project([event("open")] + events, AT)["tasks_admitted"] is None


@pytest.mark.parametrize(
    "command",
    [
        "bash -c '" + append("admit", "task=TASK-1") + "'",
        "/bin/sh -c '" + append("land", "task=TASK-1") + "'",
        "env bash -c '" + append("admit", "task=TASK-1") + "'",
        "command " + append("admit", "task=TASK-1"),
        "timeout 1 " + append("admit", "task=TASK-1"),
        "$WRITER append codex/state-synthetic-loop1.jsonl admit task=TASK-1",
        "/tmp/synthetic/opaque-writer",
        "if true; then opaque_writer; fi",
    ],
)
def test_unsupported_execution_is_uncertain_not_an_exact_zero(command):
    events = observed(command, "seq=1\n")
    fields = loop_live.project([event("open")] + events, AT)
    assert fields["tasks_admitted"] is None
    assert fields["tasks_landed"] is None
    assert fields["parks_total"] is None
    assert fields["phase_input"]["park_events_so_far"] is None
    assert not any(e["ev"] in {"admit", "land", "close"} for e in events)


@pytest.mark.parametrize("producer", ["printf 'seq=1\\n'", "echo seq=1"])
def test_unconditional_append_failure_cannot_be_hidden_by_a_receipt_producer(producer):
    # Recorded shell success belongs to the last producer, not necessarily the earlier writer.
    events = observed(append("land", "task=TASK-1") + "; " + producer, "seq=1\n")
    assert not any(e["ev"] == "land" for e in events)
    assert loop_live.project([event("open")] + events, AT)["tasks_landed"] is None


@pytest.mark.parametrize(
    "barrier", ["exit 0", "exec true", "source /tmp/synthetic/opaque", "eval true", "unknown_command"]
)
def test_opaque_or_terminating_prefix_cannot_prove_a_later_append_ran(barrier):
    command = "printf 'seq=1\\n'; " + barrier + "; " + append("land", "task=TASK-1")
    events = observed(command, "seq=1\n")
    assert not any(e["ev"] == "land" for e in events)
    assert loop_live.project([event("open")] + events, AT)["tasks_landed"] is None


def test_successful_final_and_chain_proves_its_literal_writer_was_reached():
    events = observed("true && " + append("admit", "task=TASK-1"), "seq=1\n")
    assert [e["ev"] for e in events] == ["admit"]
    assert loop_live.project(events, AT)["tasks_admitted"] == 1


@pytest.mark.parametrize("matching_call", [True, False])
def test_only_causally_later_snapshot_resolves_equal_timestamp_uncertainty(matching_call):
    command = "python3 -c 'print(1)'; " + append(
        "judgement", "'text=Check the candidate.'", target="/tmp/synthetic/codex/state-synthetic-loop1.jsonl"
    )
    events = loop_live.append_events(command, "/tmp/synthetic", REPORT, AT, "1\nseq=1\n", call_uid="call-one")
    assert any(e["ev"] == "uncertain" for e in events)
    known = next(e for e in events if e["ev"] == "judgement")
    if not matching_call:
        known["_source_call"] = "call-two"
    state = loop_live.project([event("open")] + events, AT)
    assert state["last_judgement"] == ("Check the candidate." if matching_call else None)
    assert state["tasks_admitted"] is None  # latest snapshot never repairs a cumulative total


@pytest.mark.parametrize(
    "command,kwargs",
    [
        ("/bin/cat > /tmp/synthetic/codex/state-synthetic-loop1.jsonl", {}),
        ("/bin/cat /tmp/synthetic/source.py > /tmp/synthetic/codex/state-synthetic-loop1.jsonl", {}),
        ("/bin/cat $(opaque_writer)", {}),
        ("/bin/cat /tmp/synthetic/source.py; opaque_writer", {}),
        ("env cat /tmp/synthetic/source.py", {}),
        ("READER=/usr/bin/cat opaque_writer", {}),
        ("/tmp/synthetic/cat /tmp/synthetic/source.py", {}),
        ("python3 -c 'print(1)'", {}),
        ("git diff --output=/tmp/synthetic/codex/state-synthetic-loop1.jsonl", {}),
        ("git -c alias.read='!opaque_writer' read", {}),
        ("rg --pre opaque_writer phase /tmp/synthetic", {}),
        ("find /tmp/synthetic -exec opaque_writer ';'", {}),
        ("/bin/cat /tmp/synthetic/source.py", {"input_truncated": True}),
    ],
)
def test_read_lookalikes_and_incomplete_commands_still_poison_heartbeat_phase(command, kwargs):
    uncertainties = loop_live.append_events(command, "/tmp/synthetic", REPORT, AT, "synthetic output", **kwargs)
    state = loop_live.project([event("open"), event("heartbeat", 120)] + uncertainties, event("heartbeat", 121)["at"])
    assert state["live_phase"] is None
    assert state["tasks_admitted"] is None


@pytest.mark.parametrize("ev", ["dispatch", "return", "close", "watch"])
def test_unproven_state_writer_remains_phase_unknown_with_readers_and_heartbeat(ev):
    # The final reader's shell success cannot prove the earlier writer succeeded.
    command = append(ev, "lane=one") + "; /bin/cat /tmp/synthetic/source.py"
    uncertainties = observed(command, "synthetic output")
    assert any(e.get("for_ev") == ev for e in uncertainties)
    state = loop_live.project([event("open"), event("heartbeat", 120)] + uncertainties, event("heartbeat", 121)["at"])
    assert state["live_phase"] is None


def test_real_reader_shell_output_never_becomes_state_writer_proof():
    command = "/bin/pwd; /bin/ls -d /tmp; /usr/bin/wc -c /dev/null"
    uncertainties = observed(command, shell_output(command))
    state = loop_live.project([event("heartbeat")] + uncertainties, AT)
    assert state["live_phase"] == "preparing"
    assert state["tasks_admitted"] is None


@pytest.mark.parametrize("command", ["cat", "head", "tail", "wc", "ls", "pwd", "grep", "git rev-parse HEAD"])
def test_bare_reader_names_do_not_prove_executable_identity(command):
    state = loop_live.project([event("heartbeat")] + observed(command, "synthetic output"), AT)
    assert state["live_phase"] is None


def test_bare_reader_can_execute_a_shell_override():
    command = "cat() { printf 'synthetic writer ran\\n'; }; cat /dev/null"
    output = shell_output(command)
    assert output == "synthetic writer ran\n"
    assert loop_live.project([event("heartbeat")] + observed(command, output), AT)["live_phase"] is None


def test_unsourced_uncertainty_never_gets_read_only_phase_trust():
    state = loop_live.project([event("open"), event("heartbeat"), event("uncertain")], AT)
    assert state["live_phase"] is None


def test_echoed_append_mention_remains_data_not_an_opaque_executable_wrapper():
    events = observed("echo '" + append("admit", "task=TASK-1") + "'", "seq=1\n")
    assert events == []
    assert loop_live.project([event("open")] + events, AT)["tasks_admitted"] == 0
