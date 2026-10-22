# agent-history

Index your coding-agent transcripts into a Postgres (ParadeDB) catalogue, then search them, trace
what an agent did and see how a long-running root spent its model calls.

It reads the session files that Claude Code (`~/.claude/projects`), the Codex CLI
(`~/.codex/sessions`) and the pi coding agent (`~/.pi/agent/sessions`, including pi-subagents
children) already write, and loads them into one schema:

- sessions, turns, prompts, replies, reasoning, sub-agent briefs and reports, compaction summaries;
- every tool call with its full input and output, every model call with its token counts;
- sub-agent spawns linked to the child sessions they started, loops and lanes, git commits and
  pushes seen in tool output, file touches, errors classified by kind;
- BM25 full-text search over the text surface and the tool I/O (ParadeDB `pg_search`), with
  optional vector search if you configure an embeddings endpoint;
- an efficiency classifier: every model call attributed to what triggered it (a prompt, a tool
  result, a status look, a wait that timed out, an injected event), so a root that polls shows up;
- a read-only MCP server so an agent can query its own history.

**Read [CONTRACT.md](CONTRACT.md) before you point this at real transcripts.** The catalogue stores
full tool input and output, system prompts and everything else in the transcripts, unredacted.
Anything an agent read or printed, including secrets, ends up in the database.

## Quick start

