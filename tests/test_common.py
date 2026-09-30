"""common.ssh_target: remote host + remote verb from shell commands (hostnames only, never arguments)."""

from __future__ import annotations

import pytest

from agent_history.common import ssh_target


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
