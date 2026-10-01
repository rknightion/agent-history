"""Read the catalogue using bounded read-only queries.

Use --format aligned, csv, json or expanded. Search defaults to hybrid when a query vector is
available and otherwise falls back to BM25. Namespaces follow the configured context.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
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
            import psycopg

            # Service-file values beat PG environment defaults. Resolve explicit
            # DSN overrides through libpq before transferring the effective
            # settings to psql. This connection runs no SQL; secrets stay in memory.
            try:
                with psycopg.connect(dsn) as connection:
                    # Keep values equal to compiled defaults too: omitting port
                    # 5432, for example, would revive an inherited wrong PGPORT.
                    effective = {
                        option.keyword.decode(): option.val.decode()
                        for option in connection.pgconn.info
                        if option.val is not None
                    }
                    effective["password"] = connection.info.password
            except psycopg.Error:
                sys.exit("agent-history: cannot resolve reader service connection")
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
    if agent not in ("claude", "codex") or ctx not in ("personal", "work"):
        return None, [f"namespace {row['namespace']} has no live home (retired or standalone); cannot resume"]
    var = "CLAUDE_CONFIG_DIR" if agent == "claude" else "CODEX_HOME"
    native_ctx = os.environ.get("AGENT_HISTORY_NATIVE_CONTEXT", context())
    native, named = Path.home() / f".{agent}", Path.home() / f".{agent}-{ctx}"
    if ctx == native_ctx and native.is_dir():
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
    """host/owner/name from a git remote URL -- mirrors bin/agent-history-collect's repo_slug()."""
    if not remote:
        return None
    url = remote.strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
    if not m:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+):(?!/)(.+)$", url)  # git@host:owner/name
    if not m:
        return None
    host, path = m.group(1).lower(), m.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
        return None
    return f"{host}/{parts[0]}/{parts[1]}".lower()


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
    p = sub.add_parser("loops", help="recent loop runs")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("loop", help="one loop: summary and lanes")
    p.add_argument("id", type=int)
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
        rc = run(
            f"SELECT * FROM ah.v_loop_summary WHERE loop_run_id = {int(args.id)};",
            {},
            "expanded" if fmt == "aligned" else fmt,
        )
        return rc or run(f"SELECT * FROM ah.loop_lanes({int(args.id)});", {}, fmt)
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
