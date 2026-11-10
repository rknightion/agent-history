"""Read the catalogue using bounded read-only queries.

Use --format aligned, csv, json or expanded. Search defaults to hybrid when a query vector is
available and otherwise falls back to BM25. Namespaces follow the configured context.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

ENV_FILE = Path(os.environ["AGENT_HISTORY_ENV"]) if os.environ.get("AGENT_HISTORY_ENV") else None
PSQL = next(
    (
        p
        for p in (
            shutil.which("psql"),
            "/opt/homebrew/bin/psql",
            "/opt/homebrew/opt/libpq/bin/psql",
            "/usr/local/opt/libpq/bin/psql",
        )
        if p and Path(p).is_file()
    ),
    "psql",
)
DURATION = re.compile(r"^(\d+)([hdw])$")

# --- query-time embedding (search/summaries/rules --similar) --------------------------------------

IDENTIFIER_LIKE = re.compile(r"[_./]|::|--|[a-z][A-Z]|[A-Za-z][0-9]|[0-9][A-Za-z]")


def query_vector(text: str, timeout: float = 3.0) -> str | None:
    if os.environ.get("AGENT_HISTORY_EMBED_ENV"):
        from .query_embedding import query_vector as legacy_vector

        return legacy_vector(text, timeout)
    from .mcp_server import _query_vector

    return _query_vector(text)


def default_vector_weight(q: str) -> float:
    """0.35 for short/identifier-shaped (keyword-style) queries, 0.7 otherwise (bake-off result)."""
    if len(q.split()) <= 3 or IDENTIFIER_LIKE.search(q):
        return 0.35
    return 0.7


def resolve_vector(q: str, mode: str | None) -> tuple[str | None, str]:
    """(qvec, effective_mode) for a search/summaries query given --mode.

    mode='bm25' skips embedding entirely. Otherwise embeds the query; on failure (no embed.env, a
    network/HTTP error or a timeout) prints one stderr note and falls back to 'bm25'. Otherwise
    effective_mode is the requested mode, or 'hybrid' when none was given.
    """
    if mode == "bm25":
        return None, "bm25"
    qvec = query_vector(q)
    if qvec is None:
        print(
            "agent-history: vector search unavailable (no embed.env, or the embedding call failed "
            "or timed out); falling back to BM25",
            file=sys.stderr,
        )
        return None, "bm25"
    return qvec, mode or "hybrid"


def _service_connection_settings(dsn: str) -> dict[str, str]:
    """Resolve libpq policy in a child with an isolated environment fallback."""
    env = dict(os.environ)
    # libpq itself preserves DSN > service file > environment precedence, including
    # an explicit zero (unlimited). Do not mutate this process's environment or pass
    # a connect_timeout keyword, which would override service-file policy.
    if not env.get("PGCONNECT_TIMEOUT"):
        env["PGCONNECT_TIMEOUT"] = "5"
    # Drain concurrently: effective settings can exceed a pipe's buffer. The
    # connection deadline remains libpq's, including an explicit unlimited zero.
    # Once the child exits, even an inherited open descriptor cannot stall us.
    payload = bytearray()
    finished = threading.Event()
    incoming, outgoing = socket.socketpair()
    with incoming, outgoing:
        incoming.settimeout(0.2)

        def receive() -> None:
            try:
                while True:
                    try:
                        chunk = incoming.recv(8192)
                    except TimeoutError:
                        if finished.is_set():
                            return
                        continue
                    if not chunk:
                        return
                    payload.extend(chunk)
                    if len(payload) > 1024 * 1024:
                        incoming.shutdown(socket.SHUT_RD)
                        return
            except OSError:
                return

        receiver = threading.Thread(target=receive)
        receiver.start()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from agent_history.reader import _service_connection_child; "
                    "_service_connection_child(int(sys.argv[1]))",
                    str(outgoing.fileno()),
                ],
                input=dsn,
                text=True,
                capture_output=True,
                env=env,
                pass_fds=(outgoing.fileno(),),
            )
        except (OSError, ValueError):
            sys.exit("agent-history: cannot resolve reader service connection")
        finally:
            outgoing.close()
            finished.set()
            receiver.join()
    try:
        if result.returncode or len(payload) > 1024 * 1024:
            raise ValueError
        effective = json.loads(payload)
        if (
            not isinstance(effective, dict)
            or "password" not in effective
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in effective.items())
        ):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        sys.exit("agent-history: cannot resolve reader service connection")
    return effective


def _service_connection_child(settings_fd: int) -> None:
    """Keep the DSN and effective credentials off argv and error output."""
    from psycopg import pq

    # psycopg's polling connect computes its deadline before service-file values
    # are resolved. Blocking libpq connect honours the effective service policy.
    connection = pq.PGconn.connect(sys.stdin.read().encode())
    try:
        if connection.status != pq.ConnStatus.OK:
            sys.exit(1)
        # Keep compiled defaults too: omitting port 5432, for example, would
        # revive an inherited wrong PGPORT when settings are passed to psql.
        effective = {
            option.keyword.decode(): option.val.decode() for option in connection.info if option.val is not None
        }
        effective["password"] = connection.password.decode()
    finally:
        connection.finish()
    # This descriptor is private IPC, never stdout/stderr or a logging sink.
    payload = memoryview(json.dumps(effective).encode())
    try:
        while payload:
            written = os.write(settings_fd, payload)
            if written == 0:
                sys.exit(1)
            payload = payload[written:]
    except OSError:
        sys.exit(1)
    finally:
        os.close(settings_fd)


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    from .config import load_config

    config = load_config()
    dsn = os.environ.get("AGENT_HISTORY_READER_DSN") or config.reader_dsn
    if dsn:
        from psycopg import pq

        # libpq does not expand conninfo in PGDATABASE. Keep credentials out of
        # argv by translating the configured connection options into PG variables.
        try:
            options = pq.Conninfo.parse(dsn.encode())
        except Exception:
            sys.exit("agent-history: invalid reader DSN")
        service = os.environ.get("PGSERVICE") or any(option.keyword == b"service" and option.val for option in options)
        if service:
            # Resolve effective settings through libpq without overriding service
            # policy. The child runs no SQL; credentials travel only through pipes.
            effective = _service_connection_settings(dsn)
            environment_names = {option.keyword.decode(): option.envvar for option in pq.Conninfo.get_defaults()}
            for key, value in effective.items():
                name = environment_names.get(key)
                if name is not None:
                    env[name.decode()] = value
            env.pop("PGSERVICE", None)
        else:
            for option in options:
                if option.val is not None:
                    if option.envvar is None:
                        sys.exit("agent-history: reader DSN contains an option without a PG environment variable")
                    env[option.envvar.decode()] = option.val.decode()
        env["PGOPTIONS"] = (
            env.get("PGOPTIONS", "") + " -c default_transaction_read_only=on -c statement_timeout=60000"
        ).strip()
        env["PAGER"] = ""
        return env
    if ENV_FILE is None or not ENV_FILE.is_file():
        sys.exit("agent-history: configure AGENT_HISTORY_ENV or a reader DSN")
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()  # the reader file wins over any PG* already in the shell
    env["PGOPTIONS"] = (
        env.get("PGOPTIONS", "") + " -c default_transaction_read_only=on -c statement_timeout=60000"
    ).strip()
    env["PAGER"] = ""
    return env


def context() -> str:
    explicit = os.environ.get("AGENT_HISTORY_CONTEXT")
    if explicit:
        return explicit
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        value = os.environ.get(var, "")
        if value.endswith("-work"):
            return "work"
        if value.endswith("-personal"):
            return "personal"
    from .config import load_config

    return load_config().default_context


def namespaces(args: argparse.Namespace) -> list[str] | None:
    if getattr(args, "all", False):
        return None
    if getattr(args, "ns", None):
        return args.ns
    ctx = context()
    from .config import load_config

    config = load_config()
    return config.namespaces(ctx) if ctx in config.contexts else [f"claude-{ctx}", f"codex-{ctx}"]


def since_sql(value: str | None) -> str | None:
    if not value:
        return None
    m = DURATION.match(value)
    if not m:
        sys.exit(f"agent-history: --since takes <N>h, <N>d or <N>w, not {value!r}")
    unit = {"h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
    return f"now() - interval '{int(m.group(1))} {unit}'"


def run(sql: str, variables: dict[str, str], fmt: str = "aligned") -> int:
    cmd = [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-P", "pager=off"]
    if fmt == "csv":
        cmd.append("--csv")
    elif fmt == "json":
        sql = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql.rstrip().rstrip(';')}) t;"
        cmd += ["-A", "-t"]
    elif fmt == "expanded":
        cmd += ["-x"]
    for key, value in variables.items():
        cmd += ["-v", f"{key}={value}"]
    return subprocess.run(cmd, input=sql, text=True, env=load_env()).returncode


def query(sql: str, variables: dict[str, str]) -> list[dict[str, str]]:
    """Run SQL and return rows as dicts (CSV round trip; values are strings, NULL is '')."""
    cmd = [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv"]
    for key, value in variables.items():
        cmd += ["-v", f"{key}={value}"]
    proc = subprocess.run(cmd, input=sql, text=True, env=load_env(), capture_output=True)
    if proc.returncode:
        sys.exit(f"agent-history: {proc.stderr.strip()}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def host_short() -> str:
    return socket.gethostname().split(".")[0].lower()


def resume_command(row: dict[str, str]) -> tuple[str | None, list[str]]:
    """Runnable resume command for a session row, plus warnings.

    The native context is configurable; other contexts use their named home. The native home is started with the
    variable unset rather than pointed at ~/.claude, so the command also works from inside a named
    session's shell and uses the same credentials the native launcher does.
    """
    warnings: list[str] = []
    agent, _, ctx = row["namespace"].partition("-")
    if (agent not in ("claude", "codex") or ctx not in ("personal", "work")) and row["namespace"] != "codex-flash":
        return None, [f"namespace {row['namespace']} has no live home (retired or standalone); cannot resume"]
    var = "CLAUDE_CONFIG_DIR" if agent == "claude" else "CODEX_HOME"
    native_ctx = os.environ.get("AGENT_HISTORY_NATIVE_CONTEXT", context())
    native, named = Path.home() / f".{agent}", Path.home() / f".{agent}-{ctx}"
    # Flash owns an independent home, even if configured as the native search context.
    if ctx != "flash" and ctx == native_ctx and native.is_dir():
        env = f"env -u {var}"
    elif named.is_dir():
        env = f"{var}={shlex.quote(str(named))}"
    else:
        return None, [f"no local home for {row['namespace']} on {host_short()} (looked for {named})"]
    tool = (
        f"claude --resume {shlex.quote(row['session_uid'])}"
        if agent == "claude"
        else f"codex resume {shlex.quote(row['session_uid'])}"
    )
    launcher = f"{agent}-{ctx}"
    if not env.startswith("env -u") and shutil.which(launcher):
        # The named-home launcher also restores that context's auth; prefer it to a bare env var.
        env, tool = "", tool.replace(agent, launcher, 1)
    cwd = row.get("cwd") or ""
    if row.get("machine") and row["machine"].lower() != host_short():
        warnings.append(f"session ran on {row['machine']}, this is {host_short()}: its transcript may not be here")
    if cwd and not Path(cwd).is_dir():
        warnings.append(f"cwd {cwd} does not exist on this machine")
    prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    return f"{prefix}{env + ' ' if env else ''}{tool}", warnings


def ns_literal(ns: list[str] | None) -> str:
    if ns is None:
        return "NULL::text[]"
    return "ARRAY[" + ",".join("'" + n.replace("'", "") + "'" for n in ns) + "]::text[]"


# --- rules --similar: resolving repo_slug to a local checkout --------------------------------------


def _normalise_host(value: str) -> str:
    """Mirrors ah.v_infra_action's `host` column: an IPv4/IPv6-shaped value is kept as-is, a
    hostname's trailing domain suffix is stripped ('host.example' -> 'host')."""
    value = value.strip()
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value) or ":" in value:
        return value.lower()
    return value.split(".")[0].lower()


