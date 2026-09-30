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
internal Compose network, with **no host database port**. Only the exporter is published, on host
loopback by default.

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
listen = "0.0.0.0:9464"
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
docker compose up -d exporter indexer
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
that have already been deleted (see CONTRACT.md).

The package ships the catalogue, search, classifier, application container and native `/metrics`
exporter. The Compose template is for local deployment; no live metrics cutover is implied.

## Running the exporter

The container image runs three separate processes from the same image: `exporter` serves
`/metrics` and `/healthz`, `index --every 60` indexes the configured sources, and
`embed --every 60` processes pending chunks. Set the three database passwords, writer and reader
DSNs, `TRANSCRIPTS_ROOT` and `AGENT_HISTORY_CONFIG_FILE` in your environment before running
`docker compose -f compose.yml up --build`. Never store passwords in the compose file.

The config file should set `[sources]` to namespace-to-directory entries under `/transcripts`,
`[embedding]` to your chosen provider, and `[exporter]` with `listen = "0.0.0.0:9464"` and
`state_dir = "/state"`. Bind only the host loopback port in compose; expose another port only
behind an authenticated scrape path. The state volume preserves counters through restarts. Set
`[exporter] collectors = ["archive", "catalogue", "self"]` explicitly when using only these
three collectors. The default collector set also includes `efficiency`, which **reads transcript
contents** from the configured sources to classify model calls. Archive collection reads file
metadata and receipts, and catalogue collection reads database aggregates; those two collectors
do not read transcript content. Archive roots (`hot`, `cold`, `incoming`, `conflicts`) are optional
and must be mounted read-only if set.

At the public exposition boundary, `model` label values are restricted to an explicit literal
allowlist of public model identifiers. Unlisted values become `other`, and empty values become
`unknown`; persisted historical collector series pass through the same boundary. Counters are
reset-adjusted durably by original source identity before privacy-mapped labelsets are summed.
Histogram buckets, sums and counts follow the same policy. Original pre-privacy counter offsets
are retained, and temporarily absent sources keep their already-counted contribution, so restarts
and source disappearance/reappearance do not reset or double-count the public total.
Standalone archive namespace values become `<agent>-standalone`, and their machine values become
`other`; counts and byte totals are summed, and modification-time extrema are retained. Metric
family names, help, types and label keys do not change.

This is not general metadata sanitisation. Other operator-configured namespaces, loop names and
collector state can be sensitive. Local collector and counter state retain original source
identities, including transcript-derived model identifiers and paths; protect the trusted state
volume as well as the transcript mounts and database. Do not expose
an unauthenticated metrics endpoint beyond a trusted local scrape path. Periodic index/embed logs
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

## Licence

MIT, see [LICENSE](LICENSE). ParadeDB is AGPL-3.0; this project talks to it over the network as a
separate service and neither includes nor links its code.
