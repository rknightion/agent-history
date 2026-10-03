"""Embedder: message chunks -> vectors through an OpenAI-compatible /v1/embeddings endpoint.

Off unless configured ([embedding] in the config file, see config.example.toml).

- ah.embedding is a cache keyed by (model, sha256 of the exact input). A rebuild recreates ah.chunk
  and finds every vector by hash, so it costs no API calls.
- Inputs: chunks of eligible prompt, assistant, subagent-brief/report and compaction-summary messages;
  session-summary title/objective/narrative text; and failed tool-call/tool-op error excerpts.
  Headers identify the class/type and cwd basename (or namespace if absent; summaries use project
  basename). Pattern-matched secret spans become [REDACTED]; other sensitive text is not scrubbed.
- Never holds the refresh lock across API calls: own singleton lock, skips while a refresh or a
  rebuild is running.
- Orphan vectors are garbage-collected by gc(): see the guards above it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

import psycopg

from . import embed_telemetry, telemetry

from .load import ADVISORY_LOCK

EMBED_LOCK = ADVISORY_LOCK + 2
TEXTFILE: Path | None = None   # Prometheus textfile output (optional)
DIMS = 1024
INPUT_VERSION = "2"  # bump on any change to scrub/chunk/header: a deliberate full re-embed
QUERY_INSTRUCTION = "Given a question about past AI coding-agent work, retrieve transcript passages that answer it"
EMBED_CLASSES = ("human_prompt", "queued_prompt", "assistant_text", "subagent_brief", "subagent_report",
                 "compaction_summary")
MAX_ATTEMPTS = 3
FILLER = {"ok", "okay", "yes", "no", "continue", "go", "go on", "thanks", "thank you", "proceed", "yep", "done"}

# --- secret redaction -----------------------------------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"\b(?:cfut|cfk|cfat)_[A-Za-z0-9]{20,}"),                       # Cloudflare tokens / keys
    re.compile(r"\bsk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_\-]{20,}"),             # OpenAI / Anthropic / OpenRouter
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),     # GitHub
    re.compile(r"\bglc_[A-Za-z0-9+/=_\-]{20,}"),                               # Grafana Cloud
    re.compile(r"\bglsa_[A-Za-z0-9_]{20,}"),                                    # Grafana service account
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),                              # AWS access key id
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),                           # Slack
    re.compile(r"\bnbt_[A-Za-z0-9]{8,}\.[A-Za-z0-9]{20,}"),                    # NetBox v2
    re.compile(r"\b(?:pk|rk)-(?:live-|test-)?[A-Za-z0-9_\-]{16,}"),            # Stripe-style
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                                  # Google API key
    re.compile(r"\b(?:hvs|hvb)\.[A-Za-z0-9_\-]{24,}"),                          # Vault / OpenBao
    re.compile(r"\btskey-[A-Za-z0-9\-]{10,}"),                                  # Tailscale
    re.compile(r"\bdt0[a-z]\d{2}\.[A-Z0-9]{24}\.[A-Z0-9]{64}\b"),               # Dynatrace
    re.compile(r"\bNRAK-[A-Z0-9]{27}\b"),                                       # New Relic
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:bearer|token|basic)\s+([A-Za-z0-9._\-+/=]{20,})"),
    # label: value / LABEL=value anywhere (YAML, compose lists, inline env, JSON); the label may be a
    # suffix of a longer name (DB_PASSWORD, refresh_token, MERAKI__API_KEY, secretText)
    re.compile(r"(?i)(?<![A-Za-z])(?:pass(?:word|wd|phrase)|pwd|secret(?:text)?|token|api[_-]?key|apikey|"
               r"access[_-]?key|private[_-]?key|client[_-]?secret|credential)[A-Za-z0-9_]*[\"']?\s*[:=]\s*"
               r"[\"']?([^\s\"',;}{)]{6,})"),
    re.compile(r"[A-Za-z0-9_.\-]{3}\dQ~[A-Za-z0-9_~.\-]{31,34}"),              # Azure / Entra client secret
    re.compile(r"(?<![A-Za-z0-9])s\.[A-Za-z0-9]{24}(?![A-Za-z0-9])"),          # legacy Vault / OpenBao token
    re.compile(r"(?<!\S)(?:-u|--user)\s+[\"']?[^\s:\"']+:([^\s\"']{4,})"),     # curl basic auth
    re.compile(r"://[^/\s:@]+:(\S+?)@[^/\s@]+(?=[/:\s?#]|$)"),                 # credentials in URLs
    re.compile(r"(?m)^(?:[A-Za-z][A-Za-z0-9.\-]*|\*|\d+\.\d+\.\d+\.\d+):(?:\d{2,5}|\*):[A-Za-z0-9_*\-]+:"
               r"[A-Za-z0-9_*\-]+:(\S{4,})$"),                                  # .pgpass lines
    # long base64 blobs: must mix upper, lower and digits and look random (see _random_enough)
    re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*[0-9])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])"
               r"[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
    re.compile(r"\b(?:[a-f0-9]{32}|[a-f0-9]{37}|[a-f0-9]{48,})\b"),              # hex keys (never 40: git shas)
]


BLOB = SECRET_PATTERNS[-2]


def _random_enough(token: str) -> bool:
    """Random base64 has short lowercase runs and several digits; CamelCase identifiers do not."""
    run = longest = digits = 0
    for ch in token:
        run = run + 1 if ch.islower() else 0
        longest = max(longest, run)
        digits += ch.isdigit()
    return longest <= 6 and digits >= 2


def scrub_spans(text: str) -> list[tuple[int, int]]:
    """Sorted, merged (start, end) spans of text that look like credentials."""
    spans = sorted((m.start(m.lastindex or 0), m.end(m.lastindex or 0))
                   for p in SECRET_PATTERNS for m in p.finditer(text)
                   if p is not BLOB or _random_enough(m.group(0)))
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def redact(text: str, spans: list[tuple[int, int]], offset: int = 0) -> str:
    out, pos = [], 0
    for s, e in spans:
        s, e = max(s - offset, 0), min(e - offset, len(text))
        if e <= 0 or s >= len(text):
            continue
        out.append(text[pos:s])
        out.append("[REDACTED]")
        pos = e
    out.append(text[pos:])
    return "".join(out)


# --- chunking -------------------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ~3 ASCII chars per token, 1.5 tokens per non-ASCII char."""
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_n / 3 + (len(text) - ascii_n) * 1.5)


