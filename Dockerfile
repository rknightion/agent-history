# Build against the same Python and libc as the minimal runtime.
FROM cgr.dev/chainguard/python:latest-dev@sha256:8a5780972067cfba4592b10e786a889807ed876c1100f89410fd0ed4e398992c AS builder
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

FROM cgr.dev/chainguard/python:latest@sha256:60e3c0d33ab77e50ace0850ccdf6eaec825ce89dad9b1ec2b0300a2070892b05
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
