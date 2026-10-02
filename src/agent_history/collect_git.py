"""Collect repository, CI, tracker, installed-feature and permission-log metadata.

Reads explicitly configured repositories and homes. Stores author identity flags, not emails,
and permission command verbs, not command text. Run once with --dry-run to inspect counts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .common import cmd_verb

HOME = Path.home()
ENV_FILE = Path(os.environ["AGENT_HISTORY_INGEST_ENV"]) if os.environ.get("AGENT_HISTORY_INGEST_ENV") else None
LOCK_FILE = HOME / ".local/state/agent-history/collect.lock"
RUNTIME_LIMIT_S = 540
LONDON = ZoneInfo("Europe/London")
CONTEXT = "default"
REPO_CONTEXTS = {}
ALLOWED_OWNERS = set()
GITHUB_OWNERS = []


def repo_context(slug: str) -> str:
    return REPO_CONTEXTS.get(slug.lower(), CONTEXT)


FIRST_RUN_DAYS = 180
RESCAN_DAYS = 14
CI_LIMIT = 100
TASK_DIRS = ("backlog/tasks", "backlog/completed", "backlog/archive/tasks")
OWNER_IDENTITIES = set()
HOMES = ()
HOME_NAMESPACES = {}
REPOSITORIES = []
MACHINE = None

REVERT = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
TASK_ID = re.compile(r"^([A-Za-z]+)-(\d+)((?:\.\d+)*)$")
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


class Deadline(Exception):
    pass


# --------------------------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------------------------


GITHUB_HOST = "github.com"
# Transports with a network authority. file:// and anything else names no remote host to own.
REMOTE_SCHEMES = frozenset({"https", "http", "ssh", "git", "git+ssh", "ssh+git"})
_HOST = r"(?P<host>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
_PATH = r"(?P<owner>[a-z0-9._-]+)/(?P<name>[a-z0-9._-]+)/?"
# The authority is matched whole: at most one userinfo, which cannot hold a character at which a
# URL client would end the authority ('/', '?', '#', a backslash), so the host is what git dials.
# ASCII matching: without it IGNORECASE folds U+212A KELVIN SIGN onto k.
_URL_REMOTE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*)://(?:[^@/?#\\\s\x00-\x1f\x7f]+@)?" + _HOST + r"(?::[0-9]{1,5})?/" + _PATH,
    re.ASCII | re.IGNORECASE,
)
_SCP_REMOTE = re.compile(
    r"(?:[a-z0-9._-]+@)?" + _HOST + ":" + _PATH, re.ASCII | re.IGNORECASE
)  # [user@]host:owner/name


def parse_remote(remote: str | None) -> tuple[str, str, str] | None:
    """(host, owner, name), lower-cased, from a git remote URL; None unless it is exactly that shape.

    Accepts scheme://[userinfo@]host[:port]/owner/name and scp-style [user@]host:owner/name, each
    with an optional .git suffix and trailing slash. Every character is accounted for: a query, a
    fragment, an encoded or empty path segment, a dot segment or a deeper path is not a repository
    this collector can attribute to an owner.
    """
    if not remote:
        return None
    url = remote.strip()
    m = _URL_REMOTE.fullmatch(url)
    if m:
        if m.group("scheme").lower() not in REMOTE_SCHEMES:
            return None
    else:
        m = _SCP_REMOTE.fullmatch(url)
    if not m:
        return None
    host, owner, name = (m.group(part).lower() for part in ("host", "owner", "name"))
    if name.endswith(".git"):
        name = name[:-4]
    if not name or {owner, name} & {".", ".."}:
        return None
    return host, owner, name


def repo_slug(remote: str | None) -> str | None:
    """host/owner/name from a git remote URL (scp-like, ssh://, https://), lower-cased; None if unparseable."""
    parsed = parse_remote(remote)
    return "/".join(parsed) if parsed else None


def slug_host(slug: str | None) -> str | None:
    """The host component of a host/owner/name slug."""
    return slug.split("/", 1)[0] if slug else None


def allowlisted(slug: str | None) -> bool:
    if not slug:
        return False
    parts = slug.split("/")
    if len(parts) != 3 or not all(parts):
        return False
    host, owner, _ = parts
    return (host, owner.lower()) in ALLOWED_OWNERS


def is_owner(email: str, extra: Iterable[str] = ()) -> bool:
    email = (email or "").strip().lower()
    return bool(email) and email in OWNER_IDENTITIES | {e.strip().lower() for e in extra if e}


def reverts_sha(body: str) -> str | None:
    m = REVERT.search(body or "")
    return m.group(1) if m else None


def parse_numstat(text: str) -> dict[str, list[int]]:
    """`git log --format=%x1e%H --numstat` -> {sha: [files, insertions, deletions]}; binary files count 0 lines.

    Split on newlines only: str.splitlines() also breaks on the \\x1e record marker.
    """
    stats: dict[str, list[int]] = {}
    current = None
    for line in text.split("\n"):
        if line.startswith("\x1e"):
            current = line[1:].strip()
            stats[current] = [0, 0, 0]
        elif current and line.count("\t") >= 2:
            ins, dele, _ = line.split("\t", 2)
            entry = stats[current]
            entry[0] += 1
            entry[1] += int(ins) if ins.isdigit() else 0
            entry[2] += int(dele) if dele.isdigit() else 0
    return stats


def parse_commit_files(repo_slug: str, text: str) -> list[dict[str, Any]]:
    """`git log --raw --numstat -z -M --diff-merges=first-parent --format=%x1e%H` -> git_commit_file rows.

    For each commit, git emits every changed file twice in the same order: first as a `--raw`
    entry (`:<oldmode> <newmode> <oldsha> <newsha> <status>\\0<path>\\0`, or `...<status>\\0<old>\\0<new>\\0`
    for a rename/copy `status` such as `R100`/`C100`), then as a `--numstat` entry (`<ins>\\t<del>\\t<path>\\0`,
    or `<ins>\\t<del>\\t\\0<old>\\0<new>\\0` for a rename/copy, `-\\t-\\t...` for a binary file). A raw entry
    always starts with `:`; a numstat entry never does, so the two blocks are told apart by that alone,
    and the raw entry's own status decides how many extra tokens its matching numstat entry consumes -
    never the numstat token's own shape, which a tab in a filename could otherwise misread.

    `--diff-merges=first-parent` makes a merge commit show its first-parent diff (one row per file,
    same as any other commit) instead of git's default of showing merge commits as empty; it changes
    nothing for a non-merge commit's diff.

    `-z` also changes how `%H` itself ends: instead of the usual blank-line separator, git terminates
    the format output with NUL, so a record is `<sha>\\0` (a commit with no diff entries; the next
    record's `\\x1e` follows straight after) or `<sha>\\0\\n<raw+numstat entries>` (one leading `\\n`
    before the diff, then no more newlines at all).
    """
    rows: list[dict[str, Any]] = []
    for record in text.split("\x1e")[1:]:
        nul = record.find("\0")
        if nul < 0:
            continue
        sha, rest = record[:nul], record[nul + 1 :]
        if rest.startswith("\n"):
            rest = rest[1:]
        tokens = rest.split("\0")
        if tokens and tokens[-1] == "":
            tokens.pop()
        i = 0
        entries: list[tuple[str, str | None, str]] = []
        while i < len(tokens) and tokens[i].startswith(":"):
            header = tokens[i]
            i += 1
            status = header.rsplit(" ", 1)[-1]
            change = status[:1] or "M"
            if change in ("R", "C"):
                old_path, path = tokens[i], tokens[i + 1]
                i += 2
            else:
                old_path, path = None, tokens[i]
                i += 1
            entries.append((change, old_path, path))
        for change, old_path, path in entries:
            ins_del = tokens[i].split("\t", 2)
            i += 1
            if change in ("R", "C"):
                i += 2  # the rename/copy numstat entry's old+new path tokens, unused (raw already has them)
            binary = ins_del[0] == "-" or ins_del[1] == "-"
            rows.append(
                {
                    "repo_slug": repo_slug,
                    "sha": sha,
                    "path": path,
                    "change": change,
                    "old_path": old_path,
                    "insertions": None if binary else int(ins_del[0]),
                    "deletions": None if binary else int(ins_del[1]),
                }
            )
    return rows


def _scalar(value: str) -> str | None:
    value = value.strip()
    if value in ("", "~", "null"):
        return None
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _inline_list(value: str) -> list[str]:
    inner = value.strip()[1:-1].strip()
    if not inner:
        return []
    items, buf, quote = [], "", None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    return [s for s in (_scalar(i) for i in items) if s is not None]


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    """The leading `---` YAML block of a Backlog.md task: flat scalars, inline/block lists, block scalars."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    out: dict[str, Any] = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            return out
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2).rstrip()
        if value.startswith("[") and value.endswith("]"):
            out[key] = _inline_list(value)
        elif value[:1] in (">", "|"):
            block = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                if lines[i].strip() == "---":
                    break
                block.append(lines[i].strip())
                i += 1
            out[key] = " ".join(b for b in block if b)
        elif value == "":
            items = []
            while i < len(lines) and re.match(r"^\s*-\s", lines[i] + " "):
                items.append(lines[i].strip()[1:].strip())
                i += 1
                while i < len(lines) and re.match(r"^\s{3,}\S", lines[i]) and not re.match(r"^\s*-\s", lines[i]):
                    items[-1] += " " + lines[i].strip()  # folded continuation of a list item
                    i += 1
            out[key] = [s for s in (_scalar(x) for x in items) if s is not None] if items else None
        else:
            out[key] = _scalar(value)
    return None  # unterminated


def london_ts(value: Any) -> datetime | None:
    """Backlog dates ('YYYY-MM-DD', 'YYYY-MM-DD HH:MM[:SS]', ISO with offset); naive = Europe/London."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=LONDON)
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text.replace(" +", "+").replace(" -", "-") if "%z" in fmt else text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=LONDON)
    return None


