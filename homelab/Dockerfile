# quirm-hermes-agent/homelab/Dockerfile
#
# Builds the Hermes-Agent image for **Quirm**, Leo's research /
# benchmarking / capability-expansion agent (Leonardo of Quirm).
# The build context is the repository root (this fork of
# NousResearch/hermes-agent) — the upstream Python source is right
# there, and we install editable so any local fork patches take effect.
#
# Build (from repo root):
#   docker build -f homelab/Dockerfile \
#     -t ghcr.io/l3ocifer/quirm-hermes-agent:latest \
#     -t ghcr.io/l3ocifer/quirm-hermes-agent:$(git rev-parse --short HEAD) .

# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget tini git \
      build-essential \
      libssl-dev libffi-dev \
      sqlite3 \
      postgresql-client \
      jq ripgrep \
      ffmpeg \
      gnupg lsb-release \
 && rm -rf /var/lib/apt/lists/*

# kubectl matched to current K3s cluster
ARG KUBECTL_VERSION=v1.34.0
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl \
 && chmod +x /usr/local/bin/kubectl

# 1Password CLI for credential lookups
RUN curl -sSfO https://downloads.1password.com/linux/keys/1password.asc \
 && gpg --dearmor 1password.asc \
 && mv 1password.asc.gpg /usr/share/keyrings/1password-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
      > /etc/apt/sources.list.d/1password.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends 1password-cli \
 && rm -rf /var/lib/apt/lists/* 1password.asc

# Install Hermes from the LOCAL fork source (not from PyPI — upstream
# does not publish to PyPI). uv is faster than pip for the install.
# Note: no himalaya here — Quirm doesn't read email; Sancho does.
RUN pip install --no-cache-dir uv

WORKDIR /opt/hermes
COPY . .

RUN uv pip install --system --no-cache "." \
      pgvector \
      psycopg2-binary \
      pandas \
      numpy \
      matplotlib \
      scikit-learn

# Create hermes user
RUN useradd -m -u 1000 -s /bin/bash hermes \
 && mkdir -p /home/hermes/.hermes/workspace /home/hermes/.hermes/skills /data \
 && chown -R hermes:hermes /home/hermes /data

USER hermes
WORKDIR /home/hermes

EXPOSE 3001 9100

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:3001/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["hermes", "gateway", "--config=/home/hermes/.hermes/config.toml", "--bind=0.0.0.0", "--port=3001"]
