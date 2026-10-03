"""Read-only sync of agentic-journal's per-conversation analyses into ah.session_summary.

agentic-journal (container on camden) exposes exactly one SQLite view for this purpose,
`ah_export_session_summary` in /opt/agentic-journal/app.db. This module reads ONLY that view --
never any other table in app.db, and never the old journal.db (deleted). Its columns, all TEXT:

    journal_conversation_id, agent, session_uid, agent_id, namespace, revision_id,
    analysed_at (RFC 3339 UTC), model, title, objective, narrative,
    outcomes (JSON array of strings), unfinished (JSON array of strings),
    classification, project, topics_json (JSON array of {slug, provenance, confidence}),
    app_instance_id (constant for the life of one journal database)

Identity mapping is a direct natural-key join, no hashing: a row's (agent, session_uid, agent_id)
-- agent_id '' for a main session, NULL treated as '' -- looks up ah.session (agent, session_uid,
agent_id). The old sidechain branch-id hashing is gone entirely. An unmatched row is still stored,
with session_id NULL.

Sync is incremental by an analysed_at watermark (ah.meta key journal_sync_at), plus a daily full
pass (ah.meta key journal_full_sync_at) that also deletes session_summary/session_topic rows whose
journal_conversation_id no longer appears in the view. If the view's app_instance_id differs from
the one stored in ah.meta (key journal_app_instance_id) -- or none is stored yet and the view is
non-empty -- every journal-sourced row is wiped and this run does a full resync. A view holding more
than one distinct app_instance_id is refused rather than guessed at.

The journal database is opened read-only (`file:...?mode=ro`) and never written. Any of: the file
missing, the file unreadable or locked (the indexer runs under systemd ProtectSystem=strict, a
read-only view of the filesystem), the view missing, or the view lacking a required column, is
treated as "nothing to do this run" -- reported in the returned dict, never raised. Only an
unexpected Postgres error propagates (refresh() already wraps this call in try/except).

Skip reasons are printed by the CLI and shipped as worker logs, so they are fixed text plus at most an
exception type name: never the database path or a driver message, which can quote schema or row text.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from . import telemetry
from psycopg.types.json import Jsonb

APP_DB = Path("/opt/agentic-journal/app.db")
VIEW = "ah_export_session_summary"
COLUMNS = (
    "journal_conversation_id",
    "agent",
    "session_uid",
    "agent_id",
    "namespace",
    "revision_id",
    "analysed_at",
    "model",
    "title",
    "objective",
    "narrative",
    "outcomes",
    "unfinished",
    "classification",
    "project",
    "topics_json",
    "app_instance_id",
)
BATCH = 500


class _Skip(Exception):
    """Any handled reason to stop this run without touching Postgres or raising out of sync()."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _ts(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("analysed_at must be a timestamp string")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("analysed_at must include a timezone")
    return timestamp


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _json_array(value: Any) -> list | None:
    """A JSON array of strings, or None if the field is empty/absent/not a JSON array."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _parse_topics(value: Any) -> list[tuple[str, str | None, float | None]] | None:
    """[(slug, provenance, confidence), ...] from topics_json; None only means "invalid, drop it"."""
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    out: list[tuple[str, str | None, float | None]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        slug = _text(item.get("slug"))
        if not slug:
            continue
        provenance = _text(item.get("provenance"))
        confidence = item.get("confidence")
        out.append((slug, provenance, confidence if isinstance(confidence, (int, float)) else None))
    return out


def _open_view(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise _Skip("journal database does not exist")
    try:
        view = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        view.execute("PRAGMA query_only = 1")
        view.execute(f"SELECT {', '.join(COLUMNS)} FROM {VIEW} LIMIT 0")
    except sqlite3.Error as exc:
        try:
            view.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        msg = str(exc)
        if "no such table" in msg or "no such view" in msg:
            raise _Skip(f"view {VIEW} does not exist") from exc
        if "no such column" in msg:
            raise _Skip(f"view {VIEW} is missing a required column") from exc
        raise _Skip(f"cannot open/read the journal database ({type(exc).__name__})") from exc
    return view


def _session_map(conn: psycopg.Connection) -> dict[tuple[str, str, str], tuple[int, str | None]]:
    out: dict[tuple[str, str, str], tuple[int, str | None]] = {}
    for sid, agent, uid, agent_id, namespace in conn.execute(
        "SELECT id, agent, session_uid, agent_id, namespace FROM ah.session"
    ):
        out[(agent, uid, agent_id or "")] = (sid, namespace)
    return out


@telemetry.instrument_pass("journal_sync.pass")
def sync(conn: psycopg.Connection, db_path: Path = APP_DB) -> dict[str, Any]:
    result: dict[str, Any] = {
        "journal_rows": 0,
        "journal_matched": 0,
        "journal_unmatched": 0,
        "journal_deleted": 0,
        "journal_bad_json": 0,
    }
    skip: _Skip | None = None
    with telemetry.operation("journal.read", {"db.system.name": "sqlite"}):
        try:
            view = _open_view(db_path)
        except _Skip as exc:
            # An expected skip is not a failed read: leave the span successful.
            skip = exc
    if skip is not None:
        result["journal_skipped_reason"] = skip.reason
        return result

    try:
        try:
            with telemetry.operation("journal.read", {"db.system.name": "sqlite"}):
                instance_ids = sorted(
                    {row[0] for row in view.execute(f"SELECT app_instance_id FROM {VIEW}") if row[0] is not None}
                )
        except sqlite3.Error as exc:
            result["journal_skipped_reason"] = f"cannot read {VIEW} ({type(exc).__name__})"
            return result
        if len(instance_ids) > 1:
            result["journal_skipped_reason"] = (
                f"{VIEW} holds {len(instance_ids)} distinct app_instance_id values, refusing to guess"
            )
            return result
        current_instance = instance_ids[0] if instance_ids else None

        stored = conn.execute("SELECT value FROM ah.meta WHERE key = 'journal_app_instance_id'").fetchone()
        stored_instance = stored[0] if stored else None
        reset = current_instance is not None and current_instance != stored_instance

        watermark = conn.execute("SELECT value FROM ah.meta WHERE key = 'journal_sync_at'").fetchone()
        since = watermark[0] if watermark else ""
        # A daily full pass picks up rows whose session wasn't indexed yet when first seen, and
        # prunes rows the journal no longer holds. A reset always runs as a full pass.
        full = conn.execute(
            "SELECT value::timestamptz < now() - interval '1 day' FROM ah.meta WHERE key = 'journal_full_sync_at'"
        ).fetchone()
        full_pass = reset or full is None or bool(full[0])
        if full_pass:
            since = ""
        conn.commit()

        query = f"SELECT {', '.join(COLUMNS)} FROM {VIEW}"
        params: tuple = ()
        if since:
            query += " WHERE julianday(analysed_at) > julianday(?)"
            params = (since,)
        query += " ORDER BY analysed_at"
        try:
            with telemetry.operation("journal.read", {"db.system.name": "sqlite"}) as read_span:
                rows = view.execute(query, params).fetchall()
                read_span.set_attribute("journal_rows", len(rows))
        except sqlite3.Error as exc:
            result["journal_skipped_reason"] = f"cannot read rows from {VIEW} ({type(exc).__name__})"
            return result
    finally:
        view.close()

    # Validate the entire selected source before reset, batching or reconciliation.
    # An invalid retained record is not evidence that its summary has disappeared.
    for row in rows:
        try:
            _ts(row[COLUMNS.index("analysed_at")])
        except (TypeError, ValueError):
            result["journal_skipped_reason"] = (
                "selected source record has invalid required analysed_at; catalogue unchanged"
            )
            return result

    # Reset, all batches, pruning and watermarks form one catalogue update. Nested
    # batch transactions below are savepoints, never independently durable writes.
    with conn.transaction():
        return _sync_rows(conn, rows, result, reset, full_pass, current_instance, since)


def _sync_rows(conn, rows, result, reset, full_pass, current_instance, since):
    if reset:
        with conn.transaction():
            conn.execute("DELETE FROM ah.session_topic")
            cur = conn.execute("DELETE FROM ah.session_summary")
            result["journal_deleted"] += cur.rowcount
        result["journal_reset"] = True

    if not rows:
        if current_instance is not None:
            conn.execute(
                "INSERT INTO ah.meta VALUES ('journal_app_instance_id', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (current_instance,),
            )
        if full_pass:
            # Nothing left in the view: prune everything, and mark the full pass done.
            cur = conn.execute("DELETE FROM ah.session_topic")
            cur2 = conn.execute("DELETE FROM ah.session_summary")
            result["journal_deleted"] += cur2.rowcount
            conn.execute(
                "INSERT INTO ah.meta VALUES ('journal_full_sync_at', now()::text) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            )
        return result

    sessions = _session_map(conn)
    newest = since
    seen_ids: set[str] = set()
    for start in range(0, len(rows), BATCH):
        with conn.transaction():
            for row in rows[start : start + BATCH]:
                (
                    conv,
                    agent,
                    session_uid,
                    agent_id,
                    namespace,
                    revision_id,
                    analysed_at,
                    model,
                    title,
                    objective,
                    narrative,
                    outcomes_raw,
                    unfinished_raw,
                    classification,
                    project,
                    topics_raw,
                    app_instance_id,
                ) = row
                analysed_ts = _ts(analysed_at)  # all selected timestamps were validated before any mutation
                seen_ids.add(conv)
                result["journal_rows"] += 1
                if not newest or analysed_ts > _ts(newest):
                    newest = analysed_at
                agent_id_norm = agent_id or ""
                match = sessions.get((agent, session_uid, agent_id_norm))
                sid = match[0] if match else None
                if sid is None:
                    result["journal_unmatched"] += 1
                else:
                    result["journal_matched"] += 1
                row_namespace = (match[1] if match else None) or _text(namespace)

                outcomes = _json_array(outcomes_raw)
                if outcomes_raw and outcomes is None:
                    result["journal_bad_json"] += 1
                unfinished = _json_array(unfinished_raw)
                if unfinished_raw and unfinished is None:
                    result["journal_bad_json"] += 1
                topics = _parse_topics(topics_raw)
                if topics_raw and topics is None:
                    result["journal_bad_json"] += 1

                conn.execute(
                    "INSERT INTO ah.session_summary (journal_conversation_id, session_id, journal_revision_id, "
                    "namespace, agent, session_uid, agent_id, app_instance_id, title, objective, narrative, "
                    "outcomes, unfinished, classification, project, model, analysed_at) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (journal_conversation_id) DO UPDATE SET "
                    "session_id = EXCLUDED.session_id, journal_revision_id = EXCLUDED.journal_revision_id, "
                    "namespace = EXCLUDED.namespace, agent = EXCLUDED.agent, session_uid = EXCLUDED.session_uid, "
                    "agent_id = EXCLUDED.agent_id, app_instance_id = EXCLUDED.app_instance_id, "
                    "title = EXCLUDED.title, objective = EXCLUDED.objective, narrative = EXCLUDED.narrative, "
                    "outcomes = EXCLUDED.outcomes, unfinished = EXCLUDED.unfinished, "
                    "classification = EXCLUDED.classification, project = EXCLUDED.project, model = EXCLUDED.model, "
                    "analysed_at = EXCLUDED.analysed_at "
                    "WHERE session_summary.analysed_at <= EXCLUDED.analysed_at",
                    (
                        conv,
                        sid,
                        revision_id,
                        row_namespace,
                        agent,
                        session_uid,
                        agent_id_norm,
                        app_instance_id,
                        _text(title),
                        _text(objective),
                        _text(narrative),
                        Jsonb(outcomes) if outcomes is not None else None,
                        Jsonb(unfinished) if unfinished is not None else None,
                        _text(classification),
                        _text(project),
                        _text(model),
                        analysed_ts,
                    ),
                )
                conn.execute("DELETE FROM ah.session_topic WHERE journal_conversation_id = %s", (conv,))
                for slug, provenance, confidence in topics or []:
                    conn.execute(
                        "INSERT INTO ah.session_topic (journal_conversation_id, topic, session_id, provenance, "
                        "confidence) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (conv, slug, sid, provenance, confidence),
                    )

    if full_pass:
        ids = list(seen_ids)
        conn.execute("DELETE FROM ah.session_topic WHERE NOT (journal_conversation_id = ANY(%s))", (ids,))
        cur = conn.execute("DELETE FROM ah.session_summary WHERE NOT (journal_conversation_id = ANY(%s))", (ids,))
        result["journal_deleted"] += cur.rowcount

    conn.execute(
        "INSERT INTO ah.meta VALUES ('journal_sync_at', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (newest,),
    )
    if full_pass:
        conn.execute(
            "INSERT INTO ah.meta VALUES ('journal_full_sync_at', now()::text) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    if current_instance is not None:
        conn.execute(
            "INSERT INTO ah.meta VALUES ('journal_app_instance_id', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (current_instance,),
        )
    return result
