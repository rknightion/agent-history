"""embed.py pure parts: secret redaction spans, raw-offset chunking, input building, provider batching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_history import embed


def test_scrub_spans_cover_known_secret_shapes():
    # Secret-shaped values are assembled at run time so no scanner sees a literal token.
    cf, sk, gh, ak, jwt = ("cf" + "ut_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
                           "sk-" + "proj-" + "abcdefghijklmnopqrstuvwx12",
                           "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789",
                           "AK" + "IA" + "ABCDEFGHIJKLMNOP",
                           "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk")
    text = (f"token {cf} and {sk} plus {gh} and {ak}; password=hunter2hunter2 "
            f"Authorization: Bearer {jwt}")
    redacted = embed.redact(text, embed.scrub_spans(text))
    for secret in (cf[:11], sk[:11], gh[:10], ak[:10], "hunter2", jwt[:8]):
        assert secret not in redacted
    assert redacted.startswith("token [REDACTED] and [REDACTED]")


@pytest.mark.parametrize("text, secret", [
    ("POSTGRES_PASSWORD: hunter2hunter2", "hunter2"),
    ("  - DB_PASSWORD=hunter2hunter2", "hunter2"),
    ("sudo CF_API_TOKEN=abcDEF123456789xyzABCDEF12 ./run.sh", "abcDEF1234"),
    ("export MERAKI__API_KEY=0123456789abcdef0123456789abcdef01234567", "0123456789abcdef"),
    ('{"refresh_token": "1.AbCdEfGhIjKlMnOp"}', "AbCdEfGhIjKl"),
    ('"secretText":"Xy78Q' + '~AbCdEfGhIjKlMnOpQrStUvWxYz0123456"', "AbCdEfGhIjKl"),
    ("client secret is Ab18Q" + "~abcdefghijklmnopqrstuvwxyz01234567", "abcdefghijklmnop"),
    ("BAO_TOKEN value s.AbCdEfGhIjKlMnOpQrStUvWx", "AbCdEfGhIjKlMnOp"),
    ("curl -u admin:s3cretPassw0rd https://x", "s3cretPassw0rd"),
    ("https://" + "sample-user:neutral-pass@" + "service/x", "neutral-pass"),
    ("db.example.test:5432:demo_catalog:demo_reader:ExamplePass12345", "ExamplePass12345"),
])
def test_scrub_catches_corpus_shapes(text, secret):
    assert secret not in embed.redact(text, embed.scrub_spans(text))


@pytest.mark.parametrize("text", [
    "def cmd_verb(command):\n    return shlex.split(command)[0]  # sha 3f2a1b9",
    "HomebrewInventoryMaximumVersionUtf8Bytes = 8 * 1024",
    "swift/Tests/ExampleProtocolTests/PreauthorisedEnrollmentV2Tests.swift",
    "2026-08-25T12:00:00.000Z/2026-08-26T12:00:00.000Z",
    "commit 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a on main",
])
def test_scrub_leaves_ordinary_code_alone(text):
    assert embed.scrub_spans(text) == []


def test_chunks_are_raw_offsets_and_never_split_a_secret():
    secret = "ghp_" + "a" * 36
    text = ("word " * 1150) + secret + (" word" * 1500)
    spans = embed.scrub_spans(text)
    chunks = embed.chunk_spans(text, spans, max_chars=6000, overlap=800)
    assert len(chunks) >= 2
    assert chunks[0][0] == 0 and chunks[-1][1] == len(text)
    for start, end in chunks:
        for s, e in spans:
            assert not (start < s < end < e) and not (s < start < e < end), "boundary inside a redaction span"
    for (a0, a1), (b0, b1) in zip(chunks, chunks[1:]):
        assert b0 < a1 and b1 > a1  # overlapping and advancing


def test_chunks_prefer_paragraph_boundaries_and_keep_fences_whole():
    fence = "```python\n" + ("x = 1\n" * 400) + "```\n"
    text = ("para one. " * 300) + "\n\n" + fence + "\n\n" + ("para two. " * 300)
    chunks = embed.chunk_spans(text, [], max_chars=6000, overlap=800)
    fence_start = text.index("```python")
    fence_end = text.index("```\n", fence_start + 3) + 4
    for start, end in chunks:
        assert not (start < fence_start < end < fence_end) or end - start >= 6000 * 0.9


def test_short_text_is_one_chunk():
    assert embed.chunk_spans("hello there, this is short", [], max_chars=6000, overlap=800) == [(0, 26)]


def test_non_ascii_text_gets_smaller_chunks():
    text = "日本語のテキスト。" * 2000
    chunks = embed.chunk_spans(text, [], max_chars=6000, overlap=800, max_tokens=3000)
    assert all(embed.estimate_tokens(text[s:e]) <= 3000 for s, e in chunks)


def test_build_input_is_stable_header_plus_redacted_slice():
    text = "use key " + "sk-" + "proj-abcdefghijklmnopqrstuvwx12 to call"
    spans = embed.scrub_spans(text)
    one = embed.build_input("human_prompt", "/home/tester/repos/example-app", "claude-local", text, spans, 0, len(text))
    assert one == "[human_prompt] example-app\nuse key [REDACTED] to call"
    assert embed.build_input("human_prompt", None, "claude-local", "hi there", [], 0, 8) == \
        "[human_prompt] claude-local\nhi there"


@pytest.mark.parametrize("text, expected", [("ok", False), ("continue", False), ("  Continue.  ", False),
                                            ("x" * 29, False), ("please check the alloy config now", True)])
def test_eligible_text(text, expected):
    assert embed.eligible(text) is expected


def test_split_batches_halves_on_client_error():
    calls = []

    def fake(texts):
        calls.append(len(texts))
        if any(t == "bad" for t in texts):
            raise embed.ProviderError(400, "too long")
        return [[1.0] + [0.0] * 1023 for _ in texts]

    good, failed = embed.embed_resilient(fake, ["a", "b", "bad", "d"])
    assert set(good) == {"a", "b", "d"} and failed == {"bad": "400"}
    assert calls[0] == 4


def test_normalise_unit_length():
    v = embed.normalise([3.0, 4.0] + [0.0] * 1022)
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9 and len(v) == 1024


def test_auth_errors_stop_instead_of_blaming_inputs():
    def denied(texts):
        raise embed.ProviderError(403, "error code: 1010")

    with pytest.raises(embed.ProviderError):
        embed.embed_resilient(denied, ["a", "b"])


@pytest.mark.parametrize("status", [0, 429, 503])
def test_outages_stop_the_run_without_blaming_inputs(status):
    def down(texts):
        raise embed.ProviderError(status, "unavailable")

    with pytest.raises(embed.ProviderError):
        embed.embed_resilient(down, ["a", "b", "c"])


# --- embed-gc ------------------------------------------------------

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def pg_text(dt):
    """ah.meta values are written as now()::text: '2026-09-26 12:00:00.123+00'."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f+00")


