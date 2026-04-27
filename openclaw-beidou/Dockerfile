FROM docker:cli AS docker-cli
FROM ghcr.io/openclaw/openclaw:latest

# Switch to root for system package installation
USER root

# Docker CLI for Docker-out-of-Docker sandbox (socket mounted at runtime)
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Proxy relay: forward 127.0.0.1:7891 -> host.docker.internal:7890
# Discord proxy config must point to loopback; this relay satisfies that requirement
RUN apt-get update \
  && apt-get install -y --no-install-recommends socat \
  && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch back to node user
USER node

EXPOSE 18789

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
