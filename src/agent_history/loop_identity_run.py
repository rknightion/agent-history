"""Run-folder dialect (protocol v5): recognition, metadata-only completion and collected counts.

A run of this dialect has no charter and no pasted launch. Its folder is
`codex/run-<YYYYMMDD>-<n>/` inside a repository. Its root is the top-level session whose complete,
successful structured write creates that folder's `AUTHORITY.md` with the qualified first line
`# Authority: <repo> run-<YYYYMMDD>-<n>` (the run name equal to the folder name) and `protocol: v5`
as the next non-empty line. The run starts at that write. It ends when the sibling `CLOSEOUT.md`
is written with `# Closeout: <repo> run-<YYYYMMDD>-<n>` and `protocol: v5`; its landed and parked
counts are the collected integers of that closeout's `## Landed` and `## Parked` headings.

Rows of this dialect have `mode = 'run'` and `protocol = 'v5'`. No v3 or historical rule reads them
and nothing here reads them for another dialect, so each dialect stays fail-closed on its own
evidence. Only collected metadata and complete successful structured transcript writes are read;
no file is opened and the repository never comes from a header or a checkout basename.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .loop_identity_v3 import ORIGIN, SHA256, TOOLS_SQL, _path, _remote, structured_content

MODE = "run"
PROTOCOL = "v5"
AUTHORITY = "AUTHORITY.md"
CLOSEOUT = "CLOSEOUT.md"
KINDS = {"AUTHORITY": "authority", "CLOSEOUT": "run_closeout"}
_NAME = r"run-[0-9]{8}-[1-9][0-9]{0,3}"
_REPO = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?"
RUN_NAME = re.compile(_NAME)
AUTHORITY_HEADER = re.compile(r"# Authority: (" + _REPO + r") (" + _NAME + r")")
RUN_CLOSEOUT_HEADER = re.compile(r"# Closeout: (" + _REPO + r") (" + _NAME + r")")
# One declared protocol token on a line of its own: `protocol: v5`, `protocol: v4.1`.
PROTOCOL_LINE = re.compile(r"protocol: (v[1-9][0-9]{0,2}(?:\.[0-9]{1,3})?)[ \t]*")
DOCUMENT = re.compile(r"(/.+)/codex/(" + _NAME + r")/(AUTHORITY|CLOSEOUT)\.md")
# Only a hint for which paths a structured input may name; the structured parse decides.
CANDIDATE = re.compile(r"[^\s\"'`*:<>|\\]*codex/" + _NAME + r"/AUTHORITY\.md")


def run_name(name: str) -> str | None:
    """The name when it is `run-<YYYYMMDD>-<n>` with a real calendar date and n from 1, else None."""
    if not RUN_NAME.fullmatch(name):
        return None
    try:
        datetime.strptime(name[4:12], "%Y%m%d")
    except ValueError:
        return None
    return name


def declared_protocol(text: str) -> str | None:
    """The token of a `protocol: <token>` line that is the first non-empty line after line 1.

    A protocol line anywhere else, a malformed token or a missing line is None.
    """
    for line in re.split(r"\r?\n", text)[1:]:
        if line.strip():
            match = PROTOCOL_LINE.fullmatch(line)
            return match[1] if match else None
    return None


def document(path: Any) -> tuple[str, str, str] | None:
    """(checkout, run name, kind) for an exact normalised absolute run-document path, else None."""
    if not isinstance(path, str) or os.path.normpath(path) != path:
        return None
    match = DOCUMENT.fullmatch(path)
    if not match or run_name(match[2]) is None:
        return None
    return match[1], match[2], KINDS[match[3]]


def qualified(kind: str, run: str, first_line: str | None) -> bool:
    """True when a first line is the exact header of `kind` for this run folder's name."""
    pattern = AUTHORITY_HEADER if kind == "authority" else RUN_CLOSEOUT_HEADER
    header = pattern.fullmatch(first_line or "")
    return bool(header) and header[2] == run


