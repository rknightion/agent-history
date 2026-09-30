"""Public loader, HTTP exporter and parity CLI contracts at their used boundaries."""

import json
import threading
from urllib.request import urlopen

import pytest

from agent_history.config import ConfigError, load_config, parse_config
from agent_history.metrics import Family, Sample
from agent_history.metrics.archive import ArchiveCollector
from agent_history.metrics.efficiency import EfficiencyCollector
from agent_history.metrics.self import SelfCollector
from agent_history.metrics.server import MetricServer, State, exposition


def test_trusted_labels_through_shared_and_mcp_loader(tmp_path, monkeypatch):
    from agent_history.mcp_server import _config

    path = tmp_path / "config.toml"
    path.write_text('[metrics_labels]\nmachines = ["worker-a"]\nmodels = ["example-model"]\n')
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(path))
    for config in (load_config(path), _config()):
        assert config.metrics_labels.machines == frozenset({"worker-a"})
        assert config.metrics_labels.models == frozenset({"example-model"})
        families = [
            Family(
                "example_calls_total",
                "counter",
                "Calls.",
                (
                    Sample((("model", "example-model"),), 3),
                    Sample((("model", "untrusted-a"),), 4),
                    Sample((("model", "untrusted-b"),), 5),
                ),
            )
        ]
        text = exposition(families, State(tmp_path / "state", config.metrics_labels))
        assert 'model="example-model"} 3' in text
        assert 'model="other"} 9' in text
    # The new optional table must not weaken any existing strict validation.
    for invalid in (
        {"surprise": 1},
        {"exporter": {"collectors": ["not-a-collector"]}},
        {"efficiency": {"surprise": 1}},
        {"sources": {"unknown": "example"}},
    ):
        with pytest.raises(ConfigError):
            parse_config({**invalid, "metrics_labels": {"machines": ["worker-a"]}})


@pytest.mark.parametrize(
    "section", [None, {}, [], "bad", {"models": "bad"}, {"models": ["example-model", 1]}, {"unexpected": []}]
)
def test_absent_empty_malformed_labels_keep_current_output(tmp_path, section):
    data = {} if section is None else {"metrics_labels": section}
    config = parse_config(data)
    family = Family("example", "gauge", "Example.", (Sample((("model", "example-model"),), 7),))
    assert exposition([family], State(tmp_path / "state", config.metrics_labels)) == exposition(
        [family], State(tmp_path / "default")
    )
    assert config.metrics_labels.machines == frozenset()
    assert config.metrics_labels.models == frozenset()


def test_allowlisted_archive_machine_and_configured_zero_namespaces(tmp_path):
    config = parse_config(
        {"sources": {"pi-standalone-worker-a": str(tmp_path / "absent")}, "metrics_labels": {"machines": ["worker-a"]}}
    )
    collector = ArchiveCollector(None, None, None, None, namespaces=config.sources, labels=config.metrics_labels)
    text = exposition(list(collector.collect()), State(tmp_path / "state", config.metrics_labels))
    labels = 'agent="pi",machine="worker-a",namespace="pi-standalone-worker-a",profile="standalone"'
    for tier in ("hot", "cold"):
        assert f'agent_sessions_storage_files{{{labels},tier="{tier}"}} 0' in text
        assert f'agent_sessions_storage_bytes{{{labels},tier="{tier}"}} 0' in text
    assert "storage_oldest_mtime_seconds{" not in text
    assert "storage_newest_mtime_seconds{" not in text


def test_loop_map_failure_is_visible_but_optional_over_http(tmp_path, monkeypatch):
    import psycopg

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("synthetic unavailability")

    monkeypatch.setattr(psycopg, "connect", unavailable)
    config = parse_config({"sources": {}, "efficiency": {"loop_dsn": "synthetic"}})
    health = SelfCollector()
    health.record("loops", 0.01, False)
    last = health.last_success["loops"]
    collectors = [EfficiencyCollector(config, tmp_path / "efficiency"), health]
    with MetricServer(("127.0.0.1", 0), collectors, State(tmp_path / "server")) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            text = urlopen(f"http://127.0.0.1:{server.server_address[1]}/metrics", timeout=5).read().decode()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert 'agent_sessions_metrics_section_success{section="loops"} 0' in text
    assert 'agent_sessions_metrics_section_duration_seconds{section="loops"}' in text
    assert health.last_success["loops"] == last
    assert 'agent_sessions_metrics_section_last_success_timestamp_seconds{section="loops"}' in text
    assert "agent_sessions_metrics_collection_success 1" in text
    assert "agent_sessions_metrics_collection_failures_total 0" in text


def test_storage_and_archive_are_distinct_kept_health_sections(tmp_path):
    with MetricServer(
        ("127.0.0.1", 0), [ArchiveCollector(None, None, None, None), SelfCollector()], State(tmp_path)
    ) as server:
        text = server.metrics()
    for name in ("storage", "archive"):
        assert f'agent_sessions_metrics_section_success{{section="{name}"}} 1' in text


