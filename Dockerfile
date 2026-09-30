FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir . && \
    groupadd --system --gid 10001 agent && useradd --system --uid 10001 --gid agent --home-dir /nonexistent agent && \
    mkdir -p /state /var/lib/alloy/textfile-agent-history && \
    chown agent:agent /state /var/lib/alloy/textfile-agent-history
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["agent-history"]
CMD ["exporter"]
