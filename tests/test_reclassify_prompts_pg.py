"""Historical prompt reclassification on disposable real PostgreSQL.

Rows indexed before the legacy peer-envelope classifier keep ``human_prompt``: an incremental
index never revisits indexed bytes. ``load.reclassify_prompts`` reparses the affected sessions'
unchanged sources with the current classifier in one transaction. All transcripts are synthetic.
"""

import json
import os

import psycopg
import pytest

from agent_history import load, parse_claude
from test_common import legacy_peer_envelope
from test_loader_pg import DSN, build_tree, clean, conn  # noqa: F401

READER_DSN = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN or not READER_DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) and AGENT_HISTORY_TEST_READER_DSN not set",
)

SID = "33333333-3333-4333-8333-333333333333"
KEY = ("claude", SID, "")
GENUINE = "Please explain permission laundering. I typed Another Claude session sent a message: as words."
REPLY = "Synthetic reply."


def quiet(*_args):
    return None


def record(uuid, ts, content, role="user", **extra):
    base = {
        "type": role,
        "sessionId": SID,
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": "/home/tester/peer",
        "message": {"role": role, "content": content},
    }
    if role == "assistant":
        base["message"] = {
            "role": "assistant",
            "id": f"msg-{uuid}",
            "model": "synthetic-model",
            "content": [{"type": "text", "text": content}],
        }
    return {**base, **extra}


FOLLOW_UP = "\n\nHuman follow-up: keep my request."


