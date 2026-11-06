"""Metadata-only archive observations for sources admitted by the hot inventory."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import time
from pathlib import Path

CENSUS_INTERVAL = 3600
CENSUS_KEY = "cold_availability_census"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: Path) -> int:
    """Resolve every component relative to a pinned directory, without following symlinks."""
    path = path.absolute()
    fd = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class Observer:
    """Pin one namespace root; at most three directory descriptors are live per file probe."""

    def __init__(self):
        self.fd: int | None = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def root(self, path: Path) -> bool:
        self.close()
        try:
            self.fd = _open_directory(path)
            return True
        except OSError:
            return False

    def stable(self, path: Path) -> bool:
        if self.fd is None:
            return False
        try:
            current = _open_directory(path)
            try:
                before, after = os.fstat(self.fd), os.fstat(current)
                return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            finally:
                os.close(current)
        except OSError:
            return False

    def file(self, root: Path, relative: Path) -> bool | None:
        if self.fd is None or relative.is_absolute() or ".." in relative.parts:
            return None
        fd = os.dup(self.fd)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            return stat.S_ISREG(os.stat(relative.name, dir_fd=fd, follow_symlinks=False).st_mode)
        except OSError as exc:
            return False if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP} else None
        finally:
            os.close(fd)


def reconcile(conn, entries, existing, sources, cold_sources, log=print):
    """Batch observations without parser writes; never admit cold-only source files."""
    row = conn.execute("SELECT value FROM ah.meta WHERE key=%s", (CENSUS_KEY,)).fetchone()
    now = time.time()
    try:
        cached = json.loads(row[0]) if row else {}
        if not isinstance(cached, dict):
            cached = {}
    except (ValueError, TypeError):
        cached = {}
    # cold_sources is the complete configured map, whereas sources may be --source-filtered.
    # Remove retired mappings even when the last one is removed, but retain filtered namespaces.
    cached = {ns: value for ns, value in cached.items() if ns in cold_sources}
    groups = {}
    for rel, entry in entries.items():
        if entry.hot is not None and entry.namespace in sources:
            groups.setdefault(entry.namespace, []).append((rel, entry))
    changes = []
    unknown = 0
    for ns, group in groups.items():
        root = cold_sources.get(ns)
        observed = {}
        if root is not None:
            fingerprint = hashlib.sha256(json.dumps((str(sources[ns]), str(root))).encode()).hexdigest()
            previous = cached.get(ns, {})
            try:
                age = now - float(previous.get("checked_at", 0))
                census = previous.get("mapping") != fingerprint or not 0 <= age < CENSUS_INTERVAL
            except (ValueError, TypeError, AttributeError):
                census = True
            observer = Observer()
            try:
                healthy = observer.root(root)
                if healthy:
                    for rel, entry in group:
                        prior = existing.get(rel)
                        if census or prior is None or prior["status"] != "indexed":
                            observed[rel] = observer.file(root, Path(rel).relative_to(ns))
                    healthy = observer.stable(root)
                if not healthy:
                    # A disappeared or replaced root invalidates every observation from this pass.
                    observed.clear()
                    unknown += 1
                else:
                    unknown += sum(value is None for value in observed.values())
                    if census:
                        cached[ns] = {"mapping": fingerprint, "checked_at": now}
            finally:
                observer.close()
        for rel, entry in group:
            prior = existing.get(rel)
            available = bool(prior and prior["available_cold"])
            if root is None:
                available = False
            elif observed.get(rel) is not None:
                available = observed[rel]
            entry.cold = root / Path(rel).relative_to(ns) if available and root is not None else None
            if prior is not None and prior["available_cold"] != available:
                changes.append((prior["id"], available))
    # A first census can update the whole catalogue. Avoid per-file parser transactions and reads.
    for start in range(0, len(changes), 512):
        batch = changes[start : start + 512]
        conn.execute(
            "UPDATE ah.source_file s SET available_cold=o.available "
            "FROM unnest(%s::bigint[], %s::boolean[]) AS o(id,available) WHERE s.id=o.id",
            ([item[0] for item in batch], [item[1] for item in batch]),
        )
    conn.execute(
        "INSERT INTO ah.meta VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=excluded.value "
        "WHERE ah.meta.value IS DISTINCT FROM excluded.value",
        (CENSUS_KEY, json.dumps(cached)),
    )
    conn.commit()
    for group in groups.values():
        for rel, entry in group:
            if rel in existing:
                existing[rel]["available_cold"] = bool(entry.cold)
    if unknown:
        log(json.dumps({"cold_availability": "unknown", "observations": unknown}))
