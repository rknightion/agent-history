"""Run-folder dialect (protocol v5): recognition and closeout evidence, without a local file lookup."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_history.loop_identity_run import (
    CloseoutWrite,
    authority_targets,
    closeout_path,
    closeout_write,
    document,
    project,
)

AT = datetime(2026, 11, 23, 9, tzinfo=timezone.utc)
CWD = "/tmp/synthetic-project"
RUN = "run-20261123-1"
AUTHORITY = f"{CWD}/codex/{RUN}/AUTHORITY.md"
CLOSEOUT = f"{CWD}/codex/{RUN}/CLOSEOUT.md"
AUTHORITY_BODY = f"# Authority: project {RUN}\n\nprotocol: v5\n\nScope.\n"
CLOSEOUT_HEADER = f"# Closeout: project {RUN}"
CLOSEOUT_BODY = CLOSEOUT_HEADER + "\nprotocol: v5\n\n## Landed\n- #1 A change\n\n## Parked\n"


def write(path, content):
    return json.dumps({"file_path": path, "content": content})


@pytest.mark.parametrize(
    ("tool", "raw"),
    [
        ("Write", write(AUTHORITY, AUTHORITY_BODY)),
        ("write", json.dumps({"path": f"codex/{RUN}/AUTHORITY.md", "content": AUTHORITY_BODY})),
        (
            "apply_patch",
            f"*** Begin Patch\n*** Add File: codex/{RUN}/AUTHORITY.md\n"
            + "".join("+" + line + "\n" for line in AUTHORITY_BODY.splitlines())
            + "*** End Patch",
        ),
        ("FileChange", json.dumps({AUTHORITY: {"type": "add", "content": AUTHORITY_BODY}})),
    ],
)
def test_a_complete_qualified_authority_write_starts_a_run(tool, raw):
    assert authority_targets(tool, raw, CWD) == [AUTHORITY]


@pytest.mark.parametrize(
    ("tool", "raw"),
    [
        # A string replacement cannot show byte zero.
        ("Edit", json.dumps({"file_path": AUTHORITY, "old_string": "a", "new_string": AUTHORITY_BODY})),
        # The header must name this folder's run, and the protocol must be v5 on the next non-empty line.
        ("Write", write(AUTHORITY, AUTHORITY_BODY.replace(f"project {RUN}", "project run-20261123-2"))),
        ("Write", write(AUTHORITY, AUTHORITY_BODY.replace("protocol: v5", "protocol: v4"))),
        ("Write", write(AUTHORITY, AUTHORITY_BODY.replace("\nprotocol: v5", "Preface.\nprotocol: v5"))),
        ("Write", write(AUTHORITY, AUTHORITY_BODY.replace("# Authority:", "# Closeout:"))),
        ("Write", write(AUTHORITY, " " + AUTHORITY_BODY)),
        # Only a dated run folder directly below codex/ is a run folder.
        ("Write", write(AUTHORITY.replace(RUN, "run-20261131-1"), AUTHORITY_BODY.replace(RUN, "run-20261131-1"))),
        ("Write", write(f"{CWD}/codex/archive/{RUN}/AUTHORITY.md", AUTHORITY_BODY)),
        ("Write", write(f"{CWD}/{RUN}/AUTHORITY.md", AUTHORITY_BODY)),
        ("Write", write(f"{CWD}/codex/{RUN}/../{RUN}/AUTHORITY.md", AUTHORITY_BODY)),
        # Shell text is never interpreted, and another file is not the authority.
        ("Bash", json.dumps({"command": f"cat > {AUTHORITY} <<EOF\n{AUTHORITY_BODY}EOF"})),
        ("Write", write(CLOSEOUT, AUTHORITY_BODY)),
    ],
)
def test_anything_else_is_not_a_run_start(tool, raw):
    assert authority_targets(tool, raw, CWD) == []


def test_one_input_creating_two_authorities_starts_no_run():
    other = AUTHORITY.replace(RUN, "run-20261123-2")
    two = {
        AUTHORITY: {"type": "add", "content": AUTHORITY_BODY},
        other: {"type": "add", "content": AUTHORITY_BODY.replace(RUN, "run-20261123-2")},
    }
    assert authority_targets("FileChange", json.dumps(two), CWD) == []
    patch = "*** Begin Patch\n"
    for path, change in two.items():
        patch += f"*** Add File: {path}\n" + "".join("+" + line + "\n" for line in change["content"].splitlines())
    assert authority_targets("apply_patch", patch + "*** End Patch", CWD) == []


def test_relative_paths_need_an_absolute_cwd():
    raw = json.dumps({"path": f"codex/{RUN}/AUTHORITY.md", "content": AUTHORITY_BODY})
    assert authority_targets("write", raw, None) == []
    assert document(AUTHORITY) == (CWD, RUN, "authority")
    assert closeout_path(AUTHORITY) == CLOSEOUT


def collected(**changes):
    row = dict(
        path=CLOSEOUT,
        kind="run_closeout",
        sha256="b" * 64,
        line1=CLOSEOUT_HEADER,
        file_mtime=AT + timedelta(hours=2),
        repo_origin="example/project",
        protocol="v5",
    )
    row.update(changes)
    return row


def authority_row(**changes):
    row = dict(
        path=AUTHORITY,
        kind="authority",
        sha256="a" * 64,
        line1=f"# Authority: project {RUN}",
        file_mtime=AT,
        repo_origin="example/project",
        protocol="v5",
    )
    row.update(changes)
    return row


def check(rows=(), writes=(), **kwargs):
    return project(AUTHORITY, AT, rows, writes, **kwargs)


def test_collected_closeout_ends_the_run_and_names_the_repository():
    result = check([authority_row(), collected()])
    assert (result.repo, result.end) == ("example/project", AT + timedelta(hours=2))
    assert check([authority_row()]).end is None
    assert check([authority_row()]).repo == "example/project"
    # Without collected documents a captured remote names the repository; a header never does.
    assert check(origins=["Example/Project"]).repo == "example/project"
    assert check().repo is None


@pytest.mark.parametrize(
    "bad",
    [
        {"line1": None},
        {"line1": CLOSEOUT_HEADER.replace(RUN, "run-20261123-2")},
        {"protocol": None},
        {"protocol": "v4"},
    ],
)
def test_unqualified_collected_closeout_does_not_end_the_run(bad):
    assert check([collected(**bad)]).end is None


@pytest.mark.parametrize("case", ["hash", "origin", "malformed_origin", "missing_hash"])
def test_conflicting_or_malformed_copies_fail_closed(case):
    other = {
        "hash": {"machine": "b", "sha256": "c" * 64},
        "origin": {"repo_origin": "example/other"},
        "malformed_origin": {"repo_origin": "not an origin"},
        "missing_hash": {"sha256": None},
    }[case]
    result = check([collected(), collected(**other)])
    assert result.end is None
    if case != "hash":
        assert result.repo is None


def test_closeout_outside_the_window_is_ignored():
    assert check([collected(file_mtime=AT - timedelta(seconds=1))]).end is None
    following = AT + timedelta(hours=1)
    bounded = check([collected()], following=following)
    assert (bounded.end, bounded.following) == (None, following)
    assert check([collected()], tied=True).end is None


def test_transcript_write_alone_ends_the_run():
    observed = closeout_write(
        "Write",
        write(CLOSEOUT, CLOSEOUT_BODY),
        CLOSEOUT,
        CWD,
        AT + timedelta(hours=3),
        AT + timedelta(hours=3, seconds=1),
    )
    assert observed.valid and observed.sha256
    assert check(writes=[observed]).end == observed.ended
    # The collected copy of the same bytes keeps the earliest evidence; different bytes fail closed.
    same = collected(sha256=observed.sha256, file_mtime=AT + timedelta(hours=3, seconds=2))
    assert check([same], [observed]).end == observed.ended
    assert check([collected()], [observed]).end is None


@pytest.mark.parametrize(
    "body",
    [
        CLOSEOUT_BODY.replace("protocol: v5", "protocol: v4"),
        CLOSEOUT_BODY.replace(RUN, "run-20261123-2"),
        CLOSEOUT_BODY.replace("# Closeout:", "# Authority:"),
    ],
)
def test_unqualified_closeout_write_does_not_end_the_run(body):
    observed = closeout_write(
        "Write", write(CLOSEOUT, body), CLOSEOUT, CWD, AT + timedelta(hours=1), AT + timedelta(hours=1)
    )
    assert observed is not None and not observed.valid
    assert check(writes=[observed]).end is None


def test_an_edit_needs_a_later_collected_copy():
    edit = closeout_write(
        "Edit",
        json.dumps({"file_path": CLOSEOUT, "old_string": "a", "new_string": "b"}),
        CLOSEOUT,
        CWD,
        AT + timedelta(hours=2, microseconds=500),
        AT + timedelta(hours=2, seconds=1),
    )
    assert edit == CloseoutWrite(edit.started, edit.ended, None, False)
    assert check(writes=[edit]).end is None
    assert check([collected(file_mtime=AT + timedelta(hours=1))], [edit]).end is None
    # A copy saved in the same whole second as the edit's start stands in for it.
    assert check([collected(file_mtime=AT + timedelta(hours=2))], [edit]).end == AT + timedelta(hours=2)


def test_latest_write_must_be_unique_and_other_paths_are_untouched():
    first = CloseoutWrite(AT + timedelta(hours=1), AT + timedelta(hours=2), "d" * 64, True)
    second = CloseoutWrite(AT + timedelta(hours=1, minutes=1), AT + timedelta(hours=2), "e" * 64, True)
    assert check(writes=[first, second]).end is None
    assert closeout_write("Write", write(AUTHORITY, CLOSEOUT_BODY), CLOSEOUT, CWD, AT, AT) is None
    assert closeout_write("Write", write(CLOSEOUT, CLOSEOUT_BODY), AUTHORITY, CWD, AT, AT) is None


@pytest.mark.parametrize(
    ("path", "queued"),
    [
        (AUTHORITY, True),
        (f"codex/{RUN}/AUTHORITY.md", True),  # a relative path, as the Codex parser keeps it
        (f"xcodex/{RUN}/AUTHORITY.md", False),
        (f"{CWD}/codex/{RUN}/PLAN.md", False),
        (f"{CWD}/codex/archive/AUTHORITY.md", False),
    ],
)
def test_migration_queues_roots_of_absolute_and_relative_authority_writes(path, queued):
    """The 053 backfill's path pattern, read from the migration (the same POSIX regex in Python)."""
    import re
    from pathlib import Path

    import agent_history

    sql = (Path(agent_history.__file__).parent / "sql" / "migrations" / "053_loop_protocol.sql").read_text()
    (pattern,) = re.findall(r"WHERE f\.path ~ '([^']+)'", sql)
    assert bool(re.search(pattern, path)) is queued