def write_peer_session(hot, mixed):
    path = hot / "claude-local" / "projects" / "-home-tester-peer" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    envelope = legacy_peer_envelope() + (FOLLOW_UP if mixed else "")
    # The peer envelope arrives first, as historical user records did: no origin or promptSource.
    lines = [
        record("peer-1", "2026-09-21T10:00:00.000Z", [{"type": "text", "text": envelope}]),
        record("reply-1", "2026-09-21T10:00:05.000Z", REPLY, role="assistant"),
        record("human-1", "2026-09-21T10:05:00.000Z", GENUINE),
        record("reply-2", "2026-09-21T10:05:05.000Z", REPLY, role="assistant"),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def classes(db):
    rows = db.execute(
        "SELECT m.event_uid, m.message_class, m.text FROM ah.message m JOIN ah.session s ON s.id = m.session_id "
        "WHERE s.agent = %s AND s.session_uid = %s AND s.agent_id = %s AND m.role = 'user' ORDER BY m.event_uid",
        KEY,
    ).fetchall()
    db.commit()
    return {uid: (cls, text) for uid, cls, text in rows}


def laundering_prompts(db):
    """What a prompt search over the conversation classes matches for the harness phrase."""
    rows = db.execute(
        "SELECT m.event_uid FROM ah.message m WHERE m.message_class IN ('human_prompt', 'queued_prompt') "
        "AND m.message_class = ANY (ah.conversation_classes()) AND m.text ILIKE '%permission laundering%' "
        "ORDER BY 1"
    ).fetchall()
    db.commit()
    return [row[0] for row in rows]


def state(db):
    rows = db.execute("SELECT agent, session_uid, agent_id, content_fingerprint FROM ah.v_session_state").fetchall()
    db.commit()
    return {(a, s, i): fp for a, s, i, fp in rows}


@pytest.fixture(params=[False, True], ids=["envelope", "mixed"])
def historical(clean, tmp_path, monkeypatch, request):  # noqa: F811
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()
    hot, cold = build_tree(tmp_path)
    path = write_peer_session(hot, request.param)
    # Index with the classifier that predates legacy peer envelopes, as the historical rows were.
    with monkeypatch.context() as patch:
        patch.setattr(parse_claude, "split_legacy_peer_injections", lambda *_args, **_kwargs: None)
        assert load.refresh(clean, hot, cold, textfile=None, log=quiet).errors == 0
    yield clean, hot, cold, path, request.param
    clean.rollback()
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()


def test_historical_peer_envelope_is_reclassified_and_genuine_prompt_is_not(historical, monkeypatch):
    db, hot, cold, _, mixed = historical
    before = classes(db)
    assert before["peer-1"][0] == "human_prompt", "the synthetic historical row must start misclassified"
    assert before["human-1"] == ("human_prompt", GENUINE)
    assert laundering_prompts(db) == ["human-1", "peer-1"]
    # An ordinary incremental pass leaves the historical row untouched.
    assert load.refresh(db, hot, cold, textfile=None, log=quiet).errors == 0
    assert classes(db) == before
    old_state = state(db)
    last_change = db.execute("SELECT COALESCE(max(seq), 0) FROM ah.change_log").fetchone()[0]
    db.commit()

    # A concurrent reader sees the old complete rows while the reparse transaction is open.
    original_post = load.post_passes
    seen = []

    def post_passes(connection, *args, **kwargs):
        with psycopg.connect(READER_DSN, autocommit=True) as reader:
            seen.append(classes(reader))
            seen.append(state(reader))
        return original_post(connection, *args, **kwargs)

    monkeypatch.setattr(load, "post_passes", post_passes)
    report = load.reclassify_prompts(db, hot, cold, log=quiet)
    assert seen == [before, old_state]
    assert report["sessions"] == 1 and report["refused_sources"] == 0
    assert (report["prompt_rows_before"], report["prompt_rows_after"]) == (2, 2 if mixed else 1)

    after = classes(db)
    if mixed:
        # Lossless split: the envelope becomes its own row; the human remainder keeps the event key.
        assert after["peer-1:injection:0"] == ("agent_message", legacy_peer_envelope())
        assert after["peer-1"][0] == "human_prompt" and after["peer-1"][1].strip() == FOLLOW_UP.strip()
    else:
        assert after["peer-1"] == ("agent_message", legacy_peer_envelope())
    assert after["human-1"] == ("human_prompt", GENUINE)
    assert laundering_prompts(db) == ["human-1"]
    # The mixed record's remainder is genuine human text at the envelope's time.
    first_human = db.execute(
        "SELECT first_human_at = %s::timestamptz FROM ah.session WHERE agent = %s AND session_uid = %s AND agent_id = %s",
        ("2026-09-21T10:00:00Z" if mixed else "2026-09-21T10:05:00Z", *KEY),
    ).fetchone()[0]
    db.commit()
    assert first_human, "human-activity bounds are recomputed, not merged with the envelope's time"

    # Consumers see one content change for the reclassified session only; nothing goes blank.
    # The fingerprint is content identity (event keys and text hashes): a whole-record class change
    # keeps it, so approvals are not invalidated; a lossless split adds a row key and changes it.
    new_state = state(db)
    assert all(new_state.values()) and set(new_state) == set(old_state)
    assert (new_state[KEY] != old_state[KEY]) == mixed
    assert {k: v for k, v in new_state.items() if k != KEY} == {k: v for k, v in old_state.items() if k != KEY}
    changed = db.execute(
        "SELECT DISTINCT agent, session_uid, agent_id FROM ah.change_log WHERE seq > %s AND kind = 'content'",
        (last_change,),
    ).fetchall()
    db.commit()
    assert changed == [KEY]

    # Idempotent: nothing is left for the classifier to split, and an index pass stays a no-op.
    monkeypatch.setattr(load, "post_passes", original_post)
    assert load.reclassify_prompts(db, hot, cold, log=quiet)["sessions"] == 0
    assert load.refresh(db, hot, cold, textfile=None, log=quiet).errors == 0
    assert classes(db) == after


def test_dry_run_and_refusals_write_nothing(historical):
    db, hot, cold, path, mixed = historical
    before = classes(db)
    report = load.reclassify_prompts(db, hot, cold, dry_run=True, log=quiet)
    assert (report["sessions"], report["prompt_rows_before"]) == (1, 2)
    assert classes(db) == before

    db.execute("INSERT INTO ah.meta VALUES ('rebuild_in_progress', '2000-01-01')")
    db.commit()
    with pytest.raises(SystemExit, match="rebuild in progress"):
        load.reclassify_prompts(db, hot, cold, log=quiet)
    db.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    db.commit()

    with load.connect(DSN) as other:
        assert load.try_lock(other)
        try:
            with pytest.raises(SystemExit, match="holds the lock"):
                load.reclassify_prompts(db, hot, cold, log=quiet)
        finally:
            load.unlock(other)

    # A source that no longer matches its indexed prefix is refused before any write.
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Synthetic reply.", b"Synthetic rep1y.", 1))
    try:
        with pytest.raises(SystemExit, match="changed since indexing"):
            load.reclassify_prompts(db, hot, cold, log=quiet)
    finally:
        path.write_bytes(original)
    assert classes(db) == before

    # A source indexed by another parser version needs a rebuild, not an in-place reparse.
    db.execute("UPDATE ah.source_file SET parser_version = 'older' WHERE rel_path LIKE %s", ("%" + SID + "%",))
    db.commit()
    with pytest.raises(SystemExit, match="changed since indexing"):
        load.reclassify_prompts(db, hot, cold, log=quiet)
    assert classes(db) == before
    current = load.parser_for("claude", "main")[1]
    db.execute("UPDATE ah.source_file SET parser_version = %s WHERE rel_path LIKE %s", (current, "%" + SID + "%"))
    db.commit()
    assert load.reclassify_prompts(db, hot, cold, log=quiet)["prompt_rows_after"] == (2 if mixed else 1)