BOUNDARIES = [re.compile(r"\n```[^\n]*\n"), re.compile(r"\n#{1,6} "), re.compile(r"\n\s*\n"),
              re.compile(r"\n"), re.compile(r"(?<=[.!?])\s"), re.compile(r"\s")]


def _fences(text: str) -> list[tuple[int, int]]:
    marks = [m.start() for m in re.finditer(r"(?m)^```", text)]
    return [(marks[i], marks[i + 1] + 3) for i in range(0, len(marks) - 1, 2)]


def chunk_spans(text: str, spans: list[tuple[int, int]], max_chars: int = 6000, overlap: int = 800,
                max_tokens: int = 3000) -> list[tuple[int, int]]:
    """Raw (start, end) chunk offsets; boundaries prefer fence/heading/paragraph breaks, never fall
    inside a redaction span or (when avoidable) a code fence."""
    n = len(text)
    if n <= max_chars and estimate_tokens(text) <= max_tokens:
        return [(0, n)]
    fences = _fences(text)

    def blocked(pos: int) -> bool:
        return any(s < pos < e for s, e in spans) or any(s < pos < e for s, e in fences)

    def unsecret(pos: int) -> int:
        for s, e in spans:
            if s < pos < e:
                return e
        return pos

    chunks: list[tuple[int, int]] = []
    start = 0
    while start < n:
        limit = min(start + max_chars, n)
        while limit > start + 200 and estimate_tokens(text[start:limit]) > max_tokens:
            limit = start + (limit - start) * 3 // 4
        if limit >= n:
            chunks.append((start, n))
            break
        floor = start + (limit - start) // 2
        cut = None
        for pattern in BOUNDARIES:
            for m in reversed(list(pattern.finditer(text, floor, limit))):
                if not blocked(m.end()):
                    cut = m.end()
                    break
            if cut:
                break
        if cut is None:
            cut = unsecret(limit)
        chunks.append((start, cut))
        nxt = max(cut - overlap, start + 1)
        # Overlap start must not land inside a secret span either.
        for s, e in spans:
            if s < nxt < e:
                nxt = s
        start = max(nxt, start + 1)
    return chunks


def header(message_class: str, cwd: str | None, namespace: str) -> str:
    name = PurePosixPath(cwd).name if cwd else ""
    return f"[{message_class}] {name or namespace}\n"


def build_input(message_class: str, cwd: str | None, namespace: str, text: str,
                spans: list[tuple[int, int]], start: int, end: int) -> str:
    inner = [(s, e) for s, e in spans if s < end and e > start]
    return header(message_class, cwd, namespace) + redact(text[start:end], inner, start)


def eligible(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    return len(stripped) >= 30 and stripped.lower().rstrip(".!") not in FILLER


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def normalise(vec: list[float]) -> list[float]:
    if len(vec) > DIMS:
        vec = vec[:DIMS]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def halfvec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in vec) + "]"


# --- providers ------------------------------------------------------------------------------------


FAILURE_REASONS = frozenset({"auth", "billing_quota", "rate_limit", "route", "provider_error", "network", "other"})
QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached", "billing_not_active", "quota_exceeded"})


def failure_reason(status: int, detail: str = "") -> str:
    """Only fixed structured provider codes distinguish quota from ordinary throttling."""
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "billing_quota"
    if status == 429:
        try:
            body = json.loads(detail)
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict) and any(isinstance(error.get(key), str) and error[key] in QUOTA_CODES
                                               for key in ("code", "type")):
                return "billing_quota"
        except (ValueError, TypeError):
            pass
        return "rate_limit"
    if status == 404:
        return "route"
    if status >= 500:
        return "provider_error"
    if status in (0, 408):
        return "network"
    return "other"