@pytest.mark.parametrize("reset, rebuilding, backlog, expected", [
    (pg_text(NOW - timedelta(days=8)), False, False, None),
    (pg_text(NOW - timedelta(days=8)), True, False, "rebuild_in_progress"),
    (None, False, False, "no_reset_marker"),
    ("yesterday-ish", False, False, "bad_reset_marker"),
    (pg_text(NOW - timedelta(days=6, hours=23)), False, False, "recent_chunk_reset"),
    (pg_text(NOW - timedelta(days=8)), False, True, "backlog_pending"),
    ("2026-09-18 13:00:00+01", False, False, None),            # 8 days before NOW, other offset
    ("2026-09-20 12:00:00.5+05:30", False, False, "recent_chunk_reset"),
])
def test_gc_skip_reason(reset, rebuilding, backlog, expected):
    assert embed.gc_skip_reason(NOW, reset, rebuilding, backlog) == expected


@pytest.mark.parametrize("last, due", [
    (None, True), ("garbage", True),
    (pg_text(NOW - timedelta(hours=23)), False),
    (pg_text(NOW - timedelta(hours=25)), True),
])
def test_gc_due_once_per_interval(last, due):
    assert embed.gc_due(NOW, last, 24) is due


class _NoDb:
    def execute(self, *a, **k):
        raise embed.psycopg.OperationalError("no database in unit tests")

    def rollback(self):
        pass


def test_embed_metrics_carry_gc_lines_between_daily_runs(tmp_path):
    prom = tmp_path / "agent-history-embed.prom"
    stats = embed.EmbedStats()
    stats.gc = {"dry_run": False, "eligible": 7, "deleted": 5, "skipped": "", "at": 1_790_000_000}
    embed.write_metrics(_NoDb(), stats, True, prom)
    text = prom.read_text()
    assert "agent_history_embed_gc_deleted 5" in text and "agent_history_embed_gc_eligible 7" in text
    # the next ten-minute run does no GC: the last GC result survives the rewrite
    embed.write_metrics(_NoDb(), embed.EmbedStats(), True, prom)
    again = prom.read_text()
    assert "agent_history_embed_gc_deleted 5" in again
    assert again.count("agent_history_embed_gc_deleted") == 1


def test_gc_metrics_update_keeps_embed_lines(tmp_path):
    prom = tmp_path / "agent-history-embed.prom"
    embed.write_metrics(_NoDb(), embed.EmbedStats(items=3), True, prom)
    embed.write_gc_metrics({"dry_run": True, "eligible": 9, "deleted": 0, "skipped": "", "at": 1}, prom)
    text = prom.read_text()
    assert "agent_history_embed_run_items 3" in text
    assert "agent_history_embed_gc_eligible 9" in text and "agent_history_embed_gc_dry_run 1" in text


def test_cli_embed_gc_and_embed_wire_gc(monkeypatch):
    from agent_history import cli, load

    class Conn:
        def close(self):
            pass

    calls = {}
    monkeypatch.setattr(load, "connect", lambda dsn=None: Conn())
    monkeypatch.setattr(embed, "gc", lambda conn, dry_run=False, max_rows=embed.GC_MAX_ROWS, **k:
                        calls.setdefault("gc", (dry_run, max_rows)) and embed.GcStats(dry_run=dry_run))
    assert cli.main(["embed-gc", "--dry-run", "--max-rows", "5"]) == 0
    assert calls["gc"] == (True, 5)

    monkeypatch.setattr(embed, "provider_from_config", lambda config=None: None)
    monkeypatch.setattr(embed, "run", lambda conn, provider, cap, daily, **k:
                        calls.setdefault("run", k) and embed.EmbedStats())
    assert cli.main(["embed"]) == 0
    assert calls["run"].get("gc_interval_hours") == 24
