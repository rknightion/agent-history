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
- Metric families are published only over OTLP, by the periodic indexer's metric collection
  (service `agent-history-index`). The package serves no metrics endpoint and writes no Prometheus
  textfile for a scraper; the worker run snapshots under `/var/lib/alloy/textfile-agent-history` are
  an internal hand-off read by that collection.

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
`backlog_task`, `backlog_done_event`, `backlog_done_scan`, `collector_mutation_audit`, `task_prefix`,
`installed_feature`, `permission_log`, `loop_receipt`, `loop_run_metadata`, `loop_state`, `session_summary`, `session_topic`).

### Rebuild reader boundary

Before a destructive rebuild, the operator must stop admission and drain every consumer's whole
logical read. This includes admitted jobs, buffered transcript pages and parent-pagination cursors,
not just active SQL statements or database connections. Stop all consumer processes and prevent
automatic restart throughout the transition. Resume with fresh processes only after the complete
rebuild succeeds. Existing requests carry no generation expectation, so the catalogue cannot detect
an old admission or cursor reused after completion. Draining is an operational precondition, not a
boolean claim the loader can independently verify.

`ah.v_session_state` preserves its existing columns but hides all rows while
`ah.meta.rebuild_in_progress` is present. The existing five-argument `ah.session_events` preserves
its arguments and result columns, and raises SQLSTATE `55000` while marked, including for missing
sessions and empty pages. A marker has no age expiry. This prevents incomplete projections from
invalidating cached owner state and prevents transcript pages from reporting a partial successful
completion. These guards do not fence arbitrary direct table reads or every search function.

Rebuild freezes a strictly readable inventory before destructive preparation and verifies that
every retained transcript/artifact produced its source row before reopening. Optional progress
sidecars keep their existing bounded validation/skip behaviour. The current SQL guards are applied
before the marker commits atomically with destructive preparation. The outer writer lock remains held
through source loading, post-passes, retained-summary relinking and BM25 index creation. Only their
complete success removes the marker. `chunks_reset_at` records both destructive chunk reset
and successful completion, preserving the paid-cache garbage-collection grace period. Source errors, a lock-held
load, post-pass/relink/index failure or an exception retain the fence. A pre-destructive refusal
leaves the existing generation and any existing marker unchanged. Incremental indexing or manual
index creation never clears a failed rebuild's marker: correct the failure, keep consumers stopped,
and run a complete source-preserving rebuild. Never remove the marker to reopen a partial load.

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
Complete reserved harness wrappers carried inside user prompt text are split into separate classed
rows with source and `text_start`/`text_end` character offsets. A non-blank human remainder keeps
one prompt row and its existing `event_uid`; whitespace-only remainder is retained on injected
rows rather than classified as a human prompt. Top-level code-fenced, Markdown-quoted, inline,
unknown and incomplete markup remains human text. Fenced and Markdown-quoted literal tags inside
an unambiguously complete reserved wrapper inherit its outer class without defining its boundary.
Recognisers do not guess away provenance; stored content is retained losslessly. Updated parser versions require retained transcripts to be re-parsed or rebuilt before
historical search and class counts reflect this boundary.

