"""Metadata collection crosses the filesystem and explicit git-origin boundary."""

import hashlib
import os
import subprocess
import pytest

from agent_history import collect_loop_metadata as cm


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "unrelated-checkout-name"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "https://example.org/sample/project.git"],
        check=True,
    )
    (root / "codex").mkdir()
    return root


def write(repo, name, data):
    path = repo / "codex" / name
    path.write_bytes(data)
    return path


def test_metadata_only_stream_hash_and_origin(repo):
    header = "# Closeout: sample/project loop12 · Charter: " + "a" * 64
    closeout = (header + "\n").encode() + b"private body\n" * 200_000
    charter = b"x" * (3 * 1024 * 1024)  # no newline: first-line retention must remain bounded
    write(repo, "closeout-project-loop12.md", closeout)
    write(repo, "charter-project-loop12.md", charter)
    write(repo, "closeout-project-loop12.md.tmp", b"not a source")
    rows = list(cm.metadata(repo, "test-machine"))
    assert len(rows) == 2
    expected = {
        "machine",
        "path",
        "kind",
        "sha256",
        "line1",
        "file_mtime",
        "repo_origin",
        "landed_count",
        "parked_count",
        "task_ids",
        "protocol",
    }
    assert {name for name, _ in cm.COLS} == expected
    assert cm.KEY == ["machine", "kind", "path"]
    for row in rows:
        assert set(row) == expected
        assert row["repo_origin"] == "sample/project"
        assert row["file_mtime"].tzinfo is not None
        raw = charter if row["kind"] == "charter" else closeout
        assert row["sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["line1"] == (None if row["kind"] == "charter" else header)
        # The oversize charter is not read for task IDs; a closeout never carries them.
        assert row["task_ids"] is None
    match = cm.CLOSEOUT_HEADER.fullmatch(header)
    assert match.groups() == ("sample/project", "loop12", "a" * 64)
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)
    assert all(row["repo_origin"] is None for row in cm.metadata(repo, "test-machine"))


@pytest.mark.parametrize(
    "header",
    [
        "# Closeout: project loop12 · Charter: " + "a" * 64,
        "# Closeout: sample/project loop12 · Charter: " + "A" * 64,
        "# Closeout: sample/project loop12 · Charter: " + "a" * 64 + " extra",
        "arbitrary private first line",
        "# Loop: sample/project loop12 · Goal: " + "a" * 64,
        "# Closeout: project loop12 · Charter: " + "a" * 64 + "x" * 5000,
    ],
)
def test_only_qualified_header_retained(repo, header):
    write(repo, "closeout-project-loop12.md", (header + "\nbody").encode())
    row = next(cm.metadata(repo, "test-machine"))
    valid = header == "# Closeout: project loop12 · Charter: " + "a" * 64
    assert row["line1"] == (header if valid else None)


