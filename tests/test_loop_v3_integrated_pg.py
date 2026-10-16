"""Real v3 launch, metadata/state collection and independent Git trailer evidence."""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from agent_history import collect_git, collect_loop_metadata, collect_receipts, load
from test_loop_identity_v3_pg import clean, conn, pytestmark  # noqa: F401
from test_loop_links_pg import AT as AT_TEXT, ROOT, assistant, header, message, write
from test_loop_outcomes import AT, encoded, framed

REPO = "github.com/example/catalogue"


def git(repo, *args, at=None):
    env = dict(os.environ)
    if at:
        env.update(GIT_AUTHOR_DATE=at.isoformat(), GIT_COMMITTER_DATE=at.isoformat())
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Example",
            "-c",
            "user.email=export@example.com",
            *args,
        ],
        env=env,
        text=True,
        timeout=30,
    ).strip()


def ingest(db, table, cols, key, rows):
    with db.cursor() as cur:
        collect_git.upsert(cur, table, cols, key, rows)
    db.commit()


def project(db):
    load.post_passes(db)
    db.commit()
    return db.execute("SELECT tasks_admitted,tasks_landed,parks_total FROM ah.loops").fetchone()


def test_real_v3_collected_state_absence_does_not_hide_independent_trailers(clean, tmp_path):  # noqa: F811
    repo = tmp_path / "catalogue"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "https://github.com/example/catalogue.git")
    codex = repo / "codex"
    codex.mkdir()
    charter = codex / "charter-synthetic-loop14.md"
    charter.write_text("Synthetic integration charter.\n")
    os.utime(charter, (AT.timestamp() - 60, AT.timestamp() - 60))
    digest = hashlib.sha256(charter.read_bytes()).hexdigest()
    root_header = {**header(ROOT), "cwd": str(repo)}
    prompt = "You are the root. Read codex/charter-synthetic-loop14.md. Write codex/closeout-synthetic-loop14.md."
    activity = assistant("working", [{"type": "text", "text": "Working."}], stopReason="stop")
    activity["timestamp"] = activity["message"]["timestamp"] = (AT + timedelta(minutes=5)).isoformat()
    write(repo / "sessions/synthetic/root.jsonl", [root_header, message("launch", "user", prompt), activity])
    assert load.refresh(clean, sources={"pi-test": repo}, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT mode FROM ah.loop_run").fetchone() == ("v3",)
    metadata = list(collect_loop_metadata.metadata(repo, "integration"))
    assert len(metadata) == 1 and metadata[0]["kind"] == "charter"
    ingest(clean, "loop_run_metadata", collect_loop_metadata.COLS, collect_loop_metadata.KEY, metadata)
    state_file = codex / "state-synthetic-loop14.jsonl"
    rows = framed(
        {"ev": "dispatch", "lane": "one", "task": "TASK-1001", "agent": "worker", "run": "one", "base": "base"}
    )
    rows[0] = {k: v for k, v in rows[0].items() if k not in ("goal_sha256", "envelope", "tier")}
    rows[0].update(charter_sha256=digest, first_wave=["TASK-1001"])

    def state(events, machine="integration"):
        state_file.write_text(encoded(events))
        captured = list(collect_receipts.states(repo, machine))
        assert len(captured) == 1
        ingest(clean, "loop_state", collect_receipts.STATE_COLS, collect_receipts.STATE_KEY, captured)

    try:
        state(rows)
        assert project(clean) == (None, None, None)
        assert clean.execute("SELECT repo,loop,goal_sha256 FROM ah.loops").fetchone() == (
            "example/catalogue",
            "loop14",
            digest,
        )
        (repo / "change.py").write_text("VALUE = 1\n")
        git(repo, "add", "--", "change.py")
        git(repo, "commit", "-q", "-m", "Synthetic change", "-m", "Task: TASK-1001", at=AT + timedelta(seconds=3))
        days = max(1, (datetime.now(timezone.utc) - AT).days + 1)
        commits = collect_git.git_commits(
            {"path": repo, "ref": "main", "slug": REPO, "emails": {"export@example.com"}}, days
        )
        assert len(commits) == 1 and commits[0]["task_ids"] == ["TASK-1001"]
        ingest(clean, "git_commit", collect_git.GIT_COLS, ["repo_slug", "sha"], commits)
        assert project(clean) == (None, 1, None)
        # Explicit authoritative v1 zero must not become a trailer-derived one.
        legacy = framed()
        legacy[0]["goal_sha256"] = digest
        state(legacy)
        assert project(clean) == (0, 0, 0)
        # Explicit root lands win, while contradictory copies remain unknown.
        explicit = json.loads(json.dumps(rows))
        for n, task in enumerate(("TASK-2001", "TASK-2002"), 3):
            explicit.append(
                {
                    "v": 1,
                    "seq": n,
                    "ts": AT_TEXT.replace("00Z", f"{n:02d}Z"),
                    "by": "root",
                    "ev": "land",
                    "task": task,
                    "sha": "b" * 40,
                    "gate": "green",
                    "mode": "after-green",
                }
            )
        state(explicit)
        assert project(clean) == (None, 2, None)
        contradictory = json.loads(json.dumps(explicit))
        contradictory[-1]["task"] = "TASK-2003"
        state(contradictory, "integration-conflict")
        assert project(clean) == (None, None, None)
    finally:
        clean.execute("DELETE FROM ah.loop_state WHERE machine IN ('integration','integration-conflict')")
        clean.execute("DELETE FROM ah.git_commit WHERE repo_slug=%s", (REPO,))
        clean.commit()