def zero_pad(config: dict[str, Any]) -> int | None:
    """Digits a Backlog.md tracker pads ids to (`zero_padded_ids`), or None when it does not pad."""
    raw = config.get("zero_padded_ids")
    try:
        n = int(str(raw).strip()) if raw is not None else 0
    except ValueError:
        return None
    return n if n > 0 else None


def parse_config(text: str) -> dict[str, Any]:
    """Top-level scalars of backlog/config.yml (no frontmatter fence)."""
    return parse_frontmatter("---\n" + text + "\n---") or {}


def task_key(raw_id: Any, pad: int | None) -> str | None:
    """Upper-case id with the main number padded to the tracker's width; subtask suffixes kept."""
    if raw_id is None:
        return None
    m = TASK_ID.match(str(raw_id).strip())
    if not m:
        return None
    prefix, num, rest = m.group(1).upper(), m.group(2), m.group(3)
    if pad:
        num = num.lstrip("0").zfill(pad) if num.strip("0") else "0".zfill(pad)
    return f"{prefix}-{num}{rest}"


def task_row(fm: dict[str, Any], pad: int | None, archived: bool) -> dict[str, Any] | None:
    key = task_key(fm.get("id"), pad)
    if not key:
        return None
    labels = fm.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    created = london_ts(fm.get("created_date"))
    updated = london_ts(fm.get("updated_date")) or created
    status = fm.get("status")
    return {
        "task_key": key,
        "title": fm.get("title"),
        "status": "Archived" if archived else (str(status) if status is not None else None),
        "priority": fm.get("priority"),
        "labels": list(labels or []),
        "project": fm.get("project") if isinstance(fm.get("project"), str) else None,
        "created_at": created,
        "updated_at": updated,
    }


def normalise_skill(name: str) -> str:
    """Skill names as transcripts record them: 'plugin:skill'; `plugin_x_y` style is left to the reader."""
    return name.strip().strip("/")


def plugin_skill_name(plugin_key: str, skill: str) -> str:
    """'<plugin>:<skill>' from an installed plugin key '<plugin>@<marketplace>'."""
    return f"{plugin_key.split('@', 1)[0]}:{skill}"


