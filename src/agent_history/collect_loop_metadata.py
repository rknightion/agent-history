"""Collect charter/closeout identity metadata, never document bodies or receipt state.

A qualified closeout also yields two integers, the distinct task IDs under its `## Landed` and
`## Parked` headings. The body is counted in memory while it is hashed and then discarded; only
the integers leave this module.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .collect_git import GIT_ENV, parse_remote
from .loop_identity_v3 import CLOSEOUT_HEADER

COLS = [
    ("machine", "text"),
    ("path", "text"),
    ("kind", "text"),
    ("sha256", "text"),
    ("line1", "text"),
    ("file_mtime", "timestamptz"),
    ("repo_origin", "text"),
    ("landed_count", "integer"),
    ("parked_count", "integer"),
]
KEY = ["machine", "kind", "path"]
SOURCE_NAME = re.compile(r"(charter|closeout)-[\w.-]*-loop[0-9]+\.md")
MAX_LINE1_BYTES = 4096
CHUNK_BYTES = 1024 * 1024
# Closeouts are a few kilobytes; a larger file is not counted (both counts NULL), so no
# unbounded body is ever held in memory.
MAX_COUNT_BYTES = 1024 * 1024
TASK_ID = re.compile(r"(?<![A-Za-z0-9_.-])[A-Z][A-Z0-9]*-[0-9]+(?:\.[0-9]+)*(?![A-Za-z0-9_-]|\.[0-9])")
HEADING2 = re.compile(r" {0,3}##(?!#)(?:[ \t]+(.*?))?[ \t]*")
FENCE = re.compile(r" {0,3}(```|~~~)")
# A counted entry is a list item or table row that opens with its task ID, optionally in bold or
# code; IDs elsewhere in an entry or in prose are references, not entries.
ENTRY = re.compile(r"\s*(?:[-*+]|[0-9]+[.)]|\|)\s*(?:\*\*|__|`)?(" + TASK_ID.pattern + ")")


def closeout_counts(body: str) -> tuple[int | None, int | None]:
    """Distinct task IDs opening an entry under the `## Landed` and `## Parked` level-2 headings.

    An entry is a list item or table row whose first token is the task ID. A section runs from its heading to the next level-2 heading. Headings match on text starting
    with Landed or Parked in any case, so trailing words are allowed. A missing section is None;
    a present section without IDs is 0. Repeated sections of one kind are counted together.
    Headings inside fenced code blocks are not headings.
    """
    found: dict[str, set[str] | None] = {"landed": None, "parked": None}
    current: str | None = None
    fence: str | None = None
    for line in body.splitlines():
        marker = FENCE.match(line)
        if marker and (fence is None or marker[1] == fence):
            fence = None if fence else marker[1]
        elif fence is None and (heading := HEADING2.fullmatch(line)):
            title = (heading[1] or "").lower()
            current = next((kind for kind in found if title.startswith(kind)), None)
            if current is not None and found[current] is None:
                found[current] = set()
            continue
        if current is not None:
            if fence is None and (entry := ENTRY.match(line)):
                found[current].add(entry[1])
    return tuple(None if ids is None else len(ids) for ids in found.values())


class Changed(OSError):
    """The anchored source no longer describes the observed snapshot."""


def _version(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _origin(repo: Path) -> str | None:
    """Use only an explicitly configured origin URL, never a checkout basename."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            env=GIT_ENV,
            timeout=30,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    remote = parse_remote(result.stdout.strip()) if result.returncode == 0 else None
    return f"{remote[1]}/{remote[2]}" if remote else None


def _utc(ns: int) -> datetime:
    return datetime.fromtimestamp(ns // 1_000_000_000, timezone.utc) + timedelta(
        microseconds=ns % 1_000_000_000 // 1000
    )