A pi session or lane file indexed under an older pi parser version is purged and reparsed from
byte zero on the next refresh (`parse_issue` kind `parser_reparsed`), once the whole file reads back.
pi artifact files replay from version `6` to `6-links1` in place, and pi progress files are outside
this check. Any other indexed file whose parser version changed is refused (`status = 'error'`, kind
`parser_upgraded`) until `agent-history rebuild`.

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
  indexed as evidence only (no messages or model calls). Both direct run-id directories and
  legacy `run-<index>` children under a root session directory establish nested lineage. A child whose
  header `parentSession` names a file with no uid in its basename (a lane's own `run-0/session.jsonl`)
  gets `parent_session_uid` from the session whose indexed file path ends with that path. Explicit
  agent names from retained spawn/artifact metadata, including `-low` agents, supply lane roles;
  directory names and timing alone never choose an agent type.

### pi compaction session events

Each recorded pi compaction also produces `ah.session_event` kind `compaction` with the same
`event_uid` as `ah.compaction`. `detail.before_tokens` is the recorded `tokensBefore` value when
it is a valid non-negative integer; `detail.before_tokens_source` is `compaction.tokensBefore`
when known and NULL otherwise. `detail.after_tokens` and `detail.after_tokens_source` are NULL
when no authoritative post-context measurement is captured. Summary-generation usage is never a
post-compaction context measurement. Existing compaction rows, summary content, summary-generation
model usage and rollups are unchanged. Branch summaries are not asserted to be compaction events.

Unknown before/after measurements are independent. Validly recorded zero is known; absence,
invalid values and inferred summary-token estimates are not zero. Historical session-event rows
require retained transcripts to be re-parsed or rebuilt after the parser version changes.

### Nullable transcript telemetry

Migration `033_nullable_telemetry.sql` adds the following optional seam in schema_version 1.
Every column is nullable, with no default or backfill. Source transcripts remain authoritative:
missing, invalid or unattributable evidence means NULL independently for each field. Validly
recorded zero, false and empty arrays are known values, not absence. Applying the migration alone
does not populate historical rows; the corresponding parser upgrade and a rebuild from retained
transcripts are required. No live catalogue rebuild is implied by migration application.

| Table | New columns and SQL types | Authoritative source and meaning |
|---|---|---|
| `ah.llm_call` | `duration_ms int`, `latency_basis text` | The observed per-call timestamp interval described below, never a turn duration or an inferred server duration. |
| `ah.llm_call` | usage counts | A Claude or pi usage record whose counts are all zero and which carries no stop reason stores every count (and a zero recorded cost) as NULL: Claude Code writes each content block with message_start usage, which a provider-adapting proxy can send as all zero. Codex token records carry no stop reason, so their zeros stay known. With a configured `[litellm]` spend-log DSN, unknown counts in the named Claude namespaces are filled from `LiteLLM_SpendLogs` by request id; a known count is never overwritten. If the spend logs cannot be read (DSN file, connection or query), the fill is rolled back, the counts stay NULL and the index pass still succeeds. |
| `ah.llm_call` | `cost_usd numeric` | A call's explicitly recorded USD usage cost (including pi assistant usage cost), not catalogue list-price accounting or an allocation from cumulative session cost. |
| `ah.llm_call` | `thinking_ms int` | Explicit recorded thinking duration in milliseconds, not reasoning tokens converted to time. |
| `ah.llm_call` | `raw_stop_reason text` | The provider/harness stop reason verbatim before any existing normalisation; `stop_reason` semantics are unchanged. |
| `ah.llm_call` | `api text`, `provider text` | Explicit call API and provider identifiers in retained assistant/request metadata, not inferred from model names. |
| `ah.llm_call` | `cache_miss_type text`, `cache_missed_tokens bigint` | Explicit per-call cache miss classification and token count in retained usage metadata, not uncached input used as a proxy. |
| `ah.llm_call` | `input_transform_types text[]` | Recorded input transform type names in source order, not transform bodies or inferred transformations. |
| `ah.llm_call` | `advisor_model text`, `inference_geo text` | Explicit advisor model and inference geography in retained call metadata, not the indexer's model or location. |
| `ah.llm_call` | `iterations int`, `ttft_ms int`, `attempts int`, `processing_ms int` | Explicit call iteration count, time to first token in milliseconds, attempt count and processing time in milliseconds. No counts or timings are synthesised from neighbouring calls. |
| `ah.cost_state` | `has_unknown_model_cost bool` | The explicit cumulative cost-state flag for unknown model cost (Claude cost telemetry), not absence of a catalogue price. |
| `ah.message` | `phase text` | The explicitly recorded message phase (including Codex response-message phase), not a phase inferred from message class or position. |
| `ah.turn` | `reasoning_summary text` | The explicitly recorded Codex `turn_context.summary` reasoning-summary mode (`none` or `detailed`), not reasoning text, a generated summary or a substitute assembled from reasoning messages. |
| `ah.turn` | `trace_id text`, `root_turn_key text`, `origin_hint text` | Explicit trace, root-turn reference and origin hint from retained turn metadata; no identity guessed from timing, paths or the indexer's environment. Existing turn keys and `origin` are unchanged. |
| `ah.turn` | `prompt_index int`, `turn_index int`, `pending_bg_agents int`, `pending_workflows int` | Explicit source indices and recorded pending-background-agent/workflow counts, not row ordinals or counts of indexed children. |
| `ah.tool_call` | `deadline_hit bool` | Explicit deadline-hit result metadata; neither an inferred timeout nor a non-zero exit code. The existing `exit_code` column is unchanged. |
| `ah.subagent_spawn` | `timeout_ms bigint`, `deadline_at timestamptz` | Explicit valid integer spawn timeout in milliseconds. A deadline is a recorded timezone-aware ISO timestamp or explicit epoch-millisecond value converted to UTC, never computed from completion or a relative timeout. Invalid or non-finite values remain NULL. |
| `ah.subagent_spawn` | `run_fanout_budget int`, `spawn_budget int`, `active_async_capacity int`, `lifecycle_status text` | Explicit retained spawn/run budget, capacity and lifecycle metadata. Budget/capacity objects contribute only a valid finite integer `.limit`; legacy scalar integers are retained directly. Unlimited/invalid values remain NULL; `.used`, `.remaining`, observed child counts and liveness are not limits. Existing launch/completion status policies are unchanged. |
| `ah.tool_op` | `mcp_plugin_id text`, `mcp_read_only bool` | Explicit executed MCP-operation plugin identifier and read-only annotation (including Codex operation metadata), not classification from tool name or arguments. |

For retained native pi single-agent launches, existing `ah.subagent_spawn.spawned_at` is the matched call-entry **dispatch** timestamp. It is not an OS process-start timestamp or tool-result completion time. A successful native single-mode result with matching `asyncId`/`runId` may establish an asynchronous launch when `async` was omitted; management calls, failures and ambiguous result shapes do not establish a spawn.

`latency_basis` is one of these source-specific descriptions when a valid interval is available:

- `pi_request_to_entry`: pi assistant message request/start timestamp to its enclosing retained
  session-entry timestamp.
- `claude_parent_to_last_line`: the causally referenced Claude parent record's timestamp to the
  last observed assistant line for the same response id. Streaming lines do not create extra calls.
- `codex_prev_boundary_to_usage`: the preceding recorded Codex call boundary to its attributable
  usage record. This is a boundary-to-usage interval, not a measured provider processing time.

An absent endpoint, ambiguous attribution or negative interval leaves both `duration_ms` and
`latency_basis` NULL. These bases are not interchangeable performance measurements. A source with
no applicable observed interval supplies no guessed basis. Explicit `ttft_ms`, `thinking_ms` and
`processing_ms` remain independent of that interval.

On natural-key conflict, `thinking_ms` takes the maximum non-NULL observation. All other new
columns take the latest non-NULL observation in loader order, including false, zero and an empty
array. NULL never erases a known value. Existing columns retain their previous policies: in
particular, message content and cumulative cost-state rows remain immutable, while their new
`phase` and `has_unknown_model_cost` fields alone can be enriched. No rename, content omission,
redaction, new grant or change to existing list-price views is introduced.

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

### Current v3 recognition, identity and completion

Recognition is shape-based. Inline operator text must contain `You are the root` or
`You are the campaign root` and one distinct accepted terminal target. Historical report tokens
keep precedence: an accepted report target selects the historical grammar even if closeout tokens
are also present, and several distinct report targets reject the launch rather than falling
through to v3. Without accepted report tokens, exactly one distinct
`codex/closeout-<name>-loop<N>.md` target selects `mode = 'v3'`. No `Contract` declaration or date
cutover is required. Historical v2-shaped launches remain valid after v3 appears.

The token prefix may be `codex/`, `./codex/` or an absolute path ending in `codex/`; names use
word characters, dots and hyphens before `-loop<N>.md`. Token boundaries reject suffixes such as
`.backup`. Relative targets resolve against the recorded cwd. An absolute spelling and its
matching relative suffix describe one target; distinct resolved targets remain ambiguous.
Inline recognition and the efficiency recogniser do not inspect the filesystem. Historical
bare launch paths remain a separate compatibility route, including complete captured exact-path
reads described below, not permission for v3 projection to open local files.

The existing columns are compatibility slots, not new v3 names:

| Column | Meaning for `mode = 'v3'` |
|---|---|
| `loop_run.report_path` | Exact closeout path. |
| `loop_run.goal_path` | Sibling charter path, replacing the closeout basename prefix with `charter-`. |
| `ah.loops.goal_sha256` | Charter SHA-256, never the launch-file hash. |
| `ah.loops.repo`, `loop` | Explicit origin `owner/repo` and exact recognised `loop<N>`. |

Migration `038_loop_run_metadata.sql` adds the collector table `ah.loop_run_metadata`, keyed by
`(machine, kind, path)`. For explicitly configured checkouts it records `charter` and `closeout`
files directly below `codex/`, named `charter-*-loop<N>.md` and `closeout-*-loop<N>.md`.
Rows retain only whole-file `sha256`, `file_mtime`, `repo_origin`, `seen_at` and nullable `line1`,
plus the key. Charter line 1 is always NULL. Closeout line 1 is retained only when the bounded
4096-byte read contains the complete qualified header:

```text
# Closeout: <name or owner/repo> loop<N> · Charter: <64 lowercase hex>
```

Migration `041_closeout_counts.sql` adds nullable `landed_count integer` and `parked_count integer`.
For a closeout whose header qualifies, they hold the number of distinct task IDs under its
`## Landed` and `## Parked` headings: level-2 headings whose text starts with `Landed` or `Parked`
in any case (trailing words allowed), each section running to the next level-2 heading, so
level-3 sub-headings, bullets and tables stay inside it. Headings inside fenced code blocks do
not count. IDs match `[A-Z][A-Z0-9]*-[0-9]+(?:\.[0-9]+)*`, case-sensitive and not embedded in a
longer word; repeats count once. A missing section is NULL and a section without IDs is 0.
Charters, unqualified closeouts, files over 1 MiB and non-UTF-8 bodies are NULL. The body is
counted from the same descriptor and snapshot it is hashed from and is not retained; only the two
integers are stored. Rows collected before this migration stay NULL until collected again.

No charter or closeout body or `## Data` is stored by this collector, and there is no body-copy
step to a transcript archive or NAS. Hashing reads the whole stable file without retaining its
body. Canonical checkout boundaries, anchored no-follow descriptors and snapshot identity checks
reject links, special files and changed inputs. Rejected inputs are reported and retried without
partial metadata. Rebuild preserves this collector table. The migration copies existing receipt
SELECT grants and grants only SELECT, INSERT and UPDATE to an existing `ah_ingest`; roles created
later need explicit equivalent grants. Existing relation grants remain unchanged.

V3 identity requires exact origin, loop and charter evidence. Collected origin supplies repo,
never the closeout display name, a basename or `ah.repo.slug`. The charter digest must agree across
exact-path charter metadata, the captured exact-path launch manifest and qualified closeout
headers when present. Without collected origin, explicit captured root/relaunch remote metadata
or eligible git-event remotes can supply origin; conflicting or malformed collected evidence
cannot be bypassed with a convenient remote. Missing pieces stay NULL; conflicting copies are not
chosen by mtime or proximity. No new state log establishes identity by itself.

A qualified exact-path closeout can finish the run with `end_evidence = 'closeout'`. Its mtime
must be in the half-open launch window bounded by the next launch on the same root or exact
closeout path. Equal-time attribution is ambiguous. Repo, loop and charter must be known and
consistent; a later linked lane start blocks that candidate end. A complete successful captured
structured `Write`/`write`, `apply_patch` Add File or executed FileChange add of that exact path can
supply the same hash/header fallback. Both dispatch and completion must belong to the window.
Failed or truncated results, shell text, output-only path lists and partial edits do not prove a
complete closeout. Update, move or delete evidence cannot reconstruct a body and conservatively
invalidates an older transcript-only fallback when completeness is no longer established.

Collector-only refreshes re-evaluate identity and closeout evidence, including finished runs.
Conflicting hashes, invalid headers, ambiguous attribution or subsequent incomplete mutations
cannot preserve a closeout-only finish. Invalidated closeout evidence restores the ordinary
lifecycle unless independent replacement or qualified state-close evidence still establishes an
end. A v3 closeout does not require a legacy notification receipt. Projection reads retained
catalogue evidence only, never the indexer's local document paths.

### HISTORICAL v2 identity and retained compatibility

The following report/goal rules describe historical v2 and legacy launches, not v3 closeouts.
Stored receipts remain usable, but new legacy receipt collection is retired globally.
Report targets use `codex/report-<name>-loop<N>.md` with the same token boundaries and path
resolution as closeouts. The older legacy grammar additionally accepts `-wave<N>.md` and keeps
its existing historical activation rules. Those rules never impose a v3 date cutover.

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

Path-only launches can also resolve from a complete, successful structured read of the exact
recorded launch path. Resolution uses captured bytes and the recorded working directory, never
files on the indexer's machine. Partial, failed or conflicting reads supply no launch contents.
A relaunched root joins an existing loop only through its exact launch-file path or a successful
final simple `loop-state append` invocation naming that loop's existing state path. Ambiguous
matches remain unlinked. A linked relaunch is another root session, not a lane; its activity
contributes to the original loop without changing the dialect's terminal evidence rules.

Historical linkage repair visits at most 128 retained sessions per refresh with a transactional
`ah.meta` cursor. Existing structured foreground spawn results can repair missing child paths;
metadata-only replay separately visits at most 128 retained pi artifact files per refresh. Neither
path rebuilds session content or invents missing evidence. A content rebuild resets the linkage
cursor; grants and collector tables retain their existing rebuild guarantees.

### Current lifecycle status

The receiver seam includes `status text`, `launch_ts timestamptz`, `end_ts timestamptz` and nullable
`waiting_until timestamptz` (migration `039_loop_wait_status.sql`). Terminal evidence takes
precedence over quiet-state observations:

- `running`: no qualifying v3 closeout, historical completion receipt, replacement-launch or attributed state-close
  evidence or current quiet-state override (a v2 report write alone is not evidence), and the root's last recorded activity
  (or launch, if later) is within 24 hours of refresh. `end_ts` is NULL.
- `finished`: a valid completion receipt exists for the launch (`end_evidence =
  'completion_receipt'`, below), the next recognised launch in the same root replaces it
  (`next_launch`), or a valid complete collected state snapshot supplies an exactly attributed
  root/dispatcher `close` as described under retained state-log outcomes. `end_ts` is that
  evidence timestamp; qualifying v3 closeout also establishes `finished` with `end_evidence =
  'closeout'`. Existing closeout/receipt/replacement evidence keeps precedence over state close. This is an observed end, not a claim of
  success or a parsed report outcome. A historical v2 transcript write of the report is not terminal
  evidence; unstructured transcript or shell-command text never decides an end. `report_write` is
  retained only as the value on rows tagged before receipts existed.
- `waiting`: the latest eligible root line records a valid future wait. `waiting_until` is that
  explicit deadline and `end_ts` is NULL.
- `paused`: the latest eligible root line records a pause. Both `waiting_until` and `end_ts` are NULL.
- `stale`: no terminal or current quiet-state evidence and no root activity within 24 hours. `end_ts` is the last recorded
  root activity (at least `launch_ts`), not an inferred death time. A loop that died without a
  report becomes stale, never finished; new root activity can make it running again.

`launch_ts` is the operator launch message timestamp. `observed_at timestamptz` is the last refresh's
transaction timestamp. Running means running **as of that refresh**, not verified process liveness.
A silent live root may be stale after 24 hours; a dead root may appear running for up to 24 hours,
plus the delay until the next successful refresh. No refresh schedule or maximum indexing lag is
guaranteed by this package. Consumers should check `observed_at` when freshness matters. Child-only
activity is not root activity. Neither report-path parsing quality (`loop_run.status`) nor its
fallback `root_last_event` end timestamp is terminal evidence. No heartbeat or phase feed is used for this lifecycle.

For v3 only, the last nonempty line of the latest original-root or exactly linked relaunch
assistant message may be `WAITING: <reason> until <UTC timestamp>` or `PAUSED: <reason>`, with a
nonempty reason. Waits use canonical minute or second UTC forms, for example
`2030-01-02T03:04Z` or `2030-01-02T03:04:05Z`; explicit `+00:00` and second fractions up to six
digits are also accepted. A missing, malformed, non-UTC or nonfuture deadline supplies no waiting
override. `waiting_until` is an explicit observation, never a calculated process deadline.
Newer root conversation, tool or model activity clears an older marker, including physically
later activity at tied timestamps. Child-only activity cannot set, invalidate or revive it.
Expiry is re-evaluated without new transcript bytes and restores ordinary running/stale semantics;
paused has no invented expiry. Terminal evidence wins and clears `waiting_until`.

The efficiency collector recognises v3 inline launches through the same pure target grammar,
retains historical protocol labels and adds only the finite `v3` label. Explicit unexpired waits
and pauses suppress stalled-root observations, not token/call accounting. Expiry or new root
activity restores the existing stall rules; missing or invalid quiet evidence never suppresses
by inference. Cache upgrades recover retained launch/quiet evidence in bounded scans without
resetting offsets or cumulative counters. This does not establish process liveness or change
unrelated collectors.

### HISTORICAL receipts: `completion_receipt` and start identity

The following describes retained rows and their historical read projection. The supported
collector no longer scans or writes legacy receipts for any harness, including enabled legacy
configurations. It collects new run metadata and state independently. Receipt tables, columns,
history, grants and read queries remain; this is not a deletion or schema retirement.

Migration `025_loop_receipts.sql` adds the collector table `ah.loop_receipt`, primary key
`(machine, kind, path)`, indexed on `(kind, path)`. Historically the hourly collector read, for every configured
repository, the exact files `codex/report-*.md.posted`, `codex/report-*.md.notified` and
`codex/goal-*.md.started` that the wave-notify tool writes beside a report or goal only after the
receiver accepted the report (`.posted`), a successful, non-degraded send (`.notified`) or an
accepted start (`.started`). Current
wave-notify writes `.posted` and no longer writes `.notified`; both remain readable as historical
completion receipts. A `.tmp.notified` or `.tmp.posted` name is not a receipt. A row holds `kind`
(`posted`, `notified` or `started`; migration `035_loop_receipt_posted.sql` adds `posted`), the absolute `path` of the receipt's target (the report or the goal), the receipt text
`content`, its mtime `receipt_mtime`, whether the target exists (`target_exists`, true also when it exists but could not be read, in which
case the hash and line are NULL), `target_sha256`
and, for reports only, `target_line1` (stored only when it is a loop header line, else NULL; the read of
the first line is bounded to 4096 bytes while the digest covers the whole file); plus `repo_origin`, the `owner/repo` of the checkout's
`origin` remote when parsable, and `seen_at`. No report or goal body is stored. The table is not
truncated by rebuild or analytics re-application. Work and Personal repositories are handled
identically. The collector role needs `SELECT, INSERT, UPDATE` on it, which the migration grants to
`ah_ingest` when that role exists at migration time (otherwise provision it with the other
collector tables), and never `DELETE`; readers of `ah.loop_run` receive `SELECT`.

A launch is finished with `end_evidence = 'completion_receipt'` when a `posted` or `notified`
receipt exists for its exact report path and

- its mtime is at or after `launch_ts` and before the next launch of the same report path
  (launches of one report path with equal `launch_ts` leave it unfinished; launches of other
  report paths, including other campaigns in the same `codex/` directory, do not bound it);
- no lane session of the loop starts after the receipt;
- if the content is `sha256:<64 hex> request <id>`, the report existed when collected, the digest
  equals `target_sha256` and `target_line1` is a loop header naming the launch's loop number (a
  digest receipt whose report is missing does not finish the loop); a legacy `request <id>`
  receipt counts on the exact path alone;
