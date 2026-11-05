"""Gated catalogue sections against a recording fake connection: no database, no sleeps."""

from contextlib import contextmanager

import pytest

from agent_history import telemetry
from agent_history.metrics import loop_health
from agent_history.metrics.catalogue import GATED_SECTIONS, TOKEN_QUERY, CatalogueCollector
from scratch_catalogue import Clock

# A fragment unique to each gated section's real query.
HEAVY = {
    "sources": "GROUP BY status",
    "lag": "sum(GREATEST(size_bytes",
    "parse_issues": "FROM ah.parse_issue GROUP BY kind",
    "links": "FROM ah.subagent_spawn WHERE child_session_id",
    "embed_pending": "FROM ah.chunk c WHERE NOT EXISTS",
    "embed_failures": "count(*) FROM ah.embed_failure",
    "embed_vectors": "count(*) FROM ah.embedding",
    "loop_health": "SELECT w.repo, w.harness",
}
TABLES = sorted({table for tables in GATED_SECTIONS.values() for table in tables})


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.conn.execute(query, params)

    def fetchall(self):
        return [
            {
                "harness": "pi",
                "repo": "example/project",
                "completed_timestamp_seconds": 100.0 + self.conn.value,
                **{name: None for name in loop_health.METRICS if name != "completed_timestamp_seconds"},
            }
        ]


class FakeDatabase:
    """Counters per table, a script of failures, and every statement executed."""

    def __init__(self):
        self.counters = {table: (1, 0, 0, 0) for table in TABLES}
        self.token_fails = False
        self.fail_on: set[str] = set()
        self.log: list[str] = []
        self.value = 0

    def touch(self, table, inserts=1):
        node, ins, upd, dele = self.counters[table]
        self.counters[table] = (node, ins + inserts, upd, dele)

    def truncate(self, table):
        node, ins, upd, dele = self.counters[table]
        self.counters[table] = (node + 1, ins, upd, dele)

    @contextmanager
    def connect(self, *args, **kwargs):
        yield Connection(self)

    def heavy(self, since=0):
        return [name for name, text in HEAVY.items() if any(text in sql for sql in self.log[since:])]

    def probes(self, since=0):
        return sorted(sql.split("ah.")[1].split(" ")[0] for sql in self.log[since:] if sql.endswith("WHERE false"))


class Connection:
    read_only = False

    def __init__(self, database):
        self.db = database
        self.value = database.value

    @contextmanager
    def transaction(self):
        yield

    def cursor(self, row_factory=None):
        return Cursor(self)

    def execute(self, query, params=None):
        self.db.log.append(query)
        if query == TOKEN_QUERY and self.db.token_fails:
            raise RuntimeError("token read failed")
        for fragment in self.db.fail_on:
            if fragment in query:
                raise RuntimeError("section failed")
        if query == TOKEN_QUERY:
            return Result([(table, *self.db.counters[table]) for table in params[0]])
        if "FROM ah.refresh_log" in query:
            return Result([(50.0,)])
        if "sum(GREATEST(size_bytes-indexed_offset,0))" in query:
            return Result([(self.db.value, 0)])
        if "GROUP BY status" in query:
            return Result([("indexed", 3 + self.db.value)])
        if "GROUP BY kind" in query:
            return Result([("json_error", 2)])
        if "pg_class WHERE oid" in query:
            return Result([(7,)])
        return Result([(self.db.value,)])


@pytest.fixture
def database(monkeypatch):
    db = FakeDatabase()
    monkeypatch.setattr(telemetry, "db_connect", db.connect)
    return db


def build():
    clock = Clock()
    return CatalogueCollector("synthetic", clock=clock), clock


def render(collector):
    return {family.name: family.samples for family in collector.collect()}


def test_unchanged_tokens_skip_heavy_queries_but_probe_every_table(database):
    collector, clock = build()
    first = render(collector)
    assert database.heavy() == list(HEAVY)
    assert database.probes() == []  # A computation needs no probe; the real query is the probe.
    mark = len(database.log)
    clock.advance(20)
    second = render(collector)
    assert second == first
    assert database.heavy(mark) == []
    expected = sorted(table for tables in GATED_SECTIONS.values() for table in tables)
    assert database.probes(mark) == expected
    assert all(failed is False for _, failed in collector.section_health.values())
    assert set(collector.section_health) >= {f"catalogue_{name}" for name in GATED_SECTIONS}
    # The cheap sections still run on every collection.
    assert any("FROM ah.refresh_log" in sql for sql in database.log[mark:])
    assert any("ah.dirty_session" in sql for sql in database.log[mark:])


