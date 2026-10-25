"""One pre-I/O target binding for destructive catalogue fault proofs, never live catalogues."""

from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from agent_history import load

KEYS = ("AGENT_HISTORY_TEST_DSN", "AGENT_HISTORY_TEST_READER_DSN", "AGENT_HISTORY_TEST_ADMIN_DSN")
ROLES = ("ah_writer", "ah_reader", "postgres")
TARGET_ENV = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGUSER")
OPTIONS = {"host", "hostaddr", "port", "dbname", "user", "password", "application_name", "connect_timeout", "sslmode"}


@dataclass(frozen=True)
class ScratchCatalogue:
    writer: str
    reader: str
    admin: str


def bind_scratch_catalogue(environ):
    """Parse all three targets before I/O; do not resolve services, DNS, defaults or host lists."""
    if any(environ.get(key) for key in TARGET_ENV):
        raise ValueError("inherited database target settings are not admitted")
    dsns = tuple(environ.get(key, "") for key in KEYS)
    targets = []
    for dsn, role in zip(dsns, ROLES, strict=True):
        if not dsn:
            raise ValueError("all three explicit scratch DSNs are required")
        try:
            options = conninfo_to_dict(dsn)
        except psycopg.Error:
            raise ValueError("invalid scratch connection parameters") from None
        host = options.get("host")
        hostaddr = options.get("hostaddr", host)
        port = options.get("port", "")
        if (
            options.keys() - OPTIONS
            or options.get("dbname") != "agent_history_test"
            or options.get("user") != role
            or host not in {"127.0.0.1", "::1"}
            or hostaddr != host
            or not port.isascii()
            or not port.isdecimal()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError("an explicit loopback disposable catalogue and dedicated roles are required")
        targets.append((host, hostaddr, int(port), options["dbname"]))
    if len(set(targets)) != 1:
        raise ValueError("writer, reader and admin must name the same scratch target")
    return ScratchCatalogue(*dsns)


@pytest.fixture(scope="module")
def scratch_catalogue():
    import os

    try:
        return bind_scratch_catalogue(os.environ)
    except ValueError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def scratch_conn(scratch_catalogue):
    # This fixture, unlike an importing module's skip marker, enforces admission before schema I/O.
    connection = load.connect(scratch_catalogue.writer)
    try:
        load.apply_schema(connection, force=True)
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _acl(admin, table):
    return admin.execute(
        "SELECT a.grantor, a.grantee, a.privilege_type, a.is_grantable "
        "FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a "
        "WHERE c.oid=%s::regclass ORDER BY 1,2,3,4",
        (f"ah.{table}",),
    ).fetchall()


@contextmanager
def table_fault(target, table, fault):
    """Fault only the bound scratch target; restore the exact original effective ACL on recovery."""
    if table not in {"refresh_log", "session", "embed_failure", "loop_run"}:
        raise ValueError("unsupported fault table")
    if fault not in {"permission", "lock"}:
        raise ValueError("unsupported fault")
    relation = sql.Identifier("ah", table)
    with psycopg.connect(target.admin, autocommit=True) as admin:
        if fault == "lock":
            admin.execute("BEGIN")
            try:
                admin.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(relation))
                yield
            finally:
                admin.execute("ROLLBACK")
        else:
            before = _acl(admin, table)
            owner, writer, reader = admin.execute(
                "SELECT relowner, 'ah_writer'::regrole::oid, 'ah_reader'::regrole::oid "
                "FROM pg_class WHERE oid=%s::regclass",
                (f"ah.{table}",),
            ).fetchone()
            # Refuse an unexpected grantor, grant option or absent grant rather than manufacture
            # privileges on recovery. Revoke and restore as the original owner/grantor.
            assert owner == writer
            assert [row for row in before if row[1:3] == (reader, "SELECT")] == [(owner, reader, "SELECT", False)]
            admin.execute("SET ROLE ah_writer")
            try:
                admin.execute(sql.SQL("REVOKE SELECT ON {} FROM ah_reader").format(relation))
                try:
                    yield
                finally:
                    admin.execute(sql.SQL("GRANT SELECT ON {} TO ah_reader").format(relation))
            finally:
                admin.execute("RESET ROLE")
            assert _acl(admin, table) == before
