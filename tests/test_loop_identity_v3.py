"""V3 metadata and exact structured closeout evidence, without a local file lookup."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_history.loop_identity_v3 import Run, Write, project, structured_closeout

AT = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
CWD = "/tmp/synthetic-project"
CLOSEOUT = CWD + "/codex/closeout-synthetic-loop14.md"
CHARTER = CWD + "/codex/charter-synthetic-loop14.md"
DIGEST = "a" * 64
HEADER = f"# Closeout: project loop14 · Charter: {DIGEST}"
RUN = Run("root:launch", 1, 14, AT, CHARTER, CLOSEOUT)


def metadata(kind="closeout", **changes):
    row = dict(
        path=CLOSEOUT if kind == "closeout" else CHARTER,
        kind=kind,
        sha256="b" * 64 if kind == "closeout" else DIGEST,
        line1=HEADER if kind == "closeout" else None,
        file_mtime=AT + timedelta(minutes=1),
        repo_origin="example/project",
    )
    row.update(changes)
    return row


def check(rows=(), writes=(), **kwargs):
    return project(RUN, [RUN], rows, writes, **kwargs)


def test_collected_charter_and_closeout_identity_and_end():
    result = check([metadata("charter"), metadata()])
    assert result.fields == dict(repo="example/project", loop="loop14", goal_sha256=DIGEST)
    assert result.end == AT + timedelta(minutes=1)
    assert check([metadata("charter")]).end is None
    assert check([metadata("charter")]).fields == result.fields
    # The header's basename is never a repository identity, and no local path is opened.
    assert check([metadata(repo_origin=None)]).fields["repo"] is None
    assert check([metadata(repo_origin=None)]).end is None


@pytest.mark.parametrize("case", ["hash", "origin", "header", "missing_origin", "wrong_loop"])
def test_conflicting_machine_copies_do_not_finish(case):
    bad = metadata()
    if case == "hash":
        bad["sha256"] = "c" * 64
    elif case == "origin":
        bad["repo_origin"] = "example/other"
    elif case == "header":
        bad["line1"] = HEADER.replace(DIGEST, "d" * 64)
    elif case == "missing_origin":
        bad["repo_origin"] = None
    else:
        bad["line1"] = HEADER.replace("loop14", "loop15")
    assert check([metadata(), bad]).end is None
    assert check([metadata(), bad]).fields == dict(repo=None, loop=None, goal_sha256=None)


def test_charter_digest_disagreement_blocks_closeout_and_fallback():
    write = Write(AT, AT + timedelta(seconds=10), HEADER, "b" * 64)
    result = check([metadata("charter", sha256="c" * 64), metadata()], [write])
    assert result.end is None
    assert result.fields["goal_sha256"] is None


def test_unique_launch_windows_ties_and_late_lanes():
    next_run = Run("other:launch", 2, 14, AT + timedelta(minutes=2), CHARTER, CLOSEOUT)
    assert project(RUN, [RUN, next_run], [metadata(file_mtime=next_run.started)], []).end is None
    tied = Run("other:launch", 2, 14, AT, CHARTER, CLOSEOUT)
    assert project(RUN, [RUN, tied], [metadata()], []).end is None
    assert check([metadata()], lane_starts=[AT + timedelta(seconds=61)]).end is None
    assert check([metadata()], lane_starts=[AT + timedelta(seconds=60)]).end is not None
    assert check([metadata(file_mtime=AT - timedelta(seconds=1))]).end is None
    # A lane from the later launch cannot revoke an earlier window's closeout.
    assert project(RUN, [RUN, next_run], [metadata()], [], lane_starts=[next_run.started]).end is not None


def test_launch_manifest_and_explicit_captured_origin_fallback():
    write = Write(AT, AT + timedelta(seconds=10), HEADER, "b" * 64)
    assert check(writes=[write]).end is None  # header is not an origin
    result = check(writes=[write], origins=["example/project"])
    assert result.end == write.ended
    assert result.fields["repo"] == "example/project"
    assert check(writes=[write], origins=["example/project", "example/other"]).end is None
    assert check([metadata()], launch_digest="c" * 64).end is None
    # Collected origin is authoritative; a stale captured session URL cannot rename it.
    assert check([metadata()], origins=["example/other"]).fields["repo"] == "example/project"


@pytest.mark.parametrize("tool", ["Write", "functions.write", "apply_patch", "functions.apply_patch", "FileChange"])
def test_complete_exact_structured_write_or_patch(tool):
    if tool.endswith("write") or tool == "Write":
        raw = json.dumps({"file_path": CLOSEOUT, "content": HEADER + "\nPrivate body.\n"})
        body = HEADER + "\nPrivate body.\n"
    elif tool == "FileChange":
        body = HEADER + "\nPrivate body.\n"
        raw = json.dumps({CLOSEOUT: {"type": "add", "content": body}})
    else:
        raw = (
            "*** Begin Patch\n*** Add File: codex/closeout-synthetic-loop14.md\n+"
            + HEADER
            + "\n+Private body.\n*** End Patch"
        )
        body = HEADER + "\nPrivate body.\n"
    observed = structured_closeout(tool, raw, CLOSEOUT, CWD, AT, AT + timedelta(seconds=10))
    assert observed == Write(AT, AT + timedelta(seconds=10), HEADER, hashlib.sha256(body.encode()).hexdigest())
    assert check(writes=[observed], origins=["example/project"]).end == observed.ended
    assert not hasattr(observed, "content")


@pytest.mark.parametrize(
    "raw",
    [
        "*** Begin Patch\n*** Update File: codex/closeout-synthetic-loop14.md\n@@\n+" + HEADER + "\n*** End Patch",
        "*** Begin Patch\n*** Add File: codex/closeout-other-loop14.md\n+" + HEADER + "\n*** End Patch",
        "echo '" + HEADER + "' > " + CLOSEOUT,
        "*** Begin Patch\n*** Add File: codex/closeout-synthetic-loop14.md\n+" + HEADER,
    ],
)
def test_partial_shell_wrong_path_or_incomplete_patch_is_not_a_closeout(raw):
    observed = structured_closeout("apply_patch", raw, CLOSEOUT, CWD, AT, AT)
    assert observed is None or observed.line1 is None
    assert check(writes=[observed] if observed else [], origins=["example/project"]).end is None


def test_latest_complete_write_and_ambiguous_timestamps():
    first = Write(AT, AT + timedelta(seconds=10), HEADER, "b" * 64)
    second = Write(AT, AT + timedelta(seconds=20), HEADER.replace(DIGEST, "c" * 64), "d" * 64)
    assert check(writes=[first, second], origins=["example/project"]).fields["goal_sha256"] == "c" * 64
    tied = Write(AT, first.ended, second.line1, second.sha256)
    assert check(writes=[first, tied], origins=["example/project"]).end is None
    # A tool dispatched before launch, or finishing outside its window, cannot finish it.
    assert (
        check(writes=[Write(AT - timedelta(seconds=1), first.ended, HEADER, "b" * 64)], origins=["example/project"]).end
        is None
    )
    malformed = structured_closeout(
        "write", json.dumps({"path": CLOSEOUT, "content": "not a header"}), CLOSEOUT, CWD, AT, second.ended
    )
    assert check(writes=[first, malformed], origins=["example/project"]).end is None
    partial = structured_closeout(
        "apply_patch",
        "*** Begin Patch\n*** Update File: " + CLOSEOUT + "\n@@\n+changed\n*** End Patch",
        CLOSEOUT,
        CWD,
        AT,
        second.ended,
    )
    assert check(writes=[first, partial], origins=["example/project"]).end is None


@pytest.mark.parametrize("tool", ["edit", "Edit", "MultiEdit"])
def test_string_replacement_edit_is_a_partial_write(tool):
    raw = json.dumps({"path" if tool == "edit" else "file_path": CLOSEOUT, "edits": []})
    edited = structured_closeout(tool, raw, CLOSEOUT, CWD, AT, AT + timedelta(seconds=1))
    assert edited == Write(AT, AT + timedelta(seconds=1), None, None)
    other = json.dumps({"path": CWD + "/codex/loop14/DECISIONS.md"})
    assert structured_closeout(tool, other, CLOSEOUT, CWD, AT, AT) is None


def test_collected_copy_after_a_partial_edit_finishes_the_run():
    first = Write(AT, AT + timedelta(seconds=10), HEADER, "c" * 64)
    edit_at = AT + timedelta(seconds=30, microseconds=580000)
    edited = Write(edit_at, edit_at + timedelta(milliseconds=50), None, None)
    # The collected copy (sha b...) differs from the first full write: the edit explains it.
    after = metadata(file_mtime=edit_at + timedelta(milliseconds=90))
    result = check([metadata("charter"), after], writes=[first, edited])
    assert result.fields == dict(repo="example/project", loop="loop14", goal_sha256=DIGEST)
    assert result.end == edited.ended
    # Whole-second mtimes from another machine still count as saved at or after the edit.
    truncated = metadata(file_mtime=edit_at.replace(microsecond=0))
    assert check([metadata("charter"), truncated], writes=[first, edited]).end == truncated["file_mtime"]


@pytest.mark.parametrize("case", ["before", "none", "bad_header", "two_copies"])
def test_partial_edit_without_a_later_valid_copy_does_not_finish(case):
    first = Write(AT, AT + timedelta(seconds=10), HEADER, "b" * 64)
    edit_at = AT + timedelta(seconds=90)
    edited = Write(edit_at, edit_at + timedelta(seconds=1), None, None)
    rows = {
        "before": [metadata()],
        "none": [],
        "bad_header": [metadata(file_mtime=edit_at + timedelta(seconds=5), line1="# not a closeout")],
        "two_copies": [
            metadata(file_mtime=edit_at + timedelta(seconds=5)),
            metadata(file_mtime=edit_at + timedelta(seconds=6), sha256="e" * 64),
        ],
    }[case]
    assert check(rows, writes=[first, edited], origins=["example/project"]).end is None
