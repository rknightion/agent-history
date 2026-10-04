#!/usr/bin/env python3
"""Generate dashboards/agent-history.json (dashboard uid agent-history).

dashboard.grafana.app/v2, tabbed. Panel shapes follow
../claude-code/_build_claude_code_agent_internals.py on this same stack.

Data sources:
  agent-history       PostgreSQL (ParadeDB) catalogue on camden, schema ah, role ah_reader (60 s timeout)
  grafanacloud-logs   the ParadeDB container's own logs (service_name="agent-history-db")
  grafanacloud-prom   every agent-history metric family, exported over OTLP by the indexer
                      (service agent-history-index, so job="agent-history-index"; the series carry no
                      instance or component label, and Grafana Cloud renames unit-1 gauges with a _ratio
                      suffix), plus the postgres exporter (instance="agent-history")

Every SQL panel filters on $namespace and $agent with the '__all__' sentinel; functions that take a
namespace array get NSARR. Text variables are interpolated with :sqlstring so quotes are escaped.

Run:  python3 grafana/build_catalogue_dashboard.py   (`--check` fails on drift and writes nothing)
"""

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "dashboards" / "agent-history.json"
FOLDER = "REPLACE_WITH_FOLDER_UID"
# Presentation mode hides private project and host names. The names are not committed: CI replaces
# this token at publish time with a `|`-separated lowercase list, used as a regex alternation in SQL.
PRIVATE_TERMS = "__AH_PRIVATE_TERMS__"
PG = "agent-history"
PROM = "grafanacloud-prom"
LOKI = "grafanacloud-logs"
GV = "13.2.0"
UID = "agent-history"

_ids = iter(range(1, 1000))
ELEMENTS = {}

# --------------------------------------------------------------------------
# SQL fragments
# --------------------------------------------------------------------------
NSALL = "'__all__' IN ($namespace)"
AGALL = "'__all__' IN ($agent)"


def NS(col="namespace"):
    return f"({NSALL} OR {col} IN ($namespace))"


def AG(col="agent"):
    return f"({AGALL} OR {col} IN ($agent))"


def NSAG(alias=""):
    p = f"{alias}." if alias else ""
    return f"{NS(p + 'namespace')} AND {AG(p + 'agent')}"


CTX = f"({NSALL} OR context IN (SELECT split_part(n, '-', 2) FROM unnest(ARRAY[$namespace]::text[]) n))"
NSARR = f"(CASE WHEN {NSALL} THEN NULL ELSE ARRAY[$namespace]::text[] END)"
SINCE = "$__timeFrom()::timestamptz"
UNTIL = "$__timeTo()::timestamptz"
BUCKET = "$__timeGroupAlias({col}, $bucket, NULL)"  # an empty bucket is a gap, never interpolated
DAYS = f"greatest(1, ceil(extract(epoch FROM ({UNTIL} - {SINCE}))/86400))::int"
# join a table carrying session_id to its session for the namespace/agent filter
JS = "JOIN ah.session s ON s.id = {a}.session_id"
# Cost and token panels read ah.llm_call directly, not the day-grain ah.v_daily_usage view: a daily
# row is stamped at local midnight, so any range that does not contain a midnight shows nothing.
LLM = f"FROM ah.llm_call l {JS.format(a='l')} WHERE $__timeFilter(l.ts) AND {NSAG('s')}"
USD = "ah.priced_usd(l.model, l.ts::date, l.input_uncached, l.cache_read, l.cache_write_5m, l.cache_write_1h, l.output)"


def priced(dims="", where="", src=None):
    """Tokens summed per (dims, model, day) first, then priced once per group: ah.priced_usd per
    llm_call row costs 10-35 s over 90 days. dims: comma-terminated select list ('' for a total)."""
    depth, n = 0, 0
    for ch in dims:  # top-level commas: each dimension is comma-terminated
        depth += (ch == "(") - (ch == ")")
        n += ch == "," and depth == 0
    group = ", ".join(str(i) for i in range(1, n + 3))
    return (
        f"(SELECT {dims} l.model, l.ts::date AS d, sum(l.input_uncached) AS i, sum(l.cache_read) AS cr, "
        f"sum(l.cache_write_5m) AS w5, sum(l.cache_write_1h) AS w1, sum(l.output) AS o "
        f"{src or LLM} {where} GROUP BY {group}) g"
    )


GUSD = "ah.priced_usd(g.model, g.d, g.i, g.cr, g.w5, g.w1, g.o)"
# Codex rows carry NULL cache-write columns: a bare a + b + c is NULL for every Codex call.
TOK = "(coalesce(l.input_uncached,0) + coalesce(l.cache_read,0) + coalesce(l.cache_write_5m,0) + coalesce(l.cache_write_1h,0) + coalesce(l.output,0))"
CIN = "(coalesce(l.input_uncached,0) + coalesce(l.cache_read,0) + coalesce(l.cache_write_5m,0) + coalesce(l.cache_write_1h,0))"
# Day-grain views (v_cli_model_timeline, v_feature_usage): include the day the range starts in.
DAYF = "day >= date_trunc('day', $__timeFrom()::timestamptz) AND day <= $__timeTo()::timestamptz"

# Presentation mode: with the hidden $redact variable set to "on" (URL var-redact=on), private repo and
# project names read "private repo" and transcript-derived text reads "(redacted)". Public repos (the
# public rknightion, m7kni and BroTEK-Solutions repos when this was generated) stay readable; the
# private terms are always redacted. Used for screenshots that get published.
PUBLIC_REPOS = [
    "brotek-solutions/ha-addons",
    "rknightion/.github",
    "rknightion/arcane",
    "rknightion/autopi-ha",
    "rknightion/backlog-publishing",
    "rknightion/bumblebee-catalog",
    "rknightion/bumblebee-intune",
    "rknightion/cf2otel",
    "rknightion/chatgpt-exporter",
    "rknightion/claude-notifications-go",
    "rknightion/codex-lb",
    "rknightion/codexlb2otel",
    "rknightion/core",
    "rknightion/fleet-management-operator",
    "rknightion/gcx",
    "rknightion/genai-otel-bridge",
    "rknightion/grafana-aio11y-demo",
    "rknightion/grafana-cloud-org-insights",
    "rknightion/grafana-cloud-reference-examples",
    "rknightion/grafana-cloud-vending-machine",
    "rknightion/graph2otel",
    "rknightion/grottrack",
    "rknightion/intune-assignments-manager",
    "rknightion/intuneomator",
    "rknightion/k8s-monitoring-helm",
    "rknightion/meraki-dashboard-exporter",
    "rknightion/meraki-dashboard-ha",
    "rknightion/mq-exporter-dist",
    "rknightion/openbao-plugin-secrets-github",
    "rknightion/opentelemetry-demo",
    "rknightion/opnsense2otel",
    "rknightion/paperless-ngx-dedupe",
    "rknightion/polylens2otel",
    "rknightion/profilarr",
    "rknightion/rfc6035-2otel",
    "rknightion/sagemcom-f3896-py",
    "rknightion/sf2loki",
    "rknightion/synthkit",
    "rknightion/tailscale2otel",
    "rknightion/third-party-patcher",
    "rknightion/transceiver-exporter",
    "rknightion/unpoller",
]
_PUB = "ARRAY[" + ", ".join(f"'{r}'" for r in PUBLIC_REPOS) + "]"
_PUB_SHORT = "ARRAY[" + ", ".join(f"'{r.split('/')[-1]}'" for r in PUBLIC_REPOS) + "]"
RED = "'$redact' = 'on'"


def RP(x):
    """A repo slug or project name, redacted unless public. An owner/name slug must match a public
    owner/name exactly (short names collide across orgs: m7kni/.github is private, rknightion/.github
    public); a bare project name is compared on the short name."""
    n = f"lower(regexp_replace(coalesce({x}, ''), '^(https?://)?(github\\.com/)?', ''))"
    public = f"(CASE WHEN {n} LIKE '%/%' THEN {n} = ANY ({_PUB}) ELSE {n} = ANY ({_PUB_SHORT}) END)"
    return (
        f"(CASE WHEN {RED} AND {x} !~ '^\\(' AND ({x} ~* '({PRIVATE_TERMS})' OR NOT {public}) "
        f"THEN 'private repo' ELSE {x} END)"
    )


def RH(x):
    """A host: private addresses, the Storage Box user and private-project hosts are redacted."""
    return (
        f"(CASE WHEN {RED} AND ({x} ~ '^[0-9.]+$' OR {x} ~* '({PRIVATE_TERMS}|^u[0-9]+$)') "
        f"THEN '(redacted host)' ELSE {x} END)"
    )


def RW(x):
    """A command or feature name: anything naming the Work context or a private repo is redacted."""
    return f"(CASE WHEN {RED} AND {x} ~* '(work|{PRIVATE_TERMS})' THEN '(redacted)' ELSE {x} END)"


def TX(x):
    """Transcript-derived or path text, redacted in presentation mode."""
    return f"(CASE WHEN {RED} THEN '(redacted)' ELSE {x} END)"


# Redacted expressions reused in f-strings (a nested same-quote f-string needs Python 3.12).
RP_NO_REPO = RP("coalesce(repo_slug,'(no repo)')")
RP_NONE = RP("coalesce(repo_slug,'(none)')")
RP_UNRESOLVED = RP("coalesce(nullif(repo_slug,''),'(unresolved)')")
RW_VERB = RW("coalesce(remote_verb,'(login)')")
TX_SNIPPET = TX("regexp_replace(snippet, '<[^>]+>', '', 'g')")
TX_TSNIPPET = TX("regexp_replace(t.snippet, '<[^>]+>', '', 'g')")
TX_TITLE = TX("coalesce(v.title,'')")


SESSION_LINK = [
    {
        "title": "Open in Session drill-down",
        "url": "/d/agent-history/?var-session=${__value.raw}&${namespace:queryparam}&${agent:queryparam}"
        "&${__url_time_range}&dtab=Session-drill-down",
    }
]
# Panels served by ParadeDB features (BM25 index, pdb.agg facets, index introspection) or by the
# pgvector HNSW index ParadeDB bundles carry this suffix and a "Powered by ParadeDB" description.
PDB = " · ParadeDB"
PDBD = "Powered by ParadeDB: "
VEC = " · pgvector"

LOOP_LINK = [
    {
        "title": "Show lanes for this loop",
        "url": "/d/agent-history/?var-loop_id=${__value.raw}&${namespace:queryparam}&${__url_time_range}&dtab=Loops",
    }
]


# --------------------------------------------------------------------------
# query builders
# --------------------------------------------------------------------------
def _q(group, ds, spec, ref="A"):
    return {
        "kind": "PanelQuery",
        "spec": {
            "refId": ref,
            "hidden": False,
            "query": {"kind": "DataQuery", "group": group, "version": "v0", "datasource": {"name": ds}, "spec": spec},
        },
    }


def sql(raw, ref="A", ts=False):
    return _q(
        "grafana-postgresql-datasource",
        PG,
        {
            "editorMode": "code",
            "rawQuery": True,
            "rawSql": " ".join(raw.split()),
            "format": "time_series" if ts else "table",
        },
        ref,
    )


def prom(expr, ref="A", legend=None, instant=False, fmt=None):
    spec = {"editorMode": "code", "expr": expr, "range": not instant, "instant": instant}
    if legend:
        spec["legendFormat"] = legend
    if fmt:
        spec["format"] = fmt
    return _q("prometheus", PROM, spec, ref)


def loki(expr, ref="A", legend=None):
    spec = {"editorMode": "code", "expr": expr, "queryType": "range", "range": True, "instant": False}
    if legend:
        spec["legendFormat"] = legend
    return _q("loki", LOKI, spec, ref)


# --------------------------------------------------------------------------
# panel builders
# --------------------------------------------------------------------------
def _panel(key, title, desc, queries, group, options, defaults, overrides=None, transformations=None):
    assert key not in ELEMENTS, key
    ELEMENTS[key] = {
        "kind": "Panel",
        "spec": {
            "id": next(_ids),
            "title": title,
            "description": desc,
            "links": [],
            "data": {
                "kind": "QueryGroup",
                "spec": {"queries": queries, "transformations": transformations or [], "queryOptions": {}},
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": group,
                "version": GV,
                "spec": {"options": options, "fieldConfig": {"defaults": defaults, "overrides": overrides or []}},
            },
        },
    }
    return key


def _thresholds(steps=None):
    return {"mode": "absolute", "steps": steps or [{"color": "text", "value": None}]}


def steps(*pairs):
    """steps('green', (600, 'yellow'), (1200, 'red'))"""
    out = [{"color": pairs[0], "value": None}]
    out += [{"color": c, "value": v} for v, c in pairs[1:]]
    return out


def stat(
    key,
    title,
    desc,
    queries,
    unit="short",
    decimals=None,
    steps_=None,
    colour="thresholds",
    mappings=None,
    graph="none",
):
    return _panel(
        key,
        title,
        desc,
        queries,
        "stat",
        {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": "/.*/" if unit == "string" else ""},
            "orientation": "auto",
            "textMode": "auto",
            "wideLayout": True,
            "colorMode": colour,
            "graphMode": graph,
            "justifyMode": "auto",
            "showPercentChange": False,
            "percentChangeColorMode": "standard",
            "text": {"titleSize": 13},
        },
        {
            "color": {"mode": colour if colour != "value" else "thresholds", "fixedColor": "text"},
            "unit": unit,
            **({"decimals": decimals} if decimals is not None else {}),
            "mappings": mappings or [],
            "thresholds": _thresholds(steps_),
        },
    )


def ok_mapping(ok="OK", bad="FAIL"):
    return [
        {
            "type": "value",
            "options": {
                "1": {"text": ok, "color": "green", "index": 0},
                "0": {"text": bad, "color": "red", "index": 1},
            },
        }
    ]


def timeseries(
    key,
    title,
    desc,
    queries,
    unit="short",
    decimals=None,
    stack=False,
    fill=10,
    bars=False,
    legend_mode="list",
    legend_pos="bottom",
    calcs=None,
    minv=None,
    maxv=None,
    overrides=None,
    transformations=None,
    points=False,
    steps_=None,
    threshold_style="off",
):
    return _panel(
        key,
        title,
        desc,
        queries,
        "timeseries",
        {
            "legend": {"showLegend": True, "displayMode": legend_mode, "placement": legend_pos, "calcs": calcs or []},
            "tooltip": {"mode": "multi", "sort": "desc", "hideZeros": False},
        },
        {
            "color": {"mode": "palette-classic"},
            "unit": unit,
            **({"decimals": decimals} if decimals is not None else {}),
            **({"min": minv} if minv is not None else {}),
            **({"max": maxv} if maxv is not None else {}),
            "mappings": [],
            "thresholds": _thresholds(steps_),
            "custom": {
                "drawStyle": "bars" if bars else ("points" if points else "line"),
                "lineInterpolation": "smooth",
                "lineWidth": 1 if bars else 2,
                "fillOpacity": 80 if bars else fill,
                "gradientMode": "none",
                "spanNulls": False,
                "showPoints": "always" if points else "never",
                "pointSize": 6,
                "barAlignment": 0,
                "axisPlacement": "auto",
                "axisLabel": "",
                "axisColorMode": "text",
                "axisBorderShow": False,
                "axisCenteredZero": False,
                "scaleDistribution": {"type": "linear"},
                "hideFrom": {"tooltip": False, "viz": False, "legend": False},
                "insertNulls": False,
                "thresholdsStyle": {"mode": threshold_style},
                "stacking": {"mode": "normal" if stack else "none", "group": "A"},
            },
        },
        overrides=overrides,
        transformations=transformations,
    )


def bars(key, title, desc, queries, unit="short", **kw):
    """Stacked bars per time bucket."""
    return timeseries(key, title, desc, queries, unit=unit, stack=True, bars=True, **kw)


def bargauge(key, title, desc, queries, unit="short", decimals=None, maxv=None):
    """A SQL table (label column + numeric column) or a Prometheus instant vector."""
    return _panel(
        key,
        title,
        desc,
        queries,
        "bargauge",
        {
            "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "valueMode": "color",
            "showUnfilled": True,
            "sizing": "manual",
            "minVizWidth": 8,
            "minVizHeight": 14,
            "maxVizHeight": 22,
            "namePlacement": "left",
            "text": {"valueSize": 13, "titleSize": 12},
            "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom", "calcs": []},
        },
        {
            "color": {"mode": "continuous-BlPu"},
            "unit": unit,
            **({"decimals": decimals} if decimals is not None else {}),
            **({"max": maxv} if maxv is not None else {}),
            "min": 0,
            "mappings": [],
            "thresholds": _thresholds(),
        },
    )


def pie(key, title, desc, queries, unit="short", overrides=None):
    return _panel(
        key,
        title,
        desc,
        queries,
        "piechart",
        {
            "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
            "pieType": "donut",
            "displayLabels": [],
            "legend": {
                "showLegend": True,
                "displayMode": "table",
                "placement": "right",
                "values": ["value", "percent"],
            },
            "tooltip": {"mode": "single", "sort": "none", "hideZeros": False},
        },
        {
            "color": {"mode": "palette-classic"},
            "unit": unit,
            "mappings": [],
            "thresholds": _thresholds(),
            "custom": {"hideFrom": {"tooltip": False, "viz": False, "legend": False}},
        },
        overrides=overrides,
    )


def barchart(key, title, desc, queries, x, unit="short", stack=False):
    return _panel(
        key,
        title,
        desc,
        queries,
        "barchart",
        {
            "xField": x,
            "orientation": "vertical",
            "barWidth": 0.8,
            "groupWidth": 0.7,
            "showValue": "never",
            "stacking": "normal" if stack else "none",
            "xTickLabelRotation": 0,
            "xTickLabelSpacing": 0,
            "fullHighlight": False,
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
            "tooltip": {"mode": "multi", "sort": "desc", "hideZeros": False},
        },
        {
            "color": {"mode": "palette-classic"},
            "unit": unit,
            "mappings": [],
            "thresholds": _thresholds(),
            "custom": {
                "lineWidth": 1,
                "fillOpacity": 80,
                "gradientMode": "none",
                "axisPlacement": "auto",
                "axisLabel": "",
                "axisBorderShow": False,
                "axisCenteredZero": False,
                "axisColorMode": "text",
                "scaleDistribution": {"type": "linear"},
                "hideFrom": {"tooltip": False, "viz": False, "legend": False},
                "thresholdsStyle": {"mode": "off"},
            },
        },
    )


def gauge(key, title, desc, queries, unit="percent", maxv=100, steps_=None):
    return _panel(
        key,
        title,
        desc,
        queries,
        "gauge",
        {
            "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "auto",
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
            "sizing": "auto",
            "minVizWidth": 75,
            "minVizHeight": 75,
            "text": {"titleSize": 12},
        },
        {
            "color": {"mode": "thresholds"},
            "unit": unit,
            "min": 0,
            "max": maxv,
            "mappings": [],
            "thresholds": _thresholds(steps_ or steps("green", (70, "yellow"), (90, "red"))),
        },
    )