def home_namespace(home: str, hostname: str) -> str:
    """Transcript namespace a home writes: named homes by suffix, native homes by machine."""
    name = home.rsplit("/", 1)[-1].lstrip(".")
    agent, _, suffix = name.partition("-")
    if suffix:
        return f"{agent}-{suffix}"
    return HOME_NAMESPACES.get(home, f"{agent}-{CONTEXT}")


def permission_row(line: str) -> dict[str, Any] | None:
    """One permission-denied.jsonl line -> structure only (tool, command verb, reason); never the target text."""
    raw = line.rstrip("\n")
    if not raw.strip():
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    ts = d.get("ts")
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    tool = d.get("tool") if isinstance(d.get("tool"), str) else None
    mode = d.get("mode") if isinstance(d.get("mode"), str) else None
    return {
        "ts": when,
        "line_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "tool_name": tool,
        "cmd_verb": cmd_verb(d.get("target")) if tool == "Bash" else None,
        "reason": f"classifier:{mode}" if mode else "classifier",
        "is_subagent": bool(d.get("subagent")),
    }


# --------------------------------------------------------------------------------------------
# Local sources
# --------------------------------------------------------------------------------------------


def git(repo: Path, *args: str, timeout: int = 60, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV, timeout=timeout, errors="replace"
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} exit {result.returncode}")
    return result.stdout if result.returncode == 0 else ""


def repo_roots() -> list[Path]:
    return list(REPOSITORIES)


def default_branch(repo: Path) -> str | None:
    ref = git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if ref.startswith("origin/"):
        return ref[len("origin/") :]
    for name in ("main", "master"):
        if git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{name}", check=False).strip():
            return name
    return None


def classify_repo(repo: Path, forks: set[str] | None) -> tuple[dict[str, Any] | None, str | None]:
    """(info, None) for a repo to collect, else (None, skip reason)."""
    dotgit = repo / ".git"
    if not dotgit.exists():
        return None, "not_git"
    if dotgit.is_file():
        return None, "worktree"
    slug = repo_slug(git(repo, "remote", "get-url", "origin", check=False).strip())
    if not slug:
        return None, "no_origin"
    if not allowlisted(slug):
        return None, "not_allowlisted"
    if slug_host(slug) == GITHUB_HOST:
        if forks is None:
            return None, "fork_status_unknown"
        if slug in forks:
            return None, "fork"
    branch = default_branch(repo)
    if not branch:
        return None, "no_default_branch"
    # Read origin's default branch, not the local checkout: both Macs then describe the same
    # history, so a Mac that is behind (or ahead with unpushed commits) cannot flip on_default
    # or mark Backlog tasks removed. The fetch only updates remote-tracking refs.
    fetched = (
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", "origin", branch],
            capture_output=True,
            env=GIT_ENV,
            timeout=60,
        ).returncode
        == 0
    )
    remote = f"origin/{branch}"
    has_remote = bool(git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/{remote}", check=False).strip())
    emails = {git(repo, "config", "user.email", check=False).strip()}
    return {
        "path": repo,
        "slug": slug,
        "branch": branch,
        "ref": remote if has_remote else branch,
        "fetched": fetched and has_remote,
        "emails": emails,
    }, None


def off_branch(repo: Path, sha: str, ref: str) -> bool:
    """True only when this clone has `sha` and it is provably not an ancestor of `ref`."""
    if (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            env=GIT_ENV,
            timeout=30,
        ).returncode
        != 0
    ):
        return False
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, ref],
            capture_output=True,
            env=GIT_ENV,
            timeout=30,
        ).returncode
        == 1
    )


def github_forks() -> set[str] | None:
    """Slugs of allowlisted GitHub repos that are forks; None when gh cannot answer."""
    forks: set[str] = set()
    for owner in GITHUB_OWNERS:
        try:
            out = subprocess.run(
                ["gh", "repo", "list", owner, "--limit", "1000", "--json", "nameWithOwner,isFork"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        for r in json.loads(out.stdout or "[]"):
            if r.get("isFork"):
                forks.add(f"{GITHUB_HOST}/{r['nameWithOwner']}".lower())
    return forks


def git_commits(info: dict[str, Any], days: int) -> list[dict[str, Any]]:
    repo, branch = info["path"], info["ref"]
    since = f"--since={days}.days.ago"
    meta = git(repo, "log", branch, since, "--format=%x1e%H%x1f%ct%x1f%ae%x1f%P%x1f%s%x1f%b", timeout=120)
    stats = parse_numstat(git(repo, "log", branch, since, "--format=%x1e%H", "--numstat", timeout=300))
    rows = []
    for record in meta.split("\x1e")[1:]:
        fields = record.split("\x1f")
        if len(fields) < 6:
            continue
        sha, ct, email, parents, subject, body = fields[0], fields[1], fields[2], fields[3], fields[4], fields[5]
        files, ins, dele = stats.get(sha, [0, 0, 0])
        rows.append(
            {
                "repo_slug": info["slug"],
                "sha": sha,
                "context": repo_context(info["slug"]),
                "committed_at": datetime.fromtimestamp(int(ct), tz=timezone.utc),
                "author_is_owner": is_owner(email, info["emails"]),
                "subject": subject[:1000],
                "parent_count": len(parents.split()),
                "files_changed": files,
                "insertions": ins,
                "deletions": dele,
                "reverts_sha": reverts_sha(body),
                "on_default": True,
            }
        )
    return rows


def git_commit_files(info: dict[str, Any], days: int) -> list[dict[str, Any]]:
    """Per-file rows for every commit `git_commits` would list in the same window, one repo at a time."""
    repo, branch = info["path"], info["ref"]
    since = f"--since={days}.days.ago"
    out = git(
        repo,
        "log",
        branch,
        since,
        "--format=%x1e%H",
        "--raw",
        "--numstat",
        "-z",
        "-M",
        "--diff-merges=first-parent",
        timeout=300,
    )
    return parse_commit_files(info["slug"], out)


class CICollectionError(RuntimeError):
    """Sanitised process-edge failure, attributable without logging raw request details."""

    def __init__(self, reason: str, exit_code: int):
        super().__init__(reason)
        self.reason, self.exit_code = reason, exit_code


def ci_runs(slug: str) -> list[dict[str, Any]]:
    owner_name = slug.split("/", 1)[1]
    out = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "-R",
            owner_name,
            "--limit",
            str(CI_LIMIT),
            "--json",
            "databaseId,headSha,workflowName,event,status,conclusion,attempt,createdAt,updatedAt",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if out.returncode != 0:
        match = re.search(r"HTTP\s+(\d{3})\b", out.stderr)
        reason = f"http_{match.group(1)}" if match else "gh_failed"
        raise CICollectionError(reason, out.returncode)

    def ts(v: str | None) -> datetime | None:
        return datetime.fromisoformat(v.replace("Z", "+00:00")) if v and not v.startswith("0001") else None

    return [
        {
            "run_id": int(r["databaseId"]),
            "repo_slug": slug,
            "context": repo_context(slug),
            "head_sha": r["headSha"],
            "workflow": r.get("workflowName"),
            "event": r.get("event"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion") or None,
            "attempt": r.get("attempt"),
            "created_at": ts(r.get("createdAt")),
            "updated_at": ts(r.get("updatedAt")),
        }
        for r in json.loads(out.stdout or "[]")
    ]


def cat_files(repo: Path, specs: list[str]) -> dict[str, str]:
    """Blob text for many '<rev>:<path>' specs through one `git cat-file --batch`."""
    if not specs:
        return {}
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(specs) + "\n").encode(),
        capture_output=True,
        env=GIT_ENV,
        timeout=120,
    )
    data, pos, out = proc.stdout, 0, {}
    for spec in specs:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].decode(errors="replace")
        pos = nl + 1
        fields = header.split()
        if header.endswith(" missing") or len(fields) != 3 or not fields[2].isdigit():
            continue
        size = int(fields[2])
        out[spec] = data[pos : pos + size].decode("utf-8", errors="replace")
        pos += size + 1
    return out