class ProviderError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.reason = reason if reason in FAILURE_REASONS else failure_reason(status, message)


@dataclass
class Provider:
    """An OpenAI-compatible embeddings endpoint: POST <base_url>/embeddings."""
    name: str
    model: str
    token: str
    base_url: str
    batch: int = 96
    dimensions: int = DIMS
    headers: dict = field(default_factory=dict)
    # Operator-trusted model identifiers ([metrics_labels] models); others export as `other`.
    trusted_models: frozenset = frozenset()
    usage: dict = field(default_factory=lambda: {"tokens": 0, "requests": 0})

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        data = json.dumps(body).encode()
        # Some gateways reject Python-urllib's default User-Agent.
        base = {"Content-Type": "application/json", "User-Agent": "agent-history-embed/1"}
        if self.token:
            base["Authorization"] = f"Bearer {self.token}"
        base.update(self.headers)
        base.update(headers)
        delay = 2.0
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, headers=base, method="POST")
            try:
                with embed_telemetry.attempt() as tel:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        tel.status(getattr(resp, "status", None))
                        self.usage["requests"] += 1
                        return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(65536).decode("utf-8", "replace")
                except (OSError, http.client.HTTPException):
                    # A broken error body must not hide the received status or bypass retries.
                    detail = ""
                if exc.code in (408, 429) or exc.code >= 500:
                    if attempt == 5:
                        raise ProviderError(exc.code, detail[:300], failure_reason(exc.code, detail)) from None
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise ProviderError(exc.code, detail[:300], failure_reason(exc.code, detail)) from None
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
                if attempt == 5:
                    raise ProviderError(0, str(exc)) from None
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise ProviderError(0, "unreachable")

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        with embed_telemetry.request(self.model, self.dimensions, self.trusted_models) as tel:
            body: dict = {"model": self.model, "input": texts}
            if self.dimensions:
                body["dimensions"] = self.dimensions
            out = self._post(self.base_url.rstrip("/") + "/embeddings", body, {})
            tel.response(out)
            vectors = [d["embedding"] for d in sorted(out.get("data") or [], key=lambda d: d["index"])]
            self.usage["tokens"] += int((out.get("usage") or {}).get("prompt_tokens") or 0)
            if len(vectors) != len(texts):
                raise ProviderError(502, f"expected {len(texts)} vectors, got {len(vectors)}")
            return [normalise(v) for v in vectors]


def provider_from_config(config=None) -> Provider:
    """The configured embedding provider; raises LookupError when embeddings are off."""
    from .config import load_config
    config = config or load_config()
    cfg = config.embedding
    if not cfg.enabled:
        raise LookupError("embeddings are off ([embedding] enabled = false)")
    return Provider("openai-compatible", cfg.model, cfg.token(), cfg.base_url, cfg.batch, cfg.dimensions,
                    dict(cfg.headers), frozenset(config.metrics_labels.models))


def embed_resilient(call: Callable[[list[str]], list[list[float]]], texts: list[str]
                    ) -> tuple[dict[str, list[float]], dict[str, str]]:
    """Embed texts; on a client error split the batch down to single inputs.
    Returns ({text: vector}, {failed text: status})."""
    good: dict[str, list[float]] = {}
    failed: dict[str, str] = {}
    stack = [list(texts)]
    while stack:
        batch = stack.pop()
        try:
            for text, vec in zip(batch, call(batch)):
                good[text] = vec
        except ProviderError as exc:
            if exc.status in (0, 401, 402, 403, 404, 408, 429) or exc.status >= 500:
                # credentials, billing, route, quota or an outage outlasting the retries: every input
                # would fail the same way. Stop the run; never count it against the inputs.
                raise
            if len(batch) == 1:
                for text in batch:
                    failed[text] = str(exc.status)
            else:
                mid = len(batch) // 2
                stack.extend([batch[mid:], batch[:mid]])
    return good, failed


# --- database -------------------------------------------------------------------------------------


@dataclass
class Item:
    kind: str                 # 'message' | 'summary' | 'tool_call' | 'tool_op'
    ref: int                  # message, summary, tool_call or tool_op id
    source_sha: str
    namespace: str
    session_id: int | None
    ts: datetime
    pieces: list[tuple[int, int, str, str]]   # (start, end, input, input_sha)


@dataclass
class EmbedStats:
    started: float = field(default_factory=time.time)
    items: int = 0
    chunks: int = 0
    api_inputs: int = 0
    cached: int = 0
    failed_inputs: int = 0
    tokens: int = 0
    skipped: str = ""
    gc: dict | None = None    # set when this run also garbage-collected orphan vectors (see gc())


