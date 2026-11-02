"""Gated catalogue sections against real statistics: tokens, TRUNCATE and the skipped heavy queries."""

import psycopg
import pytest
from scratch_catalogue import Clock, flush_stats, scratch_catalogue, scratch_conn  # noqa: F401 - guarded fixtures

from agent_history import load, telemetry
from agent_history.metrics.catalogue import CatalogueCollector

PARSE_ISSUES = "FROM ah.parse_issue GROUP BY kind"
EMBED_FAILURES = "count(*) FROM ah.embed_failure"
SOURCES = "FROM ah.source_file GROUP BY status"


class Spy(psycopg.Cursor):
    log: list[str] = []

    def execute(self, query, *args, **kwargs):
        text = query if isinstance(query, str) else query.as_string()
        Spy.log.append(text)
        return super().execute(query, *args, **kwargs)


@pytest.fixture
def conn(scratch_conn):  # noqa: F811 - imported fixture
    scratch_conn.execute(
        "TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE"
    )
    scratch_conn.commit()
    yield scratch_conn
    scratch_conn.rollback()


@pytest.fixture
def spy(monkeypatch):
    Spy.log = []
    # Bypass the telemetry wrapper: the spy sees every statement the collector's connection executes.
    monkeypatch.setattr(
        telemetry, "db_connect", lambda *args, **kwargs: psycopg.connect(*args, cursor_factory=Spy, **kwargs)
    )
    return Spy


def values(collector):
    families = {family.name: family.samples for family in collector.collect()}
    return {name: {s.labels: s.value for s in samples} for name, samples in families.items()}


def ran(fragment, since):
    return any(fragment in sql for sql in Spy.log[since:])


def test_unchanged_catalogue_skips_heavy_queries_and_changes_are_seen(conn, scratch_catalogue, spy):  # noqa: F811
    source = conn.execute(
        "INSERT INTO ah.source_file (rel_path, namespace, agent, profile, file_role, tier) "
        "VALUES ('synthetic/gating.jsonl','synthetic','pi','local','main','hot') RETURNING id"
    ).fetchone()[0]
    conn.execute("INSERT INTO ah.parse_issue (source_id, byte_offset, kind) VALUES (%s, 0, 'json_error')", (source,))
    flush_stats(conn)
    conn.commit()
    clock = Clock()
    collector = CatalogueCollector(scratch_catalogue.reader, clock=clock)

    issues = "agent_history_parse_issues_total"
    first = values(collector)
    assert first[issues] == {(("kind", "json_error"),): 1}
    assert ran(PARSE_ISSUES, 0) and ran(EMBED_FAILURES, 0)

    # Nothing changed: no heavy query, the same values, and a zero-row probe on every table read.
    mark = len(Spy.log)
    clock.advance(20)
    assert values(collector) == first
    assert not any(ran(fragment, mark) for fragment in (PARSE_ISSUES, EMBED_FAILURES, SOURCES))
    assert ran("FROM ah.parse_issue WHERE false", mark) and ran("FROM ah.loop_run WHERE false", mark)
    assert all(failed is False for _, failed in collector.section_health.values())

    # A committed write moves the token; inside the minimum interval the cached value is reused...
    conn.execute("INSERT INTO ah.parse_issue (source_id, byte_offset, kind) VALUES (%s, 1, 'json_error')", (source,))
    flush_stats(conn)
    conn.commit()
    mark = len(Spy.log)
    assert values(collector)[issues] == {(("kind", "json_error"),): 1}
    assert not ran(PARSE_ISSUES, mark)

    # ...and past it only the section whose table changed recomputes.
    clock.advance(61)
    mark = len(Spy.log)
    assert values(collector)[issues] == {(("kind", "json_error"),): 2}
    assert ran(PARSE_ISSUES, mark)
    assert not ran(EMBED_FAILURES, mark) and not ran(SOURCES, mark)


def test_truncate_is_seen_although_it_moves_no_tuple_counter(conn, scratch_catalogue, spy):  # noqa: F811
    conn.execute("INSERT INTO ah.embed_failure (model, input_sha256) VALUES ('synthetic', 'a')")
    flush_stats(conn)
    conn.commit()
    clock = Clock()
    collector = CatalogueCollector(scratch_catalogue.reader, clock=clock)
    assert values(collector)["agent_history_embed_failed_inputs"] == {(): 1}
    token = collector.cache["embed_failures"].token
    conn.execute("TRUNCATE ah.embed_failure")
    flush_stats(conn)
    conn.commit()
    clock.advance(61)
    mark = len(Spy.log)
    assert values(collector)["agent_history_embed_failed_inputs"] == {(): 0}
    assert ran(EMBED_FAILURES, mark)
    assert collector.cache["embed_failures"].token != token
    # Only the file node differs: the insert, update and delete counters are all unchanged.
    (before,), (after,) = token, collector.cache["embed_failures"].token
    assert before[0] != after[0] and before[1:] == after[1:]
