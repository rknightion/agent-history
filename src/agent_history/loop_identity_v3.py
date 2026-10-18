"""Metadata-only v3 run identity and closeout completion.

Only collected hashes/qualified headers and complete successful structured transcript writes
are inspected. No file is opened, no report body or Data section is projected, and repository
names never come from a checkout basename or a header's display name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

CLOSEOUT_HEADER = re.compile(
    r"# Closeout: ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?) (loop[0-9]+) · Charter: ([0-9a-f]{64})"
)
ORIGIN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA256 = re.compile(r"[0-9a-f]{64}")
UNKNOWN = {"repo": None, "loop": None, "goal_sha256": None}


@dataclass(frozen=True)
class Run:
    uid: str
    root: int
    number: int | None
    started: datetime
    charter: str | None
    closeout: str | None
    mode: str = "v3"


@dataclass(frozen=True)
class Window:
    following: datetime | None
    tied: bool


@dataclass(frozen=True)
class Write:
    started: datetime
    ended: datetime
    line1: str | None
    sha256: str | None


@dataclass(frozen=True)
class Projection:
    fields: dict[str, Any]
    end: datetime | None = None


def _path(path: Any, cwd: str | None) -> str | None:
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    if not os.path.isabs(path) and (not cwd or not os.path.isabs(cwd)):
        return None
    return os.path.normpath(os.path.join(cwd or "", path))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate structured member")
        result[key] = value
    return result


PATCH_FILE = re.compile(r"\*\*\* (Add|Update|Delete) File: (.+)")


def _patch_content(patch: str, target: str, cwd: str | None) -> tuple[bool, str | None]:
    # Only LF and CRLF delimit patch records. Unicode separators, vertical tabs and
    # bare CR are file content, not opportunities to manufacture a first header line.
    lines = re.split(r"\r?\n", patch)
    if lines and lines[-1] == "":
        lines.pop()
    # Find known mutations before validating unrelated syntax. An unfamiliar or
    # malformed earlier block must never hide a later exact-target invalidation.
    touched = any(
        (_path(match[2], cwd) == target if (match := PATCH_FILE.fullmatch(line)) else False)
        or (line.startswith("*** Move to: ") and _path(line[13:], cwd) == target)
        for line in lines
    )
    if not touched or not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return touched, None
    additions, valid, index = [], True, 1
    while index < len(lines) - 1:
        match = PATCH_FILE.fullmatch(lines[index])
        if not match:
            valid = False
            index += 1
            continue
        index += 1
        body = []
        while index < len(lines) - 1 and not PATCH_FILE.fullmatch(lines[index]):
            body.append(lines[index])
            index += 1
        move = None
        if match[1] == "Update" and body and body[0].startswith("*** Move to: "):
            move = body.pop(0)[13:]
        if match[1] == "Add":
            valid &= all(line.startswith("+") for line in body)
        elif match[1] == "Delete":
            valid &= not body
        else:
            for position, line in enumerate(body):
                if line == "*** End of File":
                    valid &= position == len(body) - 1
                else:
                    valid &= not line or line[0] in " +-" or line == "@@" or line.startswith("@@ ")
        if _path(match[2], cwd) == target or (move is not None and _path(move, cwd) == target):
            if match[1] != "Add":
                return True, None
            additions.append("".join(line[1:] + "\n" for line in body))
    return True, additions[0] if valid and len(additions) == 1 else None


def structured_closeout(
    tool: str, raw: str | None, target: str, cwd: str | None, started: datetime, ended: datetime
) -> Write | None:
    """Extract only a hash and first line from a complete exact-path Write or added file.

    An apply_patch Add File contains the entire new file. Update hunks, moves, shell text and
    output-only file lists cannot prove byte zero or completeness and are not reconstructed.
    Codex's executed FileChange add shape is also complete evidence, not a partial unified diff.
    """
    if not raw or ended < started:
        return None
    tool = tool.rsplit(".", 1)[-1].lower()
    content = None
    touched = False
    unknown_write = Write(started, ended, None, None)
    try:
        args = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        args = None
    if tool == "write" and isinstance(args, dict):
        if _path(args.get("file_path", args.get("path")), cwd) == target:
            touched = True
            content = args.get("content")
    elif tool in {"edit", "multiedit"} and isinstance(args, dict):
        # A string replacement changes the file without revealing its bytes.
        touched = _path(args.get("file_path", args.get("path")), cwd) == target
    elif tool == "filechange" and isinstance(args, dict):
        matches = [v for p, v in args.items() if _path(p, cwd) == target]
        touched = bool(matches)
        if len(matches) == 1 and isinstance(matches[0], dict):
            change = matches[0]
            if change.get("type") == "add" and not change.get("move_path"):
                content = change.get("content")
    elif tool == "apply_patch":
        patch = args.get("input", args.get("patch")) if isinstance(args, dict) else raw
        if not isinstance(patch, str):
            return None
        touched, content = _patch_content(patch, target, cwd)
    if not isinstance(content, str) or "\x00" in content:
        return unknown_write if touched else None
    first = content.partition("\n")[0].removesuffix("\r")
    if not CLOSEOUT_HEADER.fullmatch(first):
        first = None
    try:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None
    return Write(started, ended, first, digest)


def project(
    run: Run,
    launches: Sequence[Run],
    metadata: Iterable[Mapping[str, Any]],
    writes: Iterable[Write],
    *,
    origins: Iterable[str] = (),
    launch_digest: str | None = None,
    lane_starts: Iterable[datetime] = (),
    window: Window | None = None,
) -> Projection:
    """Fail-closed attribution for one run, independent of storage and refresh ordering."""
    empty = Projection(dict(UNKNOWN))
    fields = {"repo": None, "loop": f"loop{run.number}" if run.number is not None else None, "goal_sha256": None}
    if run.mode != "v3" or run.number is None or not run.closeout:
        return empty
    # Both exact-path reuse on any root and a replacement in this root bound the run.
    if window is None:
        related = [r for r in launches if r.closeout == run.closeout or r.root == run.root]
        later = [r.started for r in related if r.started > run.started]
        following = min(later) if later else None
        tied = sum(r.started == run.started and r.closeout == run.closeout for r in launches) != 1
    else:
        following, tied = window.following, window.tied

    def inside(at):
        return run.started <= at and (following is None or at < following)

    rows = list(metadata)
    charters = [r for r in rows if r["kind"] == "charter" and r["path"] == run.charter]
    closeouts = [r for r in rows if r["kind"] == "closeout" and r["path"] == run.closeout and inside(r["file_mtime"])]
    # Charter snapshots overwritten after a later launch cannot describe the older run.
    charters = [r for r in charters if following is None or r["file_mtime"] < following]
    evidence = charters + ([] if tied else closeouts)
    metadata_origins = []
    for row in evidence:
        origin = row.get("repo_origin")
        if not isinstance(origin, str) or not ORIGIN.fullmatch(origin) or not SHA256.fullmatch(row.get("sha256") or ""):
            return empty
        metadata_origins.append(origin.lower())
    # Explicit collected origins win over captured checkout metadata. Without collected
    # provenance, root/relaunch captured remote URLs are allowed, but never header text.
    repos = set(metadata_origins or (o.lower() for o in origins if ORIGIN.fullmatch(o)))
    if len(repos) > 1:
        return empty
    digests = {r["sha256"] for r in charters}
    if launch_digest:
        if not SHA256.fullmatch(launch_digest):
            return empty
        digests.add(launch_digest)
    snapshots = set()
    end_candidates = []
    if not tied:
        for row in closeouts:
            header = CLOSEOUT_HEADER.fullmatch(row.get("line1") or "")
            if not header or header[2] != fields["loop"]:
                return empty
            digests.add(header[3])
            snapshots.add(row["sha256"])
            end_candidates.append(row["file_mtime"])
    if len(snapshots) > 1:
        return empty
    eligible = [w for w in writes if inside(w.started) and inside(w.ended)] if not tied else []
    if eligible:
        latest = max(w.ended for w in eligible)
        latest_writes = {w for w in eligible if w.ended == latest}
        if len(latest_writes) != 1:
            return empty
        (written,) = latest_writes
        if written.sha256 is None:
            # A partial edit hides the final bytes. Only a collected copy saved at or after it
            # (whose header was checked above) can stand in; mtimes may be whole seconds.
            if not snapshots or any(r["file_mtime"] < written.started.replace(microsecond=0) for r in closeouts):
                return empty
        else:
            header = CLOSEOUT_HEADER.fullmatch(written.line1 or "")
            if not header or header[2] != fields["loop"]:
                return empty
            digests.add(header[3])
            # A collected copy may have a later revision, but conflicting observed bytes
            # never select the convenient machine or silently prefer a transcript write.
            if snapshots and written.sha256 not in snapshots:
                return empty
        end_candidates.append(written.ended)
    if len(digests) > 1:
        return empty
    fields["repo"] = next(iter(repos), None)
    fields["goal_sha256"] = next(iter(digests), None)
    if not fields["repo"] or not fields["goal_sha256"] or not end_candidates:
        return Projection(fields)
    lanes = [t for t in lane_starts if inside(t)]
    ends = [at for at in end_candidates if not any(t > at for t in lanes)]
    return Projection(fields, min(ends) if ends else None)


def _remote(url: str | None) -> str | None:
    """Strict captured URL attribution without importing the Mac-only collector runtime."""
    if not url or re.search(r"[\s\\\\]", url):
        return None
    if "://" in url:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https", "ssh", "git", "git+ssh", "ssh+git"}:
                return None
            if not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname or "") or parsed.query or parsed.fragment:
                return None
            if parsed.port is not None and not 0 < parsed.port <= 65535:
                return None
            path = parsed.path.removeprefix("/")
        except ValueError:
            return None
    else:
        match = re.fullmatch(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:([^:]+)", url)
        if not match:
            return None
        path = match[1]
    path = path.removesuffix("/").lower().removesuffix(".git")
    if not ORIGIN.fullmatch(path) or set(path.split("/")) & {".", ".."}:
        return None
    return path


def _manifest(text: str, path: str | None, cwd: str | None) -> str | None:
    """A captured manifest's exact charter path, never a launch hash or arbitrary digest."""
    digests = set()
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([0-9a-f]{64})[ \t]+(\S+)[ \t]*", line)
        if match and _path(match[2], cwd) == path:
            digests.add(match[1])
    return next(iter(digests)) if len(digests) == 1 else "conflict" if digests else None


