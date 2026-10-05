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


# Reserved harness wrappers, not arbitrary XML supplied by a person. Unwrapped instruction
# headings and unknown tags have no reliable end boundary and are deliberately not inferred.
PROMPT_INJECTION_CLASSES = {
    "system-reminder": "system_reminder",
    "local-command-caveat": "system_reminder",
    "environment_context": "context_injection",
    "instructions": "context_injection",
    "user_instructions": "context_injection",
    "permissions": "context_injection",
    "skill": "skill_body",
    "hook_prompt": "hook_output",
    "turn_aborted": "interrupt_marker",
    "subagent_notification": "agent_message",
    "task-notification": "agent_message",
    "local-command-stdout": "local_command_output",
    "local-command-stderr": "local_command_output",
    "bash-stdout": "local_command_output",
    "bash-stderr": "local_command_output",
}
PROMPT_TAG = re.compile(r"</?(?P<tag>" + "|".join(map(re.escape, PROMPT_INJECTION_CLASSES)) + r")(?:\s+[^<>]*?)?>")
PROMPT_FENCE = re.compile(r"(?m)^ {0,3}(`{3,}|~{3,})([^\n]*)$")
PROMPT_QUOTE = re.compile(r"(?m)^ {0,3}>[^\n]*$")


def split_prompt_injections(text: str) -> tuple[str, list[tuple[str, str, str, int, int]]]:
    """Separate complete reserved wrapper blocks without discarding a character.

    Only block-position openers (line start, or immediately after another wrapper) count.
    Markdown code fences, inline mentions, unknown wrappers and incomplete wrappers remain
    user text. Same-tag nesting is balanced; nested content keeps the outer wrapper's class.
    The human remainder stays one message, retaining its original key and prompt count.
    Injection offsets are character offsets in the parser's original text, not file bytes.
    """
    code: list[tuple[int, int]] = []
    fence: tuple[str, int, int] | None = None
    for match in PROMPT_FENCE.finditer(text):
        marker, rest = match.groups()
        if fence is None:
            fence = (marker[0], len(marker), match.start())
        elif marker[0] == fence[0] and len(marker) >= fence[1] and not rest.strip():
            code.append((fence[2], match.end()))
            fence = None
    if fence is not None:
        code.append((fence[2], len(text)))
    # Fenced and Markdown-quoted examples never define an enclosing wrapper's boundary.
    # Their bytes still inherit its class when that outer wrapper is complete.
    literal = code + [(match.start(), match.end()) for match in PROMPT_QUOTE.finditer(text)]
    tokens = [match for match in PROMPT_TAG.finditer(text)
              if not any(start <= match.start() < stop for start, stop in literal)]
    injections: list[tuple[str, str, str, int, int]] = []
    human: list[str] = []
    end = 0
    i = 0
    while i < len(tokens):
        match = tokens[i]
        start, tag = match.start(), match.group("tag")
        prefix = text[text.rfind("\n", 0, start) + 1 : start]
        if match.group().startswith("</") or (prefix.strip() and start != end):
            i += 1
            continue
        depth = 1
        j = i + 1
        while j < len(tokens):
            other = tokens[j]
            if other.group("tag") == tag:
                depth += -1 if other.group().startswith("</") else 1
                if depth == 0:
                    break
            j += 1
        if depth:
            i += 1
            continue
        stop = tokens[j].end()
        human.append(text[end:start])
        injections.append((PROMPT_INJECTION_CLASSES[tag], tag, text[start:stop], start, stop))
        end = stop
        i = j + 1
    human.append(text[end:])
    remainder = "".join(human)
    if injections and not remainder.strip():
        # Whitespace-only separators belong to the injected content, not a phantom human prompt.
        # Retain them in the adjacent wrapper's text, along with the corresponding offsets.
        expanded = []
        for n, (cls, tag, _, start, stop) in enumerate(injections):
            start = 0 if n == 0 else injections[n - 1][4]
            stop = len(text) if n == len(injections) - 1 else stop
            expanded.append((cls, tag, text[start:stop], start, stop))
        return "", expanded
    return remainder, injections


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


SHELL_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "time"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}