def _parents(repo: Path, repo_fd: int, codex_fd: int, root: tuple, parent: tuple) -> None:
    """Check both anchored descriptors and their current public path bindings."""
    if (
        _version(os.fstat(repo_fd)) != root
        or _version(os.stat(repo, follow_symlinks=False)) != root
        or _version(os.fstat(codex_fd)) != parent
        or _version(os.stat("codex", dir_fd=repo_fd, follow_symlinks=False)) != parent
    ):
        raise Changed("source directory changed")


def _snapshot(
    repo: Path,
    repo_fd: int,
    codex_fd: int,
    root: tuple,
    parent: tuple,
    name: str,
    kind: str,
    machine: str,
    origin: str | None,
) -> dict[str, Any]:
    _parents(repo, repo_fd, codex_fd, root, parent)
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=codex_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular metadata source")
        if _version(os.stat(name, dir_fd=codex_fd, follow_symlinks=False)) != _version(before):
            raise Changed(name)
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        # Even an arbitrarily long first line cannot become retained content. Hash all bytes,
        # including the bounded prefix, through the same open descriptor.
        first = handle.readline(MAX_LINE1_BYTES)
        digest = hashlib.sha256(first)
        # A closeout's body is counted from the same descriptor and snapshot it is hashed from.
        body = bytearray() if kind == "closeout" and before.st_size <= MAX_COUNT_BYTES else None
        remaining = max(0, before.st_size - len(first))
        while remaining:
            chunk = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise Changed(name)
            digest.update(chunk)
            if body is not None:
                body += chunk
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
        bound = os.stat(name, dir_fd=codex_fd, follow_symlinks=False)
        public = os.stat(repo / "codex" / name, follow_symlinks=False)
        _parents(repo, repo_fd, codex_fd, root, parent)
        if any(_version(info) != _version(before) for info in (after, bound, public)):
            raise Changed(name)
    line1 = None
    if kind == "closeout":
        try:
            candidate = first.decode("utf-8").removesuffix("\n").removesuffix("\r")
        except UnicodeDecodeError:
            candidate = ""
        # A full-size prefix without a newline is incomplete, even if it resembles a header.
        if (len(first) < MAX_LINE1_BYTES or first.endswith(b"\n")) and CLOSEOUT_HEADER.fullmatch(candidate):
            line1 = candidate
    landed = parked = None
    if line1 is not None and body is not None:
        try:
            landed, parked = closeout_counts(body.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    del body
    return {
        "machine": machine,
        "path": str(repo / "codex" / name),
        "kind": kind,
        "sha256": digest.hexdigest(),
        "line1": line1,
        "file_mtime": _utc(before.st_mtime_ns),
        "repo_origin": origin,
        "landed_count": landed,
        "parked_count": parked,
    }


def metadata(repo: Path, machine: str, errors: list[dict[str, str]] | None = None) -> Iterator[dict[str, Any]]:
    """Yield stable metadata independently of legacy receipts and state-log availability.

    The configured checkout may be an alias. Its canonical root is the boundary; links below
    that boundary, special files and changed snapshots are refused and retried next run.
    """

    def report(error: Exception) -> None:
        if errors is not None:
            errors.append({"repo": repo.name, "step": "loop_run_metadata", "error": type(error).__name__})

    try:
        repo = repo.resolve(strict=True)
        repo_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root = _version(os.fstat(repo_fd))
            try:
                codex_fd = os.open("codex", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=repo_fd)
            except FileNotFoundError:
                return  # A configured repository need not have loop documents.
            try:
                parent = _version(os.fstat(codex_fd))
                origin = _origin(repo)
                for name in sorted(os.listdir(codex_fd)):
                    match = SOURCE_NAME.fullmatch(name)
                    if not match:
                        continue
                    try:
                        yield _snapshot(repo, repo_fd, codex_fd, root, parent, name, match[1], machine, origin)
                    except OSError as error:
                        report(error)
            finally:
                os.close(codex_fd)
        finally:
            os.close(repo_fd)
    except (OSError, RuntimeError) as error:
        report(error)