- a `posted` receipt's content must be `sha256:<64 hex> receiver <JSON object>` on one line, and
  is checked exactly as a digest `notified` receipt is. It never counts on the path alone.

`end_ts` is the earliest qualifying receipt mtime across machines. Only launches that have a
receipt and are not already finished are examined. A root that pings before moving its report into
place has no receipt, so its loop stays running or stale.

The finish is an observed end, not a claim that nothing happened afterwards. A lane session linked
to the loop that starts after the receipt blocks the finish whenever the loop is next evaluated, but
a loop is re-evaluated only when its root or one of its sessions is re-indexed, so a late lane not
yet indexed, or a root that sends the notification and carries on, leaves it finished. Re-tagging a
root or a rebuild drops `report_write` evidence for good (rows already carrying it stay finished
until then), so a loop with no receipt then reads running or stale unless replacement-launch
or valid exactly attributed state-close evidence establishes its end.

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
  `commits bigint` includes recorded commits/cherry-picks; `pushes bigint` counts recorded pushes,
  one per distinct pushed range and branch (a range reported twice is one push) plus each push
  recorded without a range. A ref range under a `From <url>` fetch header is not a push; only
  ranges under `To <url>`, or with no header, are read as pushes.
  No matching git event leaves the count NULL, rather than claiming none happened.

