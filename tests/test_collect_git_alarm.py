"""Worker-thread refusal must leave the process-wide caller alarm untouched."""

import signal
import threading
import time

import pytest

from agent_history import collect_git


@pytest.mark.parametrize("entrypoint", ["main", "run"])
@pytest.mark.parametrize("seconds, interval", [(0.0, 0.0), (120.0, 0.0), (120.0, 60.0)])
def test_thread_refusal_does_not_touch_callers_timer(tmp_path, monkeypatch, capsys, entrypoint, seconds, interval):
    config = tmp_path / "config.toml"
    config.write_text(f'[git]\nrepos = []\n[collector]\nhomes = {{}}\nlock_file = "{tmp_path / "lock"}"\n')
    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)
    timer_calls = []
    real_setitimer = signal.setitimer
    real_alarm = signal.alarm

    def setitimer(*args):
        timer_calls.append(args)
        return real_setitimer(*args)

    def alarm(*args):
        timer_calls.append(args)
        return real_alarm(*args)

    def previous_handler(signum, frame):
        pytest.fail("the caller's long timer must not expire during this bounded test")

    results = []

    def invoke():
        try:
            results.append(getattr(collect_git, entrypoint)(["--dry-run", "--config", str(config)]))
        except Exception as error:
            results.append(error)

    signal.signal(signal.SIGALRM, previous_handler)
    real_setitimer(signal.ITIMER_REAL, seconds, interval)
    monkeypatch.setattr(signal, "setitimer", setitimer)
    monkeypatch.setattr(signal, "alarm", alarm)
    started = time.monotonic()
    try:
        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "thread refusal must not start collection"
        assert timer_calls == [], "refusal must precede even cancelling the caller's timer"
        assert signal.getsignal(signal.SIGALRM) is previous_handler
        remaining, repeat = signal.getitimer(signal.ITIMER_REAL)
        assert remaining == pytest.approx(max(0, seconds - (time.monotonic() - started)), abs=0.1)
        assert repeat == interval
        assert not (tmp_path / "lock").exists()
        assert len(results) == 1
        if entrypoint == "main":
            assert isinstance(results[0], ValueError)
            assert "main thread" in str(results[0])
        else:
            assert results == [1]
            assert "ValueError" in capsys.readouterr().err
    finally:
        real_setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)
        real_setitimer(signal.ITIMER_REAL, *original_timer)
