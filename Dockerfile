# syntax=docker/dockerfile:1.4
#
# Collabora Online (CODE) backend with a built-in file manager UI for OpenHost.
#
# Layout inside the container:
#   - coolwsd (collabora editor backend) on 127.0.0.1:9980  [loopback only]
#   - Quart UI + WOPI host on 0.0.0.0:8080                  [exposed]
#   - openhost-entrypoint.sh starts both, forwards SIGTERM
#
# The Quart app reverse-proxies /browser/ /cool/ /lool/ /hosting/ to the
# loopback coolwsd, so the user-visible URL is a single origin.

FROM collabora/code:25.04.9.4.1

# ----------------------------------------------------------------------
# Build-time additions: Python runtime + Quart app + blank-doc templates.
# ----------------------------------------------------------------------

USER 0

# Python + pip plus the few Quart/proxy deps.  We install via the distro's
# package manager rather than pip-from-source to keep the image small and
# avoid dragging in a compiler.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Isolate Quart deps in a virtualenv so they don't collide with any system
# python the upstream image expects.
RUN python3 -m venv /opt/openhost-venv \
 && /opt/openhost-venv/bin/pip install --no-cache-dir \
        "quart>=0.19,<0.21" \
        "hypercorn>=0.17,<0.18" \
        "httpx>=0.27,<0.29" \
        "websockets>=13,<15"

# Build the blank-document seeds the UI ships as "+ New …" templates.
# This runs Collabora's bundled headless LibreOffice once at build time;
# results are static .odt/.ods/.odp baked into the image.
COPY scripts/generate-blank-templates.sh /tmp/generate-blank-templates.sh
RUN chmod 0755 /tmp/generate-blank-templates.sh \
 && /tmp/generate-blank-templates.sh \
 && rm /tmp/generate-blank-templates.sh

# Copy the Quart app sources.  The blank_templates symlink points at the
# build-time output so the app finds them via a relative path.
COPY app /opt/openhost-app
RUN ln -s /opt/collabora-blank-templates /opt/openhost-app/blank_templates \
 && chown -R 1001:1001 /opt/openhost-app

COPY openhost-entrypoint.sh /openhost-entrypoint.sh
RUN chmod 0755 /openhost-entrypoint.sh

# ----------------------------------------------------------------------
# Runtime user.  Upstream's "cool" user is uid 1001; we keep that so the
# pre-existing /etc/coolwsd / /opt/cool ownership is honoured.
# ----------------------------------------------------------------------

USER 1001

# DONT_GEN_SSL_CERT skips the start-collabora-online.sh self-signed cert
# generation; OpenHost terminates TLS in Caddy.
ENV DONT_GEN_SSL_CERT=1 \
    PATH="/opt/openhost-venv/bin:$PATH"

# 8080 is the Quart UI / proxy.  Coolwsd's 9980 stays on loopback only.
EXPOSE 8080

ENTRYPOINT ["/openhost-entrypoint.sh"]