These project existing catalogue evidence, with the same membership convention as
`ah.v_loop_summary`: root calls/actions inside the launch window and linked lane sessions whole.
Running, waiting, paused and stale roots have no terminal cutoff for progress;
finished roots stop at the recorded end. Session
activity timestamps describe the newest observed root/lane event, not process liveness. There is
no new collector or heartbeat. NULL always means unknown, never an alias for zero.

Every successful refresh post-pass updates open rows (`running`, `waiting` and `paused`) and
roots of dirty root/child sessions, including heuristic lanes and descendants. Dirty-owner
selection uses `loop_run_progress_owner_idx`; it does not walk finished-loop history. Receiver
queries for open loops should include all three statuses, not only `running`. This bounds
progress selection, not the separate lifecycle and identity refreshes described above.
Initial historical fill processes at most 128 additional rows per pass with a transactional cursor
in `ah.meta`, so existing finished rows fill without rebuild and work stays bounded. No refresh
cadence is guaranteed; historical fill may therefore take several passes. Rebuild re-projects
retained transcript evidence. Analytics re-application
preserves values, and both operations preserve table-level consumer SELECT grants. Identity and
lifecycle meanings above are unchanged; consumers should still inspect `observed_at` for freshness.

### Planner and report signals

Migration `026_loop_planner_signals.sql` adds the collector tables `ah.backlog_done_event`, primary key
`(repo_slug, task_key, sha)`, and `ah.backlog_done_scan`, and three nullable `ah.loops` columns:

