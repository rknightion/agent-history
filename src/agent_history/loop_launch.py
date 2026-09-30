"""Loop launch parser (seam v2.1 section 1).

Pure, stdlib-only module. No imports from other project scripts, and no I/O beyond the explicit `read_file`
injection point and the filesystem existence checks it documents below. Its public surface is
`parse_launch` and `report_target`; do not import anything else from outside.

A message is a launch when its operator text either:

  (a) matches ``\\bYou are the (campaign )?root\\b`` and contains exactly one distinct report
      token; or
  (b) consists only of a path to an existing regular file named ``launch-*.txt`` or
      ``launch-*.md`` (after trimming whitespace and surrounding backticks), whose contents
      satisfy (a).

The launch format is the fan-out loop protocol's: a root prompt naming one report file.
"""

import datetime
import hashlib
import os
import re
from pathlib import Path

MARKER_RE = re.compile(r"\bYou are the (?:campaign )?root\b")

# <name> patterns. Legacy is a strict superset of v2 (it additionally accepts "-waveN.md").
NAME_V2 = r"report-[\w.-]*-loop\d+\.md"
NAME_LEGACY = r"report-[\w.-]*-(?:wave|loop)\d+\.md"

# Report-token prefixes: "/abs/.../codex/", "./codex/" or bare "codex/".
_PREFIX = r"(?:/(?:[^\s`'\"()]*/)*codex/|\./codex/|codex/)"

# Left boundary: start of text, whitespace, or one of ` ' " (
# Right boundary: end of text, whitespace, or one of ` ' " ) , ; :, or a sentence-ending "."
# that is itself followed by whitespace or the end of text ("Write codex/report-x-loop5.md.").
# "codex/report-47.md.backup" still does not match: that "." is followed by "b".
_LEFT = r"(?:\A|(?<=[\s`'\"(]))"
_RIGHT = r"(?:\Z|(?=[\s`'\")\,;:])|(?=\.(?:\s|\Z)))"

# v2 names carry their loop number only as "-loop<N>.md"; legacy names also as "-wave<N>.md".
_LOOP_SUFFIX_RE = re.compile(r"-loop(\d+)\.md$")
_LEGACY_LOOP_SUFFIX_RE = re.compile(r"-(?:wave|loop)(\d+)\.md$")

_BARE_LAUNCH_NAME_RE = re.compile(r"^launch-.*\.(?:txt|md)$")

# Optional "Time budget: <N>s|m|h" line in the recognised launch text (pasted or file-backed).
_BUDGET_RE = re.compile(r"^\s*Time budget:\s*(\d+)\s*([smh])\b", re.IGNORECASE | re.MULTILINE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def _token_pattern(name_pattern):
    return re.compile(_LEFT + "(" + _PREFIX + name_pattern + ")" + _RIGHT)


_TOKEN_RE_V2 = _token_pattern(NAME_V2)
_TOKEN_RE_LEGACY = _token_pattern(NAME_LEGACY)


def _parse_ts(ts):
    if not ts:
        return None
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _is_legacy(launch_ts, activated_at):
    """A launch recognised before activation (or with no activation timestamp) is legacy."""
    if not activated_at:
        return True
    a = _parse_ts(activated_at)
    t = _parse_ts(launch_ts)
    if a is None or t is None:
        return True
    return t < a


def _find_report_tokens(text, legacy):
    pattern = _TOKEN_RE_LEGACY if legacy else _TOKEN_RE_V2
    return [m.group(1) for m in pattern.finditer(text)]


def _extract_loop(token, legacy):
    m = (_LEGACY_LOOP_SUFFIX_RE if legacy else _LOOP_SUFFIX_RE).search(token)
    return int(m.group(1)) if m else None


def _budget_seconds(text):
    m = _BUDGET_RE.search(text)
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()] if m else None


def _single_target(tokens, base, legacy):
    """Resolve every token against `base` and return {"report", "loop"} when they name exactly one
    distinct target, else None.

    A relative token that is a path suffix of an absolute token in the same text is that same
    target, whatever `base` is: the absolute spelling wins, even when the relative one would
    resolve elsewhere (a launch pasted from a subdirectory). Every other token is compared after
    resolution against `base`, so two spellings of one path are one target and a relative path
    that is not such a suffix is a second target.
    """
    absolutes = [os.path.normpath(token) for token in tokens if token.startswith("/")]
    targets = {}
    for token in tokens:
        if not token.startswith("/"):
            rel = token[2:] if token.startswith("./") else token
            if any(a.endswith("/" + rel) for a in absolutes):
                continue
        targets.setdefault(_resolve_token(token, base), token)
    if len(targets) != 1:
        return None
    (report, token), = targets.items()
    return {"report": report, "loop": _extract_loop(token, legacy)}


