"""Explicit harness context windows; unknown identifiers have no default."""

import re


# Owner policy 2026-09-30: Opus, Sonnet and Fable use 1M; Haiku 4.5 uses 200K.
# Bare public identifiers (including date suffixes) do not need a [1m] marker.
CLAUDE_CONTEXT_WINDOWS: dict[str, int] = {
    "opus": 1000000,
    "sonnet": 1000000,
    "fable": 1000000,
    "haiku-4-5": 200000,
}


def claude_context_window(model: object) -> int | None:
    """Resolve only owner-approved public model families, never a fallback."""
    if not isinstance(model, str):
        return None
    identifier = model.removesuffix("[1m]")
    match = re.fullmatch(r"claude-(opus|sonnet|fable)-[a-z0-9]+(?:-[a-z0-9]+)*", identifier)
    if match:
        return CLAUDE_CONTEXT_WINDOWS[match[1]]
    if re.fullmatch(r"claude-haiku-4-5(?:-[a-z0-9]+)*", identifier):
        return CLAUDE_CONTEXT_WINDOWS["haiku-4-5"]
    return None


PI_CONTEXT_WINDOWS: dict[str, int] = {
    # pi overlay models.json: providers.openai.models[id=gpt-6.1-sol].contextWindow.
    "gpt-6.1-sol": 700000,
    # pi overlay models.json: providers.openai.modelOverrides.gpt-6-luna.contextWindow.
    "gpt-6-luna": 700000,
    # pi overlay models.json: providers.openai.modelOverrides.gpt-6-astra.contextWindow.
    "gpt-6-astra": 700000,
}