_CONTROL = ("&&", "||", ";;", "|&", ";", "&", "|", "(", ")")
_COMPOUND = {"if": "fi", "while": "done", "until": "done", "for": "done", "select": "done", "case": "esac"}
_RESERVED = {"then", "elif", "else", "fi", "do", "done", "esac", "in", "}"}
_STARTERS = {"then", "elif", "else", "do", "{", "!", "time", "if", "while", "until"}


class _ShellSyntax(Exception):
    """The command is not shell this reader can follow; it proves nothing."""


_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.-]+))")


def _strip_heredocs(command: str) -> str | None:
    """The command without here-document bodies, which are data rather than commands.

    A body starts after the first unquoted newline that follows its `<<WORD` and ends at a line that is
    exactly WORD (leading tabs ignored for `<<-`). None when a body never ends.
    """
    if "<<" not in command:
        return command
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote: str | None = None
    word_start = True
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and quote != "'":
            out.append(command[i:i + 2])
            i += 2
            word_start = False
            continue
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and word_start:
            end = command.find("\n", i)
            end = n if end < 0 else end
            out.append(command[i:end])
            i = end
            continue
        elif command.startswith("<<<", i):
            out.append("<<<")
            i += 3
            word_start = False
            continue
        elif command.startswith("<<", i) and (match := _HEREDOC.match(command, i)):
            word = next(g for g in match.group(2, 3, 4) if g is not None)
            pending.append((word, match.group(1) == "-"))
            out.append(match.group(0))
            i = match.end()
            word_start = False
            continue
        elif ch == "\n" and pending:
            out.append(ch)
            i += 1
            for word, tabs in pending:
                while True:
                    if i >= n:
                        return None
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if tabs else line) == word:
                        break
            pending = []
            word_start = True
            continue
        word_start = quote is None and ch in " \t\n;&|()<>"
        out.append(ch)
        i += 1
    return None if pending else "".join(out)


def _join_continuations(command: str) -> str:
    """Drop backslash-newline line continuations outside single quotes, as the shell does."""
    out: list[str] = []
    quoted = False
    i = 0
    while i < len(command):
        c = command[i]
        if c == "'" and not (i and command[i - 1] == "\\" and not quoted):
            quoted = not quoted
        elif c == "\\" and not quoted and command.startswith("\\\n", i):
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _shell_tokens(command: str) -> list[str] | None:
    """Shell words with control operators as their own tokens; redirections stay words."""
    stripped = _strip_heredocs(_join_continuations(command))
    tokens = _tokens(stripped) if stripped is not None else None
    if tokens is None:
        return None
    out: list[str] = []
    for token in tokens:
        if not token or not set(token) <= set("();<>|&"):
            out.append(token)
            continue
        i = 0
        while i < len(token):
            if token[i] in "<>" or token.startswith("&>", i):
                j = i + (2 if token.startswith("&>", i) else 1)
                while j < len(token) and token[j] in "<>&|":
                    j += 1
                out.append(token[i:j])
                i = j
                continue
            op = next(o for o in _CONTROL if token.startswith(o, i))
            out.append(op)
            i += len(op)
    return out