def _capture(tmp_path, name, value, stamp=120, section="loops", counter="agent_efficiency_llm_calls_total"):
    path = tmp_path / name
    path.write_text(
        f"# captured_at {stamp}\n# TYPE {counter} counter\n"
        f'{counter}{{loop="example/loop1"}} {value}\n'
        "# TYPE agent_sessions_metrics_section_success gauge\n"
        f'agent_sessions_metrics_section_success{{section="{section}"}} 1\n'
    )
    return path


def _parity(old, new, *extra):
    from agent_history.cli import main

    return main(["metrics", "parity", "--legacy", str(old), "--new", str(new), *extra])


def test_parity_malformed_url_is_private_at_cli_boundary(tmp_path):
    import subprocess
    import sys

    marker = "synthetic-private-url-marker"
    new = _capture(tmp_path, "new.prom", 1000)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_history.cli",
            "metrics",
            "parity",
            "--legacy",
            f"http://127.0.0.1/{marker} space",
            "--new",
            str(new),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "invalid input"
    assert result.stderr == ""
    assert marker not in result.stdout + result.stderr


@pytest.mark.parametrize("cross_origin", [False, True])
def test_parity_cli_does_not_fetch_redirect_destinations(tmp_path, cross_origin):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import subprocess
    import sys

    requests = []
    destination = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.server.server_port, self.path))
            if self.path == "/given":
                self.send_response(302)
                self.send_header("Location", destination)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"# captured_at 120\n# TYPE example gauge\nexample 1\n")

        def log_message(self, *args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2 if cross_origin else 1)]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    destination = f"http://127.0.0.1:{servers[-1].server_port}/not-given" if cross_origin else "/not-given"
    for thread in threads:
        thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_history.cli",
                "metrics",
                "parity",
                "--legacy",
                f"http://127.0.0.1:{servers[0].server_port}/given",
                "--new",
                str(_capture(tmp_path, "new.prom", 1000)),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    assert requests == [(servers[0].server_port, "/given")]
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "invalid input"
    assert result.stderr == ""


def test_parity_ended_loop_exact_and_otherwise_half_percent(tmp_path, capsys):
    old = _capture(tmp_path, "old.prom", 1000)
    new = _capture(tmp_path, "new.prom", 1004, 121)
    assert _parity(old, new) == 0
    assert _parity(old, new, "--ended-loop", "example/loop1") == 1
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["differences"][0]["class"] == "ended-loop-counter"
    new = _capture(tmp_path, "new.prom", 1006, 121)
    assert _parity(old, new) == 1


