#!/bin/bash
# Two-process supervisor for the Collabora-with-UI image.
#
# Children:
#   1. coolwsd  (Collabora's editor backend)        — listens 127.0.0.1:9980
#   2. hypercorn → Quart UI + WOPI host + proxy    — listens 0.0.0.0:8080
#
# Why bash and not supervisord/s6:
#   This is a two-process supervisor that exists only to forward SIGTERM
#   and exit when either child dies.  A 30-line bash script is easier to
#   read and audit than another tool's config file, and there is no
#   restart logic to get wrong.  If either child crashes the whole
#   container exits and OpenHost reschedules.
#
# coolwsd's start-collabora-online.sh is preserved as the launcher for
# child 1 because we still want its ssl-cert-skip logic and "$extra_params"
# contract.  We override its config via $extra_params so it binds to
# loopback only, since the Quart proxy is what's exposed to the world.
#
# Bash strict mode keeps a missing executable from silently degrading the
# container into a half-working state.

set -euo pipefail

ZONE_DOMAIN="${OPENHOST_ZONE_DOMAIN:-localhost}"
APP_NAME="${OPENHOST_APP_NAME:-collabora}"
SERVER_NAME="${APP_NAME}.${ZONE_DOMAIN}"

# WOPI hosts permitted to proxy through the editor.  The default permits
# our own UI (which lives on the same hostname).  Override WOPI_HOST_REGEX
# in the OpenHost dashboard to additionally allow remote hosts.
ZONE_DOMAIN_REGEX_ESCAPED="$(printf '%s' "${ZONE_DOMAIN}" | sed 's/\./\\./g')"
WOPI_HOST_REGEX="${WOPI_HOST_REGEX:-https://[^/]+\\.${ZONE_DOMAIN_REGEX_ESCAPED}}"

# coolwsd extra params:
#   server_name     — what coolwsd publishes in /hosting/discovery URLs.
#                     Has to match what the user's browser sees, since
#                     that's the URL the editor iframe connects back to.
#   ssl.enable      — false; we do plain HTTP between Quart and coolwsd
#                     (loopback) and between OpenHost router and Quart.
#   ssl.termination — true; coolwsd builds outbound URLs as https,
#                     reflecting what the public-facing TLS terminator
#                     will serve.
#   net.listen      — loopback so coolwsd can't be reached except through
#                     Quart's proxy.  Defence in depth — the OpenHost
#                     router only publishes 8080 anyway.
#   security.{cap,seccomp}=false — rootless OpenHost can't grant
#                     CAP_SYS_ADMIN or load custom seccomp profiles.
export extra_params="
--o:server_name=${SERVER_NAME}
--o:ssl.enable=false
--o:ssl.termination=true
--o:net.proto=IPv4
--o:net.listen=loopback
--o:storage.wopi.alias_groups[@mode]=groups
--o:storage.wopi.alias_groups.group[1].host[@allow]=true
--o:storage.wopi.alias_groups.group[1].host=http://127\\.0\\.0\\.1:8080
--o:storage.wopi.alias_groups.group[2].host[@allow]=true
--o:storage.wopi.alias_groups.group[2].host=${WOPI_HOST_REGEX}
--o:security.capabilities=false
--o:security.seccomp=false
--o:logging.level=warning
"

# ----------------------------------------------------------------------
# Start child 1: coolwsd via the upstream launcher.
# ----------------------------------------------------------------------

/start-collabora-online.sh &
COOLWSD_PID=$!
echo "[entrypoint] coolwsd pid=${COOLWSD_PID}"

# ----------------------------------------------------------------------
# Start child 2: Quart UI + proxy.
# ----------------------------------------------------------------------

cd /opt/openhost-app
exec_hypercorn() {
    # `exec` so SIGTERM hits hypercorn directly, not the bash wrapper.
    exec /opt/openhost-venv/bin/hypercorn \
        --bind 0.0.0.0:8080 \
        --access-logfile - \
        --error-logfile - \
        --workers 1 \
        server:app
}

exec_hypercorn &
QUART_PID=$!
echo "[entrypoint] quart pid=${QUART_PID}"

# ----------------------------------------------------------------------
# Forward SIGTERM and wait for either child to exit.
# ----------------------------------------------------------------------

shutdown() {
    echo "[entrypoint] SIGTERM received, stopping children"
    kill -TERM "${COOLWSD_PID}" "${QUART_PID}" 2>/dev/null || true
    wait "${COOLWSD_PID}" "${QUART_PID}" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# `wait -n` returns when the first child exits.  Whichever it is, we
# tear down the other and exit; OpenHost reschedules.
wait -n "${COOLWSD_PID}" "${QUART_PID}"
EXIT_CODE=$?
echo "[entrypoint] a child exited (code ${EXIT_CODE}); shutting down"
shutdown
