# Completed-loop health observations

The periodic indexer's catalogue collector exports the latest completed loop per canonical
`owner/repo` and root `harness` (`pi`, `claude`, `codex`). These are gauges, not counters or
per-loop series. They have exactly `repo` and `harness` attributes, never loop, session, task,
model, message, path or owner labels. Repository identity comes from `ah.loops.repo` through
`ah.canonical_repo`, not a cwd basename. Unknown repository identity is excluded.

The collector selects the greatest completed end time first, breaking ties by launch time and
launch UID. Each field then belongs to that one loop. A newer unknown field removes the older
known observation; it does not hold an older known value. No loop age limit is applied. The
completion timestamp gauge distinguishes an old completion from fresh collection. A rebuild
marker withholds all loop-health samples.

## Window and scope

Only `ah.loops.status = 'finished'` with a positive launch-to-end window qualifies. Stale,
running, waiting and paused loops are not completions. The window ends at the earlier of the
recorded lifecycle end and the next launch on the same root or exact report path. Equal-time
ambiguous launches produce no observation. The original root and exactly linked `relaunch`
sessions contribute within this window. Lanes never contribute to root text or timing.

Root messages means stored `assistant_text` messages with role `assistant` inside the half-open
window, not model requests, tool-only assistant envelopes, reasoning, briefs, human prompts,
notifications or injected wrappers. The count is exported alongside the rates. Repeated blocks
and quoted text can count as separate messages; this is an observation of indexed text, not a
transcript completeness guarantee.

## Model time

The numerator reuses the pi root model-time definition: sum valid persisted finished assistant
request intervals, clipped to launch and observation, then divide by wall time and cap only the
share at one. It is a sum, not interval union: overlaps can overcount before capping. Tool, wait,
review and idle time stay in the denominator. The launch is the catalogue launch timestamp, not
first call time; observation is the bounded catalogue end. This can differ slightly from the pi
UI's producer launch timestamp.

Calls are root `llm_call` rows selected by completion in the half-open window, excluding
`usage:*`, `compaction` and `branch_summary` stop reasons, as in the efficiency classifier.
Only a unique ended `request_progress` row linked by exact session and final response identity
supplies timing. Its valid start/end and persisted snapshot must be at or before observation.
No ordinal, proximity or `llm_call.duration_ms` fallback mixes timing boundaries. Timing is
currently supported for pi only. Missing or ambiguous timing is not zero: the full share is
omitted unless every selected assistant call has valid finished timing. No calls also means
unknown. Valid zero durations remain known. `root_model_calls` and `root_model_timed_calls`
expose coverage independently. Unlike the pi UI, partial coverage does not publish a full share.

## Approximate text signals

These are deliberately text heuristics, not confirmed owner interventions or authoritative
state outcomes. Matching is case-insensitive, with word boundaries, once per root assistant text
message, regardless of the number of occurrences:

* Owner questions: `owner question`, `question for owner`, `questions for owner` (also with
  `the owner`), or the word `owner` or `you` followed on the same line within 160 characters by
  `?`. Implicit questions, names and other phrasings can be missed.
* Owner parks: the word `park`, `parked`, `parks` or `parking`. Mentions, quotations and
  negations can match even when no owner action occurred. This is not `parks_total`.
* For-ratification rulings: messages containing `for-ratification` or `for ratification`.
  Several rulings in one message count once; mentioning a ruling does not verify it happened.

Question and park counts are divided by root assistant text messages and multiplied by 1000.
A nonempty indexed text surface with no matches establishes zero. No root assistant text leaves
both rates and the ratification count unknown, not zero.

## Outcomes, cost and transport

`lane_complete_rate` is lanes with latest `return_status = 'complete'` divided by all indexed
`ah.lane` rows for that run. Missing returns are not complete; no indexed lanes is unknown.
This is not a scheduled-lane census, root acceptance rate or publication rate. `tasks_done`
uses the catalogue's existing independent delivery-evidence precedence. `estimated_cost_usd`
uses `ah.loops.priced_cost_usd`, including root and linked lanes under existing catalogue
membership. It is list-price accounting, not an invoice or recorded provider spend. Unknown
prices or usage remain unknown, never a subtotal or zero.

All families share the same immutable collection snapshot for OTLP and the in-process
Prometheus proof representation. There is no HTTP metrics endpoint or new telemetry stack.
The generated catalogue dashboard has a Loop health tab with trend rows and dedicated canonical
repo and harness filters. Namespace and the older short-slug Repo filter do not apply to these
panels. The repo filter and series legends display the real canonical repository names from the
catalogue, not hashes or aliases. This also applies in presentation mode (`var-redact=on`): that
mode does not hide repository names on the Loop health tab. Use an appropriate dashboard view
when sharing screenshots. Public fixtures and generated dashboard source contain neutral examples,
not private catalogue names. Grafana Cloud normalises unit-one share/rate gauges with the `_ratio`
suffix.

Gauges repeat until a newer completion is observed. Several loops completing between collection
passes can be missed; this is a low-cardinality visual trend, not an event ledger or a substitute
for direct catalogue queries. Text matching stays in PostgreSQL; no text is returned to metrics.
The read-only query has the catalogue collector's 60-second statement timeout.
