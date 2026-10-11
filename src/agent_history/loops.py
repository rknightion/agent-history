"""Loop/campaign tagging post-pass.

Runs inside load.post_passes' transaction over the roots of dirty sessions. Launches are found in
the root's operator text (human/queued prompts) with `loop_launch.parse_launch`. The indexer
never reads launch or report files (they may live on another machine), so:
- a bare `launch-*.txt|md` path is retained, and resolved only from a complete successful recorded
  read of that exact file, never the indexer's filesystem;
- exact path-only relaunches and successful final loop-state append invocations can join a root to
  an existing run, but never supply identity or terminal evidence;
- a loop is finished by a wave-notify completion receipt collected into ah.loop_receipt, else
  by the next launch in the same root session; the root session's last event is only a fallback
  timestamp. An exactly attributed valid collected state close can replace that analytical fallback
  and establish lifecycle completion. Transcript text and shell commands are never
  interpreted for completion or identity.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from .loop_launch import REPORT_HEADER, identity_fields, parse_launch, report_lane_counts

ACTIVATED_AT = "1970-01-01T00:00:00Z"  # launches before this instant are ignored
BARE_LAUNCH = re.compile(r"^`?\s*(\S*launch-[^\s`/]*\.(?:txt|md))\s*`?$")
LANE_LINE = re.compile(r"^\s*Lane:\s*(\S+)", re.M)
# A lane-return block opens on a line of its own. Its JSON is decoded, so a fence quoted in a field's
# text (a gate tail) never ends it early; the closing fence may follow the JSON on the same line.
LANE_OPEN = re.compile(r"^ {0,3}```lane-return[ \t]*\r?\n", re.M)
LANE_CLOSE = re.compile(r"\s*```")
FENCE_LINE_END = re.compile(r"```[ \t]*\r?$", re.M)
UNPARSED = object()
LANE_STATUS_V2 = ("complete", "partial", "blocked", "failed")
CAMPAIGN = re.compile(r"^report-(.*?)-(?:loop|wave)\d+\.md$")
# wave-notify completion receipts by kind. `.notified` (no longer written): `sha256:<64 hex> request
# <id>`, or the legacy `request <id>`. `.posted`, written once the receiver accepted the report:
# `sha256:<64 hex> receiver <JSON object>`, always with a digest.
COMPLETION = {
    "notified": re.compile(r"(?:sha256:([0-9a-f]{64}) )?request \S+\n?"),
    "posted": re.compile(r"sha256:([0-9a-f]{64}) receiver \{[^\n]*\}\n?"),
}
# wave-notify start receipt: `<owner>/<repo>#loop<N>#<goal sha256>`.
# The identity a completion receipt's report first line carries (the repo name in it is never used).
HEADER_IDENTITY = re.compile(r"# Loop: [A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)? (loop[0-9]+) · Goal: ([0-9a-f]{64})")
START = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(loop[0-9]+)#([0-9a-f]{64})\n?")
# Evidence values that end a loop. `report_write` only survives on rows tagged before receipts.
START_SKEW = timedelta(seconds=120)  # a start ping may be stamped slightly before its launch message
FINISHED_EVIDENCE = ("completion_receipt", "next_launch", "report_write")


def _no_read(path: Any) -> str:  # noqa: ARG001
    raise OSError("the indexer does not read launch files")


def _launch(text: str, cwd: str | None, ts_iso: str) -> dict[str, Any] | None:
    # Handle paths before the shared file-backed parser, so even existence checks cannot consult
    # the indexer's filesystem. Only captured transcript reads resolve their contents below.
    bare = BARE_LAUNCH.match(text.strip())
    if bare:
        path = bare.group(1)
        if not os.path.isabs(path) and cwd:
            path = os.path.normpath(os.path.join(cwd, path))
        # The launch file is not read; derive its report by the protocol's naming convention
        # (launch-<x>.txt|md -> report-<x>.md in the same codex/ directory).
        stem = PurePosixPath(path).stem.removeprefix("launch-")
        report = str(PurePosixPath(path).parent / f"report-{stem}.md")
        loop = re.search(r"-(?:loop|wave)(\d+)$", stem)
        return {
            "status": "unresolved_path",
            "launch_path": path,
            "report": report,
            "loop": int(loop.group(1)) if loop else None,
            "mode": None,
            "launch_sha256": None,
            "budget": None,
        }
    try:
        found = parse_launch(text, cwd, ts_iso, ACTIVATED_AT, read_file=_no_read)
    except Exception:
        found = None
    if found:
        found["status"] = "resolved"
    return found


def _report_meta(report: str | None) -> dict[str, Any]:
    if not report:
        return {}
    p = PurePosixPath(report)
    out: dict[str, Any] = {"naming": "wave" if "-wave" in p.name else "loop"}
    if p.parent.name == "codex":
        out["repo_slug"] = p.parent.parent.name
        out["goal_path"] = str(p.parent / p.name.replace("report-", "goal-", 1))
    m = CAMPAIGN.match(p.name)
    if m:
        out["campaign_slug"] = m.group(1)
    return out


def enqueue_backfill(conn: psycopg.Connection) -> None:
    """Visit at most 128 retained sessions per refresh, atomically with their projections.

    No schema change is needed: ah.meta already holds transactional projection cursors. Rebuild
    explicitly resets this cursor with the derived session rows.
    """
    # A new evidence policy needs a new bounded historical pass, not a reset of the old cursor.
    key = "loops_links_projection_v2"
    saved = conn.execute("SELECT value FROM ah.meta WHERE key = %s", (key,)).fetchone()
    cursor = saved[0] if saved else "0"
    if cursor == "complete":
        return
    ids = [r[0] for r in conn.execute("SELECT id FROM ah.session WHERE id > %s ORDER BY id LIMIT 128", (int(cursor),))]
    if ids:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO dirty_now VALUES (%s) ON CONFLICT DO NOTHING", [(sid,) for sid in ids])
    conn.execute(
        "INSERT INTO ah.meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(ids[-1]) if len(ids) == 128 else "complete"),
    )


def repair_pi_spawns(conn: psycopg.Connection) -> None:
    """Re-project missing foreground paths from retained, exact structured tool results.

    Session content need not be reparsed just to fill this additive metadata. Names and timings
    never choose a child; the same parent/call/index and recorded sessionFile supply its run path.
    """
    from .parse_pi import SESSION_PATH_RE

    for parent, call, raw in conn.execute(
        "SELECT i.session_id, i.call_uid, i.result_json FROM dirty_now d "
        "JOIN ah.tool_io i ON i.session_id = d.session_id "
        "WHERE i.agent = 'pi' AND i.tool_name = 'subagent' AND i.result_json IS NOT NULL "
        "AND NOT COALESCE(i.output_truncated, false)",
    ):
        try:
            details = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        results = details.get("results") if isinstance(details, dict) else None
        if not isinstance(results, list):
            continue
        for n, result in enumerate(results):
            if not isinstance(result, dict) or not isinstance(result.get("sessionFile"), str):
                continue
            path = SESSION_PATH_RE.search(result["sessionFile"])
            index = result.get("index")
            if path:
                conn.execute(
                    "UPDATE ah.subagent_spawn SET child_task_name = %s WHERE agent = 'pi' "
                    "AND parent_session_id = %s AND spawn_uid = %s AND requested_type = %s "
                    "AND child_task_name IS NULL",
                    (
                        "/".join(p for p in path.groups() if p),
                        parent,
                        f"{call}:{index if type(index) is int else n}",
                        result.get("agent"),
                    ),
                )


def _captured_launch(
    conn: psycopg.Connection, root_id: int, hit: dict[str, Any], start: Any, following: Any = None
) -> str | None:
    """Complete successful structured reads of the exact launch file, never local file I/O.

    Partial, failed and truncated reads do not resolve a path. Conflicting observed contents remain
    unknown. A captured launch must name the protocol's exact report target.
    """
    path = hit.get("launch_path")
    if not path:
        return None
    from .parse_pi import source_output_truncated

    cwd = conn.execute("SELECT cwd FROM ah.session WHERE id = %s", (root_id,)).fetchone()[0]
    texts = set()
    for raw, output, result in conn.execute(
        "SELECT i.input_text, i.output_text, i.result_json FROM ah.tool_io i JOIN ah.tool_call t "
        "ON t.agent = i.agent AND t.call_uid = i.call_uid "
        "WHERE i.session_id = %s AND lower(i.tool_name) = 'read' "
        "AND t.outcome = 'ok' AND t.ended_at IS NOT NULL "
        "AND NOT COALESCE(i.input_truncated, false) AND NOT COALESCE(i.output_truncated, false) "
        "AND i.ts >= %s AND (%s::timestamptz IS NULL OR i.ts < %s)",
        (root_id, start, following, following),
    ):
        try:
            args = json.loads(raw or "")
            details = json.loads(result) if result else None
        except (ValueError, RecursionError):
            continue
        if source_output_truncated("read", output, details):
            continue
        recorded_path = args.get("path", args.get("file_path")) if isinstance(args, dict) else None
        if not isinstance(recorded_path, str) or os.path.normpath(os.path.join(cwd or "", recorded_path)) != path:
            continue
        if args.get("offset") not in (None, 1) or args.get("limit") is not None:
            continue
        if output:
            texts.add(output)
    if len(texts) != 1:
        return None
    text = next(iter(texts))
    parsed = _launch(text, str(PurePosixPath(path).parent.parent), start.isoformat())
    return text if parsed and parsed.get("report") == hit.get("report") and parsed["status"] == "resolved" else None


def _resume_targets(conn: psycopg.Connection, root_id: int, cwd: str | None) -> set[str]:
    """Exact path-only first prompts and successful loop-state append invocations.

    Command text is linkage evidence only. It never supplies identity or a terminal outcome.
    Do not interpret shell expansions, quoted text printed by echo, pipelines or substitutions.
    """
    targets = set()
    first = conn.execute(
        "SELECT text FROM ah.message WHERE session_id = %s "
        "AND message_class IN ('human_prompt', 'queued_prompt') ORDER BY ts, id LIMIT 1",
        (root_id,),
    ).fetchone()
    if first:
        path = first[0].strip().strip("`")
        if re.fullmatch(r"\S*/(?:launch|goal)-[^/\s]+\.(?:txt|md)", path):
            path = os.path.normpath(os.path.join(cwd or "", path))
            p = PurePosixPath(path)
            if p.parent.name == "codex":
                targets.add(str(p.with_name(re.sub(r"^(launch|goal)-", "report-", p.stem) + ".md")))
    for raw in conn.execute(
        "SELECT i.input_text FROM ah.tool_io i JOIN ah.tool_call t "
        "ON t.agent = i.agent AND t.call_uid = i.call_uid "
        "WHERE i.session_id = %s AND lower(i.tool_name) = 'bash' AND t.outcome = 'ok' "
        "AND t.ended_at IS NOT NULL AND NOT COALESCE(i.input_truncated, false)",
        (root_id,),
    ):
        try:
            args = json.loads(raw[0] or "")
            command = args.get("command") if isinstance(args, dict) else None
            if not isinstance(command, str) or any(c in command for c in ("$", "`", "|", "&", "(", ")", "<", ">")):
                continue
            lexer = shlex.shlex(command.replace("\n", "\n;"), posix=True, punctuation_chars=";")
            lexer.whitespace += "\n"
            lexer.whitespace_split = True
            tokens = list(lexer)
        except (ValueError, RecursionError):
            continue
        for i in range(len(tokens) - 2):
            if i and tokens[i - 1] != ";":
                continue
            if PurePosixPath(tokens[i]).name != "loop-state" or tokens[i + 1] != "append":
                continue
            # Whole-command success proves only the final simple invocation, not an earlier
            # failure hidden by a later semicolon command.
            if ";" in tokens[i + 3 :]:
                continue
            recorded_path = tokens[i + 2]
            if any(c in recorded_path for c in "~*?[]"):
                continue
            # A preceding command can change the effective directory. Do not reconstruct shell
            # state from text or resolve a relative final append against the session's old cwd.
            if not os.path.isabs(recorded_path) and (i != 0 or not cwd or not os.path.isabs(cwd)):
                continue
            path = os.path.normpath(os.path.join(cwd or "", recorded_path))
            p = PurePosixPath(path)
            if p.parent.name == "codex" and re.fullmatch(r"state-.+-(?:loop|wave)\d+\.jsonl", p.name):
                targets.add(str(p.with_name(p.stem.replace("state-", "report-", 1) + ".md")))
    return targets


def _link_relaunch(conn: psycopg.Connection, root_id: int) -> bool:
    root = conn.execute(
        "SELECT cwd, first_event_at, session_uid, is_subagent FROM ah.session WHERE id = %s",
        (root_id,),
    ).fetchone()
    if not root or root[3]:
        return False
    cwd, first_at, uid, _ = root
    if (
        first_at is None
        or conn.execute(
            "SELECT 1 FROM ah.loop_run WHERE root_session_id = %s LIMIT 1",
            (root_id,),
        ).fetchone()
    ):
        return False
    # An explicit new root launch is not a continuation merely because it uses the same state log.
    prompts = conn.execute(
        "SELECT text FROM ah.message WHERE session_id = %s AND message_class IN ('human_prompt','queued_prompt')",
        (root_id,),
    )
    if any(_launch(text, cwd, first_at.isoformat()) and not BARE_LAUNCH.match(text.strip()) for (text,) in prompts):
        return False
    targets = _resume_targets(conn, root_id, cwd)
    if len(targets) != 1:
        return False
    matches = conn.execute(
        "SELECT id, root_session_id, launch_ts FROM ah.loop_run "
        "WHERE report_path = %s AND root_session_id <> %s AND launch_ts <= %s",
        (next(iter(targets)), root_id, first_at),
    ).fetchall()
    if len(matches) != 1:
        return False
    loop_id, owner, start = matches[0]
    conn.execute(
        "UPDATE ah.session SET loop_run_id = %s, root_session_id = %s, loop_link_method = 'relaunch' "
        "WHERE id = %s AND (loop_run_id IS NULL OR loop_run_id = %s)",
        (loop_id, owner, root_id, loop_id),
    )
    conn.execute(
        "UPDATE ah.session SET root_session_id = %s WHERE agent = 'pi' AND root_session_uid = %s AND id <> %s",
        (owner, uid, root_id),
    )
    _tag_tree(conn, loop_id, owner, start, None)
    return True


def retract_unproven_relaunches(conn: psycopg.Connection) -> None:
    """Remove only retained relaunch memberships contradicted by the current evidence policy.

    The bounded historical cursor makes these roots dirty. Their exact pi root uid identifies
    descendants that inherited the same false membership; unrelated lanes and content are untouched.
    Re-enqueue the former owner so progress and lifecycle no longer attribute the rejected root.
    """
    candidates = conn.execute(
        "SELECT s.id, s.cwd, s.session_uid, s.loop_run_id, l.root_session_id, l.report_path, s.first_event_at "
        "FROM dirty_now d JOIN ah.session s ON s.id = d.session_id "
        "JOIN ah.loop_run l ON l.id = s.loop_run_id WHERE s.loop_link_method = 'relaunch'",
    ).fetchall()
    for sid, cwd, uid, loop_id, owner, report, started in candidates:
        targets = _resume_targets(conn, sid, cwd)
        matches = conn.execute(
            "SELECT id FROM ah.loop_run WHERE report_path = %s AND root_session_id <> %s AND launch_ts <= %s",
            (report, sid, started),
        ).fetchall()
        if targets == {report} and matches == [(loop_id,)]:
            continue
        descendants = [
            r[0]
            for r in conn.execute(
                "UPDATE ah.session SET root_session_id = %s, loop_run_id = NULL, loop_link_method = NULL "
                "WHERE agent = 'pi' AND root_session_uid = %s AND root_session_id = %s "
                "AND loop_run_id = %s AND id <> %s RETURNING id",
                (sid, uid, owner, loop_id, sid),
            )
        ]
        if descendants:
            conn.execute("DELETE FROM ah.lane WHERE loop_run_id = %s AND session_id = ANY(%s)", (loop_id, descendants))
            conn.execute("INSERT INTO dirty_now SELECT unnest(%s::bigint[]) ON CONFLICT DO NOTHING", (descendants,))
        conn.execute(
            "UPDATE ah.session SET root_session_id = id, loop_run_id = NULL, loop_link_method = NULL WHERE id = %s",
            (sid,),
        )
        conn.execute("INSERT INTO dirty_now VALUES (%s) ON CONFLICT DO NOTHING", (owner,))


def run(conn: psycopg.Connection) -> dict[str, int]:
    roots = [
        r[0]
        for r in conn.execute(
            "WITH roots AS (SELECT DISTINCT COALESCE(s.root_session_id, s.id) AS id FROM dirty_now d "
            "JOIN ah.session s ON s.id = d.session_id) "
            "SELECT r.id FROM roots r JOIN ah.session s ON s.id = r.id "
            "ORDER BY s.first_event_at NULLS LAST, r.id"
        )
    ]
    found = 0
    for root_id in roots:
        if not _link_relaunch(conn, root_id):
            found += _tag_root(conn, root_id)
    # A root and its continuation may arrive together with either source order.
    for root_id in roots:
        _link_relaunch(conn, root_id)
    return {"loop_roots": len(roots), "launches": found}


def refresh_live(conn: psycopg.Connection) -> dict[str, int]:
    """Refresh lifecycle state even without dirty transcripts (silence ages on every pass).

    A completion receipt or replacement launch is terminal evidence. Otherwise, root activity
    within 24 hours is evidence of running, not proof of a live process. A silent root is stale and
    may resume. Do not reuse loop_run's fallback end timestamp as evidence of completion.
    """
    finished = _refresh_completion_receipts(conn)
    result = conn.execute("""
        INSERT INTO ah.loops (launch_uid, status, launch_ts, end_ts, observed_at)
        SELECT l.launch_uid,
               CASE WHEN l.end_evidence IN ('completion_receipt', 'next_launch', 'report_write')
                    AND l.end_ts IS NOT NULL
                    THEN 'finished'
                    WHEN greatest(s.last_event_at, l.launch_ts) >= now() - interval '24 hours'
                    THEN 'running' ELSE 'stale' END,
               l.launch_ts,
               CASE WHEN l.end_evidence IN ('completion_receipt', 'next_launch', 'report_write')
                    AND l.end_ts IS NOT NULL
                    THEN l.end_ts
                    WHEN greatest(s.last_event_at, l.launch_ts) >= now() - interval '24 hours'
                    THEN NULL ELSE greatest(s.last_event_at, l.launch_ts) END,
               now()
        FROM ah.loop_run l LEFT JOIN LATERAL (
            SELECT max(last_event_at) AS last_event_at FROM ah.session
            WHERE id = l.root_session_id OR (loop_run_id = l.id AND loop_link_method = 'relaunch')
        ) s ON true
        WHERE l.launch_ts IS NOT NULL
        ON CONFLICT (launch_uid) DO UPDATE SET status = EXCLUDED.status,
            launch_ts = EXCLUDED.launch_ts, end_ts = EXCLUDED.end_ts,
            observed_at = EXCLUDED.observed_at
    """)
    count = result.rowcount
    _refresh_identity(conn, finished)
    _refresh_progress(conn)
    _refresh_state_counts(conn)
    from .loop_live import refresh as refresh_phase

    refresh_phase(conn)
    return {"live_loops": count}


COMPLETION_SQL = """
    WITH cand AS (
        SELECT l.id, l.launch_uid, l.root_session_id, l.loop_number, l.naming, l.launch_ts, l.report_path
        FROM ah.loop_run l
        WHERE l.launch_ts IS NOT NULL AND l.report_path IS NOT NULL
          AND {scope}
          AND EXISTS (SELECT 1 FROM ah.loop_receipt r
                      WHERE r.kind IN ('notified', 'posted') AND r.path = l.report_path)
    ), windows AS (
        SELECT s.launch_uid,
               lead(s.launch_ts) OVER (PARTITION BY s.report_path ORDER BY s.launch_ts, s.launch_uid) AS following,
               count(*) OVER (PARTITION BY s.report_path, s.launch_ts) > 1 AS tied
        FROM ah.loop_run s
        WHERE s.launch_ts IS NOT NULL AND s.report_path IN (SELECT report_path FROM cand)
    )
    SELECT c.launch_uid, c.loop_number, c.naming, r.kind, r.receipt_mtime, r.content,
           r.target_exists, r.target_sha256, r.target_line1, r.repo_origin
    FROM cand c
    JOIN windows w USING (launch_uid)
    JOIN ah.loop_receipt r ON r.kind IN ('notified', 'posted') AND r.path = c.report_path
    WHERE NOT w.tied
      AND r.receipt_mtime >= c.launch_ts
      AND (w.following IS NULL OR r.receipt_mtime < w.following)
      AND NOT EXISTS (
          SELECT 1 FROM ah.session s
          WHERE s.id <> c.root_session_id
            AND (s.root_session_id = c.root_session_id OR s.loop_run_id = c.id)
            AND s.loop_link_method IS DISTINCT FROM 'relaunch'
            AND s.first_event_at >= c.launch_ts
            AND (w.following IS NULL OR s.first_event_at < w.following)
            AND s.first_event_at > r.receipt_mtime)
