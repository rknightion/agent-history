"""Idle-parent notification wakes are not human turns; genuine user text is retained."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agent_history.memstore import MemStore, run_file
from agent_history.model import FileContext
from agent_history.parse_pi import PiParser
from test_loader_pg import DSN, clean, conn  # noqa: F401 (database fixtures)

FIXTURE = Path(__file__).parent / "fixtures" / "pi" / "parent_wake.py"
spec = importlib.util.spec_from_file_location("parent_wake_fixture", FIXTURE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def parse(tmp_path, records, batch_lines=1):
    path = tmp_path / "root.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    ctx = FileContext(str(path), "pi-test/sessions/slug/root.jsonl", "pi-test", "pi", "test", None, "main")
    store = MemStore()
    run_file(PiParser, ctx, store, batch_lines=batch_lines)
    return store


@pytest.mark.parametrize("batch_lines", [1, 5000])
def test_parent_wake_notification_context_excludes_only_injected_turns(tmp_path, batch_lines):
    store = parse(tmp_path, fixture.records(), batch_lines)
    # This is the exact predicate used by load.ROLLUP_SQL for session_rollup.turns_human.
    human = [r["turn_key"] for r in store.rows("turn") if r.get("origin") == "human"]
    assert human == ["launch", "genuine"]
    messages = {r["event_uid"].split(":", 1)[1]: r for r in store.rows("message")}
    for index in range(2):
        wake = messages[f"wake-{index}"]
        assert wake["message_class"] == "task_notification_summary"
        assert wake["text"] == "Subagent updates above."
        assert wake["detail"]["source"] == "subagent-parent-wake"
        assert wake["turn_key"] == f"notice-{index}"
    turns = {r["turn_key"]: r for r in store.rows("turn")}
    assert set(turns) == {"launch", "notice-0", "notice-1", "genuine"}
    assert all(turns[f"notice-{i}"]["origin"] == "task_notification" for i in range(2))
    session = store.rows("session")[0]
    assert session["last_human_at"] == messages["genuine"]["ts"]


@pytest.mark.parametrize(
    "text,notice",
    [
        ("Subagent updates above.", False),
        ("Please explain 'Subagent updates above.'.", True),
        ("```\nSubagent updates above.\n```", True),
        ("> Subagent updates above.", True),
        ("Subagent updates above.\nAlso check the tests.", True),
    ],
)
def test_literal_or_mixed_genuine_user_text_is_not_a_wake(tmp_path, text, notice):
    records = fixture.records()[:3]
    if notice:
        records.append(
            {
                "type": "custom_message",
                "id": "notice",
                "timestamp": "2026-10-07T00:01:00Z",
                "customType": "subagent-notify",
                "content": "Synthetic update.",
            }
        )
    records.append(
        {
            "type": "message",
            "id": "typed",
            "timestamp": "2026-10-07T00:01:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )
    store = parse(tmp_path, records)
    message = next(r for r in store.rows("message") if r["event_uid"].endswith(":typed"))
    assert message["message_class"] == "human_prompt"
    assert message["text"] == text
    assert [r["turn_key"] for r in store.rows("turn") if r.get("origin") == "human"] == ["launch", "typed"]


@pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="Disposable database DSN not set")
def test_parent_wakes_through_loader_have_correct_session_rollup(clean, tmp_path):  # noqa: F811
    from agent_history import load

    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    base.mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    path = base / "2026-10-07T00-00-00-000Z_synthetic-parent-wake.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in fixture.records()))
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 1
    assert clean.execute("SELECT turns_human, turns_other FROM ah.session_rollup").fetchall() == [(2, 2)]
    assert clean.execute(
        "SELECT text FROM ah.message WHERE message_class = 'human_prompt' ORDER BY byte_offset"
    ).fetchall() == [("Start the synthetic work.",), ("Please check the final result.",)]
    assert (
        clean.execute("SELECT count(*) FROM ah.message WHERE message_class = 'task_notification_summary'").fetchone()[0]
        == 4
    )
    clean.rollback()