def _repo_slug_from_remote(remote: str) -> str | None:
    """host/owner/name from a git remote URL, decided by the collector's own grammar.

    The reader and the collector must agree on which remotes are a repository, so this calls
    `collect_git.repo_slug` rather than carrying a second parser. The import is deferred: it needs
    only the standard library and `.common`, and the other reader commands never pay for it.
    """
    from .collect_git import repo_slug

    return repo_slug(remote)


def find_local_checkout(repo_slug: str) -> Path | None:
    """A local git checkout whose origin remote resolves to `repo_slug`, or None.

    Scans configured git repositories using the collector slug grammar.
    """
    from .config import load_config

    candidates = load_config().git_repos
    for path in candidates:
        if not (path / ".git").exists():
            continue
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode != 0:
            continue
        if _repo_slug_from_remote(proc.stdout.strip()) == repo_slug:
            return path
    return None


def narrow_rule_similarity(
    repo_slug: str, path: str, sha: str, committed_at: str, days: int, ns: list[str] | None
) -> None:
    """Prints similar-human-prompt counts before/after a rule change (`rules --similar`)."""
    checkout = find_local_checkout(repo_slug)
    if checkout is None:
        print(
            f"agent-history: no local checkout found for {repo_slug} (scanned configured git repositories); skipping --similar for {path}@{sha[:12]}",
            file=sys.stderr,
        )
        return
    proc = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{sha}:{path}"], capture_output=True, text=True, timeout=15
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"agent-history: git show {sha[:12]}:{path} failed in {checkout}; skipping --similar", file=sys.stderr)
        return
    qvec = query_vector(proc.stdout)
    if qvec is None:
        print("agent-history: vector search unavailable; skipping --similar narrowing", file=sys.stderr)
        return
    variables = {"qvec": qvec, "committed_at": committed_at}
    before = query(
        f"SELECT count(*) AS n FROM ah.similar_messages(:'qvec'::halfvec, {ns_literal(ns)}, "
        f":'committed_at'::timestamptz - interval '{int(days)} days', "
        f":'committed_at'::timestamptz, 200, 0.6) AS x;",
        variables,
    )
    after = query(
        f"SELECT count(*) AS n FROM ah.similar_messages(:'qvec'::halfvec, {ns_literal(ns)}, "
        f":'committed_at'::timestamptz, "
        f":'committed_at'::timestamptz + interval '{int(days)} days', 200, 0.6) AS x;",
        variables,
    )
    b = before[0]["n"] if before else "0"
    a = after[0]["n"] if after else "0"
    print(f"  similar human prompts near {path}@{sha[:12]}: before={b}  after={a}")


# --- wrapped: pretty-print the jsonb report ---------------------------------------------------------


def print_wrapped(w: dict) -> None:
    since_d, until_d = str(w.get("since") or "?")[:10], str(w.get("until") or "?")[:10]
    print(f"=== Agent History Wrapped: {since_d} to {until_d} ===")
    print(f"{w.get('sessions', 0)} sessions, {w.get('human_prompts', 0)} human prompts")
    hour = w.get("busiest_hour_of_day")
    if hour is not None:
        print(f"Busiest hour: {int(hour):02d}:00   Busiest day: {w.get('busiest_weekday') or '?'}")
    night = w.get("latest_night_session") or {}
    if night:
        print(
            f"Latest into the night: {night.get('local_time')} on "
            f"{night.get('title') or night.get('session_uid')} ({night.get('namespace')})"
        )
    exp = w.get("most_expensive_session") or {}
    if exp:
        print(
            f"Most expensive session: {exp.get('title') or exp.get('session_uid')} "
            f"(${exp.get('priced_cost_usd')}, {exp.get('namespace')})"
        )
    longest = w.get("longest_running_session") or {}
    if longest:
        print(
            f"Longest running session: {longest.get('title') or longest.get('session_uid')} "
            f"({longest.get('wall_s')}s, {longest.get('namespace')})"
        )
    loop = w.get("longest_loop") or {}
    if loop:
        print(f"Longest loop: run {loop.get('loop_run_id')} in {loop.get('repo_slug')} ({loop.get('wall_s')}s)")
    denied = w.get("most_denied_command_verb") or {}
    if denied:
        print(f"Most denied command: {denied.get('cmd_verb')} ({denied.get('denials')} denials)")
    for label, key, cols in (
        ("Top skills/commands", "top_skills", ("name", "uses")),
        ("Top tools", "top_tools", ("tool_name", "calls")),
        ("Top MCP servers", "top_mcp_servers", ("mcp_server", "calls")),
    ):
        items = w.get(key) or []
        if items:
            print(f"{label}:")
            for item in items[:10]:
                print(f"  {item.get(cols[0])}: {item.get(cols[1])}")
    tm = w.get("tokens_and_cost_by_model") or []
    if tm:
        print("Tokens & cost by model:")
        for row in tm:
            print(f"  {row.get('model')}: output={row.get('output')} cost=${row.get('priced_cost_usd')}")


def loop_id_array(ids: list[int]) -> str:
    return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]::bigint[]"


