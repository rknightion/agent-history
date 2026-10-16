"""Lifecycle markers are observations, never process-liveness assertions."""

from datetime import datetime, timezone

import pytest

from agent_history.loop_v3_status import parse_status

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Progress.\nWAITING: review until 2026-10-04T13:00:00Z\n\n", ("waiting", NOW.replace(hour=13))),
        ("WAITING: review until 2026-10-04T13:00Z", ("waiting", NOW.replace(hour=13))),
        ("WAITING: review until 2026-10-04T12:00Z", None),
        ("WAITING: review until 2026-10-04T13:00", None),
        ("PAUSED: owner decision", ("paused", None)),
        ("WAITING: review until 2026-10-04T13:00:00+00:00", ("waiting", NOW.replace(hour=13))),
        ("WAITING: review until 2026-10-04T12:00:00Z", None),
        ("WAITING: review until 2026-10-03T13:00:00Z", None),
        ("WAITING: review until 2026-10-04T13:00:00+01:00", None),
        ("WAITING: review until 2026-10-04T13:00:00", None),
        ("WAITING: review until tomorrow", None),
        ("WAITING:  until 2026-10-04T13:00:00Z", None),
        ("PAUSED:   ", None),
        ("Paused: owner decision", None),
        ("PAUSED: owner decision\nContinuing now.", None),
        ("```\nPAUSED: example\n```", None),
        ("", None),
    ],
)
def test_last_nonempty_line_is_the_only_marker(text, expected):
    assert parse_status(text, now=NOW) == expected