MESSAGE_SQL = """
SELECT m.id, m.content_sha256, m.namespace, m.session_id, m.ts, m.message_class, s.cwd, m.text
FROM ah.message m
JOIN ah.session s ON s.id = m.session_id
WHERE m.message_class = ANY(%(classes)s) AND length(m.text) >= 30
  AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.message_id = m.id AND c.model = %(model)s
                  AND c.source_sha256 = m.content_sha256)
ORDER BY m.id DESC
LIMIT %(lim)s
"""

SUMMARY_SQL = """
SELECT ss.id, ss.journal_revision_id, ss.namespace, ss.session_id, ss.analysed_at, ss.project,
       concat_ws(E'\\n', ss.title, ss.objective, ss.narrative)
FROM ah.session_summary ss
WHERE NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.summary_id = ss.id AND c.model = %(model)s
                  AND c.source_sha256 = ss.journal_revision_id)
ORDER BY ss.id DESC
LIMIT %(lim)s
"""


# Failed tool calls/ops: the post-pass's error_excerpt (~500 chars), keyed by its sha256.
ERROR_SQL = {
    kind: f"""
SELECT x.id, encode(sha256(convert_to(x.error_excerpt, 'UTF8')), 'hex'), s.namespace, x.session_id,
       COALESCE({ts}), {name}, s.cwd, x.error_excerpt, x.error_class
FROM ah.{kind} x
JOIN ah.session s ON s.id = x.session_id
WHERE x.error_excerpt IS NOT NULL AND length(x.error_excerpt) >= 30
  AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.{kind}_id = x.id AND c.model = %(model)s
                  AND c.source_sha256 = encode(sha256(convert_to(x.error_excerpt, 'UTF8')), 'hex'))
ORDER BY x.id DESC
LIMIT %(lim)s
""" for kind, ts, name in (("tool_call", "x.started_at, x.ended_at, s.last_event_at", "x.tool_name"),
                            ("tool_op", "x.started_at, x.completed_at, s.last_event_at", "x.item_type"))}


def error_header(tool: str | None, error_class: str | None, cwd: str | None, namespace: str) -> str:
    return header(f"tool_error:{tool or 'unknown'}:{error_class or 'other'}", cwd, namespace)


def _items(conn: psycopg.Connection, model: str, lim: int) -> list[Item]:
    items: list[Item] = []
    for mid, csha, ns, sid, ts, cls, cwd, text in conn.execute(
            MESSAGE_SQL, {"classes": list(EMBED_CLASSES), "model": model, "lim": lim}):
        if not eligible(text):
            items.append(Item("message", mid, csha, ns, sid, ts, []))  # marker: nothing to embed
            continue
        spans = scrub_spans(text)
        pieces = []
        for s, e in chunk_spans(text, spans):
            inp = build_input(cls, cwd, ns, text, spans, s, e)
            pieces.append((s, e, inp, sha(inp)))
        items.append(Item("message", mid, csha, ns, sid, ts, pieces))
    for sid_, rev, ns, sess, ts, project, text in conn.execute(SUMMARY_SQL, {"model": model, "lim": lim}):
        if not eligible(text):
            items.append(Item("summary", sid_, rev, ns, sess, ts, []))  # marker: nothing to embed
            continue
        spans = scrub_spans(text)
        pieces = []
        for s, e in chunk_spans(text, spans):
            inp = header("session_summary", project, ns) + redact(text[s:e], spans, s)
            pieces.append((s, e, inp, sha(inp)))
        items.append(Item("summary", sid_, rev, ns, sess, ts, pieces))
    for kind, sql in ERROR_SQL.items():
        for ref, esha, ns, sess, ts, tool, cwd, text, cls in conn.execute(sql, {"model": model, "lim": lim}):
            spans = scrub_spans(text)
            pieces = []
            for s_, e in chunk_spans(text, spans):
                inp = error_header(tool, cls, cwd, ns) + redact(text[s_:e], spans, s_)
                pieces.append((s_, e, inp, sha(inp)))
            items.append(Item(kind, ref, esha, ns, sess, ts, pieces))
    conn.rollback()
    return items


def _cached(conn: psycopg.Connection, model: str, hashes: list[str]) -> set[str]:
    rows = conn.execute("SELECT input_sha256 FROM ah.embedding WHERE model = %s AND input_sha256 = ANY(%s)",
                        (model, hashes)).fetchall()
    return {r[0] for r in rows}


def _dead(conn: psycopg.Connection, model: str, hashes: list[str]) -> set[str]:
    rows = conn.execute("SELECT input_sha256 FROM ah.embed_failure WHERE model = %s AND input_sha256 = ANY(%s) "
                        "AND attempts >= %s", (model, hashes, MAX_ATTEMPTS)).fetchall()
    return {r[0] for r in rows}


