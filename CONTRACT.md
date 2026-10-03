# Catalogue contract

What the agent-history catalogue (Postgres schema `ah`) stores, what it guarantees and what it does
not. Code that writes the catalogue follows this file; code that reads it can rely on it.

## Content policy: everything, unredacted

**The catalogue stores full tool input and output, system prompts and every other piece of text the
transcripts contain, unredacted.** That includes prompts, replies, reasoning text where the harness
recorded it, sub-agent briefs and reports, compaction summaries, harness-injected text (system
prompts, reminders, instruction files, hook output, command and skill bodies), the complete input
and output of every tool call, and textual attachments. If an agent read a secret from a file or
printed one in a command's output, the secret is in the catalogue.

- Content lives in `ah.message.text`, `ah.tool_io` (`input_text`, `output_text`, `stdout_text`,
  `stderr_text`, `result_json`) and `ah.attachment.text`.
- The parsers and the loader never redact, omit or scrub content. Privacy is the operator's call:
  choose which agent homes to index, who can reach the database, and which namespaces each reader
  searches.
- Binary payloads (images, documents) are metadata only: `tool_io.output_parts` and
  `ah.attachment` keep type and size, never the bytes.
- `detail` and `meta` JSON columns hold structure and scalars, not content.
- Embeddings are off by default. When enabled, the corpus embedder sends chunks of `human_prompt`,
  `queued_prompt`, `assistant_text`, `subagent_brief`, `subagent_report` and `compaction_summary`
  messages; session-summary title/objective/narrative text; and failed `tool_call`/`tool_op`
  `error_excerpt` text. Each chunk includes a class/type header and a cwd basename (or namespace when
  absent; session summaries use the project basename). Regex-matched secret-shaped spans are replaced
  with `[REDACTED]`; other sensitive text is not scrubbed. The stored text remains unredacted.
- Embed run failures expose `agent_history_embed_last_failure_reason{reason="..."} 1` alongside
  `agent_history_embed_run_success 0`. The only reason labels are `auth` (401/403),
  `billing_quota` (402 or a 429 with a fixed structured quota/billing code), `rate_limit` (other 429),
  `route` (404), `provider_error` (5xx), `network` (status 0/408), and `other`. The recognised 429
  `error.code` or `error.type` strings are `insufficient_quota`, `billing_hard_limit_reached`,
  `billing_not_active` and `quota_exceeded`. Free-text messages never determine quota. The bounded
  reason is retained in catalogue metadata across failed runs and omitted from the snapshot after
  success. Labels never contain response bodies, status text, URLs or input text. Existing embed
  metric names, labels and help text are unchanged.

Treat the database like the transcript directories it was built from: local, access-controlled,
and never exposed to a network you do not trust. There is no row-level security.

Registering the MCP server gives the connected agent read access to every database row, including
stored tool inputs/outputs and secrets captured in transcripts. Its connected model provider may
receive that content when the agent includes returned tool results in its context. Contexts do not
limit the `sql` tool. If embeddings are enabled, hybrid MCP `search` sends the raw query (up to 12,000
characters, without redaction) to the configured embeddings endpoint; BM25-only search does not.
Setting `AGENT_HISTORY_EMBED_ENV` also enables the legacy query-embedding provider, even when
`[embedding]` is disabled. Hybrid/vector reader searches and hybrid MCP `search`/`search_summaries`
then send up to 12,000 unredacted query characters to that environment file's provider endpoint.

## Derived data

Everything except the collector tables is derived from the transcript files, which stay the
authority. `agent-history rebuild` empties the derived tables and re-reads every configured source.
With a source map it can only re-read what the agent homes still hold: if an agent has deleted old
transcripts (Claude Code removes sessions older than its `cleanupPeriodDays` setting), a rebuild
loses them. Keep your own copies if you want history to survive that.

Not truncated by `rebuild`: `ah.change_log`, `ah.refresh_log`, `ah.embedding` (a paid cache keyed by
model and input hash) and the collector tables (`git_commit`, `git_commit_file`, `ci_run`,
`backlog_task`, `task_prefix`, `installed_feature`, `permission_log`, `loop_receipt`,
`session_summary`, `session_topic`).

