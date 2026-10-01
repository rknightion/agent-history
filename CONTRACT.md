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
`backlog_task`, `task_prefix`, `installed_feature`, `permission_log`, `session_summary`,
`session_topic`).

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
`installed_feature` and `permission_log`. Git subjects, tracker titles, labels and project values,
file paths, repository slugs, workflow names and installed-feature names are stored verbatim.
Author emails are compared to configured identities but only `author_is_owner` is stored.
Permission logs contribute timestamps, line hashes, tool names, command verbs, classifier reasons
and sub-agent flags, never command arguments or target text. These metadata fields may themselves
be sensitive. Protect collector homes, configuration and the catalogue accordingly.

Repositories and homes are explicitly configured. A failed CI query is an attributable collection
error, not a failed or successful CI run. Other repositories continue independently. Feature
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