def resolve_loop(value: str) -> list[int]:
    """Loop run ids for a numeric id or an exact closeout path; exit with one line if unknown.

    A v3 run relaunched under the same closeout has one loop_run per launch, so a path can
    name several. They are returned together, oldest first, to be treated as one run.
    """
    if value.isdigit():
        rows = query(f"SELECT id FROM ah.loop_run WHERE id = {int(value)};", {})
        if not rows:
            sys.exit(f"agent-history: no loop run with id {int(value)}")
        return [int(rows[0]["id"])]
    if not os.path.isabs(value):
        sys.exit("agent-history: loop takes a numeric id or an absolute closeout path")
    path = os.path.normpath(value)
    rows = query("SELECT id FROM ah.loop_run WHERE report_path = :'path' ORDER BY launch_ts, id;", {"path": path})
    if not rows:
        sys.exit(f"agent-history: no loop run has closeout path {path}")
    return [int(row["id"]) for row in rows]


USAGE_SUMMARY_SQL = """
SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS as_of,
       EXTRACT(EPOCH FROM (max(CASE WHEN live.status = 'finished' AND l.end_ts IS NOT NULL
                                    THEN l.end_ts ELSE now() END) - min(l.launch_ts)))::bigint AS wall_s,
       (SELECT count(*) FROM ah.session s WHERE s.loop_run_id = ANY({ids})
          AND s.loop_link_method = 'heuristic') AS heuristic_sessions,
       max(live.tasks_landed) AS landed,
       max(live.parks_total) AS parked,
       max(l.report_path) AS report_path,
       (SELECT string_agg(DISTINCT x.agent, ',' ORDER BY x.agent) FROM ah.session x
        WHERE x.id IN (SELECT root_session_id FROM ah.loop_run WHERE id = ANY({ids}))
           OR (x.loop_run_id = ANY({ids}) AND x.loop_link_method = 'relaunch')) AS root_agents,
       (SELECT string_agg(DISTINCT x.agent || COALESCE(' ' || x.cli_version_last, ''), ', '
                          ORDER BY x.agent || COALESCE(' ' || x.cli_version_last, '')) FROM ah.session x
        WHERE x.id IN (SELECT root_session_id FROM ah.loop_run WHERE id = ANY({ids}))
           OR (x.loop_run_id = ANY({ids}) AND x.loop_link_method = 'relaunch')) AS harness
FROM ah.loop_run l LEFT JOIN ah.loops live USING (launch_uid)
WHERE l.id = ANY({ids});
"""

# The runs of one closeout and every session that belongs to them, scoped as ah.v_loop_summary:
# a run's own root session is read inside its launch window, a relaunch session counts as root and
# every other linked session is read whole under its lane role. Shared by every usage query below.
USAGE_SCOPE = """
runs AS (
    -- An unfinished run's end_ts is only its last observed root event: leave the window open.
    SELECT l.id, l.root_session_id, l.launch_ts, CASE WHEN live.status = 'finished' THEN l.end_ts END AS end_ts
    FROM ah.loop_run l LEFT JOIN ah.loops live USING (launch_uid) WHERE l.id = ANY({ids})
), members AS (
    SELECT r.root_session_id AS session_id, 'root' AS role, true AS windowed, r.launch_ts, r.end_ts
    FROM runs r
    UNION ALL
    SELECT s.id, CASE WHEN s.loop_link_method = 'relaunch' THEN 'root'
                      ELSE COALESCE(NULLIF(ln.role, ''), 'unattributed') END, false, r.launch_ts, r.end_ts
    FROM runs r JOIN ah.session s ON s.loop_run_id = r.id AND s.id <> r.root_session_id
    LEFT JOIN ah.lane ln ON ln.loop_run_id = r.id AND ln.session_id = s.id
)
"""

# Prices as ah.v_session_cost: one ah.priced_usd per model and day. Relaunch sessions are root.
USAGE_ROWS_SQL = (
    "WITH "
    + USAGE_SCOPE
    + """, calls AS (
    SELECT DISTINCT ON (c.id) c.id, m.role, c.model, c.ts, c.input_uncached, c.cache_read,
           c.cache_write_5m, c.cache_write_1h, c.output
    FROM members m JOIN ah.llm_call c ON c.session_id = m.session_id
    WHERE NOT m.windowed OR (c.ts >= m.launch_ts AND c.ts < COALESCE(m.end_ts, 'infinity'))
    ORDER BY c.id, m.role
), priced AS (
    SELECT role, model, count(*) AS calls,
           ah.priced_usd(model, ts::date, sum(input_uncached), sum(cache_read), sum(cache_write_5m),
                         sum(cache_write_1h), sum(output)) AS cost
    FROM calls GROUP BY role, model, ts::date
)
SELECT role, COALESCE(model, '(unknown)') AS model, sum(calls) AS calls, sum(cost) AS cost,
       COALESCE(sum(calls) FILTER (WHERE cost IS NULL), 0) AS unpriced
FROM priced GROUP BY 1, 2
ORDER BY role <> 'root', role, sum(cost) DESC NULLS LAST, model;
"""
)

# The root's own numbers: the largest context any root call carried, its compactions (a Claude
# compaction records whether the owner typed it; Codex and pi record no trigger) and the owner
# question tools (Claude's, and Codex's two) with the time the root spent waiting on them. A
# question with no result yet is open: it has no wait to add.
USAGE_ROOT_SQL = (
    "WITH "
    + USAGE_SCOPE
    + """, roots AS (
    SELECT * FROM members WHERE role = 'root'
), peak AS (
    SELECT max(c.context_tokens) AS peak_context
    FROM roots m JOIN ah.llm_call c ON c.session_id = m.session_id
    WHERE NOT m.windowed OR (c.ts >= m.launch_ts AND c.ts < COALESCE(m.end_ts, 'infinity'))
), compacted AS (
    SELECT k.id, k.trigger
    FROM roots m JOIN ah.compaction k ON k.session_id = m.session_id
    WHERE NOT m.windowed OR (k.ts >= m.launch_ts AND k.ts < COALESCE(m.end_ts, 'infinity'))
    GROUP BY k.id, k.trigger
), asked AS (
    SELECT t.id, t.duration_ms, t.ended_at
    FROM roots m JOIN ah.tool_call t ON t.session_id = m.session_id
    WHERE t.tool_name IN ('AskUserQuestion', 'request_user_input', 'request_user_input_async')
      AND (NOT m.windowed OR (COALESCE(t.started_at, t.ended_at) >= m.launch_ts
                              AND COALESCE(t.started_at, t.ended_at) < COALESCE(m.end_ts, 'infinity')))
    GROUP BY t.id, t.duration_ms, t.ended_at
)
SELECT (SELECT peak_context FROM peak) AS peak_context,
       (SELECT count(*) FROM compacted) AS compactions,
       (SELECT count(*) FROM compacted WHERE trigger = 'manual') AS compactions_typed,
       (SELECT count(*) FROM compacted WHERE trigger IS NULL) AS compactions_untyped,
       (SELECT count(*) FROM asked) AS ask_calls,
       (SELECT COALESCE(sum(duration_ms), 0) / 1000 FROM asked)::bigint AS ask_blocked_s,
       (SELECT count(*) FROM asked WHERE ended_at IS NULL) AS ask_open,
       (SELECT count(*) FROM asked WHERE ended_at IS NOT NULL AND duration_ms IS NULL) AS ask_untimed;
"""
)

# Lanes by role, and how many of them handed back a complete return.
USAGE_LANES_SQL = """
SELECT COALESCE(NULLIF(ln.role, ''), 'unattributed') AS role, count(*) AS lanes,
       count(*) FILTER (WHERE ln.return_status = 'complete') AS complete
FROM ah.lane ln WHERE ln.loop_run_id = ANY({ids})
GROUP BY 1 ORDER BY 1;
"""

# Tool failures that matter to the run: shell calls that timed out in a lane, and every denial by
# its class. A shell tool is Claude's Bash, pi's bash or Codex's command tools. Only the Claude
# parser records a timeout or a denial, so the sessions' harnesses come back too, as 'agent' rows
# labelled root:<agent> or lane:<agent>, for the block to say where a count is not recorded.
USAGE_TOOLS_SQL = (
    "WITH "
    + USAGE_SCOPE
    + """, tools AS (
    SELECT DISTINCT ON (t.id) t.id, m.role, t.tool_name, t.timed_out, t.outcome, t.denial_kind
    FROM members m JOIN ah.tool_call t ON t.session_id = m.session_id
    WHERE NOT m.windowed OR (COALESCE(t.started_at, t.ended_at) >= m.launch_ts
                             AND COALESCE(t.started_at, t.ended_at) < COALESCE(m.end_ts, 'infinity'))
    ORDER BY t.id, m.role
)
SELECT 'timeout' AS kind, '' AS label, count(*) AS n FROM tools
WHERE role <> 'root' AND tool_name IN ('Bash', 'bash', 'exec_command', 'shell', 'local_shell')
  AND (COALESCE(timed_out, false) OR outcome = 'timeout')
UNION ALL
SELECT 'denial', COALESCE(denial_kind, 'denied'), count(*) FROM tools
WHERE denial_kind IS NOT NULL OR outcome = 'denied'
GROUP BY 2
UNION ALL
SELECT 'agent', m.role_kind || ':' || s.agent, count(DISTINCT m.session_id)
FROM (SELECT session_id, CASE WHEN role = 'root' THEN 'root' ELSE 'lane' END AS role_kind FROM members) m
JOIN ah.session s ON s.id = m.session_id
GROUP BY 2
ORDER BY 1, 3 DESC, 2;
"""
)


