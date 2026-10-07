"""Graceful stop for periodic workers.

SIGTERM or SIGINT asks the worker to drain: the pass in flight runs to its own commit or rollback,
no new pass (or loop-live job) is admitted, and the worker exits 0. A stopped container is the hold;
nothing restarts it until the operator does. During a pass the handler only sets a flag, so it never
interrupts a database transaction, a provider request or a state write part way through; between
passes it also ends the idle sleep so the stop does not wait out the interval.
"""

import os
import signal
import sys
import threading
import time

SIGNALS = (signal.SIGTERM, signal.SIGINT)


_active = []  # the Drain this process is running under, if any


class _Wake(Exception):
    pass


def stop_requested():
    """True once the enclosing periodic worker has been asked to drain; long passes check it at
    their own safe boundaries (a committed batch) and return early."""
    return bool(_active) and _active[-1].requested


class Drain:
    def __init__(self, name):
        self.name = name
        self.requested = False
        self._idle = False
        self._previous = {}

    def __enter__(self):
        _active.append(self)
        if threading.current_thread() is threading.main_thread():
            for signum in SIGNALS:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._request)
        return self

    def __exit__(self, *exc):
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()
        _active.remove(self)
        return False

    def _request(self, signum, frame):
        if not self.requested:
            self.requested = True
            # os.write, not print: a buffered stream write is not reentrant from a signal handler,
            # and a pass may have redirected sys.stderr to a sink the operator never sees.
            try:
                os.write(2, f"agent-history: {self.name} drain requested (signal {signum})\n".encode())
            except OSError:
                pass
        if self._idle:
            raise _Wake  # only ever raised out of the idle sleep, never inside a pass

    def wait(self, seconds, sleep=time.sleep):
        """Sleep the interval once; a drain request ends the sleep early. Returns `requested`."""
        try:
            self._idle = True
            try:
                if not self.requested:
                    sleep(seconds)
            finally:
                self._idle = False
        except _Wake:
            pass
        return self.requested

    def drained(self):
        print(f"agent-history: {self.name} drained", file=sys.stderr, flush=True)