Needs Docker with Compose. The template builds the application image and keeps ParadeDB on the
internal Compose network, with **no host database port** and no other published port. Metrics,
traces and logs leave only over OTLP, when you configure an endpoint (see [Metrics](#metrics)).

Copy `.env.example` to `.env` and fill every required value: `POSTGRES_PASSWORD`,
`AH_WRITER_PASSWORD`, `AH_READER_PASSWORD`, `AGENT_HISTORY_DSN`, `AGENT_HISTORY_READER_DSN`,
`TRANSCRIPTS_ROOT` and `AGENT_HISTORY_CONFIG_FILE`. Use long random passwords. The DSNs use the
Compose service `db`, port `5432`, database `agent_history` and the corresponding writer/reader
passwords (URL-encode passwords when necessary). The two mount variables must name an existing
absolute directory and a readable TOML file on the host. Keep `.env` private and out of version
control.

Prepare a transcript tree with agent homes below it, for example `claude/`, `codex/` and `pi/`.
The directory is mounted read-only as `/transcripts`. Create the config file named by
`AGENT_HISTORY_CONFIG_FILE` with container paths, not host paths:

```toml
[sources]
claude-local = "/transcripts/claude"
codex-local = "/transcripts/codex"
pi-local = "/transcripts/pi"

[embedding]
enabled = false

[exporter]
state_dir = "/state"
```

Bootstrap the catalogue before starting the periodic workers:

```sh
docker compose config --quiet              # validate required environment and mounts
docker compose up -d db
docker compose run --build --rm indexer init
docker compose run --rm indexer index      # configured read-only /transcripts sources
docker compose run --rm indexer search "flaky test"
docker compose run --rm indexer efficiency
docker compose up -d indexer
```

`index` is incremental: it remembers how far it read each file and only parses new bytes. The
indexer repeats every 60 seconds by default, and BM25 indexes are created after the first load.
Embeddings are off by default; do not start the embedder unless you have explicitly configured and
approved an embeddings endpoint. This is a local deployment template, not evidence of a live
cutover or successful deployment.

## Configuration

Optional, in `~/.config/agent-history/config.toml` (or `$AGENT_HISTORY_CONFIG`); see
[config.example.toml](config.example.toml). The parts that matter most:

- **sources**: which agent homes to read, as `namespace = "directory"`. The namespace prefix
  (`claude-`, `codex-`, `pi-`) picks the parser. Several homes of one agent are several namespaces.
- **contexts**: named search scopes, each a set of namespaces. They filter search results, but are
  not an access-control boundary: a database reader can query every row through `sql` or another
  context. For real separation, use separate databases; reader roles on the same tables do not
  isolate rows without row-level security, which this package does not implement.
- **identities** and **git**: your email addresses and the local checkouts whose history
  `agent-history collect-git` ingests, so `why <sha>` can join a commit to the session that made it.
  Migration 021 removes the legacy owner alias; move all catalogue readers and writers to `author_is_owner` before deploying it.
- **embedding**: off by default. When enabled, the indexer sends chunks of `human_prompt`,
  `queued_prompt`, `assistant_text`, `subagent_brief`, `subagent_report` and `compaction_summary`
  messages, session-summary title/objective/narrative text, and failed tool-call/tool-op error
  excerpts to the configured embeddings endpoint. Each input includes a header with its class/type
  and a cwd basename (or namespace when absent; summaries use the project basename). Pattern-matched
  secret-shaped spans are replaced with `[REDACTED]`; other sensitive text is not scrubbed.

## MCP server

The stock application image does not install the optional `mcp` dependency. Only if you build
an image with that extra installed can you use `docker compose run --rm -T indexer mcp` as the
stdio command from the project directory with the internal reader DSN. For a separately
managed database, install the CLI with `uv tool install '.[mcp]'` and register
`agent-history mcp` with `AGENT_HISTORY_READER_DSN` pointing at that database. The template does
not publish a database port for a host CLI. Tools: `search`, `find_sessions`, `session`, `why`, `touched`, `loops`, `active_sessions`,
`infra_actions`, `efficiency`, `sql` and `schema`.

Registering this server gives the connected agent read access to every database row, including every
stored tool output and any secrets captured in transcripts. The connected model provider may receive
that content when the agent puts returned tool results into its context. Contexts do not limit the
`sql` tool. With embeddings enabled, hybrid `search` sends the raw query (up to 12,000 characters,
without redaction) to the configured embeddings endpoint; `mode="bm25"` does not send a query there.

The server refuses superusers, schema or table owners, roles with write privileges on the
catalogue, roles with membership in PostgreSQL's `pg_execute_server_program`,
`pg_write_server_files` or `pg_read_server_files` roles, and roles without
`default_transaction_read_only = on`. Use the `ah_reader` role the compose file creates
(`src/agent_history/sql/roles.sql`). The `sql`
tool runs one statement per call in a read-only transaction with a 30 second timeout; the server
rejects a second statement.

## The efficiency classifier

`agent-history efficiency` (and `ah.efficiency()` / `ah.efficiency_calls()` in SQL) attribute every
model call to the event that last changed the agent's state before it: `user`, `model`, `work`,
`status`, `wait`, `event`, `noop`, `orchestrate` or `agent_msg` (definitions in
`src/agent_history/sql/efficiency.sql`). A root whose calls are mostly `wait` and `status` is
polling instead of waiting on events. It classifies Claude Code, Codex and pi. Exact per-call
ordering needs result offsets and Claude record origins populated by a rebuild; older catalogue rows
without them cannot establish the same-millisecond ordering. A rebuild cannot recover transcripts
that have already been deleted (see CONTRACT.md). Optional `[cold_sources]` preserves cold-only history on rebuild; run `agent-history rebuild-check` for read-only tier counts and refusal decisions before rebuilding.

The package ships the catalogue, search, classifier and application container. The Compose
template is for local deployment.

## Loop recognition and lifecycle

Inline root launches recognise both historical report targets and v3 closeout targets. A v3
launch uses `codex/closeout-<name>-loop<N>.md`, its sibling charter and an explicit charter digest;
it needs no date cutover or `Contract` declaration. Existing report tokens keep precedence,
including ambiguous-target rejection. Recognition is pure for inline text, not a lookup of files
on the indexer's machine. The compatibility columns remain `report_path`, `goal_path` and
`goal_sha256`, meaning closeout path, charter path and charter digest for `mode = 'v3'`.

Qualified closeout evidence can finish a v3 run, and its collected Landed and Parked task counts
then supply that run's `tasks_landed`, `tasks_done` and `parks_total` ahead of every other
count source ([count precedence](docs/loop-counts.md)). Retained state has two strictly validated body
dialects, both still wire version 1; the pi charter/first-wave dialect does not invent zero
outcomes when events are absent. Observed Git Task trailers provide an independent v3 landed-task
fallback, not proof of complete charter coverage. `WAITING` and `PAUSED` root lines can project
`waiting` and `paused` with nullable `waiting_until`; expiry or new root activity restores the
ordinary lifecycle, and terminal evidence wins. These are observations, not process liveness.

Paid loop enrichment is retired globally, even with an existing enabled configuration. Refresh
admits no new paid jobs, draining claims nothing and the worker CLI exits without running a
worker. Historical summaries, queues, budgets, reservations, phase history, columns, grants and
read queries remain; deterministic projection can still run. This retirement does not disable
optional corpus embeddings or unrelated collectors. See [CONTRACT.md](CONTRACT.md) for exact
evidence and historical compatibility rules.

### Live loop tasks for a narrow consumer

`ah.live_loop_tasks('owner/repo')` returns a JSON document listing that repository's running,
waiting and paused loops, each with its number, launch time, status, `waiting_until`, the count of
lanes whose return is not yet recorded and the tasks those lanes are on. A lane's task comes only
from the first line of the root's brief, `Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded`;
a brief without it is a live lane with no task. The document carries nothing else from the
catalogue. Linking and the per-harness limits are in [CONTRACT.md](CONTRACT.md).

The function runs with its owner's rights, so a role that can execute it needs no table access,
and it answers a narrow role only for the repositories that role is scoped to. To give a build job
such a role on an existing catalogue, let the indexer apply migration 043 (any `agent-history index`
or `init` does), then as a superuser:

```sh
psql -X -v ON_ERROR_STOP=1 -d agent_history -v repo=owner/repo -f src/agent_history/sql/live_loop_tasks_role.sql
psql -X -d agent_history -c '\password ah_loop_tasks'
```

Run the first command again with another `-v repo` to add a repository; without `-v repo` it adds
none. The migration also grants EXECUTE to the role if it already exists. The script refuses to
finish if the role could read any `ah` relation. Connect as `ah_loop_tasks` and run
`SELECT ah.live_loop_tasks('owner/repo')`; another repository is refused with SQLSTATE 42501 and
anything else in `ah` is refused. `agent-history loops --live --repo owner/repo` prints the same
document with `--format json`, or one line per loop and task otherwise.

`ah.live_loop_detail('owner/repo')` (migration 044) returns a richer document for the same role
and scope: per open loop, lane counts, the charter's first-wave tasks and every other task the
loop's lanes name, each with a state (`landed`, `running`, `blocked`, `returned` or `planned`),
the 20 newest lane events and the 10 newest commits that land one of the loop's tasks. Re-run the
role script once after migration 044 if the role predates it and the migration did not grant it.
The exact shape is in [CONTRACT.md](CONTRACT.md).

## Metrics

The container image runs two periodic processes from the same image: `index --every 60` indexes the
configured sources and `embed --every 60` processes pending chunks. Set the three database
passwords, writer and reader DSNs, `TRANSCRIPTS_ROOT` and `AGENT_HISTORY_CONFIG_FILE` in your
environment before running `docker compose -f compose.yml up --build`. Never store passwords in the
compose file.

Metrics are OTLP only; nothing serves `/metrics`. The periodic indexer also hosts the metric
collectors, on their own thread and at their own cadence (`[exporter] refresh_interval`, 15 seconds
by default): a slow or failing index pass never stops metrics, and a failing collector never fails an
index pass. Collection runs only when the optional `otel` extra is installed and
`OTEL_EXPORTER_OTLP_ENDPOINT` (or the metrics-specific endpoint) is set; otherwise nothing is
collected and no collector state is written. A one-shot `index` never collects. If collection cannot
start, the indexer keeps indexing, prints `metric collection disabled (config)` and emits a
`telemetry.configuration.invalid` OTLP log; the `agent-history-metrics-stale` alert rule catches a
collection that is absent or no longer completes runs.

Each periodic iteration prints one line: `<command> succeeded`, or `<command> failed (<category>);
retrying`, where the category is fixed (`config: <key>` names the config key at fault, e.g.
`config: dsn_file`, never its value or path; then `database`, `io`, `provider`, `status` or `error`).
An index pass that could not read its optional `[litellm]` usage source still succeeds and prints
`index succeeded (usage unknown: litellm.<stage>)`.

The `[exporter]` table keeps its historical name and configures this collection; its `listen` key
is accepted and ignored. Set `state_dir = "/state"`: the state volume preserves the collectors'
counter offsets and efficiency state through restarts, and must be writable by the indexer. Set
`[exporter] collectors = ["archive", "catalogue", "self"]` explicitly when using only these
three collectors. The default collector set also includes `efficiency`, which **reads transcript
contents** from the configured sources to classify model calls. Archive collection reads file
metadata and receipts, and catalogue collection reads database aggregates; those two collectors
do not read transcript content. Archive roots (`hot`, `cold`, `incoming`, `conflicts`) are optional
and must be mounted read-only if set.

The indexer publishes every `agent_history_*`, `agent_sessions_*` and `agent_efficiency_*` family
through the OpenTelemetry meter as service `agent-history-index`, as the mapping in
`docs/otel-design.md` describes. Use `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=CUMULATIVE` or
leave it unset; any other value switches metric publication off. A label value outside its producer's
vocabulary is refused, never rewritten. Each collection also renders the same samples as Prometheus
text in process, which the tests use as the proof format: `just otlp-parity` runs one synthetic
collection, decodes the real OTLP request and compares every mapped family against that text.

A backend may translate names. Grafana Cloud maps `service.name` to the `job` label and appends a
unit suffix to some names: a gauge with unit `1` gains `_ratio` (`agent_history_run_success_ratio`),
`agent_efficiency_wait_timeout_ms_total` becomes `agent_efficiency_wait_timeout_ms_milliseconds_total`,
and the legacy histogram components gain `_total` (`agent_efficiency_lane_seconds_bucket_total`).
The alert rules in `alerts/grafana-managed/` select those stored names.

Non-string pi model values are treated as missing: the model remains unknown unless a valid string was previously recorded.
At the public exposition boundary, `model` label values are restricted to an explicit literal
allowlist of public model identifiers. Unlisted values become `other`, and empty values become
`unknown`; persisted historical collector series pass through the same boundary. Counters are
reset-adjusted durably by original source identity before privacy-mapped labelsets are summed.
Histogram buckets, sums and counts follow the same policy. Original pre-privacy counter offsets
are retained, and temporarily absent sources keep their already-counted contribution, so restarts
and source disappearance/reappearance do not reset or double-count the public total.
Loop offsets are removed only on an explicit successful collector retirement after the retention window; a falling raw is a reset except when the previous non-integer raw exactly equals the current raw rendered to 12 significant digits (the legacy rounding alias, which cannot distinguish a genuine reset to that same alias).
Standalone archive namespace values become `<agent>-standalone`, and their machine values become
`other`; counts and byte totals are summed, and modification-time extrema are retained. Metric
family names, help, types and label keys do not change. An optional private `[metrics_labels]`
table can trust exact `machines` and `models` values for verbatim exposition (see
`config.example.toml`). Absent, empty or malformed tables grant no extra trust. This section is
loaded by the shared CLI/MCP loader; it does not relax other config validation. Configured archive
namespaces emit zero file/byte series for empty hot and cold tiers.

Efficiency counters read Claude session transcripts and their direct subagents, Claude workflow
agent transcripts (`subagents/workflows/*/agent-*.jsonl`), Codex `sessions/` and pi sessions.
Workflow journals and Codex `archived_sessions/` copies are never counted. Set
`[efficiency] workflow_transcripts = false` to leave workflow agents out, for example to compare
with a collector that never read them; turning it back on adds their calls from that point.

Each collection parses transcripts for at most 20 seconds, smallest remaining work first; lines
left unread keep their offset and are parsed by the next collection, so a long first parse delays
counts rather than metric freshness. A complete record the parser cannot read is skipped and
counted in `agent_efficiency_malformed_records_total` (`agent`, `namespace`; zero until one is
seen, no record content), stored under that same name.

Self-health keeps `storage`, `archive`, `efficiency` and `loops` separate from worker `runs`.
`loops` times the optional catalogue loop-map fetch: a failed fetch sets its success to zero,
records duration and retains its last-success timestamp, without failing overall collection.

This is not general metadata sanitisation. Other operator-configured namespaces, loop names and
collector state can be sensitive. Local collector and counter state retain original source
identities, including transcript-derived model identifiers and paths; protect the trusted state
volume as well as the transcript mounts and database. Periodic index/embed logs
discard nested command stdout/stderr and use only fixed status/error categories, without raw
exception messages. `SystemExit` still exits rather than retrying, but its payload is replaced
with a numeric status; interrupts and other `BaseException` types still escape. Interactive
one-shot commands retain detailed diagnostics and should be run
and captured only in a private environment.

## Development

```sh
just setup      # dev virtualenv and the leak-gate git hooks
just check      # format, lint, unit tests, leak gate
just ci         # check plus the database tests against a throwaway ParadeDB container
```

Test fixtures are synthetic. Never add a real transcript to `tests/fixtures`.

### Isolated telemetry environment

The test suite never inherits OpenTelemetry settings from the shell that runs it. `tests/conftest.py`
removes every `OTEL_*` variable when pytest starts, so a shell that exports an OTLP endpoint, headers or a
service name cannot start exporters in a test process or in a subprocess a test spawns, and nothing can
export to an inherited destination. A test that covers telemetry sets its own variables with
`monkeypatch.setenv` (or builds an explicit subprocess environment) and points them at a loopback receiver
it starts itself, never at a real endpoint. `tests/test_hermetic_telemetry_env.py` pins this behaviour. Run
`just check` as usual from an instrumented shell; no variable needs unsetting first.

## Licence

MIT, see [LICENSE](LICENSE). ParadeDB is AGPL-3.0; this project talks to it over the network as a
separate service and neither includes nor links its code.

## Collector

`agent-history collect` and `agent-history-collect` run the metadata collector once. Use
`--dry-run` without a database, or `--rescan-days N` for an explicit wider git rescan. Configure
`[git].repos` as explicit checkout paths and `[identities].git_owners` as allowed `host/owner`
strings. Unlisted owners, forks and unknown fork status are skipped. The collector fetches the
remote default branch before reading git objects; it never edits the checkout or its tracker.
Independently of that classification, it collects `codex/charter-*-loop<N>.md` and
`codex/closeout-*-loop<N>.md` into the metadata-only `ah.loop_run_metadata` table: whole-file
hash, qualified closeout line 1, mtime and explicit checkout origin, never document bodies. A
qualified closeout also records two integers, the distinct task IDs opening a list item or table row
under its `## Landed` and `## Parked` headings (migration 041); titles and those IDs are not stored.
A charter records the ordered task IDs opening the rows under its `## First wave` heading
(migration 044) and nothing else from its body.
Migration 038 adds this table and the narrow metadata grants for an existing `ah_ingest` role;
provision those grants separately when creating the role later. Exact state JSONL snapshots
continue to populate `ah.loop_state`. This does not copy charter or closeout bodies to a transcript
archive or NAS. See [the loop contract](CONTRACT.md#live-loop-lifecycle-ahloops) and
[count precedence](docs/loop-counts.md).

Legacy wave-notify receipt collection is retired globally, not merely disabled by default.
Existing `ah.loop_receipt` rows and historical read projections remain available; no upgrade
receipt scan is required. Git, task, feature, permission, journal and archive collectors retain
their separate existing behaviour. GitHub CI requires an authenticated `gh` CLI. A failed Actions request is reported against its
repository and does not create a CI observation or prevent collection of other repositories.

`[collector].homes` maps feature/permission-log home paths to namespace names. Feature locations
are the homes' skills, plugins and MCP configuration; permission logs are `logs/permission-denied.jsonl`
below each configured home. Optional keys are `machine`, `repo_contexts` (slug to context),
`lock_file` and `journal_db`. Machine defaults to the local machine name, with
`AGENT_HISTORY_MACHINE` as an override. There are no implicit repository or home scan roots.
The collector DSN comes from `--dsn` on `collect`, then `AGENT_HISTORY_INGEST_DSN`,
`AGENT_HISTORY_DSN` or the configured `dsn`. The legacy `AGENT_HISTORY_INGEST_ENV` PG-variable
file is also supported when no DSN is configured. Both hourly entry points require session and
current identity `ah_ingest`, without administrative privileges, schema/table ownership or
membership in owning roles. Provision it separately with only the metadata-table privileges
needed by the collector. This check does not apply to the separate writer/indexer `collect-git`
command, which uses the indexer's existing connection.

## Reader and MCP

The reader commands retain their arguments and aligned/CSV/JSON/expanded output via
`agent-history --format csv search QUERY --mode bm25 --since 7d`. `search` keeps the reader's
hybrid/BM25/vector meanings; the former indexer BM25 command is now `bm25-search`. Reader commands
use `AGENT_HISTORY_READER_DSN` or configured `reader_dsn`; a legacy PG-variable file may be
selected with `AGENT_HISTORY_ENV`. Install `psql` for reader commands. Context and repository
lookup come from the package configuration, with `AGENT_HISTORY_CONTEXT` and
`AGENT_HISTORY_NATIVE_CONTEXT` available to select search and native resume-home contexts.
When resolving a reader connection service, `connect_timeout` takes precedence from the explicit
DSN, then the service file, then `PGCONNECT_TIMEOUT`, then a five-second default. Explicit long
values and zero (unlimited) are preserved. The default bounds libpq's connection handshake per
host/address, not DNS resolution or the total time across multiple hosts. Resolution runs in a
child with an isolated environment; credentials use pipes, never command arguments, and failures
report a generic error without connection details.

Run `agent-history mcp` or `agent-history-mcp` after installing the `mcp` extra. The tool set
includes `search_summaries` and `task`, retains explicit `namespaces` overrides, and includes
`efficiency`. The existing plain-reader role checks and single-statement SQL guard remain in force.
Contexts are search scopes, not access-control boundaries. For compatibility, `session`, `why`,
`touched`, `loops` and `task` keep the reader's unscoped defaults; an explicit `context` on the
first four opts into context filtering. The optional legacy embedding adapters
read `AGENT_HISTORY_EMBED_ENV`; OpenAI/Cohere gateway routes require `EMBED_BASE_URL` alongside
`EMBED_PROVIDER`, `EMBED_MODEL` and `CF_AIG_TOKEN`. Workers AI also reads `CF_ACCOUNT_ID` and optional
`CF_AIG_GATEWAY_ID`. Otherwise query embedding uses `[embedding]` from the public config.

`agent-history loop <id>` prints one run's summary and lanes. The argument may instead be the
run's absolute closeout path; when several launches share that closeout (a relaunch), they are
shown together and the output says so. `agent-history loop <id or closeout path> --usage` prints
a short Markdown block for a v3 root to paste into its closeout Notes: an `as of` UTC timestamp,
wall time, LLM calls, the list-price cost total and a cost and calls table by role (root, then
each lane role from `ah.lane.role`, `unattributed` for linked sessions without a lane) and model.
Scope follows `ah.v_loop_summary` (root calls inside each launch window, linked sessions whole)
and prices follow `ah.v_session_cost`. Unpriced calls and heuristic-linked sessions are stated
when non-zero. It prints no prompt text or lane names. An unknown id or path exits non-zero with
a one-line reason. The catalogue lags the transcripts by an indexer pass, hence the timestamp.

Private command add-ons can import `agent_history.reader.main(argv, register, dispatch)`.
`register(subparsers)` adds parsers; `dispatch(args)` returns an integer status when handled or
`None` for public dispatch. The public reader exposes `run`, `query`, `since_sql`, `namespaces`
and `context` for those add-ons.

## Journal sync

`agent-history journal-sync --source-db /opt/journal/app.db` reads only the
`ah_export_session_summary` SQLite view, using a read-only connection. `collector.journal_db`
can supply the source path instead. The destination uses `--dsn`, `AGENT_HISTORY_DSN` or `dsn`
and needs write access to `ah.session_summary`, `ah.session_topic` and `ah.meta`.
Mount the source database directory read-only, including SQLite WAL/SHM sidecars when present;
mounting only the database file may hide recent WAL-backed rows. The catalogue DSN is supplied
at runtime, never baked into the image. Run periodically if journal summaries are wanted:
transcript indexing alone does not populate these tables. Instance changes cause a full resync;
daily reconciliation removes summaries and topics no longer present in the source view. An invalid
required source timestamp stops the selected sync before reset, reconciliation or watermark writes;
existing summaries and topics remain unchanged and the result reports a skipped reason.

## Synthetic fixture provenance

All parser fixtures are toy records, not captures of real sessions or private prompt content.
The pi records are generated by `tests/fixtures/pi/generate.py` (`just gen-fixtures`); the existing
Claude and Codex fixtures were assembled synthetically for parser contracts. Collector and journal
integration tests construct their own toy git repositories, SQLite records and disposable catalogue
rows at runtime. Keep this provenance: do not replace them with actual transcripts or credentials.