def _duration(seconds: int) -> str:
    hours, rest = divmod(max(0, seconds), 3600)
    days, hours = divmod(hours, 24)
    text = f"{hours}h {rest // 60}m"
    return f"{days}d {text}" if days else text


CLOSEOUT_NAME = re.compile(r"closeout-[\w.-]*-loop([0-9]+)\.md")
MAX_LEDGER_BYTES = 4 * 1024 * 1024
LEDGER_KEPT = ("event", "outcome", "pr", "head", "issues", "fix_forward", "slot_seconds")


def read_ledger(report_path: str) -> dict | None:
    """The landing ledger beside a closeout on this machine, or None when there is none.

    A closeout `<dir>/closeout-<date>-loop<N>.md` keeps its run files in `<dir>/loop<N>/`, and the
    ledger is `landings.jsonl` there. Only a regular file is read, at most MAX_LEDGER_BYTES of it.
    Returns the landing events (the keys the block uses) and the count of lines that were not
    events; `too_large` replaces both when the file is over the bound.
    """
    name = CLOSEOUT_NAME.fullmatch(os.path.basename(report_path or ""))
    if name is None:
        return None
    path = Path(report_path).parent / f"loop{name[1]}" / "landings.jsonl"
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > MAX_LEDGER_BYTES:
        return {"too_large": True, "events": [], "unreadable": 0}
    events, unreadable = [], 0
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            unreadable += 1
        elif event["event"] != "closeout-check":
            events.append({key: event.get(key) for key in LEDGER_KEPT})
    return {"too_large": False, "events": events, "unreadable": unreadable}


def _issue(value: object) -> str | None:
    """An issue as the block counts it: `5` and `#5` are one."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = str(value).strip().removeprefix("#").strip()
    return text or None


def _seconds(value: object) -> int | None:
    """A slot time, or None for anything that is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value)


def ledger_landings(events: list[dict]) -> list[dict]:
    """One row per landing, in first-seen order. A landing is its pull request within the run, as
    the landing tool keys it, or its head in direct mode where there is no pull request. Its state
    is its last event and its head the latest one any event carried (a refusal can carry an old
    head and an error none). It landed when a `merged` or `verified` event exists, and its issues
    are the ones those events name. The ledger has no repository key, so pull requests of two
    repositories with one number are not told apart."""
    landings: dict[tuple[str, str], dict] = {}
    for event in events:
        head = str(event["head"]) if event["head"] else ""
        if event["pr"] is not None:
            key = ("pr", str(event["pr"]))
        elif head:
            key = ("head", head)
        else:
            continue
        row = landings.setdefault(
            key, {"key": key, "head": "", "landed": False, "issues": [], "fix_forward": False, "slot_seconds": None}
        )
        row["outcome"] = event["outcome"] if event["outcome"] is not None else event["event"]
        row["head"] = head or row["head"]
        slot = _seconds(event["slot_seconds"])
        if slot is not None:
            row["slot_seconds"] = slot
        if event["event"] in ("merged", "verified"):
            row["landed"] = True
            row["fix_forward"] = row["fix_forward"] or bool(event["fix_forward"])
            for issue in event["issues"] if isinstance(event["issues"], list) else []:
                if (name := _issue(issue)) is not None and name not in row["issues"]:
                    row["issues"].append(name)
    return list(landings.values())


def _cell(value: object, width: int = 40) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()[:width].replace("|", "\\|") or "-"


