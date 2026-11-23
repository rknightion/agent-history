# Build against the same Python and libc as the minimal runtime.
FROM cgr.dev/chainguard/python:latest-dev@sha256:e2736acdf97f2b1cb59545f4f26317b18a953e90e59fa4c212a782315b248111 AS builder
USER 0
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
# Use the builder's pip to populate a pip-free virtualenv.
RUN python -c 'import sys; assert sys.version_info[:2] == (3, 15)' && \
    python -m venv --without-pip /opt/venv && \
    python -m pip --python /opt/venv install --no-cache-dir ".[otel]" && \
    PYTHONTZPATH="" /opt/venv/bin/python -c 'from zoneinfo import ZoneInfo; ZoneInfo("Europe/London"); import agent_history.collect_git' && \
    mkdir -p /state /var/lib/alloy/textfile-agent-history && \
    chown 10001:10001 /state /var/lib/alloy/textfile-agent-history

FROM cgr.dev/chainguard/python:latest@sha256:f33049a06b10d6bd729e08a181094774ca5e9c7b0cf1bbe674aa977cd5258daa
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
COPY --from=builder --chown=10001:10001 /state /state
COPY --from=builder --chown=10001:10001 /var/lib/alloy/textfile-agent-history /var/lib/alloy/textfile-agent-history
USER 10001:10001
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["agent-history"]
CMD ["--help"]
