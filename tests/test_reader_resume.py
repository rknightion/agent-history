"""Resume hints keep the catalogue identity and select the namespace's local home."""

import csv
import io
import shlex
import subprocess

import pytest

from agent_history import cli


@pytest.mark.parametrize(
    ("agent", "profile", "native_context", "launcher", "expected"),
    [
        ("codex", "flash", "personal", True, "{namespace} resume"),
        ("codex", "flash", "flash", True, "{namespace} resume"),
        ("codex", "flash", "work", False, "CODEX_HOME={home}/.{namespace} codex resume"),
        ("codex", "personal", "personal", True, "env -u CODEX_HOME codex resume"),
        ("codex", "work", "work", True, "env -u CODEX_HOME codex resume"),
        ("codex", "work", "personal", True, "{namespace} resume"),
        ("codex", "personal", "work", False, "CODEX_HOME={home}/.{namespace} codex resume"),
        ("claude", "personal", "personal", True, "env -u CLAUDE_CONFIG_DIR claude --resume"),
        ("claude", "work", "personal", True, "{namespace} --resume"),
    ],
)
def test_resume_cli_preserves_uid_and_home(
    agent, profile, native_context, launcher, expected, monkeypatch, tmp_path, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    namespace = f"{agent}-{profile}"
    (home / f".{agent}").mkdir()
    (home / f".{namespace}").mkdir()
    cwd = tmp_path / "project with spaces"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_HISTORY_NATIVE_CONTEXT", native_context)
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", "dbname=synthetic")
    config = tmp_path / "config.toml"
    config.write_text("")
    # Use real executable discovery, not the operator's installed launchers.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    if launcher:
        executable = bin_dir / namespace
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    uid = "synthetic-thread 'exact identity'"
    row = {
        "agent": agent,
        "namespace": namespace,
        "session_uid": uid,
        "agent_id": "",
        "is_subagent": "f",
        "last_event_at": "2030-01-02T03:04:05Z",
        "title": "Synthetic resume fixture",
        "cwd": str(cwd),
        "machine": "",
    }
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)

    def process(argv, **kwargs):
        assert "ah.find_sessions" in kwargs["input"]
        assert namespace in kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, stdout=output.getvalue())

    monkeypatch.setattr(subprocess, "run", process)
    assert cli.main(["--config", str(config), "resume", "synthetic", "--ns", namespace]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        f"cd {shlex.quote(str(cwd))} && {expected.format(home=home, namespace=namespace)} {shlex.quote(uid)}"
    )
    assert uid in captured.err
    assert "warning:" not in captured.err