@pytest.mark.parametrize("stamp", [180, 181, 119])
def test_parity_refuses_unsynchronised_captures(tmp_path, capsys, stamp):
    assert _parity(_capture(tmp_path, "old.prom", 1000), _capture(tmp_path, "new.prom", 1000, stamp)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "not synchronised"


def test_parity_reports_roster_and_allows_only_self_counter_rebase(tmp_path, capsys):
    old = _capture(tmp_path, "old.prom", 1000, counter="agent_sessions_metrics_collection_runs_total")
    new = _capture(tmp_path, "new.prom", 0, 121, counter="agent_sessions_metrics_collection_runs_total")
    with old.open("a") as out:
        out.write('# TYPE agent_sessions_index_rows gauge\nagent_sessions_index_rows{table="sessions"} 5\n')
    assert _parity(old, new) == 0
    report = json.loads(capsys.readouterr().out)
    # The shipped roster has no not-emitted entries: the SQLite activity families are dropped.
    assert {item["action"] for item in report["rostered"]} == {"dropped", "rebase-allowed"}
    assert report["counts"]["rostered"] == 2
    assert report["counts"]["kept"] == 1


def test_parity_never_rosters_away_loops_or_unknown_missing_families(tmp_path, capsys):
    old = _capture(tmp_path, "old.prom", 1000)
    new = _capture(tmp_path, "new.prom", 1000, 121, section="runs")
    with old.open("a") as out:
        out.write("# TYPE example_missing gauge\nexample_missing 1\n")
    assert _parity(old, new) == 1
    report = json.loads(capsys.readouterr().out)
    assert all(item["action"] == "not-emitted" for item in report["rostered"])
    assert not any(item["name"] in {"loops", "example_missing"} for item in report["rostered"])
    assert {item["class"] for item in report["differences"]} == {"missing-family", "label-values"}


def test_parity_url_fetch_crossing_minute_is_refused(tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace
    from agent_history.metrics import parity

    old = _capture(tmp_path, "old.prom", 1000, 179)
    # Exercise the real HTTP fetch; only the clock is controlled at the process edge.
    with MetricServer(("127.0.0.1", 0), [SelfCollector()], State(tmp_path / "state")) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stamps = iter([179, 180])
        monkeypatch.setattr(parity, "time", SimpleNamespace(time=lambda: next(stamps)))
        try:
            assert _parity(old, f"http://127.0.0.1:{server.server_address[1]}/metrics") == 2
        finally:
            server.shutdown()
            thread.join(timeout=5)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not synchronised"
    assert report["comparison_performed"] is False
    assert report["counts"] == {"kept": 0, "differences": 0, "rostered": 0}


def test_parity_kept_gauges_and_not_emitted_roster(tmp_path, capsys):
    old = tmp_path / "old.prom"
    new = tmp_path / "new.prom"
    old.write_text("# captured_at 120\n# TYPE example_storage_bytes gauge\nexample_storage_bytes 1000\n")
    new.write_text(old.read_text().replace("120", "121").replace("1000", "1006"))
    assert _parity(old, new) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["differences"][0]["class"] == "counter-tolerance"
    roster = tmp_path / "roster.json"
    roster.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "scope": "family",
                        "name": "example_retired",
                        "action": "not-emitted",
                        "reason": "Confirmed absent in the baseline capture.",
                    }
                ],
            }
        )
    )
    new.write_text(old.read_text().replace("120", "121"))
    assert _parity(old, new, "--roster", str(roster)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rostered"][0]["action"] == "not-emitted"
    with old.open("a") as stream:
        stream.write("# TYPE example_retired gauge\nexample_retired 1\n")
    assert _parity(old, new, "--roster", str(roster)) == 1
    assert json.loads(capsys.readouterr().out)["differences"][0]["class"] == "unexpected-emission"


def test_parity_family_types_label_keys_and_escaping(tmp_path, capsys):
    old = tmp_path / "old.prom"
    new = tmp_path / "new.prom"
    old.write_text('# captured_at 120\n# TYPE example gauge\nexample{value="a,b\\"c"} 1\n')
    new.write_text(old.read_text().replace("120", "121"))
    assert _parity(old, new) == 0
    new.write_text(new.read_text().replace("value=", "different="))
    assert _parity(old, new) == 1
    assert "label-keys" in capsys.readouterr().out
    new.write_text(old.read_text().replace("gauge", "counter"))
    assert _parity(old, new) == 1
    assert "family-type" in capsys.readouterr().out


def test_exposition_keeps_legacy_precision(tmp_path):
    families = [
        Family("example_timestamp_seconds", "gauge", "Epoch.", (Sample((), 1790785741.01),)),
        Family("example_seconds_total", "counter", "Time.", (Sample((), 1.2000000476837158),)),
        Family("example_total", "counter", "Count.", (Sample((), 3.0),)),
    ]
    text = exposition(families, State(tmp_path / "state"))
    assert "example_timestamp_seconds 1790785741.01\n" in text
    assert "example_seconds_total 1.20000004768\n" in text
    assert "example_total 3\n" in text


def test_parity_compares_self_timing_gauges_by_shape_only(tmp_path, capsys):
    def capture(name, stamp, duration, success, files):
        path = tmp_path / name
        path.write_text(
            f"# captured_at {stamp}\n"
            "# TYPE agent_sessions_metrics_section_duration_seconds gauge\n"
            f'agent_sessions_metrics_section_duration_seconds{{section="loops"}} {duration}\n'
            "# TYPE agent_sessions_metrics_section_success gauge\n"
            f'agent_sessions_metrics_section_success{{section="loops"}} {success}\n'
            "# TYPE agent_efficiency_tracked_files gauge\n"
            f"agent_efficiency_tracked_files {files}\n"
        )
        return path

    old = capture("old.prom", 120, 0.31, 1, 20)
    assert _parity(old, capture("new.prom", 121, 0.034, 1, 20)) == 0
    capsys.readouterr()
    # Success and data gauges keep the numeric contract.
    assert _parity(old, capture("new.prom", 121, 0.034, 0, 22)) == 1
    families = {item["family"] for item in json.loads(capsys.readouterr().out)["differences"]}
    assert families == {"agent_sessions_metrics_section_success", "agent_efficiency_tracked_files"}


def test_retired_sqlite_activity_families_are_dropped_whether_or_not_emitted(tmp_path, capsys):
    old = _capture(tmp_path, "old.prom", 1000)
    new = _capture(tmp_path, "new.prom", 1000, 121)
    with old.open("a") as out:
        out.write('# TYPE agent_sessions_activity_messages gauge\nagent_sessions_activity_messages{agent="pi"} 5\n')
    assert _parity(old, new) == 0
    report = json.loads(capsys.readouterr().out)
    assert {"agent_sessions_activity_messages"} <= {i["name"] for i in report["rostered"] if i["action"] == "dropped"}
