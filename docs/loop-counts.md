# Loop task count precedence

The nullable bigint columns describe separate evidence surfaces, not interchangeable totals.
Unknown is NULL, not an invented zero. See [the catalogue contract](../CONTRACT.md) for exact
identity, lifecycle and state validation rules. Neither loop status nor a worker return proves
publication, and these projections do not authorise paid work.

## V3 closeout counts first

A v3 run finished by its own qualified closeout (`end_evidence = 'closeout'`) takes
`tasks_landed` and `tasks_done` from the closeout's collected `landed_count`, and `parks_total`
from its `parked_count`: distinct task IDs under the `## Landed` and `## Parked` headings. This
supersedes every rule below for that run and is reapplied last on every refresh. NULL counts
(an old collector row, a missing section, an oversize file) fall through to the rules below,
column by column. Revoking the closeout end restores them on the next refresh. See the
contract's "V3 closeout delivery counts".

## Legacy `tasks_done`

`ah.loops.tasks_done` keeps its column and report/reader shape:

1. A valid, exactly attributed **closed** collected state log overrides the legacy Done-flip
   count. Count distinct exact tracker IDs on authoritative `land` events using the same
   publication projection as `tasks_landed`. Authority is root/dispatcher in the original body
   dialect and root alone in the pi charter dialect. Operational aliases, extension/daemon
   observations and a return's `landed` claim never add tasks. Matching machine copies do not
   inflate totals. In the original dialect, a qualifying closed log with no lands establishes
   zero. In the pi dialect the same qualifying close can supply NULL when lands are absent;
   closing does not manufacture a known zero.
2. Otherwise retain the existing Backlog Done-flip fallback: distinct tasks in collected
   default-branch Done flips for the loop repository within `[launch_ts, end_ts)`. Open statuses
   have an open window; a stale window ends at root last activity. The fallback does not filter
   by publishing session. Unknown repo or missing first Done scan supplies NULL; a completed
   scan with no qualifying flips supplies zero.

An open alone never overrides `tasks_done`. A receipt, closeout or replacement launch is not a
collected state close. The close must pass the selected frozen schema, exact origin/loop/digest
and launch-window attribution, actor authority and agreement at every shared sequence position.
Missing, invalid, ambiguous or conflicting state retains the Done-flip fallback. Lifecycle end
precedence is independent of task-count precedence.

Every refresh recomputes the fallback before applying a qualifying closed-state override,
including collector-only passes for finished loops. Removing or invalidating a snapshot restores
the fallback, not an old state-derived value. Rebuild preserves state and Done-flip collector
tables and reapplies the projection.

## State outcomes and the second pi dialect

Both strict body dialects retain integer wire `v = 1`. The original open uses `goal_sha256`;
the pi open uses `charter_sha256`, `root = 'llm'`, `root_model` and `first_wave`, with actors
`root`, `ext` and `daemon`. Selection is by open shape, never by date. Unknown members or invalid
events invalidate the snapshot; the second dialect does not relax the first validator.

- `tasks_admitted`: distinct exact tracker IDs on explicit admits, not first-wave/envelope lists
  or dispatches.
- `tasks_landed`: distinct exact tracker IDs on authoritative lands, not returns or accepts.
- `parks_total`: cumulative valid park events, including aliases; duplicate snapshots do not add
  parks, but separate sequence positions do.
- `lanes_accepted`: distinct true-accept lanes. Task-only accepts require one exact preceding
  dispatched lane; unresolved attribution leaves acceptance NULL independently of other counts.

A valid original-dialect open can establish known zero. In the pi dialect, absent outcomes stay
independently NULL: open, first-wave tasks, dispatch and return alone establish none of these
counts. Explicit valid events project independently. Shared-position conflicts withhold affected
outcomes; open conflicts withhold all. Full-ledger agreement is required for terminal close.
No state outcome uses the Done-flip fallback.

## Observed v3 Task-trailer fallback

Only launches with `loop_run.mode = 'v3'` can use this independent fallback for `tasks_landed`:

1. Authoritative qualified state lands take precedence. Invalid or conflicting qualified state
   cannot be bypassed with a trailer count. The original dialect's authoritative open/zero
   policy also keeps precedence.
2. A valid pi cohort with no explicit root land, no invalid sentinel and no open/land conflict
   permits fallback because it does not assert an absent land as zero. With no qualifying state
   cohort, fallback is also available. Other outcome fields are unchanged.
3. Count distinct exact task IDs in known terminal Git Task trailers on observed on-default
   commits within the half-open launch window. Its upper bound is the earlier recorded end or
   next launch on the same root or exact terminal path. Equal-time launches are ambiguous and
   supply no trailer count.

The observed repository set is authoritative `ah.loops.repo` plus captured in-window
`git_event.remote_url` values on the root or linked sessions. Remotes use `ah.remote_slug`;
collector slugs lose exactly the first component, retaining owner/repo. Host is not part of this
identity. No basename, local file, `ah.repo.slug` or uncaptured charter invents repository coverage.
This counts observed publication, not every repository in a charter.

Migration 037 adds nullable `ah.git_commit.task_ids text[]`, without a default or backfill.
Historical NULL means unobserved trailers; known `[]` means an observed commit with no valid task
IDs. Folded terminal trailer values are validated whole; body mentions and aliases do not count.
Exact spelling and subtasks survive. Counts deduplicate repositories, commits and collector copies.
Neutral public aliases (`Task: loop<N>-<lane>`) never count, even with a numeric lane; the private
commit that consumes the work carries one `Task: <ID>` per delivered task.
No observed commits, or any matching commit with unknown trailers, leaves NULL. Observed commits
with known empty arrays can establish zero. Collector-only refreshes recompute even finished
loops; legacy `tasks_done`, retained commit history, reader shapes and grants remain unchanged.