def backlog(info: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], datetime | None]:
    """(task_prefix row, task rows, tip commit time) from the default-branch tip; (None, [], None) if no tracker."""
    repo, branch = info["path"], info["ref"]
    config_spec = f"{branch}:backlog/config.yml"
    config_text = cat_files(repo, [config_spec]).get(config_spec)
    if config_text is None:
        return None, [], None
    config = parse_config(config_text)
    prefix = str(config.get("task_prefix") or "task").upper()
    pad = zero_pad(config)
    paths = [
        p
        for p in git(repo, "ls-tree", "-r", "-z", "--name-only", branch, "--", *TASK_DIRS).split("\0")
        if p.endswith(".md")
    ]
    blobs = cat_files(repo, [f"{branch}:{p}" for p in paths])
    rank = {d: i for i, d in enumerate(TASK_DIRS)}
    chosen: dict[str, tuple[int, dict[str, Any]]] = {}
    for p in paths:
        fm = parse_frontmatter(blobs.get(f"{branch}:{p}", ""))
        if not fm:
            continue
        where = next(d for d in TASK_DIRS if p.startswith(d + "/"))
        row = task_row(fm, pad, archived=where == "backlog/archive/tasks")
        if not row:
            continue
        prev = chosen.get(row["task_key"])
        order = (row["updated_at"] or datetime.min.replace(tzinfo=timezone.utc), -rank[where])
        if prev is None or order > prev[0]:
            chosen[row["task_key"]] = (order, row)
    tasks = [dict(r, repo_slug=info["slug"], context=repo_context(info["slug"])) for _, r in chosen.values()]
    tip = datetime.fromtimestamp(int(git(repo, "log", "-1", "--format=%ct", branch).strip()), tz=timezone.utc)
    return (
        {"prefix": prefix, "repo_slug": info["slug"], "context": repo_context(info["slug"]), "zero_pad": pad},
        tasks,
        tip,
    )