def _slot(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    minutes, rest = divmod(max(0, seconds), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def _tasks(n: int) -> str:
    return f"{n:,} task" + ("" if n == 1 else "s")


def _count(row: dict[str, str], key: str) -> int | None:
    value = row.get(key)
    return int(value) if value not in (None, "") else None


# The parsers that record a tool timeout or a denial, and those that record an owner question tool.
RECORDS_TIMEOUTS = {"claude"}
RECORDS_QUESTIONS = {"claude", "codex"}


def _agents(labels: list[str]) -> set[str]:
    return {label.partition(":")[2] if ":" in label else label for label in labels if label}


def usage_detail_lines(
    summary: dict[str, str], rows: list[dict[str, str]], runs: int, detail: dict
) -> tuple[list[str], list[str]]:
    """(bullets, tables) for the v4 numbers. A number the catalogue could not supply prints as
    unknown, never as zero."""
    from decimal import Decimal

    root, ledger = detail["root"], detail.get("ledger")
    landings = ledger_landings(ledger["events"]) if ledger else []
    landed_rows = [x for x in landings if x["landed"]]
    closeout_landed, parked = _count(summary, "landed"), _count(summary, "parked")
    # The ledger's issues stand in for the closeout's count only when every landing names some.
    if landed_rows and all(x["issues"] for x in landed_rows):
        landed, landed_from = len({i for x in landed_rows for i in x["issues"]}), "the ledger"
    else:
        landed, landed_from = closeout_landed, "the closeout"
    harness = summary.get("harness") or "unknown"
    if not landings:
        mode = "unknown"
    else:
        mode = "pull request" if any(x["key"][0] == "pr" for x in landings) else "direct"
    root_calls = sum(int(r["calls"]) for r in rows if r["role"] == "root")
    peak = _count(root, "peak_context")
    untyped = _count(root, "compactions_untyped") or 0
    typed = "unknown" if untyped else f"{_count(root, 'compactions_typed') or 0:,}"
    marks = [r["label"] for r in detail["tools"] if r["kind"] == "agent"]
    lane_agents = _agents([m for m in marks if m.startswith("lane:")])
    session_agents = _agents(marks)
    timeouts = sum(int(r["n"]) for r in detail["tools"] if r["kind"] == "timeout")
    blind = sorted(lane_agents - RECORDS_TIMEOUTS)
    if blind and not lane_agents & RECORDS_TIMEOUTS:
        timeout_text = f"unknown (not recorded for {', '.join(blind)} lanes)"
    elif blind:
        timeout_text = f"{timeouts:,} (not recorded for {', '.join(blind)} lanes)"
    else:
        timeout_text = f"{timeouts:,}"
    root_agents = _agents(summary.get("root_agents", "").split(","))
    no_question = sorted(root_agents - RECORDS_QUESTIONS)
    if not root_agents or not root_agents & RECORDS_QUESTIONS:
        ask_text = f"unknown (no question tool recorded{' for ' + ', '.join(no_question) if no_question else ''})"
    else:
        notes = [f"blocked {(_count(root, 'ask_blocked_s') or 0) / 3600:.2f} hours"]
        if _count(root, "ask_open"):
            notes.append(f"{_count(root, 'ask_open'):,} still open at closeout")
        if _count(root, "ask_untimed"):
            notes.append(f"{_count(root, 'ask_untimed'):,} with no duration recorded")
        if no_question:
            notes.append(f"not recorded for {', '.join(no_question)}")
        ask_text = f"{_count(root, 'ask_calls') or 0:,} ({', '.join(notes)})"
    bullets = [
        f"- Harness: {harness}",
        f"- Landing mode: {mode}" + (" (from the ledger)" if landings else ""),
        "- Plugin and agent-docs versions: not recorded in the catalogue",
        f"- Landed: {'unknown' if landed is None else f'{_tasks(landed)} (from {landed_from})'}",
        f"- Parked: {'unknown' if parked is None else f'{_tasks(parked)} (from the closeout)'}",
        f"- Root calls: {root_calls:,}",
        "- Root calls per landed task: "
        + (f"{root_calls / landed:,.1f}" if landed else "unknown (no landed tasks counted)"),
        f"- Peak root context: {'unknown' if peak is None else f'{peak:,} tokens'}",
        f"- Compactions: {_count(root, 'compactions') or 0:,} (typed by the owner: {typed}"
        + (f", {untyped:,} with no trigger recorded" if untyped else "")
        + ")",
        f"- Root question-tool calls: {ask_text}",
        f"- Lane Bash timeouts: {timeout_text}",
    ]
    missing_denials = sorted(session_agents - RECORDS_TIMEOUTS)
    if missing_denials and not session_agents & RECORDS_TIMEOUTS:
        bullets.append(f"- Denials by class: not recorded for {', '.join(missing_denials)}")
    elif missing_denials:
        bullets.append(f"- Denials by class: recorded for claude sessions only, not for {', '.join(missing_denials)}")
    if ledger and ledger["too_large"]:
        bullets.append("- Landing ledger: too large to read")
    elif ledger:
        span = int(summary.get("wall_s") or 0)
        per_hour = f"{len(landed_rows) * 3600 / span:,.2f}" if span else "unknown"
        count = len(landed_rows)
        bullets.append(
            f"- Landings per hour: {per_hour} ({count:,} landing{'' if count == 1 else 's'} in {_duration(span)})"
        )
        fixes = sum(1 for x in landed_rows if x["fix_forward"])
        if fixes:
            bullets.append(f"- Fix-forward landings: {fixes:,}")
        if ledger["unreadable"]:
            bullets.append(f"- Landing ledger lines that were not events: {ledger['unreadable']:,}")
    cost: dict[str, Decimal] = {}
    calls: dict[str, int] = {}
    unpriced: dict[str, int] = {}
    for r in rows:
        cost[r["role"]] = cost.get(r["role"], Decimal(0)) + (Decimal(r["cost"]) if r["cost"] else Decimal(0))
        calls[r["role"]] = calls.get(r["role"], 0) + int(r["calls"])
        unpriced[r["role"]] = unpriced.get(r["role"], 0) + int(r["unpriced"])
    total = sum(cost.values(), Decimal(0))
    tables: list[list[str]] = []
    if rows:
        # A role with an unpriced call is flagged: its figure is a floor, and every share is of
        # the priced cost only.
        flagged = any(unpriced.values())
        spend = ["| Role | Cost | Share of priced cost |" if flagged else "| Role | Cost | Share of cost |"]
        spend.append("| --- | ---: | ---: |")
        for role, value in sorted(cost.items(), key=lambda item: (item[0] != "root", -item[1], item[0])):
            share = f"{value / total:.1%}" if total else "n/a"
            if unpriced[role] == calls[role]:
                figures = ["unpriced", "unpriced"]
            elif unpriced[role]:
                figures = [f"${value:,.2f} (partial)", f"{share} (partial)"]
            else:
                figures = [f"${value:,.2f}", share]
            spend.append(f"| {_cell(role)} | {figures[0]} | {figures[1]} |")
        tables.append(spend)
    if detail["lanes"]:
        lanes = ["| Lane role | Lanes | Complete | Complete rate |", "| --- | ---: | ---: | ---: |"]
        for r in detail["lanes"]:
            n, done = int(r["lanes"]), int(r["complete"])
            lanes.append(f"| {_cell(r['role'])} | {n:,} | {done:,} | {done / n:.0%} |")
        tables.append(lanes)
    denials = [r for r in detail["tools"] if r["kind"] == "denial"]
    if denials:
        table = ["| Denial class | Denials |", "| --- | ---: |"]
        table += [f"| {_cell(r['label'])} | {int(r['n']):,} |" for r in denials]
        tables.append(table)
    if landings:
        table = ["| Landing | Outcome | Slot time |", "| --- | --- | ---: |"]
        for x in landings:
            if x["key"][0] == "pr":
                label = f"PR {x['key'][1]}" + (f" ({x['head'][:7]})" if x["head"] else "")
            else:
                label = f"direct {x['key'][1][:12]}"
            table.append(f"| {_cell(label)} | {_cell(x.get('outcome'))} | {_slot(x['slot_seconds'])} |")
        tables.append(table)
    return bullets, [line for table in tables for line in ["", *table]]


def usage_markdown(summary: dict[str, str], rows: list[dict[str, str]], runs: int, detail: dict | None = None) -> str:
    """The closeout Notes block: totals, the v4 numbers when `detail` is given, and a role/model
    table, with no prompt text or lane names."""
    from decimal import Decimal

    calls = sum(int(r["calls"]) for r in rows)
    unpriced = sum(int(r["unpriced"]) for r in rows)
    cost = sum((Decimal(r["cost"]) for r in rows if r["cost"]), Decimal(0))
    lines = [
        f"Usage as of {summary['as_of']} (agent-history catalogue, list prices)",
        "",
        f"- Wall time: {_duration(int(summary['wall_s'] or 0))}",
        f"- LLM calls: {calls:,}",
        f"- List-price cost: ${cost:,.2f}",
    ]
    if runs > 1:
        lines.append(f"- Launches: {runs} share this closeout and are counted as one run")
    if unpriced:
        lines.append(f"- Unpriced calls: {unpriced:,} (not in the cost total)")
    heuristic = int(summary.get("heuristic_sessions") or 0)
    if heuristic:
        lines.append(f"- Heuristic-linked sessions: {heuristic:,} (attributed by timing, not by an explicit link)")
    more: list[str] = []
    if detail is not None:
        bullets, more = usage_detail_lines(summary, rows, runs, detail)
        lines += bullets
    if rows:
        lines += ["", "| Role | Model | Calls | Cost |", "| --- | --- | ---: | ---: |"]
        for r in rows:
            priced = f"${Decimal(r['cost']):,.2f}" if r["cost"] else "unpriced"
            role, model = (r[k].replace("|", "\\|") for k in ("role", "model"))
            lines.append(f"| {role} | {model} | {int(r['calls']):,} | {priced} |")
    return "\n".join(lines + more) + "\n"


def loop_usage(ids: list[int]) -> str:
    array = loop_id_array(ids)
    (summary,) = query(USAGE_SUMMARY_SQL.format(ids=array), {})
    rows = query(USAGE_ROWS_SQL.format(ids=array), {})
    (root,) = query(USAGE_ROOT_SQL.format(ids=array), {})
    detail = {
        "root": root,
        "lanes": query(USAGE_LANES_SQL.format(ids=array), {}),
        "tools": query(USAGE_TOOLS_SQL.format(ids=array), {}),
        "ledger": read_ledger(summary.get("report_path") or ""),
    }
    return usage_markdown(summary, rows, len(ids), detail)


def live_loops(repo: str | None, fmt: str) -> int:
    """Print ah.live_loop_tasks(repo): the JSON document for --format json, else one line per loop
    and one indented line per live task. A title is untrusted brief header text: the aligned form
    replaces its non-printable characters (terminal escapes included) with spaces."""
    from .live_loop_tasks import REPO_PATTERN

    if not repo or not re.fullmatch(REPO_PATTERN, repo):
        sys.exit("agent-history: loops --live needs --repo owner/repo")
    rows = query("SELECT ah.live_loop_tasks(:'repo') AS live;", {"repo": repo})
    document = json.loads(rows[0]["live"])
    if fmt == "json":
        print(json.dumps(document, ensure_ascii=False))
        return 0
    for loop in document["loops"]:
        label = f"loop{loop['loop']}" if loop["loop"] is not None else "loop?"
        fields = [label, loop["status"], loop["launched_at"], f"active lanes {loop['active_lanes']}"]
        if loop.get("waiting_until"):
            fields.append(f"waiting until {loop['waiting_until']}")
        print("\t".join(str(field) for field in fields))
        for task in loop["tasks"]:
            title = "".join(ch if ch.isprintable() else " " for ch in task["title"])
            print("\t" + task["id"] + "\t" + title)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agent-history", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--format", choices=["aligned", "csv", "json", "expanded"], default="aligned")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def scoped(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--all", action="store_true", help="all namespaces (Work and Personal)")
        p.add_argument("--ns", action="append", help="explicit namespace (repeatable)")
        return p

    p = scoped(sub.add_parser("search", help="hybrid (BM25 + vector) search over prompts, replies, briefs, reports"))
    p.add_argument("query")
    p.add_argument(
        "--mode",
        choices=["hybrid", "bm25", "vector"],
        default=None,
        help="default: hybrid when a query vector is available, else bm25",
    )
    p.add_argument("--since")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--class", dest="classes", action="append")
    p.add_argument(
        "--w-vec",
        type=float,
        default=None,
        help="override the vector RRF weight (default: 0.35 short/identifier-shaped, 0.7 otherwise)",
    )
    p = sub.add_parser("loops", help="recent loop runs, or with --live the open loops of --repo and their live tasks")
    p.add_argument("--repo", help="owner/repo; required with --live")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument(
        "--live", action="store_true", help="open loops of --repo and their live lanes' tasks (ah.live_loop_tasks)"
    )
    p = sub.add_parser("loop", help="one loop: summary and lanes, or --usage for its closeout")
    p.add_argument("id", help="numeric loop_run_id, or the run's absolute closeout path")
    p.add_argument(
        "--usage", action="store_true", help="print a Markdown usage and list-price cost block for a closeout"
    )
    p = sub.add_parser("compare", help="loops side by side")
    p.add_argument("ids", type=int, nargs="+")
    p = sub.add_parser("session", help="structural timeline of one session")
    p.add_argument("uid")
    p.add_argument("--agent-id", default="")
    p.add_argument("--limit", type=int, default=400)
    p = scoped(sub.add_parser("tools", help="tool reliability"))
    p.add_argument("--since", default="30d")
    p = scoped(sub.add_parser("usage", help="daily token usage"))
    p.add_argument("--days", type=int, default=14)
    p = sub.add_parser("touched", help="sessions that touched a path (SQL LIKE pattern)")
    p.add_argument("path_like")
    p.add_argument("--limit", type=int, default=50)
    p = scoped(sub.add_parser("commits", help="git commits/pushes made by agents"))
    p.add_argument("--since", default="7d")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--quality", action="store_true", help="resolve to git_commit/ci_run ground truth")
    p.add_argument("--weekly", action="store_true", help="with --quality: per repo/week summary")
    p = scoped(sub.add_parser("hooks", help="hook latency per event/name (Claude only)"))
    p.add_argument("--since", default="7d")
    p = scoped(sub.add_parser("denials", help="user rejections, auto-mode blocks, permission-log prompts"))
    p.add_argument("--since", default="7d")
    p = scoped(sub.add_parser("unused", help="installed skills/plugins/MCP servers and whether used"))
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--status", choices=["used", "unused", "new", "unknown"], help="only this status")
    p = sub.add_parser("why", help="the session, prompt and CI behind a commit sha")
    p.add_argument("sha")
    p = scoped(sub.add_parser("resume", help="find a session by phrase and print its resume command"))
    p.add_argument("query")
    p.add_argument("--pick", type=int, default=1, help="resume the Nth match (default 1)")
    p.add_argument("--limit", type=int, default=5, help="matches to list")
    p = sub.add_parser("task", help="effort on one backlog task (sessions mentioning it)")
    p.add_argument("key")
    p = scoped(sub.add_parser("limits", help="rate-limit forecast per window"))
    p = scoped(sub.add_parser("summaries", help="hybrid (BM25 + vector) search over journal session summaries"))
    p.add_argument("query")
    p.add_argument("--since")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument(
        "--mode",
        choices=["hybrid", "bm25", "vector"],
        default=None,
        help="default: hybrid when a query vector is available, else bm25",
    )
    p.add_argument(
        "--w-vec",
        type=float,
        default=None,
        help="override the vector RRF weight (default: 0.35 short/identifier-shaped, 0.7 otherwise)",
    )
    p = scoped(sub.add_parser("week", help="per day/project summary of the last week"))
    p.add_argument("--since", default="7d")
    p = scoped(sub.add_parser("digest", help="correction digest: interrupts, rejections, what followed"))
    p.add_argument("--since", default="7d")
    p = scoped(sub.add_parser("cost", help="daily priced cost per namespace/agent/model"))
    p.add_argument("--days", type=int, default=14)
    p = scoped(sub.add_parser("rules", help="policy-file changes with a before/after effect summary"))
    p.add_argument(
        "path_like",
        nargs="?",
        help="SQL LIKE pattern (default: canonical policy paths -- rules/, reference/, SKILL.md, AGENTS.md, CLAUDE.md)",
    )
    p.add_argument("--since", default="90d")
    p.add_argument("--days", type=int, default=14, help="before/after window size in days")
    p.add_argument(
        "--similar",
        action="store_true",
        help="embed the rule text at each commit and compare similar human prompts before/after",
    )
    p = scoped(sub.add_parser("permissions", help="denial patterns worth a permission rule"))
    p.add_argument("--since", default="7d")
    p = scoped(sub.add_parser("routing", help="model routing / subagent spawn outcome report"))
    p.add_argument("--since", default="30d")
    p = sub.add_parser("changes", help="infra actions (ssh/remote exec) around a host and time")
    p.add_argument("--host", required=True)
    p.add_argument("--around", default="now")
    p.add_argument("--window", default="2h")
    p = scoped(sub.add_parser("ps", help="what's running right now (active root sessions)"))
    p.add_argument("--minutes", type=int, default=15)
    p = scoped(sub.add_parser("threads", help="open threads: unfinished journals, unmerged edits, stale tasks"))
    p.add_argument("--since", default="30d")
    p = scoped(sub.add_parser("lessons", help="belief-correction phrases worth turning into a lesson"))
    p.add_argument("--since", default="30d")
    p.add_argument("--limit", type=int, default=50)
    p = scoped(sub.add_parser("wrapped", help="a fun year(ish)-in-review report"))
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--year", type=int)
    sub.add_parser("schema", help="list tables, views and functions")
    p = sub.add_parser("sql", help="run read-only SQL")
    p.add_argument("statement")
    return parser


def execute(args) -> int:
    fmt = args.format

    if args.cmd == "search":
        since_expr = since_sql(args.since) or "NULL"
        classes = ns_literal(args.classes) if args.classes else "ah.conversation_classes()"
        ns = namespaces(args)
        qvec, mode = resolve_vector(args.query, args.mode)
        w_vec = args.w_vec if args.w_vec is not None else default_vector_weight(args.query)
        variables = {"q": args.query}
        if qvec is not None:
            variables["qvec"] = qvec
        if mode == "vector":
            # Bypass ah.hybrid_search's RRF (its BM25 term always carries weight 1.0, so w_vec alone
            # cannot zero it out): rank purely by ah.vector_candidates' distance instead.
            qvec_expr = ":'qvec'::halfvec"
            return run(
                f"WITH v AS (SELECT message_id, char_start, char_end, distance, "
                f"row_number() OVER (ORDER BY distance)::int AS vec_rank "
                f"FROM ah.vector_candidates({qvec_expr}, {ns_literal(ns)}, {since_expr}, NULL, "
                f"{classes}, {int(args.limit)}) ORDER BY distance LIMIT {int(args.limit)}) "
                f"SELECT m.id AS message_id, m.session_id, m.agent, m.namespace, s.session_uid, "
                f"s.agent_id, m.ts, m.role, m.message_class, "
                f"round((1.0 / (60 + v.vec_rank))::numeric, 4) AS score, NULL::int AS bm25_rank, "
                f"v.vec_rank, left(substr(m.text, v.char_start + 1, v.char_end - v.char_start), 240) "
                f"AS snippet, s.cwd, COALESCE(s.custom_title, s.title) AS title "
                f"FROM v JOIN ah.message m ON m.id = v.message_id "
                f"JOIN ah.session s ON s.id = m.session_id ORDER BY v.vec_rank;",
                variables,
                fmt,
            )
        qvec_expr = ":'qvec'::halfvec" if qvec is not None else "NULL::halfvec"
        return run(
            f"SELECT message_id, session_id, agent, namespace, session_uid, agent_id, ts, role, "
            f"message_class, round(score::numeric, 4) AS score, bm25_rank, vec_rank, snippet, "
            f"cwd, title FROM ah.hybrid_search(:'q', {qvec_expr}, {ns_literal(ns)}, {since_expr}, "
            f"{int(args.limit)}, {w_vec}, {classes});",
            variables,
            fmt,
        )
    if args.cmd == "loops" and args.live:
        return live_loops(args.repo, fmt)
    if args.cmd == "loops":
        repo = ":'repo'" if args.repo else "NULL"
        return run(
            f"SELECT loop_run_id, status, repo_slug, campaign_slug, loop_number, launch_ts, wall_s, lanes, "
            f"sessions, turns_human, output, tool_calls, tool_errors, commits "
            f"FROM ah.recent_loops({repo}, {int(args.limit)});",
            {"repo": args.repo or ""},
            fmt,
        )
    if args.cmd == "loop":
        if args.id.isdigit() and not args.usage:
            rc = run(
                f"SELECT * FROM ah.v_loop_summary WHERE loop_run_id = {int(args.id)};",
                {},
                "expanded" if fmt == "aligned" else fmt,
            )
            return rc or run(f"SELECT * FROM ah.loop_lanes({int(args.id)});", {}, fmt)
        ids = resolve_loop(args.id)
        if args.usage:
            print(loop_usage(ids), end="")
            return 0
        if len(ids) > 1:
            print(f"{len(ids)} launches share this closeout; showing them together as one run.")
        array = loop_id_array(ids)
        rc = run(
            f"SELECT * FROM ah.v_loop_summary WHERE loop_run_id = ANY({array}) ORDER BY launch_ts;",
            {},
            "expanded" if fmt == "aligned" else fmt,
        )
        return rc or run(
            f"SELECT l.* FROM unnest({array}) AS r(id), ah.loop_lanes(r.id) AS l ORDER BY l.first_event_at;",
            {},
            fmt,
        )
    if args.cmd == "compare":
        ids = ",".join(str(int(i)) for i in args.ids)
        return run(
            f"SELECT * FROM ah.loop_compare(ARRAY[{ids}]::bigint[]);", {}, "expanded" if fmt == "aligned" else fmt
        )
    if args.cmd == "session":
        return run(
            f"SELECT * FROM ah.session_timeline(:'uid', :'aid', {int(args.limit)});",
            {"uid": args.uid, "aid": args.agent_id},
            fmt,
        )
    if args.cmd == "tools":
        since = since_sql(args.since) or "now() - interval '30 days'"
        return run(f"SELECT * FROM ah.tool_reliability({since}, {ns_literal(namespaces(args))}) LIMIT 60;", {}, fmt)
    if args.cmd == "usage":
        ns = namespaces(args)
        where = "" if ns is None else f"AND namespace = ANY({ns_literal(ns)})"
        return run(
            f"SELECT day::date, namespace, agent, model, llm_calls, input_uncached, cache_read, cache_write, "
            f"output, reasoning FROM ah.v_daily_usage WHERE day >= now() - interval '{int(args.days)} days' "
            f"{where} ORDER BY day DESC, output DESC NULLS LAST;",
            {},
            fmt,
        )
    if args.cmd == "touched":
        return run(f"SELECT * FROM ah.who_touched(:'p', {int(args.limit)});", {"p": args.path_like}, fmt)
    if args.cmd == "commits":
        since = since_sql(args.since) or "now() - interval '7 days'"
        ns = namespaces(args)
        where = "" if ns is None else f"AND namespace = ANY({ns_literal(ns)})"
        if args.quality and args.weekly:
            # weekly rows are per repo; the namespace filter applies through the per-commit view
            return run(
                f"SELECT date_trunc('week', ts)::date AS week, repo_slug, count(*) AS agent_commits, "
                f"count(*) FILTER (WHERE resolved) AS resolved, "
                f"count(*) FILTER (WHERE ci_conclusion = 'success') AS ci_success, "
                f"count(*) FILTER (WHERE ci_conclusion = 'failure') AS ci_failure, "
                f"count(*) FILTER (WHERE reverted) AS reverted, sum(lines_changed) FILTER (WHERE resolved) "
                f"AS lines_changed FROM ah.v_agent_commit_quality WHERE ts >= {since} {where} "
                f"GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC LIMIT {int(args.limit)};",
                {},
                fmt,
            )
        if args.quality:
            return run(
                f"SELECT ts, sha_short, repo_slug, agent, session_uid, resolved, on_default, lines_changed, "
                f"ci_runs, ci_conclusion, reverted, left(subject, 70) AS subject "
                f"FROM ah.v_agent_commit_quality WHERE ts >= {since} {where} "
                f"ORDER BY ts DESC LIMIT {int(args.limit)};",
                {},
                fmt,
            )
        return run(
            f"SELECT ts, op, sha_short, branch, cwd, agent, session_uid, loop_run_id FROM ah.v_git_commits "
            f"WHERE ts >= {since} {where} ORDER BY ts DESC LIMIT {int(args.limit)};",
            {},
            fmt,
        )
    if args.cmd == "hooks":
        since = since_sql(args.since) or "now() - interval '7 days'"
        return run(f"SELECT * FROM ah.hook_latency({since}, {ns_literal(namespaces(args))}) LIMIT 80;", {}, fmt)
    if args.cmd == "denials":
        since = since_sql(args.since) or "now() - interval '7 days'"
        return run(f"SELECT * FROM ah.denials({since}, {ns_literal(namespaces(args))});", {}, fmt)
    if args.cmd == "unused":
        ns = namespaces(args)
        where = [] if ns is None else [f"namespace = ANY({ns_literal(ns)})"]
        if args.status:
            where.append("status = :'status'")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return run(
            f"SELECT status, kind, name, namespace, machine, home, uses, last_used_at, first_seen_at "
            f"FROM ah.unused_features({int(args.days)}) {clause} LIMIT 400;",
            {"status": args.status or ""},
            fmt,
        )
    if args.cmd == "why":
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.sha):
            sys.exit("agent-history: why takes a 7-40 character hex sha prefix")
        return run("SELECT * FROM ah.why(:'sha');", {"sha": args.sha.lower()}, "expanded" if fmt == "aligned" else fmt)
    if args.cmd == "resume":
        rows = query(
            f"SELECT * FROM ah.find_sessions(:'q', {ns_literal(namespaces(args))}, "
            f"{max(int(args.limit), int(args.pick))});",
            {"q": args.query},
        )
        if not rows:
            sys.exit("agent-history: no session matches that phrase (try --all or other words)")
        for i, r in enumerate(rows, 1):
            kind = "sub-agent" if r["is_subagent"] == "t" else "session"
            print(
                f"{'>' if i == args.pick else ' '} {i}. {r['last_event_at'][:16]}  {r['namespace']}  {kind}  "
                f"{r['session_uid']}  {(r['title'] or '')[:60]}  [{r['cwd']}]",
                file=sys.stderr,
            )
        if not 1 <= args.pick <= len(rows):
            sys.exit(f"agent-history: --pick {args.pick} is out of range 1..{len(rows)}")
        row = rows[args.pick - 1]
        if row["is_subagent"] == "t":
            print(
                f"agent-history: refusing to resume a sub-agent ({row['agent_id'] or row['session_uid']}); "
                f"it cannot be resumed on its own. Its root session:",
                file=sys.stderr,
            )
            roots = query(
                "SELECT agent, namespace, session_uid, cwd, machine, false AS is_subagent FROM ah.session "
                "WHERE agent = :'agent' AND session_uid = :'uid' AND agent_id = '' LIMIT 1",
                {"agent": row["agent"], "uid": row["root_session_uid"] or row["session_uid"]},
            )
            if not roots:
                sys.exit("agent-history: the root session is not in the catalogue")
            row = roots[0]
        command, warnings = resume_command(row)
        for warning in warnings:
            print(f"agent-history: warning: {warning}", file=sys.stderr)
        if command is None:
            return 1
        print(command)
        return 0
    if args.cmd == "task":
        return run(
            "SELECT row_kind, task, task_title, task_status, weight, agent, namespace, session_uid, agent_id, "
            "left(session_title, 50) AS session_title, mentions, in_human, in_brief, first_ts, wall_s, tokens, "
            "round(priced_cost_usd, 2) AS priced_usd, round(claude_cost_usd, 2) AS claude_usd, commits "
            "FROM ah.task_effort(:'k');",
            {"k": args.key},
            fmt,
        )
    if args.cmd == "limits":
        ns = namespaces(args)
        where = "" if ns is None else f"WHERE namespace = ANY({ns_literal(ns)})"
        return run(
            f"SELECT * FROM ah.v_rate_limit_forecast {where} ORDER BY namespace, window_minutes, latest_ts DESC "
            f"LIMIT 100;",
            {},
            fmt,
        )
    if args.cmd == "summaries":
        since_expr = since_sql(args.since) or "NULL"
        ns = namespaces(args)
        qvec, mode = resolve_vector(args.query, args.mode)
        w_vec = args.w_vec if args.w_vec is not None else default_vector_weight(args.query)
        variables = {"q": args.query}
        if qvec is not None:
            variables["qvec"] = qvec
        if mode == "vector":
            qvec_expr = ":'qvec'::halfvec"
            rc = run(
                f"WITH v AS (SELECT summary_id, "
                f"row_number() OVER (ORDER BY distance)::int AS vec_rank "
                f"FROM ah.vector_candidates({qvec_expr}, {ns_literal(ns)}, {since_expr}, NULL, NULL, "
                f"{int(args.limit)}, true) ORDER BY distance LIMIT {int(args.limit)}) "
                f"SELECT ss.analysed_at::date AS analysed, ss.namespace, s.session_uid, "
                f"ss.classification, ss.project, round((1.0 / (60 + v.vec_rank))::numeric, 4) AS score, "
                f"NULL::int AS bm25_rank, v.vec_rank, left(ss.title, 70) AS title, "
                f"left(COALESCE(ss.narrative, ss.objective), 240) AS snippet "
                f"FROM v JOIN ah.session_summary ss ON ss.id = v.summary_id "
                f"LEFT JOIN ah.session s ON s.id = ss.session_id ORDER BY v.vec_rank;",
                variables,
                fmt,
            )
        else:
            qvec_expr = ":'qvec'::halfvec" if qvec is not None else "NULL::halfvec"
            rc = run(
                f"SELECT analysed_at::date AS analysed, namespace, session_uid, classification, project, "
                f"round(score::numeric, 2) AS score, bm25_rank, vec_rank, left(title, 70) AS title, snippet "
                f"FROM ah.hybrid_search_summaries(:'q', {qvec_expr}, {ns_literal(ns)}, {since_expr}, "
                f"{int(args.limit)}, {w_vec});",
                variables,
                fmt,
            )
        if rc == 0 and fmt == "aligned":
            where = "" if ns is None else f"AND s.namespace = ANY({ns_literal(ns)})"
            window = since_sql(args.since) or "now() - interval '30 days'"
            run(
                f"SELECT count(*) AS root_sessions, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ah.session_summary x "
                f"WHERE x.session_id = s.id)) AS summarised, round(avg((EXISTS (SELECT 1 FROM ah.session_summary x "
                f"WHERE x.session_id = s.id))::int), 3) AS coverage FROM ah.session s WHERE NOT s.is_stub "
                f"AND NOT s.is_subagent AND s.last_event_at >= {window} {where};",
                {},
                fmt,
            )
        return rc
    if args.cmd == "week":
        since = since_sql(args.since) or "now() - interval '7 days'"
        return run(f"SELECT * FROM ah.week({since}, {ns_literal(namespaces(args))});", {}, fmt)
    if args.cmd == "digest":
        since = since_sql(args.since) or "now() - interval '7 days'"
        return run(f"SELECT * FROM ah.correction_digest({since}, {ns_literal(namespaces(args))});", {}, fmt)
    if args.cmd == "cost":
        ns = namespaces(args)
        where = "" if ns is None else f"AND namespace = ANY({ns_literal(ns)})"
        return run(
            f"SELECT day::date AS day, namespace, agent, model, sum(llm_calls) AS llm_calls, "
            f"sum(output) AS output, round(sum(priced_cost_usd), 2) AS priced_usd, "
            f"sum(llm_calls) FILTER (WHERE priced_cost_usd IS NULL) AS unpriced_calls "
            f"FROM ah.v_daily_usage WHERE day >= date_trunc('day', now()) - interval '{int(args.days)} days' "
            f"{where} GROUP BY GROUPING SETS ((day, namespace, agent, model), (day), ()) "
            f"ORDER BY day DESC NULLS LAST, namespace NULLS FIRST, priced_usd DESC NULLS LAST LIMIT 500;",
            {},
            fmt,
        )
    if args.cmd == "rules":
        since_expr = since_sql(args.since) or "now() - interval '90 days'"
        path_expr = ":'path_like'" if args.path_like else "NULL"
        path_vars = {"path_like": args.path_like} if args.path_like else {}
        rc = run(
            f"SELECT repo_slug, sha, committed_at, subject, author_is_owner, on_default, path, change, "
            f"insertions, deletions FROM ah.policy_changes({path_expr}, {since_expr}) "
            f"ORDER BY committed_at DESC;",
            path_vars,
            fmt,
        )
        changed = query(
            f"SELECT DISTINCT repo_slug, path FROM ah.policy_changes({path_expr}, {since_expr});", path_vars
        )
        if not changed:
            return rc
        # rule_effect (and --similar's similar_messages) are namespace-scoped human-behaviour signals;
        # policy_changes itself is git-commit based and has no namespace to filter on.
        ns = namespaces(args)
        for row in changed:
            print(f"\n-- rule_effect: {row['repo_slug']} {row['path']} --", file=sys.stderr)
            effects = query(
                f"SELECT * FROM ah.rule_effect(:'repo', :'path', {int(args.days)}, "
                f"{ns_literal(ns)}) ORDER BY committed_at;",
                {"repo": row["repo_slug"], "path": row["path"]},
            )
            run(
                f"SELECT sha, committed_at, before_human_turns, before_corrections, "
                f"before_corrections_per100, before_denials, before_denials_per100, before_tool_errors, "
                f"before_interrupts, after_human_turns, after_corrections, after_corrections_per100, "
                f"after_denials, after_denials_per100, after_tool_errors, after_interrupts "
                f"FROM ah.rule_effect(:'repo', :'path', {int(args.days)}, {ns_literal(ns)}) "
                f"ORDER BY committed_at;",
                {"repo": row["repo_slug"], "path": row["path"]},
                fmt,
            )
            if args.similar:
                for effect in effects:
                    narrow_rule_similarity(
                        row["repo_slug"], row["path"], effect["sha"], effect["committed_at"], int(args.days), ns
                    )
        return 0
    if args.cmd == "permissions":
        since = since_sql(args.since) or "now() - interval '7 days'"
        print(
            "agent-history: manual (human-clicked) approvals are never visible in transcripts; "
            "this is denials, retries and the prompt-log only.",
            file=sys.stderr,
        )
        return run(f"SELECT * FROM ah.permission_candidates({since}, {ns_literal(namespaces(args))});", {}, fmt)
    if args.cmd == "routing":
        since = since_sql(args.since) or "now() - interval '30 days'"
        return run(f"SELECT * FROM ah.routing_report({since}, {ns_literal(namespaces(args))});", {}, fmt)
    if args.cmd == "changes":
        host = _normalise_host(args.host)
        m = DURATION.match(args.window)
        if not m:
            sys.exit(f"agent-history: --window takes <N>h, <N>d or <N>w, not {args.window!r}")
        unit = {"h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
        window_expr = f"interval '{int(m.group(1))} {unit}'"
        variables = {"host": host}
        if args.around in (None, "now", ""):
            around_expr = "now()"
        else:
            variables["around"] = args.around
            around_expr = ":'around'::timestamptz"
        return run(f"SELECT * FROM ah.infra_actions(:'host', {around_expr}, {window_expr});", variables, fmt)
    if args.cmd == "ps":
        ns = namespaces(args)
        where = "" if ns is None else f"WHERE namespace = ANY({ns_literal(ns)})"
        if fmt == "aligned":
            print(
                "agent-history: data_lag_s is seconds since the collector last saw activity for that "
                "session -- the collector runs periodically, so this can be ~10 min behind.",
                file=sys.stderr,
            )
        return run(
            f"SELECT session_id, namespace, agent, session_uid, agent_id, data_lag_s, machine, cwd, "
            f"model, context_tokens, context_window, priced_cost_usd, running_subagents, "
            f"last_human_prompt, last_event_at FROM ah.active_sessions({int(args.minutes)}) {where} "
            f"ORDER BY last_event_at DESC;",
            {},
            fmt,
        )
    if args.cmd == "threads":
        since = since_sql(args.since) or "now() - interval '30 days'"
        return run(
            f"SELECT * FROM ah.open_threads({since}, {ns_literal(namespaces(args))}) "
            f"ORDER BY kind, ts DESC NULLS LAST;",
            {},
            fmt,
        )
    if args.cmd == "lessons":
        since = since_sql(args.since) or "now() - interval '30 days'"
        return run(
            f"SELECT * FROM ah.lesson_candidates({since}, {ns_literal(namespaces(args))}, {int(args.limit)});", {}, fmt
        )
    if args.cmd == "wrapped":
        ns = namespaces(args)
        if args.year:
            if args.since or args.until:
                sys.exit("agent-history: wrapped takes --year or --since/--until, not both")
            since_expr = f"'{int(args.year)}-01-01T00:00:00Z'::timestamptz"
            until_expr = f"'{int(args.year) + 1}-01-01T00:00:00Z'::timestamptz"
        else:
            since_expr = since_sql(args.since) or "now() - interval '365 days'"
            until_expr = since_sql(args.until) or "now()"
        if fmt != "aligned":
            return run(f"SELECT ah.wrapped({since_expr}, {until_expr}, {ns_literal(ns)});", {}, fmt)
        rows_ = query(f"SELECT ah.wrapped({since_expr}, {until_expr}, {ns_literal(ns)}) AS w;", {})
        if not rows_ or not rows_[0].get("w"):
            print("agent-history: no data in that window", file=sys.stderr)
            return 1
        print_wrapped(json.loads(rows_[0]["w"]))
        return 0
    if args.cmd == "schema":
        return run(
            "SELECT c.relname AS name, CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' END AS kind, "
            "obj_description(c.oid) AS comment FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'ah' AND c.relkind IN ('r','v') UNION ALL "
            "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', 'function', NULL "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ah' "
            "ORDER BY 2, 1;",
            {},
            fmt,
        )
    if args.cmd == "sql":
        return run(args.statement, {}, fmt)
    return 2


def main(argv: list[str] | None = None, register=None, dispatch=None) -> int:
    """Extension point: register private command parsers, then dispatch their parsed arguments.

    dispatch returns an integer exit status for a handled command, or None to use public commands.
    """
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if register:
        register(sub)
    args = parser.parse_args(argv)
    if dispatch:
        result = dispatch(args)
        if result is not None:
            return result
    return execute(args)


if __name__ == "__main__":
    sys.exit(main())
