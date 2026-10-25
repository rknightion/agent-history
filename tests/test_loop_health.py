"""Completed-loop gauges must omit unknowns and never carry run identities."""

import runpy
from pathlib import Path

from agent_history.metrics.loop_health import families
from agent_history.metrics.collection import State, exposition


def test_health_unknown_is_not_zero_and_labels_are_bounded(tmp_path):
    assert families([]) == []
    rows = [
        dict(
            repo="example/project",
            harness="pi",
            completed_timestamp_seconds=123,
            root_model_time_share=None,
            root_model_timed_calls=0,
            root_model_calls=2,
            root_messages=10,
            owner_questions_per_1k_root_messages=100,
            owner_parks_per_1k_root_messages=0,
            ratification_rulings=0,
            lane_complete_rate=None,
            tasks_done=0,
            estimated_cost_usd=None,
        )
    ]
    text = exposition(families(rows), State(tmp_path))
    assert 'agent_history_loop_health_tasks_done{harness="pi",repo="example/project"} 0' in text
    assert "agent_history_loop_health_owner_parks_per_1k_root_messages{" in text
    for unknown in ("root_model_time_share", "lane_complete_rate", "estimated_cost_usd"):
        assert f"agent_history_loop_health_{unknown}{{" not in text
    assert 'loop="' not in text and 'session="' not in text
    assert "agent_history_loop_health_root_model_calls{" in text


def test_generated_health_queries_use_the_exported_names_and_both_filters():
    from test_archive_dashboard import SELECTOR, STORED

    root = Path(__file__).resolve().parents[1]
    dashboard = runpy.run_path(str(root / "grafana" / "build_catalogue_dashboard.py"))["DASH"]
    elements = dashboard["spec"]["elements"]
    health = [element for key, element in elements.items() if key.startswith("health_")]
    assert health, "the generated catalogue dashboard has no loop health row"
    observed = set()
    for element in health:
        spec = element["spec"]
        if "owner questions" in spec["title"] or "owner parks" in spec["title"]:
            assert "heuristic" in spec["description"].lower()
        refs = []
        assert not spec["data"]["spec"]["transformations"]
        for query in spec["data"]["spec"]["queries"]:
            data = query["spec"]["query"]["spec"]
            # Real repository names remain visible, even in presentation mode. Neither
            # PromQL nor panel transforms may replace repo with a name-derived alias.
            assert data["legendFormat"].startswith("{{repo}} / {{harness}}")
            assert "label_replace" not in data["expr"]
            refs.append(query["spec"]["refId"])
            for name, selector in SELECTOR.findall(data["expr"]):
                assert name in STORED, name
                assert 'repo=~"${health_repo:regex}"' in selector
                assert 'harness=~"${health_harness:regex}"' in selector
                assert 'job="agent-history-index"' in selector
                observed.add(STORED[name])
        assert len(refs) == len(set(refs))
    assert "agent_history_loop_health_estimated_cost_usd" in observed
    assert "agent_history_loop_health_root_model_time_share" in observed
    variables = {v["spec"]["name"] for v in dashboard["spec"]["variables"]}
    assert {"health_repo", "health_harness"} <= variables