def _recognize(text, legacy, base):
    """Return {"report": str, "loop": int|None} if `text` matches form (a), else None."""
    if not MARKER_RE.search(text):
        return None
    return _single_target(_find_report_tokens(text, legacy), base, legacy)


def _codex_dir_parent(abs_path):
    """The parent of the nearest enclosing `codex/` directory, or None if there is none."""
    for ancestor in Path(abs_path).parents:
        if ancestor.name == "codex":
            return str(ancestor.parent)
    return None


def _resolve_token(token, base):
    """Resolve a report token to a path. `base` is None for a token that is already absolute.

    A relative token with no base (the pasted-launch cwd was unavailable) is left relative rather
    than failing recognition outright: a caller that scans a transcript incrementally only gets one
    chance to recognise a given launch line, so refusing to recognise it here would lose the launch
    permanently rather than merely defer its resolution to a later, cwd-aware check.
    """
    if token.startswith("/"):
        return os.path.normpath(token)
    rel = token[2:] if token.startswith("./") else token
    if not base:
        return rel
    return os.path.normpath(os.path.join(base, rel))


def _strip_wrapping(text):
    s = text.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        s = s[1:-1].strip()
    return s


def _bare_candidate_path(text):
    """Return the bare path text if `text` is only a path (no other content), else None."""
    s = _strip_wrapping(text)
    if not s or "\n" in s:
        return None
    return s


def _finalize(legacy, target, text, launch_ts, hash_bytes):
    return {
        "report": target["report"],
        "loop": target["loop"],
        "launch_ts": launch_ts,
        "mode": "legacy" if legacy else "v2",
        "launch_sha256": hashlib.sha256(hash_bytes).hexdigest(),
        # Optional, not one of the frozen snapshot keys: the "Time budget" of the recognised text
        # (the launch file's contents for a bare-path launch), in seconds, or None.
        "budget": _budget_seconds(text),
    }


def report_target(text, cwd, mode):
    """Section 1 token grammar and resolution for a message that is not itself a launch (for
    example a "Do not pivot on receipt" replacement): the single distinct report target in `text`,
    as ``{report, loop}``, or None for zero or several targets. `mode` is the armed snapshot's
    report mode ("legacy" or "v2"), which fixes the accepted report-name shapes.
    """
    if not text:
        return None
    legacy = mode == "legacy"
    return _single_target(_find_report_tokens(text, legacy), cwd or None, legacy)


def parse_launch(text, cwd, launch_ts, activated_at, read_file=Path.read_text):
    """Recognise a loop launch in `text`.

    Returns ``{report, loop, launch_ts, mode, launch_sha256, budget}`` or None (`budget` is an
    optional, non-frozen key: seconds or None). `cwd` is the launch-time
    working directory used to resolve a relative report token pasted directly in `text`. `read_file`
    is called as ``read_file(Path(path))`` and defaults to `Path.read_text`, letting a caller stub
    file reads in tests without touching the real filesystem.
    """
    if not text:
        return None
    legacy = _is_legacy(launch_ts, activated_at)

    direct = _recognize(text, legacy, cwd or None)
    if direct is not None:
        return _finalize(legacy, direct, text, launch_ts, text.encode("utf-8"))

    candidate = _bare_candidate_path(text)
    if candidate is None:
        return None
    launch_path = candidate if os.path.isabs(candidate) else os.path.normpath(os.path.join(cwd or "", candidate))
    if not _BARE_LAUNCH_NAME_RE.match(os.path.basename(launch_path)):
        return None
    if not os.path.isfile(launch_path):
        return None
    try:
        content = read_file(Path(launch_path))
    except (OSError, ValueError):  # ValueError includes UnicodeDecodeError
        return None
    if not isinstance(content, str):
        return None

    # A relative report resolves against the parent of the codex/ directory holding the launch
    # file. Outside any codex/ directory there is no base, so the file must name an absolute
    # report path (a relative token survives only as a suffix of that absolute one).
    inner = _recognize(content, legacy, _codex_dir_parent(launch_path))
    if inner is None or not os.path.isabs(inner["report"]):
        return None
    return _finalize(legacy, inner, content, launch_ts, content.encode("utf-8"))
