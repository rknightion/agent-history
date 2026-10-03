"""agent-history: index agent transcripts into ParadeDB and query them.

Writer commands (init, index, rebuild, create-indexes, postpass, embed, collect-git) connect with
the writer DSN: --dsn, else $AGENT_HISTORY_DSN, else `dsn` in the config file. Reader commands
(search, efficiency) use the reader DSN when one is configured, else the writer DSN.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
import sys
import time
from pathlib import Path

from . import load, telemetry
from .config import ConfigError, load_config

SQL_DIR = Path(__file__).resolve().parent / "sql"


def _sources(config, overrides: list[str] | None) -> dict[str, Path]:
    if not overrides:
        return dict(config.sources)
    out: dict[str, Path] = {}
    for item in overrides:
        namespace, sep, directory = item.partition("=")
        if not sep:
            raise SystemExit(f"--source expects NAMESPACE=DIR, got {item!r}")
        from .config import NAMESPACE

        if not NAMESPACE.match(namespace):
            raise SystemExit(
                f"--source namespace {namespace!r} must look like claude-<name>, codex-<name> or pi-<name>"
            )
        out[namespace] = Path(directory).expanduser().resolve()
    return out


def _ensure_search_indexes(conn) -> bool:
    """Create the BM25 indexes after the first load (they are built after bulk loads, not before)."""
    present = conn.execute("SELECT to_regclass('ah.message_search_idx') IS NOT NULL").fetchone()[0]
    conn.commit()
    if present:
        return False
    load.create_post_load_indexes(conn)
    return True


def _reader(args, config):
    import psycopg

    dsn = (
        args.dsn
        or os.environ.get("AGENT_HISTORY_READER_DSN")
        or config.reader_dsn
        or os.environ.get("AGENT_HISTORY_DSN")
        or config.dsn
    )
    if not dsn:
        raise SystemExit("agent-history: no database DSN (set AGENT_HISTORY_DSN or dsn in the config)")
    conn = psycopg.connect(dsn, application_name="agent-history-cli", autocommit=False)
    conn.read_only = True
    return conn


def _print_rows(rows, columns, as_json: bool) -> None:
    if as_json:
        print(json.dumps([dict(zip(columns, row)) for row in rows], default=str, ensure_ascii=False))
        return
    for row in rows:
        print("\t".join("" if v is None else str(v).replace("\n", " ")[:200] for v in row))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-history", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dsn", help="database DSN (overrides AGENT_HISTORY_DSN and the config file)")
    parser.add_argument(
        "--config", type=Path, help="config file (default $AGENT_HISTORY_CONFIG or ~/.config/agent-history/config.toml)"
    )
    parser.add_argument("--format", choices=("aligned", "csv", "json", "expanded"), default="aligned")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="apply the baseline schema, migrations and analytics SQL")
    seed = sub.add_parser("seed-pricing", help="insert the optional list-price seed into ah.model_pricing")
    seed.add_argument("--file", type=Path, default=SQL_DIR / "seed_pricing.sql")

    idx = sub.add_parser("index", help="incremental index of new or changed transcript bytes")
    idx.add_argument(
        "--source",
        action="append",
        metavar="NAMESPACE=DIR",
        help="index these homes instead of the configured ones (repeatable)",
    )
    idx.add_argument(
        "--archive-root",
        type=Path,
        help="index an archive laid out as <root>/<namespace>/... instead of the source map",
    )
    idx.add_argument("--namespace", action="append", help="with --archive-root: only these namespaces")
    idx.add_argument("--limit-files", type=int)
    idx.add_argument("--every", type=float, help="repeat index every N seconds (positive)")

    reb = sub.add_parser("rebuild", help="empty every derived table and re-index from the sources")
    reb.add_argument("--source", action="append", metavar="NAMESPACE=DIR")
    check = sub.add_parser("rebuild-check", help="read-only source-tier counts and rebuild refusal decision")
    check.add_argument("--source", action="append", metavar="NAMESPACE=DIR")

    sub.add_parser("create-indexes", help="create the BM25 indexes (done automatically after the first index)")
    sub.add_parser("postpass", help="run link resolution, rollups and loop tagging for dirty sessions")
    sub.add_parser("stats", help="row counts and meta")

    srch = sub.add_parser("bm25-search", help="BM25 search over prompts, replies, briefs, reports and summaries")
    srch.add_argument("query")
    srch.add_argument("--context", help="named namespace set from the config (default: default_context)")
    srch.add_argument("--since", help="ISO timestamp lower bound")
    srch.add_argument("--limit", type=int, default=20)
    srch.add_argument("--mode", choices=("all", "any", "phrase"), default="all")
    srch.add_argument("--json", action="store_true")

    eff = sub.add_parser("efficiency", help="classify each LLM call by what triggered it (ah.efficiency)")
    eff.add_argument("--session", help="session_uid (root or child)")
    eff.add_argument("--agent-id", default="")
    eff.add_argument("--calls", action="store_true", help="one row per call instead of per-session totals")
    eff.add_argument("--context", help="named namespace set (default: default_context)")
    eff.add_argument("--json", action="store_true")

    emb = sub.add_parser("embed", help="embed new message chunks (needs [embedding] enabled = true)")
    emb.add_argument("--cap", type=int, default=3_000_000, help="token cap for this run (0 = none)")
    emb.add_argument("--daily-cap", type=int, default=30_000_000, help="UTC-day token cap (0 = none)")
    emb.add_argument("--every", type=float, help="repeat embed every N seconds (positive)")
    gcp = sub.add_parser("embed-gc", help="delete orphan ah.embedding rows")
    gcp.add_argument("--dry-run", action="store_true")
    gcp.add_argument("--max-rows", type=int, default=50_000)
    sub.add_parser("create-vector-index", help="build the HNSW index on ah.embedding (after a backfill)")

    sub.add_parser("collect-git", help="ingest commits of the configured [git] repos into ah.git_commit")
    sub.add_parser("mcp", help="run the read-only MCP server on stdio")
    sub.add_parser("exporter", help="serve Prometheus /metrics and /healthz")
    metrics = sub.add_parser("metrics", help="metrics cutover validation")
    parity = metrics.add_subparsers(dest="metrics_command", required=True).add_parser("parity")
    from .metrics.parity import ROSTER

    parity.add_argument("--legacy", required=True, help="legacy exposition file or HTTP URL")
    parity.add_argument("--new", required=True, help="new exposition file or HTTP URL")
    parity.add_argument("--roster", type=Path, default=ROSTER)
    parity.add_argument("--legacy-at", help="actual capture time: Unix seconds or timezone-qualified ISO timestamp")
    parity.add_argument("--new-at", help="actual capture time: Unix seconds or timezone-qualified ISO timestamp")
    parity.add_argument(
        "--ended-loop", action="append", default=[], help="concluded loop label requiring exact equality"
    )

    from . import reader

    reader_parser = reader.build_parser()
    reader_sub = next(a for a in reader_parser._actions if isinstance(a, argparse._SubParsersAction))
    reader_commands = set(reader_sub.choices)
    for name, child in reader_sub.choices.items():
        sub.add_parser(name, parents=[child], add_help=False, help=child.description or child.prog)
    col = sub.add_parser("collect", help="collect repository, CI, tracker, feature and permission metadata")
    col.add_argument("--dry-run", action="store_true")
    col.add_argument("--rescan-days", type=int)
    journal = sub.add_parser("journal-sync", help="sync a read-only journal export view")
    journal.add_argument("--source-db", type=Path)

    args = parser.parse_args(argv)
    workers = {
        "index": "index.pass",
        "embed": "embed.pass",
        "postpass": "postpass.pass",
        "journal-sync": "journal_sync.pass",
        "collect-git": "collect_git.pass",
        "collect": "collect.pass",
    }
    if args.command not in workers and args.command != "exporter":
        return _dispatch(args, parser, argv, reader_commands)
    with telemetry.lifecycle("agent-history-" + args.command):
        if args.command == "exporter" or (args.command in ("index", "embed") and args.every is not None):
            return _dispatch(args, parser, argv, reader_commands)
        with telemetry.pass_span(workers[args.command]) as span:
            result = _dispatch(args, parser, argv, reader_commands)
            if result:
                span.counts({"errors": 1})
            return result


def _dispatch(args, parser, argv, reader_commands):
    from . import reader

    if args.command == "metrics":
        from .metrics.parity import run

        return run(args)

    # Periodic workers reload config inside the suppressed single-shot iteration.
    if args.command in ("index", "embed") and args.every is not None:
        if args.every <= 0:
            parser.error("--every must be positive")
        once = list(argv if argv is not None else sys.argv[1:])
        # Match argparse's accepted long-option abbreviations and =value form.
        # Other options keep their original spelling and ordering.
        for position, token in enumerate(once):
            option, separator, _ = token.partition("=")
            if option.startswith("--") and "--every".startswith(option):
                del once[position : position + (1 if separator else 2)]
                break
        # The public single-shot commands intentionally do not write private Alloy
        # textfiles. Periodic container workers supply a shared-volume destination
        # to the existing producer emitters without changing their catalogue logic.
        output = Path("/var/lib/alloy/textfile-agent-history")
        if args.command == "index":
            original_refresh = load.refresh

            def observed_refresh(conn, *call_args, **call_kwargs):
                parameters = list(call_args)
                if len(parameters) >= 5:
                    parameters[4] = output / "agent-history.prom"
                else:
                    call_kwargs["textfile"] = output / "agent-history.prom"
                return original_refresh(conn, *parameters, **call_kwargs)

            load.refresh = observed_refresh
        else:
            from . import embed

            original_embed_run = embed.run

            def observed_embed_run(conn, *call_args, **call_kwargs):
                try:
                    stats = original_embed_run(conn, *call_args, **call_kwargs)
                except Exception:
                    embed.write_metrics(conn, embed.EmbedStats(), False, output / "agent-history-embed.prom")
                    raise
                embed.write_metrics(conn, stats, True, output / "agent-history-embed.prom")
                return stats

            embed.run = observed_embed_run
        try:
            while True:
                category = None
                try:
                    # A bounded sink: discard routine nested output, including diagnostics
                    # emitted before an exception. Never retain transcript/error text in memory.
                    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                        result = main(once)
                    category = "status" if result else None
                except SystemExit as exc:
                    # Escape, do not retry; the interpreter must never print a raw payload.
                    raise SystemExit(exc.code if isinstance(exc.code, int) else 1) from None
                except ConfigError:
                    category = "config"
                except OSError:
                    category = "io"
                except LookupError:
                    category = "provider"
                except Exception:
                    category = "error"
                if category:
                    print(f"agent-history: {args.command} failed ({category}); retrying", file=sys.stderr)
                else:
                    print(f"agent-history: {args.command} succeeded", file=sys.stderr)
                # Ended iteration spans/logs reach OTLP before the idle interval.
                telemetry.force_flush()
                time.sleep(args.every)
        finally:
            if args.command == "index":
                load.refresh = original_refresh
            else:
                embed.run = original_embed_run

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"agent-history: config: {exc}", file=sys.stderr)
        return 2

    if args.command in reader_commands:
        # Preserve the reader's arguments, SQL and output shape, including search's hybrid modes.
        previous = {key: os.environ.get(key) for key in ("AGENT_HISTORY_CONFIG", "AGENT_HISTORY_READER_DSN")}
        try:
            if args.config:
                os.environ["AGENT_HISTORY_CONFIG"] = str(args.config)
            if args.dsn:
                os.environ["AGENT_HISTORY_READER_DSN"] = args.dsn
            args.cmd = args.command
            return reader.execute(args)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    if args.command == "collect":
        from .collect_git import main as collect_main

        options = ["--config", str(args.config)] if args.config else []
        if args.dry_run:
            options.append("--dry-run")
        if args.rescan_days is not None:
            options.extend(["--rescan-days", str(args.rescan_days)])
        return collect_main(options, dsn=args.dsn)

    if args.command == "journal-sync":
        from .journal_sync import sync

        source = args.source_db or config.collector.journal_db
        if source is None:
            parser.error("journal-sync needs --source-db or collector.journal_db")
        with load.connect(args.dsn or os.environ.get("AGENT_HISTORY_DSN") or config.dsn) as conn:
            print(json.dumps(sync(conn, source), default=str))
        return 0

    if args.command == "exporter":
        from .metrics.archive import ArchiveCollector
        from .metrics.catalogue import CatalogueCollector, RunCollector
        from .metrics.otlp import Bridge, Refresher
        from .metrics.self import SelfCollector
        from .metrics.server import MetricServer, State

        setting = config.exporter
        dsn = (
            args.dsn
            or os.environ.get("AGENT_HISTORY_READER_DSN")
            or config.reader_dsn
            or os.environ.get("AGENT_HISTORY_DSN")
            or config.dsn
        )
        builders = {
            "archive": lambda: ArchiveCollector(
                setting.hot,
                setting.cold,
                setting.incoming,
                setting.conflicts,
                namespaces=config.sources,
                labels=config.metrics_labels,
            ),
            "catalogue": lambda: CatalogueCollector(dsn) if dsn else parser.error("exporter needs a reader DSN"),
            "runs": lambda: RunCollector(Path("/var/lib/alloy/textfile-agent-history")),
            "self": SelfCollector,
        }
        if "efficiency" in setting.collectors:
            from .metrics.efficiency import EfficiencyCollector

            builders["efficiency"] = lambda: EfficiencyCollector(config, setting.state_dir)
        collectors = [builders[name]() for name in setting.collectors]
        host, port = setting.listen.rsplit(":", 1)
        # None unless OTLP metric export is explicitly enabled: then /metrics is unchanged and the same
        # collection also feeds the OTel meter, refreshed on schedule even with no scraper.
        bridge = Bridge.create()
        with MetricServer(
            (host, int(port)),
            collectors,
            State(setting.state_dir, config.metrics_labels),
            setting.refresh_interval,
            bridge=bridge,
        ) as server:
            refresher = Refresher(server) if bridge is not None else None
            if refresher is not None:
                refresher.start()
            try:
                server.serve_forever()
            finally:
                if refresher is not None:
                    refresher.stop()
                server.quiesce()
        return 0

    if args.command == "mcp":
        if args.config:
            os.environ["AGENT_HISTORY_CONFIG"] = str(args.config)
        from .mcp_server import main as mcp_main

        return mcp_main()

    if args.command == "rebuild-check":
        with _reader(args, config) as conn:
            _, report = load.check_rebuild_sources(conn, _sources(config, args.source), config.cold_sources)
            print(json.dumps(report, sort_keys=True))
            return 1 if any(r["refused"] for r in report.values()) else 0

    if args.command in ("bm25-search", "efficiency"):
        conn = _reader(args, config)
        try:
            namespaces = config.namespaces(args.context)
            if args.command == "bm25-search":
                cur = conn.execute(
                    "SELECT ts, namespace, session_uid, agent_id, message_class, round(score::numeric, 3), snippet "
                    "FROM ah.search(%s, %s, %s::timestamptz, %s, %s)",
                    (args.query, namespaces, args.since, args.limit, args.mode),
                )
            elif args.calls:
                cur = conn.execute(
                    "SELECT e.session_uid, e.agent_id, e.ts, e.response_id, e.role, e.trigger, e.model "
                    "FROM ah.efficiency_calls(%s, %s, %s) e ORDER BY e.session_uid, e.agent_id, e.ts, e.response_id",
                    (namespaces, args.session, args.agent_id if args.session else None),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM ah.efficiency(%s, %s, %s)",
                    (namespaces, args.session, args.agent_id if args.session else None),
                )
            rows = cur.fetchall()
            _print_rows(rows, [d.name for d in cur.description], args.json)
            return 0
        except ConfigError as exc:
            print(f"agent-history: {exc}", file=sys.stderr)
            return 2
        finally:
            conn.close()

    conn = load.connect(args.dsn or os.environ.get("AGENT_HISTORY_DSN") or config.dsn)
    try:
        if args.command == "index":
            if args.archive_root:
                stats = load.refresh(conn, args.archive_root, None, args.namespace, args.limit_files, None)
            else:
                stats = load.refresh(
                    conn, None, None, None, args.limit_files, None, sources=_sources(config, args.source)
                )
            if not stats.lock_held:
                _ensure_search_indexes(conn)
            return 1 if stats.errors else 0
        if args.command == "rebuild":
            stats = load.rebuild(
                conn, None, None, None, sources=_sources(config, args.source), cold_sources=config.cold_sources
            )
            return 1 if stats.errors else 0
        if args.command == "stats":
            print(json.dumps(load.stats_report(conn), indent=2, default=str))
            return 0
        if args.command in ("embed", "embed-gc", "create-vector-index"):
            from . import embed

            if args.command == "embed":
                try:
                    provider = embed.provider_from_config(config)
                except LookupError as exc:
                    print(f"agent-history: {exc}", file=sys.stderr)
                    return 2
                stats = embed.run(conn, provider, args.cap, args.daily_cap, gc_interval_hours=embed.GC_INTERVAL_HOURS)
                print(json.dumps(stats.__dict__, default=str))
            elif args.command == "embed-gc":
                print(json.dumps(embed.gc_stats_dict(embed.gc(conn, dry_run=args.dry_run, max_rows=args.max_rows))))
            else:
                embed.create_vector_index(conn)
            return 0
        if args.command == "collect-git":
            from .collect_git import collect

            print(json.dumps(collect(conn, config)))
            return 0
        if not load.try_lock(conn):
            print("another index run holds the lock; exiting")
            return 0
        try:
            if args.command == "init":
                print("applied" if load.apply_schema(conn, force=True) else "unchanged")
                conn.commit()
            elif args.command == "seed-pricing":
                load.apply_schema(conn)
                conn.execute(args.file.read_text())
                conn.commit()
                print("seeded")
            elif args.command == "create-indexes":
                load.create_post_load_indexes(conn)
            elif args.command == "postpass":
                print(json.dumps(load.post_passes(conn)))
                conn.commit()
        finally:
            load.unlock(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
