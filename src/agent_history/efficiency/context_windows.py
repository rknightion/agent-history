"""Explicit pi context windows; unknown identifiers have no default.

These are configuration values, not inferred limits for other harnesses.
"""

PI_CONTEXT_WINDOWS: dict[str, int] = {
    # pi overlay models.json: providers.openai.models[id=gpt-6.1-sol].contextWindow.
    "gpt-6.1-sol": 700000,
    # pi overlay models.json: providers.openai.modelOverrides.gpt-6-luna.contextWindow.
    "gpt-6-luna": 700000,
    # pi overlay models.json: providers.openai.modelOverrides.gpt-6-astra.contextWindow.
    "gpt-6-astra": 700000,
}