- `backlog_done_event`: one row per first-parent commit of the default branch (a merge counts at the
  merge, not at the branch's own commits) whose backlog task file has frontmatter `status` Done where
  its first parent had another status. Only frontmatter counts; a `status:` line in a task body does
  not. A task file created as Done, or moved while already Done, is not a flip. The collector reads
  the same window as `git_commit`, widens it once per repo to every stored commit (recorded in the
  collector table `ah.backlog_done_scan`, `repo_slug` and `widened_at`), and removes rows in the
  window whose commit the clone proves is no longer on the default branch. Rebuild keeps both tables.
- `tasks_done bigint`: distinct tasks with a Done flip in the loop's `repo` between `launch_ts` and
  `end_ts`, whatever session made the change. Running/waiting/paused windows are open; a stale loop's
  ends at its `end_ts`, the root's last observed activity. NULL when `repo` is unknown or the collector
  has not yet finished that repo's first Done scan (`ah.backlog_done_scan`). Recomputed on every
  refresh because collector rows can arrive after a loop finishes. Qualifying retained state
  evidence, and for a v3 run its counted closeout, supersede this legacy fallback as described below.
- `lanes_reported bigint`: all lanes in the `lanes` list of the latest captured report's
  `## Data`, read under the identity rules above. NULL when no report was captured, its Data is
  absent, invalid or names another identity, or has no `lanes` list. `lanes_total` is unchanged: indexed lane sessions.
  `lanes_accepted` now requires qualifying retained state evidence as described below; report
  Data supplies only `lanes_reported`, never a fallback accepted outcome.

`ah.lane.returns` (migration `040_lane_returns.sql`) holds every `lane-return` block of the lane, oldest first, as a
JSON array of `{at, source, event_uid, status, return}`. `source` is `report` for a block in the lane session's own
`subagent_report` messages, or `hand_back` for a block in a Claude subagent hand-back (`[Subagent hand-back]`,
`<agent-message from="<the lane's agent id>">`) relayed to the lane's parent as an `agent_message` or
`queued_prompt`: a lane resumed after a one-shot hand-back returns again only that way. A relayed block equal to
one already held is the same return. `ah.lane.return_status` and `lane_return` are the latest entry's.
A v2 or v3 block (`"v"` the JSON integer 2 or 3, with `lane` and `status` in
`complete|partial|blocked|failed`) is stored whole and gives `return_status` only when both are valid;
an object with any other `v` is stored whole with no `return_status`; the earlier free-form object (no
`v`) keeps any string `status`. The block's JSON is decoded, so a fence inside a string never ends it;
the closing fence may sit on its own line or end the JSON's last line, and CRLF line endings are
accepted. `ah.lane.lane_name` is the `lane` of the latest valid v2 or v3 block when the lane has one, else the
spawn name, agent path or agent type as before.

`ah.v_loop_summary.status` is the lifecycle status of `ah.loops` (`running`, `waiting`, `paused`, `stale` or
`finished`), and the launch-resolution `loop_run.status` only while no lifecycle row exists. Its `turns_human`
counts root `human_prompt` and `queued_prompt` messages inside the launch window except relayed agent messages
(`prompt_origin = 'agent_message'`, or text opening `<agent-message `). pi parent wake notices
(`PARENT_WAKE_TEXTS`, sent after a subagent notice) are `task_notification_summary`, not prompts.

`ah.v_notify_lag` has one row per completion notification a session received (message class
`task_notification_summary`, or source `subagent-notify` / `subagent-incremental-child-notify`):
`lag_s` is the wait to the session's next successful assistant model call, and `steered` marks
notifications that joined a turn already running. Notifications steered into a running turn open no
`task_notification` turn, so a turn-origin count under-reports them.

Session capture rules that changed with these columns (parser versions bumped, so a rebuild or
re-parse applies them): a `git commit`, `git push` or `git cherry-pick` that the successful command's
exit status proves ran and succeeded, but that printed no `[branch sha]` line or push range, yields a
`git_event` with `evidence = 'command'`, NULL `sha_short` and `event_uid` `<call>:<op>:cmd<n>`;
output-matched events are not duplicated. Nothing later in the same list or in an enclosing group may
absorb a failure (a later `||`, `;` or newline then another command, `&`, or a pipe without an earlier
`set -o pipefail`), and nothing under `!`, in a keyword compound, a command substitution or a function
body counts; here-document bodies are not commands. pi
`cacheWrite` is stored as `cache_write_5m` with `cache_write_1h = 0` (0 stays 0), so loop
`cache_write` and `priced_cost_usd` are known for pi loops. Codex `cache_write_input_tokens` is stored the
same way (absent means 0, `cache_write_1h = 0`); a zeroed breakdown beside a non-zero total stays NULL. pi's `loop-pi-runtime` custom entry
(`{v:1, variant, models:{<id>:{service_tier}}}`) sets `llm_call.service_tier` for calls to that model
in the same session, else NULL. A pi `Background task completed|failed: **<agent>**` notification
sets `completion_status` on the spawn row of the async launch named by its
`async-subagent-runs/<runId>` line.

### Retained state-log counts

Two strict body schemas share integer wire `v = 1`; selection is by the open event's fields,
not wire version or date. The original frozen schema uses `open.goal_sha256` and actors
`root`/`dispatcher`/`ext`/`daemon` as defined by that schema. The second pi schema is selected by
`open.charter_sha256`, requires `root = 'llm'`, `root_model` and `first_wave`, and permits
`by` values `root`, `ext` and `daemon`. Its frozen producer SHA-256 is
`052d7fd5702b849572c826906d0fe48c505eb5c2af8972ebb64477f85a6c440f`.
It rejects legacy-only dispatcher actors, envelope-source admits, gate events and gate/triage
dispatch kinds. Unknown events, unknown members and invalid types invalidate the snapshot;
this is a second validator, not a relaxation of the original validator.

In the pi dialect a valid open, first-wave task list, dispatch or return does not establish
admitted, landed, parked or accepted outcomes. Missing events remain independently NULL, never
invented zero. Explicit valid events project independently, with only `by = 'root'` lands proving
publication. Extension/daemon observations and a return's `landed` claim cannot prove a land.
Shared-position fingerprint conflicts still withhold the affected outcome; a conflicting open
withholds all outcomes. Close attribution and terminal proof retain the full-ledger agreement
requirement. The original dialect's valid-open known-zero policy is unchanged.

The detailed original-dialect rules below retain their historical meaning. References to
`open.goal_sha256` use `open.charter_sha256` for the pi dialect through the same compatibility
slot and exact origin/loop/digest attribution. References to root/dispatcher authority apply to
root alone in the pi dialect. Statements that an open establishes zero apply only to the original
dialect. See [count precedence](docs/loop-counts.md) for the independent v3 trailer fallback.

Migration `032_loop_state_counts.sql` adds `ah.loop_state`, a collector table keyed by
`(machine, path)`. The hourly collector reads exact `codex/state-*-loop<N>.jsonl` files from
explicitly configured repository copies, retaining complete UTF-8 JSONL `content`, absolute
`path`, filename `loop`, checkout `repo_origin`, `state_mtime` and `seen_at`. Contents are
unredacted; rebuild and analytics re-application preserve the table. Files above 16 MiB,
non-regular or unreadable files, invalid UTF-8 and concurrent rewrites are skipped, reported as
`loop_state` errors and retried without a partial snapshot. The configured checkout's canonical
root defines the source boundary; state files and their `codex` directory are opened through
anchored no-follow descriptors. File or directory links and replaced input identities are rejected.

Snapshots join only to already observed `repo`, `loop` and `goal_sha256`: origin matches repo
case-insensitively, the explicit filename loop label matches, and a valid frozen-v1
`open.goal_sha256` matches. Local copy paths need not equal transcript paths. State contents do
not establish identity or lifecycle root activity. A valid attributed root/dispatcher close can
establish completion under the outcome rules below. Explicit valid root heartbeat
records can supply live-phase activity through the exact attribution below, independently of counts. The open must lie at most 120
seconds before launch and before the next launch of that identity. An open attributable to more
than one same-identity launch, including overlapping skew windows or equal timestamps, supplies
no state override; proximity never chooses a root. Count events lie at or after launch and before the next
launch of that identity; a stale root timestamp is not a state-event cutoff.

With qualifying evidence, `lanes_accepted` counts distinct lanes on `ev=accept` events with JSON
boolean `accepted=true`. A non-empty explicit `lane` is authoritative. A task-only acceptance
identifies a lane only when that exact task has one distinct preceding dispatched lane in the
qualifying cohort; never choose a worker over a reviewer, use timing proximity or infer from a
return. Any unresolved true acceptance leaves `lanes_accepted` NULL, independently of known
landed-task counts. False and string-shaped booleans and return statuses do not add lanes. This
counts observed acceptance events, not final acceptance state: later rejection does not erase an
earlier true event. In the original dialect a valid open without true acceptance establishes zero for `lanes_accepted`.
The separate legacy `tasks_done` override requires a valid attributed closed state log, with
root/dispatcher close authority and agreement across machine copies. It counts distinct exact
tracker IDs on root/dispatcher `ev=land` events using the `tasks_landed` projection, never alias
lands, worker observations or a return's `landed` claim. In the original dialect a qualifying closed log with no such lands
establishes zero; in the pi dialect a qualifying close with absent lands supplies NULL, not zero.
Duplicate events and machine copies do not inflate counts. Without a qualifying closed-state
override, collected Done-flip task counts remain the unchanged fallback. See `docs/loop-counts.md` for precedence.
Invalid framing supplies no state override; torn non-events are ignored while original bytes remain
stored. Without qualifying state evidence, `lanes_accepted` is NULL. No state-only outcome column
takes the Done-flip fallback. `lanes_reported` and `lanes_total` are unchanged.

#### Observed v3 Task-trailer lands

For launches explicitly recorded with `loop_run.mode = 'v3'`, `tasks_landed` can also count
observed Git Task trailers when no qualifying retained state cohort exists. Authoritative state evidence,
including unknown outcomes from conflicting qualified snapshots, keeps precedence. A valid pi
cohort with no explicit root land and no invalid/open/land conflict does not assert a known land
count, so it permits this independent fallback. An invalid or conflicting cohort cannot be used
to claim that lands were absent. v2 launches
and the other state outcome fields retain their existing meanings.

The observed repository set is the authoritative `ah.loops.repo` plus explicit captured
`git_event.remote_url` values on the root or linked sessions inside the launch window. Remotes
are normalised with `ah.remote_slug`; collector repository slugs lose exactly their first
component, retaining owner/repo. Host is not part of this identity. No basename, inferred
`ah.repo.slug`, local file or uncaptured charter supplies repository evidence. This set describes
observed repositories, not proof that every charter repository has been collected.

Only `on_default` commits in the half-open launch window qualify. Its upper bound is the earlier
recorded loop end or following launch on the same root or exact report path. Equal-time launches
on that root or path are ambiguous and supply no trailer count. Exact task IDs are deduplicated
across repositories, collector copies and commits. No matching commit evidence, or any matching
commit whose trailers are still unknown, leaves the count NULL. Observed commits with known
empty Task-trailer arrays can establish zero. Collector-only refreshes recompute these counts,
including finished launches, without changing legacy `tasks_done` or deleting commit history.

Migration `037_git_commit_tasks.sql` adds nullable `ah.git_commit.task_ids text[]` without a
default or historical backfill. The collector retains distinct exact tracker IDs from Git's
terminal Task trailers, validating complete unfolded values rather than body mentions or aliases.
Existing rows stay NULL until rescanned; an observed commit without valid IDs supplies `[]`.
Metadata, statistics and trailers come from one snapshot per pass: the ref resolved once to a
commit and the window fixed once as an absolute cutoff. A commit missing from any of those reads
fails that repository's `git_commit` step and leaves its stored `ah.git_commit` rows and their
off-default reconciliation unchanged; it never supplies `[]` or zeros.
A neutral public alias (`Task: loop<N>-<lane>`, any case) is never a task, even when its lane is
numeric and so has the tracker shape: the collector drops it and the count ignores any already
collected. The private commit that consumes the work carries one `Task: <ID>` per delivered task,
and those IDs are the landed count.

#### V3 closeout delivery counts

The closeout is a v3 run's authoritative delivery record. For a `mode = 'v3'` run that is
`finished` with `end_evidence = 'closeout'`, a non-NULL collected `landed_count` sets both
`tasks_landed` and `tasks_done`, and a non-NULL `parked_count` sets `parks_total`. The counted rows
are the exact-path closeout snapshots with a qualified line 1 whose mtime lies in the run's
half-open window (bounded by the next launch on the same root or exact closeout path); they must
share one hash, and machine copies of that snapshot agree. This override is applied after the
Done-flip, state and Task-trailer projections on every refresh, collector-only passes included,
so it takes precedence over all three for that run. A NULL count leaves that column as the other
projections computed it. When the closeout end is revoked, the next refresh restores the ordinary
values; a v3 run whose repository identity is withdrawn has `tasks_done` NULL. Runs whose end
came only from a transcript write, v3 runs without a counted closeout, and v2 or legacy runs
keep the projections described above. The dialect is never selected by date.

#### State-only outcome normalization

Existing nullable `tasks_admitted`, `tasks_landed`, `parks_total` and `lanes_accepted` columns
project complete qualified wire-v1 state snapshots on every successful refresh, including closed loops
and collector-only passes. There is no new column, default, schema replacement or grant. Projection
uses the same exact origin, filename loop, frozen open-goal and launch-window attribution above.
No report, transcript append, return claim or local-file lookup is a state outcome fallback.
The separately observed v3 Task-trailer fallback for `tasks_landed` is described above. Missing,
invalid or ambiguously attributed state evidence leaves these numeric columns NULL, independently
of legacy transcript structure or progress telemetry. In the original dialect a qualifying valid open establishes known
zero, including when its envelope names tasks but no admit occurred. The pi dialect requires
explicit events and retains NULL for absent outcomes.

- `tasks_admitted`: distinct exact tracker IDs on explicit valid `admit` events. Envelope entries,
  dispatches, accepts and parks do not admit tasks.
- `tasks_landed`: distinct exact tracker IDs on explicit valid `land` events recorded by `root`
  or `dispatcher`. A worker return's `landed` claim is not publication proof. Duplicate lands do
  not add tasks; later `revert` does not erase historical publication.
- `parks_total`: cumulative valid `park` events, including operational aliases. Two parks of the
  same task at different sequence positions are two events. Machine snapshots of the same
  frozen event are one observation, never two parks.
- `lanes_accepted`: the cumulative distinct true-accept lane attribution above, unchanged by later
  rejection. Ambiguous task-only acceptance leaves this field NULL independently of the others.

Tracker IDs match `[A-Za-z][A-Za-z0-9]*-[0-9]+(?:\.[0-9]+)*`, including subtasks; comparisons retain
exact spelling. All other task values are operational aliases, retained in collected bytes but
excluded from admitted/landed tracker totals. Parks and lane attribution still observe aliases.
These counts normalize the canonical state log's events, not its live recovery fold's final task
state; accepting, rejecting, reverting or replaying a return cannot rewrite historical events.

A `close` requires a canonical reason (`nothing-admissible`, `owner-stop`, `blocked` or `budget`),
valid framing, an earlier frozen open and attribution to exactly one launch. Only `root` or
`dispatcher` observations establish this end. `status` becomes the existing enum `finished`, not
an inferred success/partial/blocked outcome. With no existing receipt/replacement end,
`end_evidence` is `state_close` and `end_ts` is the earliest qualifying close across machine copies.
Shared sequence positions must agree before a close is terminal. A torn close, conflicting copies,
wrong identity or overlapping open attribution cannot finish a launch. Lifecycle re-evaluation
starts from existing transcript/receipt evidence each pass, so a removed or invalidated state
snapshot cannot retain a state-only finish. Identity and root activity remain unchanged.

For the original body dialect, every complete framed event is validated against the frozen
public `bin/loop-state` v1 `validate_event` schema before observations are extracted. Its source SHA-256 is
`f44713f19942be7296feb377c79c6e9db4310c0796a0f4869dcd5ecadba00561`; the catalogue contains a local
frozen validator, with no runtime dependency on that CLI or a checkout. The additive optional
`return.artifact` field extends that baseline: it must be a nonempty absolute POSIX path without
NUL and is retained as event metadata, never dereferenced. Existing logs without it remain valid.
The producer supplies the resolved native package artifact path from correlated completion
metadata, not lane-authored text. It names package-normalized output, not raw provider bytes.
Compatible readers must be deployed before producers emit the field. All thirteen event types
retain their existing validation. Unknown events, unknown
members, missing required members and invalid types invalidate the snapshot, including unprojected
return, gate, watch and judgement events. Invalid heartbeat activity alone retains its established
independent unknown-activity policy; it supplies no activity and cannot erase independent counts.
Torn non-events remain ignored.

A complete framed-event fingerprint ledger is retained before projection, actor and launch-window
filters. Frozen `(open timestamp, seq)` identifies repeated snapshots; every shared position must
agree for terminal proof, including returns and non-root closes. Conflicting observations involving
an outcome event leave that count NULL rather than choosing a machine by mtime. Same-position
true/false accepts are contradictory copies and leave acceptance NULL, not a chronological rejection
of an earlier true accept. Dispatch conflicts also withhold acceptance attribution; unrelated counts
remain independent. A conflicting open withholds every outcome. Phase refresh uses these projected totals before producing its digest and cannot
overwrite them with incomplete transcript evidence. A state-only close is structurally closing;
historical projection does not itself authorise a paid call.

The migration copies existing receipt SELECT grants to the new table and grants only SELECT,
INSERT and UPDATE to existing `ah_ingest`. Provision these privileges if that role is created
later. Existing relation grants are unchanged.

### Deterministic live phase and retained summaries

Paid loop admission and execution are retired globally for every harness and configuration,
including `enabled = true`. Refresh creates no new paid jobs or reservations. `drain_one` claims
nothing and invokes no provider; `run_pass` does nothing before loading config or connecting.
The worker CLI retains its accepted arguments and interval validation but exits without a repeat
loop or worker. Old queued work is not resumed by repaired credentials, a new day or a rebuild.
There is no instruction to schedule a new worker.

Deterministic structure/phase projection, retained cache application and phase history may
continue. No table, column, historical queue/cache/budget/reservation row, grant or existing read
query is removed. Optional corpus embeddings and unrelated collectors are not retired by this
change. The historical paid rules below explain stored data, not current admission or execution.

Additive `ah.loops` fields project retained catalogue evidence from pi roots and linked
relaunches: successful complete append calls to the recorded state target, structured watches
and heartbeats, explicit subagent metadata and retained returns. Projection never opens a local
state file or uses a local path lookup to supply content or identity. Native pi single-agent
launches use the matched, successfully accepted call-entry dispatch timestamp described under
nullable transcript telemetry, not message generation, result completion or OS process timing.
The existing parser and Writer supply these rows; projection does not manufacture a spawn.
Accepted native `watch_start` results join terminal `loop-watch` observations by exact session
and watch id, never by label or timing proximity. Their expiry bound is explicitly derived from
call entry plus the recorded `deadline_s`, not a native process-start or deadline observation.
A terminal hook without a captured accepted start cannot establish an active watch. Native
`loop-wake` is activity, not heartbeat. Heartbeats come from explicitly timestamped native
`loop-heartbeat` evidence or collected `ah.loop_state` records with `ev=heartbeat` and a valid
UTC activity `at`. State heartbeats reuse the exact origin, filename loop and frozen open-goal
attribution described above; activity and append-observation times must both belong to that
launch window. Duplicate machine copies do not inflate heartbeat activity. A heartbeat-only
root can receive a live phase without inventing lanes, open structure or known counts.
Missing, truncated, failed, opaque or ambiguously attributable append or watch evidence leaves
the affected transcript structure unknown independently of enrichment. Outcome totals use the
complete collected-state normalization above, independently of this transcript structure.
Projection still uses only collected bytes, not a local state-file lookup; lifecycle root activity
is unchanged and completion follows the exactly attributed close rule above.

A run with an attributed pi-dialect (v3) state cohort takes its live structure from that log alone,
whatever the root harness: the loop-state extension writes dispatch, return, watch and heartbeat
events itself and the root writes none by shell, so transcript appends, native watch records and
spawn synthesis are not read for it. Machine copies of one event are one observation; an invalid
copy leaves the structure unknown and disagreeing copies leave the affected kinds unknown. Root
`judgement` and `park` events in that log supply `last_judgement` and `last_park`. A finished v3
run whose end evidence is its closeout, completion receipt or report is `closing`. Outcome counts
are unchanged: absent v3 admits, parks and lands stay NULL.

`live_phase` is nullable and uses preparing, working, reviewing, gating, landing, waiting or
closing. `phase_since` is the observed transition time. The historical hybrid combined deterministic
lane/timing evidence with retained Jev yes/no decisions; an explicit unexpired watch overrides nominal
stale lanes and preparation. Stop/deadline retires the watch. Heartbeat evidence is deduplicated
to one per five minutes. Independent nullable `jev_phase` and `jev_phase_probs` retain the whole
phase decision. Jev results are accepted only when their authoritative returned model is
`jev-1.13.0`, never an inferred alias or a request-field echo.

Nullable structure includes `active_lanes` (lane/task/title/agent/started_at), `last_gate`
(sha/scope/exit/at), `last_park` (task/needs/reason), `last_judgement` (at most 500 characters),
`last_judgement_at` and explicit `ops_state`. Unknown stays NULL; an observed transcript open
establishes known empty structure. `parks_total`, `tasks_admitted` and `tasks_landed` instead
require a qualifying collected open and valid events under the state-only outcome rules above.

The JSONB producer shape below matches the receiver's nullable member validation. Validation
never coerces a number, boolean, array or object to text, truncates an overlong member or emits
only a prefix of an oversized lane list. Source transcript content remains stored unredacted.
All emitted objects carry every listed key, even when its value is JSON null; absent or explicitly
null source members become JSON null, not empty text or zero. Unknown containers are SQL NULL.
No unlisted members are emitted.

| Field | Container and limits | Members |
|---|---|---|
| `active_lanes` | JSON array of at most 64 objects; the whole field is SQL NULL if the complete observed active set exceeds 64 or cannot be attributed. Never an object or an array containing non-objects. An observed open with no dispatches, or all observed dispatches returned, establishes `[]`; absence of evidence is NULL. | `lane`, `task`, `title`, `agent`: string of at most 1000 Unicode characters each, otherwise JSON null. `started_at`: UTC timestamp string as described below, otherwise JSON null. |
| `last_gate` | One JSON object for the latest attributable gate observation, otherwise SQL NULL. Never an array or scalar. | `sha`, `scope`: string with no length or hash/enum format restriction, otherwise JSON null. `exit`: JSON integer in signed Int32 range (`-2147483648` to `2147483647`), otherwise JSON null; booleans, floats and numeric strings are not integers. `at`: UTC timestamp string as described below, otherwise JSON null. |

Strings must be Unicode scalar text encodable as UTF-8 without NUL, matching JSONB and receiver
storage. Empty strings are valid; limits count characters, not UTF-8 bytes. Timestamp members
use the Gregorian RFC3339 microsecond UTC profile: `YYYY-MM-DDTHH:MM:SS.ffffffZ` with exactly
six fractional digits. Recorded timezone-aware instants are converted to UTC without guessing a
timezone, adding precision or consulting the indexer's clock. `started_at` is the observed dispatch
time; `last_gate.at` is the observed gate-result time. An unknown or invalid timestamp remains
JSON null. Canonical UTC output is accepted by the receiver's broader microsecond/offset profile
and needs no consumer alignment change. Member nulls do not invalidate the remaining object;
invalid members never reach `ah.loops` verbatim.

### HISTORICAL paid enrichment and stored data

This section specifies retained paid results and the pre-retirement worker. All admission,
queueing, inference, retry and scheduling descriptions here are historical only. Current refresh
and worker entrypoints cannot authorise or execute them, even with enabled configuration.
Deterministic projection and retained cache application are the exceptions described above.

`headline` is at most 120 characters; `summary` is at most 600 characters and two to four
sentences. Generation time/model/error and `final_summary` accompany them. Failure retains the
last successful text. Full provider replies, including reasoning, are retained unredacted in
the independent paid cache. `ah.loop_phase_event` records launch/time/phase/source/headline with
a surrogate id for equal timestamps and a cascading launch foreign key. Only observed phase
transitions or changed headlines append history. Rebuild clears derived history, reprojects
retained evidence and restores cached summaries without another paid call.

Refresh only projects database structure and queues one changed digest per loop/transaction,
including explicit close. Meaningful phase/watch/quiet-bucket changes, not an exact minute
schedule, admit work; closed loops do not keep ageing the quiet bucket. Heartbeat timestamps alone
do not change the paid digest. Historical structural projection may restore existing cache but
never itself authorises paid work: admission requires a selected running loop or a newly observed
explicit close of an already tracked/live-admitted loop. Admitted closes take priority.
The complete digest is
bounded to 12,000 UTF-8 JSON bytes and the complete serialized inference request to 32,768 bytes;
oversize is a visible error, not a partial lane list or transcript dump.

Inference requires a separately scheduled worker on an idle autocommit connection, never inline
on the collector. Results apply on later refreshes, including completed jobs on closed loops.
Interrupted claims acquire a visible timeout error after five minutes. The collector marks only
the exact completed outcome or deferral it observed before reading replies; a later completion
stays unapplied for the following refresh, including on closed loops. Worker updates bind their
exact claimed job identity. Unpaid auth and daily-cap refusals are recoverable deferrals in
restricted queue metadata, not permanent cache entries. Unchanged unavailable prerequisites do
not retry; admission may resume only when auth/routes become available or the refused UTC day
advances. A successful paid step is reused when the remaining unpaid step is deferred.
Committed reservations with unknown results remain terminal/uncertain: no inference retry or
refund on token/day changes, missing usage, timeout or rebuild. Public `summary_error` contains
finite reason/status labels only; full unredacted provider errors stay in restricted cache.
Before retirement, enabling enrichment required configured routes and a separately scheduled
bounded worker. It must not be scheduled to resume paid work under the current contract.

A catalogue-global atomic UTC-day USD 5 ceiling uses independent budget/reservation tables that
survive refresh and rebuild. Reservations commit before calls and are never refunded, including
missing usage, timeout or crash. Jev reserves 65,536 input tokens at USD 0.042/M with free output.
Summary reserves the full 1,048,576-token context at peak USD 0.30/M uncached input and at most
2,048 generated tokens (including reasoning) at USD 1.20/M. Both include a 1.05 fee allowance.
This deliberately coarse summary ceiling allows about fourteen complete enrichments per day;
the request-byte cap alone is not proof of a tighter tokenizer/framing ceiling. Development and
evaluation calls are exempt from production admission.

No existing reader grant or SELECT is widened. Consumers use additive `ah.loops` columns under
the existing grant. Migration 029 revokes inherited/default reader and PUBLIC privileges on only
`loop_live_job`, `loop_live_cache`, `loop_live_budget` and `loop_live_reservation`; existing loops,
history and other table grants stay unchanged. Paid tables have no
loop/session foreign keys, so derived-data rebuild cannot cascade into them. Identity,
collector-key and paid-embedding-cache contracts are unchanged. Lifecycle uses the additional
exactly attributed collected-state close evidence above.

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

The collector writes `task_prefix`, `backlog_task`, `backlog_done_event`, `backlog_done_scan`, `collector_mutation_audit`, `git_commit`,
`git_commit_file`, `ci_run`, `installed_feature`, `permission_log` and `loop_run_metadata`. Git subjects, tracker titles, labels and project values,
file paths, repository slugs, workflow names and installed-feature names are stored verbatim.
The collector also writes `loop_state`: complete unredacted JSONL snapshots, including judgement
text, protected like transcripts. They supply progress counts and explicitly attributed root
heartbeat activity for live phase and exactly attributed valid close evidence for completion,
never identity or inferred lifecycle root activity.
Author emails are compared to configured identities but only `author_is_owner` is stored.
Permission logs contribute timestamps, line hashes, tool names, command verbs, classifier reasons
and sub-agent flags, never command arguments or target text. New run metadata contributes only
hashes, qualified closeout line 1, mtime, origin and two closeout task counts alongside its key,
never charter/closeout bodies, titles or task IDs.
Legacy `loop_receipt` rows retain receipt text, qualified report line 1, hashes and origin for
historical reads; new receipt collection is retired globally. These metadata fields may themselves
be sensitive. Protect collector homes, configuration and the catalogue accordingly.

Repositories and homes are explicitly configured. A failed CI query is an attributable collection
error, not a failed or successful CI run. Other repositories continue independently. New metadata
or state inputs that cannot be read or change during collection are skipped, reported in the
run's `errors` and retried next run; like other step errors they do not fail the run (only
`gh_unavailable` does). Feature
snapshots reconcile removed features; task removal and off-default git flags require a successful
remote fetch. The collector changes remote-tracking git refs, never the working tree or tracker.
The hourly collector validates the server's session and current role as dedicated `ah_ingest`,
rejecting administrative privileges and catalogue-owning role membership before collection.
The guard completes its transaction before writes so each metadata write can commit normally.
The pre-existing writer/indexer `collect-git` path accepts the indexer's connection separately.

Migration `030_collector_mutation_audit.sql` adds the collector table `ah.collector_mutation_audit`.
Each proven off-default reconciliation appends its table name, operation, full natural key in
`row_key`, repository slug, compared ref and resolved commit SHA, and transaction timestamp
`deleted_at`, before mutation in the same transaction. `operation = 'off_default'` records the
existing `git_commit.on_default = false` UPDATE: the commit and its file rows are retained, not
deleted. `operation = 'delete'` records the actual `backlog_done_event` DELETE. For an off-default
UPDATE, `deleted_at` is the reconciliation timestamp, not evidence of physical deletion. Audit
and mutation commit or roll back together; only exact locked rows are mutated. Unknown commits,
on-default commits, unsuccessful fetches and dry runs produce neither mutation nor audit.
Rebuild preserves this table. The dedicated collector receives SELECT and INSERT on it, never
UPDATE or DELETE; a role provisioned after migration needs those permissions and USAGE on its
identity sequence.

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