@pytest.mark.parametrize("kind", ["file", "directory", "fifo"])
def test_refuses_links_and_special_files(repo, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "charter-project-loop12.md"
    target.write_bytes(b"outside")
    if kind == "directory":
        (repo / "codex").rmdir()
        (repo / "codex").symlink_to(outside, target_is_directory=True)
    elif kind == "file":
        (repo / "codex" / target.name).symlink_to(target)
    else:
        os.mkfifo(repo / "codex" / target.name)
    errors = []
    assert list(cm.metadata(repo, "test-machine", errors)) == []
    assert errors and all(e["step"] == "loop_run_metadata" for e in errors)


@pytest.mark.parametrize("race", ["rewrite", "replace", "symlink", "parent", "root"])
def test_rejects_changed_snapshots(repo, monkeypatch, race):
    path = write(repo, "charter-project-loop12.md", b"before")
    original = cm.hashlib.sha256

    def raced(*args, **kwargs):
        if race == "rewrite":
            path.write_bytes(b"after!")
        elif race == "replace":
            path.unlink()
            path.write_bytes(b"before")
        elif race == "symlink":
            replacement = repo / "outside.md"
            replacement.write_bytes(b"before")
            path.unlink()
            path.symlink_to(replacement)
        elif race == "parent":
            (repo / "codex").rename(repo / "old-codex")
            (repo / "codex").mkdir()
            write(repo, path.name, b"before")
        else:
            repo.rename(repo.with_name("old-root"))
            repo.mkdir()
            (repo / "codex").mkdir()
            write(repo, path.name, b"before")
        return original(*args, **kwargs)

    monkeypatch.setattr(cm.hashlib, "sha256", raced)
    errors = []
    assert list(cm.metadata(repo, "test-machine", errors)) == []
    assert errors and errors[0]["step"] == "loop_run_metadata"


def test_stream_reads_are_bounded(repo, monkeypatch):
    data = b"x" * (4 * 1024 * 1024 + 17)
    write(repo, "charter-project-loop12.md", data)
    original = cm.os.fdopen
    reads = []

    class Bounded:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def readline(self, size):
            assert 0 < size <= 4096
            return self.handle.readline(size)

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            reads.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(cm.os, "fdopen", lambda *a, **kw: Bounded(original(*a, **kw)))
    assert next(cm.metadata(repo, "test-machine"))["sha256"] == hashlib.sha256(data).hexdigest()
    # Reads are bounded by the initial snapshot size, not an ever-moving EOF.
    assert sum(reads) == len(data) - cm.MAX_LINE1_BYTES


HEADER = "# Closeout: sample/project loop12 · Charter: " + "a" * 64


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Bullets, with a duplicate, a subtask, a second ID inside an entry (a reference, not an
        # entry) and a mention outside both sections.
        (
            "Preamble ABC-0009.\n## Landed\n- ABC-0001 (first): 1a2b3c.\n- ABC-0002, ABC-0001 again.\n"
            "- XY2-0003.1.2 (subtask) and DEF-0004\n## Parked\n- ABC-0005: needs the owner.\n"
            "## For ratification\n- ABC-0006 is not landed or parked.\n## Notes\nABC-0007.\n",
            (3, 1),
        ),
        # A table and level-3 sub-headings stay inside their section; trailing heading text is allowed.
        (
            "## Landed (about 40 tasks)\n| Task | SHA |\n| --- | --- |\n| ABC-0001 (x) | `abc` |\n"
            "| ABC-0002 (y) | `def` |\n### Release\n- ABC-0003 released.\n## PARKED and waiting\n"
            "| ABC-0004 | owner |\n",
            (3, 1),
        ),
        # Missing sections are unknown; an empty one is a known zero.
        ("## Landed\nNothing shipped.\n## Notes\nABC-0001\n", (0, None)),
        ("## Notes\nABC-0001\n", (None, None)),
        ("## Parked\n\n## Landed\n- ABC-0001\n", (1, 0)),
        # Lower case, embedded and partial shapes do not open an entry; bold, code and numbered
        # entries do; repeated sections are combined.
        (
            "## Landed\n- abc-0001\n- xABC-0002\n- ABC-0003x\n- ABC-\n- -0004\n- ABC-0005-6\n"
            "- loop12-lane\n- (ABC-0010) in brackets\n## Notes\n## Landed again\n- **ABC-0008** bold\n"
            "* `ABC-0007` code\n1. ABC-0009 numbered\n- ABC-0007 again\n",
            (3, None),
        ),
        # A task named in prose is a reference, not a landed entry.
        ("## Landed\nNone. Only one commit changes source: ABC-0290 (repin).\n", (0, None)),
        # A heading inside a fenced block does not open or close a section, and a list item inside
        # one is code, not an entry.
        ("## Notes\n```\n## Landed\n- ABC-0001\n```\n## Landed\n```\n## Parked\n- ABC-0002\n```\n", (0, None)),
        ("## Landed\n- ABC-0001\n~~~\n- ABC-0002\n~~~\n- ABC-0003\n", (2, None)),
        # Level 1 and level 3 headings are not section boundaries.
        ("### Landed\nABC-0001\n# Landed\nABC-0002\n", (None, None)),
    ],
)
def test_closeout_counts(body, expected):
    assert cm.closeout_counts(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Issue-number rows in a list and in a table count, in bold and code too; the second
        # column of a row is a reference and a repeat is one entry.
        (
            "## Landed\n- #101 (first)\n- **#102** bold\n* `#103` code\n1. #104 numbered\n- #101 again\n"
            "| #105 (table) | see #106 |\n## Parked\n- #201: needs the owner\n| #202 | owner |\n",
            (5, 2),
        ),
        # Task IDs and issue numbers are counted together, as distinct entries.
        ("## Landed\n- ABC-0001\n- #1\n- #1 repeated\n## Parked\n- ABC-0002\n- #2\n", (2, 2)),
        # Prose, headings, links, longer digit runs and embedded hashes are not entries.
        (
            "## Landed\n#301 starts a line\n# 302\nNothing but #303 in prose.\n- see #304\n- [#305](u)\n"
            "- #12345678 too long\n- #306x\n- ##307\n- a#308\n- #309.5\n- #\n- #-1\n",
            (0, None),
        ),
        # A fenced block holds code, and a heading in another section ends the section.
        ("## Landed\n```\n- #401\n```\n- #402\n## Notes\n- #403\n", (1, None)),
    ],
)
def test_closeout_counts_issue_number_rows(body, expected):
    assert cm.closeout_counts(body) == expected