MARKER = "loops_identity_v3_projection_v1"
HISTORY_BATCH = 128

SELECTION_SQL = """
    WITH changed AS MATERIALIZED (
        SELECT s.id,s.root_session_id,s.loop_run_id FROM dirty_now d JOIN ah.session s ON s.id=d.session_id
    ), dirty_owners AS MATERIALIZED (
        SELECT l.launch_uid FROM changed c JOIN ah.loop_run l ON l.root_session_id=COALESCE(c.root_session_id,c.id)
        UNION SELECT l.launch_uid FROM changed c JOIN ah.loop_run l ON l.id=c.loop_run_id
        UNION SELECT l.launch_uid FROM changed c JOIN ah.session p ON p.id=c.root_session_id
              JOIN ah.loop_run l ON l.id=p.loop_run_id
    ), dirty_paths AS (
        SELECT DISTINCT l.report_path FROM dirty_owners d JOIN ah.loop_run l USING(launch_uid)
    ), selected AS (
        SELECT launch_uid FROM ah.loops WHERE status IN ('running','waiting','paused')
        UNION SELECT launch_uid FROM dirty_owners
        UNION SELECT l.launch_uid FROM dirty_paths p JOIN ah.loop_run l ON l.report_path=p.report_path
        UNION SELECT l.launch_uid FROM ah.loop_run_metadata r JOIN ah.loop_run l ON l.goal_path=r.path
              WHERE r.kind='charter' AND r.seen_at >= %s::timestamptz - interval '2 hours' AND r.seen_at <= %s
        UNION SELECT l.launch_uid FROM ah.loop_run_metadata r JOIN ah.loop_run l ON l.report_path=r.path
              WHERE r.kind='closeout' AND r.seen_at >= %s::timestamptz - interval '2 hours' AND r.seen_at <= %s
        UNION SELECT unnest(%s::text[]) AS launch_uid
    )
    SELECT l.launch_uid FROM selected p JOIN ah.loop_run l USING(launch_uid)
    WHERE l.mode='v3' AND l.launch_ts IS NOT NULL ORDER BY l.launch_uid
"""

