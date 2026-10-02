"""Loop/campaign tagging post-pass.

Runs inside load.post_passes' transaction over the roots of dirty sessions. Launches are found in
the root's operator text (human/queued prompts) with `loop_launch.parse_launch`. The indexer
never reads launch or report files (they may live on another machine), so:
- a bare `launch-*.txt|md` path launch is stored as status 'unresolved_path', never dropped;
- the loop end comes from transcript evidence: a write of the report path, else the next launch
  in the same root session, else the root session's last event.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import PurePosixPath
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .loop_launch import identity_fields, parse_launch

ACTIVATED_AT = "1970-01-01T00:00:00Z"   # launches before this instant are ignored
BARE_LAUNCH = re.compile(r"^`?\s*(\S*launch-[^\s`/]*\.(?:txt|md))\s*`?$")
LANE_LINE = re.compile(r"^\s*Lane:\s*(\S+)", re.M)
LANE_RETURN = re.compile(r"```lane-return\s*\n(.*?)```", re.S)
CAMPAIGN = re.compile(r"^report-(.*?)-(?:loop|wave)\d+\.md$")


def _no_read(path: Any) -> str:  # noqa: ARG001
    raise OSError("the indexer does not read launch files")


def _launch(text: str, cwd: str | None, ts_iso: str) -> dict[str, Any] | None:
    try:
        found = parse_launch(text, cwd, ts_iso, ACTIVATED_AT, read_file=_no_read)
    except Exception:
        found = None
    if found:
        found["status"] = "resolved"
        return found
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
        return {"status": "unresolved_path", "launch_path": path, "report": report,
                "loop": int(loop.group(1)) if loop else None, "mode": None, "launch_sha256": None, "budget": None}
    return None


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


def run(conn: psycopg.Connection) -> dict[str, int]:
    roots = [r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(s.root_session_id, s.id) FROM dirty_now d "
        "JOIN ah.session s ON s.id = d.session_id")]
    found = 0
    for root_id in roots:
        found += _tag_root(conn, root_id)
    return {"loop_roots": len(roots), "launches": found}


def refresh_live(conn: psycopg.Connection) -> dict[str, int]:
    """Refresh lifecycle state even without dirty transcripts (silence ages on every pass).

    A report write or replacement launch is terminal evidence. Otherwise, root activity within
    24 hours is evidence of running, not proof of a live process. A silent root is stale and may
    resume. Do not reuse loop_run's fallback end timestamp as evidence of completion.
    """
    result = conn.execute("""
        INSERT INTO ah.loops (launch_uid, status, launch_ts, end_ts, observed_at)
        SELECT l.launch_uid,
               CASE WHEN l.end_evidence IN ('report_write', 'next_launch') AND l.end_ts IS NOT NULL
                    THEN 'finished'
                    WHEN greatest(s.last_event_at, l.launch_ts) >= now() - interval '24 hours'
                    THEN 'running' ELSE 'stale' END,
               l.launch_ts,
               CASE WHEN l.end_evidence IN ('report_write', 'next_launch') AND l.end_ts IS NOT NULL
                    THEN l.end_ts
                    WHEN greatest(s.last_event_at, l.launch_ts) >= now() - interval '24 hours'
                    THEN NULL ELSE greatest(s.last_event_at, l.launch_ts) END,
               now()
        FROM ah.loop_run l LEFT JOIN ah.session s ON s.id = l.root_session_id
        WHERE l.launch_ts IS NOT NULL
        ON CONFLICT (launch_uid) DO UPDATE SET status = EXCLUDED.status,
            launch_ts = EXCLUDED.launch_ts, end_ts = EXCLUDED.end_ts,
            observed_at = EXCLUDED.observed_at
    """)
    count = result.rowcount
    _refresh_identity(conn)
    return {"live_loops": count}


def _refresh_identity(conn: psycopg.Connection) -> None:
    """Re-project exact identity on every pass, including previously indexed launches.

    Only recorded metadata is authoritative: never read the current checkout or goal/report files.
    Report contents come from completed successful structured writes of the exact report path.
    Shell command strings are not interpreted. Keep this independent of terminal-evidence tagging.
    """
    initial = conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM ah.meta WHERE key = 'loops_identity_projection_v1')"
    ).fetchone()[0]
    rows = conn.execute("""
        SELECT l.launch_uid, l.root_session_id,
               CASE WHEN l.naming = 'loop' THEN l.loop_number END, l.goal_path, l.report_path,
               l.launch_ts, m.text,
               (SELECT min(n.launch_ts) FROM ah.loop_run n
                WHERE n.root_session_id = l.root_session_id AND n.launch_ts > l.launch_ts)
        FROM ah.loop_run l JOIN ah.session s ON s.id = l.root_session_id
        JOIN ah.loops live ON live.launch_uid = l.launch_uid
        LEFT JOIN ah.message m ON m.session_id = s.id
            AND l.launch_uid = s.agent || '/' || s.session_uid || '/' || s.agent_id || ':' || m.event_uid
        WHERE l.launch_ts IS NOT NULL AND (
            %s OR live.status = 'running' OR EXISTS (
                SELECT 1 FROM dirty_now d JOIN ah.session changed ON changed.id = d.session_id
                WHERE COALESCE(changed.root_session_id, changed.id) = l.root_session_id
            )
        )
    """, (initial,)).fetchall()
    for uid, root, number, goal_path, report_path, started, launch_text, following in rows:
        fields = identity_fields(launch_text or "", number, goal_path)
        if report_path:
            for tool, raw in conn.execute("""
                SELECT i.tool_name, i.input_text FROM ah.tool_io i
                JOIN ah.tool_call t ON t.agent = i.agent AND t.call_uid = i.call_uid
                JOIN ah.session s ON s.id = i.session_id
                WHERE (s.id = %s OR s.root_session_id = %s) AND i.ts >= %s
                  AND (%s::timestamptz IS NULL OR i.ts < %s)
                  AND t.outcome = 'ok' AND t.ended_at IS NOT NULL
                  AND lower(i.tool_name) ~ '(^|[.])write$'
                  AND NOT COALESCE(i.input_truncated, false)
                ORDER BY i.ts, i.id
            """, (root, root, started, following, following)):
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
        conn.execute(
            "UPDATE ah.loops SET repo = %s, loop = %s, goal_sha256 = %s WHERE launch_uid = %s "
            "AND (repo, loop, goal_sha256) IS DISTINCT FROM (%s, %s, %s)",
            (fields["repo"], fields["loop"], fields["goal_sha256"], uid,
             fields["repo"], fields["loop"], fields["goal_sha256"]),
        )
    if initial:
        conn.execute(
            "INSERT INTO ah.meta (key, value) VALUES ('loops_identity_projection_v1', '1') "
            "ON CONFLICT (key) DO NOTHING"
        )


def _tag_root(conn: psycopg.Connection, root_id: int) -> int:
    root = conn.execute("SELECT cwd, last_event_at, agent, session_uid, agent_id FROM ah.session WHERE id = %s",
                        (root_id,)).fetchone()
    if root is None:
        return 0
    cwd, root_last, agent, session_uid, agent_id = root
    root_key = f"{agent}/{session_uid}/{agent_id}"   # natural key: launch_uid survives rebuild
    launches: list[tuple[str, Any, dict[str, Any]]] = []
    for event_uid, ts, text in conn.execute(
            "SELECT event_uid, ts, text FROM ah.message WHERE session_id = %s "
            "AND message_class IN ('human_prompt','queued_prompt') ORDER BY ts, id", (root_id,)):
        if "root" not in text and "launch-" not in text:
            continue
        hit = _launch(text, cwd, ts.isoformat().replace("+00:00", "Z"))
        if hit:
            launches.append((event_uid, ts, hit))
    for index, (event_uid, ts, hit) in enumerate(launches):
        meta = _report_meta(hit.get("report"))
        next_ts = launches[index + 1][1] if index + 1 < len(launches) else None
        end_ts, evidence = None, None
        if hit.get("report"):
            row = conn.execute(
                "SELECT min(a.ts) FROM ah.artifact a JOIN ah.session s ON s.id = a.session_id "
                "WHERE (s.id = %s OR s.root_session_id = %s) AND a.path = %s AND a.ts >= %s "
                "AND a.action IN ('write','edit','create','update','add') AND (%s::timestamptz IS NULL OR a.ts < %s)",
                (root_id, root_id, hit["report"], ts, next_ts, next_ts)).fetchone()
            if row and row[0]:
                end_ts, evidence = row[0], "report_write"
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
            "goal_path = EXCLUDED.goal_path RETURNING id",
            (f"{root_key}:{event_uid}", root_id, hit["status"], meta.get("repo_slug"), meta.get("campaign_slug"),
             hit.get("loop"), meta.get("naming"), hit.get("mode"), hit.get("report"), meta.get("goal_path"),
             hit.get("launch_path"), hit.get("launch_sha256"), ts, end_ts, evidence, hit.get("budget")),
        ).fetchone()[0]
        _tag_tree(conn, loop_id, root_id, ts, end_ts)
    return len(launches)


def _tag_tree(conn: psycopg.Connection, loop_id: int, root_id: int, start: Any, end: Any) -> None:
    conn.execute("UPDATE ah.session SET loop_run_id = %s, loop_link_method = 'launch' WHERE id = %s "
                 "AND (loop_run_id IS NULL OR loop_run_id = %s)", (loop_id, root_id, loop_id))
    members = conn.execute(
        "UPDATE ah.session s SET loop_run_id = %s, loop_link_method = 'lineage' "
        "WHERE s.root_session_id = %s AND s.id <> %s AND s.first_event_at >= %s "
        "AND (%s::timestamptz IS NULL OR s.first_event_at <= %s) "
        "AND (s.loop_run_id IS NULL OR s.loop_run_id = %s) RETURNING s.id",
        (loop_id, root_id, root_id, start, end, end, loop_id)).fetchall()
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
        (loop_id, root_id, root_id, start, end, end)).fetchall()
    for sid, method in [(m, "lineage") for m in member_ids] + [(h[0], "heuristic") for h in heuristic]:
        _lane(conn, loop_id, sid, method)


def _lane(conn: psycopg.Connection, loop_id: int, session_id: int, method: str) -> None:
    info = conn.execute(
        "SELECT s.agent_type, s.agent_role, s.agent_path, sp.name, sp.child_task_name, sp.requested_type, s.agent "
        "FROM ah.session s LEFT JOIN ah.subagent_spawn sp ON sp.child_session_id = s.id WHERE s.id = %s LIMIT 1",
        (session_id,)).fetchone()
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
            "WHERE sp.child_session_id = %s LIMIT 1", (session_id,)).fetchone()
        if brief:
            m = LANE_LINE.search(brief[0][:2000])
            name = m.group(1) if m else None
    lane_return, return_status = None, None
    report = conn.execute(
        "SELECT text FROM ah.message WHERE session_id = %s AND message_class = 'subagent_report' "
        "ORDER BY ts DESC LIMIT 1", (session_id,)).fetchone()
    if report:
        m = LANE_RETURN.search(report[0])
        if m:
            try:
                lane_return = json.loads(m.group(1))
            except ValueError:
                lane_return = {"unparsed": True}
            if isinstance(lane_return, dict):
                status = lane_return.get("status")
                return_status = status if isinstance(status, str) else None
    conn.execute(
        "INSERT INTO ah.lane (loop_run_id, session_id, lane_name, role, link_method, return_status, lane_return) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (loop_run_id, session_id) DO UPDATE SET "
        "lane_name = COALESCE(EXCLUDED.lane_name, lane.lane_name), role = COALESCE(EXCLUDED.role, lane.role), "
        "return_status = COALESCE(EXCLUDED.return_status, lane.return_status), "
        "lane_return = COALESCE(EXCLUDED.lane_return, lane.lane_return)",
        (loop_id, session_id, name, agent_type or agent_role or requested, method, return_status,
         Jsonb(lane_return) if lane_return is not None else None))
