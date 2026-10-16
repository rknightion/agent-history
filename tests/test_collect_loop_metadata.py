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
    expected = {"machine", "path", "kind", "sha256", "line1", "file_mtime", "repo_origin"}
    assert {name for name, _ in cm.COLS} == expected
    assert cm.KEY == ["machine", "kind", "path"]
    for row in rows:
        assert set(row) == expected
        assert row["repo_origin"] == "sample/project"
        assert row["file_mtime"].tzinfo is not None
        raw = charter if row["kind"] == "charter" else closeout
        assert row["sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["line1"] == (None if row["kind"] == "charter" else header)
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