def _content_qualifies(kind: str, run: str, content: Any) -> bool:
    if not isinstance(content, str) or "\x00" in content:
        return False
    first = content.partition("\n")[0].removesuffix("\r")
    return qualified(kind, run, first) and declared_protocol(content) == PROTOCOL


def authority_targets(tool: str, raw: str | None, cwd: str | None) -> list[str]:
    """Exact AUTHORITY.md paths one structured input creates whole with a qualified v5 body.

    Only a complete `Write`/`write`, `apply_patch` Add File or executed FileChange add counts:
    an edit cannot show byte zero, and shell text is never interpreted.
    """
    found = []
    for candidate in dict.fromkeys(CANDIDATE.findall(raw or "")):
        target = _path(candidate, cwd)
        place = document(target)
        if not place or place[2] != "authority":
            continue
        _, content = structured_content(tool, raw, target, cwd)
        if _content_qualifies("authority", place[1], content):
            found.append(target)
    return found


def closeout_path(authority: str) -> str:
    return os.path.join(os.path.dirname(authority), CLOSEOUT)


@dataclass(frozen=True)
class CloseoutWrite:
    started: datetime
    ended: datetime
    sha256: str | None  # None: the mutation hides the final bytes (an edit)
    valid: bool  # complete bytes with this run's qualified header and protocol line


def closeout_write(
    tool: str, raw: str | None, target: str, cwd: str | None, started: datetime, ended: datetime
) -> CloseoutWrite | None:
    """Evidence one captured mutation gives about the exact CLOSEOUT.md, or None when untouched."""
    place = document(target)
    if not raw or ended < started or not place or place[2] != "run_closeout":
        return None
    touched, content = structured_content(tool, raw, target, cwd)
    if not touched:
        return None
    if not isinstance(content, str) or "\x00" in content:
        return CloseoutWrite(started, ended, None, False)
    try:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None
    return CloseoutWrite(started, ended, digest, _content_qualifies("run_closeout", place[1], content))


@dataclass(frozen=True)
class RunProjection:
    repo: str | None
    end: datetime | None
    following: datetime | None = None


def project(
    authority: str,
    started: datetime,
    metadata: Iterable[Mapping[str, Any]],
    writes: Iterable[CloseoutWrite],
    *,
    origins: Iterable[str] = (),
    following: datetime | None = None,
    tied: bool = False,
) -> RunProjection:
    """Fail-closed repository and closeout end for one run of this dialect.

    The window is half-open, from the AUTHORITY.md write to the next run on the same root or exact
    folder. Collected origins win over captured remotes; any malformed collected row or two origins
    leave the repository unknown and the run without a closeout end. Every in-window collected
    closeout must carry this run's header and `protocol: v5` and share one hash. The latest in-window
    captured write must be unique; complete bytes must qualify and match a collected copy when one
    exists, and an edit needs a collected copy saved at or after it. The end is the earliest of
    the evidence that remains.
    """
    place = document(authority)
    empty = RunProjection(None, None, following)
    if not place or place[2] != "authority":
        return empty
    run = place[1]
    closeout = closeout_path(authority)

    def inside(at):
        return started <= at and (following is None or at < following)

    rows = list(metadata)
    authorities = [
        r
        for r in rows
        if r["kind"] == "authority" and r["path"] == authority and (following is None or r["file_mtime"] < following)
    ]
    closeouts = [r for r in rows if r["kind"] == "run_closeout" and r["path"] == closeout and inside(r["file_mtime"])]
    evidence = authorities + ([] if tied else closeouts)
    collected = []
    for row in evidence:
        origin = row.get("repo_origin")
        if not isinstance(origin, str) or not ORIGIN.fullmatch(origin) or not SHA256.fullmatch(row.get("sha256") or ""):
            return empty
        collected.append(origin.lower())
    repos = set(collected or (o.lower() for o in origins if ORIGIN.fullmatch(o)))
    if len(repos) > 1:
        return empty
    repo = next(iter(repos), None)
    if tied:
        return RunProjection(repo, None, following)
    snapshots = set()
    ends = []
    for row in closeouts:
        if not qualified("run_closeout", run, row.get("line1")) or row.get("protocol") != PROTOCOL:
            return RunProjection(repo, None, following)
        snapshots.add(row["sha256"])
        ends.append(row["file_mtime"])
    if len(snapshots) > 1:
        return RunProjection(repo, None, following)
    eligible = [w for w in writes if inside(w.started) and inside(w.ended)]
    if eligible:
        latest = max(w.ended for w in eligible)
        latest_writes = {w for w in eligible if w.ended == latest}
        if len(latest_writes) != 1:
            return RunProjection(repo, None, following)
        (written,) = latest_writes
        if written.sha256 is None:
            # Only a collected copy saved at or after the edit (mtimes may be whole seconds) stands in.
            if not snapshots or any(r["file_mtime"] < written.started.replace(microsecond=0) for r in closeouts):
                return RunProjection(repo, None, following)
        elif not written.valid or (snapshots and written.sha256 not in snapshots):
            return RunProjection(repo, None, following)
        ends.append(written.ended)
    return RunProjection(repo, min(ends) if ends else None, following)


