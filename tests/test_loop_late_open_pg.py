"""Late opens qualify valid prefix observations, never corrupt-prefix identity."""

from datetime import timedelta

import pytest

from agent_history import load
from test_loop_outcomes import AT, encoded, framed
from test_loop_outcomes_pg import collect, launch, surface
import test_loops_pg as helpers

clean = helpers.clean
conn = helpers.conn
pytestmark = helpers.pytestmark


def late_open_rows(*, close=True):
    rows = framed(
        {"ev": "admit", "task": "EX-0001", "source": "envelope", "owned": ["src"], "accept": "proof"},
        {"ev": "park", "task": "EX-0001", "reason": "decision", "needs": "owner"},
    )
    rows = [rows[1], rows[2], rows[0]]
    if close:
        rows.append(framed({"ev": "close", "reason": "owner-stop"})[1])
    for n, row in enumerate(rows, 1):
        row.update(seq=n, ts=(AT + timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return rows


@pytest.mark.parametrize("case", ["closed", "no-open", "pre-open-close", "worker-close", "before-launch"])
def test_late_open_public_outcomes(clean, tmp_path, case):
    launch(clean, tmp_path)
    rows = late_open_rows()
    expected = ("finished", 1, 0, 1, 0)
    if case == "no-open":
        rows.pop(2)
        rows[-1]["seq"] = 3
        expected = ("stale", None, None, None, None)
    elif case == "pre-open-close":
        rows[2], rows[3] = rows[3], rows[2]
        for n, row in enumerate(rows, 1):
            row.update(seq=n, ts=(AT + timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        expected = ("stale", 1, 0, 1, 0)
    elif case == "worker-close":
        rows[-1]["by"] = "ext"
        expected = ("stale", 1, 0, 1, 0)
    elif case == "before-launch":
        for n, row in enumerate(rows[:2]):
            row["ts"] = (AT - timedelta(seconds=2 - n)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expected = ("finished", 0, 0, 0, 0)
    collect(clean, tmp_path, encoded(rows), copies=2)
    load.post_passes(clean)
    assert surface(clean) == [expected]
    if case in ("pre-open-close", "worker-close", "no-open"):
        assert clean.execute("SELECT end_evidence FROM ah.loop_run JOIN ah.loops USING (launch_uid)").fetchone() != (
            "state_close",
        )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("corruption", ["schema", "sequence", "duplicate-member"])
def test_late_open_invalid_copy_withholds_all_proof(clean, tmp_path, reverse, corruption):
    launch(clean, tmp_path)
    rows = late_open_rows()
    valid = encoded(rows)
    bad = dict(rows[-1])
    if corruption == "schema":
        bad["unexpected"] = True
    elif corruption == "sequence":
        bad["seq"] += 1
    invalid = encoded(rows[:-1]) + encoded([bad])
    if corruption == "duplicate-member":
        invalid = encoded(rows[:-1]) + encoded([bad]).replace('"seq": 4', '"seq": 4, "seq": 4')
    left, right = (invalid, valid) if reverse else (valid, invalid)
    collect(clean, tmp_path, left)
    clean.execute(
        "INSERT INTO ah.loop_state (machine,path,loop,repo_origin,content,state_mtime) "
        "VALUES ('synthetic-copy','/tmp/synthetic/state','loop1','example/project',%s,now())",
        (right,),
    )
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]
    assert clean.execute("SELECT end_evidence FROM ah.loop_run JOIN ah.loops USING (launch_uid)").fetchone() != (
        "state_close",
    )


@pytest.mark.parametrize("case", ["bad-prefix", "bad-open"])
def test_late_open_identity_after_corruption_is_not_attributed(clean, tmp_path, case):
    launch(clean, tmp_path)
    rows = late_open_rows()
    if case == "bad-prefix":
        rows[0].pop("accept")
    else:
        rows[2]["unexpected"] = True
    collect(clean, tmp_path, encoded(rows))
    # A post-corruption goal must not poison an independently valid open/copy.
    clean.execute(
        "INSERT INTO ah.loop_state (machine,path,loop,repo_origin,content,state_mtime) "
        "VALUES ('synthetic-valid','/tmp/synthetic/state','loop1','example/project',%s,now())",
        (encoded(framed({"ev": "close", "reason": "owner-stop"})),),
    )
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("finished", 0, 0, 0, 0)]
