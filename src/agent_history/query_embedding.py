"""Query embedding adapters for the reader's legacy environment-file interface."""

from __future__ import annotations
import json
import math
import os
import re
import urllib.request
from pathlib import Path

EMBED_ENV_FILE = Path(os.environ["AGENT_HISTORY_EMBED_ENV"]) if os.environ.get("AGENT_HISTORY_EMBED_ENV") else None
EMBED_DIMS = 1024
# Frozen instruction string for the workers_ai adapter -- must match agent_history/embed.py's
# QUERY_INSTRUCTION exactly (same model family, so the same instruction prefix).
QUERY_INSTRUCTION = "Given a question about past AI coding-agent work, retrieve transcript passages that answer it"
# Short queries and identifier-shaped ones (paths, dotted names, snake_case, camelCase, alnum ids)
# read as keyword search; the bake-off showed fusing vectors in hurts them, so they get a low w_vec.
IDENTIFIER_LIKE = re.compile(r"[_./]|::|--|[a-z][A-Z]|[A-Za-z][0-9]|[0-9][A-Za-z]")


def _load_embed_env() -> dict[str, str] | None:
    """Read the explicitly configured environment file, or return None if unavailable."""
    if EMBED_ENV_FILE is None or not EMBED_ENV_FILE.is_file():
        return None
    env: dict[str, str] = {}
    try:
        for line in EMBED_ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return None
    return env


def _normalise_vector(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in vec) + "]"


def _embed_post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    # A User-Agent is required: Cloudflare's bot filter answers Python-urllib with 403 / error 1010.
    base = {
        "Content-Type": "application/json",
        "User-Agent": "agent-history-cli/1",
        "cf-aig-collect-log-payload": "false",
        "cf-aig-skip-cache": "true",
    }
    base.update(headers)
    req = urllib.request.Request(url, data=data, headers=base, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def query_vector(text: str, timeout: float = 3.0) -> str | None:
    """Legacy reader provider adapters. Configuration is entirely environment-file driven.

    EMBED_BASE_URL is the provider-specific base URL for the gateway's OpenAI or Cohere route.
    The Workers AI adapter uses the official API and optional CF_AIG_GATEWAY_ID.
    Missing configuration and provider errors fall back to BM25, as before.
    """
    env = _load_embed_env()
    if not env:
        return None
    try:
        provider = env.get("EMBED_PROVIDER", "openai")
        model = env["EMBED_MODEL"]
        token = env["CF_AIG_TOKEN"]
        clipped = text[:12000]
        # EMBED_BYOK_ALIAS picks a non-default stored provider key in the gateway (cf-aig-byok-alias).
        alias = {"cf-aig-byok-alias": env["EMBED_BYOK_ALIAS"]} if env.get("EMBED_BYOK_ALIAS") else {}
        if provider == "openai":
            # No Authorization header: the gateway uses the stored provider key (BYOK).
            out = _embed_post(
                env["EMBED_BASE_URL"].rstrip("/") + "/embeddings",
                {"model": model, "input": [clipped], "dimensions": EMBED_DIMS},
                {"cf-aig-authorization": f"Bearer {token}", **alias},
                timeout,
            )
            vec = out["data"][0]["embedding"]
        elif provider == "cohere":
            out = _embed_post(
                env["EMBED_BASE_URL"].rstrip("/") + "/embed",
                {
                    "model": model,
                    "texts": [clipped],
                    "input_type": "search_query",
                    "embedding_types": ["float"],
                    "output_dimension": EMBED_DIMS,
                },
                {"cf-aig-authorization": f"Bearer {token}", **alias},
                timeout,
            )
            vec = out["embeddings"]["float"][0]
        elif provider == "workers_ai":
            account = env.get("CF_ACCOUNT_ID", "")
            out = _embed_post(
                f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
                {"queries": clipped, "instruction": QUERY_INSTRUCTION},
                {"Authorization": f"Bearer {token}", "cf-aig-gateway-id": env.get("CF_AIG_GATEWAY_ID", "default")},
                timeout,
            )
            vec = (out.get("result") or {}).get("data")[0]
        else:
            return None
        return _vector_literal(_normalise_vector([float(x) for x in vec][:EMBED_DIMS]))
    except Exception:
        return None
