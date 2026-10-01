"""Read-only stdio MCP server over the agent-history catalogue (schema `ah`).

It never writes and never shells out. Every call opens a fresh psycopg connection as the reader role
(reader DSN: $AGENT_HISTORY_READER_DSN, else `reader_dsn` / `reader_dsn_file` in the config), runs
one statement in a read-only transaction with a 30 s statement_timeout, and closes it.

The server refuses to start a query unless the role is a plain reader:
- not a superuser, not the owner (or a member of the owner) of schema `ah` or any of its tables;
- no INSERT/UPDATE/DELETE/TRUNCATE on any `ah` table and no CREATE on the schema;
- no membership in `pg_execute_server_program`, `pg_write_server_files` or `pg_read_server_files`;
- `default_transaction_read_only` is `on` for the role (ALTER ROLE ... SET, see sql/roles.sql).
The sql() tool additionally sends the statement with the extended protocol, so the server rejects a
second `;`-separated statement outright.

Scoping: tools that search read only the namespaces of one configured context: the `context`
argument, else $AGENT_HISTORY_CONTEXT, else the config's default_context. Every response's first
line names the context and namespaces used.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Literal

import psycopg
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.rows import dict_row

from .config import ConfigError, load_config

DURATION = re.compile(r"^(\d+)([hdw])$")
STATEMENT_TIMEOUT = "30s"


class UnsafeRole(ToolError):
    pass


def _config():
    try:
        return load_config()
    except ConfigError as exc:
        raise ToolError(f"config: {exc}") from exc


def _ns(context: str | list[str] | None, namespaces: list[str] | None = None) -> tuple[list[str], str]:
    if namespaces or isinstance(context, list):
        return list(namespaces or context), "explicit"
    config = _config()
    name = context or os.environ.get("AGENT_HISTORY_CONTEXT") or config.default_context
    try:
        return config.namespaces(name), name
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc


def _header(ctx: str, namespaces: list[str] | None = None, note: str = "") -> str:
    parts = [f"context={ctx}"]
    if namespaces is not None:
        parts.append(f"namespaces={','.join(namespaces)}")
    if note:
        parts.append(note)
    return "[" + " ".join(parts) + "]"


def _identifier_like(word: str) -> bool:
    if re.search(r"[_./\-:]|\d", word):
        return True
    return any(c.isupper() for c in word[1:]) and any(c.islower() for c in word)


def _w_vec(query: str) -> float:
    """Vector weight for hybrid search: lower for short or identifier-like queries (paths, shas)."""
    words = query.split()
    if len(words) <= 3 or any(_identifier_like(w) for w in words):
        return 0.35
    return 0.7


def _query_vector(text: str) -> str | None:
    """A query embedding from the configured provider within 3 s, else None (BM25-only)."""
    emb = _config().embedding
    if not emb.enabled:
        from .query_embedding import query_vector

        return query_vector(text)
    try:
        body = json.dumps({"model": emb.model, "input": [text[:12000]], "dimensions": emb.dimensions}).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "agent-history-mcp/1", **emb.headers}
        token = emb.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            emb.base_url.rstrip("/") + "/embeddings", data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=3.0) as response:
            vector = json.loads(response.read())["data"][0]["embedding"]
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return "[" + ",".join(f"{v / norm:.6g}" for v in vector) + "]"
    except Exception:
        return None


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    m = DURATION.match(value)
    if m:
        unit = {"h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
        return datetime.now(timezone.utc) - timedelta(**{unit: int(m.group(1))})
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"expected <N>h, <N>d, <N>w or an ISO timestamp, got {value!r}") from None


def _normalise_host(host: str) -> str:
    h = host.strip()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h) or ":" in h:
        return h.lower()
    return h.split(".", 1)[0].lower()


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(int(n), hi))


# --- database access -------------------------------------------------------------------------------

GUARD_SQL = """
SELECT r.rolsuper,
       current_setting('default_transaction_read_only') AS read_only_default,
       COALESCE((SELECT pg_has_role(current_user, n.nspowner, 'MEMBER')
                 FROM pg_namespace n WHERE n.nspname = 'ah'), false) AS owns_schema,
       COALESCE((SELECT has_schema_privilege(current_user, 'ah', 'CREATE')), false) AS can_create,
       EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'ah' AND c.relkind IN ('r', 'p')
                 AND (pg_has_role(current_user, c.relowner, 'MEMBER')
                      OR has_table_privilege(current_user, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')))
           AS can_write,
       pg_has_role(current_user, 'pg_execute_server_program', 'MEMBER') AS can_execute_server_program,
       pg_has_role(current_user, 'pg_write_server_files', 'MEMBER') AS can_write_server_files,
       pg_has_role(current_user, 'pg_read_server_files', 'MEMBER') AS can_read_server_files
FROM pg_roles r WHERE r.rolname = current_user
"""


def check_role(conn: psycopg.Connection) -> None:
    """Raise UnsafeRole unless the connected role is a read-only, non-owner, non-superuser reader."""
    row = conn.execute(GUARD_SQL).fetchone()
    conn.rollback()
    if row is None:
        raise UnsafeRole("refusing to run: cannot read the connected role")
    values = (
        row
        if isinstance(row, dict)
        else dict(
            zip(
                (
                    "rolsuper",
                    "read_only_default",
                    "owns_schema",
                    "can_create",
                    "can_write",
                    "can_execute_server_program",
                    "can_write_server_files",
                    "can_read_server_files",
                ),
                row,
            )
        )
    )
    problems = []
    if values["rolsuper"]:
        problems.append("the role is a superuser")
    if values["owns_schema"]:
        problems.append("the role owns schema ah")
    if values["can_create"]:
        problems.append("the role can CREATE in schema ah")
    if values["can_write"]:
        problems.append("the role owns or can write ah tables")
    if values["can_execute_server_program"]:
        problems.append("the role is a member of pg_execute_server_program")
    if values["can_write_server_files"]:
        problems.append("the role is a member of pg_write_server_files")
    if values["can_read_server_files"]:
        problems.append("the role is a member of pg_read_server_files")
    if values["read_only_default"] != "on":
        problems.append("default_transaction_read_only is not on for the role")
    if problems:
        raise UnsafeRole(
            "refusing to run as a writable role: "
            + "; ".join(problems)
            + ". Connect as a SELECT-only reader (sql/roles.sql)."
        )


def _dsn() -> str:
    dsn = os.environ.get("AGENT_HISTORY_READER_DSN") or os.environ.get("AGENT_HISTORY_MCP_DSN") or _config().reader_dsn
    if not dsn:
        raise ToolError("no reader DSN: set AGENT_HISTORY_READER_DSN or reader_dsn in the config")
    return dsn


def _connect() -> psycopg.Connection:
    try:
        conn = psycopg.connect(_dsn(), application_name="agent-history-mcp", connect_timeout=5, row_factory=dict_row)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"database connection failed: {type(exc).__name__}") from exc
    try:
        check_role(conn)
    except Exception:
        conn.close()
        raise
    conn.read_only = True
    return conn


def _run(statement: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            cur.execute(statement, params)
            return cur.fetchall()
    except psycopg.Error as exc:
        raise ToolError(f"query failed: {exc}") from exc
    finally:
        conn.close()


def _cell(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…[truncated]"
    return value


def _json(data: Any, forced_cap: int = 300) -> str:
    if isinstance(data, list) and len(data) > forced_cap:
        body = json.dumps(data[:forced_cap], default=str, ensure_ascii=False)
        return f"{body}\n[truncated: showing first {forced_cap} of {len(data)} rows]"
    return json.dumps(data, default=str, ensure_ascii=False)


# --- tool implementations ---------------------------------------------------------------------------

mcp = MCPServer(
    "agent-history",
    instructions=(
        "Read-only access to the agent-history catalogue: agent transcripts, tool calls, git activity, "
        "loop runs and per-call efficiency for Claude Code, Codex and pi sessions. Use search() to find "
        "prior work by phrase, find_sessions() + session() to inspect one session, why()/touched() for "
        "provenance, efficiency() for how a root spent its model calls, and sql()/schema() for anything "
        "else. Every response's first line names the context and namespaces searched."
    ),
)


def search_impl(query: str, mode: str, since: str | None, limit: int, context: str | None) -> str:
    ns, ctx = _ns(context)
    qvec = None if mode == "bm25" else _query_vector(query)
    note = "bm25 requested" if mode == "bm25" else ("" if qvec else "no query vector, BM25 only")
    rows = _run(
        "SELECT message_id, session_id, agent, namespace, session_uid, agent_id, ts, role, message_class, "
        "round(score::numeric, 3) AS score, bm25_rank, vec_rank, snippet, cwd, title "
        "FROM ah.hybrid_search(%s, %s::halfvec, %s, %s, %s::integer, %s::real, ah.conversation_classes())",
        (query, qvec, ns, _parse_when(since), _clamp(limit, 1, 200), _w_vec(query)),
    )
    return _header(ctx, ns, note) + "\n" + _json(rows)


@mcp.tool()
def search(
    query: str,
    mode: Literal["hybrid", "bm25"] = "hybrid",
    since: str | None = None,
    limit: int = 20,
    context: str | None = None,
    namespaces: list[str] | None = None,
) -> str:
    """Search prompts, replies, sub-agent briefs/reports and compaction summaries by phrase.

    BM25 keyword search, fused with vector similarity when embeddings are configured (otherwise
    BM25 only, noted in the header). `since` takes '7d', '2w', '12h' or an ISO timestamp."""
    return search_impl(query, mode, since, limit, namespaces or context)


def find_sessions_impl(query: str, context: str | None) -> str:
    ns, ctx = _ns(context)
    rows = _run("SELECT * FROM ah.find_sessions(%s, %s, %s)", (query, ns, 10))
    return _header(ctx, ns) + "\n" + _json(rows)


@mcp.tool()
def find_sessions(query: str, context: str | None = None, namespaces: list[str] | None = None) -> str:
    """Find past sessions by phrase. When a hit is a sub-agent, its root_session_uid is the session
    to resume."""
    return find_sessions_impl(query, namespaces or context)


def _in_context(rows: list[dict[str, Any]], ns: list[str]) -> list[dict[str, Any]]:
    """Rows of the context's namespaces; rows with no namespace (ingested git history) are kept."""
    return [r for r in rows if r.get("namespace") is None or r.get("namespace") in ns]


def session_impl(session_uid: str, agent_id: str, context: str | None = None) -> str:
    ns, ctx = _ns(context)
    if context is None:
        rows = _run("SELECT * FROM ah.session_timeline(%s, %s, %s, %s)", (session_uid, agent_id, 400, 200))
        return _header(ctx) + "\n" + _json(rows)
    found = _run(
        "SELECT namespace FROM ah.session WHERE session_uid = %s AND agent_id = %s AND NOT is_stub",
        (session_uid, agent_id),
    )
    if not found or found[0]["namespace"] not in ns:
        raise ToolError(f"no session {session_uid!r} in context {ctx!r}")
    rows = _run("SELECT * FROM ah.session_timeline(%s, %s, %s, %s)", (session_uid, agent_id, 400, 200))
    return _header(ctx, ns) + "\n" + _json(rows)


@mcp.tool()
def session(session_uid: str, agent_id: str = "", context: str | None = None) -> str:
    """Structural timeline of one session: turns, bounded message excerpts, tool calls (name, outcome,
    duration), sub-agent spawns, compactions and git events. agent_id '' is the main thread."""
    return session_impl(session_uid, agent_id, context)


def why_impl(sha: str, context: str | None = None) -> str:
    ns, ctx = _ns(context)
    rows = _run("SELECT * FROM ah.why(%s)", (sha.strip().lower(),))
    if context is not None:
        rows = _in_context(rows, ns)
    return _header(ctx, ns if context is not None else None) + "\n" + _json(rows)


@mcp.tool()
def why(sha: str, context: str | None = None) -> str:
    """Why a commit was made: the session and event that made it, the prompt or brief before it, and
    ingested git_commit rows for the sha prefix (7-40 hex characters)."""
    return why_impl(sha, context)


def touched_impl(path: str, context: str | None = None) -> str:
    ns, ctx = _ns(context)
    rows = _run("SELECT * FROM ah.who_touched(%s, %s)", (path, 200 if context is not None else 50))
    if context is not None:
        rows = _in_context(rows, ns)[:50]
    return _header(ctx, ns if context is not None else None) + "\n" + _json(rows)


@mcp.tool()
def touched(path: str, context: str | None = None) -> str:
    """Sessions that touched a file. `path` is a SQL LIKE pattern, e.g. '%/load.py'."""
    return touched_impl(path, context)


def loops_impl(limit: int, context: str | None = None) -> str:
    ns, ctx = _ns(context)
    lim = _clamp(limit, 1, 100)
    rows = _run("SELECT * FROM ah.recent_loops(%s, %s)", (None, 100 if context is not None else lim))
    if context is not None:
        rows = _in_context(rows, ns)[:lim]
    return _header(ctx, ns if context is not None else None) + "\n" + _json(rows)


@mcp.tool()
def loops(limit: int = 20, context: str | None = None) -> str:
    """Most recent fan-out loop runs: status, repo, lanes, sessions, tokens, tool calls, commits."""
    return loops_impl(limit, context)


def active_sessions_impl(minutes: int, context: str | None) -> str:
    ns, ctx = _ns(context)
    rows = _run("SELECT * FROM ah.active_sessions(%s)", (_clamp(minutes, 1, 1440),))
    rows = [r for r in rows if r.get("namespace") in ns]  # a session always has a namespace
    return _header(ctx, ns) + "\n" + _json(rows)


@mcp.tool()
def active_sessions(minutes: int = 15, context: str | None = None, namespaces: list[str] | None = None) -> str:
    """Root sessions with events in the last `minutes` minutes."""
    return active_sessions_impl(minutes, namespaces or context)


def infra_actions_impl(host: str, around: str | None, window: str, context: str | None) -> str:
    ns, ctx = _ns(context)
    around_ts = _parse_when(around) or datetime.now(timezone.utc)
    rows = _run("SELECT * FROM ah.infra_actions(%s, %s, %s::interval)", (_normalise_host(host), around_ts, window))
    rows = [r for r in rows if r.get("namespace") in ns]
    return _header(ctx, ns) + "\n" + _json(rows)


@mcp.tool()
def infra_actions(
    host: str,
    around: str | None = None,
    window: str = "2 hours",
    context: str | None = None,
    namespaces: list[str] | None = None,
) -> str:
    """Agent-driven remote actions (ssh/scp/rsync) against one host within a window around a time."""
    return infra_actions_impl(host, around, window, namespaces or context)


def efficiency_impl(session_uid: str | None, agent_id: str | None, calls: bool, context: str | None) -> str:
    ns, ctx = _ns(context)
    if calls:
        if not session_uid:
            raise ToolError("calls=true needs a session_uid")
        rows = _run(
            "SELECT session_uid, agent_id, role, ts, response_id, model, trigger, input_tokens, "
            "cache_read, output FROM ah.efficiency_calls(%s, %s, %s) ORDER BY ts, byte_offset",
            (ns, session_uid, agent_id),
        )
    else:
        rows = _run(
            "SELECT * FROM ah.efficiency(%s, %s, %s) ORDER BY calls DESC LIMIT 200", (ns, session_uid, agent_id)
        )
    return _header(ctx, ns) + "\n" + _json(rows)


@mcp.tool()
def efficiency(
    session_uid: str | None = None, agent_id: str | None = None, calls: bool = False, context: str | None = None
) -> str:
    """How sessions spent their model calls: each call attributed to what triggered it (user, model,
    work, status, wait, event, noop, orchestrate, agent_msg). A root that polls shows a high share of
    wait and status calls. calls=true lists every call of one session."""
    return efficiency_impl(session_uid, agent_id, calls, context)


def sql_impl(query: str) -> str:
    statement = query.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if not statement:
        raise ToolError("sql: empty statement")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            # stream() uses the extended query protocol, so Postgres rejects a second ';'-separated
            # statement. params=None keeps a literal % (LIKE patterns) from being read as a placeholder.
            with closing(cur.stream(statement, None)) as result:
                rows = list(islice(result, 201))
    except psycopg.Error as exc:
        raise ToolError(f"sql: {exc}") from exc
    finally:
        conn.close()
    truncated = len(rows) > 200
    rows = [{k: _cell(v) for k, v in row.items()} for row in rows[:200]]
    body = json.dumps(rows, default=str, ensure_ascii=False)
    if truncated:
        body += "\n[truncated: showing first 200 rows; the query returned more]"
    return _header(_ns(None)[1]) + "\n" + body


@mcp.tool()
def sql(query: str) -> str:
    """Run exactly one read-only SQL statement against schema `ah` (30 s timeout, 200 rows, cells cut
    at 2000 characters). Writes, DDL and a second statement are rejected by the server. Namespaces
    are not filtered here: add `namespace = ANY(...)` yourself."""
    return sql_impl(query)


def schema_impl(table: str | None) -> str:
    columns = _run(
        "SELECT table_name, column_name, data_type, ordinal_position FROM information_schema.columns "
        "WHERE table_schema = 'ah' AND (%s::text IS NULL OR table_name = %s) ORDER BY table_name, ordinal_position",
        (table, table),
    )
    functions = _run(
        "SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS args, "
        "pg_get_function_result(p.oid) AS returns FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'ah' AND (%s::text IS NULL OR p.proname = %s) ORDER BY p.proname",
        (table, table),
    )
    return _header(_ns(None)[1]) + "\n" + _json({"columns": columns, "functions": functions})


@mcp.tool()
def schema(table: str | None = None) -> str:
    """Tables, views and columns in schema `ah` (optionally one name), plus function signatures."""
    return schema_impl(table)


def search_summaries_impl(
    query: str, since: str | None, limit: int, namespaces: list[str] | None = None, context: str | None = None
) -> str:
    ns, ctx = _ns(context, namespaces)
    qvec = _query_vector(query)
    rows = _run(
        "SELECT summary_id, session_id, namespace, agent, session_uid, agent_id, analysed_at, "
        "classification, project, title, objective, snippet, round(score::numeric, 3) AS score, "
        "bm25_rank, vec_rank, cwd FROM ah.hybrid_search_summaries(%s, %s::halfvec, %s, %s, %s::integer, %s::real)",
        (query, qvec, ns, _parse_when(since), _clamp(limit, 1, 200), _w_vec(query)),
    )
    return _header(ctx, ns, "" if qvec else "vector unavailable, BM25-only fallback") + "\n" + _json(rows)


@mcp.tool()
def search_summaries(
    query: str,
    since: str | None = None,
    limit: int = 20,
    namespaces: list[str] | None = None,
    context: str | None = None,
) -> str:
    """Search journal summary title, objective and narrative, with BM25 fallback."""
    return search_summaries_impl(query, since, limit, namespaces, context)


def task_impl(task_key: str) -> str:
    rows = _run(
        "SELECT row_kind, task, task_title, task_status, weight, agent, namespace, session_uid, "
        "agent_id, session_title, cwd, mentions, in_human, in_brief, first_ts, last_ts, wall_s, "
        "tokens, output, priced_cost_usd, claude_cost_usd, commits FROM ah.task_effort(%s)",
        (task_key,),
    )
    return _header(_ns(None)[1]) + "\n" + _json(rows)


@mcp.tool()
def task(task_key: str) -> str:
    """Effort, tokens, priced cost and commits attributed to one backlog task."""
    return task_impl(task_key)


def main() -> int:
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
