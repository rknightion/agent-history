"""Synthetic transcripts exercise context fill through the public collector."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_history.config import parse_config
from agent_history.efficiency.parser import EfficiencyParser, EfficiencyRun, efficiency_file_state
from agent_history.metrics.efficiency import EfficiencyCollector


@pytest.mark.parametrize("model", [{"id": "gpt-6.1-sol"}, ["gpt-6.1-sol"]])
def test_malformed_pi_model_has_no_context_window(model):
    state = efficiency_file_state("pi-local/sessions/synthetic.jsonl", 0)
    state["model"] = model
    parser = EfficiencyParser(EfficiencyRun({"baseline_ts": 0}), state, "pi-local/sessions/synthetic.jsonl")
    assert parser.context_window() is None


@pytest.mark.parametrize(
    "agent,model,window",
    [
        ("pi", "gpt-6.1-sol", 700000),
        ("pi", "gpt-6-luna", 700000),
        ("pi", "gpt-6-astra", 700000),
        ("pi", "unknown-model", None),
        ("pi", None, None),
        ("codex", "gpt-6.1-sol", 100000),
        ("codex", "gpt-6.1-sol", None),
        ("claude", "claude-opus-4-6", None),
    ],
)
def test_context_fill_public_collector(tmp_path, agent, model, window):
    home = tmp_path / agent
    directory = home / ("projects" if agent == "claude" else "sessions") / "synthetic"
    directory.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    records = []
    for index, tokens in enumerate((70000, 140000, 210000)):
        timestamp = (now - timedelta(seconds=30 - index)).isoformat()
        if agent == "codex":
            if index == 0:
                records.append({"type": "turn_context", "timestamp": timestamp, "payload": {"model": model}})
                records.append(
                    {
                        "type": "event_msg",
                        "timestamp": timestamp,
                        "payload": {"type": "task_started", "model_context_window": window},
                    }
                )
            record = {
                "type": "token_usage_record",
                "timestamp": timestamp,
                "payload": {"usage": {"input_tokens": tokens, "output_tokens": 1}},
            }
        else:
            usage = (
                {"input": tokens - 10000, "cacheRead": 10000, "output": 1}
                if agent == "pi"
                else {"input_tokens": tokens, "output_tokens": 1}
            )
            record = {
                "type": "message" if agent == "pi" else "assistant",
                "timestamp": timestamp,
                "message": {"id": f"call-{index}", "role": "assistant", "usage": usage, "content": []},
            }
            if model is not None:
                record["message"]["model"] = model
        records.append(record)
    (directory / "synthetic.jsonl").write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    )
    config = parse_config({"sources": {f"{agent}-local": str(home)}, "efficiency": {"baseline_ts": 0}})
    families = EfficiencyCollector(config, tmp_path / "state").collect()
    calls = [
        sample for family in families if family.name == "agent_efficiency_llm_calls_total" for sample in family.samples
    ]
    assert sum(sample.value for sample in calls) == 3
    fill = [family for family in families if family.name == "agent_efficiency_context_fill_ratio"]
    if window is None:
        assert fill == []
    else:
        assert len(fill) == 1
        assert fill[0].help == "Input tokens over the model context window per model call in the last 10 minutes."
        assert [(dict(sample.labels), sample.value) for sample in fill[0].samples] == [
            (
                {"agent": agent, "namespace": f"{agent}-local", "role": "solo", "quantile": quantile, "loop": "none"},
                pytest.approx(tokens / window),
            )
            for quantile, tokens in (("0.5", 140000), ("0.9", 210000))
        ]