def test_changed_token_recomputes_only_its_sections_after_the_minimum_interval(database):
    collector, clock = build()
    render(collector)
    database.touch("parse_issue")
    database.value = 5
    mark = len(database.log)
    clock.advance(10)  # Younger than the minimum interval: bounded staleness, reuse.
    render(collector)
    assert database.heavy(mark) == []
    mark = len(database.log)
    clock.advance(51)
    render(collector)
    assert database.heavy(mark) == ["parse_issues"]
    mark = len(database.log)
    clock.advance(20)
    render(collector)  # The new token is cached now.
    assert database.heavy(mark) == []


def test_truncate_moves_the_token_without_any_tuple_counter(database):
    collector, clock = build()
    render(collector)
    database.truncate("embedding")
    mark = len(database.log)
    clock.advance(61)
    render(collector)
    assert database.heavy(mark) == ["embed_pending", "embed_vectors"]


def test_shared_tables_recompute_sections_unless_completed_scope_is_unchanged(database):
    collector, clock = build()
    render(collector)
    database.touch("session")
    mark = len(database.log)
    clock.advance(61)
    render(collector)
    assert database.heavy(mark) == ["links"]
    database.touch("source_file")
    mark = len(database.log)
    clock.advance(61)
    render(collector)
    assert database.heavy(mark) == ["sources", "lag"]


def test_maximum_age_recomputes_unchanged_sections(database):
    collector, clock = build()
    render(collector)
    clock.advance(CatalogueCollector.MAX_AGE_SECONDS - 1)
    mark = len(database.log)
    render(collector)
    assert database.heavy(mark) == []
    clock.advance(1)
    mark = len(database.log)
    render(collector)
    assert database.heavy(mark) == list(HEAVY)


def test_failed_token_read_recomputes_everything(database):
    collector, clock = build()
    render(collector)
    database.token_fails = True
    clock.advance(20)
    mark = len(database.log)
    families = render(collector)
    assert database.heavy(mark) == list(HEAVY)
    assert database.probes(mark) == []
    assert "agent_history_sources" in families
    assert all(failed is False for _, failed in collector.section_health.values())
    database.token_fails = False
    mark = len(database.log)
    clock.advance(20)
    render(collector)
    # Nothing was cached without a token, so the next collection with tokens computes again.
    assert database.heavy(mark) == list(HEAVY)
    mark = len(database.log)
    clock.advance(20)
    render(collector)
    assert database.heavy(mark) == []


def test_probe_failure_publishes_nothing_and_drops_the_cache_entry(database):
    collector, clock = build()
    first = render(collector)
    database.fail_on = {"ah.embed_failure WHERE false"}
    clock.advance(20)
    mark = len(database.log)
    families = render(collector)
    assert "agent_history_embed_failed_inputs" not in families
    assert "agent_history_sources" in families  # Other sections are unaffected.
    assert database.heavy(mark) == []
    assert collector.section_health["catalogue_embed_failures"][1] is True
    assert collector.section_health["catalogue_embed_vectors"][1] is False
    assert "embed_failures" not in collector.cache
    database.fail_on = set()
    clock.advance(20)
    mark = len(database.log)
    recovered = render(collector)
    assert database.heavy(mark) == ["embed_failures"]  # Recomputed, not served from a dropped entry.
    assert recovered == first
    assert collector.section_health["catalogue_embed_failures"][1] is False


def test_query_failure_publishes_nothing_and_drops_the_cache_entry(database):
    collector, clock = build()
    render(collector)
    database.touch("parse_issue")
    database.fail_on = {HEAVY["parse_issues"]}
    clock.advance(61)
    families = render(collector)
    assert "agent_history_parse_issues_total" not in families
    assert collector.section_health["catalogue_parse_issues"][1] is True
    assert "parse_issues" not in collector.cache
    database.fail_on = set()
    mark = len(database.log)
    clock.advance(1)
    render(collector)
    assert database.heavy(mark) == ["parse_issues"]


def test_completed_dependency_failure_and_relation_rewrite_never_reuse_stale_success(database):
    collector, clock = build()
    first = render(collector)
    database.touch("message")
    database.fail_on = {"SELECT jsonb_build_object("}
    clock.advance(61)
    failed = render(collector)
    assert loop_health.PREFIX + "completed_timestamp_seconds" not in failed
    assert collector.section_health["catalogue_loop_health"][1] is True
    assert "loop_health" not in collector.cache
    database.fail_on = set()
    assert render(collector) == first
    database.truncate("message")
    clock.advance(61)
    mark = len(database.log)
    render(collector)
    assert "loop_health" in database.heavy(mark)
