"""The suite never inherits telemetry exporter settings from the shell that runs it."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INNER = "AGENT_HISTORY_HERMETIC_INNER"


def _otel_names(environ):
    return sorted(name for name in environ if name.upper().startswith("OTEL_"))


def test_tests_start_without_inherited_otel_variables():
    assert _otel_names(os.environ) == []


def test_default_subprocess_environment_is_free_of_otel_variables():
    result = subprocess.run(
        [sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.upper().startswith('OTEL_')))"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_a_test_may_set_its_own_otel_variables(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:1")
    assert _otel_names(os.environ) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]


@pytest.mark.skipif(os.environ.get(INNER) == "1", reason="the inner run is the thing being observed")
def test_a_shell_that_exports_otel_variables_does_not_reach_the_suite():
    env = {**os.environ, INNER: "1"}
    env.update(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME": "inherited-shell",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_hermetic_telemetry_env.py::test_tests_start_without_inherited_otel_variables",
            "tests/test_hermetic_telemetry_env.py::test_default_subprocess_environment_is_free_of_otel_variables",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
