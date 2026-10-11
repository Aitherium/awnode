FROM python:3.12-slim

LABEL org.opencontainers.image.title="awnode" \
      org.opencontainers.image.description="Lightweight local gateway and MCP server for AitherOS" \
      org.opencontainers.image.vendor="Aitherium" \
      org.opencontainers.image.url="https://aitherium.com" \
      org.opencontainers.image.source="https://github.com/Aitherium/aither" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AITHERNODE_PORT=8090

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    openssh-server \
    && rm -rf /var/lib/apt/lists/*

# Cloud-GPU providers (Vast.ai --ssh --direct, etc.) require the box to be
# SSH-reachable once running. Root login via key only — no password auth.
RUN sed -i \
    -e 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' \
    -e 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' \
    -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
    /etc/ssh/sshd_config

COPY awnode/README.md awnode/pyproject.toml ./
COPY awnode/awnode ./awnode
COPY awnode/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir .

EXPOSE 8090 22

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${AITHERNODE_PORT}/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "awnode"]
CMD ["start", "--host", "0.0.0.0", "--port", "8090"]
