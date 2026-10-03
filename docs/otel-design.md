# OpenTelemetry design
Status: proposed frozen seam, subject to independent design review before implementation.
Repository baseline: `292bf2d7b3d113979e85d32f1482893e01226b06`.
## 1. Scope and invariants
Add optional OpenTelemetry traces, metrics and content-free operational logs. Do not replace the catalogue, its unredacted content policy, or the existing Prometheus collection path.
The exporter container remains the collection host. It adds OTLP export alongside its existing `/metrics` endpoint. Keep:
- `/metrics` and `/healthz`;
- the index and embed worker textfiles;
- the existing Alloy scrape;
- durable `counters.json` state and its counter-reset adjustments;
- existing metric family names, labels, types, help text and rendering.
OTLP export goes directly to the operator-selected Grafana Cloud OTLP gateway. The operator supplies endpoint and authentication through standard `OTEL_*` environment variables. No gateway address, tenant identifier, authentication value or credential path belongs in this repository. This design requires no Alloy change.
Do not remove any existing route until collection parity and backend readback have been proven. A local OTLP receiver proves application behaviour; it does not prove acceptance by a production gateway.
With the optional extra absent, or with no explicitly configured OTLP endpoint, execution behaves exactly as before: no OTel imports are required, no exporter threads or network connections start, and output, exit status, database work and Prometheus state remain unchanged.
### Source anchors
All repository line references in this document refer to the baseline above.
- `pyproject.toml:1-27`: package dependencies, optional extras and entry points.
- `src/agent_history/cli.py:177-242`: recursive single-shot execution inside index/embed `--every` loops.
- `src/agent_history/cli.py:282-333`: journal-sync and exporter dispatch.
- `src/agent_history/cli.py:376-437`: writer commands, index, embed, collect-git and standalone postpass.
- `src/agent_history/metrics/__init__.py:9-29`: frozen `Sample`, `Family` and `Collector` seam.
- `src/agent_history/metrics/server.py:121-279`: durable counter adjustments, retirement and exposition.
- `src/agent_history/metrics/server.py:295-351`: shared collection/cache path.
- `CONTRACT.md:7-35`: unredacted catalogue content and existing bounded embedding failure labels.
## 2. Packaging and public telemetry seam
Add an optional extra, installed as `agent-history[otel]`, containing:
- `opentelemetry-api`;
- `opentelemetry-sdk`;
- `opentelemetry-exporter-otlp-proto-http`.
Resolve compatible released versions together and record them in the repository's lock file. Do not add these packages to mandatory runtime dependencies. Do not install automatic HTTP, database, subprocess or logging instrumentation.
The only OTel-specific application dependency is `src/agent_history/telemetry.py`. Other modules must be able to import it without the extra installed.
### Required API
- `setup(service_name: str) -> Telemetry`
- `Telemetry.shutdown() -> None`
- `tracer()`
- `meter()`
Freeze these additional lifecycle and safe-log facilities:
- `Telemetry.enabled: bool`
- `Telemetry.force_flush(timeout_millis: int = 10000) -> bool`
- `emit(event_name: str, attributes: Mapping[str, Scalar]) -> None`
`Scalar` means a string, boolean, integer or finite floating-point number permitted by the event's schema. This annotation must not require an OTel import.
`tracer()` and `meter()` return the active module-owned tracer and meter. Do not replace a caller's global OTel providers. Use owned providers and explicit provider references for logs. This prevents recursive CLI execution and embedding-library consumers from fighting over global provider registration.
`setup()` is idempotent for the same effective service within one process. The outer process owns shutdown; nested command execution borrows the active instance. Only one effective service identity is supported per CLI process. Journal-sync or postpass invoked as part of another worker remain child operations of that worker, not a second provider stack.
Do not cache an enabled tracer or meter at module import time. Obtain it after setup, so instruments are bound to the correct provider.
### No-op contract
The no-op implementation supports every operation used by application instrumentation:
- tracer `start_as_current_span()` and its context manager;
- span `set_attribute()`, `set_attributes()`, `set_status()`, `add_event()`, `is_recording()` and `end()`;
- meter creation of counters, histograms, observable counters and observable gauges;
- synchronous instrument `add()` and `record()`;
- lifecycle `force_flush()` and `shutdown()`;
- safe `emit()`.
No-op span contexts propagate application exceptions unchanged. They do not suppress exceptions, evaluate exception messages, invoke observation callbacks, create threads, perform I/O or print diagnostics. `force_flush()` returns true and `shutdown()` is harmless and idempotent.
The seam must also fail open for telemetry setup/export errors: telemetry failure cannot change the worker's result or roll back successful catalogue work. A configured but unavailable extra remains silent and no-op. If enabled telemetry configuration is invalid, a diagnostic may use only a fixed event/category, never the invalid value.
## 3. Configuration and providers
### Environment-only configuration
Do not add telemetry flags, TOML settings or `AGENT_HISTORY_OTEL_*` variables.
Supported endpoint and authentication configuration:
| Variable | Behaviour |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Explicit base URL for all configured signals. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Optional exact trace URL, overriding the base URL for traces. |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | Optional exact metrics URL, overriding the base URL for metrics. |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | Optional exact logs URL, overriding the base URL for logs. |
| `OTEL_EXPORTER_OTLP_HEADERS` | SDK-parsed authentication/header pairs; never logged. |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | Standard trace-specific override. |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` | Standard metrics-specific override. |
| `OTEL_EXPORTER_OTLP_LOGS_HEADERS` | Standard logs-specific override. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf`; this extra does not support gRPC or HTTP JSON. |
| `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_PROTOCOL` | If set, must also be `http/protobuf`. |
| `OTEL_SERVICE_NAME` | Overrides the command's default service name. |
| `OTEL_EXPORTER_OTLP_TIMEOUT` and signal-specific timeout variables | SDK export timeouts, with the SDK's documented units. |
| `OTEL_METRIC_EXPORT_INTERVAL` / `OTEL_METRIC_EXPORT_TIMEOUT` | Standard metric reader controls, in milliseconds. |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | Legacy snapshot instruments require `CUMULATIVE`. |
Treat an unset or empty endpoint as absent. A signal is enabled only when its effective endpoint is explicitly present; the SDK's localhost default must not activate export accidentally. A generic endpoint enables all three signals. A signal-specific endpoint alone enables only that signal.
For HTTP/protobuf, the generic base URL gains `v1/traces`, `v1/metrics` or `v1/logs`. Signal-specific URLs are used as supplied. Delegate these rules and header decoding to the SDK, rather than maintaining a second parser.
If the configured temporality preference is incompatible with the cumulative legacy bridge, disable that bridge with a fixed diagnostic; do not silently publish deltas under a cumulative parity claim. Production configuration uses `CUMULATIVE`.
### Default service names
| Command/process | Default `service.name` |
|---|---|
| `index`, including `--every` | `agent-history-index` |
| standalone `postpass` | `agent-history-postpass` |
| `embed`, including `--every` | `agent-history-embed` |
| standalone `journal-sync` | `agent-history-journal-sync` |
| `collect-git` | `agent-history-collect-git` |
| `collect` / `agent-history-collect` | `agent-history-collect` |
| `exporter` | `agent-history-exporter` |
A nested postpass inherits `agent-history-index`. Do not identify a service from a hostname, working directory, database DSN or transcript namespace.
### Provider construction
Use:
- `TracerProvider` with `BatchSpanProcessor` and HTTP `OTLPSpanExporter`;
- `MeterProvider` with `PeriodicExportingMetricReader` and HTTP `OTLPMetricExporter`;
- `LoggerProvider` with `BatchLogRecordProcessor` and HTTP `OTLPLogExporter`.
Create only the providers required by enabled signals.
The resource is an explicit allowlist:
- effective `service.name`;
- package `service.version`;
- SDK name, language and version.
Do not export arbitrary `OTEL_RESOURCE_ATTRIBUTES`, automatically detected host/process/container data, process arguments, environment variables or baggage. In particular, do not pass an unfiltered `Resource.create()` result to a provider. Construct or filter the resource so only the allowlisted fields survive.
Support `OTEL_SERVICE_NAME` explicitly; unsupported resource fields must never weaken the content rule. Endpoint/header values are configuration for transport, not resource attributes.
SDK and HTTP-exporter diagnostics must not escape as raw URLs, headers, response bodies or exception messages. Do not attach their loggers to the operational OTLP logger. Use a narrowly scoped diagnostic filter/handler, without changing unrelated application logging.
## 4. Worker spans and exact callsites
All pass spans are `INTERNAL` spans. Use fixed names.
Every span context disables automatic exception recording and automatic exception-status handling:
- `record_exception=False`;
- `set_status_on_exception=False`.
On failure, set `ERROR` with no raw description and add only a bounded `error.type`. Never call `record_exception(exc)`.
Successful, skipped and failed outcomes are distinct. A lock-held or disabled pass is not a failed pass. A returned error count greater than zero is a failed pass even if no exception escaped.
### Pass boundaries
| Span | Existing boundary | Required coverage and safe result fields |
|---|---|---|
| `index.pass` | Index command dispatch, `cli.py:376-392`, calling `load.refresh`, `load.py:1238-1322` | Start before writer connection; finish after refresh, optional search-index creation and connection cleanup. Include counts for files, lines, rows and errors, plus bounded lock-held/skipped outcome. `load.refresh` covers schema application, source inventory, parsing, postpass, commits and textfile emission. |
| `postpass.pass` | `load.post_passes`, `load.py:984-1060`; standalone dispatch at `cli.py:433-435` | One span per actual postpass invocation, including dirty-session early return, live-loop refresh, task references, structure passes and transaction exit. Child of index when nested. Standalone command span also covers connection/lock acquisition and final commit. Export only integer result counts, not a blanket copy of the result dict. |
| `embed.pass` | Embed command dispatch, `cli.py:398-409`, and the call to `embed.run`, `embed.py:522-649` | Include provider selection, connection work, gate/lock checks, cache/chunk work, existing retry behaviour, optional GC, cleanup and the periodic metrics wrapper. Export item/chunk/API-input/cache/failure/token counts and a fixed skip/failure reason. |
| `journal_sync.pass` | `journal_sync.sync`, `journal_sync.py:152-225`; command dispatch at `cli.py:282-292` | Include read-only SQLite open/read/close, source validation, destination transaction, batches, pruning and watermark updates. Export `journal_rows`, matched/unmatched/deleted/bad-json counts and reset/full-pass flags where known. Do not export `journal_skipped_reason` text. |
| `collect_git.pass` | `collect_git.collect`, `collect_git.py:1267-1291`; `cli.py:416-420` | Include configured-repository iteration, local git reads and database upserts. Export repository/commit/file counts and number skipped. Never export the returned `skipped` list. |
| `collect.pass` | `collect_git.main`, `collect_git.py:1305-1368` | Cover lock/deadline handling, connection, repository/CI/home collection and cleanup. Export aggregate table counts, repository count, dry-run flag and bounded outcome. Do not export the full summary dict. |
| `exporter.collect` | `MetricServer.metrics`, `metrics/server.py:295-351` | One span for a real refresh, not each cache hit or HTTP scrape. Child spans `exporter.collector` carry the fixed collector name and success/failure. |
Application functions invoked within an already-open command/pass span must not create a duplicate same-name pass span. A small shared pass wrapper may provide the function boundary for direct module invocation and reuse the active pass when entered from the CLI. Nested postpass is a separate genuine pass, not a duplicate index pass.
For index/embed, a pass finishes before `time.sleep(args.every)`. Do not create a process-lifetime span covering idle intervals.
Known result fields must be selected explicitly. For example, `task_refs` may have a structured result: export its numeric fields individually, never stringify it.
### Safe common attributes
Permitted attributes include:
- `agent_history.worker`: fixed worker enum;
- `agent_history.outcome`: `success`, `skipped`, `error`;
- `agent_history.skip_reason`: a producer-defined bounded enum;
- `error.type`: bounded category;
- integer counts and finite durations;
- an existing refresh identifier;
- explicitly selected session UID, canonical repository slug or loop identifier when an operation genuinely has one.
Passes that process many sessions must not invent a single session or loop identity. These identifiers are never sourced from text snippets, paths, exception messages or arbitrary JSON.
Do not add session identifiers to legacy metric families.
## 5. Outbound and process-edge spans
Do not use auto-instrumentation: automatic SQL, exception and HTTP attributes could breach the content rule.
### Database boundaries
Instrument the actual psycopg connection/cursor boundary used by the workers:
- `load.connect`, `load.py:1152-1159`;
- `collect_git.connect`, `collect_git.py:877-907`;
- `CatalogueCollector.collect`, `metrics/catalogue.py:133-217`;
- the optional loop-map connection in `EfficiencyCollector.collect`, `metrics/efficiency.py:117-132`.
Use driver-compatible connection/cursor subclasses or an equivalent adapter that preserves the existing transaction, cursor, copy and context-manager contracts. Do not change DSNs, SQL or database permissions.
At the process edge, cover:
- connection establishment: `db.connect`, `CLIENT`;
- each `execute` or `executemany` operation: `db.query`, `CLIENT`;
- a streaming `copy` operation, including its writes and context exit: `db.copy`, `CLIENT`;
- commit/rollback round trips: `db.commit` / `db.rollback`, `CLIENT`.
Avoid recording the same operation twice when connection convenience methods delegate to an instrumented cursor.
Allowed database attributes are `db.system.name="postgresql"`, a fixed operation category, duration, safe row count where known and bounded outcome/error category. Never record SQL text, parameters, fetched values, DSN, database username, host, schema-derived content or connection error text.
Journal SQLite is local I/O, not a remote service. Use an `INTERNAL` child span `journal.read` around `_open_view` and its selected view reads (`journal_sync.py:122-140`, `152-203`). Allow the fixed database-system value `sqlite` and aggregate row counts; no source path or rows.
### Embedding requests
The embedding pass owner supplies only the pass wrapper. The embedding-request owner instruments:
- `Provider.embed`, `embed.py:327-338`, for one logical embeddings request;
- each actual `urllib.request.urlopen` attempt in `Provider._post`, `embed.py:298-325`.
The logical request is a `CLIENT` GenAI span named `embeddings <approved model>`. It covers request construction, the existing retry loop, response parsing and vector-count validation. Every HTTP attempt has a child `CLIENT` span `embedding.http_attempt`; its duration excludes backoff sleep. Retries retain their existing maximum, delay and classification behaviour.
Record:
- approved model and provider identifiers;
- `gen_ai.operation.name="embeddings"`;
- actual input-token usage when supplied by the provider;
- total logical-operation duration;
- numeric retry count under `agent_history.retry_count`;
- bounded outcome and error category;
- numeric HTTP status on attempt spans.
Do not infer an external provider identity merely because an endpoint is OpenAI-compatible. Use a known provider identifier when available; otherwise a fixed `openai-compatible` identifier is honest.
Never export input strings, vectors, response bodies, headers, tokens, URLs, model names copied from response prose or `ProviderError` messages. Do not emit content-related GenAI events.
For this delivery, use the released GenAI conventions at semantic-conventions `v1.41.0`, which define the required embeddings attributes and `gen_ai.client.token.usage`. The current development documents have moved to a separate repository and changed token-metric definitions; do not silently mix those definitions with this released schema.
New native instruments:
- `gen_ai.client.operation.duration`: Histogram, unit `s`, one observation per logical request, including failed requests; use the released recommended explicit boundaries.
- `gen_ai.client.token.usage`: Histogram, unit `{token}`, one observation for known input usage, with `gen_ai.token.type="input"`; do not invent output tokens or a zero measurement when usage is unknown.
Metric attributes are the released operation/provider/model fields and bounded error category where applicable. Retry count belongs on spans, not a high-cardinality metric attribute.
The embedding-request owner owns this code. Do not add a second generic URL wrapper or globally monkey-patch urllib to circumvent that ownership. Final proof of “every outbound call” includes these requests after request-level instrumentation is integrated.
### Git and GitHub subprocess edges
Instrument logical outbound subprocess operations, without claiming visibility into a subprocess's internal HTTP pagination:
| Span | Actual callsite |
|---|---|
| `git.fetch`, `CLIENT` | `classify_repo`'s direct `git fetch`, `collect_git.py:481-489` |
| `github.list_repositories`, `CLIENT` | `github_forks`'s `gh repo list`, `collect_git.py:526-544` |
| `github.list_runs`, `CLIENT` | `ci_runs`'s `gh run list`, `collect_git.py:606-631` |
Use fixed operation names, numeric exit status, duration and bounded failure category. A canonical repository identifier may be included; owner/repository values are identifiers, never a command rendering.
Local git reads through `git`, `collect_git.py:435-441`, may have `INTERNAL` `git.read` spans with a fixed verb enum. They are not network client spans. `cat_files` reads must never export blob contents. Do not export argv, environment, stdout, stderr, commit subjects, author emails, tracker titles or file paths.
OTLP transport's own HTTP calls are excluded from application instrumentation. Otherwise telemetry could recursively instrument itself.
## 6. Provider lifecycle and `--every`
### Single-shot commands
1. Parse the command sufficiently to select its default service.
2. Call `setup()` before config loading and external work.
3. Enter the command/pass span.
4. Execute the existing command without changing its output, transaction or exit contract.
5. Emit its content-free completion event while its span is current.
6. Close spans and application connections.
7. In the outermost `finally`, force-flush enabled providers and call `shutdown()`.
Handled skips must still produce a pass span and correlated completion event. Failure before the core function starts, such as config or connection failure, must be attributable to the command span.
### Periodic index/embed
The outer invocation at `cli.py:177-242` owns setup exactly once. Recursive `main(once)` calls borrow that instance:
- one pass span per iteration;
- no setup/registration on each iteration;
- no provider shutdown by the nested invocation;
- no active pass context while sleeping;
- emit the completion event inside the pass;
- force-flush after the iteration's spans have ended, before sleeping.
Existing suppressed single-shot stdout/stderr and fixed periodic status lines remain unchanged. OTel does not recover, buffer or export the discarded output.
On normal exit, `KeyboardInterrupt` or a handled termination signal, stop starting new work, let ordinary cleanup run, flush and shut down once. Signal handling must not call the exporter from inside a signal handler. Use the handler to request exit or raise the established termination exception; cleanup runs in `finally`.
Do not promise export after SIGKILL, `os._exit`, a process crash or a forced shutdown deadline. In particular, the collector's hard alarm at `collect_git.py:1324-1326` remains a hard safety fence.
### Exporter
The exporter owns one telemetry instance for its server lifetime.
When legacy OTLP metrics are enabled, an explicit refresh scheduler calls the same cached collection path used by `/metrics`, at the existing exporter refresh cadence. OTLP publication must continue when no Prometheus client is scraping.
Use the existing collection/cache lock to prevent concurrent duplicate collections by the scheduler and HTTP threads. OTel observation callbacks read only the completed immutable snapshot; they never query the database, read transcripts, mutate state or trigger collection.
At shutdown:
1. stop and join the refresh scheduler;
2. stop serving new requests and finish active collection safely;
3. retain the final completed snapshot;
4. force-flush metrics, ended spans and logs;
5. shut down the owned providers.
### Bounded cleanup
`force_flush(timeout_millis=10000)` uses a shared deadline rather than granting ten seconds separately to every provider. SDK export timeouts remain finite and credentials/configuration are never included in timeout diagnostics.
The SDK's trace and log provider shutdown methods do not expose identical timeout signatures. Implementation must use the selected release's supported APIs and finite exporter/processor waits; do not assume a timeout keyword exists on all providers. Prove shutdown against a non-responsive local receiver, including worker termination.
Telemetry exceptions and flush failure return values are contained and classified. They never replace the command's original exception or result. `shutdown()` is idempotent; an atexit fallback may call it again safely.
## 7. Legacy metric bridge
### One authoritative collection
The OTLP bridge consumes the same completed public snapshot as `/metrics`, not a second run of the collectors.
The sequence remains:
1. materialise each collector's `Family` values and collect section health;
2. include the self collector;
3. apply loop retirement;
4. adjust each original counter source through `State.observe`;
5. perform existing public-label mapping and aggregation;
6. render the existing Prometheus text;
7. publish an immutable structured snapshot of those same public samples;
8. acknowledge retirement only after durable state processing succeeds.
Add a side channel or an equivalent structured result without changing the returned exposition bytes. The OTLP bridge must not call `State.observe` again. Never export original counter-state keys.
The frozen bridge helper belongs under `metrics/`, not in the worker modules. It registers instruments once through `telemetry.meter()` and replaces their shared snapshot atomically after a successful collection.
A callback reads one coherent snapshot. A force-flush must not cause an extra collection or double-count counters.
Preserve current omission semantics: absent gauges are absent, not fabricated zeroes. Retired loops disappear according to the existing positive retirement signal, not due to an OTLP cardinality limit. Preserve original counter reset adjustments, public-label aggregation and source retention exactly.
Loop-label changes made elsewhere do not justify a new OTLP cap or relabelling. Both outputs use the same resulting labels, with no capacity limit introduced by this bridge.
### Instrument mapping
For every table below:
- the OTLP instrument name is exactly the listed metric name;
- listed Prometheus labels become data-point attributes with the same keys and values;
- no additional session, source-path, model or loop attributes are added;
- help text supplies the description;
- `G` means `ObservableGauge`, exported as an OTLP Gauge;
- `C` means `ObservableCounter`, exported as a cumulative monotonic OTLP Sum.
Even a metric ending in `_total` remains `G` when its existing family type is gauge. Do not infer instrument type from its name.
Units are explicit. Names are not rewritten to dotted equivalents. Backend translation may normalise metric names or add resource labels, so backend identity must be verified separately before cutover. Application parity is checked against decoded OTLP, not a guessed backend name.
### Existing `agent_history_*` families
Sources: `metrics/catalogue.py:34-56,133-217`, `metrics/self.py:100-115`, `metrics/archive.py:380-384`.
| Metric / OTLP name | Type | Unit | Attributes |
|---|---|---|---|
| `agent_history_run_success` | G | `1` | none |
| `agent_history_run_duration_seconds` | G | `s` | none |
| `agent_history_lock_held` | G | `1` | none |
| `agent_history_run_files` | G | `{file}` | `result` |
| `agent_history_run_lines` | G | `{line}` | none |
| `agent_history_run_rows` | G | `{row}` | none |
| `agent_history_embed_last_failure_reason` | G | `1` | `reason` |
| `agent_history_embed_run_success` | G | `1` | none |
| `agent_history_embed_run_duration_seconds` | G | `s` | none |
| `agent_history_embed_run_items` | G | `{item}` | none |
| `agent_history_embed_run_chunks` | G | `{chunk}` | none |
| `agent_history_embed_run_api_inputs` | G | `{input}` | none |
| `agent_history_embed_run_cached_inputs` | G | `{input}` | none |
| `agent_history_embed_run_tokens` | G | `{token}` | none |
| `agent_history_embed_run_skipped` | G | `1` | `reason` |
| `agent_history_embed_last_success_timestamp_seconds` | G | `s` | none |
| `agent_history_embed_gc_last_run_timestamp_seconds` | G | `s` | none |
| `agent_history_embed_gc_dry_run` | G | `1` | none |
| `agent_history_embed_gc_eligible` | G | `{vector}` | none |
| `agent_history_embed_gc_deleted` | G | `{vector}` | none |
| `agent_history_embed_gc_skipped` | G | `1` | `reason` |
| `agent_history_last_success_timestamp_seconds` | G | `s` | none |
| `agent_history_sources` | G | `{file}` | `status` |
| `agent_history_lag_bytes` | G | `By` | none |
| `agent_history_parse_issues_total` | G | `{issue}` | `kind` |
| `agent_history_unresolved_links` | G | `{link}` | `kind` |
| `agent_history_dirty_sessions` | G | `{session}` | none |
| `agent_history_rows` | G | `{row}` | `table` |
| `agent_history_embed_pending_messages` | G | `{chunk}` | none |
| `agent_history_embed_failed_inputs` | G | `{input}` | none |
| `agent_history_embed_vectors` | G | `{vector}` | none |
| `agent_history_exporter_collection_duration_seconds` | G | `s` | `collector` |
| `agent_history_exporter_collection_errors_total` | C | `{error}` | `collector` |
| `agent_history_cold_tier_available` | G | `1` | none |
The run families describe the latest run, not lifetime totals. Keep them as gauges.
The embed-backlog family retains its existing name and value even though its name says “messages” and the catalogue collector counts chunks. Do not change its meaning in this telemetry change.
### Archive and storage families
Source: `metrics/archive.py:149-384`.
`S` below expands to `tier,namespace,agent,profile,machine`. It is only a table abbreviation, not an exported attribute.
| Metric / OTLP name | Type | Unit | Attributes |
|---|---|---|---|
| `agent_sessions_storage_root_available` | G | `1` | `tier` |
| `agent_sessions_filesystem_bytes` | G | `By` | `tier,kind` |
| `agent_sessions_filesystem_inodes` | G | `{inode}` | `tier,kind` |
| `agent_sessions_storage_files` | G | `{file}` | S |
| `agent_sessions_storage_bytes` | G | `By` | S |
| `agent_sessions_storage_oldest_mtime_seconds` | G | `s` | S |
| `agent_sessions_storage_newest_mtime_seconds` | G | `s` | S |
| `agent_sessions_archive_pending_files` | G | `{file}` | none |
| `agent_sessions_archive_pending_bytes` | G | `By` | none |
| `agent_sessions_hot_retention_eligible_files` | G | `{file}` | none |
| `agent_sessions_hot_retention_eligible_bytes` | G | `By` | none |
| `agent_sessions_cold_nfs_mounted` | G | `1` | none |
| `agent_sessions_archive_receipts` | G | `{receipt}` | none |
| `agent_sessions_archive_last_success_timestamp_seconds` | G | `s` | none |
| `agent_sessions_archive_receipt_jsonl_files` | G | `{file}` | none |
| `agent_sessions_archive_receipt_jsonl_bytes` | G | `By` | none |
| `agent_sessions_archive_version_snapshots` | G | `{snapshot}` | none |
| `agent_sessions_archive_version_files` | G | `{file}` | none |
| `agent_sessions_archive_version_bytes` | G | `By` | none |
| `agent_sessions_archive_version_newest_mtime_seconds` | G | `s` | none |
| `agent_sessions_archive_available` | G | `1` | `tier` |
| `agent_sessions_archive_files` | G | `{file}` | `tier` |
| `agent_sessions_archive_bytes` | G | `By` | `tier` |
| `agent_sessions_archive_receipt_timestamp_seconds` | G | `s` | none |
Timestamp values remain Unix seconds, not elapsed time. Existing namespace/machine mapping and aggregation remain unchanged; directory or receipt paths are never attributes.
### Exporter self-health families
Source: `metrics/self.py:42-98`.
| Metric / OTLP name | Type | Unit | Attributes |
|---|---|---|---|
| `agent_sessions_metrics_section_success` | G | `1` | `section` |
| `agent_sessions_metrics_section_duration_seconds` | G | `s` | `section` |
| `agent_sessions_metrics_section_last_success_timestamp_seconds` | G | `s` | `section` |
| `agent_sessions_metrics_collection_success` | G | `1` | none |
| `agent_sessions_metrics_collection_runs_total` | C | `{run}` | none |
| `agent_sessions_metrics_collection_failures_total` | C | `{run}` | none |
| `agent_sessions_metrics_collection_duration_seconds` | G | `s` | none |
| `agent_sessions_metrics_build_info` | G | `1` | `version` |
| `agent_sessions_metrics_last_success_timestamp_seconds` | G | `s` | none |
The existing build-info `version="1"` is preserved; it is not substituted with the package version.
### Efficiency counters
Sources: `efficiency/parser.py:40-145,2367-2395`, used by `metrics/efficiency.py:186-215`.
`N` expands to `agent,namespace`. Every counter in this table additionally has `loop`, as declared by the producer. Attribute order is immaterial to OTLP but label keys and values are unchanged.
| Metric / OTLP name | Type | Unit | Attributes besides N and `loop` |
|---|---|---|---|
| `agent_efficiency_time_seconds_total` | C | `s` | `role,state` |
| `agent_efficiency_llm_calls_total` | C | `{call}` | `role,trigger` |
| `agent_efficiency_input_tokens_total` | C | `{token}` | `role,trigger,cache` |
| `agent_efficiency_output_tokens_total` | C | `{token}` | `role` |
| `agent_efficiency_model_seconds_total` | C | `s` | `role,model` |
| `agent_efficiency_model_calls_total` | C | `{call}` | `role,model` |
| `agent_efficiency_tool_calls_total` | C | `{call}` | `role,class` |
| `agent_efficiency_poll_calls_total` | C | `{call}` | `role,target,result` |
| `agent_efficiency_poll_seconds_total` | C | `s` | `role,target,result` |
| `agent_efficiency_wait_timeout_ms_total` | C | `ms` | `role,tool` |
| `agent_efficiency_wait_requests_total` | C | `{request}` | `role,tool` |
| `agent_efficiency_spawns_total` | C | `{spawn}` | none |
| `agent_efficiency_compactions_total` | C | `{compaction}` | `role` |
| `agent_efficiency_turn_errors_total` | C | `{error}` | `kind` |
| `agent_efficiency_spawns_by_route_total` | C | `{spawn}` | `role,spawn_model,effort,agent_type,fork` |
| `agent_efficiency_spawn_errors_total` | C | `{error}` | `kind` |
| `agent_efficiency_git_pushes_total` | C | `{push}` | `role,outcome` |
| `agent_efficiency_ci_waits_total` | C | `{wait}` | `role,outcome` |
| `agent_efficiency_gate_runs_total` | C | `{run}` | `role,outcome` |
| `agent_efficiency_coderabbit_findings_total` | C | `{finding}` | `role,severity` |
| `agent_efficiency_coderabbit_reviews_total` | C | `{review}` | `role,outcome` |
| `agent_efficiency_tool_failures_total` | C | `{failure}` | `role,class` |
| `agent_efficiency_interventions_total` | C | `{intervention}` | `role,kind` |
| `agent_efficiency_root_llm_calls_by_protocol_total` | C | `{call}` | `protocol,poll` |
| `agent_efficiency_root_time_seconds_by_protocol_total` | C | `s` | `protocol,state` |
### Efficiency histogram families
Existing histograms store cumulative aggregates, not individual observations. Do not feed their cumulative sums or counts into `Histogram.record()`, replay fabricated observations, or discard their distributions.
For each legacy histogram family `F`, expose these exact OTLP component instruments through the meter:
- `F_bucket`: `ObservableCounter`, cumulative monotonic Sum, unit `{observation}`, existing labels plus `le`;
- `F_count`: `ObservableCounter`, cumulative monotonic Sum, unit `{observation}`, existing labels;
- `F_sum`: `ObservableCounter`, cumulative monotonic Sum, unit `s`, existing labels.
This is an explicit compatibility representation, not a native OTLP Histogram. It preserves all information present in the legacy family and supports exact parity. New GenAI histograms use native `Histogram` instruments because those operations have real individual observations.
| Legacy family F | Attributes | Finite bucket bounds, in seconds |
|---|---|---|
| `agent_efficiency_lane_seconds` | `agent,namespace,loop` | `300,900,1800,3600,7200,14400` |
| `agent_efficiency_first_spawn_seconds` | `agent,namespace,loop` | `60,300,900,1800,3600` |
Both include the existing `le="+Inf"` bucket. Public offset adjustment must be applied to every component exactly as in existing exposition.
Sources: `efficiency/parser.py:126-145,2378-2395`; `metrics/server.py:234-249`.
A future native-histogram conversion would need its own reviewed aggregate-reader design. It is not part of this delivery.
### Efficiency gauges
Sources: `efficiency/parser.py:2396-2606`, `metrics/efficiency.py:190-207`.
| Metric / OTLP name | Type | Unit | Attributes |
|---|---|---|---|
| `agent_efficiency_rate_limit_used_percent` | G | `%` | `agent,namespace,window` |
| `agent_efficiency_rate_limit_resets_at_seconds` | G | `s` | `agent,namespace,window` |
| `agent_efficiency_rate_limit_window_minutes` | G | `min` | `agent,namespace,window` |
| `agent_efficiency_active_threads` | G | `{thread}` | `agent,namespace,role,loop` |
| `agent_efficiency_active_roots_with_idle_workers` | G | `{thread}` | `agent,namespace,loop` |
| `agent_efficiency_roots_without_lanes` | G | `{thread}` | `agent,namespace,loop` |
| `agent_efficiency_root_no_lane_seconds` | G | `s` | `agent,namespace,loop` |
| `agent_efficiency_context_fill_ratio` | G | `1` | `agent,namespace,role,quantile,loop` |
| `agent_efficiency_context_tokens` | G | `{token}` | `agent,namespace,role,quantile,loop` |
| `agent_efficiency_tracked_files` | G | `{file}` | none |
| `agent_efficiency_baseline_timestamp_seconds` | G | `s` | none |
| `agent_efficiency_loop_map_loops` | G | `{loop}` | none |
| `agent_efficiency_loop_map_age_seconds` | G | `s` | none |
| `agent_efficiency_loop_labels` | G | `{loop}` | none |
The two quantile families remain gauges with `quantile` attributes, not OTLP summaries or reconstructed histograms. Preserve percentages as percentages; do not divide by 100.
### Excluded historical families
`metrics/parity-roster.json` describes legacy families explicitly dropped before this baseline, including SQLite activity/index and systemd families. It is not a list of currently emitted families.
Do not recreate those dropped families as OTLP instruments. Rebase permissions in that roster concern the earlier legacy-exporter cutover; they do not waive equality between this exporter's Prometheus and OTLP snapshots.
### Label safety
Use the same existing public-label transformations for both outputs. Do not export original state keys before model mapping.
Producer enums remain authoritative:
- run results, skip reasons and embedding failure reasons: `metrics/catalogue.py:58-87`;
- catalogue statuses and table names: `metrics/catalogue.py:11-25`;
- collector/section names: fixed collector names and fixed sub-sections;
- archive tier/kind values: fixed producer enums;
- efficiency dimensions: producer-defined enums and identifier mappings.
For parse-issue `kind`, permit only implementation-defined kinds emitted by the parsers/loader, not any identifier-shaped string from a database row. The current producer vocabulary is:
`json_error`, `not_object`, `parser_exception`, `missing_field`, `unknown_type`,
`session_mismatch`, `missing_session_meta`, `late_session_meta`,
`ordinal_not_monotonic`, `missing_call_id`, `orphan_output`, `missing_item_id`,
`missing_session_header`, `late_session_header`.
Existing collector acceptance of identifier-shaped strings alone is not proof of this content guarantee. An out-of-contract label must be refused by OTLP with a fixed diagnostic and visible parity failure, never silently reclassified or counted as equal. Do not change Prometheus output to hide the discrepancy.
Configured identifier labels must be genuine identifiers. Operator trust for metric labels is not permission to export transcript text or credentials disguised as identifiers.
## 8. Content-free log route
Container stdout/stderr already reaches Loki. Do not bridge the root Python logger, existing worker `print()` calls, subprocess output or container streams into OTLP.
Create a private OTel logger solely for fixed operational events. Use the SDK's direct logger emission API, with the current span context, rather than the deprecated SDK `LoggingHandler`.
The operational OTLP logger has no stdout/stderr handler and does not propagate into the existing console route. Thus an OTLP event is not also sent through the stdout-to-Loki path.
Allowed event names and constant bodies:
- `worker.pass.completed`;
- `worker.pass.skipped`;
- `worker.pass.failed`;
- `outbound.call.failed`;
- `telemetry.configuration.invalid`;
- `telemetry.export.failed`.
The first three use the active pass context. Emit completion before ending the span, so logs contain matching trace and span identifiers. A failure event may carry a bounded error category, counts, HTTP status and retry count, but no exception object or formatted message.
Do not emit duplicate success logs for every database operation. Spans supply detailed operation timing; logs supply pass-level operational correlation and selected failures.
Internal exporter failure diagnostics must not recursively log through the failed exporter. Their fallback is a fixed, rate-limited console category with no exception, endpoint or response details.
Existing command output remains unchanged. This design does not certify every legacy stdout message as content-safe; it prevents those messages from becoming OTel records. For example, the collector's existing summary may contain raw error details, and journal skip strings can contain paths. Never bridge or copy them.
## 9. Testable content exclusion
The catalogue remains unredacted. Telemetry exclusion happens at telemetry construction boundaries, not by changing parsers or stored rows.
The following are prohibited in span names, span attributes, span events, status descriptions, metric names/attributes, resource attributes, instrumentation-scope attributes and OTel log bodies/attributes:
- transcript text, prompts, system prompts and reasoning;
- tool input/output, command bodies, stdout/stderr and tool-result JSON;
- provider request/response content, embedding inputs and vectors;
- file contents, journal titles/objectives/narratives/topics and tracker/commit prose;
- credentials, tokens, authentication headers and DSNs;
- raw exception messages, stack traces and payload-derived status descriptions;
- arbitrary environment/resource/baggage values;
- URLs, file paths or process arguments copied from runtime requests.
Content hashes are not a substitute for content exclusion. Do not export hashes derived from prompts, responses, files or embedding inputs.
Permitted metadata is selected by schema: session UID, canonical repository slug, loop label, approved model identifier, counts, durations, numeric status codes and bounded enums. Not every string in a “metadata” column is safe.
Requirements:
1. Every instrumentation callsite has an explicit attribute/event schema. There is no `str(exc)`, `repr(payload)`, blanket dict export, arbitrary event body or arbitrary `extra` forwarding.
2. Automatic exception recording is disabled on every span context.
3. The private logger never receives an exception object, `exc_info`, stack information or existing formatted output.
4. Resource construction excludes arbitrary environment and detector output.
5. Metric callbacks consume only the validated public snapshot, never transcript rows or original counter-state keys.
6. GenAI content capture is absent, regardless of content-capture environment settings used by unrelated instrumentation.
7. Exporter diagnostics contain no raw transport error, authentication value or response body.
8. Work and Personal data follow the same rules. There is no context-specific redaction or omission in the catalogue.
## 10. Proof required before completion
This document is not runtime proof.
### Optional/no-op behaviour
Run existing public command and focused integration fixtures with the extra absent, including when an endpoint variable is present. Compare stdout/stderr, exit status, database results and textfile/exposition output with the baseline behaviour.
With the extra installed and all endpoint variables absent, prove:
- no network attempt;
- no telemetry worker thread;
- no resource detection;
- unchanged command output;
- no invocation of observable callbacks;
- no mutation of `counters.json` attributable to telemetry.
### Local OTLP receiver
Use an isolated local HTTP/protobuf receiver, not a production tenant.
Exercise the actual index, postpass, embed, journal-sync and collect-git/collect command paths using synthetic collaborators. Prove:
- one correctly bounded pass per invocation;
- nested postpass parentage;
- each instrumented external call represented;
- correlated completion logs;
- numeric result/error metadata without content;
- embedding logical request/attempt relationships, retries and usage after request-level integration.
For `--every`, observe at least two iterations and terminate normally or with a handled signal. Prove a shared provider lifecycle, separate finished pass spans, no sleep-spanning context and flush of the last completed iteration.
Use a non-responsive receiver to verify finite export/cleanup behaviour and preservation of the application's original result.
### Content proof
Build synthetic transcript, provider-error, tool-output, SQLite-summary and credential-shaped markers at runtime. Drive each instrumented path through both success and failure, including configuration errors, retries, skips and shutdown.
Decode all exported OTLP requests. Inspect every exported name, body, attribute, event, status description, resource and instrumentation scope. Assert that none contains any prohibited marker or payload representation.
Also attempt injection through:
- `OTEL_RESOURCE_ATTRIBUTES`;
- resource detector configuration;
- exception messages and provider error bodies;
- arbitrary Python logging records;
- an unrecognised parse-issue kind;
- transport/exporter diagnostics.
Prove that the check observes the real outgoing OTLP body, not only an in-memory helper. The reproduction must fail against an intentionally unsafe instrumented fixture for the correct reason before it is accepted as a content check.
### Metric parity
The metrics implementation supplies a documented local parity command. Its required behaviour is frozen here, not its CLI spelling:
1. perform one real collection over synthetic archive, run snapshots, catalogue and efficiency inputs;
2. retain the resulting Prometheus exposition and capture the corresponding decoded OTLP metrics;
3. canonicalise only resource/timestamp differences and the explicitly documented histogram component representation;
4. compare every currently mapped family, sample identity, value and declared instrument type;
5. require cumulative monotonic Sums for legacy counters;
6. reconstruct each legacy histogram from its exact bucket/sum/count instruments and compare every component;
7. report missing, extra, rejected and unvalidated families distinctly and fail on any;
8. demonstrate that unchanged Prometheus exposition remains byte-identical.
Cover counter reset, independent original-source adjustment before public-label aggregation, unchanged repeated snapshots, restart with retained `counters.json`, collector failure, missing gauges, loop retirement and label counts above former capacity limits.
Capture-time comparison between independent collections is weaker than this one-snapshot proof and does not replace it. The existing legacy/new Prometheus comparator remains useful but cannot validate OTLP by itself.
A skipped database test, absent receiver capture or comparison of two copies of the same output is not a pass.
### Deployment readback
After local proof, the operator verifies traces, correlated logs and metrics at the configured backend for the exact deployed candidate. Check OTLP/Prometheus stream attribution so they are not accidentally summed together during coexistence.
Backend name/unit translation and resource-label promotion must be observed, not assumed. Keep the Prometheus/textfile/Alloy routes until the operator accepts parity.
## 11. Ownership and review boundary
Implementation proceeds only after independent review of the materialised design.
- SDK/traces/logs implementation owns `telemetry.py`, optional packaging, lifecycle and permitted worker/database/subprocess wrappers.
- Metrics implementation owns the additive `metrics/` bridge and its all-family parity proof.
- Embedding-request implementation owns request-level changes in `embed.py`, released GenAI spans/metrics and retry/content proof.
- The embedding pass wrapper is the only embedder change made by the SDK/traces/logs implementation.
- Final all-outbound-call acceptance includes embedding request instrumentation; the pass-only implementation cannot claim that portion independently.
No production database, telemetry tenant or deployment mutation is needed to implement the local proof.
## 12. Upstream documentation and decisions
Documentation inspected on 2026-10-03:
1. [Python exporters](https://opentelemetry.io/docs/languages/python/exporters/).
   The guide states that `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` defaults to `CUMULATIVE`, and shows HTTP/protobuf span and metric exporters with batch/periodic readers.
2. [OTLP exporter specification](https://opentelemetry.io/docs/specs/otel/protocol/exporter/).
   For signal-specific endpoints, “the URL MUST be used as-is without any modification”; the generic endpoint is a base for signal-relative paths. Headers use comma-separated key/value pairs.
3. [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/).
   The guide describes explicit provider setup and says that the logs API/SDK are still under development. Its exception-recording examples are deliberately not adopted here because raw exceptions breach this design's content rule.
4. [Python SDK log implementation](https://github.com/open-telemetry/opentelemetry-python/blob/main/opentelemetry-sdk/src/opentelemetry/sdk/_logs/_internal/__init__.py).
   The inspected implementation marks SDK `LoggingHandler` deprecated and provides direct `Logger.emit`. It can also attach exception attributes, which this design expressly forbids.
5. [Python SDK meter implementation](https://github.com/open-telemetry/opentelemetry-python/blob/main/opentelemetry-sdk/src/opentelemetry/sdk/metrics/_internal/__init__.py).
   Confirms observable counter/gauge creation and metric-provider flush/shutdown APIs.
6. [Python SDK trace implementation](https://github.com/open-telemetry/opentelemetry-python/blob/main/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py).
   Confirms trace-provider flush and shutdown have different signatures; implementation must not invent a common timeout keyword.
7. [Released embeddings span conventions, v1.41.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.41.0/docs/gen-ai/gen-ai-spans.md#embeddings).
   Defines `CLIENT` embeddings spans, `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model` and input-token usage.
8. [Released GenAI metric conventions, v1.41.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.41.0/docs/gen-ai/gen-ai-metrics.md).
   Defines `gen_ai.client.operation.duration` and `gen_ai.client.token.usage` as Histograms, including units, attributes and recommended boundaries.
9. [Current GenAI convention repository](https://github.com/open-telemetry/semantic-conventions-genai).
   The current original-repository pages point here. Current development token metrics differ from the released schema selected above. A future convention upgrade requires an explicit reviewed change.
SDK `main` links are research evidence, not dependency pins. Implementation selects compatible released packages and verifies their APIs. The GenAI convention links are release-specific.
## 13. Remaining verification and open items
No product-scope decision is left open by this design.
Remaining implementation/review obligations:
- independently approve or reject the proposed frozen seam;
- verify the selected released Python log API and finite shutdown behaviour;
- implement and prove all-family one-snapshot parity, including histogram components;
- run final all-outbound-call proof after embedding request instrumentation is integrated;
- verify backend metric translation and coexistence attribution before removing any existing route.
Legacy stdout error details are outside the new OTLP log route. If those existing messages require a separate content-policy change, handle that separately rather than silently changing established command output here.