def test_charter_first_wave_ignores_issue_number_rows():
    assert cm.charter_task_ids("## First wave\n- #11 (row)\n- APP-0001\n| #12 | x |\n") == ["APP-0001"]


def test_collected_issue_number_counts_leave_only_as_integers(repo):
    body = "## Landed\n- #101 (confidential title)\n- #102\n## Parked\n- #103\n"
    write(repo, "closeout-project-loop12.md", f"{HEADER}\n{body}".encode())
    row = next(cm.metadata(repo, "test-machine"))
    assert (row["landed_count"], row["parked_count"]) == (2, 1)
    assert row["task_ids"] is None
    assert not any("confidential" in str(value) or "#10" in str(value) for value in row.values())


def test_collected_closeout_counts_only_leave_as_integers(repo):
    body = "## Landed\n- ABC-0001 (confidential title)\n- ABC-0002\n## Parked\n- ABC-0003\n"
    write(repo, "closeout-project-loop12.md", f"{HEADER}\n{body}".encode())
    write(repo, "charter-project-loop12.md", body.encode())
    rows = {row["kind"]: row for row in cm.metadata(repo, "test-machine")}
    assert (rows["closeout"]["landed_count"], rows["closeout"]["parked_count"]) == (2, 1)
    # Charters and unqualified closeouts are never counted.
    assert (rows["charter"]["landed_count"], rows["charter"]["parked_count"]) == (None, None)
    assert not any(
        "confidential" in str(value) or "ABC-" in str(value) for row in rows.values() for value in row.values()
    )
    write(repo, "closeout-project-loop12.md", f"# Closeout: draft\n{body}".encode())
    row = next(r for r in cm.metadata(repo, "test-machine") if r["kind"] == "closeout")
    assert (row["line1"], row["landed_count"], row["parked_count"]) == (None, None, None)


def test_oversize_or_undecodable_closeout_is_not_counted(repo):
    body = "## Landed\n- ABC-0001\n## Parked\n"
    padding = "x" * (cm.MAX_COUNT_BYTES + 1)
    write(repo, "closeout-project-loop12.md", f"{HEADER}\n{body}{padding}\n".encode())
    row = next(cm.metadata(repo, "test-machine"))
    assert row["line1"] == HEADER
    assert (row["landed_count"], row["parked_count"]) == (None, None)
    write(repo, "closeout-project-loop12.md", f"{HEADER}\n{body}".encode() + b"\xff\n")
    row = next(cm.metadata(repo, "test-machine"))
    assert (row["landed_count"], row["parked_count"]) == (None, None)
    write(repo, "closeout-project-loop12.md", f"{HEADER}\n{body}".encode())
    row = next(cm.metadata(repo, "test-machine"))
    assert (row["landed_count"], row["parked_count"]) == (1, 0)


CHARTER = """# Charter: sample/project loop12

## Goal

Land APP-0001 and the rest of the first wave.

## First wave

Ordered as dispatched; every row names its task first.

| Task | Scope | Route | Note |
|---|---|---|---|
| APP-2433 (short title) | paths | worker | depends on APP-0002 |
| APP-1895.19 (dotted id) | paths | worker | |
| **APP-0003** | paths | senior | bold |
| `APP-0004` | paths | worker | code |
| APP-2433 (repeated) | paths | worker | |
| not-an-id | paths | worker | |
| see APP-0005 | paths | worker | id later in the row |

- APP-0006 as a list item
1. APP-0007 numbered
Prose naming APP-0008 is a reference.

### Sub-heading stays inside the section

- APP-0009.1.2.3 (three dot parts)
- APP-0010.1.2.3.4 (four dot parts: too many)
- APP-1234567 (seven digits: too long)
- ABCDEFGHIJKLMNOPQ-1 (seventeen-character prefix: too long)
- app-0011 (lower case)

```
| APP-0012 | inside a fence |
```

## Next heading

| APP-0013 (after the section) | paths | worker | |
- APP-0014 outside the first wave

## First wave (continued)

- APP-0015 joins the earlier section
- APP-0003 repeated
"""


