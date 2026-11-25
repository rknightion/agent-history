"""Repository scripts start on Python 3.15, never on whatever python3 an older host has first.

bin/leak-scan, bin/leak-terms-hash, bin/test_leak_scan.py and hooks/ are shared verbatim with another
repository and stay version-agnostic on purpose: the git hooks must keep scanning where 3.15 is absent.
The hooks prefer 3.15 and fall back to python3.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHARED = {"bin/leak-scan", "bin/leak-terms-hash", "bin/test_leak_scan.py"}
HOOKS = ("pre-commit", "commit-msg", "pre-push")


def tracked_shebangs():
    names = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.split("\0")
    for name in filter(None, names):
        path = REPO / name
        if not path.is_file() or name in SHARED or name.startswith("tests/fixtures/"):
            continue
        with path.open("rb") as handle:
            first = handle.readline().decode("utf-8", "replace").rstrip("\n")
        if first.startswith("#!") and "python" in first:
            yield name, first


def test_python_scripts_name_python_3_15():
    wrong = {name: first for name, first in tracked_shebangs() if first != "#!/usr/bin/env python3.15"}
    assert not wrong, f"these must start with #!/usr/bin/env python3.15: {wrong}"


def test_justfile_never_runs_the_bare_python3_except_for_the_shared_scan():
    bare = [
        line.strip()
        for line in (REPO / "justfile").read_text().splitlines()
        if "python3" in line and "leak" not in line and not line.lstrip().startswith("#")
    ]
    assert not bare, f"the justfile must call {{{{ python }}}}, not python3: {bare}"


def test_justfile_python_is_3_15():
    out = subprocess.run(
        ["just", "--evaluate", "python"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": __import__("os").environ["PATH"]},
    ).stdout
    assert "--python 3.15" in out


def test_git_hooks_prefer_python_3_15_and_fall_back():
    for name in HOOKS:
        text = (REPO / "hooks" / name).read_text()
        assert "python3.15" in text and "/opt/homebrew/bin/python3.15" in text, name
        assert "py=$(on_path python3 || true)" in text, f"{name} must still fall back to python3"