"""


def _valid_completions(conn: psycopg.Connection, uid: str | None = None) -> dict[str, list[tuple]]:
    """Valid completion receipts as {launch_uid: [(mtime, repo_origin, target_line1, has_digest)]}.

    Without `uid`, only launches not already finished; with it, that one launch whatever its state.
    A receipt is valid when it names the launch's exact report path inside the launch's window (at or
    after launch, before the next launch of that report path, no lane of the loop starting after
    it) and, when it carries a digest, the collected report exists, matches it, and its first line
    names the launch's loop number. A legacy `notified` receipt is valid on the exact path alone; a
    `posted` receipt always carries a digest and is checked the same way as a digest `notified` one.
    """
    if uid is None:
        scope, params = "NOT COALESCE(l.end_evidence = ANY(%s), false)", (list(FINISHED_EVIDENCE),)
    else:
        scope, params = "l.launch_uid = %s", (uid,)
    found: dict[str, list[tuple]] = {}
    for row in conn.execute(COMPLETION_SQL.format(scope=scope), params):
        uid_, number, naming, kind, when, content, exists, digest, line1, origin = row
        match = COMPLETION[kind].fullmatch(content)
        if not match:
            continue
        if match[1]:
            # A digest receipt vouches for a report it can be checked against.
            if not exists:
                continue
            head = REPORT_HEADER.fullmatch(line1 or "")
            if match[1] != digest or not head or head[1] != naming or int(head[2]) != number:
                continue
        found.setdefault(uid_, []).append((when, origin, line1, bool(match[1])))
    return found


def _refresh_completion_receipts(conn: psycopg.Connection) -> list[str]:
    """Finish unfinished launches that have a valid wave-notify completion receipt.

    See `_valid_completions` for validity. The earliest valid mtime across machines is the end.
    Only launches with a receipt are read, and nothing here reads transcript or command text.
    Returns the launches finished by this call.
    """
    ends = {uid: min(r[0] for r in receipts) for uid, receipts in _valid_completions(conn).items()}
    if ends:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE ah.loop_run SET end_ts = %s, end_evidence = 'completion_receipt' WHERE launch_uid = %s",
                [(when, uid) for uid, when in ends.items()],
            )
    return list(ends)


UNKNOWN = {"repo": None, "loop": None, "goal_sha256": None}


def _start_identity(
    conn: psycopg.Connection, goal_path: str | None, label: str | None, launch_ts: Any
) -> tuple[str, dict[str, Any] | None]:
    """The identity a wave-notify start receipt for the exact goal path carries.

    Returns ("none", None) when no receipt applies, ("unknown", None) when receipts apply but
    cannot be trusted, else ("ok", identity).

    The receipt row is keyed by goal path, so a relaunch overwrites it. A receipt therefore belongs
    to a launch only when its mtime is within START_SKEW before that launch and before the next
    launch of the same goal path; outside that window it is not evidence about this launch. A
    receipt inside the window of more than one launch cannot be attributed (unknown). A receipt
    naming another loop is ignored. Every remaining receipt, on every machine, must parse, name the
    repository its own checkout's origin names (case-insensitively) and agree with the others.
    """
    if not goal_path or launch_ts is None:
        return "none", None
    receipts = conn.execute(
        "SELECT content, repo_origin, receipt_mtime FROM ah.loop_receipt "
        "WHERE kind = 'started' AND path = %s ORDER BY machine",
        (goal_path,),
    ).fetchall()
    if not receipts:
        return "none", None
    launches = sorted(
        r[0]
        for r in conn.execute(
            "SELECT launch_ts FROM ah.loop_run WHERE goal_path = %s AND launch_ts IS NOT NULL", (goal_path,)
        )
    )

    def inside(when: Any, start: Any) -> bool:
        later = [t for t in launches if t > start]
        return start - START_SKEW <= when and (not later or when < later[0])

    applicable = []
    for content, origin, when in receipts:
        if not inside(when, launch_ts):
            continue
        if sum(inside(when, t) for t in launches) > 1:
            return "unknown", None
        match = START.fullmatch(content)
        if match and label is not None and match[2] != label:
            continue
        if not match or label is None or not origin or match[1].lower() != origin.lower():
            return "unknown", None
        applicable.append(match.groups())
    if not applicable:
        return "none", None
    if len({(repo.lower(), loop, digest) for repo, loop, digest in applicable}) != 1:
        return "unknown", None
    repo, loop, digest = applicable[0]
    return "ok", {"repo": repo, "loop": loop, "goal_sha256": digest}


def _completion_identity(conn: psycopg.Connection, uid: str, label: str | None) -> tuple[str, dict[str, Any] | None]:
    """The identity a finished launch's valid completion receipts carry, same return shape.

    Only a launch finished by a receipt qualifies; a running loop has none. repo is the receipt's
    checkout origin, never the name in the report header. loop and goal digest come from the
    receipt's report first line, which must name the launch's own loop label. Only a digest receipt
    binds that line to the posted or notified bytes; a legacy receipt, or one without an origin or a usable
    header, or naming another loop, contributes nothing. Valid receipts that
    disagree with one another make the result unknown.
    """
    evidence = conn.execute("SELECT end_evidence FROM ah.loop_run WHERE launch_uid = %s", (uid,)).fetchone()
    if not evidence or evidence[0] != "completion_receipt" or label is None:
        return "none", None
    seen = set()
    for _, origin, line1, has_digest in _valid_completions(conn, uid).get(uid, []):
        match = HEADER_IDENTITY.fullmatch(line1 or "")
        if not has_digest or not match or match[1] != label or not origin:
            continue
        seen.add((origin.lower(), match[1], match[2]))
    if not seen:
        return "none", None
    if len(seen) != 1:
        return "unknown", None
    ((origin, loop, digest),) = seen
    return "ok", {"repo": origin, "loop": loop, "goal_sha256": digest}


def _receipt_identity(
    conn: psycopg.Connection, uid: str, goal_path: str | None, label: str | None, fields: dict[str, Any], launch_ts: Any
) -> dict[str, Any]:
    """Overlay receipt identity on what the launch and report recorded.

    A valid start-receipt identity wins over the completion receipt's; if both exist and disagree on
    any field the result is all NULL. Neither contradicts identity already recorded: that is NULL
    too. No applicable receipt leaves `fields` untouched.
    """
    start, started_id = _start_identity(conn, goal_path, label, launch_ts)
    if start == "unknown":
        return dict(UNKNOWN)
    done, done_id = _completion_identity(conn, uid, label)
    if done == "unknown":
        return dict(UNKNOWN)
    if (
        started_id
        and done_id
        and (
            started_id["repo"].lower() != done_id["repo"].lower()
            or started_id["loop"] != done_id["loop"]
            or started_id["goal_sha256"] != done_id["goal_sha256"]
        )
    ):
        return dict(UNKNOWN)
    found = started_id or done_id
    if not found:
        return fields
    if (fields["repo"] and fields["repo"].lower() != found["repo"].lower()) or (
        fields["goal_sha256"] and fields["goal_sha256"] != found["goal_sha256"]
    ):
        return dict(UNKNOWN)
    return found


def _refresh_identity(conn: psycopg.Connection, finished: Sequence[str] = ()) -> None:
    """Re-project exact identity on every pass, including previously indexed launches.

    Only recorded metadata is authoritative: never read the current checkout or goal/report files.
    Report contents come from completed successful structured writes of the exact report path.
    Shell command strings are not interpreted. Keep this independent of terminal-evidence tagging.
    """
    initial = conn.execute(
        "SELECT (SELECT count(*) FROM ah.meta WHERE key IN "
        "('loops_identity_projection_v1', 'loops_lanes_projection_v1')) < 2"
    ).fetchone()[0]
    # Start receipts first seen since the last pass re-project their launch even when it is finished.
    mark = conn.execute("SELECT value FROM ah.meta WHERE key = 'loops_receipt_identity_seen'").fetchone()
    newest = conn.execute("SELECT max(seen_at) FROM ah.loop_receipt WHERE kind = 'started'").fetchone()[0]
    rows = conn.execute(
        """
        WITH ordered_launches AS (
            SELECT l.launch_uid, l.root_session_id,
                   CASE WHEN l.naming = 'loop' THEN l.loop_number END AS number,
                   l.goal_path, l.report_path, l.launch_ts, m.text,
                   lead(l.launch_ts) OVER (
                       PARTITION BY l.root_session_id ORDER BY l.launch_ts, m.id
                   ) AS following,
                   bool_and(COALESCE(m.ts = l.launch_ts, false)) OVER (
                       PARTITION BY l.root_session_id
                   ) AS window_known,
                   count(*) OVER (PARTITION BY l.root_session_id, l.launch_ts) > 1 AS tied_start
            FROM ah.loop_run l JOIN ah.session s ON s.id = l.root_session_id
            LEFT JOIN ah.message m ON m.session_id = s.id
                AND l.launch_uid = s.agent || '/' || s.session_uid || '/' || s.agent_id || ':' || m.event_uid
            WHERE l.launch_ts IS NOT NULL
        )
        SELECT l.launch_uid, l.root_session_id, l.number, l.goal_path, l.report_path,
               l.launch_ts, l.text, l.following, l.window_known, l.tied_start
        FROM ordered_launches l JOIN ah.loops live ON live.launch_uid = l.launch_uid
        WHERE %s OR live.status = 'running' OR EXISTS (
            SELECT 1 FROM dirty_now d JOIN ah.session changed ON changed.id = d.session_id
            WHERE COALESCE(changed.root_session_id, changed.id) = l.root_session_id
        ) OR EXISTS (
            SELECT 1 FROM ah.loop_receipt r WHERE r.kind = 'started' AND r.path = l.goal_path
              AND r.seen_at >= %s::timestamptz - interval '2 hours'
        ) OR l.launch_uid = ANY(%s)
    """,
        (initial, mark[0] if mark else "-infinity", list(finished)),
    ).fetchall()
    for uid, root, number, goal_path, report_path, started, launch_text, following, window_known, tied_start in rows:
        fields = identity_fields(launch_text or "", number, goal_path)
        lane_counts = None  # no captured report yet
        # Match _tag_root's (ts, id) order before filtering refresh candidates. Missing
        # launch messages make report ownership uncertain, not permission to guess it.
        if report_path and window_known:
            for tool, raw in conn.execute(
                """
                SELECT i.tool_name, i.input_text FROM ah.tool_io i
                JOIN ah.tool_call t ON t.agent = i.agent AND t.call_uid = i.call_uid
                JOIN ah.session s ON s.id = i.session_id
                WHERE (s.id = %s OR s.root_session_id = %s) AND i.ts >= %s
                  AND (NOT %s OR i.ts > %s)
                  AND (%s::timestamptz IS NULL OR i.ts < %s)
                  AND t.outcome = 'ok' AND t.ended_at IS NOT NULL
                  AND lower(i.tool_name) ~ '(^|[.])write$'
                  AND NOT COALESCE(i.input_truncated, false)
                ORDER BY i.ts, i.id
            """,
                (root, root, started, tied_start, started, following, following),
            ):
                # Tool IO ids and message ids have no shared ordering. A write at a
                # tied launch timestamp may precede the final launch; ignore it.
                if (tool or "").rsplit(".", 1)[-1].lower() != "write":
                    continue
                try:
                    args = json.loads(raw or "")
                except (ValueError, RecursionError):
                    continue
                if not isinstance(args, dict):
                    continue
                path = args.get("file_path", args.get("path"))
                content = args.get("content")
                if path == report_path and isinstance(content, str) and content.startswith("# Loop: "):
                    # Latest observed report identity supersedes launch metadata, including null
                    # for a conflicting Data block or a legacy basename without an owner.
                    fields = identity_fields(content, number, report=True)
                    lane_counts = report_lane_counts(content) or (None, None)
        fields = _receipt_identity(
            conn, uid, goal_path, f"loop{number}" if number is not None else None, fields, started
        )
        conn.execute(
            "UPDATE ah.loops SET repo = %s, loop = %s, goal_sha256 = %s WHERE launch_uid = %s "
            "AND (repo, loop, goal_sha256) IS DISTINCT FROM (%s, %s, %s)",
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
        if lane_counts is not None:  # the latest captured report's Data, NULL when it has no exact count
            conn.execute(
                "UPDATE ah.loops SET lanes_reported = %s WHERE launch_uid = %s AND lanes_reported IS DISTINCT FROM %s",
                (lane_counts[1], uid, lane_counts[1]),
            )
    if initial:
        conn.execute(
            "INSERT INTO ah.meta (key, value) VALUES ('loops_identity_projection_v1', '1'), "
            "('loops_lanes_projection_v1', '1') ON CONFLICT (key) DO NOTHING"
        )
    if newest is not None:
        conn.execute(
            "INSERT INTO ah.meta (key, value) VALUES ('loops_receipt_identity_seen', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (newest.isoformat(),),
        )


PROGRESS_SELECTION_SQL = """
    WITH changed AS MATERIALIZED (
        SELECT s.id, s.root_session_id, s.loop_run_id
        FROM dirty_now d CROSS JOIN LATERAL (
            SELECT id, root_session_id, loop_run_id FROM ah.session WHERE id = d.session_id OFFSET 0
        ) s
    ), dirty_owners AS (
        SELECT owned.launch_uid FROM changed c CROSS JOIN LATERAL (
            SELECT launch_uid FROM ah.loop_run
            WHERE root_session_id = COALESCE(c.root_session_id, c.id) OFFSET 0
        ) owned
        UNION
        SELECT owned.launch_uid FROM changed c CROSS JOIN LATERAL (
            SELECT launch_uid FROM ah.loop_run WHERE id = c.loop_run_id OFFSET 0
        ) owned
        UNION
        SELECT owned.launch_uid FROM changed c CROSS JOIN LATERAL (
            SELECT l.launch_uid FROM ah.session parent JOIN ah.loop_run l ON l.id = parent.loop_run_id
            WHERE parent.id = c.root_session_id OFFSET 0
        ) owned
    )
    SELECT launch_uid FROM ah.loops WHERE status = 'running'
    UNION
    SELECT launch_uid FROM dirty_owners