MARKER = "loops_run_projection_v1"

SELECTION_SQL = """
    WITH changed AS MATERIALIZED (
        SELECT s.id,s.root_session_id,s.loop_run_id FROM dirty_now d JOIN ah.session s ON s.id=d.session_id
    ), selected AS (
        SELECT launch_uid FROM ah.loops WHERE status IN ('running','waiting','paused')
        UNION SELECT l.launch_uid FROM changed c JOIN ah.loop_run l ON l.root_session_id=COALESCE(c.root_session_id,c.id)
        UNION SELECT l.launch_uid FROM changed c JOIN ah.loop_run l ON l.id=c.loop_run_id
        UNION SELECT l.launch_uid FROM ah.loop_run_metadata r JOIN ah.loop_run l ON l.goal_path=r.path
              WHERE r.kind='authority' AND r.seen_at >= %s::timestamptz - interval '2 hours' AND r.seen_at <= %s
        UNION SELECT l.launch_uid FROM ah.loop_run_metadata r JOIN ah.loop_run l ON l.report_path=r.path
              WHERE r.kind='run_closeout' AND r.seen_at >= %s::timestamptz - interval '2 hours' AND r.seen_at <= %s
    )
    SELECT l.launch_uid,l.root_session_id,l.id,l.launch_ts,l.goal_path,l.report_path,s.git_remote_url,
           least((SELECT min(n.launch_ts) FROM ah.loop_run n
                  WHERE n.report_path=l.report_path AND n.launch_ts>l.launch_ts),
                 (SELECT min(n.launch_ts) FROM ah.loop_run n
                  WHERE n.root_session_id=l.root_session_id AND n.launch_ts>l.launch_ts)) AS following,
           EXISTS(SELECT 1 FROM ah.loop_run n WHERE n.report_path=l.report_path
                  AND n.launch_ts=l.launch_ts AND n.launch_uid<>l.launch_uid) AS tied
    FROM selected p JOIN ah.loop_run l USING(launch_uid) JOIN ah.session s ON s.id=l.root_session_id
    WHERE l.mode='run' AND l.launch_ts IS NOT NULL ORDER BY l.launch_uid
"""