def _write_chunks(conn: psycopg.Connection, model: str, item: Item) -> int:
    """Replace this item's chunks, guarded against a concurrent rebuild re-using the id."""
    with conn.transaction():
        if item.kind == "message":
            ok = conn.execute("SELECT 1 FROM ah.message WHERE id = %s AND content_sha256 = %s",
                              (item.ref, item.source_sha)).fetchone()
            col = "message_id"
        elif item.kind == "summary":
            ok = conn.execute("SELECT 1 FROM ah.session_summary WHERE id = %s AND journal_revision_id = %s",
                              (item.ref, item.source_sha)).fetchone()
            col = "summary_id"
        else:
            ok = conn.execute(f"SELECT 1 FROM ah.{item.kind} WHERE id = %s AND error_excerpt IS NOT NULL "
                              "AND encode(sha256(convert_to(error_excerpt, 'UTF8')), 'hex') = %s",
                              (item.ref, item.source_sha)).fetchone()
            col = f"{item.kind}_id"
        if not ok:
            return 0
        conn.execute(f"DELETE FROM ah.chunk WHERE {col} = %s AND model = %s", (item.ref, model))
        pieces = item.pieces or [(0, 0, "", "")]   # empty marker row so the message is not re-selected
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO ah.chunk ({col}, session_id, namespace, ts, chunk_no, char_start, char_end, "
                "source_sha256, model, input_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(item.ref, item.session_id, item.namespace, item.ts, i, s, e, item.source_sha, model, h)
                 for i, (s, e, _inp, h) in enumerate(pieces)])
    return len(item.pieces)