WINDOWS_SQL = """
    SELECT l.launch_uid,l.root_session_id,l.loop_number,l.launch_ts,l.goal_path,l.report_path,l.mode,
           s.cwd,s.git_remote_url,m.text,
           least((SELECT min(n.launch_ts) FROM ah.loop_run n
                  WHERE n.report_path=l.report_path AND n.launch_ts>l.launch_ts),
                 (SELECT min(n.launch_ts) FROM ah.loop_run n
                  WHERE n.root_session_id=l.root_session_id AND n.launch_ts>l.launch_ts)) AS following,
           EXISTS(SELECT 1 FROM ah.loop_run n WHERE n.report_path=l.report_path
                  AND n.launch_ts=l.launch_ts AND n.launch_uid<>l.launch_uid) AS tied,
           l.id
    FROM ah.loop_run l JOIN ah.session s ON s.id=l.root_session_id
    LEFT JOIN ah.message m ON m.session_id=s.id AND
        l.launch_uid=s.agent || '/' || s.session_uid || '/' || s.agent_id || ':' || m.event_uid
    WHERE l.launch_uid=ANY(%s)
"""

TOOLS_SQL = """
    SELECT i.tool_name,i.input_text,s.cwd,i.ts,COALESCE(t.ended_at,o.completed_at),i.output_text
    FROM ah.tool_io i JOIN ah.session s ON s.id=i.session_id
    LEFT JOIN ah.tool_call t ON t.agent=i.agent AND t.call_uid=i.call_uid AND t.session_id=i.session_id
    LEFT JOIN ah.tool_op o ON o.agent=i.agent AND o.item_uid=i.item_uid AND o.session_id=i.session_id
    WHERE (s.id=%s OR (s.loop_run_id=%s AND s.loop_link_method='relaunch'))
      AND i.ts >= %s AND i.ts < COALESCE(%s::timestamptz,'infinity')
      AND COALESCE(t.ended_at,o.completed_at) >= i.ts
      AND COALESCE(t.ended_at,o.completed_at) < COALESCE(%s::timestamptz,'infinity')
      AND lower(i.tool_name) ~ '(^|[.])(write|edit|multiedit|apply_patch|filechange)$'
      AND NOT COALESCE(i.input_truncated,false)
      AND (NOT COALESCE(i.output_truncated,false) OR lower(i.tool_name) ~ '(^|[.])(edit|multiedit)$')
      AND ((i.kind='call' AND t.outcome='ok' AND t.ended_at IS NOT NULL) OR
           (i.kind='op' AND o.status='completed' AND o.completed_at IS NOT NULL
            AND o.is_error IS FALSE AND (o.exit_code IS NULL OR o.exit_code=0)))
"""


