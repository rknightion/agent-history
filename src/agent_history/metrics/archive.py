"""Filesystem and receipt health for the hot and cold transcript archive."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from agent_history.config import MetricsLabels

from . import Family, Sample
from .catalogue import gauge

RECEIPT = re.compile(r"\d{8}T\d{6}\.\d+Z\.json\Z")
NAMESPACE = re.compile(r"(pi|claude|codex)-[a-z0-9-]{1,48}\Z")


def family(name: str, help_text: str, values: list[tuple[dict[str, str], float]]) -> Family:
    return Family(
        name, "gauge", help_text, tuple(Sample(tuple(sorted(labels.items())), value) for labels, value in values)
    )


def namespace_labels(namespace: str, trusted: MetricsLabels = MetricsLabels()) -> dict[str, str]:
    agent, profile = namespace.split("-", 1)
    machine = "shared"
    if profile.startswith("standalone-"):
        machine = profile.removeprefix("standalone-")
        profile = "standalone"
        if machine not in trusted.machines:
            machine = "other"
            namespace = agent + "-standalone"  # No untrusted host suffix in a label.
    return {"namespace": namespace, "agent": agent, "profile": profile, "machine": machine}


def walk_jsonl(root: Path | None, *, exclude_admin: bool = False) -> dict[str, tuple[int, int]]:
    files = {}
    if root is None or root.is_symlink() or not root.is_dir():
        return files
    for namespace in sorted(root.iterdir()):
        # Root-level JSONL files also contribute to archive totals, but not namespace labels.
        if namespace.suffix == ".jsonl" and not namespace.is_symlink() and namespace.is_file():
            try:
                stat = namespace.stat()
            except (FileNotFoundError, PermissionError):
                continue
            files[namespace.name] = (stat.st_size, stat.st_mtime_ns)
            continue
        if not NAMESPACE.fullmatch(namespace.name) or namespace.is_symlink() or not namespace.is_dir():
            continue
        for folder, dirs, names in os.walk(namespace, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not (Path(folder) / name).is_symlink()
                and (not exclude_admin or name not in {".versions", ".archive-receipts"})
            ]
            for name in names:
                path = Path(folder) / name
                if not name.endswith(".jsonl") or path.is_symlink():
                    continue
                try:
                    stat = path.stat(follow_symlinks=False)
                except (FileNotFoundError, PermissionError):
                    continue
                if path.is_file():
                    files[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return files


def nfs_mounted(path: Path | None) -> bool:
    if path is None:
        return False
    target = path.resolve()
    try:
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    candidates = []
    for line in mounts:
        left, right = line.split(" - ", 1)
        mount_point = Path(left.split()[4].replace("\\040", " "))
        if target == mount_point or mount_point in target.parents:
            candidates.append((len(str(mount_point)), right.split()[0]))
    return bool(candidates and max(candidates)[1].startswith("nfs"))


class ArchiveCollector:
    name = "archive"

    def __init__(
        self,
        hot: Path | None,
        cold: Path | None,
        incoming: Path | None,
        conflicts: Path | None,
        retention_days: int = 90,
        *,
        namespaces=(),
        labels: MetricsLabels = MetricsLabels(),
    ):
        self.paths = {"hot": hot, "cold": cold, "incoming": incoming, "conflicts": conflicts}
        self.retention_days = retention_days
        self.namespaces = tuple(n for n in namespaces if NAMESPACE.fullmatch(n))
        self.labels = labels
        self.section_health: dict[str, tuple[float, bool]] = {}

    def collect(self):
        result = []
        by_tier = {"hot": {}, "cold": {}}
        self.section_health = {}
        started = time.monotonic()
        try:
            storage, by_tier = self._storage()
            result.extend(storage)
            failed = False
        except Exception:
            failed = True
        self.section_health["storage"] = (time.monotonic() - started, failed)
        started = time.monotonic()
        try:
            result.extend(self._archive(by_tier))
            failed = False
        except Exception:
            failed = True
        self.section_health["archive"] = (time.monotonic() - started, failed)
        return tuple(result)

    def _storage(self):
        hot, cold = self.paths["hot"], self.paths["cold"]
        roots = {"hot": hot, "cold": cold}
        result = []
        root_available, filesystem_bytes, filesystem_inodes = [], [], []
        by_tier = {}
        for tier, root in roots.items():
            available = bool(root and root.is_dir() and not root.is_symlink())
            root_available.append(({"tier": tier}, int(available)))
            if available:
                stat = os.statvfs(root)
                for kind, value in {
                    "total": stat.f_blocks * stat.f_frsize,
                    "free": stat.f_bfree * stat.f_frsize,
                    "available": stat.f_bavail * stat.f_frsize,
                    "used": (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                }.items():
                    filesystem_bytes.append(({"tier": tier, "kind": kind}, value))
                for kind, value in {"total": stat.f_files, "free": stat.f_ffree}.items():
                    filesystem_inodes.append(({"tier": tier, "kind": kind}, value))
            by_tier[tier] = walk_jsonl(root, exclude_admin=tier == "cold")
        result.extend(
            (
                family(
                    "agent_sessions_storage_root_available",
                    "Whether the configured agent session storage root is an available real directory.",
                    root_available,
                ),
                family(
                    "agent_sessions_filesystem_bytes",
                    "Filesystem capacity for an agent session storage tier by byte kind.",
                    filesystem_bytes,
                ),
                family(
                    "agent_sessions_filesystem_inodes",
                    "Filesystem inode capacity for an agent session storage tier.",
                    filesystem_inodes,
                ),
            )
        )
        groups = {key: [] for key in ("files", "bytes", "oldest_mtime_seconds", "newest_mtime_seconds")}
        namespaces = sorted(
            set(self.namespaces)
            | {path.split("/", 1)[0] for files in by_tier.values() for path in files if "/" in path}
            | {
                p.name
                for root in roots.values()
                if root and root.is_dir()
                for p in root.iterdir()
                if p.is_dir() and not p.is_symlink() and NAMESPACE.fullmatch(p.name)
            }
        )[:64]  # Hard cap even if an untrusted mount grows arbitrary namespace directories.
        for tier, files in by_tier.items():
            for namespace in namespaces:
                values = [(size, mtime) for path, (size, mtime) in files.items() if path.startswith(namespace + "/")]
                labels = {"tier": tier, **namespace_labels(namespace, self.labels)}
                groups["files"].append((labels, len(values)))
                groups["bytes"].append((labels, sum(size for size, _ in values)))
                if values:
                    mtimes = [mtime / 1e9 for _, mtime in values]
                    groups["oldest_mtime_seconds"].append((labels, min(mtimes)))
                    groups["newest_mtime_seconds"].append((labels, max(mtimes)))
        for suffix, help_text in (
            ("files", "JSONL files in the main agent session storage tree."),
            ("bytes", "JSONL bytes in the main agent session storage tree."),
            ("oldest_mtime_seconds", "Oldest JSONL modification time in an agent session namespace."),
            ("newest_mtime_seconds", "Newest JSONL modification time in an agent session namespace."),
        ):
            merged = {}
            for labels, value in groups[suffix]:
                key = tuple(sorted(labels.items()))
                if key not in merged:
                    merged[key] = value
                elif suffix == "oldest_mtime_seconds":
                    merged[key] = min(merged[key], value)
                elif suffix == "newest_mtime_seconds":
                    merged[key] = max(merged[key], value)
                else:
                    merged[key] += value
            result.append(
                family("agent_sessions_storage_" + suffix, help_text, [(dict(k), v) for k, v in merged.items()])
            )
        hot_files, cold_files = by_tier["hot"], by_tier["cold"]
        pending = [
            size - cold_files.get(path, (0, 0))[0]
            for path, (size, _) in hot_files.items()
            if size > cold_files.get(path, (0, 0))[0]
        ]
        eligible = [
            size for size, mtime in hot_files.values() if mtime < (time.time() - self.retention_days * 86400) * 1e9
        ]
        result.extend(
            (
                gauge(
                    "agent_sessions_archive_pending_files",
                    "Hot JSONL files absent from or larger than the corresponding current cold copy.",
                    len(pending),
                ),
                gauge(
                    "agent_sessions_archive_pending_bytes",
                    "Hot JSONL bytes not yet represented in the corresponding current cold copy.",
                    sum(pending),
                ),
                gauge(
                    "agent_sessions_hot_retention_eligible_files",
                    "Hot JSONL files older than the configured retention period.",
                    len(eligible),
                ),
                gauge(
                    "agent_sessions_hot_retention_eligible_bytes",
                    "Hot JSONL bytes older than the configured retention period.",
                    sum(eligible),
                ),
                gauge(
                    "agent_sessions_cold_nfs_mounted",
                    "Whether the cold agent session root resolves to an NFS mount.",
                    int(nfs_mounted(cold)),
                ),
            )
        )
        return result, by_tier

    def _archive(self, by_tier):
        cold = self.paths["cold"]
        result = []
        receipt_root = cold / ".archive-receipts" if cold else None
        receipts = (
            sorted(p for p in receipt_root.glob("*.json") if p.is_file() and not p.is_symlink())
            if receipt_root and receipt_root.is_dir()
            else []
        )
        result.append(
            gauge("agent_sessions_archive_receipts", "Archive receipt files retained on the cold tier.", len(receipts))
        )
        archived = [p for p in receipts if RECEIPT.fullmatch(p.name)]
        if archived:
            latest = archived[-1]
            try:
                payload = json.loads(latest.read_text(encoding="utf-8"))
                receipt_files = int(payload.get("jsonl_files", 0))
                receipt_bytes = int(payload.get("jsonl_bytes", 0))
                receipt_mtime = latest.stat().st_mtime
            except (ValueError, AttributeError, TypeError, OSError):
                pass  # A malformed or concurrently removed receipt is not a collection failure.
            else:
                result.extend(
                    (
                        gauge(
                            "agent_sessions_archive_last_success_timestamp_seconds",
                            "Modification time of the latest successful archive receipt.",
                            receipt_mtime,
                        ),
                        gauge(
                            "agent_sessions_archive_receipt_jsonl_files",
                            "JSONL file count recorded by the latest archive receipt.",
                            receipt_files,
                        ),
                        gauge(
                            "agent_sessions_archive_receipt_jsonl_bytes",
                            "JSONL byte count recorded by the latest archive receipt.",
                            receipt_bytes,
                        ),
                    )
                )
        versions = cold / ".versions" if cold else None
        snapshots = version_files = version_bytes = 0
        newest = 0.0
        if versions and versions.is_dir():
            snapshots = sum(p.is_dir() and not p.is_symlink() for p in versions.iterdir())
            for folder, dirs, names in os.walk(versions, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
                for name in names:
                    path = Path(folder) / name
                    if not name.endswith(".jsonl") or path.is_symlink():
                        continue
                    try:
                        stat = path.stat(follow_symlinks=False)
                    except (FileNotFoundError, PermissionError):
                        continue
                    version_files += 1
                    version_bytes += stat.st_size
                    newest = max(newest, stat.st_mtime)
        result.extend(
            (
                gauge(
                    "agent_sessions_archive_version_snapshots",
                    "Version snapshot directories retained on the cold tier.",
                    snapshots,
                ),
                gauge(
                    "agent_sessions_archive_version_files",
                    "Versioned JSONL files retained on the cold tier.",
                    version_files,
                ),
                gauge(
                    "agent_sessions_archive_version_bytes",
                    "Versioned JSONL bytes retained on the cold tier.",
                    version_bytes,
                ),
            )
        )
        if newest:
            result.append(
                gauge(
                    "agent_sessions_archive_version_newest_mtime_seconds",
                    "Newest versioned JSONL modification time on the cold tier.",
                    newest,
                )
            )
        # Additional bounded deployment health beyond the private collector's families.
        available, counts, sizes = [], [], []
        for tier, root in self.paths.items():
            ok = bool(root and root.is_dir() and not root.is_symlink())
            available.append(({"tier": tier}, int(ok)))
            file_sizes = [size for size, _ in by_tier[tier].values()] if tier in by_tier else []
            if ok and tier not in by_tier:
                for folder, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = [n for n in dirs if not (Path(folder) / n).is_symlink()]
                    for n in names:
                        p = Path(folder) / n
                        if n.endswith(".jsonl") and p.is_file() and not p.is_symlink():
                            try:
                                file_sizes.append(p.stat().st_size)
                            except (FileNotFoundError, PermissionError):
                                continue
            counts.append(({"tier": tier}, len(file_sizes)))
            sizes.append(({"tier": tier}, sum(file_sizes)))
        all_receipts = (
            [p for p in receipt_root.iterdir() if p.is_file() and not p.is_symlink()]
            if receipt_root and receipt_root.is_dir()
            else []
        )
        receipt_mtimes = []
        for receipt in all_receipts:
            try:
                receipt_mtimes.append(receipt.stat().st_mtime)
            except OSError:
                continue
        newest_receipt = max(receipt_mtimes, default=0)
        result.extend(
            (
                family("agent_sessions_archive_available", "Archive tier is mounted.", available),
                family("agent_sessions_archive_files", "JSONL files in each archive tier.", counts),
                family("agent_sessions_archive_bytes", "JSONL bytes in each archive tier.", sizes),
                gauge(
                    "agent_sessions_archive_receipt_timestamp_seconds",
                    "Newest archive receipt timestamp.",
                    newest_receipt,
                ),
                gauge(
                    "agent_history_cold_tier_available",
                    "Cold archive has fresh receipts.",
                    int(bool(cold and cold.is_dir() and receipt_mtimes and time.time() - newest_receipt < 48 * 3600)),
                ),
            )
        )
        return tuple(result)