def test_charter_first_wave_ids_in_order():
    assert cm.charter_task_ids(CHARTER) == [
        "APP-2433",
        "APP-1895.19",
        "APP-0003",
        "APP-0004",
        "APP-0006",
        "APP-0007",
        "APP-0009.1.2.3",
        "APP-0015",
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Charter\n\n## Goal\n\n- APP-0001\n", []),
        ("## First wave\n\nNo rows yet.\n", []),
        ("### First wave\n- APP-0001\n", []),
        ("## first WAVE\n- APP-0001\n", ["APP-0001"]),
        ("## Notes\n```\n## First wave\n- APP-0001\n```\n- APP-0002\n", []),
    ],
)
def test_charter_task_ids_only_from_first_wave_rows(body, expected):
    assert cm.charter_task_ids(body) == expected


def test_charter_task_ids_are_capped_after_dedupe():
    rows = "".join(f"- APP-{n} (row)\n- APP-{n} (again)\n" for n in range(1, cm.MAX_CHARTER_TASK_IDS + 50))
    ids = cm.charter_task_ids("## First wave\n" + rows)
    assert len(ids) == cm.MAX_CHARTER_TASK_IDS == 200
    assert ids[:2] == ["APP-1", "APP-2"] and ids[-1] == f"APP-{cm.MAX_CHARTER_TASK_IDS}"


def test_collected_charter_keeps_only_first_wave_ids(repo):
    write(repo, "charter-project-loop12.md", CHARTER.encode())
    write(repo, "closeout-project-loop12.md", f"{HEADER}\n{CHARTER}".encode())
    rows = {row["kind"]: row for row in cm.metadata(repo, "test-machine")}
    assert rows["charter"]["task_ids"] == cm.charter_task_ids(CHARTER)
    assert rows["closeout"]["task_ids"] is None
    # Nothing else from the body leaves the module.
    assert not any("short title" in str(value) or "Goal" in str(value) for value in rows["charter"].values())
    write(repo, "charter-project-loop12.md", b"# Charter\n\nNo first wave.\n")
    assert next(cm.metadata(repo, "test-machine"))["task_ids"] == []
    write(repo, "charter-project-loop12.md", CHARTER.encode() + b"\xff\n")
    assert next(cm.metadata(repo, "test-machine"))["task_ids"] is None
    write(repo, "charter-project-loop12.md", CHARTER.encode() + b"x" * cm.MAX_COUNT_BYTES)
    assert next(cm.metadata(repo, "test-machine"))["task_ids"] is None


# ---- protocol line and the run-folder dialect --------------------------------------------------

RUN = "run-20261123-1"
AUTHORITY_HEADER = f"# Authority: sample/project {RUN}"
RUN_CLOSEOUT_HEADER = f"# Closeout: sample/project {RUN}"


def run_file(repo, folder, name, data):
    path = repo / "codex" / folder / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Title\nprotocol: v5\nbody\n", "v5"),
        ("# Title\n\n  \nprotocol: v4.1  \r\nbody\n", "v4.1"),
        ("# Title\r\nprotocol: v5\r\n", "v5"),
        # Anywhere but the first non-empty line after line 1 is not a declaration.
        ("# Title\nIntro.\nprotocol: v5\n", None),
        ("protocol: v5\n", None),
        # The token and its spelling are strict.
        ("# Title\nProtocol: v5\n", None),
        ("# Title\nprotocol:v5\n", None),
        ("# Title\nprotocol: V5\n", None),
        ("# Title\nprotocol: v5 (draft)\n", None),
        ("# Title\nprotocol: v05\n", None),
        ("# Title\nprotocol: five\n", None),
        ("# Title\nprotocol: v1234\n", None),
        ("# Title\n- protocol: v5\n", None),
        ("# Title\n", None),
    ],
)
def test_declared_protocol_line_is_strict(body, expected):
    from agent_history.loop_identity_run import declared_protocol

    assert declared_protocol(body) == expected


def test_charter_and_closeout_carry_their_declared_protocol(repo):
    write(repo, "charter-project-loop12.md", b"# Charter\nprotocol: v4\n## First wave\n- ABC-0001\n")
    write(repo, "closeout-project-loop12.md", (HEADER + "\n\n## Landed\n- ABC-0001\n").encode())
    rows = {row["kind"]: row for row in cm.metadata(repo, "test-machine")}
    assert (rows["charter"]["protocol"], rows["charter"]["task_ids"], rows["charter"]["line1"]) == (
        "v4",
        ["ABC-0001"],
        None,
    )
    # A closeout without the line declares nothing; its counts are unchanged.
    assert (rows["closeout"]["protocol"], rows["closeout"]["landed_count"]) == (None, 1)


