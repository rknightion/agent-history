"""Periodic workers drain on SIGTERM: the pass in flight finishes, no new pass starts, exit 0."""

import signal
import subprocess
import sys
import textwrap
import time

import pytest

from agent_history import loop_live
from agent_history.config import Config, LoopLive

# Drives the real periodic scheduler in its own process; only the single pass is synthetic, so the
# signal reaches the same handler a container stop does.
WORKER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from agent_history import cli

    marks = Path(sys.argv[1])
    real = cli.main

    def worker(argv):
        if "--every" in argv:
            return real(argv)
        with marks.open("a") as out:
            out.write("start\\n")
        time.sleep(float(sys.argv[3]))
        with marks.open("a") as out:
            out.write("end\\n")
        return 0

    cli.main = worker
    sys.exit(worker([sys.argv[2], "--every", sys.argv[4]]))
    """
)


def _start(tmp_path, command, pass_seconds, every):
    marks = tmp_path / "marks"
    marks.touch()
    proc = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(marks), command, str(pass_seconds), str(every)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, marks


def _wait_for(marks, text, proc):
    deadline = time.monotonic() + 10
    while text not in marks.read_text():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "worker never started a pass"
        time.sleep(0.05)


@pytest.mark.parametrize("command", ["index", "embed", "journal-sync"])
def test_sigterm_mid_pass_finishes_the_pass_and_admits_no_other(tmp_path, command):
    proc, marks = _start(tmp_path, command, pass_seconds=1.5, every=0.1)
    _wait_for(marks, "start", proc)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, err
    assert marks.read_text().split() == ["start", "end"]
    assert f"{command} drain requested" in err
    assert f"{command} drained" in err


def test_sigterm_between_passes_exits_without_waiting_out_the_interval(tmp_path):
    proc, marks = _start(tmp_path, "embed", pass_seconds=0, every=300)
    _wait_for(marks, "end", proc)
    began = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, err
    assert time.monotonic() - began < 5
    assert marks.read_text().split() == ["start", "end"]


def test_loop_live_pass_claims_no_job_after_a_drain_request(monkeypatch):
    config = Config(dsn="synthetic", loop_live=LoopLive(enabled=True))
    monkeypatch.setattr(loop_live, "load_config", lambda: config)
    monkeypatch.delenv("AGENT_HISTORY_DSN", raising=False)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(loop_live.psycopg, "connect", lambda dsn, **kw: Connection())

    class Stop:
        requested = False

    stop = Stop()
    calls = []

    def drain_one(*_):
        calls.append(True)
        stop.requested = True  # the signal lands while this job is in flight
        return True

    monkeypatch.setattr(loop_live, "drain_one", drain_one)
    loop_live.run_pass(stop)
    assert len(calls) == 1
