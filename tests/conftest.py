"""Hermetic telemetry environment for the whole suite.

The shell that runs the tests may export OpenTelemetry settings (an OTLP endpoint, headers, a service
name). Left in place they would make every subprocess and every in-process `telemetry.setup` start real
exporters against whatever the shell points at, slow down shutdown and change diagnostics.

Every `OTEL_*` variable is therefore removed when pytest loads this file, before any test module is
imported or any subprocess is started, and again around each test. A test that covers telemetry sets its
own variables with `monkeypatch.setenv`, pointing at a loopback receiver it started itself.
"""

import os

import pytest

_PREFIX = "OTEL_"


def _scrub(environ) -> list[str]:
    names = [name for name in environ if name.upper().startswith(_PREFIX)]
    for name in names:
        del environ[name]
    return names


# Inherited variables are dropped here so that subprocesses spawned with the default environment, and
# collection-time imports, never see them.
_scrub(os.environ)


@pytest.fixture(autouse=True)
def _isolated_telemetry_environment(monkeypatch):
    for name in [name for name in os.environ if name.upper().startswith(_PREFIX)]:
        monkeypatch.delenv(name)
    yield