def projections(conn) -> dict[str, Projection]:
    """Select current/dirty/metadata-changed owners and 128 historical launches, once.

    Both the catch-up cursor and the metadata watermark share one transactional marker.
    First use leaves historical metadata to bounded catch-up, not an unbounded initial scan.
    The watermark retains the receipt projection's two-hour overlap for concurrent collectors.
    SQL resolves windows/ties before transferring any retained structured tool input.
    """
    saved, observed_at = conn.execute("SELECT (SELECT value FROM ah.meta WHERE key=%s),now()", (MARKER,)).fetchone()
    state = json.loads(saved) if saved else {}
    cursor, seen = state.get("cursor", ""), state.get("metadata_seen")
    historical = []
    if cursor != "complete":
        historical = [
            r[0]
            for r in conn.execute(
                "SELECT launch_uid FROM ah.loop_run WHERE mode='v3' AND launch_ts IS NOT NULL "
                "AND launch_uid>%s ORDER BY launch_uid LIMIT %s",
                (cursor, HISTORY_BATCH),
            )
        ]
    # NULL means no previous watermark: bootstrap old metadata only via the bounded cohort.
    selected = [r[0] for r in conn.execute(SELECTION_SQL, (seen, observed_at, seen, observed_at, historical))]
    records = conn.execute(WINDOWS_SQL, (selected,)).fetchall() if selected else []
    result = {}
    for row in records:
        run = Run(*row[:7])
        cwd, remote, launch_text, following, tied, loop_id = row[7:]
        window = Window(following, tied)
        metadata = [
            dict(zip(("path", "kind", "sha256", "line1", "file_mtime", "repo_origin"), values, strict=True))
            for values in conn.execute(
                "SELECT path,kind,sha256,line1,file_mtime,repo_origin FROM ah.loop_run_metadata "
                "WHERE ((kind='charter' AND path=%s) OR "
                "(kind='closeout' AND path=%s AND file_mtime >= %s)) "
                "AND file_mtime < COALESCE(%s::timestamptz,'infinity')",
                (run.charter, run.closeout, run.started, following),
            )
        ]
        writes = []
        tool_rows = conn.execute(TOOLS_SQL, (run.root, loop_id, run.started, following, following)) if not tied else ()
        for tool, raw, tool_cwd, started, ended, output in tool_rows:
            # Older Codex rows can say ok for explicit patch verification failures.
            if tool.rsplit(".", 1)[-1].lower() == "apply_patch" and re.match(
                r"\s*(?:apply_patch verification failed|error|failed|invalid patch)", output or "", re.I
            ):
                continue
            observed = structured_closeout(tool, raw, run.closeout, tool_cwd, started, ended) if run.closeout else None
            if observed:
                writes.append(observed)
        urls = [remote]
        urls.extend(
            r[0]
            for r in conn.execute(
                "SELECT git_remote_url FROM ah.session WHERE loop_run_id=%s AND loop_link_method='relaunch' "
                "AND first_event_at < COALESCE(%s::timestamptz,'infinity') "
                "UNION SELECT remote_url FROM ah.git_event WHERE session_id=%s AND ts >= %s "
                "AND ts < COALESCE(%s::timestamptz,'infinity')",
                (loop_id, following, run.root, run.started, following),
            )
        )
        lane_starts = [
            r[0]
            for r in conn.execute(
                "SELECT first_event_at FROM ah.session WHERE id<>%s AND (root_session_id=%s OR loop_run_id=%s) "
                "AND loop_link_method IS DISTINCT FROM 'relaunch' AND first_event_at >= %s "
                "AND first_event_at < COALESCE(%s::timestamptz,'infinity')",
                (run.root, run.root, loop_id, run.started, following),
            )
        ]
        result[run.uid] = project(
            run,
            (),
            metadata,
            writes,
            origins=[found for url in urls if (found := _remote(url))],
            launch_digest=_manifest(launch_text or "", run.charter, cwd),
            lane_starts=lane_starts,
            window=window,
        )
    following_cursor = (
        cursor if cursor == "complete" else historical[-1] if len(historical) == HISTORY_BATCH else "complete"
    )
    conn.execute(
        "INSERT INTO ah.meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (MARKER, json.dumps({"cursor": following_cursor, "metadata_seen": observed_at.isoformat()})),
    )
    return result