def _skills_in(root: Path) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/SKILL.md")) if root.is_dir() else []


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def claude_features(home: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in _skills_in(home / "skills"):
        rows.append({"kind": "skill", "name": normalise_skill(name), "source": "user", "enabled": True})
    for synced in (home / "skills/synced").glob("*") if (home / "skills/synced").is_dir() else []:
        for name in _skills_in(synced):  # claude.ai-synced skills surface as anthropic-skills:<name>
            rows.append({"kind": "skill", "name": f"anthropic-skills:{name}", "source": "synced", "enabled": True})
    settings = _json(home / "settings.json") or {}
    enabled_map = settings.get("enabledPlugins") or {}
    installed = (_json(home / "plugins/installed_plugins.json") or {}).get("plugins") or {}
    for key in sorted(set(installed) | set(enabled_map)):
        enabled = bool(enabled_map.get(key, False))
        rows.append(
            {"kind": "plugin", "name": key, "source": key.split("@", 1)[1] if "@" in key else None, "enabled": enabled}
        )
        entries = installed.get(key) or []
        path = Path(entries[0].get("installPath", "")) if entries and isinstance(entries[0], dict) else None
        if not path or not path.is_dir():
            continue
        for skill in _skills_in(path / "skills"):
            rows.append({"kind": "skill", "name": plugin_skill_name(key, skill), "source": key, "enabled": enabled})
        for server in sorted(((_json(path / ".mcp.json") or {}).get("mcpServers") or {})):
            rows.append(
                {
                    "kind": "mcp_server",
                    "name": f"plugin:{key.split('@', 1)[0]}:{server}",
                    "source": key,
                    "enabled": enabled,
                }
            )
    denied = {d.get("serverName") for d in settings.get("deniedMcpServers") or [] if isinstance(d, dict)}
    servers: dict[str, str] = {}
    config = _json(home / ".claude.json") or {}
    for name in config.get("mcpServers") or {}:
        servers.setdefault(name, "user")
    for project in (config.get("projects") or {}).values():
        for name in (project or {}).get("mcpServers") or {}:
            servers.setdefault(name, "project")
    for name in _json(home / "mcp-servers.json") or {}:
        servers.setdefault(name, "mcp-servers.json")
    for name, source in sorted(servers.items()):
        rows.append({"kind": "mcp_server", "name": name, "source": source, "enabled": name not in denied})
    return rows


def _codex_plugin_dir(home: Path, key: str) -> Path | None:
    plugin, _, market = key.partition("@")
    base = home / "plugins/cache" / market / plugin
    versions = [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
    return max(versions, key=lambda p: p.stat().st_mtime) if versions else None


def codex_features(home: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in _skills_in(home / "skills"):
        rows.append({"kind": "skill", "name": normalise_skill(name), "source": "user", "enabled": True})
    for name in _skills_in(home / "skills/.system"):
        rows.append({"kind": "skill", "name": normalise_skill(name), "source": "system", "enabled": True})
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    for key, value in sorted((config.get("plugins") or {}).items()):
        enabled = bool((value or {}).get("enabled", True))
        rows.append(
            {"kind": "plugin", "name": key, "source": key.split("@", 1)[1] if "@" in key else None, "enabled": enabled}
        )
        path = _codex_plugin_dir(home, key)
        if path:
            for skill in _skills_in(path / "skills"):
                rows.append({"kind": "skill", "name": plugin_skill_name(key, skill), "source": key, "enabled": enabled})
    for name, value in sorted((config.get("mcp_servers") or {}).items()):
        rows.append(
            {
                "kind": "mcp_server",
                "name": name,
                "source": "config.toml",
                "enabled": bool((value or {}).get("enabled", True)),
            }
        )
    return rows


def features(home: Path) -> list[dict[str, Any]]:
    rows = claude_features(home) if home.name.startswith(".claude") else codex_features(home)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        seen.setdefault((r["kind"], r["name"]), r)  # first source wins on duplicates
    return list(seen.values())


def permission_rows(home: Path) -> list[dict[str, Any]]:
    path = home / "logs/permission-denied.jsonl"
    if not path.is_file():
        return []
    rows, seen = [], set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            row = permission_row(line)
            if row and row["line_hash"] not in seen:
                seen.add(row["line_hash"])
                rows.append(row)
    return rows


# --------------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------------


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines() if ENV_FILE else []:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


class UnsafeIngestRole(RuntimeError):
    """The hourly collector requires a dedicated, non-administrative ingest role."""


def _check_ingest_role(conn) -> None:
    # This is intentionally independent of the optional MCP dependency. Check the
    # authenticated identity as well as SET ROLE, using server facts, not DSN text.
    # Owning-role membership also permits SET ROLE even without INHERIT.
    with conn.transaction():
        safe = conn.execute(
            "SELECT session_user = 'ah_ingest' AND current_user = 'ah_ingest' "
            "AND NOT EXISTS (SELECT 1 FROM pg_roles r "
            "WHERE (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication) "
            "AND pg_has_role(session_user, r.oid, 'MEMBER')) "
            "AND NOT EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = 'ah' "
            "AND pg_has_role(session_user, n.nspowner, 'MEMBER')) "
            "AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'ah' AND pg_has_role(session_user, c.relowner, 'MEMBER')) "
            "AND NOT has_schema_privilege(session_user, 'ah', 'CREATE') "
            "AND NOT pg_has_role(session_user, 'pg_execute_server_program', 'MEMBER') "
            "AND NOT pg_has_role(session_user, 'pg_write_server_files', 'MEMBER') "
            "AND NOT pg_has_role(session_user, 'pg_read_server_files', 'MEMBER')"
        ).fetchone()
        if not safe or safe[0] is not True:
            raise UnsafeIngestRole(
                "refusing hourly collection: connect as dedicated ah_ingest without administrative or owner privileges"
            )
    # Ending the guard transaction is essential: Collector.write() must commit,
    # not become a savepoint within an implicit transaction left by this SELECT.


def connect(dsn=None, config=None):
    import psycopg
    from .config import load_config

    dsn = (
        dsn
        or os.environ.get("AGENT_HISTORY_INGEST_DSN")
        or os.environ.get("AGENT_HISTORY_DSN")
        or (config or load_config()).dsn
    )
    if dsn:
        conn = psycopg.connect(
            dsn, application_name="agent-history-collect", connect_timeout=10, options="-c statement_timeout=120000"
        )
    else:
        env = load_env()
        conn = psycopg.connect(
            host=env.get("PGHOST"),
            port=env.get("PGPORT", "5432"),
            dbname=env.get("PGDATABASE"),
            user=env.get("PGUSER"),
            password=env.get("PGPASSWORD"),
            application_name=env.get("PGAPPNAME", "agent-history-collect"),
            connect_timeout=int(env.get("PGCONNECT_TIMEOUT", "10") or 10),
            options="-c statement_timeout=120000",
        )
    try:
        _check_ingest_role(conn)
    except Exception:
        conn.close()
        raise
    return conn


def upsert(
    cur,
    table: str,
    cols: list[tuple[str, str]],
    key: list[str],
    rows: list[dict[str, Any]],
    guard: str | None = None,
    touch: bool = True,
) -> tuple[int, int]:
    """Bulk INSERT .. ON CONFLICT DO UPDATE only where a non-key column differs. Returns (inserted, updated).

    `cols` pairs each column with its SQL array type; table/column names are constants, values are bound.
    `guard` is an extra SQL condition (constant) ANDed into the update's WHERE.
    """
    from psycopg import sql

    if not rows:
        return 0, 0
    names = [c for c, _ in cols]
    data = [c for c in names if c not in key]
    # unnest() flattens a text[][], so array-valued columns travel as jsonb and are rebuilt per row.
    arg_types = ["jsonb" if t == "text[]" else t for _, t in cols]
    unnest = sql.SQL(", ").join(sql.SQL("{}::{}").format(sql.Placeholder(), sql.SQL(t + "[]")) for t in arg_types)
    select = sql.SQL(", ").join(
        sql.SQL("ARRAY(SELECT jsonb_array_elements_text(u.{}))").format(sql.Identifier(c))
        if t == "text[]"
        else sql.SQL("u.{}").format(sql.Identifier(c))
        for c, t in cols
    )
    target = sql.Identifier("ah", table)
    tbl = sql.Identifier(table)
    stmt = sql.SQL(
        "INSERT INTO {target} AS {tbl} ({cols}) SELECT {select} FROM unnest({unnest}) AS u({cols}) "
        "ON CONFLICT ({key}) DO UPDATE SET {sets}{touch} "
        "WHERE ({old}) IS DISTINCT FROM ({new}){guard} RETURNING (xmax = 0) AS inserted"
    ).format(
        target=target,
        tbl=tbl,
        select=select,
        touch=sql.SQL(", seen_at = now()" if touch else ""),
        cols=sql.SQL(", ").join(map(sql.Identifier, names)),
        unnest=unnest,
        key=sql.SQL(", ").join(map(sql.Identifier, key)),
        sets=sql.SQL(", ").join(sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in data),
        old=sql.SQL(", ").join(sql.SQL("{}.{}").format(tbl, sql.Identifier(c)) for c in data),
        new=sql.SQL(", ").join(sql.SQL("EXCLUDED.{}").format(sql.Identifier(c)) for c in data),
        guard=sql.SQL(" AND (" + guard + ")") if guard else sql.SQL(""),
    )
    from psycopg.types.json import Jsonb

    params = [[Jsonb(r[c]) if t == "text[]" else r[c] for r in rows] for c, t in cols]
    cur.execute(stmt, params)
    flags = [r[0] for r in cur.fetchall()]
    return sum(1 for f in flags if f), sum(1 for f in flags if not f)


GIT_COLS = [
    ("repo_slug", "text"),
    ("sha", "text"),
    ("context", "text"),
    ("committed_at", "timestamptz"),
    ("author_is_owner", "boolean"),
    ("subject", "text"),
    ("parent_count", "smallint"),
    ("files_changed", "integer"),
    ("insertions", "integer"),
    ("deletions", "integer"),
    ("reverts_sha", "text"),
    ("on_default", "boolean"),
]
GIT_FILE_COLS = [
    ("repo_slug", "text"),
    ("sha", "text"),
    ("path", "text"),
    ("change", "text"),
    ("old_path", "text"),
    ("insertions", "integer"),
    ("deletions", "integer"),
]
CI_COLS = [
    ("run_id", "bigint"),
    ("repo_slug", "text"),
    ("context", "text"),
    ("head_sha", "text"),
    ("workflow", "text"),
    ("event", "text"),
    ("status", "text"),
    ("conclusion", "text"),
    ("attempt", "integer"),
    ("created_at", "timestamptz"),
    ("updated_at", "timestamptz"),
]
PREFIX_COLS = [("prefix", "text"), ("repo_slug", "text"), ("context", "text"), ("zero_pad", "smallint")]
TASK_COLS = [
    ("task_key", "text"),
    ("repo_slug", "text"),
    ("context", "text"),
    ("title", "text"),
    ("status", "text"),
    ("priority", "text"),
    ("labels", "text[]"),
    ("project", "text"),
    ("created_at", "timestamptz"),
    ("updated_at", "timestamptz"),
]
FEATURE_COLS = [
    ("machine", "text"),
    ("home", "text"),
    ("namespace", "text"),
    ("kind", "text"),
    ("name", "text"),
    ("source", "text"),
    ("enabled", "boolean"),
]
PERM_COLS = [
    ("machine", "text"),
    ("home", "text"),
    ("namespace", "text"),
    ("ts", "timestamptz"),
    ("line_hash", "text"),
    ("tool_name", "text"),
    ("cmd_verb", "text"),
    ("reason", "text"),
    ("is_subagent", "boolean"),
]
# Rows only move forward in updated_at; a row marked removed comes back when it reappears at the tip.
TASK_GUARD = (
    "backlog_task.updated_at IS NULL OR EXCLUDED.updated_at > backlog_task.updated_at "
    "OR (EXCLUDED.updated_at = backlog_task.updated_at AND EXCLUDED.status = 'Archived') "
    "OR (backlog_task.status = 'removed' AND EXCLUDED.updated_at >= backlog_task.updated_at)"
)


class Collector:
    def __init__(self, conn, dry_run: bool, deadline: float, rescan_days: int | None = None):
        self.conn, self.dry_run, self.deadline, self.rescan_days = conn, dry_run, deadline, rescan_days
        self.counts: dict[str, dict[str, int]] = {}
        self.skipped: list[dict[str, str]] = []
        self.errors: list[dict[str, str]] = []
        self.repos = 0

    def rollback(self) -> None:
        if self.conn is not None:
            self.conn.rollback()

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise Deadline()

    def add(self, table: str, **kv: int) -> None:
        bucket = self.counts.setdefault(table, {})
        for k, v in kv.items():
            bucket[k] = bucket.get(k, 0) + v

    def write(self, table, cols, key, rows, guard=None):
        if self.dry_run:
            self.add(table, rows=len(rows))
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            # permission_log and git_commit_file (migration 003) have no seen_at column to touch.
            ins, upd = upsert(
                cur, table, cols, key, rows, guard, touch=table not in ("permission_log", "git_commit_file")
            )
        self.add(table, inserted=ins, updated=upd)

    def scalar(self, query: str, params: tuple) -> Any:
        if self.dry_run:
            return None
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        self.conn.commit()
        return row[0] if row else None

    # -- repositories ------------------------------------------------------------------------
    def repositories(self) -> None:
        forks = github_forks()
        if forks is None:
            self.errors.append({"step": "github", "error": "gh_unavailable"})  # every github.com repo skipped
        prefixes_seen: dict[str, str] = {}
        slugs_seen: set[str] = set()
        for repo in repo_roots():
            self.check_time()
            try:
                info, reason = classify_repo(repo, forks)
            except (RuntimeError, subprocess.TimeoutExpired, OSError):
                info, reason = None, "git_error"
            if reason:
                if reason != "not_git":
                    self.skipped.append({"repo": repo.name, "reason": reason})
                continue
            if info["slug"] in slugs_seen:
                self.skipped.append({"repo": repo.name, "reason": "duplicate_slug"})
                continue
            slugs_seen.add(info["slug"])
            self.repos += 1
            self.one_repo(info, prefixes_seen)

    def one_repo(self, info: dict[str, Any], prefixes_seen: dict[str, str]) -> None:
        slug = info["slug"]
        days = None
        try:
            known = self.scalar("SELECT 1 FROM ah.git_commit WHERE repo_slug = %s LIMIT 1", (slug,))
            days = self.rescan_days or (RESCAN_DAYS if known else FIRST_RUN_DAYS)
            rows = git_commits(info, days)
            self.write("git_commit", GIT_COLS, ["repo_slug", "sha"], rows)
            if not self.dry_run and info["fetched"]:
                # Stored commits in the window that origin's branch no longer contains. Only a sha this
                # clone has and can prove is not an ancestor is marked; unknown shas are left alone.
                with self.conn.cursor() as cur:
                    cur.execute(
                        "SELECT sha FROM ah.git_commit WHERE repo_slug = %s AND on_default IS DISTINCT FROM false "
                        "AND committed_at >= now() - make_interval(days => %s) AND NOT (sha = ANY(%s))",
                        (slug, days, [r["sha"] for r in rows]),
                    )
                    candidates = [r[0] for r in cur.fetchall()]
                self.conn.commit()
                gone = [sha for sha in candidates if off_branch(info["path"], sha, info["ref"])]
                if gone:
                    with self.conn.transaction(), self.conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ah.git_commit SET on_default = false, seen_at = now() "
                            "WHERE repo_slug = %s AND sha = ANY(%s)",
                            (slug, gone),
                        )
                        self.add("git_commit", off_default=cur.rowcount)
        except Exception as error:  # one repo never stops the run
            self.rollback()
            self.errors.append({"repo": slug, "step": "git_commit", "error": type(error).__name__})
        self.check_time()
        try:
            self._git_commit_files(info, days if days is not None else RESCAN_DAYS)
        except Exception as error:
            self.rollback()
            self.errors.append({"repo": slug, "step": "git_commit_file", "error": type(error).__name__})
        self.check_time()
        if slug_host(slug) == GITHUB_HOST:
            try:
                self.write("ci_run", CI_COLS, ["run_id"], ci_runs(slug))
            except Exception as error:
                self.rollback()
                failure = {"repo": slug, "step": "ci_run", "error": type(error).__name__}
                if isinstance(error, CICollectionError):
                    failure.update(reason=error.reason, exit=error.exit_code)
                self.errors.append(failure)
        self.check_time()
        try:
            self._backlog(info, prefixes_seen)
        except Exception as error:
            self.rollback()
            self.errors.append({"repo": slug, "step": "backlog", "error": type(error).__name__})

    def _git_commit_files(self, info: dict[str, Any], days: int) -> None:
        """Same window as `git_commit`, widened once per repo to backfill a repo with no file rows yet.

        A fresh migration 003 leaves every already-ingested commit without git_commit_file rows. Rather
        than making a full-history rescan the default for everyone, only a repo that still has zero
        git_commit_file rows gets its window widened - to cover every git_commit row already stored for
        it - on this one run; once that first pass has written rows, later runs stay on the normal
        `days` window. `--rescan-days N` still works as an explicit, operator-driven alternative.
        """
        slug = info["slug"]
        file_days = days
        if not self.dry_run:
            has_files = self.scalar("SELECT 1 FROM ah.git_commit_file WHERE repo_slug = %s LIMIT 1", (slug,))
            if not has_files:
                oldest = self.scalar("SELECT min(committed_at) FROM ah.git_commit WHERE repo_slug = %s", (slug,))
                if oldest is not None:
                    file_days = max(file_days, (datetime.now(timezone.utc) - oldest).days + 1)
        self.write("git_commit_file", GIT_FILE_COLS, ["repo_slug", "sha", "path"], git_commit_files(info, file_days))

    def _backlog(self, info: dict[str, Any], prefixes_seen: dict[str, str]) -> None:
        slug = info["slug"]
        prefix, tasks, tip = backlog(info)
        if not prefix:
            return
        owner = prefixes_seen.get(prefix["prefix"])
        if owner and owner != slug:
            self.skipped.append({"repo": slug, "reason": f"prefix_conflict:{prefix['prefix']}"})
            return
        prefixes_seen[prefix["prefix"]] = slug
        self.write("task_prefix", PREFIX_COLS, ["prefix"], [prefix])
        self.write("backlog_task", TASK_COLS, ["task_key"], tasks, TASK_GUARD)
        if not self.dry_run and info["fetched"]:
            # Keys gone from origin's tip become 'removed', only after a successful fetch and only when
            # the tip is newer than the task, so a stale Mac never removes a task it has not seen.
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE ah.backlog_task SET status = 'removed', seen_at = now() "
                    "WHERE repo_slug = %s AND status IS DISTINCT FROM 'removed' "
                    "AND NOT (task_key = ANY(%s)) AND coalesce(updated_at, created_at, '-infinity') <= %s",
                    (slug, [t["task_key"] for t in tasks], tip),
                )
                self.add("backlog_task", removed=cur.rowcount)

    # -- homes -------------------------------------------------------------------------------
    def homes(self, machine: str, hostname: str) -> None:
        for home_label in HOMES:
            self.check_time()
            home = Path(os.path.expanduser(home_label))
            if not home.is_dir():
                continue
            namespace = HOME_NAMESPACES.get(home_label) or home_namespace(home_label, hostname)
            try:
                rows = [dict(r, machine=machine, home=home_label, namespace=namespace) for r in features(home)]
            except Exception as error:  # a malformed config file must not stop the other homes
                self.errors.append({"home": home_label, "step": "installed_feature", "error": type(error).__name__})
                rows = None
            if rows is not None:
                try:
                    self.snapshot(machine, home_label, rows)
                except Exception as error:
                    self.rollback()
                    self.errors.append({"home": home_label, "step": "installed_feature", "error": type(error).__name__})
            if home.name.startswith(".claude"):
                try:
                    perms = [
                        dict(r, machine=machine, home=home_label, namespace=namespace) for r in permission_rows(home)
                    ]
                    self.write("permission_log", PERM_COLS, ["machine", "home", "line_hash"], perms)
                except Exception as error:
                    self.rollback()
                    self.errors.append({"home": home_label, "step": "permission_log", "error": type(error).__name__})

    def snapshot(self, machine: str, home: str, rows: list[dict[str, Any]]) -> None:
        """Replace one (machine, home) feature snapshot in a single transaction, keeping first_seen_at."""
        if self.dry_run:
            self.add("installed_feature", rows=len(rows))
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            ins, upd = upsert(cur, "installed_feature", FEATURE_COLS, ["machine", "home", "kind", "name"], rows)
            cur.execute(
                "DELETE FROM ah.installed_feature f WHERE f.machine = %s AND f.home = %s AND NOT EXISTS "
                "(SELECT 1 FROM unnest(%s::text[], %s::text[]) AS k(kind, name) "
                "WHERE k.kind = f.kind AND k.name = f.name)",
                (machine, home, [r["kind"] for r in rows], [r["name"] for r in rows]),
            )
            self.add("installed_feature", inserted=ins, updated=upd, deleted=cur.rowcount)


def configure(config) -> None:
    global REPOSITORIES, ALLOWED_OWNERS, GITHUB_OWNERS, OWNER_IDENTITIES
    global CONTEXT, REPO_CONTEXTS, HOMES, HOME_NAMESPACES, MACHINE, LOCK_FILE
    REPOSITORIES = config.git_repos
    ALLOWED_OWNERS = {tuple(owner.split("/", 1)) for owner in config.identities.git_owners}
    GITHUB_OWNERS = sorted(owner for host, owner in ALLOWED_OWNERS if host == GITHUB_HOST)
    OWNER_IDENTITIES = set(config.identities.owner_emails)
    setting = config.collector
    CONTEXT = config.default_context
    REPO_CONTEXTS = setting.repo_contexts
    HOMES = tuple(setting.homes)
    HOME_NAMESPACES = setting.homes
    MACHINE = os.environ.get("AGENT_HISTORY_MACHINE") or setting.machine
    LOCK_FILE = setting.lock_file


def collect(conn, config, context=None):
    """Compatibility entry point for collect-git, using the common commit/file parser."""
    configure(config)
    result = {"repos": 0, "skipped": [], "commits": 0, "files": 0}
    for repo in config.git_repos:
        if not (repo / ".git").exists():
            result["skipped"].append(f"{repo.name}: not a git checkout")
            continue
        slug = repo_slug(git(repo, "remote", "get-url", "origin", check=False)) or f"local/{repo.name.lower()}"
        if config.identities.git_owners and not allowlisted(slug):
            result["skipped"].append(f"{repo.name}: owner not in [identities] git_owners")
            continue
        ref = git(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD", check=False).strip() or "HEAD"
        info = {"path": repo, "slug": slug, "ref": ref, "emails": set()}
        rows = git_commits(info, config.git_days)
        for row in rows:
            row["context"] = context or config.default_context
        files = git_commit_files(info, config.git_days)
        with conn.transaction(), conn.cursor() as cur:
            upsert(cur, "git_commit", GIT_COLS, ["repo_slug", "sha"], rows)
            upsert(cur, "git_commit_file", GIT_FILE_COLS, ["repo_slug", "sha", "path"], files, touch=False)
        result["repos"] += 1
        result["commits"] += len(rows)
        result["files"] += len(files)
    return result


def machine_name() -> tuple[str, str]:
    try:
        local = subprocess.run(
            ["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        local = ""
    host = socket.gethostname()
    return (local or host.split(".")[0]).lower(), f"{local} {host}"


def main(argv: list[str] | None = None, dsn=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-history-collect", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="collect without touching the database")
    parser.add_argument(
        "--rescan-days",
        type=int,
        metavar="N",
        help=f"rescan N days of git history for every repo (default: {FIRST_RUN_DAYS} for a new "
        f"repo, else {RESCAN_DAYS}); upserts correct changed rows in place",
    )
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    from .config import load_config

    config = load_config(args.config)
    configure(config)
    started = time.monotonic()

    def on_alarm(signum, frame):  # the hard bound: never let launchd accumulate a hung run
        print(json.dumps({"ok": False, "error": "runtime_limit", "limit_s": RUNTIME_LIMIT_S + 60}), flush=True)
        os._exit(124)

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(RUNTIME_LIMIT_S + 60)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"ok": False, "error": "already_running"}), flush=True)
        return 0
    detected, hostname = machine_name()
    machine = MACHINE or detected
    summary: dict[str, Any] = {"ok": True, "machine": machine, "dry_run": args.dry_run}
    conn = None
    try:
        conn = None if args.dry_run else connect(dsn, config)
        collector = Collector(conn, args.dry_run, started + RUNTIME_LIMIT_S, args.rescan_days)
        try:
            collector.repositories()
            collector.homes(machine, hostname)
        except Deadline:
            summary.update(ok=False, error="deadline")
        summary.update(
            repos=collector.repos, tables=collector.counts, skipped=collector.skipped, errors=collector.errors
        )
        if any(e.get("error") == "gh_unavailable" for e in collector.errors):
            summary["ok"] = False  # visible to a log/launchd check instead of silently skipping GitHub
    except Exception as error:  # a failed run is reported, never raised into launchd as a traceback storm
        summary.update(ok=False, error=type(error).__name__, detail=(str(error).splitlines() or [""])[0][:200])
    finally:
        if conn is not None:
            conn.close()
    summary["duration_s"] = round(time.monotonic() - started, 1)
    summary["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(summary, separators=(",", ":")), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