## Namespaces and contexts

Each source is a namespace `<agent>-<profile>` (`claude-local`, `codex-client`, `pi-lab`). Every
session, message and tool I/O row carries its namespace. A context is a named search scope (a set of
namespaces) in the config; search functions take a namespace list and the MCP server passes the
selected context's list. Contexts are not access-control boundaries: the `sql` tool is not filtered,
and any reader granted SELECT on these tables can read every row. Use separate databases for real
separation. Different reader roles on this shared schema do not isolate contexts without additional
row-level security or table-level partitioning, neither of which this package implements.

## Roles

`sql/roles.sql` creates `ah_writer` (owns schema `ah`, runs the indexer) and `ah_reader`
(SELECT only, `default_transaction_read_only = on`, a 60 s statement timeout). The MCP server checks
the role on every connection and refuses a superuser, the owner or a member of the owner of the
schema or any of its tables, a role with INSERT/UPDATE/DELETE/TRUNCATE on any `ah` table or CREATE
on the schema, a member of `pg_execute_server_program`, `pg_write_server_files` or
`pg_read_server_files`, and a role whose default transactions are not read-only.

## Schema changes

`sql/baseline.sql` is the squashed schema (schema_version 1), applied once to a database without
schema `ah`. Later changes are numbered files in `sql/migrations/`, applied once each and recorded in
`ah.meta` as `migration:<file>`. `analytics.sql`, `search.sql`, `structure.sql` and
`efficiency.sql` hold views and functions and are re-applied whenever their content changes. A
breaking change bumps `schema_version` and is rebuilt into a new schema, not altered in place.

## Stable keys

Surrogate ids (`session.id`, `message.id`, `tool_call.id`, ...) are reassigned by a rebuild. Key
anything you keep outside the catalogue on natural keys:

| Object | Natural key |
|---|---|
| session | `(agent, session_uid, agent_id)`: Claude main `(claude, sessionId, '')`, Claude sub-agent `(claude, parent sessionId, agentId)`, Codex `(codex, thread id, '')`, pi `(pi, header id, '')` |
| message | `(agent, event_uid)` |
| tool_call, tool I/O of a call | `(agent, call_uid)`; `tool_io.io_uid = call_uid` |
| tool_op, tool I/O of an op | `(agent, item_uid)`; `tool_io.io_uid = 'item:' \|\| item_uid` |
| llm_call | `(agent, response_id)` |
| turn | session key + `turn_key` |
| subagent_spawn | `(agent, spawn_uid)` |
| hook_event, compaction, cost_state | `(agent, event_uid)` |
| attachment / file_touch | `(agent, attachment_uid)` / `(agent, touch_uid)` |
| loop_run | `launch_uid` |

`message.seq` and `tool_*.seq` are deterministic from the source position, so a rebuild reproduces
them; after a `kind = 'rebuild'` row in `ah.change_log`, re-page any cursor from 0.
`message.content_sha256` is the sha256 of `text` as stored (UTF-8, NUL characters dropped).

## Content model

- `message.message_class`: the conversation classes `human_prompt`, `queued_prompt`,
  `assistant_text`, `subagent_brief`, `subagent_report`, `compaction_summary`,
  `task_notification_summary` (`ah.conversation_classes()`), plus `reasoning`, `system_prompt`,
  `system_reminder`, `context_injection`, `hook_output`, `command_expansion`, `skill_body`,
  `local_command_output`, `agent_message` and `interrupt_marker`. `detail.source` names the
  attachment type, tag or pi custom message type.
- `message.prompt_origin` on prompts: `typed`, `pasted`, `slash_command`, `skill`, `local_command`,
  `launch_message` (a loop root's launch prompt) or `agent_message`.
- `ah.tool_io`: one row per call (`kind = 'call'`) and per Codex executed operation (`kind = 'op'`),
  with the text columns above, `*_truncated` when the source truncated it, and `*_bytes` /
  `*_sha256` of each stored text.
- `tool_call.error_class` on failed calls: `denied`, `timeout`, `interrupted`, `validation_error`,
  `not_found`, `permission`, `network`, `nonzero_exit`, `tool_error`, `fake_cell_wait` (a Codex
  code-mode wait on a cell id the session never returned) or `other`, with `error_excerpt`.
- `session.agent_type` is the child's type with `agent_type_source` `explicit` (recorded in the
  child or its exact spawn request) or `default` (the request omitted it and the harness default is
  known). Display names never supply a type.