def test_run_folder_documents_are_collected_with_counts_and_protocol(repo):
    authority = (AUTHORITY_HEADER + "\n\nprotocol: v5\n\nPrivate scope.\n").encode()
    closeout = (
        RUN_CLOSEOUT_HEADER
        + "\nprotocol: v5\n\n## Landed\n- #12 First change\n- #13 Second change\n\n## Parked\n- #14 Waits\n\n"
        "## Decisions needed\n- #15 is only a reference.\n\n## Spend by role\n| role | calls |\n"
    ).encode()
    run_file(repo, RUN, "AUTHORITY.md", authority)
    run_file(repo, RUN, "CLOSEOUT.md", closeout)
    run_file(repo, RUN, "PLAN.md", b"not collected")
    errors = []
    rows = sorted(cm.metadata(repo, "test-machine", errors), key=lambda row: row["kind"])
    assert errors == []
    assert [(r["kind"], r["path"], r["line1"], r["protocol"], r["landed_count"], r["parked_count"]) for r in rows] == [
        ("authority", str(repo / "codex" / RUN / "AUTHORITY.md"), AUTHORITY_HEADER, "v5", None, None),
        ("run_closeout", str(repo / "codex" / RUN / "CLOSEOUT.md"), RUN_CLOSEOUT_HEADER, "v5", 2, 1),
    ]
    assert [r["sha256"] for r in rows] == [hashlib.sha256(authority).hexdigest(), hashlib.sha256(closeout).hexdigest()]
    assert all(r["repo_origin"] == "sample/project" and r["task_ids"] is None for r in rows)


@pytest.mark.parametrize(
    "suffix",
    [
        "sample/project run-20261123-2",  # another run's name
        "sample/project run-20261123-1 extra",
        "sample/project loop12",
        "sample/project run-20261123-1 · Charter: " + "a" * 64,
        None,  # each document carries the other document's header
    ],
)
def test_run_folder_header_must_name_its_folder(repo, suffix):
    authority = f"# Authority: {suffix}" if suffix else RUN_CLOSEOUT_HEADER
    closeout = f"# Closeout: {suffix}" if suffix else AUTHORITY_HEADER
    run_file(repo, RUN, "AUTHORITY.md", (authority + "\nprotocol: v5\n").encode())
    run_file(repo, RUN, "CLOSEOUT.md", (closeout + "\nprotocol: v5\n## Landed\n- #1 x\n").encode())
    rows = list(cm.metadata(repo, "test-machine"))
    assert len(rows) == 2
    assert all(r["line1"] is None and r["landed_count"] is None and r["protocol"] == "v5" for r in rows)


@pytest.mark.parametrize(
    "folder", ["run-20261332-1", "run-20261123-0", "run-2026112-1", "run-20261123-01", "run-20261123-1x", "runs"]
)
def test_only_dated_run_folders_are_read(repo, folder):
    run_file(repo, folder, "AUTHORITY.md", (f"# Authority: sample/project {folder}\nprotocol: v5\n").encode())
    errors = []
    assert list(cm.metadata(repo, "test-machine", errors)) == []
    assert errors == []


def test_linked_run_folder_and_documents_are_refused(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AUTHORITY.md").write_bytes((AUTHORITY_HEADER + "\nprotocol: v5\n").encode())
    (repo / "codex" / RUN).symlink_to(outside, target_is_directory=True)
    errors = []
    assert list(cm.metadata(repo, "test-machine", errors)) == []
    assert errors and all(e["step"] == "loop_run_metadata" for e in errors)
    (repo / "codex" / RUN).unlink()
    (repo / "codex" / RUN).mkdir()
    (repo / "codex" / RUN / "AUTHORITY.md").symlink_to(outside / "AUTHORITY.md")
    errors = []
    assert list(cm.metadata(repo, "test-machine", errors)) == []
    assert errors


def test_run_folder_changed_during_read_is_rejected(repo, monkeypatch):
    run_file(repo, RUN, "AUTHORITY.md", (AUTHORITY_HEADER + "\nprotocol: v5\n").encode())
    original = cm.hashlib.sha256

    def raced(*args, **kwargs):
        folder = repo / "codex" / RUN
        folder.rename(repo / "codex" / "old-run")
        run_file(repo, RUN, "AUTHORITY.md", (AUTHORITY_HEADER + "\nprotocol: v5\n").encode())
        return original(*args, **kwargs)

    monkeypatch.setattr(cm.hashlib, "sha256", raced)
    errors = []
    assert list(cm.metadata(repo, "test-machine", errors)) == []
    assert errors and errors[0]["step"] == "loop_run_metadata"
