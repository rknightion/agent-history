"""The lane brief header that names a live lane's task.

By the loop contract a root's lane brief opens with exactly one line

    Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded

`ah.lane_brief_task` (migration 043) applies LANE_HEADER_PATTERN to a brief's first line. This
module is the reference grammar the tests hold that SQL to; nothing else reads a task from a brief.
The pattern is written to mean the same in Python `re` and PostgreSQL's regular expressions.
"""

from __future__ import annotations

import re

LANE_HEADER_PATTERN = (
    r"^Lane: [!-~]+ · Task: ([A-Za-z][A-Za-z0-9]*-[0-9]+(?:\.[0-9]+)*) \((.+)\) · Tier: (?:routine|guarded)$"
)
TITLE_MAX = 500
REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_HEADER = re.compile(LANE_HEADER_PATTERN)


def parse_lane_header(brief: str) -> tuple[str, str] | None:
    """(task id, title) from a brief whose first line is the lane header, else None.

    Only the first line counts, and only when it is exactly the header: a task named anywhere else
    in the brief is not this lane's task. A title longer than TITLE_MAX characters is no header.
    """
    first = brief.split("\n", 1)[0].rstrip("\r")
    match = _HEADER.match(first)
    if match is None or len(match.group(2)) > TITLE_MAX:
        return None
    return match.group(1), match.group(2)