- pi: children of a pi-subagents run are linked to their parent by path; archived
  `subagent-artifacts/*_transcript.jsonl` copies pair a run id with the child's response id and are
  indexed as evidence only (no messages or model calls).

## Structure, change feed and search

- `ah.v_session_orchestration`: whether a session is a loop, wave or fan-out root, a lane, a poller,
  a reviewer or a plain sub-agent, and its orchestration root.
- Change feed: every index run gets a `refresh_id` (`ah.refresh_log`), appends one `ah.change_log`
  row per changed session and sends `NOTIFY ah_refresh, '<refresh_id>'` on commit.
- `ah.search(q, namespaces, since, lim, mode, classes)`: BM25 over message text (`all`, `any` or
  `phrase`). `ah.hybrid_search(q, qvec, namespaces, since, lim, w_vec, classes)`: reciprocal rank
  fusion of message BM25, tool I/O BM25 and message-chunk vectors; `qvec` NULL means BM25 only.
- `ah.session_timeline`, `ah.find_sessions`, `ah.why`, `ah.who_touched`, `ah.recent_loops`,
  `ah.active_sessions`, `ah.infra_actions` back the MCP tools.

## Live loop lifecycle (`ah.loops`)

`ah.loops` is a migration-owned table, not an analytics alias. Consumers may grant SELECT on it;
analytics re-application and rebuild preserve that grant. Each recognised, timestamped launch has
one row keyed by `launch_uid` (the same stable natural key as `ah.loop_run`). The indexer applies
migration `022_live_loops.sql` on its first schema pass and fills the table during the following
refresh post-pass, including refreshes with no dirty sessions. Rebuild re-reads retained transcripts
and refills it; rows whose source transcripts are gone cannot be recovered.

Migration `023_loop_identity.sql` adds the nullable receiver join fields `repo text`, `loop text`
and `goal_sha256 text`. `repo` is an explicitly recorded `owner/repo`, never a directory or report
basename. `loop` is the recognised launch's `loop<N>` label. `goal_sha256` is 64 lowercase hex for
the goal, never the launch file's hash. Each unknown field is independently NULL, not guessed.

A running launch can supply identity through an explicit canonical `# Loop: <owner/repo> loop<N> ·
Goal: <sha256>` line, and its goal hash through a same-line manifest entry naming the exact goal
path. Conflicting goal hashes leave only that hash unknown. Directory names and the indexer's own
checkouts are never consulted. A bare-path launch whose contents were not collected supplies no
identity itself; a captured report, a start receipt or a valid completion receipt (below) may
populate its fields later. Legacy `wave<N>` launches are not
asserted to have a `loop<N>` identity.

Recorded report identity supersedes launch identity when a completed successful structured `Write`
or `write` call records the exact report path and complete contents beginning with the header at
byte zero. Its `## Data` identity, if present, must match line 1; invalid or ambiguous Data leaves
repo and goal hash unknown. A legacy basename-only header leaves repo NULL while preserving its
exact loop and goal hash. Shell commands and partial edits are not interpreted to reconstruct
reports; absent captured report contents cannot establish report identity. These columns describe
observed identity, independently of terminal-status evidence or report outcome.

The first refresh after migration projects identity for every existing launch, even without dirty
sessions; no rebuild is needed. A transactional `ah.meta` projection-version marker avoids repeated
historical report scans. Subsequent refreshes project running loops and the roots of dirty root or
child sessions, updating identity only when a value changes. Rebuild recreates values from retained
transcript evidence and preserves consumer table-level SELECT grants.
The migration changes no grants or roles, and imposes no constraint or default on existing rows.

The receiver seam also includes `status text`, `launch_ts timestamptz`, `end_ts timestamptz`:

