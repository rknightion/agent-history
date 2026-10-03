"""common.ssh_target: remote host + remote verb from shell commands (hostnames only, never arguments)."""

from __future__ import annotations

import pytest

from agent_history.common import git_event_extras, git_ops_from_command, ssh_target


@pytest.mark.parametrize("command, expected", [
    ("ssh buildhost 'docker compose up -d'", ("buildhost", "docker")),
    ("ssh -i ~/.ssh/key -p 2222 deploy@buildhost sudo docker ps", ("buildhost", "docker")),
    ("cd /tmp && ssh runner1 uptime", ("runner1", "uptime")),
    ("echo x | ssh vmhost 'bash -s'", ("vmhost", "bash")),
    ("ssh -o ConnectTimeout=5 -J bastion root@192.0.2.10 -- systemctl restart nginx", ("192.0.2.10", "systemctl")),
    ("ssh ssh://deploy@backup.example.net:2200 ls", ("backup.example.net", "ls")),
    ("ssh buildhost", ("buildhost", None)),
    ("tailscale ssh vmhost qm list", ("vmhost", "qm")),
    ("scp -r runner1:/opt/x .", ("runner1", None)),
    ("scp ./file.txt deploy@buildhost:/tmp/", ("buildhost", None)),
    ("rsync -av -e ssh ./dir buildhost:/opt/dir", ("buildhost", None)),
    ("sudo env FOO=1 ssh buildhost 'cat /etc/hostname'", ("buildhost", "cat")),
    ("bash -c \"ssh buildhost 'journalctl -u nginx'\"", ("buildhost", "journalctl")),
    (["bash", "-lc", "ssh BUILDHOST uptime"], ("buildhost", "uptime")),
    ("timeout 10 ssh buildhost uptime", ("buildhost", "uptime")),
    ("timeout -k 5 30s ssh runner1 'df -h'", ("runner1", "df")),
    ("nice -n 10 ssh vmhost uptime", ("vmhost", "uptime")),
    ("ssh -ti ~/.ssh/key buildhost uptime", ("buildhost", "uptime")),
])
def test_remote_commands(command, expected):
    assert ssh_target(command) == expected


@pytest.mark.parametrize("command", [
    "scp a.txt b.txt",
    "rsync -av ./a ./b",
    "git push origin main",
    "echo 'ssh buildhost'",
    "ls ~/.ssh",
    "ssh ${HOSTS[@]} uptime",
    "ssh $h uptime",
    "scp C:\\x d.txt",
    "",
    None,
])
def test_local_commands(command):
    assert ssh_target(command) is None


@pytest.mark.parametrize("command, ops", [
    ("git commit -q -m x", ["commit"]),
    ('git -C "/a dir" commit -m x', ["commit"]),
    ("git -c user.name=x -c core.pager=cat commit --amend", ["commit"]),
    ('cd w && git add . && git commit -qm "a; git push" && git push -q origin main', ["commit", "push"]),
    ("bash -lc 'git commit -qm hi'", ["commit"]),
    ("(cd w && git commit -qm x)", ["commit"]),
    ("git add . ; git commit -qm x", ["commit"]),
    ("git commit -qm x;", ["commit"]),
    ("set -o pipefail; git commit -qm x | tee log", ["commit"]),
    ("set -euo pipefail; git commit -qm x | tee log", ["commit"]),
    # success of the whole command does not prove the commit ran or worked
    ("git commit -qm x || true", []),
    ("git commit -qm x; echo done", []),
    ("git commit -qm x\necho done", []),
    ("git commit -qm x | tee log", []),
    ("git commit -qm x | tee pipefail.log", []),
    ("echo pipefail; git commit -qm x | tee log", []),
    ("make test || git commit -qm x", []),
    ("git commit -qm x &", []),
    ("if git commit -q -m x; then echo ok; fi", []),
    ("GIT_AUTHOR_NAME=x git cherry-pick abc", ["cherry_pick"]),
    (["/bin/zsh", "-lc", "git -C . push origin main"], ["push"]),
    ("echo 'git commit'", []),
    ("git status | grep commit", []),
    ("git push --dry-run", []),
    ("git push -n origin main", []),
    ("git commit --dry-run", []),
    ("git cherry-pick --no-commit abc", []),
    ("git log --oneline", []),
    ("", []),
    (None, []),
])
def test_git_ops_from_command(command, ops):
    assert git_ops_from_command(command) == ops


def test_git_event_extras_skip_what_output_already_showed():
    # one output-matched commit covers one command commit; the second is command-only
    assert git_event_extras(["commit", "commit", "push"], 1, 0) == [("commit", 1), ("push", 0)]
    assert git_event_extras(["commit", "push"], 1, 1) == []
