"""Rebuild must retain journal enrichment and reconnect it to new session IDs."""

import pytest
from agent_history import load

from test_loader_pg import DSN, build_tree, clean, conn  # noqa: F401

pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set",
)


def test_rebuild_keeps_summary_and_topic(clean, tmp_path, monkeypatch):  # noqa: F811
    clean.execute("DELETE FROM ah.session_topic WHERE journal_conversation_id = 'test-conversation'")
    clean.execute("DELETE FROM ah.session_summary WHERE journal_conversation_id = 'test-conversation'")
    clean.commit()
    hot, cold = build_tree(tmp_path)
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    key = clean.execute("SELECT agent, session_uid, agent_id, id FROM ah.session ORDER BY id LIMIT 1").fetchone()
    assert key is not None
    clean.execute(
        "INSERT INTO ah.session_summary (journal_conversation_id, session_id, journal_revision_id, "
        "namespace, title, analysed_at) VALUES ('test-conversation', %s, 'rev', 'test', 'keep', now())",
        (key[3],),
    )
    clean.execute(
        "INSERT INTO ah.session_topic (journal_conversation_id, topic, session_id) "
        "VALUES ('test-conversation', 'topic', %s)",
        (key[3],),
    )
    clean.commit()
    monkeypatch.setattr(load, "cold_tier_ok", lambda _: True)
    assert load.rebuild(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    new_id = clean.execute(
        "SELECT id FROM ah.session WHERE (agent,session_uid,agent_id) = (%s,%s,%s)",
        key[:3],
    ).fetchone()[0]
    assert clean.execute(
        "SELECT title, session_id FROM ah.session_summary WHERE journal_conversation_id = 'test-conversation'"
    ).fetchone() == ("keep", new_id)
    assert clean.execute(
        "SELECT topic, session_id FROM ah.session_topic WHERE journal_conversation_id = 'test-conversation'"
    ).fetchone() == ("topic", new_id)
