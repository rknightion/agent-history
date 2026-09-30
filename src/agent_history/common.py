"""Helpers shared by both parsers. Pure functions, stdlib only."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

MARKDOWN_PATH = re.compile(r"\[[^\]]*\]\((?:file://)?(<?/[^)>]+>?)(?:\s+['\"][^'\"]*['\"])?\)")
GIT_COMMIT_LINE = re.compile(r"^\[([^\]\s]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\]", re.M)
GIT_PUSH_RANGE = re.compile(r"^\s*([0-9a-f]{7,})\.\.([0-9a-f]{7,})\s+(\S+)\s+->\s+(\S+)", re.M)
BOUNDED_STATE_SECONDS = 24 * 3600


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 string or epoch seconds/milliseconds to an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def json_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass"))


def key_set(record: dict[str, Any]) -> str:
    return ",".join(sorted(record.keys()))


def text_blocks(content: Any, allowed: set[str]) -> str:
    """Join text of the allowed block types; a bare string counts as text."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    values = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in allowed:
            continue
        value = block.get("text")
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "\n\n".join(values)


def artifact_kind(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".xlsx", ".xls", ".csv", ".tsv", ".ods"}:
        return "spreadsheet"
    if suffix in {".docx", ".doc", ".pdf", ".md", ".txt", ".rtf"}:
        return "document"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}:
        return "image"
    if suffix in {".pptx", ".ppt", ".key"}:
        return "presentation"
    if suffix:
        return "file"
    return "path"


def linked_paths(text: str) -> list[str]:
    """Absolute paths an assistant linked with Markdown, in order, de-duplicated (v1 parity)."""
    found: list[str] = []
    for match in MARKDOWN_PATH.finditer(text):
        path = match.group(1).strip("<>")
        if path not in found:
            found.append(path)
    return found


WRAPPERS = {"sudo", "env", "time", "nice", "nohup", "exec", "command", "timeout", "gtimeout", "caffeinate",
            "ionice", "chrt", "xargs", "npx", "bunx", "stdbuf"}
SKIP_VERBS = {"cd", "pushd", "popd", "export", "set", "source", ".", "unset", "local", "true", ":"}
OPERATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "{", "}"}