"""


def _refresh_progress(conn: psycopg.Connection) -> None:
    """Refresh active/dirty loops plus at most 128 historical rows per transaction.

    The cursor is transactional with the projection. Existing catalogues therefore backfill on
    scheduled passes without rebuild, but never repeatedly aggregate all historical transcripts.
    Root calls use the launch window; linked lane sessions contribute whole, as v_loop_summary does.
    Unlike that view's display defaults, unknown totals remain null and costs must be fully priced.
    """
    key = "loops_progress_projection_v1"
    saved = conn.execute("SELECT value FROM ah.meta WHERE key = %s", (key,)).fetchone()
    cursor = saved[0] if saved else ""
    historical = []
    if cursor != "complete":
        historical = [
            r[0]
            for r in conn.execute(
                "SELECT launch_uid FROM ah.loops WHERE launch_uid > %s ORDER BY launch_uid LIMIT 128",
                (cursor,),
            )
        ]
    active = [r[0] for r in conn.execute(PROGRESS_SELECTION_SQL)]
    for uid in dict.fromkeys(historical + active):
        conn.execute(
            """
            WITH target AS (
                SELECT l.id, l.root_session_id, l.launch_ts,
                       CASE WHEN live.status = 'finished' THEN l.end_ts END AS end_ts
                FROM ah.loop_run l JOIN ah.loops live USING (launch_uid) WHERE l.launch_uid = %s
            ), members AS (
                SELECT s.id, s.last_event_at, s.agent,
                       (s.id = l.root_session_id OR s.loop_link_method = 'relaunch') AS is_root,
                       l.launch_ts, l.end_ts
                FROM target l JOIN ah.session s ON s.id = l.root_session_id OR
                    (s.loop_run_id = l.id AND s.id <> l.root_session_id)
            ), calls AS (
                SELECT c.* FROM members s JOIN ah.llm_call c ON c.session_id = s.id
                WHERE NOT s.is_root OR (c.ts >= s.launch_ts AND c.ts < COALESCE(s.end_ts, 'infinity'))
            ), usage AS (
                SELECT NULLIF(count(*), 0) AS llm_calls,
                       CASE WHEN count(input_uncached) = count(*) THEN sum(input_uncached) END AS input_uncached,
                       CASE WHEN count(cache_read) = count(*) THEN sum(cache_read) END AS cache_read,
                       CASE WHEN count(cache_write_5m) = count(*) AND count(cache_write_1h) = count(*)
                            THEN sum(cache_write_5m + cache_write_1h) END AS cache_write,
                       CASE WHEN count(output) = count(*) THEN sum(output) END AS output,
                       CASE WHEN count(*) > 0 THEN count(*) FILTER (WHERE is_api_error) END AS api_errors
                FROM calls
            ), priced AS (
                SELECT CASE WHEN input_uncached IS NOT NULL AND cache_read IS NOT NULL
                                 AND cache_write_5m IS NOT NULL AND cache_write_1h IS NOT NULL
                                 AND output IS NOT NULL
                            THEN ah.priced_usd(model, ts::date, input_uncached, cache_read,
                                               cache_write_5m, cache_write_1h, output) END AS cost FROM calls
            ), costs AS (
                SELECT CASE WHEN count(cost) = count(*) THEN sum(cost) END AS cost FROM priced
            ), tools AS (
                SELECT CASE WHEN count(*) > 0 AND count(t.outcome) = count(*)
                            THEN count(*) FILTER (WHERE t.outcome = 'error') END AS errors
                FROM members s JOIN ah.tool_call t ON t.session_id = s.id
                WHERE NOT s.is_root OR (t.started_at >= s.launch_ts AND
                                       t.started_at < COALESCE(s.end_ts, 'infinity'))
            ), git AS (
                SELECT NULLIF(count(*) FILTER (WHERE g.op IN ('commit','cherry_pick')), 0) AS commits,
                       NULLIF(count(*) FILTER (WHERE g.op = 'push'), 0) AS pushes
                FROM members s JOIN ah.git_event g ON g.session_id = s.id
                WHERE NOT s.is_root OR (g.ts >= s.launch_ts AND g.ts < COALESCE(s.end_ts, 'infinity'))
            ), lanes AS (
                SELECT NULLIF(count(*), 0) AS total,
                       NULLIF(count(*) FILTER (WHERE lane_return IS NOT NULL), 0) AS returned
                FROM ah.lane WHERE loop_run_id = (SELECT id FROM target)
            )
            UPDATE ah.loops SET lanes_total = lanes.total, lanes_returned = lanes.returned,
                last_activity_at = (SELECT max(last_event_at) FROM members),
                llm_calls = usage.llm_calls, input_uncached = usage.input_uncached,
                cache_read = usage.cache_read, cache_write = usage.cache_write, output = usage.output,
                priced_cost_usd = costs.cost, tool_errors = tools.errors, api_errors = usage.api_errors,
                commits = git.commits, pushes = git.pushes,
                root_agent = (SELECT agent FROM ah.session WHERE id = (SELECT root_session_id FROM target))
            FROM usage, costs, tools, git, lanes WHERE launch_uid = %s
        """,
            (uid, uid),
        )
    _refresh_tasks_done(conn)
    if cursor != "complete":
        following = historical[-1] if len(historical) == 128 else "complete"
        conn.execute(
            "INSERT INTO ah.meta (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, following),
        )


def _refresh_tasks_done(conn: psycopg.Connection) -> None:
    """Distinct backlog tasks that moved to Done in the loop's repo within [launch_ts, end_ts).

    Whatever session made the change: the evidence is the repo's own history, collected into
    ah.backlog_done_event. Only a running loop's window is open; a stale loop's ends at its end_ts, the
    root's last observed activity. NULL when the loop's repo is unknown or the collector has not yet
    finished that repo's first Done scan (ah.backlog_done_scan); a scanned repo gets 0, not NULL.
    Collector rows can arrive after a loop finishes, so every pass recomputes every loop.
    """
    conn.execute("""
        WITH tracked AS (
            SELECT DISTINCT lower(regexp_replace(repo_slug, '^[^/]+/', '')) AS slug FROM ah.backlog_done_scan
        ), counted AS (
            SELECT l.launch_uid,
                   CASE WHEN EXISTS (SELECT 1 FROM tracked r WHERE r.slug = lower(l.repo))
                        THEN (SELECT count(DISTINCT d.task_key) FROM ah.backlog_done_event d
                              WHERE lower(regexp_replace(d.repo_slug, '^[^/]+/', '')) = lower(l.repo)
                                AND d.done_at >= l.launch_ts
                                AND d.done_at < COALESCE(l.end_ts, 'infinity'))
                   END AS n
            FROM ah.loops l WHERE l.repo IS NOT NULL AND l.launch_ts IS NOT NULL
        )
        UPDATE ah.loops l SET tasks_done = c.n
        FROM counted c WHERE l.launch_uid = c.launch_uid AND l.tasks_done IS DISTINCT FROM c.n
    """)


# Frozen loop-state v1 validate_event schema, from public bin/loop-state.
# Source SHA-256: f44713f19942be7296feb377c79c6e9db4310c0796a0f4869dcd5ecadba00561.
# No runtime checkout/CLI dependency. Retained framing also requires real timestamps,
# an exact goal hash and one open. Invalid heartbeat activity is independently unknown.
_STATE_TIER = ("routine", "guarded")
_STATE_SCHEMA = {
    "open": (
        {
            "goal_sha256": "str",
            "tier": _STATE_TIER,
            "root": ("llm", "dispatcher"),
            "root_model": "str",
            "envelope": "strlist",
        },
        {},
    ),
    "admit": (
        {"task": "str", "source": ("envelope", "loop-created"), "owned": "owned", "accept": "str"},
        {"tier": _STATE_TIER, "surfaces": "strlist"},
    ),
    "dispatch": (
        {"lane": "str", "task": "str", "agent": "str", "run": "str", "base": "str"},
        {
            "deadline": "str",
            "model": "str",
            "effort": "str",
            "surface": "str",
            "tier": _STATE_TIER,
            "tasks": "strlist",
            "kind": ("work", "gate", "triage", "review"),
            "recovery_of": "str",
        },
    ),
    "return": (
        {"lane": "str", "run": "str", "status": ("complete", "partial", "blocked", "failed")},
        {
            "sha": "str?",
            "landed": "bool",
            "check": "str",
            "exit": "int?",
            "ci": "str?",
            "coderabbit": "coderabbit",
            "base": "str",
            "questions": "strlist",
        },
    ),
    "gate": (
        {"scope": ("lane", "composed"), "sha": "str", "cmd": "str", "exit": "int?"},
        {"lane": "str", "base": "str"},
    ),
    "accept": ({"task": "str", "accepted": "bool", "reason": "str"}, {"lane": "str"}),
    "park": (
        {
            "task": "str",
            "reason": "str",
            "needs": ("owner", "authority", "evidence-later", "dependency", "defect", "budget"),
        },
        {"lane": "str", "until": "str"},
    ),
    "land": (
        {"task": "str", "sha": "str", "gate": "str", "mode": ("after-green", "pre-green")},
        {"lane": "str", "ci": "str"},
    ),
    "revert": (
        {"sha": "str", "reverted_by": "str", "reason": ("ci-red", "ci-timeout", "composed-red", "root-decision")},
        {"task": "str"},
    ),
    "watch": ({"op": ("start", "stop"), "what": "str", "deadline": "str"}, {}),
    "heartbeat": ({"at": "str"}, {}),
    "judgement": ({"text": "str"}, {"task": "str", "lane": "str"}),
    "close": ({"reason": ("nothing-admissible", "owner-stop", "blocked", "budget")}, {}),
}


def _state_value_valid(kind, value) -> bool:
    if isinstance(kind, tuple):
        return isinstance(value, str) and value in kind
    if kind.endswith("?"):
        return value is None or _state_value_valid(kind[:-1], value)
    if kind == "str":
        return isinstance(value, str) and bool(value)
    if kind == "int":
        return type(value) is int
    if kind == "bool":
        return type(value) is bool
    if kind == "strlist":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if kind == "owned":
        return _state_value_valid("str", value) or _state_value_valid("strlist", value)
    if kind == "coderabbit":
        return value is None or (
            isinstance(value, dict)
            and type(value.get("ran")) is bool
            and type(value.get("major")) is int
            and type(value.get("unreviewed")) is int
        )
    return False


def _state_body_valid(event) -> bool:
    schema = _STATE_SCHEMA.get(event["ev"])
    if schema is None:
        return False
    required, optional = schema
    if set(event) - required.keys() - optional.keys() - {"ev", "v", "seq", "ts", "by"}:
        return False
    # An invalid/missing heartbeat at does not erase independent complete count evidence.
    if event["ev"] == "heartbeat":
        return True
    if any(key not in event or not _state_value_valid(kind, event[key]) for key, kind in required.items()):
        return False
    if any(key in event and not _state_value_valid(kind, event[key]) for key, kind in optional.items()):
        return False
    if event["ev"] == "judgement":
        try:
            return len(event["text"].encode("utf-8")) <= 4096
        except UnicodeEncodeError:
            return False
    return True


class _StateEvents(list):
    """Projected observations with a complete pre-filter framed-event fingerprint ledger."""

    def __init__(self, events=(), *, ledger=None):
        super().__init__(events)
        self.ledger = {} if ledger is None else ledger


def _state_conflicts(cohorts) -> set[str]:
    """Event kinds involved in disagreement at the same frozen open/sequence position."""
    observed = {}
    conflicts = set()
    for opened, events in cohorts:
        ledger = getattr(events, "ledger", None)
        if ledger is None:
            ledger = {e["seq"]: (e["ev"], json.dumps(e, sort_keys=True, default=str)) for e in events}
        for seq, value in ledger.items():
            copies = observed.setdefault((opened, seq), set())
            copies.add(value)
            if len(copies) > 1:
                conflicts.update(kind for kind, _ in copies)
    return conflicts


def state_progress(content: str) -> tuple[str, datetime, list[dict[str, Any]]] | None:
    """Read frozen v1 framing, count evidence and explicitly recorded root heartbeats.

    Preserve source bytes in the collector. A torn JSON object has no event and is ignored, as
    by loop-state check. Other invalid framing or malformed count evidence leaves counts unknown.
    """
    return _read_state_progress(content)


def _read_state_progress(content: str, *, identity_only: bool = False):
    """Share full validation with the prefix identity used only to invalidate bad copies."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate state member")
            result[key] = value
        return result

    def string(event, key):
        return isinstance(event.get(key), str) and bool(event[key])

    events = _StateEvents()
    opened = None
    previous = 0
    for raw in content.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, RecursionError):
            # A cut-short object is not an observed event. A complete malformed object is invalid.
            if raw.lstrip().startswith("{") and not raw.rstrip().endswith("}"):
                continue
            return None
        if not isinstance(event, dict):
            return None
        seq, ts = event.get("seq"), event.get("ts")
        if (
            type(event.get("v")) is not int
            or event["v"] != 1
            or type(seq) is not int
            or seq != previous + 1
            or event.get("by") not in ("root", "dispatcher", "ext", "daemon")
            or not isinstance(ts, str)
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ts)
            or not string(event, "ev")
        ):
            return None
        try:
            at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        previous = seq
        ev = event["ev"]
        if not _state_body_valid(event):
            return None
        # Capture every frame, including return, open, non-root close and invalid
        # heartbeat activity, before any projection, actor or launch-window filter.
        events.ledger[seq] = (ev, json.dumps(event, sort_keys=True))
        if ev == "open":
            goal = event.get("goal_sha256")
            if opened is not None or not isinstance(goal, str) or not re.fullmatch(r"[0-9a-f]{64}", goal):
                return None
            opened = (goal, at)
            if identity_only:
                # The entire prefix through this open is valid. Never expose prefix counts.
                return (*opened, [])
        elif ev == "heartbeat":
            activity = event.get("at")
            # loop-state framing ts is append observation time. Root activity is the
            # separate UTC at member, preserving the producer's subsecond precision.
            if not isinstance(activity, str) or not re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", activity
            ):
                continue
            try:
                activity_at = datetime.fromisoformat(activity.replace("Z", "+00:00"))
            except ValueError:
                continue
            events.append({**event, "at": activity_at, "_observed_at": at})
        elif ev in ("dispatch", "accept", "land", "admit", "park"):
            events.append({**event, "at": at})
        elif ev == "close":
            # Neither a pre-open close nor a worker/daemon observation establishes an end.
            if opened is not None and event["by"] in ("root", "dispatcher"):
                events.append({**event, "at": at})
    return (*opened, events) if opened else None