def col(name, **props):
    """Field override for a table column: unit, width, links, gauge=True, hidden=True, colour steps."""
    p = []
    if "unit" in props:
        p.append({"id": "unit", "value": props["unit"]})
    if "decimals" in props:
        p.append({"id": "decimals", "value": props["decimals"]})
    if "width" in props:
        p.append({"id": "custom.width", "value": props["width"]})
    if props.get("links"):
        p.append({"id": "links", "value": props["links"]})
    if props.get("gauge"):
        p.append({"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}})
        p.append({"id": "color", "value": {"mode": "continuous-BlPu"}})
    if props.get("bg"):
        p.append({"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}})
        p.append({"id": "thresholds", "value": _thresholds(props["bg"])})
    if props.get("text_colour"):
        p.append({"id": "custom.cellOptions", "value": {"type": "color-text"}})
        p.append({"id": "thresholds", "value": _thresholds(props["text_colour"])})
    if props.get("hidden"):
        p.append({"id": "custom.hidden", "value": True})
    if "max" in props:
        p.append({"id": "max", "value": props["max"]})
    if "wrap" in props:
        p.append({"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}})
    return {"matcher": {"id": "byName", "options": name}, "properties": p}


RATE_RED = steps("green", (0.05, "yellow"), (0.15, "red"))


def table(key, title, desc, queries, cols=None, sort=None, unit="short", filterable=True):
    return _panel(
        key,
        title,
        desc,
        queries,
        "table",
        {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": []},
            "sortBy": [{"displayName": sort, "desc": True}] if sort else [],
        },
        {
            "color": {"mode": "thresholds"},
            "unit": unit,
            "mappings": [],
            "thresholds": _thresholds(),
            "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": True, "filterable": filterable},
        },
        overrides=cols or [],
    )


def logs(key, title, desc, queries):
    return _panel(
        key,
        title,
        desc,
        queries,
        "logs",
        {
            "showTime": True,
            "showLabels": False,
            "showCommonLabels": False,
            "wrapLogMessage": True,
            "prettifyLogMessage": False,
            "enableLogDetails": True,
            "enableInfiniteScrolling": False,
            "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
        {},
    )


def text(key, title, content):
    return _panel(
        key,
        title,
        "",
        [],
        "text",
        {
            "mode": "markdown",
            "content": content,
            "code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False},
        },
        {},
    )


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------
def grid(rows):
    """rows: list of (height, [(key, width), ...])"""
    items, y = [], 0
    for height, cells in rows:
        x = 0
        for key, width in cells:
            items.append(
                {
                    "kind": "GridLayoutItem",
                    "spec": {
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                        "element": {"kind": "ElementReference", "name": key},
                    },
                }
            )
            x += width
        assert x <= 24, (cells, x)
        y += height
    return {"kind": "GridLayout", "spec": {"items": items}}


TABS = []


def tab(title, rows):
    TABS.append({"kind": "TabsLayoutTab", "spec": {"title": title, "layout": grid(rows)}})


# ==========================================================================
# TAB: Overview
# ==========================================================================
AH = 'job="agent-history-index"'
stat(
    "ov_run",
    "Indexer last run",
    "agent_history_run_success_ratio from the 5-minute index timer on camden.",
    [prom(f"agent_history_run_success_ratio{{{AH}}}", instant=True)],
    mappings=ok_mapping(),
    colour="background",
)
stat(
    "ov_idx_age",
    "Catalogue age",
    "Seconds since the last successful index refresh. AgentHistoryIndexStale fires at 20 min.",
    [prom(f"time() - agent_history_last_success_timestamp_seconds{{{AH}}}", instant=True)],
    unit="s",
    steps_=steps("green", (600, "yellow"), (1200, "red")),
)
stat(
    "ov_emb_age",
    "Embeddings age",
    "Seconds since the embedder last succeeded (10-minute timer). AgentHistoryEmbedStale fires at 2 h; a dry BYOK key shows up here first.",
    [prom(f"time() - agent_history_embed_last_success_timestamp_seconds{{{AH}}}", instant=True)],
    unit="s",
    steps_=steps("green", (3600, "yellow"), (7200, "red")),
)
stat(
    "ov_pg",
    "ParadeDB up",
    "pg_up from the postgres exporter in Fleet pipeline agent_history_dbo11y.",
    [prom('pg_up{instance="agent-history"}', instant=True)],
    mappings=ok_mapping("UP", "DOWN"),
    colour="background",
)
stat(
    "ov_lag",
    "Unindexed bytes",
    "agent_history_lag_bytes: transcript bytes on disk not yet parsed into the catalogue.",
    [prom(f"agent_history_lag_bytes{{{AH}}}", instant=True)],
    unit="bytes",
    steps_=steps("green", (50e6, "yellow"), (500e6, "red")),
)
stat(
    "ov_arch_age",
    "Archive age",
    "Seconds since the camden JSONL archive last succeeded (shared with the SQLite index).",
    [prom(f"time() - agent_sessions_archive_last_success_timestamp_seconds{{{AH}}}", instant=True)],
    unit="s",
    steps_=steps("green", (3600, "yellow"), (7200, "red")),
)

stat(
    "ov_sessions",
    "Sessions",
    "Top-level (non-subagent) sessions whose first event is in range.",
    [
        sql(
            f"SELECT count(*) FROM ah.session WHERE $__timeFilter(first_event_at) AND NOT is_subagent AND NOT is_stub AND {NSAG()}"
        )
    ],
)
stat(
    "ov_subs",
    "Subagent sessions",
    "Subagent, fork and Codex child sessions started in range.",
    [
        sql(
            f"SELECT count(*) FROM ah.session WHERE $__timeFilter(first_event_at) AND is_subagent AND NOT is_stub AND {NSAG()}"
        )
    ],
)
stat(
    "ov_prompts",
    "Human prompts",
    "Genuine prompts in range: ah.is_genuine_prompt (typed, pasted, launch, slash and skill prompts; harness control commands such as /model and peer agent messages excluded).",
    [
        sql(
            f"SELECT count(*) FROM ah.message WHERE $__timeFilter(ts) AND ah.is_genuine_prompt(message_class, prompt_origin, text) AND {NSAG()}"
        )
    ],
)
stat("ov_calls", "LLM calls", "API responses recorded (ah.llm_call).", [sql(f"SELECT count(*) {LLM}")])
stat(
    "ov_tools",
    "Tool calls",
    "Claude and Codex tool calls started in range (ah.tool_call).",
    [sql(f"SELECT count(*) FROM ah.tool_call c {JS.format(a='c')} WHERE $__timeFilter(c.started_at) AND {NSAG('s')}")],
)
stat(
    "ov_commits",
    "Agent commits",
    "git commit operations made by agents (ah.git_event op=commit).",
    [
        sql(
            f"SELECT count(*) FROM ah.git_event g {JS.format(a='g')} WHERE $__timeFilter(g.ts) AND g.op='commit' AND {NSAG('s')}"
        )
    ],
)
stat(
    "ov_tokens",
    "Tokens",
    "Uncached input + cache read + cache write + output (ah.llm_call).",
    [sql(f"SELECT coalesce(sum({TOK}),0) {LLM}")],
)
stat(
    "ov_cost",
    "Priced cost",
    "ah.llm_call priced at ah.model_pricing list prices, not the bill.",
    [sql(f"SELECT coalesce(sum({GUSD}),0) FROM {priced()}")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "ov_ccost",
    "Claude-reported cost",
    "Claude Code's own total_cost_usd for sessions started in range (session_rollup.claude_cost_usd).",
    [
        sql(
            f"SELECT coalesce(sum(r.claude_cost_usd),0) FROM ah.session_rollup r {JS.format(a='r')} WHERE $__timeFilter(s.first_event_at) AND {NSAG('s')}"
        )
    ],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "ov_cache",
    "Cache read share",
    "cache_read / (uncached input + cache read + cache write). Higher is cheaper.",
    [sql(f"SELECT sum(l.cache_read)::numeric/nullif(sum({CIN}),0) {LLM}")],
    unit="percentunit",
    decimals=1,
)
stat(
    "ov_loops",
    "Loops",
    "Loop runs launched in range (ah.v_loop_summary).",
    [
        sql(
            f"SELECT count(*) FROM ah.v_loop_summary WHERE $__timeFilter(launch_ts) AND {NS()} AND ({AGALL} OR root_agent IN ($agent))"
        )
    ],
)
stat(
    "ov_active",
    "Active now",
    "Root sessions with an event in the last 15 minutes (ah.active_sessions).",
    [sql(f"SELECT count(*) FROM ah.active_sessions(15) WHERE {NSAG()}")],
    colour="value",
    steps_=steps("text", (1, "green")),
)

bars(
    "ov_cost_model",
    "Priced cost by model",
    "Priced cost per bucket and model (ah.llm_call x ah.model_pricing).",
    [
        sql(
            f"SELECT g.time, coalesce(g.model,'(none)') AS metric, sum({GUSD}) AS value FROM {priced(BUCKET.format(col='l.ts') + ',')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="currencyUSD",
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "ov_sessions_ns",
    "Sessions started by namespace",
    "Top-level sessions per bucket and namespace.",
    [
        sql(
            f"SELECT {BUCKET.format(col='first_event_at')}, namespace AS metric, count(*) AS value FROM ah.session WHERE $__timeFilter(first_event_at) AND NOT is_subagent AND NOT is_stub AND {NSAG()} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "ov_tokens_type",
    "Tokens by type",
    "Token mix per bucket (ah.llm_call). Cache reads usually dominate.",
    [
        sql(
            f'SELECT {BUCKET.format(col="l.ts")}, sum(l.input_uncached) AS "input (uncached)", sum(l.cache_read) AS "cache read", sum(coalesce(l.cache_write_5m,0) + coalesce(l.cache_write_1h,0)) AS "cache write", sum(l.output) AS output {LLM} GROUP BY 1 ORDER BY 1',
            ts=True,
        )
    ],
)
bars(
    "ov_prompts_ns",
    "Human prompts by namespace",
    "Genuine prompts (ah.is_genuine_prompt) per bucket.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, namespace AS metric, count(*) AS value FROM ah.message WHERE $__timeFilter(ts) AND ah.is_genuine_prompt(message_class, prompt_origin, text) AND {NSAG()} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "ov_projects",
    "Projects in range",
    "ah.week(since, namespaces) rolled up per project (root session cwd). Sub-agent tokens and cost roll into their root. summary_coverage = share of root sessions with a journal summary.",
    [
        sql(
            f"SELECT {RP('project')} AS project, sum(sessions) AS sessions, sum(subagent_sessions) AS subagents, sum(loops) AS loops, sum(commits) AS commits, sum(tokens) AS tokens, sum(priced_cost_usd) AS cost_usd, sum(claude_cost_usd) AS claude_cost_usd, round(avg(coverage),3) AS summary_coverage FROM ah.week({SINCE}, {NSARR}) WHERE day <= $__timeTo()::date GROUP BY 1 ORDER BY cost_usd DESC NULLS LAST LIMIT 40"
        )
    ],
    cols=[
        col("cost_usd", unit="currencyUSD", gauge=True),
        col("claude_cost_usd", unit="currencyUSD"),
        col("summary_coverage", unit="percentunit"),
        col("tokens", unit="short"),
    ],
    sort="cost_usd",
)
table(
    "ov_active_t",
    "Active sessions (last 15 min)",
    "ah.active_sessions(15): root sessions with events in the last 15 minutes. context_used = latest context / window. data_lag_s = how far the catalogue trails the transcripts.",
    [
        sql(
            f"SELECT namespace, agent, session_uid, {RP('ah.project_of(cwd)')} AS project, model, context_tokens::numeric / NULLIF(context_window, 0) AS context_used, priced_cost_usd AS cost_usd, running_subagents AS subagents, last_event_at, data_lag_s FROM ah.active_sessions(15) WHERE {NSAG()} ORDER BY last_event_at DESC"
        )
    ],
    cols=[
        col("session_uid", links=SESSION_LINK),
        col("context_used", unit="percentunit", gauge=True, max=1),
        col("cost_usd", unit="currencyUSD"),
        col("data_lag_s", unit="s"),
    ],
)

tab(
    "Overview",
    [
        (4, [("ov_run", 4), ("ov_idx_age", 4), ("ov_emb_age", 4), ("ov_pg", 4), ("ov_lag", 4), ("ov_arch_age", 4)]),
        (
            4,
            [
                ("ov_sessions", 4),
                ("ov_subs", 4),
                ("ov_prompts", 4),
                ("ov_calls", 4),
                ("ov_tools", 4),
                ("ov_commits", 4),
            ],
        ),
        (4, [("ov_tokens", 4), ("ov_cost", 4), ("ov_ccost", 4), ("ov_cache", 4), ("ov_loops", 4), ("ov_active", 4)]),
        (9, [("ov_cost_model", 12), ("ov_sessions_ns", 12)]),
        (9, [("ov_tokens_type", 12), ("ov_prompts_ns", 12)]),
        (9, [("ov_projects", 24)]),
        (8, [("ov_active_t", 24)]),
    ],
)

# ==========================================================================
# TAB: Activity
# ==========================================================================
MSG = f"FROM ah.message WHERE $__timeFilter(ts) AND {NSAG()}"
stat(
    "ac_msgs",
    "Conversation messages",
    "Messages in the conversation classes (ah.conversation_classes(): prompts, visible replies, briefs, reports, summaries) in range. Reasoning and harness-injected text are on the Content tab.",
    [sql(f"SELECT count(*) {MSG} AND message_class = ANY (ah.conversation_classes())")],
)
stat(
    "ac_asst",
    "Assistant replies",
    "assistant_text messages.",
    [sql(f"SELECT count(*) {MSG} AND message_class='assistant_text'")],
)
stat(
    "ac_briefs",
    "Subagent briefs",
    "subagent_brief messages: the prompts handed to lanes.",
    [sql(f"SELECT count(*) {MSG} AND message_class='subagent_brief'")],
)
stat(
    "ac_turns",
    "Turns",
    "Turns started in range (ah.turn).",
    [sql(f"SELECT count(*) FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND {NSAG('s')}")],
)
stat(
    "ac_art",
    "Artifact references",
    "File, document, image and path references (ah.artifact) in range.",
    [sql(f"SELECT count(*) FROM ah.artifact a {JS.format(a='a')} WHERE $__timeFilter(a.ts) AND {NSAG('s')}")],
)
stat(
    "ac_files",
    "Distinct files touched",
    "Distinct artifact paths written, edited, added or updated.",
    [
        sql(
            f"SELECT count(DISTINCT a.path) FROM ah.artifact a {JS.format(a='a')} WHERE $__timeFilter(a.ts) AND a.action IN ('update','edited','written','add','modified','delete') AND {NSAG('s')}"
        )
    ],
)
bars(
    "ac_class",
    "Conversation messages by class",
    "Per bucket and message_class, conversation classes only.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, message_class AS metric, count(*) AS value {MSG} AND message_class = ANY (ah.conversation_classes()) GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "ac_turn_model",
    "Turns by model",
    "Turns per bucket by turn model.",
    [
        sql(
            f"SELECT {BUCKET.format(col='t.started_at')}, coalesce(t.model,'(unknown)') AS metric, count(*) AS value FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "ac_art_kind",
    "Artifacts by kind",
    "Artifact references per bucket by kind.",
    [
        sql(
            f"SELECT {BUCKET.format(col='a.ts')}, a.kind AS metric, count(*) AS value FROM ah.artifact a {JS.format(a='a')} WHERE $__timeFilter(a.ts) AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "ac_art_action",
    "Artifacts by action",
    "Artifact references per bucket by action (read, update, edited, add, linked, ...).",
    [
        sql(
            f"SELECT {BUCKET.format(col='a.ts')}, a.action AS metric, count(*) AS value FROM ah.artifact a {JS.format(a='a')} WHERE $__timeFilter(a.ts) AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
barchart(
    "ac_hour",
    "Human prompts by hour of day",
    "Europe/London hour of each human prompt in range.",
    [
        sql(
            f"SELECT lpad(extract(hour FROM ts AT TIME ZONE 'Europe/London')::int::text,2,'0') AS hour, count(*) FILTER (WHERE agent='claude') AS claude, count(*) FILTER (WHERE agent='codex') AS codex {MSG} AND ah.is_genuine_prompt(message_class, prompt_origin, text) GROUP BY 1 ORDER BY 1"
        )
    ],
    x="hour",
    stack=True,
)
barchart(
    "ac_dow",
    "Human prompts by weekday",
    "Europe/London weekday of each human prompt in range.",
    [
        sql(
            f"SELECT to_char(ts AT TIME ZONE 'Europe/London','ID Dy') AS day, count(*) FILTER (WHERE agent='claude') AS claude, count(*) FILTER (WHERE agent='codex') AS codex {MSG} AND ah.is_genuine_prompt(message_class, prompt_origin, text) GROUP BY 1 ORDER BY 1"
        )
    ],
    x="day",
    stack=True,
)
pie(
    "ac_entry",
    "Sessions by entrypoint",
    "How sessions were started: cli, codex-tui, sdk-py (agent SDK), codex_exec, desktop apps.",
    [
        sql(
            f"SELECT coalesce(entrypoint,'(unknown)') AS entrypoint, count(*) AS sessions FROM ah.session WHERE $__timeFilter(first_event_at) AND NOT is_stub AND {NSAG()} GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
pie(
    "ac_ns",
    "Sessions by namespace",
    "All sessions (root and child) started in range, per namespace.",
    [
        sql(
            f"SELECT namespace, count(*) AS sessions FROM ah.session WHERE $__timeFilter(first_event_at) AND NOT is_stub AND {NSAG()} GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
table(
    "ac_hot_files",
    "Most-touched files",
    "Artifact paths with the most write/edit/update references in range, with how many sessions touched each.",
    [
        sql(
            f"SELECT {TX('a.path')} AS path, count(*) AS writes, count(*) FILTER (WHERE a.action='read') AS reads, count(DISTINCT a.session_id) AS sessions, max(a.ts) AS last FROM ah.artifact a {JS.format(a='a')} WHERE $__timeFilter(a.ts) AND a.kind IN ('file','document') AND {NSAG('s')} GROUP BY a.path HAVING count(*) FILTER (WHERE a.action <> 'read') > 0 ORDER BY count(*) FILTER (WHERE a.action <> 'read') DESC LIMIT 40"
        )
    ],
    cols=[col("writes", gauge=True), col("path", width=520)],
    sort="writes",
)
table(
    "ac_projects_act",
    "Activity by project",
    "Root and child sessions grouped by project (ah.project_of(cwd)): prompts, turns and messages.",
    [
        sql(
            f"SELECT {RP('ah.project_of(v.cwd)')} AS project, count(*) FILTER (WHERE NOT v.is_subagent) AS sessions, count(*) FILTER (WHERE v.is_subagent) AS subagents, sum(v.turns_human) AS human_turns, sum(v.messages) AS messages, sum(v.tool_calls) AS tool_calls, sum(v.commits) AS commits, max(v.last_event_at) AS last FROM ah.v_session_summary v WHERE $__timeFilter(v.first_event_at) AND {NSAG('v')} GROUP BY 1 ORDER BY human_turns DESC NULLS LAST LIMIT 40"
        )
    ],
    cols=[col("human_turns", gauge=True)],
    sort="human_turns",
)

tab(
    "Activity",
    [
        (4, [("ac_msgs", 4), ("ac_asst", 4), ("ac_briefs", 4), ("ac_turns", 4), ("ac_art", 4), ("ac_files", 4)]),
        (9, [("ac_class", 12), ("ac_turn_model", 12)]),
        (8, [("ac_art_kind", 12), ("ac_art_action", 12)]),
        (8, [("ac_hour", 12), ("ac_dow", 12)]),
        (8, [("ac_entry", 12), ("ac_ns", 12)]),
        (10, [("ac_hot_files", 12), ("ac_projects_act", 12)]),
    ],
)

# ==========================================================================
# TAB: Content (parser v4: every message class, prompt origins, reasoning, attachments, file touches)
# ==========================================================================
GENUINE = "ah.is_genuine_prompt(message_class, prompt_origin, text)"
CGROUP = (
    "(CASE WHEN message_class = ANY (ah.conversation_classes()) THEN 'conversation' "
    "WHEN message_class = 'reasoning' THEN 'reasoning' ELSE 'harness-injected' END)"
)
HARNESS = "message_class <> 'reasoning' AND NOT message_class = ANY (ah.conversation_classes())"
FT = f"FROM ah.file_touch f {JS.format(a='f')} LEFT JOIN ah.repo r ON r.id = f.repo_id WHERE $__timeFilter(f.ts) AND {NSAG('s')}"
FREPO = "coalesce(r.slug, ah.repo_name(r.local_root), '(no repo)')"
ATT = f"FROM ah.attachment a WHERE $__timeFilter(a.ts) AND {NSAG('a')}"
stat(
    "ct_prompts",
    "Genuine prompts",
    "ah.is_genuine_prompt: typed, pasted, launch, slash-command and skill prompts. Harness control commands (/model, /clear, ...) and peer agent messages are excluded.",
    [sql(f"SELECT count(*) {MSG} AND {GENUINE}")],
)
stat(
    "ct_reason",
    "Reasoning blocks",
    "reasoning messages. Claude signature-only / redacted thinking and encrypted-only Codex reasoning have no plaintext and are flagged in detail.",
    [sql(f"SELECT count(*) {MSG} AND message_class = 'reasoning'")],
)
stat(
    "ct_harness",
    "Harness-injected messages",
    "System prompts, system reminders, context injections, hook output, command and skill bodies, local command output, agent messages and interrupt markers.",
    [sql(f"SELECT count(*) {MSG} AND {HARNESS}")],
)
stat(
    "ct_tio",
    "Tool I/O rows",
    "ah.tool_io rows in range: one per tool call and per executed Codex operation, with full input and output.",
    [sql(f"SELECT count(*) FROM ah.tool_io t WHERE $__timeFilter(t.ts) AND {NSAG('t')}")],
)
stat(
    "ct_att",
    "Attachments",
    "ah.attachment rows: images, files, documents and pasted text attached to prompts or returned by tools (binary payloads are metadata only).",
    [sql(f"SELECT count(*) {ATT}")],
)
stat(
    "ct_files",
    "Files changed",
    "Distinct paths created, edited, deleted or moved (ah.file_touch).",
    [sql(f"SELECT count(DISTINCT f.path) {FT} AND f.op IN ('create','edit','delete','move')")],
)
bars(
    "ct_group",
    "Messages by content group",
    "Every stored message per bucket: conversation classes, reasoning, harness-injected text.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, {CGROUP} AS metric, count(*) AS value {MSG} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "ct_harness_ts",
    "Harness-injected messages by class",
    "The non-conversation, non-reasoning classes per bucket.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, message_class AS metric, count(*) AS value {MSG} AND {HARNESS} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "ct_volume",
    "Stored text by class",
    "Messages and UTF-8 bytes of text per message_class in range, with the share of all message text. Harness-injected classes are what repeats in every session.",
    [
        sql(
            f"SELECT message_class, {CGROUP} AS content_group, count(*) AS messages, sum(octet_length(text)) AS bytes, round(avg(octet_length(text))) AS avg_bytes, sum(octet_length(text))::numeric / nullif(sum(sum(octet_length(text))) OVER (), 0) AS share {MSG} GROUP BY 1, 2 ORDER BY bytes DESC NULLS LAST"
        )
    ],
    cols=[col("bytes", unit="bytes", gauge=True), col("avg_bytes", unit="bytes"), col("share", unit="percentunit")],
    sort="bytes",
)
bars(
    "ct_origin_ts",
    "Prompts by origin",
    "human_prompt and queued_prompt messages per bucket by prompt_origin: typed, pasted, slash_command, skill, local_command, launch_message, agent_message.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, coalesce(prompt_origin, '(none)') AS metric, count(*) AS value {MSG} AND message_class IN ('human_prompt','queued_prompt') GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
pie(
    "ct_origin",
    "Prompt origin",
    "Share of prompts by prompt_origin in range.",
    [
        sql(
            f"SELECT coalesce(prompt_origin, '(none)') AS origin, count(*) AS prompts {MSG} AND message_class IN ('human_prompt','queued_prompt') GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
bars(
    "ct_reason_ts",
    "Reasoning blocks by agent",
    "reasoning messages per bucket, split by whether plaintext was stored or the block was signature-only / redacted.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, agent || CASE WHEN coalesce((detail->>'signature_only')::boolean, false) OR coalesce((detail->>'redacted')::boolean, false) THEN ' (no plaintext)' ELSE ' (plaintext)' END AS metric, count(*) AS value {MSG} AND message_class = 'reasoning' GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "ct_att_ts",
    "Attachments by kind",
    "ah.attachment per bucket by kind (image, file, document, pasted_text).",
    [
        sql(
            f"SELECT {BUCKET.format(col='a.ts')}, a.kind AS metric, count(*) AS value {ATT} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
table(
    "ct_att_mime",
    "Attachments by type",
    "Attachments per kind, source and MIME type with their recorded size.",
    [
        sql(
            f"SELECT a.kind, a.source, coalesce(a.mime, '(unknown)') AS mime, count(*) AS attachments, sum(a.size_bytes) AS bytes, count(DISTINCT a.session_id) AS sessions {ATT} GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 40"
        )
    ],
    cols=[col("attachments", gauge=True), col("bytes", unit="bytes")],
    sort="attachments",
)
bars(
    "ct_ft_ts",
    "File touches by operation",
    "ah.file_touch per bucket by op: read, create, edit, delete, move.",
    [
        sql(
            f"SELECT {BUCKET.format(col='f.ts')}, f.op AS metric, count(*) AS value {FT} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
timeseries(
    "ct_lines",
    "Lines added and removed",
    "Sum of lines_added and lines_removed on edits and creates per bucket (ah.file_touch).",
    [
        sql(
            f"SELECT {BUCKET.format(col='f.ts')}, sum(f.lines_added) AS added, -sum(f.lines_removed) AS removed {FT} GROUP BY 1 ORDER BY 1",
            ts=True,
        )
    ],
    bars=True,
)
table(
    "ct_ft_repo",
    "File changes by repo",
    "Edits, creates and deletes per repo (ah.file_touch.repo_id via ah.repo, filled from checkout roots). Reads counted separately.",
    [
        sql(
            f"SELECT {RP(FREPO)} AS repo, count(*) FILTER (WHERE f.op <> 'read') AS changes, count(*) FILTER (WHERE f.op = 'read') AS reads, count(DISTINCT f.path) FILTER (WHERE f.op <> 'read') AS files_changed, sum(f.lines_added) AS lines_added, sum(f.lines_removed) AS lines_removed, count(DISTINCT f.session_id) AS sessions {FT} GROUP BY 1 ORDER BY changes DESC LIMIT 40"
        )
    ],
    cols=[col("changes", gauge=True)],
    sort="changes",
)
table(
    "ct_ft_hot",
    "Most-changed files",
    "Repo-relative paths with the most edits in range (absolute path when outside a known checkout).",
    [
        sql(
            f"SELECT {RP(FREPO)} AS repo, {TX('coalesce(f.repo_path, f.path)')} AS path, count(*) AS changes, sum(f.lines_added) AS added, sum(f.lines_removed) AS removed, count(DISTINCT f.session_id) AS sessions, max(f.ts) AS last_ts {FT} AND f.op <> 'read' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 50"
        )
    ],
    cols=[col("changes", gauge=True), col("path", width=460)],
    sort="changes",
)

tab(
    "Content",
    [
        (4, [("ct_prompts", 4), ("ct_reason", 4), ("ct_harness", 4), ("ct_tio", 4), ("ct_att", 4), ("ct_files", 4)]),
        (9, [("ct_group", 12), ("ct_harness_ts", 12)]),
        (9, [("ct_volume", 24)]),
        (8, [("ct_origin_ts", 14), ("ct_origin", 10)]),
        (8, [("ct_reason_ts", 12), ("ct_att_ts", 12)]),
        (8, [("ct_att_mime", 24)]),
        (8, [("ct_ft_ts", 12), ("ct_lines", 12)]),
        (10, [("ct_ft_repo", 12), ("ct_ft_hot", 12)]),
    ],
)

# ==========================================================================
# TAB: Tokens & models
# ==========================================================================
stat(
    "tk_out",
    "Output tokens",
    "Sum of llm_call.output in range (Codex output includes reasoning).",
    [sql(f"SELECT coalesce(sum(l.output),0) {LLM}")],
)
stat(
    "tk_reason",
    "Reasoning share",
    "reasoning / output tokens (Codex reports reasoning separately; Claude does not).",
    [sql(f"SELECT sum(l.reasoning)::numeric/nullif(sum(l.output),0) {LLM}")],
    unit="percentunit",
    decimals=1,
)
stat(
    "tk_cw",
    "Cache writes (1h share)",
    "Share of cache-write tokens written with the 1-hour TTL (priced higher than 5 min).",
    [
        sql(
            f"SELECT sum(l.cache_write_1h)::numeric/nullif(sum(coalesce(l.cache_write_5m,0)+coalesce(l.cache_write_1h,0)),0) {LLM}"
        )
    ],
    unit="percentunit",
    decimals=1,
)
stat(
    "tk_ctx",
    "Median context fill",
    "p50 of context_tokens / context_window over calls that report a window.",
    [
        sql(
            f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY l.context_tokens::float/l.context_window) {LLM} AND l.context_window > 0"
        )
    ],
    unit="percentunit",
    decimals=1,
)
stat(
    "tk_ttft",
    "Median TTFT",
    "p50 turn time to first token (ah.turn.ttft_ms).",
    [
        sql(
            f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t.ttft_ms) FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND t.ttft_ms IS NOT NULL AND {NSAG('s')}"
        )
    ],
    unit="ms",
)
stat(
    "tk_web",
    "Web search + fetch",
    "Server-side web_search and web_fetch requests (Claude).",
    [sql(f"SELECT coalesce(sum(l.web_search_requests+l.web_fetch_requests),0) {LLM}")],
)
bars(
    "tk_class",
    "Token classes",
    "llm_call tokens per bucket by class.",
    [
        sql(
            f'SELECT {BUCKET.format(col="l.ts")}, sum(l.input_uncached) AS "input (uncached)", sum(l.cache_read) AS "cache read", sum(l.cache_write_5m) AS "cache write 5m", sum(l.cache_write_1h) AS "cache write 1h", sum(l.output) AS output {LLM} GROUP BY 1 ORDER BY 1',
            ts=True,
        )
    ],
)
bars(
    "tk_model",
    "Output tokens by model",
    "llm_call.output per bucket and model.",
    [
        sql(
            f"SELECT {BUCKET.format(col='l.ts')}, coalesce(l.model,'(none)') AS metric, sum(l.output) AS value {LLM} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "tk_ns",
    "Total tokens by namespace",
    "All token classes per bucket and namespace.",
    [
        sql(
            f"SELECT {BUCKET.format(col='l.ts')}, s.namespace AS metric, sum({TOK}) AS value {LLM} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
timeseries(
    "tk_cache_model",
    "Cache read share by agent",
    "cache_read / all input per bucket and agent.",
    [
        sql(
            f"SELECT {BUCKET.format(col='l.ts')}, l.agent AS metric, sum(l.cache_read)::float/nullif(sum({CIN}),0) AS value {LLM} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="percentunit",
    minv=0,
    maxv=1,
)
timeseries(
    "tk_turn_dur",
    "Turn duration p50 / p90",
    "ah.turn.duration_ms per bucket (complete turns).",
    [
        sql(
            f"SELECT {BUCKET.format(col='t.started_at')}, percentile_cont(0.5) WITHIN GROUP (ORDER BY t.duration_ms) AS p50, percentile_cont(0.9) WITHIN GROUP (ORDER BY t.duration_ms) AS p90 FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND t.duration_ms IS NOT NULL AND {NSAG('s')} GROUP BY 1 ORDER BY 1",
            ts=True,
        )
    ],
    unit="ms",
)
timeseries(
    "tk_ttft_ts",
    "Time to first token p50 / p90 by agent",
    "ah.turn.ttft_ms per bucket.",
    [
        sql(
            f"SELECT {BUCKET.format(col='t.started_at')}, s.agent || ' p50' AS metric, percentile_cont(0.5) WITHIN GROUP (ORDER BY t.ttft_ms) AS value FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND t.ttft_ms IS NOT NULL AND {NSAG('s')} GROUP BY 1,2 UNION ALL SELECT {BUCKET.format(col='t.started_at')}, s.agent || ' p90', percentile_cont(0.9) WITHIN GROUP (ORDER BY t.ttft_ms) FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND t.ttft_ms IS NOT NULL AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="ms",
)
timeseries(
    "tk_ctx_ts",
    "Context fill p50 / p90: root vs subagent",
    "context_tokens / context_window per call, per bucket, split by session kind.",
    [
        sql(
            f"SELECT {BUCKET.format(col='l.ts')}, CASE WHEN s.is_subagent THEN 'subagent' ELSE 'root' END || ' p50' AS metric, percentile_cont(0.5) WITHIN GROUP (ORDER BY l.context_tokens::float/l.context_window) AS value {LLM} AND l.context_window > 0 GROUP BY 1,2 UNION ALL SELECT {BUCKET.format(col='l.ts')}, CASE WHEN s.is_subagent THEN 'subagent' ELSE 'root' END || ' p90', percentile_cont(0.9) WITHIN GROUP (ORDER BY l.context_tokens::float/l.context_window) {LLM} AND l.context_window > 0 GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="percentunit",
    minv=0,
    maxv=1,
)
pie(
    "tk_effort",
    "Calls by reasoning effort",
    "llm_call.effort (Codex reasoning effort, Claude effort level).",
    [sql(f"SELECT coalesce(l.effort,'(none)') AS effort, count(*) AS calls {LLM} GROUP BY 1 ORDER BY 2 DESC")],
)
pie(
    "tk_stop",
    "Claude stop reasons",
    "stop_reason on Claude responses: tool_use, end_turn, max_tokens (truncated), refusal.",
    [
        sql(
            f"SELECT coalesce(l.stop_reason,'(none)') AS stop_reason, count(*) AS calls {LLM} AND l.agent='claude' GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
table(
    "tk_models",
    "Model scoreboard",
    "Per model over the range: calls, sessions, token volume, average output per call, cache share and context fill.",
    [
        sql(
            f"SELECT coalesce(l.model,'(none)') AS model, count(*) AS calls, count(DISTINCT l.session_id) AS sessions, sum(l.output) AS output, round(avg(l.output)) AS avg_output, sum(l.reasoning) AS reasoning, sum(l.cache_read)::numeric/nullif(sum({CIN}),0) AS cache_share, percentile_cont(0.9) WITHIN GROUP (ORDER BY l.context_tokens::float/nullif(l.context_window,0)) AS p90_ctx_fill, count(*) FILTER (WHERE l.is_api_error) AS api_errors, max(l.ts) AS last_used {LLM} GROUP BY 1 ORDER BY calls DESC LIMIT 40"
        )
    ],
    cols=[
        col("calls", gauge=True),
        col("cache_share", unit="percentunit"),
        col("p90_ctx_fill", unit="percentunit"),
        col("api_errors", text_colour=steps("text", (1, "red"))),
    ],
    sort="calls",
)

tab(
    "Tokens & models",
    [
        (4, [("tk_out", 4), ("tk_reason", 4), ("tk_cw", 4), ("tk_ctx", 4), ("tk_ttft", 4), ("tk_web", 4)]),
        (9, [("tk_class", 12), ("tk_model", 12)]),
        (8, [("tk_ns", 12), ("tk_cache_model", 12)]),
        (8, [("tk_turn_dur", 12), ("tk_ttft_ts", 12)]),
        (8, [("tk_ctx_ts", 12), ("tk_effort", 6), ("tk_stop", 6)]),
        (10, [("tk_models", 24)]),
    ],
)

# ==========================================================================
# TAB: Cost
# ==========================================================================
CLAUDE = "AND l.agent = 'claude'"
CODEX = "AND l.agent = 'codex'"
PROMPTS = f"(SELECT count(*) FROM ah.message WHERE $__timeFilter(ts) AND ah.is_genuine_prompt(message_class, prompt_origin, text) AND {NSAG()})"
ROOTS = f"(SELECT count(*) FROM ah.session WHERE $__timeFilter(first_event_at) AND NOT is_subagent AND NOT is_stub AND {NSAG()})"
stat(
    "co_total",
    "Priced cost",
    "All agents at list price (ah.llm_call x ah.model_pricing).",
    [sql(f"SELECT coalesce(sum({GUSD}),0) FROM {priced()}")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "co_claude",
    "Claude priced",
    "Priced cost of Claude calls.",
    [sql(f"SELECT coalesce(sum({GUSD}),0) FROM {priced(where=CLAUDE)}")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "co_codex",
    "Codex priced",
    "Priced cost of Codex calls.",
    [sql(f"SELECT coalesce(sum({GUSD}),0) FROM {priced(where=CODEX)}")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "co_per_prompt",
    "Cost per human prompt",
    "Priced cost / human prompts in range.",
    [sql(f"SELECT (SELECT sum({GUSD}) FROM {priced()}) / nullif({PROMPTS},0)")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "co_per_session",
    "Cost per root session",
    "Priced cost / top-level sessions started in range.",
    [sql(f"SELECT (SELECT sum({GUSD}) FROM {priced()}) / nullif({ROOTS},0)")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "co_unpriced",
    "Unpriced calls",
    "LLM calls whose model has no ah.model_pricing row (cost reads as 0). Add a pricing row to fix.",
    [
        sql(
            f"SELECT count(*) {LLM} AND l.model IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ah.model_pricing mp WHERE mp.model = l.model AND mp.effective_from <= l.ts::date)"
        )
    ],
    steps_=steps("green", (1, "yellow")),
)
bars(
    "co_agent",
    "Priced cost by agent",
    "Priced cost per bucket and agent.",
    [
        sql(
            f"SELECT g.time, g.metric, sum({GUSD}) AS value FROM {priced(BUCKET.format(col='l.ts') + ', l.agent AS metric,')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="currencyUSD",
    calcs=["sum"],
)
bars(
    "co_ns",
    "Priced cost by namespace",
    "Priced cost per bucket and namespace (the Work/Personal boundary).",
    [
        sql(
            f"SELECT g.time, g.metric, sum({GUSD}) AS value FROM {priced(BUCKET.format(col='l.ts') + ', s.namespace AS metric,')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="currencyUSD",
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
timeseries(
    "co_cum",
    "Cumulative priced cost",
    "Running total over the range per agent.",
    [
        sql(
            f"SELECT time, metric, sum(v) OVER (PARTITION BY metric ORDER BY time) AS value FROM (SELECT g.time, g.metric, sum({GUSD}) AS v FROM {priced('$__timeGroupAlias(l.ts, $__interval), l.agent AS metric,')} GROUP BY 1,2) x ORDER BY 1",
            ts=True,
        )
    ],
    unit="currencyUSD",
    fill=20,
)
bars(
    "co_class",
    "Priced cost by token class",
    "Cost per bucket split into uncached input, cache read, cache write and output at list price.",
    [
        sql(
            f'SELECT g.time, sum(ah.priced_usd(g.model, g.d, g.i, 0, 0, 0, 0)) AS "input (uncached)", sum(ah.priced_usd(g.model, g.d, 0, g.cr, 0, 0, 0)) AS "cache read", sum(ah.priced_usd(g.model, g.d, 0, 0, g.w5, g.w1, 0)) AS "cache write", sum(ah.priced_usd(g.model, g.d, 0, 0, 0, 0, g.o)) AS output FROM {priced(BUCKET.format(col="l.ts") + ",")} GROUP BY 1 ORDER BY 1',
            ts=True,
        )
    ],
    unit="currencyUSD",
    calcs=["sum"],
)
table(
    "co_sessions",
    "Most expensive session trees",
    "Root sessions started in range with their whole subagent tree priced from ah.llm_call. Click a session to drill down.",
    [
        sql(
            f"WITH t AS (SELECT r.id AS root, l.session_id, l.model, l.ts::date AS d, count(*) AS calls, sum(l.output) AS output, sum(l.input_uncached) AS i, sum(l.cache_read) AS cr, sum(l.cache_write_5m) AS w5, sum(l.cache_write_1h) AS w1, sum(l.output) AS o FROM ah.session r JOIN ah.session s ON coalesce(s.root_session_id, s.id) = r.id JOIN ah.llm_call l ON l.session_id = s.id WHERE $__timeFilter(r.first_event_at) AND NOT r.is_subagent AND {NSAG('r')} GROUP BY 1,2,3,4), c AS (SELECT root, count(DISTINCT session_id) AS sessions, sum(calls) AS llm_calls, sum(output) AS output, sum(ah.priced_usd(model, d, i, cr, w5, w1, o)) AS cost_usd FROM t GROUP BY 1 ORDER BY cost_usd DESC NULLS LAST LIMIT 30) SELECT r.namespace, r.agent, r.session_uid, {RP('ah.project_of(r.cwd)')} AS project, {TX('coalesce(r.custom_title, r.title)')} AS title, r.first_event_at AS started, c.sessions, c.llm_calls, c.output, c.cost_usd FROM c JOIN ah.session r ON r.id = c.root ORDER BY c.cost_usd DESC NULLS LAST"
        )
    ],
    cols=[
        col("session_uid", links=SESSION_LINK),
        col("cost_usd", unit="currencyUSD", gauge=True),
        col("title", width=320),
    ],
    sort="cost_usd",
)
table(
    "co_tasks",
    "Cost by backlog task",
    "ah.v_task_effort: sessions where the task appears in a human prompt or brief, with priced and Claude-reported cost. Tasks last referenced in range.",
    [
        sql(
            f"SELECT task_key, {TX('title')} AS title, status, {RP('repo_slug')} AS repo_slug, sessions_counted, sessions_mentioned, wall_s, tokens, priced_cost_usd AS cost_usd, claude_cost_usd, commits, last_ts FROM ah.v_task_effort WHERE $__timeFilter(last_ts) AND {CTX} ORDER BY priced_cost_usd DESC NULLS LAST LIMIT 30"
        )
    ],
    cols=[
        col("cost_usd", unit="currencyUSD", gauge=True),
        col("claude_cost_usd", unit="currencyUSD"),
        col("wall_s", unit="s"),
    ],
    sort="cost_usd",
)
PROJ_SRC = f"FROM ah.llm_call l {JS.format(a='l')} LEFT JOIN ah.session r ON r.id = s.root_session_id WHERE $__timeFilter(l.ts) AND {NSAG('s')}"
bars(
    "co_project",
    "Priced cost by project",
    "Priced cost per bucket and project (root session cwd), top 10 projects in range.",
    [
        sql(
            f"WITH c AS (SELECT g.time, g.project, sum({GUSD}) AS usd FROM {priced(BUCKET.format(col='l.ts') + ', ah.project_of(coalesce(r.cwd, s.cwd)) AS project,', src=PROJ_SRC)} GROUP BY 1,2), top AS (SELECT project FROM c GROUP BY 1 ORDER BY sum(usd) DESC NULLS LAST LIMIT 10) SELECT time, CASE WHEN project IN (SELECT project FROM top) THEN {RP('project')} ELSE '(other)' END AS metric, sum(usd) AS value FROM c GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="currencyUSD",
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "co_pricing",
    "Model pricing",
    "ah.model_pricing: USD per million tokens. Add a newer effective_from row when a price changes.",
    [
        sql(
            "SELECT model, effective_from, input_per_mtok, cached_input_per_mtok, cache_write_per_mtok, cache_write_1h_per_mtok, output_per_mtok, source FROM ah.model_pricing ORDER BY model, effective_from DESC"
        )
    ],
    cols=[
        col(c, unit="currencyUSD")
        for c in (
            "input_per_mtok",
            "cached_input_per_mtok",
            "cache_write_per_mtok",
            "cache_write_1h_per_mtok",
            "output_per_mtok",
        )
    ],
)

tab(
    "Cost",
    [
        (
            4,
            [
                ("co_total", 4),
                ("co_claude", 4),
                ("co_codex", 4),
                ("co_per_prompt", 4),
                ("co_per_session", 4),
                ("co_unpriced", 4),
            ],
        ),
        (8, [("co_agent", 12), ("co_ns", 12)]),
        (8, [("co_cum", 12), ("co_class", 12)]),
        (9, [("co_project", 24)]),
        (10, [("co_sessions", 24)]),
        (12, [("co_tasks", 14), ("co_pricing", 10)]),
    ],
)

# ==========================================================================
# TAB: Sessions & subagents
# ==========================================================================
SP = f"FROM ah.subagent_spawn p JOIN ah.session s ON s.id = p.parent_session_id WHERE $__timeFilter(p.spawned_at) AND {NSAG('s')}"
SO = f"FROM ah.v_spawn_outcome o WHERE $__timeFilter(o.spawned_at) AND {NS('o.parent_namespace')} AND {AG('o.parent_agent')}"
stat(
    "su_spawns",
    "Spawns",
    "Subagent spawns (Claude Agent/Task calls, Codex spawn_agent) in range.",
    [sql(f"SELECT count(*) {SP}")],
)
stat(
    "su_done",
    "Completed",
    "Share of spawns with completion_status=completed.",
    [sql(f"SELECT avg((p.completion_status='completed')::int) {SP}")],
    unit="percentunit",
    decimals=1,
    steps_=steps("red", (0.8, "yellow"), (0.95, "green")),
)
stat(
    "su_failed",
    "Failed / interrupted / killed",
    "Spawns that did not complete cleanly.",
    [sql(f"SELECT count(*) {SP} AND p.completion_status IN ('failed','interrupted','killed')")],
    steps_=steps("green", (1, "yellow"), (20, "red")),
)
stat(
    "su_redo",
    "Redo rate",
    "Share of spawns re-issued for the same task within 2 h (ah.v_spawn_outcome.redo).",
    [sql(f"SELECT avg(o.redo::int) {SO}")],
    unit="percentunit",
    decimals=1,
    steps_=RATE_RED,
)
stat(
    "su_bg",
    "Background share",
    "Share of spawns launched in the background.",
    [sql(f"SELECT avg(coalesce(p.background,false)::int) {SP}")],
    unit="percentunit",
    decimals=1,
)
stat(
    "su_compact",
    "Compactions",
    "Context compactions in range (ah.compaction).",
    [sql(f"SELECT count(*) FROM ah.compaction c {JS.format(a='c')} WHERE $__timeFilter(c.ts) AND {NSAG('s')}")],
)
bars(
    "su_model",
    "Spawns by resolved model",
    "Spawns per bucket by the model the child actually ran.",
    [
        sql(
            f"SELECT {BUCKET.format(col='p.spawned_at')}, coalesce(p.resolved_model, p.requested_model, '(inherit)') AS metric, count(*) AS value {SP} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bargauge(
    "su_type",
    "Spawns by agent type",
    "Recorded request type for Claude, Codex and pi spawns. (unknown) means no supported source or certain harness default was linked; top 20.",
    [
        sql(
            f"SELECT coalesce(p.requested_type,'(unknown)') AS agent_type, count(*) AS spawns {SP} GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        )
    ],
)
pie(
    "su_status",
    "Completion status",
    "subagent_spawn.completion_status ((none) = no completion recorded yet, usually Codex).",
    [
        sql(
            f"SELECT coalesce(p.completion_status,'(none)') AS status, count(*) AS spawns {SP} GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
timeseries(
    "su_dur",
    "Spawn duration p50 / p90",
    "Spawn to completion per bucket (ah.v_spawn_outcome.duration_s).",
    [
        sql(
            f"SELECT {BUCKET.format(col='o.spawned_at')}, percentile_cont(0.5) WITHIN GROUP (ORDER BY o.duration_s) AS p50, percentile_cont(0.9) WITHIN GROUP (ORDER BY o.duration_s) AS p90 {SO} AND o.duration_s IS NOT NULL GROUP BY 1 ORDER BY 1",
            ts=True,
        )
    ],
    unit="s",
)
barchart(
    "su_dur_hist",
    "Spawn duration distribution",
    "Completed spawns bucketed by wall time.",
    [
        sql(
            f"SELECT b.label AS duration, count(o.*) AS spawns FROM (VALUES (1,'<1m',0,60),(2,'1-5m',60,300),(3,'5-15m',300,900),(4,'15-30m',900,1800),(5,'30-60m',1800,3600),(6,'1-2h',3600,7200),(7,'>2h',7200,1e9)) b(o,label,lo,hi) LEFT JOIN (SELECT o.duration_s {SO} AND o.duration_s IS NOT NULL) o ON o.duration_s >= b.lo AND o.duration_s < b.hi GROUP BY b.o, b.label ORDER BY b.o"
        )
    ],
    x="duration",
)
bargauge(
    "su_effort",
    "Spawns by model and effort",
    "resolved model x reasoning_effort, top 20.",
    [
        sql(
            f"SELECT coalesce(p.resolved_model, p.requested_model, '(inherit)') || ' / ' || coalesce(p.reasoning_effort,'-') AS route, count(*) AS spawns {SP} GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        )
    ],
)
bars(
    "su_compact_ts",
    "Compactions by trigger",
    "Compactions per bucket by trigger (manual, auto, or blank for Codex).",
    [
        sql(
            f"SELECT {BUCKET.format(col='c.ts')}, CASE WHEN s.is_subagent THEN 'subagent ' ELSE 'root ' END || coalesce(nullif(c.trigger,''),'codex') AS metric, count(*) AS value FROM ah.compaction c {JS.format(a='c')} WHERE $__timeFilter(c.ts) AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
table(
    "su_routing",
    "Model routing report",
    "ah.routing_report: spawns by resolved model and agent type, redo rate, child cost and the CI outcome of child commits.",
    [
        sql(
            f"SELECT resolved_model AS model, agent_type, spawns, completed, redo_rate, avg_duration_s, total_priced_cost_usd AS cost_usd, commits, ci_success_rate FROM ah.routing_report({SINCE}, {NSARR}) ORDER BY spawns DESC LIMIT 60"
        )
    ],
    cols=[
        col("spawns", gauge=True),
        col("redo_rate", unit="percentunit", text_colour=RATE_RED),
        col("ci_success_rate", unit="percentunit"),
        col("cost_usd", unit="currencyUSD"),
        col("avg_duration_s", unit="s"),
    ],
    sort="spawns",
)
table(
    "su_problem",
    "Spawns that failed, were redone or interrupted",
    "Recent v_spawn_outcome rows with a non-completed status or redo=true. description is the spawn's short label, not the brief.",
    [
        sql(
            f"SELECT o.spawned_at, o.parent_namespace AS namespace, o.child_agent_type AS agent_type, o.resolved_model AS model, {TX('o.name')} AS name, {TX('o.description')} AS description, o.completion_status, o.lane_return_status, o.redo, o.duration_s, o.priced_cost_usd AS cost_usd, o.child_session_uid AS session_uid {SO} AND (o.redo OR o.completion_status IN ('failed','interrupted','killed')) ORDER BY o.spawned_at DESC LIMIT 60"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("cost_usd", unit="currencyUSD"), col("duration_s", unit="s")],
)
table(
    "su_trees",
    "Largest session trees",
    "ah.v_session_tree roots started in range, by LLM calls across the whole tree.",
    [
        sql(
            f"SELECT t.namespace, t.agent, t.session_uid, {RP('ah.project_of(t.cwd)')} AS project, t.sessions, t.turns_human, t.llm_calls, t.output, t.tool_calls, t.tool_errors, t.compactions, t.commits, t.pushes, t.first_event_at AS started FROM ah.v_session_tree t WHERE $__timeFilter(t.first_event_at) AND {NSAG('t')} ORDER BY t.llm_calls DESC LIMIT 30"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("llm_calls", gauge=True)],
    sort="llm_calls",
)
barchart(
    "su_depth",
    "Spawn depth",
    "Child sessions by nesting depth (1 = spawned by a root).",
    [
        sql(
            f"SELECT coalesce(spawn_depth,0)::text AS depth, count(*) AS sessions FROM ah.session WHERE $__timeFilter(first_event_at) AND is_subagent AND {NSAG()} GROUP BY spawn_depth ORDER BY spawn_depth"
        )
    ],
    x="depth",
)
stat(
    "su_dropped",
    "Tokens dropped by compaction",
    "Sum of pre_tokens - post_tokens over compactions in range.",
    [
        sql(
            f"SELECT coalesce(sum(coalesce(c.dropped_tokens, c.pre_tokens - c.post_tokens, 0)),0) FROM ah.compaction c {JS.format(a='c')} WHERE $__timeFilter(c.ts) AND {NSAG('s')}"
        )
    ],
)

tab(
    "Sessions & subagents",
    [
        (4, [("su_spawns", 4), ("su_done", 4), ("su_failed", 4), ("su_redo", 4), ("su_bg", 4), ("su_compact", 4)]),
        (9, [("su_model", 12), ("su_type", 6), ("su_status", 6)]),
        (8, [("su_dur", 8), ("su_dur_hist", 8), ("su_effort", 8)]),
        (8, [("su_compact_ts", 12), ("su_depth", 8), ("su_dropped", 4)]),
        (10, [("su_routing", 24)]),
        (10, [("su_problem", 24)]),
        (10, [("su_trees", 24)]),
    ],
)

# ==========================================================================
# TAB: Orchestration (parser v4: loop/wave/fan-out/workflow roots and descendants, session rollup v4)
# ==========================================================================
ROOTK = "('loop_root','wave_root','fanout_root','workflow_root')"
ORCH = f"(s.orchestration_kind IN {ROOTK} OR s.is_orchestration_descendant)"
RSLUG = "coalesce(r.repo_slug, '(no repo)')"
SR = f"FROM ah.session s JOIN ah.session_rollup r ON r.session_id = s.id WHERE $__timeFilter(s.first_event_at) AND NOT s.is_stub AND {NSAG('s')}"
for key, kind, label in [
    ("or_loop", "loop_root", "Loop roots"),
    ("or_wave", "wave_root", "Wave roots"),
    ("or_fan", "fanout_root", "Fan-out roots"),
    ("or_wf", "workflow_root", "Workflow roots"),
]:
    stat(
        key,
        label,
        f"Sessions started in range classified orchestration_kind = {kind} (ah.v_session_orchestration).",
        [sql(f"SELECT count(*) {SR} AND s.orchestration_kind = '{kind}'")],
    )
stat(
    "or_share",
    "Orchestrated sessions",
    "Share of sessions started in range that are an orchestration root or any descendant of one (loop members linked heuristically included).",
    [sql(f"SELECT avg({ORCH}::int) {SR}")],
    unit="percentunit",
    decimals=1,
)
stat(
    "or_cost",
    "Orchestrated cost share",
    "Share of priced cost (session_rollup.priced_cost_usd) from orchestration roots and their descendants.",
    [sql(f"SELECT sum(r.priced_cost_usd) FILTER (WHERE {ORCH}) / nullif(sum(r.priced_cost_usd), 0) {SR}")],
    unit="percentunit",
    decimals=1,
)
bars(
    "or_kind_ts",
    "Sessions by orchestration kind",
    "Every session (roots and subagents) started per bucket by orchestration_kind.",
    [
        sql(
            f"SELECT {BUCKET.format(col='s.first_event_at')}, s.orchestration_kind AS metric, count(*) AS value {SR} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "or_cost_ts",
    "Priced cost: orchestrated vs direct",
    "session_rollup.priced_cost_usd per bucket (by session start): orchestration trees vs everything else.",
    [
        sql(
            f"SELECT {BUCKET.format(col='s.first_event_at')}, CASE WHEN {ORCH} THEN 'orchestrated' ELSE 'direct' END AS metric, sum(r.priced_cost_usd) AS value {SR} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="currencyUSD",
)
table(
    "or_roots",
    "Orchestration roots",
    "Roots started in range with their whole tree (every session whose orchestration_root_session_id is the root): sessions, priced cost, active time, prompts and failures. evidence is the signal that classified the root.",
    [
        sql(
            f"SELECT s.first_event_at, s.orchestration_kind AS kind, s.namespace, s.agent, {RP('coalesce(r.repo_slug, ah.project_of(s.cwd))')} AS repo, count(d.id) AS tree_sessions, sum(dr.priced_cost_usd) AS tree_cost_usd, sum(dr.active_s) AS tree_active_s, sum(dr.human_prompts) AS prompts, sum(dr.error_count) AS failures, {TX('s.orchestration_evidence')} AS evidence, s.session_uid {SR} AND s.orchestration_kind IN {ROOTK} AND NOT s.is_subagent GROUP BY s.id, r.repo_slug ORDER BY s.first_event_at DESC LIMIT 100".replace(
                "WHERE $__timeFilter",
                "LEFT JOIN ah.session d ON d.orchestration_root_session_id = s.id LEFT JOIN ah.session_rollup dr ON dr.session_id = d.id WHERE $__timeFilter",
            )
        )
    ],
    cols=[
        col("session_uid", links=SESSION_LINK),
        col("tree_cost_usd", unit="currencyUSD", gauge=True),
        col("tree_active_s", unit="s"),
        col("evidence", width=260),
    ],
)
bars(
    "or_active",
    "Active time by namespace",
    "Sum of session_rollup.active_s (gaps of at most 5 min between events) for root sessions started per bucket. Subagents overlap their root and are left out.",
    [
        sql(
            f"SELECT {BUCKET.format(col='s.first_event_at')}, s.namespace AS metric, sum(r.active_s) AS value {SR} AND NOT s.is_subagent GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="s",
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bargauge(
    "or_active_repo",
    "Active time by repo",
    "Root-session active time per session_rollup.repo_slug (the cwd's checkout), top 20.",
    [
        sql(
            f"SELECT {RP(RSLUG)} AS repo, sum(r.active_s) AS active {SR} AND NOT s.is_subagent GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 20"
        )
    ],
    unit="s",
)
pie(
    "or_final",
    "How sessions ended",
    "session_rollup.final_turn_status of root sessions started in range.",
    [
        sql(
            f"SELECT coalesce(r.final_turn_status, '(none)') AS status, count(*) AS sessions {SR} AND NOT s.is_subagent GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
table(
    "or_err",
    "Sessions with the most failures",
    "session_rollup.error_count (failed tool calls + failed Codex operations) against the work done.",
    [
        sql(
            f"SELECT s.first_event_at, s.namespace, s.agent, s.orchestration_kind AS kind, {RP('coalesce(r.repo_slug, ah.project_of(s.cwd))')} AS repo, r.error_count AS failures, r.tool_calls, r.human_prompts AS prompts, r.active_s, r.priced_cost_usd AS cost_usd, r.final_turn_status, s.session_uid {SR} AND r.error_count > 0 ORDER BY r.error_count DESC LIMIT 50"
        )
    ],
    cols=[
        col("session_uid", links=SESSION_LINK),
        col("failures", gauge=True),
        col("active_s", unit="s"),
        col("cost_usd", unit="currencyUSD"),
    ],
    sort="failures",
)
table(
    "or_cont",
    "Continuations",
    "Sessions linked to the one they continue (resume, fork, compaction continuation, /clear), from ah.session_continuation and Codex forks. Only links the transcripts record explicitly are resolved.",
    [
        sql(
            f"SELECT v.first_event_at, v.namespace, v.agent, v.continuation_kind, {TX('v.display_title')} AS title, v.session_uid, v.continued_from_session_uid FROM ah.v_session_state v WHERE v.continuation_kind IS NOT NULL AND $__timeFilter(v.first_event_at) AND {NSAG('v')} ORDER BY v.first_event_at DESC LIMIT 100"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("continued_from_session_uid", links=SESSION_LINK)],
)

tab(
    "Orchestration",
    [
        (4, [("or_loop", 4), ("or_wave", 4), ("or_fan", 4), ("or_wf", 4), ("or_share", 4), ("or_cost", 4)]),
        (9, [("or_kind_ts", 12), ("or_cost_ts", 12)]),
        (10, [("or_roots", 24)]),
        (8, [("or_active", 10), ("or_active_repo", 8), ("or_final", 6)]),
        (9, [("or_err", 24)]),
        (7, [("or_cont", 24)]),
    ],
)

# ==========================================================================
# TAB: Agent efficiency (the SQLite collector's agent_efficiency_* view, from SQL)
# ==========================================================================
ROOT_LLM = f"FROM ah.llm_call l {JS.format(a='l')} WHERE $__timeFilter(l.ts) AND NOT s.is_subagent AND {NSAG('s')}"
stat(
    "ef_root_calls",
    "Root LLM calls",
    "LLM calls made by top-level sessions (the orchestrator), excluding subagents.",
    [sql(f"SELECT count(*) {ROOT_LLM}")],
)
stat(
    "ef_calls_prompt",
    "Root calls per human prompt",
    "Root LLM calls / human prompts: how many model round-trips each instruction costs the orchestrator.",
    [
        sql(
            f"SELECT (SELECT count(*) {ROOT_LLM})::numeric / nullif((SELECT count(*) FROM ah.message WHERE $__timeFilter(ts) AND ah.is_genuine_prompt(message_class, prompt_origin, text) AND {NSAG()}),0)"
        )
    ],
    decimals=1,
)
stat(
    "ef_calls_spawn",
    "Root calls per spawn",
    "Root LLM calls / subagent spawns. Falling = more delegation per orchestrator step.",
    [sql(f"SELECT (SELECT count(*) {ROOT_LLM})::numeric / nullif((SELECT count(*) {SP}),0)")],
    decimals=1,
)
stat(
    "ef_turn_err",
    "Turn errors",
    "Turns ending in error or aborted (ah.turn.status).",
    [
        sql(
            f"SELECT count(*) FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND t.status IN ('error','aborted') AND {NSAG('s')}"
        )
    ],
    steps_=steps("green", (1, "yellow"), (25, "red")),
)
stat(
    "ef_tool_err",
    "Tool error rate",
    "Tool calls with outcome error / timeout over all tool calls.",
    [
        sql(
            f"SELECT avg((c.outcome IN ('error','timeout'))::int) FROM ah.tool_call c {JS.format(a='c')} WHERE $__timeFilter(c.started_at) AND {NSAG('s')}"
        )
    ],
    unit="percentunit",
    decimals=2,
    steps_=RATE_RED,
)
# Codex fake-cell waits: a code-mode `wait` on an exec cell id the session never yielded
# (none, x, bogus, ...) where the root meant collaboration wait_agent. Each fails in ~0.5 s and costs a
# root turn. Red at 50 per root-hour: one root made 263 in its first clock hour (739 per 1,000 root
# calls), and over a 30 day window only 4 roots reached 50 in any hour.
FAKE_HOUR = steps("green", (10, "yellow"), (50, "red"))
FAKE_1K = steps("green", (5, "yellow"), (20, "red"))
FCW = f"FROM ah.v_fake_cell_wait_hourly WHERE $__timeFilter(hour) AND {NSAG()}"
stat(
    "ef_fake_1k",
    "Fake-cell waits per 1,000 root calls",
    "Codex code-mode wait calls on an exec cell the session never yielded (error_class fake_cell_wait) by top-level sessions, per 1,000 root LLM calls in range. Loop v2.1 pooled 17.3; the v2.2 target is ~0.",
    [
        sql(
            f"SELECT 1000.0 * (SELECT count(*) FROM ah.tool_call c {JS.format(a='c')} WHERE $__timeFilter(c.started_at) AND c.error_class = 'fake_cell_wait' AND NOT s.is_subagent AND {NSAG('s')}) / nullif((SELECT count(*) {ROOT_LLM}),0)"
        )
    ],
    decimals=1,
    steps_=FAKE_1K,
)
timeseries(
    "ef_fake_ts",
    "Fake-cell waits per root per hour",
    "ah.v_fake_cell_wait_hourly: fake_cell_wait calls per root session per clock hour (hours with none are gaps). Yellow at 10, red at 50 per root-hour.",
    [
        sql(
            f"SELECT hour AS time, {RP_NO_REPO} || ' ' || left(session_uid, 8) AS metric, fake_cell_waits AS value {FCW} ORDER BY 1",
            ts=True,
        )
    ],
    points=True,
    steps_=FAKE_HOUR,
    threshold_style="dashed+area",
    legend_mode="table",
    legend_pos="right",
    calcs=["max", "sum"],
)
table(
    "ef_fake_roots",
    "Fake-cell waits by root",
    "ah.v_fake_cell_wait_hourly summed per root session over the range. wait_agent_calls and root_llm_calls count only the hours that had a fake-cell wait; per_1k is fake-cell waits per 1,000 of those root LLM calls.",
    [
        sql(
            f"SELECT session_uid, {RP_NO_REPO} AS repo, orchestration_kind AS kind, sum(fake_cell_waits) AS fake_cell_waits, max(fake_cell_waits) AS peak_hour, min(hour) AS first_hour, sum(wait_agent_calls) AS wait_agent_calls, sum(root_llm_calls) AS root_llm_calls, round(1000.0 * sum(fake_cell_waits) / nullif(sum(root_llm_calls),0), 1) AS per_1k {FCW} GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 50"
        )
    ],
    cols=[
        col("session_uid", links=SESSION_LINK),
        col("peak_hour", bg=FAKE_HOUR),
        col("fake_cell_waits", gauge=True),
        col("per_1k", text_colour=steps("green", (20, "yellow"), (100, "red"))),
    ],
    sort="fake_cell_waits",
)
stat(
    "ef_interrupts",
    "Interrupts",
    "User interrupts (session_event kind=interrupt).",
    [
        sql(
            f"SELECT count(*) FROM ah.session_event e {JS.format(a='e')} WHERE $__timeFilter(e.ts) AND e.kind='interrupt' AND {NSAG('s')}"
        )
    ],
)
timeseries(
    "ef_active",
    "Active sessions by role",
    "Distinct sessions making LLM calls per bucket: root vs subagent.",
    [
        sql(
            f"SELECT {BUCKET.format(col='l.ts')}, CASE WHEN s.is_subagent THEN 'subagent' ELSE 'root' END AS metric, count(DISTINCT l.session_id) AS value {LLM} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    fill=20,
)
timeseries(
    "ef_ratio",
    "Subagent / root call ratio",
    "Subagent LLM calls per root LLM call, per bucket. Higher = more work delegated.",
    [
        sql(
            f'SELECT {BUCKET.format(col="l.ts")}, count(*) FILTER (WHERE s.is_subagent)::float / nullif(count(*) FILTER (WHERE NOT s.is_subagent),0) AS "subagent calls per root call" {LLM} GROUP BY 1 ORDER BY 1',
            ts=True,
        )
    ],
    decimals=2,
)
timeseries(
    "ef_calls_per_prompt_ts",
    "Root calls per human prompt",
    "Per bucket: orchestrator round-trips per instruction.",
    [
        sql(
            f'WITH c AS (SELECT {BUCKET.format(col="l.ts")}, count(*) AS n {ROOT_LLM} GROUP BY 1), p AS (SELECT {BUCKET.format(col="ts")}, count(*) AS n FROM ah.message WHERE $__timeFilter(ts) AND ah.is_genuine_prompt(message_class, prompt_origin, text) AND {NSAG()} GROUP BY 1) SELECT c.time, c.n::float / nullif(p.n,0) AS "root calls per prompt" FROM c JOIN p USING (time) ORDER BY 1',
            ts=True,
        )
    ],
    decimals=1,
)
bars(
    "ef_turn_err_ts",
    "Turn and API errors by kind",
    "Aborted/errored turns by abort reason plus API errors by error_kind, per bucket.",
    [
        sql(
            f"SELECT {BUCKET.format(col='t.started_at')}, 'turn ' || t.status || coalesce(' ' || t.abort_reason,'') AS metric, count(*) AS value FROM ah.turn t {JS.format(a='t')} WHERE $__timeFilter(t.started_at) AND t.status IN ('error','aborted') AND {NSAG('s')} GROUP BY 1,2 UNION ALL SELECT {BUCKET.format(col='l.ts')}, 'api ' || coalesce(l.error_kind,'unknown'), count(*) {LLM} AND l.is_api_error GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "ef_push",
    "Git pushes and PRs",
    "Agent git_event push / pr / commit per bucket.",
    [
        sql(
            f"SELECT {BUCKET.format(col='g.ts')}, g.op || coalesce(' ' || g.pr_action,'') AS metric, count(*) AS value FROM ah.git_event g {JS.format(a='g')} WHERE $__timeFilter(g.ts) AND g.op IN ('push','pr','commit') AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "ef_ci",
    "CI runs by conclusion",
    "GitHub Actions runs created in range on owned repos (ah.ci_run, hourly collector), by conclusion.",
    [
        sql(
            f"SELECT {BUCKET.format(col='created_at')}, coalesce(conclusion, status) AS metric, count(*) AS value FROM ah.ci_run WHERE $__timeFilter(created_at) AND {CTX} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    overrides=[
        {
            "matcher": {"id": "byName", "options": n},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}],
        }
        for n, c in (
            ("success", "green"),
            ("failure", "red"),
            ("cancelled", "text"),
            ("skipped", "blue"),
            ("in_progress", "yellow"),
            ("queued", "orange"),
        )
    ],
)
bars(
    "ef_review",
    "Review and gate commands",
    "Shell commands by verb for review/gate tools (coderabbit, just, gh, xreview) from Claude Bash meta and Codex tool_op.",
    [
        sql(
            f"SELECT {BUCKET.format(col='c.started_at')}, c.meta->>'cmd_verb' AS metric, count(*) AS value FROM ah.tool_call c {JS.format(a='c')} WHERE $__timeFilter(c.started_at) AND c.meta->>'cmd_verb' IN ('coderabbit','just','xreview','gh') AND {NSAG('s')} GROUP BY 1,2 UNION ALL SELECT {BUCKET.format(col='o.started_at')}, o.cmd_verb, count(*) FROM ah.tool_op o {JS.format(a='o')} WHERE $__timeFilter(o.started_at) AND o.cmd_verb IN ('coderabbit','just','xreview','gh') AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
gauge(
    "ef_rl",
    "Rate-limit used % (latest)",
    "Latest used_percent per limit window (ah.v_rate_limit_forecast).",
    [
        sql(
            f"SELECT agent || ' ' || coalesce(plan_type,'') || ' ' || window_kind || coalesce(' ' || (window_minutes/60)::text || 'h','') AS window, used_percent FROM ah.v_rate_limit_forecast WHERE used_percent IS NOT NULL AND latest_ts > now() - interval '1 day' AND {NSAG()} ORDER BY used_percent DESC LIMIT 8"
        )
    ],
)
timeseries(
    "ef_interrupt_ts",
    "Interventions",
    "Interrupts, user questions, denials and model switches per bucket (ah.session_event).",
    [
        sql(
            f"SELECT {BUCKET.format(col='e.ts')}, e.kind AS metric, count(*) AS value FROM ah.session_event e {JS.format(a='e')} WHERE $__timeFilter(e.ts) AND e.kind IN ('interrupt','user_question','denial','model_switch','effort_change','plan_mode','clear') AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)

tab(
    "Agent efficiency",
    [
        (
            4,
            [
                ("ef_root_calls", 4),
                ("ef_calls_prompt", 4),
                ("ef_calls_spawn", 4),
                ("ef_turn_err", 4),
                ("ef_tool_err", 4),
                ("ef_interrupts", 4),
            ],
        ),
        (8, [("ef_fake_1k", 4), ("ef_fake_ts", 20)]),
        (8, [("ef_fake_roots", 24)]),
        (8, [("ef_active", 12), ("ef_ratio", 12)]),
        (8, [("ef_calls_per_prompt_ts", 12), ("ef_turn_err_ts", 12)]),
        (8, [("ef_push", 12), ("ef_ci", 12)]),
        (8, [("ef_review", 12), ("ef_interrupt_ts", 12)]),
        (8, [("ef_rl", 24)]),
    ],
)

# ==========================================================================
# TAB: Loops
# ==========================================================================
LS = f"FROM ah.v_loop_summary WHERE $__timeFilter(launch_ts) AND {NS()} AND ({AGALL} OR root_agent IN ($agent)) AND ('__all__' IN ($repo) OR repo_slug IN ($repo))"
stat("lo_n", "Loops", "Loop runs launched in range.", [sql(f"SELECT count(*) {LS}")])
stat(
    "lo_cost",
    "Loop cost",
    "Priced cost of all loop sessions.",
    [sql(f"SELECT coalesce(sum(priced_cost_usd),0) {LS}")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "lo_wall",
    "Median wall time",
    "p50 loop wall time.",
    [sql(f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY wall_s) {LS}")],
    unit="s",
)
stat("lo_lanes", "Lanes", "Lanes across loops in range.", [sql(f"SELECT coalesce(sum(lanes),0) {LS}")])
stat("lo_commits", "Loop commits", "Commits made inside loops.", [sql(f"SELECT coalesce(sum(commits),0) {LS}")])
stat("lo_running", "Running", "Loops with no end recorded.", [sql(f"SELECT count(*) {LS} AND end_ts IS NULL")])
table(
    "lo_recent",
    "Recent loops",
    "ah.v_loop_summary launched in range. Click a loop id to load its lanes below.",
    [
        sql(
            f"SELECT loop_run_id AS loop, launch_ts AS launched, {RP('repo_slug')} AS repo, {TX('campaign_slug')} AS campaign, loop_number AS n, mode, status, wall_s, budget_s, lanes, sessions, llm_calls, tool_calls, tool_errors, api_errors, compactions, commits, pushes, priced_cost_usd AS cost_usd {LS} ORDER BY launch_ts DESC LIMIT 100"
        )
    ],
    cols=[
        col("loop", links=LOOP_LINK),
        col("cost_usd", unit="currencyUSD", gauge=True),
        col("wall_s", unit="s"),
        col("budget_s", unit="s"),
    ],
)
timeseries(
    "lo_cost_ts",
    "Cost per loop",
    "Each point is one loop at its launch time, by repo.",
    [
        sql(
            f"SELECT launch_ts AS time, {RP_NO_REPO} AS metric, priced_cost_usd AS value {LS} ORDER BY 1 LIMIT 500",
            ts=True,
        )
    ],
    unit="currencyUSD",
    points=True,
)
timeseries(
    "lo_wall_ts",
    "Loop wall time vs budget",
    "wall_s per loop (points) against its budget_s.",
    [sql(f'SELECT launch_ts AS time, wall_s AS "wall", budget_s AS "budget" {LS} ORDER BY 1 LIMIT 500', ts=True)],
    unit="s",
    points=True,
)
pie(
    "lo_return",
    "Lane return status",
    "lane.return_status for lanes of loops launched in range. (none) = the lane ended without a parsed lane-return block, not a failure.",
    [
        sql(
            f"SELECT coalesce(l.return_status,'(none)') AS status, count(*) AS lanes FROM ah.lane l JOIN ah.v_loop_summary v ON v.loop_run_id = l.loop_run_id WHERE $__timeFilter(v.launch_ts) AND {NS('v.namespace')} AND ('__all__' IN ($repo) OR v.repo_slug IN ($repo)) GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
    overrides=[
        {
            "matcher": {"id": "byName", "options": "(none)"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#9e9e9e"}}],
        }
    ],
)
table(
    "lo_lanes_t",
    "Lanes for loop $loop_id",
    "ah.loop_lanes(loop_id): one row per lane/session. Pick a loop in the loop_id variable or click a loop above.",
    [
        sql(
            "SELECT lane_name AS lane, role, agent, model, return_status, spawn_completion, first_event_at AS started, wall_s, llm_calls, output AS output_tokens, tool_calls, tool_errors, tool_denials, compactions, commits, priced_cost_usd AS cost_usd FROM ah.loop_lanes(nullif('$loop_id','')::bigint) ORDER BY first_event_at LIMIT 200"
        )
    ],
    cols=[col("cost_usd", unit="currencyUSD", gauge=True), col("wall_s", unit="s")],
)
table(
    "lo_repo",
    "Loops by repo",
    "Per repo over the range: loops, wall time, cost and outcome counts.",
    [
        sql(
            f"SELECT {RP_NONE} AS repo, count(*) AS loops, sum(lanes) AS lanes, sum(wall_s) AS wall_s, sum(commits) AS commits, sum(tool_errors) AS tool_errors, sum(api_errors) AS api_errors, sum(priced_cost_usd) AS cost_usd, sum(priced_cost_usd)/nullif(sum(commits),0) AS cost_per_commit {LS} GROUP BY 1 ORDER BY cost_usd DESC NULLS LAST"
        )
    ],
    cols=[
        col("cost_usd", unit="currencyUSD", gauge=True),
        col("cost_per_commit", unit="currencyUSD"),
        col("wall_s", unit="s"),
    ],
)

tab(
    "Loops",
    [
        (4, [("lo_n", 4), ("lo_cost", 4), ("lo_wall", 4), ("lo_lanes", 4), ("lo_commits", 4), ("lo_running", 4)]),
        (10, [("lo_recent", 24)]),
        (8, [("lo_cost_ts", 9), ("lo_wall_ts", 9), ("lo_return", 6)]),
        (10, [("lo_lanes_t", 24)]),
        (8, [("lo_repo", 24)]),
    ],
)

# ==========================================================================
# TAB: Tools & hooks
# ==========================================================================
TC = f"FROM ah.tool_call c {JS.format(a='c')} WHERE $__timeFilter(c.started_at) AND {NSAG('s')}"
stat("to_calls", "Tool calls", "Tool calls in range.", [sql(f"SELECT count(*) {TC}")])
stat("to_err", "Errors", "outcome=error.", [sql(f"SELECT count(*) {TC} AND c.outcome='error'")])
stat(
    "to_denied",
    "Denied",
    "outcome=denied (user rejection, auto-mode block or permission rule).",
    [sql(f"SELECT count(*) {TC} AND c.outcome='denied'")],
)
stat("to_timeout", "Timeouts", "outcome=timeout.", [sql(f"SELECT count(*) {TC} AND c.outcome='timeout'")])
stat("to_mcp", "MCP calls", "Calls to MCP servers.", [sql(f"SELECT count(*) {TC} AND c.mcp_server IS NOT NULL")])
stat(
    "to_out",
    "Tool output volume",
    "Sum of tool output bytes: what tools pushed back into context.",
    [sql(f"SELECT coalesce(sum(c.output_bytes),0) {TC}")],
    unit="bytes",
)
timeseries(
    "to_rates",
    "Tool call outcome rates",
    "Share of tool calls per bucket that errored, were denied, timed out or were interrupted.",
    [
        sql(
            f"SELECT {BUCKET.format(col='c.started_at')}, avg((c.outcome='error')::int) AS error, avg((c.outcome='denied')::int) AS denied, avg((c.outcome='timeout')::int) AS timeout, avg((c.outcome='interrupted')::int) AS interrupted {TC} GROUP BY 1 ORDER BY 1",
            ts=True,
        )
    ],
    unit="percentunit",
    decimals=2,
)
bars(
    "to_family",
    "Tool calls by family",
    "builtin, custom (Codex shell/apply_patch), collaboration (spawn/wait), function, mcp.",
    [
        sql(
            f"SELECT {BUCKET.format(col='c.started_at')}, coalesce(nullif(c.tool_family,''),'(none)') AS metric, count(*) AS value {TC} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bargauge(
    "to_top",
    "Top tools",
    "Top 20 tools by calls in range.",
    [sql(f"SELECT c.tool_name, count(*) AS calls {TC} GROUP BY 1 ORDER BY 2 DESC LIMIT 20")],
)
bargauge(
    "to_verbs",
    "Shell command verbs",
    "First command verb of Claude Bash calls and Codex command executions, top 25.",
    [
        sql(
            f"SELECT verb, sum(n) AS calls FROM (SELECT c.meta->>'cmd_verb' AS verb, count(*) AS n {TC} AND c.meta ? 'cmd_verb' GROUP BY 1 UNION ALL SELECT o.cmd_verb, count(*) FROM ah.tool_op o {JS.format(a='o')} WHERE $__timeFilter(o.started_at) AND o.cmd_verb IS NOT NULL AND o.cmd_verb <> '' AND {NSAG('s')} GROUP BY 1) x GROUP BY 1 ORDER BY 2 DESC LIMIT 25"
        )
    ],
)
bargauge(
    "to_mcp_top",
    "MCP servers",
    "Calls per MCP server, top 20.",
    [
        sql(
            f"SELECT c.mcp_server, count(*) AS calls {TC} AND c.mcp_server IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        )
    ],
)
timeseries(
    "to_p95",
    "Tool duration p95 by family",
    "p95 duration_ms per bucket and tool family.",
    [
        sql(
            f"SELECT {BUCKET.format(col='c.started_at')}, coalesce(nullif(c.tool_family,''),'(none)') AS metric, percentile_cont(0.95) WITHIN GROUP (ORDER BY c.duration_ms) AS value {TC} AND c.duration_ms IS NOT NULL GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="ms",
)
table(
    "to_rel",
    "Tool reliability",
    "ah.tool_reliability(since, namespaces): calls, error/denial/interrupt rates and latency. Top 60 by calls.",
    [
        sql(
            f"SELECT agent, tool_name AS tool, mcp_server, calls, error_rate, denial_rate, interrupt_rate, p50_ms, p95_ms FROM ah.tool_reliability({SINCE}, {NSARR}) WHERE {AG()} ORDER BY calls DESC LIMIT 60"
        )
    ],
    cols=[
        col("calls", gauge=True),
        col("error_rate", unit="percentunit", text_colour=RATE_RED),
        col("denial_rate", unit="percentunit", text_colour=RATE_RED),
        col("interrupt_rate", unit="percentunit"),
        col("p50_ms", unit="ms"),
        col("p95_ms", unit="ms"),
    ],
    sort="calls",
)
table(
    "to_burn",
    "Context burn by tool",
    "Output bytes each tool pushed back into context, with persisted (spilled to file) output.",
    [
        sql(
            f"SELECT c.tool_name AS tool, count(*) AS calls, sum(c.output_bytes) AS output_bytes, round(avg(c.output_bytes)) AS avg_bytes, max(c.output_bytes) AS max_bytes, sum(c.persisted_output_bytes) AS persisted_bytes {TC} GROUP BY 1 ORDER BY output_bytes DESC NULLS LAST LIMIT 30"
        )
    ],
    cols=[
        col("output_bytes", unit="bytes", gauge=True),
        col("avg_bytes", unit="bytes"),
        col("max_bytes", unit="bytes"),
        col("persisted_bytes", unit="bytes"),
    ],
    sort="output_bytes",
)
bars(
    "to_hooks_ts",
    "Hook runs by event",
    "Claude hook runs per bucket by hook event (Codex records none).",
    [
        sql(
            f"SELECT {BUCKET.format(col='h.ts')}, h.hook_event AS metric, count(*) AS value FROM ah.hook_event h {JS.format(a='h')} WHERE $__timeFilter(h.ts) AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "to_hook_out",
    "Hook outcomes (non-success)",
    "Hook runs that errored, were cancelled, blocked or injected context/messages.",
    [
        sql(
            f"SELECT {BUCKET.format(col='h.ts')}, h.hook_event || ' ' || h.outcome AS metric, count(*) AS value FROM ah.hook_event h {JS.format(a='h')} WHERE $__timeFilter(h.ts) AND h.outcome <> 'success' AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
table(
    "to_hooks",
    "Hook latency",
    "ah.hook_latency(since, namespaces). coverage = share of runs with a recorded duration.",
    [
        sql(
            f"SELECT hook_event, hook_name, runs, sessions, coverage, p50_ms, p95_ms, max_ms, total_s, error_rate, blocked_rate, timeout_rate FROM ah.hook_latency({SINCE}, {NSARR}) ORDER BY total_s DESC NULLS LAST LIMIT 50"
        )
    ],
    cols=[
        col("total_s", unit="s", gauge=True),
        col("coverage", unit="percentunit"),
        col("p50_ms", unit="ms"),
        col("p95_ms", unit="ms"),
        col("max_ms", unit="ms"),
        col("error_rate", unit="percentunit", text_colour=RATE_RED),
        col("blocked_rate", unit="percentunit"),
        col("timeout_rate", unit="percentunit", text_colour=RATE_RED),
    ],
    sort="total_s",
)
table(
    "to_denials",
    "Denials",
    "ah.denials(since, namespaces): auto-mode blocks and user rejections first, then permission-rule hits (rules working).",
    [
        sql(
            f"SELECT category, source, namespace, tool_name AS tool, cmd_verb, {TX('reason')} AS reason, events, sessions, last_ts FROM ah.denials({SINCE}, {NSARR}) ORDER BY (category LIKE 'permission-rule%'), events DESC LIMIT 50"
        )
    ],
    cols=[col("events", gauge=True), col("reason", width=360)],
)
table(
    "to_perm",
    "Permission rule candidates",
    "ah.permission_candidates: denied calls that were retried and then succeeded. A high retried_ok_rate is a candidate for an allow rule.",
    [
        sql(
            f"SELECT source, namespace, tool_name AS tool, mcp_server, cmd_verb, denial_kind, {TX('reason')} AS reason, denials, sessions, retried_ok, retried_ok_rate, last_ts FROM ah.permission_candidates({SINCE}, {NSARR}) ORDER BY denials DESC LIMIT 50"
        )
    ],
    cols=[col("retried_ok_rate", unit="percentunit", gauge=True, max=1), col("reason", width=320)],
)

# failures: tool_call and Codex tool_op rows with an error_class (parser v4)
FAIL = (
    f"(SELECT c.started_at AS ts, s.namespace, s.agent, s.session_uid, c.tool_name AS tool, c.error_class, c.error_excerpt "
    f"FROM ah.tool_call c {JS.format(a='c')} WHERE c.error_class IS NOT NULL AND $__timeFilter(c.started_at) AND {NSAG('s')} "
    f"UNION ALL SELECT coalesce(o.started_at, o.completed_at), s.namespace, s.agent, s.session_uid, "
    f"coalesce(nullif(o.cmd_verb, ''), o.item_type), o.error_class, o.error_excerpt "
    f"FROM ah.tool_op o {JS.format(a='o')} WHERE o.error_class IS NOT NULL "
    f"AND $__timeFilter(coalesce(o.started_at, o.completed_at)) AND {NSAG('s')}) f"
)
bars(
    "to_errclass",
    "Failures by error class",
    "Failed tool calls and Codex operations per bucket by error_class: denied, timeout, interrupted, validation_error, not_found, fake_cell_wait (Codex wait on an exec cell the session never yielded), permission, network, nonzero_exit, tool_error, other.",
    [
        sql(
            f"SELECT {BUCKET.format(col='f.ts')}, f.error_class AS metric, count(*) AS value FROM {FAIL} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "to_errtool",
    "Failures by tool and class",
    "Failed calls/ops per tool (Codex ops by command verb) and error_class, top 40.",
    [
        sql(
            f"SELECT f.tool, f.error_class, count(*) AS failures, count(DISTINCT f.session_uid) AS sessions, max(f.ts) AS last_ts FROM {FAIL} GROUP BY 1,2 ORDER BY 3 DESC LIMIT 40"
        )
    ],
    cols=[col("failures", gauge=True)],
    sort="failures",
)
table(
    "to_errlog",
    "Recent failures",
    "The latest 200 failures with error_excerpt (~500 chars from the first error-looking line, else the stderr tail). Excerpts are embedded, so semantic search finds similar failures.",
    [
        sql(
            f"SELECT f.ts, f.namespace, f.agent, f.tool, f.error_class, {TX('f.error_excerpt')} AS error_excerpt, f.session_uid FROM {FAIL} ORDER BY f.ts DESC LIMIT 200"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("error_excerpt", width=760)],
)
TIOQ = f"FROM ah.tool_io t WHERE $__timeFilter(t.ts) AND {NSAG('t')}"
bars(
    "to_io_bytes",
    "Tool I/O volume",
    "Bytes stored per bucket in ah.tool_io: input, model-visible output, stdout and stderr (UTF-8 bytes of the stored text).",
    [
        sql(
            f"SELECT {BUCKET.format(col='t.ts')}, sum(t.input_bytes) AS input, sum(t.output_bytes) AS output, sum(t.stdout_bytes) AS stdout, sum(t.stderr_bytes) AS stderr {TIOQ} GROUP BY 1 ORDER BY 1",
            ts=True,
        )
    ],
    unit="bytes",
)
table(
    "to_io_tools",
    "Tool I/O by tool",
    "ah.tool_io per tool: rows, bytes of each stream, share truncated by the source, and binary parts (images, documents) kept as metadata only.",
    [
        sql(
            f"SELECT t.tool_name AS tool, count(*) AS rows, sum(t.input_bytes) AS input, sum(t.output_bytes) AS output, sum(t.stdout_bytes) AS stdout, sum(t.stderr_bytes) AS stderr, avg((coalesce(t.output_truncated, false) OR coalesce(t.input_truncated, false))::int) AS truncated, coalesce(sum(jsonb_array_length(t.output_parts)) FILTER (WHERE jsonb_typeof(t.output_parts) = 'array'), 0) AS binary_parts {TIOQ} GROUP BY 1 ORDER BY output DESC NULLS LAST LIMIT 40"
        )
    ],
    cols=[col(c, unit="bytes") for c in ("input", "stdout", "stderr")]
    + [col("output", unit="bytes", gauge=True), col("truncated", unit="percentunit")],
    sort="output",
)

tab(
    "Tools & hooks",
    [
        (4, [("to_calls", 4), ("to_err", 4), ("to_denied", 4), ("to_timeout", 4), ("to_mcp", 4), ("to_out", 4)]),
        (8, [("to_rates", 12), ("to_family", 12)]),
        (10, [("to_top", 8), ("to_verbs", 8), ("to_mcp_top", 8)]),
        (8, [("to_p95", 24)]),
        (10, [("to_rel", 14), ("to_burn", 10)]),
        (9, [("to_errclass", 12), ("to_errtool", 12)]),
        (10, [("to_errlog", 24)]),
        (9, [("to_io_bytes", 10), ("to_io_tools", 14)]),
        (8, [("to_hooks_ts", 12), ("to_hook_out", 12)]),
        (9, [("to_hooks", 24)]),
        (9, [("to_denials", 12), ("to_perm", 12)]),
    ],
)

# ==========================================================================
# TAB: Limits & errors
# ==========================================================================
table(
    "li_fc",
    "Rate-limit forecast",
    "ah.v_rate_limit_forecast: latest used_percent per limit window, 6 h burn rate and projected exhaustion (only when it lands before the reset).",
    [
        sql(
            f"SELECT namespace, agent, plan_type, limit_id, window_kind, window_minutes, used_percent, burn_pct_per_hour, resets_at, projected_exhaustion_at, latest_ts, samples_6h, note FROM ah.v_rate_limit_forecast WHERE {NSAG()} ORDER BY used_percent DESC NULLS LAST LIMIT 50"
        )
    ],
    cols=[
        col("used_percent", unit="percent", gauge=True, max=100),
        col("burn_pct_per_hour", unit="percent"),
        col("projected_exhaustion_at", text_colour=steps("red")),
    ],
)
timeseries(
    "li_used",
    "Rate-limit used % over time",
    "Max used_percent per bucket per agent/plan/window (ah.rate_limit_sample). Credits samples carry no percentage.",
    [
        sql(
            f"SELECT $__timeGroupAlias(r.ts, $__interval), r.agent || coalesce(' ' || r.plan_type, '') || ' ' || r.window_kind || coalesce(' ' || (r.window_minutes/60)::text || 'h', '') AS metric, max(r.used_percent) AS value FROM ah.rate_limit_sample r {JS.format(a='r')} WHERE $__timeFilter(r.ts) AND r.used_percent IS NOT NULL AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    unit="percent",
    minv=0,
    maxv=100,
)
bars(
    "li_api",
    "API errors by kind",
    "llm_call rows flagged is_api_error per bucket (rate_limit, server_error, authentication_failed, ...).",
    [
        sql(
            f"SELECT {BUCKET.format(col='l.ts')}, l.agent || ' ' || coalesce(l.error_kind,'unknown') AS metric, count(*) AS value {LLM} AND l.is_api_error GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "li_reached",
    "Limit reached events",
    "rate_limit_sample rows with reached_type set (the limit actually rejected a request).",
    [
        sql(
            f"SELECT {BUCKET.format(col='r.ts')}, r.agent || ' ' || coalesce(r.limit_id,'') || ' ' || r.reached_type AS metric, count(*) AS value FROM ah.rate_limit_sample r {JS.format(a='r')} WHERE $__timeFilter(r.ts) AND r.reached_type IS NOT NULL AND {NSAG('s')} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
table(
    "li_api_t",
    "Recent API errors",
    "session_event kind=api_error: the value and HTTP status recorded by the CLI.",
    [
        sql(
            f"SELECT e.ts, s.namespace, s.agent, s.session_uid, {TX('e.value')} AS error, e.detail->>'status' AS status FROM ah.session_event e {JS.format(a='e')} WHERE $__timeFilter(e.ts) AND e.kind='api_error' AND {NSAG('s')} ORDER BY e.ts DESC LIMIT 100"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK)],
)
stat(
    "li_api_n",
    "API errors",
    "llm_call is_api_error in range.",
    [sql(f"SELECT count(*) {LLM} AND l.is_api_error")],
    steps_=steps("green", (1, "yellow"), (50, "red")),
)
stat(
    "li_rl_n",
    "Rate-limit errors",
    "API errors of kind rate_limit.",
    [sql(f"SELECT count(*) {LLM} AND l.error_kind='rate_limit'")],
    steps_=steps("green", (1, "yellow"), (20, "red")),
)
stat(
    "li_auth_n",
    "Auth errors",
    "authentication_failed / oauth_org_not_allowed.",
    [sql(f"SELECT count(*) {LLM} AND l.error_kind IN ('authentication_failed','oauth_org_not_allowed')")],
    steps_=steps("green", (1, "red")),
)
stat(
    "li_max",
    "Highest window now",
    "Highest latest used_percent across limit windows sampled in the last day.",
    [
        sql(
            f"SELECT max(used_percent) FROM ah.v_rate_limit_forecast WHERE latest_ts > now() - interval '1 day' AND {NSAG()}"
        )
    ],
    unit="percent",
    steps_=steps("green", (70, "yellow"), (90, "red")),
)

tab(
    "Limits & errors",
    [
        (4, [("li_max", 6), ("li_api_n", 6), ("li_rl_n", 6), ("li_auth_n", 6)]),
        (8, [("li_fc", 24)]),
        (9, [("li_used", 24)]),
        (8, [("li_api", 12), ("li_reached", 12)]),
        (9, [("li_api_t", 24)]),
    ],
)

# ==========================================================================
# TAB: Quality & git
# ==========================================================================
REPO = "('__all__' IN ($repo) OR repo_slug IN ($repo))"
stat(
    "qa_commits",
    "Agent commits",
    "ah.v_agent_commit_quality events in range.",
    [sql(f"SELECT count(*) FROM ah.v_agent_commit_quality WHERE $__timeFilter(ts) AND {NSAG()}")],
)
stat(
    "qa_resolved",
    "Resolved to ground truth",
    "Share of agent commits matched to a git_commit row in an owned repo.",
    [sql(f"SELECT avg(resolved::int) FROM ah.v_agent_commit_quality WHERE $__timeFilter(ts) AND {NSAG()}")],
    unit="percentunit",
    decimals=1,
)
stat(
    "qa_cifail",
    "CI failure rate",
    "CI failure / commits with CI, over resolved agent commits in range.",
    [
        sql(
            f"SELECT count(*) FILTER (WHERE ci_conclusion IN ('failure','mixed'))::numeric / nullif(count(*) FILTER (WHERE ci_conclusion IS NOT NULL AND ci_conclusion <> 'pending'),0) FROM ah.v_agent_commit_quality WHERE $__timeFilter(ts) AND {NSAG()}"
        )
    ],
    unit="percentunit",
    decimals=1,
    steps_=RATE_RED,
)
stat(
    "qa_revert",
    "Revert rate",
    "Reverted / agent commits.",
    [
        sql(
            f"SELECT avg(coalesce(reverted,false)::int) FROM ah.v_agent_commit_quality WHERE $__timeFilter(ts) AND {NSAG()}"
        )
    ],
    unit="percentunit",
    decimals=1,
    steps_=RATE_RED,
)
stat(
    "qa_lines",
    "Lines changed (agents)",
    "Sum of lines_changed over resolved agent commits.",
    [sql(f"SELECT coalesce(sum(lines_changed),0) FROM ah.v_agent_commit_quality WHERE $__timeFilter(ts) AND {NSAG()}")],
)
stat(
    "qa_prs",
    "PRs opened/merged",
    "git_event op=pr in range.",
    [
        sql(
            f"SELECT count(*) FROM ah.git_event g {JS.format(a='g')} WHERE $__timeFilter(g.ts) AND g.op='pr' AND {NSAG('s')}"
        )
    ],
)
timeseries(
    "qa_rate_ts",
    "Agent commit CI failure and revert rate (weekly)",
    "ah.v_agent_commit_quality_weekly summed over repos. Widen the range to 30d+ for a trend.",
    [
        sql(
            f'SELECT greatest(week, {SINCE}) AS time, sum(ci_failure)::numeric / nullif(sum(with_ci),0) AS "CI failure rate", sum(reverted)::numeric / nullif(sum(agent_commits),0) AS "revert rate" FROM ah.v_agent_commit_quality_weekly WHERE week >= date_trunc(\'week\', {SINCE}) AND week <= {UNTIL} AND {REPO} GROUP BY 1 ORDER BY 1',
            ts=True,
        )
    ],
    unit="percentunit",
    points=True,
)
bars(
    "qa_commits_ts",
    "Commits on owned repos: agent vs human",
    "ah.git_commit per bucket: commits authored by Rob vs other authors (bots, agents with their own identity).",
    [
        sql(
            f"SELECT {BUCKET.format(col='committed_at')}, CASE WHEN author_is_owner THEN 'rob identity' ELSE 'other author' END AS metric, count(*) AS value FROM ah.git_commit WHERE $__timeFilter(committed_at) AND {REPO} AND {CTX} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
table(
    "qa_repo",
    "Agent commits by repo",
    "ah.v_agent_commit_quality_weekly rolled up over the range. Blank repo = not yet resolved to a remote.",
    [
        sql(
            f"SELECT {RP_UNRESOLVED} AS repo, sum(agent_commits) AS commits, sum(with_ci) AS with_ci, sum(ci_success) AS ci_success, sum(ci_failure) AS ci_failure, sum(reverted) AS reverted, sum(lines_changed) AS lines, sum(ci_failure)::numeric / nullif(sum(with_ci),0) AS ci_failure_rate, sum(reverted)::numeric / nullif(sum(agent_commits),0) AS revert_rate FROM ah.v_agent_commit_quality_weekly WHERE week >= date_trunc('week', {SINCE}) AND week <= {UNTIL} AND {REPO} GROUP BY 1 ORDER BY commits DESC LIMIT 50"
        )
    ],
    cols=[
        col("commits", gauge=True),
        col("ci_failure_rate", unit="percentunit", text_colour=RATE_RED),
        col("revert_rate", unit="percentunit", text_colour=RATE_RED),
    ],
    sort="commits",
)
table(
    "qa_workflows",
    "CI failure rate by workflow",
    "ah.ci_run created in range, per repo and workflow (latest attempt per run).",
    [
        sql(
            f"SELECT {RP('repo_slug')} AS repo, workflow, count(*) AS runs, count(*) FILTER (WHERE conclusion='failure') AS failures, count(*) FILTER (WHERE conclusion='failure')::numeric / nullif(count(*) FILTER (WHERE conclusion IS NOT NULL),0) AS failure_rate, max(created_at) AS last FROM ah.ci_run WHERE $__timeFilter(created_at) AND {REPO} AND {CTX} GROUP BY 1,2 HAVING count(*) > 1 ORDER BY failures DESC, runs DESC LIMIT 40"
        )
    ],
    cols=[col("failure_rate", unit="percentunit", text_colour=RATE_RED), col("failures", gauge=True)],
    sort="failures",
)
table(
    "qa_bad",
    "Agent commits with CI failure or revert",
    "ah.v_agent_commit_quality rows in range whose CI concluded failure/mixed or that were reverted. `agent-history why <sha>` explains one.",
    [
        sql(
            f"SELECT ts, {RP('repo_slug')} AS repo, sha_short AS sha, {TX('branch')} AS branch, agent, namespace, {TX('subject')} AS subject, lines_changed, ci_conclusion, ci_failed_workflows, reverted, reverted_by, session_uid FROM ah.v_agent_commit_quality WHERE $__timeFilter(ts) AND {NSAG()} AND {REPO} AND (ci_conclusion IN ('failure','mixed') OR reverted) ORDER BY ts DESC LIMIT 50"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("subject", width=360), col("sha", unit="string")],
)
table(
    "qa_churn",
    "Repo churn",
    "ah.git_commit over the range: commits, files and lines per repo on the default branch.",
    [
        sql(
            f"SELECT {RP('repo_slug')} AS repo, count(*) AS commits, count(*) FILTER (WHERE author_is_owner) AS rob_identity, sum(files_changed) AS files, sum(insertions) AS insertions, sum(deletions) AS deletions, count(*) FILTER (WHERE reverts_sha IS NOT NULL) AS reverts, max(committed_at) AS last FROM ah.git_commit WHERE $__timeFilter(committed_at) AND on_default AND {REPO} AND {CTX} GROUP BY 1 ORDER BY commits DESC LIMIT 40"
        )
    ],
    cols=[col("commits", gauge=True)],
    sort="commits",
)
table(
    "qa_hot_paths",
    "Most-changed paths",
    "ah.git_commit_file joined to commits in range: paths changed most often (all authors).",
    [
        sql(
            f"SELECT {RP('f.repo_slug')} AS repo, (CASE WHEN {RP('f.repo_slug')} = 'private repo' THEN '(redacted)' ELSE f.path END) AS path, count(*) AS commits, sum(f.insertions) AS insertions, sum(f.deletions) AS deletions FROM ah.git_commit_file f JOIN ah.git_commit c USING (repo_slug, sha) WHERE $__timeFilter(c.committed_at) AND {CTX.replace('context', 'c.context')} AND ('__all__' IN ($repo) OR f.repo_slug IN ($repo)) GROUP BY 1,2 ORDER BY commits DESC LIMIT 40"
        )
    ],
    cols=[col("commits", gauge=True), col("path", width=420)],
    sort="commits",
)
pie(
    "qa_backlog",
    "Backlog tasks by status",
    "ah.backlog_task (default-branch tip of each owned repo's backlog/).",
    [sql(f"SELECT status, count(*) AS tasks FROM ah.backlog_task WHERE {REPO} AND {CTX} GROUP BY 1 ORDER BY 2 DESC")],
)
table(
    "qa_backlog_t",
    "Backlog by repo",
    "Open vs done tasks per repo, with tasks updated in range.",
    [
        sql(
            f"SELECT {RP('repo_slug')} AS repo, count(*) FILTER (WHERE status NOT ILIKE 'done%') AS open, count(*) FILTER (WHERE status ILIKE 'done%') AS done, count(*) FILTER (WHERE $__timeFilter(updated_at)) AS updated_in_range, count(*) FILTER (WHERE priority IN ('P0','high')) AS p0_high FROM ah.backlog_task WHERE {REPO} AND {CTX} GROUP BY 1 ORDER BY open DESC LIMIT 40"
        )
    ],
    cols=[col("open", gauge=True)],
    sort="open",
)
table(
    "qa_policy",
    "Agent policy changes",
    "ah.policy_changes: commits in range that touched AGENTS.md, CLAUDE.md, rules or skills files. Pair with the Signals tab to see whether corrections moved.",
    [
        sql(
            f"SELECT committed_at, {RP('repo_slug')} AS repo, left(sha,10) AS sha, (CASE WHEN {RP('repo_slug')} = 'private repo' THEN '(redacted)' ELSE path END) AS path, change, insertions, deletions, {TX('subject')} AS subject FROM ah.policy_changes(NULL, {SINCE}) pc WHERE committed_at <= {UNTIL} AND EXISTS (SELECT 1 FROM ah.git_commit gc WHERE gc.repo_slug = pc.repo_slug AND gc.sha = pc.sha AND {CTX.replace('context', 'gc.context')}) AND (path ~* '(AGENTS|CLAUDE)\\.md$' OR path ~* '(^|/)(rules|skills|reference)/') ORDER BY committed_at DESC LIMIT 60"
        )
    ],
    cols=[col("path", width=320), col("subject", width=320), col("sha", unit="string")],
)

tab(
    "Quality & git",
    [
        (
            4,
            [("qa_commits", 4), ("qa_resolved", 4), ("qa_cifail", 4), ("qa_revert", 4), ("qa_lines", 4), ("qa_prs", 4)],
        ),
        (8, [("qa_rate_ts", 12), ("qa_commits_ts", 12)]),
        (10, [("qa_repo", 12), ("qa_workflows", 12)]),
        (10, [("qa_bad", 24)]),
        (10, [("qa_churn", 12), ("qa_hot_paths", 12)]),
        (9, [("qa_backlog", 8), ("qa_backlog_t", 16)]),
        (10, [("qa_policy", 24)]),
    ],
)

# ==========================================================================
# TAB: Signals & features
# ==========================================================================
CS = f"FROM ah.v_correction_signals WHERE $__timeFilter(first_event_at) AND {NSAG()}"
stat(
    "sg_int",
    "Interrupts",
    "User interrupts (Esc / stop) in sessions started in range (ah.v_correction_signals).",
    [sql(f"SELECT coalesce(sum(interrupts),0) {CS}")],
)
stat(
    "sg_rej",
    "User rejections",
    "Tool calls the user rejected at a permission prompt.",
    [sql(f"SELECT coalesce(sum(user_rejections),0) {CS}")],
)
stat(
    "sg_q",
    "Questions asked",
    "AskUserQuestion prompts answered.",
    [sql(f"SELECT coalesce(sum(user_questions),0) {CS}")],
)
stat(
    "sg_abort",
    "Aborted turns",
    "Turns aborted mid-flight (interrupted or aborted mid-stream).",
    [sql(f"SELECT coalesce(sum(aborted_turns),0) {CS}")],
)
stat(
    "sg_queue",
    "Queued prompt ops",
    "Queue operations (typing ahead while the agent works).",
    [sql(f"SELECT coalesce(sum(queue_ops),0) {CS}")],
)
stat(
    "sg_per100",
    "Corrections per 100 prompts",
    "(interrupts + rejections + aborted turns) per 100 human prompts.",
    [
        sql(
            f"SELECT 100.0 * (SELECT sum(interrupts + user_rejections + aborted_turns) {CS}) / nullif((SELECT count(*) FROM ah.message WHERE $__timeFilter(ts) AND ah.is_genuine_prompt(message_class, prompt_origin, text) AND {NSAG()}),0)"
        )
    ],
    decimals=1,
)
bars(
    "sg_ts",
    "Correction signals",
    "ah.v_correction_signals by session start bucket. Queue ops are left out (they dwarf the rest).",
    [
        sql(
            f"SELECT {BUCKET.format(col='first_event_at')}, sum(interrupts) AS interrupts, sum(user_rejections) AS rejections, sum(user_questions) AS questions, sum(aborted_turns) AS aborted_turns {CS} GROUP BY 1 ORDER BY 1",
            ts=True,
        )
    ],
)
bars(
    "sg_skills",
    "Skills and slash commands",
    "skill_invoke and slash_command events per bucket, top 12 values in range.",
    [
        sql(
            f"WITH e AS (SELECT e.ts, e.kind || ' ' || e.value AS v FROM ah.session_event e {JS.format(a='e')} WHERE $__timeFilter(e.ts) AND e.kind IN ('skill_invoke','slash_command') AND {NSAG('s')}), top AS (SELECT v FROM e GROUP BY 1 ORDER BY count(*) DESC LIMIT 12) SELECT {BUCKET.format(col='ts')}, CASE WHEN v IN (SELECT v FROM top) THEN v ELSE '(other)' END AS metric, count(*) AS value FROM e GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "sg_sessions",
    "Sessions with most corrections",
    "Top 25 sessions by interrupts + rejections + aborted turns.",
    [
        sql(
            f"SELECT first_event_at AS started, agent, namespace, session_uid, interrupts, user_rejections AS rejections, user_questions AS questions, queue_ops AS queued, aborted_turns {CS} AND interrupts + user_rejections + aborted_turns > 0 ORDER BY interrupts + user_rejections + aborted_turns DESC LIMIT 25"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("interrupts", gauge=True)],
)
table(
    "sg_followups",
    "What followed a correction",
    "ah.correction_digest followup rows: each interrupt, rejection or tool error with the human prompt that came next (excerpt, 200 chars).",
    [
        sql(
            f"SELECT ts, namespace, agent, signal, {TX('excerpt')} AS excerpt, session_uid FROM ah.correction_digest({SINCE}, {NSARR}) WHERE row_kind='followup' AND ts <= {UNTIL} AND {AG()} ORDER BY ts DESC LIMIT 100"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("excerpt", width=700)],
)
table(
    "sg_features",
    "Feature usage",
    "ah.v_feature_usage summed over the range: skills, slash commands, MCP tools/servers, model switches, plan mode.",
    [
        sql(
            f"SELECT kind, value, sum(uses) AS uses, count(DISTINCT day) AS active_days FROM ah.v_feature_usage WHERE {DAYF} AND {NSAG()} GROUP BY 1,2 ORDER BY uses DESC LIMIT 60"
        )
    ],
    cols=[col("uses", gauge=True)],
    sort="uses",
)
table(
    "sg_unused",
    "Installed but unused features",
    "ah.unused_features(days of the range): installed skills, plugins and MCP servers with no use. 'new' = first seen inside the window; 'unknown' = the agent records no use for that kind.",
    [
        sql(
            f"SELECT status, kind, {RW('name')} AS name, machine, namespace, enabled, uses, last_used_at, first_seen_at FROM ah.unused_features({DAYS}) WHERE status <> 'used' AND {NS()} ORDER BY status, kind, name LIMIT 300"
        )
    ],
)
table(
    "sg_cli",
    "CLI version and model timeline",
    "ah.v_cli_model_timeline: which CLI version ran which model, first/last day seen and sessions.",
    [
        sql(
            f"SELECT agent, cli_version, model, min(day) AS first_day, max(day) AS last_day, sum(sessions) AS sessions FROM ah.v_cli_model_timeline WHERE {DAYF} AND {AG()} GROUP BY 1,2,3 ORDER BY last_day DESC, sessions DESC LIMIT 80"
        )
    ],
)
bars(
    "sg_cli_ts",
    "Sessions by CLI version",
    "v_cli_model_timeline sessions per day by agent + CLI version.",
    [
        sql(
            f"SELECT greatest(day, $__timeFrom()::timestamptz) AS time, agent || ' ' || coalesce(cli_version,'?') AS metric, sum(sessions) AS value FROM ah.v_cli_model_timeline WHERE {DAYF} AND {AG()} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
)
table(
    "sg_installed",
    "Installed features by machine",
    "ah.installed_feature: what is installed per machine/home and kind, from the hourly Mac collector.",
    [
        sql(
            f"SELECT machine, home, namespace, kind, count(*) AS installed, count(*) FILTER (WHERE enabled) AS enabled, max(seen_at) AS last_seen FROM ah.installed_feature WHERE {NS()} GROUP BY 1,2,3,4 ORDER BY 1,2,4"
        )
    ],
)

tab(
    "Signals & features",
    [
        (4, [("sg_int", 4), ("sg_rej", 4), ("sg_q", 4), ("sg_abort", 4), ("sg_queue", 4), ("sg_per100", 4)]),
        (9, [("sg_ts", 10), ("sg_skills", 14)]),
        (10, [("sg_sessions", 24)]),
        (10, [("sg_followups", 24)]),
        (10, [("sg_features", 12), ("sg_unused", 12)]),
        (9, [("sg_cli_ts", 12), ("sg_cli", 12)]),
        (8, [("sg_installed", 24)]),
    ],
)

# ==========================================================================
# TAB: Infra actions
# ==========================================================================
IA = f"FROM ah.v_infra_action WHERE $__timeFilter(ts) AND {NSAG()}"
stat(
    "ia_n", "Remote actions", "ssh / scp / rsync / tailscale ssh calls made by agents.", [sql(f"SELECT count(*) {IA}")]
)
stat("ia_hosts", "Hosts touched", "Distinct normalised target hosts.", [sql(f"SELECT count(DISTINCT host) {IA}")])
stat(
    "ia_fail",
    "Not OK",
    "Remote actions whose tool outcome was not ok.",
    [sql(f"SELECT count(*) {IA} AND outcome <> 'ok'")],
    steps_=steps("green", (1, "yellow")),
)
stat("ia_sessions", "Sessions", "Sessions that ran a remote action.", [sql(f"SELECT count(DISTINCT session_uid) {IA}")])
bars(
    "ia_ts",
    "Remote actions by host",
    "Per bucket and target host (normalised).",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, {RH('host')} AS metric, count(*) AS value {IA} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bargauge(
    "ia_verbs",
    "Remote command verbs",
    "The first command run on the remote host, top 20 ((login) = interactive).",
    [sql(f"SELECT {RW_VERB} AS verb, count(*) AS actions {IA} GROUP BY 1 ORDER BY 2 DESC LIMIT 20")],
)
table(
    "ia_host",
    "Actions per host",
    "Remote actions, failures, sessions and the command verbs seen per host.",
    [
        sql(
            f"SELECT {RH('host')} AS host, count(*) AS actions, count(*) FILTER (WHERE outcome <> 'ok') AS not_ok, count(DISTINCT session_uid) AS sessions, string_agg(DISTINCT {RW_VERB}, ', ') AS verbs, max(ts) AS last {IA} GROUP BY 1 ORDER BY actions DESC"
        )
    ],
    cols=[col("actions", gauge=True), col("verbs", width=420)],
    sort="actions",
)
table(
    "ia_log",
    "Agent actions on hosts",
    "ah.v_infra_action rows in range. Enable the 'Agent infra actions' annotation to overlay them on every time panel.",
    [
        sql(
            f"SELECT ts, {RH('host')} AS host, {RW('remote_verb')} AS remote_verb, namespace, agent, outcome, duration_ms, session_uid {IA} ORDER BY ts DESC LIMIT 500"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("duration_ms", unit="ms")],
)

tab(
    "Infra actions",
    [
        (4, [("ia_n", 6), ("ia_hosts", 6), ("ia_fail", 6), ("ia_sessions", 6)]),
        (9, [("ia_ts", 14), ("ia_verbs", 10)]),
        (9, [("ia_host", 24)]),
        (12, [("ia_log", 24)]),
    ],
)

# ==========================================================================
# TAB: Jevgrep (jg, the Jev-ranked code search in rob/agents; derived from tool I/O, nothing extra collected)
# ==========================================================================
# A jg run is a Claude Bash call or a Codex CommandExecution whose command invokes `jg` as a command
# word (start of the command, or after whitespace, ; & | or (). The command is unwrapped from the
# stored tool input first (Claude {"command": ...}, Codex [shell, -lc, script]) so a quoted mention
# such as grep "jg " does not count. A mention inside a quoted argument can still match, so the
# headline panels count only runs whose output carries a jg or wrapper message; the rest are shown
# as "no jg output" in the result chart and the log.
# The result comes from jg's own stdout: "Jevgrep: N relevant files[; discovery incomplete]." for a
# search, "Jev connection verified" for doctor, and the rob/agents wrapper's "jg: refusing to search".
JG_WORD = r"(^|[\s;&|(])jg\s"
JG_ADMIN = r"(^|[\s;&|(])jg\s+(doctor|auth|skill|--version|--help|-h)"
# The session that built and tested jg (2026-09-27): its test runs and greps are not usage.
JG_BUILD = "a6080704-44be-4fdd-ad3e-5f890d0be3ee"
JG = (
    "(SELECT io.ts, io.namespace, io.agent, s.session_uid, ah.project_of(s.cwd) AS project, x.cmd,"
    " coalesce(c.duration_ms, o.duration_ms) AS duration_ms, coalesce(o.exit_code, c.exit_code) AS exit_code,"
    # One tool call is one run: a call whose output has a search result is a search even if it also ran jg doctor.
    f" CASE WHEN r.out !~ 'Jevgrep: [0-9]+ relevant files' AND x.cmd ~ '{JG_ADMIN}' THEN 'admin' ELSE 'search' END AS kind,"
    " CASE WHEN r.out LIKE '%jg: refusing to search%' THEN 'refused'"
    "  WHEN r.out ~ 'Jevgrep: [0-9]+ relevant files; discovery incomplete' THEN 'incomplete'"
    "  WHEN r.out ~ 'Jevgrep: [0-9]+ relevant files[.]' THEN 'complete'"
    "  WHEN r.out LIKE '%Jev connection verified%' THEN 'doctor ok'"
    "  WHEN r.out ~ '(Command failed[.] Check the root|Run jg auth|Jev connection check failed|jg: AGENT_CONTEXT|jg is not installed|Interrupted[.])'"
    "  THEN 'failed' ELSE 'no jg output' END AS result,"
    " substring(r.out from 'Jevgrep: ([0-9]+) relevant files')::int AS files"
    " FROM ah.tool_io io JOIN ah.session s ON s.id = io.session_id"
    " CROSS JOIN LATERAL (SELECT coalesce(io.stdout_text,'') || coalesce(io.output_text,'') AS out) r"
    " CROSS JOIN LATERAL (SELECT CASE WHEN NOT pg_input_is_valid(io.input_text, 'jsonb') THEN io.input_text"
    "  WHEN jsonb_typeof(io.input_text::jsonb) = 'array' THEN io.input_text::jsonb->>-1"
    "  ELSE io.input_text::jsonb->>'command' END AS cmd) x"
    " LEFT JOIN ah.tool_call c ON io.kind = 'call' AND c.agent = io.agent AND c.call_uid = io.io_uid"
    " LEFT JOIN ah.tool_op o ON io.kind = 'op' AND o.agent = io.agent AND 'item:' || o.item_uid = io.io_uid"
    " WHERE $__timeFilter(io.ts) AND io.tool_name IN ('Bash','CommandExecution')"
    f" AND io.input_text LIKE '%jg%' AND x.cmd ~ '{JG_WORD}' AND s.session_uid <> '{JG_BUILD}' AND {NSAG('io')}) j"
)
JGS = f"FROM {JG} WHERE kind = 'search' AND result <> 'no jg output'"
# Skill loads: Claude records skill_invoke; Codex reads the installed plugin's SKILL.md through a shell.
JG_SKILL = (
    f"(SELECT e.ts, s.namespace, s.agent, s.session_uid FROM ah.session_event e {JS.format(a='e')}"
    f" WHERE $__timeFilter(e.ts) AND e.kind = 'skill_invoke' AND e.value ILIKE 'jevgrep%' AND {NSAG('s')}"
    " UNION ALL SELECT io.ts, io.namespace, io.agent, s.session_uid FROM ah.tool_io io JOIN ah.session s ON s.id = io.session_id"
    " WHERE $__timeFilter(io.ts) AND io.agent = 'codex' AND io.tool_name = 'CommandExecution'"
    f" AND io.input_text LIKE '%plugins/cache/%jevgrep/%SKILL.md%' AND {NSAG('io')}) k"
)
text(
    "jg_help",
    "What this tab counts",
    (
        "**jg** (jevgrep) ranks the files that matter for a question using the Jev model through the "
        "Cloudflare AI Gateway; the `jevgrep` skill tells agents when to use it. Nothing extra is "
        "collected: a run is a Claude `Bash` call or Codex command whose command invokes `jg`, and the "
        "result is read from jg's own output. **complete** = ranked files returned, **incomplete** = Jev "
        "calls failed or were rate limited (Workers AI throttles Jev with 429s), **refused** = the wrapper "
        "blocked a backup repo, agent home or credential folder, **no jg output** = `jg` appeared in a "
        "command that never printed a jg message (a quoted mention, not a run) and is left out of the headline numbers. Admin runs (`jg doctor`, `jg --version`) are counted apart "
        "from searches. Panels stay empty until agents start using it."
    ),
)
stat("jg_n", "Searches", "jg search runs in range.", [sql(f"SELECT count(*) {JGS}")])
stat(
    "jg_sessions",
    "Sessions using jg",
    "Distinct sessions that ran a jg search.",
    [sql(f"SELECT count(DISTINCT session_uid) {JGS}")],
)
stat(
    "jg_skill",
    "Skill loads",
    "Claude skill_invoke of jevgrep plus Codex reads of the installed jevgrep SKILL.md.",
    [sql(f"SELECT count(*) FROM {JG_SKILL}")],
)
stat(
    "jg_ok",
    "Complete",
    "Share of searches that returned a complete ranked file list.",
    [sql(f"SELECT avg((result = 'complete')::int) {JGS}")],
    unit="percentunit",
    decimals=0,
    steps_=steps("red", (0.8, "yellow"), (0.95, "green")),
)
stat(
    "jg_inc",
    "Incomplete",
    "Searches that reported 'discovery incomplete' (Jev failures or Workers AI 429s).",
    [sql(f"SELECT count(*) {JGS} AND result = 'incomplete'")],
    steps_=steps("green", (1, "yellow"), (5, "red")),
)
stat(
    "jg_p50",
    "Median search time",
    "Median tool-call duration of complete searches.",
    [sql(f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) {JGS} AND result = 'complete'")],
    unit="ms",
    steps_=steps("green", (20000, "yellow"), (60000, "red")),
)
bars(
    "jg_ts",
    "jg runs by result",
    "Search and admin runs per bucket by result.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, kind || ' ' || result AS metric, count(*) AS value FROM {JG} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
bars(
    "jg_ns",
    "Searches and skill loads by namespace",
    "jg searches and jevgrep skill loads per bucket and namespace.",
    [
        sql(
            f"SELECT {BUCKET.format(col='ts')}, namespace || ' search' AS metric, count(*) AS value {JGS} GROUP BY 1,2 "
            f"UNION ALL SELECT {BUCKET.format(col='ts')}, namespace || ' skill' AS metric, count(*) AS value FROM {JG_SKILL} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "jg_proj",
    "Searches by project",
    "Per project (root session cwd): searches, sessions, outcome mix, files returned and timing.",
    [
        sql(
            f"SELECT {RP('project')} AS project, count(*) AS searches, count(DISTINCT session_uid) AS sessions, count(*) FILTER (WHERE result = 'complete') AS complete, count(*) FILTER (WHERE result = 'incomplete') AS incomplete, count(*) FILTER (WHERE result NOT IN ('complete','incomplete')) AS other, round(avg(files) FILTER (WHERE result = 'complete'), 1) AS avg_files, percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE result = 'complete') AS p50_ms, max(ts) AS last {JGS} GROUP BY 1 ORDER BY searches DESC LIMIT 40"
        )
    ],
    cols=[col("searches", gauge=True), col("p50_ms", unit="ms")],
    sort="searches",
)
table(
    "jg_log",
    "Recent jg runs",
    "Every jg run in range, newest first. The command is the whole tool input (redacted in presentation mode).",
    [
        sql(
            f"SELECT ts, namespace, agent, kind, result, files, duration_ms, exit_code, {RP('project')} AS project, {TX('left(cmd, 400)')} AS command, session_uid FROM {JG} ORDER BY ts DESC LIMIT 200"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("duration_ms", unit="ms"), col("command", width=520)],
)

tab(
    "Jevgrep",
    [
        (4, [("jg_help", 24)]),
        (4, [("jg_n", 4), ("jg_sessions", 4), ("jg_skill", 4), ("jg_ok", 4), ("jg_inc", 4), ("jg_p50", 4)]),
        (9, [("jg_ts", 12), ("jg_ns", 12)]),
        (9, [("jg_proj", 24)]),
        (12, [("jg_log", 24)]),
    ],
)

# ==========================================================================
# TAB: Search (ParadeDB BM25 over the catalogue; the SQLite index had only FTS counters)
# ==========================================================================
QOK = "${q:sqlstring} <> ''"
# $mode is a key:value custom variable: text all/any/phrase for ah.search, value the ParadeDB
# operator (&&& match all, ||| match any, ### phrase) for the direct index queries.
HIT = "m.text ${mode} ${q:sqlstring}"
# $scope: conversation classes only (the default, what the CLI and MCP search) or every class, including
# reasoning and harness-injected text (system prompts, reminders, hook output, skill bodies).
SCOPE = "('${scope}' = 'all' OR m.message_class = ANY (ah.conversation_classes()))"
SCOPE_ARR = "(CASE WHEN '${scope}' = 'all' THEN NULL ELSE ah.conversation_classes() END)"
MHIT = f"FROM ah.message m WHERE {QOK} AND {HIT} AND {SCOPE} AND $__timeFilter(m.ts) AND {NSAG('m')}"
TIO = f"FROM ah.search_tool_io(${{q:sqlstring}}, {NSARR}, {SINCE}, 200, '${{mode:text}}') WHERE {QOK} AND ts <= {UNTIL} AND {AG()}"
text(
    "se_help",
    "How to search",
    (
        "Type in **Search** above; **Match** picks all words, any word or the exact phrase; **Scope** searches the "
        "conversation (prompts, replies, briefs, reports, summaries) or everything, including reasoning and "
        "harness-injected text. **Tool I/O hits** search tool inputs, outputs, stdout and stderr. Panels marked "
        "**· ParadeDB** run on the ParadeDB BM25 indexes (`message_search_idx` over message text with the "
        "`source_code` tokenizer, `session_summary_search_idx` over journal summaries with English stemming): "
        "the `&&&` / `|||` / `###` match operators, `pdb.score` ranking, and `pdb.agg` facets computed from the "
        "index's columnar fast fields rather than by scanning rows. **Watch terms** trends several phrases at once. "
        "Semantic search needs a query embedding, so hybrid (BM25 + pgvector HNSW) stays in "
        "`agent-history search --hybrid` and the MCP. Click a session to open it in **Session drill-down**."
    ),
)
stat(
    "se_n",
    "Matching messages" + PDB,
    PDBD + "count of messages matching the query in range, answered from the BM25 index.",
    [sql(f"SELECT count(*) {MHIT}")],
)
stat(
    "se_sess_n",
    "Sessions mentioning it" + PDB,
    PDBD + "distinct sessions with at least one matching message.",
    [sql(f"SELECT count(DISTINCT m.session_id) {MHIT}")],
)
stat(
    "se_first",
    "First mention" + PDB,
    PDBD + "earliest matching message in range.",
    [sql(f"SELECT extract(epoch FROM min(m.ts)) * 1000 AS first {MHIT}")],
    unit="dateTimeAsLocalNoDateIfToday",
)
stat(
    "se_last",
    "Latest mention" + PDB,
    PDBD + "latest matching message in range.",
    [sql(f"SELECT extract(epoch FROM max(m.ts)) * 1000 AS latest {MHIT}")],
    unit="dateTimeAsLocalNoDateIfToday",
)
bars(
    "se_trend",
    "Mentions over time" + PDB,
    PDBD + "matching messages per bucket and namespace (BM25 match operator, ts is an index fast field).",
    [
        sql(
            f"SELECT {BUCKET.format(col='m.ts')}, m.namespace AS metric, count(*) AS value {MHIT} GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
pie(
    "se_facet_ns",
    "Hits by namespace" + PDB,
    PDBD + "a pdb.agg terms facet over the namespace fast field: one index pass, no row scan.",
    [
        sql(
            f"SELECT b->>'key' AS namespace, (b->>'doc_count')::bigint AS hits FROM (SELECT pdb.agg('{{\"terms\": {{\"field\": \"namespace\", \"size\": 20}}}}') AS a {MHIT}) x, jsonb_array_elements(x.a->'buckets') b ORDER BY 2 DESC"
        )
    ],
)
bargauge(
    "se_facet_class",
    "Hits by message class" + PDB,
    PDBD + "a pdb.agg terms facet over message_class: prompts, replies, briefs, reports, summaries.",
    [
        sql(
            f"SELECT b->>'key' AS message_class, (b->>'doc_count')::bigint AS hits FROM (SELECT pdb.agg('{{\"terms\": {{\"field\": \"message_class\", \"size\": 20}}}}') AS a {MHIT}) x, jsonb_array_elements(x.a->'buckets') b ORDER BY 2 DESC"
        )
    ],
)
bargauge(
    "se_projects",
    "Hits by project" + PDB,
    PDBD + "matching messages grouped by session in the index, then by the session's project.",
    [
        sql(
            f"SELECT {RP('ah.project_of(s.cwd)')} AS project, sum(h.n) AS hits FROM (SELECT m.session_id, count(*) AS n {MHIT} GROUP BY 1) h JOIN ah.session s ON s.id = h.session_id GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
        )
    ],
)
table(
    "se_msgs",
    "Message hits" + PDB,
    PDBD + "ah.search(q, namespaces, since, 100, mode): BM25-ranked messages with an excerpt around the first hit.",
    [
        sql(
            f"SELECT ts, namespace, agent, role, message_class, round(score::numeric,2) AS score, {TX_SNIPPET} AS snippet, {RP('ah.project_of(cwd)')} AS project, {TX('title')} AS title, session_uid FROM ah.search(${{q:sqlstring}}, {NSARR}, {SINCE}, 100, '${{mode:text}}', {SCOPE_ARR}) WHERE {QOK} AND ts <= {UNTIL} AND {AG()} ORDER BY score DESC"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("snippet", width=640), col("score", gauge=True)],
)
table(
    "se_sessions",
    "Best sessions" + PDB,
    PDBD
    + "ah.find_sessions(q): BM25 over messages grouped by session (top 300 hits) plus journal title/objective hits.",
    [
        sql(
            f"SELECT last_event_at, namespace, agent, round(score::numeric,2) AS score, message_hits, summary_hit, is_subagent, {RP('ah.project_of(cwd)')} AS project, {TX('title')} AS title, session_uid, root_session_uid FROM ah.find_sessions(${{q:sqlstring}}, {NSARR}, 100) WHERE {QOK} AND {AG()} AND last_event_at BETWEEN {SINCE} AND {UNTIL} ORDER BY score DESC LIMIT 30"
        )
    ],
    cols=[
        col("session_uid", links=SESSION_LINK),
        col("root_session_uid", links=SESSION_LINK),
        col("score", gauge=True),
    ],
)
table(
    "se_summaries",
    "Journal summary hits" + PDB,
    PDBD + "ah.search_summaries(q): BM25 over agentic-journal titles, objectives and narratives with English stemming.",
    [
        sql(
            f"SELECT analysed_at, namespace, classification, {RP('project')} AS project, {TX('title')} AS title, {TX('objective')} AS objective, {TX_SNIPPET} AS snippet, round(score::numeric,2) AS score, session_uid FROM ah.search_summaries(${{q:sqlstring}}, {NSARR}, {SINCE}, 50) WHERE {QOK} AND {AG()} ORDER BY score DESC"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("snippet", width=480), col("objective", width=320)],
)
stat(
    "se_tio_n",
    "Tool I/O hits" + PDB,
    PDBD
    + "tool calls and Codex operations whose input, output, stdout, stderr or structured result match (ah.search_tool_io over tool_io_search_idx, top 200).",
    [sql(f"SELECT count(*) {TIO}")],
)
table(
    "se_tio",
    "Tool I/O hits" + PDB,
    PDBD
    + "ah.search_tool_io(q): BM25 over tool_io (input, output, stdout, stderr, result) with an excerpt around the first hit. The session link opens the calling session.",
    [
        sql(
            f"SELECT t.ts, t.namespace, t.agent, t.tool_name, round(t.score::numeric,2) AS score, c.error_class, {TX_TSNIPPET} AS snippet, s.session_uid FROM ah.search_tool_io(${{q:sqlstring}}, {NSARR}, {SINCE}, 100, '${{mode:text}}') t JOIN ah.session s ON s.id = t.session_id LEFT JOIN ah.tool_call c ON c.agent = t.agent AND c.call_uid = t.call_uid WHERE {QOK} AND t.ts <= {UNTIL} AND {AG('t.agent')} ORDER BY t.score DESC"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("snippet", width=640), col("score", gauge=True)],
)
bars(
    "se_watch",
    "Watch terms" + PDB,
    PDBD
    + "phrase matches (###) per bucket for each term in the Watch terms variable. Edit the variable to track your own phrases.",
    [
        sql(
            f"SELECT {BUCKET.format(col='m.ts')}, t.term AS metric, count(*) AS value FROM unnest(ARRAY[$watch]::text[]) t(term) CROSS JOIN LATERAL (SELECT m.ts FROM ah.message m WHERE m.text ### t.term AND {SCOPE} AND $__timeFilter(m.ts) AND {NSAG('m')}) m GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
table(
    "se_threads",
    "Open threads" + PDB,
    PDBD
    + "ah.open_threads(since): journal 'unfinished' items and similar loose ends, closed off by later BM25 matches.",
    [
        sql(
            f"SELECT ts, kind, namespace, agent, {TX('title')} AS title, {TX('ref')} AS ref, {TX('detail')} AS detail, session_uid FROM ah.open_threads({SINCE}, {NSARR}) WHERE ts <= {UNTIL} AND {AG()} ORDER BY ts DESC LIMIT 200"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("detail", width=480)],
)
table(
    "se_lessons",
    "Lesson candidates" + PDB,
    PDBD
    + "ah.lesson_candidates(since): BM25 over correction-shaped phrasing, scored, with the session's correction count.",
    [
        sql(
            f"SELECT ts, namespace, agent, message_class, round(score::numeric,2) AS score, session_corrections, {TX_SNIPPET} AS snippet, session_uid FROM ah.lesson_candidates({SINCE}, {NSARR}, 60) WHERE ts <= {UNTIL} AND {AG()} ORDER BY score DESC"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("snippet", width=640)],
)
table(
    "se_week",
    "Journal: what happened each day",
    "ah.week(since): one row per day and project with journal titles and objectives (only analysed conversations have them).",
    [
        sql(
            f"SELECT day, {RP('project')} AS project, sessions, subagent_sessions, coverage, commits, loops, priced_cost_usd AS cost_usd, {TX('titles')} AS titles, {TX('objectives')} AS objectives FROM ah.week({SINCE}, {NSARR}) WHERE day <= {UNTIL}::date AND project <> '(all)' ORDER BY day DESC, sessions DESC LIMIT 200"
        )
    ],
    cols=[
        col("coverage", unit="percentunit"),
        col("cost_usd", unit="currencyUSD"),
        col("titles", width=480),
        col("objectives", width=480),
    ],
)
pie(
    "se_class",
    "Journal classification",
    "session_summary.classification for conversations analysed in range.",
    [
        sql(
            f"SELECT classification, count(*) AS sessions FROM ah.session_summary WHERE $__timeFilter(analysed_at) AND {NS()} GROUP BY 1 ORDER BY 2 DESC"
        )
    ],
)
bargauge(
    "se_topics",
    "Top journal topics",
    "ah.session_topic for conversations analysed in range, top 20.",
    [
        sql(
            f"SELECT {TX('t.topic')} AS topic, count(*) AS sessions FROM ah.session_topic t JOIN ah.session_summary m ON m.journal_conversation_id = t.journal_conversation_id WHERE $__timeFilter(m.analysed_at) AND {NS('m.namespace')} GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        )
    ],
)

tab(
    "Search",
    [
        (5, [("se_help", 24)]),
        (4, [("se_n", 5), ("se_sess_n", 5), ("se_tio_n", 4), ("se_first", 5), ("se_last", 5)]),
        (8, [("se_trend", 14), ("se_facet_ns", 10)]),
        (8, [("se_facet_class", 12), ("se_projects", 12)]),
        (12, [("se_msgs", 24)]),
        (12, [("se_tio", 24)]),
        (9, [("se_sessions", 12), ("se_summaries", 12)]),
        (8, [("se_watch", 24)]),
        (9, [("se_threads", 24)]),
        (9, [("se_lessons", 24)]),
        (8, [("se_class", 8), ("se_topics", 16)]),
        (12, [("se_week", 24)]),
    ],
)

# ==========================================================================
# TAB: Session drill-down
# ==========================================================================
SESS = "${session:sqlstring}"
TREE = f"(SELECT id FROM ah.session WHERE session_uid = {SESS} OR root_session_uid = {SESS})"
table(
    "sd_pick",
    "Recent root sessions",
    "Top-level sessions with activity in range. Click a session_uid to load it into this tab (or paste one into the Session box).",
    [
        sql(
            f"SELECT v.last_event_at, v.namespace, v.agent, v.session_uid, {RP('ah.project_of(v.cwd)')} AS project, {TX_TITLE} AS title, v.turns_human, v.llm_calls, v.subagents, v.commits, v.duration_s FROM ah.v_session_summary v WHERE $__timeFilter(v.last_event_at) AND NOT v.is_subagent AND {NSAG('v')} ORDER BY v.last_event_at DESC LIMIT 100"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("duration_s", unit="s"), col("title", width=360)],
)
table(
    "sd_sum",
    "Session and its subagents",
    "Selected session and descendants with agent type provenance. A missing type is unknown, not a display label.",
    [
        sql(
            f"SELECT v.is_subagent, v.agent_type, s.agent_type_source, v.agent_id, v.first_event_at, v.last_event_at, v.duration_s, v.turns_human, v.llm_calls, v.output, v.peak_context_tokens, v.tool_calls, v.tool_errors, v.subagents, v.compactions, v.commits, v.pushes, v.claude_cost_usd, ah.session_cost(v.session_id) AS priced_cost_usd, array_to_string(v.models, ', ') AS models FROM ah.v_session_summary v JOIN ah.session s ON s.id = v.session_id WHERE v.session_id IN {TREE} ORDER BY v.is_subagent, v.first_event_at LIMIT 300"
        )
    ],
    cols=[
        col("duration_s", unit="s"),
        col("claude_cost_usd", unit="currencyUSD"),
        col("priced_cost_usd", unit="currencyUSD", gauge=True),
    ],
)
stat(
    "sd_cost",
    "Tree priced cost",
    "Priced cost of the session and all its descendants.",
    [sql(f"SELECT coalesce(sum(ah.session_cost(id)),0) FROM ah.session WHERE id IN {TREE}")],
    unit="currencyUSD",
    decimals=2,
)
stat(
    "sd_calls",
    "Tree LLM calls",
    "LLM calls across the selected session and its descendants.",
    [sql(f"SELECT count(*) FROM ah.llm_call WHERE session_id IN {TREE}")],
)
stat(
    "sd_tools",
    "Tree tool calls",
    "Tool calls across the selected session and its descendants.",
    [sql(f"SELECT count(*) FROM ah.tool_call WHERE session_id IN {TREE}")],
)
stat(
    "sd_subs",
    "Sessions in tree",
    "The selected session plus every session rooted in it.",
    [sql(f"SELECT count(*) FROM ah.session WHERE id IN {TREE}")],
)
table(
    "sd_journal",
    "Journal summary",
    "agentic-journal's summary of the session, when it has been analysed.",
    [
        sql(
            f"SELECT m.analysed_at, m.classification, {RP('m.project')} AS project, {TX('m.title')} AS title, {TX('m.objective')} AS objective, {TX('m.narrative')} AS narrative, {TX('m.outcomes::text')} AS outcomes, {TX('m.unfinished::text')} AS unfinished FROM ah.session_summary m JOIN ah.session s ON s.id = m.session_id WHERE s.session_uid = {SESS} ORDER BY m.analysed_at DESC LIMIT 5"
        )
    ],
    cols=[col("narrative", width=640), col("objective", width=320)],
)
timeseries(
    "sd_tokens",
    "Tokens over the session",
    "llm_call tokens per interval across the whole tree.",
    [
        sql(
            f'SELECT $__timeGroupAlias(ts, $__interval), sum(output) AS output, sum(input_uncached) AS "input (uncached)", sum(coalesce(cache_write_5m,0) + coalesce(cache_write_1h,0)) AS "cache write" FROM ah.llm_call WHERE session_id IN {TREE} AND $__timeFilter(ts) GROUP BY 1 ORDER BY 1',
            ts=True,
        )
    ],
    stack=True,
    bars=True,
)
timeseries(
    "sd_ctx",
    "Context tokens (root)",
    "context_tokens per call in the root session: the saw-tooth is compaction.",
    [
        sql(
            f"SELECT l.ts AS time, l.context_tokens AS context FROM ah.llm_call l JOIN ah.session s ON s.id = l.session_id WHERE s.session_uid = {SESS} AND NOT s.is_subagent AND $__timeFilter(l.ts) ORDER BY 1 LIMIT 5000",
            ts=True,
        )
    ],
)
bargauge(
    "sd_tool_top",
    "Tools used",
    "Tool calls across the tree, top 20.",
    [
        sql(
            f"SELECT tool_name, count(*) AS calls FROM ah.tool_call WHERE session_id IN {TREE} GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        )
    ],
)
table(
    "sd_spawns",
    "Spawns",
    "ah.v_spawn_outcome for spawns made anywhere in the tree.",
    [
        sql(
            f"SELECT o.spawned_at, o.child_agent_type AS agent_type, o.resolved_model AS model, o.reasoning_effort AS effort, {TX('o.name')} AS name, {TX('o.description')} AS description, o.completion_status, o.lane_return_status, o.duration_s, o.priced_cost_usd AS cost_usd, o.child_commits, o.child_ci_conclusion, o.redo FROM ah.v_spawn_outcome o WHERE o.parent_session_id IN {TREE} ORDER BY o.spawned_at LIMIT 300"
        )
    ],
    cols=[col("duration_s", unit="s"), col("cost_usd", unit="currencyUSD", gauge=True)],
)
table(
    "sd_timeline",
    "Timeline",
    "ah.session_timeline(uid): turns, message excerpts, tool calls, spawns, compactions and git events in order (bounded to 400 rows, no tool I/O).",
    [sql(f"SELECT ts, kind, {TX('detail')} AS detail FROM ah.session_timeline({SESS}, '', 400, 200) ORDER BY ts")],
    cols=[col("detail", width=1100), col("kind", width=140)],
)
table(
    "sd_git",
    "Git events",
    "Commits, pushes and PRs made in the tree.",
    [
        sql(
            f"SELECT g.ts, g.op, {TX('g.branch')} AS branch, g.sha_short, g.pr_number, g.pr_action, {TX('g.pr_url')} AS pr_url, {RP('ah.project_of(g.cwd)')} AS project FROM ah.git_event g WHERE g.session_id IN {TREE} AND g.op <> 'session_start' ORDER BY g.ts LIMIT 200"
        )
    ],
    cols=[col("sha_short", unit="string")],
)
table(
    "sd_files",
    "Files touched",
    "ah.file_touch in the tree: operations per file with lines added and removed (repo-relative when inside a known checkout).",
    [
        sql(
            f"SELECT {RP(FREPO)} AS repo, {TX('coalesce(f.repo_path, f.path)')} AS path, string_agg(DISTINCT f.op, ', ') AS ops, count(*) AS touches, sum(f.lines_added) AS added, sum(f.lines_removed) AS removed, min(f.ts) AS first, max(f.ts) AS last FROM ah.file_touch f LEFT JOIN ah.repo r ON r.id = f.repo_id WHERE f.session_id IN {TREE} GROUP BY 1, 2 ORDER BY touches DESC LIMIT 200"
        )
    ],
    cols=[col("path", width=460)],
)
# the root session of the selected uid (main session: agent_id '')
ROOT = f"(SELECT agent FROM ah.session WHERE session_uid = {SESS} AND agent_id = '' LIMIT 1)"
table(
    "sd_state",
    "Orchestration and state",
    "ah.v_session_orchestration + ah.v_session_state + session_rollup for every session in the tree: where it sits in an orchestration, whether it is still open, what it cost and how it ended.",
    [
        sql(
            f"SELECT o.agent_id, o.orchestration_kind AS kind, o.is_orchestration_descendant AS orchestrated, o.root_session_uid AS orchestration_root, {TX('o.orchestration_evidence')} AS evidence, v.is_open, v.settled_at, v.continuation_kind, v.continued_from_session_uid, r.active_s, r.human_prompts AS prompts, r.error_count AS failures, r.priced_cost_usd AS cost_usd, {RP('r.repo_slug')} AS repo, {TX('r.git_branch')} AS branch, r.final_turn_status FROM ah.v_session_orchestration o JOIN ah.v_session_state v ON v.session_id = o.session_id LEFT JOIN ah.session_rollup r ON r.session_id = o.session_id WHERE o.session_id IN {TREE} ORDER BY o.is_subagent, o.first_event_at LIMIT 300"
        )
    ],
    cols=[
        col("orchestration_root", links=SESSION_LINK),
        col("continued_from_session_uid", links=SESSION_LINK),
        col("active_s", unit="s"),
        col("cost_usd", unit="currencyUSD", gauge=True),
    ],
)
table(
    "sd_similar",
    "Similar sessions" + VEC,
    "ah.similar_sessions: nearest sessions by the mean of their message-chunk vectors (HNSW over ah.session_embedding). Lower distance is closer.",
    [
        sql(
            f"SELECT x.first_event_at, x.namespace, x.agent, round(x.distance::numeric, 3) AS distance, {TX('x.title')} AS title, x.session_uid FROM ah.similar_sessions({ROOT}, {SESS}, '', 15, {NSARR}) x ORDER BY x.distance"
        )
    ],
    cols=[col("session_uid", links=SESSION_LINK), col("title", width=420)],
)
table(
    "sd_conv",
    "Conversation",
    "The root session's conversation in seq order (the order ah.session_events pages): prompts with their origin, replies, briefs, reports and summaries, first 1,500 characters each, bounded to 500 messages.",
    [
        sql(
            f"SELECT m.ts, m.seq, m.message_class, m.prompt_origin, {TX('left(m.text, 1500)')} AS text FROM ah.message m JOIN ah.session s ON s.id = m.session_id WHERE s.session_uid = {SESS} AND s.agent_id = '' AND m.message_class = ANY (ah.conversation_classes()) ORDER BY m.seq LIMIT 500"
        )
    ],
    cols=[col("text", width=1100, wrap=True), col("message_class", width=150)],
)
table(
    "sd_fail",
    "Failures",
    "Failed tool calls and Codex operations in the tree with their error_excerpt.",
    [
        sql(
            f"SELECT c.started_at AS ts, c.tool_name AS tool, c.error_class, {TX('c.error_excerpt')} AS error_excerpt FROM ah.tool_call c WHERE c.session_id IN {TREE} AND c.error_class IS NOT NULL UNION ALL SELECT coalesce(o.started_at, o.completed_at), coalesce(nullif(o.cmd_verb, ''), o.item_type), o.error_class, {TX('o.error_excerpt')} FROM ah.tool_op o WHERE o.session_id IN {TREE} AND o.error_class IS NOT NULL ORDER BY 1 LIMIT 300"
        )
    ],
    cols=[col("error_excerpt", width=900)],
)
table(
    "sd_att",
    "Attachments",
    "ah.attachment in the tree (binary payloads are metadata only).",
    [
        sql(
            f"SELECT a.ts, a.kind, a.source, a.mime, {TX('a.file_name')} AS file_name, a.size_bytes FROM ah.attachment a WHERE a.session_id IN {TREE} ORDER BY a.ts LIMIT 200"
        )
    ],
    cols=[col("size_bytes", unit="bytes")],
)

tab(
    "Session drill-down",
    [
        (9, [("sd_pick", 24)]),
        (4, [("sd_cost", 6), ("sd_calls", 6), ("sd_tools", 6), ("sd_subs", 6)]),
        (8, [("sd_sum", 24)]),
        (6, [("sd_journal", 24)]),
        (8, [("sd_tokens", 12), ("sd_ctx", 12)]),
        (9, [("sd_tool_top", 8), ("sd_spawns", 16)]),
        (8, [("sd_state", 24)]),
        (14, [("sd_conv", 24)]),
        (14, [("sd_timeline", 24)]),
        (8, [("sd_fail", 24)]),
        (8, [("sd_git", 12), ("sd_files", 12)]),
        (8, [("sd_similar", 14), ("sd_att", 10)]),
    ],
)

# ==========================================================================
# TAB: Index & embeddings
# ==========================================================================
stat(
    "ix_ok",
    "Last index run",
    "agent_history_run_success_ratio for the last 5-minute refresh.",
    [prom(f"agent_history_run_success_ratio{{{AH}}}", instant=True)],
    mappings=ok_mapping(),
    colour="background",
)
stat(
    "ix_dur",
    "Last run duration",
    "agent_history_run_duration_seconds of the last refresh.",
    [prom(f"agent_history_run_duration_seconds{{{AH}}}", instant=True)],
    unit="s",
)
stat(
    "ix_dirty",
    "Dirty sessions",
    "Sessions queued for a rollup recompute.",
    [prom(f"agent_history_dirty_sessions{{{AH}}}", instant=True)],
)
stat(
    "ix_lock",
    "Refresh lock held",
    "1 while a refresh or rebuild holds the advisory lock.",
    [prom(f"agent_history_lock_held_ratio{{{AH}}}", instant=True)],
)
stat(
    "ix_cold",
    "Cold tier available",
    "Whether the NFS cold archive was reachable on the last run.",
    [prom(f"agent_history_cold_tier_available_ratio{{{AH}}}", instant=True)],
    mappings=ok_mapping("YES", "NO"),
    colour="background",
)
stat(
    "ix_parse",
    "Parse issues",
    "Rows in ah.parse_issue (tolerated oddities, not failures).",
    [prom(f"sum(agent_history_parse_issues_total{{{AH}}})", instant=True)],
)
timeseries(
    "ix_dur_ts",
    "Index run duration",
    "Seconds per 5-minute refresh.",
    [prom(f"agent_history_run_duration_seconds{{{AH}}}", legend="duration")],
    unit="s",
)
timeseries(
    "ix_files_ts",
    "Files per run by result",
    "parsed, rewritten (a transcript that shrank or changed head and was re-read) and load_error.",
    [prom(f"sum by (result) (agent_history_run_files{{{AH}}})", legend="{{result}}")],
)
timeseries(
    "ix_rows_ts",
    "Rows and lines ingested per run",
    "Rows written and transcript lines read per refresh.",
    [
        prom(f"agent_history_run_rows{{{AH}}}", legend="rows"),
        prom(f"agent_history_run_lines{{{AH}}}", ref="B", legend="lines"),
    ],
    overrides=[
        {
            "matcher": {"id": "byFrameRefID", "options": "B"},
            "properties": [{"id": "custom.axisPlacement", "value": "right"}],
        }
    ],
)
timeseries(
    "ix_lag_ts",
    "Lag and backlog",
    "Unindexed bytes and dirty sessions.",
    [
        prom(f"agent_history_lag_bytes{{{AH}}}", legend="lag bytes"),
        prom(f"agent_history_dirty_sessions{{{AH}}}", ref="B", legend="dirty sessions"),
    ],
    overrides=[
        {"matcher": {"id": "byName", "options": "lag bytes"}, "properties": [{"id": "unit", "value": "bytes"}]},
        {
            "matcher": {"id": "byName", "options": "dirty sessions"},
            "properties": [{"id": "custom.axisPlacement", "value": "right"}],
        },
    ],
)
timeseries(
    "ix_unres",
    "Unresolved links",
    "Sessions whose root or spawn child could not be linked yet (usually the other side not synced).",
    [prom(f"sum by (kind) (agent_history_unresolved_links{{{AH}}})", legend="{{kind}}")],
)
timeseries(
    "ix_parse_ts",
    "Parse issues by kind",
    "agent_history_parse_issues_total by kind (tolerated oddities, not failures).",
    [prom(f"sum by (kind) (agent_history_parse_issues_total{{{AH}}})", legend="{{kind}}")],
)
timeseries(
    "ix_rows_growth",
    "Catalogue rows by table",
    "agent_history_rows per table.",
    [prom(f"sum by (table) (agent_history_rows{{{AH}}})", legend="{{table}}")],
    legend_mode="table",
    legend_pos="right",
    calcs=["lastNotNull"],
)
table(
    "ix_sources",
    "Source files",
    "ah.source_file by namespace, status and tier, with parser versions and rewrite counts.",
    [
        sql(
            f"SELECT namespace, agent, status, tier, count(*) AS files, sum(size_bytes) AS bytes, sum(indexed_offset)::numeric / nullif(sum(size_bytes),0) AS byte_coverage, count(*) FILTER (WHERE available_hot) AS hot, count(*) FILTER (WHERE available_cold) AS cold, sum(rewrite_count) AS rewrites, string_agg(DISTINCT parser_version, ', ') AS parsers, max(indexed_at) AS last_indexed FROM ah.source_file WHERE {NSAG()} GROUP BY 1,2,3,4 ORDER BY bytes DESC"
        )
    ],
    cols=[col("bytes", unit="bytes", gauge=True), col("byte_coverage", unit="percentunit")],
)
table(
    "ix_drift",
    "New record types (schema drift)",
    "ah.record_type_seen first seen in range: new JSONL record shapes from a CLI upgrade. The parser may be ignoring them.",
    [
        sql(
            "SELECT agent, record_type, subtype, first_seen, last_seen, seen_count, cli_version, left(key_set, 200) AS keys FROM ah.record_type_seen WHERE $__timeFilter(first_seen) ORDER BY first_seen DESC LIMIT 100"
        )
    ],
    cols=[col("keys", width=520)],
)
table(
    "ix_issues",
    "Recent parse issues",
    "ah.parse_issue rows created in range.",
    [
        sql(
            f"SELECT p.created_at, p.kind, f.namespace, {TX('f.rel_path')} AS rel_path, p.line_number, {TX('left(p.detail, 200)')} AS detail FROM ah.parse_issue p LEFT JOIN ah.source_file f ON f.id = p.source_id WHERE $__timeFilter(p.created_at) AND {NS('f.namespace')} ORDER BY p.created_at DESC LIMIT 100"
        )
    ],
    cols=[col("rel_path", width=380), col("detail", width=380)],
)
# embeddings
EMB = "agent_history_embed"
stat(
    "em_vec",
    "Vectors" + VEC,
    "Rows in ah.embedding (paid cache keyed by model + input hash).",
    [prom(f"{EMB}_vectors{{{AH}}}", instant=True)],
)
stat(
    "em_pending",
    "Pending messages",
    "Eligible messages without a vector yet.",
    [prom(f"{EMB}_pending_messages{{{AH}}}", instant=True)],
    steps_=steps("green", (5000, "yellow"), (50000, "red")),
)
stat(
    "em_failed",
    "Failed inputs",
    "Inputs that failed 3+ times (ah.embed_failure).",
    [prom(f"{EMB}_failed_inputs{{{AH}}}", instant=True)],
    steps_=steps("green", (1, "yellow"), (100, "red")),
)
stat(
    "em_ok",
    "Last embed run",
    "agent_history_embed_run_success_ratio for the last 10-minute embed run.",
    [prom(f"{EMB}_run_success_ratio{{{AH}}}", instant=True)],
    mappings=ok_mapping(),
    colour="background",
)
stat(
    "em_cov",
    "Message coverage" + VEC,
    "Share of embeddable messages (conversation classes except task_notification_summary, non-empty text) that have at least one chunk with a vector.",
    [
        sql(
            "SELECT (SELECT count(DISTINCT message_id) FROM ah.chunk WHERE message_id IS NOT NULL AND input_sha256 <> '')::numeric / nullif((SELECT count(*) FROM ah.message WHERE length(text) > 0 AND message_class = ANY (ah.conversation_classes()) AND message_class <> 'task_notification_summary'),0)"
        )
    ],
    unit="percentunit",
    decimals=1,
)
stat(
    "em_model",
    "Embedding model",
    "ah.meta embedding_model and input version (a change is a deliberate full re-embed).",
    [
        sql(
            "SELECT (SELECT value FROM ah.meta WHERE key='embedding_model') || ' v' || coalesce((SELECT value FROM ah.meta WHERE key='embed_input_version'),'?') AS model"
        )
    ],
    unit="string",
)
timeseries(
    "em_run",
    "Embed run: API vs cached inputs",
    "Per 10-minute run: inputs sent to the API, inputs served from the embedding cache, chunks.",
    [
        prom(f"{EMB}_run_api_inputs{{{AH}}}", legend="api inputs"),
        prom(f"{EMB}_run_cached_inputs{{{AH}}}", ref="B", legend="cached inputs"),
        prom(f"{EMB}_run_chunks{{{AH}}}", ref="C", legend="chunks"),
    ],
)
timeseries(
    "em_tok",
    "Embed tokens per run",
    "OpenAI tokens billed per run (text-embedding-3-large list price $0.13/M).",
    [prom(f"{EMB}_run_tokens{{{AH}}}", legend="tokens")],
)
timeseries(
    "em_pend_ts",
    "Vectors and pending",
    "Vectors in ah.embedding and messages still waiting for one.",
    [
        prom(f"{EMB}_vectors{{{AH}}}", legend="vectors"),
        prom(f"{EMB}_pending_messages{{{AH}}}", ref="B", legend="pending"),
    ],
    overrides=[
        {
            "matcher": {"id": "byName", "options": "pending"},
            "properties": [{"id": "custom.axisPlacement", "value": "right"}],
        }
    ],
)
timeseries(
    "em_skip",
    "Embed skips by reason",
    "Why a run did nothing: refresh in progress, rebuild, daily cap, none.",
    [prom(f"sum by (reason) ({EMB}_run_skipped_ratio{{{AH}}})", legend="{{reason}}")],
)
bars(
    "em_daily",
    "Embedding tokens per day",
    "ah.meta embed_tokens_<date>: tokens the 10-minute timer counted against its daily cap. Manual uncapped runs (a full re-embed) are not in this ledger; see Embed tokens per run.",
    [
        sql(
            "SELECT greatest(time, $__timeFrom()::timestamptz) AS time, tokens FROM (SELECT to_date(substr(key, 14), 'YYYY-MM-DD')::timestamptz AS time, value::bigint AS tokens FROM ah.meta WHERE key LIKE 'embed\\_tokens\\_%') d WHERE time >= date_trunc('day', $__timeFrom()::timestamptz) AND time <= $__timeTo()::timestamptz ORDER BY 1",
            ts=True,
        )
    ],
)
table(
    "em_ns",
    "Semantic coverage by namespace" + VEC,
    "Chunks and embedded messages per namespace (ah.chunk).",
    [
        sql(
            f"SELECT namespace, count(*) AS chunks, count(DISTINCT message_id) AS messages, count(DISTINCT summary_id) AS summaries, count(*) FILTER (WHERE input_sha256 = '') AS unembedded_chunks, max(ts) AS newest FROM ah.chunk WHERE {NS()} GROUP BY 1 ORDER BY chunks DESC"
        )
    ],
    cols=[col("chunks", gauge=True)],
)
table(
    "em_fail_t",
    "Embedding failures",
    "ah.embed_failure rows (last_error is the API message, not content).",
    [
        sql(
            "SELECT last_at, model, attempts, left(last_error, 200) AS last_error, message_id, summary_id FROM ah.embed_failure ORDER BY last_at DESC LIMIT 50"
        )
    ],
)

stat(
    "em_err",
    "Error excerpts embedded" + VEC,
    "tool_call / tool_op error_excerpt chunks with a vector (header [tool_error:<tool>:<class>]); search_hybrid ranks them as tool I/O hits.",
    [
        sql(
            "SELECT count(*) FROM ah.chunk WHERE (tool_call_id IS NOT NULL OR tool_op_id IS NOT NULL) AND input_sha256 <> ''"
        )
    ],
)
stat(
    "em_sess",
    "Session vectors" + VEC,
    "ah.session_embedding rows: the mean of each session's message-chunk vectors, recomputed by the post-pass at no API cost. Feeds ah.similar_sessions.",
    [sql("SELECT count(*) FROM ah.session_embedding")],
)
stat(
    "cf_open",
    "Open sessions",
    "ah.v_session_state.is_open: an event in the last 30 min, or an open turn active in the last 6 h. The journal waits for these to settle.",
    [sql(f"SELECT count(*) FROM ah.v_session_state WHERE is_open AND {NSAG()}")],
)
stat(
    "cf_changes",
    "Session changes",
    "ah.change_log rows in range: one per session changed by a refresh (the agentic-journal feed).",
    [sql("SELECT count(*) FROM ah.change_log WHERE $__timeFilter(at) AND kind <> 'rebuild'")],
)
stat(
    "cf_refresh",
    "Last refresh",
    "Time since the last finished refresh in ah.refresh_log (each ends with NOTIFY ah_refresh).",
    [sql("SELECT extract(epoch FROM now() - max(finished_at)) FROM ah.refresh_log WHERE ok")],
    unit="s",
    steps_=steps("green", (600, "yellow"), (1200, "red")),
)
stat(
    "cf_rebuild",
    "Last rebuild",
    "The latest kind='rebuild' marker in ah.change_log: consumers re-page seq cursors from 0 after it.",
    [sql("SELECT extract(epoch FROM max(at)) * 1000 FROM ah.change_log WHERE kind = 'rebuild'")],
    unit="dateTimeAsLocal",
)
bars(
    "cf_ts",
    "Change feed by kind",
    "ah.change_log rows per bucket: content (new events) and orchestration (reclassified) changes.",
    [
        sql(
            f"SELECT {BUCKET.format(col='at')}, kind AS metric, count(*) AS value FROM ah.change_log WHERE $__timeFilter(at) AND kind <> 'rebuild' GROUP BY 1,2 ORDER BY 1",
            ts=True,
        )
    ],
)
timeseries(
    "cf_refresh_ts",
    "Sessions changed per refresh",
    "ah.refresh_log: sessions_changed per refresh and how long each took.",
    [
        sql(
            'SELECT finished_at AS time, sessions_changed AS "sessions changed", extract(epoch FROM finished_at - started_at) AS "duration s" FROM ah.refresh_log WHERE $__timeFilter(finished_at) AND ok ORDER BY 1',
            ts=True,
        )
    ],
    points=True,
    overrides=[
        {
            "matcher": {"id": "byName", "options": "duration s"},
            "properties": [{"id": "custom.axisPlacement", "value": "right"}, {"id": "unit", "value": "s"}],
        }
    ],
)

tab(
    "Index & embeddings",
    [
        (4, [("ix_ok", 4), ("ix_dur", 4), ("ix_dirty", 4), ("ix_lock", 4), ("ix_cold", 4), ("ix_parse", 4)]),
        (8, [("ix_dur_ts", 8), ("ix_files_ts", 8), ("ix_rows_ts", 8)]),
        (8, [("ix_lag_ts", 8), ("ix_unres", 8), ("ix_parse_ts", 8)]),
        (9, [("ix_rows_growth", 24)]),
        (8, [("ix_sources", 24)]),
        (8, [("ix_drift", 12), ("ix_issues", 12)]),
        (4, [("em_vec", 4), ("em_pending", 4), ("em_failed", 4), ("em_ok", 4), ("em_cov", 4), ("em_model", 4)]),
        (8, [("em_run", 12), ("em_tok", 12)]),
        (8, [("em_pend_ts", 8), ("em_skip", 8), ("em_daily", 8)]),
        (7, [("em_ns", 12), ("em_fail_t", 12)]),
        (4, [("em_err", 4), ("em_sess", 4), ("cf_open", 4), ("cf_changes", 4), ("cf_refresh", 4), ("cf_rebuild", 4)]),
        (8, [("cf_ts", 12), ("cf_refresh_ts", 12)]),
    ],
)

# ==========================================================================
# TAB: Storage & database
# ==========================================================================
AS = AH
PGI = 'instance="agent-history"'
PGT = 'instance="agent-history",schemaname="ah"'
stat(
    "st_hot",
    "Hot JSONL",
    "Transcript bytes in the camden hot tier (the shared archive metrics).",
    [prom(f'sum(agent_sessions_storage_bytes{{{AS},tier="hot",namespace=~"$ns_re"}})', instant=True)],
    unit="bytes",
)
stat(
    "st_cold",
    "Cold JSONL",
    "Transcript bytes on the scotty NFS cold tier (the permanent authority).",
    [prom(f'sum(agent_sessions_storage_bytes{{{AS},tier="cold",namespace=~"$ns_re"}})', instant=True)],
    unit="bytes",
)
stat(
    "st_db",
    "Database size",
    "pg_database_size('agent_history').",
    [sql("SELECT pg_database_size('agent_history')")],
    unit="bytes",
)
stat(
    "st_nfs",
    "Cold NFS mounted",
    "agent_sessions_cold_nfs_mounted_ratio: 1 when the scotty NFS cold tier is mounted on camden.",
    [prom(f"agent_sessions_cold_nfs_mounted_ratio{{{AS}}}", instant=True)],
    mappings=ok_mapping("MOUNTED", "DOWN"),
    colour="background",
)
stat(
    "st_pending",
    "Awaiting archive",
    "Files not yet archived to the cold tier.",
    [prom(f"agent_sessions_archive_pending_files{{{AS}}}", instant=True)],
)
stat(
    "st_conns",
    "DB connections",
    "pg_stat_activity count for agent_history.",
    [prom(f'sum(pg_stat_activity_count{{{PGI},datname="agent_history"}})', instant=True)],
)
timeseries(
    "st_tiers",
    "JSONL by namespace and tier",
    "agent_sessions_storage_bytes per namespace and tier.",
    [
        prom(
            f'sum by (namespace, tier) (agent_sessions_storage_bytes{{{AS},namespace=~"$ns_re"}})',
            legend="{{namespace}} {{tier}}",
        )
    ],
    unit="bytes",
    legend_mode="table",
    legend_pos="right",
    calcs=["lastNotNull"],
)
timeseries(
    "st_fs",
    "Filesystem utilisation",
    "Hot (camden) and cold (NFS) filesystems.",
    [
        prom(
            f'100 * sum by (tier) (agent_sessions_filesystem_bytes{{{AS},kind="used"}}) / sum by (tier) (agent_sessions_filesystem_bytes{{{AS},kind="total"}})',
            legend="{{tier}}",
        )
    ],
    unit="percent",
    minv=0,
    maxv=100,
)
timeseries(
    "st_tables",
    "Table size (heap)",
    "pg_stat_user_tables_table_size_bytes for the ah schema, top 12.",
    [prom(f"topk(12, sum by (relname) (pg_stat_user_tables_table_size_bytes{{{PGT}}}))", legend="{{relname}}")],
    unit="bytes",
    legend_mode="table",
    legend_pos="right",
    calcs=["lastNotNull"],
)
timeseries(
    "st_idx",
    "Index size",
    "pg_stat_user_tables_index_size_bytes for the ah schema, top 12 (includes the ParadeDB BM25 and HNSW indexes).",
    [prom(f"topk(12, sum by (relname) (pg_stat_user_tables_index_size_bytes{{{PGT}}}))", legend="{{relname}}")],
    unit="bytes",
    legend_mode="table",
    legend_pos="right",
    calcs=["lastNotNull"],
)
timeseries(
    "st_ins",
    "Rows written per second",
    "Insert + update + delete rate per table: the ingest shape of each refresh.",
    [
        prom(
            f"topk(10, sum by (relname) (rate(pg_stat_user_tables_n_tup_ins{{{PGT}}}[$__rate_interval]) + rate(pg_stat_user_tables_n_tup_upd{{{PGT}}}[$__rate_interval]) + rate(pg_stat_user_tables_n_tup_del{{{PGT}}}[$__rate_interval])))",
            legend="{{relname}}",
        )
    ],
    unit="wps",
)
timeseries(
    "st_dead",
    "Dead tuples",
    "n_dead_tup per table, top 10. Rising and never falling = autovacuum not keeping up.",
    [prom(f"topk(10, sum by (relname) (pg_stat_user_tables_n_dead_tup{{{PGT}}}))", legend="{{relname}}")],
)
timeseries(
    "st_seq",
    "Sequential scans per second",
    "seq_scan rate per table: the tables dashboards and the CLI read without an index.",
    [
        prom(
            f"topk(10, sum by (relname) (rate(pg_stat_user_tables_seq_scan{{{PGT}}}[$__rate_interval])))",
            legend="{{relname}}",
        )
    ],
    unit="ops",
)
timeseries(
    "st_conn_ts",
    "Connections by state",
    "pg_stat_activity_count for agent_history by connection state.",
    [prom(f'sum by (state) (pg_stat_activity_count{{{PGI},datname="agent_history"}})', legend="{{state}}")],
)
table(
    "st_rel",
    "Relations by total size",
    "Heap, index and TOAST per ah relation (pg_total_relation_size), live now.",
    [
        sql(
            "SELECT c.relname AS relation, pg_total_relation_size(c.oid) AS total, pg_relation_size(c.oid) AS heap, pg_indexes_size(c.oid) AS indexes, pg_total_relation_size(c.oid) - pg_relation_size(c.oid) - pg_indexes_size(c.oid) AS toast, c.reltuples::bigint AS est_rows FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ah' AND c.relkind = 'r' ORDER BY total DESC LIMIT 40"
        )
    ],
    cols=[
        col("total", unit="bytes", gauge=True),
        col("heap", unit="bytes"),
        col("indexes", unit="bytes"),
        col("toast", unit="bytes"),
    ],
    sort="total",
)
table(
    "st_vac",
    "Vacuum and analyze",
    "Last (auto)vacuum and (auto)analyze per table from the exporter, and rows modified since analyze.",
    [prom(f"max by (relname) (pg_stat_user_tables_n_mod_since_analyze{{{PGT}}})", instant=True, fmt="table")],
    cols=[col("Value", unit="short")],
    sort="Value",
)

PGLOG = '{service_name="agent-history-db"}'
timeseries(
    "st_pglog_ts",
    "Postgres log lines by level",
    "Log lines from the ParadeDB container per interval, split by Loki's detected_level. Postgres writes "
    "ERROR lines as level error but FATAL and most other lines as unknown, so the error panel beside it "
    "matches the severity token itself.",
    [loki(f"sum by (detected_level) (count_over_time({PGLOG} [$__auto]))", legend="{{detected_level}}")],
    bars=True,
    legend_mode="table",
    legend_pos="right",
    calcs=["sum"],
)
logs(
    "st_pglog_err",
    "Postgres errors, fatals and panics",
    "ParadeDB container log lines carrying the ERROR, FATAL or PANIC severity token (Postgres logs it as ':ERROR:  '). "
    "A match on the bare word error is far broader: it also hits statement text.",
    [loki(f'{PGLOG} |~ ":(ERROR|FATAL|PANIC):  "')],
)

IDX_VALUES = (
    "(VALUES ('ah.message_search_idx'::regclass), ('ah.session_summary_search_idx'::regclass), "
    "('ah.tool_io_search_idx'::regclass)) i(idx)"
)
stat(
    "px_ver",
    "ParadeDB version" + PDB,
    PDBD + "paradedb.version_info(). Upgrades are manual: pull, up -d, ALTER EXTENSION pg_search UPDATE.",
    [sql("SELECT version FROM paradedb.version_info()")],
    unit="string",
)
stat(
    "px_docs",
    "Documents in BM25 indexes" + PDB,
    PDBD
    + "num_docs summed over visible segments of the three BM25 indexes (messages, journal summaries, tool I/O; paradedb.index_info).",
    [sql(f"SELECT sum(x.num_docs) FROM {IDX_VALUES} CROSS JOIN LATERAL paradedb.index_info(i.idx) x")],
)
stat(
    "px_segs",
    "BM25 segments" + PDB,
    PDBD + "visible Tantivy segments across the three indexes. Many small segments = merges falling behind.",
    [sql(f"SELECT count(*) FROM {IDX_VALUES} CROSS JOIN LATERAL paradedb.index_info(i.idx) x")],
    steps_=steps("green", (60, "yellow"), (150, "red")),
)
stat(
    "px_del",
    "Deleted docs awaiting merge" + PDB,
    PDBD + "num_deleted across segments: rows rewritten or deleted but still in a segment until the next merge.",
    [sql(f"SELECT sum(x.num_deleted) FROM {IDX_VALUES} CROSS JOIN LATERAL paradedb.index_info(i.idx) x")],
)
stat(
    "px_bytes",
    "BM25 index bytes" + PDB,
    PDBD + "byte_size of the three BM25 indexes.",
    [sql(f"SELECT sum(x.byte_size) FROM {IDX_VALUES} CROSS JOIN LATERAL paradedb.index_info(i.idx) x")],
    unit="bytes",
)
stat(
    "px_hnsw",
    "HNSW index bytes" + VEC,
    "pgvector HNSW index embedding_hnsw_idx (halfvec cosine), bundled with ParadeDB.",
    [sql("SELECT pg_relation_size('ah.embedding_hnsw_idx')")],
    unit="bytes",
)
table(
    "px_comp",
    "BM25 index composition" + PDB,
    PDBD
    + "bytes per Tantivy component in each index: term dictionary, postings, positions (phrase search), fast fields (facets, sorting, ts filters), fieldnorms and the doc store.",
    [
        sql(
            f"SELECT i.idx::text AS index, count(*) AS segments, sum(x.num_docs) AS docs, sum(x.num_deleted) AS deleted, sum(x.termdict_bytes) AS termdict, sum(x.postings_bytes) AS postings, sum(x.positions_bytes) AS positions, sum(x.fast_fields_bytes) AS fast_fields, sum(x.fieldnorms_bytes) AS fieldnorms, sum(x.store_bytes) AS store, sum(x.byte_size) AS total FROM {IDX_VALUES} CROSS JOIN LATERAL paradedb.index_info(i.idx) x GROUP BY 1"
        )
    ],
    cols=[col(c, unit="bytes") for c in ("termdict", "postings", "positions", "fast_fields", "fieldnorms", "store")]
    + [col("total", unit="bytes", gauge=True)],
)
barchart(
    "px_seg_sizes",
    "Segment size distribution" + PDB,
    PDBD
    + "docs per segment for message_search_idx (pdb.index_segments), largest first. A long tail of tiny segments means the background merger is behind.",
    [
        sql(
            "SELECT lpad(row_number() OVER (ORDER BY num_docs DESC)::text, 3, '0') AS segment, num_docs AS docs, num_deleted AS deleted FROM pdb.index_segments('ah.message_search_idx') ORDER BY num_docs DESC LIMIT 60"
        )
    ],
    x="segment",
    stack=True,
)
table(
    "px_schema",
    "BM25 index schema" + PDB,
    PDBD
    + "paradedb.schema(): which fields are indexed, which are fast (columnar, usable by pdb.agg and ts filters) and their tokenizers.",
    [
        sql(
            "SELECT 'message_search_idx' AS index, name, field_type, tokenizer, fast, indexed, fieldnorms FROM paradedb.schema('ah.message_search_idx') UNION ALL SELECT 'session_summary_search_idx', name, field_type, tokenizer, fast, indexed, fieldnorms FROM paradedb.schema('ah.session_summary_search_idx') UNION ALL SELECT 'tool_io_search_idx', name, field_type, tokenizer, fast, indexed, fieldnorms FROM paradedb.schema('ah.tool_io_search_idx') ORDER BY 1, 2"
        )
    ],
)

tab(
    "Storage, database & ParadeDB",
    [
        (4, [("st_hot", 4), ("st_cold", 4), ("st_db", 4), ("st_nfs", 4), ("st_pending", 4), ("st_conns", 4)]),
        (8, [("st_tiers", 14), ("st_fs", 10)]),
        (9, [("st_tables", 12), ("st_idx", 12)]),
        (8, [("st_ins", 12), ("st_dead", 12)]),
        (8, [("st_seq", 12), ("st_conn_ts", 12)]),
        (10, [("st_rel", 14), ("st_vac", 10)]),
        (9, [("st_pglog_ts", 10), ("st_pglog_err", 14)]),
        (4, [("px_ver", 4), ("px_docs", 4), ("px_segs", 4), ("px_del", 4), ("px_bytes", 4), ("px_hnsw", 4)]),
        (7, [("px_comp", 24)]),
        (9, [("px_seg_sizes", 12), ("px_schema", 12)]),
    ],
)


# ==========================================================================
# variables, annotations, assemble
# ==========================================================================
def sqlvar(name, label, raw, multi=True, include_all=True, all_value="'__all__'", hide="dontHide"):
    return {
        "kind": "QueryVariable",
        "spec": {
            "name": name,
            "label": label,
            "hide": hide,
            "skipUrlSync": False,
            "multi": multi,
            "includeAll": include_all,
            **({"allValue": all_value} if include_all else {}),
            "allowCustomValue": True,
            "refresh": "onTimeRangeChanged" if "$__time" in raw else "onDashboardLoad",
            "regex": "",
            "sort": "disabled",
            "options": [],
            "current": {"text": "All", "value": "$__all"} if include_all else {"text": "", "value": ""},
            "definition": raw,
            "query": {
                "kind": "DataQuery",
                "group": "grafana-postgresql-datasource",
                "version": "v0",
                "datasource": {"name": PG},
                "spec": {"rawSql": raw, "format": "table", "editorMode": "code", "rawQuery": True, "refId": name},
            },
        },
    }


def textvar(name, label, default=""):
    return {
        "kind": "TextVariable",
        "spec": {
            "name": name,
            "label": label,
            "hide": "dontHide",
            "skipUrlSync": False,
            "query": default,
            "current": {"text": default, "value": default},
        },
    }


def customvar(name, label, values, default, multi=False, hide="dontHide"):
    """values: list of str, or (text, value) pairs rendered as Grafana's 'text : value' syntax."""
    pairs = [v if isinstance(v, tuple) else (v, v) for v in values]
    query = ", ".join(t if t == v else f"{t} : {v}" for t, v in pairs)
    defaults = default if isinstance(default, list) else [default]
    cur = [(t, v) for t, v in pairs if t in defaults]
    current = (
        {"text": [t for t, _ in cur], "value": [v for _, v in cur]}
        if multi
        else {"text": cur[0][0], "value": cur[0][1]}
    )
    return {
        "kind": "CustomVariable",
        "spec": {
            "name": name,
            "label": label,
            "hide": hide,
            "skipUrlSync": False,
            "query": query,
            "multi": multi,
            "includeAll": False,
            # Custom variables feed unescaped SQL fragments: only their listed options are allowed.
            "allowCustomValue": False,
            "current": current,
            "options": [{"text": t, "value": v, "selected": t in defaults} for t, v in pairs],
        },
    }


stat(
    "type_unknown",
    "Children with unknown type",
    "Subagent sessions with no explicit type or certain harness default. Display names and task labels are never type evidence.",
    [
        sql(
            f"SELECT count(*) FROM ah.session c WHERE $__timeFilter(c.first_event_at) AND c.is_subagent AND c.agent_type IS NULL AND {NSAG('c')}"
        )
    ],
)
bargauge(
    "type_source",
    "Child types and source",
    "Child session agent type and provenance: explicit from recorded launch or child metadata; default only when the harness default is certain. Unknown remains visible.",
    [
        sql(
            f"SELECT coalesce(c.agent_type,'(unknown)') || ' / ' || coalesce(c.agent_type_source,'unknown') AS type_source, count(*) AS sessions FROM ah.session c WHERE $__timeFilter(c.first_event_at) AND c.is_subagent AND {NSAG('c')} GROUP BY 1 ORDER BY 2 DESC LIMIT 30"
        )
    ],
)
tab("Agent types", [(10, [("type_source", 18), ("type_unknown", 6)])])

VARIABLES = [
    customvar("redact", "Redact", ["off", "on"], "off", hide="hideVariable"),
    sqlvar(
        "namespace",
        "Namespace",
        "SELECT DISTINCT namespace FROM ah.session WHERE namespace IS NOT NULL ORDER BY 1 LIMIT 100",
    ),
    sqlvar("agent", "Agent", "SELECT DISTINCT agent FROM ah.session ORDER BY 1"),
    sqlvar(
        "repo",
        "Repo",
        "SELECT DISTINCT repo_slug FROM ah.git_commit UNION SELECT DISTINCT repo_slug FROM ah.loop_run WHERE repo_slug IS NOT NULL ORDER BY 1 LIMIT 300",
    ),
    sqlvar(
        "ns_re",
        "Namespace regex",
        "SELECT CASE WHEN '__all__' IN ($namespace) THEN '.+' ELSE array_to_string(ARRAY[$namespace]::text[], '|') END",
        multi=False,
        include_all=False,
        hide="hideVariable",
    ),
    customvar("bucket", "Bucket", ["1h", "6h", "1d", "1w"], "1d"),
    sqlvar(
        "loop_id",
        "Loop",
        "WITH choices AS (SELECT loop_run_id::text AS __value, "
        "CASE WHEN '$redact' = 'on' THEN 'loop' || coalesce(loop_number::text,'?') "
        "ELSE coalesce(repo_slug,'(unknown repo)') || '/' || "
        "CASE WHEN loop_number IS NULL THEN coalesce(campaign_slug,'run') ELSE 'loop' || loop_number::text END END "
        "|| ' · ' || to_char(launch_ts, 'MM-DD HH24:MI') AS __text, launch_ts "
        "FROM ah.v_loop_summary WHERE $__timeFilter(launch_ts) "
        "AND ('__all__' IN ($repo) OR repo_slug IN ($repo)) "
        "AND ('__all__' IN ($namespace) OR namespace IN ($namespace)) "
        "ORDER BY launch_ts DESC LIMIT 100), labelled AS ("
        "SELECT __value,__text,launch_ts FROM choices UNION ALL "
        "SELECT '', 'No loops in selected range', NULL::timestamptz WHERE NOT EXISTS (SELECT 1 FROM choices)) "
        "SELECT __value,__text FROM labelled ORDER BY launch_ts DESC NULLS LAST",
        multi=False,
        include_all=False,
    ),
    textvar("q", "Search"),
    customvar("mode", "Match", [("all", "&&&"), ("any", "|||"), ("phrase", "###")], "all"),
    customvar("scope", "Scope", [("conversation", "conversation"), ("everything", "all")], "conversation"),
    customvar(
        "watch",
        "Watch terms",
        ["rate limit", "timeout", "permission denied", "CodeRabbit", "rollback", "flaky", "OpenBao", "Grafana"],
        ["rate limit", "timeout", "permission denied", "rollback"],
        multi=True,
    ),
    textvar("session", "Session"),
]

ANNOTATIONS = [
    {
        "kind": "AnnotationQuery",
        "spec": {
            "builtIn": True,
            "enable": True,
            "hide": True,
            "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts",
            "query": {
                "kind": "DataQuery",
                "group": "grafana",
                "version": "v0",
                "datasource": {"name": "-- Grafana --"},
                "spec": {},
            },
        },
    },
    {
        "kind": "AnnotationQuery",
        "spec": {
            "builtIn": False,
            "enable": False,
            "hide": False,
            "iconColor": "orange",
            "name": "Agent infra actions",
            "query": {
                "kind": "DataQuery",
                "group": "grafana-postgresql-datasource",
                "version": "v0",
                "datasource": {"name": PG},
                "spec": {
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "refId": "Anno",
                    "rawSql": "SELECT ts AS time, host || ' ' || COALESCE(remote_verb, '(login)') || ' [' || agent || ']' AS text, host AS tags FROM ah.v_infra_action WHERE $__timeFilter(ts) AND ('__all__' IN ($namespace) OR namespace IN ($namespace)) ORDER BY ts LIMIT 2000",
                },
            },
        },
    },
    {
        "kind": "AnnotationQuery",
        "spec": {
            "builtIn": False,
            "enable": False,
            "hide": False,
            "iconColor": "purple",
            "name": "Loop launches",
            "query": {
                "kind": "DataQuery",
                "group": "grafana-postgresql-datasource",
                "version": "v0",
                "datasource": {"name": PG},
                "spec": {
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "refId": "Loops",
                    "rawSql": "SELECT launch_ts AS time, end_ts AS timeend, 'loop ' || loop_run_id || ' ' || coalesce(repo_slug,'') || coalesce(' ' || campaign_slug,'') AS text, 'loop' AS tags FROM ah.v_loop_summary WHERE $__timeFilter(launch_ts) AND ('__all__' IN ($namespace) OR namespace IN ($namespace)) ORDER BY 1 LIMIT 500",
                },
            },
        },
    },
    {
        "kind": "AnnotationQuery",
        "spec": {
            "builtIn": False,
            "enable": False,
            "hide": False,
            "iconColor": "yellow",
            "name": "Policy changes",
            "query": {
                "kind": "DataQuery",
                "group": "grafana-postgresql-datasource",
                "version": "v0",
                "datasource": {"name": PG},
                "spec": {
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "refId": "Policy",
                    "rawSql": "SELECT committed_at AS time, repo_slug || ': ' || path || ' (' || left(subject, 80) || ')' AS text, 'policy' AS tags FROM ah.policy_changes(NULL, $__timeFrom()::timestamptz) WHERE committed_at <= $__timeTo()::timestamptz AND (path ~* '(AGENTS|CLAUDE)\\.md$' OR path ~* '(^|/)(rules|skills|reference)/') ORDER BY 1 LIMIT 300",
                },
            },
        },
    },
]

LAYOUT = {"kind": "TabsLayout", "spec": {"tabs": TABS}}

placed = {i["spec"]["element"]["name"] for t in TABS for i in t["spec"]["layout"]["spec"]["items"]}
orphans, missing = set(ELEMENTS) - placed, placed - set(ELEMENTS)
assert not orphans, f"orphaned elements (would crash the dashboard): {orphans}"
assert not missing, f"layout references undefined elements: {missing}"

DASH = {
    "apiVersion": "dashboard.grafana.app/v2",
    "kind": "Dashboard",
    "metadata": {"name": UID, "annotations": {"grafana.app/folder": FOLDER}},
    "spec": {
        "title": "Agent History catalogue (ParadeDB) · Overview",
        "description": (
            "Claude Code and Codex history from the agent-history ParadeDB catalogue on camden (schema ah): "
            "activity, stored content (message classes, prompt origins, reasoning, attachments, file touches), tokens, "
            "cost, subagents, orchestration, efficiency, loops, tools, failures and tool I/O, hooks, limits, commit "
            "quality, correction signals, infra actions, BM25 search over messages and tool I/O, per-session "
            "drill-down, the change feed, and the health of the indexer, embedder, archive and database. Generated by grafana/build_catalogue_dashboard.py."
        ),
        "tags": ["agent-history", "agents", "claude", "codex", "paradedb", "camden"],
        "editable": True,
        "preload": False,
        "cursorSync": "Crosshair",
        "liveNow": False,
        "links": [
            {
                "title": "Session Archive and Index (SQLite)",
                "type": "link",
                "icon": "external link",
                "tooltip": "The v1 SQLite index dashboard",
                "url": "/d/agent-session-archive",
                "tags": [],
                "asDropdown": False,
                "targetBlank": False,
                "includeVars": False,
                "keepTime": True,
            }
        ],
        "annotations": ANNOTATIONS,
        "variables": VARIABLES,
        "elements": ELEMENTS,
        "layout": LAYOUT,
        "timeSettings": {
            "from": "now-7d",
            "to": "now",
            "autoRefresh": "",
            "autoRefreshIntervals": ["1m", "5m", "15m", "1h"],
            "hideTimepicker": False,
            "fiscalYearStartMonth": 0,
            "timezone": "browser",
        },
    },
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(DASH, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != content:
            raise SystemExit(f"generated dashboard drift: {OUT.relative_to(ROOT)}")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
