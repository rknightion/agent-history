"""Metadata-only checkout warnings over the existing explicit transcript inventory.

The hourly collector observes local checkouts and publishes its own OTLP snapshot. It does
not depend on the indexer's filesystem or introduce a catalogue persistence/privilege seam.
No checkout documents are read and no checkout is enrolled.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import tomllib
from dataclasses import replace
from pathlib import Path

from agent_history.collect_git import GIT_ENV, repo_admitted, repo_slug
from agent_history.efficiency import parser
from agent_history.loop_launch import recognize_inline
from agent_history.metrics import Family, Sample

WARNING = "unconfigured_allowlisted_checkout"
MAX_BYTES = 4 * 1024 * 1024
MAX_RECORD = 1024 * 1024
MAX_FILES = 64
MAX_GIT = 64
BUDGET_SECONDS = 5
MAX_AGE = 3600
MAX_INVENTORY_ENTRIES = 8192
MAX_INVENTORY_DIRS = 256
MAX_INVENTORY_FILES = 1024
MAX_INVENTORY_MEMORY = 1024 * 1024
MAX_CACHED_FILES = 2048


def local_config(config):
    """Only actual file/table declarations authorise discovery, never parsed defaults.

    A missing file, absent path, unreadable file or missing source/home tables is unavailable.
    In-memory Config values alone do not establish provenance, even if they resemble defaults.
    """
    sources = {}
    if config.path is None:
        return replace(config, sources=sources)
    try:
        with config.path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return replace(config, sources=sources)
    homes = data.get("collector", {}).get("homes", {})
    for home, namespace in homes.items():
        sources[namespace] = Path(home).expanduser()
    for namespace, home in data.get("sources", {}).items():
        sources[namespace] = Path(home).expanduser()
    return replace(config, sources=sources)


def fingerprint(config):
    config = local_config(config)
    value = [
        sorted((key, str(path.absolute())) for key, path in config.sources.items()),
        sorted(str(path.absolute()) for path in config.git_repos),
        sorted(config.identities.git_owners),
        sorted(config.identities.git_repos),
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _record(entry, agent, record):
    """Only original user launch text supplies eligibility; never tool outputs or child briefs."""
    kind = record.get("type")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if agent == "pi" and kind == "session":
        entry["cwd"] = record.get("cwd")
        entry["child"] = bool(record.get("parentSession"))
    elif agent == "codex" and kind == "session_meta":
        entry["cwd"] = payload.get("cwd")
        source = payload.get("source")
        entry["child"] = isinstance(source, dict) and bool(source.get("subagent"))
    elif agent == "codex" and kind == "turn_context":
        entry["cwd"] = payload.get("cwd", entry.get("cwd"))
    elif agent == "claude":
        entry["cwd"] = record.get("cwd", entry.get("cwd"))
        entry["child"] = bool(record.get("agentId")) or entry.get("child", False)
    text = None
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    if agent == "pi" and kind == "message" and message.get("role") == "user":
        content = message.get("content")
        text = content if isinstance(content, str) else parser.efficiency_block_text(content)
    elif agent == "codex" and kind == "event_msg" and payload.get("type") == "user_message":
        text = payload.get("message")
    elif agent == "codex" and kind == "response_item" and payload.get("role") == "user":
        if parser.efficiency_codex_human_item(payload.get("content")):
            text = parser.efficiency_codex_human_text(payload.get("content"))
    elif agent == "claude" and kind == "user":
        content = message.get("content")
        results = isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
        if not results:
            candidate = content if isinstance(content, str) else parser.efficiency_block_text(content)
            if parser.efficiency_claude_human(record, candidate):
                text = candidate
    if isinstance(text, str) and not entry.get("child"):
        launch = recognize_inline(text, entry.get("cwd"))
        if launch and Path(launch["report"]).name.startswith("closeout-"):
            cwd = entry.get("cwd")
            if isinstance(cwd, str) and Path(cwd).is_absolute():
                entry["root_cwd"] = cwd
            else:
                entry["unknown_cwd"] = True


def observe(
    config,
    sources,
    state,
    *,
    monotonic=time.monotonic,
    deadline=None,
    inventory_complete=True,
    inventory_errors=(),
):
    """Consume at most 4 MiB / 64 files / 5 seconds, resuming saved metadata-only offsets.

    The existing efficiency inventory is the sole discovery boundary. Record bodies are
    discarded immediately. Oversized/torn records and unvisited inputs leave a partial result.
    Git reads share that time budget, with at most 64 cwd probes and one second per subprocess.
    """
    if deadline is None:
        deadline = monotonic() + BUDGET_SECONDS
    entries = state.setdefault("checkout_files", {})
    if inventory_complete:
        for key in list(entries):
            if key not in sources:
                del entries[key]
    remaining = MAX_BYTES
    touched = 0
    complete = inventory_complete and not inventory_errors
    errors = set(inventory_errors)
    ordered = sorted(sources.items(), key=lambda item: entries.get(item[0], {}).get("seen", 0))
    for relative, (path, size, mtime) in ordered:
        if relative not in entries and len(entries) >= MAX_CACHED_FILES:
            complete = False
            errors.add("cache_capacity")
            continue
        entry = entries.setdefault(relative, {"offset": 0})
        if size < entry["offset"] or (entry.get("mtime") != mtime and entry["offset"] >= size):
            entry.clear()
            entry["offset"] = 0
        if entry["offset"] >= size:
            if entry.get("invalid") or entry.get("unknown_cwd"):
                complete = False
            continue
        if touched >= MAX_FILES or remaining <= 0 or monotonic() >= deadline:
            complete = False
            continue
        touched += 1
        entry["seen"] = time.time()
        try:
            with path.open("rb") as handle:
                # Identity probes spend the same byte budget as record parsing. The shared
                # efficiency head helper permits a 4 MiB first line, too large per probe here.
                head_raw = handle.readline(min(MAX_RECORD, remaining) + 1)
                remaining -= len(head_raw)
                if remaining < 0 or (head_raw and not head_raw.endswith(b"\n")):
                    complete = False
                    continue
                head = hashlib.sha256(head_raw).hexdigest()[:16] if head_raw else None
                if entry.get("head") and entry["head"] != head:
                    entry.clear()
                    entry["offset"] = 0
                entry["head"] = head
                handle.seek(entry["offset"])
                while remaining > 0 and monotonic() < deadline:
                    raw = handle.readline(min(MAX_RECORD, remaining) + 1)
                    if not raw:
                        break
                    remaining -= len(raw)
                    if not raw.endswith(b"\n") or len(raw) > MAX_RECORD or remaining < 0:
                        complete = False
                        break
                    entry["offset"] += len(raw)
                    try:
                        record = json.loads(raw)
                        if isinstance(record, dict):
                            _record(entry, relative.split("-", 1)[0], record)
                    except (ValueError, TypeError, AttributeError):
                        entry["invalid"] = True
            entry["mtime"] = mtime
        except OSError:
            complete = False
        if entry["offset"] < size or entry.get("invalid") or entry.get("unknown_cwd"):
            complete = False
    configured = set()
    configuration_complete = True
    for path in config.git_repos:
        if monotonic() >= deadline:
            complete = configuration_complete = False
            break
        configured.add(path.resolve())
    count = 0
    roots = {entry["root_cwd"] for entry in entries.values() if entry.get("root_cwd") and not entry.get("child")}
    checked = set()
    for attempt, cwd in enumerate(sorted(roots)):
        if not configuration_complete or attempt >= MAX_GIT or monotonic() >= deadline:
            complete = False
            break
        try:

            def git(*args):
                return subprocess.run(
                    ["git", "-C", cwd, *args],
                    env=GIT_ENV,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=max(0.01, min(1, deadline - monotonic())),
                )

            top = git("rev-parse", "--show-toplevel")
            if top.returncode:
                # Absent checkout is unknown; a present non-git directory is ineligible.
                if not Path(cwd).is_dir() or "not a git repository" not in top.stderr:
                    complete = False
                continue
            root = Path(top.stdout.strip()).resolve()
            if root in checked or root in configured:
                continue
            checked.add(root)
            origin = git("remote", "get-url", "origin")
            slug = repo_slug(origin.stdout.strip()) if origin.returncode == 0 else None
            if repo_admitted(slug, config.identities.git_owners, config.identities.git_repos):
                count += 1
        except (OSError, ValueError, subprocess.TimeoutExpired):
            complete = False
    return {
        "at": time.time(),
        "config": fingerprint(config),
        "count": count,
        "complete": complete,
        "inventory_errors": sorted(errors),
    }


def families(summary):
    return (
        Family(
            "agent_history_loop_checkout_unconfigured",
            "gauge",
            "Observed allowlisted loop checkouts absent from git.repos; a lower bound while observation is partial.",
            (Sample((), summary["count"]),),
        ),
        Family(
            "agent_history_loop_checkout_observation_complete",
            "gauge",
            "Whether configured transcript inventory and loop checkout observation completed within its budget.",
            (Sample((), int(summary["complete"])),),
        ),
    )


def _fields(summary):
    return {
        "loop_checkout_observation": "complete" if summary["complete"] else "partial",
        "warnings": [{"step": "loop_checkout", "warning": WARNING, "count": summary["count"]}]
        if summary["count"]
        else [],
    }


def collect(config):
    """Hourly local observation plus a direct OTLP bridge on the existing collector lifecycle.

    No shared filesystem with the remote indexer is required. Without an existing OTLP
    endpoint, the bridge is disabled and the CLI summary still reports its observation.
    """
    from .archive import DiscoveryBudget
    from .efficiency import EfficiencyCollector, _save
    from .otlp import Bridge

    config = local_config(config)
    if not config.sources:
        return {"loop_checkout_observation": "unavailable", "warnings": []}
    budget = DiscoveryBudget(
        deadline=time.monotonic() + BUDGET_SECONDS,
        monotonic=time.monotonic,
        max_entries=MAX_INVENTORY_ENTRIES,
        max_dirs=MAX_INVENTORY_DIRS,
        max_files=MAX_INVENTORY_FILES,
        max_memory=MAX_INVENTORY_MEMORY,
    )
    path = config.exporter.state_dir / "loop-checkout-state.json"
    state = parser.read_state(path)
    scope = sorted([key, str(home.absolute())] for key, home in config.sources.items())
    if state.get("source_scope") != scope:
        # Cache evidence from a removed/reconfigured root is no longer authorised to probe.
        state = {"source_scope": scope}
    inventory = EfficiencyCollector(config, config.exporter.state_dir).inventory(budget=budget)
    cutoff = (time.time() - config.efficiency.first_parse_days * 86400) * 1_000_000_000
    summary = observe(
        config,
        {key: value for key, value in inventory.items() if value[2] >= cutoff},
        state,
        monotonic=budget.monotonic,
        deadline=budget.deadline,
        inventory_complete=budget.complete,
        inventory_errors=budget.errors,
    )
    _save(path, state)
    _save(config.exporter.state_dir / "loop-checkout-summary.json", summary)
    bridge = Bridge.create()
    if bridge is not None:
        bridge.publish(families(summary))
    return _fields(summary)


def summary_fields(config):
    """Read a local metadata snapshot for diagnostics, preserving unknown/stale semantics."""
    try:
        summary = json.loads((config.exporter.state_dir / "loop-checkout-summary.json").read_text())
        if summary["config"] != fingerprint(config) or not 0 <= time.time() - summary["at"] <= MAX_AGE:
            raise ValueError("stale observation")
        count = summary["count"]
        if type(count) is not int or count < 0 or type(summary["complete"]) is not bool:
            raise ValueError("invalid observation")
        return _fields(summary)
    except (OSError, ValueError, KeyError, TypeError):
        return {"loop_checkout_observation": "unavailable", "warnings": []}