def _state_event_counts(cohorts: list[tuple[datetime, list[dict[str, Any]]]]) -> tuple[int | None, int | None]:
    """Correlate exact tasks using preceding recorded dispatches, never return status or role.

    Copies sharing an open timestamp share the same frozen sequence. Union their dispatch evidence
    before resolving accepts so identical copies deduplicate and conflicting copies stay ambiguous.
    Different opens do not supply each other's sequence ordering.
    """
    if any(e["ev"] == "invalid" for _, events in cohorts for e in events):
        return None, None
    dispatches: dict[datetime, dict[str, dict[int, set[str]]]] = {}
    for opened, events in cohorts:
        for event in events:
            if event["ev"] == "dispatch":
                task_dispatches = dispatches.setdefault(opened, {}).setdefault(event["task"], {})
                task_dispatches.setdefault(event["seq"], set()).add(event["lane"])
    conflicts = _state_conflicts(cohorts)
    accepted, landed = set(), set()
    unresolved = bool(conflicts & {"open", "accept", "dispatch"})
    for opened, events in cohorts:
        for event in events:
            if event["ev"] == "accept" and event["accepted"] is True:
                if lane := event.get("lane"):
                    accepted.add(lane)
                    continue
                candidates = set()
                for seq, lanes in dispatches.get(opened, {}).get(event["task"], {}).items():
                    if seq < event["seq"]:
                        candidates.update(lanes)
                if len(candidates) == 1:
                    accepted.update(candidates)
                else:
                    unresolved = True
            elif event["ev"] == "land":
                landed.add(event["task"])
    return (None if unresolved else len(accepted)), (None if conflicts & {"open", "land"} else len(landed))


