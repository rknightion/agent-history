"""Analytics SQL (sql/analytics.sql) tests.

The database tests run against a disposable Postgres (ParadeDB) database and are skipped unless
AGENT_HISTORY_TEST_DSN names a *_test database. They load the transcript fixtures with the real
loader, then seed the collector/journal tables (git_commit, ci_run, backlog_task, installed_feature,
permission_log, session_summary, ...) directly. Run: just ci.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
needs_db = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                              reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

COLLECTOR_TABLES = ("task_ref", "task_prefix", "backlog_task", "git_commit", "ci_run", "installed_feature",
                    "permission_log", "session_topic", "session_summary", "git_commit_file")
TEST_MODELS = ("test-model-1h", "test-model-5m")
CLAUDE_UID = "11111111-1111-4111-8111-111111111111"
COMMIT_SHA = "abc1234def56" + "0" * 28


# --- database fixtures ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    if not DSN or "agent_history_test" not in DSN:
        pytest.skip("AGENT_HISTORY_TEST_DSN (a *_test database) not set")
    pytest.importorskip("psycopg")
    from agent_history import load
    sys.path.insert(0, str(Path(__file__).parent))
    from test_loader_pg import build_tree

    conn = load.connect(DSN)
    load.apply_schema(conn, force=True)
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES + COLLECTOR_TABLES)
                 + " RESTART IDENTITY CASCADE")
    conn.commit()
    hot, cold = build_tree(tmp_path_factory.mktemp("analytics"))
    stats = load.refresh(conn, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0
    seed(conn)
    conn.commit()
    load.create_post_load_indexes(conn)   # message + session_summary ParadeDB indexes
    yield conn
    conn.rollback()
    conn.execute("DELETE FROM ah.model_pricing WHERE model = ANY(%s)", (list(TEST_MODELS),))
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in COLLECTOR_TABLES) + " CASCADE")
    conn.commit()
    conn.close()


def sid(conn, agent: str, uid: str, agent_id: str = "") -> int:
    return conn.execute("SELECT id FROM ah.session WHERE agent=%s AND session_uid=%s AND agent_id=%s",
                        (agent, uid, agent_id)).fetchone()[0]


def seed(conn) -> None:
    main = sid(conn, "claude", CLAUDE_UID)
    other = sid(conn, "claude", "22222222-2222-4222-8222-222222222222")
    codex = sid(conn, "codex", "11111111-1111-4111-8111-111111111111")
    commit_ts = conn.execute("SELECT ts FROM ah.git_event WHERE sha_short = 'abc1234def56'").fetchone()[0]
    x = conn.execute
    # ground truth: the agent commit, a same-prefix decoy in another repo a day later, a revert, CI runs
    x("INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, subject, insertions, deletions, "
      "files_changed, on_default) VALUES ('github.com/example-org/widget', %s, 'local', %s, 'fix widget', 10, 2, 1, true), "
      "('github.com/example-org/decoy', %s, 'local', %s + interval '1 day', 'decoy', 1, 1, 1, true)",
      (COMMIT_SHA, commit_ts, "abc1234" + "f" * 33, commit_ts))
    # a same-prefix commit days away from the 'fedcba9' event in a session with no remote: not its commit
    x("INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, subject) "
      "SELECT 'github.com/example-org/decoy', 'fedcba9' || repeat('0', 33), 'local', ts + interval '3 days', 'decoy' "
      "FROM ah.git_event WHERE sha_short = 'fedcba9'")
    x("INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, subject, reverts_sha) "
      "VALUES ('github.com/example-org/widget', %s, 'local', %s + interval '2 hours', 'Revert fix widget', %s)",
      ("9" * 40, commit_ts, COMMIT_SHA))
    x("INSERT INTO ah.ci_run (run_id, repo_slug, context, head_sha, workflow, status, conclusion, created_at) VALUES "
      "(1, 'github.com/example-org/widget', 'local', %(s)s, 'ci', 'completed', 'failure', %(t)s),"
      "(2, 'github.com/example-org/widget', 'local', %(s)s, 'ci', 'completed', 'success', %(t)s + interval '5 min'),"
      "(3, 'github.com/example-org/widget', 'local', %(s)s, 'lint', 'completed', 'failure', %(t)s)",
      {"s": COMMIT_SHA, "t": commit_ts})
    # backlog: the main session names ABC-0019 in a prompt, the other only mentions it
    x("INSERT INTO ah.task_prefix (prefix, repo_slug, context, zero_pad) VALUES ('ABC', 'git.example.net/team/chat', 'local', 4)")
    x("INSERT INTO ah.backlog_task (task_key, repo_slug, context, title, status) "
      "VALUES ('ABC-0019', 'git.example.net/team/chat', 'local', 'Widget task', 'In Progress')")
    x("INSERT INTO ah.task_ref (session_id, task_key, first_ts, last_ts, mentions, in_human, in_brief) VALUES "
      "(%s, 'ABC-0019', now(), now(), 2, true, false), (%s, 'ABC-0019', now(), now(), 1, false, false)", (main, other))
    # installed features (Claude home) and one Codex skill
    x("INSERT INTO ah.installed_feature (machine, home, namespace, kind, name, first_seen_at) VALUES "
      "('laptop', '~/.claude', 'claude-local', 'skill', 'writing', now() - interval '90 days'),"
      "('laptop', '~/.claude', 'claude-local', 'skill', 'wplugin:writing', now() - interval '90 days'),"
      "('laptop', '~/.claude', 'claude-local', 'skill', 'never-used', now() - interval '90 days'),"
      "('laptop', '~/.claude', 'claude-local', 'skill', 'just-installed', now() - interval '2 days'),"
      "('laptop', '~/.claude', 'claude-local', 'mcp_server', 'srv_a', now() - interval '90 days'),"
      "('laptop', '~/.claude', 'claude-local', 'mcp_server', 'claude.ai Slack', now() - interval '90 days'),"
      "('laptop', '~/.codex', 'codex-local', 'skill', 'codex-skill', now() - interval '90 days')")
    # a user rejection on the failing Bash call, and a permission-log prompt
    x("UPDATE ah.tool_call SET meta = '{\"cmd_verb\": \"git\"}' WHERE call_uid = 'toolu_b2'")
    x("INSERT INTO ah.session_event (agent, event_uid, session_id, ts, kind, value, detail, source_id, byte_offset) "
      "VALUES ('claude', 'toolu_b2:denial', %s, %s, 'denial', 'user-rejected', '{\"tool\": \"Bash\"}', 0, 0)",
      (main, commit_ts))
    x("INSERT INTO ah.permission_log (machine, home, namespace, ts, line_hash, tool_name, cmd_verb, reason) "
      "VALUES ('laptop', '~/.claude', 'claude-local', now(), 'h1', 'Bash', 'rm', 'auto-mode classifier')")
    # rate limits: a Codex window burning 5 %/h, and a Claude window with no percentage
    x("INSERT INTO ah.rate_limit_sample (agent, event_uid, session_id, ts, window_kind, limit_id, plan_type, "
      "used_percent, window_minutes, resets_at, source_id, byte_offset) VALUES "
      "('codex', 'rl1', %(c)s, now() - interval '5 hours', 'primary', 'codex', 'pro', 40, 10080, now() + interval '2 days', 0, 0),"
      "('codex', 'rl2', %(c)s, now() - interval '1 hour', 'primary', 'codex', 'pro', 60, 10080, now() + interval '2 days', 0, 0),"
      "('claude', 'rl3', %(m)s, now() - interval '1 hour', 'claude_five_hour', 'five_hour', NULL, NULL, NULL, now() + interval '3 hours', 0, 0)",
      {"c": codex, "m": main})
    # journal summaries: one mapped to the main Claude session, one unmapped
    x("INSERT INTO ah.session_summary (journal_conversation_id, session_id, journal_revision_id, namespace, title, "
      "objective, narrative, analysed_at) VALUES "
      "('j1', %s, 'r1', 'claude-local', 'Repairing the gizmo pipeline', 'Get the gizmo pipeline green', "
      "'We traced the gizmo failure to a stale cache and rebuilt it.', now()),"
      "('j2', NULL, 'r1', 'claude-local', 'Unrelated housekeeping', 'Tidy folders', 'Moved files.', now())", (main,))
    # prices for a 1h-cache model (own 1h price) and a 5m-only model (1h falls back to the 5m price)
    x("INSERT INTO ah.model_pricing (model, effective_from, input_per_mtok, cached_input_per_mtok, cache_write_per_mtok, "
      "cache_write_1h_per_mtok, output_per_mtok) VALUES ('test-model-1h', '2000-01-01', 1, 0.1, 2, 4, 10), "
      "('test-model-5m', '2000-01-01', 1, 0.1, 2, NULL, 10) ON CONFLICT DO NOTHING")
    x("INSERT INTO ah.llm_call (agent, response_id, session_id, ts, model, input_uncached, cache_read, cache_write_5m, "
      "cache_write_1h, output, source_id, byte_offset) VALUES "
      "('claude', 'test-r1', %(s)s, now(), 'test-model-1h', 1000000, 1000000, 1000000, 1000000, 1000000, 0, 0),"
      "('claude', 'test-r2', %(s)s, now(), 'test-model-5m', 0, 0, 0, 1000000, 0, 0, 0)", {"s": other})

    # --- phase 3 fixtures (analytics.sql additions) -----------------------------------------------

    # ah.policy_changes: a policy-path file in the same commit already seeded above.
    x("INSERT INTO ah.git_commit_file (repo_slug, sha, path, change, insertions, deletions) VALUES "
      "('github.com/example-org/widget', %s, 'AGENTS.md', 'M', 4, 1)", (COMMIT_SHA,))

    # ah.rule_effect: a dedicated repo/path with one change, and a synthetic session with controlled
    # before/after activity (4 human turns / 2 interrupts / 1 non-rejection denial / 1 tool error
    # before; 2 human turns / nothing after).
    rule_sha = "r" * 40
    rule_ts = conn.execute("SELECT now() - interval '10 days'").fetchone()[0]
    x("INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, subject) VALUES "
      "('github.com/example-org/ruletest', %s, 'local', %s, 'tighten the rule')", (rule_sha, rule_ts))
    x("INSERT INTO ah.git_commit_file (repo_slug, sha, path, change, insertions, deletions) VALUES "
      "('github.com/example-org/ruletest', %s, 'rules/foo.md', 'M', 3, 1)", (rule_sha,))
    # Namespace 'claude-other' keeps this session (and its high signal count) out of the other pinned
    # tests' 'claude-local' scope (ah.correction_digest, ah.week) -- rule_effect itself is called
    # with namespaces=['claude-other'] in its test, isolating the before/after counts to just this data.
    rule_session = conn.execute(
        "INSERT INTO ah.session (agent, session_uid, agent_id, is_stub, namespace, first_event_at, last_event_at) "
        "VALUES ('claude', '44444444-4444-4444-8444-444444444444', '', false, 'claude-other', %(t)s, %(t)s) "
        "RETURNING id", {"t": rule_ts}).fetchone()[0]
    x("INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
      "content_sha256, byte_length, source_id, byte_offset) "
      "SELECT 'claude', 'rule-b' || n, %(s)s, 'claude-other', 'p', %(t)s - (n || ' days')::interval, "
      "'user', 'human_prompt', 'before turn ' || n, 'x', 1, 0, 0 FROM unnest(ARRAY[1, 3, 4, 5]) n",
      {"s": rule_session, "t": rule_ts})
    x("INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
      "content_sha256, byte_length, source_id, byte_offset) "
      "SELECT 'claude', 'rule-a' || n, %(s)s, 'claude-other', 'p', %(t)s + (n || ' days')::interval, "
      "'user', 'human_prompt', 'after turn ' || n, 'x', 1, 0, 0 FROM unnest(ARRAY[1, 3]) n",
      {"s": rule_session, "t": rule_ts})
    x("INSERT INTO ah.session_event (agent, event_uid, session_id, ts, kind, value, source_id, byte_offset) VALUES "
      "('claude', 'rule-int-1', %(s)s, %(t)s - interval '2 days', 'interrupt', NULL, 0, 0),"
      "('claude', 'rule-int-2', %(s)s, %(t)s - interval '1 days', 'interrupt', NULL, 0, 0),"
      "('claude', 'rule-den-1', %(s)s, %(t)s - interval '2 days', 'denial', 'automode-blocked', 0, 0)",
      {"s": rule_session, "t": rule_ts})
    x("INSERT INTO ah.tool_call (agent, call_uid, session_id, tool_name, started_at, outcome, source_id, byte_offset) "
      "VALUES ('claude', 'rule-err-1', %(s)s, 'Bash', %(t)s - interval '1 days', 'error', 0, 0)",
      {"s": rule_session, "t": rule_ts})

    # ah.permission_candidates: a same-tool/same-verb OK retry shortly after the existing user-rejected
    # Bash+git denial (toolu_b2), the 'retried_ok' friction signal.
    x("INSERT INTO ah.tool_call (agent, call_uid, session_id, tool_name, started_at, outcome, meta, source_id, byte_offset) "
      "SELECT 'claude', 'retry_git_ok', c.session_id, 'Bash', c.ended_at + interval '2 minutes', 'ok', "
      "'{\"cmd_verb\": \"git\"}'::jsonb, 0, 0 FROM ah.tool_call c WHERE c.call_uid = 'toolu_b2'")

    # ah.v_infra_action / ah.infra_actions: an SSH-shaped action on the same Bash call (meta merge
    # keeps the existing cmd_verb); a domain-suffixed host to check normalisation; and an unexpanded
    # shell-variable host that must be dropped entirely.
    x("UPDATE ah.tool_call SET meta = meta || '{\"target_host\": \"buildhost\", \"remote_verb\": \"docker\"}'::jsonb "
      "WHERE call_uid = 'toolu_b2'")
    x("INSERT INTO ah.tool_call (agent, call_uid, session_id, tool_name, started_at, outcome, meta, "
      "source_id, byte_offset) VALUES "
      "('claude', 'infra_fqdn', %(s)s, 'Bash', now(), 'ok', "
      "'{\"target_host\": \"build01.example.net\", \"remote_verb\": \"uptime\"}'::jsonb, 0, 0),"
      "('claude', 'infra_unexpanded', %(s)s, 'Bash', now(), 'ok', "
      "'{\"target_host\": \"$h\", \"remote_verb\": \"uptime\"}'::jsonb, 0, 0)", {"s": main})

    # ah.v_spawn_outcome / ah.routing_report: a second 'Explore' spawn from the same parent within 2h
    # of the first (spawn_uid 'toolu_a1'), making that first spawn's `redo` true.
    x("INSERT INTO ah.subagent_spawn (agent, spawn_uid, parent_session_id, spawned_at, description, "
      "source_id, byte_offset) SELECT 'claude', 'toolu_a1_redo', sp.parent_session_id, "
      "sp.spawned_at + interval '30 minutes', 'Explore', 0, 0 FROM ah.subagent_spawn sp "
      "WHERE sp.spawn_uid = 'toolu_a1'")

    # ah.open_threads (a): unfinished journal items for the LATER of the two Codex root sessions (its
    # project has no later root session). Kept off the Claude/'claude-local' sessions used by
    # ah.week's/ah.find_sessions' own pinned assertions, so this does not shift their counts.
    x("INSERT INTO ah.session_summary (journal_conversation_id, session_id, journal_revision_id, namespace, "
      "title, objective, narrative, unfinished, analysed_at) VALUES "
      "('j3', %s, 'r1', 'codex-local', 'Codex follow-up', 'Wrap up the synthetic repo work', "
      "'Left two things dangling.', '[\"Finish the rollout\", \"Verify the cache fix\"]'::jsonb, now())",
      (codex,))
    # ah.open_threads (c): a backlog task with zero referencing sessions ever -- unconditionally stale.
    x("INSERT INTO ah.backlog_task (task_key, repo_slug, context, title, status) VALUES "
      "('ABC-0099', 'git.example.net/team/chat', 'local', 'Stale task', 'In Progress')")

    # ah.lesson_candidates: a belief-correction phrase.
    x("INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
      "content_sha256, byte_length, source_id, byte_offset) VALUES "
      "('claude', 'lesson-1', %s, 'claude-local', 'p', now(), 'user', 'human_prompt', "
      "'It turns out the root cause was a stale cache, not what we thought at first.', 'lx1', 1, 0, 0)", (main,))



def rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [d.name for d in cur.description]
    out = [dict(zip(names, r)) for r in cur.fetchall()]
    conn.rollback()
    return out


LONG_AGO = "now() - interval '100 years'"


# --- database tests ------------------------------------------------------------------------------


@needs_db
def test_every_new_object_executes(db):
    for sql in (f"SELECT * FROM ah.hook_latency({LONG_AGO})", f"SELECT * FROM ah.denials({LONG_AGO})",
                "SELECT * FROM ah.unused_features(36500)", "SELECT * FROM ah.why('abc1234')",
                "SELECT * FROM ah.find_sessions('widget', ARRAY['claude-local'])",
                f"SELECT * FROM ah.correction_digest({LONG_AGO})", "SELECT * FROM ah.task_effort('ABC-19')",
                "SELECT * FROM ah.v_task_effort", "SELECT * FROM ah.v_agent_commit_quality",
                "SELECT * FROM ah.v_agent_commit_quality_weekly", "SELECT * FROM ah.v_rate_limit_forecast",
                "SELECT * FROM ah.search_summaries('gizmo')", f"SELECT * FROM ah.week({LONG_AGO})",
                "SELECT * FROM ah.v_daily_usage", "SELECT * FROM ah.v_session_cost",
                "SELECT * FROM ah.v_loop_summary", "SELECT * FROM ah.recent_loops()",
                f"SELECT * FROM ah.policy_changes(NULL, {LONG_AGO})",
                "SELECT * FROM ah.rule_effect('no/such', 'no/such.md')",
                f"SELECT * FROM ah.permission_candidates({LONG_AGO})",
                "SELECT * FROM ah.v_spawn_outcome", f"SELECT * FROM ah.routing_report({LONG_AGO})",
                "SELECT * FROM ah.v_infra_action", "SELECT * FROM ah.infra_actions('buildhost', now())",
                "SELECT * FROM ah.active_sessions(36500000)", f"SELECT * FROM ah.open_threads({LONG_AGO})",
                f"SELECT * FROM ah.lesson_candidates({LONG_AGO})"):
        rows(db, sql)
    wrapped = rows(db, f"SELECT ah.wrapped({LONG_AGO}, now() + interval '1 day') AS w")
    assert wrapped and wrapped[0]["w"] is not None
    hooks = rows(db, f"SELECT * FROM ah.hook_latency({LONG_AGO})")
    assert hooks and all(h["data_scope"] == "claude only" for h in hooks)


@needs_db
def test_session_cost_view_marks_claude_snapshot_calls(db):
    got = rows(db, "SELECT s.agent, v.snapshot_calls AS view, "
                   "(SELECT count(*) FROM ah.llm_call c WHERE c.session_id = s.id AND c.stop_reason IS NULL "
                   "AND NOT c.is_api_error) AS expected "
                   "FROM ah.session s JOIN ah.v_session_cost v ON v.session_id = s.id")
    claude = [g for g in got if g["agent"] == "claude"]
    codex = [g for g in got if g["agent"] == "codex"]
    assert claude and codex
    assert all(g["view"] == g["expected"] for g in claude), claude
    assert any(g["view"] > 0 for g in claude), claude
    # Codex never records a stop reason, so the flag would be meaningless there
    assert all(g["view"] is None for g in codex), codex


def test_priced_cost_splits_cache_write_windows(db):
    other = sid(db, "claude", "22222222-2222-4222-8222-222222222222")
    # test-model-1h: 1 in + 0.1 read + 2 (5m) + 4 (1h) + 10 out; test-model-5m: 1h falls back to 2
    got = rows(db, "SELECT model, sum(priced_cost_usd) AS usd FROM ah.v_daily_usage WHERE model = ANY(%s) "
                   "GROUP BY model ORDER BY model", (list(TEST_MODELS),))
    assert [(r["model"], float(r["usd"])) for r in got] == [("test-model-1h", 17.1), ("test-model-5m", 2.0)]
    session = rows(db, "SELECT priced_cost_usd, unpriced_calls FROM ah.v_session_cost WHERE session_id = %s", (other,))
    assert float(session[0]["priced_cost_usd"]) >= 19.1
    assert float(rows(db, "SELECT ah.session_cost(%s) AS c", (other,))[0]["c"]) == float(session[0]["priced_cost_usd"])


@needs_db
def test_why_links_commit_to_prompt_ci_and_ground_truth(db):
    got = rows(db, "SELECT * FROM ah.why('ABC1234D')")
    kinds = [r["kind"] for r in got]
    assert kinds.count("agent_commit") == 1
    prompt = next(r for r in got if r["kind"] == "prompt")
    assert prompt["detail"] == "Please fix the synthetic widget"          # last human prompt before the commit
    commits = {r["sha"]: r for r in got if r["kind"] == "git_commit"}
    assert "near_event" in commits[COMMIT_SHA]["detail"] and "REVERTED" in commits[COMMIT_SHA]["detail"]
    ci = sorted((r["title"], r["detail"].split()[1]) for r in got if r["kind"] == "ci")
    assert ci == [("ci", "success"), ("lint", "failure")]                  # latest run per workflow
    with pytest.raises(Exception, match="7-40 hex"):
        db.execute("SELECT * FROM ah.why('abc12')")
    db.rollback()


@needs_db
def test_commit_quality_resolves_the_near_commit_not_the_decoy(db):
    got = rows(db, "SELECT * FROM ah.v_agent_commit_quality WHERE sha_short = 'abc1234def56'")
    assert len(got) == 1
    q = got[0]
    assert q["sha"] == COMMIT_SHA and q["repo_slug"] == "github.com/example-org/widget"
    assert q["ci_conclusion"] == "failure" and q["ci_runs"] == 2 and q["reverted"] and q["lines_changed"] == 12
    unrelated = rows(db, "SELECT resolved, sha FROM ah.v_agent_commit_quality WHERE sha_short = 'fedcba9'")
    assert unrelated == [{"resolved": False, "sha": None}]
    weekly = rows(db, "SELECT * FROM ah.v_agent_commit_quality_weekly WHERE repo_slug = 'github.com/example-org/widget'")
    assert weekly[0]["reverted"] == 1 and weekly[0]["ci_failure"] == 1


@needs_db
def test_task_effort_counts_only_prompted_sessions(db):
    got = rows(db, "SELECT * FROM ah.task_effort('abc-19')")
    total = next(r for r in got if r["row_kind"] == "total")
    sessions = [r for r in got if r["row_kind"] == "session"]
    assert total["task"] == "ABC-0019" and total["task_title"] == "Widget task"
    assert total["weight"] == "1 counted, 1 mentioned"
    counted = next(r for r in sessions if r["weight"] == "counted")
    mentioned = next(r for r in sessions if r["weight"] == "mentioned")
    assert counted["session_uid"] == CLAUDE_UID and mentioned["priced_cost_usd"] is not None
    assert total["tokens"] == counted["tokens"] and total["priced_cost_usd"] == counted["priced_cost_usd"]
    view = rows(db, "SELECT * FROM ah.v_task_effort WHERE task_key = 'ABC-0019'")[0]
    assert (view["sessions_counted"], view["sessions_mentioned"]) == (1, 1)
    assert view["priced_cost_usd"] == counted["priced_cost_usd"]


@needs_db
def test_unused_features_status(db):
    got = {(r["namespace"], r["name"]): r["status"] for r in rows(db, "SELECT * FROM ah.unused_features(36500)")}
    assert got == {("claude-local", "writing"): "used", ("claude-local", "wplugin:writing"): "used",
                   ("claude-local", "never-used"): "unused",
                   ("claude-local", "just-installed"): "new", ("claude-local", "srv_a"): "used",
                   ("claude-local", "claude.ai Slack"): "unknown", ("codex-local", "codex-skill"): "unknown"}


@needs_db
def test_rate_limit_forecast_burn_and_claude_no_data(db):
    got = {r["namespace"]: r for r in rows(db, "SELECT * FROM ah.v_rate_limit_forecast")}
    codex = got["codex-local"]
    assert codex["used_percent"] == 60 and float(codex["burn_pct_per_hour"]) == 5.0
    assert codex["projected_exhaustion_at"] is not None and codex["projected_exhaustion_at"] < codex["resets_at"]
    assert got["claude-local"]["note"] == "no data"


@needs_db
def test_find_sessions_and_search_summaries(db):
    found = rows(db, "SELECT * FROM ah.find_sessions('synthetic widget', ARRAY['claude-local'], 5)")
    assert found[0]["session_uid"] == CLAUDE_UID and not found[0]["is_subagent"]
    by_summary = rows(db, "SELECT * FROM ah.find_sessions('gizmo', NULL, 5)")
    assert [r["summary_hit"] for r in by_summary] == [True] and by_summary[0]["agent_id"] == ""
    hits = rows(db, "SELECT * FROM ah.search_summaries('gizmos', ARRAY['claude-local'])")   # stemmed
    assert [h["title"] for h in hits] == ["Repairing the gizmo pipeline"]
    assert "gizmo" in hits[0]["snippet"] and hits[0]["session_uid"] == CLAUDE_UID
    assert rows(db, "SELECT * FROM ah.search_summaries('gizmo', ARRAY['codex-other'])") == []


@needs_db
def test_correction_digest_and_denials(db):
    got = rows(db, f"SELECT * FROM ah.correction_digest({LONG_AGO}, ARRAY['claude-local'])")
    session = next(r for r in got if r["row_kind"] == "session")
    assert session["session_uid"] == CLAUDE_UID
    assert (session["user_rejections"], session["errors_then_prompt"]) == (1, 1)
    followups = {r["signal"]: r["excerpt"] for r in got if r["row_kind"] == "followup"}
    assert followups["interrupt:tool_use"] == "continue please"
    assert followups["tool_error:Bash"] == "continue please"
    denials = rows(db, f"SELECT * FROM ah.denials({LONG_AGO}, ARRAY['claude-local'])")
    assert [(d["category"], d["tool_name"], d["cmd_verb"]) for d in denials] == [
        ("user-rejected", "Bash", "git"), ("prompt-log", "Bash", "rm"),
        ("permission-rule (rules working)", "Edit", None)]


@needs_db
def test_week_reports_summary_coverage(db):
    got = rows(db, f"SELECT * FROM ah.week({LONG_AGO}, ARRAY['claude-local'])")
    repo = next(r for r in got if r["project"] == "repo")
    assert repo["summarised"] == 1 and float(repo["coverage"]) == 1.0 and "gizmo" in repo["titles"]
    total = got[-1]
    assert total["project"] == "(all)" and total["sessions"] == 2 and float(total["coverage"]) == 0.5


# --- phase 3: SQL contract (analytics.sql additions) -----------------------------------------------


@needs_db
def test_policy_changes_filters_to_policy_paths(db):
    got = rows(db, f"SELECT * FROM ah.policy_changes(NULL, {LONG_AGO})")
    assert [(r["repo_slug"], r["path"]) for r in got] == [("github.com/example-org/widget", "AGENTS.md")]
    narrowed = rows(db, f"SELECT * FROM ah.policy_changes(%s, {LONG_AGO})", ("%zzz-nomatch%",))
    assert narrowed == []   # a supplied path_like REPLACES the canonical set, not narrows it


@needs_db
def test_rule_effect_before_after_windows(db):
    got = rows(db, "SELECT * FROM ah.rule_effect('github.com/example-org/ruletest', 'rules/foo.md', "
                   "14, ARRAY['claude-other'])")
    assert len(got) == 1
    r = got[0]
    assert (r["before_human_turns"], r["before_corrections"], r["before_denials"],
            r["before_tool_errors"], r["before_interrupts"]) == (4, 2, 1, 1, 2)
    assert (r["after_human_turns"], r["after_corrections"], r["after_denials"],
            r["after_tool_errors"], r["after_interrupts"]) == (2, 0, 0, 0, 0)
    assert float(r["before_corrections_per100"]) == 50.0 and float(r["before_denials_per100"]) == 25.0
    assert float(r["after_corrections_per100"]) == 0.0   # after_human_turns > 0, so a real zero, not NULL
    assert rows(db, "SELECT * FROM ah.rule_effect('no/such', 'no/such.md')") == []


@needs_db
def test_permission_candidates_retried_ok(db):
    got = rows(db, f"SELECT * FROM ah.permission_candidates({LONG_AGO})")
    transcript = {(r["tool_name"], r["cmd_verb"]): r for r in got if r["source"] == "transcript"}
    bash_git = transcript[("Bash", "git")]
    assert bash_git["denials"] == 1 and bash_git["retried_ok"] == 1 and float(bash_git["retried_ok_rate"]) == 1.0
    edit = transcript[("Edit", None)]
    assert edit["denial_kind"] == "permission-rule" and edit["retried_ok"] == 0
    log = next(r for r in got if r["source"] == "permission_log")
    assert log["sessions"] is None and log["retried_ok"] is None   # never inferred for the prompt log


@needs_db
def test_routing_report_and_spawn_outcome_redo(db):
    spawns = rows(db, "SELECT * FROM ah.v_spawn_outcome")
    explore = next(r for r in spawns if r["description"] == "Explore" and r["child_session_id"] is not None)
    assert explore["redo"] is True and explore["resolved_model"] == "claude-sonnet-5"
    redo_spawn = next(r for r in spawns if r["description"] == "Explore" and r["child_session_id"] is None)
    assert redo_spawn["redo"] is False   # nothing later re-does the redo spawn itself
    report = rows(db, f"SELECT * FROM ah.routing_report({LONG_AGO})")
    assert sum(r["redo_count"] for r in report) == 1
    assert sum(r["spawns"] for r in report) == len(spawns) - 1   # one fixture spawn has no spawned_at


@needs_db
def test_infra_actions_normalises_and_drops_bad_hosts(db):
    got = {r["host"]: r for r in rows(db, "SELECT * FROM ah.v_infra_action")}
    assert got["buildhost"]["target_host"] == "buildhost" and got["buildhost"]["remote_verb"] == "docker"
    assert got["build01"]["target_host"] == "build01.example.net"        # FQDN normalised, raw value kept
    assert "$h" not in got and all("$" not in h for h in got)       # unexpanded shell var dropped
    hits = rows(db, "SELECT * FROM ah.infra_actions('build01', now(), interval '1 hour')")
    assert len(hits) == 1 and hits[0]["target_host"] == "build01.example.net"
    assert rows(db, "SELECT * FROM ah.infra_actions('build01.example.net', now(), interval '1 hour')") == []


@needs_db
def test_active_sessions_window(db):
    got = {(r["agent"], r["session_uid"]): r for r in rows(db, "SELECT * FROM ah.active_sessions(36500000)")
           if not r["agent_id"]}
    main_row = got[("claude", CLAUDE_UID)]
    assert main_row["last_human_prompt"] is not None and len(main_row["last_human_prompt"]) <= 120
    assert main_row["data_lag_s"] >= 0
    assert rows(db, "SELECT * FROM ah.active_sessions(0)") == []


@needs_db
def test_open_threads_three_kinds(db):
    got = rows(db, f"SELECT * FROM ah.open_threads({LONG_AGO})")
    by_kind = {}
    for r in got:
        by_kind.setdefault(r["kind"], []).append(r)
    assert any(r["ref"] == "j3" for r in by_kind.get("unfinished_journal", []))
    assert any(r["ref"] == "/home/tester/repo/widget.py" for r in by_kind.get("unmerged_edit", []))
    assert any(r["ref"] == "ABC-0099" for r in by_kind.get("stale_task", []))


@needs_db
def test_lesson_candidates_matches_correction_phrase(db):
    got = rows(db, f"SELECT * FROM ah.lesson_candidates({LONG_AGO})")
    hit = next(r for r in got if "turns" in r["snippet"].lower())
    assert hit["session_uid"] == CLAUDE_UID and hit["session_corrections"] >= 0
    with pytest.raises(Exception, match="since is required"):
        db.execute("SELECT * FROM ah.lesson_candidates(NULL)")
    db.rollback()


@needs_db
def test_wrapped_covers_the_headline_stats(db):
    w = rows(db, f"SELECT ah.wrapped({LONG_AGO}, now() + interval '1 day') AS w")[0]["w"]
    assert w["sessions"] > 0 and w["human_prompts"] > 0
    assert 0 <= w["busiest_hour_of_day"] <= 23
    assert w["most_expensive_session"] is not None and w["most_expensive_session"]["priced_cost_usd"] is not None
    assert isinstance(w["top_tools"], list) and len(w["top_tools"]) > 0
    assert isinstance(w["tokens_and_cost_by_model"], list) and len(w["tokens_and_cost_by_model"]) > 0
    assert w["longest_running_session"] is not None


@needs_db
def test_wrapped_timezone_defaults_to_utc_and_accepts_a_choice(db):
    namespace = "claude-tztest"
    ts = "2025-06-01 23:30:00+00"
    since, until = "2025-06-01 23:29:59+00", "2025-06-01 23:30:01+00"
    session_id = db.execute(
        "INSERT INTO ah.session (agent, session_uid, namespace, is_stub, first_event_at, last_event_at) "
        "VALUES ('claude', 'f0000000-0000-4000-8000-000000000001', %s, false, %s, %s) RETURNING id",
        (namespace, ts, ts),
    ).fetchone()[0]
    text = "A synthetic prompt for timezone behavior."
    db.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
        "content_sha256, source_id, byte_offset, byte_length) "
        "VALUES ('claude', 'timezone-test-prompt', %s, %s, 'tztest', %s, 'user', 'human_prompt', %s, 'tz-test', 0, 0, %s)",
        (session_id, namespace, ts, text, len(text)),
    )
    result = rows(
        db,
        "SELECT ah.wrapped(%s, %s, %s) AS default_utc, "
        "ah.wrapped(%s, %s, %s, 'Europe/London') AS london",
        (since, until, [namespace], since, until, [namespace]),
    )[0]
    assert result["default_utc"]["busiest_hour_of_day"] == 23
    assert result["default_utc"]["latest_night_session"]["local_time"] == "23:30"
    assert result["london"]["busiest_hour_of_day"] == 0
    assert result["london"]["latest_night_session"]["local_time"] == "00:30"


# --- resume command (no database) ----------------------------------------------------------------


