"""Verify or regenerate the migration ledger appended to the schema-only baseline dump.

The pg_dump schema body is retained byte for byte. This is not a schema dumper:
when squashing schema changes, produce the schema-only dump separately first.
020 is deliberately recorded without inserting its optional list-price data.
021 and 022-025 still run on fresh databases; all migrations still run on upgrades
unless their ledger entry already exists.
"""

from __future__ import annotations

import argparse
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "src" / "agent_history" / "sql"
MARKER = "-- Seed data omitted by the schema-only dump.\n"
# The dump already contains 015-019's schema. 020's data is explicit opt-in.
RECORDED = (
    "015_preserve_journal_links.sql",
    "016_tool_output_offset.sql",
    "017_pi_artifact_evidence.sql",
    "018_git_owner_alias.sql",
    "019_message_record_origin.sql",
    "020_gpt_6_1_sol_pricing.sql",
)
REPLAYED = (
    "021_drop_git_owner_alias.sql",
    "022_live_loops.sql",
    "023_loop_identity.sql",
    "024_loop_progress.sql",
    "025_loop_receipts.sql",
)


def render(sql_dir: Path) -> str:
    """Fail closed if a new migration has not been classified for fresh init."""
    actual = {path.name for path in (sql_dir / "migrations").glob("*.sql")}
    expected = set(RECORDED + REPLAYED)
    if actual != expected:
        raise ValueError(
            f"classify migrations first: missing={sorted(expected - actual)}, new={sorted(actual - expected)}"
        )
    original = (sql_dir / "baseline.sql").read_text()
    if original.count(MARKER) != 1:
        raise ValueError("baseline must contain exactly one seed-data boundary")
    schema, _ = original.split(MARKER)
    keys = ("chunks_reset_at", *(f"migration:{name}" for name in RECORDED))
    return (
        schema
        + MARKER
        + "".join(
            f"INSERT INTO ah.meta (key, value) VALUES ('{key}', now()::text) ON CONFLICT (key) DO NOTHING;\n"
            for key in keys
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate only the baseline's seed-data trailer")
    args = parser.parse_args()
    try:
        generated = render(SQL_DIR)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    baseline = SQL_DIR / "baseline.sql"
    if args.write:
        baseline.write_text(generated)
        print("baseline migration ledger regenerated; schema dump retained")
    elif baseline.read_text() != generated:
        parser.exit(1, "baseline migration ledger is stale; run just gen-baseline-ledger\n")
    else:
        print("baseline migration ledger matches classified migrations")


if __name__ == "__main__":
    main()