def state_cohorts(
    conn: psycopg.Connection, launch_uid: str | None = None
) -> dict[str, list[tuple[datetime, list[dict[str, Any]]]]]:
    """Existing exact origin/loop/open-goal attribution, shared by counts and heartbeats.

    Identity must already be observed. Local paths, proximity and collector mtimes do not
    select a root. Compute competing launch windows before selecting an optional target.
    """
    rows = conn.execute(
        """
        SELECT launch_uid, lower(repo), loop, goal_sha256, launch_ts,
               lead(launch_ts) OVER (
                   PARTITION BY lower(repo), loop, goal_sha256 ORDER BY launch_ts, launch_uid
               ) AS following,
               count(*) OVER (PARTITION BY lower(repo), loop, goal_sha256, launch_ts) > 1 AS tied
        FROM ah.loops WHERE repo IS NOT NULL AND loop IS NOT NULL AND goal_sha256 IS NOT NULL
          AND (%s::text IS NULL OR (lower(repo), loop, goal_sha256) = (
              SELECT lower(repo), loop, goal_sha256 FROM ah.loops WHERE launch_uid = %s
          ))
        """,
        (launch_uid, launch_uid),
    ).fetchall()
    windows = {}
    for uid, repo, label, goal, started, following, _ in rows:
        if started is not None:
            windows.setdefault((repo, label, goal), []).append((uid, started - START_SKEW, following))
    snapshots = {}
    result = {}
    for uid, repo, label, goal, started, following, tied in rows:
        if tied or started is None or launch_uid is not None and uid != launch_uid:
            continue
        key = (repo, label)
        if key not in snapshots:
            snapshots[key] = []
            for (content,) in conn.execute(
                "SELECT content FROM ah.loop_state WHERE lower(repo_origin) = %s AND loop = %s",
                key,
            ):
                parsed = state_progress(content)
                if parsed is not None:
                    snapshots[key].append(parsed)
                    continue
                # A valid prefix through the first open can attribute a later-invalid snapshot.
                # Do not silently use a stale valid machine copy as complete outcome proof.
                prefix = _read_state_progress(content, identity_only=True)
                if prefix is not None:
                    digest, opened, _ = prefix
                    snapshots[key].append((digest, opened, [{"ev": "invalid", "seq": 0, "at": opened}]))
        cohorts = []
        for digest, opened, events in snapshots[key]:
            if digest != goal:
                continue
            attributed = [
                candidate
                for candidate, earliest, cutoff in windows[(repo, label, goal)]
                if opened >= earliest and (cutoff is None or opened < cutoff)
            ]
            if attributed != [uid]:
                continue
            cohorts.append(
                (
                    opened,
                    _StateEvents(
                        [
                            event
                            for event in events
                            if (event["ev"] == "invalid" or event["at"] >= started)
                            and (not following or event["at"] < following)
                            and (
                                event["ev"] != "heartbeat"
                                or event["_observed_at"] >= started
                                and (not following or event["_observed_at"] < following)
                            )
                        ],
                        ledger=getattr(events, "ledger", {}),
                    ),
                )
            )
        if cohorts:
            result[uid] = cohorts
    return result