def gate(conn: psycopg.Connection) -> str | None:
    """Reason to skip this run, or None. Takes the embed singleton lock when returning None."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (EMBED_LOCK,)).fetchone()[0]:
        conn.commit()
        return "embed_running"
    busy = not conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,)).fetchone()[0]
    if not busy:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
    # A rebuild killed before its finally leaves the flag behind: ignore it after 3 hours.
    rebuilding = conn.execute(
        "SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress' "
        "AND value::timestamptz > now() - interval '3 hours'").fetchone()
    conn.commit()
    if busy or rebuilding:
        conn.execute("SELECT pg_advisory_unlock(%s)", (EMBED_LOCK,))
        conn.commit()
        return "refresh_running" if busy else "rebuild_in_progress"
    return None


@telemetry.instrument_pass("embed.pass")
def run(conn: psycopg.Connection, provider: Provider, cap_tokens: int = 3_000_000,
        daily_cap: int = 30_000_000, batch_items: int = 2000, log=print,
        gc_interval_hours: float | None = None) -> EmbedStats:
    """Embed pending chunks. With gc_interval_hours, also run gc() after the pass, still under the
    embed lock, when the last completed GC (meta.embed_gc_at) is older than that."""
    stats = EmbedStats()
    reason = gate(conn)
    if reason:
        stats.skipped = reason
        return stats
    try:
        active = conn.execute("SELECT value FROM ah.meta WHERE key = 'embedding_model'").fetchone()
        if active and active[0] != provider.model:
            raise SystemExit(f"meta.embedding_model is {active[0]}, provider model {provider.model}: "
                             "switch models deliberately")
        conn.execute("INSERT INTO ah.meta VALUES ('embedding_model', %s) ON CONFLICT (key) DO NOTHING",
                     (provider.model,))
        version = conn.execute("SELECT value FROM ah.meta WHERE key = 'embed_input_version'").fetchone()
        if not version or version[0] != INPUT_VERSION:
            # scrub/chunk/header changed: re-chunk everything. Unchanged inputs hit the cache, so only
            # inputs whose text actually changed are sent again.
            conn.execute("DELETE FROM ah.chunk WHERE model = %s", (provider.model,))
            conn.execute("INSERT INTO ah.meta VALUES ('embed_input_version', %s) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (INPUT_VERSION,))
            mark_chunks_reset(conn)   # same transaction as the DELETE: embed-gc waits 7 days
            log(f"embed: input version {version[0] if version else None} -> {INPUT_VERSION}, re-chunking")
        conn.commit()
        day_key = "embed_tokens_" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute("SELECT value FROM ah.meta WHERE key = %s", (day_key,)).fetchone()
        used_today = int(row[0]) if row else 0
        conn.commit()
        while True:
            if cap_tokens and stats.tokens >= cap_tokens:
                break
            if daily_cap and used_today + stats.tokens >= daily_cap:
                stats.skipped = "daily_cap"
                break
            items = _items(conn, provider.model, batch_items)
            if not items:
                break
            hashes = sorted({h for it in items for (_s, _e, _i, h) in it.pieces})
            have = _cached(conn, provider.model, hashes) | _dead(conn, provider.model, hashes)
            conn.rollback()
            todo: dict[str, str] = {}
            for it in items:
                for _s, _e, inp, h in it.pieces:
                    if h not in have:
                        todo.setdefault(h, inp)
            stats.cached += len(_cached(conn, provider.model, hashes))
            conn.rollback()
            failed_now: set[str] = set()
            pending = list(todo.items())
            before = provider.usage["tokens"]
            for i in range(0, len(pending), provider.batch):
                part = pending[i:i + provider.batch]
                good, failed = embed_resilient(lambda texts: provider.embed(texts), [inp for _h, inp in part])
                by_input = {inp: h for h, inp in part}
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO ah.embedding (model, input_sha256, embedding, input_tokens) "
                            "VALUES (%s, %s, %s::halfvec, %s) ON CONFLICT DO NOTHING",
                            [(provider.model, by_input[inp], halfvec_literal(vec), estimate_tokens(inp))
                             for inp, vec in good.items()])
                        cur.executemany(
                            "INSERT INTO ah.embed_failure (model, input_sha256, attempts, last_error) "
                            "VALUES (%s, %s, 1, %s) ON CONFLICT (model, input_sha256) DO UPDATE SET "
                            "attempts = ah.embed_failure.attempts + 1, last_error = EXCLUDED.last_error, "
                            "last_at = now()",
                            [(provider.model, by_input[inp], status) for inp, status in failed.items()])
                stats.api_inputs += len(good)
                stats.failed_inputs += len(failed)
                failed_now |= {by_input[inp] for inp in failed}
            stats.tokens += provider.usage["tokens"] - before
            dead = _dead(conn, provider.model, hashes)
            conn.rollback()
            progressed = 0
            for it in items:
                # Write chunks once every piece is embedded or permanently failed; retry the rest later.
                if any(h in failed_now and h not in dead for _s, _e, _i, h in it.pieces):
                    continue
                stats.chunks += _write_chunks(conn, provider.model, it)
                stats.items += 1
                progressed += 1
            conn.execute("INSERT INTO ah.meta VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         (day_key, str(used_today + stats.tokens)))
            conn.commit()
            log(f"embed: items={stats.items} chunks={stats.chunks} api={stats.api_inputs} cached={stats.cached} "
                f"failed={stats.failed_inputs} tokens~{stats.tokens}")
            if progressed == 0:
                break
        if gc_interval_hours:
            last = conn.execute("SELECT now(), (SELECT value FROM ah.meta WHERE key = 'embed_gc_at')").fetchone()
            conn.rollback()
            if gc_due(last[0], last[1], gc_interval_hours):
                try:
                    result = gc(conn, log=log, locked=True)
                except psycopg.Error as exc:
                    # A failing GC must not fail the embed run (and its stale alert) every ten
                    # minutes: report it in the gc metrics and try again after the interval.
                    conn.rollback()
                    log(f"embed-gc: failed ({type(exc).__name__}); next attempt in {gc_interval_hours} h")
                    conn.execute("INSERT INTO ah.meta VALUES ('embed_gc_at', now()::text) "
                                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
                    conn.commit()
                    result = GcStats(skipped="error")
                stats.gc = gc_stats_dict(result)
        conn.execute("DELETE FROM ah.meta WHERE key = 'embed_last_failure_reason'")
        conn.commit()
    except Exception as exc:
        # The CLI creates fresh stats on failure, so retain only the bounded reason in the catalogue.
        failure = exc.reason if isinstance(exc, ProviderError) else "other"
        try:
            conn.rollback()
            conn.execute("INSERT INTO ah.meta VALUES ('embed_last_failure_reason', %s) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (failure,))
            conn.commit()
        except psycopg.Error:
            conn.rollback()
        raise
    finally:
        try:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (EMBED_LOCK,))
            conn.commit()
        except psycopg.Error:
            conn.rollback()
    return stats


def write_metrics(conn: psycopg.Connection, stats: EmbedStats, success: bool, path: Path | None = TEXTFILE) -> None:
    lines = [f"agent_history_embed_run_success {int(success)}",
             f"agent_history_embed_run_duration_seconds {time.time() - stats.started:.3f}",
             f"agent_history_embed_run_items {stats.items}",
             f"agent_history_embed_run_chunks {stats.chunks}",
             f"agent_history_embed_run_api_inputs {stats.api_inputs}",
             f"agent_history_embed_run_cached_inputs {stats.cached}",
             f"agent_history_embed_run_tokens {stats.tokens}",
             f'agent_history_embed_run_skipped{{reason="{stats.skipped or "none"}"}} 1']
    prev = None
    try:
        for line in path.read_text().splitlines():
            if line.startswith("agent_history_embed_last_success_timestamp_seconds "):
                prev = float(line.split()[1])
    except (OSError, ValueError):
        pass
    last = time.time() if success and stats.skipped in ("", "daily_cap") else prev
    if last:
        lines.append(f"agent_history_embed_last_success_timestamp_seconds {last:.0f}")
    # GC runs once a day; the ten-minute runs in between carry its last result forward.
    lines += gc_metric_lines(stats.gc) if stats.gc else _previous_gc_lines(path)
    try:
        if not success:
            failure = conn.execute("SELECT value FROM ah.meta WHERE key = 'embed_last_failure_reason'").fetchone()
            reason = failure[0] if failure and failure[0] in FAILURE_REASONS else "other"
            lines.append(f'agent_history_embed_last_failure_reason{{reason="{reason}"}} 1')
        model = conn.execute("SELECT value FROM ah.meta WHERE key = 'embedding_model'").fetchone()
        if model:
            pending = conn.execute(
                "SELECT count(*) FROM ah.message m WHERE m.message_class = ANY(%s) AND length(m.text) >= 30 "
                "AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.message_id = m.id AND c.model = %s "
                "AND c.source_sha256 = m.content_sha256)", (list(EMBED_CLASSES), model[0])).fetchone()[0]
            failed = conn.execute("SELECT count(*) FROM ah.embed_failure WHERE model = %s AND attempts >= %s",
                                  (model[0], MAX_ATTEMPTS)).fetchone()[0]
            vectors = conn.execute("SELECT count(*) FROM ah.embedding WHERE model = %s", (model[0],)).fetchone()[0]
            lines += [f"agent_history_embed_pending_messages {pending}",
                      f"agent_history_embed_failed_inputs {failed}",
                      f"agent_history_embed_vectors {vectors}"]
        conn.rollback()
    except psycopg.Error:
        conn.rollback()
    if path.parent.is_dir():
        tmp = path.with_suffix(".prom.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)


# --- orphan vector GC ----------------------------------------------
#
# ah.embedding is a paid cache: a vector is only garbage once nothing can ask for it again. A rebuild
# or an INPUT_VERSION re-chunk empties ah.chunk and refills it gradually, so for a while almost every
# vector looks orphaned. GC therefore runs only when all of these hold, else it skips with a reason:
#   - it holds the embed lock (EMBED_LOCK) and, for the one short transaction, the refresh/rebuild
#     lock (ADVISORY_LOCK), so no rebuild can empty ah.chunk and no refresh add messages meanwhile;
#   - meta.rebuild_in_progress is absent (at any age: a killed rebuild blocks GC until cleared);
#   - meta.chunks_reset_at is present, parses, and is at least 7 days old;
#   - the embed backlog is drained (every embeddable message and summary has its chunks);
# and it deletes at most max_rows vectors of the active model older than 30 days with no chunk.

GC_MIN_AGE_DAYS = 30
GC_RESET_QUIET_DAYS = 7
GC_MAX_ROWS = 50_000
GC_INTERVAL_HOURS = 24

GC_PREDICATE = """
FROM ah.embedding e
WHERE e.model = %(model)s
  AND e.created_at < now() - make_interval(days => %(min_age_days)s)
  AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.model = e.model AND c.input_sha256 = e.input_sha256)