def cmd_verb(command: Any) -> str | None:
    """First executable word of a shell command, basename only; never arguments or values.

    Tokenises the whole command (quotes respected) before splitting on shell operators, then skips
    env assignments, wrapper commands with their flags/numeric arguments, and builtins such as cd.
    `cd x && timeout 900 codex exec ...` gives 'codex'; `uv run pytest` gives 'pytest'.
    """
    if isinstance(command, list):
        words = [w for w in command if isinstance(w, str)]
        if len(words) >= 3 and PurePosixPath(words[0]).name in {"bash", "sh", "zsh"} and words[1] in {"-c", "-lc"}:
            return cmd_verb(words[2])
        command = shlex.join(words)
    if not isinstance(command, str) or not command.strip():
        return None
    lexer = shlex.shlex(command.replace("\n", "\n;"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segment: list[str] = []
    for token in tokens + [";"]:
        if token in OPERATORS or set(token) <= set(";&|()"):
            verb = _segment_verb(segment)
            if verb:
                return verb
            segment = []
        else:
            segment.append(token)
    return None


def _segment_verb(words: list[str]) -> str | None:
    i = 0
    while i < len(words):
        word = words[i]
        if "=" in word and not word.startswith(("=", "-")) and word.split("=", 1)[0].replace("_", "").isalnum():
            i += 1
            continue
        name = PurePosixPath(word).name
        if name in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or words[i].replace(".", "").isdigit()
                                      or words[i][:-1].replace(".", "").isdigit()):
                takes_arg = name == "sudo" and words[i] in {"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t"}
                i += 2 if takes_arg else 1
            continue
        if name in {"bash", "sh", "zsh"} and i + 2 < len(words) and words[i + 1] in {"-c", "-lc"}:
            return cmd_verb(words[i + 2])
        if name == "uv" and i + 1 < len(words) and words[i + 1] == "run":
            i += 2
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in {"--with", "--python", "--project", "--directory"} else 1
            continue
        if name in SKIP_VERBS:
            return None
        return name[:64] or None
    return None


SSH_ARG_OPTS = set("bcDEeFIiJLlmOopQRSWw")
REMOTE_PATH = re.compile(r"^(?:[^@/\s]+@)?(\[[^\]]+\]|[A-Za-z0-9][A-Za-z0-9._-]*):")


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command.replace("\n", "\n;"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError:
        return None


HOST_OK = re.compile(r"^[a-z0-9][a-z0-9._:\[\]-]*$")


def _host(value: str) -> str | None:
    if any(ch in value for ch in "$`{}*?"):
        return None  # unexpanded variables and globs are not hosts
    value = value.removeprefix("ssh://").split("@")[-1]
    if value.startswith("["):
        value = value[1:].split("]")[0]
    elif value.count(":") == 1:
        value = value.split(":")[0]
    value = value.strip().lower()
    return value[:120] if len(value) >= 2 and HOST_OK.match(value) else None


def ssh_target(command: Any) -> tuple[str, str | None] | None:
    """(remote host, remote command verb) for ssh / tailscale ssh / mosh / scp / rsync, else None.

    Host is the alias or address as typed, without user@ or port, lower case; the verb is cmd_verb of
    the remote command (None for scp/rsync or an interactive login). Scans every segment, so
    `cd x && ssh h cmd` and `echo | ssh h 'bash -s'` are found. Never returns arguments or paths.
    """
    if isinstance(command, list):
        words = [w for w in command if isinstance(w, str)]
        if len(words) >= 3 and PurePosixPath(words[0]).name in {"bash", "sh", "zsh"} and words[1] in {"-c", "-lc"}:
            return ssh_target(words[2])
        command = shlex.join(words)
    if not isinstance(command, str) or not command.strip():
        return None
    tokens = _tokens(command)
    if tokens is None:
        return None
    segment: list[str] = []
    for token in tokens + [";"]:
        if token in OPERATORS or set(token) <= set(";&|()"):
            found = _segment_target(segment)
            if found:
                return found
            segment = []
        else:
            segment.append(token)
    return None


def _segment_target(words: list[str]) -> tuple[str, str | None] | None:
    i = 0
    while i < len(words):
        word = words[i]
        if "=" in word and not word.startswith(("=", "-")) and word.split("=", 1)[0].replace("_", "").isalnum():
            i += 1
            continue
        name = PurePosixPath(word).name
        if name in WRAPPERS:
            i += 1
            # wrapper flags, their arguments and numeric/duration arguments (timeout 10, nice -n 5)
            while i < len(words) and (words[i].startswith("-") or words[i].replace(".", "").isdigit()
                                      or words[i][:-1].replace(".", "").isdigit()):
                takes_arg = ((name == "sudo" and words[i] in {"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t"})
                             or (name in {"timeout", "gtimeout"} and words[i] in {"-k", "-s"})
                             or (name == "env" and words[i] in {"-u", "-C"}))
                i += 2 if takes_arg else 1
            continue
        if name in {"bash", "sh", "zsh"} and i + 2 < len(words) and words[i + 1] in {"-c", "-lc"}:
            return ssh_target(words[i + 2])
        if name == "tailscale" and i + 1 < len(words) and words[i + 1] == "ssh":
            return _ssh_args(words[i + 2:])
        if name in {"ssh", "mosh"}:
            return _ssh_args(words[i + 1:])
        if name in {"scp", "rsync", "sftp"}:
            for arg in words[i + 1:]:
                if arg.startswith("-"):
                    continue
                m = REMOTE_PATH.match(arg)
                if m and not arg.startswith(("/", "./", "~")) and len(m.group(1)) >= 2:
                    host = _host(m.group(1))
                    return (host, None) if host else None
            return None
        return None
    return None


def _ssh_args(args: list[str]) -> tuple[str, str | None] | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("-") and len(arg) > 1:
            # a flag cluster (-ti) whose last flag takes an argument consumes the next word; an
            # attached value (-p2222, -oX=y) does not
            takes = arg[1:].isalpha() and arg[-1] in SSH_ARG_OPTS
            i += 2 if takes else 1
            continue
        break
    if i >= len(args):
        return None
    host = _host(args[i])
    if not host:
        return None
    rest = args[i + 1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    verb = cmd_verb(" ".join(rest) if len(rest) > 1 else rest[0]) if rest else None
    return host, verb


def git_from_output(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]]]:
    """(commits [(branch, sha)], pushes [(old, new, src, dst)]) found in command output."""
    if not isinstance(text, str) or not text:
        return [], []
    commits = [(m.group(1), m.group(2)) for m in GIT_COMMIT_LINE.finditer(text)]
    pushes = [(m.group(1), m.group(2), m.group(3), m.group(4)) for m in GIT_PUSH_RANGE.finditer(text)]
    return commits, pushes


def mcp_split(tool_name: str) -> tuple[str | None, str | None]:
    """`mcp__<server>__<tool>` -> (server, tool). Server names may contain single underscores."""
    if not tool_name.startswith("mcp__"):
        return None, None
    rest = tool_name[5:]
    server, sep, tool = rest.rpartition("__")
    if not sep:
        return rest or None, None
    return server or None, tool or None


# --- parser v4: prompt origin (shared by both parsers; lanes do not edit this block) -------------

LAUNCH_PHRASE = re.compile(r"\bYou are the (?:campaign |loop |wave )?root\b", re.I)
# a launch pasted as a bare path: the whole message is one launch-*.txt|md path (optionally in backticks)
BARE_LAUNCH_PATH = re.compile(r"^\s*`?\s*\S*launch-[\w.-]+\.(?:txt|md)\s*`?\s*$", re.I)
PASTED_MARKERS = ("<pasted_content", "[Pasted text #")


def prompt_origin(text: str) -> str:
    """Origin of genuine owner text: launch_message | pasted | typed. Callers set slash_command,
    skill, local_command etc. themselves; this only separates the three kinds of typed prompt."""
    if LAUNCH_PHRASE.search(text[:8000]) or BARE_LAUNCH_PATH.match(text):
        return "launch_message"
    if any(marker in text for marker in PASTED_MARKERS):
        return "pasted"
    return "typed"