TRACKER_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+(?:\.[0-9]+)*")


def state_outcomes(cohorts: list[tuple[datetime, list[dict[str, Any]]]]) -> tuple[int | None, int | None, int | None]:
    """Explicit tracker admission/publication and cumulative parks, not the digest's live fold.

    Sequence and open timestamp identify an event across machine snapshots. Repeated parks at
    different sequence positions count separately. Conflicting observations of a relevant event
    leave that outcome unknown rather than selecting a newer machine or adding both copies.
    """
    if not cohorts or any(e["ev"] == "invalid" for _, events in cohorts for e in events):
        return None, None, None
    conflicts = _state_conflicts(cohorts)
    result = []
    for kind in ("admit", "land", "park"):
        if kind in conflicts or "open" in conflicts:
            result.append(None)
        elif kind == "park":
            result.append(len({(opened, e["seq"]) for opened, events in cohorts for e in events if e["ev"] == kind}))
        else:
            tasks = {
                event["task"]
                for _, events in cohorts
                for event in events
                if event["ev"] == kind
                and TRACKER_ID.fullmatch(event["task"])
                and (kind != "land" or event["by"] in ("root", "dispatcher"))
            }
            result.append(len(tasks))
    return tuple(result)


def _refresh_state_counts(conn: psycopg.Connection) -> None:
    """Project qualified state outcomes on every pass, even closed collector-only updates.

    Missing evidence is NULL, never a report/transcript fallback. Lifecycle is refreshed before
    this function, so loss or invalidation of a state snapshot cannot retain a guessed close.
    """
    qualified = state_cohorts(conn)
    conn.execute(
        "UPDATE ah.loops SET lanes_accepted=NULL,tasks_admitted=NULL,tasks_landed=NULL,parks_total=NULL "
        "WHERE NOT (launch_uid = ANY(%s)) AND "
        "(lanes_accepted IS NOT NULL OR tasks_admitted IS NOT NULL OR tasks_landed IS NOT NULL OR parks_total IS NOT NULL)",
        (list(qualified),),
    )
    for uid, cohorts in qualified.items():
        accepted, landed = _state_event_counts(cohorts)
        admitted, published, parks = state_outcomes(cohorts)
        conn.execute(
            "UPDATE ah.loops SET lanes_accepted=%s,tasks_admitted=%s,tasks_landed=%s,parks_total=%s "
            "WHERE launch_uid=%s",
            (accepted, admitted, published, parks, uid),
        )
        if landed is not None:  # Preserve the separate legacy Done-flip fallback on invalid state.
            conn.execute("UPDATE ah.loops SET tasks_done=%s WHERE launch_uid=%s", (landed, uid))
        # All copies must agree at shared sequence positions before a close is terminal.
        closes = []
        for opened, events in cohorts:
            for event in events:
                if event["ev"] == "close" and event["at"] >= opened:
                    closes.append(event["at"])
        if (
            closes
            and not any(e["ev"] == "invalid" for _, events in cohorts for e in events)
            and not _state_conflicts(cohorts)
        ):
            closed = min(closes)
            conn.execute(
                "UPDATE ah.loop_run SET end_ts=%s,end_evidence='state_close' "
                "WHERE launch_uid=%s AND end_evidence IN ('root_last_event','state_close')",
                (closed, uid),
            )
            conn.execute(
                "UPDATE ah.loops SET status='finished',end_ts=CASE WHEN status='finished' THEN end_ts ELSE %s END "
                "WHERE launch_uid=%s",
                (closed, uid),
            )