def refresh_completion(conn, projected: Mapping[str, Projection] | None = None) -> list[str]:
    """Revoke invalid closeout-only ends, preserving independent replacement/state evidence."""
    if projected is None:
        projected = projections(conn)
    for uid, value in projected.items():
        if value.end is not None:
            conn.execute(
                "UPDATE ah.loop_run SET end_ts=%s,end_evidence='closeout' WHERE launch_uid=%s "
                "AND (end_evidence IS NULL OR end_evidence IN ('root_last_event','closeout','state_close'))",
                (value.end, uid),
            )
        else:
            conn.execute(
                "UPDATE ah.loop_run l SET end_evidence='root_last_event',end_ts=("
                "SELECT greatest(max(s.last_event_at),l.launch_ts) FROM ah.session s "
                "WHERE s.id=l.root_session_id OR (s.loop_run_id=l.id AND s.loop_link_method='relaunch')) "
                "WHERE l.launch_uid=%s AND l.end_evidence='closeout'",
                (uid,),
            )
    return [uid for uid, value in projected.items() if value.end is not None]


def refresh_identity(conn, projected: Mapping[str, Projection] | None = None) -> None:
    """Compatibility: goal_sha256 holds the v3 charter digest, not a new redundant column."""
    if projected is None:
        projected = projections(conn)
    for uid, value in projected.items():
        fields = value.fields
        conn.execute(
            "UPDATE ah.loops SET repo=%s,loop=%s,goal_sha256=%s WHERE launch_uid=%s "
            "AND (repo,loop,goal_sha256) IS DISTINCT FROM (%s,%s,%s)",
            (
                fields["repo"],
                fields["loop"],
                fields["goal_sha256"],
                uid,
                fields["repo"],
                fields["loop"],
                fields["goal_sha256"],
            ),
        )
