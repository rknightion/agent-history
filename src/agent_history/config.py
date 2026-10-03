"""Configuration: database, transcript sources, contexts, owner identities and embeddings.

One TOML file, `$AGENT_HISTORY_CONFIG` or `~/.config/agent-history/config.toml`. Every key is
optional; see config.example.toml. Without a file the indexer reads the three default agent homes:

    claude-local = ~/.claude      codex-local = ~/.codex      pi-local = ~/.pi/agent

A namespace is `<agent>-<profile>`: the agent (claude, codex or pi) picks the parser, the profile is
a free label. Contexts are named search scopes over namespaces, not access-control boundaries: a
reader can query every row through the `sql` tool. Use separate databases for real separation;
reader roles on shared tables do not isolate rows without row-level security.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SOURCES = {"claude-local": "~/.claude", "codex-local": "~/.codex", "pi-local": "~/.pi/agent"}
NAMESPACE = re.compile(r"^(claude|codex|pi)-[a-z0-9][a-z0-9_-]{0,62}$")
DIMENSIONS = 1024  # ah.embedding is halfvec(1024)


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    explicit = os.environ.get("AGENT_HISTORY_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "agent-history" / "config.toml"


def _secret(value: str | None, file: str | None, env: str | None, what: str) -> str | None:
    """A value given inline, in a file (first line) or in an environment variable."""
    if env and os.environ.get(env):
        return os.environ[env]
    if file:
        path = Path(file).expanduser()
        try:
            return path.read_text().strip().splitlines()[0].strip()
        except (OSError, IndexError) as exc:
            raise ConfigError(f"{what}: cannot read {path}") from exc
    return value


@dataclass
class Embedding:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "text-embedding-3-large"
    dimensions: int = DIMENSIONS
    batch: int = 96
    api_key_env: str | None = "OPENAI_API_KEY"
    api_key_file: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def token(self) -> str:
        return _secret(None, self.api_key_file, self.api_key_env, "embedding api key") or ""


@dataclass
class Identities:
    owner_emails: frozenset[str] = frozenset()
    git_owners: frozenset[str] = frozenset()  # "host/owner", e.g. "github.com/example-org"

    def is_owner(self, email: str) -> bool:
        return email.strip().lower() in self.owner_emails


# Efficiency collector configuration (lane C): independent of catalogue ingestion.
@dataclass
class Efficiency:
    baseline_ts: float = 0.0
    loop_dsn: str | None = None
    first_parse_days: int = 3
    workflow_transcripts: bool = True


# Metric collection settings, run by the periodic indexer. The table keeps its historical name
# `[exporter]`. All roots are optional; no private paths are defaults.
@dataclass
class Exporter:
    refresh_interval: float = 15.0
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/state/agent-history")
    collectors: tuple[str, ...] = ("archive", "catalogue", "runs", "efficiency", "self")
    hot: Path | None = None
    cold: Path | None = None
    incoming: Path | None = None
    conflicts: Path | None = None


@dataclass(frozen=True)
class MetricsLabels:
    """Explicit trust for verbatim labels. Malformed input grants no trust."""

    machines: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()


def _metrics_labels(value: Any) -> MetricsLabels:
    if not isinstance(value, dict) or set(value) - {"machines", "models"}:
        return MetricsLabels()
    for key, items in value.items():
        if not isinstance(items, list) or not all(
            isinstance(item, str)
            and item
            and len(item) <= 128
            and not any(ord(char) < 32 or ord(char) == 127 for char in item)
            and (key != "machines" or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", item))
            for item in items
        ):
            return MetricsLabels()
    return MetricsLabels(frozenset(value.get("machines", [])), frozenset(value.get("models", [])))


@dataclass
class CollectorConfig:
    machine: str | None = None
    homes: dict[str, str] = field(default_factory=dict)
    repo_contexts: dict[str, str] = field(default_factory=dict)
    lock_file: Path = field(default_factory=lambda: Path.home() / ".local/state/agent-history/collect.lock")
    journal_db: Path | None = None


@dataclass
class Config:
    dsn: str | None = None
    reader_dsn: str | None = None
    sources: dict[str, Path] = field(default_factory=dict)
    cold_sources: dict[str, Path] = field(default_factory=dict)
    contexts: dict[str, list[str]] = field(default_factory=dict)
    default_context: str = "default"
    identities: Identities = field(default_factory=Identities)
    git_repos: list[Path] = field(default_factory=list)
    git_days: int = 180
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    embedding: Embedding = field(default_factory=Embedding)
    efficiency: Efficiency = field(default_factory=Efficiency)
    exporter: Exporter = field(default_factory=Exporter)
    metrics_labels: MetricsLabels = field(default_factory=MetricsLabels)
    path: Path | None = None

    def namespaces(self, context: str | None = None) -> list[str]:
        name = context or self.default_context
        if name not in self.contexts:
            raise ConfigError(f"unknown context {name!r} (known: {', '.join(sorted(self.contexts))})")
        return list(self.contexts[name])


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def parse_config(data: dict[str, Any], path: Path | None = None) -> Config:
    known = {
        "dsn",
        "dsn_file",
        "reader_dsn",
        "reader_dsn_file",
        "sources",
        "cold_sources",
        "contexts",
        "default_context",
        "identities",
        "git",
        "collector",
        "embedding",
        "efficiency",  # lane C
        "exporter",
        "metrics_labels",
    }
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    sources_raw = _table(data, "sources") if "sources" in data else dict(DEFAULT_SOURCES)
    sources: dict[str, Path] = {}
    for namespace, directory in sources_raw.items():
        if not NAMESPACE.match(namespace):
            raise ConfigError("sources namespace key must look like claude-<name>, codex-<name> or pi-<name>")
        if not isinstance(directory, str):
            raise ConfigError(f"source {namespace}: directory must be a string")
        # The configured home itself may be a symlink (resolved here); symlinked transcript files
        # inside it are still skipped by the loader.
        sources[namespace] = Path(directory).expanduser().resolve()
    cold_sources: dict[str, Path] = {}
    for namespace, directory in _table(data, "cold_sources").items():
        if namespace not in sources or not isinstance(directory, str) or not directory:
            raise ConfigError("cold_sources must map configured source namespaces to directory strings")
        cold_sources[namespace] = Path(directory).expanduser().resolve()
    contexts_raw = _table(data, "contexts")
    contexts: dict[str, list[str]] = {}
    for name, namespaces in contexts_raw.items():
        if not isinstance(namespaces, list) or not all(isinstance(n, str) and NAMESPACE.match(n) for n in namespaces):
            raise ConfigError(f"context {name!r} must be a list of namespaces")
        contexts[name] = list(namespaces)
    if not contexts:
        contexts = {"default": sorted(sources)}
    default_context = data.get("default_context") or ("default" if "default" in contexts else sorted(contexts)[0])
    if default_context not in contexts:
        raise ConfigError(f"default_context {default_context!r} is not a context")
    ident = _table(data, "identities")
    identities = Identities(
        owner_emails=frozenset(e.strip().lower() for e in ident.get("owner_emails", [])),
        git_owners=frozenset(o.strip().lower().rstrip("/") for o in ident.get("git_owners", [])),
    )
    git = _table(data, "git")
    col = _table(data, "collector")
    allowed_collector = {"machine", "homes", "repo_contexts", "lock_file", "journal_db"}
    if set(col) - allowed_collector:
        raise ConfigError(f"unknown collector keys: {', '.join(sorted(set(col) - allowed_collector))}")
    homes = col.get("homes", {})
    repo_contexts = col.get("repo_contexts", {})
    if not isinstance(homes, dict) or not all(
        isinstance(p, str) and isinstance(n, str) and NAMESPACE.match(n) for p, n in homes.items()
    ):
        raise ConfigError("collector.homes must map home paths to namespaces")
    if not isinstance(repo_contexts, dict) or not all(
        isinstance(p, str) and isinstance(n, str) for p, n in repo_contexts.items()
    ):
        raise ConfigError("collector.repo_contexts must map repo slugs to contexts")
    collector = CollectorConfig(
        machine=col.get("machine"),
        homes=homes,
        repo_contexts={p.lower(): n for p, n in repo_contexts.items()},
        lock_file=Path(col.get("lock_file", Path.home() / ".local/state/agent-history/collect.lock")).expanduser(),
        journal_db=Path(col["journal_db"]).expanduser() if col.get("journal_db") else None,
    )
    emb = _table(data, "embedding")
    # Efficiency collector configuration (lane C).
    eff = _table(data, "efficiency")
    unexpected = set(eff) - {"baseline_ts", "loop_dsn", "first_parse_days", "workflow_transcripts"}
    if unexpected:
        raise ConfigError(f"unknown efficiency keys: {', '.join(sorted(unexpected))}")
    efficiency = Efficiency(
        baseline_ts=float(eff.get("baseline_ts", 0.0)),
        loop_dsn=eff.get("loop_dsn"),
        first_parse_days=int(eff.get("first_parse_days", 3)),
        workflow_transcripts=eff.get("workflow_transcripts", True),
    )
    if not isinstance(efficiency.workflow_transcripts, bool):
        raise ConfigError("efficiency workflow_transcripts must be true or false")
    if efficiency.first_parse_days < 0 or efficiency.baseline_ts < 0:
        raise ConfigError("efficiency baseline and first_parse_days must be non-negative")
    embedding = Embedding(
        enabled=bool(emb.get("enabled", False)),
        base_url=str(emb.get("base_url", Embedding.base_url)),
        model=str(emb.get("model", Embedding.model)),
        dimensions=int(emb.get("dimensions", DIMENSIONS)),
        batch=int(emb.get("batch", 96)),
        api_key_env=emb.get("api_key_env", "OPENAI_API_KEY"),
        api_key_file=emb.get("api_key_file"),
        headers={str(k): str(v) for k, v in (emb.get("headers") or {}).items()},
    )
    # Metric collection: reject misspellings and unsafe collector selectors. `listen` is accepted and
    # ignored so a config written for the retired HTTP exporter still loads.
    exp = _table(data, "exporter")
    allowed = {"listen", "refresh_interval", "state_dir", "collectors", "hot", "cold", "incoming", "conflicts"}
    if set(exp) - allowed:
        raise ConfigError(f"unknown [exporter] keys: {', '.join(sorted(set(exp) - allowed))}")
    selected = exp.get("collectors", ["archive", "catalogue", "runs", "efficiency", "self"])
    if (
        not isinstance(selected, list)
        or len(set(selected)) != len(selected)
        or not set(selected) <= {"archive", "catalogue", "runs", "self", "efficiency"}
    ):
        raise ConfigError("exporter.collectors must be unique names: archive, catalogue, runs, self, efficiency")
    import math

    try:
        interval = float(exp.get("refresh_interval", 15))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError("exporter.refresh_interval must be finite and positive") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise ConfigError("exporter.refresh_interval must be finite and positive")
    exporter = Exporter(
        refresh_interval=interval,
        state_dir=Path(exp.get("state_dir", Path.home() / ".local/state/agent-history")).expanduser(),
        collectors=tuple(selected),
        **{
            key: Path(exp[key]).expanduser() if exp.get(key) else None
            for key in ("hot", "cold", "incoming", "conflicts")
        },
    )
    if embedding.dimensions != DIMENSIONS:
        raise ConfigError(f"embedding.dimensions must be {DIMENSIONS} (the ah.embedding column type)")
    return Config(
        dsn=_secret(data.get("dsn"), data.get("dsn_file"), None, "dsn"),
        reader_dsn=_secret(data.get("reader_dsn"), data.get("reader_dsn_file"), None, "reader_dsn"),
        sources=sources,
        cold_sources=cold_sources,
        contexts=contexts,
        default_context=default_context,
        identities=identities,
        git_repos=[Path(p).expanduser() for p in git.get("repos", [])],
        git_days=int(git.get("days", 180)),
        collector=collector,
        embedding=embedding,
        efficiency=efficiency,
        exporter=exporter,
        metrics_labels=_metrics_labels(data.get("metrics_labels")),
        path=path,
    )


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return parse_config({}, None)
    with path.open("rb") as handle:
        return parse_config(tomllib.load(handle), path)
