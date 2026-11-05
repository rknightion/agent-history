"""Conservative source-tail classification never consumes or rewrites transcript bytes."""

import pytest

from agent_history import load


@pytest.mark.parametrize(
    "tail,expected", [(b"\0" * 73, 73), (b'{"type":', 0), (b"{}", 0), (b"\0x", 0), (b"\0\n", 0), (b"", 0)]
)
def test_only_stable_nul_only_tail_is_classified(tmp_path, tail, expected):
    p = tmp_path / "source.jsonl"
    prefix = b'{"type":"synthetic"}\n'
    original = prefix + tail
    p.write_bytes(original)
    observed = p.stat()
    assert load.nul_tail_bytes(p, len(prefix), observed) == expected
    assert p.read_bytes() == original
    assert list(load.read_batches(p, 0, 0))[0][0][0] == prefix


def test_changed_or_oversized_tail_is_not_classified(tmp_path):
    p = tmp_path / "source.jsonl"
    p.write_bytes(b"\0" * 20)
    observed = p.stat()
    p.write_bytes(b"\0" * 21)
    assert load.nul_tail_bytes(p, 0, observed) == 0
    p.write_bytes(b"\0" * (load.NUL_TAIL_LIMIT + 1))
    assert load.nul_tail_bytes(p, 0, p.stat()) == 0


def test_replaced_file_does_not_inherit_tail_classification(tmp_path):
    p = tmp_path / "source.jsonl"
    p.write_bytes(b"\0" * 20)
    observed = p.stat()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"\0" * 20)
    replacement.replace(p)
    assert load.nul_tail_bytes(p, 0, observed) == 0
