# Build against the same Python and libc as the minimal runtime.
FROM cgr.dev/chainguard/python:latest-dev@sha256:a876b1000774bdd68322ac020cd813aaf42b52941ca812f4d2f0149059bd0e67 AS builder
USER 0
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
# Use the builder's pip to populate a pip-free virtualenv.
RUN python -c 'import sys; assert sys.version_info[:2] == (3, 14)' && \
    python -m venv --without-pip /opt/venv && \
    python -m pip --python /opt/venv install --no-cache-dir ".[otel]" && \
    mkdir -p /state /var/lib/alloy/textfile-agent-history && \
    chown 10001:10001 /state /var/lib/alloy/textfile-agent-history

FROM cgr.dev/chainguard/python:latest@sha256:b7af1ae90e2fcfb5c32be03908e74d32fdfd64156c2b7c535bd3e497e7846d84
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
