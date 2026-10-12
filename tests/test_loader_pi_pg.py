"""pi ingestion through the loader against a disposable ParadeDB database (see test_loader_pg.py).

Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database. Needs the schema to accept
agent 'pi' in ah.source_file and ah.session (their agent CHECK constraints).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

from test_loader_pg import DSN, clean, conn  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                                reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

from agent_history import load  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "pi"
ROOT_UID = "01900000-0000-7000-8000-00000000a001"


def test_pi_header_family_is_coherent_across_independently_arriving_parents(clean, tmp_path):  # noqa: F811
    spec = importlib.util.spec_from_file_location("pi_lineage_fixture", FIX / "lineage.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    fixture.initial(base)

    def refresh():
        stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
        assert stats.errors == 0
        return stats

    def family(uid):
        return clean.execute(
            "SELECT c.parent_session_uid, p.session_uid, c.root_session_uid, r.session_uid, c.spawn_depth "
            "FROM ah.session c LEFT JOIN ah.session p ON p.id=c.parent_session_id "
            "LEFT JOIN ah.session r ON r.id=c.root_session_id WHERE c.agent='pi' AND c.session_uid=%s",
            (uid,)).fetchone()

    assert refresh().files_parsed == 6
    for uid in (fixture.CHILD, fixture.NESTED_PARENT):
        assert family(uid) == (fixture.HEADER_PARENT, None, None, None, None)
    assert family(fixture.NESTED_CHILD) == (None, fixture.NESTED_PARENT, None, None, None)
    for uid in (fixture.MISSING, fixture.FIXED):
        assert family(uid) == (fixture.DIRECTORY_ROOT, fixture.DIRECTORY_ROOT,
                               fixture.DIRECTORY_ROOT, fixture.DIRECTORY_ROOT, 1)
    # Historical v10 rows carry the old mixed family and may have no dirty transcript. Repair
    # their existing metadata without a parser upgrade, content replay or a source append.
    clean.execute("UPDATE ah.session SET root_session_uid=%s, root_session_id="
                  "(SELECT id FROM ah.session WHERE session_uid=%s), spawn_depth=1 "
                  "WHERE session_uid IN (%s,%s)",
                  (fixture.DIRECTORY_ROOT, fixture.DIRECTORY_ROOT, fixture.CHILD, fixture.NESTED_PARENT))
    clean.execute("DELETE FROM ah.dirty_session")
    clean.commit()
    assert refresh().files_parsed == 0
    for uid in (fixture.CHILD, fixture.NESTED_PARENT):
        assert family(uid) == (fixture.HEADER_PARENT, None, None, None, None)
    fixture.parent_arrives(base)
    assert refresh().files_parsed == 1
    for uid in (fixture.CHILD, fixture.NESTED_PARENT):
        assert family(uid) == (fixture.HEADER_PARENT, fixture.HEADER_PARENT, None, None, None)
    assert family(fixture.NESTED_CHILD) == (None, fixture.NESTED_PARENT, None, None, None)
    fixture.root_arrives(base)
    assert refresh().files_parsed == 1
    for uid in (fixture.CHILD, fixture.NESTED_PARENT):
        assert family(uid) == (fixture.HEADER_PARENT, fixture.HEADER_PARENT,
                               fixture.ACTUAL_ROOT, fixture.ACTUAL_ROOT, 2)
    assert family(fixture.NESTED_CHILD) == (None, fixture.NESTED_PARENT,
                                          fixture.ACTUAL_ROOT, fixture.ACTUAL_ROOT, 3)
    assert family(fixture.HEADER_PARENT) == (fixture.ACTUAL_ROOT, fixture.ACTUAL_ROOT,
                                           fixture.ACTUAL_ROOT, fixture.ACTUAL_ROOT, 1)
    before = {uid: family(uid) for uid in (fixture.CHILD, fixture.NESTED_PARENT, fixture.NESTED_CHILD)}
    assert refresh().files_parsed == 0
    assert {uid: family(uid) for uid in before} == before
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn").fetchone()[0] == 0
    assert clean.execute("SELECT count(*) FROM ah.message").fetchone()[0] == 0
    # A real retained foreground spawn under the directory root must not override the usable
    # header parent, even though its recorded sessionFile matches the child's physical path.
    fixture.path_spawn(base)
    assert refresh().files_parsed == 1
    assert clean.execute("SELECT count(*), count(child_session_id) FROM ah.subagent_spawn").fetchone() == (1, 0)
    assert family(fixture.CHILD) == before[fixture.CHILD]
    assert clean.execute("SELECT agent_type FROM ah.session WHERE session_uid=%s",
                         (fixture.CHILD,)).fetchone() == (None,)
    clean.rollback()


def _lineage_fixture():
    spec = importlib.util.spec_from_file_location("pi_lineage_fixture", FIX / "lineage.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


def _pi_lineage_snapshot(db):
    """Every session and spawn column the lineage repair may touch, keyed by stable uids."""
    sessions = db.execute(
        "SELECT c.session_uid, p.session_uid, c.root_session_uid, r.session_uid, c.spawn_depth, "
        "c.parent_call_uid, c.agent_type, c.agent_type_source FROM ah.session c "
        "LEFT JOIN ah.session p ON p.id=c.parent_session_id LEFT JOIN ah.session r ON r.id=c.root_session_id "
        "WHERE c.agent='pi' ORDER BY c.session_uid").fetchall()
    spawns = db.execute(
        "SELECT s.spawn_uid, p.session_uid, c.session_uid, s.requested_type FROM ah.subagent_spawn s "
        "LEFT JOIN ah.session p ON p.id=s.parent_session_id LEFT JOIN ah.session c ON c.id=s.child_session_id "
        "ORDER BY s.spawn_uid").fetchall()
    return sessions, spawns


def test_pi_upgrade_from_stale_spawn_links_equals_a_fresh_index(clean, tmp_path):  # noqa: F811
    fixture = _lineage_fixture()
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    fixture.initial(base)
    fixture.parent_arrives(base)
    fixture.root_arrives(base)
    fixture.path_spawn(base)

    def refresh():
        stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
        assert stats.errors == 0
        return stats

    assert refresh().files_parsed == 8
    fresh = _pi_lineage_snapshot(clean)
    # The directory-root spawn names the child by path, but the child's header names another
    # parent: a fresh index leaves the spawn unlinked and the child untyped.
    assert fresh[1] == [("lineage-spawn:0", fixture.DIRECTORY_ROOT, None, "mapper")]
    # Reproduce the catalogue an older indexer left behind: it linked that spawn to the child,
    # took parent/root/depth from the directory, and copied the spawn's call and type onto it.
    clean.execute(
        "UPDATE ah.session SET parent_session_id=(SELECT id FROM ah.session WHERE session_uid=%s), "
        "root_session_uid=%s, root_session_id=(SELECT id FROM ah.session WHERE session_uid=%s), spawn_depth=1, "
        "parent_call_uid='lineage-spawn:0', agent_type='mapper', agent_type_source='explicit' "
        "WHERE agent='pi' AND session_uid=%s",
        (fixture.DIRECTORY_ROOT, fixture.DIRECTORY_ROOT, fixture.DIRECTORY_ROOT, fixture.CHILD))
    clean.execute("UPDATE ah.subagent_spawn SET child_session_id=(SELECT id FROM ah.session WHERE session_uid=%s) "
                  "WHERE spawn_uid='lineage-spawn:0'", (fixture.CHILD,))
    clean.execute("DELETE FROM ah.dirty_session")
    clean.commit()
    assert _pi_lineage_snapshot(clean) != fresh
    assert refresh().files_parsed == 0
    assert _pi_lineage_snapshot(clean) == fresh
    assert refresh().files_parsed == 0
    assert _pi_lineage_snapshot(clean) == fresh
    clean.rollback()


def test_pi_chain_stops_at_its_root_and_stored_depth_stays_within_the_walk_bound(clean, tmp_path):  # noqa: F811
    fixture = _lineage_fixture()
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    fixture.initial(base)
    fixture.parent_arrives(base)
    fixture.root_arrives(base)
    # A headerless child 18 run directories below the root: no parent row can exist for it, so
    # only its directory depth is evidence, and the loader never stores more than its walk bound.
    deep = "/".join(f"30000000-0000-4000-8000-{n:012d}/run-0" for n in range(18))
    deep_uid = "10000000-0000-4000-8000-0000000000ff"
    fixture.write(base, f"{fixture.basename(fixture.DIRECTORY_ROOT)}/{deep}/session.jsonl", deep_uid)

    def refresh():
        stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
        assert stats.errors == 0

    refresh()
    fresh = _pi_lineage_snapshot(clean)
    depths = {row[0]: row[4] for row in fresh[0]}
    assert depths[deep_uid] == 16
    assert depths[fixture.CHILD] == 2
    # An older indexer could give a root a parent. The walk ends at the first root it reaches
    # instead of also treating that row's ancestors as roots of the same child.
    for _ in range(3):
        clean.execute("UPDATE ah.session SET parent_session_id=(SELECT id FROM ah.session WHERE session_uid=%s), "
                      "root_session_uid=NULL, root_session_id=NULL, spawn_depth=NULL "
                      "WHERE agent='pi' AND session_uid=%s", (fixture.DIRECTORY_ROOT, fixture.ACTUAL_ROOT))
        clean.execute("UPDATE ah.session SET root_session_uid=NULL, root_session_id=NULL, spawn_depth=NULL "
                      "WHERE agent='pi' AND session_uid IN (%s,%s,%s,%s)",
                      (fixture.CHILD, fixture.HEADER_PARENT, fixture.NESTED_PARENT, fixture.NESTED_CHILD))
        clean.execute("DELETE FROM ah.dirty_session")
        clean.commit()
        refresh()
        got = _pi_lineage_snapshot(clean)
        assert [r for r in got[0] if r[0] in (fixture.CHILD, fixture.NESTED_CHILD, deep_uid)] == \
            [r for r in fresh[0] if r[0] in (fixture.CHILD, fixture.NESTED_CHILD, deep_uid)]
    clean.rollback()


def test_pi_cyclic_headers_and_ambiguous_directory_parents_do_not_invent_a_family(clean, tmp_path):  # noqa: F811
    spec = importlib.util.spec_from_file_location("pi_lineage_fixture", FIX / "lineage.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    fixture.initial(base)
    # Two headers identify each other, but the path still misleadingly names a real root.
    fixture.write(base, f"{fixture.basename(fixture.DIRECTORY_ROOT)}/{fixture.RUNS[0]}/run-0/session.jsonl",
                  fixture.CHILD, f"/tmp/synthetic/{fixture.HEADER_PARENT}.jsonl")
    fixture.write(base, f"{fixture.basename(fixture.DIRECTORY_ROOT)}/{fixture.RUNS[5]}/session.jsonl",
                  fixture.HEADER_PARENT, f"/tmp/synthetic/{fixture.CHILD}.jsonl")
    # Two real session headers at the same parent agent_path make the nested path fallback
    # ambiguous. No basename, source order or surrogate id may choose between them.
    fixture.write(base, f"{fixture.basename(fixture.DIRECTORY_ROOT)}/{fixture.RUNS[3]}/run-0/other.jsonl",
                  fixture.ACTUAL_ROOT)
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0
    for uid in (fixture.CHILD, fixture.HEADER_PARENT, fixture.NESTED_PARENT):
        assert clean.execute("SELECT root_session_uid, root_session_id, spawn_depth FROM ah.session "
                             "WHERE session_uid=%s", (uid,)).fetchone() == (None, None, None)
    assert clean.execute("SELECT parent_session_id FROM ah.session WHERE session_uid=%s",
                         (fixture.NESTED_CHILD,)).fetchone() == (None,)
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn").fetchone()[0] == 0
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    clean.rollback()


def build(root: Path) -> tuple[Path, Path]:
    hot, cold = root / "hot", root / "cold"
    shutil.copytree(FIX, hot / "pi-local")
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    return hot, cold


def test_pi_tree_loads_links_children_and_tags_the_launch(clean, tmp_path):  # noqa: F811
    hot, cold = build(tmp_path)
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 8
    rows = clean.execute(
        "SELECT c.agent_type, p.session_uid, r.session_uid, c.spawn_depth "
        "FROM ah.session c JOIN ah.session p ON p.id = c.parent_session_id "
        "JOIN ah.session r ON r.id = c.root_session_id "
        "WHERE c.agent = 'pi' AND c.is_subagent ORDER BY c.agent_path").fetchall()
    # The artifact transcripts pair each run id with its child's response id: an exact link that
    # also supplies the child type.
    assert rows == [("mapper", ROOT_UID, ROOT_UID, 1), ("lane-worker", ROOT_UID, ROOT_UID, 1)]
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn WHERE agent='pi' "
                         "AND child_session_id IS NOT NULL").fetchone()[0] == 2
    orphans = clean.execute("SELECT count(*) FROM ah.session WHERE agent = 'pi' AND NOT is_stub "
                            "AND root_session_id IS NULL").fetchone()[0]
    assert orphans == 0
    loop = clean.execute("SELECT loop_number, status FROM ah.loop_run").fetchall()
    assert loop == [(1, "resolved")]   # the loop root's launch prompt
    clean.rollback()


def test_pi_compaction_projects_session_event_without_inventing_after(clean, tmp_path):  # noqa: F811
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    base.mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    at = "2026-10-01T10:00:00Z"
    records = [
        {"type": "session", "id": ROOT_UID, "timestamp": at, "cwd": "/tmp/synthetic", "version": 3},
        {
            "type": "compaction",
            "id": "compact",
            "timestamp": at,
            "tokensBefore": 600000,
            "firstKeptEntryId": "kept",
            "summary": "Synthetic retained compaction summary.",
            # This is the summary-generation call, not the post-compaction context.
            "usage": {"input": 500000, "output": 4000, "cacheRead": 0, "cacheWrite": 0},
        },
    ]
    (base / f"2026-10-01T10-00-00-000Z_{ROOT_UID}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 1
    assert clean.execute("SELECT pre_tokens, post_tokens FROM ah.compaction").fetchall() == [(600000, None)]
    assert clean.execute("SELECT text FROM ah.message WHERE message_class='compaction_summary'").fetchall() == [
        ("Synthetic retained compaction summary.",)
    ]
    assert clean.execute("SELECT compactions, llm_calls FROM ah.session_rollup").fetchall() == [(1, 1)]
    events = clean.execute("SELECT event_uid, detail FROM ah.session_event WHERE kind='compaction'").fetchall()
    assert events == [
        (
            f"{ROOT_UID}:compact",
            {
                "before_tokens": 600000,
                "before_tokens_source": "compaction.tokensBefore",
                "after_tokens": None,
                "after_tokens_source": None,
            },
        )
    ]
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT event_uid, detail FROM ah.session_event WHERE kind='compaction'").fetchall() == events
    assert clean.execute("SELECT input_uncached, output, stop_reason FROM ah.llm_call").fetchall() == [
        (500000, 4000, "compaction")]
    # Replay the same real parser rows through Writer to exercise the event natural-key conflict,
    # not just the unchanged-file fast path above. No source metadata or catalogue reset is needed.
    from agent_history.model import FileContext, LinePos
    from agent_history.parse_pi import PiParser

    source_id, rel_path = clean.execute("SELECT id, rel_path FROM ah.source_file").fetchone()
    ctx = FileContext(str(hot / rel_path), rel_path, "pi-local", "pi", "local", None, "main")
    parser, offset = PiParser(ctx, {}), 0
    writer = load.Writer(clean)
    for number, record in enumerate(records, 1):
        raw = (json.dumps(record) + "\n").encode()
        writer.write(list(parser.line(record, LinePos(offset, len(raw), number))), source_id, ctx)
        offset += len(raw)
    assert clean.execute("SELECT event_uid, detail FROM ah.session_event WHERE kind='compaction'").fetchall() == events
    assert clean.execute("SELECT count(*), min(pre_tokens), min(post_tokens) FROM ah.compaction").fetchone() == (1, 600000, None)
    assert clean.execute("SELECT count(*), min(input_uncached), min(output) FROM ah.llm_call").fetchone() == (1, 500000, 4000)
    clean.rollback()


def test_pi_compaction_sql_keeps_zero_and_unknown_measurements(clean, tmp_path):  # noqa: F811
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    base.mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    at = "2026-10-01T10:00:00Z"
    records = [{"type": "session", "id": ROOT_UID, "timestamp": at, "cwd": "/tmp/synthetic", "version": 3}]
    values = [0, None, -1, True, False, 12.5, 12.0, "12", {"tokens": 12}, [12]]
    for index, value in enumerate(values):
        record = {"type": "compaction", "id": f"compact-{index}", "timestamp": at,
                  "tokensBefore": value, "firstKeptEntryId": "kept"}
        if value is None:
            record.pop("tokensBefore")
        records.append(record)
    records.append({"type": "branch_summary", "id": "branch", "timestamp": at,
                    "summary": "Synthetic branch summary.", "tokensBefore": 600000})
    (base / f"2026-10-01T10-00-00-000Z_{ROOT_UID}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records))
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 1
    events = clean.execute("SELECT event_uid, detail FROM ah.session_event WHERE kind='compaction' "
                           "ORDER BY byte_offset").fetchall()
    assert events == [(f"{ROOT_UID}:compact-{index}", {
        "before_tokens": 0 if index == 0 else None,
        "before_tokens_source": "compaction.tokensBefore" if index == 0 else None,
        "after_tokens": None, "after_tokens_source": None,
    }) for index in range(len(values))]
    # Legacy compaction values stay byte-for-byte compatible with their existing conversion rules.
    assert clean.execute("SELECT pre_tokens FROM ah.compaction ORDER BY byte_offset").fetchall() == [
        (0,), (None,), (-1,), (None,), (None,), (12,), (12,), (12,), (None,), (None,)]
    assert clean.execute("SELECT compactions, llm_calls FROM ah.session_rollup").fetchone() == (10, 0)
    assert clean.execute("SELECT text FROM ah.message WHERE message_class='compaction_summary'").fetchall() == [
        ("Synthetic branch summary.",)]
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT count(*) FROM ah.session_event WHERE kind='compaction'").fetchone()[0] == 10
    clean.rollback()


def test_pi_refresh_is_idempotent(clean, tmp_path):  # noqa: F811
    hot, cold = build(tmp_path)
    load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    counts = [clean.execute(f"SELECT count(*) FROM ah.{t}").fetchone()[0] for t in ("llm_call", "message", "turn")]
    time.sleep(0.01)
    load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert [clean.execute(f"SELECT count(*) FROM ah.{t}").fetchone()[0] for t in ("llm_call", "message", "turn")] == counts
    clean.rollback()


def test_async_pi_child_type_uses_exact_run_and_response_evidence(clean, tmp_path):  # noqa: F811
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    base.mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    root_uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    runs = ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    dirs = ("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "ffffffff-ffff-4fff-8fff-ffffffffffff")
    types = ("mapper", "lane-worker")
    root_base = f"2026-09-28T07-27-09-000Z_{root_uid}"
    at = "2026-09-28T07:27:09Z"

    def write(path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in records))

    root_records = [{"type": "session", "id": root_uid, "timestamp": at, "cwd": "/tmp/synthetic", "version": 3}]
    for index, (run, directory, agent_type) in enumerate(zip(runs, dirs, types)):
        root_records.extend([
            {"type": "message", "id": f"a{index}", "timestamp": at,
             "message": {"role": "assistant", "timestamp": at, "model": "test", "responseId": f"root-{index}",
                         "stopReason": "toolUse", "usage": {"input": 1, "output": 1},
                         "content": [{"type": "toolCall", "id": f"call-{index}", "name": "subagent",
                                      "arguments": {"agent": agent_type, "task": "synthetic brief", "async": True}}]}},
            {"type": "message", "id": f"r{index}", "timestamp": at,
             "message": {"role": "toolResult", "timestamp": at, "toolCallId": f"call-{index}",
                         "toolName": "subagent", "isError": False, "content": [],
                         "details": {"mode": "async", "runId": run, "results": []}}},
            {"type": "custom_message", "id": f"n{index}", "timestamp": at,
             "customType": "subagent-notify",
             "content": (f"run {run} at /{directory}/run-0/session.jsonl"
                         if index == 0 else f"run {run} finished")},
        ])
    write(base / f"{root_base}.jsonl", root_records)
    for index, directory in enumerate(dirs):
        write(base / root_base / directory / "run-0" / "session.jsonl", [
            {"type": "session", "id": f"00000000-0000-4000-8000-00000000000{index}",
             "timestamp": at, "cwd": "/tmp/synthetic", "version": 3},
            {"type": "session_info", "id": f"i{index}", "timestamp": at,
             "name": "WIRE-HARNESS: display label"},
            {"type": "message", "id": f"m{index}", "timestamp": at,
             "message": {"role": "assistant", "timestamp": at, "model": "test",
                         "responseId": f"child-{index}", "stopReason": "stop",
                         "usage": {"input": 1, "output": 1}, "content": []}},
        ])
    for index, (run, agent_type) in enumerate(zip(runs, types)):
        write(base / "subagent-artifacts" / f"{run}_{agent_type}_transcript.jsonl", [
            {"version": 1, "recordType": "message", "runId": run, "agent": agent_type,
             "timestamp": at, "message": {"role": "assistant", "responseId": f"child-{index}"}},
        ])
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 6
    rows = clean.execute("SELECT agent_type, agent_type_source FROM ah.session "
                         "WHERE agent='pi' AND spawn_kind='pi_subagent' ORDER BY agent_type NULLS LAST").fetchall()
    assert rows == [("lane-worker", "explicit"), ("mapper", "explicit"), (None, None)]
    assert clean.execute("SELECT count(*), count(child_session_id) FROM ah.subagent_spawn "
                         "WHERE agent='pi'").fetchone() == (2, 2)
    assert clean.execute("SELECT count(*) FROM ah.pi_run_response").fetchone()[0] == 2
    assert clean.execute("SELECT count(*) FROM ah.llm_call WHERE agent='pi'").fetchone()[0] == 5
    conflicting_run = "99999999-9999-4999-8999-999999999999"
    for agent_type in ("mapper", "reviewer"):
        write(base / "subagent-artifacts" / f"{conflicting_run}_{agent_type}_transcript.jsonl", [
            {"version": 1, "recordType": "message", "runId": conflicting_run, "agent": agent_type,
             "timestamp": at, "message": {"role": "assistant", "responseId": "child-2"}},
        ])
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT count(*) FROM ah.session WHERE agent='pi' AND spawn_kind='pi_subagent' "
                         "AND agent_type IS NULL").fetchone()[0] == 1
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn WHERE agent='pi'").fetchone()[0] == 2
    assert clean.execute("SELECT count(*) FROM ah.dirty_session").fetchone()[0] == 0
    clean.rollback()


def test_pi_loop_keeps_cache_write_and_priced_cost(clean, tmp_path):  # noqa: F811
    # pi reports one cacheWrite (5m price) and often 0; the loop totals must stay known, not NULL
    hot, cold = build(tmp_path)
    added = clean.execute("INSERT INTO ah.model_pricing (model, effective_from, input_per_mtok, cached_input_per_mtok, "
                          "cache_write_per_mtok, output_per_mtok) VALUES ('model-large','2000-01-01',1,1,1,1), "
                          "('model-small','2000-01-01',1,1,1,1) ON CONFLICT DO NOTHING "
                          "RETURNING model, effective_from").fetchall()
    clean.commit()
    try:
        load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
        load.post_passes(clean)
        row = clean.execute("SELECT cache_write, priced_cost_usd, llm_calls FROM ah.loops").fetchone()
        assert row[2] > 0 and row[0] == 0 and row[1] is not None
    finally:
        clean.rollback()
        for model, effective_from in added:  # only the rows this test inserted
            clean.execute("DELETE FROM ah.model_pricing WHERE model = %s AND effective_from = %s",
                          (model, effective_from))
        clean.commit()


def test_notify_lag_counts_steered_and_idle_notifications(clean, tmp_path):  # noqa: F811
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "--x--"
    base.mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}

    def ts(sec):
        return f"2026-10-01T10:00:{sec:02d}.000Z"

    def assistant(entry, sec, stop, content):
        return {"type": "message", "id": entry, "timestamp": ts(sec),
                "message": {"role": "assistant", "model": "m", "responseId": f"resp-{entry}", "stopReason": stop,
                            "usage": usage, "content": content}}

    def notify(entry, sec):
        return {"type": "custom_message", "id": entry, "timestamp": ts(sec), "customType": "subagent-notify",
                "content": "Background task completed: **lane-worker**\n\ndone"}

    records = [
        {"type": "session", "version": 3, "id": "lag1", "timestamp": ts(0), "cwd": "/p"},
        {"type": "message", "id": "e1", "timestamp": ts(0), "message": {"role": "user", "content": "go"}},
        assistant("a2", 1, "toolUse", [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}]),
        notify("n3", 2),   # steered into the running turn: opens no task_notification turn
        {"type": "message", "id": "t4", "timestamp": ts(3),
         "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": False,
                     "content": [{"type": "text", "text": "ok"}]}},
        assistant("a5", 5, "stop", []),
        notify("n6", 10),  # idle: opens its own turn
        assistant("a7", 14, "stop", []),
    ]
    (base / "2026-10-01T10-00-00-000Z_lag1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    rows = clean.execute("SELECT source, steered, lag_s::int FROM ah.v_notify_lag ORDER BY notified_at").fetchall()
    assert rows == [("subagent-notify", True, 3), ("subagent-notify", False, 4)]
    clean.rollback()
