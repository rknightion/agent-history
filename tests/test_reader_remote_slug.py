"""The reader resolves a remote to a repo slug with the collector's grammar, not its own."""

import subprocess

import pytest

from agent_history import reader


@pytest.mark.parametrize(
    "remote",
    [
        # '#' ends the authority for curl, so the host is github.com and the rest is a fragment.
        "https://github.com#@evil.example/o/n",
        # git percent-decodes the whole URL first: this dials evil.example.
        "ssh://git%40evil.example%2fx@github.com/owner/repo",
        # a leading [...] is taken as the host by the ssh client.
        "ssh://[evil.example]@github.com/owner/repo.git",
        # the git:// protocol has no userinfo.
        "git://x@github.com/owner/repo",
        "https://github.com/owner/repo?x=1",
        "https://github.com/owner/repo/extra",
        "file:///srv/owner/repo",
        "",
    ],
)
def test_a_remote_the_collector_rejects_gives_no_slug(remote):
    assert reader._repo_slug_from_remote(remote) is None


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("git@github.com:owner/repo.git", "github.com/owner/repo"),
        ("https://github.com/owner/repo", "github.com/owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "github.com/owner/repo"),
        ("https://GitHub.com/Owner/Repo/", "github.com/owner/repo"),
        # a look-alike host keeps its own literal host and never becomes github.com
        ("https://github.com.example.net/owner/repo", "github.com.example.net/owner/repo"),
    ],
)
def test_an_accepted_remote_resolves_to_its_own_host(remote, slug):
    assert reader._repo_slug_from_remote(remote) == slug


def test_find_local_checkout_ignores_a_checkout_whose_origin_only_looks_like_the_slug(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com#@evil.example/o/n"], check=True
    )
    config = tmp_path / "config.toml"
    config.write_text(f'[git]\nrepos=["{repo}"]\n')
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(config))
    assert reader.find_local_checkout("evil.example/o/n") is None
    assert reader.find_local_checkout("github.com/o/n") is None


def test_find_local_checkout_finds_a_checkout_whose_origin_the_collector_accepts(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:owner/repo.git"], check=True)
    config = tmp_path / "config.toml"
    config.write_text(f'[git]\nrepos=["{repo}"]\n')
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(config))
    assert reader.find_local_checkout("github.com/owner/repo") == repo