class _ShellReader:
    """A list/and-or/pipeline tree of one shell command, enough to tell which git calls its exit status proves.

    Nodes: list [(and_or, terminator)], and_or (pipelines, operators), pipeline (negated, pipefail, commands),
    command ("simple", words) | ("group", list) | ("opaque",). Keyword compounds (if, loops, case) and
    command substitutions are opaque: their exit status proves nothing about the commands inside.
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens, self.pos, self.pipefail = tokens, 0, False

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def expect(self, token: str) -> None:
        if self.peek() != token:
            raise _ShellSyntax(token)
        self.pos += 1

    def parse(self) -> list[Any]:
        items = self.list(None)
        if self.peek() is not None:
            raise _ShellSyntax(self.peek())
        return items

    def list(self, end: str | None) -> list[Any]:
        items: list[Any] = []
        while True:
            while self.peek() == ";":
                self.pos += 1
            token = self.peek()
            if token is None or token == end:
                return items
            and_or = self.and_or()
            terminator = self.peek() if self.peek() in {";", "&"} else None
            if terminator:
                self.pos += 1
            elif self.peek() not in {None, end}:
                raise _ShellSyntax(self.peek())
            items.append((and_or, terminator))

    def and_or(self) -> tuple[list[Any], list[str]]:
        pipelines, operators = [self.pipeline()], []
        while self.peek() in {"&&", "||"}:
            operators.append(self.tokens[self.pos])
            self.pos += 1
            while self.peek() == ";":   # a newline after the operator continues the command
                self.pos += 1
            pipelines.append(self.pipeline())
        return pipelines, operators

    def pipeline(self) -> tuple[bool, bool, list[Any]]:
        negated = False
        while self.peek() in {"!", "time", "-p"}:
            negated = negated or self.peek() == "!"
            self.pos += 1
        pipefail, commands = self.pipefail, [self.command()]
        while self.peek() in {"|", "|&"}:
            self.pos += 1
            while self.peek() == ";":
                self.pos += 1
            commands.append(self.command())
        return negated, pipefail, commands

    def command(self) -> Any:
        token = self.peek()
        if token is None or token in _CONTROL and token != "(" or token in _RESERVED:
            raise _ShellSyntax(token)
        if token == "function" or self.tokens[self.pos + 1:self.pos + 3] == ["(", ")"]:
            # a function definition runs nothing: read its body for syntax, then forget it
            self.pos += 2 if token == "function" else 1
            if self.tokens[self.pos:self.pos + 2] == ["(", ")"]:
                self.pos += 2
            while self.peek() == ";":
                self.pos += 1
            saved = self.pipefail
            self.command()
            self.pipefail = saved
            return ("opaque",)
        if token == "(":
            self.pos += 1
            saved = self.pipefail   # a subshell's options end with it
            node: Any = ("group", self.list(")"))
            self.expect(")")
            self.pipefail = saved
        elif token == "{":
            self.pos += 1
            node = ("group", self.list("}"))
            self.expect("}")
        elif token in _COMPOUND:
            self.skip_compound()
            node = ("opaque",)
        else:
            return self.simple()
        while self.peek() is not None and self.peek() not in _CONTROL:
            self.pos += 1   # redirections after a group or compound
        return node

    def simple(self) -> tuple[str, list[str]]:
        words: list[str] = []
        while self.peek() is not None and (self.peek() not in _CONTROL or self.peek() == "("):
            if self.peek() == "(":   # $( ) or <( ): never proven by this command's status
                self.pos += 1
                saved = self.pipefail
                self.list(")")
                self.expect(")")
                self.pipefail = saved
                continue
            words.append(self.tokens[self.pos])
            self.pos += 1
        if words and words[0] == "set" and _PIPEFAIL_RE.search(" ".join(words)):
            self.pipefail = True
        return "simple", words

    def skip_compound(self) -> None:
        stack = [_COMPOUND[self.tokens[self.pos]]]
        self.pos += 1
        previous = "if"
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            self.pos += 1
            at_start = previous in _CONTROL or previous in _STARTERS
            previous = token
            if not at_start:
                continue
            if token in _COMPOUND:
                stack.append(_COMPOUND[token])
            elif token in {"fi", "done", "esac"}:
                if token != stack.pop():
                    raise _ShellSyntax(token)
                if not stack:
                    return
        raise _ShellSyntax("unterminated compound")


def _proven_ops(items: list[Any], proven: bool, ops: list[str]) -> None:
    """Append the git ops of `items` in order, keeping only those an exit status of 0 proves succeeded."""
    for index, ((pipelines, operators), terminator) in enumerate(items):
        # only the last item of a list sets its status, and a backgrounded item's status is always 0
        item_proven = proven and index == len(items) - 1 and terminator != "&"
        for k, (negated, pipefail, commands) in enumerate(pipelines):
            # status 0 shows this pipeline ran and passed only if it was reached by `&&` (or is first) and
            # nothing after it could replace a failure with success: every later operator is `&&`
            ran = k == 0 or operators[k - 1] == "&&"
            pipeline_proven = item_proven and ran and all(op == "&&" for op in operators[k:]) and not negated
            for j, node in enumerate(commands):
                # without pipefail a pipeline's status is its last command's
                node_proven = pipeline_proven and (j == len(commands) - 1 or pipefail)
                if node[0] == "group":
                    _proven_ops(node[1], node_proven, ops)
                elif node[0] == "simple":
                    op = _git_segment_op(node[1]) if node_proven else None
                    if op:
                        ops.append(op)


def _git_segment_op(words: list[str]) -> str | None:
    """The delivery op of one shell segment that runs git: commit | cherry_pick | push, else None."""
    i = 0
    while i < len(words):
        word = words[i]
        if word in SHELL_KEYWORDS:
            i += 1
        elif "=" in word and not word.startswith(("=", "-")) and word.split("=", 1)[0].replace("_", "").isalnum():
            i += 1
        elif PurePosixPath(word).name in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or words[i].replace(".", "").isdigit()):
                i += 1
        else:
            break
    if i >= len(words):
        return None
    if PurePosixPath(words[i]).name in {"bash", "sh", "zsh"} and i + 2 < len(words) and words[i + 1] in {"-c", "-lc"}:
        ops = git_ops_from_command(words[i + 2])
        return ops[0] if ops else None
    if PurePosixPath(words[i]).name != "git":
        return None
    i += 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in GIT_OPTS_WITH_VALUE else 1
    if i >= len(words):
        return None
    sub, flags = words[i], words[i + 1:]
    longs = {f for f in flags if f.startswith("--")}
    shorts = "".join(f[1:] for f in flags if f.startswith("-") and not f.startswith("--"))
    if "--help" in longs:
        return None
    if sub == "commit":
        return None if "--dry-run" in longs else "commit"
    if sub == "push":
        return None if longs & {"--dry-run", "--delete"} or "n" in shorts else "push"
    if sub == "cherry-pick":
        skip = {"--no-commit", "--abort", "--quit", "--skip"}
        return None if longs & skip or "n" in shorts else "cherry_pick"
    return None


_PIPEFAIL_RE = re.compile(r"(?:^|[;&|(\s])set\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*o\s+pipefail\b")


def git_ops_from_command(command: Any) -> list[str]:
    """Delivery ops a shell command would perform, one per git invocation, in order.

    `commit`, `cherry_pick` or `push`. Tolerates global options (`-C <dir>`, `-c k=v`), env
    assignments, wrappers and `bash -c`; only a segment whose executable is git counts, so a quoted
    mention such as `echo "git commit"` yields nothing. Dry runs, `--help` and cherry-pick
    `--no-commit`/`--abort` yield nothing. Command text only: the caller must also know the command
    succeeded, because a failed commit prints nothing a parser can tell apart. Only git calls whose
    success the command's exit status proves count: nothing later in the same list, or in any
    enclosing group or subshell, may absorb a failure (a later `||`, `;` or newline followed by another
    command, `&`, or a pipe without an earlier `set -o pipefail`), and none under `!`, inside a
    keyword compound (`if`, loops, `case`) or inside a command substitution. Text this reader cannot
    follow yields nothing.
    """
    if isinstance(command, list):
        words = [w for w in command if isinstance(w, str)]
        if len(words) >= 3 and PurePosixPath(words[0]).name in {"bash", "sh", "zsh"} and words[1] in {"-c", "-lc"}:
            return git_ops_from_command(words[2])
        command = shlex.join(words)
    if not isinstance(command, str) or not command.strip():
        return []
    tokens = _shell_tokens(command)
    if tokens is None:
        return []
    try:
        tree = _ShellReader(tokens).parse()
    except (_ShellSyntax, RecursionError):
        return []
    ops: list[str] = []
    _proven_ops(tree, True, ops)
    return ops


def git_event_extras(ops: list[str], commits_seen: int, pushes_seen: int) -> list[tuple[str, int]]:
    """Command-evidence git events not already covered by output matches: [(op, n)], n counting up
    per group. A command's commits and cherry-picks share one budget, so output that matched a
    commit line suppresses one command-derived commit rather than adding a second event for it."""
    commit_like = [op for op in ops if op != "push"]
    extras = [(op, n) for n, op in enumerate(commit_like) if n >= commits_seen]
    extras += [("push", n) for n in range(pushes_seen, sum(1 for op in ops if op == "push"))]
    return extras


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