def projections(conn) -> dict[str, RunProjection]:
    """Project selected runs of this dialect: active, dirty, or with metadata seen since the last pass.

    The first pass has no watermark and selects every collected row, which is bounded by the few
    runs of this dialect. The watermark keeps the v3 projection's two-hour collector overlap.
    """
    saved, observed_at = conn.execute("SELECT (SELECT value FROM ah.meta WHERE key=%s),now()", (MARKER,)).fetchone()
    seen = json.loads(saved).get("metadata_seen") if saved else None
    seen = seen or "-infinity"
    result = {}
    for uid, root, loop_id, started, authority, closeout, remote, following, tied in conn.execute(
        SELECTION_SQL, (seen, observed_at, seen, observed_at)
    ).fetchall():
        metadata = [
            dict(zip(("path", "kind", "sha256", "line1", "file_mtime", "repo_origin", "protocol"), values, strict=True))
            for values in conn.execute(
                "SELECT path,kind,sha256,line1,file_mtime,repo_origin,protocol FROM ah.loop_run_metadata "
                "WHERE ((kind='authority' AND path=%s) OR (kind='run_closeout' AND path=%s AND file_mtime >= %s)) "
                "AND file_mtime < COALESCE(%s::timestamptz,'infinity')",
                (authority, closeout, started, following),
            )
        ]
        writes = []
        for tool, raw, tool_cwd, at, ended, output in (
            conn.execute(TOOLS_SQL, (root, loop_id, started, following, following)) if not tied and closeout else ()
        ):
            if tool.rsplit(".", 1)[-1].lower() == "apply_patch" and re.match(
                r"\s*(?:apply_patch verification failed|error|failed|invalid patch)", output or "", re.I
            ):
                continue
            observed = closeout_write(tool, raw, closeout, tool_cwd, at, ended)
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
                (loop_id, following, root, started, following),
            )
        )
        result[uid] = project(
            authority,
            started,
            metadata,
            writes,
            origins=[found for url in urls if (found := _remote(url))],
            following=following,
            tied=tied,
        )
    conn.execute(
        "INSERT INTO ah.meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (MARKER, json.dumps({"metadata_seen": observed_at.isoformat()})),
    )
    return result


def refresh_completion(conn, projected: Mapping[str, RunProjection]) -> None:
    """Set or revoke the closeout end of each projected run of this dialect.

    A closeout end replaces the root's last event or the next run's start. Revoked, the end
    returns to the next run's start when there is one, else to the root's last event.
    """
    for uid, value in projected.items():
        if value.end is not None:
            conn.execute(
                "UPDATE ah.loop_run SET end_ts=%s,end_evidence='closeout' WHERE launch_uid=%s AND mode=%s "
                "AND (end_evidence IS NULL OR end_evidence IN ('root_last_event','next_launch','closeout')) "
                "AND (end_ts,end_evidence) IS DISTINCT FROM (%s::timestamptz,'closeout')",
                (value.end, uid, MODE, value.end),
            )
        elif value.following is not None:
            conn.execute(
                "UPDATE ah.loop_run SET end_ts=%s,end_evidence='next_launch' "
                "WHERE launch_uid=%s AND mode=%s AND end_evidence='closeout'",
                (value.following, uid, MODE),
            )
        else:
            conn.execute(
                "UPDATE ah.loop_run l SET end_evidence='root_last_event',end_ts=("
                "SELECT greatest(max(s.last_event_at),l.launch_ts) FROM ah.session s "
                "WHERE s.id=l.root_session_id OR (s.loop_run_id=l.id AND s.loop_link_method='relaunch')) "
                "WHERE l.launch_uid=%s AND l.mode=%s AND l.end_evidence='closeout'",
                (uid, MODE),
            )


def refresh_identity(conn, projected: Mapping[str, RunProjection]) -> None:
    """Repository and run name for projected runs; the checkout fallback fills an unknown repository."""
    from .loops import REPO_FALLBACK_SQL

    for uid, value in projected.items():
        name = conn.execute("SELECT goal_path FROM ah.loop_run WHERE launch_uid=%s", (uid,)).fetchone()
        place = document(name[0]) if name else None
        label = place[1] if place else None
        conn.execute(
            "UPDATE ah.loops SET repo=COALESCE(%s," + REPO_FALLBACK_SQL + "),loop=%s,goal_sha256=NULL "
            "WHERE launch_uid=%s AND (repo,loop,goal_sha256) IS DISTINCT FROM "
            "(COALESCE(%s," + REPO_FALLBACK_SQL + "),%s,NULL)",
            (value.repo, uid, uid, label, uid, value.repo, uid, uid, label),
        )