- `running`: no qualifying completion receipt or replacement-launch evidence (a report write alone
  is not evidence), and the root's last recorded activity
  (or launch, if later) is within 24 hours of refresh. `end_ts` is NULL.
- `finished`: a valid completion receipt exists for the launch (`end_evidence =
  'completion_receipt'`, below), or the next recognised launch in the same root replaces it
  (`next_launch`). `end_ts` is that evidence timestamp. This is an observed end, not a claim of
  success or a parsed report outcome. A transcript write of the report is not terminal evidence
  and no transcript or shell-command text is interpreted to decide an end. `report_write` is
  retained only as the value on rows tagged before receipts existed.
- `stale`: no terminal evidence and no root activity within 24 hours. `end_ts` is the last recorded
  root activity (at least `launch_ts`), not an inferred death time. A loop that died without a
  report becomes stale, never finished; new root activity can make it running again.

`launch_ts` is the operator launch message timestamp. `observed_at timestamptz` is the last refresh's
transaction timestamp. Running means running **as of that refresh**, not verified process liveness.
A silent live root may be stale after 24 hours; a dead root may appear running for up to 24 hours,
plus the delay until the next successful refresh. No refresh schedule or maximum indexing lag is
guaranteed by this package. Consumers should check `observed_at` when freshness matters. Child-only
activity is not root activity. Neither report-path parsing quality (`loop_run.status`) nor its
fallback `root_last_event` end timestamp is terminal evidence. No heartbeat or phase feed is used.

### Receipts: `completion_receipt` and start identity

Migration `025_loop_receipts.sql` adds the collector table `ah.loop_receipt`, primary key
`(machine, kind, path)`, indexed on `(kind, path)`. The hourly collector reads, for every configured
repository, the exact files `codex/report-*.md.notified` and `codex/goal-*.md.started` that the
wave-notify tool writes beside a report or goal only after a successful, non-degraded send or an
accepted start. A `.tmp.notified` name is not a receipt. A row holds `kind` (`notified` or
`started`), the absolute `path` of the receipt's target (the report or the goal), the receipt text
`content`, its mtime `receipt_mtime`, whether the target exists (`target_exists`, true also when it exists but could not be read, in which
case the hash and line are NULL), `target_sha256`
and, for reports only, `target_line1` (stored only when it is a loop header line, else NULL; the read of
the first line is bounded to 4096 bytes while the digest covers the whole file); plus `repo_origin`, the `owner/repo` of the checkout's
`origin` remote when parsable, and `seen_at`. No report or goal body is stored. The table is not
truncated by rebuild or analytics re-application. Work and Personal repositories are handled
identically. The collector role needs `SELECT, INSERT, UPDATE` on it, which the migration grants to
`ah_ingest` when that role exists at migration time (otherwise provision it with the other
collector tables), and never `DELETE`; readers of `ah.loop_run` receive `SELECT`.

A launch is finished with `end_evidence = 'completion_receipt'` when a `notified` receipt exists
for its exact report path and

- its mtime is at or after `launch_ts` and before the next launch of the same report path
  (launches of one report path with equal `launch_ts` leave it unfinished; launches of other
  report paths, including other campaigns in the same `codex/` directory, do not bound it);
- no lane session of the loop starts after the receipt;
- if the content is `sha256:<64 hex> request <id>`, the report existed when collected, the digest
  equals `target_sha256` and `target_line1` is a loop header naming the launch's loop number (a
  digest receipt whose report is missing does not finish the loop); a legacy `request <id>`
  receipt counts on the exact path alone.

`end_ts` is the earliest qualifying receipt mtime across machines. Only launches that have a
receipt and are not already finished are examined. A root that pings before moving its report into
place has no receipt, so its loop stays running or stale.

The finish is an observed end, not a claim that nothing happened afterwards. A lane session linked
to the loop that starts after the receipt blocks the finish whenever the loop is next evaluated, but
a loop is re-evaluated only when its root or one of its sessions is re-indexed, so a late lane not
yet indexed, or a root that sends the notification and carries on, leaves it finished. Re-tagging a
root or a rebuild drops `report_write` evidence for good (rows already carrying it stay finished
until then), so a loop with no receipt, which includes every loop that predates receipts, then reads
running or stale, never finished, unless a receipt exists for it.

