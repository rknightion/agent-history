# Loop task count precedence

`ah.loops.tasks_done` keeps its nullable bigint column and existing report/reader
shape. Its source precedence is separate from the state-only outcome columns.

1. A valid, exactly attributed **closed** collected v1 state log overrides the
   legacy Done-flip count. Count distinct exact tracker IDs on `land` events whose
   `by` is `root` or `dispatcher`, using the same publication projection as
   `tasks_landed`. Operational aliases, extension/daemon observations and a
   worker return's `landed` claim never add tasks. Duplicate events and matching
   machine copies do not inflate this total. A qualifying closed log with no
   qualifying lands establishes zero, even when the Done-flip source is unknown.
2. Otherwise the existing Backlog Done-flip fallback is unchanged: count distinct
   tasks in collected default-branch Done flips for the loop's repository within
   `[launch_ts, end_ts)`. The running window is open; the stale window ends at the
   root's last observed activity. The fallback does not filter by publishing
   session. An unknown repository or missing first Done scan supplies no known
   count; a completed scan with no qualifying flips supplies zero.

A valid open alone does **not** override `tasks_done`. Neither a receipt nor a
replacement launch substitutes for a collected state close. The close must pass
frozen event validation, exact origin/loop/open-goal and launch-window attribution,
root/dispatcher authority and agreement at every shared sequence position across
machine copies. Missing, invalid, ambiguous or conflicting state evidence retains
the Done-flip fallback. Receipt/replacement evidence still has its existing
precedence for lifecycle end timestamps, independently of task-count precedence.

Every refresh recomputes the fallback before applying the closed-state override,
including collector-only passes for finished loops. Removing or invalidating a
previously qualifying state snapshot therefore restores the fallback; an old
state-derived value is not retained as proof. Rebuild preserves the collected
state and Done-flip tables and reapplies the same projection.

The other outcome columns retain their existing semantics: a qualifying valid
open can establish known zero for `tasks_admitted`, `tasks_landed`, `parks_total`
and `lanes_accepted`; a close is not required. None takes a Done-flip fallback.
The report fields, reader functions, schema version and grants are unchanged.
No schema migration is needed for this projection-only correction.
