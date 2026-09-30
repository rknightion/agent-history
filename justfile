set shell := ["bash", "-euo", "pipefail", "-c"]

# renovate: datasource=docker depName=paradedb/paradedb
paradedb_image := "paradedb/paradedb:18-v0.25.10@sha256:188591a0bc317beb2c6d6d3f9ef0cb3e859d09ecc15a71dda5e9a027876686cf"

# List the recipes
default:
    @just --list

# Create the dev virtualenv and install the leak-gate git hooks
setup: hooks
    uv sync --locked --group dev

# Format the package's own Python files and the justfile
[group('dev')]
fmt:
    uv run --locked --group dev ruff format .
    just --fmt

# Check formatting without changing anything
[group('check')]
fmt-check:
    uv run --locked --group dev ruff format --check .
    just --fmt --check

# Lint Python and the justfile
[group('check')]
lint:
    uv run --locked --group dev ruff check .
    just --dump --dump-format json > /dev/null

# Unit tests (database tests skip without AGENT_HISTORY_TEST_DSN; `just ci` runs them)
[group('check')]
test:
    uv run --locked --group dev python -m pytest -q
    python3 -m unittest discover -s bin -p 'test_leak_scan.py'

# The pre-commit gate: format, lint, tests and the leak gate
[group('check')]
check: fmt-check lint test leak

# check plus the legs that need a Docker daemon
[group('check')]
ci: check pg-test

# Needs a Docker daemon: the database tests against a disposable ParadeDB container
[group('check')]
pg-test:
    #!/usr/bin/env bash
    set -euo pipefail
    name="agent-history-test-$$"
    admin="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    writer="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    reader="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    docker run -d --rm --name "$name" -e POSTGRES_PASSWORD="$admin" -e POSTGRES_DB=agent_history_test \
        -p 127.0.0.1::5432 "{{ paradedb_image }}" > /dev/null
    trap 'docker rm -f -v "$name" > /dev/null 2>&1 || true' EXIT
    for _ in $(seq 1 90); do
        docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d agent_history_test > /dev/null 2>&1 && break
        sleep 1
    done
    docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d agent_history_test > /dev/null
    docker exec -i "$name" psql -q -X -U postgres -d agent_history_test \
        -v writer_password="$writer" -v reader_password="$reader" < src/agent_history/sql/roles.sql
    port="$(docker port "$name" 5432/tcp | head -n 1 | sed 's/.*://')"
    base="127.0.0.1:${port}/agent_history_test"
    AGENT_HISTORY_TEST_DSN="postgresql://ah_writer:${writer}@${base}" \
    AGENT_HISTORY_TEST_READER_DSN="postgresql://ah_reader:${reader}@${base}" \
    AGENT_HISTORY_TEST_ADMIN_DSN="postgresql://postgres:${admin}@${base}" \
    AGENT_HISTORY_CONFIG=/dev/null/absent \
        uv run --locked --group dev python -m pytest -q -rs tests

# Regenerate the synthetic pi session fixtures
[group('gen')]
gen-fixtures:
    python3 tests/fixtures/pi/generate.py

# Install the leak-gate git hooks from hooks/
[group('dev')]
hooks:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git config --get core.hooksPath || true)" ]; then echo "core.hooksPath is set; unset it" >&2; exit 1; fi
    dir="$(git rev-parse --git-path hooks)"
    mkdir -p "$dir"
    for hook in pre-commit commit-msg pre-push; do install -m 0755 "hooks/$hook" "$dir/$hook"; done

# Scan the tree and every reachable commit for forbidden terms; verify the installed hooks
[group('check')]
leak:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git config --get core.hooksPath || true)" ]; then echo "core.hooksPath is set; unset it" >&2; exit 1; fi
    dir="$(git rev-parse --git-path hooks)"
    for hook in pre-commit commit-msg pre-push; do
      cmp -s "hooks/$hook" "$dir/$hook" && [ -x "$dir/$hook" ] || { echo "hook $hook is missing or differs from hooks/$hook; run just setup" >&2; exit 1; }
    done
    # CI sets LEAK_SCAN_PUBLIC_ONLY=1 for fork pull requests, which get no secrets; the scan says so.
    flag=()
    if [ "${LEAK_SCAN_PUBLIC_ONLY:-}" = "1" ]; then flag=(--public-only); fi
    python3 bin/leak-scan --path . ${flag[@]+"${flag[@]}"}
    python3 bin/leak-scan --history ${flag[@]+"${flag[@]}"}