"""
GC_COUNT_SQL = "SELECT count(*)" + GC_PREDICATE
GC_DELETE_SQL = ("DELETE FROM ah.embedding d USING (SELECT e.model, e.input_sha256" + GC_PREDICATE
                 + "LIMIT %(max_rows)s) victim\n"
                 "WHERE d.model = victim.model AND d.input_sha256 = victim.input_sha256")
BACKLOG_SQL = """
SELECT EXISTS (
    SELECT 1 FROM ah.message m
    WHERE m.message_class = ANY(%(classes)s) AND length(m.text) >= 30
      AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.message_id = m.id AND c.model = %(model)s
                      AND c.source_sha256 = m.content_sha256))
    OR EXISTS (
    SELECT 1 FROM ah.session_summary ss
    WHERE NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.summary_id = ss.id AND c.model = %(model)s
                      AND c.source_sha256 = ss.journal_revision_id))
    OR EXISTS (
    SELECT 1 FROM ah.tool_call x WHERE x.error_excerpt IS NOT NULL AND length(x.error_excerpt) >= 30
      AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.tool_call_id = x.id AND c.model = %(model)s))
    OR EXISTS (
    SELECT 1 FROM ah.tool_op x WHERE x.error_excerpt IS NOT NULL AND length(x.error_excerpt) >= 30
      AND NOT EXISTS (SELECT 1 FROM ah.chunk c WHERE c.tool_op_id = x.id AND c.model = %(model)s))
"""


@dataclass
class GcStats:
    dry_run: bool = False
    eligible: int = 0
    deleted: int = 0
    skipped: str = ""
    at: float = field(default_factory=time.time)


def gc_stats_dict(g: GcStats) -> dict:
    return {"dry_run": g.dry_run, "eligible": g.eligible, "deleted": g.deleted, "skipped": g.skipped, "at": g.at}


def _parse_meta_time(value: str | None) -> datetime | None:
    """ah.meta times are now()::text ('2026-09-26 12:00:00.12+00'); None when missing or unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def gc_skip_reason(now: datetime, chunks_reset_at: str | None, rebuild_in_progress: bool,
                   backlog_pending: bool) -> str | None:
    """Why GC must not delete anything now, or None when it may."""
    if rebuild_in_progress:
        return "rebuild_in_progress"
    if chunks_reset_at is None:
        return "no_reset_marker"
    reset = _parse_meta_time(chunks_reset_at)
    if reset is None:
        return "bad_reset_marker"
    if now - reset < timedelta(days=GC_RESET_QUIET_DAYS):
        return "recent_chunk_reset"
    if backlog_pending:
        return "backlog_pending"
    return None


def gc_due(now: datetime, last_gc_at: str | None, interval_hours: float) -> bool:
    last = _parse_meta_time(last_gc_at)
    return last is None or now - last >= timedelta(hours=interval_hours)


