# ── Stage 1: build and prune the dependency tree ────────────────────────────
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Drop precompiled bytecode to shrink the shipped image. The container rebuilds
# its own cache on first start, so steady-state startup is unaffected.
ARG SLIM_BYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Prune content that never runs in production. litellm/proxy is deliberately
# kept: litellm imports it at module load and removing it breaks every call.
RUN set -eux; \
    SP=/usr/local/lib/python3.14/site-packages; \
    find "$SP" -name '*.so' -exec strip --strip-unneeded {} + 2>/dev/null || true; \
    find "$SP" -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true; \
    find "$SP" -name '*.pyi' -delete 2>/dev/null || true; \
    if [ "$SLIM_BYTECODE" = "1" ]; then \
        find "$SP" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true; \
    fi

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Los_Angeles

WORKDIR /app

# git is required by entrypoint.sh for the self-update pull; gh backs the
# autonomous dev agent's pull-request flow. Set INCLUDE_GH=0 to drop ~37 MB
# if you never use the PR feature.
ARG INCLUDE_GH=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git tzdata; \
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && echo "$TZ" > /etc/timezone; \
    if [ "$INCLUDE_GH" = "1" ]; then \
        apt-get install -y --no-install-recommends curl; \
        install -m 0755 -d /etc/apt/keyrings; \
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
        chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            > /etc/apt/sources.list.d/github-cli.list; \
        apt-get update && apt-get install -y --no-install-recommends gh; \
        apt-get purge -y --auto-remove curl; \
    fi; \
    rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/locale

COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Bake private config into /root/config so the image is self-contained: it can be
# `docker save`d to a tarball, copied to the NAS, and run with no extra files or
# bind mounts. entrypoint.sh restores these into /app on boot.
#
# The trade-off this accepts: credentials live in an image layer and remain
# recoverable from the tarball, so this image must not be pushed to a registry
# or shared. Build with --build-arg EMBED_SECRETS=0 to leave them out and supply
# them at runtime instead (see README).
ARG EMBED_SECRETS=1
RUN mkdir -p /root/config
COPY .env* service_account.json* /root/config/
RUN if [ "$EMBED_SECRETS" != "1" ]; then rm -f /root/config/.env /root/config/service_account.json; fi

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-u", "main.py"]
