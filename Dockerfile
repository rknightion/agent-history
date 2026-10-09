# Build against the same Python and libc as the minimal runtime.
FROM cgr.dev/chainguard/python:latest-dev@sha256:7e19f3a10fe87a502e9e1bc39e3fb917281cd72dabec34d70bacbe2d7d6dd63b AS builder
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

FROM cgr.dev/chainguard/python:latest@sha256:e23e5598a0770ec4a9bbc5dbd9f157f097b4825c4508914e36acd33454484535
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
