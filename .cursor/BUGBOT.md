# Review the agent session catalogue

## Software and operators

Operators use the catalogue to read session and event history, report telemetry, and diagnose
collection and archive failures. Trace parsing, storage, readers, and background work under
`src/agent_history/` and preserve the public contracts in `CONTRACT.md`.

## Invariants

Preserve stable event identity, incremental processing order, and historical catalogue reads.
Keep parser and loader fidelity and apply presentation rules at the documented boundary.
Preserve the rebuild marker through incomplete loads and keep readers drained during a replacement.

## Sensitive changes

Use synthetic session and service fixtures. Review schema and reader changes together, including
the migration ledger and compatibility of retained data. Keep source identifiers, credentials and
real transcripts out of public files and review notes. Keep alert and dashboard identities stable,
and review the Python generators in `grafana/` for behavior changes.

## Checks already in CI

`just check` covers formatting, lint, generated drift, local tests and the leak gate. Preserve the
existing hook and CI checks. Focus findings on behavioral gaps beyond those checks.

## Verification

Run `just check </dev/null` with the repository's scoped leak scanner. Use disposable PostgreSQL
and the database-backed checks in `just ci` when reviewing reader fences, rebuilds and migrations.
Exercise failure and resume paths and record which local checks and database cases ran.