def mark_chunks_reset(conn: psycopg.Connection) -> None:
    """Record that ah.chunk was emptied (rebuild or re-chunk). Caller owns the transaction."""
    conn.execute("INSERT INTO ah.meta VALUES ('chunks_reset_at', now()::text) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")


def gc(conn: psycopg.Connection, dry_run: bool = False, max_rows: int = GC_MAX_ROWS, log=print,
       locked: bool = False) -> GcStats:
    """Delete (or with dry_run only count) orphan vectors under the guards above.

    locked=True means the caller already holds EMBED_LOCK (embed.run); otherwise gc takes it through
    gate() and releases it. A completed non-dry run, including a guard skip, records meta.embed_gc_at;
    a lock-contention skip does not, so the next ten-minute embed run tries again.
    """
    stats = GcStats(dry_run=dry_run)
    if not locked:
        reason = gate(conn)
        if reason:
            stats.skipped = reason
            log(f"embed-gc: skipped ({reason})")
            return stats
    try:
        with conn.transaction():
            # Held to commit: a rebuild cannot empty ah.chunk and a refresh cannot add messages mid-GC.
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (ADVISORY_LOCK,)).fetchone()[0]:
                stats.skipped = "refresh_running"
            else:
                conn.execute("SET LOCAL statement_timeout = '15min'")
                now, model, reset, rebuilding = conn.execute(
                    "SELECT now(), (SELECT value FROM ah.meta WHERE key = 'embedding_model'), "
                    "(SELECT value FROM ah.meta WHERE key = 'chunks_reset_at'), "
                    "EXISTS (SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress')").fetchone()
                if not model:
                    stats.skipped = "no_model"
                else:
                    pending = conn.execute(BACKLOG_SQL, {"classes": list(EMBED_CLASSES), "model": model}
                                           ).fetchone()[0]
                    stats.skipped = gc_skip_reason(now, reset, rebuilding, pending) or ""
                    if not stats.skipped:
                        params = {"model": model, "min_age_days": GC_MIN_AGE_DAYS, "max_rows": max_rows}
                        stats.eligible = conn.execute(GC_COUNT_SQL, params).fetchone()[0]
                        if not dry_run and stats.eligible:
                            stats.deleted = conn.execute(GC_DELETE_SQL, params).rowcount
                if not dry_run and stats.skipped != "refresh_running":
                    conn.execute("INSERT INTO ah.meta VALUES ('embed_gc_at', now()::text) "
                                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    finally:
        if not locked:
            try:
                conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(%s)", (EMBED_LOCK,))
                conn.commit()
            except psycopg.Error:
                conn.rollback()
    mode = "dry-run " if dry_run else ""
    log(f"embed-gc: {mode}skipped ({stats.skipped})" if stats.skipped else
        f"embed-gc: {mode}eligible={stats.eligible} deleted={stats.deleted} cap={max_rows}")
    return stats


GC_METRIC_PREFIX = "agent_history_embed_gc_"


def gc_metric_lines(g: dict) -> list[str]:
    return [f"{GC_METRIC_PREFIX}last_run_timestamp_seconds {g['at']:.0f}",
            f"{GC_METRIC_PREFIX}dry_run {int(g['dry_run'])}",
            f"{GC_METRIC_PREFIX}eligible {g['eligible']}",
            f"{GC_METRIC_PREFIX}deleted {g['deleted']}",
            f'{GC_METRIC_PREFIX}skipped{{reason="{g["skipped"] or "none"}"}} 1']


def _previous_gc_lines(path: Path) -> list[str]:
    try:
        return [line for line in path.read_text().splitlines() if line.startswith(GC_METRIC_PREFIX)]
    except OSError:
        return []


def write_gc_metrics(g: dict, path: Path = TEXTFILE) -> None:
    """Replace only the GC lines of the embed textfile (standalone `embed-gc`)."""
    if not path.parent.is_dir():
        return
    try:
        kept = [line for line in path.read_text().splitlines() if not line.startswith(GC_METRIC_PREFIX)]
    except OSError:
        kept = []
    tmp = path.with_suffix(".prom.tmp")
    tmp.write_text("\n".join(kept + gc_metric_lines(g)) + "\n")
    os.replace(tmp, path)


def create_vector_index(conn: psycopg.Connection) -> None:
    conn.autocommit = True
    try:
        conn.execute("SET maintenance_work_mem = '3GB'")
        conn.execute("SET max_parallel_maintenance_workers = 4")
        conn.execute("CREATE INDEX IF NOT EXISTS embedding_hnsw_idx ON ah.embedding "
                     "USING hnsw (embedding halfvec_cosine_ops)")
        conn.execute("ANALYZE ah.embedding")
        conn.execute("ANALYZE ah.chunk")
    finally:
        conn.autocommit = False


def query_vector(text: str, provider: Provider | None = None) -> str:
    provider = provider or provider_from_config()
    return halfvec_literal(provider.embed([text[:12000]], kind="query")[0])
