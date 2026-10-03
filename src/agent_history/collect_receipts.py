"""wave-notify receipt files of configured repositories, as metadata for ah.loop_receipt.

wave-notify writes `<report>.notified` beside a loop report after a successful, non-degraded
completion notification, and `<goal>.started` beside a goal file after the receiver accepted the
start. Only the exact receipt names are read (a `.tmp.notified` name is not a receipt). The
collector ships the receipt text and metadata about its target, never a report or goal body, and
treats every configured repository identically whatever context it belongs to.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .collect_git import GIT_ENV, Deadline, parse_remote

COLS = [
    ("machine", "text"),
    ("path", "text"),
    ("kind", "text"),
    ("content", "text"),
    ("receipt_mtime", "timestamptz"),
    ("target_exists", "boolean"),
    ("target_sha256", "text"),
    ("target_line1", "text"),
    ("repo_origin", "text"),
]
KEY = ["machine", "kind", "path"]
# (glob under codex/, suffix, kind): the target is the receipt path minus the suffix. The globs end
# in `.md.<suffix>`, so a `.tmp.notified` name is never a receipt.
SOURCES = (
    ("report-*.md.notified", ".notified", "notified"),
    ("goal-*.md.started", ".started", "started"),
)
MAX_RECEIPT_BYTES = 4096
MAX_LINE1_BYTES = 4096
# A report's first line. Only a line of this shape is stored as `target_line1`; the finish rule in
# loops.py matches the same pattern.
REPORT_HEADER = re.compile(r"# Loop: [A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)? (loop|wave)([0-9]+) · Goal: [0-9a-f]{64}")
_stat = os.stat  # the closing stat of a read; replaceable to exercise a concurrent rewrite


class Changed(OSError):
    """A file changed while it was being read."""


def _open_regular(path: Path):
    """Open a regular file for reading without blocking on a FIFO or device; anything else is an OSError."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path.name}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _utc(ns: int) -> datetime:
    """UTC time from integer nanoseconds, exact to the database's microsecond resolution."""
    return datetime.fromtimestamp(ns // 1_000_000_000, timezone.utc) + timedelta(
        microseconds=ns % 1_000_000_000 // 1000
    )


def _version(stat: Any) -> tuple:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _origin(repo: Path) -> str | None:
    """owner/repo from the origin remote URL, or None when absent or unparsable."""
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
    parsed = parse_remote(result.stdout.strip()) if result.returncode == 0 else None
    return f"{parsed[1]}/{parsed[2]}" if parsed else None


def _target(path: Path, want_line1: bool) -> tuple[bool, str | None, str | None]:
    """(exists, sha256, first line) of one file read through one descriptor.

    The hash and the first line come from the same bytes: a file that changes while it is read
    raises Changed rather than producing a hash and a header from different versions.
    """
    try:
        handle = _open_regular(path)
    except FileNotFoundError:
        return False, None, None
    with handle:
        before = os.fstat(handle.fileno())
        first = handle.readline(MAX_LINE1_BYTES)  # bounded; the hash still covers the whole file
        hasher = hashlib.sha256(first)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
        after = _stat(path)
    if _version(before) != _version(after):
        raise Changed(path.name)
    line1 = first.decode("utf-8", errors="replace").rstrip("\r\n") if want_line1 else None
    if line1 is not None and not REPORT_HEADER.fullmatch(line1):
        line1 = None
    return True, hasher.hexdigest(), line1


def _receipt(path: Path, kind: str, suffix: str, machine: str, origin: str | None) -> dict[str, Any]:
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        raw = handle.read(MAX_RECEIPT_BYTES + 1)
        after = _stat(path)
    if _version(before) != _version(after):
        raise Changed(path.name)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("oversized receipt")
    target = path.with_name(path.name.removesuffix(suffix))
    try:
        exists, digest, line1 = _target(target, kind == "notified")
    except Changed:
        raise
    except OSError:
        # Present but unreadable (permissions, a directory): the receipt itself is still evidence.
        # Unknown hash and header mean a digest receipt cannot be checked, so it will not finish a loop.
        exists, digest, line1 = True, None, None
    return {
        "machine": machine,
        "path": str(target.absolute()),
        "kind": kind,
        "content": raw.decode("utf-8"),
        "receipt_mtime": _utc(before.st_mtime_ns),
        "target_exists": exists,
        "target_sha256": digest,
        "target_line1": line1,
        "repo_origin": origin,
    }


def receipts(repo: Path, machine: str, errors: list[dict[str, str]] | None = None):
    """Yield one row per exact receipt of `repo`; a receipt that cannot be read is skipped.

    A skipped receipt is retried on the next run and reported in `errors`.
    """
    origin = None
    resolved = False
    for pattern, suffix, kind in SOURCES:
        for path in sorted((repo / "codex").glob(pattern)):
            if not resolved:
                origin, resolved = _origin(repo), True
            try:
                yield _receipt(path, kind, suffix, machine, origin)
            except (OSError, ValueError) as error:  # includes Changed and UnicodeDecodeError
                if errors is not None:
                    errors.append({"repo": repo.name, "step": "loop_receipt", "error": type(error).__name__})


def collect(collector, repositories, machine: str) -> None:
    """Collect the receipts of explicitly configured repositories, one transaction per repository."""
    for repo in repositories:
        collector.check_time()
        try:
            rows = []
            for row in receipts(Path(repo), machine, collector.errors):
                collector.check_time()
                rows.append(row)
            collector.write("loop_receipt", COLS, KEY, rows)
        except Deadline:
            raise
        except Exception as error:  # one repository must not stop the others
            collector.rollback()
            collector.errors.append({"repo": Path(repo).name, "step": "loop_receipt", "error": type(error).__name__})
