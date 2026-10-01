"""Ingest commits of configured local checkouts into ah.git_commit and ah.git_commit_file.

`agent-history collect-git` reads [git] repos (local checkout paths) and [identities] from the config.
For each checkout it resolves the repo slug (host/owner/name) from the `origin` remote and, when
[identities] git_owners is set, skips a checkout whose host/owner is not listed. It records the
default branch's commits from the last [git] days: subject, stats, parents, the reverted sha and
whether the author email is one of [identities] owner_emails. Emails themselves are never stored.

The owner match is ah.git_commit.author_is_owner: "the author email is an owner identity".
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from .config import Config

REVERT = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV, timeout=timeout
    ).stdout


def repo_slug(remote: str | None) -> str | None:
    """host/owner/name from an https, ssh or scp-style remote URL."""
    if not remote:
        return None
    remote = remote.strip()
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", remote)
    if not m:
        return None
    host, path = m.group(1).lower(), m.group(2).strip("/")
    if path.count("/") < 1:
        return None
    return f"{host}/{path.lower()}"


def default_ref(repo: Path) -> str | None:
    for ref in ("refs/remotes/origin/HEAD", "HEAD"):
        try:
            return git(repo, "rev-parse", "--verify", "--quiet", ref).strip() and ref
        except subprocess.CalledProcessError:
            continue
    return None


def numstat(
    repo: Path, ref: str, days: int
) -> dict[str, tuple[int, int, int, list[tuple[str, str, int | None, int | None]]]]:
    out = git(repo, "log", ref, f"--since={days}.days.ago", "--format=%x1e%H", "--numstat", "--no-renames")
    stats: dict[str, tuple[int, int, int, list]] = {}
    for record in out.split("\x1e")[1:]:
        lines = [line for line in record.splitlines() if line.strip()]
        if not lines:
            continue
        sha, files = lines[0].strip(), []
        added = removed = 0
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a = None if parts[0] == "-" else int(parts[0])
            d = None if parts[1] == "-" else int(parts[1])
            added += a or 0
            removed += d or 0
            files.append((parts[2], "M", a, d))
        stats[sha] = (len(files), added, removed, files)
    return stats


def commits(repo: Path, ref: str, days: int, config: Config, slug: str, context: str) -> tuple[list[dict], list[dict]]:
    meta = git(repo, "log", ref, f"--since={days}.days.ago", "--format=%x1e%H%x1f%ct%x1f%ae%x1f%P%x1f%s%x1f%b")
    stats = numstat(repo, ref, days)
    rows, files = [], []
    for record in meta.split("\x1e")[1:]:
        fields = record.split("\x1f")
        if len(fields) < 6:
            continue
        sha, ct, email, parents, subject, body = fields[:6]
        count, added, removed, changed = stats.get(sha, (0, 0, 0, []))
        revert = REVERT.search(body)
        rows.append(
            {
                "repo_slug": slug,
                "sha": sha,
                "context": context,
                "committed_at": datetime.fromtimestamp(int(ct), tz=timezone.utc),
                "author_is_owner": config.identities.is_owner(email),
                "subject": subject[:1000],
                "parent_count": len(parents.split()),
                "files_changed": count,
                "insertions": added,
                "deletions": removed,
                "reverts_sha": revert.group(1) if revert else None,
                "on_default": True,
            }
        )
        for path, change, a, d in changed:
            files.append(
                {
                    "repo_slug": slug,
                    "sha": sha,
                    "path": path,
                    "change": change,
                    "old_path": None,
                    "insertions": a,
                    "deletions": d,
                }
            )
    return rows, files


GIT_COLUMNS = (
    "repo_slug",
    "sha",
    "context",
    "committed_at",
    "author_is_owner",
    "subject",
    "parent_count",
    "files_changed",
    "insertions",
    "deletions",
    "reverts_sha",
    "on_default",
)
FILE_COLUMNS = ("repo_slug", "sha", "path", "change", "old_path", "insertions", "deletions")


def collect(conn: psycopg.Connection, config: Config, context: str | None = None) -> dict[str, Any]:
    """Ingest every configured checkout; returns counts per outcome."""
    context = context or config.default_context
    result: dict[str, Any] = {"repos": 0, "skipped": [], "commits": 0, "files": 0}
    for repo in config.git_repos:
        if not (repo / ".git").exists():
            result["skipped"].append(f"{repo.name}: not a git checkout")
            continue
        try:
            remote = git(repo, "remote", "get-url", "origin").strip()
        except subprocess.CalledProcessError:
            remote = None
        slug = repo_slug(remote) or f"local/{repo.name.lower()}"
        owner = "/".join(slug.split("/")[:2])
        if config.identities.git_owners and owner not in config.identities.git_owners:
            result["skipped"].append(f"{repo.name}: owner not in [identities] git_owners")
            continue
        ref = default_ref(repo)
        if ref is None:
            result["skipped"].append(f"{repo.name}: no commits")
            continue
        rows, files = commits(repo, ref, config.git_days, config, slug, context)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO ah.git_commit ({', '.join(GIT_COLUMNS)}) VALUES ({', '.join(['%s'] * len(GIT_COLUMNS))}) "
                    "ON CONFLICT (repo_slug, sha) DO UPDATE SET author_is_owner = EXCLUDED.author_is_owner, "
                    "subject = EXCLUDED.subject, on_default = EXCLUDED.on_default, seen_at = now()",
                    [tuple(r[c] for c in GIT_COLUMNS) for r in rows],
                )
                cur.executemany(
                    f"INSERT INTO ah.git_commit_file ({', '.join(FILE_COLUMNS)}) "
                    f"VALUES ({', '.join(['%s'] * len(FILE_COLUMNS))}) ON CONFLICT (repo_slug, sha, path) DO NOTHING",
                    [tuple(f[c] for c in FILE_COLUMNS) for f in files],
                )
        result["repos"] += 1
        result["commits"] += len(rows)
        result["files"] += len(files)
    return result