A `started` receipt at the exact goal path of a launch (`loop_run.goal_path`) supplies `repo`,
`loop` and `goal_sha256` when its content is exactly `<owner>/<repo>#loop<N>#<goal sha256>` (`owner/repo`
characters `A-Za-z0-9_.-`, `loop<N>` the launch's own label, 64 lowercase hex). A receipt row is
keyed by goal path and a relaunch overwrites it, so a receipt belongs to a launch only when its
mtime is at most 120 seconds before the launch (clock skew allowance) and before the next launch of
the same goal path. A receipt outside that window is not evidence about the launch and is ignored; a
receipt inside the window of more than one launch (including equal `launch_ts`) cannot be
attributed and leaves all three NULL for each of them. A receipt naming a different loop number is
also ignored. Every other receipt for that goal on every machine must name the same repository as
its own `repo_origin` (compared case-insensitively), agree with the others and not contradict
identity already recorded from the launch or report. Any conflict or missing piece (different
origin, no origin, disagreeing machines, malformed content, or a contradiction) leaves `repo`,
`loop` and `goal_sha256` all NULL; repo is never filled from the origin alone and never from a
basename. A launch finished by a valid completion receipt (the rule above; a running loop gets nothing from
this path) also takes identity from it when the start receipt supplies none. `repo` is the
receipt's `repo_origin`, never the name written in the report header. `loop` and `goal_sha256` are
parsed from `target_line1`, which must match `# Loop: <name> loop<N> · Goal: <64 lowercase hex>`
with `loop<N>` equal to the launch's own label. A receipt with no origin, no usable header (for
example a legacy header without a goal digest) or a header naming another loop contributes
nothing, leaving the other rules' result. Valid receipts on different machines that disagree on
origin or header, or that contradict identity already recorded, leave all three NULL. A valid start
receipt's identity wins; if both exist and disagree on any field, all three are NULL.
Without a receipt the identity rules above are unchanged. The receiver seam
cites this section and "Live loop lifecycle" for both rules.

### Progress fields

Migration `024_loop_progress.sql` adds nullable columns on the same grant-preserving table:

- `lanes_total bigint`: indexed lane sessions; `lanes_returned bigint`: lanes with a recorded
  `lane-return` value. No lanes or no captured returns means NULL, never a fabricated zero. A
  return count does not imply success, nor that every scheduled lane's return was captured.
- `last_activity_at timestamptz`: newest recorded session event time across the root and linked
  lanes; `root_agent text`: the root's recorded harness (`pi`, `claude` or `codex`).
- `llm_calls bigint`: recorded model calls; `input_uncached`, `cache_read`, `cache_write` and
  `output` (all bigint): token totals. Cache writes combine the 5-minute and 1-hour buckets.
  Missing calls or any missing component of a token total leaves that total NULL.
- `priced_cost_usd numeric`: USD at the recorded model's effective catalogue price. No calls,
  any unpriced call or any missing token component leaves the total NULL, not a partial cost or
  zero. This is list-price accounting, not an invoice.
- `tool_errors bigint` and `api_errors bigint`: recorded tool failures and API-error calls.
  No tool/call evidence leaves the respective count NULL; known outcomes can establish zero.
  `commits bigint` includes recorded commits/cherry-picks; `pushes bigint` counts recorded pushes.
  No matching git event leaves the count NULL, rather than claiming none happened.

These project existing catalogue evidence, with the same membership convention as
`ah.v_loop_summary`: root calls/actions inside the launch window and linked lane sessions whole.
Running/stale roots have no terminal cutoff; finished roots stop at the recorded end. Session
activity timestamps describe the newest observed root/lane event, not process liveness. There is
no new collector or heartbeat. NULL always means unknown, never an alias for zero.