def _tag_root(conn: psycopg.Connection, root_id: int) -> int:
    root = conn.execute(
        "SELECT cwd, last_event_at, agent, session_uid, agent_id FROM ah.session WHERE id = %s", (root_id,)
    ).fetchone()
    if root is None:
        return 0
    cwd, root_last, agent, session_uid, agent_id = root
    root_key = f"{agent}/{session_uid}/{agent_id}"  # natural key: launch_uid survives rebuild
    launches: list[tuple[str, Any, dict[str, Any]]] = []
    for event_uid, ts, text in conn.execute(
        "SELECT event_uid, ts, text FROM ah.message WHERE session_id = %s "
        "AND message_class IN ('human_prompt','queued_prompt') ORDER BY ts, id",
        (root_id,),
    ):
        if "root" not in text and "launch-" not in text:
            continue
        hit = _launch(text, cwd, ts.isoformat().replace("+00:00", "Z"))
        if hit:
            launches.append((event_uid, ts, hit))
    for index, (event_uid, ts, hit) in enumerate(launches):
        meta = _report_meta(hit.get("report"))
        next_ts = launches[index + 1][1] if index + 1 < len(launches) else None
        captured = _captured_launch(conn, root_id, hit, ts, next_ts)
        if captured:
            base = str(PurePosixPath(hit["launch_path"]).parent.parent)
            resolved = _launch(captured, base, ts.isoformat())
            if resolved:
                hit = {**resolved, "launch_path": hit["launch_path"]}
        end_ts, evidence = None, None
        if end_ts is None and next_ts is not None:
            end_ts, evidence = next_ts, "next_launch"
        if end_ts is None:
            end_ts, evidence = root_last, "root_last_event"
        loop_id = conn.execute(
            "INSERT INTO ah.loop_run (launch_uid, root_session_id, status, repo_slug, campaign_slug, loop_number, "
            "naming, mode, report_path, goal_path, launch_path, launch_sha256, launch_ts, end_ts, end_evidence, budget_s) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (launch_uid) DO UPDATE SET end_ts = EXCLUDED.end_ts, end_evidence = EXCLUDED.end_evidence, "
            "status = EXCLUDED.status, report_path = EXCLUDED.report_path, loop_number = EXCLUDED.loop_number, "
            "repo_slug = EXCLUDED.repo_slug, campaign_slug = EXCLUDED.campaign_slug, naming = EXCLUDED.naming, "
            "goal_path = EXCLUDED.goal_path, launch_path = EXCLUDED.launch_path, "
            "launch_sha256 = EXCLUDED.launch_sha256, mode = EXCLUDED.mode, budget_s = EXCLUDED.budget_s RETURNING id",
            (
                f"{root_key}:{event_uid}",
                root_id,
                hit["status"],
                meta.get("repo_slug"),
                meta.get("campaign_slug"),
                hit.get("loop"),
                meta.get("naming"),
                hit.get("mode"),
                hit.get("report"),
                meta.get("goal_path"),
                hit.get("launch_path"),
                hit.get("launch_sha256"),
                ts,
                end_ts,
                evidence,
                hit.get("budget"),
            ),
        ).fetchone()[0]
        _tag_tree(conn, loop_id, root_id, ts, next_ts)
    return len(launches)


