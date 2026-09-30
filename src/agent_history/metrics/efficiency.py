"""Incremental public efficiency collector over configured transcript sources."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from agent_history.config import Config
from agent_history.efficiency import parser as rules
from agent_history.metrics import Family, Sample


class _Series:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str, str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def add(self, name, value, labels=None, help_text="", metric_type="gauge", family=None):
        key = (family or name, name, metric_type, help_text, tuple(sorted((labels or {}).items())))
        self.values[key] += float(value)

    def families(self) -> tuple[Family, ...]:
        collected: dict[tuple[str, str, str], list[Sample]] = defaultdict(list)
        for (family, name, metric_type, help_text, labels), value in self.values.items():
            collected[family, metric_type, help_text].append(Sample(labels, value, name if name != family else None))
        return tuple(
            Family(
                family,
                metric_type,
                help_text,
                tuple(sorted(samples, key=lambda item: (item.labels, item.name or family))),
            )
            for (family, metric_type, help_text), samples in sorted(collected.items())
        )


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EfficiencyCollector:
    """One state file per collector. A complete line is the transaction boundary."""

    name = "efficiency"

    def __init__(self, config: Config, state_dir: Path) -> None:
        self.config = config
        self.state_dir = Path(state_dir)
        self.section_health: dict[str, tuple[float, bool]] = {}

    def _claude_transcript(self, parts: tuple[str, ...]) -> bool:
        """Session transcripts, their direct subagents and, unless disabled, workflow agents."""
        if len(parts) < 3 or parts[0] != "projects":
            return False
        if len(parts) == 3 or (len(parts) == 5 and parts[3] == "subagents"):
            return True
        # Workflow journals only record agent state changes; they are not transcripts.
        return (
            self.config.efficiency.workflow_transcripts
            and len(parts) == 7
            and parts[3:5] == ("subagents", "workflows")
            and parts[6].startswith("agent-")
        )

    def collect(self) -> tuple[Family, ...]:
        now = time.time()
        path = self.state_dir / "efficiency-state.json"
        state = rules.read_efficiency_state(path, self.config.efficiency.baseline_ts)
        sources: dict[str, tuple[Path, int, int]] = {}
        for namespace, home in self.config.sources.items():
            # A missing home must not erase its offsets. Reject symlinked transcript files.
            if not home.is_dir():
                continue
            for file in home.rglob("*.jsonl"):
                try:
                    if file.is_symlink() or "subagent-artifacts" in file.parts or not file.is_file():
                        continue
                    stat = file.stat()
                except OSError:  # An active transcript can disappear between directory and file reads.
                    continue
                parts = file.relative_to(home).parts
                agent = namespace.split("-", 1)[0]
                if agent == "claude" and not self._claude_transcript(parts):
                    continue
                # archived_sessions holds moved copies of rollouts already counted from sessions/.
                if agent == "codex" and parts[0] != "sessions":
                    continue
                if agent == "pi" and (
                    parts[0] != "sessions"
                    or not (len(parts) == 3 or (len(parts) >= 6 and parts[-2].startswith("run-")))
                ):
                    continue
                rel = namespace + "/" + file.relative_to(home).as_posix()
                sources[rel] = (file, stat.st_size, stat.st_mtime_ns)
        rules.efficiency_prune_loops(state, now)
        fresh_loop_map = None
        self.section_health = {}
        loop_started = time.monotonic()
        loop_failed = True  # Like the legacy optional section, no DSN means no successful fetch.
        if self.config.efficiency.loop_dsn:
            try:
                import psycopg

                with psycopg.connect(
                    self.config.efficiency.loop_dsn,
                    connect_timeout=5,
                    options="-c statement_timeout=10000 -c default_transaction_read_only=on",
                ) as db:
                    roots = db.execute(rules.LOOP_ROOTS_SQL).fetchall()
                    members = db.execute(rules.LOOP_MEMBERS_SQL).fetchall()
                fresh_loop_map = rules.build_loop_map(roots, members, now)
                loop_failed = False
            except Exception:
                # The catalogue is optional: stale mapping is bounded by its recorded age.
                pass
        self.section_health["loops"] = (time.monotonic() - loop_started, loop_failed)
        loop_map = rules.efficiency_select_loop_map(state, fresh_loop_map, now)
        run = rules.EfficiencyRun(state, rules.LoopMap(loop_map))
        files = state["files"]
        stale_ns = int((now - self.config.efficiency.first_parse_days * 86400) * 1_000_000_000)
        for rel in list(files):
            mtime_ns = sources[rel][2] if rel in sources else int(files[rel].get("mtime_ns") or 0)
            if mtime_ns < stale_ns:
                del files[rel]
        for rel, (file, size, mtime_ns) in sorted(sources.items()):
            entry = files.get(rel)
            if entry is None:
                if mtime_ns < stale_ns:
                    continue
                skip = max(self.config.efficiency.baseline_ts, now - self.config.efficiency.first_parse_days * 86400)
                entry = files[rel] = rules.efficiency_file_state(rel, skip)
            if (entry["size"], entry["mtime_ns"]) == (size, mtime_ns) and entry["offset"] >= size:
                continue
            snapshot = json.dumps(entry)
            try:
                with file.open("rb") as handle:
                    head = rules.efficiency_head(handle)
                    if size < entry["offset"] or (entry["head"] and head and head != entry["head"]):
                        skip = max(float(entry["skip"]), float(entry["counted"] or 0))
                        entry.clear()
                        entry.update(rules.efficiency_file_state(rel, skip))
                    entry["head"] = head
                    handle.seek(entry["offset"])
                    parser = rules.EfficiencyParser(run, entry, rel)
                    for raw in handle:
                        if not raw.endswith(b"\n"):
                            break
                        try:
                            parser.line(raw)
                        except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError):
                            # Like the original collector, consume an unparseable complete record.
                            pass
                        entry["offset"] += len(raw)
                entry["size"], entry["mtime_ns"] = size, mtime_ns
            except OSError:
                entry.clear()
                entry.update(json.loads(snapshot))
                run.rollback()
                continue
            run.commit()
        rules.efficiency_close_lanes(run, files, {k: v[1:] for k, v in sources.items()}, now)
        run.commit()
        state["recent_calls"] = [x for x in state["recent_calls"] if x[0] >= now - rules.EFFICIENCY_ACTIVE_SECONDS]
        state["recent_calls"] = state["recent_calls"][-rules.EFFICIENCY_MAX_RECENT_CALLS :]
        _save(path, state)
        rendered = _Series()
        rules.efficiency_emit(rendered, state, now, run)
        labels = {label for spans in (loop_map or {}).get("roots", {}).values() for _, _, label in spans}
        labels.update((loop_map or {}).get("members", {}).values())
        rendered.add(
            "agent_efficiency_loop_map_loops",
            len(labels),
            help_text="Distinct loops in the catalogue loop map used for the loop label (0 when none is available).",
        )
        if loop_map:
            rendered.add(
                "agent_efficiency_loop_map_age_seconds",
                max(0, now - loop_map["fetched"]),
                help_text="Age of the catalogue loop map used for the loop label; absent while every thread reads none.",
            )
        rendered.add(
            "agent_efficiency_loop_labels",
            len(state["loops"]),
            help_text="Loop label values currently carried by the efficiency series (bounded; excludes none).",
        )
        return rendered.families()