Every successful refresh post-pass updates running rows and roots of dirty root/child sessions,
including heuristic lanes and their descendants. Progress selection starts from the partial
`loops_progress_repo_idx` over running rows and resolves dirty owners through
`loop_run_progress_owner_idx`; it does not walk finished-loop history. Receiver queries can use
`WHERE repo = <exact owner/repo> AND status = 'running'` on that same repository index. This bounds
progress selection, not the separate lifecycle and identity refreshes described above.
Initial historical fill processes at most 128 additional rows per pass with a transactional cursor
in `ah.meta`, so existing finished rows fill without rebuild and work stays bounded. The deployed
refresh cadence is about 320 seconds today, not a package guarantee; historical fill may therefore
take several passes. Rebuild re-projects retained transcript evidence. Analytics re-application
preserves values, and both operations preserve table-level consumer SELECT grants. Identity and
lifecycle meanings above are unchanged; consumers should still inspect `observed_at` for freshness.

## Efficiency classifier

`ah.efficiency_calls(namespaces, session_uid, agent_id)` returns one row per model call with its
`trigger`; `ah.efficiency(...)` sums them per session. Definitions are at the top of
`sql/efficiency.sql`. Rows from pi `usage` entries and compaction summaries are not model calls of
the conversation and are excluded.

Supported today: Claude Code, Codex and pi sessions. The classifier uses tool-result byte offsets
and Claude raw-record origin to order events where recorded timestamps tie or disagree with their
physical position. Existing catalogue rows do not acquire these fields merely by applying the
migrations: rebuild from retained transcripts before relying on exact per-call parity. A rebuild
cannot recover transcripts already deleted from their source homes.

## Collector metadata

The collector writes `task_prefix`, `backlog_task`, `git_commit`, `git_commit_file`, `ci_run`,
`installed_feature`, `permission_log` and `loop_receipt`. Git subjects, tracker titles, labels and project values,
file paths, repository slugs, workflow names and installed-feature names are stored verbatim.
Author emails are compared to configured identities but only `author_is_owner` is stored.
Permission logs contribute timestamps, line hashes, tool names, command verbs, classifier reasons
and sub-agent flags, never command arguments or target text. Loop receipts contribute the receipt
text, a report's first line, target hashes and the checkout's origin slug, never a report or goal body. These metadata fields may themselves
be sensitive. Protect collector homes, configuration and the catalogue accordingly.

Repositories and homes are explicitly configured. A failed CI query is an attributable collection
error, not a failed or successful CI run. Other repositories continue independently. A receipt that cannot be read, or changed while it was
read, is skipped, reported in the run's `errors` and retried next run; like other step errors it
does not fail the run (only `gh_unavailable` does). Feature
snapshots reconcile removed features; task removal and off-default git flags require a successful
remote fetch. The collector changes remote-tracking git refs, never the working tree or tracker.
The hourly collector validates the server's session and current role as dedicated `ah_ingest`,
rejecting administrative privileges and catalogue-owning role membership before collection.
The guard completes its transaction before writes so each metadata write can commit normally.
The pre-existing writer/indexer `collect-git` path accepts the indexer's connection separately.

## MCP compatibility

The package supplies one MCP server, including `search_summaries`, `task` and `efficiency`.
Explicit namespace overrides and configured contexts select search scopes, not permissions.
Legacy provenance tools (`session`, `why`, `touched`, `loops`, `task`) default to unscoped output;
the first four support explicit context filtering. Plain-reader role validation and the extended-protocol single-statement SQL guard remain required.
Registering it can disclose unredacted catalogue content to the connected model provider, as
stated above.

## Journal summary source

`journal-sync` reads only `ah_export_session_summary` in an operator-selected SQLite database,
opened read-only with query-only mode. It stores title, objective, narrative, outcomes, unfinished
items, classification, project, model and topics without redaction. Session identity joins use
`(agent, session_uid, agent_id)`; unmatched summaries are retained. Watermarks make ordinary runs
incremental. A daily full pass reconciles deletions, and a changed source instance identifier
resets summary/topic rows before resync. Every selected record's required timestamp must parse
with a timezone before any reset, batch write, deletion or metadata update. Invalid timestamps
produce a visible skipped reason and preserve the entire previous journal catalogue state.
It never drops or rewrites catalogue schema.