def _tag_tree(conn: psycopg.Connection, loop_id: int, root_id: int, start: Any, end: Any) -> None:
    conn.execute(
        "UPDATE ah.session SET loop_run_id = %s, loop_link_method = 'launch' WHERE id = %s "
        "AND (loop_run_id IS NULL OR loop_run_id = %s)",
        (loop_id, root_id, loop_id),
    )
    members = conn.execute(
        "UPDATE ah.session s SET loop_run_id = %s, loop_link_method = 'lineage' "
        "WHERE s.root_session_id = %s AND s.id <> %s AND s.first_event_at >= %s "
        "AND s.loop_link_method IS DISTINCT FROM 'relaunch' "
        "AND (%s::timestamptz IS NULL OR s.first_event_at < %s) "
        "AND (s.loop_run_id IS NULL OR s.loop_run_id = %s) RETURNING s.id",
        (loop_id, root_id, root_id, start, end, end, loop_id),
    ).fetchall()
    member_ids = [m[0] for m in members]
    # Heuristic: Codex exec sessions started while a lineage session ran a Codex command in the same
    # cwd tree and inside the loop window.
    heuristic = conn.execute(
        "UPDATE ah.session x SET loop_run_id = %s, loop_link_method = 'heuristic' "
        "FROM ah.tool_call t JOIN ah.session ts ON ts.id = t.session_id "
        "WHERE (ts.id = %s OR ts.root_session_id = %s) AND t.meta->>'cmd_verb' = 'codex' "
        "AND x.agent = 'codex' AND x.entrypoint = 'codex_exec' AND x.loop_run_id IS NULL "
        "AND x.first_event_at BETWEEN t.started_at AND COALESCE(t.ended_at, t.started_at + interval '2 hours') "
        "AND t.started_at >= %s AND (%s::timestamptz IS NULL OR t.started_at <= %s) "
        "AND (x.cwd = ts.cwd OR x.cwd LIKE ts.cwd || '/%%' OR ts.cwd LIKE x.cwd || '/%%') RETURNING x.id",
        (loop_id, root_id, root_id, start, end, end),
    ).fetchall()
    for sid, method in [(m, "lineage") for m in member_ids] + [(h[0], "heuristic") for h in heuristic]:
        _lane(conn, loop_id, sid, method)


def parse_lane_return(text: str) -> tuple[Any, str | None]:
    """(lane_return, return_status) of the last ```lane-return block in a lane's final message.

    Two shapes, one projection: the earlier free-form object (no `v` key) keeps any string `status`; the
    v2 object (`"v"` the integer 2, with `lane` and `status` in complete|partial|blocked|failed) is stored
    whole and gives return_status only when both are valid. An object with any other `v` is stored whole
    with no return_status. A block that is not a JSON object is {"unparsed": true}. CRLF line endings
    and a closing fence on the JSON's last line are accepted.
    """
    text = text or ""
    value: Any = None
    pos = 0
    while opened := LANE_OPEN.search(text, pos):
        body = opened.end()
        start = len(text) - len(text[body:].lstrip())
        try:
            candidate, end = json.JSONDecoder().raw_decode(text, start)
            close = LANE_CLOSE.match(text, end)
        except (ValueError, RecursionError):
            close = None
        if close is None:
            # not one JSON value then a fence: the block runs to the next fence that ends a line
            close = FENCE_LINE_END.search(text, body)
            if close is None:
                break
            candidate = None
        value = candidate if isinstance(candidate, dict) else UNPARSED
        pos = close.end()
    if value is None or value is UNPARSED:
        return (None if value is None else {"unparsed": True}), None
    status = value.get("status")
    if "v" in value:
        # a versioned object: only the integer 2 is v2, and an unknown version never falls back to the
        # free-form shape
        valid = (
            type(value["v"]) is int
            and value["v"] == 2
            and isinstance(value.get("lane"), str)
            and status in LANE_STATUS_V2
        )
        return value, status if valid else None
    return value, status if isinstance(status, str) else None


def _lane(conn: psycopg.Connection, loop_id: int, session_id: int, method: str) -> None:
    info = conn.execute(
        "SELECT s.agent_type, s.agent_role, s.agent_path, sp.name, sp.child_task_name, sp.requested_type, s.agent "
        "FROM ah.session s LEFT JOIN ah.subagent_spawn sp ON sp.child_session_id = s.id WHERE s.id = %s LIMIT 1",
        (session_id,),
    ).fetchone()
    agent_type, agent_role, agent_path, spawn_name, task_name, requested, agent = info or (None,) * 7
    if agent == "pi":
        # a pi child's agent_path and spawn child_task_name end in "<run dir>/run-<i>", never a
        # lane name: the spawn's workflow key or agent, else the child's own agent type
        name = spawn_name or agent_type
    else:
        name = spawn_name or task_name or (agent_path.rsplit("/", 1)[-1] if agent_path else None)
    if not name:
        brief = conn.execute(
            "SELECT m.text FROM ah.subagent_spawn sp JOIN ah.message m ON m.event_uid = sp.spawn_uid || ':brief' "
            "WHERE sp.child_session_id = %s LIMIT 1",
            (session_id,),
        ).fetchone()
        if brief:
            m = LANE_LINE.search(brief[0][:2000])
            name = m.group(1) if m else None
    lane_return, return_status = None, None
    report = conn.execute(
        "SELECT text FROM ah.message WHERE session_id = %s AND message_class = 'subagent_report' "
        "ORDER BY ts DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if report:
        lane_return, return_status = parse_lane_return(report[0])
    conn.execute(
        "INSERT INTO ah.lane (loop_run_id, session_id, lane_name, role, link_method, return_status, lane_return) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (loop_run_id, session_id) DO UPDATE SET "
        "lane_name = COALESCE(EXCLUDED.lane_name, lane.lane_name), role = COALESCE(EXCLUDED.role, lane.role), "
        "return_status = COALESCE(EXCLUDED.return_status, lane.return_status), "
        "lane_return = COALESCE(EXCLUDED.lane_return, lane.lane_return)",
        (
            loop_id,
            session_id,
            name,
            agent_type or agent_role or requested,
            method,
            return_status,
            Jsonb(lane_return) if lane_return is not None else None,
        ),
    )