def refresh_counts(conn) -> None:
    """A run of this dialect finished by its closeout takes its counts from the collected closeout.

    Same rule as the v3 closeout counts, on every pass: the in-window exact-path snapshots with this
    run's header and `protocol: v5` must share one hash; a NULL count leaves that column as the other
    projections computed it.
    """
    conn.execute(
        """
        WITH target AS (
            SELECT l.launch_uid, r.report_path, r.launch_ts,
                   (SELECT min(n.launch_ts) FROM ah.loop_run n
                    WHERE n.launch_ts > r.launch_ts
                      AND (n.report_path = r.report_path OR n.root_session_id = r.root_session_id)) AS following
            FROM ah.loops l JOIN ah.loop_run r USING (launch_uid)
            WHERE r.mode = %s AND r.end_evidence = 'closeout' AND r.end_ts IS NOT NULL
              AND l.status = 'finished' AND r.report_path IS NOT NULL
        ), counted AS (
            SELECT t.launch_uid, max(m.landed_count) AS landed, max(m.parked_count) AS parked
            FROM target t JOIN ah.loop_run_metadata m
              ON m.kind = 'run_closeout' AND m.path = t.report_path AND m.line1 IS NOT NULL
             AND m.protocol = %s
             AND m.file_mtime >= t.launch_ts AND m.file_mtime < COALESCE(t.following, 'infinity')
            GROUP BY t.launch_uid
            HAVING count(DISTINCT m.sha256) = 1
        )
        UPDATE ah.loops l SET tasks_landed = COALESCE(c.landed, l.tasks_landed),
                              tasks_done = COALESCE(c.landed, l.tasks_done),
                              parks_total = COALESCE(c.parked, l.parks_total)
        FROM counted c
        WHERE l.launch_uid = c.launch_uid
          AND (c.landed IS NOT NULL OR c.parked IS NOT NULL)
          AND (l.tasks_landed, l.tasks_done, l.parks_total) IS DISTINCT FROM
              (COALESCE(c.landed, l.tasks_landed), COALESCE(c.landed, l.tasks_done), COALESCE(c.parked, l.parks_total))
        """,
        (MODE, PROTOCOL),
    )


def refresh_protocol(conn) -> None:
    """The declared protocol of every run of the earlier dialects, from its collected documents.

    The value is the one token declared by the exact-path charter and closeout snapshots; none,
    or two different tokens, is NULL. Runs of this dialect keep `v5` from their recognition.
    """
    conn.execute(
        """
        WITH declared AS (
            SELECT l.id, m.protocol FROM ah.loop_run l JOIN ah.loop_run_metadata m
              ON m.kind = 'charter' AND m.path = l.goal_path
            WHERE l.mode IS DISTINCT FROM %s AND m.protocol IS NOT NULL
            UNION ALL
            SELECT l.id, m.protocol FROM ah.loop_run l JOIN ah.loop_run_metadata m
              ON m.kind = 'closeout' AND m.path = l.report_path
            WHERE l.mode IS DISTINCT FROM %s AND m.protocol IS NOT NULL
        ), agreed AS (
            SELECT id, CASE WHEN count(DISTINCT protocol) = 1 THEN min(protocol) END AS protocol
            FROM declared GROUP BY id
        )
        UPDATE ah.loop_run l SET protocol = a.protocol
        FROM ah.loop_run x LEFT JOIN agreed a ON a.id = x.id
        WHERE l.id = x.id AND l.mode IS DISTINCT FROM %s AND l.protocol IS DISTINCT FROM a.protocol
        """,
        (MODE, MODE, MODE),
    )
