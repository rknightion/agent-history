#!/usr/bin/env python3
"""Tests for bin/leak-scan and bin/leak-terms-hash. Stdlib unittest only.

Run from the repository root: `python3 -m unittest discover -s bin -p 'test_leak_scan.py'`.
Every term here is synthetic. Shapes the public patterns refuse (private addresses) are built at
run time so this file never carries one in clear.
"""
from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

BIN = Path(__file__).resolve().parent
SCAN = BIN / "leak-scan"
HASH = BIN / "leak-terms-hash"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TERMS = "\n".join([
    "# synthetic private list",
    "customer#1\tlit:Zebracorp Holdings",
    "host#1\tlit:vault-7.quillorchard.example",
    "email#1\tlit:marlow.fenwick@quillorchard.example",
    "id#1\tlit:4471902",
    "task#1\tre:\\bQRX-\\d{4}\\b",
]) + "\n"

PUBLIC = "\n".join([
    "# generic shapes",
    "rfc1918\tre:\\b10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\b",
]) + "\n"


def private_ip() -> str:
    return ".".join(["10", "20", "30", "40"])


class Repo:
    """A throwaway repository root carrying the scanner's committed config files."""

    def __init__(self, tmp: Path, terms: str | None = TERMS):
        self.root = tmp / "repo"
        self.root.mkdir()
        (self.root / "bin").mkdir()
        shutil.copy2(SCAN, self.root / "bin/leak-scan")
        (self.root / "leak-patterns.public.txt").write_text(PUBLIC)
        (self.root / "leak-scan.repo").write_text("demo\n")
        (self.root / "leak-scan.binary-allow").write_text("# globs\nassets/*.png\n")
        self.config = tmp / "xdg"
        (self.config / "leak-scan").mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("LEAK_")}
        self.env["XDG_CONFIG_HOME"] = str(self.config)
        self.env["HOME"] = str(tmp / "home")
        self.env["GIT_CONFIG_GLOBAL"] = str(tmp / "gitconfig")
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        if terms is not None:
            (self.config / "leak-scan/demo.txt").write_text(terms)

    def write(self, rel: str, data) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data)
        return path

    def scan(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / "bin/leak-scan"), *args], cwd=self.root,
                              env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=120)

    def git(self, *args: str, env: dict | None = None) -> str:
        base = {**self.env, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        return subprocess.run(["git", "-C", str(self.root), *args], env={**base, **(env or {})}, check=True,
                              capture_output=True, text=True).stdout

    def init(self) -> None:
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")


class LeakScanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="leak-scan-test-")
        self.tmp = Path(self._tmp.name)
        self.repo = Repo(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assertClean(self, proc: subprocess.CompletedProcess) -> None:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def assertHit(self, proc: subprocess.CompletedProcess, *fragments: str) -> None:
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        for fragment in fragments:
            self.assertIn(fragment, proc.stdout)


class FailClosed(LeakScanTestCase):
    def test_no_term_source_exits_2(self):
        (self.repo.config / "leak-scan/demo.txt").unlink()
        self.repo.write("a.txt", "hello\n")
        proc = self.repo.scan("--path", ".")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no private term source", proc.stderr)

    def test_empty_list_exits_2(self):
        self.repo.write("a.txt", "hello\n")
        proc = self.repo.scan("--path", ".", env={"LEAK_TERMS": "# only a comment\n\n"})
        self.assertEqual(proc.returncode, 2)

    def test_regex_compile_error_and_posix_class_exit_2(self):
        self.repo.write("a.txt", "hello\n")
        for bad in ("x\tre:(unclosed", "x\tre:[[:alpha:]]"):
            proc = self.repo.scan("--path", ".", env={"LEAK_TERMS": bad + "\n"})
            self.assertEqual(proc.returncode, 2, bad)
            self.assertNotIn("unclosed", proc.stdout + proc.stderr)

    def test_malformed_line_exits_2_without_echoing_it(self):
        self.repo.write("a.txt", "hello\n")
        proc = self.repo.scan("--path", ".", env={"LEAK_TERMS": "Zebracorp without a tab\n"})
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Zebracorp", proc.stdout + proc.stderr)

    def test_zero_files_exits_2(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        proc = self.repo.scan("--path", str(empty))
        self.assertEqual(proc.returncode, 2)

    def test_missing_public_patterns_exit_2(self):
        (self.repo.root / "leak-patterns.public.txt").unlink()
        self.repo.write("a.txt", "hello\n")
        self.assertEqual(self.repo.scan("--path", ".").returncode, 2)

    def test_unexpected_exception_is_generic(self):
        module = load("leak_scan_exc", SCAN)

        def boom(*_args, **_kwargs):
            raise RuntimeError("Zebracorp Holdings secret detail")

        module.load_terms = boom
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(["--path", str(self.repo.root), "--root", str(self.repo.root)])
        self.assertEqual(code, 2)
        self.assertNotIn("Zebracorp", out.getvalue() + err.getvalue())
        self.assertIn("internal error", err.getvalue())

    def test_shallow_repository_history_exits_2(self):
        self.repo.init()
        self.repo.write("a.txt", "one\n")
        self.repo.git("add", "a.txt")
        self.repo.git("commit", "-q", "-m", "one")
        self.repo.write("a.txt", "two\n")
        self.repo.git("commit", "-q", "-am", "two")
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo.root}", str(clone)], check=True,
                       env=self.repo.env, capture_output=True)
        proc = subprocess.run([sys.executable, str(self.repo.root / "bin/leak-scan"), "--history",
                               "--root", str(self.repo.root)], cwd=clone, env=self.repo.env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("shallow", proc.stderr)


class Matching(LeakScanTestCase):
    def test_hit_prints_path_line_label_only(self):
        self.repo.write("docs/notes.md", "intro\nwe met Zebracorp Holdings today\n")
        proc = self.repo.scan("--path", ".")
        self.assertHit(proc, "docs/notes.md:2: customer#1")
        self.assertNotIn("Zebracorp", proc.stdout + proc.stderr)
        self.assertNotIn("we met", proc.stdout + proc.stderr)

    def test_every_occurrence_is_reported(self):
        self.repo.write("a.txt", "Zebracorp Holdings\nclean\nzebracorp holdings again\n")
        self.assertHit(self.repo.scan("--path", "."), "a.txt:1: customer#1", "a.txt:3: customer#1")

    def test_clean_tree_reports_file_count(self):
        self.repo.write("a.txt", "nothing to see\n")
        proc = self.repo.scan("--path", ".")
        self.assertClean(proc)
        self.assertRegex(proc.stdout, r"clean \(\d+ files\)")

    def test_literal_with_punctuation_matches_on_normalised_tokens(self):
        self.repo.write("a.cfg", "url = https://Vault-7.QuillOrchard.example/v1\n")
        self.repo.write("b.cfg", "contact: Marlow.Fenwick@QuillOrchard.example\n")
        self.repo.write("c.cfg", "tenant4471902\n")
        proc = self.repo.scan("--path", ".")
        self.assertHit(proc, "a.cfg:1: host#1", "b.cfg:1: email#1", "c.cfg:1: id#1")

    def test_literal_does_not_match_inside_a_longer_number(self):
        self.repo.write("a.txt", "build 144719023 ok\n")
        self.assertClean(self.repo.scan("--path", "."))

    def test_regex_terms_are_case_insensitive(self):
        self.repo.write("a.txt", "see qrx-1234\n")
        self.assertHit(self.repo.scan("--path", "."), "a.txt:1: task#1")

    def test_normalised_pass_defeats_zero_width_fullwidth_and_line_splits(self):
        self.repo.write("zw.txt", "Zebra\u200bcorp Holdings\n")
        self.repo.write("fw.txt", "\uff3aebracorp holdings\n")
        self.repo.write("split.txt", "first Zebracorp\nHoldings second\n")
        self.repo.write("glued.txt", "ZEBRACORP-holdings\n")
        proc = self.repo.scan("--path", ".")
        self.assertHit(proc, "zw.txt:1: customer#1", "fw.txt:1: customer#1", "split.txt:1: customer#1",
                       "glued.txt:1: customer#1")

    def test_regexes_also_run_on_the_zero_width_stripped_line(self):
        self.repo.write("a.txt", "see QRX-12\u200b34\n")
        self.assertHit(self.repo.scan("--path", "."), "a.txt:1: task#1")

    def test_base64_encoded_term_is_found(self):
        encoded = base64.b64encode(b"note: Zebracorp Holdings owns this").decode()
        self.repo.write("blob.txt", f"data = {encoded}\n")
        self.assertHit(self.repo.scan("--path", "."), "blob.txt:1: customer#1 (base64)")

    def test_file_names_and_dotfiles_are_scanned_and_vendored_dirs_skipped(self):
        self.repo.write("reports/zebracorp-holdings.md", "clean\n")
        self.repo.write(".env.example", "HOST=vault-7.quillorchard.example\n")
        self.repo.write("node_modules/pkg/index.js", "Zebracorp Holdings\n")
        self.repo.write(".git-not-a-repo/x", "clean\n")
        proc = self.repo.scan("--path", ".")
        self.assertHit(proc, "reports/zebracorp-holdings.md:name: customer#1", ".env.example:1: host#1")
        self.assertNotIn("node_modules", proc.stdout)

    def test_public_patterns_always_apply(self):
        self.repo.write("net.txt", f"gateway {private_ip()}\n")
        self.assertHit(self.repo.scan("--path", "."), "net.txt:1: rfc1918")

    def test_binary_file_refused_unless_allowlisted(self):
        self.repo.write("assets/logo.png", b"\x89PNG\x00\x01binary")
        self.assertClean(self.repo.scan("--path", "."))
        self.repo.write("dist/blob.bin", b"\x00\x01\x02")
        self.assertHit(self.repo.scan("--path", "."), "dist/blob.bin:0: binary-not-allowed")


class Sources(LeakScanTestCase):
    def test_env_terms_win_over_file_and_default(self):
        self.repo.write("a.txt", "Zebracorp Holdings\nonly-in-env-term\n")
        proc = self.repo.scan("--path", ".", env={"LEAK_TERMS": "envterm\tlit:only-in-env-term\n"})
        self.assertHit(proc, "a.txt:2: envterm")
        self.assertNotIn("customer#1", proc.stdout)

    def test_terms_file_wins_over_default(self):
        terms = self.tmp / "t.txt"
        terms.write_text("filterm\tlit:only-in-env-term\n")
        self.repo.write("a.txt", "Zebracorp Holdings\nonly-in-env-term\n")
        proc = self.repo.scan("--path", ".", env={"LEAK_TERMS_FILE": str(terms)})
        self.assertHit(proc, "a.txt:2: filterm")
        self.assertNotIn("customer#1", proc.stdout)

    def test_public_only_warns_and_skips_private_terms(self):
        (self.repo.config / "leak-scan/demo.txt").unlink()
        self.repo.write("a.txt", "Zebracorp Holdings\n")
        proc = self.repo.scan("--path", ".", "--public-only")
        self.assertClean(proc)
        self.assertIn("PUBLIC PATTERNS ONLY", proc.stderr)

    def test_hashed_terms_match_like_their_literals(self):
        hashed = subprocess.run([sys.executable, str(HASH), "--salt", "s4lt"], input=TERMS, capture_output=True,
                                text=True, check=True).stdout
        self.assertNotIn("Zebracorp", hashed)
        self.assertNotIn("quillorchard", hashed.lower())
        self.assertIn("re:\\bQRX-\\d{4}\\b", hashed)
        self.assertIn("customer#1\th:s4lt:", hashed)
        self.repo.write("a.txt", "Zebracorp  Holdings\nvault-7.quillorchard.example\nqrx-0001\n")
        self.repo.write("b.txt", "MARLOW.FENWICK@quillorchard.example\nid 4471902\n")
        proc = self.repo.scan("--path", ".", env={"LEAK_TERMS": hashed})
        self.assertHit(proc, "a.txt:1: customer#1", "a.txt:2: host#1", "a.txt:3: task#1", "b.txt:1: email#1",
                       "b.txt:2: id#1")

    def test_hash_helper_refuses_a_literal_it_cannot_find(self):
        proc = subprocess.run([sys.executable, str(HASH)], input="x\tlit:one two three four five six\n",
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("three", proc.stdout + proc.stderr)
        proc = subprocess.run([sys.executable, str(HASH)], input="x\tlit:   \n", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


class GitModes(LeakScanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.init()
        self.repo.write("a.txt", "clean\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "init")

    def test_history_finds_content_removed_later(self):
        self.repo.write("old.txt", "Zebracorp Holdings\n")
        self.repo.git("add", "old.txt")
        self.repo.git("commit", "-q", "-m", "add")
        self.repo.git("rm", "-q", "old.txt")
        self.repo.git("commit", "-q", "-m", "remove")
        proc = self.repo.scan("--history")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"old\.txt@[0-9a-f]{12}:1: customer#1")
        self.assertNotIn("Zebracorp", proc.stdout + proc.stderr)

    def test_history_checks_author_committer_and_message(self):
        self.repo.write("b.txt", "b\n")
        self.repo.git("add", "b.txt")
        self.repo.git("commit", "-q", "-m", "b", env={"GIT_COMMITTER_EMAIL": "marlow.fenwick@quillorchard.example"})
        proc = self.repo.scan("--history")
        self.assertHit(proc, "commit metadata: email#1")
        self.repo.git("commit", "-q", "--allow-empty", "-m", "fix QRX-1234")
        self.assertHit(self.repo.scan("--history"), "commit metadata: task#1")

    def test_history_checks_path_names_and_tags(self):
        self.repo.git("tag", "-a", "v1", "-m", "release for Zebracorp Holdings")
        self.assertHit(self.repo.scan("--history"), "tag v1: customer#1")
        self.repo.git("tag", "-d", "v1")
        self.repo.write("zebracorp-holdings/readme", "x\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "dir")
        self.repo.git("rm", "-rq", "zebracorp-holdings")
        self.repo.git("commit", "-q", "-m", "rm")
        self.assertHit(self.repo.scan("--history"), "zebracorp-holdings/readme:name: customer#1")

    def test_history_clean_reports_counts(self):
        proc = self.repo.scan("--history")
        self.assertClean(proc)
        self.assertRegex(proc.stdout, r"clean \(1 commits, \d+ blobs\)")

    def test_staged_scans_the_index_not_the_worktree(self):
        self.repo.write("c.txt", "Zebracorp Holdings\n")
        self.repo.git("add", "c.txt")
        self.repo.write("c.txt", "clean now but not staged\n")
        self.assertHit(self.repo.scan("--staged"), "c.txt:1: customer#1")

    def test_staged_with_nothing_staged_is_clean(self):
        proc = self.repo.scan("--staged")
        self.assertClean(proc)
        self.assertIn("nothing staged", proc.stdout)

    def test_message_mode(self):
        msg = self.tmp / "MSG"
        msg.write_text("subject\n\nfor Zebracorp Holdings\n")
        self.assertHit(self.repo.scan("--message", str(msg)), "commit message:3: customer#1")
        msg.write_text("subject only\n")
        self.assertClean(self.repo.scan("--message", str(msg)))


if __name__ == "__main__":
    unittest.main()
